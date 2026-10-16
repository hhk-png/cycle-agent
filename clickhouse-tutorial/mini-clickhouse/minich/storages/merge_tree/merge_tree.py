"""``MergeTreeData`` —— MergeTree 引擎本体。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  MergeTree 的三个支柱(记住这三条,就记住了这个引擎)                  ║
   ╚══════════════════════════════════════════════════════════════════════╝

   ① **写入永远是追加**:一次 ``INSERT`` 产出一个**不可变**的 part
      (``tmp_insert_*`` → 原子改名)。不改已有数据 → 不需要写锁、
      可以做无锁并发读。**这是后面一切性能的前提。**

   ② **part 内有序**:写 part 前按排序键排一次。于是:
      * ``primary.idx`` 能用二分定位 granule;
      * 多个 part 能用 k 路归并 O(N log k) 合并 —— 而不是重排。

   ③ **合并是后台异步的**:所以 ``SELECT`` 看到的是"当前所有 part 的并集",
      在合并收敛之前**可能看到重复行**(ReplacingMergeTree 尤其明显)。
      这一点写在 :mod:`minich.storages.merge_tree.merger` 的文档里。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  一次 INSERT 的完整生命周期                                            │
   └──────────────────────────────────────────────────────────────────────┘

     write(ctx, block)
        │
        ├─ ① 约束检查(params['constraints'] 里有 CHECK 就逐行验)
        ├─ ② 补 DEFAULT / MATERIALIZED 列
        ├─ ③ **按分区分块**:一个 block 只能属于一个分区 →
        │      按 partition 值把它切成若干子块(真实的 INSERT 也是这么做的)
        ├─ ④ 按 max_insert_block_size(**默认 1048576 行**)再切
        │      ★ 注意真实语义:超过它**只是警告**,不是错误 —— 见下面的 warning
        ├─ ⑤ "part 太多"保护:parts_to_throw_insert(**默认 3000**)
        │      单分区的活跃 part 数超过它就抛 TOO_MANY_PARTS ——
        │      这是"写入速率远超合并速率"时唯一能让系统活下来的闸门
        └─ ⑥ write_part(...) → 原子改名 → 登记进 self.parts

   .. warning::

      **``max_insert_block_size`` 的真实语义**(最容易被误记的一条):

      .. code-block:: text

         block 行数 > max_insert_block_size
            → 真实 ClickHouse: **照常写入**,但在日志里打一条
              "Splitting block of N rows into M parts" 之类的**警告**。
              它不会报错,因为"拒绝写入"对用户太不友好。

        mini-clickhouse 同样"照常写入 + 记 warning 日志",并且
        额外提供 ``strict_insert_block_size`` 开关(默认关)让它**能**
        在测试里被断言。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  一次 SELECT 的完整路径(列裁剪 + granule 裁剪 + 跳过索引)           │
   └──────────────────────────────────────────────────────────────────────┘

     read(ctx)
        │
        ├─ ① 列裁剪:只保留 SELECT/WHERE 用到的列(含谓词的依赖列)
        ├─ ② 分区裁剪:PartitionPruner 用 PARTITION BY 表达式筛掉整个分区
        ├─ ③ 对每个 part:
        │      ├─ KeyCondition.attach_part(part)
        │      ├─ mark_range = kc.get_ranges()          ← 稀疏索引二分
        │      ├─ mark_range ∩= 跳过索引筛出的区间      ← skp_idx_*.idx
        │      └─ 按行读出需要的列,切成 max_block_size 的块
        ├─ ④ 逐行过滤(WHERE 的谓词在这里落地)
        ├─ ⑤ ``FINAL``? 就地做一次归并去重(见下面 warning)
        └─ ⑥ 产出 Chunk,并累计 ProfileEvents

   .. warning::

      **``FINAL`` 会毁掉并行读。** 详见 :mod:`...merger` 的文档;
      这里只强调实现后果:mini-clickhouse 的 FINAL 是"把选中的行
      全部读进内存 → 排序 → 归并",于是:

      * 内存占用从"一个 block"变成"整次查询的行数";
      * 读路径不再是流式(第一批数据要等全部读完才算得出来);
      * ``max_threads`` 失去意义(单线程归并)。

      真实 ClickHouse 的 FINAL 是流式的(``FinishOn`` 处理器 +
      有序流归并),但**并行度的损失是一样的**。
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from ...block import Block, Chunk, ColumnWithTypeAndName, chunk_from_block
from ...catalog import StorageID, Table, new_uuid
from ...errors import ErrorCode, MiniChError, raise_error
from ...types import DataType, get_data_type
from ...util.keys import KeyDescription
from ...util.logger import get_logger
from ...util.stopwatch import ProfileEvents, Stopwatch
from ..base import IStorage, ReadContext, StorageFeatures
from .disk import DiskLocal, IDisk, get_available_space
from .expression import evaluate_expression, evaluate_predicate
from .key_condition import (
    CheckResult,
    KeyCondition,
    KeyConditionBuildInformation,
    PartitionPruner,
    describe_key_expression,
)
from .marks import (
    DEFAULT_INDEX_GRANULARITY,
    DEFAULT_INDEX_GRANULARITY_BYTES,
    MIN_INDEX_GRANULARITY_BYTES,
    IndexGranularity,
    MarkRange,
    RANGE_MAX,
    merge_ranges,
)
from .merge_policy import MergeSelectorParams, SimpleMergePolicy
from .merger import MergeTreeMergeConfig, merge_parts as _merge_parts_impl
from .part import (
    DEFAULT_MIN_BYTES_FOR_WIDE_PART,
    DEFAULT_MIN_ROWS_FOR_WIDE_PART,
    MergeTreePart,
    PartWriteContext,
    _materialize_block,
    column_expression,
    convert_to_column_type,
    create_column_for_type,
    format_part_name,
    missing_column_default,
    load_part,
    parse_part_name,
    write_part as _write_part_impl,
)
from .mutations import (
    MutationCommand,
    MutationEntry,
    MutationRegistry,
    build_mutation_command,
)
from .skip_index import (
    IndexCondition,
    IndexDescription,
    IndexHelper,
    MergeTreeIndexReader,
    collect_index_conditions,
)
from .ttl import (
    DEFAULT_TTL_ONLY_DROP_PARTS,
    TTLDescription,
    TTLInfo,
    TTLRowsRemover,
    compute_ttl_info,
    merge_ttl_infos,
)

__all__ = ["MergeTreeData", "DEFAULT_SETTINGS"]

_log = get_logger("MergeTree")


# ══════════════════════════════════════════════════════════════════════════
# 默认设置 —— 名字与真实 ClickHouse 的 ``MergeTreeSettings`` 一致
# ══════════════════════════════════════════════════════════════════════════

#: 真实的默认值(带注释说明它为什么是这个数)。
DEFAULT_SETTINGS: Dict[str, Any] = {
    # ── granule ────────────────────────────────────────────────────────
    "index_granularity": DEFAULT_INDEX_GRANULARITY,               # 8192 行
    "index_granularity_bytes": DEFAULT_INDEX_GRANULARITY_BYTES,   # 10 MiB
    "min_index_granularity_bytes": MIN_INDEX_GRANULARITY_BYTES,   # 1 KiB
    "enable_mixed_granularity_parts": 1,
    # ── 写入 ────────────────────────────────────────────────────────────
    "max_insert_block_size": 1048576,
    "min_insert_block_size_rows": 1048576,
    "min_insert_block_size_bytes": 256 * 1024 * 1024,
    "parts_to_delay_insert": 1000,
    "parts_to_throw_insert": 3000,
    # ── part 格式 ───────────────────────────────────────────────────────
    "min_bytes_for_wide_part": DEFAULT_MIN_BYTES_FOR_WIDE_PART,
    "min_rows_for_wide_part": DEFAULT_MIN_ROWS_FOR_WIDE_PART,
    # ── 合并 ────────────────────────────────────────────────────────────
    "max_bytes_to_merge_at_max_space_in_pool": 150 * 1024 ** 3,
    "max_bytes_to_merge_at_min_space_in_pool": 1 * 1024 ** 2,
    "number_of_free_entries_in_pool_to_execute_mutation": 20,
    "background_pool_size": 16,
    "merge_selecting_sleep_ms": 1000,
    # ── TTL ─────────────────────────────────────────────────────────────
    "ttl_only_drop_parts": DEFAULT_TTL_ONLY_DROP_PARTS,
    "merge_with_ttl_timeout": 4 * 3600,
    # ── 读取 ────────────────────────────────────────────────────────────
    "max_block_size": 65536,
    "max_threads": 8,
    "optimize_read_in_order": 1,
    "use_skip_indexes": 1,
    # ── 杂项 ────────────────────────────────────────────────────────────
    "old_parts_lifetime": 480,
    "finished_mutations_to_keep": 100,
}


def _setting(settings: Any, name: str, default: Any) -> Any:
    """从设置对象里读一个设置 —— **绝不**在模块级导入 ``minich.settings``。

    ``minich.settings`` 由另一个模块负责,可能还不存在,或者 API 会变。
    这里用 try/except 兜住所有异常:设置读不到就退回默认值,
    而"默认值错了"是**可观测**的(行为不对),"导入失败"是**致命的**。
    """
    if settings is None:
        return default
    for attr in ("try_get", "get", "get_setting"):
        fn = getattr(settings, attr, None)
        if callable(fn):
            try:
                return fn(name, default)
            except Exception:
                try:
                    v = fn(name)
                    return default if v is None else v
                except Exception:
                    continue
    return default


# ══════════════════════════════════════════════════════════════════════════
# MergeTreeData
# ══════════════════════════════════════════════════════════════════════════


#: 引擎**认得**但**不执行**的设置 —— ``MODIFY SETTING`` 会对它们抛
#: ``NOT_IMPLEMENTED``(理由见 :meth:`MergeTreeData.modify_setting`)。
_KNOWN_BUT_IGNORED_SETTINGS: Set[str] = {
    "background_pool_size",
    "number_of_free_entries_in_pool_to_execute_mutation",
    "merge_with_ttl_timeout",
    "min_insert_block_size_rows",
    "min_insert_block_size_bytes",
    "old_parts_lifetime",
    "finished_mutations_to_keep",
    "optimize_read_in_order",
}


class MergeTreeData(IStorage):
    """MergeTree 存储引擎 —— 所有 ``*MergeTree`` 家族的基类。"""

    engine_name = "MergeTree"

    #: 引擎家族 —— 子类覆盖(``Replacing`` / ``Summing`` / ``Collapsing`` …)
    engine_family: str = "MergeTree"

    features = StorageFeatures(
        primary_key=True,
        partitions=True,
        sorting_key=True,
        parallel_read=True,
        skip_indexes=True,
        ttl=True,
        mutations=True,
        merges=True,
        sampling=False,
        projections=True,
    )

    # ══════════════════════════════════════════════════════════════════
    # 构造
    # ══════════════════════════════════════════════════════════════════

    def __init__(
        self,
        table: Table,
        storage_id: StorageID,
        context: Any = None,
        engine_args: Optional[Sequence[Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(table, storage_id, context, engine_args)
        p = dict(params or {})
        self.params = p

        # ── 设置 ────────────────────────────────────────────────────────
        self.settings: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        for k, v in (p.get("settings") or {}).items():
            self.settings[str(k)] = v
        self.strict_insert_block_size = bool(p.get("strict_insert_block_size", False))

        # ── 键表达式 ────────────────────────────────────────────────────
        self.sorting_key_exprs: List[Any] = _as_expr_list(p.get("order_by"))
        self.sorting_key_reverse: List[bool] = _directions_of(p.get("order_by"))
        self.partition_key_exprs: List[Any] = _as_expr_list(p.get("partition_by"))
        self.primary_key_exprs: List[Any] = _as_expr_list(p.get("primary_key")) \
            or self.sorting_key_exprs
        self.sample_by_expr: Optional[Any] = p.get("sample_by")

        if not self.sorting_key_exprs:
            raise_error(
                ErrorCode.STORAGE_REQUIRES_PARAMETER,
                f"ENGINE = {self.engine_name} requires an ORDER BY (or a PRIMARY KEY) "
                f"expression",
            )

        # ── 索引 / TTL / 约束 ───────────────────────────────────────────
        self.index_descriptions: List[IndexDescription] = list(p.get("indexes") or [])
        self._bind_index_types()
        self.ttls: List[TTLDescription] = _as_ttl_list(p.get("ttl"))
        self.constraints: List[Tuple[str, str, Any]] = list(p.get("constraints") or [])
        self.projections: List[Any] = list(p.get("projections") or [])

        # ── 键描述(供 KeyCondition / 分区裁剪用)────────────────────────
        header = self.table.header()
        self.sorting_key_reverse, self.key_condition_info = _build_key_info(
            header, self.sorting_key_exprs, self.sorting_key_reverse
        )
        self.partition_key_types: List[DataType] = _types_of(header, self.partition_key_exprs)
        self.partition_key_reverse: List[bool] = [False] * len(self.partition_key_exprs)

        # ── 磁盘与路径 ──────────────────────────────────────────────────
        self.disk: IDisk = _resolve_disk(p, context)
        base = _resolve_base_path(p, context)
        self.disk_root = self.disk.get_root_path()
        self.data_path = f"{self.storage_id.database}/{self.storage_id.table}"
        if not self.disk.exists_directory(self.data_path):
            self.disk.create_dir(self.data_path)
        del base

        # ── part 清单 ───────────────────────────────────────────────────
        self.parts: Dict[str, MergeTreePart] = {}
        self.obsolete_parts: Dict[str, MergeTreePart] = {}
        self.parts_by_partition: Dict[str, List[str]] = {}
        self._parts_in_merge: Set[str] = set()

        #: part 的**引用计数** —— 一个 part 被"借出"(正在被读)时,即使它
        #: 已经被合并/删除替换,磁盘上的目录也**不会**立刻删掉。
        #: 真实 ClickHouse 靠 ``shared_ptr`` 做到这一点:查询还持有它,
        #: merge 就不会真的去删。没有这个计数时,一个在飞的
        #: ``SELECT`` 会拿到"目录已消失"的 ``FileNotFoundError``。
        self._part_pins: Dict[str, int] = {}
        #: 已经被替换、但**还有读者**的 part —— 等最后一个引用归还时删目录。
        self._pending_part_removals: Dict[str, MergeTreePart] = {}
        self.block_number = 0

        # ── 计数器 / 日志 ───────────────────────────────────────────────
        self.profile_events = ProfileEvents()
        self.last_read_events = ProfileEvents()
        self.part_log: Optional[List[Dict[str, Any]]] = None
        self.merges_log: List[Dict[str, Any]] = []

        # ── mutation ───────────────────────────────────────────────────
        self.mutations = MutationRegistry()
        self.column_defaults: Optional[Callable[..., Any]] = p.get("column_defaults") \
            or getattr(context, "column_defaults", None)

        # ── 合并 ────────────────────────────────────────────────────────
        self.merge_config = MergeTreeMergeConfig(family=self.engine_family)
        self._configure_family(p)
        self.merge_policy = SimpleMergePolicy(MergeSelectorParams(
            max_bytes_to_merge_at_max_space_in_pool=int(
                self.settings["max_bytes_to_merge_at_max_space_in_pool"]),
            max_bytes_to_merge_at_min_space_in_pool=int(
                self.settings["max_bytes_to_merge_at_min_space_in_pool"]),
        ))
        self._merge_thread: Optional[threading.Thread] = None
        self._merge_stop = threading.Event()
        self._merge_wakeup = threading.Event()
        self._merge_interval_s = float(self.settings.get("merge_selecting_sleep_ms", 1000)) / 1000.0
        self._merges_running = 0
        self._merge_cond = threading.Condition()

        # ── 复制钩子(``ReplicatedMergeTree`` 用,见 engines.py)───────
        self.replication: Any = None

        # ── 启动时加载已有 part ─────────────────────────────────────────
        self._load_existing_parts()

    # ── 家族配置 ────────────────────────────────────────────────────────

    def _configure_family(self, params: Dict[str, Any]) -> None:
        """从引擎参数/建表参数里抽出"家族语义"。

        真实 ClickHouse 的 ``ReplacingMergeTree(ver, is_deleted)`` 把
        ``ver``/``is_deleted`` 作为**引擎参数**传进来(``engine_args``);
        我们同时接受 ``params`` 里的同名字段,这样用 dict 建表的
        调用方(与解释器)两条路都能走。
        """
        cfg = self.merge_config
        args = [str(a) for a in self.engine_args if isinstance(a, (str, int))]
        fam = self.engine_family
        if fam == "Replacing":
            cfg.version_column = args[0] if len(args) > 0 else str(
                params.get("version_column", "") or "")
            cfg.is_deleted_column = args[1] if len(args) > 1 else str(
                params.get("is_deleted_column", "") or "")
        elif fam == "Summing":
            cols = params.get("sum_columns")
            if cols is None and self.engine_args:
                first = self.engine_args[0]
                if isinstance(first, (list, tuple)):
                    cols = list(first)
                elif isinstance(first, str):
                    cols = [first]
            cfg.sum_columns = [str(c) for c in (cols or [])]
        elif fam in ("Collapsing", "VersionedCollapsing"):
            cfg.sign_column = args[0] if len(args) > 0 else str(
                params.get("sign_column", "") or "")
            if fam == "VersionedCollapsing":
                cfg.version_column = args[1] if len(args) > 1 else str(
                    params.get("version_column", "") or "")
        self._validate_family_columns()

    def _validate_family_columns(self) -> None:
        """家族需要的列**必须**存在 —— 否则合并时会静默算错。"""
        cfg = self.merge_config
        for name, what in (
            (cfg.version_column, "version"),
            (cfg.sign_column, "sign"),
            (cfg.is_deleted_column, "is_deleted"),
        ):
            if name and self.table.get_column(name) is None:
                raise_error(
                    ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                    f"ENGINE = {self.engine_name}: column {name!r} ({what}) "
                    f"does not exist in table {self.full_table_name()}",
                )

    def _bind_index_types(self) -> None:
        """给每个跳数索引算出"索引表达式的结果类型"。

        .. warning::

           **求不出类型时必须报错,不能悄悄当成 ``String``。**

           以前这里 ``except Exception: d.data_type = String``。后果是:

           .. code-block:: text

             CREATE TABLE t (..., INDEX ix (a,b) TYPE minmax) ...
                 ↓ 表达式解析失败/不支持 → 类型被当成 String
             INSERT ...  → 索引列里拿到一个 Python list
                 → _value_blob 让它 "Cannot convert list to String"
                 → **建表过了、一插入就崩**,而且错误消息完全指不出原因

           类型是索引的**地基**:``minmax`` 要按类型序列化 min/max、
           ``bloom`` 要按类型哈希值 —— 类型错了,索引要么崩、要么
           (更糟)悄悄算出一堆无意义的摘要。

           所以现在:CREATE TABLE 阶段就抛 ``ILLEGAL_TYPE_OF_ARGUMENT``,
           把问题挡在"建表"这一刻,并明确告诉用户换个表达式。
        """
        header = self.table.header()
        for d in self.index_descriptions:
            if d.data_type is not None:
                continue
            try:
                d.data_type = evaluate_expression(d.expr, header).type
            except MiniChError as exc:
                raise_error(
                    ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                    f"Cannot determine the type of the skip index {d.name!r} "
                    f"expression {d.expr_sql!r} in {self.full_table_name()}: "
                    f"{exc.message}. The index would be built with the wrong "
                    f"serialization; use a simpler expression (a column, or a "
                    f"tuple of columns).",
                )
            except Exception as exc:  # pragma: no cover - 兜底
                raise_error(
                    ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                    f"Cannot determine the type of the skip index {d.name!r} "
                    f"expression {d.expr_sql!r} in {self.full_table_name()}: {exc}",
                )

    # ── 已有 part 的加载 ────────────────────────────────────────────────

    def _load_existing_parts(self) -> None:
        """启动时扫描数据目录,把已提交的 part 都登记进来。

        **先看目录名再打开文件**:一个 ``tmp_`` 开头的目录说明上次写到一半
        崩了 —— 直接删掉(真实 CH 的 ``clearOldTemporaryDirectories``)。
        这也是"原子改名"这套设计在崩溃恢复上的体现:
        因为提交是原子的,所以"存在但没提交"的目录**一定**是垃圾。
        """
        for entry in self.disk.list_dir(self.data_path):
            full = f"{self.data_path}/{entry}"
            if not self.disk.exists_directory(full):
                continue
            if entry.startswith("tmp"):
                _log.warning("Removing leftover temporary part directory", part=entry)
                self.disk.remove_dir(full)
                continue
            try:
                part = load_part(
                    self.disk, full, self.table,
                    indexes=self.index_descriptions,
                    partition_key_types=self.partition_key_types,
                    sorting_key_reverse=self.sorting_key_reverse,
                    primary_key_columns=self._primary_key_column_names(),
                    primary_key_types=self._primary_key_types_safe(),
                    table_columns=self.table.columns,
                )
            except MiniChError as exc:
                _log.error(f"Skipping unreadable part {entry}: {exc}")
                continue
            self._register_part(part)
            self.block_number = max(self.block_number,
                                    part.get_max_block_number())

    def _register_part(self, part: MergeTreePart) -> None:
        with self._lock:
            self.parts[part.get_name()] = part
            self.parts_by_partition.setdefault(part.get_partition_id(), [])
            if part.get_name() not in self.parts_by_partition[part.get_partition_id()]:
                self.parts_by_partition[part.get_partition_id()].append(part.get_name())

    # ══════════════════════════════════════════════════════════════════
    # 元信息
    # ══════════════════════════════════════════════════════════════════

    def stored_column_names(self) -> List[str]:
        return self.table.stored_column_names()

    def key_column_names(self) -> List[str]:
        """排序键用到的**落盘列名**(用于 SummingMergeTree 的"自动求和列"判定)。"""
        names: List[str] = []
        for e in self.sorting_key_exprs:
            n = _expr_column_name(e)
            if n and n not in names:
                names.append(n)
        return names

    def _primary_key_column_names(self) -> List[str]:
        names = []
        for e in self.primary_key_exprs:
            n = _expr_column_name(e)
            names.append(n or "")
        return names

    def _primary_key_types_safe(self) -> List[DataType]:
        header = self.table.header()
        out: List[DataType] = []
        for e in self.primary_key_exprs:
            try:
                out.append(evaluate_expression(e, header).type)
            except Exception:
                out.append(get_data_type("String"))
        return out

    def row_count(self) -> int:
        """总行数 —— **读 ``count.txt``,不读数据**(``count()`` 平凡优化的落点)。"""
        with self._lock:
            return sum(p.rows_count() for p in self.parts.values())

    def size_in_bytes(self) -> int:
        with self._lock:
            return sum(p.bytes_on_disk() for p in self.parts.values())

    def part_count(self) -> int:
        with self._lock:
            return len(self.parts)

    def all_parts_count(self) -> int:
        with self._lock:
            return len(self.parts) + len(self.obsolete_parts)

    def get_parts(self) -> List[MergeTreePart]:
        with self._lock:
            return list(self.parts.values())

    def get_data_parts(self) -> List[MergeTreePart]:
        """活跃且非空的 part。"""
        return [p for p in self.get_parts() if not p.is_empty()]

    def get_part(self, name: str) -> Optional[MergeTreePart]:
        with self._lock:
            return self.parts.get(name)

    def get_parts_by_partition(self, partition_id: str) -> List[MergeTreePart]:
        with self._lock:
            return [self.parts[n] for n in self.parts_by_partition.get(partition_id, [])
                    if n in self.parts]

    def get_partition_ids(self) -> List[str]:
        with self._lock:
            return sorted(self.parts_by_partition)

    def partition_value_of(self, part: MergeTreePart) -> Any:
        return part.partition_value

    def partition_key_values(self) -> Dict[str, Any]:
        """``{partition_id: 分区键取值}`` —— 分区裁剪的输入。"""
        out: Dict[str, Any] = {}
        for pid in self.get_partition_ids():
            for p in self.get_parts_by_partition(pid):
                out[pid] = p.partition_value
                break
        return out

    def get_sorting_key_description(self) -> str:
        return ", ".join(_expr_sql(e) for e in self.sorting_key_exprs)

    def get_primary_key_description(self) -> str:
        return ", ".join(_expr_sql(e) for e in self.primary_key_exprs)

    def get_partition_key_description(self) -> str:
        return ", ".join(_expr_sql(e) for e in self.partition_key_exprs)

    def describe_stats(self) -> Dict[str, Any]:
        """``system.parts`` 用得上的统计。"""
        parts = self.get_parts()
        return {
            "engine": self.engine_name,
            "parts": len(parts),
            "active_parts": len(parts),
            "obsolete_parts": len(self.obsolete_parts),
            "rows": sum(p.rows_count() for p in parts),
            "bytes_on_disk": sum(p.bytes_on_disk() for p in parts),
            "partitions": len(self.get_partition_ids()),
            "sorting_key": self.get_sorting_key_description(),
            "primary_key": self.get_primary_key_description(),
            "partition_key": self.get_partition_key_description(),
            "index_granularity": int(self.settings["index_granularity"]),
            "index_granularity_bytes": int(self.settings["index_granularity_bytes"]),
            "ttl_only_drop_parts": int(self.settings["ttl_only_drop_parts"]),
            "parts_list": parts,
            "pending_mutations": len(self.mutations.undone()),
        }

    def parts_rows(self) -> List[Dict[str, Any]]:
        """``system.parts`` 的每一行。"""
        out = []
        for p in sorted(self.get_parts(), key=lambda x: x.get_name()):
            row = p.get_stats()
            row.update({
                "database": self.storage_id.database,
                "table": self.storage_id.table,
                "engine": self.engine_name,
                "active": 1,
                "sorting_key": self.get_sorting_key_description(),
            })
            out.append(row)
        return out

    def check(self) -> str:
        """``CHECK TABLE`` —— 逐个 part 校验 checksums。"""
        problems: List[str] = []
        for p in self.get_parts():
            try:
                p.verify_checksums()
            except MiniChError as exc:
                problems.append(str(exc))
        return "\n".join(problems)

    # ══════════════════════════════════════════════════════════════════
    # 写
    # ══════════════════════════════════════════════════════════════════

    def write(self, ctx: Any, block: Block) -> None:
        """``INSERT`` —— 补齐默认值、按分区/大小切块、逐个写成 part。"""
        if self.is_shutdown():
            raise_error(ErrorCode.ABORTED,
                        f"Table {self.full_table_name()} is shutting down")
        if block.rows() == 0:
            return
        events = self._events(ctx)

        # 常量列先摊平 —— 否则后面"按块切""按分区切""补齐默认值"
        # 全都会在这类列上做出错误的取值(详见 part._materialize_block)。
        block = _materialize_block(block)
        block = self._fill_defaults(block)
        self._check_constraints(block)

        for chunk_block in self._split_by_partition(block):
            for sub in self._split_by_size(chunk_block):
                self._write_one_part(sub, ctx, events)

    def _events(self, ctx: Any) -> ProfileEvents:
        if isinstance(ctx, ProfileEvents):
            return ctx
        holder = getattr(ctx, "profile_events", None)
        if isinstance(holder, ProfileEvents):
            return holder
        return self.profile_events

    def _fill_defaults(self, block: Block) -> Block:
        """补齐 ``DEFAULT`` / ``MATERIALIZED`` 列。

        .. code-block:: text

           三条规则(与真实 ClickHouse 一致):

             DEFAULT expr      —— **没提供**这一列才用表达式;
                                  提供了(哪怕是 NULL)就用提供的值
             MATERIALIZED expr —— **永远**由表达式算,用户不能提供
             ALIAS expr        —— 不落盘;查询时替换成表达式

           另外:一张表可能刚 ``ALTER ADD COLUMN`` 过,于是
           **新列在老 part 里不存在** —— 那种情况在**读**的时候补
           (见 MergeTreePart.missing_columns),与写入无关。

        ``self.column_defaults`` 是**解释器提供的可调用对象**:
        ``column_defaults(expr_sql, block) -> IColumn``。拿不到它就退回
        类型的零值 —— 结果是"默认值表达式失效",这在教程里会标出来
        (它属于"引擎不该自己解析 SQL"的边界)。
        """
        out = Block([], block.info.clone())
        wanted = self.stored_column_names()
        for name in wanted:
            cdef = self.table.get_column(name)
            existing = None if cdef.is_materialized() else block.try_get_by_name(name)
            if existing is not None:
                out.add_column(ColumnWithTypeAndName(name, cdef.type, existing.column))
                continue
            # ★ 表达式在 ``out``(已经拼好的部分 block)上求值,而**不是**在
            #   输入的 ``block`` 上 —— 因为输入 block 的列名可能还是 SELECT
            #   里的表达式名(``number``),而 ``MATERIALIZED v * 2`` 依赖的是
            #   **目标列名** ``v``。用 ``out`` 求值让"列之间的依赖"自然成立
            #   (这也是真实 ClickHouse 用 ExpressionActions 做的事)。
            out.add_column(ColumnWithTypeAndName(
                name, cdef.type, self._default_column_for(cdef, out)
            ))
        return out

    def _default_column_for(self, cdef: Any, block: Block) -> Any:
        """算出"这一列在插入时被省略了"应该填什么。

        .. code-block:: text

           三条来源,按优先级:

           ① 解释器提供的 ``column_defaults(sql, block) -> IColumn``
              — 真实 ClickHouse 由 ExpressionActions 承担这个角色
           ② 引擎自己解析 ``default_expr_sql`` 并**在 block 上逐行求值**
              — ``MATERIALIZED v * 2`` 必须这么算,否则那一列会全是零
           ③ 类型的零值(``0`` / ``''`` / ``NULL``)

        .. warning::

           **②失败时不能静默退化成零值。** 以前这里在拿不到
           ``column_defaults`` 时直接补零,于是
           ``MATERIALIZED v * 2`` 的列在磁盘上全是 0 —— 查询不报错、
           结果全错,是最难查的一类 bug。现在:
           解析不出来就抛 ``CANNOT_ADD_COLUMN_DEFAULT``(错误码 68,
           真实 ClickHouse 里就是这个),让问题在**插入时**暴露。
        """
        rows = block.rows()
        if not cdef.default_expr_sql:
            col = create_column_for_type(cdef.type)
            for _ in range(rows):
                col.append(cdef.type.normalize(cdef.type.default_value()))
            return col

        if self.column_defaults is not None:
            try:
                col = self.column_defaults(cdef.default_expr_sql, block)
            except MiniChError:
                raise
            except Exception as exc:  # pragma: no cover
                _log.warning(f"column_defaults failed for {cdef.name}: {exc}")
                col = None
            if col is not None:
                if col.size() != rows:
                    raise_error(
                        ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                        f"column_defaults({cdef.default_expr_sql!r}) returned "
                        f"{col.size()} rows, expected {rows}",
                    )
                return col

        try:
            expr = column_expression(cdef)
            evaluated = evaluate_expression(expr, block, self.context).column
            if evaluated.is_const():
                evaluated = evaluated.convert_to_full_column()
            if evaluated.size() != rows:
                evaluated = evaluated.clone_resized(rows)
            return convert_to_column_type(evaluated, cdef.type)
        except MiniChError as exc:
            raise_error(
                ErrorCode.CANNOT_ADD_COLUMN_DEFAULT,
                f"Cannot compute the value for column {cdef.name!r} of "
                f"{self.full_table_name()}: expression "
                f"{cdef.default_expr_sql!r} could not be evaluated for this block "
                f"({exc.message}). Provide a `column_defaults` callable to the "
                f"storage engine, or drop the expression.",
            )
        return None  # pragma: no cover - raise_error 不会返回

    def _check_constraints(self, block: Block) -> None:
        """``CONSTRAINT c CHECK expr`` —— 插入时逐行验。

        真实 ClickHouse 的错误码是 ``VIOLATED_CONSTRAINT``(不在我们的
        编号表里),我们用 ``SETTING_CONSTRAINT_VIOLATION`` 代替,
        并在教程里标注这个替换。
        """
        if not self.constraints:
            return
        from ...util.strings import escape_tsv

        for name, kind, expr in self.constraints:
            if str(kind).upper() != "CHECK":
                continue
            mask = evaluate_predicate(expr, block)
            failed = [i for i, ok in enumerate(mask) if not ok]
            if failed:
                raise_error(
                    ErrorCode.SETTING_CONSTRAINT_VIOLATION,
                    f"Constraint {name} is violated at row {failed[0]} "
                    f"({self.full_table_name()})",
                )
        del escape_tsv

    def _split_by_partition(self, block: Block) -> List[Block]:
        """把 block 按分区键取值切开 —— **一个 part 只能属于一个分区**。"""
        if not self.partition_key_exprs:
            return [block]
        if len(self.partition_key_exprs) == 1:
            col = evaluate_expression(self.partition_key_exprs[0], block).column
            col = col.convert_to_full_column() if col.is_const() else col
            values = [col.get(i) for i in range(block.rows())]
        else:
            cols = []
            for e in self.partition_key_exprs:
                c = evaluate_expression(e, block).column
                cols.append(c.convert_to_full_column() if c.is_const() else c)
            values = [tuple(c.get(i) for c in cols) for i in range(block.rows())]
        groups: Dict[Any, List[int]] = {}
        order: List[Any] = []
        for i, v in enumerate(values):
            key = _hashable(v)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(i)
        if len(order) == 1:
            return [block]
        out: List[Block] = []
        for key in order:
            idx = groups[key]
            # 用 ``take``(返回新 Block)而不是 ``permute``(就地改写):
            # 后者会在第一次调用时把 block 缩小,后续分区的索引就越界了。
            out.append(block.take(idx) if len(idx) != block.rows() else block)
        return out

    def _split_by_size(self, block: Block) -> List[Block]:
        """按 ``max_insert_block_size`` 切块 —— 超了只警告(真实语义)。"""
        limit = int(self.settings.get("max_insert_block_size", 1048576))
        if limit <= 0 or block.rows() <= limit:
            return [block]
        msg = (f"Splitting a block of {block.rows()} rows into parts of "
               f"{limit} rows because it exceeds max_insert_block_size")
        if self.strict_insert_block_size:
            raise_error(ErrorCode.QUERY_IS_TOO_LARGE, msg)
        _log.warning(msg + " (this is a warning, not an error — but you should "
                           "insert in smaller batches)")
        out: List[Block] = []
        offset = 0
        while offset < block.rows():
            n = min(limit, block.rows() - offset)
            out.append(block.slice(offset, n))
            offset += n
        return out

    def _write_one_part(self, block: Block, ctx: Any, events: ProfileEvents) -> MergeTreePart:
        partition_value = self._partition_value_of_block(block)
        pid = _partition_id_of(partition_value)
        self._check_parts_threshold(pid)
        self.block_number += 1
        name = format_part_name(pid, self.block_number, self.block_number, 0)
        context = self.make_write_context(
            partition_value=partition_value, level=0, block_number=self.block_number,
            events=events, ttl_info=self.compute_ttl_info_for_block(block),
        )
        path = f"{self.data_path}/{name}"
        part = _write_part_impl(self.disk, path, block, context)
        self._register_part(part)
        self._log_part("NewPart", part)
        if events is not None:
            events.increment("InsertedRows", block.rows())
            events.increment("InsertedBytes", sum(c.column.byte_size() for c in block.data))
        return part

    def _partition_value_of_block(self, block: Block) -> Any:
        if not self.partition_key_exprs:
            return "all"
        vals = []
        for e in self.partition_key_exprs:
            c = evaluate_expression(e, block).column
            col = c.convert_to_full_column() if c.is_const() else c
            vals.append(col.get(0))
        return vals[0] if len(vals) == 1 else tuple(vals)

    def _check_parts_threshold(self, partition_id: str) -> None:
        """``parts_to_throw_insert`` —— "part 太多"的硬闸门。

        .. code-block:: text

           为什么要有这个闸门?

             写入速率 > 合并速率 时,part 数会**无界增长**。
             每个 part 至少是一个目录 + 若干文件;
             一个分区有 10 万个 part 时,每次 SELECT 要打开 10 万个文件 ——
             目录遍历、文件句柄、内存都会崩。

             ClickHouse 的选择是:**宁可拒绝写入,也不让表退化到不可用**。
             错误消息就是一句"该加合并线程 / 降低写入频率"的提示。

           两个阈值:
             parts_to_delay_insert(1000)  超过后**主动放慢** INSERT(睡眠)
             parts_to_throw_insert(3000)  超过后**直接报错**
        """
        count = len(self.parts_by_partition.get(partition_id, []))
        throw_at = int(self.settings.get("parts_to_throw_insert", 3000))
        if count >= throw_at:
            raise_error(
                ErrorCode.TOO_MANY_PARTS,
                f"Too many parts ({count}) in partition {partition_id!r} of table "
                f"{self.full_table_name()}. Merges are processing significantly "
                f"slower than inserts. Increase background_pool_size or insert in "
                f"larger batches.",
            )
        delay_at = int(self.settings.get("parts_to_delay_insert", 1000))
        if count >= delay_at:
            # 真实 CH 会按超额比例睡眠;我们固定睡一小会儿,并记一条日志
            _log.warning(f"Delaying insert: partition {partition_id!r} already has "
                         f"{count} parts (parts_to_delay_insert={delay_at})")
            time.sleep(0.001)

    # ── 写上下文 ────────────────────────────────────────────────────────

    def compute_ttl_info_for_block(self, block: Block) -> Optional[TTLInfo]:
        """算出一个 block 的 :class:`TTLInfo` —— 写进 ``ttl.txt``。

        写 part 时顺手算一次,以后所有 TTL 判定都是"读一个 JSON 比一次大小",
        而不是"重扫整个 part"。这就是 ``ttl.txt`` 存在的全部意义。
        """
        if not self.ttls or block.rows() == 0:
            return None
        try:
            return compute_ttl_info(self.ttls, block)
        except MiniChError:
            raise
        except Exception as exc:  # pragma: no cover - TTL 求值失败不该阻断写入
            _log.warning(f"Could not compute TTL info for a new part: {exc}")
            return None

    def make_write_context(
        self,
        partition_value: Any = None,
        level: int = 0,
        block_number: int = 0,
        events: Optional[ProfileEvents] = None,
        codecs: Optional[Dict[str, str]] = None,
        ttl_info: Optional[TTLInfo] = None,
    ) -> PartWriteContext:
        stored = self.stored_column_names()
        types = {n: self.table.get_column(n).type for n in stored}
        # 列级 ``CODEC(...)`` 在这里**接线** —— 建表时声明的编解码器存在列元数据里
        # (``ColumnDefinition.codec``),过去没人把它传下去,于是每一列都被按默认的
        # LZ4 写出:声明被静默丢掉。现在按列名收集,所有 ``make_write_context`` 的
        # 调用点(INSERT / OPTIMIZE 重写 / 后台合并)都自动生效。
        resolved_codecs = self._column_codec_names(stored)
        if codecs:
            # 调用方显式传入的覆盖优先级最高(目前没有生产调用方这么做)
            resolved_codecs.update(codecs)
        gran = IndexGranularity(
            index_granularity=int(self.settings["index_granularity"]),
            index_granularity_bytes=int(self.settings["index_granularity_bytes"]),
            adaptive=bool(self.settings.get("enable_mixed_granularity_parts", 1)),
            min_index_granularity_bytes=int(self.settings["min_index_granularity_bytes"]),
        )
        return PartWriteContext(
            table=self.table,
            column_names=stored,
            column_types=types,
            codecs=resolved_codecs,
            sorting_key_exprs=list(self.sorting_key_exprs),
            sorting_key_reverse=list(self.sorting_key_reverse),
            primary_key_positions=list(range(len(self.primary_key_exprs))),
            partition_key_exprs=list(self.partition_key_exprs),
            partition_key_types=list(self.partition_key_types),
            partition_value=partition_value,
            index_granularity=gran,
            indexes=list(self.index_descriptions),
            level=level,
            block_number=block_number,
            profile_events=events if events is not None else self.profile_events,
            table_uuid=self.storage_id.uuid,
            min_bytes_for_wide_part=int(self.settings["min_bytes_for_wide_part"]),
            min_rows_for_wide_part=int(self.settings["min_rows_for_wide_part"]),
            ttl_info=ttl_info,
            build_skip_indexes=True,
        )

    def _column_codec_names(self, stored: Sequence[str]) -> Dict[str, str]:
        """把每一列**建表时声明的** ``CODEC(...)`` 转成"块里实际要用的编解码器名"。

        声明存在列元数据里(:attr:`minich.catalog.ColumnDefinition.codec`,由解析器
        :meth:`minich.sql.parser` 从 ``CODEC(...)`` 子句里取出)。只有写了 ``CODEC``
        的列才进结果字典;没写的列留给 :meth:`PartWriteContext.codec_for` 回退到
        默认的 :data:`minich.storages.merge_tree.part.DEFAULT_CODEC`。

        解析 / 校验交给 :func:`_resolve_codec_name`(它用的是
        ``CompressionCodecFactory.parse_codec_chain``)—— 于是非法链
        (``CODEC(Delta)``)会在这里报错,而不是被静默忽略。
        """
        out: Dict[str, str] = {}
        for name in stored:
            cdef = self.table.get_column(name)
            text = getattr(cdef, "codec", None) if cdef is not None else None
            if not text:
                continue
            out[name] = _resolve_codec_name(str(text))
        return out

    # ══════════════════════════════════════════════════════════════════
    # 读
    # ══════════════════════════════════════════════════════════════════

    def analyze_where(self, expr: Any, ctx: Any = None) -> Any:
        """把 ``WHERE`` 变成 :class:`KeyCondition` —— 优化器从这里拿裁剪信息。"""
        if expr is None or not self.key_condition_info.key_exprs:
            return None
        kc = KeyCondition(self.key_condition_info)
        kc.add(expr)
        return kc

    def supports_where_pushdown(self) -> bool:
        return True

    def read(self, ctx: Any = None, read_ctx: Any = None,
             **kwargs: Any) -> Iterator[Chunk]:
        """读取 —— 引擎的**唯一**入口。

        .. warning::

           **签名里的 ``read_ctx`` 是刻意的。**

           ``minich.execution.interpreter._call_read`` 会按签名适配:

           .. code-block:: text

              参数里同时有 ctx 与 read_ctx → engine.read(ctx, read_ctx)
              只有 ctx 与 **kwargs(两个参数)→ engine.read(ctx)   ← **丢失全部上下文**

           第二个分支会把 ``ReadContext`` 整个丢掉 —— 于是 ``final``、
           ``settings``、``limit``、``partition_ids`` **全部失效**,
           而表现是"``FROM t FINAL`` 没去重""``SETTINGS use_skip_indexes=0``
           没效果"这类**沉默的功能丢失**。所以这里显式保留
           ``read_ctx`` 这个形参名,让适配器走第一个分支。

           真实 ClickHouse 没有这个问题:它的接口是固定的
           ``IStorage::read(QueryPlan &, SelectQueryInfo &, ...)``。
        """
        rc = _normalize_read_context(ctx if read_ctx is None else read_ctx, kwargs)
        if ctx is not None and read_ctx is None and not isinstance(ctx, ReadContext):
            rc.ctx = ctx if rc.ctx is None else rc.ctx
        events = _new_read_events(self, rc)
        before_events = events.snapshot()
        #: 这次读"借出"的 part —— 读期间它们不会被合并/删除抽走目录。
        pinned: List[MergeTreePart] = []
        try:
            where = _collect_where(rc, kwargs)

            requested = rc.wanted_columns(self.table.get_column_names()) or []
            read_columns = self._plan_columns(requested, where)

            # ① 分区裁剪
            selected_partitions = self._prune_partitions(where, rc, events)

            # ② 逐 part 读取
            parts = [p for p in self.get_data_parts()
                     if selected_partitions is None
                     or p.get_partition_id() in selected_partitions]
            parts.sort(key=lambda p: (p.get_partition_id(), p.get_name()))
            if events is not None:
                events.increment("SelectedParts", len(parts))

            # ★ 借出这批 part。这是流式读:``yield`` 之后控制权回到调用方,
            #   后台合并线程**随时**可能把这些 part 合并掉并删目录。
            #   没有这一步,一个慢查询会在读第 N 个 part 时报
            #   ``FileNotFoundError``(真实 ClickHouse 靠 shared_ptr 引用
            #   计数避免 —— 见 :meth:`pin_parts`)。
            self.pin_parts(parts)
            pinned = parts

            max_block = int(self.settings.get("max_block_size", 65536))
            produced = 0
            collected: List[Block] = []
            for part in parts:
                for blk in self._read_part(part, read_columns, where, rc, events):
                    if rc.final:
                        collected.append(blk)
                        continue
                    if blk.rows() == 0:
                        continue
                    chunk = self._make_chunk(blk, requested, rc)
                    produced += chunk.num_rows
                    yield chunk
                    if rc.limit is not None and produced >= rc.limit:
                        return

            if rc.final and collected:
                merged = self._final_merge_blocks(collected, read_columns)
                merged = self._filter_block(merged, where, rc)
                offset = 0
                while offset < merged.rows():
                    n = min(max_block, merged.rows() - offset)
                    sub = merged.slice(offset, n)
                    chunk = self._make_chunk(sub, requested, rc)
                    produced += chunk.num_rows
                    yield chunk
                    offset += n
                    if rc.limit is not None and produced >= rc.limit:
                        break
        finally:
            # 归还借出的 part。生成器被提前关闭(``LIMIT`` 早停、调用方
            # 只取前几个 chunk)时也会走到这里 —— ``finally`` 是唯一的
            # 可靠归还点,漏掉就会让目录永远删不掉。
            self.unpin_parts(pinned)
            self._merge_events_into_global(events, before_events)

    def _plan_columns(self, requested: Sequence[str], where: Any) -> List[str]:
        """列裁剪计划:请求的列 ∪ 谓词的依赖列(都要真的从磁盘读)。"""
        stored = set(self.stored_column_names())
        needed: List[str] = []

        def add(name: str) -> None:
            if name in stored and name not in needed:
                needed.append(name)

        for name in requested:
            if name in stored:
                add(name)
            else:
                for dep in _expression_dependencies(self.table.get_column(name)):
                    add(dep)
        for name in _ast_dependencies(where):
            add(name)
        # 家族列 —— 合并/去重必须读到
        cfg = self.merge_config
        for c in (cfg.version_column, cfg.is_deleted_column, cfg.sign_column):
            if c:
                add(c)
        # 保持表定义顺序,保证 chunk 的列顺序稳定可预测
        order = {n: i for i, n in enumerate(self.table.get_column_names())}
        needed.sort(key=lambda n: order.get(n, 1 << 30))
        return needed

    def _prune_partitions(
        self, where: Any, rc: ReadContext, events: Optional[ProfileEvents]
    ) -> Optional[Set[str]]:
        if rc.partition_ids is not None:
            return set(rc.partition_ids)
        if where is None or not self.partition_key_exprs:
            return None
        info = KeyConditionBuildInformation()
        header = self.table.header()
        try:
            exprs = []
            cols = []
            types = []
            for e in self.partition_key_exprs:
                sql, col, func, rtype = describe_key_expression(e, header)
                exprs.append(sql)
                cols.append(col or "")
                types.append(rtype)
            info.key_exprs = exprs
            info.key_types = types
            info.reverse = [False] * len(exprs)
            info.key_columns = cols
            info.key_functions = [None] * len(exprs)
            for i, e in enumerate(self.partition_key_exprs):
                _, _, func, _ = describe_key_expression(e, header)
                info.key_functions[i] = func
        except MiniChError:
            return None
        pruner = PartitionPruner(info)
        pruner.add(where)
        values = self.partition_key_values()
        if not values:
            return None
        pruned = pruner.prune(values)
        if events is not None and len(pruned) < len(values):
            events.increment("SelectedRanges", len(values) - len(pruned))
        return pruned

    def _read_part(
        self,
        part: MergeTreePart,
        columns: Sequence[str],
        where: Any,
        rc: ReadContext,
        events: Optional[ProfileEvents],
    ) -> Iterator[Block]:
        """读一个 part(已做 granule 裁剪 + 跳过索引)。"""
        ranges = self._mark_ranges_for_part(part, where, rc, events)
        for mark_range in ranges:
            if mark_range.is_empty():
                continue
            marks = part.get_marks_count()
            mark_range = MarkRange(mark_range.begin, min(mark_range.end, marks))
            if events is not None:
                events.increment("SelectedMarks", mark_range.get_marks_count())

            granularity = part.get_index_granularity()
            start_row = granularity.get_rows_before_mark(mark_range.begin)
            total = granularity.get_rows_before_mark(mark_range.end) - start_row
            if total <= 0:
                continue

            # 整段读一次(每列一次 IO),然后按块切
            cols: Dict[str, Any] = {}
            for name in columns:
                if part.has_column(name):
                    cols[name] = part.read_column(rc, name, mark_range, events)
                else:
                    cdef = self.table.get_column(name)
                    if cdef is None:
                        continue
                    cols[name] = self._missing_column_default(cdef, total)

            max_block = int(self.settings.get("max_block_size", 65536))
            offset = 0
            while offset < total:
                n = min(max_block, total - offset)
                block = Block()
                for name, col in cols.items():
                    cdef = self.table.get_column(name)
                    sub = col.clone_empty()
                    sub.insert_range_from(col, offset, n)
                    block.add_column(ColumnWithTypeAndName(name, cdef.type, sub))
                if events is not None:
                    events.increment("SelectedRows", n)
                    events.increment("SelectedBytes",
                                     sum(c.column.byte_size() for c in block.data))
                filtered = self._filter_block(block, where, rc)
                if filtered.rows() > 0:
                    yield filtered
                offset += n

    def _mark_ranges_for_part(
        self, part: MergeTreePart, where: Any, rc: ReadContext, events: Optional[ProfileEvents]
    ) -> List[MarkRange]:
        """一个 part 上"该读哪几段 granule"。

        返回的是**列表**而不是单个区间,因为 ``KeyCondition`` 可能给出
        多段(例如 ``WHERE id != 5`` 会产生 "(-∞,5)" 与 "(5,+∞)" 两段)。
        真实 ClickHouse 的 ``getRanges`` 同样返回 ``MarkRanges``(一个列表)。
        """
        marks = part.get_marks_count()
        full: List[MarkRange] = [MarkRange(0, marks)]

        kc = rc.key_analysis
        if kc is None and where is not None and self.key_condition_info.key_exprs:
            kc = KeyCondition(self.key_condition_info)
            kc.add(where)
        if isinstance(kc, KeyCondition) and kc.has_usable_ranges():
            kc.attach_part(part)
            ranges = kc.get_ranges()
            if ranges:
                whole = MarkRange(0, marks)
                full = merge_ranges([whole.intersect_with(r) for r in ranges])
            else:
                full = []
        if full and where is not None and self.index_descriptions \
                and self._skip_indexes_enabled(rc):
            full = self._apply_skip_indexes(part, full, where, events)
        return full

    def _skip_indexes_enabled(self, rc: ReadContext) -> bool:
        """两个设置的联合判定 —— ``use_skip_indexes`` 与
        ``use_skip_indexes_if_final``。

        .. code-block:: text

           use_skip_indexes            (默认 1)  关掉它 → 跳过索引完全不参与
           use_skip_indexes_if_final   (默认 0)  **FINAL** 读时默认**不用**跳过索引

        为什么 ``FINAL`` 默认把跳过索引关掉? 因为 FINAL 要做"同键折叠",
        而跳过索引的粒度是"若干 granule" —— 一个被保留的 granule 里,
        被剪掉的那些行本来可能参与折叠(比如它们的 sign 要和别的行抵消)。
        剪掉它们会让 FINAL 的结果**不一样**。所以默认是"要么全读,
        要么由用户自己承担风险显式打开"。

        这两个设置都必须从**查询级**读(见 :func:`_query_setting`),
        否则 ``SELECT ... SETTINGS use_skip_indexes = 0``
        这个"证明索引真的在干活"的演示就完全没效果 ——
        而这正是教程里用来给读者看对照实验的手段。
        """
        if not int(_query_setting(self, rc, "use_skip_indexes", 1)):
            return False
        if rc.final and not int(_query_setting(self, rc, "use_skip_indexes_if_final", 0)):
            return False
        return True

    def _apply_skip_indexes(
        self, part: MergeTreePart, mark_ranges: Sequence[MarkRange], where: Any,
        events: Optional[ProfileEvents],
    ) -> List[MarkRange]:
        """用跳数索引把 mark 区间再切细。

        .. code-block:: text

           WHERE url = 'x'  (url 上没有排序键,但有 bloom_filter 索引)

             所有 mark ──► 对每个候选 mark 问索引:
                            "这一组里**可能**有 url='x' 吗?"
                              │
                     ┌────────┴────────┐
                   否(确定没有)      是/不确定
                     砍掉              保留
                              │
                              ▼
                       剩下的 mark 区间
                       ★ 只可能"多读",绝不可能"漏读"
        """
        if not part.index_readers:
            return list(mark_ranges)
        # 预先算一次"每个索引能从 WHERE 里拿到哪些条件" ——
        # 不要放进 mark 循环里(那是 O(marks × 索引数 × AST 遍历))。
        per_index: List[Tuple[MergeTreeIndexReader, List[IndexCondition]]] = []
        for reader in part.index_readers.values():
            # 传索引表达式的**结果类型**:条件里的字面量要按它归一化,
            # 否则 ``WHERE d >= '2024-01-04'``(字符串)会和列里的
            # "天数"(整数)比不了 → 轻则裁不掉,重则(bloom)丢数据。
            conds = collect_index_conditions(where, reader.index.expr_sql,
                                             reader.index.data_type)
            if conds:
                per_index.append((reader, conds))
        if not per_index:
            return list(mark_ranges)

        marks = part.get_marks_count()
        kept: List[MarkRange] = []
        for mr in mark_ranges:
            begin = mr.begin
            end = min(mr.end, marks)
            current_start: Optional[int] = None
            for m in range(begin, end):
                one = MarkRange(m, m + 1)
                ok = any(
                    reader.may_be_true_for_mark_range(one, conds, part.get_index_granularity())
                    for reader, conds in per_index
                )
                if ok:
                    if current_start is None:
                        current_start = m
                else:
                    if current_start is not None:
                        kept.append(MarkRange(current_start, m))
                        current_start = None
            if current_start is not None:
                kept.append(MarkRange(current_start, end))
        # 注意:这里**不**累加 SelectedMarks —— 计数器在 _read_part 里
        # 按"最终要读的 mark 数"统一加一次,避免重复计数。
        return merge_ranges(kept)

    def _filter_block(self, block: Block, where: Any, rc: ReadContext) -> Block:
        """把 ``WHERE`` 谓词逐行套用到 block 上。

        读取层必须自己过滤:mark 裁剪只会给出"可能匹配"的**超集**
        (granule 是最小单位,里面总有边角不匹配的行)。
        真实 ClickHouse 由解释器里的 ``FilterTransform`` 完成这一步;
        存储层在这里做,是为了让 ``MergeTreeData.read`` 单独使用时
        (教程、测试)语义仍然正确。重复过滤是无害的。
        """
        if where is None or block.rows() == 0:
            return block
        mask = evaluate_predicate(where, block, getattr(rc, "ctx", None))
        if all(mask):
            return block
        return block.filter(mask)

    def _make_chunk(self, block: Block, requested: Sequence[str], rc: ReadContext) -> Chunk:
        """把内部 block 转成对外的 Chunk(列顺序 = 请求顺序)。"""
        out_cols: List[ColumnWithTypeAndName] = []
        for name in requested:
            cdef = self.table.get_column(name)
            if cdef is None:
                continue
            existing = block.try_get_by_name(name)
            if existing is not None:
                out_cols.append(ColumnWithTypeAndName(name, cdef.type, existing.column))
                continue
            # 非落盘列(ALIAS / MATERIALIZED / EPHEMERAL)→ 读时计算
            col = evaluate_expression(column_expression(cdef), block,
                                      getattr(rc, "ctx", None)).column
            if col.is_const():
                col = col.convert_to_full_column()
            out_cols.append(ColumnWithTypeAndName(name, cdef.type, col))
        chunk_header = Block([ColumnWithTypeAndName(c.name, c.type, None) for c in out_cols]) \
            if False else Block([ColumnWithTypeAndName(c.name, c.type, c.type.create_column())
                                 for c in out_cols])
        return Chunk(chunk_header, [c.column for c in out_cols], block.rows())

    def _final_merge_blocks(self, blocks: Sequence[Block], columns: Sequence[str],
                            config: Optional[Any] = None) -> Block:
        """``FINAL`` 的实现:把读到的行按排序键归并 + 家族折叠。

        .. warning::

           **这里就是 ``FINAL`` 的代价所在。** 它把"流式读"变成了
           "先攒起来、再排序、再归并":

           .. code-block:: text

              不带 FINAL:  granule 可以独立并行读,算出一块就往下游发一块
                          内存占用 ≈ max_block_size 行

              带 FINAL:    所有行必须先落到内存里,按排序键对齐之后
                          才能折叠 —— 同一个键的多行必须相遇
                          内存占用 ≈ 整次查询的行数
                          并行度 ≈ 1(归并是单线程的)

           真实 ClickHouse 的 FINAL 是**流式**的(它靠
           "输入已按排序键有序"这个不变量做有序流归并),
           所以它不需要把全部行攒在内存里;但**并行度的损失是一样的**
           —— 同一个键的行必须落到同一个处理线程上。
        """
        from .merger import BlockRowSource, MergeTreeMergingIterator

        types = {n: self.table.get_column(n).type for n in columns}
        merged = Block()
        for b in blocks:
            if merged.is_empty():
                for c in b.data:
                    merged.add_column(ColumnWithTypeAndName(c.name, c.type, c.column.clone()))
            else:
                merged.merge(b)
        return self._collapse_block(merged, columns, config)

    def _collapse_block(self, merged: Block, columns: Sequence[str],
                        config: Optional[Any] = None) -> Block:
        """对一个内存 Block 跑一遍"家族折叠"(合并期语义)。

        ``FINAL`` 的读时去重、``OPTIMIZE ... FINAL`` 的单 part 收敛、
        ``OPTIMIZE ... DEDUPLICATE`` 都走这一条路径 ——
        因为它们的语义**完全相同**:「把同键的多行按引擎家族的规则合成一行」。
        """
        from .merger import BlockRowSource, MergeTreeMergingIterator

        cfg = config or self.merge_config
        types = {n: self.table.get_column(n).type for n in columns}
        if merged.rows() == 0:
            return Block([ColumnWithTypeAndName(n, types[n], create_column_for_type(types[n]))
                          for n in columns])
        source = BlockRowSource(merged, types, self.sorting_key_exprs,
                               self.sorting_key_reverse)
        it = MergeTreeMergingIterator([source], cfg, self.key_column_names())
        out = Block([ColumnWithTypeAndName(n, types[n], create_column_for_type(types[n]))
                     for n in columns])
        for row in it.rows():
            for c in out.data:
                c.column.append(row[c.name])
        return out

    def _merge_events_into_global(self, events: Optional[ProfileEvents],
                                  before: Optional[Dict[str, int]] = None) -> None:
        """读取结束时:记下本次增量,并把它并进表级的累积计数。

        ``before`` 是读取开始时的快照 —— 有了它,即使计数器和调用方
        共用同一个对象,``last_read_events`` 拿到的也永远是**增量**。
        """
        if events is None:
            self.last_read_events = ProfileEvents()
            return
        delta = ProfileEvents.delta(events.snapshot(), before or {})
        last = ProfileEvents()
        for name, value in delta.items():
            last.set(name, value)
        self.last_read_events = last
        if events is not self.profile_events:
            self.profile_events.merge(last)

    def reset_part_log(self, sink: Optional[List[Dict[str, Any]]]) -> None:
        """接一个 ``part_log`` 的落地处 —— ``system.part_log`` 的数据源。"""
        self.part_log = sink

    def _log_part(self, event_type: str, part: MergeTreePart,
                  extra: Optional[Dict[str, Any]] = None) -> None:
        if self.part_log is None:
            return
        row = {
            "event_type": event_type,
            "event_time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "database": self.storage_id.database,
            "table": self.storage_id.table,
            "part_name": part.get_name(),
            "rows": part.rows_count(),
            "bytes_on_disk": part.bytes_on_disk(),
            "partition_id": part.get_partition_id(),
            "level": part.get_level(),
        }
        row.update(extra or {})
        self.part_log.append(row)

    # ══════════════════════════════════════════════════════════════════
    # 合并
    # ══════════════════════════════════════════════════════════════════

    def start_merges(self) -> None:
        """启动后台合并线程。

        .. code-block:: text

           真实 ClickHouse 用的是**线程池**(``background_pool_size`` 默认 16),
           每个线程从"待合并队列"里取任务 —— 所以并发合并是默认行为。
           mini-clickhouse 用一个**单线程**循环:语义一样
           (异步、可停、可等),但没有并发合并带来的锁竞争,
           在教学演示里更容易推理。差异标在下面的 warning 里。

        .. warning::

           单线程合并 ⇒ 合并吞吐上限比真实 CH 低。在"写入速率 > 合并
           吞吐"时,part 数会增长得更快,更容易撞到
           ``parts_to_throw_insert``。这是刻意的取舍:
           **可预测性优先于吞吐**。
        """
        if self.is_shutdown():
            raise_error(ErrorCode.ABORTED,
                        f"Table {self.full_table_name()} is shut down")
        if self._merge_thread is not None and self._merge_thread.is_alive():
            return
        self._merge_stop.clear()
        self._merge_thread = threading.Thread(
            target=self._merge_loop,
            name=f"MergeTree-merge-{self.storage_id.table}",
            daemon=True,
        )
        self._merge_thread.start()

    def stop_merges(self) -> None:
        """停止后台合并线程并等待它退出(**必须幂等**)。

        测试会反复 start/stop 引擎实例;线程泄漏会让测试进程永远不退出
        (daemon 线程虽然不会阻止退出,但会持续吃 CPU 并干扰下一个用例)。
        """
        self._merge_stop.set()
        self._merge_wakeup.set()
        t = self._merge_thread
        if t is not None and t.is_alive():
            t.join(timeout=30.0)
        self._merge_thread = None

    def wait_for_merges(self, timeout: float = 60.0) -> bool:
        """等到"没有正在跑的合并,且没有可合并的候选"。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._merge_cond:
                if self._merges_running == 0 and self._select_merge_candidate() is None:
                    return True
            time.sleep(0.005)
        return False

    def _merge_loop(self) -> None:
        while not self._merge_stop.is_set():
            try:
                candidate = self._select_merge_candidate()
                # 选中即占用 —— 前台 OPTIMIZE 也在挑候选,不预约的话两边
                # 会合同一批 part、写同一个目标名字,``os.replace`` 撞
                # ``PermissionError``(见 :meth:`_reserve_merge_candidate`)。
                if candidate is not None and self._reserve_merge_candidate(candidate):
                    self._run_merge(candidate)
                    continue
            except Exception as exc:  # pragma: no cover - 后台线程不能崩
                _log.error(f"Background merge failed: {exc}")
            self._merge_wakeup.wait(self._merge_interval_s)
            self._merge_wakeup.clear()

    def _reserve_merge_candidate(self, candidate: Any) -> bool:
        """把候选的输入 part **原子地**登记进 ``_parts_in_merge``。

        "筛选候选"与"占用候选"分成两步做,中间就有一个窗口:后台线程与
        前台 ``OPTIMIZE`` 可以同时看到同一批 part 并各自发起合并。两边的
        目标 part 名由 ``_merged_part_name`` 决定(输入相同 → 名字相同),
        于是第二个写盘的一方在 ``os.replace`` 上撞 ``PermissionError``。

        预约成功返回 ``True``;候选里只要有一个 part 已经不在活跃清单里、
        或者已被别的合并占用,返回 ``False``(调用方重挑)。
        """
        if candidate is None:
            return False
        parts = list(getattr(candidate, "parts", ()) or ())
        if len(parts) < 2:
            return False
        with self._lock:
            for p in parts:
                n = p.get_name()
                if n not in self.parts or n in self._parts_in_merge:
                    return False
            for p in parts:
                self._parts_in_merge.add(p.get_name())
            return True

    def _select_merge_candidate(self) -> Any:
        with self._lock:
            parts_by_partition: Dict[str, List[MergeTreePart]] = {}
            for pid, names in self.parts_by_partition.items():
                group = [self.parts[n] for n in names
                         if n in self.parts and n not in self._parts_in_merge]
                if len(group) >= 2:
                    parts_by_partition[pid] = group
        if not parts_by_partition:
            return None
        available = get_available_space(self.disk.get_root_path())[2]
        return self.merge_policy.select_for_partitions(parts_by_partition, available)

    def _run_merge(self, candidate: Any, ctx: Any = None,
                   events: Optional[ProfileEvents] = None,
                   apply_ttl: bool = True) -> Optional[MergeTreePart]:
        """执行一次合并(含 mutation、TTL 的合并期套用)。"""
        names = [p.get_name() for p in candidate.parts]
        with self._lock:
            for n in names:
                self._parts_in_merge.add(n)
        with self._merge_cond:
            self._merges_running += 1
        sw = Stopwatch()
        try:
            new_part = self._merge_parts_locked(candidate.parts, ctx, events, apply_ttl, names)
        finally:
            with self._lock:
                for n in names:
                    self._parts_in_merge.discard(n)
            with self._merge_cond:
                self._merges_running -= 1
                self._merge_cond.notify_all()
        self.merges_log.append({
            "parts": names,
            "result": getattr(new_part, "name", None),
            "elapsed_ms": round(sw.elapsed_ms(), 3),
        })
        return new_part

    def _merge_parts_locked(
        self,
        parts: Sequence[MergeTreePart],
        ctx: Any,
        events: Optional[ProfileEvents],
        apply_ttl: bool,
        input_names: Sequence[str],
    ) -> Optional[MergeTreePart]:
        events = events if events is not None else self.profile_events
        pending = self.mutations.undone()
        new_part = _merge_parts_impl(self, list(parts), ctx, events, mutations=pending)

        if apply_ttl and self.ttls:
            new_part = self._apply_ttl(new_part, ctx, events)

        if new_part is not None and not new_part.is_empty():
            # 合并时已经把 pending 里的 mutation 作用在**归并出来的每一行**上了,
            # 所以新 part 的"已应用集合" = 各输入 part 的并集 ∪ 这次做掉的。
            inherited: Set[str] = set()
            for p in parts:
                inherited |= set(p.applied_mutations)
            inherited |= {e.mutation_id for e in pending}
            new_part.applied_mutations = inherited
        with self._lock:
            for n in input_names:
                part = self.parts.get(n)
                if part is not None:
                    # 走 _obsolete_part:如果这个 part 还被某个在飞的读 pin 着,
                    # 目录会推迟到引用归还时再删(锁是可重入的,这里安全)。
                    self._obsolete_part(part)
            if new_part is not None and not new_part.is_empty():
                self._register_part(new_part)
                self._log_part("MergeParts", new_part,
                               {"merged_from": ", ".join(input_names)})
        # ★ 进度必须按**当前** part 重算,而不是照输入名字打勾 ——
        #   输入 part 可能已被别的 mutation 重写成新名字,
        #   照着旧名字"标记完成"会让 mutation 静默地什么都不做。
        for entry in pending:
            self._refresh_mutation_progress(entry)
        if new_part is None or new_part.is_empty():
            return None
        return new_part

    def _apply_ttl(self, part: Optional[MergeTreePart], ctx: Any,
                   events: Optional[ProfileEvents]) -> Optional[MergeTreePart]:
        """在合并结果上套用 TTL —— 这条路径与真实 CH 的"合并期 TTL"对应。

        三条规则:

        .. code-block:: text

           ① 整个 part 都过期(用 ttl.txt 里的 delete_at 一次比较)
              → **直接丢弃**,不重写。这是 ttl_only_drop_parts 想要的
                那个"快上百倍"的路径(见 ttl.py 的文档)。
           ② 只有部分行过期 → 读出所有行、过滤、写回新 part。
              代价与 part 大小成正比 —— **这就是为什么 TTL 要和分区配合**。
           ③ ``ttl_only_drop_parts = 1`` 且只有部分行过期 → **什么都不做**
              (宁可让数据多留一会儿,也不重写)。

        .. warning::

           **"整块丢弃"这条快路径必须比行级路径更保守 —— 两个 bug 曾经
           让它们不一致,而且都是"悄悄删掉不该删的行":**

           .. code-block:: text

             ① ``TTL ... DELETE WHERE id = 1``
                行级路径知道要检查 WHERE,而 part 级极值只反映"过期时间"
                → 一个 part 可能整体过期、但只有一部分行匹配 WHERE
                → 整块丢掉会连带删掉 **WHERE 排除掉的那些行**。
                → 现在:只要有任何一条 DELETE 规则带 WHERE,
                  **直接不走快路径**(见 TTLRowsRemover.has_row_filter)。

             ② ``TTL ts + INTERVAL 1 DAY`` 且 ``ts`` 是 ``Nullable``
                行级路径把 NULL 当"永不过期",而 part 级极值**跳过** NULL
                → "非 NULL 行全过期 + 有 NULL 行"的 part 看起来整块过期
                → NULL 行被误删。
                → 现在:NULL 按 ``TTL_NEVER_EXPIRES`` 参与 min/max,
                  于是 max 变成极大值 → 快路径自动放弃。

           .. code-block:: text

             结论:快路径的判据是"**这个 part 里每一行都必定过期**",
             而不是"极值看起来过期了"。任何"可能有例外"的情况
             (row filter / NULL)都必须退化成逐行判断。

           真实 ClickHouse 在 ``ttl_only_drop_parts = 0`` 且"只有部分行过期"
           时也是整份重写;当"整个 part 都过期"时它会先重写出一个空 part、
           再把它删掉(多一次无谓 IO)。mini-clickhouse 在**可以证明**
           整块过期时直接丢弃 —— 结果一致,少了那次多余的重写。
        """
        if part is None or part.is_empty():
            return None
        now = int(time.time())
        remover = TTLRowsRemover(self.ttls, now)
        info = _ttl_info_of(part, self.ttls, self, ctx)

        if remover.should_remove_anything(info):
            # 快路径的三个前提:① 没有 WHERE;② 极值证明整块过期;
            # ③ (NULL 已按 TTL_NEVER_EXPIRES 计入极值,见 ttl.py)
            if (not remover.has_row_filter()) and info is not None \
                    and info.whole_part_expired_for_delete(now):
                _log.information(
                    f"Dropping part {part.get_name()}: every row in it has expired "
                    f"(delete_at={info.delete_at}, now={now}) — this is the fast path, "
                    f"no rewrite happens"
                )
                self._remove_part(part)
                return None
            if int(self.settings.get("ttl_only_drop_parts", 0)) == 1:
                return part
            block = self._read_part_fully(part)
            before = block.rows()
            remover.process(block, ctx)
            if block.rows() == before:
                return part
            if block.rows() == 0:
                self._remove_part(part)
                return None
            if events is not None:
                events.increment("FilteredRows", before - block.rows())
            return self._rewrite_block(part, block, ctx, events)
        return part

    def _missing_column_default(self, cdef: Any, rows: int) -> Any:
        """缺列补默认值 —— 实现只有一份,在
        :func:`minich.storages.merge_tree.part.missing_column_default`。

        读路径(老 part 缺列)与**合并路径**(mutation/TTL/OPTIMIZE 的重写)
        必须走同一个实现,否则会出现"SELECT 看到 7,OPTIMIZE 之后变成 0"
        这种前后不一致 —— 而且后续 mutation 会在那一列上匹配不到任何行。
        """
        return missing_column_default(cdef, rows)

    def _read_part_fully(self, part: MergeTreePart) -> Block:
        """把一个 part 的**全部落盘列**读进内存(合并/重写用)。

        注意这里不能直接 ``part.read_column(name, ...)``:``ALTER ADD COLUMN``
        之后的老 part 里**没有**那个列的文件,会抛 ``NO_SUCH_COLUMN_IN_TABLE``。
        缺列必须走 :meth:`_missing_column_default` 现补 ——
        否则 ``OPTIMIZE`` 会在"给表加过列"的表上直接崩。
        """
        # 整体读取期间也借出这个 part —— TTL / mutation / 折叠都可能与
        # 后台合并线程并发,输入 part 的目录不能被抽走。
        self.pin_parts([part])
        try:
            columns = self.stored_column_names()
            marks = part.get_marks_count()
            block = Block()
            for name in columns:
                cdef = self.table.get_column(name)
                if cdef is None:
                    continue
                if part.has_column(name):
                    col = part.read_column(None, name, MarkRange(0, marks))
                else:
                    col = self._missing_column_default(cdef, part.rows)
                block.add_column(ColumnWithTypeAndName(name, cdef.type, col))
            return block
        finally:
            self.unpin_parts([part])

    def _rewrite_block(self, part: MergeTreePart, block: Block, ctx: Any,
                       events: Optional[ProfileEvents],
                       applied: Optional[Set[str]] = None) -> Optional[MergeTreePart]:
        """把一份"已经改过的行"写成新 part(level+1,名字唯一)。

        ``applied`` 是"这次重写顺带做掉了哪些 mutation" —— 新 part 会
        **继承源 part 的已应用集合**再并上它。少了这一步,后一条
        mutation 就会以为自己还没作用在这个 part 上(或者反过来,
        以为已经作用过了)—— 详见 :meth:`_apply_pending_mutations`。
        """
        pn = part.part_name
        name = format_part_name(pn.partition_id, pn.min_block, pn.max_block, pn.level + 1)
        if self.disk.exists_directory(f"{self.data_path}/{name}"):
            name = format_part_name(pn.partition_id, pn.min_block, pn.max_block,
                                    pn.level + 1 + 1)
        context = self.make_write_context(
            partition_value=part.partition_value, level=pn.level + 1,
            block_number=pn.max_block, events=events,
        )
        new_part = _write_part_impl(self.disk, f"{self.data_path}/{name}", block, context)
        new_part.applied_mutations = set(part.applied_mutations) | set(applied or ())
        with self._lock:
            self._obsolete_part(part)
            self._register_part(new_part)
            self._log_part("MergeParts", new_part, {"merged_from": part.get_name()})
        return new_part

    def optimize(self, final: bool = False, deduplicate: bool = False,
                 partition: Any = None) -> None:
        """``OPTIMIZE TABLE ... [PARTITION p] [FINAL] [DEDUPLICATE]``。

        .. code-block:: text

           OPTIMIZE TABLE t FINAL
              │
              └── 同步地反复合并,直到**每个分区只剩一个 part**
                  (或者合并策略再也挑不出候选)

           注意它与后台合并的关系:后台合并是"挑一个候选就返回",
           OPTIMIZE FINAL 是"**一直合到不能再合**"。
           所以 OPTIMIZE FINAL 对一张大表可能跑很久 ——
           真实 ClickHouse 也是,而且它会占用后台线程池的名额。

        ``partition`` 给定时只处理那个分区。
        """
        target_pid = _partition_id_of(partition) if partition is not None else None
        events = self.profile_events
        # ① **先套 TTL** —— 顺序很关键。
        #    如果先合并,一个"整个 part 都过期"的小 part 会被并进一个还没
        #    过期的大 part 里,于是"整 part 丢弃"这条 O(1) 快路径就永远
        #    走不到了,只能逐行删 + 整份重写(见 ttl.py 的对比图)。
        #    真实 ClickHouse 由 TTLMergeSelector 保证同样的效果:
        #    "宁可整块丢掉,也不去合并它"。
        if final and self.ttls:
            self._apply_ttl_to_all(events)
        guard = 0
        while True:
            guard += 1
            if guard > 10000:
                raise_error(ErrorCode.LOGICAL_ERROR,
                            "OPTIMIZE did not converge — merge selection is looping")
            with self._lock:
                candidates: List[List[MergeTreePart]] = []
                for pid, names in self.parts_by_partition.items():
                    if target_pid is not None and pid != target_pid:
                        continue
                    group = [self.parts[n] for n in names if n in self.parts]
                    if len(group) >= 2:
                        candidates.append(group)
            if not candidates:
                break
            progressed = False
            retry = False
            for group in candidates:
                available = get_available_space(self.disk.get_root_path())[2]
                if final or deduplicate:
                    # ★ FINAL/DEDUPLICATE 必须**一次合并整个分区**。
                    #
                    #   为什么不能让合并策略自己挑一小段? 看
                    #   CollapsingMergeTree 的一个真实反例:
                    #
                    #   .. code-block:: text
                    #
                    #      分区里有 3 个 part:
                    #        A = (+1, ver=1)     B = (-1, ver=1)     C = (+1, ver=2)
                    #
                    #      ① 若先合并 {A, C}:
                    #           (+1,1) 没有配对的 -1 → 保留
                    #           (+1,2) → 保留
                    #         → 2 行;接下来再和 B 合并:
                    #           (+1,1) 与 (-1,1) 抵消 → 只剩 (+1,2) ✓ 碰巧对了
                    #
                    #      ② 若先合并 {B, C}:
                    #           (-1,1) 单独一组 → sum(sign) < 0 → **整组丢弃**  ← 信息没了!
                    #           (+1,2) → 保留
                    #         → 只剩 (+1,2);再和 A 合并:
                    #           (+1,1) 仍然没有配对 → 保留
                    #         → **(+1,1) 与 (+1,2) 两行** ✗ 与 ① 的答案不同
                    #
                    #   也就是说:**折叠是有损的**,它对"合并顺序"敏感。
                    #   真实 ClickHouse 在文档里明说了这一点(结果取决于合并
                    #   顺序,可能不是确定的);但 ``OPTIMIZE ... FINAL`` 的
                    #   承诺是"收敛到一个确定的结果",所以它必须把整个分区
                    #   一次性归并 —— 只有"所有同键行同时在场",折叠才是良定义的。
                    cand = _force_candidate(group)
                else:
                    cand = self.merge_policy.select(group, available)
                    if cand is None:
                        # 策略挑不出(比如尺寸差距太大)→ 手工全量合并这个分区
                        cand = _force_candidate(group)
                # ★ 先把候选预约下来再动手 —— 否则后台合并线程可能在我们
                #   挑完之后、``_run_merge`` 登记之前抢先合并同一批 part,
                #   两边写出**同一个目标 part 名**,``os.replace`` 抛
                #   ``PermissionError``。预约失败说明后台正在处理这批 part,
                #   等它结束再重挑。
                if not self._reserve_merge_candidate(cand):
                    self.wait_for_merges(timeout=30.0)
                    retry = True
                    continue
                self._run_merge(cand, events=events, apply_ttl=True)
                progressed = True
            if not progressed:
                if retry:
                    # 候选被后台线程抢先拿走了 —— 重新挑一轮(part 清单已变)
                    continue
                break
        # 再处理"剩下的 pending mutation":单 part 也需要重写一次
        self._apply_pending_mutations(events)
        # ② 单 part 的家族折叠
        if final or deduplicate:
            self._collapse_single_parts(events, deduplicate)
        if final:
            self._apply_ttl_to_all(events)

    def _collapse_single_parts(self, events: Optional[ProfileEvents],
                               deduplicate: bool) -> int:
        """让 ``OPTIMIZE ... FINAL`` 在"一个分区只有一个 part"时也生效。

        .. code-block:: text

           ┌── 为什么单独需要这一步? ─────────────────────────────────┐
           │                                                          │
           │  合并循环只在"一个分区有 ≥2 个 part"时才有候选。          │
           │  但 ReplacingMergeTree 的**去重**发生在"同键的多行相遇"   │
           │  的时刻 —— 而它们完全可能**就在同一个 part 里**           │
           │  (一次 INSERT 里同一主键出现两次,是很常见的)。           │
           │                                                          │
           │  于是"一个分区一个 part"的表,如果只靠合并语义,          │
           │  那些重复行永远去不掉。                                   │
           │                                                          │
           │  真实 ClickHouse 对 OPTIMIZE FINAL 的承诺是              │
           │  "把每个分区收敛成一个 part,并且**把该折叠的折叠掉**"。   │
           │  所以这里显式地对单 part 也跑一遍家族折叠,               │
           │  只有**行数真的变了**才重写(避免无谓的 IO)。            │
           └──────────────────────────────────────────────────────────┘

        ``OPTIMIZE ... DEDUPLICATE`` 走同一条路,但用的是
        "按排序键去重"的语义(等价于一个没有 version 列的
        ReplacingMergeTree),而且**总是重写**(它就是被显式要求去重的)。
        """
        count = 0
        for pid in self.get_partition_ids():
            parts = self.get_parts_by_partition(pid)
            if len(parts) != 1:
                continue
            part = parts[0]
            block = self._read_part_fully(part)
            if block.rows() == 0:
                continue
            cfg = self.merge_config
            if deduplicate and cfg.family == "MergeTree":
                from dataclasses import replace as _dc_replace

                cfg = _dc_replace(cfg, family="Replacing", version_column="",
                                  is_deleted_column="")
            columns = list(self.stored_column_names())
            collapsed = self._collapse_block(block, columns, cfg)
            if collapsed.rows() == block.rows() and not deduplicate:
                continue
            if events is not None and collapsed.rows() != block.rows():
                events.increment("FilteredRows", block.rows() - collapsed.rows())
            if collapsed.rows() == 0:
                self._remove_part(part)
            else:
                self._rewrite_block(part, collapsed, None, events)
            count += 1
        return count

    def _apply_pending_mutations(self, events: Optional[ProfileEvents]) -> int:
        """把还没完成、又没被合并顺带做掉的 mutation 逐个补做。

        .. warning::

           **两条纪律,以前都违反过:**

           ① **目标必须是"当前的 part",不是登记时的快照。**
              mutation 登记时记下的是"当时有哪些 part";而前面一条
              mutation 执行完会**重写**这些 part(名字都变了)。
              如果第二条 mutation 还照着旧名字找,它就一个 part 都找不到 ——
              然后就**什么都没做却把自己标成 is_done**。

           ② **"完成"必须由"当前所有 part 都覆盖到了"来判定**,
              不能无条件 ``entry.is_done = True``。

           实现方式:每个 part 带一个 ``applied_mutations`` 集合
           (记录"哪些 mutation 已经作用在它身上了"),重写/合并时把
           集合传递下去。于是:

           .. code-block:: text

              目标 = [当前 part 中,entry.mutation_id ∉ part.applied_mutations]
              完成 = (目标为空)

           这个模型天然幂等:重复跑 OPTIMIZE 不会把同一条 mutation
           重复作用在同一个 part 上。
        """
        applied = 0
        for entry in self.mutations.undone():
            targets = [p for p in self.get_parts()
                       if entry.mutation_id not in p.applied_mutations]
            for part in targets:
                block = self._read_part_fully(part)
                for command in entry.commands:
                    from .mutations import apply_mutation_to_block

                    apply_mutation_to_block(command, block)
                if block.rows() == 0:
                    with self._lock:
                        self._remove_part(part)
                    continue
                self._rewrite_block(part, block, None, events,
                                    applied={entry.mutation_id})
                applied += 1
            self._refresh_mutation_progress(entry)
        return applied

    def _refresh_mutation_progress(self, entry: MutationEntry) -> None:
        """按**当前** part 重新算 ``parts_to_do`` —— 只有真覆盖完才算 done。"""
        remaining = [p.get_name() for p in self.get_parts()
                     if entry.mutation_id not in p.applied_mutations]
        entry.mark_parts(remaining)

    def _apply_ttl_to_all(self, events: Optional[ProfileEvents]) -> None:
        for part in list(self.get_parts()):
            self._apply_ttl(part, None, events)

    def _remove_part(self, part: MergeTreePart) -> None:
        self._obsolete_part(part)

    # ── part 引用计数(共享 part 的生命周期)────────────────────────────
    #
    # 真实 ClickHouse 的 part 是 ``shared_ptr``:一个查询读某个 part 时
    # 就持有一份引用,``clearOldParts`` 只把"没人引用的"从磁盘删掉。
    # 没有引用计数时,merge 一替换 part 就``remove_dir``,
    # **正在读它的查询会直接拿到 FileNotFoundError**。
    # 这里用显式的 pin/unpin 模拟引用计数;调用方:
    #
    #   * ``read()``          —— 一次 SELECT 的生命周期(含流式早停)
    #   * ``_read_part_fully`` —— 合并/TTL/mutation 期间的整体读取

    def pin_parts(self, parts: Sequence[MergeTreePart]) -> None:
        """借出一批 part(引用计数 +1)。"""
        if not parts:
            return
        with self._lock:
            for p in parts:
                n = p.get_name()
                self._part_pins[n] = self._part_pins.get(n, 0) + 1

    def unpin_parts(self, parts: Sequence[MergeTreePart]) -> None:
        """归还一批 part(引用计数 -1)。

        计数归零时,如果这个 part 已经被替换/删除(:meth:`_obsolete_part`
        把它登记进了 ``_pending_part_removals``),**现在**才真正删目录。
        """
        if not parts:
            return
        to_remove: List[MergeTreePart] = []
        with self._lock:
            for p in parts:
                n = p.get_name()
                c = self._part_pins.get(n, 0) - 1
                if c > 0:
                    self._part_pins[n] = c
                    continue
                self._part_pins.pop(n, None)
                pending = self._pending_part_removals.pop(n, None)
                if pending is not None:
                    to_remove.append(pending)
        for p in to_remove:
            self.disk.remove_dir(f"{self.data_path}/{p.get_name()}")

    def _obsolete_part(self, part: MergeTreePart) -> None:
        """把一个 part 从活跃清单挪进 ``obsolete_parts``。

        **被 pin 的 part 不删目录** —— 只登记成"待删",等
        :meth:`unpin_parts` 归还最后一个引用时才删。这样"在飞的读"
        永远看不到一个被抽走一半的 part 目录。

        .. note::

           本方法自己拿 ``self._lock``(可重入),所以既能在已持锁的
           代码里调用,也能单独调用;磁盘 IO 放在锁外。
        """
        name = part.get_name()
        remove_now = False
        with self._lock:
            self.parts.pop(name, None)
            self.obsolete_parts[name] = part
            lst = self.parts_by_partition.get(part.get_partition_id())
            if lst and name in lst:
                lst.remove(name)
            if self._part_pins.get(name):
                self._pending_part_removals[name] = part
            else:
                remove_now = True
        if remove_now:
            self.disk.remove_dir(f"{self.data_path}/{name}")

    # ══════════════════════════════════════════════════════════════════
    # mutation
    # ══════════════════════════════════════════════════════════════════

    def mutate(self, command: Any) -> str:
        """登记一条 mutation,返回 ``mutation_id``。

        .. code-block:: text

           登记 ≠ 执行。返回的那一刻:

             system.mutations.is_done      = 0
             system.mutations.parts_to_do  = 当前 part 数

           之后每次这些 part 参与**合并**(或显式 OPTIMIZE)时,
           这条 mutation 才被套用一次,parts_to_do 递减。

           → 这就是"UPDATE 一行可能要等很久"的全部原因。
             ``OPTIMIZE TABLE t FINAL`` 能立刻催它。

        ``command`` 可以是 AST 的 ``AlterCommand``,也可以是
        :class:`MutationCommand`(测试里直接构造更方便)。
        """
        if isinstance(command, MutationCommand):
            cmd = command
        else:
            cmd = build_mutation_command(command, self.table)
        mid = self.mutations.next_id()
        entry = MutationEntry(
            mid, [cmd],
            database=self.storage_id.database,
            table=self.storage_id.table,
            block_numbers=[self.block_number],
        )
        with self._lock:
            entry.mark_parts([p.get_name() for p in self.parts.values()])
        self.mutations.add(entry)
        self._merge_wakeup.set()
        return mid

    def mutation_status(self) -> List[Dict[str, Any]]:
        """``system.mutations`` 的行。"""
        return self.mutations.all_rows()

    def wait_for_mutations(self, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.mutations.undone():
                return True
            self.optimize(final=False)
            time.sleep(0.005)
        return not self.mutations.undone()

    # ══════════════════════════════════════════════════════════════════
    # DDL / ALTER
    # ══════════════════════════════════════════════════════════════════

    def truncate(self) -> None:
        """``TRUNCATE TABLE`` —— 删掉所有 part。

        被在飞的读 pin 住的 part 只登记待删,目录等引用归还后再删。
        """
        for part in list(self.parts.values()):
            self._obsolete_part(part)
        with self._lock:
            self.parts.clear()
            self.parts_by_partition.clear()

    def alter_add_column(self, column: Any, after: Optional[str] = None) -> None:
        """``ALTER ADD COLUMN`` —— **纯元数据操作,不重写任何 part**。

        .. code-block:: text

           为什么不用重写?

             旧 part 里没有这一列 —— 但读的时候可以**现补默认值**:
             ``MergeTreePart.missing_columns`` 记着"表里有、part 里没有"的列,
             ``MergeTreePartReader`` 读到它时补零值。

             于是"给一张 100 亿行的表加一列"是**毫秒级**的 ——
             这是列存相对行存的一个决定性优势(行存要 ALTER TABLE + 回填)。

           代价:老数据读出来是"默认值",而不是"当时应该有的值"。
           如果那一列是 MATERIALIZED 且依赖别的列,读时计算能给出正确值;
           所以真实 CH 建议 ALTER 加 MATERIALIZED 列而不是普通列。
        """
        super().alter_add_column(column, after)

    def alter_drop_column(self, name: str) -> None:
        if name in self.key_column_names():
            raise_error(
                ErrorCode.ILLEGAL_COLUMN,
                f"Cannot drop column {name!r}: it is used in the sorting key",
            )
        super().alter_drop_column(name)

    def alter_modify_column(self, name: str, new_type: DataType,
                            default_expr: Any = None) -> None:
        """``ALTER MODIFY COLUMN`` —— 只改元数据;老 part 的数据**不转换**。

        .. warning::

           真实 ClickHouse 会拒绝"有损的"类型修改(比如 ``String`` →
           ``UInt8``),除非显式 ``ALTER ... MODIFY COLUMN c UInt8`` 且
           ``allow_experimental...`` 之类的开关。mini-clickhouse 只改元数据,
           于是"老 part 读出来时用新类型反序列化" —— 如果新类型与旧数据
           的字节布局不兼容,会读到垃圾或报错。这属于**已知差异**,
           在教程里标出(真实 CH 会在后台做一次 mutation 转换数据)。
        """
        super().alter_modify_column(name, new_type, default_expr)

    def alter_rename_column(self, old: str, new: str) -> None:
        """``ALTER RENAME COLUMN`` —— 纯元数据,part 里的**文件名不变**。

        真实 ClickHouse 也是这样:part 里的列文件名是老名字,
        靠 ``columns.txt`` 与实际文件名的映射来读 —— 所以重命名是
        瞬间完成的。mini-clickhouse 简化成"改 columns.txt 的解释",
        读的时候用**新名字**去找文件(文件还是老的)。为了不让读失败,
        这里顺手把已有 part 的 ``column_types`` 键改掉,并把旧文件
        改名(代价 = 每个 part 一次 rename,仍然是廉价的元数据操作)。
        """
        super().alter_rename_column(old, new)
        with self._lock:
            for part in self.parts.values():
                if part.has_column(old):
                    t = part.column_types.pop(old)
                    part.column_types[new] = t
                    if old in part.marks:
                        part.marks[new] = part.marks.pop(old)
                    # 物理文件改名(真实 CH 不做这一步,我们为了读路径简单而做)
                    for suffix in (".bin", ".mrk2"):
                        src = f"{self.data_path}/{part.name}/{old}{suffix}"
                        dst = f"{self.data_path}/{part.name}/{new}{suffix}"
                        if self.disk.exists(src):
                            self.disk.move(src, dst)
                    part.column_names = [new if n == old else n for n in part.column_names]

    def drop_partition(self, partition_value: Any) -> int:
        """``ALTER DROP PARTITION`` —— **O(1) 的删除**,列存最爽的操作。

        .. code-block:: text

           删掉 2024 年 3 月的全部数据:

             行存: DELETE FROM t WHERE d >= '2024-03-01' AND d < '2024-04-01'
                     → 逐行扫描 + 逐行标记 + 之后 vacuum 回收
                     → 几十 GB 的 IO,而且锁表

             列存: ALTER TABLE t DROP PARTITION 202403
                     → 删掉几个目录(每个月的 part)
                     → **元数据操作,毫秒级**

           这就是"分区是给运维用的,不是给查询用的"这句话的由来:
           分区最大的价值是**可整块删除**,其次才是裁剪。
        """
        pid = _partition_id_of(partition_value)
        removed = 0
        with self._lock:
            names = list(self.parts_by_partition.get(pid, []))
            for n in names:
                part = self.parts.get(n)
                if part is None:
                    continue
                self._obsolete_part(part)
                removed += 1
            self.parts_by_partition[pid] = []
        return removed

    def drop_part(self, part_name: str) -> bool:
        with self._lock:
            part = self.parts.get(part_name)
            if part is None:
                return False
            self._remove_part(part)
            return True

    def detach_partition(self, partition_value: Any) -> int:
        """``DETACH PARTITION`` —— 把 part 挪出数据目录(可再 ATTACH 回来)。"""
        pid = _partition_id_of(partition_value)
        detach_dir = f"{self.data_path}/detached"
        self.disk.create_dir(detach_dir)
        moved = 0
        for part in self.get_parts_by_partition(pid):
            src = f"{self.data_path}/{part.name}"
            dst = f"{detach_dir}/{part.name}"
            self.disk.move(src, dst)
            with self._lock:
                self.parts.pop(part.get_name(), None)
            moved += 1
        with self._lock:
            self.parts_by_partition[pid] = []
        return moved

    def attach_partition(self, partition_value: Any) -> int:
        """``ATTACH PARTITION`` —— 把 ``detached/`` 里的 part 挂回来。"""
        pid = _partition_id_of(partition_value)
        detach_dir = f"{self.data_path}/detached"
        if not self.disk.exists_directory(detach_dir):
            return 0
        attached = 0
        for entry in self.disk.list_dir(detach_dir):
            try:
                info = parse_part_name(entry)
            except MiniChError:
                continue
            if info.partition_id != pid:
                continue
            self.disk.move(f"{detach_dir}/{entry}", f"{self.data_path}/{entry}")
            part = load_part(
                self.disk, f"{self.data_path}/{entry}", self.table,
                indexes=self.index_descriptions,
                partition_key_types=self.partition_key_types,
                sorting_key_reverse=self.sorting_key_reverse,
                primary_key_columns=self._primary_key_column_names(),
                primary_key_types=self._primary_key_types_safe(),
                table_columns=self.table.columns,
            )
            self._register_part(part)
            attached += 1
        return attached

    def modify_ttl(self, ttls: Sequence[Any]) -> None:
        """``ALTER MODIFY TTL`` —— 只改元数据,下一次合并时生效。"""
        self.ttls = _as_ttl_list(ttls)

    def modify_setting(self, name: str, value: Any) -> None:
        """``ALTER TABLE ... MODIFY SETTING``。

        .. warning::

           **mini-clickhouse 明确拒绝它"接下了却不会执行"的设置。**

           这些设置要么依赖我们没有的子系统(线程池、LRU 淘汰、
           TTL 调度器),要么只影响真实 CH 的内部行为:

           * ``background_pool_size`` —— 我们只有一个合并线程;
           * ``number_of_free_entries_in_pool_to_execute_mutation``;
           * ``merge_with_ttl_timeout`` —— 我们没有 TTL 定时器;
           * ``min_insert_block_size_rows`` / ``min_insert_block_size_bytes``
             —— 我们只按 ``max_insert_block_size`` 切块;
           * ``old_parts_lifetime`` —— 我们的 obsolete part 立即删除;
           * ``finished_mutations_to_keep``;
           * ``optimize_read_in_order`` —— 我们总是按排序键顺序读。

           接受它们然后**什么都不做**是最糟的选择:用户会以为
           "我把并发合并开到 32 了",而实际上什么都没变 ——
           这类"沉默的谎言"比直接报错难排查得多。
        """
        if name not in DEFAULT_SETTINGS:
            raise_error(
                ErrorCode.UNKNOWN_SETTING,
                f"Unknown MergeTree setting {name!r}. Known: "
                f"{', '.join(sorted(DEFAULT_SETTINGS))}",
            )
        if value is None:
            # ``SETTINGS x = DEFAULT`` / ``ALTER ... RESET SETTING x``
            # 都走这条路 —— 语义是"恢复默认值",不是"设成 NULL"。
            self.reset_setting(name)
            return
        if name in _KNOWN_BUT_IGNORED_SETTINGS:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"SETTING {name} is recognized but NOT implemented in "
                f"mini-clickhouse (see the modify_setting docstring for the "
                f"full list). Refusing to accept it silently, because doing so "
                f"would make you believe the behaviour changed when it did not.",
            )
        self.settings[name] = value

    def reset_setting(self, name: str) -> None:
        if name not in DEFAULT_SETTINGS:
            raise_error(ErrorCode.UNKNOWN_SETTING, f"Unknown setting {name!r}")
        if name in _KNOWN_BUT_IGNORED_SETTINGS:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"SETTING {name} is recognized but NOT implemented in "
                f"mini-clickhouse; there is nothing to reset.",
            )
        self.settings[name] = DEFAULT_SETTINGS[name]

    def add_index(self, index: Any) -> None:
        """``ALTER TABLE ... ADD INDEX`` —— **只改元数据**。

        .. warning::

           **已有的 part 不会因此获得这个索引。** 它们里面没有
           ``skp_idx_<name>.idx``,读的时候 ``index_readers`` 是空的,
           于是"跳过"这件事不会发生。

           这不是偷懒,而是真实 ClickHouse 的语义:``ADD INDEX`` 只让
           **之后写入/合并出来的 part** 带上索引(所以它快到毫秒级)。
           要让老数据也享受索引,必须再跑
           ``ALTER TABLE ... MATERIALIZE INDEX``(见
           :meth:`materialize_index`)或者等 part 被合并重写。
        """
        self.index_descriptions.append(index)
        self._bind_index_types()

    def alter_index(self, command: Any) -> Any:
        """``ADD INDEX`` / ``DROP INDEX`` / ``MATERIALIZE INDEX`` 的统一入口。

        (真实 ClickHouse 的 ``InterpreterAlterQuery`` 会按 ``AlterCommand``
        的 ``type`` 分派到 ``StorageMergeTree::alter`` 里的不同分支;
        我们把这个分派放在引擎里,这样解释器只需要传一个命令对象。)

        .. code-block:: text

           ADD INDEX idx k TYPE minmax GRANULARITY 1     → 只改元数据
           DROP INDEX idx                                → 只改元数据
           MATERIALIZE INDEX idx                         → **重写所有 part**

        ``command.index`` 是语法树的 ``IndexDefinition``,``command.name``
        用于 DROP/MATERIALIZE(两种写法都接受)。
        """
        kind = str(getattr(command, "kind", "")).upper()
        ast_index = getattr(command, "index", None)
        if kind == "ADD_INDEX":
            if ast_index is None:
                raise_error(ErrorCode.LOGICAL_ERROR,
                            "ALTER ADD_INDEX without an index definition")
            desc = IndexDescription(
                name=ast_index.name,
                expr_sql=_sql_of(ast_index.expr),
                expr=ast_index.expr,
                index_type=ast_index.type,
                params=list(ast_index.type_params or []),
                granularity=int(ast_index.granularity or 1),
            )
            if any(d.name == desc.name for d in self.index_descriptions):
                raise_error(
                    ErrorCode.DUPLICATE_COLUMN,
                    f"Index {desc.name!r} already exists in "
                    f"{self.full_table_name()}",
                )
            self.add_index(desc)
            return desc
        name = getattr(command, "name", None) or \
            getattr(ast_index, "name", None) or ""
        if kind == "DROP_INDEX":
            if not name:
                raise_error(ErrorCode.LOGICAL_ERROR,
                            "ALTER DROP_INDEX without an index name")
            if not self.drop_index(name):
                raise_error(ErrorCode.THERE_IS_NO_COLUMN,
                            f"There is no index named {name!r}")
            return True
        if kind == "MATERIALIZE_INDEX":
            if not name:
                raise_error(ErrorCode.LOGICAL_ERROR,
                            "ALTER MATERIALIZE_INDEX without an index name")
            return self.materialize_index(name)
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"alter_index does not handle command kind {kind!r}",
        )

    def modify_order_by(self, expr: Any) -> None:
        """``ALTER TABLE ... MODIFY ORDER BY``。

        .. warning::

           **只在空表上允许。** 改变排序键意味着已有的 part
           是按**旧顺序**排好的 —— 继续用旧顺序读,新的
           ``primary.idx`` 描述就与实际数据不符,裁剪会给出**错误结果**。

           真实 ClickHouse 的做法是直接拒绝:

           .. code-block:: text

              Code: 524. DB::Exception: Cannot MODIFY ORDER BY: the table
              has data parts. ...

           (想改就得重建表、重新灌数据。)mini-clickhouse 照抄这个限制;
           空表上改是安全的,所以允许。
        """
        if self.parts:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Cannot MODIFY ORDER BY of {self.full_table_name()}: "
                f"the table already has {len(self.parts)} data part(s), whose rows "
                f"are sorted by the OLD key. Recreate the table and re-insert.",
            )
        exprs = _as_expr_list(expr if not hasattr(expr, "elements") else expr.elements)
        if not exprs:
            raise_error(ErrorCode.SYNTAX_ERROR, "MODIFY ORDER BY needs an expression")
        reverse = _directions_of(exprs)
        header = self.table.header()
        self.sorting_key_reverse, self.key_condition_info = _build_key_info(
            header, exprs, reverse)
        self.sorting_key_exprs = exprs
        self.primary_key_exprs = exprs

    def drop_index(self, name: str) -> bool:
        """``ALTER TABLE ... DROP INDEX`` —— 从元数据里摘掉。

        老 part 里的 ``skp_idx_<name>.idx`` **文件仍然留在磁盘上**
        (真实 CH 也是这样:它不做清理,靠后续重写自然消失),
        但读路径不再加载它们,所以它不再影响任何查询。
        """
        before = len(self.index_descriptions)
        self.index_descriptions = [d for d in self.index_descriptions if d.name != name]
        return len(self.index_descriptions) != before

    def freeze(self, partition_value: Any = None,
               backup_name: Optional[str] = None) -> int:
        """``ALTER TABLE ... FREEZE [PARTITION p]`` —— 给 part 做一份快照。

        真实 ClickHouse 的做法是**硬链接**:

        .. code-block:: text

           data/default/t/202403_1_1_0/id.bin
                    │  ln(2)
                    ▼
           shadow/<backup_name>/data/default/t/202403_1_1_0/id.bin
                    ↑ 同一个 inode,零字节拷贝,毫秒级完成

           于是"备份"变成了一次目录操作 —— 这是 ClickHouse 敢在
           TB 级表上做高频快照的原因。之后把 ``shadow/`` 打包走即可
           (``clickhouse-backup`` 就是这么干的)。

        .. warning::

           **mini-clickhouse 用"拷贝"代替"硬链接"**,因为硬链接在
           Windows 上需要管理员权限、跨平台行为也不一致。
           代价是:

           * 快照耗时与数据量成正比(真实 CH 是 O(part 数));
           * 快照占额外的磁盘空间(真实 CH 直到源 part 被删才占空间)。

           语义(快照之后源 part 再变也不影响快照)是**一样的**。

        返回被冻结的 part 数。
        """
        backup = backup_name or f"backup_{int(time.time())}"
        base = f"{self.data_path}/shadow/{backup}"
        self.disk.create_dir(base)
        targets = self.get_parts() if partition_value is None else \
            self.get_parts_by_partition(_partition_id_of(partition_value))
        count = 0
        for p in targets:
            src_dir = f"{self.data_path}/{p.get_name()}"
            dst_dir = f"{base}/{p.get_name()}"
            self.disk.create_dir(dst_dir)
            for entry in self.disk.list_dir(src_dir):
                src = f"{src_dir}/{entry}"
                if self.disk.exists_directory(src):
                    continue
                self.disk.write_file(f"{dst_dir}/{entry}", self.disk.read_file(src))
            count += 1
        _log.information(
            f"FREEZE: snapshotted {count} part(s) into {base} "
            f"(real ClickHouse hard-links instead of copying)"
        )
        return count

    def unfreeze(self, backup_name: str, partition_value: Any = None) -> int:
        """``ALTER TABLE ... UNFREEZE WITH NAME`` —— 删掉一份快照。"""
        base = f"{self.data_path}/shadow/{backup_name}"
        if not self.disk.exists_directory(base):
            return 0
        count = 0
        for entry in self.disk.list_dir(base):
            if partition_value is not None:
                try:
                    if parse_part_name(entry).partition_id != _partition_id_of(partition_value):
                        continue
                except MiniChError:
                    continue
            self.disk.remove_dir(f"{base}/{entry}")
            count += 1
        return count

    def shadow_parts(self) -> List[str]:
        """列出所有快照 —— ``system.backup_log`` 之类的地方会用到。"""
        base = f"{self.data_path}/shadow"
        if not self.disk.exists_directory(base):
            return []
        return [f"{base}/{b}" for b in self.disk.list_dir(base)]

    def materialize_index(self, name: str) -> int:
        """``ALTER TABLE ... MATERIALIZE INDEX idx`` —— 给**已有 part** 补建索引。

        .. code-block:: text

           ADD INDEX idx ...              ← 只改元数据:
                                            **新写入的 part** 会带上这个索引,
                                            老 part 一个字节都不动(毫秒级)

           MATERIALIZE INDEX idx          ← 把**已有 part 重写一遍**,
                                            于是老数据也能被这个索引加速
                                            (代价 = 重写整张表的 part)

           这正是真实 ClickHouse 的语义,也是"为什么加了索引查询没变快"
           这个经典困惑的答案:你还需要 MATERIALIZE(或者等 part 被合并/重写)。

        返回被重写的 part 数。
        """
        desc = next((d for d in self.index_descriptions if d.name == name), None)
        if desc is None:
            raise_error(
                ErrorCode.THERE_IS_NO_COLUMN,
                f"There is no index named {name!r} in table {self.full_table_name()}. "
                f"Known indexes: "
                f"{', '.join(d.name for d in self.index_descriptions) or '(none)'}",
            )
        if desc.data_type is None:
            self._bind_index_types()
        rewritten = 0
        for part in list(self.get_parts()):
            block = self._read_part_fully(part)
            if block.rows() == 0:
                continue
            self._rewrite_block(part, block, None, None)
            rewritten += 1
        return rewritten

    # ══════════════════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════════════════

    def startup(self) -> None:
        self.start_merges()

    def shutdown(self) -> None:
        """停止后台线程 —— **幂等**,而且必须真的把线程收干净。"""
        if self.is_shutdown():
            # 幂等:第二次调用也要确保线程停了(测试里 DROP + 析构都会调)
            self.stop_merges()
            return
        self._shutdown = True
        self.stop_merges()

    def flush(self) -> None:
        return

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<{type(self).__name__} {self.full_table_name()} "
                f"parts={len(self.parts)} rows={self.row_count()}>")


# ══════════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════════


def _normalize_read_context(ctx: Any, kwargs: Dict[str, Any]) -> ReadContext:
    if isinstance(ctx, ReadContext):
        rc = ctx
    else:
        rc = ReadContext(ctx=ctx)
    if not isinstance(ctx, ReadContext) and ctx is not None:
        # 允许把 keyword 直接塞进来(教程与测试的用法)
        pass
    for k, v in kwargs.items():
        if hasattr(rc, k):
            setattr(rc, k, v)
    return rc


def _new_read_events(data: MergeTreeData, rc: ReadContext) -> ProfileEvents:
    """一次读取要往里累加计数器的那个 ``ProfileEvents``。

    .. code-block:: text

       优先用**调用方(执行器)的**那一个:

         ReadContext.profile_events  →  rc.ctx.profile_events
                                        (也就是 system.query_log 里那份)

       为什么必须共用同一个对象? 因为 ``SelectedMarks`` 这类计数器的
       全部价值就在于"用户能在 ``system.query_log`` / ``EXPLAIN`` 里看见它"。
       如果引擎自己攒一份、外面看不到,那教程里
       "对比 SelectedMarks 证明索引在干活"的演示就永远做不出来。

       拿不到就新建一个(直连引擎的测试、教程脚本走这条路),
       并在读取结束时 merge 进 ``data.profile_events``。

    ``data.last_read_events`` 里保存的是**本次读取的增量**
    (读取前后各取一次快照相减),语义与 ``system.query_log.ProfileEvents``
    一致 —— 共享对象也不会把历史值带进来。
    """
    for holder in (getattr(rc, "profile_events", None),
                   getattr(getattr(rc, "ctx", None), "profile_events", None)):
        if isinstance(holder, ProfileEvents):
            return holder
    if isinstance(rc.query_info, ProfileEvents):
        return rc.query_info
    return ProfileEvents()


def _query_setting(data: "MergeTreeData", rc: ReadContext, name: str,
                   default: Any) -> Any:
    """读一个设置 —— **查询级优先于表级**。

    真实 ClickHouse 的设置是四层的,后一层覆盖前一层:

    .. code-block:: text

       ① 服务器默认(``config.xml`` / ``SettingDef`` 里的默认值)
       ② **表级**设置(``CREATE TABLE ... SETTINGS x = y`` 里的)
       ③ **会话**级(``SET x = y``)
       ④ **查询**级(``SELECT ... SETTINGS x = y``)—— 优先级最高

    这就是为什么 ``SELECT ... SETTINGS use_skip_indexes = 0`` 能覆盖
    一张表上写死的 ``SETTINGS use_skip_indexes = 1`` ——
    "临时改一下行为"不必去改表定义。

    引擎自己能看到 ②,也能从 ``ReadContext`` 看到 ③④
    (:attr:`ReadContext.settings`,或者 ``rc.ctx.settings`` / ``rc.query_info``)。
    这一层"谁覆盖谁"的逻辑必须由**引擎**实现,因为只有它同时知道
    表级设置与本次查询的设置。
    """
    for holder in (getattr(rc, "settings", None),
                   getattr(getattr(rc, "ctx", None), "settings", None),
                   getattr(rc.query_info, "settings", None)
                   if rc.query_info is not None else None):
        if holder is None:
            continue
        v = _setting(holder, name, None)
        if v is not None:
            return v
    return data.settings.get(name, default)


def _collect_where(rc: ReadContext, kwargs: Dict[str, Any]) -> Any:
    """找到本次读取的 ``WHERE`` 谓词(可能为 None)。

    引擎需要它做三件事 —— 缺了它就**一件也做不了**:

    .. code-block:: text

       ① 主键区间裁剪   → WHERE 里关于排序键前缀的那些比较
       ② 分区裁剪       → WHERE 里关于分区键的那些比较
       ③ 跳过索引       → WHERE 里关于 skp_idx 表达式列的那些比较

    搜索顺序(从最明确到最隐式):

    .. code-block:: text

       ① kwargs['where'] / kwargs['prewhere']       直接调引擎时(教程/测试)
       ② rc.where / rc.prewhere                     ReadContext 上的显式字段
                                                    —— **推荐执行器用这一条**
       ③ rc.ctx.where / rc.query_info.where         执行上下文里挂着的
       ④ rc.key_analysis 已经给了                   → 不用再自己解析(返回 None)

    ② 之所以存在,是因为 ``ReadContext`` 是一个普通 dataclass ——
    执行器可以只加一行 ``read_ctx.where = node.where`` 就把谓词交给引擎,
    不需要改任何接口。**没有这一步,存储层的索引裁剪在 SQL 路径上就是死的**
    (引擎看不到 WHERE,自然无从裁剪),而症状恰恰是"EXPLAIN 里
    完全看不出 SelectedMarks 的变化"。
    """
    for key in ("where", "prewhere"):
        if key in kwargs and kwargs[key] is not None:
            return kwargs[key]
    for attr in ("where", "prewhere"):
        v = getattr(rc, attr, None)
        if v is not None:
            return v
    if rc.key_analysis is not None:
        return None   # 已经有 key_analysis 了,不需要再自己解析
    for holder in (rc.ctx, rc.query_info):
        if holder is None:
            continue
        for attr in ("where", "prewhere"):
            v = getattr(holder, attr, None)
            if v is not None:
                return v
    return None


def _resolve_codec_name(text: str) -> str:
    """把列上声明的 ``CODEC(...)`` 文本解析成**单个**编解码器名。

    返回的名字要能被 :func:`minich.storages.merge_tree.part._named_codec` 直接认出
    ——它只按**单个名字**取编解码器(见 ``part.py`` 的 ``write_compressed_block``)。

    * **单编解码器**(``CODEC(NONE)`` / ``CODEC(ZSTD(3))``)—— 取它自己;
    * **链式**(``CODEC(Delta, ZSTD(3))``)—— 取**链尾的通用编解码器**。

    .. warning::

       **链式 CODEC 是一处刻意的简化,不是完整的链式压缩。**

       真实 ClickHouse 会把整条链(前置变换 ``Delta``/``Gorilla``/… → 通用压缩
       ``LZ4``/``ZSTD`` → 加密)逐层套用,并把整条链写进块里。本实现的压缩块头只有
       **一个** method 字节(见 ``part.py`` 的 ``write_compressed_block``,以及读路径的
       ``_codec_by_method`` ——它只认单个编解码器),**表达不了链**。

       所以我们只保留**最后一环(通用编解码器)真正去压**,前面那些"只改布局、
        不压缩"的前置变换被跳过。数据因此能正确往返(压缩用的就是链尾那个
        codec),但压缩率不如真实 CH。**绝不做的事**:像修前那样把整条声明静默丢掉、
        统一按 LZ4 写 —— 那是"沉默的谎言"。

       解析 / 校验交给 ``CompressionCodecFactory.parse_codec_chain``:它会拒绝
       ``CODEC(Delta)`` 这种"专用编解码器后面没有通用编解码器"的非法链
       (抛 ``ILLEGAL_TYPE_OF_ARGUMENT``,与真实 ClickHouse 一致),不会静默吞掉。
    """
    from ...compression import CompressionCodecFactory

    chain = CompressionCodecFactory.parse_codec_chain(text)
    # 单编解码器直接用它;链式取链尾 —— parse_codec_chain 已保证链尾是通用编解码器。
    return chain[0].name if len(chain) == 1 else chain[-1].name


def _as_expr_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _directions_of(value: Any) -> List[bool]:
    """从 ``ORDER BY`` 的元素里抽出方向(``DESC`` → True)。"""
    out: List[bool] = []
    for e in _as_expr_list(value):
        direction = getattr(e, "direction", "ASC")
        out.append(str(direction).upper() == "DESC")
    return out


def _as_ttl_list(value: Any) -> List[TTLDescription]:
    if not value:
        return []
    out: List[TTLDescription] = []
    for t in (value if isinstance(value, (list, tuple)) else [value]):
        if isinstance(t, TTLDescription):
            out.append(t)
            continue
        out.append(TTLDescription(
            action=str(getattr(t, "action", "DELETE") or "DELETE"),
            expr=getattr(t, "expr", None),
            target=getattr(t, "target", None),
            codec=getattr(t, "codec", None),
            where=getattr(t, "where", None),
            group_by=list(getattr(t, "group_by", []) or []),
            text=_sql_of(t),
        ))
    return out


def _build_key_info(
    header: Block,
    exprs: Sequence[Any],
    reverse: Sequence[bool],
) -> Tuple[List[bool], KeyConditionBuildInformation]:
    """构造 :class:`KeyConditionBuildInformation`。

    只取**能描述的前缀**:一旦某个排序键表达式不被支持(比如
    ``ORDER BY (a, b, c)`` 里的 ``c`` 是 ``a * b``),就把它**之后的**
    全部丢掉 —— 因为排序键是"前缀有序"的,
    第 k 个表达式不能用时,第 k+1 个也就无法定位了。
    """
    info = KeyConditionBuildInformation()
    rev: List[bool] = []
    for i, e in enumerate(exprs):
        try:
            sql, col, func, rtype = describe_key_expression(e, header)
        except MiniChError:
            break
        info.key_exprs.append(sql)
        info.key_types.append(rtype)
        rev.append(bool(reverse[i]) if i < len(reverse) else False)
        info.key_columns.append(col or "")
        info.key_functions.append(func)
    info.reverse = rev
    info.description = ", ".join(info.key_exprs)
    return rev, info


def _types_of(header: Block, exprs: Sequence[Any]) -> List[DataType]:
    out: List[DataType] = []
    for e in exprs:
        try:
            out.append(evaluate_expression(e, header).type)
        except Exception:
            out.append(get_data_type("String"))
    return out


def _expr_column_name(expr: Any) -> str:
    from ...sql import ast_ as A

    if isinstance(expr, A.Identifier):
        return expr.name
    if isinstance(expr, A.FunctionCall) and len(expr.args) == 1 and \
            isinstance(expr.args[0], A.Identifier):
        return expr.args[0].name
    if isinstance(expr, A.CastExpr):
        return _expr_column_name(expr.expr)
    return ""


def _expr_sql(expr: Any) -> str:
    return _sql_of(expr)


def _sql_of(node: Any) -> str:
    try:
        return node.to_sql()
    except Exception:  # pragma: no cover
        return repr(node)


def _expression_dependencies(cdef: Any) -> List[str]:
    """一个非落盘列依赖哪些落盘列(最多解析到表达式层面)。"""
    sql = getattr(cdef, "default_expr_sql", None)
    if not sql:
        return []
    try:
        expr = column_expression(cdef)
    except MiniChError:
        return []
    return _ast_dependencies(expr)


def _ast_dependencies(node: Any) -> List[str]:
    """AST 里引用到的所有 ``Identifier`` 名字(粗略但安全的多读)。"""
    if node is None:
        return []
    from ...sql import ast_ as A

    out: List[str] = []
    seen: Set[int] = set()

    def walk(n: Any) -> None:
        if n is None or id(n) in seen:
            return
        seen.add(id(n))
        if isinstance(n, A.Identifier):
            if n.name not in out:
                out.append(n.name)
        for c in n.children():
            walk(c)

    walk(node)
    return out


def _resolve_disk(params: Dict[str, Any], context: Any) -> IDisk:
    disk = params.get("disk")
    if disk is not None:
        return disk
    disk = getattr(context, "disk", None)
    if disk is not None:
        return disk
    return DiskLocal(_resolve_base_path(params, context))


def _resolve_base_path(params: Dict[str, Any], context: Any) -> str:
    """决定"这块盘的根目录在哪"。

    优先级(从高到低)—— 每一层都是"更明确的一方胜出":

    .. code-block:: text

       params['path'] / params['data_path'] / params['root_path']
         ↑ 建表参数(``CREATE TABLE ... SETTINGS`` 或解释器直接给的)
       context.data_path / context.path / context.root_path
         ↑ 会话/服务器上下文(真实 CH 的 ``Context::getPath``)
       默认:<系统临时目录>/minich-clickhouse-data
         ↑ 教学默认值 —— **绝不写进你自己的项目目录**

    .. warning::

       默认落在系统临时目录是刻意的:测试与教程会反复建表,
       写进仓库会让 ``git status`` 一片脏。真实 ClickHouse 用的是
       ``/var/lib/clickhouse/data``(由 ``path`` 配置项决定)。
    """
    for holder, attr in (
        (params, "path"), (params, "data_path"), (params, "root_path"),
        (context, "data_path"), (context, "path"), (context, "root_path"),
    ):
        v = holder.get(attr) if isinstance(holder, dict) else getattr(holder, attr, None)
        if v:
            return str(v)
    return os.path.join(tempfile.gettempdir(), "minich-clickhouse-data")


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _partition_id_of(value: Any) -> str:
    from .part import format_partition_id

    if value is None:
        return "all"
    if isinstance(value, str):
        return value
    return format_partition_id(value)


def _force_candidate(parts: Sequence[MergeTreePart]) -> Any:
    """``OPTIMIZE FINAL`` 兜底:策略挑不出候选时,直接把整个分区合起来。"""
    from .merge_policy import MergeCandidate

    total = sum(p.bytes_on_disk() for p in parts)
    return MergeCandidate(list(parts), total, reason="OPTIMIZE FINAL (forced)")


def _ttl_info_of(part: MergeTreePart, ttls: Sequence[TTLDescription],
                 data: MergeTreeData, ctx: Any) -> Optional[TTLInfo]:
    """取一个 part 的 :class:`TTLInfo` —— 优先用 ``ttl.txt``,没有就算一遍。

    ``ttl.txt`` 是**写入时**算好的(见 :func:`compute_ttl_info`),
    于是 TTL 判定是"一次比较"而不是"扫全 part"。
    老 part 没有这个文件时退回真算一遍(慢,但正确)。
    """
    if not ttls:
        return None
    if part.ttl_info:
        info = TTLInfo()
        info.delete_min, info.delete_at = _ttl_action_bounds(part.ttl_info, "delete")
        info.recompress_min, info.recompress_at = _ttl_action_bounds(part.ttl_info, "recompress")
        info.to_disk_min, info.to_disk_at = _ttl_action_bounds(part.ttl_info, "to_disk")
        info.to_volume_min, info.to_volume_at = _ttl_action_bounds(part.ttl_info, "to_volume")
        if any(v is not None for v in (info.delete_at, info.delete_min,
                                       info.recompress_at, info.to_disk_at,
                                       info.to_volume_at)):
            return info
    # 老 part 没有 ttl.txt → 只能真读一遍算出来(慢但正确)
    block = data._read_part_fully(part)
    return compute_ttl_info(ttls, block, ctx)


def _ttl_action_bounds(stored: Dict[str, Any], action: str) -> Tuple[Optional[int], Optional[int]]:
    """从 ``ttl.txt`` 的 JSON 里读出某个动作的 ``(min, max)``(epoch 秒)。

    兼容两种形状:嵌套的 ``{"min":…,"max":…}``(我们自己写的)
    与扁平的字符串(真实 ClickHouse 的形状,只有一个时间点)。
    """
    v = stored.get(action)
    if v is None:
        return None, None
    if isinstance(v, dict):
        return _parse_ttl_ts(v.get("min")), _parse_ttl_ts(v.get("max"))
    ts = _parse_ttl_ts(v)
    return ts, ts


def _parse_ttl_ts(v: Any) -> Optional[int]:
    if not v:
        return None
    try:
        return int(_dt.datetime.strptime(str(v), "%Y-%m-%dT%H:%M:%S")
                   .replace(tzinfo=_dt.timezone.utc).timestamp())
    except Exception:
        return None
