"""查询上下文 —— ``Context``,以及一次查询的结果 ``QueryResult``。

``Context`` 是 ClickHouse 里最"什么都知道"的一个类
(``src/Interpreters/Context.cpp``,一万多行)。它被传来传去,
承载的东西包括:

.. code-block:: text

   会话状态       当前数据库、当前用户、会话级 settings、临时表
   安全边界       用户/角色/行策略/配额(第 23 章)
   资源与统计     ProfileEvents 计数器、CurrentMetrics 瞬时值、查询 ID
   基础设施       磁盘、Keeper 连接、集群拓扑、后台线程池
   查询生命周期   查询开始/结束的钩子(query_log、进程列表)

为什么不做成"每样东西一个全局变量"? 因为 ClickHouse 里有**三个层次**
的上下文,而且它们**继承**关系明确:

.. code-block:: text

   Server 级 Context          进程唯一。磁盘、集群、监听端口、后台池
        │
        ├─► Session 级 Context  每个客户端连接一个。当前库、用户、会话 settings
        │        │
        │        └─► Query 级 Context   每条查询一个。查询 ID、查询级 settings、
        │                                LIMIT 下推、中间结果的内存配额
        └─► 后台任务的 Context   merge/TTL/fetch 用,带上"我是后台"的标记

**settings 的解析顺序**就是沿着这条链往上看:查询级 > 会话级 > 服务级。
第 24 章有一节专门讲这个顺序,以及"为什么我在客户端 SET 了却不管用"
(答案通常是:那条查询的 ``SETTINGS`` 子句覆盖了它)。

mini-clickhouse 把这些压成**一个** ``Context`` 类,靠 ``clone()``
来做"继承"(子上下文共享不可变部分,拥有自己的 settings)。
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..block import Block, ColumnWithTypeAndName
from ..catalog import DatabaseCatalog, StorageID, Table
from ..errors import ErrorCode, MiniChError, raise_error
from ..util.logger import LogLevel, get_logger
from ..util.stopwatch import MetricsRegistry, ProfileEvents, Stopwatch

__all__ = ["Context", "QueryResult", "QueryStatus", "QueryInfo"]


class QueryStatus:
    """查询状态 —— 与真实 ClickHouse 的 ``QueryStatus`` 枚举同名同义。

    ``system.processes`` 的 ``query`` 与 ``system.query_log`` 的
    ``type`` 列就是它的取值。注意到一个反直觉的地方:
    **``QueryStart`` 是"开始"而不是"完成"**,而一条成功的查询
    留在 query_log 里的 type 是 ``QueryFinish``。
    """

    QueryStart = "QueryStart"
    QueryFinish = "QueryFinish"
    ExceptionBeforeStart = "ExceptionBeforeStart"
    ExceptionWhileProcessing = "ExceptionWhileProcessing"


class QueryInfo:
    """一条查询的运行时信息 —— ``system.processes`` / ``system.query_log`` 的数据源。

    真实 ClickHouse 的 ``QueryInfo`` 有一百多个字段(几乎所有列都能
    从它映射出来)。这里保留教学与排错最常用的那批。
    """

    __slots__ = (
        "query_id", "query", "user", "current_database", "client_hostname",
        "start_time", "start_time_us", "finish_time", "elapsed", "read_rows",
        "read_bytes", "written_rows", "written_bytes", "result_rows",
        "result_bytes", "memory_usage", "profile_events", "settings",
        "status", "exception", "format", "log_comment", "revoked",
        "thread_ids", "query_kind", "is_initial_query", "os_user",
        "snapshot", "blocks_from_remote",
    )

    def __init__(self, query_id: str, query: str, user: str = "default",
                 database: str = "default") -> None:
        self.query_id = query_id
        self.query = query
        self.user = user
        self.current_database = database
        self.client_hostname = "localhost"
        self.start_time = _dt.datetime.now()
        self.start_time_us = int(time.time() * 1_000_000)
        self.finish_time: Optional[_dt.datetime] = None
        self.elapsed: float = 0.0
        self.read_rows = 0
        self.read_bytes = 0
        self.written_rows = 0
        self.written_bytes = 0
        self.result_rows = 0
        self.result_bytes = 0
        self.memory_usage = 0
        self.profile_events: Dict[str, int] = {}
        self.settings: Dict[str, Any] = {}
        self.status = QueryStatus.QueryStart
        self.exception: Optional[str] = None
        self.format = ""
        self.log_comment = ""
        self.revoked = False
        self.thread_ids: List[int] = []
        self.query_kind = "Select"
        self.is_initial_query = 1
        self.os_user = ""
        self.snapshot: Optional[Dict[str, int]] = None
        self.blocks_from_remote = 0

    def elapsed_seconds(self) -> float:
        return self.elapsed

    def to_row(self) -> Dict[str, Any]:
        """映射到 ``system.query_log`` 的一行。"""
        return {
            "type": self.status,
            "event_time": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "query_start_time": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "query_duration_ms": int(self.elapsed * 1000),
            "read_rows": self.read_rows,
            "read_bytes": self.read_bytes,
            "written_rows": self.written_rows,
            "written_bytes": self.written_bytes,
            "result_rows": self.result_rows,
            "result_bytes": self.result_bytes,
            "memory_usage": self.memory_usage,
            "current_database": self.current_database,
            "query": self.query,
            "exception": self.exception or "",
            "query_id": self.query_id,
            "user": self.user,
            "ProfileEvents": dict(self.profile_events),
            "Settings": dict(self.settings),
            "log_comment": self.log_comment,
            "query_kind": self.query_kind,
        }


class QueryResult:
    """一次查询的结果。

    设计上和真实 ClickHouse 的客户端一样支持**两种消费方式**:

    * **流式** —— :meth:`stream` 逐个 Block 产出,适合大结果集
      (服务端不必把整个结果集放进内存);
    * **物化** —— :meth:`blocks` / :meth:`rows` 一次性拿到全部
      (测试与交互式使用方便)。

    两条路径共用同一份缓存,所以"先 stream 再 rows"不会重复执行查询。

    .. note::

       ``FORMAT`` 的处理放在这里而不是解释器里,是因为真实 ClickHouse
       也是这么做的:解释器产出 ``Block`` 流,格式层把它写成文本/二进制。
       两者的接口就是 :class:`typing.Iterator` of :class:`Block` ——
       这也是"为什么 ClickHouse 能把第一行结果几乎立刻发给客户端"的原因。
    """

    def __init__(
        self,
        ctx: "Context",
        query_id: str,
        header: Optional[Block] = None,
        format_name: str = "PrettyCompact",
        info: Optional[QueryInfo] = None,
        source: Optional[Iterator[Block]] = None,
    ) -> None:
        self.ctx = ctx
        self.query_id = query_id
        self.header = header
        self.format_name = format_name
        self.info = info or QueryInfo(query_id, "")
        self._source = source
        self._cache: List[Block] = []
        self._exhausted = source is None
        self._text: Optional[str] = None
        self._elapsed_applied = False

    # ── 消费 ────────────────────────────────────────────────────────────

    def stream(self) -> Iterator[Block]:
        """流式产出结果块(**会消费底层迭代器**)。"""
        if self._source is not None:
            for block in self._source:
                self._cache.append(block)
                yield block
            self._source = None
            self._exhausted = True
        else:
            for block in self._cache:
                yield block

    def blocks(self) -> List[Block]:
        """把结果**全部物化**并返回(会跑完整个查询)。"""
        if not self._exhausted:
            for _ in self.stream():
                pass
        return self._cache

    def column_names(self) -> List[str]:
        if self.header is not None:
            return self.header.get_names()
        blocks = self.blocks()
        return blocks[0].get_names() if blocks else []

    def column_types(self) -> List[str]:
        if self.header is not None:
            return [c.type.get_name() for c in self.header.data]
        blocks = self.blocks()
        return [c.type.get_name() for c in blocks[0].data] if blocks else []

    def rows(self) -> List[Tuple[Any, ...]]:
        """把结果拉平成 ``[(v1, v2), ...]`` —— 测试里最常用的形式。"""
        out: List[Tuple[Any, ...]] = []
        for block in self.blocks():
            for i in range(block.rows()):
                out.append(tuple(c.column.get(i) for c in block.data))
        return out

    def first_row(self) -> Optional[Tuple[Any, ...]]:
        for block in self.blocks():
            if block.rows():
                return tuple(c.column.get(i) for c in block.data for i in (0,))
        return None

    def row_count(self) -> int:
        return sum(b.rows() for b in self.blocks())

    def scalar(self) -> Any:
        """单行单列结果的便捷取值(``SELECT count() ...``)。"""
        row = self.first_row()
        return row[0] if row else None

    def to_block(self) -> Block:
        """把所有结果合成**一个** Block。"""
        from ..block import Block as _Block

        blocks = self.blocks()
        if not blocks:
            return self.header.clone_empty() if self.header is not None else _Block()

        def _materialize_consts(b: Block) -> Block:
            # ``SELECT 1 UNION ALL SELECT 2`` 这类查询里,每个分支的 ``1`` / ``2``
            # 都是**常量列**(``ColumnConst``)。直接把分支块 merge 到一起会走到
            # ``ColumnConst.append``,而它的语义是"只能追加与已有值**相同**的值"
            # —— 于是第二个分支的 ``2`` 撞上报
            # ``Cannot insert a different value into a constant column``。
            # 真实 CH 的 ``UnionTransform`` / ``MergingSortedTransform`` 在拼接
            # 之前会 ``convertToFullColumnIfConst``;我们在这里做同样的事。
            changed = any(c.column.is_const() for c in b.data)
            if not changed:
                return b
            cols = [
                ColumnWithTypeAndName(
                    c.name,
                    c.type,
                    c.column.convert_to_full_column() if c.column.is_const() else c.column,
                )
                for c in b.data
            ]
            return _Block(cols, b.info.clone())

        out = _materialize_consts(blocks[0]).clone_empty()
        for b in blocks:
            out.merge(_materialize_consts(b))
        return out

    # ── 文本输出 ────────────────────────────────────────────────────────

    def text(self, format_name: Optional[str] = None) -> str:
        """按指定格式把结果渲染成文本。

        默认用查询里写的 ``FORMAT``(没有就用 ``PrettyCompact`` ——
        与 ``clickhouse-client`` 的默认一致)。
        """
        if format_name is None and self._text is not None:
            return self._text
        from ..formats import get_output_format

        fmt_name = format_name or self.format_name or "PrettyCompact"
        fmt = get_output_format(fmt_name)
        header = self.header if self.header is not None else (
            self.blocks()[0].clone_empty() if self._cache else Block()
        )
        out = _StringWriter()
        fmt.write_prefix(header, out, self.ctx.settings if self.ctx else None)
        for block in self.blocks():
            fmt.write(block, out, self.ctx.settings if self.ctx else None)
        fmt.write_suffix(header, out, self.ctx.settings if self.ctx else None)
        text = out.getvalue()
        if format_name is None:
            self._text = text
        return text

    def as_dataframe(self) -> Any:
        """可选:转成 pandas DataFrame(没装 pandas 就报清楚的错)。"""
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                "QueryResult.as_dataframe() requires pandas to be installed",
            )
        return pd.DataFrame(self.rows(), columns=self.column_names())

    # ── 诊断 ────────────────────────────────────────────────────────────

    def profile_events(self) -> Dict[str, int]:
        return dict(self.info.profile_events)

    def elapsed_seconds(self) -> float:
        return self.info.elapsed

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<QueryResult {self.query_id[:8]} {self.row_count()} rows, "
            f"{len(self.column_names())} columns, {self.elapsed_seconds():.4f}s>"
        )

    def __iter__(self) -> Iterator[Tuple[Any, ...]]:
        return iter(self.rows())

    def __len__(self) -> int:
        return self.row_count()


class _StringWriter:
    """一个最小的"可写文本流" —— 格式输出器只需要 ``write``。"""

    __slots__ = ("_parts",)

    def __init__(self) -> None:
        self._parts: List[str] = []

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)

    def getvalue(self) -> str:
        return "".join(self._parts)


class Context:
    """服务端上下文 —— mini-clickhouse 的"一个服务端实例"。

    .. code-block:: python

       ctx = Context()                       # 内存目录 + 默认 settings
       ctx = Context(data_path=".data")      # 把 MergeTree 的 part 落到磁盘
       ctx.execute("SELECT 1").scalar()      # → 1

    ``Context`` 同时是**对外 API 的入口** —— 教程里所有例子都从
    ``Context()`` 开始。这对应真实 ClickHouse 里的 ``clickhouse-server``
    进程;区别是我们没有网络层(除非走 :mod:`minich.client`)。
    """

    def __init__(
        self,
        data_path: Optional[str] = None,
        database: str = "default",
        user: str = "default",
        settings: Any = None,
        catalog: Optional[DatabaseCatalog] = None,
    ) -> None:
        # ── 目录与存储 ──────────────────────────────────────────────────
        self.catalog = catalog if catalog is not None else DatabaseCatalog()
        #: 引擎的磁盘根目录。**永远是一个真实路径** —— 见下面的说明。
        #:
        #: .. warning::
        #:
        #:    ``data_path=None`` 曾经被解释成"纯内存、没有持久化",
        #:    而 MergeTree 在拿不到磁盘时会退回到一个**固定的**系统临时目录
        #:    (``<tmp>/minich-clickhouse-data``)。两处说法互相矛盾,真实
        #:    行为是后者,后果很具体:
        #:
        #:    * 同一个进程里建两个 ``Context()`` —— 它们共用同一份数据;
        #:    * 上一次运行留下的 part 会在下一次启动时被自动加载回来,
        #:      于是 ``SELECT count()`` 越跑越大、``CREATE TABLE`` 不再幂等;
        #:    * 测试之间互相污染(项目的测试套件里真的因此出现过
        #:      "单独跑通过、一起跑失败")。
        #:
        #:    现在:**每个 Context 拿到自己的临时目录**,``shutdown()``
        #:    时删掉。语义是"这次会话的数据只属于这次会话"。
        #:    要持久化就显式传 ``data_path``,那时目录归调用方所有,我们不删。
        if data_path:
            self.data_path = data_path
            self._owned_data_path: Optional[str] = None
        else:
            import tempfile

            self.data_path = tempfile.mkdtemp(prefix="minich-session-")
            self._owned_data_path = self.data_path
        #: 引擎可用的"磁盘"。
        self.disk = _make_disk(self.data_path)

        # ── 会话状态 ────────────────────────────────────────────────────
        self.database = database
        self.user = user
        self.session_id = f"session-{id(self):x}"
        self.session_start_time = _dt.datetime.now()

        # ── settings ────────────────────────────────────────────────────
        from ..settings import Settings

        self.settings = settings if settings is not None else Settings()
        #: 只读标记 —— ``SET readonly = 1`` 之后不允许写
        self._readonly = False

        # ── 统计与日志 ──────────────────────────────────────────────────
        #: **累积**计数器(server 级)—— ``system.events``
        self.profile_events = ProfileEvents()
        #: **瞬时**值 —— ``system.metrics``
        self.metrics = MetricsRegistry()
        self.logger = get_logger("minich")
        #: 已完成的查询(``system.query_log`` 的数据源)
        self.query_log: List[QueryInfo] = []
        #: 正在跑的查询(``system.processes``)—— query_id → QueryInfo
        self.processes: Dict[str, QueryInfo] = {}
        self._query_counter = 0
        self._lock = threading.RLock()

        # ── 可选的外部设施 ──────────────────────────────────────────────
        #: Keeper 服务(复制表用;为 None 时复制表退化成单副本)
        self.keeper: Any = None
        #: 集群拓扑(``Distributed`` 引擎与 remote() 表函数用)
        self.clusters: Dict[str, Any] = {}
        #: 分布式查询的"远端执行器" —— 由 cluster 模块注入
        self.remote_executor: Any = None
        #: 字典集合
        self.dictionaries: Dict[str, Any] = {}
        #: 访问控制
        self.access: Any = None

        self.metrics.set("BackgroundMergesAndMutationsPoolTask", 0)
        self.metrics.set("TCPConnection", 0)
        self.metrics.set("Query", 0)

    # ── 派生上下文 ──────────────────────────────────────────────────────

    def clone(self, **overrides: Any) -> "Context":
        """派生一个子上下文 —— 共享目录/磁盘/统计,**但有自己的 settings**。

        这就是"查询级 settings 覆盖会话级"的落地方式:
        解释器为每条查询 ``clone()`` 一份,把 ``SETTINGS`` 子句应用到
        副本上,查询结束就丢掉,会话级设置不受影响。
        """
        child = Context.__new__(Context)
        child.__dict__.update(self.__dict__)
        # 需要**独立**的东西
        child.settings = self.settings.clone()
        child.processes = self.processes          # 共享(server 级视图)
        child.query_log = self.query_log          # 共享
        child.profile_events = self.profile_events
        child.metrics = self.metrics
        child._lock = self._lock
        for key, value in overrides.items():
            setattr(child, key, value)
        return child

    def with_settings(self, settings: Any) -> "Context":
        return self.clone(settings=settings)

    def set_current_database(self, database: str) -> None:
        self.catalog.get_database(database)  # 不存在直接抛 UNKNOWN_DATABASE
        self.database = database

    # ── 查询入口 ────────────────────────────────────────────────────────

    def new_query_id(self) -> str:
        """生成查询 ID。

        真实 ClickHouse 的 ID 是一个随机 UUID;mini-clickhouse 用一个
        "进程内自增 + 时间戳"的短 ID,便于在教程输出里肉眼对照。
        """
        with self._lock:
            self._query_counter += 1
            n = self._query_counter
        return f"{int(time.time() * 1000) % 1000000:06d}-{n:04d}"

    def execute(self, sql: str, settings: Optional[Dict[str, Any]] = None) -> QueryResult:
        """执行一条 SQL —— **对外的主入口**。

        .. code-block:: python

           ctx.execute("SELECT 1 + 1").scalar()        # 2
           ctx.execute("SELECT * FROM t").text()       # PrettyCompact 文本

        ``settings`` 参数对应真实客户端的"每条查询附带设置",
        优先级**高于**会话级设置(与 ``SETTINGS`` 子句同级)。
        """
        from .interpreter import execute_query

        if settings:
            ctx = self.with_settings(self.settings.clone())
            ctx.settings.apply_dict(settings, scope="query")
        else:
            ctx = self
        return execute_query(ctx, sql)

    def execute_many(self, *statements: str) -> List[QueryResult]:
        """依次执行多条语句。任一条失败即抛出(前面的**不会**回滚)。"""
        return [self.execute(s) for s in statements]

    def show(self, sql: str, format_name: str = "PrettyCompact") -> str:
        """执行并直接返回文本 —— 交互式与文档演示里最顺手的一个方法。"""
        return self.execute(sql).text(format_name)

    def print(self, sql: str, format_name: str = "PrettyCompact") -> None:  # pragma: no cover
        print(self.show(sql, format_name))

    # ── 便捷建表 ────────────────────────────────────────────────────────

    def create_table(self, name: str, columns_sql: str, engine_sql: str = "Memory",
                     database: Optional[str] = None) -> Table:
        """一句话建表 —— 测试与示例里少写很多 SQL。"""
        db = database or self.database
        sql = f"CREATE TABLE {db}.{name} ({columns_sql}) ENGINE = {engine_sql}"
        if engine_sql.strip().lower().startswith("mergetree") and "order by" not in engine_sql.lower():
            sql += " ORDER BY tuple()"
        self.execute(sql)
        return self.catalog.get_table(db, name)

    def insert(self, table: str, rows: Sequence[Sequence[Any]],
               columns: Optional[Sequence[str]] = None) -> QueryResult:
        """按行插入 —— 会自动按列的**声明类型**格式化字面量。

        .. warning::

           这里做的是"值 → SQL 字面量 → 解析回来"的往返,与真实客户端
           走二进制协议不同。它让示例代码好写,但**不是**高性能路径 ——
           别用它插一百万行。真要批量插入,用
           :meth:`insert_block`。
        """
        db, tbl = _split_table_name(table, self.database)
        t = self.catalog.get_table(db, tbl)
        names = list(columns) if columns else [c.name for c in t.columns if c.is_stored()]
        cols = ", ".join(f"`{n}`" for n in names)
        types = [t.get_column(n).type for n in names]
        value_rows = []
        for row in rows:
            if len(row) != len(names):
                raise_error(
                    ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                    f"Expected {len(names)} values, got {len(row)}",
                )
            value_rows.append(
                "(" + ", ".join(
                    _literal_for(tp, v) for tp, v in zip(types, row)
                ) + ")"
            )
        sql = f"INSERT INTO {db}.{tbl} ({cols}) VALUES " + ", ".join(value_rows)
        return self.execute(sql)

    def insert_block(self, table: str, block: Block) -> None:
        """直接写入一个 ``Block``(绕过 SQL 解析,是**真正的**快路径)。"""
        db, tbl = _split_table_name(table, self.database)
        t = self.catalog.get_table(db, tbl)
        t.write(self, block)
        self.profile_events.increment("InsertedRows", block.rows())
        self.profile_events.increment("InsertedBytes", sum(
            c.column.byte_size() for c in block.data
        ))

    # ── 生命周期 ────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """关闭所有表引擎(停后台线程、flush buffer)。

        **必须幂等** —— 测试里 ``finally: ctx.shutdown()`` 很常见,
        而有些测试会提前失败并在另一个 finally 里再关一次。
        """
        with self._lock:
            for table in self.catalog.all_tables():
                if table.engine is not None:
                    try:
                        table.engine.shutdown()
                    except Exception:  # pragma: no cover
                        pass
            self.metrics.set("Query", 0)
            owned = getattr(self, "_owned_data_path", None)
            if owned:
                # 只删**自己建的**那个临时目录;调用方给的 data_path
                # 归调用方所有 —— 删掉别人的数据是不可逆的破坏。
                import shutil

                shutil.rmtree(owned, ignore_errors=True)
                self._owned_data_path = None

    def __enter__(self) -> "Context":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()

    # ── 统计 ────────────────────────────────────────────────────────────

    def uptime(self) -> int:
        return int((_dt.datetime.now() - self.session_start_time).total_seconds())

    def stats(self) -> Dict[str, Any]:
        return {
            "databases": len(self.catalog.databases),
            "tables": len(self.catalog.all_tables()),
            "queries": int(self.metrics.get("Query")),
            "uptime": self.uptime(),
        }

    def __repr__(self) -> str:  # pragma: no cover
        s = self.stats()
        return (
            f"<Context db={self.database} user={self.user} "
            f"tables={s['tables']} queries={s['queries']}>"
        )


# ══════════════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════════════


def _make_disk(data_path: Optional[str]) -> Any:
    """按需创建磁盘对象(``data_path`` 一定是真实路径)。

    目录的**归属**由 :class:`Context` 决定:调用方给了 ``data_path``,
    那就是他的目录,我们不删;调用方没给,Context 会自己建一个临时目录
    并在 ``shutdown()`` 时删掉(见 ``Context.__init__`` 的说明)。
    """
    try:
        from ..storages.merge_tree.disk import DiskLocal

        return DiskLocal(data_path)
    except ImportError:  # pragma: no cover - MergeTree 未装载时的降级
        import os

        os.makedirs(data_path, exist_ok=True)
        return None


def _split_table_name(name: str, default_database: str) -> Tuple[str, str]:
    if "." in name:
        db, _, tbl = name.partition(".")
        return db.strip("`"), tbl.strip("`")
    return default_database, name.strip("`")


def _literal_for(dtype: Any, value: Any) -> str:
    """把一个 Python 值渲染成 SQL 字面量,类型由 ``dtype`` 决定。"""
    from ..types import composite as _c

    if value is None:
        return "NULL"
    if isinstance(dtype, _c.DataTypeNullable):
        if value is None:
            return "NULL"
        return _literal_for(dtype.nested, value)
    if isinstance(dtype, _c.DataTypeLowCardinality):
        # ``LowCardinality`` 只是个**外壳**(字典编码),值的 SQL 表示与
        # 里面的类型一模一样 —— 必须拆壳。
        #
        # .. warning::
        #
        #    不拆壳时,``LowCardinality(String)`` 走不到下面那条
        #    ``dtype.is_string()``(它在这个外壳上返回 False),
        #    于是落到最后一行 ``return str(value)``,生成一个**没有引号**
        #    的字面量 ``x`` —— ``ctx.insert`` 拼出来的
        #    ``INSERT INTO t VALUES (1, x)`` 解析回来就是
        #    ``Code:47 Unknown identifier: 'x'``。
        #    (走显式 ``INSERT INTO t VALUES (1, 'x')`` 反而没事 ——
        #     那条路径根本不经过这里。)
        #
        #    嵌套顺序两种都要照顾:``Nullable(LowCardinality(String))``
        #    先命中上面的 Nullable 分支再落到这里;而
        #    ``LowCardinality(Nullable(String))`` 在本引擎里被 types 层
        #    直接拒绝(见 DataTypeLowCardinality.__init__)。
        return _literal_for(dtype.nested, value)
    if isinstance(dtype, _c.DataTypeArray):
        return "[" + ", ".join(_literal_for(dtype.element, v) for v in value) + "]"
    if isinstance(dtype, _c.DataTypeTuple):
        return "(" + ", ".join(
            _literal_for(t, v) for t, v in zip(dtype.elements, value)
        ) + ")"
    if isinstance(dtype, _c.DataTypeMap):
        return "{" + ", ".join(
            f"{_literal_for(dtype.key, k)}: {_literal_for(dtype.value, v)}"
            for k, v in value.items()
        ) + "}"
    if dtype.is_string() or dtype.is_date() or dtype.is_date_time() \
            or dtype.is_uuid() or dtype.is_enum():
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
