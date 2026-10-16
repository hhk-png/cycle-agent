"""``uniq`` 家族 —— 去重计数,以及"用近似换内存"的全部权衡。

去重计数("这一列有多少个不同的值")是数据分析里出现频率最高的聚合之一,
也是**精确解最贵**的聚合之一:精确解必须记住每一个不同的值,
内存占用随基数 **线性** 增长。一列 10 亿个不同的 user_id,
精确去重就是 10 亿个键 —— 这就是 HyperLogLog 存在的理由。

.. code-block:: text

   精确                    近似
   ┌────────────────┐      ┌──────────────────────────────┐
   │ uniqExact      │      │ uniq / uniqHLL12 / uniqCombined │
   │ = 一个键集合    │      │ = 一个 HyperLogLog 草图        │
   │ 内存 O(基数)   │      │ 内存 O(2^精度) —— **与基数无关** │
   │ 结果永远精确    │      │ 误差 ~1.6%/√(2^精度)           │
   └────────────────┘      └──────────────────────────────┘

**HyperLogLog 的核心思想**(一句话版):用一个随机哈希把每个值映射到
``2^p`` 个桶之一,记录每个桶里"哈希值前导零最多有多少个"。
"前导零很长"是稀有事件,稀有程度可以直接反推"大概有多少个不同的值":
m 个桶、每个桶最长前导零平均是 log2(n/m) 时,n 就是这个量级。

.. code-block:: text

    哈希 = 0b ....xxxx  ┌── p 位当桶号 ──┐
                       │                  │
    桶号 = 低 p 位        剩下的位用来数"前导(尾随)零"
    桶里存 = 见过的最长零串 + 1

    估计值 = α_m · m² / Σ 2^(-桶[i])
             └ 调和平均:桶里值越大,贡献越小 —— 一个"很稀疏"的桶
               就能显著拉高估计,这正是"看见稀有事 ⇒ 基数大"的数学化

**为什么 ``uniqExact`` 和 ``uniq`` 会在小基数上分歧?**

这是 ClickHouse 里一条著名的"反直觉"note:文档明说
"``uniq`` 有时会返回比真实值略大的数,例如只有 2 个不同值时返回 3"。

原因在于:HyperLogLog 的估计值是一个**连续量的舍入**。当真实基数是 k 时,
估计值的**绝对误差**大致是 ``±1.04/√m · k`` 加上一个与 m 相关的常数项;
对非常小的 k,这个误差的**绝对值**可以到 ±1 甚至 ±0.5 量级,
于是 ``round(2.6) = 3``。换句话说:

.. code-block:: text

   真实 2 个值 → 估计 2.4 → 四舍五入 2   (碰巧对)
   真实 2 个值 → 估计 2.6 → 四舍五入 3   (文档里那条"返回 3")
   真实 3 个值 → 估计 2.7 → 四舍五入 3   (也可能偏小)

.. warning::

   **mini-clickhouse 在这一族上比真实 ClickHouse 更"准"一点**,
   所以上面那条"2 个值返回 3"的现象在这里**通常不会复现**。原因是我们
   用了教科书里的 **linear counting 小基数修正**:当估计值小于 ``2.5m``
   时改用 ``m · ln(m / 空桶数)`` —— 这个公式在小基数下几乎是精确的,
   于是 ``uniq`` 与 ``uniqExact`` 会在基数到**数千**之后才出现可见分歧。
   我们选择正确的 HLL 而不是复刻 ClickHouse 的偏差,是因为教程的重点是
   "为什么会有误差",而不是"误差必须一模一样"。想亲眼看分歧,
   把精度调到 ``uniq(12)`` 再喂几万个不同值即可。

序列化:HLL 的状态是 ``2^p`` 个**一字节**寄存器,所以
``serialize_state`` 就是 ``[varint(p)][m 字节寄存器]`` —— 紧凑、确定、
往返精确。
"""

from __future__ import annotations

import math
from typing import Any, List, Sequence, Set

from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..util.hashing import siphash64
from .base import ByteReader, ByteWriter, IAggregateFunction, key_bytes, register_aggregate

__all__ = [
    "HyperLogLog",
    "AggregateFunctionUniq",
    "AggregateFunctionUniqExact",
    "AggregateFunctionUniqCombined",
    "AggregateFunctionUniqHLL12",
    "AggregateFunctionUniqUpTo",
]

#: 允许的 HLL 精度范围(与真实 ClickHouse 一致:12–20)
MIN_PRECISION = 12
MAX_PRECISION = 20
DEFAULT_PRECISION = 14


class HyperLogLog:
    """标准 HyperLogLog 草图 —— 手动实现,不是库调用。

    寄存器用 ``bytearray`` 存(每个桶 1 字节),因为 64 位哈希下
    单桶最大计数值是 ``64 - p + 1 ≤ 53``,一个字节绰绰有余。
    """

    __slots__ = ("p", "m", "registers")

    def __init__(self, precision: int = DEFAULT_PRECISION) -> None:
        if precision < MIN_PRECISION or precision > MAX_PRECISION:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"HyperLogLog precision must be in [{MIN_PRECISION}, {MAX_PRECISION}], "
                f"got {precision}",
            )
        self.p = precision
        self.m = 1 << precision
        self.registers = bytearray(self.m)

    # ── 喂数据 ──────────────────────────────────────────────────────────

    def add_hash(self, h: int) -> None:
        h &= (1 << 64) - 1
        idx = h & (self.m - 1)  # 低 p 位当桶号
        w = h >> self.p  # 剩下的位用来数零
        rank = 1
        max_rank = 64 - self.p + 1
        while rank <= max_rank and (w & 1) == 0:
            rank += 1
            w >>= 1
        if rank > self.registers[idx]:
            self.registers[idx] = rank

    def merge(self, other: "HyperLogLog") -> None:
        """两个草图合并 = **逐桶取最大** —— 这就是 HLL 可分布式的全部秘密。"""
        if other.p != self.p:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot merge HyperLogLog with different precisions: "
                f"{self.p} vs {other.p}",
            )
        for i in range(self.m):
            if other.registers[i] > self.registers[i]:
                self.registers[i] = other.registers[i]

    # ── 估计 ────────────────────────────────────────────────────────────

    def estimate(self) -> int:
        m = self.m
        zeros = 0
        inv_sum = 0.0
        for r in self.registers:
            inv_sum += 2.0 ** (-r)
            if r == 0:
                zeros += 1
        alpha = 0.7213 / (1.0 + 1.079 / m)
        raw = alpha * m * m / inv_sum
        if raw <= 2.5 * m and zeros > 0:
            # linear counting —— 小基数修正(见模块 docstring 的说明)
            raw = m * math.log(m / zeros)
        if zeros == m:
            return 0
        return int(round(raw))

    def clone(self) -> "HyperLogLog":
        out = HyperLogLog(self.p)
        out.registers = bytearray(self.registers)
        return out


def _hash_key(values: Sequence[Any]) -> int:
    """把一行参数值哈希成 64 位(确定性)。"""
    return siphash64(key_bytes(values))


def _row_has_null(values: Sequence[Any]) -> bool:
    """``uniq`` 整族的 NULL 规则:**任一实参为 NULL ⇒ 整行不计**。

    真实 ClickHouse 里给可空实参套的是 ``Null`` 组合器,它在编码键之前
    就把含 NULL 的行丢掉。所以 ``uniq(NULL, NULL) = 0``、
    ``uniqExact(1, NULL) = 0``(这些行**不算一个"不同的值"**)。
    早先的实现直接对值做 ``key_bytes``,于是 NULL 被当成了一个普通的不同值。
    """
    return any(v is None for v in values)


class _UniqHLLBase(IAggregateFunction):
    """所有"HLL 型 uniq"的公共实现(默认精度由子类指定)。"""

    _fixed_precision: int = DEFAULT_PRECISION

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        if params and isinstance(params[0], (int, float)):
            precision = int(params[0])
        else:
            precision = self._fixed_precision
        if precision < MIN_PRECISION or precision > MAX_PRECISION:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"{name}: precision must be in [{MIN_PRECISION}, {MAX_PRECISION}], got {precision}",
            )
        self.precision = precision

    def get_return_type(self) -> DataType:
        return get_data_type("UInt64")

    def create_state(self) -> HyperLogLog:
        return HyperLogLog(self.precision)

    def add(self, state: HyperLogLog, columns: Sequence[Any], row: int) -> None:
        values = [columns[i].get(row) for i in range(len(self.arg_types))]
        if _row_has_null(values):
            return
        state.add_hash(_hash_key(values))

    def merge(self, dst_state: HyperLogLog, src_state: HyperLogLog) -> None:
        dst_state.merge(src_state)

    def insert_result_into(self, state: HyperLogLog, column: Any) -> None:
        column.append(state.estimate())

    def serialize_state(self, state: HyperLogLog) -> bytes:
        w = ByteWriter()
        w.uvarint(state.p)
        w.raw(bytes(state.registers))
        return w.bytes()

    def deserialize_state(self, data: bytes) -> HyperLogLog:
        r = ByteReader(data)
        p = r.uvarint()
        hll = HyperLogLog(p)
        hll.registers = bytearray(r.raw(hll.m))
        return hll


class AggregateFunctionUniq(_UniqHLLBase):
    """``uniq(x[, ...])`` —— HyperLogLog 近似去重计数(默认精度 14)。

    精度可以显式给:``uniq(20)(x)`` 更准但更占内存(``2^20`` 个桶);
    ``uniq(12)(x)`` 更省但更糙。误差约 ``1.6% / √(2^p)``。
    """

    _fixed_precision = DEFAULT_PRECISION


class AggregateFunctionUniqHLL12(_UniqHLLBase):
    """``uniqHLL12`` —— 固定精度 12 的 HLL(兼容老版本的名字)。"""

    _fixed_precision = 12


class AggregateFunctionUniqExact(IAggregateFunction):
    """``uniqExact`` —— **精确**去重计数:状态就是一个键集合。

    永远正确,内存随基数线性增长。基数小到几十万以内时它是首选;
    再大就该换 ``uniq``。状态序列化时**对键排序**,所以结果是确定性的。
    """

    def get_return_type(self) -> DataType:
        return get_data_type("UInt64")

    def create_state(self) -> Set[bytes]:
        return set()

    def add(self, state: Set[bytes], columns: Sequence[Any], row: int) -> None:
        values = [columns[i].get(row) for i in range(len(self.arg_types))]
        if _row_has_null(values):
            return
        state.add(key_bytes(values))

    def merge(self, dst_state: Set[bytes], src_state: Set[bytes]) -> None:
        dst_state |= src_state

    def insert_result_into(self, state: Set[bytes], column: Any) -> None:
        column.append(len(state))

    def serialize_state(self, state: Set[bytes]) -> bytes:
        w = ByteWriter()
        keys = sorted(state)
        w.uvarint(len(keys))
        for k in keys:
            w.uvarint(len(k))
            w.raw(k)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Set[bytes]:
        r = ByteReader(data)
        n = r.uvarint()
        out: Set[bytes] = set()
        for _ in range(n):
            length = r.uvarint()
            out.add(r.raw(length))
        return out


class AggregateFunctionUniqCombined(IAggregateFunction):
    """``uniqCombined`` —— **小基数精确、大基数近似**的组合策略。

    这其实就是真实 ClickHouse 里 ``uniq`` 背后的做法:一个分组一开始用
    精确的键集合(便宜且准),集合超过阈值后才**退化**成 HLL。
    用生产里的话说 —— "能精确就精确,不能精确才近似"。

    这里阈值取 1000;真实 CH 内部也是类似的"两者取长补短"。
    """

    #: 超过这个基数就切换到 HLL
    EXACT_THRESHOLD = 1000

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.precision = int(params[0]) if params and isinstance(params[0], (int, float)) \
            else DEFAULT_PRECISION

    def get_return_type(self) -> DataType:
        return get_data_type("UInt64")

    def create_state(self) -> dict:
        return {"exact": set(), "hll": None, "exact_mode": True}

    def _promote(self, state: dict) -> None:
        hll = HyperLogLog(self.precision)
        for k in state["exact"]:
            hll.add_hash(siphash64(k))
        state["hll"] = hll
        state["exact_mode"] = False

    def add(self, state: dict, columns: Sequence[Any], row: int) -> None:
        values = [columns[i].get(row) for i in range(len(self.arg_types))]
        if _row_has_null(values):
            return
        k = key_bytes(values)
        if state["exact_mode"]:
            state["exact"].add(k)
            if len(state["exact"]) > self.EXACT_THRESHOLD:
                self._promote(state)
        else:
            state["hll"].add_hash(siphash64(k))

    def merge(self, dst_state: dict, src_state: dict) -> None:
        if not src_state["exact_mode"]:
            if dst_state["exact_mode"]:
                self._promote(dst_state)
            dst_state["hll"].merge(src_state["hll"])
            return
        if dst_state["exact_mode"]:
            dst_state["exact"] |= src_state["exact"]
            if len(dst_state["exact"]) > self.EXACT_THRESHOLD:
                self._promote(dst_state)
            return
        for k in src_state["exact"]:
            dst_state["hll"].add_hash(siphash64(k))

    def insert_result_into(self, state: dict, column: Any) -> None:
        if state["exact_mode"]:
            column.append(len(state["exact"]))
        else:
            column.append(state["hll"].estimate())

    def serialize_state(self, state: dict) -> bytes:
        w = ByteWriter()
        if state["exact_mode"]:
            w.raw(b"\x00")
            keys = sorted(state["exact"])
            w.uvarint(len(keys))
            for k in keys:
                w.uvarint(len(k))
                w.raw(k)
        else:
            w.raw(b"\x01")
            w.uvarint(state["hll"].p)
            w.raw(bytes(state["hll"].registers))
        return w.bytes()

    def deserialize_state(self, data: bytes) -> dict:
        r = ByteReader(data)
        mode = r.raw(1)
        if mode == b"\x00":
            n = r.uvarint()
            exact = set()
            for _ in range(n):
                length = r.uvarint()
                exact.add(r.raw(length))
            return {"exact": exact, "hll": None, "exact_mode": True}
        p = r.uvarint()
        hll = HyperLogLog(p)
        hll.registers = bytearray(r.raw(hll.m))
        return {"exact": set(), "hll": hll, "exact_mode": False}


class AggregateFunctionUniqUpTo(IAggregateFunction):
    """``uniqUpTo(N)(x)`` —— 精确计数,但**数到 N 就够**。

    真实语义:如果不同值的个数 ``≤ N`` 就返回精确值;否则返回 ``N + 1``
    ("超过 N 了,具体多少我不关心")。它比 ``uniqExact`` 省内存的地方在于:
    一旦超过 N 就可以**丢掉所有已记录的键**,只保留一个"超了"的标记。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.n = int(params[0]) if params else 5

    def get_return_type(self) -> DataType:
        return get_data_type("UInt64")

    def create_state(self) -> dict:
        return {"keys": set(), "exceeded": False}

    def add(self, state: dict, columns: Sequence[Any], row: int) -> None:
        if state["exceeded"]:
            return
        values = [columns[i].get(row) for i in range(len(self.arg_types))]
        if _row_has_null(values):
            return
        state["keys"].add(key_bytes(values))
        if len(state["keys"]) > self.n:
            state["exceeded"] = True
            state["keys"] = set()  # 丢掉明细,省内存

    def merge(self, dst_state: dict, src_state: dict) -> None:
        if dst_state["exceeded"] or src_state["exceeded"]:
            dst_state["exceeded"] = True
            dst_state["keys"] = set()
            return
        dst_state["keys"] |= src_state["keys"]
        if len(dst_state["keys"]) > self.n:
            dst_state["exceeded"] = True
            dst_state["keys"] = set()

    def insert_result_into(self, state: dict, column: Any) -> None:
        column.append(self.n + 1 if state["exceeded"] else len(state["keys"]))

    def serialize_state(self, state: dict) -> bytes:
        w = ByteWriter()
        w.raw(b"\x01" if state["exceeded"] else b"\x00")
        keys = sorted(state["keys"])
        w.uvarint(len(keys))
        for k in keys:
            w.uvarint(len(k))
            w.raw(k)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> dict:
        r = ByteReader(data)
        exceeded = r.raw(1) == b"\x01"
        n = r.uvarint()
        keys = set()
        for _ in range(n):
            length = r.uvarint()
            keys.add(r.raw(length))
        return {"keys": keys, "exceeded": exceeded}


register_aggregate(
    "uniq",
    AggregateFunctionUniq,
    description="HyperLogLog 近似去重计数(默认精度 14,可用 uniq(p) 指定)",
)
register_aggregate("uniqExact", AggregateFunctionUniqExact, description="精确去重计数(内存随基数线性增长)")
register_aggregate(
    "uniqCombined",
    AggregateFunctionUniqCombined,
    description="小基数精确、大基数近似的组合去重计数",
)
register_aggregate("uniqHLL12", AggregateFunctionUniqHLL12, description="固定精度 12 的 HLL 去重计数")
register_aggregate("uniqUpTo", AggregateFunctionUniqUpTo, description="数到 N 就够的去重计数(超过则返回 N+1)")
