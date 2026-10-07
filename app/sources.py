from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from calendar import monthrange
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urljoin
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

from app.config import resolve_project_path


def _parse_datetime(value: Any, timezone: ZoneInfo) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime_time.min)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(value), datetime_time.min)
            except ValueError:
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _read_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    content = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(content, dict):
        content = content.get("items", [])
    return content if isinstance(content, list) else []


class AsyncTTLCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._retry_after: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def get_or_load(
        self, key: str, ttl_seconds: int, loader: Callable[[], Awaitable[Any]]
    ) -> Any:
        now = time.monotonic()
        cached = self._items.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
        async with self._lock:
            cached = self._items.get(key)
            if cached and now - cached[0] < ttl_seconds:
                return cached[1]
            value = await loader()
            self._items[key] = (time.monotonic(), value)
            return value

    async def get_or_load_with_stale(
        self,
        key: str,
        ttl_seconds: int,
        stale_seconds: int,
        retry_seconds: int,
        loader: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, bool]:
        """Load a value while retaining the last good result during outages."""
        now = time.monotonic()
        cached = self._items.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1], False
        if now < self._retry_after.get(key, 0):
            if cached and now - cached[0] < stale_seconds:
                return cached[1], True
            raise RuntimeError("Datenquelle befindet sich in der Wiederholpause")
        async with self._lock:
            now = time.monotonic()
            cached = self._items.get(key)
            if cached and now - cached[0] < ttl_seconds:
                return cached[1], False
            if now < self._retry_after.get(key, 0):
                if cached and now - cached[0] < stale_seconds:
                    return cached[1], True
                raise RuntimeError("Datenquelle befindet sich in der Wiederholpause")
            try:
                value = await loader()
            except Exception:
                self._retry_after[key] = time.monotonic() + retry_seconds
                if cached and time.monotonic() - cached[0] < stale_seconds:
                    return cached[1], True
                raise
            self._items[key] = (time.monotonic(), value)
            self._retry_after.pop(key, None)
            return value, False

    def discard(self, key: str) -> None:
        self._items.pop(key, None)
        self._retry_after.pop(key, None)


_cache = AsyncTTLCache()
_market_metadata_cache = AsyncTTLCache()
_market_search_cache = AsyncTTLCache()
_market_fx_cache = AsyncTTLCache()


def _demo_series(base: float, amplitude: float, count: int = 28) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    minute_phase = now.timestamp() / 1400
    return [
        {
            "time": (now - timedelta(minutes=(count - index - 1) * 15)).isoformat(),
            "value": round(base + math.sin(minute_phase + index / 4) * amplitude, 2),
        }
        for index in range(count)
    ]


def _demo_climate(config: dict[str, Any]) -> dict[str, Any]:
    configured = config.get("climate", {}).get("demo_metrics") or [
        {"id": "temperature", "label": "Temperatur", "value": 21.6, "unit": "°C"},
        {"id": "humidity", "label": "Luftfeuchte", "value": 48.0, "unit": "%"},
        {"id": "co2", "label": "CO₂", "value": 620.0, "unit": "ppm"},
        {"id": "pressure", "label": "Luftdruck", "value": 1017.0, "unit": "hPa"},
    ]
    metrics = []
    for index, item in enumerate(configured):
        base = float(item.get("value", 0))
        amplitude = max(abs(base) * 0.012, 0.2) * (index + 1) / 2
        points = _demo_series(base, amplitude)
        metrics.append(
            {
                "id": item.get("id", f"metric-{index}"),
                "label": item.get("label", "Messwert"),
                "value": points[-1]["value"],
                "unit": item.get("unit", ""),
                "decimals": int(item.get("decimals", 1)),
                "points": points,
            }
        )
    return {
        "status": "demo",
        "message": "Beispieldaten – InfluxDB noch nicht aktiviert",
        "metrics": metrics,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _flux_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _query_influx_sync(config: dict[str, Any]) -> dict[str, Any]:
    from influxdb_client import InfluxDBClient

    influx = config.get("influxdb", {})
    climate = config.get("climate", {})
    url = influx.get("url", "")
    token = influx.get("token", "")
    org = influx.get("org", "")
    bucket = influx.get("bucket", "")
    if not all((url, token, org, bucket)):
        raise ValueError("InfluxDB URL, Token, Organisation oder Bucket fehlt")

    result_metrics: list[dict[str, Any]] = []
    with InfluxDBClient(url=url, token=token, org=org, timeout=10_000) as client:
        query_api = client.query_api()
        for index, metric in enumerate(climate.get("metrics", [])):
            measurement = _flux_escape(metric["measurement"])
            field = _flux_escape(metric["field"])
            filters = [
                f'r["_measurement"] == "{measurement}"',
                f'r["_field"] == "{field}"',
            ]
            for tag, value in metric.get("tags", {}).items():
                filters.append(
                    f'r["{_flux_escape(tag)}"] == "{_flux_escape(value)}"'
                )
            query = (
                f'from(bucket: "{_flux_escape(bucket)}")\n'
                f'  |> range(start: {climate.get("range", "-24h")})\n'
                f'  |> filter(fn: (r) => {" and ".join(filters)})\n'
                f'  |> aggregateWindow(every: {climate.get("window", "15m")}, '
                "fn: mean, createEmpty: false)"
            )
            tables = query_api.query(query=query, org=org)
            points = [
                {"time": record.get_time().isoformat(), "value": record.get_value()}
                for table in tables
                for record in table.records
                if isinstance(record.get_value(), (int, float))
            ]
            if not points:
                continue
            result_metrics.append(
                {
                    "id": metric.get("id", f"metric-{index}"),
                    "label": metric.get("label", metric["field"]),
                    "value": points[-1]["value"],
                    "unit": metric.get("unit", ""),
                    "decimals": int(metric.get("decimals", 1)),
                    "points": points,
                }
            )
    return {
        "status": "live",
        "metrics": result_metrics,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def discover_influx_fields(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the numeric Influx series that currently contain data."""
    from influxdb_client import InfluxDBClient

    influx = config.get("influxdb", {})
    url = influx.get("url", "")
    token = influx.get("token", "")
    org = influx.get("org", "")
    bucket = influx.get("bucket", "")
    if not all((url, token, org, bucket)):
        raise ValueError("InfluxDB URL, Token, Organisation oder Bucket fehlt")

    lookback = influx.get("discovery_range", "-30d")
    query = (
        f'from(bucket: "{_flux_escape(bucket)}")\n'
        f"  |> range(start: {lookback})\n"
        "  |> last()"
    )
    found: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    with InfluxDBClient(url=url, token=token, org=org, timeout=15_000) as client:
        for table in client.query_api().query(query=query, org=org):
            for record in table.records:
                measurement = str(record.values.get("_measurement", "")).strip()
                field = str(record.values.get("_field", "")).strip()
                value = record.get_value()
                tags = tuple(
                    sorted(
                        (str(key), str(tag_value))
                        for key, tag_value in record.values.items()
                        if key not in {"result", "table"}
                        and not str(key).startswith("_")
                        and tag_value is not None
                        and str(tag_value).strip()
                    )
                )
                if (
                    measurement
                    and field
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    found.add((measurement, field, tags))
    return [
        {"measurement": measurement, "field": field, "tags": dict(tags)}
        for measurement, field, tags in sorted(
            found,
            key=lambda item: (
                item[0].lower(),
                item[1].lower(),
                tuple((key.lower(), value.lower()) for key, value in item[2]),
            ),
        )
    ]


async def get_climate(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("influxdb", {}).get("enabled", False):
        return _demo_climate(config)
    try:
        ttl = int(config.get("climate", {}).get("cache_seconds", 60))
        selection = json.dumps(
            config.get("climate", {}).get("metrics", []), sort_keys=True, default=str
        )
        return await _cache.get_or_load(
            f"climate:{selection}",
            ttl,
            lambda: asyncio.to_thread(_query_influx_sync, config),
        )
    except Exception as exc:  # external source must not take down the display
        fallback = _demo_climate(config)
        fallback.update(status="error", message=f"InfluxDB nicht erreichbar: {exc}")
        return fallback


def _normalise_timed_items(
    items: list[dict[str, Any]], date_key: str, timezone: ZoneInfo
) -> list[dict[str, Any]]:
    now = datetime.now(timezone)
    normalised: list[dict[str, Any]] = []
    for item in items:
        parsed = _parse_datetime(item.get(date_key), timezone)
        if not parsed:
            continue
        entry = {**item, date_key: parsed.isoformat()}
        delta = parsed.date() - now.date()
        entry["days_remaining"] = delta.days
        normalised.append(entry)
    return sorted(normalised, key=lambda entry: entry[date_key])


def _recurring_occurrence(start: datetime, recurrence: str, index: int) -> datetime:
    if recurrence == "daily":
        return start + timedelta(days=index)
    if recurrence == "weekly":
        return start + timedelta(weeks=index)
    if recurrence == "monthly":
        month_index = start.month - 1 + index
        year = start.year + month_index // 12
        month = month_index % 12 + 1
        return start.replace(
            year=year, month=month, day=min(start.day, monthrange(year, month)[1])
        )
    if recurrence == "yearly":
        year = start.year + index
        return start.replace(
            year=year, day=min(start.day, monthrange(year, start.month)[1])
        )
    return start


def _expand_recurring_events(
    items: list[dict[str, Any]], timezone: ZoneInfo, horizon: datetime
) -> list[dict[str, Any]]:
    earliest = datetime.now(timezone) - timedelta(hours=12)
    expanded: list[dict[str, Any]] = []
    supported = {"daily", "weekly", "monthly", "yearly"}
    for item in items:
        recurrence = str(item.get("recurrence") or "none").lower()
        if recurrence not in supported:
            expanded.append(item)
            continue
        start = _parse_datetime(item.get("start"), timezone)
        end = _parse_datetime(item.get("end"), timezone)
        if not start:
            continue
        duration = end - start if end else None
        until_value = str(item.get("recurrence_until") or "").strip()
        try:
            recurrence_until = date.fromisoformat(until_value) if until_value else None
        except ValueError:
            recurrence_until = None
        for index in range(5000):
            occurrence = _recurring_occurrence(start, recurrence, index)
            if occurrence > horizon:
                break
            if recurrence_until and occurrence.date() > recurrence_until:
                break
            if occurrence >= earliest:
                entry = {**item, "start": occurrence.isoformat()}
                if duration is not None:
                    entry["end"] = (occurrence + duration).isoformat()
                entry["recurrence_instance"] = index
                expanded.append(entry)
    return expanded


async def get_deadlines(config: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
    section = config.get("deadlines", {})
    configured_items = section.get("items")
    items = (
        list(configured_items)
        if isinstance(configured_items, list)
        else _read_json(
            resolve_project_path(
                config, section.get("file", "data/deadlines.json")
            )
        )
    )
    errors: list[str] = []

    async def fetch_feed(url: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
            return payload.get("items", payload) if isinstance(payload, dict) else payload

    for url in section.get("json_feeds", []):
        try:
            remote = await _cache.get_or_load(
                f"deadline:{url}",
                int(section.get("cache_seconds", 900)),
                lambda url=url: fetch_feed(url),
            )
            if isinstance(remote, list):
                items.extend(remote)
        except Exception as exc:
            errors.append(f"Deadline-Quelle nicht erreichbar: {exc}")

    horizon = datetime.now(timezone).date() + timedelta(
        days=int(section.get("horizon_days", 365))
    )
    result = [
        item
        for item in _normalise_timed_items(items, "due", timezone)
        if item["days_remaining"] >= -1
        and datetime.fromisoformat(item["due"]).date() <= horizon
    ]
    return {"items": result, "errors": errors}


def _calendar_items(raw: bytes, source_name: str, timezone: ZoneInfo) -> list[dict[str, Any]]:
    calendar = Calendar.from_ical(raw)
    items: list[dict[str, Any]] = []
    for component in calendar.walk("VEVENT"):
        start_property = component.get("dtstart")
        if not start_property:
            continue
        start = _parse_datetime(start_property.dt, timezone)
        if not start:
            continue
        end_property = component.get("dtend")
        end = _parse_datetime(end_property.dt, timezone) if end_property else None
        items.append(
            {
                "title": str(component.get("summary", "Termin")),
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
                "location": str(component.get("location", "")),
                "source": source_name,
            }
        )
    return items


async def get_events(config: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
    section = config.get("events", {})
    configured_items = section.get("items")
    items = (
        list(configured_items)
        if isinstance(configured_items, list)
        else _read_json(
            resolve_project_path(config, section.get("file", "data/events.json"))
        )
    )
    errors: list[str] = []

    async def fetch_calendar(url: str, name: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        return _calendar_items(response.content, name, timezone)

    for source in section.get("ical", []):
        if not source.get("url"):
            continue
        try:
            calendar_items = await _cache.get_or_load(
                f'ical:{source["url"]}',
                int(section.get("cache_seconds", 300)),
                lambda source=source: fetch_calendar(
                    source["url"], source.get("name", "Kalender")
                ),
            )
            items.extend(calendar_items)
        except Exception as exc:
            errors.append(f'{source.get("name", "Kalender")} nicht erreichbar: {exc}')

    now = datetime.now(timezone)
    horizon = now + timedelta(days=int(section.get("horizon_days", 60)))
    items = _expand_recurring_events(items, timezone, horizon)
    result = [
        item
        for item in _normalise_timed_items(items, "start", timezone)
        if datetime.fromisoformat(item["start"]) >= now - timedelta(hours=12)
        and datetime.fromisoformat(item["start"]) <= horizon
    ]
    return {"items": result, "errors": errors}


def _re1_direction(departure: dict[str, Any]) -> str | None:
    direction = str(
        departure.get("direction") or departure.get("destination") or ""
    ).lower()
    eastbound = (
        "berlin",
        "frankfurt (oder)",
        "eisenhüttenstadt",
        "cottbus",
        "fürstenwalde",
        "erkner",
    )
    if "magdeburg" in direction:
        return "magdeburg"
    if any(name in direction for name in eastbound):
        return "berlin"

    stop_names: list[str] = []
    for stopover in departure.get("stopovers") or departure.get("route") or []:
        if not isinstance(stopover, dict):
            continue
        stop = stopover.get("stop") if isinstance(stopover.get("stop"), dict) else stopover
        stop_names.append(str(stop.get("name") or "").lower())
    for index, name in enumerate(stop_names):
        if "brandenburg hbf" in name or "brandenburg, hbf" in name:
            stop_names = stop_names[index + 1 :]
            break
    route = " ".join(stop_names)
    if "magdeburg" in route:
        return "magdeburg"
    if any(name in route for name in eastbound):
        return "berlin"
    return None


def _departure_clock(value: Any, timezone: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hour, minute = (int(part) for part in value.split(":"))
    now = datetime.now(timezone)
    departure = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if departure < now - timedelta(hours=2):
        departure += timedelta(days=1)
    return departure


def _normalise_departure(
    departure: dict[str, Any], timezone: ZoneInfo
) -> dict[str, Any]:
    planned_dt = _departure_clock(departure.get("scheduledDeparture"), timezone)
    delay_value = departure.get("delayDeparture")
    delay_minutes = (
        max(0, int(delay_value)) if isinstance(delay_value, (int, float)) else None
    )
    actual_dt = (
        planned_dt + timedelta(minutes=delay_minutes or 0) if planned_dt else None
    )
    train = str(departure.get("train") or "RE1")
    line_match = re.search(r"\bRE\s*1\b", train, flags=re.IGNORECASE)
    return {
        "line": line_match.group(0).replace(" ", "").upper() if line_match else "RE1",
        "destination": str(departure.get("destination") or ""),
        "planned": planned_dt.isoformat() if planned_dt else None,
        "realtime": actual_dt.isoformat() if actual_dt else None,
        "delay_minutes": delay_minutes,
        "platform": departure.get("platform"),
        "planned_platform": departure.get("scheduledPlatform"),
        "cancelled": bool(departure.get("isCancelled")),
    }


async def get_departures(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("departures", {})
    if not section.get("enabled", False):
        return {
            "status": "disabled",
            "station": str(section.get("station", "Brandenburg Hbf")),
            "directions": {"magdeburg": [], "berlin": []},
            "message": "Abfahrtsmonitor ist deaktiviert",
        }

    base_url = str(section.get("base_url", "https://dbf.finalrewind.org")).rstrip("/")
    station_name = str(section.get("station", "Brandenburg Hbf"))
    max_results = max(1, min(8, int(section.get("results_per_direction", 5))))
    try:
        timezone = ZoneInfo(
            str(config.get("dashboard", {}).get("timezone", "Europe/Berlin"))
        )
    except Exception:
        timezone = ZoneInfo("Europe/Berlin")

    async def load() -> dict[str, Any]:
        headers = {"User-Agent": "WW14-Dashboard/1.0"}
        async with httpx.AsyncClient(timeout=14, follow_redirects=True) as client:
            departure_response = await client.get(
                f"{base_url}/{quote(station_name, safe='')}.json",
                params={
                    "version": 3,
                    "limit": max(50, max_results * 10),
                },
                headers=headers,
            )
            departure_response.raise_for_status()
            payload = departure_response.json()

        if isinstance(payload, dict) and payload.get("errstr"):
            raise RuntimeError(str(payload["errstr"]))
        departures = payload.get("departures", []) if isinstance(payload, dict) else []
        directions: dict[str, list[dict[str, Any]]] = {"magdeburg": [], "berlin": []}
        for departure in departures:
            if not isinstance(departure, dict):
                continue
            if not departure.get("scheduledDeparture"):
                continue
            line_name = str(departure.get("train") or "")
            if not re.search(r"\bRE\s*1\b", line_name, flags=re.IGNORECASE):
                continue
            direction = _re1_direction(departure)
            if direction and len(directions[direction]) < max_results:
                directions[direction].append(_normalise_departure(departure, timezone))

        return {
            "station": station_name,
            "directions": directions,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    try:
        data, stale = await _cache.get_or_load_with_stale(
            f"departures:{base_url}:{station_name}",
            int(section.get("cache_seconds", 60)),
            int(section.get("stale_seconds", 1800)),
            int(section.get("retry_seconds", 90)),
            load,
        )
        return {
            **data,
            "status": "stale" if stale else "live",
            "stale": stale,
            "message": "Letzter erfolgreicher Stand" if stale else "",
        }
    except Exception as exc:
        return {
            "status": "error",
            "station": station_name,
            "directions": {"magdeburg": [], "berlin": []},
            "message": f"Bahn-Echtzeitdaten nicht erreichbar: {exc}",
        }


_BOERSE_API = "https://api.live.deutsche-boerse.com/v1"
_BOERSE_SITE = "https://www.boerse-frankfurt.de/"
_KRAKEN_API = "https://api.kraken.com/0/public"
_CRYPTO_NAMES = {
    "AAVE": "Aave",
    "ADA": "Cardano",
    "AVAX": "Avalanche",
    "BCH": "Bitcoin Cash",
    "BTC": "Bitcoin",
    "DOGE": "Dogecoin",
    "DOT": "Polkadot",
    "ETH": "Ethereum",
    "LINK": "Chainlink",
    "LTC": "Litecoin",
    "SOL": "Solana",
    "XLM": "Stellar",
    "XRP": "XRP",
}


def _market_headers() -> dict[str, str]:
    return {"User-Agent": "WW14-Dashboard/1.0", "Accept": "application/json"}


async def _boerse_salt() -> str:
    async def load() -> str:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            page = await client.get(_BOERSE_SITE, headers=_market_headers())
            page.raise_for_status()
            scripts = re.findall(
                r'<script[^>]+src=["\']([^"\']*main\.[^"\']+)["\']',
                page.text,
                flags=re.IGNORECASE,
            )
            if not scripts:
                raise RuntimeError("Börse-Frankfurt-Konfiguration fehlt")
            script = await client.get(
                urljoin(str(page.url), scripts[-1]), headers=_market_headers()
            )
            script.raise_for_status()
        match = re.search(r'tracing:\{salt:"([a-f0-9]+)"', script.text)
        if not match:
            raise RuntimeError("Börse-Frankfurt-Signatur fehlt")
        return match.group(1)

    return await _market_metadata_cache.get_or_load("boerse:salt", 900, load)


def _boerse_signed_headers(url: str, salt: str) -> dict[str, str]:
    client_date = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    local_now = datetime.now(ZoneInfo("Europe/Berlin"))
    return {
        **_market_headers(),
        "Client-Date": client_date,
        "X-Client-TraceId": hashlib.md5(
            f"{client_date}{url}{salt}".encode(), usedforsecurity=False
        ).hexdigest(),
        "X-Security": hashlib.md5(
            local_now.strftime("%Y%m%d%H%M").encode(), usedforsecurity=False
        ).hexdigest(),
    }


async def _boerse_signed_get(path: str, params: dict[str, Any]) -> Any:
    url = str(httpx.URL(f"{_BOERSE_API}/{path.lstrip('/')}", params=params))
    for attempt in range(2):
        salt = await _boerse_salt()
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(url, headers=_boerse_signed_headers(url, salt))
        if response.status_code not in {401, 403} or attempt:
            response.raise_for_status()
            return response.json()
        _market_metadata_cache.discard("boerse:salt")
    raise RuntimeError("Börse Frankfurt hat den Abruf abgelehnt")


def _translated_market_name(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "")
    translations = value.get("translations") or {}
    return str(
        translations.get("others")
        or translations.get("de")
        or value.get("originalValue")
        or ""
    )


async def _boerse_security_details(isin: str) -> dict[str, Any]:
    payload = await _boerse_signed_get("data/data_sheet_header", {"isin": isin})
    return payload if isinstance(payload, dict) else {}


async def _search_boerse_stocks(query: str) -> list[dict[str, str]]:
    payload = await _boerse_signed_get(
        "global_search/limitedsearch/de",
        {"searchTerms": ",".join(query.split())},
    )
    grouped = payload if isinstance(payload, list) else []
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in grouped:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")).upper() not in {"EQUITY", "ETF"}:
                continue
            isin = str(item.get("isin", "")).strip().upper()
            if not isin or isin in seen:
                continue
            seen.add(isin)
            matches.append(item)
            if len(matches) >= 6:
                break
        if len(matches) >= 6:
            break

    details = await asyncio.gather(
        *(_boerse_security_details(str(item["isin"])) for item in matches),
        return_exceptions=True,
    )
    results: list[dict[str, str]] = []
    for item, detail in zip(matches, details):
        detail = detail if isinstance(detail, dict) else {}
        isin = str(item["isin"]).upper()
        symbol = str(
            detail.get("exchangeSymbol") or detail.get("wkn") or isin
        ).upper()
        label = _translated_market_name(
            detail.get("instrumentName") or item.get("name")
        )
        results.append(
            {
                "symbol": symbol,
                "label": label or symbol,
                "exchange": "Börse Frankfurt / Xetra",
                "asset_type": "stock",
                "currency": "EUR",
                "provider": "boerse_frankfurt",
                "provider_id": isin,
            }
        )
    return results


def _crypto_display_symbol(base: str) -> str:
    raw = base.upper()
    if raw in {"XBT", "XXBT"}:
        return "BTC"
    if raw in {"XDG", "XXDG"}:
        return "DOGE"
    return raw[1:] if len(raw) > 3 and raw[0] in {"X", "Z"} else raw


async def _kraken_eur_pairs() -> list[dict[str, str]]:
    async def load() -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                f"{_KRAKEN_API}/AssetPairs", headers=_market_headers()
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("error"):
            raise RuntimeError(", ".join(payload["error"]))
        result: list[dict[str, str]] = []
        for pair_id, item in (payload.get("result") or {}).items():
            wsname = str(item.get("wsname") or "")
            if not wsname.endswith("/EUR") or item.get("status") != "online":
                continue
            base = _crypto_display_symbol(wsname.split("/", 1)[0])
            result.append(
                {
                    "symbol": f"{base}-EUR",
                    "label": _CRYPTO_NAMES.get(base, base),
                    "exchange": "Kraken",
                    "asset_type": "crypto",
                    "currency": "EUR",
                    "provider": "kraken",
                    "provider_id": str(item.get("altname") or pair_id).upper(),
                }
            )
        return result

    return await _market_metadata_cache.get_or_load("kraken:eur-pairs", 21600, load)


async def _search_kraken_assets(query: str) -> list[dict[str, str]]:
    lowered = query.casefold()
    matches = [
        item
        for item in await _kraken_eur_pairs()
        if lowered in item["symbol"].casefold()
        or lowered in item["label"].casefold()
    ]
    return matches[:6]


async def _search_yahoo_assets(query: str) -> list[dict[str, str]]:
    params = {
        "q": query,
        "quotesCount": "8",
        "newsCount": "0",
        "enableFuzzyQuery": "true",
        "lang": "de-DE",
        "region": "DE",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params=params,
            headers=_market_headers(),
        )
        response.raise_for_status()
        payload = response.json()
    results: list[dict[str, str]] = []
    for item in payload.get("quotes", []):
        quote_type = str(item.get("quoteType", "")).upper()
        if quote_type not in {"EQUITY", "ETF", "CRYPTOCURRENCY"}:
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        results.append(
            {
                "symbol": symbol,
                "label": str(item.get("longname") or item.get("shortname") or symbol),
                "exchange": str(item.get("exchDisp") or item.get("exchange") or "Yahoo"),
                "asset_type": "crypto"
                if quote_type == "CRYPTOCURRENCY"
                else "stock",
                "currency": str(item.get("currency") or "").upper(),
                "provider": "yahoo",
                "provider_id": symbol,
            }
        )
    return results


async def search_stocks(query: str) -> list[dict[str, str]]:
    async def load() -> list[dict[str, str]]:
        sources = await asyncio.gather(
            _search_boerse_stocks(query),
            _search_kraken_assets(query),
            _search_yahoo_assets(query),
            return_exceptions=True,
        )
        candidates = [
            item
            for source in sources
            if isinstance(source, list)
            for item in source
        ]
        if not candidates and all(isinstance(source, Exception) for source in sources):
            raise RuntimeError(str(sources[0]))
        results: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in candidates:
            label_key = re.sub(r"\W+", "", item["label"].casefold())
            key = (item["asset_type"], label_key or item["symbol"].casefold())
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
        lowered = query.casefold()

        def relevance(item: dict[str, str]) -> tuple[int, str]:
            symbol = item["symbol"].casefold()
            label = item["label"].casefold()
            score = (
                0
                if lowered in {symbol, label}
                else 1
                if symbol.startswith(lowered) or label.startswith(lowered)
                else 2
            )
            return score, label

        return sorted(results, key=relevance)[:10]

    return await _market_search_cache.get_or_load(
        f"market-search:{query.casefold()}", 900, load
    )


async def _fetch_yahoo_quote(symbol: str) -> dict[str, Any]:
    encoded = quote(symbol, safe="")
    params = {"range": "1d", "interval": "5m", "includePrePost": "false"}
    errors: list[str] = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                response = await client.get(
                    f"https://{host}/v8/finance/chart/{encoded}",
                    params=params,
                    headers=_market_headers(),
                )
                response.raise_for_status()
                payload = response.json()
            chart = payload.get("chart") or {}
            if chart.get("error"):
                raise RuntimeError(str(chart["error"]))
            result = (chart.get("result") or [None])[0]
            if not isinstance(result, dict):
                raise RuntimeError("Yahoo lieferte keinen Kurs")
            meta = result.get("meta") or {}
            closes = [
                value
                for value in result.get("indicators", {})
                .get("quote", [{}])[0]
                .get("close", [])
                if isinstance(value, (int, float))
            ]
            current = meta.get("regularMarketPrice") or (
                closes[-1] if closes else None
            )
            if not isinstance(current, (int, float)):
                raise RuntimeError("Yahoo lieferte keinen aktuellen Wert")
            previous = meta.get("chartPreviousClose") or meta.get("previousClose")
            change_percent = (
                ((current - previous) / previous) * 100
                if isinstance(previous, (int, float)) and previous
                else None
            )
            return {
                "price": current,
                "previous_close": previous,
                "change_percent": change_percent,
                "currency": meta.get("currency", ""),
                "points": closes[-48:],
                "market_state": meta.get("marketState", ""),
            }
        except Exception as exc:
            errors.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errors))


async def _yahoo_quote_in_euro(quote_data: dict[str, Any]) -> dict[str, Any]:
    raw_currency = str(quote_data.get("currency") or "EUR")
    currency = raw_currency.upper()
    minor_factor = 0.01 if raw_currency in {"GBp", "ZAc", "ILA"} else 1.0
    if minor_factor != 1.0:
        quote_data = {
            **quote_data,
            "price": quote_data.get("price") * minor_factor,
            "previous_close": quote_data.get("previous_close") * minor_factor
            if isinstance(quote_data.get("previous_close"), (int, float))
            else quote_data.get("previous_close"),
            "points": [
                value * minor_factor
                for value in quote_data.get("points", [])
                if isinstance(value, (int, float))
            ],
        }
        currency = {"GBP": "GBP", "ZAC": "ZAR", "ILA": "ILS"}.get(
            currency, currency
        )
    if currency == "EUR":
        return {**quote_data, "currency": "EUR"}

    async def load_rate() -> float:
        rate_quote = await _fetch_yahoo_quote(f"{currency}EUR=X")
        rate = rate_quote.get("price")
        if not isinstance(rate, (int, float)) or rate <= 0:
            raise RuntimeError(f"Kein Yahoo-Wechselkurs für {currency}/EUR")
        return float(rate)

    rate, _ = await _market_fx_cache.get_or_load_with_stale(
        f"yahoo-fx:{currency}:EUR", 3600, 86400, 300, load_rate
    )
    converted = {
        **quote_data,
        "currency": "EUR",
        "original_currency": raw_currency,
    }
    for key in ("price", "previous_close"):
        value = quote_data.get(key)
        if isinstance(value, (int, float)):
            converted[key] = value * rate
    converted["points"] = [
        value * rate
        for value in quote_data.get("points", [])
        if isinstance(value, (int, float))
    ]
    return converted


async def _fetch_yahoo_market(item: dict[str, Any]) -> dict[str, Any]:
    provider = str(item.get("provider") or "").lower()
    symbol = str(
        (item.get("provider_id") if provider == "yahoo" else None)
        or item.get("symbol")
        or ""
    ).upper()
    quote_data = await _yahoo_quote_in_euro(await _fetch_yahoo_quote(symbol))
    return {
        **item,
        **quote_data,
        "provider": provider or "yahoo",
        "provider_id": item.get("provider_id") or symbol,
        "source": "yahoo",
        "status": "live",
    }


async def _resolve_boerse_isin(item: dict[str, Any]) -> str:
    provider_id = str(item.get("provider_id") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", provider_id):
        return provider_id
    results = await _search_boerse_stocks(
        str(item.get("label") or item.get("symbol") or "")
    )
    if not results:
        raise RuntimeError(
            f'Kein Börsenplatz für {item.get("label") or item.get("symbol")}'
        )
    symbol = str(item.get("symbol") or "").split(".", 1)[0].upper()
    exact = next(
        (result for result in results if result["symbol"] == symbol), results[0]
    )
    return exact["provider_id"]


async def _fetch_boerse_stock(item: dict[str, Any]) -> dict[str, Any]:
    isin = await _resolve_boerse_isin(item)
    price_payload: dict[str, Any] = {}
    selected_mic = "XETR"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for mic in ("XETR", "XFRA"):
            response = await client.get(
                f"{_BOERSE_API}/data/price_information/single",
                params={"isin": isin, "mic": mic},
                headers=_market_headers(),
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and isinstance(
                payload.get("lastPrice"), (int, float)
            ):
                price_payload = payload
                selected_mic = mic
                break
    if not price_payload:
        raise RuntimeError(f"Kein Kurs für {item.get('label') or item.get('symbol')}")

    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    points: list[float] = []
    try:
        history = await _boerse_signed_get(
            "data/price_history",
            {
                "isin": isin,
                "mic": selected_mic,
                "minDate": (today - timedelta(days=45)).isoformat(),
                "maxDate": today.isoformat(),
                "cleanSplit": "false",
                "cleanPayout": "false",
                "cleanSubscriptionRights": "false",
            },
        )
        points = [
            float(entry["close"])
            for entry in reversed(history.get("data", []))
            if isinstance(entry, dict) and isinstance(entry.get("close"), (int, float))
        ][-32:]
    except Exception:
        points = []
    previous = price_payload.get("closingPricePrevTradingDay")
    current = price_payload.get("lastPrice")
    if len(points) < 2:
        points = [
            float(value)
            for value in (previous, current)
            if isinstance(value, (int, float))
        ]
    return {
        **item,
        "provider": "boerse_frankfurt",
        "provider_id": isin,
        "source": "boerse_frankfurt",
        "price": current,
        "previous_close": previous,
        "change_percent": price_payload.get("changeToPrevDayInPercent"),
        "currency": "EUR",
        "points": points,
        "market_state": price_payload.get("timestampLastPrice", ""),
        "status": "live",
    }


async def _resolve_kraken_pair(item: dict[str, Any]) -> str:
    provider_id = str(item.get("provider_id") or "").strip().upper()
    if item.get("provider") == "kraken" and provider_id:
        return provider_id
    symbol = str(item.get("symbol") or "").upper()
    base = symbol.replace("-EUR", "").replace("/EUR", "").split("-", 1)[0]
    pair = next(
        (
            value
            for value in await _kraken_eur_pairs()
            if value["symbol"] == f"{base}-EUR"
        ),
        None,
    )
    if not pair:
        raise RuntimeError(f"Kein EUR-Kryptopaar für {symbol}")
    return pair["provider_id"]


async def _fetch_kraken_crypto(item: dict[str, Any]) -> dict[str, Any]:
    pair = await _resolve_kraken_pair(item)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        ticker_response, ohlc_response = await asyncio.gather(
            client.get(
                f"{_KRAKEN_API}/Ticker",
                params={"pair": pair},
                headers=_market_headers(),
            ),
            client.get(
                f"{_KRAKEN_API}/OHLC",
                params={"pair": pair, "interval": 60},
                headers=_market_headers(),
            ),
        )
    ticker_response.raise_for_status()
    ohlc_response.raise_for_status()
    ticker_payload = ticker_response.json()
    ohlc_payload = ohlc_response.json()
    errors = list(ticker_payload.get("error") or []) + list(
        ohlc_payload.get("error") or []
    )
    if errors:
        raise RuntimeError(", ".join(errors))
    ticker = next(iter((ticker_payload.get("result") or {}).values()), {})
    ohlc_result = ohlc_payload.get("result") or {}
    candles = next(
        (value for key, value in ohlc_result.items() if key != "last"), []
    )
    points = [
        float(candle[4])
        for candle in candles[-24:]
        if isinstance(candle, list) and len(candle) > 4
    ]
    current = float(ticker["c"][0])
    previous = points[0] if points else float(ticker.get("o") or current)
    change = ((current - previous) / previous * 100) if previous else None
    return {
        **item,
        "provider": "kraken",
        "provider_id": pair,
        "source": "kraken",
        "price": current,
        "previous_close": previous,
        "change_percent": change,
        "currency": "EUR",
        "points": points or [previous, current],
        "market_state": "open",
        "status": "live",
    }


async def _fetch_market_item(item: dict[str, Any]) -> dict[str, Any]:
    is_crypto = item.get("asset_type") == "crypto" or item.get("provider") == "kraken"
    if is_crypto:
        loaders = (_fetch_kraken_crypto, _fetch_yahoo_market)
    elif item.get("provider") == "yahoo":
        loaders = (_fetch_yahoo_market, _fetch_boerse_stock)
    else:
        loaders = (_fetch_boerse_stock, _fetch_yahoo_market)
    errors: list[str] = []
    for loader in loaders:
        try:
            return await loader(item)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def _demo_stocks(section: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = [
        {"symbol": "SAP.DE", "label": "SAP", "price": 232.4, "currency": "EUR"},
        {"symbol": "SIE.DE", "label": "Siemens", "price": 226.1, "currency": "EUR"},
        {"symbol": "AAPL", "label": "Apple", "price": 246.5, "currency": "USD"},
    ]
    items = section.get("symbols") or defaults
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        price = float(item.get("price", 100 + index * 30))
        values = [point["value"] for point in _demo_series(price, price * 0.008, 34)]
        change = (values[-1] - values[0]) / values[0] * 100
        result.append(
            {
                **item,
                "price": values[-1],
                "change_percent": change,
                "currency": "EUR",
                "points": values,
                "status": "demo",
                "asset_type": item.get("asset_type", "stock"),
            }
        )
    return result


async def get_stocks(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("stocks", {})
    cached_quotes = section.get("cached_quotes", {})

    def fallback_item(item: dict[str, Any], error: Exception | str) -> dict[str, Any]:
        cached = cached_quotes.get(str(item.get("symbol") or "").upper())
        if isinstance(cached, dict) and isinstance(cached.get("price"), (int, float)):
            return {
                **item,
                **cached,
                "label": item.get("label") or cached.get("label"),
                "symbol": item.get("symbol") or cached.get("symbol"),
                "currency": "EUR",
                "status": "stale",
                "error": str(error),
            }
        return {
            **item,
            "currency": "EUR",
            "points": [],
            "status": "error",
            "error": str(error),
        }

    if not section.get("enabled", False) or section.get("provider", "demo") == "demo":
        return {
            "status": "demo",
            "message": "Beispieldaten – Marktquelle noch nicht aktiviert",
            "items": _demo_stocks(section),
        }

    async def load() -> dict[str, Any]:
        selected = section.get("symbols", [])
        tasks = [_fetch_market_item(item) for item in selected]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors: list[str] = []
        items: list[dict[str, Any]] = []
        for configured, result in zip(selected, results):
            if isinstance(result, Exception):
                label = str(
                    configured.get("label") or configured.get("symbol") or "Wert"
                )
                errors.append(f"{label}: {result}")
                items.append(fallback_item(configured, result))
            elif isinstance(result, dict):
                items.append(result)
        if errors and len(errors) == len(results):
            raise RuntimeError("; ".join(errors))
        return {"items": items, "errors": errors}

    try:
        symbols = ",".join(
            f'{item.get("provider", "")}:{item.get("provider_id") or item.get("symbol", "")}'
            for item in section.get("symbols", [])
        )
        market_data, stale = await _cache.get_or_load_with_stale(
            f"stocks:{symbols}",
            int(section.get("cache_seconds", 300)),
            int(section.get("stale_seconds", 21600)),
            int(section.get("retry_seconds", 120)),
            load,
        )
        items = market_data.get("items", [])
        errors = market_data.get("errors", [])
        unavailable = [
            str(item.get("label") or item.get("symbol") or "Wert")
            for item in items
            if item.get("status") == "error"
        ]
        saved = [
            str(item.get("label") or item.get("symbol") or "Wert")
            for item in items
            if item.get("status") == "stale"
        ]
        if stale:
            status = "stale"
            message = "Letzter erfolgreicher Stand"
        elif errors:
            status = "partial"
            details = []
            if saved:
                details.append(f"letzter Kurs: {', '.join(saved)}")
            if unavailable:
                details.append(f"ohne Kurs: {', '.join(unavailable)}")
            message = " · ".join(details)
        else:
            status = "live"
            source_names = {
                "boerse_frankfurt": "Börse Frankfurt",
                "kraken": "Kraken",
                "yahoo": "Yahoo",
            }
            active_sources = list(
                dict.fromkeys(
                    source_names.get(str(item.get("source") or ""), "")
                    for item in items
                    if item.get("source")
                )
            )
            message = " · ".join(filter(None, active_sources)) or "Marktdaten live"
        return {
            "status": status,
            "message": message,
            "items": items,
        }
    except Exception as exc:
        items = [fallback_item(item, exc) for item in section.get("symbols", [])]
        has_saved_value = any(item.get("status") == "stale" for item in items)
        return {
            "status": "stale" if has_saved_value else "error",
            "message": "Letzter dauerhaft gespeicherter Kurs"
            if has_saved_value
            else f"Marktdaten konnten nicht abgerufen werden: {exc}",
            "items": items,
        }


class _MensaHTMLParser(HTMLParser):
    """Extract the menu sections from the public iMensa day page."""

    _menu_headings = (
        "angebot",
        "tagesangebot",
        "salattheke",
        "salatbar",
        "dessert",
        "suppe",
        "menü",
        "buffet",
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[tuple[str, list[str]]] = []
        self._heading_parts: list[str] = []
        self._section_parts: list[str] = []
        self._heading = ""
        self._in_heading = False
        self._ignored_depth = 0

    def _finish_section(self) -> None:
        if self._heading and self._section_parts:
            self.sections.append((self._heading, self._section_parts))
        self._heading = ""
        self._section_parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"h2", "h3"}:
            self._finish_section()
        if tag == "h3":
            self._in_heading = True
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "h3" and self._in_heading:
            heading = " ".join(self._heading_parts).strip()
            if heading.lower().startswith(self._menu_headings):
                self._heading = heading
            self._in_heading = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_heading:
            self._heading_parts.append(text)
        elif self._heading:
            self._section_parts.append(text)

    def close(self) -> None:
        super().close()
        self._finish_section()


def _parse_mensa_menu(content: str) -> list[dict[str, Any]]:
    parser = _MensaHTMLParser()
    parser.feed(content)
    parser.close()
    items: list[dict[str, Any]] = []
    metadata_markers = (
        "zusatz",
        "allergen",
        "nährwert",
        "knoblauch",
        "vegan",
        "vegetarisch",
        "geflügel",
        "schwein",
        "rind",
        "fisch",
    )
    badge_labels = (
        ("vegan", "Vegan"),
        ("vegetarisch", "Vegetarisch"),
        ("geflügel", "Geflügel"),
        ("schwein", "Schwein"),
        ("rind", "Rind"),
        ("fisch", "Fisch"),
    )

    for category, parts in parser.sections:
        full_text = " ".join(parts)
        lowered = full_text.lower()
        prices = list(dict.fromkeys(re.findall(r"\d+[,.]\d{2}\s*€", full_text)))
        calories = re.search(r"(\d+)\s*kcal", full_text, flags=re.IGNORECASE)
        badges = [label for marker, label in badge_labels if marker in lowered]

        if category.lower().startswith(("salattheke", "salatbar")):
            descriptions = [
                part
                for part in parts
                if "€" not in part
                and not any(marker in part.lower() for marker in metadata_markers)
                and "installieren" not in part.lower()
            ][:3]
            name = " · ".join(descriptions) or "Salat nach Wahl"
        else:
            name = next(
                (
                    part
                    for part in parts
                    if "€" not in part
                    and not any(marker in part.lower() for marker in metadata_markers)
                    and "installieren" not in part.lower()
                ),
                "",
            )
        if not name:
            continue
        items.append(
            {
                "category": category,
                "name": name,
                "price": " / ".join(price.replace(".", ",") for price in prices),
                "calories": int(calories.group(1)) if calories else None,
                "badges": badges,
            }
        )
    return items


async def get_mensa(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("mensa", {})
    if not section.get("enabled", False):
        return {"status": "disabled", "items": [], "message": "Mensa-Anzeige deaktiviert"}

    timezone_name = str(config.get("dashboard", {}).get("timezone", "Europe/Berlin"))
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Europe/Berlin")
    today = datetime.now(timezone).date()
    target_date = today
    if today.weekday() >= 5:
        target_date += timedelta(days=7 - today.weekday())
    weekdays = ("montag", "dienstag", "mittwoch", "donnerstag", "freitag")
    weekday = weekdays[target_date.weekday()]
    base_url = str(section.get("base_url") or "https://www.imensa.de/brandenburg-an-der-havel/mensa-brandenburg-an-der-havel").rstrip("/")
    url = f"{base_url}/{weekday}.html"

    async def load() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "WW14-Dashboard/1.0"})
            response.raise_for_status()
        items = _parse_mensa_menu(response.text)
        if not items:
            raise ValueError("Auf der iMensa-Seite wurden keine Gerichte gefunden")
        return {
            "items": items,
            "date": target_date.isoformat(),
            "weekday": weekday.capitalize(),
            "source_url": url,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    try:
        menu, stale = await _cache.get_or_load_with_stale(
            f"mensa:{target_date.isoformat()}",
            max(300, int(section.get("cache_seconds", 1800))),
            max(3600, int(section.get("stale_seconds", 43200))),
            max(60, int(section.get("retry_seconds", 300))),
            load,
        )
        return {
            **menu,
            "status": "live",
            "stale": stale,
            "message": "Zuletzt geladener Speiseplan" if stale else "Preise für Studierende · Angaben ohne Gewähr",
        }
    except Exception as exc:
        return {
            "status": "error",
            "items": [],
            "date": target_date.isoformat(),
            "weekday": weekday.capitalize(),
            "source_url": url,
            "message": f"Speiseplan momentan nicht erreichbar: {exc}",
        }


def _weather_description(code: int) -> tuple[str, str]:
    if code == 0:
        return "Klar", "☀️"
    if code in {1, 2}:
        return "Leicht bewölkt", "🌤️"
    if code == 3:
        return "Bedeckt", "☁️"
    if code in {45, 48}:
        return "Nebel", "🌫️"
    if code in {51, 53, 55, 56, 57}:
        return "Nieselregen", "🌦️"
    if code in {61, 63, 65, 66, 67}:
        return "Regen", "🌧️"
    if code in {71, 73, 75, 77}:
        return "Schnee", "🌨️"
    if code in {80, 81, 82}:
        return "Regenschauer", "🌦️"
    if code in {85, 86}:
        return "Schneeschauer", "🌨️"
    if code in {95, 96, 99}:
        return "Gewitter", "⛈️"
    return "Wechselhaft", "🌥️"


def _parse_radar_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _expand_radar_times(value: str) -> list[datetime]:
    times: list[datetime] = []
    for item in value.split(","):
        parts = [part.strip() for part in item.strip().split("/")]
        if len(parts) == 1:
            parsed = _parse_radar_time(parts[0])
            if parsed:
                times.append(parsed)
            continue
        if len(parts) != 3:
            continue
        start = _parse_radar_time(parts[0])
        end = _parse_radar_time(parts[1])
        duration = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", parts[2])
        if not start or not end or not duration:
            continue
        step = timedelta(
            hours=int(duration.group(1) or 0),
            minutes=int(duration.group(2) or 0),
            seconds=int(duration.group(3) or 0),
        )
        if step <= timedelta(0):
            continue
        cursor = start
        while cursor <= end and len(times) < 5000:
            times.append(cursor)
            cursor += step
    return sorted(set(times))


def _radar_frames_from_capabilities(
    content: bytes, now: datetime | None = None
) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(content)
    layer_name = "Radar_rv_product_1x1km_ger"
    dimension_value = ""

    for layer in root.iter():
        if layer.tag.rsplit("}", 1)[-1] != "Layer":
            continue
        name = next(
            (
                (child.text or "").strip()
                for child in layer
                if child.tag.rsplit("}", 1)[-1] == "Name"
            ),
            "",
        )
        if name.rsplit(":", 1)[-1] != layer_name:
            continue
        dimension_value = next(
            (
                (child.text or "").strip()
                for child in layer
                if child.tag.rsplit("}", 1)[-1] in {"Dimension", "Extent"}
                and child.attrib.get("name", "").lower() == "time"
                and (child.text or "").strip()
            ),
            "",
        )
        break

    reference = (now or datetime.now(UTC)).astimezone(UTC)
    available = _expand_radar_times(dimension_value)
    if not available:
        return []

    current = max(
        (item for item in available if item <= reference),
        default=None,
    )
    if not current:
        return []
    candidates = [
        item
        for item in available
        if current <= item <= current + timedelta(hours=2, minutes=5)
    ]
    if not candidates:
        return []

    # The source offers five-minute steps. Fifteen-minute animation frames keep
    # the wall display fluid without repeatedly loading dozens of large maps.
    selected = [candidates[0]]
    for item in candidates[1:]:
        if item - selected[-1] >= timedelta(minutes=15):
            selected.append(item)
    if candidates[-1] - selected[-1] >= timedelta(minutes=8):
        selected.append(candidates[-1])

    return [
        {
            "time": item.isoformat().replace("+00:00", "Z"),
            "minutes_ahead": max(
                0, int(round((item - reference).total_seconds() / 300) * 5)
            ),
        }
        for item in selected
    ]


async def _get_radar(section: dict[str, Any]) -> dict[str, Any] | None:
    latitude = float(section.get("latitude", 52.4125))
    longitude = float(section.get("longitude", 12.5316))
    cache_key = f"weather:radar:{latitude}:{longitude}"

    async def load() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                "https://maps.dwd.de/geoserver/dwd/wms",
                params={
                    "service": "WMS",
                    "version": "1.1.1",
                    "request": "GetCapabilities",
                },
            )
            response.raise_for_status()
            frames = _radar_frames_from_capabilities(response.content)
            if not frames:
                raise ValueError("DWD liefert momentan keine Radar-Zeitpunkte")
            return {
                "endpoint": "https://maps.dwd.de/geoserver/dwd/wms",
                "layer": "dwd:Radar_rv_product_1x1km_ger",
                "frames": frames,
                "latitude": latitude,
                "longitude": longitude,
                "zoom": 7,
                "source": "DWD RADVOR",
                "updated_at": datetime.now(UTC).isoformat(),
            }

    try:
        radar, stale = await _cache.get_or_load_with_stale(
            cache_key,
            max(30, int(section.get("radar_cache_seconds", 120))),
            max(300, int(section.get("radar_stale_seconds", 21600))),
            max(30, int(section.get("radar_retry_seconds", 120))),
            load,
        )
        return {**radar, "stale": stale}
    except Exception:
        return None


async def search_weather_locations(query: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        response = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": query,
                "count": 8,
                "language": "de",
                "format": "json",
            },
        )
        response.raise_for_status()
        payload = response.json()

    locations: list[dict[str, Any]] = []
    for item in payload.get("results", []):
        try:
            latitude = float(item["latitude"])
            longitude = float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        label_parts = []
        for value in (name, item.get("admin1"), item.get("country")):
            part = str(value or "").strip()
            if part and part not in label_parts:
                label_parts.append(part)
        locations.append(
            {
                "name": name,
                "label": ", ".join(label_parts),
                "latitude": latitude,
                "longitude": longitude,
                "timezone": str(item.get("timezone") or "auto"),
                "admin1": str(item.get("admin1") or ""),
                "country": str(item.get("country") or ""),
            }
        )
    return locations


async def get_weather(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("weather", {})
    if not section.get("enabled", False):
        return {
            "status": "disabled",
            "location": section.get("location", ""),
            "timezone": section.get("timezone", "auto"),
            "hourly": [],
            "daily": [],
        }

    async def load() -> dict[str, Any]:
        params = {
            "latitude": section.get("latitude", 52.4125),
            "longitude": section.get("longitude", 12.5316),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,weather_code,precipitation_probability",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": section.get("timezone", "Europe/Berlin"),
            "forecast_days": max(1, min(int(section.get("forecast_days", 5)), 7)),
        }
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                "https://api.open-meteo.com/v1/forecast", params=params
            )
            response.raise_for_status()
            payload = response.json()
        current = payload.get("current", {})
        current_code = int(current.get("weather_code", -1))
        current_label, current_icon = _weather_description(current_code)

        hourly = payload.get("hourly", {})
        hourly_times = hourly.get("time", [])
        current_time = datetime.fromisoformat(
            current.get("time") or datetime.now().isoformat(timespec="minutes")
        )
        hourly_items: list[dict[str, Any]] = []
        for index, timestamp in enumerate(hourly_times):
            forecast_time = datetime.fromisoformat(timestamp)
            if forecast_time.date() != current_time.date() or forecast_time.hour % 3:
                continue
            codes = hourly.get("weather_code", [])
            temperatures = hourly.get("temperature_2m", [])
            precipitation = hourly.get("precipitation_probability", [])
            code = int(codes[index]) if index < len(codes) else -1
            label, icon = _weather_description(code)
            hourly_items.append(
                {
                    "time": timestamp,
                    "weather_code": code,
                    "label": label,
                    "icon": icon,
                    "temperature": temperatures[index]
                    if index < len(temperatures)
                    else None,
                    "precipitation_probability": precipitation[index]
                    if index < len(precipitation)
                    else None,
                    "is_current": forecast_time.hour
                    <= current_time.hour
                    < forecast_time.hour + 3,
                }
            )

        daily = payload.get("daily", {})
        items: list[dict[str, Any]] = []
        dates = daily.get("time", [])
        for index, day in enumerate(dates):
            code = int(daily.get("weather_code", [-1] * len(dates))[index])
            label, icon = _weather_description(code)
            items.append(
                {
                    "date": day,
                    "weather_code": code,
                    "label": label,
                    "icon": icon,
                    "temperature_max": daily.get("temperature_2m_max", [None] * len(dates))[index],
                    "temperature_min": daily.get("temperature_2m_min", [None] * len(dates))[index],
                    "precipitation_probability": daily.get(
                        "precipitation_probability_max", [None] * len(dates)
                    )[index],
                }
            )
        return {
            "status": "live",
            "location": section.get("location", "Brandenburg an der Havel"),
            "timezone": section.get("timezone", "auto"),
            "current": {
                "temperature": current.get("temperature_2m"),
                "apparent_temperature": current.get("apparent_temperature"),
                "wind_speed": current.get("wind_speed_10m"),
                "weather_code": current_code,
                "label": current_label,
                "icon": current_icon,
            },
            "hourly": hourly_items,
            "daily": items,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    try:
        forecast = await _cache.get_or_load(
            "weather:forecast:"
            f'{section.get("latitude", 52.4125)}:'
            f'{section.get("longitude", 12.5316)}:'
            f'{section.get("timezone", "Europe/Berlin")}',
            int(section.get("cache_seconds", 900)),
            load,
        )
    except Exception as exc:
        return {
            "status": "error",
            "location": section.get("location", "Brandenburg an der Havel"),
            "timezone": section.get("timezone", "auto"),
            "message": f"Wettervorhersage nicht erreichbar: {exc}",
            "hourly": [],
            "daily": [],
            "radar": None,
        }
    return {**forecast, "radar": await _get_radar(section)}

