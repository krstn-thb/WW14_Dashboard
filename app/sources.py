from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote
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


_cache = AsyncTTLCache()


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


async def get_climate(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("influxdb", {}).get("enabled", False):
        return _demo_climate(config)
    try:
        ttl = int(config.get("climate", {}).get("cache_seconds", 60))
        return await _cache.get_or_load(
            "climate", ttl, lambda: asyncio.to_thread(_query_influx_sync, config)
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


async def get_deadlines(config: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
    section = config.get("deadlines", {})
    items = _read_json(resolve_project_path(config, section.get("file", "data/deadlines.json")))
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
    items = _read_json(resolve_project_path(config, section.get("file", "data/events.json")))
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
    result = [
        item
        for item in _normalise_timed_items(items, "start", timezone)
        if datetime.fromisoformat(item["start"]) >= now - timedelta(hours=12)
        and datetime.fromisoformat(item["start"]) <= horizon
    ]
    return {"items": result, "errors": errors}


async def _fetch_yahoo_stock(symbol: str) -> dict[str, Any]:
    encoded = quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
    params = {"range": "1d", "interval": "5m", "includePrePost": "false"}
    headers = {"User-Agent": "WW14-Dashboard/1.0"}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    closes = [
        value
        for value in result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        if isinstance(value, (int, float))
    ]
    current = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    previous = meta.get("chartPreviousClose") or meta.get("previousClose")
    change_percent = (
        ((current - previous) / previous) * 100
        if isinstance(current, (int, float)) and isinstance(previous, (int, float)) and previous
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
                "currency": item.get("currency", "EUR"),
                "points": values,
                "status": "demo",
            }
        )
    return result


async def get_stocks(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("stocks", {})
    if not section.get("enabled", False) or section.get("provider", "demo") == "demo":
        return {
            "status": "demo",
            "message": "Beispieldaten – Aktienquelle noch nicht aktiviert",
            "items": _demo_stocks(section),
        }

    async def load() -> list[dict[str, Any]]:
        results = []
        for item in section.get("symbols", []):
            quote_data = await _fetch_yahoo_stock(item["symbol"])
            results.append({**item, **quote_data, "status": "live"})
        return results

    try:
        items = await _cache.get_or_load(
            "stocks", int(section.get("cache_seconds", 300)), load
        )
        return {"status": "live", "items": items}
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Aktienkurse nicht erreichbar: {exc}",
            "items": _demo_stocks(section),
        }

