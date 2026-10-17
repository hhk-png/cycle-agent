"""BITMAP 索引：每个不同的值一张位图。

## 结构

```
"active"  -> 1010 0110 0100 1000 ...   (每个 bit 对应一行)
"pending" -> 0101 0000 0000 0001 ...
"closed"  -> 0000 1001 1011 0011 ...
```

一张位图 = 每个被索引行占 1 bit。于是：

* ``col = 'x'`` → 取那张位图；
* ``col IN (a, b, c)`` → 三张位图 **按字求 OR**；
* ``col = 'a' AND col2 = 'b'`` → 两张位图 **按字求 AND**；
* ``NOT ...`` → **按字取反**再和有效位掩码相与。

关键在于第三步：一次布尔运算处理 **64 行**（一个 ``uint64`` 字），
而按行号集合做交并是"一次一行"。这就是位图索引在低基数列上
能把过滤速度拉起来一个量级的全部原因 —— 不是算法更聪明，是**批处理粒度**更大。

## 什么时候不该用

位图的总位数 = ``不同值个数 × 行数``。于是：

* 低基数列（``status``、``region``、布尔列）：值只有几十个，
  位图总量 ≈ 行数的几十倍**比特** = 行数的几倍字节。**非常划算**，
  而且能白送"值域内任意组合的布尔谓词"。
* 高基数列（``user_id``、UUID）：值个数 ≈ 行数，总位数变成 O(n²/64)，
  比直接存一遍列还大。**灾难**。这种列该用 BTREE。

判据就在 :meth:`stats` 的 ``bits_per_row`` 里：远小于 8 说明很划算，
超过几十就该换索引。真实 LanceDB 的规划器做的正是这个判断。

## "行号集会变成空集"的两个坑

1. **null 行不在任何一张位图里**，所以 ``NOT`` 的补集必须限制在
   "非 null 行"上（否则 null 行会被 ``NOT (col = 'a')`` 捞出来，
   而 SQL 三值逻辑要求它不匹配）。
2. **位图的长度是"被索引行数"，不是 row_id 的最大值。**
   如果拿 row_id 直接当 bit 下标，一个 id=10^9 的行会要一张 125MB 的位图。
   所以这里一律在"局部行号"（0..n-1）上建位图，返回时再经
   ``self.row_ids`` 映射回真实行号。Lance 的位图按 fragment 建，
   本质上也是这个思路（fragment 内的行偏移 + fragment id）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ..arrays import Array
from ..errors import IndexBuildError
from ._expr_util import row_ids_of, sort_key, values_of
from .base import (
    And,
    ExactMatch,
    IndexMetadata,
    IndexStore,
    IsIn,
    Not,
    Or,
    RangeQuery,
    ScalarIndex,
    ScalarIndexExpr,
)

__all__ = ["BitmapIndex", "WORDSIZE"]


#: 一个"字"的位数。用 numpy 的 uint64 就固定是 64。
WORDSIZE = 64


def _hashable(value: Any) -> Any:
    """BITMAP 需要一个可哈希的键；列表/数组先转成元组。"""

    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, np.ndarray):
        return tuple(value.tolist())
    return value


class BitmapIndex(ScalarIndex):
    """``BITMAP`` 标量索引。"""

    type_name = "BITMAP"

    def __init__(self, metadata: IndexMetadata) -> None:
        super().__init__(metadata)
        #: 去重后的值表（按 :func:`sort_key` 排序，保证输出稳定）
        self.value_list: List[Any] = []
        self.value_index: Dict[Any, int] = {}
        #: ``(num_values, nwords)`` 的 uint64 位图矩阵
        self.bitmaps: np.ndarray = np.zeros((0, 0), dtype=np.uint64)
        #: 位图里的字个数
        self.nwords: int = 0
        #: 每个值命中的行数（统计用；也用来判断基数形态）
        self.value_counts: List[int] = []
        self.null_rows: int = 0

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls, values: Array, row_ids: np.ndarray, *, metadata: IndexMetadata
    ) -> "BitmapIndex":
        index = cls(metadata)
        rows = row_ids_of(row_ids, len(values))
        vals = values_of(values)

        covered: List[int] = []
        groups: Dict[Any, List[int]] = {}
        for i, value in enumerate(vals):
            if value is None:
                index.null_rows += 1
                continue
            key = _hashable(value)
            try:
                hash(key)
            except TypeError:
                raise IndexBuildError(
                    "BITMAP 索引只支持标量列（值必须可哈希）",
                    got=type(value).__name__,
                    hint="列表列请用 LABEL_LIST；结构体列请先取子列",
                ) from None
            local = len(covered)
            covered.append(int(rows[i]))
            groups.setdefault(key, []).append(local)

        index.row_ids = np.asarray(covered, dtype=np.int64)
        n = len(covered)
        index.nwords = (n + WORDSIZE - 1) // WORDSIZE if n else 0

        index.value_list = sorted(groups, key=sort_key)
        index.value_index = {v: i for i, v in enumerate(index.value_list)}
        index.bitmaps = np.zeros((len(index.value_list), index.nwords), dtype=np.uint64)
        index.value_counts = [0] * len(index.value_list)

        for vi, value in enumerate(index.value_list):
            locals_ = np.asarray(groups[value], dtype=np.int64)
            index.value_counts[vi] = int(len(locals_))
            if index.nwords == 0:
                continue
            words = (locals_ >> 6).astype(np.int64)
            bits = np.left_shift(
                np.uint64(1), (locals_ & np.int64(WORDSIZE - 1)).astype(np.uint64)
            )
            # 同一个字里可能有多行 → 用 .at 做"就地累加"，普通赋值会互相覆盖
            np.bitwise_or.at(index.bitmaps[vi], words, bits)

        metadata.num_indexed_rows = int(n)
        metadata.details["num_values"] = len(index.value_list)
        metadata.size_bytes = int(index.size_bytes)
        return index

    # -- 位图基本操作 -----------------------------------------------------
    def valid_words(self) -> np.ndarray:
        """有效位掩码：最后一个字里超出行数的那几个 bit 必须清掉。

        不清掉的话 ``NOT`` 会凭空多出一批"行号越界"的命中 —— 这是位图实现里
        最常见的 off-by-64 错误。
        """

        words = np.zeros(self.nwords, dtype=np.uint64)
        if self.nwords == 0:
            return words
        words[:] = np.uint64(0xFFFFFFFFFFFFFFFF)
        rem = len(self.row_ids) % WORDSIZE
        if rem:
            words[-1] = np.uint64((1 << rem) - 1)
        return words

    def bitmap_of(self, value: Any) -> Optional[np.ndarray]:
        """单个值的位图（返回的是**副本**，调用方可以随便改）。"""

        vi = self.value_index.get(_hashable(value))
        if vi is None:
            return None
        return self.bitmaps[vi].copy()

    @staticmethod
    def union(bitmaps: Sequence[np.ndarray]) -> Optional[np.ndarray]:
        """按字 OR。``len(bitmaps) * nwords`` 次字操作 —— 这就是代价。"""

        if not bitmaps:
            return None
        out = bitmaps[0].copy()
        for bm in bitmaps[1:]:
            out |= bm
        return out

    @staticmethod
    def intersect(bitmaps: Sequence[np.ndarray]) -> Optional[np.ndarray]:
        if not bitmaps:
            return None
        out = bitmaps[0].copy()
        for bm in bitmaps[1:]:
            out &= bm
        return out

    def complement(self, bitmap: np.ndarray) -> np.ndarray:
        """补集：取反之后必须和有效位掩码相与（见 :meth:`valid_words`）。"""

        return np.bitwise_and(np.bitwise_not(bitmap), self.valid_words())

    def words_to_rows(self, words: np.ndarray) -> np.ndarray:
        """位图 → 行号（先展开成 bit 下标，再映射回真实行号）。"""

        if len(words) == 0 or not words.any():
            return np.zeros(0, dtype=np.int64)
        # 显式转成小端再 view：'little' bitorder 只有配上小端字节序，
        # 第 k 个 bit 才真的对应第 k 行。不写死字节序会在大小端上有分歧。
        raw = np.ascontiguousarray(words.astype("<u8", copy=False)).view(np.uint8)
        bits = np.unpackbits(raw, bitorder="little")
        local = np.flatnonzero(bits[: len(self.row_ids)])
        return np.asarray(self.row_ids[local], dtype=np.int64)

    # -- 查询 -------------------------------------------------------------
    def _eval_words(self, expr: ScalarIndexExpr) -> Optional[np.ndarray]:
        """把谓词求值成一张位图（含 And/Or/Not 的按字运算）。"""

        if isinstance(expr, IsIn):
            # 值的位图不存在 == 这一列里没有这个值 == 命中的行数为 0。
            # 用"全零位图"参与运算，而不是"跳过"：跳过会让 IN 的结果
            # 意外变成"答不了"，而位图索引其实**知道**答案。
            zero = np.zeros(self.nwords, dtype=np.uint64)
            parts = [self.bitmap_of(v) for v in expr.values]
            parts = [p if p is not None else zero for p in parts]
            if not parts:
                return zero  # ``IN ()``：空集合，谁也不命中
            return self.union(parts)
        if isinstance(expr, ExactMatch):
            words = np.zeros(self.nwords, dtype=np.uint64)
            if self.nwords == 0 or len(expr.row_ids) == 0:
                return words
            wanted = {int(r) for r in np.asarray(expr.row_ids).tolist()}
            local = [i for i, r in enumerate(self.row_ids.tolist()) if r in wanted]
            for i in local:
                words[i >> 6] |= np.uint64(1) << np.uint64(i & (WORDSIZE - 1))
            return words
        if isinstance(expr, And):
            parts = [self._eval_words(c) for c in expr.children]
            if not parts or any(p is None for p in parts):
                return None
            return self.intersect(parts)  # type: ignore[arg-type]
        if isinstance(expr, Or):
            parts = [self._eval_words(c) for c in expr.children]
            if not parts or any(p is None for p in parts):
                return None
            return self.union(parts)  # type: ignore[arg-type]
        if isinstance(expr, Not):
            inner = self._eval_words(expr.child)
            return None if inner is None else self.complement(inner)
        return None

    def search(self, query: ScalarIndexExpr) -> Optional[ScalarIndexExpr]:
        """翻译谓词。

        ``RangeQuery`` **明确返回 ``None``**：位图把顺序打碎了，
        ``col BETWEEN 3 AND 9`` 在位图上只能"把值表里落入区间的值逐个取位图再 OR"，
        值多的时候比全表扫还慢。承认答不了、交给规划器换 BTREE 或退化成扫描，
        比"能跑但很慢"更诚实。
        """

        if isinstance(query, RangeQuery):
            return None
        if isinstance(query, (IsIn, ExactMatch, And, Or, Not)):
            words = self._eval_words(query)
            if words is None:
                return None
            return ExactMatch(np.sort(self.words_to_rows(words)))
        return None

    def search_values(self, values: Sequence[Any]) -> np.ndarray:
        """``col IN (...)`` 的快捷入口（跳过表达式层，直接给行号）。"""

        zero = np.zeros(self.nwords, dtype=np.uint64)
        parts = [self.bitmap_of(v) for v in values]
        parts = [p if p is not None else zero for p in parts]
        if not parts:
            return np.zeros(0, dtype=np.int64)
        return np.sort(self.words_to_rows(self.union(parts)))

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        n = len(self.row_ids)
        bitmap_bytes = self.bitmaps.nbytes
        return {
            "index_type": self.type_name,
            "num_rows": n,
            "num_values": len(self.value_list),
            "nwords": self.nwords,
            "null_rows": self.null_rows,
            "total_bitmap_bytes": int(bitmap_bytes),
            "avg_bitmap_bytes": (bitmap_bytes / len(self.value_list)) if self.value_list else 0.0,
            #: 每行摊到多少**比特**。远小于 8 = 划算；几十上百 = 该换索引
            "bits_per_row": (bitmap_bytes * 8 / n) if n else 0.0,
            #: 每个值平均命中多少行（1.0 表示这一列每个值都只出现一次 = 高基数）
            "avg_rows_per_value": (sum(self.value_counts) / len(self.value_counts))
            if self.value_counts
            else 0.0,
            "max_rows_per_value": max(self.value_counts) if self.value_counts else 0,
            "num_words_per_scan": self.nwords,
            "size_bytes": int(bitmap_bytes),
        }

    # -- 持久化 -----------------------------------------------------------
    def save(self, store: IndexStore) -> List[str]:
        """位图按字存，值表单独存（真实 Lance 里这两者一个在
        ``bitmaps.lance``、一个在 ``dictionary.lance``）。"""

        store.write_json(
            "metadata.json",
            {
                "num_rows": int(len(self.row_ids)),
                "num_values": len(self.value_list),
                "nwords": self.nwords,
                "null_rows": self.null_rows,
                "version": 1,
            },
        )
        store.write_json("values.json", self.value_list)
        store.write_numpy("value_counts.npy", np.asarray(self.value_counts, dtype=np.int64))
        store.write_numpy("bitmaps.npy", self.bitmaps)
        store.write_numpy("index_rows.npy", np.asarray(self.row_ids, dtype=np.int64))
        files = [
            "metadata.json",
            "values.json",
            "value_counts.npy",
            "bitmaps.npy",
            "index_rows.npy",
        ]
        self.metadata.files = list(files)
        return files

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "BitmapIndex":
        raw = store.read_json("metadata.json")
        index = cls(metadata)
        index.row_ids = store.read_numpy("index_rows.npy").astype(np.int64)
        index.value_list = list(store.read_json("values.json"))
        index.value_index = {v: i for i, v in enumerate(index.value_list)}
        index.value_counts = [int(x) for x in store.read_numpy("value_counts.npy").tolist()]
        index.bitmaps = store.read_numpy("bitmaps.npy")
        index.nwords = int(raw.get("nwords", index.bitmaps.shape[1] if index.bitmaps.size else 0))
        index.null_rows = int(raw.get("null_rows", 0))
        return index

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"BitmapIndex(rows={len(self.row_ids)}, values={len(self.value_list)}, "
            f"words={self.nwords})"
        )
