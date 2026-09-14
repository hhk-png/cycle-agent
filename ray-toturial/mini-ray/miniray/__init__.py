"""mini-ray:一个可运行的、工程化的 Ray 简化实现。

公共 API 与真实 Ray 对齐(名字、参数、返回类型、异常语义),所以从 Ray 迁移过来的
代码通常只需要把 ``import ray`` 换成 ``import miniray as ray``。

.. code-block:: python

    import miniray as ray

    @ray.remote
    def square(x):
        return x * x

    @ray.remote
    class Counter:
        def __init__(self):
            self.value = 0
        def inc(self):
            self.value += 1
            return self.value

    ray.init(num_cpus=4)
    try:
        refs = [square.remote(i) for i in range(8)]
        print(ray.get(refs))                     # [0, 1, 4, 9, ...]

        counter = Counter.remote()
        print([ray.get(counter.inc.remote()) for _ in range(3)])   # [1, 2, 3]
    finally:
        ray.shutdown()

API 分组:

==============================  ==================================================
任务                             ``remote`` / ``method`` / ``get`` / ``put`` / ``wait`` / ``cancel``
actor                           ``remote``(装饰类)/ ``get_actor`` / ``list_actors`` / ``kill``
集群                            ``init`` / ``shutdown`` / ``is_initialized`` / ``nodes`` / ``cluster_resources``
调度                            ``util.placement_group`` / ``util.scheduling_strategies``
可观测性                        ``state`` / ``timeline`` / ``get_runtime_context`` / ``get_gpu_ids``
工具                            ``util.ActorPool``
==============================  ==================================================
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# 核心 API
# ---------------------------------------------------------------------------
from .actor import ActorClass, ActorHandle
from .errors import (
    ActorDiedError,
    GetTimeoutError,
    MiniRayError,
    ObjectLostError,
    ObjectStoreFullError,
    RayActorError,
    RayError,
    RaySystemError,
    RayTaskError,
    TaskCancelledError,
    WorkerCrashedError,
)
from .ids import ActorID, JobID, NodeID, ObjectID, PlacementGroupID, TaskID, WorkerID
from .object_ref import ObjectRef, ObjectRefGenerator
from .remote import RemoteFunction, method, remote
from .runtime import (
    RuntimeContext,
    get_core_worker,
    get_gpu_ids,
    get_runtime_context,
    get_session_dir,
    init,
    is_initialized,
    shutdown,
)

#: 常量(对齐 Ray 的 ``ray.RAY_VERSION`` 等)
RAY_VERSION = __version__

__all__ = [
    "__version__",
    "RAY_VERSION",
    # 任务 / 对象
    "remote",
    "method",
    "RemoteFunction",
    "get",
    "put",
    "wait",
    "cancel",
    "ObjectRef",
    "ObjectRefGenerator",
    # actor
    "ActorClass",
    "ActorHandle",
    "get_actor",
    "list_actors",
    "kill",
    # 集群
    "init",
    "shutdown",
    "is_initialized",
    "nodes",
    "cluster_resources",
    "available_resources",
    "get_runtime_context",
    "get_gpu_ids",
    "get_session_dir",
    # 可观测性
    "timeline",
    # ID
    "ObjectID",
    "TaskID",
    "ActorID",
    "NodeID",
    "WorkerID",
    "JobID",
    "PlacementGroupID",
    # 异常
    "MiniRayError",
    "RayError",
    "RaySystemError",
    "RayTaskError",
    "RayActorError",
    "WorkerCrashedError",
    "ActorDiedError",
    "TaskCancelledError",
    "GetTimeoutError",
    "ObjectLostError",
    "ObjectStoreFullError",
]


# ---------------------------------------------------------------------------
# 取值:ray.get
# ---------------------------------------------------------------------------


def _shape_of(value: Any):
    """把嵌套结构变成一棵「形状树」,叶子是真实值。

    ``ray.get([ref1, {"k": [ref2, 3]}])`` 要按原结构返回 —— 所以先把结构记下来。
    """
    if isinstance(value, list):
        return ("list", [_shape_of(item) for item in value])
    if isinstance(value, tuple):
        return ("tuple", [_shape_of(item) for item in value])
    if isinstance(value, dict):
        return ("dict", {key: _shape_of(item) for key, item in value.items()})
    return ("leaf", value)


def _collect_leaves(shape, out: List[Any]) -> None:
    kind, payload = shape
    if kind == "leaf":
        out.append(payload)
    elif kind in ("list", "tuple"):
        for child in payload:
            _collect_leaves(child, out)
    else:  # dict
        for child in payload.values():
            _collect_leaves(child, out)


def _rebuild(shape, iterator: Iterator[Any]) -> Any:
    kind, payload = shape
    if kind == "leaf":
        return next(iterator)
    if kind == "list":
        return [_rebuild(child, iterator) for child in payload]
    if kind == "tuple":
        return tuple(_rebuild(child, iterator) for child in payload)
    return {key: _rebuild(child, iterator) for key, child in payload.items()}


def _get_impl(worker, refs: Any, timeout: Optional[float] = None) -> Any:
    """``ray.get`` 的实现:等齐 → 拉取 → 按原结构返回。"""
    if refs is None or isinstance(refs, (int, float, str, bool, bytes)):
        return refs

    if isinstance(refs, ObjectRefGenerator):
        values = []
        for ref in refs:
            values.append(_get_impl(worker, ref, timeout=timeout))
        return values

    shape = _shape_of(refs)
    leaves: List[Any] = []
    _collect_leaves(shape, leaves)
    obj_refs = [leaf for leaf in leaves if isinstance(leaf, ObjectRef)]
    if not obj_refs:
        return refs

    ready, remaining = worker.wait(
        obj_refs, num_returns=len(obj_refs), timeout=timeout
    )
    if remaining:
        raise GetTimeoutError(
            f"ray.get 等待 {timeout} 秒后仍有 {len(remaining)} 个对象没有就绪。"
            f"提示:对象可能还在排队(资源不足)、依赖未完成,或者上游任务失败了。"
        )
    values = worker.fetch_values(obj_refs)

    # 远程异常在这里重新抛出:让调用方看到原始的异常类型与远程堆栈。
    # 注意判据是「值本身是个异常对象」而不是「值是 RayTaskError」——
    # 因为对象存储里除了任务的异常(RayTaskError)还可能有:
    #   * TaskCancelledError(任务被取消)
    #   * ObjectLostError(对象丢失且无法重建)
    #   * ActorDiedError(actor 死了)
    # Ray 的语义是:这些「错误对象」在 ray.get 时都抛出来。
    for value in values:
        if isinstance(value, BaseException):
            raise value

    iterator = iter(values)
    filled = [next(iterator) if isinstance(leaf, ObjectRef) else leaf for leaf in leaves]
    return _rebuild(shape, iter(filled))


def get(refs: Any, timeout: Optional[float] = None) -> Any:
    """等待并取出对象的值(阻塞)。

    :param refs: 一个 ObjectRef、一组 ObjectRef,或者任意嵌套结构
        (list/tuple/dict,里面的非 ObjectRef 值原样返回)。
    :param timeout: 最长等待秒数(``None`` = 一直等)。

    远程任务抛出的异常会在这里重新抛出为 :class:`RayTaskError`,
    原始异常挂在 ``.cause`` 上,也可以用 ``.as_instanceof_cause()`` 还原类型。
    """
    worker = get_core_worker()
    return _get_impl(worker, refs, timeout=timeout)


def put(value: Any) -> ObjectRef:
    """把值放进对象存储,返回它的 ObjectRef。

    大 numpy 数组走共享内存零拷贝路径 —— 放进去一次,所有同节点消费者
    ``ray.get`` 出来的都是**同一块内存的只读视图**。
    """
    return get_core_worker().put(value)


def wait(
    refs: Sequence[ObjectRef],
    *,
    num_returns: int = 1,
    timeout: Optional[float] = None,
    fetch_local: bool = True,
) -> Tuple[List[ObjectRef], List[ObjectRef]]:
    """等 ``num_returns`` 个对象就绪,返回 ``(ready, remaining)``。

    这是**做背压的标准工具**:提交到一定数量就先 ``wait`` 收回几个,
    避免无限堆积(Ray 官方 *limit-pending-tasks* 模式)。
    """
    worker = get_core_worker()
    flat: List[ObjectRef] = []
    shape = _shape_of(list(refs))
    leaves: List[Any] = []
    _collect_leaves(shape, leaves)
    for leaf in leaves:
        if isinstance(leaf, ObjectRef):
            flat.append(leaf)
    return worker.wait(
        flat, num_returns=num_returns, timeout=timeout, fetch_local=fetch_local
    )


def cancel(ref: ObjectRef, *, force: bool = False, recursive: bool = True) -> None:
    """取消一个任务。

    :param force: ``False``(默认)时只取消**还没开始跑**的任务;
        ``True`` 会杀掉正在执行它的 worker(代价是那个 worker 的其它工作也一起没了)。
    """
    worker = get_core_worker()
    _ = recursive  # mini-ray 只取消这个对象对应的那一个 task
    worker._raylet.cancel_task(ref.hex(), force=force)


# ---------------------------------------------------------------------------
# actor
# ---------------------------------------------------------------------------


def get_actor(name: str, namespace: Optional[str] = None) -> ActorHandle:
    """按名字取 actor(需要创建时指定 ``name=`` 或 ``lifetime="detached"``)。"""
    worker = get_core_worker()
    info = worker.get_actor(name, namespace)
    if info is None:
        raise ValueError(
            f"找不到名字为 {name!r} 的 actor(命名空间 {namespace or worker.namespace})。"
            "检查:1) 创建时是否传了 name=;2) 命名空间是否一致。"
        )
    # ``method_groups`` 里存的就是 actor 注册上来的**完整方法元数据**
    # (``{"split": {"num_returns": 2, "concurrency_group": "", "is_async": False}}``)。
    # ⚠️ 这里以前把所有方法的 ``num_returns`` 一律写成 1 —— 于是
    # ``@miniray.method(num_returns=2)`` 的方法经由 ``ray.get_actor(name)`` 拿到的
    # 句柄调用时,调用方只要 1 个结果、**第二个返回值被静默丢掉**,
    # 而且返回类型从 list 变成了单个 ObjectRef(不报任何错)。
    raw_metadata = info.get("method_groups") or {}
    metadata = {
        method: (dict(meta) if isinstance(meta, dict) else {"num_returns": 1})
        for method, meta in raw_metadata.items()
    }
    return ActorHandle(
        ActorID(bytes.fromhex(info["actor_id"])),
        info.get("class_name", "Actor"),
        metadata,
        node_id=info.get("node_id"),
    )


def list_actors() -> List[Dict[str, Any]]:
    """列出所有 actor 的信息(对齐 ``ray.util.list_actors`` 的返回形状)。"""
    return get_core_worker().list_actors()


def kill(actor: ActorHandle, *, no_restart: bool = True) -> None:
    """杀掉 actor(``ray.kill``)。

    :param no_restart: ``True``(默认)表示不重启;``False`` 时如果 actor 配了
        ``max_restarts`` 还有额度,它会重启。
    """
    get_core_worker().kill_actor(actor.actor_id, no_restart=no_restart)


# ---------------------------------------------------------------------------
# 集群信息
# ---------------------------------------------------------------------------


def nodes() -> List[Dict[str, Any]]:
    """节点列表(对齐 ``ray.nodes()`` 的字段名)。

    单机时只有一条;用 ``init(num_nodes=N)`` 可以模拟多节点集群。
    """
    worker = get_core_worker()
    out = []
    for index, info in enumerate(worker._gcs.list_nodes()):
        out.append(
            {
                "NodeID": info["node_id"],
                "Alive": info["state"] == "ALIVE",
                "Resources": info["resources"],
                "NodeManagerAddress": info["address"].split("/")[0],
                "NodeManagerHostname": f"miniray-node-{index}",
                "NodeManagerPort": 0,
            }
        )
    return out


def cluster_resources() -> Dict[str, float]:
    """集群总资源(对齐 ``ray.cluster_resources()``)。"""
    return get_core_worker()._raylet.cluster_resources()


def available_resources() -> Dict[str, float]:
    """当前可用资源(对齐 ``ray.available_resources()``)。"""
    return get_core_worker()._raylet.available_resources()


def timeline(filename: Optional[str] = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """导出时间线。

    :param filename: Chrome Trace JSON 输出路径(可以直接拖进 ``chrome://tracing``
        或 Perfetto 看)。
    :param html: 额外输出一个自包含的 HTML 甘特图(内嵌 SVG,双击即可看)。

    .. note::
       实现放在 ``miniray/_timeline.py`` 而不是 ``miniray/timeline.py`` ——
       因为同名的子模块会在 ``import`` 之后**覆盖**同名的函数属性
       (``ray.timeline`` 是函数,不是模块)。这种坑只有踩过才知道。
    """
    from ._timeline import timeline as _timeline

    return _timeline(filename, **kwargs)


# ---------------------------------------------------------------------------
# 子模块(导入即注册,保证 ``miniray.util.xxx`` / ``miniray.state`` 可用)
# ---------------------------------------------------------------------------
from . import state  # noqa: E402,F401
from . import util  # noqa: E402,F401
