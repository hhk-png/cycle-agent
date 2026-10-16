"""具体的 MergeTree 引擎与注册 —— ``ENGINE = MergeTree`` 这些话落地的地方。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  一个引擎族的七个成员(它们**共用全部代码**,只差"合并时怎么折叠")  ║
   ╚══════════════════════════════════════════════════════════════════════╝

   ENGINE = MergeTree()                    不动(有重复就有重复)
   ENGINE = ReplacingMergeTree([ver])      同键只留 version 最大的那行
   ENGINE = SummingMergeTree([cols])       同键把数值列相加
   ENGINE = AggregatingMergeTree()         同键把聚合状态 merge 起来
   ENGINE = CollapsingMergeTree(sign)      同键按 sign 抵消
   ENGINE = VersionedCollapsingMergeTree(sign, ver)   同上但按 version 配对
   ENGINE = GraphiteMergeTree(...)         按 Graphite 的 rollup 规则降采样

   这七者的差别**只有** :class:`minich.storages.merge_tree.merger.MergeTreeMergeConfig`
   里那一个 ``family`` 字段。其余全一样:

   .. code-block:: text

     写入(追加 part)      完全一样
     读取(granule 裁剪)   完全一样
     稀疏索引             完全一样
     后台合并的调度       完全一样
     只有"同键行怎么合并"   ← 唯一的差别

   这就是 ClickHouse 引擎家族最漂亮的地方:**它没有为 Replacing 单独写
   一个存储引擎**。它写了一个"可插拔的合并语义",然后用
   ``ENGINE = ReplacingMergeTree(ver)`` 这一行语法把它打开。

.. code-block:: text

   ┌── "怎么选引擎"的一个速查(第 04 章的完整版)──────────────────────┐
   │                                                                  │
   │  场景                              选什么                       │
   │  ────────────────────────────────  ──────────────────────────  │
   │  只追加的明细 / 日志                MergeTree                    │
   │  需要"按主键去重"的最新快照         ReplacingMergeTree(ver)     │
   │  需要"预聚合"(按天汇总的指标)     SummingMergeTree / Aggregating│
   │  需要"用追加表达删除/更新"          CollapsingMergeTree          │
   │  同上但写入可能乱序                  VersionedCollapsingMergeTree│
   │  监控指标自动降采样                  GraphiteMergeTree            │
   │                                                                  │
   │  ★ 一个常见误区:以为「ReplacingMergeTree 就等于主键唯一」——    │
   │    不是。它只在**合并时**去重,所以"合并跑之前"能查到重复。      │
   │    要么 `OPTIMIZE ... FINAL`,要么查询时 `FINAL`,要么用         │
   │    `GROUP BY key + argMax(ver)` 自己收敛。                       │
   └──────────────────────────────────────────────────────────────────┘

.. warning::

   **mini-clickhouse 与真实 ClickHouse 在"引擎注册"上的差异**:

   * 真实 CH 为每个引擎写了一个 ``registerStorageMergeTree()`` 之类的
     注册函数,并在 ``StorageFactory`` 的构造期把它们全部静态注册;
     mini-clickhouse 用 :func:`minich.storages.base.register_storage_engine`,
     在**导入本模块时**注册(所以 ``minich/storages/__init__.py``
     必须 import 一次)。
   * 真实 CH 的引擎名是**大小写敏感**的(``MergeTree`` 不是 ``mergetree``);
     mini-clickhouse 的注册表是小写键 —— 更宽容,但与真实略有出入。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...catalog import StorageID, Table
from ...errors import ErrorCode, raise_error
from ...util.logger import get_logger
from ..base import register_storage_engine
from .merge_tree import MergeTreeData

__all__ = [
    "StorageMergeTree",
    "StorageReplacingMergeTree",
    "StorageSummingMergeTree",
    "StorageAggregatingMergeTree",
    "StorageCollapsingMergeTree",
    "StorageVersionedCollapsingMergeTree",
    "StorageGraphiteMergeTree",
    "StorageReplicatedMergeTree",
    "register_merge_tree_engines",
]

_log = get_logger("MergeTree")


# ══════════════════════════════════════════════════════════════════════════
# 具体引擎
# ══════════════════════════════════════════════════════════════════════════


class StorageMergeTree(MergeTreeData):
    """``ENGINE = MergeTree()`` —— 最基础的一个:只追加,合并只做排序归并。

    它没有"去重""求和"任何语义。于是同一个排序键可以有任意多行 ——
    这正是"明细表"想要的。
    """

    engine_name = "MergeTree"
    engine_family = "MergeTree"


class StorageReplacingMergeTree(MergeTreeData):
    """``ENGINE = ReplacingMergeTree([ver [, is_deleted]])``。

    .. code-block:: text

       ┌── 它到底"替换"了什么? ────────────────────────────────────────┐
       │                                                                │
       │  表的排序键(ORDER BY)就是"主键"的意思。合并时,排序键**相同**  │
       │  的多行只保留**version 最大**的那一行:                         │
       │                                                                │
       │    INSERT (1, 'a', 100)                                        │
       │    INSERT (1, 'b', 200)                                        │
       │       │                                                        │
       │       └── OPTIMIZE TABLE t FINAL ──► 只剩 (1, 'b', 200)        │
       │                                                                │
       │  没写 ver 列时,保留"最后插入"的那一行。                        │
       │                                                                │
       │  ★ 关键认知:**它是"去重",不是"唯一约束"。**                    │
       │    写入时**不会**检查重复;重复只在合并时消除。                  │
       └────────────────────────────────────────────────────────────────┘

    ``is_deleted`` 参数(ClickHouse 23.8+)让它能做**软删除**:
    ``is_deleted = 1`` 的行在合并时**整组丢弃** ——
    于是"删一行"变成了"追加一条 is_deleted=1、version 更大的行"。
    这是"用追加实现删除"在 ReplacingMergeTree 上的做法。
    """

    engine_name = "ReplacingMergeTree"
    engine_family = "Replacing"


class StorageSummingMergeTree(MergeTreeData):
    """``ENGINE = SummingMergeTree([(cols)])`` —— 合并时按排序键求和。

    .. code-block:: text

       ┌── 它的真实定位:物化视图的"落地表" ────────────────────────────┐
       │                                                                │
       │   CREATE MATERIALIZED VIEW mv TO daily                            │
       │   AS SELECT toDate(ts) d, user_id, count() c, sum(x) s           │
       │      FROM raw GROUP BY d, user_id;                               │
       │                                                                │
       │   daily 引擎用 SummingMergeTree((c, s)) ——                       │
       │   于是"同一个 (d, user_id) 的多批增量"会在合并时被**加起来**:  │
       │       批次 1: (2024-01-01, 7, c=3, s=30)                        │
       │       批次 2: (2024-01-01, 7, c=2, s=15)                        │
       │       合并后: (2024-01-01, 7, c=5, s=45)   ← 自动汇总           │
       │                                                                │
       │   这就是 ClickHouse 做"增量物化视图"的标准姿势:                  │
       │   **把"部分聚合结果"当成可以继续相加的数据存起来。**            │
       └────────────────────────────────────────────────────────────────┘

    .. warning::

       **求和列会发生溢出而 ClickHouse 不检查**。``UInt8`` 的
       ``250 + 10 = 4``。这是本引擎最经典的坑,汇总列的类型
       一定要留够余量(通常直接 ``UInt64``/``Int64``)。
    """

    engine_name = "SummingMergeTree"
    engine_family = "Summing"


class StorageAggregatingMergeTree(MergeTreeData):
    """``ENGINE = AggregatingMergeTree()`` —— 合并时 merge **聚合状态**。

    它和 ``SummingMergeTree`` 的区别是"存什么":

    .. code-block:: text

       SummingMergeTree          列里存 **值**(42)
       AggregatingMergeTree      列里存 **状态**(一段二进制)

       于是后者能做 ``uniq``/``quantile``/``avg`` 这些
       "不能靠简单相加合并"的聚合 —— 因为它们的状态是
       HyperLogLog 草图 / 分位数直方图 / (和, 计数)。

    用法一定要配合组合器:

    .. code-block:: sql

       CREATE TABLE mv_daily (d Date, uv AggregateFunction(uniq, UInt64))
       ENGINE = AggregatingMergeTree() ORDER BY d;

       INSERT INTO mv_daily SELECT toDate(ts), uniqState(user_id)
       FROM raw GROUP BY 1;                       -- ← 存状态

       SELECT d, uniqMerge(uv) FROM mv_daily GROUP BY d;   -- ← 读状态
    """

    engine_name = "AggregatingMergeTree"
    engine_family = "Aggregating"


class StorageCollapsingMergeTree(MergeTreeData):
    """``ENGINE = CollapsingMergeTree(sign)`` —— 用 ``sign`` 表示"增/删"。

    .. code-block:: text

       ┌── 在 CollapsingMergeTree 出现之前 ─────────────────────────────┐
       │                                                                │
       │  ClickHouse **不能** UPDATE / DELETE 单行(只有重 mutation)。  │
       │  要"改一行"只能靠"追加"。                                      │
       │                                                                │
       │  于是有了这个设计:用 sign 列表达意图                            │
       │      +1 = "这一行存在"                                          │
       │      -1 = "前面那行作废"                                        │
       │                                                                │
       │  改一行:  追加 (-1, 旧值) + (+1, 新值)                          │
       │  删一行:  追加 (-1, 旧值)                                       │
       │  合并时:  整组抵消 → 磁盘上不留垃圾                              │
       │  查询时:  SELECT ..., SUM(x*sign) ... GROUP BY key              │
       └────────────────────────────────────────────────────────────────┘

    .. warning::

       **它要求写入方保证顺序**:``+1`` 必须在 ``-1`` **之前**写入。
       否则"中间态"会让 ``SUM(x*sign)`` 出现负数。
       这就是 ``VersionedCollapsingMergeTree`` 存在的理由 ——
       后者用 version 列配对,天然容忍乱序(比如 Distributed 表
       把两边发到了不同分片)。
    """

    engine_name = "CollapsingMergeTree"
    engine_family = "Collapsing"


class StorageVersionedCollapsingMergeTree(MergeTreeData):
    """``ENGINE = VersionedCollapsingMergeTree(sign, version)``。

    比 :class:`StorageCollapsingMergeTree` 多一个 ``version`` 列,
    配对规则变成"**同一个 version 下**的 +1/-1 才算一对":

    .. code-block:: text

       写入顺序乱了也没关系 —— 合并前按 (version, sign) 排一下:

         (sign=+1, ver=1)
         (sign=-1, ver=1)   ← 与上面配对抵消
         (sign=+1, ver=2)
         (sign=-1, ver=3)   ← ver 不同,不抵消;留下 ver=2 的那条

    ``ORDER BY`` 里**必须**包含 version 列(通常放在最后),否则
    "同一个 key 的不同 version"会被合并到不同的 granule,
    抵消逻辑就看不全它们了。
    """

    engine_name = "VersionedCollapsingMergeTree"
    engine_family = "VersionedCollapsing"


class StorageGraphiteMergeTree(MergeTreeData):
    """``ENGINE = GraphiteMergeTree(config_section)`` —— **只注册,不实现**。

    .. warning::

       **本类在 mini-clickhouse 里退化成普通 MergeTree。**

       真实 ClickHouse 的 ``GraphiteMergeTree`` 会读一份
       ``graphite_rollup`` 配置,按"时间跨度越老、精度越粗"的规则
       **降采样**监控指标:

       .. code-block:: text

         最近 1 小时   → 保留原始精度(每秒一个点)
         最近 1 天     → 每分钟一个点
         最近 30 天    → 每小时一个点
         更早          → 每天一个点

         规则由 (pattern, function, age, precision) 四元组描述,
         函数可以是 avg / min / max / sum / any …

       这套规则需要:① 一份 YAML/XML 的 rollup 配置树;
       ② 按 pattern 匹配 metric 名的正则;③ 合并时按 age 分桶聚合。
       它是一整章的内容,与 MergeTree 的核心语义**正交** ——
       所以 mini-clickhouse 只**注册这个名字**(于是
       ``SHOW ENGINES`` 能看到它、建表不会报"未知引擎"),
       但**不应用任何 rollup 规则**,只在建表时打一条警告。

       这个取舍写在 README 的能力矩阵里。
    """

    engine_name = "GraphiteMergeTree"
    engine_family = "MergeTree"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        params = kwargs.get("params") or (args[4] if len(args) > 4 else {})
        section = ""
        engine_args = kwargs.get("engine_args") or (args[3] if len(args) > 3 else [])
        if engine_args:
            section = str(engine_args[0])
        _log.warning(
            "GraphiteMergeTree is registered but NOT implemented in mini-clickhouse: "
            f"rollup rules {(section or (params.get('config_section') if isinstance(params, dict) else ''))!r} "
            "will be ignored and the table behaves like a plain MergeTree. "
            "See minich/storages/merge_tree/engines.py for details."
        )
        super().__init__(*args, **kwargs)


class StorageReplicatedMergeTree(MergeTreeData):
    """``ENGINE = ReplicatedMergeTree(path, replica_name)`` —— 复制表。

    .. code-block:: text

       ┌── 复制的全部难点都不在"存储",在"协调" ───────────────────────┐
       │                                                                │
       │   ReplicatedMergeTree 与普通 MergeTree 的**存储格式完全一样**  │
       │   (同样的 part、同样的 primary.idx、同样的 .mrk2)。           │
       │                                                                │
       │   差别在**谁来调度合并**:                                      │
       │     普通表      每个副本自己决定合哪些 part                     │
       │     复制表      所有副本通过 **ZooKeeper / Keeper** 达成一致:   │
       │                   * 谁先抢到 merge 权,谁去合并                 │
       │                   * 合并后的 part 名字写进 ZK 的一个队列       │
       │                   * 其他副本看到队列变化 → 去拉这个 part       │
       │                                                                │
       │   于是"同一份数据、所有副本的 part 名字都相同"变成一个          │
       │   **可验证的不变量** —— ``SYSTEM SYNC REPLICA`` 校验的就是它。  │
       └────────────────────────────────────────────────────────────────┘

    .. warning::

       **mini-clickhouse 的 ``minich.replication`` 是可选的。**

       :mod:`minich.replication` 由另一个模块负责,可能不存在。
       所以这里:

       * **绝不**在模块级 import 它(会让整个包起不来);
       * 能拿到协调器就挂上去,拿不到就**完全退化成单机 MergeTree**,
         并打一条**明确的**警告 —— 而不是假装复制成功。

       "假装复制成功"是最危险的行为:用户会以为数据有多副本,
       实际只有一份,而且**谁都不会发现**,直到丢数据的那一天。
       """

    engine_name = "ReplicatedMergeTree"
    engine_family = "MergeTree"

    #: 真实复制表的必需参数
    zoo_path: str = ""
    replica_name: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            table, storage_id = args[0], args[1]
            context = args[2] if len(args) > 2 else None
            engine_args = args[3] if len(args) > 3 else []
            params = args[4] if len(args) > 4 else {}
        else:
            table = kwargs["table"]
            storage_id = kwargs["storage_id"]
            context = kwargs.get("context")
            engine_args = kwargs.get("engine_args") or []
            params = kwargs.get("params") or {}
        args_list = [str(a) for a in (engine_args or [])]
        self.zoo_path = args_list[0] if args_list else str(params.get("zoo_path", "") or "")
        self.replica_name = args_list[1] if len(args_list) > 1 else str(
            params.get("replica_name", "") or "")
        super().__init__(table, storage_id, context, engine_args, params)
        self._attach_replication(context)
        if self.replication is None:
            _log.warning(
                f"ReplicatedMergeTree({self.zoo_path!r}, {self.replica_name!r}) is "
                "running WITHOUT replication: minich.replication is unavailable, so "
                "this table behaves exactly like a local MergeTree. There is only "
                "ONE copy of the data."
            )

    def _attach_replication(self, context: Any) -> None:
        """惰性找一个复制协调器 —— 找不到就留 ``None``(并已打过警告)。"""
        self.replication = None
        try:
            from ... import replication as _rep  # 惰性:可能不存在
        except Exception:
            return
        for attr in ("get_coordinator", "coordinator_for", "get_replication"):
            fn = getattr(_rep, attr, None)
            if callable(fn):
                try:
                    coord = fn(context, self.zoo_path, self.replica_name)
                except Exception as exc:
                    _log.warning(f"minich.replication refused to attach: {exc}")
                    return
                if coord is not None:
                    self.replication = coord
                    try:
                        coord.attach_table(self)
                    except Exception:  # pragma: no cover
                        pass
                    return
        # 模块在,但没有可用的协调器工厂 —— 也算"没有复制"
        cls = getattr(_rep, "ReplicationCoordinator", None)
        if cls is not None:
            try:
                coord = cls(context, self.zoo_path, self.replica_name)
                coord.attach_table(self)
                self.replication = coord
            except Exception as exc:  # pragma: no cover
                _log.warning(f"Failed to construct ReplicationCoordinator: {exc}")

    # ── 复制钩子(无复制时是 no-op)──────────────────────────────────

    def _notify_replication_new_part(self, part: Any) -> None:
        """新 part 提交后通知协调器 —— 无复制时什么都不做。"""
        if self.replication is None:
            return
        for attr in ("publish_part", "on_new_part", "announce_part"):
            fn = getattr(self.replication, attr, None)
            if callable(fn):
                try:
                    fn(part.get_name())
                except Exception as exc:  # pragma: no cover
                    _log.warning(f"Replication hook {attr} failed: {exc}")
                return

    def _write_one_part(self, block: Any, ctx: Any, events: Any) -> Any:
        part = super()._write_one_part(block, ctx, events)
        self._notify_replication_new_part(part)
        return part


# ══════════════════════════════════════════════════════════════════════════
# 工厂与注册
# ══════════════════════════════════════════════════════════════════════════


def _make_factory(cls: type) -> Any:
    """所有引擎共用一个工厂形状 —— ``(table, storage_id, context, engine_args, params)``。

    ``create_storage`` 就是这么调的(见 :mod:`minich.storages.base`)。
    """

    def factory(
        table: Table,
        storage_id: StorageID,
        context: Any = None,
        engine_args: Optional[Sequence[Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> MergeTreeData:
        return cls(table, storage_id, context, engine_args, params)

    factory.__name__ = f"create_{cls.__name__}"
    factory.__qualname__ = factory.__name__
    return factory


#: 引擎名 → (类, 一句话说明, 是否 deprecated)
_ENGINES: List[Any] = [
    (StorageMergeTree, "MergeTree",
     "The most robust table engine for high-load production systems: "
     "append-only parts, sparse primary index, background merges.", False),
    (StorageReplacingMergeTree, "ReplacingMergeTree",
     "Same as MergeTree, but rows with the same sorting key are collapsed "
     "on merge, keeping the row with the maximum version.", False),
    (StorageSummingMergeTree, "SummingMergeTree",
     "Same as MergeTree, but numeric columns are summed for rows with the "
     "same sorting key during merges.", False),
    (StorageAggregatingMergeTree, "AggregatingMergeTree",
     "Same as MergeTree, but aggregate function states are merged for rows "
     "with the same sorting key.", False),
    (StorageCollapsingMergeTree, "CollapsingMergeTree",
     "Same as MergeTree, but +1/-1 rows with the same sorting key collapse "
     "into each other during merges.", False),
    (StorageVersionedCollapsingMergeTree, "VersionedCollapsingMergeTree",
     "Same as CollapsingMergeTree, but uses a version column so that out-of-order "
     "writes still collapse correctly.", False),
    (StorageGraphiteMergeTree, "GraphiteMergeTree",
     "Graphite rollup rules (registered but NOT applied in mini-clickhouse).",
     False),
    (StorageReplicatedMergeTree, "ReplicatedMergeTree",
     "MergeTree with replication coordinated through Keeper/ZooKeeper "
     "(delegates to minich.replication when available, otherwise local-only).",
     False),
]


def register_merge_tree_engines() -> List[str]:
    """把整套引擎注册进 :mod:`minich.storages.base` 的注册表。

    **幂等**:重复调用只是覆盖同样的名字(``register_storage_engine``
    的语义就是覆盖),所以多次 import 不会出问题。
    """
    names: List[str] = []
    for cls, name, description, deprecated in _ENGINES:
        register_storage_engine(name, _make_factory(cls),
                                deprecated=deprecated, description=description)
        names.append(name)
    # 真实 ClickHouse 里 ``MergeTree`` 有一堆历史别名
    # (``ReplacingMergeTree`` 没有;``TinyLog``/``Log`` 别的模块管)。
    register_storage_engine("ReplicatedReplacingMergeTree",
                            _make_factory(StorageReplicatedMergeTree),
                            description="Alias kept for compatibility in real "
                                        "ClickHouse; mini-clickhouse maps it to "
                                        "ReplicatedMergeTree (no replacing semantics).")
    names.append("ReplicatedReplacingMergeTree")
    return names
