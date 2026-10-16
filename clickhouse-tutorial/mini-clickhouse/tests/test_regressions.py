"""回归测试 —— **每个测试对应一个开发期间踩过的真实 bug**。

写这些测试的目的不是"再跑一遍功能",而是把**曾经错过一次的地方**钉住:
每一条都在注释里写清"当初的症状是什么",这样将来有人改坏了能立刻对上号。

除了解释性的注释,还包含本套件在写测试时**新发现**的一个系统性缺陷
(用 ``xfail`` 标注,非 strict):

* ``ErrorCode`` 类与 ``_CODES`` 表脱节 —— 若干被 ``minich/`` 内部引用的错误码
  没有作为类属性导出,走到那些分支时抛的是 ``AttributeError`` 而不是
  ``MiniChError``(例如元组长度不符、UUID 解析失败、Enum 元素非法)。
  该测试**动态扫描**源码,不会因为实现补齐了某几个码而过时。
"""

from __future__ import annotations

import numpy as np
import pytest

from minich.block import Block, ColumnWithTypeAndName
from minich.columns import ColumnVector
from minich.errors import ErrorCode, MiniChError
from minich.execution.context import Context, QueryResult
from minich.types import get_data_type


#: 本模块创建的 ``Context`` 都会登记到这里,由 ``_shutdown_contexts`` 统一关闭。
#: 回归测试里大量使用裸 ``Context()``;其中 ``CREATE TABLE ... ENGINE = MergeTree``
#: 会启动后台合并线程,不关就会在整场测试里泄漏线程。
_CONTEXTS = []


def _ctx() -> Context:
    c = Context()
    _CONTEXTS.append(c)
    return c


@pytest.fixture(autouse=True)
def _shutdown_contexts():
    yield
    while _CONTEXTS:
        try:
            _CONTEXTS.pop().shutdown()
        except Exception:  # pragma: no cover - 清理路径不该改变测试结果
            pass


# ══════════════════════════════════════════════════════════════════════════
# (a) 投影 bug:``SELECT 1 + 1 AS x`` 曾经返回两列
# ══════════════════════════════════════════════════════════════════════════


def test_select_expression_with_alias_projects_one_column():
    """**症状**:``SELECT 1 + 1 AS x`` 曾返回两列(表达式本身 + 别名列)。

    根因是别名没有被"吸收"进投影,而是额外追加了一列。
    """
    ctx = _ctx()
    r = ctx.execute("SELECT 1 + 1 AS x")
    assert r.column_names() == ["x"]
    assert r.rows() == [(2,)]


def test_select_multiple_aliased_expressions():
    ctx = _ctx()
    r = ctx.execute("SELECT 1 AS a, 2 + 3 AS b, 'x' AS c")
    assert r.column_names() == ["a", "b", "c"]
    assert r.rows() == [(1, 5, "x")]


# ══════════════════════════════════════════════════════════════════════════
# (b) ``LIMIT BY`` 曾经在 ``ORDER BY`` 之前执行
# ══════════════════════════════════════════════════════════════════════════


def test_limit_by_runs_after_order_by():
    """**症状**:``ORDER BY ts DESC LIMIT 2 BY uid`` 取的"每组前 2 行"是按**原始顺序**
    而不是按 ``ts`` 排序后的顺序 —— 因为 LIMIT BY 被提前到了排序之前。
    """
    ctx = _ctx()
    ctx.execute("CREATE TABLE t (uid UInt32, ts UInt32, url String) ENGINE = Memory")
    ctx.insert("t", [(1, 10, "a"), (1, 20, "b"), (1, 5, "c"), (2, 7, "d"), (2, 30, "e")])
    r = ctx.execute("SELECT uid, ts, url FROM t ORDER BY ts DESC LIMIT 2 BY uid")
    # 全局降序:30,20,10,7,5;每组留 2 个:uid2→30,7;uid1→20,10
    assert r.rows() == [(2, 30, "e"), (1, 20, "b"), (1, 10, "a"), (2, 7, "d")]


# ══════════════════════════════════════════════════════════════════════════
# (c) 聚合检测曾经"钻进"标量子查询
# ══════════════════════════════════════════════════════════════════════════


def test_aggregate_detection_does_not_descend_into_scalar_subquery():
    """**症状**:``SELECT uid, (SELECT max(y) FROM t2) FROM t`` 里,外层的
    ``max(y)`` 被错误地当成外层聚合,导致整个查询塌成一行(变成了聚合查询)。

    正确语义:子查询的聚合**不外泄**,外层是逐行的非聚合查询。
    """
    ctx = _ctx()
    ctx.execute("CREATE TABLE t (uid UInt32) ENGINE = Memory")
    ctx.execute("CREATE TABLE t2 (y UInt32) ENGINE = Memory")
    ctx.insert("t", [(1,), (2,)])
    ctx.insert("t2", [(5,), (7,)])
    r = ctx.execute("SELECT uid, (SELECT max(y) FROM t2) AS m FROM t ORDER BY uid")
    assert r.rows() == [(1, 7), (2, 7)]     # 两行,不是一行


def test_scalar_subquery_in_where_does_not_aggregate_outer():
    ctx = _ctx()
    ctx.execute("CREATE TABLE t2 (y UInt32) ENGINE = Memory")
    ctx.insert("t2", [(10,)])
    ctx.execute("CREATE TABLE t (x UInt32) ENGINE = Memory")
    ctx.insert("t", [(5,), (15,)])
    r = ctx.execute("SELECT x FROM t WHERE x > (SELECT max(y) FROM t2) ORDER BY x")
    assert r.rows() == [(15,)]


# ══════════════════════════════════════════════════════════════════════════
# (d) ``if(x, NULL, y)`` 曾经在 ``Nullable(Nothing)`` 上崩
# ══════════════════════════════════════════════════════════════════════════


def test_if_with_null_branch_uses_nullable_nothing():
    """**症状**:``SELECT if(1, NULL, 'a')`` 直接报错 —— 因为 ``NULL`` 的类型是
    ``Nullable(Nothing)``,而实现初期把 ``Nothing`` 标成了"不可空"。
    """
    ctx = _ctx()
    assert ctx.execute("SELECT if(1, NULL, 'a') AS v").rows() == [(None,)]
    assert ctx.execute("SELECT if(0, NULL, 'a') AS v").rows() == [("a",)]


def test_select_bare_null_is_nullable_nothing():
    ctx = _ctx()
    r = ctx.execute("SELECT NULL AS n")
    assert r.rows() == [(None,)]
    assert r.column_types() == ["Nullable(Nothing)"]


# ══════════════════════════════════════════════════════════════════════════
# (e) ``x IN ('a','c')`` 曾经返回空
# ══════════════════════════════════════════════════════════════════════════


def test_string_in_list_returns_matches():
    """**症状**:``WHERE url IN ('a','c')`` 一直返回空集 —— 字符串元组列表的比较走错了分支。"""
    ctx = _ctx()
    ctx.execute("CREATE TABLE t (url String) ENGINE = Memory")
    ctx.insert("t", [("a",), ("b",), ("c",)])
    r = ctx.execute("SELECT url FROM t WHERE url IN ('a', 'c') ORDER BY url")
    assert r.rows() == [("a",), ("c",)]
    r2 = ctx.execute("SELECT url FROM t WHERE url NOT IN ('a', 'c') ORDER BY url")
    assert r2.rows() == [("b",)]


# ══════════════════════════════════════════════════════════════════════════
# (f) ``arrayMap`` 曾经在 ``LambdaArgument`` 上崩
# ══════════════════════════════════════════════════════════════════════════


def test_array_map_with_lambda_argument():
    """**症状**:``arrayMap(x -> x*2, arr)`` 崩溃 —— LambdaArgument 不是普通列,
    求值器把它当列处理时报错。
    """
    ctx = _ctx()
    assert ctx.execute("SELECT arrayMap(x -> x * 2, [1, 2, 3]) AS m").rows() == [([2, 4, 6],)]


def test_array_filter_with_lambda():
    ctx = _ctx()
    r = ctx.execute("SELECT arrayFilter(x -> x > 1, [1, 2, 3]) AS m")
    assert r.rows() == [([2, 3],)]


# ══════════════════════════════════════════════════════════════════════════
# (g) ``SELECT count() FROM empty`` 曾经返回零行
# ══════════════════════════════════════════════════════════════════════════


def test_count_from_empty_table_returns_one_row_zero():
    """**症状**:空表上 ``SELECT count()`` 返回**零行**,而不是"一行 0"。

    语义上聚合查询**总是**产出一行(即使输入为空,只是分组为空)。
    """
    ctx = _ctx()
    ctx.execute("CREATE TABLE empty (x UInt8) ENGINE = Memory")
    r = ctx.execute("SELECT count() FROM empty")
    assert r.rows() == [(0,)]


def test_group_by_on_empty_table_returns_zero_rows():
    """与上一条相反:带 ``GROUP BY`` 的空输入产出**零行**(没有任何分组)。"""
    ctx = _ctx()
    ctx.execute("CREATE TABLE empty (k UInt8, v UInt8) ENGINE = Memory")
    r = ctx.execute("SELECT k, count() FROM empty GROUP BY k")
    assert r.rows() == []


def test_sum_from_empty_table_is_one_row_zero():
    ctx = _ctx()
    ctx.execute("CREATE TABLE empty (x UInt64) ENGINE = Memory")
    assert ctx.execute("SELECT sum(x) FROM empty").rows() == [(0,)]


# ══════════════════════════════════════════════════════════════════════════
# (h) ``CREATE TABLE t2 AS t1`` 曾经丢掉排序键
# ══════════════════════════════════════════════════════════════════════════


def test_create_table_as_copies_sorting_key():
    """**症状**:``CREATE TABLE t2 AS t1`` 只复制了列定义,``ORDER BY`` 丢了 ——
    t2 变成了没有排序键的 MergeTree,查询计划完全不同。
    """
    ctx = _ctx()
    ctx.execute("CREATE TABLE t1 (id UInt64, v String) ENGINE = MergeTree ORDER BY id")
    ctx.execute("CREATE TABLE t2 AS t1")
    t1 = ctx.catalog.get_table("default", "t1")
    t2 = ctx.catalog.get_table("default", "t2")
    assert t2.engine.get_sorting_key_description() == "id"
    assert t2.engine.get_sorting_key_description() == t1.engine.get_sorting_key_description()
    assert t2.get_column_names() == t1.get_column_names()


# ══════════════════════════════════════════════════════════════════════════
# (i) ``MATERIALIZED`` 列曾经被当成"非存储列"
# ══════════════════════════════════════════════════════════════════════════


def test_materialized_column_is_stored_and_computed():
    """**症状**:``MATERIALIZED`` 列被当成"不存储"的列 —— 于是 ``INSERT`` 时不算它、
    ``SELECT`` 时又读不到,值永远为空。

    正确语义:MATERIALIZED 列的**值由表达式生成**,但它在语义上是"存在"的。
    """
    ctx = _ctx()
    ctx.execute("CREATE TABLE mt (a UInt8, b UInt8 MATERIALIZED a + 1) ENGINE = Memory")
    ctx.insert("mt", [(5,)], columns=["a"])
    assert ctx.execute("SELECT a, b FROM mt").rows() == [(5, 6)]
    b_col = ctx.catalog.get_table("default", "mt").get_column("b")
    assert b_col.is_stored() is True


# ══════════════════════════════════════════════════════════════════════════
# (j) 循环里对**同一个** Block 反复 permute 越界(``_split_by_partition`` bug)
# ══════════════════════════════════════════════════════════════════════════


def _block(n):
    return Block([
        ColumnWithTypeAndName("x", get_data_type("UInt32"),
                              ColumnVector(np.uint32, list(range(n))))
    ])


def test_permute_is_in_place_and_the_documented_loop_trap():
    """**症状**:按分区切分数据时,在循环里对**同一个** Block 反复 ``permute`` ——
    第一次 permute 就把 Block 缩小了,第二次的索引越界,报的是毫无线索的
    ``IndexError: index ... out of bounds``。

    这里把"陷阱本身"和"正确做法"都固定下来:``permute`` 就地改,
    ``take`` 返回新块因此可以反复调用。
    """
    b = _block(10)
    sub = b.permute([0, 1, 2])          # 就地:原 Block 变成 3 行
    assert b.rows() == 3
    assert sub is b
    with pytest.raises(IndexError):
        b.permute([5, 6, 7])            # 第二次用旧索引 → 越界


def test_take_in_a_loop_is_safe():
    """正确做法:用 ``take`` —— 它返回**新** Block,可以反复对同一个源取子集。"""
    src = _block(10)
    parts = [src.take([0, 1]), src.take([2, 3, 4]), src.take([5])]
    assert [p.rows() for p in parts] == [2, 3, 1]
    assert src.rows() == 10
    assert parts[0].get_by_name("x").column.to_list() == [0, 1]


# ══════════════════════════════════════════════════════════════════════════
# 附加发现 1:包层面导出的 ``Context`` 走的是惰性 ``__getattr__``
# ══════════════════════════════════════════════════════════════════════════


def test_package_exports_context_as_documented():
    """``minich/__init__.py`` 的文档写着 ``import minich; minich.Context()``。

    它靠 PEP 562 的模块级 ``__getattr__`` **惰性**导出 —— 这样
    ``import minich`` 不会把整个引擎都加载进来(文档里专门解释了原因)。
    这里验证文档承诺的入口确实可用,且它就是 ``execution.context.Context``。
    """
    import minich as ch
    assert ch.Context is Context
    # 惰性导出的其它公共名字也应当**就是**那几个类(不是"非空"就算过)
    assert ch.QueryResult is QueryResult
    assert ch.MiniChError is MiniChError


# ══════════════════════════════════════════════════════════════════════════
# 附加发现 2:ErrorCode 类与 _CODES 表脱节(系统性问题)
# ══════════════════════════════════════════════════════════════════════════

def _referenced_errorcode_attrs() -> list:
    """扫描 ``minich/`` 源码,找出**被引用但没有定义**的 ``ErrorCode.X`` 属性。

    刻意做成"动态扫描"而不是写死一份名单 —— 实现一直在演进(有些缺失项
    会被补上),写死的名单会过时。扫描用的是逐行正则,只看 ``ErrorCode.X``
    (用负向后顾排除 ``KeeperErrorCode.X`` 这类同后缀名字)。
    """
    import os
    import re

    import minich

    root = os.path.dirname(minich.__file__)
    defined = {a for a in dir(ErrorCode) if a.isupper()}
    pattern = re.compile(r"(?<![A-Za-z0-9_])ErrorCode\.([A-Z_0-9]+)")
    used = set()
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            with open(os.path.join(dirpath, fname), encoding="utf-8") as fh:
                for line in fh:
                    used.update(pattern.findall(line))
    return sorted(used - defined)


def test_errorcode_class_defines_every_referenced_code():
    """断言"规格":``minich/`` 引用的每个 ``ErrorCode.X`` 都应当是类的属性。

    缺陷存在时它失败(记录在 ``xfail`` 下);缺陷被补全后它会变绿。
    """
    missing = _referenced_errorcode_attrs()
    assert missing == [], f"ErrorCode 缺少这些被引用的属性: {missing}"


# ══════════════════════════════════════════════════════════════════════════
# 回归:每一个 ``ErrorCode.<NAME>`` 都必须真的存在
# ══════════════════════════════════════════════════════════════════════════
#
# 这一类 bug 在实现过程中**出现过五次**:``CANNOT_READ_ALL_DATA``、
# ``CANNOT_PARSE_FLOAT``、``READONLY``、``ILLEGAL_DIVISION``、
# ``CANNOT_WRITE_TO_OSTREAM`` —— 代码里写 ``raise_error(ErrorCode.XXX, …)``,
# 而 ``XXX`` 从来没有在类上定义过。后果是**抛出的不是那个错误**:
#
#   AttributeError: type object 'ErrorCode' has no attribute 'CANNOT_WRITE_TO_OSTREAM'
#
# 它比"错误消息不好"严重得多:调用方拿不到 ``MiniChError``、拿不到错误码,
# 而**只有走到那条错误路径时才会暴露** —— 常规测试覆盖不到。
#
# 所以把它变成一条**结构性**断言:全仓库扫一遍,任何被引用的名字都必须在
# 类上存在。这比"为每个错误路径写一个测试"可靠得多,因为它**不需要**你
# 想到每一条错误路径。


def test_every_referenced_error_code_is_defined():
    """``minich/`` 里引用的每个 ``ErrorCode.X`` 都必须真的定义过。"""
    import pathlib
    import re

    from minich.errors import ErrorCode

    root = pathlib.Path(__file__).resolve().parent.parent / "minich"
    assert root.is_dir(), f"找不到 minich 源码目录: {root}"

    defined = {name for name in dir(ErrorCode) if name.isupper()}
    assert defined, "ErrorCode 上一个常量都没有?"

    # 前面不能是标识符字符 —— 否则 ``ZKErrorCode.NONODE`` 会被误认成
    # ``ErrorCode.NONODE``(keeper 里就有这个类)。
    pattern = re.compile(r"(?<![A-Za-z0-9_])ErrorCode\.([A-Z][A-Z0-9_]*)")

    missing: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            if m.group(1) not in defined:
                missing.setdefault(m.group(1), []).append(path.name)

    assert not missing, (
        "这些错误码被引用了但 ErrorCode 上没有定义(走到那条路径会抛 "
        f"AttributeError 而不是 MiniChError):{missing}"
    )


def test_error_code_numbers_are_unique_per_name():
    """同一个编号不能对应两个名字 —— 否则查表的结果取决于插入顺序。"""
    from minich.errors import _CODES

    seen: dict[str, int] = {}
    for code, name in sorted(_CODES.items()):
        assert name not in seen, f"错误名 {name} 同时映射到 {seen.get(name)} 与 {code}"
        seen[name] = code
