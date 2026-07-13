"""Tests for the inverted character n-gram search index.

The index must return *exactly* the same results as the old linear substring
scan (same semantics: name substring OR exact tag match, centrality-ranked),
while staying consistent under item eviction and WAL/snapshot recovery.
"""

from __future__ import annotations

import random

from relgraph.graph import Item, RelGraph
from relgraph.wal import WriteAheadLog


def _linear_search(g: RelGraph, q: str, k: int = 10) -> list[dict]:
    """Reference implementation: the pre-index O(N) full scan. Used only to
    prove the indexed path produces identical output."""
    q_lower = q.lower()
    hits = []
    for item in g.items():
        name_match = q_lower in item.name.lower()
        tag_match = any(q_lower == t.lower() for t in item.tags)
        if not (name_match or tag_match):
            continue
        base = 2.0 if tag_match else 1.0
        score = base * (1.0 + g._centrality(item.id))
        hits.append((item, score))
    hits.sort(key=lambda x: (-x[1], x[0].id))
    return [
        {"item_id": it.id, "name": it.name, "tags": list(it.tags), "score": round(s, 4)}
        for it, s in hits[:k]
    ]


def _seed_graph() -> RelGraph:
    rng = random.Random(1234)
    g = RelGraph()
    words = ["red", "blue", "green", "shoe", "hat", "apple", "banana",
             "pro", "max", "mini", "run", "sky", "sun", "leaf", "gold"]
    tag_pool = [f"cat{i}" for i in range(8)] + words
    for i in range(600):
        name = " ".join(rng.sample(words, k=rng.randint(1, 3))) + f" {i}"
        tags = tuple(rng.sample(tag_pool, k=rng.randint(0, 3)))
        g.upsert_item(Item(id=f"i{i}", name=name, tags=tags))
    # add some co-occurrence so centrality varies (affects ranking)
    for t in range(2000):
        u = f"u{rng.randint(0, 200)}"
        it = f"i{rng.randint(0, 599)}"
        g.ingest(u, it, rng.choice(["view", "click", "purchase"]), ts=float(t))
    return g


def test_index_search_matches_linear_scan_on_seeded_dataset() -> None:
    g = _seed_graph()
    queries = ["red", "blue", "shoe", "apple", "pro", "green", "cat3",
               "un", "an", "ax", "leaf", "zzz", "gold", "sky sun", "banana"]
    for q in queries:
        assert g.search(q, k=25) == _linear_search(g, q, k=25), f"mismatch for {q!r}"


def test_index_search_still_matches_name_and_tag() -> None:
    g = RelGraph()
    g.upsert_item(Item(id="1", name="Red Shoes", tags=("shoes", "red")))
    g.upsert_item(Item(id="2", name="Blue Hat", tags=("hat", "blue")))
    g.upsert_item(Item(id="3", name="Sneaker Pro", tags=("shoes",)))

    assert {r["item_id"] for r in g.search("shoes")} == {"1", "3"}  # exact tag
    assert [r["item_id"] for r in g.search("blue")] == ["2"]        # tag + name substr
    assert {r["item_id"] for r in g.search("sho")} == {"1"}         # name substring only


def test_update_reindexes_old_ngrams_dropped() -> None:
    g = RelGraph()
    g.upsert_item(Item(id="1", name="Apple", tags=()))
    assert {r["item_id"] for r in g.search("app")} == {"1"}
    # rename: old name must no longer be findable, new one must be
    g.upsert_item(Item(id="1", name="Banana", tags=()))
    assert g.search("app") == []
    assert {r["item_id"] for r in g.search("ban")} == {"1"}


def test_eviction_consistency_removed_item_not_findable() -> None:
    g = _seed_graph()
    q = "apple"
    before = {r["item_id"] for r in g.search(q, k=500)}
    assert before, "test needs at least one match"
    victim = next(iter(before))
    g.remove_item(victim)
    after = {r["item_id"] for r in g.search(q, k=500)}
    assert victim not in after
    assert after == before - {victim}  # k=500 > total matches, so no truncation churn
    # index agrees with a fresh linear scan post-eviction
    assert g.search(q, k=50) == _linear_search(g, q, k=50)


def test_recovery_consistency_wal_replay_rebuilds_index(tmp_path) -> None:
    wal_path = str(tmp_path / "wal.jsonl")
    wal = WriteAheadLog(wal_path)
    g = RelGraph(wal=wal)
    g.upsert_item(Item(id="a", name="Apple", tags=("fruit",)))
    g.upsert_item(Item(id="b", name="Banana", tags=("fruit",)))
    g.ingest("u1", "a", "view", ts=1.0)
    g.ingest("u1", "b", "purchase", ts=2.0)
    wal.close()

    wal2 = WriteAheadLog(wal_path)
    g2 = RelGraph()
    g2.replay_wal(wal2)
    wal2.close()

    assert {r["item_id"] for r in g2.search("fruit")} == {"a", "b"}
    assert {r["item_id"] for r in g2.search("app")} == {"a"}


def test_recovery_consistency_snapshot_rebuilds_index(tmp_path) -> None:
    g = _seed_graph()
    snap = str(tmp_path / "snap.json")
    g.snapshot_to_file(snap)
    restored = RelGraph()
    restored.load_from_file(snap)
    for q in ["apple", "blue", "shoe", "cat3", "gold"]:
        assert restored.search(q, k=50) == _linear_search(restored, q, k=50)


def test_short_query_edge_bounded_fallback() -> None:
    g = RelGraph()
    g.upsert_item(Item(id="1", name="Apple", tags=("a",)))
    g.upsert_item(Item(id="2", name="Banana", tags=()))
    g.upsert_item(Item(id="3", name="Cat", tags=()))
    # 1-char query < NGRAM_N=2 -> bounded fallback scan, still correct semantics
    got = {r["item_id"] for r in g.search("a")}
    # name substring "a": Apple, Banana, Cat ; exact tag "a": Apple
    assert got == {"1", "2", "3"}
    assert g.search("a", k=10) == _linear_search(g, "a", k=10)
    # empty query returns everything (substring "" in every name), same as before
    assert {r["item_id"] for r in g.search("")} == {"1", "2", "3"}


def test_bulk_upsert_is_indexed() -> None:
    g = RelGraph()
    g.bulk_upsert([
        Item(id="1", name="Alpha", tags=("x",)),
        Item(id="2", name="Beta", tags=("y",)),
    ])
    assert {r["item_id"] for r in g.search("alph")} == {"1"}
    assert {r["item_id"] for r in g.search("x")} == {"1"}
