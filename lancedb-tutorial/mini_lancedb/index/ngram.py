"""NGRAM 索引：加速 ``LIKE '%abc%'`` / ``contains(col, 'abc')``。

## 为什么需要它

``LIKE '%abc%'`` 用不上 BTREE（保序结构只能答前缀），也用不上倒排
（倒排按**词**切，而子串可能横跨词边界，``"data base"`` 里搜 ``"abase"``
是搜不到的）。于是这类查询在数据库里天然只能全表扫 —— 直到你给列建
**字符级 n 元组索引**：

```
"database"  的 3-gram = {dat, ata, tab, aba, bas, ase}
"abacus"    的 3-gram = {aba, bac, acu, cus}
```

查 ``'%aba%'`` 时，先算 ``aba`` 的 3-gram = ``{aba}``，取它的 postings，
只对这批候选做**真正的子串判断**。

## 关键性质：n 元组索引给出的是**候选集**，不是答案

``'%aba%'`` 的 3-gram 只有一个 ``aba``。命中 ``aba`` 这个 gram 的行，
只是"含有 aba 这三个连续字符"，它当然包含子串 ``"aba"`` —— 看起来精确，
但如果查询是 ``'%abacus%'``（5 个字符）：3-gram 是 ``{aba, bac, acu, cus}``，
一行只要有 ``aba`` 和 ``cus``（比如 ``"abacus... cus"``？）就同时命中两个 gram，
却并不含 ``abacus``。**gram 命中只证明了"碎片都在"，不证明"顺序和距离对"**。

所以流程永远是两步：

```
候选 = ⋂(每个 gram 的 postings)     ← 用索引，便宜
结果 = [d for d in 候选 if needle in value[d]]   ← 必须精确验证，贵但候选很少
```

这就是搜索引擎里 **"index → candidate → verify"** 的标准模式
（Lance 的 NGRAM、Lucene 的 ``NGramTokenFilter`` + 后置验证、
PostgreSQL 的 ``pg_trgm`` 都是这个套路）。**假阳性不可避免**，
唯一能消除它的是"把整个字符串也放进索引"（那就是把索引变成数据表了）。

## 参数选择

``n=3`` 是通用默认：n 太小（2）→ 每个 gram 的 postings 太长、候选太多，
验证反而成了瓶颈；n 太大（5）→ gram 种类爆炸、索引变大，
而且短查询（少于 n 个字符）**根本用不了索引**（我们会直接返回 ``None``
让上游走全扫 —— 这是诚实的退化，不是 bug）。

## 本实现与真实 Lance 的差别

真实 Lance 的 NGRAM 索引里**只有 gram → 行号**，验证时回表取值。
我们把原值也留了一份（``self.values``），因为教学实现没有"表"可回 ——
代价是索引体积翻倍，这一点在 :meth:`stats` 的 ``stored_value_bytes``
里明明白白地列出来，不做掩饰。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set

import numpy as np

from ..arrays import Array
from ..errors import IndexBuildError
from ._expr_util import combine_rows, materialize, row_ids_of, values_of
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

__all__ = ["NGramIndex", "Contains", "Like", "DEFAULT_NGRAM_SIZE"]


#: 默认的 gram 长度。3 是几乎所有实现（pg_trgm、Lucene、Lance）的默认值。
DEFAULT_NGRAM_SIZE = 3


@dataclass
class Contains(ScalarIndexExpr):
    """``contains(col, 'abc')`` —— 子串包含。"""

    value: str
    #: 大小写不敏感。**注意：本索引无法加速它**（gram 是按原始大小写建的），
    #: 见 :meth:`NGramIndex.search` 的说明。
    case_insensitive: bool = False


@dataclass
class Like(ScalarIndexExpr):
    """``col LIKE '%abc%'``。

    只支持 **"没有其它通配符的包含式"** 与 **"完全无通配符的等值"**
    两种形态；``'abc%'``（前缀）、``'%abc'``（后缀）、``'a%c'`` 一律返回
    ``None`` 让上游全扫。理由：前缀/后缀确实能用 gram 取候选，
    但"用前 n 个字符的 gram"取到的候选集**几乎等于全集**，
    加速比可以忽略，不如把"我不会"说清楚。
    """

    pattern: str
    case_insensitive: bool = False


class NGramIndex(ScalarIndex):
    """``NGRAM`` 标量索引。"""

    type_name = "NGRAM"

    def __init__(self, metadata: IndexMetadata) -> None:
        super().__init__(metadata)
        params: Mapping[str, Any] = metadata.params or {}
        self.n: int = int(params.get("ngram_size", DEFAULT_NGRAM_SIZE))
        if self.n < 1:
            raise IndexBuildError("ngram_size 必须为正", got=self.n)
        #: 排序后的 gram 表
        self.gram_list: List[str] = []
        self.gram_index: Dict[str, int] = {}
        #: CSR：``gram_offsets[i] .. gram_offsets[i+1]`` 是第 i 个 gram 的 postings
        self.gram_offsets: np.ndarray = np.zeros(1, dtype=np.int64)
        self.gram_rows: np.ndarray = np.zeros(0, dtype=np.int64)
        #: 行下标（局部）→ 原值。**只在验证阶段用**，真实实现是回表取
        self.values: List[str] = []
        self.null_rows: int = 0
        #: 每行的 gram 个数（统计用）
        self.grams_per_row: np.ndarray = np.zeros(0, dtype=np.int64)
        #: 上一次查询的候选/验证统计 —— 专门用来证明"假阳性真的存在"
        self.last_search_stats: Dict[str, Any] = {}

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls, values: Array, row_ids: np.ndarray, *, metadata: IndexMetadata
    ) -> "NGramIndex":
        index = cls(metadata)
        rows = row_ids_of(row_ids, len(values))
        vals = values_of(values)
        n = index.n

        covered: List[int] = []
        groups: Dict[str, List[int]] = {}
        per_row: List[int] = []
        texts: List[str] = []
        for i, value in enumerate(vals):
            if value is None:
                index.null_rows += 1
                continue
            text = value if isinstance(value, str) else str(value)
            local = len(covered)
            covered.append(int(rows[i]))
            texts.append(text)
            grams = ngrams_of(text, n)
            per_row.append(len(grams))
            for gram in grams:
                groups.setdefault(gram, []).append(local)

        index.row_ids = np.asarray(covered, dtype=np.int64)
        index.values = texts
        index.grams_per_row = np.asarray(per_row, dtype=np.int64)
        index.gram_list = sorted(groups)
        index.gram_index = {g: i for i, g in enumerate(index.gram_list)}

        offsets = np.zeros(len(index.gram_list) + 1, dtype=np.int64)
        flat: List[int] = []
        for gi, gram in enumerate(index.gram_list):
            members = sorted(groups[gram])
            flat.extend(members)
            offsets[gi + 1] = len(flat)
        index.gram_offsets = offsets
        index.gram_rows = np.asarray(flat, dtype=np.int64)

        metadata.num_indexed_rows = int(len(covered))
        metadata.details["num_grams"] = len(index.gram_list)
        metadata.details["ngram_size"] = n
        metadata.size_bytes = int(index.size_bytes)
        return index

    # -- postings ---------------------------------------------------------
    def postings(self, gram: str) -> np.ndarray:
        gi = self.gram_index.get(gram)
        if gi is None:
            return np.zeros(0, dtype=np.int64)
        lo, hi = int(self.gram_offsets[gi]), int(self.gram_offsets[gi + 1])
        return self.gram_rows[lo:hi]

    def candidates(self, needle: str) -> Optional[Set[int]]:
        """取候选行号（局部下标）—— **验证之前**的集合。

        单独暴露出来是为了教学：测试里可以断言"候选数 > 真实命中数"，
        亲手证明假阳性存在、验证步骤不可省。
        """

        if len(needle) < self.n:
            return None
        candidate: Optional[Set[int]] = None
        for gram in ngrams_of(needle, self.n):
            rows = {int(r) for r in self.postings(gram).tolist()}
            candidate = rows if candidate is None else (candidate & rows)
            if not candidate:
                return set()
        return candidate if candidate is not None else set()

    # -- 谓词翻译 ---------------------------------------------------------
    def _contains_rows(self, needle: str) -> np.ndarray:
        """完整的"候选 → 验证"两步，返回命中的**真实行号**（未排序）。"""

        candidate = self.candidates(needle)
        if candidate is None:
            raise _CannotAnswer(needle)
        verified: List[int] = []
        for local in sorted(candidate):
            if needle in self.values[local]:
                verified.append(int(self.row_ids[local]))
        self.last_search_stats = {
            "needle": needle,
            "raw_candidates": len(candidate),
            "matches": len(verified),
            #: 假阳性 = 过了 gram 过滤但通不过精确验证的行数
            "false_positives": len(candidate) - len(verified),
        }
        return np.asarray(verified, dtype=np.int64)

    def _like_rows(self, pattern: str) -> Optional[np.ndarray]:
        """把 LIKE 模式翻译成"包含"或"等值"；翻译不了返回 ``None``。

        支持且仅支持两种形态：

        * ``'%abc%'``（两端 ``%``，中间再无通配符）→ 子串包含；
        * ``'abc'``（完全没有通配符）→ 等值。

        注意 ``_`` 也是通配符（匹配任意单字符），所以 ``'%a_c%'``
        里的 ``a_c`` **不能**当作字面量去搜，否则会静默漏掉 ``"abc"``。
        这类模式一律返回 ``None``。
        """

        if "%" not in pattern:
            if "_" in pattern:
                return None
            return self._exact_rows(pattern)
        if pattern.startswith("%") and pattern.endswith("%"):
            inner = pattern[1:-1]
            if "%" in inner or "_" in inner:
                return None
            return self._contains_rows(inner)
        return None

    def _exact_rows(self, value: str) -> np.ndarray:
        candidate = self.candidates(value)
        if candidate is None:
            raise _CannotAnswer(value)
        hits = [int(self.row_ids[local]) for local in sorted(candidate) if self.values[local] == value]
        self.last_search_stats = {
            "needle": value,
            "mode": "exact",
            "raw_candidates": len(candidate),
            "matches": len(hits),
            "false_positives": len(candidate) - len(hits),
        }
        return np.asarray(hits, dtype=np.int64)

    def search(self, query: ScalarIndexExpr) -> Optional[ScalarIndexExpr]:
        """翻译谓词。答不了（短查询、无法加速的模式、大小写不敏感）返回 ``None``。"""

        try:
            if isinstance(query, Contains):
                if query.case_insensitive:
                    # gram 是按原始大小写建的，大小写不敏感的子串查询无法用索引。
                    # 真实实现要么再建一份 lower(列) 的索引，要么就全扫。
                    return None
                return ExactMatch(np.sort(self._contains_rows(query.value)))
            if isinstance(query, Like):
                if query.case_insensitive:
                    return None
                rows = self._like_rows(query.pattern)
                return None if rows is None else ExactMatch(np.sort(rows))
            if isinstance(query, (And, Or, Not)):
                materialized = materialize(query, self.search)
                if materialized is None:
                    return None
                return combine_rows(self, materialized)
            if isinstance(query, ExactMatch):
                return query
        except _CannotAnswer:
            return None
        # RangeQuery / IsIn / 别的索引的谓词：NGRAM 答不了
        return None

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        num_grams = len(self.gram_list)
        num_postings = int(len(self.gram_rows))
        value_bytes = sum(len(v.encode("utf-8")) for v in self.values)
        gram_bytes = sum(len(g.encode("utf-8")) for g in self.gram_list)
        size = (
            gram_bytes
            + self.gram_offsets.nbytes
            + self.gram_rows.nbytes
            + value_bytes
            + self.row_ids.nbytes
        )
        return {
            "index_type": self.type_name,
            "num_rows": int(len(self.row_ids)),
            "ngram_size": self.n,
            "num_grams": num_grams,
            "num_postings": num_postings,
            "null_rows": self.null_rows,
            "avg_grams_per_row": (
                float(self.grams_per_row.mean()) if len(self.grams_per_row) else 0.0
            ),
            "avg_rows_per_gram": (num_postings / num_grams) if num_grams else 0.0,
            #: 最热门的 gram 命中多少行 —— 接近行数说明这个 gram 没有区分度
            "max_rows_per_gram": (
                int(np.diff(self.gram_offsets).max()) if num_grams else 0
            ),
            "stored_value_bytes": int(value_bytes),
            "size_bytes": int(size),
        }

    # -- 持久化 -----------------------------------------------------------
    def save(self, store: IndexStore) -> List[str]:
        store.write_json(
            "metadata.json",
            {
                "num_rows": int(len(self.row_ids)),
                "ngram_size": self.n,
                "num_grams": len(self.gram_list),
                "null_rows": self.null_rows,
                "version": 1,
            },
        )
        store.write_json("grams.json", self.gram_list)
        store.write_json("values.json", self.values)
        store.write_numpy("gram_offsets.npy", self.gram_offsets)
        store.write_numpy("gram_rows.npy", self.gram_rows)
        store.write_numpy("grams_per_row.npy", self.grams_per_row)
        store.write_numpy("index_rows.npy", np.asarray(self.row_ids, dtype=np.int64))
        files = [
            "metadata.json",
            "grams.json",
            "values.json",
            "gram_offsets.npy",
            "gram_rows.npy",
            "grams_per_row.npy",
            "index_rows.npy",
        ]
        self.metadata.files = list(files)
        return files

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "NGramIndex":
        raw = store.read_json("metadata.json")
        metadata.params.setdefault("ngram_size", int(raw.get("ngram_size", DEFAULT_NGRAM_SIZE)))
        index = cls(metadata)
        index.gram_list = list(store.read_json("grams.json"))
        index.gram_index = {g: i for i, g in enumerate(index.gram_list)}
        index.values = list(store.read_json("values.json"))
        index.gram_offsets = store.read_numpy("gram_offsets.npy")
        index.gram_rows = store.read_numpy("gram_rows.npy")
        index.grams_per_row = store.read_numpy("grams_per_row.npy")
        index.row_ids = store.read_numpy("index_rows.npy").astype(np.int64)
        index.null_rows = int(raw.get("null_rows", 0))
        return index

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"NGramIndex(rows={len(self.row_ids)}, grams={len(self.gram_list)}, n={self.n})"
        )


class _CannotAnswer(Exception):
    """内部信号：查询太短，gram 索引用不上。"""


def ngrams_of(text: str, n: int) -> List[str]:
    """字符串的全部 n 元组（**去重**，保持首次出现顺序）。

    去重很重要：``"aaaa"`` 的 3-gram 只有 ``{aaa}``，如果不去重，
    一个重复字符很多的字符串会往 postings 里塞成千上万个重复项，
    索引瞬间膨胀到没意义。位图/倒排的 postings 里"一行出现一次"就够了。
    """

    if n <= 0 or len(text) < n:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for i in range(len(text) - n + 1):
        gram = text[i : i + n]
        if gram not in seen:
            seen.add(gram)
            out.append(gram)
    return out
