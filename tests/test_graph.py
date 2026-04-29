from relgraph.graph import Item, RelGraph


def test_ingest_links_co_occurring_items_in_session() -> None:
    g = RelGraph()
    for i in range(3):
        g.upsert_item(Item(id=f"i{i}", name=f"Item {i}"))

    g.ingest("u1", "i0", "view", ts=100.0)
    partners = g.ingest("u1", "i1", "view", ts=101.0)

    assert partners == 1
    assert g.edge_weight("i0", "i1") > 0
    assert g.edge_weight("i0", "i2") == 0


def test_purchase_weighs_more_than_view() -> None:
    g = RelGraph()
    g.upsert_item(Item(id="a", name="A"))
    g.upsert_item(Item(id="b", name="B"))
    g.upsert_item(Item(id="c", name="C"))

    g.ingest("u1", "a", "view", ts=10.0)
    g.ingest("u1", "b", "view", ts=11.0)

    g.ingest("u2", "a", "purchase", ts=20.0)
    g.ingest("u2", "c", "purchase", ts=21.0)

    assert g.edge_weight("a", "c") > g.edge_weight("a", "b")


def test_session_gap_resets_window() -> None:
    g = RelGraph()
    g.upsert_item(Item(id="a", name="A"))
    g.upsert_item(Item(id="b", name="B"))

    g.ingest("u1", "a", "view", ts=0.0)
    partners = g.ingest("u1", "b", "view", ts=10_000.0)

    assert partners == 0
    assert g.edge_weight("a", "b") == 0


def test_recommend_orders_by_weight_desc() -> None:
    g = RelGraph()
    for x in ("a", "b", "c", "d"):
        g.upsert_item(Item(id=x, name=x.upper()))

    g.ingest("u1", "a", "view", ts=0.0)
    g.ingest("u1", "b", "purchase", ts=1.0)

    g.ingest("u2", "a", "view", ts=10.0)
    g.ingest("u2", "c", "view", ts=11.0)

    out = g.recommend("a", k=10)
    ids = [r["item_id"] for r in out]

    assert ids[0] == "b"
    assert "c" in ids
    assert "d" not in ids
    assert out[0]["why"].startswith("co-occurred with a")


def test_recommend_unknown_item_raises() -> None:
    g = RelGraph()
    try:
        g.recommend("ghost")
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_search_matches_name_and_tag() -> None:
    g = RelGraph()
    g.upsert_item(Item(id="1", name="Red Shoes", tags=("shoes", "red")))
    g.upsert_item(Item(id="2", name="Blue Hat", tags=("hat", "blue")))
    g.upsert_item(Item(id="3", name="Sneaker Pro", tags=("shoes",)))

    by_name = g.search("shoes")
    ids = {r["item_id"] for r in by_name}
    assert ids == {"1", "3"}

    by_tag = g.search("blue")
    assert [r["item_id"] for r in by_tag] == ["2"]


def test_search_ranks_by_centrality() -> None:
    g = RelGraph()
    for x in ("a", "b", "c", "d"):
        g.upsert_item(Item(id=x, name=f"item {x}", tags=("thing",)))

    g.ingest("u1", "a", "view", ts=0.0)
    g.ingest("u1", "b", "purchase", ts=1.0)
    g.ingest("u1", "c", "purchase", ts=2.0)

    out = g.search("thing")
    assert out[0]["item_id"] in {"b", "c"}
    assert out[-1]["item_id"] == "d"


def test_idle_sessions_are_evicted_and_counted() -> None:
    g = RelGraph(max_sessions=100, session_idle_sec=10.0)
    g.upsert_item(Item(id="x", name="X"))
    g.ingest("alice", "x", "view", ts=0.0)
    g.ingest("bob", "x", "view", ts=5.0)

    # 50s elapsed → both alice and bob exceed idle TTL=10s
    removed = g.evict_idle_sessions(now=50.0)
    assert removed == 2
    s = g.stats()
    assert s["users"] == 0
    assert s["evicted_idle"] == 2
    assert s["evicted_overflow"] == 0


def test_overflow_eviction_drops_oldest_session() -> None:
    g = RelGraph(max_sessions=2, session_idle_sec=1_000_000.0)
    g.upsert_item(Item(id="x", name="X"))
    g.ingest("u1", "x", "view", ts=10.0)
    g.ingest("u2", "x", "view", ts=20.0)
    # third user pushes past max_sessions=2; oldest (u1) gets dropped
    g.ingest("u3", "x", "view", ts=30.0)

    s = g.stats()
    assert s["users"] == 2
    assert s["evicted_overflow"] >= 1
    assert s["evicted_idle"] == 0


def test_stats_exposes_eviction_config() -> None:
    g = RelGraph(max_sessions=42, session_idle_sec=99.0)
    s = g.stats()
    assert s["max_sessions"] == 42
    assert s["session_idle_sec"] == 99.0
    assert s["evicted_idle"] == 0
    assert s["evicted_overflow"] == 0
