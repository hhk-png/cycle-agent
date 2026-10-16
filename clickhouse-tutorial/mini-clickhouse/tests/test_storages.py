"""表引擎测试 —— "能不能存、能不能读回、能不能改、什么时候不能"。

本文件围绕 :mod:`minich.storages` 回答三个问题:

1. **引擎注册表**长什么样(名字、大小写、是否弃用);
2. **数据往返**:同一个"折磨行"(NULL、空串、Unicode、极值、数组、元组、
   Map、Date/DateTime/Decimal/Enum/UUID/FixedString/IP)写进每一种**真正存
   数据**的引擎,再 ``SELECT`` 回来,断言**逐列精确相等**;
3. **DDL/生命周期**:``TRUNCATE``、``ALTER``(增/删/改名/改类型)、
   ``shutdown()`` 的幂等性。

外加一节**表函数**(``numbers`` / ``values`` / ``file`` / …)的精确行断言。

.. note::

   **"精确相等"的期望值是怎么算出来的?**

   mini-clickhouse 的列是**列存**:``Date`` 存的是 epoch 天数、``DateTime``
   存的是 epoch 秒、``Decimal`` 存的是放大后的整数、``Enum`` 存的是下标。
   所以 ``SELECT`` 回来的并不是你写进去的字符串,而是**内部表示**。
   本文件统一用 :meth:`DataType.normalize` 把期望值**归一化到同一表示**,
   再和结果比较 —— 这样断言既精确,又不依赖"格式化"这一层。

.. note::

   文件末尾有一节 ``xfail`` 测试,把写这些测试时**发现的 minich 缺陷**
   钉住(非 ``strict``):它们在缺陷修好之后会自动变成 ``XPASS``,
   而不是把测试套件变成红色。
"""

from __future__ import annotations

import os

import pytest

from conftest import assert_rows, assert_rows_unordered

from minich.block import Block, ColumnWithTypeAndName
from minich.errors import ErrorCode, MiniChError
from minich.storages import registered_storage_engines
from minich.storages.base import (
    storage_engine_display_name,
    storage_engine_exists,
)
from minich.storages.table_functions import registered_table_functions
from minich.types import get_data_type


# ══════════════════════════════════════════════════════════════════════════
# "折磨行" —— 故意把边界情况塞满
# ══════════════════════════════════════════════════════════════════════════

#: 17 列,覆盖整数/浮点/字符串/可空/数组/元组/映射/日期时间/定点/枚举/UUID/定长/IP。
TORTURE_DDL = (
    "n Nullable(Int32), s String, u String, i8 Int8, u8 UInt8, i64 Int64, "
    "arr Array(UInt8), tup Tuple(UInt8, String), m Map(String, UInt8), "
    "d Date, dt DateTime, dec Decimal(10,2), e Enum8('a'=1,'b'=2), "
    "uid UUID, fx FixedString(2), ip4 IPv4, f Float64"
)

#: DDL 里的列名(顺序)**必须**与实际建出来的表一致 —— 用来断言结构没有被改写。
TORTURE_COLUMNS = [
    "n", "s", "u", "i8", "u8", "i64", "arr", "tup", "m",
    "d", "dt", "dec", "e", "uid", "fx", "ip4", "f",
]

#: 第 1 行:每个类型的"边界值"—— NULL / 空串 / Unicode / Int64 下界 / 空数组。
TORTURE_ROW1 = (
    None, "", "中文🚀", -128, 255, -9223372036854775808,
    [1, 2, 3], (7, "x"), {"k": 1},
    "2024-01-02", "2024-01-02 03:04:05", "3.14", "a",
    "11111111-1111-1111-1111-111111111111", "ab", "1.2.3.4", 0.5,
)

#: 第 2 行:另一组值,尤其让排序键 ``(i8, u8)`` 与第 1 行不同,
#: 免得 Summing/Replacing 这类引擎在 merge 时把两行折叠掉。
TORTURE_ROW2 = (
    -5, "hello", "", 127, 0, 0,
    [], (0, ""), {"z": 9},
    "1970-01-01", "1970-01-01 00:00:00", "0.00", "b",
    "00000000-0000-0000-0000-000000000000", "zz", "0.0.0.0", -1.25,
)

TORTURE_ROWS = [TORTURE_ROW1, TORTURE_ROW2]

#: 排序键 —— 两行的 ``i8`` 分别是 -128 / 127,足以给出稳定顺序。
TORTURE_ORDER_BY = "i8, u8"


# ══════════════════════════════════════════════════════════════════════════
# 小工具:借"列定义 + normalize"造 Block / 算期望值
# ══════════════════════════════════════════════════════════════════════════


def _stored_columns(table):
    """真正落盘的列(ALIAS 列不落盘,``SELECT *`` 才返回它)。"""
    return [c for c in table.columns if c.is_stored()]


def _block_of(table, rows):
    """把"行列表"装成一个 :class:`Block`(列式)。

    这是比 ``ctx.insert()`` 更底层的写路径:它绕过 SQL 解析,
    直接喂给引擎。用 :meth:`DataType.normalize` 把 Python 值翻成
    内部表示(None→NULL、``"2024-01-02"``→19724、``"3.14"``→314 …)。
    """
    cols = _stored_columns(table)
    data = []
    for c in cols:
        column = c.type.create_column()
        for row in rows:
            column.append(c.type.normalize(row[cols.index(c)]))
        data.append(ColumnWithTypeAndName(c.name, c.type, column))
    return Block(data)


def _expected_rows(table, rows):
    """期望的 ``SELECT *`` 结果 —— 每个值先经类型归一化。"""
    cols = _stored_columns(table)
    return [
        tuple(c.type.normalize(row[cols.index(c)]) for c in cols)
        for row in rows
    ]


def _roundtrip(ctx, engine_sql, ddl=TORTURE_DDL, rows=TORTURE_ROWS,
               order_by=TORTURE_ORDER_BY):
    """建表 → 写入 → 读回。返回 ``(table, QueryResult)``。"""
    ctx.execute(f"CREATE TABLE t ({ddl}) ENGINE = {engine_sql}")
    table = ctx.catalog.get_table("default", "t")
    ctx.insert_block("t", _block_of(table, rows))
    result = ctx.execute(f"SELECT * FROM t ORDER BY {order_by}")
    return table, result


def _make_ctx(make_context, tmp_path, on_disk):
    """按参数决定是内存 Context 还是落盘 Context。"""
    if on_disk:
        return make_context(data_path=str(tmp_path / "minich_data"))
    return make_context()


# ══════════════════════════════════════════════════════════════════════════
# ① 引擎注册表
# ══════════════════════════════════════════════════════════════════════════


def test_registered_storage_engines_is_sorted_and_has_display_case():
    """``registered_storage_engines()`` 返回 ``(名字, 弃用, 说明)`` 且按名字排序。

    名字是**注册时的大小写**(``MergeTree``),不是小写键 ——
    这正是 ``system.table_engines`` / ``SHOW ENGINES`` 要显示的东西。
    """
    engines = registered_storage_engines()
    names = [name for name, _, _ in engines]
    assert names == sorted(names)
    # 只列正名,且是规范化的大小写。
    assert "MergeTree" in names
    assert "mergetree" not in names


def test_engine_registry_entries_have_expected_shape():
    for name, deprecated, description in registered_storage_engines():
        assert isinstance(name, str) and name
        assert isinstance(deprecated, bool)
        assert isinstance(description, str)


def test_engine_registry_contains_the_documented_families():
    """README 能力矩阵里承诺的引擎一个都不能少。"""
    names = {name for name, _, _ in registered_storage_engines()}
    required = {
        # MergeTree 家族
        "MergeTree", "ReplacingMergeTree", "SummingMergeTree",
        "AggregatingMergeTree", "CollapsingMergeTree",
        "VersionedCollapsingMergeTree", "GraphiteMergeTree",
        "ReplicatedMergeTree",
        # 简单引擎
        "Memory", "TinyLog", "Log", "StripeLog", "Null", "Set", "Join",
        "Buffer", "Merge", "Values", "View", "MaterializedView", "File", "URL",
    }
    missing = required - names
    assert not missing, f"引擎注册表缺少:{sorted(missing)}"


def test_engine_display_name_normalizes_case_but_keeps_unknown():
    assert storage_engine_display_name("mergetree") == "MergeTree"
    assert storage_engine_display_name("MEMORY") == "Memory"
    # 未知名字原样返回 —— 不做猜测。
    assert storage_engine_display_name("NoSuchEngine") == "NoSuchEngine"


def test_storage_engine_exists_is_case_insensitive():
    assert storage_engine_exists("MergeTree")
    assert storage_engine_exists("mergeTREE")
    assert not storage_engine_exists("NoSuchEngine")


def test_create_table_with_unknown_engine_raises_unknown_storage(memory_ctx):
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute("CREATE TABLE t (a UInt8) ENGINE = NoSuchEngine")
    assert ei.value.code == ErrorCode.UNKNOWN_STORAGE


# ══════════════════════════════════════════════════════════════════════════
# ② 折磨行:写进去、读回来,逐列精确相等
# ══════════════════════════════════════════════════════════════════════════

#: ``(案例 id, 建表用的 ENGINE 子句, 是否落盘)``。
#:
#: * ``Memory`` / ``Log`` 家族 / ``Buffer`` —— 用内存 Context(它们不依赖目录);
#: * ``MergeTree`` 家族 —— 用 ``disk_ctx``(part 真的写盘),并显式给出
#:   ``ORDER BY``,避开 ``create_table`` 自动补的 ``ORDER BY tuple()`` 缺陷。
TORTURE_ENGINES = [
    ("Memory", "Memory", False),
    ("TinyLog", "TinyLog", False),
    ("Log", "Log", False),
    ("StripeLog", "StripeLog", False),
    ("Buffer", "Buffer", False),
    ("MergeTree", "MergeTree ORDER BY (i8, u8)", True),
    ("ReplacingMergeTree", "ReplacingMergeTree ORDER BY (i8, u8)", True),
    ("SummingMergeTree", "SummingMergeTree ORDER BY (i8, u8)", True),
    ("AggregatingMergeTree", "AggregatingMergeTree ORDER BY (i8, u8)", True),
    ("GraphiteMergeTree", "GraphiteMergeTree ORDER BY (i8, u8)", True),
    ("ReplicatedMergeTree", "ReplicatedMergeTree ORDER BY (i8, u8)", True),
]


@pytest.mark.parametrize(
    "engine_sql,on_disk",
    [(sql, disk) for _, sql, disk in TORTURE_ENGINES],
    ids=[name for name, _, _ in TORTURE_ENGINES],
)
def test_torture_roundtrip_is_exact(engine_sql, on_disk, make_context, tmp_path):
    """每种存数据的引擎都要能把折磨行**原样**存进去、读回来。"""
    ctx = _make_ctx(make_context, tmp_path, on_disk)
    table, result = _roundtrip(ctx, engine_sql)

    # 结构:列名与顺序不被改写。
    assert table.get_column_names() == TORTURE_COLUMNS
    assert result.column_names() == TORTURE_COLUMNS
    # 行数:两行都在(read 的 row_count 走的是"元数据捷径")。
    assert table.row_count() == 2
    # 内容:逐列精确相等。
    assert_rows(result, _expected_rows(table, TORTURE_ROWS))


@pytest.mark.parametrize(
    "engine_sql,on_disk",
    [(sql, disk) for _, sql, disk in TORTURE_ENGINES],
    ids=[name for name, _, _ in TORTURE_ENGINES],
)
def test_torture_selected_columns_are_exact(engine_sql, on_disk, make_context, tmp_path):
    """只取几列时结果同样精确 —— 顺带验证**列裁剪**不改变值。"""
    ctx = _make_ctx(make_context, tmp_path, on_disk)
    ctx.execute(f"CREATE TABLE t ({TORTURE_DDL}) ENGINE = {engine_sql}")
    table = ctx.catalog.get_table("default", "t")
    ctx.insert_block("t", _block_of(table, TORTURE_ROWS))
    result = ctx.execute(f"SELECT s, i64, dec, ip4 FROM t ORDER BY {TORTURE_ORDER_BY}")
    assert result.column_names() == ["s", "i64", "dec", "ip4"]
    expected = [
        (t[1], t[5], t[11], t[15]) for t in _expected_rows(table, TORTURE_ROWS)
    ]
    assert_rows(result, expected)


def test_memory_engine_row_count_and_size(memory_ctx):
    """``Memory`` 引擎:``row_count`` 走元数据捷径,不需要真的扫一遍。"""
    memory_ctx.execute("CREATE TABLE t (a Int32) ENGINE = Memory")
    table = memory_ctx.catalog.get_table("default", "t")
    assert table.row_count() == 0
    memory_ctx.execute("INSERT INTO t VALUES (1), (2), (3)")
    assert table.row_count() == 3
    assert table.engine.engine_name == "Memory"
    assert table.size_in_bytes() > 0


def test_context_insert_roundtrips_scalars_and_composite(memory_ctx):
    """``ctx.insert()``(值 → SQL 字面量 → 解析)对**非 Map/IPv4** 列同样精确。

    Map 与 IPv4 走这条路径会失败,见文件末尾的 xfail。
    """
    ddl = (
        "n Nullable(Int32), s String, arr Array(UInt8), tup Tuple(UInt8, String), "
        "d Date, dt DateTime, dec Decimal(10,2), e Enum8('a'=1,'b'=2), "
        "uid UUID, fx FixedString(2), f Float64"
    )
    row = (
        None, "", [1, 2], (7, "x"), "2024-01-02", "2024-01-02 03:04:05",
        "3.14", "a", "11111111-1111-1111-1111-111111111111", "ab", 0.5,
    )
    memory_ctx.create_table("t", ddl, engine_sql="Memory")
    memory_ctx.insert("t", [row])
    table = memory_ctx.catalog.get_table("default", "t")
    got = memory_ctx.execute("SELECT * FROM t").rows()
    assert got == _expected_rows(table, [row])
    # 再钉几个"内部表示"的字面值,防止 normalize 变成自证。
    (n, s, arr, tup, d, dt, dec, e, uid, fx, f), = got
    assert (n, s, arr, tup) == (None, "", [1, 2], (7, "x"))
    assert d == 19724                      # 2024-01-02 距 1970-01-01 的天数
    assert dt == 1704164645                # 2024-01-02 03:04:05 的 epoch 秒
    assert dec == 314                      # Decimal(10,2) 存的是放大 100 倍的整数
    assert e == 1                          # Enum8 第 0 个成员
    assert fx == "ab" and f == 0.5


# ══════════════════════════════════════════════════════════════════════════
# ③ 不存数据 / 只存一部分的引擎
# ══════════════════════════════════════════════════════════════════════════


def test_null_engine_discards_every_write(memory_ctx):
    """``Null`` 丢弃一切写入,读取永远为空(压测基线)。"""
    memory_ctx.execute("CREATE TABLE n (a Int32, b String) ENGINE = Null")
    memory_ctx.execute("INSERT INTO n VALUES (1, 'x'), (2, 'y')")
    table = memory_ctx.catalog.get_table("default", "n")
    assert table.row_count() == 0
    assert memory_ctx.execute("SELECT * FROM n").rows() == []
    assert memory_ctx.execute("SELECT count() FROM n").scalar() == 0
    # 结构还在:``SELECT *`` 能展开出正确的列名。
    assert memory_ctx.execute("SELECT * FROM n").column_names() == ["a", "b"]


def test_values_engine_reads_empty_without_attached_rows(memory_ctx):
    """``ENGINE = Values`` 的列来自引擎参数;没有 attach 数据时为零行。"""
    memory_ctx.execute("CREATE TABLE v ENGINE = Values('a UInt8', 'b String')")
    result = memory_ctx.execute("SELECT * FROM v")
    assert result.column_names() == ["a", "b"]
    assert result.rows() == []


def test_set_engine_keeps_only_deduped_first_column(memory_ctx):
    """``Set`` 只保留第一列的去重值(``IN`` 半连接优化的形态)。"""
    memory_ctx.execute("CREATE TABLE s (a Int32) ENGINE = Set")
    memory_ctx.execute("INSERT INTO s VALUES (1), (2), (2)")
    assert_rows(memory_ctx.execute("SELECT * FROM s ORDER BY a"), [(1,), (2,)])


def test_set_engine_rejects_multi_column_insert(memory_ctx):
    """``Set`` 只认单列 —— 插两列要被明确拒绝,而不是静默丢列。"""
    memory_ctx.execute("CREATE TABLE s (a Int32, b String) ENGINE = Set")
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute("INSERT INTO s VALUES (1, 'x')")
    assert ei.value.code == ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH


def test_join_engine_roundtrip(memory_ctx):
    """``Join`` 是内存哈希表,``SELECT`` 时按 key 取出整行。"""
    memory_ctx.execute("CREATE TABLE j (a Int32, b String) ENGINE = Join(ANY, LEFT, a)")
    memory_ctx.execute("INSERT INTO j VALUES (1, 'x'), (2, 'y')")
    assert_rows(memory_ctx.execute("SELECT * FROM j ORDER BY a"), [(1, "x"), (2, "y")])


def test_collapsing_merge_tree_roundtrip(disk_ctx):
    """``CollapsingMergeTree`` 需要显式的 ``sign`` 列;未 merge 时按原样读回。"""
    disk_ctx.execute(
        "CREATE TABLE c (a Int32, b Int32, sign Int8) "
        "ENGINE = CollapsingMergeTree(sign) ORDER BY (a)"
    )
    disk_ctx.execute("INSERT INTO c VALUES (1, 10, 1), (1, 10, -1), (2, 20, 1)")
    assert_rows(
        disk_ctx.execute("SELECT * FROM c ORDER BY a, sign DESC"),
        [(1, 10, 1), (1, 10, -1), (2, 20, 1)],
    )


def test_versioned_collapsing_merge_tree_roundtrip(disk_ctx):
    """``VersionedCollapsingMergeTree`` 需要 ``sign`` + ``ver`` 两列。"""
    disk_ctx.execute(
        "CREATE TABLE vc (a Int32, b Int32, sign Int8, ver UInt8) "
        "ENGINE = VersionedCollapsingMergeTree(sign, ver) ORDER BY (a)"
    )
    disk_ctx.execute("INSERT INTO vc VALUES (1, 10, 1, 1)")
    assert_rows(disk_ctx.execute("SELECT * FROM vc ORDER BY a"), [(1, 10, 1, 1)])


def test_merge_engine_is_a_virtual_view_over_matching_tables(memory_ctx):
    """``Merge`` 引擎把多张同构表拼成一张只读虚拟表。"""
    memory_ctx.execute("CREATE TABLE m1 (a UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE TABLE m2 (a UInt8) ENGINE = Memory")
    memory_ctx.execute("INSERT INTO m1 VALUES (1)")
    memory_ctx.execute("INSERT INTO m2 VALUES (2)")
    memory_ctx.execute("CREATE TABLE m_all (a UInt8) ENGINE = Merge('default', 'm[0-9]')")
    assert_rows(memory_ctx.execute("SELECT * FROM m_all ORDER BY a"), [(1,), (2,)])


# ══════════════════════════════════════════════════════════════════════════
# ④ TRUNCATE
# ══════════════════════════════════════════════════════════════════════════

#: 支持 ``TRUNCATE`` 的引擎。用单列 ``(a Int32)`` 是为了让 ``Set``(只认首列)
#: 也能进同一张参数表。
TRUNCATE_ENGINES = [
    ("Memory", "Memory", False),
    ("TinyLog", "TinyLog", False),
    ("Log", "Log", False),
    ("StripeLog", "StripeLog", False),
    ("Buffer", "Buffer", False),
    ("Null", "Null", False),
    ("Set", "Set", False),
    ("Join", "Join(ANY, LEFT, a)", False),
    ("MergeTree", "MergeTree ORDER BY (a)", True),
    ("ReplacingMergeTree", "ReplacingMergeTree ORDER BY (a)", True),
    ("SummingMergeTree", "SummingMergeTree ORDER BY (a)", True),
    ("AggregatingMergeTree", "AggregatingMergeTree ORDER BY (a)", True),
    ("GraphiteMergeTree", "GraphiteMergeTree ORDER BY (a)", True),
    ("ReplicatedMergeTree", "ReplicatedMergeTree ORDER BY (a)", True),
]


@pytest.mark.parametrize(
    "engine_sql,on_disk",
    [(sql, disk) for _, sql, disk in TRUNCATE_ENGINES],
    ids=[name for name, _, _ in TRUNCATE_ENGINES],
)
def test_truncate_empties_the_table(engine_sql, on_disk, make_context, tmp_path):
    ctx = _make_ctx(make_context, tmp_path, on_disk)
    ctx.execute(f"CREATE TABLE t (a Int32) ENGINE = {engine_sql}")
    ctx.execute("INSERT INTO t VALUES (1), (2)")
    table = ctx.catalog.get_table("default", "t")
    ctx.execute("TRUNCATE TABLE t")
    assert table.row_count() == 0
    assert ctx.execute("SELECT * FROM t").rows() == []
    # 结构不受影响 —— TRUNCATE 只清数据,不动 schema。
    assert table.get_column_names() == ["a"]


def test_truncate_collapsing_merge_tree(disk_ctx):
    """折叠家族的表也能 TRUNCATE —— 而且必须带着它的 ``sign`` 列一起清空。"""
    disk_ctx.execute(
        "CREATE TABLE c (a Int32, sign Int8) "
        "ENGINE = CollapsingMergeTree(sign) ORDER BY (a)"
    )
    disk_ctx.execute("INSERT INTO c VALUES (1, 1), (2, 1)")
    table = disk_ctx.catalog.get_table("default", "c")
    assert table.row_count() == 2
    disk_ctx.execute("TRUNCATE TABLE c")
    assert table.row_count() == 0
    assert disk_ctx.execute("SELECT * FROM c").rows() == []


# ══════════════════════════════════════════════════════════════════════════
# ⑤ ALTER:增 / 删 / 改名 / 改类型
# ══════════════════════════════════════════════════════════════════════════

#: 能做 ALTER 的引擎(schema 断言对所有这些成立)。
ALTER_ENGINES = [
    ("Memory", "Memory", False),
    ("TinyLog", "TinyLog", False),
    ("Log", "Log", False),
    ("StripeLog", "StripeLog", False),
    ("Buffer", "Buffer", False),
    ("Null", "Null", False),
    ("MergeTree", "MergeTree ORDER BY (a)", True),
]

#: 其中**数据不受影响**的引擎(``Null`` 不存数据;``Buffer`` 见 xfail)。
ALTER_ENGINES_WITH_DATA = [
    ("Memory", "Memory", False),
    ("TinyLog", "TinyLog", False),
    ("Log", "Log", False),
    ("StripeLog", "StripeLog", False),
    ("MergeTree", "MergeTree ORDER BY (a)", True),
]


@pytest.mark.parametrize(
    "engine_sql,on_disk",
    [(sql, disk) for _, sql, disk in ALTER_ENGINES],
    ids=[name for name, _, _ in ALTER_ENGINES],
)
def test_alter_changes_schema(engine_sql, on_disk, make_context, tmp_path):
    """四类 ALTER 逐一改到位,并断言最终 schema。"""
    ctx = _make_ctx(make_context, tmp_path, on_disk)
    ctx.execute(f"CREATE TABLE t (a Int32, b String) ENGINE = {engine_sql}")
    ctx.execute("INSERT INTO t VALUES (1, 'x')")
    table = ctx.catalog.get_table("default", "t")

    # ADD COLUMN:新列出现在**末尾**。
    ctx.execute("ALTER TABLE t ADD COLUMN c UInt8")
    assert table.get_column_names() == ["a", "b", "c"]
    assert table.get_column("c").type.get_name() == "UInt8"

    # DROP COLUMN:列消失,其余顺序不变。
    ctx.execute("ALTER TABLE t DROP COLUMN c")
    assert table.get_column_names() == ["a", "b"]

    # RENAME COLUMN:只改名,不改类型。
    ctx.execute("ALTER TABLE t RENAME COLUMN b TO bb")
    assert table.get_column_names() == ["a", "bb"]
    assert table.get_column("bb").type.get_name() == "String"

    # MODIFY COLUMN:类型变了,列名不变。
    ctx.execute("ALTER TABLE t MODIFY COLUMN a Int64")
    assert table.get_column("a").type.get_name() == "Int64"
    assert table.get_column_names() == ["a", "bb"]


@pytest.mark.parametrize(
    "engine_sql,on_disk",
    [(sql, disk) for _, sql, disk in ALTER_ENGINES_WITH_DATA],
    ids=[name for name, _, _ in ALTER_ENGINES_WITH_DATA],
)
def test_alter_preserves_existing_data(engine_sql, on_disk, make_context, tmp_path):
    """schema 变了,老数据还要在 —— 这是 ALTER 最容易被写坏的地方。"""
    ctx = _make_ctx(make_context, tmp_path, on_disk)
    ctx.execute(f"CREATE TABLE t (a Int32, b String) ENGINE = {engine_sql}")
    ctx.execute("INSERT INTO t VALUES (1, 'x')")
    ctx.execute("ALTER TABLE t ADD COLUMN c UInt8")
    ctx.execute("ALTER TABLE t RENAME COLUMN b TO bb")
    ctx.execute("ALTER TABLE t MODIFY COLUMN a Int64")
    assert_rows(ctx.execute("SELECT a, bb FROM t"), [(1, "x")])


def test_alter_add_column_without_default_reads_zero(memory_ctx):
    """新加的列没有 DEFAULT —— 老行读到类型的默认值(整数是 0)。"""
    memory_ctx.execute("CREATE TABLE t (a Int32) ENGINE = Memory")
    memory_ctx.execute("INSERT INTO t VALUES (1)")
    memory_ctx.execute("ALTER TABLE t ADD COLUMN c UInt8")
    assert_rows(memory_ctx.execute("SELECT * FROM t"), [(1, 0)])


def test_alter_add_column_with_default_backfills_on_mergetree(disk_ctx):
    """MergeTree 的 part 里没有新列,DEFAULT 在**读**的时候补上。"""
    disk_ctx.execute("CREATE TABLE t (a Int32) ENGINE = MergeTree ORDER BY (a)")
    disk_ctx.execute("INSERT INTO t VALUES (1)")
    disk_ctx.execute("ALTER TABLE t ADD COLUMN c UInt8 DEFAULT 7")
    assert_rows(disk_ctx.execute("SELECT * FROM t"), [(1, 7)])


def test_alter_rename_to_existing_name_raises(memory_ctx):
    memory_ctx.execute("CREATE TABLE t (a Int32, b String) ENGINE = Memory")
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute("ALTER TABLE t RENAME COLUMN a TO b")
    assert ei.value.code == ErrorCode.DUPLICATE_COLUMN


# ══════════════════════════════════════════════════════════════════════════
# ⑥ shutdown() 的幂等性
# ══════════════════════════════════════════════════════════════════════════


def test_shutdown_is_idempotent_for_memory(memory_ctx):
    """``shutdown()`` 必须能安全调用两次(显式关闭 + 退出时又关一次)。"""
    memory_ctx.execute("CREATE TABLE t (a UInt8) ENGINE = Memory")
    memory_ctx.shutdown()
    memory_ctx.shutdown()  # 第二次不得抛异常


def test_shutdown_is_idempotent_for_mergetree(disk_ctx):
    disk_ctx.execute("CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY (a)")
    disk_ctx.execute("INSERT INTO t VALUES (1)")
    disk_ctx.shutdown()
    disk_ctx.shutdown()


def test_engine_shutdown_is_idempotent(memory_ctx):
    memory_ctx.execute("CREATE TABLE t (a UInt8) ENGINE = Memory")
    engine = memory_ctx.catalog.get_table("default", "t").engine
    assert not engine.is_shutdown()
    engine.shutdown()
    engine.shutdown()
    assert engine.is_shutdown()


def test_drop_then_shutdown_twice(disk_ctx):
    disk_ctx.execute("CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY (a)")
    disk_ctx.execute("DROP TABLE t")
    disk_ctx.shutdown()
    disk_ctx.shutdown()


def test_context_manager_shuts_down_on_exit(memory_ctx):
    with memory_ctx as ctx:
        assert ctx is memory_ctx
        ctx.execute("CREATE TABLE t (a UInt8) ENGINE = Memory")
    # 退出 with 之后再关一次也不该出错。
    memory_ctx.shutdown()


# ══════════════════════════════════════════════════════════════════════════
# ⑦ 表函数
# ══════════════════════════════════════════════════════════════════════════


def test_table_function_registry_contains_the_documented_names():
    names = {name for name, _ in registered_table_functions()}
    required = {
        "numbers", "zeros", "system.numbers", "input", "values", "file",
        "url", "format", "generateRandom", "view", "merge", "null",
        "remote", "cluster",
    }
    missing = required - names
    assert not missing, f"表函数注册表缺少:{sorted(missing)}"


def test_tf_numbers_variants(memory_ctx):
    """``numbers`` 的三种参数形态:``(n)`` / ``(start, end)`` / ``(start, end, step)``。"""
    assert_rows(memory_ctx.execute("SELECT * FROM numbers(5)"),
                [(0,), (1,), (2,), (3,), (4,)])
    assert_rows(memory_ctx.execute("SELECT * FROM numbers(2, 5)"),
                [(2,), (3,), (4,)])
    assert_rows(memory_ctx.execute("SELECT * FROM numbers(0, 10, 2)"),
                [(0,), (2,), (4,), (6,), (8,)])
    # start >= end 时为空(不是负数循环)。
    assert memory_ctx.execute("SELECT * FROM numbers(5, 2)").rows() == []


def test_tf_numbers_column_metadata(memory_ctx):
    result = memory_ctx.execute("SELECT * FROM numbers(3)")
    assert result.column_names() == ["number"]
    assert result.column_types() == ["UInt64"]


def test_tf_zeros(memory_ctx):
    result = memory_ctx.execute("SELECT * FROM zeros(3)")
    assert result.column_names() == ["zero"]
    assert result.column_types() == ["UInt8"]
    assert_rows(result, [(0,), (0,), (0,)])
    assert memory_ctx.execute("SELECT count() FROM zeros(10)").scalar() == 10
    assert memory_ctx.execute("SELECT count() FROM zeros(0)").scalar() == 0


def test_tf_system_numbers_and_numbers_mt(memory_ctx):
    """``system.numbers`` / ``system.numbers_mt`` 是**同一份**惰性序列。"""
    assert_rows(memory_ctx.execute("SELECT * FROM system.numbers LIMIT 3"),
                [(0,), (1,), (2,)])
    assert_rows(memory_ctx.execute("SELECT * FROM system.numbers_mt LIMIT 3"),
                [(0,), (1,), (2,)])
    # 别名 ``numbers_mt`` 指向同一个 ``numbers``。
    assert_rows(memory_ctx.execute("SELECT * FROM numbers_mt(3)"),
                [(0,), (1,), (2,)])


def test_tf_values_two_columns(memory_ctx):
    assert_rows(
        memory_ctx.execute(
            "SELECT * FROM values('a UInt8, b String', (1, 'x'), (2, 'y'))"
        ),
        [(1, "x"), (2, "y")],
    )


def test_tf_values_single_column(memory_ctx):
    result = memory_ctx.execute("SELECT * FROM values('a UInt8', (1), (2), (3))")
    assert result.column_names() == ["a"]
    assert_rows(result, [(1,), (2,), (3,)])


def test_tf_generate_random_is_seed_deterministic(memory_ctx):
    first = memory_ctx.execute("SELECT * FROM generateRandom('a UInt8', 42) LIMIT 5")
    second = memory_ctx.execute("SELECT * FROM generateRandom('a UInt8', 42) LIMIT 5")
    assert first.column_names() == ["a"]
    assert first.column_types() == ["UInt8"]
    assert first.rows() == second.rows()          # 同种子 → 同结果
    assert len(first.rows()) == 5
    assert all(0 <= v[0] <= 255 for v in first.rows())


def test_tf_null_produces_no_rows(memory_ctx):
    assert memory_ctx.execute("SELECT count() FROM null()").scalar() == 0
    assert memory_ctx.execute("SELECT * FROM null()").rows() == []


def test_tf_merge_concatenates_matching_tables(memory_ctx):
    memory_ctx.execute("CREATE TABLE m1 (a UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE TABLE m2 (a UInt8) ENGINE = Memory")
    memory_ctx.execute("INSERT INTO m1 VALUES (1)")
    memory_ctx.execute("INSERT INTO m2 VALUES (2)")
    assert_rows(
        memory_ctx.execute("SELECT * FROM merge('default', 'm[0-9]') ORDER BY a"),
        [(1,), (2,)],
    )


def test_tf_format_tsv(memory_ctx):
    """``format(FORMAT, data, structure)`` —— 把内联文本当表解析。"""
    assert_rows(
        memory_ctx.execute(
            "SELECT * FROM format('TSV', '1\\t2\\n3\\t4', 'a UInt8, b UInt8')"
        ),
        [(1, 2), (3, 4)],
    )


def test_tf_format_csv(memory_ctx):
    assert_rows(
        memory_ctx.execute(
            "SELECT * FROM format('CSV', '1,2\\n3,4', 'a UInt8, b UInt8')"
        ),
        [(1, 2), (3, 4)],
    )


def test_tf_file_reads_a_csv(memory_ctx, tmp_path):
    """``file(path, format, structure)`` 读磁盘上的文件。

    .. note::

       SQL 字符串里反斜杠是**转义符**,所以 Windows 路径必须换成正斜杠 ——
       直接写 ``C:\\...`` 会被解析成别的字符,``file()`` 就找不到文件(返回零行)。
    """
    path = tmp_path / "x.csv"
    path.write_bytes(b"1,hello\n2,world\n")
    sql_path = str(path).replace(os.sep, "/")
    result = memory_ctx.execute(
        f"SELECT * FROM file('{sql_path}', 'CSV', 'a UInt8, b String')"
    )
    assert_rows(result, [(1, "hello"), (2, "world")])


def test_tf_input_uses_attached_data(memory_ctx):
    """``input(structure)`` 读的是"解释器挂到上下文上的输入数据"。"""
    uint8 = get_data_type("UInt8")
    column = uint8.create_column()
    column.append(5)
    column.append(6)
    block = Block([ColumnWithTypeAndName("a", uint8, column)])
    ctx = memory_ctx.clone(input_data=block)
    assert_rows(ctx.execute("SELECT * FROM input('a UInt8')"), [(5,), (6,)])


def test_tf_input_without_attached_data_raises(memory_ctx):
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute("SELECT * FROM input('a UInt8')")
    assert ei.value.code == ErrorCode.NOT_IMPLEMENTED


@pytest.mark.parametrize(
    "name",
    ["remote", "cluster", "clusterAllReplicas"],
)
def test_tf_remote_like_requires_a_remote_executor(memory_ctx, name):
    """没有注入 ``remote_executor`` 时,这三个表函数要**明确说不支持**。"""
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute(f"SELECT * FROM {name}('c', 'db', 't')")
    assert ei.value.code == ErrorCode.NOT_IMPLEMENTED


@pytest.mark.parametrize(
    "name",
    ["s3", "hdfs", "jdbc", "mysql", "postgresql", "sqlite", "odbc",
     "executable", "dictionary"],
)
def test_tf_explicitly_unsupported_functions_raise_clearly(memory_ctx, name):
    """这些表函数**注册了名字但故意不实现**,调用要抛 NOT_IMPLEMENTED(带原因)。"""
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute(f"SELECT * FROM {name}('x')")
    assert ei.value.code == ErrorCode.NOT_IMPLEMENTED


# ══════════════════════════════════════════════════════════════════════════
# ⑧ 视图:普通视图(View)与物化视图(MaterializedView)
# ══════════════════════════════════════════════════════════════════════════
#
# 这一节断言的是**语义**,不是"能跑通":
#
# * 普通视图不存数据 —— 每次查询都重跑子查询,源表后来的 INSERT 立刻可见;
# * 物化视图是**插入触发器** —— 只有 INSERT 之后的行走 TO 目标表,
#   而且**只处理刚插入的那个 block**(历史数据不会被重复统计);
# * ``POPULATE`` 是**一次性**回填,重复调用不重复灌;
# * 视图是**只读**的(``INSERT`` 报 ``TABLE_IS_READ_ONLY``);
# * 没写 ``TO`` 的物化视图落在**内存内部表**上 —— 这是本实现的**限制**,
#   用测试钉住它,而不是假装它持久化了。
#
# 本条曾经是一对 ``xfail`` 测试:``CREATE VIEW`` / ``CREATE MATERIALIZED VIEW``
# 在解释器里用关键字参数(``query=`` / ``target_table=``)构造引擎,而引擎
# 的构造器只认 ``params`` 字典 —— 两条路径不一致,建视图直接 ``TypeError``。
# 现在两条路径都走 ``params``,测试换成真正的断言。


def test_view_is_expanded_into_its_subquery(memory_ctx):
    """普通视图不存数据:``SELECT * FROM v`` 等价于重跑它的子查询。"""
    memory_ctx.execute("CREATE TABLE raw (a UInt8, b UInt8) ENGINE = Memory")
    memory_ctx.execute("INSERT INTO raw VALUES (1, 10), (2, 20), (3, 30)")
    memory_ctx.execute("CREATE VIEW v AS SELECT a, b + 1 AS c FROM raw WHERE a > 1")
    assert_rows(memory_ctx.execute("SELECT * FROM v"), [(2, 21), (3, 31)])
    # 过滤条件可以直接作用在视图算出来的列上(``c`` 不是源表的列)。
    assert_rows(memory_ctx.execute("SELECT a FROM v WHERE c = 21"), [(2,)])
    # ``count()`` 也必须走"读数据"那条路:视图的 ``row_count()`` 是 None
    # (它不提供行数元数据),平凡 count 优化会因此放弃捷径。
    assert memory_ctx.execute("SELECT count() FROM v").scalar() == 2


def test_view_re_runs_its_subquery_on_every_read(memory_ctx):
    """视图不缓存结果 —— 源表后来的 INSERT 立刻能在视图里看到。"""
    memory_ctx.execute("CREATE TABLE raw (a UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE VIEW v AS SELECT a FROM raw")
    assert_rows(memory_ctx.execute("SELECT * FROM v"), [])
    memory_ctx.execute("INSERT INTO raw VALUES (1), (2)")
    assert_rows(memory_ctx.execute("SELECT * FROM v"), [(1,), (2,)])


def test_materialized_view_is_an_insert_trigger(memory_ctx):
    """物化视图把**源表的新行**加工后 push 进 ``TO`` 的目标表。"""
    memory_ctx.execute("CREATE TABLE raw (k UInt8, v UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE TABLE daily (k UInt8, s UInt64) ENGINE = Memory")
    memory_ctx.execute(
        "CREATE MATERIALIZED VIEW mv TO daily AS SELECT k, sum(v) AS s FROM raw GROUP BY k"
    )
    memory_ctx.execute("INSERT INTO raw VALUES (1, 10), (1, 20), (2, 5)")
    assert_rows_unordered(memory_ctx.execute("SELECT * FROM daily"), [(1, 30), (2, 5)])

    # **第二次 INSERT 只处理第二批行**。如果实现是"每插入一次就把源表
    # 整体重算一遍",这里会得到 (1, 130) —— 一个静默的重复统计。
    memory_ctx.execute("INSERT INTO raw VALUES (1, 100)")
    assert_rows_unordered(
        memory_ctx.execute("SELECT * FROM daily"), [(1, 30), (1, 100), (2, 5)]
    )

    # 有 ``TO`` 的物化视图读的就是目标表(它自己不存数据)。
    assert_rows_unordered(memory_ctx.execute("SELECT * FROM mv"), [(1, 30), (1, 100), (2, 5)])
    assert memory_ctx.execute("SELECT sum(s) FROM daily").scalar() == 135


def test_materialized_view_populate_backfills_once_and_is_idempotent(memory_ctx):
    """``POPULATE`` 一次性回填**建视图之前**已有的数据,且重复调用不重复灌。"""
    memory_ctx.execute("CREATE TABLE raw (k UInt8, v UInt8) ENGINE = Memory")
    memory_ctx.execute("INSERT INTO raw VALUES (1, 10), (1, 20), (2, 5)")
    memory_ctx.execute("CREATE TABLE daily (k UInt8, s UInt64) ENGINE = Memory")
    # POPULATE 写在 ``AS`` 之前(与真实 ClickHouse 的语法位置一致)。
    memory_ctx.execute(
        "CREATE MATERIALIZED VIEW mv TO daily POPULATE "
        "AS SELECT k, sum(v) AS s FROM raw GROUP BY k"
    )
    assert_rows_unordered(memory_ctx.execute("SELECT * FROM daily"), [(1, 30), (2, 5)])

    # 幂等:再回填一次返回 0,目标表一行都不变。
    engine = memory_ctx.catalog.get_table("default", "mv").engine
    assert engine.populate() == 0
    assert_rows_unordered(memory_ctx.execute("SELECT * FROM daily"), [(1, 30), (2, 5)])

    # 回填之后的 INSERT 仍然只处理新行(不会把历史再算一遍)。
    memory_ctx.execute("INSERT INTO raw VALUES (2, 7)")
    assert_rows_unordered(
        memory_ctx.execute("SELECT * FROM daily"), [(1, 30), (2, 5), (2, 7)]
    )


def test_insert_without_populate_never_sees_pre_existing_rows(memory_ctx):
    """不写 ``POPULATE`` 时,建视图之前插入的行**永远不会**被看到。

    这是"物化视图是触发器、不是刷新任务"最容易被踩到的一条:
    先灌数据、后建视图,目标表是空的。
    """
    memory_ctx.execute("CREATE TABLE raw (k UInt8) ENGINE = Memory")
    memory_ctx.execute("INSERT INTO raw VALUES (1), (2)")
    memory_ctx.execute("CREATE TABLE seen (k UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE MATERIALIZED VIEW mv TO seen AS SELECT k FROM raw")
    assert_rows(memory_ctx.execute("SELECT * FROM seen"), [])
    memory_ctx.execute("INSERT INTO raw VALUES (3)")
    assert_rows(memory_ctx.execute("SELECT * FROM seen"), [(3,)])


def test_views_are_read_only(memory_ctx):
    """两种视图都不能被直接 INSERT —— 报 ``TABLE_IS_READ_ONLY``。"""
    memory_ctx.execute("CREATE TABLE raw (a UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE VIEW v AS SELECT a FROM raw")
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute("INSERT INTO v VALUES (1)")
    assert ei.value.code == ErrorCode.TABLE_IS_READ_ONLY

    # 视图的表结构来自它的 SELECT(DESCRIBE 能看),但一样不可写。
    assert_rows(memory_ctx.execute("DESCRIBE v"), [("a", "UInt8", "", "", "", "", "")])
    memory_ctx.execute("CREATE TABLE dst (a UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE MATERIALIZED VIEW mv TO dst AS SELECT a FROM raw")
    with pytest.raises(MiniChError) as ei:
        memory_ctx.execute("INSERT INTO mv VALUES (1)")
    assert ei.value.code == ErrorCode.TABLE_IS_READ_ONLY

    # 普通视图的 ``row_count()`` 是 None("我不知道"),不是 0("是空的")——
    # 返回 0 会让 ``SELECT count() FROM v`` 被平凡 count 优化答成 0。
    assert memory_ctx.catalog.get_table("default", "v").engine.row_count() is None


def test_materialized_view_without_to_lands_in_an_in_memory_inner_table(memory_ctx):
    """没有 ``TO`` 的物化视图:结果落在**进程内的内存缓冲**里。

    .. warning::

       这条测试固化的是一个**限制**,不是"它也能当落地表用":

       真实 ClickHouse 会给没写 ``TO`` 的物化视图建一张**真正的内部表**
       (引擎取自 ``ENGINE =`` 子句:落盘、能被 ``SHOW CREATE`` 出来、
       在 ``system.tables`` 里以 ``.inner.`` 前缀出现),
       还有个后台线程在 ``DROP`` 时清掉它。
       mini-clickhouse **省略了这张内部表**:``ENGINE =`` 被忽略,
       数据只活在 :attr:`StorageMaterializedView._owned` 这个列表里,
       进程结束就没了,也不落盘。

       所以:在本实现里,"没有 TO 的物化视图"只能当**同进程内的缓存**用;
       需要一张能被 ``TO``、能被其它 MV 继续引用、能 ``SHOW CREATE`` 的
       落地表时,**必须写 ``TO``**。
    """
    memory_ctx.execute("CREATE TABLE raw (k UInt8, v UInt8) ENGINE = Memory")
    memory_ctx.execute(
        "CREATE MATERIALIZED VIEW mv ENGINE = Memory AS SELECT k, v FROM raw"
    )
    engine = memory_ctx.catalog.get_table("default", "mv").engine
    assert engine.has_inner_table() is True
    assert engine.target_table() is None

    memory_ctx.execute("INSERT INTO raw VALUES (1, 10), (2, 20)")
    memory_ctx.execute("INSERT INTO raw VALUES (3, 30)")
    assert_rows_unordered(memory_ctx.execute("SELECT * FROM mv"), [(1, 10), (2, 20), (3, 30)])
    # 每次 INSERT 攒一个 block —— 内部表就是个 block 列表,不是一个引擎。
    assert engine.describe_stats()["owned_blocks"] == 2
    assert engine.row_count() == 3


def test_materialized_view_only_fires_one_round_of_propagation(memory_ctx):
    """链式 MV 只触发**一轮**:MV2 挂在 MV1 的目标表上时不会被 MV1 带起来。

    .. warning::

       **这条测试钉住的是 mini-clickhouse 的简化,不是真实 ClickHouse 的行为。**

       真实 ClickHouse **支持级联物化视图**(官方文档有专门一节
       "Cascading materialized views"):把上一环的 ``TO`` 目标表写成
       下一环的源表,``INSERT`` 就会沿链条一级级传下去,多级 rollup
       (原始 → 小时 → 天 → 月)正是这么搭的,中间层配合
       ``sumMergeState`` / ``countMergeState`` 把子状态并成父状态。

       本实现只做一轮传播(MV 的结果直接写目标引擎,不再经过 ``INSERT``
       解释器),所以第二环及之后**不会**被触发 —— 需要多级 rollup 时
       只能把每一环当成独立的一步显式去做。
    """
    memory_ctx.execute("CREATE TABLE a (k UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE TABLE b (k UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE TABLE c (k UInt8) ENGINE = Memory")
    memory_ctx.execute("CREATE MATERIALIZED VIEW mv1 TO b AS SELECT k FROM a")
    memory_ctx.execute("CREATE MATERIALIZED VIEW mv2 TO c AS SELECT k FROM b")
    memory_ctx.execute("INSERT INTO a VALUES (1)")
    assert_rows(memory_ctx.execute("SELECT * FROM b"), [(1,)])
    # MV1 写进 b 的那一行**不会**再触发 mv2。
    assert_rows(memory_ctx.execute("SELECT * FROM c"), [])
    # 但直接往 b 里 INSERT 会触发 mv2 —— 触发器挂在**源表**上。
    memory_ctx.execute("INSERT INTO b VALUES (7)")
    assert_rows(memory_ctx.execute("SELECT * FROM c"), [(7,)])


# ══════════════════════════════════════════════════════════════════════════
# ⑨ 已发现的 minich 缺陷(xfail,非 strict)
# ══════════════════════════════════════════════════════════════════════════
#
# 这些测试**记录**缺陷而不是修复它:一旦 bug 修好,它们会 XPASS
# (非 strict,不会把套件变红)。每一条都写清"症状"与"根因"。
#


def test_context_insert_map_column(memory_ctx):
    memory_ctx.create_table("t", "m Map(String, UInt8)", engine_sql="Memory")
    memory_ctx.insert("t", [({"k": 1},)])
    assert_rows(memory_ctx.execute("SELECT * FROM t"), [({"k": 1},)])


@pytest.mark.xfail(
    raises=MiniChError,
    reason="Context.insert 的 _literal_for 不把 IPv4 当字符串,原样输出 1.2.3.4,"
           "被解析成浮点/元组取值而报 ILLEGAL_TYPE_OF_ARGUMENT",
)
def test_bug_context_insert_ipv4_column(memory_ctx):
    memory_ctx.create_table("t", "ip IPv4", engine_sql="Memory")
    memory_ctx.insert("t", [("1.2.3.4",)])
    assert_rows(memory_ctx.execute("SELECT * FROM t"),
                [(get_data_type("IPv4").normalize("1.2.3.4"),)])


@pytest.mark.xfail(
    raises=MiniChError,
    reason="ENGINE = Values 只能靠解释器调用 attach_rows 填数据,"
           "而解释器从不调用它 → 这类表永远是空的、且 INSERT 报 TABLE_IS_READ_ONLY",
)
def test_bug_values_engine_cannot_be_populated(memory_ctx):
    memory_ctx.execute("CREATE TABLE v (a UInt8) ENGINE = Values")
    memory_ctx.execute("INSERT INTO v VALUES (1)")
    assert_rows(memory_ctx.execute("SELECT * FROM v"), [(1,)])


@pytest.mark.xfail(
    raises=MiniChError,
    reason="view() 表函数在求值参数时用的是没有 subquery_executor 的求值器,"
           "于是 view((SELECT ...)) 报 'Subqueries require a subquery executor'",
)
def test_bug_view_table_function_needs_subquery_executor(memory_ctx):
    memory_ctx.execute("SELECT * FROM view((SELECT 42 AS x))")


@pytest.mark.xfail(
    raises=MiniChError,
    reason="joinGet() 没有注册进函数注册表 → UNKNOWN_FUNCTION(Join 引擎本身是好的)",
)
def test_bug_joinget_function_is_missing(memory_ctx):
    memory_ctx.execute("CREATE TABLE j (a Int32, b String) ENGINE = Join(ANY, LEFT, a)")
    memory_ctx.execute("INSERT INTO j VALUES (1, 'x')")
    assert memory_ctx.execute("SELECT joinGet('j', 'b', 1)").scalar() == "x"


@pytest.mark.xfail(
    raises=MiniChError,
    reason="裸 null(不带括号)被解析成标识符 `null`,于是当成表 'default.null' 报 UNKNOWN_TABLE;"
           "要读空结果集必须写 null()",
)
def test_bug_bare_null_from_is_not_recognized_as_table_function(memory_ctx):
    memory_ctx.execute("SELECT * FROM null")


@pytest.mark.xfail(
    raises=None,
    reason="Buffer 引擎执行 RENAME COLUMN 后,内存里的块仍按旧列名存放,"
           "读回时该列变成空串(数据没丢,但列映射错了)",
)
def test_bug_buffer_rename_column_loses_buffered_value(memory_ctx):
    memory_ctx.execute("CREATE TABLE t (a Int32, b String) ENGINE = Buffer")
    memory_ctx.execute("INSERT INTO t VALUES (1, 'x')")
    memory_ctx.execute("ALTER TABLE t RENAME COLUMN b TO bb")
    assert_rows(memory_ctx.execute("SELECT a, bb FROM t"), [(1, "x")])
