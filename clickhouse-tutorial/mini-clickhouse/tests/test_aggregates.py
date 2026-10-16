"""聚合函数测试 —— 覆盖 ``minich.aggregates`` 的注册表、三条边界与全部组合器。

本文件**只测聚合函数本身**(在 API 层直接调用),不碰执行层。分九块:

1. **注册表与名字解析** —— ``get_aggregate_function`` / ``split_combinators``
   / ``aggregate_function_exists``;
2. **序列化往返** —— 对注册表里**每一个**名字都跑一遍
   ``deserialize_state(serialize_state(s))`` 并断言“再序列化得到同一串字节”;
3. **merge** —— 把输入切成两半分别聚合再 ``merge``,结果必须等于一次跑完
   (这就是“分布式聚合 / AggregatingMergeTree”的核心不变量);
4. **``insert_result_into`` 的精确结果** —— ``sum([1,2,3]) == 6`` 这类逐字断言;
5. **组合器** —— ``-If`` / ``-Array`` / ``-State`` / ``-Merge`` / ``-Distinct``
   / ``-OrNull`` / ``-OrDefault`` / ``-ForEach``,以及可叠加的 ``sumIfMerge``;
6. **HyperLogLog** —— 误差界、合并、精度边界、克隆独立性;
7. **空集 / NULL 语义**;
8. **明确未实现的函数**(``maxIntersections``)—— 用 ``xfail()`` 记录;
9. **SQL 端到端** —— 借 ``ev`` 夹具验证聚合确实接进了执行层。

关于“为什么用 ``ValueColumn``”:聚合的 ``add`` 入口是“列 + 行号”,所以喂数据
时必须把一串 Python 值伪装成列。``ValueColumn`` 就是为此提供的极薄适配器
(见 ``minich/aggregates/base.py``)。
"""

from __future__ import annotations

import math

import pytest

from conftest import assert_rows
from minich.aggregates import (
    HyperLogLog,
    ValueColumn,
    aggregate_function_exists,
    get_aggregate_function,
    registered_aggregate_functions,
    split_combinators,
)
from minich.errors import ErrorCode, MiniChError
from minich.types import get_data_type
from minich.types.composite import DataTypeAggregateFunction

# ── 常用类型(模块级常量,避免每个测试重复构造) ─────────────────────────

U = get_data_type("UInt64")
U8 = get_data_type("UInt8")
F64 = get_data_type("Float64")
STR = get_data_type("String")
ARR_U64 = get_data_type("Array(UInt64)")
ARR_U8 = get_data_type("Array(UInt8)")


# ══════════════════════════════════════════════════════════════════════════
# 小工具:喂数据、取结果、比较
# ══════════════════════════════════════════════════════════════════════════


def _cols(rows):
    """把“行列表”转置成“列列表”——``add`` 要的是一列一列的数据。"""
    if not rows:
        return []
    return [list(c) for c in zip(*rows)]


def _make(name, params=(), arg_types=()):
    """按名字构造聚合函数实例(参数与参数类型都可选)。"""
    return get_aggregate_function(name, list(params), list(arg_types))


def _feed(f, rows):
    """新建一个空状态,把 ``rows`` 一行行喂进去,返回状态。"""
    cols = [ValueColumn(c) for c in _cols(rows)]
    state = f.create_state()
    for row_index in range(len(rows)):
        f.add(state, cols, row_index)
    return state


def _feed_range(f, cols, lo, hi):
    """只喂 ``cols`` 的 ``[lo, hi)`` 行 —— 用于“切两半再 merge”的测试。"""
    state = f.create_state()
    for row_index in range(lo, hi):
        f.add(state, cols, row_index)
    return state


def _result(f, state):
    """跑 ``insert_result_into`` 并取出刚 append 的那个值。"""
    column = f.get_return_type().create_column()
    f.insert_result_into(state, column)
    return column.get(0)


def _run(name, arg_types, rows, params=()):
    f = _make(name, params, arg_types)
    return _result(f, _feed(f, rows))


def _close(got, want):
    """容错比较:浮点用 ``math.isclose``,``nan`` 视作相等,其余用 ``==``。"""
    if isinstance(want, float):
        if math.isnan(want):
            return isinstance(got, float) and math.isnan(got)
        return math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(want, list) and any(isinstance(w, float) for w in want):
        return len(got) == len(want) and all(_close(g, w) for g, w in zip(got, want))
    return got == want


#: 注册表里的全部名字(小写)。
_REGISTERED = [name for name, _ in registered_aggregate_functions()]

#: ``create_state`` 直接抛错的函数(见专门的 xfail 测试)。
_UNIMPLEMENTED = {"maxintersections"}

#: ``insert_result_into`` 是空操作、结果列取不出值的函数(见 ``ColumnNothing``)。
_NO_RESULT = {"nothing", "nothingnull"}

#: 有些函数至少需要两个参数才能构造;给它们一个专门的喂数方案。
#: 键是小写名字,值是 ``(arg_types, rows)``。
_SPEC_OVERRIDES = {
    "argmin": ([STR, U], [("/a", 3), ("/b", 1), ("/c", 2)]),
    "argmax": ([STR, U], [("/a", 3), ("/b", 1), ("/c", 2)]),
    "avgweighted": ([F64, F64], [(1.0, 2.0), (2.0, 4.0), (3.0, 6.0)]),
    "corr": ([F64, F64], [(1.0, 2.0), (2.0, 4.0), (3.0, 5.0)]),
    "covar": ([F64, F64], [(1.0, 2.0), (2.0, 4.0), (3.0, 5.0)]),
    "covarpop": ([F64, F64], [(1.0, 2.0), (2.0, 4.0), (3.0, 5.0)]),
    "covarsamp": ([F64, F64], [(1.0, 2.0), (2.0, 4.0), (3.0, 5.0)]),
    "rankcorr": ([F64, F64], [(1.0, 2.0), (2.0, 4.0), (3.0, 5.0)]),
    "sequencecount": ([U, U8, U8], [(1, 1, 0), (2, 0, 1), (3, 1, 0), (4, 0, 1)]),
    "summap": ([ARR_U64, ARR_U64], [([1, 2], [10, 20]), ([2, 3], [5, 5])]),
}

#: 默认喂数方案:一列 UInt64,值为 ``1, 2, 2, 3``(含重复,便于测去重)。
_DEFAULT_SPEC = ([U], [(1,), (2,), (2,), (3,)])


def _spec(name):
    return _SPEC_OVERRIDES.get(name, _DEFAULT_SPEC)


# ══════════════════════════════════════════════════════════════════════════
# 1. 注册表与名字解析
# ══════════════════════════════════════════════════════════════════════════


def test_registry_lists_core_functions():
    """注册表能把招牌函数都列出来,而且数量是“几十个”这个量级。"""
    names = {name for name, _ in registered_aggregate_functions()}
    expected = {
        "sum", "count", "avg", "min", "max", "any", "anylast", "anyheavy",
        "argmin", "argmax", "uniq", "uniqexact", "quantile", "median",
        "grouparray", "countdistinct", "summap", "varpop", "entropy", "topk",
    }
    assert expected <= names
    # 组合器变体(sumIf 等)不进注册表 —— 它们是运行时拼出来的,
    # 所以这里断言“不包含 sumif”同样重要。
    assert "sumif" not in names
    assert len(names) >= 60


def test_registry_rows_are_lowercase_name_description_pairs():
    """``registered_aggregate_functions`` 的形状:``[(小写名, 非空说明), ...]``。"""
    rows = registered_aggregate_functions()
    assert isinstance(rows, list)
    for name, description in rows:
        assert isinstance(name, str) and name == name.lower()
        assert isinstance(description, str) and description, f"{name} 缺少说明"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("sum", True),
        ("sumIf", True),
        ("sumArrayIf", True),
        ("sumIfMerge", True),
        ("quantilesTiming", True),
        ("countDistinct", True),
        ("uniqArray", True),
        ("definitelyNotAggregate", False),
        ("summ", False),
    ],
)
def test_aggregate_function_exists(name, expected):
    """存在性判断要能穿透组合器后缀,也要能对不存在的名字说不。"""
    assert aggregate_function_exists(name) is expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("sum", ("sum", None, False, False)),
        ("sumIf", ("sum", "If", False, False)),
        ("sumIfMerge", ("sum", "IfMerge", False, True)),
        ("sumIfState", ("sum", "IfState", True, False)),
        ("uniqArray", ("uniq", "Array", False, False)),
        ("sumForEach", ("sum", "ForEach", False, False)),
        ("sumDistinct", ("sum", "Distinct", False, False)),
        # 本身就是注册过的整名 —— **不能**被误拆成 group+Array / count+Distinct。
        ("groupArray", ("groupArray", None, False, False)),
        ("countDistinct", ("countDistinct", None, False, False)),
        ("sumMap", ("sumMap", None, False, False)),
    ],
)
def test_split_combinators(name, expected):
    """组合器拆分:基础名 / 链 / 是否 -State / 是否 -Merge。"""
    assert split_combinators(name) == expected


def test_unknown_aggregate_function_raises():
    """不认识的名字要抛 ``UNKNOWN_AGGREGATE_FUNCTION``,而不是静默返回空。"""
    with pytest.raises(MiniChError) as excinfo:
        get_aggregate_function("definitelyNotAFunction", [], [U])
    assert excinfo.value.code == ErrorCode.UNKNOWN_AGGREGATE_FUNCTION


def test_name_embedded_params_are_parsed():
    """``quantiles(0.5, 0.9)`` 这种“参数写在名字里”的形态也要能解析。"""
    f = get_aggregate_function("quantiles(0.5, 0.9)", [], [U])
    assert f.params == [0.5, 0.9]
    assert f.get_return_type().get_name() == "Array(Float64)"


# ══════════════════════════════════════════════════════════════════════════
# 2. 序列化往返 —— 对注册表里每一个名字都跑
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("name", _REGISTERED)
def test_state_serialize_roundtrip_is_deterministic(name):
    """喂完数据后 ``deserialize∘serialize`` 必须回到“同一串字节”。

    这是 ``AggregatingMergeTree`` 的硬要求:状态落盘再读回,必须一模一样,
    否则“合并部分状态”会得到和单机不一致的结果。
    """
    if name in _UNIMPLEMENTED:
        pytest.skip("maxIntersections 的 create_state 未实现(见专门的 xfail 测试)")

    arg_types, rows = _spec(name)
    f = _make(name, (), arg_types)
    state = _feed(f, rows)

    blob = f.serialize_state(state)
    restored = f.deserialize_state(blob)
    assert f.serialize_state(restored) == blob


@pytest.mark.parametrize("name", _REGISTERED)
def test_deserialized_state_yields_same_result(name):
    """状态往返之后,``insert_result_into`` 吐出的结果也必须不变。"""
    if name in _UNIMPLEMENTED:
        pytest.skip("maxIntersections 的 create_state 未实现")
    if name in _NO_RESULT:
        pytest.skip(f"{name} 的 insert_result_into 是空操作(Nothing 列不能 append)")

    arg_types, rows = _spec(name)
    f = _make(name, (), arg_types)
    state = _feed(f, rows)
    expected = _result(f, state)

    restored = f.deserialize_state(f.serialize_state(state))
    assert _close(_result(f, restored), expected), f"{name} 往返后结果变了"


def test_nothing_serializes_to_empty_and_onwards():
    """``nothing`` / ``nothingNull``:状态是 ``None``,序列化就是空字节。"""
    for name in ("nothing", "nothingNull"):
        f = _make(name, (), [])
        state = f.create_state()
        assert state is None
        assert f.serialize_state(state) == b""
        assert f.deserialize_state(b"") is None
        # ``add`` / ``merge`` 都是空操作 —— 不该因“没有参数列”而报错。
        f.add(state, [], 0)
        f.merge(state, None)


def test_nothing_return_types():
    """``nothing()`` → ``Nothing``;``nothingNull()`` → ``Nullable(Nothing)``。"""
    assert _make("nothing", (), []).get_return_type().get_name() == "Nothing"
    assert _make("nothingNull", (), []).get_return_type().get_name() == "Nullable(Nothing)"


# ══════════════════════════════════════════════════════════════════════════
# 3. merge == 一次跑完
# ══════════════════════════════════════════════════════════════════════════


#: 每个用例:``(id, 函数名, params, arg_types, rows, 期望值或 None)``。
#: 期望值给 ``None`` 表示“只比较 merge 与单次聚合是否相等”。
_MERGE_CASES = [
    ("sum", "sum", (), [U], [(1,), (2,), (3,), (4,)], 10),
    ("count", "count", (), [U], [(1,), (2,), (3,), (4,)], 4),
    ("avg", "avg", (), [U], [(1,), (2,), (3,), (4,)], 2.5),
    ("min", "min", (), [U], [(4,), (1,), (3,), (2,)], 1),
    ("max", "max", (), [U], [(4,), (1,), (3,), (2,)], 4),
    ("sumCount", "sumCount", (), [U], [(1,), (2,), (3,), (4,)], (10, 4)),
    ("uniq", "uniq", (), [U], [(1,), (2,), (2,), (3,)], 3),
    ("uniqExact", "uniqExact", (), [U], [(1,), (2,), (2,), (3,)], 3),
    ("countDistinct", "countDistinct", (), [U], [(1,), (1,), (2,), (3,)], 3),
    ("groupArray", "groupArray", (), [U], [(1,), (2,), (3,), (4,)], [1, 2, 3, 4]),
    ("groupUniqArray", "groupUniqArray", (), [U], [(3,), (1,), (3,), (2,)], [1, 2, 3]),
    ("argMax", "argMax", (), [STR, U], [("/a", 1), ("/b", 4), ("/c", 3), ("/d", 2)], "/b"),
    ("quantile", "quantile", (), [U], [(1,), (2,), (3,), (4,)], 2.5),
    ("median", "median", (), [U], [(1,), (2,), (3,), (4,)], 2.5),
    ("topK", "topK", (2,), [U], [(1,), (1,), (2,), (3,)], [1, 2]),
    ("entropy", "entropy", (), [U], [(1,), (1,), (2,), (3,)], 1.5),
    ("varPop", "varPop", (), [U], [(1,), (2,), (3,), (4,)], 1.25),
    ("anyHeavy", "anyHeavy", (), [U], [(1,), (2,), (2,), (3,)], 2),
    ("sumIf", "sumIf", (), [U, U8], [(1, 1), (2, 0), (3, 1), (4, 0)], 4),
    ("sumMap", "sumMap", (), [ARR_U64, ARR_U64],
     [([1, 2], [10, 20]), ([2, 3], [5, 5])], {1: 10, 2: 25, 3: 5}),
    ("sumKahan", "sumKahan", (), [F64], [(1.5,), (2.5,)], 4.0),
    ("corr", "corr", (), [F64, F64], [(1.0, 2.0), (2.0, 4.0), (3.0, 6.0), (4.0, 8.0)], 1.0),
    ("histogram", "histogram", (2,), [U], [(1,), (2,), (3,), (4,)], None),
]


@pytest.mark.parametrize(
    "name,fn,params,arg_types,rows,expected",
    _MERGE_CASES,
    ids=[case[0] for case in _MERGE_CASES],
)
def test_merge_two_halves_equals_single_pass(name, fn, params, arg_types, rows, expected):
    """把输入切成两半各算一份状态,``merge`` 起来必须等于一次跑完。

    这是“分布式聚合”和 ``AggregatingMergeTree`` 的全部不变量:每个分片
    各算一半,归并时把状态并起来,结果不能变。
    """
    f = _make(fn, params, arg_types)
    cols = [ValueColumn(c) for c in _cols(rows)]

    full = _result(f, _feed(f, rows))

    half = len(rows) // 2
    left = _feed_range(f, cols, 0, half)
    right = _feed_range(f, cols, half, len(rows))
    f.merge(left, right)
    merged = _result(f, left)

    assert _close(merged, full), f"{name}: merge 结果与单次聚合不一致"
    if expected is not None:
        assert _close(merged, expected), f"{name}: merge 结果不等于期望值"


@pytest.mark.parametrize("name", ["uniq", "uniqExact", "countDistinct", "sum"])
def test_merge_result_is_order_independent_for_commutative_functions(name):
    """``merge(a, b)`` 与 ``merge(b, a)`` 必须给出同一个结果(这些函数可交换)。

    ``groupArray`` **不在**这里 —— 它是可结合的但不可交换:合并顺序会改变
    数组元素的排列(见下面的 ``test_group_array_merge_order_is_observable``)。
    """
    rows = [(1,), (2,), (2,), (3,)]
    f = _make(name, (), [U])
    cols = [ValueColumn(c) for c in _cols(rows)]

    a = _feed_range(f, cols, 0, 2)
    b = _feed_range(f, cols, 2, 4)
    f.merge(a, b)
    forward = _result(f, a)

    a2 = _feed_range(f, cols, 0, 2)
    b2 = _feed_range(f, cols, 2, 4)
    f.merge(b2, a2)
    backward = _result(f, b2)

    assert _close(forward, backward)


def test_group_array_merge_order_is_observable():
    """``groupArray`` 的可交换性只到“元素多重集”为止 —— 顺序由合并顺序决定。"""
    rows = [(1,), (2,), (2,), (3,)]
    f = _make("groupArray", (), [U])
    cols = [ValueColumn(c) for c in _cols(rows)]

    a = _feed_range(f, cols, 0, 2)
    b = _feed_range(f, cols, 2, 4)
    f.merge(a, b)
    forward = _result(f, a)

    a2 = _feed_range(f, cols, 0, 2)
    b2 = _feed_range(f, cols, 2, 4)
    f.merge(b2, a2)
    backward = _result(f, b2)

    assert forward == [1, 2, 2, 3]
    assert sorted(forward) == sorted(backward)
    assert forward != backward  # 顺序确实被合并顺序左右


# ══════════════════════════════════════════════════════════════════════════
# 4. insert_result_into 的精确结果
# ══════════════════════════════════════════════════════════════════════════


#: ``(id, 函数名, params, arg_types, rows, 期望值)``。
_EXACT_CASES = [
    ("sum", "sum", (), [U], [(1,), (2,), (3,)], 6),
    ("sum_uint8_promotes_to_uint64", "sum", (), [U8], [(200,), (200,)], 400),
    ("sum_uint8_overflow_wraps", "sumWithOverflow", (), [U8], [(200,), (200,)], 144),
    ("avg", "avg", (), [U], [(1,), (2,), (3,)], 2.0),
    ("count_star", "count", (), [], [(), (), ()], 3),
    ("count_column", "count", (), [U], [(1,), (2,), (3,)], 3),
    ("min", "min", (), [U], [(3,), (1,), (2,)], 1),
    ("max", "max", (), [U], [(3,), (1,), (2,)], 3),
    ("sumCount", "sumCount", (), [U], [(1,), (2,), (3,)], (6, 3)),
    ("uniq", "uniq", (), [U], [(1,), (2,), (2,), (3,)], 3),
    ("uniqExact", "uniqExact", (), [U], [(1,), (2,), (2,), (3,)], 3),
    ("groupArray", "groupArray", (), [U], [(1,), (2,), (2,)], [1, 2, 2]),
    ("groupUniqArray_sorted", "groupUniqArray", (), [U], [(3,), (1,), (3,), (2,)], [1, 2, 3]),
    ("groupArrayLast", "groupArrayLast", (2,), [U], [(1,), (2,), (3,), (4,)], [3, 4]),
    ("groupArraySample_takes_first_n", "groupArraySample", (2,), [U],
     [(1,), (2,), (3,), (4,)], [1, 2]),
    ("topK", "topK", (2,), [U], [(1,), (1,), (2,), (3,), (1,)], [1, 2]),
    ("argMax", "argMax", (), [STR, U], [("/a", 1), ("/b", 5), ("/c", 3)], "/b"),
    ("argMin", "argMin", (), [STR, U], [("/a", 3), ("/b", 1), ("/c", 2)], "/b"),
    ("quantile_interpolates", "quantile", (), [U], [(1,), (2,), (2,), (3,)], 2.0),
    ("quantileExact_no_interpolation", "quantileExact", (), [U], [(1,), (2,), (2,), (3,)], 2),
    ("quantiles_plural", "quantiles", (0.5, 0.9), [U],
     [(1,), (2,), (3,), (4,), (5,)], [3.0, 4.6]),
    ("median", "median", (), [U], [(1,), (2,), (3,), (4,)], 2.5),
    ("sumMap_sorted_by_key", "sumMap", (), [ARR_U64, ARR_U64],
     [([1, 2], [10, 20]), ([2, 3], [5, 5])], {1: 10, 2: 25, 3: 5}),
    ("entropy_all_distinct_of_four", "entropy", (), [U], [(1,), (2,), (3,), (4,)], 2.0),
    ("entropy_half_repeats", "entropy", (), [U], [(1,), (1,), (2,), (3,)], 1.5),
    ("varPop", "varPop", (), [U], [(1,), (2,), (3,), (4,)], 1.25),
    ("varSamp", "varSamp", (), [U], [(1,), (2,), (3,), (4,)], 5.0 / 3.0),
    ("stddevPop", "stddevPop", (), [U], [(1,), (2,), (3,), (4,)], math.sqrt(1.25)),
    ("corr_perfectly_linear", "corr", (), [F64, F64],
     [(1.0, 2.0), (2.0, 4.0), (3.0, 6.0)], 1.0),
    ("any_first_value", "any", (), [U], [(7,), (8,), (9,)], 7),
    ("anyLast_last_value", "anyLast", (), [U], [(7,), (8,), (9,)], 9),
]


@pytest.mark.parametrize(
    "name,fn,params,arg_types,rows,expected",
    _EXACT_CASES,
    ids=[case[0] for case in _EXACT_CASES],
)
def test_insert_result_into_exact_values(name, fn, params, arg_types, rows, expected):
    """逐字断言结果值 —— 这些是“教科书答案”,任何回归都会立刻打脸。"""
    assert _close(_run(fn, arg_types, rows, params), expected)


@pytest.mark.parametrize(
    "name,arg_types,lang",
    [
        ("sum", [U], "UInt64"),
        ("min", [U], "UInt64"),
        ("max", [U], "UInt64"),
        ("count", [], "UInt64"),
        ("avg", [U], "Float64"),
        ("uniq", [U], "UInt64"),
        ("groupArray", [U], "Array(UInt64)"),
        ("topK", [U], "Array(UInt64)"),
        ("sumCount", [U], "Tuple(UInt64, UInt64)"),
        ("argMax", [STR, U], "String"),
        ("quantile", [U], "Float64"),
    ],
)
def test_return_types(name, arg_types, lang):
    """``get_return_type`` 只依赖 ``arg_types``,不看数据 —— 结果类型必须稳定。"""
    assert _make(name, (), arg_types).get_return_type().get_name() == lang


# ══════════════════════════════════════════════════════════════════════════
# 5. 组合器
# ══════════════════════════════════════════════════════════════════════════


def test_if_combinator_filters_rows():
    """``sumIf(x, cond)`` 只累加条件为真的行 —— 等价于 ``sum(x) WHERE cond``。"""
    rows = [(1, 1), (2, 0), (3, 1), (4, 1)]
    assert _run("sumIf", [U, U8], rows) == 8


def test_countIf_counts_true_rows():
    """``countIf(cond)`` 数条件为真的行数。"""
    assert _run("countIf", [U8], [(1,), (0,), (1,), (1,), (0,)]) == 3


def test_if_without_condition_column_degrades_to_base():
    """``-If`` 允许省略条件列(教学实现的宽松处)—— 此时退化成基函数。"""
    assert _run("sumIf", [U], [(1,), (2,), (3,)]) == 6


def test_array_combinator_unpacks_arrays():
    """``-Array`` 把数组拍平后喂给基聚合,结果类型与基函数相同。"""
    f = _make("sumArray", (), [ARR_U64])
    assert _result(f, _feed(f, [([1, 2, 3],), ([4, 5],)])) == 15
    assert f.get_return_type().get_name() == "UInt64"

    f = _make("uniqArray", (), [ARR_U64])
    assert _result(f, _feed(f, [([1, 2],), ([2, 3],)])) == 3


def test_state_combinator_returns_state_type():
    """``-State`` 的结果类型是 ``AggregateFunction(...)``,而不是最终值类型。"""
    f = _make("sumState", (), [U])
    assert f.get_return_type().get_name() == "AggregateFunction(sum, UInt64)"
    assert f.is_state_combinator() is True

    # 结果列里放的是“状态对象”,不是数。
    state = _feed(f, [(1,), (2,), (3,)])
    column = f.get_return_type().create_column()
    f.insert_result_into(state, column)
    state_type = f.get_return_type()
    assert column.get(0) == [6]

    # 用 -Merge 消费这个状态 → 又得到 6。
    merge_fn = get_aggregate_function("sumMerge", [], [state_type])
    assert _result(merge_fn, _feed(merge_fn, [(column.get(0),)])) == 6


def test_merge_combinator_consumes_serialized_bytes_too():
    """``-Merge`` 既能吃“状态对象”,也能吃从磁盘读回的**裸字节**。"""
    state_type = _make("sumState", (), [U]).get_return_type()
    producer = _make("sumState", (), [U])
    blob = producer.serialize_state(_feed(producer, [(1,), (2,), (3,)]))

    merge_fn = get_aggregate_function("sumMerge", [], [state_type])
    assert isinstance(blob, (bytes, bytearray))
    assert _result(merge_fn, _feed(merge_fn, [(blob,)])) == 6


def test_state_merge_roundtrip_is_lossless():
    """``sumState`` → 序列化 → ``sumMerge`` 走完整条“物化视图”链路。"""
    producer = _make("sumState", (), [U])
    state_type = producer.get_return_type()
    state = _feed(producer, [(10,), (20,), (30,)])
    blob = producer.serialize_state(state)

    restored = producer.deserialize_state(blob)
    merge_fn = get_aggregate_function("sumMerge", [], [state_type])
    assert _result(merge_fn, _feed(merge_fn, [(restored,)])) == 60


def test_distinct_combinator_deduplicates_before_aggregating():
    """``-Distinct`` 只把**首次出现**的参数元组喂给基聚合一回。"""
    assert _run("sumDistinct", [U], [(1,), (1,), (2,), (3,), (3,)]) == 6
    assert _run("countDistinct", [U], [(1,), (1,), (2,), (3,), (3,)]) == 3


def test_distinct_state_roundtrip_and_merge():
    """``-Distinct`` 的状态是 ``{inner, seen}``,也要能往返与合并。"""
    f = _make("sumDistinct", (), [U])
    state = _feed(f, [(1,), (1,), (2,)])
    blob = f.serialize_state(state)
    restored = f.deserialize_state(blob)
    assert f.serialize_state(restored) == blob
    assert _result(f, restored) == 3

    # 两半的值**互不重叠**时,合并就是正确的(6 = 1+2+3)。
    left = _feed(f, [(1,), (2,)])
    right = _feed(f, [(3,), (4,)])
    f.merge(left, right)
    assert _result(f, left) == 10


def test_sql_multi_key_order_by_secondary_key_asc(ev):
    """多键 ORDER BY:主键降序、同值时**次级键升序**(曾经被主键的 DESC 带偏)。"""
    assert_rows(
        ev.execute("SELECT url, count() AS c FROM ev GROUP BY url ORDER BY c DESC, url"),
        [("/a", 3), ("/b", 1), ("/c", 1)],
    )


# ══════════════════════════════════════════════════════════════════════════
# 10. 回归:groupBitXor / -Distinct merge / sumMap 排序 / groupArrayInsertAt
# ══════════════════════════════════════════════════════════════════════════


def test_group_bit_xor_is_exclusive_or():
    """``groupBitXor`` 必须是**异或** —— 曾因 ``"or" in "groupbitxor"`` 被当成按位或。"""
    assert _run("groupBitXor", [U], [(5,), (3,)]) == 6  # 5 ^ 3
    assert _run("groupBitXor", [U], [(5,), (3,), (1,)]) == 7  # 5 ^ 3 ^ 1
    # 邻居不受影响,顺便钉住精确匹配:
    assert _run("groupBitOr", [U], [(5,), (3,)]) == 7  # 5 | 3
    assert _run("groupBitAnd", [U], [(5,), (3,)]) == 1  # 5 & 3


def test_distinct_merge_does_not_double_count_overlapping_shards():
    """``-Distinct`` 的 merge 必须按**去重后的集合**合并,而不是无条件累加内层状态。

    两个分片有重叠值时,旧实现会把重叠部分算两遍(分布式 / 两阶段聚合 /
    ``AggregatingMergeTree`` 下 ``sumDistinct`` 会偏大)。
    """
    f = _make("sumDistinct", (), [U])
    cols = [ValueColumn(c) for c in _cols([(1,), (2,), (2,), (3,)])]
    left = _feed_range(f, cols, 0, 2)  # {1, 2}
    right = _feed_range(f, cols, 0, 4)  # {1, 2, 3} —— 与 left 重叠
    f.merge(left, right)
    assert _result(f, left) == 6  # 1 + 2 + 3,而不是 9

    g = _make("avgDistinct", (), [U])
    cols = [ValueColumn(c) for c in _cols([(1,), (2,), (2,), (4,)])]
    left = _feed_range(g, cols, 0, 2)  # {1, 2}
    right = _feed_range(g, cols, 0, 4)  # {1, 2, 4}
    g.merge(left, right)
    assert math.isclose(_result(g, left), 7.0 / 3.0)  # 单次聚合也是 7/3

    # 互不重叠的分片仍然正确(护栏)
    h = _make("sumDistinct", (), [U])
    cols = [ValueColumn(c) for c in _cols([(1,), (2,), (3,), (4,)])]
    left = _feed_range(h, cols, 0, 2)
    right = _feed_range(h, cols, 2, 4)
    h.merge(left, right)
    assert _result(h, left) == 10


def test_sum_map_keys_sorted_numerically():
    """``sumMap`` 必须按 key **数值**升序 —— 旧实现用 ``repr`` 排序,``10`` 排在 ``2`` 前。"""
    got = _run("sumMap", [ARR_U64, ARR_U64], [([2, 10], [1, 1])])
    assert list(got.keys()) == [2, 10]
    assert got == {2: 1, 10: 1}


def test_group_array_insert_at_merge_matches_single_pass():
    """``groupArrayInsertAt`` 的 merge 必须与单次聚合一致(旧实现丢值 / 把默认值当空槽)。"""
    # 追加模式(不给 pos):分片应当依次拼接
    f = _make("groupArrayInsertAt", (0,), [U])
    assert _result(f, _feed(f, [(1,), (2,), (2,), (3,)])) == [1, 2, 2, 3]
    cols = [ValueColumn(c) for c in _cols([(1,), (2,), (2,), (3,)])]
    left = _feed_range(f, cols, 0, 2)
    right = _feed_range(f, cols, 2, 4)
    f.merge(left, right)
    assert _result(f, left) == [1, 2, 2, 3]

    # 带位置模式的**重叠位置**:后喂的行覆盖先喂的(与单次聚合一致)
    g = _make("groupArrayInsertAt", (0,), [U, U])
    assert _result(g, _feed(g, [(1, 0), (2, 0)])) == [2]
    l2 = _feed(g, [(1, 0)])
    r2 = _feed(g, [(2, 0)])
    g.merge(l2, r2)
    assert _result(g, l2) == [2]

    # 稀疏位置 + 默认值填充仍要正确
    h = _make("groupArrayInsertAt", (0,), [U, U])
    assert _result(h, _feed(h, [(9, 4)])) == [0, 0, 0, 0, 9]
