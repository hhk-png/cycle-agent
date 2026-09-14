"""``miniray.util.metrics`` —— 自定义指标(Counter / Gauge / Histogram)。

真实 Ray 的 ``ray.util.metrics`` 是对 **OpenCensus** 的一层薄封装:
你在任务/actor 里打点,Ray 把数据推给已注册的 exporter,
再经 Prometheus 之类的系统暴露出去(第 11 章 §11.5 讲的就是这条链路)。

.. code-block:: python

    from miniray.util.metrics import Counter, Gauge, Histogram

    # ⚠️ 在模块顶层创建(或 init 之后),不要放进任务函数里反复创建
    REQUESTS = Counter("requests_total", description="处理的请求数")
    LATENCY = Histogram("latency_ms", description="处理耗时",
                        boundaries=[1, 5, 10, 50, 100, 500, 1000])

    @miniray.remote
    def handle(x):
        REQUESTS.inc()                       # 计数 +1
        with LATENCY.timer():                # 自动 observe 耗时(毫秒)
            ...
        return x

    @miniray.remote
    class Worker:
        def __init__(self):
            self.queue_depth = Gauge("queue_depth", description="队列深度")
        def work(self, n):
            self.queue_depth.set(n)          # 瞬时值
            ...

**为什么这一节在 mini-ray 里是"可讲解"的?**
因为指标系统的难点从来不是"怎么计数",而是三件事:

1. **打点对象必须是单例** —— 每次调用都 ``Counter(...)`` 会重复注册;
2. **打点必须线程/进程安全** —— worker 是多线程(actor 还有事件循环);
3. **打点是热路径** —— 一次 ``inc()`` 的开销必须远小于一次业务计算,
   否则"加了监控反而变慢"。

本实现把这三条都做出来了:进程内注册表 + 锁 + 近似无锁的读取路径。
真实 Ray 的做法不同(它把数据交给 OpenCensus 的全局统计器,
再周期性导出),但**上层的三条约束完全一样**。

.. note::
   **与真实 Ray 的差异**:

   * 真实 Ray 的指标**只在 worker 内可用**(在 driver 里创建会报错或者不生效),
     并通过 ``ray.init(_metrics_export_port=...)`` 暴露给 Prometheus;
     本实现是**进程内**的,``snapshot()`` 只能看到**当前进程**打过的点 ——
     跨进程聚合需要你自己从各 worker 里取回(示例 13 演示了其中一种做法);
   * 真实 Ray 的 ``Histogram`` 默认边界、以及名字校验的具体正则,**未确认**;
     本实现用一套合理的默认值并显式校验;
   * 真实 Ray 还有 ``_Metric`` 基类与 ``metrics.get_metric(...)`` 之类的私有入口,
     本实现不提供。
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..errors import MiniRayError

__all__ = ["Counter", "Gauge", "Histogram", "Metric", "snapshot", "reset"]

#: 名字校验:与"能被 Prometheus 接受"的字符集对齐
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:")

#: Histogram 的默认边界(毫秒量级,覆盖"从快到慢"的常见区间)
DEFAULT_BOUNDARIES = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise MiniRayError(f"指标名必须是非空字符串,得到 {name!r}")
    bad = sorted({ch for ch in name if ch not in _ALLOWED})
    if bad:
        raise MiniRayError(
            f"指标名 {name!r} 含有非法字符 {bad}。"
            "只允许字母、数字、下划线和冒号(冒号保留给记录规则)。"
        )
    if name[0].isdigit():
        raise MiniRayError(f"指标名 {name!r} 不能以数字开头")
    return name


def _normalize_tags(tags: Optional[Dict[str, Any]]) -> Tuple[Tuple[str, str], ...]:
    """把 tags 变成可哈希、可比较的元组(顺序固定,便于聚合)。"""
    if not tags:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in tags.items()))


class _Registry:
    """进程内的指标注册表。

    用一把锁保护所有写入。这不是最高效的做法(真实系统会分片或用原子操作),
    但**足以说明问题**:一次 ``inc()`` 只做一次字典查找 + 一次加法,
    在微秒量级,相对于任何有意义的业务计算都可以忽略。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: Dict[Tuple[str, str], "Metric"] = {}

    def register(self, metric: "Metric") -> None:
        key = (type(metric).__name__, metric.name)
        with self._lock:
            if key in self._metrics:
                # 与 Ray 一样:重复创建同名指标是**用户错误**,要显式报出来,
                # 否则会出现"我在 A 处打点、B 处看不到"的诡异现象。
                raise MiniRayError(
                    f"指标 {metric.name!r}({type(metric).__name__})已经注册过了。"
                    "指标对象应当在模块顶层创建一次并复用,"
                    "不要放进任务函数里反复创建。"
                )
            self._metrics[key] = metric

    def all(self) -> List["Metric"]:
        with self._lock:
            return list(self._metrics.values())

    def clear(self) -> None:
        with self._lock:
            self._metrics.clear()


_REGISTRY = _Registry()


class Metric:
    """所有指标的基类。**不要直接实例化它**,用下面三个子类。"""

    def __init__(
        self,
        name: str,
        description: str = "",
        tag_keys: Optional[Iterable[str]] = None,
    ) -> None:
        self.name = _validate_name(name)
        self.description = description or ""
        self.tag_keys = tuple(tag_keys or ())
        self._lock = threading.Lock()
        _REGISTRY.register(self)

    # ---------------------------------------------------------------- 内部
    def _check_tags(self, tags: Optional[Dict[str, Any]]) -> None:
        if tags and self.tag_keys:
            unknown = sorted(set(tags) - set(self.tag_keys))
            if unknown:
                raise MiniRayError(
                    f"指标 {self.name!r} 的 tag_keys 是 {list(self.tag_keys)},"
                    f"但收到了未声明的标签 {unknown}。"
                )

    def _key(self, tags: Optional[Dict[str, Any]]) -> Tuple[Tuple[str, str], ...]:
        """把一次打点的 tags 归一化成注册表的键。

        声明了 ``tag_keys`` 时**只保留声明过的标签** —— 这样"多传了一个标签"
        不会导致数据分裂成两条时间线(那是很难查的一类监控事故)。
        """
        if not tags:
            return ()
        if self.tag_keys:
            return _normalize_tags({k: v for k, v in tags.items() if k in self.tag_keys})
        return _normalize_tags(tags)

    def snapshot(self) -> Any:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - 展示
        return f"{type(self).__name__}(name={self.name!r})"


class Counter(Metric):
    """只增不减的计数。问的是「发生了多少次」。

    .. code-block:: python

        ERRORS = Counter("errors_total", description="失败次数",
                         tag_keys=("kind",))
        ERRORS.inc()                       # +1
        ERRORS.inc(5)                      # +5
        ERRORS.inc(tags={"kind": "timeout"})
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        tag_keys: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(name, description, tag_keys)
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}

    def inc(self, value: float = 1.0, tags: Optional[Dict[str, Any]] = None) -> None:
        """计数加 ``value``(默认 1)。"""
        self._check_tags(tags)
        key = self._key(tags)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(value)

    def snapshot(self) -> Dict[Tuple[Tuple[str, str], ...], float]:
        with self._lock:
            return dict(self._values)

    def get(self, tags: Optional[Dict[str, Any]] = None) -> float:
        """读当前值(测试与调试用)。"""
        key = self._key(tags)
        with self._lock:
            return self._values.get(key, 0.0)


class Gauge(Metric):
    """可增可减的瞬时值。问的是「现在是多少」。

    .. code-block:: python

        DEPTH = Gauge("queue_depth", description="队列深度")
        DEPTH.set(42)
        print(DEPTH.get())     # 42
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        tag_keys: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(name, description, tag_keys)
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}

    def set(self, value: float, tags: Optional[Dict[str, Any]] = None) -> None:
        """把当前值设为 ``value``。"""
        self._check_tags(tags)
        key = self._key(tags)
        with self._lock:
            self._values[key] = float(value)

    def get(self, tags: Optional[Dict[str, Any]] = None) -> float:
        key = self._key(tags)
        with self._lock:
            return self._values.get(key, 0.0)

    def snapshot(self) -> Dict[Tuple[Tuple[str, str], ...], float]:
        with self._lock:
            return dict(self._values)


class Histogram(Metric):
    """分布统计。问的是「耗时/大小的分布长什么样」。

    :param boundaries: 桶边界(升序)。落进桶 ``i`` 的值满足
        ``boundaries[i-1] <= v < boundaries[i]``;最后一个桶是 ``>= boundaries[-1]``。

    .. code-block:: python

        LAT = Histogram("latency_ms", boundaries=[1, 10, 100])

        with LAT.timer():          # 自动以**毫秒**为单位 observe
            do_work()

        LAT.observe(3.5)           # 手动
        print(LAT.quantile(0.95))  # 近似分位数(按桶线性插值)
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        boundaries: Optional[Iterable[float]] = None,
        tag_keys: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(name, description, tag_keys)
        bounds = tuple(float(b) for b in (boundaries or DEFAULT_BOUNDARIES))
        if not bounds:
            raise MiniRayError("Histogram 的 boundaries 不能为空")
        if any(b2 <= b1 for b1, b2 in zip(bounds, bounds[1:])):
            raise MiniRayError(f"Histogram 的 boundaries 必须严格升序,得到 {bounds}")
        self.boundaries = bounds
        #: 每个键的计数:len(boundaries)+1 个桶(最后一个是 +Inf 溢出桶)
        self._counts: Dict[Tuple[Tuple[str, str], ...], List[int]] = {}
        self._sums: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._totals: Dict[Tuple[Tuple[str, str], ...], int] = {}

    # ---------------------------------------------------------------- 打点
    def observe(self, value: float, tags: Optional[Dict[str, Any]] = None) -> None:
        """记录一个观测值。"""
        self._check_tags(tags)
        key = self._key(tags)
        index = self._bucket_index(float(value))
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = [0] * (len(self.boundaries) + 1)
                self._counts[key] = counts
                self._sums[key] = 0.0
                self._totals[key] = 0
            counts[index] += 1
            self._sums[key] += float(value)
            self._totals[key] += 1

    @contextmanager
    def timer(self, tags: Optional[Dict[str, Any]] = None):
        """上下文管理器:自动以**毫秒**为单位记录这段代码的耗时。

        .. code-block:: python

            with LAT.timer():
                work()
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe((time.perf_counter() - start) * 1000.0, tags=tags)

    def _bucket_index(self, value: float) -> int:
        for index, bound in enumerate(self.boundaries):
            if value < bound:
                return index
        return len(self.boundaries)

    # ---------------------------------------------------------------- 读取
    def snapshot(self) -> Dict[Tuple[Tuple[str, str], ...], Dict[str, Any]]:
        with self._lock:
            out: Dict[Tuple[Tuple[str, str], ...], Dict[str, Any]] = {}
            for key, counts in self._counts.items():
                out[key] = {
                    "counts": list(counts),
                    "sum": self._sums[key],
                    "total": self._totals[key],
                }
            return out

    def count(self, tags: Optional[Dict[str, Any]] = None) -> int:
        """观测次数。"""
        key = self._key(tags)
        with self._lock:
            return self._totals.get(key, 0)

    def mean(self, tags: Optional[Dict[str, Any]] = None) -> float:
        key = self._key(tags)
        with self._lock:
            total = self._totals.get(key, 0)
            if not total:
                return 0.0
            return self._sums[key] / total

    def quantile(self, q: float, tags: Optional[Dict[str, Any]] = None) -> float:
        """**近似**分位数 —— 按桶线性插值,所以精度受 boundaries 限制。

        真实系统(Prometheus ``histogram_quantile``)也是这么算的,
        所以你会看到「P99 = 480ms」这种数在两次采集之间跳 —— 那是桶的粒度,
        不是数据在跳。
        """
        if not 0.0 <= q <= 1.0:
            raise MiniRayError(f"分位数必须在 [0, 1] 之间,得到 {q}")
        key = self._key(tags)
        with self._lock:
            counts = self._counts.get(key)
            total = self._totals.get(key, 0)
            if not counts or not total:
                return 0.0
            counts = list(counts)
        target = q * total
        cumulative = 0
        lower = 0.0
        for index, count in enumerate(counts):
            upper = self.boundaries[index] if index < len(self.boundaries) else math.inf
            if cumulative + count >= target and count:
                if math.isinf(upper):
                    return lower
                # 桶内线性插值:假设值在桶里均匀分布
                return lower + (upper - lower) * ((target - cumulative) / count)
            cumulative += count
            lower = upper
        return lower


# ---------------------------------------------------------------------------
# 模块级工具:用于自测与调试
# ---------------------------------------------------------------------------


def snapshot() -> Dict[str, Dict[Any, Any]]:
    """把**当前进程**注册表里所有指标的值导出成一个字典。

    这是本实现特有的调试入口(真实 Ray 靠 exporter 往外推)。
    在 worker 里调用它,只能看到那个 worker 进程自己打过的点。
    """
    out: Dict[str, Dict[Any, Any]] = {}
    for metric in _REGISTRY.all():
        out[f"{type(metric).__name__}:{metric.name}"] = metric.snapshot()
    return out


def reset() -> None:
    """清空注册表。**仅供测试使用**。"""
    _REGISTRY.clear()
