"""ObjectRef —— 「未来值的句柄」。

``f.remote(x)`` 不会阻塞,它立刻返回一个 :class:`ObjectRef`,代表「将来会有」的值。
这个设计是 Ray 异步性的根源:你可以提交 1000 个任务而一个都不等,
然后一次性 ``ray.get``。

ObjectRef 同时承担了 **引用计数** 的职责(和 Ray 一样):

* 创建时 +1、被 GC 时 -1,增量通过后台线程批量汇报给 raylet;
* 只要还有引用活着,对象就不能被回收;
* ``ray.get`` 到 numpy 数组时,对象会被额外 **pin** 住(见 object_store.py),
  pin 跟着**数组**的生命周期走,而不是跟着 ObjectRef —— 因为用户可能
  ``arr = ray.get(ref); del ref`` 之后继续用 ``arr``。

.. code-block:: text

   driver                     raylet                     worker
   ──────                     ──────                     ──────
   f.remote(x)
     └─ 提交任务 ─────────────▶ 排队 ──分配 worker───────▶ 执行 f(x)
     ◀── ObjectRef(oid) ───── 登记 refs                    └─ put 结果对象
   ray.get(ref)
     └─ 等对象就绪 ──────────▶ (依赖通知) ────────────────▶
     ◀── 零拷贝视图 / bytes ──
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from . import serialization
from .errors import MiniRayError
from .execution import error_from_payload
from .ids import ObjectID

__all__ = ["ObjectRef", "ObjectRefGenerator"]


class ObjectRef:
    """一个对象(未来值)的句柄。

    :param object_id: 对象的 ID
    :param owner_address: 拥有它的 raylet 地址(``"ip:port"``)。
        mini-ray 只有一个 raylet,所以这个字段主要是为了「跨集群/序列化」的完整性,
        以及给报错信息提供上下文。
    :param call_site: 创建它的位置(``f.remote()`` 的调用点),用于调试
    """

    __slots__ = ("_id", "_owner_address", "_call_site", "_released", "__weakref__")

    def __init__(
        self,
        object_id: ObjectID,
        owner_address: str = "",
        call_site: str = "",
        *,
        _register: bool = True,
    ) -> None:
        self._id = object_id
        self._owner_address = owner_address
        self._call_site = call_site
        self._released = False
        if _register:
            from . import runtime

            worker = runtime.get_core_worker(create=False)
            if worker is not None:
                worker.add_object_ref(self)

    # ---------------------------------------------------------------- 属性
    @property
    def id(self) -> ObjectID:
        return self._id

    @property
    def owner_address(self) -> str:
        return self._owner_address

    def hex(self) -> str:
        return self._id.hex()

    def __repr__(self) -> str:
        return f"ObjectRef({self._id.hex()})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ObjectRef) and self._id == other._id

    def __hash__(self) -> int:
        return hash(self._id)

    def __lt__(self, other: "ObjectRef") -> bool:
        return self._id < other._id

    # ------------------------------------------------------- 序列化与生命周期
    def __reduce__(self):
        # ObjectRef 作为**值**传递时(任务返回值里带 ref、actor handle 里带 ref),
        # 必须把 ID 与 owner 地址一起带上,接收方才能去 ray.get。
        return (
            _rebuild_object_ref,
            (self._id.binary(), self._owner_address, self._call_site),
        )

    def __del__(self) -> None:
        self._release()

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            from . import runtime

            worker = runtime.get_core_worker(create=False)
            if worker is not None:
                worker.remove_object_ref(self)
        except Exception:  # pragma: no cover - 解释器退出时什么都可能发生
            pass

    # ---------------------------------------------------------------- 取值
    def __await__(self):
        """支持 ``await ref``(异步 actor / asyncio driver 里用)。

        实现上把阻塞的 ``ray.get`` 丢到线程池里跑 —— 因为 ObjectRef 的取值
        走的是同步 RPC,而事件循环不能被阻塞。
        """
        return _await_ref(self).__await__()

    async def _async_get(self, timeout: Optional[float] = None) -> Any:
        return (await _await_ref(self, timeout=timeout))


def _rebuild_object_ref(raw_id: bytes, owner_address: str, call_site: str) -> ObjectRef:
    """反序列化端:重建 ObjectRef(走 ``_register=True``,所以引用计数会 +1)。"""
    return ObjectRef(ObjectID(raw_id), owner_address, call_site)


async def _await_ref(ref: ObjectRef, timeout: Optional[float] = None) -> Any:
    from . import get

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: get(ref, timeout=timeout))


# ---------------------------------------------------------------------------
# 生成器(streaming generator / num_returns="dynamic")
# ---------------------------------------------------------------------------


class ObjectRefGenerator:
    """``num_returns="dynamic"`` 的生成器 task 返回的东西。

    Ray 的流式生成器语义:一个 task 可以 ``yield`` 多次,每次产出一个对象,
    下游可以**边产边消费** —— 这是 Ray 做流式推理/大结果分块的基础设施。

    .. code-block:: python

        @ray.remote(num_returns="dynamic")
        def chunks(n):
            for i in range(n):
                yield i

        gen = chunks.remote(3)
        for ref in gen:              # 每 yield 一个,这里就多一个可取的 ref
            print(ray.get(ref))      # 0 / 1 / 2

    注意 **mini-ray** 要求生成器 task 的 ``max_retries=0``。

    这是 mini-ray 的**刻意简化**,不是 Ray 的限制:
    真实 Ray **支持**生成器任务重试,语义是「跳过已产出的值、从断点继续」
    (重新调用函数,忽略前 N 个已 yield 的值,从第 N+1 个接着 yield)。
    流式重放要做对很难,mini-ray 宁可显式不支持,也不给一个会错位的实现。
    详见教程第 5 章 5.3 与第 10 章 10.2。
    """

    def __init__(self, generator_id: str, owner_address: str, num_returns: int = -1) -> None:
        self._generator_id = generator_id
        self._owner_address = owner_address
        self._num_returns = num_returns
        self._index = 0
        self._refs: List[ObjectRef] = []
        self._finished = False
        #: 生成器**失败**结束时的异常(正常结束为 ``None``)
        self._error: Optional[BaseException] = None

    @property
    def generator_id(self) -> str:
        return self._generator_id

    def __iter__(self) -> "ObjectRefGenerator":
        return self

    def __next__(self) -> ObjectRef:
        """等下一个 chunk 就绪,返回它的 ObjectRef。"""
        while True:
            self._refresh()
            if self._index < len(self._refs):
                ref = self._refs[self._index]
                self._index += 1
                return ref
            if self._finished:
                # 失败结束:取完已产出的 chunk 之后把异常抛出来。
                # (顺序很重要 —— 先交付已经拿到的结果,再报错。)
                if self._error is not None:
                    raise self._error
                raise StopIteration
            # 阻塞等 raylet:有新 chunk 或生成器结束才返回(条件变量,不是轮询)
            self._worker().wait_for_generator(self._generator_id, self._index)

    def __len__(self) -> int:
        """已知的 chunk 数(生成器还没跑完时只反映「到目前为止」的个数」)。"""
        self._refresh()
        return len(self._refs)

    def _refresh(self) -> None:
        info = self._worker().get_generator_state(self._generator_id)
        self._refs = [
            ObjectRef(ObjectID(bytes.fromhex(oid)), self._owner_address) for oid in info["refs"]
        ]
        self._finished = bool(info["done"])
        payload = info.get("error")
        self._error = error_from_payload(payload) if payload else None

    @staticmethod
    def _worker():
        from . import runtime

        worker = runtime.get_core_worker(create=False)
        if worker is None:
            raise MiniRayError("遍历 ObjectRefGenerator 需要在 miniray.init() 之后进行")
        return worker

    def __repr__(self) -> str:
        return (
            f"ObjectRefGenerator(id={self._generator_id[:8]}, "
            f"chunks={len(self._refs)}, finished={self._finished})"
        )


# 注册给序列化层:从此 ``serialization.dumps`` 认识 ObjectRef,
# 任务参数里的 ref 会被内联成占位符(见 serialization.py 的模块注释)。
serialization.register_object_ref_type(ObjectRef)
