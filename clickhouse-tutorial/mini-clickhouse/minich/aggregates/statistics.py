"""统计聚合 —— 方差、协方差、相关性、偏度峰度、熵、直方图、位运算。

这一族函数有一个**共同的工程难题**:它们的结果都依赖"所有数据",但
状态必须能被**合并**。朴素的累加式实现(先求和、再求平方和)在数值上
非常危险 —— 方差的经典公式

.. code-block:: text

   Var = E[X²] − (E[X])²

在数据均值很大、方差很小时会**灾难性抵消**:两个几乎相等的大数相减,
有效数字全丢光。所以这里统一用 **Welford 在线算法**:

.. code-block:: text

   n    += 1
   δ     = x − mean
   mean += δ / n
   M2   += δ · (x − mean)        ← M2 就是 Σ(x−mean)²,一次都不碰 x²

它逐行更新、数值稳定,而且**可合并**(两份状态能并成一份):

.. code-block:: text

   n     = n₁ + n₂
   δ     = mean₂ − mean₁
   mean  = mean₁ + δ · n₂ / n
   M2    = M2₁ + M2₂ + δ² · n₁ · n₂ / n

.. code-block:: text

   函数              返回               状态
   varPop/varSamp    方差               [n, mean, M2]
   stddevPop/Samp    标准差             [n, mean, M2](最后开方)
   covarPop/covarSamp 协方差            [n, Σx, Σy, Σxx, Σyy, Σxy]
   corr              皮尔逊相关系数     同上
   skew/kurt         偏度 / 峰度         [n, Σx, Σx², Σx³, Σx⁴]
   entropy(x)        香农熵(比特)      {值: 计数}
   histogram(N)(x)   等宽直方图          [值...](本实现保留全部值)

.. warning::

   **本模块的简化之处**(均在对应类的文档里标注):

   * ``covar`` / ``corr`` / ``skew`` / ``kurt`` 用**原始幂和**实现,
     没有做 Welford 式的在线中心矩 —— 对"数值量级极端"的数据会有
     精度损失。方差本身用的是稳定的 Welford,不受影响。
   * ``histogram`` 保留全部值再分桶(内存 O(n));真实实现增量维护。
   * ``sequenceCount`` 假设行**按时间到达**(真实实现会自己排序)。
   * ``maxIntersections`` **未实现**,调用 ``create_state`` 会明确抛错。
   * ``rankCorr`` 保留全部配对再算秩(内存 O(n));真实实现之一也是 O(n),
     但会做抽样。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.composite import DataTypeArray, DataTypeNullable, DataTypeTuple
from ..types.scalars import DataTypeNothing
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    decode_key_value,
    key_bytes,
    register_aggregate,
)

__all__ = [
    "AggregateFunctionVariance",
    "AggregateFunctionCovariance",
    "AggregateFunctionCorrelation",
    "AggregateFunctionMoment",
    "AggregateFunctionEntropy",
    "AggregateFunctionHistogram",
    "AggregateFunctionSequenceCount",
    "AggregateFunctionGroupBit",
    "AggregateFunctionMaxIntersections",
    "AggregateFunctionRankCorr",
    "AggregateFunctionNothing",
]

_MASK64 = (1 << 64) - 1


def _float64() -> DataType:
    return get_data_type("Float64")


def _need_args(name: str, arg_types: Sequence[DataType], n: int) -> None:
    if len(arg_types) < n:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Aggregate function {name} requires at least {n} argument(s), got {len(arg_types)}",
        )


# ══════════════════════════════════════════════════════════════════════════
# 方差 / 标准差 —— Welford 在线算法
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionVariance(IAggregateFunction):
    """``varPop`` / ``varSamp`` / ``stddevPop`` / ``stddevSamp`` 的统一实现。

    ``Pop`` = 总体(除以 ``n``),``Samp`` = 样本(除以 ``n-1``);
    ``stddev`` 只是对方差开个方。
    """

    def __init__(
        self,
        name: str,
        arg_types: Sequence[DataType],
        params: Sequence[Any] = (),
        *,
        sample: bool = False,
        sqrt: bool = False,
    ) -> None:
        super().__init__(name, arg_types, params)
        _need_args(name, arg_types, 1)
        self.sample = sample  # True → 除以 n-1
        self.sqrt = sqrt  # True → 最后开方(stddev)

    def get_return_type(self) -> DataType:
        return _float64()

    def create_state(self) -> List[float]:
        return [0, 0.0, 0.0]  # [n, mean, M2]

    def add(self, state: List[float], columns: Sequence[Any], row: int) -> None:
        x = columns[0].get(row)
        if x is None:
            return
        x = float(x)
        state[0] += 1
        delta = x - state[1]
        state[1] += delta / state[0]
        state[2] += delta * (x - state[1])

    def merge(self, dst_state: List[float], src_state: List[float]) -> None:
        n1, mean1, m2_1 = dst_state
        n2, mean2, m2_2 = src_state
        if n2 == 0:
            return
        n = n1 + n2
        delta = mean2 - mean1
        dst_state[0] = n
        dst_state[1] = mean1 + delta * n2 / n
        dst_state[2] = m2_1 + m2_2 + delta * delta * n1 * n2 / n

    def _variance(self, state: List[float]) -> float:
        n, _, m2 = state
        if self.sample:
            if n < 2:
                return float("nan")
            return m2 / (n - 1)
        if n < 1:
            return 0.0
        return m2 / n

    def insert_result_into(self, state: List[float], column: Any) -> None:
        v = self._variance(state)
        column.append(math.sqrt(v) if (self.sqrt and not math.isnan(v)) else v)

    def serialize_state(self, state: List[float]) -> bytes:
        w = ByteWriter()
        w.uvarint(state[0])
        w.f64(state[1])
        w.f64(state[2])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[float]:
        r = ByteReader(data)
        return [r.uvarint(), r.f64(), r.f64()]


def _variance_factory(*, sample: bool, sqrt: bool):
    def factory(name: str, params: Sequence[Any], arg_types: Sequence[DataType]) -> IAggregateFunction:
        return AggregateFunctionVariance(name, arg_types, params, sample=sample, sqrt=sqrt)

    return factory


# ══════════════════════════════════════════════════════════════════════════
# 协方差 / 相关系数
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionCovariance(IAggregateFunction):
    """``covarPop`` / ``covarSamp`` / ``corr`` 的统一实现。

    状态是原始和 ``[n, Σx, Σy, Σxx, Σyy, Σxy]``:

    .. code-block:: text

       cov = (Σxy − Σx·Σy/n) / (n 或 n−1)
       corr = cov / (σx · σy)
    """

    def __init__(
        self,
        name: str,
        arg_types: Sequence[DataType],
        params: Sequence[Any] = (),
        *,
        sample: bool = False,
        correlation: bool = False,
    ) -> None:
        super().__init__(name, arg_types, params)
        _need_args(name, arg_types, 2)
        self.sample = sample
        self.correlation = correlation

    def get_return_type(self) -> DataType:
        return _float64()

    def create_state(self) -> List[float]:
        return [0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def add(self, state: List[float], columns: Sequence[Any], row: int) -> None:
        x = columns[0].get(row)
        y = columns[1].get(row)
        if x is None or y is None:
            return
        x, y = float(x), float(y)
        state[0] += 1
        state[1] += x
        state[2] += y
        state[3] += x * x
        state[4] += y * y
        state[5] += x * y

    def merge(self, dst_state: List[float], src_state: List[float]) -> None:
        for i in range(6):
            dst_state[i] += src_state[i]

    def insert_result_into(self, state: List[float], column: Any) -> None:
        n, sx, sy, sxx, syy, sxy = state
        if n == 0:
            column.append(float("nan"))
            return
        cov = (sxy - sx * sy / n) / ((n - 1) if self.sample else n)
        if not self.correlation:
            column.append(cov)
            return
        var_x = sxx - sx * sx / n
        var_y = syy - sy * sy / n
        denom = math.sqrt(var_x * var_y)
        if denom == 0:
            column.append(float("nan"))
        else:
            column.append((sxy - sx * sy / n) / denom)

    def serialize_state(self, state: List[float]) -> bytes:
        w = ByteWriter()
        w.uvarint(state[0])
        for i in range(1, 6):
            w.f64(state[i])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[float]:
        r = ByteReader(data)
        out = [r.uvarint()]
        for _ in range(5):
            out.append(r.f64())
        return out


def _covar_factory(*, sample: bool, correlation: bool):
    def factory(name: str, params: Sequence[Any], arg_types: Sequence[DataType]) -> IAggregateFunction:
        return AggregateFunctionCovariance(name, arg_types, params, sample=sample, correlation=correlation)

    return factory


# ══════════════════════════════════════════════════════════════════════════
# 偏度 / 峰度
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionMoment(IAggregateFunction):
    """``skewPop`` / ``skewSamp`` / ``kurtPop`` / ``kurtSamp`` 的统一实现。

    偏度(三阶)描述分布**左右不对称**程度;峰度(四阶)描述**尾部厚重**程度。
    正态分布的峰度(超额)是 ``0``、偏度是 ``0``。

    状态是原始幂和 ``[n, Σx, Σx², Σx³, Σx⁴]``,用它们还原中心矩:

    .. code-block:: text

       M2 = Σx² − n·μ²
       M3 = Σx³ − 3μ·Σx² + 3μ²·Σx − n·μ³
       M4 = Σx⁴ − 4μ·Σx³ + 6μ²·Σx² − 4μ³·Σx + n·μ⁴
    """

    def __init__(
        self,
        name: str,
        arg_types: Sequence[DataType],
        params: Sequence[Any] = (),
        *,
        order: int = 3,
        sample: bool = False,
    ) -> None:
        super().__init__(name, arg_types, params)
        _need_args(name, arg_types, 1)
        self.order = order  # 3 = 偏度, 4 = 峰度
        self.sample = sample

    def get_return_type(self) -> DataType:
        return _float64()

    def create_state(self) -> List[float]:
        return [0, 0.0, 0.0, 0.0, 0.0]

    def add(self, state: List[float], columns: Sequence[Any], row: int) -> None:
        x = columns[0].get(row)
        if x is None:
            return
        x = float(x)
        state[0] += 1
        state[1] += x
        x2 = x * x
        state[2] += x2
        state[3] += x2 * x
        state[4] += x2 * x2

    def merge(self, dst_state: List[float], src_state: List[float]) -> None:
        for i in range(5):
            dst_state[i] += src_state[i]

    def insert_result_into(self, state: List[float], column: Any) -> None:
        n, s1, s2, s3, s4 = state
        if n == 0:
            column.append(float("nan"))
            return
        mu = s1 / n
        m2 = s2 - n * mu * mu
        if self.order == 3:
            m3 = s3 - 3 * mu * s2 + 3 * mu * mu * s1 - n * mu ** 3
            var = m2 / n
            if var == 0:
                column.append(float("nan"))
                return
            g1 = (m3 / n) / (var ** 1.5)
            if self.sample:
                if n < 3:
                    column.append(float("nan"))
                    return
                g1 = math.sqrt(n * (n - 1)) / (n - 2) * g1
            column.append(g1)
        else:
            m4 = s4 - 4 * mu * s3 + 6 * mu * mu * s2 - 4 * mu ** 3 * s1 + n * mu ** 4
            var = m2 / n
            if var == 0:
                column.append(float("nan"))
                return
            g2 = (m4 / n) / (var * var) - 3.0
            if self.sample:
                if n < 4:
                    column.append(float("nan"))
                    return
                g2 = ((n + 1) * g2 + 6) * (n - 1) / ((n - 2) * (n - 3))
            column.append(g2)

    def serialize_state(self, state: List[float]) -> bytes:
        w = ByteWriter()
        w.uvarint(state[0])
        for i in range(1, 5):
            w.f64(state[i])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[float]:
        r = ByteReader(data)
        out = [r.uvarint()]
        for _ in range(4):
            out.append(r.f64())
        return out


def _moment_factory(*, order: int, sample: bool):
    def factory(name: str, params: Sequence[Any], arg_types: Sequence[DataType]) -> IAggregateFunction:
        return AggregateFunctionMoment(name, arg_types, params, order=order, sample=sample)

    return factory


# ══════════════════════════════════════════════════════════════════════════
# 熵
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionEntropy(IAggregateFunction):
    """``entropy(x)`` —— 香农熵 ``−Σ p·log₂p``(单位:比特)。

    "这一列有多乱":全相同的列熵为 ``0``,``2^n`` 个等概率取值时熵为 ``n``。
    状态是一个计数分布字典。
    """

    def get_return_type(self) -> DataType:
        return _float64()

    def create_state(self) -> Dict[bytes, int]:
        return {}

    def add(self, state: Dict[bytes, int], columns: Sequence[Any], row: int) -> None:
        k = key_bytes((columns[0].get(row),))
        state[k] = state.get(k, 0) + 1

    def merge(self, dst_state: Dict[bytes, int], src_state: Dict[bytes, int]) -> None:
        for k, c in src_state.items():
            dst_state[k] = dst_state.get(k, 0) + c

    def insert_result_into(self, state: Dict[bytes, int], column: Any) -> None:
        total = sum(state.values())
        if total == 0:
            column.append(0.0)
            return
        ent = 0.0
        for c in state.values():
            p = c / total
            ent -= p * math.log2(p)
        column.append(ent)

    def serialize_state(self, state: Dict[bytes, int]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for k in sorted(state):
            w.uvarint(len(k))
            w.raw(k)
            w.uvarint(state[k])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> Dict[bytes, int]:
        r = ByteReader(data)
        n = r.uvarint()
        out: Dict[bytes, int] = {}
        for _ in range(n):
            klen = r.uvarint()
            k = r.raw(klen)
            out[k] = r.uvarint()
        return out


# ══════════════════════════════════════════════════════════════════════════
# 直方图
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionHistogram(IAggregateFunction):
    """``histogram(N)(x)`` —— 等宽直方图,返回 ``Array(Tuple(Float64, Float64, UInt64))``。

    每个元组是 ``(下界, 上界, 计数)``。N 是桶数(函数参数)。

    .. warning::

       本实现把所有值都存进状态,最后才分桶。真实实现增量维护 min/max
       和每桶计数,内存 O(N) 而不是 O(数据量)。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        _need_args(name, arg_types, 1)
        self.bins = int(params[0]) if params else 10

    def get_return_type(self) -> DataType:
        return DataTypeArray(DataTypeTuple([_float64(), _float64(), get_data_type("UInt64")]))

    def create_state(self) -> List[float]:
        return []

    def add(self, state: List[float], columns: Sequence[Any], row: int) -> None:
        v = columns[0].get(row)
        if v is None:
            return
        state.append(float(v))

    def merge(self, dst_state: List[float], src_state: List[float]) -> None:
        dst_state.extend(src_state)

    def insert_result_into(self, state: List[float], column: Any) -> None:
        if not state:
            column.append([])
            return
        lo, hi = min(state), max(state)
        if hi == lo:
            column.append([(lo, hi, len(state))])
            return
        width = (hi - lo) / self.bins
        counts = [0] * self.bins
        for v in state:
            idx = int((v - lo) / width)
            if idx >= self.bins:
                idx = self.bins - 1  # 最大值落到最后一桶
            counts[idx] += 1
        out = []
        for i in range(self.bins):
            out.append((lo + i * width, lo + (i + 1) * width, counts[i]))
        column.append(out)

    def serialize_state(self, state: List[float]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for v in state:
            w.f64(v)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[float]:
        r = ByteReader(data)
        n = r.uvarint()
        return [r.f64() for _ in range(n)]


# ══════════════════════════════════════════════════════════════════════════
# sequenceCount —— 序列模式匹配(简化)
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionSequenceCount(IAggregateFunction):
    """``sequenceCount(pattern)(time, cond1, ...)`` —— 统计模式出现次数。

    .. warning::

       **简化实现**。真实实现会按 ``time`` 排好序再匹配(所以与到达顺序
       无关);这里**假设行按时间到达**(即已经 ORDER BY time),
       逐个推进状态机。模式从 ``params[0]`` 里解析(形如 ``"(?1)(?2)"``,
       数字是条件的 1-based 序号);省略模式则等价于"所有条件依次成立"。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        self.pattern = _parse_pattern(params, len(arg_types) - 1)

    def get_return_type(self) -> DataType:
        return get_data_type("UInt64")

    def create_state(self) -> List[int]:
        return [0, 0]  # [已匹配到模式第几步, 完成次数]

    def add(self, state: List[int], columns: Sequence[Any], row: int) -> None:
        # columns[0] 是 time(忽略——假设已排序),columns[1:] 是各条件
        if not self.pattern:
            return
        expected = self.pattern[state[0]]
        cond = columns[expected].get(row)  # expected 是 1-based → columns[1]
        if cond:
            state[0] += 1
            if state[0] == len(self.pattern):
                state[0] = 0  # 非重叠匹配:匹配完从头再来
                state[1] += 1

    def merge(self, dst_state: List[int], src_state: List[int]) -> None:
        # 序列模式的状态**不可交换**,合并只能近似 —— 真实 CH 也只对
        # 同一分片内的序列负责。这里把两段拼接:保留 dst 的进度,
        # 再把 src 的完成次数加上(保守做法,文档已说明)。
        dst_state[1] += src_state[1]

    def insert_result_into(self, state: List[int], column: Any) -> None:
        column.append(state[1])

    def serialize_state(self, state: List[int]) -> bytes:
        w = ByteWriter()
        w.uvarint(state[0])
        w.uvarint(state[1])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[int]:
        r = ByteReader(data)
        return [r.uvarint(), r.uvarint()]


def _parse_pattern(params: Sequence[Any], n_conditions: int) -> List[int]:
    """从 ``"(?1)(?2)"`` 里解析出条件序号列表(1-based)。"""
    if not params:
        return list(range(1, n_conditions + 1))
    text = str(params[0])
    nums = [int(ch) for ch in text if ch.isdigit()]
    if not nums:
        return list(range(1, n_conditions + 1))
    return nums


# ══════════════════════════════════════════════════════════════════════════
# 位运算聚合
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionGroupBit(IAggregateFunction):
    """``groupBitAnd`` / ``groupBitOr`` / ``groupBitXor`` 的统一实现。

    .. code-block:: sql

       SELECT groupBitAnd(flags) FROM t;   -- 所有行的 flags 按位与

    状态是一个整数累加器,天然可合并(对应的位运算都是结合的)。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        _need_args(name, arg_types, 1)
        op = name.lower()
        # 必须**精确**区分 and / or / xor:子串匹配会把 ``groupbitxor`` 里的
        # ``"or"`` 认成按位或,于是 groupBitXor 算成了或(5 | 3 = 7 而不是 6)。
        if op.endswith("xor"):
            self.op = "xor"
        elif op.endswith("or"):
            self.op = "or"
        else:
            self.op = "and"

    def get_return_type(self) -> DataType:
        return get_data_type("UInt64")

    def create_state(self) -> List[Any]:
        init = _MASK64 if self.op == "and" else 0
        return [False, init]  # [是否见过值, 累加器]

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        v = columns[0].get(row)
        if v is None:
            return
        v = int(v) & _MASK64
        state[0] = True
        if self.op == "and":
            state[1] &= v
        elif self.op == "or":
            state[1] |= v
        else:
            state[1] ^= v

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        if not src_state[0]:
            return
        if not dst_state[0]:
            dst_state[0] = True
            dst_state[1] = src_state[1]
            return
        if self.op == "and":
            dst_state[1] &= src_state[1]
        elif self.op == "or":
            dst_state[1] |= src_state[1]
        else:
            dst_state[1] ^= src_state[1]

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        column.append(state[1] if state[0] else 0)

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        w.raw(b"\x01" if state[0] else b"\x00")
        w.u64(state[1])
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        r = ByteReader(data)
        has = r.raw(1) == b"\x01"
        return [has, r.u64()]


# ══════════════════════════════════════════════════════════════════════════
# maxIntersections —— 明确未实现
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionMaxIntersections(IAggregateFunction):
    """``maxIntersections(start, end)`` —— 区间最大重叠数(扫描线算法)。

    .. warning::

       **mini-clickhouse 未实现这个函数。** 它的正确实现需要把 (start, end)
       事件排序后做一次扫描线,而"事件排序"依赖一个 mini-clickhouse 里
       还没有的时间/区间类型体系。这里保留名字与类型推导,但
       ``create_state`` 会抛出 ``NOT_IMPLEMENTED_IN_MINICH`` —— 我们
       **明确报错**,而不是悄悄返回一个错误答案。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)

    def get_return_type(self) -> DataType:
        return get_data_type("UInt64")

    def create_state(self) -> Any:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
            "maxIntersections is not implemented in mini-clickhouse "
            "(it needs an ordered interval scan that this teaching build omits)",
        )


# ══════════════════════════════════════════════════════════════════════════
# rankCorr —— 斯皮尔曼秩相关(简化)
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionRankCorr(IAggregateFunction):
    """``rankCorr(x, y)`` —— 斯皮尔曼秩相关系数。

    .. warning::

       **简化实现**:保留所有 ``(x, y)`` 配对,结束时先各自转秩、再算
       皮尔逊相关。内存 O(n);真实实现会在采样后做同样的事。结果与
       "秩"的定义一致,只是不省内存。
    """

    def __init__(self, name: str, arg_types: Sequence[DataType], params: Sequence[Any] = ()) -> None:
        super().__init__(name, arg_types, params)
        _need_args(name, arg_types, 2)

    def get_return_type(self) -> DataType:
        return _float64()

    def create_state(self) -> List[Tuple[float, float]]:
        return []

    def add(self, state: List[Tuple[float, float]], columns: Sequence[Any], row: int) -> None:
        x = columns[0].get(row)
        y = columns[1].get(row)
        if x is None or y is None:
            return
        state.append((float(x), float(y)))

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        dst_state.extend(src_state)

    def insert_result_into(self, state: List[Tuple[float, float]], column: Any) -> None:
        n = len(state)
        if n < 2:
            column.append(float("nan"))
            return
        rx = _ranks([p[0] for p in state])
        ry = _ranks([p[1] for p in state])
        mx = sum(rx) / n
        my = sum(ry) / n
        cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
        vy = math.sqrt(sum((b - my) ** 2 for b in ry))
        if vx == 0 or vy == 0:
            column.append(float("nan"))
        else:
            column.append(cov / (vx * vy))

    def serialize_state(self, state: List[Tuple[float, float]]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for x, y in state:
            w.f64(x)
            w.f64(y)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Tuple[float, float]]:
        r = ByteReader(data)
        n = r.uvarint()
        return [(r.f64(), r.f64()) for _ in range(n)]


def _ranks(values: List[float]) -> List[float]:
    """把一列值转成秩(并列取平均秩,与斯皮尔曼的定义一致)。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


# ══════════════════════════════════════════════════════════════════════════
# nothing / nothingNull
# ══════════════════════════════════════════════════════════════════════════


class AggregateFunctionNothing(IAggregateFunction):
    """``nothing`` / ``nothingNull`` —— 永远返回"空"。

    ``nothing()`` 的结果类型是 ``Nothing``,``nothingNull()`` 是
    ``Nullable(Nothing)`` —— 两者都表示"没有值"。它们主要出现在
    "把 NULL 常量塞进聚合位置"的场合。``insert_result_into`` 是**空操作**,
    因为 ``Nothing`` 类型的列本身不允许 append(见 ``ColumnNothing``)。

    .. warning::

       **偏离一处**:真实 ClickHouse 里 ``SELECT NULL`` 的类型就是
       ``Nullable(Nothing)``,但 mini-clickhouse 的 ``DataTypeNullable``
       显式禁止包装 ``Nothing``(``Nothing.nullable_allowed = False``)。
       我们不去改类型的规则,所以 ``nothingNull()`` 在这里**降级**成
       普通的 ``Nothing``(语义上都是"没有值",只是少了可空标记)。
    """

    def __init__(
        self,
        name: str,
        arg_types: Sequence[DataType],
        params: Sequence[Any] = (),
        *,
        nullable: bool = False,
    ) -> None:
        super().__init__(name, arg_types, params)
        self.nullable = nullable

    def get_return_type(self) -> DataType:
        if not self.nullable:
            return DataTypeNothing()
        # 真实 ClickHouse 里 ``SELECT NULL`` 的类型就是 Nullable(Nothing);
        # 但 mini-clickhouse 的 DataTypeNullable 显式**禁止**包装 Nothing
        # (Nothing.nullable_allowed = False)。我们不去改类型的规则,
        # 而是如实降级成 Nothing,并在 docstring 里标注这条偏离。
        try:
            return DataTypeNullable(DataTypeNothing())
        except Exception:  # noqa: BLE001  (MiniChError:Nothing cannot be nullable)
            return DataTypeNothing()

    def create_state(self) -> None:
        return None

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        return None

    def merge(self, dst_state: Any, src_state: Any) -> None:
        return None

    def insert_result_into(self, state: Any, column: Any) -> None:
        # Nothing 列不能 append —— 保持空操作
        return None

    def serialize_state(self, state: Any) -> bytes:
        return b""

    def deserialize_state(self, data: bytes) -> None:
        return None

    def requires_arg_columns(self) -> bool:
        return False


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════

register_aggregate("varPop", factory=_variance_factory(sample=False, sqrt=False),
                   description="总体方差(Welford 在线算法)")
register_aggregate("varSamp", factory=_variance_factory(sample=True, sqrt=False),
                   description="样本方差(除以 n-1)", aliases=["var"])
register_aggregate("stddevPop", factory=_variance_factory(sample=False, sqrt=True),
                   description="总体标准差")
register_aggregate("stddevSamp", factory=_variance_factory(sample=True, sqrt=True),
                   description="样本标准差", aliases=["stddev"])

register_aggregate("covarPop", factory=_covar_factory(sample=False, correlation=False),
                   description="总体协方差")
register_aggregate("covarSamp", factory=_covar_factory(sample=True, correlation=False),
                   description="样本协方差", aliases=["covar"])
register_aggregate("corr", factory=_covar_factory(sample=False, correlation=True),
                   description="皮尔逊相关系数")

register_aggregate("skewPop", factory=_moment_factory(order=3, sample=False),
                   description="总体偏度")
register_aggregate("skewSamp", factory=_moment_factory(order=3, sample=True),
                   description="样本偏度")
register_aggregate("kurtPop", factory=_moment_factory(order=4, sample=False),
                   description="总体峰度(超额)")
register_aggregate("kurtSamp", factory=_moment_factory(order=4, sample=True),
                   description="样本峰度")

register_aggregate("entropy", AggregateFunctionEntropy, description="香农熵(比特)")
register_aggregate("histogram", AggregateFunctionHistogram,
                   description="等宽直方图,返回 Array(Tuple(下界, 上界, 计数))")
register_aggregate("sequenceCount", AggregateFunctionSequenceCount,
                   description="序列模式出现次数(简化实现:假设按时间到达)")
register_aggregate("groupBitAnd", AggregateFunctionGroupBit, description="按位与聚合")
register_aggregate("groupBitOr", AggregateFunctionGroupBit, description="按位或聚合")
register_aggregate("groupBitXor", AggregateFunctionGroupBit, description="按位异或聚合")
register_aggregate("maxIntersections", AggregateFunctionMaxIntersections,
                   description="区间最大重叠数(mini-clickhouse 未实现,调用即报错)")
register_aggregate("rankCorr", AggregateFunctionRankCorr,
                   description="斯皮尔曼秩相关(简化实现)")

register_aggregate(
    "nothing",
    factory=lambda n, p, a: AggregateFunctionNothing(n, a, p, nullable=False),
    description="永远返回 Nothing(空)",
)
register_aggregate(
    "nothingNull",
    factory=lambda n, p, a: AggregateFunctionNothing(n, a, p, nullable=True),
    description="永远返回 Nullable(Nothing)(空)",
)
