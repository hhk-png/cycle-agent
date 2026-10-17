"""标量索引之间共享的小工具（私有模块，不对外导出）。

这里的每一样东西之所以"共享"，是因为它们解决的是**所有标量索引都会碰到**的
同一批问题：

1. **类型杂糅的排序键。** BTREE 要按值排序，可一列里可能有 ``None``、
   ``int``、``str``（schema 演进、union 之后真的会出现）。直接 ``sorted()``
   会在 ``None < 1`` 上抛 ``TypeError``。所以统一映射到一条人为的全序上：
   ``None < bool < 数值 < 字符串 < 其它``。
   注意这不是"数据库该有的语义"（真实 SQL 里 NULL 的排序由
   ``NULLS FIRST/LAST`` 决定），它只是为了让索引结构**永远建得出来**；
   真正的语义过滤发生在查询翻译那一步 —— null 行压根不进索引。

2. **行号集合的表达式求值。** ``ScalarIndexExpr`` 是标量索引对外唯一的
   "语言"：叶子是 :class:`ExactMatch`，内部节点是 ``And`` / ``Or`` / ``Not``。
   每个索引只需要学会把**自己的**谓词翻译成 ``ExactMatch``，剩下的交并补
   由这里统一做 —— 这就是"索引可组合"的关键：规划器不需要知道
   BTREE、BITMAP、FTS 各自怎么实现，只需要它们都会说 ``ExactMatch``。

3. **诚实的退化。** :func:`materialize` 遇到任何一个索引答不了的叶子
   就整棵树返回 ``None``，绝不用"部分答案"冒充完整答案。
   Lance 的 ``ScalarIndex::search`` 返回 ``Option<ScalarIndexExpr>``
   也是这个意思：``None`` = "请退化成全表扫描"。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

import numpy as np

from ..arrays import Array
from .base import And, ExactMatch, Not, Or, ScalarIndex, ScalarIndexExpr

__all__ = [
    "sort_key",
    "values_of",
    "row_ids_of",
    "materialize",
    "resolve_mask",
    "combine_rows",
    "estimate_array_bytes",
]


def sort_key(value: Any) -> tuple:
    """把任意标量映射到一条全序上（见模块 docstring 第 1 点）。"""

    if value is None:
        return (0, 0)
    if isinstance(value, (bool, np.bool_)):
        # bool 必须在数值之前判：Python 里 True == 1，不先拦下来就分不清
        return (1, int(value))
    if isinstance(value, (int, float, np.integer, np.floating)):
        if isinstance(value, float) and value != value:  # NaN 排到最后
            return (2, float("inf"))
        return (2, float(value))
    if isinstance(value, str):
        return (3, value)
    if isinstance(value, (bytes, bytearray)):
        return (3, bytes(value))
    return (4, repr(value))


def values_of(values: Array) -> List[Any]:
    """列式数组 → Python 值列表，``None`` 表示 null。

    索引一律按"值列表"工作而不是按 Arrow 数组工作：标量索引的规模是
    行的数量级（不是向量的维度量级），逐值取出来反而更简单也更可控。
    真正需要向量化的是**词项到行号**的映射，那部分我们已经有 numpy 了。
    """

    return list(values.to_pylist())


def row_ids_of(row_ids: Optional[Sequence[int]], n: int) -> np.ndarray:
    """把行号规整成 ``int64`` 数组；没给行号就当作 ``0..n-1``。"""

    if row_ids is None:
        return np.arange(n, dtype=np.int64)
    arr = np.asarray(row_ids, dtype=np.int64)
    if arr.ndim != 1 or len(arr) != n:
        raise ValueError(f"row_ids 长度 {len(arr)} 与列长度 {n} 不符")
    return arr


def estimate_array_bytes(array: Array) -> int:
    return int(getattr(array, "nbytes", 0) or 0)


# ---------------------------------------------------------------------------
# 表达式：翻译与求值
# ---------------------------------------------------------------------------


def materialize(
    expr: ScalarIndexExpr,
    leaf: Callable[[ScalarIndexExpr], Optional[ScalarIndexExpr]],
) -> Optional[ScalarIndexExpr]:
    """把表达式树里的叶子交给 ``leaf`` 处理，内部节点原样保留。

    ``leaf`` 返回 ``None`` 表示"这个谓词我答不了"，此时整棵树返回 ``None``。
    特别地，已经是 :class:`ExactMatch` 的叶子直接放行 —— 它已经是最简形式了，
    再交给索引去"翻译"一次反而会出错（索引收到 ExactMatch 只会说答不了）。
    """

    if isinstance(expr, ExactMatch):
        return expr
    if isinstance(expr, (And, Or)):
        kids: List[ScalarIndexExpr] = []
        for child in (
            expr.children if isinstance(expr, (And, Or)) else []  # type: ignore[attr-defined]
        ):
            got = materialize(child, leaf)
            if got is None:
                return None
            kids.append(got)
        return And(kids) if isinstance(expr, And) else Or(kids)
    if isinstance(expr, Not):
        got = materialize(expr.child, leaf)
        return None if got is None else Not(got)
    return leaf(expr)


def resolve_mask(index: ScalarIndex, expr: ScalarIndexExpr) -> Optional[np.ndarray]:
    """把表达式求值成"索引自己的行宇宙"上的布尔掩码。

    "行宇宙" = ``index.row_ids``（被索引的那些行），**不是**整张表的行。
    这和基类 ``ScalarIndex.resolve(expr, num_rows)`` 是两件事：基类要的是
    面向表行号的掩码，供查询引擎最终过滤用；这里要的是索引内部的掩码，
    用来做索引内部的交并补。两者混用是"过滤结果莫名多出几行"的经典原因。

    索引答不了（``range`` 之类）时返回 ``None``。
    """

    universe = np.asarray(index.row_ids, dtype=np.int64)
    n = len(universe)
    if isinstance(expr, ExactMatch):
        return _ids_to_mask(universe, expr.row_ids)
    if isinstance(expr, And):
        parts = [resolve_mask(index, c) for c in expr.children]
        if any(p is None for p in parts):
            return None
        out = np.ones(n, dtype=bool)
        for p in parts:
            out &= p  # type: ignore[operator]
        return out
    if isinstance(expr, Or):
        parts = [resolve_mask(index, c) for c in expr.children]
        if any(p is None for p in parts):
            return None
        out = np.zeros(n, dtype=bool)
        for p in parts:
            out |= p  # type: ignore[operator]
        return out
    if isinstance(expr, Not):
        inner = resolve_mask(index, expr.child)
        return None if inner is None else ~inner
    return None


def _ids_to_mask(universe: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """行号集合 → 布尔掩码（按 ``universe`` 的下标顺序）。

    用 ``searchsorted`` 而不是 Python 集合：行号是稠密的整数，
    一次排序加一次二分比 n 次哈希快一个量级。
    """

    n = len(universe)
    mask = np.zeros(n, dtype=bool)
    if n == 0 or len(ids) == 0:
        return mask
    ids = np.asarray(ids, dtype=np.int64)
    order = np.argsort(universe, kind="stable")
    sorted_universe = universe[order]
    pos = np.searchsorted(sorted_universe, ids)
    pos = np.clip(pos, 0, n - 1)
    ok = sorted_universe[pos] == ids
    mask[order[pos[ok]]] = True
    return mask


def combine_rows(
    index: ScalarIndex, expr: ScalarIndexExpr
) -> Optional[ExactMatch]:
    """把只含 ``ExactMatch`` 的表达式塌缩成一个 ``ExactMatch``。

    这一层的存在让"索引可组合"变成一件小事：``BTREE 的范围 + FTS 的匹配``
    只需要 ``And([range_match, fts_match])``，两者的实现完全不用互相知道。
    """

    mask = resolve_mask(index, expr)
    if mask is None:
        return None
    return ExactMatch(np.sort(np.asarray(index.row_ids, dtype=np.int64)[mask]))
