"""ZONEMAP：**没有索引的"索引"** —— 每个数据块记一份 "min / max / null 个数"。

## 这是 Lance 最重要的优化思想之一

Lance/LanceDB 能在**不建任何索引**的情况下把很多查询加速一个量级，
靠的就是 zone map（又叫 min-max 索引、data skipping、block pruning）。
它做的事少得可怜：

```
块 0（第 0~1023 行  ）: min=3   max=57   nulls=0
块 1（第 1024~2047行）: min=61  max=118  nulls=2
块 2（第 2048~3071行）: min=120 max=204  nulls=0
```

查询 ``WHERE x BETWEEN 0 AND 50`` 时，块 1、块 2 的 ``min`` 都大于 50 ——
**这两块里不可能有满足条件的行**，连读都不用读。三块跳掉两块。

为什么这招威力这么大？因为**数据通常是有相关性的**：按时间写进去的日志、
按 id 排序写入的主键、按类别聚在一起的订单。相关性越强，每个块的值域越窄，
块与块之间越不重叠，能跳掉的块就越多。这就是 Lance 在文件格式里把
"列按块组织 + 每块带统计" 做成**默认行为**（而不是可选优化）的原因：
它不是"给某些查询用的索引"，它让**所有**查询都受益。

## 它和 BTREE / BITMAP 的本质差别

|                | BTREE / BITMAP            | ZONEMAP                        |
|----------------|---------------------------|--------------------------------|
| 查询前要不要建 | 要，建索引很贵             | 不要，写文件时顺手就算出来了    |
| 精度           | 精确（行号集合）           | **近似**（只知道"这一块里没有"）|
| 能否跳过读盘   | 要读索引文件               | 连索引文件都不用读（统计在页头）|
| 对随机数据      | 照常有效                   | **完全失效**（每块的 min/max 都是全域）|

最后一行是关键：zone map 的效果**完全取决于数据排布**。这也解释了
Lance 为什么要在 compaction 里做"按排序列重写" —— 重写的直接收益就是
让 zone map 重新变锋利。

## 为什么 ``search()`` 返回 ``None``

因为 zone map **从来不产生"命中行号集合"**，它只产生"哪些块可以跳过"。
把它硬塞进 ``ScalarIndexExpr`` 的返回类型里会撒谎。所以本类老实实现
``search() -> None``（= "我答不了这个查询，请照常执行"），
真正的接口是 :meth:`prune` 和 :meth:`prune_expr`。

## 本实现刻意不做的事

真实 Lance 的页头统计还会存**截断后的字符串前缀**（只存前几个字节，
这样页头大小是常数），代价是字符串范围判断只能到"前缀级别"、精度略降。
这里存整串，精度更高，也更容易验证"绝不误剪"。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..arrays import Array
from ..errors import IndexBuildError
from ._expr_util import estimate_array_bytes, row_ids_of, sort_key, values_of
from .base import (
    And,
    IndexMetadata,
    IndexStore,
    IsIn,
    Or,
    RangeQuery,
    ScalarIndex,
    ScalarIndexExpr,
)

__all__ = [
    "ZoneMapIndex",
    "BlockStats",
    "PruningStats",
    "BlockMatcher",
    "RangeMatcher",
    "SetMatcher",
    "NullMatcher",
    "AndMatcher",
    "OrMatcher",
    "matcher_from_expr",
    "DEFAULT_PAGE_SIZE",
]


#: 一个块装多少行。真实 Lance 按**字节**切页（默认 1MiB 左右），
#: 这里按行数切，因为教学数据没有页大小概念，按行更好解释。
DEFAULT_PAGE_SIZE = 1024


# ---------------------------------------------------------------------------
# 块统计
# ---------------------------------------------------------------------------


@dataclass
class BlockStats:
    """一个块的统计。这是"数据跳过"的全部依据。"""

    block: int
    row_start: int
    row_end: int  # 右开
    min_value: Any
    max_value: Any
    null_count: int

    @property
    def num_rows(self) -> int:
        return self.row_end - self.row_start

    @property
    def all_null(self) -> bool:
        return self.null_count == self.num_rows

    def as_dict(self) -> Dict[str, Any]:
        return {
            "block": self.block,
            "rows": f"[{self.row_start}, {self.row_end})",
            "min": self.min_value,
            "max": self.max_value,
            "nulls": self.null_count,
        }


@dataclass
class PruningStats:
    """一次剪枝的效果 —— 教程里量"谓词下推省了多少"就靠它。"""

    blocks_total: int
    blocks_pruned: int
    rows_total: int
    rows_skipped: int
    bytes_skipped_estimate: int
    #: 谓词本身的描述，便于日志里对照
    matcher: str = ""

    @property
    def blocks_kept(self) -> int:
        return self.blocks_total - self.blocks_pruned

    @property
    def prune_ratio(self) -> float:
        """跳过的块占比。0 表示这个谓词对当前数据排布毫无帮助。"""

        return (self.blocks_pruned / self.blocks_total) if self.blocks_total else 0.0

    @property
    def row_skip_ratio(self) -> float:
        return (self.rows_skipped / self.rows_total) if self.rows_total else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "blocks_total": self.blocks_total,
            "blocks_pruned": self.blocks_pruned,
            "blocks_kept": self.blocks_kept,
            "rows_total": self.rows_total,
            "rows_skipped": self.rows_skipped,
            "bytes_skipped_estimate": self.bytes_skipped_estimate,
            "prune_ratio": self.prune_ratio,
            "row_skip_ratio": self.row_skip_ratio,
            "matcher": self.matcher,
        }

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"跳过 {self.blocks_pruned}/{self.blocks_total} 块 "
            f"({self.prune_ratio:.0%})、{self.rows_skipped}/{self.rows_total} 行，"
            f"估算省下 {self.bytes_skipped_estimate} 字节  [{self.matcher}]"
        )


# ---------------------------------------------------------------------------
# 匹配器：一个块"有没有可能"命中
# ---------------------------------------------------------------------------


class BlockMatcher(abc.ABC):
    """块级谓词。

    唯一的方法是 :meth:`could_match` —— 它必须**永不误判为"不可能"**
    （宁可返回 True 多读一块，也不能返回 False 剪掉真正命中的块）。
    这是 zone map 的全部正确性要求：**只能保守地不动手**。
    """

    @abc.abstractmethod
    def could_match(self, min_value: Any, max_value: Any, null_count: int) -> bool: ...

    def describe(self) -> str:  # pragma: no cover - 只为打印
        return type(self).__name__


@dataclass
class RangeMatcher(BlockMatcher):
    """``start <= col <= end``。"""

    start: Any
    end: Any
    inclusive_start: bool = True
    inclusive_end: bool = True

    def could_match(self, min_value: Any, max_value: Any, null_count: int) -> bool:
        if min_value is None or max_value is None:
            return False  # 整块都是 null，值谓词不可能命中
        lo = sort_key(min_value)
        hi = sort_key(max_value)
        if self.start is not None:
            s = sort_key(self.start)
            # 块的最大值都够不到下界 → 整块在区间左侧，可以剪
            if hi < s or (hi == s and not self.inclusive_start):
                return False
        if self.end is not None:
            e = sort_key(self.end)
            # 块的最小值都超过上界 → 整块在区间右侧，可以剪
            if lo > e or (lo == e and not self.inclusive_end):
                return False
        return True

    def describe(self) -> str:
        lb = "[" if self.inclusive_start else "("
        rb = "]" if self.inclusive_end else ")"
        return f"range{lb}{self.start}, {self.end}{rb}"


@dataclass
class SetMatcher(BlockMatcher):
    """``col IN (...)``。

    只用一个"是否有值落在 [min, max] 内"的判断 —— 精度不如把整个值集存下来，
    但页头统计就这么点信息，再多就不划算了。
    """

    values: List[Any]

    def could_match(self, min_value: Any, max_value: Any, null_count: int) -> bool:
        if min_value is None or max_value is None:
            return False
        lo = sort_key(min_value)
        hi = sort_key(max_value)
        for v in self.values:
            if v is None:
                continue
            k = sort_key(v)
            if lo <= k <= hi:
                return True
        return False

    def describe(self) -> str:
        return f"in{len(self.values)}"


@dataclass
class NullMatcher(BlockMatcher):
    """``col IS NULL`` / ``col IS NOT NULL`` —— 唯一真正用到 ``null_count`` 的谓词。"""

    want_null: bool = True

    def could_match(self, min_value: Any, max_value: Any, null_count: int) -> bool:
        return (null_count > 0) if self.want_null else (null_count < 1 or min_value is not None)

    def describe(self) -> str:
        return "isnull" if self.want_null else "isnotnull"


class _CompositeMatcher(BlockMatcher):
    """``AND`` / ``OR`` 的块级语义就藏在这两行里。"""

    kind = "?"

    def __init__(self, children: Sequence[BlockMatcher]) -> None:
        self.children = list(children)

    def describe(self) -> str:
        return f"{self.kind}({', '.join(c.describe() for c in self.children)})"


class AndMatcher(_CompositeMatcher):
    """块能命中 ``a AND b`` ⟺ **每一个**子谓词都可能在块内命中。

    于是剪枝是"各子谓词剪枝结果的**并集**"—— 任何一个条件能排除这块就够了。
    多重过滤条件的下推之所以收益巨大，就是这个并集在起作用。
    """

    kind = "AND"

    def could_match(self, min_value: Any, max_value: Any, null_count: int) -> bool:
        return all(c.could_match(min_value, max_value, null_count) for c in self.children)


class OrMatcher(_CompositeMatcher):
    """块能命中 ``a OR b`` ⟺ 只要有**一个**子谓词可能命中。

    剪枝是"交集"—— 必须所有子谓词都排除了这块，才敢跳。
    """

    kind = "OR"

    def could_match(self, min_value: Any, max_value: Any, null_count: int) -> bool:
        return any(c.could_match(min_value, max_value, null_count) for c in self.children)


def matcher_from_expr(expr: ScalarIndexExpr) -> Optional[BlockMatcher]:
    """把 ``ScalarIndexExpr`` 翻译成块级匹配器；翻译不了返回 ``None``。

    ``Not`` 刻意不支持：zone map 只知道"某一块里有没有可能",**不知道**
    "某一块里是不是全部满足"，而 ``NOT p`` 的剪枝恰恰需要后者
    （``NOT (x > 5)`` 只有在"整块都 > 5"时才能剪掉）。
    强行按 De Morgan 展开反而是错的，所以这里老实放弃。
    """

    if isinstance(expr, RangeQuery):
        return RangeMatcher(
            expr.start, expr.end, expr.inclusive_start, expr.inclusive_end
        )
    if isinstance(expr, IsIn):
        return SetMatcher(list(expr.values))
    if isinstance(expr, And):
        kids = [matcher_from_expr(c) for c in expr.children]
        if not kids or any(k is None for k in kids):
            return None
        return AndMatcher([k for k in kids if k is not None])
    if isinstance(expr, Or):
        kids = [matcher_from_expr(c) for c in expr.children]
        if not kids or any(k is None for k in kids):
            return None
        return OrMatcher([k for k in kids if k is not None])
    return None


# ---------------------------------------------------------------------------
# 索引本体
# ---------------------------------------------------------------------------


class ZoneMapIndex(ScalarIndex):
    """``ZONEMAP``：每个块一份 min/max/null_count。

    注意它不是"索引"而是"统计信息"：不产生行号、不参与 ``ScalarIndexExpr``
    的求值，只回答"这个块能不能跳过"。
    """

    type_name = "ZONEMAP"

    def __init__(self, metadata: IndexMetadata) -> None:
        super().__init__(metadata)
        params: Mapping[str, Any] = metadata.params or {}
        self.page_size: int = int(params.get("page_size", DEFAULT_PAGE_SIZE))
        if self.page_size < 1:
            raise IndexBuildError("page_size 必须为正", got=self.page_size)
        self.blocks: List[BlockStats] = []
        #: 整列的值域（不建索引也能算，这里顺手存下来当"全局剪枝"的依据）
        self.min_value: Any = None
        self.max_value: Any = None
        self.null_rows: int = 0
        #: 每行平均多少字节 —— 用来估算"跳过这块省了多少读盘"
        self.bytes_per_row: float = 0.0
        #: 哪些行是 null（``IS NULL`` 类查询直接可用它，不需要块级判断）
        self.null_mask: np.ndarray = np.zeros(0, dtype=bool)
        #: 原值副本。**真实实现不留这个** —— 剪枝后它会去读那一页。
        #: 这里留一份，是为了让"剪枝 + 求值"的对照实验能在索引内部闭环完成。
        self._values: List[Any] = []

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls, values: Array, row_ids: np.ndarray, *, metadata: IndexMetadata
    ) -> "ZoneMapIndex":
        """扫描一遍列，算出每块的 min/max/null 数。

        注意这一步是**随写文件顺手做的**：数据本来就要顺序写一遍，
        统计只是顺手比较大小。这就是 zone map 相对 BTREE/BITMAP 的最大优势 ——
        **零额外开销**（真实系统里连第二趟扫描都不需要）。
        """

        index = cls(metadata)
        rows = row_ids_of(row_ids, len(values))
        vals = values_of(values)
        index.row_ids = rows
        n = len(vals)

        nulls = np.asarray([v is None for v in vals], dtype=bool)
        index.null_mask = nulls
        index.null_rows = int(nulls.sum())
        size = estimate_array_bytes(values)
        index.bytes_per_row = (size / n) if n else 0.0

        index._values = list(vals)
        global_min: Any = None
        global_max: Any = None
        for start in range(0, max(n, 1), index.page_size):
            end = min(start + index.page_size, n)
            if start >= n:
                break
            chunk = vals[start:end]
            min_v: Any = None
            max_v: Any = None
            null_count = 0
            for v in chunk:
                if v is None:
                    null_count += 1
                    continue
                if min_v is None or sort_key(v) < sort_key(min_v):
                    min_v = v
                if max_v is None or sort_key(v) > sort_key(max_v):
                    max_v = v
            index.blocks.append(
                BlockStats(
                    block=len(index.blocks),
                    row_start=start,
                    row_end=end,
                    min_value=min_v,
                    max_value=max_v,
                    null_count=null_count,
                )
            )
            if min_v is not None:
                if global_min is None or sort_key(min_v) < sort_key(global_min):
                    global_min = min_v
                if global_max is None or sort_key(max_v) > sort_key(global_max):
                    global_max = max_v
        index.min_value = global_min
        index.max_value = global_max
        metadata.num_indexed_rows = int(n)
        metadata.details["num_blocks"] = len(index.blocks)
        metadata.details["page_size"] = index.page_size
        metadata.size_bytes = int(index.size_bytes)
        return index

    # -- 剪枝 -------------------------------------------------------------
    def block_could_match(self, matcher: BlockMatcher) -> np.ndarray:
        """每个块"有没有可能命中"（True = 不能剪）。长度 = 块数。"""

        return np.asarray(
            [
                bool(matcher.could_match(b.min_value, b.max_value, b.null_count))
                for b in self.blocks
            ],
            dtype=bool,
        )

    def prune(self, matcher: Any) -> np.ndarray:
        """**哪些块可以跳过**（True = 可以跳过）。

        ``matcher`` 既可以是 :class:`BlockMatcher`，也可以直接给一个
        ``ScalarIndexExpr``（内部会自动翻译）。翻译不了时返回全 False ——
        "一块都别跳"是唯一安全的答案。
        """

        if not isinstance(matcher, BlockMatcher):
            matcher = matcher_from_expr(matcher)
        if matcher is None:
            return np.zeros(len(self.blocks), dtype=bool)
        return ~self.block_could_match(matcher)

    def rows_to_keep(self, matcher: Any) -> np.ndarray:
        """把块级判断摊成行级掩码（True = 这一行所在块没被剪，仍然要读）。"""

        pruned = self.prune(matcher)
        mask = np.zeros(len(self.row_ids), dtype=bool)
        for b, skip in zip(self.blocks, pruned.tolist()):
            if not skip:
                mask[b.row_start : b.row_end] = True
        return mask

    def prune_expr(self, expr: ScalarIndexExpr) -> Tuple[np.ndarray, PruningStats]:
        """给一个 ``ScalarIndexExpr``，返回 ``(行级保留掩码, PruningStats)``。"""

        matcher = matcher_from_expr(expr)
        pruned = self.prune(matcher)
        rows_total = len(self.row_ids)
        rows_skipped = 0
        for b, skip in zip(self.blocks, pruned.tolist()):
            if skip:
                rows_skipped += b.num_rows
        stats = PruningStats(
            blocks_total=len(self.blocks),
            blocks_pruned=int(pruned.sum()),
            rows_total=rows_total,
            rows_skipped=rows_skipped,
            bytes_skipped_estimate=int(round(rows_skipped * self.bytes_per_row)),
            matcher=matcher.describe() if matcher is not None else "unsupported",
        )
        return self.rows_to_keep(matcher), stats

    def search(self, query: ScalarIndexExpr) -> Optional[ScalarIndexExpr]:
        """永远返回 ``None``。

        zone map 不产生行号集合，硬要它返回一个 ``ExactMatch`` 就等于骗人。
        查询引擎的正确用法是：先 ``prune_expr`` 拿到"要读哪些行"，
        然后再对这些行做真正的谓词求值。
        """

        return None

    def filter_by_expr(
        self, expr: ScalarIndexExpr, predicate: Any
    ) -> Tuple[np.ndarray, PruningStats]:
        """一个完整的"下推 + 求值"示范：

        1. 用 zone map 剪掉不可能命中的块；
        2. 只在剩下的行上跑真正的 ``predicate(value)``。

        结果和"直接全表跑 predicate"**必须完全一致** —— 这是 zone map
        唯一不能违反的性质，测试里拿暴力法逐行对拍。
        """

        keep, stats = self.prune_expr(expr)
        rows = np.flatnonzero(keep)
        matched = [int(r) for r in rows if predicate(self.value_at(int(r)))]
        return np.asarray(matched, dtype=np.int64), stats

    # -- 取值 -------------------------------------------------------------
    def value_at(self, row: int) -> Any:
        """按行号取值。为了自洽，ZONEMAP 也把原值留了一份。

        （真实实现当然不留 —— 它会去读那一页。这里留一份是为了让
        "剪枝后求值"的对照实验能在索引内部完成。）
        """

        return self._values[int(row)] if self._values else None

    def values(self) -> List[Any]:
        """原值列表（只在本实现里有；真实实现请回表取）。"""

        return list(self._values)

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        blocks_with_nulls = sum(1 for b in self.blocks if b.null_count)
        # 每块 2 个值 + 1 个计数，粗估 32 字节；真实 Lance 里页头是定长大小的
        size = len(self.blocks) * 32 + self.null_mask.nbytes
        return {
            "index_type": self.type_name,
            "num_rows": int(len(self.row_ids)),
            "page_size": self.page_size,
            "num_blocks": len(self.blocks),
            "blocks_with_nulls": blocks_with_nulls,
            "null_rows": self.null_rows,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "bytes_per_row": self.bytes_per_row,
            #: 每块平均的值域宽度（数值列才有意义）
            "avg_block_span": self._avg_block_span(),
            "stored_value_bytes": sum(len(str(v).encode("utf-8")) for v in self._values if v is not None),
            "size_bytes": int(size),
        }

    def _avg_block_span(self) -> Optional[float]:
        spans: List[float] = []
        for b in self.blocks:
            if b.min_value is None or b.max_value is None:
                continue
            if isinstance(b.min_value, (int, float)) and isinstance(
                b.max_value, (int, float)
            ):
                spans.append(float(b.max_value) - float(b.min_value))
        return (sum(spans) / len(spans)) if spans else None

    def block_table(self) -> List[Dict[str, Any]]:
        """给教程用的块统计表。"""

        return [b.as_dict() for b in self.blocks]

    # -- 持久化 -----------------------------------------------------------
    def save(self, store: IndexStore) -> List[str]:
        store.write_json(
            "metadata.json",
            {
                "num_rows": int(len(self.row_ids)),
                "page_size": self.page_size,
                "num_blocks": len(self.blocks),
                "null_rows": self.null_rows,
                "min_value": self.min_value,
                "max_value": self.max_value,
                "bytes_per_row": self.bytes_per_row,
                "version": 1,
            },
        )
        store.write_json(
            "blocks.json",
            [
                {
                    "block": b.block,
                    "row_start": b.row_start,
                    "row_end": b.row_end,
                    "min_value": b.min_value,
                    "max_value": b.max_value,
                    "null_count": b.null_count,
                }
                for b in self.blocks
            ],
        )
        store.write_numpy("index_rows.npy", np.asarray(self.row_ids, dtype=np.int64))
        store.write_json("values.json", self._values)
        store.write_numpy("null_mask.npy", self.null_mask)
        files = [
            "metadata.json",
            "blocks.json",
            "index_rows.npy",
            "null_mask.npy",
            "values.json",
        ]
        self.metadata.files = list(files)
        return files

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "ZoneMapIndex":
        raw = store.read_json("metadata.json")
        metadata.params.setdefault("page_size", int(raw.get("page_size", DEFAULT_PAGE_SIZE)))
        index = cls(metadata)
        index.row_ids = store.read_numpy("index_rows.npy").astype(np.int64)
        index.blocks = [
            BlockStats(
                block=int(b["block"]),
                row_start=int(b["row_start"]),
                row_end=int(b["row_end"]),
                min_value=b["min_value"],
                max_value=b["max_value"],
                null_count=int(b["null_count"]),
            )
            for b in store.read_json("blocks.json")
        ]
        index.null_mask = store.read_numpy("null_mask.npy")
        if store.exists("values.json"):
            index._values = list(store.read_json("values.json"))
        index.null_rows = int(raw.get("null_rows", 0))
        index.page_size = int(raw.get("page_size", DEFAULT_PAGE_SIZE))
        index.min_value = raw.get("min_value")
        index.max_value = raw.get("max_value")
        index.bytes_per_row = float(raw.get("bytes_per_row", 0.0))
        return index

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ZoneMapIndex(rows={len(self.row_ids)}, blocks={len(self.blocks)}, "
            f"page_size={self.page_size})"
        )
