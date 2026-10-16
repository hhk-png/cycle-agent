"""Tuple 与 Map 函数 —— 两个"容器"类型,但它们的行语义**正好相反**。

.. code-block:: text

   Tuple(UInt8, String) 的一行 = 横着的一条:  (1, 'a')
   ┌─────┬────────┐
   │  1  │  'a'   │        ← 一行里**并列**着若干个值
   └─────┴────────┘

   Map(String, UInt8) 的一行 = 竖着的一小段:  {'x': 1, 'y': 2}
   ┌──────┬──────┬──────┬──────┐
   │ 'x'  │  1   │ 'y'  │  2   │   ← 行里是若干 (k, v) 对
   └──────┴──────┴──────┴──────┘

这个区别解释了两件事:

1. ``Tuple`` 的成员数在**类型**里就定死了(``Tuple(UInt8, String)`` 永远 2 个),
   所以 ``tupleElement(t, 1)`` 的下标越界时能直接返回**该成员的默认值**;
2. ``Map`` 的键值对个数**逐行不同**,所以它才需要 ``offsets`` 那样的
   变长布局 —— 和 ``Array`` 完全一样(``Map(K, V)`` 在存储上就是
   ``Array(Tuple(K, V))``)。

.. note::

   ``t.1`` / ``t.name`` 这种"点号取成员"的写法是**解析器**层面的语法糖,
   不是函数名。mini-clickhouse 在函数表里只注册 ``tupleElement``;
   点号写法应当在 SQL 解析器里被改写成 ``tupleElement(t, ...)``。

.. note::

   ``untuple`` 与 ``arrayJoin`` 一样,**会改变块的列数**,所以它不可能是
   普通标量函数(引擎在解释阶段把它展开)。这里注册它并在执行时抛出
   精确的错误,而不是假装能算 —— 见 :class:`FunctionUntuple`。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..columns import ColumnTuple
from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from .array import common_type
from .base import FunctionBase, LambdaArgument, register_function
from .string import like_to_regex

__all__ = ["tuple_type_of", "map_pairs_of"]

_UINT8 = get_data_type("UInt8")
_STRING = get_data_type("String")


# ══════════════════════════════════════════════════════════════════════════
# 底座
# ══════════════════════════════════════════════════════════════════════════


class _TypedRowWise(FunctionBase):
    """带类型的逐行底座(取默认值、推元素类型时都要看类型)。"""

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


def tuple_type_of(types: Sequence[DataType]) -> DataType:
    parts = ", ".join(t.get_name() for t in types)
    return get_data_type(f"Tuple({parts})")


def map_pairs_of(t: DataType) -> Tuple[DataType, DataType]:
    """取 ``Map(K, V)`` 的键/值类型。"""
    if not t.is_map():
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Expected a Map type, got {t.get_name()}",
        )
    return t.key, t.value  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════════
# tuple / tupleElement / untuple
# ══════════════════════════════════════════════════════════════════════════


class FunctionTuple(_TypedRowWise):
    """``tuple(a, b, ...)`` —— 构造元组。

    成员类型就是各实参的类型 —— 元组**不做类型的公共提升**:
    ``tuple(1, 2.5)`` 的类型是 ``Tuple(UInt8, Float64)``,而不是
    ``Tuple(Float64, Float64)``。这一点和 ``array(1, 2.5)`` 相反,
    因为元组的每个位置本来就有各自的类型。
    """

    #: **不**走默认的 NULL 传播 —— ``tuple(NULL, 1)`` 是 ``(NULL, 1)``,
    #: 不是 NULL。真实 CH 的 ``FunctionTuple`` 同样关掉了
    #: ``useDefaultImplementationForNulls``:容器构造类函数要自己把每个
    #: 成员(可能是 NULL)装进去。
    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return tuple_type_of(args)

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return tuple(values)


class FunctionTupleElement(_TypedRowWise):
    """``tupleElement(t, i)`` —— 取元组第 i 个成员。

    **1-based**;负数从尾部数;越界返回**该成员类型的默认值**。
    第二个参数也可以是字符串 —— 那样就是按**具名元组**的字段名取。

    .. code-block:: sql

       SELECT tupleElement((1, 'a'), 1);      -- 1
       SELECT tupleElement((1, 'a'), -1);     -- 'a'
       SELECT tupleElement((1, 'a'), 3);      -- 0(越界,给默认值)
       SELECT tuple(1, 2).1;                  -- 解析器改写成的同一件事
    """

    def _resolve(self, t: DataType, selector: Any) -> Tuple[Optional[int], Optional[DataType]]:
        """把"下标 / 字段名"解析成 **(0-based 位置, 成员类型)**。

        .. warning::

           这里**必须**同时返回位置和类型,不能只返回类型 —— 因为
           ``Tuple(UInt8, UInt8)`` 这种"两个成员同类型"的元组,
           按类型去反查位置会永远拿到第 0 个。
        """
        elements = getattr(t, "elements", None)
        if elements is None:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"tupleElement expects a Tuple, got {t.get_name()}",
            )
        if isinstance(selector, str):
            idx = t.get_subcolumn_index(selector)
            return (idx, elements[idx]) if idx >= 0 else (None, None)
        i = int(selector)
        n = len(elements)
        if i < 0:
            i = n + i + 1
        if i < 1 or i > n:
            return None, None
        return i - 1, elements[i - 1]

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        elements = getattr(args[0], "elements", None)
        if elements is None:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"tupleElement expects a Tuple, got {args[0].get_name()}",
            )
        return elements[0]

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        tup_type = args[0].type
        col = args[0].column
        if col.is_const():
            col = col.convert_to_full_column()
        sel_col = args[1].column
        if sel_col.is_const():
            sel_col = sel_col.convert_to_full_column()
        elements = tup_type.elements  # type: ignore[attr-defined]
        resolved = [self._resolve(tup_type, sel_col.get(i)) for i in range(rows)]
        # 结果列的类型按"本批实际取到的那些成员"的公共超类型来定。
        # .. warning:: 真实 ClickHouse 会把常量下标代入 `get_return_type`
        #    得到**精确**的类型(比如 `t.1` 就是 UInt8);mini-clickhouse
        #    看不到那个值,所以这里只能用"实际取到的成员类型"近似。
        used = [m for _, m in resolved if m is not None]
        out_type = common_type(used) if used else elements[0]
        out = out_type.create_column()
        for i in range(rows):
            pair = col.get(i)
            idx, member = resolved[i]
            if member is None:
                out.append(out_type.normalize(elements[0].default_value()))
            else:
                value = pair[idx] if idx < len(pair) else member.default_value()
                out.append(out_type.normalize(value))
        return out


class FunctionUntuple(FunctionBase):
    """``untuple(t)`` —— **未实现**(它改变的是"列数",不是"行数")。

    ``SELECT untuple(tuple(1, 2, 3))`` 在真实 ClickHouse 里会产出**三列**,
    而不是三行。一个返回 ``IColumn`` 的标量函数签名**表达不了**这件事,
    所以它必须在解释阶段被改写成"把一列换成三列"的变换。

    mini-clickhouse 注册它、并在这里抛出精确的错误,而不是返回一个
    "看起来像元组"的值 —— 后者会让调用方以为拿到了三列,实际只拿到一列。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        elements = getattr(args[0], "elements", None)
        return elements[0] if elements else _STRING

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            "untuple changes the number of COLUMNS in the block, so it cannot be a "
            "scalar function; the query engine must rewrite it during analysis "
            "(real ClickHouse handles untuple in the interpreter as well)",
        )


class FunctionTupleToNameValuePairs(_TypedRowWise):
    """``tupleToNameValuePairs(t)`` —— 具名元组 → ``Array(Tuple(String, T))``。

    .. code-block:: sql

       SELECT tupleToNameValuePairs(tuple(1 AS a, 'x' AS b));
       -- [('a', 1), ('b', 'x')]

    匿名元组没有名字,会得到**空数组**(而不是报错)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        inner = common_type(list(getattr(args[0], "elements", []))) if \
            getattr(args[0], "elements", None) else _STRING
        return get_data_type(f"Array(Tuple(String, {inner.get_name()}))")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        names = types[0].get_subcolumn_names()
        if not names:
            return []
        # 值那一侧只能是**一个**类型,所以混合类型的具名元组会被
        # 挤压成公共超类型(通常就是 String)。真实 ClickHouse 在
        # 这种情况下同样要挑一个超类型,只是它的超类型规则更细致。
        inner = self.get_return_type(types).get_elements_type().elements[1]
        return [(n, inner.normalize(v)) for n, v in zip(names, values[0])]


class FunctionTupleConcat(_TypedRowWise):
    """``tupleConcat(t1, t2, ...)`` —— 把多个元组拼成一个更长的元组。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        parts: List[DataType] = []
        for t in args:
            parts.extend(getattr(t, "elements", []))
        if not parts:
            raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, "tupleConcat expects tuples")
        return tuple_type_of(parts)

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        out: List[Any] = []
        for v in values:
            out.extend(v)
        return tuple(out)


register_function("tuple", FunctionTuple, aliases=["Tuple"],
                  description="构造元组(各位置保留自己的类型)")
register_function("tupleElement", FunctionTupleElement,
                  aliases=["tuple_element"],
                  description="取元组第 i 个成员(1-based,越界给默认值)")
register_function("untuple", FunctionUntuple,
                  description="展开元组为多列(由查询引擎改写,不是普通函数)")
register_function("tupleToNameValuePairs", FunctionTupleToNameValuePairs,
                  description="具名元组转成 (名字, 值) 数组")
register_function("tupleConcat", FunctionTupleConcat,
                  description="把多个元组首尾相接")


# ══════════════════════════════════════════════════════════════════════════
# map 系列
# ══════════════════════════════════════════════════════════════════════════


class FunctionMap(FunctionBase):
    """``map(k1, v1, k2, v2, ...)`` —— 构造 ``Map(K, V)``。

    参数必须是**偶数**个。键类型取所有键的公共超类型,值类型同理。

    **不**走默认的 NULL 传播:``map('a', NULL)`` 是 ``{'a': NULL}``,
    不是 NULL(与 ``tuple``/``array`` 一样,容器构造类函数自己装成员)。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        if len(args) % 2 != 0:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"map() requires an even number of arguments (key, value pairs), "
                f"got {len(args)}",
            )
        keys = common_type(args[0::2])
        values = common_type(args[1::2])
        return get_data_type(f"Map({keys.get_name()}, {values.get_name()})")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        if len(values) % 2 != 0:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "map() requires an even number of arguments",
            )
        out: Dict[Any, Any] = {}
        for i in range(0, len(values), 2):
            out[values[i]] = values[i + 1]
        return out


class FunctionMapKeys(_TypedRowWise):
    """``mapKeys(m)`` —— 所有键组成的数组。

    因为 ``Map`` 在存储上就是 ``Array(Tuple(K, V))``,``mapKeys(m)``
    和 ``arrayMap(x -> x.1, m)`` 是**同一件事的两种写法** ——
    这是理解 ClickHouse 容器类型设计的一个好例子。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        key, _ = map_pairs_of(args[0])
        return get_data_type(f"Array({key.get_name()})")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return list((values[0] or {}).keys())


class FunctionMapValues(_TypedRowWise):
    """``mapValues(m)`` —— 所有值组成的数组(顺序与 ``mapKeys`` 对齐)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        _, value = map_pairs_of(args[0])
        return get_data_type(f"Array({value.get_name()})")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return list((values[0] or {}).values())


class FunctionMapContains(_TypedRowWise):
    """``mapContains(m, key)`` —— 别名 ``mapContainsKey``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return 1 if values[1] in (values[0] or {}) else 0


class FunctionMapContainsKeyLike(_TypedRowWise):
    """``mapContainsKeyLike(m, pattern)`` —— 是否有键匹配 LIKE 模式。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        rx = like_to_regex(str(values[1]))
        return 1 if any(rx.match(str(k)) for k in (values[0] or {})) else 0


class FunctionMapExtractKeyLike(_TypedRowWise):
    """``mapExtractKeyLike(m, pattern)`` —— 抽出键匹配 LIKE 模式的那些对。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[0]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        rx = like_to_regex(str(values[1]))
        return {
            k: v for k, v in (values[0] or {}).items() if rx.match(str(k))
        }


class FunctionMapFromArrays(_TypedRowWise):
    """``mapFromArrays(keys, values)`` —— 由两个数组构造 Map。

    两个数组长度必须一致 —— 否则会静默丢数据(真实 ClickHouse 抛错,
    我们同样抛错)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        key = args[0].get_elements_type() or _STRING
        value = args[1].get_elements_type() or _STRING
        return get_data_type(f"Map({key.get_name()}, {value.get_name()})")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        keys = list(values[0] or [])
        vals = list(values[1] or [])
        if len(keys) != len(vals):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"mapFromArrays: keys has {len(keys)} elements but values has {len(vals)}",
            )
        return dict(zip(keys, vals))


def _map_pair_columns(pairs: Dict[Any, Any], key_type: DataType,
                      value_type: DataType) -> Tuple[Any, Any]:
    """把一行的 dict 拆成"键列 + 值列",好喂给 lambda。"""
    kcol = key_type.create_column()
    vcol = value_type.create_column()
    for k, v in pairs.items():
        kcol.append(k)
        vcol.append(v)
    return kcol, vcol


class FunctionMapApply(_TypedRowWise):
    """``mapApply(func, map)`` —— 对每个 (k, v) 调用 lambda,要求返回 ``(k', v')``。

    .. code-block:: sql

       SELECT mapApply((k, v) -> (k, v * 2), map('a', 1, 'b', 2));
       -- {'a': 2, 'b': 4}

    **lambda 在前,map 在后** —— 与 ``arrayMap`` 的位置习惯一致。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        types = [t for t in args if isinstance(t, DataType)]
        return types[0]

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        lam = args[0]
        if not isinstance(lam, LambdaArgument):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "mapApply's first argument must be a lambda",
            )
        map_col = args[1].column
        if map_col.is_const():
            map_col = map_col.convert_to_full_column()
        map_type = args[1].type
        key_type, value_type = map_pairs_of(map_type)
        out = self.get_return_type([a.type for a in args[1:]]).create_column()
        for i in range(rows):
            pairs = map_col.get(i) or {}
            kcol, vcol = _map_pair_columns(pairs, key_type, value_type)
            result = lam.call(kcol, vcol)
            out.append(_tuple_column_to_dict(result))
        return out


class FunctionMapFilter(_TypedRowWise):
    """``mapFilter(func, map)`` —— 保留 lambda 返回真的那些对。

    .. code-block:: sql

       SELECT mapFilter((k, v) -> v > 1, map('a', 1, 'b', 2));
       -- {'b': 2}
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        types = [t for t in args if isinstance(t, DataType)]
        return types[0]

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        lam = args[0]
        if not isinstance(lam, LambdaArgument):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "mapFilter's first argument must be a lambda",
            )
        map_col = args[1].column
        if map_col.is_const():
            map_col = map_col.convert_to_full_column()
        map_type = args[1].type
        key_type, value_type = map_pairs_of(map_type)
        out = self.get_return_type([a.type for a in args[1:]]).create_column()
        for i in range(rows):
            pairs = map_col.get(i) or {}
            kcol, vcol = _map_pair_columns(pairs, key_type, value_type)
            flags = [bool(x) for x in lam.call(kcol, vcol).to_list()]
            kept = {
                k: v for (k, v), keep in zip(pairs.items(), flags) if keep
            }
            out.append(kept)
        return out


def _tuple_column_to_dict(column: Any) -> Dict[Any, Any]:
    """把 lambda 返回的 ``ColumnTuple`` 还原成一个 dict。"""
    if isinstance(column, ColumnTuple) and len(column.columns) == 2:
        keys = list(column.columns[0].to_list())
        vals = list(column.columns[1].to_list())
        return dict(zip(keys, vals))
    raise_error(
        ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
        "mapApply's lambda must return a 2-element tuple (key, value)",
    )


register_function("map", FunctionMap, aliases=["Map"],
                  description="由键值对构造 Map(参数个数必须为偶数)")
register_function("mapKeys", FunctionMapKeys, aliases=["map_keys"],
                  description="Map 的所有键组成的数组")
register_function("mapValues", FunctionMapValues, aliases=["map_values"],
                  description="Map 的所有值组成的数组")
register_function("mapContains", FunctionMapContains,
                  aliases=["mapContainsKey"],
                  description="Map 是否含有某个键")
register_function("mapContainsKeyLike", FunctionMapContainsKeyLike,
                  description="是否有键匹配 LIKE 模式")
register_function("mapExtractKeyLike", FunctionMapExtractKeyLike,
                  description="抽出键匹配 LIKE 模式的键值对")
register_function("mapFromArrays", FunctionMapFromArrays,
                  description="由键数组与值数组构造 Map")
register_function("mapApply", FunctionMapApply, aliases=["map_apply"],
                  description="对 Map 的每一对 (k, v) 调用 lambda 并重构 Map")
register_function("mapFilter", FunctionMapFilter, aliases=["map_filter"],
                  description="按 lambda 过滤 Map 的键值对")
