"""Tests for the promtool-style rule checker and unit-test runner."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path

import pytest

from mini_prometheus.promtool import (
    PromtoolError,
    expand_values,
    main,
    parse_series_labels,
    run_test_file,
)


def write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def test_expand_plain_and_missing_values():
    assert expand_values("1 2 _ 4") == [1.0, 2.0, None, 4.0]


def test_expand_arithmetic_and_repetition():
    assert expand_values("1+1x4") == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert expand_values("1-2x3") == [1.0, -1.0, -3.0, -5.0]
    assert expand_values("1*2x3") == [1.0, 2.0, 4.0, 8.0]
    assert expand_values("8/2x3") == [8.0, 4.0, 2.0, 1.0]
    assert expand_values("3x2") == [3.0, 3.0, 3.0]


def test_expand_stale_and_specials():
    values = expand_values("stale NaN +Inf")
    assert math.isnan(values[0])
    assert math.isnan(values[1])
    assert math.isinf(values[2]) and values[2] > 0


def test_expand_rejects_bad_token():
    with pytest.raises(PromtoolError):
        expand_values("1+2x")


def test_parse_series_labels_roundtrip():
    labels = parse_series_labels('up{job="api",instance="a:1"}')
    assert labels.get("__name__") == "up"
    assert labels.get("job") == "api"
    assert labels.get("instance") == "a:1"


def test_parse_series_labels_rejects_regex():
    with pytest.raises(PromtoolError):
        parse_series_labels('up{job=~"api.*"}')


def test_promql_expression_test_passes(tmp_path):
    write(tmp_path / "rules.yml", """
        groups:
          - name: recording
            rules:
              - record: job:up:sum
                expr: sum by (job) (up)
    """)
    test_file = write(tmp_path / "tests.yml", """
        rule_files:
          - rules.yml
        evaluation_interval: 1m
        tests:
          - name: recording rule is queryable
            interval: 1m
            input_series:
              - series: 'up{job="api",instance="a"}'
                values: '1 1 1'
              - series: 'up{job="api",instance="b"}'
                values: '0 1 1'
            promql_expr_test:
              - expr: job:up:sum
                eval_time: 2m
                exp_samples:
                  - labels: 'job:up:sum{job="api"}'
                    value: 2
    """)
    outcomes = run_test_file(test_file)
    assert len(outcomes) == 1
    assert outcomes[0].passed, outcomes[0].failures


def test_alert_rule_test_passes_after_for(tmp_path):
    write(tmp_path / "rules.yml", """
        groups:
          - name: alerts
            rules:
              - alert: InstanceDown
                expr: up == 0
                for: 1m
                labels:
                  severity: warning
                annotations:
                  summary: "{{ $labels.job }} is down"
    """)
    test_file = write(tmp_path / "tests.yml", """
        rule_files:
          - rules.yml
        evaluation_interval: 1m
        tests:
          - name: alert fires after one minute
            interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '1 1 0 0 0'
            alert_rule_test:
              - eval_time: 3m
                alertname: InstanceDown
                exp_alerts:
                  - exp_labels:
                      severity: warning
                      job: api
                    exp_annotations:
                      summary: "api is down"
    """)
    outcomes = run_test_file(test_file)
    assert outcomes[0].passed, outcomes[0].failures


def test_alert_does_not_fire_before_for(tmp_path):
    write(tmp_path / "rules.yml", """
        groups:
          - name: alerts
            rules:
              - alert: InstanceDown
                expr: up == 0
                for: 2m
    """)
    test_file = write(tmp_path / "tests.yml", """
        rule_files:
          - rules.yml
        evaluation_interval: 1m
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '1 1 0 0 0'
            alert_rule_test:
              - eval_time: 2m
                alertname: InstanceDown
                exp_alerts: []
    """)
    outcomes = run_test_file(test_file)
    assert outcomes[0].passed, outcomes[0].failures


def test_recording_rule_is_visible_to_alert_in_same_group(tmp_path):
    write(tmp_path / "rules.yml", """
        groups:
          - name: derived
            rules:
              - record: job:up:sum
                expr: sum by (job) (up)
              - alert: NoUp
                expr: job:up:sum == 0
    """)
    test_file = write(tmp_path / "tests.yml", """
        rule_files:
          - rules.yml
        evaluation_interval: 1m
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '0'
            alert_rule_test:
              - eval_time: 0m
                alertname: NoUp
                exp_alerts:
                  - exp_labels:
                      job: api
                    exp_annotations: {}
    """)
    outcomes = run_test_file(test_file)
    assert outcomes[0].passed, outcomes[0].failures


def test_keep_firing_for_holds_alert_then_resolves(tmp_path):
    write(tmp_path / "rules.yml", """
        groups:
          - name: alerts
            rules:
              - alert: Flapping
                expr: up == 0
                keep_firing_for: 2m
    """)
    test_file = write(tmp_path / "tests.yml", """
        rule_files:
          - rules.yml
        evaluation_interval: 1m
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '0 0 1 1 1'
            alert_rule_test:
              - eval_time: 3m
                alertname: Flapping
                exp_alerts:
                  - exp_labels:
                      job: api
                    exp_annotations: {}
              - eval_time: 4m
                alertname: Flapping
                exp_alerts: []
    """)
    outcomes = run_test_file(test_file)
    assert outcomes[0].passed, outcomes[0].failures


def test_alert_mismatch_is_reported(tmp_path):
    write(tmp_path / "rules.yml", """
        groups:
          - name: alerts
            rules:
              - alert: InstanceDown
                expr: up == 0
    """)
    test_file = write(tmp_path / "tests.yml", """
        rule_files:
          - rules.yml
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '0 0'
            alert_rule_test:
              - eval_time: 0m
                alertname: InstanceDown
                exp_alerts: []
    """)
    outcomes = run_test_file(test_file)
    assert not outcomes[0].passed
    assert any("unexpected firing alert" in failure for failure in outcomes[0].failures)


def test_promql_mismatch_is_reported(tmp_path):
    test_file = write(tmp_path / "tests.yml", """
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '0'
            promql_expr_test:
              - expr: up
                eval_time: 0m
                exp_samples:
                  - labels: 'up{job="api"}'
                    value: 1
    """)
    outcomes = run_test_file(test_file)
    assert not outcomes[0].passed
    assert any("expected 1" in failure for failure in outcomes[0].failures)


def test_missing_sample_is_reported(tmp_path):
    test_file = write(tmp_path / "tests.yml", """
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '_ 1'
            promql_expr_test:
              - expr: up
                eval_time: 0m
                exp_samples:
                  - labels: 'up{job="api"}'
                    value: 1
    """)
    outcomes = run_test_file(test_file)
    assert not outcomes[0].passed
    assert any("missing sample" in failure for failure in outcomes[0].failures)


def test_stale_marker_hides_series(tmp_path):
    test_file = write(tmp_path / "tests.yml", """
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '1 stale'
            promql_expr_test:
              - expr: up
                eval_time: 1m
                exp_samples: []
    """)
    outcomes = run_test_file(test_file)
    assert outcomes[0].passed, outcomes[0].failures


def test_shipped_demo_suite_passes():
    path = Path(__file__).resolve().parent.parent / "examples" / "rules" / "demo_test.yml"
    outcomes = run_test_file(path)
    assert outcomes
    assert all(outcome.passed for outcome in outcomes), [
        failure for outcome in outcomes for failure in outcome.failures
    ]


def test_check_rules_cli(tmp_path, capsys):
    rule_file = write(tmp_path / "rules.yml", """
        groups:
          - name: demo
            rules:
              - record: job:up:sum
                expr: sum by (job) (up)
    """)
    assert main(["check", "rules", str(rule_file)]) == 0
    assert "1 rule group(s), 1 rule(s) validated" in capsys.readouterr().out


def test_test_rules_cli_failure(tmp_path, capsys):
    test_file = write(tmp_path / "tests.yml", """
        tests:
          - interval: 1m
            input_series:
              - series: 'up{job="api"}'
                values: '0'
            promql_expr_test:
              - expr: up
                eval_time: 0m
                exp_samples:
                  - labels: 'up{job="api"}'
                    value: 1
    """)
    assert main(["test", "rules", str(test_file)]) == 1
    output = capsys.readouterr().out
    assert "FAILED" in output


def test_missing_rule_file_is_reported(tmp_path):
    test_file = write(tmp_path / "tests.yml", """
        rule_files:
          - does-not-exist.yml
        tests:
          - interval: 1m
            input_series: []
            promql_expr_test: []
    """)
    with pytest.raises(Exception):
        run_test_file(test_file)


def test_invalid_test_file_rejected(tmp_path):
    test_file = write(tmp_path / "tests.yml", "tests: []\n")
    with pytest.raises(PromtoolError):
        run_test_file(test_file)
