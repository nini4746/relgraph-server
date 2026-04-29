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
MAX_SESSIONS = 100_000
SESSION_IDLE_SEC = 24 * 60 * 60


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
    def __init__(self, max_sessions: int = MAX_SESSIONS, session_idle_sec: float = SESSION_IDLE_SEC) -> None:
        self._items: dict[str, Item] = {}
        self._edges: dict[tuple[str, str], float] = defaultdict(float)
        self._neighbors: dict[str, set[str]] = defaultdict(set)
        self._sessions: dict[str, _Session] = {}
        self._lock = Lock()
        self._max_sessions = max_sessions
        self._session_idle_sec = session_idle_sec
        self._evicted_idle = 0
        self._evicted_overflow = 0

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
                "max_sessions": self._max_sessions,
                "session_idle_sec": self._session_idle_sec,
                "evicted_idle": self._evicted_idle,
                "evicted_overflow": self._evicted_overflow,
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
