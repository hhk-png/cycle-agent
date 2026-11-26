"""A small, deliberately non-eval search DSL.

Supported grammar (case-insensitive keywords)::

    expr        := or_expr
    or_expr     := and_expr (OR and_expr)*
    and_expr    := unary (AND unary)*
    unary       := NOT unary | '(' expr ')' | predicate
    predicate   := field (comparison literal
                          | [NOT] IN '(' literal (',' literal)* ')'
                          | [NOT] LIKE string
                          | [NOT] ILIKE string
                          | IS [NOT] NULL)
    field       := IDENT | '`' IDENT '`'
    comparison  := '=' | '!=' | '<>' | '>' | '>=' | '<' | '<='

Scopes are ``attributes``, ``params``, ``tags``, ``metrics``, ``datasets``
and ``assessments``.  The parser does not execute anything.  A compiler turns
the AST into parameterized SQL;
an evaluator supports small registry searches where loading all rows and
filtering in Python is simpler and safer than dynamic SQL.

This is intentionally *not* a general SQL parser.  For example, it does not
support subqueries, functions, arithmetic, or user-defined operators.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .errors import SearchSyntaxError


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    scope: str
    key: str

    @property
    def text(self) -> str:
        return f"{self.scope}.{self.key}"


@dataclass(frozen=True)
class Predicate:
    field: Field
    op: str
    value: Any


@dataclass(frozen=True)
class IsNull:
    field: Field
    negated: bool = False


@dataclass(frozen=True)
class BoolOp:
    op: str
    children: tuple[Any, ...]


@dataclass(frozen=True)
class Not:
    child: Any


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    pos: int


_TOKEN_RE = re.compile(
    r"""
    (?P<WS>\s+)
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<COMMA>,)
  | (?P<STRING>'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.|"")*")
  | (?P<BACKTICK>`(?:[^`]|``)*`)
  | (?P<NUMBER>-?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<OP>!=|<>|>=|<=|=|>|<)
  | (?P<IDENT>[A-Za-z_][A-Za-z0-9_.@/\-]*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"AND", "OR", "NOT", "IN", "LIKE", "ILIKE", "IS", "NULL"}


def _unquote_string(raw: str) -> str:
    quote = raw[0]
    body = raw[1:-1]
    # SQL-style doubled quotes first, then backslash escapes.
    body = body.replace(quote + quote, quote)
    out: list[str] = []
    escaped = False
    for char in body:
        if escaped:
            mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
            out.append(mapping.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    if escaped:
        raise SearchSyntaxError("string literal ends with a dangling escape")
    return "".join(out)


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    position = 0
    length = len(text)
    while position < length:
        match = _TOKEN_RE.match(text, position)
        if not match:
            excerpt = text[position : position + 20]
            raise SearchSyntaxError(f"unexpected input near {excerpt!r}")
        kind = match.lastgroup or ""
        raw = match.group()
        start = position
        position = match.end()
        if kind == "WS":
            continue
        if kind == "STRING":
            tokens.append(Token("LITERAL", _unquote_string(raw), start))
        elif kind == "BACKTICK":
            content = raw[1:-1].replace("``", "`")
            if not content:
                raise SearchSyntaxError("empty backtick identifier")
            tokens.append(Token("BT_IDENT", content, start))
        elif kind == "NUMBER":
            if re.search(r"[.eE]", raw):
                tokens.append(Token("LITERAL", float(raw), start))
            else:
                tokens.append(Token("LITERAL", int(raw), start))
        elif kind == "IDENT":
            upper = raw.upper()
            if upper in _KEYWORDS:
                tokens.append(Token(upper, upper, start))
            elif upper in {"TRUE", "FALSE"}:
                tokens.append(Token("LITERAL", upper == "TRUE", start))
            else:
                tokens.append(Token("IDENT", raw, start))
        else:
            tokens.append(Token(kind, raw, start))
    tokens.append(Token("EOF", None, length))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        self.index += 1
        return token

    def accept(self, kind: str) -> Token | None:
        if self.current.kind == kind:
            return self.advance()
        return None

    def expect(self, kind: str) -> Token:
        if self.current.kind != kind:
            raise SearchSyntaxError(
                f"expected {kind} at position {self.current.pos}, got {self.current.kind}"
            )
        return self.advance()

    def parse(self) -> Any:
        node = self.parse_or()
        if self.current.kind != "EOF":
            raise SearchSyntaxError(f"unexpected token {self.current.kind!r} at position {self.current.pos}")
        return node

    def parse_or(self) -> Any:
        children = [self.parse_and()]
        while self.accept("OR"):
            children.append(self.parse_and())
        return children[0] if len(children) == 1 else BoolOp("OR", tuple(children))

    def parse_and(self) -> Any:
        children = [self.parse_unary()]
        while self.accept("AND"):
            children.append(self.parse_unary())
        return children[0] if len(children) == 1 else BoolOp("AND", tuple(children))

    def parse_unary(self) -> Any:
        if self.accept("NOT"):
            return Not(self.parse_unary())
        if self.accept("LPAREN"):
            node = self.parse_or()
            self.expect("RPAREN")
            return node
        return self.parse_predicate()

    def parse_field(self) -> Field:
        if self.current.kind == "BT_IDENT":
            text = str(self.advance().value)
        elif self.current.kind == "IDENT":
            text = str(self.advance().value)
            # Backtick keys: tags.`some key` arrives as IDENT("tags.") + BT_IDENT.
            if text.endswith(".") and self.current.kind == "BT_IDENT":
                text += str(self.advance().value)
        else:
            raise SearchSyntaxError(f"expected a field at position {self.current.pos}")
        if "." not in text:
            # Convenience aliases for run attributes.
            return Field("attributes", text)
        scope, key = text.split(".", 1)
        scope = scope.lower()
        if scope not in {"attributes", "params", "tags", "metrics", "datasets", "assessments"}:
            raise SearchSyntaxError(f"unsupported field scope {scope!r}")
        if not key:
            raise SearchSyntaxError("field key must not be empty")
        return Field(scope, key)

    def parse_value(self) -> Any:
        token = self.current
        if token.kind == "LITERAL":
            self.advance()
            return token.value
        if token.kind == "IDENT":
            self.advance()
            return token.value
        raise SearchSyntaxError(f"expected a literal at position {token.pos}")

    def parse_predicate(self) -> Any:
        field = self.parse_field()
        if self.accept("IS"):
            negated = bool(self.accept("NOT"))
            self.expect("NULL")
            return IsNull(field, negated)

        negated = False
        if self.current.kind == "NOT":
            # NOT can only introduce IN / LIKE / ILIKE in this position.
            if self.tokens[self.index + 1].kind not in {"IN", "LIKE", "ILIKE"}:
                raise SearchSyntaxError("NOT must be followed by IN, LIKE or ILIKE here")
            self.advance()
            negated = True

        if self.current.kind in {"LIKE", "ILIKE"}:
            op = self.advance().kind
            if self.current.kind != "LITERAL" or not isinstance(self.current.value, str):
                raise SearchSyntaxError(f"{op} requires a string literal")
            value = self.advance().value
            return Predicate(field, ("NOT " if negated else "") + op, value)

        if self.current.kind == "IN":
            self.advance()
            self.expect("LPAREN")
            values = [self.parse_value()]
            while self.accept("COMMA"):
                values.append(self.parse_value())
                if len(values) > 500:
                    raise SearchSyntaxError("IN list supports at most 500 values")
            self.expect("RPAREN")
            if not values:
                raise SearchSyntaxError("IN list must not be empty")
            return Predicate(field, ("NOT IN" if negated else "IN"), tuple(values))

        if negated:
            raise SearchSyntaxError("unexpected NOT")

        token = self.current
        if token.kind != "OP":
            raise SearchSyntaxError(f"expected a comparison operator at position {token.pos}")
        op = self.advance().value
        value = self.parse_value()
        return Predicate(field, op, value)


def parse_filter(expression: str | None) -> Any | None:
    if expression is None or not str(expression).strip():
        return None
    text = str(expression)
    if len(text) > 64 * 1024:
        raise SearchSyntaxError("filter expression is too long (max 64 KiB)")
    try:
        return _Parser(tokenize(text)).parse()
    except RecursionError as exc:
        raise SearchSyntaxError("filter expression is nested too deeply") from exc


# ---------------------------------------------------------------------------
# Run SQL compiler
# ---------------------------------------------------------------------------
_ATTR_COLUMNS = {
    "run_id": "r.run_id",
    "experiment_id": "r.experiment_id",
    "run_name": "r.run_name",
    "user_id": "r.user_id",
    "status": "r.status",
    "start_time": "r.start_time",
    "end_time": "r.end_time",
    "artifact_uri": "r.artifact_uri",
    "lifecycle_stage": "r.lifecycle_stage",
}

_ATTR_ORDER = {
    "run_id": "r.run_id",
    "experiment_id": "r.experiment_id",
    "run_name": "r.run_name",
    "user_id": "r.user_id",
    "status": "r.status",
    "start_time": "r.start_time",
    "end_time": "r.end_time",
    "lifecycle_stage": "r.lifecycle_stage",
}


# Only the three fields documented by MLflow are searchable.  Source,
# source_type, schema and profile live on the dataset entity but are not
# filter scopes; accepting them here would invent a query dialect that real
# clients cannot use.
_DATASET_COLUMNS = {
    "name": "dataset_name",
    "digest": "dataset_digest",
    "context": "context",
}


def _latest_metric_condition(key: str, inner: str) -> str:
    return (
        "m.id = (SELECT m2.id FROM metrics m2 WHERE m2.run_id = m.run_id"
        " AND m2.key = m.key ORDER BY m2.timestamp DESC, m2.id DESC LIMIT 1)"
    )


def _field_predicate(scope: str, key: str, op: str, value: Any) -> tuple[str, list[Any]]:
    op = op.upper()
    if scope == "attributes":
        column = _ATTR_COLUMNS.get(key.lower())
        if column is None:
            raise SearchSyntaxError(f"unknown run attribute {key!r}")
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            negate = "NOT " if op.startswith("NOT ") else ""
            base = op.removeprefix("NOT ")
            if base == "ILIKE":
                return f"LOWER({column}) {negate}LIKE LOWER(?)", [value]
            return f"{column} {negate}LIKE ?", [value]
        if op in {"IN", "NOT IN"}:
            values = list(value)
            placeholders = ",".join("?" for _ in values)
            negate = "NOT " if op == "NOT IN" else ""
            return f"{column} {negate}IN ({placeholders})", values
        return f"{column} {op} ?", [value]

    if scope == "params":
        comparison = op
        cast = ""
        if op in {">", ">=", "<", "<="} and isinstance(value, (int, float)) and not isinstance(value, bool):
            comparison = op
            cast = "CAST(p.value AS REAL)"
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            if op.endswith("ILIKE"):
                expr = "LOWER(p.value) NOT LIKE LOWER(?)" if op.startswith("NOT ") else "LOWER(p.value) LIKE LOWER(?)"
            else:
                expr = "p.value NOT LIKE ?" if op.startswith("NOT ") else "p.value LIKE ?"
            return (
                f"EXISTS (SELECT 1 FROM run_params p WHERE p.run_id = r.run_id AND p.key = ? AND {expr})",
                [key, value],
            )
        if op in {"IN", "NOT IN"}:
            values = list(value)
            placeholders = ",".join("?" for _ in values)
            negate = "NOT " if op == "NOT IN" else ""
            return (
                f"EXISTS (SELECT 1 FROM run_params p WHERE p.run_id = r.run_id AND p.key = ?"
                f" AND p.value {negate}IN ({placeholders}))",
                [key, *values],
            )
        left = cast or "p.value"
        numeric_guard = ""
        if cast:
            # CAST('linear' AS REAL) is 0.0 in SQLite. Require the stored
            # string to look numeric before applying a relational comparison.
            numeric_guard = (
                " AND p.value NOT GLOB '*[^0-9eE+.-]*'"
                " AND p.value GLOB '*[0-9]*'"
            )
        return (
            f"EXISTS (SELECT 1 FROM run_params p WHERE p.run_id = r.run_id AND p.key = ?"
            f"{numeric_guard} AND {left} {comparison} ?)",
            [key, value],
        )

    if scope == "tags":
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            if op.endswith("ILIKE"):
                expr = "LOWER(t.value) NOT LIKE LOWER(?)" if op.startswith("NOT ") else "LOWER(t.value) LIKE LOWER(?)"
            else:
                expr = "t.value NOT LIKE ?" if op.startswith("NOT ") else "t.value LIKE ?"
            return (
                f"EXISTS (SELECT 1 FROM run_tags t WHERE t.run_id = r.run_id AND t.key = ? AND {expr})",
                [key, value],
            )
        if op in {"IN", "NOT IN"}:
            values = list(value)
            placeholders = ",".join("?" for _ in values)
            negate = "NOT " if op == "NOT IN" else ""
            return (
                f"EXISTS (SELECT 1 FROM run_tags t WHERE t.run_id = r.run_id AND t.key = ?"
                f" AND t.value {negate}IN ({placeholders}))",
                [key, *values],
            )
        return (
            f"EXISTS (SELECT 1 FROM run_tags t WHERE t.run_id = r.run_id AND t.key = ? AND t.value {op} ?)",
            [key, value],
        )

    if scope == "datasets":
        column = _DATASET_COLUMNS.get(key.lower())
        if column is None:
            raise SearchSyntaxError(
                f"unknown dataset field {key!r}; searchable fields are "
                "name, digest and context"
            )
        if op in {">", ">=", "<", "<="}:
            raise SearchSyntaxError(
                "dataset fields are strings; use =, !=, IN, LIKE or ILIKE"
            )
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            if op.endswith("ILIKE"):
                expr = (
                    f"LOWER(d.{column}) NOT LIKE LOWER(?)"
                    if op.startswith("NOT ")
                    else f"LOWER(d.{column}) LIKE LOWER(?)"
                )
            else:
                expr = (
                    f"d.{column} NOT LIKE ?"
                    if op.startswith("NOT ")
                    else f"d.{column} LIKE ?"
                )
            return (
                f"EXISTS (SELECT 1 FROM run_inputs d WHERE d.run_id = r.run_id AND {expr})",
                [value],
            )
        if op in {"IN", "NOT IN"}:
            values = list(value)
            placeholders = ",".join("?" for _ in values)
            negate = "NOT " if op == "NOT IN" else ""
            return (
                f"EXISTS (SELECT 1 FROM run_inputs d WHERE d.run_id = r.run_id"
                f" AND d.{column} {negate}IN ({placeholders}))",
                values,
            )
        return (
            f"EXISTS (SELECT 1 FROM run_inputs d WHERE d.run_id = r.run_id"
            f" AND d.{column} {op} ?)",
            [value],
        )

    if scope == "metrics":
        if isinstance(value, bool):
            raise SearchSyntaxError("metric comparisons require numeric literals")
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            raise SearchSyntaxError("LIKE is not supported for metrics")
        if op in {"IN", "NOT IN"}:
            numeric_values: list[float] = []
            for item in value:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise SearchSyntaxError("metric IN values must be numeric")
                numeric_values.append(float(item))
            placeholders = ",".join("?" for _ in numeric_values)
            negate = "NOT " if op == "NOT IN" else ""
            return (
                "EXISTS (SELECT 1 FROM metrics m WHERE m.run_id = r.run_id AND m.key = ?"
                f" AND m.value {negate}IN ({placeholders}) AND {_latest_metric_condition(key, '')})",
                [key, *numeric_values],
            )
        if isinstance(value, (int, float)):
            numeric = float(value)
        else:
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise SearchSyntaxError("metric comparisons require numeric literals") from exc
        return (
            "EXISTS (SELECT 1 FROM metrics m WHERE m.run_id = r.run_id AND m.key = ?"
            f" AND m.value {op} ? AND {_latest_metric_condition(key, '')})",
            [key, numeric],
        )

    raise SearchSyntaxError(f"unsupported scope {scope!r}")


def _compile_node(node: Any) -> tuple[str, list[Any]]:
    if isinstance(node, BoolOp):
        clauses: list[str] = []
        params: list[Any] = []
        joiner = f" {node.op} "
        for child in node.children:
            clause, child_params = _compile_node(child)
            clauses.append(f"({clause})")
            params.extend(child_params)
        return joiner.join(clauses), params
    if isinstance(node, Not):
        clause, params = _compile_node(node.child)
        return f"NOT ({clause})", params
    if isinstance(node, IsNull):
        if node.field.scope == "attributes":
            column = _ATTR_COLUMNS.get(node.field.key.lower())
            if column is None:
                raise SearchSyntaxError(f"unknown run attribute {node.field.key!r}")
            return f"{column} IS {'NOT ' if node.negated else ''}NULL", []
        table, column = (
            ("run_params", "p") if node.field.scope == "params" else
            ("run_tags", "t") if node.field.scope == "tags" else
            ("metrics", "m") if node.field.scope == "metrics" else ("", "")
        )
        if not table:
            raise SearchSyntaxError(f"IS NULL is not supported for {node.field.scope!r}")
        exists = (
            f"EXISTS (SELECT 1 FROM {table} {column} WHERE {column}.run_id = r.run_id"
            f" AND {column}.key = ?)"
        )
        return (exists if node.negated else f"NOT ({exists})"), [node.field.key]
    if isinstance(node, Predicate):
        return _field_predicate(node.field.scope, node.field.key, node.op, node.value)
    raise SearchSyntaxError("invalid filter expression")


def _normalize_attributes(node: Any) -> Any:
    if isinstance(node, Field):
        if node.scope == "attributes":
            return Field("attributes", node.key.lower())
        return node
    if isinstance(node, Predicate):
        return Predicate(_normalize_attributes(node.field), node.op.upper(), node.value)
    if isinstance(node, IsNull):
        return IsNull(_normalize_attributes(node.field), node.negated)
    if isinstance(node, BoolOp):
        return BoolOp(node.op, tuple(_normalize_attributes(child) for child in node.children))
    if isinstance(node, Not):
        return Not(_normalize_attributes(node.child))
    return node


def compile_run_filter(
    expression: str | None, *, include_deleted: bool = False
) -> tuple[str, list[Any]]:
    node = parse_filter(expression)
    clauses = ["r.lifecycle_stage = 'active'"] if not include_deleted else ["1 = 1"]
    params: list[Any] = []
    if node is not None:
        node = _normalize_attributes(node)
        clause, params = _compile_node(node)
        clauses.append(f"({clause})")
    return " AND ".join(clauses), params


def compile_run_order(order_by: Sequence[str] | None) -> tuple[str, list[Any]]:
    """Compile ORDER BY and return its placeholders in left-to-right order."""
    if not order_by:
        return "r.start_time DESC, r.run_id ASC", []
    parts: list[str] = []
    params: list[Any] = []
    for raw in order_by:
        text = str(raw).strip()
        if not text:
            raise SearchSyntaxError("order_by entries must not be empty")
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.\-]*)(?:\s+(ASC|DESC))?", text, re.IGNORECASE)
        if not match:
            raise SearchSyntaxError(f"invalid order_by entry: {raw!r}")
        field_text, direction = match.group(1), (match.group(2) or "ASC").upper()
        if "." not in field_text:
            field = Field("attributes", field_text.lower())
        else:
            scope, key = field_text.split(".", 1)
            field = Field(scope.lower(), key)
        if field.scope == "attributes":
            column = _ATTR_ORDER.get(field.key.lower())
            if column is None:
                raise SearchSyntaxError(f"cannot order by run attribute {field.key!r}")
            parts.append(f"{column} {direction}")
        elif field.scope == "metrics":
            parts.append(
                "(SELECT m.value FROM metrics m WHERE m.run_id = r.run_id AND m.key = ?"
                " ORDER BY m.timestamp DESC, m.id DESC LIMIT 1) "
                f"{direction}"
            )
            params.append(field.key)
        elif field.scope == "params":
            parts.append(
                "(SELECT p.value FROM run_params p WHERE p.run_id = r.run_id AND p.key = ?) "
                f"{direction}"
            )
            params.append(field.key)
        elif field.scope == "tags":
            parts.append(
                "(SELECT t.value FROM run_tags t WHERE t.run_id = r.run_id AND t.key = ?) "
                f"{direction}"
            )
            params.append(field.key)
        else:
            raise SearchSyntaxError(f"cannot order by {field.text!r}")
    parts.append("r.run_id ASC")
    return ", ".join(parts), params


# ---------------------------------------------------------------------------
# Experiment SQL compiler
# ---------------------------------------------------------------------------
_EXPERIMENT_ATTRS = {
    "name": "e.name",
    "experiment_id": "e.experiment_id",
    "lifecycle_stage": "e.lifecycle_stage",
    "creation_time": "e.creation_time",
    "last_update_time": "e.last_update_time",
}
_EXPERIMENT_ORDER = {
    "name": "e.name",
    "experiment_id": "e.experiment_id",
    "creation_time": "e.creation_time",
    "last_update_time": "e.last_update_time",
    "lifecycle_stage": "e.lifecycle_stage",
}


def _compile_experiment_node(node: Any) -> tuple[str, list[Any]]:
    if isinstance(node, BoolOp):
        parts, params = [], []
        for child in node.children:
            clause, child_params = _compile_experiment_node(child)
            parts.append(f"({clause})")
            params.extend(child_params)
        return f" {node.op} ".join(parts), params
    if isinstance(node, Not):
        clause, params = _compile_experiment_node(node.child)
        return f"NOT ({clause})", params
    if isinstance(node, IsNull):
        if node.field.scope == "attributes":
            column = _EXPERIMENT_ATTRS.get(node.field.key.lower())
            if not column:
                raise SearchSyntaxError(f"unknown experiment attribute {node.field.key!r}")
            return f"{column} IS {'NOT ' if node.negated else ''}NULL", []
        if node.field.scope == "tags":
            exists = "EXISTS (SELECT 1 FROM experiment_tags t WHERE t.experiment_id = e.experiment_id AND t.key = ?)"
            return (exists if node.negated else f"NOT ({exists})"), [node.field.key]
        raise SearchSyntaxError(f"unsupported experiment field {node.field.scope!r}")
    if not isinstance(node, Predicate):
        raise SearchSyntaxError("invalid experiment filter")
    field = node.field
    op = node.op.upper()
    if field.scope == "attributes":
        column = _EXPERIMENT_ATTRS.get(field.key.lower())
        if not column:
            raise SearchSyntaxError(f"unknown experiment attribute {field.key!r}")
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            negate = "NOT " if op.startswith("NOT ") else ""
            left = f"LOWER({column})" if op.endswith("ILIKE") else column
            right = "LOWER(?)" if op.endswith("ILIKE") else "?"
            return f"{left} {negate}LIKE {right}", [node.value]
        if op in {"IN", "NOT IN"}:
            placeholders = ",".join("?" for _ in node.value)
            negate = "NOT " if op == "NOT IN" else ""
            return f"{column} {negate}IN ({placeholders})", list(node.value)
        return f"{column} {op} ?", [node.value]
    if field.scope == "tags":
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            negate = "NOT " if op.startswith("NOT ") else ""
            if op.endswith("ILIKE"):
                expr = f"LOWER(t.value) {negate}LIKE LOWER(?)"
            else:
                expr = f"t.value {negate}LIKE ?"
            return (
                f"EXISTS (SELECT 1 FROM experiment_tags t WHERE t.experiment_id = e.experiment_id"
                f" AND t.key = ? AND {expr})",
                [field.key, node.value],
            )
        if op in {"IN", "NOT IN"}:
            placeholders = ",".join("?" for _ in node.value)
            negate = "NOT " if op == "NOT IN" else ""
            return (
                "EXISTS (SELECT 1 FROM experiment_tags t WHERE t.experiment_id = e.experiment_id"
                f" AND t.key = ? AND t.value {negate}IN ({placeholders}))",
                [field.key, *node.value],
            )
        return (
            "EXISTS (SELECT 1 FROM experiment_tags t WHERE t.experiment_id = e.experiment_id"
            f" AND t.key = ? AND t.value {op} ?)",
            [field.key, node.value],
        )
    raise SearchSyntaxError("experiment filters support attributes.* and tags.* only")


def compile_experiment_filter(expression: str | None, *, include_deleted: bool = False) -> tuple[str, list[Any]]:
    node = parse_filter(expression)
    clauses = ["1 = 1"] if include_deleted else ["e.lifecycle_stage = 'active'"]
    params: list[Any] = []
    if node is not None:
        node = _normalize_attributes(node)
        clause, params = _compile_experiment_node(node)
        clauses.append(f"({clause})")
    return " AND ".join(clauses), params


def compile_experiment_order(order_by: Sequence[str] | None) -> str:
    if not order_by:
        return "e.creation_time ASC, e.name ASC"
    parts: list[str] = []
    for raw in order_by:
        text = str(raw).strip()
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.\-]*)(?:\s+(ASC|DESC))?", text, re.IGNORECASE)
        if not match:
            raise SearchSyntaxError(f"invalid order_by entry: {raw!r}")
        name, direction = match.group(1), (match.group(2) or "ASC").upper()
        if "." in name:
            scope, key = name.split(".", 1)
            if scope.lower() != "attributes":
                raise SearchSyntaxError("experiment order_by supports attributes only")
            name = key
        column = _EXPERIMENT_ORDER.get(name.lower())
        if not column:
            raise SearchSyntaxError(f"cannot order experiments by {name!r}")
        parts.append(f"{column} {direction}")
    parts.append("e.experiment_id ASC")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# LoggedModel SQL compiler (MLflow 3)
# ---------------------------------------------------------------------------
# Real ``search_logged_models`` supports bare attributes plus ``params.`` and
# ``metrics.``.  It deliberately does *not* support tag filters and (at the
# time of writing) documents AND-only composition.  Mini keeps those
# restrictions so a filter that works here also works against a real server.
_LOGGED_MODEL_ATTRS = {
    "model_id": "lm.model_id",
    "experiment_id": "lm.experiment_id",
    "name": "lm.name",
    "model_type": "lm.model_type",
    "status": "lm.status",
    "source_run_id": "lm.source_run_id",
    "artifact_location": "lm.artifact_location",
    "creation_time": "lm.creation_timestamp",
    "creation_timestamp": "lm.creation_timestamp",
    "last_updated_time": "lm.last_updated_timestamp",
    "last_updated_timestamp": "lm.last_updated_timestamp",
}

_LOGGED_MODEL_ORDER = {
    "model_id": "lm.model_id",
    "experiment_id": "lm.experiment_id",
    "name": "lm.name",
    "model_type": "lm.model_type",
    "status": "lm.status",
    "source_run_id": "lm.source_run_id",
    "creation_time": "lm.creation_timestamp",
    "creation_timestamp": "lm.creation_timestamp",
    "last_updated_time": "lm.last_updated_timestamp",
    "last_updated_timestamp": "lm.last_updated_timestamp",
}


def _latest_logged_metric_condition() -> str:
    # MLflow 3.18 changed logged-model metric ordering to "highest step,
    # then latest timestamp".  Keep that order so a filter matches the value
    # a UI would show.
    return (
        "lmm.id = (SELECT lmm2.id FROM logged_model_metrics lmm2"
        " WHERE lmm2.model_id = lmm.model_id AND lmm2.key = lmm.key"
        " ORDER BY lmm2.step DESC, lmm2.timestamp DESC, lmm2.id DESC LIMIT 1)"
    )


def _logged_model_status_value(value: Any) -> Any:
    from .entities import normalize_logged_model_status

    return normalize_logged_model_status(value)


_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _check_logged_literal(value: Any) -> Any:
    """Reject literals SQLite cannot bind, instead of surfacing a 500."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not (_INT64_MIN <= value <= _INT64_MAX):
        raise SearchSyntaxError("integer literal is outside the signed 64-bit range")
    return value


def _compile_logged_model_node(node: Any) -> tuple[str, list[Any]]:
    if isinstance(node, BoolOp):
        if node.op != "AND":
            raise SearchSyntaxError("logged-model filters support AND only")
        clauses: list[str] = []
        params: list[Any] = []
        for child in node.children:
            clause, child_params = _compile_logged_model_node(child)
            clauses.append(f"({clause})")
            params.extend(child_params)
        return " AND ".join(clauses), params
    if isinstance(node, Not):
        raise SearchSyntaxError(
            "logged-model filters do not support unary NOT; use !=, NOT IN "
            "or NOT LIKE on a single field"
        )
    if isinstance(node, IsNull):
        if node.field.scope != "attributes":
            raise SearchSyntaxError("IS NULL is supported for logged-model attributes only")
        column = _LOGGED_MODEL_ATTRS.get(node.field.key.lower())
        if column is None:
            raise SearchSyntaxError(f"unknown logged-model attribute {node.field.key!r}")
        return f"{column} IS {'NOT ' if node.negated else ''}NULL", []
    if not isinstance(node, Predicate):
        raise SearchSyntaxError("invalid logged-model filter")

    field = node.field
    op = node.op.upper()
    if field.scope == "tags":
        raise SearchSyntaxError(
            "logged-model search does not support tag filters; "
            "filter by attributes, params.* or metrics.*"
        )
    if field.scope == "attributes":
        column = _LOGGED_MODEL_ATTRS.get(field.key.lower())
        if column is None:
            raise SearchSyntaxError(f"unknown logged-model attribute {field.key!r}")
        value = node.value
        attribute = field.key.lower()
        if attribute in {"name", "status"} and op not in {"=", "!=", "IN", "NOT IN"}:
            raise SearchSyntaxError(
                f"logged-model {attribute} supports =, !=, IN and NOT IN only"
            )
        if op in {"IN", "NOT IN"}:
            value = tuple(_check_logged_literal(item) for item in value)
        else:
            value = _check_logged_literal(value)
        if attribute == "status":
            if op in {"IN", "NOT IN"}:
                value = tuple(_logged_model_status_value(item) for item in value)
            else:
                value = _logged_model_status_value(value)
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            negate = "NOT " if op.startswith("NOT ") else ""
            if op.endswith("ILIKE"):
                return f"LOWER({column}) {negate}LIKE LOWER(?)", [value]
            return f"{column} {negate}LIKE ?", [value]
        if op in {"IN", "NOT IN"}:
            placeholders = ",".join("?" for _ in value)
            negate = "NOT " if op == "NOT IN" else ""
            return f"{column} {negate}IN ({placeholders})", list(value)
        return f"{column} {op} ?", [value]

    if field.scope == "params":
        if op in {">", ">=", "<", "<="}:
            raise SearchSyntaxError("logged-model params are strings; use =, !=, IN or LIKE")
        if op in {"IN", "NOT IN"}:
            if any(not isinstance(item, str) for item in node.value):
                raise SearchSyntaxError(
                    "logged-model param IN values must be quoted strings"
                )
        elif not isinstance(node.value, str):
            raise SearchSyntaxError(
                "logged-model param comparisons require quoted string literals "
                "(values are stored as strings, so 0.1 and '0.1' are not interchangeable)"
            )
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            negate = "NOT " if op.startswith("NOT ") else ""
            if op.endswith("ILIKE"):
                expr = f"LOWER(lmp.value) {negate}LIKE LOWER(?)"
            else:
                expr = f"lmp.value {negate}LIKE ?"
            return (
                "EXISTS (SELECT 1 FROM logged_model_params lmp"
                " WHERE lmp.model_id = lm.model_id AND lmp.key = ? AND "
                + expr
                + ")",
                [field.key, node.value],
            )
        if op in {"IN", "NOT IN"}:
            placeholders = ",".join("?" for _ in node.value)
            negate = "NOT " if op == "NOT IN" else ""
            return (
                "EXISTS (SELECT 1 FROM logged_model_params lmp"
                " WHERE lmp.model_id = lm.model_id AND lmp.key = ?"
                f" AND lmp.value {negate}IN ({placeholders}))",
                [field.key, *node.value],
            )
        return (
            "EXISTS (SELECT 1 FROM logged_model_params lmp"
            " WHERE lmp.model_id = lm.model_id AND lmp.key = ? AND lmp.value "
            + op
            + " ?)",
            [field.key, node.value],
        )

    if field.scope == "metrics":
        if op in {"LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"}:
            raise SearchSyntaxError("LIKE is not supported for logged-model metrics")
        if op in {"IN", "NOT IN"}:
            numeric: list[float] = []
            for item in node.value:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise SearchSyntaxError("logged-model metric IN values must be numeric")
                number = float(item)
                if not math.isfinite(number):
                    raise SearchSyntaxError("logged-model metric values must be finite")
                numeric.append(number)
            placeholders = ",".join("?" for _ in numeric)
            negate = "NOT " if op == "NOT IN" else ""
            return (
                "EXISTS (SELECT 1 FROM logged_model_metrics lmm"
                " WHERE lmm.model_id = lm.model_id AND lmm.key = ?"
                f" AND lmm.value {negate}IN ({placeholders})"
                f" AND {_latest_logged_metric_condition()})",
                [field.key, *numeric],
            )
        if isinstance(node.value, bool):
            raise SearchSyntaxError("logged-model metric comparisons require numeric literals")
        try:
            numeric = float(node.value)
        except (TypeError, ValueError) as exc:
            raise SearchSyntaxError(
                "logged-model metric comparisons require numeric literals"
            ) from exc
        if not math.isfinite(numeric):
            raise SearchSyntaxError("logged-model metric values must be finite")
        return (
            "EXISTS (SELECT 1 FROM logged_model_metrics lmm"
            " WHERE lmm.model_id = lm.model_id AND lmm.key = ?"
            f" AND lmm.value {op} ? AND {_latest_logged_metric_condition()})",
            [field.key, numeric],
        )

    raise SearchSyntaxError(f"unsupported logged-model scope {field.scope!r}")


def compile_logged_model_filter(expression: str | None) -> tuple[str, list[Any]]:
    node = parse_filter(expression)
    if node is None:
        return "1 = 1", []
    return _compile_logged_model_node(node)


def compile_logged_model_order(order_by: Sequence[str] | None) -> tuple[str, list[Any]]:
    if not order_by:
        return "lm.creation_timestamp DESC, lm.model_id ASC", []
    parts: list[str] = []
    params: list[Any] = []
    for raw in order_by:
        text = str(raw).strip()
        match = re.fullmatch(
            r"([A-Za-z_][A-Za-z0-9_.\-]*)(?:\s+(ASC|DESC))?", text, re.IGNORECASE
        )
        if not match:
            raise SearchSyntaxError(f"invalid order_by entry: {raw!r}")
        field_text, direction = match.group(1), (match.group(2) or "ASC").upper()
        if "." not in field_text:
            field = Field("attributes", field_text.lower())
        else:
            scope, key = field_text.split(".", 1)
            field = Field(scope.lower(), key)
        if field.scope == "attributes":
            column = _LOGGED_MODEL_ORDER.get(field.key.lower())
            if column is None:
                raise SearchSyntaxError(
                    f"cannot order logged models by attribute {field.key!r}"
                )
            parts.append(f"{column} {direction}")
        elif field.scope == "metrics":
            parts.append(
                "(SELECT lmm.value FROM logged_model_metrics lmm"
                " WHERE lmm.model_id = lm.model_id AND lmm.key = ?"
                " ORDER BY lmm.step DESC, lmm.timestamp DESC, lmm.id DESC LIMIT 1) "
                f"{direction}"
            )
            params.append(field.key)
        elif field.scope == "params":
            parts.append(
                "(SELECT lmp.value FROM logged_model_params lmp"
                " WHERE lmp.model_id = lm.model_id AND lmp.key = ?) "
                f"{direction}"
            )
            params.append(field.key)
        else:
            raise SearchSyntaxError(f"cannot order logged models by {field.text!r}")
    parts.append("lm.model_id ASC")
    return ", ".join(parts), params


# ---------------------------------------------------------------------------
# In-memory evaluator for registry-like entities
# ---------------------------------------------------------------------------
def _like_to_regex(pattern: str, flags: int = 0) -> re.Pattern[str]:
    pieces = ["^"]
    for char in pattern:
        if char == "%":
            pieces.append(".*")
        elif char == "_":
            pieces.append(".")
        else:
            pieces.append(re.escape(char))
    pieces.append("$")
    return re.compile("".join(pieces), re.DOTALL | flags)


def _compare(left: Any, op: str, right: Any) -> bool:
    if op in {"=", "!="}:
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            equal = float(left) == float(right)
        else:
            equal = str(left) == str(right)
        return equal if op == "=" else not equal
    if left is None:
        return False
    if op in {">", ">=", "<", "<="}:
        try:
            left_value, right_value = float(left), float(right)
        except (TypeError, ValueError):
            left_value, right_value = str(left), str(right)
        return {
            ">": left_value > right_value,
            ">=": left_value >= right_value,
            "<": left_value < right_value,
            "<=": left_value <= right_value,
        }[op]
    raise SearchSyntaxError(f"unsupported operator {op!r}")


def evaluate_ast(node: Any, resolve: Callable[[Field], Any]) -> bool:
    if isinstance(node, BoolOp):
        values = (evaluate_ast(child, resolve) for child in node.children)
        return all(values) if node.op == "AND" else any(values)
    if isinstance(node, Not):
        return not evaluate_ast(node.child, resolve)
    if isinstance(node, IsNull):
        value = resolve(node.field)
        return (value is not None) if node.negated else (value is None)
    if not isinstance(node, Predicate):
        raise SearchSyntaxError("invalid filter AST")
    left = resolve(node.field)
    op = node.op.upper()
    right = node.value
    if left is None:
        # SQL EXISTS-based filtering only matches when the key exists. In
        # particular, a missing tag is not "!= x" and is not "NOT IN (...)".
        return False
    if op == "LIKE":
        return bool(_like_to_regex(str(right), re.IGNORECASE).match(str(left)))
    if op == "ILIKE":
        return bool(_like_to_regex(str(right), re.IGNORECASE).match(str(left)))
    if op == "NOT LIKE":
        return not bool(_like_to_regex(str(right), re.IGNORECASE).match(str(left)))
    if op == "NOT ILIKE":
        return not bool(_like_to_regex(str(right), re.IGNORECASE).match(str(left)))
    if op in {"IN", "NOT IN"}:
        present = any(_compare(left, "=", item) for item in right)
        return present if op == "IN" else not present
    return _compare(left, op, right)
