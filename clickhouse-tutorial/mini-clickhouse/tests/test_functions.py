"""标量函数库的测试 —— 覆盖注册表、**广谱调用扫描**,以及一批经典陷阱。

本文件只碰 ``minich.functions`` 这一层,不建表、不落盘:绝大多数断言
直接用 :func:`minich.functions.get_function` 拿到一个 ``FunctionBase``,
再用 :meth:`FunctionBase.execute` 对着"手工搭出来的列"求值 —— 这正是
引擎内部调用函数的真实方式。注意 :class:`minich.block.ColumnWithTypeAndName`
的语义:一列 = **名字 + 类型 + 数据**;函数拿到的是"列",不是"值",
这是 ClickHouse 函数系统与行式数据库最根本的区别。

分四部分:

1. **注册表与 API** —— ``get_function`` / ``function_exists`` /
   ``registered_functions`` / 函数名大小写不敏感 / 未知函数报 78。
2. **广谱扫描** —— 对 ``registered_functions()`` 里**每一个**带非空说明的
   函数(含别名),拿一组"看起来合理"的实参去调它;第一组没抛异常的实参
   用来断言两件事:

   * 结果列行数 == 请求行数 ``ROWS``;
   * 结果列的**具体类**与声明返回类型 ``get_return_type(...).create_column()``
     的类一致(即"声明的类型"与"实际物化出来的列"不脱节)。

   挑不出合适实参的(需要 lambda、字典、或函数参数 ``params``)优雅跳过。
   ``sleep`` / ``sleepEachRow`` 被**刻意排除**:它们真的会睡,会把测试从
   毫秒级拖成秒级 —— 这本身就是"扫描型测试要防的坑"。

3. **经典陷阱** —— ``length`` vs ``lengthUTF8``、``substring`` 的 1 基与
   负偏移、三值逻辑、``if`` 的惰性、``arrayMap`` 的 lambda、``MD5`` 的原始
   字节、``reinterpretAsString`` 的小端、``arrayElement`` 越界取默认值。
4. **已记录的偏离** —— 用 ``xfail()`` 钉住两处"**文档写错了**":
   ``hashing.py`` 声称 ``hex(MD5(''))`` 是小写,``higher_order.py`` 声称
   ``arrayMap((x,y) -> x+y, [1,2,3], [10])`` 得到 ``[11,12,13]``;实测都不是。
   两处的**实际行为都与真实 ClickHouse 一致**,错的是文档里的例子。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from minich.block import ColumnWithTypeAndName
from minich.errors import ErrorCode, MiniChError
from minich.functions import (
    function_exists,
    get_function,
    registered_functions,
)
# ``function_aliases`` 只在 ``base`` 里定义,``functions/__init__`` 没有把它
# 转出去(它的 ``__all__`` 里没有这个名字)—— 所以按真实路径导入。
from minich.functions.base import (
    LambdaArgument,
    execute_with_null_propagation,
    function_aliases,
)
from minich.types import get_data_type


# ══════════════════════════════════════════════════════════════════════════
# 0. 广谱扫描的"实参弹药库"
# ══════════════════════════════════════════════════════════════════════════

#: 扫描时请求的行数 —— 每个候选实参都必须构造出这么多行。
ROWS = 3

#: ``(类型名, 三个值)`` —— 单参数函数的候选,也是多参数组合的积木。
#: 类型覆盖了数值族(含 Int/UInt 与 Float)、字符串、定长串、日期时间、
#: 数组、UUID、IPv4、元组、Bool、Map —— 尽量把不同"重载口味"都喂到。
_SINGLES: List[Tuple[str, List[Any]]] = [
    ("UInt8", [1, 2, 3]),
    ("UInt16", [2, 3, 4]),
    ("UInt32", [5, 6, 7]),
    ("UInt64", [7, 8, 9]),
    ("Int8", [-1, 2, -3]),
    ("Int16", [-2, 3, -4]),
    ("Int32", [-3, 4, -5]),
    ("Int64", [-4, 5, -6]),
    ("Float32", [1.5, 2.5, -3.5]),
    ("Float64", [2.5, -1.5, 0.5]),
    ("String", ["abc", "hello", "xy"]),
    ("FixedString(16)", ["0123456789abcdef"] * 3),
    ("Date", [19723, 19724, 19725]),
    ("DateTime", [1704067200, 1704067201, 1704067202]),
    ("Array(UInt8)", [[1, 2, 3], [4, 5], [6]]),
    ("Array(Int32)", [[1, 2, 3], [4, 5], [6]]),
    ("Array(String)", [["a", "b"], ["c"], ["d", "e", "f"]]),
    ("UUID", [0x550E8400E29B41D4A716446655440000, 0, 1]),
    ("IPv4", [0xC0A80001, 0, 0x01020304]),
    ("Tuple(UInt8, UInt8)", [(1, 2), (3, 4), (5, 6)]),
    ("Bool", [True, False, True]),
    ("Map(String, UInt8)", [{"a": 1}, {"b": 2}, {"c": 3}]),
]

#: 有些函数要"特定的字面量"才肯干活(日期差要 ``'day'`` 这种合法单位、时区
#: 要 ``'UTC'``、``inet_aton`` 要一个 IPv4 字面量)。它们作为**完整组合**
#: 追加在通用候选之后 —— 只有通用候选全失败时才会用到,所以不会影响已经能
#: 调通的函数。
_EXTRA_SPECS: List[List[Tuple[str, List[Any]]]] = [
    # toTimezone(DateTime, String)
    [("DateTime", [1704067200, 1704067201, 1704067202]),
     ("String", ["UTC", "UTC", "UTC"])],
    # normalizeUTF8('NFD', s)
    [("String", ["NFD", "NFC", "NFD"]), ("String", ["e", "a", "o"])],
    # inet_aton / IPv4StringToNum —— 需要一个像样的点分十进制
    [("String", ["1.2.3.4", "0.0.0.0", "255.255.255.255"])],
    # dateDiff / age('day', Date, Date)
    [("String", ["day", "hour", "year"]),
     ("Date", [19723, 19724, 19725]),
     ("Date", [19724, 19725, 19726])],
]


def _type_map() -> Dict[str, Tuple[str, List[Any]]]:
    """``类型名 -> (类型名, 值)`` —— 组合候选时按名取积木。"""
    return {name: (name, values) for name, values in _SINGLES}


def _build_candidates() -> List[List[Tuple[str, List[Any]]]]:
    """把 ``_SINGLES`` 拼成 arity 0~4 的候选组合,再附上 ``_EXTRA_SPECS``。

    这不是"穷举所有签名",而是"常见风格各来一套":同类型自配对
    (``plus(UInt8,UInt8)``)、几个经典的异类型配对(``String``+``UInt8``、
    ``Array``+``String``、``Date``+``Int``),以及几个三元组。
    """
    m = _type_map()
    out: List[List[Tuple[str, List[Any]]]] = [[]]  # arity 0

    # arity 1:每个单类型都试一遍
    out += [[s] for s in _SINGLES]

    # arity 2:同类型自配对 + 若干异类型配对
    out += [[s, s] for s in _SINGLES]
    for a, b in [
        ("UInt8", "String"), ("String", "UInt8"), ("Array(UInt8)", "String"),
        ("String", "Array(UInt8)"), ("UInt32", "UInt32"), ("Float64", "Float64"),
        ("DateTime", "UInt8"), ("Array(UInt8)", "Array(UInt8)"),
        ("String", "Int32"), ("UInt8", "DateTime"), ("Date", "UInt8"),
        ("Date", "Int32"), ("Array(String)", "String"),
        ("Tuple(UInt8, UInt8)", "UInt8"), ("Map(String, UInt8)", "String"),
        ("Array(UInt8)", "UInt8"),
    ]:
        out.append([m[a], m[b]])

    # arity 3:几个经典三元组
    for a, b, c in [
        ("UInt8", "UInt8", "UInt8"), ("String", "String", "String"),
        ("String", "UInt8", "UInt8"), ("UInt8", "String", "String"),
        ("Array(UInt8)", "Array(UInt8)", "Array(UInt8)"),
        ("Array(String)", "String", "String"), ("String", "String", "UInt8"),
        ("UInt32", "UInt32", "UInt32"), ("DateTime", "UInt8", "UInt8"),
        ("Array(UInt8)", "UInt8", "UInt8"), ("Int32", "Int32", "Int32"),
        ("String", "Date", "Date"),
    ]:
        out.append([m[a], m[b], m[c]])

    # arity 4
    for quad in [
        ("UInt8", "UInt8", "UInt8", "UInt8"),
        ("String", "String", "String", "String"),
        ("DateTime", "UInt8", "UInt8", "UInt8"),
    ]:
        out.append([m[q] for q in quad])

    out += _EXTRA_SPECS
    return out


_ALL_CANDIDATES = _build_candidates()


def _columns_for(spec: Sequence[Tuple[str, List[Any]]]):
    """把一份候选实参规格变成 ``(类型列表, ColumnWithTypeAndName 列表)``。

    每列都按 ``类型.create_column()`` 造好,再把这个类型的三个值 ``append``
    进去 —— 与引擎里"先定类型、再灌数据"的顺序一致。
    """
    types = []
    cols = []
    for i, (tname, values) in enumerate(spec):
        t = get_data_type(tname)
        col = t.create_column()
        for v in values:
            col.append(v)
        types.append(t)
        cols.append(ColumnWithTypeAndName(f"c{i}", t, col))
    return types, cols


#: 会真的阻塞的函数 —— 扫描必须跳过,否则一次扫描就要多花好几秒。
_SWEEP_EXCLUDE = frozenset({"sleep", "sleepeachrow"})

#: 所有带非空说明的函数名(含别名)—— 扫描的题目集合。
_FUNC_NAMES: List[str] = [
    name for name, _cat, desc in registered_functions() if desc
]


def _sweep(name: str):
    """对一个函数尝试所有候选实参,返回第一个"能跑通"的 ``(列, 返回类型)``。

    一个候选算"跑通",要求:构造类型、取返回类型、执行三步都不抛异常。
    找不到就返回 ``None``(调用方据此 ``skip``)。

    .. note::

       "合理实参"其实很宽:很多函数会因为类型不对而抛 ``MiniChError``,
       有的会因为值不合法抛 ``ValueError``/``OverflowError``,还有的会
       在构造阶段就 ``IndexError``(参数个数不够)。这些**都只说明"这个
       候选不合适"**,所以用一个宽的 ``except Exception`` 跳到下一个候选。
       真正要断言的,是"跑通之后结果对不对"。
    """
    if name in _SWEEP_EXCLUDE:
        return None
    for spec in _ALL_CANDIDATES:
        try:
            types, cols = _columns_for(spec)
            fn = get_function(name, [], types)
            ret = fn.get_return_type(types)
            col = fn.execute(cols, ROWS)
        except Exception:
            continue
        return col, ret
    return None


# ══════════════════════════════════════════════════════════════════════════
# 1. 注册表与 API
# ══════════════════════════════════════════════════════════════════════════


def test_registered_functions_are_sorted_and_unique():
    """``registered_functions()`` 必须按名字有序 —— ``system.functions`` 靠它稳定输出。"""
    names = [n for n, _c, _d in registered_functions()]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_at_least_600_functions_have_a_description():
    """本快照里带说明的函数应有 600 个以上 —— 扫描的覆盖面取决于这个数。

    (名字里既有"正名"也有"别名";``registered_functions`` 两者都返回。)
    """
    assert len(_FUNC_NAMES) >= 600
    for _name, _cat, desc in registered_functions():
        assert desc


def test_unknown_function_raises_code_78():
    """未知函数报 ``UNKNOWN_FUNCTION``(78)—— 与真实 ClickHouse 的错误码一致。"""
    assert not function_exists("definitely_not_a_function")
    with pytest.raises(MiniChError) as ei:
        get_function("definitely_not_a_function")
    assert ei.value.code == ErrorCode.UNKNOWN_FUNCTION


def test_function_lookup_is_case_insensitive():
    """注册表键统一小写,所以 ``PLUS`` / ``Plus`` / ``plus`` 都能解析。"""
    assert function_exists("PLUS")
    assert get_function("PLUS").name == "PLUS"  # 实例上保留调用时的原名
    u8 = get_data_type("UInt8")
    for spelling in ("plus", "PLUS", "Plus"):
        assert get_function(spelling).get_return_type([u8, u8]).get_name() == "UInt16"


def test_function_aliases_map_alias_to_target():
    """``function_aliases()`` 是 ``别名 -> 目标名`` 的字典(两者都已小写)。"""
    aliases = function_aliases()
    assert aliases.get("add") == "plus"
    assert aliases.get("array_map") == "arraymap"
    # 每个别名的目标确实是个能取到的函数
    for alias, target in aliases.items():
        assert function_exists(alias)
        assert function_exists(target)


# ══════════════════════════════════════════════════════════════════════════
# 2. 广谱扫描 —— 每个带说明的函数各一条测试
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("name", _FUNC_NAMES, ids=lambda n: n)
def test_function_sweep(name):
    """能调通就断言"行数 + 列类";调不通(需要 lambda/字典/params)则跳过。

    断言的两条正是"函数没写坏"的最低要求:

    * 行数:函数返回的列必须和请求的行数对齐(否则下游 ``Block`` 会崩);
    * 列类:结果列的**具体类**要和声明类型造出来的列同类 —— 这条能抓住
      "声明返回 ``String`` 却返回了数值列"这类静默错误。
    """
    got = _sweep(name)
    if got is None:
        pytest.skip(f"{name}: 找不到简单实参(多半需要 lambda / 字典 / params)")
    col, ret = got
    assert col.size() == ROWS, f"{name}: 返回 {col.size()} 行,期望 {ROWS} 行"
    expected_cls = type(ret.create_column())
    assert isinstance(col, expected_cls), (
        f"{name}: 声明返回 {ret.get_name()}({expected_cls.__name__}),"
        f"实际列却是 {type(col).__name__}"
    )


def test_sweep_coverage_is_broad():
    """整轮扫描至少能调通 540 个函数 —— 这是"广泛覆盖"的硬指标。

    实测约 577/654。低于这个数通常意味着注册表或某个函数实现被改坏了。
    """
    callable_names = [n for n in _FUNC_NAMES if _sweep(n) is not None]
    assert len(callable_names) >= 540, (
        f"只有 {len(callable_names)} 个函数能调通,低于 540"
    )


# ══════════════════════════════════════════════════════════════════════════
# 3. 经典陷阱
# ══════════════════════════════════════════════════════════════════════════


def _scalar(ctx, sql: str) -> Any:
    """跑一条 ``SELECT`` 取单值 —— 陷阱测试里最顺手的写法。"""
    return ctx.execute(sql).scalar()


# ── 3.1 length vs lengthUTF8 ─────────────────────────────────────────────


def test_length_counts_bytes_not_characters(memory_ctx):
    """``length`` 数的是**字节**,``lengthUTF8`` 数的是**码点**。

    ``String`` 是二进制安全的,内部就是字节串,所以 ``'你好'`` 是 6 个字节
    而只有 2 个字符 —— 从 MySQL/Python 迁过来最容易踩的一条。
    """
    assert _scalar(memory_ctx, "SELECT length('你好')") == 6
    assert _scalar(memory_ctx, "SELECT lengthUTF8('你好')") == 2


def test_length_and_lengthutf8_return_uint64_and_agree_on_ascii():
    """两者都返回 ``UInt64``;对纯 ASCII 输入,字节数 == 字符数。"""
    string_type = get_data_type("String")
    col = string_type.create_column()
    for s in ("abc", "hello", "xy"):
        col.append(s)

    for fname in ("length", "lengthUTF8"):
        fn = get_function(fname, [], [string_type])
        assert fn.get_return_type([string_type]).get_name() == "UInt64"
        out = fn.execute([ColumnWithTypeAndName("s", string_type, col)], ROWS)
        assert [out.get(i) for i in range(ROWS)] == [3, 5, 2]


# ── 3.2 substring 的 1 基与负偏移 ────────────────────────────────────────


def test_substring_is_one_based_and_clips(memory_ctx):
    """``substring(s, offset, length)`` 的 offset 是 **1 基**;长超出会被裁剪。"""
    assert _scalar(memory_ctx, "SELECT substring('hello', 2, 3)") == "ell"
    # offset 3 起取 100 个 —— 只剩 3 个,裁剪成 'llo'
    assert _scalar(memory_ctx, "SELECT substring('hello', 3, 100)") == "llo"


def test_substring_negative_offset_counts_from_end(memory_ctx):
    """**负 offset 从末尾数**:``-2`` 指的是倒数第 2 个字符。"""
    assert _scalar(memory_ctx, "SELECT substring('hello', -2, 2)") == "lo"


def test_substring_negative_or_zero_length_is_empty(memory_ctx):
    """长度 <= 0 一律得到空串(不是报错,也不是"取到末尾")。"""
    assert _scalar(memory_ctx, "SELECT substring('hello', 2, -1)") == ""
    assert _scalar(memory_ctx, "SELECT substring('hello', 1, 0)") == ""


def test_substring_direct_call_matches_engine():
    """直接走 ``FunctionBase`` 的结果与 SQL 引擎一致 —— 两条入口同语义。"""
    string_type = get_data_type("String")
    u8 = get_data_type("UInt8")
    s = string_type.create_column()
    s.append("hello")
    off = u8.create_column()
    off.append(2)
    ln = u8.create_column()
    ln.append(3)
    fn = get_function("substring", [], [string_type, u8, u8])
    out = fn.execute(
        [ColumnWithTypeAndName("s", string_type, s),
         ColumnWithTypeAndName("o", u8, off),
         ColumnWithTypeAndName("l", u8, ln)],
        1,
    )
    assert out.get(0) == "ell"


# ── 3.3 三值逻辑 and / or / not ──────────────────────────────────────────


@pytest.mark.parametrize(
    "sql,expected",
    [
        # NULL AND 0 = 0(已能定局),NULL AND 1 = NULL(无法定局)
        ("SELECT and(NULL, 0)", 0),
        ("SELECT and(NULL, 1)", None),
        # NULL OR 1 = 1,NULL OR 0 = NULL —— 与 and 对偶
        ("SELECT or(NULL, 1)", 1),
        ("SELECT or(NULL, 0)", None),
        # NOT NULL 仍是 NULL
        ("SELECT not(NULL)", None),
        # 非 NULL 时就是普通布尔
        ("SELECT and(1, 1)", 1),
        ("SELECT or(0, 0)", 0),
    ],
)
def test_three_valued_logic(memory_ctx, sql, expected):
    """三值逻辑:能靠一侧定局就定局,定不了才给 NULL。

    这几条是 ``and``/``or``/``not`` **必须**自己实现 NULL 逻辑的原因
    (它们声明 ``propagate_nulls = False``):朴素的"任一参数 NULL 就返回
    NULL"会把 ``NULL AND 0`` 错算成 NULL,而正确答案是 ``0``。
    """
    assert _scalar(memory_ctx, sql) == expected


# ── 3.4 if / multiIf 的惰性 ──────────────────────────────────────────────


def test_if_is_lazy_only_evaluates_taken_branch(memory_ctx):
    """``if`` 只求值**被选中**的分支 —— 没被选中的那支哪怕会报错也不该报。

    这里两支里都埋了 ``1 / 0``:如果引擎把两支都算出来,一定会抛除零错误。
    能拿到 42,说明真的是惰性求值。
    """
    assert _scalar(memory_ctx, "SELECT if(1, 42, 1 / 0)") == 42
    assert _scalar(memory_ctx, "SELECT if(0, 1 / 0, 42)") == 42


def test_multiif_skips_failed_conditions(memory_ctx):
    """``multiIf(NULL, 1, 2)``:NULL 条件当假,落到默认分支 2。"""
    assert _scalar(memory_ctx, "SELECT multiIf(NULL, 1, 2)") == 2


# ── 3.5 arrayMap 的 lambda ───────────────────────────────────────────────


def test_arraymap_single_array(memory_ctx):
    """``arrayMap(x -> x*2, [1,2,3])`` 逐元素映射。"""
    assert _scalar(memory_ctx, "SELECT arrayMap(x -> x * 2, [1, 2, 3])") == [2, 4, 6]


def test_arraymap_multiple_arrays_pads_shorter_with_default(memory_ctx):
    """多数组形态:短数组按**元素默认值**补齐到最长。

    ``[10]`` 补成 ``[10, 0, 0]``,所以 ``[1,2,3] + [10,0,0] = [11,2,3]``
    —— 而不是不少人以为的 ``[11,12,13]``。这条与真实 ClickHouse 一致。
    """
    assert _scalar(
        memory_ctx, "SELECT arrayMap((x, y) -> x + y, [1, 2, 3], [10])"
    ) == [11, 2, 3]


def test_arraymap_via_lambda_argument_object():
    """直接构造 :class:`LambdaArgument` 调用 ``arrayMap``。

    高阶函数的第一个实参**不是列**,而是一段程序:求值器把它包成
    ``LambdaArgument(names, body)``,函数实现再对每行的元素列调
    ``body(x=<元素列>)``。这里手工搭一个 body 来验证这条契约。
    """
    arr_type = get_data_type("Array(UInt8)")
    arr = arr_type.create_column()
    for v in ([1, 2, 3], [4, 5], [6]):
        arr.append(v)

    uint8 = get_data_type("UInt8")

    def body_double(x):
        """``x`` 是一列元素;返回"每个元素乘 2"的一列。"""
        out = uint8.create_column()
        for v in x.to_list():
            out.append(v * 2)
        return out

    lam = LambdaArgument(["x"], body_double)
    fn = get_function("arrayMap", [], [arr_type])
    assert fn.get_return_type([arr_type]).get_name() == "Array(UInt8)"
    out = fn.execute([lam, ColumnWithTypeAndName("a", arr_type, arr)], ROWS)
    assert [out.get(i) for i in range(ROWS)] == [[2, 4, 6], [8, 10], [12]]


# ── 3.6 MD5 的原始字节 ───────────────────────────────────────────────────


def test_md5_returns_raw_fixedstring_16(memory_ctx):
    """``MD5`` 返回 ``FixedString(16)`` 的**原始摘要字节**,不是 32 位十六进制串。

    证据有三条:类型名是 ``FixedString(16)``、``length`` 是 16(而不是 32),
    以及最硬的 —— 把取回的字符串按 surrogateescape 还原成字节后,与
    :func:`hashlib.md5` 的 ``digest()`` **逐字节相等**。
    """
    assert _scalar(memory_ctx, "SELECT toTypeName(MD5(''))") == "FixedString(16)"
    assert _scalar(memory_ctx, "SELECT length(MD5(''))") == 16

    string_type = get_data_type("String")
    col = string_type.create_column()
    col.append("")
    fn = get_function("md5", [], [string_type])
    # MD5('') 的 16 字节在 Python str 里是"utf-8 + surrogateescape"解码的结果,
    # 所以 len() 未必是 16;真正可靠的是再编码回字节。
    raw = fn.execute([ColumnWithTypeAndName("s", string_type, col)], 1).get(0)
    assert raw.encode("utf-8", "surrogateescape") == hashlib.md5(b"").digest()


def test_hex_of_md5_is_uppercase_hex_of_raw_digest(memory_ctx):
    """``hex(MD5(''))`` 就是原始摘要的**大写**十六进制。

    ``hex`` 与真实 ClickHouse 一致,输出 ``FF`` 而不是 ``ff``。
    (``hashing.py`` 文档里的例子写成了小写,是一条已记录的文档偏离,见第 4 节。)
    """
    got = _scalar(memory_ctx, "SELECT hex(MD5(''))")
    assert got == hashlib.md5(b"").hexdigest().upper()
    assert got == "D41D8CD98F00B204E9800998ECF8427E"


def test_hex_is_uppercase_overall(memory_ctx):
    """``hex`` 一律大写 —— ``hex(toUInt8(255))`` 给的是 ``'FF'``。"""
    assert _scalar(memory_ctx, "SELECT hex(toUInt8(255))") == "FF"
    assert _scalar(memory_ctx, "SELECT hex('z')") == "7A"


# ── 3.7 reinterpretAsString 的小端 ──────────────────────────────────────


def test_reinterpret_as_string_is_little_endian_bytes(memory_ctx):
    """``reinterpretAsString`` 把值按**内存字节**原样解释 —— x86 是小端。

    ``toUInt32(1)`` 的内存是 ``01 00 00 00``;``256`` 则是 ``00 01 00 00``。
    """
    assert _scalar(
        memory_ctx, "SELECT reinterpretAsString(toUInt32(1))"
    ) == "\x01\x00\x00\x00"
    assert _scalar(
        memory_ctx, "SELECT reinterpretAsString(toUInt32(256))"
    ) == "\x00\x01\x00\x00"

    u32 = get_data_type("UInt32")
    col = u32.create_column()
    col.append(1)
    fn = get_function("reinterpretAsString", [], [u32])
    assert fn.execute(
        [ColumnWithTypeAndName("x", u32, col)], 1
    ).get(0) == "\x01\x00\x00\x00"


# ── 3.8 arrayElement 的 1 基与越界 ──────────────────────────────────────


@pytest.mark.parametrize(
    "index,expected",
    [(1, 10), (2, 20), (3, 30)],
)
def test_arrayelement_is_one_based(memory_ctx, index, expected):
    """``arrayElement`` 的索引是 **1 基** —— 与多数语言的下标习惯不同。"""
    assert _scalar(
        memory_ctx, f"SELECT arrayElement([10, 20, 30], {index})"
    ) == expected


def test_arrayelement_out_of_range_returns_element_default(memory_ctx):
    """越界**不报错**,返回元素类型的默认值。

    索引 ``0`` 与超上界的 ``5`` 都给 ``UInt8`` 的默认值 ``0``;若元素是
    ``String`` 则给空串 —— 关键是"给默认值"而不是抛异常。
    """
    assert _scalar(memory_ctx, "SELECT arrayElement([10, 20, 30], 0)") == 0
    assert _scalar(memory_ctx, "SELECT arrayElement([10, 20, 30], 5)") == 0
    assert _scalar(memory_ctx, "SELECT arrayElement(['a', 'b'], 5)") == ""
    # 默认值的类型就是元素类型
    assert _scalar(memory_ctx, "SELECT toTypeName(arrayElement([1, 2, 3], 0))") == "UInt8"
    assert _scalar(memory_ctx, "SELECT toTypeName(arrayElement(['a', 'b'], 0))") == "String"

    # 直接调函数走一遍,确认返回值确实是"元素类型默认值"
    arr_type = get_data_type("Array(UInt8)")
    arr = arr_type.create_column()
    arr.append([10, 20, 30])
    idx_type = get_data_type("UInt8")
    idx = idx_type.create_column()
    idx.append(5)
    fn = get_function("arrayElement", [], [arr_type, idx_type])
    out = fn.execute(
        [ColumnWithTypeAndName("a", arr_type, arr),
         ColumnWithTypeAndName("i", idx_type, idx)],
        1,
    )
    assert out.get(0) == get_data_type("UInt8").default_value() == 0


def test_arrayelement_negative_index_counts_from_end(memory_ctx):
    """负索引从末尾数:``-1`` 是最后一个元素(与 ``substring`` 的负偏移同思路)。"""
    assert _scalar(memory_ctx, "SELECT arrayElement([10, 20, 30], -1)") == 30


# ── 3.9 NULL 传播的统一入口 ─────────────────────────────────────────────


def test_execute_with_null_propagation_masks_nulls():
    """``execute_with_null_propagation`` 是引擎调函数的**统一入口**。

    任一实参是 ``Nullable`` 时,结果类型抬成 ``Nullable``,空位图取各实参
    空位图的或;``plus`` 的实现里因此**看不到 NULL 的影子**。
    """
    nullable_u8 = get_data_type("Nullable(UInt8)")
    u8 = get_data_type("UInt8")

    a = nullable_u8.create_column()
    a.append(None)
    a.append(5)
    b = u8.create_column()
    b.append(1)
    b.append(1)

    fn = get_function("plus", [], [nullable_u8, u8])
    col, ret_type = execute_with_null_propagation(
        fn,
        [ColumnWithTypeAndName("a", nullable_u8, a),
         ColumnWithTypeAndName("b", u8, b)],
        2,
    )
    assert ret_type.get_name() == "Nullable(UInt16)"   # UInt8 + UInt8 -> UInt16
    assert col.get(0) is None                          # NULL + 1 = NULL
    assert col.get(1) == 6                             # 5 + 1


# ══════════════════════════════════════════════════════════════════════════
# 4. 已记录的偏离 —— 文档与实现的出入
# ══════════════════════════════════════════════════════════════════════════
#
# 下面两条钉住的是**文档写错了**(不是实现错了):实现的行为与真实
# ClickHouse 一致,而库里的 docstring 例子对不上实际输出。用 strict xfail
# 固定下来,免得哪天样例被悄悄改动、或者有人照着文档去断言错的值。


@pytest.mark.xfail(
    raises=AssertionError,
    reason="文档缺陷:minich/functions/hashing.py 的 docstring 声称 "
           "hex(MD5('')) == 'd41d8cd98f00b204e9800998ecf8427e'(小写);"
           "实测是大写 'D41D8CD98F00B204E9800998ECF8427E'。"
           "hex 返回大写与真实 ClickHouse 一致,错的是文档里的例子。",
)
def test_hashing_docstring_hex_md5_lowercase_is_wrong(memory_ctx):
    """按 ``hashing.py`` 文档,``hex(MD5(''))`` 应是小写;实测是大写。"""
    assert _scalar(memory_ctx, "SELECT hex(MD5(''))") == "d41d8cd98f00b204e9800998ecf8427e"


@pytest.mark.xfail(
    raises=AssertionError,
    reason="文档缺陷:minich/functions/higher_order.py 的 _padded docstring "
           "声称 arrayMap((x,y)->x+y,[1,2,3],[10]) == [11,12,13];"
           "实测短数组按元素默认值(0)补齐,得到 [11,2,3] —— 后者才是真实 "
           "ClickHouse 的行为。",
)
def test_higher_order_docstring_padding_example_is_wrong(memory_ctx):
    """按 ``higher_order.py`` 文档,短数组补齐后应得 ``[11,12,13]``;实测 ``[11,2,3]``。"""
    assert _scalar(
        memory_ctx, "SELECT arrayMap((x, y) -> x + y, [1, 2, 3], [10])"
    ) == [11, 12, 13]
