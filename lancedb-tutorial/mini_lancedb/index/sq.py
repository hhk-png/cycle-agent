"""标量量化（Scalar Quantization, SQ）。

SQ 是"最笨但最划算"的量化：对**每一维**独立地记下 ``min`` 和 ``max``，
把浮点值线性映射到 ``2**num_bits`` 个整数格点上。
``num_bits=8`` 时每条向量从 ``4d`` 字节压到 ``d`` 字节（4 倍），
精度损失却通常小到看不出来 —— 原因很直观：8 bit 把一段区间切成 256 份，
误差上界只有 ``(max-min)/255/2``，而真实向量每一维的取值区间本来就不宽。

它和 PQ 的取舍完全不同：

* **SQ 不训练**（或者说训练只需要扫一遍求 min/max），建索引是 ``O(n·d)``，
  而 PQ 的 k-means 要迭代很多轮；
* **SQ 的相邻性保持得更好**（每一维都独立保留），但压缩率只有 4~8 倍，
  PQ 可以是 100 倍以上；
* **SQ 的查询可以完全不走解码**：8 bit 时一维只有 256 种取值，
  可以把 ``(q_d - decode(level))²`` 预先算成一张 ``(d, 256)`` 的表，
  之后一条编码的距离就是 ``d`` 次查表求和 —— 这正是
  :meth:`ScalarQuantizer.adc_distances_from_table` 做的事。

本模块另外提供 :class:`IvfSqIndex`（``IVF_SQ``）：用 IVF 分区缩小候选集，
再在候选上做 SQ 的 ADC 打分。这是"要快一点、又要很准"时的默认选择。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from ..errors import IndexBuildError
from ._common import as_matrix, as_row_ids, prepare_matrix, resolve_metric, total_nbytes
from .base import IndexMetadata, IndexStore
from .ivf import IvfBase

__all__ = ["QuantizationError", "ScalarQuantizer", "IvfSqIndex", "SUPPORTED_SQ_BITS"]

SUPPORTED_SQ_BITS = (1, 2, 4, 8)


@dataclass
class QuantizationError:
    """量化误差的统计量。

    "相对误差"是 ``||x - x̂|| / ||x||``。**必须看中位数而不是均值**：
    向量里偶尔会出现模长极小的样本（接近零向量），它们的相对误差
    会大到 100% 并把均值整个带偏，但这类样本对检索结果几乎没有影响。
    """

    mean_relative_error: float
    median_relative_error: float
    max_relative_error: float
    mean_squared_error: float
    num_bits: int
    num_samples: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mean_relative_error": self.mean_relative_error,
            "median_relative_error": self.median_relative_error,
            "max_relative_error": self.max_relative_error,
            "mean_squared_error": self.mean_squared_error,
            "num_bits": self.num_bits,
            "num_samples": self.num_samples,
        }


class ScalarQuantizer:
    """逐维（或全局）线性量化为 ``num_bits`` 位整数。"""

    def __init__(self, *, num_bits: int = 8, per_dimension: bool = True) -> None:
        num_bits = int(num_bits)
        if num_bits not in SUPPORTED_SQ_BITS:
            raise IndexBuildError(
                "不支持的 SQ 位数",
                num_bits=num_bits,
                supported=list(SUPPORTED_SQ_BITS),
            )
        self.num_bits = num_bits
        #: 最大码值：8 bit → 255，4 bit → 15
        self.max_level = (1 << num_bits) - 1
        #: True = 每一维有自己的 min/max；False = 全局共用一组
        self.per_dimension = bool(per_dimension)
        self.dim: int = 0
        self.min: np.ndarray = np.zeros(0, dtype=np.float32)
        self.max: np.ndarray = np.zeros(0, dtype=np.float32)
        #: 每个格点的宽度 ``(max - min) / max_level``，编码时用它做除法
        self.scale: np.ndarray = np.zeros(0, dtype=np.float32)
        self.trained: bool = False
        #: 编码结果，``encode`` 时会存下来，供 ``distance_to_codes`` 使用
        self.codes: np.ndarray = np.zeros((0, 0), dtype=np.uint8)

    # -- 训练 -------------------------------------------------------------
    def train(
        self, data: np.ndarray, *, sample_size: Optional[int] = None, seed: int = 0
    ) -> "ScalarQuantizer":
        """扫一遍求 min/max；只需要 ``O(n·d)``，不用迭代。"""

        m = as_matrix(data, what="SQ 训练数据")
        self.dim = int(m.shape[1])
        sample = m
        if sample_size is not None and int(sample_size) < m.shape[0]:
            rng = np.random.default_rng(int(seed))
            pick = rng.choice(m.shape[0], size=int(sample_size), replace=False)
            sample = m[pick]
        if self.per_dimension:
            lo = sample.min(axis=0).astype(np.float32)
            hi = sample.max(axis=0).astype(np.float32)
        else:
            lo = np.full(1, float(sample.min()), dtype=np.float32)
            hi = np.full(1, float(sample.max()), dtype=np.float32)
        self.min = lo
        self.max = hi
        self.scale = self._scale_from(self.min, self.max)
        self.trained = True
        return self

    def _scale_from(self, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """格点宽度。区间为 0 的维度特殊处理成 1.0。

        如果原样算 ``(hi-lo)/max_level``，常数列的宽度会是 0，
        编码时出现 0/0 = NaN。设成 1.0 后所有值都被编成 0 号格点，
        解码恰好还原成 ``min``，即"常量列无损"。
        """

        span = (hi - lo) / float(self.max_level)
        return np.where(span > 0, span, np.float32(1.0)).astype(np.float32)

    # -- 编码 / 解码 ------------------------------------------------------
    def encode(self, data: np.ndarray) -> np.ndarray:
        """``(n, d)`` float32 → ``(n, d)`` uint8 码值。"""

        self._require_trained()
        m = np.asarray(data, dtype=np.float32)
        if m.ndim == 1:
            m = m.reshape(1, -1)
        if m.shape[1] != self.dim:
            raise IndexBuildError(
                "编码数据的维度与量化器不一致", data_dim=int(m.shape[1]), sq_dim=self.dim
            )
        raw = np.rint((m - self.min) / self.scale)
        codes = np.clip(raw, 0, self.max_level).astype(np.uint8)
        return codes

    def decode(self, codes: np.ndarray) -> np.ndarray:
        """``(n, d)`` uint8 → ``(n, d)`` float32 重建值。"""

        self._require_trained()
        c = np.asarray(codes)
        if c.ndim == 1:
            c = c.reshape(1, -1)
        return np.ascontiguousarray(
            self.min.astype(np.float32) + c.astype(np.float32) * self.scale,
            dtype=np.float32,
        )

    # -- 误差 -------------------------------------------------------------
    def quantization_error(self, data: np.ndarray) -> QuantizationError:
        m = np.asarray(data, dtype=np.float32)
        rec = self.decode(self.encode(m))
        err = m - rec
        sq = np.einsum("ij,ij->i", err, err)
        rel = np.sqrt(sq) / np.maximum(np.linalg.norm(m, axis=1), 1e-12)
        return QuantizationError(
            mean_relative_error=float(rel.mean()),
            median_relative_error=float(np.median(rel)),
            max_relative_error=float(rel.max()),
            mean_squared_error=float(sq.mean()),
            num_bits=int(self.num_bits),
            num_samples=int(m.shape[0]),
        )

    # -- ADC --------------------------------------------------------------
    def adc_table(self, query: np.ndarray) -> np.ndarray:
        """``(d, max_level+1)`` 的查找表：``table[j][v] = (q_j - decode(v))²``。

        一维只有 256 个格点，所以穷举所有格点比"先解码再相减"便宜 ——
        而且这张表与库里的编码无关，一次查询只算一遍。
        """

        self._require_trained()
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.dim:
            raise IndexBuildError(
                "查询向量维度与量化器不一致", query_dim=int(q.shape[0]), sq_dim=self.dim
            )
        levels = np.arange(self.max_level + 1, dtype=np.float32)
        # decoded[j, v] = min_j + v * scale_j
        decoded = self.min.reshape(-1, 1) + self.scale.reshape(-1, 1) * levels.reshape(1, -1)
        diff = q.reshape(-1, 1) - decoded
        return np.ascontiguousarray(diff * diff, dtype=np.float32)

    def adc_distances_from_table(self, table: np.ndarray, codes: np.ndarray) -> np.ndarray:
        """查表 + 求和得到 ``(n,)`` 的近似平方距离。"""

        c = np.asarray(codes)
        if c.ndim == 1:
            c = c.reshape(1, -1)
        n = c.shape[0]
        acc = np.zeros(n, dtype=np.float32)
        if n == 0:
            return acc
        # 逐维累加，避免一次构造 (n, d) 的中间矩阵
        for j in range(c.shape[1]):
            acc += table[j][c[:, j].astype(np.int64)]
        return acc

    def adc_distances(self, query: np.ndarray, codes: np.ndarray) -> np.ndarray:
        """等价于"解码后算 l2"，但不构造解码矩阵。"""

        return self.adc_distances_from_table(self.adc_table(query), codes)

    def distance_to_codes(self, query: np.ndarray) -> np.ndarray:
        """``(q, d)`` 的查询 → 到 :attr:`codes` 里所有编码的近似距离 ``(q, n)``。

        查询向量保持浮点，只有库侧是编码的 —— 这就是"非对称"：
        误差只来自库侧，比两侧都量化小得多。
        """

        self._require_trained()
        if self.codes.size == 0:
            return np.zeros((0, 0), dtype=np.float32)
        q = np.asarray(query, dtype=np.float32)
        if q.ndim == 1:
            q = q.reshape(1, -1)
        out = np.zeros((q.shape[0], self.codes.shape[0]), dtype=np.float32)
        for i in range(q.shape[0]):
            out[i] = self.adc_distances(q[i], self.codes)
        return out

    # -- 持久化 -----------------------------------------------------------
    def state_arrays(self) -> Dict[str, np.ndarray]:
        return {
            "sq_min.npy": self.min.astype(np.float32),
            "sq_max.npy": self.max.astype(np.float32),
        }

    @classmethod
    def from_state(
        cls, lo: np.ndarray, hi: np.ndarray, *, num_bits: int, per_dimension: bool = True
    ) -> "ScalarQuantizer":
        sq = cls(num_bits=num_bits, per_dimension=per_dimension)
        sq.min = np.asarray(lo, dtype=np.float32)
        sq.max = np.asarray(hi, dtype=np.float32)
        sq.scale = sq._scale_from(sq.min, sq.max)
        sq.dim = int(sq.min.shape[0]) if sq.per_dimension else int(hi.shape[0])
        sq.trained = True
        return sq

    def size_bytes(self) -> int:
        return total_nbytes(self.state_arrays())

    @property
    def compression_ratio(self) -> float:
        """相对 float32 的压缩比（8 bit → 4，4 bit → 8）。"""

        return 32.0 / float(self.num_bits)

    # -- 内部 -------------------------------------------------------------
    def _require_trained(self) -> None:
        if not self.trained:
            raise IndexBuildError("ScalarQuantizer 还没有训练过", hint="先调用 train()")


# ---------------------------------------------------------------------------
# IVF_SQ
# ---------------------------------------------------------------------------


class IvfSqIndex(IvfBase):
    """``IVF_SQ``：IVF 只管缩小候选集，打分完全交给标量量化。

    它和 ``IVF_PQ`` 的分工值得对比着看：PQ 用"降到 8 字节"换 16~64 倍压缩，
    SQ 用"降到 1 字节/维"换 4 倍压缩。所以 SQ 的召回率通常显著更高
    （同一维度的信息保留得更完整），代价是每分区占的内存大得多。
    真实 LanceDB 里 ``IVF_SQ`` 是"精度优先"的那一档。
    """

    type_name = "IVF_SQ"
    #: 候选集打分是近似的（SQ 有量化误差），所以 refine_factor 有意义
    exact_candidates = False

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        self.sq = ScalarQuantizer(num_bits=8)
        #: (n, d) 的 uint8 编码
        self.codes: np.ndarray = np.zeros((0, 0), dtype=np.uint8)

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "IvfSqIndex":
        metric = resolve_metric(metadata)
        params = dict(metadata.params)
        num_bits = int(params.get("num_bits", 8))
        per_dim = bool(params.get("per_dimension", True))
        matrix = prepare_matrix(as_matrix(data), metric)
        ids = as_row_ids(row_ids, matrix.shape[0])

        index = cls(metadata, metric)
        index.vectors = matrix
        index.row_ids = ids
        index.sq = ScalarQuantizer(num_bits=num_bits, per_dimension=per_dim)
        index.sq.train(matrix)
        index.codes = index.sq.encode(matrix)
        centroids, inertia, iters, partitions = index._train_partitions(matrix)
        index.centroids = centroids
        index.kmeans_iterations = iters
        index.train_inertia = inertia
        index._build_partitions()
        index._record_details(partitions)
        return index

    # -- 打分 -------------------------------------------------------------
    def _cheap_scores(self, query: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """候选集上的 ADC 打分：只有查表和加法。"""

        table = self.sq.adc_table(self._prepare_query(query))
        return self.sq.adc_distances_from_table(table, self.codes[positions])

    # -- 维护 -------------------------------------------------------------
    def _encode_append(self, data: np.ndarray, new_assign: np.ndarray) -> None:
        # 沿用训练好的 min/max；超出训练区间的值会被 clip 到端点 ——
        # 这是增量场景下的已知精度损失，真实 LanceDB 同样建议定期重建。
        self.codes = np.concatenate([self.codes, self.sq.encode(data)], axis=0)

    def _remap_state(self, keep: np.ndarray) -> None:
        self.codes = self.codes[keep]

    # -- 持久化 -----------------------------------------------------------
    def _persisted_arrays(self) -> Dict[str, np.ndarray]:
        arrays = dict(super()._persisted_arrays())
        arrays.update(self.sq.state_arrays())
        arrays["sq_codes.npy"] = self.codes
        return arrays

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "IvfSqIndex":
        index = cls(metadata, resolve_metric(metadata))
        index._load_common(store, metadata)
        params = dict(metadata.params)
        index.sq = ScalarQuantizer.from_state(
            store.read_numpy("sq_min.npy"),
            store.read_numpy("sq_max.npy"),
            num_bits=int(params.get("num_bits", 8)),
            per_dimension=bool(params.get("per_dimension", True)),
        )
        index.codes = np.asarray(store.read_numpy("sq_codes.npy"), dtype=np.uint8)
        return index

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        info = super().stats()
        code_bytes = int(self.codes.nbytes)
        raw_bytes = int(self.vectors.size * 4)
        info.update(
            {
                "num_bits": int(self.sq.num_bits),
                "per_dimension": bool(self.sq.per_dimension),
                "sq_max_level": int(self.sq.max_level),
                "code_bytes": code_bytes,
                # 压缩比只看"编码 vs 原始浮点"：这是量化器本身的功劳
                "compression_ratio": (
                    raw_bytes / code_bytes if code_bytes else 1.0
                ),
                "sq_error": self.sq.quantization_error(self.vectors).as_dict(),
            }
        )
        return info
