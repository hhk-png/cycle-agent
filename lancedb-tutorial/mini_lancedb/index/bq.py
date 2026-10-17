"""二值量化（Binary Quantization, BQ）—— 1 bit/维 + 汉明距离。

BQ 是量化谱系里最极端的一档：每一维只留**符号**（正/负），不留幅度。

**32 倍的省法从哪来。** float32 的一个维度占 32 位，BQ 只占 1 位，
所以内存严格地小 32 倍；打包成 64 位字之后，一次 ``popcount(xor)``
能同时比较 64 个维度 —— 这带来两个后果：

* 内存：1 亿条 768 维向量从 300 GB 降到 9.6 GB，单机内存能装下了；
* 速度：汉明距离是 CPU 的单个指令（``POPCNT``），比浮点点积快
  5~10 倍，而且没有乘法。

**代价是召回率，而且它不能靠"多探几个分区"补回来。**
符号丢掉的是幅度信息：两个分量大小差 100 倍的向量，只要符号相同就
"看起来一样"。所以 BQ 的召回率通常只有 50%~80%，而且**提高它的唯一
有效手段是"用便宜的距离多取候选，再用浮点精排"**（:attr:`refine_factor`）。

这就是 RaBitQ 那一类工作的出发点：给每个向量再存一个标量
（这里是每行的平均绝对值 :attr:`scales`），于是重建值 ``sign(x)·scale``
保留了幅度尺度，精排时不必回表就能得到比"纯符号"好得多的近似距离。
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
    total_nbytes,
)
from .base import IndexMetadata, IndexStore, SearchResult, VectorIndex
from .metric import hamming_distance, pairwise_distances

__all__ = ["BinaryQuantizer", "BqIndex", "pack_signs", "unpack_signs"]

#: 一个 ``uint64`` 装 64 个维度（1 bit/维），所以"每字 64 维"。
BITS_PER_WORD = 64


def pack_signs(signs: np.ndarray) -> np.ndarray:
    """``(n, d)`` 的布尔符号位 → ``(n, ceil(d/64))`` 的 ``uint64``。

    位序用的是 ``bitorder="little"``：第 0 维落在第一个字节的最低位，
    于是小端机器上"第 k 维 = 第 k 个 bit"这一直觉成立。混用位序是
    二进制索引里最经典的静默 bug（数据写进去能读出来，但距离全错）。
    """

    s = np.asarray(signs, dtype=bool)
    if s.ndim == 1:
        s = s.reshape(1, -1)
    n, d = s.shape
    words = max(1, (d + BITS_PER_WORD - 1) // BITS_PER_WORD)
    packed = np.packbits(s, axis=1, bitorder="little")
    need = words * 8
    if packed.shape[1] < need:
        packed = np.pad(packed, ((0, 0), (0, need - packed.shape[1])))
    return np.ascontiguousarray(packed).view(np.uint64)


def unpack_signs(packed: np.ndarray, dim: int) -> np.ndarray:
    """反向操作，主要用于测试与调试。"""

    p = np.ascontiguousarray(np.asarray(packed))
    if p.ndim == 1:
        p = p.reshape(1, -1)
    raw = p.view(np.uint8)
    bits = np.unpackbits(raw, axis=1, bitorder="little")
    return bits[:, :dim].astype(bool)


class BinaryQuantizer:
    """1-bit 量化器：只记符号，另外附一个"尺度"标量供精排使用。"""

    def __init__(self, dim: int = 0) -> None:
        self.dim = int(dim)
        self.words = max(1, (int(dim) + BITS_PER_WORD - 1) // BITS_PER_WORD)
        #: 每行的平均绝对值 —— RaBitQ 风格的"尺度"，
        #: 让重建值 ``sign(x)·scale`` 不至于丢掉所有幅度信息。
        self.scales: np.ndarray = np.zeros(0, dtype=np.float32)

    # -- 编码 -------------------------------------------------------------
    @property
    def bits_per_dimension(self) -> int:
        return 1

    @property
    def compression_ratio(self) -> float:
        """相对 float32 的压缩比：32 倍（1 bit 对 32 bit）。"""

        return 32.0

    def encode(self, data: np.ndarray) -> np.ndarray:
        """``(n, d)`` float32 → ``(n, words)`` uint64。

        符号的判定用 ``x >= 0``：关键在于**查询侧和库侧用同一条规则**，
        否则所有比较都会系统性地偏移一位边界。
        """

        m = np.asarray(data, dtype=np.float32)
        if m.ndim == 1:
            m = m.reshape(1, -1)
        if self.dim and m.shape[1] != self.dim:
            raise IndexBuildError(
                "编码数据的维度与量化器不一致",
                data_dim=int(m.shape[1]),
                bq_dim=self.dim,
            )
        return pack_signs(m >= 0)

    def fit_scales(self, data: np.ndarray) -> np.ndarray:
        m = np.asarray(data, dtype=np.float32)
        if m.ndim == 1:
            m = m.reshape(1, -1)
        self.scales = np.mean(np.abs(m), axis=1).astype(np.float32)
        return self.scales

    def reconstruct(self, codes: np.ndarray) -> np.ndarray:
        """``sign · scale`` 的近似重建。BQ 只有 1 bit，重建必然很粗 ——
        它的意义在于**精排不需要回表**（见模块文档里的 RaBitQ 说明）。
        """

        signs = unpack_signs(codes, self.dim).astype(np.float32) * 2.0 - 1.0
        if self.scales.shape[0] != signs.shape[0]:
            return signs
        return np.ascontiguousarray(signs * self.scales[:, None], dtype=np.float32)

    # -- 距离 -------------------------------------------------------------
    def hamming(self, codes: np.ndarray, packed_query: np.ndarray) -> np.ndarray:
        """``(n, words)`` 编码到一条打包查询的汉明距离 ``(n,)``。

        ``hamming_distance`` 走的是位异或 + ``popcount``，不经过浮点，
        所以**不能**用 ``pairwise_distances``（它会把输入先转成 float32，
        把打包好的位串毁掉）。
        """

        return np.asarray(hamming_distance(codes, packed_query), dtype=np.int64)

    def state_arrays(self) -> Dict[str, np.ndarray]:
        return {"bq_scales.npy": self.scales}

    def size_bytes(self) -> int:
        return total_nbytes(self.state_arrays())


class BqIndex(VectorIndex):
    """``BQ``：1-bit 编码 + 汉明距离粗筛 + 浮点精排。

    它没有 IVF 分区也没有图 —— 因为汉明距离本身已经足够便宜，
    全量扫一遍的代价常常还在可接受范围内，而**多探分区救不了符号损失**。
    """

    type_name = "BQ"

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        self.quantizer = BinaryQuantizer(0)
        #: ``(n, words)`` 的 uint64 编码
        self.codes: np.ndarray = np.zeros((0, 0), dtype=np.uint64)
        #: 原始向量副本，用于精排（与 IVF 系列同理：mini 实现里没有数据层）
        self.vectors: np.ndarray = np.zeros((0, 0), dtype=np.float32)

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
    ) -> "BqIndex":
        metric = resolve_metric(metadata)
        matrix = prepare_matrix(as_matrix(data), metric)
        ids = as_row_ids(row_ids, matrix.shape[0])
        index = cls(metadata, metric)
        index.vectors = matrix
        index.row_ids = ids
        index.quantizer = BinaryQuantizer(int(matrix.shape[1]))
        index.quantizer.fit_scales(matrix)
        index.codes = index.quantizer.encode(matrix)
        metadata.num_indexed_rows = int(matrix.shape[0])
        metadata.details.update(
            {
                "metric_type": metric.name,
                "dim": int(matrix.shape[1]),
                "bits_per_dimension": 1,
                "words_per_vector": int(index.quantizer.words),
            }
        )
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
        """汉明粗筛 → 取 ``k·refine_factor`` 个候选 → 浮点精排。

        ``refine_factor`` 是 BQ 的**必需品而不是可选项**：纯汉明距离的
        排序在符号边界附近完全不可信，多取几倍候选做一次精确距离重排，
        通常能把召回率从 0.5 左右拉到 0.9 以上。
        """

        p = dict(params or {})
        x = check_queries(queries, self.dim)
        k = check_k(k, self.num_indexed_rows)
        refine = max(1, int(p.get("refine_factor", 1)))
        mask_arr = as_mask(mask, self.num_indexed_rows)

        out_d = np.full((x.shape[0], k), np.inf, dtype=np.float32)
        out_i = np.full((x.shape[0], k), -1, dtype=np.int64)
        n = self.num_indexed_rows
        for i in range(x.shape[0]):
            packed_q = self.quantizer.encode(x[i : i + 1])[0]
            # 汉明距离最大也就 d（几千），float32 存得下精确整数；
            # 转成浮点是为了能把"被掩码过滤掉"表示成 +inf。
            ham = self.quantizer.hamming(self.codes, packed_q).astype(np.float32)
            if mask_arr is not None:
                ham = np.where(mask_arr, ham, np.inf)
            take = min(n, k * refine)
            if take < n:
                sel = np.argpartition(ham, take - 1)[:take]
            else:
                sel = np.arange(n)
            if refine > 1:
                # 精排：用真实的浮点距离重新排序候选。这一步保证
                # "看到的候选集合是便宜的，排名是准确的"。
                scores = np.asarray(
                    pairwise_distances(self.vectors[sel], x[i : i + 1], self.metric.name),
                    dtype=np.float32,
                )[0]
                if mask_arr is not None:
                    scores = np.where(mask_arr[sel], scores, np.inf)
            else:
                scores = ham[sel]
            take = min(k, int(np.isfinite(scores).sum()), scores.size)
            order = np.argsort(scores, kind="stable")[:take]
            out_d[i, :take] = scores[order]
            out_i[i, :take] = self.row_ids[sel[order]].astype(np.int64)

        return SearchResult(
            out_d,
            out_i,
            approximate=True,
            stats={
                "refine_factor": int(refine),
                "distance_kind": "exact" if refine > 1 else "hamming",
                "bits_per_dimension": 1,
            },
        )

    # -- 维护 -------------------------------------------------------------
    def append(self, data: np.ndarray, row_ids: np.ndarray) -> None:
        """追加：新向量用**同一套符号规则**编码 —— BQ 不需要"训练"，
        所以增量写入没有任何精度退化，这是它相对 PQ 的一个实际优势。
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
        self.codes = np.concatenate([self.codes, self.quantizer.encode(matrix)], axis=0)
        scales = np.mean(np.abs(matrix), axis=1).astype(np.float32)
        self.quantizer.scales = np.concatenate([self.quantizer.scales, scales])
        self.metadata.num_indexed_rows = int(self.row_ids.shape[0])

    def remap(self, old_to_new: np.ndarray) -> None:
        old = np.asarray(old_to_new, dtype=np.int64)
        keep = old[self.row_ids.astype(np.int64)] >= 0
        self.vectors = self.vectors[keep]
        self.codes = self.codes[keep]
        self.quantizer.scales = self.quantizer.scales[keep]
        self.row_ids = old[self.row_ids.astype(np.int64)[keep]].astype(np.uint64)
        self.metadata.num_indexed_rows = int(self.row_ids.shape[0])

    # -- 持久化 -----------------------------------------------------------
    def _persisted_arrays(self) -> Dict[str, np.ndarray]:
        arrays = {
            "vectors.npy": self.vectors,
            "row_ids.npy": self.row_ids,
            "bq_codes.npy": self.codes,
        }
        arrays.update(self.quantizer.state_arrays())
        return arrays

    def save(self, store: IndexStore) -> List[str]:
        return save_arrays(store, self._persisted_arrays())

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "BqIndex":
        metric = resolve_metric(metadata)
        index = cls(metadata, metric)
        index.vectors = np.ascontiguousarray(store.read_numpy("vectors.npy"), dtype=np.float32)
        index.row_ids = np.asarray(store.read_numpy("row_ids.npy"), dtype=np.uint64)
        index.codes = np.ascontiguousarray(store.read_numpy("bq_codes.npy"), dtype=np.uint64)
        index.quantizer = BinaryQuantizer(int(index.vectors.shape[1]))
        index.quantizer.scales = np.asarray(store.read_numpy("bq_scales.npy"), dtype=np.float32)
        metadata.num_indexed_rows = int(index.row_ids.shape[0])
        return index

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        arrays = self._persisted_arrays()
        code_bytes = int(self.codes.nbytes)
        raw_bytes = int(self.vectors.size * 4)
        return {
            "index_type": self.type_name,
            "num_indexed_rows": int(self.num_indexed_rows),
            "size_bytes": total_nbytes(arrays),
            "memory_bytes": code_bytes,
            "dim": self.dim,
            "metric_type": self.metric.name,
            "num_bits": 1,
            "bits_per_dimension": 1,
            "words_per_vector": int(self.quantizer.words),
            "code_bytes": code_bytes,
            "code_bytes_per_vector": int(self.quantizer.words * 8),
            # BQ 的招牌数字：32 bit → 1 bit
            "compression_ratio": (raw_bytes / code_bytes) if code_bytes else 1.0,
            "vectors_kept": True,
            "persisted_files": sorted(arrays),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"BqIndex(rows={self.num_indexed_rows}, dim={self.dim})"
