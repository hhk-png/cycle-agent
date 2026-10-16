"""**可观测性**类系统表之二 —— 查询日志、进程列表、文本日志、错误统计。

这一组表回答的是"**刚刚发生了什么**",它们有一个共同特点:
**数据是"活"的**,会随着查询本身而变化。这一点有两个直接后果:

1. ``SELECT * FROM system.query_log`` 这条查询**自己也会进 query_log**
   ——真实 ClickHouse 里这会造成"日志越查越多",所以有
   ``log_queries = 0`` 这个开关;
2. ``system.processes`` 读的是"此刻正在跑的查询",所以它显示的是
   **读它之前**的状态(常见困惑:"我明明在跑一条慢查询,为什么
   ``system.processes`` 里是空的?"——因为那条查询在等它返回)。

.. code-block:: text

   system.query_log         ── 已结束的查询(分析慢查询/内存/失败率的主力)
   system.query_thread_log  ── 每条查询的**执行线程**(mini-clickhouse 为空)
   system.processes         ── 正在跑的查询(≈ MySQL 的 SHOW PROCESSLIST)
   system.text_log          ── 服务端日志(用 SQL 查日志)
   system.errors            ── 每类错误累计发生了多少次
   system.crash_log         ── 崩溃现场(本实现恒空)
   system.metric_log        ── 指标随时间的变化(本实现恒空)
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, List, Tuple

from ..util.logger import LogLevel, all_loggers
from .base import SimpleSystemTable, register_system_table

__all__ = [
    "SystemQueryLog",
    "SystemQueryThreadLog",
    "SystemProcesses",
    "SystemTextLog",
    "SystemErrors",
    "SystemCrashLog",
    "SystemMetricLog",
    "record_error",
    "error_rows",
    "reset_errors",
]


# ══════════════════════════════════════════════════════════════════════════
# system.query_log
# ══════════════════════════════════════════════════════════════════════════


def _query_log_row(info: Any) -> Tuple[Any, ...]:
    """把一个 :class:`minich.execution.context.QueryInfo` 映射成一行。

    ``QueryInfo.to_row()`` 已经给出了大部分列(它就是照着
    ``system.query_log`` 的形状设计的);我们在这里补上它没有的三列:
    ``stack_trace`` / ``thread_ids`` / ``interface``。
    """
    row = info.to_row()
    return (
        row["type"],
        row["event_time"],
        row["query_start_time"],
        row["query_duration_ms"],
        row["read_rows"],
        row["read_bytes"],
        row["written_rows"],
        row["written_bytes"],
        row["result_rows"],
        row["result_bytes"],
        row["memory_usage"],
        row["current_database"],
        row["query"],
        row["exception"],
        "",                                   # stack_trace —— 我们不做 C++ 栈回溯
        row["query_id"],
        row["user"],
        dict(row["ProfileEvents"]),
        dict(row["Settings"]),
        row["log_comment"],
        row["query_kind"],
        list(getattr(info, "thread_ids", []) or []),
        "TCP",                                # interface —— 我们只有进程内调用
    )


class SystemQueryLog(SimpleSystemTable):
    """``system.query_log`` —— 回答"过去每条查询干了什么、花了多少、多久"。

    这是**最重要的一张排障表**,没有之一。它能回答的问题包括:

    .. code-block:: sql

       -- ① 最耗内存的查询(找 OOM 元凶)
       SELECT query_id, memory_usage, query
       FROM system.query_log
       ORDER BY memory_usage DESC LIMIT 10;

       -- ② 最慢的查询
       SELECT query_duration_ms, read_rows, query
       FROM system.query_log
       WHERE type = 'QueryFinish'
       ORDER BY query_duration_ms DESC LIMIT 10;

       -- ③ 失败率
       SELECT type, count() FROM system.query_log GROUP BY type;

    ``type`` 有四态(见 :class:`minich.execution.context.QueryStatus`),
    最容易搞混的是:**``QueryStart`` 不是"成功"**,它只是"开始" ——
    成功的查询最终以 ``QueryFinish`` 收尾。另外
    ``ProfileEvents`` / ``Settings`` 两列存的是**该次查询的增量/生效值**,
    所以"这条查询开了哪些非默认设置"可以直接从一行里看出来。

    .. note::

       数据来自 ``ctx.query_log``(**已结束**查询的列表;正在跑的查询在
       ``system.processes``)。这个列表是**共享**的 —— ``Context.clone()``
       不会复制它,所以一条查询读到的日志是"服务级"的。
       mini-clickhouse **不做** ``log_queries`` 开关:查询一律记入。
    """

    name = "query_log"
    columns = [
        ("type", "String"),
        ("event_time", "DateTime"),
        ("query_start_time", "DateTime"),
        ("query_duration_ms", "UInt64"),
        ("read_rows", "UInt64"),
        ("read_bytes", "UInt64"),
        ("written_rows", "UInt64"),
        ("written_bytes", "UInt64"),
        ("result_rows", "UInt64"),
        ("result_bytes", "UInt64"),
        ("memory_usage", "UInt64"),
        ("current_database", "String"),
        ("query", "String"),
        ("exception", "String"),
        ("stack_trace", "String"),
        ("query_id", "String"),
        ("user", "String"),
        ("ProfileEvents", "Map(String, String)"),
        ("Settings", "Map(String, String)"),
        ("log_comment", "String"),
        ("query_kind", "String"),
        ("thread_ids", "Array(UInt64)"),
        ("interface", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        log = getattr(ctx, "query_log", None)
        if not log:
            return
        for info in log:
            yield _query_log_row(info)


# ══════════════════════════════════════════════════════════════════════════
# system.query_thread_log
# ══════════════════════════════════════════════════════════════════════════


class SystemQueryThreadLog(SimpleSystemTable):
    """``system.query_thread_log`` —— 回答"这条查询是**哪个线程**把时间花掉的"。

    这张表的存在理由值得单独讲清楚。真实 ClickHouse 里一条查询会被拆成
    **很多个执行线程**(读线程、聚合线程、合并线程……),``system.query_log``
    只记"整条查询"的汇总,``system.query_thread_log`` 则**每个执行线程一行**:

    .. code-block:: text

       system.query_log                system.query_thread_log
       ┌──────────────┬──────────┐     ┌──────────┬──────────┬──────────┐
       │ query_id     │ 总耗时   │     │ 线程 A   │ 线程 B   │ 线程 C   │
       ├──────────────┼──────────┤     ├──────────┼──────────┼──────────┤
       │ q-1          │ 1000 ms  │     │ 950 ms   │ 40 ms    │ 10 ms    │
       └──────────────┴──────────┘     └──────────┴──────────┴──────────┘
                                         ↑ 瓶颈在 A —— 这才是"哪一步慢"的答案

    所以它回答的是"**瓶颈在线程,而不只是在这条查询**"。要开它得先
    ``SET log_query_threads = 1``(默认关,因为行数会翻好几倍)。

    .. note::

       **mini-clickhouse 里这张表恒空**,原因很简单:**我们是单线程的**
       —— 一条查询至多占一个 Python 线程,所以"每线程一行"和
       "每查询一行"没有区别,单独建一张表没有信息量。保留 schema 是为了
       (i) 让引用了这张表的教程 SQL 不报错;
       (ii) 把"真实 CH 为什么需要它"讲清楚 —— 这正是分布式/多核数据库
            的天然产物,不是可有可无的装饰。
    """

    name = "query_thread_log"
    columns = [
        ("host_name", "String"),
        ("thread_name", "String"),
        ("thread_id", "UInt64"),
        ("master_thread_id", "UInt64"),
        ("query_id", "String"),
        ("event_time", "DateTime"),
        ("query_start_time", "DateTime"),
        ("query_duration_ms", "UInt64"),
        ("read_rows", "UInt64"),
        ("read_bytes", "UInt64"),
        ("written_rows", "UInt64"),
        ("written_bytes", "UInt64"),
        ("result_rows", "UInt64"),
        ("result_bytes", "UInt64"),
        ("memory_usage", "UInt64"),
        ("current_database", "String"),
        ("query", "String"),
        ("exception", "String"),
        ("interface", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        return iter(())


# ══════════════════════════════════════════════════════════════════════════
# system.processes
# ══════════════════════════════════════════════════════════════════════════


class SystemProcesses(SimpleSystemTable):
    """``system.processes`` —— 回答"现在有哪些查询正在跑"(≈ ``SHOW PROCESSLIST``)。

    这是发现**积压**和**杀死慢查询**的入口:

    .. code-block:: sql

       -- 进度:读了多少行 / 预计还要多久(真实 CH 里 elapsed 会一起涨)
       SELECT query_id, elapsed, read_rows, query
       FROM system.processes ORDER BY elapsed DESC;

       -- 顺手杀掉最慢的那条
       KILL QUERY WHERE query_id = '...';

    ``is_initial_query`` / ``initial_user`` 这一组列是为**分布式**准备的:
    一个查询从 A 节点发到 B、C 节点执行时,B/C 上看到的是"别人让我跑的",
    ``is_initial_query = 0``,而 ``initial_address`` 指向发起方 ——
    排查"这条查询到底是谁发起的"全靠它们。

    .. note::

       数据来自 ``ctx.processes``(``query_id → QueryInfo`` 的字典,
       **正在执行**的查询)。mini-clickhouse 的 ``processes`` 目前只会被
       写入"进入执行"的查询;由于我们基本是同步执行,你几乎总是
       看到空表 —— 这是真实的,不是 bug。
    """

    name = "processes"
    columns = [
        ("is_initial_query", "UInt8"),
        ("user", "String"),
        ("query_id", "String"),
        ("address", "String"),
        ("port", "UInt32"),
        ("initial_user", "String"),
        ("initial_query_id", "String"),
        ("initial_address", "String"),
        ("elapsed", "Float64"),
        ("read_rows", "UInt64"),
        ("read_bytes", "UInt64"),
        ("written_rows", "UInt64"),
        ("written_bytes", "UInt64"),
        ("memory_usage", "UInt64"),
        ("query", "String"),
        ("current_database", "String"),
        ("query_kind", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        processes = getattr(ctx, "processes", None) or {}
        items = processes.values() if isinstance(processes, dict) else list(processes)
        for info in items:
            yield (
                int(getattr(info, "is_initial_query", 1) or 0),
                getattr(info, "user", "") or "",
                getattr(info, "query_id", "") or "",
                "127.0.0.1",                       # address —— 进程内调用
                0,                                  # port
                getattr(info, "user", "") or "",
                getattr(info, "query_id", "") or "",
                "127.0.0.1",
                float(getattr(info, "elapsed", 0.0) or 0.0),
                int(getattr(info, "read_rows", 0) or 0),
                int(getattr(info, "read_bytes", 0) or 0),
                int(getattr(info, "written_rows", 0) or 0),
                int(getattr(info, "written_bytes", 0) or 0),
                int(getattr(info, "memory_usage", 0) or 0),
                getattr(info, "query", "") or "",
                getattr(info, "current_database", "") or "",
                getattr(info, "query_kind", "Select") or "Select",
            )


# ══════════════════════════════════════════════════════════════════════════
# system.text_log
# ══════════════════════════════════════════════════════════════════════════


class SystemTextLog(SimpleSystemTable):
    """``system.text_log`` —— 回答"服务端日志里有没有相关的报错"。

    它的意义是**用 SQL 查日志**:不用 SSH 上去 ``grep``,直接

    .. code-block:: sql

       SELECT event_time, level, query_id, message
       FROM system.text_log
       WHERE level IN ('Error', 'Warning') AND event_time > now() - 300
       ORDER BY event_time DESC;

    就能把最近 5 分钟的告警拉出来,而且**能 JOIN 别的系统表**
    (比如按 ``query_id`` 关联 ``system.query_log`` 看完整上下文)。

    .. note::

       **真实 ClickHouse 默认不创建这张表。** 要用得先手工
       ``CREATE TABLE system.text_log (...)`` 再
       ``SET text_log = 1`` —— 很多人以为"没有日志"是坏了,
       其实只是没开(:mod:`minich.util.logger` 的文档讲了这个)。
       mini-clickhouse **默认就有**,因为日志本来就在内存里
       (``minich.util.logger.all_loggers()``)。
       ``level`` 用 ``String`` 表示(真实 CH 是 ``Enum8`` 的级别名);
       ``event_time_microseconds`` 是 ``DateTime64(6)``。
    """

    name = "text_log"
    columns = [
        ("event_time", "DateTime"),
        ("event_time_microseconds", "DateTime64(6)"),
        ("level", "String"),
        ("query_id", "String"),
        ("thread_id", "UInt64"),
        ("logger_name", "String"),
        ("message", "String"),
        ("source_file", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        try:
            loggers = all_loggers()
        except Exception:  # pragma: no cover
            return
        for name in sorted(loggers):
            for record in loggers[name].records():
                yield (
                    record.event_time,
                    record.event_time_microseconds,
                    LogLevel.to_name(record.level),
                    record.query_id or "",
                    int(record.thread_id or 0),
                    record.logger_name,
                    record.message,
                    record.source_file or "",
                )


# ══════════════════════════════════════════════════════════════════════════
# system.errors
# ══════════════════════════════════════════════════════════════════════════

#: 错误名 → ``{code, value, last_time, last_message}``。
#: 这是模块级状态,相当于真实 ClickHouse 里 ``ErrorCodes`` 的计数表。
_ERRORS: Dict[str, Dict[str, Any]] = {}
_ERRORS_LOCK = threading.Lock()


def record_error(exc: BaseException) -> None:
    """记一次错误 —— **可选钩子**,由解释器在捕获异常时调用。

    ... 但解释器**目前还没有调用它**(见下方 ``SystemErrors`` 的说明),
    所以这张表现在恒空。函数已经就位:一旦解释器在
    :func:`minich.execution.interpreter.execute_query` 的 ``except``
    分支里加上 ``record_error(e)``,``system.errors`` 就会立刻工作。

    兼容两种入参:

    * :class:`minich.errors.MiniChError` —— 取它的 ``code`` / ``name`` /
      ``message``(最精确);
    * 任意其它 ``Exception`` —— 用类名当错误名,``code = 0``。
    """
    code = int(getattr(exc, "code", 0) or 0)
    name = getattr(exc, "name", None) or type(exc).__name__
    message = getattr(exc, "message", None) or str(exc)
    import datetime as _dt

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _ERRORS_LOCK:
        entry = _ERRORS.get(name)
        if entry is None:
            _ERRORS[name] = {
                "code": code, "value": 1, "last_time": now, "last_message": message,
            }
        else:
            entry["value"] += 1
            entry["last_time"] = now
            entry["last_message"] = message
            if code:
                entry["code"] = code


def error_rows() -> List[Tuple[Any, ...]]:
    """当前错误计数的快照(按错误名排序)。"""
    with _ERRORS_LOCK:
        snapshot = {k: dict(v) for k, v in _ERRORS.items()}
    return [
        (name, e["code"], e["value"], e["last_time"], e["last_message"], 0)
        for name, e in sorted(snapshot.items())
    ]


def reset_errors() -> None:
    """清空错误计数 —— 给测试用。"""
    with _ERRORS_LOCK:
        _ERRORS.clear()


class SystemErrors(SimpleSystemTable):
    """``system.errors`` —— 回答"每一类错误累计发生了多少次、最后一次是什么"。

    和 ``system.query_log`` 里那些失败行不同,这张表是**按错误类型聚合**的,
    所以它一眼就能看出"服务现在最痛的是哪类错误":

    .. code-block:: sql

       SELECT name, code, value, last_error_message
       FROM system.errors ORDER BY value DESC;

    ``value`` 是**累计**值(只增不减)。要看"错误是不是正在变多",
    得隔一段时间取两次相减 —— 和 ``system.events`` 是同一套用法。

    .. note::

       **本实现里这张表目前恒为空。** 计数靠 :func:`record_error` 维护,
       而它需要解释器在 ``except`` 里主动调用 —— 那一步**还没有接上**。
       我们把钩子、schema、查询路径全部准备好,只差一行调用:
       这是"可选集成点"的典型写法,接不接由解释器决定,
       系统表这一侧不越界去改别的模块。
    """

    name = "errors"
    columns = [
        ("name", "String"),
        ("code", "Int32"),
        ("value", "UInt64"),
        ("last_error_time", "DateTime"),
        ("last_error_message", "String"),
        ("remote", "UInt8"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        return error_rows()


# ══════════════════════════════════════════════════════════════════════════
# system.crash_log / system.metric_log
# ══════════════════════════════════════════════════════════════════════════


class SystemCrashLog(SimpleSystemTable):
    """``system.crash_log`` —— 回答"服务上次崩在哪"。

    真实 ClickHouse 崩溃(段错误、断言失败)时,会把现场写进这张表:
    调用栈、信号、出错时的查询 ID、构建版本。这是**事后定位崩溃**的唯一线索。

    .. note::

       **mini-clickhouse 恒空。** 原因很直接:我们跑在 CPython 上,
       崩的是解释器(Python 进程挂了),不该由数据库自己去捕获
       Python 级别的段错误;而且这张表本来的价值在于 C++ 栈回溯,
       纯 Python 实现里没有对应物。schema 保留以对齐真实 CH。
    """

    name = "crash_log"
    columns = [
        ("hostname", "String"),
        ("event_time", "DateTime"),
        ("event_date", "Date"),
        ("signal", "UInt8"),
        ("thread_id", "UInt64"),
        ("query_id", "String"),
        ("far_pointer", "UInt64"),
        ("version", "String"),
        ("stack_trace", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        return iter(())


class SystemMetricLog(SimpleSystemTable):
    """``system.metric_log`` —— 回答"某个指标**随时间**是怎么变的"。

    ``system.metrics`` 只给"此刻的值";要画趋势就得靠这张表 ——
    它由后台线程把 ``system.metrics`` 定期**拍一张快照存下来**:

    .. code-block:: sql

       SELECT event_time, value FROM system.metric_log
       WHERE metric = 'Query' ORDER BY event_time;

    .. note::

       **mini-clickhouse 恒空** —— 我们没有"后台线程定期落盘指标"这条流水线。
       schema 与真实 CH 对齐(``event_date`` / ``event_time`` /
       ``event_time_microseconds`` / ``metric`` / ``value``)。
    """

    name = "metric_log"
    columns = [
        ("event_date", "Date"),
        ("event_time", "DateTime"),
        ("event_time_microseconds", "DateTime64(6)"),
        ("metric", "String"),
        ("value", "Float64"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        return iter(())


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════


def register() -> None:
    for cls in (
        SystemQueryLog,
        SystemQueryThreadLog,
        SystemProcesses,
        SystemTextLog,
        SystemErrors,
        SystemCrashLog,
        SystemMetricLog,
    ):
        register_system_table(cls())
