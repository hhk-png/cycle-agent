"""系统表测试 —— ``system.*`` 既能"读得通",也能"读得对"。

系统表是 ClickHouse 的**自省界面**:``system.tables`` / ``system.columns`` /
``system.parts`` 在真实运维里被无数脚本依赖。所以这一层有两条底线:

1. **每一张注册的系统表都必须能 ``SELECT *``** —— 列定义自洽、类型能解析、
   不抛异常。真实 ClickHouse 里漏注册一张表会让客户端直接报错;
2. **系统表的内容必须和实例状态一致** —— 建了库/表、插了数据之后,
   对应的行要出现,列的类型要对。

.. note::

   ``system.numbers`` 是**无界**表:不加 ``LIMIT`` 它在真实 ClickHouse 里
   永远不返回。mini-clickhouse 给了一个默认上界(见
   :data:`minich.system.base.DEFAULT_NUMBERS_LIMIT`),但测试里仍然显式写
   ``LIMIT`` —— 这条习惯值得保留。
"""

from __future__ import annotations

import pytest

from conftest import assert_rows

from minich.execution.context import QueryResult
from minich.storages import registered_storage_engines
from minich.system import system_table_names
from minich.types import get_data_type


# ══════════════════════════════════════════════════════════════════════════
# ① 每一张系统表都能被 SELECT * —— 列类型逐个能被 get_data_type 解析
# ══════════════════════════════════════════════════════════════════════════

#: ``numbers`` 系列是无界表,读的时候必须带 ``LIMIT``。
_UNBOUNDED = {"numbers", "numbers_mt"}


def _star_sql(name: str) -> str:
    sql = f"SELECT * FROM system.{name}"
    if name in _UNBOUNDED:
        sql += " LIMIT 3"
    return sql


def test_system_table_names_is_sorted_and_non_empty():
    names = system_table_names()
    assert names, "系统表注册表为空 —— 多半是 minich.system 没有被导入"
    assert names == sorted(names)
    # 只写了名字、没有 ``system.`` 前缀。
    assert all(not n.startswith("system.") for n in names)


@pytest.mark.parametrize("name", system_table_names())
def test_every_system_table_can_be_selected(name, memory_ctx):
    """``SELECT * FROM system.<name>`` 不得抛异常,且每个列类型都能解析。"""
    result = memory_ctx.execute(_star_sql(name))
    assert isinstance(result, QueryResult)
    columns = result.column_names()
    types = result.column_types()
    # 列名与列类型一一对应。
    assert len(columns) == len(types)
    # 每一列的类型名都要能被类型工厂解析回来(列定义没写错字)。
    for type_name in types:
        parsed = get_data_type(type_name)
        # 规范化后的名字必须稳定(parse → get_name → 再 parse 得同一个对象名)。
        assert get_data_type(parsed.get_name()).get_name() == parsed.get_name()


@pytest.mark.parametrize("name", system_table_names())
def test_every_system_table_header_matches_rows(name, memory_ctx):
    """结果的列名不能有重复;``row_count()`` 与 ``len(rows())`` 一致。"""
    result = memory_ctx.execute(_star_sql(name))
    columns = result.column_names()
    assert len(set(columns)) == len(columns), f"system.{name} 有重复列名:{columns}"
    assert result.row_count() == len(result.rows())


# ══════════════════════════════════════════════════════════════════════════
# ② 内容是"对"的:照着实例状态断言
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def populated_ctx(memory_ctx):
    """建库、建表、插一行、再跑一次 SELECT —— 给系统表提供可断言的状态。"""
    memory_ctx.execute("CREATE DATABASE analytics")
    memory_ctx.execute(
        "CREATE TABLE analytics.events ("
        "  ts DateTime, uid UInt32, url String"
        ") ENGINE = Memory"
    )
    memory_ctx.execute(
        "INSERT INTO analytics.events VALUES ('2024-01-01 00:00:00', 1, '/a')"
    )
    memory_ctx.execute("SELECT count() FROM analytics.events")
    return memory_ctx


def test_system_tables_lists_the_created_table(populated_ctx):
    rows = populated_ctx.execute(
        "SELECT database, name, engine, total_rows FROM system.tables "
        "WHERE database = 'analytics'"
    ).rows()
    assert rows == [("analytics", "events", "Memory", 1)]


def test_system_tables_only_lists_catalog_tables(populated_ctx):
    """``system.tables`` 列的是**目录里的表**,不含 ``system.*`` 自己。

    系统表不是 :class:`~minich.catalog.DatabaseCatalog` 的成员 ——
    它们由 ``minich.system`` 单独实现,所以不会出现在 ``system.tables`` 里。
    """
    databases = {
        db for (db,) in populated_ctx.execute(
            "SELECT DISTINCT database FROM system.tables"
        ).rows()
    }
    assert "analytics" in databases
    assert "system" not in databases


def test_system_columns_lists_columns_with_types_in_position_order(populated_ctx):
    rows = populated_ctx.execute(
        "SELECT name, type, position FROM system.columns "
        "WHERE database = 'analytics' AND table = 'events' "
        "ORDER BY position"
    ).rows()
    assert rows == [
        ("ts", "DateTime", 1),
        ("uid", "UInt32", 2),
        ("url", "String", 3),
    ]


def test_system_columns_includes_system_tables_own_columns(populated_ctx):
    """``system.columns`` 自己也描述 ``system.*`` 表 —— 这是自举,不能缺。"""
    names = {
        n for (n,) in populated_ctx.execute(
            "SELECT name FROM system.columns "
            "WHERE database = 'system' AND table = 'tables'"
        ).rows()
    }
    assert {"database", "name", "engine", "total_rows"} <= names


def test_system_databases_lists_default_and_system_atomic(populated_ctx):
    rows = populated_ctx.execute(
        "SELECT name, engine FROM system.databases ORDER BY name"
    ).rows()
    assert ("analytics", "Atomic") in rows
    assert ("default", "Atomic") in rows
    assert ("system", "Atomic") in rows


def test_system_settings_exposes_max_block_size(populated_ctx):
    rows = populated_ctx.execute(
        "SELECT name, value FROM system.settings WHERE name = 'max_block_size'"
    ).rows()
    # 默认 65409 = 65536 - 127(见 minich.settings 的说明)。
    assert rows == [("max_block_size", "65409")]


def test_system_settings_is_large_and_typed(populated_ctx):
    result = populated_ctx.execute("SELECT name, value, type FROM system.settings")
    assert result.row_count() > 50
    # 每一行的 ``name`` 都非空,``value`` 是字符串。
    for name, value, _type in result.rows():
        assert name and isinstance(value, str)


def test_system_functions_is_non_empty_and_lists_known_ones(populated_ctx):
    names = {
        n for (n,) in populated_ctx.execute(
            "SELECT name FROM system.functions"
        ).rows()
    }
    assert {"count", "sum"} <= names
    assert len(names) > 100


def test_system_metrics_exposes_server_gauges(populated_ctx):
    values = dict(
        populated_ctx.execute("SELECT metric, value FROM system.metrics").rows()
    )
    # Context.__init__ 里显式 set 过这三个 gauge。
    assert "Query" in values
    assert "MemoryTracking" in values
    assert "TCPConnection" in values
    assert all(isinstance(v, (int, float)) for v in values.values())


def test_system_events_is_non_empty_with_integer_counters(populated_ctx):
    result = populated_ctx.execute("SELECT event, value FROM system.events")
    assert result.row_count() > 0
    for _event, value in result.rows():
        assert isinstance(value, int)


def test_system_processes_lists_the_currently_running_query(populated_ctx):
    """读 ``system.processes`` 的那一刻,这条查询自己就在列表里。"""
    rows = populated_ctx.execute("SELECT query FROM system.processes").rows()
    assert any("system.processes" in q for (q,) in rows)


def test_system_query_log_grows_after_a_select(populated_ctx):
    before = populated_ctx.execute("SELECT count() FROM system.query_log").scalar()
    populated_ctx.execute("SELECT 998877 AS marker_query_log_probe")
    after = populated_ctx.execute("SELECT count() FROM system.query_log").scalar()
    assert after > before
    hits = populated_ctx.execute(
        "SELECT count() FROM system.query_log "
        "WHERE query LIKE '%marker_query_log_probe%'"
    ).scalar()
    assert hits >= 1


def test_system_query_log_records_ddl_and_dml(populated_ctx):
    queries = {
        q for (q,) in populated_ctx.execute(
            "SELECT query FROM system.query_log"
        ).rows()
    }
    # 上面 populated_ctx 跑过的三条语句都应留下痕迹。
    assert any("CREATE DATABASE analytics" in q for q in queries)
    assert any("INSERT INTO analytics.events" in q for q in queries)


def test_system_parts_lists_mergetree_parts(disk_ctx):
    """``system.parts`` 的数据源是 MergeTree 引擎的 part 列表。"""
    disk_ctx.execute("CREATE TABLE mt (a Int32, b String) ENGINE = MergeTree ORDER BY (a)")
    disk_ctx.execute("INSERT INTO mt VALUES (1, 'x')")
    rows = disk_ctx.execute(
        "SELECT database, table, rows, active FROM system.parts WHERE table = 'mt'"
    ).rows()
    assert rows == [("default", "mt", 1, 1)]


def test_system_parts_is_empty_without_mergetree(memory_ctx):
    """没有 MergeTree 表时 ``system.parts`` 为空 —— 这是**正常**状态。"""
    assert memory_ctx.execute("SELECT * FROM system.parts").rows() == []


def test_system_table_engines_matches_the_storage_registry(memory_ctx):
    names = {
        n for (n,) in memory_ctx.execute(
            "SELECT name FROM system.table_engines"
        ).rows()
    }
    assert names == {name for name, _, _ in registered_storage_engines()}


def test_system_table_engines_marks_mergetree_capabilities(memory_ctx):
    """``MergeTree`` 行要如实标出"支持排序键"这一类能力位。"""
    row = memory_ctx.execute(
        "SELECT supports_sort_order, supports_ttl FROM system.table_engines "
        "WHERE name = 'MergeTree'"
    ).rows()
    assert row == [(1, 1)]


def test_system_data_type_families_lists_core_types(memory_ctx):
    names = {
        n for (n,) in memory_ctx.execute(
            "SELECT name FROM system.data_type_families"
        ).rows()
    }
    assert {"UInt8", "String", "DateTime"} <= names


def test_system_formats_lists_core_formats(memory_ctx):
    rows = memory_ctx.execute("SELECT name, is_input, is_output FROM system.formats").rows()
    names = {n for n, _, _ in rows}
    assert {"TSV", "CSV", "JSON"} <= names
    # ``PrettyCompact`` 能写不能读 —— 至少 ``is_output`` 为真。
    pretty = [r for r in rows if r[0] == "PrettyCompact"]
    assert pretty and pretty[0][2] == 1


def test_system_one_returns_exactly_one_zero_row(memory_ctx):
    result = memory_ctx.execute("SELECT * FROM system.one")
    assert result.column_names() == ["dummy"]
    assert result.rows() == [(0,)]


# ══════════════════════════════════════════════════════════════════════════
# ③ system.numbers / system.numbers_mt —— 惰性整数流
# ══════════════════════════════════════════════════════════════════════════


def test_system_numbers_streams_from_zero(memory_ctx):
    assert_rows(memory_ctx.execute("SELECT * FROM system.numbers LIMIT 5"),
                [(0,), (1,), (2,), (3,), (4,)])


def test_system_numbers_mt_streams_from_zero(memory_ctx):
    assert_rows(memory_ctx.execute("SELECT * FROM system.numbers_mt LIMIT 5"),
                [(0,), (1,), (2,), (3,), (4,)])


def test_system_numbers_column_metadata(memory_ctx):
    result = memory_ctx.execute("SELECT number FROM system.numbers LIMIT 1")
    assert result.column_names() == ["number"]
    assert result.column_types() == ["UInt64"]


def test_system_numbers_can_be_filtered_and_aggregated(memory_ctx):
    """系统表是普通表:一样能 WHERE / 聚合(没有给解释器开后门)。"""
    total = memory_ctx.execute(
        "SELECT sum(number) FROM system.numbers WHERE number < 4"
    ).scalar()
    assert total == 0 + 1 + 2 + 3
    count = memory_ctx.execute(
        "SELECT count() FROM system.numbers WHERE number < 10"
    ).scalar()
    assert count == 10


def test_system_numbers_has_a_finite_default_upper_bound(memory_ctx):
    """mini-clickhouse 给无界表一个默认上界(真实 ClickHouse 没有)。

    这是本套件唯一一处"不忠实"的简化 —— 见
    :data:`minich.system.base.DEFAULT_NUMBERS_LIMIT`。
    """
    assert memory_ctx.execute("SELECT count() FROM system.numbers").scalar() == 10000
    assert memory_ctx.execute(
        "SELECT min(number), max(number) FROM system.numbers"
    ).rows() == [(0, 9999)]


def test_system_numbers_respects_a_smaller_limit(memory_ctx):
    assert memory_ctx.execute("SELECT * FROM system.numbers LIMIT 2").rows() == [(0,), (1,)]
