"""PromQL abstract syntax tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..labels import Matcher


class Expr:
    value_type: str = "unknown"
    position: int = 0


@dataclass(slots=True)
class NumberLiteral(Expr):
    value: float
    position: int = 0
    value_type = "scalar"


@dataclass(slots=True)
class StringLiteral(Expr):
    value: str
    position: int = 0
    value_type = "string"


@dataclass(slots=True)
class AtModifier:
    """``@ <timestamp>``; ``kind`` is ``timestamp``, ``start`` or ``end``."""

    kind: str
    timestamp: int = 0


@dataclass(slots=True)
class VectorSelector(Expr):
    name: str
    matchers: list[Matcher]
    offset_ms: int = 0
    at: AtModifier | None = None
    position: int = 0
    value_type = "vector"

    def all_matchers(self) -> list[Matcher]:
        return list(self.matchers)


@dataclass(slots=True)
class MatrixSelector(Expr):
    vector: VectorSelector
    range_ms: int
    position: int = 0
    value_type = "matrix"

    @property
    def offset_ms(self) -> int:
        return self.vector.offset_ms

    @property
    def at(self) -> AtModifier | None:
        return self.vector.at


@dataclass(slots=True)
class SubqueryExpr(Expr):
    expr: Expr
    range_ms: int
    step_ms: int | None = None
    offset_ms: int = 0
    at: AtModifier | None = None
    position: int = 0
    value_type = "matrix"


@dataclass(slots=True)
class ParenExpr(Expr):
    expr: Expr
    position: int = 0

    @property
    def value_type(self) -> str:  # type: ignore[override]
        return self.expr.value_type


@dataclass(slots=True)
class UnaryExpr(Expr):
    op: str
    expr: Expr
    position: int = 0

    @property
    def value_type(self) -> str:  # type: ignore[override]
        return self.expr.value_type


@dataclass(slots=True)
class VectorMatching:
    on: bool = False
    labels: list[str] = field(default_factory=list)
    card: str = "one-to-one"  # one-to-one | many-to-one | one-to-many
    include: list[str] = field(default_factory=list)

    def matching_labels(self, labels) -> tuple[tuple[str, str], ...]:
        if self.on:
            wanted = set(self.labels)
            return tuple((n, v) for n, v in labels.items() if n in wanted)
        ignored = set(self.labels) | {"__name__"}
        return tuple((n, v) for n, v in labels.items() if n not in ignored)

    def signature(self, labels) -> tuple[tuple[str, str], ...]:
        return self.matching_labels(labels)


@dataclass(slots=True)
class BinaryExpr(Expr):
    op: str
    lhs: Expr
    rhs: Expr
    bool_modifier: bool = False
    matching: VectorMatching = field(default_factory=VectorMatching)
    position: int = 0
    value_type: str = "vector"


@dataclass(slots=True)
class AggregateExpr(Expr):
    op: str
    expr: Expr
    param: Expr | None = None
    grouping: list[str] = field(default_factory=list)
    without: bool = False
    position: int = 0
    value_type = "vector"


@dataclass(slots=True)
class Call(Expr):
    func: str
    args: list[Expr] = field(default_factory=list)
    position: int = 0
    value_type: str = "vector"


AGGREGATORS = {
    "sum", "avg", "min", "max", "count", "stddev", "stdvar", "group",
    "topk", "bottomk", "count_values", "quantile", "limitk", "limit_ratio",
}
PARAM_AGGREGATORS = {"topk", "bottomk", "quantile", "limitk", "limit_ratio"}


def walk(node: Expr):
    yield node
    if isinstance(node, (ParenExpr, UnaryExpr)):
        yield from walk(node.expr)
    elif isinstance(node, BinaryExpr):
        yield from walk(node.lhs)
        yield from walk(node.rhs)
    elif isinstance(node, AggregateExpr):
        yield from walk(node.expr)
        if node.param is not None:
            yield from walk(node.param)
    elif isinstance(node, Call):
        for arg in node.args:
            yield from walk(arg)
    elif isinstance(node, SubqueryExpr):
        yield from walk(node.expr)
    elif isinstance(node, MatrixSelector):
        yield from walk(node.vector)


def selector_matchers(node: Expr) -> list[Matcher]:
    """Best-effort matcher extraction for absent() label synthesis.

    Upstream deliberately does not recurse into a ``ParenExpr``; ``absent((x))``
    therefore returns an empty label set rather than borrowing the selector's
    equality labels.
    """
    if isinstance(node, MatrixSelector):
        return selector_matchers(node.vector)
    if isinstance(node, SubqueryExpr):
        return selector_matchers(node.expr)
    if isinstance(node, VectorSelector):
        return list(node.matchers)
    return []


def name_from_matchers(matchers: list[Matcher]) -> str:
    for matcher in matchers:
        if matcher.name == "__name__" and matcher.op == "=":
            return matcher.value
    return ""


def __repr__(node: Any) -> str:  # pragma: no cover - helper for debugging
    return f"<{type(node).__name__}>"
