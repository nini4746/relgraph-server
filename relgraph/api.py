from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from time import time

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from .graph import MAX_SESSIONS, SESSION_IDLE_SEC, Item, RelGraph
from .metrics import GraphMetrics


class ItemIn(BaseModel):
    id: str
    name: str
    tags: list[str] = Field(default_factory=list)


class EventIn(BaseModel):
    user_id: str
    item_id: str
    action: str
    ts: float | None = None


class RecommendIn(BaseModel):
    item_id: str
    k: int = 10


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def create_app(graph: RelGraph | None = None) -> FastAPI:
    if graph is None:
        max_sessions = _env_int("RELGRAPH_MAX_SESSIONS", MAX_SESSIONS)
        idle_sec = _env_float("RELGRAPH_SESSION_IDLE_SEC", float(SESSION_IDLE_SEC))
        g = RelGraph(max_sessions=max_sessions, session_idle_sec=idle_sec)
    else:
        g = graph

    sweep_interval = _env_float("RELGRAPH_SWEEP_INTERVAL_SEC", 0.0)
    metrics = GraphMetrics()
    last_evicted = {"idle": 0, "overflow": 0}

    def refresh_metrics() -> None:
        s = g.stats()
        metrics.items_gauge.set(s["items"])
        metrics.edges_gauge.set(s["edges"])
        metrics.users_gauge.set(s["users"])
        idle_delta = s["evicted_idle"] - last_evicted["idle"]
        overflow_delta = s["evicted_overflow"] - last_evicted["overflow"]
        if idle_delta > 0:
            metrics.session_evictions.labels(kind="idle").inc(idle_delta)
            last_evicted["idle"] = s["evicted_idle"]
        if overflow_delta > 0:
            metrics.session_evictions.labels(kind="overflow").inc(overflow_delta)
            last_evicted["overflow"] = s["evicted_overflow"]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if sweep_interval > 0:
            async def sweeper():
                while True:
                    await asyncio.sleep(sweep_interval)
                    g.evict_idle_sessions(time())
            task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(title="relgraph-server", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, **g.stats()}

    @app.post("/items", status_code=201)
    def upsert_item(payload: ItemIn) -> dict:
        g.upsert_item(Item(id=payload.id, name=payload.name, tags=tuple(payload.tags)))
        return {"item_id": payload.id}

    @app.post("/items/bulk", status_code=201)
    def bulk_items(payload: list[ItemIn]) -> dict:
        g.bulk_upsert(Item(id=p.id, name=p.name, tags=tuple(p.tags)) for p in payload)
        return {"count": len(payload)}

    @app.post("/events", status_code=202)
    def event(payload: EventIn) -> dict:
        try:
            partners = g.ingest(
                user_id=payload.user_id,
                item_id=payload.item_id,
                action=payload.action,
                ts=payload.ts if payload.ts is not None else time(),
            )
        except KeyError as e:
            raise HTTPException(404, f"unknown item: {e.args[0]}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        metrics.events.labels(action=payload.action).inc()
        return {"linked": partners}

    @app.post("/recommend")
    def recommend(payload: RecommendIn) -> dict:
        try:
            with metrics.recommend_latency.time():
                results = g.recommend(payload.item_id, payload.k)
        except KeyError:
            raise HTTPException(404, f"unknown item: {payload.item_id}")
        metrics.recommends.inc()
        return {"item_id": payload.item_id, "results": results}

    @app.get("/search")
    def search(q: str, k: int = 10) -> dict:
        results = g.search(q, k)
        metrics.searches.inc()
        return {"q": q, "results": results}

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        refresh_metrics()
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()
