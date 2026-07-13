"""Micro-benchmark: indexed n-gram search vs. naive linear substring scan.

Not a pytest test (timing is machine-dependent / flaky as an assertion). Run:

    .venv/bin/python scripts/bench_search.py
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relgraph.graph import Item, RelGraph

N_ITEMS = 50_000
SEED = 42
# Realistic catalog: a large, diverse vocabulary so most queries are selective
# (real product names are not drawn from a pool of 17 words). Query tokens are
# regular members of the vocabulary; their match frequency is naturally low.
_SYLL = ["ka", "ri", "to", "mu", "lo", "ne", "sa", "pi", "du", "ze",
         "fa", "gu", "wi", "xo", "ve", "ba", "cy", "ho", "ju", "me"]
QUERIES = ["kato", "rimu", "loze", "fagu", "wixo", "meba cyho"]


def _make_vocab(rng: random.Random, size: int = 2000) -> list[str]:
    return list({"".join(rng.choice(_SYLL) for _ in range(2)) for _ in range(size * 2)})[:size]


def _linear_search(g: RelGraph, q: str, k: int = 10) -> int:
    q_lower = q.lower()
    hits = []
    for item in g.items():
        name_match = q_lower in item.name.lower()
        tag_match = any(q_lower == t.lower() for t in item.tags)
        if name_match or tag_match:
            base = 2.0 if tag_match else 1.0
            hits.append((item, base * (1.0 + g._centrality(item.id))))
    hits.sort(key=lambda x: (-x[1], x[0].id))
    return len(hits[:k])


def main() -> None:
    rng = random.Random(SEED)
    g = RelGraph()
    vocab = _make_vocab(rng)
    tag_pool = [f"cat{i}" for i in range(30)]
    for i in range(N_ITEMS):
        name = " ".join(rng.sample(vocab, k=rng.randint(2, 4))) + f" {i}"
        tags = tuple(rng.sample(tag_pool, k=rng.randint(0, 3)))
        g.upsert_item(Item(id=f"i{i}", name=name, tags=tags))
    print(f"items: {N_ITEMS}   distinct ngrams indexed: {len(g._ngram_index)}")

    reps = 20
    for q in QUERIES:
        t0 = time.perf_counter()
        for _ in range(reps):
            n_lin = _linear_search(g, q)
        lin_ms = (time.perf_counter() - t0) / reps * 1000

        t0 = time.perf_counter()
        for _ in range(reps):
            n_idx = len(g.search(q))
        idx_ms = (time.perf_counter() - t0) / reps * 1000

        speedup = lin_ms / idx_ms if idx_ms else float("inf")
        print(f"q={q!r:14} linear={lin_ms:7.3f}ms  indexed={idx_ms:7.3f}ms  "
              f"speedup={speedup:6.1f}x  (top-k linear={n_lin} indexed={n_idx})")


if __name__ == "__main__":
    main()
