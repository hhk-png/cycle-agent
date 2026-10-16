"""``FORMAT`` 子系统的测试 —— 往返、自描述、以及**已记录的偏离**。

格式层的验收标准只有一句话:**同一个 Block 写出去再读回来必须逐值相等**
(见 ``minich/formats/base.py`` 的模块文档)。所以本文件的主干是"一种格式一个
酷刑块(torture block)"的往返测试,块里塞满了最容易在转义/秒懂边界上翻车的值:

* ``NULL`` 与空串(必须分得开);
* 字符串里的制表符 / 换行 / 反斜杠 / 单双引号 / CJK / emoji;
* 浮点的 ``nan`` / ``inf`` / ``-inf`` / ``-0.0``(``==`` 覆盖不到的三个角落);
* 整数极值(``UInt8`` 顶、``Int64`` 底、``UInt64`` 顶);
* 空数组与嵌套数组、Tuple、Map;
* ``Date`` / ``DateTime``(带时区)/ ``DateTime64`` / ``Decimal`` / ``Enum`` /
  ``UUID`` / ``FixedString`` / ``IPv4`` / ``IPv6`` / ``LowCardinality(String)``。

第二类测试固定**文档里写过的取舍**:CSV 用 ``\\N`` 哨兵区分 NULL 与空串、
JSON 默认把 64 位整数写成带引号的字符串(以及怎么关掉它)、只能输出的格式
(``Pretty`` 等)不允许被读。

第三类测试用 ``xfail()`` 记下**实现里真有的缺陷**(不是测试写错):
这些点在往返时会丢信息或直接报错。按规矩,发现缺陷只记录、不修改 ——
这样文档与实现不会悄悄漂移。
"""

from __future__ import annotations

import io
import math

import pytest

from minich.block import Block, ColumnWithTypeAndName
from minich.errors import ErrorCode, MiniChError
from minich.formats import (
    block_from_columns,
    block_from_rows,
    format_exists,
    get_input_format,
    get_output_format,
    registered_formats,
)
from minich.types import get_data_type


# ══════════════════════════════════════════════════════════════════════════
# 常量:格式清单
# ══════════════════════════════════════════════════════════════════════════

#: 可往返的格式 —— 既能写又能读,``read(write(block)) == block`` 必须成立。
ROUNDTRIPPABLE_FORMATS = [
    "TSV", "TSVWithNames", "TSVWithNamesAndTypes",
    "CSV", "CSVWithNames",
    "Values",
    "JSONEachRow", "JSONStringsEachRow",
    "JSONCompactEachRow", "JSONCompactEachRowWithNames",
    "JSON", "JSONCompact",
    "RowBinary", "RowBinaryWithNames", "RowBinaryWithNamesAndTypes",
    "Native",
]

#: 只能输出的"人看"格式 —— ``get_input_format`` 必须抛 ``UNKNOWN_FORMAT``。
OUTPUT_ONLY_FORMATS = [
    "Pretty", "PrettyCompact", "PrettyCompactMonoBlock", "PrettySpace",
    "Vertical", "Markdown", "Null", "SQLInsert",
]

#: 自描述格式 —— 表头里有列名(+ 类型),可以用**空** sample_block 读。
SELF_DESCRIBING_FORMATS = [
    "TSVWithNamesAndTypes", "JSON", "JSONCompact",
    "RowBinaryWithNamesAndTypes", "Native",
]

#: 不自描述的格式 —— 光看字节无法知道类型,必须给非空 sample_block。
NEEDS_SCHEMA_FORMATS = ["TSV", "CSV", "Values", "JSONEachRow", "RowBinary"]


# ══════════════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════════════


def _header(pairs):
    """``[(名字, 类型名)]`` → 零行的 schema Block(即 Block 的 header)。"""
    return Block([ColumnWithTypeAndName(name, get_data_type(t)) for name, t in pairs])


def _emit(fmt_name, sample, rows, settings=None, out=None):
    """按"prefix → write → suffix"的时间线把一批行写成 ``bytearray``。

    这正是输出格式的调用顺序(见 ``IOutputFormat``):表头只能从零行的
    ``sample`` 拿,数据行则来自 ``write``。顺序错了(比如漏掉 ``write_prefix``)
    带流式状态的格式(``Values`` / ``JSON``)会算错分隔符。
    """
    fmt = get_output_format(fmt_name)
    if out is None:
        out = bytearray()
    fmt.write_prefix(sample, out, settings=settings)
    fmt.write(block_from_rows(sample, rows), out, settings=settings)
    fmt.write_suffix(sample, out, settings=settings)
    return out


def _flatten(blocks):
    """把 ``read`` 产出的 Block 流拉平成 ``[[值, …], …]``(按行)。"""
    out = []
    for block in blocks:
        for r in range(block.rows()):
            out.append([block.data[i].column.get(r) for i in range(block.columns())])
    return out


def _value_equal(a, b):
    """逐值比较 —— 额外处理 ``==`` 抓不到的 NaN 与 ``-0.0``。

    ``nan == nan`` 是 ``False``,而 ``-0.0 == 0.0`` 是 ``True`` —— 后者会
    把"符号丢了"这种真实的往返失败掩盖掉,所以这里显式比符号位。
    """
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        if a == 0.0 and b == 0.0:
            return math.copysign(1.0, a) == math.copysign(1.0, b)
        return a == b
    return a == b


def _assert_roundtrip(fmt_name, sample, rows, settings=None):
    """写出去再读回来,逐列断言 名字 / 类型 / 值 全部相等。"""
    data = _emit(fmt_name, sample, rows, settings=settings)
    blocks = list(get_input_format(fmt_name).read(bytes(data), sample, settings=settings))

    got = _flatten(blocks)
    # 期望值 = 原始 rows(经类型 normalize 后,与我们构造的 block 完全相同)
    blk = block_from_rows(sample, rows)
    expected = [[blk.data[i].column.get(r) for i in range(blk.columns())] for r in range(blk.rows())]

    assert len(got) == len(expected), (
        f"{fmt_name}: 读回 {len(got)} 行,期望 {len(expected)} 行"
    )

    # 列名与类型:读回的每一块都必须与 schema 一致
    for block in blocks:
        assert block.get_names() == sample.get_names(), f"{fmt_name}: 列名不一致"
        assert [c.type.get_name() for c in block.data] == [
            c.type.get_name() for c in sample.data
        ], f"{fmt_name}: 列类型不一致"

    for i, (g, w) in enumerate(zip(got, expected)):
        assert len(g) == len(w)
        for j in range(len(w)):
            assert _value_equal(w[j], g[j]), (
                f"{fmt_name}: 第 {i} 行第 {j} 列({sample.data[j].name} "
                f"{sample.data[j].type.get_name()})往返后不同: "
                f"{w[j]!r} -> {g[j]!r}"
            )


# ══════════════════════════════════════════════════════════════════════════
# 酷刑块 —— 尽可能多地塞进"容易翻车"的值
# ══════════════════════════════════════════════════════════════════════════

#: 酷刑块的 schema。顺序即列的存储顺序。
TORTURE_SCHEMA = [
    ("null_i64", "Nullable(Int64)"),
    ("str", "String"),
    ("empty_str_n", "Nullable(String)"),
    ("f64", "Float64"),
    ("u8", "UInt8"),
    ("i64", "Int64"),
    ("u64", "UInt64"),
    ("arr", "Array(UInt8)"),
    ("narr", "Array(Array(UInt8))"),
    ("tup", "Tuple(UInt8, String)"),
    ("mp", "Map(String, UInt32)"),
    ("d", "Date"),
    ("dt", "DateTime"),
    ("dtz", "DateTime('Asia/Shanghai')"),
    ("dt64", "DateTime64(3)"),
    ("dec", "Decimal(10, 2)"),
    ("en", "Enum8('a' = 1, 'b' = 2)"),
    ("uu", "UUID"),
    ("fs", "FixedString(6)"),
    ("ip4", "IPv4"),
    ("ip6", "IPv6"),
    ("lc", "LowCardinality(String)"),
]

#: 四行数据 —— 每一行都刻意覆盖不同的边界(见列上的注释)。
TORTURE_ROWS = [
    [None, "a\tb\nc\\d'e\"f", "", float("nan"), 0, -(2 ** 63), 2 ** 64 - 1,
     [], [[1], []], (1, "a"), {"k": 1}, 19723, 1704067200, 1704067200,
     1704067200123, 123456789, 1,
     0x550E8400E29B41D4A716446655440000, "ab", 0x01020304, "2001:db8::1", "GET"],
    [7, "你好🚀", None, float("inf"), 255, 2 ** 63 - 1, 0,
     [1, 2, 3], [], (2, ""), {}, 0, 0, 0,
     0, 0, 2, 0, "abcdef", 0, "::1", "POST"],
    [-(2 ** 63), "", "x", float("-inf"), 1, 0, 42,
     [255], [[1, 2], [3]], (0, "x y"), {"a": 1, "b": 2}, 1, 1, 1000000000,
     5, -5, 1, 17, "x", 0xFFFFFFFF, "::", ""],
    [0, "\\N", "", -0.0, 128, -1, 1,
     [0, 0], [[]], (255, "z"), {"x": 0}, 1200, 1704067201, 1,
     999, 50, 2, 2 ** 128 - 1, "", 0x7F000001, "fe80::1", "GET"],
]


# ══════════════════════════════════════════════════════════════════════════
# 1. 注册表
# ══════════════════════════════════════════════════════════════════════════


def test_registered_formats_is_sorted_with_flags():
    """``registered_formats()`` 返回 ``[(名字, 可读, 可写, 说明)]``,按名字排序。

    只列**正名** —— 别名(如 ``TabSeparated``)不单独成行,这与真实
    ClickHouse 的 ``system.formats`` 一致。
    """
    rows = registered_formats()
    names = [r[0] for r in rows]
    assert names == sorted(names)
    assert all(len(r) == 4 for r in rows)

    by_name = {r[0]: r for r in rows}
    for fmt in ROUNDTRIPPABLE_FORMATS:
        assert by_name[fmt][1] is True and by_name[fmt][2] is True, fmt
    for fmt in OUTPUT_ONLY_FORMATS:
        assert by_name[fmt][1] is False, f"{fmt} 不该可读"
        assert by_name[fmt][2] is True, f"{fmt} 该可写"

    # 别名不该作为独立条目出现在清单里
    assert "TabSeparated" not in names


def test_format_exists_handles_case_and_aliases():
    """格式名大小写不敏感,别名也认 —— ``FORMAT tsv`` 等价于 ``FORMAT TSV``。"""
    assert format_exists("TSV")
    assert format_exists("tsv")
    assert format_exists("TabSeparated")           # TSV 的别名
    assert format_exists("tabseparated")
    assert not format_exists("DefinitelyNotAFormat")
    # 取出来的实例带着**正名**
    assert get_output_format("tsv").name == "TSV"


def test_unknown_format_name_raises_unknown_format():
    with pytest.raises(MiniChError) as ei:
        get_output_format("NoSuchFormat")
    assert ei.value.code == ErrorCode.UNKNOWN_FORMAT


@pytest.mark.parametrize("fmt_name", OUTPUT_ONLY_FORMATS)
def test_output_only_formats_reject_input(fmt_name):
    """只能输出的格式(Pretty 等)调 ``get_input_format`` 必须抛 ``UNKNOWN_FORMAT``。

    这比"注册了却抛 NOT_IMPLEMENTED"更干净:能力缺失应当是**查得到**的。
    """
    with pytest.raises(MiniChError) as ei:
        get_input_format(fmt_name)
    assert ei.value.code == ErrorCode.UNKNOWN_FORMAT


# ══════════════════════════════════════════════════════════════════════════
# 2. 酷刑块往返 —— 16 种格式
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fmt_name", ROUNDTRIPPABLE_FORMATS)
def test_torture_block_roundtrip(fmt_name):
    """每种可往返格式都要能把酷刑块**逐值**读回来。

    这是格式层的核心验收标准,也解释了为什么 ClickHouse 默认用 TSV:
    转义规则一旦和解析规则不对齐,这里立刻会炸。
    """
    _assert_roundtrip(fmt_name, _header(TORTURE_SCHEMA), TORTURE_ROWS)


@pytest.mark.parametrize("fmt_name", ROUNDTRIPPABLE_FORMATS)
def test_multiple_blocks_stream_roundtrip(fmt_name):
    """``write`` 可以被调用多次(每个 Block 一次),拼接结果仍要能读回来。

    这一条专抓**流式状态**的 bug:``Values`` 要知道"上一行写过没"决定逗号、
    ``JSON`` 要数行、``Native`` 要在末尾写结束标记 —— 漏掉任何一处,
    多块场景就会露出破绽。
    """
    sample = _header([("a", "UInt8"), ("s", "String")])
    fmt = get_output_format(fmt_name)
    out = bytearray()
    fmt.write_prefix(sample, out)
    fmt.write(block_from_rows(sample, [[1, "x"], [2, "y"]]), out)
    fmt.write(block_from_rows(sample, [[3, "z"]]), out)
    fmt.write_suffix(sample, out)

    blocks = list(get_input_format(fmt_name).read(bytes(out), sample))
    assert _flatten(blocks) == [[1, "x"], [2, "y"], [3, "z"]]


# ══════════════════════════════════════════════════════════════════════════
# 3. 自描述 vs 需要 schema
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fmt_name", SELF_DESCRIBING_FORMATS)
def test_self_describing_formats_read_with_empty_sample(fmt_name):
    """自描述格式可以只靠文件本身读回 schema(列名 + 类型)。

    这是 ``TSVWithNamesAndTypes`` / ``JSON`` / ``Native`` 相比裸 ``TSV``
    多付的"表头字节"换来的能力:读取端不必提前知道表长什么样。
    """
    sample = _header([("a", "UInt8"), ("s", "String")])
    data = _emit(fmt_name, sample, [[1, "x"], [2, "y"]])

    blocks = list(get_input_format(fmt_name).read(bytes(data), Block()))
    assert _flatten(blocks) == [[1, "x"], [2, "y"]]
    # schema 是从文件里重建出来的
    assert [c.name for c in blocks[0].data] == ["a", "s"]
    assert [c.type.get_name() for c in blocks[0].data] == ["UInt8", "String"]


@pytest.mark.parametrize("fmt_name", NEEDS_SCHEMA_FORMATS)
def test_need_schema_formats_reject_empty_sample(fmt_name):
    """不自描述的格式必须拿到非空 sample_block,否则无从解析(报列数错误)。

    ``TSV`` 里的 ``1`` 到底是 ``UInt8`` 还是 ``String``?文件里没有任何线索 ——
    所以 schema 是**必需**的,缺了就只能明确报错,不能猜。
    """
    sample = _header([("a", "UInt8"), ("s", "String")])
    data = _emit(fmt_name, sample, [[1, "x"]])

    with pytest.raises(MiniChError) as ei:
        list(get_input_format(fmt_name).read(bytes(data), Block()))
    assert ei.value.code == ErrorCode.INCORRECT_NUMBER_OF_COLUMNS


# ══════════════════════════════════════════════════════════════════════════
# 4. 已记录的取舍:NULl 哨兵 / JSON 64 位整数
# ══════════════════════════════════════════════════════════════════════════


def test_csv_null_sentinel_and_escaped_backslash():
    """CSV 用 ``\\N`` 当 NULL 哨兵,并把值里**字面**的反斜杠转义成两个。

    这样三件事才分得开:NULL(``\\N``)、空串(``""``)、值恰好是 ``\\N``
    的字符串(``\\\\N``)。纯 RFC-4180 做不到后两者的区分。
    """
    sample = _header([("n", "Nullable(String)"), ("z", "String")])
    data = _emit("CSV", sample, [[None, "a"], ["\\N", "b"]])
    assert data.decode("utf-8") == "\\N,a\n" + "\\\\N,b\n"


def test_csv_null_roundtrip_preserves_three_states():
    """上面的哨兵必须能无损读回:NULL、空串、字面 ``\\N`` 三者互不串味。"""
    sample = _header([("n", "Nullable(String)"), ("e", "String")])
    rows = [[None, ""], ["\\N", "x"]]
    _assert_roundtrip("CSV", sample, rows)


def test_tsv_null_sentinel_roundtrip():
    """TSV 的 NULL 哨兵同样是 ``\\N`` —— NULL 与空串不会塌成一个字段。"""
    sample = _header([("n", "Nullable(Int64)"), ("s", "String")])
    rows = [[None, ""], [0, "0"]]
    _assert_roundtrip("TSV", sample, rows)


def test_non_nullable_column_rejects_null_sentinel():
    """非可空列读到 ``\\N`` 要**报错**,而不是把 NULL 静默塞进去。

    这与真实 ClickHouse 一致:NULL 进不了非可空列,早报错比后面算出零值好。
    """
    sample = _header([("a", "UInt8")])
    with pytest.raises(MiniChError) as ei:
        list(get_input_format("TSV").read(b"\\N\n", sample))
    assert ei.value.code == ErrorCode.CANNOT_PARSE_TEXT


JSON_64BIT_FORMATS = ["JSONEachRow", "JSONCompactEachRow", "JSON", "JSONCompact"]


@pytest.mark.parametrize("fmt_name", JSON_64BIT_FORMATS)
def test_json_quotes_64bit_integers_by_default(fmt_name):
    """默认 ``output_format_json_quote_64bit_integers=1``:``UInt64`` 写成带引号的字符串。

    原因很实际:JavaScript 的 Number 只有 53 位精度,裸的
    ``18446744073709551615`` 到了浏览器里会被改成 ``18446744073709552000``。
    所以 ClickHouse 默认加引号 —— 代价是 ``typeof`` 会变成 ``"string"``。
    """
    sample = _header([("u", "UInt64")])
    text = _emit(fmt_name, sample, [[2 ** 64 - 1]]).decode("utf-8")
    assert '"18446744073709551615"' in text


def test_json_eachrow_only_widens_64bit_integers():
    """加引号只影响 **位宽 > 32** 的整数;``UInt8`` 依然写成裸数字。"""
    sample = _header([("u", "UInt64"), ("i", "UInt8")])
    text = _emit("JSONEachRow", sample, [[2 ** 64 - 1, 5]]).decode("utf-8")
    assert text.strip() == '{"u":"18446744073709551615","i":5}'


@pytest.mark.parametrize("fmt_name", JSON_64BIT_FORMATS)
def test_json_can_turn_off_64bit_quoting(fmt_name):
    """传 ``output_format_json_quote_64bit_integers=0`` 就得到裸数字。"""
    sample = _header([("u", "UInt64")])
    settings = {"output_format_json_quote_64bit_integers": 0}
    text = _emit(fmt_name, sample, [[2 ** 64 - 1]], settings=settings).decode("utf-8")
    assert "18446744073709551615" in text
    assert '"18446744073709551615"' not in text


def test_json_reader_accepts_both_quoting_styles():
    """**读取端两种写法都认**:带引号的字符串会按目标类型解析回整数。"""
    sample = _header([("u", "UInt64"), ("i", "UInt8")])
    quoted = '{"u":"18446744073709551615","i":"5"}'
    bare = '{"u":18446744073709551615,"i":5}'
    for text in (quoted, bare):
        rows = _flatten(list(get_input_format("JSONEachRow").read(text, sample)))
        assert rows == [[2 ** 64 - 1, 5]]


# ══════════════════════════════════════════════════════════════════════════
# 5. 直接看文本的"金标准"断言
# ══════════════════════════════════════════════════════════════════════════


def test_tsv_output_exact_text():
    """小块的 TSV 输出应当是确定的几个字节 —— 转义正确时``值+换行``而已。"""
    sample = _header([("a", "UInt8"), ("b", "String")])
    data = _emit("TSV", sample, [[1, "x"], [2, "y"]])
    assert data.decode("utf-8") == "1\tx\n2\ty\n"


def test_tsv_with_names_and_types_headers():
    """``TSVWithNamesAndTypes`` 的前两行分别是列名与列类型。"""
    sample = _header([("a", "UInt8"), ("b", "String")])
    data = _emit("TSVWithNamesAndTypes", sample, [[1, "x"]])
    lines = data.decode("utf-8").split("\n")
    assert lines[0] == "a\tb"
    assert lines[1] == "UInt8\tString"


def test_prettycompact_contains_column_headers():
    """``PrettyCompact`` 把列名嵌在最上面的框线里,并把值对齐成表格。"""
    sample = _header([("a", "UInt8"), ("b", "String")])
    text = _emit("PrettyCompact", sample, [[1, "x"], [2, "y"]]).decode("utf-8")
    assert "┌─a─┬─b─┐" in text
    assert "│ 1 │ x │" in text
    assert "│ 2 │ y │" in text
    assert "└───┴───┘" in text


def test_text_format_accepts_stringio_output():
    """文本格式写进任何有 ``.write(str)`` 的对象都行,不只是 ``bytearray``。"""
    sample = _header([("a", "UInt8")])
    buf = io.StringIO()
    _emit("TSV", sample, [[1], [2]], out=buf)
    assert buf.getvalue() == "1\n2\n"


# ══════════════════════════════════════════════════════════════════════════
# 6. 构造 Block 的小工具
# ══════════════════════════════════════════════════════════════════════════


def test_block_from_columns_builds_typed_block():
    """``block_from_columns`` 按列装数据,类型与名字来自 sample。"""
    sample = _header([("a", "UInt8"), ("s", "String")])
    blk = block_from_columns(sample, [[1, 2], ["x", "y"]])
    assert blk.rows() == 2
    assert blk.get_names() == ["a", "s"]
    assert [blk.data[i].column.get(1) for i in range(2)] == [2, "y"]


def test_block_from_rows_rejects_wrong_row_width():
    """行宽与列数不符要立刻报错 —— 这正``check_number_of_rows``的意义。"""
    sample = _header([("a", "UInt8"), ("s", "String")])
    with pytest.raises(MiniChError) as ei:
        block_from_rows(sample, [[1]])          # 少了一列
    assert ei.value.code == ErrorCode.INCORRECT_NUMBER_OF_COLUMNS


# ══════════════════════════════════════════════════════════════════════════
# 7. 已记录的**缺陷**(xfail,strict)——只记录,不修
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.xfail(
    raises=MiniChError,
    reason="已知缺陷:文本格式读回时把一个字面值 'NULL' 的 String 判定成了 NULL 字面量,"
           "于是往非可空 String 列里 append(None) 报 ILLEGAL_TYPE_OF_ARGUMENT —— "
           "字符串 'NULL' 无法经 TSV/CSV 往返。根因在 parse_rendered_value 无条件"
           "把 'NULL' 当 NULL,而 parse_value_text(TSV/CSV 走的路径)复用了它。",
)
def test_string_literal_NULL_roundtrip_tsv():
    sample = _header([("s", "String")])
    _assert_roundtrip("TSV", sample, [["NULL"]])


@pytest.mark.xfail(
    reason="已知缺陷:凡文本格式都会 strip 掉字段两端空白,所以 String '  sp  ' "
           "往返后变成 'sp'。parse_rendered_value 开头的 s = text.strip() 是元凶。",
)
def test_string_with_surrounding_spaces_roundtrip_tsv():
    sample = _header([("s", "String")])
    _assert_roundtrip("TSV", sample, [["  sp  "]])


@pytest.mark.xfail(
    reason="已知缺陷:FixedString 的写法与读法不对称 —— 写时 base.render_value_text 对"
           "字符串类调 escape_tsv,读时 parse_rendered_value 却直接调 FixedString 的"
           "deserialize_text(它不做反转义)。于是含制表符/换行的 FixedString 值"
           "往返后被写成反斜杠字面量。",
)
def test_fixedstring_with_tab_roundtrip_tsv():
    sample = _header([("fs", "FixedString(6)")])
    _assert_roundtrip("TSV", sample, [["a\tb"]])


# ══════════════════════════════════════════════════════════════════════════
# 8. 回归:格式层自己写出去、却读不回来的几种输入(**现已修复**)
# ══════════════════════════════════════════════════════════════════════════
#
# 这一节的每一条都是**往返失败**:写出去的东西是自己的实现生成的,
# 读回来却报错或丢数据。这类缺陷的共同点是"单看写、单看读都没问题" ——
# 只有把两端接起来才露馅。


# ── 8.1 JSON 的 Infinity / -Infinity ─────────────────────────────────────


@pytest.mark.parametrize(
    "literal, expected",
    [
        ("Infinity", float("inf")),
        ("-Infinity", float("-inf")),
        ("infinity", float("inf")),
        ("-INFINITY", float("-inf")),
        ("inf", float("inf")),
        ("-inf", float("-inf")),
        ("nan", float("nan")),
        ("NaN", float("nan")),
    ],
)
def test_json_accepts_nonstandard_float_literals(literal, expected):
    """``nan`` / ``inf`` / ``Infinity``(**大小写不敏感**、可带负号)都要能读。

    .. warning::

       这条曾经是裸 ``KeyError``:匹配这类字面量的正则写的是
       ``-?inf(?:inity)?``,而"替换表"只登记了 ``nan``/``inf``/``-inf``
       三个键 —— ``Infinity`` 一进来就是 ``KeyError: 'infinity'``。
       最讽刺的地方在于 ``Infinity`` **正是本仓库自己**
       :func:`minich.util.json_text.to_json_text` 对 ``float('inf')`` 的输出:
       "自己写出去、自己读不回来",而且抛的还不是 ``MiniChError``。
    """
    sample = _header([("x", "Float64")])
    (block,) = list(
        get_input_format("JSONEachRow").read(b"{\"x\":" + literal.encode() + b"}\n", sample)
    )
    got = block.data[0].column.get(0)
    assert _value_equal(got, expected), f"{literal} 读成了 {got!r}"


def test_json_infinity_roundtrip_for_every_json_format():
    """``inf`` / ``-inf`` / ``nan`` 在 4 种 JSON 格式上都要往返成功。"""
    sample = _header([("x", "Float64")])
    for fmt in JSON_64BIT_FORMATS:
        _assert_roundtrip(fmt, sample, [[float("inf")], [float("-inf")], [float("nan")]])


def test_json_infinity_literal_failure_is_a_minich_error():
    """替换表**漏键**时必须是 ``MiniChError``,不是裸 ``KeyError``。

    这条直接钉住"出错也不许漏出 ``KeyError``"这条约束 ——
    ``KeyError`` 不是引擎的错误类型,调用方接不住(它既不是
    ``MiniChError``,也没有错误码)。
    """
    from minich.formats.json_formats import _canonical_literal

    assert _canonical_literal("infinity") == "Infinity"
    assert _canonical_literal("-INFINITY") == "-Infinity"
    with pytest.raises(MiniChError) as ei:
        _canonical_literal("nope")
    assert ei.value.code == ErrorCode.CANNOT_PARSE_TEXT


# ── 8.2 TSV 的"整行为空"记录 ────────────────────────────────────────────


def test_tsv_single_empty_string_row_roundtrip():
    """单列 ``String`` 的空串行**必须**能往返。

    单列表里"空串"的合法表示就是一行空白(``'\\na\\n'``)。读取端以前写的是
    ``if raw != "": …`` —— 把所有空行都洗掉了,于是 ``['', 'a']`` 读回来
    只剩 ``['a']``,**行数少了一条**。(CSV 没这个毛病:空串会被 ``""``
    包起来,记录不是空的。)
    """
    sample = _header([("s", "String")])
    _assert_roundtrip("TSV", sample, [[""], ["a"]])
    _assert_roundtrip("TSV", sample, [["a"], [""]])
    _assert_roundtrip("TSV", sample, [[""]])
    _assert_roundtrip("TSV", sample, [[""], [""], [""]])


def test_tsv_trailing_newline_is_not_a_record():
    """文本末尾的那个换行是**行终止符**,不是"最后一条空记录"。

    规则读起来像吹毛求疵,但它是上面那条的前提:不把结尾这个空元素丢掉,
    ``['', 'a']`` 写出的 ``'\\na\\n'`` 就会被读成 3 行(多一个 ``''``)。
    """
    sample = _header([("s", "String")])
    assert _flatten(list(get_input_format("TSV").read(b"a\n", sample))) == [["a"]]
    assert _flatten(list(get_input_format("TSV").read(b"a", sample))) == [["a"]]
    assert _flatten(list(get_input_format("TSV").read(b"", sample))) == []
    # 空串行(写出来就是一个空行)与"什么都没写"必须分得开
    assert _flatten(list(get_input_format("TSV").read(b"\n", sample))) == [[""]]


def test_tsv_blank_line_in_a_multi_column_table_is_an_error():
    """**取舍**:多列表里的空行是**错误**,不是"排版用的空行"。

    字节上分不出"一条只有 1 个空字段的记录"和"顺手敲的一个空行",
    而静默丢掉整行数据比报错危险得多 —— 所以这里报
    ``INCORRECT_NUMBER_OF_COLUMNS``,与真实 ClickHouse 的列数校验一致。
    """
    sample = _header([("a", "UInt8"), ("b", "String")])
    with pytest.raises(MiniChError) as ei:
        list(get_input_format("TSV").read(b"1\tx\n\n2\ty\n", sample))
    assert ei.value.code == ErrorCode.INCORRECT_NUMBER_OF_COLUMNS


# ── 8.3 垂直制表符 \v ────────────────────────────────────────────────────


def test_tsv_vertical_tab_roundtrip():
    """``\\x0b``(垂直制表)以前会**丢字符**。

    根因看着在转义表(``escape_tsv`` 少了 ``\\v``),真正的伤害在读取端:
    ``\\x0b`` 是 Python ``str.strip()`` 眼里的空白,裸写出去的值会被
    ``parse_rendered_value`` 开头的 ``text.strip()`` 直接吃掉。转义成
    ``\\v`` 之后它不再"位于两端",于是不必动那条 strip 就能往返。
    """
    sample = _header([("s", "String")])
    _assert_roundtrip("TSV", sample, [["\x0b"], ["a\x0bb"]])
    _assert_roundtrip("TSVWithNames", sample, [["\x0b"]])


# ── 8.4 LowCardinality 的字面量渲染 ──────────────────────────────────────


def test_literal_for_unwraps_low_cardinality():
    """``_literal_for`` 要**穿透 ``LowCardinality`` 外壳**。

    ``LowCardinality`` 只是字典编码的外壳,值的 SQL 表示与里面一模一样。
    不拆壳时 ``LowCardinality(String)`` 撞不进 ``dtype.is_string()``
    (它在外壳上返回 False),于是值被当成裸标识符打出 —— ``'x'`` 变成 ``x``。
    """
    from minich.execution.context import _literal_for

    assert _literal_for(get_data_type("LowCardinality(String)"), "x") == "'x'"
    assert _literal_for(get_data_type("LowCardinality(String)"), "it's") == "'it\\'s'"
    # 嵌套外壳:Nullable 在外、LowCardinality 在内(本引擎的推荐写法)
    assert _literal_for(get_data_type("Nullable(LowCardinality(String))"), "x") == "'x'"
    assert _literal_for(get_data_type("Nullable(LowCardinality(String))"), None) == "NULL"
    # 容器里的 LC 元素同样要拆壳
    assert _literal_for(get_data_type("Array(LowCardinality(String))"), ["a", "b"]) == "['a', 'b']"
    # 非字符串的 LC 不受影响(仍然是裸字面量,只是多了一层解包)
    assert _literal_for(get_data_type("LowCardinality(UInt32)"), 3) == "3"


def test_context_insert_into_low_cardinality_column(memory_ctx):
    """``Context.insert``(值 → SQL 字面量 → 解析回来)要能写 LC 列。

    复现原来的报错:``Context.insert("t", [(1, 'x')])`` 拼出的是
    ``INSERT INTO t VALUES (1, x)``,解析时 ``x`` 成了标识符 ——
    ``Code:47 Unknown identifier: 'x'``。(走显式
    ``INSERT INTO t VALUES (1, 'x')`` 反而不受影响,那条路径不经过
    ``_literal_for`` —— 这正是它一直没被发现的原因。)
    """
    ctx = memory_ctx
    ctx.execute(
        "CREATE TABLE t (k UInt32, lc LowCardinality(String)) ENGINE = Memory"
    )
    ctx.insert("t", [(1, "x"), (2, "it's")])
    assert [tuple(r) for r in ctx.execute("SELECT k, lc FROM t").rows()] == [
        (1, "x"),
        (2, "it's"),
    ]


def test_context_insert_into_nullable_low_cardinality_column(memory_ctx):
    """嵌套外壳 ``Nullable(LowCardinality(String))`` 也要拆对。

    这一条走的是两层外壳:``_literal_for`` 先命中 ``Nullable`` 分支
    (值非空时把内核交出去),再命中 ``LowCardinality`` 分支。NULL 走的是
    ``Nullable`` 分支自己的 ``"NULL"`` —— 三条路径都必须对。
    """
    ctx = memory_ctx
    ctx.execute(
        "CREATE TABLE t (k UInt32, n Nullable(LowCardinality(String))) ENGINE = Memory"
    )
    ctx.insert("t", [(1, None), (2, "y")])
    assert [tuple(r) for r in ctx.execute("SELECT k, n FROM t").rows()] == [
        (1, None),
        (2, "y"),
    ]
