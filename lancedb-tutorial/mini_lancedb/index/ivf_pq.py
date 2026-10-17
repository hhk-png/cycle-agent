"""``IVF_PQ``：倒排分区 + 乘积量化，LanceDB 里最常用的组合索引。

它把两个正交的技术叠在一起，**解决的是两个不同的问题**：

* IVF 解决"要算的距离太多"：把候选集从 n 缩到 ``nprobes/P · n``；
* PQ 解决"每条向量太大"：把 4d 字节压到 M 字节。

所以这两个参数是独立的旋钮：``num_partitions``/``nprobes`` 调"看多少数据"，
``num_sub_vectors``/``num_bits`` 调"每条数据看多准"。调优时应该分开看 ——
先用 ``refine_factor`` 排除量化误差，再调 ``nprobes``，反过来做会同时
改变两个误差源，根本读不出结论。

## 残差向量：一个被低估的细节

朴素的 PQ 直接量化原向量 ``x``，但 ``x`` 的分布是**多峰**的（数据本来就
分簇），而 k-means 的码本只有 K 个质心，它得把全部簇的形态塞进一张码本里。
更好的做法是先减掉它所属分区的质心：

    residual = x - centroid[partition(x)]

残差的分布集中在原点附近、方差小得多，同样的 K 个码本能描述得精细得多。
这就是 :attr:`residual`（对应 LanceDB 的 ``residual_vector`` 参数，默认 True）
在做的事。它的代价是多存一份质心（可忽略），以及查询侧的每一次打分
都要按分区减一次质心。

注意残差**只在有粗量化时才有意义**：没有 IVF 分区就无处可减。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

from ._common import as_matrix, as_row_ids, prepare_matrix, resolve_metric
from .base import IndexMetadata, IndexStore
from .ivf import IvfBase
from .pq import ProductQuantizer

__all__ = ["IvfPqIndex"]


def _default_sub_vectors(dim: int) -> int:
    """默认子向量个数：``d`` 能被 8 整除就用 8，否则退到 4/2/1。

    ``num_sub_vectors`` 必须整除维度（每个子空间宽度一致才能建码本），
    所以这里不能随便取 ``d/4`` 之类的值。
    """

    for cand in (8, 4, 2):
        if dim % cand == 0:
            return cand
    return 1


def _default_num_bits(n: int) -> int:
    """默认位数：码本大小 ``2**num_bits`` 不能超过训练样本数。"""

    if n >= 256:
        return 8
    if n >= 16:
        return 4
    if n >= 4:
        return 2
    return 1


class IvfPqIndex(IvfBase):
    """``IVF_PQ``：粗量化 + （残差）乘积量化。"""

    type_name = "IVF_PQ"
    exact_candidates = False

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        self.pq = ProductQuantizer()
        #: ``(n, num_sub_vectors)`` 的 uint8 编码
        self.codes: np.ndarray = np.zeros((0, 0), dtype=np.uint8)
        #: True = 量化的是 ``x - 所属分区质心``（LanceDB 的默认行为）
        self.residual: bool = True

    # -- 属性 -------------------------------------------------------------
    @property
    def num_sub_vectors(self) -> int:
        return int(self.pq.num_sub_vectors)

    @property
    def num_bits(self) -> int:
        return int(self.pq.num_bits)

    # -- 训练目标 ---------------------------------------------------------
    def _pq_targets(self, matrix: np.ndarray, assign: np.ndarray) -> np.ndarray:
        """PQ 真正去量化的那个向量：残差或原向量。"""

        if not self.residual:
            return matrix
        if self.num_partitions == 0:
            return matrix
        return matrix - self.centroids[assign]

    def _reconstruct(self, codes: np.ndarray, assign: Optional[np.ndarray] = None) -> np.ndarray:
        """编码 → 重建向量。残差模式下要把分区质心加回去。"""

        decoded = self.pq.decode(codes)
        if self.residual and self.num_partitions:
            idx = self.assignments if assign is None else assign
            decoded = decoded + self.centroids[np.asarray(idx, dtype=np.int64)]
        return np.ascontiguousarray(decoded, dtype=np.float32)

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "IvfPqIndex":
        metric = resolve_metric(metadata)
        params = dict(metadata.params)
        matrix = prepare_matrix(as_matrix(data), metric)
        ids = as_row_ids(row_ids, matrix.shape[0])
        n, d = matrix.shape

        index = cls(metadata, metric)
        index.vectors = matrix
        index.row_ids = ids
        index.residual = bool(params.get("residual_vector", True))

        # 1) 先做 IVF 的粗量化 —— 残差依赖它，所以顺序不能反
        centroids, inertia, iters, partitions = index._train_partitions(matrix)
        index.centroids = np.ascontiguousarray(centroids, dtype=np.float32)
        index.kmeans_iterations = int(iters)
        index.train_inertia = float(inertia)
        index._build_partitions()  # 算出 assignments + CSR

        # 2) 再训练 PQ（在残差或原向量上）
        num_sub_vectors = int(
            params.get("num_sub_vectors", _default_sub_vectors(int(d)))
        )
        num_bits = int(params.get("num_bits", _default_num_bits(int(n))))
        targets = index._pq_targets(matrix, index.assignments)
        index.pq.train(
            targets,
            num_sub_vectors=num_sub_vectors,
            num_bits=num_bits,
            max_iters=int(params.get("max_iters", 25)),
            sample_size=params.get("sample_size"),
            seed=int(params.get("seed", 0)),
        )
        index.codes = index.pq.encode(targets)
        index._record_details(partitions)
        return index

    # -- 打分 -------------------------------------------------------------
    def _cheap_scores(self, query: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """候选集上的 ADC 打分。

        残差模式下每个分区的**查询残差不同**（各自减自己的质心），所以
        不能只建一张表，得按分区分组、每组建一张 ``(M, K)`` 的表。
        这正是残差 PQ 在查询侧多出来的那点成本。
        """

        positions = np.asarray(positions, dtype=np.int64)
        q = self._prepare_query(query)
        parts = self._partition_of(positions)
        scores = np.empty(positions.shape[0], dtype=np.float32)
        for p in np.unique(parts):
            sel = parts == p
            sub_pos = positions[sel]
            q_target = (
                q - self.centroids[int(p)] if self.residual else q
            )
            table = self.pq.compute_distance_table(q_target)
            sub_codes = self.codes[sub_pos]
            acc = np.zeros(sub_pos.shape[0], dtype=np.float32)
            for s in range(self.num_sub_vectors):
                acc += table[s][sub_codes[:, s].astype(np.int64)]
            scores[sel] = acc
        return scores

    # -- 维护 -------------------------------------------------------------
    def _encode_append(self, data: np.ndarray, new_assign: np.ndarray) -> None:
        targets = self._pq_targets(data, new_assign)
        self.codes = np.concatenate([self.codes, self.pq.encode(targets)], axis=0)

    def _remap_state(self, keep: np.ndarray) -> None:
        self.codes = self.codes[keep]

    # -- 持久化 -----------------------------------------------------------
    def _persisted_arrays(self) -> Dict[str, np.ndarray]:
        arrays = dict(super()._persisted_arrays())
        arrays.update(self.pq.state_arrays())
        arrays["pq_codes.npy"] = self.codes
        return arrays

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "IvfPqIndex":
        index = cls(metadata, resolve_metric(metadata))
        index._load_common(store, metadata)
        params = dict(metadata.params)
        index.residual = bool(params.get("residual_vector", True))
        index.pq = ProductQuantizer.from_codebooks(
            store.read_numpy("pq_codebooks.npy"),
            num_bits=int(params.get("num_bits", 8)),
        )
        index.codes = np.asarray(store.read_numpy("pq_codes.npy"), dtype=np.uint8)
        return index

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        info = super().stats()
        code_bytes = int(self.codes.nbytes)
        raw_bytes = int(self.vectors.size * 4)
        info.update(
            {
                "num_sub_vectors": self.num_sub_vectors,
                "num_bits": self.num_bits,
                "num_codes_per_sub_vector": int(self.pq.num_codes),
                "sub_dim": int(self.pq.sub_dim),
                "residual_vector": bool(self.residual),
                "code_bytes_per_vector": int(self.pq.code_bytes_per_vector),
                "code_bytes": code_bytes,
                # 压缩比只看"编码 vs 原始浮点"，这是 PQ 本身的功劳；
                # size_bytes 里还包含为了精确重排保留的原始向量副本。
                "compression_ratio": (raw_bytes / code_bytes) if code_bytes else 1.0,
                "codebook_bytes": self.pq.size_bytes(),
            }
        )
        return info
