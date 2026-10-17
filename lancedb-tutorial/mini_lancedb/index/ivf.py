"""IVF（倒排文件）系列索引的骨架，以及 ``IVF_FLAT``。

IVF 的想法非常朴素，但它是所有"亿级向量检索"系统的地基：

1. **粗量化**：对全量向量跑一次 k-means，得到 ``num_partitions`` 个质心。
   每个向量被挂到"离它最近的那个质心"的倒排链表上；
2. **查询时只探几个桶**：拿查询向量和所有质心比一遍（这一步是
   ``O(P·d)``，和 n 无关），挑最近的 ``nprobes`` 个桶，只在桶内算距离。

代价是"真正的最近邻可能落在没探的桶里"——这就是召回率的全部来源。
它把 ``O(n·d)`` 变成 ``O(P·d + nprobes/P · n · d)``，而精度损失通常很小，
因为 k-means 的桶在空间上确实是聚簇的。

**为什么 ``nprobes`` 是唯一的精度旋钮，而且很划算。**
粗量化只用了"向量到质心的距离"这一个比特级的信息，桶边界上的误判
是主要误差源。多探一个桶的代价是线性的，而收益在初期非常大
（``nprobes`` 从 1 到 8 通常能拿回一半以上的召回率损失）。

本模块的 :class:`IvfBase` 只负责"分区"这件事，把"候选集上怎么打分"
留给子类的 :meth:`IvfBase._cheap_scores`：``IVF_FLAT`` 用精确距离、
``IVF_SQ`` 用标量量化查表、``IVF_PQ`` 用乘积量化的 ADC。三种打分的
工程差异巨大，但"探哪些桶、怎么重排、怎么过滤删除行、怎么增量"
的骨架完全一样 —— 这正是把它抽出来做基类的理由。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

from ..errors import IndexBuildError
from ._common import (
    DEFAULT_NPROBES,
    as_mask,
    as_matrix,
    as_row_ids,
    check_k,
    check_queries,
    default_num_partitions,
    partition_size_stats,
    prepare_matrix,
    resolve_metric,
    save_arrays,
    total_nbytes,
)
from .base import (
    IndexMetadata,
    IndexStore,
    SearchResult,
    VectorIndex,
    require_positive,
)
from .metric import normalize, pairwise_distances
from .pq import kmeans

__all__ = ["IvfBase", "IvfIndex"]


class IvfBase(VectorIndex):
    """IVF 家族共享的部分：分区、候选集、精确重排、增量与压实。

    子类必须提供：

    * :attr:`exact_candidates` —— 候选集上的打分是不是精确的
      （决定 ``refine_factor`` 有没有意义）；
    * :meth:`_cheap_scores` —— 候选集上的（近似）打分；
    * :meth:`_encode_append` / :meth:`_remap_state` —— 自己的编码怎么跟着追加/删除走。
    """

    type_name = ""
    #: 子类的候选集打分是否精确。IVF_FLAT 是 True，量化系列是 False。
    exact_candidates: bool = False

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        #: 粗量化质心 ``(num_partitions, d)``
        self.centroids: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        #: 原始向量（或归一化后的副本）。
        #:
        #: 真实 LanceDB 的索引里**没有**这一份 —— 精确重排时它从数据文件
        #: 回表取原始向量。mini 实现没有独立的数据层（索引是自包含的），
        #: 所以这里保留一份副本，它同时被 append / remap / 精确重排复用。
        #: 代价是索引变大；``stats()`` 里会用 ``code_bytes`` 单独报告
        #: 量化编码到底多大，``compression_ratio`` 也只看这一项。
        self.vectors: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        #: CSR 形式的倒排链表：第 p 个桶是 members[offsets[p]:offsets[p+1]]
        self.partition_offsets: np.ndarray = np.zeros(1, dtype=np.int64)
        self.partition_members: np.ndarray = np.zeros(0, dtype=np.int64)
        #: 每个位置属于哪个桶（**派生数据**，不落盘，load 时重建）
        self.assignments: np.ndarray = np.zeros(0, dtype=np.int64)
        self.kmeans_iterations: int = 0
        self.train_inertia: float = 0.0
        self.default_nprobes: int = DEFAULT_NPROBES

    # -- 属性 -------------------------------------------------------------
    @property
    def dim(self) -> int:
        if self.vectors.size:
            return int(self.vectors.shape[1])
        return int(self.centroids.shape[1]) if self.centroids.ndim == 2 else 0

    @property
    def num_partitions(self) -> int:
        return int(self.centroids.shape[0])

    # -- 分区 -------------------------------------------------------------
    def _train_partitions(self, matrix: np.ndarray) -> Tuple[np.ndarray, float, int, int]:
        """跑粗量化的 k-means。返回 ``(centroids, inertia, iters, 实际分区数)``。"""

        n = int(matrix.shape[0])
        params = dict(self.metadata.params)
        wanted = require_positive(params, "num_partitions", default_num_partitions(n))
        if wanted > n:
            # 桶数超过样本数时必然有空桶，而且"一行一个桶"退化成了全量扫描。
            raise IndexBuildError(
                "num_partitions 大于索引行数",
                num_partitions=wanted,
                rows=n,
                hint="分区数不应超过数据行数",
            )
        max_iters = require_positive(params, "max_iters", 25)
        sample_rate = float(params.get("training_sample_rate", 1.0))
        seed = int(params.get("seed", 0))
        centroids, inertia, iters = kmeans(
            matrix,
            wanted,
            max_iters=max_iters,
            seed=seed,
            sample_rate=sample_rate,
            metric=self.metric.name,
        )
        return centroids, inertia, iters, wanted

    def _assign_to_centroids(self, matrix: np.ndarray) -> np.ndarray:
        """把一批向量分到最近的桶。这是"粗量化"本身。"""

        if self.num_partitions == 0:
            return np.zeros(matrix.shape[0], dtype=np.int64)
        d = pairwise_distances(self.centroids, matrix, self.metric.name)  # (n, P)
        return np.argmin(d, axis=1).astype(np.int64)

    def _build_partitions(self) -> None:
        self.assignments = self._assign_to_centroids(self.vectors)
        self._rebuild_csr()

    def _rebuild_csr(self) -> None:
        """把"每个位置属于哪个桶"整理成 CSR。

        CSR（offsets + 连续 members）而不是"每个桶一个 list"，是因为
        候选集查询要的是"把几个桶的成员拼起来"，CSR 上就是两次切片
        加一次 ``concatenate``；而且它天然可序列化成两个数组。
        """

        offsets = self.partition_offsets
        counts = np.bincount(self.assignments, minlength=self.num_partitions).astype(np.int64)
        # stable 排序保证**同一桶内的成员按位置升序**。这不只是好看：
        # IVF_HNSW 的每分区图用局部下标寻址，增量插入时"新点的局部下标
        # 必然最大"这条性质就依赖它。
        order = np.argsort(self.assignments, kind="stable").astype(np.int64)
        self.partition_members = order
        self.partition_offsets = np.concatenate(
            [np.zeros(1, dtype=np.int64), np.cumsum(counts)]
        ).astype(np.int64)

    def _partition_members(self, p: int) -> np.ndarray:
        lo = int(self.partition_offsets[p])
        hi = int(self.partition_offsets[p + 1])
        return self.partition_members[lo:hi]

    def _gather(self, parts: np.ndarray) -> np.ndarray:
        """把若干个桶的成员拼成一个候选位置数组（顺序无关）。"""

        if parts.size == 0:
            return np.zeros(0, dtype=np.int64)
        if parts.size == 1:
            return self._partition_members(int(parts[0]))
        return np.concatenate([self._partition_members(int(p)) for p in parts])

    def _partition_of(self, positions: np.ndarray) -> np.ndarray:
        """**位置** → 它属于哪个桶。

        注意这里必须查 :attr:`assignments`，而**不能**对 CSR 的
        ``partition_offsets`` 做二分查找：CSR 里的下标是"桶内第几个成员"
        的序号，和全局位置差一个置换。两者混用会静默地把候选分到错误的
        桶上 —— 距离全错，但不报任何异常（这个坑在实现时真的踩过一次）。
        """

        return self.assignments[np.asarray(positions, dtype=np.int64)]

    def _partition_sizes(self) -> np.ndarray:
        return np.diff(self.partition_offsets)

    def _record_details(self, partitions: int) -> None:
        """把运行时才知道的细节写回元数据（``index_stats()`` 直接读它）。

        分区数、平均桶长、k-means 迭代次数这些量在 build 之前是算不出来的，
        但它们又必须能被"只看 manifest 就展示索引信息"的路径读到，所以
        统一记在 ``metadata.details`` 里。
        """

        self.metadata.num_indexed_rows = int(self.row_ids.shape[0])
        sizes = self._partition_sizes()
        self.metadata.details.update(
            {
                "metric_type": self.metric.name,
                "dim": int(self.dim),
                "num_partitions": int(self.num_partitions),
                "avg_partition_size": float(sizes.mean()) if sizes.size else 0.0,
                "kmeans_iterations": int(self.kmeans_iterations),
                "train_inertia": float(self.train_inertia),
            }
        )
        # 分区数写回 params：load 之后不用重新训练也能知道桶的规模。
        self.metadata.params.setdefault("num_partitions", int(partitions))
        params = dict(self.metadata.params)
        self.default_nprobes = max(
            1,
            min(
                int(params.get("nprobes", DEFAULT_NPROBES)),
                max(1, self.num_partitions),
            ),
        )

    # -- 打分（子类实现） -------------------------------------------------
    def _prepare_query(self, query: np.ndarray) -> np.ndarray:
        """把查询拉进"索引空间"：cosine 索引里库侧存的是归一化向量，
        查询也必须归一化，否则量化的查表距离和精确距离会给出不同的排序。
        """

        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if self.metric.needs_normalization:
            q = normalize(q.reshape(1, -1)).reshape(-1)
        return q

    def _cheap_scores(self, query: np.ndarray, positions: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def _exact_scores(self, query: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """精确打分。刻意走规范的 ``pairwise_distances``，与暴力检索逐位一致。"""

        sub = self.vectors[np.asarray(positions, dtype=np.int64)]
        return np.asarray(
            pairwise_distances(sub, query.reshape(1, -1), self.metric.name), dtype=np.float32
        )[0]

    # -- 查询 -------------------------------------------------------------
    def search(
        self,
        queries: np.ndarray,
        k: int,
        *,
        mask: Optional[np.ndarray] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> SearchResult:
        """探桶 → 候选集打分 → 可选精确重排 → 取 Top-K。

        ``refine_factor`` 的语义（和 LanceDB 一致）：先用**便宜的表示**
        取出 ``k * refine_factor`` 个候选，再对这些候选算**精确距离**重排。
        为什么值得这么做：量化打分里"排序靠前"的部分已经基本可信，
        真正容易错的是第 ``k`` 名附近的边界（那里几条候选的距离只差
        零点几）。多取 2~4 倍候选做一次精确重排，通常能拿回量化损失掉的
        一半以上召回率，而精确重排的代价只有 ``O(k·refine·d)``。
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
        mask_arr = as_mask(mask, self.num_indexed_rows)

        # 查询 × 质心：一次 (q, P) 的距离，和 n 无关
        centroid_d = np.asarray(
            pairwise_distances(self.centroids, x, self.metric.name), dtype=np.float32
        )

        for i in range(q_count):
            if nprobes >= self.num_partitions:
                parts = np.arange(self.num_partitions, dtype=np.int64)
            else:
                parts = np.argpartition(centroid_d[i], nprobes - 1)[:nprobes]
            cand = self._gather(parts)
            if cand.size == 0:
                continue
            if mask_arr is not None:
                # 删除行过滤（也是 prefilter 的语义）：被删的行连候选都当不了。
                cand = cand[mask_arr[cand]]
                if cand.size == 0:
                    continue
            scores = self._cheap_scores(x[i], cand)
            if refine > 1 and not self.exact_candidates:
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

        # 探满所有桶时，IVF_FLAT 的候选集就是全集 → 结果精确。
        exact_scan = self.exact_candidates and nprobes >= self.num_partitions
        return SearchResult(
            out_d,
            out_i,
            approximate=not exact_scan,
            stats={
                "nprobes": int(nprobes),
                "refine_factor": int(refine),
                "num_partitions": int(self.num_partitions),
                "exact_scan": bool(exact_scan),
            },
        )

    # -- 维护 -------------------------------------------------------------
    def append(self, data: np.ndarray, row_ids: np.ndarray) -> None:
        """增量：新向量挂到**已有**质心上，不重跑 k-means。

        为什么不在每次写入时重训：重训会让所有旧向量的桶归属全部变化，
        等于重建整个索引。LanceDB 里这件事由"主索引 + delta 索引"和
        后台的 ``optimize()`` 分工完成；这里把"挂到旧质心"的行为直接
        暴露出来，方便观察增量索引的精度退化。
        """

        matrix = prepare_matrix(as_matrix(data), self.metric)
        if self.dim and matrix.shape[1] != self.dim:
            raise IndexBuildError(
                "追加数据的维度与索引不一致",
                appended_dim=int(matrix.shape[1]),
                index_dim=self.dim,
            )
        ids = as_row_ids(row_ids, matrix.shape[0])
        new_assign = self._assign_to_centroids(matrix)
        self.vectors = np.concatenate([self.vectors, matrix], axis=0)
        self.row_ids = np.concatenate([self.row_ids, ids])
        self.assignments = np.concatenate([self.assignments, new_assign])
        self._encode_append(matrix, new_assign)
        self._rebuild_csr()
        self.metadata.num_indexed_rows = int(self.row_ids.shape[0])

    def remap(self, old_to_new: np.ndarray) -> None:
        old = np.asarray(old_to_new, dtype=np.int64)
        keep = old[self.row_ids.astype(np.int64)] >= 0
        self.vectors = self.vectors[keep]
        self.row_ids = old[self.row_ids.astype(np.int64)[keep]].astype(np.uint64)
        self.assignments = self.assignments[keep]
        self._remap_state(keep)
        self._rebuild_csr()
        self.metadata.num_indexed_rows = int(self.row_ids.shape[0])

    def _encode_append(self, data: np.ndarray, new_assign: np.ndarray) -> None:
        """子类钩子：把新数据的编码/索引结构补上。基类（IVF_FLAT）无事可做。"""

    def _remap_state(self, keep: np.ndarray) -> None:
        """子类钩子：把每行一份的编码结构按掩码裁掉。"""

    # -- 持久化 -----------------------------------------------------------
    def _persisted_arrays(self) -> Dict[str, np.ndarray]:
        return {
            "centroids.npy": self.centroids,
            "vectors.npy": self.vectors,
            "row_ids.npy": self.row_ids,
            "partition_offsets.npy": self.partition_offsets,
            "partition_members.npy": self.partition_members,
        }

    def save(self, store: IndexStore) -> List[str]:
        return save_arrays(store, self._persisted_arrays())

    def _load_common(self, store: IndexStore, metadata: IndexMetadata) -> None:
        self.centroids = np.ascontiguousarray(
            store.read_numpy("centroids.npy"), dtype=np.float32
        )
        self.vectors = np.ascontiguousarray(store.read_numpy("vectors.npy"), dtype=np.float32)
        self.row_ids = np.asarray(store.read_numpy("row_ids.npy"), dtype=np.uint64)
        self.partition_offsets = np.asarray(
            store.read_numpy("partition_offsets.npy"), dtype=np.int64
        )
        self.partition_members = np.asarray(
            store.read_numpy("partition_members.npy"), dtype=np.int64
        )
        # assignments 是派生量：从 CSR 反推，省下一份 8 字节/行的落盘数据。
        counts = np.diff(self.partition_offsets)
        labels = np.repeat(np.arange(counts.shape[0], dtype=np.int64), counts)
        self.assignments = np.zeros(self.partition_members.shape[0], dtype=np.int64)
        if self.partition_members.size:
            self.assignments[self.partition_members] = labels
        params = dict(metadata.params)
        self.default_nprobes = max(
            1, min(int(params.get("nprobes", DEFAULT_NPROBES)), max(1, self.num_partitions))
        )
        metadata.num_indexed_rows = int(self.row_ids.shape[0])
        self.metadata.details.setdefault("metric_type", self.metric.name)

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        arrays = self._persisted_arrays()
        sizes = self._partition_sizes()
        info: Dict[str, Any] = {
            "index_type": self.type_name,
            "num_indexed_rows": int(self.num_indexed_rows),
            "size_bytes": total_nbytes(arrays),
            "memory_bytes": int(self.vectors.nbytes) + int(self.centroids.nbytes),
            "num_partitions": int(self.num_partitions),
            "dim": self.dim,
            "metric_type": self.metric.name,
            "nprobes_default": int(self.default_nprobes),
            "kmeans_iterations": int(self.kmeans_iterations),
            "train_inertia": float(self.train_inertia),
            "vectors_kept": True,
            "persisted_files": sorted(arrays),
        }
        info.update(partition_size_stats(sizes))
        return info


class IvfIndex(IvfBase):
    """``IVF_FLAT``：分区 + 桶内精确距离。

    它存在的意义是**隔离召回率的两个来源**。``IVF_FLAT`` 的误差 100% 来自
    "探漏了桶"，没有任何量化误差。所以评测一个量化器时，先看
    ``IVF_FLAT`` 在同样 ``nprobes`` 下的召回率——那是这个参数下的天花板。
    """

    type_name = "IVF_FLAT"
    exact_candidates = True

    @classmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "IvfIndex":
        metric = resolve_metric(metadata)
        matrix = prepare_matrix(as_matrix(data), metric)
        ids = as_row_ids(row_ids, matrix.shape[0])
        index = cls(metadata, metric)
        index.vectors = matrix
        index.row_ids = ids
        centroids, inertia, iters, partitions = index._train_partitions(matrix)
        index.centroids = np.ascontiguousarray(centroids, dtype=np.float32)
        index.kmeans_iterations = int(iters)
        index.train_inertia = float(inertia)
        index._build_partitions()
        index._record_details(partitions)
        return index

    def _cheap_scores(self, query: np.ndarray, positions: np.ndarray) -> np.ndarray:
        # 桶内本来就是精确距离：所以 refine_factor 对 IVF_FLAT 没有意义
        # （重排的对象已经是精确分数了）。
        return self._exact_scores(query, positions)

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "IvfIndex":
        index = cls(metadata, resolve_metric(metadata))
        index._load_common(store, metadata)
        return index

    def stats(self) -> Dict[str, Any]:
        info = super().stats()
        info.update(
            {
                "compression_ratio": 1.0,
                "code_bytes": int(self.vectors.nbytes),
                "exact_candidates": True,
            }
        )
        return info
