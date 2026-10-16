"""``quantile`` 家族 —— 分位数与中位数。

分位数是最能体现"近似换性能"的聚合之一。真实 ClickHouse 里这一族有
十来个成员,区别全在"用什么结构近似"和"插值不插值":

.. code-block:: text

   quantile / quantiles        采样 + 插值          有误差,最常用
   median                      = quantile(0.5)
   quantileTiming             直方图近似(专为耗时分布设计,精度 0.1ms)
   quantileExact              精确:排序后**直接取某个真实值**(不插值)
   quantileDeterministic      带种子的可复现采样
   quantileTDigest            t-digest 草图(尾部更准)

两个容易混淆的点:

1. **插值 vs 不插值**。``quantile(0.5)`` 对 ``[1, 2, 2, 3]`` 做**线性
   插值**,可能返回一个**不在数据里**的值(比如 ``1.5``);而
   ``quantileExact`` 返回**一定在数据里**的真实元素。所以
   ``quantile`` 的结果类型恒为 ``Float64``,``quantileExact`` 则保持
   输入类型 —— 这不是随便定的,是"结果到底是不是原始值"决定的。
2. **levels 是函数参数,不是列**。``quantiles(0.5, 0.9)(x)`` 里
   ``0.5, 0.9`` 是**声明的常量**(写在函数名后的括号里),
   所以它们进的是 ``params``,返回的是 ``Array(Float64)``。

.. warning::

   **mini-clickhouse 在几个成员上刻意做了简化**,每一处都标了出来:

   * ``quantile`` —— 真实实现用**蓄水池采样**(内存 O(1)),这里**精确
     排序**(内存 O(n))。结果更准,但没有体现"采样"的成本模型;
   * ``quantileTiming`` / ``quantileTDigest`` —— 真实实现用直方图 / t-digest
     草图,这里**退化成精确实现**。语义(插入顺序无关、结果可合并、
     往返精确)完全保留,只是少了"内存不随数据量增长"这一条;
   * ``quantileDeterministic`` —— 真实实现用带种子的采样保证
     "同一份数据、不同分片、结果一致"。这里精确计算,天然满足这个性质。
"""

from __future__ import annotations

import math
from typing import Any, List, Sequence

from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.composite import DataTypeArray
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    decode_key_value,
    encode_key_value,
    register_aggregate,
)

__all__ = ["AggregateFunctionQuantile"]


def _normalize_levels(params: Sequence[Any], name: str) -> List[float]:
    """把函数参数里的 levels 规范成 ``list[float]``(缺省 ``[0.5]``)。"""
    if not params:
        return [0.5]
    levels: List[float] = []
    for p in params:
        try:
            level = float(p)
        except (TypeError, ValueError):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"{name}: quantile level must be a number in [0, 1], got {p!r}",
            )
        if level < 0.0 or level > 1.0:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"{name}: quantile level must be in [0, 1], got {level}",
            )
        levels.append(level)
    return levels


class AggregateFunctionQuantile(IAggregateFunction):
    """``quantile`` / ``quantiles`` / ``median`` 及各种精确/近似变体的统一实现。

    状态就是一个值的列表(精确实现);不同变体只在"插值方式"和
    "结果类型"上不同。所有变体都实现 ``merge``(列表拼接)与
    ``serialize_state``(排序后逐个编码),所以天然支持分布式聚合。
    """

    def __init__(
        self,
        name: str,
        arg_types: Sequence[DataType],
        params: Sequence[Any] = (),
        *,
        interpolate: bool = True,
        plural: bool = False,
        exact_type: bool = False,
    ) -> None:
        super().__init__(name, arg_types, params)
        # 注意:这里**不**强制要求 arg_types 非空 —— 分析器可能先用
        # ``get_aggregate_function("quantilesTiming")`` 探测函数是否存在,
        # 拿到实例后再补 arg_types。参数个数的校验留到 get_return_type。
        #: 是否在相邻两个数据点之间做线性插值
        self.interpolate = interpolate
        #: 结果是否是数组(``quantiles`` 形态)
        self.plural = plural
        #: 是否保持输入类型(``quantileExact`` 形态)
        self.exact_type = exact_type
        self.levels = _normalize_levels(params, name)

    # ── 类型 ────────────────────────────────────────────────────────────

    def _element_type(self) -> DataType:
        if self.exact_type and self.arg_types and self.arg_types[0].is_numeric():
            return self.arg_types[0]
        return get_data_type("Float64")

    def get_return_type(self) -> DataType:
        element = self._element_type()
        if self.plural or len(self.levels) > 1:
            return DataTypeArray(element)
        return element

    # ── 状态 ────────────────────────────────────────────────────────────

    def create_state(self) -> List[Any]:
        return []

    def add(self, state: List[Any], columns: Sequence[Any], row: int) -> None:
        value = columns[0].get(row)
        if value is None:
            return
        state.append(value)

    def merge(self, dst_state: List[Any], src_state: List[Any]) -> None:
        dst_state.extend(src_state)

    # ── 结果 ────────────────────────────────────────────────────────────

    def _quantile_of_sorted(self, values: List[Any], level: float) -> Any:
        """在一个**已排序**列表上取 ``level`` 分位。"""
        n = len(values)
        if n == 0:
            return float("nan")
        if not self.interpolate:
            # nearest-rank:返回一个**真实存在**的元素
            idx = min(n - 1, max(0, math.ceil(level * n) - 1))
            return values[idx]
        if n == 1:
            return float(values[0])
        pos = level * (n - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return float(values[lo])
        frac = pos - lo
        return float(values[lo]) * (1.0 - frac) + float(values[hi]) * frac

    def insert_result_into(self, state: List[Any], column: Any) -> None:
        if not state:
            element = self._element_type()
            empty = float("nan") if element.is_float() or element.get_name() == "Float64" \
                else element.default_value()
            if self.plural or len(self.levels) > 1:
                column.append([empty for _ in self.levels])
            else:
                column.append(empty)
            return
        ordered = sorted(state)
        results = [self._quantile_of_sorted(ordered, lv) for lv in self.levels]
        if self.plural or len(self.levels) > 1:
            column.append(results)
        else:
            column.append(results[0])

    # ── 序列化 ──────────────────────────────────────────────────────────

    def serialize_state(self, state: List[Any]) -> bytes:
        w = ByteWriter()
        w.uvarint(len(state))
        for v in state:
            encode_key_value(w, v)
        return w.bytes()

    def deserialize_state(self, data: bytes) -> List[Any]:
        r = ByteReader(data)
        n = r.uvarint()
        return [decode_key_value(r) for _ in range(n)]


# ── 各个具体函数:用 factory 把"插值 / 复数 / 类型"三个开关固定下来 ──


def _quantile_factory(
    *,
    interpolate: bool,
    plural: bool,
    exact_type: bool = False,
    fixed_levels: Sequence[float] | None = None,
):
    def factory(name: str, params: Sequence[Any], arg_types: Sequence[DataType]) -> IAggregateFunction:
        use_params = list(fixed_levels) if fixed_levels is not None else list(params)
        return AggregateFunctionQuantile(
            name,
            arg_types,
            use_params,
            interpolate=interpolate,
            plural=plural,
            exact_type=exact_type,
        )

    return factory


register_aggregate(
    "quantile",
    factory=_quantile_factory(interpolate=True, plural=False),
    description="分位数(线性插值,结果 Float64;默认 level=0.5)",
)
register_aggregate(
    "quantiles",
    factory=_quantile_factory(interpolate=True, plural=True),
    description="多个分位数(返回 Array(Float64))",
)
register_aggregate(
    "median",
    factory=_quantile_factory(interpolate=True, plural=False, fixed_levels=[0.5]),
    description="中位数(等价于 quantile(0.5))",
)
register_aggregate(
    "quantileExact",
    factory=_quantile_factory(interpolate=False, plural=False, exact_type=True),
    description="精确分位数(nearest-rank,返回真实元素,保持输入类型)",
)
register_aggregate(
    "quantilesExact",
    factory=_quantile_factory(interpolate=False, plural=True, exact_type=True),
    description="多个精确分位数(返回数组)",
)
register_aggregate(
    "quantileTiming",
    factory=_quantile_factory(interpolate=True, plural=False),
    description="分位数(真实实现用直方图;本实现精确——见模块 docstring 的 warning)",
)
register_aggregate(
    "quantilesTiming",
    factory=_quantile_factory(interpolate=True, plural=True),
    description="多个分位数(真实实现用直方图;本实现精确)",
)
register_aggregate(
    "quantileDeterministic",
    factory=_quantile_factory(interpolate=True, plural=False),
    description="确定性的分位数(真实实现用带种子采样;本实现精确,天然确定)",
)
register_aggregate(
    "quantilesDeterministic",
    factory=_quantile_factory(interpolate=True, plural=True),
    description="多个确定性分位数(本实现精确)",
)
register_aggregate(
    "quantileTDigest",
    factory=_quantile_factory(interpolate=True, plural=False),
    description="t-digest 近似分位数(本实现退化为精确——见模块 docstring 的 warning)",
)
register_aggregate(
    "quantilesTDigest",
    factory=_quantile_factory(interpolate=True, plural=True),
    description="多个 t-digest 分位数(本实现精确)",
)
