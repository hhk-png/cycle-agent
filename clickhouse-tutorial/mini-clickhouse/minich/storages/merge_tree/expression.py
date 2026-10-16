"""列表达式求值 —— MergeTree 自己需要的那个**小**求值器。

.. warning::

   **这不是执行引擎,也不该被当成执行引擎。**

   真实 ClickHouse 的存储层**从来不算表达式**:``ALTER ... DELETE WHERE``
   会生成一个普通的 ``SELECT`` 计划,由 ``MutationsInterpreter`` 交给
   解释器执行;TTL 的行删除同样走 ``ExpressionActions``。存储层只负责
   "把某个 part 重写成符合谓词的新 part"。

   mini-clickhouse 里 :mod:`minich.storages.merge_tree` 需要三个地方求值,
   而它们都发生在**存储层内部**、且**不能**依赖并行开发的
   ``minich.execution.*``(模块级导入会造成循环依赖):

   .. code-block:: text

      DEFAULT / MATERIALIZED 列的补值        ALTER ADD COLUMN 之后读老 part
      ALTER ... DELETE WHERE 的谓词          mutation 在 merge 时套用
      TTL 的表达式(RECOMPRESS / DELETE)     判断一行是否过期

   所以这里实现了一个**够用就好**的求值器:支持字面量、列引用、
   一元/二元运算、比较、逻辑、``IN``/``BETWEEN``/``IS NULL``/``LIKE``,
   以及一批**列上的日期/类型函数**(``toYYYYMM``/``toDate``/``toMonday``/
   ``intDiv``/``toUInt32`` …),因为分区键与 TTL 都依赖它们。

   不支持的东西会**抛** ``NOT_IMPLEMENTED``,绝不静默返回默认值 ——
   静默会让 mutation 删错行、TTL 删错数据,是存储层最不可接受的 bug。

.. code-block:: text

   evaluate_expression(expr, block)
        │
        ├── Literal      → 常量列(ColumnConst)
        ├── Identifier   → 从 block 里取列
        ├── FunctionCall → 内置列函数 或 minich.functions 注册表
        ├── BinaryOp     → 算术 / 比较 / 逻辑(自己算,不依赖函数表)
        ├── InExpr / BetweenExpr / IsNullExpr / LikeExpr / CastExpr
        └── 其它          → 抛 NOT_IMPLEMENTED
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ...block import ColumnWithTypeAndName, Block
from ...columns import (
    ColumnConst,
    ColumnNullable,
    ColumnString,
    ColumnVector,
    IColumn,
)
from ...errors import ErrorCode, raise_error
from ...types import DataType, get_data_type
from ...types.scalars import days_to_date, date_to_days

__all__ = [
    "evaluate_expression",
    "evaluate_predicate",
    "evaluate_constant",
    "literal_type_of",
    "truthy_value",
    "MONOTONIC_COLUMN_FUNCTIONS",
]

#: 一天有多少秒 —— 日期/时间函数在内部统一用这两个域:
#: ``Date``/``Date32`` 用**天数**,``DateTime``/``DateTime64`` 用**秒数**。
SECONDS_PER_DAY = 86400


# ══════════════════════════════════════════════════════════════════════════
# 真值语义
# ══════════════════════════════════════════════════════════════════════════


def truthy_value(value: Any) -> bool:
    """ClickHouse 的"哪些值算真" —— ``0``/``''``/``[]``/``NULL`` 为假。

    真实 ClickHouse 里 ``WHERE`` 只接受 ``UInt8``(或可转成 UInt8 的值),
    字符串/数组直接进 ``WHERE`` 会报 ``ILLEGAL_TYPE_OF_COLUMN_FOR_FILTER``。
    mini-clickhouse 在存储层放宽到"按值判定真值",因为 mutation 谓词
    常常是从字面量比较推出来的。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (str, bytes, bytearray, list, tuple, dict)):
        return len(value) > 0
    return bool(value)


# ══════════════════════════════════════════════════════════════════════════
# 字面量的类型推断
# ══════════════════════════════════════════════════════════════════════════


def literal_type_of(value: Any) -> DataType:
    """把 Python 值映射成 ClickHouse 类型 —— 只用于**求值期**,不做精确推导。

    真实 ClickHouse 有一整套字面量类型推导(``1`` 是 ``UInt8``、
    ``1.0`` 是 ``Float64``、``-1`` 是 ``Int8``…),并且在"和列比较"时
    按**列的类型**反推。我们在比较的落地点用 ``normalize`` 做同样的
    反推(见 :func:`_binary_compare`),这里只给一个合理的初值。
    """
    if value is None:
        return get_data_type("Nothing")
    if isinstance(value, bool):
        return get_data_type("UInt8")
    if isinstance(value, int):
        return get_data_type("Int64") if value < 0 else get_data_type("UInt64")
    if isinstance(value, float):
        return get_data_type("Float64")
    if isinstance(value, (bytes, bytearray)):
        return get_data_type("String")
    if isinstance(value, str):
        return get_data_type("String")
    if isinstance(value, (list, tuple)):
        inner = literal_type_of(value[0]) if value else get_data_type("Nothing")
        return get_data_type(f"Array({inner.get_name()})")
    # 兜底:当成字符串(与 :func:`minich.literals.parse_literal` 的行为一致)
    return get_data_type("String")


def _const_column(value: Any, dtype: Optional[DataType] = None) -> ColumnWithTypeAndName:
    """造一个"只有一行的常量列"。

    ``ColumnConst`` 的语义是"一个值 + 一个行数",在这里行数由调用方
    之后统一 ``resize`` 到 block 的行数 —— 这样常量列在整块上都是同一个值。
    """
    t = dtype if dtype is not None else literal_type_of(value)
    data = t.create_column()
    if t.is_nothing():
        # ``Nothing`` 不能 append,但它本来就是"没有值"——
        # 用一个空 ColumnConst 表示(读取时会抛,但 NULL 比较不会走到读值)。
        return ColumnWithTypeAndName("", t, ColumnConst(data, 0))
    data.append(t.normalize(value))
    return ColumnWithTypeAndName("", t, ColumnConst(data, 1))


# ══════════════════════════════════════════════════════════════════════════
# 列函数 —— 分区键 / TTL / mutation 会用到的那些
# ══════════════════════════════════════════════════════════════════════════


def _as_days(value: Any, src: DataType) -> int:
    """把一个值转成"自 1970-01-01 起的天数"。"""
    if src.is_date():
        return int(value)
    if src.is_date_time():
        return int(value) // SECONDS_PER_DAY
    if isinstance(value, str) or hasattr(value, "year"):
        return date_to_days(value)
    return int(value) // SECONDS_PER_DAY


def _as_seconds(value: Any, src: DataType) -> int:
    """把一个值转成"自 1970-01-01 起的秒数"。"""
    if src.is_date_time():
        return int(value)
    if src.is_date():
        return int(value) * SECONDS_PER_DAY
    if isinstance(value, str) or hasattr(value, "year"):
        return date_to_days(value) * SECONDS_PER_DAY
    return int(value)


def _days_to_dt(days: int) -> _dt.datetime:
    return _dt.datetime(1970, 1, 1) + _dt.timedelta(days=int(days))


def _dt_to_days(dt: _dt.datetime) -> int:
    return (dt.date() - _dt.date(1970, 1, 1)).days


def _to_start_of_month(days: int) -> int:
    d = _days_to_dt(days)
    return _dt_to_days(_dt.datetime(d.year, d.month, 1))


def _to_start_of_year(days: int) -> int:
    d = _days_to_dt(days)
    return _dt_to_days(_dt.datetime(d.year, 1, 1))


def _to_monday(days: int) -> int:
    dt = _days_to_dt(days)
    return _dt_to_days(dt - _dt.timedelta(days=dt.weekday()))


def _to_start_of_week(days: int) -> int:
    dt = _days_to_dt(days)
    # ClickHouse 的 toStartOfWeek 默认 mode=0 → 周日作为一周开始
    return _dt_to_days(dt - _dt.timedelta(days=(dt.weekday() + 1) % 7))


def _yyyymmddhhmmss(value: Any, src: DataType) -> int:
    """``toYYYYMMDDhhmmss`` —— 把日期/时间压成一个 14 位十进制整数。

    ``Date`` 输入只有日期部分,时分秒补 0(与真实 ClickHouse 一致:
    Date 会被先提升成 DateTime,零点那一刻)。
    """
    if src.is_date_time():
        dt = _dt.datetime(1970, 1, 1) + _dt.timedelta(seconds=int(value))
    else:
        dt = _days_to_dt(_as_days(value, src))
    return (dt.year * 10**10 + dt.month * 10**8 + dt.day * 10**6
            + dt.hour * 10**4 + dt.minute * 100 + dt.second)


def _months_since_epoch(days: int) -> int:
    d = _days_to_dt(days)
    return d.year * 12 + (d.month - 1)


#: 名字 → 求值函数。参数是 ``(值列表, 参数值列表, 参数类型列表)``。
#:
#: 这张表是**分区裁剪**与 **KeyCondition 单调函数**共用的语义来源 ——
#: 两边必须用同一个实现,否则"裁剪说能跳过、实际读出来了"就会不一致。
MONOTONIC_COLUMN_FUNCTIONS: Dict[str, Any] = {
    "toDate": lambda v, p, t: _as_days(v[0], t[0]),
    "toDate32": lambda v, p, t: _as_days(v[0], t[0]),
    "toDateTime": lambda v, p, t: _as_seconds(v[0], t[0]),
    "toYYYYMM": lambda v, p, t: (_days_to_dt(_as_days(v[0], t[0])).year * 100
                                + _days_to_dt(_as_days(v[0], t[0])).month),
    "toYYYYMMDD": lambda v, p, t: (_days_to_dt(_as_days(v[0], t[0])).year * 10000
                                  + _days_to_dt(_as_days(v[0], t[0])).month * 100
                                  + _days_to_dt(_as_days(v[0], t[0])).day),
    "toStartOfMonth": lambda v, p, t: _to_start_of_month(_as_days(v[0], t[0])),
    "toStartOfYear": lambda v, p, t: _to_start_of_year(_as_days(v[0], t[0])),
    "toStartOfWeek": lambda v, p, t: _to_start_of_week(_as_days(v[0], t[0])),
    "toMonday": lambda v, p, t: _to_monday(_as_days(v[0], t[0])),
    "toStartOfDay": lambda v, p, t: _as_days(v[0], t[0]) * SECONDS_PER_DAY
                                    if t[0].is_date_time() else _as_days(v[0], t[0]),
    "toYYYYMMDDhhmmss": lambda v, p, t: _yyyymmddhhmmss(v[0], t[0]),
    "toStartOfHour": lambda v, p, t: (_as_seconds(v[0], t[0]) // 3600) * 3600,
    "toYear": lambda v, p, t: _days_to_dt(_as_days(v[0], t[0])).year,
    "toMonth": lambda v, p, t: _days_to_dt(_as_days(v[0], t[0])).month,
    "toDayOfMonth": lambda v, p, t: _days_to_dt(_as_days(v[0], t[0])).day,
    "toQuarter": lambda v, p, t: (_days_to_dt(_as_days(v[0], t[0])).month - 1) // 3 + 1,
    "intDiv": lambda v, p, t: int(int(v[0]) // int(v[1])),
    "toUInt32": lambda v, p, t: int(v[0]) & 0xFFFFFFFF,
    "toUInt64": lambda v, p, t: int(v[0]) & 0xFFFFFFFFFFFFFFFF,
    "toInt32": lambda v, p, t: int(v[0]),
    "toInt64": lambda v, p, t: int(v[0]),
    "toUInt8": lambda v, p, t: int(v[0]) & 0xFF,
    "identity": lambda v, p, t: v[0],
    "toDate32OrNull": lambda v, p, t: _as_days(v[0], t[0]),
}


def _eval_column_function(
    name: str,
    values: List[Any],
    params: Sequence[Any],
    arg_types: Sequence[DataType],
) -> Any:
    fn = MONOTONIC_COLUMN_FUNCTIONS.get(name)
    if fn is None:
        return _CALL_MISSING
    return fn(values, list(params), list(arg_types))


#: 哨兵:表示"这个函数不在内置表里"。
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_CALL_MISSING = _Missing()


# ══════════════════════════════════════════════════════════════════════════
# 求值主体
# ══════════════════════════════════════════════════════════════════════════


class _EvalContext:
    """求值期的小状态 —— 目前只记 block 的行数与列。"""

    __slots__ = ("block", "rows", "context")

    def __init__(self, block: Block, context: Any = None) -> None:
        self.block = block
        self.rows = block.rows()
        self.context = context


def evaluate_expression(expr: Any, block: Block, context: Any = None) -> ColumnWithTypeAndName:
    """求一个表达式的值,返回**行数与 block 相同**的列。"""
    ec = _EvalContext(block, context)
    out = _eval(expr, ec)
    if out.column.is_const() and out.column.size() != ec.rows:
        # 常量列统一展开到整块的行数:下游按位置取值才不会错位
        out.column = ColumnConst(out.column.data.clone(), ec.rows)
    if out.column.size() != ec.rows:
        raise_error(
            ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
            f"Expression {_sql(expr)} produced {out.column.size()} rows "
            f"but the block has {ec.rows} rows",
        )
    return out


def evaluate_predicate(expr: Any, block: Block, context: Any = None) -> List[bool]:
    """把表达式当成布尔谓词求值 —— 返回逐行的 ``True/False``。

    ``DELETE WHERE`` 的掩码、``TTL`` 的过期判定都用它。
    """
    col = evaluate_expression(expr, block, context).column
    full = col.convert_to_full_column() if col.is_const() else col
    return [truthy_value(full.get(i)) for i in range(block.rows())]


def evaluate_constant(expr: Any, context: Any = None) -> Any:
    """求一个**不含列**的常量表达式(分区表达式的取值走这条路)。"""
    empty = Block([])
    col = evaluate_expression(expr, empty, context).column
    if isinstance(col, ColumnConst):
        return col.data.get(0) if col.data.size() else None
    # 表达式的行数可能是 1(常量列被 resize 到 0 行时读不出来)
    return col.get(0) if col.size() else None


def _eval(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    from ...sql import ast_ as A

    if isinstance(expr, A.Literal):
        return _const_column(expr.value)
    if isinstance(expr, A.Identifier):
        name = expr.name
        c = ec.block.try_get_by_name(name)
        if c is None:
            raise_error(
                ErrorCode.UNKNOWN_IDENTIFIER,
                f"There is no column {name!r} in the block "
                f"({', '.join(ec.block.get_names()) or 'empty'})",
            )
        return ColumnWithTypeAndName(name, c.type, c.column)
    if isinstance(expr, A.AliasExpr):
        return _eval(expr.expr, ec)
    if isinstance(expr, A.CastExpr):
        inner = _eval(expr.expr, ec)
        target = get_data_type(expr.type_name)
        return _cast_column(inner, target)
    if isinstance(expr, A.UnaryOp):
        return _eval_unary(expr, ec)
    if isinstance(expr, A.BinaryOp):
        return _eval_binary(expr, ec)
    if isinstance(expr, A.InExpr):
        return _eval_in(expr, ec)
    if isinstance(expr, A.BetweenExpr):
        return _eval_between(expr, ec)
    if isinstance(expr, A.IsNullExpr):
        return _eval_is_null(expr, ec)
    if isinstance(expr, A.LikeExpr):
        return _eval_like(expr, ec)
    if isinstance(expr, A.FunctionCall):
        return _eval_function(expr, ec)
    if isinstance(expr, A.ArrayLiteral):
        return _const_column([_eval_literal_value(i, ec) for i in expr.items])
    if isinstance(expr, A.TupleLiteral):
        return _eval_tuple(expr, ec)
    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"Expression evaluator does not support {type(expr).__name__} "
        f"({_sql(expr)})",
    )


def _eval_tuple(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    """``(a, b)`` —— 求值成**一整列元组**,而不是"第一行的常量元组"。

    .. warning::

       **这是 ``INDEX ix (a, b) TYPE minmax`` 能工作的前提。**

       以前这里把 ``TupleLiteral`` 当常量处理:对每一项取"第 0 行的值",
       拼成一个常量元组。于是:

       .. code-block:: text

          ``(a, b)`` 的类型被推成 ``Tuple(UInt32, UInt32)``?   —— 不,
          常量路径只认得字面量,遇到 Identifier 就退回 ``String``,
          而值是一个 **Python list** →
          INSERT 时 ``Cannot convert list to String`` → **建表能过、插入崩**

       正确做法:每一项各自求值成一列,再竖着摞成 ``ColumnTuple``,
       类型是真正的 ``Tuple(T1, T2)``。这样 min/max、序列化、比较
       全都按元组的语义走。
    """
    from ...columns import ColumnTuple

    cols = []
    types = []
    for item in expr.items:
        c = _eval(item, ec)
        col = _materialize(c.column, ec.rows)
        cols.append(col)
        types.append(c.type)
    tuple_type = get_data_type("Tuple(" + ", ".join(t.get_name() for t in types) + ")")
    return ColumnWithTypeAndName("", tuple_type, ColumnTuple(cols))


def _eval_literal_value(expr: Any, ec: _EvalContext) -> Any:
    col = _eval(expr, ec).column
    if isinstance(col, ColumnConst):
        return col.data.get(0) if col.data.size() else None
    return col.get(0) if col.size() else None


# ── 一元 ─────────────────────────────────────────────────────────────────


def _eval_unary(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    operand = _eval(expr.operand, ec)
    out = operand.type.create_column()
    n = ec.rows
    col = _materialize(operand.column, n)
    if expr.op == "-":
        for i in range(n):
            v = col.get(i)
            if v is None:
                # 与二元算术一致:``-NULL`` 就是 ``NULL``(真实 ClickHouse)。
                # 以前这里直接 ``-None`` → 裸 ``TypeError``。
                out.append(None)
                continue
            out.append(-v)
        return ColumnWithTypeAndName("", operand.type, out)
    if expr.op.upper() == "NOT":
        for i in range(n):
            out.append(0 if truthy_value(col.get(i)) else 1)
        return ColumnWithTypeAndName("", get_data_type("UInt8"), out)
    raise_error(ErrorCode.NOT_IMPLEMENTED, f"Unsupported unary operator {expr.op!r}")


# ── 二元 ─────────────────────────────────────────────────────────────────


_COMPARISONS = {"=", "==", "!=", "<>", "<", "<=", ">", ">="}
_ARITHMETIC = {"+", "-", "*", "/", "%", "intDiv"}


def _eval_binary(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    op = expr.op
    up = op.upper()
    if up in ("AND", "OR"):
        return _eval_logical(expr, ec)
    if op == "||":
        return _eval_concat(expr, ec)
    from ...sql import ast_ as A

    if op in ("+", "-") and (isinstance(expr.left, A.IntervalExpr)
                             or isinstance(expr.right, A.IntervalExpr)):
        return _eval_interval_arith(expr, ec)
    left = _eval(expr.left, ec)
    right = _eval(expr.right, ec)
    if op in _COMPARISONS:
        return _binary_compare(op, left, right, ec)
    if op in _ARITHMETIC:
        return _binary_arithmetic(op, left, right, ec)
    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"Unsupported binary operator {op!r} in {_sql(expr)}",
    )


def _eval_interval_arith(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    """``date + INTERVAL 30 DAY`` / ``ts - INTERVAL 1 HOUR``。

    Interval 的算术**必须按类型分流**,这是 ClickHouse 最容易被忽视的语义:

    .. code-block:: text

       Date     + INTERVAL 30 DAY   → 日期加 30 **天**(内部是天数，+30)
       DateTime + INTERVAL 1 HOUR   → 时间加 1 **小时**(内部是秒，+3600)
       Date     + INTERVAL 1 MONTH  → **日历月**(不是 30 天!)
       DateTime + INTERVAL 1 MONTH  → 日历月,涉及闰年/月末的取舍

    注意最后一条:ClickHouse 的 ``INTERVAL 1 MONTH`` 走的是**日历算术**,
    不是"30 天"。这与 ``toStartOfMonth`` 之类的函数语义保持一致。
    """
    from ...sql import ast_ as A

    if isinstance(expr.right, A.IntervalExpr):
        interval_node, base_node, sign = expr.right, expr.left, (1 if expr.op == "+" else -1)
    else:
        interval_node, base_node, sign = expr.left, expr.right, 1
    base = _eval(base_node, ec)
    kind = (interval_node.kind or "DAY").upper()
    amount = _eval_literal_value(interval_node.amount, ec) if interval_node.amount is not None else 0
    amount = int(amount or 0) * sign
    is_dt = base.type.is_date_time()
    col = _materialize(base.column, ec.rows)
    out = base.type.create_column()
    for i in range(ec.rows):
        v = col.get(i)
        if v is None:
            out.append_default()
            continue
        out.append(_shift_value(v, is_dt, amount, kind))
    return ColumnWithTypeAndName("", base.type, out)


def _shift_value(value: Any, is_datetime: bool, amount: int, kind: str) -> Any:
    """把一个 Date/DateTime 内部值按 interval 平移。"""
    if kind in ("MONTH", "QUARTER", "YEAR"):
        months = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}[kind] * amount
        if is_datetime:
            dt = _dt.datetime(1970, 1, 1) + _dt.timedelta(seconds=int(value))
        else:
            dt = _days_to_dt(int(value))
        total = dt.year * 12 + (dt.month - 1) + months
        year, month = divmod(total, 12)
        month += 1
        day = min(dt.day, _days_in_month(year, month))
        shifted = dt.replace(year=year, month=month, day=day)
        if is_datetime:
            return int((shifted - _dt.datetime(1970, 1, 1)).total_seconds())
        return _dt_to_days(shifted)
    if kind == "WEEK":
        amount *= 7
        kind = "DAY"
    if kind == "DAY":
        if is_datetime:
            return int(value) + amount * SECONDS_PER_DAY
        return int(value) + amount
    per_second = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600}.get(kind)
    if per_second is None:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"Unsupported INTERVAL unit {kind!r}")
    if is_datetime:
        return int(value) + amount * per_second
    return int(value) + (amount * per_second) // SECONDS_PER_DAY


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    return (nxt - _dt.timedelta(days=1)).day


def _materialize(col: IColumn, rows: int) -> IColumn:
    """把常量列展开成**恰好 rows 行、每行都是同一个值**的完整列。

    逐行求值必须先把常量摊平,而且必须**重复那个值** ——
    这里有一个非常隐蔽的坑:

    .. code-block:: text

       ColumnConst(data=[77777], sizeness=1)

       ① col.convert_to_full_column()   → [77777]          (1 行)
       ② 再 append_many_default(1023)   → [77777, 0, 0, …]  ← **错!**
          补的是**零值**,不是把 77777 重复 1024 次。
          于是 `id = 77777` 只在第 0 行可能匹配,其余全被过滤掉。

       正确做法:insert_many_from(data, 0, rows) —— 明确地"重复这个值"。

    这个 bug 的症状是"点查返回 0 行",而且**只在 block 行数 > 1 时出现**
    (行数为 1 时两种做法结果相同)—— 极难定位,所以在这里用注释钉死。
    """
    if not col.is_const():
        return col
    data = col.data
    out = data.clone_empty()
    if data.size() == 0:
        out.append_many_default(rows)
    else:
        out.insert_many_from(data, 0, rows)
    return out


def _coerce_const(cwtn: ColumnWithTypeAndName, target: DataType) -> ColumnWithTypeAndName:
    """把一个常量列的值按 ``target`` 类型规范化。

    这就是 ``WHERE d >= '2024-03-01'`` 能工作的原因:左操作数是 ``Date``
    列,右操作数是字符串常量,右值被 ``Date.normalize`` 转成"天数"。
    """
    if not cwtn.column.is_const() or cwtn.type.equals(target):
        return cwtn
    src = cwtn.column
    if src.size() == 0:
        return ColumnWithTypeAndName("", target, ColumnConst(target.create_column(), 0))
    raw = src.data.get(0)
    value = _convert_value(raw, cwtn.type, target)
    return _const_column(value, target)


def _convert_value(value: Any, src: DataType, target: DataType) -> Any:
    if value is None:
        return None
    try:
        if target.is_date() or target.is_date_time():
            if isinstance(value, str):
                return target.normalize(value)
            if src.is_date() and target.is_date_time():
                return int(value) * SECONDS_PER_DAY
            if src.is_date_time() and target.is_date():
                return int(value) // SECONDS_PER_DAY
            return target.normalize(value)
        if target.is_string():
            return target.serialize_text(value)
        if target.is_numeric():
            return target.normalize(value)
        return target.normalize(value)
    except Exception:
        return value


def _binary_compare(
    op: str,
    left: ColumnWithTypeAndName,
    right: ColumnWithTypeAndName,
    ec: _EvalContext,
) -> ColumnWithTypeAndName:
    # 常量往非常量那一侧的类型靠 —— 这一步是"字面量按列类型反推"的落地点
    if right.column.is_const() and not left.column.is_const():
        right = _coerce_const(right, left.type)
    elif left.column.is_const() and not right.column.is_const():
        left = _coerce_const(left, right.type)

    n = ec.rows
    lc = _materialize(left.column, n)
    rc = _materialize(right.column, n)
    out = ColumnVector(np.uint8)
    for i in range(n):
        a, b = lc.get(i), rc.get(i)
        out.append(1 if _compare_values(op, a, b) else 0)
    return ColumnWithTypeAndName("", get_data_type("UInt8"), out)


def _compare_values(op: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        # ClickHouse 里 NULL 与任何值比较都是假(结果既不是 true 也不是 false,
        # 但用在 WHERE 里等价于"不匹配")。两个 NULL 只在 ``<=`` 与 ``>=``
        # 上为真(真实 CH 的 NULL 排序语义)。这里保守地按"相等"处理。
        if a is None and b is None:
            return op in ("=", "==", "<=", ">=")
        return False
    if op in ("=", "=="):
        return a == b
    if op in ("!=", "<>"):
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    raise_error(ErrorCode.LOGICAL_ERROR, f"Unknown comparison operator {op!r}")


def _strip_nullable(t: DataType) -> DataType:
    """剥掉 ``Nullable(...)`` 包装(没有包装就原样返回)。

    这是全代码库统一的做法:``Nullable`` 是独立的类型族,``is_numeric()`` /
    ``is_date()`` 之类的判定在它上面都不成立,所以凡是要"按内层类型推理"
    的地方都先剥一层(解释器里同样是 ``t.nested if t.is_nullable() else t``,
    见 ``minich/execution/expression.py`` 的 ``_common_type``)。
    """
    return t.nested if t.is_nullable() else t  # type: ignore[attr-defined]


def _binary_arithmetic(
    op: str,
    left: ColumnWithTypeAndName,
    right: ColumnWithTypeAndName,
    ec: _EvalContext,
) -> ColumnWithTypeAndName:
    from ...types import DataTypeNullable, get_wider_numeric_type

    # 类型推导在**剥掉 Nullable 之后**做 —— 否则 ``v + 1``(v 是
    # ``Nullable(Int32)``)会因为 ``Nullable(Int32).is_numeric() == False``
    # 落到"取另一个操作数的类型"这条兜底路上,结果类型被推成 ``UInt64``。
    # 真实 ClickHouse 的答案是 ``Nullable(Int32)``:
    # 算术在内层类型上做,可空性再"传染"回结果类型。
    lt = _strip_nullable(left.type)
    rt = _strip_nullable(right.type)
    if lt.is_numeric() and rt.is_numeric():
        result_type = get_wider_numeric_type(lt, rt)
    else:
        result_type = lt if lt.is_numeric() else rt
    if op == "/":
        result_type = get_data_type("Float64")
    nullable = left.type.is_nullable() or right.type.is_nullable()
    if nullable and not result_type.is_nullable():
        result_type = DataTypeNullable(result_type)
    n = ec.rows
    lc = _materialize(left.column, n)
    rc = _materialize(right.column, n)
    out = result_type.create_column()
    for i in range(n):
        a, b = lc.get(i), rc.get(i)
        if a is None or b is None:
            # ★ NULL 是**传播**的,不是错误。``NULL + 1`` 在真实 ClickHouse
            #   里就是 ``NULL``;以前这里抛 CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN,
            #   于是 ``ALTER TABLE ... UPDATE v = v + 1`` 碰到一行 NULL 就整条
            #   mutation 失败(而同样表达式走解释器是好的)。
            out.append(None)
            continue
        if op == "+":
            v = a + b
        elif op == "-":
            v = a - b
        elif op == "*":
            v = a * b
        elif op == "/":
            v = a / b if b != 0 else float("nan")
        elif op == "%":
            v = a % b
        elif op == "intDiv":
            v = int(a) // int(b) if b != 0 else 0
        else:  # pragma: no cover
            raise_error(ErrorCode.NOT_IMPLEMENTED, f"Unknown arithmetic op {op!r}")
        out.append(result_type.normalize(v))
    return ColumnWithTypeAndName("", result_type, out)


def _eval_logical(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    left = _materialize(_eval(expr.left, ec).column, ec.rows)
    right = _materialize(_eval(expr.right, ec).column, ec.rows)
    out = ColumnVector(np.uint8)
    is_and = expr.op.upper() == "AND"
    for i in range(ec.rows):
        a, b = truthy_value(left.get(i)), truthy_value(right.get(i))
        result = (a and b) if is_and else (a or b)
        out.append(1 if result else 0)
    return ColumnWithTypeAndName("", get_data_type("UInt8"), out)


def _eval_concat(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    left = _materialize(_eval(expr.left, ec).column, ec.rows)
    right = _materialize(_eval(expr.right, ec).column, ec.rows)
    out = ColumnString()
    for i in range(ec.rows):
        out.append(str(left.get(i)) + str(right.get(i)))
    return ColumnWithTypeAndName("", get_data_type("String"), out)


# ── IN / BETWEEN / IS NULL / LIKE ────────────────────────────────────────


def _in_values(expr: Any, ec: _EvalContext) -> List[Any]:
    from ...sql import ast_ as A

    right = expr.right
    if isinstance(right, A.ArrayLiteral):
        return [_eval_literal_value(i, ec) for i in right.items]
    if isinstance(right, A.TupleLiteral):
        return [_eval_literal_value(i, ec) for i in right.items]
    if isinstance(right, A.Literal) and isinstance(right.value, (list, tuple)):
        return list(right.value)
    if isinstance(right, A.FunctionCall) and right.name.lower() in ("array", "tuple"):
        return [_eval_literal_value(i, ec) for i in right.args]
    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"IN with a non-literal right-hand side is not supported ({_sql(expr)})",
    )


def _eval_in(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    left = _eval(expr.left, ec)
    values = _in_values(expr, ec)
    target = left.type
    norm: List[Any] = []
    for v in values:
        try:
            norm.append(target.normalize(v) if target.is_date() or target.is_date_time()
                        else v)
        except Exception:
            norm.append(v)
    col = _materialize(left.column, ec.rows)
    out = ColumnVector(np.uint8)
    neg = expr.negated
    for i in range(ec.rows):
        hit = col.get(i) in norm
        out.append(0 if (hit == neg) else 1)
    return ColumnWithTypeAndName("", get_data_type("UInt8"), out)


def _eval_between(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    low = _const_column(_eval_literal_value(expr.low, ec))
    high = _const_column(_eval_literal_value(expr.high, ec))
    left = _eval(expr.expr, ec)
    low = _coerce_const(low, left.type)
    high = _coerce_const(high, left.type)
    col = _materialize(left.column, ec.rows)
    lo = low.column.get(0) if low.column.size() else None
    hi = high.column.get(0) if high.column.size() else None
    out = ColumnVector(np.uint8)
    for i in range(ec.rows):
        v = col.get(i)
        inside = (v is not None and lo is not None and hi is not None and lo <= v <= hi)
        out.append(0 if (inside == expr.negated) else 1)
    return ColumnWithTypeAndName("", get_data_type("UInt8"), out)


def _eval_is_null(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    inner = _eval(expr.expr, ec).column
    n = ec.rows
    out = ColumnVector(np.uint8)
    for i in range(n):
        v = inner.get(i)
        is_null = v is None
        out.append(0 if (is_null == expr.negated) else 1)
    return ColumnWithTypeAndName("", get_data_type("UInt8"), out)


def _eval_like(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    import re

    left = _materialize(_eval(expr.left, ec).column, ec.rows)
    pattern = _eval_literal_value(expr.pattern, ec)
    flags = re.IGNORECASE if expr.case_insensitive else 0
    if expr.is_regexp:
        rx = re.compile(str(pattern), flags)
    else:
        rx = re.compile(_like_to_regex(str(pattern)), flags | re.DOTALL)
    out = ColumnVector(np.uint8)
    for i in range(ec.rows):
        hit = rx.search(str(left.get(i))) is not None
        out.append(0 if (hit == expr.negated) else 1)
    return ColumnWithTypeAndName("", get_data_type("UInt8"), out)


def _like_to_regex(pattern: str) -> str:
    out = ["^"]
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("$")
    return "".join(out)


# ── 函数调用 ─────────────────────────────────────────────────────────────


def _eval_function(expr: Any, ec: _EvalContext) -> ColumnWithTypeAndName:
    name = expr.name
    lower = name.lower()
    # ``now()`` / ``today()``：TTL 表达式里最常见的一对。
    # 真实 ClickHouse 里它们是**非确定性函数**（每次调用结果不同），
    # 因此不会被常量折叠 —— 折叠会让整条查询里所有 now() 变成同一时刻。
    if lower in ("now", "today") and not expr.args:
        import time as _time

        epoch = int(_time.time())
        if lower == "now":
            return _const_column(epoch, get_data_type("DateTime"))
        return _const_column(epoch // SECONDS_PER_DAY, get_data_type("Date"))
    arg_cols = [_eval(a, ec) for a in expr.args]
    arg_types = [c.type for c in arg_cols]

    # ① 常量折叠:全常量参数且函数是内置列函数 → 直接算一个常量
    if all(c.column.is_const() or c.column.size() == 1 for c in arg_cols) and arg_cols:
        values = [_eval_literal_value(a, ec) for a in expr.args]
        params = [_eval_literal_value(p, ec) for p in expr.params]
        res = _eval_column_function(name, values, params, arg_types)
        if res is not _CALL_MISSING:
            return _const_column(res)

    # ② 内置列函数(逐行)
    builtin = MONOTONIC_COLUMN_FUNCTIONS.get(name)
    if builtin is not None:
        params = [_eval_literal_value(p, ec) for p in expr.params]
        plain = [_materialize(c.column, ec.rows) for c in arg_cols]
        out_type = _function_result_type(name, arg_types, params)
        out = out_type.create_column()
        for i in range(ec.rows):
            vals = [c.get(i) for c in plain]
            out.append(out_type.normalize(builtin(vals, params, arg_types)))
        return ColumnWithTypeAndName(name, out_type, out)

    # ③ 交给 minich.functions 的注册表
    try:
        from ...functions import get_function

        params = [_eval_literal_value(p, ec) for p in expr.params]
        fn = get_function(name, params, arg_types)
        ret = fn.get_return_type(arg_types)
        cols = [_materialize(c.column, ec.rows) for c in arg_cols]
        args = [
            ColumnWithTypeAndName(a.name if hasattr(a, "name") else "", t, c)
            for a, t, c in zip(arg_cols, arg_types, cols)
        ]
        col = fn.execute(args, ec.rows, ec.context)
        return ColumnWithTypeAndName(name, ret, col)
    except Exception as exc:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Expression evaluator cannot evaluate function {name}({', '.join(t.get_name() for t in arg_types)}): "
            f"{exc}",
        )


def _function_result_type(name: str, arg_types: Sequence[DataType], params: Sequence[Any]) -> DataType:
    """内置列函数的结果类型 —— 与真实 ClickHouse 的类型保持一致。"""
    src = arg_types[0] if arg_types else get_data_type("UInt64")
    if name in ("toDate", "toDate32", "toStartOfMonth", "toStartOfYear",
                "toStartOfWeek", "toMonday"):
        return get_data_type("Date")
    if name in ("toDateTime", "toStartOfDay", "toStartOfHour"):
        return get_data_type("DateTime")
    if name in ("toYYYYMM", "toYYYYMMDD", "toUInt32", "toQuarter"):
        return get_data_type("UInt32")
    if name in ("toYear", "toMonth", "toDayOfMonth", "toUInt8"):
        return get_data_type("UInt16") if name != "toUInt8" else get_data_type("UInt8")
    if name in ("intDiv", "toInt32"):
        return get_data_type("Int64")
    if name == "toInt64":
        return get_data_type("Int64")
    if name == "toUInt64":
        return get_data_type("UInt64")
    return src


# ── 类型转换 ─────────────────────────────────────────────────────────────


def _cast_column(cwtn: ColumnWithTypeAndName, target: DataType) -> ColumnWithTypeAndName:
    out = target.create_column()
    src = cwtn.type
    if cwtn.column.is_const():
        raw = cwtn.column.data.get(0) if cwtn.column.data.size() else None
        return _const_column(_convert_value(raw, src, target), target)
    col = cwtn.column
    for i in range(col.size()):
        out.append(_convert_value(col.get(i), src, target))
    return ColumnWithTypeAndName(cwtn.name, target, out)


# ── 调试 ─────────────────────────────────────────────────────────────────


def _sql(expr: Any) -> str:
    try:
        return expr.to_sql()
    except Exception:  # pragma: no cover
        return repr(expr)


def _sql_join(left: ColumnWithTypeAndName, op: str, right: ColumnWithTypeAndName) -> str:
    return f"{left.name or left.type.get_name()} {op} {right.name or right.type.get_name()}"


# ══════════════════════════════════════════════════════════════════════════
# 给 KeyCondition / 分区裁剪用的公开小工具
# ══════════════════════════════════════════════════════════════════════════


def function_result_type(
    name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()
) -> DataType:
    """内置列函数的结果类型(公开包装,供键描述构造时复用)。"""
    return _function_result_type(name, arg_types, list(params))


def is_monotonic_column_function(name: str) -> bool:
    """这个函数是否被我们当作"单调列函数"来用。

    ``intDiv``、``toUInt32`` 之类的函数在**定义域边界**上并不是严格单调的
    (整数除法有平台段),但它们都是"非递减"的 —— 而非递减就足以支撑
    "把范围往外放大一点"的裁剪推导。真实 ClickHouse 对每个函数都
    注册了一组 ``Monotonicity`` 描述(是否单调、是否严格),我们用一个
    名字集合近似。
    """
    return name in MONOTONIC_COLUMN_FUNCTIONS


def evaluate_function_value(
    name: str, values: Sequence[Any], arg_types: Sequence[DataType],
    params: Sequence[Any] = (),
) -> Any:
    """按内置语义算一个函数在**一组标量**上的值。"""
    return _eval_column_function(name, list(values), list(params), list(arg_types))


def date_span_start(days: int) -> int:
    """``toYYYYMM`` 意义上的"这个月第一天"(供分区边界推导用)。"""
    return _to_start_of_month(days)


__all__ += ["function_result_type", "is_monotonic_column_function", "evaluate_function_value"]
