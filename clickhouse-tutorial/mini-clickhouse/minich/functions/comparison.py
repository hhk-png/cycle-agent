"""比较函数 —— ``equals`` / ``less`` / ``greatest`` / ``isNotDistinctFrom``。

比较函数实现起来比看起来难,难在三个地方,而且每一个都经常被写错:

.. code-block:: text

   ① 返回类型是 UInt8,不是 Bool
        SELECT toTypeName(1 = 1)   → UInt8
      ClickHouse 里 "真/假" 就是 1/0 两个字节,没有独立的布尔类型
      (Bool 是后来加的独立类型,但比较函数仍然返回 UInt8)。
      好处:比较结果可以直接求和( countIf(x) = sum(x = 1) )、
      可以直接当位掩码用。

   ② NULL 参与比较时,结果也是 NULL —— 但有两组函数不遵守
        equals(NULL, 1)          → NULL      ← 默认的 NULL 传播
        isNull(NULL)             → 1         ← 必须能"看见" NULL
        isNotDistinctFrom(NULL,NULL) → 1     ← 把 NULL 当成一个普通值
      mini-clickhouse 里这靠 ``propagate_nulls`` 一个开关切换:
      置 False 的函数自己处理 NULL(见 :class:`FunctionIsNull`)。

   ③ "相等"的定义不是唯一的
        = (equals)               NULL = NULL  → NULL
        <=> (isNotDistinctFrom)  NULL <=> NULL → 1
        GROUP BY / ORDER BY      NULL 与 NULL 是**同一组**(见 IColumn.hash_value)
      这三套语义在真实系统里同时存在,而且都是对的 —— 因为它们回答的问题不同。
      面试里"GROUP BY 时 NULL 怎么处理"和"NULL = NULL 是什么"是两个问题。

比较的**取值语义**直接复用 :meth:`minich.columns.IColumn.compare_at`:
字符串按**字节序**(memcmp,不是词典序)、日期按内部的整数表示、
NULL 被当成"最大的值"且两个 NULL 相等(所以 ``ORDER BY x`` 默认 NULL 在最后)。

还有一个容易忽略的角落:**NaN 不等于它自己**。``equals(nan, nan)`` 返回 0
(IEEE 754 的语义,真实 CH 用的就是 C++ 的 ``==``),但 ``GROUP BY``/``ORDER BY``
里 NaN 会被当成"同一个值"(见 :mod:`minich.util.keys` 里的 ``NAN_KEY``)。
"比较函数用 IEEE 语义、排序分组用全序语义"是 ClickHouse 的有意选择 ——
前者必须和别的系统算得一样,后者必须让分组结果稳定。
"""

from __future__ import annotations

from decimal import Decimal as _PyDec
from typing import Any, Optional, Sequence, Tuple

import numpy as np

from ..block import ColumnWithTypeAndName
from ..columns import ColumnVector, IColumn
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.composite import DataTypeNullable
from .arithmetic import (
    check_arity,
    coerce_value,
    common_supertype,
    full_column,
    is_date_time_like,
    np_dtype_of,
    numpy_arg,
    unwrap_nullable,
)
from .arithmetic import _register
from .base import FunctionBase, execute_with_null_propagation

__all__ = [
    "FunctionEquals",
    "FunctionNotEquals",
    "FunctionLess",
    "FunctionLessOrEquals",
    "FunctionGreater",
    "FunctionGreaterOrEquals",
    "FunctionIsNull",
    "FunctionIsNotNull",
    "FunctionIsNotDistinctFrom",
    "FunctionIsDistinctFrom",
    "FunctionGreatest",
    "FunctionLeast",
    "FunctionNullIf",
]

#: ``op`` → NumPy 的比较 ufunc。用查表而不是 if/elif,
#: 是为了让"向量化路径"和"逐行路径"共用同一个 op 名字。
_UFUNC: dict = {
    "eq": np.equal,
    "ne": np.not_equal,
    "lt": np.less,
    "le": np.less_equal,
    "gt": np.greater,
    "ge": np.greater_equal,
}

#: ``op`` → 由 ``compare_at`` 的 -1/0/1 得到布尔值的判定
_FROM_CMP: dict = {
    "eq": lambda c: c == 0,
    "ne": lambda c: c != 0,
    "lt": lambda c: c < 0,
    "le": lambda c: c <= 0,
    "gt": lambda c: c > 0,
    "ge": lambda c: c >= 0,
}


def _can_vectorize(ta: DataType, tb: DataType) -> bool:
    """两侧能否直接丢给 NumPy 比较。

    否决条件与算术模块同源(见 ``_BinaryArithmetic._can_vectorize``):
    128/256 位、以及"两个 64 位但符号不同" —— 后者的锅在 NumPy 身上,
    它碰到 ``int64`` 与 ``uint64`` 会一起提升成 ``float64``,
    于是 ``2^63`` 和 ``2^63 + 1`` 就变成"相等"了。
    """
    da, db = np_dtype_of(ta), np_dtype_of(tb)
    if da is None or db is None:
        return False
    if (da.kind == "f") != (db.kind == "f"):
        narrower_int = db if da.kind == "f" else da
        if narrower_int.itemsize > 4:
            return False
    if da.kind != db.kind and da.itemsize == 8 and db.itemsize == 8:
        return False
    return True


def _comparable_pair(ta: DataType, tb: DataType, a: Any, b: Any) -> Tuple[Any, Any]:
    """把两侧的值转成"能用 Python 比较"的形式(逐行走这条)。

    只有三件事需要动:

    * **Decimal** 两侧 scale 不同要先对齐(``1.20`` 与 ``1.2000`` 是相等的,
      但它们存的未缩放整数是 120 与 12000);
    * **日期与字符串**混比时要先把字符串解析成日期(真实 CH 支持
      ``toDate('2024-01-01') = '2024-01-01'``);
    * 其余情况原样返回 —— 字符串是 ``str``、数字是 Python 的大整数,
      比较语义天然正确。
    """
    if ta.is_decimal() and tb.is_decimal():
        scale = max(ta.scale, tb.scale)
        return (
            _PyDec(int(a)).scaleb(scale - ta.scale),
            _PyDec(int(b)).scaleb(scale - tb.scale),
        )
    if ta.is_decimal() and not tb.is_decimal():
        return _PyDec(int(a)).scaleb(-ta.scale), _PyDec(int(b))
    if tb.is_decimal() and not ta.is_decimal():
        return _PyDec(int(a)), _PyDec(int(b)).scaleb(-tb.scale)
    if is_date_time_like(ta) and tb.is_string():
        b = ta.normalize(b)
    elif is_date_time_like(tb) and ta.is_string():
        a = tb.normalize(a)
    return a, b


class _Comparison(FunctionBase):
    """二元比较函数的公共骨架(``equals`` / ``less`` / …)。

    三条路,和算术完全对称:

    .. code-block:: text

       ① 有 NULL?     → 交给 execute_with_null_propagation(结果变成 Nullable(UInt8))
       ② 数值类型?    → NumPy 向量化比较
       ③ 其它(String / 日期 / Decimal / 128 位)
                      → 逐行走 IColumn::compare_at(那才是列自己定义的比较语义)
    """

    #: ``eq`` / ``ne`` / ``lt`` / ``le`` / ``gt`` / ``ge``
    op: str = "eq"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        _check_comparable(self.name, ta, tb)
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ta, tb = args[0].type, args[1].type
        self.get_return_type([ta, tb])  # 顺便做类型合法性检查
        # Decimal **不能**直接向量化比较:列里存的是未缩放整数,
        # 两个 scale 不同的 Decimal 逐整数比较会得到错误答案
        # (1.20 存成 120,1.2000 存成 12000,直接比整数就是"不相等")。
        if (
            ta.is_numeric()
            and tb.is_numeric()
            and not (ta.is_decimal() or tb.is_decimal())
            and _can_vectorize(ta, tb)
        ):
            ufunc = _UFUNC[self.op]
            a = numpy_arg(args[0])
            b = numpy_arg(args[1])
            return ColumnVector(np.uint8, np.asarray(ufunc(a, b), dtype=np.uint8))
        return self._execute_rowwise(args, rows, ta, tb)

    def _execute_rowwise(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        ta: DataType,
        tb: DataType,
    ) -> IColumn:
        a = full_column(args[0])
        b = full_column(args[1])
        predicate = _FROM_CMP[self.op]
        out = ColumnVector(np.uint8)
        same_layout = ta.equals(tb) and not (ta.is_decimal() or tb.is_decimal())
        if same_layout:
            # 同类型:直接借列自己的 compare_at(字节序 / 整数表示都由它决定)
            for i in range(rows):
                out.append(1 if predicate(a.compare_at(i, b, i)) else 0)
            return out
        for i in range(rows):
            va, vb = _comparable_pair(ta, tb, a.get(i), b.get(i))
            out.append(1 if predicate(_cmp(va, vb)) else 0)
        return out


def _cmp(a: Any, b: Any) -> int:
    """``-1 / 0 / 1`` 三态比较(Python 的比较运算符,对数值与字符串都成立)。"""
    if a == b:
        return 0
    return -1 if a < b else 1


def _check_comparable(name: str, ta: DataType, tb: DataType) -> None:
    """比较的两侧类型是否兼容 —— 不兼容就**报错,而不是猜**。

    真实 ClickHouse 在这里是严格的:``1 = 'a'`` 会抛
    ``Illegal types of arguments``。它也曾经宽松过(老版本会把字符串转成数字),
    结果是"迁移过来的查询静默变了语义",后来被收紧。
    mini-clickhouse 跟着严格:能比的(数值/字符串/日期/枚举/UUID 同族)才放行。
    """
    if ta.equals(tb):
        return
    if ta.is_numeric() and tb.is_numeric():
        return
    if ta.is_string() and tb.is_string():
        return
    if (ta.is_string() and tb.is_enum()) or (tb.is_string() and ta.is_enum()):
        return
    if ta.is_uuid() and tb.is_uuid():
        return
    # 日期与字符串:允许(真实 CH 会把字符串按日期解析)
    if (is_date_time_like(ta) and tb.is_string()) or (is_date_time_like(tb) and ta.is_string()):
        return
    if is_date_time_like(ta) and is_date_time_like(tb):
        return
    raise_error(
        ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
        f"Illegal types of arguments for function {name}: "
        f"{ta.get_name()}, {tb.get_name()}",
    )


@_register("equals", aliases=["=="], description="相等比较,返回 UInt8")
class FunctionEquals(_Comparison):
    """``equals(a, b)`` —— 相等,返回 ``UInt8``(1/0)。别名 ``==``。

    .. code-block:: sql

       SELECT equals(1, 1), 1 == 1, 'abc' = 'abd', NULL = NULL;
       -- 1   1   0   NULL      ← 最后一个是 NULL,不是 0!

    .. warning:: **NULL 的语义**。``equals`` 遵循默认的 NULL 传播:
       任一侧为 NULL,结果就是 NULL(而不是 0)。这会让
       ``WHERE a = b`` 自动过滤掉 NULL 行(和 SQL 标准一致),
       但会让 ``sum(a = b)`` 把 NULL 行**排除**在外 ——
       想统计"包括 NULL 在内的不等"必须用 ``isNotDistinctFrom``。
    """

    op = "eq"


@_register("notEquals", aliases=["!=", "<>"], description="不等比较,返回 UInt8")
class FunctionNotEquals(_Comparison):
    """``notEquals(a, b)`` —— 不等,别名 ``!=`` 与 ``<>``。

    注意 ``NULL != 1`` 的结果是 **NULL**,不是 1 —— 这是新手最容易翻车的地方:
    "找出所有不等于 1 的行" 这个需求必须写成
    ``WHERE a != 1 OR a IS NULL``(或者 ``WHERE NOT isNotDistinctFrom(a, 1)``)。
    """

    op = "ne"


@_register("less", aliases=["<"], description="小于比较,返回 UInt8")
class FunctionLess(_Comparison):
    """``less(a, b)`` —— 小于,别名 ``<``。字符串按**字节序**比较::

       SELECT 'Z' < 'a', '10' < '9';
       -- 1   1        ← 字符串比较是逐字节的,'1'(0x31) < '9'(0x39)
    """

    op = "lt"


@_register("lessOrEquals", aliases=["<="], description="小于等于比较,返回 UInt8")
class FunctionLessOrEquals(_Comparison):
    """``lessOrEquals(a, b)`` —— 小于等于,别名 ``<=``。"""

    op = "le"


@_register("greater", aliases=[">"], description="大于比较,返回 UInt8")
class FunctionGreater(_Comparison):
    """``greater(a, b)`` —— 大于,别名 ``>``。"""

    op = "gt"


@_register("greaterOrEquals", aliases=[">="], description="大于等于比较,返回 UInt8")
class FunctionGreaterOrEquals(_Comparison):
    """``greaterOrEquals(a, b)`` —— 大于等于,别名 ``>=``。"""

    op = "ge"


# ══════════════════════════════════════════════════════════════════════════
# NULL 判定与"三值逻辑之外的"比较
# ══════════════════════════════════════════════════════════════════════════


@_register("isNull", description="判断是否为 NULL")
class FunctionIsNull(FunctionBase):
    """``isNull(x)`` —— 是 NULL 返回 1,否则返回 0。

    **这是必须自己处理 NULL 的一类函数**:默认的 NULL 传播会在
    ``isNull`` 看到 NULL 之前就把结果变成 NULL —— 那这个函数就永远返回不了 1,
    也就不可能用来"找出 NULL 行"了。所以:

    .. code-block:: text

       class FunctionIsNull(FunctionBase):
           propagate_nulls = False      ← 唯一需要做的就是这件事

    返回值是 ``UInt8`` 且**永不为 NULL**(``isNull`` 的答案总是确定的),
    这也是 ``return_type_cannot_be_nullable`` 的典型用例。
    """

    propagate_nulls = False
    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        return _null_mask_column(args[0], rows, invert=False)

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return 1 if values[0] is None else 0


@_register("isNotNull", description="判断是否不为 NULL")
class FunctionIsNotNull(FunctionIsNull):
    """``isNotNull(x)`` —— 不是 NULL 返回 1,否则返回 0。``isNull`` 的反面。"""

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        return _null_mask_column(args[0], rows, invert=True)

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return 0 if values[0] is None else 1


def _null_mask_column(arg: ColumnWithTypeAndName, rows: int, invert: bool) -> IColumn:
    """``isNull`` / ``isNotNull`` 共用的取空位图逻辑。

    直接用 ``ColumnNullable.null_map`` —— 这是"位置图 + 默认值"设计的又一次回报:
    判空是**读一个位**,不是逐行取值比较,所以它天然是 O(rows) 且可以向量化。
    """
    from ..columns import ColumnNullable

    col = arg.column
    if isinstance(col, ColumnNullable):
        data = np.asarray(col.null_map[:rows], dtype=np.uint8)
    elif arg.type.is_nullable():
        data = np.fromiter((1 if col.get(i) is None else 0 for i in range(rows)), dtype=np.uint8, count=rows)
    else:
        data = np.zeros(rows, dtype=np.uint8)
    return ColumnVector(np.uint8, (1 - data) if invert else data)


@_register("isNotDistinctFrom", aliases=["<=>"], description="不区分 NULL 的相等比较")
class FunctionIsNotDistinctFrom(FunctionBase):
    """``isNotDistinctFrom(a, b)`` —— "无法区分"(相等,或两边都是 NULL),别名 ``<=>``。

    .. code-block:: text

       a       b       a = b      a <=> b
       ─────   ─────   ────────   ────────
       1       1       1          1
       1       2       0          0
       NULL    2       NULL       0        ← 能区分:一个是 NULL 一个不是
       NULL    NULL    NULL       1        ← 不能区分:都是 NULL

    这是"NULL 安全比较"(NULL-safe comparison)。MySQL 里同一个东西叫 ``<=>``,
    PostgreSQL 里叫 ``IS NOT DISTINCT FROM``;ClickHouse 两个名字都认。

    **它和 ``equals`` 的对比是整个 SQL 里 NULL 语义的缩影**:

    * ``equals`` 回答的是 "这两个值相等吗" —— NULL 表示"不知道",所以结果是 NULL;
    * ``isNotDistinctFrom`` 回答的是 "这两个值**能不能区分开**" ——
      NULL 也是能观察到的值,所以结果是确定的 0/1。

    什么时候必须用它? 判断两列"内容一模一样"时::

       -- 找出两次快照之间真正有变化的行(NULL → 1 也算变化)
       SELECT count() FROM s1 INNER JOIN s2 USING id
       WHERE NOT isNotDistinctFrom(s1.v, s2.v);

    它 ``propagate_nulls = False``(要自己看 NULL),结果**永不为 NULL**。
    """

    propagate_nulls = False
    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        _check_comparable(self.name, ta, tb)
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        ta, tb = args[0].type, args[1].type
        self.get_return_type([ta, tb])
        a = full_column(args[0])
        b = full_column(args[1])
        na, nb = unwrap_nullable(ta), unwrap_nullable(tb)
        out = ColumnVector(np.uint8)
        for i in range(rows):
            va, vb = a.get(i), b.get(i)
            if va is None or vb is None:
                out.append(1 if (va is None and vb is None) else 0)
                continue
            x, y = _comparable_pair(na, nb, va, vb)
            out.append(1 if x == y else 0)
        return out


@_register("isDistinctFrom", description="不区分 NULL 的不等比较")
class FunctionIsDistinctFrom(FunctionIsNotDistinctFrom):
    """``isDistinctFrom(a, b)`` —— ``isNotDistinctFrom`` 的反面。

    .. code-block:: text

       1 vs 2        → 1      能区分
       1 vs 1        → 0
       NULL vs NULL  → 0      不能区分
       NULL vs 1     → 1      能区分

    它是"找变化"最直白的写法(见 :class:`FunctionIsNotDistinctFrom` 的例子)。
    """

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        col = super().execute(args, rows, context)
        return ColumnVector(np.uint8, np.asarray(1 - col.numpy_view(), dtype=np.uint8))


# ══════════════════════════════════════════════════════════════════════════
# greatest / least —— 变参,且**忽略 NULL**
# ══════════════════════════════════════════════════════════════════════════


class _GreatestLeast(FunctionBase):
    """``greatest`` / ``least`` 的公共骨架。

    两个语义点:

    * **NULL 被忽略** —— ``greatest(1, NULL, 3) = 3``。真实 CH 就是这么定义的,
      而且这是它和 ``max()`` 聚合的关键区别(``max`` 聚合同样忽略 NULL,
      但结果类型固定;而 ``greatest`` 是逐行的)。
    * **结果类型是所有实参的公共类型**(见 :func:`common_supertype`):
      ``greatest(toUInt8(1), toUInt16(2))`` 的类型是 ``UInt16`` ——
      取"更宽的那个",而**不做** ``plus`` 那种"进一档"
      (取最大值不会让数值变大,所以没必要多要一位)。

    它 ``propagate_nulls = False``:要自己看 NULL 才知道该忽略哪一个。
    """

    #: ``True`` = 取最大(greatest),``False`` = 取最小(least)
    want_max: bool = True

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, None)
        base = unwrap_nullable(args[0])
        any_nullable = args[0].is_nullable()
        for t in args[1:]:
            base = common_supertype(base, unwrap_nullable(t))
            any_nullable = any_nullable or t.is_nullable()
        if any_nullable and base.nullable_allowed:
            return DataTypeNullable(base)
        return base

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, None)
        ret = self.get_return_type([a.type for a in args])
        naive = [unwrap_nullable(a.type) for a in args]
        cols = [full_column(a) for a in args]
        out = ret.create_column()
        for i in range(rows):
            values = [c.get(i) for c in cols]
            if all(v is None for v in values):
                out.append(None)
                continue
            best_value: Optional[Any] = None
            best_type: Optional[DataType] = None
            for v, t in zip(values, naive):
                if v is None:
                    continue
                if best_value is None:
                    best_value, best_type = v, t
                    continue
                x, y = _comparable_pair(best_type, t, best_value, v)
                if (x < y) if self.want_max else (x > y):
                    best_value, best_type = v, t
            out.append(coerce_value(ret, best_value))
        return out


@_register("greatest", description="多个值中的最大者(NULL 被忽略)")
class FunctionGreatest(_GreatestLeast):
    """``greatest(a, b, ...)`` —— 返回最大的那个值,**NULL 被忽略**。

    .. code-block:: sql

       SELECT greatest(1, 5, 3), greatest(1, NULL, 3), greatest(NULL, NULL);
       -- 5   3   NULL

    .. note:: 它与 ``max(x, y)`` 的区别:``max`` 是**聚合函数**(把一列压成一行),
       ``greatest`` 是**标量函数**(一行进、一行出)。写
       ``SELECT max(a, b) FROM t`` 是错的 —— 那不是"行内取大",
       而是"把 b 忽略掉只聚合 a"(真实 CH 会直接报错)。
    """

    want_max = True


@_register("least", description="多个值中的最小者(NULL 被忽略)")
class FunctionLeast(_GreatestLeast):
    """``least(a, b, ...)`` —— 返回最小的那个值,NULL 被忽略。见
    :class:`FunctionGreatest`。"""

    want_max = False


# ══════════════════════════════════════════════════════════════════════════
# nullIf
# ══════════════════════════════════════════════════════════════════════════


@_register("nullIf", description="两值相等时返回 NULL,否则返回第一个值")
class FunctionNullIf(FunctionBase):
    """``nullIf(a, b)`` —— ``a = b`` 时返回 NULL,否则返回 ``a``。

    .. code-block:: sql

       SELECT nullIf(1, 1), nullIf(1, 2), toTypeName(nullIf(1, 1));
       -- NULL   1   Nullable(UInt8)

    典型用途是"把哨兵值变成真正的 NULL":

    .. code-block:: sql

       -- 老数据里用 '' 表示未知,导入时转成 NULL
       SELECT nullIf(region, '') AS region FROM raw;

    与 ``if``/``coalesce`` 一样,它也要看见 NULL 才能正确工作,所以
    ``propagate_nulls = False``:``nullIf(NULL, 1)`` 的结果是 NULL(而不是"整体变 NULL")。

    ``.. warning::`` 结果类型**总是** ``Nullable`` —— 即使参数不可空。
    这正是 ``nullIf`` 存在的意义,不想变成 Nullable 就该用 ``if``。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        _check_comparable(self.name, ta, tb)
        if args[0].is_nullable() or ta.is_nothing():
            return args[0]
        return DataTypeNullable(ta)

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        ret = self.get_return_type([a.type for a in args])
        ta, tb = args[0].type, args[1].type
        na, nb = unwrap_nullable(ta), unwrap_nullable(tb)
        a = full_column(args[0])
        b = full_column(args[1])
        out = ret.create_column()
        for i in range(rows):
            va, vb = a.get(i), b.get(i)
            if va is None or vb is None:
                out.append(va)
                continue
            x, y = _comparable_pair(na, nb, va, vb)
            out.append(None if x == y else coerce_value(ret, va))
        return out


@_register("ifNull", aliases=["nvl"], description="第一个参数为 NULL 时返回第二个参数")
class FunctionIfNull(FunctionBase):
    """``ifNull(x, y)`` —— ``x`` 是 NULL 就返回 ``y``,否则返回 ``x``(别名 ``nvl``)。

    .. code-block:: sql

       SELECT ifNull(NULL, 0), ifNull(1, 0), toTypeName(ifNull(toNullable(1), 0));
       -- 0   1   UInt8

    它是 :class:`minich.functions.conditional.FunctionCoalesce` 的**二元特例**,
    也是把 ``Nullable`` 列拉回不可空的三种方式之一::

       coalesce(a, b, c, '')     -- 变参,任一支非空都行
       ifNull(ifNull(a, b), '')  -- 只要两支
       assumeNotNull(a)          -- 不断言检查,只把类型标记摘掉(危险)

    与 ``coalesce`` 一样,结果**是否可空**取决于"有没有哪一支保证非空":

    .. code-block:: text

       ifNull(x Nullable, 0)             → UInt8          第二支是常量 0,兜得住
       ifNull(x Nullable, y Nullable)    → Nullable(...)  两支都可能空
       ifNull(NULL, NULL)                → Nullable(Nothing)  只能全是 NULL
       ifNull(1, 'a')                    → 报错(没有公共类型)

    它 ``propagate_nulls = False``:必须**自己看见 NULL** 才知道该不该取第二支。
    如果走默认的 NULL 传播,``ifNull(NULL, 0)`` 会变成 NULL ——
    这个函数就彻底废了。这也是 :mod:`minich.functions.base` 里点名的那一类函数
    (``isNull`` / ``coalesce`` / ``ifNull`` / ``assumeNotNull`` / ``toTypeName``)。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        # 不调用 _check_comparable:这里的两个参数**不需要可比**,
        # 它们只需要有公共类型(common_supertype 会在没有时抛出清楚的消息)
        base = common_supertype(ta, tb)
        if base.is_nothing():
            # 两支都是 NULL → ``Nullable(Nothing)``(见 ``coalesce`` 的同款说明)。
            return DataTypeNullable(base)
        first_may_be_null = args[0].is_nullable() or ta.is_nothing()
        second_may_be_null = args[1].is_nullable() or tb.is_nothing()
        if first_may_be_null and second_may_be_null and base.nullable_allowed:
            return DataTypeNullable(base)
        return base

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        ret = self.get_return_type([a.type for a in args])
        if ret.is_nothing():
            from ..columns import ColumnNothing

            return ColumnNothing()
        a = full_column(args[0])
        b = full_column(args[1])
        out = ret.create_column()
        for i in range(rows):
            va = a.get(i)
            # 只读需要的那一支:第一支非 NULL 时**根本不去碰第二支**
            out.append(coerce_value(ret, va if va is not None else b.get(i)))
        return out
