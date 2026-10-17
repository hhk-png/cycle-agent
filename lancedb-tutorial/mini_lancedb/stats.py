"""表统计 —— "这张表现在有多健康"的体检报告。

为什么值得单独一个模块？因为在列存 + 追加写的系统里，
**"能查"和"查得快"是两件事**，而两者之间的差距全部体现在几个数字上：

.. list-table::
   :header-rows: 1

   * - 症状
     - 数字告诉你什么
     - 药方
   * - 打开表变慢
     - ``num_fragments`` 很大（几万以上）
     - ``compact_files``
   * - 扫描读了很多用不上的行
     - ``deleted_rows_ratio`` 高
     - ``compact_files`` 把删除落地
   * - 磁盘占用远超数据量
     - ``total_bytes / bytes_per_row`` 对不上预期
     - ``cleanup_old_versions``
   * - 某几列特别贵
     - ``bytes_per_column`` 高度倾斜
     - 改类型（字典编码 / 降精度）或干脆别存
   * - 过滤后还是读到很多 null
     - ``null_counts`` 高
     - 后加的列在老片段里全是 null，是预期的

**一个必须说清楚的代价**：``bytes_per_column`` 需要读每个数据文件的
**页表**（footer），这是**真实的 I/O**（每个文件一次 range read）。
所以 :func:`collect_table_statistics` 有 ``sample_fragments`` 参数：
默认只抽样前 8 个片段，把字节数按比例外推。要精确值就把
它设成 ``None``，但要接受"统计本身可能比查询还贵"这件事。
真实 LanceDB 的 ``table.stats()`` 走的是 Lance 的
``DatasetStatistics``，那里面的列级大小来自文件 footer 里的缓存，
不需要额外读 —— 本实现没有那份缓存，所以必须显式说明这个差异。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = ["TableStatistics", "FragmentStats", "collect_table_statistics", "index_stats"]

#: 默认抽样多少个片段来估列级字节数（``None`` = 全量、精确、但慢）。
DEFAULT_COLUMN_SAMPLE = 8

#: 列级字节分布直方图的默认分桶数。
DEFAULT_HISTOGRAM_BINS = 8


@dataclass
class FragmentStats:
    """片段大小分布 —— 片段是"读放大"的计量单位。"""

    num_fragments: int = 0
    min_rows: int = 0
    median_rows: float = 0.0
    max_rows: int = 0
    mean_rows: float = 0.0
    total_physical_rows: int = 0
    histogram: Dict[str, int] = dc_field(default_factory=dict)
    num_small_fragments: int = 0

    def describe(self) -> str:
        if not self.num_fragments:
            return "（无片段）"
        return (
            f"{self.num_fragments} 个片段，行数 min/median/max = "
            f"{self.min_rows}/{self.median_rows:.0f}/{self.max_rows}"
        )


@dataclass
class TableStatistics:
    """一张表的体检报告。"""

    uri: str = ""
    version: int = 0
    num_rows: int = 0
    physical_rows: int = 0
    num_deleted_rows: int = 0
    num_columns: int = 0
    fragments: FragmentStats = dc_field(default_factory=FragmentStats)
    total_bytes: int = 0
    bytes_per_row: float = 0.0
    #: ``{列名: 字节数}``
    bytes_per_column: Dict[str, int] = dc_field(default_factory=dict)
    #: 列级字节数是抽样外推的（不是精确值）时为 True
    bytes_per_column_sampled: bool = False
    #: ``{列名: null 个数}``
    null_counts: Dict[str, int] = dc_field(default_factory=dict)
    #: 每个索引一行摘要
    indices: List[Dict[str, Any]] = dc_field(default_factory=list)
    num_versions: int = 0
    extra: Dict[str, Any] = dc_field(default_factory=dict)

    # -- 便利属性 ---------------------------------------------------------
    @property
    def deleted_rows_ratio(self) -> float:
        """删除行占比。分母是**物理**行数（含已删除的行）。

        它是"读放大"的直接度量：0.5 意味着扫一遍表要读两倍的行
        才能凑出实际的数据量。真实 LanceDB 的 ``TableStatistics``
        里没有这一项，是我们补的 —— 因为它是最值得盯的一个数字。
        """

        if not self.physical_rows:
            return 0.0
        return self.num_deleted_rows / self.physical_rows

    @property
    def num_fragments(self) -> int:
        return self.fragments.num_fragments

    # -- 渲染 -------------------------------------------------------------
    def report(self) -> str:
        """渲染成**对齐的纯文本表**（不是 markdown）。

        用 ``str.ljust`` 手工对齐而不是 pandas / tabulate：
        这一层的输出会出现在教程的每一个"看看现在什么样"小节里，
        不能因为环境里没装 pandas 就退化。
        """

        out: List[str] = []
        out.append("TableStatistics")
        out.append("=" * 58)
        out.extend(_kv_rows([
            ("uri", self.uri),
            ("version", f"v{self.version}（共 {self.num_versions} 个历史版本）"),
            ("行数", f"{self.num_rows}"),
            ("物理行数", f"{self.physical_rows}"),
            ("已删除行", f"{self.num_deleted_rows}  （{self.deleted_rows_ratio:.1%}）"),
            ("列数", f"{self.num_columns}"),
            ("片段数", self.fragments.describe()),
            ("<1024 行的小片段", str(self.fragments.num_small_fragments)),
            ("数据字节", f"{self.total_bytes}  （{self.bytes_per_row:.1f} 字节/行）"),
        ]))
        out.append("")
        out.append("片段行数分布")
        out.append("-" * 58)
        if self.fragments.histogram:
            width = max(len(k) for k in self.fragments.histogram)
            for bucket, count in self.fragments.histogram.items():
                bar = "█" * min(count, 40)
                out.append(f"{bucket:>{width}}  {count:>6}  {bar}")
        else:
            out.append("（无片段）")

        out.append("")
        out.append("列级字节 / null")
        out.append("-" * 58)
        out.append(f"{'列':<24}{'字节':>12}{'占比':>8}{'null':>10}")
        total_col = sum(self.bytes_per_column.values()) or 1
        for name, nbytes in sorted(
            self.bytes_per_column.items(), key=lambda kv: -kv[1]
        ):
            share = nbytes / total_col
            out.append(
                f"{name:<24}{nbytes:>12}{share:>7.1%}{self.null_counts.get(name, 0):>10}"
            )
        if self.bytes_per_column_sampled:
            out.append("（列级字节数是抽样外推值，非精确值）")

        out.append("")
        out.append("索引")
        out.append("-" * 58)
        if self.indices:
            for idx in self.indices:
                out.append(
                    f"{idx['name']:<20}{idx['index_type']:<12}"
                    f"列={','.join(idx.get('columns') or [])} "
                    f"覆盖={idx.get('num_indexed_rows')} 行 "
                    f"{idx.get('size_bytes')} B"
                    f"{'（已失效）' if not idx.get('valid', True) else ''}"
                )
        else:
            out.append("（无索引）")
        return "\n".join(out)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uri": self.uri,
            "version": self.version,
            "num_rows": self.num_rows,
            "physical_rows": self.physical_rows,
            "num_deleted_rows": self.num_deleted_rows,
            "deleted_rows_ratio": self.deleted_rows_ratio,
            "num_columns": self.num_columns,
            "num_fragments": self.fragments.num_fragments,
            "fragment_rows": {
                "min": self.fragments.min_rows,
                "median": self.fragments.median_rows,
                "max": self.fragments.max_rows,
                "mean": self.fragments.mean_rows,
            },
            "fragment_histogram": dict(self.fragments.histogram),
            "num_small_fragments": self.fragments.num_small_fragments,
            "total_bytes": self.total_bytes,
            "bytes_per_row": self.bytes_per_row,
            "bytes_per_column": dict(self.bytes_per_column),
            "null_counts": dict(self.null_counts),
            "indices": list(self.indices),
            "num_versions": self.num_versions,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"TableStatistics(rows={self.num_rows}, fragments={self.num_fragments}, "
            f"bytes={self.total_bytes})"
        )


def _kv_rows(rows: Sequence[Any]) -> List[str]:
    width = max(len(k) for k, _ in rows)
    return [f"{k:<{width}}  {v}" for k, v in rows]


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def collect_table_statistics(
    table_or_dataset: Any,
    *,
    sample_fragments: Optional[int] = DEFAULT_COLUMN_SAMPLE,
    histogram_bins: int = DEFAULT_HISTOGRAM_BINS,
) -> TableStatistics:
    """把一张表的"体检数字"全部采出来。

    Args:
        table_or_dataset:
            ``LanceTable`` 或 ``Dataset``。
        sample_fragments:
            估列级字节数时抽样多少个数据文件；``None`` 表示全量（精确但慢）。
            **抽样是按"片段数"抽的，不是按行抽的**，所以片段大小不均时
            外推值会偏；均匀写入的表上它相当准。
        histogram_bins:
            片段行数直方图的分桶数。

    Returns:
        :class:`TableStatistics`（自带 :meth:`TableStatistics.report`）。
    """

    ds = _dataset_of(table_or_dataset)
    stats = TableStatistics(
        uri=str(getattr(ds, "uri", "")),
        version=int(getattr(ds, "version", 0)),
        num_rows=int(getattr(ds, "num_rows", 0)),
        physical_rows=int(getattr(ds, "physical_rows", 0)),
        num_deleted_rows=int(getattr(ds, "num_deleted_rows", 0)),
        num_columns=len(getattr(ds, "schema", []) or []),
    )

    fragments = list(getattr(ds, "fragments", []) or [])
    stats.fragments = _fragment_stats(fragments, bins=histogram_bins)
    stats.total_bytes = sum(f.size_bytes for f in fragments)
    stats.bytes_per_row = (
        round(stats.total_bytes / stats.num_rows, 2) if stats.num_rows else 0.0
    )

    # -- 列级字节（需要读 footer，是真实 I/O）--
    per_col, sampled = _column_bytes(ds, fragments, sample_fragments)
    stats.bytes_per_column = per_col
    stats.bytes_per_column_sampled = sampled

    # -- 列级 null 数（**零 I/O**：统计已经抄在 manifest 里了）--
    stats.null_counts = _null_counts(ds, fragments)

    # -- 索引 --
    for meta in getattr(ds, "indices", []) or []:
        stats.indices.append(
            {
                "name": meta.name,
                "index_type": meta.index_type,
                "columns": list(meta.columns),
                "num_indexed_rows": int(meta.num_indexed_rows),
                "size_bytes": int(meta.size_bytes),
                "distance_type": meta.distance_type,
                "valid": bool(meta.valid),
            }
        )

    stats.num_versions = _count_versions(ds)
    return stats


def _dataset_of(target: Any) -> Any:
    ds = getattr(target, "_dataset", None)
    if ds is not None and hasattr(ds, "fragments"):
        return ds
    if hasattr(target, "fragments") and hasattr(target, "schema"):
        return target
    raise TypeError(f"需要 LanceTable 或 Dataset，收到 {type(target).__name__}")


def _fragment_stats(fragments: Sequence[Any], *, bins: int) -> FragmentStats:
    sizes = [int(f.num_rows) for f in fragments]
    fs = FragmentStats(
        num_fragments=len(sizes),
        total_physical_rows=sum(sizes),
        num_small_fragments=sum(1 for n in sizes if n < 1024),
    )
    if not sizes:
        return fs
    fs.min_rows = min(sizes)
    fs.max_rows = max(sizes)
    fs.median_rows = float(statistics.median(sizes))
    fs.mean_rows = round(sum(sizes) / len(sizes), 2)
    fs.histogram = _histogram(sizes, bins=max(1, int(bins)))
    return fs


def _histogram(values: Sequence[int], *, bins: int) -> Dict[str, int]:
    """把行数分成对数尺度的桶。

    **用对数桶而不是等宽桶**，因为片段大小的分布天然是长尾的：
    写入热身后可能同时存在 1 行、100 行和 100 万行的片段。
    等宽桶会把 99% 的片段挤进第一个桶里，直方图就白画了。
    """

    n = len(values)
    if n == 0:
        return {}
    lo = max(1, min(values))
    hi = max(values)
    if hi == lo:
        return {f"{lo}": n}
    edges: List[float] = []
    ratio = (hi / lo) ** (1.0 / bins)
    cur = float(lo)
    for _ in range(bins):
        edges.append(cur)
        cur *= ratio
    out: Dict[str, int] = {}
    for v in values:
        idx = 0
        for i, e in enumerate(edges):
            if v >= e:
                idx = i
        left = int(edges[idx])
        right = int(edges[idx + 1]) if idx + 1 < len(edges) else hi
        key = f"[{left}, {right})"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: _bucket_left(kv[0])))


def _bucket_left(key: str) -> int:
    try:
        return int(key.strip("[").split(",")[0])
    except Exception:  # pragma: no cover
        return 0


def _column_bytes(
    ds: Any, fragments: Sequence[Any], sample: Optional[int]
) -> tuple:
    """按列统计字节数，返回 ``({列: 字节}, 是否抽样)``。

    页表里每个页都有 ``num_bytes``，把一棵列的**子树**加起来就得到
    这一列的编码后大小（嵌套列的字节挂在子列上，所以必须往下走）。

    没有这一列的文件贡献 0 字节 —— 这正是"``add_columns`` 加列是
    零成本"的可观测证据：新列在老片段里根本不占空间。
    """

    try:
        from .format.lance_file import LanceFileReader
    except Exception:  # pragma: no cover
        return {}, False

    taken = fragments if sample is None else list(fragments)[: max(0, int(sample))]
    sampled = sample is not None and len(taken) < len(fragments)

    totals: Dict[str, int] = {}
    scale = 1.0
    if sampled and taken:
        # 按"片段数"外推：采样片段的总字节 / 全表总字节
        all_bytes = sum(f.size_bytes for f in fragments) or 1
        sampled_bytes = sum(f.size_bytes for f in taken) or 1
        scale = all_bytes / sampled_bytes

    for frag in taken:
        for data_file in frag.files:
            try:
                reader = LanceFileReader(ds.store, data_file.path)
                cols = reader.columns
            except Exception:  # pragma: no cover - 文件损坏时统计不该整个失败
                continue
            child_ids = set()
            for c in cols:
                child_ids.update(c.get("children") or [])
            for i, c in enumerate(cols):
                if i in child_ids:
                    continue
                totals[c["name"]] = totals.get(c["name"], 0) + _subtree_bytes(cols, i)

    if scale != 1.0:
        totals = {k: int(round(v * scale)) for k, v in totals.items()}
    return dict(sorted(totals.items(), key=lambda kv: -kv[1])), sampled


def _subtree_bytes(cols: Sequence[Mapping[str, Any]], index: int) -> int:
    c = cols[index]
    total = int(c.get("validity_size") or 0)
    for page in c.get("pages") or []:
        total += int(page.get("num_bytes") or 0)
    for child in c.get("children") or []:
        total += _subtree_bytes(cols, int(child))
    return total


def _null_counts(ds: Any, fragments: Sequence[Any]) -> Dict[str, int]:
    """列级 null 数。**零 I/O** —— 统计在写片段时就抄进 manifest 了。

    注意这里数的是**物理行**上的 null（含已删除的行）。
    所以删除比例高时 ``null_counts`` 看起来会偏大，
    和 ``num_rows`` 对不上是正常的。
    """

    counts: Dict[str, int] = {}
    names = list(getattr(getattr(ds, "schema", None), "names", []) or [])
    for name in names:
        counts[name] = 0
    for frag in fragments:
        present: Dict[str, bool] = {}
        for data_file in frag.files:
            for col in data_file.column_names:
                present[col] = True
            for col, st in (data_file.column_stats or {}).items():
                if col in counts:
                    counts[col] += int(st.get("null_count") or 0)
        # 这个片段里没有的列 → 全是 null（add_columns 的必然结果）
        for name in names:
            if not present.get(name):
                counts[name] = counts.get(name, 0) + int(frag.physical_rows)
    return counts


def _count_versions(ds: Any) -> int:
    try:
        return len(ds.list_versions())
    except Exception:  # pragma: no cover
        return 0


# ---------------------------------------------------------------------------
# index_stats
# ---------------------------------------------------------------------------


def index_stats(index: Any, dataset: Any) -> Optional[Dict[str, Any]]:
    """单个索引的统计，形状对齐真实 LanceDB 的 ``IndexStatistics``。

    返回的键：``index_type`` / ``distance_type`` / ``num_indexed_rows`` /
    ``num_unindexed_rows`` / ``num_indices`` / ``size_bytes`` / ``details``。

    ⚠️ **索引名不存在时返回 ``None``，不抛异常。**

    这是从真实 LanceDB 抄来的行为，而且是**有意**的：
    * 好处 —— ``if table.index_stats("vec_idx"): ...`` 可以直接写，
      不用先 ``list_indices()`` 再判断；
    * 代价 —— 它和"索引存在但没建好"无法区分，两者都是 ``None``。

    ``num_unindexed_rows`` 是**索引建好之后又写进来的行数**。
    它不为 0 不等于"索引坏了"，只意味着新数据没被索引覆盖；
    要么重建，要么依赖 delta 索引（本实现没有）。

    Args:
        index: 索引名（``str``）或 :class:`~mini_lancedb.index.base.IndexMetadata`。
        dataset: 表或 dataset。

    Returns:
        dict 或 ``None``。
    """

    ds = _dataset_of(dataset)
    meta = None
    if isinstance(index, str):
        for m in getattr(ds, "indices", []) or []:
            if m.name == index:
                meta = m
                break
    else:
        name = getattr(index, "name", None)
        for m in getattr(ds, "indices", []) or []:
            if name is not None and m.name == name:
                meta = m
                break
        if meta is None and hasattr(index, "index_type"):
            meta = index
    if meta is None:
        return None

    total_rows = int(getattr(ds, "num_rows", 0))
    indexed = int(meta.num_indexed_rows)
    return {
        "index_type": meta.index_type,
        "distance_type": meta.distance_type,
        "num_indexed_rows": indexed,
        "num_unindexed_rows": max(0, total_rows - indexed),
        "num_indices": 1,
        "size_bytes": int(meta.size_bytes),
        "details": dict(getattr(meta, "details", {}) or {}),
        "name": meta.name,
        "columns": list(getattr(meta, "columns", []) or []),
        "valid": bool(getattr(meta, "valid", True)),
    }


def format_index_stats(info: Optional[Mapping[str, Any]]) -> str:
    """把 :func:`index_stats` 的结果渲染成对齐文本（``None`` 也能渲染）。"""

    if info is None:
        return "index_stats = None\n（索引名不存在。真实 LanceDB 也是返回 None 而不是报错）"
    rows = [
        ("name", info.get("name", "-")),
        ("index_type", info.get("index_type", "-")),
        ("distance_type", info.get("distance_type", "-")),
        ("num_indexed_rows", info.get("num_indexed_rows", "-")),
        ("num_unindexed_rows", info.get("num_unindexed_rows", "-")),
        ("num_indices", info.get("num_indices", "-")),
        ("size_bytes", info.get("size_bytes", "-")),
        ("valid", info.get("valid", "-")),
    ]
    width = max(len(k) for k, _ in rows)
    lines = [f"{k:<{width}}  {v}" for k, v in rows]
    details = info.get("details") or {}
    if details:
        lines.append("details:")
        for k, v in sorted(details.items()):
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)
