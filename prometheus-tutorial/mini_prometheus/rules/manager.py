"""Rule evaluation, recording rules and the alert state machine."""

from __future__ import annotations

import glob as globlib
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError
from ..labels import Labels, validate_label_name
from ..model import PrometheusError, format_value
from ..promql.engine import Engine
from ..promql.parser import parse_expr
from ..promql.value import Scalar, Vector
from ..tsdb.tsdb import TSDB
from ..util.timeutil import format_timestamp, parse_duration
from .notifier import AlertmanagerNotifier, alert_payload


@dataclass
class RecordingRule:
    record: str
    expr_text: str
    expr: Any
    labels: dict[str, str] = field(default_factory=dict)
    health: str = "unknown"
    last_error: str = ""
    last_evaluation: int = 0
    evaluation_time: float = 0.0
    last_value: int = 0
    evaluation_count: int = 0
    failures: int = 0

    @property
    def type(self) -> str:
        return "recording"


@dataclass
class Alert:
    labels: Labels
    annotations: dict[str, str]
    state: str  # pending | firing | inactive
    active_at: int
    last_seen: int
    value: float
    fired_at: int = 0
    resolved_at: int = 0
    keep_firing_since: int = 0

    @property
    def fingerprint(self) -> str:
        material = "\xff".join(f"{n}={v}" for n, v in self.labels.items())
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def to_api(self) -> dict:
        return {
            "labels": self.labels.to_dict(),
            "annotations": self.annotations,
            "state": self.state,
            "activeAt": format_timestamp(self.active_at) if self.active_at else "",
            "value": format_value(self.value),
            "keepFiringSince": format_timestamp(self.keep_firing_since)
            if self.keep_firing_since
            else "",
        }


@dataclass
class AlertingRule:
    alert: str
    expr_text: str
    expr: Any
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    for_ms: int = 0
    keep_firing_for_ms: int = 0
    health: str = "unknown"
    last_error: str = ""
    last_evaluation: int = 0
    evaluation_time: float = 0.0
    active: dict[str, Alert] = field(default_factory=dict)
    evaluation_count: int = 0
    failures: int = 0
    last_value: int = 0

    @property
    def type(self) -> str:
        return "alerting"


@dataclass
class RuleGroup:
    name: str
    file: str
    interval_ms: int
    rules: list[Any]
    limit: int = 0
    query_offset_ms: int = 0
    last_evaluation: int = 0
    evaluation_time: float = 0.0
    health: str = "unknown"
    last_error: str = ""

    def to_api(self) -> dict:
        return {
            "name": self.name,
            "file": self.file,
            "interval": self.interval_ms / 1000.0,
            "limit": self.limit,
            "queryOffset": self.query_offset_ms / 1000.0,
            "lastEvaluation": format_timestamp(self.last_evaluation)
            if self.last_evaluation
            else "",
            "evaluationTime": self.evaluation_time,
            "health": self.health,
            "lastError": self.last_error,
            "rules": [_rule_api(rule) for rule in self.rules],
        }


class RuleManager:
    def __init__(
        self,
        tsdb: TSDB,
        groups: list[RuleGroup],
        *,
        evaluation_interval_ms: int = 60_000,
        external_labels: dict[str, str] | None = None,
        notifier: AlertmanagerNotifier | None = None,
        generator_url: str = "",
        lookback_ms: int = 5 * 60 * 1000,
        clock=time.time,
    ) -> None:
        self.tsdb = tsdb
        self.groups = groups
        self.evaluation_interval_ms = evaluation_interval_ms
        self.external_labels = external_labels or {}
        self.notifier = notifier
        self.generator_url = generator_url
        self.engine = Engine(
            tsdb,
            lookback_ms=lookback_ms,
            default_step_ms=evaluation_interval_ms,
        )
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_run: dict[int, int] = {}
        self.stats = {"evaluations": 0, "failures": 0, "alerts_fired": 0}

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        start = int(self.clock() * 1000)
        for index, group in enumerate(self.groups):
            self._next_run[id(group)] = start
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rule-manager", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = int(self.clock() * 1000)
            for group in self.groups:
                due = self._next_run.get(id(group), 0)
                if now >= due:
                    self.evaluate_group(group, now)
                    interval = group.interval_ms or self.evaluation_interval_ms
                    # Catch up without spinning if a rule took too long.
                    self._next_run[id(group)] = max(now + 1, due + interval)
            self._stop.wait(0.1)

    def reload(self, groups: list[RuleGroup]) -> None:
        self.groups = groups
        self._next_run = {id(group): int(self.clock() * 1000) for group in groups}

    # ---- evaluation ----------------------------------------------------
    def evaluate(self, now: int | None = None) -> None:
        now = int(self.clock() * 1000) if now is None else now
        for group in self.groups:
            self.evaluate_group(group, now)

    def evaluate_group(self, group: RuleGroup, now: int) -> None:
        started = self.clock()
        firing: list[dict] = []
        resolved: list[dict] = []
        group_error = ""
        for rule in group.rules:
            try:
                if isinstance(rule, RecordingRule):
                    self._evaluate_recording(rule, group, now)
                else:
                    fired, resolved_now = self._evaluate_alerting(rule, group, now)
                    firing.extend(fired)
                    resolved.extend(resolved_now)
            except Exception as exc:
                rule.health = "err"
                rule.last_error = str(exc)
                rule.last_evaluation = now
                rule.failures += 1
                self.stats["failures"] += 1
                group_error = str(exc)
        duration = self.clock() - started
        group.last_evaluation = now
        group.evaluation_time = duration
        group.health = "err" if group_error else "ok"
        group.last_error = group_error
        self.stats["evaluations"] += 1
        if self.notifier:
            if firing:
                self.notifier.enqueue(firing)
            if resolved:
                self.notifier.enqueue(resolved)

    def _evaluate_recording(self, rule: RecordingRule, group: RuleGroup, now: int) -> None:
        started = self.clock()
        # query_offset shifts only the *query* time; results are still written
        # at the group's evaluation timestamp, matching upstream Prometheus.
        query_at = now - group.query_offset_ms
        result = self.engine.query(rule.expr, query_at)
        if isinstance(result.value, Vector):
            items = result.value.items
        elif isinstance(result.value, Scalar):
            items = []
        else:
            raise PrometheusError(
                f"recording rule {rule.record!r} must produce an instant vector"
            )
        if group.limit and len(items) > group.limit:
            raise PrometheusError(
                f"recording rule {rule.record!r} produced {len(items)} samples, "
                f"exceeding group limit {group.limit}"
            )
        samples = []
        for item in items:
            labels = item.labels.without("__name__").merge(Labels(rule.labels))
            labels = labels.set("__name__", rule.record)
            samples.append((labels, now, item.value))
        if isinstance(result.value, Scalar):
            labels = Labels(rule.labels).set("__name__", rule.record)
            samples.append((labels, now, result.value.value))
        errors = []
        if samples:
            _appended, errors = self.tsdb.append_batch(samples)
        rule.last_evaluation = now
        rule.evaluation_time = self.clock() - started
        rule.last_value = len(samples)
        rule.evaluation_count += 1
        rule.health = "err" if errors else "ok"
        rule.last_error = "; ".join(errors[:3])
        if errors:
            rule.failures += 1

    def _evaluate_alerting(
        self, rule: AlertingRule, group: RuleGroup, now: int
    ) -> tuple[list[dict], list[dict]]:
        started = self.clock()
        # See _evaluate_recording: the offset affects the data window, while
        # pending/firing timers advance on the real evaluation clock.
        query_at = now - group.query_offset_ms
        result = self.engine.query(rule.expr, query_at)
        if not isinstance(result.value, Vector):
            raise PrometheusError(
                f"alerting rule {rule.alert!r} must produce an instant vector"
            )
        items = result.value.items
        if group.limit and len(items) > group.limit:
            raise PrometheusError(
                f"alerting rule {rule.alert!r} produced {len(items)} samples, "
                f"exceeding group limit {group.limit}"
            )
        seen: dict[str, Alert] = {}
        firing_payloads: list[dict] = []
        for item in items:
            labels = item.labels.without("__name__").merge(Labels(rule.labels))
            labels = labels.set("alertname", rule.alert)
            fingerprint = _fingerprint_for(labels)
            existing = rule.active.get(fingerprint)
            annotations = {
                key: render_template(text, labels, item.value, self.external_labels)
                for key, text in rule.annotations.items()
            }
            if existing is None:
                active_at = now
                fired_at = now if rule.for_ms == 0 else 0
                alert = Alert(
                    labels,
                    annotations,
                    "firing" if rule.for_ms == 0 else "pending",
                    active_at,
                    now,
                    item.value,
                    fired_at,
                )
                rule.active[fingerprint] = alert
            else:
                alert = existing
                alert.annotations = annotations
                alert.value = item.value
                alert.last_seen = now
                if alert.state == "pending" and now - alert.active_at >= rule.for_ms:
                    alert.state = "firing"
                    alert.fired_at = now
                    self.stats["alerts_fired"] += 1
                if alert.state == "firing":
                    alert.keep_firing_since = 0
            seen[fingerprint] = alert
            if alert.state == "firing":
                firing_payloads.append(self._payload(alert, now, resolved=False))
        resolved_payloads: list[dict] = []
        for fingerprint, alert in list(rule.active.items()):
            if fingerprint in seen:
                continue
            if alert.state == "firing":
                if rule.keep_firing_for_ms:
                    # Start the keep-firing clock at the first evaluation where
                    # the condition disappeared, not at the previous seen
                    # evaluation.  Otherwise a keep_firing_for equal to the
                    # evaluation interval expires one interval early.
                    if not alert.keep_firing_since:
                        alert.keep_firing_since = now
                    if now - alert.keep_firing_since < rule.keep_firing_for_ms:
                        alert.state = "firing"
                        firing_payloads.append(self._payload(alert, now, resolved=False))
                        continue
                alert.state = "inactive"
                alert.resolved_at = now
                resolved_payloads.append(self._payload(alert, now, resolved=True))
            rule.active.pop(fingerprint, None)
        rule.last_evaluation = now
        rule.evaluation_time = self.clock() - started
        rule.last_value = len(items)
        rule.evaluation_count += 1
        rule.health = "ok"
        rule.last_error = ""
        return firing_payloads, resolved_payloads

    def _payload(self, alert: Alert, now: int, *, resolved: bool) -> dict:
        ends_at = alert.resolved_at if resolved else now + 5 * 60 * 1000
        payload = alert_payload(
            alert.labels.to_dict(),
            alert.annotations,
            starts_at=alert.active_at,
            ends_at=ends_at,
            generator_url=self.generator_url,
            value=alert.value,
        )
        payload["fingerprint"] = alert.fingerprint
        return payload

    # ---- introspection -------------------------------------------------
    def alerts(self) -> list[Alert]:
        out = []
        for group in self.groups:
            for rule in group.rules:
                if isinstance(rule, AlertingRule):
                    out.extend(rule.active.values())
        return sorted(out, key=lambda alert: alert.labels)

    def to_api(self) -> dict:
        return {"groups": [group.to_api() for group in self.groups]}


def _rule_api(rule) -> dict:
    if isinstance(rule, RecordingRule):
        return {
            "name": rule.record,
            "query": rule.expr_text,
            "labels": rule.labels,
            "health": rule.health,
            "lastError": rule.last_error,
            "lastEvaluation": format_timestamp(rule.last_evaluation)
            if rule.last_evaluation
            else "",
            "evaluationTime": rule.evaluation_time,
            "type": "recording",
            "lastValue": rule.last_value,
        }
    return {
        "name": rule.alert,
        "query": rule.expr_text,
        "labels": rule.labels,
        "annotations": rule.annotations,
        "for": rule.for_ms / 1000.0,
        "keepFiringFor": rule.keep_firing_for_ms / 1000.0,
        "health": rule.health,
        "lastError": rule.last_error,
        "lastEvaluation": format_timestamp(rule.last_evaluation)
        if rule.last_evaluation
        else "",
        "evaluationTime": rule.evaluation_time,
        "type": "alerting",
        "alerts": [alert.to_api() for alert in rule.active.values()],
        "lastValue": rule.last_value,
    }


def _fingerprint_for(labels: Labels) -> str:
    material = "\xff".join(f"{n}={v}" for n, v in labels.items())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


Alert._fingerprint_for = staticmethod(_fingerprint_for)  # type: ignore[attr-defined]


_TEMPLATE_EXPR = re.compile(r"\{\{\s*(.*?)\s*\}\}")


def render_template(
    text: str, labels: Labels, value: float, external_labels: dict[str, str]
) -> str:
    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if expression.startswith("$labels."):
            return labels.get(expression[len("$labels.") :])
        if expression.startswith('$labels["') and expression.endswith('"]'):
            return labels.get(expression[len('$labels["') : -2])
        if expression.startswith("$externalLabels."):
            return external_labels.get(expression[len("$externalLabels.") :], "")
        if expression == "$value":
            return format_value(value)
        formatted = re.match(r'printf\s+"(.*?)"\s+\$value$', expression)
        if formatted:
            try:
                return formatted.group(1) % value
            except (TypeError, ValueError):
                return format_value(value)
        if expression.startswith("$labels"):
            return ""
        return match.group(0)

    return _TEMPLATE_EXPR.sub(replace, text)


# ---------------------------------------------------------------------------
# loading


def load_rule_groups(
    patterns: list[str],
    *,
    base_dir: str = ".",
    evaluation_interval_ms: int = 60_000,
    default_query_offset_ms: int = 0,
) -> list[RuleGroup]:
    groups: list[RuleGroup] = []
    seen: set[tuple[str, str]] = set()
    for pattern in patterns:
        path_pattern = pattern
        if not os.path.isabs(path_pattern):
            path_pattern = os.path.join(base_dir, pattern)
        matches = sorted(globlib.glob(path_pattern))
        if not matches:
            raise ConfigError(f"rule file pattern {pattern!r} matched no files")
        for filename in matches:
            groups.extend(
                _load_file(
                    filename,
                    evaluation_interval_ms,
                    seen,
                    default_query_offset_ms,
                )
            )
    return groups


def _load_file(
    filename: str,
    evaluation_interval_ms: int,
    seen: set[tuple[str, str]],
    default_query_offset_ms: int = 0,
) -> list[RuleGroup]:
    try:
        data = yaml.safe_load(Path(filename).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot load rule file {filename}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ConfigError(f"rule file {filename} must contain a 'groups' list")
    out = []
    for group_data in data["groups"]:
        if not isinstance(group_data, dict) or not group_data.get("name"):
            raise ConfigError(f"rule group in {filename} requires a name")
        name = str(group_data["name"])
        if (filename, name) in seen:
            raise ConfigError(f"duplicate rule group {name!r} in {filename}")
        seen.add((filename, name))
        interval = str(group_data.get("interval", "")) or None
        interval_ms = (
            parse_duration(interval) if interval else evaluation_interval_ms
        )
        if interval_ms <= 0:
            raise ConfigError(f"group {name!r}: interval must be positive")
        limit = int(group_data.get("limit", 0) or 0)
        if "query_offset" in group_data:
            raw_offset = str(group_data.get("query_offset", ""))
            try:
                query_offset_ms = parse_duration(raw_offset)
            except ValueError as exc:
                raise ConfigError(
                    f"group {name!r}: invalid query_offset {raw_offset!r}: {exc}"
                ) from exc
        else:
            query_offset_ms = default_query_offset_ms
        if query_offset_ms < 0:
            raise ConfigError(f"group {name!r}: query_offset must not be negative")
        rules = []
        for rule_data in group_data.get("rules", []) or []:
            rules.append(_parse_rule(rule_data, name))
        if not rules:
            raise ConfigError(f"rule group {name!r} contains no rules")
        out.append(
            RuleGroup(
                name,
                filename,
                interval_ms,
                rules,
                limit,
                query_offset_ms,
            )
        )
    return out


def _parse_rule(data: dict, group_name: str):
    if not isinstance(data, dict):
        raise ConfigError(f"group {group_name!r}: rules must be mappings")
    has_record = "record" in data
    has_alert = "alert" in data
    if has_record == has_alert:
        raise ConfigError(
            f"group {group_name!r}: each rule needs exactly one of record/alert"
        )
    expression_text = data.get("expr")
    if not expression_text or not isinstance(expression_text, str):
        raise ConfigError(f"group {group_name!r}: rule requires a string expr")
    try:
        expression = parse_expr(expression_text)
    except Exception as exc:
        raise ConfigError(f"group {group_name!r}: invalid expr: {exc}") from exc
    labels = {str(k): str(v) for k, v in (data.get("labels") or {}).items()}
    for label_name in labels:
        validate_label_name(label_name, legacy_only=False)
        if label_name == "__name__":
            raise ConfigError("rule labels must not set __name__")
    if has_record:
        record = str(data["record"])
        if not record:
            raise ConfigError("recording rule requires a non-empty record name")
        if expression.value_type not in ("vector", "scalar"):
            raise ConfigError(
                f"recording rule {record!r} must be an instant vector or scalar"
            )
        return RecordingRule(record, expression_text, expression, labels)
    alert = str(data["alert"])
    if not alert:
        raise ConfigError("alerting rule requires a non-empty alert name")
    if expression.value_type != "vector":
        raise ConfigError(f"alerting rule {alert!r} must be an instant vector")
    annotations = {
        str(k): str(v) for k, v in (data.get("annotations") or {}).items()
    }
    for_value = str(data.get("for", "0s") or "0s")
    keep_value = str(data.get("keep_firing_for", "0s") or "0s")
    try:
        for_ms = parse_duration(for_value)
        keep_ms = parse_duration(keep_value)
    except ValueError as exc:
        raise ConfigError(f"alerting rule {alert!r}: {exc}") from exc
    if for_ms < 0 or keep_ms < 0:
        raise ConfigError(f"alerting rule {alert!r}: durations must not be negative")
    return AlertingRule(
        alert, expression_text, expression, labels, annotations, for_ms, keep_ms
    )
