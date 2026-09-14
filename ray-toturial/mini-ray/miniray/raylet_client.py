"""RayletClient —— raylet 的 RPC 封装(薄薄一层,只负责「怎么说话」)。

把「RPC 方法名 + 参数顺序」集中在一个文件里,好处是 raylet 的服务端实现
(``raylet.py``)与调用方(``core_worker.py`` / ``worker.py``)可以各自演化,
不会出现「改了一边忘了另一边」。

真实 Ray 里对应的东西是 ``raylet_client`` / ``core_worker_client`` 两套 gRPC stub。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence

from .rpc import RpcClient

__all__ = ["RayletClient"]


class RayletClient:
    """线程安全的 raylet 客户端。

    .. warning::
       ``request_work`` / ``actor_poll`` 是**长轮询**,会占住连接最多 ``timeout``
       秒。多线程并发执行时每个线程要用自己的 client(见 ``worker.py`` 的 actor
       运行时),否则会互相阻塞。
    """

    def __init__(self, address: str) -> None:
        self._rpc = RpcClient(address, name="raylet")

    @property
    def address(self) -> str:
        return self._rpc.address

    def close(self) -> None:
        self._rpc.close()

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """通用调用入口。

        大多数方法都在下面有专门的封装(参数顺序一目了然);
        这个方法留给那些零星的、没必要单独包一层的方法
        (``task_chunk`` / ``report_actor_creation`` …)。
        """
        return self._rpc.call(method, *args, **kwargs)

    # ------------------------------------------------------------ worker 生命周期
    def register_worker(
        self,
        worker_id: str,
        node_id: str,
        pid: int,
        worker_type: str = "task",
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._rpc.call(
            "register_worker",
            worker_id=worker_id,
            node_id=node_id,
            pid=pid,
            worker_type=worker_type,
            actor_id=actor_id,
        )

    def disconnect_worker(self, worker_id: str) -> None:
        self._rpc.call("disconnect_worker", worker_id)

    def request_work(self, worker_id: str) -> Dict[str, Any]:
        """长轮询要任务。返回 ``{"kind": "task"|"noop"|"shutdown", ...}``。"""
        return self._rpc.call("request_work", worker_id)

    # ------------------------------------------------------------ 任务
    def submit_task(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc.call("submit_task", spec)

    def submit_actor_task(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc.call("submit_actor_task", spec)

    def create_actor(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc.call("create_actor", spec)

    def kill_actor(self, actor_id: str, *, no_restart: bool = True) -> None:
        self._rpc.call("kill_actor", actor_id, no_restart=no_restart)

    def actor_poll(self, actor_id: str, worker_id: str, group: str = "") -> Dict[str, Any]:
        return self._rpc.call("actor_poll", actor_id, worker_id, group)

    def register_actor_methods(self, actor_id: str, methods: Dict[str, Any]) -> None:
        self._rpc.call("register_actor_methods", actor_id, methods)

    def task_done(
        self,
        task_id: str,
        worker_id: str,
        results: List[Dict[str, Any]],
        *,
        actor_id: Optional[str] = None,
    ) -> None:
        self._rpc.call("task_done", task_id, worker_id, results, actor_id=actor_id)

    def task_failed(
        self,
        task_id: str,
        worker_id: str,
        error: Dict[str, Any],
        *,
        actor_id: Optional[str] = None,
        crashed: bool = False,
    ) -> None:
        self._rpc.call(
            "task_failed", task_id, worker_id, error, actor_id=actor_id, crashed=crashed
        )

    def cancel_task(self, task_id: str, *, force: bool = False) -> None:
        self._rpc.call("cancel_task", task_id, force=force)

    # ------------------------------------------------------------ 对象
    def put_object(self, worker_id: str, object_id: str, payload: Dict[str, Any]) -> None:
        self._rpc.call("put_object", worker_id, object_id, payload)

    def allocate_object(self, worker_id: str, size: int) -> Dict[str, Any]:
        return self._rpc.call("allocate_object", worker_id, size)

    def commit_object(
        self,
        worker_id: str,
        object_id: str,
        block: Dict[str, Any],
        *,
        kind: str,
        dtype: Optional[str] = None,
        shape: Optional[Sequence[int]] = None,
    ) -> None:
        self._rpc.call(
            "commit_object",
            worker_id,
            object_id,
            block,
            kind=kind,
            dtype=dtype,
            shape=list(shape) if shape is not None else None,
        )

    def fetch_objects(self, worker_id: str, object_ids: Sequence[str]) -> Dict[str, Any]:
        return self._rpc.call("fetch_objects", worker_id, list(object_ids))

    def wait_for_objects(
        self,
        worker_id: str,
        object_ids: Sequence[str],
        *,
        num_returns: int = 1,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._rpc.call(
            "wait_for_objects",
            worker_id,
            list(object_ids),
            num_returns=num_returns,
            timeout=timeout,
        )

    def update_ref_counts(self, worker_id: str, deltas: Dict[str, int]) -> None:
        self._rpc.call("update_ref_counts", worker_id, deltas)

    def pin_object(self, worker_id: str, object_id: str) -> None:
        self._rpc.call("pin_object", worker_id, object_id)

    def unpin_object(self, worker_id: str, object_id: str) -> None:
        self._rpc.call("unpin_object", worker_id, object_id)

    # ------------------------------------------------------------ 生成器
    def get_generator_state(self, generator_id: str) -> Dict[str, Any]:
        return self._rpc.call("get_generator_state", generator_id)

    def wait_for_generator(
        self, worker_id: str, generator_id: str, index: int, timeout: float = 60.0
    ) -> Dict[str, Any]:
        return self._rpc.call("wait_for_generator", worker_id, generator_id, index, timeout)

    # ------------------------------------------------------------ 放置组
    def create_placement_group(
        self, bundles: List[Dict[str, float]], strategy: str, name: Optional[str] = None
    ) -> str:
        return self._rpc.call("create_placement_group", bundles, strategy, name)

    def remove_placement_group(self, placement_group_id: str) -> None:
        self._rpc.call("remove_placement_group", placement_group_id)

    def placement_group_ready_object(self, placement_group_id: str) -> Optional[str]:
        return self._rpc.call("placement_group_ready_object", placement_group_id)

    # ------------------------------------------------------------ 状态
    def get_state(self) -> Dict[str, Any]:
        return self._rpc.call("get_state")

    def list_tasks(self) -> List[Dict[str, Any]]:
        return self._rpc.call("list_tasks")

    def list_objects(self) -> List[Dict[str, Any]]:
        return self._rpc.call("list_objects")

    def list_workers(self) -> List[Dict[str, Any]]:
        return self._rpc.call("list_workers")

    def cluster_resources(self) -> Dict[str, float]:
        return self._rpc.call("cluster_resources")

    def available_resources(self) -> Dict[str, float]:
        return self._rpc.call("available_resources")

    def get_timeline_events(self) -> List[Dict[str, Any]]:
        return self._rpc.call("get_timeline_events")

    def lose_objects(self, *, node_id: Optional[str] = None) -> Dict[str, Any]:
        """故障注入:模拟节点故障/对象丢失(仅用于教学与测试)。"""
        return self._rpc.call("lose_objects", node_id=node_id)

    def shutdown(self) -> None:
        self._rpc.call("shutdown_raylet")
