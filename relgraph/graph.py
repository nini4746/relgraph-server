from __future__ import annotations

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
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}
        self._edges: dict[tuple[str, str], float] = defaultdict(float)
        self._neighbors: dict[str, set[str]] = defaultdict(set)
        self._sessions: dict[str, _Session] = defaultdict(_Session)
        self._lock = Lock()

    def upsert_item(self, item: Item) -> None:
        with self._lock:
            self._items[item.id] = item

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
            session = self._sessions[user_id]
            if ts - session.last_ts > SESSION_GAP_SEC:
                session.events.clear()
            partners = [e for e in session.events if e[0] != item_id]
            for partner_id, partner_ts in partners:
                gap_decay = max(0.1, 1.0 - (ts - partner_ts) / SESSION_GAP_SEC)
                key = _edge_key(item_id, partner_id)
                self._edges[key] += weight * gap_decay
                self._neighbors[item_id].add(partner_id)
                self._neighbors[partner_id].add(item_id)
            session.events.append((item_id, ts))
            session.last_ts = ts
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
            }

    def bulk_upsert(self, items: Iterable[Item]) -> None:
        with self._lock:
            for it in items:
                self._items[it.id] = it
