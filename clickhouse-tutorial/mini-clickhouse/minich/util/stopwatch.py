"""计时与性能计数器 —— ``system.query_log`` 与 ``EXPLAIN`` 的数据来源。

真实 ClickHouse 有两套相互配合的度量:

* **ProfileEvents** —— 单调递增的**计数器**(读了多少行、扫描了多少字节、
  做了多少次 merge、选了多少个 granule)。它是**累积**的,
  想知道"这条查询读了多少行",要在查询前后各取一次快照再相减;
* **CurrentMetrics** —— **瞬时值** gauge(当前有多少个活跃查询、
  内存占用多少、后台 merge 有多少个在跑)。

``system.events`` 暴露前者,``system.metrics`` 暴露后者,
而 ``system.query_log`` 每一行的 ``ProfileEvents`` 列就是
"这条查询从开始到结束的计数器增量"。

.. note::

   真实 ClickHouse 用 ``Stopwatch``(基于 ``clock_gettime(CLOCK_MONOTONIC)``)
   与 ``ThreadProfileEvents``。mini-clickhouse 用 ``time.perf_counter_ns()``
   —— 同样单调,不受系统时间调整影响。**永远不要用 ``time.time()``
   测耗时**:它是墙上时钟,会被 NTP 回拨搞成负数。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["Stopwatch", "ProfileEvents", "MetricsRegistry", "measure"]


class Stopwatch:
    """单调计时器。

    .. code-block:: python

       sw = Stopwatch()
       ... do work ...
       print(sw.elapsed_ms())

    ``start()`` 可以重复调用来**重置**(真实 CH 的 ``Stopwatch::restart``)。
    """

    __slots__ = ("_start_ns", "_elapsed_ns", "_running")

    def __init__(self, start: bool = True) -> None:
        self._elapsed_ns = 0
        self._running = False
        self._start_ns = 0
        if start:
            self.start()

    def start(self) -> None:
        self._start_ns = time.perf_counter_ns()
        self._running = True
        self._elapsed_ns = 0

    def restart(self) -> None:
        self.start()

    def stop(self) -> None:
        if self._running:
            self._elapsed_ns += time.perf_counter_ns() - self._start_ns
            self._running = False

    def elapsed_ns(self) -> int:
        if self._running:
            return self._elapsed_ns + (time.perf_counter_ns() - self._start_ns)
        return self._elapsed_ns

    def elapsed_us(self) -> float:
        return self.elapsed_ns() / 1_000.0

    def elapsed_ms(self) -> float:
        return self.elapsed_ns() / 1_000_000.0

    def elapsed_seconds(self) -> float:
        return self.elapsed_ns() / 1_000_000_000.0

    def __enter__(self) -> "Stopwatch":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


#: ProfileEvents 的**规范名字**。真实 ClickHouse 的完整列表在
#: ``src/Common/ProfileEvents.cpp`` 里(几百个),这里只保留教学与
#: 排错真正会用到的那些。命名规则与真实 CH 一致:
#: ``<对象><动作>``,全部大写骆驼拼写。
KNOWN_EVENTS: Tuple[str, ...] = (
    "Query",
    "SelectQuery",
    "InsertQuery",
    "FailedQuery",
    "ReadCompressedBytes",
    "ReadUncompressedBytes",
    "SelectedRows",
    "SelectedBytes",
    "SelectedMarks",
    "SelectedParts",
    "SelectedRanges",
    "InsertedRows",
    "InsertedBytes",
    "MergedRows",
    "MergedUncompressedBytes",
    "Merge",
    "MutateTask",
    "CreatedReadBufferOrdinary",
    "CreatedWriteBufferOrdinary",
    "CreatedReadBufferMergeTree",
    "CreatedWriteBufferMergeTree",
    "DiskReadElapsedMicroseconds",
    "DiskWriteElapsedMicroseconds",
    "RealTimeMicroseconds",
    "UserTimeMicroseconds",
    "SystemTimeMicroseconds",
    "MemoryAllocatorPurge",
    "QueryProfilerRuns",
    "AggregatedRows",
    "AggregateFunctionErrors",
    "RowsReadByMainReader",
    "RowsReadByPreReader",
    "FilteredRows",
    "JoinedRows",
    "JoinBuildTableRowCount",
    "JoinProbeTableRowCount",
    "SortedRows",
    "CompressedReadBufferBlocks",
    "CompressedWriteBufferBlocks",
    "CompressionSize",
    "UncompressedSize",
    "MarkCacheHits",
    "MarkCacheMisses",
    "UncompressedCacheHits",
    "UncompressedCacheMisses",
    "QueryCacheHits",
    "QueryCacheMisses",
    "QueryCacheAgeSeconds",
    "MergeTreeDataWriterBlocks",
    "MergeTreeDataWriterRows",
    "PartsLockHoldMicroseconds",
    "PartsLockWaitMicroseconds",
    "ContextLockWaitMicroseconds",
    "IOBufferAllocs",
    "S3Requests",
    "S3GetObject",
    "S3PutObject",
    "ZooKeeperRequests",
    "ZooKeeperInit",
    "KeeperRequests",
    "ReplicatedPartFetches",
    "ReplicatedPartMerges",
    "DuplicateInterserverIOHTTP",
    "NetworkReceiveElapsedMicroseconds",
    "NetworkSendElapsedMicroseconds",
    "ThrottlerSleepMicroseconds",
    "OSCPUWaitMicroseconds",
    "OSIOWaitMicroseconds",
    "OSReadChars",
    "OSWriteChars",
    "ThreadPoolTask",
    "BackgroundMergesAndMutationsPoolTask",
    "BackgroundFetchesPoolTask",
    "BackgroundSchedulePoolTask",
    "AsyncLogWrite",
)


class ProfileEvents:
    """一个计数器表 —— ``名字 → 次数``。

    用法和真实 ClickHouse 完全对应:

    .. code-block:: python

       ev = ProfileEvents()
       ev.increment("SelectedRows", 100)
       ev.increment("SelectedRows")           # 默认 +1
       ev.get("SelectedRows")                 # 100
       delta = ev.snapshot_delta(before)      # 两个快照的差值

    ``system.events`` 输出的是**累积值**,而 ``system.query_log`` 的
    ``ProfileEvents`` 输出的是**本次查询的增量** —— 后者就是
    "结束后取快照,减去开始前的快照"。
    """

    __slots__ = ("_counters",)

    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}

    def increment(self, name: str, delta: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + delta

    def get(self, name: str) -> int:
        return self._counters.get(name, 0)

    def has(self, name: str) -> bool:
        return name in self._counters

    def add(self, name: str, delta: int) -> None:
        self.increment(name, delta)

    def set(self, name: str, value: int) -> None:
        self._counters[name] = value

    def snapshot(self) -> Dict[str, int]:
        return dict(self._counters)

    def reset(self) -> None:
        self._counters.clear()

    def merge(self, other: "ProfileEvents") -> None:
        for k, v in other._counters.items():
            self._counters[k] = self._counters.get(k, 0) + v

    @staticmethod
    def delta(after: Dict[str, int], before: Dict[str, int]) -> Dict[str, int]:
        """两个快照的差 —— 只保留非零项(真实 CH 也这么做,省空间)。"""
        out: Dict[str, int] = {}
        for k, v in after.items():
            d = v - before.get(k, 0)
            if d:
                out[k] = d
        return out

    def as_rows(self) -> List[Tuple[str, int]]:
        """按名字排序的 ``(name, value)`` 列表 —— ``system.events`` 的行。"""
        return sorted(self._counters.items())

    def __getitem__(self, name: str) -> int:
        return self.get(name)

    def __len__(self) -> int:
        return len(self._counters)

    def __repr__(self) -> str:  # pragma: no cover
        return f"ProfileEvents({len(self._counters)} counters)"


class MetricsRegistry:
    """瞬时值表 —— ``system.metrics`` 与 ``system.asynchronous_metrics``。

    ``system.metrics`` 与 ``system.events`` 的区别只有一条,但很重要:

    * metrics 是 **gauge**(可上可下):"当前活跃查询数 = 3";
    * events 是 **counter**(只增不减):"累计执行了 12345 条查询"。

    搞混这两者会导致监控告警写错(比如对 counter 做"大于 5 就告警",
    结果跑一会儿必然触发)。
    """

    __slots__ = ("_gauges",)

    def __init__(self) -> None:
        self._gauges: Dict[str, float] = {}

    def set(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def get(self, name: str, default: float = 0.0) -> float:
        return self._gauges.get(name, default)

    def inc(self, name: str, delta: float = 1.0) -> None:
        self._gauges[name] = self._gauges.get(name, 0.0) + delta

    def dec(self, name: str, delta: float = 1.0) -> None:
        self._gauges[name] = self._gauges.get(name, 0.0) - delta

    def as_rows(self) -> List[Tuple[str, float]]:
        return sorted(self._gauges.items())


def measure(fn: Any, *args: Any, **kwargs: Any) -> Tuple[Any, float]:
    """跑一个函数并返回 ``(结果, 毫秒数)`` —— 测试与基准用。"""
    sw = Stopwatch()
    result = fn(*args, **kwargs)
    sw.stop()
    return result, sw.elapsed_ms()
