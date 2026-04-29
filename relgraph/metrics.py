from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class GraphMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.events = Counter(
            "relgraph_events_total",
            "Total ingest events processed",
            ["action"],
            registry=self.registry,
        )
        self.recommends = Counter(
            "relgraph_recommends_total",
            "Total recommend calls",
            registry=self.registry,
        )
        self.recommend_latency = Histogram(
            "relgraph_recommend_seconds",
            "Recommend latency seconds",
            buckets=(0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
            registry=self.registry,
        )
        self.searches = Counter(
            "relgraph_searches_total",
            "Total search calls",
            registry=self.registry,
        )
        self.session_evictions = Counter(
            "relgraph_session_evictions_total",
            "Sessions evicted",
            ["kind"],
            registry=self.registry,
        )
        self.items_gauge = Gauge(
            "relgraph_items",
            "Current item count",
            registry=self.registry,
        )
        self.edges_gauge = Gauge(
            "relgraph_edges",
            "Current edge count",
            registry=self.registry,
        )
        self.users_gauge = Gauge(
            "relgraph_active_users",
            "Active session count",
            registry=self.registry,
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
