"""A tour of PromQL against an in-memory querier."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)





from mini_prometheus.labels import Labels
from mini_prometheus.promql.engine import Engine, InMemoryQuerier


def run() -> None:
    querier = InMemoryQuerier()
    for code in ("200", "500"):
        querier.add_series(
            Labels({"__name__": "demo_http_requests_total", "code": code, "job": "demo"}),
            [(index * 15_000, index * (2.0 if code == "200" else 0.5)) for index in range(20)],
        )
    for upper, count in (("0.1", 50), ("0.5", 90), ("1", 95), ("+Inf", 100)):
        querier.add_series(
            Labels({"__name__": "demo_request_duration_seconds_bucket", "le": upper}),
            [(index * 15_000, float(index * count)) for index in range(20)],
        )
    engine = Engine(querier)
    now = 300_000
    queries = [
        "demo_http_requests_total",
        'rate(demo_http_requests_total{code="200"}[1m])',
        "sum by (code) (rate(demo_http_requests_total[1m]))",
        "histogram_quantile(0.9, sum by (le) (rate(demo_request_duration_seconds_bucket[1m])))",
        "topk(1, demo_http_requests_total)",
        "demo_http_requests_total > 10",
        "sum(rate(demo_http_requests_total[1m])) * 60",
        'first_over_time(demo_http_requests_total{code="200"}[1m])',
        'ts_of_last_over_time(demo_http_requests_total{code="200"}[1m])',
        'integral(demo_http_requests_total{code="200"}[1m])',
    ]
    print(f"evaluating at t={now}ms")
    for query in queries:
        result = engine.query(query, now)
        if result.value.type == "vector":
            rendered = [
                (item.labels.to_dict(), round(item.value, 4))
                for item in result.value.items
            ]
        else:
            rendered = result.value
        print(f"  {query}\n    -> {rendered}")


if __name__ == "__main__":
    run()
