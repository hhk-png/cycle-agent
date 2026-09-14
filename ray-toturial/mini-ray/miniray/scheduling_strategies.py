"""调度策略 —— 告诉调度器「这个任务/actor 该放在哪里」。

Ray 的 ``scheduling_strategy`` 是**运行期**指定放置约束的手段(和 ``@ray.remote``
上的静态资源声明互补):

.. code-block:: python

    from miniray.util.scheduling_strategies import (
        NodeAffinitySchedulingStrategy, PlacementGroupSchedulingStrategy,
    )

    # 1) 字符串:'SPREAD' / 'DEFAULT' / ''  —— ⚠️ mini-ray **不支持**这一档,
    #    传了会抛 MiniRayError('不认识的调度策略: ...')。见下面的"局限"。

    # 2) 指定节点(软硬两档)
    f.options(scheduling_strategy=NodeAffinitySchedulingStrategy(node_id, soft=False)).remote()

    # 3) 放进放置组的某个 bundle
    f.options(scheduling_strategy=PlacementGroupSchedulingStrategy(pg, placement_group_bundle_index=0)).remote()

真实 Ray 2.58 里还有 ``NodeLabelSchedulingStrategy``(alpha,按标签/表达式调度)与
拓扑感知调度(2.56 起支持 ``ray.io/gpu-domain`` 的 NVLink 域感知放置组),
mini-ray 只实现后两种(**策略对象**)。

局限(与真实 Ray 的差异,别互相照抄):

* **字符串策略完全不被接受** —— ``"SPREAD"`` / ``"DEFAULT"`` / ``""``
  一律抛 ``MiniRayError: 不认识的调度策略``。真实 Ray 里它们是生效的
  (``SchedulingType::SPREAD`` / ``DEFAULT``)。本模块导出的 ``DEFAULT`` /
  ``SPREAD`` 两个常量只是**为将来留的名字**,当前没有任何代码消费它们。
* ``NodeAffinitySchedulingStrategy`` 的 ``soft=True`` **只有硬亲和的实现**:
  ``soft`` 会被记下来,但调度器无条件硬过滤,候选为空时直接抛
  ``ScheduleError``;"等不到就退回普通调度"是真实 Ray 才有的行为。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .errors import MiniRayError

__all__ = [
    "NodeAffinitySchedulingStrategy",
    "PlacementGroupSchedulingStrategy",
    "DEFAULT",
    "SPREAD",
]

#: 字符串策略常量(与 Ray 一致:空字符串等价于 DEFAULT)
DEFAULT = "DEFAULT"
SPREAD = "SPREAD"


class _EncodableStrategy:
    """能被序列化进任务消息的策略。"""

    def _encode(self) -> Dict[str, Any]:  # pragma: no cover - 抽象
        raise NotImplementedError


class NodeAffinitySchedulingStrategy(_EncodableStrategy):
    """把任务钉在某个节点上。

    :param soft: ``False``(硬亲和)时,节点资源不够就**一直等**。
        ``True``(软亲和)**在 mini-ray 里没有实现回退** —— 该值会被记录下来,
        但调度器无条件按硬亲和过滤,候选为空时直接抛 ``ScheduleError``。
        真实 Ray 的 ``soft=True`` 会退回普通调度,两边行为不同。

    Ray 里还支持 ``_spill_on_unavailable``(资源不足时把该节点上已有的任务
    赶走来腾地方),mini-ray 没实现。
    """

    def __init__(self, node_id: Any, soft: bool = False, _spill_on_unavailable: bool = False) -> None:
        self.node_id = node_id.hex() if hasattr(node_id, "hex") and not isinstance(node_id, str) else str(node_id)
        self.soft = bool(soft)
        self._spill_on_unavailable = bool(_spill_on_unavailable)

    def _encode(self) -> Dict[str, Any]:
        return {"kind": "node_affinity", "node_id": self.node_id, "soft": self.soft}

    def __repr__(self) -> str:  # pragma: no cover - 展示
        return f"NodeAffinitySchedulingStrategy(node_id={self.node_id[:8]}, soft={self.soft})"


class PlacementGroupSchedulingStrategy(_EncodableStrategy):
    """把任务放进放置组。

    :param placement_group_bundle_index: 指定 bundle 下标;
        ``-1``(Ray 的默认值)表示「这个放置组里任意一个装得下的 bundle」。
    :param placement_group_capture_child_tasks: 子任务是否自动继承该放置组。
        mini-ray 记录该标志但不实现继承(Ray 里它会影响嵌套任务的落点)。
    """

    def __init__(
        self,
        placement_group: Any,
        placement_group_bundle_index: int = -1,
        placement_group_capture_child_tasks: Optional[bool] = None,
    ) -> None:
        pg_id = getattr(placement_group, "id", None)
        if pg_id is None:
            raise MiniRayError(
                "PlacementGroupSchedulingStrategy 需要 placement_group 对象"
                "(来自 miniray.util.placement_group(...))"
            )
        self.placement_group = placement_group
        self.placement_group_bundle_index = int(placement_group_bundle_index)
        self.placement_group_capture_child_tasks = placement_group_capture_child_tasks

    def _encode(self) -> Dict[str, Any]:
        return {
            "kind": "placement_group",
            "placement_group_id": self.placement_group.id.hex()
            if hasattr(self.placement_group.id, "hex")
            else str(self.placement_group.id),
            "bundle_index": self.placement_group_bundle_index,
        }

    def __repr__(self) -> str:  # pragma: no cover - 展示
        return (
            f"PlacementGroupSchedulingStrategy(bundle_index={self.placement_group_bundle_index})"
        )
