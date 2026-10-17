"""BTREE 索引：真正的 B+ 树，不是"排序数组 + 二分"。

## 为什么是 B+ 树而不是哈希表

哈希表能做到 O(1) 的等值查找，但**范围查询**在它上面是 O(n) ——
因为哈希把顺序彻底打碎了，``age BETWEEN 30 AND 40`` 无从下手。
数据库里范围谓词（``>``、``<``、``BETWEEN``、``ORDER BY ... LIMIT``）
出现得比等值谓词还频繁，所以标量索引的主力结构必须**保序**。

在保序结构里，为什么不用平衡二叉搜索树（红黑树）？

* B+ 树的**扇出**（一个节点几百个键）让树高极低。100 万行、叶节点 256 个键时，
  树高只有 3 层 —— 三次页读取就能定位，而红黑树要 20 次。
  磁盘/页缓存场景下，"层数"直接等于 I/O 次数，这就是 B+ 树赢的原因。
* B+ 树把**数据全放在叶子**，内部节点只存分隔键（``keys[i]`` 是
  ``children[i+1]`` 的最小键）。于是内部节点很小、缓存友好，
  而且**叶子之间有链表**，范围扫描是"定位一次 + 顺序走到底"，
  不需要反复回根节点做中序遍历。

这两个性质是 B+ 树在数据库里活到今天的原因，也是本实现刻意保留的形状：

```
        [ 30 | 70 ]                     <- 内部节点：只放分隔键
        /    |     \\
   [1..29] [30..69] [70..99]             <- 叶子：键 + 行号列表，next 串成链表
        ->       ->        ->
```

## 为什么行号要存成"一个键对应一串行号"

一列索引不是主键，``status = 'active'`` 会命中几十万行。如果每个 (键, 行号)
都存一个条目，B+ 树会被重复键撑爆（树高多一层、叶节点全是同值）。
所以叶子里的条目是 ``(键, 值, 行号数组)`` —— **键唯一，行号是列表**。
这也是为什么 B+ 树天然适合做"列索引"而不是"主键索引"。

## 这个索引能回答什么

* ``RangeQuery`` —— 本命（保序结构的强项）；
* ``IsIn`` 单值 —— 就是等值查找；
* ``IsIn`` 多值 —— 每个值查一次再合并（值很多时不如 BITMAP，
  这就是 LanceDB 规划器要按"基数列 + 谓词形态"挑索引的原因）。

``IsIn`` 的**值很多**时，真正的 Lance 会转而用 BITMAP。我们在这里不做这种
自动切换（那是查询规划器的活），但把选择依据写在 :meth:`stats` 里：
``num_keys`` 相对 ``num_rows`` 的比例就是基数。

## 与真实 Lance 的差别

真实 Lance 的 BTREE 是**磁盘上分页**的（``page_data.lance`` +
``page_lookup.lance``：一页装 N 个键，页表记录每页的首键），
所以"树"其实是"页表 + 二分"。本实现把所有节点放在内存里做真正的分裂，
形状更接近教科书 B+ 树，教学上更容易看清"分裂"和"分隔键维护"这两件事。
两者在下标 API 上完全一致。
"""

from __future__ import annotations

import bisect
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..arrays import Array
from ..errors import IndexBuildError
from ._expr_util import combine_rows, materialize, row_ids_of, sort_key, values_of
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

__all__ = ["BTreeIndex", "BTreeNode", "DEFAULT_MAX_LEAF_SIZE", "DEFAULT_MAX_CHILDREN"]

#: 一个叶子最多放多少个不同的键。真实引擎按"页大小 / 条目大小"算出来，
#: 这里给一个常量；测试里会把它调小到 4 来强制触发分裂。
DEFAULT_MAX_LEAF_SIZE = 64
#: 一个内部节点最多有多少个孩子（= 最多 63 个分隔键）。
DEFAULT_MAX_CHILDREN = 64


class BTreeNode:
    """B+ 树节点的公共基类（只为 ``isinstance`` 判断和可读性存在）。"""

    __slots__ = ("keys",)

    keys: List[Any]


class _Leaf(BTreeNode):
    """叶子：``(keys[i], values[i], rows[i])`` 是一个条目，``next`` 串链表。"""

    __slots__ = ("keys", "values", "rows", "next")

    def __init__(self) -> None:
        self.keys: List[Any] = []
        self.values: List[Any] = []
        self.rows: List[List[int]] = []
        self.next: Optional["_Leaf"] = None

    @property
    def minkey(self) -> Any:
        """这棵子树里的最小键（叶子里就是 ``keys[0]``）。"""

        return self.keys[0] if self.keys else None


class _Internal(BTreeNode):
    """内部节点：``keys[i]`` 是 ``children[i+1]`` 的最小键。

    不变式（维护它的是 :meth:`BTreeIndex._insert` 里那几行赋值）：
    ``children[i]`` 里的所有键都 ``< keys[i] <= children[i+1]`` 里的键。
    ``children[0]`` 没有下界 —— 它由**父节点**的分隔键来管，
    所以 ``keys`` 的长度恒为 ``len(children) - 1``。

    ⚠️ 一个很容易踩的坑：内部节点的 ``keys[0]`` 是"**第 2 个**孩子的最小键"，
    **不是**这棵子树的最小键。所以父节点要维护分隔键时**不能**读
    ``child.keys[0]`` —— 那会在"往最左边插入更小的键"时给出偏大的边界，
    表现为范围扫描莫名漏掉或带进来一些行。正确做法是显式带上
    :attr:`minkey`（子树真正的最小键），沿着递归路径逐层往上传。
    """

    __slots__ = ("keys", "children")

    def __init__(self) -> None:
        self.keys: List[Any] = []
        self.children: List[BTreeNode] = []

    @property
    def minkey(self) -> Any:
        return self.children[0].minkey if self.children else None


class BTreeIndex(ScalarIndex):
    """``BTREE`` 标量索引。

    位置参数和 :class:`ScalarIndex` 一致；额外参数从
    ``metadata.params`` 读：

    * ``max_leaf_size``（默认 64）
    * ``max_children``（默认 64，内部节点扇出）
    """

    type_name = "BTREE"

    def __init__(self, metadata: IndexMetadata) -> None:
        super().__init__(metadata)
        params: Mapping[str, Any] = metadata.params or {}
        self.max_leaf_size = int(params.get("max_leaf_size", DEFAULT_MAX_LEAF_SIZE))
        self.max_children = int(params.get("max_children", DEFAULT_MAX_CHILDREN))
        if self.max_leaf_size < 2:
            raise IndexBuildError("max_leaf_size 至少为 2", got=self.max_leaf_size)
        if self.max_children < 3:
            raise IndexBuildError("max_children 至少为 3", got=self.max_children)

        self.root: Optional[BTreeNode] = None
        self.depth: int = 0
        #: 建索引那一刻列的宽类型 —— 用来判断"这次查询还值不值得走索引"
        self.value_type: Optional[Any] = None
        #: 参与索引的行数；null 行不进索引（见 build 的说明）
        self.null_rows: int = 0

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls, values: Array, row_ids: np.ndarray, *, metadata: IndexMetadata
    ) -> "BTreeIndex":
        """从一列建立 B+ 树。

        **null 值不进索引。** 这是刻意对齐 SQL 的三值逻辑：``col = 1`` 对
        NULL 行求值是 UNKNOWN 而不是 TRUE，所以 NULL 行永远不该被
        "等于/大于"这类谓词命中。（要筛 NULL 得用 ``IS NULL``，
        那是另一条路径，Lance 用 zone map 的 null_count 直接答。）

        索引因此只覆盖"非 null 行"，这一点会写进 :meth:`stats` 的
        ``null_rows``，让"索引用不上"的排查有据可查。
        """

        index = cls(metadata)
        rows = row_ids_of(row_ids, len(values))
        vals = values_of(values)
        index.value_type = values.type
        covered: List[int] = []
        for value, row in zip(vals, rows):
            if value is None:
                index.null_rows += 1
                continue
            covered.append(int(row))
            index.add(value, int(row))
        # row_ids 只包含**真的进了索引**的行（非 null）。
        # 这一点很要紧：And/Or/Not 的求值以 row_ids 为"全集"，
        # 于是 NOT(col = 1) 不会把 null 行算进去 —— 和 SQL 的三值逻辑一致。
        index.row_ids = np.asarray(covered, dtype=np.int64)
        metadata.num_indexed_rows = int(len(covered))
        metadata.size_bytes = int(index.size_bytes)
        return index

    def add(self, value: Any, row: int) -> None:
        """插入 ``(value, row)``；同值行号挂在同一个条目上。"""

        key = sort_key(value)
        if self.root is None:
            self.root = _Leaf()
        split = self._insert(self.root, key, value, row)
        if split is not None:
            new_root = _Internal()
            new_root.children = [self.root, split]
            new_root.keys = [split.minkey]
            self.root = new_root
            self.depth += 1

    # -- 树的内部操作 -----------------------------------------------------
    def _insert(
        self, node: BTreeNode, key: Any, value: Any, row: int
    ) -> Optional[BTreeNode]:
        """递归插入；返回**新分裂出来的右兄弟**表示发生了分裂。"""

        if isinstance(node, _Leaf):
            i = bisect.bisect_left(node.keys, key)
            if i < len(node.keys) and node.keys[i] == key:
                # 键已存在：只追加行号，不新建条目 —— 这是"列索引"的关键
                node.rows[i].append(row)
                return None
            node.keys.insert(i, key)
            node.values.insert(i, value)
            node.rows.insert(i, [row])
            if len(node.keys) > self.max_leaf_size:
                return self._split_leaf(node)
            return None

        assert isinstance(node, _Internal)
        i = bisect.bisect_right(node.keys, key)
        child = node.children[i]
        split = self._insert(child, key, value, row)
        if i > 0:
            # 维护分隔键：children[i] 的最小键可能因为这次插入变小了。
            # 用 minkey（子树真正的最小键）而不是 keys[0]（第 2 个孩子的最小键）。
            # i == 0 时没有"左边的分隔键"可改 —— 那一侧由父节点递归维护。
            node.keys[i - 1] = child.minkey
        if split is None:
            return None
        node.children.insert(i + 1, split)
        node.keys.insert(i, split.minkey)
        if len(node.children) > self.max_children:
            return self._split_internal(node)
        return None

    def _split_leaf(self, leaf: _Leaf) -> _Leaf:
        mid = len(leaf.keys) // 2
        right = _Leaf()
        right.keys = leaf.keys[mid:]
        right.values = leaf.values[mid:]
        right.rows = leaf.rows[mid:]
        del leaf.keys[mid:]
        del leaf.values[mid:]
        del leaf.rows[mid:]
        # 链表接上：范围扫描靠它才能"定位一次、顺序走到底"
        right.next = leaf.next
        leaf.next = right
        return right

    def _split_internal(self, node: _Internal) -> _Internal:
        mid = len(node.children) // 2
        right = _Internal()
        right.children = node.children[mid:]
        right.keys = node.keys[mid:]
        node.children = node.children[:mid]
        node.keys = node.keys[: mid - 1]
        # 注意 promoted 不用单独返回：right.minkey 就是那个要提升的分隔键
        # （它等于原来的 keys[mid-1] == children[mid] 的最小键）。
        return right

    def _find_leaf(self, key: Any) -> Optional[_Leaf]:
        node = self.root
        if node is None:
            return None
        while isinstance(node, _Internal):
            node = node.children[bisect.bisect_right(node.keys, key)]
        return node

    def _leftmost_leaf(self) -> Optional[_Leaf]:
        node = self.root
        if node is None:
            return None
        while isinstance(node, _Internal):
            node = node.children[0]
        return node

    # -- 查询 -------------------------------------------------------------
    def lookup(self, value: Any) -> List[int]:
        """等值查找，返回行号（按插入顺序）。"""

        if value is None:
            return []  # null 不参与等值匹配
        leaf = self._find_leaf(sort_key(value))
        if leaf is None:
            return []
        key = sort_key(value)
        i = bisect.bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            return list(leaf.rows[i])
        return []

    def range_rows(
        self,
        start: Any,
        end: Any,
        inclusive_start: bool = True,
        inclusive_end: bool = True,
    ) -> List[int]:
        """区间扫描：定位起点叶子，然后顺着 ``next`` 链表往前走。"""

        leaf = self._leftmost_leaf() if start is None else self._find_leaf(sort_key(start))
        lo_key = None if start is None else sort_key(start)
        hi_key = None if end is None else sort_key(end)
        out: List[int] = []
        while leaf is not None:
            stop = False
            for key, rows in zip(leaf.keys, leaf.rows):
                if hi_key is not None:
                    if key > hi_key or (key == hi_key and not inclusive_end):
                        stop = True
                        break
                if lo_key is not None:
                    if key < lo_key or (key == lo_key and not inclusive_start):
                        continue
                out.extend(rows)
            if stop:
                break
            leaf = leaf.next
        return out

    def _rows_of_many(self, values: Sequence[Any]) -> List[int]:
        out: List[int] = []
        for v in values:
            out.extend(self.lookup(v))
        return out

    def search(self, query: ScalarIndexExpr) -> Optional[ScalarIndexExpr]:
        """翻译谓词；答不了返回 ``None``（让上游退化成扫描）。"""

        if isinstance(query, ExactMatch):
            # 已经是最简形式，原样返回：省掉一次无意义的"翻译"
            return query
        if isinstance(query, RangeQuery):
            rows = self.range_rows(
                query.start, query.end, query.inclusive_start, query.inclusive_end
            )
            return ExactMatch(np.sort(np.asarray(rows, dtype=np.int64)))
        if isinstance(query, IsIn):
            rows = self._rows_of_many(query.values)
            return ExactMatch(np.sort(np.asarray(rows, dtype=np.int64)))
        if isinstance(query, (And, Or, Not)):
            materialized = materialize(query, self.search)
            if materialized is None:
                return None
            return combine_rows(self, materialized)
        # 例如向量检索的谓词、FTS 的 MatchQuery —— BTREE 一律承认自己答不了
        return None

    # -- 观测 -------------------------------------------------------------
    def num_keys(self) -> int:
        return sum(len(leaf.keys) for leaf in self._leaves())

    def _leaves(self) -> List[_Leaf]:
        out: List[_Leaf] = []
        leaf = self._leftmost_leaf()
        while leaf is not None:
            out.append(leaf)
            leaf = leaf.next
        return out

    def num_nodes(self) -> int:
        if self.root is None:
            return 0

        def walk(node: BTreeNode) -> int:
            if isinstance(node, _Leaf):
                return 1
            return 1 + sum(walk(c) for c in node.children)

        return walk(self.root)

    def key_bounds(self) -> Tuple[Optional[Any], Optional[Any]]:
        """``(最小值, 最大值)`` —— 剪枝（"谓词和这棵树的值域不相交"）的依据。"""

        leaves = self._leaves()
        if not leaves or not leaves[0].keys:
            return None, None
        first = leaves[0]
        last = leaves[-1]
        return first.values[0], last.values[-1]

    def stats(self) -> Dict[str, Any]:
        leaves = self._leaves()
        entries = sum(len(leaf.keys) for leaf in leaves)
        rows = sum(len(r) for leaf in leaves for r in leaf.rows)
        # 粗估：每个键一个 Python 对象 + 每行一个 int64
        size = entries * 48 + rows * 8 + self.num_nodes() * 64
        lo, hi = self.key_bounds()
        return {
            "index_type": self.type_name,
            "num_rows": int(len(self.row_ids)),
            "num_keys": entries,
            "num_nodes": self.num_nodes(),
            "num_leaves": len(leaves),
            "depth": self.depth,
            "max_leaf_size": self.max_leaf_size,
            "max_children": self.max_children,
            "null_rows": self.null_rows,
            "min_key": lo,
            "max_key": hi,
            #: 基数 = 不同值 / 总行数。越接近 1 越适合 BTREE，越接近 0 越适合 BITMAP
            "cardinality": (entries / rows) if rows else 0.0,
            "size_bytes": int(size),
        }

    # -- 持久化 -----------------------------------------------------------
    def save(self, store: IndexStore) -> List[str]:
        """写盘：拍平成"有序的 (值, 行号列表)"两张表。

        重建 B+ 树只需要**有序插入**，所以没必要把节点布局也存下来 ——
        Lance 也一样，索引文件里存的是"按序遍历能顺序读到的页"，
        而不是指针结构。
        """

        values: List[Any] = []
        row_blocks: List[List[int]] = []
        for leaf in self._leaves():
            for value, rows in zip(leaf.values, leaf.rows):
                values.append(value)
                row_blocks.append(list(rows))
        store.write_json(
            "metadata.json",
            {
                "num_keys": len(values),
                "num_rows": int(len(self.row_ids)),
                "null_rows": self.null_rows,
                "max_leaf_size": self.max_leaf_size,
                "max_children": self.max_children,
                "version": 1,
            },
        )
        store.write_json("keys.json", values)
        flat: List[int] = []
        offsets: List[int] = [0]
        for rows in row_blocks:
            flat.extend(int(r) for r in rows)
            offsets.append(len(flat))
        store.write_numpy("row_offsets.npy", np.asarray(offsets, dtype=np.int64))
        store.write_numpy("row_ids.npy", np.asarray(flat, dtype=np.int64))
        store.write_numpy(
            "index_rows.npy", np.asarray(self.row_ids, dtype=np.int64)
        )
        files = ["metadata.json", "keys.json", "row_offsets.npy", "row_ids.npy", "index_rows.npy"]
        self.metadata.files = list(files)
        return files

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "BTreeIndex":
        raw = store.read_json("metadata.json")
        metadata.params.setdefault("max_leaf_size", int(raw.get("max_leaf_size", DEFAULT_MAX_LEAF_SIZE)))
        metadata.params.setdefault("max_children", int(raw.get("max_children", DEFAULT_MAX_CHILDREN)))
        index = cls(metadata)
        keys = store.read_json("keys.json")
        offsets = store.read_numpy("row_offsets.npy")
        flat = store.read_numpy("row_ids.npy")
        index.row_ids = store.read_numpy("index_rows.npy").astype(np.int64)
        index.null_rows = int(raw.get("null_rows", 0))
        # 按序遍历插入 → 叶节点顺序自然正确，不需要再平衡
        for i, value in enumerate(keys):
            rows = flat[int(offsets[i]) : int(offsets[i + 1])]
            for row in rows:
                index.add(value, int(row))
        return index

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"BTreeIndex(keys={self.num_keys()}, rows={len(self.row_ids)}, "
            f"depth={self.depth})"
        )
