"""向量索引的公共工具：度量解析、距离内核、掩码、Top-K 组装与尺寸统计。

这个模块刻意只放"每个索引都要用、但和具体算法无关"的东西：

* :func:`resolve_metric` —— 从 ``IndexMetadata`` 里把度量捞出来。LanceDB 把
  ``distance_type`` 记在**索引参数**里而不是索引文件里，所以打开索引时只需要
  元数据就能知道该用哪个距离函数；
* :class:`DistanceKernel` —— 把距离计算固定成"一次预处理 + 多次
  *查询 × 一批行号*"。这是所有 ANN 索引的性能命门：HNSW 建图时要算几十万
  次小规模距离，如果每次都走 :func:`pairwise_distances`（它要先按行归一化、
  再构造 ``||a||²`` 的中间量），Python 侧的常数会大到没法用。所以这里把
  "按度量预处理矩阵"（cosine 先归一化）和"单查询到一批行的距离"拆开，
  热循环里只走后者。
* 掩码（删除行 / prefilter）、Top-K 结果组装、真实字节数统计等小工具。

关于 cosine 的一个关键细节：对**单位向量**有 ``1 - cos(a,b) = ||a-b||² / 2``。
所以 kernel 在 cosine 模式下先把库里的向量归一化，之后统一用平方欧氏距离，
再乘 1/2 —— 得到的数值和 :func:`cosine_distance` 完全一致（不是"单调等价"，
是同一个数），这样不同索引类型之间比较距离才有意义。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional

import numpy as np

from ..errors import IndexBuildError, QueryError
from .base import IndexMetadata, IndexStore, SearchResult
from .metric import Metric, get_metric, normalize, pairwise_distances, top_k_from_matrix

__all__ = [
    "DEFAULT_METRIC",
    "DEFAULT_NPROBES",
    "resolve_metric",
    "as_matrix",
    "as_row_ids",
    "prepare_matrix",
    "check_queries",
    "check_k",
    "as_mask",
    "default_num_partitions",
    "topk_result",
    "save_arrays",
    "total_nbytes",
    "DistanceKernel",
    "partition_size_stats",
]

#: 没有显式指定时用平方欧氏距离（LanceDB 的默认值）。
DEFAULT_METRIC = "l2"

#: IVF 系列的默认 ``nprobes``。真实 LanceDB 用的是 20。
DEFAULT_NPROBES = 20


# ---------------------------------------------------------------------------
# 参数与输入校验
# ---------------------------------------------------------------------------


def resolve_metric(metadata: IndexMetadata) -> Metric:
    """从索引元数据恢复度量。

    优先读 ``params["distance_type"]``（LanceDB 的正式字段名），
    退回 ``details["metric_type"]``（我们在 build 时补写的运行时信息），
    都没有就按默认的 l2 处理。
    """

    name = (
        metadata.params.get("distance_type")
        or metadata.details.get("metric_type")
        or DEFAULT_METRIC
    )
    return get_metric(str(name))


def as_matrix(data: Any, *, what: str = "向量") -> np.ndarray:
    """把输入规整成 ``(n, d)`` 的 C 连续 float32 矩阵。

    向量索引对输入形状的假设比查询引擎严格得多：它需要"行是向量、
    列是维度"的稠密矩阵，因为所有距离计算都是矩阵乘法的变体。
    """

    m = np.asarray(data, dtype=np.float32)
    if m.ndim == 1:
        m = m.reshape(1, -1)
    if m.ndim != 2:
        raise IndexBuildError(f"{what}必须是二维矩阵", shape=list(m.shape))
    if m.shape[0] == 0:
        raise IndexBuildError(f"{what}为空：索引至少需要一行数据", shape=list(m.shape))
    if m.shape[1] == 0:
        raise IndexBuildError(f"{what}的维度为 0", shape=list(m.shape))
    if not np.isfinite(m).all():
        # 真实 Lance 允许 NaN（代表 null），但那样距离排序会被污染，
        # 这种数据应该在写入路径就被 on_bad_vectors 处理掉。
        raise IndexBuildError(f"{what}里含有 NaN/Inf，无法建索引")
    return np.ascontiguousarray(m, dtype=np.float32)


def as_row_ids(row_ids: Any, n: int) -> np.ndarray:
    """校验并规整行号数组。行号是**表的行号**，不是索引内的位置下标。"""

    ids = np.asarray(row_ids)
    if ids.ndim != 1:
        raise IndexBuildError("row_ids 必须是一维数组", shape=list(ids.shape))
    if ids.shape[0] != n:
        raise IndexBuildError(
            "row_ids 长度与向量行数不一致", rows=n, row_ids=int(ids.shape[0])
        )
    if ids.dtype.kind not in "iu":
        if ids.dtype.kind == "f" and bool(np.all(ids == np.floor(ids))):
            ids = ids.astype(np.int64)
        else:
            raise IndexBuildError("row_ids 必须是整数数组", dtype=str(ids.dtype))
    if ids.size and int(ids.min()) < 0:
        raise IndexBuildError("row_ids 不能为负数")
    return ids.astype(np.uint64, copy=False)


def prepare_matrix(data: Any, metric: Metric) -> np.ndarray:
    """按度量预处理存储矩阵。

    对 cosine：写入时归一化（正是 LanceDB 的做法 —— 省掉每次查询的除法）。
    对 l2 / dot / hamming：原样返回。
    """

    m = np.ascontiguousarray(np.asarray(data, dtype=np.float32))
    if metric.needs_normalization:
        m = np.ascontiguousarray(normalize(m))
    return m


def check_queries(queries: Any, dim: int) -> np.ndarray:
    """校验查询矩阵的形状。维度对不上是**查询错误**，不是构建错误。"""

    x = np.asarray(queries, dtype=np.float32)
    if x.ndim == 1:
        x = x.reshape(1, -1)
    if x.ndim != 2:
        raise QueryError("查询向量必须是 (q, d) 的二维矩阵", shape=list(x.shape))
    if x.shape[1] != dim:
        raise QueryError(
            "查询向量维度与索引不一致",
            query_dim=int(x.shape[1]),
            index_dim=int(dim),
            hint="检查是不是把另一个向量列的查询发到了这个索引上",
        )
    return np.ascontiguousarray(x, dtype=np.float32)


def check_k(k: int, n: int) -> int:
    """校验 top-k 的 k。"""

    k = int(k)
    if k <= 0:
        raise QueryError("k 必须为正整数", k=k)
    if n <= 0:
        raise QueryError("索引里没有任何行", num_indexed_rows=n)
    if k > n:
        raise QueryError(
            "k 大于索引覆盖的行数",
            k=k,
            num_indexed_rows=n,
            hint="索引只覆盖已建索引的行；新写入的行需要重建或走 delta 索引",
        )
    return k


def as_mask(mask: Any, n: int) -> Optional[np.ndarray]:
    """把删除行掩码规整成 ``(n,)`` 的 bool 数组；``None`` 表示不过滤。"""

    if mask is None:
        return None
    m = np.asarray(mask, dtype=bool).ravel()
    if m.shape[0] != n:
        raise QueryError(
            "掩码长度与索引行数不一致", mask_len=int(m.shape[0]), rows=int(n)
        )
    return m


def default_num_partitions(n: int) -> int:
    """IVF 分区数的经验默认值：``sqrt(n)``，上限 256。

    **这是我们自己的启发式，不是真实 LanceDB 的做法。** 真实库是按
    目标分区大小反推的：``clamp(num_rows / target_partition_size, 1, 4096)``
    （实测 2 万行 → 2 个分区、20 万行 → 24 个，而 ``sqrt(N)`` 会给出
    141 和 447）。这里用 ``sqrt`` 是因为它在教学规模上够用、而且
    一行就能讲明白；差别写在第 12 章 §12.5 与第 13 章 §13.4 里。
    """

    return int(max(1, min(256, round(math.sqrt(max(1, n))))))


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------


def topk_result(
    scores: np.ndarray,
    row_ids: np.ndarray,
    k: int,
    *,
    approximate: bool = True,
    stats: Optional[Mapping[str, Any]] = None,
) -> SearchResult:
    """把 ``(q, n_candidates)`` 的候选分数拼成 :class:`SearchResult`。

    两个必须处理的细节：

    * 掩码把被过滤掉的候选分数设成了 ``+inf``，取 top-k 时它们可能被选进来
      填充空位 —— 这里用 ``isfinite`` 把它们转成行号 ``-1``（表示"没有结果"）；
    * 行号是**表的行号**，不是候选数组的下标：``row_ids[position]``。
    """

    vals, idx = top_k_from_matrix(np.asarray(scores, dtype=np.float32), k)
    out_d = np.ascontiguousarray(vals, dtype=np.float32)
    out_i = np.full(idx.shape, -1, dtype=np.int64)
    valid = (idx >= 0) & np.isfinite(out_d)
    if valid.any():
        safe = np.where(valid, idx, 0)
        out_i[valid] = row_ids[safe[valid]].astype(np.int64)
    return SearchResult(out_d, out_i, approximate=approximate, stats=dict(stats or {}))


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def save_arrays(store: IndexStore, arrays: Mapping[str, np.ndarray]) -> List[str]:
    """把一组命名数组写进索引目录，返回文件名列表。"""

    names = list(arrays)
    for name in names:
        store.write_numpy(name, np.asarray(arrays[name]))
    return names


def total_nbytes(arrays: Mapping[str, np.ndarray]) -> int:
    """一组数组的真实字节数（``size_bytes`` 就是它们的和，不是估出来的）。"""

    return int(sum(np.asarray(a).nbytes for a in arrays.values()))


def partition_size_stats(sizes: np.ndarray) -> Dict[str, Any]:
    """倒排链表的长度分布 —— 判断"桶均不均匀"就看这几个数。

    ``imbalance = max / mean``：真实数据里 k-means 的桶往往严重不均衡，
    单个超大桶会让最坏情况查询时间退化到接近暴力检索，所以这个指标
    是运维时最先看的东西。
    """

    s = np.asarray(sizes, dtype=np.float64)
    if s.size == 0:
        return {
            "min_partition_size": 0,
            "max_partition_size": 0,
            "median_partition_size": 0.0,
            "avg_partition_size": 0.0,
            "imbalance": 0.0,
            "empty_partitions": 0,
        }
    mean = float(s.mean())
    return {
        "min_partition_size": int(s.min()),
        "max_partition_size": int(s.max()),
        "median_partition_size": float(np.median(s)),
        "avg_partition_size": mean,
        "imbalance": float(s.max() / mean) if mean > 0 else 0.0,
        "empty_partitions": int((s == 0).sum()),
    }


# ---------------------------------------------------------------------------
# 距离内核
# ---------------------------------------------------------------------------


class DistanceKernel:
    """"固定矩阵 + 变化查询"场景下的距离计算器。

    用法::

        kernel = DistanceKernel(vectors, metric)     # cosine 会在这里归一化
        q = kernel.prepare_query(query)              # 查询向量走同一条预处理
        d = kernel.to_positions(q, positions)        # 到指定行号的精确距离

    为什么值得单独抽出来：HNSW 建图时每次插入要算几百次"查询到 16 个邻居"
    的距离，每次都是一个长度 16 的小向量化调用。``pairwise_distances``
    里那些"归一化矩阵""算 ||a||²"的开销在这种规模下完全盖过了真正的计算。
    """

    __slots__ = ("matrix", "metric_name", "normalize_queries")

    def __init__(self, matrix: np.ndarray, metric: Metric) -> None:
        self.metric_name = metric.name
        self.normalize_queries = bool(metric.needs_normalization)
        m = np.ascontiguousarray(np.asarray(matrix, dtype=np.float32))
        if self.normalize_queries:
            m = np.ascontiguousarray(normalize(m))
        self.matrix = m

    # -- 查询侧 -----------------------------------------------------------
    def set_matrix(self, matrix: np.ndarray) -> None:
        self.matrix = np.ascontiguousarray(np.asarray(matrix, dtype=np.float32))
        if self.normalize_queries:
            self.matrix = np.ascontiguousarray(normalize(self.matrix))

    def prepare_query(self, query: Any) -> np.ndarray:
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if self.normalize_queries:
            q = normalize(q.reshape(1, -1)).reshape(-1)
        return q

    # -- 距离 -------------------------------------------------------------
    def to_positions(self, query: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """已预处理的查询向量 → 到 ``positions`` 这些行的距离。"""

        pos = np.asarray(positions, dtype=np.int64)
        if pos.size == 0:
            return np.zeros(0, dtype=np.float32)
        sub = self.matrix[pos]
        if self.metric_name == "dot":
            return np.asarray(-(sub @ query), dtype=np.float32)
        diff = sub - query
        sq = np.einsum("ij,ij->i", diff, diff)
        if self.metric_name == "cosine":
            # 单位向量上 1 - cos = ||a - b||² / 2，数值和 cosine_distance 一致
            sq = sq * 0.5
        return np.asarray(sq, dtype=np.float32)

    def pairwise(self, positions: np.ndarray) -> np.ndarray:
        """``positions`` 内部的两两距离矩阵（用于 HNSW 的多样性剪枝）。"""

        pos = np.asarray(positions, dtype=np.int64)
        sub = self.matrix[pos]
        g = sub @ sub.T
        if self.metric_name == "dot":
            return np.asarray(-g, dtype=np.float32)
        diag = np.diag(g)
        d = diag[:, None] + diag[None, :] - 2.0 * g
        d = np.maximum(d, 0.0)  # 浮点误差可能产生 -1e-7
        if self.metric_name == "cosine":
            d = d * 0.5
        return np.asarray(d, dtype=np.float32)

    def exact_to_positions(
        self, queries: np.ndarray, positions: np.ndarray
    ) -> np.ndarray:
        """走规范实现 :func:`pairwise_distances` 的精确距离（用于重排）。

        刻意和 :meth:`to_positions` 分开：``to_positions`` 为了速度换了
        等价公式，浮点结果的第 7 位可能不同；"精确重排"路径必须和暴力
        检索逐位一致，否则"重排后是否等于暴力"这种测试会随机失败。
        """

        sub = self.matrix[np.asarray(positions, dtype=np.int64)]
        return np.asarray(
            pairwise_distances(sub, queries, self.metric_name), dtype=np.float32
        )
