"""条件函数 —— ``if`` / ``multiIf`` / ``coalesce`` / ``assumeNotNull`` …

这一族函数有两个"必须先想明白"的点,而且它们经常被写错。

**第一点:条件为 NULL 算什么?**

.. code-block:: sql

   SELECT if(NULL, 'a', 'b'), multiIf(NULL, 1, 2);
   -- b   2

条件被当成 ``UInt8`` 用,而 NULL 转成 ``UInt8`` 就是 0 —— 也就是**假**。
所以 ``if(NULL, a, b)`` 走的是 else 分支。这条规则让
``if(x > 0, ...)`` 在 ``x`` 是 NULL 时不会"整行消失",而是走 else ——
想区分"NULL"和"假"必须显式写 ``isNull(x)`` 才行。

**第二点:分支是"惰性"的。**

.. code-block:: sql

   SELECT if(x != 0, 1 / x, 0) FROM t;     -- x = 0 的那一行不会出事

真实 ClickHouse 有一个 ``short_circuit_function_evaluation`` 设置(默认开),
它会**按块**只求值被选中的那一支 —— 于是上面的 ``1 / x`` 在 ``x = 0`` 的块里
根本不会被算。mini-clickhouse 没有改表达式求值顺序的那一层,但做到了
"列级别"的惰性:

.. code-block:: text

   条件列是常量      → 直接返回被选中的那一支(另一支**一次都不碰**)
   条件列非空(常量)  → 逐行只读被选中的那一支的值(get(i) 只发生在选中侧)

也就是说:本函数**不会去读**没被选中的那一支。在真实 CH 里这靠
``short_circuit_function_evaluation`` 保证;mini-clickhouse 只保证函数内部
这一层,表达式本身是否被提前求值取决于求值器 —— 这个边界必须说清楚,
否则读者会以为"写了 if 就万事大吉"。

.. warning:: ``ifNull`` / ``nullIf`` 不在本模块注册 —— 它们已经在
   :mod:`minich.functions.comparison` 里注册了(两者都要做比较,
   放在一起更自然)。本模块只提供 ``if``/``multiIf``/``coalesce``/
   ``assumeNotNull``/``toNullable`` 这些"选择与类型标记"类函数。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from ..block import ColumnWithTypeAndName
from ..columns import ColumnNothing, ColumnNullable, IColumn
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.composite import DataTypeNullable
from .arithmetic import _register
from .arithmetic import (
    check_arity,
    coerce_value,
    common_supertype,
    full_column,
    unwrap_nullable,
)
from .base import FunctionBase

__all__ = [
    "is_true",
    "branch_return_type",
    "read_value",
    "FunctionIf",
    "FunctionMultiIf",
    "FunctionCaseWithExpression",
    "FunctionCoalesce",
    "FunctionAssumeNotNull",
    "FunctionToNullable",
]


def is_true(value: Any) -> bool:
    """``if`` 系列里的"条件为真"判定 —— **非零即真,NULL 即假**。

    真实 CH 会把条件转成 ``UInt8``(NULL → 0),所以这里的判定顺序是:
    先排除 NULL,再看是否为零。字符串按"非空即真"处理
    (真实 CH 会先把字符串转成数字,mini 这里简化了 —— 见模块 docstring)。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return len(value) > 0
    return value != 0


def read_value(column: IColumn, type_: DataType, index: int) -> Any:
    """读第 ``index`` 行的值 —— ``Nothing`` 类型的列恒为空,于是恒为 NULL。

    ``ColumnNothing`` 的 ``get`` 会抛 ``ARGUMENT_OUT_OF_BOUND``(它"永远是空的"),
    所以 ``if(1, NULL, NULL)`` 之类的表达式必须走这一层过滤,
    否则会在读值时炸掉。真实 CH 里同样有一堆"遇到 Nothing 就返回空列"的特判。
    """
    if unwrap_nullable(type_).is_nothing():
        return None
    return column.get(index)


def branch_return_type(branch_types: Sequence[DataType]) -> DataType:
    """若干"分支"的公共类型 —— 任一支可空(或为 Nothing)则结果可空。

    ``if(x, 1, NULL)`` 的类型是 ``Nullable(UInt8)``:值为 NULL 的那一支
    让整个表达式"可能没有值",所以结果必须带上 Nullable 标记。
    反过来,**条件**可空并不会让结果可空 —— 因为 NULL 条件走 else,
    结果照样有一个确定的值。
    """
    if not branch_types:
        return get_data_type("Nothing")
    base = unwrap_nullable(branch_types[0])
    any_nullable = branch_types[0].is_nullable()
    for t in branch_types:
        inner = unwrap_nullable(t)
        if inner.is_nothing():
            any_nullable = True
            continue
        base = inner if base.is_nothing() else common_supertype(base, inner)
        any_nullable = any_nullable or t.is_nullable()
    if base.is_nothing():
        # 所有分支都是 NULL 字面量:结果类型是 Nothing(只能表示 NULL 的类型)
        return base
    if any_nullable and base.nullable_allowed:
        return DataTypeNullable(base)
    return base


def _append_value(out: IColumn, ret: DataType, value: Any) -> None:
    out.append(coerce_value(ret, value))


@_register("if", description="条件表达式(只读取被选中的分支)")
class FunctionIf(FunctionBase):
    """``if(cond, then, else)`` —— 三分支条件表达式。

    .. code-block:: sql

       SELECT if(1, 'yes', 'no'), if(0, 'yes', 'no'), if(NULL, 'yes', 'no');
       -- yes   no   no          ← NULL 条件按"假"处理

    实现上的核心是 :meth:`execute` 里那段循环:它**只读被选中那一支**
    的 ``get(i)``。这就是"列级别的惰性求值" —— 配合"条件列是常量"的快路径,
    ``if(1, expensive_a, b)`` 会完全跳过 ``b``。

    结果类型 = 两个分支的公共类型(任一支可空则结果可空)::

       if(x, toUInt8(1), toUInt16(2))  → UInt16
       if(x, 1, NULL)                  → Nullable(UInt8)
       if(x, 1, 'a')                   → Illegal types of arguments(没有公共类型)
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 3, 3)
        return branch_return_type([args[1], args[2]])

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 3, 3)
        ret = self.get_return_type([a.type for a in args])
        if ret.is_nothing():
            return ColumnNothing()
        cond_col = args[0].column
        out = ret.create_column()
        if cond_col.is_const():
            # 快路径:条件恒真/恒假 → **另一支一次都不碰**(真正的短路)
            picked = 1 if is_true(cond_col.get(0)) else 2
            if unwrap_nullable(args[picked].type).is_nothing():
                return ColumnNothing()
            chosen = full_column(args[picked])
            for i in range(rows):
                _append_value(out, ret, read_value(chosen, args[picked].type, i))
            return out
        then_col = full_column(args[1])
        else_col = full_column(args[2])
        for i in range(rows):
            if is_true(cond_col.get(i)):
                _append_value(out, ret, read_value(then_col, args[1].type, i))
            else:
                _append_value(out, ret, read_value(else_col, args[2].type, i))
        return out


@_register("multiIf", description="多重条件(奇数个参数,最后一个是 else)")
class FunctionMultiIf(FunctionBase):
    """``multiIf(cond1, then1, cond2, then2, ..., else)`` —— 多重条件。

    .. code-block:: sql

       SELECT multiIf(x < 0, 'neg', x = 0, 'zero', 'pos') FROM t;

    它等价于嵌套的 ``if``,但是**平的**:

    .. code-block:: text

       multiIf(c1, v1, c2, v2, d)  ==  if(c1, v1, if(c2, v2, d))

    表达式求值器更喜欢平的结构(少一层递归),所以真实 ClickHouse 里
    很多"条件分类"最后都落到这个函数上。

    **参数个数必须是奇数**:每两个一组 ``(条件, 值)``,最后一个是
    所有条件都不满足时的默认值。这是真实 CH 的硬性要求 ——
    它解释了为什么 ``multiIf`` 不能省略 else(否则"所有条件都不满足"时
    结果该是什么就无从知道了)。

    返回类型 = 所有"值"位置参数与默认值的公共类型。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 3, None)
        if len(args) % 2 == 0:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"Number of arguments for function {self.name} must be odd: "
                f"(cond, then) pairs plus one else branch, passed {len(args)}",
            )
        return branch_return_type(list(args[1::2]))

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 3, None)
        ret = self.get_return_type([a.type for a in args])
        if ret.is_nothing():
            return ColumnNothing()
        cols = list(args)
        out = ret.create_column()
        last = len(cols) - 1
        for i in range(rows):
            value: Any = None
            matched = False
            for k in range(0, last, 2):
                if is_true(cols[k].column.get(i)):
                    value = read_value(cols[k + 1].column, cols[k + 1].type, i)
                    matched = True
                    break
            if not matched:
                value = read_value(cols[last].column, cols[last].type, i)
            _append_value(out, ret, value)
        return out


@_register("caseWithExpression", description="CASE expr WHEN v THEN r ... ELSE d END")
class FunctionCaseWithExpression(FunctionBase):
    """``caseWithExpression(expr, variant, result, ..., default)`` —— SQL 的 ``CASE``。

    .. code-block:: sql

       CASE status WHEN 200 THEN 'ok' WHEN 404 THEN 'missing' ELSE 'other' END
       -- 等价于
       caseWithExpression(status, 200, 'ok', 404, 'missing', 'other')

    参数结构:第一个是"被比较的表达式",后面是成对的 ``(变体, 结果)``,
    最后**可选**一个默认值。于是参数个数总是 ``1 + 2n``(无默认值)
    或 ``1 + 2n + 1``(有默认值)—— 函数自己就能判断出来:

    .. code-block:: text

       (len(args) - 1) 是偶数 → 没有默认值,最后一个参数是 result
       (len(args) - 1) 是奇数 → 最后一个参数是 default

    比较用的是**相等**语义,所以别忘了它的 NULL 规则:``CASE NULL WHEN NULL``
    **不会**匹配(NULL 不等于任何东西,包括 NULL 自己),于是直接落到默认值分支。
    想要 NULL 安全的匹配就得先 ``ifNull(...)`` 或者用 ``multiIf`` 显式判空。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 3, None)
        has_default = (len(args) - 1) % 2 == 1
        result_types: List[DataType] = list(args[2::2])
        if has_default:
            result_types.append(args[-1])
        return branch_return_type(result_types)

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 3, None)
        ret = self.get_return_type([a.type for a in args])
        if ret.is_nothing():
            return ColumnNothing()
        cols = list(args)
        has_default = (len(args) - 1) % 2 == 1
        out = ret.create_column()
        for i in range(rows):
            subject = read_value(cols[0].column, cols[0].type, i)
            value: Any = None
            matched = False
            for k in range(1, len(cols) - 1, 2):
                variant = read_value(cols[k].column, cols[k].type, i)
                # NULL 与任何值都不相等 —— 匹配不上就走默认值
                if subject is not None and variant is not None and subject == variant:
                    value = read_value(cols[k + 1].column, cols[k + 1].type, i)
                    matched = True
                    break
            if not matched:
                value = read_value(cols[-1].column, cols[-1].type, i) if has_default else None
            _append_value(out, ret, value)
        return out


@_register("coalesce", description="返回第一个非 NULL 的参数")
class FunctionCoalesce(FunctionBase):
    """``coalesce(a, b, ...)`` —— 返回第一个非 NULL 的参数。

    .. code-block:: sql

       SELECT coalesce(NULL, NULL, 3), coalesce(NULL, 'x'), toTypeName(coalesce(NULL, 1));
       -- 3   x   UInt8      ← 结果类型**不是** Nullable(有兜底值)

    它是"填补缺失值"的标准手段,也是把 ``Nullable`` 列**安全地拉回不可空**的
    方式(与 ``assumeNotNull`` 的"断言"不同,coalesce 是真的检查了值)::

       -- 把 Nullable(String) 变成 String,缺的补 ''
       SELECT coalesce(name, '') FROM t;

    .. note:: 是否可空,取决于"有没有哪个实参保证非空":

       * ``coalesce(NULL, 1)`` → ``UInt8``(第二支是常量 1,永远兜得住);
       * ``coalesce(NULL, NULL)`` → ``Nullable(Nothing)``(没有非空分支,只能全是 NULL);
       * ``coalesce(x, y)`` 两个都可空 → ``Nullable(UInt8)``。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, None)
        base: Optional[DataType] = None
        all_nullable = True
        for t in args:
            inner = unwrap_nullable(t)
            all_nullable = all_nullable and (t.is_nullable() or inner.is_nothing())
            if inner.is_nothing():
                continue
            base = inner if base is None else common_supertype(base, inner)
        if base is None:
            # 所有实参都是 NULL:结果类型是 ``Nullable(Nothing)``
            # (不是裸 ``Nothing``)—— 真实 CH 对"只可能为 NULL"的表达式
            # 一律给 ``Nullable(Nothing)``,``SELECT NULL`` 本身就是这个类型。
            # 返回裸 ``Nothing`` 会连带把结果列做成 0 行的 ``ColumnNothing``,
            # 于是 ``coalesce(NULL, NULL)`` 报 "produced 0 rows" 的错。
            return DataTypeNullable(get_data_type("Nothing"))
        if all_nullable and base.nullable_allowed:
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
        if ret.is_nothing():
            return ColumnNothing()
        cols = list(args)
        out = ret.create_column()
        for i in range(rows):
            value: Any = None
            for a in cols:
                v = read_value(a.column, a.type, i)
                if v is not None:
                    value = v
                    break
            _append_value(out, ret, value)
        return out


@_register("assumeNotNull", description="去掉 Nullable 标记(断言式,不检查值)")
class FunctionAssumeNotNull(FunctionBase):
    """``assumeNotNull(x)`` —— 把 ``Nullable(T)`` **断言**成 ``T``。

    .. code-block:: sql

       SELECT assumeNotNull(toNullable(1)), toTypeName(assumeNotNull(toNullable(1)));
       -- 1   UInt8

    它**不是转换,是承诺**:它不检查值到底是不是 NULL,只是告诉类型系统
    "把 Nullable 标记去掉"。所以真的遇到 NULL 时,你会拿到该类型的
    **零值**(``0`` / ``''`` / ``[]``)—— 因为 ``ColumnNullable`` 的 NULL 位置上
    存的本来就是零值(见 ``columns/composite.py`` 的"位置图 + 默认值")。
    真实 ClickHouse 的文档明确说这是**未定义行为**,只是实践中如此。

    为什么需要它? 因为 ``Nullable`` 会**传染**:一个可空列参与比较/聚合,
    结果类型全都跟着变 Nullable,而 Nullable 会关掉一批优化
    (见 :class:`minich.types.composite.DataTypeNullable` 的 warning)。
    在"业务上确定不可能为 NULL,只是建表时写成了 Nullable"的场景里,
    用它把标记摘掉能让整条查询回到快路径。

    正确用法只有一种:紧跟一次判空过滤之后::

       SELECT assumeNotNull(v) FROM t WHERE v IS NOT NULL;
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return unwrap_nullable(args[0])

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        ret = self.get_return_type([args[0].type])
        if ret.is_nothing():
            return ColumnNothing()
        col = args[0].column
        out = ret.create_column()
        if isinstance(col, ColumnNullable):
            # 直接取 nested 列:NULL 的位置上本来就是零值
            nested = _nested_full(col)
            for i in range(rows):
                out.append(coerce_value(ret, nested.get(i)))
            return out
        full = full_column(args[0])
        for i in range(rows):
            out.append(coerce_value(ret, full.get(i)))
        return out


def _nested_full(col: ColumnNullable) -> IColumn:
    """取 ``ColumnNullable`` 的非空侧列(常量则展开)。"""
    nested = col.nested
    return nested.convert_to_full_column() if nested.is_const() else nested


@_register("toNullable", description="把类型包成 Nullable(T)")
class FunctionToNullable(FunctionBase):
    """``toNullable(x)`` —— 把 ``T`` 包成 ``Nullable(T)``。

    .. code-block:: sql

       SELECT toTypeName(toNullable(1)), toNullable(NULL);
       -- Nullable(UInt8)   NULL

    用途主要是两个:

    1. **让分支/列的类型对得上**。``if(x > 0, NULL, 1)`` 会自动得到
       ``Nullable(UInt8)``;而如果你想**显式**构造一个可空结果
       (比如往 ``Nullable`` 列里插数据),就要 ``toNullable(1)``;
    2. **测试 NULL 相关行为**:它是造"所有行同一个值、但带 Nullable 标记"
       的列的最短写法。

    已经是 Nullable 的类型原样返回 —— ``Nullable(Nullable(T))`` 在类型系统里
    是非法的(见 :class:`minich.types.composite.DataTypeNullable`)。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        inner = unwrap_nullable(args[0])
        if args[0].is_nullable() or inner.is_nothing():
            return args[0]
        return DataTypeNullable(inner)

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        ret = self.get_return_type([args[0].type])
        col = args[0].column
        if isinstance(col, ColumnNullable):
            return col
        if ret.is_nothing():
            return ColumnNothing()
        full = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            out.append(coerce_value(ret, full.get(i)))
        return out
