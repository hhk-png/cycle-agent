"""倒排索引 + BM25 打分（``FTS`` 索引）。

## 一、倒排索引到底"倒"在哪

正排是 ``文档 → 词项``；倒排把它翻过来存 ``词项 → 文档``：

```
term        -> [(doc, tf, positions), ...]
"quick"     -> [(0, 2, [1, 5]), (7, 1, [3])]
"brown"     -> [(0, 1, [2])]
```

于是"哪些文档含 quick"从"扫全表"变成"查一次字典"。这就是全文检索能做到
亚线性的全部秘密 —— 没有别的魔法。

物理布局（对齐 Lance 的 ``invert`` / ``tokens`` 两个文件）：

* ``terms``：**排好序**的词项表。排序不是为了查找（查找走哈希），
  而是为了让"按词项前缀扫描""合并两个索引"这类操作能顺序做；
* ``doc_offsets``：长度 ``num_terms + 1`` 的前缀和数组，
  ``[doc_offsets[t], doc_offsets[t+1])`` 就是词项 ``t`` 的 postings 区间。
  一个 term 一段连续内存 —— 遍历时全是顺序访问，这是倒排索引快的第二个原因；
* ``postings_doc`` / ``postings_tf``：摊平的 (文档号, 词频) 两条平行数组；
* ``pos_offsets`` + ``positions``：位置用 **varint + 差分** 压成一段字节流。
  位置是 (文档内) 递增的，差分之后全是小整数，一个字节就能装下大多数。
  （真实 Lance 更进一步，用 bitpacking 按块打包差分值，比 varint 还省。）

## 二、BM25：为什么是这一个公式

```
score(q, d) = Σ_t IDF(t) · tf·(k1+1) / (tf + k1·(1 - b + b·|d|/avgdl))
IDF(t)      = ln(1 + (N - df + 0.5) / (df + 0.5))
```

这就是 **Lucene / Lance 用的那个公式**（LanceDB 的 FTS 直接调 tantivy，
tantivy 的 ``Bm25Weight`` 与 Lucene 的 ``BM25Similarity`` 一致）。
逐项解释：

* ``IDF`` —— 词越稀有分越高。注意是 ``ln(1 + ...)`` 而不是经典的
  ``ln((N-df+0.5)/(df+0.5))``：后者的分子会随着 df 逼近 N 变成负数，
  **一个几乎出现在所有文档里的词会获得负分**，于是"文档里多一个词反而降分"。
  Lucene 在 4.0 加了那个 ``1 +`` 就是为了把 IDF 压成恒正。
* ``tf·(k1+1) / (tf + k1·(...))`` —— 词频的**饱和**曲线。``tf`` 从 1 涨到 2
  得分大涨，从 50 涨到 51 几乎没差。这正是 BM25 打败 TF-IDF 的地方：
  关键词重复十遍不该让文档排到十倍前。
  ``k1``（默认 1.2）控制饱和速度：``k1=0`` 退化成"只看有没有"，
  ``k1`` 越大越接近线性。
* ``1 - b + b·|d|/avgdl`` —— **长度归一化**。长文档天然更容易命中任意词，
  所以要按"比平均长度长多少"来打折。``b``（默认 0.75）控制打折力度：
  ``b=0`` 完全不归一化（适合长度本来就该随意的情况，比如日志），
  ``b=1`` 完全归一化（适合长度均匀的正文）。

### `k1`/`b` 的敏感性在测试里是可验证的

* ``b=0`` 时，把文档后面追加一堆无关词，分数**不变**；
* ``b=1`` 时，同样的操作分数**下降**；
* ``k1→0`` 时，同一个词出现 2 次和出现 10 次的分数**趋于相等**。

这三个断言比"分数等于某个数"更能说明 BM25 在干什么。

## 三、⚠️ `_score` 的方向和向量检索**相反**

* 向量检索：``_distance`` —— **越小越好**（L2 距离、cosine 距离）。
* 全文检索：``_score`` —— **越大越好**（BM25 相似度）。

这在混合检索（hybrid search）里是**头号 bug 来源**：两路结果合并时，
向量那一路要升序、FTS 这一路要降序；想做加权融合必须先把两边的分数
归一化到同一个区间，否则一个 0.3 的距离和一个 12.7 的 BM25 分数相加，
等于只保留了 BM25。LanceDB 的做法是分别返回 ``_distance`` 和 ``_score``
两个字段（RRF 融合则干脆只用排名、不看分值），本实现保持同样的约定。

## 四、短语查询与位置空档

``PhraseQuery`` 不要求两个词项"位置相邻"，而是要求它们的**位置差**
等于查询里的偏移差。因为分词器在删停用词时**留了空档**（见
``fts/tokenizer.py`` 的 position increment 说明），所以：

* 原文 ``"quick brown"`` → ``quick@0, brown@1``，位置差 1；
* 原文 ``"quick the brown"`` → ``quick@0, brown@2``，位置差 2，**不匹配**；
* 查询 ``"quick brown"`` 里的偏移差是 1，所以只有前者命中。

如果位置是压缩的（tantivy 的做法），后一种会被误判为命中。这是"位置"这个
字段存在的唯一理由 —— 它不是为了显示。

## 五、CJK 二元组的后果

中文走字符二元组（``ftstokenizer`` 里说明了原因），于是**单查两个字是不够的**：
``"中国"`` 会被切成一个二元组 ``中国``，能直接命中；但 ``"中国人的"``
切成 ``中国/国人/人的``，如果按 OR 查，所有含"中国"或"人"的文档都会进来。
正确做法是短语查询：二元组的位置相邻恰好等价于原文相邻。
所以中文检索里短语查询不是"高级功能"，是**基本功能**。

## 六、本实现刻意没做的事

* **WAND / BlockMax-WAND**：真实引擎靠上界剪枝，只展开有可能进 top-k 的
  文档。这里老老实实把命中集合全部算出来再排序（稠密数组求值），
  在小数据上更简单也更不容易错，代价是 O(命中数) 而不是 O(k)。
* **delta 索引合并**：真实 LanceDB 写入后会建一个小的"补丁索引"，
  查询时和主索引一起搜再合并。这里只支持全量重建。
* **跳表（skip list）**：postings 求交时有它就快了。这里用 numpy 的
  ``np.intersect1d``（本质是排序+归并），语义等价。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np

from ..arrays import Array
from ..errors import IndexBuildError, QueryError
from ..fts.tokenizer import Tokenizer, TokenizerConfig
from ._expr_util import combine_rows, materialize
from .base import (
    And,
    ExactMatch,
    IndexMetadata,
    IndexStore,
    Not,
    Or,
    ScalarIndex,
    ScalarIndexExpr,
)

__all__ = [
    "InvertedIndex",
    "MatchQuery",
    "TermQuery",
    "PhraseQuery",
    "BooleanQuery",
    "parse_query",
    "varint_encode",
    "varint_decode",
]


# ---------------------------------------------------------------------------
# varint（LEB128）
# ---------------------------------------------------------------------------


def varint_encode(values: Iterable[int], out: Optional[bytearray] = None) -> bytearray:
    """把非负整数序列写成 LEB128（每字节 7 位，最高位表示"还有后续"）。

    小整数只占 1 字节 —— 位置差分值绝大多数落在 0..127，所以 varint 对
    "差分后的位置流"几乎是无损压到 1/4（相对 int32）。
    缺点变长编码不能随机访问，必须从头顺序解 —— 这就是为什么要配一个
    ``pos_offsets`` 索引表，让人能直接跳到某个 posting 的起点。
    """

    buf = bytearray() if out is None else out
    for value in values:
        v = int(value)
        if v < 0:
            raise ValueError(f"varint 只编码非负整数，收到 {v}")
        while True:
            byte = v & 0x7F
            v >>= 7
            if v:
                buf.append(byte | 0x80)
            else:
                buf.append(byte)
                break
    return buf


def varint_decode(data: bytes, start: int, end: int) -> List[int]:
    """从 ``data[start:end]`` 解出一串 varint。"""

    out: List[int] = []
    pos = start
    while pos < end:
        shift = 0
        value = 0
        while True:
            if pos >= end:  # 编码被截断 —— 宁可报错也不给半截结果
                raise IndexBuildError("varint 位置流被截断", offset=pos)
            byte = data[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        out.append(value)
    return out


# ---------------------------------------------------------------------------
# 查询表达式
# ---------------------------------------------------------------------------


@dataclass
class MatchQuery(ScalarIndexExpr):
    """FTS 的查询叶子。

    真实 Lance 里对应 ``MatchQuery``（``TantivyQuery``）。
    它继承 ``ScalarIndexExpr`` 是为了能直接塞进 ``And`` / ``Or`` / ``Not``：
    ``fts("hello") AND btree(col, 3..9)`` 就是这样组合出来的。
    """

    query: str
    #: 只取前 k 个（按分数降序）；``None`` 表示全要
    k: Optional[int] = None
    #: 是否把 ``query`` 当成纯文本（True）还是已经解析好的查询树（False）
    parse: bool = True


@dataclass
class QueryNode:
    """查询树的内部节点基类。"""

    def all_terms(self) -> List[str]:
        """收集这棵子树里出现的所有词项（explain / 调试用）。"""

        return []


@dataclass
class TermQuery(QueryNode):
    """单个词项。"""

    term: str

    def all_terms(self) -> List[str]:
        return [self.term]


@dataclass
class PhraseQuery(QueryNode):
    """短语：``terms`` 必须按 ``offsets`` 给出的位置差同时命中。

    ``offsets`` 是**相对于第一个词项**的位置偏移，单调不减。
    两个词的短语通常是 ``[0, 1]``；如果中间夹了被停用词表吃掉的词，
    就会是 ``[0, 2]`` 这种 —— 这正是不该压缩位置的原因。
    """

    terms: List[str]
    offsets: List[int]

    def all_terms(self) -> List[str]:  # type: ignore[override]
        return list(self.terms)

    @property
    def span(self) -> int:
        return self.offsets[-1] if self.offsets else 0


@dataclass
class BooleanQuery(QueryNode):
    """布尔查询 —— 语义**完全照抄 Lucene 的 BooleanQuery**。

    子句分三类：

    * ``must`` —— 必须命中（``+term`` / ``AND``）；
    * ``should`` —— 应该命中。**如果有任何 must 子句，should 就退化成
      纯粹的加分项**（命中更好、不命中也能返回）；如果没有 must，
      则至少要命中一个 should（这就是默认的 OR 语义）；
    * ``must_not`` —— 必须不命中（``-term`` / ``NOT``）。

    这个"should 的语义随 must 是否存在而变"的规则是布尔检索里最反直觉的
    一条，但它是**对的**：``+沙发 -皮质`` 要的是"含沙发的都给我，皮质沙发滚"，
    如果 should 也强制命中，就没法表达"可选加分"了。

    没有 must 也没有 should、只留下 must_not 时，本实现返回"全集减去
    must_not"。Lucene 在这种"纯否定查询"下返回**空集**（它认为没有正向
    子句就没有起点）。我们选择前者，因为它更符合直觉；读者需要知道这是个
    有意的偏差，真实系统里 ``-foo`` 这种查询请自己补一个 ``+something``。
    """

    must: List[QueryNode] = dc_field(default_factory=list)
    should: List[QueryNode] = dc_field(default_factory=list)
    must_not: List[QueryNode] = dc_field(default_factory=list)

    def all_terms(self) -> List[str]:  # type: ignore[override]
        out: List[str] = []
        for group in (self.must, self.should, self.must_not):
            for node in group:
                out.extend(node.all_terms())
        return out

    @property
    def is_empty(self) -> bool:
        return not self.must and not self.should and not self.must_not


#: 解析器内部用的"子句"表示：出现方式 + 节点
_Clause = Tuple[str, QueryNode]

#: 会被当成布尔运算符的裸词（大小写不敏感）。想搜 "and" 这个词本身请加引号。
_OPERATOR_WORDS = {"and", "or", "not"}


@dataclass
class _Atom:
    kind: str  # term | phrase | and | or | not | plus | minus | lparen | rparen
    text: str


def _lex_query(query: str) -> List[_Atom]:
    """把查询串切成原子。

    规则（刻意很小）：

    * 空白是分隔符；
    * ``"..."`` 是短语；
    * ``(`` / ``)`` 分组；
    * ``+`` / ``-`` 在**词首**是出现方式标记。``foo-bar`` 里的 ``-`` 不是 ——
      因为在扫描 ``foo`` 时它已经被当成词内字符吃掉了，所以能走到运算符
      判断的 ``-`` 一定是词首的。
    * 裸词 ``and`` / ``or`` / ``not``（任意大小写）是运算符。
    """

    atoms: List[_Atom] = []
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 1
            atoms.append(_Atom("phrase", query[i + 1 : j]))
            i = j + 1 if j < n else j
            continue
        if c == "(":
            atoms.append(_Atom("lparen", "("))
            i += 1
            continue
        if c == ")":
            atoms.append(_Atom("rparen", ")"))
            i += 1
            continue
        if c in "+-":
            atoms.append(_Atom("plus" if c == "+" else "minus", c))
            i += 1
            continue
        j = i
        while j < n and not query[j].isspace() and query[j] not in '()"':
            j += 1
        word = query[i:j]
        low = word.lower()
        if low in _OPERATOR_WORDS:
            atoms.append(_Atom(low, word))
        else:
            atoms.append(_Atom("term", word))
        i = j
    return atoms


class _QueryParser:
    """递归下降解析器。

    文法（``adjacency`` 表示两个原子挨着写，等价于默认运算符 OR）::

        expr      := or_level
        or_level  := and_level (('OR' | adjacency) and_level)*
        and_level := unary ('AND' unary)*
        unary     := ('+' | '-' | 'NOT') primary | primary
        primary   := '(' expr ')' | PHRASE | TERM

    语义上的两条约定：

    * **相邻 = OR**（搜索引擎的默认），想要求全部命中必须写 ``AND`` 或 ``+``；
    * ``AND`` 会把它两侧的 should 子句**提升**为 must，
      ``(a b) AND c`` 因此是"a、b、c 都得有"，和 Lucene 的经典行为一致。
    """

    def __init__(self, atoms: Sequence[_Atom], tokenizer: Tokenizer) -> None:
        self.atoms = list(atoms)
        self.i = 0
        self.tokenizer = tokenizer
        #: 被分词器吃掉、没进查询树的词项（调试用）
        self.dropped: List[str] = []

    # -- 词法工具 ---------------------------------------------------------
    def _peek(self) -> Optional[_Atom]:
        return self.atoms[self.i] if self.i < len(self.atoms) else None

    def _peek_kind(self) -> Optional[str]:
        atom = self._peek()
        return None if atom is None else atom.kind

    def _next(self) -> _Atom:
        atom = self.atoms[self.i]
        self.i += 1
        return atom

    # -- 入口 -------------------------------------------------------------
    def parse(self) -> BooleanQuery:
        clauses = self._or_level()
        return _build_boolean(clauses)

    # -- 各层 -------------------------------------------------------------
    def _or_level(self) -> List[_Clause]:
        clauses = self._and_level()
        while True:
            kind = self._peek_kind()
            if kind == "or":
                self._next()
                clauses.extend(self._and_level())
            elif kind in ("term", "phrase", "lparen", "plus", "minus", "not"):
                # 相邻 = 隐式 OR
                clauses.extend(self._and_level())
            else:
                break
        return clauses

    def _and_level(self) -> List[_Clause]:
        clauses = self._unary()
        while self._peek_kind() == "and":
            self._next()
            # 两侧的 should 全部提升成 must；must_not 保持不变
            clauses = [_promote(c) for c in clauses]
            clauses.extend(_promote(c) for c in self._unary())
        return clauses

    def _unary(self) -> List[_Clause]:
        kind = self._peek_kind()
        if kind == "plus":
            self._next()
            return [_mark("must", self._primary())]
        if kind in ("minus", "not"):
            self._next()
            return [_mark("must_not", self._primary())]
        return self._primary()

    def _primary(self) -> List[_Clause]:
        atom = self._peek()
        if atom is None:
            return []
        if atom.kind == "lparen":
            self._next()
            inner = self._or_level()
            if self._peek_kind() == "rparen":
                self._next()
            return inner
        if atom.kind == "rparen":
            return []
        if atom.kind in ("term", "phrase"):
            self._next()
            node = self._node(atom)
            if node is None:
                return []
            return [("should", node)]
        # 运算符出现在不该出现的位置（比如 "a AND OR b"）—— 跳过它，
        # 解析器对畸形输入一律"尽量理解"而不是抛异常。
        self._next()
        return []

    # -- 词项构造 ---------------------------------------------------------
    def _node(self, atom: _Atom) -> Optional[QueryNode]:
        tokens = self.tokenizer.tokenize_query(atom.text)
        if not tokens:
            # 整串被停用词/长度过滤吃光了。Lucene 会把这种子句直接丢掉。
            self.dropped.append(atom.text)
            return None
        if len(tokens) == 1:
            return TermQuery(tokens[0].text)
        positions = [t.position if t.position is not None else i for i, t in enumerate(tokens)]
        base = positions[0]
        return PhraseQuery(
            terms=[t.text for t in tokens],
            offsets=[p - base for p in positions],
        )


def _promote(clause: _Clause) -> _Clause:
    occur, node = clause
    return ("must", node) if occur == "should" else clause


def _mark(occur: str, clauses: List[_Clause]) -> _Clause:
    """给一个（可能含多个子句的）括号组打上出现方式标记。

    只有单个子句时直接标记它；多个子句时必须**先包成一个布尔节点**，
    否则 ``+(a OR b)`` 会被摊平并错误地变成"a 和 b 都要"。
    """

    if not clauses:
        # 空组：包一个永远不会命中的布尔节点（"什么都没写"）
        return occur, BooleanQuery()
    if len(clauses) == 1:
        return occur, clauses[0][1]
    return occur, _build_boolean(clauses)


def _build_boolean(clauses: Sequence[_Clause]) -> BooleanQuery:
    must: List[QueryNode] = []
    should: List[QueryNode] = []
    must_not: List[QueryNode] = []
    for occur, node in clauses:
        if occur == "must":
            must.append(node)
        elif occur == "must_not":
            must_not.append(node)
        else:
            should.append(node)
    return BooleanQuery(must=must, should=should, must_not=must_not)


def parse_query(text: str, tokenizer: Optional[Tokenizer] = None) -> BooleanQuery:
    """把查询串解析成 :class:`BooleanQuery`（独立入口，便于测试与教学）。"""

    tok = tokenizer if tokenizer is not None else Tokenizer()
    return _QueryParser(_lex_query(text), tok).parse()


# ---------------------------------------------------------------------------
# 倒排索引
# ---------------------------------------------------------------------------


class InvertedIndex(ScalarIndex):
    """``FTS`` 索引：倒排表 + BM25。

    生命周期和别的标量索引一样：``build`` → ``search`` / ``search_ranked``
    → ``save`` / ``load``。区别在于它**不只回答"有没有"，还回答"有多相关"** ——
    所以它有两个出口：

    * :meth:`search` 走 ``ScalarIndex`` 的统一接口，返回 ``ExactMatch``
      （行号集合，无排序）。这是给 ``And`` / ``Or`` / 谓词下推用的；
    * :meth:`search_ranked` 返回 ``(row_ids, scores)``，按 ``_score`` **降序**。
      这是给最终结果用的。
    """

    type_name = "FTS"

    def __init__(
        self,
        metadata: IndexMetadata,
        *,
        tokenizer: Optional[Tokenizer] = None,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        super().__init__(metadata)
        self.tokenizer = tokenizer or Tokenizer()
        self.k1 = float(k1)
        self.b = float(b)

        # -- 词项表 --------------------------------------------------------
        self.term_list: List[str] = []
        self.term_index: Dict[str, int] = {}
        self.doc_freqs: np.ndarray = np.zeros(0, dtype=np.uint32)
        # -- postings（摊平） ----------------------------------------------
        self.doc_offsets: np.ndarray = np.zeros(1, dtype=np.int64)
        self.postings_doc: np.ndarray = np.zeros(0, dtype=np.uint32)
        self.postings_tf: np.ndarray = np.zeros(0, dtype=np.uint32)
        # -- 位置（varint 差分流 + 字节偏移表） ----------------------------
        self.pos_offsets: np.ndarray = np.zeros(1, dtype=np.int64)
        self.pos_data: bytes = b""
        self.has_positions: bool = bool(self.tokenizer.with_position)
        # -- 文档侧 --------------------------------------------------------
        self.doc_lengths: np.ndarray = np.zeros(0, dtype=np.uint32)
        self.avg_doc_length: float = 0.0
        #: 上次 ``search_ranked`` 的中间统计（explain 用）
        self.last_search_stats: Dict[str, Any] = {}

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls,
        values: Array,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "InvertedIndex":
        """``values`` 必须是字符串列（或能按字符串解释的列）。

        ``metadata.params`` 里可以放：

        * ``tokenizer``：``TokenizerConfig`` 或它的 dict 形式；
        * ``k1`` / ``b``：BM25 参数。
        """

        params = dict(metadata.params or {})
        raw_cfg = params.get("tokenizer")
        if isinstance(raw_cfg, TokenizerConfig):
            config = raw_cfg
        elif isinstance(raw_cfg, Mapping):
            config = TokenizerConfig.from_dict(raw_cfg)
        elif raw_cfg is None:
            config = TokenizerConfig()
        else:
            raise IndexBuildError("tokenizer 参数必须是 TokenizerConfig 或 dict")

        index = cls(
            metadata,
            tokenizer=Tokenizer(config),
            k1=float(params.get("k1", 1.2)),
            b=float(params.get("b", 0.75)),
        )
        index._build_from(values, row_ids)
        # 把实际生效的配置回填进元数据 —— 索引必须自描述，
        # 否则"用哪套配置查"就只能靠人记着，那是灾难的开始。
        metadata.params.setdefault("tokenizer", config.to_dict())
        metadata.params.setdefault("k1", index.k1)
        metadata.params.setdefault("b", index.b)
        metadata.num_indexed_rows = int(len(index.row_ids))
        metadata.size_bytes = int(index.size_bytes)
        return index

    def _build_from(self, values: Array, row_ids: np.ndarray) -> None:
        row_ids = np.asarray(row_ids, dtype=np.int64)
        texts = list(values.to_pylist())
        if len(texts) != len(row_ids):
            raise IndexBuildError(
                "列长度与行号长度不符", rows=len(texts), row_ids=len(row_ids)
            )
        n = len(row_ids)
        self.row_ids = row_ids

        # 先在 Python 侧聚合成 term -> {doc: (tf, positions)}，最后一次性摊平。
        # 这样只需一趟构建、没有反复扩容的 numpy 数组。
        postings: Dict[str, Dict[int, Tuple[int, List[int]]]] = {}
        doc_lengths = np.zeros(n, dtype=np.uint32)

        for local, text in enumerate(texts):
            if text is None:
                continue  # null 行 = 空文档，不进任何 postings
            if not isinstance(text, str):
                text = str(text)
            tokens = self.tokenizer.tokenize(text)
            if not tokens:
                continue
            doc_lengths[local] = len(tokens)
            per_doc: Dict[str, Tuple[int, List[int]]] = {}
            for tok in tokens:
                tf, positions = per_doc.get(tok.text, (0, []))
                positions = list(positions)
                if tok.position is not None:
                    positions.append(int(tok.position))
                per_doc[tok.text] = (tf + 1, positions)
            for term, (tf, positions) in per_doc.items():
                postings.setdefault(term, {})[local] = (tf, positions)

        self.doc_lengths = doc_lengths
        total = int(doc_lengths.sum())
        self.avg_doc_length = (total / n) if n else 0.0

        term_list = sorted(postings)
        num_terms = len(term_list)
        self.term_list = term_list
        self.term_index = {t: i for i, t in enumerate(term_list)}

        num_postings = sum(len(d) for d in postings.values())
        doc_offsets = np.zeros(num_terms + 1, dtype=np.int64)
        postings_doc = np.zeros(num_postings, dtype=np.uint32)
        postings_tf = np.zeros(num_postings, dtype=np.uint32)
        pos_offsets = np.zeros(num_postings + 1, dtype=np.int64)
        doc_freqs = np.zeros(num_terms, dtype=np.uint32)
        pos_buf = bytearray()

        cursor = 0
        for ti, term in enumerate(term_list):
            docs = sorted(postings[term])
            doc_freqs[ti] = len(docs)
            for local in docs:
                tf, positions = postings[term][local]
                postings_doc[cursor] = local
                postings_tf[cursor] = tf
                if self.has_positions and positions:
                    # 差分编码：第一个位置存绝对值，其余存与前一个的差。
                    # 位置在文档内递增，所以差分值很小，varint 平均一字节。
                    prev = 0
                    for p in positions:
                        varint_encode([p - prev], pos_buf)
                        prev = p
                cursor += 1
                pos_offsets[cursor] = len(pos_buf)
            doc_offsets[ti + 1] = cursor

        self.doc_freqs = doc_freqs
        self.doc_offsets = doc_offsets
        self.postings_doc = postings_doc
        self.postings_tf = postings_tf
        self.pos_offsets = pos_offsets
        self.pos_data = bytes(pos_buf)

    # -- 基本量 -----------------------------------------------------------
    @property
    def num_docs(self) -> int:
        return int(len(self.row_ids))

    @property
    def num_terms(self) -> int:
        return len(self.term_list)

    @property
    def num_postings(self) -> int:
        return int(len(self.postings_doc))

    def idf(self, doc_freq: int) -> float:
        """``ln(1 + (N - df + 0.5)/(df + 0.5))`` —— 恒正版本（Lucene / tantivy）。"""

        n = self.num_docs
        return math.log(1.0 + (n - doc_freq + 0.5) / (doc_freq + 0.5))

    # -- postings 访问 ----------------------------------------------------
    def _posting_range(self, term: str) -> Optional[Tuple[int, int]]:
        ti = self.term_index.get(term)
        if ti is None:
            return None
        return int(self.doc_offsets[ti]), int(self.doc_offsets[ti + 1])

    def postings(self, term: str) -> Tuple[np.ndarray, np.ndarray]:
        """返回 ``(局部文档号, 词频)`` 两条数组；词项不存在则返回空数组。"""

        rng = self._posting_range(term)
        if rng is None:
            return np.zeros(0, dtype=np.uint32), np.zeros(0, dtype=np.uint32)
        lo, hi = rng
        return self.postings_doc[lo:hi], self.postings_tf[lo:hi]

    def positions_of(self, term: str, local_doc: int) -> List[int]:
        """取某个文档里某个词项的全部位置（差分解码）。"""

        rng = self._posting_range(term)
        if rng is None or not self.has_positions:
            return []
        lo, hi = rng
        docs = self.postings_doc[lo:hi]
        idx = int(np.searchsorted(docs, local_doc))
        if idx >= len(docs) or int(docs[idx]) != local_doc:
            return []
        start = int(self.pos_offsets[lo + idx])
        end = int(self.pos_offsets[lo + idx + 1])
        deltas = varint_decode(self.pos_data, start, end)
        out: List[int] = []
        acc = 0
        for d in deltas:
            acc += d
            out.append(acc)
        return out

    # -- 打分 -------------------------------------------------------------
    def score_term(self, term: str) -> Tuple[np.ndarray, np.ndarray]:
        """单词项的 BM25 贡献：``(局部文档号, 分数)``。

        这就是公式里那个求和里的**单一项**，向量化实现在下面四行。
        """

        rng = self._posting_range(term)
        if rng is None or self.num_docs == 0 or self.avg_doc_length <= 0:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
        lo, hi = rng
        docs = self.postings_doc[lo:hi].astype(np.int64)
        tf = self.postings_tf[lo:hi].astype(np.float64)
        df = int(self.doc_freqs[self.term_index[term]])
        dl = self.doc_lengths[docs].astype(np.float64)
        norm = 1.0 - self.b + self.b * dl / self.avg_doc_length
        denom = tf + self.k1 * norm
        scores = self.idf(df) * (tf * (self.k1 + 1.0)) / denom
        return docs, scores

    def phrase_matches(self, query: PhraseQuery) -> np.ndarray:
        """短语匹配，返回命中的**局部文档号**（升序）。

        算法是"逐词项收窄位置集合"：

        1. 候选文档 = 所有词项 postings 的交集（任何一个词项没有 posting
           就直接没有候选 —— 这就是 Lucene 里"短语含未知词必然不命中"）；
        2. 对每个候选文档，取第一个词项的位置集合 ``S``；
           ``S`` 里保留那些"加上偏移差之后仍然命中下一个词项"的位置；
        3. 走完所有词项，``S`` 非空即命中。
        """

        terms = query.terms
        if not terms:
            return np.zeros(0, dtype=np.int64)
        # 退化情形："短语"其实只有一个不同的词项（比如 "cat"、或者 "run run"
        # 被偏移压成同一个位置跨度 0）—— 那就等价于一次普通词项查询。
        degenerate = len(set(terms)) == 1 and query.span == 0
        if len(terms) == 1 or degenerate:
            docs, _ = self.postings(terms[0])
            return np.sort(docs.astype(np.int64))
        if not self.has_positions:
            raise QueryError(
                "该 FTS 索引建的时候 with_position=False，无法做短语查询",
                hint="重建索引时设 tokenizer.with_position=True",
            )

        candidate: Optional[Set[int]] = None
        for term in terms:
            docs, _ = self.postings(term)
            as_set = {int(d) for d in docs}
            candidate = as_set if candidate is None else (candidate & as_set)
            if not candidate:
                return np.zeros(0, dtype=np.int64)

        base_off = query.offsets[0]
        out: List[int] = []
        for doc in sorted(candidate):
            alive = set(self.positions_of(terms[0], doc))
            for k in range(1, len(terms)):
                shift = query.offsets[k] - base_off
                pos = set(self.positions_of(terms[k], doc))
                if not alive:
                    break
                alive = {p for p in alive if (p + shift) in pos}
            if alive:
                out.append(doc)
        return np.asarray(out, dtype=np.int64)

    # -- 查询求值 ---------------------------------------------------------
    def _eval(self, node: QueryNode) -> Tuple[np.ndarray, np.ndarray]:
        """求值一个查询节点，返回 ``(掩码, 分数)``，长度都是 ``num_docs``。

        分数是**加性**的：一个文档如果同时命中 must 和 should，分数相加。
        用稠密数组而不是稀疏游标，是为了让代码短到能一眼读懂；
        真实的 WAND 之所以存在，只是因为这个稠密数组在亿级文档上放不下。
        """

        n = self.num_docs
        if isinstance(node, TermQuery):
            mask = np.zeros(n, dtype=bool)
            scores = np.zeros(n, dtype=np.float64)
            docs, s = self.score_term(node.term)
            if len(docs):
                mask[docs] = True
                scores[docs] = s
            return mask, scores
        if isinstance(node, PhraseQuery):
            mask = np.zeros(n, dtype=bool)
            scores = np.zeros(n, dtype=np.float64)
            hits = self.phrase_matches(node)
            if len(hits):
                mask[hits] = True
                # 短语的分数 = 它包含的各个词项分数的和（Lucene 的做法）。
                # 更讲究的做法是给"短语本身"建一个伪词项、用短语频次打分，
                # 那需要额外的存储，这里不做。
                for term in node.terms:
                    docs, s = self.score_term(term)
                    if len(docs):
                        # score_term 返回的是"命中行"的稀疏分数，先摊回稠密数组
                        contrib = np.zeros(n, dtype=np.float64)
                        contrib[docs] = s
                        scores += np.where(mask, contrib, 0.0)
            return mask, scores
        if isinstance(node, BooleanQuery):
            return self._eval_boolean(node)
        raise QueryError(f"不能求值的查询节点: {node!r}")

    def _eval_boolean(self, query: BooleanQuery) -> Tuple[np.ndarray, np.ndarray]:
        n = self.num_docs
        if query.is_empty:
            # 空查询：什么都不命中（Lucene 也是这个语义）
            return np.zeros(n, dtype=bool), np.zeros(n, dtype=np.float64)

        must_mask = np.ones(n, dtype=bool)
        must_scores = np.zeros(n, dtype=np.float64)
        for child in query.must:
            m, s = self._eval(child)
            must_mask &= m
            must_scores += np.where(m, s, 0.0)

        should_mask = np.zeros(n, dtype=bool)
        should_scores = np.zeros(n, dtype=np.float64)
        for child in query.should:
            m, s = self._eval(child)
            should_mask |= m
            should_scores += np.where(m, s, 0.0)

        not_mask = np.zeros(n, dtype=bool)
        for child in query.must_not:
            m, _ = self._eval(child)
            not_mask |= m

        if query.must:
            base = must_mask
            scores = must_scores + np.where(must_mask, should_scores, 0.0)
        elif query.should:
            base = should_mask
            scores = should_scores
        else:
            # 纯否定查询：全集减去必须不命中的（见 BooleanQuery docstring）
            base = np.ones(n, dtype=bool)
            scores = np.zeros(n, dtype=np.float64)

        return base & ~not_mask, np.where(base & ~not_mask, scores, 0.0)

    # -- 对外查询 ---------------------------------------------------------
    def to_boolean_query(self, query: Any) -> BooleanQuery:
        """把 ``str`` / ``MatchQuery`` / ``BooleanQuery`` 统一成布尔树。"""

        if isinstance(query, BooleanQuery):
            return query
        if isinstance(query, MatchQuery):
            text = query.query
        elif isinstance(query, str):
            text = query
        else:
            raise QueryError(f"FTS 不认识的查询类型: {type(query).__name__}")
        parser = _QueryParser(_lex_query(text), self.tokenizer)
        return parser.parse()

    def search_ranked(
        self,
        query: Any,
        k: Optional[int] = None,
        *,
        mask: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """返回 ``(行号, _score)``，按 **_score 降序**（越大越相关）。

        ``mask`` 是长度 ``num_docs`` 的布尔数组（True = 这一行的局部下标还活着），
        用来把删除行、prefilter 的结果塞进来 —— 和 ``VectorIndex.search`` 的
        ``mask`` 语义一致。

        并列分数的名次用行号升序决定（``np.lexsort``），保证**结果可复现**：
        否则同样的查询在不同机器上顺序不同，测试会间歇性失败。
        """

        query = query if isinstance(query, BooleanQuery) else self.to_boolean_query(query)
        hits, scores = self._eval(query)
        if mask is not None:
            m = np.asarray(mask, dtype=bool)
            if m.shape[0] != self.num_docs:
                raise QueryError("mask 长度不符", expected=self.num_docs, got=int(m.shape[0]))
            hits = hits & m
        local = np.flatnonzero(hits)
        self.last_search_stats = {
            "num_docs": self.num_docs,
            "num_hits": int(len(local)),
            "num_terms": self.num_terms,
            "query_terms": query.all_terms(),
            "max_score": float(scores[local].max()) if len(local) else 0.0,
        }
        if len(local) == 0:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
        s = scores[local]
        order = np.lexsort((self.row_ids[local], -s))
        local = local[order]
        s = s[order]
        if k is not None:
            local = local[: int(k)]
            s = s[: int(k)]
        return np.asarray(self.row_ids[local], dtype=np.int64), np.asarray(s, dtype=np.float64)

    def search(self, query: ScalarIndexExpr) -> Optional[ScalarIndexExpr]:
        """``ScalarIndex`` 接口：把查询翻译成行号集合。

        注意这里**不带排序**：返回的 ``ExactMatch`` 只表达"哪些行满足"。
        排序是 ``search_ranked`` 的事 —— 布尔组合（``AND`` 一个范围查询）
        时排序没有意义，只有最终输出才需要。
        """

        if isinstance(query, (MatchQuery, str, BooleanQuery)):
            hits, _ = self._eval(self.to_boolean_query(query))
            return ExactMatch(np.sort(self.row_ids[hits].astype(np.int64)))
        if isinstance(query, (And, Or, Not)):
            materialized = materialize(query, self.search)
            if materialized is None:
                return None
            return combine_rows(self, materialized)
        return None

    # -- 持久化 -----------------------------------------------------------
    def save(self, store: IndexStore) -> List[str]:
        """把索引写成一堆文件。

        文件划分刻意和真实 Lance 的 FTS 目录对齐（``metadata`` / ``docs`` /
        ``invert`` / ``tokens``）：**元数据和倒排表分开**，这样"打开索引先看体积
        和配置"不用读整份 postings。
        """

        files: List[str] = []
        store.write_json(
            "metadata.json",
            {
                "num_docs": self.num_docs,
                "num_terms": self.num_terms,
                "avg_doc_length": self.avg_doc_length,
                "k1": self.k1,
                "b": self.b,
                "has_positions": self.has_positions,
                "tokenizer": self.tokenizer.config.to_dict(),
                "version": 1,
            },
        )
        files.append("metadata.json")
        store.write_json("terms.json", self.term_list)
        files.append("terms.json")
        store.write_numpy("doc_freqs.npy", self.doc_freqs)
        store.write_numpy("doc_lengths.npy", self.doc_lengths)
        store.write_numpy("row_ids.npy", np.asarray(self.row_ids, dtype=np.int64))
        store.write_numpy("doc_offsets.npy", self.doc_offsets)
        store.write_numpy("postings_doc.npy", self.postings_doc)
        store.write_numpy("postings_tf.npy", self.postings_tf)
        store.write_numpy("pos_offsets.npy", self.pos_offsets)
        files += [
            "doc_freqs.npy",
            "doc_lengths.npy",
            "row_ids.npy",
            "doc_offsets.npy",
            "postings_doc.npy",
            "postings_tf.npy",
            "pos_offsets.npy",
        ]
        store.write("positions.bin", self.pos_data)
        files.append("positions.bin")
        metadata = self.metadata
        metadata.files = list(files)
        metadata.size_bytes = int(self.size_bytes)
        return files

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "InvertedIndex":
        raw = store.read_json("metadata.json")
        config = TokenizerConfig.from_dict(raw.get("tokenizer") or {})
        index = cls(
            metadata,
            tokenizer=Tokenizer(config),
            k1=float(raw.get("k1", 1.2)),
            b=float(raw.get("b", 0.75)),
        )
        index.term_list = list(store.read_json("terms.json"))
        index.term_index = {t: i for i, t in enumerate(index.term_list)}
        index.doc_freqs = store.read_numpy("doc_freqs.npy")
        index.doc_lengths = store.read_numpy("doc_lengths.npy")
        index.row_ids = store.read_numpy("row_ids.npy").astype(np.int64)
        index.doc_offsets = store.read_numpy("doc_offsets.npy")
        index.postings_doc = store.read_numpy("postings_doc.npy")
        index.postings_tf = store.read_numpy("postings_tf.npy")
        index.pos_offsets = store.read_numpy("pos_offsets.npy")
        index.pos_data = store.read("positions.bin")
        index.has_positions = bool(raw.get("has_positions", True))
        index.avg_doc_length = float(raw.get("avg_doc_length", 0.0))
        return index

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """``index_stats()`` 用的信息。

        ``size_bytes`` 是**估算**：真实 Lance 会把 postings 再压一遍
        （bitpacking + 块压缩），所以这里的数字是上界而不是实际落盘大小。
        """

        term_bytes = sum(len(t.encode("utf-8")) for t in self.term_list)
        size = (
            term_bytes
            + self.doc_freqs.nbytes
            + self.doc_lengths.nbytes
            + self.doc_offsets.nbytes
            + self.postings_doc.nbytes
            + self.postings_tf.nbytes
            + self.pos_offsets.nbytes
            + len(self.pos_data)
            + self.row_ids.nbytes
        )
        return {
            "index_type": self.type_name,
            "num_docs": self.num_docs,
            "num_unique_terms": self.num_terms,
            "num_postings": self.num_postings,
            "avg_doc_length": self.avg_doc_length,
            "total_doc_length": int(self.doc_lengths.sum()) if self.num_docs else 0,
            "has_positions": self.has_positions,
            "k1": self.k1,
            "b": self.b,
            "tokenizer": self.tokenizer.config.describe(),
            "size_bytes": int(size),
            #: 每个词项平均出现在多少文档里 —— 越接近 1 说明语料越分散
            "avg_doc_frequency": (
                float(self.doc_freqs.mean()) if self.num_terms else 0.0
            ),
            "max_doc_frequency": int(self.doc_freqs.max()) if self.num_terms else 0,
        }

    def explain(self, query: Any, row_id: int, k: int = 10) -> Dict[str, Any]:
        """给出某个文档对某个查询的分数构成（逐词项拆开）。

        这是排查"为什么这篇文档排前面"的唯一靠谱工具 —— BM25 的分数是加性的，
        拆开看每一项的 IDF/tf/长度归一化因子，异常一眼可见。
        """

        boolean = self.to_boolean_query(query)
        local_ids = np.flatnonzero(np.asarray(self.row_ids) == int(row_id))
        if len(local_ids) == 0:
            raise QueryError(f"行号 {row_id} 不在该索引覆盖范围内")
        local = int(local_ids[0])
        dl = int(self.doc_lengths[local])
        parts: List[Dict[str, Any]] = []
        total = 0.0
        for term in dict.fromkeys(boolean.all_terms()):
            rng = self._posting_range(term)
            if rng is None:
                parts.append({"term": term, "in_doc": False, "score": 0.0})
                continue
            lo, hi = rng
            docs = self.postings_doc[lo:hi]
            idx = int(np.searchsorted(docs, local))
            if idx >= len(docs) or int(docs[idx]) != local:
                parts.append(
                    {
                        "term": term,
                        "in_doc": False,
                        "df": int(self.doc_freqs[self.term_index[term]]),
                        "score": 0.0,
                    }
                )
                continue
            tf = int(self.postings_tf[lo + idx])
            df = int(self.doc_freqs[self.term_index[term]])
            idf = self.idf(df)
            norm = 1.0 - self.b + self.b * (dl / self.avg_doc_length if self.avg_doc_length else 1.0)
            score = idf * (tf * (self.k1 + 1.0)) / (tf + self.k1 * norm)
            total += score
            parts.append(
                {
                    "term": term,
                    "in_doc": True,
                    "tf": tf,
                    "df": df,
                    "idf": idf,
                    "norm": norm,
                    "score": score,
                }
            )
        return {
            "row_id": int(row_id),
            "doc_length": dl,
            "avg_doc_length": self.avg_doc_length,
            "total_score": total,
            "parts": parts,
            "note": "分数是各词项贡献之和；命中集合还会经过 must/should/must_not 过滤",
        }

    def analyze(self, text: str) -> Any:
        """把这个索引的分词器跑一遍（调试用）。"""

        return self.tokenizer.analyze(text)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"InvertedIndex(docs={self.num_docs}, terms={self.num_terms}, "
            f"postings={self.num_postings}, {self.tokenizer.config.describe()})"
        )
