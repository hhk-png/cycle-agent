"""``@miniray.remote`` —— 把普通 Python 函数/类变成「远程可调用对象」。

这是整个框架的门面。看起来只是个装饰器,实际上它决定了三件事:

1. **调用语义**:``f.remote(x)`` 是**异步提交**,立刻返回 ObjectRef;
   真正的执行发生在别的进程里(除非 ``local_mode``)。
2. **资源与调度**:``@miniray.remote(num_gpus=1)`` 里的声明会变成调度约束 ——
   这就是 Ray 里「用 Python 描述集群资源」的方式。
3. **函数怎么传过去**:装饰器只持有原函数;真正导出到函数表发生在第一次
   ``.remote()``(见 ``function_manager.py``)。

.. code-block:: python

    @miniray.remote(num_cpus=2, max_retries=3)
    def train_shard(path):
        return ...

    ref = train_shard.remote("s3://...")      # 异步:立刻返回 ObjectRef
    value = miniray.get(ref)                  # 同步:阻塞取值

    # 运行期改选项
    ref = train_shard.options(num_cpus=8).remote("s3://...")
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Dict, Optional

from .errors import MiniRayError
from .object_ref import ObjectRef, ObjectRefGenerator

__all__ = ["RemoteFunction", "remote", "method", "TASK_OPTION_KEYS"]


#: task 上可用的选项(= ``ray.remote(...)`` 认识的关键字)
TASK_OPTION_KEYS = {
    "num_cpus",
    "num_gpus",
    "memory",
    "resources",
    "max_retries",
    "num_returns",
    "runtime_env",
    "scheduling_strategy",
    "name",
    "placement_group",  # 已弃用,但为了兼容旧代码仍然接受
    "placement_group_bundle_index",
}


class RemoteFunction:
    """被 ``@miniray.remote`` 包装后的函数。"""

    def __init__(
        self,
        function: Callable,
        options: Optional[Dict[str, Any]] = None,
        bound_args: Optional[tuple] = None,
        bound_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not callable(function):
            raise MiniRayError(f"miniray.remote 需要可调用对象,得到 {type(function).__name__}")
        self._function = function
        self._options = dict(options or {})
        self._bound_args = bound_args
        self._bound_kwargs = dict(bound_kwargs or {})
        functools.update_wrapper(self, function)

    # ---------------------------------------------------------------- 属性
    @property
    def options_dict(self) -> Dict[str, Any]:
        return dict(self._options)

    def _get_function(self) -> Callable:
        """绑定参数后的实际函数(``.bind()`` 用)"""
        if self._bound_args is None and not self._bound_kwargs:
            return self._function
        base = self._function

        @functools.wraps(base)
        def _bound(*args, **kwargs):
            merged = dict(self._bound_kwargs)
            merged.update(kwargs)
            return base(*self._bound_args, *args, **merged)

        return _bound

    # ---------------------------------------------------------------- 调用
    def __call__(self, *args, **kwargs):
        raise TypeError(
            f"远程函数 {getattr(self._function, '__name__', '?')} 不能直接调用。"
            "请用 .remote(*args) 提交任务并用 miniray.get(ref) 取结果。"
            "提示:如果你确实想在本地执行,用 miniray.get(f.remote(...)),"
            "或者用 miniray.init(local_mode=True) 让它在 driver 里跑。"
        )

    def remote(self, *args, **kwargs):
        """提交任务。返回 ObjectRef(``num_returns=1``)或 ObjectRef 列表。"""
        from . import runtime

        worker = runtime.get_core_worker()
        options = self._options
        function = self._get_function()
        # num_returns="dynamic" 是生成器任务:它 yield 多少个值就有多少个返回对象,
        # 所以这里不能按整数解析(Ray 用同一个参数表达两种语义)。
        #
        # ⚠️ 与真实 Ray 的**字面量差异**(刻意保留,不是笔误):
        #   真实 Ray 现在用 "streaming";"dynamic" 是**已弃用的旧名**。
        #   mini-ray 只认 "dynamic" —— 写 "streaming" 会走到下面的 int() 并抛
        #   ValueError: invalid literal for int() with base 10: 'streaming'。
        #   迁移时必须把这个字面量一起改(教程第 21 章 A.2 与附录 B B.3.1 有对照)。
        is_generator = options.get("num_returns") == "dynamic"
        num_returns = 1 if is_generator else int(options.get("num_returns", 1) or 1)

        # 生成器函数必须显式声明 num_returns="dynamic" —— 否则 Python 会把
        # 生成器对象本身当成返回值,然后在序列化时报一个和真正原因无关的错。
        # Ray 在这里也是明确报错(它比 mini-ray 更早:提交时就拦)。
        if not is_generator and inspect.isgeneratorfunction(function):
            raise MiniRayError(
                f"{getattr(function, '__name__', '函数')} 是一个生成器函数,"
                '必须用 @miniray.remote(num_returns="dynamic") 声明。'
                "生成器任务会 yield 多个对象,不能按普通任务(num_returns=1)提交。"
            )

        strategy = options.get("scheduling_strategy")
        if strategy is None and options.get("placement_group") is not None:
            # 老写法:placement_group=pg 等价于 PlacementGroupSchedulingStrategy
            from .scheduling_strategies import PlacementGroupSchedulingStrategy

            strategy = PlacementGroupSchedulingStrategy(
                options["placement_group"],
                placement_group_bundle_index=int(options.get("placement_group_bundle_index", -1)),
            )

        result = worker.submit_task(
            function,
            args,
            kwargs,
            num_returns=num_returns,
            num_cpus=options.get("num_cpus"),
            num_gpus=options.get("num_gpus"),
            memory=options.get("memory"),
            resources=options.get("resources"),
            max_retries=int(options.get("max_retries", 3)),
            name=options.get("name") or getattr(self._function, "__name__", None),
            runtime_env=options.get("runtime_env"),
            scheduling_strategy=strategy,
            is_generator=is_generator,
        )
        if isinstance(result, ObjectRefGenerator):
            return result
        if num_returns == 1:
            return result[0]
        return result

    def options(self, **options: Any) -> "RemoteFunction":
        """返回一个带新选项的副本(不改原对象)。"""
        unknown = set(options) - TASK_OPTION_KEYS
        if unknown:
            raise MiniRayError(
                f"未知的 task 选项: {', '.join(sorted(unknown))}。"
                f"可用选项: {', '.join(sorted(TASK_OPTION_KEYS))}"
            )
        merged = dict(self._options)
        merged.update(options)
        return RemoteFunction(self._function, merged, self._bound_args, self._bound_kwargs)

    def bind(self, *args: Any, **kwargs: Any) -> "RemoteFunction":
        """绑定一部分参数,返回新的 RemoteFunction(Ray 2.8+ 的 ``.bind()``)。

        用途:把一个「模板任务」传给别的任务/actor 再补参数。
        """
        return RemoteFunction(self._function, self._options, args, kwargs)

    def __repr__(self) -> str:
        name = getattr(self._function, "__qualname__", getattr(self._function, "__name__", "?"))
        return f"RemoteFunction({name})"


def remote(*args: Any, **kwargs: Any):
    """``@miniray.remote`` 装饰器。

    三种用法::

        @miniray.remote                     # 默认选项
        @miniray.remote(num_gpus=1)         # 带选项
        miniray.remote(func)                # 直接调用
    """
    if len(args) == 1 and not kwargs and (inspect.isfunction(args[0]) or inspect.isclass(args[0])):
        target = args[0]
        if inspect.isclass(target):
            from .actor import ActorClass

            return ActorClass(target, {})
        return RemoteFunction(target, {})

    if args:
        raise MiniRayError(
            "@miniray.remote 的位置参数只能是函数/类本身;"
            "选项请用关键字,例如 @miniray.remote(num_cpus=2)"
        )

    def _decorator(target: Any):
        if inspect.isclass(target):
            from .actor import ActorClass

            return ActorClass(target, kwargs)
        if not callable(target):
            raise MiniRayError(
                f"@miniray.remote 只能装饰函数或类,得到 {type(target).__name__}"
            )
        return RemoteFunction(target, kwargs)

    return _decorator


def method(*, concurrency_group: Optional[str] = None, num_returns: int = 1):
    """装饰 actor 方法,声明它的并发组与返回个数(对齐 ``ray.method``)。

    .. code-block:: python

        @miniray.remote(concurrency_groups={"io": 2, "compute": 4})
        class MyActor:
            @miniray.method(concurrency_group="io")
            def fetch(self): ...

            @miniray.method(concurrency_group="compute", num_returns=2)
            def split(self): ...
    """

    def _decorator(func: Callable) -> Callable:
        metadata = getattr(func, "_miniray_method_metadata", {})
        metadata = dict(metadata)
        if concurrency_group is not None:
            metadata["concurrency_group"] = concurrency_group
        metadata["num_returns"] = int(num_returns)
        func._miniray_method_metadata = metadata  # type: ignore[attr-defined]
        return func

    return _decorator
