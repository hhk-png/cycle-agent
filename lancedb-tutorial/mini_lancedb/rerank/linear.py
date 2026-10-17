"""线性加权融合：先归一化，再相加。

## 配方

.. code-block:: text

    rel(d) = weight * norm_vector(d) + (1 - weight) * norm_fts(d)

三件事必须按顺序做，顺序错了结果就错了：

1. **统一方向**。向量路的 ``_distance`` 是"越小越好"，先取负号翻成
   "越大越好"（:func:`~mini_lancedb.rerank.base.SCORE_SCALES`）；
2. **归一化**。两路的原始分数没有可比性，归到同一个区间（通常 ``[0, 1]``）；
3. **加权求和**。此时加法才有意义。

``weight`` 的含义和真实 LanceDB 一致：**给向量路的权重**，
所以 ``weight=0`` 等于"只用 FTS"，``weight=1`` 等于"只用向量"。

## 三种归一化，各自踩什么坑

.. list-table::
   :header-rows: 1

   * - 模式
     - 做法
     - 失败模式
   * - ``"minmax"``
     - ``(x - min) / (max - min)``
     - **对异常值极其敏感**：一批 ``[0.1, 0.2, 0.3, 100]`` 的分数里，
       前三个归一化之后是 ``[0.000, 0.001, 0.002]`` —— 它们的差距被压缩
       到几乎看不见；反过来，如果某一路召回**质量很差**、分数分布很扁，
       min-max 又会把这点微小的差异**放大**成 0~1 的完整区间，
       于是这一路在加权时拿到了远超它应得的发言权。真实系统里它必须
       配合"取前 N 条"使用（前 N 条里通常没有太离谱的离群点）。
   * - ``"rank"``
     - 名次归一化：第 1 名 1.0，第 N 名 0.0，中间线性
     - **丢掉幅度信息**（和 RRF 一样的病）：一个"遥遥领先"的第 1 名
       和"勉强领先"的第 1 名得到的分数完全一样。另外，**并列分数**
       必须显式处理，否则同一分数会因为出现的先后被拆成不同分数
       （这里用平均名次，并列者拿到完全相同的归一化分数）。
   * - ``"none"``
     - 不归一化，直接拿原始分数（先统一方向）
     - 只有在你**确认两路分数已经在同一个尺度上**时才能用
       （比如你自己已经把所有分数映射到了 BM25 量级）。
       这是最容易悄悄错的一个：它不会报错，只会让权重的物理含义消失。

## 缺失值怎么填

一篇文档经常只被一路召回。"另一路没有分数"这件事必须有个**约定**，
否则它就该被当成 0 分、还是被丢弃？这里用 ``fill`` 参数显式回答：

* ``fill`` 是浮点数 → 直接用这个常数（在**归一化之后**的相关性空间里，
  ``0.0`` = "当作最不相关"，``1.0`` = "当作最相关"，``0.5`` = "当作中位"）；
* ``fill`` 是字符串 → 从这一路**已有的**归一化分数里算：
  ``"min"``（= 0.0）、``"max"``（= 1.0）、``"mean"``（该路均值）、
  ``"zero"``（= 0.0，和 ``"min"`` 同义）。

默认 ``0.0``：**没被这一路召回 = 这一路给它零分**。这是最保守、也最好解释的
约定。（真实 LanceDB 的默认 ``fill=1.0`` 是在"距离空间"里填的惩罚值，
方向是反的，官方注释自己都写了 "pretty confusing" —— 这里刻意换成了
"归一化后的相关性空间"，方向统一。）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from ..errors import QueryError
from .base import (
    DISTANCE_COL,
    RELEVANCE_COL,
    ROWID_COL,
    SCORE_COL,
    Row,
    Reranker,
    as_rows,
    resolve_ids,
    sort_rows_by_relevance,
)

__all__ = [
    "LinearCombinationReranker",
    "normalize_scores",
    "average_ranks",
    "NORMALIZE_MODES",
    "FILL_STRATEGIES",
]

#: 支持的归一化模式。
NORMALIZE_MODES = ("minmax", "rank", "none")
#: ``fill`` 允许的字符串策略。
FILL_STRATEGIES = ("min", "max", "mean", "zero", "worst", "best")


def average_ranks(values: Sequence[float]) -> np.ndarray:
    """1-based **平均名次**（并列取平均），越大越排后面。

    和 ``scipy.stats.rankdata(method="average")`` 一致，但这里只用 numpy
    实现，因为我们的依赖表里没有 scipy。

    为什么非要做并列处理：归一化之后分数要拿去**加权**。如果并列的两篇
    文档因为"谁先出现"被拆成 1 名和 2 名，那么排序结果的差异完全由
    输入顺序决定 —— 换个召回的返回顺序，结果就变了。这在因果上说不通，
    也让测试无法稳定复现。
    """

    v = np.asarray(values, dtype=np.float64).ravel()
    n = v.size
    out = np.empty(n, dtype=np.float64)
    if n == 0:
        return out
    order = np.argsort(v, kind="stable")
    sorted_v = v[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_v[j + 1] == sorted_v[i]:
            j += 1
        # 名次从 1 开始：位置 i..j（0-based）对应的平均名次
        out[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return out


def normalize_scores(
    values: Sequence[float],
    mode: str = "minmax",
    *,
    higher_is_better: bool = True,
    eps: float = 1e-12,
) -> np.ndarray:
    """把一批分数归一化成 ``[0, 1]`` 且**方向统一为"越大越相关"**。

    Parameters
    ----------
    values :
        原始分数。
    mode :
        ``"minmax"`` / ``"rank"`` / ``"none"``。
    higher_is_better :
        输入分数的方向。``False``（例如 ``_distance``）会先取负号。
        注意 ``"none"`` 模式下取负号**只是为了统一方向**，尺度并未对齐，
        所以调用方要自己保证两路可比。

    边界情形（都有明确约定，且被测试钉住）：

    * 空输入 → 空数组；
    * 所有值相同（``max == min``）→ 全部归到 ``1.0``。
      含义是"这批结果之间没有任何信息可供区分，所以谁也别因为
      min-max 的除零而被判成 0 分"。
    """

    v = np.asarray(values, dtype=np.float64).ravel()
    if mode not in NORMALIZE_MODES:
        raise QueryError("不支持的归一化模式", mode=mode, supported=list(NORMALIZE_MODES))
    if v.size == 0:
        return v.copy()
    if not higher_is_better:
        v = -v

    if mode == "none":
        return v.copy()

    if mode == "minmax":
        lo = float(v.min())
        hi = float(v.max())
        if hi - lo < eps:
            return np.ones_like(v)
        return (v - lo) / (hi - lo)

    # rank。``average_ranks`` 是**升序**名次（值最小的拿名次 1），
    # 而我们这里的 ``v`` 已经统一成"越大越好"，所以要先取负号再排名次，
    # 否则会把最不相关的结果排成 1.0 —— 方向反了，而且不会报错。
    n = v.size
    if n == 1:
        return np.ones(1, dtype=np.float64)
    ranks = average_ranks(-v)
    # 名次 1 → 1.0，名次 n → 0.0
    return (n - ranks) / (n - 1)


class LinearCombinationReranker(Reranker):
    """归一化 + 线性加权。

    Parameters
    ----------
    weight : float, default 0.7
        **向量路**的权重，必须在 ``[0, 1]`` 内。真实 LanceDB 的默认值也是 0.7 ——
        经验上向量召回负责"语义"，FTS 负责"精确词匹配"，前者通常更值得信任一点。
    normalize : str, default "minmax"
        ``"minmax"`` / ``"rank"`` / ``"none"``。
    fill : float | str, default 0.0
        缺失分数的填充策略，见模块文档。
    return_score : str, default "relevance"
    """

    def __init__(
        self,
        weight: float = 0.7,
        normalize: str = "minmax",
        fill: Union[float, str] = 0.0,
        return_score: str = "relevance",
        *,
        id_column: str = ROWID_COL,
    ) -> None:
        weight = float(weight)
        if not 0.0 <= weight <= 1.0:
            raise QueryError("weight 必须在 [0, 1] 之间", weight=weight)
        if normalize not in NORMALIZE_MODES:
            raise QueryError(
                "不支持的归一化模式", normalize=normalize, supported=list(NORMALIZE_MODES)
            )
        if isinstance(fill, str):
            if fill not in FILL_STRATEGIES:
                raise QueryError(
                    "不支持的 fill 策略", fill=fill, supported=list(FILL_STRATEGIES)
                )
        elif not isinstance(fill, (int, float)):
            raise QueryError("fill 必须是数字或策略字符串", fill=fill)
        super().__init__(return_score, id_column=id_column)
        self.weight = weight
        self.normalize = normalize
        self.fill = fill

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"LinearCombinationReranker(weight={self.weight}, "
            f"normalize={self.normalize!r}, fill={self.fill!r})"
        )

    # -- 把"一行"的原始分数抽出来 -----------------------------------------
    @staticmethod
    def _vector_raw(row: Row) -> Optional[float]:
        """向量路的原始分，翻成"越大越好"。缺列或值为 None → None。"""

        value = row.get(DISTANCE_COL)
        if value is None:
            return None
        # 取负号而不是真实 LanceDB 的 ``1 - d``：minmax / rank 都对
        # 仿射变换免疫，所以两种写法等价；而"none"模式下取负不会把
        # 一个本来无界的分数量纲又压进 [0, 1]，语义更诚实。
        return -float(value)

    @staticmethod
    def _fts_raw(row: Row) -> Optional[float]:
        """FTS 路的原始分（BM25，本来就越打越相关）。"""

        value = row.get(SCORE_COL)
        if value is None:
            return None
        return float(value)

    # -- 归一化 + 填充 ----------------------------------------------------
    def _normalize_with_fill(
        self, raw: Sequence[Optional[float]]
    ) -> Tuple[List[float], List[bool]]:
        """归一化一批"可能缺失"的分数。

        返回 ``(归一化后的值, 是否缺失)``。**缺失项不参与 min/max/rank 的
        统计量估计** —— 这一点很重要：如果先把缺失项当成 0 塞进去再算
        min-max，那么"这一路一条都没召回"会变成"召回了一堆 0 分"，
        两种情况的行为就一样了，而它们本该不一样。
        """

        present = [i for i, value in enumerate(raw) if value is not None]
        out: List[float] = [0.0] * len(raw)
        missing: List[bool] = [value is None for value in raw]
        if not present:
            return out, missing

        normalized = normalize_scores(
            [raw[i] for i in present], self.normalize, higher_is_better=True
        )
        for slot, value in zip(present, normalized):
            out[slot] = float(value)

        fill_value = self._fill_value(normalized)
        for i, is_missing in enumerate(missing):
            if is_missing:
                out[i] = fill_value
        return out, missing

    def _fill_value(self, normalized: np.ndarray) -> float:
        if isinstance(self.fill, str):
            if self.fill in ("min", "zero", "worst"):
                # 归一化之后最小值恒为 0.0（rank 模式下并列最小值也是 0.0）
                return float(normalized.min())
            if self.fill in ("max", "best"):
                return float(normalized.max())
            return float(normalized.mean())  # "mean"
        return float(self.fill)

    # -- 融合 -------------------------------------------------------------
    def _combine(
        self, vector_raw: Sequence[Optional[float]], fts_raw: Sequence[Optional[float]]
    ) -> np.ndarray:
        """两路（可能缺失）的原始分 → 一行一个 ``_relevance_score``。"""

        has_vector = any(v is not None for v in vector_raw)
        has_fts = any(v is not None for v in fts_raw)

        # 退化情形：只有一路有分。这时候"缺失"不是缺失 —— 这一路压根没参与
        # 这次检索。若还按 fill 扣分，就会把整路结果统一压低，纯属噪音。
        if has_vector and not has_fts:
            values, _ = self._normalize_with_fill(vector_raw)
            return np.asarray(values, dtype=np.float64)
        if has_fts and not has_vector:
            values, _ = self._normalize_with_fill(fts_raw)
            return np.asarray(values, dtype=np.float64)
        if not has_vector and not has_fts:
            return np.zeros(len(vector_raw), dtype=np.float64)

        v_norm, _ = self._normalize_with_fill(vector_raw)
        f_norm, _ = self._normalize_with_fill(fts_raw)
        return self.weight * np.asarray(v_norm, dtype=np.float64) + (
            1.0 - self.weight
        ) * np.asarray(f_norm, dtype=np.float64)

    def _fuse_lists(self, vector_rows: Any, fts_rows: Any) -> List[Row]:
        vec = as_rows(vector_rows)
        fts = as_rows(fts_rows)
        if not vec and not fts:
            return []
        if not vec or not fts:
            # 只有一路有结果：这时"另一路缺失"不是缺失 —— 这一路压根没参与
            # 本次检索，按 fill 给它扣分只会把整批结果统一压低。
            # 直接走 _combine：它按**行里实际有哪些列**决定用哪一路，
            # 所以调用方就算把向量结果放进 fts 槽位，也不会静默拿到一堆 0 分。
            rows = fts if not vec else vec
            values = self._combine(
                [self._vector_raw(r) for r in rows], [self._fts_raw(r) for r in rows]
            )
            out = [dict(r) for r in rows]
            for row, value in zip(out, values):
                row[RELEVANCE_COL] = float(value)
            return out

        # 按 id 合并（向量路在前：和 merge_results 的"先出现者胜"一致）
        vec_ids = resolve_ids(vec, self.id_column)
        fts_ids = resolve_ids(fts, self.id_column)
        rows: List[Row] = []
        index: Dict[Any, int] = {}
        for row_id, row in zip(vec_ids, vec):
            index[row_id] = len(rows)
            rows.append(dict(row))
        for row_id, row in zip(fts_ids, fts):
            existing = index.get(row_id)
            if existing is None:
                index[row_id] = len(rows)
                rows.append(dict(row))
            else:
                for key, value in row.items():
                    if key not in rows[existing] or rows[existing][key] is None:
                        rows[existing][key] = value

        scores = self._combine(
            [self._vector_raw(r) for r in rows], [self._fts_raw(r) for r in rows]
        )
        for row, value in zip(rows, scores):
            row[RELEVANCE_COL] = float(value)
        return rows

    def rerank_hybrid(
        self,
        query: Optional[str],
        vector_results: Any,
        fts_results: Any,
        *,
        k: Optional[int] = None,
        **_: Any,
    ) -> List[Row]:
        fused = self._fuse_lists(vector_results, fts_results)
        return self._finalize(sort_rows_by_relevance(fused), k)

    def score_rows(self, query: Optional[str], rows: Sequence[Row]) -> List[float]:
        """单列表打分：行里有什么分数就用什么。

        一条行里同时有 ``_distance`` 和 ``_score``（比如已经把两路合并过了）
        就按 ``weight`` 加权；只有一路就只用那一路。
        """

        rows = list(rows)
        scores = self._combine(
            [self._vector_raw(r) for r in rows], [self._fts_raw(r) for r in rows]
        )
        return [float(s) for s in scores]
