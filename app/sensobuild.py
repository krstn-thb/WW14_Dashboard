from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import socket
import ssl
from http.client import HTTPSConnection
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from app.config import resolve_project_path
from app.sources import AsyncTTLCache


_cache = AsyncTTLCache()
_last_errors: dict[str, str] = {}
_SENSOR_LOCATIONS = {
    1: "Hinten links",
    2: "Vorne links",
    3: "Vorne rechts",
    4: "Hinten rechts",
    5: "Dachmitte",
}


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if math.isfinite(number) else None


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    raw_sensors = payload.get("sensors")
    if not isinstance(raw_sensors, list):
        raise ValueError("SensoBuild hat keine Sensordaten geliefert")

    sensors = []
    for raw in raw_sensors:
        if not isinstance(raw, dict):
            continue
        try:
            sensor_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        if sensor_id not in _SENSOR_LOCATIONS:
            continue
        latest = raw.get("latest") if isinstance(raw.get("latest"), dict) else {}
        history = raw.get("history") if isinstance(raw.get("history"), list) else []
        stride = max(1, math.ceil(len(history) / 120))
        sampled = history[::stride]
        if history and sampled[-1] is not history[-1]:
            sampled.append(history[-1])
        sensors.append({
            "id": sensor_id,
            "location": _SENSOR_LOCATIONS[sensor_id],
            "temperature": _finite_number(latest.get("temperature")),
            "humidity": _finite_number(latest.get("humidity")),
            "measured_at": latest.get("timestamp"),
            "stale": bool(raw.get("stale")),
            "history": [
                {
                    "temperature": _finite_number(point.get("temperature")),
                    "humidity": _finite_number(point.get("humidity")),
                }
                for point in sampled if isinstance(point, dict)
            ],
        })

    return {
        "status": "live",
        "updated_at": payload.get("last_success"),
        "window_start": payload.get("window_start"),
        "window_end": payload.get("window_end"),
        "sensors": sorted(sensors, key=lambda sensor: sensor["id"]),
    }


def _source_context(ca_bundle: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_bundle or None)
    # SensoBuild's private CA has no keyUsage extension. Keep CA and hostname
    # verification, but allow this legacy CA in this request's context only.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


class _AddressedHTTPSConnection(HTTPSConnection):
    """Connect to an IP while retaining the URL host for SNI and TLS checks."""

    def __init__(self, hostname: str, connect_ip: str, context: ssl.SSLContext) -> None:
        super().__init__(hostname, timeout=20, context=context)
        self._connect_ip = str(ipaddress.ip_address(connect_ip))
        self._tls_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._connect_ip, self.port), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _read_bytes(url: str, ca_bundle: str, connect_ip: str, limit: int, accept: str) -> bytes:
    context = _source_context(ca_bundle)
    request = Request(url, headers={"Accept": accept})
    try:
        with urlopen(request, timeout=20, context=context) as response:
            data = response.read(limit + 1)
    except URLError as exc:
        if not connect_ip or not isinstance(exc.reason, socket.gaierror):
            raise
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("SensoBuild benötigt eine HTTPS-Adresse") from exc
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection = _AddressedHTTPSConnection(parsed.hostname, connect_ip, context)
        try:
            connection.request("GET", target, headers={"Accept": accept})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"SensoBuild antwortet mit HTTP {response.status}")
            data = response.read(limit + 1)
        finally:
            connection.close()
    if len(data) > limit:
        raise ValueError("SensoBuild-Antwort ist zu groß")
    return data


def _read_source(url: str, ca_bundle: str, connect_ip: str = "") -> dict[str, Any]:
    data = _read_bytes(url, ca_bundle, connect_ip, 2_000_000, "application/json")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError("SensoBuild hat kein JSON-Objekt geliefert")
    return _summarize(payload)


def _source_settings(config: dict[str, Any]) -> tuple[str, str, str]:
    section = config.get("sensobuild", {})
    base_url = str(section.get("url") or "https://sensobuild.datahub.local/").rstrip("/") + "/"
    ca_bundle = str(resolve_project_path(config, str(section.get("ca_bundle") or "app/certs/sensobuild-ca.pem")))
    connect_ip = str(section.get("connect_ip") or "").strip()
    return base_url, ca_bundle, connect_ip


async def get_sensobuild_roof(config: dict[str, Any]) -> bytes:
    base_url, ca_bundle, connect_ip = _source_settings(config)
    url = urljoin(base_url, "static/images/dach.png")
    return await asyncio.to_thread(_read_bytes, url, ca_bundle, connect_ip, 2_000_000, "image/png")


async def get_sensobuild(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("sensobuild", {})
    if not section.get("enabled", True):
        return {"status": "disabled", "sensors": []}
    base_url, ca_bundle, connect_ip = _source_settings(config)
    url = urljoin(base_url, "api/dashboard")
    cache_key = f"sensobuild:{url}:{ca_bundle}:{connect_ip}"

    async def load() -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(_read_source, url, ca_bundle, connect_ip)
        except Exception as exc:
            _last_errors[cache_key] = str(exc)
            raise
        _last_errors.pop(cache_key, None)
        return result

    try:
        result, stale = await _cache.get_or_load_with_stale(
            cache_key,
            max(15, int(section.get("cache_seconds", 60))),
            3600,
            60,
            load,
        )
    except Exception as exc:
        return {"status": "error", "message": _last_errors.get(cache_key, str(exc)), "sensors": []}
    return {**result, "status": "stale" if stale else "live"}
