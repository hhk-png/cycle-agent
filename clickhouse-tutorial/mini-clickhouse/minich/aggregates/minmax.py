"""``min`` / ``max`` —— 最简单的"可合并"聚合,也是 ``SimpleAggregateFunction`` 的样板。

.. code-block:: sql

    SELECT min(x), max(x) FROM t;

``min`` / ``max`` 的状态就是**当前的最小值/最大值本身**。这带来一个
非常重要的性质 —— 它满足:

.. code-block:: text

   f(f(a, b), c) == f(a, f(b, c))      ← 结合律
   而且中间结果可以直接当最终值存          ← "状态 == 值"

所以 ``min`` / ``max`` 可以用 ``SimpleAggregateFunction(min, UInt64)``
来存:表里那一列**就是**一个普通 ``UInt64``,不需要二进制状态。
``sum`` 也满足(加法有结合律),但 ``avg`` / ``uniq`` 不满足 ——
它们的"状态"和"结果"是两回事。

.. warning::

   **NULL 的处理**:``min`` / ``max`` 忽略 NULL(空集 / 全 NULL 时返回
   结果类型的**默认值** ``0`` / ``''``)。这与真实 ClickHouse 一致 ——
   但它意味着 ``min`` 结果里**区分不出**"没有非空值"和"最小值恰好是 0"。
   要区分请用 ``minOrNull``(由 ``-OrNull`` 组合器生成)。
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

__all__ = ["AggregateFunctionMin", "AggregateFunctionMax"]


class _AggregateFunctionMinMax(IAggregateFunction):
    """``min`` / ``max`` 的公共实现(只差一个比较方向)。"""

    #: 子类覆盖:``True`` 表示取最小,``False`` 表示取最大
    _take_min = True

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        if not arg_types:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Aggregate function {name} requires exactly one argument",
            )

    def get_return_type(self) -> DataType:
        return self.arg_types[0]

    def create_state(self) -> List[Any]:
        # [是否已有值, 当前值] —— 用一个显式的 has 标志,
        # 因为值本身可能是 0 / '' / None,不能靠它判断"还没见过值"。
        return [False, None]

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:
            return
        if not state[0]:
            state[0] = True
            state[1] = value
            return
        better = value < state[1] if self._take_min else value > state[1]
        if better:
            state[1] = value

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        if not src_state[0]:
            return
        if not dst_state[0]:
            dst_state[0] = True
            dst_state[1] = src_state[1]
            return
        better = src_state[1] < dst_state[1] if self._take_min else src_state[1] > dst_state[1]
        if better:
            dst_state[1] = src_state[1]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        if state[0]:
            column.append(state[1])
        else:
            # 空集 → 结果类型的默认值(与真实 CH 的"忽略 NULL"一致)
            column.append(self.get_return_type().default_value())

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        w.raw(b"\x01" if state[0] else b"\x00")
        if state[0]:
            encode_key_value(w, state[1])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        r = ByteReader(data)
        if r.raw(1) == b"\x00":
            return [False, None]
        return [True, decode_key_value(r)]


class AggregateFunctionMin(_AggregateFunctionMinMax):
    """``min(x)`` —— 最小值。"""

    _take_min = True


class AggregateFunctionMax(_AggregateFunctionMinMax):
    """``max(x)`` —— 最大值。"""

    _take_min = False


register_aggregate("min", AggregateFunctionMin, description="最小值(忽略 NULL,空集返回类型默认值)")
register_aggregate("max", AggregateFunctionMax, description="最大值(忽略 NULL,空集返回类型默认值)")
