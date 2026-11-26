from __future__ import annotations

import pytest

from mini_prometheus.model import ParseError
from mini_prometheus.promql import ast
from mini_prometheus.promql.parser import parse_expr


def test_number_literal():
    expr = parse_expr("42")
    assert isinstance(expr, ast.NumberLiteral)
    assert expr.value == 42
    assert expr.value_type == "scalar"


def test_float_and_scientific_literal():
    assert parse_expr("1.5").value == 1.5
    assert parse_expr("1e3").value == 1000
    assert parse_expr(".5").value == 0.5
    assert parse_expr("0x10").value == 16


def test_string_literal():
    expr = parse_expr('"hello"')
    assert isinstance(expr, ast.StringLiteral)
    assert expr.value == "hello"
    assert expr.value_type == "string"


def test_raw_string_literal():
    assert parse_expr("`hello\\nworld`").value == "hello\\nworld"


def test_metric_selector():
    expr = parse_expr("http_requests_total")
    assert isinstance(expr, ast.VectorSelector)
    assert expr.name == "http_requests_total"
    assert expr.matchers[0].value == "http_requests_total"


def test_selector_with_matchers():
    expr = parse_expr('http_requests_total{job="api",code=~"5.."}')
    matchers = {matcher.name: matcher for matcher in expr.matchers}
    assert len(expr.matchers) == 3
    assert matchers["job"].value == "api"
    assert matchers["code"].op == "=~"
    assert matchers["__name__"].value == "http_requests_total"


def test_selector_without_metric_name():
    expr = parse_expr('{job="api"}')
    assert isinstance(expr, ast.VectorSelector)
    assert expr.name == ""


def test_quoted_metric_name():
    expr = parse_expr('{"http.requests.total",job="api"}')
    assert expr.name == "http.requests.total"
    assert {matcher.name for matcher in expr.matchers} == {"__name__", "job"}


def test_empty_selector_rejected():
    with pytest.raises(ParseError):
        parse_expr("{}")


def test_selector_matching_empty_rejected():
    with pytest.raises(ParseError):
        parse_expr('{job=~".*"}')


def test_negative_matcher_alone_rejected():
    with pytest.raises(ParseError):
        parse_expr('{job!="api"}')


def test_metric_name_conflict_rejected():
    with pytest.raises(ParseError):
        parse_expr('foo{__name__="bar"}')


def test_range_selector_type():
    expr = parse_expr("foo[5m]")
    assert isinstance(expr, ast.MatrixSelector)
    assert expr.range_ms == 300_000
    assert expr.value_type == "matrix"


def test_range_on_expression_rejected():
    with pytest.raises(ParseError):
        parse_expr("(foo)[5m]")


def test_subquery():
    expr = parse_expr("rate(foo[5m])[1h:5m]")
    assert isinstance(expr, ast.SubqueryExpr)
    assert expr.range_ms == 3_600_000
    assert expr.step_ms == 300_000


def test_subquery_default_step():
    expr = parse_expr("foo[1h:]")
    assert expr.step_ms is None


def test_subquery_step_larger_than_range_rejected():
    with pytest.raises(ParseError):
        parse_expr("foo[1m:5m]")


def test_offset_and_at():
    expr = parse_expr("foo offset 5m @ 1609746000")
    assert isinstance(expr, ast.VectorSelector)
    assert expr.offset_ms == 300_000
    assert expr.at.kind == "timestamp"
    assert expr.at.timestamp == 1_609_746_000_000


def test_negative_offset():
    expr = parse_expr("foo offset -5m")
    assert expr.offset_ms == -300_000


def test_at_start_and_end():
    assert parse_expr("foo @ start()").at.kind == "start"
    assert parse_expr("foo @ end()").at.kind == "end"


def test_aggregation_suffix_and_prefix():
    prefix = parse_expr("sum by (job) (rate(foo[5m]))")
    suffix = parse_expr("sum(rate(foo[5m])) by (job)")
    assert isinstance(prefix, ast.AggregateExpr)
    assert prefix.grouping == ["job"]
    assert suffix.grouping == ["job"]


def test_aggregation_without():
    expr = parse_expr("sum without (instance, pod) (up)")
    assert expr.without is True
    assert expr.grouping == ["instance", "pod"]


def test_aggregation_duplicate_grouping_rejected():
    with pytest.raises(ParseError):
        parse_expr("sum by (job, job) (up)")


def test_aggregation_param():
    expr = parse_expr("topk(3, up)")
    assert isinstance(expr.param, ast.NumberLiteral)
    assert expr.param.value == 3


def test_aggregation_param_type_checked():
    with pytest.raises(ParseError):
        parse_expr('topk("three", up)')


def test_count_values_param_type():
    assert parse_expr('count_values("code", up)').param.value == "code"
    with pytest.raises(ParseError):
        parse_expr("count_values(1, up)")


def test_binary_precedence():
    expr = parse_expr("1 + 2 * 3 ^ 2")
    assert expr.op == "+"
    assert expr.rhs.op == "*"
    assert expr.rhs.rhs.op == "^"


def test_power_is_right_associative():
    expr = parse_expr("2 ^ 3 ^ 2")
    assert expr.op == "^"
    assert expr.rhs.op == "^"


def test_unary_binds_looser_than_power():
    expr = parse_expr("-2^2")
    assert isinstance(expr, ast.UnaryExpr)
    assert expr.expr.op == "^"


def test_comparison_modifiers():
    expr = parse_expr("a > bool on(job) group_left(version) b")
    assert expr.op == ">"
    assert expr.bool_modifier is True
    assert expr.matching.on is True
    assert expr.matching.labels == ["job"]
    assert expr.matching.card == "many-to-one"
    assert expr.matching.include == ["version"]


def test_ignoring():
    expr = parse_expr("a / ignoring(instance) b")
    assert expr.matching.on is False
    assert expr.matching.labels == ["instance"]


def test_scalar_comparison_requires_bool():
    with pytest.raises(ParseError):
        parse_expr("1 > 2")
    assert parse_expr("1 > bool 2").value_type == "scalar"


def test_set_operator_group_modifier_rejected():
    with pytest.raises(ParseError):
        parse_expr("a and on(job) group_left b")


def test_set_operator_requires_vectors():
    with pytest.raises(ParseError):
        parse_expr("1 and 2")


def test_group_modifier_requires_two_vectors():
    with pytest.raises(ParseError):
        parse_expr("a / on(job) group_left 2")


def test_bool_only_for_comparison():
    with pytest.raises(ParseError):
        parse_expr("a + bool b")


def test_on_and_group_same_label_rejected():
    with pytest.raises(ParseError):
        parse_expr("a / on(job) group_left(job) b")


def test_matrix_binary_operand_rejected():
    with pytest.raises(ParseError):
        parse_expr("foo[5m] + 1")


def test_unary_on_matrix_rejected():
    with pytest.raises(ParseError):
        parse_expr("-foo[5m]")


def test_offset_on_binary_rejected():
    with pytest.raises(ParseError):
        parse_expr("(foo + bar) offset 5m")


def test_unknown_function_rejected():
    with pytest.raises(ParseError):
        parse_expr("nonsense(foo)")


def test_function_arity_checked():
    with pytest.raises(ParseError):
        parse_expr("rate(foo)")
    with pytest.raises(ParseError):
        parse_expr("rate(foo[5m], 1)")


def test_function_type_checked():
    with pytest.raises(ParseError):
        parse_expr("rate(foo)")
    with pytest.raises(ParseError):
        parse_expr('label_replace(up, "a")')


def test_unknown_label_matcher_operator():
    with pytest.raises(ParseError):
        parse_expr('foo{job~"a"}')


def test_empty_query_rejected():
    with pytest.raises(ParseError):
        parse_expr("   ")


def test_trailing_tokens_rejected():
    with pytest.raises(ParseError):
        parse_expr("up down")


def test_comments_are_ignored():
    expr = parse_expr("# leading\nup # trailing\n")
    assert isinstance(expr, ast.VectorSelector)


def test_function_call_type():
    assert parse_expr("time()").value_type == "scalar"
    assert parse_expr("vector(1)").value_type == "vector"
    assert parse_expr("scalar(up)").value_type == "scalar"
    assert parse_expr("rate(foo[5m])").value_type == "vector"


def test_subquery_requires_instant_vector():
    with pytest.raises(ParseError):
        parse_expr("foo[5m][1h:]")


def test_string_in_binary_rejected():
    with pytest.raises(ParseError):
        parse_expr('"a" + "b"')
