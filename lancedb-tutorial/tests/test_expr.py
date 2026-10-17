"""``mini_lancedb.expr`` 的测试。

这个文件刻意写得比较"硬核"，因为表达式子系统里绝大部分 bug 都是
**静默**的：一个 NULL 语义错了的 ``NOT``、一个丢了换行的 ``.``、
一个把 ``AND`` 当作可部分下推的 ``OR``…… 都不会抛异常，
只会让结果少几行或者多几行。所以测试的重点是：

1. **对拍**：随机数据上，向量化求值器必须和一份手写的慢速 Python
   三值逻辑参考实现逐行一致（值和 ``valid`` 位图都要对得上）；
2. **定点对拍**：LIKE 的快速路径（``startswith`` / 正则）必须和
   一个独立实现的通配匹配器逐位一致；
3. **报错位置**：语法错误的 ``position`` 必须精确指到出错字符上。

一个必须记住的坑：:class:`Expr` 的 ``__eq__`` 被重载成"构造比较节点"，
所以 ``assert a == b`` **永远成立**。本文件一律用 :meth:`Expr.equals`
或 ``to_sql()`` 比较。
"""

from __future__ import annotations

import json
import math
import random

import numpy as np
import pytest

from mini_lancedb.arrays import (
    FixedSizeListArray,
    ListArray,
    NullArray,
    PrimitiveArray,
    StructArray,
)
from mini_lancedb.errors import ExpressionSyntaxError, PlanError, QueryError
from mini_lancedb.expr import (
    Between,
    BinaryOp,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    IsIn,
    IsNull,
    Like,
    Literal,
    UnaryOp,
    and_,
    col,
    compile_like,
    deserialize,
    estimate_selectivity,
    evaluate,
    evaluate_predicate,
    extract_zone_map_predicate,
    fold_constants,
    like_to_regex,
    lit,
    literal_value,
    not_,
    or_,
    parse,
    parse_predicate,
    serialize,
    simplify,
    split_conjuncts,
    split_disjuncts,
    to_index_expr,
    tokenize,
    type_to_sql,
)
from mini_lancedb.index.base import (
    And as IndexAnd,
    IsIn as IndexIsIn,
    Not as IndexNot,
    Or as IndexOr,
    RangeQuery,
)
from mini_lancedb.record import RecordBatch
from mini_lancedb.schema import Field, Schema
from mini_lancedb.types import (
    BOOL,
    FLOAT32,
    FLOAT64,
    INT32,
    INT64,
    NULL,
    UTF8,
    FixedSizeList,
    List as ListType,
    Struct as StructType,
)


# ===========================================================================
# 工具
# ===========================================================================

TOLERATED_INPUTS = "ab%_"


def sql_of(text: str) -> str:
    return parse(text).to_sql()


def assert_sql(text: str, expected: str) -> None:
    assert sql_of(text) == expected, f"{text!r} -> {sql_of(text)!r} != {expected!r}"


def batch_of(**columns: object) -> RecordBatch:
    return RecordBatch.from_pydict(columns)


def mask_of(sql: str, batch: RecordBatch) -> list:
    return evaluate_predicate(parse(sql), batch).tolist()


# -- 慢速参考实现（三值逻辑） ----------------------------------------------


def _is_nan(v: object) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _r_and(a: object, b: object) -> object:
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def _r_or(a: object, b: object) -> object:
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def _r_not(a: object) -> object:
    return None if a is None else (not a)


def _r_cmp(op: str, a: object, b: object) -> object:
    if a is None or b is None:
        return None
    if _is_nan(a) or _is_nan(b):
        # 本实现的规则：涉及 NaN 的比较是 NULL（真实 LanceDB 用全序，见 eval 模块文档）
        return None
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


def _r_arith(op: str, a: object, b: object) -> object:
    if a is None or b is None:
        return None
    if op in ("/", "%") and b == 0:
        return None
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if isinstance(a, int) and isinstance(b, int):
            q = abs(a) // abs(b)
            return q if (a < 0) == (b < 0) else -q
        return a / b
    if isinstance(a, int) and isinstance(b, int):
        return int(math.fmod(a, b))
    return math.fmod(a, b)


def _split_pattern(pattern: str, escape):
    """测试侧独立的 LIKE 模式分词器。"""

    toks = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if escape is not None and ch == escape:
            i += 1
            if i >= len(pattern):
                raise ValueError("dangling escape")
            toks.append(("lit", pattern[i]))
            i += 1
            continue
        if ch == "%":
            toks.append(("%", None))
        elif ch == "_":
            toks.append(("_", None))
        else:
            toks.append(("lit", ch))
        i += 1
    return toks


def _glob_match(toks, s: str) -> bool:
    """经典的通配匹配（贪婪 + 回溯），与 numpy / 正则路径完全独立。"""

    ti = si = 0
    star = -1
    star_si = 0
    while si < len(s):
        if ti < len(toks):
            kind, ch = toks[ti]
            if kind == "lit" and ch == s[si]:
                ti += 1
                si += 1
                continue
            if kind == "_":
                ti += 1
                si += 1
                continue
            if kind == "%":
                star = ti
                star_si = si
                ti += 1
                continue
        if star >= 0:
            ti = star + 1
            star_si += 1
            si = star_si
            continue
        return False
    while ti < len(toks) and toks[ti][0] == "%":
        ti += 1
    return ti == len(toks)


def _ref_like(value, pattern: str, escape, negated: bool) -> object:
    if value is None:
        return None
    hit = _glob_match(_split_pattern(pattern, escape), value)
    return (not hit) if negated else hit


def ref_eval(expr: Expr, cols: dict, n: int):
    """把 AST 逐行解释一遍，返回 ``list[True/False/其它值/None]``。"""

    if isinstance(expr, Literal):
        return [expr.value] * n
    if isinstance(expr, ColumnRef):
        return list(cols[expr.name])
    if isinstance(expr, UnaryOp):
        inner = ref_eval(expr.operand, cols, n)
        if expr.op == "NOT":
            return [_r_not(v) for v in inner]
        if expr.op == "-":
            return [None if v is None else -v for v in inner]
        return list(inner)
    if isinstance(expr, IsNull):
        inner = ref_eval(expr.child, cols, n)
        return [(v is None) if not expr.negated else (v is not None) for v in inner]
    if isinstance(expr, IsIn):
        child = ref_eval(expr.child, cols, n)
        values = [ref_eval(v, cols, n) for v in expr.values]
        out = []
        for i in range(n):
            if expr.negated:
                acc: object = _r_cmp("!=", child[i], values[0][i])
                for v in values[1:]:
                    acc = _r_and(acc, _r_cmp("!=", child[i], v[i]))
            else:
                acc = _r_cmp("=", child[i], values[0][i])
                for v in values[1:]:
                    acc = _r_or(acc, _r_cmp("=", child[i], v[i]))
            out.append(acc)
        return out
    if isinstance(expr, Between):
        child = ref_eval(expr.child, cols, n)
        low = ref_eval(expr.low, cols, n)
        high = ref_eval(expr.high, cols, n)
        out = []
        for i in range(n):
            inner = _r_and(
                _r_cmp(">=", child[i], low[i]), _r_cmp("<=", child[i], high[i])
            )
            out.append(_r_not(inner) if expr.negated else inner)
        return out
    if isinstance(expr, Like):
        child = ref_eval(expr.child, cols, n)
        pattern = expr.literal_pattern()
        assert pattern is not None, "参考实现只支持字面量 LIKE 模式"
        return [_ref_like(child[i], pattern, expr.escape, expr.negated) for i in range(n)]
    if isinstance(expr, BinaryOp):
        left = ref_eval(expr.left, cols, n)
        right = ref_eval(expr.right, cols, n)
        op = expr.op
        if op == "AND":
            return [_r_and(left[i], right[i]) for i in range(n)]
        if op == "OR":
            return [_r_or(left[i], right[i]) for i in range(n)]
        if op in ("=", "!=", "<", "<=", ">", ">="):
            return [_r_cmp(op, left[i], right[i]) for i in range(n)]
        if op == "||":
            return [
                None if left[i] is None or right[i] is None else str(left[i]) + str(right[i])
                for i in range(n)
            ]
        return [_r_arith(op, left[i], right[i]) for i in range(n)]
    raise AssertionError(f"参考实现不支持 {type(expr).__name__}")


def random_columns(seed: int = 20261017, n: int = 64) -> dict:
    rng = random.Random(seed)
    words = ["apple", "banana", "cherry", "", "avocado", "berry", "ab"]
    return {
        "a": [rng.choice([None] + list(range(0, 10))) for _ in range(n)],
        "f": [
            rng.choice([None, float("nan"), 0.0, 1.5, 2.0, 7.25, -3.5])
            for _ in range(n)
        ],
        "s": [rng.choice([None] + words) for _ in range(n)],
    }


# ===========================================================================
# 1. 分词器
# ===========================================================================


def test_tokenize_basic_kinds():
    toks = tokenize("a = 1")
    assert [(t.kind, t.value) for t in toks] == [
        ("ident", "a"),
        ("op", "="),
        ("number", 1),
        ("eof", None),
    ]


def test_tokenize_positions():
    toks = tokenize("a >= 1.5")
    assert [t.pos for t in toks] == [0, 2, 5, 8]
    assert toks[1].value == ">="


def test_tokenize_number_flavours():
    # 用 (值, 类型) 列表而不是 dict —— Python 里 1 == 1.0 且哈希相同，
    # 用 dict 会把 int 和 float 两个字面量合并掉
    pairs = [
        (t.value, type(t.value))
        for t in tokenize("1 1.5 .5 1. 1e3 2E-2 1.5e+7")
        if t.kind == "number"
    ]
    assert pairs == [
        (1, int),
        (1.5, float),
        (0.5, float),
        (1.0, float),
        (1000.0, float),
        (0.02, float),
        (15000000.0, float),
    ]


def test_tokenize_string_doubled_quote():
    toks = tokenize("'it''s'")
    assert toks[0].kind == "string"
    assert toks[0].value == "it's"


def test_tokenize_string_keeps_backslash_literally():
    # SQL 标准里反斜杠在普通字符串里**没有**特殊含义（不同于 MySQL）
    assert tokenize(r"'a\nb'")[0].value == "a\\nb"


def test_tokenize_quoted_identifiers():
    assert tokenize('"double"')[0].value == "double"
    assert tokenize("`back`")[0].value == "back"
    assert tokenize('"a""b"')[0].value == 'a"b'
    assert tokenize("`a``b`")[0].value == "a`b"


def test_tokenize_hex_bytes_literal():
    tok = tokenize("X'6869'")[0]
    assert tok.kind == "bytes" and tok.value == b"hi"
    assert tokenize("x'aabb'")[0].value == b"\xaa\xbb"


def test_tokenize_unknown_char():
    with pytest.raises(ExpressionSyntaxError) as ei:
        tokenize("a @ b")
    assert ei.value.position == 2


def test_tokenize_unterminated_string_reports_open_quote():
    with pytest.raises(ExpressionSyntaxError) as ei:
        tokenize("a = 'oops")
    assert ei.value.position == 4


def test_tokenize_multi_char_operator_prefers_longest():
    assert [t.value for t in tokenize("a <= b")] == ["a", "<=", "b", None]
    assert [t.value for t in tokenize("a <> b")] == ["a", "<>", "b", None]
    assert [t.value for t in tokenize("a || b")] == ["a", "||", "b", None]
    assert [t.value for t in tokenize("a::int")] == ["a", "::", "int", None]


# ===========================================================================
# 2. 解析优先级 / 结合性
# ===========================================================================


def test_precedence_or_below_and():
    assert_sql("a OR b AND c", "a OR b AND c")
    assert_sql("(a OR b) AND c", "(a OR b) AND c")
    expr = parse("a OR b AND c")
    assert isinstance(expr.right, BinaryOp) and expr.right.op == "AND"


def test_precedence_not_below_comparison():
    # SQL 标准：NOT a = 1 是 NOT (a = 1)
    expr = parse("NOT a = 1")
    assert isinstance(expr, UnaryOp) and expr.op == "NOT"
    assert isinstance(expr.operand, BinaryOp) and expr.operand.op == "="
    assert_sql("NOT a = 1", "NOT a = 1")
    assert_sql("NOT (a AND b)", "NOT (a AND b)")


def test_precedence_arithmetic_over_comparison():
    expr = parse("a + 1 > b * 2")
    assert expr.op == ">"
    assert expr.left.op == "+" and expr.right.op == "*"


def test_precedence_mul_over_add():
    assert_sql("a + b * c", "a + b * c")
    assert_sql("(a + b) * c", "(a + b) * c")
    assert_sql("a * b + c", "a * b + c")
    assert_sql("a * (b + c)", "a * (b + c)")


def test_associativity_subtraction_left():
    assert_sql("a - b - c", "a - b - c")
    assert_sql("a - (b - c)", "a - (b - c)")
    assert_sql("a / b / c", "a / b / c")
    assert_sql("a / (b * c)", "a / (b * c)")
    assert_sql("(a * b) / c", "a * b / c")


def test_associativity_and_or_left():
    # AND/OR 结合但**不**重排结构，保证 to_sql 能原样还原
    assert_sql("a AND b AND c", "a AND b AND c")
    assert_sql("a AND (b AND c)", "a AND (b AND c)")
    assert_sql("a OR (b OR c)", "a OR (b OR c)")


def test_comparison_chain_renders_left_assoc():
    assert_sql("(a = b) = c", "a = b = c")
    assert_sql("a = (b = c)", "a = (b = c)")


def test_cast_binds_tighter_than_unary_minus():
    expr = parse("-a::int")
    assert isinstance(expr, UnaryOp) and expr.op == "-"
    assert isinstance(expr.operand, Cast)
    assert_sql("-a::int", "-CAST(a AS int32)")


def test_unary_minus_never_emits_double_dash():
    # ``--`` 在 SQL 里是行注释，生成这种文本很危险
    assert_sql("- -a", "-(-a)")
    assert_sql("-(-a)", "-(-a)")


def test_string_concat_precedence():
    assert_sql("a || b || c", "a || b || c")
    assert_sql("a || (b || c)", "a || (b || c)")
    assert_sql("a || b = c", "a || b = c")


def test_deeply_nested_parentheses():
    for depth in (10, 25):
        text = "(" * depth + "a = 1" + ")" * depth
        assert parse(text).equals(parse("a = 1"))
    assert_sql("((((((((((a + b)))))))))) * c", "(a + b) * c")


def test_parentheses_change_meaning_and_stay_distinct():
    assert not parse("(a OR b) AND c").equals(parse("a OR b AND c"))
    assert parse("(a + b) * c").to_sql() != parse("a + b * c").to_sql()


def test_all_comparison_operators():
    for op, canonical in [("=", "="), ("!=", "!="), ("<>", "!="), ("<", "<"),
                          ("<=", "<="), (">", ">"), (">=", ">=")]:
        assert_sql(f"a {op} 1", f"a {canonical} 1")


def test_all_arithmetic_operators():
    for op in ("+", "-", "*", "/", "%"):
        assert_sql(f"a {op} 1", f"a {op} 1")
    assert_sql("a % b * c", "a % b * c")


def test_c_style_operators_are_sugar_only():
    assert ((col("a") > 1) & (col("b") < 2)).to_sql() == "a > 1 AND b < 2"
    assert ((col("a") > 1) | (col("b") < 2)).to_sql() == "a > 1 OR b < 2"
    assert (~col("a")).to_sql() == "NOT a"


def test_in_list_parens_and_brackets():
    assert_sql("a IN (1, 2, 3)", "a IN (1, 2, 3)")
    assert_sql("a IN [1, 2]", "a IN (1, 2)")
    assert_sql("a NOT IN (1)", "a NOT IN (1)")


def test_in_with_expressions_and_strings():
    assert_sql("a IN (1 + 1, 'x')", "a IN (1 + 1, 'x')")


def test_like_needs_no_escape_by_default():
    # 默认转义符是 None：反斜杠没有特殊含义（和 MySQL 不同）
    node = parse(r"a LIKE 'x\%y'")
    assert isinstance(node, Like) and node.escape is None


def test_like_with_escape_clause():
    node = parse(r"a LIKE 'x\%y' ESCAPE '\'")
    assert isinstance(node, Like) and node.escape == "\\"
    assert node.to_sql() == r"a LIKE 'x\%y' ESCAPE '\'"
    assert parse(node.to_sql()).equals(node)


def test_like_not_like_and_ilike():
    assert_sql("a NOT LIKE 'x%'", "a NOT LIKE 'x%'")
    # ILIKE 是本实现支持的方言糖：转成 lower() 两侧
    assert_sql("a ILIKE 'X%'", "lower(a) LIKE lower('X%')")


def test_is_null_forms():
    assert isinstance(parse("a IS NULL"), IsNull)
    assert parse("a IS NOT NULL").negated is True
    assert_sql("a IS NULL", "a IS NULL")
    assert_sql("a IS NOT NULL", "a IS NOT NULL")


def test_between_forms():
    node = parse("a BETWEEN 1 AND 2")
    assert isinstance(node, Between) and node.negated is False
    assert_sql("a NOT BETWEEN 1 AND 2", "a NOT BETWEEN 1 AND 2")
    assert_sql("a BETWEEN b + 1 AND c * 2", "a BETWEEN b + 1 AND c * 2")


def test_cast_forms_are_equivalent():
    assert_sql("cast(a as double) > 1", "CAST(a AS float64) > 1")
    assert parse("a::double").equals(parse("CAST(a AS double)"))
    assert parse("a::int").equals(parse("CAST(a AS int)"))
    assert parse("a::text").type == UTF8
    assert_sql("a::double::int32", "CAST(CAST(a AS float64) AS int32)")
    assert type_to_sql(INT32) == "int32"
    assert type_to_sql(BOOL) == "bool"


def test_literals_special_floats_and_negatives():
    assert_sql("nan", "nan")
    assert_sql("inf", "inf")
    assert_sql("-1", "-1")
    assert isinstance(parse("-1"), UnaryOp)
    ok, value = literal_value(parse("-1"))
    assert ok and value == -1
    assert isinstance(parse("-1.5e2"), UnaryOp)
    assert parse("TRUE").value is True
    assert parse("NULL").value is None


def test_string_literal_escaping_roundtrip():
    node = parse("'it''s a ''quote'''")
    assert node.value == "it's a 'quote'"
    assert parse(node.to_sql()).value == node.value


def test_bytes_literal_roundtrip():
    node = parse("X'00ff'")
    assert node.value == b"\x00\xff"
    assert parse(node.to_sql()).value == b"\x00\xff"


def test_quoted_column_reference_styles():
    assert parse('"weird name"').name == "weird name"
    assert parse("`other name`").name == "other name"
    assert parse('"with""quote"').name == 'with"quote'
    # 关键字做列名必须加引号，否则会被当成关键字
    assert parse('"and"').name == "and"
    assert col("and").to_sql() == '"and"'
    assert parse(col("and").to_sql()).name == "and"


def test_parser_roundtrip_over_many_samples():
    samples = [
        "a = 1", "a <> 1 AND b > 2 OR c", "NOT a = 1", "a IN (1, 2, 3)",
        "a NOT IN ('x', 'y')", "a IS NULL", "a IS NOT NULL",
        "a BETWEEN 1 AND 2", "a NOT BETWEEN 1 AND 2",
        "a LIKE 'x%'", "a NOT LIKE '%y'", r"a LIKE 'z\_' ESCAPE '\'",
        "a + b * c - d / e % f", "a::double > 1.5", "cast(b as utf8) = 'x'",
        "array_contains(v, 1)", "length(s) > 3", "lower(s) = upper(t)",
        "'a' || 'b' || s", "[1, 2, 3]", "a = 'it''s'", "X'0102' = b",
        "a IS NOT NULL AND (b < 1 OR b > 9)", "NOT (a AND b) OR NOT c",
        "a BETWEEN -1 AND -5", "nan = a", "abs(a - 1) < 0.5",
    ]
    for text in samples:
        first = parse(text)
        second = parse(first.to_sql())
        assert first.equals(second), f"{text!r}: {first.to_sql()!r} 往返不一致"


# ===========================================================================
# 3. 报错位置
# ===========================================================================


@pytest.mark.parametrize(
    "text,position",
    [
        ("a = ", 4),
        ("a AND", 5),
        ("(a = 1", 6),
        ("a = 'x", 4),
        ("a @ b", 2),
        ("a IN", 4),
        ("a IN (1, 2", 10),
        ("1 +", 3),
        ("a BETWEEN 1", 11),
        ("a = 1)", 5),
        ("*", 0),
        ("a LIKE", 6),
        ("a IN ()", 6),
        (")", 0),
        ("a IS 5", 5),
        ("a = 1 AND (", 11),
        ("1 2 3", 2),
        ("nope(1)", 0),
    ],
)
def test_error_positions(text, position):
    with pytest.raises(ExpressionSyntaxError) as ei:
        parse(text)
    assert ei.value.position == position, f"{text!r} 报到了 {ei.value.position}"


def test_error_caret_alignment():
    with pytest.raises(ExpressionSyntaxError) as ei:
        parse("a @ b")
    assert ei.value.context["caret"] == "  ^"
    assert ei.value.expression == "a @ b"
    assert ei.value.context["expression"] == "a @ b"


def test_error_message_mentions_expected_token():
    with pytest.raises(ExpressionSyntaxError) as ei:
        parse("(a = 1")
    assert ")" in str(ei.value)


def test_error_on_empty_in_list():
    with pytest.raises(ExpressionSyntaxError) as ei:
        parse("a IN ()")
    assert "空" in str(ei.value)


def test_error_on_bad_function_name():
    with pytest.raises(ExpressionSyntaxError) as ei:
        parse("no_such_fn(a)")
    assert ei.value.position == 0
    assert "no_such_fn" in str(ei.value)


def test_error_on_wrong_function_arity():
    with pytest.raises(ExpressionSyntaxError):
        parse("abs(a, b)")
    with pytest.raises(ExpressionSyntaxError):
        parse("length()")


def test_error_on_incomplete_list_literal():
    with pytest.raises(ExpressionSyntaxError) as ei:
        parse("[1, 2")
    assert ei.value.position == 5


def test_error_on_non_literal_list():
    with pytest.raises(ExpressionSyntaxError):
        parse("[a + 1]")


def test_error_on_dangling_like_escape():
    node = parse(r"a LIKE 'x\%' ESCAPE '\'")
    # 解析没问题；模式里的转义由 LIKE 编译器负责报错
    assert node.escape == "\\"
    with pytest.raises(ExpressionSyntaxError):
        compile_like("x\\", "\\")


def test_error_on_multi_char_escape():
    with pytest.raises(ExpressionSyntaxError):
        parse("a LIKE 'x' ESCAPE 'ab'")


def test_error_on_cast_without_as():
    with pytest.raises(ExpressionSyntaxError) as ei:
        parse("CAST(a 5)")
    assert ei.value.position == 7


def test_error_on_unknown_cast_type():
    with pytest.raises(ExpressionSyntaxError):
        parse("a::nosuchtype")


# ===========================================================================
# 4. 节点 / 序列化
# ===========================================================================


def test_operator_overloads_build_comparison_nodes():
    node = col("a") == 1
    assert isinstance(node, BinaryOp) and node.op == "="
    assert isinstance(col("a") != 1, BinaryOp)
    assert (col("a") < 1).op == "<"
    assert (col("a") <= 1).op == "<="
    assert (col("a") > 1).op == ">"
    assert (col("a") >= 1).op == ">="
    assert (col("a") + 1).op == "+"
    assert (1 + col("a")).op == "+"
    assert (col("a") - 1).op == "-"
    assert (1 - col("a")).op == "-"
    assert (col("a") * 2).op == "*"
    assert (2 * col("a")).op == "*"
    assert (col("a") / 2).op == "/"
    assert (col("a") % 2).op == "%"
    assert (-col("a")).op == "-"
    assert isinstance(col("a") & col("b"), BinaryOp)


def test_expr_eq_returns_expr_not_bool():
    node = col("a") == col("b")
    assert isinstance(node, Expr)
    assert bool(node) is True  # 这就是"assert a == b 永远成立"的来源
    assert node.to_sql() == "a = b"
    assert col("a").equals(col("a"))
    assert not col("a").equals(col("b"))


def test_expr_hash_follows_sql_text():
    assert hash(col("a") == 1) == hash(parse("a = 1"))
    assert (col("a") == 1).key() == "a = 1"


def test_references_collects_all_columns():
    expr = parse("a > 1 AND (b LIKE 'x%' OR length(c) = 2)")
    assert expr.references() == {"a", "b", "c"}
    assert parse("1 + 2").references() == set()


def test_children_exposed_for_traversal():
    expr = parse("a = 1 AND b = 2")
    assert len(expr.children) == 2
    assert all(isinstance(c, Expr) for c in expr.children)
    assert parse("a LIKE 'x%'").children[1].value == "x%"


def test_serialize_roundtrip_all_node_types():
    samples = [
        "a", "1", "'x'", "X'00'", "NULL", "TRUE", "[1, 2]",
        "a = 1", "a AND b", "a + 1", "NOT a", "-a",
        "a IS NULL", "a IS NOT NULL", "a IN (1, 2)", "a NOT IN (1)",
        "a LIKE 'x%'", "a NOT LIKE '%y'", r"a LIKE 'z\_' ESCAPE '\'",
        "a BETWEEN 1 AND 2", "CAST(a AS int64)", "lower(a)",
    ]
    for text in samples:
        node = parse(text)
        restored = deserialize(serialize(node))
        assert node.equals(restored), text


def test_serialize_is_json_safe():
    for text in ["a = 1 AND b LIKE 'x%'", "X'00ff'", "[1, 2, 3]", "a BETWEEN 1 AND 2"]:
        payload = json.dumps(serialize(parse(text)))
        assert json.loads(payload) == serialize(parse(text))


def test_helpers_and_or_not_are_faithful():
    assert and_(col("a"), col("b")).to_sql() == "a AND b"
    assert and_().value is True
    assert or_().value is False
    assert and_(col("a"), col("b"), col("c")).to_sql() == "a AND b AND c"
    assert and_(col("a"), lit(True)).to_sql() == "a AND TRUE"  # 不做化简
    assert not_(col("a")).to_sql() == "NOT a"
    assert not_(not_(col("a"))).to_sql() == "NOT NOT a"


def test_literal_type_inference():
    assert lit(1).type == INT64
    assert lit(1.5).type == FLOAT64
    assert lit("x").type == UTF8
    assert lit(True).type == BOOL
    assert lit(None).type == NULL
    assert lit([1, 2]).type == FixedSizeList(INT64, 2)
    assert lit(np.int32(3)).value == 3 and isinstance(lit(np.int32(3)).value, int)


# ===========================================================================
# 5. parse_predicate：列解析
# ===========================================================================


SCHEMA = Schema(
    [
        Field("a", INT64),
        Field("f", FLOAT64),
        Field("s", UTF8),
        Field("flag", BOOL),
        Field("v", FixedSizeList(FLOAT32, 4)),
    ]
)


def test_parse_predicate_resolves_column_types():
    expr = parse_predicate("a > 1 AND f < 2.5", SCHEMA)
    # 解析成 ((a > 1) AND (f < 2.5))
    assert isinstance(expr.left.left, ColumnRef)
    assert expr.left.left.name == "a" and expr.left.left.type == INT64
    assert expr.left.right.type == INT64  # 字面量 1
    assert expr.right.left.type == FLOAT64  # 列 f
    assert expr.right.right.type == FLOAT64  # 字面量 2.5
    assert expr.type == BOOL


def test_parse_predicate_reports_available_columns():
    with pytest.raises(PlanError) as ei:
        parse_predicate("nope = 1", SCHEMA)
    assert "nope" in str(ei.value)
    assert ei.value.context["available"] == SCHEMA.names
    assert "a" in ei.value.context["hint"]


def test_parse_predicate_rejects_non_boolean_bare_column():
    with pytest.raises(PlanError):
        parse_predicate("a", SCHEMA)
    # 布尔列裸写是合法的
    assert parse_predicate("flag", SCHEMA).name == "flag"


def test_parse_predicate_keeps_case_of_columns():
    schema = Schema([Field("MixedCase", INT64)])
    assert parse_predicate("MixedCase = 1", schema).left.name == "MixedCase"
    with pytest.raises(PlanError):
        parse_predicate("mixedcase = 1", schema)


def test_parse_without_schema_keeps_column_names():
    expr = parse("a > 1")
    assert isinstance(expr.left, ColumnRef)
    assert expr.left.name == "a"
    assert expr.left.type == NULL  # 类型未知，求值时再按实际列类型走


def test_parse_predicate_function_result_type():
    assert parse_predicate("length(s) = 3", SCHEMA).left.type == INT64
    assert parse_predicate("lower(s) = 'x'", SCHEMA).left.type == UTF8
    assert parse_predicate("starts_with(s, 'x')", SCHEMA).type == BOOL


def test_parse_predicate_array_contains():
    expr = parse_predicate("array_contains(v, 1.0)", SCHEMA)
    assert isinstance(expr, FunctionCall) and expr.type == BOOL


# ===========================================================================
# 6. 三值逻辑
# ===========================================================================

AND_TABLE = [
    (True, True, True),
    (True, False, False),
    (True, None, None),
    (False, True, False),
    (False, False, False),
    (False, None, False),
    (None, True, None),
    (None, False, False),
    (None, None, None),
]

OR_TABLE = [
    (True, True, True),
    (True, False, True),
    (True, None, True),
    (False, True, True),
    (False, False, False),
    (False, None, None),
    (None, True, True),
    (None, False, None),
    (None, None, None),
]


def _mk_batch_from_pairs(table):
    a = [row[0] for row in table]
    b = [row[1] for row in table]
    return batch_of(a=a, b=b)


def test_three_valued_and_truth_table():
    batch = _mk_batch_from_pairs(AND_TABLE)
    got = evaluate(parse("a AND b"), batch).to_pylist()
    assert got == [row[2] for row in AND_TABLE]


def test_three_valued_or_truth_table():
    batch = _mk_batch_from_pairs(OR_TABLE)
    got = evaluate(parse("a OR b"), batch).to_pylist()
    assert got == [row[2] for row in OR_TABLE]


def test_three_valued_not_truth_table():
    batch = batch_of(a=[True, False, None])
    assert evaluate(parse("NOT a"), batch).to_pylist() == [False, True, None]


def test_predicate_excludes_null_rows():
    batch = batch_of(a=[1, None, 3])
    assert mask_of("a = 1", batch) == [True, False, False]
    assert mask_of("a > 0", batch) == [True, False, True]


def test_not_of_null_comparison_also_excludes_null_rows():
    """这就是"NOT 不会把 NULL 行放回来"—— 与把 NULL 当 False 的实现的关键区别。"""

    batch = batch_of(a=[1, None, 3])
    assert mask_of("NOT (a = 1)", batch) == [False, False, True]
    assert mask_of("a <> 1", batch) == [False, False, True]
    assert mask_of("a IS NULL", batch) == [False, True, False]


def test_is_null_never_returns_null():
    batch = batch_of(a=[1, None, 3])
    for sql in ("a IS NULL", "a IS NOT NULL"):
        value = evaluate(parse(sql), batch)
        assert value.valid.all(), sql


def test_comparison_with_null_literal_is_null():
    batch = batch_of(a=[1, 2])
    value = evaluate(parse("a = NULL"), batch)
    assert value.to_pylist() == [None, None]
    assert mask_of("a = NULL", batch) == [False, False]


def test_not_in_with_null_element_never_true():
    batch = batch_of(a=[1, 2, 3])
    value = evaluate(parse("a NOT IN (1, NULL)"), batch)
    assert value.to_pylist() == [False, None, None]
    assert mask_of("a NOT IN (1, NULL)", batch) == [False, False, False]


def test_in_with_null_element_can_still_be_true():
    batch = batch_of(a=[1, 2, None])
    assert mask_of("a IN (1, NULL)", batch) == [True, False, False]
    assert evaluate(parse("a IN (1, NULL)"), batch).to_pylist() == [True, None, None]


def test_null_propagates_through_arithmetic():
    batch = batch_of(a=[1, None, 3])
    value = evaluate(parse("a + 1"), batch)
    assert value.to_pylist() == [2, None, 4]
    assert value.valid.tolist() == [True, False, True]


def test_null_propagates_through_string_concat():
    batch = batch_of(s=["a", None, "c"], t=["1", "2", None])
    value = evaluate(parse("s || t"), batch)
    assert value.to_pylist() == ["a1", None, None]


def test_division_by_zero_is_null_not_error():
    batch = batch_of(a=[6, 0, -6], b=[3, 0, 2])
    assert evaluate(parse("a / b"), batch).to_pylist() == [2, None, -3]
    assert evaluate(parse("a % b"), batch).to_pylist() == [0, None, 0]
    # 浮点除零同样是 NULL，而不是 inf
    f = batch_of(f=[1.0, 1.0], z=[2.0, 0.0])
    assert evaluate(parse("f / z"), f).to_pylist() == [0.5, None]


def test_integer_division_truncates_toward_zero():
    batch = batch_of(a=[7, -7, 7, -7], b=[2, 2, -2, -2])
    assert evaluate(parse("a / b"), batch).to_pylist() == [3, -3, -3, 3]


def test_modulo_sign_follows_dividend():
    batch = batch_of(a=[7, -7, 7, -7], b=[3, 3, -3, -3])
    assert evaluate(parse("a % b"), batch).to_pylist() == [1, -1, 1, -1]


def test_float_division_when_either_side_is_float():
    batch = batch_of(a=[7], f=[2.0])
    value = evaluate(parse("a / f"), batch)
    assert value.to_pylist() == [3.5]
    assert value.values.dtype.kind == "f"


def test_nan_comparison_is_false():
    batch = batch_of(f=[float("nan"), 1.0, None])
    for sql in ("f = f", "f > 1.0", "f < 1.0", "f != 1.0", "f = 1.0"):
        value = evaluate(parse(sql), batch)
        assert value.valid.tolist()[0] is False, sql
        assert mask_of(sql, batch)[0] is False, sql


def test_nan_does_not_leak_into_other_rows():
    batch = batch_of(f=[float("nan"), 5.0, None])
    assert mask_of("f > 1.0", batch) == [False, True, False]
    assert mask_of("f IS NULL", batch) == [False, False, True]


def test_null_vector_rows_vs_nan_vector_rows():
    """空向量行（整列为 NULL）与含 NaN 的行是两回事，位图和值都不同。"""

    vec = FixedSizeList(FLOAT64, 2)
    batch = RecordBatch(
        Schema([Field("v", vec), Field("d", FLOAT64)]),
        {
            "v": FixedSizeListArray.from_pylist(vec, [[1.0, 2.0], None, [3.0, 4.0]]),
            "d": PrimitiveArray.from_pylist(FLOAT64, [1.0, float("nan"), None]),
        },
    )
    null_row = evaluate(parse("v"), batch).to_pylist()
    assert null_row[1] is None and null_row[0] == [1.0, 2.0]
    # 含 NaN 的行：值存在，但任何比较都是 NULL
    assert evaluate(parse("d > 0"), batch).to_pylist() == [True, None, None]
    assert evaluate(parse("d IS NULL"), batch).to_pylist() == [False, False, True]


def test_numeric_and_boolean_three_valued_combination():
    batch = batch_of(a=[1, None, 3], b=[True, True, None])
    assert evaluate(parse("a > 0 AND b"), batch).to_pylist() == [True, None, None]
    assert evaluate(parse("a > 0 OR b"), batch).to_pylist() == [True, None, True]


def test_constant_folding_of_null_and():
    # NULL AND FALSE 是 FALSE（FALSE 是吸收元），不是 NULL
    assert evaluate(parse("NULL AND FALSE"), batch_of(a=[1])).to_pylist() == [False]
    assert evaluate(parse("NULL OR TRUE"), batch_of(a=[1])).to_pylist() == [True]
    assert evaluate(parse("NULL AND TRUE"), batch_of(a=[1])).to_pylist() == [None]


# ===========================================================================
# 7. 向量化 vs 慢速参考实现
# ===========================================================================

REFERENCE_PREDICATES = [
    "a > 3",
    "a = 5",
    "a != 5",
    "a <= 2 OR a >= 7",
    "a > 2 AND a < 8",
    "NOT (a = 3)",
    "a IS NULL",
    "a IS NOT NULL",
    "f > 1.5",
    "f <= 2.0",
    "f != 0.0",
    "s LIKE 'a%'",
    "s LIKE '%e%'",
    "s LIKE 'a_'",
    "s NOT LIKE 'a%'",
    "s = 'apple'",
    "s IS NULL OR s = ''",
    "a IN (1, 3, 5)",
    "a NOT IN (1, 3, 5)",
    "a IN (1, NULL, 9)",
    "a NOT IN (1, NULL)",
    "a BETWEEN 2 AND 6",
    "a NOT BETWEEN 2 AND 6",
    "a + 1 > 4",
    "a % 3 = 1",
    "a / 2 = 1",
    "f IS NOT NULL AND f < 5",
    "a > 1 AND (s LIKE '%a%' OR f < 2.0)",
    "NOT (a > 1 OR s LIKE 'a%')",
    "a > 1 AND f > 0.0 AND s IS NOT NULL",
]


@pytest.mark.parametrize("sql", REFERENCE_PREDICATES)
def test_vectorized_matches_reference(sql):
    columns = random_columns()
    n = len(columns["a"])
    batch = batch_of(**columns)
    expr = parse(sql)
    reference = ref_eval(expr, columns, n)

    value = evaluate(expr, batch)
    assert value.valid.tolist() == [r is not None for r in reference], sql
    assert evaluate_predicate(expr, batch).tolist() == [
        r is True for r in reference
    ], sql


@pytest.mark.parametrize(
    "sql",
    ["a + 1", "a - 3", "a * 2", "a % 4", "a / 3", "f + 1.5", "f * -2.0",
     "a || 'x'", "-a", "f / 2.0"],
)
def test_vectorized_values_match_reference(sql):
    columns = random_columns()
    n = len(columns["a"])
    batch = batch_of(**columns)
    expr = parse(sql)
    reference = ref_eval(expr, columns, n)
    value = evaluate(expr, batch)
    got = value.to_pylist()
    for i, (g, r) in enumerate(zip(got, reference)):
        if r is None:
            assert g is None, (sql, i, g, r)
        elif isinstance(r, float) and math.isnan(r):
            # NaN 是"有值但特殊"的行：两边都必须是 NaN（不是 NULL）
            assert g is not None and math.isnan(float(g)), (sql, i, g, r)
        elif isinstance(r, float):
            assert abs(float(g) - r) < 1e-9, (sql, i, g, r)
        else:
            assert g == r, (sql, i, g, r)


# ===========================================================================
# 8. LIKE
# ===========================================================================


def test_like_to_regex_basics():
    rx = like_to_regex("a%c")
    assert rx.match("abc") and rx.match("ac") and rx.match("aXXc")
    assert not rx.match("ab")
    assert not rx.match("xabc")


def test_like_to_regex_underscore_matches_exactly_one():
    rx = like_to_regex("a_c")
    assert rx.match("abc") and rx.match("a c")
    assert not rx.match("ac") and not rx.match("abbc")


def test_like_to_regex_anchored_both_ends():
    rx = like_to_regex("ab")
    assert rx.match("ab") and not rx.match("abc") and not rx.match("xab")


def test_like_to_regex_escapes_regex_metachars():
    rx = like_to_regex("a.c+")
    assert rx.match("a.c+")
    assert not rx.match("abc+")
    assert not rx.match("aXcX")


def test_like_to_regex_dotall_matches_newline():
    # Python 的 '.' 默认不匹配换行；LIKE 的 % 必须匹配
    assert like_to_regex("a%c").match("a\nc")
    assert like_to_regex("a_c").match("a\nc")
    assert not like_to_regex("a_c").match("a\n\nc")


def test_like_escape_makes_wildcards_literal():
    rx = like_to_regex(r"a\%b", "\\")
    assert rx.match("a%b")
    assert not rx.match("aXb")
    matcher = compile_like(r"a\%b", "\\")
    assert matcher.kind == "eq"
    assert matcher.match(np.array(["a%b", "aXb"], dtype=object)).tolist() == [True, False]


def test_like_escape_of_underscore():
    matcher = compile_like(r"a\_c", "\\")
    assert matcher.kind == "eq"
    assert matcher.match(np.array(["a_c", "abc"], dtype=object)).tolist() == [True, False]


def test_like_fastpath_kinds():
    assert compile_like("abc").kind == "eq"
    assert compile_like("abc%").kind == "prefix"
    assert compile_like("%abc").kind == "suffix"
    assert compile_like("%abc%").kind == "contains"
    assert compile_like("a%b").kind == "prefix_suffix"
    assert compile_like("a%b%c").kind == "regex"
    assert compile_like("a_c").kind == "regex"
    assert compile_like("%").kind == "prefix_suffix"


def test_like_fastpath_equals_regex_path():
    """所有快速路径的结果必须和正则路径**逐位相同**。"""

    matchers = [
        compile_like("abc"),
        compile_like("abc%"),
        compile_like("%abc"),
        compile_like("%abc%"),
        compile_like("a%b"),
        compile_like("a_c"),
        compile_like("%"),
        compile_like("a%_b"),
        compile_like(""),
    ]
    values = np.array(
        ["abc", "abcdef", "xabc", "xabcx", "", "a", "ab", "aXb", "a123b", "axb"],
        dtype=object,
    )
    for matcher in matchers:
        fast = matcher.match(values)
        slow = np.array([bool(matcher.regex.match(v)) for v in values])
        assert fast.tolist() == slow.tolist(), matcher.pattern


def test_like_fastpath_agrees_with_independent_matcher():
    """再用一个"手写通配匹配器"对一次拍，避免两个路径共享同一个 bug。"""

    rng = random.Random(4242)
    alphabet = "ab"
    for _ in range(200):
        pattern = "".join(
            rng.choice(list(alphabet) + ["%", "_"]) for _ in range(rng.randint(1, 5))
        )
        values = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5)))
            for _ in range(12)
        ]
        toks = _split_pattern(pattern, None)
        expected = [_glob_match(toks, v) for v in values]
        matcher = compile_like(pattern)
        got = matcher.match(np.array(values, dtype=object)).tolist()
        assert got == expected, f"{pattern!r} vs {values}"


def test_like_with_escape_fastpath_agrees_with_matcher():
    rng = random.Random(7)
    alphabet = "ab"
    for _ in range(120):
        pattern = "".join(
            rng.choice(list(alphabet) + ["%", "_", "\\"]) for _ in range(rng.randint(1, 5))
        )
        if pattern.endswith("\\"):
            continue
        values = [
            "".join(rng.choice(alphabet + "%_") for _ in range(rng.randint(0, 4)))
            for _ in range(10)
        ]
        toks = _split_pattern(pattern, "\\")
        expected = [_glob_match(toks, v) for v in values]
        matcher = compile_like(pattern, "\\")
        got = matcher.match(np.array(values, dtype=object)).tolist()
        assert got == expected, f"{pattern!r} vs {values}"


def test_like_on_null_column_is_null():
    batch = batch_of(s=["apple", None, "banana"])
    assert evaluate(parse("s LIKE 'a%'"), batch).to_pylist() == [True, None, False]
    assert evaluate(parse("s NOT LIKE 'a%'"), batch).to_pylist() == [False, None, True]


def test_like_is_case_sensitive_by_default():
    batch = batch_of(s=["Apple", "apple"])
    assert mask_of("s LIKE 'a%'", batch) == [False, True]


def test_like_on_non_string_column_raises():
    with pytest.raises(PlanError):
        evaluate(parse("a LIKE 'x%'"), batch_of(a=[1, 2]))


def test_like_with_dynamic_pattern():
    batch = batch_of(s=["apple", "banana"], p=["a%", "b%"])
    assert mask_of("s LIKE p", batch) == [True, True]
    batch2 = batch_of(s=["apple", "banana"], p=["a%", "x%"])
    assert mask_of("s LIKE p", batch2) == [True, False]


def test_like_single_row_never_crashes_on_special_values():
    batch = batch_of(s=[""])
    assert mask_of("s LIKE '%'", batch) == [True]
    assert mask_of("s LIKE ''", batch) == [True]
    assert mask_of("s LIKE '_'", batch) == [False]


# ===========================================================================
# 9. 标量函数
# ===========================================================================


def test_string_functions():
    batch = batch_of(s=["  Apple ", "banana", None])
    assert evaluate(parse("lower(s)"), batch).to_pylist() == ["  apple ", "banana", None]
    assert evaluate(parse("upper(s)"), batch).to_pylist() == ["  APPLE ", "BANANA", None]
    assert evaluate(parse("trim(s)"), batch).to_pylist() == ["Apple", "banana", None]
    assert evaluate(parse("ltrim(s)"), batch).to_pylist() == ["Apple ", "banana", None]
    assert evaluate(parse("rtrim(s)"), batch).to_pylist() == ["  Apple", "banana", None]
    assert evaluate(parse("length(s)"), batch).to_pylist() == [8, 6, None]


def test_numeric_functions():
    batch = batch_of(f=[-1.5, 2.25, None], a=[-3, 4, None])
    assert evaluate(parse("abs(f)"), batch).to_pylist() == [1.5, 2.25, None]
    assert evaluate(parse("abs(a)"), batch).to_pylist() == [3, 4, None]
    assert evaluate(parse("floor(f)"), batch).to_pylist() == [-2.0, 2.0, None]
    assert evaluate(parse("ceil(f)"), batch).to_pylist() == [-1.0, 3.0, None]
    assert evaluate(parse("round(f)"), batch).to_pylist() == [-2.0, 2.0, None]
    assert evaluate(parse("round(f, 1)"), batch).to_pylist() == [-1.5, 2.2, None]


def test_function_null_propagation():
    batch = batch_of(s=["ab", None], t=["cd", "ef"])
    for sql in ("lower(s)", "upper(s)", "length(s)", "starts_with(s, 'a')",
                "ends_with(s, 'b')", "contains(s, 'a')", "concat(s, t)"):
        value = evaluate(parse(sql), batch)
        assert value.valid.tolist() == [True, False], sql
        assert value.to_pylist()[1] is None, sql


def test_boolean_string_functions():
    batch = batch_of(s=["apple", "banana", "", None])
    assert mask_of("starts_with(s, 'app')", batch) == [True, False, False, False]
    assert mask_of("ends_with(s, 'na')", batch) == [False, True, False, False]
    assert mask_of("contains(s, 'nan')", batch) == [False, True, False, False]


def test_coalesce_removes_null():
    batch = batch_of(a=[1, None, 3], b=[9, 8, None])
    assert evaluate(parse("coalesce(a, b, 0)"), batch).to_pylist() == [1, 8, 3]
    assert evaluate(parse("coalesce(a, b)"), batch).valid.tolist() == [True, True, True]


def test_nullif_introduces_null():
    batch = batch_of(a=[1, 2, 3])
    assert evaluate(parse("nullif(a, 2)"), batch).to_pylist() == [1, None, 3]


def test_array_functions():
    lst = ListType(INT64)
    batch = RecordBatch(
        Schema([Field("l", lst)]),
        {"l": ListArray.from_pylist(lst, [[1, 2], [3], None, []])},
    )
    assert mask_of("array_contains(l, 1)", batch) == [True, False, False, False]
    assert evaluate(parse("array_contains(l, 1)"), batch).to_pylist() == [
        True, False, None, False,
    ]
    assert mask_of("array_has_any(l, [2, 3])", batch) == [True, True, False, False]
    assert mask_of("array_has_all(l, [1, 2])", batch) == [True, False, False, False]
    assert evaluate(parse("length(l)"), batch).to_pylist() == [2, 1, None, 0]
    assert mask_of("array_contains(l, 9)", batch) == [False, False, False, False]


def test_array_functions_on_vector_column():
    vec = FixedSizeList(FLOAT64, 3)
    batch = RecordBatch(
        Schema([Field("v", vec)]),
        {"v": FixedSizeListArray.from_pylist(vec, [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])},
    )
    assert mask_of("array_contains(v, 5.0)", batch) == [False, True]
    assert evaluate(parse("length(v)"), batch).to_pylist() == [3, 3]


def test_cast_functions():
    batch = batch_of(a=[1, 2, None], s=["3", "x", "5"])
    assert evaluate(parse("cast(a as double)"), batch).to_pylist() == [1.0, 2.0, None]
    assert evaluate(parse("cast(s as int64)"), batch).to_pylist() == [3, None, 5]
    assert evaluate(parse("cast(a as utf8)"), batch).to_pylist() == ["1", "2", None]
    assert evaluate(parse("cast(a as bool)"), batch).to_pylist() == [True, True, None]


def test_cast_bad_rows_become_null_not_error():
    batch = batch_of(s=["1", "not-a-number", "", None])
    value = evaluate(parse("cast(s as int64)"), batch)
    assert value.to_pylist() == [1, None, None, None]


# ===========================================================================
# 10. 各种 Array 子类上的求值
# ===========================================================================


def test_evaluate_on_int_column():
    value = evaluate(parse("a"), batch_of(a=[1, 2, 3]))
    assert value.values.dtype == np.int64
    assert value.valid.all()


def test_evaluate_on_float_column_with_nan():
    value = evaluate(parse("f"), batch_of(f=[1.0, float("nan"), None]))
    assert value.values.dtype == np.float64
    assert value.valid.tolist() == [True, True, False]


def test_evaluate_on_string_column():
    value = evaluate(parse("s"), batch_of(s=["a", None, "c"]))
    assert value.to_pylist() == ["a", None, "c"]
    assert value.type == UTF8


def test_evaluate_on_bool_column():
    value = evaluate(parse("flag"), batch_of(flag=[True, None, False]))
    assert value.values.dtype == bool
    assert value.to_pylist() == [True, None, False]


def test_evaluate_on_list_column():
    lst = ListType(UTF8)
    batch = RecordBatch(
        Schema([Field("l", lst)]),
        {"l": ListArray.from_pylist(lst, [["a"], ["b", "c"], None])},
    )
    value = evaluate(parse("l"), batch)
    assert value.to_pylist() == [["a"], ["b", "c"], None]


def test_evaluate_on_fixed_size_list_column():
    vec = FixedSizeList(FLOAT32, 2)
    batch = RecordBatch(
        Schema([Field("v", vec)]),
        {"v": FixedSizeListArray.from_pylist(vec, [[1.0, 2.0], None])},
    )
    value = evaluate(parse("v"), batch)
    assert value.to_pylist() == [[1.0, 2.0], None]
    assert value.valid.tolist() == [True, False]


def test_evaluate_on_struct_column():
    st = StructType([Field("x", INT64), Field("y", UTF8)])
    batch = RecordBatch(
        Schema([Field("st", st)]),
        {
            "st": StructArray.from_pylist(
                st, [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}, None]
            )
        },
    )
    value = evaluate(parse("st"), batch)
    assert value.to_pylist()[0]["x"] == 1
    assert value.valid.tolist() == [True, True, False]
    assert evaluate(parse("struct_field(st, 'x')"), batch).to_pylist() == [1, 2, None]


def test_evaluate_on_entirely_null_column():
    batch = RecordBatch(
        Schema([Field("n", NULL)]), {"n": NullArray(3)}
    )
    value = evaluate(parse("n"), batch)
    assert value.valid.tolist() == [False, False, False]
    assert value.to_pylist() == [None, None, None]
    # 全 null 列上的任何比较都是 NULL，IS NULL 则恒为真
    assert mask_of("n = 1", batch) == [False, False, False]
    assert mask_of("n IS NULL", batch) == [True, True, True]
    assert mask_of("n IS NOT NULL", batch) == [False, False, False]


def test_evaluate_on_null_column_arithmetic():
    batch = RecordBatch(Schema([Field("n", NULL)]), {"n": NullArray(2)})
    value = evaluate(parse("n + 1"), batch)
    assert value.to_pylist() == [None, None]


def test_evaluate_returns_ndarray_subclass():
    value = evaluate(parse("a + 1"), batch_of(a=[1, 2]))
    assert isinstance(value, np.ndarray)
    assert np.asarray(value).tolist() == [2, 3]
    assert len(value) == 2


def test_value_array_to_arrow_array_roundtrip():
    batch = batch_of(a=[1, None], s=["x", None])
    assert evaluate(parse("a"), batch).to_array().to_pylist() == [1, None]
    assert evaluate(parse("s"), batch).to_array().to_pylist() == ["x", None]
    assert evaluate(parse("upper(s)"), batch).to_array().to_pylist() == ["X", None]


def test_evaluate_predicate_requires_boolean():
    with pytest.raises(QueryError):
        evaluate_predicate(parse("a + 1"), batch_of(a=[1]))
    # 布尔列裸写可以
    assert evaluate_predicate(parse("flag"), batch_of(flag=[True, False])).tolist() == [
        True,
        False,
    ]


def test_evaluate_missing_column_raises_plan_error():
    with pytest.raises(PlanError) as ei:
        evaluate(parse("nope"), batch_of(a=[1]))
    assert "nope" in str(ei.value)


def test_empty_batch_evaluates_to_empty_mask():
    batch = RecordBatch.from_pydict({"a": []}, Schema([Field("a", INT64)]))
    assert evaluate_predicate(parse("a > 1"), batch).tolist() == []


# ===========================================================================
# 11. 常量折叠
# ===========================================================================


def test_fold_constants_arithmetic():
    assert fold_constants(parse("1 + 2")).value == 3
    assert fold_constants(parse("1 + 2 * 3")).value == 7
    assert fold_constants(parse("a > 1 + 2")).to_sql() == "a > 3"
    assert fold_constants(parse("3 * (2 + 1)")).value == 9


def test_fold_constants_logic_and_null():
    assert fold_constants(parse("TRUE AND FALSE")).value is False
    assert fold_constants(parse("NOT TRUE")).value is False
    assert fold_constants(parse("1 = 2")).value is False
    # 整棵子树都是常量时会整体塌缩，包括三值逻辑的结果
    assert fold_constants(parse("NULL AND TRUE")).value is None
    assert fold_constants(parse("NULL AND FALSE")).value is False
    assert fold_constants(parse("NULL OR TRUE")).value is True
    assert fold_constants(parse("NULL OR FALSE")).value is None
    assert fold_constants(parse("1 > 0 AND NULL")).value is None
    # 有列引用就不折叠
    assert fold_constants(parse("a AND NULL")).to_sql() == "a AND NULL"


def test_fold_constants_functions():
    assert fold_constants(parse("upper('ab')")).value == "AB"
    assert fold_constants(parse("abs(-3)")).value == 3
    assert fold_constants(parse("length('abcd')")).value == 4


def test_fold_constants_leaves_column_expressions_alone():
    node = parse("a + 1")
    assert fold_constants(node).equals(node)
    node = parse("a > 1 AND b > 2")
    assert fold_constants(node).equals(node)


def test_fold_constants_partial():
    folded = fold_constants(parse("a > 1 + 1 AND b = 2"))
    assert folded.to_sql() == "a > 2 AND b = 2"


def test_fold_constants_is_idempotent():
    node = parse("a > 1 + 2 AND length('xy') = 2")
    once = fold_constants(node)
    assert fold_constants(once).equals(once)


def test_fold_constants_null_like():
    assert fold_constants(parse("NULL IS NULL")).value is True
    assert fold_constants(parse("NULL = NULL")).value is None


# ===========================================================================
# 12. 化简
# ===========================================================================


def test_simplify_identity_rules():
    assert simplify(parse("a = 1 AND TRUE")).to_sql() == "a = 1"
    assert simplify(parse("TRUE AND a = 1")).to_sql() == "a = 1"
    assert simplify(parse("a = 1 AND FALSE")).to_sql() == "FALSE"
    assert simplify(parse("a = 1 OR FALSE")).to_sql() == "a = 1"
    assert simplify(parse("a = 1 OR TRUE")).to_sql() == "TRUE"
    assert simplify(parse("FALSE OR FALSE")).to_sql() == "FALSE"


def test_simplify_double_negation():
    assert simplify(parse("NOT NOT (a = 1)")).to_sql() == "a = 1"
    assert simplify(parse("NOT TRUE")).to_sql() == "FALSE"
    assert simplify(parse("NOT FALSE")).to_sql() == "TRUE"
    assert simplify(parse("NOT NULL")).to_sql() == "NULL"


def test_simplify_single_element_in_becomes_equality():
    assert simplify(parse("a IN (5)")).to_sql() == "a = 5"
    assert simplify(parse("a NOT IN (5)")).to_sql() == "a != 5"
    # 多变元不动
    assert simplify(parse("a IN (5, 6)")).to_sql() == "a IN (5, 6)"


def test_simplify_dedupes_conjuncts():
    assert simplify(parse("a = 1 AND a = 1")).to_sql() == "a = 1"
    assert simplify(parse("a = 1 OR a = 1")).to_sql() == "a = 1"


def test_simplify_flattens_and_keeps_disjuncts():
    assert simplify(parse("(a = 1 AND b = 2) AND TRUE")).to_sql() == "a = 1 AND b = 2"
    assert simplify(parse("a = 1 AND b = 2 AND c = 3")).to_sql() == "a = 1 AND b = 2 AND c = 3"


def test_simplify_unlocks_index_rewrite():
    # 化简的意义：``a > 1 + 2`` 本来翻译不出索引条件，化简之后可以
    raw = parse("a > 1 + 2")
    assert to_index_expr(raw, "a") is None
    assert to_index_expr(simplify(raw), "a") == RangeQuery(3, None, False, True)


def test_simplify_is_idempotent():
    for text in ["a = 1 AND TRUE", "a IN (5)", "NOT NOT (a = 1)",
                 "a = 1 AND a = 1 AND TRUE", "a > 1 + 2"]:
        once = simplify(parse(text))
        assert simplify(once).to_sql() == once.to_sql(), text


def test_simplify_cast_idempotence():
    assert simplify(parse("cast(cast(a as int64) as int64)")).to_sql() == "CAST(a AS int64)"


def test_simplify_preserves_semantics():
    batch = batch_of(a=[1, 2, None], b=[1, 2, None])
    for text in ["a = 1 AND TRUE", "a IN (5) OR FALSE", "NOT NOT (a = 1)",
                 "a = 1 OR TRUE", "(a = 1 AND b = 1) AND TRUE"]:
        before = mask_of(text, batch)
        after = evaluate_predicate(simplify(parse(text)), batch).tolist()
        assert before == after, text


# ===========================================================================
# 13. 合取/析取拆分
# ===========================================================================


def test_split_conjuncts():
    assert [e.to_sql() for e in split_conjuncts(parse("a AND b AND c"))] == ["a", "b", "c"]
    assert [e.to_sql() for e in split_conjuncts(parse("(a AND b) AND (c AND d)"))] == [
        "a", "b", "c", "d",
    ]
    assert [e.to_sql() for e in split_conjuncts(parse("a OR b"))] == ["a OR b"]
    assert [e.to_sql() for e in split_conjuncts(parse("a"))] == ["a"]
    # OR 的子树不会被拆进合取项
    assert [e.to_sql() for e in split_conjuncts(parse("a AND (b OR c)"))] == [
        "a", "b OR c",
    ]


def test_split_disjuncts():
    assert [e.to_sql() for e in split_disjuncts(parse("a OR b OR c"))] == ["a", "b", "c"]
    assert [e.to_sql() for e in split_disjuncts(parse("a AND b"))] == ["a AND b"]


# ===========================================================================
# 14. 选择率估计
# ===========================================================================


STATS = {
    "a": {"ndv": 100, "min": 0, "max": 1000, "null_frac": 0.05},
    "s": {"ndv": 10, "null_frac": 0.2},
}


def test_selectivity_bounds_and_determinism():
    for text in ["a = 1", "a IS NULL", "s LIKE '%x%'", "length(s) = 1", "a"]:
        value = estimate_selectivity(parse(text), STATS)
        assert 0.0 <= value <= 1.0
        assert value == estimate_selectivity(parse(text), STATS)


def test_selectivity_true_false_literals():
    assert estimate_selectivity(parse("TRUE")) == 1.0
    assert estimate_selectivity(parse("FALSE")) == 0.0


def test_selectivity_equality_uses_ndv():
    assert estimate_selectivity(parse("a = 1"), STATS) == pytest.approx(1 / 100)
    assert estimate_selectivity(parse("a = 1")) == 0.1


def test_selectivity_and_is_monotone_decreasing():
    base = estimate_selectivity(parse("a = 1"), STATS)
    assert estimate_selectivity(parse("a = 1 AND a > 0"), STATS) <= base
    assert estimate_selectivity(parse("a = 1 AND a > 0 AND a IS NOT NULL"), STATS) <= (
        estimate_selectivity(parse("a = 1 AND a > 0"), STATS)
    )


def test_selectivity_or_is_monotone_increasing():
    base = estimate_selectivity(parse("a = 1"), STATS)
    two = estimate_selectivity(parse("a = 1 OR a = 2"), STATS)
    three = estimate_selectivity(parse("a = 1 OR a = 2 OR a = 3"), STATS)
    assert two >= base
    assert three >= two


def test_selectivity_in_is_monotone_in_value_count():
    one = estimate_selectivity(parse("a IN (1)"), STATS)
    three = estimate_selectivity(parse("a IN (1, 2, 3)"), STATS)
    ten = estimate_selectivity(parse(f"a IN ({', '.join(str(i) for i in range(10))})"), STATS)
    assert one <= three <= ten


def test_selectivity_or_saturates_at_one():
    text = " OR ".join(f"a = {i}" for i in range(150))
    assert estimate_selectivity(parse(text), STATS) == 1.0
    # OR 的估计值被截断在 1，绝不会溢出成 1.5 这种值
    half = " OR ".join(f"a = {i}" for i in range(50))
    assert estimate_selectivity(parse(half), STATS) == pytest.approx(0.5)


def test_selectivity_like_shapes():
    prefix = estimate_selectivity(parse("s LIKE 'abc%'"), STATS)
    contains = estimate_selectivity(parse("s LIKE '%abc%'"), STATS)
    assert prefix < contains
    assert estimate_selectivity(parse("s NOT LIKE 'abc%'"), STATS) == pytest.approx(
        1 - prefix
    )


def test_selectivity_uses_min_max_to_prove_empty():
    assert estimate_selectivity(parse("a > 5000"), STATS) == 0.0
    assert estimate_selectivity(parse("a < -1"), STATS) == 0.0
    assert estimate_selectivity(parse("a > -1"), STATS) == 1.0
    assert estimate_selectivity(parse("a <= 9999"), STATS) == 1.0


def test_selectivity_negation_complements():
    eq = estimate_selectivity(parse("a = 1"), STATS)
    ne = estimate_selectivity(parse("a != 1"), STATS)
    assert eq + ne == pytest.approx(1.0)
    assert estimate_selectivity(parse("NOT (a = 1)"), STATS) == pytest.approx(ne)


def test_selectivity_null_fraction_from_stats():
    assert estimate_selectivity(parse("a IS NULL"), STATS) == pytest.approx(0.05)
    assert estimate_selectivity(parse("a IS NOT NULL"), STATS) == pytest.approx(0.95)
    assert estimate_selectivity(parse("a IS NULL")) == pytest.approx(0.1)


def test_selectivity_unknown_shapes_are_neutral():
    assert estimate_selectivity(parse("length(s) = 3"), STATS) == 0.5
    assert estimate_selectivity(parse("cast(a as utf8) = 'x'"), STATS) == 0.5


# ===========================================================================
# 15. 索引改写
# ===========================================================================


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("a = 5", RangeQuery(5, 5, True, True)),
        ("5 = a", RangeQuery(5, 5, True, True)),
        ("a != 5", IndexNot(RangeQuery(5, 5, True, True))),
        ("a < 5", RangeQuery(None, 5, True, False)),
        ("a <= 5", RangeQuery(None, 5, True, True)),
        ("a > 5", RangeQuery(5, None, False, True)),
        ("a >= 5", RangeQuery(5, None, True, True)),
        ("a IN (1, 2, 3)", IndexIsIn([1, 2, 3])),
        ("a NOT IN (1, 2)", IndexNot(IndexIsIn([1, 2]))),
        ("a BETWEEN 1 AND 9", RangeQuery(1, 9, True, True)),
        ("a NOT BETWEEN 1 AND 9", IndexNot(RangeQuery(1, 9, True, True))),
        ("a = -5", RangeQuery(-5, -5, True, True)),
        ("a > -1 AND a < 5", IndexAnd([RangeQuery(-1, None, False, True),
                                       RangeQuery(None, 5, True, False)])),
        ("a = 1 OR a = 2", IndexOr([RangeQuery(1, 1, True, True),
                                    RangeQuery(2, 2, True, True)])),
        ("NOT a = 5", IndexNot(RangeQuery(5, 5, True, True))),
        ("a IN (-1, -2)", IndexIsIn([-1, -2])),
    ],
)
def test_to_index_expr_shapes(sql, expected):
    assert to_index_expr(parse(sql), "a") == expected


def test_to_index_expr_partial_pushdown_for_and():
    # AND 允许只下推能翻译的那部分：返回的是**超集**，下游过滤补精确性
    node = to_index_expr(parse("a = 5 AND other = 1"), "a")
    assert node == RangeQuery(5, 5, True, True)


def test_to_index_expr_or_requires_all_branches():
    # OR 只要有一支翻译不了，整体就必须放弃 —— 漏一支会让结果**变少**
    assert to_index_expr(parse("a = 5 OR other = 1"), "a") is None
    assert to_index_expr(parse("a = 5 OR other = 1"), "other") is None
    assert to_index_expr(parse("a = 5 OR a = 6"), "a") is not None


def test_to_index_expr_like_prefix_becomes_range():
    node = to_index_expr(parse("a LIKE 'abc%'"), "a")
    assert isinstance(node, RangeQuery)
    assert node.start == "abc" and node.inclusive_start
    assert node.end.startswith("abc") and node.end > "abc"


def test_to_index_expr_like_without_prefix_unsupported():
    assert to_index_expr(parse("a LIKE '%abc'"), "a") is None
    assert to_index_expr(parse("a LIKE '%abc%'"), "a") is None
    assert to_index_expr(parse("a LIKE 'a_c'"), "a") is None
    assert to_index_expr(parse("a NOT LIKE 'abc%'"), "a") is None
    # 无通配符的 LIKE 退化成等值
    assert to_index_expr(parse("a LIKE 'abc'"), "a") == RangeQuery("abc", "abc", True, True)


def test_to_index_expr_returns_none_when_unanswerable():
    for sql in ["a IS NULL", "a IS NOT NULL", "a + 1 = 5", "a = b", "a = NULL",
                "1 = 1", "length(a) = 3", "a LIKE b"]:
        assert to_index_expr(parse(sql), "a") is None, sql


def test_to_index_expr_ignores_other_columns():
    assert to_index_expr(parse("b = 1"), "a") is None
    assert to_index_expr(parse("a = 1 AND b = 1"), "b") == RangeQuery(1, 1, True, True)


def test_to_index_expr_requires_column_name():
    with pytest.raises(PlanError):
        to_index_expr(parse("a = 1"), "")


# ===========================================================================
# 16. zone map 剪枝
# ===========================================================================


def test_extract_zone_map_predicate_basic():
    items = extract_zone_map_predicate(parse("a > 1 AND b <= 2"))
    assert [e.to_sql() for e in items] == ["a > 1", "b <= 2"]
    assert [e.to_sql() for e in extract_zone_map_predicate(parse("5 < a"))] == ["5 < a"]
    assert [e.to_sql() for e in extract_zone_map_predicate(parse("a IN (1, 2)"))] == [
        "a IN (1, 2)"
    ]
    assert [e.to_sql() for e in extract_zone_map_predicate(parse("a BETWEEN 1 AND 3"))] == [
        "a BETWEEN 1 AND 3"
    ]


def test_zone_map_excludes_or_functions_and_null():
    assert extract_zone_map_predicate(parse("a > 1 OR b < 2")) == []
    assert extract_zone_map_predicate(parse("lower(a) = 1")) == []
    assert extract_zone_map_predicate(parse("a = NULL")) == []
    assert extract_zone_map_predicate(parse("a IS NULL")) == []
    assert extract_zone_map_predicate(parse("a = b")) == []
    assert [e.to_sql() for e in extract_zone_map_predicate(
        parse("a = 1 AND (b = 2 OR c = 3)")
    )] == ["a = 1"]
    assert extract_zone_map_predicate(parse("a IN (1, NULL)")) == []


def test_zone_map_usable_for_all_comparison_operators():
    for op in ("=", "!=", "<", "<=", ">", ">="):
        items = extract_zone_map_predicate(parse(f"a {op} 1"))
        assert len(items) == 1, op


# ===========================================================================
# 17. 端到端：解析 → 化简 → 索引 → 求值
# ===========================================================================


def test_end_to_end_where_pipeline():
    batch = batch_of(
        a=[1, 2, 3, 4, 5, None],
        s=["apple", "banana", "avocado", None, "cherry", "ab"],
    )
    text = "a > 1 + 1 AND s LIKE 'a%'"
    expr = parse(text)
    simplified = simplify(expr)
    assert simplified.to_sql() == "a > 2 AND s LIKE 'a%'"

    index_expr = to_index_expr(simplified, "a")
    assert index_expr == RangeQuery(2, None, False, True)

    mask = evaluate_predicate(simplified, batch)
    assert mask.tolist() == [False, False, True, False, False, False]
    assert batch.filter(mask).to_pylist() == [
        {"a": 3, "s": "avocado"},
    ]


def test_end_to_end_null_safety_of_not():
    batch = batch_of(a=[1, 2, None])
    # 这两条在"NULL 当作 False"的实现里会给出不同答案，在本实现里必须一致
    assert mask_of("NOT (a = 1)", batch) == mask_of("a <> 1", batch)
    assert mask_of("NOT (a = 1)", batch) == [False, True, False]


def test_end_to_end_index_mask_matches_bruteforce():
    batch = batch_of(a=[1, 2, 3, 4, 5])
    expr = simplify(parse("a > 2 AND a < 5"))
    index_expr = to_index_expr(expr, "a")
    assert index_expr == IndexAnd(
        [RangeQuery(2, None, False, True), RangeQuery(None, 5, True, False)]
    )
    assert mask_of("a > 2 AND a < 5", batch) == [False, False, True, True, False]
