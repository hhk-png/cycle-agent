"""表达式 AST —— 查询引擎的"中间语言"。

LanceDB 的 ``.where("a > 1 AND b LIKE 'x%'")`` 最终并不会被逐字符解释执行。
它先被解析成一棵**带类型的树**（本模块），然后：

1. :mod:`mini_lancedb.expr.rewrite` 把它改写/下推 —— 能交给标量索引的部分
   变成 :class:`~mini_lancedb.index.base.ScalarIndexExpr`，能交给 zone map
   的部分变成 min/max 剪枝条件，剩下的才是真正的过滤器；
2. :mod:`mini_lancedb.expr.eval` 用 numpy 向量化求值，一次算一整列；
3. ``explain`` 打印的就是这棵树 —— 所以 ``to_sql()`` 必须能**往返**
   （``parse(expr.to_sql()).equals(expr)``），否则计划缓存和 explain
   输出就会和真实执行计划对不上。

为什么要有独立的 AST，而不是直接拿 Python 的 ``lambda`` 或者 ``eval``？
因为要做**静态分析**：只有树才能回答"这个谓词引用了哪些列"
（:meth:`Expr.references`，决定要读哪些列）、"它能不能被某个索引回答"、
"它的选择率大概是多少"（决定 prefilter / postfilter）。这些问题的答案
决定了查询快慢，而它们全都需要结构，不能需要执行。

## 关于 ``__eq__`` 被重载的说明（这是**故意**的）

:class:`Expr` 的 ``__eq__`` 不返回 ``bool``，而是返回一个 :class:`BinaryOp`
比较节点。这样做是为了让构造表达式写起来和 pandas / Polars / DataFusion
Python 绑定一致::

    col("a") == 1                 # 而不是 lit("a = 1")
    (col("a") > 1) & (col("b") < 2)

代价是三个必须记住的后果（本模块内部一律用 :meth:`Expr.equals` 比较）：

* ``expr1 == expr2`` 得到的对象**永远是真值**，所以
  ``assert expr1 == expr2`` 恒成立 —— 测试里必须写 ``assert a.equals(b)``；
* ``expr in [list]``、``expr in {set}`` 这类操作依赖 ``__eq__`` 的返回值，
  对 :class:`Expr` 不可靠，请改用 :meth:`Expr.equals` 或 ``to_sql()``；
* :class:`Expr` 进了 ``set`` / ``dict`` 之后，哈希碰撞时会退化成
  "返回真值的 ``__eq__``"，于是被误判为相等。内部代码因此**从不**把
  :class:`Expr` 当字典键，只用它的 SQL 字符串。

``__hash__`` 因此按 ``to_sql()`` 定义：哈希相同 + SQL 相同，就是"同一个表达式"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..errors import ExpressionSyntaxError, PlanError
from ..types import (
    BINARY,
    BOOL,
    DataType,
    FLOAT64,
    INT64,
    NULL,
    UTF8,
    infer_type,
    parse_type,
    promote_types,
)

__all__ = [
    "Expr",
    "ColumnRef",
    "Literal",
    "BinaryOp",
    "UnaryOp",
    "IsNull",
    "IsIn",
    "Like",
    "Between",
    "Cast",
    "FunctionCall",
    "col",
    "lit",
    "and_",
    "or_",
    "not_",
    "literal_value",
    "serialize",
    "deserialize",
    "type_to_sql",
    "COMPARISON_OPS",
    "LOGICAL_OPS",
    "ARITHMETIC_OPS",
    "STRING_OPS",
    "KEYWORD_OPS",
    "BINARY_OPS",
    "FLIP_COMPARISON",
    # 优先级
    "PREC_OR",
    "PREC_AND",
    "PREC_NOT",
    "PREC_CMP",
    "PREC_ADD",
    "PREC_MUL",
    "PREC_UNARY",
    "PREC_CAST",
    "PREC_PRIMARY",
]


# ---------------------------------------------------------------------------
# 运算符与优先级
# ---------------------------------------------------------------------------

#: 比较运算符。``<>`` 是 SQL 标准写法，规范化为 ``!=``。
COMPARISON_OPS: Tuple[str, ...] = ("=", "!=", "<", "<=", ">", ">=")
#: 逻辑运算符（关键字，全大写存储）
LOGICAL_OPS: Tuple[str, ...] = ("AND", "OR")
#: 算术运算符
ARITHMETIC_OPS: Tuple[str, ...] = ("+", "-", "*", "/", "%")
#: 字符串拼接（SQL 里是 ``||``，不是 ``+``）
STRING_OPS: Tuple[str, ...] = ("||",)
#: 关键字形式的二元运算。解析器一般产出专用节点（Like / IsIn / IsNull /
#: Between），但 :class:`BinaryOp` 也接受这些写法，便于手工构造。
KEYWORD_OPS: Tuple[str, ...] = (
    "LIKE",
    "NOT LIKE",
    "IN",
    "NOT IN",
    "IS",
    "IS NOT",
)
BINARY_OPS: Tuple[str, ...] = (
    COMPARISON_OPS + LOGICAL_OPS + ARITHMETIC_OPS + STRING_OPS + KEYWORD_OPS
)

#: 把 ``a < 5`` 翻成 ``5 > a`` 时用的映射 —— 索引侧只认"列在左、字面量在右"。
FLIP_COMPARISON: Dict[str, str] = {
    "=": "=",
    "!=": "!=",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}

# 优先级数值越大结合越紧。这套数字同时被两处使用：
# * 解析器用它决定递归下降的层数；
# * ``to_sql`` 用它决定"要不要加括号"，从而保证往返一致。
PREC_OR = 1
PREC_AND = 2
PREC_NOT = 3
PREC_CMP = 4
PREC_ADD = 5
PREC_MUL = 6
PREC_UNARY = 7
PREC_CAST = 8
PREC_PRIMARY = 9

_BINARY_PREC: Dict[str, int] = {}
for _op in COMPARISON_OPS + KEYWORD_OPS:
    _BINARY_PREC[_op] = PREC_CMP
_BINARY_PREC["AND"] = PREC_AND
_BINARY_PREC["OR"] = PREC_OR
for _op in ("+", "-", "||"):
    _BINARY_PREC[_op] = PREC_ADD
for _op in ("*", "/", "%"):
    _BINARY_PREC[_op] = PREC_MUL

#: 默认就在这些运算符右边加一层括号（左结合）
_LEFT_ASSOC = set(BINARY_OPS) - set(COMPARISON_OPS) - set(KEYWORD_OPS)


# ---------------------------------------------------------------------------
# 标识符 / 字面量渲染
# ---------------------------------------------------------------------------

#: 这些词在解析器里是关键字，作为列名裸写会被当成关键字 —— 必须加引号。
_RESERVED = frozenset(
    {
        "and", "or", "not", "in", "like", "ilike", "is", "null", "true",
        "false", "between", "escape", "as", "cast", "select", "from",
        "where", "group", "order", "by", "limit", "offset", "having",
        "join", "on", "union", "all", "distinct", "case", "when", "then",
        "else", "end", "exists", "asc", "desc", "nan", "inf", "infinity",
    }
)


def _is_plain_ident(name: str) -> bool:
    """能否不加引号地写出来。"""

    if not name or name[0].isdigit():
        return False
    if name.lower() in _RESERVED:
        return False
    for ch in name:
        if not (ch.isascii() and (ch.isalnum() or ch == "_")):
            return False
    return True


def quote_ident(name: str) -> str:
    """需要时给标识符加双引号，内部的双引号翻倍转义。"""

    if _is_plain_ident(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _quote_string(value: str) -> str:
    """SQL 字符串字面量：单引号内的单引号写成两个。"""

    return "'" + value.replace("'", "''") + "'"


def _format_float(value: float) -> str:
    """浮点字面量渲染。

    ``repr`` 保证 ``float(repr(x)) == x``，这是往返正确的关键。
    ``inf`` / ``nan`` 会渲染成同名标识符 —— 解析器认得它们
    （见 :mod:`mini_lancedb.expr.parser` 里的 ``_SPECIAL_FLOATS``）。
    """

    text = repr(float(value))
    if text.endswith(".0"):
        return text
    if "e" in text or "E" in text or "." in text or text in ("inf", "-inf", "nan"):
        return text
    return text + ".0"


_ARROW_TYPE_NAMES: Tuple[Tuple[str, str], ...] = (
    ("bool", "bool"),
    ("int8", "int8"),
    ("int16", "int16"),
    ("int32", "int32"),
    ("int64", "int64"),
    ("uint8", "uint8"),
    ("uint16", "uint16"),
    ("uint32", "uint32"),
    ("uint64", "uint64"),
    ("float16", "float16"),
    ("float32", "float32"),
    ("float64", "float64"),
    ("utf8", "utf8"),
    ("large_utf8", "large_utf8"),
    ("binary", "binary"),
    ("large_binary", "large_binary"),
    ("date32", "date32"),
    ("date64", "date64"),
)


def type_to_sql(t: DataType) -> str:
    """类型 → SQL 里的类型名（``CAST(x AS <这里>)``）。

    只用最普通的标量名，避免把 ``fixed_size_list<float32>[128]``
    这类内部名字泄进 SQL —— 那种字符串没法再解析回来。
    """

    for name, text in _ARROW_TYPE_NAMES:
        if t.name == name:
            return text
    if t.name == "null":
        return "null"
    if t.name.startswith("timestamp"):
        return "timestamp"
    if t.name.startswith("decimal128"):
        return "double"
    return t.name


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class Expr:
    """表达式节点基类。

    子类必须提供：

    * ``type`` —— 结果类型（推导用的，不参与求值；求值时以实际列类型为准）；
    * :meth:`children`、:meth:`references`、:meth:`_sql_text`、:meth:`_sql_prec`、
      :meth:`serialize`。
    """

    __slots__ = ()

    #: 节点名，进 :meth:`serialize` 的 ``"node"`` 字段，也用于 ``repr``
    node_name: str = "Expr"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.node_name == "Expr":
            cls.node_name = cls.__name__

    # -- 结构 -------------------------------------------------------------
    @property
    def children(self) -> Tuple["Expr", ...]:
        return ()

    def references(self) -> set:
        """这个表达式引用了哪些列 —— 投影下推（只读需要的列）就靠它。"""

        out: set = set()
        for c in self.children:
            out |= c.references()
        return out

    # -- SQL 渲染 ---------------------------------------------------------
    def to_sql(self) -> str:
        return self._render(0)


    def _render(self, parent_prec: int) -> str:
        text = self._sql_text()
        if self._sql_prec() < parent_prec:
            return "(" + text + ")"
        return text

    def _sql_text(self) -> str:
        raise NotImplementedError(self.node_name)

    def _sql_prec(self) -> int:
        return PREC_PRIMARY

    # -- 相等 / 哈希 -------------------------------------------------------
    def key(self) -> str:
        return self.to_sql()

    def equals(self, other: object) -> bool:
        """结构等价判断。

        **不要**用 ``==`` —— 那个被重载成构造比较节点了（见模块文档）。
        """

        return (
            isinstance(other, Expr)
            and type(self) is type(other)
            and self.type == other.type
            and self.to_sql() == other.to_sql()
        )

    def __hash__(self) -> int:
        return hash(self.to_sql())

    def __repr__(self) -> str:
        return f"{self.node_name}({self.to_sql()!r})"

    # -- 运算符重载 -------------------------------------------------------
    def _bin(self, op: str, other: Any) -> "BinaryOp":
        return BinaryOp(op, self, _coerce_operand(other))

    def __eq__(self, other: Any) -> "BinaryOp":  # type: ignore[override]
        return self._bin("=", other)

    def __ne__(self, other: Any) -> "BinaryOp":  # type: ignore[override]
        return self._bin("!=", other)

    def __lt__(self, other: Any) -> "BinaryOp":
        return self._bin("<", other)

    def __le__(self, other: Any) -> "BinaryOp":
        return self._bin("<=", other)

    def __gt__(self, other: Any) -> "BinaryOp":
        return self._bin(">", other)

    def __ge__(self, other: Any) -> "BinaryOp":
        return self._bin(">=", other)

    def __add__(self, other: Any) -> "BinaryOp":
        return self._bin("+", other)

    def __radd__(self, other: Any) -> "BinaryOp":
        return BinaryOp("+", _coerce_operand(other), self)

    def __sub__(self, other: Any) -> "BinaryOp":
        return self._bin("-", other)

    def __rsub__(self, other: Any) -> "BinaryOp":
        return BinaryOp("-", _coerce_operand(other), self)

    def __mul__(self, other: Any) -> "BinaryOp":
        return self._bin("*", other)

    def __rmul__(self, other: Any) -> "BinaryOp":
        return BinaryOp("*", _coerce_operand(other), self)

    def __truediv__(self, other: Any) -> "BinaryOp":
        return self._bin("/", other)

    def __rtruediv__(self, other: Any) -> "BinaryOp":
        return BinaryOp("/", _coerce_operand(other), self)

    def __mod__(self, other: Any) -> "BinaryOp":
        return self._bin("%", other)

    def __rmod__(self, other: Any) -> "BinaryOp":
        return BinaryOp("%", _coerce_operand(other), self)

    def __and__(self, other: Any) -> "BinaryOp":
        """Python 的 ``&`` 映射成 SQL 的 ``AND``。

        DataFusion / Polars 的 Python 绑定就是这个约定，原因是 Python 里
        ``and`` 没法重载、而且会强制求值成 bool。注意 SQL 文本里没有 ``&``
        —— 真实 DataFusion 解析 ``a & 1`` 会直接报
        "Operator & is not supported"。也就是说 ``&`` 只是**构造 AST 的语法糖**。
        """

        return self._bin("AND", other)

    def __or__(self, other: Any) -> "BinaryOp":
        """Python 的 ``|`` 映射成 SQL 的 ``OR``（同上，仅构造语法糖）。"""

        return self._bin("OR", other)

    def __invert__(self) -> "UnaryOp":
        """``~expr`` 等于 ``NOT expr``。"""

        return UnaryOp("NOT", self)

    def __neg__(self) -> "UnaryOp":
        return UnaryOp("-", self)

    def __pos__(self) -> "UnaryOp":
        return UnaryOp("+", self)

    # -- 语法糖 -----------------------------------------------------------
    def is_null(self) -> "IsNull":
        return IsNull(self, False)

    def is_not_null(self) -> "IsNull":
        return IsNull(self, True)

    def isin(self, values: Sequence[Any]) -> "IsIn":
        return IsIn(self, [lit(v) for v in values], False)

    def between(self, low: Any, high: Any) -> "Between":
        return Between(self, lit(low), lit(high), False)

    def like(self, pattern: str, *, escape: Optional[str] = None) -> "Like":
        return Like(self, lit(pattern), False, escape)

    def cast(self, target: Any) -> "Cast":
        return Cast(self, target if isinstance(target, DataType) else parse_type(str(target)))

    def __getitem__(self, key: Any) -> "FunctionCall":
        """``expr[0]`` / ``expr["k"]`` —— 取列表元素或结构体字段。

        取列表下标走 ``list_extract``，取字符串键走 ``struct_field``。
        真实 DataFusion 也提供这两个函数名。
        """

        if isinstance(key, str):
            return FunctionCall("struct_field", [self, lit(key)], NULL)
        return FunctionCall("list_extract", [self, lit(int(key))], NULL)

    # -- 序列化 -----------------------------------------------------------
    def serialize(self) -> Dict[str, Any]:
        raise NotImplementedError(self.node_name)

    @staticmethod
    def deserialize(obj: Mapping[str, Any]) -> "Expr":
        return deserialize(obj)


def _coerce_operand(value: Any) -> Expr:
    return value if isinstance(value, Expr) else lit(value)


# ---------------------------------------------------------------------------
# 叶子节点
# ---------------------------------------------------------------------------


class ColumnRef(Expr):
    """列引用。``name`` 是物理列名（区分大小写，SQL 里裸写不区分是解析器的事）。"""

    __slots__ = ("name", "type")

    node_name = "ColumnRef"

    def __init__(self, name: str, type: Optional[DataType] = None) -> None:
        if not isinstance(name, str) or not name:
            raise PlanError("列名必须是非空字符串", got=repr(name))
        self.name = name
        self.type = type if type is not None else NULL

    def references(self) -> set:
        return {self.name}

    def _sql_text(self) -> str:
        return quote_ident(self.name)

    def serialize(self) -> Dict[str, Any]:
        return {"node": "ColumnRef", "name": self.name, "type": self.type.to_json()}


class Literal(Expr):
    """常量。

    ``value`` 永远是**原生 Python 值**（int / float / str / bytes / bool / None /
    list / dict），不用 numpy 标量 —— 这样 :meth:`serialize` 出来的 dict
    能直接 ``json.dumps``，计划缓存才存得下。
    """

    __slots__ = ("value", "type")

    node_name = "Literal"

    def __init__(self, value: Any, type: Optional[DataType] = None) -> None:
        self.value = _normalize_scalar(value)
        self.type = type if type is not None else _infer_literal_type(self.value)

    def is_null_literal(self) -> bool:
        """这个名字刻意带上 ``literal`` 后缀：:meth:`Expr.is_null` 是
        "构造 ``IS NULL`` 谓词"，而这个方法是"我本身是不是 NULL 常量"。"""

        return self.value is None

    def to_python(self) -> Any:
        return self.value

    def _sql_text(self) -> str:
        return _literal_sql(self.value)

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "Literal",
            "value": _jsonable_scalar(self.value),
            "type": self.type.to_json(),
        }


def _literal_sql(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, str):
        return _quote_string(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal_sql(v) for v in value) + "]"
    raise PlanError(f"无法把 {value!r} 渲染成 SQL 字面量")


def _normalize_scalar(value: Any) -> Any:
    """把 numpy 标量降级成 Python 标量；列表递归处理。"""

    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_scalar(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_normalize_scalar(v) for v in value.tolist()]
    if isinstance(value, Mapping):
        if set(value) == {"__bytes__"}:
            return bytes.fromhex(str(value["__bytes__"]))
        return {k: _normalize_scalar(v) for k, v in value.items()}
    return value


#: JSON 里表示 bytes 的标记键（``Literal`` 的值必须能过 ``json.dumps``，
#: 否则计划缓存就存不下带二进制字面量的表达式）。
_BYTES_MARKER = "__bytes__"


def _jsonable_scalar(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {_BYTES_MARKER: bytes(value).hex()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_scalar(v) for v in value]
    return value


def _infer_literal_type(value: Any) -> DataType:
    """字面量类型推断。刻意和 :func:`mini_lancedb.types.infer_type` 对齐：
    Python ``int`` → ``int64``、``float`` → ``float64``、``str`` → ``utf8``。

    不做"挑最窄的整数"那种优化：类型一旦写进 AST 就会被下游当契约用，
    让它随常量值漂移只会带来惊喜。
    """

    if value is None:
        return NULL
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return INT64
    if isinstance(value, float):
        return FLOAT64
    if isinstance(value, str):
        return UTF8
    if isinstance(value, (bytes, bytearray)):
        return BINARY
    if isinstance(value, (list, tuple)):
        # 注意要**包一层**：infer_type 接受的是"一列值"，我们这里只有
        # 一个值（本身是个列表），所以要让它按"一行"来看待。
        # 少了这层包装，``lit([1, 2])`` 会被推成 int64 而不是
        # ``fixed_size_list<int64>[2]``。
        return infer_type([list(value)])
    if isinstance(value, Mapping):
        return infer_type([value])
    raise PlanError(f"无法推断字面量 {value!r} 的类型")


# ---------------------------------------------------------------------------
# 二元运算
# ---------------------------------------------------------------------------


class BinaryOp(Expr):
    """二元运算节点。

    ``op`` 全大写存储（关键字形式），比较运算符原样。``<>`` 会被规范成 ``!=``，
    这样下游只需要处理一种写法。
    """

    __slots__ = ("op", "left", "right", "type")

    node_name = "BinaryOp"

    def __init__(
        self,
        op: str,
        left: Expr,
        right: Expr,
        type: Optional[DataType] = None,
    ) -> None:
        op = _normalize_op(op)
        if op not in BINARY_OPS:
            raise PlanError(f"不支持的二元运算符 {op!r}", supported=list(BINARY_OPS))
        self.op = op
        self.left = left if isinstance(left, Expr) else lit(left)
        self.right = right if isinstance(right, Expr) else lit(right)
        self.type = type if type is not None else infer_binary_type(op, self.left.type, self.right.type)

    @property
    def children(self) -> Tuple[Expr, ...]:
        return (self.left, self.right)

    def _sql_prec(self) -> int:
        return _BINARY_PREC[self.op]

    def _sql_text(self) -> str:
        p = self._sql_prec()
        right_prec = p + 1 if self.op in _LEFT_ASSOC or self.op in COMPARISON_OPS else p
        return f"{self.left._render(p)} {self.op} {self.right._render(right_prec)}"

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "BinaryOp",
            "op": self.op,
            "left": self.left.serialize(),
            "right": self.right.serialize(),
            "type": self.type.to_json(),
        }


def _normalize_op(op: str) -> str:
    text = str(op).strip()
    if text == "<>":
        return "!="
    if text.isalpha() or " " in text:
        return " ".join(text.upper().split())
    return text


class UnaryOp(Expr):
    """一元运算：``NOT``、一元负号、一元正号。"""

    __slots__ = ("op", "operand", "type")

    node_name = "UnaryOp"

    def __init__(self, op: str, operand: Expr, type: Optional[DataType] = None) -> None:
        op = op.upper() if op.isalpha() else op
        if op not in ("NOT", "-", "+"):
            raise PlanError(f"不支持的一元运算符 {op!r}", supported=["NOT", "-", "+"])
        self.op = op
        self.operand = operand if isinstance(operand, Expr) else lit(operand)
        if type is not None:
            self.type = type
        elif op == "NOT":
            self.type = BOOL
        else:
            self.type = self.operand.type

    @property
    def children(self) -> Tuple[Expr, ...]:
        return (self.operand,)

    def _sql_prec(self) -> int:
        return PREC_NOT if self.op == "NOT" else PREC_UNARY

    def _sql_text(self) -> str:
        p = self._sql_prec()
        text = self.operand._render(p)
        if self.op in ("-", "+") and text.startswith("-"):
            # ``--a`` 在 SQL 里会被读成行注释。我们的分词器没有注释，
            # 但生成这种文本对任何 SQL 工具都是坑，索性加括号。
            text = "(" + text + ")"
        if self.op == "NOT":
            return f"NOT {text}"
        return f"{self.op}{text}"

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "UnaryOp",
            "op": self.op,
            "operand": self.operand.serialize(),
            "type": self.type.to_json(),
        }


# ---------------------------------------------------------------------------
# 谓词专用节点
# ---------------------------------------------------------------------------


class IsNull(Expr):
    """``x IS NULL`` / ``x IS NOT NULL``。

    单独建节点而不是写成 ``BinaryOp("IS", x, Literal(None))``，是因为
    ``x = NULL``（永远 NULL）和 ``x IS NULL``（真布尔值）在 SQL 里
    **语义完全不同** —— 前者是踩坑第一名，节点分开就不会混。
    """

    __slots__ = ("child", "negated", "type")

    node_name = "IsNull"

    def __init__(self, child: Expr, negated: bool = False) -> None:
        self.child = child if isinstance(child, Expr) else lit(child)
        self.negated = bool(negated)
        self.type = BOOL

    @property
    def children(self) -> Tuple[Expr, ...]:
        return (self.child,)

    def _sql_prec(self) -> int:
        return PREC_CMP

    def _sql_text(self) -> str:
        return f"{self.child._render(PREC_CMP)} IS {'NOT ' if self.negated else ''}NULL"

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "IsNull",
            "child": self.child.serialize(),
            "negated": self.negated,
            "type": self.type.to_json(),
        }


class IsIn(Expr):
    """``x IN (v1, v2, ...)`` / ``x NOT IN (...)``。

    ``IN`` 和 ``NOT IN`` 在 SQL 里对 NULL 的处理是**最反直觉**的一处：
    ``x NOT IN (1, NULL)`` 对任何 x 都不是 TRUE（要么 NULL 要么 FALSE），
    因为展开成 ``x != 1 AND x != NULL`` 之后最后一项是 NULL。
    求值见 :mod:`mini_lancedb.expr.eval`。
    """

    __slots__ = ("child", "values", "negated", "type")

    node_name = "IsIn"

    def __init__(self, child: Expr, values: Sequence[Expr], negated: bool = False) -> None:
        self.child = child if isinstance(child, Expr) else lit(child)
        self.values = tuple(v if isinstance(v, Expr) else lit(v) for v in values)
        if not self.values:
            raise ExpressionSyntaxError("IN 后面至少要有一个值")
        self.negated = bool(negated)
        self.type = BOOL

    @property
    def children(self) -> Tuple[Expr, ...]:
        return (self.child, *self.values)

    def literal_values(self) -> Optional[List[Any]]:
        """全是常量时返回 Python 值列表，否则 ``None`` —— 索引改写要用。"""

        out: List[Any] = []
        for v in self.values:
            ok, value = literal_value(v)
            if not ok:
                return None
            out.append(value)
        return out

    def _sql_prec(self) -> int:
        return PREC_CMP

    def _sql_text(self) -> str:
        inner = ", ".join(v._render(0) for v in self.values)
        kw = "NOT IN" if self.negated else "IN"
        return f"{self.child._render(PREC_CMP)} {kw} ({inner})"

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "IsIn",
            "child": self.child.serialize(),
            "values": [v.serialize() for v in self.values],
            "negated": self.negated,
            "type": self.type.to_json(),
        }


class Like(Expr):
    """``x LIKE 'a%' [ESCAPE '\\']`` / ``x NOT LIKE ...``。

    ``escape`` 是**单个字符**（SQL 标准），默认是 ``None``
    （此时反斜杠**没有**特殊含义 —— 这一点和 MySQL 相反，踩坑率高）。
    """

    __slots__ = ("child", "pattern", "negated", "escape", "type")

    node_name = "Like"

    def __init__(
        self,
        child: Expr,
        pattern: Expr,
        negated: bool = False,
        escape: Optional[str] = None,
    ) -> None:
        self.child = child if isinstance(child, Expr) else lit(child)
        self.pattern = pattern if isinstance(pattern, Expr) else lit(pattern)
        self.negated = bool(negated)
        if escape is not None and len(escape) != 1:
            raise ExpressionSyntaxError("ESCAPE 只能是单个字符", position=None)
        self.escape = escape
        self.type = BOOL

    @property
    def children(self) -> Tuple[Expr, ...]:
        return (self.child, self.pattern)

    def literal_pattern(self) -> Optional[str]:
        if isinstance(self.pattern, Literal) and isinstance(self.pattern.value, str):
            return self.pattern.value
        return None

    def _sql_prec(self) -> int:
        return PREC_CMP

    def _sql_text(self) -> str:
        kw = "NOT LIKE" if self.negated else "LIKE"
        text = f"{self.child._render(PREC_CMP)} {kw} {self.pattern._render(PREC_ADD)}"
        if self.escape is not None:
            text += f" ESCAPE {_quote_string(self.escape)}"
        return text

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "Like",
            "child": self.child.serialize(),
            "pattern": self.pattern.serialize(),
            "negated": self.negated,
            "escape": self.escape,
            "type": self.type.to_json(),
        }


class Between(Expr):
    """``x BETWEEN lo AND hi`` —— 闭区间，等价于 ``x >= lo AND x <= hi``。"""

    __slots__ = ("child", "low", "high", "negated", "type")

    node_name = "Between"

    def __init__(
        self, child: Expr, low: Expr, high: Expr, negated: bool = False
    ) -> None:
        self.child = child if isinstance(child, Expr) else lit(child)
        self.low = low if isinstance(low, Expr) else lit(low)
        self.high = high if isinstance(high, Expr) else lit(high)
        self.negated = bool(negated)
        self.type = BOOL

    @property
    def children(self) -> Tuple[Expr, ...]:
        return (self.child, self.low, self.high)

    def _sql_prec(self) -> int:
        return PREC_CMP

    def _sql_text(self) -> str:
        kw = "NOT BETWEEN" if self.negated else "BETWEEN"
        return (
            f"{self.child._render(PREC_CMP)} {kw} "
            f"{self.low._render(PREC_ADD)} AND {self.high._render(PREC_ADD)}"
        )

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "Between",
            "child": self.child.serialize(),
            "low": self.low.serialize(),
            "high": self.high.serialize(),
            "negated": self.negated,
            "type": self.type.to_json(),
        }


class Cast(Expr):
    """类型转换。

    渲染成 ``CAST(x AS t)`` 而不是 ``x::t``：前者往返更稳（类型名里带
    ``<>``/``[]`` 的嵌套类型用 ``::`` 写出来是有歧义的），也与 Arrow SQL
    方言一致。
    """

    __slots__ = ("child", "target", "type")

    node_name = "Cast"

    def __init__(self, child: Expr, target: DataType) -> None:
        if not isinstance(target, DataType):
            raise PlanError("CAST 的目标必须是 DataType", got=repr(target))
        self.child = child if isinstance(child, Expr) else lit(child)
        self.target = target
        self.type = target

    @property
    def children(self) -> Tuple[Expr, ...]:
        return (self.child,)

    def _sql_text(self) -> str:
        return f"CAST({self.child._render(0)} AS {type_to_sql(self.target)})"

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "Cast",
            "child": self.child.serialize(),
            "target": self.target.to_json(),
            "type": self.type.to_json(),
        }


class FunctionCall(Expr):
    """标量函数调用。函数名一律小写规范化（SQL 里函数名不区分大小写）。"""

    __slots__ = ("name", "args", "type")

    node_name = "FunctionCall"

    def __init__(
        self, name: str, args: Sequence[Expr], type: Optional[DataType] = None
    ) -> None:
        self.name = str(name).lower()
        self.args = tuple(a if isinstance(a, Expr) else lit(a) for a in args)
        self.type = type if type is not None else NULL

    @property
    def children(self) -> Tuple[Expr, ...]:
        return tuple(self.args)

    def _sql_text(self) -> str:
        inner = ", ".join(a._render(0) for a in self.args)
        return f"{self.name}({inner})"

    def serialize(self) -> Dict[str, Any]:
        return {
            "node": "FunctionCall",
            "name": self.name,
            "args": [a.serialize() for a in self.args],
            "type": self.type.to_json(),
        }


# ---------------------------------------------------------------------------
# 类型推导
# ---------------------------------------------------------------------------


def infer_binary_type(op: str, left: DataType, right: DataType) -> DataType:
    """二元运算的结果类型。

    规则（够用就行，宁可保守）：

    * 比较 / 逻辑 / LIKE / IN / IS → ``bool``
    * ``||`` → ``utf8``
    * 算术 → 两侧的公共超类型；两边不可提升时取更"具体"的一侧
      （例如 ``col` 类型未知（null）时，结果就是另一边的类型）
    """

    if op in COMPARISON_OPS or op in LOGICAL_OPS or op in KEYWORD_OPS:
        return BOOL
    if op in STRING_OPS:
        return UTF8
    promoted = promote_types(left, right)
    if promoted is not None:
        return promoted
    if isinstance(left, type(NULL)):
        return right
    if isinstance(right, type(NULL)):
        return left
    return left


# ---------------------------------------------------------------------------
# 便捷构造函数
# ---------------------------------------------------------------------------


def col(name: str, type: Optional[DataType] = None) -> ColumnRef:
    """列引用。``col("a")`` 是构造表达式的起点。"""

    return ColumnRef(name, type)


def lit(value: Any, type: Optional[DataType] = None) -> Literal:
    """常量。已经是 :class:`Expr` 的原样返回，方便写通用代码。"""

    if isinstance(value, Expr):
        return value  # type: ignore[return-value]
    return Literal(value, type)


def and_(*exprs: Any) -> Expr:
    """把若干条件串成左结合的 ``AND`` 链。

    刻意**不做任何化简**（不丢 ``TRUE``、不去重）——化简是
    :func:`mini_lancedb.expr.rewrite.simplify` 的职责。构造器保持"忠实"，
    解析器的往返一致才成立：``parse("a AND TRUE").to_sql() == "a AND TRUE"``。
    """

    items = [e if isinstance(e, Expr) else lit(e) for e in exprs if e is not None]
    if not items:
        return Literal(True, BOOL)
    out = items[0]
    for e in items[1:]:
        out = BinaryOp("AND", out, e)
    return out


def or_(*exprs: Any) -> Expr:
    """把若干条件串成左结合的 ``OR`` 链（同样不做化简）。"""

    items = [e if isinstance(e, Expr) else lit(e) for e in exprs if e is not None]
    if not items:
        return Literal(False, BOOL)
    out = items[0]
    for e in items[1:]:
        out = BinaryOp("OR", out, e)
    return out


def not_(expr: Any) -> UnaryOp:
    """逻辑取反。不做双重否定消去，理由同 :func:`and_`。"""

    return UnaryOp("NOT", expr if isinstance(expr, Expr) else lit(expr))


def literal_value(expr: Expr) -> Tuple[bool, Any]:
    """取出"编译期常量"的值，成功返回 ``(True, value)``。

    比 ``isinstance(expr, Literal)`` 多认一种情况：**一元正负号作用在数值
    字面量上**。因为解析器把 ``-1`` 建成了 ``UnaryOp("-", Literal(1))``
    （这样 AST 和 SQL 文本是一一对应的，``to_sql`` 才能往返），
    但 ``a > -1`` 里的 ``-1`` 在索引改写和选择率估计眼里就是**一个常量**。
    少了这一步，所有带负数的范围条件都会白白退化成全表扫描。
    """

    if isinstance(expr, Literal):
        return True, expr.value
    if isinstance(expr, UnaryOp) and expr.op in ("-", "+"):
        ok, inner = literal_value(expr.operand)
        if ok and isinstance(inner, (int, float)) and not isinstance(inner, bool):
            return True, (-inner if expr.op == "-" else inner)
    return False, None


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

_NODE_TYPES: Dict[str, Any] = {}


def _register() -> None:
    for cls in (
        ColumnRef,
        Literal,
        BinaryOp,
        UnaryOp,
        IsNull,
        IsIn,
        Like,
        Between,
        Cast,
        FunctionCall,
    ):
        _NODE_TYPES[cls.node_name] = cls


_register()


def serialize(expr: Expr) -> Dict[str, Any]:
    """AST → 纯 JSON 结构（计划缓存 / explain 输出用）。"""

    if not isinstance(expr, Expr):
        raise PlanError("serialize 只接受 Expr", got=type(expr).__name__)
    return expr.serialize()


def deserialize(obj: Mapping[str, Any]) -> Expr:
    """:func:`serialize` 的逆运算。"""

    if isinstance(obj, Expr):
        return obj
    if not isinstance(obj, Mapping):
        raise PlanError("表达式序列化结构必须是 dict", got=repr(obj))
    node = obj.get("node")
    typ = DataType.from_json(obj["type"]) if "type" in obj else None
    if node == "ColumnRef":
        return ColumnRef(obj["name"], typ)
    if node == "Literal":
        return Literal(obj["value"], typ)
    if node == "BinaryOp":
        return BinaryOp(obj["op"], deserialize(obj["left"]), deserialize(obj["right"]), typ)
    if node == "UnaryOp":
        return UnaryOp(obj["op"], deserialize(obj["operand"]), typ)
    if node == "IsNull":
        return IsNull(deserialize(obj["child"]), bool(obj["negated"]))
    if node == "IsIn":
        return IsIn(
            deserialize(obj["child"]),
            [deserialize(v) for v in obj["values"]],
            bool(obj["negated"]),
        )
    if node == "Like":
        return Like(
            deserialize(obj["child"]),
            deserialize(obj["pattern"]),
            bool(obj["negated"]),
            obj.get("escape"),
        )
    if node == "Between":
        return Between(
            deserialize(obj["child"]),
            deserialize(obj["low"]),
            deserialize(obj["high"]),
            bool(obj["negated"]),
        )
    if node == "Cast":
        return Cast(deserialize(obj["child"]), DataType.from_json(obj["target"]))
    if node == "FunctionCall":
        return FunctionCall(obj["name"], [deserialize(a) for a in obj["args"]], typ)
    raise PlanError(f"未知的表达式节点 {node!r}", known=sorted(_NODE_TYPES))
