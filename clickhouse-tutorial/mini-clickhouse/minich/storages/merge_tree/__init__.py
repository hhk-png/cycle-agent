"""``minich.storages.merge_tree`` —— MergeTree 引擎族的完整实现。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  这是 mini-clickhouse 里**唯一**一个真正的"存储引擎"               ║
   ║  其它引擎(Memory/Log/Null…)只是"没有索引的容器"                     ║
   ╚══════════════════════════════════════════════════════════════════════╝

   .. code-block:: text

      模块           作用                                    教程章节
      ─────────────  ──────────────────────────────────────  ──────────
      ``disk``       磁盘抽象 + **原子改名**                  05 disk
      ``marks``      granule / mark / 自适应粒度              05 part format
      ``part``       part 的磁盘布局与读写(**核心**)         05 part format
      ``key_condition`` WHERE → key range(稀疏索引二分)    05 sparse index
      ``skip_index`` 数据跳过索引(minmax/set/bloom/ngram…)
      ``merge_policy`` 该合并哪几个 part                      05 merges
      ``merger``     k 路归并 + 引擎家族的折叠语义            05 merges
      ``mutations``  ALTER UPDATE/DELETE(异步重写)            05 mutations
      ``ttl``        TTL:DELETE / TO DISK / RECOMPRESS        05 ttl
      ``merge_tree`` ``MergeTreeData`` 本体(读写/合并/DDL)   05 engine
      ``engines``    八个具体引擎 + 注册
      ``expression`` 存储层自用的小求值器(见它的文档)

   学习顺序建议:**disk → marks → part → key_condition → skip_index →
   merge_policy → merger → mutations → ttl → merge_tree → engines**。
   每读一个模块,先看它的模块 docstring(那里有 ASCII 图和"为什么")。

.. note::

   本模块的 ``__init__`` 会在**导入时**把所有引擎注册进
   :mod:`minich.storages.base` 的注册表 —— 于是
   ``minich/storages/__init__.py`` 只要 ``from .merge_tree import *``
   一次,``CREATE TABLE ... ENGINE = MergeTree`` 就能找到工厂了。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  SQL 可达性:哪些 ALTER / SYSTEM 命令真的会落到这个引擎上            │
   └──────────────────────────────────────────────────────────────────────┘

   ===================================  ====================================
   命令                                状态
   ===================================  ====================================
   ``ALTER ... DELETE WHERE``          **实现**(异步 mutation,合并/OPTIMIZE
                                       时生效)—— :meth:`MergeTreeData.mutate`
   ``ALTER ... UPDATE c = e WHERE``     **实现**(同上,**只改谓词为真的行**)
   ``ALTER ... ADD COLUMN``            **实现**(纯元数据,老 part 读时补默认值)
   ``ALTER ... DROP COLUMN``           **实现**(排序键列不允许删)
   ``ALTER ... MODIFY COLUMN``         **实现**(只改元数据,**不转换**老数据)
   ``ALTER ... RENAME COLUMN``         **实现**(顺带把 part 里的文件名改掉)
   ``ALTER ... MODIFY ORDER BY``       **实现**,但**仅限空表**
                                       (有 part 时抛 ``NOT_IMPLEMENTED``,与真实 CH 一致)
   ``ALTER ... DROP PARTITION``        **实现**(O(1) 元数据删除)
   ``ALTER ... DROP PART``             **实现**(:meth:`drop_part`)
   ``ALTER ... DETACH/ATTACH PARTITION`` 引擎侧**已实现**
                                       (:meth:`detach_partition` / :meth:`attach_partition`),
                                       但 **SQL 层未接线** ——
                                       ``InterpreterAlterQuery`` 对这些 kind
                                       直接抛 ``NOT_IMPLEMENTED``
                                       (那是 SQL 层的取舍,不是引擎没实现)
   ``ALTER ... MOVE PARTITION``        引擎与 SQL 层都未实现(需要多磁盘/卷)
   ``ALTER ... MODIFY TTL``            **实现**(改元数据,下次合并生效)
   ``ALTER ... MODIFY/RESET SETTING``  **部分实现**:引擎真正执行的设置可以改;
                                       引擎不执行的设置 **抛 ``NOT_IMPLEMENTED``**
                                       (绝不"接受但什么都不做")
   ``ALTER ... ADD/DROP/MATERIALIZE INDEX``
                                       引擎侧**已实现**(:meth:`alter_index` /
                                       :meth:`add_index` / :meth:`drop_index` /
                                       :meth:`materialize_index`,后三者是 API 级),
                                       但 **SQL 层拒绝** —— mini-clickhouse 的
                                       跳数索引只能在 ``CREATE TABLE`` 时用
                                       ``INDEX`` 子句声明
   ``ALTER ... FREEZE / UNFREEZE``     引擎侧**已实现(拷贝式快照)**
                                       (:meth:`freeze` / :meth:`unfreeze`),
                                       SQL 层同样未接线
   ``OPTIMIZE ... [FINAL] [DEDUPLICATE]`` **实现**,含"单 part 也要折叠"与
                                       "FINAL 时整个分区一次归并"(折叠对
                                       合并顺序敏感,必须一次做完)
   ``SYSTEM START/STOP MERGES``        对应 :meth:`start_merges` / :meth:`stop_merges`
   ===================================  ====================================

   .. note::

      ``use_skip_indexes`` / ``use_skip_indexes_if_final`` 这两个设置
      由 :meth:`MergeTreeData._skip_indexes_enabled` 判定,读取顺序是
      **查询级 → 会话级 → 表级**(见 ``_query_setting``)。
      所以 ``SET use_skip_indexes = 0`` 能立刻关掉索引裁剪,
      用它去做"索引到底有没有生效"的对照实验。

   ``system.mutations`` / ``system.parts`` / ``system.part_log`` 的数据分别由
   :meth:`MergeTreeData.mutation_status`、:meth:`MergeTreeData.parts_rows`、
   ``part_log`` sink 提供。

   .. warning::

      **``INSERT`` 的 DEFAULT / MATERIALIZED 列由谁算?**

      * ``MATERIALIZED`` 列**由引擎算**(见 :meth:`MergeTreeData._fill_defaults`):
        因为用户**不允许**写这一列,引擎必须根据**目标列的 block** 求值
        (``MATERIALIZED v * 2`` 里的 ``v`` 是表里的列名,不是 SELECT 的输出名)。
      * ``DEFAULT`` 列**理论上由解释器算**(真实 ClickHouse 在
        ``ExpressionActions`` 里做)。如果解释器用它自己的零值把这一列填满了,
        引擎**无法分辨**"用户显式写了 0"与"解释器补了 0" ——
        所以引擎不会去覆盖它。这条边界写在这里,免得读者以为
        ``DEFAULT`` 失效是引擎的锅。
"""

from __future__ import annotations

# ── 磁盘 ────────────────────────────────────────────────────────────────
from .disk import DiskLocal, DiskSpace, IDisk, get_available_space

# ── granule / mark ──────────────────────────────────────────────────────
from .marks import (
    DEFAULT_INDEX_GRANULARITY,
    DEFAULT_INDEX_GRANULARITY_BYTES,
    MIN_INDEX_GRANULARITY_BYTES,
    RANGE_MAX,
    IndexGranularity,
    Mark,
    MarkRange,
    intersect_ranges,
    mark_range_for_rows,
    merge_ranges,
    round_down_to_multiple,
    round_up_to_multiple,
)

# ── part ────────────────────────────────────────────────────────────────
from .part import (
    DEFAULT_MIN_BYTES_FOR_WIDE_PART,
    DEFAULT_MIN_ROWS_FOR_WIDE_PART,
    MergeTreePart,
    PartName,
    PartWriteContext,
    format_part_name,
    format_partition_id,
    load_part,
    parse_part_name,
    read_compressed_block,
    write_compressed_block,
    write_part,
)

# ── 稀疏索引 ────────────────────────────────────────────────────────────
from .key_condition import (
    CheckResult,
    KeyCondition,
    KeyConditionBuildInformation,
    KeyFunctionSpec,
    PartitionPruner,
    Range,
    compare_index_values,
    describe_key_expression,
)

# ── 跳数索引 ────────────────────────────────────────────────────────────
from .skip_index import (
    BloomFilter,
    BloomFilterIndex,
    IMergeTreeIndex,
    IndexCondition,
    IndexDescription,
    IndexHelper,
    MergeTreeIndexReader,
    MinMaxIndex,
    NgramBFIndex,
    SetIndex,
    TokenBFIndex,
    collect_index_conditions,
    create_index,
    supported_index_types,
)

# ── 合并 ────────────────────────────────────────────────────────────────
from .merge_policy import (
    MergeCandidate,
    MergeSelectorParams,
    SimpleMergePolicy,
)
from .merger import (
    BlockRowSource,
    MergeTreeDataPartWriter,
    MergeTreeMergeConfig,
    MergeTreeMergingIterator,
    MergeTreePartReader,
    merge_parts,
)

# ── mutation / TTL ──────────────────────────────────────────────────────
from .mutations import (
    MutationCommand,
    MutationEntry,
    MutationRegistry,
    apply_mutation_to_block,
    build_mutation_command,
)
from .ttl import (
    TTLDescription,
    TTLInfo,
    TTLRowsRemover,
    compute_ttl_info,
    evaluate_ttl_bounds,
    ttl_value_in_seconds,
)

# ── 引擎 ────────────────────────────────────────────────────────────────
from .merge_tree import DEFAULT_SETTINGS, MergeTreeData
from .engines import (
    StorageAggregatingMergeTree,
    StorageCollapsingMergeTree,
    StorageGraphiteMergeTree,
    StorageMergeTree,
    StorageReplicatedMergeTree,
    StorageReplacingMergeTree,
    StorageSummingMergeTree,
    StorageVersionedCollapsingMergeTree,
    register_merge_tree_engines,
)

# 导入即注册 —— 让 ``ENGINE = MergeTree`` 之类的建表语句能找到工厂
register_merge_tree_engines()

__all__ = [
    # disk
    "IDisk", "DiskLocal", "DiskSpace", "get_available_space",
    # marks
    "Mark", "MarkRange", "IndexGranularity", "RANGE_MAX",
    "mark_range_for_rows", "merge_ranges", "intersect_ranges",
    "round_up_to_multiple", "round_down_to_multiple",
    "DEFAULT_INDEX_GRANULARITY", "DEFAULT_INDEX_GRANULARITY_BYTES",
    "MIN_INDEX_GRANULARITY_BYTES",
    # part
    "MergeTreePart", "PartName", "PartWriteContext", "write_part", "load_part",
    "parse_part_name", "format_part_name", "format_partition_id",
    "write_compressed_block", "read_compressed_block",
    "DEFAULT_MIN_BYTES_FOR_WIDE_PART", "DEFAULT_MIN_ROWS_FOR_WIDE_PART",
    # key condition
    "KeyCondition", "KeyConditionBuildInformation", "KeyFunctionSpec",
    "PartitionPruner", "Range", "CheckResult", "compare_index_values",
    "describe_key_expression",
    # skip index
    "IMergeTreeIndex", "MinMaxIndex", "SetIndex", "BloomFilterIndex",
    "NgramBFIndex", "TokenBFIndex", "BloomFilter", "IndexDescription",
    "IndexHelper", "MergeTreeIndexReader", "IndexCondition",
    "collect_index_conditions", "create_index", "supported_index_types",
    # merge
    "SimpleMergePolicy", "MergeSelectorParams", "MergeCandidate",
    "merge_parts", "MergeTreePartReader", "MergeTreeMergingIterator",
    "MergeTreeDataPartWriter", "MergeTreeMergeConfig", "BlockRowSource",
    # mutations / ttl
    "MutationCommand", "MutationEntry", "MutationRegistry",
    "apply_mutation_to_block", "build_mutation_command",
    "TTLDescription", "TTLInfo", "TTLRowsRemover", "compute_ttl_info",
    "evaluate_ttl_bounds", "ttl_value_in_seconds",
    # engines
    "MergeTreeData", "DEFAULT_SETTINGS",
    "StorageMergeTree", "StorageReplacingMergeTree", "StorageSummingMergeTree",
    "StorageAggregatingMergeTree", "StorageCollapsingMergeTree",
    "StorageVersionedCollapsingMergeTree", "StorageGraphiteMergeTree",
    "StorageReplicatedMergeTree", "register_merge_tree_engines",
]
