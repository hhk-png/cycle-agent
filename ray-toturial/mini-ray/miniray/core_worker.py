"""CoreWorker —— 每个进程里的「Ray 客户端」。

driver 进程、每个 worker 进程,都各有一个 CoreWorker。它是上层 API
(``f.remote()`` / ``ray.get`` / ``ray.put``)与底层 raylet/GCS 之间的那层。

.. code-block:: text

    ┌─────────────────────── 用户代码 ───────────────────────┐
    │  f.remote(x)   ray.get(refs)   ray.put(v)   actor.m()  │
    └───────────────────────────┬───────────────────────────┘
                                │
                         ┌──────▼──────┐
                         │  CoreWorker │  ← 本文件
                         └──┬───────┬──┘
              任务/对象/等待 │       │ 函数表/actor 表/对象目录
                     ┌──────▼──┐ ┌──▼─────┐
                     │  raylet │ │  GCS   │
                     └─────────┘ └────────┘

CoreWorker 负责四件事:

1. **提交任务**:把参数序列化(内联 ObjectRef)、导出函数、发 RPC,拿回 ObjectRef;
2. **取值**:``ray.get`` 的等待 + 拉取(顺便把 numpy 数组变成零拷贝视图并 pin);
3. **引用计数**:本地记数 + 后台线程批量上报给 raylet(在 ``__del__`` 里发 RPC 是
   自找麻烦,所以是「攒着批量发」);
4. **actor 调用**:把方法调用变成发往 actor mailbox 的消息。
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import GetTimeoutError, MiniRayError, RayTaskError, RaySystemError
from .function_manager import FunctionDescriptor, FunctionManager
from .gcs import GcsClient
from .ids import ActorID, JobID, NodeID, ObjectID, TaskID, WorkerID
from .object_ref import ObjectRef, ObjectRefGenerator
from .raylet_client import RayletClient
from . import serialization

__all__ = ["CoreWorker", "ReferenceCounter"]

logger = logging.getLogger(__name__)

#: 引用计数上报间隔(秒)。Ray 也是批量上报的,否则每个 ObjectRef 一次 RPC 太贵。
_REF_FLUSH_INTERVAL = 0.05


class ReferenceCounter:
    """本进程持有的对象引用计数(delta 批量上报)。"""

    def __init__(self, flush: Callable[[Dict[str, int]], None]) -> None:
        self._lock = threading.Lock()
        self._deltas: Dict[str, int] = {}
        self._flush = flush
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._warned = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="miniray-ref-flush", daemon=True)
        self._thread.start()

    @property
    def stopped(self) -> bool:
        """已经停了吗?(收尾阶段的失败可以忽略,运行期的不能)"""
        return self._stop.is_set()

    def _loop(self) -> None:
        while not self._stop.wait(_REF_FLUSH_INTERVAL):
            self.flush()

    def add(self, object_id_hex: str, count: int = 1) -> None:
        if self._stop.is_set():
            return
        with self._lock:
            self._deltas[object_id_hex] = self._deltas.get(object_id_hex, 0) + count

    def remove(self, object_id_hex: str, count: int = 1) -> None:
        self.add(object_id_hex, -count)

    def flush(self) -> None:
        with self._lock:
            if not self._deltas:
                return
            deltas, self._deltas = self._deltas, {}
        try:
            self._flush(deltas)
        except Exception as exc:
            # raylet 已经退出(比如 shutdown 之后的收尾 GC)时,失败是正常的。
            # 但**其它**异常必须说出来 —— 这里以前是一个无声的 ``except: pass``,
            # 于是「raylet 调了一个对象存储上根本不存在的方法」这种错误
            # 瞒过了整整 148 个测试:每次上报都抛 AttributeError,每次都被吞掉。
            # 静默失败是教学实现里最坏的一种失败(第 22 章「明确不做」的边界)。
            if self._stop.is_set():
                logger.debug("引用计数上报失败(收尾阶段,忽略): %r", exc)
            elif not self._warned:
                self._warned = True
                logger.warning(
                    "引用计数上报失败,后续相同错误只记 debug: %r", exc, exc_info=True
                )
            else:
                logger.debug("引用计数上报仍失败: %r", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.flush()

    def pending(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._deltas)


class CoreWorker:
    """一个进程的 Ray 客户端。"""

    def __init__(
        self,
        *,
        raylet_address: str,
        gcs_address: str,
        worker_id: str,
        node_id: str,
        job_id: str,
        is_driver: bool = False,
        local_mode: bool = False,
        namespace: str = "default",
    ) -> None:
        self.worker_id = worker_id
        self.node_id = node_id
        self.job_id = job_id
        self.is_driver = is_driver
        self.local_mode = local_mode
        self.namespace = namespace
        self._raylet = RayletClient(raylet_address)
        self._gcs = GcsClient(gcs_address)
        self.functions = FunctionManager(self._gcs)
        self.references = ReferenceCounter(self._report_ref_counts)
        self.references.start()
        self._closed = False
        #: 生成器 task:generator_id → ObjectRefGenerator
        self._generators: Dict[str, ObjectRefGenerator] = {}
        #: 本进程打开过的共享内存段(必须在进程存活期间一直持有)
        self._shm_views = 0

    # ================================================================= 任务
    def submit_task(
        self,
        func: Callable,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        *,
        num_returns: int = 1,
        num_cpus: Optional[float] = None,
        num_gpus: Optional[float] = None,
        memory: Optional[float] = None,
        resources: Optional[Dict[str, float]] = None,
        max_retries: int = 3,
        name: Optional[str] = None,
        runtime_env: Optional[Dict[str, Any]] = None,
        scheduling_strategy: Any = None,
        is_generator: bool = False,
        force_local: bool = False,
    ) -> Any:
        """提交一个 task。返回 ObjectRef(或 ObjectRefGenerator)。"""
        descriptor = self.functions.export(func)
        # 参数里如果混着 kwargs,统一塞进 args 的最后一个位置(Ray 也是这么做的)
        payload_args = list(args)
        if kwargs:
            payload_args.append(_Kwargs(kwargs))
        args_bytes = serialization.dumps(tuple(payload_args), inline_object_refs=True)

        spec = {
            "function": descriptor.to_dict(),
            "args": args_bytes,
            "num_returns": int(num_returns),
            "num_cpus": num_cpus,
            "num_gpus": num_gpus,
            "memory": memory,
            "resources": dict(resources or {}),
            "max_retries": int(max_retries),
            "name": name or f"{descriptor.qualname}",
            "runtime_env": dict(runtime_env or {}),
            "scheduling_strategy": _encode_scheduling_strategy(scheduling_strategy),
            "owner": self.worker_id,
            "is_generator": bool(is_generator),
            "force_local": bool(force_local),
        }
        reply = self._raylet.submit_task(spec)
        if reply.get("generator_id"):
            generator = ObjectRefGenerator(
                generator_id=reply["generator_id"],
                owner_address=self._raylet.address,
            )
            self._generators[reply["generator_id"]] = generator
            return generator
        return [
            ObjectRef(ObjectID(bytes.fromhex(oid)), self._raylet.address)
            for oid in reply["result_ids"]
        ]

    def call_actor_method(
        self,
        actor_id: ActorID,
        method_name: str,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        *,
        num_returns: int = 1,
        runtime_env: Optional[Dict[str, Any]] = None,
    ) -> List[ObjectRef]:
        """调用 actor 方法:异步投递,立刻返回 ObjectRef。"""
        payload_args = list(args)
        if kwargs:
            payload_args.append(_Kwargs(kwargs))
        spec = {
            "actor_id": actor_id.hex(),
            "method_name": method_name,
            "args": serialization.dumps(tuple(payload_args), inline_object_refs=True),
            "num_returns": int(num_returns),
            "owner": self.worker_id,
            "runtime_env": dict(runtime_env or {}),
        }
        reply = self._raylet.submit_actor_task(spec)
        return [
            ObjectRef(ObjectID(bytes.fromhex(oid)), self._raylet.address)
            for oid in reply["result_ids"]
        ]

    def create_actor(
        self,
        actor_class: type,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        *,
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        """创建 actor:把类导出、发创建请求,返回 raylet 的回复。"""
        descriptor = self.functions.export(actor_class)
        payload_args = list(args)
        if kwargs:
            payload_args.append(_Kwargs(kwargs))
        spec = {
            "function": descriptor.to_dict(),
            "class_name": getattr(actor_class, "__name__", "Actor"),
            "args": serialization.dumps(tuple(payload_args), inline_object_refs=True),
            "owner": self.worker_id,
            # 注意 options 是**嵌套**的:raylet 从 spec["options"] 读
            # (曾经把它平铺在顶层,结果 num_cpus / max_concurrency / name
            #  这些选项全被静默丢掉了 —— 这种「字段位置不一致」的 bug 很隐蔽)
            "options": dict(options),
        }
        return self._raylet.create_actor(spec)

    def kill_actor(self, actor_id: ActorID, *, no_restart: bool = True) -> None:
        self._raylet.kill_actor(actor_id.hex(), no_restart=no_restart)

    def get_actor(self, name: str, namespace: Optional[str] = None) -> Optional[Dict[str, Any]]:
        actor_id = self._gcs.get_named_actor(name, namespace or self.namespace)
        if actor_id is None:
            return None
        return self._gcs.get_actor(actor_id)

    def list_actors(self) -> List[Dict[str, Any]]:
        return self._gcs.list_actors()

    # ================================================================= 对象
    def put(self, value: Any) -> ObjectRef:
        """``ray.put``:把值写进对象存储,返回 ObjectRef。"""
        object_id = ObjectID.from_random()
        self._write_value(object_id.hex(), value, register=True)
        return ObjectRef(object_id, self._raylet.address)

    def get(self, refs: Any, timeout: Optional[float] = None) -> Any:
        from . import _get_impl

        return _get_impl(self, refs, timeout=timeout)

    def wait(
        self,
        refs: Sequence[ObjectRef],
        *,
        num_returns: int = 1,
        timeout: Optional[float] = None,
        fetch_local: bool = True,
    ) -> Tuple[List[ObjectRef], List[ObjectRef]]:
        if not refs:
            return [], []
        if num_returns > len(refs) and num_returns >= 0:
            raise ValueError("num_returns 不能大于 refs 的数量")
        oids = [ref.hex() for ref in refs]
        reply = self._raylet.wait_for_objects(
            self.worker_id, oids, num_returns=num_returns, timeout=timeout
        )
        ready_set = set(reply["ready"])
        ready = [ref for ref in refs if ref.hex() in ready_set]
        remaining = [ref for ref in refs if ref.hex() not in ready_set]
        if fetch_local and ready:
            # 提前把数据拉到手(和 Ray 的 fetch_local 语义一致),顺便触发 pin
            self._fetch_values([ref.hex() for ref in ready])
        return ready, remaining

    def fetch_values(self, refs: Sequence[ObjectRef]) -> List[Any]:
        return self._fetch_values([ref.hex() for ref in refs])

    def _fetch_values(self, oids: Sequence[str]) -> List[Any]:
        payloads = self._raylet.fetch_objects(self.worker_id, list(oids))
        values = []
        for oid in oids:
            payload = payloads.get(oid)
            if payload is None:
                raise RaySystemError(
                    f"raylet 没有返回对象 {oid[:8]} 的数据(可能已被驱逐或丢失)"
                )
            values.append(self._decode_payload(oid, payload))
        return values

    def pin_object(self, object_id_hex: str) -> None:
        """把对象钉住(有零拷贝视图时)。"""
        self._raylet.pin_object(self.worker_id, object_id_hex)

    def unpin_object(self, object_id_hex: str) -> None:
        self._raylet.unpin_object(self.worker_id, object_id_hex)

    # ---------------------------------------------------------- 值的编解码
    def _write_value(self, object_id_hex: str, value: Any, *, register: bool) -> Dict[str, Any]:
        """把 Python 值写进对象存储,返回给 raylet 的负载描述。

        两条路径:

        * **共享内存路径**(大 numpy 数组 / 大 blob):先向 raylet 申请一块共享内存,
          直接把数据写进去再登记。数据从此只存在一份,同节点所有消费者零拷贝读。
          返回 ``{"kind": "block"}`` —— 对象此刻已经登记好了。
        * **普通路径**(小对象):pickle 成 bytes,``register=True`` 时立刻发
          ``put_object``(``ray.put`` 用),``register=False`` 时把负载交给调用方
          (worker 会把它塞进 ``task_done`` 一次发回,省一次 RPC)。
        """
        import numpy as np

        from .object_store import _SHM_THRESHOLD, _open_shm

        if (
            isinstance(value, np.ndarray)
            and value.dtype.hasobject is False
            and value.flags.c_contiguous
            and value.nbytes >= _SHM_THRESHOLD
        ):
            block = self._raylet.allocate_object(self.worker_id, int(value.nbytes))
            shm = _open_shm(block["shm"])
            dest = np.ndarray(value.shape, value.dtype, buffer=shm.buf, offset=int(block["offset"]))
            dest[:] = value
            self._raylet.commit_object(
                self.worker_id,
                object_id_hex,
                block,
                kind="ndarray",
                dtype=value.dtype.str,
                shape=list(value.shape),
            )
            return {"kind": "block"}

        data = serialization.dumps(value, inline_object_refs=False)
        if len(data) >= 64 * 1024:
            block = self._raylet.allocate_object(self.worker_id, len(data))
            shm = _open_shm(block["shm"])
            shm.buf[int(block["offset"]) : int(block["offset"]) + len(data)] = data
            self._raylet.commit_object(
                self.worker_id, object_id_hex, block, kind="bytes_in_block"
            )
            return {"kind": "block"}

        if register:
            self._raylet.put_object(
                self.worker_id, object_id_hex, {"kind": "bytes", "data": data}
            )
        return {"kind": "bytes", "data": data}

    # ---------------------------------------------------- worker 侧协议
    # 下面这些方法只由 worker 进程调用(见 worker.py)。
    def request_work(self) -> Dict[str, Any]:
        return self._raylet.request_work(self.worker_id)

    def report_task_done(
        self,
        task_id: str,
        results: List[Dict[str, Any]],
        *,
        actor_id: Optional[str] = None,
        generator_done: bool = False,
        num_produced: int = 0,
    ) -> None:
        if generator_done:
            self._raylet.task_done(task_id, self.worker_id, [], actor_id=actor_id)
            return
        self._raylet.task_done(task_id, self.worker_id, results, actor_id=actor_id)

    def report_task_failed(
        self, task_id: str, error: Dict[str, Any], *, actor_id: Optional[str] = None, crashed: bool = False
    ) -> None:
        self._raylet.task_failed(task_id, self.worker_id, error, actor_id=actor_id, crashed=crashed)

    def report_task_chunk(
        self, task_id: str, generator_id: str, payload: Dict[str, Any], *, is_last: bool = False
    ) -> None:
        self._raylet.call(
            "task_chunk", task_id, generator_id, payload, is_last=is_last
        )

    def encode_result(self, object_id_hex: str, value: Any) -> Dict[str, Any]:
        """任务结果 → 负载(不注册,RPC 由 task_done 统一带回)。"""
        return self._write_value(object_id_hex, value, register=False)

    def encode_chunk(self, value: Any) -> Dict[str, Any]:
        """生成器 chunk → 负载(对象 ID 由 raylet 现场分配)。"""
        return {"kind": "bytes", "data": serialization.dumps(value, inline_object_refs=False)}

    def fetch_dep_values(self, dep_ids: Sequence[str]) -> Dict[bytes, Any]:
        """取一组依赖对象,返回 ``{对象 ID 的二进制: 值}``(供内联反序列化)。

        依赖对象如果是「上游 task 失败后写进去的异常对象」,这里会**抛出来** ——
        于是当前 task 也失败,错误沿着依赖链继续往下传(Ray 的语义)。
        """
        if not dep_ids:
            return {}
        payloads = self._raylet.fetch_objects(self.worker_id, list(dep_ids))
        values: Dict[bytes, Any] = {}
        for object_id in dep_ids:
            payload = payloads.get(object_id)
            if payload is None:
                raise MiniRayError(f"依赖对象 {object_id[:8]} 无法获取")
            value = self._decode_payload(object_id, payload)
            # 判据是「**值本身是个异常对象**」,而不是「它是不是 RayTaskError」。
            # 之前只挡 RayTaskError,于是 TaskCancelledError / ObjectLostError
            # (它们继承 MiniRayError,不是 RayTaskError)会**当普通参数**灌进
            # 下游用户函数 —— 用户看到的是一个莫名其妙的 TypeError,
            # 真正的原因(上游被取消 / 对象丢了)被彻底掩盖。
            # (ActorDiedError 恰好继承 RayTaskError,所以以前没暴露。)
            if isinstance(value, BaseException):
                raise value
            values[bytes.fromhex(object_id)] = value
        return values

    def report_actor_creation(
        self, actor_id: str, *, ok: bool, error: Optional[Dict[str, Any]] = None
    ) -> None:
        self._raylet.call("report_actor_creation", actor_id, ok=ok, error=error)

    def register_actor_methods(self, actor_id: str, methods: Dict[str, Any]) -> None:
        self._raylet.register_actor_methods(actor_id, methods)

    def actor_poll(self, actor_id: str, worker_id: str, group: str = "") -> Dict[str, Any]:
        return self._raylet.actor_poll(actor_id, worker_id, group)

    def write_object(self, object_id_hex: Optional[str], value: Any, *, transient: bool = False) -> Dict[str, Any]:
        """worker 侧写对象(生成器 chunk 用 transient 模式)。"""
        if transient or object_id_hex is None:
            return self.encode_chunk(value)
        return self.encode_result(object_id_hex, value)

    def _decode_payload(self, object_id_hex: str, payload: Dict[str, Any]) -> Any:
        """把 raylet 返回的负载变成 Python 对象。

        * ``ndarray``:建**只读零拷贝视图**,并 pin 住底层共享内存 ——
          pin 的释放挂在数组上(``weakref.finalize``),不是挂在 ObjectRef 上,
          因为用户可能 ``arr = ray.get(ref)`` 之后把 ref 丢掉继续用 arr。
        * ``bytes`` / ``bytes_in_block``:反序列化。
        """
        from .object_store import _open_shm, create_ndarray_view

        kind = payload.get("kind")
        if kind == "bytes":
            return serialization.loads(payload["data"])

        if kind == "bytes_in_block":
            shm = _open_shm(payload["shm"])
            start, size = int(payload["offset"]), int(payload["nbytes"])
            return serialization.loads(bytes(shm.buf[start : start + size]))

        if kind == "ndarray":
            descriptor = {
                "kind": "ndarray",
                "shm": payload["shm"],
                "offset": payload["offset"],
                "nbytes": payload["nbytes"],
                "dtype": payload["dtype"],
                "shape": payload["shape"],
                "readonly": payload.get("readonly", True),
            }
            array = create_ndarray_view(descriptor)
            self.pin_object(object_id_hex)
            self._shm_views += 1
            # 数组被 GC 时自动 unpin(避免在 __del__ 里做 RPC)
            counter = self.references

            def _release(oid: str = object_id_hex) -> None:
                counter.add(f"__unpin__:{oid}", 1)

            weakref.finalize(array, _release)
            return array

        if kind == "ndarray_raw":
            import numpy as np

            array = np.frombuffer(payload["data"], dtype=np.dtype(payload["dtype"]))
            return array.reshape(payload["shape"])

        raise RaySystemError(f"未知的对象负载类型: {kind}")

    # ------------------------------------------------------- 生成器 task
    def get_generator_state(self, generator_id: str) -> Dict[str, Any]:
        return self._raylet.get_generator_state(generator_id)

    def wait_for_generator(self, generator_id: str, index: int, timeout: float = 60.0) -> None:
        self._raylet.wait_for_generator(self.worker_id, generator_id, index, timeout)

    # ================================================================= 内部
    def _report_ref_counts(self, deltas: Dict[str, int]) -> None:
        """把引用计数增量报给 raylet;``__unpin__:`` 前缀是「释放 pin」。"""
        unpins: List[str] = []
        counts: Dict[str, int] = {}
        for key, value in deltas.items():
            if key.startswith("__unpin__:"):
                unpins.extend([key.split(":", 1)[1]] * value)
            else:
                counts[key] = counts.get(key, 0) + value
        # 两件事必须**各自**尽力而为:上报计数失败不能让「释放 pin」跟着被跳过 ——
        # pin 漏释放的对象会被永久钉在共享内存里,最终表现为「存储看着是空的
        # 却报 ObjectStoreFullError」。
        failure: Optional[BaseException] = None
        try:
            if counts:
                self._raylet.update_ref_counts(self.worker_id, counts)
        except Exception as exc:
            failure = exc
        for oid in unpins:
            try:
                self._raylet.unpin_object(self.worker_id, oid)
            except Exception:
                pass
        # ★ ``_stop`` 只存在于 :class:`ReferenceCounter` 上,``CoreWorker`` 没有这个
        #   属性。以前这里写的是 ``self._stop.is_set()`` —— 于是「上报真的失败」
        #   这条诊断路径本身是坏的:每一次失败都换成
        #   ``AttributeError("'CoreWorker' object has no attribute '_stop'")``,
        #   **真实的异常被完全顶掉**(第二条起还降级成 debug)。诊断路径出错比
        #   不诊断更糟:它把用户引到一个根本不存在的方向上。
        if failure is not None and not self.references.stopped:
            raise failure

    def add_object_ref(self, ref: ObjectRef) -> None:
        self.references.add(ref.hex(), 1)

    def remove_object_ref(self, ref: ObjectRef) -> None:
        self.references.remove(ref.hex(), 1)

    def get_function(self, descriptor: FunctionDescriptor) -> Callable:
        return self.functions.get(descriptor)

    def flush_refs(self) -> None:
        self.references.flush()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.references.stop()
        try:
            self._raylet.disconnect_worker(self.worker_id)
        except Exception:
            pass
        self._raylet.close()
        self._gcs.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试
        role = "driver" if self.is_driver else "worker"
        return f"CoreWorker({role}, id={self.worker_id[:8]}, node={self.node_id[:8]})"


class _Kwargs:
    """占位包装:让 ``f.remote(a, k=v)`` 的 kwargs 也能走同一条序列化通道。

    反序列化端由 :func:`miniray._private.signature.unpack_args` 拆开。
    """

    __slots__ = ("kwargs",)

    def __init__(self, kwargs: Dict[str, Any]) -> None:
        self.kwargs = kwargs


def _encode_scheduling_strategy(strategy: Any) -> Optional[Dict[str, Any]]:
    """把 ``NodeAffinitySchedulingStrategy`` / ``PlacementGroupSchedulingStrategy``
    变成可传输的 dict。"""
    if strategy is None:
        return None
    encoder = getattr(strategy, "_encode", None)
    if encoder is None:
        raise MiniRayError(f"不认识的调度策略: {strategy!r}")
    return encoder()
