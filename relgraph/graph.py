from __future__ import annotations

import json
import os
import random
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Iterable


ACTION_WEIGHTS: dict[str, float] = {
    "view": 1.0,
    "click": 2.0,
    "cart": 4.0,
    "purchase": 8.0,
}

SESSION_WINDOW = 5
SESSION_GAP_SEC = 30 * 60
MAX_SESSIONS = 100_000
SESSION_IDLE_SEC = 24 * 60 * 60
MAX_ITEMS = 1_000_000
MAX_EDGES = 5_000_000


@dataclass
class Item:
    id: str
    name: str
    tags: tuple[str, ...] = ()


@dataclass
class _Session:
    events: deque = field(default_factory=lambda: deque(maxlen=SESSION_WINDOW))
    last_ts: float = 0.0


def _edge_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class RelGraph:
    def __init__(self, max_sessions: int = MAX_SESSIONS, session_idle_sec: float = SESSION_IDLE_SEC,
                 max_items: int = MAX_ITEMS, max_edges: int = MAX_EDGES, wal=None) -> None:
        self._items: dict[str, Item] = {}
        self._edges: dict[tuple[str, str], float] = defaultdict(float)
        self._neighbors: dict[str, set[str]] = defaultdict(set)
        self._sessions: dict[str, _Session] = {}
        self._lock = Lock()
        self._max_sessions = max_sessions
        self._session_idle_sec = session_idle_sec
        self._max_items = max_items
        self._max_edges = max_edges
        self._evicted_idle = 0
        self._evicted_overflow = 0
        self._evicted_edges = 0
        self._rejected_items = 0
        self._wal = wal  # optional WriteAheadLog instance

    def upsert_item(self, item: Item) -> None:
        with self._lock:
            if item.id not in self._items and len(self._items) >= self._max_items:
                self._rejected_items += 1
                raise ValueError(f"item cap reached ({self._max_items})")
            self._items[item.id] = item
        if self._wal is not None:
            self._wal.append("upsert_item",
                             {"id": item.id, "name": item.name, "tags": list(item.tags)})

    def items(self) -> list[Item]:
        with self._lock:
            return list(self._items.values())

    def get_item(self, item_id: str) -> Item | None:
        with self._lock:
            return self._items.get(item_id)

    def ingest(self, user_id: str, item_id: str, action: str, ts: float) -> int:
        if item_id not in self._items:
            raise KeyError(item_id)
        weight = ACTION_WEIGHTS.get(action)
        if weight is None:
            raise ValueError(f"unknown action: {action}")
        with self._lock:
            session = self._sessions.get(user_id)
            if session is None:
                if len(self._sessions) >= self._max_sessions:
                    self._evict_sessions_locked(ts)
                session = _Session()
                self._sessions[user_id] = session
            if ts - session.last_ts > SESSION_GAP_SEC:
                session.events.clear()
            partners = [e for e in session.events if e[0] != item_id]
            for partner_id, partner_ts in partners:
                gap_decay = max(0.1, 1.0 - (ts - partner_ts) / SESSION_GAP_SEC)
                key = _edge_key(item_id, partner_id)
                if key not in self._edges and len(self._edges) >= self._max_edges:
                    self._evict_weakest_edge_locked()
                self._edges[key] += weight * gap_decay
                self._neighbors[item_id].add(partner_id)
                self._neighbors[partner_id].add(item_id)
            session.events.append((item_id, ts))
            session.last_ts = ts
        if self._wal is not None:
            self._wal.append("ingest",
                             {"user_id": user_id, "item_id": item_id, "action": action, "ts": ts})
        return len(partners)

    def edge_weight(self, a: str, b: str) -> float:
        with self._lock:
            return self._edges.get(_edge_key(a, b), 0.0)

    def recommend(self, item_id: str, k: int = 10) -> list[dict]:
        if item_id not in self._items:
            raise KeyError(item_id)
        with self._lock:
            neighbors = list(self._neighbors.get(item_id, ()))
            scored = []
            for nb in neighbors:
                w = self._edges[_edge_key(item_id, nb)]
                scored.append((nb, w))
            scored.sort(key=lambda x: (-x[1], x[0]))
            out = []
            for nb_id, w in scored[:k]:
                nb = self._items[nb_id]
                out.append(
                    {
                        "item_id": nb_id,
                        "name": nb.name,
                        "score": round(w, 4),
                        "why": f"co-occurred with {item_id} (weight={round(w, 2)})",
                    }
                )
            return out

    def recommend_random_walk(self, item_id: str, k: int = 10, *,
                              walks: int = 200, depth: int = 3,
                              restart: float = 0.15, seed: int | None = None) -> list[dict]:
        """Personalized PageRank-ish via short random walks with restart.
        Edges sampled proportionally to weight; visit counts (excluding seed) scored.
        """
        if item_id not in self._items:
            raise KeyError(item_id)
        rng = random.Random(seed)
        with self._lock:
            visits: dict[str, int] = defaultdict(int)
            for _ in range(walks):
                node = item_id
                for _ in range(depth):
                    if rng.random() < restart:
                        node = item_id
                        continue
                    nbs = list(self._neighbors.get(node, ()))
                    if not nbs:
                        break
                    weights = [self._edges[_edge_key(node, nb)] for nb in nbs]
                    total = sum(weights)
                    if total <= 0:
                        break
                    r = rng.random() * total
                    acc = 0.0
                    pick = nbs[-1]
                    for nb, w in zip(nbs, weights):
                        acc += w
                        if r <= acc:
                            pick = nb
                            break
                    node = pick
                    if node != item_id:
                        visits[node] += 1
            scored = sorted(visits.items(), key=lambda x: (-x[1], x[0]))
            out = []
            for nb_id, count in scored[:k]:
                if nb_id not in self._items:
                    continue
                nb = self._items[nb_id]
                edge_w = self._edges.get(_edge_key(item_id, nb_id), 0.0)
                hops = "direct" if edge_w > 0 else "multi-hop"
                out.append({
                    "item_id": nb_id,
                    "name": nb.name,
                    "score": count,
                    "why": f"random-walk visits={count} from {item_id} ({hops})",
                })
            return out

    def recommend_for_user(self, user_id: str, k: int = 10) -> list[dict]:
        """Aggregate recommendations from items in user's recent session window.
        Excludes items the user already touched. Scores summed across seeds.
        """
        with self._lock:
            session = self._sessions.get(user_id)
            if session is None or not session.events:
                return []
            seen = {ev[0] for ev in session.events}
            agg: dict[str, float] = defaultdict(float)
            seeds: dict[str, list[str]] = defaultdict(list)
            for seed_id, _ in session.events:
                for nb in self._neighbors.get(seed_id, ()):
                    if nb in seen:
                        continue
                    w = self._edges[_edge_key(seed_id, nb)]
                    agg[nb] += w
                    seeds[nb].append(seed_id)
            scored = sorted(agg.items(), key=lambda x: (-x[1], x[0]))
            out = []
            for nb_id, score in scored[:k]:
                if nb_id not in self._items:
                    continue
                nb = self._items[nb_id]
                contrib = ",".join(seeds[nb_id][:3])
                out.append({
                    "item_id": nb_id,
                    "name": nb.name,
                    "score": round(score, 4),
                    "why": f"co-occurred with session [{contrib}] (sum_w={round(score, 2)})",
                })
            return out

    def subgraph(self, item_id: str, depth: int = 1, max_nodes: int = 64) -> dict:
        """Return BFS subgraph (nodes + edges) up to `depth` hops."""
        if item_id not in self._items:
            raise KeyError(item_id)
        with self._lock:
            visited = {item_id}
            frontier = {item_id}
            for _ in range(max(0, depth)):
                nxt = set()
                for n in frontier:
                    for nb in self._neighbors.get(n, ()):
                        if nb in visited:
                            continue
                        if len(visited) >= max_nodes:
                            break
                        visited.add(nb)
                        nxt.add(nb)
                    if len(visited) >= max_nodes:
                        break
                frontier = nxt
            nodes = []
            for nid in visited:
                it = self._items.get(nid)
                if it is None:
                    continue
                nodes.append({"item_id": nid, "name": it.name, "tags": list(it.tags)})
            edges = []
            seen_edge: set[tuple[str, str]] = set()
            for n in visited:
                for nb in self._neighbors.get(n, ()):
                    if nb not in visited:
                        continue
                    key = _edge_key(n, nb)
                    if key in seen_edge:
                        continue
                    seen_edge.add(key)
                    edges.append({"a": key[0], "b": key[1], "w": round(self._edges[key], 4)})
            return {"root": item_id, "depth": depth, "nodes": nodes, "edges": edges}

    def remove_item(self, item_id: str) -> int:
        """Delete an item and all its edges. Returns edges removed."""
        with self._lock:
            if item_id not in self._items:
                raise KeyError(item_id)
            nbs = list(self._neighbors.get(item_id, ()))
            removed = 0
            for nb in nbs:
                key = _edge_key(item_id, nb)
                if self._edges.pop(key, None) is not None:
                    removed += 1
                self._neighbors[nb].discard(item_id)
                if not self._neighbors[nb]:
                    del self._neighbors[nb]
            self._neighbors.pop(item_id, None)
            del self._items[item_id]
            return removed

    def decay_edges(self, factor: float, prune_below: float = 0.0) -> int:
        """Multiply all edge weights by `factor` (0<f<=1). Edges <= prune_below removed.
        Returns count of edges pruned."""
        if not (0.0 < factor <= 1.0):
            raise ValueError("factor must be in (0,1]")
        with self._lock:
            pruned = 0
            for key in list(self._edges.keys()):
                self._edges[key] *= factor
                if self._edges[key] <= prune_below:
                    a, b = key
                    del self._edges[key]
                    self._neighbors[a].discard(b)
                    self._neighbors[b].discard(a)
                    if not self._neighbors[a]:
                        self._neighbors.pop(a, None)
                    if not self._neighbors[b]:
                        self._neighbors.pop(b, None)
                    pruned += 1
            return pruned

    def compact_wal(self, snapshot_path: str) -> dict:
        """Snapshot current state then rotate the WAL. Net effect: WAL shrinks to empty.
        Returns dict with snapshot stats and archived wal path (or None)."""
        if self._wal is None:
            raise RuntimeError("no WAL configured")
        stats = self.snapshot_to_file(snapshot_path)
        suffix = f"compacted-{int(__import__('time').time()*1000)}"
        archived = self._wal.rotate(suffix)
        return {"snapshot": stats, "archived_wal": archived}

    def _centrality(self, item_id: str) -> float:
        return sum(self._edges[_edge_key(item_id, nb)] for nb in self._neighbors.get(item_id, ()))

    def search(self, q: str, k: int = 10) -> list[dict]:
        q_lower = q.lower()
        with self._lock:
            hits = []
            for item in self._items.values():
                name_match = q_lower in item.name.lower()
                tag_match = any(q_lower == t.lower() for t in item.tags)
                if not (name_match or tag_match):
                    continue
                base = 2.0 if tag_match else 1.0
                score = base * (1.0 + self._centrality(item.id))
                hits.append((item, score))
            hits.sort(key=lambda x: (-x[1], x[0].id))
            return [
                {
                    "item_id": it.id,
                    "name": it.name,
                    "tags": list(it.tags),
                    "score": round(s, 4),
                }
                for it, s in hits[:k]
            ]

    def stats(self) -> dict:
        with self._lock:
            return {
                "items": len(self._items),
                "edges": len(self._edges),
                "users": len(self._sessions),
                "max_sessions": self._max_sessions,
                "session_idle_sec": self._session_idle_sec,
                "max_items": self._max_items,
                "max_edges": self._max_edges,
                "evicted_idle": self._evicted_idle,
                "evicted_overflow": self._evicted_overflow,
                "evicted_edges": self._evicted_edges,
                "rejected_items": self._rejected_items,
            }

    def bulk_upsert(self, items: Iterable[Item]) -> None:
        with self._lock:
            for it in items:
                self._items[it.id] = it

    def _evict_sessions_locked(self, now: float) -> int:
        cutoff = now - self._session_idle_sec
        idle = [uid for uid, s in self._sessions.items() if s.last_ts < cutoff]
        for uid in idle:
            del self._sessions[uid]
        self._evicted_idle += len(idle)
        overflow = 0
        if len(self._sessions) >= self._max_sessions:
            ranked = sorted(self._sessions.items(), key=lambda kv: kv[1].last_ts)
            drop = len(self._sessions) - self._max_sessions + 1
            for uid, _ in ranked[:drop]:
                del self._sessions[uid]
                overflow += 1
            self._evicted_overflow += overflow
        return len(idle) + overflow

    def evict_idle_sessions(self, now: float) -> int:
        with self._lock:
            return self._evict_sessions_locked(now)

    def replay_wal(self, wal) -> int:
        """Replay a WAL into this graph. WAL writes are temporarily disabled to avoid
        re-logging the replayed entries. Returns count of entries successfully applied."""
        prior = self._wal
        self._wal = None
        applied = 0
        try:
            for entry in wal.replay():
                op = entry.get("op")
                if op == "upsert_item":
                    self.upsert_item(Item(id=entry["id"], name=entry["name"],
                                          tags=tuple(entry.get("tags", []))))
                    applied += 1
                elif op == "ingest":
                    try:
                        self.ingest(user_id=entry["user_id"], item_id=entry["item_id"],
                                    action=entry["action"], ts=float(entry["ts"]))
                        applied += 1
                    except (KeyError, ValueError):
                        # item missing or unknown action — skip, advance log
                        continue
        finally:
            self._wal = prior
        return applied

    def _evict_weakest_edge_locked(self) -> None:
        # find the lowest-weight edge and remove it; also update neighbor sets.
        weakest_key, _ = min(self._edges.items(), key=lambda kv: kv[1])
        a, b = weakest_key
        del self._edges[weakest_key]
        # only remove neighbor link if no other edge connects them (always true since each pair has 1 edge)
        self._neighbors[a].discard(b)
        self._neighbors[b].discard(a)
        if not self._neighbors[a]:
            del self._neighbors[a]
        if not self._neighbors[b]:
            del self._neighbors[b]
        self._evicted_edges += 1

    def snapshot_to_file(self, path: str) -> dict:
        """Atomically write items + edges + neighbors to a JSON file. Returns counts."""
        with self._lock:
            payload = {
                "version": 1,
                "items": [{"id": it.id, "name": it.name, "tags": list(it.tags)}
                          for it in self._items.values()],
                "edges": [{"a": k[0], "b": k[1], "w": w} for k, w in self._edges.items()],
                "neighbors": {k: sorted(v) for k, v in self._neighbors.items()},
                "stats": {
                    "items": len(self._items),
                    "edges": len(self._edges),
                    "evicted_idle": self._evicted_idle,
                    "evicted_overflow": self._evicted_overflow,
                },
            }
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".relgraph-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return payload["stats"]

    def load_from_file(self, path: str) -> dict:
        """Restore items/edges/neighbors from a snapshot. Sessions are NOT restored (ephemeral)."""
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("version") != 1:
            raise ValueError(f"unsupported snapshot version: {payload.get('version')!r}")
        with self._lock:
            self._items = {it["id"]: Item(id=it["id"], name=it["name"], tags=tuple(it["tags"]))
                           for it in payload["items"]}
            self._edges = defaultdict(float)
            for e in payload["edges"]:
                self._edges[_edge_key(e["a"], e["b"])] = float(e["w"])
            self._neighbors = defaultdict(set)
            for k, v in payload["neighbors"].items():
                self._neighbors[k] = set(v)
        return payload["stats"]
