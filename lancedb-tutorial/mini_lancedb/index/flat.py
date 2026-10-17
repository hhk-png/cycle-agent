"""FLAT —— 暴力精确检索，所有近似索引的"标准答案"。

FLAT 不做任何近似：它把整列向量原样存下来，查询时和每一条算一遍距离。
复杂度是 ``O(n·d)``，在千万级以上就没法用了，但它的三个作用无可替代：

1. **基准**：评测任何 ANN 索引的唯一硬指标就是"和 FLAT 的召回率"，
   ``recall_against_bruteforce`` 里的 ground truth 就是它；
2. **兜底**：过滤之后候选集已经很小、或者维度很低时，老老实实全量扫
   反而比走图的随机访存更快（这也是为什么真实 LanceDB 在
   ``prefilter`` 之后会退回暴力路径）；
3. **正确性参照**：其它索引的所有实现细节（行号映射、掩码、重排）
   都能拿它对照。

工程上唯一值得说的地方是 :meth:`FlatIndex.search` 里距离矩阵的形状：
``(q, n)``。它是**一次**矩阵运算而不是 q 次循环 —— 因为
``pairwise_distances`` 展开成了 ``||q||² - 2q·mᵀ + ||m||²``，本质是一次
``(q,d)×(d,n)`` 的 GEMM，这正是 BLAS 最擅长的形状。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import numpy as np

from ..errors import IndexBuildError
from ._common import (
    as_mask,
    as_matrix,
    as_row_ids,
    check_k,
    check_queries,
    prepare_matrix,
    resolve_metric,
    save_arrays,
    topk_result,
    total_nbytes,
)
from .base import IndexMetadata, IndexStore, SearchResult, VectorIndex
from .metric import pairwise_distances

__all__ = ["FlatIndex"]


class FlatIndex(VectorIndex):
    """精确暴力索引。``type_name = "FLAT"``。"""

    type_name = "FLAT"

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        #: 被索引的向量本体；cosine 情况下这里存的是**已归一化**的副本
        self.vectors: np.ndarray = np.zeros((0, 0), dtype=np.float32)

    # -- 属性 -------------------------------------------------------------
    @property
    def dim(self) -> int:
        return int(self.vectors.shape[1]) if self.vectors.ndim == 2 else 0

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "FlatIndex":
        metric = resolve_metric(metadata)
        matrix = prepare_matrix(as_matrix(data), metric)
        ids = as_row_ids(row_ids, matrix.shape[0])
        index = cls(metadata, metric)
        index.vectors = matrix
        index.row_ids = ids
        metadata.num_indexed_rows = int(matrix.shape[0])
        metadata.details["metric_type"] = metric.name
        metadata.details["dim"] = int(matrix.shape[1])
        return index

    # -- 查询 -------------------------------------------------------------
    def search(
        self,
        queries: np.ndarray,
        k: int,
        *,
        mask: Optional[np.ndarray] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> SearchResult:
        x = check_queries(queries, self.dim)
        k = check_k(k, self.num_indexed_rows)
        d = np.asarray(pairwise_distances(self.vectors, x, self.metric.name), dtype=np.float32)
        if mask is not None:
            m = as_mask(mask, self.num_indexed_rows)
            # 被删掉的行距离设成 +inf：它会排到最后，并在 topk_result 里
            # 被改写成行号 -1，而不是错误地返回一个已删除的行号。
            d = np.ascontiguousarray(np.where(m[None, :], d, np.inf), dtype=np.float32)
        return topk_result(d, self.row_ids, k, approximate=False, stats={"brute_force": True})

    # -- 维护 -------------------------------------------------------------
    def append(self, data: np.ndarray, row_ids: np.ndarray) -> None:
        """暴力索引天然支持增量：追加就是拼接矩阵。

        真实 LanceDB 里 FLAT 不是一种"建出来的索引"，而是没有索引时的
        默认路径；这里保留 append 是为了给 delta 索引的合并逻辑提供靶子。
        """

        matrix = prepare_matrix(as_matrix(data), self.metric)
        if self.dim and matrix.shape[1] != self.dim:
            raise IndexBuildError(
                "追加数据的维度与索引不一致",
                appended_dim=int(matrix.shape[1]),
                index_dim=self.dim,
            )
        ids = as_row_ids(row_ids, matrix.shape[0])
        self.vectors = np.concatenate([self.vectors, matrix], axis=0)
        self.row_ids = np.concatenate([self.row_ids, ids])
        self.metadata.num_indexed_rows = int(self.vectors.shape[0])

    def remap(self, old_to_new: np.ndarray) -> None:
        """压实后重映射行号；``old_to_new`` 里为 -1 的行被丢弃。"""

        old = np.asarray(old_to_new, dtype=np.int64)
        keep = old[self.row_ids.astype(np.int64)] >= 0
        self.vectors = self.vectors[keep]
        self.row_ids = old[self.row_ids.astype(np.int64)[keep]].astype(np.uint64)
        self.metadata.num_indexed_rows = int(self.vectors.shape[0])

    # -- 持久化 -----------------------------------------------------------
    def _persisted_arrays(self) -> Dict[str, np.ndarray]:
        """这个索引落盘时到底写了哪些数组 —— ``size_bytes`` 就是它们的真实和。"""

        return {"vectors.npy": self.vectors, "row_ids.npy": self.row_ids}

    def save(self, store: IndexStore) -> List[str]:
        return save_arrays(store, self._persisted_arrays())

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "FlatIndex":
        index = cls(metadata, resolve_metric(metadata))
        index.vectors = np.ascontiguousarray(store.read_numpy("vectors.npy"), dtype=np.float32)
        index.row_ids = np.asarray(store.read_numpy("row_ids.npy"), dtype=np.uint64)
        metadata.num_indexed_rows = int(index.vectors.shape[0])
        return index

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        arrays = self._persisted_arrays()
        raw_bytes = int(self.vectors.size * 4)
        return {
            "index_type": self.type_name,
            "num_indexed_rows": int(self.num_indexed_rows),
            "size_bytes": total_nbytes(arrays),
            "memory_bytes": raw_bytes,
            "dim": self.dim,
            "metric_type": self.metric.name,
            "exact": True,
            "compression_ratio": 1.0 if raw_bytes == 0 else raw_bytes / max(1, total_nbytes(arrays)),
            "persisted_files": sorted(arrays),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"FlatIndex(rows={self.num_indexed_rows}, dim={self.dim})"
