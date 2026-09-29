from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import ROOT_DIR, load_config, resolve_project_path
from app.sources import get_climate, get_deadlines, get_events, get_stocks
from app.store import TodoStore


class TodoCreate(BaseModel):
    text: str = Field(min_length=1, max_length=240)


class TodoUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=240)
    done: bool | None = None


def _timezone(config: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(config.get("dashboard", {}).get("timezone", "Europe/Berlin"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
    database_path = resolve_project_path(
        config, config.get("storage", {}).get("database", "data/dashboard.db")
    )
    store = TodoStore(database_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await asyncio.to_thread(store.initialise)
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
        climate, deadlines, events, stocks, todos = await asyncio.gather(
            get_climate(config),
            get_deadlines(config, timezone),
            get_events(config, timezone),
            get_stocks(config),
            asyncio.to_thread(store.list),
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
            "todos": todos,
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

