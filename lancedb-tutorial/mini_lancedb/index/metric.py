"""距离度量与 Top-K 选择。

向量检索里有两个容易被忽视、但会直接决定结果对错的细节：

**1. "距离"和"相似度"方向相反。**
LanceDB 的接口统一暴露 ``_distance``，且**越小越好**。于是
"内积越大越好"必须取负号，否则 ANN 索引里的排序会全反。
这个符号错误在只有 l2 的场景下看不出来，一旦切到 dot 就会静默错。

**2. 归一化只对 cosine 有意义。**
cosine 距离 = ``1 - a·b/(|a||b|)``。如果查询向量没归一化，
每次比较都要多做一次除法（或提前把库里的向量归一化 —— LanceDB
的做法是在写入时就把 cosine 列归一化，查询时只归一化查询向量）。
归一化之后 cosine 和 l2 有单调关系：``l2² = 2(1 - cos)``，
所以**对归一化向量，用 l2 索引查 cosine 结果是一样的**，
这也是为什么很多实现干脆用 l2 索引加速 cosine。

**3. 二进制向量用 Hamming 距离。**
1-bit 量化之后的向量是位串，比较方式是 popcount(xor)。
这在内存上便宜 32 倍，在速度上通常快 5~10 倍，召回率损失
可以通过"多探几个分区 + 用浮点重排"补回来。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..errors import MiniLanceError, QueryError

__all__ = [
    "Metric",
    "METRICS",
    "get_metric",
    "l2_distance",
    "cosine_distance",
    "dot_distance",
    "hamming_distance",
    "pairwise_distances",
    "top_k",
    "top_k_from_matrix",
    "normalize",
    "is_normalized",
]


class Metric:
    """一个距离度量的全部信息。"""

    __slots__ = ("name", "aliases", "higher_is_better", "needs_normalization")

    def __init__(
        self,
        name: str,
        *,
        aliases: Sequence[str] = (),
        higher_is_better: bool = False,
        needs_normalization: bool = False,
    ) -> None:
        self.name = name
        self.aliases = tuple(aliases)
        #: True 表示原始相似度"值越大越相似"。内部一律先翻成"越小越好"
        #: 的距离再排序，避免混排时方向搞反。
        self.higher_is_better = higher_is_better
        self.needs_normalization = needs_normalization

    def __repr__(self) -> str:  # pragma: no cover
        return f"Metric({self.name})"


#: LanceDB 支持的四种度量。名字和官方 API 里的字符串完全一致。
METRICS: Dict[str, Metric] = {
    "l2": Metric("l2", aliases=("euclidean", "l2sq")),
    "cosine": Metric("cosine", needs_normalization=True),
    "dot": Metric("dot", aliases=("inner_product", "ip"), higher_is_better=True),
    "hamming": Metric("hamming"),
}

_ALIAS_TO_NAME = {m.name: m.name for m in METRICS.values()}
for _m in METRICS.values():
    for _a in _m.aliases:
        _ALIAS_TO_NAME[_a] = _m.name


def get_metric(name: str) -> Metric:
    key = str(name).lower().strip()
    try:
        return METRICS[_ALIAS_TO_NAME[key]]
    except KeyError:
        raise QueryError(
            f"不支持的距离度量 {name!r}",
            supported=sorted(METRICS),
        ) from None


# ---------------------------------------------------------------------------
# 单个距离函数（都返回"越小越好"的分数）
# ---------------------------------------------------------------------------


def l2_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """平方欧氏距离。

    刻意**不开平方**：排序只需要单调，开方是纯浪费。
    返回值也和真实 LanceDB 的 ``_distance``（l2 时）一致 —— 它给的
    也是平方距离，很多人第一次看到会以为算错了。
    """

    diff = a - b
    return np.einsum("...i,...i->...", diff, diff)


def cosine_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``1 - cos(a, b)``。已归一化的向量上等价于 ``l2² / 2``。"""

    na = np.linalg.norm(a, axis=-1, keepdims=True)
    nb = np.linalg.norm(b, axis=-1, keepdims=True)
    denom = na * nb
    dot = np.einsum("...i,...i->...", a, b)
    with np.errstate(divide="ignore", invalid="ignore"):
        sim = np.where(denom > 0, dot / denom, 0.0)
    return 1.0 - sim


def dot_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """内积距离 = ``1 - a·b``。

    **注意不是简单的取负。** Lance 的 ``lance-linalg`` 里 Dot 的实现是
    ``1.0 - x·y``：这样"完全同向且模长合适"的向量距离接近 0，
    和 l2/cosine 的值域大致对齐，用户在看 ``_distance`` 时不会
    因为度量不同而看到差几个数量级的数字。

    排序结果和"取负"完全一样（单调变换），但**绝对值不一样** ——
    任何把 ``_distance`` 和阈值比较、或者跨度量做混合排序的代码，
    都必须用这个定义。
    """

    return 1.0 - np.einsum("...i,...i->...", a, b)


def hamming_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """位串汉明距离。输入可以是 ``uint8`` 数组（每元素 8 个 bit）
    或已打包的位串（每个元素装 8 位）。"""

    x = np.bitwise_xor(
        np.ascontiguousarray(a).view(np.uint8), np.ascontiguousarray(b).view(np.uint8)
    )
    return _popcount(x)


_POPCOUNT_TABLE = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount(bytes_arr: np.ndarray) -> np.ndarray:
    # numpy 2.x 有 bitwise_count，老版本用查表
    fn = getattr(np, "bitwise_count", None)
    if fn is not None:
        return fn(bytes_arr).sum(axis=-1)
    return _POPCOUNT_TABLE[bytes_arr].sum(axis=-1)


# ---------------------------------------------------------------------------
# 批量距离
# ---------------------------------------------------------------------------


def pairwise_distances(
    matrix: np.ndarray, queries: np.ndarray, metric_name: str
) -> np.ndarray:
    """``(n, d) × (q, d) -> (q, n)`` 的距离矩阵。

    这是暴力检索的全部实现。真实系统里它是"兜底路径" ——
    当向量维度极低、数据量很小、或者过滤后候选集已经不多时，
    老老实实算全量比走 ANN 更快也更准。
    """

    metric = get_metric(metric_name)
    m = np.ascontiguousarray(matrix, dtype=np.float32)
    q = np.ascontiguousarray(queries, dtype=np.float32)
    if q.ndim == 1:
        q = q[None, :]
    if metric.name == "hamming":
        return _hamming_matrix(m, q)
    if metric.name == "cosine":
        m = normalize(m)
        q = normalize(q)
        # 归一化之后用矩阵乘法 + 单调变换，比逐对算快一个数量级
        sim = q @ m.T
        return np.asarray(1.0 - sim, dtype=np.float32)
    if metric.name == "dot":
        return np.asarray(1.0 - (q @ m.T), dtype=np.float32)
    # l2：用 ||a||² - 2a·b + ||b||² 展开，避免构造 (q, n, d) 的三维张量
    qq = np.einsum("qi,qi->q", q, q)[:, None]
    mm = np.einsum("ni,ni->n", m, m)[None, :]
    return np.asarray(qq + mm - 2.0 * (q @ m.T), dtype=np.float32)


def _hamming_matrix(m: np.ndarray, q: np.ndarray) -> np.ndarray:
    mb = np.ascontiguousarray(m, dtype=np.uint8).view(np.uint8)
    qb = np.ascontiguousarray(q, dtype=np.uint8).view(np.uint8)
    # (q, n, bytes) 的位异或，按 bytes 维求和
    x = np.bitwise_xor(qb[:, None, :], mb[None, :, :])
    fn = getattr(np, "bitwise_count", None)
    if fn is not None:
        return fn(x).sum(axis=-1)
    return _POPCOUNT_TABLE[x].sum(axis=-1)


def normalize(matrix: np.ndarray, *, eps: float = 1e-12) -> np.ndarray:
    """按行 L2 归一化。零向量保持为零（避免产生 NaN）。"""

    m = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    safe = np.where(norms < eps, 1.0, norms)
    return m / safe


def is_normalized(matrix: np.ndarray, tol: float = 1e-3) -> bool:
    m = np.asarray(matrix, dtype=np.float64)
    norms = np.linalg.norm(m, axis=-1)
    return bool(np.all(np.abs(norms - 1.0) < tol))


# ---------------------------------------------------------------------------
# Top-K
# ---------------------------------------------------------------------------


def top_k(
    distances: np.ndarray, k: int, *, largest: bool = False
) -> Tuple[np.ndarray, np.ndarray]:
    """一维数组取前 k：返回 ``(值, 下标)``，按值升序（或降序）排列。

    用 ``argpartition`` 做到 ``O(n)``，再对 k 个元素排序 ——
    在 n = 1e8 的候选集上，``O(n log n)`` 排序和 ``O(n)`` 分区的差别
    就是 5 秒和 0.5 秒的差别。

    **NaN 的处理是关键**：Lance 里"这一行的向量是 null"会被编码成 NaN。
    NaN 在比较中永远为 False，如果不显式处理，它会随机出现在结果里。
    这里把 NaN 一律当作 +inf（最不相似），排到最后并被截断掉。
    """

    arr = np.asarray(distances, dtype=np.float32).ravel()
    n = arr.size
    k = int(k)
    if k <= 0 or n == 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.int64)

    if np.isnan(arr).any():
        arr = np.where(np.isnan(arr), np.inf, arr)
    if k >= n:
        order = np.argsort(arr if not largest else -arr, kind="stable")
        return arr[order], order.astype(np.int64)

    # 分区：把最小的 k 个放到前 k 位
    key = arr if not largest else -arr
    part = np.argpartition(key, k - 1)[:k]
    order = part[np.argsort(key[part], kind="stable")]
    return arr[order], order.astype(np.int64)


def top_k_from_matrix(
    distances: np.ndarray, k: int, *, largest: bool = False
) -> Tuple[np.ndarray, np.ndarray]:
    """``(q, n)`` 距离矩阵逐行取 Top-K。返回 ``(值(q,k), 下标(q,k))``。"""

    d = np.atleast_2d(distances)
    q, n = d.shape
    vals = np.full((q, k), np.inf, dtype=np.float32)
    idx = np.full((q, k), -1, dtype=np.int64)
    for i in range(q):
        v, j = top_k(d[i], k, largest=largest)
        vals[i, : len(v)] = v
        idx[i, : len(j)] = j
    return vals, idx


def distance_to_similarity(distances: np.ndarray, metric_name: str) -> np.ndarray:
    """把内部距离转成用户可读的"相似度分数"（越大越相似）。

    真实 LanceDB 的 FTS 返回 ``_score``（BM25，越大越好），
    向量检索返回 ``_distance``（越小越好）。两者语义不同，
    混合检索的重排器必须先把它们对齐到同一个方向 ——
    这是混合检索最容易出错的地方。
    """

    metric = get_metric(metric_name)
    d = np.asarray(distances, dtype=np.float32)
    if metric.name == "cosine":
        return 1.0 - d / 2.0 if float(np.nanmax(d)) > 1.5 else 1.0 - d
    if metric.name == "dot":
        return 1.0 - d
    return 1.0 / (1.0 + d)


def metric_summary() -> List[Dict[str, Any]]:
    """给教程用的汇总表。"""

    return [
        {
            "name": m.name,
            "aliases": list(m.aliases),
            "更相似意味着": "值更小" if not m.higher_is_better else "值更大（取负后仍是最小优先）",
            "查询向量是否需归一化": m.needs_normalization,
        }
        for m in METRICS.values()
    ]
