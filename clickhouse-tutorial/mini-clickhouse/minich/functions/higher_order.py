"""高阶函数 —— 参数里有一个 **lambda**,而不是一个值。

先讲清楚"引擎怎么把 lambda 交给我们",因为这是本模块最反直觉的地方:

.. code-block:: text

   表达式 arrayMap(x -> x * 2, [1, 2, 3]) 的求值过程

   ① 求值器看到 arrayMap 的第一个参数是 `x -> x * 2`
      —— 它**不求值**这段表达式,
   ② 而是把它包装成一个 minich.functions.base.LambdaArgument:

        LambdaArgument(
            names    = ["x"],                 ← 形参名
            body     = <把 x 这一列映射成结果列的可调用对象>,
            captured = [<外层作用域用到的列>],  ← 闭包捕获的环境
        )

   ③ arrayMap 收到 [LambdaArgument, ColumnArray] —— **第一个"参数"不是列**;
   ④ arrayMap 自己决定怎么调用它:通常是
      `lam.body(x = <元素列>)`,拿回一个 IColumn,再按行组装。

.. code-block:: text

   ┌─────────────── arrayMap 的输入 ───────────────┐
   │ args[0] = LambdaArgument(names=['x'])          │
   │ args[1] = ColumnArray(nested=[1,2,3], off=[3]) │
   └────────────────────────────────────────────────┘
                    ▼ 对每一行:
   ┌ nested 列切片 ─┐   ┌ lam.call(x=切片) ┐
   │ ColumnVector   │ → │ ColumnVector     │ → [2,4,6]
   │ [1,2,3]        │   │ [2,4,6]          │
   └────────────────┘   └──────────────────┘

**捕获环境**那一条尤其值得注意:lambda 里可以引用外层的列,比如

.. code-block:: sql

   SELECT arrayMap(x -> x + outer_col, arr) FROM t;

此时 `outer_col` 不在形参里,而是被**闭包**(`body` 捕获的变量)带进来的。
所以 `LambdaArgument.captured` 只是"记账",真正的绑定发生在求值器构造
`body` 的时候 —— 我们调用 `lam.call(x=...)` 就能拿到正确结果。

.. warning::

   ``get_return_type`` 只能看到**类型**,看不到 lambda 的表达式树,
   所以 ``arrayMap`` / ``arrayFold`` 这类"结果类型取决于 lambda"的函数,
   声明的返回类型只能**近似**(见各函数的 docstring)。
   **数据本身是按 lambda 的真实输出列构造的**,所以算出来的值是对的;
   只有"列类型"这一项比真实 ClickHouse 宽松。真实 ClickHouse 会去
   分析 lambda 的表达式树来推导精确类型。
"""

from __future__ import annotations

from typing import Any, List, Sequence

from ..columns import ColumnArray
from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from .array import FunctionArrayReverseSort, FunctionArraySort  # noqa: F401  (再导出)
from .base import FunctionBase, LambdaArgument, register_function

__all__ = [
    "FunctionArrayMap",
    "FunctionArrayFilter",
    "FunctionArrayCount",
    "FunctionArrayExists",
    "FunctionArrayAll",
    "FunctionArrayFirst",
    "FunctionArrayFirstIndex",
    "FunctionArrayLast",
    "FunctionArrayLastIndex",
    "FunctionArrayFold",
    "FunctionArraySort",
    "FunctionArrayReverseSort",
    "lambda_element_columns",
]


_UINT8 = get_data_type("UInt8")
_UINT32 = get_data_type("UInt32")
_UINT64 = get_data_type("UInt64")


def _data_types(args: Sequence[Any]) -> List[DataType]:
    """从实参类型表里挑出真正的类型(去掉 lambda 那种没有类型的参数)。"""
    return [a for a in args if isinstance(a, DataType)]


def _full(column: Any) -> Any:
    """常量列展开成完整列 —— lambda 求值不能对着"只有一行"的常量列做。"""
    return column.convert_to_full_column() if column.is_const() else column


def lambda_element_columns(groups: Sequence[Sequence[Any]],
                           elem_types: Sequence[DataType]) -> List[Any]:
    """把每行的**元素列表**重新装成一列,喂给 lambda。

    为什么要"拆开再装回去"? 因为 lambda 的语义是"对列求值":
    ``x -> x * 2`` 里的 ``x`` 是一**列**。而高阶函数是按行遍历数组的,
    所以每一行都要重新构造一次元素列。真实 ClickHouse 更聪明 ——
    它直接在 ``ColumnArray.nested`` 这块连续内存上跑 lambda,
    一次算完整列再切;我们这里为了可读性按行做,性能差异见第 15 章。
    """
    cols = []
    for group, elem in zip(groups, elem_types):
        col = elem.create_column()
        for v in group:
            col.append(v)
        cols.append(col)
    return cols


def _padded(groups: Sequence[Sequence[Any]], elem_types: Sequence[DataType]) -> List[List[Any]]:
    """多数组 lambda 的**长度对齐**。

    真实 ClickHouse 在长度不一致时**直接报错** ``SIZES_OF_ARRAYS_DONT_MATCH``
    (``arrayMap((x, y) -> x + y, [1,2,3], [10])`` 抛错,不返回任何结果)。
    本实现刻意放宽成"按元素默认值补齐",这是一处**有意的偏差**。
    """
    n = max((len(g) for g in groups), default=0)
    out = []
    for g, elem in zip(groups, elem_types):
        if len(g) < n:
            out.append(list(g) + [elem.default_value()] * (n - len(g)))
        else:
            out.append(list(g))
    return out


def _array_column(nested: Any, offsets: List[int]) -> ColumnArray:
    """用现成的嵌套列 + 偏移数组拼一个 ``ColumnArray``。"""
    col = ColumnArray(nested)
    col.offsets = offsets
    return col


class _LambdaFunction(FunctionBase):
    """所有高阶函数的底座:第一个实参是 lambda(或数组)。"""

    #: lambda 之后还有几个"数组"参数
    def array_args(self, args: Sequence[Any]) -> Sequence[Any]:
        return args

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        lam = args[0]
        array_cols = [_full(a.column) for a in args[1:]]
        array_types = [a.type for a in args[1:]]
        elem_types = [t.get_elements_type() for t in array_types]
        for et, t in zip(elem_types, array_types):
            if et is None:
                raise_error(
                    ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                    f"{self.name}: expected an Array argument, got {t.get_name()}",
                )
        grouped = [[c.get(i) or [] for c in array_cols] for i in range(rows)]
        return self.run(lam, grouped, elem_types, [a.type for a in args[1:]])

    def run(self, lam: LambdaArgument, grouped: List[List[List[Any]]],
            elem_types: List[DataType], all_types: Sequence[Any]) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: run() not implemented")


# ══════════════════════════════════════════════════════════════════════════
# arrayMap
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayMap(_LambdaFunction):
    """``arrayMap(func, arr1[, arr2, ...])`` —— 逐元素映射。

    支持**多数组**形态:``arrayMap((x, y) -> x + y, a, b)`` ——
    此时 lambda 收两个参数(两个元素列),长度短的数组按默认值补齐。

    .. warning::

       声明的返回类型取**第一个数组的元素类型**(``Array(T)``)——
       因为 lambda 的返回类型要分析表达式树才能得到,而那不在本实现的
       能力范围内。**数据**是按 lambda 真实输出列写的,所以数值是对的;
       若第一个数组是 ``UInt8`` 而 ``x * 2`` 会产生 ``UInt16`` 的值,
       请显式写 ``arrayMap(x -> toUInt16(x * 2), ...)`` 来固定列类型。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        types = _data_types(args)
        for t in types:
            if t.is_array():
                elem = t.get_elements_type()
                if elem is not None:
                    return get_data_type(f"Array({elem.get_name()})")
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            "arrayMap expects at least one Array argument",
        )

    def run(self, lam, grouped, elem_types, all_types):
        nested = None
        offsets: List[int] = []
        total = 0
        for groups in grouped:
            padded = _padded(groups, elem_types)
            cols = lambda_element_columns(padded, elem_types)
            result = lam.call(*cols)
            n = len(padded[0]) if padded else 0
            if nested is None:
                nested = result.clone_empty()
            nested.insert_range_from(result, 0, n)
            total += n
            offsets.append(total)
        if nested is None:
            decl = self.get_return_type(all_types)
            nested = decl.get_elements_type().create_column()
        return _array_column(nested, offsets)


register_function("arrayMap", FunctionArrayMap, aliases=["array_map"],
                  description="对数组每个元素应用 lambda")


# ══════════════════════════════════════════════════════════════════════════
# arrayFilter / arrayCount / arrayExists / arrayAll
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayFilter(_LambdaFunction):
    """``arrayFilter(func, arr)`` —— 保留断定式为真的元素。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        types = _data_types(args)
        for t in types:
            if t.is_array():
                return t
        raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, "arrayFilter expects an Array")

    def run(self, lam, grouped, elem_types, all_types):
        out = self.get_return_type(all_types).create_column()
        for groups in grouped:
            mask = [bool(x) for x in _predicate(lam, groups[0], elem_types[0]).to_list()]
            out.append([v for v, keep in zip(groups[0], mask) if keep])
        return out


class _CountLike(_LambdaFunction):
    """``arrayCount`` / ``arrayExists`` / ``arrayAll`` 共用的"统计断定式"逻辑。"""

    def run(self, lam, grouped, elem_types, all_types):
        out = self.get_return_type(all_types).create_column()
        for groups in grouped:
            flags = [
                bool(x) for x in _predicate(lam, groups[0], elem_types[0]).to_list()
            ]
            out.append(self.value(flags))
        return out

    def value(self, flags: List[bool]) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "not implemented")


class FunctionArrayCount(_CountLike):
    """``arrayCount([func,] arr)`` —— 断定式为真的元素个数。

    不带 lambda 时,统计的是**非零**元素的个数(``0``/``''``/``[]`` 为假)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        if args and isinstance(args[0], LambdaArgument):
            return super().execute(args, rows, context)
        # 没有 lambda:把元素本身当成布尔,统计"非零"的个数
        col = _full(args[0].column)
        out = self.get_return_type([a.type for a in args[1:]]).create_column()
        for i in range(rows):
            out.append(sum(1 for x in (col.get(i) or []) if x))
        return out

    def value(self, flags):
        return sum(1 for f in flags if f)


class FunctionArrayExists(_CountLike):
    """``arrayExists(func, arr)`` —— 是否存在满足断定式的元素。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def value(self, flags):
        return 1 if any(flags) else 0


class FunctionArrayAll(_CountLike):
    """``arrayAll(func, arr)`` —— 是否**所有**元素都满足断定式(空数组为 1)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def value(self, flags):
        return 1 if all(flags) else 0


def _predicate(lam: LambdaArgument, group: Sequence[Any], elem: DataType) -> Any:
    """对一行的数组求一次布尔断定式,返回一列。"""
    col = elem.create_column()
    for v in group:
        col.append(v)
    return lam.call(col)


register_function("arrayFilter", FunctionArrayFilter, aliases=["array_filter"],
                  description="按 lambda 过滤数组元素")
register_function("arrayCount", FunctionArrayCount, aliases=["array_count"],
                  description="统计满足 lambda 的元素个数(无 lambda 时数非零)")
register_function("arrayExists", FunctionArrayExists,
                  description="是否存在满足 lambda 的元素")
register_function("arrayAll", FunctionArrayAll,
                  description="是否所有元素都满足 lambda(空数组为 1)")


# ══════════════════════════════════════════════════════════════════════════
# arrayFirst / arrayLast 系列
# ══════════════════════════════════════════════════════════════════════════


class _FirstLast(_LambdaFunction):
    """``arrayFirst`` / ``arrayFirstIndex`` / ``arrayLast`` / ``arrayLastIndex``。"""

    from_end = False
    want_index = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        types = _data_types(args)
        if self.want_index:
            return _UINT32
        for t in types:
            if t.is_array():
                elem = t.get_elements_type()
                if elem is not None:
                    return elem
        raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, f"{self.name} expects an Array")

    def run(self, lam, grouped, elem_types, all_types):
        ret = self.get_return_type(all_types)
        out = ret.create_column()
        default = ret.default_value()
        for groups in grouped:
            flags = [bool(x) for x in _predicate(lam, groups[0], elem_types[0]).to_list()]
            idx = -1
            rng = range(len(flags) - 1, -1, -1) if self.from_end else range(len(flags))
            for j in rng:
                if flags[j]:
                    idx = j
                    break
            if idx < 0:
                out.append(0 if self.want_index else default)
            elif self.want_index:
                out.append(idx + 1)
            else:
                out.append(groups[0][idx])
        return out


class FunctionArrayFirst(_FirstLast):
    from_end = False
    want_index = False


class FunctionArrayFirstIndex(_FirstLast):
    from_end = False
    want_index = True


class FunctionArrayLast(_FirstLast):
    from_end = True
    want_index = False


class FunctionArrayLastIndex(_FirstLast):
    from_end = True
    want_index = True


register_function("arrayFirst", FunctionArrayFirst, aliases=["array_first"],
                  description="第一个满足 lambda 的元素(没有则给默认值)")
register_function("arrayFirstIndex", FunctionArrayFirstIndex,
                  description="第一个满足 lambda 的元素下标(1-based,没有为 0)")
register_function("arrayLast", FunctionArrayLast,
                  description="最后一个满足 lambda 的元素")
register_function("arrayLastIndex", FunctionArrayLastIndex,
                  description="最后一个满足 lambda 的元素下标(1-based,没有为 0)")


# ══════════════════════════════════════════════════════════════════════════
# arrayFold
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayFold(FunctionBase):
    """``arrayFold(func, arr1[, arr2, ...], accumulator)`` —— 归约。

    **累加器是最后一个参数**,不是第一个:

    .. code-block:: sql

       SELECT arrayFold((acc, x) -> acc + x, [1, 2, 3], toUInt64(0));
       -- 6

    lambda 的形参顺序是 ``(acc, x1[, x2, ...])`` —— 累加器在最前面。
    返回类型就是累加器的类型。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        types = _data_types(args)
        if not types:
            raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, "arrayFold expects arguments")
        return types[-1]

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        lam = args[0]
        if not isinstance(lam, LambdaArgument):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "arrayFold's first argument must be a lambda",
            )
        array_cols = [_full(a.column) for a in args[1:-1]]
        array_types = [a.type for a in args[1:-1]]
        elem_types = [t.get_elements_type() for t in array_types]
        acc_col = _full(args[-1].column)
        acc_type = args[-1].type
        ret = self.get_return_type([a.type for a in args[1:]])
        out = ret.create_column()
        for i in range(rows):
            groups = [c.get(i) or [] for c in array_cols]
            padded = _padded(groups, elem_types)
            acc = acc_col.get(i)
            for j in range(len(padded[0]) if padded else 0):
                cell_cols = lambda_element_columns(
                    [[p[j]] for p in padded], elem_types
                )
                acc_holder = acc_type.create_column()
                acc_holder.append(acc)
                result = lam.call(acc_holder, *cell_cols)
                acc = result.get(0)
            out.append(acc)
        return out


register_function("arrayFold", FunctionArrayFold, aliases=["array_fold"],
                  description="用一个 lambda 把数组归约成单个值(累加器在最后)")


# ══════════════════════════════════════════════════════════════════════════
# arraySort / arrayReverseSort 的 lambda 形态
# ══════════════════════════════════════════════════════════════════════════
#
# 这两个名字**已经在 array.py 里注册过了**(那里同时实现了"纯数组"和
# "带 lambda"两种形态)。这里只把类再导出一次,不再重复 register_function ——
# 因为重复注册会让"哪个模块后 import 谁生效"变成一个随机行为,而
# `system.functions` 里也会出现一条来源不明的重复记录。
#
# 两个类都从上面 import 进来并在 `__all__` 里再导出,所以
# `from minich.functions.higher_order import FunctionArraySort` 依然可用。
