from fastapi.testclient import TestClient

from relgraph.api import create_app
from relgraph.graph import RelGraph


def _client() -> TestClient:
    return TestClient(create_app(RelGraph()))


def test_health_returns_stats() -> None:
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["items"] == 0


def test_full_flow_ingest_recommend_search() -> None:
    c = _client()
    items = [
        {"id": "p1", "name": "Apple", "tags": ["fruit", "red"]},
        {"id": "p2", "name": "Banana", "tags": ["fruit", "yellow"]},
        {"id": "p3", "name": "Cherry", "tags": ["fruit", "red"]},
    ]
    assert c.post("/items/bulk", json=items).status_code == 201

    events = [
        ("u1", "p1", "view", 0.0),
        ("u1", "p2", "purchase", 1.0),
        ("u2", "p1", "view", 5.0),
        ("u2", "p3", "purchase", 6.0),
    ]
    for u, i, a, ts in events:
        r = c.post("/events", json={"user_id": u, "item_id": i, "action": a, "ts": ts})
        assert r.status_code == 202

    rec = c.post("/recommend", json={"item_id": "p1", "k": 5}).json()
    rec_ids = [r["item_id"] for r in rec["results"]]
    assert set(rec_ids) == {"p2", "p3"}
    assert rec["results"][0]["why"].startswith("co-occurred with p1")

    s = c.get("/search", params={"q": "fruit"}).json()
    assert {r["item_id"] for r in s["results"]} == {"p1", "p2", "p3"}


def test_event_unknown_item_returns_404() -> None:
    c = _client()
    r = c.post("/events", json={"user_id": "u", "item_id": "ghost", "action": "view", "ts": 0})
    assert r.status_code == 404


def test_event_unknown_action_returns_400() -> None:
    c = _client()
    c.post("/items", json={"id": "x", "name": "X"})
    r = c.post("/events", json={"user_id": "u", "item_id": "x", "action": "stare", "ts": 0})
    assert r.status_code == 400


def test_metrics_endpoint_exposes_counters() -> None:
    c = _client()
    c.post("/items", json={"id": "m1", "name": "M1"})
    c.post("/items", json={"id": "m2", "name": "M2"})
    c.post("/events", json={"user_id": "u", "item_id": "m1", "action": "view", "ts": 0})
    c.post("/events", json={"user_id": "u", "item_id": "m2", "action": "purchase", "ts": 1})
    c.post("/recommend", json={"item_id": "m1", "k": 5})
    c.get("/search", params={"q": "M"})

    r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "relgraph_events_total" in body
    assert "relgraph_recommends_total" in body
    assert "relgraph_searches_total" in body
    assert "relgraph_items" in body
