"""数组函数 —— 全部围绕一条规则转:**下标从 1 开始**。

这一条与 Python、C、JavaScript 都相反,而且它**不报错** —— 只是给出
一个你不期待的值。所以本模块把三种"越界"情形在文档里重复写了三遍:

.. code-block:: text

   arr = ['a', 'b', 'c']

   arr[1]   = 'a'    ← 1-based
   arr[-1]  = 'c'    ← 负数从**尾部**数(-1 是最后一个)
   arr[0]   = ''     ← 0 **不是**越界,它返回元素的默认值
   arr[9]   = ''     ← 越界也返回默认值,而不是报错

   indexOf(arr, 'b') = 2      ← 同样 1-based,找不到是 **0**

真实 ClickHouse 之所以这么定,是因为它要跟 SQL 标准(UUID/数组下标、
``arrayElement``)以及 PCRE 的习惯保持一致;而"越界给默认值"是为了
**不打断向量化** —— 一行越界就抛异常会让整列退化成慢路径。
第 15 章用 `arrayElement` 举了这个例子。

.. note::

   本模块**没有** ``arrayUnion``。ClickHouse 里没有这个函数 —— 要做并集
   请用 ``arrayConcat`` + ``arrayDistinct``。凭空造一个"看起来合理"的
   函数名比不实现更糟:用户会以为它是标准的一部分。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from .base import FunctionBase, LambdaArgument, register_function

__all__ = [
    "common_type",
    "array_type",
    "elements_type",
]


_UINT8 = get_data_type("UInt8")
_UINT32 = get_data_type("UInt32")
_UINT64 = get_data_type("UInt64")
_INT64 = get_data_type("Int64")
_FLOAT64 = get_data_type("Float64")
_NOTHING = get_data_type("Nothing")


def array_type(element: DataType) -> DataType:
    """``Array(T)`` 的构造快捷方式。"""
    return get_data_type(f"Array({element.get_name()})")


def elements_type(t: DataType) -> Optional[DataType]:
    """``Array(T)`` / ``Map(K,V)`` 的元素类型;其它类型返回 ``None``。"""
    return t.get_elements_type()


def common_type(types: Sequence[DataType]) -> DataType:
    """求若干个类型的"公共超类型"。

    真实 ClickHouse 有一套完整的"超类型"推导(``getLeastSupertype``),
    覆盖 String/数值/Date/Array/……。mini-clickhouse 只做最常见的几类:

    * 全同 → 原样;
    * 全是数值 → 用 ``get_wider_numeric_type`` 逐级提升;
    * 有 ``Nothing`` 参与 → 取另一个(``[]`` 与 ``[1]`` 的公共类型是
      ``Array(UInt8)``);
    * 有 ``Nullable`` 参与 → 结果也可空;
    * 其余混搭 → ``String``(并且这属于"我们做得比真实 CH 宽松"的地方)。
    """
    if not types:
        return _NOTHING
    non_nothing = [t for t in types if not t.is_nothing()]
    if not non_nothing:
        return _NOTHING
    if len(non_nothing) == 1:
        return non_nothing[0]
    nullable = any(t.is_nullable() for t in non_nothing)
    stripped = [t.nested if t.is_nullable() else t for t in non_nothing]  # type: ignore[attr-defined]
    names = {t.get_name() for t in stripped}
    if len(names) == 1:
        base = stripped[0]
    elif all(t.is_numeric() for t in stripped):
        from ..types import get_wider_numeric_type

        base = stripped[0]
        for t in stripped[1:]:
            base = get_wider_numeric_type(base, t)
    elif all(t.is_string() for t in stripped):
        base = get_data_type("String")
    elif all(t.is_date() for t in stripped):
        base = get_data_type("Date32") if any(
            t.get_name() == "Date32" for t in stripped
        ) else get_data_type("Date")
    else:
        base = get_data_type("String")
    if nullable and base.nullable_allowed:
        return get_data_type(f"Nullable({base.get_name()})")
    return base


# ══════════════════════════════════════════════════════════════════════════
# 逐行底座(带类型)
# ══════════════════════════════════════════════════════════════════════════


class _ArrayFunction(FunctionBase):
    """把实参的**类型**一起交给 ``compute`` 的行式底座。

    "越界返回元素默认值"需要知道元素类型 —— ``execute_row`` 拿不到类型,
    所以这里重写 ``execute``。真实 ClickHouse 的 ``executeImpl`` 本来就
    同时握着列与类型,不存在这个限制。
    """

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: compute() not implemented")

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        cols = [
            a.column.convert_to_full_column() if a.column.is_const() else a.column
            for a in args
        ]
        types = [a.type for a in args]
        out = self.get_return_type(types).create_column()
        for i in range(rows):
            out.append(self.compute([c.get(i) for c in cols], types))
        return out


def _register_array_fn(name: str, ret: Any, impl: Any, *, aliases: Sequence[str] = (),
                       description: str = "") -> type:
    """注册一个"返回类型固定"的数组函数。"""

    class _F(_ArrayFunction):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            if callable(ret):
                return ret(args)
            return ret

        def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
            return impl(values)

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(name, _F, aliases=list(aliases), description=description)
    return _F


# ══════════════════════════════════════════════════════════════════════════
# 构造
# ══════════════════════════════════════════════════════════════════════════


class FunctionArray(_ArrayFunction):
    """``array(a, b, ...)`` —— 别名 ``[]``。

    元素类型取全体参数的**公共超类型**:``array(1, 2.5)`` 是
    ``Array(Float64)``,``array(1, NULL)`` 是 ``Array(Nullable(UInt8))``。
    """

    #: **不**走默认的 NULL 传播 —— ``array(1, NULL)`` 的结果是
    #: ``[1, NULL]``(一个**装着一个 NULL 的数组**),不是 NULL。
    #: 真实 CH 的 ``FunctionArray`` 同样关掉了 ``useDefaultImplementationForNulls``:
    #: 容器构造类函数要自己把每个成员(可能是 NULL)装进去。
    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return array_type(common_type(args))

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return list(values)


class FunctionRange(_ArrayFunction):
    """``range([start,] end[, step])`` —— 左闭右开。

    ``range(4) = [0,1,2,3]``;``range(1, 4) = [1,2,3]``;
    ``range(1, 10, 3) = [1,4,7]``。``step`` 为 0 会抛错(而不是死循环)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        if len(args) == 1:
            return array_type(_UINT64 if args[0].get_name().startswith("UInt") else args[0])
        return array_type(common_type(args))

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        if len(values) == 1:
            start, end, step = 0, int(values[0]), 1
        elif len(values) == 2:
            start, end, step = int(values[0]), int(values[1]), 1
        else:
            start, end, step = int(values[0]), int(values[1]), int(values[2])
        if step == 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, "range() step cannot be 0")
        out: List[int] = []
        cur = start
        if step > 0:
            while cur < end:
                out.append(cur)
                cur += step
        else:
            while cur > end:
                out.append(cur)
                cur += step
        return out


register_function("array", FunctionArray, aliases=["[]"],
                  description="由参数构造数组(元素类型取公共超类型)")
register_function("range", FunctionRange, description="构造等差整数数组(左闭右开)")


# ══════════════════════════════════════════════════════════════════════════
# 取元素
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayElement(_ArrayFunction):
    """``arrayElement(arr, i)`` —— 别名 ``[]``。**1-based**。

    .. code-block:: text

       i > 0       从头部数
       i < 0       从尾部数(-1 是最后一个)
       i == 0      返回元素默认值
       越界        返回元素默认值(**不报错**)

    "返回默认值而不是报错"是真实 ClickHouse 的行为,也是它能在向量化
    路径上一口气算完整列的原因。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        elem = args[0].get_elements_type()
        if elem is None:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"arrayElement expects an Array, got {args[0].get_name()}",
            )
        return elem

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = values[0] or []
        elem_type = self.get_return_type(types)
        idx = int(values[1])
        n = len(arr)
        if idx < 0:
            idx = n + idx + 1
        if idx < 1 or idx > n:
            return elem_type.default_value()
        return arr[idx - 1]


register_function("arrayElement", FunctionArrayElement,
                  description="取数组第 i 个元素(1-based,负数从尾部数,越界给默认值)")


# ══════════════════════════════════════════════════════════════════════════
# 包含关系
# ══════════════════════════════════════════════════════════════════════════


def _has_substr(arr: Sequence[Any], sub: Sequence[Any]) -> bool:
    if not sub:
        return True
    n, m = len(arr), len(sub)
    for i in range(n - m + 1):
        if list(arr[i : i + m]) == list(sub):
            return True
    return False


_register_array_fn("has", _UINT8, lambda v: 1 if v[1] in (v[0] or []) else 0,
                   description="数组里是否含有某个元素")
_register_array_fn("hasAll", _UINT8,
                   lambda v: 1 if all(x in (v[0] or []) for x in (v[1] or [])) else 0,
                   description="数组是否包含另一个数组的全部元素")
_register_array_fn("hasAny", _UINT8,
                   lambda v: 1 if any(x in (v[0] or []) for x in (v[1] or [])) else 0,
                   description="数组是否与另一个数组有交集")
_register_array_fn("hasSubstr", _UINT8,
                   lambda v: 1 if _has_substr(v[0] or [], v[1] or []) else 0,
                   description="数组是否把另一个数组作为**连续子序列**包含")


def _index_of(arr: Sequence[Any], x: Any) -> int:
    for i, v in enumerate(arr):
        if v == x:
            return i + 1
    return 0


_register_array_fn("indexOf", _UINT64,
                   lambda v: _index_of(v[0] or [], v[1]),
                   aliases=["arrayIndexOf"],
                   description="元素第一次出现的 1-based 下标(0 = 没有)")
_register_array_fn("countEqual", _UINT64,
                   lambda v: sum(1 for x in (v[0] or []) if x == v[1]),
                   description="等于某个值的元素个数")


# ══════════════════════════════════════════════════════════════════════════
# 增删改
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayConcat(_ArrayFunction):
    """``arrayConcat(a1, a2, ...)`` —— 顺序拼接,元素类型取公共超类型。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return array_type(common_type([t.get_elements_type() or _NOTHING for t in args]))

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        out: List[Any] = []
        for v in values:
            out.extend(v or [])
        return out


register_function("arrayConcat", FunctionArrayConcat,
                  aliases=["array_concat"],
                  description="把多个数组拼接成一个")


_register_array_fn("arrayPopBack", lambda a: a[0],
                   lambda v: (v[0] or [])[:-1], description="去掉数组最后一个元素")
_register_array_fn("arrayPopFront", lambda a: a[0],
                   lambda v: (v[0] or [])[1:], description="去掉数组第一个元素")


class FunctionArrayPush(_ArrayFunction):
    """``arrayPushBack(arr, x)`` / ``arrayPushFront(arr, x)``。"""

    front = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return array_type(common_type([args[0].get_elements_type() or _NOTHING, args[1]]))

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = list(values[0] or [])
        if self.front:
            return [values[1]] + arr
        return arr + [values[1]]


class FunctionArrayPushFront(FunctionArrayPush):
    front = True


register_function("arrayPushBack", FunctionArrayPush,
                  description="在数组末尾追加一个元素")
register_function("arrayPushFront", FunctionArrayPushFront,
                  description="在数组头部插入一个元素")


class FunctionArrayResize(_ArrayFunction):
    """``arrayResize(arr, size[, extender])`` —— 截断或补默认值。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        elem = args[0].get_elements_type() or _NOTHING
        if len(args) >= 3:
            elem = common_type([elem, args[2]])
        return array_type(elem)

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = list(values[0] or [])
        size = int(values[1])
        if size < 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"arrayResize: negative size {size}")
        if len(arr) > size:
            return arr[:size]
        if len(arr) == size:
            return arr
        fill = values[2] if len(values) >= 3 else self.get_return_type(types).get_elements_type().default_value()
        return arr + [fill] * (size - len(arr))


register_function("arrayResize", FunctionArrayResize,
                  description="把数组调整到指定长度(短了补默认值)")


class FunctionArraySlice(_ArrayFunction):
    """``arraySlice(arr, offset[, length])`` —— 规则与 ``substring`` **完全一致**。

    1-based;负 ``offset`` 从尾部数;``offset = 0`` 返回空数组。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = list(values[0] or [])
        n = len(arr)
        offset = int(values[1])
        if offset < 0:
            pos = n + offset + 1
        elif offset == 0:
            pos = 0
        else:
            pos = offset
        if pos < 1 or pos > n:
            return []
        start = pos - 1
        if len(values) >= 3:
            length = max(0, int(values[2]))
        else:
            length = n - start
        return arr[start : start + length]


register_function("arraySlice", FunctionArraySlice,
                  description="取数组的一段(1-based,负偏移从尾部数)")


_register_array_fn("arrayReverse", lambda a: a[0],
                   lambda v: list(reversed(v[0] or [])),
                   description="反转数组")
_register_array_fn("arrayDistinct", lambda a: a[0],
                   lambda v: list(dict.fromkeys(v[0] or [])),
                   description="去重(保留首次出现的顺序)")
_register_array_fn("arrayUniq", _UINT64,
                   lambda v: len({tuple(x if not isinstance(x, list) else tuple(x)
                                        for x in (v[0] or []))}),
                   description="数组中不同值的个数")
_register_array_fn("arrayEnumerate", lambda a: array_type(_UINT32),
                   lambda v: list(range(1, len(v[0] or []) + 1)),
                   description="生成 [1..n](常用于拿下标)")
_register_array_fn("arrayEnumerateDense", lambda a: array_type(_UINT32),
                   lambda v: _enumerate_dense(v[0] or []),
                   description="每个元素的**密集排名**(相同值同名次)")
_register_array_fn("arrayEnumerateUniq", lambda a: array_type(_UINT32),
                   lambda v: _enumerate_uniq(v[0] or []),
                   description="每个元素是它第几次出现(从 1 开始)")


def _enumerate_dense(arr: Sequence[Any]) -> List[int]:
    seen: Dict[Any, int] = {}
    out: List[int] = []
    for x in arr:
        key = _hashable(x)
        if key not in seen:
            seen[key] = len(seen) + 1
        out.append(seen[key])
    return out


def _enumerate_uniq(arr: Sequence[Any]) -> List[int]:
    counter: Dict[Any, int] = {}
    out: List[int] = []
    for x in arr:
        key = _hashable(x)
        counter[key] = counter.get(key, 0) + 1
        out.append(counter[key])
    return out


def _hashable(x: Any) -> Any:
    if isinstance(x, (list, dict)):
        return repr(x)
    return x


# ══════════════════════════════════════════════════════════════════════════
# 累计 / 差分 / 排序
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayCumSum(_ArrayFunction):
    """``arrayCumSum(arr)`` / ``arrayCumSumNonNegative(arr)``。

    ``NonNegative`` 版本在**前缀和变负数时把它压回 0**
    (``arrayCumSumNonNegative([1, -2, 3]) = [1, 0, 3]``)——
    这个函数存在的意义就是"算活跃度曲线时不想看到负数"。
    """

    non_negative = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        out: List[Any] = []
        acc = 0
        for x in values[0] or []:
            acc = acc + x
            if self.non_negative and acc < 0:
                acc = 0
            out.append(acc)
        return out


class FunctionArrayCumSumNonNegative(FunctionArrayCumSum):
    non_negative = True


register_function("arrayCumSum", FunctionArrayCumSum,
                  description="前缀和")
register_function("arrayCumSumNonNegative", FunctionArrayCumSumNonNegative,
                  description="前缀和(负数截断为 0)")


_register_array_fn("arrayDifference", lambda a: a[0], lambda v: _array_difference(v[0] or []),
                   description="相邻元素之差(第一项恒为 0,与真实 CH 一致)")


def _array_difference(arr: Sequence[Any]) -> List[Any]:
    out: List[Any] = [0]
    for i in range(1, len(arr)):
        out.append(arr[i] - arr[i - 1])
    return out


class FunctionArrayFill(_ArrayFunction):
    """``arrayFill(f, arr)`` —— 用前一个值填补"不满足条件"的位置。

    ``f`` 既可以是 lambda(``x -> x != 0``),也可以是一个布尔数组。
    真实 ClickHouse 只接受 lambda;布尔数组是 mini-clickhouse 为了方便
    教学例子加的口子,它在语义上等价于"lambda 就是恒等映射"。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[1]

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[1]

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        arr_col = args[1].column
        if arr_col.is_const():
            arr_col = arr_col.convert_to_full_column()
        ret = args[1].type
        out = ret.create_column()
        elem_type = ret.get_elements_type()
        assert elem_type is not None
        first = args[0]
        if isinstance(first, LambdaArgument):
            for i in range(rows):
                elements = arr_col.get(i) or []
                mask = self._mask_via_lambda(first, elements, elem_type)
                out.append(_fill(elements, mask, elem_type.default_value()))
            return out
        flag_col = args[0].column
        if flag_col.is_const():
            flag_col = flag_col.convert_to_full_column()
        for i in range(rows):
            elements = arr_col.get(i) or []
            flags = flag_col.get(i) or []
            mask = [bool(x) for x in flags]
            out.append(_fill(elements, mask, elem_type.default_value()))
        return out

    @staticmethod
    def _mask_via_lambda(lam: LambdaArgument, elements: Sequence[Any],
                         elem_type: DataType) -> List[bool]:
        if not elements:
            return []
        col = elem_type.create_column()
        for v in elements:
            col.append(v)
        result = lam.call(col)
        return [bool(x) for x in result.to_list()]


def _fill(elements: Sequence[Any], mask: Sequence[bool], default: Any) -> List[Any]:
    out: List[Any] = []
    prev = default
    for i, x in enumerate(elements):
        keep = mask[i] if i < len(mask) else True
        if keep:
            prev = x
            out.append(x)
        else:
            out.append(prev)
    return out


register_function("arrayFill", FunctionArrayFill,
                  description="用前一个满足条件的值填补当前位置")


# ══════════════════════════════════════════════════════════════════════════
# 排序 —— 纯形式;lambda 形式见 higher_order.py(共用同一个类)
# ══════════════════════════════════════════════════════════════════════════


def _sort_key(x: Any) -> Any:
    # NULL 在 ClickHouse 里排序时**最大**(见 IColumn::compare_at 的约定)
    return (1, 0) if x is None else (0, x)


class FunctionArraySort(_ArrayFunction):
    """``arraySort(arr)`` / ``arraySort(lambda, arr)``。

    两种调用形态:

    .. code-block:: sql

       SELECT arraySort([3, 1, 2]);                    -- [1, 2, 3]
       SELECT arraySort(x -> -x, [3, 1, 2]);           -- [3, 2, 1]
       SELECT arraySort((x, y) -> x + y, a, b);        -- 多数组(lambda 收两个参数的列)

    .. note::

       **lambda 形式归 higher_order.py 管**,但两个模块注册的是**同一个名字** ——
       所以这里一次把两种形态都实现了,``higher_order`` 只做再导出。
       否则"哪个模块后被 import 就谁能生效"会变成一个随机行为。
    """

    reverse = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return self._array_type(args)

    @staticmethod
    def _array_type(args: Sequence[Any]) -> DataType:
        # ``args`` 里可能混着一个 LambdaArgument(没有 .is_array()),跳过它
        for t in reversed(args):
            if isinstance(t, DataType) and t.is_array():
                return t
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            "arraySort expects at least one Array argument",
        )

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        first = args[0]
        array_args = args[1:] if isinstance(first, LambdaArgument) else args
        ret = self._array_type([a.type for a in array_args])
        out = ret.create_column()
        elem_type = ret.get_elements_type()
        assert elem_type is not None
        if isinstance(first, LambdaArgument):
            array_cols = [
                (a.column.convert_to_full_column() if a.column.is_const() else a.column)
                for a in args[1:]
            ]
            array_types = [a.type for a in args[1:]]
            for i in range(rows):
                groups = [c.get(i) or [] for c in array_cols]
                keys = self._lambda_keys(first, groups, array_types)
                n = len(groups[0]) if groups else 0
                order = sorted(range(n), key=lambda j: _sort_key(keys[j]),
                               reverse=self.reverse)
                out.append([groups[0][j] for j in order])
            return out
        col = args[0].column
        if col.is_const():
            col = col.convert_to_full_column()
        for i in range(rows):
            arr = list(col.get(i) or [])
            out.append(sorted(arr, key=_sort_key, reverse=self.reverse))
        return out

    @staticmethod
    def _lambda_keys(lam: LambdaArgument, groups: List[List[Any]],
                     array_types: Sequence[DataType]) -> List[Any]:
        cols = []
        for g, t in zip(groups, array_types):
            elem = t.get_elements_type()
            assert elem is not None
            c = elem.create_column()
            for v in g:
                c.append(v)
            cols.append(c)
        result = lam.call(*cols)
        return result.to_list()


class FunctionArrayReverseSort(FunctionArraySort):
    reverse = True


register_function("arraySort", FunctionArraySort,
                  aliases=["array_sort"],
                  description="数组排序(可传 lambda 作为排序键)")
register_function("arrayReverseSort", FunctionArrayReverseSort,
                  description="数组逆序排序(可传 lambda)")


# ══════════════════════════════════════════════════════════════════════════
# 聚合类
# ══════════════════════════════════════════════════════════════════════════


class FunctionArraySum(_ArrayFunction):
    """``arraySum`` / ``arrayProduct`` / ``arrayMin`` / ``arrayMax``。

    .. warning::

       与真实 ClickHouse 一致,返回类型就是**元素类型** —— 所以
       ``arraySum([toUInt8(200), toUInt8(200)])`` 会**回绕**成 144,
       而不是 400。要在数组上做不会溢出的求和,先用 ``arrayMap``
       把元素提升成 ``UInt64``。
    """

    op = "sum"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0].get_elements_type() or _NOTHING

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = values[0] or []
        if not arr:
            return 0
        if self.op == "sum":
            return sum(arr)
        if self.op == "product":
            out: Any = 1
            for x in arr:
                out = out * x
            return out
        if self.op == "min":
            return min(arr)
        return max(arr)


class FunctionArrayProduct(FunctionArraySum):
    op = "product"


class FunctionArrayMin(FunctionArraySum):
    op = "min"


class FunctionArrayMax(FunctionArraySum):
    op = "max"


register_function("arraySum", FunctionArraySum, description="数组元素求和(元素类型,可能回绕)")
register_function("arrayProduct", FunctionArrayProduct, description="数组元素求积")
register_function("arrayMin", FunctionArrayMin, description="数组最小值(空数组为 0)")
register_function("arrayMax", FunctionArrayMax, description="数组最大值(空数组为 0)")


class FunctionArrayAvg(_ArrayFunction):
    """``arrayAvg(arr)`` —— 返回 ``Float64``;空数组返回 0。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _FLOAT64

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = values[0] or []
        return (sum(arr) / len(arr)) if arr else 0.0


register_function("arrayAvg", FunctionArrayAvg, description="数组平均值")


class FunctionArrayAUC(_ArrayFunction):
    """``arrayAUC(scores, labels)`` —— ROC 曲线下面积。

    用 **Mann–Whitney U 统计量**计算(带并列名次的平均处理),
    与真实 ClickHouse 的 ``arrayAUC`` 结果一致。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _FLOAT64

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        scores = list(values[0] or [])
        labels = list(values[1] or [])
        if len(scores) != len(labels):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                "arrayAUC: scores and labels must have the same length",
            )
        pos = [s for s, l in zip(scores, labels) if l]
        neg = [s for s, l in zip(scores, labels) if not l]
        if not pos or not neg:
            return 0.0
        # 秩和法:并列取平均秩
        order = sorted(range(len(scores)), key=lambda i: scores[i])
        ranks = [0.0] * len(scores)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        rank_sum = sum(ranks[i] for i, l in enumerate(labels) if l)
        n_pos, n_neg = len(pos), len(neg)
        return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


register_function("arrayAUC", FunctionArrayAUC,
                  aliases=["array_auc"],
                  description="ROC 曲线下面积(秩和法,支持并列)")


# ══════════════════════════════════════════════════════════════════════════
# 集合运算
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayIntersect(_ArrayFunction):
    """``arrayIntersect(a1, a2, ...)`` —— 所有数组都有的**不同**元素。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return array_type(common_type([t.get_elements_type() or _NOTHING for t in args]))

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arrays = [list(v or []) for v in values]
        if not arrays:
            return []
        out: List[Any] = []
        seen = set()
        for x in arrays[0]:
            key = _hashable(x)
            if key in seen:
                continue
            seen.add(key)
            if all(any(_hashable(y) == key for y in a) for a in arrays[1:]):
                out.append(x)
        return out


class FunctionArraysOverlap(_ArrayFunction):
    """``arraysOverlap(a1, a2, ...)`` —— 是否至少有两个数组共享一个元素。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arrays = [list(v or []) for v in values]
        for i in range(len(arrays)):
            for j in range(i + 1, len(arrays)):
                if any(x in arrays[j] for x in arrays[i]):
                    return 1
        return 0


class FunctionArrayJaccardIndex(_ArrayFunction):
    """``arrayJaccardIndex(a1, a2)`` —— ``|A ∩ B| / |A ∪ B|``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _FLOAT64

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        a = set(_hashable(x) for x in (values[0] or []))
        b = set(_hashable(x) for x in (values[1] or []))
        union = len(a | b)
        if union == 0:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "arrayJaccardIndex: both arrays are empty",
            )
        return len(a & b) / union


class FunctionArraySymmetricDifference(_ArrayFunction):
    """``arraySymmetricDifference(a1, a2, ...)`` —— 出现**奇数次**的不同元素。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return array_type(common_type([t.get_elements_type() or _NOTHING for t in args]))

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        counts: Dict[Any, int] = {}
        order: List[Any] = []
        for v in values:
            for x in set(_hashable(y) for y in (v or [])):
                if x not in counts:
                    counts[x] = 0
                    order.append(x)
                counts[x] += 1
        # 重新映射回原值以保留类型
        lookup = {}
        for v in values:
            for y in (v or []):
                lookup.setdefault(_hashable(y), y)
        return [lookup[k] for k in order if counts[k] % 2 == 1]


register_function("arrayIntersect", FunctionArrayIntersect,
                  aliases=["array_intersect"],
                  description="多个数组的交集(不同元素)")
register_function("arraysOverlap", FunctionArraysOverlap,
                  aliases=["arrays_overlap"],
                  description="多个数组之间是否有公共元素")
register_function("arrayJaccardIndex", FunctionArrayJaccardIndex,
                  description="两个数组的 Jaccard 相似度")
register_function("arraySymmetricDifference", FunctionArraySymmetricDifference,
                  aliases=["array_symmetric_difference"],
                  description="多个数组的对称差(出现奇数次的元素)")


# ══════════════════════════════════════════════════════════════════════════
# 形状变换
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayZip(_ArrayFunction):
    """``arrayZip(a1, a2, ...)`` —— 逐位打包成 ``Array(Tuple(...))``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        parts = ", ".join((t.get_elements_type() or _NOTHING).get_name() for t in args)
        return get_data_type(f"Array(Tuple({parts}))")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arrays = [list(v or []) for v in values]
        n = min(len(a) for a in arrays) if arrays else 0
        return [tuple(a[i] for a in arrays) for i in range(n)]


class FunctionArrayFlatten(_ArrayFunction):
    """``arrayFlatten(arr)`` —— 把嵌套数组**递归**压平(别名 ``flatten``)。

    .. warning::

       真实 ClickHouse 的 ``arrayFlatten`` 只压**一层**
       (``Array(Array(T))`` → ``Array(T)``),多层嵌套需要套多次。
       mini-clickhouse 递归压到底(``Array(Array(Array(T)))`` 一次到底),
       这个差异会在"嵌套深度不确定"的 schema 上体现出来。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        elem = args[0].get_elements_type() or _NOTHING
        while elem.is_array():
            elem = elem.get_elements_type()  # type: ignore[assignment]
        return array_type(elem)

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        out: List[Any] = []
        _flatten_into(values[0] or [], out)
        return out


def _flatten_into(value: Any, out: List[Any]) -> None:
    if isinstance(value, (list, tuple)):
        for v in value:
            _flatten_into(v, out)
    else:
        out.append(value)


class FunctionArrayCompact(_ArrayFunction):
    """``arrayCompact(arr)`` —— 去掉**相邻**的重复元素。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        out: List[Any] = []
        for x in values[0] or []:
            if not out or out[-1] != x:
                out.append(x)
        return out


class FunctionArrayRotate(_ArrayFunction):
    """``arrayRotateLeft`` / ``arrayRotateRight`` —— 循环移位。"""

    to_left = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = list(values[0] or [])
        if not arr:
            return []
        n = int(values[1]) % len(arr)
        if not self.to_left:
            n = (-n) % len(arr)
        return arr[n:] + arr[:n]


class FunctionArrayRotateRight(FunctionArrayRotate):
    to_left = False


class FunctionArrayShift(_ArrayFunction):
    """``arrayShiftLeft`` / ``arrayShiftRight`` —— 非循环移位,空出来的位置补默认值。"""

    to_left = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = list(values[0] or [])
        n = int(values[1]) if len(values) >= 2 else 1
        elem = self.get_return_type(types).get_elements_type()
        default = elem.default_value() if elem is not None else 0
        n = min(max(n, 0), len(arr))
        if n == 0:
            return arr
        if self.to_left:
            return arr[n:] + [default] * n
        return [default] * n + arr[: len(arr) - n]


class FunctionArrayShiftRight(FunctionArrayShift):
    to_left = False


class FunctionArrayWithConstant(_ArrayFunction):
    """``arrayWithConstant(n, x)`` —— ``n`` 个 ``x`` 组成的数组。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return array_type(args[1])

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        n = int(values[0])
        if n < 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"arrayWithConstant: negative n {n}")
        return [values[1]] * n


class FunctionEmptyArrayToSingle(_ArrayFunction):
    """``emptyArrayToSingle(arr)`` —— 空数组变成"一个默认元素"。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        arr = list(values[0] or [])
        if arr:
            return arr
        elem = self.get_return_type(types).get_elements_type()
        return [elem.default_value()] if elem is not None else []


register_function("arrayZip", FunctionArrayZip, aliases=["array_zip"],
                  description="把多个数组逐位打包成元组数组")
register_function("arrayFlatten", FunctionArrayFlatten, aliases=["flatten"],
                  description="递归压平嵌套数组")
register_function("arrayCompact", FunctionArrayCompact,
                  description="去掉相邻的重复元素")
register_function("arrayRotateLeft", FunctionArrayRotate,
                  aliases=["array_rotate_left"],
                  description="向左循环移位")
register_function("arrayRotateRight", FunctionArrayRotateRight,
                  aliases=["array_rotate_right"],
                  description="向右循环移位")
register_function("arrayShiftLeft", FunctionArrayShift,
                  description="向左移位,右侧补默认值")
register_function("arrayShiftRight", FunctionArrayShiftRight,
                  description="向右移位,左侧补默认值")
register_function("arrayWithConstant", FunctionArrayWithConstant,
                  description="生成 n 个相同元素组成的数组")
register_function("emptyArrayToSingle", FunctionEmptyArrayToSingle,
                  description="空数组变成含一个默认元素的数组")


# ══════════════════════════════════════════════════════════════════════════
# arrayReduce / arrayReduceInRanges
# ══════════════════════════════════════════════════════════════════════════

#: 支持的内建聚合名 → (实现, 结果类型名 | None 表示"取元素类型")
_REDUCERS: Dict[str, Any] = {
    "sum": (lambda vs: sum(vs), None),
    "min": (lambda vs: min(vs) if vs else None, None),
    "max": (lambda vs: max(vs) if vs else None, None),
    "product": (lambda vs: math.prod(vs) if vs else 1, None),
    "avg": (lambda vs: (sum(vs) / len(vs)) if vs else None, "Float64"),
    "count": (lambda vs: len(vs), "UInt64"),
    "uniq": (lambda vs: len(set(_hashable(x) for x in vs)), "UInt64"),
    "uniqExact": (lambda vs: len(set(_hashable(x) for x in vs)), "UInt64"),
    "any": (lambda vs: vs[0] if vs else None, None),
    "anyLast": (lambda vs: vs[-1] if vs else None, None),
    "all": (lambda vs: all(vs), "UInt8"),
    "groupArray": (list, None),
    "groupUniqArray": (lambda vs: list(dict.fromkeys(vs)), None),
    "median": (lambda vs: _median(vs), "Float64"),
    "sumForEach": (lambda vs: list(vs), None),
}


def _median(values: Sequence[Any]) -> Any:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


class FunctionArrayReduce(_ArrayFunction):
    """``arrayReduce(agg_name, arr, ...)`` —— 把数组当成"一组参数"喂给聚合函数。

    .. warning::

       mini-clickhouse **没有聚合函数注册表**(那属于聚合引擎那一章),
       所以这里内建了 ``sum/min/max/avg/count/uniq/any/all/groupArray/
       groupUniqArray/median`` 这些;别的名字会抛 ``NOT_IMPLEMENTED``,
       **不会**静默返回一个错值。另外,``agg_name`` 是**实参的值**,
       而返回类型推导只能看到类型 —— 所以声明的返回类型一律取
       第一个数组的元素类型(``avg``/``count`` 这类会不准,取不到
       就退化成元素类型)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[1].get_elements_type() or _NOTHING

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        name = str(values[0])
        fn = _REDUCERS.get(name)
        if fn is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"arrayReduce: aggregate '{name}' is not available in mini-clickhouse; "
                f"supported: {', '.join(sorted(_REDUCERS))}",
            )
        arrays = [list(v or []) for v in values[1:]]
        arg = arrays[0] if len(arrays) == 1 else list(zip(*arrays))
        return fn[0](arg)


class FunctionArrayReduceInRanges(_ArrayFunction):
    """``arrayReduceInRanges(agg_name, ranges, arr, ...)``。

    ``ranges`` 是 ``[(起始下标1based, 长度), ...]``,返回一个数组,
    每个元素是那一小段上的聚合结果。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        inner = args[2].get_elements_type() or _NOTHING
        return array_type(inner)

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        name = str(values[0])
        fn = _REDUCERS.get(name)
        if fn is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"arrayReduceInRanges: aggregate '{name}' is not available in "
                f"mini-clickhouse; supported: {', '.join(sorted(_REDUCERS))}",
            )
        ranges = values[1] or []
        arrays = [list(v or []) for v in values[2:]]
        out: List[Any] = []
        for rng in ranges:
            start = int(rng[0]) - 1
            length = int(rng[1])
            if len(arrays) == 1:
                arg = arrays[0][start : start + length]
            else:
                arg = list(zip(*[a[start : start + length] for a in arrays]))
            out.append(fn[0](arg))
        return out


register_function("arrayReduce", FunctionArrayReduce,
                  aliases=["array_reduce"],
                  description="把数组喂给聚合函数(名字由第一个参数给出)")
register_function("arrayReduceInRanges", FunctionArrayReduceInRanges,
                  aliases=["array_reduce_in_ranges"],
                  description="对数组的若干区间分别聚合")


# ══════════════════════════════════════════════════════════════════════════
# arrayJoin —— 注册但执行时报错
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayJoin(FunctionBase):
    """``arrayJoin(arr)`` —— **它不是普通函数,它改变行数**。

    真实 ClickHouse 在解释器里对 ``arrayJoin`` 做了特殊处理:
    遇到它就把当前块"炸开"(一行数组变成多行),这个动作把 N 行变成
    ``sum(len(arr))`` 行 —— **行数变了**,所以它不可能是一个"逐列求值"
    的标量函数。解释器会把它重写成 ``ArrayJoinTransform``。

    mini-clickhouse 里对应的是"查询引擎的改写阶段"。这里把它注册出来,
    是为了让 ``system.functions`` 里能看到它,并在有人不小心直接
    ``execute`` 它时给出**准确的**错误信息。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        elem = args[0].get_elements_type() or _NOTHING
        return elem

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            "arrayJoin cannot be executed as a scalar function: it changes the "
            "number of rows in the block. The query engine rewrites it into an "
            "ArrayJoinTransform before execution (real ClickHouse does the same "
            "in the interpreter).",
        )


register_function("arrayJoin", FunctionArrayJoin,
                  aliases=["array_join"],
                  description="把数组展开成多行(由查询引擎改写,不是普通函数)")
