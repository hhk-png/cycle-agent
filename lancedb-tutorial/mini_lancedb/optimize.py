"""维护（maintenance）—— 压实、清理旧版本、重建索引。

**为什么"追加得很快"的系统都需要一个维护动作？**

因为 Lance 的写路径全是"只追加、不修改"：每次 ``add`` 写一个新片段、
每次 ``delete`` 写一个新删除文件、每次 ``update`` 写一段删除 + 一个新片段。
这个设计的收益巨大（写入无锁、历史可重放、读者永不看到半成品），
代价是**三类垃圾会持续累积**：

1. **小片段**：每次 ``add`` 一行也产生一个片段。片段多了之后，
   查一次要打开 N 个文件 —— manifest 越来越大，"打开表"本身变慢，
   而且**每次查询的固定开销 = 片段数 × 元数据读**；
2. **删除文件**：删掉的行仍然占着物理位置，读的时候要额外读一遍
   删除文件并做掩码。删除比例到 50% 时，读放大就是 2 倍；
3. **旧版本**：``overwrite`` 和 ``update`` 都不删老文件（这正是
   "时间旅行"能工作的原因），所以磁盘占用是"当前数据 + 所有历史数据"。

**什么时候该跑 ``optimize()``？** 官方文档给的启发式是：

    * 累计 **~100,000 行变更**，或者
    * 累计 **20 次修改操作**

**以先到者为准**。这两个数字是两个不同的瓶颈：前者防的是"每次改几行
但改了几十万次"（删除文件碎片化），后者防的是"改得少但改得很勤"
（manifest 版本数爆炸）。所以判断条件是 ``or`` 不是 ``and``。

⚠️ **``optimize()`` 是参照 VACUUM 建模的，它可能临时把磁盘占用顶上去。**
压实要把旧片段读出来、写成新片段，**在新片段落盘并且新 manifest 提交之前，
旧文件都不能删**。所以一个 10 GB 的表在最坏情况下会短暂占 20 GB。
在容量紧张的环境里，这不是理论问题 —— 它是"维护脚本跑挂了整个集群"
的常见原因之一。顺序应该是：先 ``cleanup_old_versions`` 腾出空间，
再 ``compact_files``。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

from .errors import QueryError

__all__ = [
    "OptimizeStats",
    "CompactionStats",
    "CleanupStats",
    "optimize",
    "compact_files",
    "cleanup_old_versions",
    "should_optimize",
    "OPTIMIZE_ROW_CHANGE_THRESHOLD",
    "OPTIMIZE_OPERATION_THRESHOLD",
]

#: 官方文档给的触发启发式：累计这么多行变更就该维护了。
OPTIMIZE_ROW_CHANGE_THRESHOLD = 100_000
#: 官方文档给的触发启发式：累计这么多次修改操作就该维护了。
OPTIMIZE_OPERATION_THRESHOLD = 20


@dataclass
class CompactionStats:
    """一次压实的账本。**每个数字都是实测的，不是估算的。**"""

    fragments_removed: int = 0
    fragments_added: int = 0
    files_removed: int = 0
    files_added: int = 0
    bytes_removed: int = 0
    bytes_added: int = 0
    rows_rewritten: int = 0
    num_deleted_rows_before: int = 0
    num_deleted_rows_after: int = 0
    compacted: bool = False

    @property
    def net_bytes(self) -> int:
        """净增字节。**压实做完之后通常应该 ≤ 0**（把删除真正落地了）。

        它是负的时候不代表"省了空间"就一定划算 —— 压实花掉的
        CPU 和临时空间没记在这个账里。
        """

        return int(self.bytes_added - self.bytes_removed)

    def report(self) -> str:
        rows = [
            ("压实是否发生", "是" if self.compacted else "否（没有需要压实的片段）"),
            ("片段 移除 / 新增", f"{self.fragments_removed} / {self.fragments_added}"),
            ("文件 移除 / 新增", f"{self.files_removed} / {self.files_added}"),
            ("字节 移除 / 新增", f"{self.bytes_removed} / {self.bytes_added}"),
            ("净字节变化", f"{self.net_bytes}"),
            ("重写行数", str(self.rows_rewritten)),
            (
                "删除行数 前 / 后",
                f"{self.num_deleted_rows_before} / {self.num_deleted_rows_after}",
            ),
        ]
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


@dataclass
class CleanupStats:
    """一次旧版本清理的账本。"""

    old_versions_removed: int = 0
    old_versions_removable: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
    kept_versions: List[int] = dc_field(default_factory=list)
    dry_run: bool = False
    cleaned: bool = False
    reason: str = ""

    def report(self) -> str:
        rows = [
            ("是否真的删除", "否（dry run）" if self.dry_run else ("是" if self.cleaned else "否")),
            ("可回收 / 已回收的版本", f"{self.old_versions_removable} / {self.old_versions_removed}"),
            ("删除的文件数", str(self.files_removed)),
            ("删除的字节数", str(self.bytes_removed)),
            ("保留的版本", ", ".join(f"v{v}" for v in self.kept_versions) or "-"),
        ]
        if self.reason:
            rows.append(("说明", self.reason))
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


@dataclass
class OptimizeStats:
    """``optimize()`` 的总账本。

    真实 LanceDB 的 ``optimize()`` **返回 ``None``**（尽管类型标注写着
    ``OptimizeStats``，实测就是 ``None``）—— 想知道发生了什么，
    得自己去调 ``compact_files()`` / ``cleanup_old_versions()``。
    **本实现刻意返回一个真对象**：维护是个有代价的动作，
    "它到底做了什么"必须是可断言、可打日志的，而不是"没报错就等于成功"。
    """

    compaction: CompactionStats = dc_field(default_factory=CompactionStats)
    cleanup: Optional[CleanupStats] = None
    indices_rebuilt: List[str] = dc_field(default_factory=list)
    version_before: int = 0
    version_after: int = 0

    @property
    def fragments_removed(self) -> int:
        return self.compaction.fragments_removed

    @property
    def fragments_added(self) -> int:
        return self.compaction.fragments_added

    @property
    def files_removed(self) -> int:
        return self.compaction.files_removed + (
            self.cleanup.files_removed if self.cleanup else 0
        )

    @property
    def files_added(self) -> int:
        return self.compaction.files_added

    @property
    def bytes_removed(self) -> int:
        return self.compaction.bytes_removed + (
            self.cleanup.bytes_removed if self.cleanup else 0
        )

    @property
    def bytes_added(self) -> int:
        return self.compaction.bytes_added

    @property
    def old_versions_removed(self) -> int:
        return self.cleanup.old_versions_removed if self.cleanup else 0

    def report(self) -> str:
        """渲染成一张对齐的纯文本表（**不是 markdown**）。

        用纯文本而不是 markdown，是为了 ``print(table.stats().report())``
        在终端里直接可读 —— 教程里的每段输出都是这么贴出来的。
        """

        head = (
            f"OptimizeStats  v{self.version_before} → v{self.version_after}\n"
            + "─" * 52
        )
        body = [head, "【压实】", self.compaction.report()]
        if self.cleanup is not None:
            body.append("")
            body.append("【旧版本清理】")
            body.append(self.cleanup.report())
        if self.indices_rebuilt:
            body.append("")
            body.append("【重建的索引】")
            body.append(", ".join(self.indices_rebuilt))
        return "\n".join(body)


# ---------------------------------------------------------------------------
# 目标解析
# ---------------------------------------------------------------------------


def _dataset_of(target: Any) -> Any:
    """``optimize()`` 的入参既可以是 ``LanceTable`` 也可以是 ``Dataset``。"""

    ds = getattr(target, "_dataset", None)
    if ds is not None and hasattr(ds, "compact"):
        return ds
    if hasattr(target, "compact") and hasattr(target, "manifest"):
        return target
    raise QueryError(
        "optimize() 需要 LanceTable 或 Dataset",
        got=type(target).__name__,
    )


def _fragment_ledger(ds: Any) -> Dict[int, Any]:
    """快照"现在有哪些片段、各多大"。

    这是把 "``compact()` 返回了一个字典" 变成
    "**每个数字都能追溯到具体的文件**" 的关键一步：拿压实前后的
    片段 id 做差集，移除和新增的字节数就是精确值，而不是估算。
    """

    return {f.id: f for f in ds.fragments}


# ---------------------------------------------------------------------------
# compact_files
# ---------------------------------------------------------------------------


def compact_files(
    target: Any,
    *,
    target_rows_per_fragment: Optional[int] = None,
    materialize_deletions: bool = True,
    min_rows_per_fragment: Optional[int] = None,
    max_fragments: Optional[int] = None,
    **kw: Any,
) -> CompactionStats:
    """把一堆小片段合并成大片段，并把删除真正落地。

    压实的收益有三个层次，从便宜到贵：

    1. **元数据变小** —— manifest 要记住每个片段。几十万个小片段会让
       "打开表"这个动作本身变慢，而且每次查询都要读一遍这个列表；
    2. **读放大变小** —— 查一次要打开 N 个文件，N 小一个数量级，
       I/O 次数就少一个数量级。这一层在对象存储上尤其致命
       （每次 "打开文件" 是一次网络往返）；
    3. **删除被落地** —— 把删除文件里标记的行真正丢掉，
       读的时候不用再逐个掩码。删除比例高时这一层的收益最大。

    ⚠️ **代价是一次完整重写**，而且重写期间新旧文件共存
    （见模块文档里关于临时磁盘占用的说明）。

    Args:
        target:
            表或 dataset。
        target_rows_per_fragment:
            目标片段行数。默认 1,048,576（对应 Lance 的默认 1M 行/文件）。
        materialize_deletions:
            是否顺便把删除落地。关掉它就只能合并小文件，
            删除造成的读放大依然存在。
        max_fragments:
            这次最多合并多少个片段。**分批压实**在线上很有用：
            一次压实整个表会造成一个巨大的 I/O 尖峰，
            分批做能让它对前台流量"不可见"。

    Returns:
        :class:`CompactionStats`。
    """

    from .dataset import DEFAULT_TARGET_ROWS_PER_FRAGMENT, MIN_ROWS_PER_FRAGMENT

    ds = _dataset_of(target)
    before = _fragment_ledger(ds)
    before_deleted = ds.num_deleted_rows

    kwargs: Dict[str, Any] = {
        "target_rows_per_fragment": int(
            target_rows_per_fragment or DEFAULT_TARGET_ROWS_PER_FRAGMENT
        ),
        "materialize_deletions": bool(materialize_deletions),
        "min_rows_per_fragment": int(min_rows_per_fragment or MIN_ROWS_PER_FRAGMENT),
    }
    if max_fragments is not None:
        kwargs["max_fragments"] = int(max_fragments)

    result = ds.compact(**kwargs)
    after = _fragment_ledger(ds)

    removed_ids = set(before) - set(after)
    added_ids = set(after) - set(before)
    stats = CompactionStats(
        fragments_removed=len(removed_ids),
        fragments_added=len(added_ids),
        files_removed=sum(len(before[i].files) for i in removed_ids),
        files_added=sum(len(after[i].files) for i in added_ids),
        bytes_removed=sum(before[i].size_bytes for i in removed_ids),
        bytes_added=sum(after[i].size_bytes for i in added_ids),
        rows_rewritten=int(result.get("rows_rewritten", 0)),
        num_deleted_rows_before=int(before_deleted),
        num_deleted_rows_after=int(ds.num_deleted_rows),
        compacted=bool(result.get("compacted", False)),
    )
    return stats


# ---------------------------------------------------------------------------
# cleanup_old_versions
# ---------------------------------------------------------------------------


def cleanup_old_versions(
    target: Any,
    *,
    older_than: Any = None,
    delete_unverified: bool = False,
    dry_run: bool = False,
    keep_versions: int = 1,
    **kw: Any,
) -> CleanupStats:
    """删除不再被引用的旧版本文件。

    **真正的危险不是"删错文件"，而是"删掉别人正在读的文件"。**
    一个长事务可能正在读 v3，这时把 v3 的数据文件删掉，
    它就会读到半个表 —— 而且不会报"文件不存在"，因为数据文件
    还在被 manifest 引用；它报的是更难查的"某列数据不对"。

    所以安全策略只能是**时间**：``older_than`` 之内的一律不删。
    真实 LanceDB 的默认值就是这个思路（它不是"删得越多越好"，
    而是"删得足够保守"）。

    Parameters
    ----------
    older_than :
        只删**早于这么久之前**的版本。接受 ``timedelta`` 或秒数。
        传 ``None`` 表示不设时间限制（危险，只在确认没有长读者时用）。
    dry_run :
        ``True`` 时**只统计、不删除**，返回可回收的字节数。
        真实 LanceDB 的 ``cleanup_old_versions`` **没有** ``dry_run``
        参数；本实现提供它是为了教学（也为了线上先看一眼再动手）。
    delete_unverified :
        **本实现与真实 LanceDB 的语义差异（重要）**：在真实 LanceDB 里
        它的意思是"连没被验证过的版本也删"；在本实现的 dataset 层，
        它被用作"真的删还是只报告"的开关。这里的映射是
        ``真的删 = (not dry_run) or delete_unverified``，
        于是两种写法都得到符合直觉的结果。
    keep_versions :
        至少保留最近这么多个版本（不管它们有多老）。

    Returns:
        :class:`CleanupStats`。
    """

    ds = _dataset_of(target)
    seconds = _as_seconds(older_than)
    do_delete = (not dry_run) or bool(delete_unverified)

    result = ds.cleanup_old_versions(
        older_than=seconds,
        keep_versions=int(keep_versions),
        delete_unverified=do_delete,
    )

    stats = CleanupStats(kept_versions=list(result.get("kept_versions", [])))
    if result.get("cleaned"):
        stats.cleaned = True
        stats.dry_run = False
        stats.old_versions_removed = len(result.get("removed_versions", []))
        stats.old_versions_removable = stats.old_versions_removed
        stats.files_removed = int(result.get("removed_files", 0))
        stats.bytes_removed = int(result.get("bytes_freed", 0))
        return stats

    stats.dry_run = bool(result.get("dry_run", False))
    stats.reason = str(result.get("reason", ""))
    removable = list(result.get("removable_versions", []))
    stats.old_versions_removable = len(removable)
    stats.files_removed = int(result.get("removable_files", 0))
    stats.bytes_removed = int(result.get("bytes_reclaimable", 0))
    if not stats.reason and stats.old_versions_removable == 0:
        stats.reason = "没有可回收的旧版本（都被 keep_versions / older_than 保护着）"
    return stats


def _as_seconds(value: Any) -> Optional[float]:
    """``timedelta`` / 秒数 / ``None`` → 秒数。"""

    if value is None:
        return None
    total = getattr(value, "total_seconds", None)
    if callable(total):
        return float(total())
    return float(value)


# ---------------------------------------------------------------------------
# optimize
# ---------------------------------------------------------------------------


def optimize(
    table_or_dataset: Any,
    *,
    cleanup_older_than: Any = None,
    delete_unverified: bool = False,
    retrain: bool = False,
    **kw: Any,
) -> OptimizeStats:
    """一次做完该做的维护：压实 →（可选）清理旧版本 →（可选）重建索引。

    参数名和真实 LanceDB 对齐：``cleanup_older_than`` / ``delete_unverified``
    / ``retrain``。``cleanup_older_than=None`` 表示**不做旧版本清理**
    （这是刻意的：清理是有破坏性的，不该在默认路径上发生）。

    ``retrain=True`` 会重建所有**失效**的向量索引。压实会把行号全部打乱，
    于是老索引里的行号指向别的行了（不是"少了几行"，是**指错行**），
    所以 dataset 层会把它们标成 ``valid=False`` 而不是硬删。
    重建它们就是把 ``retrain`` 打开的理由。

    ⚠️ 顺序很重要：**先清理再压实**最省空间，但本实现按真实 LanceDB 的
    顺序做（压实 → 清理），因为"压实失败时至少旧版本还在"更安全。
    容量紧张时请手动先调 :func:`cleanup_old_versions`。
    """

    ds = _dataset_of(table_or_dataset)
    stats = OptimizeStats(version_before=ds.version)

    # 1) 压实
    stats.compaction = compact_files(ds, **kw)

    # 2) 重建失效的索引（可选）
    if retrain:
        stats.indices_rebuilt = _retrain_indices(ds)

    # 3) 清理旧版本（可选）
    if cleanup_older_than is not None or delete_unverified:
        stats.cleanup = cleanup_old_versions(
            ds,
            older_than=cleanup_older_than,
            delete_unverified=delete_unverified,
        )

    stats.version_after = ds.version
    return stats


def _retrain_indices(ds: Any) -> List[str]:
    """重建所有 ``valid=False`` 的向量索引。

    **为什么不自动重建？** 建索引是这里最贵的操作（要跑 k-means、
    量化、写文件），把它塞进 ``compact_files`` 会让"合并几个小文件"
    变成一个几十分钟的任务。所以默认不重建，只标记失效，
    由 ``retrain=True`` 或用户显式重建。
    """

    rebuilt: List[str] = []
    for meta in list(ds.indices):
        if meta.valid or not meta.is_vector:
            continue
        column = meta.columns[0] if meta.columns else None
        if column is None:
            continue
        params = {
            k: v
            for k, v in dict(meta.params).items()
            if k not in ("distance_type",)
        }
        ds.create_index(
            column,
            index_type=meta.index_type,
            name=meta.name,
            metric=meta.distance_type or "l2",
            replace=True,
            **params,
        )
        rebuilt.append(meta.name)
    return rebuilt


# ---------------------------------------------------------------------------
# 该不该维护
# ---------------------------------------------------------------------------


def should_optimize(
    dataset: Any,
    *,
    row_change_threshold: int = OPTIMIZE_ROW_CHANGE_THRESHOLD,
    operation_threshold: int = OPTIMIZE_OPERATION_THRESHOLD,
) -> Dict[str, Any]:
    """按官方文档的启发式判断"该不该跑了"。

    判据是两个计数器的**或**（不是与）：

    * ``dirty_rows`` —— 累计被删除 / 被重写的行数。它衡量的是
      "删除文件和重复数据有多少"，对应读放大；
    * ``operations`` —— 版本数（每次写都是一个新版本，含删除、
      更新、建索引）。它衡量的是"manifest 列表有多长"，
      对应"打开表有多慢"。

    一个只改一行的脚本连跑十万次：``dirty_rows`` 很小，
    ``operations`` 爆表 —— 这时该压实的是**版本历史**，不是片段。
    反过来，一次删掉一半行：``operations`` 只有 1，``dirty_rows`` 巨大 ——
    这时该落地删除。两个阈值就是这么分工的。
    """

    num_deleted = int(getattr(dataset, "num_deleted_rows", 0))
    physical = int(getattr(dataset, "physical_rows", 0))
    num_rows = int(getattr(dataset, "num_rows", 0))
    dirty_rows = max(num_deleted, physical - num_rows)
    operations = int(getattr(dataset, "version", 0))

    small = 0
    nfrag = 0
    for f in getattr(dataset, "fragments", []):
        nfrag += 1
        if f.num_rows < 1024:
            small += 1

    return {
        "dirty_rows": dirty_rows,
        "row_change_threshold": int(row_change_threshold),
        "operations": operations,
        "operation_threshold": int(operation_threshold),
        "num_fragments": nfrag,
        "num_small_fragments": small,
        "should_optimize": bool(
            dirty_rows >= row_change_threshold or operations >= operation_threshold
        ),
        "reason": _why(
            dirty_rows, operations, small, row_change_threshold, operation_threshold
        ),
    }


def _why(
    dirty_rows: int, operations: int, small: int, rt: int, ot: int
) -> str:
    reasons = []
    if dirty_rows >= rt:
        reasons.append(f"累计行变更 {dirty_rows} ≥ {rt}")
    if operations >= ot:
        reasons.append(f"版本数 {operations} ≥ {ot}")
    if small:
        reasons.append(f"有 {small} 个 <1024 行的小片段")
    if not reasons:
        reasons.append("还没到官方文档给的触发线，但小片段多时也值得压实")
    return "；".join(reasons)
