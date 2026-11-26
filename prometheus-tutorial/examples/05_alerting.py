"""Drive the alert state machine and deliver alerts to Alertmanager-lite."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)



import time

from alertmanager_lite import AlertmanagerLite
from mini_prometheus.config import AlertmanagerConfig
from mini_prometheus.labels import Labels
from mini_prometheus.promql.parser import parse_expr
from mini_prometheus.rules.manager import AlertingRule, RuleGroup, RuleManager
from mini_prometheus.rules.notifier import AlertmanagerNotifier
from mini_prometheus.tsdb import TSDB, TSDBOptions
import tempfile


def run() -> None:
    receiver = AlertmanagerLite()
    port = receiver.start()
    try:
        now = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as directory:
            tsdb = TSDB(
                TSDBOptions(path=directory, use_lockfile=False)
            ).open()
            tsdb.append(
                Labels({"__name__": "demo_errors_total", "service": "api"}), now, 5
            )
            tsdb.append(
                Labels({"__name__": "demo_up", "service": "api"}), now, 0
            )
            warning = AlertingRule(
                "HighErrorRate",
                "demo_errors_total > 0",
                parse_expr("demo_errors_total > 0"),
                {"severity": "warning", "service": "api"},
                {"summary": "{{ $labels.service }} has {{ $value }} errors"},
                for_ms=30_000,
            )
            critical = AlertingRule(
                "ServiceDown",
                "demo_up == 0",
                parse_expr("demo_up == 0"),
                {"severity": "critical", "service": "api"},
                {"summary": "{{ $labels.service }} is down"},
                for_ms=0,
            )
            notifier = AlertmanagerNotifier(
                [AlertmanagerConfig(targets=[f"127.0.0.1:{port}"])],
                start_worker=False,
            )
            manager = RuleManager(
                tsdb,
                [RuleGroup("demo", "inline", 15_000, [warning, critical])],
                notifier=notifier,
            )
            manager.evaluate(now)
            print(f"t+0s states: {[(a.labels.get('alertname'), a.state) for a in manager.alerts()]}")
            manager.evaluate(now + 30_000)
            print(f"t+30s states: {[(a.labels.get('alertname'), a.state) for a in manager.alerts()]}")
            notifier.flush()
            print(f"alertmanager groups: { {k: len(v) for k, v in receiver.groups().items()} }")
            print(f"inhibited warnings: {[(w['labels']['alertname'], c['labels']['alertname']) for w, c in receiver.inhibited()]}")
            tsdb.close()
    finally:
        receiver.stop()


if __name__ == "__main__":
    run()
