"""``sum`` 家族 —— 求和,以及它的几种"**数值上更小心**"的变体。

求和看起来是聚合里最简单的一件事,但在列存数据库里它有一堆值得讲的
细节,这一族函数基本覆盖了它们:

.. code-block:: text

    sum            普通求和。小整数会**自动提升**:sum(UInt8) → UInt64
    sumWithOverflow 结果类型 = 输入类型,溢出就**回绕**(不提升)
    sumKahan       用 Kahan 补偿求和,抵消浮点误差累积
    sumCount       同时返回 (和, 计数) —— 算平均值用
    sumMap         按 key 分组求和,返回一个 Map

**为什么小整数求和会提升?** 因为 ``SELECT sum(x)`` 里 ``x`` 是 ``UInt8``
的话,一列 100 万个 200 会立刻溢出成荒唐值。ClickHouse 的规则是:

.. code-block:: text

   输入类型          sum 的结果类型
   UInt8/16/32   →   UInt64
   UInt64        →   UInt64
   Int8/16/32    →   Int64
   Int64         →   Int64
   Float32/64    →   Float64
   Decimal       →   同类型

对照一下 ``sumWithOverflow``:它**不提升**,结果类型等于输入类型 ——
所以 ``sumWithOverflow(UInt8)`` 会像 C++ 的 ``uint8_t`` 那样回绕。
这个区别是"为什么默认的 sum 更安全"的答案。

.. note::

   Python 的整数是任意精度的,所以 mini-clickhouse 的 ``sum`` 在写回结果
   列时才会按结果类型**回绕**。真实 ClickHouse 在累加过程中溢出是
   UB(未定义行为),这里用"先精确累加、再回绕"代替 —— 对**不溢出**的
   情况完全一致,对溢出的情况更可预测(也更安全)。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.composite import DataTypeMap
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    decode_key_value,
    encode_key_value,
    register_aggregate,
    wrap_int_to_type,
)

__all__ = [
    "sum_result_type",
    "AggregateFunctionSum",
    "AggregateFunctionSumWithOverflow",
    "AggregateFunctionSumKahan",
    "AggregateFunctionSumCount",
    "AggregateFunctionSumMap",
]


def sum_result_type(arg_type: DataType) -> DataType:
    """``sum`` 的结果类型 —— 小整数自动提升到位宽 64 位。"""
    if arg_type.is_decimal():
        return arg_type
    if arg_type.is_float():
        return get_data_type("Float64")
    if not arg_type.is_numeric():
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"sum requires a numeric argument, got {arg_type.get_name()}",
        )
    bits = getattr(arg_type, "bits", 64)
    if bits > 64:
        return arg_type
    if getattr(arg_type, "signed", True):
        return get_data_type("Int64")
    return get_data_type("UInt64")


def _require_arg(fn: str, arg_types: Sequence[DataType]) -> DataType:
    if not arg_types:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Aggregate function {fn} requires an argument",
        )
    return arg_types[0]


class AggregateFunctionSum(IAggregateFunction):
    """``sum`` —— 状态是 ``[和]``。"""

    def get_return_type(self) -> DataType:
        return sum_result_type(_require_arg(self.name, self.arg_types))

    def create_state(self) -> List[Any]:
        return [0]

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:  # sum 忽略 NULL
            return
        state[0] += value

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        dst_state[0] += src_state[0]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        rt = self.get_return_type()
        value = state[0]
        if rt.is_integer():
            value = wrap_int_to_type(int(value), rt)
        column.append(value)

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        w.number(state[0])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        return [ByteReader(data).number()]


class AggregateFunctionSumWithOverflow(IAggregateFunction):
    """``sumWithOverflow`` —— 结果类型 = 输入类型,溢出**回绕**。

    .. code-block:: sql

       SELECT sumWithOverflow(x), sum(x) FROM (SELECT toUInt8(200) AS x UNION ALL SELECT 200);
       -- sumWithOverflow = 144(UInt8 回绕),sum = 400(UInt64)
    """

    def get_return_type(self) -> DataType:
        return _require_arg(self.name, self.arg_types)

    def create_state(self) -> List[int]:
        return [0]

    def add(self, state: List[int], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:
            return
        state[0] += int(value)

    def merge(self, dst_state: List[int], src_state: List[int]) -> None:
        dst_state[0] += src_state[0]

    def insert_result_into(self, state: List[int], column: Any) -> None:
        column.append(wrap_int_to_type(int(state[0]), self.get_return_type()))

    def serialize_state(self, state: List[int]) -> bytes:
        w = ByteWriter()
        w.number(state[0])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[int]:
        return [int(ByteReader(data).number())]


class AggregateFunctionSumKahan(IAggregateFunction):
    """``sumKahan`` —— **Kahan 补偿求和**,专门对付浮点误差累积。

    .. code-block:: text

       朴素求和 0.1+0.2+... 加到 1e7 项,误差能到 1e-3 量级;
       Kahan 用一个"补偿项 c"记住每次加法**丢掉**的低位,
       把它们攒起来下次补回去,误差降到 1e-10 量级。

       y = x - c          # 先把上次的补偿减掉
       t = sum + y        # 朴素相加(这里会丢低位)
       c = (t - sum) - y  # 精确算出"丢了什么"
       sum = t

    真实 ClickHouse 也提供 ``sumKahan``,实现就是这段教科书算法。
    """

    def get_return_type(self) -> DataType:
        # 恒为 Float64 —— 补偿求和只在浮点上才有意义
        return get_data_type("Float64")

    def create_state(self) -> List[float]:
        return [0.0, 0.0]  # [sum, compensation]

    def add(self, state: List[float], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:
            return
        x = float(value) - state[1]
        t = state[0] + x
        state[1] = (t - state[0]) - x
        state[0] = t

    def merge(self, dst_state: List[float], src_state: List[float]) -> None:
        # 把两个部分和(Kahan 意义上)合并:先加和,再补补偿项
        y = src_state[0] - dst_state[1]
        t = dst_state[0] + y
        dst_state[1] = (t - dst_state[0]) - y
        dst_state[0] = t
        dst_state[0] += src_state[1]

    def insert_result_into(self, state: List[float], column: Any) -> None:
        column.append(state[0])

    def serialize_state(self, state: List[float]) -> bytes:
        w = ByteWriter()
        w.f64(state[0])
        w.f64(state[1])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[float]:
        r = ByteReader(data)
        return [r.f64(), r.f64()]


class AggregateFunctionSumCount(IAggregateFunction):
    """``sumCount`` —— 同时算出 ``(sum, count)``,返回 ``Tuple``。

    它存在的意义是"算平均值但不想再扫一遍":``avg(x)`` 内部的状态
    其实就是 ``sumCount``,只不过最后多做一次除法。当你想在应用层
    自己算"分位数之外的各种比值平均值"时,直接 ``sumCount`` 更灵活。
    """

    def get_return_type(self) -> DataType:
        from ..types.composite import DataTypeTuple

        st = sum_result_type(_require_arg(self.name, self.arg_types))
        return DataTypeTuple([st, get_data_type("UInt64")])

    def create_state(self) -> List[Any]:
        return [0, 0]  # [sum, count]

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:
            return
        state[0] += value
        state[1] += 1

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        dst_state[0] += src_state[0]
        dst_state[1] += src_state[1]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        rt = self.get_return_type()
        total = state[0]
        if rt.elements[0].is_integer():  # type: ignore[attr-defined]
            total = wrap_int_to_type(int(total), rt.elements[0])  # type: ignore[attr-defined]
        column.append((total, state[1]))

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


class AggregateFunctionSumMap(IAggregateFunction):
    """``sumMap`` —— 按 key 分组求和,返回 ``Map(K, V)``。

    .. code-block:: sql

       SELECT sumMap(keys, values) FROM t;
       -- 两种输入形态都支持:
       --   ① sumMap(Array(K), Array(V))  两列数组
       --   ② sumMap(Map(K, V))           一列 Map

    结果按 **key 升序**排列 —— 这既是真实 ClickHouse 的行为,
    也让序列化天然确定性(不必额外排序)。
    """

    def _key_value_types(self) -> Tuple[DataType, DataType]:
        if len(self.arg_types) >= 2:
            kt = self.arg_types[0].element  # type: ignore[attr-defined]
            vt = self.arg_types[1].element  # type: ignore[attr-defined]
        elif len(self.arg_types) == 1:
            kt = self.arg_types[0].key  # type: ignore[attr-defined]
            vt = self.arg_types[0].value  # type: ignore[attr-defined]
        else:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "sumMap requires (keys, values) or a single Map argument",
            )
        return kt, vt

    def get_return_type(self) -> DataType:
        kt, vt = self._key_value_types()
        return DataTypeMap(kt, sum_result_type(vt))

    def create_state(self) -> Dict[Any, Any]:
        return {}

    def add(self, state: Dict[Any, Any], columns: Sequence[Any], row: int) -> None:
        if len(self.arg_types) >= 2:
            keys = columns[0].get(row)
            values = columns[1].get(row)
            if keys is None or values is None:
                return
            for k, v in zip(keys, values):
                if v is None:
                    continue
                state[k] = state.get(k, 0) + v
        else:
            mapping = columns[0].get(row)
            if not mapping:
                return
            for k, v in mapping.items():
                if v is None:
                    continue
                state[k] = state.get(k, 0) + v

    def merge(self, dst_state: Dict[Any, Any], src_state: Dict[Any, Any]) -> None:
        for k, v in src_state.items():
            dst_state[k] = dst_state.get(k, 0) + v

    def insert_result_into(self, state: Dict[Any, Any], column: Any) -> None:
        column.append(dict(sorted(state.items(), key=lambda kv: _sort_key(kv[0]))))

    def serialize_state(self, state: Dict[Any, Any]) -> bytes:
        w = ByteWriter()
        items = sorted(state.items(), key=lambda kv: _sort_key(kv[0]))
        w.uvarint(len(items))
        for k, v in items:
            kw = ByteWriter()
            encode_key_value(kw, k)
            kb = kw.bytes()
            w.uvarint(len(kb))
            w.raw(kb)
            w.number(v)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Dict[Any, Any]:
        r = ByteReader(data)
        n = r.uvarint()
        out: Dict[Any, Any] = {}
        for _ in range(n):
            kb_len = r.uvarint()
            key = _decode_key(r.raw(kb_len))
            out[key] = r.number()
        return out


def _sort_key(value: Any) -> Any:
    """``sumMap`` 结果的排序键:**数值按数值序**,其余按文本序。

    直接用 ``repr`` 会把 key 一律当字符串比较,于是 ``10`` 排在 ``2`` 前面
    (``'10' < '2'``)—— 这与真实 ClickHouse 以及本类的 docstring 要求的
    "按 key 升序"不符。

    返回值是 ``(组号, 排序值)`` 二元组:组 0 是数值(彼此可比较),
    组 1 是文本。两组不会互相比较数值,所以既给出正确的数值序,又不会
    在 ``int`` 与 ``str`` 混排时抛 ``TypeError``。
    """
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, int):
        return (0, value)
    if isinstance(value, float):
        return (0, value)
    if isinstance(value, (str, bytes, bytearray)):
        return (1, str(value))
    # Decimal / Date 之类:能转成数值就按数值序,否则退回文本序
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _decode_key(data: bytes) -> Any:
    """把 :func:`encode_key_value` 写下的字节解回一个值。"""
    return decode_key_value(ByteReader(data))


register_aggregate("sum", AggregateFunctionSum, description="求和:小整数会自动提升到 64 位类型")
register_aggregate(
    "sumWithOverflow",
    AggregateFunctionSumWithOverflow,
    description="求和:结果类型与输入相同,溢出时回绕(不提升)",
)
register_aggregate("sumKahan", AggregateFunctionSumKahan, description="Kahan 补偿求和:抵消浮点误差累积")
register_aggregate("sumCount", AggregateFunctionSumCount, description="同时返回 (和, 计数) 的元组")
register_aggregate("sumMap", AggregateFunctionSumMap, description="按 key 分组求和,返回有序 Map")
