"""日志 —— 落进 ``system.text_log``,同时可选打到控制台。

真实 ClickHouse 的日志体系有三个特点,这个模块把前两个做了出来:

1. **日志是表**。``system.text_log`` 是一张可查询的表,所以你可以写
   ``SELECT * FROM system.text_log WHERE level = 'Error' AND query_id = '...'``
   ——**用 SQL 查日志**。这是 ClickHouse 排障体验里最舒服的一点:
   不需要去翻文件、也没必要装 ELK。
2. **日志有级别,而且可以用 ``send_logs_level`` 在客户端临时调高**。
   ``clickhouse-client`` 的 ``--send_logs_level=trace`` 能把服务端的
   详细日志"顺着查询通道"推回客户端 —— 排查单条慢查询时非常有用。
3. **结构化的字段**。每条日志都带 ``query_id`` / ``thread_id`` /
   ``table`` / ``part_name`` 等字段,而不是只有一段文本。

.. note::

   ``text_log`` 表在真实 ClickHouse 里**默认不创建**,需要手工
   ``CREATE TABLE system.text_log`` + ``SET text_log = 1``。
   很多人以为"没有日志"是 bug,其实只是没开。第 24 章有一节讲这个。
"""

from __future__ import annotations

import datetime as _dt
import sys
import threading
from typing import Any, Dict, List, Optional

__all__ = ["LogLevel", "LogRecord", "Logger", "get_logger", "set_log_level"]


class LogLevel:
    """日志级别 —— 编号与真实 ClickHouse 的 ``Poco::Message::Priority`` 一致。"""

    NONE = 0
    FATAL = 1
    CRITICAL = 2
    ERROR = 3
    WARNING = 4
    NOTICE = 5
    INFORMATION = 6
    DEBUG = 7
    TRACE = 8
    TEST = 9

    #: 名字 → 编号
    NAMES: Dict[str, int] = {
        "none": NONE,
        "fatal": FATAL,
        "critical": CRITICAL,
        "error": ERROR,
        "warning": WARNING,
        "notice": NOTICE,
        "information": INFORMATION,
        "debug": DEBUG,
        "trace": TRACE,
        "test": TEST,
    }

    #: 编号 → 名字(``system.text_log.level`` 列显示的就是这个)
    TO_NAME: Dict[int, str] = {v: k.capitalize() for k, v in NAMES.items()}
    TO_NAME[INFORMATION] = "Information"
    TO_NAME[WARNING] = "Warning"
    TO_NAME[ERROR] = "Error"

    @classmethod
    def from_name(cls, name: str) -> int:
        return cls.NAMES.get(name.strip().lower(), cls.INFORMATION)

    @classmethod
    def to_name(cls, value: int) -> str:
        return cls.TO_NAME.get(value, f"Level{value}")


class LogRecord:
    """一条日志 —— 与 ``system.text_log`` 的列一一对应。"""

    __slots__ = ("event_time", "event_time_microseconds", "level", "query_id",
                 "thread_id", "logger_name", "message", "source_file", "extra")

    def __init__(
        self,
        level: int,
        logger_name: str,
        message: str,
        query_id: str = "",
        thread_id: int = 0,
        source_file: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = _dt.datetime.now()
        self.event_time = now.strftime("%Y-%m-%d %H:%M:%S")
        self.event_time_microseconds = now.strftime("%Y-%m-%d %H:%M:%S.%f")
        self.level = level
        self.query_id = query_id
        self.thread_id = thread_id
        self.logger_name = logger_name
        self.message = message
        self.source_file = source_file
        self.extra = extra or {}

    @property
    def level_name(self) -> str:
        return LogLevel.to_name(self.level)

    def format_text(self) -> str:
        """``text_log`` 表里 ``message`` 列的形态(带前缀的完整行)。"""
        return (
            f"{self.event_time_microseconds} [{ self.thread_id }] "
            f"{{{self.logger_name}}} <{self.level_name}> {self.message}"
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LogRecord {self.level_name} {self.message[:40]!r}>"


class Logger:
    """一个带级别的日志器。

    **线程安全**:ClickHouse 的日志来自几十个线程(后台 merge、每个查询
    的执行线程),所以写入必须加锁。真实 CH 用无锁队列 + 单个写线程,
    mini-clickhouse 用一把锁 —— 后者足够,前者更快。
    """

    def __init__(self, name: str, level: int = LogLevel.INFORMATION) -> None:
        self.name = name
        self.level = level
        self._records: List[LogRecord] = []
        self._lock = threading.Lock()
        #: 是否同时打到 stderr。真实 CH 有 ``logger.console`` 设置。
        self.console = False
        self._max_records = 100_000

    # ── 主入口 ──────────────────────────────────────────────────────────

    def log(self, level: int, message: str, **extra: Any) -> None:
        if level > self.level:
            return
        rec = LogRecord(
            level,
            self.name,
            message,
            query_id=extra.pop("query_id", ""),
            thread_id=extra.pop("thread_id", threading.get_ident()),
            source_file=extra.pop("source_file", ""),
            extra=extra,
        )
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max_records:
                # 环形截断 —— 真实 CH 也会丢弃最老的日志,只是阈值大得多
                del self._records[: len(self._records) - self._max_records // 2]
        if self.console:
            sys.stderr.write(rec.format_text() + "\n")

    def fatal(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.FATAL, message, **extra)

    def critical(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.CRITICAL, message, **extra)

    def error(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.ERROR, message, **extra)

    def warning(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.WARNING, message, **extra)

    def notice(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.NOTICE, message, **extra)

    def information(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.INFORMATION, message, **extra)

    def debug(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.DEBUG, message, **extra)

    def trace(self, message: str, **extra: Any) -> None:
        self.log(LogLevel.TRACE, message, **extra)

    # ── 查询 ────────────────────────────────────────────────────────────

    def records(self, min_level: Optional[int] = None) -> List[LogRecord]:
        with self._lock:
            if min_level is None:
                return list(self._records)
            return [r for r in self._records if r.level <= min_level]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def set_level(self, level: int) -> None:
        self.level = level


#: ``logger name → Logger``。真实 CH 里日志器是按**组件**分的
#: (``MergeTree``、``Keeper``、``Query``…),我们沿用这个习惯。
_LOGGERS: Dict[str, Logger] = {}
_GLOBAL_LEVEL = LogLevel.INFORMATION
_LOCK = threading.Lock()


def get_logger(name: str = "minich") -> Logger:
    """取(或创建)一个具名日志器。"""
    with _LOCK:
        lg = _LOGGERS.get(name)
        if lg is None:
            lg = Logger(name, _GLOBAL_LEVEL)
            _LOGGERS[name] = lg
        return lg


def set_log_level(level: int) -> None:
    """全局调整级别(对应 ``SET send_logs_level = 'trace'`` 的服务端侧)。"""
    global _GLOBAL_LEVEL
    _GLOBAL_LEVEL = level
    with _LOCK:
        for lg in _LOGGERS.values():
            lg.level = level


def all_loggers() -> Dict[str, Logger]:
    return dict(_LOGGERS)
