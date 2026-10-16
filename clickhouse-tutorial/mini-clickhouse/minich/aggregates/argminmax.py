"""``argMin`` / ``argMax`` —— "取使某个量最小/最大的那一行"。

.. code-block:: sql

    -- 每个用户耗时最长的那次请求的 URL
    SELECT user_id, argMax(url, duration) FROM requests GROUP BY user_id;

它是 "topN 里 N=1" 的精确形态:不求整个排序,只留一个当前最优。

.. code-block:: text

   状态 = [已有值?, 当前最优的"排名依据 val", 对应的"取回值 arg"]
   add: 若 val < best_val(对 argMin)→ 一起替换
   result: 返回 best_arg

.. note::

   ``argMin`` / ``argMax`` 是**不可交换**的(``argMax(a, b)`` 与
   ``argMax(b, a)`` 是两回事),所以不能用 ``SimpleAggregateFunction``
   存 —— 必须走 ``AggregateFunction``(状态里得同时记住 arg 和 val)。

   当只给一个参数时(``argMax(x)``),排名依据和被取回的值都是 ``x`` 本身,
   于是它退化成 ``max(x)`` —— 这是 mini-clickhouse 的宽松处理,
   真实 ClickHouse 要求两个参数。
"""

from __future__ import annotations

from typing import Any, List, Sequence

from ..errors import ErrorCode, raise_error
from ..types import DataType
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    decode_key_value,
    encode_key_value,
    register_aggregate,
)

__all__ = ["AggregateFunctionArgMin", "AggregateFunctionArgMax"]


class _AggregateFunctionArgMinMax(IAggregateFunction):
    """``argMin`` / ``argMax`` 的公共实现。"""

    #: 子类覆盖:``True`` 表示取 argMin
    _take_min = True

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        if not arg_types:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Aggregate function {name} requires at least one argument",
            )

    def get_return_type(self) -> DataType:
        return self.arg_types[0]

    def create_state(self) -> List[Any]:
        # [has, best_val, best_arg]
        return [False, None, None]

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        arg = columns[0].get(row)
        val = columns[1].get(row) if len(columns) >= 2 else arg
        if val is None:
            return
        if not state[0]:
            state[0] = True
            state[1] = val
            state[2] = arg
            return
        better = val < state[1] if self._take_min else val > state[1]
        if better:
            state[1] = val
            state[2] = arg

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        if not src_state[0]:
            return
        if not dst_state[0]:
            dst_state[0] = True
            dst_state[1] = src_state[1]
            dst_state[2] = src_state[2]
            return
        better = src_state[1] < dst_state[1] if self._take_min else src_state[1] > dst_state[1]
        if better:
            dst_state[1] = src_state[1]
            dst_state[2] = src_state[2]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        if state[0]:
            column.append(state[2])
        else:
            column.append(self.get_return_type().default_value())

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        w.raw(b"\x01" if state[0] else b"\x00")
        if state[0]:
            encode_key_value(w, state[1])
            encode_key_value(w, state[2])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        r = ByteReader(data)
        if r.raw(1) == b"\x00":
            return [False, None, None]
        val = decode_key_value(r)
        arg = decode_key_value(r)
        return [True, val, arg]


class AggregateFunctionArgMin(_AggregateFunctionArgMinMax):
    """``argMin(arg, val)`` —— 取 ``val`` 最小时对应的 ``arg``。"""

    _take_min = True


class AggregateFunctionArgMax(_AggregateFunctionArgMinMax):
    """``argMax(arg, val)`` —— 取 ``val`` 最大时对应的 ``arg``。"""

    _take_min = False


register_aggregate("argMin", AggregateFunctionArgMin, description="取使 val 最小的那行的 arg")
register_aggregate("argMax", AggregateFunctionArgMax, description="取使 val 最大的那行的 arg")
