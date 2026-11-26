"""PromQL evaluation engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from ..labels import Labels, Matcher, match_all
from ..model import ErrorType, PrometheusError, Sample
from ..util.timeutil import is_stale
from . import ast
from .functions import EvalContext, FUNCTIONS, hash_labels
from .parser import parse_expr
from .value import (
    Matrix,
    QueryResult,
    Scalar,
    Series,
    StringValue,
    Value,
    Vector,
    VectorItem,
)

DEFAULT_LOOKBACK_MS = 5 * 60 * 1000
DEFAULT_STEP_MS = 60 * 1000
DEFAULT_MAX_SAMPLES = 50_000_000


class Querier(Protocol):
    def select(
        self, matchers: list[Matcher], mint: int, maxt: int
    ) -> list[tuple[Labels, list[Sample]]]:
        ...


class InMemoryQuerier:
    """A tiny querier used by tests and by `promtool test rules`."""

    def __init__(self) -> None:
        self._series: dict[Labels, list[Sample]] = {}

    def add(
        self, labels: Labels, timestamp: int | Sequence[int], value: float | Sequence[float] | None = None
    ) -> None:
        if isinstance(timestamp, Sequence) and not isinstance(timestamp, (str, bytes)):
            points = list(zip(timestamp, value))  # type: ignore[arg-type]
        else:
            points = [(int(timestamp), float(value))]  # type: ignore[arg-type]
        target = self._series.setdefault(labels, [])
        for ts, val in points:
            target.append(Sample(int(ts), float(val)))
        target.sort(key=lambda sample: sample.timestamp)

    def add_series(self, labels: Labels, samples: list[tuple[int, float]]) -> None:
        target = self._series.setdefault(labels, [])
        target.extend(Sample(ts, value) for ts, value in samples)
        target.sort(key=lambda sample: sample.timestamp)

    def series(self) -> dict[Labels, list[Sample]]:
        return {labels: list(samples) for labels, samples in self._series.items()}

    def select(
        self, matchers: list[Matcher], mint: int, maxt: int
    ) -> list[tuple[Labels, list[Sample]]]:
        out = []
        for labels, samples in self._series.items():
            if not match_all(matchers, labels):
                continue
            selected = [
                sample for sample in samples if mint <= sample.timestamp <= maxt
            ]
            if selected:
                out.append((labels, selected))
        return out


@dataclass
class _EvalState:
    start: int
    end: int
    warnings: list[str] = field(default_factory=list)
    samples: int = 0
    max_samples: int = DEFAULT_MAX_SAMPLES

    def count(self, value: int) -> None:
        self.samples += value
        if self.samples > self.max_samples:
            raise PrometheusError(
                "query processing would load too many samples into memory",
                error_type=ErrorType.EXECUTION,
            )


class Engine:
    def __init__(
        self,
        querier: Querier,
        *,
        lookback_ms: int = DEFAULT_LOOKBACK_MS,
        default_step_ms: int = DEFAULT_STEP_MS,
        max_samples: int = DEFAULT_MAX_SAMPLES,
    ) -> None:
        self.querier = querier
        self.lookback_ms = lookback_ms
        self.default_step_ms = default_step_ms
        self.max_samples = max_samples

    # ---- public --------------------------------------------------------
    def query(self, query: str | ast.Expr, timestamp: int) -> QueryResult:
        expr = parse_expr(query) if isinstance(query, str) else query
        state = _EvalState(timestamp, timestamp, max_samples=self.max_samples)
        value = self.evaluate(expr, timestamp, state)
        return QueryResult(value, state.warnings, _stats(value))

    def query_range(
        self, query: str | ast.Expr, start: int, end: int, step: int
    ) -> QueryResult:
        expr = parse_expr(query) if isinstance(query, str) else query
        if step <= 0:
            raise PrometheusError("zero or negative query resolution step widths are not accepted", error_type=ErrorType.BAD_DATA)
        if end < start:
            raise PrometheusError("end timestamp must not be before start time", error_type=ErrorType.BAD_DATA)
        if expr.value_type == "string":
            raise PrometheusError(
                "query_range does not support string expressions",
                error_type=ErrorType.BAD_DATA,
            )
        state = _EvalState(start, end, max_samples=self.max_samples)
        collected: dict[Labels, list[tuple[int, float]]] = {}
        timestamps = list(range(start, end + 1, step))
        for ts in timestamps:
            value = self.evaluate(expr, ts, state)
            if isinstance(value, Scalar):
                collected.setdefault(Labels.empty(), []).append((ts, value.value))
            elif isinstance(value, Vector):
                for item in value.items:
                    collected.setdefault(item.labels, []).append((ts, item.value))
            elif isinstance(value, Matrix):
                raise PrometheusError(
                    "query_range does not support range-vector expressions",
                    error_type=ErrorType.BAD_DATA,
                )
            else:
                raise PrometheusError(
                    "query_range does not support string expressions",
                    error_type=ErrorType.BAD_DATA,
                )
            state.count(
                len(value.items) if isinstance(value, Vector) else 1
            )
        series = [
            Series(labels, points) for labels, points in sorted(collected.items())
        ]
        return QueryResult(Matrix(series), state.warnings, {
            "series": len(series),
            "samples": sum(len(item.samples) for item in series),
            "steps": len(timestamps),
        })

    # ---- evaluation ----------------------------------------------------
    def evaluate(self, node: ast.Expr, ts: int, state: _EvalState) -> Value:
        if isinstance(node, ast.NumberLiteral):
            return Scalar(node.value)
        if isinstance(node, ast.StringLiteral):
            return StringValue(node.value)
        if isinstance(node, ast.ParenExpr):
            return self.evaluate(node.expr, ts, state)
        if isinstance(node, ast.VectorSelector):
            return self._eval_vector_selector(node, ts, state)
        if isinstance(node, ast.MatrixSelector):
            return self._eval_matrix_selector(node, ts, state)
        if isinstance(node, ast.SubqueryExpr):
            return self._eval_subquery(node, ts, state)
        if isinstance(node, ast.UnaryExpr):
            return self._eval_unary(node, ts, state)
        if isinstance(node, ast.BinaryExpr):
            return self._eval_binary(node, ts, state)
        if isinstance(node, ast.AggregateExpr):
            return self._eval_aggregate(node, ts, state)
        if isinstance(node, ast.Call):
            return self._eval_call(node, ts, state)
        raise PrometheusError(f"cannot evaluate node {type(node).__name__}")

    def _effective_time(self, at: ast.AtModifier | None, offset_ms: int, ts: int, state: _EvalState) -> int:
        if at is None:
            base = ts
        elif at.kind == "timestamp":
            base = at.timestamp
        elif at.kind == "start":
            base = state.start
        else:
            base = state.end
        return base - offset_ms

    def _eval_vector_selector(
        self, node: ast.VectorSelector, ts: int, state: _EvalState
    ) -> Vector:
        evaluation_time = self._effective_time(node.at, node.offset_ms, ts, state)
        mint = evaluation_time - self.lookback_ms
        maxt = evaluation_time
        # Prometheus 3.0 made range selectors and the lookback left-open:
        # a sample exactly at t-lookback is no longer considered "fresh".
        series = self.querier.select(node.matchers, mint + 1, maxt)
        out: list[VectorItem] = []
        selected = 0
        for labels, samples in series:
            latest = None
            for sample in samples:
                if sample.timestamp > maxt:
                    break
                if sample.timestamp <= mint:
                    continue
                latest = sample
            if latest is None:
                continue
            if is_stale(latest.value):
                continue
            if math.isnan(latest.value):
                # Ordinary NaN is a valid sample; only the stale bit pattern
                # suppresses a series.
                pass
            out.append(
                VectorItem(labels, latest.value, ts, latest.timestamp)
            )
            selected += 1
        state.count(selected)
        out.sort(key=lambda item: item.labels)
        return Vector(out)

    def _eval_matrix_selector(
        self, node: ast.MatrixSelector, ts: int, state: _EvalState
    ) -> Matrix:
        selector = node.vector
        evaluation_time = self._effective_time(selector.at, selector.offset_ms, ts, state)
        mint = evaluation_time - node.range_ms
        maxt = evaluation_time
        # Range vectors are left-open, right-closed: (t-range, t].
        series = self.querier.select(selector.matchers, mint + 1, maxt)
        out: list[Series] = []
        total = 0
        for labels, samples in series:
            points = [
                (sample.timestamp, sample.value)
                for sample in samples
                if not is_stale(sample.value) and mint < sample.timestamp <= maxt
            ]
            if points:
                out.append(Series(labels, points))
                total += len(points)
        state.count(total)
        return Matrix(out, node.range_ms, mint, maxt)

    def _eval_subquery(
        self, node: ast.SubqueryExpr, ts: int, state: _EvalState
    ) -> Matrix:
        end = self._effective_time(node.at, node.offset_ms, ts, state)
        start = end - node.range_ms
        step = node.step_ms or self.default_step_ms
        # Prometheus evaluates subqueries on the absolute step grid and, since
        # 3.0, excludes the left boundary.  Aligning to ``start`` instead would
        # make [1m:1m] return two points and would diverge for non-aligned
        # windows.
        first = (start // step + 1) * step
        times = list(range(first, end + 1, step))
        collected: dict[Labels, list[tuple[int, float]]] = {}
        for moment in times:
            value = self.evaluate(node.expr, moment, state)
            if isinstance(value, Vector):
                for item in value.items:
                    collected.setdefault(item.labels, []).append((moment, item.value))
            elif isinstance(value, Scalar):
                collected.setdefault(Labels.empty(), []).append((moment, value.value))
            else:
                raise PrometheusError(
                    "subquery is only allowed on instant vector expressions",
                    error_type=ErrorType.BAD_DATA,
                )
        return Matrix(
            [Series(labels, points) for labels, points in collected.items()],
            node.range_ms,
            start,
            end,
        )

    def _eval_unary(self, node: ast.UnaryExpr, ts: int, state: _EvalState) -> Value:
        value = self.evaluate(node.expr, ts, state)
        if node.op == "+":
            return value
        if isinstance(value, Scalar):
            return Scalar(-value.value)
        if isinstance(value, Vector):
            # Prometheus drops __name__ for unary minus (unary plus is a no-op).
            return Vector(
                [
                    VectorItem(
                        item.labels.without("__name__"),
                        -item.value,
                        item.timestamp,
                        item.source_timestamp,
                    )
                    for item in value.items
                ]
            )
        raise PrometheusError("unary operator requires a scalar or instant vector")

    # ---- binary --------------------------------------------------------
    def _eval_binary(self, node: ast.BinaryExpr, ts: int, state: _EvalState) -> Value:
        lhs = self.evaluate(node.lhs, ts, state)
        rhs = self.evaluate(node.rhs, ts, state)
        op = node.op
        if isinstance(lhs, Scalar) and isinstance(rhs, Scalar):
            if op in _COMPARISONS:
                if not node.bool_modifier:
                    raise PrometheusError(
                        "comparisons between scalars must use the bool modifier",
                        error_type=ErrorType.BAD_DATA,
                    )
                return Scalar(1.0 if _compare(op, lhs.value, rhs.value) else 0.0)
            value = _apply_binary(op, lhs.value, rhs.value)
            return Scalar(value)
        if isinstance(lhs, Vector) and isinstance(rhs, Scalar):
            return self._vector_scalar(node, lhs, rhs, ts, scalar_on_left=False)
        if isinstance(lhs, Scalar) and isinstance(rhs, Vector):
            return self._vector_scalar(node, rhs, lhs, ts, scalar_on_left=True)
        if isinstance(lhs, Vector) and isinstance(rhs, Vector):
            return self._vector_vector(node, lhs, rhs, ts)
        raise PrometheusError(
            f"binary operator {op!r} is not defined for these operand types",
            error_type=ErrorType.BAD_DATA,
        )

    def _vector_scalar(
        self, node: ast.BinaryExpr, vector: Vector, scalar: Scalar, ts: int, *, scalar_on_left: bool
    ) -> Vector:
        op = node.op
        out = []
        for item in vector.items:
            left = scalar.value if scalar_on_left else item.value
            right = item.value if scalar_on_left else scalar.value
            truth = _compare(op, left, right) if op in _COMPARISONS else None
            if truth is not None:
                if node.bool_modifier:
                    out.append(
                        VectorItem(
                            item.labels.without("__name__"),
                            1.0 if truth else 0.0,
                            ts,
                        )
                    )
                elif truth:
                    out.append(VectorItem(item.labels, item.value, ts, item.source_timestamp))
            else:
                value = _apply_binary(op, left, right)
                out.append(
                    VectorItem(
                        item.labels.without("__name__"), value, ts
                    )
                )
        return Vector(out)

    def _vector_vector(
        self, node: ast.BinaryExpr, lhs: Vector, rhs: Vector, ts: int
    ) -> Vector:
        op = node.op
        matching = node.matching
        right_groups: dict[tuple, list[VectorItem]] = {}
        for item in rhs.items:
            right_groups.setdefault(matching.signature(item.labels), []).append(item)
        left_groups: dict[tuple, list[VectorItem]] = {}
        for item in lhs.items:
            left_groups.setdefault(matching.signature(item.labels), []).append(item)

        if op in _SET_OPS:
            return self._set_operation(node, lhs, rhs, right_groups, ts)

        out: list[VectorItem] = []

        def reject_duplicates(groups: dict[tuple, list[VectorItem]], side: str) -> None:
            for items in groups.values():
                if len(items) > 1:
                    raise PrometheusError(
                        f"found duplicate series for the match group on the {side} hand-side",
                        error_type=ErrorType.EXECUTION,
                    )

        if matching.card == "one-to-one":
            # Upstream builds a signature map for each side and rejects
            # duplicates even when the other side has no matching group.
            reject_duplicates(left_groups, "left")
            reject_duplicates(right_groups, "right")
            for signature, left_items in left_groups.items():
                right_items = right_groups.get(signature, [])
                if not right_items:
                    continue
                pair = self._match_pair(node, left_items[0], right_items[0], ts, "lhs")
                if pair is not None:
                    out.append(pair)
            return Vector(out)

        if matching.card == "many-to-one":
            # group_left: many on the left, exactly one on the right.
            reject_duplicates(right_groups, "right")
            for signature, left_items in left_groups.items():
                right_items = right_groups.get(signature, [])
                if not right_items:
                    continue
                for left_item in left_items:
                    pair = self._match_pair(node, left_item, right_items[0], ts, "lhs")
                    if pair is not None:
                        out.append(pair)
            return Vector(out)

        # one-to-many (group_right): exactly one on the left, many on the right.
        reject_duplicates(left_groups, "left")
        for signature, right_items in right_groups.items():
            left_items = left_groups.get(signature, [])
            if not left_items:
                continue
            for right_item in right_items:
                pair = self._match_pair(node, left_items[0], right_item, ts, "rhs")
                if pair is not None:
                    out.append(pair)
        return Vector(out)

    def _result_labels(
        self, matching: ast.VectorMatching, base: Labels, other: Labels
    ) -> Labels:
        """Upstream ``resultMetric``: keep/drop for one-to-one, include for group.

        For one-to-one matching, ``on(...)`` keeps only the listed labels and
        ``ignoring(...)`` removes them; both drop everything else (including
        ``__name__``).  For group_left/group_right the "many" side keeps its
        labels and copies ``include`` labels from the "one" side.
        """
        if matching.card == "one-to-one":
            if matching.on:
                labels = base.keep(*matching.labels)
            else:
                labels = base.without(*matching.labels)
        else:
            labels = base
        for name in matching.include:
            labels = labels.set(name, other.get(name))
        return labels.without("__name__")

    def _match_pair(
        self, node: ast.BinaryExpr, left: VectorItem, right: VectorItem, ts: int, side: str
    ) -> VectorItem:
        op = node.op
        matching = node.matching
        base = left.labels if side == "lhs" else right.labels
        other = right.labels if side == "lhs" else left.labels
        labels = self._result_labels(matching, base, other)
        if op in _COMPARISONS:
            truth = _compare(op, left.value, right.value)
            assert truth is not None
            if node.bool_modifier:
                return VectorItem(labels, 1.0 if truth else 0.0, ts)
            if not truth:
                return None  # type: ignore[return-value]
            # A filtering comparison returns the sample from the "many" side:
            # LHS for one-to-one/group_left, RHS for group_right, but with the
            # resultMetric labels computed above (include/on/ignoring apply).
            if side == "lhs":
                return VectorItem(labels, left.value, ts, left.source_timestamp)
            return VectorItem(labels, right.value, ts, right.source_timestamp)
        value = _apply_binary(op, left.value, right.value)
        return VectorItem(labels, value, ts)

    def _set_operation(
        self,
        node: ast.BinaryExpr,
        lhs: Vector,
        rhs: Vector,
        right_groups: dict[tuple, list[VectorItem]],
        ts: int,
    ) -> Vector:
        op = node.op
        matching = node.matching
        if op == "and":
            return Vector(
                [
                    VectorItem(item.labels, item.value, ts, item.source_timestamp)
                    for item in lhs.items
                    if matching.signature(item.labels) in right_groups
                ]
            )
        if op == "unless":
            return Vector(
                [
                    VectorItem(item.labels, item.value, ts, item.source_timestamp)
                    for item in lhs.items
                    if matching.signature(item.labels) not in right_groups
                ]
            )
        # or
        out = [
            VectorItem(item.labels, item.value, ts, item.source_timestamp)
            for item in lhs.items
        ]
        lhs_signatures = {matching.signature(item.labels) for item in lhs.items}
        out.extend(
            VectorItem(item.labels, item.value, ts, item.source_timestamp)
            for item in rhs.items
            if matching.signature(item.labels) not in lhs_signatures
        )
        return Vector(out)

    # ---- aggregation ---------------------------------------------------
    def _eval_aggregate(
        self, node: ast.AggregateExpr, ts: int, state: _EvalState
    ) -> Vector:
        value = self.evaluate(node.expr, ts, state)
        if not isinstance(value, Vector):
            raise PrometheusError(
                "aggregation requires an instant vector", error_type=ErrorType.BAD_DATA
            )
        param_value = None
        if node.param is not None:
            param = self.evaluate(node.param, ts, state)
            if isinstance(param, Scalar):
                param_value = param.value
            elif isinstance(param, StringValue):
                param_value = param.value
        groups: dict[Labels, list[VectorItem]] = {}
        for item in value.items:
            key = _aggregation_key(item.labels, node.grouping, node.without)
            groups.setdefault(key, []).append(item)
        out: list[VectorItem] = []
        for key, items in groups.items():
            values = [item.value for item in items]
            op = node.op
            if op == "sum":
                out.append(VectorItem(key, sum(values), ts))
            elif op == "avg":
                out.append(VectorItem(key, sum(values) / len(values), ts))
            elif op == "count":
                out.append(VectorItem(key, float(len(values)), ts))
            elif op == "group":
                out.append(VectorItem(key, 1.0, ts))
            elif op in ("min", "max"):
                # Prometheus ignores NaN while any finite value exists; all
                # NaN yields NaN.  Python's min/max would let NaN win based
                # on input order.
                finite = [value for value in values if not math.isnan(value)]
                if finite:
                    result = min(finite) if op == "min" else max(finite)
                else:
                    result = math.nan
                out.append(VectorItem(key, result, ts))
            elif op in ("stddev", "stdvar"):
                mean = sum(values) / len(values)
                variance = sum((v - mean) ** 2 for v in values) / len(values)
                out.append(
                    VectorItem(
                        key,
                        math.sqrt(variance) if op == "stddev" else variance,
                        ts,
                    )
                )
            elif op in ("topk", "bottomk"):
                if param_value is None or math.isnan(param_value):
                    continue
                if math.isinf(param_value):
                    k = len(items) if param_value > 0 else 0
                else:
                    k = int(param_value)
                if k <= 0:
                    continue
                ordered = _select_topk(items, k, top=op == "topk")
                for item in ordered:
                    out.append(
                        VectorItem(item.labels, item.value, ts, item.source_timestamp)
                    )
            elif op == "quantile":
                out.append(VectorItem(key, _quantile(param_value or 0.0, values), ts))
            elif op == "count_values":
                label_name = str(param_value)
                counts: dict[str, int] = {}
                for item in items:
                    formatted = _format_float(item.value)
                    counts[formatted] = counts.get(formatted, 0) + 1
                for formatted, count in counts.items():
                    labels = key.set(label_name, formatted)
                    out.append(VectorItem(labels, float(count), ts))
            elif op == "limitk":
                if param_value is None or math.isnan(param_value):
                    continue
                if math.isinf(param_value):
                    k = len(items) if param_value > 0 else 0
                else:
                    k = int(param_value)
                if k <= 0:
                    continue
                ordered = sorted(items, key=lambda item: hash_labels(item.labels))
                for item in ordered[:k]:
                    out.append(
                        VectorItem(item.labels, item.value, ts, item.source_timestamp)
                    )
            elif op == "limit_ratio":
                if param_value is None or not math.isfinite(param_value):
                    continue
                ratio = float(param_value)
                if ratio < 0:
                    kept = [
                        item for item in items
                        if hash_labels(item.labels) > (1 + ratio)
                    ]
                else:
                    kept = [
                        item for item in items
                        if hash_labels(item.labels) <= ratio
                    ]
                for item in kept:
                    out.append(
                        VectorItem(item.labels, item.value, ts, item.source_timestamp)
                    )
            else:
                raise PrometheusError(f"unknown aggregation {op!r}")
        return Vector(out)

    # ---- function calls ------------------------------------------------
    def _eval_call(self, node: ast.Call, ts: int, state: _EvalState) -> Value:
        spec = FUNCTIONS[node.func]
        if node.func in ("absent", "absent_over_time"):
            return self._eval_absent(node, ts, state)
        args: list[Value] = []
        for index, arg_node in enumerate(node.args):
            args.append(self.evaluate(arg_node, ts, state))
        ctx = EvalContext(ts, state.start, state.end, state.warnings)
        ctx.range_hint = _range_from_ast(node.args[0]) if node.args else 0
        if args and isinstance(args[0], Matrix) and args[0].range_ms:
            ctx.range_hint = args[0].range_ms
        result = spec.handler(args, ctx)
        if isinstance(result, Vector):
            state.count(len(result.items))
        elif isinstance(result, Matrix):
            state.count(sum(len(series.samples) for series in result.series))
        return result

    def _eval_absent(self, node: ast.Call, ts: int, state: _EvalState) -> Vector:
        arg = node.args[0]
        value = self.evaluate(arg, ts, state)
        empty = False
        if isinstance(value, Vector):
            empty = len(value.items) == 0
        elif isinstance(value, Matrix):
            empty = len(value.series) == 0
        if not empty:
            return Vector([])
        matchers = ast.selector_matchers(arg)
        # Upstream walks the matchers in order: the first matcher for a name
        # may set it (only a non-empty equality), and any later matcher for
        # the same name removes it.  Thus `a="1",a="2"` yields no `a` label.
        derived: dict[str, str] = {}
        seen: set[str] = set()
        for matcher in matchers:
            if matcher.name == "__name__":
                continue
            if matcher.name in seen:
                derived.pop(matcher.name, None)
                continue
            seen.add(matcher.name)
            if matcher.op == "=" and matcher.value != "":
                derived[matcher.name] = matcher.value
            else:
                derived.pop(matcher.name, None)
        return Vector([VectorItem(Labels(derived), 1.0, ts)])


# ---------------------------------------------------------------------------
# operators


_COMPARISONS = {"==", "!=", "<", ">", "<=", ">="}
_SET_OPS = {"and", "or", "unless"}


def _apply_binary(op: str, left: float, right: float) -> float:
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            if left == 0 or math.isnan(left):
                return math.nan
            # IEEE sign is sign(left) XOR sign(right), so 1/-0 is -Inf.
            sign = math.copysign(1.0, left) * math.copysign(1.0, right)
            return math.inf * sign
        return left / right
    if op == "%":
        if right == 0:
            return math.nan
        try:
            return math.fmod(left, right)
        except ValueError:
            # Go's math.Mod(+Inf, y) is NaN; Python raises instead.
            return math.nan
    if op == "^":
        try:
            return math.pow(left, right)
        except ValueError:
            # Go's math.Pow(0, negative) is +Inf; Python raises. Negative
            # bases with fractional exponents are NaN in both.
            if left == 0 and right < 0:
                return math.inf
            return math.nan
        except OverflowError:
            # Go returns ±Inf on overflow.
            negative = left < 0 and float(right).is_integer() and int(right) % 2 == 1
            return -math.inf if negative else math.inf
    if op == "atan2":
        return math.atan2(left, right)
    raise PrometheusError(f"unknown binary operator {op!r}")


def _compare(op: str, left: float, right: float) -> bool:
    # IEEE 754 / Go semantics: NaN != x is true, NaN == x is false, and all
    # ordering comparisons with NaN are false.  A blanket NaN check would
    # incorrectly drop `NaN != x` results.
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if math.isnan(left) or math.isnan(right):
        return False
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == ">=":
        return left >= right
    raise PrometheusError(f"unknown comparison {op!r}")


def _aggregation_key(labels: Labels, grouping: list[str], without: bool) -> Labels:
    if without:
        # `without` always drops the metric name, even if it is listed.
        return labels.without(*grouping, "__name__")
    wanted = set(grouping)
    # `by (__name__)` is the documented way to preserve the metric name;
    # discarding it here would make that pattern silently fail.
    return Labels((name, value) for name, value in labels.items() if name in wanted)


def _select_topk(items: list[VectorItem], k: int, *, top: bool) -> list[VectorItem]:
    finite = [item for item in items if not math.isnan(item.value)]
    nans = [item for item in items if math.isnan(item.value)]
    finite.sort(key=lambda item: item.value, reverse=top)
    # Both topk and bottomk prefer real values over NaN; NaN is only
    # returned when there are fewer than k finite samples.
    return (finite + nans)[:k]


def _quantile(q: float, values: list[float]) -> float:
    if not values or math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    # Prometheus' vectorByValueHeap sorts NaN before every finite value.
    ordered = sorted(
        values, key=lambda value: (0, 0.0) if math.isnan(value) else (1, value)
    )
    if len(ordered) == 1:
        return ordered[0]
    rank = q * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _format_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(value)


def _range_from_ast(node: ast.Expr) -> int:
    if isinstance(node, ast.MatrixSelector):
        return node.range_ms
    if isinstance(node, ast.SubqueryExpr):
        return node.range_ms
    if isinstance(node, ast.ParenExpr):
        return _range_from_ast(node.expr)
    return 0


def _stats(value: Value) -> dict:
    if isinstance(value, Vector):
        return {"series": len(value.items), "samples": len(value.items)}
    if isinstance(value, Matrix):
        return {
            "series": len(value.series),
            "samples": sum(len(series.samples) for series in value.series),
        }
    return {"series": 1, "samples": 1}
