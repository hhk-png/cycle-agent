"""任务执行的公共逻辑 —— worker 进程与 ``local_mode`` 共用同一套代码。

「怎么执行一个 task」这件事,在两种场景下是**完全一样**的:

* worker 进程里(跨进程边界);
* ``local_mode`` 下(在 raylet 线程里直接跑)。

所以把这段逻辑抽出来共享。这也顺带说明了 ``local_mode`` 的价值与代价:
**同一份用户代码、同一条执行路径,只是没有了进程边界** —— 断点能停、
`print` 不会被缓冲、异常堆栈是连续的,代价是没有并行、全局状态会污染 driver。

.. code-block:: text

   任务消息(纯数据)                    执行
   ─────────────────                    ────
   function: FunctionDescriptor  ──▶  从函数表取回函数
   args: bytes(pickle)           ──▶  取依赖对象 → 内联 → 反序列化
   deps: [oid…]                  ──▶  raylet.fetch_objects(零拷贝/拷贝)
   runtime_env: {env_vars: …}    ──▶  临时改环境变量,执行完还原
   gpu_ids: [0]                  ──▶  CUDA_VISIBLE_DEVICES=0
   result_ids: [oid…]            ──▶  结果写回对象存储
"""

from __future__ import annotations

import builtins
import contextlib
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import MiniRayError, RayTaskError

__all__ = [
    "unpack_args",
    "normalize_results",
    "error_payload",
    "error_from_payload",
    "runtime_env_context",
]


def unpack_args(args: Tuple[Any, ...]) -> Tuple[tuple, Dict[str, Any]]:
    """拆开 ``(a, b, _Kwargs({...}))`` 这种打包。

    为什么要打包?因为 ``f.remote(1, k=2)`` 里的 kwargs 必须和 args 走**同一条**
    序列化通道(它们可能都含 ObjectRef,都要内联),所以统一塞进 args 的末尾,
    执行前再拆开。
    """
    from .core_worker import _Kwargs

    if args and isinstance(args[-1], _Kwargs):
        return tuple(args[:-1]), dict(args[-1].kwargs)
    return tuple(args), {}


def normalize_results(value: Any, num_returns: int) -> List[Any]:
    """把函数返回值整理成 ``num_returns`` 个对象。

    Ray 的语义:

    * ``num_returns=1``(默认)—— 返回值就是唯一那个对象;
    * ``num_returns=n`` —— 返回值必须是长度恰好为 n 的 tuple/list。

    不满足就报错,而不是悄悄塞一个残缺的列表 —— 这类错误在分布式环境里
    非常难查,必须在源头拦住。
    """
    if num_returns == 1:
        return [value]
    if isinstance(value, (list, tuple)):
        if len(value) != num_returns:
            raise MiniRayError(
                f"函数声明 num_returns={num_returns},但返回了 {len(value)} 个值"
            )
        return list(value)
    raise MiniRayError(
        f"函数声明 num_returns={num_returns} 时必须返回等长的 tuple/list,"
        f"实际返回了 {type(value).__name__}"
    )


def error_payload(exc: BaseException, function_name: str = "") -> Dict[str, Any]:
    """把异常变成可跨进程传输的 ``{type, message, traceback}``。

    **不传异常对象本身**,因为异常对象经常不可序列化(里面可能有锁、文件句柄、
    本地 C 扩展对象)。只在 raylet 侧重建一个等价的 :class:`RayTaskError`。
    """
    import traceback as _tb

    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)),
        "function": function_name,
    }


def error_from_payload(payload: Dict[str, Any], *, actor: bool = False) -> RayTaskError:
    """从 ``{type, message, traceback}`` 还原 :class:`RayTaskError`。

    尽量还原原始异常类型(``ValueError`` 还是 ``ValueError``),这样
    ``except ValueError`` 在调用方仍然能工作(用 ``as_instanceof_cause()``)。

    :param actor: ``True`` 时返回 :class:`RayActorError` —— actor 方法抛异常在
        Ray 里是 ``RayActorError``(它的父类才是 ``RayTaskError``),
        因为「actor 挂了」和「任务失败了」对调用方意味着不同的事:
        前者可能还影响 actor 的状态。
    """
    from .errors import RayActorError

    cause_type = payload.get("type", "Exception")
    # 注意用 import builtins 而不是 __builtins__:在被 import 的模块里,
    # __builtins__ 是一个 **dict**(只有在 __main__ 里才是 module),
    # getattr(dict, "ValueError") 拿到的是 None —— 于是异常类型全退化成 Exception。
    cause_cls = getattr(builtins, cause_type, None)
    cause: Optional[BaseException] = None
    if isinstance(cause_cls, type):
        try:
            cause = cause_cls(payload.get("message", ""))
        except Exception:  # pragma: no cover - 有些异常的构造函数不接受单参数
            cause = None
    if cause is None:
        cause = Exception(payload.get("message", ""))
    error_cls = RayActorError if actor else RayTaskError
    return error_cls(
        function_name=payload.get("function") or "<task>",
        cause=cause,
        traceback_str=payload.get("traceback", ""),
    )


@contextlib.contextmanager
def runtime_env_context(payload: Optional[Dict[str, Any]], gpu_ids: Optional[Iterable[int]] = None):
    """临时应用 runtime_env(环境变量 + 可见 GPU),退出时还原。

    ``runtime_env`` 是 Ray 的「依赖注入」机制:文件、包、环境变量都可以按
    task/actor 级别指定。mini-ray 实现了最常用也最安全的一类 —— **环境变量**
    (以及 GPU 可见性);``pip`` / ``working_dir`` 见 ``runtime_env.py`` 的说明。

    .. code-block:: python

        f.options(runtime_env={"env_vars": {"MY_VAR": "1"}}).remote()
    """
    payload = payload or {}
    saved: Dict[str, Optional[str]] = {}
    env_vars = dict(payload.get("env_vars") or {})

    gpu_ids = list(gpu_ids or [])
    if gpu_ids:
        env_vars["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in gpu_ids)

    try:
        for key, value in env_vars.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = str(value)
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
