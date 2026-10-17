"""手写的 SQL 表达式解析器：分词器 + 递归下降。

**为什么不用 ``ast.literal_eval`` / ``eval``?**

``.where("a > 1")`` 收进来的是**不可信的字符串**。用 ``eval`` 就是把
远程代码执行直接暴露给调用方；用 Python 的 ``ast`` 模块解析 SQL 更不成立 ——
SQL 的 ``AND`` / ``LIKE`` / ``::`` 在 Python 语法里根本不存在。
真实 LanceDB 用的是 Rust 的 DataFusion SQL 解析器（``sqlparser`` crate），
本模块用同一个思路复刻它的表达式子集：**先把字符串切成 token，
再用递归下降按运算符优先级建树**。

**为什么递归下降而不是算符优先（shunting-yard）?**

因为 SQL 的表达式语法不是纯运算符语法：``IN`` 后面要跟括号列表、
``LIKE`` 后面可以跟 ``ESCAPE``、``BETWEEN`` 中间夹着 ``AND``、
``CAST(x AS t)`` 里 ``AS`` 的位置不能错。递归下降可以给每一层配
人话报错（"这里期待 ``)``，实际读到 ``AND``"），而算符优先表做不到。

## 优先级（低 → 高）

::

    OR
    AND
    NOT              (前缀，右结合)
    比较 = != <> < <= > >=  LIKE  IN  IS  BETWEEN
    + - ||
    * / %
    一元 - +         (前缀，右结合)
    ::               (后缀，向前看绑定最紧)
    基本表达式

关键点（和 SQL 标准一致，与 C 不同）：

* ``NOT a = 1`` 是 ``NOT (a = 1)``，不是 ``(NOT a) = 1``；
* ``a AND b = c`` 是 ``a AND (b = c)``；
* ``a OR b AND c`` 是 ``a OR (b AND c)``；
* ``-a :: int`` 是 ``-(a :: int)``：``::`` 比一元负号更紧。

## 报错位置

每个 token 都带 ``pos``（在原始字符串里的字符下标）。出错时抛
:class:`~mini_lancedb.errors.ExpressionSyntaxError`，它会把
``position`` 渲染成一行插入符::

    ExpressionSyntaxError: 这里期待 ')'
      a = (1 + 2
               ^

位置信息**不是**锦上添花：``.where()`` 的报错质量基本决定了这个 API
好不好用，所以宁可多写几行代码也要把 ``pos`` 一路传下去。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import ExpressionSyntaxError, PlanError, SchemaError
from ..schema import Schema
from ..types import (
    BINARY,
    BOOL,
    DataType,
    FLOAT64,
    INT64,
    NULL as NULL_TYPE,
    UTF8,
    parse_type,
)
from .nodes import (
    Between,
    BinaryOp,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    IsIn,
    IsNull,
    Like,
    Literal,
    UnaryOp,
)

__all__ = [
    "Token",
    "tokenize",
    "parse",
    "parse_predicate",
    "parse_type_name",
    "FUNCTIONS",
    "is_function_name",
]


# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------

#: 关键字（大小写不敏感）。分词时统一升成大写并存进 ``Token.value``。
KEYWORDS = frozenset(
    {
        "AND", "OR", "NOT", "IN", "LIKE", "ILIKE", "IS", "NULL", "TRUE",
        "FALSE", "BETWEEN", "ESCAPE", "AS", "CAST",
    }
)

#: 裸写成标识符时会被当成这几个名字的特殊常量。DataFusion 也认它们
#: （``SELECT `` 里可以直接写 ``nan``），而 PostgreSQL 用 ``'NaN'::float``。
_SPECIAL_FLOATS: Dict[str, float] = {
    "nan": float("nan"),
    "inf": float("inf"),
    "infinity": float("inf"),
}

#: 多字符运算符必须**按长度倒序**匹配，否则 ``<=`` 会被吃成 ``<`` + ``=``。
_OPERATORS: Tuple[str, ...] = (
    "::", "||", "<=", ">=", "<>", "!=", "=", "<", ">",
    "+", "-", "*", "/", "%", "(", ")", "[", "]", ",", ".",
)

_NUMBER_RE = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_HEX_RE = re.compile(r"[0-9a-fA-F]*")


class Token:
    """一个词法单元。

    ``kind`` ∈ ``{ident, keyword, string, bytes, number, op, eof}``。
    ``pos`` 是它在原串里的起始下标 —— 报错插入符就是靠它对齐的。
    """

    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Token({self.kind}, {self.value!r}, @{self.pos})"

    def is_op(self, *symbols: str) -> bool:
        return self.kind == "op" and self.value in symbols

    def is_kw(self, *words: str) -> bool:
        return self.kind == "keyword" and self.value in words


def tokenize(sql: str) -> List[Token]:
    """把表达式字符串切成 token 列表（末尾一定补一个 ``eof``）。"""

    if not isinstance(sql, str):
        raise ExpressionSyntaxError("表达式必须是字符串", expression=str(sql))
    out: List[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        # -- 空白 ---------------------------------------------------------
        if ch.isspace():
            i += 1
            continue

        # -- 字符串字面量：单引号，'' 表示一个真引号 ------------------------
        if ch == "'":
            start = i
            i += 1
            buf: List[str] = []
            while True:
                if i >= n:
                    raise ExpressionSyntaxError(
                        "字符串字面量没有闭合（缺少结尾的单引号）",
                        expression=sql,
                        position=start,
                    )
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            out.append(Token("string", "".join(buf), start))
            continue

        # -- 引号标识符："" 和 `` 两种风格，内部引号翻倍转义 ----------------
        if ch in ('"', "`"):
            start = i
            quote = ch
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise ExpressionSyntaxError(
                        f"标识符没有闭合（缺少结尾的 {quote}）",
                        expression=sql,
                        position=start,
                    )
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        buf.append(quote)
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            out.append(Token("ident", "".join(buf), start))
            continue

        # -- 数字 ---------------------------------------------------------
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = _NUMBER_RE.match(sql, i)
            assert m is not None  # 上面的判断保证了必然匹配
            text = m.group(0)
            i = m.end()
            if "." in text or "e" in text or "E" in text:
                out.append(Token("number", float(text), m.start()))
            else:
                out.append(Token("number", int(text), m.start()))
            continue

        # -- 标识符 / 关键字 / X'..' 十六进制字面量 -------------------------
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i]
            if word.lower() == "x" and i < n and sql[i] == "'":
                hex_start = i
                i += 1
                digits = []
                while i < n and sql[i] != "'":
                    digits.append(sql[i])
                    i += 1
                if i >= n:
                    raise ExpressionSyntaxError(
                        "X'..' 字面量没有闭合",
                        expression=sql,
                        position=hex_start,
                    )
                i += 1
                raw = "".join(digits)
                if _HEX_RE.fullmatch(raw) is None:
                    raise ExpressionSyntaxError(
                        "X'..' 里出现了非十六进制字符",
                        expression=sql,
                        position=hex_start + 1,
                    )
                if len(raw) % 2:
                    raise ExpressionSyntaxError(
                        "X'..' 的十六进制位数必须是偶数",
                        expression=sql,
                        position=hex_start + 1,
                    )
                out.append(Token("bytes", bytes.fromhex(raw), start))
                continue
            upper = word.upper()
            if upper in KEYWORDS:
                out.append(Token("keyword", upper, start))
            else:
                out.append(Token("ident", word, start))
            continue

        # -- 运算符 -------------------------------------------------------
        for sym in _OPERATORS:
            if sql.startswith(sym, i):
                out.append(Token("op", sym, i))
                i += len(sym)
                break
        else:
            raise ExpressionSyntaxError(
                f"无法识别的字符 {ch!r}",
                expression=sql,
                position=i,
            )

    out.append(Token("eof", None, len(sql)))
    return out


# ---------------------------------------------------------------------------
# 函数表
# ---------------------------------------------------------------------------


class _FuncSpec:
    """标量函数的签名：名字、参数个数范围、返回类型规则。"""

    __slots__ = ("name", "min_args", "max_args", "kind")

    def __init__(self, name: str, min_args: int, max_args: int, kind: str) -> None:
        self.name = name
        self.min_args = min_args
        self.max_args = max_args
        self.kind = kind

    def returns(self, arg_types: Sequence[DataType]) -> DataType:
        if self.kind == "bool":
            return BOOL
        if self.kind == "int":
            return INT64
        if self.kind == "utf8":
            return UTF8
        if self.kind == "float":
            return FLOAT64
        if self.kind == "same":
            return arg_types[0] if arg_types else NULL_TYPE
        if self.kind == "coalesce":
            from ..types import promote_types

            acc: Optional[DataType] = None
            for t in arg_types:
                acc = t if acc is None else (promote_types(acc, t) or acc)
            return acc or NULL_TYPE
        return NULL_TYPE


_VARARGS = 64


def _spec(name: str, lo: int, hi: int, kind: str) -> Tuple[str, _FuncSpec]:
    return name, _FuncSpec(name, lo, hi, kind)


#: 支持的标量函数。这张表同时是**白名单** —— 表里没有的名字会被当成
#: 语法错误拒绝掉，而不是"运行时再报未知函数"。
FUNCTIONS: Dict[str, _FuncSpec] = dict(
    [
        # 数组
        _spec("array_contains", 2, 2, "bool"),
        _spec("array_has_any", 2, _VARARGS, "bool"),
        _spec("array_has_all", 2, _VARARGS, "bool"),
        _spec("array_length", 1, 1, "int"),
        _spec("list_extract", 2, 2, "same"),
        # 字符串
        _spec("length", 1, 1, "int"),
        _spec("char_length", 1, 1, "int"),
        _spec("character_length", 1, 1, "int"),
        _spec("lower", 1, 1, "utf8"),
        _spec("upper", 1, 1, "utf8"),
        _spec("trim", 1, 2, "utf8"),
        _spec("ltrim", 1, 2, "utf8"),
        _spec("rtrim", 1, 2, "utf8"),
        _spec("starts_with", 2, 2, "bool"),
        _spec("ends_with", 2, 2, "bool"),
        _spec("contains", 2, 2, "bool"),
        _spec("concat", 1, _VARARGS, "utf8"),
        _spec("substring", 2, 3, "utf8"),
        _spec("substr", 2, 3, "utf8"),
        _spec("strpos", 2, 2, "int"),
        _spec("replace", 3, 3, "utf8"),
        # 数值
        _spec("abs", 1, 1, "same"),
        _spec("round", 1, 2, "float"),
        _spec("floor", 1, 1, "float"),
        _spec("ceil", 1, 1, "float"),
        _spec("ceiling", 1, 1, "float"),
        _spec("sqrt", 1, 1, "float"),
        _spec("power", 2, 2, "float"),
        _spec("greatest", 1, _VARARGS, "coalesce"),
        _spec("least", 1, _VARARGS, "coalesce"),
        # 空值 / 结构体
        _spec("coalesce", 1, _VARARGS, "coalesce"),
        _spec("nullif", 2, 2, "same"),
        _spec("struct_field", 2, 2, "same"),
    ]
)


def is_function_name(name: str) -> bool:
    return name.lower() in FUNCTIONS


# ---------------------------------------------------------------------------
# 类型名（CAST(x AS <t>) 与 x::<t>）
# ---------------------------------------------------------------------------

#: SQL 方言的类型别名 → 本库的类型名。
_TYPE_ALIASES: Dict[str, str] = {
    "bool": "bool",
    "boolean": "bool",
    "tinyint": "int8",
    "int1": "int8",
    "smallint": "int16",
    "int2": "int16",
    "int": "int32",
    "integer": "int32",
    "bigint": "int64",
    "int8": "int8",
    "int16": "int16",
    "int32": "int32",
    "int64": "int64",
    "uint8": "uint8",
    "uint16": "uint16",
    "uint32": "uint32",
    "uint64": "uint64",
    "float": "float32",
    "float4": "float32",
    "real": "float32",
    "float32": "float32",
    "double": "float64",
    "double precision": "float64",
    "float8": "float64",
    "float64": "float64",
    "string": "utf8",
    "text": "utf8",
    "varchar": "utf8",
    "char": "utf8",
    "utf8": "utf8",
    "large_utf8": "large_utf8",
    "binary": "binary",
    "blob": "binary",
    "bytea": "binary",
    "large_binary": "large_binary",
    "date": "date32",
    "date32": "date32",
    "date64": "date64",
    "timestamp": "timestamp",
    "null": "null",
}


def parse_type_name(name: str) -> DataType:
    """把 SQL 里的类型名解析成 :class:`~mini_lancedb.types.DataType`。"""

    key = " ".join(str(name).strip().lower().split())
    if key in _TYPE_ALIASES:
        return parse_type(_TYPE_ALIASES[key])
    try:
        return parse_type(key)
    except SchemaError:
        raise ExpressionSyntaxError(
            f"未知的类型名 {name!r}",
            expression=str(name),
            position=0,
        ) from None


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


class _Parser:
    """递归下降解析器。

    ``schema`` 不为 ``None`` 时，标识符会在**解析阶段**就解析成带类型的
    :class:`ColumnRef`，列名不认识立刻抛 :class:`PlanError` ——
    错误早发现，而且后面做类型推导时手里已经有类型了。
    """

    __slots__ = ("sql", "toks", "i", "schema")

    def __init__(self, sql: str, tokens: List[Token], schema: Optional[Schema] = None) -> None:
        self.sql = sql
        self.toks = tokens
        self.i = 0
        self.schema = schema

    # -- token 游标 -------------------------------------------------------
    def peek(self, k: int = 0) -> Token:
        idx = min(self.i + k, len(self.toks) - 1)
        return self.toks[idx]

    def advance(self) -> Token:
        tok = self.toks[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def error(self, message: str, tok: Optional[Token] = None) -> ExpressionSyntaxError:
        t = tok if tok is not None else self.peek()
        return ExpressionSyntaxError(message, expression=self.sql, position=t.pos)

    def describe(self, tok: Optional[Token] = None) -> str:
        t = tok if tok is not None else self.peek()
        if t.kind == "eof":
            return "表达式末尾"
        return repr(t.value)

    def expect_op(self, sym: str) -> Token:
        tok = self.peek()
        if not tok.is_op(sym):
            raise self.error(f"这里期待 {sym!r}，实际读到 {self.describe(tok)}", tok)
        return self.advance()

    def expect_kw(self, word: str) -> Token:
        tok = self.peek()
        if not tok.is_kw(word):
            raise self.error(f"这里期待关键字 {word}，实际读到 {self.describe(tok)}", tok)
        return self.advance()

    # -- 入口 -------------------------------------------------------------
    def parse_expression(self) -> Expr:
        expr = self.parse_or()
        tok = self.peek()
        if tok.kind != "eof":
            raise self.error(f"表达式解析完了但还剩 {self.describe(tok)} 没处理", tok)
        return expr

    # -- OR ---------------------------------------------------------------
    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.peek().is_kw("OR"):
            self.advance()
            right = self.parse_and()
            left = BinaryOp("OR", left, right)
        return left

    # -- AND --------------------------------------------------------------
    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.peek().is_kw("AND"):
            self.advance()
            right = self.parse_not()
            left = BinaryOp("AND", left, right)
        return left

    # -- NOT --------------------------------------------------------------
    def parse_not(self) -> Expr:
        if self.peek().is_kw("NOT"):
            self.advance()
            # 右结合：NOT NOT a 合法
            return UnaryOp("NOT", self.parse_not())
        return self.parse_comparison()

    # -- 比较 -------------------------------------------------------------
    def parse_comparison(self) -> Expr:
        left = self.parse_add()
        while True:
            tok = self.peek()

            # 关键字比较：LIKE / IN / IS / BETWEEN，以及它们的 NOT 形式
            if tok.kind == "keyword":
                word = tok.value
                if word in ("LIKE", "ILIKE"):
                    self.advance()
                    left = self._finish_like(left, negated=False, ilike=(word == "ILIKE"))
                    continue
                if word == "IN":
                    self.advance()
                    left = IsIn(left, self._parse_in_list(), False)
                    continue
                if word == "IS":
                    self.advance()
                    left = self._finish_is(left)
                    continue
                if word == "BETWEEN":
                    self.advance()
                    left = self._finish_between(left, negated=False)
                    continue
                if word == "NOT" and self.peek(1).kind == "keyword":
                    nxt = self.peek(1).value
                    if nxt in ("LIKE", "ILIKE", "IN", "BETWEEN"):
                        self.advance()
                        self.advance()
                        if nxt == "IN":
                            left = IsIn(left, self._parse_in_list(), True)
                        elif nxt == "BETWEEN":
                            left = self._finish_between(left, negated=True)
                        else:
                            left = self._finish_like(
                                left, negated=True, ilike=(nxt == "ILIKE")
                            )
                        continue
                return left

            if tok.kind == "op" and tok.value in ("=", "!=", "<>", "<", "<=", ">", ">="):
                self.advance()
                # ``<>`` 和 ``!=`` 是同一个东西，统一成 ``!=``
                right = self.parse_add()
                left = BinaryOp("!=" if tok.value == "<>" else tok.value, left, right)
                continue

            return left

    def _parse_in_list(self) -> List[Expr]:
        """``IN`` 后面的括号（或方括号）列表。"""

        tok = self.peek()
        if tok.is_op("("):
            self.advance()
            closing = ")"
        elif tok.is_op("["):
            self.advance()
            closing = "]"
        else:
            raise self.error(
                f"IN 后面必须是括号列表，实际读到 {self.describe(tok)}", tok
            )
        values: List[Expr] = []
        if self.peek().is_op(closing):
            raise self.error("IN 列表不能为空", self.peek())
        while True:
            values.append(self.parse_expression_until_list_sep(closing))
            nxt = self.peek()
            if nxt.is_op(","):
                self.advance()
                continue
            break
        closing_tok = self.peek()
        if not closing_tok.is_op(closing):
            raise self.error(
                f"IN 列表缺少 {closing!r}，实际读到 {self.describe(closing_tok)}",
                closing_tok,
            )
        self.advance()
        return values

    def parse_expression_until_list_sep(self, closing: str) -> Expr:
        """解析一个列表元素。

        ``IN`` 列表里的元素**不能再包含**未加括号的 ``OR`` 链
        （``a IN (1 OR 2, 3)`` 没有意义），所以这里用比较级而不是
        最外层。好处是报错位置更准：``a IN (1, 2`` 会停在 ``)`` 该在的地方。
        """

        return self.parse_comparison()

    def _finish_like(self, left: Expr, *, negated: bool, ilike: bool) -> Expr:
        pattern = self.parse_add()
        if self.peek().is_kw("ESCAPE"):
            self.advance()
            esc_tok = self.peek()
            if esc_tok.kind != "string":
                raise self.error(
                    f"ESCAPE 后面必须是一个字符串字面量，实际读到 {self.describe(esc_tok)}",
                    esc_tok,
                )
            self.advance()
            escape: Optional[str] = esc_tok.value
            if len(escape) != 1:
                raise self.error(
                    "ESCAPE 只能是单个字符", esc_tok
                )
        else:
            escape = None
        if ilike:
            # ``ILIKE`` 是 PostgreSQL 方言：大小写不敏感的 LIKE。
            # 我们的 Like 节点没有 is_ci 字段，这里用 lower() 归一化两侧，
            # 语义等价（转义字符不受影响）。
            left = FunctionCall("lower", [left], UTF8)
            pattern = FunctionCall("lower", [pattern], UTF8)
        return Like(left, pattern, negated, escape)

    def _finish_is(self, left: Expr) -> Expr:
        negated = False
        if self.peek().is_kw("NOT"):
            self.advance()
            negated = True
        tok = self.peek()
        if tok.is_kw("NULL"):
            self.advance()
            return IsNull(left, negated)
        if tok.is_kw("TRUE") or tok.is_kw("FALSE"):
            self.advance()
            value = tok.value == "TRUE"
            eq = BinaryOp("!=" if value is False else "=", left, Literal(value, BOOL))
            # x IS TRUE  → x = TRUE 且 x 不为 NULL
            # x IS FALSE → x = FALSE 且 x 不为 NULL（注意 NULL 单独判）
            cond = BinaryOp(
                "AND", eq, IsNull(left, True)
            )
            return UnaryOp("NOT", cond) if negated else cond
        raise self.error(
            f"IS 后面只能是 NULL / TRUE / FALSE，实际读到 {self.describe(tok)}", tok
        )

    def _finish_between(self, left: Expr, *, negated: bool) -> Expr:
        low = self.parse_add()
        and_tok = self.peek()
        if not and_tok.is_kw("AND"):
            raise self.error(
                f"BETWEEN 中间缺少 AND，实际读到 {self.describe(and_tok)}", and_tok
            )
        self.advance()
        high = self.parse_add()
        return Between(left, low, high, negated)

    # -- 加减 / 拼接 -------------------------------------------------------
    def parse_add(self) -> Expr:
        left = self.parse_mul()
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.value in ("+", "-", "||"):
                self.advance()
                right = self.parse_mul()
                left = BinaryOp(tok.value, left, right)
                continue
            return left

    # -- 乘除 -------------------------------------------------------------
    def parse_mul(self) -> Expr:
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.value in ("*", "/", "%"):
                self.advance()
                right = self.parse_unary()
                left = BinaryOp(tok.value, left, right)
                continue
            return left

    # -- 一元 -------------------------------------------------------------
    def parse_unary(self) -> Expr:
        tok = self.peek()
        if tok.is_op("-"):
            self.advance()
            return UnaryOp("-", self.parse_unary())
        if tok.is_op("+"):
            self.advance()
            return UnaryOp("+", self.parse_unary())
        if tok.is_kw("NOT"):
            self.advance()
            return UnaryOp("NOT", self.parse_unary())
        return self.parse_cast()

    # -- :: 后缀转换 -------------------------------------------------------
    def parse_cast(self) -> Expr:
        expr = self.parse_primary()
        while self.peek().is_op("::"):
            self.advance()
            expr = Cast(expr, self._parse_cast_target())
        return expr

    def _parse_cast_target(self) -> DataType:
        tok = self.peek()
        if tok.kind not in ("ident", "keyword"):
            raise self.error(
                f":: 后面必须是类型名，实际读到 {self.describe(tok)}", tok
            )
        # 支持 ``double precision`` 这种两词类型名
        parts = [str(self.advance().value)]
        if self.peek().kind == "ident" and parts[0].lower() == "double":
            parts.append(str(self.advance().value))
        return parse_type_name(" ".join(parts))

    # -- 基本表达式 --------------------------------------------------------
    def parse_primary(self) -> Expr:
        tok = self.peek()

        if tok.kind == "number":
            self.advance()
            return Literal(tok.value)

        if tok.kind == "string":
            self.advance()
            return Literal(tok.value, UTF8)

        if tok.kind == "bytes":
            self.advance()
            return Literal(tok.value, BINARY)

        if tok.kind == "keyword":
            if tok.value == "NULL":
                self.advance()
                return Literal(None, NULL_TYPE)
            if tok.value in ("TRUE", "FALSE"):
                self.advance()
                return Literal(tok.value == "TRUE", BOOL)
            if tok.value == "CAST":
                self.advance()
                return self._parse_cast_call()
            raise self.error(
                f"这里期待一个值或列名，实际读到关键字 {tok.value}", tok
            )

        if tok.is_op("("):
            self.advance()
            inner = self.parse_or()
            closing = self.peek()
            if not closing.is_op(")"):
                raise self.error(
                    f"括号没有闭合：这里期待 ')'，实际读到 {self.describe(closing)}",
                    closing,
                )
            self.advance()
            return inner

        if tok.is_op("["):
            return self._parse_list_literal()

        if tok.kind == "ident":
            return self._parse_ident()

        raise self.error(
            f"这里期待一个值、列名或括号，实际读到 {self.describe(tok)}", tok
        )

    def _parse_list_literal(self) -> Expr:
        opening = self.expect_op("[")
        items: List[Expr] = []
        if self.peek().is_op("]"):
            self.advance()
            return Literal([], None)
        while True:
            items.append(self.parse_or())
            nxt = self.peek()
            if nxt.is_op(","):
                self.advance()
                continue
            break
        closing = self.peek()
        if not closing.is_op("]"):
            raise self.error(
                f"列表没有闭合：这里期待 ']'，实际读到 {self.describe(closing)}",
                closing,
            )
        self.advance()
        values = [v.value if isinstance(v, Literal) else None for v in items]
        if any(not isinstance(v, Literal) for v in items):
            raise self.error("列表字面量里只能放常量", opening)
        return Literal(values)

    def _parse_cast_call(self) -> Expr:
        """``CAST(expr AS type)``。``CAST`` 已经在 :meth:`parse_primary` 里吃掉了。"""

        self.expect_op("(")
        inner = self.parse_or()
        as_tok = self.peek()
        if not as_tok.is_kw("AS"):
            raise self.error(
                f"CAST 里缺少 AS，实际读到 {self.describe(as_tok)}", as_tok
            )
        self.advance()
        target = self._parse_cast_target()
        closing = self.peek()
        if not closing.is_op(")"):
            raise self.error(
                f"CAST 缺少 ')'，实际读到 {self.describe(closing)}", closing
            )
        self.advance()
        return Cast(inner, target)

    def _parse_ident(self) -> Expr:
        tok = self.advance()
        name = str(tok.value)

        # 函数调用：名字后面紧跟 '('
        if self.peek().is_op("("):
            lower = name.lower()
            spec = FUNCTIONS.get(lower)
            if spec is None:
                raise self.error(
                    f"未知函数 {name!r}（可用函数: {', '.join(sorted(FUNCTIONS))}）",
                    tok,
                )
            self.advance()  # 吃掉 '('
            args: List[Expr] = []
            if not self.peek().is_op(")"):
                while True:
                    args.append(self.parse_or())
                    nxt = self.peek()
                    if nxt.is_op(","):
                        self.advance()
                        continue
                    break
            closing = self.peek()
            if not closing.is_op(")"):
                raise self.error(
                    f"函数 {lower} 缺少 ')'，实际读到 {self.describe(closing)}",
                    closing,
                )
            self.advance()
            if not (spec.min_args <= len(args) <= spec.max_args):
                raise self.error(
                    f"{lower} 需要 {spec.min_args}~{spec.max_args} 个参数，"
                    f"实际给了 {len(args)} 个",
                    tok,
                )
            return FunctionCall(lower, args, spec.returns([a.type for a in args]))

        # nan / inf：DataFusion 允许裸写
        special = _SPECIAL_FLOATS.get(name.lower())
        if special is not None and (self.schema is None or name not in self.schema):
            return Literal(special, FLOAT64)

        # 普通列引用
        if self.schema is not None:
            field = self.schema.get(name)
            if field is None:
                raise PlanError(
                    f"没有名为 {name!r} 的列",
                    available=self.schema.names,
                    valid_fields=self.schema.names,
                    hint="Valid fields are " + ", ".join(f"`{n}`" for n in self.schema.names),
                )
            return ColumnRef(name, field.type)
        return ColumnRef(name)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def parse(sql: str, schema: Optional[Schema] = None) -> Expr:
    """解析一个 SQL 表达式。

    ``schema`` 为 ``None`` 时列名原样保留且类型为 ``null``（未知），
    求值时再按 batch 里的实际列类型走；传了 ``schema`` 则会在解析阶段
    就把列名和类型钉死。
    """

    tokens = tokenize(sql)
    return _Parser(sql, tokens, schema).parse_expression()


def parse_predicate(sql: str, schema: Schema) -> Expr:
    """解析一个谓词并把列名解析到 ``schema`` 上。

    * 列名不认识 → :class:`~mini_lancedb.errors.PlanError`，
      报错里列出所有可用列名（照抄真实 LanceDB 的措辞：
      ``Valid fields are `a`, `b``）；
    * 语法错误 → :class:`~mini_lancedb.errors.ExpressionSyntaxError`，
      带精确字符位置。

    返回的表达式**不保证**是布尔类型 —— 交给
    :func:`mini_lancedb.expr.eval.evaluate_predicate` 检查
    （那里会报"表达式不是布尔"），因为 ``WHERE`` 里写个纯数值列
    是个语义错误而不是语法错误。
    """

    if not isinstance(schema, Schema):
        raise PlanError("parse_predicate 需要 Schema", got=type(schema).__name__)
    expr = parse(sql, schema)
    # 裸列名当谓词是常见手误（``where("is_active")``），提前给出人话提示
    if isinstance(expr, ColumnRef):
        field = schema.get(expr.name)
        if field is not None and not isinstance(field.type, type(BOOL)):
            raise PlanError(
                f"谓词 {expr.name!r} 不是布尔类型（{field.type.name}）",
                column=expr.name,
            )
    return expr


def parse_and_resolve(sql: str, schema: Schema) -> Expr:
    """``parse_predicate`` 的别名，语义一致但名字更直白。"""

    return parse_predicate(sql, schema)
