"""合并策略 —— "该合并哪几个 part"的决策。

合并策略回答的**只有**这一个问题:

.. code-block:: text

   现在磁盘上有 N 个 part:{A(1KB), B(2KB), C(4KB), D(100MB), E(101MB)}
   下一步该合并哪些?

   答案不是"全部" —— 那会产生一个 200MB 的巨型 IO 任务,
   而且没有任何收益(D 和 E 本来就已经很大了)。

   答案也不是"最小的两个" —— 那会让 A 反复与各种小 part 合并,
   写放大严重(这叫"写放大"问题,LSM-Tree 家族的经典难题)。

   真实的选择是:**一段连续的、大小相近的小 part**。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  SimpleMergeSelector 的三条规则(真实 ClickHouse 的默认策略)          │
   └──────────────────────────────────────────────────────────────────────┘

   ① **连续的**:合并的是排序后的**一段**,不是随便挑几个。
      原因:合并要把 N 个有序流归并成 1 个;段越小,归并的
      内存/IO 越可控,而且天然的"分批推进"让大 part 最后才被碰。

   ② **大小相近**:段内最大元素 ≤ 最小元素 × ``max_size_ratio``(默认 3)。
      原因:如果 1KB 和 100MB 一起合并,合并结果的"贡献"几乎全来自
      100MB 那个 —— 1KB 的数据等于白搬了一遍。更糟的是:
      下次再有 1KB 进来,它又会和那个 100MB 合并一次。
      "只和同等量级合并"是控制写放大的核心手段。

   ③ **总和受限**:段内字节总和 ≤
      ``max_bytes_to_merge_at_max_space_in_pool``(默认 **150 GiB**)。
      原因:合并是**在内存/磁盘空间里做**的;一次合并 500GB 的 part
      会瞬间吃满空间。真实 CH 还会检查
      "可用空间 × 0.1 ≥ 待合并总量"。

   在这三条约束下,**优先选总量最小的那段** —— 这保证小 part 先被
   消化掉,而在"数据持续写入"的场景里,小 part 的数量就是查询性能的
   主要杀手(part 越多,读一次表要打开的文件越多)。

.. note::

   **真实 ClickHouse 有更复杂的一层:TTL 合并策略。**
   它把"TTL 过期"和"空间回收"也纳入决策(``TTLMergeSelector``),
   于是"过期数据"能被优先合并掉。mini-clickhouse 只实现
   ``SimpleMergeSelector``,并在 :mod:`minich.storages.merge_tree.ttl`
   里用一条独立的"TTL 触发"路径处理过期。

.. warning::

   真实 ClickHouse 的合并策略还有两个 mini-clickhouse 不做的维度:

   * **年龄**:新写入的 part 要等一会儿才参与合并
     (``min_age_to_force_merge_seconds``),否则"边写边合"会
     反复重写同一批数据;
   * **partition 数量上限**:一个表里 part 太多时优先合并
     "part 数最多的那个分区"(``max_partitions_to_merge``)。

   这两条都是**性能/稳定性**优化,不影响正确性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ...errors import ErrorCode, raise_error
from ...util.stopwatch import ProfileEvents

__all__ = ["SimpleMergePolicy", "MergeSelectorParams", "MergeCandidate"]


#: 真实 ClickHouse 的默认值(``MergeTreeSettings``)
DEFAULT_MAX_BYTES_TO_MERGE_AT_MAX_SPACE_IN_POOL = 150 * 1024 ** 3   # 150 GiB
DEFAULT_MAX_BYTES_TO_MERGE_AT_MIN_SPACE_IN_POOL = 1 * 1024 ** 2     # 1 MiB
DEFAULT_MAX_PARTS_TO_MERGE_AT_ONCE = 100
#: 段内最大 part 与最小 part 的字节比上限
DEFAULT_MAX_SIZE_RATIO = 3.0


@dataclass
class MergeSelectorParams:
    """合并策略的参数 —— 名字与真实 ClickHouse 设置一一对应。"""

    max_bytes_to_merge_at_max_space_in_pool: int = DEFAULT_MAX_BYTES_TO_MERGE_AT_MAX_SPACE_IN_POOL
    max_bytes_to_merge_at_min_space_in_pool: int = DEFAULT_MAX_BYTES_TO_MERGE_AT_MIN_SPACE_IN_POOL
    max_parts_to_merge_at_once: int = DEFAULT_MAX_PARTS_TO_MERGE_AT_ONCE
    max_size_ratio: float = DEFAULT_MAX_SIZE_RATIO
    #: 至少要几个 part 才值得合并(真实 CH 是 2 —— 1 个 part 没什么可合)
    min_parts_to_merge: int = 2
    #: 可用空间的比例上限:一次合并不能吃掉超过这个比例的可用空间
    space_ratio: float = 0.1

    def max_bytes_for_available_space(self, available: Optional[int]) -> int:
        if not available:
            return self.max_bytes_to_merge_at_max_space_in_pool
        return min(
            self.max_bytes_to_merge_at_max_space_in_pool,
            max(self.max_bytes_to_merge_at_min_space_in_pool,
                int(available * self.space_ratio)),
        )


@dataclass
class MergeCandidate:
    """一次合并的候选方案 —— 一段连续的 part + 理由(便于日志与测试断言)。"""

    parts: List[Any]
    total_bytes: int
    reason: str = ""

    def level(self) -> int:
        """合并结果的 level = 段内最大 level + 1。

        .. code-block:: text

           level 的语义是"数据被重写过几次":
             INSERT 出来的 part            level = 0
             两个 level 0 合并 →             level = 1
             一个 level 1 与一个 level 0 →    level = 2

           于是 level 越大 = 数据越"熟"。合并策略优先合并
           level 相同的小 part,正是为了"让数据被均匀地重写",
           而不是让某一个小 part 被反复重写(写放大)。

           .. warning::
              真实 ClickHouse 用的是 ``max(levels) + 1`` 吗? 不完全 ——
              它在 ``MergeTreeDataMergerMutator::getMaxLevel`` 里取
              **段内最大 level + 1**,并对"跨越太多 level 的段"
              有额外惩罚。我们取 max+1,与"结果是新的一层"这一直觉一致。
        """
        return max((p.get_level() for p in self.parts), default=0) + 1

    def min_block_number(self) -> int:
        return min(p.get_min_block_number() for p in self.parts)

    def max_block_number(self) -> int:
        return max(p.get_max_block_number() for p in self.parts)

    def __repr__(self) -> str:  # pragma: no cover
        names = ", ".join(p.get_name() for p in self.parts)
        return f"MergeCandidate([{names}], {self.total_bytes} bytes, {self.reason})"


class SimpleMergePolicy:
    """真实 ClickHouse ``SimpleMergeSelector`` 的行为实现。

    调用方式::

        policy = SimpleMergePolicy()
        candidate = policy.select(parts_of_one_partition, available_space=...)
        if candidate is not None:
            merge(candidate)

    ``parts`` 必须是**同一个分区**的(真实 CH 也是这样:合并绝不跨分区,
    因为跨分区会破坏"删除整个分区"的 O(1) 语义)。
    """

    def __init__(self, params: Optional[MergeSelectorParams] = None) -> None:
        self.params = params or MergeSelectorParams()

    # ── 主入口 ──────────────────────────────────────────────────────────

    def select(
        self,
        parts: Sequence[Any],
        available_space: Optional[int] = None,
        events: Any = None,
    ) -> Optional[MergeCandidate]:
        """从 ``parts`` 里挑一段该合并的,挑不出来返回 ``None``。"""
        if len(parts) < self.params.min_parts_to_merge:
            return None

        ordered = sorted(parts, key=lambda p: (p.bytes_on_disk(), p.get_name()))
        max_bytes = self.params.max_bytes_for_available_space(available_space)
        best: Optional[MergeCandidate] = None

        n = len(ordered)
        for i in range(n):
            total = 0
            group: List[Any] = []
            smallest = ordered[i].bytes_on_disk()
            for j in range(i, n):
                part = ordered[j]
                size = max(1, part.bytes_on_disk())
                # 规则②:大小相近 —— 超过比例就断开这一段
                if size > max(1, smallest) * self.params.max_size_ratio:
                    break
                if len(group) + 1 > self.params.max_parts_to_merge_at_once:
                    break
                if total + size > max_bytes:
                    break
                total += size
                group.append(part)
                if len(group) < self.params.min_parts_to_merge:
                    continue
                cand = MergeCandidate(
                    list(group), total,
                    reason=f"{'level<=%d' % cand_level(group)} "
                           f"sizes={[p.bytes_on_disk() for p in group]}",
                )
                # 规则③之后再选"总量最小"的;并列时选 part 更多的
                if best is None or (total, -len(group)) < (best.total_bytes, -len(best.parts)):
                    best = cand
            # 内层已经覆盖了以 i 开头的所有段;继续外层即可
        if best is not None and events is not None:
            events.increment("Merge")
        return best

    # ── 便利方法 ────────────────────────────────────────────────────────

    def select_for_partitions(
        self,
        parts_by_partition: Dict[str, Sequence[Any]],
        available_space: Optional[int] = None,
        events: Any = None,
    ) -> Optional[MergeCandidate]:
        """在多个分区里各选一次,返回**总量最小**的那个候选。

        真实 ClickHouse 的 ``SimpleMergeSelector`` 一次只处理一个分区的
        候选集(``select``),由 ``MergeTreeData::selectPartsForMerge``
        在每个分区上调一次。我们把它包一层:优先服务"最需要合并的分区"
        (候选总量最小的那个),这样在单机演示里能更快收敛到
        "每个分区一个 part"。
        """
        best: Optional[MergeCandidate] = None
        for pid, parts in parts_by_partition.items():
            cand = self.select(parts, available_space, events=None)
            if cand is None:
                continue
            if best is None or cand.total_bytes < best.total_bytes:
                best = cand
        if best is not None and events is not None:
            events.increment("Merge")
        return best


def cand_level(parts: Sequence[Any]) -> int:
    return max((p.get_level() for p in parts), default=0)
