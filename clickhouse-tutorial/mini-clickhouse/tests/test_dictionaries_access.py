"""外部字典(minich/dictionaries.py)与访问控制(minich/access.py)的端到端测试。

这个文件只碰**对外可见的那一层**,不直接构造 ``Dictionary`` / ``Layout``
对象,而是老老实实走 SQL:

* 字典侧:``CREATE DICTIONARY ... SOURCE(...) LAYOUT(...) LIFETIME(...)`` +
  ``dictGet`` 家族 + ``system.dictionaries`` + ``SYSTEM RELOAD DICTIONARY``;
* 访问侧:``CREATE USER/ROLE``、``GRANT/REVOKE``、``check_access``、
  ``apply_row_policies``。

为什么要这么写? 因为这两块机制里最容易出错的地方恰恰是**接线**:DDL 解析
出来的 token 有没有正确落到 ``Dictionary`` / ``Grant`` 对象上、函数注册表
里到底有没有 ``dictGet``、系统表读的字段名对不对。只测内部对象会漏掉
整条链路。

分两大段:

1. **字典**(DICTIONARIES)——
   * 每种已实现的布局(flat / hashed / hashed_array / sparse_hashed /
     complex_key_hashed / range_hashed / cache / direct)各做一次精确查找;
   * 缺失键返回 DDL 里声明的 ``DEFAULT``;
   * ``dictGet`` 家族 + 函数注册表;
   * ``LIFETIME`` 过期后的**惰性重载**与 ``SYSTEM RELOAD DICTIONARY``;
   * ``SELECT ... FROM system.dictionaries`` 每个字典一行。

2. **访问控制**(ACCESS)——
   * GRANT / REVOKE / ``WITH GRANT OPTION``;
   * ``check_access`` 在缺权限时抛 ``ACCESS_DENIED``(497);
   * 行策略过滤行。

对**当前实现不满足、但真实 ClickHouse 应当满足**的点,用
``@pytest.mark.xfail(raises=...)`` 钉住(非 strict),把 bug 记录在案而
不假装通过 —— 与 ``test_execution.py`` / ``test_functions.py`` 的写法一致。
"""

from __future__ import annotations

import time
from typing import Any, List, Tuple

import pytest

from conftest import assert_rows
from minich.access import (
    RowPolicy,
    apply_row_policies,
    check_access,
    get_access_control,
    has_access,
)
from minich.dictionaries import reload_dictionaries
from minich.errors import ErrorCode, MiniChError
from minich.functions.base import function_exists
from minich.sql import parse


# ── 小工具 ──────────────────────────────────────────────────────────────


def _make_table(ctx: Any, name: str, ddl: str, rows: List[Tuple[Any, ...]]) -> None:
    """建一张内存表并填数据 —— 供 TABLE 源读取。"""
    ctx.create_table(name, ddl, engine_sql="Memory")
    ctx.insert(name, rows)


def _visible_rows(ctx: Any, table: str, user: str) -> List[Tuple[Any, ...]]:
    """按 ``user`` 应用行策略后,``SELECT * FROM table`` 实际能看到哪些行。

    行策略的过滤入口是 :func:`minich.access.apply_row_policies`,它作用在
    一个 ``Block`` 上。这里把整张表读成一个 Block,过滤后逐行取出。
    """
    tbl = ctx.catalog.get_table("default", table)
    block = ctx.execute(f"SELECT * FROM {table}").to_block()
    apply_row_policies(ctx, tbl, block, user=user)
    return [tuple(c.column.get(i) for c in block.data) for i in range(block.rows())]


# ══════════════════════════════════════════════════════════════════════════
# 一、字典:各布局的查找
# ══════════════════════════════════════════════════════════════════════════


def test_hashed_layout_lookup_and_missing_default(memory_ctx):
    """``hashed`` 是最通用的布局:任意键,``dict`` 查找。

    同时验证**字典最重要的语义** —— 缺失的键**不报错**,而是回该属性
    DDL 里声明的 ``DEFAULT``。
    """
    ctx = memory_ctx
    _make_table(
        ctx, "users_src",
        "id UInt64, name String, age UInt8",
        [(1, "alice", 30), (2, "bob", 25), (3, "carol", 40)],
    )
    ctx.execute(
        "CREATE DICTIONARY users_dict ("
        "  id UInt64,"
        "  name String DEFAULT 'unknown',"
        "  age UInt8 DEFAULT 0"
        ") PRIMARY KEY id "
        "SOURCE(TABLE(name 'users_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"
    )
    # 命中的键 → 源的列值
    assert_rows(ctx.execute("SELECT dictGet('users_dict', 'name', toUInt64(1))"), [("alice",)])
    assert_rows(ctx.execute("SELECT dictGet('users_dict', 'name', toUInt64(3))"), [("carol",)])
    assert_rows(ctx.execute("SELECT dictGet('users_dict', 'age', toUInt64(2))"), [(25,)])

    # 未命中的键 → 属性的 DEFAULT(不是 NULL,也不报错)
    assert_rows(ctx.execute("SELECT dictGet('users_dict', 'name', toUInt64(99))"), [("unknown",)])
    assert_rows(ctx.execute("SELECT dictGet('users_dict', 'age', toUInt64(99))"), [(0,)])


def test_flat_layout_lookup(memory_ctx):
    """``flat`` —— 键当数组下标,要求键稠密;命中和未命中都走同一套 API。"""
    ctx = memory_ctx
    _make_table(ctx, "dense_src", "id UInt64, v String",
                [(0, "zero"), (1, "one"), (2, "two")])
    ctx.execute(
        "CREATE DICTIONARY flat_dict (id UInt64, v String DEFAULT 'missing') "
        "PRIMARY KEY id SOURCE(TABLE(name 'dense_src')) "
        "LAYOUT(FLAT()) LIFETIME(0)"
    )
    assert_rows(ctx.execute("SELECT dictGetString('flat_dict', 'v', toUInt64(2))"), [("two",)])
    # 下标越界(max_key 之外)→ 同样回 DEFAULT
    assert_rows(ctx.execute("SELECT dictGetString('flat_dict', 'v', toUInt64(9))"), [("missing",)])


def test_flat_layout_rejects_sparse_key(memory_ctx):
    """``flat`` 的硬约束:键必须稠密。

    一个键 ``10^8`` 会让 ``flat`` 去开 1 亿个格子 —— 真实 ClickHouse 会
    拒绝并建议改用 ``hashed``,mini-clickhouse 同样拒绝(抛
    ``ARGUMENT_OUT_OF_BOUND``)。这一条是**正确的行为**,不是 bug。
    """
    ctx = memory_ctx
    _make_table(ctx, "sparse_src", "id UInt64, v String", [(100_000_000, "big")])
    with pytest.raises(MiniChError) as exc:
        ctx.execute(
            "CREATE DICTIONARY flat_bad (id UInt64, v String) "
            "PRIMARY KEY id SOURCE(TABLE(name 'sparse_src')) "
            "LAYOUT(FLAT()) LIFETIME(0)"
        )
    assert exc.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND
    assert "HASHED" in str(exc.value)  # 建议换布局


def test_hashed_array_layout_lookup(memory_ctx):
    """``hashed_array`` —— 键 → 行号,属性存在平行数组里;查找结果与 hashed 一致。"""
    ctx = memory_ctx
    _make_table(ctx, "ha_src", "id UInt64, v String",
                [(5, "five"), (9, "nine")])
    ctx.execute(
        "CREATE DICTIONARY ha_dict (id UInt64, v String DEFAULT 'x') "
        "PRIMARY KEY id SOURCE(TABLE(name 'ha_src')) "
        "LAYOUT(HASHED_ARRAY()) LIFETIME(0)"
    )
    assert_rows(ctx.execute("SELECT dictGetString('ha_dict', 'v', toUInt64(9))"), [("nine",)])
    assert_rows(ctx.execute("SELECT dictGetString('ha_dict', 'v', toUInt64(5))"), [("five",)])


def test_sparse_hashed_layout_lookup(memory_ctx):
    """``sparse_hashed`` —— 一整条小块的链,永不 rehash;对外行为与 hashed 相同。"""
    ctx = memory_ctx
    _make_table(ctx, "sh_src", "id UInt64, v String",
                [(5, "five"), (9, "nine")])
    ctx.execute(
        "CREATE DICTIONARY sh_dict (id UInt64, v String DEFAULT 'x') "
        "PRIMARY KEY id SOURCE(TABLE(name 'sh_src')) "
        "LAYOUT(SPARSE_HASHED()) LIFETIME(0)"
    )
    assert_rows(ctx.execute("SELECT dictGetString('sh_dict', 'v', toUInt64(5))"), [("five",)])
    assert_rows(ctx.execute("SELECT dictGetString('sh_dict', 'v', toUInt64(7))"), [("x",)])


def test_complex_key_hashed_layout_lookup(memory_ctx):
    """``complex_key_hashed`` —— 键是**元组**(复合键)。

    同一个用户在不同的 region 下是不同的行;lookup 要把两个键都传进去。
    """
    ctx = memory_ctx
    _make_table(
        ctx, "geo_src",
        "uid UInt64, region String, city String",
        [(1, "eu", "paris"), (1, "us", "nyc"), (2, "eu", "rome")],
    )
    ctx.execute(
        "CREATE DICTIONARY geo_dict ("
        "  uid UInt64, region String, city String DEFAULT '?'"
        ") PRIMARY KEY uid, region "
        "SOURCE(TABLE(name 'geo_src')) "
        "LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(0)"
    )
    assert_rows(
        ctx.execute("SELECT dictGetString('geo_dict', 'city', toUInt64(1), 'us')"),
        [("nyc",)],
    )
    assert_rows(
        ctx.execute("SELECT dictGetString('geo_dict', 'city', toUInt64(2), 'eu')"),
        [("rome",)],
    )
    # 键不存在 → DEFAULT
    assert_rows(
        ctx.execute("SELECT dictGetString('geo_dict', 'city', toUInt64(3), 'us')"),
        [("?",)],
    )


def test_range_hashed_range_semantics(memory_ctx):
    """``range_hashed`` —— 键 + ``[range_min, range_max)`` 区间,按时间点查值。

    数据(同一个 ``id=1`` 的三个连续区间)::

        [100,200) → 1.0    [200,300) → 2.0    [300,400) → 3.0

    关键断言:**边界归属左闭右开** —— ``ts=200`` 命中第二个区间(2.0),
    而不是第一个。区间外(没有覆盖的时间点)回 DEFAULT。
    """
    ctx = memory_ctx
    _make_table(
        ctx, "tariff_src",
        "id UInt64, range_min UInt64, range_max UInt64, price Float64",
        [(1, 100, 200, 1.0), (1, 200, 300, 2.0), (1, 300, 400, 3.0)],
    )
    ctx.execute(
        "CREATE DICTIONARY tariff_dict ("
        "  id UInt64, range_min UInt64, range_max UInt64, price Float64 DEFAULT -1.0"
        ") PRIMARY KEY id "
        "SOURCE(TABLE(name 'tariff_src')) "
        "LAYOUT(RANGE_HASHED()) LIFETIME(0)"
    )

    def price_at(ts: int) -> Any:
        return ctx.execute(
            f"SELECT dictGetFloat64('tariff_dict', 'price', toUInt64(1), toUInt64({ts}))"
        ).rows()

    assert price_at(150) == [(1.0,)]          # 落在 [100,200)
    assert price_at(199) == [(1.0,)]
    assert price_at(200) == [(2.0,)]          # 边界归下一个区间 [200,300)
    assert price_at(250) == [(2.0,)]
    assert price_at(300) == [(3.0,)]          # 边界归 [300,400)
    assert price_at(350) == [(3.0,)]
    assert price_at(50) == [(-1.0,)]          # 低于第一个区间下界 → DEFAULT
    assert price_at(450) == [(-1.0,)]         # 高于最后一个区间上界 → DEFAULT


def test_cache_and_direct_layouts_lookup(memory_ctx):
    """``cache``(LRU 套一层)与 ``direct``(每次问源)语义上仍是同样的查找。"""
    ctx = memory_ctx
    _make_table(ctx, "cd_src", "id UInt64, v String",
                [(1, "one"), (2, "two")])
    ctx.execute(
        "CREATE DICTIONARY cache_dict (id UInt64, v String DEFAULT 'x') "
        "PRIMARY KEY id SOURCE(TABLE(name 'cd_src')) "
        "LAYOUT(CACHE(SIZE_IN_CELLS 16)) LIFETIME(0)"
    )
    assert ctx.dictionaries["default.cache_dict"].layout_name.upper() == "CACHE"
    assert_rows(ctx.execute("SELECT dictGetString('cache_dict', 'v', toUInt64(2))"), [("two",)])

    ctx.execute(
        "CREATE DICTIONARY direct_dict (id UInt64, v String DEFAULT 'x') "
        "PRIMARY KEY id SOURCE(TABLE(name 'cd_src')) "
        "LAYOUT(DIRECT()) LIFETIME(0)"
    )
    assert_rows(ctx.execute("SELECT dictGetString('direct_dict', 'v', toUInt64(1))"), [("one",)])


# ══════════════════════════════════════════════════════════════════════════
# 二、字典:dictGet 家族与函数注册表
# ══════════════════════════════════════════════════════════════════════════


def test_dictget_family_is_registered(memory_ctx):
    """dictionaries.py 在**导入时**就把整族函数注册进标量函数表。

    这里直接问注册表,验证"导入即注册"这条约定没被打破 —— 否则一个
    只 import 了 ``minich.dictionaries`` 的进程会报 ``UNKNOWN_FUNCTION``。
    """
    for name in ("dictGet", "dictGetOrDefault", "dictGetOrNull", "dictHas",
                 "dictGetString", "dictGetUInt64", "dictGetInt32"):
        assert function_exists(name), f"函数 {name} 没有注册"
    # 大小写不敏感(注册表统一按小写存放)
    assert function_exists("dictget")


def test_dictget_variants_and_null_and_default(memory_ctx):
    """``dictGetOrNull`` 回 NULL、``dictGetOrDefault`` 回调用方给的兜底值、
    ``dictHas`` 回 0/1 —— 三种"未命中"的语义差别。"""
    ctx = memory_ctx
    _make_table(ctx, "g_src", "id UInt64, n Int32, s String",
                [(1, 42, "hi")])
    ctx.execute(
        "CREATE DICTIONARY gd (id UInt64, n Int32 DEFAULT -7, s String DEFAULT 'NONE') "
        "PRIMARY KEY id SOURCE(TABLE(name 'g_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"
    )
    # 命中
    assert_rows(ctx.execute("SELECT dictGet('gd', 'n', toUInt64(1))"), [(42,)])
    assert_rows(ctx.execute("SELECT dictGetInt32('gd', 'n', toUInt64(1))"), [(42,)])
    # dictHas:存在 → 1,不存在 → 0(UInt8)
    assert_rows(
        ctx.execute("SELECT dictHas('gd', toUInt64(1)), dictHas('gd', toUInt64(9))"),
        [(1, 0)],
    )
    # dictGetOrDefault:未命中时用**第四参数**当兜底(不是属性的 DEFAULT)
    assert_rows(
        ctx.execute("SELECT dictGetOrDefault('gd', 's', toUInt64(9), 'FALLBACK')"),
        [("FALLBACK",)],
    )
    # dictGetOrNull:未命中 → NULL
    assert_rows(ctx.execute("SELECT dictGetOrNull('gd', 's', toUInt64(9))"), [(None,)])
    # 普通 dictGet:未命中 → 属性的 DEFAULT
    assert_rows(ctx.execute("SELECT dictGet('gd', 's', toUInt64(9))"), [("NONE",)])
    assert_rows(ctx.execute("SELECT dictGet('gd', 'n', toUInt64(9))"), [(-7,)])


def test_missing_attribute_reports_error(memory_ctx):
    """取一个不存在的属性必须报错(``THERE_IS_NO_COLUMN``),而不是默默给默认值。"""
    ctx = memory_ctx
    _make_table(ctx, "e_src", "id UInt64, v String", [(1, "x")])
    ctx.execute(
        "CREATE DICTIONARY ed (id UInt64, v String) "
        "PRIMARY KEY id SOURCE(TABLE(name 'e_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"
    )
    with pytest.raises(MiniChError) as exc:
        ctx.execute("SELECT dictGet('ed', 'nope', toUInt64(1))")
    assert exc.value.code == ErrorCode.THERE_IS_NO_COLUMN


def test_undeclared_default_is_type_zero(memory_ctx):
    """属性**没有**写 DEFAULT 时,缺失键回该类型的零值(String → ``''``)。"""
    ctx = memory_ctx
    _make_table(ctx, "z_src", "id UInt64, v String", [(1, "x")])
    ctx.execute(
        "CREATE DICTIONARY zd (id UInt64, v String) "
        "PRIMARY KEY id SOURCE(TABLE(name 'z_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"
    )
    assert_rows(ctx.execute("SELECT dictGetString('zd', 'v', toUInt64(9))"), [("",)])


# ══════════════════════════════════════════════════════════════════════════
# 三、字典:LIFETIME 过期与重载
# ══════════════════════════════════════════════════════════════════════════


def test_lifetime_parse_min_max(memory_ctx):
    """``LIFETIME(MIN a MAX b)`` 存成 ``(a, b)``;``system.dictionaries.lifetime``
    对外报的是**最小值**。"""
    ctx = memory_ctx
    _make_table(ctx, "lt_src", "id UInt64, v String", [(1, "x")])
    ctx.execute(
        "CREATE DICTIONARY lt_dict (id UInt64, v String) "
        "PRIMARY KEY id SOURCE(TABLE(name 'lt_src')) "
        "LAYOUT(HASHED()) LIFETIME(MIN 300 MAX 360)"
    )
    dic = ctx.dictionaries["default.lt_dict"]
    assert dic.lifetime_config.min_seconds == 300
    assert dic.lifetime_config.max_seconds == 360
    assert dic.lifetime == 300
    assert_rows(
        ctx.execute("SELECT lifetime FROM system.dictionaries WHERE name = 'lt_dict'"),
        [(300,)],
    )


def test_lifetime_expiry_triggers_lazy_reload(memory_ctx):
    """**过期是惰性的**:到期后不是定时器去刷,而是**下一次访问**时才重读源。

    这里把生命周期设成 1 秒,改源表后先确认字典**仍给旧值**(没到期);
    再把生命周期对象的"下次刷新时刻"直接拨到过去 —— 用一个确定性的
    **快进**代替 ``sleep``,避免测试真的睡 1 秒以上 —— 然后一次
    ``dictGet`` 就会触发重载并把新值带出来。
    """
    ctx = memory_ctx
    _make_table(ctx, "rl_src", "id UInt64, v String", [(1, "old")])
    ctx.execute(
        "CREATE DICTIONARY rl_dict (id UInt64, v String DEFAULT 'd') "
        "PRIMARY KEY id SOURCE(TABLE(name 'rl_src')) "
        "LAYOUT(HASHED()) LIFETIME(1)"
    )
    assert_rows(ctx.execute("SELECT dictGetString('rl_dict', 'v', toUInt64(1))"), [("old",)])

    # 源表里插入一条同键的新值;字典没到期,仍缓存旧值
    ctx.insert("rl_src", [(1, "new")])
    assert_rows(ctx.execute("SELECT dictGetString('rl_dict', 'v', toUInt64(1))"), [("old",)])

    # 快进:把下次刷新时刻拨到过去,制造"已过期"
    lifetime = ctx.dictionaries["default.rl_dict"].lifetime_config
    assert not lifetime.is_expired()
    lifetime._next_reload = time.time() - 1
    assert lifetime.is_expired()

    # 一次访问即触发惰性重载 → 新值可见
    assert_rows(ctx.execute("SELECT dictGetString('rl_dict', 'v', toUInt64(1))"), [("new",)])


def test_manual_reload_picks_up_new_data(memory_ctx):
    """手动重载 —— 唯一的"立即刷新"手段,与生命周期无关。

    这里直接调 :func:`minich.dictionaries.reload_dictionaries`(``SYSTEM
    RELOAD DICTIONARY`` 语句背后的同一个入口;该语句本身有 bug,见下方
    xfail)。``LIFETIME(0)`` 表示永不自动刷新,所以只有手动重载才能看到新值。
    """
    ctx = memory_ctx
    _make_table(ctx, "sr_src", "id UInt64, v String", [(1, "v1")])
    ctx.execute(
        "CREATE DICTIONARY sr_dict (id UInt64, v String DEFAULT 'd') "
        "PRIMARY KEY id SOURCE(TABLE(name 'sr_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"  # LIFETIME(0) = 永不自动刷新
    )
    assert_rows(ctx.execute("SELECT dictGetString('sr_dict', 'v', toUInt64(1))"), [("v1",)])

    ctx.insert("sr_src", [(1, "v2")])
    # 永不自动刷新 → 仍是旧值
    assert_rows(ctx.execute("SELECT dictGetString('sr_dict', 'v', toUInt64(1))"), [("v1",)])

    reload_dictionaries(ctx, "sr_dict")
    assert_rows(ctx.execute("SELECT dictGetString('sr_dict', 'v', toUInt64(1))"), [("v2",)])


@pytest.mark.xfail(
    raises=AssertionError,
    reason="bug:parser.parse_system 把字典名当成命令词吞掉了 —— "
    "'SYSTEM RELOAD DICTIONARY default.sr_dict' 解析出的 command 是 "
    "'RELOAD DICTIONARY DEFAULT'、table 为 None;于是 reload_dictionaries "
    "走'重载全部'分支(名字丢失,还可能去重载别的 Context 留下的旧字典)",
)
def test_system_reload_dictionary_statement_captures_name():
    """期望 ``SYSTEM RELOAD DICTIONARY default.sr_dict`` 解析成
    ``command='RELOAD DICTIONARY'`` 且 ``table='sr_dict'``。"""
    node = parse("SYSTEM RELOAD DICTIONARY default.sr_dict")
    assert node.command == "RELOAD DICTIONARY"
    assert node.database == "default"
    assert node.table == "sr_dict"


# ══════════════════════════════════════════════════════════════════════════
# 四、字典:system.dictionaries / CREATE-DROP 生命周期
# ══════════════════════════════════════════════════════════════════════════


def test_system_dictionaries_has_row_per_dictionary(memory_ctx):
    """``system.dictionaries`` 每个字典一行,列要能反映布局、键、元素数、生命周期。"""
    ctx = memory_ctx
    _make_table(ctx, "sd_src", "id UInt64, v String",
                [(0, "z"), (1, "o"), (2, "t")])
    ctx.execute(
        "CREATE DICTIONARY sd_hashed (id UInt64, v String DEFAULT 'd') "
        "PRIMARY KEY id SOURCE(TABLE(name 'sd_src')) "
        "LAYOUT(HASHED()) LIFETIME(300)"
    )
    ctx.execute(
        "CREATE DICTIONARY sd_flat (id UInt64, v String DEFAULT 'd') "
        "PRIMARY KEY id SOURCE(TABLE(name 'sd_src')) "
        "LAYOUT(FLAT()) LIFETIME(300)"
    )
    rows = ctx.execute(
        "SELECT name, status, type, key, element_count, lifetime "
        "FROM system.dictionaries WHERE name LIKE 'sd_%' ORDER BY name"
    ).rows()
    assert rows == [
        ("sd_flat", "LOADED", "FLAT", "id", 3, 300),
        ("sd_hashed", "LOADED", "HASHED", "id", 3, 300),
    ]


def test_failed_dictionary_stays_visible_in_system_table(memory_ctx):
    """加载失败的字典**仍留在注册表里**,并在 ``system.dictionaries`` 里如实
    报 ``status='FAILED'`` 与 ``last_exception`` —— 这正是排障第一步的由来。"""
    ctx = memory_ctx
    with pytest.raises(MiniChError) as exc:
        ctx.execute(
            "CREATE DICTIONARY bad_dict (id UInt64, v String) "
            "PRIMARY KEY id SOURCE(TABLE(name 'no_such_table')) "
            "LAYOUT(HASHED()) LIFETIME(0)"
        )
    assert exc.value.code == ErrorCode.UNKNOWN_TABLE

    # 字典没消失,状态是 FAILED
    assert ctx.dictionaries["default.bad_dict"].status == "FAILED"
    rows = ctx.execute(
        "SELECT name, status, last_exception FROM system.dictionaries WHERE name = 'bad_dict'"
    ).rows()
    assert len(rows) == 1
    name, status, last_exception = rows[0]
    assert name == "bad_dict"
    assert status == "FAILED"
    assert "no_such_table" in last_exception


def test_drop_dictionary_and_if_not_exists(memory_ctx):
    """``DROP DICTIONARY`` 删注册项;不存在时报 ``UNKNOWN_TABLE``,
    ``IF EXISTS`` 则静默通过;``CREATE ... IF NOT EXISTS`` 不重建已有的字典。"""
    ctx = memory_ctx
    _make_table(ctx, "dp_src", "id UInt64, v String", [(1, "x")])
    ctx.execute(
        "CREATE DICTIONARY dp_dict (id UInt64, v String) "
        "PRIMARY KEY id SOURCE(TABLE(name 'dp_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"
    )
    # IF NOT EXISTS 命中已存在 → 不报错,仍是同一个
    ctx.execute(
        "CREATE DICTIONARY IF NOT EXISTS dp_dict (id UInt64, v String) "
        "PRIMARY KEY id SOURCE(TABLE(name 'dp_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"
    )
    assert "default.dp_dict" in ctx.dictionaries

    ctx.execute("DROP DICTIONARY default.dp_dict")
    assert "default.dp_dict" not in ctx.dictionaries

    with pytest.raises(MiniChError) as exc:
        ctx.execute("DROP DICTIONARY default.dp_dict")
    assert exc.value.code == ErrorCode.UNKNOWN_TABLE

    # IF EXISTS 版本静默通过
    ctx.execute("DROP DICTIONARY IF EXISTS default.dp_dict")


# ══════════════════════════════════════════════════════════════════════════
# 五、字典:已记录的缺口(HIERARCHICAL 属性标志)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.xfail(
    raises=MiniChError,
    reason="缺口:CREATE DICTIONARY 的属性解析只认'名字 + 类型',后面跟的"
    "HIERARCHICAL 标志会被当成下一个属性名,于是 DDL 直接报 SYNTAX_ERROR(61)"
    "—— 层级字典无法从 SQL 建出来,dictGetHierarchy 也就无从测起",
)
def test_hierarchical_attribute_flag_is_parseable(memory_ctx):
    """真实 ClickHouse 允许在属性类型后写 ``HIERARCHICAL``,``dictGetHierarchy``
    依赖它。这里期望 DDL 能建成功并给出从子到父的链 ``[2, 1]``。"""
    ctx = memory_ctx
    _make_table(ctx, "h_src", "id UInt64, parent UInt64, name String",
                [(1, 0, "root"), (2, 1, "child")])
    ctx.execute(
        "CREATE DICTIONARY h_dict ("
        "  id UInt64, parent UInt64 HIERARCHICAL, name String"
        ") PRIMARY KEY id SOURCE(TABLE(name 'h_src')) "
        "LAYOUT(HASHED()) LIFETIME(0)"
    )
    assert_rows(ctx.execute("SELECT dictGetHierarchy('h_dict', toUInt64(2))"), [([2, 1],)])


# ══════════════════════════════════════════════════════════════════════════
# 六、访问控制:GRANT / REVOKE / check_access
# ══════════════════════════════════════════════════════════════════════════


def test_grant_then_revoke_privilege(memory_ctx):
    """``GRANT SELECT ON db.t TO u`` 之后 ``has_access`` 放行;``REVOKE`` 之后被拒。"""
    ctx = memory_ctx
    _make_table(ctx, "t1", "a UInt64", [(1,)])
    _make_table(ctx, "t2", "a UInt64", [(1,)])
    ctx.execute("CREATE USER alice")

    ctx.execute("GRANT SELECT ON default.t1 TO alice")
    assert has_access(ctx, "SELECT", "default", "t1", user="alice") is True
    # 只有 t1 的权限,t2 应当没有
    assert has_access(ctx, "SELECT", "default", "t2", user="alice") is False
    # INSERT 也没授
    assert has_access(ctx, "INSERT", "default", "t1", user="alice") is False

    ctx.execute("REVOKE SELECT ON default.t1 FROM alice")
    assert has_access(ctx, "SELECT", "default", "t1", user="alice") is False


def test_grant_with_grant_option_flag(memory_ctx):
    """``WITH GRANT OPTION`` 会把这条授权的 ``grant_option`` 置位。"""
    ctx = memory_ctx
    _make_table(ctx, "t1", "a UInt64", [(1,)])
    ctx.execute("CREATE USER bob")
    ctx.execute("GRANT SELECT ON default.t1 TO bob WITH GRANT OPTION")

    ac = get_access_control(ctx)
    bob_grants = [g for g in ac.grants if g.grantee == "bob" and g.access_type == "SELECT"]
    assert len(bob_grants) == 1
    assert bob_grants[0].grant_option is True


def test_revoke_missing_privilege_raises(memory_ctx):
    """回收一条**不存在**的授权必须报 ``ACCESS_DENIED``(497)。"""
    ctx = memory_ctx
    _make_table(ctx, "t1", "a UInt64", [(1,)])
    ctx.execute("CREATE USER carol")
    with pytest.raises(MiniChError) as exc:
        ctx.execute("REVOKE SELECT ON default.t1 FROM carol")
    assert exc.value.code == ErrorCode.ACCESS_DENIED


def test_check_access_raises_access_denied_497(memory_ctx):
    """``check_access`` 在缺权限时抛 ``ACCESS_DENIED``(编号 497),
    错误消息里要能看出是谁、要什么权限。"""
    ctx = memory_ctx
    _make_table(ctx, "t1", "a UInt64", [(1,)])
    ctx.execute("CREATE USER dave")  # 建了用户但没发任何 GRANT

    with pytest.raises(MiniChError) as exc:
        check_access(ctx, "SELECT", "default", "t1", user="dave")
    assert exc.value.code == ErrorCode.ACCESS_DENIED == 497
    msg = str(exc.value)
    assert "dave" in msg
    assert "SELECT" in msg


def test_check_access_is_noop_without_access_control(memory_ctx):
    """**没有**挂访问控制的 Context 上 ``check_access`` 是空操作 ——
    mini-clickhouse 默认不拦查询(这是与真实 ClickHouse 明说的差异)。"""
    ctx = memory_ctx
    assert ctx.access is None  # 全新 Context 上还没有 AccessControl
    # 不抛异常即为通过
    check_access(ctx, "SELECT", "default", "anything", user="whoever")
    assert has_access(ctx, "SELECT", "default", "anything", user="whoever") is True


def test_role_grant_confers_privileges(memory_ctx):
    """``GRANT role TO user`` 后,角色的权限并入用户的有效权限集合。"""
    ctx = memory_ctx
    _make_table(ctx, "t2", "a UInt64", [(1,)])
    ctx.execute("CREATE USER erin")
    ctx.execute("CREATE ROLE reader")
    ctx.execute("GRANT SELECT ON default.t2 TO reader")
    # 授角色之前:没权限
    assert has_access(ctx, "SELECT", "default", "t2", user="erin") is False

    ctx.execute("GRANT reader TO erin")
    ac = get_access_control(ctx)
    assert ac.effective_roles("erin") == ["reader"]
    assert has_access(ctx, "SELECT", "default", "t2", user="erin") is True


# ══════════════════════════════════════════════════════════════════════════
# 七、访问控制:行策略
# ══════════════════════════════════════════════════════════════════════════


def test_row_policy_filters_rows(memory_ctx):
    """``CREATE ROW POLICY ... USING a >= 2`` —— 过滤掉不满足表达式的行。

    ``apply_row_policies`` 是行策略的强制入口(解释器默认不调用它,所以
    这里直接调,精确断言"哪些行看得见")。
    """
    ctx = memory_ctx
    _make_table(ctx, "rp", "a UInt64", [(1,), (2,), (3,)])
    ctx.execute("CREATE ROW POLICY p_ge2 ON default.rp FOR SELECT USING a >= 2")

    assert _visible_rows(ctx, "rp", "alice") == [(2,), (3,)]


def test_row_policy_grantees_via_python_api(memory_ctx):
    """行策略只对 ``grantees`` 里点名的用户生效 —— 用 Python API 建带
    ``grantees`` 的策略(``CREATE ROW POLICY`` 的 ``TO`` 子句见下方 xfail)。"""
    ctx = memory_ctx
    _make_table(ctx, "rp2", "a UInt64", [(1,), (2,), (3,)])
    ac = get_access_control(ctx)
    ac.row_policies["p_alice"] = RowPolicy(
        "p_alice", "default", "rp2", "a >= 2", grantees=["alice"]
    )

    assert _visible_rows(ctx, "rp2", "alice") == [(2,), (3,)]
    # bob 不在 grantees 里 → 看到全部
    assert _visible_rows(ctx, "rp2", "bob") == [(1,), (2,), (3,)]


@pytest.mark.xfail(
    raises=AssertionError,
    reason="bug:CREATE ROW POLICY 的语法解析没有处理 TO 子句 —— "
    "'... TO alice' 里的 alice 被丢弃,RowPolicy.grantees 始终为空,"
    "于是策略对**所有**用户生效(bob 也被过滤)",
)
def test_row_policy_to_clause_restricts_to_named_user(memory_ctx):
    """期望:``TO alice`` 让策略只作用于 alice,bob 应看到全部三行。"""
    ctx = memory_ctx
    ctx.execute("CREATE USER alice")
    ctx.execute("CREATE USER bob")
    _make_table(ctx, "rp3", "a UInt64", [(1,), (2,), (3,)])
    ctx.execute("CREATE ROW POLICY p_to ON default.rp3 FOR SELECT USING a >= 2 TO alice")

    assert _visible_rows(ctx, "rp3", "alice") == [(2,), (3,)]
    # 当前实现:bob 也被过滤 → 这里会失败(xfail 捕获)
    assert _visible_rows(ctx, "rp3", "bob") == [(1,), (2,), (3,)]


@pytest.mark.xfail(
    raises=AssertionError,
    reason="bug:解析器丢弃字符串字面量的引号,`USING b = 'x'` 变成 `b = x`;"
    "求值失败后 _evaluate_filter 返回 None 按'不拦'处理(fail-open),"
    "于是策略形同虚设,两行都可见",
)
def test_row_policy_with_string_literal_filters(memory_ctx):
    """期望:``USING b = 'x'`` 只留下 b='x' 的那一行。"""
    ctx = memory_ctx
    _make_table(ctx, "rp4", "a UInt64, b String", [(1, "x"), (2, "y")])
    ctx.execute("CREATE ROW POLICY p_str ON default.rp4 FOR SELECT USING b = 'x'")

    assert _visible_rows(ctx, "rp4", "alice") == [(1, "x")]
