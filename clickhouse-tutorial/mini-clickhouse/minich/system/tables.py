"""**结构与元数据**类系统表 —— ``databases`` / ``tables`` / ``columns`` / ``parts`` 等。

这一组表回答的是"我的库里有什么、它们长什么样、数据在磁盘上被切成了几块"。
它们是 ClickHouse 排障的**第一站**:拿到一个陌生实例,先跑

.. code-block:: sql

   SELECT name, engine, total_rows, total_bytes FROM system.tables
   WHERE database NOT IN ('system', 'INFORMATION_SCHEMA')
   ORDER BY total_bytes DESC LIMIT 20;

就能知道"哪张大表在占地方"。再跑 ``system.parts`` 看它被切成了多少块,
就能回答"为什么插入越来越慢"(too many parts)。

.. code-block:: text

   system.databases ── 有哪些库,用的什么引擎(Atomic?Ordinary?)
        │
        ▼
   system.tables    ── 每张表的引擎、主键、行数、磁盘占用
        │
        ├─► system.columns ── 每张表的每一列(类型/默认值/注释/编码)
        │
        └─► system.parts   ── MergeTree 表在磁盘上的 part 清单
                              (行数、字节数、分区、level)

.. note::

   **mini-clickhouse 的目录是内存态的**(见 :mod:`minich.catalog`),所以
   ``data_path`` / ``metadata_path`` 这类"磁盘路径"列大多为空 ——
   我们没有落盘元数据这件事,与其编一个假的路径,不如如实留空。
   差异会逐表写在 docstring 里。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .base import SimpleSystemTable, register_system_table, to_text

__all__ = [
    "SystemDatabases",
    "SystemTables",
    "SystemColumns",
    "SystemParts",
    "SystemPartLog",
    "SystemMutations",
    "SystemMerges",
    "SystemMergeTreeSettings",
    "SystemDictionaries",
]


# ══════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════


def _safe_stats(engine: Any) -> Dict[str, Any]:
    """调 ``engine.describe_stats()`` 并吞掉异常 —— 元数据查询不该把查询拖挂。"""
    if engine is None:
        return {}
    try:
        stats = engine.describe_stats()
    except Exception:  # pragma: no cover - 引擎内部出问题时降级
        return {}
    return stats if isinstance(stats, dict) else {}


def _engine_name(table: Any) -> str:
    """``system.tables.engine`` 的取值。没有引擎时真实 CH 显示 ``None``。"""
    engine = getattr(table, "engine", None)
    if engine is None:
        return "None"
    return getattr(engine, "engine_name", None) or type(engine).__name__


def _engine_full(table: Any) -> str:
    """``engine_full`` —— 带参数的完整引擎名,如 ``MergeTree`` / ``Distributed('c','d','t')``。"""
    engine = getattr(table, "engine", None)
    if engine is None:
        return "None"
    try:
        return engine.get_name()
    except Exception:  # pragma: no cover
        return _engine_name(table)


def _key_desc(table: Any, method: str) -> str:
    engine = getattr(table, "engine", None)
    if engine is None:
        return ""
    fn = getattr(engine, method, None)
    if fn is None:
        return ""
    try:
        return fn() or ""
    except Exception:  # pragma: no cover
        return ""


def _all_catalog_tables(ctx: Any) -> Iterator[Any]:
    """遍历目录里**所有**库的所有表(按 ``(库名, 表名)`` 排序,保证确定性)。"""
    catalog = getattr(ctx, "catalog", None)
    if catalog is None:
        return
    for db_name in catalog.database_names():
        for table in sorted(catalog.tables_of(db_name), key=lambda t: t.name):
            yield table


# ══════════════════════════════════════════════════════════════════════════
# system.databases
# ══════════════════════════════════════════════════════════════════════════


class SystemDatabases(SimpleSystemTable):
    """``system.databases`` —— 回答"这个实例上有哪些库、各自用什么引擎"。

    生产里最常用的两个用途:

    * ``SELECT name, engine FROM system.databases`` —— 确认有没有人还在用
      老的 ``Ordinary`` 库(它的 DDL 会互相加锁,是大集群的常见坑);
    * 配合 ``system.tables`` 做"按库统计容量"。

    .. note::

       ``uuid`` / ``data_path`` / ``metadata_path`` 在 mini-clickhouse 里
       **恒为空字符串** —— 目录是内存态的,没有落盘元数据,也没有给库分配
       UUID。真实 ClickHouse 的 ``Atomic`` 库这些列都是实打实的路径。
    """

    name = "databases"
    columns = [
        ("name", "String"),
        ("engine", "String"),
        ("data_path", "String"),
        ("metadata_path", "String"),
        ("uuid", "String"),
        ("comment", "String"),
        ("create_time", "DateTime"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        catalog = getattr(ctx, "catalog", None)
        if catalog is None:
            return
        for db_name in catalog.database_names():
            db = catalog.databases[db_name]
            yield (
                db.name,
                db.engine,
                "",                      # data_path —— 未落盘,如实留空
                "",                      # metadata_path —— 同上
                "",                      # uuid —— 库没有 UUID
                getattr(db, "comment", "") or "",
                getattr(db, "create_time", "1970-01-01 00:00:00"),
            )


# ══════════════════════════════════════════════════════════════════════════
# system.tables
# ══════════════════════════════════════════════════════════════════════════


class SystemTables(SimpleSystemTable):
    """``system.tables`` —— 回答"每张表用什么引擎、多大、按什么键组织"。

    这一张表是整个可观测性章节的核心:

    .. code-block:: sql

       -- 容量 Top 10
       SELECT database, name, total_rows, total_bytes,
              formatReadableSize(total_bytes) AS size
       FROM system.tables ORDER BY total_bytes DESC LIMIT 10;

       -- 找出所有 MergeTree 表的主键与分区键
       SELECT name, partition_key, sorting_key, primary_key
       FROM system.tables WHERE engine = 'MergeTree';

    ``engine`` 只是引擎族名(``MergeTree``),``engine_full`` 才带参数。
    真实 ClickHouse 里这两者的区别经常被搞混 —— 例如
    ``ENGINE = MergeTree ORDER BY x`` 的 ``engine`` 是 ``MergeTree``,
    ``sorting_key`` 是 ``x``;而 ``ENGINE = ReplicatedMergeTree(...)``
    的 ``engine_full`` 才是 ``ReplicatedMergeTree('/path', 'replica')``。

    .. note::

       **mini-clickhouse 只列出目录里真实注册的表。** 我们的
       ``system.*`` 是虚拟表(不经过 :class:`minich.catalog.DatabaseCatalog`),
       所以不像真实 ClickHouse 那样出现在这里。这是有意为之 ——
       与其编一个引擎名,不如如实不列。
       ``total_rows`` / ``total_bytes`` 走引擎的 ``row_count()`` /
       ``size_in_bytes()``(对 MergeTree 是"读元数据",不是扫数据)。
    """

    name = "tables"
    columns = [
        ("database", "String"),
        ("name", "String"),
        ("uuid", "String"),
        ("engine", "String"),
        ("is_temporary", "UInt8"),
        ("data_paths", "Array(String)"),
        ("metadata_path", "String"),
        ("create_table_query", "String"),
        ("engine_full", "String"),
        ("partition_key", "String"),
        ("sorting_key", "String"),
        ("primary_key", "String"),
        ("sampling_key", "String"),
        ("storage_policy", "String"),
        ("total_rows", "UInt64"),
        ("total_bytes", "UInt64"),
        ("comment", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for table in _all_catalog_tables(ctx):
            stats = _safe_stats(getattr(table, "engine", None))
            path = stats.get("path")
            data_paths = [str(path)] if path else []
            try:
                total_rows = int(table.row_count())
            except Exception:  # pragma: no cover
                total_rows = 0
            try:
                total_bytes = int(table.size_in_bytes())
            except Exception:  # pragma: no cover
                total_bytes = 0
            yield (
                table.database,
                table.name,
                getattr(table.storage_id, "uuid", "") or "",
                _engine_name(table),
                0,                                   # is_temporary —— 临时表不在这里
                data_paths,
                "",                                  # metadata_path —— 未落盘
                getattr(table, "metadata_sql", "") or "",
                _engine_full(table),
                _key_desc(table, "get_partition_key_description"),
                _key_desc(table, "get_sorting_key_description"),
                _key_desc(table, "get_primary_key_description"),
                "",                                  # sampling_key —— SAMPLE 未实现
                "default",                           # storage_policy
                total_rows,
                total_bytes,
                getattr(table, "comment", "") or "",
            )


# ══════════════════════════════════════════════════════════════════════════
# system.columns
# ══════════════════════════════════════════════════════════════════════════


class SystemColumns(SimpleSystemTable):
    """``system.columns`` —— 回答"某张表每一列的类型、默认值、编码是什么"。

    ``DESCRIBE TABLE`` 是它的"人读版";真正大规模排查时用的是 SQL:

    .. code-block:: sql

       -- 找出所有还在用 String 存时间戳的列(该换成 DateTime)
       SELECT database, table, name
       FROM system.columns WHERE type = 'String' AND name LIKE '%time%';

       -- 谁在用 Nullable 把内存撑大(Nullable 会多存一个空位图)
       SELECT database, table, name, type
       FROM system.columns WHERE type LIKE 'Nullable%';

    ``default_kind`` 的四种取值(DEFAULT / MATERIALIZED / ALIAS / EPHEMERAL)
    语义完全不同,见 :class:`minich.catalog.ColumnDefinition`。

    .. note::

       与 ``system.tables`` 不同,这里**同时列出目录里的表和 system 库的
       虚拟表** —— 后者的列是真实存在的(它们就是各系统表的 schema),
       列出来才能让"``SELECT * FROM system.tables`` 到底有哪些列"
       用 SQL 自问自答。
    """

    name = "columns"
    columns = [
        ("database", "String"),
        ("table", "String"),
        ("name", "String"),
        ("type", "String"),
        ("position", "UInt64"),
        ("default_kind", "String"),
        ("default_expression", "String"),
        ("comment", "String"),
        ("codec_expression", "String"),
        ("ttl_expression", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for table in _all_catalog_tables(ctx):
            for pos, col in enumerate(table.columns, start=1):
                yield (
                    table.database,
                    table.name,
                    col.name,
                    col.type.get_name(),
                    pos,
                    col.default_kind or "",
                    col.default_expr_sql or "",
                    col.comment or "",
                    col.codec or "",
                    col.ttl_sql or "",
                )
        # ── system 库的虚拟表(列是真实存在的,所以如实列出)──
        from .base import registry

        reg = registry()
        for tname in sorted(reg):
            try:
                header = reg[tname].get_header(ctx)
            except Exception:  # pragma: no cover - 降级:跳过这一张
                continue
            for pos, cwt in enumerate(header.data, start=1):
                yield (
                    "system",
                    tname,
                    cwt.name,
                    cwt.type.get_name(),
                    pos,
                    "",          # default_kind
                    "",          # default_expression
                    "",          # comment
                    "",          # codec_expression
                    "",          # ttl_expression
                )


# ══════════════════════════════════════════════════════════════════════════
# system.parts
# ══════════════════════════════════════════════════════════════════════════


class SystemParts(SimpleSystemTable):
    """``system.parts`` —— 回答"数据在磁盘上被切成了多少块、每块多大"。

    这是排查 **"too many parts"** 的第一站(也是唯一一站):

    .. code-block:: sql

       -- 每个分区的活跃 part 数与总大小
       SELECT database, table, partition,
              count() AS parts, sum(rows) AS rows, sum(bytes_on_disk) AS bytes
       FROM system.parts WHERE active
       GROUP BY database, table, partition
       ORDER BY parts DESC LIMIT 10;

    ``active = 0`` 的行是**已被合并取代、等待被删除**的旧 part ——
    真实 ClickHouse 保留它们一段时间(``old_parts_lifetime``,默认 480 秒),
    因为可能还有正在跑的查询在读。这解释了"磁盘占用比数据量大"
    的一个常见原因:新旧 part 同时存在。

    ``part_type`` 是 ``Wide``(每列一个文件)还是 ``Compact``(所有列一个文件),
    由 ``min_bytes_for_wide_part``(默认 10 MiB)决定 —— 小 part 用 Compact
    能省文件句柄,这是 ClickHouse 写小批量数据时的重要优化。

    .. note::

       **``database`` 与 ``table`` 是前两列,而且必须排在最前面。**
       没有它们,你只能写"全库所有 part 的汇总"
       (``SELECT partition, sum(rows) FROM system.parts GROUP BY partition``),
       **没法把范围收窄到一张表** —— 而排查 "too many parts" 时,
       99% 的情况都是"某一张表的某个分区炸了"。真实 ClickHouse 的
       ``system.parts`` 同样把这两列放在最前面,原因就在这里。

       **这张表的数据全部来自存储引擎自己的 part 清单。**
       mini-clickhouse 里**只有 MergeTree 家族**维护 part(要有磁盘布局
       才有 part 这个概念);``Memory`` / ``Log`` / ``Null`` 等引擎没有
       part,于是它们在这里**一行都不出现** —— 这是"降级为空"而不是
       "报错",因为"没有 part"本身就是一个合法答案。
       如果引擎连 ``describe_stats()`` 都没实现,同样降级为空。
    """

    name = "parts"
    columns = [
        ("database", "String"),
        ("table", "String"),
        ("partition", "String"),
        ("name", "String"),
        ("part_type", "String"),
        ("active", "UInt8"),
        ("marks", "UInt64"),
        ("rows", "UInt64"),
        ("bytes_on_disk", "UInt64"),
        ("data_compressed_bytes", "UInt64"),
        ("data_uncompressed_bytes", "UInt64"),
        ("primary_key_bytes_in_memory", "UInt64"),
        ("min_date", "Date"),
        ("max_date", "Date"),
        ("min_block_number", "Int64"),
        ("max_block_number", "Int64"),
        ("level", "UInt32"),
        ("primary_key_size", "UInt64"),
        ("disk_name", "String"),
        ("path", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for table in _all_catalog_tables(ctx):
            engine = getattr(table, "engine", None)
            for part in _iter_parts(engine):
                yield _part_row(table, part)


def _iter_parts(engine: Any) -> List[Any]:
    """尽最大努力把一张表的 part 清单掏出来 —— 掏不到就返回空列表。

    支持四种约定(按"信息量"从高到低找):

    1. ``engine.parts_rows()`` —— 引擎直接给出 ``system.parts`` 的行
       (**MergeTree 走的就是这条**,每行已是 dict);
    2. ``describe_stats()["parts_list"]`` —— 引擎报告的 part 对象列表;
    3. ``engine.parts`` —— 一个 list 或 ``{名字: part}`` 的 dict;
    4. ``engine.get_parts()`` / ``active_parts()`` / ``all_parts()`` —— 方法。

    .. warning::

       ``describe_stats()["parts"]`` 是**个数**(int),不是清单 ——
       很多引擎都用这个约定,所以这里刻意**跳过**整数,避免把
       "3 个 part" 当成"3 行 part"。读不到清单就返回空列表,
       让上层优雅降级(``Memory``/``Log`` 这些引擎本来就没有 part)。
    """
    if engine is None:
        return []
    # ① 引擎直接给 system.parts 的行(最精确)
    rows_fn = getattr(engine, "parts_rows", None)
    if callable(rows_fn):
        try:
            rows = rows_fn()
        except Exception:  # pragma: no cover
            rows = None
        if rows:
            return _as_list(rows)
    # ② describe_stats 里的 part 清单(注意绕开 int 的 "parts")
    stats = _safe_stats(engine)
    for key in ("parts_list", "part_list", "active_parts_list"):
        value = stats.get(key)
        if value is not None and not isinstance(value, int):
            return _as_list(value)
    # ③ / ④ 引擎属性或方法
    for attr in ("parts", "get_parts", "active_parts", "all_parts"):
        value = getattr(engine, attr, None)
        if value is None:
            continue
        try:
            value = value() if callable(value) else value
        except Exception:  # pragma: no cover
            continue
        if value is not None and not isinstance(value, int):
            return _as_list(value)
    return []


def _as_list(parts: Any) -> List[Any]:
    if isinstance(parts, dict):
        return list(parts.values())
    if isinstance(parts, (list, tuple, set)):
        return list(parts)
    try:
        return list(parts)
    except TypeError:  # pragma: no cover
        return []


def _field(part: Any, name: str, default: Any = None) -> Any:
    """从 part(对象或 dict)里取一个字段。"""
    if isinstance(part, dict):
        return part.get(name, default)
    return getattr(part, name, default)


def _field_any(part: Any, names: Sequence[str], default: Any = None) -> Any:
    """按一组**别名**取字段,返回第一个存在的。

    为什么需要它:不同的 part 表示用了不同的命名 —— MergeTree 的
    ``MergeTreePart.get_stats()`` 用的是 ``bytes_on_disk`` / ``type`` /
    ``partition``(真实 ClickHouse 的列名),而 :class:`minich.replication.PartInfo`
    用的是 ``size_bytes`` / ``partition_id``。系统表要能同时吃下这两种,
    所以按别名列表逐个找,找到就用 —— **不猜、不合并、不编**。
    """
    for name in names:
        value = _field(part, name, None)
        if value is not None:
            return value
    return default


def _part_row(table: Any, part: Any) -> Tuple[Any, ...]:
    """把一个 part 映射成 ``system.parts`` 的一行。

    字段名同时兼容真实 ClickHouse 的列名(``bytes_on_disk`` / ``partition``
    / ``type``)与 :class:`minich.replication.PartInfo` 的命名
    (``size_bytes`` / ``partition_id``)—— 见 :func:`_field_any`。
    取不到就用该类型的默认值 —— 不猜。
    """
    bytes_on_disk = _field_any(part, ("bytes_on_disk", "size_bytes"), 0) or 0
    name = str(_field_any(part, ("name",), ""))
    # active:优先读 part 自己报告的;没有就看副本状态(PartState.COMMITTED == 3)。
    active = _field(part, "active", None)
    if active is None:
        state = _field(part, "state", None)
        active = 1 if state is None else (1 if int(state) == 3 else 0)
    # 块号区间真实 CH 存在 **part 名字**里(``<partition>_<min>_<max>_<level>``),
    # part 对象不一定单独暴露它们 —— 缺了就按名字解回来(这是最原始的来源)。
    min_block, max_block = _block_range(part, name)
    return (
        str(_field_any(part, ("database",), getattr(table, "database", ""))),
        str(_field_any(part, ("table",), getattr(table, "name", ""))),
        str(_field_any(part, ("partition", "partition_id"), "all")),
        name,
        str(_field_any(part, ("part_type", "type"), "") or ""),
        int(active),
        int(_field_any(part, ("marks", "marks_count"), 0) or 0),
        int(_field_any(part, ("rows",), 0) or 0),
        int(bytes_on_disk),
        int(_field_any(part, ("data_compressed_bytes", "compressed_bytes"),
                       bytes_on_disk) or 0),
        int(_field_any(part, ("data_uncompressed_bytes", "uncompressed_bytes"), 0) or 0),
        int(_field_any(part, ("primary_key_bytes_in_memory",), 0) or 0),
        str(_field_any(part, ("min_date",), "1970-01-01") or "1970-01-01"),
        str(_field_any(part, ("max_date",), "1970-01-01") or "1970-01-01"),
        min_block,
        max_block,
        int(_field_any(part, ("level",), 0) or 0),
        int(_field_any(part, ("primary_key_size",), 0) or 0),
        str(_field_any(part, ("disk_name",), "default") or "default"),
        str(_field_any(part, ("path",), "") or ""),
    )


def _block_range(part: Any, name: str) -> Tuple[int, int]:
    """取 part 的 ``(min_block_number, max_block_number)``。

    优先用 part 自己暴露的字段;没有就从 part 名字里解 —— 名字的格式
    ``<partition>_<min>_<max>_<level>`` 是 ClickHouse 的**稳定约定**,
    解析它和真实 ClickHouse 读自身 part 目录是同一种做法。
    实在解不出来就返回 ``(0, 0)``(而不是编一个看起来合理的区间)。
    """
    lo = _field_any(part, ("min_block_number", "min_block"), None)
    hi = _field_any(part, ("max_block_number", "max_block"), None)
    if lo is not None and hi is not None:
        return int(lo), int(hi)
    parsed = _parse_block_range_from_name(name)
    if parsed is not None:
        return parsed
    return int(lo or 0), int(hi or 0)


def _parse_block_range_from_name(name: str) -> Optional[Tuple[int, int]]:
    parts = name.rsplit("_", 3)
    if len(parts) != 4:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════
# system.part_log
# ══════════════════════════════════════════════════════════════════════════


class SystemPartLog(SimpleSystemTable):
    """``system.part_log`` —— 回答"每次 part 的诞生与死亡是谁干的"。

    真实 ClickHouse 里这是**最好用的一张排障表**:

    .. code-block:: sql

       -- 最近 10 分钟的合并活动
       SELECT event_type, database, table, part_name,
              rows, size_in_bytes, duration_ms
       FROM system.part_log
       WHERE event_time > now() - 600 AND event_type = 'MergeParts'
       ORDER BY event_time DESC;

    它把 part 生命周期的每一件事都记成一行:``NewPart``(插入产生)、
    ``MergeParts``(后台合并)、``DownloadPart``(副本拉取)、
    ``RemovePart``(被删)、``MutatePart``(mutation)。所以
    "为什么磁盘突然涨了""为什么合并一直不收敛"都能在这里找到答案。

    .. note::

       **数据来自引擎自己的 part 事件清单**(``engine.part_log``)。当
       MergeTree 引擎挂了后台合并、或有 part 被写入/删除时,它会把这些
       事件攒成行;系统表把它们如实转出来。

       **如果一个引擎没有接上 part_log sink,这张表就是空的** —— 这是
       "降级为空",而不是 "永远为空":mini-clickhouse 的 MergeTree 是否
       写 part_log,取决于它有没有通过 ``reset_part_log()`` 接上落地处。
       没有事件时保留正确的 schema,是为了让 ``SELECT *`` 不报错,
       并把"这张表本该有什么"讲清楚。
    """

    name = "part_log"
    columns = [
        ("host_name", "String"),
        ("event_type", "String"),
        ("event_time", "DateTime"),
        ("duration_ms", "UInt64"),
        ("database", "String"),
        ("table", "String"),
        ("part_name", "String"),
        ("partition_id", "String"),
        ("rows", "UInt64"),
        ("size_in_bytes", "UInt64"),
        ("merged_from", "UInt64"),
        ("error", "UInt32"),
        ("exception", "String"),
        ("disk_name", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for table in _all_catalog_tables(ctx):
            engine = getattr(table, "engine", None)
            events = getattr(engine, "part_log", None) if engine is not None else None
            if not events:
                continue
            for event in _as_list(events):
                yield _part_log_row(table, event)


def _part_log_row(table: Any, event: Any) -> Tuple[Any, ...]:
    """把一个 part 事件(引擎给的 dict)映射成 ``system.part_log`` 的一行。"""
    merged_from = _field_any(event, ("merged_from",), None)
    if merged_from is None:
        sources = _field(event, "parts", None)
        merged_from = len(sources) if isinstance(sources, (list, tuple)) else 0
    return (
        str(_field_any(event, ("host_name",), "localhost")),
        str(_field_any(event, ("event_type", "type"), "") or ""),
        str(_field_any(event, ("event_time",), "1970-01-01 00:00:00")),
        int(_field_any(event, ("duration_ms",), 0) or 0),
        str(_field_any(event, ("database",), getattr(table, "database", ""))),
        str(_field_any(event, ("table",), getattr(table, "name", ""))),
        str(_field_any(event, ("part_name", "name"), "") or ""),
        str(_field_any(event, ("partition_id", "partition"), "") or ""),
        int(_field_any(event, ("rows",), 0) or 0),
        int(_field_any(event, ("size_in_bytes", "bytes_on_disk"), 0) or 0),
        int(merged_from or 0),
        int(_field_any(event, ("error",), 0) or 0),
        str(_field_any(event, ("exception",), "") or ""),
        str(_field_any(event, ("disk_name",), "default") or "default"),
    )


# ══════════════════════════════════════════════════════════════════════════
# system.mutations
# ══════════════════════════════════════════════════════════════════════════


class SystemMutations(SimpleSystemTable):
    """``system.mutations`` —— 回答"我那条 ``ALTER ... DELETE`` 到底做完了没有"。

    这就是这张表存在的**全部理由**:ClickHouse 的 ``ALTER TABLE ... DELETE``
    / ``UPDATE`` 是**异步**的 —— 语句返回的那一刻,mutation 只是"登记"了,
    真正改写数据要等后台合并一个个 part 去套用它。所以:

    .. code-block:: sql

       -- ① 我提交的 mutation 完成了吗?(is_done = 1 才算)
       SELECT mutation_id, command, is_done, parts_to_do, latest_fail_reason
       FROM system.mutations WHERE table = 'hits';

       -- ② 如果没完成:是哪几个 part 卡住了?
       SELECT mutation_id, parts_to_do_names FROM system.mutations
       WHERE NOT is_done;

    **``parts_to_do`` 是这里最该盯的数**:

    * 它一直不为 0 ⇒ mutation **还没生效完**(查出来的旧数据不是 bug);
    * 它**长时间**不为 0 ⇒ 去看 ``system.merges`` 有没有合并在跑 ——
      mutation 只能靠合并推进,合并停了 mutation 也就停了;
    * ``latest_fail_reason`` 非空 ⇒ 直接告诉你失败原因(最常见的是
      磁盘满或某个 part 损坏)。

    ``is_killed`` 对应 ``KILL MUTATION``:被显式杀掉的 mutation 保留在表里,
    标成已杀而不是消失 —— 否则"我提交的那个 ALTER 去哪了"会变成一个谜。

    .. note::

       数据来自 MergeTree 引擎的 mutation 登记表(``engine.mutations``,
       一个 :class:`minich.storages.merge_tree.mutations.MutationRegistry`)。
       **该模块是延迟导入的**:它不在(或引擎不维护 mutation)时本表
       降级为空,绝不因此让查询失败。

       ``is_killed`` 与 ``last_mutation_id`` 在 mini-clickhouse 里**恒为
       0** —— 我们没有实现 ``KILL MUTATION``,也没有记录"某个 part 最后
       应用的 mutation 号"。保留列是为了 schema 与真实 ClickHouse 对齐,
       而不是填一个猜出来的值。
    """

    name = "mutations"
    columns = [
        ("database", "String"),
        ("table", "String"),
        ("mutation_id", "String"),
        ("command", "String"),
        ("create_time", "DateTime"),
        ("block_numbers", "Array(UInt64)"),
        ("parts_to_do", "Int64"),
        ("parts_to_do_names", "Array(String)"),
        ("is_done", "UInt8"),
        ("is_killed", "UInt8"),
        ("latest_failed_part", "String"),
        ("latest_fail_time", "DateTime"),
        ("latest_fail_reason", "String"),
        ("last_mutation_id", "UInt64"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for table in _all_catalog_tables(ctx):
            engine = getattr(table, "engine", None)
            for row in _mutation_rows(engine):
                yield _mutation_row(table, row)


def _mutation_rows(engine: Any) -> List[Any]:
    """把引擎的 mutation 登记表读成一批行;读不到返回空列表。

    **延迟导入** ``minich.storages.merge_tree.mutations`` —— 那个模块(以及
    整个 MergeTree 包)都是可选的,导入失败要安静降级,不能把
    ``SELECT * FROM system.mutations`` 变成一场异常。
    """
    if engine is None:
        return []
    try:
        from ..storages.merge_tree import mutations as _mutations  # noqa: F401
    except Exception:
        return []
    store = getattr(engine, "mutations", None)
    if store is None:
        return []
    fn = getattr(store, "all_rows", None)
    if callable(fn):
        try:
            rows = fn()
        except Exception:  # pragma: no cover
            rows = None
        if rows is not None:
            return list(rows)
    # 退路:直接读 entries
    entries = getattr(store, "entries", None)
    out: List[Any] = []
    for entry in entries or ():
        status_row = getattr(entry, "status_row", None)
        if callable(status_row):
            try:
                out.append(status_row())
            except Exception:  # pragma: no cover
                continue
    return out


def _mutation_row(table: Any, row: Any) -> Tuple[Any, ...]:
    """把一条 mutation(引擎给的 dict)映射成 ``system.mutations`` 的一行。"""
    return (
        str(_field_any(row, ("database",), getattr(table, "database", ""))),
        str(_field_any(row, ("table",), getattr(table, "name", ""))),
        str(_field_any(row, ("mutation_id",), "") or ""),
        str(_field_any(row, ("command",), "") or ""),
        _datetime_or_epoch(_field(row, "create_time")),
        [int(x) for x in (_field(row, "block_numbers", None) or [])],
        int(_field_any(row, ("parts_to_do",), 0) or 0),
        [str(x) for x in (_field(row, "parts_to_do_names", None) or [])],
        int(_field(row, "is_done", 0) or 0),
        int(_field(row, "is_killed", 0) or 0),
        str(_field_any(row, ("latest_failed_part",), "") or ""),
        _datetime_or_epoch(_field(row, "latest_fail_time")),
        str(_field_any(row, ("latest_fail_reason",), "") or ""),
        int(_field(row, "last_mutation_id", 0) or 0),
    )


def _datetime_or_epoch(value: Any) -> str:
    """把一个可能是 ``datetime`` / 字符串 / 空的时间值变成 ``DateTime`` 能收的文本。

    **空值必须换成纪元**,不能留空串 —— ``DateTime`` 的 ``normalize`` 解不了
    ``""``,会抛 ``CANNOT_PARSE_DATETIME``。真实 ClickHouse 在
    "从未发生"的时间列上填的也是 ``1970-01-01 00:00:00``。
    """
    if value is None or value == "":
        return "1970-01-01 00:00:00"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


# ══════════════════════════════════════════════════════════════════════════
# system.merges
# ══════════════════════════════════════════════════════════════════════════


class SystemMerges(SimpleSystemTable):
    """``system.merges`` —— 回答"此刻有哪些后台合并正在跑、跑到哪了"。

    .. code-block:: sql

       SELECT database, table, num_parts, rows_read, rows_written, progress,
              elapsed, source_part_names
       FROM system.merges ORDER BY elapsed DESC;

    它和 ``system.parts`` 是一对:

    .. code-block:: text

       system.parts   数据被切成了多少块      ← 结果(静态)
       system.merges  正在把这些块合并         ← 过程(动态)

    "too many parts" 的完整剧情是:插入太碎 → part 太多 → 后台合并追不上 →
    ``system.merges`` 一直有活、``parts_to_delay_insert`` 触发限速。
    只看 ``system.parts`` 会以为"合并坏了";两张表一起看才知道
    "合并一直在跑,只是数据来得比合并快"。

    .. warning::

       **``system.merges`` 为空是"服务器空闲"的正常状态,不是故障。**

       这是最容易被误读的一张表:一条空闲的实例上它**永远**是 0 行,
       因为合并是"有活才跑"的。看到 0 行就去报警"合并挂了"是常见误判 ——
       正确的判据是"``system.parts`` 里 part 数在涨、但 ``system.merges``
       长期为空",那才说明合并真的停了。

    .. note::

       数据来自引擎暴露的在途合并信息。MergeTree 引擎目前只公开
       **正在参与合并的 part 名字集合**(``_parts_in_merge``),所以
       这张表里只有 ``source_part_names`` / ``num_parts`` /
       ``database`` / ``table`` / ``partition_id`` 是**真实读数**;
       ``elapsed`` / ``progress`` / 各类字节数 / ``thread_id`` 等
       运行时计量**没有采集**,如实填 0 —— 不编。
       如果引擎连在途 part 集合都不暴露,本表降级为空。
    """

    name = "merges"
    columns = [
        ("database", "String"),
        ("table", "String"),
        ("elapsed", "Float64"),
        ("progress", "Float64"),
        ("num_parts", "UInt64"),
        ("result_part_name", "String"),
        ("result_part_path", "String"),
        ("source_part_names", "Array(String)"),
        ("partition_id", "String"),
        ("total_size_bytes_compressed", "UInt64"),
        ("bytes_read_uncompressed", "UInt64"),
        ("rows_read", "UInt64"),
        ("bytes_written_uncompressed", "UInt64"),
        ("rows_written", "UInt64"),
        ("memory_usage", "UInt64"),
        ("thread_id", "UInt64"),
        ("merge_type", "String"),
        ("merge_algorithm", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for table in _all_catalog_tables(ctx):
            engine = getattr(table, "engine", None)
            for merge in _in_flight_merges(engine):
                yield _merge_row(table, merge)


def _in_flight_merges(engine: Any) -> List[Any]:
    """取引擎当前在途的合并。

    先找**公开的状态钩子**(``get_active_merges`` / ``active_merges`` /
    ``merges_status``),有就用它;没有就退回到引擎内部的"正在合并的
    part 名字集合"。都没有 → 空列表(= 空闲,正常状态)。
    """
    if engine is None:
        return []
    for attr in ("get_active_merges", "active_merges", "merges_status",
                 "get_merges_status"):
        fn = getattr(engine, attr, None)
        if callable(fn):
            try:
                rows = fn()
            except Exception:  # pragma: no cover
                rows = None
            if rows is not None:
                return _as_list(rows)
    names = getattr(engine, "_parts_in_merge", None)
    if names:
        try:
            names = sorted(str(n) for n in names)
        except Exception:  # pragma: no cover
            return []
        if names:
            return [{
                "source_part_names": names,
                "num_parts": len(names),
                "partition_id": _partition_from_part_name(names[0]),
            }]
    return []


def _partition_from_part_name(name: str) -> str:
    """从 part 名字 ``<partition>_<min>_<max>_<level>`` 里取出分区号。"""
    parts = name.rsplit("_", 3)
    return parts[0] if len(parts) == 4 else ""


def _merge_row(table: Any, merge: Any) -> Tuple[Any, ...]:
    """把一个在途合并映射成 ``system.merges`` 的一行(未知的计量填 0)。"""
    src = _field(merge, "source_part_names", None)
    if src is None:
        src = _field(merge, "parts", None)
    sources = [str(x) for x in (src or [])]
    num_parts = _field_any(merge, ("num_parts",), len(sources)) or 0
    return (
        str(_field_any(merge, ("database",), getattr(table, "database", ""))),
        str(_field_any(merge, ("table",), getattr(table, "name", ""))),
        float(_field_any(merge, ("elapsed",), 0.0) or 0.0),
        float(_field_any(merge, ("progress",), 0.0) or 0.0),
        int(num_parts),
        str(_field_any(merge, ("result_part_name",), "") or ""),
        str(_field_any(merge, ("result_part_path",), "") or ""),
        sources,
        str(_field_any(merge, ("partition_id", "partition"), "") or ""),
        int(_field_any(merge, ("total_size_bytes_compressed",), 0) or 0),
        int(_field_any(merge, ("bytes_read_uncompressed",), 0) or 0),
        int(_field_any(merge, ("rows_read",), 0) or 0),
        int(_field_any(merge, ("bytes_written_uncompressed",), 0) or 0),
        int(_field_any(merge, ("rows_written",), 0) or 0),
        int(_field_any(merge, ("memory_usage",), 0) or 0),
        int(_field_any(merge, ("thread_id",), 0) or 0),
        str(_field_any(merge, ("merge_type",), "") or ""),
        str(_field_any(merge, ("merge_algorithm",), "") or ""),
    )


# ══════════════════════════════════════════════════════════════════════════
# system.merge_tree_settings
# ══════════════════════════════════════════════════════════════════════════

#: 当 **MergeTree 模块整个缺席**时的退路:从统一设置注册表里,按这份
#: 名字清单筛出"属于 MergeTree 的那些"。这是最后一道保底 ——
#: 正常情况下会用引擎自己导出的 ``DEFAULT_SETTINGS``(见下)。
_MERGE_TREE_SETTING_NAMES = (
    "index_granularity",
    "index_granularity_bytes",
    "enable_mixed_granularity_parts",
    "min_bytes_for_wide_part",
    "min_rows_for_wide_part",
    "max_parts_in_total",
    "parts_to_delay_insert",
    "parts_to_throw_insert",
    "max_part_loading_threads",
    "max_suspicious_broken_parts",
    "merge_tree_min_rows_for_concurrent_read",
    "merge_tree_max_rows_to_use_cache",
    "max_bytes_to_merge_at_max_space_in_pool",
    "old_parts_lifetime",
    "max_number_of_merges_with_ttl_in_pool",
    "ttl_only_drop_parts",
)


class SystemMergeTreeSettings(SimpleSystemTable):
    """``system.merge_tree_settings`` —— 回答"MergeTree 的这批参数现在是多少"。

    和 ``system.settings`` 的区别只在于**归属**:``insert_quorum`` 是
    查询/会话参数,``parts_to_throw_insert`` 是**存储引擎**参数。真实
    ClickHouse 把后者单独放一张表,因为它们由建表时的 ``SETTINGS`` 子句
    或 ``config.xml`` 里的 ``<merge_tree>`` 段控制,**不**随普通 ``SET`` 变化。

    .. code-block:: sql

       SELECT name, value, changed FROM system.merge_tree_settings
       WHERE changed = 1;   -- 谁被改过

    .. note::

       数据来源按优先级三级降级(依次尝试):

       1. MergeTree 模块导出的 ``registered_merge_tree_settings()`` ——
          如果它将来提供的话(预留的接口);
       2. **MergeTree 引擎自己的 ``DEFAULT_SETTINGS``** —— 这是当前
          mini-clickhouse 实际走的路径:引擎在
          :mod:`minich.storages.merge_tree.merge_tree` 里声明了它认的每一条
          设置(名字与真实 ``MergeTreeSettings`` 一致);
       3. MergeTree 模块**整个缺席**时,退回 :mod:`minich.settings`
          里按 :data:`_MERGE_TREE_SETTING_NAMES` 筛。

       无论走哪条,``value`` 都尽量反映"当前生效值"(第 2 条里会用
       ``ctx.settings`` 覆盖引擎默认,这样 ``SET`` 过的项会显示 ``changed = 1``)。
    """

    name = "merge_tree_settings"
    columns = [
        ("name", "String"),
        ("value", "String"),
        ("changed", "UInt8"),
        ("description", "String"),
        ("min", "String"),
        ("max", "String"),
        ("readonly", "UInt8"),
        ("type", "String"),
        ("default", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        # ── ① / ② 引擎侧(注册表 或 DEFAULT_SETTINGS)──
        engine_rows = _merge_tree_settings_from_engine(ctx)
        if engine_rows is not None:
            for row in engine_rows:
                yield tuple(row)
            return
        # ── ③ 退路:MergeTree 模块不在,从统一设置注册表按名字筛 ──
        settings = getattr(ctx, "settings", None)
        if settings is None:
            return
        wanted = set(_MERGE_TREE_SETTING_NAMES)
        for row in settings.as_rows():
            name = row[0]
            if name not in wanted:
                continue
            # as_rows 列顺序:name, value, changed, description, min, max,
            #                 type, default, readonly, is_obsolete, tier
            yield (
                name,
                to_text(row[1]),
                int(row[2]),
                row[3],
                to_text(row[4]),
                to_text(row[5]),
                int(row[8]),      # readonly
                row[6],           # type
                to_text(row[7]),  # default
            )


def _merge_tree_settings_from_engine(ctx: Any) -> Optional[List[Tuple[Any, ...]]]:
    """从引擎侧拿 MergeTree 设置行;引擎模块不在、或什么都不提供时返回 ``None``。

    **延迟导入 + 吞掉异常**:``minich.storages.merge_tree`` 由另一个模块负责,
    它可能不存在(会抛 ``ImportError``),也可能装到一半。任何一种情况下
    我们都安静地返回 ``None``,让上层退回统一注册表 —— 元数据查询绝不能
    因为"某个可选模块不在"就整个失败。
    """
    try:
        from ..storages import merge_tree  # type: ignore
    except Exception:
        return None

    # ① 未来可能的正式接口
    fn = getattr(merge_tree, "registered_merge_tree_settings", None)
    if callable(fn):
        try:
            rows = fn()
        except Exception:  # pragma: no cover
            rows = None
        if rows:
            return [_normalize_merge_tree_row(r) for r in rows]

    # ② 引擎自己声明的 DEFAULT_SETTINGS(当前实现走的路径)
    defaults = getattr(merge_tree, "DEFAULT_SETTINGS", None)
    if isinstance(defaults, dict) and defaults:
        return _rows_from_engine_defaults(ctx, defaults)
    return None


def _rows_from_engine_defaults(ctx: Any, defaults: Dict[str, Any]) -> List[Tuple[Any, ...]]:
    """把"引擎声明的默认设置"变成 ``system.merge_tree_settings`` 的行。

    ``value`` 取"当前生效值":如果这条名字在 :mod:`minich.settings` 里也注册过,
    就用 ``ctx.settings`` 的值(于是 ``SET`` 过的项自然显示 ``changed = 1``);
    否则就是引擎的默认值。``description`` / ``type`` / ``min`` / ``max`` 也从
    统一注册表借(借得到就有,借不到留空)—— 这些都是**取回已有事实**。
    """
    get_def = _settings_def_lookup()
    settings = getattr(ctx, "settings", None)
    out: List[Tuple[Any, ...]] = []
    for name in sorted(defaults):
        engine_default = defaults[name]
        defn = get_def(name) if get_def is not None else None
        value = engine_default
        if settings is not None and defn is not None:
            try:
                value = settings.try_get(name, engine_default)
            except Exception:  # pragma: no cover
                value = engine_default
        changed = 0 if to_text(value) == to_text(engine_default) else 1
        out.append(
            (
                name,
                to_text(value),
                changed,
                getattr(defn, "description", "") if defn is not None else "",
                "" if defn is None or defn.min is None else to_text(defn.min),
                "" if defn is None or defn.max is None else to_text(defn.max),
                int(bool(getattr(defn, "is_readonly", False))) if defn is not None else 0,
                defn.type_name if defn is not None else "",
                to_text(engine_default),
            )
        )
    return out


def _settings_def_lookup() -> Optional[Any]:
    """返回 :func:`minich.settings.get_setting_def`(拿不到就 ``None``)。"""
    try:
        from ..settings import get_setting_def

        return get_setting_def
    except Exception:  # pragma: no cover
        return None


def _normalize_merge_tree_row(row: Any) -> Tuple[Any, ...]:
    """把引擎给的 MergeTree 设置行(dict 或 tuple)规整成 9 列。"""
    if isinstance(row, dict):
        return (
            row.get("name", ""),
            to_text(row.get("value", "")),
            int(row.get("changed", 0)),
            row.get("description", ""),
            "" if row.get("min") is None else to_text(row["min"]),
            "" if row.get("max") is None else to_text(row["max"]),
            int(row.get("readonly", 0)),
            row.get("type", ""),
            to_text(row.get("default", "")),
        )
    return tuple(row)


# ══════════════════════════════════════════════════════════════════════════
# system.dictionaries
# ══════════════════════════════════════════════════════════════════════════


class SystemDictionaries(SimpleSystemTable):
    """``system.dictionaries`` —— 回答"字典加载成功了吗、占了多少内存"。

    字典(``CREATE DICTIONARY``)是 ClickHouse 做维表关联的利器,
    但它们**是异步加载的** —— 服务刚起来时字典可能还在
    ``LOADING`` 或干脆 ``FAILED``。所以排障第一步永远是:

    .. code-block:: sql

       SELECT name, status, last_exception, element_count, bytes_allocated
       FROM system.dictionaries;

    ``status`` 不是 ``LOADED`` 的那几行,就是"为什么 JOIN 报了
    ``Dictionary not found``"的答案。

    .. note::

       **数据来自 ``ctx.dictionaries``(如果字典子系统被挂上来了)。**
       它是另一个模块(``minich/dictionaries.py``)负责填充的;
       在这一侧我们只做 ``getattr`` 式的**尽力映射** —— 拿不到某个字段
       就填该类型的默认值,而不是猜。子系统缺席时这张表为空。
    """

    name = "dictionaries"
    columns = [
        ("database", "String"),
        ("name", "String"),
        ("status", "String"),
        ("origin", "String"),
        ("type", "String"),
        ("key", "String"),
        ("bytes_allocated", "UInt64"),
        ("element_count", "UInt64"),
        ("load_factor", "Float64"),
        ("source", "String"),
        ("lifetime", "UInt64"),
        ("loading_start_time", "DateTime"),
        ("loading_duration", "Float64"),
        ("last_exception", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        dictionaries = getattr(ctx, "dictionaries", None)
        if not dictionaries:
            return
        items = dictionaries.items() if isinstance(dictionaries, dict) else (
            (getattr(d, "name", str(i)), d) for i, d in enumerate(dictionaries)
        )
        for key, dic in sorted(items, key=lambda kv: str(kv[0])):
            name = getattr(dic, "name", None) or str(key)
            yield (
                getattr(dic, "database", "") or "",
                name,
                getattr(dic, "status", "") or "",
                getattr(dic, "origin", "") or "",
                getattr(dic, "type", getattr(dic, "dictionary_type", "")) or "",
                getattr(dic, "key", "") or "",
                int(getattr(dic, "bytes_allocated", 0) or 0),
                int(getattr(dic, "element_count", 0) or 0),
                float(getattr(dic, "load_factor", 0.0) or 0.0),
                getattr(dic, "source", "") or "",
                int(getattr(dic, "lifetime", 0) or 0),
                str(getattr(dic, "loading_start_time", "1970-01-01 00:00:00")),
                float(getattr(dic, "loading_duration", 0.0) or 0.0),
                getattr(dic, "last_exception", "") or "",
            )


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════


def register() -> None:
    """把本模块的表装进全局注册表。由 :mod:`minich.system` 在导入时调用。"""
    for cls in (
        SystemDatabases,
        SystemTables,
        SystemColumns,
        SystemParts,
        SystemPartLog,
        SystemMutations,
        SystemMerges,
        SystemMergeTreeSettings,
        SystemDictionaries,
    ):
        register_system_table(cls())
