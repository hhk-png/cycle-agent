"""Run a promtool-style rule unit-test suite against Mini-Prometheus.

The example builds a temporary rule file and test file, runs the same
``RuleManager`` and PromQL engine the server uses, then shows what a failure
looks like.  This is the fastest feedback loop for alerting changes: no
scrape loop, no sleeping, no real Alertmanager.
"""

from __future__ import annotations

import sys as _sys
import tempfile
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)

from mini_prometheus.promtool import run_test_file  # noqa: E402

RULES = """
groups:
  - name: api
    rules:
      - record: job:api_requests:rate5m
        expr: sum by (job) (rate(api_requests_total[5m]))

      - alert: ApiErrorRatioHigh
        expr: |
          sum by (job) (rate(api_requests_total{code="500"}[5m]))
          /
          sum by (job) (rate(api_requests_total[5m])) > 0.2
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.job }} 5xx ratio is high"
"""

PASSING_TESTS = """
rule_files:
  - rules.yml
evaluation_interval: 1m
tests:
  - name: 5xx ratio alert fires after two minutes
    interval: 1m
    input_series:
      - series: 'api_requests_total{job="api",code="200"}'
        values: '0+10x8'
      - series: 'api_requests_total{job="api",code="500"}'
        values: '0+5x8'
    promql_expr_test:
      - expr: job:api_requests:rate5m
        eval_time: 5m
        exp_samples:
          - labels: 'job:api_requests:rate5m{job="api"}'
            value: 0.25
    alert_rule_test:
      - eval_time: 7m
        alertname: ApiErrorRatioHigh
        exp_alerts:
          - exp_labels:
              severity: warning
              job: api
            exp_annotations:
              summary: "api 5xx ratio is high"
"""

FAILING_TESTS = """
rule_files:
  - rules.yml
evaluation_interval: 1m
tests:
  - name: intentionally wrong expectation
    interval: 1m
    input_series:
      - series: 'api_requests_total{job="api",code="500"}'
        values: '0+1x4'
      - series: 'api_requests_total{job="api",code="200"}'
        values: '0+99x4'
    alert_rule_test:
      - eval_time: 4m
        alertname: ApiErrorRatioHigh
        exp_alerts:
          - exp_labels:
              severity: critical
              job: api
"""


def _write(directory: _Path, name: str, text: str) -> _Path:
    path = directory / name
    path.write_text(text.lstrip(), encoding="utf-8")
    return path


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="mini-rule-tests-") as tmp:
        root = _Path(tmp)
        _write(root, "rules.yml", RULES)
        passing = _write(root, "passing.yml", PASSING_TESTS)
        failing = _write(root, "failing.yml", FAILING_TESTS)

        print("=== passing suite ===")
        for outcome in run_test_file(passing):
            print(f"{'PASS' if outcome.passed else 'FAIL'}: {outcome.name}")
            for failure in outcome.failures:
                print(f"  - {failure}")
            assert outcome.passed, outcome.failures

        print("\n=== failing suite (expected) ===")
        outcomes = run_test_file(failing)
        for outcome in outcomes:
            print(f"{'PASS' if outcome.passed else 'FAIL'}: {outcome.name}")
            for failure in outcome.failures:
                print(f"  - {failure}")
        assert outcomes and not outcomes[0].passed
        assert any(
            "missing expected alert" in failure for failure in outcomes[0].failures
        )
        print("\nrule unit tests behave as expected")


if __name__ == "__main__":
    run()
