from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.sources import get_weather


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
            "/api/stocks", json={"symbol": "VNA.DE", "label": "Vonovia"}
        )
        listed = client.get("/api/stocks")
        deleted = client.delete("/api/stocks/VNA.DE")

    assert created.status_code == 201
    assert listed.json() == [
        {"symbol": "VNA.DE", "label": "Vonovia", "currency": ""}
    ]
    assert deleted.status_code == 204


def test_climate_metric_selection_lifecycle(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app) as client:
        current = client.get("/api/climate/metrics")
        created = client.post(
            "/api/climate/metrics",
            json={"measurement": "loxone", "field": "temperature"},
        )
        deleted = client.delete(f'/api/climate/metrics/{created.json()["id"]}')

    assert current.json() == []
    assert created.status_code == 201
    assert created.json()["unit"] == "°C"
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
            },
        )
        listed = client.get("/api/events")
        deleted = client.delete(f'/api/events/{created.json()["id"]}')

    assert created.status_code == 201
    assert created.json()["start"].endswith("+01:00")
    assert created.json()["location"] == "WW14"
    assert any(item["title"] == "Projektbesprechung" for item in listed.json())
    assert deleted.status_code == 204


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
    assert len(result["hourly"]) == 8
    assert result["hourly"][3]["is_current"] is True
    assert result["radar"]["source"] == "DWD RADVOR"
    assert len(result["radar"]["frames"]) >= 8
    assert result["radar"]["frames"][0]["minutes_ahead"] == 0
    assert result["radar"]["frames"][-1]["minutes_ahead"] >= 110

