"""k-means 与乘积量化（Product Quantization, PQ）。

这是整个向量索引里最"数学"的一块，也是压缩率的来源。分三步讲清楚：

**1. 为什么需要量化。**
一条 768 维的 float32 向量占 3 KB。十亿条就是 3 TB，装不进内存，
更别说还要按随机顺序访问。量化的目标是把每条向量压成几十个字节，
并且**让压缩后的表示能直接算距离**（否则还要解码，就白压了）。

**2. PQ 的做法（Jégou et al. 2011）。**
把 ``d`` 维空间切成 ``M = num_sub_vectors`` 个子空间，每个子空间宽
``d/M`` 维。在**每个子空间内部**独立跑一次 k-means，得到
``K = 2**num_bits`` 个质心。于是每条向量被表示成 ``M`` 个下标
（每个下标 0..K-1），一共 ``M·num_bits`` 位：

* ``d = 128, M = 8, num_bits = 8`` → 8 字节，相对 float32 压缩 **64 倍**；
* 每个子空间用「子向量到 K 个质心的距离」这张小表就能查距离，
  所以查询时是 **查表 + 加法，没有乘法**。

**3. 为什么是"非对称"（ADC）。**
查询向量保持浮点不量化，只把库里的向量量化。查询到达时先算一张
``(M, K)`` 的距离表 ``LUT[s][c] = ||q_s - centroid[s][c]||²``，
之后任一编码的距离就是 ``Σ_s LUT[s][code_s]``。查表和编码是同一量级的
操作，误差只来自"库侧被量化"这一半，所以召回率远好于"两侧都量化"。

本模块还提供独立的 :func:`kmeans`：k-means++ 初始化、空簇重播种，
IVF 的粗量化和 PQ 的每个子空间都用它。参数顺序刻意做成
``(centroids, inertia, iterations)``，和 sklearn 的习惯一致。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np

from ..errors import IndexBuildError
from ._common import as_matrix, total_nbytes
from .base import check_divisible
from .metric import normalize, pairwise_distances

__all__ = ["kmeans", "ProductQuantizer", "SUPPORTED_NUM_BITS", "MAX_NUM_BITS"]

#: 编码用 ``uint8`` 装下标，所以每个子空间的码本最多 ``2**8 = 256`` 个质心。
#: 1~8 位都合法（6 位 = 64 个质心也是常见配置），超过 8 位放不进一个字节。
MAX_NUM_BITS = 8
SUPPORTED_NUM_BITS = tuple(range(1, MAX_NUM_BITS + 1))


# ---------------------------------------------------------------------------
# k-means
# ---------------------------------------------------------------------------


def _kmeans_plusplus(X: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """k-means++ 初始化：按 ``D²`` 概率挑种子。

    随机取 k 个点当初始质心会让 k-means 频繁收敛到很差的局部最优
    （尤其在高维），而 k-means++ 只需要多算几轮距离就能把 inertia
    降低一个数量级。做法：第一个质心均匀随机，之后每个新质心按
    "到已选质心的最近距离的平方"成比例地抽。
    """

    n = X.shape[0]
    first = int(rng.integers(n))
    centroids = [X[first]]
    closest = np.maximum(pairwise_distances(X, X[first : first + 1], "l2")[0], 0.0)
    for _ in range(1, k):
        total = float(closest.sum())
        if total <= 0.0:
            # 所有点到已选质心的距离都是 0（数据里有大量重复点），
            # 只能随机补一个，否则概率分布退化成全 0。
            idx = int(rng.integers(n))
        else:
            probs = closest / total
            # pairwise_distances 展开成 ||a||²-2a·b+||b||²，浮点误差会让
            # 本该为 0 的距离变成 -1e-7，直接拿去当概率 numpy 会拒绝。
            probs = np.maximum(probs, 0.0)
            probs = probs / probs.sum()
            idx = int(rng.choice(n, p=probs))
        centroids.append(X[idx])
        new_d = np.maximum(pairwise_distances(X, X[idx : idx + 1], "l2")[0], 0.0)
        closest = np.minimum(closest, new_d)
    return np.ascontiguousarray(np.stack(centroids), dtype=np.float32)


def kmeans(
    data: np.ndarray,
    k: int,
    *,
    max_iters: int = 25,
    seed: int = 0,
    sample_rate: float = 1.0,
    metric: str = "l2",
) -> Tuple[np.ndarray, float, int]:
    """Lloyd 迭代的 k-means，返回 ``(centroids, inertia, iterations)``。

    教学实现里有三个工程细节值得一提，它们决定了这个函数能不能用在
    真实数据上：

    * **采样训练**：``sample_rate`` 让我们在几百万行上只取一小部分点来
      求质心。质心是"数据的统计量"，用 1% 的样本估计出来的质心，
      和全量相比几乎没差别，但训练时间少两个数量级；
    * **空簇重播种**：某个质心一个点都没分到（很常见，尤其当 k 接近 n）
      时，如果放着不管，这个质心会永远停在原地浪费一个码字。做法是把
      "离自己质心最远的那些点"搬过来当新质心 —— 这也顺便降低了 inertia；
    * **cosine 的处理**：在归一化向量上跑 l2 k-means，每轮把质心重新
      归一化。因为 ``||a-b||² = 2 - 2cos`` 在两个向量都单位长时成立，
      所以这等价于直接在余弦空间里做球面 k-means。
    """

    m = np.ascontiguousarray(np.asarray(data, dtype=np.float32))
    if m.ndim != 2 or m.shape[1] == 0:
        raise IndexBuildError("kmeans 需要 (n, d) 的矩阵", shape=list(m.shape))
    k = int(k)
    if k <= 0:
        raise IndexBuildError("kmeans 的 k 必须为正", k=k)
    n = m.shape[0]
    if n == 0:
        raise IndexBuildError("kmeans 的样本数为 0")
    if k > n:
        # 质心数超过样本数时必然有空簇，而且"每个点一个质心"没有意义。
        raise IndexBuildError(
            "kmeans 的 k 大于样本数",
            k=k,
            samples=n,
            hint="num_partitions / 2**num_bits 不能超过训练样本数",
        )

    rng = np.random.default_rng(int(seed))
    if sample_rate < 1.0:
        size = max(k, int(round(n * float(sample_rate))))
        pick = rng.choice(n, size=size, replace=False)
        m = np.ascontiguousarray(m[pick])
        n = m.shape[0]

    cosine = str(metric).lower() == "cosine"
    work = np.ascontiguousarray(normalize(m)) if cosine else m

    if k == 1:
        # 一个簇的 k-means 有闭式解：均值。省掉一整轮迭代。
        c = work.mean(axis=0, keepdims=True)
        if cosine:
            c = normalize(c)
        inertia = float(((work - c) ** 2).sum())
        return np.ascontiguousarray(c, dtype=np.float32), inertia, 0

    centroids = _kmeans_plusplus(work, k, rng)
    dist = pairwise_distances(centroids, work, "l2")  # (n, k)
    labels = np.argmin(dist, axis=1).astype(np.int64)
    inertia = float(dist[np.arange(n), labels].sum())
    iterations = 0

    for it in range(int(max_iters)):
        # --- M 步：把每个簇的均值作为新质心 ---
        counts = np.bincount(labels, minlength=k)
        sums = np.zeros((k, work.shape[1]), dtype=np.float64)
        np.add.at(sums, labels, work)  # 有重复下标，普通 += 会丢更新
        new_centroids = np.zeros((k, work.shape[1]), dtype=np.float32)
        non_empty = counts > 0
        new_centroids[non_empty] = (sums[non_empty] / counts[non_empty, None]).astype(
            np.float32
        )
        if cosine:
            new_centroids = normalize(new_centroids)

        # --- 空簇重播种：把"最委屈"的点搬过来 ---
        empty = np.flatnonzero(~non_empty)
        if empty.size:
            # 不能拿全 0 当新质心（那会把一个簇永久钉在原点），
            # 取当前距离自己质心最远的若干点。
            far = np.argsort(-dist[np.arange(n), labels])[: empty.size]
            new_centroids[empty] = work[far]

        shift = float(np.abs(new_centroids - centroids).max())
        centroids = new_centroids
        iterations = it + 1

        dist = pairwise_distances(centroids, work, "l2")
        labels = np.argmin(dist, axis=1).astype(np.int64)
        inertia = float(dist[np.arange(n), labels].sum())
        if shift <= 1e-5:
            break  # 质心基本不动了，提前停

    return np.ascontiguousarray(centroids, dtype=np.float32), inertia, iterations


# ---------------------------------------------------------------------------
# 乘积量化
# ---------------------------------------------------------------------------


class ProductQuantizer:
    """把 ``d`` 维向量拆成 ``M`` 段、每段独立查码本的量化器。

    码本形状 ``(M, 2**num_bits, d/M)``。这是索引里唯一"学习出来"的参数，
    也是 ``save`` 时真正占空间的东西（``num_bits=8, M=8, d=128`` 时约 32 KB）。
    """

    def __init__(self) -> None:
        #: (M, K, sub_dim) 的码本
        self.codebooks: np.ndarray = np.zeros((0, 0, 0), dtype=np.float32)
        self.num_sub_vectors: int = 0
        self.num_bits: int = 8
        self.sub_dim: int = 0
        self.dim: int = 0
        self.k: int = 0
        self.trained: bool = False
        self.train_inertia: float = 0.0

    # -- 基本属性 ---------------------------------------------------------
    @property
    def num_codes(self) -> int:
        return 1 << int(self.num_bits)

    @property
    def code_bytes_per_vector(self) -> int:
        """每条向量的编码字节数（每个子向量一个 ``uint8``）。"""

        return int(self.num_sub_vectors)

    @property
    def raw_bytes_per_vector(self) -> int:
        return int(self.dim) * 4

    @property
    def compression_ratio(self) -> float:
        if self.code_bytes_per_vector == 0:
            return 1.0
        return self.raw_bytes_per_vector / self.code_bytes_per_vector

    # -- 训练 -------------------------------------------------------------
    def train(
        self,
        data: np.ndarray,
        *,
        num_sub_vectors: int,
        num_bits: int = 8,
        max_iters: int = 25,
        sample_size: Optional[int] = None,
        seed: int = 0,
    ) -> "ProductQuantizer":
        """在每个子空间里跑一次 k-means，拼出码本。

        ``2**num_bits`` 是每个子空间的质心数：它同时决定了**压缩率**
        （每个子向量的字节数固定是 1）和**精度**（质心越多，
        子向量被重建得越准）。所以 ``num_bits`` 调大只提升精度不涨存储 ——
        涨的是训练时间和码本大小。
        """

        m = as_matrix(data, what="PQ 训练数据")
        num_bits = int(num_bits)
        if not 1 <= num_bits <= MAX_NUM_BITS:
            raise IndexBuildError(
                "不支持的 PQ 位数",
                num_bits=num_bits,
                supported=f"1..{MAX_NUM_BITS}",
                hint="编码下标用 uint8 存，所以每个子空间最多 256 个质心",
            )
        num_sub_vectors = int(num_sub_vectors)
        if num_sub_vectors <= 0:
            raise IndexBuildError("num_sub_vectors 必须为正", value=num_sub_vectors)
        self.dim = int(m.shape[1])
        # 维度必须能被整除，否则每个子空间宽度不一，码本没法建。
        self.sub_dim = check_divisible(self.dim, num_sub_vectors, what="向量维度")
        self.num_sub_vectors = num_sub_vectors
        self.num_bits = num_bits
        self.k = 1 << num_bits

        sample = m
        if sample_size is not None and int(sample_size) < m.shape[0]:
            rng = np.random.default_rng(int(seed))
            pick = rng.choice(m.shape[0], size=int(sample_size), replace=False)
            sample = np.ascontiguousarray(m[np.sort(pick)])
        if self.k > sample.shape[0]:
            raise IndexBuildError(
                "码本大小超过训练样本数",
                codebook_size=self.k,
                samples=int(sample.shape[0]),
                hint="减少 num_bits，或给 train 传更多数据",
            )

        codebooks = np.zeros((num_sub_vectors, self.k, self.sub_dim), dtype=np.float32)
        inertia = 0.0
        for s in range(num_sub_vectors):
            lo = s * self.sub_dim
            hi = lo + self.sub_dim
            # 每个子空间换一个种子：否则所有子空间的 k-means++ 会挑到
            # 同一批相对位置的点，码本之间产生单调的相关性。
            cent, sub_inertia, _ = kmeans(
                np.ascontiguousarray(sample[:, lo:hi]),
                self.k,
                max_iters=max_iters,
                seed=int(seed) + s,
            )
            codebooks[s] = cent
            inertia += float(sub_inertia)
        self.codebooks = codebooks
        self.train_inertia = inertia
        self.trained = True
        return self

    # -- 编码 / 解码 ------------------------------------------------------
    def encode(self, data: np.ndarray) -> np.ndarray:
        """``(n, d)`` float32 → ``(n, M)`` uint8 下标。"""

        self._require_trained()
        m = np.ascontiguousarray(np.asarray(data, dtype=np.float32))
        if m.ndim == 1:
            m = m.reshape(1, -1)
        if m.shape[1] != self.dim:
            raise IndexBuildError(
                "编码数据的维度与码本不一致", data_dim=int(m.shape[1]), pq_dim=self.dim
            )
        codes = np.zeros((m.shape[0], self.num_sub_vectors), dtype=np.uint8)
        for s in range(self.num_sub_vectors):
            lo = s * self.sub_dim
            sub = m[:, lo : lo + self.sub_dim]
            # pairwise_distances(codebook, sub) → (n, K)：每个子向量到 K 个质心
            d = pairwise_distances(self.codebooks[s], sub, "l2")
            codes[:, s] = np.argmin(d, axis=1).astype(np.uint8)
        return codes

    def decode(self, codes: np.ndarray) -> np.ndarray:
        """``(n, M)`` uint8 → ``(n, d)`` float32（量化器的重建值）。"""

        self._require_trained()
        c = np.ascontiguousarray(np.asarray(codes))
        if c.ndim == 1:
            c = c.reshape(1, -1)
        if c.shape[1] != self.num_sub_vectors:
            raise IndexBuildError(
                "编码的列数与 num_sub_vectors 不一致",
                codes_cols=int(c.shape[1]),
                num_sub_vectors=self.num_sub_vectors,
            )
        out = np.empty((c.shape[0], self.dim), dtype=np.float32)
        for s in range(self.num_sub_vectors):
            lo = s * self.sub_dim
            out[:, lo : lo + self.sub_dim] = self.codebooks[s][c[:, s].astype(np.int64)]
        return out

    def reconstruction_error(self, data: np.ndarray) -> Dict[str, Any]:
        """重建误差统计 —— 衡量"codebook 学得好不好"唯一直接可测的量。

        相对误差按行算 ``||x - x̂|| / ||x||``，因为不同样本的模长差别很大，
        绝对误差会掩盖"短向量被压得很糟"这件事。
        """

        self._require_trained()
        m = np.asarray(data, dtype=np.float32)
        rec = self.decode(self.encode(m))
        err = m - rec
        sq = np.einsum("ij,ij->i", err, err)
        norms = np.linalg.norm(m, axis=1)
        rel = np.sqrt(sq) / np.maximum(norms, 1e-12)
        return {
            "mean_squared_error": float(sq.mean()),
            "rmse": float(np.sqrt(sq.mean())),
            "mean_relative_error": float(rel.mean()),
            "median_relative_error": float(np.median(rel)),
            "max_relative_error": float(rel.max()),
            "num_sub_vectors": int(self.num_sub_vectors),
            "num_bits": int(self.num_bits),
            "code_bytes_per_vector": self.code_bytes_per_vector,
            "raw_bytes_per_vector": self.raw_bytes_per_vector,
            "compression_ratio": self.compression_ratio,
        }

    # -- ADC --------------------------------------------------------------
    def compute_distance_table(self, query: np.ndarray) -> np.ndarray:
        """查询向量的 ``(M, K)`` 距离查找表 —— PQ 快就快在这张表上。

        ``table[s][c] = ||q_s - codebook[s][c]||²``。它把"查询"这一侧
        的浮点计算**一次性做完**，之后每个编码只需要 ``M`` 次表查找和
        ``M-1`` 次加法。这就是 ADC（asymmetric distance computation）
        里 "table" 的来源。
        """

        self._require_trained()
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.dim:
            raise IndexBuildError(
                "查询向量维度与码本不一致", query_dim=int(q.shape[0]), pq_dim=self.dim
            )
        table = np.empty((self.num_sub_vectors, self.k), dtype=np.float32)
        for s in range(self.num_sub_vectors):
            lo = s * self.sub_dim
            diff = self.codebooks[s] - q[lo : lo + self.sub_dim]
            table[s] = np.einsum("ij,ij->i", diff, diff)
        return table

    def adc_distances(self, queries: np.ndarray, codes: np.ndarray) -> np.ndarray:
        """``(q, d) × (n, M) → (q, n)`` 的近似平方距离矩阵。

        内层是 ``table[s][codes[:, s]]``：一次 fancy-index（查表）加一次
        向量加法。**没有乘法**，也没有解码 —— 解码会构造 ``(n, d)`` 的
        大矩阵，等于把压缩白省的内存又还回去了。
        """

        self._require_trained()
        q = np.ascontiguousarray(np.asarray(queries, dtype=np.float32))
        if q.ndim == 1:
            q = q.reshape(1, -1)
        c = np.ascontiguousarray(np.asarray(codes))
        n = int(c.shape[0])
        out = np.zeros((q.shape[0], n), dtype=np.float32)
        for i in range(q.shape[0]):
            table = self.compute_distance_table(q[i])
            acc = np.zeros(n, dtype=np.float32)
            for s in range(self.num_sub_vectors):
                acc += table[s][c[:, s].astype(np.int64)]
            out[i] = acc
        return out

    # -- 持久化 -----------------------------------------------------------
    def state_arrays(self) -> Dict[str, np.ndarray]:
        return {"pq_codebooks.npy": self.codebooks}

    @classmethod
    def from_codebooks(cls, codebooks: np.ndarray, *, num_bits: int) -> "ProductQuantizer":
        pq = cls()
        cb = np.ascontiguousarray(np.asarray(codebooks, dtype=np.float32))
        pq.codebooks = cb
        pq.num_sub_vectors = int(cb.shape[0])
        pq.k = int(cb.shape[1])
        pq.sub_dim = int(cb.shape[2])
        pq.dim = pq.num_sub_vectors * pq.sub_dim
        pq.num_bits = int(num_bits)
        pq.trained = True
        return pq

    def size_bytes(self) -> int:
        return total_nbytes(self.state_arrays())

    # -- 内部 -------------------------------------------------------------
    def _require_trained(self) -> None:
        if not self.trained:
            raise IndexBuildError("ProductQuantizer 还没有训练过", hint="先调用 train()")
