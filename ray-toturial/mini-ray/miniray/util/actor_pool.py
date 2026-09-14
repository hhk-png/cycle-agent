"""``miniray.util.ActorPool`` —— 用一组 actor 并行跑同一个函数。

Ray 官方文档里 ``ActorPool`` 是「常见模式」之一:模型推理、参数服务器、
分片计算 —— 凡是「同一个函数 + 一堆输入 + 一组固定 actor」的场景都适用。

.. code-block:: python

    pool = ActorPool([Worker.remote() for _ in range(4)])
    for result in pool.map_unordered(lambda actor, x: actor.process.remote(x), items):
        print(result)

实现上的关键点:**背压**。``map`` 不会一次性把所有任务都提交出去(那会把
对象存储和 actor 邮箱挤爆),而是「每个 actor 手里最多一个任务」,
拿到结果再补下一个 —— 这正是 Ray 官方推荐的
``ray.wait`` 背压模式(见 ``ray-core/patterns/limit-pending-tasks``)。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Sequence

from .. import get as ray_get
from .. import wait as ray_wait
from ..errors import GetTimeoutError, MiniRayError
from ..object_ref import ObjectRef

__all__ = ["ActorPool"]


class ActorPool:
    """一组 actor 的工作池。

    :param actors: actor handle 列表(通常是 ``ActorClass.remote()`` 的产物)
    """

    def __init__(self, actors: Sequence[Any]) -> None:
        if not actors:
            raise MiniRayError("ActorPool 需要至少一个 actor")
        self._idle: Deque[Any] = deque(actors)
        self._busy: Dict[ObjectRef, Any] = {}
        self._all = list(actors)

    # ---------------------------------------------------------------- 基本信息
    def num_actors(self) -> int:
        return len(self._all)

    def num_idle(self) -> int:
        return len(self._idle)

    def num_busy(self) -> int:
        return len(self._busy)

    def pop_idle(self) -> Any:
        """取出一个空闲 actor(没有空闲的会抛异常 —— 调用方负责判断)。"""
        if not self._idle:
            raise MiniRayError("没有空闲的 actor 了(先用 has_free() / num_idle() 判断)")
        return self._idle.popleft()

    def pop_busy(self) -> tuple:
        """取回一个**已完成**任务的 actor,返回 ``(actor, ref)``。"""
        if not self._busy:
            raise MiniRayError("没有在跑的 actor")
        ready, _ = ray_wait(list(self._busy.keys()), num_returns=1)
        ref = ready[0]
        actor = self._busy.pop(ref)
        self._idle.append(actor)
        return actor, ref

    def has_free(self) -> bool:
        return bool(self._idle)

    def has_next(self) -> bool:
        return bool(self._busy)

    # ---------------------------------------------------------------- 提交/收取
    def submit(self, fn: Callable[[Any, Any], ObjectRef], value: Any) -> ObjectRef:
        """用下一个空闲 actor 提交一个任务。返回 ObjectRef。"""
        actor = self.pop_idle()
        ref = fn(actor, value)
        if not isinstance(ref, ObjectRef):
            raise MiniRayError(
                "ActorPool.submit 的回调必须返回 ObjectRef —— "
                "通常是 lambda actor, x: actor.method.remote(x) 这样的写法"
            )
        self._busy[ref] = actor
        return ref

    def get_next(self, timeout: Optional[float] = None) -> Any:
        """等任意一个任务完成,返回它的结果(会阻塞)。"""
        if not self._busy:
            raise MiniRayError("没有在跑的任务")
        ready, _ = ray_wait(list(self._busy.keys()), num_returns=1, timeout=timeout)
        if not ready:
            raise GetTimeoutError(f"等 {timeout} 秒还没有任务完成")
        ref = ready[0]
        actor = self._busy.pop(ref)
        self._idle.append(actor)
        return ray_get(ref)

    # ---------------------------------------------------------------- 批量映射
    def map(self, fn: Callable[[Any, Any], ObjectRef], values: Sequence[Any]) -> Iterator[Any]:
        """按**提交顺序**返回结果(可能有队头阻塞:第一个慢,后面都要等)。

        实现要点:单独维护一个「提交顺序」的队列。``get_next()`` 是「谁先算完
        谁先返回」,直接用它做 ordered map 会得到乱序结果(这是个很容易写错的
        地方)。有序版本必须**盯着队头的那个 ref 等**,它回来了才轮到下一个。
        """
        iterator = iter(values)
        order: Deque[ObjectRef] = deque()

        def fill() -> None:
            while self.has_free():
                try:
                    value = next(iterator)
                except StopIteration:
                    return
                order.append(self.submit(fn, value))

        fill()
        while order:
            ref = order.popleft()
            value = ray_get(ref)
            actor = self._busy.pop(ref, None)
            if actor is not None:
                self._idle.append(actor)
            fill()
            yield value

    def map_unordered(
        self, fn: Callable[[Any, Any], ObjectRef], values: Sequence[Any]
    ) -> Iterator[Any]:
        """谁先算完先返回谁(吞吐优先,顺序不保证)。"""
        iterator = iter(values)
        while self.has_free():
            try:
                value = next(iterator)
            except StopIteration:
                break
            self.submit(fn, value)
        while self.has_next():
            yield self.get_next()
            while self.has_free():
                try:
                    value = next(iterator)
                except StopIteration:
                    break
                self.submit(fn, value)

    def __repr__(self) -> str:  # pragma: no cover - 展示
        return f"ActorPool(actors={self.num_actors()}, idle={self.num_idle()}, busy={self.num_busy()})"
