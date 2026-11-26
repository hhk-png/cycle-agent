"""Recursive-descent PromQL parser with type checking."""

from __future__ import annotations

from ..labels import METRIC_NAME_LABEL, LabelError, Matcher
from ..model import ParseError
from . import ast
from .functions import FUNCTIONS, INSTANT, RANGE, SCALAR, STRING, FunctionSpec
from .lexer import Token, TokenKind, tokenize

PRECEDENCE = {
    "or": 1,
    "and": 2,
    "unless": 2,
    "==": 3, "!=": 3, "<=": 3, "<": 3, ">=": 3, ">": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5, "%": 5, "atan2": 5,
    "^": 6,
}
COMPARISON_OPS = {"==", "!=", "<=", "<", ">=", ">"}
SET_OPS = {"and", "or", "unless"}
ARITHMETIC_OPS = {"+", "-", "*", "/", "%", "^", "atan2"}
AGGREGATORS = ast.AGGREGATORS
PARAM_AGGREGATORS = ast.PARAM_AGGREGATORS


class Parser:
    def __init__(self, text: str) -> None:
        if not text or not text.strip():
            raise ParseError("empty query")
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    # ---- token helpers -------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        index = self.pos + offset
        if index >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[index]

    def next(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def accept(self, kind: TokenKind, text: str | None = None) -> Token | None:
        token = self.peek()
        if token.kind == kind and (text is None or token.text == text):
            return self.next()
        return None

    def accept_ident(self, text: str) -> Token | None:
        token = self.peek()
        if token.kind == TokenKind.IDENT and token.text == text:
            return self.next()
        return None

    def expect(self, kind: TokenKind, text: str | None = None) -> Token:
        token = self.peek()
        if token.kind != kind or (text is not None and token.text != text):
            expected = text or kind.value
            raise ParseError(
                f"expected {expected!r}, got {token.text!r}", token.position
            )
        return self.next()

    # ---- entry ---------------------------------------------------------
    def parse(self) -> ast.Expr:
        expr = self.parse_expr(0)
        if self.peek().kind != TokenKind.EOF:
            token = self.peek()
            raise ParseError(f"unexpected token {token.text!r}", token.position)
        return expr

    def parse_expr(self, min_precedence: int) -> ast.Expr:
        lhs = self.parse_unary()
        while True:
            token = self.peek()
            op = token.text if token.kind in (TokenKind.OPERATOR, TokenKind.IDENT) else ""
            precedence = PRECEDENCE.get(op)
            if precedence is None or precedence < min_precedence:
                break
            position = token.position
            self.next()
            matching, bool_modifier = self.parse_binary_modifiers(op)
            next_min = precedence if op == "^" else precedence + 1
            rhs = self.parse_expr(next_min)
            lhs = self.build_binary(op, lhs, rhs, matching, position, bool_modifier)
        return lhs

    def parse_unary(self) -> ast.Expr:
        token = self.peek()
        if token.kind == TokenKind.OPERATOR and token.text in ("+", "-"):
            self.next()
            operand = self.parse_expr(6)
            if operand.value_type not in (SCALAR, INSTANT):
                raise ParseError(
                    f"unary {token.text!r} is only allowed on scalars and instant vectors",
                    token.position,
                )
            return ast.UnaryExpr(token.text, operand, token.position)
        return self.parse_postfix(self.parse_primary())

    # ---- primaries -----------------------------------------------------
    def parse_primary(self) -> ast.Expr:
        token = self.peek()
        if token.kind == TokenKind.NUMBER:
            self.next()
            return ast.NumberLiteral(float(token.value), token.position)
        if token.kind == TokenKind.STRING:
            self.next()
            return ast.StringLiteral(str(token.value), token.position)
        if token.kind == TokenKind.LPAREN:
            self.next()
            expr = self.parse_expr(0)
            self.expect(TokenKind.RPAREN)
            return ast.ParenExpr(expr, token.position)
        if token.kind == TokenKind.LBRACE:
            return self.parse_selector("", token.position)
        if token.kind == TokenKind.IDENT:
            name = token.text
            following = self.peek(1)
            if name in AGGREGATORS and (
                following.kind == TokenKind.LPAREN
                or (
                    following.kind == TokenKind.IDENT
                    and following.text in ("by", "without")
                )
            ):
                return self.parse_aggregation()
            if (
                name in ("by", "without")
                and following.kind == TokenKind.LPAREN
            ):
                raise ParseError(f"unexpected grouping keyword {name!r}", token.position)
            if following.kind == TokenKind.LPAREN:
                return self.parse_call()
            self.next()
            if self.peek().kind == TokenKind.LBRACE:
                return self.parse_selector(name, token.position)
            return self._new_selector(name, [], token.position)
        raise ParseError(f"unexpected token {token.text!r}", token.position)

    def parse_selector(self, name: str, position: int) -> ast.VectorSelector:
        self.expect(TokenKind.LBRACE)
        matchers: list[Matcher] = []
        quoted_name = ""
        saw_item = False
        while True:
            if self.accept(TokenKind.RBRACE):
                break
            token = self.peek()
            if token.kind == TokenKind.STRING:
                following = self.peek(1)
                if following.kind == TokenKind.OPERATOR:
                    # Prometheus 3: a quoted label name, e.g.
                    # {"http.server.duration", "service.name"="api"}.
                    label_name = str(self.next().value)
                    if not label_name:
                        raise ParseError("label name must not be empty", token.position)
                else:
                    # A quoted metric name is only allowed as the first item
                    # and is written without an operator.
                    if saw_item or quoted_name or name:
                        raise ParseError(
                            "metric name must be the first element of the selector",
                            token.position,
                        )
                    quoted_name = str(self.next().value)
                    if not quoted_name:
                        raise ParseError("metric name must not be empty", token.position)
                    saw_item = True
                    if self.accept(TokenKind.COMMA):
                        continue
                    self.expect(TokenKind.RBRACE)
                    break
            elif token.kind == TokenKind.IDENT:
                label_name = self.next().text
            else:
                raise ParseError(
                    f"expected label name, got {token.text!r}", token.position
                )
            op_token = self.expect(TokenKind.OPERATOR)
            if op_token.text not in ("=", "!=", "=~", "!~"):
                raise ParseError(
                    f"invalid label matching operator {op_token.text!r}",
                    op_token.position,
                )
            value_token = self.expect(TokenKind.STRING)
            try:
                matcher = Matcher(label_name, op_token.text, str(value_token.value))
            except LabelError as exc:
                raise ParseError(str(exc), value_token.position) from exc
            matchers.append(matcher)
            saw_item = True
            if label_name == METRIC_NAME_LABEL and op_token.text == "=":
                if name and name != matcher.value:
                    raise ParseError(
                        "metric name must not be set twice", value_token.position
                    )
                name = matcher.value
            if self.accept(TokenKind.COMMA):
                continue
            self.expect(TokenKind.RBRACE)
            break
        effective_name = name or quoted_name
        if quoted_name and name:
            raise ParseError("metric name specified more than once", position)
        return self._new_selector(effective_name, matchers, position)

    def _new_selector(
        self, name: str, matchers: list[Matcher], position: int
    ) -> ast.VectorSelector:
        all_matchers = list(matchers)
        if name:
            existing = [
                m for m in all_matchers
                if m.name == METRIC_NAME_LABEL and m.op == "="
            ]
            if existing:
                if existing[0].value != name:
                    raise ParseError("metric name must not be set twice", position)
            else:
                all_matchers.append(Matcher(METRIC_NAME_LABEL, "=", name))
        if not all_matchers:
            raise ParseError(
                "vector selector must contain at least one non-empty matcher",
                position,
            )
        if not any(not matcher.matches("") for matcher in all_matchers):
            raise ParseError(
                "vector selector must contain at least one non-empty matcher",
                position,
            )
        return ast.VectorSelector(name, all_matchers, position=position)

    # ---- postfix: range, offset, @ -------------------------------------
    def parse_postfix(self, node: ast.Expr) -> ast.Expr:
        while True:
            token = self.peek()
            if token.kind == TokenKind.LBRACKET:
                node = self._parse_bracket(node)
                continue
            if token.kind == TokenKind.IDENT and token.text == "offset":
                self.next()
                sign = 1
                if self.peek().kind == TokenKind.OPERATOR and self.peek().text == "-":
                    self.next()
                    sign = -1
                duration = self.expect(TokenKind.DURATION)
                offset = sign * int(duration.value)
                target = self._offset_target(node)
                if target is None:
                    raise ParseError(
                        "offset modifier must be preceded by an instant vector selector "
                        "or a subquery",
                        token.position,
                    )
                target.offset_ms += offset
                continue
            if token.kind == TokenKind.AT:
                self.next()
                at = self._parse_at(token.position)
                target = self._offset_target(node)
                if target is None:
                    raise ParseError(
                        "@ modifier must be preceded by an instant vector selector "
                        "or a subquery",
                        token.position,
                    )
                target.at = at
                continue
            break
        return node

    def _offset_target(self, node: ast.Expr):
        if isinstance(node, ast.VectorSelector):
            return node
        if isinstance(node, ast.SubqueryExpr):
            return node
        if isinstance(node, ast.MatrixSelector):
            return node.vector
        return None

    def _parse_bracket(self, node: ast.Expr) -> ast.Expr:
        start = self.expect(TokenKind.LBRACKET)
        range_token = self.expect(TokenKind.DURATION)
        range_ms = int(range_token.value)
        if self.peek().kind == TokenKind.COLON:
            self.next()
            step_ms = None
            if self.peek().kind == TokenKind.DURATION:
                step_ms = int(self.next().value)
            self.expect(TokenKind.RBRACKET)
            if range_ms <= 0:
                raise ParseError("subquery range must be positive", start.position)
            if step_ms is not None and step_ms <= 0:
                raise ParseError("subquery step must be positive", start.position)
            if step_ms is not None and step_ms > range_ms:
                raise ParseError(
                    "subquery step must not be larger than the range", start.position
                )
            if node.value_type != INSTANT:
                raise ParseError(
                    "subquery is only allowed on instant vector expressions",
                    start.position,
                )
            return ast.SubqueryExpr(
                node, range_ms, step_ms, position=start.position
            )
        self.expect(TokenKind.RBRACKET)
        if range_ms <= 0:
            raise ParseError("range must be positive", start.position)
        if not isinstance(node, ast.VectorSelector):
            raise ParseError(
                "ranges are only allowed for vector selectors", start.position
            )
        return ast.MatrixSelector(node, range_ms, start.position)

    def _parse_at(self, position: int) -> ast.AtModifier:
        token = self.peek()
        if token.kind == TokenKind.NUMBER:
            self.next()
            return ast.AtModifier("timestamp", int(round(float(token.value) * 1000)))
        if token.kind == TokenKind.OPERATOR and token.text == "-" and self.peek(1).kind == TokenKind.NUMBER:
            self.next()
            number = self.next()
            return ast.AtModifier("timestamp", int(round(float(number.value) * 1000)))
        if token.kind == TokenKind.IDENT and token.text in ("start", "end"):
            self.next()
            self.expect(TokenKind.LPAREN)
            self.expect(TokenKind.RPAREN)
            return ast.AtModifier(token.text)
        raise ParseError(
            f"expected timestamp after '@', got {token.text!r}", position
        )

    # ---- aggregation ---------------------------------------------------
    def parse_aggregation(self) -> ast.AggregateExpr:
        name_token = self.next()
        op = name_token.text
        grouping: list[str] = []
        without = False
        grouped_before = False
        if self.peek().kind == TokenKind.IDENT and self.peek().text in ("by", "without"):
            grouping, without = self.parse_grouping()
            grouped_before = True
        self.expect(TokenKind.LPAREN)
        param: ast.Expr | None = None
        if op in PARAM_AGGREGATORS:
            param = self.parse_expr(0)
            if param.value_type != SCALAR:
                raise ParseError(
                    f"{op} parameter must be a scalar", param.position
                )
            self.expect(TokenKind.COMMA)
            expr = self.parse_expr(0)
        elif op == "count_values":
            param = self.parse_expr(0)
            if param.value_type != STRING:
                raise ParseError("count_values parameter must be a string", param.position)
            self.expect(TokenKind.COMMA)
            expr = self.parse_expr(0)
        else:
            expr = self.parse_expr(0)
            if self.peek().kind == TokenKind.COMMA:
                raise ParseError(
                    f"aggregation {op!r} takes exactly one argument",
                    self.peek().position,
                )
        self.expect(TokenKind.RPAREN)
        if not grouped_before and self.peek().kind == TokenKind.IDENT and self.peek().text in ("by", "without"):
            grouping, without = self.parse_grouping()
        if expr.value_type != INSTANT:
            raise ParseError(
                f"aggregation {op!r} requires an instant vector", expr.position
            )
        if len(set(grouping)) != len(grouping):
            raise ParseError("duplicate label in grouping", name_token.position)
        return ast.AggregateExpr(
            op, expr, param, grouping, without, name_token.position
        )

    def parse_grouping(self) -> tuple[list[str], bool]:
        keyword = self.next().text
        self.expect(TokenKind.LPAREN)
        labels: list[str] = []
        while not self.accept(TokenKind.RPAREN):
            token = self.peek()
            if token.kind == TokenKind.STRING:
                # Prometheus 3 allows quoted grouping labels, e.g.
                # sum by ("k8s.pod.name") (...).
                name = str(self.next().value)
                if not name:
                    raise ParseError("grouping label must not be empty", token.position)
            else:
                name = self.expect(TokenKind.IDENT).text
            labels.append(name)
            if self.accept(TokenKind.COMMA):
                continue
            self.expect(TokenKind.RPAREN)
            break
        return labels, keyword == "without"

    # ---- function calls ------------------------------------------------
    def parse_call(self) -> ast.Call:
        name_token = self.next()
        name = name_token.text
        spec = FUNCTIONS.get(name)
        if spec is None:
            raise ParseError(f"unknown function {name!r}", name_token.position)
        self.expect(TokenKind.LPAREN)
        args: list[ast.Expr] = []
        while not self.accept(TokenKind.RPAREN):
            args.append(self.parse_expr(0))
            if self.accept(TokenKind.COMMA):
                if self.peek().kind == TokenKind.RPAREN:
                    self.next()
                    break
                continue
            self.expect(TokenKind.RPAREN)
            break
        self._check_call(spec, args, name_token)
        return ast.Call(name, args, name_token.position, value_type=spec.return_type)

    def _check_call(self, spec: FunctionSpec, args: list[ast.Expr], token: Token) -> None:
        if len(args) < spec.min_args:
            raise ParseError(
                f"function {spec.name!r} expects at least {spec.min_args} argument(s), "
                f"got {len(args)}",
                token.position,
            )
        if len(args) > spec.max_args:
            raise ParseError(
                f"function {spec.name!r} expects at most {spec.max_args} argument(s), "
                f"got {len(args)}",
                token.position,
            )
        for index, arg in enumerate(args):
            expected = spec.expected_type(index)
            if arg.value_type != expected:
                raise ParseError(
                    f"function {spec.name!r} argument {index + 1} must be "
                    f"{expected}, got {arg.value_type}",
                    arg.position,
                )

    # ---- binary --------------------------------------------------------
    def parse_binary_modifiers(self, op: str) -> tuple[ast.VectorMatching, bool]:
        matching = ast.VectorMatching()
        bool_modifier = False
        seen_on = seen_ignoring = seen_group = False
        while True:
            token = self.peek()
            if token.kind != TokenKind.IDENT:
                break
            if token.text == "bool":
                if op not in COMPARISON_OPS:
                    raise ParseError(
                        "bool modifier can only be used on comparison operators",
                        token.position,
                    )
                if bool_modifier:
                    raise ParseError("bool modifier specified twice", token.position)
                if seen_on or seen_ignoring or seen_group:
                    raise ParseError(
                        "bool modifier must be specified before on/ignoring and group modifiers",
                        token.position,
                    )
                self.next()
                bool_modifier = True
                continue
            if token.text in ("on", "ignoring"):
                if seen_on or seen_ignoring:
                    raise ParseError("label matching specified twice", token.position)
                if seen_group:
                    raise ParseError(
                        "on/ignoring must be specified before group modifiers",
                        token.position,
                    )
                self.next()
                labels = self._parse_label_list()
                matching.on = token.text == "on"
                matching.labels = labels
                seen_on = seen_ignoring = True
                continue
            if token.text in ("group_left", "group_right"):
                if seen_group:
                    raise ParseError("group modifier specified twice", token.position)
                if op in SET_OPS:
                    raise ParseError(
                        "grouping modifiers are not allowed for set operators",
                        token.position,
                    )
                self.next()
                include: list[str] = []
                if self.peek().kind == TokenKind.LPAREN:
                    include = self._parse_label_list()
                matching.card = "many-to-one" if token.text == "group_left" else "one-to-many"
                matching.include = include
                seen_group = True
                continue
            break
        return matching, bool_modifier

    def _parse_label_list(self) -> list[str]:
        self.expect(TokenKind.LPAREN)
        labels: list[str] = []
        while not self.accept(TokenKind.RPAREN):
            token = self.peek()
            if token.kind == TokenKind.STRING:
                name = str(self.next().value)
                if not name:
                    raise ParseError(
                        "matching label must not be empty", token.position
                    )
            else:
                name = self.expect(TokenKind.IDENT).text
            labels.append(name)
            if self.accept(TokenKind.COMMA):
                continue
            self.expect(TokenKind.RPAREN)
            break
        if len(set(labels)) != len(labels):
            raise ParseError("duplicate label in matching clause", self.peek().position)
        return labels

    def build_binary(
        self,
        op: str,
        lhs: ast.Expr,
        rhs: ast.Expr,
        matching: ast.VectorMatching,
        position: int,
        bool_modifier: bool = False,
    ) -> ast.BinaryExpr:
        if lhs.value_type in ("matrix", STRING) or rhs.value_type in ("matrix", STRING):
            raise ParseError(
                f"binary operator {op!r} is not allowed on {lhs.value_type} and "
                f"{rhs.value_type}",
                position,
            )
        if op in SET_OPS:
            if matching.card != "one-to-one":
                raise ParseError(
                    "group modifiers are not allowed for set operators", position
                )
            if lhs.value_type != INSTANT or rhs.value_type != INSTANT:
                raise ParseError("set operators require instant vectors", position)
            value_type = INSTANT
        elif lhs.value_type == SCALAR and rhs.value_type == SCALAR:
            if op in COMPARISON_OPS and not bool_modifier:
                raise ParseError(
                    "comparisons between scalars must use the bool modifier", position
                )
            value_type = SCALAR
        else:
            if matching.card != "one-to-one" and not (
                lhs.value_type == INSTANT and rhs.value_type == INSTANT
            ):
                raise ParseError(
                    "group modifiers require instant vectors on both sides", position
                )
            if matching.include and matching.card == "one-to-one":
                raise ParseError(
                    "grouping labels require group_left or group_right", position
                )
            if matching.on and any(label in matching.labels for label in matching.include):
                raise ParseError(
                    "label present in both on() and group_left()/group_right()",
                    position,
                )
            if bool_modifier and op not in COMPARISON_OPS:
                raise ParseError("bool modifier requires a comparison operator", position)
            value_type = INSTANT
        return ast.BinaryExpr(
            op, lhs, rhs, bool_modifier, matching, position, value_type=value_type
        )


def parse_expr(text: str) -> ast.Expr:
    return Parser(text).parse()


def parse_duration_literal(text: str) -> int:
    from ..util.timeutil import parse_duration

    return parse_duration(text)
