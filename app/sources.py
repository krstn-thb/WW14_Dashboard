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


def discover_influx_fields(config: dict[str, Any]) -> list[dict[str, str]]:
    """Return measurement/field pairs that currently contain data."""
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
        '  |> group(columns: ["_measurement", "_field"])\n'
        "  |> last()\n"
        '  |> keep(columns: ["_measurement", "_field", "_value"])'
    )
    found: set[tuple[str, str]] = set()
    with InfluxDBClient(url=url, token=token, org=org, timeout=15_000) as client:
        for table in client.query_api().query(query=query, org=org):
            for record in table.records:
                measurement = str(record.values.get("_measurement", "")).strip()
                field = str(record.values.get("_field", "")).strip()
                value = record.get_value()
                if (
                    measurement
                    and field
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    found.add((measurement, field))
    return [
        {"measurement": measurement, "field": field}
        for measurement, field in sorted(found, key=lambda item: (item[0].lower(), item[1].lower()))
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


async def search_stocks(query: str) -> list[dict[str, str]]:
    params = {
        "q": query,
        "quotesCount": "10",
        "newsCount": "0",
        "enableFuzzyQuery": "true",
        "lang": "de-DE",
        "region": "DE",
    }
    headers = {"User-Agent": "WW14-Dashboard/1.0"}

    async def load() -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        results: list[dict[str, str]] = []
        for item in payload.get("quotes", []):
            if item.get("quoteType") not in {"EQUITY", "ETF"}:
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            results.append(
                {
                    "symbol": symbol,
                    "label": str(
                        item.get("longname") or item.get("shortname") or symbol
                    ),
                    "exchange": str(
                        item.get("exchDisp") or item.get("exchange") or ""
                    ),
                }
            )
        return results

    return await _cache.get_or_load(f"stock-search:{query.lower()}", 900, load)


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
        symbols = ",".join(item.get("symbol", "") for item in section.get("symbols", []))
        items = await _cache.get_or_load(
            f"stocks:{symbols}", int(section.get("cache_seconds", 300)), load
        )
        return {"status": "live", "items": items}
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Aktienkurse nicht erreichbar: {exc}",
            "items": _demo_stocks(section),
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


async def get_weather(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("weather", {})
    if not section.get("enabled", False):
        return {"status": "disabled", "location": section.get("location", ""), "daily": []}

    async def load() -> dict[str, Any]:
        params = {
            "latitude": section.get("latitude", 52.4125),
            "longitude": section.get("longitude", 12.5316),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
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
            "current": {
                "temperature": current.get("temperature_2m"),
                "apparent_temperature": current.get("apparent_temperature"),
                "wind_speed": current.get("wind_speed_10m"),
                "weather_code": current_code,
                "label": current_label,
                "icon": current_icon,
            },
            "daily": items,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    try:
        return await _cache.get_or_load(
            "weather",
            int(section.get("cache_seconds", 900)),
            load,
        )
    except Exception as exc:
        return {
            "status": "error",
            "location": section.get("location", "Brandenburg an der Havel"),
            "message": f"Wettervorhersage nicht erreichbar: {exc}",
            "daily": [],
        }

