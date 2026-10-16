"""``count`` 家族 —— 最基础、也最容易被想错的一族聚合。

.. code-block:: text

    count()        数"有多少行"                 → UInt64
    count(x)       数"有多少行 x 不是 NULL"      → UInt64
    countIf(x, c)  = count + -If                → UInt64
    countDistinct(x)  数"x 有多少个不同的值"      → UInt64

三条必须记住的语义:

1. ``count()`` 与 ``count(x)`` **不一样**。前者不看任何列(所以可以整列
   跳过不读),后者要读 ``x`` 那一列并跳过 NULL。这是"按列裁剪"的一个
   直接体现 —— ``count()`` 甚至可以不碰任何一个数据文件,只读元数据里的
   行数(这也是 ClickHouse ``SELECT count()`` 极快的根本原因);
2. 返回值**永远是 ``UInt64``**,而且**永远不可空** —— 空集的 ``count()``
   是 ``0``,不是 ``NULL``。(对比 ``sum`` 空集是 ``0``、``avg`` 空集是
   ``nan``。)
3. ``count`` 的状态就是一个整数,所以它**可合并**且**可序列化**成本极低
   —— ``merge`` 就是加法。这是分布式 ``count()`` 为什么几乎是免费的。

.. note::

   Python 的 ``int`` 是**不可变**的,而接口约定 ``merge`` **就地**改
   ``dst_state``。所以这里把状态包成**单元素列表** ``[n]`` —— 这是本模块
   里唯一一个"状态不是裸值"的基础聚合,其余函数的状态天然可变
   (dict / list / set)。
"""

from __future__ import annotations

from typing import Any, List, Sequence

from ..types import DataType, get_data_type
from .base import ByteReader, ByteWriter, IAggregateFunction, key_bytes, register_aggregate

__all__ = ["AggregateFunctionCount", "AggregateFunctionCountDistinct"]


def _uint64() -> DataType:
    return get_data_type("UInt64")


class AggregateFunctionCount(IAggregateFunction):
    """``count`` / ``count(x)`` —— 状态是 ``[UInt64]``(单元素可变列表)。"""

    def __init__(
        self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()
    ) -> None:
        super().__init__(name, arg_types, params)
        #: 没有参数时整列都不用读 —— 引擎据此走"零列"快路径
        self._requires_columns = bool(arg_types)

    def get_return_type(self) -> DataType:
        return _uint64()

    def create_state(self) -> List[int]:
        return [0]

    def add(self, state: List[int], columns: Sequence[Any], row: int) -> None:
        if self.arg_types and columns[0].get(row) is None:
            # count(x):只有 x 非 NULL 才计数(与真实 CH 一致)
            return
        state[0] += 1

    def merge(self, dst_state: List[int], src_state: List[int]) -> None:
        dst_state[0] += src_state[0]

    def insert_result_into(self, state: List[int], column: Any) -> None:
        column.append(state[0])

    def serialize_state(self, state: List[int]) -> bytes:
        w = ByteWriter()
        w.u64(state[0])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[int]:
        return [ByteReader(data).u64()]

    def requires_arg_columns(self) -> bool:
        return self._requires_columns


class AggregateFunctionCountDistinct(IAggregateFunction):
    """``countDistinct`` —— 严格去重计数(语义等价于 ``uniqExact``)。

    真实 ClickHouse 把 ``countDistinct`` 实现成 ``uniqExact`` 的别名。
    mini-clickhouse 单独实现一份,是为了把它当成"**精确去重**"的范本:
    状态就是一个键的集合,``merge`` 是并集,回答"到底有多少个不同值"
    永远精确 —— 代价是内存占用随基数线性增长(这正是 ``uniq`` 存在的理由)。
    """

    def __init__(
        self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()
    ) -> None:
        super().__init__(name, arg_types, params)

    def get_return_type(self) -> DataType:
        return _uint64()

    def create_state(self) -> set:
        return set()

    def add(self, state: set, columns: Sequence[Any], row: int) -> None:
        values = [columns[i].get(row) for i in range(len(self.arg_types))]
        # ``count(DISTINCT ...)`` / ``countDistinct`` 走的是 ``uniqExact`` 语义
        # (真实 CH 的 ``count_distinct_implementation`` 默认就是它),而
        # ``uniq*`` 整族**忽略 NULL**:任一实参为 NULL 的行**整行不计**。
        if any(v is None for v in values):
            return
        state.add(key_bytes(values))

    def merge(self, dst_state: set, src_state: set) -> None:
        dst_state |= src_state

    def insert_result_into(self, state: set, column: Any) -> None:
        column.append(len(state))

    def serialize_state(self, state: set) -> bytes:
        w = ByteWriter()
        keys = sorted(state)  # 排序 → 序列化确定性
        w.uvarint(len(keys))
        for k in keys:
            w.uvarint(len(k))
            w.raw(k)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> set:
        r = ByteReader(data)
        n = r.uvarint()
        out = set()
        for _ in range(n):
            length = r.uvarint()
            out.add(r.raw(length))
        return out


register_aggregate(
    "count",
    AggregateFunctionCount,
    description="计数:count() 数行数,count(x) 数非 NULL 的行数",
)

register_aggregate(
    "countDistinct",
    AggregateFunctionCountDistinct,
    description="精确去重计数(等价于 uniqExact,但名字强调「计数」语义)",
)
