"""放置组(Placement Group)—— 「把一组资源先占下来」。

问题:你想跑一个 4 个 actor 的分布式训练,每个 actor 要 1 GPU。
如果只是逐个创建,调度器可能把它们分散到 4 台不同机器上 —— 甚至可能前 3 个
成功了、第 4 个因为没资源而**永远等下去**(死锁)。

放置组的答案:**先原子性地占下资源,再往里放东西**。

.. code-block:: python

    import miniray as ray
    from miniray.util.placement_group import placement_group
    from miniray.util.scheduling_strategies import PlacementGroupSchedulingStrategy

    pg = placement_group([{"CPU": 1, "GPU": 1}] * 4, strategy="STRICT_PACK")
    ray.get(pg.ready())                     # 等资源真的占下来

    workers = [
        Worker.options(
            scheduling_strategy=PlacementGroupSchedulingStrategy(
                pg, placement_group_bundle_index=i
            )
        ).remote()
        for i in range(4)
    ]

四种策略(Ray 的语义):

============== =====================================================
STRICT_PACK    所有 bundle 必须在**同一个节点**
PACK           尽量少用节点(装不下才换)
SPREAD         尽量摊开,但允许同节点
STRICT_SPREAD  每个 bundle 一个节点,且**不允许同节点**
============== =====================================================

放置组的资源是「预留」的:被 bundle 占住的资源,普通任务用不了 ——
所以放置组也是**成本**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import MiniRayError
from .ids import PlacementGroupID
from .object_ref import ObjectRef

__all__ = [
    "PlacementGroup",
    "placement_group",
    "remove_placement_group",
    "get_placement_group",
    "get_current_placement_group",
    "placement_group_table",
]


class PlacementGroup:
    """放置组的客户端句柄。"""

    def __init__(self, placement_group_id: PlacementGroupID, *, bundle_specs: Optional[List[Dict]] = None, strategy: str = "PACK", name: Optional[str] = None) -> None:
        self._id = placement_group_id
        self._bundle_specs = bundle_specs or []
        self._strategy = strategy
        self._name = name

    # ---------------------------------------------------------------- 属性
    @property
    def id(self) -> PlacementGroupID:
        return self._id

    @property
    def bundle_count(self) -> int:
        return len(self._bundle_specs)

    @property
    def bundle_specs(self) -> List[Dict]:
        return list(self._bundle_specs)

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def name(self) -> Optional[str]:
        return self._name

    # ---------------------------------------------------------------- 等待
    def ready(self) -> ObjectRef:
        """返回一个 ObjectRef,它的值是 ``{bundle 下标: 节点 ID}``。

        注意 Ray 的 ``pg.ready()`` 返回的就是 ObjectRef(不是 bool),
        所以标准写法是 ``ray.get(pg.ready())``。
        """
        from . import runtime

        worker = runtime.get_core_worker()
        object_id_hex = worker._raylet.placement_group_ready_object(self._id.hex())
        if object_id_hex is None:
            raise MiniRayError(
                f"放置组 {self._id.hex()[:8]} 不存在或已删除"
            )
        from .ids import ObjectID

        return ObjectRef(ObjectID(bytes.fromhex(object_id_hex)), worker._raylet.address)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """阻塞等待放置组就绪。返回是否就绪(超时返回 ``False``)。"""
        from . import runtime

        worker = runtime.get_core_worker()
        object_id_hex = worker._raylet.placement_group_ready_object(self._id.hex())
        if object_id_hex is None:
            return False
        result = worker._raylet.wait_for_objects(
            worker.worker_id, [object_id_hex], num_returns=1, timeout=timeout
        )
        return bool(result["ready"])

    # ---------------------------------------------------------------- 状态
    def _info(self) -> Dict[str, Any]:
        from . import runtime

        worker = runtime.get_core_worker()
        info = worker._gcs.call("get_placement_group", self._id.hex())
        return info or {}

    @property
    def state(self) -> str:
        return self._info().get("state", "UNKNOWN")

    def __repr__(self) -> str:
        return (
            f"PlacementGroup({self._id.hex()[:8]}, bundles={self.bundle_count}, "
            f"strategy={self._strategy}, state={self.state})"
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PlacementGroup) and other._id == self._id

    def __hash__(self) -> int:
        return hash(self._id)


def placement_group(
    bundles: List[Dict[str, float]],
    strategy: str = "PACK",
    name: Optional[str] = None,
) -> PlacementGroup:
    """创建一个放置组(异步:创建请求立刻返回,资源分配在后台重试)。

    :param bundles: 例如 ``[{"CPU": 1, "GPU": 1}, {"CPU": 1}]``
        (键名大写,和 Ray 一致:``CPU`` / ``GPU`` / 自定义资源)
    """
    from . import runtime

    if not bundles:
        raise MiniRayError("放置组至少要有一个 bundle")
    for index, bundle in enumerate(bundles):
        if not bundle:
            raise MiniRayError(f"第 {index} 个 bundle 是空的;每个 bundle 都要声明资源")
    normalized = [
        {str(key): float(value) for key, value in bundle.items()} for bundle in bundles
    ]
    worker = runtime.get_core_worker()
    pg_id = worker._raylet.create_placement_group(normalized, strategy, name)
    return PlacementGroup(
        PlacementGroupID(bytes.fromhex(pg_id)),
        bundle_specs=normalized,
        strategy=strategy.upper(),
        name=name,
    )


def remove_placement_group(placement_group: PlacementGroup) -> None:
    """删除放置组并把预留的资源还给集群。

    .. warning::
       Ray 里删除放置组会**杀掉**所有使用它的 actor/任务(这是有意的:
       资源承诺没了,里面的东西必须让路)。mini-ray 只释放资源,不杀 actor。
    """
    from . import runtime

    worker = runtime.get_core_worker()
    worker._raylet.remove_placement_group(placement_group.id.hex())


def get_placement_group(placement_group_id: Any) -> Optional[PlacementGroup]:
    from . import runtime

    worker = runtime.get_core_worker()
    if isinstance(placement_group_id, PlacementGroup):
        placement_group_id = placement_group_id.id.hex()
    elif not isinstance(placement_group_id, str):
        placement_group_id = str(placement_group_id)
    info = worker._gcs.call("get_placement_group", placement_group_id)
    if not info:
        return None
    return PlacementGroup(
        PlacementGroupID(bytes.fromhex(info["placement_group_id"])),
        bundle_specs=info["bundles"],
        strategy=info["strategy"],
        name=info.get("name"),
    )


def get_current_placement_group() -> Optional[PlacementGroup]:
    """返回当前任务所属的放置组。

    mini-ray 没有实现「放置组继承」(``placement_group_capture_child_tasks``),
    所以这里总是返回 ``None``。
    """
    return None


def placement_group_table() -> Dict[str, Dict[str, Any]]:
    """当前所有放置组的状态(对齐 ``ray.util.placement_group_table``)。"""
    from . import runtime

    worker = runtime.get_core_worker()
    infos = worker._gcs.call("list_placement_groups")
    return {info["placement_group_id"]: info for info in infos}
