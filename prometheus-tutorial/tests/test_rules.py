from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mini_prometheus.config import AlertmanagerConfig, ConfigError
from mini_prometheus.labels import Labels, Matcher
from mini_prometheus.rules.manager import (
    AlertingRule,
    RecordingRule,
    RuleManager,
    load_rule_groups,
    render_template,
)
from mini_prometheus.rules.notifier import AlertmanagerNotifier


def write_rules(tmp_path, groups):
    path = tmp_path / "rules.yml"
    path.write_text(yaml.safe_dump({"groups": groups}), encoding="utf-8")
    return path


def load(tmp_path, groups, **kwargs):
    path = write_rules(tmp_path, groups)
    return load_rule_groups(
        [str(path)], base_dir=str(tmp_path), **kwargs
    )


def series(tsdb, name, labels=None, points=None):
    labels = Labels({"__name__": name, **(labels or {})})
    for timestamp, value in points or []:
        tsdb.append(labels, timestamp, value)
    return labels


def test_rule_labels_accept_utf8_names(tmp_path):
    groups = load(
        tmp_path,
        [
            {
                "name": "g",
                "rules": [
                    {
                        "record": "job:rate",
                        "expr": "sum by (job) (x)",
                        "labels": {"service.name": "api"},
                    }
                ],
            }
        ],
    )
    assert groups[0].rules[0].labels.get("service.name") == "api"


def test_load_recording_rule(tmp_path):
    groups = load(
        tmp_path,
        [
            {
                "name": "g",
                "rules": [
                    {
                        "record": "job:rate",
                        "expr": "sum by (job) (rate(x[1m]))",
                        "labels": {"team": "sre"},
                    }
                ],
            }
        ],
    )
    assert len(groups) == 1
    rule = groups[0].rules[0]
    assert isinstance(rule, RecordingRule)
    assert rule.record == "job:rate"
    assert rule.labels == {"team": "sre"}


def test_load_alerting_rule(tmp_path):
    groups = load(
        tmp_path,
        [
            {
                "name": "g",
                "rules": [
                    {
                        "alert": "High",
                        "expr": "x > 0",
                        "for": "5m",
                        "keep_firing_for": "1m",
                        "labels": {"severity": "page"},
                        "annotations": {"summary": "{{ $value }}"},
                    }
                ],
            }
        ],
    )
    rule = groups[0].rules[0]
    assert isinstance(rule, AlertingRule)
    assert rule.for_ms == 300_000
    assert rule.keep_firing_for_ms == 60_000
    assert rule.labels == {"severity": "page"}


def test_rule_file_requires_groups(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("rules: []\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_rule_groups([str(path)], base_dir=str(tmp_path))


def test_rule_needs_record_or_alert(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path, [{"name": "g", "rules": [{"expr": "up"}]}])


def test_rule_cannot_have_both(tmp_path):
    with pytest.raises(ConfigError):
        load(
            tmp_path,
            [{"name": "g", "rules": [{"record": "a", "alert": "b", "expr": "up"}]}],
        )


def test_alert_requires_vector_expr(tmp_path):
    with pytest.raises(ConfigError):
        load(
            tmp_path,
            [{"name": "g", "rules": [{"alert": "a", "expr": "1 + 1"}]}],
        )


def test_duplicate_group_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load(
            tmp_path,
            [
                {"name": "g", "rules": [{"record": "a", "expr": "up"}]},
                {"name": "g", "rules": [{"record": "b", "expr": "up"}]},
            ],
        )


def test_invalid_expr_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path, [{"name": "g", "rules": [{"record": "a", "expr": "not(("}]}])


def test_empty_group_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path, [{"name": "g", "rules": []}])


def test_recording_rule_writes_series(tsdb):
    series(tsdb, "x", {"job": "a"}, [(0, 1), (60_000, 2)])
    from mini_prometheus.rules.manager import RecordingRule
    from mini_prometheus.promql.parser import parse_expr

    rule = RecordingRule(
        "x:sum", "sum by (job) (x)", parse_expr("sum by (job) (x)"), {"team": "s"}
    )
    from mini_prometheus.rules.manager import RuleGroup

    group = RuleGroup("g", "test", 60_000, [rule])
    manager = RuleManager(tsdb, [group])
    manager.evaluate(now=60_000)
    result = tsdb.select([Matcher("__name__", "=", "x:sum")], 0, 10**9)
    assert len(result) == 1
    labels, samples = result[0]
    assert labels.get("job") == "a"
    assert labels.get("team") == "s"
    assert samples[-1].value == 2
    assert rule.health == "ok"


def test_recording_rule_query_offset_shifts_query_but_not_write_time(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import RecordingRule, RuleGroup

    series(tsdb, "x", {"job": "a"}, [(0, 1), (60_000, 2)])
    rule = RecordingRule("x:copy", "x", parse_expr("x"), {})
    group = RuleGroup("g", "test", 60_000, [rule], query_offset_ms=60_000)
    RuleManager(tsdb, [group]).evaluate(now=60_000)
    labels, samples = tsdb.select([Matcher("__name__", "=", "x:copy")], 0, 10**9)[0]
    assert labels.get("job") == "a"
    # The query sees t=0 (value 1), but the recorded sample is written at the
    # evaluation timestamp (60s), matching Prometheus' query_offset semantics.
    assert samples[-1].value == 1
    assert samples[-1].timestamp == 60_000


def test_alerting_rule_query_offset_uses_older_window(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import AlertingRule, RuleGroup

    series(tsdb, "x", {}, [(0, 1), (60_000, 2)])
    rule = AlertingRule("High", "x > 1", parse_expr("x > 1"))
    group = RuleGroup("g", "test", 60_000, [rule], query_offset_ms=60_000)
    RuleManager(tsdb, [group]).evaluate(now=60_000)
    assert rule.active == {}

    RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])]).evaluate(now=60_000)
    assert len(rule.active) == 1


def test_rule_group_query_offset_parsing_and_api(tmp_path):
    groups = load(
        tmp_path,
        [
            {
                "name": "g",
                "query_offset": "30s",
                "rules": [{"record": "x:copy", "expr": "x"}],
            }
        ],
    )
    assert groups[0].query_offset_ms == 30_000
    assert groups[0].to_api()["queryOffset"] == 30.0

    overridden = load(
        tmp_path,
        [
            {
                "name": "g",
                "query_offset": "30s",
                "rules": [{"record": "x:copy", "expr": "x"}],
            }
        ],
        default_query_offset_ms=15_000,
    )
    assert overridden[0].query_offset_ms == 30_000

    inherited = load(
        tmp_path,
        [{"name": "h", "rules": [{"record": "y:copy", "expr": "y"}]}],
        default_query_offset_ms=15_000,
    )
    assert inherited[0].query_offset_ms == 15_000


def test_rule_group_query_offset_invalid_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load(
            tmp_path,
            [
                {
                    "name": "g",
                    "query_offset": "-1m",
                    "rules": [{"record": "x:copy", "expr": "x"}],
                }
            ],
        )


def test_recording_rule_label_override(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import RecordingRule, RuleGroup

    series(tsdb, "x", {"job": "a", "team": "orig"}, [(0, 5)])
    rule = RecordingRule(
        "x:copy", "x", parse_expr("x"), {"team": "override", "env": "prod"}
    )
    RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])]).evaluate(0)
    labels = tsdb.select([Matcher("__name__", "=", "x:copy")], 0, 1000)[0][0]
    assert labels.get("team") == "override"
    assert labels.get("env") == "prod"


def test_recording_rule_scalar(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import RecordingRule, RuleGroup

    rule = RecordingRule("one", "1", parse_expr("1"), {})
    RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])]).evaluate(0)
    assert tsdb.select([Matcher("__name__", "=", "one")], 0, 1000)[0][1][0].value == 1


def test_recording_rule_limit(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import RecordingRule, RuleGroup

    for index in range(3):
        series(tsdb, "x", {"i": str(index)}, [(0, index)])
    rule = RecordingRule("x:copy", "x", parse_expr("x"), {})
    group = RuleGroup("g", "test", 60_000, [rule], limit=2)
    RuleManager(tsdb, [group]).evaluate(0)
    assert rule.health == "err"
    assert "limit" in rule.last_error
    assert tsdb.select([Matcher("__name__", "=", "x:copy")], 0, 1000) == []


def test_alert_pending_then_firing(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import AlertingRule, RuleGroup

    series(tsdb, "x", {"job": "a"}, [(0, 10)])
    rule = AlertingRule(
        "High", "x > 0", parse_expr("x > 0"), {"severity": "page"},
        {"summary": "{{ $labels.job }}={{ $value }}"}, for_ms=30_000,
    )
    manager = RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])])
    manager.evaluate(0)
    alerts = manager.alerts()
    assert len(alerts) == 1
    assert alerts[0].state == "pending"
    assert alerts[0].annotations["summary"] == "a=10"
    manager.evaluate(30_000)
    assert manager.alerts()[0].state == "firing"


def test_alert_resolves(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import AlertingRule, RuleGroup

    labels = series(tsdb, "x", {"job": "a"}, [(0, 10)])
    rule = AlertingRule("High", "x > 0", parse_expr("x > 0"), {}, {}, for_ms=0)
    manager = RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])])
    manager.evaluate(0)
    assert manager.alerts()[0].state == "firing"
    tsdb.append(labels, 10_000, 0)
    manager.evaluate(10_000)
    assert manager.alerts() == []


def test_alert_keep_firing_for(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import AlertingRule, RuleGroup

    labels = series(tsdb, "x", {"job": "a"}, [(0, 10)])
    rule = AlertingRule(
        "High", "x > 0", parse_expr("x > 0"), {}, {}, for_ms=0, keep_firing_for_ms=60_000
    )
    manager = RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])])
    manager.evaluate(0)
    tsdb.append(labels, 10_000, 0)
    manager.evaluate(10_000)
    assert len(manager.alerts()) == 1
    manager.evaluate(70_000)
    assert manager.alerts() == []


def test_alert_identity_per_series(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import AlertingRule, RuleGroup

    series(tsdb, "x", {"job": "a"}, [(0, 1)])
    series(tsdb, "x", {"job": "b"}, [(0, 2)])
    rule = AlertingRule("High", "x > 0", parse_expr("x > 0"), {}, {}, for_ms=0)
    manager = RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])])
    manager.evaluate(0)
    assert len(manager.alerts()) == 2
    assert {alert.labels.get("job") for alert in manager.alerts()} == {"a", "b"}


def test_alert_resolution_payload(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import AlertingRule, RuleGroup

    labels = series(tsdb, "x", {"job": "a"}, [(0, 10)])
    rule = AlertingRule("High", "x > 0", parse_expr("x > 0"), {}, {}, for_ms=0)
    notifier = _RecordingNotifier()
    manager = RuleManager(
        tsdb, [RuleGroup("g", "test", 60_000, [rule])], notifier=notifier
    )
    manager.evaluate(0)
    assert notifier.batches
    firing = notifier.batches[-1][0]
    assert firing["labels"]["alertname"] == "High"
    assert "startsAt" in firing and "endsAt" in firing
    tsdb.append(labels, 10_000, 0)
    manager.evaluate(10_000)
    resolved = notifier.batches[-1][0]
    assert resolved["endsAt"] == "1970-01-01T00:00:10.000Z"


class _RecordingNotifier:
    def __init__(self):
        self.batches = []

    def enqueue(self, alerts):
        self.batches.append(alerts)


def test_render_template():
    labels = Labels({"job": "api", "instance": "h1"})
    assert render_template("{{ $labels.job }}", labels, 1.5, {}) == "api"
    assert render_template("{{ $labels.instance }}", labels, 1.5, {}) == "h1"
    assert render_template("{{ $value }}", labels, 1.5, {}) == "1.5"
    assert render_template('{{ printf "%.2f" $value }}', labels, 1.5, {}) == "1.50"
    assert render_template("{{ $externalLabels.cluster }}", labels, 1.5, {"cluster": "c1"}) == "c1"
    assert render_template("plain", labels, 1.5, {}) == "plain"


def test_rules_api(tsdb):
    from mini_prometheus.promql.parser import parse_expr
    from mini_prometheus.rules.manager import AlertingRule, RecordingRule, RuleGroup

    record = RecordingRule("x:sum", "sum(x)", parse_expr("sum(x)"), {})
    alert = AlertingRule("High", "x > 0", parse_expr("x > 0"), {}, {"summary": "s"}, for_ms=0)
    manager = RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [record, alert])])
    data = manager.to_api()
    assert data["groups"][0]["name"] == "g"
    assert data["groups"][0]["rules"][0]["type"] == "recording"
    assert data["groups"][0]["rules"][1]["type"] == "alerting"


def test_rule_evaluation_error_keeps_state(tsdb):
    from mini_prometheus.rules.manager import RuleGroup, RecordingRule
    from mini_prometheus.promql.parser import parse_expr

    rule = RecordingRule("x:sum", "nonexistent{job=~\".+\"}", parse_expr('nonexistent{job=~".+"}'), {})
    manager = RuleManager(tsdb, [RuleGroup("g", "test", 60_000, [rule])])
    manager.evaluate(0)
    assert rule.health == "ok"
    assert rule.last_value == 0


def test_notifier_delivery():
    sent = []

    def sender(url, headers, body, timeout):
        sent.append((url, json.loads(body)))
        return 200

    config = AlertmanagerConfig(targets=["am:9093"])
    notifier = AlertmanagerNotifier([config], sender=sender, start_worker=False)
    notifier.enqueue([{"labels": {"alertname": "A"}, "annotations": {}}])
    notifier.flush()
    assert sent
    assert sent[0][0] == "http://am:9093/api/v2/alerts"
    assert sent[0][1][0]["labels"]["alertname"] == "A"


def test_notifier_drops_4xx_without_retry():
    calls = []

    def sender(url, headers, body, timeout):
        calls.append(1)
        return 400

    config = AlertmanagerConfig(targets=["am:9093"])
    notifier = AlertmanagerNotifier([config], sender=sender, start_worker=False)
    notifier.enqueue([{"labels": {"alertname": "A"}}])
    notifier.flush()
    assert len(calls) == 1
    assert notifier.stats["dropped"] == 1


def test_rule_files_glob(tmp_path):
    (tmp_path / "a.yml").write_text(
        yaml.safe_dump({"groups": [{"name": "a", "rules": [{"record": "r", "expr": "up"}]}]})
    )
    (tmp_path / "b.yml").write_text(
        yaml.safe_dump({"groups": [{"name": "b", "rules": [{"record": "r2", "expr": "up"}]}]})
    )
    groups = load_rule_groups([str(tmp_path / "*.yml")], base_dir=str(tmp_path))
    assert {group.name for group in groups} == {"a", "b"}
