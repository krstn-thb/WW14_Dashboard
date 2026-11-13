from __future__ import annotations

import asyncio
import json
import math
import ssl
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.config import resolve_project_path
from app.sources import AsyncTTLCache


_cache = AsyncTTLCache()
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


def _read_source(url: str, ca_bundle: str) -> dict[str, Any]:
    context = ssl.create_default_context(cafile=ca_bundle or None)
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=20, context=context) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("SensoBuild-Antwort ist zu groß")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError("SensoBuild hat kein JSON-Objekt geliefert")
    return _summarize(payload)


async def get_sensobuild(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("sensobuild", {})
    if not section.get("enabled", True):
        return {"status": "disabled", "sensors": []}
    base_url = str(section.get("url") or "https://sensobuild.datahub.local/").rstrip("/") + "/"
    url = urljoin(base_url, "api/dashboard")
    ca_bundle = str(section.get("ca_bundle") or "app/certs/sensobuild-ca.pem")
    if ca_bundle:
        ca_bundle = str(resolve_project_path(config, ca_bundle))
    try:
        result, stale = await _cache.get_or_load_with_stale(
            f"sensobuild:{url}:{ca_bundle}",
            max(15, int(section.get("cache_seconds", 60))),
            3600,
            60,
            lambda: asyncio.to_thread(_read_source, url, ca_bundle),
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc), "sensors": []}
    return {**result, "status": "stale" if stale else "live"}
