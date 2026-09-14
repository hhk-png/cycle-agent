"""Worker 进程 —— 真正执行用户代码的地方。

``worker_main`` 是 spawn 出来的子进程入口(必须是模块级函数,参数必须可 pickle)。
它有两种形态:

.. code-block:: text

   ┌─ task worker ────────────────────────────────────────────────┐
   │  while True:                                                 │
   │      task = raylet.request_work()   # 长轮询,最多 1 秒        │
   │      fetch deps → 反序列化 → 调函数 → 结果写回对象存储        │
   └──────────────────────────────────────────────────────────────┘
   ┌─ actor worker ───────────────────────────────────────────────┐
   │  1. 执行 __init__ 构造实例(实例常驻内存)                     │
   │  2. 每个并发组开 N 个线程,各自长轮询 actor_poll              │
   │     (async actor 则用一个 asyncio 事件循环 + 最多 1000 并发)  │
   └──────────────────────────────────────────────────────────────┘

**为什么 actor 用线程/协程而不是进程?** 因为 actor 的语义是「有状态的实例」——
状态在内存里,所以固定在一个进程里执行;并发度靠线程(sync actor)或事件循环
(async actor)提供。这也是 Ray 的 actor 模型:进程隔离 + 组内并发。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from . import serialization
from .core_worker import CoreWorker
from .errors import MiniRayError, RayActorError
from .execution import (
    error_payload,
    normalize_results,
    runtime_env_context,
    unpack_args,
)
from .function_manager import FunctionDescriptor
from .ids import ActorID

__all__ = ["worker_main"]

logger = logging.getLogger("miniray.worker")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def worker_main(config: Dict[str, Any]) -> None:
    """worker 进程入口。

    .. warning::
       这里**不能**做任何依赖父进程内存状态的事 —— spawn 出来的子进程是全新的
       解释器,只继承了命令行、环境变量与 sys.path。
    """
    _setup_logging(config)
    worker_id = config["worker_id"]
    core = CoreWorker(
        raylet_address=config["raylet_address"],
        gcs_address=config["gcs_address"],
        worker_id=worker_id,
        node_id=config["node_id"],
        job_id=config["job_id"],
        is_driver=False,
        namespace=config.get("namespace", "default"),
    )
    # 登记到进程全局:这样 ObjectRef 的引用计数(以及它依赖的 runtime)在 worker 里也能工作
    from . import runtime as _runtime

    _runtime.set_core_worker(core, is_driver=False)
    if config.get("actor_id"):
        os.environ["MINIRAY_ACTOR_ID"] = config["actor_id"]
    exit_code = 0
    try:
        core._raylet.register_worker(
            worker_id=worker_id,
            node_id=config["node_id"],
            pid=os.getpid(),
            worker_type=config.get("worker_type", "task"),
            actor_id=config.get("actor_id"),
        )
        if config.get("is_actor_worker"):
            _run_actor_worker(config, core)
        else:
            _run_task_worker(config, core)
    except KeyboardInterrupt:  # pragma: no cover - 开发时手动打断
        pass
    except Exception:
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            core.shutdown()
        except Exception:  # pragma: no cover
            pass
        # 用 os._exit 而不是正常返回:避免后台线程/GC 在退出阶段卡住主进程
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)


def _setup_logging(config: Dict[str, Any]) -> None:
    level = config.get("logging_level")
    logging.basicConfig(
        level=level if isinstance(level, int) else logging.WARNING,
        format=f"[miniray worker pid={os.getpid()}] %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# task worker
# ---------------------------------------------------------------------------


def _run_task_worker(config: Dict[str, Any], core: CoreWorker) -> None:
    worker_id = config["worker_id"]
    while True:
        try:
            message = core.request_work()
        except Exception as exc:  # raylet 没了 → 退出
            logger.warning("与 raylet 的连接断了: %r", exc)
            return
        kind = message.get("kind")
        if kind == "shutdown":
            return
        if kind == "noop":
            continue
        if kind == "task":
            _execute_task(core, message["task"])


def _execute_task(core: CoreWorker, task: Dict[str, Any]) -> None:
    """执行一个 task:取依赖 → 反序列化 → 调用 → 写回结果。"""
    task_id = task["task_id"]
    started = time.time()
    try:
        descriptor = FunctionDescriptor.from_dict(task["function"])
        func = core.get_function(descriptor)
        dep_values = core.fetch_dep_values(task.get("deps") or [])
        args = serialization.loads(task["args"], ref_values=dep_values)
        args, kwargs = unpack_args(args)
        num_returns = int(task.get("num_returns", 1))

        with runtime_env_context(task.get("runtime_env"), task.get("gpu_ids")):
            if task.get("is_generator"):
                _run_generator(core, task, func, args, kwargs)
                return
            result = func(*args, **kwargs)

        results = normalize_results(result, num_returns)
        payloads = [
            core.write_object(object_id, value)
            for object_id, value in zip(task["result_ids"], results)
        ]
        core.report_task_done(task_id, payloads)
        logger.debug(
            "task %s(%s) 执行完成,耗时 %.3fs",
            task.get("name"),
            task_id[:8],
            time.time() - started,
        )
    except Exception as exc:
        core.report_task_failed(
            task_id, error_payload(exc, task.get("name", "")), crashed=False
        )


def _run_generator(
    core: CoreWorker, task: Dict[str, Any], func, args: tuple, kwargs: Dict[str, Any]
) -> None:
    """流式生成器 task:每 ``yield`` 一个值就产出一个对象,下游可以边产边消费。"""
    generator_id = task["generator_id"]
    task_id = task["task_id"]
    produced = 0
    try:
        for value in func(*args, **kwargs):
            payload = core.write_object(None, value, transient=True)
            core.report_task_chunk(task_id, generator_id, payload, is_last=False)
            produced += 1
    except Exception as exc:
        core.report_task_failed(
            task_id, error_payload(exc, task.get("name", "")), crashed=False
        )
        return
    # 正常结束:告诉 raylet 生成器已经结束(第 0 个 chunk 都没产出也算)
    core.report_task_done(task_id, [], generator_done=True, num_produced=produced)


# ---------------------------------------------------------------------------
# actor worker
# ---------------------------------------------------------------------------


def _run_actor_worker(config: Dict[str, Any], core: CoreWorker) -> None:
    """actor 进程入口:先把 actor 级 ``runtime_env`` / GPU 可见性装上。

    ``CUDA_VISIBLE_DEVICES`` 必须**在 actor 进程里**设好 —— 那才是
    ``ray.get_gpu_ids()`` 读的地方。此前 actor 的这条链是断的:
    资源在 raylet 侧被正确扣掉了,但 actor 自己看不到被分到哪几张卡,
    于是 ``get_gpu_ids()`` 返回 ``[]``、actor 里 ``torch.cuda`` 能看见全部 GPU。
    """
    with runtime_env_context(config.get("runtime_env"), config.get("gpu_ids")):
        _run_actor_worker_inner(config, core)


def _run_actor_worker_inner(config: Dict[str, Any], core: CoreWorker) -> None:
    """actor 进程:构造实例,然后按并发模型开执行上下文。"""
    spec = config["actor_spec"]
    actor_id = config["actor_id"]
    class_name = spec.get("class_name", "Actor")
    try:
        descriptor = FunctionDescriptor.from_dict(spec["function"])
        actor_class = core.get_function(descriptor)
    except Exception as exc:
        core.report_actor_creation(actor_id, ok=False, error=error_payload(exc, class_name))
        return

    # ---- 1) 跑 __init__ ----
    creation_error: Optional[BaseException] = None
    instance: Any = None
    try:
        deps = spec.get("creation_deps") or []
        dep_values = core.fetch_dep_values(deps) if deps else {}
        args = serialization.loads(spec["args"], ref_values=dep_values)
        args, kwargs = unpack_args(args)
        instance = actor_class(*args, **kwargs)
    except Exception as exc:
        creation_error = exc
        core.report_actor_creation(actor_id, ok=False, error=error_payload(exc, class_name))
    else:
        core.report_actor_creation(actor_id, ok=True)

    # ---- 2) 注册方法元数据(并发组等)----
    method_metadata = dict(spec.get("method_metadata") or {})
    try:
        core.register_actor_methods(actor_id, method_metadata)
    except Exception:  # pragma: no cover - raylet 已退出
        return

    if spec.get("is_async"):
        executor = _AsyncActorContext(
            config=config,
            core=core,
            instance=instance,
            creation_error=creation_error,
            method_metadata=method_metadata,
            max_concurrency=int(spec.get("max_concurrency", 1000)),
        )
        executor.run()
        return

    concurrency = {"": max(1, int(spec.get("max_concurrency", 1)))}
    for group, value in (spec.get("concurrency_groups") or {}).items():
        concurrency[group] = max(1, int(value))

    threads: List[threading.Thread] = []
    stop_event = threading.Event()
    for group, slots in concurrency.items():
        for index in range(slots):
            context = _SyncActorContext(
                config=config,
                group=group,
                index=index,
                instance=instance,
                creation_error=creation_error,
                method_metadata=method_metadata,
                stop_event=stop_event,
            )
            thread = threading.Thread(
                target=context.run,
                name=f"miniray-actor-{group or 'default'}-{index}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
    for thread in threads:
        thread.join()


class _ActorMethodExecutor:
    """actor 方法执行的公共部分(同步/异步共用)。"""

    def __init__(
        self,
        *,
        instance: Any,
        creation_error: Optional[BaseException],
        method_metadata: Dict[str, Dict[str, Any]],
        class_name: str,
    ) -> None:
        self.instance = instance
        self.creation_error = creation_error
        self.method_metadata = method_metadata
        self.class_name = class_name

    def prepare(self, task: Dict[str, Any], core: CoreWorker):
        """取依赖 + 反序列化参数 + 取出方法。"""
        if self.creation_error is not None:
            raise RayActorError(
                self.class_name, cause=self.creation_error, traceback_str=""
            )
        deps = _deps_of(task["args"])
        dep_values = core.fetch_dep_values(deps) if deps else {}
        args = serialization.loads(task["args"], ref_values=dep_values)
        args, kwargs = unpack_args(args)
        method = getattr(self.instance, task["method_name"], None)
        if method is None:
            raise AttributeError(
                f"{self.class_name} 没有方法 {task['method_name']!r}"
            )
        return method, args, kwargs

    def num_returns(self, task: Dict[str, Any]) -> int:
        """这个调用要产出几个结果对象。

        **唯一权威是 raylet 决定的 ``task["num_returns"]``** —— 它在提交时按
        调用方的 ``options(num_returns=…)`` 就分配好了结果对象。以前这里优先用
        **方法元数据**(``@ray.method(num_returns=…)`` 或默认的 1),于是
        ``handle.method.options(num_returns=2).remote()`` 在两边对不上:
        driver 侧 raylet 分配了 2 个结果对象,actor 侧只产出 1 个,
        而 ``finish`` 里的 ``zip`` 把多余的那个**静默截断** —— 第二个 ref
        永远停在 PENDING,``ray.get(refs)`` 永久挂起,没有报错也没有诊断。
        对照:task 路径遇到数量不符会**显式抛错**(``execution.normalize_results``)。
        """
        return int(task.get("num_returns", 1) or 1)

    def finish(self, core: CoreWorker, task: Dict[str, Any], result: Any) -> None:
        num_returns = self.num_returns(task)
        object_ids = list(task.get("result_ids") or [])
        if len(object_ids) != num_returns:
            # 和 task 路径一样**显式报错**,而不是静默截断/补齐 ——
            # 静默补 ``None`` 尤其糟:``write_object(None, value)`` 造出的 payload
            # 根本没有任何对象 ID 会接收它,结果就是一个永远 PENDING 的 ref。
            # 这里抛出去,由 _SyncActorContext.run / _AsyncActorContext._handle
            # 兜住,走 ``report_task_failed`` 把异常写进**每一个**结果对象。
            raise MiniRayError(
                f"actor 方法 {task.get('method_name')!r} 声明 num_returns={num_returns},"
                f"但 raylet 分配了 {len(object_ids)} 个结果对象 —— 两边必须一致"
            )
        results = normalize_results(result, num_returns)
        payloads = [
            core.write_object(object_id, value) for object_id, value in zip(object_ids, results)
        ]
        core.report_task_done(
            task["task_id"], payloads, actor_id=task["actor_id"]
        )


class _SyncActorContext(_ActorMethodExecutor):
    """同步 actor 的一个执行槽位:一个线程 + 一个专属 CoreWorker。"""

    def __init__(
        self,
        *,
        config: Dict[str, Any],
        group: str,
        index: int,
        instance: Any,
        creation_error: Optional[BaseException],
        method_metadata: Dict[str, Dict[str, Any]],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(
            instance=instance,
            creation_error=creation_error,
            method_metadata=method_metadata,
            class_name=config["actor_spec"].get("class_name", "Actor"),
        )
        self.config = config
        self.group = group
        self.index = index
        self.stop_event = stop_event
        self.actor_id = config["actor_id"]

    def run(self) -> None:
        worker_id = f"{self.config['worker_id']}#{self.group or 'default'}#{self.index}"
        core = CoreWorker(
            raylet_address=self.config["raylet_address"],
            gcs_address=self.config["gcs_address"],
            worker_id=worker_id,
            node_id=self.config["node_id"],
            job_id=self.config["job_id"],
            is_driver=False,
            namespace=self.config.get("namespace", "default"),
        )
        try:
            core._raylet.register_worker(
                worker_id=worker_id,
                node_id=self.config["node_id"],
                pid=os.getpid(),
                worker_type="actor",
                actor_id=self.actor_id,
            )
            while not self.stop_event.is_set():
                try:
                    reply = core.actor_poll(self.actor_id, worker_id, self.group)
                except Exception:
                    return  # raylet 没了
                if reply.get("kind") == "shutdown":
                    return
                if reply.get("kind") != "actor_task":
                    continue
                task = reply["task"]
                try:
                    method, args, kwargs = self.prepare(task, core)
                    with runtime_env_context(task.get("runtime_env")):
                        result = method(*args, **kwargs)
                        if inspect.isawaitable(result):
                            # 同步上下文里拿到协程:用事件循环跑完
                            result = asyncio.run(result)
                    self.finish(core, task, result)
                except Exception as exc:
                    core.report_task_failed(
                        task["task_id"],
                        error_payload(exc, task.get("method_name", "")),
                        actor_id=self.actor_id,
                    )
        finally:
            try:
                core.shutdown()
            except Exception:  # pragma: no cover
                pass


class _AsyncActorContext(_ActorMethodExecutor):
    """async actor:一个事件循环,默认最多 1000 个方法并发(Ray 的默认值)。"""

    def __init__(
        self,
        *,
        config: Dict[str, Any],
        core: CoreWorker,
        instance: Any,
        creation_error: Optional[BaseException],
        method_metadata: Dict[str, Dict[str, Any]],
        max_concurrency: int,
    ) -> None:
        super().__init__(
            instance=instance,
            creation_error=creation_error,
            method_metadata=method_metadata,
            class_name=config["actor_spec"].get("class_name", "Actor"),
        )
        self.config = config
        self.core = core                      # 上报结果用(在事件循环线程里调用)
        self.max_concurrency = max(1, max_concurrency)
        self.actor_id = config["actor_id"]
        self.worker_id = f"{config['worker_id']}#async"
        #: 轮询专用连接(在 executor 线程里用)。
        #:
        #: **为什么必须分开?** 因为 RpcClient 是「一次一个请求」的:
        #: 长轮询会在服务端挂起最多 1 秒,期间整条连接被占住。
        #: 如果上报结果复用同一条连接,而上报又是在**事件循环线程**里同步调用的,
        #: 事件循环就会被阻塞整整一秒 —— 表现为「async actor 的并发没生效」:
        #: 8 个 sleep(0.4) 的协程里有 7 个要等 1 秒才醒(实测)。
        #: 分开两条连接之后,轮询在等,上报随时能走。
        self._poll_worker_id = f"{config['worker_id']}#poll"
        #: 每条并发组一条**独立**的轮询连接。
        #:
        #: 为什么不能共用一条?RpcClient 内部加锁串行化,而长轮询会占住连接
        #: 最多 1 秒 —— 多个组共用一条,就等于把它们的轮询串成了一队。
        self._poll_cores: Dict[str, CoreWorker] = {}
        self._poll_worker_ids: Dict[str, str] = {}
        self._inflight = 0
        #: 需要轮询的并发组。``""`` 是默认组,其余来自 ``concurrency_groups``。
        #:
        #: ⚠️ 少列一个组,那个组的方法调用就会**永远没人取** ——
        #: raylet 按方法声明的组投递,而这里如果只轮询 ``""``,
        #: 投进 ``"io"`` 邮箱的任务就石沉大海,``ray.get`` 永久挂起
        #: (没有报错,因为「没有 worker 来取」和「任务还在排队」长得一样)。
        self._poll_groups = [""] + [
            str(g) for g in (config["actor_spec"].get("concurrency_groups") or {})
        ]

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:  # pragma: no cover
            pass

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        groups = self._poll_groups
        # 每个组一条长轮询线程,外加给「同步方法丢线程池」留余量
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(4, len(groups) + 2), thread_name_prefix="miniray-actor-poll"
        )
        #: 正在飞的长轮询:future → 它属于哪个组
        polls = {
            asyncio.ensure_future(loop.run_in_executor(pool, self._poll, group)): group
            for group in groups
        }
        pending = set()
        try:
            while True:
                if self._inflight >= self.max_concurrency:
                    await asyncio.sleep(0.002)
                    continue
                # 谁先有活谁先返回,不等其它组的长轮询超时 —— 否则某个组
                # 一旦有空转,就会把其它组的延迟拖到整个轮询周期。
                done, _ = await asyncio.wait(
                    set(polls), return_when=asyncio.FIRST_COMPLETED
                )
                for future in done:
                    group = polls.pop(future)
                    try:
                        reply = future.result()
                    except Exception:
                        # 与 raylet 的连接断了:退出循环,由 finally 收尾
                        return
                    kind = reply.get("kind")
                    if kind == "shutdown":
                        return
                    if kind == "actor_task":
                        self._inflight += 1
                        task = asyncio.ensure_future(self._handle(reply["task"]))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                    # 给这个组补上一条新的长轮询
                    new_future = asyncio.ensure_future(
                        loop.run_in_executor(pool, self._poll, group)
                    )
                    polls[new_future] = group
        finally:
            for future in list(polls):
                future.cancel()
            for task in list(pending):
                task.cancel()
            pool.shutdown(wait=False)
            for core in list(self._poll_cores.values()):
                try:
                    core.shutdown()
                except Exception:  # pragma: no cover
                    pass

    def _ensure_poll_core(self, group: str = "") -> CoreWorker:
        """懒创建某条并发组的轮询专用连接(executor 线程里用,所以延迟初始化)。"""
        core = self._poll_cores.get(group)
        if core is None:
            worker_id = self._poll_worker_id if not group else f"{self._poll_worker_id}:{group}"
            core = CoreWorker(
                raylet_address=self.config["raylet_address"],
                gcs_address=self.config["gcs_address"],
                worker_id=worker_id,
                node_id=self.config["node_id"],
                job_id=self.config["job_id"],
                is_driver=False,
                namespace=self.config.get("namespace", "default"),
            )
            core._raylet.register_worker(
                worker_id=worker_id,
                node_id=self.config["node_id"],
                pid=os.getpid(),
                worker_type="actor",
                actor_id=self.actor_id,
            )
            self._poll_cores[group] = core
            self._poll_worker_ids[group] = worker_id
        return core

    def _poll(self, group: str = "") -> Dict[str, Any]:
        return self._ensure_poll_core(group).actor_poll(
            self.actor_id, self._poll_worker_ids.get(group, self._poll_worker_id), group
        )

    async def _handle(self, task: Dict[str, Any]) -> None:
        try:
            method, args, kwargs = self.prepare(task, self.core)
            with runtime_env_context(task.get("runtime_env")):
                if inspect.iscoroutinefunction(method):
                    result = await method(*args, **kwargs)
                else:
                    # 同步方法在 async actor 里会阻塞事件循环 —— 丢到线程池里跑,
                    # 这也是 Ray 对 async actor 的推荐做法
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: method(*args, **kwargs)
                    )
            self.finish(self.core, task, result)
        except Exception as exc:
            self.core.report_task_failed(
                task["task_id"],
                error_payload(exc, task.get("method_name", "")),
                actor_id=self.actor_id,
            )
        finally:
            self._inflight -= 1


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _deps_of(args_bytes: bytes) -> List[str]:
    from .serialization import extract_object_ids

    return [oid.hex() for oid in extract_object_ids(args_bytes)]
