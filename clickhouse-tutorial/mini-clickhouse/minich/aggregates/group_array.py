"""``groupArray`` 家族 —— 把一组值收集成一个数组,以及几种"有节制地收集"。

.. code-block:: text

   groupArray(x)              收集所有值   → Array(T)
   groupUniqArray(x)          收集去重后的值
   groupArrayLast(N)(x)       只保留**最后** N 个
   groupArraySample(N)(x)     只保留 N 个(真实实现是蓄水池采样)
   groupArrayInsertAt(d, p)(x) 按位置 p 插入 x,空位填默认值 d
   topK(N)(x)                 出现次数最多的 N 个值

这是一个**代价很高**的聚合家族,值得单独理解:

.. code-block:: text

   sum 的状态是 1 个数    → 合并两份状态是 O(1)
   groupArray 的状态是数组 → 合并两份状态是 O(n),而且要**复制**

所以官方文档反复强调:``groupArray`` 之后接 ``arrayJoin`` 是
"先把行聚起来再拆开",如果只是想把数组展开,**直接用 ``arrayJoin``
读原始列**往往更快(第 16 章会用数据说明)。但当"分组后的集合"
确实是你要的输出时(比如"每个用户看过的所有页面"),它无可替代。

.. warning::

   ``groupArraySample`` 在这里是**简化实现**:它保留**前 N 个**值,
   不做随机采样(因此 ``seed`` 参数被忽略)。真实实现用蓄水池采样,
   保证结果**随机且与输入顺序无关**。之所以简化,是因为随机采样会让
   状态里多带一个 RNG,序列化与"往返精确"都会变得别扭 —— 而这正是
   本模块要演示的主线。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from ..errors import ErrorCode, raise_error
from ..types import DataType
from ..types.composite import DataTypeArray
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    decode_key_value,
    encode_key_value,
    key_bytes,
    register_aggregate,
)

__all__ = [
    "AggregateFunctionGroupArray",
    "AggregateFunctionGroupUniqArray",
    "AggregateFunctionGroupArrayLast",
    "AggregateFunctionGroupArraySample",
    "AggregateFunctionGroupArrayInsertAt",
    "AggregateFunctionTopK",
]


def _write_values(w: ByteWriter, values: Sequence[Any]) -> None:
    w.uvarint(len(values))
    for v in values:
        encode_key_value(w, v)


def _read_values(r: ByteReader) -> List[Any]:
    n = r.uvarint()
    return [decode_key_value(r) for _ in range(n)]


def _arg0(arg_types: Sequence[DataType], name: str) -> DataType:
    if not arg_types:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Aggregate function {name} requires at least one argument",
        )
    return arg_types[0]


class AggregateFunctionGroupArray(IAggregateFunction):
    """``groupArray(x)`` —— 收集所有(非 NULL 之外的)值成一个数组。"""

    def get_return_type(self) -> DataType:
        return DataTypeArray(_arg0(self.arg_types, self.name))

    def create_state(self) -> List[Any]:
        return []

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        state.append(columns[0].get(row))

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        dst_state.extend(src_state)

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        column.append(list(state))

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        _write_values(w, state)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        return _read_values(ByteReader(data))


class AggregateFunctionGroupUniqArray(IAggregateFunction):
    """``groupUniqArray([N])(x)`` —— 去重后收集(可选上限 ``N``)。

    状态用 ``dict``(键 = 编码后的值,值 = 原值)而不是 ``set``,
    因为去重之后还得把**原值**吐出来。字典在 Python 里保序,
    但序列化时我们**按键排序**,所以结果顺序是确定的。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.limit = int(params[0]) if params else 0  # 0 = 不限

    def get_return_type(self) -> DataType:
        return DataTypeArray(_arg0(self.arg_types, self.name))

    def create_state(self) -> Dict[bytes, Any]:
        return {}

    def add(self, state: Dict[bytes, Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        k = key_bytes((value,))
        if k not in state:
            if self.limit and len(state) >= self.limit:
                return
            state[k] = value

    def merge(self, dst_state: Dict[bytes, Any], src_state: Dict[bytes, Any]) -> None:
        for k, v in src_state.items():
            if self.limit and len(dst_state) >= self.limit and k not in dst_state:
                continue
            dst_state.setdefault(k, v)

    def insert_result_into(self, state: Dict[bytes, Any], column: Any) -> None:
        column.append([state[k] for k in sorted(state)])

    def serialize_state(self, state: Dict[bytes, Any]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for k in sorted(state):
            w.uvarint(len(k))
            w.raw(k)
            encode_key_value(w, state[k])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Dict[bytes, Any]:
        r = ByteReader(data)
        n = r.uvarint()
        out: Dict[bytes, Any] = {}
        for _ in range(n):
            klen = r.uvarint()
            k = r.raw(klen)
            out[k] = decode_key_value(r)
        return out


class AggregateFunctionGroupArrayLast(IAggregateFunction):
    """``groupArrayLast(N)(x)`` —— 只保留**最后** ``N`` 个值。"""

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.n = int(params[0]) if params else 1

    def get_return_type(self) -> DataType:
        return DataTypeArray(_arg0(self.arg_types, self.name))

    def create_state(self) -> List[Any]:
        return []

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        state.append(columns[0].get(row))
        if len(state) > self.n:
            del state[0]  # 超上限就丢最老的

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        dst_state.extend(src_state)
        if len(dst_state) > self.n:
            del dst_state[: len(dst_state) - self.n]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        column.append(list(state))

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        _write_values(w, state)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        return _read_values(ByteReader(data))


class AggregateFunctionGroupArraySample(IAggregateFunction):
    """``groupArraySample(N[, seed])(x)`` —— 保留 ``N`` 个值。

    .. warning::

       本实现保留**前 ``N`` 个**值,不做随机采样(``seed`` 被忽略)。
       见模块 docstring 的说明。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.n = int(params[0]) if params else 10
        #: 忽略 —— 保留字段以对齐真实签名
        self.seed = params[1] if len(params) > 1 else 0

    def get_return_type(self) -> DataType:
        return DataTypeArray(_arg0(self.arg_types, self.name))

    def create_state(self) -> List[Any]:
        return []

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        if len(state) < self.n:
            state.append(columns[0].get(row))

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        for v in src_state:
            if len(dst_state) >= self.n:
                break
            dst_state.append(v)

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        column.append(list(state))

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        _write_values(w, state)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        return _read_values(ByteReader(data))


class AggregateFunctionGroupArrayInsertAt(IAggregateFunction):
    """``groupArrayInsertAt(default, pos)(x)`` —— 按位置插入。

    每个 ``(x, pos)`` 把 ``x`` 写进数组的第 ``pos`` 个槽;没被写到的槽
    保持默认值 ``default``。用于"把稀疏的位置编码补成稠密数组"。

    状态里存的是**逐行的 ``(值, 位置)`` 条目**(位置为 ``None`` 表示
    "按出现顺序追加"),而不是一个已物化的数组 —— 因为 ``merge`` 必须等价于
    "先把目的分片的行喂进来、再把源分片的行喂进来"。物化发生在
    :meth:`insert_result_into`。

    .. warning:: 同一个位置被写多次时,mini-clickhouse 用**后写者覆盖**
       (见 :meth:`insert_result_into`)。真实 ClickHouse 文档说单线程下
       "**先**写入的值胜出"。这条差异记录在教程里。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.default = params[0] if params else 0

    def get_return_type(self) -> DataType:
        return DataTypeArray(_arg0(self.arg_types, self.name))

    def create_state(self) -> List[Any]:
        return []

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        pos: Any = None
        if len(columns) >= 2 and columns[1].get(row) is not None:
            pos = int(columns[1].get(row))
        # 没给位置就标记成 None —— 物化时才用"当时的数组末尾"当位置
        if pos is not None and pos < 0:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"groupArrayInsertAt: negative position {pos}",
            )
        state.append((value, pos))

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        # 把源的行**接在**目的行之后 —— 等价于"先喂 dst 的行,再喂 src 的行"。
        # 旧实现用"值是否等于默认值"猜空槽,既会丢追加模式的值,又会被
        # "恰好等于默认值的真实数据"骗到。
        dst_state.extend(src_state)

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        arr: List[Any] = []
        for value, pos in state:
            if pos is None:
                pos = len(arr)  # 追加模式:放到当前末尾
            while len(arr) <= pos:
                arr.append(self.default)
            arr[pos] = value  # 同一位置重复写入 → 后写者覆盖
        column.append(arr)

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for value, pos in state:
            encode_key_value(w, value)
            if pos is None:
                w.raw(b"\x00")
            else:
                w.raw(b"\x01")
                w.uvarint(pos)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        r = ByteReader(data)
        n = r.uvarint()
        out: List[Any] = []
        for _ in range(n):
            value = decode_key_value(r)
            pos = None if r.raw(1) == b"\x00" else r.uvarint()
            out.append((value, pos))
        return out


class AggregateFunctionTopK(IAggregateFunction):
    """``topK(N)(x)`` —— 出现次数最多的 ``N`` 个值。

    真实 ClickHouse 用 Space-Saving / Filtered-Space-Saving 这类
    **近似**算法(内存 O(N) 而与基数无关)。mini-clickhouse 用精确计数字典,
    结果永远正确 —— 见 :class:`~minich.aggregates.any.AggregateFunctionAnyHeavy`
    的同类说明。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.n = int(params[0]) if params else 10
        #: topKWeighted 用:第二个参数是权重
        self.weighted = len(arg_types) >= 2

    def get_return_type(self) -> DataType:
        return DataTypeArray(_arg0(self.arg_types, self.name))

    def create_state(self) -> Dict[bytes, List[Any]]:
        return {}  # key_bytes → [计数, 原值]

    def add(self, state: Dict[bytes, List[Any]], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        weight = 1
        if self.weighted:
            w = columns[1].get(row)
            if w is None:
                return
            weight = w
        k = key_bytes((value,))
        entry = state.get(k)
        if entry is None:
            state[k] = [weight, value]
        else:
            entry[0] += weight

    def merge(self, dst_state: Dict[bytes, List[Any]], src_state: Dict[bytes, List[Any]]) -> None:
        for k, (count, value) in src_state.items():
            entry = dst_state.get(k)
            if entry is None:
                dst_state[k] = [count, value]
            else:
                entry[0] += count

    def insert_result_into(self, state: Dict[bytes, List[Any]], column: Any) -> None:
        # 按计数降序;并列时按 key 字节序,保证确定性
        ordered = sorted(state, key=lambda k: (-state[k][0], k))
        column.append([state[k][1] for k in ordered[: self.n]])

    def serialize_state(self, state: Dict[bytes, List[Any]]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for k in sorted(state):
            count, value = state[k]
            w.uvarint(len(k))
            w.raw(k)
            w.number(count)
            encode_key_value(w, value)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Dict[bytes, List[Any]]:
        r = ByteReader(data)
        n = r.uvarint()
        out: Dict[bytes, List[Any]] = {}
        for _ in range(n):
            klen = r.uvarint()
            k = r.raw(klen)
            count = r.number()
            value = decode_key_value(r)
            out[k] = [count, value]
        return out


register_aggregate("groupArray", AggregateFunctionGroupArray, description="收集所有值成一个数组")
register_aggregate(
    "groupUniqArray",
    AggregateFunctionGroupUniqArray,
    description="去重后收集成一个数组(可用 groupUniqArray(N) 限制大小)",
)
register_aggregate("groupArrayLast", AggregateFunctionGroupArrayLast, description="只保留最后 N 个值组成数组")
register_aggregate(
    "groupArraySample",
    AggregateFunctionGroupArraySample,
    description="保留 N 个值(简化实现:取前 N 个,不做随机采样)",
)
register_aggregate(
    "groupArrayInsertAt",
    AggregateFunctionGroupArrayInsertAt,
    description="按位置把值插入数组,空位填默认值",
)
register_aggregate("topK", AggregateFunctionTopK, description="出现次数最多的 N 个值")
register_aggregate(
    "topKWeighted",
    AggregateFunctionTopK,
    description="按权重计数的 topN(第二个参数是权重)",
)
