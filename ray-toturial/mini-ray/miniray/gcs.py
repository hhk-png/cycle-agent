"""GCS(Global Control Service)—— 集群元数据服务。

Ray 官方文档对 GCS 的定位:

    "The Global Control Service, or GCS, manages cluster-level metadata. It also
     provides a handful of cluster-level operations including **actor, placement
     groups and node management**."

    "By default, the GCS isn't fault tolerant because it stores all data in memory.
     **If it fails, the entire Ray cluster fails.**"

GCS 里放的都是「全集群唯一一份」的东西:

======================================  ==========================================
内容                                     为什么必须在 GCS
======================================  ==========================================
节点注册表(地址、资源)                  调度决策要看全局视图
函数表(function_id → 函数体)             每个函数只需导出一次
对象目录(object_id → 哪些节点上有)       「对象在哪」的全局索引,决定去哪拉数据
actor 注册表(含命名 actor)              按名字找 actor 只能有一个权威答案
放置组(placement group)                  资源预留是集群级承诺
job / namespace                         名字服务的隔离边界
======================================  ==========================================

mini-ray 的 GCS 跑在 **driver 进程里的一个线程**(真实 Ray 里它是独立的
``gcs_server`` 进程)。它只做「读写一张内存表」,不做任何调度决策 —— 放置组的
**分配**由 raylet 用调度器算,算完写回 GCS。这样切分是为了让「元数据」和
「决策」的边界清楚。

.. code-block:: text

     driver 进程
     ┌──────────────────────────────────────────────────────────┐
     │  GCS 线程(本文件)      Raylet 线程(raylet.py)         │
     │  ├ 节点表                ├ 调度器(资源账本)              │
     │  ├ 函数表                ├ 对象存储 × N(每节点一个)       │
     │  ├ 对象目录  ◀───────────┤ 对象目录缓存/拉取               │
     │  ├ actor 表 ◀────────────┤ worker 池                       │
     │  └ 放置组表 ◀────────────┤ 血缘(对象重建)                │
     └──────────────────────────────────────────────────────────┘
              ▲                          ▲
              │ RPC(127.0.0.1:port)     │ RPC
              └──────────┬───────────────┘
                    driver CoreWorker / worker 进程
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .errors import MiniRayError
from .ids import ActorID, FunctionID, JobID, NodeID, PlacementGroupID
from .rpc import RpcClient

__all__ = ["GcsService", "GcsClient", "ActorInfo", "NodeInfo", "PlacementGroupInfo"]


# ---------------------------------------------------------------------------
# 元数据类型
# ---------------------------------------------------------------------------


@dataclass
class NodeInfo:
    node_id: str
    address: str
    resources: Dict[str, float]
    state: str = "ALIVE"
    registered_at: float = field(default_factory=time.time)


@dataclass
class ActorInfo:
    actor_id: str
    class_name: str
    node_id: str
    job_id: str
    name: Optional[str] = None
    namespace: Optional[str] = None
    lifetime: str = "non_detached"  # non_detached / detached
    max_restarts: int = 0
    max_task_retries: int = 0
    max_concurrency: int = 1
    concurrency_groups: Dict[str, int] = field(default_factory=dict)
    resources: Dict[str, float] = field(default_factory=dict)
    state: str = "PENDING"  # PENDING / ALIVE / RESTARTING / DEAD
    num_restarts: int = 0
    pid: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    #: 创建 task 的结果对象 ID(创建失败时把异常带给第一个方法调用)
    creation_object_id: Optional[str] = None
    #: 方法名 → 并发组(worker 注册 actor 时上报)
    method_groups: Dict[str, str] = field(default_factory=dict)
    is_async: bool = False


@dataclass
class PlacementGroupInfo:
    placement_group_id: str
    bundles: List[Dict[str, float]]
    strategy: str
    name: Optional[str] = None
    state: str = "PENDING"  # PENDING / CREATED / REMOVED
    #: {node_id: [bundle 下标]}
    assignment: Dict[str, List[int]] = field(default_factory=dict)
    #: 创建完成后 `pg.ready()` 返回的那个对象
    ready_object_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# 服务实现
# ---------------------------------------------------------------------------


class GcsService:
    """GCS 的实际存储(纯内存,和 Ray 默认行为一致 —— 所以它挂了集群就挂了)。"""

    def __init__(self, *, session_dir: str = "", job_id: str = "", namespace: str = "") -> None:
        self._lock = threading.RLock()
        self.session_dir = session_dir
        self.job_id = job_id
        self.namespace = namespace or "default"
        self.nodes: Dict[str, NodeInfo] = {}
        self.actors: Dict[str, ActorInfo] = {}
        self.named_actors: Dict[str, str] = {}  # "namespace:name" → actor_id
        self.placement_groups: Dict[str, PlacementGroupInfo] = {}
        self.objects: Dict[str, List[str]] = {}  # object_id(hex) → [node_id…]
        self.functions: Dict[str, bytes] = {}
        self.function_digests: Dict[str, str] = {}  # sha1 → function_id
        #: 对象「死亡通知」:对象被删除/丢失时,raylet 会来问
        self.deleted_objects: Dict[str, float] = {}
        self.start_time = time.time()

    # ------------------------------------------------------------ 节点管理
    def register_node(self, node_id: str, address: str, resources: Dict[str, float]) -> Dict[str, Any]:
        with self._lock:
            info = NodeInfo(node_id=node_id, address=address, resources=dict(resources))
            self.nodes[node_id] = info
            return asdict(info)

    def list_nodes(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [asdict(info) for info in self.nodes.values()]

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            info = self.nodes.get(node_id)
            return asdict(info) if info else None

    def unregister_node(self, node_id: str) -> None:
        with self._lock:
            self.nodes.pop(node_id, None)
            # 该节点上的对象位置全部失效
            for object_id, nodes in list(self.objects.items()):
                if node_id in nodes:
                    nodes.remove(node_id)
                    if not nodes:
                        self.objects.pop(object_id, None)
                        self.deleted_objects[object_id] = time.time()

    # ------------------------------------------------------------ 函数表
    def export_function(self, blob: bytes) -> str:
        """导出函数体。返回 FunctionID(同一份函数体只存一次)。"""
        import hashlib

        digest = hashlib.sha1(blob).hexdigest()
        with self._lock:
            existing = self.function_digests.get(digest)
            if existing is not None:
                return existing
            function_id = FunctionID.from_random().hex()
            self.functions[function_id] = blob
            self.function_digests[digest] = function_id
            return function_id

    def get_function(self, function_id: str) -> Optional[bytes]:
        with self._lock:
            return self.functions.get(function_id)

    # ------------------------------------------------------------ 对象目录
    def add_object_locations(self, object_ids: Sequence[str], node_id: str) -> None:
        with self._lock:
            for object_id in object_ids:
                nodes = self.objects.setdefault(object_id, [])
                if node_id not in nodes:
                    nodes.append(node_id)
                self.deleted_objects.pop(object_id, None)

    def remove_object_locations(self, object_ids: Sequence[str], node_id: str) -> None:
        with self._lock:
            for object_id in object_ids:
                nodes = self.objects.get(object_id)
                if not nodes:
                    continue
                if node_id in nodes:
                    nodes.remove(node_id)
                if not nodes:
                    self.objects.pop(object_id, None)

    def remove_object(self, object_id: str) -> None:
        """对象彻底没了(驱逐/溢出失败/节点故障)。"""
        with self._lock:
            self.objects.pop(object_id, None)
            self.deleted_objects[object_id] = time.time()

    def get_object_locations(self, object_ids: Sequence[str]) -> Dict[str, List[str]]:
        with self._lock:
            return {oid: list(self.objects.get(oid, ())) for oid in object_ids}

    # ------------------------------------------------------------ actor
    def register_actor(self, info: Dict[str, Any]) -> None:
        with self._lock:
            actor = ActorInfo(**info)
            self.actors[actor.actor_id] = actor
            if actor.name:
                self.named_actors[f"{actor.namespace or 'default'}:{actor.name}"] = actor.actor_id

    def update_actor(self, actor_id: str, **fields: Any) -> None:
        with self._lock:
            actor = self.actors.get(actor_id)
            if actor is None:
                return
            for key, value in fields.items():
                setattr(actor, key, value)

    def get_actor(self, actor_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            actor = self.actors.get(actor_id)
            return asdict(actor) if actor else None

    def list_actors(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [asdict(actor) for actor in self.actors.values()]

    def get_named_actor(self, name: str, namespace: Optional[str] = None) -> Optional[str]:
        with self._lock:
            return self.named_actors.get(f"{namespace or self.namespace}:{name}")

    def unregister_named_actor(self, name: str, namespace: Optional[str] = None) -> None:
        with self._lock:
            self.named_actors.pop(f"{namespace or self.namespace}:{name}", None)

    # ------------------------------------------------------------ 放置组
    def create_placement_group(
        self,
        bundles: List[Dict[str, float]],
        strategy: str,
        name: Optional[str] = None,
    ) -> str:
        with self._lock:
            pg_id = PlacementGroupID.from_random().hex()
            self.placement_groups[pg_id] = PlacementGroupInfo(
                placement_group_id=pg_id, bundles=bundles, strategy=strategy, name=name
            )
            return pg_id

    def get_placement_group(self, pg_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            info = self.placement_groups.get(pg_id)
            return asdict(info) if info else None

    def list_placement_groups(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [asdict(info) for info in self.placement_groups.values()]

    def get_pending_placement_groups(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                asdict(info)
                for info in self.placement_groups.values()
                if info.state == "PENDING"
            ]

    def update_placement_group(self, pg_id: str, **fields: Any) -> None:
        with self._lock:
            info = self.placement_groups.get(pg_id)
            if info is None:
                return
            for key, value in fields.items():
                setattr(info, key, value)

    def remove_placement_group(self, pg_id: str) -> None:
        with self._lock:
            info = self.placement_groups.get(pg_id)
            if info is not None:
                info.state = "REMOVED"

    # ------------------------------------------------------------ 状态
    def get_cluster_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "namespace": self.namespace,
                "session_dir": self.session_dir,
                "nodes": [asdict(node) for node in self.nodes.values()],
                "actors": [asdict(actor) for actor in self.actors.values()],
                "placement_groups": [asdict(pg) for pg in self.placement_groups.values()],
                "num_objects": len(self.objects),
                "num_functions": len(self.functions),
                "uptime": time.time() - self.start_time,
            }

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "num_nodes": len(self.nodes),
                "num_actors": len(self.actors),
                "num_placement_groups": len(self.placement_groups),
                "num_objects_tracked": len(self.objects),
                "num_functions": len(self.functions),
            }

    # 生命周期(供 RpcServer 调用)
    def ping(self) -> str:
        return "pong"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class GcsClient:
    """GCS 的客户端封装(raylet 与 core worker 各持一个)。"""

    def __init__(self, address: str) -> None:
        self._rpc = RpcClient(address, name="gcs")

    @property
    def address(self) -> str:
        return self._rpc.address

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._rpc.call(method, *args, **kwargs)

    # 常用方法的语法糖(免得满屏字符串)
    def export_function(self, blob: bytes) -> str:
        return self._rpc.call("export_function", blob)

    def get_function(self, function_id: str) -> Optional[bytes]:
        return self._rpc.call("get_function", function_id)

    def add_object_locations(self, object_ids: Sequence[str], node_id: str) -> None:
        self._rpc.call("add_object_locations", list(object_ids), node_id)

    def remove_object_locations(self, object_ids: Sequence[str], node_id: str) -> None:
        self._rpc.call("remove_object_locations", list(object_ids), node_id)

    def remove_object(self, object_id: str) -> None:
        self._rpc.call("remove_object", object_id)

    def get_object_locations(self, object_ids: Sequence[str]) -> Dict[str, List[str]]:
        return self._rpc.call("get_object_locations", list(object_ids))

    def get_actor(self, actor_id: str) -> Optional[Dict[str, Any]]:
        return self._rpc.call("get_actor", actor_id)

    def get_named_actor(self, name: str, namespace: Optional[str] = None) -> Optional[str]:
        return self._rpc.call("get_named_actor", name, namespace)

    def list_actors(self) -> List[Dict[str, Any]]:
        return self._rpc.call("list_actors")

    def list_nodes(self) -> List[Dict[str, Any]]:
        return self._rpc.call("list_nodes")

    def get_cluster_state(self) -> Dict[str, Any]:
        return self._rpc.call("get_cluster_state")

    def close(self) -> None:
        self._rpc.close()


# ---------------------------------------------------------------------------
# 与真实 Ray 的差异
#   * 真实 GCS 是独立的 ``gcs_server`` 进程(C++),通过 gRPC 服务所有人;
#     mini-ray 是 driver 进程里的一个线程(TCP + pickle)。
#   * 真实 GCS 默认**内存存储**,挂了集群就挂;2.57 起可用嵌入式 RocksDB
#     (``RAY_gcs_storage=rocksdb``,REP-64)或外部 Redis 做容错。mini-ray 只有内存版。
#   * 真实 GCS 还管 autoscaler、dashboard、task/actor 事件、runtime_env 等;
#     mini-ray 只保留元数据最小集。
# ---------------------------------------------------------------------------
