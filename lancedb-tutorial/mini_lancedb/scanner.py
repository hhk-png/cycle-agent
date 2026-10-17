"""扫描器 —— 把"用户的查询"翻译成"读哪些片段的哪些列"。

一次 ``SELECT ... WHERE ... LIMIT ...`` 在 Lance 里会被拆成这些下推：

1. **列投影下推**：只打开包含所需列的数据文件，只读这些列的页；
2. **谓词下推（zone map 剪枝）**：用片段级 / 列级的 min-max 统计
   直接跳过"不可能命中"的片段，**这一步不读任何数据**；
3. **索引下推**：如果谓词能翻译成标量索引能回答的形式（区间、IN、等值），
   就用索引直接拿到行号集合，把"扫描"变成"取行"；
4. **延迟物化（late materialization）**：先只读过滤用的列，算出掩码，
   再只把活下来的行需要的投影列读回来。**这是列存最大的杀招**：
   过滤条件命中率 1% 时，投影列的读取量就只有 1%；
5. **行范围下推**：``limit`` 只影响"读够了就停"，不需要全表。

"下推"这个词听起来抽象，但它只回答一个问题：
**这个操作能不能让下游少读一点字节？** 能下推的算子越多，
同一台机器能撑的数据量就越大。

本模块还把每一步的实际 I/O 记下来（``ScanStats``），
因为"下推到底有没有生效"只能靠数字判断，不能靠感觉。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .arrays import Array, indices_to_mask
from .errors import NotSupportedError, PlanError
from .expr import evaluate, evaluate_predicate, parse, split_conjuncts, to_index_expr
from .index.base import ExactMatch, ScalarIndexExpr
from .record import RecordBatch
from .schema import Schema

__all__ = [
    "Scanner",
    "ScanStats",
    "ScanPlan",
    "FragmentPruning",
    "count_rows_matching",
]


@dataclass
class ScanStats:
    """扫描的实测数字 —— 性能章节里所有结论的证据来源。"""

    fragments_total: int = 0
    fragments_scanned: int = 0
    fragments_pruned_by_stats: int = 0
    fragments_pruned_by_index: int = 0
    rows_scanned: int = 0
    rows_after_filter: int = 0
    bytes_read: int = 0
    range_reads: int = 0
    pages_read: int = 0
    pages_skipped: int = 0
    index_lookups: int = 0
    elapsed_ms: float = 0.0
    #: 延迟物化省下了多少行的投影列读取
    late_materialization_saved_rows: int = 0

    def summary(self) -> Dict[str, Any]:
        return {
            "片段总数": self.fragments_total,
            "实际扫描片段": self.fragments_scanned,
            "被 zone map 剪掉的片段": self.fragments_pruned_by_stats,
            "被索引剪掉的片段": self.fragments_pruned_by_index,
            "扫描行数": self.rows_scanned,
            "过滤后行数": self.rows_after_filter,
            "读取字节": self.bytes_read,
            "范围读次数": self.range_reads,
            "读页数": self.pages_read,
            "跳过页数": self.pages_skipped,
            "索引查询次数": self.index_lookups,
            "延迟物化省下的行数": self.late_materialization_saved_rows,
            "耗时(ms)": round(self.elapsed_ms, 3),
        }

    def report(self) -> str:
        return "\n".join(f"{k}: {v}" for k, v in self.summary().items())


@dataclass
class FragmentPruning:
    fragment_id: int
    kept: bool
    reason: str
    rows: int = 0


@dataclass
class ScanPlan:
    """可打印的查询计划 —— 和 Lance 的 ``explain_plan()`` 输出同构。"""

    dataset_uri: str
    dataset_version: int
    projection: List[str]
    filter_sql: Optional[str]
    limit: Optional[int]
    steps: List[str] = dc_field(default_factory=list)
    pruned: List[FragmentPruning] = dc_field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        pad = " " * indent
        lines = [
            f"{pad}ProjectionExec: {', '.join(self.projection)}",
            f"{pad}  Dataset: {self.dataset_uri} @ v{self.dataset_version}",
        ]
        for s in self.steps:
            lines.append(f"{pad}  {s}")
        kept = sum(1 for p in self.pruned if p.kept)
        lines.append(
            f"{pad}  FragmentScan: 保留 {kept}/{len(self.pruned)} 个片段"
        )
        for p in self.pruned:
            # 刻意只用 ASCII 标记：Windows 控制台默认是 GBK，
            # 打印 ✓ / ✗ 这类字符会直接抛 UnicodeEncodeError，
            # 把一次正常的 explain 变成一次崩溃。
            mark = "[keep]" if p.kept else "[PRUNE]"
            lines.append(f"{pad}    {mark:<8} fragment {p.fragment_id} ({p.rows} 行): {p.reason}")
        return "\n".join(lines)


class Scanner:
    """数据集上的一个懒查询。

    Scanner 是**不可变 + 链式**的：每个 ``with_*`` 返回一个新的 Scanner，
    于是同一个 Dataset 上可以安全地并发构造多个查询。
    """

    def __init__(
        self,
        dataset: Any,
        *,
        columns: Optional[Sequence[str]] = None,
        filter: Optional[str] = None,
        limit: Optional[int] = None,
        batch_size: Optional[int] = None,
        row_range: Optional[Tuple[int, int]] = None,
        use_indices: bool = True,
        use_zone_maps: bool = True,
        late_materialization: bool = True,
        with_row_id: bool = False,
    ) -> None:
        self.dataset = dataset
        # 注意用 ``is not None`` 而不是真值判断：``columns=[]``（只要行数、
        # 不要任何列）是合法且常用的请求，用真值判断会把它变成"全选"。
        self.columns = list(columns) if columns is not None else None
        self.filter_sql = filter
        self.limit = limit
        self.batch_size = batch_size
        self.row_range = row_range
        self.use_indices = use_indices
        self.use_zone_maps = use_zone_maps
        self.late_materialization = late_materialization
        self.with_row_id = with_row_id
        self.stats = ScanStats()
        self._pruned: List[FragmentPruning] = []

    # -- 链式构造 ---------------------------------------------------------
    def _clone(self, **kw: Any) -> "Scanner":
        s = Scanner(
            self.dataset,
            columns=kw.pop("columns", self.columns),
            filter=kw.pop("filter", self.filter_sql),
            limit=kw.pop("limit", self.limit),
            batch_size=kw.pop("batch_size", self.batch_size),
            row_range=kw.pop("row_range", self.row_range),
            use_indices=kw.pop("use_indices", self.use_indices),
            use_zone_maps=kw.pop("use_zone_maps", self.use_zone_maps),
            late_materialization=kw.pop("late_materialization", self.late_materialization),
            with_row_id=kw.pop("with_row_id", self.with_row_id),
        )
        return s

    def with_columns(self, columns: Sequence[str]) -> "Scanner":
        return self._clone(columns=list(columns))

    def with_filter(self, predicate: Optional[str]) -> "Scanner":
        return self._clone(filter=predicate)

    def with_limit(self, limit: Optional[int]) -> "Scanner":
        return self._clone(limit=limit)

    def with_batch_size(self, n: Optional[int]) -> "Scanner":
        return self._clone(batch_size=n)

    def with_row_range(self, rng: Optional[Tuple[int, int]]) -> "Scanner":
        return self._clone(row_range=rng)

    def with_row_id(self, enabled: bool = True) -> "Scanner":
        return self._clone(with_row_id=enabled)

    def disable_indices(self) -> "Scanner":
        return self._clone(use_indices=False)

    def disable_zone_maps(self) -> "Scanner":
        return self._clone(use_zone_maps=False)

    def disable_late_materialization(self) -> "Scanner":
        return self._clone(late_materialization=False)

    def select(self, columns: Sequence[str]) -> "Scanner":
        return self.with_columns(columns)

    # -- 解析 -------------------------------------------------------------
    @property
    def schema(self) -> Schema:
        if self.columns:
            return self.dataset.schema.select(self.columns)
        return self.dataset.schema

    def _projection(self) -> List[str]:
        return list(self.columns) if self.columns else list(self.dataset.schema.names)

    def _filter_columns(self) -> List[str]:
        if not self.filter_sql:
            return []
        from .expr import parse

        expr = parse(self.filter_sql, self.dataset.schema)
        return sorted(expr.references())

    def _needed_columns(self) -> List[str]:
        """真正需要读的列 = 投影列 ∪ 过滤列。"""

        proj = self._projection()
        filt = self._filter_columns()
        out = list(proj)
        for c in filt:
            if c not in out:
                out.append(c)
        return out

    # -- 执行 -------------------------------------------------------------
    def to_batches(self) -> Iterator[RecordBatch]:
        start = time.perf_counter()
        before = self.dataset.store.stats.snapshot()
        results = list(self._run())
        after = self.dataset.store.stats.snapshot()
        # I/O 统计直接从对象存储那一层取差值：这样无论读路径怎么重构
        # （换编码、加缓存、改下推策略），数字都还是真的。
        self.stats.bytes_read = after["bytes_read"] - before["bytes_read"]
        self.stats.range_reads = after["gets"] - before["gets"]
        self.stats.elapsed_ms = (time.perf_counter() - start) * 1000
        emitted = 0
        size = self.batch_size or 4096
        for batch in results:
            for piece in batch.split_by_rows(size):
                if piece.num_rows == 0:
                    continue
                if self.limit is not None and emitted >= self.limit:
                    return
                if self.limit is not None and emitted + piece.num_rows > self.limit:
                    piece = piece.slice(0, self.limit - emitted)
                emitted += piece.num_rows
                yield piece

    def _run(self) -> Iterator[RecordBatch]:
        ds = self.dataset
        ds._row_map()  # 确保映射已建
        self.stats.fragments_total = len(ds.fragments)

        base_mask = self._index_mask()
        keep = self._prune_fragments()

        # 索引给出的掩码也要用来**跳过整个片段**。
        #
        # 只在片段内部过滤是不够的：一个 `WHERE n BETWEEN 100 AND 200`
        # 在 5 万行、25 个片段的表上只命中 1 个片段里的 101 行，
        # 但如果不跳过其它片段，扫描器仍然会把 25 个片段全打开、
        # 读 1.5 MB —— 比不建索引时靠统计剪枝还慢 60 倍。
        # "建了索引反而更慢"就是这么来的。
        if base_mask is not None:
            fi_all, _ = ds._row_map()
            for f_idx in np.flatnonzero(keep):
                rows = np.flatnonzero(fi_all == f_idx)
                if len(rows) and not base_mask[rows].any():
                    keep[f_idx] = False
                    self.stats.fragments_pruned_by_index += 1

        for f_idx, frag in enumerate(ds.fragments):
            if not keep[f_idx]:
                continue
            self.stats.fragments_scanned += 1
            batch = self._scan_fragment(f_idx, frag, base_mask)
            if batch is not None and batch.num_rows:
                self.stats.rows_after_filter += batch.num_rows
                yield batch

    # -- 剪枝 -------------------------------------------------------------
    def _prune_fragments(self) -> np.ndarray:
        """用片段统计决定"哪些片段根本不用打开"。

        没有这一步，一个"查某个 id"的查询会打开表里**所有**片段，
        每个片段至少付一次"读列元数据"的往返。有了它，
        常见查询能把片段数从几千降到几个。
        """

        ds = self.dataset
        n = len(ds.fragments)
        keep = np.ones(n, dtype=bool)
        # **先清空再累加。** 这个方法可能被调用多次
        # （``to_batches`` 一次、``explain`` 里又一次），
        # 不清空的话计数会翻倍，计划里每个片段会打印两遍 ——
        # 一个纯展示层的 bug，但它会让"剪掉了 18/10 个片段"这种
        # 明显不可能的数字出现在输出里。
        self._pruned = []
        self.stats.fragments_pruned_by_stats = 0
        self.stats.fragments_pruned_by_index = 0
        if not self.filter_sql or not self.use_zone_maps:
            return keep

        conjuncts = split_conjuncts(parse(self.filter_sql, ds.schema))
        for i, frag in enumerate(ds.fragments):
            reason = "谓词无法用统计剪枝，需扫描"
            kept = True
            for c in conjuncts:
                verdict = _stats_can_match(c, frag, ds.schema)
                if verdict is False:
                    kept = False
                    reason = f"被统计剪枝: {c.to_sql()}"
                    break
            keep[i] = kept
            self._pruned.append(FragmentPruning(frag.id, kept, reason, frag.physical_rows))
            if not kept:
                self.stats.fragments_pruned_by_stats += 1
        return keep


    # -- 索引 -------------------------------------------------------------
    def _index_mask(self) -> Optional[np.ndarray]:
        """尝试用标量索引把谓词变成行掩码；做不到返回 ``None``。

        注意**掩码是"逻辑行号"空间的**，长度 = ``count_rows()``。
        """

        ds = self.dataset
        if not self.filter_sql or not self.use_indices:
            return None
        available = [i for i in ds.indices if i.is_scalar and i.valid]
        if not available:
            return None
        expr = parse(self.filter_sql, ds.schema)
        for meta in available:
            col = meta.columns[0] if meta.columns else None
            if col is None:
                continue
            idx_expr = to_index_expr(expr, col)
            if idx_expr is None:
                continue
            try:
                index = ds.load_index(meta.name)
            except Exception:
                continue
            self.stats.index_lookups += 1
            answered = index.search(idx_expr)
            if answered is None:
                continue
            mask = index.resolve(answered, ds.num_rows)
            if mask is not None:
                return mask
        return None

    # -- 扫一个片段 -------------------------------------------------------
    def _scan_fragment(
        self, f_idx: int, frag: Any, base_mask: Optional[np.ndarray]
    ) -> Optional[RecordBatch]:
        ds = self.dataset
        needed = self._needed_columns()
        projection = self._projection()

        # 这个片段在"逻辑行号"空间里占据哪一段
        fi, offs = ds._row_map()
        positions = np.flatnonzero(fi == f_idx)
        if len(positions) == 0:
            return None
        logical_rows = positions  # 已经按物理顺序排好
        phys_offsets = offs[positions]

        # 索引掩码先作用一次（在逻辑行号空间）
        if base_mask is not None:
            local_keep = base_mask[logical_rows]
            if not local_keep.any():
                return None
            logical_rows = logical_rows[local_keep]
            phys_offsets = phys_offsets[local_keep]

        if self.row_range is not None:
            lo, hi = self.row_range
            sel = (phys_offsets >= lo) & (phys_offsets < hi)
            logical_rows = logical_rows[sel]
            phys_offsets = phys_offsets[sel]
            if len(logical_rows) == 0:
                return None

        # ---- 延迟物化：先只读过滤列 ----
        filter_cols = self._filter_columns()
        mask = None
        rows_for_output = phys_offsets
        logical_for_output = logical_rows

        if self.filter_sql:
            read_cols = filter_cols if (self.late_materialization and filter_cols) else needed
            probe = self._read_rows(frag, read_cols, rows_for_output)
            expr = parse(self.filter_sql, ds.schema)
            mask = evaluate_predicate(expr, probe)
            self.stats.rows_scanned += len(rows_for_output)
            if not mask.any():
                return None
            if self.late_materialization and filter_cols:
                self.stats.late_materialization_saved_rows += int(
                    (~mask).sum() * max(0, len(projection) - len(set(filter_cols) & set(projection)))
                )
            rows_for_output = rows_for_output[mask]
            logical_for_output = logical_rows[mask]
        else:
            self.stats.rows_scanned += len(rows_for_output)

        batch = self._read_rows(frag, projection, rows_for_output)
        if self.with_row_id:
            batch = batch.add_rowid(0)
            ids = np.arange(len(batch), dtype=np.uint64)
            # 用全局逻辑行号覆盖
            from .arrays import PrimitiveArray
            from .types import UINT64

            batch = batch.with_column(
                "_rowid", PrimitiveArray(UINT64, logical_for_output.astype(np.uint64))
            )
        return batch

    def _read_rows(
        self, frag: Any, columns: Sequence[str], phys_offsets: np.ndarray
    ) -> RecordBatch:
        """按片段的物理偏移读一批行（含已删除行，删除过滤在外面做）。

        有一个重要的快速路径：**偏移连续时走行范围下推**。
        扫描是按片段顺序做的，所以绝大多数情况下偏移就是一段连续区间；
        这时可以直接告诉 reader "只读第 lo~hi 行"，让它跳过整页，
        而不是"把整个片段读进来再 take"。
        这就是"行范围下推"和"物化后过滤"的差别，
        在有删除文件的表上尤其明显。
        """

        ds = self.dataset
        if len(phys_offsets) == 0:
            return RecordBatch.empty(Schema([ds.schema[c] for c in columns]))

        offsets = phys_offsets.astype(np.int64)
        contiguous = bool(
            len(offsets) == 1 or np.all(np.diff(offsets) == 1)
        )
        if contiguous:
            lo = int(offsets[0])
            hi = int(offsets[-1]) + 1
            return ds.read_fragment(
                frag, columns=list(columns), row_range=(lo, hi), include_deleted=True
            )
        batch = ds.read_fragment(frag, columns=list(columns), include_deleted=True)
        return batch.take(offsets)

    # -- 结果 -------------------------------------------------------------
    def to_batch(self) -> RecordBatch:
        batches = list(self.to_batches())
        if not batches:
            return RecordBatch.empty(self.schema)
        return RecordBatch.concat(batches) if len(batches) > 1 else batches[0]

    def to_pylist(self) -> List[Dict[str, Any]]:
        return self.to_batch().to_pylist()

    def to_arrow(self) -> Any:
        return self.to_batch().to_arrow()

    def count_rows(self) -> int:
        """只数行数，不取数据。

        真实 Lance 在这里能做真正的"零读统计"（片段行数在 manifest 里），
        没有谓词时一次 I/O 都不用发。这里给同样的快路径。
        """

        if not self.filter_sql and self.row_range is None:
            if self.limit is not None:
                return min(self.limit, self.dataset.num_rows)
            return self.dataset.num_rows
        if not self.filter_sql and self.row_range is not None:
            lo, hi = self.row_range
            return max(0, min(hi, self.dataset.physical_rows) - lo)
        return sum(b.num_rows for b in self.to_batches())

    # -- 解释 -------------------------------------------------------------
    def explain(self, analyze: bool = False) -> str:
        if analyze:
            list(self.to_batches())
        plan = self.build_plan()
        out = [plan.render()]
        if analyze:
            out.append("")
            out.append("--- 实测 I/O ---")
            out.append(self.stats.report())
        return "\n".join(out)

    def build_plan(self) -> ScanPlan:
        ds = self.dataset
        steps: List[str] = []
        steps.append("LanceRead: 片段级列存扫描")
        steps.append("  ProjectionPushdown: 只读 " + ", ".join(sorted(set(self._needed_columns()))))
        if self.filter_sql:
            steps.append(f"  FilterExec: {self.filter_sql}")
            steps.append(
                "  PredicatePushdown: "
                + ("列级 min/max 剪枝已启用" if self.use_zone_maps else "已关闭")
            )
        if self.use_indices and ds.indices:
            for i in ds.indices:
                if i.is_scalar and i.valid:
                    steps.append(f"  ScalarIndexQuery: {i.name} ({i.index_type}) on {i.columns}")
        if any(i.is_vector for i in ds.indices):
            for i in ds.indices:
                if i.is_vector and i.valid:
                    steps.append(
                        f"  ANNIvfPartition: {i.name} ({i.index_type}, nprobes 可调)"
                    )
        if self.late_materialization:
            steps.append("  LateMaterialization: 先算掩码，再取投影列")
        if self.limit is not None:
            steps.append(f"  LimitExec: {self.limit}")
        plan = ScanPlan(
            dataset_uri=ds.uri,
            dataset_version=ds.version,
            projection=self._projection(),
            filter_sql=self.filter_sql,
            limit=self.limit,
            steps=steps,
        )
        # 剪枝信息在 _prune_fragments 里填充；这里为了 explain 单独跑一遍
        if self._pruned:
            # 已经扫过一次了，直接复用 —— 不要重跑 _prune_fragments()，
            # 那会把计数加到已经算好的数字上。
            plan.pruned = list(self._pruned)
        elif self.filter_sql and self.use_zone_maps:
            self._prune_fragments()
            plan.pruned = list(self._pruned)
        else:
            plan.pruned = [
                FragmentPruning(f.id, True, "无谓词，全扫", f.physical_rows)
                for f in ds.fragments
            ]
        return plan

    def analyze(self) -> Dict[str, Any]:
        list(self.to_batches())
        return self.stats.summary()


# ---------------------------------------------------------------------------
# 统计剪枝
# ---------------------------------------------------------------------------


def _stats_can_match(expr: Any, frag: Any, schema: Schema) -> Optional[bool]:
    """根据片段里保存的列统计判断"这个片段可能有行满足谓词吗"。

    返回 ``True``（可能）、``False``（不可能）、``None``（不知道）。
    **只有 ``False`` 才是安全的**：统计只覆盖"有数据的行"，
    而"列在这个片段里没有数据"（后加的列）意味着全是 null ——
    此时任何比较都是 null，都不会命中，所以可以放心剪。
    """

    from .expr import Between, BinaryOp, ColumnRef, IsIn, IsNull, Literal

    if isinstance(expr, BinaryOp) and expr.op in ("AND",):
        results = [_stats_can_match(expr.left, frag, schema), _stats_can_match(expr.right, frag, schema)]
        if False in results:
            return False
        return True if True in results else None

    if isinstance(expr, BinaryOp) and expr.op in ("OR",):
        results = [_stats_can_match(expr.left, frag, schema), _stats_can_match(expr.right, frag, schema)]
        if True in results:
            return True
        if results == [False, False]:
            return False
        return None

    if isinstance(expr, BinaryOp) and expr.op in ("=", "<", "<=", ">", ">=") and isinstance(
        expr.left, ColumnRef
    ) and isinstance(expr.right, Literal):
        stats = _fragment_column_stats(frag, expr.left.name, schema)
        if stats is None:
            return None
        value = _literal_value(expr.right)
        if value is None:
            return None
        return _range_might_match(expr.op, value, stats)

    if isinstance(expr, Between) and isinstance(expr.child, ColumnRef):
        stats = _fragment_column_stats(frag, expr.child.name, schema)
        if stats is None:
            return None
        lo, hi = stats.get("min"), stats.get("max")
        if lo is None or hi is None:
            return False
        # **必须取字面量的值，不能直接比较 AST 节点。**
        # ``Expr.__lt__`` 被重载成"构造一个比较节点"，所以
        # ``expr.high < lo`` 永远返回一个对象、永远为真 ——
        # 于是每个片段都被判成"不可能命中"，查询静默返回 0 行。
        # 这个 bug 只影响 BETWEEN（其它谓词走的是 Literal 分支，
        # 早就取过 .value），而且只在开启统计剪枝时出现，
        # 所以关掉 pruning 一测就"好了"，很容易被误判成随机问题。
        low = _literal_value(expr.low)
        high = _literal_value(expr.high)
        if low is None or high is None:
            return None
        try:
            return not (high < lo or low > hi)
        except TypeError:
            return None

    if isinstance(expr, IsIn) and isinstance(expr.child, ColumnRef):
        stats = _fragment_column_stats(frag, expr.child.name, schema)
        if stats is None:
            return None
        return True  # 小集合不值得再细算

    if isinstance(expr, IsNull):
        stats = _fragment_column_stats(frag, expr.child.name, schema)
        if stats is None:
            return None
        if expr.negated:
            return True
        return (stats.get("null_count") or 0) > 0

    return None


def _literal_value(node: Any) -> Any:
    """从一个 AST 节点里取出字面量的 Python 值。

    单独抽出来是因为这个"取错东西"的模式会反复出现：
    ``Expr`` 重载了比较运算符，拿节点去和数字比**永远不会报错**，
    只会给出一个恒真的结果。任何要把谓词和统计做比较的地方
    都必须先解出真正的值。
    """

    if node is None:
        return None
    value = getattr(node, "value", None)
    if value is not None:
        return value
    # 兼容 ``-1`` 这种被解析成 ``UnaryOp("-", Literal(1))`` 的写法
    op = getattr(node, "op", None)
    operand = getattr(node, "operand", None)
    if op in ("-", "+") and operand is not None:
        inner = _literal_value(operand)
        if inner is None:
            return None
        return -inner if op == "-" else inner
    return None


def _range_might_match(op: str, value: Any, stats: Mapping[str, Any]) -> Optional[bool]:
    lo, hi = stats.get("min"), stats.get("max")
    if lo is None or hi is None:
        # 没有统计（例如字符串列被截断了）→ 无法判断
        return None
    try:
        if op == "=":
            return not (value < lo or value > hi)
        if op == "<":
            return lo < value
        if op == "<=":
            return lo <= value
        if op == ">":
            return hi > value
        if op == ">=":
            return hi >= value
    except TypeError:
        return None
    return None


def _fragment_column_stats(frag: Any, column: str, schema: Schema) -> Optional[Mapping[str, Any]]:
    fid = schema[column].field_id if column in schema else None
    if fid is None:
        return None
    for f in frag.files:
        if fid in f.fields and column in (f.column_stats or {}):
            return f.column_stats[column]
    # 这一列在这个片段里没有文件 → 全是 null
    return {"min": None, "max": None, "null_count": frag.physical_rows}


def count_rows_matching(dataset: Any, predicate: str) -> int:
    """便捷函数：``WHERE`` 命中的行数。"""

    return Scanner(dataset, columns=[], filter=predicate).count_rows()
