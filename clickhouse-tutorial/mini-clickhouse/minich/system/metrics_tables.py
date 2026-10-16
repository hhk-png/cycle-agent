"""**可观测性**类系统表之一 —— 计数器与瞬时值。

ClickHouse 有两套互补的度量,这是理解监控的**第一道分水岭**:

.. code-block:: text

   ┌─────────────────────────┬─────────────────────────┬───────────────────┐
   │                         │ system.events           │ system.metrics    │
   ├─────────────────────────┼─────────────────────────┼───────────────────┤
   │ 语义                    │ **counter**(只增不减)    │ **gauge**(可上可下)│
   │ 例子                    │ 累计读了多少行           │ 当前几个查询在跑   │
   │ 告警写法                │ 求**速率**(差值/秒)      │ 直接比阈值         │
   │ 数据源                  │ ctx.profile_events      │ ctx.metrics       │
   └─────────────────────────┴─────────────────────────┴───────────────────┘

**把这两者搞混是监控里最经典的错误**:对 counter 写"当前值 > 5 就告警",
服务跑上一天必然触发(因为它只增不减);对 gauge 求"速率"则毫无意义
(它本来就一直在变)。所以这两张表列在同一个模块里,就是为了让读者一眼
看到它们的对称关系。

第三张表 ``system.asynchronous_metrics`` 是"定期采样"的 gauge
(磁盘剩余空间、内存驻留量这类**读取有成本**的指标),它不在每次查询时
更新,而是由后台线程周期性刷新。
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Iterable, List, Tuple

from .base import SimpleSystemTable, register_system_table

__all__ = [
    "SystemMetrics",
    "SystemEvents",
    "SystemAsynchronousMetrics",
]


# ══════════════════════════════════════════════════════════════════════════
# system.metrics(gauge)
# ══════════════════════════════════════════════════════════════════════════

#: 我们**维护**的那几个 gauge 的说明。真实 ClickHouse 有几百个 metric,
#: 这里只保留教学与排错真正会用到的,外加一批"由系统表自己算出来"的派生量。
_METRIC_DESCRIPTIONS: Dict[str, str] = {
    "Query": "正在执行的查询数(由 ctx.metrics 维护的 gauge)。",
    "TCPConnection": "当前的客户端连接数。",
    "MemoryTracking": "进程当前使用的内存字节数(RSS 近似)。",
    "BackgroundMergesAndMutationsPoolTask":
        "后台 merge / mutation 池里正在执行的任务数。",
    "PartsTemporary": "正在写入、尚未提交的临时 part 数(磁盘上的 tmp_insert_*)。",
    "PartsPreCommitted": "已经写好但尚未提交的 part 数。",
    "ReplicatedChecks": "正在进行的一致性检查(副本校验)任务数。",
}


def _process_memory() -> Tuple[int, int]:
    """尽力取当前进程的 (RSS, 虚拟内存) 字节数;拿不到返回 ``(0, 0)``。

    优先用 ``psutil``(跨平台),退回到 Linux 的 ``/proc/self/statm``。
    两者都不可用时如实返回 0 —— 系统表报 0 比报一个编出来的数字安全。
    """
    try:
        import psutil  # type: ignore

        info = psutil.Process().memory_info()
        return int(info.rss), int(info.vms)
    except Exception:
        pass
    # Linux 回退
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            fields = fh.read().split()
        pages = os.sysconf("SC_PAGE_SIZE")
        return int(fields[1]) * pages, int(fields[0]) * pages
    except Exception:
        return 0, 0


def _sum_engine_stat(ctx: Any, key: str) -> int:
    """把目录里每个引擎 ``describe_stats()[key]`` 加起来(取不到算 0)。"""
    catalog = getattr(ctx, "catalog", None)
    if catalog is None:
        return 0
    total = 0
    for table in catalog.all_tables():
        engine = getattr(table, "engine", None)
        if engine is None:
            continue
        try:
            stats = engine.describe_stats()
        except Exception:  # pragma: no cover
            continue
        if isinstance(stats, dict):
            try:
                total += int(stats.get(key, 0) or 0)
            except Exception:  # pragma: no cover
                pass
    return total


class SystemMetrics(SimpleSystemTable):
    """``system.metrics`` —— 回答"**此刻**服务器处在什么状态"。

    这是监控面板上的那排"仪表盘":

    .. code-block:: sql

       SELECT metric, value FROM system.metrics
       WHERE metric IN ('Query', 'TCPConnection', 'MemoryTracking');

    最常用的三个判读:

    * ``Query`` 持续不降 —— 有慢查询或者查询堆积(去看 ``system.processes``);
    * ``MemoryTracking`` 逼近上限 —— 快 OOM 了(去 ``system.query_log``
       按 ``memory_usage`` 排序找元凶);
    * ``BackgroundMergesAndMutationsPoolTask`` 长期打满 —— 合并跟不上写入,
      该关注 "too many parts" 了。

    .. note::

       数据 = ``ctx.metrics``(会话/服务级 gauge)**并上**几个本表自己算的
       派生量(``PartsTemporary`` / ``PartsPreCommitted`` / ``ReplicatedChecks``
       —— 从各引擎的 ``describe_stats()`` 汇总,或从 ``ctx.metrics`` 原样取)。
       ``is_async`` 恒为 0:那是"由后台线程周期刷新"的标记,
       mini-clickhouse 不区分,如实报 0。
    """

    name = "metrics"
    columns = [
        ("metric", "String"),
        ("value", "Int64"),
        ("description", "String"),
        ("is_async", "UInt8"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        metrics = getattr(ctx, "metrics", None)
        values: Dict[str, int] = {}
        if metrics is not None:
            for name, value in metrics.as_rows():
                try:
                    values[name] = int(value)
                except Exception:  # pragma: no cover
                    values[name] = 0
        # ── 派生量:优先用 ctx.metrics 的原值,没有就现算 ──
        values.setdefault("Query", 0)
        values.setdefault("TCPConnection", 0)
        values.setdefault("BackgroundMergesAndMutationsPoolTask", 0)
        rss, _vms = _process_memory()
        values["MemoryTracking"] = rss
        values["PartsTemporary"] = _sum_engine_stat(ctx, "temporary_parts")
        values["PartsPreCommitted"] = _sum_engine_stat(ctx, "precommitted_parts")
        values.setdefault("ReplicatedChecks", 0)

        for name in sorted(values):
            yield (
                name,
                values[name],
                _METRIC_DESCRIPTIONS.get(name, ""),
                0,
            )


# ══════════════════════════════════════════════════════════════════════════
# system.events(counter)
# ══════════════════════════════════════════════════════════════════════════

#: 每条 ProfileEvent 的说明。名字与 :data:`minich.util.stopwatch.KNOWN_EVENTS`
#: 一一对应 —— 这张表最怕的就是"名字看着懂、其实不懂",所以这里逐条写清楚。
_EVENT_DESCRIPTIONS: Dict[str, str] = {
    "Query": "累计执行的查询数(含 INSERT / DDL)。",
    "SelectQuery": "累计执行的 SELECT 数。",
    "InsertQuery": "累计执行的 INSERT 数。",
    "FailedQuery": "累计失败的查询数 —— 排查「为什么偶发报错」先看它有没有涨。",
    "ReadCompressedBytes": "从磁盘读入的**压缩**字节数。",
    "ReadUncompressedBytes": "解压后的**未压缩**字节数(与前者之比≈压缩率)。",
    "SelectedRows": "累计从表中读取的行数。",
    "SelectedBytes": "累计读取的字节数。",
    "SelectedMarks": "累计跳过的索引标记(mark)数量 —— 越小说明主键裁剪越有效。",
    "SelectedParts": "累计读取的 part 数。",
    "SelectedRanges": "累计读取的(主键)区间数 —— 直观反映索引的有用程度。",
    "InsertedRows": "累计插入的行数。",
    "InsertedBytes": "累计插入的字节数。",
    "MergedRows": "后台合并累计处理的行数。",
    "MergedUncompressedBytes": "后台合并累计处理的未压缩字节数。",
    "Merge": "累计发生的后台合并次数。",
    "MutateTask": "累计调度的 mutation 任务数(ALTER UPDATE/DELETE)。",
    "CreatedReadBufferOrdinary": "为普通(Log 家族/File)表创建的读缓冲数。",
    "CreatedWriteBufferOrdinary": "为普通表创建的写缓冲数。",
    "CreatedReadBufferMergeTree": "为 MergeTree 创建的读缓冲数。",
    "CreatedWriteBufferMergeTree": "为 MergeTree 创建的写缓冲数。",
    "DiskReadElapsedMicroseconds": "磁盘读累计耗时(微秒)——缓慢的 I/O 会在这里积累。",
    "DiskWriteElapsedMicroseconds": "磁盘写累计耗时(微秒)。",
    "RealTimeMicroseconds": "查询累计占用的墙钟时间(微秒)。",
    "UserTimeMicroseconds": "查询累计消耗的用户态 CPU 时间(微秒)。",
    "SystemTimeMicroseconds": "查询累计消耗的内核态 CPU 时间(微秒)。",
    "MemoryAllocatorPurge": "内存分配器主动回收(resize/purge)的次数。",
    "QueryProfilerRuns": "查询剖析器采样次数。",
    "AggregatedRows": "聚合处理器累计摄入的行数(喂给聚合函数的行)。",
    "AggregateFunctionErrors": "聚合函数内部出错的次数(正常应为 0)。",
    "RowsReadByMainReader": "主线程读入的行数。",
    "RowsReadByPreReader": "预读线程读入的行数。",
    "FilteredRows": "被 WHERE 过滤掉的行数(配合 SelectedRows 看过滤率)。",
    "JoinedRows": "JOIN 累计产出的行数。",
    "JoinBuildTableRowCount": "JOIN 构建阶段(哈希表)累计行数。",
    "JoinProbeTableRowCount": "JOIN 探测阶段累计行数。",
    "SortedRows": "排序处理器累计处理的行数。",
    "CompressedReadBufferBlocks": "压缩读缓冲累计处理的块数。",
    "CompressedWriteBufferBlocks": "压缩写缓冲累计处理的块数。",
    "CompressionSize": "压缩后累计字节数。",
    "UncompressedSize": "压缩前累计字节数。",
    "MarkCacheHits": "标记(mark)缓存命中次数 —— 命中率低说明索引缓存太小。",
    "MarkCacheMisses": "标记缓存未命中次数。",
    "UncompressedCacheHits": "未压缩数据缓存命中次数。",
    "UncompressedCacheMisses": "未压缩数据缓存未命中次数。",
    "QueryCacheHits": "查询结果缓存命中次数。",
    "QueryCacheMisses": "查询结果缓存未命中次数。",
    "QueryCacheAgeSeconds": "命中结果的新鲜度(秒)。",
    "MergeTreeDataWriterBlocks": "MergeTree 写路径累计写出的块数。",
    "MergeTreeDataWriterRows": "MergeTree 写路径累计写出的行数。",
    "PartsLockHoldMicroseconds": "持有 parts 锁的累计时长(微秒)——过长会拖慢所有写入。",
    "PartsLockWaitMicroseconds": "等待 parts 锁的累计时长(微秒)——高说明锁竞争激烈。",
    "ContextLockWaitMicroseconds": "等待 context 锁的累计时长(微秒)。",
    "IOBufferAllocs": "IO 缓冲累计分配次数。",
    "S3Requests": "对 S3 发起的请求数。",
    "S3GetObject": "S3 GET 对象次数。",
    "S3PutObject": "S3 PUT 对象次数。",
    "ZooKeeperRequests": "对 ZooKeeper 发起的请求数。",
    "ZooKeeperInit": "ZooKeeper 会话初始化次数。",
    "KeeperRequests": "对 Keeper 发起的请求数。",
    "ReplicatedPartFetches": "复制表累计从其它副本拉取的 part 数。",
    "ReplicatedPartMerges": "复制表累计合并的 part 数。",
    "DuplicateInterserverIOHTTP": "副本间数据传输的重复请求数。",
    "NetworkReceiveElapsedMicroseconds": "网络接收累计耗时(微秒)。",
    "NetworkSendElapsedMicroseconds": "网络发送累计耗时(微秒)。",
    "ThrottlerSleepMicroseconds": "因限速而睡眠的累计时长(微秒)。",
    "OSCPUWaitMicroseconds": "等待 CPU 的累计时长(微秒)。",
    "OSIOWaitMicroseconds": "等待 I/O 的累计时长(微秒)。",
    "OSReadChars": "操作系统层面累计读入的字符数。",
    "OSWriteChars": "操作系统层面累计写出的字符数。",
    "ThreadPoolTask": "线程池累计执行的任务数。",
    "BackgroundMergesAndMutationsPoolTask": "后台 merge/mutation 池累计执行的任务数。",
    "BackgroundFetchesPoolTask": "后台 fetch(副本拉取)池累计执行的任务数。",
    "BackgroundSchedulePoolTask": "后台调度池累计执行的任务数。",
    "AsyncLogWrite": "异步日志累计写入次数。",
}


class SystemEvents(SimpleSystemTable):
    """``system.events`` —— 回答"自启动以来,累计发生了什么、发生了多少次"。

    这是**唯一**能回答"量级"问题的表:

    .. code-block:: sql

       -- 压缩率:未压缩 / 压缩,越大说明压得越好
       SELECT
         (SELECT value FROM system.events WHERE event='ReadUncompressedBytes')
       / (SELECT value FROM system.events WHERE event='ReadCompressedBytes');

    它的值**只增不减**,所以正确的用法是"隔一段时间取两次快照相减"
    (这正是 ``system.query_log.ProfileEvents`` 列做的事 ——
    每条查询结束时的增量)。

    .. note::

       列出的行 = ``ctx.profile_events`` 里已有的计数器,加上
       :data:`minich.util.stopwatch.KNOWN_EVENTS` 里**尚未触发**(值为 0)
       的那些 —— 这样 schema 是稳定的,``WHERE event = 'X'`` 不会因为
       "这个事件刚重启还没发生过"而查不到行。每条都给一句中文说明。
    """

    name = "events"
    columns = [
        ("event", "String"),
        ("value", "UInt64"),
        ("description", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        values: Dict[str, int] = {}
        events = getattr(ctx, "profile_events", None)
        if events is not None:
            for name, value in events.as_rows():
                try:
                    values[name] = int(value)
                except Exception:  # pragma: no cover
                    values[name] = 0
        try:
            from ..util.stopwatch import KNOWN_EVENTS

            for name in KNOWN_EVENTS:
                values.setdefault(name, 0)
        except Exception:  # pragma: no cover
            pass
        for name in sorted(values):
            yield (name, values[name], _EVENT_DESCRIPTIONS.get(name, ""))


# ══════════════════════════════════════════════════════════════════════════
# system.asynchronous_metrics(周期性 gauge)
# ══════════════════════════════════════════════════════════════════════════


class SystemAsynchronousMetrics(SimpleSystemTable):
    """``system.asynchronous_metrics`` —— 回答"那些**读起来有点贵**的指标现在是多少"。

    与 ``system.metrics`` 的区别是**更新方式**:

    .. code-block:: text

       system.metrics              每次查询/操作时**就地更新**(便宜)
       system.asynchronous_metrics 后台线程**定期采样**(读 /proc、statfs… 有成本)

    所以磁盘剩余空间、进程 RSS 这类指标在这里,而不是在 ``system.metrics``。
    真实 ClickHouse 默认每 60 秒刷新一次(``asynchronous_metrics_update_period_s``)。

    .. note::

       我们只报能**诚实算出来**的五个:``Uptime`` / ``MemoryResident`` /
       ``MemoryVirtual`` / ``FilesystemAvailable`` / ``FilesystemFree`` /
       ``FilesystemTotal``。真实 ClickHouse 有**几百个**异步指标
       (CPU、内存分配器、页表、cgroup 限制……),这里不铺开。
       取不到内存信息(没装 psutil 且不是 Linux)时 ``MemoryResident``
       与 ``MemoryVirtual`` 为 0;取不到磁盘时三个文件系统指标为 0。
    """

    name = "asynchronous_metrics"
    columns = [
        ("metric", "String"),
        ("value", "Float64"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        rows: List[Tuple[Any, ...]] = []
        uptime = 0
        try:
            uptime = int(ctx.uptime())
        except Exception:  # pragma: no cover
            uptime = 0
        rows.append(("Uptime", float(uptime)))

        rss, vms = _process_memory()
        rows.append(("MemoryResident", float(rss)))
        rows.append(("MemoryVirtual", float(vms)))

        path = getattr(ctx, "data_path", None) or os.getcwd()
        free = total = 0
        try:
            usage = shutil.disk_usage(path)
            free, total = int(usage.free), int(usage.total)
        except OSError:  # pragma: no cover
            pass
        rows.append(("FilesystemAvailable", float(free)))
        rows.append(("FilesystemFree", float(free)))
        rows.append(("FilesystemTotal", float(total)))
        return rows


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════


def register() -> None:
    for cls in (SystemMetrics, SystemEvents, SystemAsynchronousMetrics):
        register_system_table(cls())
