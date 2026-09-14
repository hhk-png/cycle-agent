"""``miniray.util.queue.Queue`` —— 跨任务/跨 actor 的分布式队列。

真实 Ray 里 ``ray.util.queue.Queue`` 是官方提供的**唯一一个开箱即用的通信原语**:
它把 ``queue.Queue`` 的语义(阻塞 put/get、maxsize 背压)做成一个 **actor**,
于是队列本身可以当参数传给别的任务、可以被多个 worker 同时消费。

.. code-block:: python

    from miniray.util.queue import Queue

    q = Queue(maxsize=100)

    @miniray.remote
    def produce(q, n):
        for i in range(n):
            q.put(i)          # 满了就阻塞,天然背压
        q.put(None)

    @miniray.remote
    def consume(q):
        out = []
        while True:
            item = q.get()
            if item is None:
                break
            out.append(item)
        return out

    @miniray.remote
    def consumer_entry(q, results):
        results.append(consume(q))     # 消费者自己也是一个任务
        return len(results)

为什么需要它?**因为 ObjectRef 是「一次性」的。**
``ray.get(ref)`` 不会把对象从存储里删掉,但它的生命周期由**引用计数**决定 ——
生产者一退出,ref 就可能被回收,消费者拿不到。队列把「交接」变成了一次
**有状态的**通信:数据放在 actor 的内存里,直到被取走为止。

实现要点(与真实 Ray 的差异见文末):

* 队列本体是一个 **async actor**,所以可以有上千个 ``get()`` 同时挂着等;
* 阻塞语义靠 ``asyncio.Queue`` 自己实现 —— **不是轮询**,
  所以 1000 个等待者不会烧 CPU;
* ``maxsize`` 满时 ``put`` 阻塞、``get`` 空时阻塞,这就是**背压**的载体。
"""

from __future__ import annotations

import asyncio
from queue import Empty, Full
from typing import Any, Dict, Optional

from .. import get as ray_get
from .. import kill as ray_kill
from .. import remote

__all__ = ["Queue", "Empty", "Full", "_QueueActor"]


@remote
class _QueueActor:
    """队列的载体。**不要直接用它**,用 :class:`Queue`。

    .. note::
       事件循环的绑定时机是个坑:actor 的 ``__init__`` 在**事件循环启动之前**
       执行,而 ``asyncio.Queue`` 在 ``put``/``get`` 时会绑定到「第一次用它的
       那个 loop」。所以这里**延迟创建**(``_ensure``),而不是在 ``__init__``
       里直接 ``asyncio.Queue(...)`` —— 否则在不同 Python 版本上会碰到
       ``got Future attached to a different loop`` 这类错误。
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._maxsize = int(maxsize or 0)
        self._queue: Optional["asyncio.Queue"] = None
        self._closed = False

    # ---------------------------------------------------------------- 内部
    def _ensure(self) -> "asyncio.Queue":
        if self._queue is None:
            self._queue = asyncio.Queue(self._maxsize)
        return self._queue

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("队列已经 shutdown,不能再读写")

    # ---------------------------------------------------------------- 写
    async def put(self, item: Any) -> bool:
        """阻塞写:队列满时挂起,直到有消费者取走一个。"""
        self._check_open()
        await self._ensure().put(item)
        return True

    async def put_nowait(self, item: Any) -> bool:
        """非阻塞写。满则返回 ``False``(由 driver 侧抛 ``Full``)。"""
        self._check_open()
        queue = self._ensure()
        if queue.full():
            return False
        queue.put_nowait(item)
        return True

    async def put_with_timeout(self, item: Any, timeout: float) -> bool:
        """带超时写。超时返回 ``False``。"""
        self._check_open()
        try:
            await asyncio.wait_for(self._ensure().put(item), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # ---------------------------------------------------------------- 读
    async def get(self) -> Any:
        """阻塞读:队列空时挂起,直到有生产者放入。"""
        self._check_open()
        return await self._ensure().get()

    async def get_nowait(self) -> Any:
        """非阻塞读。空则返回 ``(False, None)``。

        .. note::
           **为什么返回元组而不是直接抛 ``Empty``?**
           因为异常一旦从 actor 内部抛出,框架会把它包成 ``RayActorError``
           (Ray 和 mini-ray 都是这个行为 —— 见第 2 章 §2.8 的错误模型)。
           那样调用方就只能 ``except RayActorError`` 再拆包,而不是
           ``except queue.Empty``。把判定挪到 driver 侧,
           **API 边界上的异常类型才是干净的** —— 这也是真实 Ray 的处理方式。
        """
        self._check_open()
        queue = self._ensure()
        if queue.empty():
            return (False, None)
        return (True, queue.get_nowait())

    async def get_with_timeout(self, timeout: float) -> Any:
        """带超时读。超时返回 ``(False, None)``。"""
        self._check_open()
        try:
            return (True, await asyncio.wait_for(self._ensure().get(), timeout))
        except asyncio.TimeoutError:
            return (False, None)

    # ---------------------------------------------------------------- 状态
    def qsize(self) -> int:
        return 0 if self._queue is None else self._queue.qsize()

    def empty(self) -> bool:
        return self.qsize() == 0

    def full(self) -> bool:
        if self._maxsize <= 0:
            return False
        return self.qsize() >= self._maxsize

    def shutdown(self) -> bool:
        """标记关闭。之后任何读写都会抛 ``RuntimeError``。"""
        self._closed = True
        return True


class Queue:
    """actor 支撑的分布式队列,API 与 ``queue.Queue`` 对齐。

    :param maxsize: 队列上限。``0``(默认)= 无上限。
        **设成非 0 才能拿到背压** —— 无上限的队列只是把内存问题推后。
    :param actor_options: 传给内部 actor 的选项(如 ``num_cpus``)。
        队列本身几乎不耗 CPU,给它 ``num_cpus=0`` 是常见做法。

    .. code-block:: python

        q = Queue(maxsize=10)
        q.put(1)
        q.put(2, block=False)      # 非阻塞:满了抛 Full
        print(q.qsize())           # 2

        @miniray.remote
        def worker(q):
            return q.get(timeout=5)   # 空队列最多等 5 秒

        print(miniray.get(worker.remote(q)))   # 1
        q.shutdown()

    .. warning::
       ``Queue`` 对象**可以**当参数传给任务(mini-ray 会把 actor handle 序列化过去),
       但**不要**在任务里再包一层 ``miniray.remote`` 去调用它 ——
       队列的方法本来就是远程调用。

    .. note::
       **与真实 Ray 的差异**:

       * 真实 Ray 的 ``Queue`` 内部 actor 类名是 ``_QueueActor``(同名),
         但**是否基于 asyncio**、以及 ``Empty``/``Full`` 到底来自
         ``queue`` 标准库还是 Ray 自己定义,**未确认** ——
         本实现直接复用标准库的 :class:`queue.Empty` / :class:`queue.Full`,
         这样 ``except queue.Empty`` 这种写法在两边都能用;
       * 真实 Ray 的 ``Queue`` 还有 ``__len__``,本实现也提供;
       * 真实 Ray 的 ``Queue`` 在 actor 被杀后行为未定义,本实现明确抛 ``RuntimeError``。
    """

    def __init__(self, maxsize: int = 0, actor_options: Optional[Dict[str, Any]] = None) -> None:
        self.maxsize = int(maxsize or 0)
        self._actor = _QueueActor.options(**(actor_options or {})).remote(self.maxsize)

    # ---------------------------------------------------------------- 写
    def put(self, item: Any, block: bool = True, timeout: Optional[float] = None) -> None:
        """放入一个元素。

        :param block: ``True``(默认)队列满时阻塞;``False`` 立刻抛 :class:`queue.Full`。
        :param timeout: 阻塞的最长秒数,超时抛 :class:`queue.Full`。
        """
        if not block:
            if not ray_get(self._actor.put_nowait.remote(item)):
                raise Full
        elif timeout is not None:
            if not ray_get(self._actor.put_with_timeout.remote(item, float(timeout))):
                raise Full
        else:
            ray_get(self._actor.put.remote(item))

    # ---------------------------------------------------------------- 读
    def get(self, block: bool = True, timeout: Optional[float] = None) -> Any:
        """取出一个元素。

        :param block: ``True``(默认)队列空时阻塞;``False`` 立刻抛 :class:`queue.Empty`。
        :param timeout: 阻塞的最长秒数,超时抛 :class:`queue.Empty`。
        """
        if not block:
            ok, item = ray_get(self._actor.get_nowait.remote())
            if not ok:
                raise Empty
            return item
        if timeout is not None:
            ok, item = ray_get(self._actor.get_with_timeout.remote(float(timeout)))
            if not ok:
                raise Empty
            return item
        return ray_get(self._actor.get.remote())

    # ---------------------------------------------------------------- 状态
    def qsize(self) -> int:
        """当前元素个数(**近似值** —— 远程调用返回时可能已经变了)。"""
        return ray_get(self._actor.qsize.remote())

    def empty(self) -> bool:
        return ray_get(self._actor.empty.remote())

    def full(self) -> bool:
        return ray_get(self._actor.full.remote())

    def shutdown(self) -> None:
        """关闭队列并把内部 actor 杀掉。"""
        try:
            ray_get(self._actor.shutdown.remote())
        finally:
            try:
                ray_kill(self._actor)
            except Exception:  # pragma: no cover - actor 可能已经没了
                pass

    # ---------------------------------------------------------------- 杂项
    @property
    def actor(self):
        """内部的 actor handle(真实 Ray 也暴露 ``.actor``)。"""
        return self._actor

    def __len__(self) -> int:
        return self.qsize()

    def __repr__(self) -> str:  # pragma: no cover - 展示
        return f"Queue(maxsize={self.maxsize}, actor={self._actor.actor_id.hex()[:12]})"
