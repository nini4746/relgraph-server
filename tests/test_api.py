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


def test_recommend_random_walk_strategy() -> None:
    c = _client()
    items = [{"id": x, "name": x.upper()} for x in ("a", "b", "c", "d")]
    c.post("/items/bulk", json=items)
    for t, (u, i, a, ts) in enumerate([
        ("u1", "a", "purchase", 0),
        ("u1", "b", "purchase", 1),
        ("u2", "a", "purchase", 10),
        ("u2", "b", "purchase", 11),
        ("u3", "a", "view", 100),
        ("u3", "c", "view", 101),
    ]):
        c.post("/events", json={"user_id": u, "item_id": i, "action": a, "ts": ts})
    r = c.post("/recommend", json={"item_id": "a", "k": 3, "strategy": "random_walk", "seed": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["strategy"] == "random_walk"
    ids = [x["item_id"] for x in body["results"]]
    assert "b" in ids
    assert "a" not in ids


def test_recommend_unknown_strategy_returns_400() -> None:
    c = _client()
    c.post("/items", json={"id": "x", "name": "X"})
    r = c.post("/recommend", json={"item_id": "x", "strategy": "magic"})
    assert r.status_code == 400


def test_recommend_user_endpoint() -> None:
    c = _client()
    items = [{"id": x, "name": x.upper()} for x in ("a", "b", "c", "d")]
    c.post("/items/bulk", json=items)
    c.post("/events", json={"user_id": "seed", "item_id": "a", "action": "purchase", "ts": 0})
    c.post("/events", json={"user_id": "seed", "item_id": "d", "action": "purchase", "ts": 1})
    c.post("/events", json={"user_id": "u1", "item_id": "a", "action": "view", "ts": 100})
    c.post("/events", json={"user_id": "u1", "item_id": "b", "action": "view", "ts": 101})
    r = c.post("/recommend/user", json={"user_id": "u1", "k": 5})
    assert r.status_code == 200
    ids = {x["item_id"] for x in r.json()["results"]}
    assert "d" in ids
    assert "a" not in ids and "b" not in ids


def test_subgraph_endpoint_returns_nodes_and_edges() -> None:
    c = _client()
    items = [{"id": x, "name": x.upper()} for x in ("a", "b", "c")]
    c.post("/items/bulk", json=items)
    c.post("/events", json={"user_id": "u1", "item_id": "a", "action": "view", "ts": 0})
    c.post("/events", json={"user_id": "u1", "item_id": "b", "action": "view", "ts": 1})
    r = c.get("/subgraph", params={"item_id": "a", "depth": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["root"] == "a"
    ids = {n["item_id"] for n in body["nodes"]}
    assert {"a", "b"}.issubset(ids)
    assert len(body["edges"]) >= 1


def test_subgraph_unknown_item_returns_404() -> None:
    c = _client()
    r = c.get("/subgraph", params={"item_id": "ghost"})
    assert r.status_code == 404


def test_delete_item_endpoint() -> None:
    c = _client()
    c.post("/items", json={"id": "x", "name": "X"})
    c.post("/items", json={"id": "y", "name": "Y"})
    c.post("/events", json={"user_id": "u", "item_id": "x", "action": "view", "ts": 0})
    c.post("/events", json={"user_id": "u", "item_id": "y", "action": "view", "ts": 1})
    r = c.delete("/items/x")
    assert r.status_code == 200
    assert r.json()["edges_removed"] == 1
    r2 = c.delete("/items/x")
    assert r2.status_code == 404


def test_admin_decay_endpoint() -> None:
    c = _client()
    items = [{"id": x, "name": x.upper()} for x in ("a", "b")]
    c.post("/items/bulk", json=items)
    c.post("/events", json={"user_id": "u", "item_id": "a", "action": "view", "ts": 0})
    c.post("/events", json={"user_id": "u", "item_id": "b", "action": "view", "ts": 1})
    r = c.post("/admin/decay", json={"factor": 0.5})
    assert r.status_code == 200
    assert "pruned" in r.json()


def test_admin_decay_invalid_returns_400() -> None:
    c = _client()
    r = c.post("/admin/decay", json={"factor": 2.0})
    assert r.status_code == 400


def test_admin_compact_without_wal_returns_409(tmp_path) -> None:
    c = _client()  # no WAL configured
    r = c.post("/admin/compact", json={"snapshot_path": str(tmp_path / "snap.json")})
    assert r.status_code == 409
