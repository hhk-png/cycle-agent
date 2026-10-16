"""``avg`` 家族 —— 平均值,以及加权平均。

平均值是"**状态必须可合并**"这条原则最漂亮的例子。朴素想法的状态是
"到目前为止的平均值",但那个状态**不可合并**:

.. code-block:: text

   分片 A: [10, 20]        平均值 = 15
   分片 B: [30, 40, 200]   平均值 = 90

   (15 + 90) / 2 = 52.5   ✗  错!
   (10+20+30+40+200) / 5 = 60  ✓

所以 ``avg`` 真正的状态是 **(和, 计数)** —— 两个都可加,合并后
重新做一次除法就对了。这正是 ``avg`` 为什么属于
``AggregateFunction``(需要存二进制状态)而不是 ``SimpleAggregateFunction``
(状态就是最终值)的原因。

.. code-block:: text

    add(x):    sum += x ;  count += 1
    merge:     sum_dst += sum_src ;  count_dst += count_src
    result:    sum / count          ← 只在最后做一次
"""

from __future__ import annotations

from typing import Any, List, Sequence

from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from .base import ByteReader, ByteWriter, IAggregateFunction, register_aggregate

__all__ = ["AggregateFunctionAvg", "AggregateFunctionAvgWeighted"]


class AggregateFunctionAvg(IAggregateFunction):
    """``avg(x)`` —— 状态是 ``[和, 计数]``,结果是 ``Float64``。

    空分组的 ``avg`` 是 ``nan``(0 除以 0),与真实 ClickHouse 一致 ——
    注意它**不是** 0,也不是 NULL。想要 NULL 请用 ``avgOrNull``。
    """

    def get_return_type(self) -> DataType:
        return get_data_type("Float64")

    def create_state(self) -> List[Any]:
        return [0, 0]

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:  # avg 忽略 NULL,而且**不把它计入分母**
            return
        state[0] += value
        state[1] += 1

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        dst_state[0] += src_state[0]
        dst_state[1] += src_state[1]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        if state[1] == 0:
            column.append(float("nan"))
        else:
            column.append(float(state[0]) / float(state[1]))

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        w.number(state[0])
        w.uvarint(state[1])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        r = ByteReader(data)
        total = r.number()
        count = r.uvarint()
        return [total, count]


class AggregateFunctionAvgWeighted(IAggregateFunction):
    """``avgWeighted(x, weight)`` —— 加权平均 ``Σ(x·w) / Σw``。

    .. code-block:: sql

       SELECT avgWeighted(x, w) FROM t;   -- 每个 x 的贡献按 w 加权

    加权平均在统计里就是"把每个值重复 ``w`` 次后的平均",但不需要真的
    展开 —— 所以状态是 ``[Σ(x·w), Σw]``,同样是"两个可加量"。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        if len(arg_types) < 2:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "avgWeighted requires two arguments: (value, weight)",
            )

    def get_return_type(self) -> DataType:
        return get_data_type("Float64")

    def create_state(self) -> List[float]:
        return [0.0, 0.0]

    def add(self, state: List[float], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        weight = columns[1].get(row)
        if value is None or weight is None:
            return
        state[0] += value * weight
        state[1] += weight

    def merge(self, dst_state: List[float], src_state: List[float]) -> None:
        dst_state[0] += src_state[0]
        dst_state[1] += src_state[1]

    def insert_result_into(self, state: List[float], column: Any) -> None:
        if state[1] == 0:
            column.append(float("nan"))
        else:
            column.append(state[0] / state[1])

    def serialize_state(self, state: List[float]) -> bytes:
        w = ByteWriter()
        w.f64(state[0])
        w.f64(state[1])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[float]:
        r = ByteReader(data)
        return [r.f64(), r.f64()]


register_aggregate("avg", AggregateFunctionAvg, description="平均值:状态是(和, 计数),空集返回 nan")
register_aggregate(
    "avgWeighted",
    AggregateFunctionAvgWeighted,
    description="加权平均:Σ(x·w)/Σw",
)
