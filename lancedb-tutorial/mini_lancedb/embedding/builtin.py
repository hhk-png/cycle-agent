"""内置的**离线**向量化函数：不联网、不装模型、结果确定。

这一组函数的定位很明确：**它们是"兜底"和"教具"，不是"好用"**。

## 为什么需要兜底

真实项目里 embedding 的调用链是：装 ``sentence-transformers`` → 下
``all-MiniLM-L6-v2``（90MB）→ 起 torch → 编码。这条链在三个场景下会断：

1. **CI / 单元测试** —— 不想让测试依赖网络和 2GB 的 torch；
2. **离线环境 / 内网** —— 拿不到 HuggingFace；
3. **第一次跑通 Pipeline** —— 你只想验证"写入 → 建索引 → 查询"
   这条链是否通，不想在模型上浪费时间。

这三件事都需要一个"**确定、快、但有结构**"的向量化函数。
"有结构"是关键：如果返回随机向量，PCA、聚类、ANN 的召回率全是噪音，
你会花一整天去 debug 一个根本不存在的 bug。

## hashing trick（哈希技巧）

:class:`HashingEmbeddingFunction` 用最经典的方案：

.. code-block:: text

    for token in tokenize(text):
        bucket = blake2b(token) % dim          # 哈希到固定维度
        sign   = ±1 (由哈希的另一位决定)        # 有符号，降低冲突偏差
        vec[bucket] += sign * (1 + log(tf))    # 次线性 TF
    vec /= ||vec||                             # L2 归一化

四个细节都不是装饰：

* **哈希而非词表** —— 不需要预先构建词表，任何新词（新域名、新术语、
  错别字）都能立即编码，维度永远固定。代价是**碰撞**：两个不同的词
  落到同一个桶里就永久混在一起，没法像词表那样分开。维度越大碰撞越少
  （``dim=2**18`` 时几乎可以忽略），这也是"hashing 向量一般比
  learned 向量维度大得多"的原因。
* **有符号（signed）** —— 纯计数哈希在碰撞时误差**单向累积**
  （两个不相关的词只会让那个桶更大）；有符号哈希的碰撞项正负相消，
  期望上是无偏的。这是个 10 行的改动，效果差别却能看得出来。
* **次线性 TF（``1 + log(tf)``）** —— 一个词出现 100 次不代表它重要 100 倍。
  不用 log 的话，长文档/重复词会主导整个向量方向。
* **L2 归一化** —— 归一化之后点积 = 余弦相似度，和 cosine 索引的语义一致，
  也避免"长文本天然分数高"。

它认得的只有**词面重叠**。"猫" 和 "kitty" 的距离和 "猫" 和 "宇宙" 一样远。
所以它适合做兜底、适合做测试、**不适合**用来评估检索质量。

## 其余三个

* :class:`CharNgramEmbeddingFunction` —— 字符 n-gram 哈希。**中文/日文
  的首选兜底**：不需要分词器，对错别字和轻微改写更鲁棒；
* :class:`DeterministicFakeEmbedding` —— 种子高斯。唯一用途是**性能测试**
  （"1 亿条 768 维向量要多大内存"），它**没有任何语义结构**，
  拿它评估召回率得到的数字毫无意义 —— 这一条必须写进文档，
  否则一定会有人拿它跑出一个"召回率 30%"然后去 debug 索引；
* :class:`BOWEmbeddingFunction` —— 固定词表上的**精确**词袋向量。
  维度小、可手算，适合写"这条查询应该命中第 3 行"这种可验证的测试。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ..errors import QueryError
from .base import TextEmbeddingFunction, register_embedding_function

__all__ = [
    "tokenize",
    "HashingEmbeddingFunction",
    "CharNgramEmbeddingFunction",
    "DeterministicFakeEmbedding",
    "BOWEmbeddingFunction",
    "builtin_function_names",
]


def stable_hash(*parts: str, bits: int = 64) -> int:
    """跨进程稳定的哈希。

    **绝对不能用内置 ``hash()``。** Python 对 ``str`` 的哈希带进程级随机盐
    （``PYTHONHASHSEED``），同一个字符串在两个进程里桶号不同 ——
    于是"离线写入的索引"和"另一个进程里发起的查询"会对不上，
    而且是静默对不上。测试里有一条跨进程比对字节的用例专门盯这件事。
    """

    h = hashlib.blake2b(digest_size=bits // 8)
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return int.from_bytes(h.digest(), "little")


def tokenize(text: str, *, lowercase: bool = True) -> List[str]:
    """极简分词：ASCII 单词整体保留，CJK **逐字**切开。

    为什么中文要逐字切：没有分词器的时候，"机器学习"如果整体作为一个 token，
    那么"机器学习算法"和"机器学习"的重叠度会是 0（token 不相等），
    这对中文来说完全不可接受。逐字切之后两者共享 4 个字，"相关"这件事
    至少被部分捕捉到了。它显然不如真正的分词器（"研究生命起源"会被切成
    单个字），但作为兜底足够了 —— 想要质量就上 :class:`CharNgramEmbeddingFunction`
    或真模型。
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
            # 空白和标点都是分隔符，本身不进词表
            flush()
    flush()
    if lowercase:
        out = [t.lower() for t in out]
    return out


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm <= 0.0:
        # 空文本 → 零向量。**不要**归一化成一个任意单位向量：
        # 那会让"空文本"和"任意文本"的距离变成随机值。
        return vec
    return vec / norm


# ---------------------------------------------------------------------------
# 1. 哈希词袋
# ---------------------------------------------------------------------------


@register_embedding_function("hashing", builtin=True)
class HashingEmbeddingFunction(TextEmbeddingFunction):
    """哈希词袋（hashing trick），带次线性 TF 和 L2 归一化。

    Parameters
    ----------
    dim : int, default 128
        桶数。演示用 128 足够；生产上 2**18 起步（见模块文档的碰撞讨论）。
    lowercase : bool, default True
    sublinear_tf : bool, default True
        是否用 ``1 + log(tf)`` 压制词频。关掉之后长文档会主导向量方向。
    """

    name = "hashing-trick"

    def __init__(
        self,
        dim: int = 128,
        *,
        lowercase: bool = True,
        sublinear_tf: bool = True,
    ) -> None:
        if dim <= 0:
            raise QueryError("dim 必须为正整数", dim=dim)
        super().__init__(dim=dim, lowercase=lowercase, sublinear_tf=sublinear_tf)
        self.dim = int(dim)
        self.lowercase = lowercase
        self.sublinear_tf = sublinear_tf
        # 哈希结果缓存：同一批文本里同一个词会被反复遇到，
        # 缓存把 SipHash/blake2b 的调用次数降到词表大小。
        self._bucket_cache: Dict[str, int] = {}

    def _model_config(self) -> Dict[str, Any]:
        return {
            "dim": self.dim,
            "lowercase": self.lowercase,
            "sublinear_tf": self.sublinear_tf,
        }

    def ndims(self) -> int:
        return self.dim

    def _bucket(self, token: str) -> int:
        cached = self._bucket_cache.get(token)
        if cached is None:
            cached = stable_hash(token) % self.dim
            self._bucket_cache[token] = cached
        return cached

    def _sign(self, token: str) -> float:
        # 用哈希的**另一位**决定符号，和桶号独立
        return 1.0 if stable_hash("sign", token) & 1 else -1.0

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        return [self._encode(text) for text in texts]

    def _encode(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = tokenize(text, lowercase=self.lowercase)
        if not tokens:
            return vec
        # 先数**每个词**的词频，再逐词累加。不能直接在桶上累加计数：
        # 那样就分不清"一个词出现了 10 次"和"10 个不同词撞进同一个桶"，
        # 而次线性 TF 只该压制前者。
        tf: Dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        for token, count in tf.items():
            if self.sublinear_tf and count > 1:
                # 1 + log(tf)：tf=1 → 1.0，tf=10 → 3.3，tf=100 → 5.6
                weight = 1.0 + math.log(count)
            else:
                weight = float(count)
            # 有符号累加：两个不同词撞进同一个桶时正负相消（期望无偏）
            vec[self._bucket(token)] += self._sign(token) * weight
        return _l2_normalize(vec)


# ---------------------------------------------------------------------------
# 2. 字符 n-gram
# ---------------------------------------------------------------------------


@register_embedding_function("char-ngram", builtin=True)
class CharNgramEmbeddingFunction(TextEmbeddingFunction):
    """字符 n-gram 哈希。**中文/日文的首选兜底。**

    Parameters
    ----------
    dim : int, default 256
    n : int, default 3
        3-gram 是通用语言上的经验甜点：够短所以对错别字和变形鲁棒，
        够长所以还能承载一点局部顺序信息（2-gram 几乎退化成词袋）。
    lowercase : bool, default True

    设计上它**根本不需要分词**。"机器学习"的 3-gram 是
    ``<机`` ``机器`` ``器学`` ``学习`` ``习>``（前后补边界符），
    "机器学习算法"共享前四个。而纯粹的单词级哈希在中文上必须先猜对分词边界。

    **但实测下来，在本实现里它的整体区分度不如
    :class:`HashingEmbeddingFunction`。** 用 ``tests/test_embedding.py``
    的 ``RELATED_PAIRS`` 夹具跑（``examples/verify/ch22_embedding_functions.py``
    会打印全表）：

    * ``hashing``    相关 ``[0.4330, 0.7385]``、不相关 ``[0.0000, 0.2835]``、
      最小间隔 ``0.3658``；
    * ``char-ngram`` 相关 ``[0.4523, 0.7031]``、不相关 ``[-0.1291, 0.3120]``、
      最小间隔 ``0.2644``。

    注意这是**平均意义上的弱，不是逐对都弱**：单看
    ``machine learning models`` 那一组，char-ngram 是 +0.7031，
    反而高于 hashing 的 +0.6124。真正的问题在**最差的那一对**——
    最小间隔差了约 28%，而且它的不相关相似度会下探到负数。

    原因是本实现的 ``HashingEmbeddingFunction`` 在分词时**已经对 CJK
    逐字（unigram）切开**，所以它早就吃到了"不需要分词"这个红利，
    而 char-ngram 还要额外付出向量更稠密、语义粒度更粗的代价。
    走字符二元组（bigram）的是另一条线——``fts`` 的 tokenizer，
    和这里的嵌入分词不是同一件事。

    结论是：**"哪个兜底嵌入更好"取决于你的分词器，不能只看名字。**
    如果换成一个对 CJK 一窍不通的空白符分词器，
    char-ngram 会立刻反超；在这个实现里则相反。
    这类"理论上应该更好、实测却更差"的结论，只能靠量，不能靠推。

    （这段数字改过两次。第一版写的是 ``0.567 / 0.756 / 两者都接近 0``，
    那三个数是拿三组临时编的中文对量的，不是夹具里的组，**对不上**。
    第二版我把 ``−0.123`` 说成"第三组"的值——**也错了**：夹具第三组
    （``python programming language``）的 char-ngram 是
    ``+0.5944``（相关）/ ``+0.3120``（不相关），两个都是正的；
    全表唯一那个负值 ``−0.1291`` 出现在**第六组**（``检索 增强 生成``）。
    **同一个数字被安置到错误的组上**，是这一类错误里最难发现的一种——
    数字本身是真的，只有归属是假的。所以这里干脆不引具体某组的值，
    要查就去看上面那张区间表。）
    """

    name = "char-ngram"

    def __init__(self, dim: int = 256, *, n: int = 3, lowercase: bool = True) -> None:
        if dim <= 0:
            raise QueryError("dim 必须为正整数", dim=dim)
        if n <= 0:
            raise QueryError("n 必须为正整数", n=n)
        super().__init__(dim=dim, n=n, lowercase=lowercase)
        self.dim = int(dim)
        self.n = int(n)
        self.lowercase = lowercase

    def _model_config(self) -> Dict[str, Any]:
        return {"dim": self.dim, "n": self.n, "lowercase": self.lowercase}

    def ndims(self) -> int:
        return self.dim

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        return [self._encode(text) for text in texts]

    def _ngrams(self, text: str) -> List[str]:
        s = text.lower() if self.lowercase else text
        # 把连续空白压成一个空格，避免"两个空格"造出一堆无意义的 n-gram
        s = " ".join(s.split())
        if not s:
            return []
        padded = ("\x02" * (self.n - 1)) + s + ("\x03" * (self.n - 1))
        return [padded[i : i + self.n] for i in range(len(padded) - self.n + 1)]

    def _encode(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        grams = self._ngrams(text)
        if not grams:
            return vec
        for gram in grams:
            bucket = stable_hash("cngram", gram) % self.dim
            sign = 1.0 if stable_hash("cngram-sign", gram) & 1 else -1.0
            vec[bucket] += sign
        return _l2_normalize(vec)


# ---------------------------------------------------------------------------
# 3. 确定性随机（只给性能测试用）
# ---------------------------------------------------------------------------


@register_embedding_function("fake", builtin=True)
class DeterministicFakeEmbedding(TextEmbeddingFunction):
    """由文本哈希播种的高斯向量。**它没有任何语义结构。**

    .. warning::

       **绝对不能**用它评估召回率 / 相关性 / 聚类质量。
       它的设计目标只有一个：用可控的维度生成**确定、可复现**的向量，
       让"1 亿条 768 维向量占多少内存""建 IVF_PQ 要多久"这类
       纯性能问题可以离线复现。

       用它的结果去判断质量，你会得到一个**漂亮但完全随机**的
       召回率曲线，然后花几天去 debug 一个不存在的索引 bug。
       这不是假设 —— 真实项目里发生过很多次。

    Parameters
    ----------
    dim : int, default 128
    seed : int, default 0
        换个种子 = 换一组完全不同的向量。同一个种子永远同结果。

    和 :class:`HashingEmbeddingFunction` 的区别：哈希词袋对
    "共享词"的文本给出更近的向量（有结构）；这个函数对**相同文本**
    给相同向量、对**任何不同文本**给近似正交的随机向量（无结构）。
    """

    name = "deterministic-fake"

    def __init__(self, dim: int = 128, *, seed: int = 0) -> None:
        if dim <= 0:
            raise QueryError("dim 必须为正整数", dim=dim)
        super().__init__(dim=dim, seed=seed)
        self.dim = int(dim)
        self.seed = int(seed)

    def _model_config(self) -> Dict[str, Any]:
        return {"dim": self.dim, "seed": self.seed}

    def ndims(self) -> int:
        return self.dim

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        out: List[Optional[np.ndarray]] = []
        for text in texts:
            seed = stable_hash("fake", str(self.seed), text) % (2**63)
            rng = np.random.default_rng(seed)
            vec = rng.standard_normal(self.dim).astype(np.float32)
            out.append(_l2_normalize(vec))
        return out


# ---------------------------------------------------------------------------
# 4. 精确词袋
# ---------------------------------------------------------------------------


@register_embedding_function("bow", builtin=True)
class BOWEmbeddingFunction(TextEmbeddingFunction):
    """固定词表上的**精确**词袋向量 —— 手算就能验证的那种。

    Parameters
    ----------
    vocabulary : Sequence[str]
        词表。向量第 i 维 = 第 i 个词的出现次数（或归一化后的值）。
    lowercase : bool, default True
    binary : bool, default False
        True 表示只记"出现过没有"（0/1），不算次数。
    normalize : str, default "l2"
        ``"l2"`` / ``"none"``。``"none"`` 时向量就是原始计数，
        测试里可以一眼看出"这条文本里有 2 个 cat"。

    Examples
    --------
    >>> fn = BOWEmbeddingFunction(["cat", "dog"], normalize="none")
    >>> fn.compute_source_vectors("cat cat dog")[0].tolist()
    [2.0, 1.0]

    它显然是"最没用的" embedding —— 词表外的东西一律编码成零向量。
    但它也是唯一一个**可以被完全手算验证**的实现，
    所以每个 embedding 管道的测试都值得留一个它。
    """

    name = "exact-bow"

    def __init__(
        self,
        vocabulary: Sequence[str],
        *,
        lowercase: bool = True,
        binary: bool = False,
        normalize: str = "l2",
    ) -> None:
        vocab = [str(w) for w in vocabulary]
        if not vocab:
            raise QueryError("词表不能为空")
        if len(set(vocab)) != len(vocab):
            raise QueryError("词表里不能有重复词", vocabulary=vocab)
        if normalize not in ("l2", "none"):
            raise QueryError("normalize 只支持 'l2' / 'none'", normalize=normalize)
        super().__init__(
            vocabulary=vocab, lowercase=lowercase, binary=binary, normalize=normalize
        )
        self.vocabulary = vocab
        self.lowercase = lowercase
        self.binary = binary
        self.normalize = normalize
        self._index = {word: i for i, word in enumerate(vocab)}

    def _model_config(self) -> Dict[str, Any]:
        return {
            "vocabulary": list(self.vocabulary),
            "lowercase": self.lowercase,
            "binary": self.binary,
            "normalize": self.normalize,
        }

    def ndims(self) -> int:
        return len(self.vocabulary)

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        return [self._encode(text) for text in texts]

    def _encode(self, text: str) -> np.ndarray:
        vec = np.zeros(len(self.vocabulary), dtype=np.float32)
        for token in tokenize(text, lowercase=self.lowercase):
            idx = self._index.get(token)
            if idx is None:
                continue  # 词表外的词直接丢弃（OOV）
            vec[idx] = 1.0 if self.binary else vec[idx] + 1.0
        if self.normalize == "l2":
            vec = _l2_normalize(vec)
        return vec

    def describe(self) -> Dict[str, Any]:
        info = super().describe()
        info["vocabulary"] = list(self.vocabulary)
        return info


#: 本模块注册的所有名字，给教程/测试用。
def builtin_function_names() -> List[str]:
    return ["hashing", "char-ngram", "fake", "bow"]
