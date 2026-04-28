"""10k 아이템 / 500k 이벤트로 그래프를 채우고 추천 P50/P95 측정."""

from __future__ import annotations

import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relgraph.graph import Item, RelGraph

N_ITEMS = 10_000
N_EVENTS = 500_000
N_USERS = 5_000
SEED = 42

ACTIONS = ["view", "view", "view", "click", "click", "cart", "purchase"]
TAG_POOL = [f"tag{i}" for i in range(50)]


def main() -> None:
    rng = random.Random(SEED)
    g = RelGraph()

    for i in range(N_ITEMS):
        tags = tuple(rng.sample(TAG_POOL, k=rng.randint(1, 3)))
        g.upsert_item(Item(id=f"i{i}", name=f"Item {i}", tags=tags))

    t0 = time.perf_counter()
    base = 0.0
    for _ in range(N_EVENTS):
        user = f"u{rng.randint(0, N_USERS - 1)}"
        item = f"i{rng.randint(0, N_ITEMS - 1)}"
        action = rng.choice(ACTIONS)
        base += rng.uniform(0.1, 30.0)
        g.ingest(user, item, action, ts=base)
    t1 = time.perf_counter()
    print(f"ingest: {N_EVENTS} events in {t1 - t0:.2f}s ({N_EVENTS / (t1 - t0):,.0f} ev/s)")
    print(f"stats:  {g.stats()}")

    targets = [f"i{rng.randint(0, N_ITEMS - 1)}" for _ in range(2_000)]
    latencies = []
    for it in targets:
        s = time.perf_counter()
        g.recommend(it, k=10)
        latencies.append((time.perf_counter() - s) * 1000)
    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]
    print(f"recommend latency (ms): p50={p50:.3f}  p95={p95:.3f}  p99={p99:.3f}")


if __name__ == "__main__":
    main()
