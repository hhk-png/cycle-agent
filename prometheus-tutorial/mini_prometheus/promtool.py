"""A small ``promtool``-compatible rule checker and unit-test runner.

Real Prometheus ships ``promtool``, which validates rule files and evaluates
``promtool test rules`` YAML suites without starting a server.  This module
implements the parts that matter for the tutorial:

* ``check rules``: parse rule files with the same loader the server uses;
* ``test rules``: build a throw-away TSDB from ``input_series``, evaluate
  recording/alerting rules on the test interval, and compare PromQL results
  and firing alerts with the expected values.

The implementation deliberately shares :class:`RuleManager`, the PromQL
engine and the real TSDB.  A test therefore exercises the same code path as
the server instead of a parallel "test-only" evaluator.

Supported YAML subset::

    rule_files:
      - rules.yml
    evaluation_interval: 1m
    tests:
      - name: alert fires after one minute
        interval: 1m
        input_series:
          - series: 'up{job="api"}'
            values: '1 1 0 0 0'
        promql_expr_test:
          - expr: up
            eval_time: 1m
            exp_samples:
              - labels: 'up{job="api"}'
                value: 0
        alert_rule_test:
          - eval_time: 2m
            alertname: InstanceDown
            exp_alerts:
              - exp_labels:
                  severity: warning
                  job: api
                exp_annotations:
                  summary: "api is down"

``values`` supports the promtool expansion syntax: numbers, ``_`` (missing),
``stale``, ``a+bxN``, ``a-bxN``, ``a*bxN``, ``a/bxN`` and ``axN``.  A
``xN`` expansion yields ``N+1`` values (the initial value plus ``N`` steps),
matching promtool's documented ``1+1x4`` -> ``1 2 3 4 5`` behaviour.

This is a teaching implementation, not a byte-for-byte clone of promtool.
Differences are listed in chapter 28 and in ``VERIFICATION.md``.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .labels import Labels
from .model import format_value
from .promql import ast
from .promql.engine import Engine
from .promql.parser import parse_expr
from .promql.value import Scalar, Vector
from .rules.manager import AlertingRule, RuleManager, load_rule_groups
from .tsdb.tsdb import TSDB, TSDBOptions
from .util.timeutil import STALE_NAN, parse_duration

_NUMBER = r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?"
_VALUE_TOKEN = re.compile(rf"^({_NUMBER})(?:([+\-*/])({_NUMBER})x(\d+)|x(\d+))?$")
_SPECIAL_VALUES = {
    "nan": math.nan,
    "NaN": math.nan,
    "inf": math.inf,
    "Inf": math.inf,
    "+Inf": math.inf,
    "-Inf": -math.inf,
    "+inf": math.inf,
    "-inf": -math.inf,
}


class PromtoolError(ValueError):
    """Raised for malformed test files or rule files."""


@dataclass
class TestOutcome:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# parsing helpers


def _parse_number(text: str) -> float:
    text = str(text).strip()
    if text in _SPECIAL_VALUES:
        return float(_SPECIAL_VALUES[text])
    return float(text)


def expand_values(text: str) -> list[float | None]:
    """Expand promtool's compact series notation.

    ``_`` means "no sample at this step" and is returned as ``None``.
    ``stale`` writes a stale NaN.  Every other token becomes a float.
    """
    values: list[float | None] = []
    tokens = str(text).split()
    if not tokens:
        raise PromtoolError("values must not be empty")
    for token in tokens:
        if token == "_":
            values.append(None)
            continue
        if token == "stale":
            values.append(STALE_NAN)
            continue
        if token in _SPECIAL_VALUES:
            values.append(float(_SPECIAL_VALUES[token]))
            continue
        match = _VALUE_TOKEN.match(token)
        if not match:
            raise PromtoolError(f"invalid value token {token!r} in values {text!r}")
        base = _parse_number(match.group(1))
        operator = match.group(2)
        step_text = match.group(3)
        count_text = match.group(4) or match.group(5)
        if count_text is None:
            values.append(base)
            continue
        count = int(count_text)
        if count < 0 or count > 100_000:
            raise PromtoolError(f"repetition count out of range in {token!r}")
        if step_text is not None:
            step = _parse_number(step_text)
            for index in range(count + 1):
                values.append(_apply_step(base, operator, step, index))
        else:
            values.extend([base] * (count + 1))
    return values


def _apply_step(base: float, operator: str | None, step: float, index: int) -> float:
    if operator == "+":
        return base + step * index
    if operator == "-":
        return base - step * index
    if operator == "*":
        return base * (step**index)
    if operator == "/":
        try:
            return base / (step**index)
        except ZeroDivisionError:
            if base == 0:
                return math.nan
            return math.inf if base > 0 else -math.inf
    return base


def parse_series_labels(text: str) -> Labels:
    """Parse ``metric{label="value"}`` into an immutable label set."""
    text = text.strip()
    if not text:
        raise PromtoolError("series must not be empty")
    try:
        expr = parse_expr(text)
    except Exception as exc:  # ParseError is intentionally broad here
        raise PromtoolError(f"invalid series selector {text!r}: {exc}") from exc
    if isinstance(expr, ast.MatrixSelector):
        expr = expr.vector
    if not isinstance(expr, ast.VectorSelector):
        raise PromtoolError(f"series selector must be a vector selector: {text!r}")
    items: list[tuple[str, str]] = []
    for matcher in expr.matchers:
        if matcher.op != "=":
            raise PromtoolError(
                f"series selector may only use '=' matchers: {text!r}"
            )
        items.append((matcher.name, matcher.value))
    if not items:
        raise PromtoolError(f"series selector has no metric name or labels: {text!r}")
    return Labels(items)


def _labels_from_expected(text: str | None) -> Labels:
    if not text:
        return Labels.empty()
    text = str(text).strip()
    if text in ("{}", ""):
        return Labels.empty()
    return parse_series_labels(text)


def _labels_to_dict(labels: Labels, *, drop_name: bool = False) -> dict[str, str]:
    data = labels.to_dict()
    if drop_name:
        data.pop("__name__", None)
    return data


def _float_equal(left: float, right: float) -> bool:
    if math.isnan(left) and math.isnan(right):
        return True
    if math.isinf(left) or math.isinf(right):
        return left == right
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# test-file model


@dataclass
class _Case:
    name: str
    interval_ms: int
    input_series: list[tuple[str, str]]
    promql_tests: list[dict]
    alert_tests: list[dict]
    external_labels: dict[str, str]


def _load_test_file(path: Path) -> tuple[list[str], int, list[_Case]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PromtoolError(f"cannot load test file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PromtoolError(f"test file {path} must be a mapping")
    rule_files = [str(item) for item in (data.get("rule_files") or [])]
    default_interval_text = str(data.get("evaluation_interval", "1m") or "1m")
    try:
        default_interval = parse_duration(default_interval_text)
    except ValueError as exc:
        raise PromtoolError(f"invalid evaluation_interval: {exc}") from exc
    if default_interval <= 0:
        raise PromtoolError("evaluation_interval must be positive")
    raw_tests = data.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise PromtoolError(f"test file {path} must contain a non-empty 'tests' list")
    cases: list[_Case] = []
    for index, raw in enumerate(raw_tests):
        if not isinstance(raw, dict):
            raise PromtoolError(f"tests[{index}] must be a mapping")
        interval_text = str(raw.get("interval", default_interval_text) or default_interval_text)
        try:
            interval = parse_duration(interval_text)
        except ValueError as exc:
            raise PromtoolError(f"tests[{index}]: invalid interval: {exc}") from exc
        if interval <= 0:
            raise PromtoolError(f"tests[{index}]: interval must be positive")
        series: list[tuple[str, str]] = []
        for item in raw.get("input_series") or []:
            if not isinstance(item, dict) or "series" not in item or "values" not in item:
                raise PromtoolError(
                    f"tests[{index}].input_series entries need series and values"
                )
            series.append((str(item["series"]), str(item["values"])))
        promql_tests = list(raw.get("promql_expr_test") or [])
        alert_tests = list(raw.get("alert_rule_test") or [])
        for item in promql_tests:
            if not isinstance(item, dict) or "expr" not in item:
                raise PromtoolError(f"tests[{index}].promql_expr_test needs expr")
            if "eval_time" not in item:
                raise PromtoolError(f"tests[{index}].promql_expr_test needs eval_time")
        for item in alert_tests:
            if not isinstance(item, dict) or "alertname" not in item:
                raise PromtoolError(f"tests[{index}].alert_rule_test needs alertname")
            if "eval_time" not in item:
                raise PromtoolError(f"tests[{index}].alert_rule_test needs eval_time")
        external = {
            str(key): str(value)
            for key, value in (raw.get("external_labels") or {}).items()
        }
        cases.append(
            _Case(
                name=str(raw.get("name") or f"test {index + 1}"),
                interval_ms=interval,
                input_series=series,
                promql_tests=promql_tests,
                alert_tests=alert_tests,
                external_labels=external,
            )
        )
    return rule_files, default_interval, cases


# ---------------------------------------------------------------------------
# evaluation


def _append_input_series(tsdb: TSDB, series: list[tuple[str, str]], interval_ms: int) -> None:
    samples: list[tuple[Labels, int, float]] = []
    for selector, values_text in series:
        labels = parse_series_labels(selector)
        for index, value in enumerate(expand_values(values_text)):
            if value is None:
                continue
            samples.append((labels, index * interval_ms, float(value)))
    if samples:
        _appended, errors = tsdb.append_batch(samples)
        if errors:
            raise PromtoolError("input series could not be appended: " + "; ".join(errors))


def _evaluate_group_at(manager: RuleManager, groups: list, timestamp: int) -> None:
    for group in groups:
        manager.evaluate_group(group, timestamp)


def _query_vector(engine: Engine, expression: str, timestamp: int) -> dict[tuple, float]:
    result = engine.query(expression, timestamp)
    if isinstance(result.value, Scalar):
        return {(): result.value.value}
    if not isinstance(result.value, Vector):
        raise PromtoolError(
            f"expression {expression!r} returned {result.value.type}, expected a vector"
        )
    return {item.labels.items(): item.value for item in result.value.items}


def _compare_promql(
    engine: Engine,
    test: dict,
    timestamp: int,
) -> list[str]:
    expression = str(test["expr"])
    try:
        actual = _query_vector(engine, expression, timestamp)
    except Exception as exc:
        return [f"expr {expression!r}: evaluation failed: {exc}"]
    expected: dict[tuple, float] = {}
    for sample in test.get("exp_samples") or []:
        if not isinstance(sample, dict):
            return [f"expr {expression!r}: exp_samples entries must be mappings"]
        labels = _labels_from_expected(sample.get("labels"))
        try:
            value = _parse_number(sample.get("value", "NaN"))
        except (TypeError, ValueError):
            return [f"expr {expression!r}: invalid expected value {sample.get('value')!r}"]
        expected[labels.items()] = value
    failures: list[str] = []
    for key in sorted(set(actual) - set(expected)):
        failures.append(
            f"expr {expression!r}: unexpected sample {key} = {format_value(actual[key])}"
        )
    for key in sorted(set(expected) - set(actual)):
        failures.append(
            f"expr {expression!r}: missing sample {key} (expected "
            f"{format_value(expected[key])})"
        )
    for key in sorted(set(actual) & set(expected)):
        if not _float_equal(actual[key], expected[key]):
            failures.append(
                f"expr {expression!r}: sample {key} = {format_value(actual[key])}, "
                f"expected {format_value(expected[key])}"
            )
    return failures


def _firing_alerts(groups: list, alertname: str) -> list[dict]:
    alerts: list[dict] = []
    for group in groups:
        for rule in group.rules:
            if not isinstance(rule, AlertingRule) or rule.alert != alertname:
                continue
            for alert in rule.active.values():
                if alert.state == "firing":
                    alerts.append(
                        {
                            "labels": _labels_to_dict(alert.labels),
                            "annotations": dict(alert.annotations),
                        }
                    )
    return alerts


def _compare_alerts(
    groups: list,
    test: dict,
    alertname: str,
) -> list[str]:
    actual = _firing_alerts(groups, alertname)
    expected: list[dict] = []
    for item in test.get("exp_alerts") or []:
        if not isinstance(item, dict):
            return [f"alert {alertname!r}: exp_alerts entries must be mappings"]
        labels = {
            str(key): str(value)
            for key, value in (item.get("exp_labels") or {}).items()
        }
        labels.setdefault("alertname", alertname)
        annotations = {
            str(key): str(value)
            for key, value in (item.get("exp_annotations") or {}).items()
        }
        expected.append({"labels": labels, "annotations": annotations})
    failures: list[str] = []
    remaining = list(actual)
    for wanted in expected:
        match = next(
            (
                candidate
                for candidate in remaining
                if candidate["labels"] == wanted["labels"]
                and candidate["annotations"] == wanted["annotations"]
            ),
            None,
        )
        if match is None:
            failures.append(
                f"alert {alertname!r}: missing expected alert "
                f"{wanted['labels']} annotations={wanted['annotations']}; "
                f"firing={[item['labels'] for item in actual]}"
            )
        else:
            remaining.remove(match)
    for extra in remaining:
        failures.append(
            f"alert {alertname!r}: unexpected firing alert "
            f"{extra['labels']} annotations={extra['annotations']}"
        )
    return failures


def _run_case(
    case: _Case,
    groups: list,
) -> TestOutcome:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mini-promtool-") as directory:
        tsdb = TSDB(
            TSDBOptions(path=directory, retention_ms=365 * 24 * 3600 * 1000, use_lockfile=False)
        ).open()
        try:
            _append_input_series(tsdb, case.input_series, case.interval_ms)
            manager = RuleManager(
                tsdb,
                groups,
                evaluation_interval_ms=case.interval_ms,
                external_labels=case.external_labels,
            )
            engine = Engine(tsdb, lookback_ms=5 * 60 * 1000, default_step_ms=case.interval_ms)

            # Evaluate at t=0 first so samples at the epoch are visible to both
            # PromQL and pending/firing transitions.
            _evaluate_group_at(manager, groups, 0)
            current = 0

            def advance(target: int) -> None:
                nonlocal current
                while current + case.interval_ms <= target:
                    current += case.interval_ms
                    _evaluate_group_at(manager, groups, current)

            events: list[tuple[int, str, dict]] = []
            for test in case.promql_tests:
                try:
                    when = parse_duration(str(test["eval_time"]))
                except ValueError as exc:
                    failures.append(f"promql_expr_test: invalid eval_time: {exc}")
                    continue
                events.append((when, "promql", test))
            for test in case.alert_tests:
                try:
                    when = parse_duration(str(test["eval_time"]))
                except ValueError as exc:
                    failures.append(f"alert_rule_test: invalid eval_time: {exc}")
                    continue
                events.append((when, "alert", test))
            events.sort(key=lambda item: item[0])
            for when, kind, test in events:
                advance(when)
                if kind == "promql":
                    failures.extend(_compare_promql(engine, test, when))
                else:
                    failures.extend(
                        _compare_alerts(groups, test, str(test["alertname"]))
                    )
        finally:
            tsdb.close()
    return TestOutcome(case.name, not failures, failures)


def run_test_file(path: str | Path) -> list[TestOutcome]:
    """Run every case in ``path`` and return one outcome per test case."""
    test_path = Path(path)
    rule_files, evaluation_interval, cases = _load_test_file(test_path)
    outcomes = []
    for case in cases:
        # Each case gets a fresh TSDB and fresh alert state.  Rule groups are
        # reloaded per case because RuleManager mutates rule state.
        case_groups = load_rule_groups(
            rule_files,
            base_dir=str(test_path.parent),
            evaluation_interval_ms=evaluation_interval,
        ) if rule_files else []
        for group in case_groups:
            # promtool tests use the test interval as the evaluation grid.
            group.interval_ms = case.interval_ms
        outcomes.append(_run_case(case, case_groups))
    return outcomes


def check_rule_files(paths: list[str], *, base_dir: str = ".") -> int:
    groups = load_rule_groups(paths, base_dir=base_dir)
    rules = sum(len(group.rules) for group in groups)
    print(f"SUCCESS: {len(groups)} rule group(s), {rules} rule(s) validated")
    return 0


# ---------------------------------------------------------------------------
# CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-promtool",
        description="Validate rule files and run promtool-style rule unit tests.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_parser = subparsers.add_parser("test", help="run tests")
    test_sub = test_parser.add_subparsers(dest="test_command", required=True)
    test_rules = test_sub.add_parser("rules", help="run a rule unit-test YAML file")
    test_rules.add_argument("files", nargs="+", help="test file(s)")
    test_rules.add_argument("--verbose", action="store_true", help="print passing cases too")

    check_parser = subparsers.add_parser("check", help="validate files")
    check_sub = check_parser.add_subparsers(dest="check_command", required=True)
    check_rules = check_sub.add_parser("rules", help="validate rule files")
    check_rules.add_argument("files", nargs="+", help="rule file(s) or glob(s)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check" and args.check_command == "rules":
        try:
            return check_rule_files(args.files)
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

    if args.command == "test" and args.test_command == "rules":
        failed = 0
        total = 0
        for filename in args.files:
            try:
                outcomes = run_test_file(filename)
            except Exception as exc:
                print(f"FAILED: {filename}: {exc}", file=sys.stderr)
                failed += 1
                total += 1
                continue
            for outcome in outcomes:
                total += 1
                if outcome.passed:
                    if args.verbose:
                        print(f"PASS: {outcome.name}")
                else:
                    failed += 1
                    print(f"FAILED: {outcome.name}")
                    for failure in outcome.failures:
                        print(f"  - {failure}")
        if failed:
            print(f"\nFAILED: {failed} of {total} test(s) failed")
            return 1
        print(f"SUCCESS: {total} test(s) passed")
        return 0

    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
