"""异常体系 —— 与真实 Ray 的 ``ray.exceptions`` 一一对应。

分布式系统里「错误」比单机复杂得多,因为错误发生在**另一个进程**里。Ray 的做法是:

1. worker 捕获远程异常,把它连同 traceback 字符串一起序列化;
2. driver 侧 ``ray.get()`` 时**重新抛出**一个 :class:`RayTaskError`,
   原始异常挂在 ``.cause`` 上,可以在需要时 ``.as_instanceof_cause()`` 还原;

这样调用方既能看到「这是一个远程错误」(便于区分本地 bug 与远程 bug),
又能看到原始的异常类型与堆栈。mini-ray 完整复刻这套语义。

.. code-block:: text

   远程 worker                         driver
   ───────────                         ──────
   raise ValueError("boom")
        │
        ▼
   RayTaskError(ValueError)  ──pickle──▶  RayTaskError
   cause = ValueError                    └── .cause          -> ValueError 实例
   traceback_str = "..."                 └── .traceback_str  -> 远程堆栈
                                         └── .as_instanceof_cause() -> ValueError
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "MiniRayError",
    "RayError",
    "RaySystemError",
    "RayTaskError",
    "RayActorError",
    "WorkerCrashedError",
    "TaskCancelledError",
    "ActorDiedError",
    "GetTimeoutError",
    "ObjectLostError",
    "ObjectStoreFullError",
    "ReferenceCountingError",
    "MiniRayletDiedError",
    "CrossLanguageError",
    "RayRuntimeEnvError",
]


class MiniRayError(Exception):
    """所有 mini-ray 异常的基类。"""


# 别名:真实 Ray 里叫 RayError,写代码时两个名字都能用
RayError = MiniRayError


class RaySystemError(MiniRayError):
    """系统级错误(raylet 死了、RPC 断了……)。"""


class RayTaskError(MiniRayError):
    """远程 task 抛出的异常在调用方的表示。

    :param function_name: 出错的远程函数名(便于定位)
    :param cause: 原始的异常实例
    :param traceback_str: 远程进程里的堆栈字符串
    :param proctitle: 出错进程的标题(真实 Ray 会用 ``ray::func_name`` 命名进程)
    """

    def __init__(
        self,
        function_name: str,
        cause: Optional[BaseException] = None,
        traceback_str: Optional[str] = None,
        proctitle: Optional[str] = None,
    ) -> None:
        self.function_name = function_name
        self.cause = cause
        self.traceback_str = traceback_str or ""
        self.proctitle = proctitle or f"miniray::{function_name}"
        cause_repr = f"{type(cause).__name__}: {cause}" if cause is not None else "<unknown>"
        super().__init__(f"{cause_repr}  (来自 {self.proctitle})")

    def as_instanceof_cause(self) -> Exception:
        """还原成原始异常类型,并把远程堆栈拼进消息(模仿 Ray 的同名方法)。

        这样 ``except ValueError`` 也能捕获到 —— 当你确实需要「把远程异常当本地异常处理」时。
        """
        if self.cause is None:
            return self
        cause = self.cause
        cause.args = (
            *cause.args,
            f"  (由 {self.proctitle} 抛出,原始堆栈见下)\n{self.traceback_str}",
        )
        return cause

    def __str__(self) -> str:  # pragma: no cover - 仅影响展示
        return (
            f"{type(self).__name__}: {self.cause!r}\n"
            f"--- 远程堆栈 ({self.proctitle}) ---\n{self.traceback_str}"
        )


class RayActorError(RayTaskError):
    """actor 方法抛出异常,或 actor 已经死亡。"""


class WorkerCrashedError(RayTaskError):
    """worker 进程**崩溃**(段错误 / os._exit / 被 OOM killer 杀掉)。

    与普通 :class:`RayTaskError` 的区别很重要:
    普通异常是「任务逻辑出错」,可以原地重试;worker 崩溃意味着进程没了,
    任务需要**换一个 worker 重跑**,并且它产生的中间对象可能丢失。
    """


class ActorDiedError(RayActorError):
    """actor 进程死亡且无法重启(超出 ``max_restarts``)。"""


class TaskCancelledError(MiniRayError):
    """任务被 ``miniray.cancel()`` 取消。"""


class GetTimeoutError(MiniRayError):
    """``ray.get(ref, timeout=...)`` 超时。"""


class ObjectLostError(MiniRayError):
    """对象丢失且无法通过 lineage 重建。

    真实 Ray 里这个异常代表「对象所在节点的 plasma 没了,而且重新执行生产它的
    task 也失败了」。mini-ray 的 lineage 重建逻辑见 ``raylet.py``。
    """

    def __init__(self, object_id: str, reason: str = "") -> None:
        self.object_id = object_id
        self.reason = reason
        super().__init__(f"对象 {object_id} 已丢失{(': ' + reason) if reason else ''}")

    def __str__(self) -> str:
        # 为什么不用 super().__init__ 里那条消息?因为异常跨进程传递时会走
        # pickle 的「(cls, args) + __dict__」协议:args 里只有一条已经格式化好的
        # 消息,重建时会变成 ObjectLostError(整条消息) —— 于是 object_id 和
        # reason 都被塞进了 object_id,打印出来就是
        # 「对象 对象 xxx 已丢失: ... 已丢失」这种叠字。
        # 把消息改成**按字段现算**,跨进程之后就永远正确。
        return f"对象 {self.object_id} 已丢失{(': ' + self.reason) if self.reason else ''}"

    def __reduce__(self):
        return (ObjectLostError, (self.object_id, self.reason))


class ObjectStoreFullError(MiniRayError):
    """对象存储写满,且无法通过溢出(spill)或驱逐(evict)腾出空间。

    真实 Ray 的 ``ray.exceptions.ObjectStoreFullError`` 语义相同。
    """


class ReferenceCountingError(MiniRayError):
    """引用计数不一致(例如引用了别的 job 的对象)。"""


class MiniRayletDiedError(RaySystemError):
    """raylet 进程/线程已退出,RPC 无法继续。"""


class RayRuntimeEnvError(MiniRayError):
    """runtime_env 配置错误或不支持(例如指定了未实现的 pip 依赖)。"""


class CrossLanguageError(MiniRayError):
    """占位:真实 Ray 支持跨语言(C++/Java)调用,mini-ray 只支持 Python。"""
