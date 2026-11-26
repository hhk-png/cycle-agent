"""Scrape a live exporter, then evaluate PromQL over the stored samples."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)





import tempfile

from demo_exporter import DemoExporter
from mini_prometheus.config import parse_config
from mini_prometheus.labels import Matcher
from mini_prometheus.promql.engine import Engine
from mini_prometheus.rules.manager import RuleManager, load_rule_groups
from mini_prometheus.scrape.manager import ScrapeManager
from mini_prometheus.tsdb import TSDB, TSDBOptions


def run() -> None:
    exporter = DemoExporter()
    port = exporter.start()
    try:
        config = parse_config(
            {
                "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
                "scrape_configs": [
                    {
                        "job_name": "demo",
                        "static_configs": [
                            {"targets": [f"127.0.0.1:{port}"], "labels": {"env": "lab"}}
                        ],
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            tsdb = TSDB(
                TSDBOptions(
                    path=directory,
                    block_range_ms=3_600_000,
                    use_lockfile=False,
                )
            ).open()
            manager = ScrapeManager(
                tsdb, config, start_threads=False
            )
            manager.sync(now=1_700_000_000_000)
            target = manager.targets()[0]
            first = manager.scrape_once(target, timestamp=1_700_000_000_000)
            second = manager.scrape_once(target, timestamp=1_700_000_015_000)
            print(f"scrape 1: {first.status}, {first.samples} samples")
            print(f"scrape 2: {second.status}, {second.samples} samples")
            print(f"target labels: {target.target_labels().to_dict()}")
            engine = Engine(tsdb)
            for query in (
                "up",
                'rate(demo_http_requests_total{code="200"}[1m])',
                "histogram_quantile(0.9, sum by (le) (rate(demo_request_duration_seconds_bucket[1m])))",
            ):
                result = engine.query(query, 1_700_000_015_000)
                print(f"  {query} -> {[(item.labels.to_dict(), round(item.value, 4)) for item in result.value.items]}")
            groups = load_rule_groups([], base_dir=".")
            print(f"rule groups loaded: {len(groups)}")
            RuleManager(tsdb, groups).evaluate(1_700_000_015_000)
            rows = tsdb.select([Matcher("__name__", "=", "up")], 0, 2**62)
            print(f"stored up series: {len(rows)}, last value: {rows[0][1][-1].value}")
            tsdb.close()
    finally:
        exporter.stop()


if __name__ == "__main__":
    run()
