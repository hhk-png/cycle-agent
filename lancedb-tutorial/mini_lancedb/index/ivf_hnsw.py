"""``IVF_HNSW_SQ`` / ``IVF_HNSW_PQ``：分区内的 HNSW 图 + 量化编码。

这两个索引是 LanceDB 里"既要少读数据、又要少算距离"的那一档，它把
三件事叠起来，每一件负责一个数量级：

=====================================  =====================================
技术                                   解决的问题
=====================================  =====================================
IVF（粗量化）                          只看 ``nprobes/P`` 的数据
HNSW（分区内图）                        在分区内部用 ``O(log m)`` 而不是 ``O(m)``
SQ / PQ（编码）                         每条候选只占 1 字节/维 或 M 字节
=====================================  =====================================

**一个必须说清楚的实现选择：图建在"重建向量"上。**
``IVF_HNSW_SQ`` 的图不是建在原始向量上，而是建在 SQ 解码出来的向量上。
这样做是对的，理由是：

* SQ 是逐维量化，``||q - decode(c)||²`` 正好等于它的 ADC 距离；
* PQ 更漂亮 —— 残差 PQ 的 ADC 距离 ``||q - centroid_p - decode(c)||²``
  恰好就是"查询到重建向量的平方距离"。所以**用重建向量建图，图搜索
  过程中的距离与 ADC 打分逐位一致**。

代价是建图时要物化一份重建向量（每个分区一份）。真实 LanceDB 在
Rust 侧共享底层 buffer，这里用副本换代码清晰 —— ``stats()`` 里会
把 ``code_bytes``（真正的量化编码）和 ``graph_bytes`` 分开报，
免得把"临时物化的重建向量"算进压缩比里。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import numpy as np

from ._common import (
    as_mask,
    check_k,
    check_queries,
    partition_size_stats,
    save_arrays,
    total_nbytes,
)
from .hnsw import HnswGraph, build_partition_graphs
from .ivf_pq import IvfPqIndex
from .base import IndexMetadata, IndexStore, SearchResult
from .metric import pairwise_distances
from .sq import IvfSqIndex

__all__ = ["IvfHnswSqIndex", "IvfHnswPqIndex"]


class _IvfHnswMixin:
    """IVF 分区 + 每分区一张 HNSW 图的公共实现。

    刻意做成 mixin 而不是基类：它要和 ``IvfSqIndex`` / ``IvfPqIndex``
    组合，而这两个类各自的 build / 编码逻辑完全不需要改 ——
    图只是"分区内怎么找候选"的另一种实现。
    """

    type_name = ""

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)  # type: ignore[misc]
        #: 每个分区一张图；空分区是 :class:`HnswGraph` 的空实例
        self.graphs: List[HnswGraph] = []

    # -- 图 ---------------------------------------------------------------
    def _graph_vectors(self) -> np.ndarray:
        """图建在哪份向量上 —— 由量化器决定（SQ 用解码值，PQ 用重建值）。"""

        raise NotImplementedError

    def _graph_params(self) -> Dict[str, int]:
        params = dict(self.metadata.params)
        return {
            "m": int(params.get("m", 16)),
            "ef_construction": int(params.get("ef_construction", 100)),
            "seed": int(params.get("seed", 0)),
        }

    def _build_graphs(self) -> None:
        prepared = self._graph_vectors()
        cfg = self._graph_params()
        members = [self._partition_members(p) for p in range(self.num_partitions)]
        self.graphs = build_partition_graphs(
            prepared,
            members,
            m=cfg["m"],
            ef_construction=cfg["ef_construction"],
            metric=self.metric,
            seed=cfg["seed"],
        )

    @property
    def ef(self) -> int:
        return max(1, int(self.metadata.params.get("ef", 50)))

    # -- 查询 -------------------------------------------------------------
    def search(
        self,
        queries: np.ndarray,
        k: int,
        *,
        mask: Optional[np.ndarray] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> SearchResult:
        """探 ``nprobes`` 个分区 → 每个分区内跑图搜索 → 合并 → 可选精确重排。

        和图搜索配合的 ``ef`` 与 IVF 的 ``nprobes`` 是**两个正交的召回率
        旋钮**：一个决定"看几个分区的数据"，另一个决定"每个分区内看多深"。
        调优时先把 ``nprobes`` 拉满，看 ``ef`` 能到多少召回率，
        再回头砍 ``nprobes``。
        """

        p = dict(params or {})
        x = check_queries(queries, self.dim)
        k = check_k(k, self.num_indexed_rows)
        q_count = int(x.shape[0])
        out_d = np.full((q_count, k), np.inf, dtype=np.float32)
        out_i = np.full((q_count, k), -1, dtype=np.int64)
        if self.num_partitions == 0:
            return SearchResult(out_d, out_i, approximate=True)

        nprobes = int(p.get("nprobes", self.default_nprobes))
        nprobes = max(1, min(nprobes, self.num_partitions))
        refine_raw = p.get("refine_factor")
        refine = max(1, int(refine_raw)) if refine_raw else 1
        ef = max(int(p.get("ef", self.ef)), k * refine)
        mask_arr = as_mask(mask, self.num_indexed_rows)
        centroid_d = np.asarray(
            pairwise_distances(self.centroids, x, self.metric.name), dtype=np.float32
        )

        for i in range(q_count):
            if nprobes >= self.num_partitions:
                parts = np.arange(self.num_partitions, dtype=np.int64)
            else:
                parts = np.argpartition(centroid_d[i], nprobes - 1)[:nprobes]
            cand_parts: List[np.ndarray] = []
            score_parts: List[np.ndarray] = []
            for raw_p in parts:
                part = int(raw_p)
                members = self._partition_members(part)
                if members.size == 0:
                    continue
                graph = self.graphs[part]
                if graph.num_nodes == 0:
                    continue
                keep_local = None
                if mask_arr is not None:
                    keep_local = mask_arr[members]
                    if not bool(keep_local.any()):
                        continue
                want = min(int(members.size), max(ef, k))
                local, dist = graph.search(x[i], want, ef=max(want, ef), keep=keep_local)
                if local.size == 0:
                    continue
                cand_parts.append(members[local])
                score_parts.append(dist)
            if not cand_parts:
                continue
            cand = np.concatenate(cand_parts)
            scores = np.concatenate(score_parts)
            if refine > 1:
                take = min(cand.size, k * refine)
                if take < cand.size:
                    sel = np.argpartition(scores, take - 1)[:take]
                else:
                    sel = np.arange(cand.size)
                cand = cand[sel]
                scores = self._exact_scores(x[i], cand)
            take = min(k, scores.size)
            order = np.argsort(scores, kind="stable")[:take]
            out_d[i, :take] = scores[order]
            out_i[i, :take] = self.row_ids[cand[order]].astype(np.int64)

        return SearchResult(
            out_d,
            out_i,
            approximate=True,
            stats={
                "nprobes": int(nprobes),
                "ef": int(ef),
                "refine_factor": int(refine),
                "num_partitions": int(self.num_partitions),
                "num_graphs": int(len(self.graphs)),
            },
        )

    # -- 维护 -------------------------------------------------------------
    def _encode_append(self, data: np.ndarray, new_assign: np.ndarray) -> None:
        """先把编码交给量化器更新，再把新点插进各自分区的图。

        这条路径能成立的前提是"同一分区内新点的局部下标最大"，它由
        :meth:`IvfBase._rebuild_csr` 的 stable 排序保证。
        """

        super()._encode_append(data, new_assign)  # type: ignore[misc]
        if not self.graphs:
            return
        prepared = self._graph_vectors()
        old_n = int(self.vectors.shape[0]) - int(np.asarray(data).shape[0])
        for part in range(self.num_partitions):
            rows = np.flatnonzero(new_assign == part)
            if rows.size == 0:
                continue
            if part >= len(self.graphs):
                continue
            new_positions = old_n + rows
            self.graphs[part].append(np.ascontiguousarray(prepared[new_positions]))

    def remap(self, old_to_new: np.ndarray) -> None:
        """删点是图索引的痛点：这里直接按存活行重建所有分区图。"""

        super().remap(old_to_new)  # type: ignore[misc]
        self._build_graphs()

    # -- 持久化 -----------------------------------------------------------
    def _persisted_arrays(self) -> Dict[str, np.ndarray]:
        arrays = dict(super()._persisted_arrays())  # type: ignore[misc]
        for part, graph in enumerate(self.graphs):
            arrays.update(graph.save_arrays(prefix=f"graph{part}_"))
        arrays["num_graphs.npy"] = np.array([len(self.graphs)], dtype=np.int64)
        return arrays

    def save(self, store: IndexStore) -> List[str]:
        return save_arrays(store, self._persisted_arrays())

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> Any:
        index = super().load(store, metadata)  # type: ignore[misc]
        arrays = {name: store.read_numpy(name) for name in store.list()}
        # 图建在重建向量上，而重建向量可以由编码算回来 —— 所以不必落盘。
        prepared = index._graph_vectors()
        num_graphs = int(np.asarray(arrays["num_graphs.npy"])[0])
        members = [index._partition_members(p) for p in range(index.num_partitions)]
        index.graphs = []
        for part in range(num_graphs):
            prefix = f"graph{part}_"
            sub = {k[len(prefix) :]: v for k, v in arrays.items() if k.startswith(prefix)}
            mem = members[part] if part < len(members) else np.zeros(0, dtype=np.int64)
            index.graphs.append(
                HnswGraph.from_arrays(
                    sub,
                    vectors=np.ascontiguousarray(prepared[mem]) if len(mem) else np.zeros((0, index.dim), dtype=np.float32),
                    metric=index.metric,
                    prefix="",
                )
            )
        return index

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        info = super().stats()  # type: ignore[misc]
        graph_bytes = int(
            sum(total_nbytes(g.save_arrays()) for g in self.graphs)
        )
        degree = np.asarray(
            [g.stats()["avg_degree_level0"] for g in self.graphs if g.num_nodes], dtype=np.float64
        )
        sizes = self._partition_sizes()
        info.update(
            {
                "num_graphs": int(len(self.graphs)),
                "num_graph_levels": int(max((g.num_levels for g in self.graphs), default=0)),
                "max_graph_level": int(max((g.max_level for g in self.graphs), default=-1)),
                "avg_degree_level0": float(degree.mean()) if degree.size else 0.0,
                "graph_bytes": graph_bytes,
                "m": int(self.metadata.params.get("m", 16)),
                "ef_construction": int(self.metadata.params.get("ef_construction", 100)),
                "ef_default": int(self.ef),
                "nprobes_default": int(self.default_nprobes),
            }
        )
        info.update(partition_size_stats(sizes))
        return info


class IvfHnswSqIndex(_IvfHnswMixin, IvfSqIndex):
    """``IVF_HNSW_SQ``：分区 + 分区内 HNSW 图 + 标量量化。

    最"稳"的一档：SQ 的 4 倍压缩几乎不损失精度，图保证分区内不必线性扫。
    内存换召回率的性价比最高，缺点是它的内存占用随维度线性增长 ——
    高维（>1000）时人们会转向 ``IVF_HNSW_PQ``。
    """

    type_name = "IVF_HNSW_SQ"

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        self.graphs = []

    @classmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "IvfHnswSqIndex":
        index = super().build(data, row_ids, metadata=metadata)
        index._build_graphs()
        index.metadata.details["graph_vectors"] = "sq_decoded"
        return index

    def _graph_vectors(self) -> np.ndarray:
        """SQ 的解码值就是图上的点。"""

        if self.codes.size == 0:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self.sq.decode(self.codes)


class IvfHnswPqIndex(_IvfHnswMixin, IvfPqIndex):
    """``IVF_HNSW_PQ``：分区 + 分区内 HNSW 图 + 残差乘积量化。

    压缩最狠的一档（``d=768, M=96`` 时约 32 倍），适合"数据大到内存放
    不下、可以接受多一点召回率损失"的场景。残差 PQ 让它比朴素 PQ 在同一
    压缩率下准一大截，而每分区图弥补了纯 IVF_PQ "候选集大时线性扫"的问题。
    """

    type_name = "IVF_HNSW_PQ"

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        self.graphs = []

    @classmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "IvfHnswPqIndex":
        index = super().build(data, row_ids, metadata=metadata)
        index._build_graphs()
        index.metadata.details["graph_vectors"] = (
            "pq_reconstructed_residual" if index.residual else "pq_reconstructed"
        )
        return index

    def _graph_vectors(self) -> np.ndarray:
        """残差 PQ 的重建向量：``centroid_p + decode(codes)``。

        这个选择有一个漂亮的等价性：查询到重建向量的平方距离
        ``||q - centroid_p - decode(c)||²`` 就是残差 PQ 的 ADC 距离，
        所以图搜索里算出来的距离可以直接当 ADC 分数用，不需要第二遍扫描。
        """

        if self.codes.size == 0:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self._reconstruct(self.codes)
