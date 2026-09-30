from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


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

