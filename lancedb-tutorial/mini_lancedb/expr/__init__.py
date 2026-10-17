"""``mini_lancedb.expr`` —— 表达式子系统。

四个模块各管一段，边界刻意划得很清楚：

============================  ===============================================
模块                           职责
============================  ===============================================
:mod:`~mini_lancedb.expr.nodes`   AST + 类型推导 + SQL 渲染 + 序列化
:mod:`~mini_lancedb.expr.parser`  分词器 + 递归下降解析（含精确报错位置）
:mod:`~mini_lancedb.expr.eval`    向量化求值 + 三值逻辑 + LIKE→正则 + 常量折叠
:mod:`~mini_lancedb.expr.rewrite` 索引改写 / zone map 提取 / 化简 / 选择率估计
============================  ===============================================

数据流是**单向**的，没有回边::

    "a > 1 AND name LIKE 'x%'"
        │  parser.parse_predicate(sql, schema)
        ▼
      Expr 树 ──rewrite.simplify──▶ 更短的 Expr 树
        │                              │
        │                              ├─rewrite.to_index_expr──▶ ScalarIndexExpr（索引）
        │                              ├─rewrite.extract_zone_map_predicate──▶ min/max 剪枝
        │                              └─ 剩下的留给 eval
        ▼
    eval.evaluate_predicate(expr, batch) ──▶ np.ndarray[bool]

最常见的两个入口::

    from mini_lancedb.expr import parse, parse_predicate, evaluate_predicate

    expr = parse_predicate("a > 1 AND b LIKE 'x%'", schema)
    mask = evaluate_predicate(expr, batch)          # numpy 布尔掩码
    batch.filter(mask)                              # 直接喂给 RecordBatch

只写 ``parse(sql)``（不带 schema）也能用：列名原样保留、类型标成
``null``（未知），求值时按 batch 里的实际列类型走。带 schema 解析的
好处是**列名写错立刻报错**，而不是等到扫到数据才发现。
"""

from __future__ import annotations

from ..schema import Schema
from .eval import (
    LikeMatcher,
    ValueArray,
    array_to_value,
    compile_like,
    evaluate,
    evaluate_predicate,
    fold_constants,
    like_to_regex,
)
from .nodes import (
    ARITHMETIC_OPS,
    BINARY_OPS,
    COMPARISON_OPS,
    KEYWORD_OPS,
    LOGICAL_OPS,
    STRING_OPS,
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
    deserialize,
    lit,
    literal_value,
    not_,
    or_,
    serialize,
    type_to_sql,
)
from .parser import parse, parse_predicate, parse_type_name, tokenize
from .rewrite import (
    estimate_selectivity,
    extract_zone_map_predicate,
    simplify,
    split_conjuncts,
    split_disjuncts,
    to_index_expr,
)

__all__ = [
    # AST
    "Expr",
    "ColumnRef",
    "Literal",
    "BinaryOp",
    "UnaryOp",
    "IsNull",
    "IsIn",
    "Like",
    "Between",
    "Cast",
    "FunctionCall",
    "col",
    "lit",
    "and_",
    "or_",
    "not_",
    "literal_value",
    "serialize",
    "deserialize",
    "type_to_sql",
    "COMPARISON_OPS",
    "LOGICAL_OPS",
    "ARITHMETIC_OPS",
    "STRING_OPS",
    "KEYWORD_OPS",
    "BINARY_OPS",
    # 解析
    "parse",
    "parse_where",
    "parse_predicate",
    "parse_type_name",
    "tokenize",
    # 求值
    "ValueArray",
    "evaluate",
    "evaluate_predicate",
    "fold_constants",
    "array_to_value",
    "like_to_regex",
    "compile_like",
    "LikeMatcher",
    # 改写
    "simplify",
    "to_index_expr",
    "split_conjuncts",
    "split_disjuncts",
    "extract_zone_map_predicate",
    "estimate_selectivity",
]


def parse_where(sql: str, schema: Schema | None = None) -> Expr:
    """``.where(...)`` 的同义入口，名字更贴近用户写的代码。

    有 schema 时等价于 :func:`~mini_lancedb.expr.parser.parse_predicate`，
    没有时等价于 :func:`~mini_lancedb.expr.parser.parse`。
    """

    if schema is None:
        return parse(sql)
    return parse_predicate(sql, schema)
