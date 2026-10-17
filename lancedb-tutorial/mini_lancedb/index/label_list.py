"""LABEL_LIST 索引：为"列表列"（比如 tags）建的位图索引。

## 它和 BITMAP 到底差在哪

BITMAP 假设 **一行 ↦ 一个值**：``status`` 列里每行只有一个值，
所以所有位图是一份**行集合的划分** —— 每一行恰好出现在一张位图里，
把所有位图 OR 起来就是全集。

LABEL_LIST 面对的是 **一行 ↦ 一串值**：``tags = ["python", "db", "sql"]``。
于是：

* 一行会出现在**多张**位图里，位图不再是划分，OR 起来会重复计数；
* 反过来，``SUM(位图的 popcount)`` 得到的是"标签实例数"而不是行数，
  两者之间的比值就是"平均每行几个标签"；
* 没有标签的行（``[]`` 或 null）**不在任何位图里** —— 这一点和 BITMAP 的
  null 行一样，但原因不同：BITMAP 是"这一行的值未知"，这里是"这一行没有值"。

结论：**位图运算是完全一样的**（还是按 64 位字做 AND/OR/NOT），
差别只在构建方式（一行要写进多个位图）和查询语义
（``array_contains`` 是"集合成员"而不是"标量相等"）。
所以本类直接复用 :class:`~mini_lancedb.index.bitmap.BitmapIndex` 的位图机制，
只重写"构建"和"谓词翻译"两部分 —— 这不是偷懒，是"两者的数据结构和算法
本来就该是同一套"的诚实表达。

## 查询语义

* ``array_contains(col, 'x')`` → :class:`ArrayContains`，取 ``x`` 的位图；
* ``col IN ('a', 'b')`` → 这里定义为"**含 a 或含 b**"（任意命中）。
  SQL 里 ``IN`` 对列表列没有标准定义（各家方言不同），所以我们挑一个
  最容易解释的：把列表列的 ``IN`` 读成"和给定标签集合有交集"。
  想表达"同时含 a 和 b"请用 ``AND([ArrayContains('a'), ArrayContains('b')])``。

## 什么时候不该用

标签种类的数量就是位图张数。``tags`` 这种列标签种类通常几百到几万，
按 10000 个标签 × 1 亿行算，就是 10000 × 12.5MB = 125GB —— 直接爆掉。
真实 LanceDB 因此对 LABEL_LIST 有基数上限检查，超了就不建（或者建议改用
倒排/前缀结构）。这个约束在 :meth:`stats` 的 ``bits_per_row`` 里能直接看出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from ..arrays import Array, ListArray
from ..errors import IndexBuildError
from ._expr_util import row_ids_of, sort_key
from .base import (
    And,
    ExactMatch,
    IndexMetadata,
    IndexStore,
    IsIn,
    Not,
    Or,
    RangeQuery,
    ScalarIndexExpr,
)
from .bitmap import WORDSIZE, BitmapIndex, _hashable

__all__ = ["LabelListIndex", "ArrayContains"]


@dataclass
class ArrayContains(ScalarIndexExpr):
    """``array_contains(col, label)`` —— "这一行是个包含 ``label`` 的列表"。

    真实 Lance 里对应 ``ArrayContainsQuery``。它和 ``IsIn`` 的区别是
    **返回值**：``IsIn`` 在标量列上返回一个标量，``array_contains``
    返回一个布尔 —— 因为列表列没有"等于某个值"这种说法，
    只有"含不含"。
    """

    label: Any


class LabelListIndex(BitmapIndex):
    """``LABEL_LIST`` 索引。"""

    type_name = "LABEL_LIST"

    def __init__(self, metadata: IndexMetadata) -> None:
        super().__init__(metadata)
        #: 没有标签的行（``[]`` / null）—— 不在任何位图里
        self.empty_rows: int = 0
        #: 每行有几个**去重后**的标签，下标是"被索引行的局部下标"
        self.labels_per_row: np.ndarray = np.zeros(0, dtype=np.int64)

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls, values: Array, row_ids: np.ndarray, *, metadata: IndexMetadata
    ) -> "LabelListIndex":
        """``values`` 必须是 ``list<...>`` 列。

        传一个标量列进来是**用错索引**，不是"数据不好" —— 所以直接报错并
        给出该用什么索引的建议，而不是静默按字符切分（那种"善意猜测"
        会在半年后变成没人能解释的错结果）。
        """

        if not isinstance(values, (ListArray,)):
            raise IndexBuildError(
                "LABEL_LIST 需要一个 list<...> 类型的列",
                got=getattr(values.type, "name", type(values).__name__),
                hint="标量列请用 BITMAP / BTREE；字符串列做子串匹配请用 NGRAM",
            )
        index = cls(metadata)
        rows = row_ids_of(row_ids, len(values))
        vals = values.to_pylist()

        covered: List[int] = []
        groups: Dict[Any, List[int]] = {}
        per_row: List[int] = []
        for i, value in enumerate(vals):
            if value is None:
                index.null_rows += 1
                continue  # null 行不进任何位图，也不占 labels_per_row 的位置
            local = len(covered)
            covered.append(int(rows[i]))
            labels = list(value)
            # 同一行里重复的标签只记一次：位图表达不了"出现两次"，
            # 这正是位图索引答不了 "count(tags) > 2" 的原因。
            seen = set()
            count = 0
            for label in labels:
                key = _hashable(label)
                if key in seen:
                    continue
                seen.add(key)
                groups.setdefault(key, []).append(local)
                count += 1
            per_row.append(count)
            if count == 0:
                index.empty_rows += 1

        index.row_ids = np.asarray(covered, dtype=np.int64)
        n = len(covered)
        index.nwords = (n + WORDSIZE - 1) // WORDSIZE if n else 0
        index.labels_per_row = np.asarray(per_row, dtype=np.int64)

        index.value_list = sorted(groups, key=sort_key)
        index.value_index = {v: i for i, v in enumerate(index.value_list)}
        index.bitmaps = np.zeros((len(index.value_list), index.nwords), dtype=np.uint64)
        index.value_counts = [0] * len(index.value_list)
        for vi, label in enumerate(index.value_list):
            locals_ = np.asarray(groups[label], dtype=np.int64)
            index.value_counts[vi] = int(len(locals_))
            if index.nwords == 0:
                continue
            words = (locals_ >> 6).astype(np.int64)
            bits = np.left_shift(
                np.uint64(1), (locals_ & np.int64(WORDSIZE - 1)).astype(np.uint64)
            )
            np.bitwise_or.at(index.bitmaps[vi], words, bits)

        metadata.num_indexed_rows = int(n)
        metadata.details["num_labels"] = len(index.value_list)
        metadata.size_bytes = int(index.size_bytes)
        return index

    # -- 谓词翻译 ---------------------------------------------------------
    def _eval_words(self, expr: ScalarIndexExpr) -> Optional[np.ndarray]:
        zero = np.zeros(self.nwords, dtype=np.uint64)
        if isinstance(expr, ArrayContains):
            bm = self.bitmap_of(expr.label)
            return zero if bm is None else bm
        if isinstance(expr, IsIn):
            # 列表列的 IN：和给定标签集合有交集（"含其中任意一个"）
            parts = []
            for label in expr.values:
                bm = self.bitmap_of(label)
                parts.append(zero if bm is None else bm)
            if not parts:
                return zero
            return self.union(parts)
        return super()._eval_words(expr)

    def search(self, query: ScalarIndexExpr) -> Optional[ScalarIndexExpr]:
        if isinstance(query, RangeQuery):
            return None
        if isinstance(query, (ArrayContains, IsIn, ExactMatch, And, Or, Not)):
            words = self._eval_words(query)
            if words is None:
                return None
            return ExactMatch(np.sort(self.words_to_rows(words)))
        return None

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        n = len(self.row_ids)
        labels = int(self.labels_per_row.sum()) if len(self.labels_per_row) else 0
        bitmap_bytes = self.bitmaps.nbytes
        return {
            "index_type": self.type_name,
            "num_rows": n,
            "num_labels": len(self.value_list),
            "null_rows": self.null_rows,
            "empty_rows": self.empty_rows,
            "num_label_assignments": labels,
            "avg_labels_per_row": (labels / n) if n else 0.0,
            "max_labels_per_row": int(self.labels_per_row.max()) if n else 0,
            "total_bitmap_bytes": int(bitmap_bytes),
            #: 每行摊到多少比特。标签种类一多就会爆 —— 这是 LABEL_LIST 的死穴
            "bits_per_row": (bitmap_bytes * 8 / n) if n else 0.0,
            "avg_rows_per_label": (labels / len(self.value_list))
            if self.value_list
            else 0.0,
            "size_bytes": int(bitmap_bytes),
        }

    # -- 持久化 -----------------------------------------------------------
    def save(self, store: IndexStore) -> List[str]:
        files = super().save(store)
        store.write_json(
            "label_meta.json",
            {"empty_rows": self.empty_rows, "null_rows": self.null_rows},
        )
        store.write_numpy("labels_per_row.npy", self.labels_per_row)
        files += ["label_meta.json", "labels_per_row.npy"]
        self.metadata.files = list(files)
        return files

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "LabelListIndex":
        raw = store.read_json("metadata.json")
        index = cls(metadata)
        index.row_ids = store.read_numpy("index_rows.npy").astype(np.int64)
        index.value_list = list(store.read_json("values.json"))
        index.value_index = {v: i for i, v in enumerate(index.value_list)}
        index.value_counts = [int(x) for x in store.read_numpy("value_counts.npy").tolist()]
        index.bitmaps = store.read_numpy("bitmaps.npy")
        index.nwords = int(raw.get("nwords", index.bitmaps.shape[1] if index.bitmaps.size else 0))
        index.null_rows = int(raw.get("null_rows", 0))
        if store.exists("labels_per_row.npy"):
            index.labels_per_row = store.read_numpy("labels_per_row.npy")
        if store.exists("label_meta.json"):
            meta = store.read_json("label_meta.json")
            index.empty_rows = int(meta.get("empty_rows", 0))
        return index

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"LabelListIndex(rows={len(self.row_ids)}, labels={len(self.value_list)}, "
            f"words={self.nwords})"
        )
