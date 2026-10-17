"""RRF（Reciprocal Rank Fusion，倒数名次融合）。

## 公式

对每一篇文档 d：

.. code-block:: text

    score(d) = Σ_i  weight_i / (rank_i(d) + k)

* ``rank_i(d)`` 是 d 在第 i 路结果里的**名次，从 1 开始**（第 1 名 rank = 1）；
* 第 i 路没召回 d，则该项**整体不出现**（不是当成 0 分再加一遍 —— 两者等价，
  但"不出现"在代码里更清楚）；
* ``k`` 是平滑常数，论文（Cormack et al., SIGIR 2009）建议 60，
  真实 LanceDB 的默认值也是 60。

## 为什么它"免归一化"（scale-free）

RRF 只读**名次**，不读分数本身。所以：

* 向量路返回的是 ``_distance`` 还是 ``_score``、量纲是 ``[0,2]`` 还是
  ``[0, 30000]``、方向是越大越好还是越小越好 —— **统统无所谓**，
  只要"每路各自按自己的标准排好序"就行；
* 新增第三路、第四路召回，也不用为它调任何权重尺度；
* 也因此它不会因为某一路的分数分布漂移（换语料之后 BM25 整体平移）
  而突然失效。

这在工程上非常值钱：混合检索最难维护的部分就是"两路分数的相对尺度"，
RRF 直接把这个自由度删掉了。

## 失败模式：丢掉幅度信息

代价也很实在：

* 名次第 1 和第 2 之间的差距**永远**是 ``1/(1+k) - 1/(2+k)``，
  和"第 1 名遥遥领先"还是"第 1、2 名几乎一样好"完全无关。
  一个查询只有一个明显正解时，RRF 会把一大堆平庸结果拉到接近的分数；
* 两路召回**质量差很多**时，RRF 默认给它们同等的权重。
  这正是 ``weights`` 参数的用处：给靠谱的那一路更大的 weight，
  而不是让名次"民主投票"；
* ``k`` 越小越"独裁"（前几名通吃），``k`` 越大越"平均"
  （名次之间差距被抹平）。经验上 60 附近不敏感，但**把 k 调到 1**
  和 **调到 10000** 的结果会明显不同 —— 见测试里的 k 敏感性断言。

## 和真实 LanceDB 的对应

真实实现里 ``k`` 参数名是大写 ``K``（``RRFReranker(K=60)``）。这里主参数
叫 ``k``（和小写惯例一致），同时保留 ``K`` 作为只读属性 + 构造别名，
方便把官方文档里的例子原样搬过来。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..errors import QueryError
from .base import RELEVANCE_COL, ROWID_COL, Row, Reranker, as_rows, resolve_ids

__all__ = ["RRFReranker", "rrf_score"]


def rrf_score(rank: int, k: int = 60, weight: float = 1.0) -> float:
    """单路的 RRF 单项：``weight / (rank + k)``。``rank`` 从 1 开始。"""

    if rank < 1:
        raise QueryError("rank 必须从 1 开始", rank=rank)
    if k <= 0:
        raise QueryError("k 必须为正", k=k)
    return float(weight) / float(rank + k)


class RRFReranker(Reranker):
    """倒数名次融合。

    Parameters
    ----------
    k : int, default 60
        平滑常数。**不是**"保留多少行"（那个是 ``rerank(..., k=...)``）。
    return_score : str, default "relevance"
        ``"relevance"`` 只返回 ``_relevance_score``；``"all"`` 额外保留
        ``_distance`` / ``_score``（把同 id 在两路里的分数合并起来）。
    weights : float | Sequence[float] | None
        每一路的权重。标量会被复制成"每一路都一样"。``None`` 表示全 1.0。
        第 i 个权重对应第 i 个列表（**空列表也占位**，否则加一路召回就会
        让后面所有路的权重错位）。
    K : int, optional
        真实 LanceDB 的参数名，给了就覆盖 ``k``。
    """

    def __init__(
        self,
        k: int = 60,
        return_score: str = "relevance",
        *,
        weights: Union[float, Sequence[float], None] = None,
        K: Optional[int] = None,
        id_column: str = ROWID_COL,
    ) -> None:
        if K is not None:
            k = K
        k = int(k)
        if k <= 0:
            raise QueryError("RRF 的 k 必须为正整数", k=k)
        super().__init__(return_score, id_column=id_column)
        #: RRF 公式里的平滑常数。
        self.k = k
        self.weights = weights

    @property
    def K(self) -> int:
        """真实 LanceDB 用的名字（大写）。"""

        return self.k

    def __str__(self) -> str:  # pragma: no cover
        w = "" if self.weights is None else f", weights={self.weights}"
        return f"RRFReranker(k={self.k}{w})"

    # -- 权重 -------------------------------------------------------------
    def _resolve_weights(self, num_lists: int) -> List[float]:
        """把 ``weights`` 规整成"长度 = 路数"的列表。"""

        if self.weights is None:
            return [1.0] * num_lists
        if isinstance(self.weights, (int, float)):
            w = float(self.weights)
            if w < 0:
                raise QueryError("RRF 权重必须非负", weights=self.weights)
            return [w] * num_lists
        weights = [float(w) for w in self.weights]
        if len(weights) != num_lists:
            raise QueryError(
                "weights 的长度必须等于结果列表的个数",
                expected=num_lists,
                got=len(weights),
            )
        if any(w < 0 for w in weights):
            raise QueryError("RRF 权重必须非负", weights=weights)
        return weights

    # -- 核心 -------------------------------------------------------------
    def fuse(self, lists: Sequence[Sequence[Row]]) -> List[Row]:
        """把 N 路结果按 RRF 融合。**这是本模块唯一的核心函数。**

        返回按 ``_relevance_score`` 降序排好的行；同一路里重复出现的 id
        （少见但可能，比如 FTS 返回了重复行）只会被计一次，
        因为"名次"的定义就是"第一次出现的位置"。
        """

        lists = [as_rows(item) for item in lists]
        if not lists:
            return []
        weights = self._resolve_weights(len(lists))

        scores: Dict[Any, float] = {}
        first_seen: Dict[Any, Tuple[int, int]] = {}
        rows_by_id: Dict[Any, Row] = {}
        order: List[Any] = []

        for list_index, rows in enumerate(lists):
            if not rows:
                continue
            ids = resolve_ids(rows, self.id_column)
            seen_in_this_list = set()
            for rank0, (row_id, row) in enumerate(zip(ids, rows)):
                if row_id in seen_in_this_list:
                    # 同一路里的重复 id 只算一次名次
                    continue
                seen_in_this_list.add(row_id)
                # 名次从 1 开始，所以分母是 (rank0 + 1) + k
                scores[row_id] = scores.get(row_id, 0.0) + weights[list_index] / (
                    rank0 + 1 + self.k
                )
                if row_id not in rows_by_id:
                    rows_by_id[row_id] = dict(row)
                    first_seen[row_id] = (list_index, rank0)
                    order.append(row_id)
                else:
                    # 同一 id 在另一路也出现：把对方独有的字段补进来，
                    # 这样 return_score="all" 才能同时看到 _distance 和 _score。
                    existing = rows_by_id[row_id]
                    for key, value in row.items():
                        if key not in existing or existing[key] is None:
                            existing[key] = value

        scored: List[Tuple[Any, Row]] = []
        for row_id in order:
            row = rows_by_id[row_id]
            row[RELEVANCE_COL] = float(scores[row_id])
            scored.append((row_id, row))

        # 排序键：分数降序 → 首次出现的位置（路号，名次）→ 行 id。
        # 最后一项是**确定性**的兜底：Python 的 sorted 是稳定的，但两个
        # 完全不同的 id 恰好分数相同、又出现在同一路同一名次是不可能的，
        # 所以真实场景里轮不到它；写在这儿是为了让"分数全相等"的极端
        # 用例也有可复现的输出。
        scored.sort(
            key=lambda item: (
                -float(item[1][RELEVANCE_COL]),
                first_seen[item[0]][0],
                first_seen[item[0]][1],
                str(item[0]),
            )
        )
        return [row for _, row in scored]

    # -- 入口 -------------------------------------------------------------
    def rerank_hybrid(
        self,
        query: Optional[str],
        vector_results: Any,
        fts_results: Any,
        *,
        k: Optional[int] = None,
        **_: Any,
    ) -> List[Row]:
        return self._finalize(self.fuse([vector_results, fts_results]), k)

    def rerank_multivector(
        self,
        query: Optional[str],
        vector_results: Any,
        *,
        k: Optional[int] = None,
        deduplicate: bool = True,
        **_: Any,
    ) -> List[Row]:
        """N 路融合。``deduplicate`` 参数在这里只有说明意义 ——
        RRF 的定义决定了它天然会去重（同一 id 的分数是累加而不是重复出现）。"""

        if isinstance(vector_results, (list, tuple)) and len(vector_results) == 0:
            raise QueryError("vector_results 不能为空")
        lists = vector_results if isinstance(vector_results, (list, tuple)) else [vector_results]
        if len(lists) == 1:
            # 只有一路：走单列表路径，省得把"名次"折算成 1/(rank+k) 又排回来
            return self.rerank_vector(query, lists[0], k=k)
        return self._finalize(self.fuse(list(lists)), k)

    def rerank_vector(
        self, query: Optional[str], vector_results: Any, *, k: Optional[int] = None, **_: Any
    ) -> List[Row]:
        """单列表：先去重，再按名次打分。

        去重是必须的 —— "融合器"的输出里不允许同一篇文档出现两次
        （真实 LanceDB 的 ``_deduplicate`` 就是这么做的）。
        分数 ``weight_0 / (rank + k)`` 单调递减，所以**顺序保持不变**：
        对单路结果做 RRF 等于不做，但它是合法的，
        让 ``rerank(query, rows)`` 在只有一路结果时也能正常工作
        （真实 LanceDB 在这里是直接抛 NotImplementedError）。
        """

        rows = self._deduplicate(as_rows(vector_results))
        return self._finalize(self._score_and_rank(query, rows), k)

    def score_rows(self, query: Optional[str], rows: Sequence[Row]) -> List[float]:
        """第 i 行（i 从 0 起）得 ``weight_0 / (i + 1 + k)``。"""

        rows = list(rows)
        weight = self._resolve_weights(1)[0]
        return [weight / (i + 1 + self.k) for i in range(len(rows))]
