"""聚合组合器 —— 把一个聚合**套壳**变成一族聚合。

真实 ClickHouse 的聚合函数有几百个名字,但真正"独立实现"的可能只有
五六十个 —— 其余都是**组合器**生成的。理解组合器,你就理解了
``system.functions`` 里那一大片的来源:

.. code-block:: text

    基函数 sum
      ├── sumIf(x, cond)        = sum  + -If
      ├── sumArray(arr)         = sum  + -Array
      ├── sumState(x)           = sum  + -State     (返回"状态")
      ├── sumMerge(state)       = sum  + -Merge     (吃"状态")
      ├── sumDistinct(x)        = sum  + -Distinct
      ├── sumOrNull(x)          = sum  + -OrNull
      ├── sumOrDefault(x)       = sum  + -OrDefault
      ├── sumForEach(arr)       = sum  + -ForEach
      ├── sumResample(...)      = sum  + -Resample
      └── sumIfMerge(state)     = sum  + -If + -Merge   ← 可以叠加

每个组合器只做两件事之一:

* **改写参数类型**(``-If`` 追加一个 ``UInt8`` 条件列;``-Array`` 把
  ``Array(T)`` 变成 ``T``;``-ForEach`` 把每个参数变成元素类型);
* **改写结果类型**(``-State`` 变成 ``AggregateFunction(...)``;
  ``-OrNull`` 变成 ``Nullable(...)``;``-ForEach`` 变成 ``Array(...)``)。

.. warning::

   **这是教学实现,有几处刻意的简化**,每一处都在对应类的文档里标了
   ``.. warning::``。最要紧的两条:

   1. ``-If`` 允许**省略条件列**(条件缺省为真)。真实 ClickHouse 要求
      条件列必须存在,这里放宽是为了让"只给一个参数"的冒烟测试能跑通;
   2. ``-Resample`` 只记住 ``(start, step)`` 参数而**不做真正的重采样**,
      结果与基函数相同。真实的 ``-Resample`` 会把时间轴切成桶分别聚合。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from ..errors import ErrorCode, raise_error
from ..types import DataType
from ..types.composite import DataTypeArray
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    ValueColumn,
    decode_key_value,
    is_condition_type,
    key_bytes,
    make_nullable,
    register_combinator,
    truthy,
)

__all__ = [
    "IfCombinator",
    "ArrayCombinator",
    "MergeCombinator",
    "StateCombinator",
    "DistinctCombinator",
    "OrNullCombinator",
    "OrDefaultCombinator",
    "ResampleCombinator",
    "ForEachCombinator",
]


# ══════════════════════════════════════════════════════════════════════════
# -If —— 追加一个条件列
# ══════════════════════════════════════════════════════════════════════════


class IfCombinator(IAggregateFunction):
    """``-If`` —— "只在条件成立的行上聚合"。

    .. code-block:: sql

       SELECT sumIf(x, x > 0) FROM t;   -- 等价于 sum(x) WHERE x > 0

    .. warning::

       mini-clickhouse 允许**没有条件列**(``has_condition=False``)——
       此时它退化成基函数。真实的 ``sumIf`` 必须有第二个参数。
       这样放宽是为了让"``get_aggregate_function('sumIf', [], [UInt64])``"
       这种只给一个类型的简化调用也能工作。
    """

    def __init__(
        self,
        name: str,
        inner: IAggregateFunction,
        condition_type: Optional[DataType],
    ) -> None:
        args = list(inner.arg_types)
        if condition_type is not None:
            args = args + [condition_type]
        super().__init__(name, args, inner.params)
        self.inner = inner
        self.has_condition = condition_type is not None

    def get_return_type(self) -> DataType:
        return self.inner.get_return_type()

    def create_state(self) -> Any:
        return self.inner.create_state()

    def clone_state(self, state: Any) -> Any:
        return self.inner.clone_state(state)

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        if self.has_condition:
            if not truthy(columns[len(columns) - 1].get(row)):
                return
        self.inner.add(state, columns, row)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        self.inner.merge(dst_state, src_state)

    def insert_result_into(self, state: Any, column: Any) -> None:
        self.inner.insert_result_into(state, column)

    def serialize_state(self, state: Any) -> bytes:
        return self.inner.serialize_state(state)

    def deserialize_state(self, data: bytes) -> Any:
        return self.inner.deserialize_state(data)

    def requires_arg_columns(self) -> bool:
        return True


def _if_handler(inner_name, full_name, params, arg_types, resolve):
    if arg_types and is_condition_type(arg_types[-1]):
        inner_args = list(arg_types[:-1])
        condition_type: Optional[DataType] = arg_types[-1]
    else:
        inner_args = list(arg_types)
        condition_type = None
    inner = resolve(inner_name, params, inner_args)
    return IfCombinator(full_name, inner, condition_type)


# ══════════════════════════════════════════════════════════════════════════
# -Array —— 对数组的每个元素聚合
# ══════════════════════════════════════════════════════════════════════════


class ArrayCombinator(IAggregateFunction):
    """``-Array`` —— 把 ``Array(T)`` 拆开,对**每个元素**跑基聚合。

    .. code-block:: sql

       SELECT sumArray([1,2,3])           -- 6
       SELECT uniqArray([[1,2],[2,3]])    -- 3  (元素去重后计数)

    语义上等价于 ``sumArray(a) == sum(arraySum(a))``,但它是**一次遍历**
    完成的(不需要先把数组拍平成一个中间列)。
    """

    def __init__(
        self,
        name: str,
        inner: IAggregateFunction,
        arg_types: Sequence[DataType],
        array_index: int,
    ) -> None:
        super().__init__(name, arg_types, inner.params)
        self.inner = inner
        #: 哪个参数是数组(``-1`` 表示"没找到数组参数",退化成基函数)
        self.array_index = array_index

    def get_return_type(self) -> DataType:
        return self.inner.get_return_type()

    def create_state(self) -> Any:
        return self.inner.create_state()

    def clone_state(self, state: Any) -> Any:
        return self.inner.clone_state(state)

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        if self.array_index < 0:
            self.inner.add(state, columns, row)
            return
        arr = columns[self.array_index].get(row)
        if arr is None:
            return
        n_inner = len(self.inner.arg_types)
        if n_inner <= 1:
            for value in arr:
                self.inner.add(state, [ValueColumn((value,))], 0)
            return
        # 多参数时:其余参数取同一行的值,数组参数换成当前元素。
        # **每个参数一个单值列** —— 否则内层若还有 -If 之类的组合器,
        # 它们按下标取列就会读错(曾经的确踩过这个坑)。
        base = [columns[i].get(row) for i in range(n_inner)]
        for value in arr:
            vals = list(base)
            if 0 <= self.array_index < n_inner:
                vals[self.array_index] = value
            self.inner.add(state, [ValueColumn((v,)) for v in vals], 0)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        self.inner.merge(dst_state, src_state)

    def insert_result_into(self, state: Any, column: Any) -> None:
        self.inner.insert_result_into(state, column)

    def serialize_state(self, state: Any) -> bytes:
        return self.inner.serialize_state(state)

    def deserialize_state(self, data: bytes) -> Any:
        return self.inner.deserialize_state(data)


def _array_handler(inner_name, full_name, params, arg_types, resolve):
    array_index = -1
    for i, t in enumerate(arg_types):
        if t.is_array():
            array_index = i
    if array_index < 0:
        # 没有数组参数:退化成基函数(文档已注明这是宽松处理)
        inner = resolve(inner_name, params, arg_types)
        return ArrayCombinator(full_name, inner, arg_types, -1)
    inner_args = list(arg_types)
    inner_args[array_index] = arg_types[array_index].element  # type: ignore[attr-defined]
    inner = resolve(inner_name, params, inner_args)
    return ArrayCombinator(full_name, inner, arg_types, array_index)


# ══════════════════════════════════════════════════════════════════════════
# -State / -Merge —— 物化视图与分布式聚合的基石
# ══════════════════════════════════════════════════════════════════════════


class StateCombinator(IAggregateFunction):
    """``-State`` —— **返回状态而不是结果**。

    .. code-block:: sql

       -- 写入时:把"和"的中间状态存进表,而不是存最终值
       INSERT INTO daily SELECT day, sumState(amount) FROM raw GROUP BY day;
       -- 读取时:把状态合并成结果
       SELECT day, sumMerge(state) FROM daily GROUP BY day;

    这正是 ``AggregatingMergeTree`` 的工作方式:表里存的列类型是
    ``AggregateFunction(sum, ...)``,读出来用 ``-Merge`` 消费。
    """

    def __init__(self, name: str, inner: IAggregateFunction) -> None:
        super().__init__(name, inner.arg_types, inner.params)
        self.inner = inner

    def get_return_type(self) -> DataType:
        from ..types.composite import DataTypeAggregateFunction

        return DataTypeAggregateFunction(
            self.inner.name.lower(), self.inner.arg_types, self.inner.params
        )

    def create_state(self) -> Any:
        return self.inner.create_state()

    def clone_state(self, state: Any) -> Any:
        return self.inner.clone_state(state)

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        self.inner.add(state, columns, row)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        self.inner.merge(dst_state, src_state)

    def insert_result_into(self, state: Any, column: Any) -> None:
        # 结果列是 ColumnAggregateFunction —— 直接把状态对象塞进去
        column.append(state)

    def serialize_state(self, state: Any) -> bytes:
        return self.inner.serialize_state(state)

    def deserialize_state(self, data: bytes) -> Any:
        return self.inner.deserialize_state(data)

    def is_state_combinator(self) -> bool:
        return True

    def requires_arg_columns(self) -> bool:
        return self.inner.requires_arg_columns()


class MergeCombinator(IAggregateFunction):
    """``-Merge`` —— **吃状态、出结果**。

    参数是一个 ``AggregateFunction(...)`` 列。``add`` 拿到的不是原始值,
    而是一段**部分状态**;要做的事就是把它 ``merge`` 进当前状态。
    """

    def __init__(
        self,
        name: str,
        inner: IAggregateFunction,
        state_type: DataType,
    ) -> None:
        super().__init__(name, [state_type], [])
        self.inner = inner
        self.state_type = state_type

    def get_return_type(self) -> DataType:
        return self.inner.get_return_type()

    def create_state(self) -> Any:
        return self.inner.create_state()

    def clone_state(self, state: Any) -> Any:
        return self.inner.clone_state(state)

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:
            return
        # 从磁盘读回的状态可能是**裸字节**(SerializationAggregateFunction
        # 反序列化出来的就是 bytes),也可能是已经解好的状态对象。
        if isinstance(value, (bytes, bytearray)):
            value = self.inner.deserialize_state(bytes(value))
        self.inner.merge(state, value)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        self.inner.merge(dst_state, src_state)

    def insert_result_into(self, state: Any, column: Any) -> None:
        self.inner.insert_result_into(state, column)

    def serialize_state(self, state: Any) -> bytes:
        return self.inner.serialize_state(state)

    def deserialize_state(self, data: bytes) -> Any:
        return self.inner.deserialize_state(data)

    def is_state_combinator(self) -> bool:
        return True


def _state_handler(inner_name, full_name, params, arg_types, resolve):
    inner = resolve(inner_name, params, arg_types)
    return StateCombinator(full_name, inner)


def _merge_handler(inner_name, full_name, params, arg_types, resolve):
    state_type: Optional[DataType] = None
    for t in arg_types:
        if t.is_aggregate_function():
            state_type = t
            break
    if state_type is None:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"{full_name}: -Merge requires an AggregateFunction(...) argument",
        )
    inner = resolve(
        state_type.func_name,  # type: ignore[attr-defined]
        list(state_type.params),  # type: ignore[attr-defined]
        list(state_type.arg_types),  # type: ignore[attr-defined]
    )
    return MergeCombinator(full_name, inner, state_type)


# ══════════════════════════════════════════════════════════════════════════
# -Distinct —— 先按整参数元组去重
# ══════════════════════════════════════════════════════════════════════════


class DistinctCombinator(IAggregateFunction):
    """``-Distinct`` —— 只把**首次出现**的参数元组喂给基聚合。

    ``uniqDistinct(x)`` 与 ``uniq(x)`` 在结果上相同(uniq 本来就去重),
    但 ``sumDistinct(x)`` 才是它真正有用的形态 —— 等价于
    ``sum(DISTINCT x)``。状态里要额外记一个"见过的键集合"。
    """

    def __init__(
        self, name: str, inner: IAggregateFunction, arg_types: Sequence[DataType]
    ) -> None:
        super().__init__(name, arg_types, inner.params)
        self.inner = inner

    def get_return_type(self) -> DataType:
        return self.inner.get_return_type()

    def create_state(self) -> Any:
        return {"inner": self.inner.create_state(), "seen": set()}

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        values = [columns[i].get(row) for i in range(len(self.arg_types))]
        key = key_bytes(values)
        if key in state["seen"]:
            return
        state["seen"].add(key)
        self.inner.add(state["inner"], columns, row)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        """把源里**新出现**的键合并进内层聚合。

        内层状态只有"总和"这类明细(比如 ``sum`` 的累加器),没法从中挑出
        "哪些值还没算过";所以这里把新键**解回原始参数值**,再重新喂一次
        内层聚合 —— 这才是"对去重后的集合做合并"。两个分片有重叠值时,
        旧实现直接累加内层状态会把重叠部分**算两遍**
        (``sumDistinct`` 对 [1,2,2,3] 的两个重叠分片会得到 9 而不是 6)。
        """
        for key in sorted(src_state["seen"] - dst_state["seen"]):
            values = _decode_distinct_key(key, len(self.arg_types))
            cols = [ValueColumn([v]) for v in values]
            self.inner.add(dst_state["inner"], cols, 0)
            dst_state["seen"].add(key)

    def insert_result_into(self, state: Any, column: Any) -> None:
        self.inner.insert_result_into(state["inner"], column)

    def serialize_state(self, state: Any) -> bytes:
        w = ByteWriter()
        seen = sorted(state["seen"])
        w.uvarint(len(seen))
        for key in seen:
            w.uvarint(len(key))
            w.raw(key)
        # 内层状态长度未知 —— 加个长度前缀再拼
        inner_blob = self.inner.serialize_state(state["inner"])
        w.uvarint(len(inner_blob))
        w.raw(inner_blob)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Any:
        r = ByteReader(data)
        count = r.uvarint()
        seen = set()
        for _ in range(count):
            n = r.uvarint()
            seen.add(r.raw(n))
        inner_len = r.uvarint()
        inner = self.inner.deserialize_state(r.raw(inner_len))
        return {"inner": inner, "seen": seen}


def _decode_distinct_key(key: bytes, n_args: int) -> List[Any]:
    """把 :func:`key_bytes` 编出的键解回**原始参数值** —— ``-Distinct`` 的
    merge 要用它把"目的状态里从没出现过的值"重新喂给内层聚合。

    编码格式与 :func:`key_bytes` 严格对称:单参数时直接是一个值的编码,
    多参数时先有一个 ``uvarint`` 的参数个数。
    """
    r = ByteReader(key)
    if n_args == 1:
        return [decode_key_value(r)]
    count = r.uvarint()
    return [decode_key_value(r) for _ in range(count)]


def _distinct_handler(inner_name, full_name, params, arg_types, resolve):
    inner = resolve(inner_name, params, arg_types)
    return DistinctCombinator(full_name, inner, arg_types)


# ══════════════════════════════════════════════════════════════════════════
# -OrNull / -OrDefault —— 空分组怎么表示
# ══════════════════════════════════════════════════════════════════════════


class OrNullCombinator(IAggregateFunction):
    """``-OrNull`` —— **空分组返回 NULL**,而不是默认值。

    .. code-block:: sql

       SELECT sumOrNull(x) FROM (SELECT 1 WHERE 0);   -- NULL(而不是 0)
       SELECT sum(x)       FROM (SELECT 1 WHERE 0);   -- 0

    ``sum(0)`` 和 ``sum(无行)`` 在纯 ``sum`` 下无法区分(都返回 0)——
    ``-OrNull`` 就是为了让"空集"能被表达出来。
    """

    def __init__(self, name: str, inner: IAggregateFunction) -> None:
        super().__init__(name, inner.arg_types, inner.params)
        self.inner = inner

    def get_return_type(self) -> DataType:
        return make_nullable(self.inner.get_return_type())

    def create_state(self) -> Any:
        return {"inner": self.inner.create_state(), "rows": 0}

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        state["rows"] += 1
        self.inner.add(state["inner"], columns, row)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        dst_state["rows"] += src_state["rows"]
        self.inner.merge(dst_state["inner"], src_state["inner"])

    def insert_result_into(self, state: Any, column: Any) -> None:
        if state["rows"] == 0:
            column.append(None)
            return
        self.inner.insert_result_into(state["inner"], column)

    def serialize_state(self, state: Any) -> bytes:
        w = ByteWriter()
        w.uvarint(state["rows"])
        blob = self.inner.serialize_state(state["inner"])
        w.uvarint(len(blob))
        w.raw(blob)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Any:
        r = ByteReader(data)
        rows = r.uvarint()
        blob_len = r.uvarint()
        inner = self.inner.deserialize_state(r.raw(blob_len))
        return {"inner": inner, "rows": rows}


class OrDefaultCombinator(IAggregateFunction):
    """``-OrDefault`` —— **空分组返回类型的默认值**(``0`` / ``''`` / ``[]``)。

    与 :class:`OrNullCombinator` 是"空集表示"的两种取法:
    ``-OrDefault`` 保持结果类型不变(不引入 ``Nullable``),
    ``-OrNull`` 引入 NULL。真实 CH 里两个都存在,因为
    "要不要为可空性付代价"本身是一个性能选择。
    """

    def __init__(self, name: str, inner: IAggregateFunction) -> None:
        super().__init__(name, inner.arg_types, inner.params)
        self.inner = inner

    def get_return_type(self) -> DataType:
        return self.inner.get_return_type()

    def create_state(self) -> Any:
        return {"inner": self.inner.create_state(), "rows": 0}

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        state["rows"] += 1
        self.inner.add(state["inner"], columns, row)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        dst_state["rows"] += src_state["rows"]
        self.inner.merge(dst_state["inner"], src_state["inner"])

    def insert_result_into(self, state: Any, column: Any) -> None:
        if state["rows"] == 0:
            column.append(self.inner.get_return_type().default_value())
            return
        self.inner.insert_result_into(state["inner"], column)

    def serialize_state(self, state: Any) -> bytes:
        w = ByteWriter()
        w.uvarint(state["rows"])
        blob = self.inner.serialize_state(state["inner"])
        w.uvarint(len(blob))
        w.raw(blob)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Any:
        r = ByteReader(data)
        rows = r.uvarint()
        blob_len = r.uvarint()
        inner = self.inner.deserialize_state(r.raw(blob_len))
        return {"inner": inner, "rows": rows}


def _ornull_handler(inner_name, full_name, params, arg_types, resolve):
    return OrNullCombinator(full_name, resolve(inner_name, params, arg_types))


def _ordefault_handler(inner_name, full_name, params, arg_types, resolve):
    return OrDefaultCombinator(full_name, resolve(inner_name, params, arg_types))


# ══════════════════════════════════════════════════════════════════════════
# -Resample —— 时间轴分桶聚合(简化)
# ══════════════════════════════════════════════════════════════════════════


class ResampleCombinator(IAggregateFunction):
    """``-Resample`` —— 按时间区间把数据切片分别聚合。

    .. warning::

       **mini-clickhouse 不做真正的重采样**。真实的
       ``-Resample(start, end, step)`` 会先给每个分组准备
       ``(end-start)/step`` 个状态、按时间参数把行分派到对应状态、
       最后把状态按起始时间排序后串起来。这里只把 ``(start, step)``
       参数**记下来**,行为与基函数完全相同 —— 因为"分桶"这件事
       在 mini-clickhouse 里更适合由执行层的窗口处理器演示,
       而不是塞进聚合函数(会掩盖"聚合只管状态"这条主线)。
    """

    def __init__(self, name: str, inner: IAggregateFunction, params: Sequence[Any]) -> None:
        super().__init__(name, inner.arg_types, params)
        self.inner = inner
        self.start = params[0] if len(params) > 0 else 0
        self.end = params[1] if len(params) > 1 else 0
        self.step = params[2] if len(params) > 2 else 1

    def get_return_type(self) -> DataType:
        return self.inner.get_return_type()

    def create_state(self) -> Any:
        return self.inner.create_state()

    def clone_state(self, state: Any) -> Any:
        return self.inner.clone_state(state)

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        self.inner.add(state, columns, row)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        self.inner.merge(dst_state, src_state)

    def insert_result_into(self, state: Any, column: Any) -> None:
        self.inner.insert_result_into(state, column)

    def serialize_state(self, state: Any) -> bytes:
        return self.inner.serialize_state(state)

    def deserialize_state(self, data: bytes) -> Any:
        return self.inner.deserialize_state(data)


def _resample_handler(inner_name, full_name, params, arg_types, resolve):
    inner = resolve(inner_name, params, arg_types)
    return ResampleCombinator(full_name, inner, params)


# ══════════════════════════════════════════════════════════════════════════
# -ForEach —— 对多个数组逐元素聚合,返回数组
# ══════════════════════════════════════════════════════════════════════════


class ForEachCombinator(IAggregateFunction):
    """``-ForEach`` —— 把 N 个数组的**每个位置**当成一个独立的聚合。

    .. code-block:: sql

       SELECT avgForEach([1,2,3], [4,5])  -- [1,2,3] 与 [4,5] 逐位平均?

    真实语义:每个下标位置 j 上有若干值,``avgForEach`` 返回
    "每个位置的平均值"组成的数组。所以结果是 ``Array(基函数结果)``。
    """

    def __init__(
        self, name: str, inner: IAggregateFunction, arg_types: Sequence[DataType]
    ) -> None:
        super().__init__(name, arg_types, inner.params)
        self.inner = inner

    def get_return_type(self) -> DataType:
        return DataTypeArray(self.inner.get_return_type())

    def create_state(self) -> Any:
        return []  # 每个位置一个内层状态,按需增长

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        n_args = len(self.inner.arg_types)
        arrays = [columns[i].get(row) for i in range(n_args)]
        lengths = [len(a) for a in arrays if a is not None]
        n = max(lengths) if lengths else 0
        while len(state) < n:
            state.append(self.inner.create_state())
        for j in range(n):
            vals: List[Any] = []
            for a in arrays:
                vals.append(a[j] if (a is not None and j < len(a)) else None)
            # 同样:每个参数一个单值列(见 ArrayCombinator.add 的说明)
            self.inner.add(state[j], [ValueColumn((v,)) for v in vals], 0)

    def merge(self, dst_state: Any, src_state: Any) -> None:
        while len(dst_state) < len(src_state):
            dst_state.append(self.inner.create_state())
        for i, st in enumerate(src_state):
            self.inner.merge(dst_state[i], st)

    def insert_result_into(self, state: Any, column: Any) -> None:
        out_col = self.inner.get_return_type().create_column()
        for st in state:
            self.inner.insert_result_into(st, out_col)
        column.append(out_col.to_list())

    def serialize_state(self, state: Any) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for st in state:
            blob = self.inner.serialize_state(st)
            w.uvarint(len(blob))
            w.raw(blob)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Any:
        r = ByteReader(data)
        n = r.uvarint()
        out = []
        for _ in range(n):
            blob_len = r.uvarint()
            out.append(self.inner.deserialize_state(r.raw(blob_len)))
        return out


def _foreach_handler(inner_name, full_name, params, arg_types, resolve):
    element_types = [t.element if t.is_array() else t for t in arg_types]  # type: ignore[attr-defined]
    inner = resolve(inner_name, params, element_types)
    return ForEachCombinator(full_name, inner, arg_types)


# ══════════════════════════════════════════════════════════════════════════
# 注册 —— 顺序即"剥后缀"的优先级
# ══════════════════════════════════════════════════════════════════════════

register_combinator("Merge", _merge_handler)
register_combinator("State", _state_handler)
register_combinator("Resample", _resample_handler)
register_combinator("OrNull", _ornull_handler)
register_combinator("OrDefault", _ordefault_handler)
register_combinator("Distinct", _distinct_handler)
register_combinator("Array", _array_handler)
register_combinator("ForEach", _foreach_handler)
register_combinator("If", _if_handler)
