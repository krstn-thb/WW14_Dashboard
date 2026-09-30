from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import ROOT_DIR, load_config, resolve_project_path
from app.sources import (
    discover_influx_fields,
    get_climate,
    get_deadlines,
    get_events,
    get_stocks,
    get_weather,
    search_stocks,
)
from app.store import TodoStore


class TodoCreate(BaseModel):
    text: str = Field(min_length=1, max_length=240)


class TodoUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=240)
    done: bool | None = None


class StockCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    label: str = Field(default="", max_length=120)
    currency: str = Field(default="", max_length=8)


class ClimateMetricCreate(BaseModel):
    measurement: str = Field(min_length=1, max_length=160)
    field: str = Field(min_length=1, max_length=160)
    label: str = Field(default="", max_length=120)
    unit: str = Field(default="", max_length=24)
    decimals: int = Field(default=1, ge=0, le=4)
    tags: dict[str, str] = Field(default_factory=dict)


class DeadlineCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    due: datetime
    kind: str = Field(default="Deadline", max_length=80)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    start: datetime
    end: datetime | None = None
    location: str = Field(default="", max_length=160)


def _metric_defaults(field: str) -> tuple[str, str, int]:
    lowered = field.lower()
    if any(word in lowered for word in ("temperatur", "temperature", "temp")):
        return "Temperatur", "°C", 1
    if any(word in lowered for word in ("humidity", "feuchte", "luftfeuchtigkeit")):
        return "Luftfeuchte", "%", 0
    if "co2" in lowered or "co₂" in lowered:
        return "CO₂", "ppm", 0
    if any(word in lowered for word in ("pressure", "luftdruck")):
        return "Luftdruck", "hPa", 0
    if any(word in lowered for word in ("power", "leistung")):
        return "Leistung", "W", 0
    if any(word in lowered for word in ("energy", "energie")):
        return "Energie", "kWh", 2
    label = field.replace("_", " ").replace("-", " ").strip().title()
    return label or field, "", 1


def _metric_sensor_label(tags: dict[str, str]) -> str:
    if not tags:
        return ""
    preferred_keys = ("room", "raum", "name", "sensor", "device", "location", "ort")
    lowered = {key.lower(): value for key, value in tags.items()}
    for key in preferred_keys:
        value = lowered.get(key, "").strip()
        if value:
            return value
    return next((value.strip() for value in tags.values() if value.strip()), "")


def _timezone(config: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(config.get("dashboard", {}).get("timezone", "Europe/Berlin"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _seed_items(
    config: dict[str, Any], section_name: str, default_path: str
) -> list[dict[str, Any]]:
    section = config.get(section_name, {})
    path = resolve_project_path(config, section.get("file", default_path))
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    return items if isinstance(items, list) else []


def _local_iso(value: datetime, timezone: ZoneInfo) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(timezone).isoformat()


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
    database_path = resolve_project_path(
        config, config.get("storage", {}).get("database", "data/dashboard.db")
    )
    store = TodoStore(database_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await asyncio.to_thread(store.initialise)
        await asyncio.to_thread(
            store.seed_stocks, config.get("stocks", {}).get("symbols", [])
        )
        await asyncio.to_thread(
            store.seed_climate_metrics, config.get("climate", {}).get("metrics", [])
        )
        await asyncio.to_thread(
            store.seed_deadlines,
            _seed_items(config, "deadlines", "data/deadlines.json"),
        )
        await asyncio.to_thread(
            store.seed_events,
            _seed_items(config, "events", "data/events.json"),
        )
        yield

    application = FastAPI(
        title="WW14 Dashboard",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.config = config
    application.state.todo_store = store

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "time": datetime.now(UTC).isoformat()}

    @application.get("/api/dashboard")
    async def dashboard(response: Response) -> dict[str, Any]:
        timezone = _timezone(config)
        (
            selected_stocks,
            selected_metrics,
            manual_deadlines,
            manual_events,
            todos,
        ) = await asyncio.gather(
            asyncio.to_thread(store.list_stocks),
            asyncio.to_thread(store.list_climate_metrics),
            asyncio.to_thread(store.list_deadlines),
            asyncio.to_thread(store.list_events),
            asyncio.to_thread(store.list),
        )
        source_config = copy.deepcopy(config)
        source_config.setdefault("stocks", {})["symbols"] = selected_stocks
        source_config.setdefault("climate", {})["metrics"] = selected_metrics
        source_config.setdefault("deadlines", {})["items"] = manual_deadlines
        source_config["deadlines"]["json_feeds"] = []
        source_config.setdefault("events", {})["items"] = manual_events
        source_config["events"]["ical"] = []
        climate, deadlines, events, stocks, weather = await asyncio.gather(
            get_climate(source_config),
            get_deadlines(source_config, timezone),
            get_events(source_config, timezone),
            get_stocks(source_config),
            get_weather(source_config),
        )
        response.headers["Cache-Control"] = "no-store"
        dashboard_config = config.get("dashboard", {})
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "settings": {
                "title": dashboard_config.get("title", "WW14 Dashboard"),
                "subtitle": dashboard_config.get("subtitle", ""),
                "refresh_seconds": int(dashboard_config.get("refresh_seconds", 60)),
                "locale": dashboard_config.get("locale", "de-DE"),
                "timezone": str(timezone),
            },
            "climate": climate,
            "deadlines": deadlines,
            "events": events,
            "stocks": stocks,
            "weather": weather,
            "todos": todos,
        }

    @application.get("/api/stocks")
    async def list_stocks() -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.list_stocks)

    @application.get("/api/stocks/search")
    async def find_stocks(
        q: str = Query(min_length=2, max_length=80),
    ) -> list[dict[str, str]]:
        try:
            return await search_stocks(q.strip())
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Aktiensuche nicht erreichbar: {exc}"
            ) from exc

    @application.post("/api/stocks", status_code=status.HTTP_201_CREATED)
    async def create_stock(payload: StockCreate) -> dict[str, Any]:
        symbol = payload.symbol.strip().upper()
        if not symbol:
            raise HTTPException(status_code=422, detail="Das Aktiensymbol fehlt")
        return await asyncio.to_thread(
            store.add_stock,
            symbol,
            payload.label.strip() or symbol,
            payload.currency.strip().upper(),
        )

    @application.delete("/api/stocks/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_stock(symbol: str) -> Response:
        deleted = await asyncio.to_thread(store.delete_stock, symbol)
        if not deleted:
            raise HTTPException(status_code=404, detail="Aktie nicht gefunden")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/climate/metrics")
    async def list_climate_metrics() -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.list_climate_metrics)

    @application.post("/api/climate/metrics", status_code=status.HTTP_201_CREATED)
    async def create_climate_metric(payload: ClimateMetricCreate) -> dict[str, Any]:
        measurement = payload.measurement.strip()
        field = payload.field.strip()
        if not measurement or not field:
            raise HTTPException(
                status_code=422, detail="Measurement und Field werden benötigt"
            )
        default_label, default_unit, default_decimals = _metric_defaults(field)
        sensor_label = _metric_sensor_label(payload.tags)
        if sensor_label:
            default_label = f"{default_label} · {sensor_label}"
        return await asyncio.to_thread(
            store.add_climate_metric,
            measurement,
            field,
            payload.label.strip() or default_label,
            payload.unit.strip() or default_unit,
            payload.decimals if payload.unit.strip() or payload.label.strip() else default_decimals,
            payload.tags,
        )

    @application.delete(
        "/api/climate/metrics/{metric_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_climate_metric(metric_id: int) -> Response:
        deleted = await asyncio.to_thread(store.delete_climate_metric, metric_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Messwert nicht gefunden")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/deadlines")
    async def list_deadlines() -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.list_deadlines)

    @application.post("/api/deadlines", status_code=status.HTTP_201_CREATED)
    async def create_deadline(payload: DeadlineCreate) -> dict[str, Any]:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Der Titel darf nicht leer sein")
        return await asyncio.to_thread(
            store.add_deadline,
            title,
            _local_iso(payload.due, _timezone(config)),
            payload.kind.strip() or "Deadline",
        )

    @application.delete(
        "/api/deadlines/{deadline_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_deadline(deadline_id: int) -> Response:
        deleted = await asyncio.to_thread(store.delete_deadline, deadline_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Deadline nicht gefunden")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/events")
    async def list_events() -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.list_events)

    @application.post("/api/events", status_code=status.HTTP_201_CREATED)
    async def create_event(payload: EventCreate) -> dict[str, Any]:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Der Titel darf nicht leer sein")
        timezone = _timezone(config)
        start = _local_iso(payload.start, timezone)
        end = _local_iso(payload.end, timezone) if payload.end else None
        if end and datetime.fromisoformat(end) < datetime.fromisoformat(start):
            raise HTTPException(
                status_code=422, detail="Das Ende darf nicht vor dem Beginn liegen"
            )
        return await asyncio.to_thread(
            store.add_event,
            title,
            start,
            end,
            payload.location.strip(),
        )

    @application.delete(
        "/api/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_event(event_id: int) -> Response:
        deleted = await asyncio.to_thread(store.delete_event, event_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Termin nicht gefunden")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/influx/fields")
    async def influx_fields() -> dict[str, Any]:
        if not config.get("influxdb", {}).get("enabled", False):
            raise HTTPException(
                status_code=400,
                detail="InfluxDB ist noch nicht aktiviert (INFLUXDB_ENABLED=true in .env)",
            )
        try:
            fields = await asyncio.to_thread(discover_influx_fields, config)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"InfluxDB konnte nicht gelesen werden: {exc}"
            ) from exc
        return {
            "bucket": config.get("influxdb", {}).get("bucket", ""),
            "items": fields,
        }

    @application.get("/api/todos")
    async def list_todos() -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.list)

    @application.post("/api/todos", status_code=status.HTTP_201_CREATED)
    async def create_todo(payload: TodoCreate) -> dict[str, Any]:
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="Die Aufgabe darf nicht leer sein")
        return await asyncio.to_thread(store.create, text)

    @application.patch("/api/todos/{todo_id}")
    async def update_todo(todo_id: int, payload: TodoUpdate) -> dict[str, Any]:
        text = payload.text.strip() if payload.text is not None else None
        if payload.text is not None and not text:
            raise HTTPException(status_code=422, detail="Die Aufgabe darf nicht leer sein")
        item = await asyncio.to_thread(
            store.update, todo_id, text=text, done=payload.done
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
        return item

    @application.delete("/api/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_todo(todo_id: int) -> Response:
        deleted = await asyncio.to_thread(store.delete, todo_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    static_dir = ROOT_DIR / "app" / "static"
    application.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return application


app = create_app()

