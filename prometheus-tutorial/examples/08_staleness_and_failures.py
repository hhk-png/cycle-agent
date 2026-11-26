"""Show stale markers, failed scrapes and target removal semantics."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)



import tempfile

from mini_prometheus.config import parse_config
from mini_prometheus.labels import Matcher
from mini_prometheus.promql.engine import Engine
from mini_prometheus.scrape.manager import HttpResponse, ScrapeManager
from mini_prometheus.tsdb import TSDB, TSDBOptions
from mini_prometheus.util.timeutil import is_stale


def body(text: str) -> HttpResponse:
    return HttpResponse(200, {"content-type": "text/plain"}, text.encode())


def run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = parse_config(
            {
                "global": {"scrape_interval": "15s", "scrape_timeout": "5s"},
                "scrape_configs": [
                    {
                        "job_name": "demo",
                        "static_configs": [{"targets": ["127.0.0.1:9100"]}],
                    }
                ],
            }
        )
        tsdb = TSDB(
            TSDBOptions(path=directory, use_lockfile=False)
        ).open()
        responses = iter(
            [
                body("a 1\nb 2\n"),
                body("a 3\n"),
                HttpResponse(500, {}, b"boom"),
            ]
        )
        manager = ScrapeManager(
            tsdb,
            config,
            fetcher=lambda url, headers, timeout: next(responses),
            start_threads=False,
        )
        manager.sync(now=1000)
        target = manager.targets()[0]
        manager.scrape_once(target, timestamp=1000)
        manager.scrape_once(target, timestamp=2000)
        b_rows = tsdb.select([Matcher("__name__", "=", "b")], 0, 10**9)
        print(f"series b samples: {[(s.timestamp, 'stale' if is_stale(s.value) else s.value) for s in b_rows[0][1]]}")
        engine = Engine(tsdb)
        print(f"query b at 2000: {engine.query('b', 2000).value.items}")
        manager.scrape_once(target, timestamp=3000)
        print(f"target health after HTTP 500: {target.health}, error={target.last_error!r}")
        up = tsdb.select([Matcher("__name__", "=", "up")], 0, 10**9)[0][1]
        print(f"up values: {[(s.timestamp, s.value) for s in up]}")
        print(f"query up at 3000: {[(i.labels.to_dict(), i.value) for i in engine.query('up', 3000).value.items]}")
        tsdb.close()


if __name__ == "__main__":
    run()
