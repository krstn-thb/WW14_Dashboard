from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import create_app
from app.sources import (
    AsyncTTLCache,
    _crypto_display_symbol,
    _expand_recurring_events,
    _parse_mensa_menu,
    _weather_description,
    _yahoo_quote_in_euro,
    get_departures,
    get_stocks,
    get_weather,
)
from app.store import TodoStore


def make_config(tmp_path: Path) -> Path:
    (tmp_path / "deadlines.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "title": "Test deadline",
                        "due": "2099-02-03T12:00:00+01:00",
                        "kind": "Test",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "events.json").write_text('{"items": []}', encoding="utf-8")
    config = tmp_path / "dashboard.yaml"
    config.write_text(
        f"""
dashboard:
  title: Test Dashboard
  timezone: Europe/Berlin
  refresh_seconds: 30
storage:
  database: "{(tmp_path / 'test.db').as_posix()}"
influxdb:
  enabled: false
climate:
  demo_metrics:
    - {{ id: temperature, label: Temperatur, value: 21, unit: "°C" }}
deadlines:
  file: "{(tmp_path / 'deadlines.json').as_posix()}"
  horizon_days: 30000
events:
  file: "{(tmp_path / 'events.json').as_posix()}"
stocks:
  enabled: false
  provider: demo
  symbols: []
""",
        encoding="utf-8",
    )
    return config


def test_health_and_dashboard(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        health = client.get("/api/health")
        dashboard = client.get("/api/dashboard")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert dashboard.status_code == 200
    assert dashboard.json()["settings"]["title"] == "Test Dashboard"
    assert dashboard.json()["climate"]["status"] == "demo"
    assert dashboard.json()["weather"]["status"] == "disabled"
    assert dashboard.json()["mensa"]["status"] == "disabled"
    assert dashboard.json()["deadlines"]["items"][0]["title"] == "Test deadline"


def test_todo_lifecycle(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        created = client.post("/api/todos", json={"text": "Dashboard prüfen"})
        todo_id = created.json()["id"]
        updated = client.patch(f"/api/todos/{todo_id}", json={"done": True})
        listed = client.get("/api/todos")
        deleted = client.delete(f"/api/todos/{todo_id}")

    assert created.status_code == 201
    assert updated.json()["done"] is True
    assert listed.json()[0]["text"] == "Dashboard prüfen"
    assert deleted.status_code == 204


def test_blank_todo_is_rejected(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        response = client.post("/api/todos", json={"text": "   "})

    assert response.status_code == 422


def test_stock_selection_lifecycle(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/stocks",
            json={
                "symbol": "VNA.DE",
                "label": "Vonovia",
                "currency": "EUR",
                "provider": "boerse_frankfurt",
                "provider_id": "DE000A1ML7J1",
            },
        )
        listed = client.get("/api/stocks")
        deleted = client.delete("/api/stocks/VNA.DE")

    assert created.status_code == 201
    assert listed.json() == [
        {
            "symbol": "VNA.DE",
            "label": "Vonovia",
            "currency": "EUR",
            "asset_type": "stock",
            "provider": "boerse_frankfurt",
            "provider_id": "DE000A1ML7J1",
        }
    ]
    assert deleted.status_code == 204


def test_crypto_selection_is_persisted(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/stocks",
            json={
                "symbol": "BTC-EUR",
                "label": "Bitcoin EUR",
                "asset_type": "crypto",
            },
        )
        listed = client.get("/api/stocks")

    assert created.status_code == 201
    assert listed.json()[0]["asset_type"] == "crypto"


def test_live_stock_collection_uses_boerse_frankfurt(monkeypatch) -> None:
    async def fake_fetch(item: dict):
        assert item["symbol"] == "TEST-EUR"
        return {
            **item,
            "price": 20.0,
            "previous_close": 18.0,
            "change_percent": 11.11,
            "currency": "EUR",
            "points": [18.0, 20.0],
            "status": "live",
        }

    monkeypatch.setattr("app.sources._fetch_boerse_stock", fake_fetch)
    result = asyncio.run(
        asyncio.wait_for(
            get_stocks(
                {
                    "stocks": {
                        "enabled": True,
                        "provider": "boerse_frankfurt",
                        "symbols": [
                            {
                                "symbol": "TEST-EUR",
                                "label": "Testwert",
                                "asset_type": "stock",
                                "provider": "boerse_frankfurt",
                                "provider_id": "TEST00000001",
                            }
                        ],
                    }
                }
            ),
            timeout=1,
        )
    )

    assert result["status"] == "live"
    assert result["items"][0]["price"] == 20.0
    assert result["items"][0]["currency"] == "EUR"


def test_failed_market_item_does_not_hide_other_selections(monkeypatch) -> None:
    async def fake_fetch(item: dict):
        if item["symbol"] == "BROKEN-EUR":
            raise RuntimeError("Quelle kennt den Wert nicht")
        return {
            **item,
            "price": 20.0,
            "change_percent": 1.0,
            "currency": "EUR",
            "points": [19.0, 20.0],
            "status": "live",
        }

    async def fake_yahoo(item: dict):
        raise RuntimeError(f"Yahoo kennt {item['symbol']} ebenfalls nicht")

    monkeypatch.setattr("app.sources._fetch_boerse_stock", fake_fetch)
    monkeypatch.setattr("app.sources._fetch_yahoo_market", fake_yahoo)
    result = asyncio.run(
        get_stocks(
            {
                "stocks": {
                    "enabled": True,
                    "provider": "boerse_frankfurt",
                    "symbols": [
                        {
                            "symbol": "GOOD-EUR",
                            "label": "Erreichbar",
                            "asset_type": "stock",
                        },
                        {
                            "symbol": "BROKEN-EUR",
                            "label": "Altbestand",
                            "asset_type": "stock",
                        },
                    ],
                }
            }
        )
    )

    assert result["status"] == "partial"
    assert [item["label"] for item in result["items"]] == ["Erreichbar", "Altbestand"]
    assert result["items"][0]["price"] == 20.0
    assert result["items"][1]["status"] == "error"


def test_all_failed_market_items_remain_visible(monkeypatch) -> None:
    async def fake_fetch(item: dict):
        raise RuntimeError(f"Kein Kurs für {item['symbol']}")

    monkeypatch.setattr("app.sources._fetch_boerse_stock", fake_fetch)
    monkeypatch.setattr("app.sources._fetch_yahoo_market", fake_fetch)
    result = asyncio.run(
        get_stocks(
            {
                "stocks": {
                    "enabled": True,
                    "provider": "boerse_frankfurt",
                    "symbols": [
                        {
                            "symbol": "ONLY-BROKEN",
                            "label": "Gespeicherter Wert",
                            "asset_type": "stock",
                        }
                    ],
                }
            }
        )
    )

    assert result["status"] == "error"
    assert result["items"][0]["label"] == "Gespeicherter Wert"
    assert result["items"][0]["status"] == "error"


def test_saved_market_quote_is_used_after_restart_style_failure(monkeypatch) -> None:
    async def fake_fetch(item: dict):
        raise RuntimeError(f"Abruf für {item['symbol']} fehlgeschlagen")

    monkeypatch.setattr("app.sources._fetch_boerse_stock", fake_fetch)
    monkeypatch.setattr("app.sources._fetch_yahoo_market", fake_fetch)
    result = asyncio.run(
        get_stocks(
            {
                "stocks": {
                    "enabled": True,
                    "provider": "boerse_frankfurt",
                    "symbols": [
                        {
                            "symbol": "SAVED-EUR",
                            "label": "Gespeicherte Aktie",
                            "asset_type": "stock",
                        }
                    ],
                    "cached_quotes": {
                        "SAVED-EUR": {
                            "symbol": "SAVED-EUR",
                            "label": "Gespeicherte Aktie",
                            "price": 123.45,
                            "currency": "EUR",
                            "points": [120.0, 123.45],
                            "status": "live",
                        }
                    },
                }
            }
        )
    )

    assert result["status"] == "stale"
    assert result["items"][0]["price"] == 123.45
    assert result["items"][0]["status"] == "stale"


def test_yahoo_is_used_when_boerse_frankfurt_fails(monkeypatch) -> None:
    async def failed_primary(item: dict):
        raise RuntimeError("Börse Frankfurt antwortet nicht")

    async def yahoo_fallback(item: dict):
        return {
            **item,
            "price": 88.0,
            "change_percent": 1.5,
            "currency": "EUR",
            "points": [86.0, 88.0],
            "source": "yahoo",
            "status": "live",
        }

    monkeypatch.setattr("app.sources._fetch_boerse_stock", failed_primary)
    monkeypatch.setattr("app.sources._fetch_yahoo_market", yahoo_fallback)
    result = asyncio.run(
        get_stocks(
            {
                "stocks": {
                    "enabled": True,
                    "provider": "boerse_frankfurt",
                    "symbols": [
                        {
                            "symbol": "YAHOO-FALLBACK",
                            "label": "Fallback-Aktie",
                            "asset_type": "stock",
                        }
                    ],
                }
            }
        )
    )

    assert result["status"] == "live"
    assert result["message"] == "Yahoo"
    assert result["items"][0]["price"] == 88.0


def test_yahoo_quote_is_converted_to_euro(monkeypatch) -> None:
    async def exchange_rate(symbol: str):
        assert symbol == "ZZZEUR=X"
        return {"price": 0.5, "currency": "EUR", "points": [0.5]}

    monkeypatch.setattr("app.sources._fetch_yahoo_quote", exchange_rate)
    converted = asyncio.run(
        _yahoo_quote_in_euro(
            {
                "price": 100.0,
                "previous_close": 90.0,
                "currency": "ZZZ",
                "points": [90.0, 100.0],
            }
        )
    )

    assert converted["price"] == 50.0
    assert converted["previous_close"] == 45.0
    assert converted["points"] == [45.0, 50.0]
    assert converted["currency"] == "EUR"


def test_market_quote_is_persisted_in_database(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "quotes.db")
    store.initialise()
    store.add_stock("BTC-USD", "Bitcoin", "USD", "crypto")
    store.save_market_quotes(
        [
            {
                "symbol": "BTC-USD",
                "label": "Bitcoin",
                "price": 54321.0,
                "currency": "EUR",
                "points": [54000.0, 54321.0],
                "status": "live",
                "provider": "kraken",
                "provider_id": "XBTEUR",
            }
        ]
    )

    quotes = store.list_market_quotes()
    selected = store.list_stocks()[0]
    assert quotes["BTC-USD"]["price"] == 54321.0
    assert quotes["BTC-USD"]["cached_at"]
    assert selected["currency"] == "EUR"
    assert selected["provider"] == "kraken"
    assert selected["provider_id"] == "XBTEUR"


def test_kraken_symbols_are_presented_with_common_names() -> None:
    assert _crypto_display_symbol("XBT") == "BTC"
    assert _crypto_display_symbol("XXBT") == "BTC"
    assert _crypto_display_symbol("XDG") == "DOGE"
    assert _crypto_display_symbol("XETH") == "ETH"


def test_live_crypto_collection_uses_kraken(monkeypatch) -> None:
    async def fake_fetch(item: dict):
        assert item["provider_id"] == "TESTEUR"
        return {
            **item,
            "price": 42.0,
            "change_percent": 2.5,
            "currency": "EUR",
            "points": [40.0, 42.0],
            "status": "live",
        }

    monkeypatch.setattr("app.sources._fetch_kraken_crypto", fake_fetch)
    result = asyncio.run(
        get_stocks(
            {
                "stocks": {
                    "enabled": True,
                    "provider": "boerse_frankfurt",
                    "cache_seconds": 1,
                    "symbols": [
                        {
                            "symbol": "TEST-EUR",
                            "label": "Testcoin",
                            "asset_type": "crypto",
                            "provider": "kraken",
                            "provider_id": "TESTEUR",
                        }
                    ],
                }
            }
        )
    )

    assert result["status"] == "live"
    assert result["items"][0]["price"] == 42.0
    assert result["items"][0]["provider"] == "kraken"


def test_mensa_menu_parser_extracts_meals() -> None:
    content = """
    <html><body>
      <h3>So schmeckt's den Nutzern der App:</h3><p>3,7 Sterne</p>
      <h3>Angebot 1</h3>
      <p>Spaghetti aglio e olio mit Kirschtomaten</p>
      <p>Knoblauch vegan ZUSATZ geschwefelt NÄHRWERT 3661 kJ 875 kcal</p>
      <p>2,15 €</p>
      <h3>Angebot 2</h3>
      <p>Chili con Quinoa mit Guacamole</p>
      <p>vegan NÄHRWERT 442 kcal</p>
      <p>2,95 €</p>
      <h2>Speiseplan</h2>
    </body></html>
    """

    items = _parse_mensa_menu(content)

    assert len(items) == 2
    assert items[0] == {
        "category": "Angebot 1",
        "name": "Spaghetti aglio e olio mit Kirschtomaten",
        "price": "2,15 €",
        "calories": 875,
        "badges": ["Vegan"],
    }


def test_climate_metric_selection_lifecycle(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        current = client.get("/api/climate/metrics")
        created = client.post(
            "/api/climate/metrics",
            json={
                "measurement": "loxone",
                "field": "temperature",
                "tags": {"room": "Büro"},
            },
        )
        deleted = client.delete(f'/api/climate/metrics/{created.json()["id"]}')

    assert current.json() == []
    assert created.status_code == 201
    assert created.json()["unit"] == "°C"
    assert created.json()["label"] == "Temperatur · Büro"
    assert created.json()["tags"] == {"room": "Büro"}
    assert deleted.status_code == 204


def test_manual_deadline_lifecycle(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/deadlines",
            json={
                "title": "Journal einreichen",
                "due": "2099-03-04T23:59:00",
                "kind": "Journal",
            },
        )
        listed = client.get("/api/deadlines")
        dashboard = client.get("/api/dashboard")
        deleted = client.delete(f'/api/deadlines/{created.json()["id"]}')

    assert created.status_code == 201
    assert created.json()["due"].endswith("+01:00")
    assert any(item["title"] == "Journal einreichen" for item in listed.json())
    assert any(
        item["title"] == "Journal einreichen"
        for item in dashboard.json()["deadlines"]["items"]
    )
    assert deleted.status_code == 204


def test_manual_event_lifecycle(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/events",
            json={
                "title": "Projektbesprechung",
                "start": "2099-03-04T10:00:00",
                "end": "2099-03-04T11:00:00",
                "location": "WW14",
                "recurrence": "weekly",
                "recurrence_until": "2099-06-30",
            },
        )
        listed = client.get("/api/events")
        deleted = client.delete(f'/api/events/{created.json()["id"]}')

    assert created.status_code == 201
    assert created.json()["start"].endswith("+01:00")
    assert created.json()["location"] == "WW14"
    assert created.json()["recurrence"] == "weekly"
    assert created.json()["recurrence_until"] == "2099-06-30"
    assert any(item["title"] == "Projektbesprechung" for item in listed.json())
    assert deleted.status_code == 204


def test_recurring_events_expand_with_preserved_duration() -> None:
    timezone = ZoneInfo("Europe/Berlin")
    start = datetime.now(timezone).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
    end = start + timedelta(hours=1)
    horizon = start + timedelta(days=15)
    items = [
        {
            "id": 1,
            "title": "Wochenrunde",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "recurrence": "weekly",
            "recurrence_until": horizon.date().isoformat(),
        }
    ]

    expanded = _expand_recurring_events(items, timezone, horizon)

    assert len(expanded) == 3
    assert all(item["recurrence"] == "weekly" for item in expanded)
    assert all(
        datetime.fromisoformat(item["end"]) - datetime.fromisoformat(item["start"])
        == timedelta(hours=1)
        for item in expanded
    )


def test_re1_departures_are_split_by_direction(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, **_):
            return FakeResponse(
                {
                    "departures": [
                        {
                            "train": "OE RE1",
                            "destination": "Magdeburg Hbf",
                            "scheduledDeparture": "16:00",
                            "delayDeparture": 5,
                            "platform": "1",
                            "scheduledPlatform": "1",
                        },
                        {
                            "train": "OE RE 1",
                            "destination": "Frankfurt (Oder)",
                            "scheduledDeparture": "16:10",
                            "delayDeparture": 0,
                            "platform": "2",
                            "scheduledPlatform": "2",
                        },
                        {"train": "OE RB51", "destination": "Rathenow", "scheduledDeparture": "16:20"},
                    ]
                }
            )

    monkeypatch.setattr("app.sources.httpx.AsyncClient", lambda **_: FakeClient())
    result = asyncio.run(
        get_departures(
            {
                "departures": {
                    "enabled": True,
                    "base_url": "https://test-vbb.invalid",
                    "station": "Brandenburg Hbf",
                    "cache_seconds": 1,
                }
            }
        )
    )

    assert result["status"] == "live"
    assert result["directions"]["magdeburg"][0]["delay_minutes"] == 5
    assert result["directions"]["berlin"][0]["delay_minutes"] == 0


def test_weather_includes_todays_timeline_and_radar(monkeypatch) -> None:
    times = [f"2026-09-30T{hour:02d}:00" for hour in range(24)]
    forecast = {
        "current": {
            "time": "2026-09-30T10:15",
            "temperature_2m": 17.4,
            "apparent_temperature": 16.1,
            "weather_code": 2,
            "wind_speed_10m": 12.0,
        },
        "hourly": {
            "time": times,
            "temperature_2m": [float(10 + hour / 2) for hour in range(24)],
            "weather_code": [2] * 24,
            "precipitation_probability": list(range(24)),
        },
        "daily": {
            "time": ["2026-09-30"],
            "weather_code": [2],
            "temperature_2m_max": [19.0],
            "temperature_2m_min": [9.0],
            "precipitation_probability_max": [25],
        },
    }
    radar_start = datetime.now(UTC).replace(second=0, microsecond=0)
    radar_start -= timedelta(minutes=radar_start.minute % 5)
    radar_end = radar_start + timedelta(hours=2)
    radar_dimension = (
        f"{radar_start.isoformat().replace('+00:00', 'Z')}/"
        f"{radar_end.isoformat().replace('+00:00', 'Z')}/PT5M"
    )
    radar_capabilities = f"""<?xml version="1.0" encoding="UTF-8"?>
    <WMT_MS_Capabilities>
      <Capability><Layer><Layer>
        <Name>dwd:Radar_rv_product_1x1km_ger</Name>
        <Dimension name="time">{radar_dimension}</Dimension>
      </Layer></Layer></Capability>
    </WMT_MS_Capabilities>""".encode()

    class FakeResponse:
        def __init__(self, payload=None, content=b""):
            self.payload = payload
            self.content = content

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, **_):
            if "maps.dwd.de" in url:
                return FakeResponse(content=radar_capabilities)
            return FakeResponse(payload=forecast)

    monkeypatch.setattr("app.sources.httpx.AsyncClient", lambda **_: FakeClient())
    result = asyncio.run(
        get_weather(
            {
                "weather": {
                    "enabled": True,
                    "location": "Brandenburg an der Havel",
                    "latitude": 52.4125,
                    "longitude": 12.5316,
                    "cache_seconds": 1,
                }
            }
        )
    )

    assert result["status"] == "live"
    assert result["current"]["icon"] == "🌤️"
    assert len(result["hourly"]) == 8
    assert result["hourly"][3]["is_current"] is True
    assert result["radar"]["source"] == "DWD RADVOR"
    assert result["radar"]["stale"] is False
    assert len(result["radar"]["frames"]) >= 8
    assert result["radar"]["frames"][0]["minutes_ahead"] == 0
    assert result["radar"]["frames"][-1]["minutes_ahead"] >= 110


def test_cache_keeps_last_good_value_during_temporary_failure() -> None:
    cache = AsyncTTLCache()
    attempts = 0

    async def exercise_cache() -> None:
        nonlocal attempts

        async def succeeds():
            nonlocal attempts
            attempts += 1
            return {"frames": ["radar"]}

        async def fails():
            nonlocal attempts
            attempts += 1
            raise RuntimeError("DWD timeout")

        first, first_stale = await cache.get_or_load_with_stale(
            "radar", 0, 3600, 120, succeeds
        )
        fallback, fallback_stale = await cache.get_or_load_with_stale(
            "radar", 0, 3600, 120, fails
        )
        during_retry, retry_stale = await cache.get_or_load_with_stale(
            "radar", 0, 3600, 120, fails
        )

        assert first == fallback == during_retry
        assert first_stale is False
        assert fallback_stale is True
        assert retry_stale is True

    asyncio.run(exercise_cache())
    assert attempts == 2


def test_weather_descriptions_include_symbols() -> None:
    assert _weather_description(0) == ("Klar", "☀️")
    assert _weather_description(3) == ("Bedeckt", "☁️")
    assert _weather_description(95) == ("Gewitter", "⛈️")


def test_weather_location_is_persisted_and_used(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    location = {
        "name": "Potsdam",
        "label": "Potsdam, Brandenburg, Deutschland",
        "latitude": 52.39886,
        "longitude": 13.06566,
        "timezone": "Europe/Berlin",
        "admin1": "Brandenburg",
        "country": "Deutschland",
    }
    with TestClient(app) as client:
        default_location = client.get("/api/weather/location")
        updated = client.put("/api/weather/location", json=location)
        selected = client.get("/api/weather/location")
        dashboard = client.get("/api/dashboard")

    assert default_location.json()["name"] == "Brandenburg an der Havel"
    assert updated.status_code == 200
    assert selected.json() == location
    assert dashboard.json()["weather"]["location"] == "Potsdam"


def test_weather_location_search(monkeypatch, tmp_path: Path) -> None:
    async def fake_search(query: str):
        assert query == "Potsdam"
        return [
            {
                "name": "Potsdam",
                "label": "Potsdam, Brandenburg, Deutschland",
                "latitude": 52.39886,
                "longitude": 13.06566,
                "timezone": "Europe/Berlin",
                "admin1": "Brandenburg",
                "country": "Deutschland",
            }
        ]

    monkeypatch.setattr("app.main.search_weather_locations", fake_search)
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        response = client.get("/api/weather/locations/search?q=Potsdam")

    assert response.status_code == 200
    assert response.json()[0]["name"] == "Potsdam"

