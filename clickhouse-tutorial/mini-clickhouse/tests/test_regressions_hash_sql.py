"""三条已确认缺陷的回归测试(哈希/编码、IPv6 往返、``to_sql`` 括号)。

这三条都不是"看着不对"的表面问题,而是**实打实的算错或崩溃**:

* **① 哈希与 ``hex``/``bin`` 对浮点和负整数算错或崩。**
  根因在 ``minich/functions/hashing.py`` 的 ``input_bytes``:整数分支写死了
  ``int(value).to_bytes(..., signed=False)`` —— 浮点被**截断成整数**(``1.1``
  与 ``1.9`` 变成同一个字节串,``sipHash64`` 直接碰撞),负数抛原生
  ``OverflowError``,NaN 抛 ``ValueError``。这会污染建在浮点列上的
  ``GROUP BY``/``uniq``(静默算错)。

* **② ``IPv6NumToString(IPv6StringToNum('::1'))`` 崩。**
  根因在 ``minich/functions/url_ip.py``:``socket.inet_pton`` 对"含 NUL 字节
  的 str"(16 字节地址在 ``FixedString(16)`` 列里的形态)抛的是 ``ValueError``,
  而代码只接了 ``OSError``。于是文档承诺的往返直接漏出原生异常。

* **③ ``to_sql()`` 丢括号,重新解析后算出不同的值。**
  根因在 ``minich/sql/ast_.py``:``BinaryOp.to_sql``/``UnaryOp.to_sql`` 直接
  拼操作数,没有按优先级/结合性补括号。这条**特别隐蔽** —— 打出的文本自身
  仍是"不动点",只有**求值结果**会变,所以"不动点"测试永远抓不到它。
  ``test_to_sql_roundtrip_preserves_value_randomized`` 用固定种子随机生成
  一批嵌套表达式,断言 ``eval(parse(e.to_sql())) == eval(e)``。

.. note::

   ``hex(MD5(''))`` / ``hex('...')`` / ``base64`` / ``URL 组件往返`` 这些
   **本来是对的**,本文件里也放了断言把它们钉住,免得修上面三条时把它们弄坏。
"""

from __future__ import annotations

import hashlib
import math
import random
import struct
from typing import Any, Tuple

import pytest

from minich.errors import MiniChError
from minich.execution.expression import Evaluator, Scope
from minich.sql import parse_expression_text


# ══════════════════════════════════════════════════════════════════════════
# ① 哈希 / hex / bin:浮点与负整数
# ══════════════════════════════════════════════════════════════════════════


def _scalar(ctx, sql: str) -> Any:
    return ctx.execute(sql).scalar()


def test_hex_float64_uses_ieee754_memory_bytes(memory_ctx):
    """已知缺陷①:``hex`` 对浮点必须是**内存里的 IEEE-754 字节**,不是截断后的整数。

    修前 ``hex(toFloat64(1.5))`` 给 ``'0100000000000000'``(1.5 被截断成 1),
    修后是 ``'000000000000F83F'``。
    """
    assert _scalar(memory_ctx, "SELECT hex(toFloat64(1.5))") == "000000000000F83F"
    assert _scalar(memory_ctx, "SELECT hex(toFloat32(1.5))") == "0000C03F"
    # 1.5 的 Float64 小端字节就是 struct.pack('<d', 1.5)
    assert _scalar(memory_ctx, "SELECT hex(toFloat64(1.5))") == struct.pack("<d", 1.5).hex().upper()


def test_hex_negative_int_is_twos_complement_not_overflow(memory_ctx):
    """已知缺陷①:``hex`` 对负整数要按**二进制补码**,不能再抛 ``OverflowError``。

    修前 ``hex(toInt8(-1))`` 直接抛原生 ``OverflowError``(不是 ``MiniChError``)。
    """
    # 修前会崩的这几条,现在必须给出补码字节
    assert _scalar(memory_ctx, "SELECT hex(toInt8(-1))") == "FF"
    assert _scalar(memory_ctx, "SELECT hex(toInt16(-2))") == "FEFF"
    assert _scalar(memory_ctx, "SELECT hex(toInt32(-1))") == "FFFFFFFF"
    assert _scalar(memory_ctx, "SELECT hex(toInt64(-1))") == "FFFFFFFFFFFFFFFF"
    # 有符号/无符号的宽度语义没被弄坏:同值同宽,不同符号,字节相同
    assert _scalar(memory_ctx, "SELECT hex(toUInt8(255))") == "FF"   # 本来就对
    assert _scalar(memory_ctx, "SELECT hex(toUInt16(256))") == "0001"  # 小端,本来就对


def test_hex_and_bin_nan_inf_do_not_raise(memory_ctx):
    """已知缺陷①:NaN / Inf 也要能取字节,不能抛 ``ValueError``。

    修前 ``hex(toFloat64(0)/toFloat64(0))`` 抛 ``ValueError: cannot convert
    float NaN to integer``。
    """
    nan_bytes = _scalar(memory_ctx, "SELECT hex(toFloat64(0) / toFloat64(0))")
    assert nan_bytes == "000000000000F8FF"          # quiet NaN 的内存表示
    inf_bytes = _scalar(memory_ctx, "SELECT hex(toFloat64(1) / toFloat64(0))")
    assert inf_bytes == "000000000000F07F"          # +Inf
    expected_bin = "".join(f"{b:08b}" for b in struct.pack("<d", 1.5))
    assert _scalar(memory_ctx, "SELECT bin(toFloat64(1.5))") == expected_bin


def test_hash_of_distinct_adjacent_floats_no_longer_collides(memory_ctx):
    """已知缺陷①:相邻浮点不能再哈希到同一个值(修前静默碰撞)。

    修前 ``sipHash64(toFloat64(1.1)) == sipHash64(toFloat64(1.9))``
    (两者都被截断成整数 1)。
    """
    a = _scalar(memory_ctx, "SELECT sipHash64(toFloat64(1.1))")
    b = _scalar(memory_ctx, "SELECT sipHash64(toFloat64(1.9))")
    assert a != b, "1.1 与 1.9 的 sipHash64 发生碰撞,说明浮点又被截断了"
    # 同一浮点值必须稳定
    assert a == _scalar(memory_ctx, "SELECT sipHash64(toFloat64(1.1))")
    # 对负浮点也要能算
    assert _scalar(memory_ctx, "SELECT sipHash64(toFloat64(-1.5))") != a


def test_group_by_on_float_column_does_not_merge_distinct_values(memory_ctx):
    """已知缺陷①的真实影响:浮点列上的 ``uniq`` 计数必须是**正确的**。

    修前 1.1 与 1.9 哈希到同一个值,``uniq`` 会把它们当成一个 → 静默算错。
    """
    memory_ctx.create_table("floats", "x Float64", engine_sql="Memory")
    memory_ctx.insert("floats", [(1.1,), (1.9,), (1.1,), (2.5,)])
    assert _scalar(memory_ctx, "SELECT uniq(x) FROM floats") == 3
    assert _scalar(memory_ctx, "SELECT count(DISTINCT x) FROM floats") == 3


def test_hash_hex_of_strings_and_md5_still_correct(memory_ctx):
    """回归保护:修①时**不能**弄坏本来正确的字符串 / MD5 / base64。

    这几条审计已确认当前是对的,``input_bytes`` 的字符串分支必须原样保留。
    """
    assert _scalar(memory_ctx, "SELECT hex('z')") == "7A"
    assert _scalar(memory_ctx, "SELECT hex('ab')") == "6162"
    assert _scalar(memory_ctx, "SELECT hex(MD5(''))") == hashlib.md5(b"").hexdigest().upper()
    assert _scalar(memory_ctx, "SELECT base64Encode('abc')") == "YWJj"
    assert _scalar(memory_ctx, "SELECT unhex('7A')") == "z"
    assert _scalar(memory_ctx, "SELECT bin(toUInt8(1))") == "00000001"


def test_cityhash64_remains_siphash64_alias(memory_ctx):
    """回归保护:``cityHash64`` 是 ``sipHash64`` 的别名,这是**明示的简化**,不要改。

    教程与 ``README.md`` 都声明这条差异(输出与真实 ClickHouse 不同)。
    """
    for text in ["'abc'", "toUInt64(123)", "toFloat64(1.5)", "toInt8(-1)"]:
        assert _scalar(memory_ctx, f"SELECT cityHash64({text})") == \
            _scalar(memory_ctx, f"SELECT sipHash64({text})")


# ══════════════════════════════════════════════════════════════════════════
# ② IPv6 字符串 ↔ 数值往返
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    ["::1", "::", "2001:db8::1", "fe80::1", "::ffff:192.168.0.1",
     "2001:0db8:85a3:0000:0000:8a2e:0370:7334"],
)
def test_ipv6_string_num_string_roundtrip(memory_ctx, text):
    """已知缺陷②:``IPv6NumToString(IPv6StringToNum(s))`` 必须能往返。

    修前 ``'::1'`` 抛 ``ValueError: embedded null character``(只接了 ``OSError``),
    其它地址抛 ``UnicodeEncodeError``(``FixedString(16)`` 里是 surrogateescape
    解出来的原始字节)。
    """
    got = _scalar(
        memory_ctx, f"SELECT IPv6NumToString(IPv6StringToNum('{text}'))"
    )
    # inet_ntop 的规范化输出应当与本机 Python 一致
    import socket

    expected = socket.inet_ntop(socket.AF_INET6, socket.inet_pton(socket.AF_INET6, text))
    assert got == expected


def test_ipv6_toipv6_roundtrip_loopback(memory_ctx):
    """已知缺陷②:``toIPv6`` 这条路径同样要能往返 ``'::1'``。"""
    assert _scalar(memory_ctx, "SELECT IPv6NumToString(toIPv6('::1'))") == "::1"


def test_ipv6_string_to_num_invalid_raises_minicherror(memory_ctx):
    """已知缺陷②:非法输入要抛 ``MiniChError``,不能漏出原生 ``ValueError``/``OSError``。"""
    for bad in ["not-an-ip", "1.2.3.4", "12345", "::1::2"]:
        with pytest.raises(MiniChError):
            _scalar(memory_ctx, f"SELECT IPv6StringToNum('{bad}')")


# ══════════════════════════════════════════════════════════════════════════
# ③ to_sql 的括号 —— "重新解析后的表达式算出不同的值"
# ══════════════════════════════════════════════════════════════════════════


def _eval_ast(ctx, node) -> Tuple[str, Any]:
    """用实现自己的求值器算一个**常量表达式** —— 返回 ``(类型名, 值)``。"""
    col = Evaluator(ctx, Scope(), rows=1).evaluate(node)
    return col.type.get_name(), col.column.get(0)


def _values_equal(before: Tuple[str, Any], after: Tuple[str, Any]) -> bool:
    """比较两次求值结果;NaN 之间视为相等(否则随机表达式里的 NaN 会假报警)。"""
    if before[0] != after[0]:
        return False
    a, b = before[1], after[1]
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def test_to_sql_inserts_multiplication_parens():
    """已知缺陷③:``(2+3)*4`` 不能回打成 ``2 + 3 * 4``(那会算出 14)。"""
    node = parse_expression_text("(2+3)*4")
    assert node.to_sql() == "(2 + 3) * 4"


def test_to_sql_inserts_parens_around_unary_minus_operand():
    """已知缺陷③:``-(1+2)`` 不能回打成 ``-1 + 2``(-3 变成 1)。"""
    node = parse_expression_text("-(1+2)")
    assert node.to_sql() == "-(1 + 2)"


def test_to_sql_inserts_parens_for_right_operand_same_precedence():
    """已知缺陷③:``10-(3-2)`` 不能回打成 ``10 - 3 - 2``(9 变成 5)。

    这是"左结合运算符的**右**操作数同级要加括号"的典型案例。
    """
    node = parse_expression_text("10-(3-2)")
    assert node.to_sql() == "10 - (3 - 2)"


_CONCRETE_EXPRS = [
    "(2+3)*4",
    "-(1+2)",
    "10-(3-2)",
    "2*(3+4)",
    "(2+3)*(4+5)",
    "1 - (2 - 3) - 4",
    "2 - 3 - 4",
]


@pytest.mark.parametrize("text", _CONCRETE_EXPRS)
def test_to_sql_concrete_expressions_reevaluate_identically(memory_ctx, text):
    """已知缺陷③:上面这些具体表达式的"求值结果"在往返后必须一致。

    注意:断言的是**值**,不是文本 —— 文本不动点永远抓不到这个 bug。
    """
    node = parse_expression_text(text)
    reparsed = parse_expression_text(node.to_sql())
    assert _values_equal(_eval_ast(memory_ctx, node), _eval_ast(memory_ctx, reparsed)), \
        f"{text!r} 回打成 {node.to_sql()!r} 后算出了不同的值"


_FLAT_EXPRS = [
    "2 + 3 * 4",
    "10 - 3 - 2",
    "2 * 3 + 4",
    "100 / 5 / 2",
    "100 % 7 % 3",
    "1 < 2 AND 3 > 4 OR 5 = 5",
    "NOT 1 = 2",
    "-1 + 2",
    "~(1 + 2)",
    "abs(-3) + 1",
    "if(1, 2 + 3, 4 * 5)",
    "1 + 2 * 3 - 4 / 5",
    "2 + 3 * 4 - 5 / 5",
]


@pytest.mark.parametrize("text", _FLAT_EXPRS)
def test_to_sql_flat_corpus_reevaluate_identically(memory_ctx, text):
    """已知缺陷③:不带括号的平铺表达式,往返后求值结果也不能变。"""
    node = parse_expression_text(text)
    reparsed = parse_expression_text(node.to_sql())
    assert _values_equal(_eval_ast(memory_ctx, node), _eval_ast(memory_ctx, reparsed)), \
        f"{text!r} 回打成 {node.to_sql()!r} 后算出了不同的值"


# ── 随机化属性测试:对任意表达式 e,``eval(parse(e.to_sql())) == eval(e)`` ──

#: 固定随机种子 —— 保证可复现(换种子会换一批表达式)
_RANDOM_SEED = 2024
#: 生成的表达式个数(去掉求值就报错的,实际检查数见断言)
_RANDOM_COUNT = 4000
_RANDOM_DEPTH = 4

_BINARY_OPS = ["+", "-", "*", "/", "%", "=", "!=", "<", ">", "<=", ">=", "AND", "OR"]
_UNARY_OPS = ["-", "NOT", "~"]
_LEAVES = ["0", "1", "2", "3", "5", "1.5", "2.25", "10"]


def _gen_expression(rng: random.Random, depth: int) -> str:
    """生成一个随机表达式 —— 二元/一元/函数调用/括号都要覆盖到。

    刻意**总是给子表达式加括号**再交给解析器:这样解析出的树结构是明确的,
    ``to_sql`` 必须自己把"够不够加括号"重新算对,才能往返一致。
    """
    if depth <= 0 or rng.random() < 0.25:
        return rng.choice(_LEAVES)
    r = rng.random()
    if r < 0.55:
        op = rng.choice(_BINARY_OPS)
        return f"({_gen_expression(rng, depth - 1)} {op} {_gen_expression(rng, depth - 1)})"
    if r < 0.75:
        op = rng.choice(_UNARY_OPS)
        sep = " " if op == "NOT" else ""
        return f"({op}{sep}{_gen_expression(rng, depth - 1)})"
    if r < 0.9:
        fn = rng.choice(["abs", "negate", "plus", "toInt32", "toUInt64", "toFloat64"])
        if fn == "plus":
            return f"{fn}({_gen_expression(rng, depth - 1)}, {_gen_expression(rng, depth - 1)})"
        return f"{fn}({_gen_expression(rng, depth - 1)})"
    return (f"if({_gen_expression(rng, depth - 1)}, "
            f"{_gen_expression(rng, depth - 1)}, {_gen_expression(rng, depth - 1)})")


@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # numpy 对 inf/NaN 的告警
def test_to_sql_roundtrip_preserves_value_randomized(memory_ctx):
    """已知缺陷③(随机化属性测试):任意表达式 ``e``,``eval(parse(e.to_sql())) == eval(e)``。

    用**实现自己的求值入口**(``Evaluator``)求值,不用 Python 的 ``eval``
    (那算的是 Python 语义,不是 ClickHouse 语义)。固定随机种子保证可复现。

    .. note::

       求值时就报错的表达式(比如 ``% 0``)会被跳过 —— 我们只比较"两边都
       能算出来"的样本,否则无法判断回打是否改变了语义。
    """
    rng = random.Random(_RANDOM_SEED)
    checked = 0
    for _ in range(_RANDOM_COUNT):
        text = _gen_expression(rng, _RANDOM_DEPTH)
        node = parse_expression_text(text)
        try:
            before = _eval_ast(memory_ctx, node)
        except MiniChError:
            continue  # 原表达式本身就求不了值(如除以零),跳过
        reparsed = parse_expression_text(node.to_sql())
        after = _eval_ast(memory_ctx, reparsed)
        assert _values_equal(before, after), (
            f"回打改变了语义:\n  原文   {text}\n  to_sql {node.to_sql()!r}\n"
            f"  求值前 {before}\n  求值后 {after}"
        )
        checked += 1

    # 保证测试**确实**检查了足够多的样本(不是被 continue 全跳过了)
    assert checked >= 2000, f"只检查了 {checked} 个表达式,样本太少"
