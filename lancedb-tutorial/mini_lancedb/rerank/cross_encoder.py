"""Cross-Encoder 重排：把 ``(query, doc)`` **成对**送进模型重新打分。

## 为什么它更强，也为什么它更贵

双塔（bi-encoder / embedding）检索的架构决定了 query 和 doc **从来没有
见过面**：两边各自编码成一个向量，相似度靠点积算。这带来一个不可修复的
损失 —— 编码器必须把"这个 doc 可能被问到的所有问题"都压进一个固定长度
的向量里。而 cross encoder 把 ``[query, doc]`` 拼成一个序列送进
Transformer，**每一层都能做 query token 和 doc token 之间的注意力**。
所以它对"否定、比较、数量关系、长问句"这类需要细粒度对齐的查询明显更好。

代价是复杂度：

* 双塔：文档向量**离线算一次**，查询时只编码查询，然后做 ANN。
  1 亿篇文档的在线成本是**一次**编码 + 一次 ANN 搜索。
* cross encoder：分数**不能预先算**（它依赖 query），每次查询要对每个候选
  做**一次完整的 Transformer 前向**。N 个候选就是 O(N) 次前向。

所以工程上的标准做法是**两阶段**：先用便宜的召回拿到 top 20~100，
再用 cross encoder 对这几十条重排。100 条 × 20ms ≈ 2s 在 CPU 上已经很勉强，
所以真实部署要么卡在 20~50 条，要么上 GPU 批处理。**永远不要对整个库
跑 cross encoder** —— 那不是"更准"，那是把检索系统降级成在线推理服务。

## 离线可测：HashCrossEncoder

真实模型要下载权重、要 GPU，测试里跑不了。所以这里提供一个
:class:`HashCrossEncoder`：一个**确定性的、可解释的玩具打分器**，
它把"一个真实 cross encoder 会学到的东西"用显式特征 + 固定随机投影
凑出来：

1. **token 重叠**：``|Q ∩ D| / sqrt(|Q| · |D|)``，即集合上的余弦；
2. **IDF 加权重叠**：稀有词的重叠比"的、是、a、the"的重叠值钱得多，
   这是 BM25 教给我们的第一课；
3. **查询覆盖率**：``|Q ∩ D| / |Q|``，衡量"文档答了多少问题"；
4. **首个命中位置**：越靠前说明这段文本越可能就是主题所在；
5. **bigram 重叠**：连续词对，能区分"银行 贷款"和"贷款 银行"；
6. **长度比**：一句话的文档和一个段落的文档不该被同等看待；
7. **精确子串**：查询原样出现在文档里是极强的信号。

再叠加一个**固定的伪随机投影**（由 ``seed`` 决定的高斯矩阵）把
"query token × doc token" 的哈希交叉特征压到一个标量扰动上，
并过 ``sigmoid``。这个扰动是**有界的**（不会盖过显式特征）、
**确定性的**（同输入同输出）、而且让分数连续分布 —— 这正是"像个真模型"
的意思：真模型不会给所有相关文档同一个分数。

**它绝对不能当成质量基准。** 它只保证"词面上重叠的排在上面"，
对同义词、改写、跨语言一概无感。测试里用它只是为了验证**管道**
（成对打分 → 写回分数 → 排序 → 截断）是对的；换真模型时管道一行都不用改。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..errors import NotSupportedError, QueryError
from .base import RELEVANCE_COL, ROWID_COL, Row, Reranker, as_rows

try:  # Python 3.8+ 才有 Protocol；本项目跑在 3.14 上，这里只是形式上的兜底
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore[assignment,misc]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls


__all__ = [
    "CrossEncoderModel",
    "HashCrossEncoder",
    "SentenceTransformersCrossEncoder",
    "CrossEncoderReranker",
    "tokenize",
]

#: 默认只重排这么多条候选。见模块文档里"两阶段"那一段。
DEFAULT_RERANK_TOP_K = 100


def tokenize(text: str, *, lowercase: bool = True) -> List[str]:
    """极简分词：ASCII 单词整体保留，**CJK 字符逐字切开**。

    真实的 cross encoder 用的是 BPE/WordPiece 子词，我们没必要复刻。
    但有一个细节必须做对：如果按"非空白字符切分"，``"机器学习"`` 会变成
    一个 token，那么"机器学习"和"机器学习算法"的重叠度是 0 ——
    这显然是错的。所以对 CJK 逐字切，让中文也具备可用的词面重叠。
    """

    out: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            out.append("".join(buf))
            buf.clear()

    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            buf.append(ch)
        elif _is_cjk(ch):
            flush()
            out.append(ch)
        else:
            flush()
    flush()
    if lowercase:
        out = [t.lower() for t in out]
    return out


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xAC00 <= code <= 0xD7AF  # 韩文音节
    )


def _blake_int(*parts: str, bits: int = 64) -> int:
    """跨进程稳定的哈希。

    绝不能用内置 ``hash()``：Python 对 ``str`` 的哈希带**每进程随机盐**
    （PYTHONHASHSEED），同一个字符串在两个进程里得到的桶号不同，
    "离线确定性"就成了空话。测试里有一条专门跨进程比对字节。
    """

    h = hashlib.blake2b(digest_size=bits // 8)
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return int.from_bytes(h.digest(), "little")


# ---------------------------------------------------------------------------
# 模型接口
# ---------------------------------------------------------------------------


@runtime_checkable
class CrossEncoderModel(Protocol):
    """cross encoder 的最小接口。

    真实 ``sentence_transformers.CrossEncoder`` 恰好就是这个形状：
    ``model.predict([[query, passage], ...], batch_size=...) -> np.ndarray``。

    **只收"成对的文本"，不收向量。** 这正是它和双塔编码器的本质区别：
    接口层面就不允许你把 doc 单独编码好存起来。
    """

    def predict(self, pairs: Sequence[Sequence[str]]) -> np.ndarray:
        """``pairs`` 是 ``[[query, doc], ...]``，返回长度相同的分数数组。""" ""
        ...


class HashCrossEncoder:
    """确定性的离线 cross encoder（见模块文档）。

    Parameters
    ----------
    dim : int, default 64
        伪随机投影的维度。越大扰动越"平滑"（方差越小），越不像随机噪声。
    hashed_features : int, default 256
        query token × doc token 交叉特征的哈希桶数。
    seed : int, default 0
        投影矩阵的种子。**换种子 = 换一个"模型"**，但同一个种子永远同分。
    """

    #: 显式特征的权重。顺序和 :meth:`_explicit_features` 的返回值一一对应。
    FEATURE_WEIGHTS: Tuple[float, ...] = (1.2, 1.6, 1.2, 0.5, 0.8, 0.3, 0.6)
    #: sigmoid 的中心点：让"完全不相干"的一对落在 0.05 附近、强重叠落在 0.9 附近。
    BIAS: float = -3.0
    #: 伪随机扰动的幅度上限（``tanh`` 的输出乘上它）。
    NOISE_SCALE: float = 0.35

    def __init__(
        self,
        dim: int = 64,
        *,
        hashed_features: int = 256,
        seed: int = 0,
        lowercase: bool = True,
    ) -> None:
        if dim <= 0 or hashed_features <= 0:
            raise QueryError("dim / hashed_features 必须为正", dim=dim, hashed_features=hashed_features)
        self.dim = int(dim)
        self.hashed_features = int(hashed_features)
        self.seed = int(seed)
        self.lowercase = lowercase
        rng = np.random.default_rng(_blake_int(f"hash-cross-encoder:{seed}") % (2**63))
        # 固定投影：因为是固定种子生成的，所以"模型"是确定的
        self._projection = rng.standard_normal((self.dim, self.hashed_features))
        self._readout = rng.standard_normal(self.dim)
        #: 语料统计（IDF）。``None`` 表示"每次 predict 时按当前批次的文档估"。
        self._idf: Optional[Dict[str, float]] = None
        self._num_docs = 0

    # -- 语料 -------------------------------------------------------------
    def fit(self, corpus: Sequence[str]) -> "HashCrossEncoder":
        """用一批文档冻结 IDF 统计。

        冻结之前（默认），IDF 在每次 :meth:`predict` 里按**当前批次的文档**
        现算 —— 因为离线实现不想引入"训练语料"这个概念。
        这让分数依赖批次组成，是个刻意的简化：真实模型的权重是训练时
        固化在参数里的，不会有这个问题。要做稳定评测就先 ``fit()``。
        """

        docs = [tokenize(text, lowercase=self.lowercase) for text in corpus]
        self._num_docs = len(docs)
        df: Dict[str, int] = {}
        for tokens in docs:
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        n = max(1, self._num_docs)
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        return self

    def _idf_table(self, docs: Sequence[Sequence[str]]) -> Dict[str, float]:
        if self._idf is not None:
            return self._idf
        n = max(1, len(docs))
        df: Dict[str, int] = {}
        for tokens in docs:
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    # -- 打分 -------------------------------------------------------------
    def predict(self, pairs: Sequence[Sequence[str]]) -> np.ndarray:
        if len(pairs) == 0:
            return np.zeros(0, dtype=np.float32)
        queries: List[List[str]] = []
        docs: List[List[str]] = []
        for pair in pairs:
            if len(pair) != 2:
                raise QueryError(
                    "cross encoder 需要 (query, doc) 成对的输入",
                    got=list(pair),
                    hint="pairs 的形状是 [[query, doc], ...]",
                )
            queries.append(tokenize(str(pair[0]), lowercase=self.lowercase))
            docs.append(tokenize(str(pair[1]), lowercase=self.lowercase))

        idf = self._idf_table(docs)
        out = np.empty(len(pairs), dtype=np.float32)
        for i, (query_tokens, doc_tokens) in enumerate(zip(queries, docs)):
            explicit = self._explicit_features(query_tokens, doc_tokens, idf)
            base = float(np.dot(np.asarray(self.FEATURE_WEIGHTS), explicit))
            noise = self._hashed_projection(query_tokens, doc_tokens)
            out[i] = _sigmoid(base + self.BIAS + noise)
        return out

    def _explicit_features(
        self, query: Sequence[str], doc: Sequence[str], idf: Dict[str, float]
    ) -> np.ndarray:
        """7 个人能看懂的特征。**顺序必须和 ``FEATURE_WEIGHTS`` 对齐。**"""

        if not query or not doc:
            return np.zeros(len(self.FEATURE_WEIGHTS), dtype=np.float64)

        q_set, d_set = set(query), set(doc)
        shared = q_set & d_set

        # 1. 集合余弦
        overlap_cos = len(shared) / math.sqrt(len(q_set) * len(d_set))
        # 2. IDF 加权重叠（归一化成余弦形式，避免长文档天然占便宜）
        q_idf = sum(idf.get(t, 1.0) for t in q_set) or 1.0
        d_idf = sum(idf.get(t, 1.0) for t in d_set) or 1.0
        shared_idf = sum(idf.get(t, 1.0) for t in shared)
        idf_overlap = shared_idf / math.sqrt(q_idf * d_idf)
        # 3. 查询覆盖率
        coverage = len(shared) / len(q_set)
        # 4. 首个命中位置（从文档左侧数，第 0 个 token 命中 → 1.0）
        first_match = 0.0
        for pos, token in enumerate(doc):
            if token in q_set:
                first_match = 1.0 / (1.0 + pos)
                break
        # 5. bigram 重叠
        q_bi = set(zip(query, query[1:]))
        d_bi = set(zip(doc, doc[1:]))
        if q_bi and d_bi:
            bigram = len(q_bi & d_bi) / math.sqrt(len(q_bi) * len(d_bi))
        else:
            bigram = 0.0
        # 6. 长度比（对称，防止用"文档越长越好"骗分）
        length_ratio = min(len(doc), len(query)) / max(len(doc), len(query))
        # 7. 精确子串
        exact = 1.0 if (len(query) >= 2 and _contains(doc, query)) else 0.0
        return np.asarray(
            [overlap_cos, idf_overlap, coverage, first_match, bigram, length_ratio, exact],
            dtype=np.float64,
        )

    def _hashed_projection(self, query: Sequence[str], doc: Sequence[str]) -> float:
        """query token × doc token 的哈希交叉特征，过一个固定随机投影。

        成本是 ``O(|Q| · |D|)``，所以两边都截断到 48 个 token ——
        真实的 cross encoder 也是靠 ``max_length`` 截断来控制这个代价的
        （注意力是 O(L²)）。
        """

        q = list(query)[:48]
        d = list(doc)[:48]
        if not q or not d:
            return 0.0
        features = np.zeros(self.hashed_features, dtype=np.float64)
        for qt in q:
            for dt in d:
                bucket = _blake_int(qt, dt, f"s{self.seed}") % self.hashed_features
                # 符号也由哈希决定：有符号哈希，冲突时正负抵消一部分，
                # 比纯计数更接近"随机投影"的无偏性
                sign = 1.0 if _blake_int(qt, dt, f"sign{self.seed}") & 1 else -1.0
                features[bucket] += sign
        norm = float(np.linalg.norm(features))
        if norm <= 0.0:
            return 0.0
        features /= norm
        projected = self._projection @ features
        return float(np.tanh(projected) @ self._readout) / math.sqrt(self.dim) * self.NOISE_SCALE

    # -- 便利方法 ---------------------------------------------------------
    def score(self, query: str, doc: str) -> float:
        """单对打分，调试时用。"""

        return float(self.predict([[query, doc]])[0])

    def describe(self) -> Dict[str, Any]:
        """给教程用的自描述。"""

        return {
            "name": type(self).__name__,
            "dim": self.dim,
            "hashed_features": self.hashed_features,
            "seed": self.seed,
            "idf_frozen": self._idf is not None,
            "确定性的": True,
            "能当质量基准用吗": "不能 —— 只认识词面重叠，同义词/改写/跨语言一概无感",
        }


def _contains(doc_tokens: Sequence[str], query_tokens: Sequence[str]) -> bool:
    """查询的 token 序列是否作为**连续子序列**出现在文档里。"""

    n, m = len(doc_tokens), len(query_tokens)
    if m == 0 or m > n:
        return False
    first = query_tokens[0]
    for i in range(n - m + 1):
        if doc_tokens[i] == first and list(doc_tokens[i : i + m]) == list(query_tokens):
            return True
    return False


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# ---------------------------------------------------------------------------
# 真模型（可选依赖）
# ---------------------------------------------------------------------------


class SentenceTransformersCrossEncoder:
    """``sentence_transformers.CrossEncoder`` 的薄封装。

    **导入这个模块不会导入 sentence_transformers**，只有真正调用
    :meth:`predict` 时才会去 import，缺依赖时抛 :class:`NotSupportedError`
    并给出安装命令。这是刻意的：教程里"接口长什么样"和"我没装 GPU 依赖"
    是两件应该能分开的事。

    常用模型（真实名字，来自 sentence-transformers 的 MS MARCO 系列）：

    * ``cross-encoder/ms-marco-TinyBERT-L-6`` —— 真实 LanceDB 的默认值，
      6 层，最快，质量一般；
    * ``cross-encoder/ms-marco-MiniLM-L-6-v2`` —— 质量/速度最平衡，最常用；
    * ``cross-encoder/ms-marco-MiniLM-L-12-v2`` —— 更慢更准；
    * ``BAAI/bge-reranker-base`` / ``bge-reranker-v2-m3`` —— 中英文都不错。
    """

    DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        device: Optional[str] = None,
        batch_size: int = 32,
        trust_remote_code: bool = True,
        **kwargs: Any,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self.trust_remote_code = trust_remote_code
        self.kwargs = kwargs
        self._model: Any = None

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                import sentence_transformers  # noqa: F401
            except ImportError:
                raise NotSupportedError(
                    "sentence_transformers 未安装，无法加载真实 cross encoder",
                    hint=(
                        "安装命令: pip install sentence-transformers "
                        "（会自动带上 torch，约 2GB）；"
                        "不装的话用内置的 HashCrossEncoder，它在离线下完全确定"
                    ),
                ) from None
            self._model = sentence_transformers.CrossEncoder(
                self.model_name,
                device=self.device,
                trust_remote_code=self.trust_remote_code,
                **self.kwargs,
            )
        return self._model

    def predict(self, pairs: Sequence[Sequence[str]]) -> np.ndarray:
        model = self.model  # 缺依赖时在这一行抛 NotSupportedError
        rows = [[str(p[0]), str(p[1])] for p in pairs]
        if not rows:
            return np.zeros(0, dtype=np.float32)
        scores = model.predict(rows, batch_size=self.batch_size)
        return np.asarray(scores, dtype=np.float32).ravel()


# ---------------------------------------------------------------------------
# 重排器
# ---------------------------------------------------------------------------


class CrossEncoderReranker(Reranker):
    """用 cross encoder 给候选重新打分。

    Parameters
    ----------
    model : CrossEncoderModel, optional
        任何有 ``predict(pairs) -> np.ndarray`` 的对象。
        默认 :class:`HashCrossEncoder`（离线、确定），换
        :class:`SentenceTransformersCrossEncoder` 即可上真模型。
    model_name : str, optional
        官方 API（``lancedb.rerankers.CrossEncoderReranker``）的第一个参数是
        ``model_name``，这里保留这个别名，方便官方文档里的例子原样搬过来：
        ``model_name="cross-encoder/ms-marco-MiniLM-L-6-v2"``
        等价于 ``model=SentenceTransformersCrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")``。
        和 ``model`` 不能同时给。
    column : str, default "text"
        从哪一列取文档正文。找不到就按 ``text`` / ``content`` / ``document``
        / ``body`` / ``passage`` 的顺序找。
    top_k : int, default 100
        **只重排前 top_k 条**。这是本模块最重要的工程参数：cross encoder
        是 O(N) 次前向，N 必须被卡在 20~100。
    batch_size : int, default 32
        只是记录在案（真模型的批大小），离线模型用不上。
    """

    #: 猜正文列时的候选顺序。
    TEXT_COLUMN_CANDIDATES = ("text", "content", "document", "body", "passage", "title")

    def __init__(
        self,
        model: Optional[Any] = None,
        *,
        model_name: Optional[str] = None,
        column: str = "text",
        top_k: int = DEFAULT_RERANK_TOP_K,
        return_score: str = "relevance",
        batch_size: int = 32,
        id_column: str = ROWID_COL,
    ) -> None:
        super().__init__(return_score, id_column=id_column)
        if model is not None and model_name is not None:
            raise QueryError("model 和 model_name 只能给一个")
        if model is None and model_name is not None:
            model = SentenceTransformersCrossEncoder(model_name)
        self.model: Any = model if model is not None else HashCrossEncoder()
        if not hasattr(self.model, "predict"):
            raise QueryError(
                "model 必须有 predict(pairs) 方法",
                got=type(self.model).__name__,
            )
        self.column = column
        top_k = int(top_k)
        if top_k <= 0:
            raise QueryError("top_k 必须为正整数", top_k=top_k)
        self.top_k = top_k
        self.batch_size = int(batch_size)

    def __str__(self) -> str:  # pragma: no cover
        return f"CrossEncoderReranker(model={type(self.model).__name__}, top_k={self.top_k})"

    # -- 正文提取 ---------------------------------------------------------
    def text_of(self, row: Row) -> str:
        if self.column in row and row[self.column] is not None:
            return str(row[self.column])
        for candidate in self.TEXT_COLUMN_CANDIDATES:
            if candidate in row and row[candidate] is not None:
                return str(row[candidate])
        raise QueryError(
            "这一行里找不到正文列",
            column=self.column,
            tried=list(self.TEXT_COLUMN_CANDIDATES),
            available=[k for k in row if not k.startswith("_")],
            hint="构造 CrossEncoderReranker 时用 column=... 指定正文列",
        )

    # -- 打分 -------------------------------------------------------------
    def score_rows(self, query: Optional[str], rows: Sequence[Row]) -> List[float]:
        if query is None:
            raise QueryError(
                "cross encoder 必须要有查询文本",
                hint="rerank(query=None, ...) 对 cross encoder 无意义",
            )
        pairs = [[query, self.text_of(row)] for row in rows]
        scores = np.asarray(self.model.predict(pairs), dtype=np.float64).ravel()
        if scores.size != len(pairs):  # pragma: no cover - 模型契约保护
            raise QueryError(
                "model.predict 返回的分数个数和输入对数不一致",
                expected=len(pairs),
                got=int(scores.size),
            )
        return [float(s) for s in scores]

    # -- 入口 -------------------------------------------------------------
    def rerank_vector(
        self, query: Optional[str], vector_results: Any, *, k: Optional[int] = None, **_: Any
    ) -> List[Row]:
        """只重排前 ``top_k`` 条，**其余保持原顺序挂在后面**。

        输入被假定是"已经按检索分排好序的候选集"（这正是两阶段检索的形态）。
        尾巴上那些没被重排的行分数是 ``None``，会排在所有有分数的行之后 ——
        这样"重排没有把任何一条候选弄丢"这件事在结果里是可验证的。
        """

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
        """先合并两路（向量在前，去重），再走单列表重排。

        注意顺序：cross encoder **不看召回分数**，所以合并的方式对结果
        没有影响 —— 但"谁进得了前 top_k"有影响。这就是为什么 top-k 截断
        要在合并之后做，而不是在两路各自做一遍。
        """

        merged = self.merge_results(vector_results, fts_results)
        return self.rerank_vector(query, merged, k=k)

    def rerank_fts(
        self, query: Optional[str], fts_results: Any, *, k: Optional[int] = None, **kwargs: Any
    ) -> List[Row]:
        return self.rerank_vector(query, fts_results, k=k)
