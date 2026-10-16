"""``any`` 家族 —— "随便取一个值",以及它们的三种取法。

.. code-block:: text

   any(x)      取"某个"值(通常是第一个遇到的)
   anyLast(x)  取"最后一个"值
   anyHeavy(x) 取"出现次数最多"的值(近似)

这族函数看起来是"偷懒"的(任意值有什么意义?),但它们在真实 ClickHouse
里非常常用,原因是:

1. **GROUP BY 后的"携带列"**。``SELECT user_id, any(country) FROM ...``
   —— 既然按 ``user_id`` 分组了,``country`` 在这个组里通常只有一个值,
   用 ``any`` 把它带出来比写 ``max`` 更便宜;
2. **配合 ``-OrNull`` 区分"空组"**;
3. ``anyHeavy`` 是"近似众数",在"这一组里最常见的是哪一类"这种问题上
   比精确众数省很多内存(它用一个类似 HeavyKeeper 的算法)。

.. note::

   ``any`` 的"第一个"在**并行执行**下并不保证是"输入顺序的第一个" ——
   真实 ClickHouse 只保证"是这一组里某个值"。mini-clickhouse 顺序执行,
   所以它确实是第一个;但**不要依赖**这个性质写查询。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from ..errors import ErrorCode, raise_error
from ..types import DataType
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    decode_key_value,
    encode_key_value,
    key_bytes,
    register_aggregate,
)

__all__ = ["AggregateFunctionAny", "AggregateFunctionAnyLast", "AggregateFunctionAnyHeavy"]


class _AggregateFunctionAnyBase(IAggregateFunction):
    """``any`` / ``anyLast`` 的公共实现。"""

    #: 子类覆盖:``True`` 表示"保留最后一个"(每个值都覆盖)
    _keep_last = False

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
        return [False, None]

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:
            # 默认的 ``any``/``anyLast`` **跳过 NULL**(真实 CH:``any`` 是
            # ``first_value`` 的别名,默认 IGNORE NULLS;保留 NULL 的是
            # ``any_respect_nulls``/``anyLast_respect_nulls`` 变体)。
            # 早先的实现把 ``None`` 也当成一个值存进去,于是
            # ``any(Nullable 列)`` 会返回 NULL。
            return
        if state[0] and not self._keep_last:
            return  # any:第一个值就锁定了
        state[0] = True
        state[1] = value

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        if not src_state[0]:
            return
        if dst_state[0] and not self._keep_last:
            return
        dst_state[0] = True
        dst_state[1] = src_state[1]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        if state[0]:
            column.append(state[1])
        else:
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


class AggregateFunctionAny(_AggregateFunctionAnyBase):
    """``any(x)`` —— 取第一个值。"""

    _keep_last = False


class AggregateFunctionAnyLast(_AggregateFunctionAnyBase):
    """``anyLast(x)`` —— 取最后一个值。"""

    _keep_last = True


class AggregateFunctionAnyHeavy(IAggregateFunction):
    """``anyHeavy(x)`` —— 取**出现次数最多**的值(近似众数)。

    .. warning::

       真实的 ``anyHeavy`` 用 HeavyKeeper 这类"有误差的计数"算法,
       内存占用 O(log n) 而不是 O(基数)。mini-clickhouse 用的是
       **精确计数字典** —— 结果永远正确(所以比真实实现更准),
       代价是内存随基数线性增长。想体验"近似"的味道,请用 ``topK``。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        if not arg_types:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "anyHeavy requires exactly one argument",
            )

    def get_return_type(self) -> DataType:
        return self.arg_types[0]

    def create_state(self) -> Dict[bytes, List[Any]]:
        # 键 = key_bytes(值),值 = [出现次数, 原始值]
        return {}

    def add(self, state: Dict[bytes, List[Any]], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        k = key_bytes((value,))
        entry = state.get(k)
        if entry is None:
            state[k] = [1, value]
        else:
            entry[0] += 1

    def merge(self, dst_state: Dict[bytes, List[Any]], src_state: Dict[bytes, List[Any]]) -> None:
        for k, (count, value) in src_state.items():
            entry = dst_state.get(k)
            if entry is None:
                dst_state[k] = [count, value]
            else:
                entry[0] += count

    def insert_result_into(self, state: Dict[bytes, List[Any]], column: Any) -> None:
        if not state:
            column.append(self.get_return_type().default_value())
            return
        # 取计数最大的那个;并列时按 key 字节序保证确定性
        best_key = max(sorted(state), key=lambda k: state[k][0])
        column.append(state[best_key][1])

    def serialize_state(self, state: Dict[bytes, List[Any]]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for k in sorted(state):
            count, value = state[k]
            w.uvarint(len(k))
            w.raw(k)
            w.uvarint(count)
            encode_key_value(w, value)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Dict[bytes, List[Any]]:
        r = ByteReader(data)
        n = r.uvarint()
        out: Dict[bytes, List[Any]] = {}
        for _ in range(n):
            klen = r.uvarint()
            k = r.raw(klen)
            count = r.uvarint()
            value = decode_key_value(r)
            out[k] = [count, value]
        return out


register_aggregate(
    "any",
    AggregateFunctionAny,
    description="取第一个遇到的值(可携带列常用)",
    aliases=["first_value"],
)
register_aggregate(
    "anyLast",
    AggregateFunctionAnyLast,
    description="取最后一个值",
    aliases=["last_value"],
)
register_aggregate(
    "anyHeavy",
    AggregateFunctionAnyHeavy,
    description="取出现次数最多的值(精确众数;真实实现是近似的)",
)
