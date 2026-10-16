"""AST —— 语法树节点。

ClickHouse 的解析器产出的是一棵**纯数据**的语法树(``IAST`` 家族,
定义在 ``src/Parsers/AST*.h``),它有几个贯穿全局的性质:

.. code-block:: text

   性质 1  节点是**可变的**,而且带 children 列表
           优化器直接在上面改(比如把 WHERE 里的条件下推到 PREWHERE),
           而不是"构造一棵新的树"。这带来性能,也带来一堆
           "谁动了我的树"的调试难题(第 22 章)。

   性质 2  每个节点都有 formatImpl(),能把树**打回 SQL 文本**
           `EXPLAIN AST` 就是它的直接输出;`SHOW CREATE TABLE`
           与 `system.query_log.query` 也靠它。

   性质 3  位置信息(pos)是**可选**的
           有它才能报"Syntax error at position 42",
           但解析器大量使用 `ParserIdentifier` 之类的辅助函数时
           很容易丢掉它 —— 真实 ClickHouse 早期版本的报错常常没有列号。

mini-clickhouse 用 ``@dataclass`` 表达节点。相比真实 ClickHouse 的
``ASTPtr``(``shared_ptr``)体系,Python 的引用语义天然就是共享的,
所以"优化器就地改树"在这里是**默认行为**而不是一个需要小心的开关 ——
第 22 章会讲这个差异带来的实际影响。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..errors import ErrorCode, raise_error

__all__ = [
    "Node",
    "Identifier",
    "Literal",
    "FunctionCall",
    "Lambda",
    "UnaryOp",
    "BinaryOp",
    "CaseExpr",
    "CastExpr",
    "ArrayLiteral",
    "TupleLiteral",
    "MapLiteral",
    "IndexExpr",
    "InExpr",
    "BetweenExpr",
    "IsNullExpr",
    "LikeExpr",
    "StarExpr",
    "AliasExpr",
    "ArrayJoinExpr",
    "IntervalExpr",
    "SubqueryExpr",
    "ParameterExpr",
    "WindowFunction",
    "WindowSpec",
    "WindowFrame",
    "TableReference",
    "SubqueryRef",
    "TableFunctionCall",
    "JoinExpr",
    "SelectQuery",
    "GroupByClause",
    "OrderByElement",
    "LimitByClause",
    "WithElement",
    "InsertQuery",
    "CreateTableQuery",
    "ColumnDefinition",
    "IndexDefinition",
    "ConstraintDefinition",
    "ProjectionDefinition",
    "TTLDefinition",
    "CreateDatabaseQuery",
    "CreateViewQuery",
    "CreateDictionaryQuery",
    "DropQuery",
    "AlterQuery",
    "AlterCommand",
    "RenameQuery",
    "TruncateQuery",
    "OptimizeQuery",
    "ShowQuery",
    "DescribeQuery",
    "ExplainQuery",
    "SystemQuery",
    "SetQuery",
    "UseQuery",
    "KillQuery",
    "GrantQuery",
    "CreateFunctionQuery",
    "ExistsQuery",
    "format_ast",
]


# ══════════════════════════════════════════════════════════════════════════
# 基类
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Node:
    """所有 AST 节点的基类。

    ``pos`` 记录该节点在原始 SQL 里的字符偏移(报错用)。
    ``-1`` 表示"这个节点是解析器合成的,没有对应的原文" ——
    例如优化器插入的常量列。
    """

    pos: int = field(default=-1, kw_only=True)

    def to_sql(self) -> str:  # pragma: no cover - 由子类覆盖
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{type(self).__name__}.to_sql")

    def children(self) -> List["Node"]:
        """返回子节点 —— 遍历/改写/统计用。"""
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{type(self).__name__}.children")

    def __str__(self) -> str:
        return self.to_sql()


# ══════════════════════════════════════════════════════════════════════════
# 表达式
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Identifier(Node):
    """标识符 —— 列名、表名、数据库名、别名。

    ``quoted`` 记录了原文是否带引号。这个信息必须保留,因为
    ``SELECT 1 AS `select``` 里的 ``select`` 是**合法标识符**
    (被引号保护),而裸写的 ``select`` 是关键字。
    真实 ClickHouse 用 ``ASTIdentifier`` 的 ``quote`` 风格来表达同一件事。
    """

    name: str = ""
    quoted: bool = False

    def to_sql(self) -> str:
        if self.quoted:
            return "`" + self.name.replace("`", "\\`") + "`"
        return self.name

    def children(self) -> List[Node]:
        return []


@dataclass
class Literal(Node):
    """字面量 —— 数字、字符串、NULL、布尔。

    ``value`` 存**已经规范化**的 Python 值,``type_name`` 存解析时
    推出来的类型名(可能为 None,留给类型推导阶段决定)。
    """

    value: Any = None
    type_name: Optional[str] = None

    def to_sql(self) -> str:
        if self.value is None:
            return "NULL"
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        if isinstance(self.value, str):
            escaped = self.value.replace("\\", "\\\\").replace("'", "\\'")
            return f"'{escaped}'"
        if isinstance(self.value, (list, tuple)):
            return "[" + ", ".join(Literal(v).to_sql() for v in self.value) + "]"
        return str(self.value)

    def children(self) -> List[Node]:
        return []


@dataclass
class FunctionCall(Node):
    """函数调用。

    ``params`` 是**函数参数**(``quantile(0.9)(x)`` 里的 ``0.9``),
    ``args`` 是**实参**(``x``)。ClickHouse 的语法把两者分开,
    因为参数在**准备阶段**就要确定(它决定选出哪个重载),
    而实参是逐行的数据。
    """

    name: str = ""
    args: List[Node] = field(default_factory=list)
    params: List[Node] = field(default_factory=list)
    #: 该函数是否写了 ``-State``/``-Merge`` 之类的组合器后缀(信息性)
    combinator: Optional[str] = None

    def to_sql(self) -> str:
        base = self.name
        if self.params:
            base += "(" + ", ".join(p.to_sql() for p in self.params) + ")"
        return f"{base}({', '.join(a.to_sql() for a in self.args)})"

    def children(self) -> List[Node]:
        return list(self.params) + list(self.args)


@dataclass
class Lambda(Node):
    """``x -> expr`` 或 ``(x, y) -> expr``。"""

    params: List[str] = field(default_factory=list)
    body: Optional[Node] = None

    def to_sql(self) -> str:
        if len(self.params) == 1:
            return f"{self.params[0]} -> {self.body.to_sql() if self.body else ''}"
        return f"({', '.join(self.params)}) -> {self.body.to_sql() if self.body else ''}"

    def children(self) -> List[Node]:
        return [self.body] if self.body else []


# ── 运算符优先级 —— 只服务于 ``to_sql`` 的括号插入 ─────────────────────
#
# 数字越大**绑定越紧**。这张表与 ``parser.py`` 的递归下降层级一一对应
# (``_parse_or`` → ``_parse_and`` → ``_parse_not`` → ``_parse_comparison``
# → ``_parse_concat`` → ``_parse_additive`` → ``_parse_multiplicative``
# → ``_parse_unary``),但解析器**不查**它 —— 它有自己的那套层级函数。
# 这里的作用是让回打出的文本再解析时得到**同一棵树**。
_BINARY_PRECEDENCE: Dict[str, int] = {
    "OR": 1,
    "AND": 2,
    "=": 4, "==": 4, "!=": 4, "<>": 4, "<": 4, ">": 4, "<=": 4, ">=": 4, "<=>": 4,
    "||": 5,
    "+": 6, "-": 6,
    "*": 7, "/": 7, "%": 7,
}
#: 逻辑非 ``NOT`` —— 比 ``AND`` 紧、比比较运算松(``NOT a = b`` 是 ``NOT (a = b)``)
_PREC_LOGICAL_NOT = 3
#: 符号型一元运算符 ``- + ~ !`` —— 比 ``*`` 紧
_PREC_UNARY = 8
#: 原子(字面量、标识符、函数调用、后缀表达式……)—— 永远不需要加括号
_PREC_ATOM = 10


def _node_precedence(node: Optional[Node]) -> int:
    """一个表达式节点的绑定强度 —— 括号插不插就看它。"""
    if isinstance(node, BinaryOp):
        # 未知运算符按比较运算处理(保守:宁可多加括号也不改变语义)
        return _BINARY_PRECEDENCE.get(node.op, 4)
    if isinstance(node, UnaryOp):
        return _PREC_LOGICAL_NOT if node.op == "NOT" else _PREC_UNARY
    if isinstance(node, (InExpr, BetweenExpr, LikeExpr, IsNullExpr)):
        return 4
    return _PREC_ATOM


def _operand_sql(node: Optional[Node], threshold: int, *, is_right: bool) -> str:
    """打出一个操作数,按优先级/结合性补括号。

    ``threshold`` 是父运算符的优先级。左结合的运算符,**右操作数同级也要
    加括号** —— 否则 ``10 - (3 - 2)`` 会被打成 ``10 - 3 - 2``,重新解析后
    算出 5 而不是 9。左操作数同级则不用(``(a + b) + c`` ≡ ``a + b + c``)。
    """
    if node is None:
        return ""
    text = node.to_sql()
    prec = _node_precedence(node)
    if prec < threshold or (is_right and prec == threshold):
        return f"({text})"
    return text


@dataclass
class UnaryOp(Node):
    op: str = "-"
    operand: Optional[Node] = None

    def to_sql(self) -> str:
        if self.operand is None:
            return self.op
        sep = " " if self.op == "NOT" else ""
        operand = self.operand.to_sql()
        # 前缀一元运算符的操作数在**重新解析**时是按"一元层级"吃的
        # (``_parse_unary``),只能是一个原子/后缀/一元表达式。所以凡是比
        # 一元层级松的东西(任何二元运算)都必须显式加括号 —— 否则
        # ``NOT a * b`` 会被解析成 ``NOT (a * b)`` 而不是 ``(NOT a) * b``。
        need = _node_precedence(self.operand) < _PREC_UNARY
        # 符号型一元运算符的操作数若也是一元运算,显式加括号 —— 免得打出
        # ``--a`` 这种被词法器当成"行注释"开头的文本。
        if not sep and isinstance(self.operand, UnaryOp):
            need = True
        if need:
            operand = f"({operand})"
        return f"{self.op}{sep}{operand}"

    def children(self) -> List[Node]:
        return [self.operand] if self.operand else []


@dataclass
class BinaryOp(Node):
    """二元运算 —— 算术、比较、逻辑、字符串拼接(``||``)。

    ``op`` 存的是 **SQL 里的写法**(``+``、``AND``、``>=``),
    求值阶段再把它映射成同名函数(``plus``、``and``、``greaterOrEquals``)。
    这个"运算符就是函数"的映射是 ClickHouse 的核心设计之一:
    ``a + b`` 与 ``plus(a, b)`` 在 AST 层面就是同一个东西。
    """

    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None

    def to_sql(self) -> str:
        # 按优先级/结合性补括号 —— 否则 ``(2+3)*4`` 会被打成 ``2 + 3 * 4``,
        # 重新解析后算出 14 而不是 20。这一条靠"文本不动点"测试**抓不到**
        # (打出的文本自身仍是稳定的),只有**求值结果**会变。
        prec = _BINARY_PRECEDENCE.get(self.op, 4)
        left = _operand_sql(self.left, prec, is_right=False)
        right = _operand_sql(self.right, prec, is_right=True)
        return f"{left} {self.op} {right}"

    def children(self) -> List[Node]:
        return [c for c in (self.left, self.right) if c is not None]


@dataclass
class CaseExpr(Node):
    """``CASE [expr] WHEN ... THEN ... [ELSE ...] END``。"""

    operand: Optional[Node] = None
    whens: List[Tuple[Node, Node]] = field(default_factory=list)
    else_expr: Optional[Node] = None

    def to_sql(self) -> str:
        parts = ["CASE"]
        if self.operand is not None:
            parts.append(self.operand.to_sql())
        for cond, res in self.whens:
            parts.append(f"WHEN {cond.to_sql()} THEN {res.to_sql()}")
        if self.else_expr is not None:
            parts.append(f"ELSE {self.else_expr.to_sql()}")
        parts.append("END")
        return " ".join(parts)

    def children(self) -> List[Node]:
        out = []
        if self.operand is not None:
            out.append(self.operand)
        for cond, res in self.whens:
            out.extend([cond, res])
        if self.else_expr is not None:
            out.append(self.else_expr)
        return out


@dataclass
class CastExpr(Node):
    """``CAST(expr AS Type)`` 与 ``expr::Type``。"""

    expr: Optional[Node] = None
    type_name: str = ""

    def to_sql(self) -> str:
        return f"CAST({self.expr.to_sql() if self.expr else ''} AS {self.type_name})"

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class ArrayLiteral(Node):
    items: List[Node] = field(default_factory=list)

    def to_sql(self) -> str:
        return "[" + ", ".join(i.to_sql() for i in self.items) + "]"

    def children(self) -> List[Node]:
        return list(self.items)


@dataclass
class TupleLiteral(Node):
    items: List[Node] = field(default_factory=list)

    def to_sql(self) -> str:
        return "(" + ", ".join(i.to_sql() for i in self.items) + ")"

    def children(self) -> List[Node]:
        return list(self.items)


@dataclass
class MapLiteral(Node):
    pairs: List[Tuple[Node, Node]] = field(default_factory=list)

    def to_sql(self) -> str:
        return "{" + ", ".join(f"{k.to_sql()}: {v.to_sql()}" for k, v in self.pairs) + "}"

    def children(self) -> List[Node]:
        out: List[Node] = []
        for k, v in self.pairs:
            out.extend([k, v])
        return out


@dataclass
class IndexExpr(Node):
    """``a[i]``、``t.1``、``m['k']`` —— 数组下标 / 元组元素 / Map 取值。"""

    base: Optional[Node] = None
    index: Optional[Node] = None

    def to_sql(self) -> str:
        b = self.base.to_sql() if self.base else ""
        i = self.index.to_sql() if self.index else ""
        return f"{b}[{i}]" if not _is_number_literal(self.index) else f"{b}.{i}"

    def children(self) -> List[Node]:
        return [c for c in (self.base, self.index) if c is not None]


def _is_number_literal(node: Optional[Node]) -> bool:
    return isinstance(node, Literal) and isinstance(node.value, int) and not isinstance(node.value, bool)


@dataclass
class InExpr(Node):
    """``x IN (...)` / ``x NOT IN (subquery)``。"""

    left: Optional[Node] = None
    right: Optional[Node] = None
    negated: bool = False

    def to_sql(self) -> str:
        op = "NOT IN" if self.negated else "IN"
        return f"{self.left.to_sql() if self.left else ''} {op} {self.right.to_sql() if self.right else ''}"

    def children(self) -> List[Node]:
        return [c for c in (self.left, self.right) if c is not None]


@dataclass
class BetweenExpr(Node):
    expr: Optional[Node] = None
    low: Optional[Node] = None
    high: Optional[Node] = None
    negated: bool = False

    def to_sql(self) -> str:
        op = "NOT BETWEEN" if self.negated else "BETWEEN"
        e = self.expr.to_sql() if self.expr else ""
        lo = self.low.to_sql() if self.low else ""
        hi = self.high.to_sql() if self.high else ""
        return f"{e} {op} {lo} AND {hi}"

    def children(self) -> List[Node]:
        return [c for c in (self.expr, self.low, self.high) if c is not None]


@dataclass
class IsNullExpr(Node):
    expr: Optional[Node] = None
    negated: bool = False

    def to_sql(self) -> str:
        return f"{self.expr.to_sql() if self.expr else ''} IS {'NOT ' if self.negated else ''}NULL"

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class LikeExpr(Node):
    """``LIKE`` / ``ILIKE`` / ``NOT LIKE`` / ``REGEXP``。"""

    left: Optional[Node] = None
    pattern: Optional[Node] = None
    negated: bool = False
    case_insensitive: bool = False
    is_regexp: bool = False

    def to_sql(self) -> str:
        if self.is_regexp:
            op = "NOT REGEXP" if self.negated else "REGEXP"
        else:
            op = ("NOT " if self.negated else "") + ("ILIKE" if self.case_insensitive else "LIKE")
        return f"{self.left.to_sql() if self.left else ''} {op} {self.pattern.to_sql() if self.pattern else ''}"

    def children(self) -> List[Node]:
        return [c for c in (self.left, self.pattern) if c is not None]


@dataclass
class StarExpr(Node):
    """``*`` 以及它的变形 ``* EXCEPT(a, b)`` / ``* REPLACE(x AS y)``。

    ``qualified`` 是 ``t.*`` 里的 ``t``。
    这两个变形在真实 ClickHouse 里是 ``ASTAsterisk`` +
    ``except``/``replace`` 两个列表,我们用同样的结构。
    """

    qualified: Optional[str] = None
    except_columns: List[str] = field(default_factory=list)
    replace_columns: List[Tuple[Node, str]] = field(default_factory=list)

    def to_sql(self) -> str:
        base = f"{self.qualified}." if self.qualified else ""
        out = base + "*"
        if self.except_columns:
            out += " EXCEPT (" + ", ".join(self.except_columns) + ")"
        if self.replace_columns:
            out += " REPLACE (" + ", ".join(
                f"{e.to_sql()} AS {n}" for e, n in self.replace_columns
            ) + ")"
        return out

    def children(self) -> List[Node]:
        return [e for e, _ in self.replace_columns]


@dataclass
class AliasExpr(Node):
    """``expr AS name`` —— 别名。

    真实 ClickHouse 里 ``ASTAlias`` 只包住"表达式 + 名字",
    而 **别名能不能被 WHERE 引用** 由作用域规则决定(见第 07 章):
    ``SELECT a+1 AS b FROM t WHERE b > 2`` 在 ClickHouse 里**是合法的**,
    因为分析期会把 ``b`` 替换成 ``a+1`` —— 这与 SQL 标准不同。
    """

    expr: Optional[Node] = None
    alias: str = ""

    def to_sql(self) -> str:
        return f"{self.expr.to_sql() if self.expr else ''} AS {self.alias}"

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class ArrayJoinExpr(Node):
    """``ARRAY JOIN arr`` / ``LEFT ARRAY JOIN arr``。

    ClickHouse 把它实现成一个**专用的 transform**(而不是函数),
    因为它会改变**行数** —— 普通函数做不到这一点。
    见第 16 章。
    """

    expr: Optional[Node] = None
    left: bool = False
    alias: Optional[str] = None

    def to_sql(self) -> str:
        prefix = "LEFT ARRAY JOIN " if self.left else "ARRAY JOIN "
        out = prefix + (self.expr.to_sql() if self.expr else "")
        if self.alias:
            out += f" AS {self.alias}"
        return out

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class IntervalExpr(Node):
    """``INTERVAL 1 DAY``。"""

    amount: Optional[Node] = None
    kind: str = "DAY"

    def to_sql(self) -> str:
        return f"INTERVAL {self.amount.to_sql() if self.amount else ''} {self.kind}"

    def children(self) -> List[Node]:
        return [self.amount] if self.amount else []


@dataclass
class SubqueryExpr(Node):
    """括号里的子查询(标量上下文 / IN 的右操作数 / FROM 的子查询)。"""

    query: Optional["SelectQuery"] = None
    alias: Optional[str] = None

    def to_sql(self) -> str:
        out = f"({self.query.to_sql() if self.query else ''})"
        if self.alias:
            out += f" AS {self.alias}"
        return out

    def children(self) -> List[Node]:
        return [self.query] if self.query else []


@dataclass
class ParameterExpr(Node):
    """``{name:Type}`` —— 查询参数,客户端绑定后才替换成字面量。"""

    name: str = ""
    type_name: str = ""

    def to_sql(self) -> str:
        return f"{{{self.name}:{self.type_name}}}"

    def children(self) -> List[Node]:
        return []


@dataclass
class WindowFrame(Node):
    """窗口帧 —— ``ROWS BETWEEN 2 PRECEDING AND CURRENT ROW``。

    ``ROWS`` 按**行数**算,``RANGE`` 按**值**算(需要 ORDER BY 的列有序)。
    两者的区别在有重复排序键时非常明显 —— 第 14 章有一个例子:
    ``SUM(x) OVER (ORDER BY k ROWS ...)`` 与 ``RANGE`` 在同一份数据上
    给出不同的结果,因为 ``RANGE`` 会把"值相等的行"看成一行。
    """

    type: str = "ROWS"
    start: Optional[str] = None       # UNBOUNDED PRECEDING / CURRENT ROW / N PRECEDING
    start_offset: Optional[Node] = None
    end: Optional[str] = None
    end_offset: Optional[Node] = None

    def to_sql(self) -> str:
        def part(kind: Optional[str], off: Optional[Node]) -> str:
            if kind in (None, "CURRENT"):
                return "CURRENT ROW"
            if kind == "UNBOUNDED_PRECEDING":
                return "UNBOUNDED PRECEDING"
            if kind == "UNBOUNDED_FOLLOWING":
                return "UNBOUNDED FOLLOWING"
            side = "PRECEDING" if kind == "PRECEDING" else "FOLLOWING"
            return f"{off.to_sql() if off else ''} {side}"

        return f"{self.type} BETWEEN {part(self.start, self.start_offset)} AND {part(self.end, self.end_offset)}"

    def children(self) -> List[Node]:
        return [c for c in (self.start_offset, self.end_offset) if c is not None]


@dataclass
class WindowSpec(Node):
    """``OVER (PARTITION BY ... ORDER BY ... ROWS ...)``。"""

    name: Optional[str] = None
    partition_by: List[Node] = field(default_factory=list)
    order_by: List["OrderByElement"] = field(default_factory=list)
    frame: Optional[WindowFrame] = None

    def to_sql(self) -> str:
        if self.name:
            return self.name
        parts = ["OVER ("]
        inner = []
        if self.partition_by:
            inner.append("PARTITION BY " + ", ".join(e.to_sql() for e in self.partition_by))
        if self.order_by:
            inner.append("ORDER BY " + ", ".join(e.to_sql() for e in self.order_by))
        if self.frame is not None:
            inner.append(self.frame.to_sql())
        parts.append(" ".join(inner))
        parts.append(")")
        return "".join(parts)

    def children(self) -> List[Node]:
        out = list(self.partition_by) + list(self.order_by)
        if self.frame is not None:
            out.append(self.frame)
        return out


@dataclass
class WindowFunction(Node):
    """带窗口的函数调用 —— ``row_number() OVER (...)``。"""

    function: Optional[Node] = None
    window: Optional[WindowSpec] = None

    def to_sql(self) -> str:
        return f"{self.function.to_sql() if self.function else ''} " \
               f"{self.window.to_sql() if self.window else ''}"

    def children(self) -> List[Node]:
        return [c for c in (self.function, self.window) if c is not None]


# ══════════════════════════════════════════════════════════════════════════
# FROM 子句
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class TableReference(Node):
    """一张表的引用。

    ``final`` 对应 ``FROM t FINAL`` —— 它强制在读取时就做 ReplacingMergeTree
    的去重(而不是等后台 merge),代价是**放弃并行读取**(见第 16 章)。
    """

    database: Optional[str] = None
    table: str = ""
    alias: Optional[str] = None
    final: bool = False
    sample: Optional[Node] = None
    table_function: Optional["TableFunctionCall"] = None

    def to_sql(self) -> str:
        if self.table_function is not None:
            out = self.table_function.to_sql()
        else:
            out = f"`{self.database}`." if self.database else ""
            out += f"`{self.table}`"
        if self.final:
            out += " FINAL"
        if self.sample is not None:
            out += f" SAMPLE {self.sample.to_sql()}"
        if self.alias:
            out += f" AS `{self.alias}`"
        return out

    def children(self) -> List[Node]:
        out: List[Node] = []
        if self.table_function is not None:
            out.append(self.table_function)
        if self.sample is not None:
            out.append(self.sample)
        return out

    def qualified_name(self) -> str:
        return f"{self.database}.{self.table}" if self.database else self.table


@dataclass
class TableFunctionCall(Node):
    """表函数 —— ``numbers(10)``、``file('x.csv', 'CSV')``、``remote(...)``。"""

    name: str = ""
    args: List[Node] = field(default_factory=list)
    alias: Optional[str] = None

    def to_sql(self) -> str:
        return f"{self.name}({', '.join(a.to_sql() for a in self.args)})"

    def children(self) -> List[Node]:
        return list(self.args)


@dataclass
class SubqueryRef(Node):
    """``FROM (SELECT ...) AS x``。"""

    query: Optional["SelectQuery"] = None
    alias: Optional[str] = None

    def to_sql(self) -> str:
        out = f"({self.query.to_sql() if self.query else ''})"
        if self.alias:
            out += f" AS `{self.alias}`"
        return out

    def children(self) -> List[Node]:
        return [self.query] if self.query else []


@dataclass
class JoinExpr(Node):
    """``JOIN``。

    ``strictness`` 与 ``kind`` 是两个**正交**的维度,这是 ClickHouse
    JOIN 语义里最关键的区分(第 13 章):

    .. code-block:: text

       kind(方向)          strictness(匹配策略)
       INNER                ALL     —— 保留所有匹配(笛卡尔)
       LEFT OUTER           ANY     —— 右表只取第一个匹配
       RIGHT OUTER          ASOF    —— 按"最近的一行"匹配(时序数据)
       FULL OUTER           SEMI    —— 只保留左表中"有匹配"的行
       CROSS                ANTI    —— 只保留左表中"没匹配"的行
       PASTE                ASOF|ANY 等组合

    ``ANY`` 是 ClickHouse 的**默认**(``join_default_strictness = 'ALL'``
    在新版本里改成了 ALL,但 ``ANY`` 仍然是最高频的写法,因为
    "左表一行匹配右表多行"在宽表 join 里通常是数据错误)。
    """

    left: Optional[Node] = None
    right: Optional[Node] = None
    kind: str = "INNER"           # INNER / LEFT / RIGHT / FULL / CROSS / PASTE
    strictness: str = "ALL"       # ALL / ANY / ASOF / SEMI / ANTI
    on_expr: Optional[Node] = None
    using_columns: List[str] = field(default_factory=list)
    global_: bool = False         # GLOBAL JOIN —— 分布式查询用

    def to_sql(self) -> str:
        parts = [self.left.to_sql() if self.left else ""]
        parts.append("GLOBAL " if self.global_ else "")
        parts.append(("" if self.kind == "INNER" else self.kind + " ") + "JOIN")
        parts.append(self.right.to_sql() if self.right else "")
        if self.using_columns:
            parts.append("USING (" + ", ".join(self.using_columns) + ")")
        elif self.on_expr is not None:
            parts.append("ON " + self.on_expr.to_sql())
        return " ".join(p for p in parts if p)

    def children(self) -> List[Node]:
        out: List[Node] = []
        for c in (self.left, self.right, self.on_expr):
            if c is not None:
                out.append(c)
        return out


# ══════════════════════════════════════════════════════════════════════════
# SELECT
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class WithElement(Node):
    """``WITH expr AS name`` 或 ``WITH name AS (subquery)``。

    两种形态的**求值时机完全不同**,这是 CTE 的关键:

    * ``WITH (SELECT max(x) FROM t) AS m SELECT x/m FROM t`` ——
      **标量子查询**,在查询开始前算一次,结果当常量用;
    * ``WITH t2 AS (SELECT * FROM t) SELECT * FROM t2`` ——
      **命名子查询**,会被**内联展开**(不是物化!),
      所以 ``FROM t2`` 出现两次就会扫两遍 ``t``。
      这是 ClickHouse 与 PostgreSQL 的 CTE 语义的重大差别
      (PG 会物化,ClickHouse 会展开)。第 07 章有实测。
    """

    name: str = ""
    expr: Optional[Node] = None
    is_subquery: bool = False

    def to_sql(self) -> str:
        if self.is_subquery:
            return f"{self.name} AS ({self.expr.to_sql() if self.expr else ''})"
        return f"{self.expr.to_sql() if self.expr else ''} AS {self.name}"

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class OrderByElement(Node):
    """``ORDER BY expr [ASC|DESC] [NULLS FIRST|LAST] [COLLATE ...]``。

    默认的 NULL 位置由**方向**决定,而不是固定的一侧 ——
    详见 :meth:`minich.util.keys.KeyDescription` 的说明。
    """

    expr: Optional[Node] = None
    direction: str = "ASC"
    nulls: Optional[str] = None       # None / "FIRST" / "LAST"
    collate: Optional[str] = None
    with_fill: Optional[Any] = None

    def to_sql(self) -> str:
        out = self.expr.to_sql() if self.expr else ""
        out += f" {self.direction}"
        if self.nulls:
            out += f" NULLS {self.nulls}"
        if self.collate:
            out += f" COLLATE '{self.collate}'"
        return out

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class GroupByClause(Node):
    """``GROUP BY`` 及其全部修饰符。

    这些修饰符产生的行**不是普通分组**,而是"小计行",
    它们在后续阶段的处理方式各不相同 —— 第 12 章会用一张表说明
    ``WITH TOTALS`` / ``WITH ROLLUP`` / ``WITH CUBE`` / ``GROUPING SETS``
    各自产生的行数公式与 NULL 填充规则。
    """

    elements: List[Node] = field(default_factory=list)
    with_rollup: bool = False
    with_cube: bool = False
    with_totals: bool = False
    grouping_sets: List[List[Node]] = field(default_factory=list)

    def to_sql(self) -> str:
        out = "GROUP BY "
        if self.grouping_sets:
            out += "GROUPING SETS (" + ", ".join(
                "(" + ", ".join(e.to_sql() for e in s) + ")" for s in self.grouping_sets
            ) + ")"
        else:
            out += ", ".join(e.to_sql() for e in self.elements)
        if self.with_rollup:
            out += " WITH ROLLUP"
        if self.with_cube:
            out += " WITH CUBE"
        if self.with_totals:
            out += " WITH TOTALS"
        return out

    def children(self) -> List[Node]:
        out = list(self.elements)
        for s in self.grouping_sets:
            out.extend(s)
        return out


@dataclass
class LimitByClause(Node):
    """``LIMIT n [OFFSET m] BY expr1, expr2`` —— 每组取前 n 行。

    它在语义上等价于窗口函数 ``row_number() OVER (PARTITION BY ...) <= n``,
    但 ClickHouse 有专门的 ``LimitByTransform`` 实现,代价低得多。
    这是"取每个用户最近 3 条记录"这类需求的最优写法(第 14 章)。
    """

    limit: Optional[Node] = None
    offset: Optional[Node] = None
    elements: List[Node] = field(default_factory=list)

    def to_sql(self) -> str:
        out = f"LIMIT {self.limit.to_sql() if self.limit else ''}"
        if self.offset is not None:
            out += f" OFFSET {self.offset.to_sql()}"
        out += " BY " + ", ".join(e.to_sql() for e in self.elements)
        return out

    def children(self) -> List[Node]:
        return [c for c in ([self.limit, self.offset] + list(self.elements)) if c is not None]


@dataclass
class SelectQuery(Node):
    """``SELECT`` 查询 —— 教程里出现频率最高的节点。

    字段顺序与 SQL 的**执行顺序**无关(那是第 10 章的内容),
    而是与真实 ClickHouse 的 ``ASTSelectQuery`` 一致。
    """

    #: WITH 子句
    with_exprs: List[WithElement] = field(default_factory=list)
    #: SELECT DISTINCT
    distinct: bool = False
    #: 选择的表达式(可能含 AliasExpr / StarExpr)
    selects: List[Node] = field(default_factory=list)
    #: FROM
    from_: Optional[Node] = None
    #: PREWHERE —— 见下方说明
    prewhere: Optional[Node] = None
    where: Optional[Node] = None
    group_by: Optional[GroupByClause] = None
    having: Optional[Node] = None
    #: WINDOW 子句定义的命名窗口
    windows: List[Tuple[str, WindowSpec]] = field(default_factory=list)
    order_by: List[OrderByElement] = field(default_factory=list)
    limit_by: Optional[LimitByClause] = None
    limit: Optional[Node] = None
    offset: Optional[Node] = None
    settings: List[Tuple[str, Node]] = field(default_factory=list)
    #: ARRAY JOIN 子句(可以有多个,``ARRAY JOIN a, b``)
    array_joins: List[ArrayJoinExpr] = field(default_factory=list)
    #: UNION ALL 的后续分支
    unions: List["SelectQuery"] = field(default_factory=list)
    #: UNION DISTINCT 而不是 UNION ALL
    union_distinct: bool = False
    #: INTO OUTFILE
    into_outfile: Optional[str] = None
    #: 末尾的 FORMAT
    format: Optional[str] = None
    #: FROM t FINAL 也可以用这个字段表达(与 TableReference.final 等价)
    final: bool = False

    def to_sql(self) -> str:
        parts: List[str] = []
        if self.with_exprs:
            parts.append("WITH " + ", ".join(e.to_sql() for e in self.with_exprs))
        head = "SELECT "
        if self.distinct:
            head += "DISTINCT "
        head += ", ".join(s.to_sql() for s in self.selects)
        parts.append(head)
        if self.from_ is not None:
            parts.append("FROM " + self.from_.to_sql())
        if self.array_joins:
            parts.extend(a.to_sql() for a in self.array_joins)
        if self.prewhere is not None:
            parts.append("PREWHERE " + self.prewhere.to_sql())
        if self.where is not None:
            parts.append("WHERE " + self.where.to_sql())
        if self.group_by is not None:
            parts.append(self.group_by.to_sql())
        if self.having is not None:
            parts.append("HAVING " + self.having.to_sql())
        if self.windows:
            parts.append("WINDOW " + ", ".join(
                f"{n} AS ({s.to_sql()[6:-1]})" for n, s in self.windows
            ))
        if self.order_by:
            parts.append("ORDER BY " + ", ".join(e.to_sql() for e in self.order_by))
        if self.limit_by is not None:
            parts.append(self.limit_by.to_sql())
        if self.limit is not None:
            parts.append("LIMIT " + self.limit.to_sql())
            if self.offset is not None:
                parts.append("OFFSET " + self.offset.to_sql())
        if self.settings:
            parts.append("SETTINGS " + ", ".join(f"{k} = {v.to_sql()}" for k, v in self.settings))
        out = " ".join(parts)
        if self.unions:
            op = "UNION DISTINCT" if self.union_distinct else "UNION ALL"
            out += " " + op + " " + (" " + op + " ").join(u.to_sql() for u in self.unions)
        if self.format:
            out += " FORMAT " + self.format
        return out

    def children(self) -> List[Node]:
        out: List[Node] = []
        out.extend(self.with_exprs)
        out.extend(self.selects)
        if self.from_ is not None:
            out.append(self.from_)
        out.extend(self.array_joins)
        for c in (self.prewhere, self.where, self.having, self.limit, self.offset):
            if c is not None:
                out.append(c)
        if self.group_by is not None:
            out.append(self.group_by)
        if self.limit_by is not None:
            out.append(self.limit_by)
        out.extend(self.order_by)
        out.extend(u for _, u in self.windows)
        out.extend(u for _, v in self.settings for u in (v,))
        out.extend(self.unions)
        return out

    def is_aggregating(self) -> bool:
        """是否是一条聚合查询 —— GROUP BY 或者 SELECT 列表里有聚合函数。

        .. note::

           真实 ClickHouse 的判断要复杂得多(要看是否有
           ``HAVING``、是否有窗口函数、是否 ``SELECT`` 里混了
           非聚合列等)。mini-clickhouse 只做这个粗判,
           真正的聚合判定发生在表达式分析阶段(第 12 章)。
        """
        if self.group_by is not None:
            return True
        return any(_contains_aggregate(s) for s in self.selects)


def _contains_aggregate(node: Optional[Node]) -> bool:
    """粗略判断一个表达式里是否含聚合函数(名字以常见聚合函数开头)。

    真正的判定要用**聚合函数注册表**(第 12 章),这里只做语法层面的探测,
    因为它被 :meth:`SelectQuery.is_aggregating` 用于早期决策,
    此时还没有类型信息。
    """
    if node is None:
        return False
    if isinstance(node, FunctionCall):
        # 聚合函数注册表是**可选依赖** —— 语法层不该硬依赖执行层,
        # 否则只做语法分析(EXPLAIN AST、SHOW CREATE 回打成 SQL)的场景
        # 就被迫加载整个聚合函数库。真实 ClickHouse 的 AST 层同样不依赖
        # AggregateFunctions,判定发生在 Analysis 阶段。
        try:
            from ..aggregates import aggregate_function_exists
            from ..aggregates import split_combinators

            lowered = node.name.lower()
            if aggregate_function_exists(lowered):
                return True
            base, _, _, _ = split_combinators(lowered)
            if aggregate_function_exists(base):
                return True
        except ImportError:  # pragma: no cover - 只在聚合模块缺失时走到
            pass
    return any(_contains_aggregate(c) for c in node.children())


# ══════════════════════════════════════════════════════════════════════════
# INSERT / DDL
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class InsertQuery(Node):
    """``INSERT INTO t [(cols)] VALUES (...)`` / ``... SELECT ...`` / ``... FORMAT``。"""

    database: Optional[str] = None
    table: str = ""
    columns: List[str] = field(default_factory=list)
    #: VALUES 里的数据(每行是表达式列表)
    values: List[List[Node]] = field(default_factory=list)
    #: INSERT ... SELECT
    select: Optional[SelectQuery] = None
    #: INSERT ... FORMAT X + 随后的原始数据(客户端会把数据附加在语句后)
    format: Optional[str] = None
    data: Optional[str] = None
    #: INSERT INTO FUNCTION 表函数
    table_function: Optional[TableFunctionCall] = None
    settings: List[Tuple[str, Node]] = field(default_factory=list)

    def to_sql(self) -> str:
        out = "INSERT INTO "
        if self.table_function is not None:
            out += "FUNCTION " + self.table_function.to_sql()
        else:
            out += (f"`{self.database}`." if self.database else "") + f"`{self.table}`"
        if self.columns:
            out += " (" + ", ".join(self.columns) + ")"
        if self.values:
            out += " VALUES " + ", ".join(
                "(" + ", ".join(v.to_sql() for v in row) + ")" for row in self.values
            )
        elif self.select is not None:
            out += " " + self.select.to_sql()
        elif self.format:
            out += " FORMAT " + self.format
        return out

    def children(self) -> List[Node]:
        out: List[Node] = []
        for row in self.values:
            out.extend(row)
        if self.select is not None:
            out.append(self.select)
        if self.table_function is not None:
            out.append(self.table_function)
        return out


@dataclass
class ColumnDefinition(Node):
    """建表时的一列。

    ``default_kind`` 是 ``DEFAULT`` / ``MATERIALIZED`` / ``ALIAS`` 三者之一,
    它们的语义差别很大(第 06 章):

    * ``DEFAULT expr`` —— 插入时**没有**提供该列才用表达式;提供了就以提供的为准;
    * ``MATERIALIZED expr`` —— **永远**由表达式算,用户插入该列会报错;
    * ``ALIAS expr`` —— 不落盘,只在查询时替换(相当于一个列级别的宏);
    * ``EPHEMERAL expr`` —— 参与计算但不落盘也不可查询(用于给
      DEFAULT 提供中间值)。

    再加 ``TTL`` 与 ``CODEC``,一个列定义能携带的信息比大多数数据库多得多。
    """

    name: str = ""
    type_name: str = ""
    default_kind: Optional[str] = None       # DEFAULT / MATERIALIZED / ALIAS / EPHEMERAL
    default_expr: Optional[Node] = None
    codec: Optional[str] = None
    ttl: Optional[Node] = None
    comment: Optional[str] = None
    primary_key: bool = False
    not_null: bool = False
    auto_increment: bool = False

    def to_sql(self) -> str:
        out = f"`{self.name}` {self.type_name}"
        if self.not_null:
            out += " NOT NULL"
        if self.default_kind and self.default_expr is not None:
            out += f" {self.default_kind} {self.default_expr.to_sql()}"
        if self.comment:
            out += f" COMMENT '{self.comment}'"
        if self.codec:
            out += f" CODEC({self.codec})"
        if self.ttl is not None:
            out += f" TTL {self.ttl.to_sql()}"
        return out

    def children(self) -> List[Node]:
        out = []
        if self.default_expr is not None:
            out.append(self.default_expr)
        if self.ttl is not None:
            out.append(self.ttl)
        return out


@dataclass
class IndexDefinition(Node):
    """数据跳过索引 —— ``INDEX name expr TYPE minmax GRANULARITY 1``。"""

    name: str = ""
    expr: Optional[Node] = None
    type: str = "minmax"
    type_params: List[Any] = field(default_factory=list)
    granularity: int = 1

    def to_sql(self) -> str:
        tp = self.type
        if self.type_params:
            tp += "(" + ", ".join(str(p) for p in self.type_params) + ")"
        return (f"INDEX `{self.name}` {self.expr.to_sql() if self.expr else ''} "
                f"TYPE {tp} GRANULARITY {self.granularity}")

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class ConstraintDefinition(Node):
    """``CONSTRAINT name CHECK expr`` —— 插入时的行级校验。"""

    name: str = ""
    expr: Optional[Node] = None
    kind: str = "CHECK"     # CHECK / ASSUME

    def to_sql(self) -> str:
        return f"CONSTRAINT `{self.name}` {self.kind} {self.expr.to_sql() if self.expr else ''}"

    def children(self) -> List[Node]:
        return [self.expr] if self.expr else []


@dataclass
class ProjectionDefinition(Node):
    """投影 —— 表内部的"物化视图",对用户透明。

    ``PROJECTION p (SELECT a, sum(b) GROUP BY a)`` 会额外维护一份
    "按 a 预聚合"的数据,查询命中时直接读它(第 11 章)。
    """

    name: str = ""
    query: Optional[SelectQuery] = None

    def to_sql(self) -> str:
        return f"PROJECTION `{self.name}` ({self.query.to_sql() if self.query else ''})"

    def children(self) -> List[Node]:
        return [self.query] if self.query else []


@dataclass
class TTLDefinition(Node):
    """``TTL expr [DELETE|TO DISK|TO VOLUME|RECOMPRESS CODEC] [WHERE ...] [GROUP BY ...]``。"""

    expr: Optional[Node] = None
    action: str = "DELETE"
    target: Optional[str] = None
    codec: Optional[str] = None
    where: Optional[Node] = None
    group_by: List[Node] = field(default_factory=list)
    set_exprs: List[Tuple[str, Node]] = field(default_factory=list)

    def to_sql(self) -> str:
        out = f"TTL {self.expr.to_sql() if self.expr else ''}"
        if self.action == "DELETE":
            pass
        elif self.action in ("TO DISK", "TO VOLUME"):
            out += f" {self.action} '{self.target}'"
        elif self.action == "RECOMPRESS":
            out += f" RECOMPRESS CODEC({self.codec})"
        if self.where is not None:
            out += f" WHERE {self.where.to_sql()}"
        if self.group_by:
            out += " GROUP BY " + ", ".join(e.to_sql() for e in self.group_by)
        if self.set_exprs:
            out += " SET " + ", ".join(f"{k} = {v.to_sql()}" for k, v in self.set_exprs)
        return out

    def children(self) -> List[Node]:
        out = []
        if self.expr is not None:
            out.append(self.expr)
        if self.where is not None:
            out.append(self.where)
        out.extend(self.group_by)
        out.extend(v for _, v in self.set_exprs)
        return out


@dataclass
class CreateTableQuery(Node):
    """``CREATE TABLE``。"""

    database: Optional[str] = None
    table: str = ""
    columns: List[ColumnDefinition] = field(default_factory=list)
    indexes: List[IndexDefinition] = field(default_factory=list)
    constraints: List[ConstraintDefinition] = field(default_factory=list)
    projections: List[ProjectionDefinition] = field(default_factory=list)
    #: 引擎名与参数,例如 ``MergeTree`` / ``Distributed`` + ['cluster', 'db', 't']
    engine: str = ""
    engine_params: List[Any] = field(default_factory=list)
    order_by: List[Node] = field(default_factory=list)
    partition_by: Optional[Node] = None
    primary_key: List[Node] = field(default_factory=list)
    sample_by: Optional[Node] = None
    ttl: List[TTLDefinition] = field(default_factory=list)
    settings: List[Tuple[str, Any]] = field(default_factory=list)
    if_not_exists: bool = False
    temporary: bool = False
    #: ``CREATE TABLE t AS other`` 或 ``AS SELECT``
    as_table: Optional[TableReference] = None
    as_select: Optional[SelectQuery] = None
    cluster: Optional[str] = None
    comment: Optional[str] = None
    uuid: Optional[str] = None

    def to_sql(self) -> str:
        out = "CREATE "
        if self.temporary:
            out += "TEMPORARY "
        out += "TABLE "
        if self.if_not_exists:
            out += "IF NOT EXISTS "
        out += (f"`{self.database}`." if self.database else "") + f"`{self.table}`"
        if self.cluster:
            out += f" ON CLUSTER {self.cluster}"
        body: List[str] = []
        body.extend(c.to_sql() for c in self.columns)
        body.extend(i.to_sql() for i in self.indexes)
        body.extend(c.to_sql() for c in self.constraints)
        body.extend(p.to_sql() for p in self.projections)
        if body:
            out += " (\n    " + ",\n    ".join(body) + "\n)"
        if self.engine:
            params = "(" + ", ".join(_quote_param(p) for p in self.engine_params) + ")" \
                if self.engine_params else ""
            out += f" ENGINE = {self.engine}{params}"
        if self.partition_by is not None:
            out += f" PARTITION BY {self.partition_by.to_sql()}"
        if self.primary_key:
            out += " PRIMARY KEY (" + ", ".join(e.to_sql() for e in self.primary_key) + ")"
        if self.order_by:
            out += " ORDER BY (" + ", ".join(_sorting_key_sql(e) for e in self.order_by) + ")"
        if self.sample_by is not None:
            out += f" SAMPLE BY {self.sample_by.to_sql()}"
        for t in self.ttl:
            out += " " + t.to_sql()
        if self.as_table is not None:
            out += f" AS {self.as_table.to_sql()}"
        elif self.as_select is not None:
            out += f" AS {self.as_select.to_sql()}"
        if self.settings:
            out += " SETTINGS " + ", ".join(f"{k} = {v}" for k, v in self.settings)
        if self.comment:
            out += f" COMMENT '{self.comment}'"
        return out

    def children(self) -> List[Node]:
        out: List[Node] = []
        for c in self.columns:
            out.extend(c.children())
        for i in self.indexes:
            out.extend(i.children())
        out.extend(self.order_by)
        if self.partition_by is not None:
            out.append(self.partition_by)
        out.extend(self.primary_key)
        if self.as_select is not None:
            out.append(self.as_select)
        return out

    def get_column(self, name: str) -> Optional[ColumnDefinition]:
        for c in self.columns:
            if c.name == name:
                return c
        return None


def _quote_param(p: Any) -> str:
    if isinstance(p, str) and not p.replace("_", "").isalnum():
        return f"'{p}'"
    return str(p)


def _sorting_key_sql(e: Any) -> str:
    """回打一个排序键元素,带上方向(``k DESC``)。

    方向是**附加属性**(解析器挂在表达式节点上,见
    ``Parser._parse_key_expression_list``)—— 没有它的节点一律按 ``ASC``,
    于是回打结果与以前完全一样(只有真正写了 ``DESC`` 才会有差别)。
    """
    text = e.to_sql()
    if str(getattr(e, "direction", "ASC")).upper() == "DESC":
        text += " DESC"
    return text


@dataclass
class CreateDatabaseQuery(Node):
    name: str = ""
    engine: str = "Atomic"
    engine_params: List[Any] = field(default_factory=list)
    if_not_exists: bool = False
    comment: Optional[str] = None

    def to_sql(self) -> str:
        out = "CREATE DATABASE "
        if self.if_not_exists:
            out += "IF NOT EXISTS "
        out += f"`{self.name}`"
        if self.engine and self.engine != "Atomic":
            out += f" ENGINE = {self.engine}"
        return out

    def children(self) -> List[Node]:
        return []


@dataclass
class CreateViewQuery(Node):
    database: Optional[str] = None
    name: str = ""
    select: Optional[SelectQuery] = None
    materialized: bool = False
    to_table: Optional[TableReference] = None
    engine: str = ""
    engine_params: List[Any] = field(default_factory=list)
    order_by: List[Node] = field(default_factory=list)
    partition_by: Optional[Node] = None
    primary_key: List[Node] = field(default_factory=list)
    if_not_exists: bool = False
    populate: bool = False
    cluster: Optional[str] = None

    def to_sql(self) -> str:
        out = "CREATE "
        if self.materialized:
            out += "MATERIALIZED "
        out += "VIEW "
        if self.if_not_exists:
            out += "IF NOT EXISTS "
        out += (f"`{self.database}`." if self.database else "") + f"`{self.name}`"
        if self.to_table is not None:
            out += f" TO {self.to_table.to_sql()}"
        elif self.engine:
            out += f" ENGINE = {self.engine}"
            if self.order_by:
                out += " ORDER BY (" + ", ".join(_sorting_key_sql(e) for e in self.order_by) + ")"
        # ``POPULATE`` 打在 ``AS`` **之前** —— 与真实 ClickHouse 的语法
        # 一致,也是回打成 SQL 后能再次解析回来的唯一位置
        # (打在末尾会被当成 SELECT 里最后一个表达式的隐式别名)。
        if self.populate:
            out += " POPULATE"
        if self.select is not None:
            out += " AS " + self.select.to_sql()
        return out

    def children(self) -> List[Node]:
        return [self.select] if self.select else []


@dataclass
class CreateDictionaryQuery(Node):
    database: Optional[str] = None
    name: str = ""
    attributes: List[Tuple[str, str, Optional[Node]]] = field(default_factory=list)
    primary_key: List[str] = field(default_factory=list)
    source: Optional[str] = None
    source_params: List[Any] = field(default_factory=list)
    layout: str = "flat"
    layout_params: List[Any] = field(default_factory=list)
    lifetime: Optional[str] = None
    if_not_exists: bool = False
    comment: Optional[str] = None

    def to_sql(self) -> str:
        attrs = ",\n    ".join(
            f"{n} {t}" + (f" DEFAULT {e.to_sql()}" if e is not None else "")
            for n, t, e in self.attributes
        )
        out = "CREATE DICTIONARY "
        if self.if_not_exists:
            out += "IF NOT EXISTS "
        out += (f"`{self.database}`." if self.database else "") + f"`{self.name}`\n(\n    {attrs}\n)"
        out += "\nPRIMARY KEY " + ", ".join(self.primary_key)
        out += f"\nSOURCE({self.source}(" + ", ".join(str(p) for p in self.source_params) + "))"
        out += f"\nLAYOUT({self.layout}" + (
            "(" + ", ".join(str(p) for p in self.layout_params) + ")" if self.layout_params else ""
        ) + ")"
        if self.lifetime:
            out += f"\nLIFETIME({self.lifetime})"
        return out

    def children(self) -> List[Node]:
        return [e for _, _, e in self.attributes if e is not None]


@dataclass
class CreateFunctionQuery(Node):
    """``CREATE FUNCTION f AS (x) -> x * 2`` —— UDF(可执行的 lambda)。"""

    name: str = ""
    body: Optional[Node] = None
    if_not_exists: bool = False

    def to_sql(self) -> str:
        return f"CREATE FUNCTION {self.name} AS {self.body.to_sql() if self.body else ''}"

    def children(self) -> List[Node]:
        return [self.body] if self.body else []


@dataclass
class DropQuery(Node):
    kind: str = "TABLE"          # TABLE / DATABASE / DICTIONARY / VIEW / FUNCTION / USER / ROLE
    names: List[TableReference] = field(default_factory=list)
    if_exists: bool = False
    sync: bool = False
    cluster: Optional[str] = None
    no_delay: bool = False

    def to_sql(self) -> str:
        out = f"DROP {self.kind} "
        if self.if_exists:
            out += "IF EXISTS "
        out += ", ".join(n.to_sql() for n in self.names)
        if self.sync:
            out += " SYNC"
        return out

    def children(self) -> List[Node]:
        return list(self.names)


@dataclass
class AlterCommand(Node):
    """``ALTER TABLE`` 的一条子命令。"""

    kind: str = ""          # ADD_COLUMN / DROP_COLUMN / MODIFY_COLUMN / RENAME_COLUMN / UPDATE / DELETE / ...
    column: Optional[ColumnDefinition] = None
    name: Optional[str] = None
    new_name: Optional[str] = None
    expr: Optional[Node] = None
    partition: Optional[Node] = None
    predicate: Optional[Node] = None
    index: Optional[IndexDefinition] = None
    ttl: Optional[TTLDefinition] = None
    setting_name: Optional[str] = None
    setting_value: Optional[Any] = None
    update_assignments: List[Tuple[str, Node]] = field(default_factory=list)

    def to_sql(self) -> str:
        if self.kind == "ADD_COLUMN":
            return "ADD COLUMN " + (self.column.to_sql() if self.column else "")
        if self.kind == "DROP_COLUMN":
            return f"DROP COLUMN {self.name}"
        if self.kind == "MODIFY_COLUMN":
            return "MODIFY COLUMN " + (self.column.to_sql() if self.column else "")
        if self.kind == "RENAME_COLUMN":
            return f"RENAME COLUMN {self.name} TO {self.new_name}"
        if self.kind == "COMMENT_COLUMN":
            return f"COMMENT COLUMN {self.name} '{self.setting_value}'"
        if self.kind == "UPDATE":
            return "UPDATE " + ", ".join(f"{k} = {v.to_sql()}" for k, v in self.update_assignments) + \
                f" WHERE {self.predicate.to_sql() if self.predicate else ''}"
        if self.kind == "DELETE":
            return f"DELETE WHERE {self.predicate.to_sql() if self.predicate else ''}"
        if self.kind == "DROP_PARTITION":
            return f"DROP PARTITION {self.partition.to_sql() if self.partition else ''}"
        if self.kind == "DROP_PART":
            return "DROP PART " + (self.partition.to_sql() if self.partition else "")
        if self.kind == "DETACH_PARTITION":
            return f"DETACH PARTITION {self.partition.to_sql() if self.partition else ''}"
        if self.kind == "ATTACH_PARTITION":
            return f"ATTACH PARTITION {self.partition.to_sql() if self.partition else ''}"
        if self.kind == "MOVE_PARTITION":
            return f"MOVE PARTITION {self.partition.to_sql() if self.partition else ''}"
        if self.kind == "ADD_INDEX":
            return "ADD " + (self.index.to_sql() if self.index else "")
        if self.kind == "DROP_INDEX":
            return f"DROP INDEX {self.name}"
        if self.kind == "MODIFY_TTL":
            return "MODIFY " + (self.ttl.to_sql() if self.ttl else "")
        if self.kind == "MODIFY_SETTING":
            return f"MODIFY SETTING {self.setting_name} = {self.setting_value}"
        if self.kind == "RESET_SETTING":
            return f"RESET SETTING {self.setting_name}"
        if self.kind == "MODIFY_ORDER_BY":
            return f"MODIFY ORDER BY ({self.expr.to_sql() if self.expr else ''})"
        if self.kind == "FREEZE":
            return "FREEZE" + (f" PARTITION {self.partition.to_sql()}" if self.partition else "")
        if self.kind == "ATTACH_PART":
            return "ATTACH PART " + (self.name or "")
        if self.kind == "CLEAR_COLUMN":
            return f"CLEAR COLUMN {self.name}" + (
                f" IN PARTITION {self.partition.to_sql()}" if self.partition else ""
            )
        return self.kind

    def children(self) -> List[Node]:
        out: List[Node] = []
        if self.column is not None:
            out.extend(self.column.children())
        for c in (self.expr, self.partition, self.predicate):
            if c is not None:
                out.append(c)
        if self.index is not None:
            out.extend(self.index.children())
        out.extend(v for _, v in self.update_assignments)
        return out


@dataclass
class AlterQuery(Node):
    database: Optional[str] = None
    table: str = ""
    commands: List[AlterCommand] = field(default_factory=list)
    if_exists: bool = False
    cluster: Optional[str] = None

    def to_sql(self) -> str:
        out = "ALTER TABLE "
        if self.if_exists:
            out += "IF EXISTS "
        out += (f"`{self.database}`." if self.database else "") + f"`{self.table}` "
        out += ", ".join(c.to_sql() for c in self.commands)
        return out

    def children(self) -> List[Node]:
        out: List[Node] = []
        for c in self.commands:
            out.extend(c.children())
        return out


@dataclass
class RenameQuery(Node):
    pairs: List[Tuple[TableReference, TableReference]] = field(default_factory=list)

    def to_sql(self) -> str:
        return "RENAME TABLE " + ", ".join(f"{a.to_sql()} TO {b.to_sql()}" for a, b in self.pairs)

    def children(self) -> List[Node]:
        return [x for pair in self.pairs for x in pair]


@dataclass
class TruncateQuery(Node):
    database: Optional[str] = None
    table: str = ""
    if_exists: bool = False

    def to_sql(self) -> str:
        out = "TRUNCATE TABLE "
        if self.if_exists:
            out += "IF EXISTS "
        return out + (f"`{self.database}`." if self.database else "") + f"`{self.table}`"

    def children(self) -> List[Node]:
        return []


@dataclass
class OptimizeQuery(Node):
    database: Optional[str] = None
    table: str = ""
    partition: Optional[Node] = None
    final: bool = False
    deduplicate: bool = False
    cleanup: bool = False

    def to_sql(self) -> str:
        out = "OPTIMIZE TABLE " + (f"`{self.database}`." if self.database else "") + f"`{self.table}`"
        if self.partition is not None:
            out += f" PARTITION {self.partition.to_sql()}"
        if self.final:
            out += " FINAL"
        if self.deduplicate:
            out += " DEDUPLICATE"
        return out

    def children(self) -> List[Node]:
        return [self.partition] if self.partition else []


@dataclass
class ShowQuery(Node):
    """``SHOW TABLES`` / ``SHOW CREATE TABLE`` / ``SHOW DATABASES`` …

    ``kind`` 取值:``TABLES``、``DATABASES``、``CREATE``、``COLUMNS``、
    ``DICTIONARIES``、``SETTINGS``、``PROCESSLIST``、``CLUSTERS``、
    ``ENGINES``、``FUNCTIONS``、``GRANTS``、``USERS``、``ROLES``。
    """

    kind: str = "TABLES"
    database: Optional[str] = None
    table: Optional[str] = None
    like: Optional[str] = None
    where: Optional[Node] = None
    from_: Optional[Node] = None
    limit: Optional[Node] = None
    full: bool = False
    temporary: bool = False
    changed: bool = False
    name: Optional[str] = None

    def to_sql(self) -> str:
        out = "SHOW "
        if self.full:
            out += "FULL "
        out += self.kind
        if self.table:
            out += " " + (f"`{self.database}`." if self.database else "") + f"`{self.table}`"
        elif self.database:
            out += f" FROM `{self.database}`"
        if self.like:
            out += f" LIKE '{self.like}'"
        if self.where is not None:
            out += " WHERE " + self.where.to_sql()
        return out

    def children(self) -> List[Node]:
        return [self.where] if self.where else []


@dataclass
class DescribeQuery(Node):
    database: Optional[str] = None
    table: str = ""
    table_function: Optional[TableFunctionCall] = None
    subquery: Optional[SelectQuery] = None

    def to_sql(self) -> str:
        if self.table_function is not None:
            return f"DESCRIBE TABLE {self.table_function.to_sql()}"
        if self.subquery is not None:
            return f"DESCRIBE ({self.subquery.to_sql()})"
        return "DESCRIBE TABLE " + (f"`{self.database}`." if self.database else "") + f"`{self.table}`"

    def children(self) -> List[Node]:
        return []


@dataclass
class ExplainQuery(Node):
    """``EXPLAIN [AST|SYNTAX|PLAN|PIPELINE|ESTIMATE|QUERY TREE] statement``。

    每一种 ``kind`` 回答一个不同的问题(第 22 章有一张对照表):

    * ``AST`` —— 解析出来了吗? 树长什么样?
    * ``SYNTAX`` —— 优化器改过之后、提交给 planner 的树长什么样?
    * ``PLAN`` —— **逻辑**查询计划:有哪些步骤、顺序如何(适合定位"为什么没走索引");
    * ``PIPELINE`` —— **物理**执行流水线:几个 processor、怎么并行、用了几条线程
      (适合定位"为什么只有 1 个核在跑");
    * ``ESTIMATE`` —— 计划里每一步的**行数估计**(适合定位"为什么选了个烂 join 顺序")。
    """

    kind: str = "PLAN"
    statement: Optional[Node] = None
    settings: List[Tuple[str, Node]] = field(default_factory=list)

    def to_sql(self) -> str:
        return f"EXPLAIN {self.kind} {self.statement.to_sql() if self.statement else ''}"

    def children(self) -> List[Node]:
        return [self.statement] if self.statement else []


@dataclass
class SystemQuery(Node):
    """``SYSTEM FLUSH LOGS`` / ``SYSTEM RELOAD DICTIONARIES`` / ``SYSTEM STOP MERGES`` …"""

    command: str = ""
    database: Optional[str] = None
    table: Optional[str] = None
    args: List[Any] = field(default_factory=list)

    def to_sql(self) -> str:
        out = "SYSTEM " + self.command
        if self.table:
            out += " " + (f"`{self.database}`." if self.database else "") + f"`{self.table}`"
        if self.args:
            out += " " + " ".join(str(a) for a in self.args)
        return out

    def children(self) -> List[Node]:
        return []


@dataclass
class SetQuery(Node):
    """``SET a = 1, b = DEFAULT``。

    ``scope`` 决定这条设置活多久 —— 这是 ClickHouse 的一个
    与 MySQL 不同的地方:``SET`` 默认是**会话级**,
    而查询级的设置写在语句末尾的 ``SETTINGS`` 子句里(第 24 章)。
    """

    items: List[Tuple[str, Any]] = field(default_factory=list)
    scope: str = "SESSION"     # SESSION / GLOBAL / LOCAL

    def to_sql(self) -> str:
        prefix = f"SET {self.scope} " if self.scope != "SESSION" else "SET "
        return prefix + ", ".join(f"{k} = {v}" for k, v in self.items)

    def children(self) -> List[Node]:
        return []


@dataclass
class UseQuery(Node):
    database: str = ""

    def to_sql(self) -> str:
        return f"USE `{self.database}`"

    def children(self) -> List[Node]:
        return []


@dataclass
class KillQuery(Node):
    kind: str = "QUERY"      # QUERY / MUTATION
    where: Optional[Node] = None
    sync: bool = False
    test: bool = False

    def to_sql(self) -> str:
        out = f"KILL {self.kind}"
        if self.where is not None:
            out += " WHERE " + self.where.to_sql()
        if self.sync:
            out += " SYNC"
        return out

    def children(self) -> List[Node]:
        return [self.where] if self.where else []


@dataclass
class GrantQuery(Node):
    """``GRANT SELECT ON db.* TO user``。"""

    revoke: bool = False
    privileges: List[str] = field(default_factory=list)
    on: str = ""
    to: List[str] = field(default_factory=list)
    with_grant_option: bool = False

    def to_sql(self) -> str:
        verb = "REVOKE" if self.revoke else "GRANT"
        out = f"{verb} " + ", ".join(self.privileges)
        if self.on:
            out += f" ON {self.on}"
        out += (" FROM " if self.revoke else " TO ") + ", ".join(self.to)
        return out

    def children(self) -> List[Node]:
        return []


@dataclass
class CreateUserQuery(Node):
    name: str = ""
    kind: str = "USER"       # USER / ROLE / QUOTA / ROW POLICY / SETTINGS PROFILE
    clauses: List[str] = field(default_factory=list)
    if_not_exists: bool = False

    def to_sql(self) -> str:
        out = f"CREATE {self.kind} "
        if self.if_not_exists:
            out += "IF NOT EXISTS "
        out += self.name
        if self.clauses:
            out += " " + " ".join(self.clauses)
        return out

    def children(self) -> List[Node]:
        return []


@dataclass
class ExistsQuery(Node):
    database: Optional[str] = None
    table: str = ""

    def to_sql(self) -> str:
        return f"EXISTS TABLE {self.database}.{self.table}" if self.database else f"EXISTS TABLE {self.table}"

    def children(self) -> List[Node]:
        return []


# ══════════════════════════════════════════════════════════════════════════
# EXPLAIN AST 用的格式化
# ══════════════════════════════════════════════════════════════════════════


def format_ast(node: Optional[Node], indent: int = 0) -> str:
    """把语法树打成缩进文本 —— ``EXPLAIN AST`` 的输出。

    形态刻意与真实 ClickHouse 的 ``EXPLAIN AST`` 接近(节点名 + 子节点缩进),
    但不追求逐字一致 —— 因为真实输出里的节点名是 C++ 类名
    (``ASTSelectQuery``、``ASTFunction``…),而我们用的是 Python 类名。
    这个差异在教程里标了出来。
    """
    if node is None:
        return "  " * indent + "(empty)"
    pad = "  " * indent
    kind = type(node).__name__
    detail = _node_detail(node)
    lines = [f"{pad}{kind}{detail}"]
    for child in node.children():
        lines.append(format_ast(child, indent + 1))
    return "\n".join(lines)


def _node_detail(node: Node) -> str:
    """节点的"一行摘要" —— 让 EXPLAIN AST 的输出可读。"""
    if isinstance(node, Identifier):
        return f" {node.name}"
    if isinstance(node, Literal):
        return f" {node.to_sql()}"
    if isinstance(node, FunctionCall):
        return f" {node.name}"
    if isinstance(node, TableReference):
        return f" {node.qualified_name()}"
    if isinstance(node, BinaryOp):
        return f" {node.op}"
    if isinstance(node, UnaryOp):
        return f" {node.op}"
    if isinstance(node, AliasExpr):
        return f" AS {node.alias}"
    if isinstance(node, OrderByElement):
        return f" {node.direction}"
    if isinstance(node, JoinExpr):
        return f" {node.kind} {node.strictness}"
    if isinstance(node, WindowFunction):
        return " window"
    return ""
