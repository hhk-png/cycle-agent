"""ColBERT / 多向量（late interaction）重排。

## MaxSim：把"一个向量"换成"一堆向量"

双塔检索把一篇文档压成**一个**向量，然后在查询向量和它之间算一次相似度。
ColBERT（Khattab & Zaharia, 2020）说：别压，**每个 token 各留一个向量**，
再用 late interaction 打分：

.. code-block:: text

    score(q, d) = Σ_i  max_j  (q_i · d_j)
                  ↑ 每个查询 token   ↑ 在文档的所有 token 里挑最像的那个

逐项读：

* ``q_i`` 是查询第 i 个 token 的向量，``d_j`` 是文档第 j 个 token 的向量；
* ``max_j`` 是"文档里哪个 token 最像这个查询 token" —— 也就是**定位**；
* 外层 Σ 是"查询的每个 token 都要在文档里找到它的归宿" ——
  找不到就贡献 0，所以**漏掉一个查询词是要扣分的**（BM25 的最长匹配思想）；
* 整个式子**先算相似度、后求和**，这就是 "late interaction"：交互发生在
  编码之后，不像 cross encoder 那样在 Transformer 内部逐层交互。
  所以文档侧的 token 向量**可以离线算、可以建索引**，这是它比 cross encoder
  便宜的全部原因。

## 为什么多向量在长文档 / 多模态上更好

* **长文档**：单向量必须把"主题 + 细节 + 措辞"全塞进固定维度，
  维度不够时只能丢信息。多向量不丢 —— 一个 512 token 的文档有 512 个向量，
  容量随长度线性增长。
* **多模态（ColPali 等）**：一张图切成 patch，一个 patch 一个向量，
  MaxSim 直接对接"查询里的这个短语对应图上的哪一块"。单向量做不到这种
  "局部对齐"，因为它在编码时就丢掉了空间/位置结构。
* **可解释性**：``argmax_j`` 直接告诉你"查询的这个词命中了文档的哪个 token"，
  可以做高亮，这在 RAG 里非常有用。

## 代价：存储 100 倍

一个 token 一个向量。一篇 256 token 的文档、128 维、float16：

* 单向量：``128 × 2 B = 256 B``
* 多向量：``256 × 128 × 2 B = 65 KB`` —— **约 250 倍**

真实系统用两招压这个数字：向量量化到 1~2 bit（ColBERTv2 的 residual
compression），以及"去掉标点/停用词对应的 token"。

## LanceDB 怎么存

多向量列在 Lance 的 schema 里是**嵌套一层的定长列表**：

.. code-block:: text

    fixed_size_list<fixed_size_list<float16>[128]>[N]
    └── 外层 [N]：这一行有多少个 token（每行可以不同，N 是最大长度）
        └── 内层 [128]：每个 token 的向量维度（必须固定）

普通向量列只有一层 ``fixed_size_list<float32>[768]``。多出来的那一层
就是"多向量"的全部秘密 —— 所以一个多向量列的 Arrow 类型是可以被
程序识别的（Lance 靠这个决定要不要走 multivector 检索路径）。
写进去之后，检索侧把查询也变成 N 个 token 向量，做
:class:`MultivectorQuery` 那样的 N 路查询再合并。

## 两种查询策略

1. **查询侧展开（query-side expansion）** —— 每个查询 token 单独发起一次
   向量检索，拿到各自的 top-k，再合并。优点：能直接复用现成的单向量 ANN
   索引；缺点：每个 token 的检索都是近似的，MaxSim 里的 ``max_j`` 是
   "在召回集合里挑最大"，**不是全局最大**，召回率有天花板。
2. **存全量 token 矩阵 + 检索后算 MaxSim** —— 先做一次粗召回，
   把候选行的 token 矩阵取回来，老老实实算 MaxSim。优点：打分精确；
   缺点：回表要拉回 100 倍的数据量，**IO 是瓶颈**。

:class:`MultivectorQuery` 实现的是第 1 种（也是 LanceDB 里
``query.multivector(...).rerank(...)`` 那条路径的核心动作）；
:class:`ColBERTReranker` 的 :meth:`~ColBERTReranker.score_rows` 实现的是
第 2 种里"算 MaxSim"那一步。两者是互补的，生产上通常串起来用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..errors import QueryError
from .base import (
    DISTANCE_COL,
    RELEVANCE_COL,
    ROWID_COL,
    Row,
    Reranker,
    as_rows,
    resolve_ids,
)
from .cross_encoder import _blake_int, tokenize

__all__ = [
    "maxsim",
    "maxsim_matrix",
    "HashingTokenEmbedder",
    "MultivectorQuery",
    "ColBERTReranker",
    "MULTIVECTOR_STORAGE_SCHEMA",
]

#: 多向量列在 Lance schema 里的类型串（教学用）。
MULTIVECTOR_STORAGE_SCHEMA = "fixed_size_list<fixed_size_list<float16>[128]>[N]"


# ---------------------------------------------------------------------------
# MaxSim 本体
# ---------------------------------------------------------------------------


def _check_token_matrix(name: str, matrix: Any) -> np.ndarray:
    arr = np.asarray(matrix, dtype=np.float32)
    if arr.ndim != 2:
        raise QueryError(
            f"{name} 必须是二维的 (token 数, 向量维度)",
            got_shape=list(arr.shape),
            hint="单个 token 向量要包成 (1, d)；空的 token 矩阵应该直接跳过",
        )
    if arr.shape[0] == 0:
        raise QueryError(f"{name} 不能是 0 个 token", got_shape=list(arr.shape))
    if arr.shape[1] == 0:
        raise QueryError(f"{name} 的向量维度不能为 0", got_shape=list(arr.shape))
    return arr


def maxsim(query_tokens: Any, doc_tokens: Any) -> float:
    """``Σ_i max_j (q_i · d_j)``。两个输入都是 ``(token 数, 维度)``。"""

    q = _check_token_matrix("query_tokens", query_tokens)
    d = _check_token_matrix("doc_tokens", doc_tokens)
    if q.shape[1] != d.shape[1]:
        raise QueryError(
            "查询和文档的 token 向量维度不一致",
            query_dim=int(q.shape[1]),
            doc_dim=int(d.shape[1]),
            hint="两边必须用同一个编码器（同一份 checkpoint）",
        )
    # (n_q, n_d) 的相似度矩阵，然后每行取最大、求和。
    # 用矩阵乘法一次算完：Python 循环版本在 512×512 上会慢三个数量级。
    sim = q @ d.T
    return float(sim.max(axis=1).sum())


def maxsim_matrix(query_tokens: Any, docs: Sequence[Any]) -> np.ndarray:
    """一批文档的 MaxSim 分数，返回 ``(len(docs),)``。"""

    q = _check_token_matrix("query_tokens", query_tokens)
    if len(docs) == 0:
        return np.zeros(0, dtype=np.float32)
    out = np.empty(len(docs), dtype=np.float32)
    for i, doc in enumerate(docs):
        out[i] = maxsim(q, doc)
    return out


# ---------------------------------------------------------------------------
# 离线 token 编码器
# ---------------------------------------------------------------------------


class HashingTokenEmbedder:
    """把文本变成 ``(token 数, dim)`` 的 token 向量矩阵 —— 离线、确定。

    每个 token 的向量由**它的哈希**决定（固定种子的高斯向量归一化），
    所以：

    * 同一个 token 在任何文本里都得到同一个向量（这是"词表"的概念）；
    * 不同进程、不同运行顺序得到的结果完全一致（用 blake2b，不用 ``hash()``）；
    * 两个共享 token 比例高的文本，MaxSim 就高 —— 结构上和真 ColBERT 一致。

    它当然没有语义：``"猫"`` 和 ``"kitty"`` 毫无关系。想验证"管道对不对"
    用它就够了，想验证"模型好不好"必须换真模型。
    """

    def __init__(self, dim: int = 32, *, seed: int = 0, lowercase: bool = True) -> None:
        if dim <= 0:
            raise QueryError("dim 必须为正", dim=dim)
        self.dim = int(dim)
        self.seed = int(seed)
        self.lowercase = lowercase
        self._cache: Dict[str, np.ndarray] = {}

    def encode_tokens(self, text: str) -> np.ndarray:
        tokens = tokenize(text, lowercase=self.lowercase)
        if not tokens:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vector(t) for t in tokens]).astype(np.float32)

    def _vector(self, token: str) -> np.ndarray:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        rng = np.random.default_rng(_blake_int(f"token-embed:{self.seed}", token) % (2**63))
        vec = rng.standard_normal(self.dim).astype(np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        self._cache[token] = vec
        return vec


# ---------------------------------------------------------------------------
# 查询侧展开
# ---------------------------------------------------------------------------


@dataclass
class MultivectorQuery:
    """把一个多向量查询拆成 N 次单向量检索，再合并。

    ``search_fn(向量, k) -> list[dict]`` 由调用方提供（它可能是
    "查 mini-LanceDB 的 IVF 索引"、也可能是"查远端服务"，这里不关心）。
    返回的每一行必须带长度可用的 ``_distance``（越小越好）。

    Parameters
    ----------
    vectors : np.ndarray or list
        ``(n_tokens, dim)``。也可以直接传 ``list[list[float]]``。
    tokens : list[str], optional
        每个向量对应的 token 文本，只用于解释和调试。
    """

    vectors: Any
    tokens: Optional[List[str]] = None
    #: 每个 token 那次检索返回了多少行（``k``），记录在案便于 explain。
    per_token_k: int = 0

    def __post_init__(self) -> None:
        self.vectors = _check_token_matrix("vectors", self.vectors)
        if self.tokens is not None and len(self.tokens) != self.vectors.shape[0]:
            raise QueryError(
                "tokens 的长度必须等于向量的个数",
                tokens=len(self.tokens),
                vectors=int(self.vectors.shape[0]),
            )
        self.per_token_k = int(self.per_token_k)

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def from_text(cls, text: str, embedder: Any) -> "MultivectorQuery":
        """用 token 编码器把一段文本变成多向量查询。"""

        if not hasattr(embedder, "encode_tokens"):
            raise QueryError(
                "embedder 必须有 encode_tokens(text) 方法",
                got=type(embedder).__name__,
            )
        tokens = tokenize(text, lowercase=getattr(embedder, "lowercase", True))
        return cls(embedder.encode_tokens(text), tokens=tokens)

    @property
    def num_tokens(self) -> int:
        return int(self.vectors.shape[0])

    # -- 检索 -------------------------------------------------------------
    def search(
        self,
        search_fn: Callable[[np.ndarray, int], Any],
        *,
        k: int = 10,
        aggregate: str = "sum",
        deduplicate: bool = True,
    ) -> List[Row]:
        """逐 token 检索并合并。

        ``aggregate`` 决定"同一个文档被多个 token 命中时怎么加"：

        * ``"sum"`` —— 每个命中的 token 贡献 ``1 / (1 + _distance)``，
          然后**求和**。这直接对应 MaxSim 的外层 Σ：查询的每个 token
          都在文档里找到了归宿，贡献累加；
        * ``"max"`` —— 取最好的那一次。对应"只要有一个 token 强命中就够"，
          适合查询很短、只有一两个实词的情况。

        没被某个 token 召回的文档，在这个 token 上贡献 0
        （不是缺失惩罚）—— 因为 MaxSim 里的 ``max_j`` 是"文档所有 token 里
        最好的那个"，而"这一路没召回"只是**近似检索的局限**，
        不应该被解释成"相似度为 0"。这个近似正是查询侧展开的召回率天花板。
        """

        if k <= 0:
            raise QueryError("k 必须为正整数", k=k)
        if aggregate not in ("sum", "max"):
            raise QueryError("aggregate 只支持 'sum' / 'max'", aggregate=aggregate)

        scores: Dict[Any, float] = {}
        rows_by_id: Dict[Any, Row] = {}
        order: List[Any] = []
        hits: Dict[Any, int] = {}
        #: 每一次"命中"都记一条（同一个 id 可能来自多个 token），
        #: ``deduplicate=False`` 时按它还原"每路 top-k 的拼接"。
        hit_log: List[Tuple[Any, float]] = []

        for vector in self.vectors:
            found = as_rows(search_fn(np.asarray(vector, dtype=np.float32), k))
            if not found:
                continue
            ids = resolve_ids(found, ROWID_COL)
            for row_id, row in zip(ids, found):
                # 距离转相关性：单调，且落在 (0, 1]，方便跨 token 相加
                relevance = 1.0 / (1.0 + max(0.0, float(row.get(DISTANCE_COL, 0.0))))
                hit_log.append((row_id, relevance))
                if row_id not in rows_by_id:
                    rows_by_id[row_id] = dict(row)
                    order.append(row_id)
                    scores[row_id] = relevance
                    hits[row_id] = 1
                else:
                    if aggregate == "sum":
                        scores[row_id] += relevance
                    else:
                        scores[row_id] = max(scores[row_id], relevance)
                    hits[row_id] += 1
                    merged = rows_by_id[row_id]
                    for key, value in row.items():
                        if key not in merged or merged[key] is None:
                            merged[key] = value

        out: List[Row] = []
        if deduplicate:
            for row_id in order:
                row = dict(rows_by_id[row_id])
                row[RELEVANCE_COL] = float(scores[row_id])
                row["_num_tokens_matched"] = hits[row_id]
                out.append(row)
            out.sort(key=lambda r: (-float(r[RELEVANCE_COL]), str(r.get(ROWID_COL))))
        else:
            # 不去重：保留"每个 token 各自的 top-k 拼在一起"的原始形态。
            # 行的分数是**这一次命中的相关性**（不是聚合值），
            # 聚合值放在 _aggregated_score 里 —— 两者都看得到，
            # 才能回答"这条是谁召回的、为什么最后排这儿"。
            for row_id, relevance in hit_log:
                row = dict(rows_by_id[row_id])
                row[RELEVANCE_COL] = float(relevance)
                row["_aggregated_score"] = float(scores[row_id])
                row["_num_tokens_matched"] = hits[row_id]
                out.append(row)
            out.sort(key=lambda r: (-float(r["_aggregated_score"]), str(r.get(ROWID_COL))))

        self.per_token_k = k
        return out

    def describe(self) -> Dict[str, Any]:
        return {
            "token 数": self.num_tokens,
            "维度": int(self.vectors.shape[1]),
            "最后一次检索的 k": self.per_token_k,
            "存储开销": (
                f"{self.num_tokens} 个 token × {self.vectors.shape[1]} 维 = "
                f"{self.num_tokens * self.vectors.shape[1] * 2 / 1024:.1f} KB (float16)，"
                f"单向量只需要 {self.vectors.shape[1] * 2 / 1024:.1f} KB"
            ),
        }


# ---------------------------------------------------------------------------
# 重排器
# ---------------------------------------------------------------------------


class ColBERTReranker(Reranker):
    """用 MaxSim 对候选行重新打分。

    Parameters
    ----------
    embedder : obj, optional
        需要 ``encode_tokens(text) -> (n_tokens, dim)``。默认
        :class:`HashingTokenEmbedder`（离线确定）。
    column : str, default "vector"
        多向量列。值可以是 ``(n_tokens, dim)`` 的 numpy 数组、
        嵌套 list，或 **bytes**（真实 Lance 文件里就是这么存的）。
        找不到这一列时会退化到用 ``text_column`` 现场编码。
    text_column : str, default "text"
        没有多向量列时，从哪一列取正文现场 token 化。
    top_k : int, default 100
        只重排前 top_k 条。MaxSim 是 ``O(|Q| × |D|)``，但真正贵的是
        **回表把 token 矩阵拉回来**（100 倍数据量），所以候选数必须卡住。
    """

    def __init__(
        self,
        embedder: Optional[Any] = None,
        *,
        column: str = "vector",
        text_column: str = "text",
        top_k: int = 100,
        return_score: str = "relevance",
        id_column: str = ROWID_COL,
    ) -> None:
        super().__init__(return_score, id_column=id_column)
        self.embedder: Any = embedder if embedder is not None else HashingTokenEmbedder()
        if not hasattr(self.embedder, "encode_tokens"):
            raise QueryError(
                "embedder 必须有 encode_tokens(text) 方法",
                got=type(self.embedder).__name__,
            )
        self.column = column
        self.text_column = text_column
        top_k = int(top_k)
        if top_k <= 0:
            raise QueryError("top_k 必须为正整数", top_k=top_k)
        self.top_k = top_k

    def __str__(self) -> str:  # pragma: no cover
        return f"ColBERTReranker(column={self.column!r}, top_k={self.top_k})"

    # -- 取多向量 ---------------------------------------------------------
    def tokens_of(self, row: Row) -> np.ndarray:
        """把一行里的多向量列解出来。"""

        raw = row.get(self.column)
        if raw is not None:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                # 真实 Lance 里多向量是按定长列表扁平存进 buffer 的
                payload = bytes(raw)
                if len(payload) % 2 != 0:
                    # float16 每个元素 2 字节；奇数长度说明根本不是一块
                    # 定长列表 buffer（或者 schema 和这份数据对不上）
                    raise QueryError(
                        "多向量列的字节长度不是 float16 的整数倍",
                        bytes=len(payload),
                        hint="确认这一列的类型是 fixed_size_list<fixed_size_list<float16>[D]>[N]",
                    )
                buf = np.frombuffer(payload, dtype=np.float16)
                # 形状由 schema 决定；这里按最常见的 128 维反推
                dim = 128
                if buf.size % dim != 0:
                    raise QueryError(
                        "多向量列的字节长度不是 128 的整数倍",
                        bytes=len(raw),
                        hint="用 tokens_of 前先确认 schema 里的 fixed_size_list 内层宽度",
                    )
                return buf.reshape(-1, dim).astype(np.float32)
            matrix = np.asarray(raw, dtype=np.float32)
            if matrix.ndim == 1:
                # 单个 token 向量也允许：等价于 1 个 token 的矩阵
                matrix = matrix[None, :]
            return _check_token_matrix(f"{self.column}", matrix)

        text = row.get(self.text_column)
        if text is None:
            raise QueryError(
                "这一行既没有多向量列也没有正文列",
                column=self.column,
                text_column=self.text_column,
                available=[k for k in row if not k.startswith("_")],
                hint="传 column=... / text_column=...，或者先把多向量算好写进行里",
            )
        matrix = self.embedder.encode_tokens(str(text))
        return _check_token_matrix(f"{self.text_column}", matrix)

    # -- 打分 -------------------------------------------------------------
    def score_rows(self, query: Optional[str], rows: Sequence[Row]) -> List[float]:
        if query is None:
            raise QueryError(
                "ColBERT 重排需要查询文本（否则没法构造 token 矩阵）",
                hint="多向量查询请走 MultivectorQuery / rerank_multivector",
            )
        query_tokens = _check_token_matrix(
            "query", self.embedder.encode_tokens(str(query))
        )
        return [maxsim(query_tokens, self.tokens_of(row)) for row in rows]

    # -- 入口 -------------------------------------------------------------
    def rerank_vector(
        self, query: Optional[str], vector_results: Any, *, k: Optional[int] = None, **_: Any
    ) -> List[Row]:
        rows = as_rows(vector_results)
        if not rows:
            return []
        head, tail = rows[: self.top_k], rows[self.top_k :]
        ranked = self._score_and_rank(query, head)
        unscored = [dict(r) for r in tail]
        for row in unscored:
            row[RELEVANCE_COL] = None
        return self._finalize(ranked + unscored, k)

    def rerank_hybrid(
        self,
        query: Optional[str],
        vector_results: Any,
        fts_results: Any,
        *,
        k: Optional[int] = None,
        **kwargs: Any,
    ) -> List[Row]:
        """合并两路 → MaxSim 重排。**召回分数在合并之后就丢掉了**：
        ColBERT 只信 token 级的交互，这正是它被当作"重排器"的原因。"""

        merged = self.merge_results(vector_results, fts_results)
        return self.rerank_vector(query, merged, k=k)

    def rerank_fts(
        self, query: Optional[str], fts_results: Any, *, k: Optional[int] = None, **kwargs: Any
    ) -> List[Row]:
        return self.rerank_vector(query, fts_results, k=k)
