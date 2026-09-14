"""Actor —— 有状态的分布式对象。

Ray 的 actor 模型可以用一句话概括:**「一个常驻进程 + 一个邮箱」**。

.. code-block:: text

    driver                          raylet                       actor 进程
    ──────                          ──────                       ──────────
    Counter.remote()  ──────────▶  占资源(终身持有)
                                   起进程 ─────────────────────▶ __init__()
    handle.inc.remote()  ───────▶  投进邮箱 ───────────────────▶ 顺序执行
    ◀── ObjectRef ────             分配结果对象
    miniray.get(ref)  ◀─────────── 结果写回对象存储 ─────────────

三个必须记住的语义:

1. **方法调用是异步的**:``handle.method.remote()`` 立刻返回 ObjectRef,
   actor 内部按**提交顺序**串行执行(默认并发度为 1)。
2. **资源是终身持有的**:actor 从创建到销毁一直占着它的 CPU/GPU ——
   这就是为什么「actor 太多会把集群占满」。
3. **行为**:actor 挂了默认**不重启**(``max_restarts=0``),在飞的方法调用
   全部失败。要重启得显式 ``max_restarts=N``,并且**状态会丢**
   (Ray 的行为:重启后重新跑 ``__init__``)。有状态恢复要靠自己写检查点。

并发模型(``max_concurrency`` / ``concurrency_groups``):见 ``worker.py`` 的
``_SyncActorContext`` / ``_AsyncActorContext``;async actor 的默认并发度是 1000,
sync actor 是 1(与 Ray 文档一致)。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import MiniRayError
from .ids import ActorID
from .object_ref import ObjectRef

__all__ = ["ActorClass", "ActorHandle", "ActorMethod", "ACTOR_OPTION_KEYS"]


#: actor 上可用的选项
ACTOR_OPTION_KEYS = {
    "num_cpus",
    "num_gpus",
    "memory",
    "resources",
    "max_restarts",
    "max_task_retries",
    "max_concurrency",
    "concurrency_groups",
    "name",
    "namespace",
    "lifetime",
    "runtime_env",
    "scheduling_strategy",
    "placement_group",
    "placement_group_bundle_index",
}


class ActorMethod:
    """``handle.method`` —— 一个可 ``.remote()`` 的方法代理。"""

    def __init__(
        self,
        actor_handle: "ActorHandle",
        method_name: str,
        *,
        bound_args: Tuple[Any, ...] = (),
        bound_kwargs: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._actor_handle = actor_handle
        self._method_name = method_name
        self._bound_args = tuple(bound_args)
        self._bound_kwargs = dict(bound_kwargs or {})
        self._options = dict(options or {})

    def remote(self, *args: Any, **kwargs: Any):
        """调用 actor 方法(异步)。返回 ObjectRef(或 ObjectRef 列表)。

        ``.bind()`` 绑定的参数会**前置**到本次调用的参数之前。
        """
        merged_args = (*self._bound_args, *args)
        merged_kwargs = {**self._bound_kwargs, **kwargs}
        return self._actor_handle._call(
            self._method_name, merged_args, merged_kwargs, options=self._options
        )

    def options(self, **options: Any) -> "ActorMethod":
        """方法级选项(``num_returns`` 等)。

        返回副本,不改原对象 —— 与 ``RemoteFunction.options()`` 语义一致。
        """
        merged = dict(self._options)
        merged.update(options)
        return ActorMethod(
            self._actor_handle,
            self._method_name,
            bound_args=self._bound_args,
            bound_kwargs=self._bound_kwargs,
            options=merged,
        )

    def bind(self, *args: Any, **kwargs: Any) -> "ActorMethod":
        """绑定一部分参数,返回新的 ``ActorMethod``(Ray 2.8+ 的 ``.bind()``)。

        用途:把一个「模板方法调用」传给别的任务/actor 再补参数，
        也常用于 ``ActorPool.submit`` 里预置固定参数。
        """
        return ActorMethod(
            self._actor_handle,
            self._method_name,
            bound_args=(*self._bound_args, *args),
            bound_kwargs={**self._bound_kwargs, **kwargs},
            options=self._options,
        )

    def __repr__(self) -> str:
        return f"ActorMethod({self._actor_handle._class_name}.{self._method_name})"

    def __call__(self, *args, **kwargs):
        raise TypeError(
            f"actor 方法 {self._method_name} 不能直接调用,请用 .remote(*args)"
        )


class ActorHandle:
    """actor 的句柄。它本身是**可序列化**的 —— 可以当参数传给别的任务。

    .. code-block:: python

        counter = Counter.remote()          # driver 里创建

        @miniray.remote
        def bump(handle):                   # 把 handle 传给 worker
            return miniray.get(handle.inc.remote())

        miniray.get(bump.remote(counter))   # worker 通过 handle 远程调用 actor

    这就是 Ray 里参数服务器、多 actor 协作的基础。
    """

    def __init__(
        self,
        actor_id: ActorID,
        class_name: str,
        method_metadata: Dict[str, Dict[str, Any]],
        *,
        creation_ref: Optional[ObjectRef] = None,
        node_id: Optional[str] = None,
    ) -> None:
        self._actor_id = actor_id
        self._class_name = class_name
        self._method_metadata = method_metadata
        self._creation_ref = creation_ref
        self._node_id = node_id

    # ---------------------------------------------------------------- 属性
    @property
    def actor_id(self) -> ActorID:
        return self._actor_id

    @property
    def _actor_id_hex(self) -> str:  # pragma: no cover - 兼容写法
        return self._actor_id.hex()

    # ---------------------------------------------------------------- 调用
    def __getattr__(self, name: str) -> ActorMethod:
        if name.startswith("_"):
            raise AttributeError(name)
        return ActorMethod(self, name)

    def _call(
        self,
        method_name: str,
        args: tuple,
        kwargs: Dict[str, Any],
        *,
        options: Optional[Dict[str, Any]] = None,
    ):
        from . import runtime

        worker = runtime.get_core_worker()
        metadata = self._method_metadata.get(method_name, {})
        # 方法级 options 优先于 @miniray.method(...) 的元数据
        opts = options or {}
        num_returns = int(opts.get("num_returns", metadata.get("num_returns", 1)) or 1)
        refs = worker.call_actor_method(
            self._actor_id,
            method_name,
            args,
            kwargs,
            num_returns=num_returns,
            runtime_env=opts.get("runtime_env"),
        )
        if num_returns == 1:
            return refs[0]
        return refs

    # ------------------------------------------------------- 序列化与展示
    def __reduce__(self):
        return (
            _rebuild_actor_handle,
            (self._actor_id.hex(), self._class_name, self._method_metadata),
        )

    def __repr__(self) -> str:
        return f"ActorHandle({self._class_name}, {self._actor_id.hex()[:8]})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ActorHandle) and other._actor_id == self._actor_id

    def __hash__(self) -> int:
        return hash(self._actor_id)

    def _describe(self) -> Dict[str, Any]:
        """给 state API / 日志用的信息。"""
        from . import runtime

        worker = runtime.get_core_worker(create=False)
        info: Dict[str, Any] = {
            "actor_id": self._actor_id.hex(),
            "class_name": self._class_name,
            "node_id": self._node_id,
        }
        if worker is not None:
            try:
                remote_info = worker._gcs.get_actor(self._actor_id.hex())
                if remote_info:
                    info.update(remote_info)
            except Exception:  # pragma: no cover
                pass
        return info


def _rebuild_actor_handle(
    actor_id_hex: str, class_name: str, method_metadata: Dict[str, Dict[str, Any]]
) -> ActorHandle:
    """反序列化端:用当前进程的 runtime 重建句柄。"""
    return ActorHandle(ActorID(bytes.fromhex(actor_id_hex)), class_name, method_metadata)


class ActorClass:
    """``@miniray.remote`` 装饰过的类。``.remote(...)`` 才是创建 actor。"""

    def __init__(self, actor_class: type, options: Optional[Dict[str, Any]] = None) -> None:
        if not inspect.isclass(actor_class):
            raise MiniRayError(f"ActorClass 需要一个类,得到 {type(actor_class).__name__}")
        self._actor_class = actor_class
        self._options = dict(options or {})

    # ---------------------------------------------------------------- 属性
    @property
    def options_dict(self) -> Dict[str, Any]:
        return dict(self._options)

    def __call__(self, *args, **kwargs):
        raise TypeError(
            f"actor 类 {self._actor_class.__name__} 不能直接实例化(那会在 driver 里创建)。"
            "请用 .remote(*args) 创建 actor。"
        )

    # ---------------------------------------------------------------- 创建
    def remote(self, *args: Any, **kwargs: Any) -> ActorHandle:
        """创建 actor(异步)。返回 :class:`ActorHandle`。"""
        from . import runtime

        worker = runtime.get_core_worker()
        metadata = collect_method_metadata(self._actor_class)
        options = dict(self._options)
        options.update(
            {
                "method_metadata": metadata,
                "is_async": any(m.get("is_async") for m in metadata.values()),
                "namespace": options.get("namespace") or worker.namespace,
            }
        )
        if options.get("scheduling_strategy") is not None:
            strategy = options.pop("scheduling_strategy")
            encoder = getattr(strategy, "_encode", None)
            if encoder is not None:
                strategy = encoder()
            if not isinstance(strategy, dict):
                # 不认识的策略:明确报错,不要像以前那样 pop 掉之后**静默丢弃**
                # (静默丢弃的表现是「策略不生效」,而调用方完全收不到任何信号)
                raise MiniRayError(f"不认识的调度策略: {strategy!r}")
            options["scheduling_strategy"] = strategy
        pattern = options.pop("placement_group", None)
        if pattern is not None:
            from .scheduling_strategies import PlacementGroupSchedulingStrategy

            strategy = PlacementGroupSchedulingStrategy(
                pattern,
                placement_group_bundle_index=int(options.pop("placement_group_bundle_index", -1)),
            )
            options["scheduling_strategy"] = strategy._encode()
        reply = worker.create_actor(self._actor_class, args, kwargs, options=options)
        # 创建任务本身也是一个对象:__init__ 失败时,异常就写在这个对象里,
        # 第一个方法调用会把它抛出来(与 Ray 的行为一致)。
        creation_ref = _ref_from_hex(reply["creation_object_id"], worker._raylet.address)
        return ActorHandle(
            ActorID(bytes.fromhex(reply["actor_id"])),
            self._actor_class.__name__,
            metadata,
            creation_ref=creation_ref,
            node_id=reply.get("node_id"),
        )

    def options(self, **options: Any) -> "ActorClass":
        unknown = set(options) - ACTOR_OPTION_KEYS
        if unknown:
            raise MiniRayError(
                f"未知的 actor 选项: {', '.join(sorted(unknown))}。"
                f"可用选项: {', '.join(sorted(ACTOR_OPTION_KEYS))}"
            )
        merged = dict(self._options)
        merged.update(options)
        return ActorClass(self._actor_class, merged)

    def __repr__(self) -> str:
        return f"ActorClass({self._actor_class.__name__})"


def _ref_from_hex(object_id_hex: str, owner_address: str) -> ObjectRef:
    from .ids import ObjectID

    return ObjectRef(ObjectID(bytes.fromhex(object_id_hex)), owner_address)


def collect_method_metadata(actor_class: type) -> Dict[str, Dict[str, Any]]:
    """扫描 actor 类,收集每个方法的元数据(并发组、返回个数、是否 async)。

    在 **driver 侧**算好再随创建请求发给 raylet,这样 raylet 不用去理解
    Python 的类(它只认数据)—— 这也是 Ray 的做法(actor 方法元数据在
    core worker 侧收集并向 GCS 注册)。
    """
    metadata: Dict[str, Dict[str, Any]] = {}
    for name, member in inspect.getmembers(actor_class):
        if name.startswith("_"):
            continue
        if not callable(member):
            continue
        declared = dict(getattr(member, "_miniray_method_metadata", {}) or {})
        metadata[name] = {
            "concurrency_group": declared.get("concurrency_group", ""),
            "num_returns": int(declared.get("num_returns", 1) or 1),
            "is_async": inspect.iscoroutinefunction(member),
        }
    return metadata
