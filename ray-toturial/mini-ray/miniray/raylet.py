"""Raylet —— 每个节点上的「本地调度器 + 对象管理 + worker 池」。

这是 mini-ray 里最大的一个文件,因为它承担了真实 Ray 里 raylet 进程的全部职责。
拆开看是六件事:

.. code-block:: text

   ┌─ Raylet ───────────────────────────────────────────────────────────────┐
   │  1. 调度循环      事件驱动:任务就绪/对象就绪/worker 空闲 → 派活          │
   │  2. 对象管理      对象的生老病死:put/commit/fetch/wait/引用计数/丢失重建 │
   │  3. worker 池     按需拉起进程、复用、空闲退休、崩溃回收                  │
   │  4. actor 生命周期 创建/邮箱/并发组/重启/杀死                            │
   │  5. 血缘账本      记下「哪个 task 产出了哪个对象」→ 对象丢了能重算        │
   │  6. 放置组        资源预留 + 分配(2.56 起 Ray 还加了 GPU domain 感知)   │
   └────────────────────────────────────────────────────────────────────────┘

**为什么调度是事件驱动的?** 因为分布式系统里「等」是最贵的。task 在等依赖、
worker 在等任务、driver 在等结果 —— 如果都靠轮询,延迟和 CPU 都会被浪费。
mini-ray 里所有的「等」都是条件变量:

* task 等依赖 → 调度器里的 ``_waiting[oid]`` 反向索引,对象就绪时被唤醒;
* worker 等任务 → 长轮询挂在 ``WorkerSlot.cond`` 上(最多 1 秒超时,顺便当心跳);
* driver 等结果 → ``ray.get`` 挂在 ``self._cond`` 上,对象就绪时被唤醒。

**参考**:Ray 官方文档 *Internals* 一节里的
"Task Lifecycle / Object Spilling / RPC Fault Tolerance" 三篇,
以及 ``src/ray/raylet/local_object_manager.cc``(对象溢出)与
``src/ray/raylet/scheduling/``(调度)的实现。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .errors import (
    ActorDiedError,
    MiniRayError,
    ObjectLostError,
    ObjectStoreFullError,
    RayActorError,
    RayTaskError,
    RaySystemError,
    TaskCancelledError,
    WorkerCrashedError,
)
from .execution import (
    error_from_payload as _error_from_payload,
    error_payload as _error_payload,
    normalize_results as _normalize_results,
    runtime_env_context,
    unpack_args as _unpack_args,
)
from .function_manager import FunctionDescriptor
from .gcs import GcsClient
from .ids import ActorID, NodeID, ObjectID, TaskID, WorkerID
from .object_store import (
    DEFAULT_OBJECT_STORE_BYTES,
    BlockHandle,
    ObjectStore,
    _open_shm,
)
from .scheduler import (
    NodeResources,
    Resources,
    ScheduleError,
    TaskRecord,
    TaskScheduler,
    TaskState,
)
from . import serialization
from .worker_pool import WorkerPool, WorkerSlot, WorkerState

__all__ = ["Raylet", "RayletConfig"]

logger = logging.getLogger("miniray.raylet")

#: 长轮询的超时(秒)。1 秒是「不及时浪费 CPU」与「退出够快」的折中。
_POLL_TIMEOUT = 1.0

#: 对象历史的保留上限(timeline/state API 用)
_MAX_EVENTS = 20000

#: 调度循环的兜底 tick(reap 死进程、退休空闲 worker、尝试分配放置组)
_TICK = 0.2


@dataclass
class RayletConfig:
    """raylet 的全部可调参数(``ray.init`` 的参数最终落到这里)。"""

    job_id: str
    namespace: str = "default"
    num_cpus: float = 0.0  # 0 表示自动探测
    num_gpus: float = 0.0
    #: 节点堆内存(字节)。0 表示自动探测(和 num_cpus 一个套路)。
    #:
    #: ⚠️ 这个字段以前**不存在**,节点内存被硬编码成 0.0 —— 后果是
    #: 「声明了 ``memory=`` 的任务/actor 永远调度不上去」,而 ``memory``
    #: 确实在 ``ACTOR_OPTION_KEYS`` 里、教程也写着它"参与调度"。
    #: 两处对不上,补齐的是这里。
    memory: float = 0.0
    resources: Dict[str, float] = field(default_factory=dict)
    #: 逻辑节点数。真实 Ray 里节点来自 ``ray start``;这里用来**模拟**多节点集群,
    #: 以便观察调度策略、对象本地性、放置组的行为。
    num_nodes: int = 1
    object_store_memory: int = DEFAULT_OBJECT_STORE_BYTES
    enable_spilling: bool = True
    spill_dir: Optional[str] = None
    max_workers_per_node: int = 0
    worker_idle_timeout_ms: int = 0
    local_mode: bool = False
    session_dir: str = ""

    @staticmethod
    def detect_cpus() -> float:
        """默认按机器的可用核数给 CPU 资源(Ray 也是这么做的)。"""
        try:
            return float(os.cpu_count() or 1)
        except Exception:  # pragma: no cover
            return 1.0

    #: 自动探测内存时取"可用内存的百分比" —— 与 Ray 的
    #: ``RAY_DEFAULT_SYSTEM_RESERVED_MEMORY_PROPORTION``(留 10% 给系统)
    #: 同一个思路,只是这里更保守:只认 70%。
    MEMORY_DETECT_FRACTION = 0.7

    @staticmethod
    def detect_memory() -> float:
        """默认按机器可用内存的 70% 给节点内存(字节)。

        Ray 自己也是这么做的(默认取"可用内存"的一个比例),
        所以这里跟随,不再把节点内存当成 0。
        """
        try:
            import psutil  # 可选依赖

            return float(psutil.virtual_memory().available) * RayletConfig.MEMORY_DETECT_FRACTION
        except Exception:
            pass
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
            return float(total) * RayletConfig.MEMORY_DETECT_FRACTION
        except Exception:  # pragma: no cover - Windows / 受限环境
            # 探测不到就给一个保守的默认值(8 GiB),而不是 0 ——
            # 0 会让任何带 memory= 声明的任务都调度不上去。
            return 8.0 * 1024**3


@dataclass
class _ActorRuntime:
    """raylet 侧维护的 actor 状态。"""

    actor_id: str
    info: Dict[str, Any]
    spec: Dict[str, Any]
    record: TaskRecord  # 占着资源的「伪任务」,用于释放资源
    node_id: str
    #: 按并发组拆分的邮箱
    mailboxes: Dict[str, deque] = field(default_factory=dict)
    inflight: Dict[str, int] = field(default_factory=dict)
    concurrency: Dict[str, int] = field(default_factory=dict)
    method_metadata: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    slot: Optional[WorkerSlot] = None
    #: actor 终身持有的 GPU 序号(由 ``assign`` 填),要传给 actor 进程
    gpu_ids: List[int] = field(default_factory=list)
    #: actor 级 runtime_env(``.options(runtime_env={...})``)
    runtime_env: Optional[Dict[str, Any]] = None
    state: str = "PENDING"
    num_restarts: int = 0
    no_restart: bool = False
    #: 正在跑的 actor task:task_id → (group, 结果对象, 原始 spec)
    running: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: 是否已经就「等资源太久」告警过(只报一次,避免刷屏)
    pending_warned: bool = False
    #: 等待投递的任务数(长轮询唤醒用)
    cond: threading.Condition = field(default_factory=threading.Condition)


class Raylet:
    """一个 raylet(在 mini-ray 里,driver 进程内的一个 RPC 服务)。"""

    def __init__(self, config: RayletConfig, gcs_address: str) -> None:
        self.config = config
        self.gcs = GcsClient(gcs_address)
        self.address = ""  # 由 runtime 在 RPC 服务起来后回填
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._schedule_needed = threading.Event()
        self._stopping = threading.Event()

        # ---- 节点与资源
        num_nodes = max(1, int(config.num_nodes))
        cpus = config.num_cpus or RayletConfig.detect_cpus()
        self.node_ids: List[str] = []
        per_node_cpu = cpus / num_nodes
        per_node_gpu = config.num_gpus / num_nodes
        memory = config.memory or RayletConfig.detect_memory()
        per_node_memory = memory / num_nodes
        self._crash_loop_lock = threading.Lock()
        nodes: Dict[str, NodeResources] = {}
        self._stores: Dict[str, ObjectStore] = {}
        for index in range(num_nodes):
            node_id = NodeID.from_random().hex()
            self.node_ids.append(node_id)
            total = Resources(
                cpu=per_node_cpu,
                gpu=per_node_gpu,
                memory=per_node_memory,
                custom={k: float(v) / num_nodes for k, v in config.resources.items()},
            )
            nodes[node_id] = NodeResources.create(node_id, total)
            self._stores[node_id] = ObjectStore(
                node_id,
                capacity_bytes=max(1, int(config.object_store_memory) // num_nodes),
                spill_dir=config.spill_dir,
                enable_spilling=config.enable_spilling,
            )
        self._scheduler = TaskScheduler(nodes)
        self._driver_node = self.node_ids[0]

        # ---- worker 池
        self._pool = WorkerPool(
            max_workers_per_node=config.max_workers_per_node,
            worker_idle_timeout_ms=config.worker_idle_timeout_ms,
            on_worker_death=self._on_worker_death,
            session_dir=config.session_dir,
        )

        # ---- 对象管理
        #: object_id(hex) → {"state": READY/RECONSTRUCTING/LOST, "node_id": …}
        self._object_states: Dict[str, Dict[str, Any]] = {}
        #: 每个 owner 持有多少引用
        self._ref_counts: Dict[str, Dict[str, int]] = {}
        #: worker_id → node_id(含 driver)
        self._worker_nodes: Dict[str, str] = {}
        #: worker_id → 类型
        self._worker_types: Dict[str, str] = {}
        #: 已分配但还没提交的共享内存块:{block_key: (worker_id, BlockHandle)}
        self._pending_blocks: Dict[str, Tuple[str, BlockHandle]] = {}

        # ---- 血缘(object_id → 生产它的 task 的可重放信息)
        self._lineage: Dict[str, Dict[str, Any]] = {}
        self._reconstructing: Set[str] = set()
        self.num_reconstructions = 0
        #: object_id → 生产它的 task_id(用于 state API)
        self._object_tasks: Dict[str, str] = {}

        # ---- actor 与放置组
        self._actors: Dict[str, _ActorRuntime] = {}
        self._local_actors: Dict[str, Any] = {}  # local_mode 下的真实实例
        self._pg_ready_objects: Dict[str, str] = {}
        #: actor 方法调用:task_id → actor_id。
        #:
        #: ``ray.cancel`` 以前只查 ``self._scheduler.tasks``,而 actor 方法调用
        #: **从来不进调度器**(它由 actor 自己的邮箱排队)—— 于是对 actor 方法 ref
        #: 调 ``ray.cancel`` 是一个**静默空操作**:不报错、不告警,方法照跑不误。
        #: 有了这张表,cancel 才能找到「这个 task 属于哪个 actor」。
        self._actor_task_index: Dict[str, str] = {}

        # ---- 生成器
        self._generators: Dict[str, Dict[str, Any]] = {}

        # ---- 观测
        self._events: List[Dict[str, Any]] = []
        #: 已经就「worker 池满」告警过的节点(只报一次)
        self._pool_warned: Set[str] = set()
        self._started_at = time.time()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._task_counter = 0

    # ==================================================================== 生命周期
    def start(self) -> "Raylet":
        """注册节点到 GCS 并启动调度循环。"""
        for node_id in self.node_ids:
            node = self._scheduler.nodes[node_id]
            self.gcs.call(
                "register_node",
                node_id=node_id,
                address=f"{self.address or 'local'}/{node_id[:8]}",
                resources=node.total.to_dict(),
            )
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="miniray-scheduler", daemon=True
        )
        self._scheduler_thread.start()
        return self

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._schedule_needed.set()
        with self._cond:
            self._cond.notify_all()
        for actor in list(self._actors.values()):
            if actor.slot is not None:
                actor.slot.request_shutdown()
        self._pool.shutdown()
        for store in self._stores.values():
            store.close()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=5.0)
        self.gcs.close()

    # ==================================================================== 事件
    def _record(self, event_kind: str, **fields: Any) -> None:
        """记一条事件(timeline / state API 的数据来源)。

        第一个参数叫 ``event_kind`` 而不是 ``kind`` —— 因为事件字段里本来就有
        ``kind``(worker 类型等),同名会撞车。
        """
        event = {"kind": event_kind, "ts": time.time(), **fields}
        self._events.append(event)
        if len(self._events) > _MAX_EVENTS:
            del self._events[: len(self._events) - _MAX_EVENTS]

    def get_timeline_events(self) -> List[Dict[str, Any]]:
        return list(self._events)

    # ==================================================================== 调度循环
    def _scheduler_loop(self) -> None:
        while not self._stopping.is_set():
            self._schedule_needed.wait(timeout=_TICK)
            self._schedule_needed.clear()
            try:
                self._schedule_tasks()
                self._process_pending_placement_groups()
                self._process_pending_actors()
                self._retire_idle_workers()
            except Exception:  # pragma: no cover - 调度循环不能死
                traceback.print_exc()
            self._pool.reap()

    def _schedule_tasks(self) -> None:
        """把 READY 的 task 派给 worker。可重入,直到没有进展为止。"""
        while not self._stopping.is_set():
            ready = self._scheduler.ready_tasks()
            if not ready:
                return
            progressed = False
            for record in ready:
                try:
                    node_id = self._scheduler.pick_node(record)
                except ScheduleError as exc:
                    self._fail_task_permanently(record.task_id.hex(), str(exc))
                    progressed = True
                    continue
                if node_id is None:
                    continue
                if self._dispatch(record, node_id):
                    progressed = True
            if not progressed:
                return

    def _warn_pool_exhausted(self, node_id: str) -> None:
        """worker 池满导致任务排队时提醒一次。

        「有 CPU 但并发上不去」最常见的两个原因就是:**0 CPU 任务不受资源限制,
        只受 worker 池限制**,以及**池子被占满**。这两种情况在状态里都表现为
        PENDING_NODE_ASSIGNMENT,不主动提示的话用户很难自己看出来。
        """
        if node_id in self._pool_warned:
            return
        self._pool_warned.add(node_id)
        logger.warning(
            "节点 %s 的 worker 池已达上限(%d),任务在排队。"
            "如果这些任务声明了 num_cpus=0,它们不受资源限制、只受 worker 池限制 —— "
            "可以调大 miniray.init(max_workers_per_node=...)",
            node_id[:8],
            self._pool.max_workers_per_node or max(16, int(self._scheduler.nodes[node_id].total.cpu) * 8),
        )

    def _dispatch(self, record: TaskRecord, node_id: str) -> bool:
        """把一个 READY 的 task 交给某个节点上的 worker(没有就拉起一个)。"""
        # ★ 第三道闸门(F1):local_mode 的就地执行走的是同一条调度循环,
        #   「正在 inline 执行」的任务绝不能在这里被派给子进程 ——
        #   这是「同一件事有两条实现路径」的典型:补了 ready_tasks 的过滤,
        #   还得在真正派活的地方再拦一次。
        if record.inline_running:
            return False
        with self._lock:
            idle = [
                slot
                for slot in self._pool.idle_slots(node_id)
                if slot.worker_type == "task"
            ]
            if idle:
                slot = idle[0]
                try:
                    self._scheduler.assign(record, node_id)
                except ScheduleError as exc:
                    # pick_node 是在锁外算的,而 assign 会在锁内**重新**核对
                    # 放置组 bundle —— 中间可能被别的任务抢走最后一个能放下的
                    # bundle。此时**绝不能**把任务丢掉:一旦 remove_task,
                    # 再没人会写它的结果对象,ray.get 就永久挂起(而且不报错)。
                    # 正确做法是把它当失败处理(写异常到结果对象),让调用方看到原因。
                    #
                    # ⚠️ 唯一的例外是「正在就地执行」:那不是失败,是它本来就在
                    #    别处跑,直接跳过、**不要**写错误对象(写了会把 inline
                    #    那次已经产出的结果覆盖成异常)。
                    if record.inline_running:
                        return False
                    self._fail_task_permanently(record.task_id.hex(), str(exc))
                    return True
                record.worker_id = slot.worker_id
                self._record(
                    "task_scheduled",
                    task_id=record.task_id.hex(),
                    name=record.name,
                    node_id=node_id,
                    worker_id=slot.worker_id,
                    pid=slot.pid,
                )
                slot.deliver(self._task_message(record))
                return True

            node = self._scheduler.nodes[node_id]
            if not self._pool.can_spawn(node_id, node.total.cpu):
                self._warn_pool_exhausted(node_id)
                return False
            try:
                self._scheduler.assign(record, node_id)
            except ScheduleError:
                return False
            worker_id = WorkerID.from_random().hex()
            record.worker_id = worker_id
            slot = self._pool.spawn(
                node_id, self._worker_config(worker_id, node_id), worker_type="task"
            )
            self._worker_nodes[worker_id] = node_id
            self._worker_types[worker_id] = "task"
            self._record(
                "worker_spawned", worker_id=worker_id, node_id=node_id, pid=slot.pid, worker_type="task"
            )
            self._record(
                "task_scheduled",
                task_id=record.task_id.hex(),
                name=record.name,
                node_id=node_id,
                worker_id=worker_id,
                pid=slot.pid,
            )
            slot.deliver(self._task_message(record))
            return True

    def _worker_config(self, worker_id: str, node_id: str) -> Dict[str, Any]:
        return {
            "raylet_address": self.address,
            "gcs_address": self.gcs.address,
            "node_id": node_id,
            "worker_id": worker_id,
            "job_id": self.config.job_id,
            "namespace": self.config.namespace,
            "session_dir": self.config.session_dir,
            "worker_type": "task",
            "driver_node_id": self._driver_node,
        }

    def _task_message(self, record: TaskRecord) -> Dict[str, Any]:
        """构造发给 worker 的任务消息(纯数据,可 pickle)。"""
        lineage = record.lineage or {}
        return {
            "kind": "task",
            "task_id": record.task_id.hex(),
            "name": record.name,
            "function": lineage.get("function"),
            "args": lineage.get("args", b""),
            "num_returns": len(record.result_ids),
            "deps": [oid.hex() for oid in record.deps],
            "result_ids": [oid.hex() for oid in record.result_ids],
            "is_generator": record.is_generator,
            "generator_id": lineage.get("generator_id"),
            "runtime_env": lineage.get("runtime_env", {}),
            "gpu_ids": list(record.gpu_ids),
            "actor_id": record.actor_id,
            "attempt": record.num_attempts,
            "name_hint": record.name,
        }

    # ==================================================================== worker 注册
    def register_worker(
        self,
        worker_id: str,
        node_id: str,
        pid: int,
        worker_type: str = "task",
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._worker_nodes[worker_id] = node_id
            self._worker_types[worker_id] = worker_type
            slot = self._pool.get(worker_id)
            if slot is not None:
                slot.pid = pid
            self._record(
                "worker_registered", worker_id=worker_id, node_id=node_id, pid=pid, worker_type=worker_type
            )
            return {"node_id": node_id, "driver_node_id": self._driver_node}

    def disconnect_worker(self, worker_id: str) -> None:
        """worker 优雅退出。"""
        with self._lock:
            self._worker_nodes.pop(worker_id, None)
        slot = self._pool.get(worker_id)
        if slot is not None and slot.state != WorkerState.DEAD:
            slot.state = WorkerState.SHUTDOWN

    def request_work(self, worker_id: str) -> Dict[str, Any]:
        """长轮询:worker 要活干。

        这是 raylet 与 worker 之间唯一的一条「下行」通道 —— 因为 worker 不监听
        端口,只能由它来问。超时(默认 1 秒)返回 ``noop``,顺便起到心跳作用:
        如果 worker 卡死了,raylet 立刻能发现(进程监控 + 连接断开)。
        """
        slot = self._pool.get(worker_id)
        if slot is None:
            return {"kind": "shutdown", "reason": "worker 未注册"}
        with slot.cond:
            if slot.pending_task is None and not slot.shutdown_requested:
                slot.state = WorkerState.IDLE
                slot.last_idle_at = time.time()
                self._schedule_needed.set()
                slot.cond.wait(timeout=_POLL_TIMEOUT)
            if slot.shutdown_requested and slot.pending_task is None:
                return {"kind": "shutdown"}
            task = slot.pending_task
            slot.pending_task = None
            if task is None:
                return {"kind": "noop"}
            return {"kind": "task", "task": task}

    # ==================================================================== 任务提交
    def submit_task(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """提交一个 task:登记血缘 → 进调度器 → 返回结果对象 ID。"""
        with self._lock:
            self._task_counter += 1
            task_id = TaskID.from_random()
            func = FunctionDescriptor.from_dict(spec["function"])
            num_returns = int(spec.get("num_returns", 1))
            result_ids = [ObjectID.from_random().binary() for _ in range(max(1, num_returns))]
            deps = _extract_deps(spec["args"])

            resources = Resources.from_request(
                spec.get("num_cpus"),
                spec.get("num_gpus"),
                spec.get("memory"),
                spec.get("resources"),
            )
            strategy = spec.get("scheduling_strategy") or {}
            record = TaskRecord(
                task_id=task_id,
                name=spec.get("name") or func.qualname,
                result_ids=result_ids,
                deps=set(deps),
                resources=resources,
                owner=spec.get("owner", ""),
                node_affinity=strategy.get("node_id"),
                placement_group_id=strategy.get("placement_group_id"),
                bundle_index=int(strategy.get("bundle_index", -1)),
                is_generator=bool(spec.get("is_generator")),
                max_retries=int(spec.get("max_retries", 3)),
                lineage={
                    "function": spec["function"],
                    "args": spec["args"],
                    "runtime_env": spec.get("runtime_env", {}),
                    "num_returns": num_returns,
                    "spec": spec,
                },
            )
            if record.is_generator:
                # 生成器 task 在 mini-ray 里**刻意不支持重试**:流可能已经被消费了
                # 一部分,重放要做对很难(必须「跳过已产出的值、从断点继续」)。
                #
                # ⚠️ 这是 mini-ray 的简化,**不是 Ray 的限制** ——
                # 真实 Ray **支持**生成器任务重试,语义正是上面那句。
                # 详见教程第 5 章 §5.3 与第 10 章 §10.2。
                record.max_retries = 0
                generator_id = ObjectID.from_random().hex()
                record.lineage["generator_id"] = generator_id
                self._generators[generator_id] = {"refs": [], "done": False, "task_id": task_id.hex()}
                self._record("task_submitted", task_id=task_id.hex(), name=record.name, generator=True)
                self._scheduler.add_task(record, ready_objects=self._ready_deps_of(record))
                self._mark_deps_ready(record)
                # ⚠️ 这里**不能**无条件 set:local_mode 下任务已经在下面这一行
                # 就地跑完了,再唤醒调度循环只会让它被拉到**子进程里再跑一遍**
                # (生成器尤其糟:函数体执行两次、chunk 交错来自两个生产者)。
                if self.config.local_mode:
                    self._execute_inline(record, spec, generator_id=generator_id)
                else:
                    self._schedule_needed.set()
                return {"generator_id": generator_id, "task_id": task_id.hex()}

            for oid in result_ids:
                self._object_states[oid.hex()] = {"state": "PENDING", "node_id": None}
                self._object_tasks[oid.hex()] = task_id.hex()
                self._lineage[oid.hex()] = {
                    "task_spec": spec,
                    "task_id": task_id.hex(),
                    "node_id": None,
                }
            self._record("task_submitted", task_id=task_id.hex(), name=record.name)
            self._scheduler.add_task(record, ready_objects=self._ready_deps_of(record))
            self._mark_deps_ready(record)
            if self.config.local_mode:
                # ★ 在**放开这把锁之前**把它摘出就绪队列并标成「不可调度」。
                #   只在 _execute_inline 入口做是不够的:从 add_task 到进入 inline
                #   之间调度线程正好能扫一遍队列(它每 _TICK=0.2 秒醒一次),
                #   捞走之后就派给一个新拉起的子进程跑第二遍 —— 副作用翻倍,
                #   而 driver 里那次的结果先写进对象存储,调用方完全看不出来。
                self._scheduler.begin_inline(record)

        if self.config.local_mode:
            self._execute_inline(record, spec)
        else:
            self._schedule_needed.set()
        return {"result_ids": [oid.hex() for oid in result_ids], "task_id": task_id.hex()}

    def _is_available(self, object_id_hex: str) -> bool:
        """这个对象此刻**真的能当输入用**吗?

        ⚠️ 关键:被判定「丢失」的对象在 ``_object_states`` 里也是 ``READY``
        (见 :meth:`_mark_lost`),但它在存储里的内容是 ``ObjectLostError``
        占位对象 —— 必须当成**不可用**。

        否则重放的任务会把一个异常对象当参数收下,报出与真正原因无关的错:
        ``'ObjectLostError' object is not subscriptable``。
        ``ray.wait`` / ``ray.get`` 那边仍然把 lost 当 ready(让调用方能拿到
        明确的报错),**只有「当输入用」这条路径需要区分**。
        """
        state = self._object_states.get(object_id_hex, {})
        return state.get("state") == "READY" and not state.get("lost")

    def _ready_deps_of(self, record: TaskRecord) -> Set[bytes]:
        """这个任务的依赖里,此刻已经就绪的那些。"""
        return {oid for oid in record.deps if self._is_available(oid.hex())}

    def _mark_deps_ready(self, record: TaskRecord) -> None:
        """把「已就绪依赖」的位置喂给调度器,用于本地性打分。"""
        deps = [oid.hex() for oid in record.deps]
        if not deps:
            return
        locations: Dict[str, List[str]] = {}
        for oid in deps:
            state = self._object_states.get(oid)
            if state and state.get("node_id"):
                locations[oid] = [state["node_id"]]
        if not locations:
            return
        for oid, nodes in locations.items():
            self._scheduler.object_locations.setdefault(bytes.fromhex(oid), set()).update(nodes)

    # ==================================================================== 任务结束
    def task_done(
        self,
        task_id: str,
        worker_id: str,
        results: List[Dict[str, Any]],
        *,
        actor_id: Optional[str] = None,
    ) -> None:
        """worker 报「任务干完了」:把结果落进对象存储并唤醒等待者。"""
        with self._lock:
            record = self._scheduler.tasks.get(task_id)
            if record is None:
                # actor 方法调用**不进调度器**(它由 actor 自己排队执行),
                # 所以这里要单独找它的结果对象;找不到说明任务已被取消,直接丢弃。
                context = self._actor_task_context(actor_id, task_id) if actor_id else None
                if context is None:
                    return
                result_ids, node_id = context
                is_generator = False
                task_name = f"actor:{actor_id[:8]}"
                duration = None
            else:
                node_id = record.node_id or self._worker_nodes.get(worker_id, self._driver_node)
                result_ids = list(record.result_ids)
                is_generator = record.is_generator
                task_name = record.name
                duration = record.duration()
                self._scheduler.task_finished(task_id)
            self._record(
                "task_finished", task_id=task_id, name=task_name, node_id=node_id, duration=duration
            )
            slot = self._pool.get(worker_id)
            if slot is not None and slot.worker_type == "task":
                slot.state = WorkerState.IDLE
                slot.last_idle_at = time.time()
                slot.task_id = None
                slot.num_tasks_executed += 1

        # 结果落盘(在锁外做,避免长时间占住 raylet 主锁)
        for object_id, payload in zip(result_ids, results):
            self._store_result(object_id, payload, node_id)
            self._mark_object_ready(object_id.hex(), node_id)

        if actor_id:
            self._actor_task_finished(actor_id, task_id)
        if is_generator:
            self._finish_generator(task_id)
        else:
            self._schedule_needed.set()

    def _actor_task_context(
        self, actor_id: str, task_id: str
    ) -> Optional[Tuple[List[bytes], str]]:
        """取出 actor 任务的「结果对象 + 所在节点」。"""
        runtime = self._actors.get(actor_id)
        if runtime is None:
            return None
        info = runtime.running.get(task_id)
        if info is None:
            return None
        result_ids = [bytes.fromhex(oid) for oid in info["task"].get("result_ids", [])]
        return result_ids, runtime.node_id

    def task_failed(
        self,
        task_id: str,
        worker_id: str,
        error: Dict[str, Any],
        *,
        actor_id: Optional[str] = None,
        crashed: bool = False,
        retryable: Optional[bool] = None,
    ) -> None:
        """worker 报「任务失败了」:按重试策略决定重跑还是把异常写进结果对象。

        :param retryable: ``None``(默认)按 ``max_retries`` 判断;``False`` 表示
            「这次失败重试也没用」(调度阶段的失败),直接写错误、不再入队。
        """
        with self._lock:
            record = self._scheduler.tasks.get(task_id)
            if record is None:
                if actor_id:
                    context = self._actor_task_context(actor_id, task_id)
                    if context is not None:
                        result_ids, node_id = context
                        error_object = _error_from_payload(error, actor=True)
                        for object_id in result_ids:
                            self._store_error(object_id, error_object, node_id)
                            self._mark_object_ready(object_id.hex(), node_id)
                        self._record("task_failed", task_id=task_id, name=f"actor:{actor_id[:8]}", error=error.get("message", ""))
                        self._actor_task_finished(actor_id, task_id)
                return
            slot = self._pool.get(worker_id)
            if slot is not None and slot.worker_type == "task":
                slot.state = WorkerState.IDLE
                slot.last_idle_at = time.time()
                slot.num_tasks_executed += 1
            retry = self._scheduler.task_failed(
                task_id,
                error.get("message", "task failed"),
                retryable=True if retryable is None else retryable,
            )
            if retry:
                self._record(
                    "task_retry", task_id=task_id, name=record.name, attempt=record.num_attempts
                )
                # 重新排进 READY 队列(结果对象的 ID 不变,等待者无感)。
                # 必须走 _make_ready:调度阶段的失败并没有被 assign 消费掉
                # 队列里那条旧记录,直接 append 会让同一个 task 在队列里出现两次,
                # 每轮翻一倍。
                with self._lock:
                    self._scheduler._make_ready(record)  # noqa: SLF001 (内部队列)
                self._schedule_needed.set()
                return
            self._record(
                "task_failed",
                task_id=task_id,
                name=record.name,
                error=error.get("message", ""),
            )
            result_ids = list(record.result_ids)

        # 重试用尽:把异常写进**每一个**结果对象(Ray 的行为)
        error_object = self._build_task_error(record, error, crashed)
        if record.is_generator:
            # ⚠️ 生成器要单独走一条路,不能套用上面那句「写进每一个结果对象」:
            #
            # 1. 生成器的 ``record.result_ids`` 是**边产边追加**的(每 yield 一个
            #    就 append 一个),所以「每一个结果对象」里包含**消费者已经取走
            #    的 chunk** —— 照写会把拿到手的数据换成异常对象。
            # 2. 更致命的是:不标记 done,消费者 ``for ref in gen`` 会一直等下一个
            #    chunk,而那个 chunk 永远不会来 —— ``wait_for_generator`` 到点
            #    只是**返回**,不抛错,于是循环每 60 秒空转一次,永不结束。
            #
            # 正确做法:让生成器以「错误」结束,消费者取完已产出的 chunk 后
            # 在 ``__next__`` 里收到这个异常。
            self._finish_generator(task_id, error=error)
            if actor_id:
                self._actor_task_finished(actor_id, task_id)
            return
        # ★ 错误对象写到**任务实际跑的那个节点**上,并用同一个 node_id 标记就绪。
        #   以前这里固定写 driver 节点、却用 record.node_id 标记 —— 跨节点消费者
        #   在下游拉取时找不到对象,回落成 ObjectLostError,把用户指向错误的方向。
        failure_node = record.node_id or self._driver_node
        for object_id in result_ids:
            self._store_error(object_id, error_object, failure_node)
            self._mark_object_ready(object_id.hex(), failure_node)
        if actor_id:
            self._actor_task_finished(actor_id, task_id)

    def cancel_task(self, object_id_or_task_id: str, *, force: bool = False) -> None:
        """取消任务(``ray.cancel``)。

        :param force: ``False`` 时只取消**还没开始执行**的任务(Ray 的默认行为,
            因为已经跑起来的任务没收手的地方);``True`` 会干掉正在跑它的 worker ——
            代价是那个 worker 上别的工作也一起没了。
        """
        task_id = self._object_tasks.get(object_id_or_task_id, object_id_or_task_id)
        with self._lock:
            record = self._scheduler.tasks.get(task_id)
        if record is None:
            # ★ actor 方法调用**从来不进调度器**(由 actor 自己的邮箱排队),
            #   所以上面查不到 —— 以前这里直接 return,于是
            #   ``ray.cancel(actor_method_ref)`` 是一个**静默空操作**:
            #   返回了、不报错、不告警,方法照跑不误。走 actor 那条路。
            actor_id = self._actor_task_index.get(task_id)
            if actor_id is not None:
                self._cancel_actor_task(actor_id, task_id, force=force)
            return
        if record.state in (TaskState.PENDING, TaskState.READY):
            self._scheduler.mark_cancelled(task_id)
            error = TaskCancelledError(f"任务 {record.name} 在开始执行前被取消")
            self._store_cancel_error(record, error)
            self._record("task_cancelled", task_id=task_id, name=record.name)
            self._schedule_needed.set()
            return
        if record.state is TaskState.RUNNING and force:
            worker_id = record.worker_id
            # 先把任务标成取消(结果对象写错误),再杀 worker。
            # 顺序不能反:worker 被 retire 之后就从池子里摘掉了,不会再走
            # _on_worker_death,那样结果对象会永远停在 PENDING,调用方死等。
            self._scheduler.mark_cancelled(task_id)
            error = TaskCancelledError(f"任务 {record.name} 被 ray.cancel(force=True) 强制终止")
            self._store_cancel_error(record, error)
            self._record("task_cancelled", task_id=task_id, name=record.name, forced=True)
            slot = self._pool.get(worker_id or "")
            if slot is not None:
                self._pool.retire(slot)

    def _cancel_actor_task(self, actor_id: str, task_id: str, *, force: bool) -> bool:
        """取消一个 actor 方法调用。返回 ``True`` 表示确实取消了。

        actor 方法调用有**两种**在途形态,所以取消要分开处理:

        * **还在邮箱里排队**(没被任何执行槽位取走)—— 直接摘掉,并把
          ``TaskCancelledError`` 写进它的每一个结果对象。这是 ``force`` 与否
          都会生效的那种,也是 ``ray.cancel`` 最有用的场景。
        * **已经在某个槽位上跑着** —— 没有真的中断手段(和 task 路径一致):
          ``force=False`` 时是「尽力而为的空操作」;``force=True`` 时把执行槽位
          放开并把结果判成取消,方法报回来的结果会被丢弃
          (``task_done`` 在 ``runtime.running`` 里找不到它就会丢掉)。
        """
        with self._lock:
            runtime = self._actors.get(actor_id)
            if runtime is None:
                return False
            info = runtime.running.get(task_id)
            task: Optional[Dict[str, Any]] = info["task"] if info is not None else None
            if task is None:
                with runtime.cond:
                    for mailbox in runtime.mailboxes.values():
                        for queued in list(mailbox):
                            if queued["task_id"] == task_id:
                                task = queued
                                mailbox.remove(queued)
                                break
                        if task is not None:
                            break
                    if task is not None:
                        runtime.cond.notify_all()
            elif info["task"].get("is_creation"):
                # ``__init__`` 的取消没有意义(它会决定 actor 能不能起来)
                return False
            elif not force:
                return False
            else:
                runtime.running.pop(task_id, None)
                group = info["group"]
                runtime.inflight[group] = max(0, runtime.inflight.get(group, 0) - 1)
                with runtime.cond:
                    runtime.cond.notify_all()
            if task is None:
                return False
            result_ids = list(task.get("result_ids") or [])
        error = TaskCancelledError(
            f"actor 方法 {task['method_name']} 被 ray.cancel(force={force}) 取消"
        )
        for object_id in result_ids:
            self._store_error(bytes.fromhex(object_id), error, runtime.node_id)
            self._mark_object_ready(object_id, runtime.node_id)
        self._record(
            "actor_task_cancelled", actor_id=actor_id, task_id=task_id, forced=force
        )
        return True

    def _store_cancel_error(self, record: TaskRecord, error: BaseException) -> None:
        """把「被取消」写进结果对象。

        ⚠️ 生成器必须走 ``_finish_generator``,**不能**套用「写进每一个结果对象」
        —— ``record.result_ids`` 对生成器是边产边追加的,里面包含消费者已经取走的
        chunk,照写会把拿到手的数据换成 CancelledError;而且不标记 ``done`` 的话
        消费端会永远等下一个 chunk(``wait_for_generator`` 到点只是返回、不抛错)。
        这和 ``task_failed`` / ``_execute_inline`` 里那两处是同一个道理。
        """
        if record.is_generator:
            self._finish_generator(
                record.task_id.hex(), error=_error_payload(error)
            )
            return
        failure_node = record.node_id or self._driver_node
        for object_id in record.result_ids:
            self._store_error(object_id, error, failure_node)
            self._mark_object_ready(object_id.hex(), failure_node)

    def _fail_task_permanently(self, task_id: str, message: str) -> None:
        """调度阶段就失败(资源/放置组/节点亲和)—— **不重试**,直接把原因写给调用方。

        名字里的 "permanently" 是认真的:重试一次也不会变得可调度,而
        ``num_attempts`` 在没走到 ``assign`` 之前永远是 0,所以按
        「``num_attempts <= max_retries``」判断会让它**无限重试**(实测 2 秒内
        READY 队列涨到 6 万条,``ray.get`` 永远返回不了)。
        """
        record = self._scheduler.tasks.get(task_id)
        if record is None:
            return
        error = {"type": "ScheduleError", "message": message, "traceback": ""}
        self.task_failed(task_id, record.worker_id or "", error, retryable=False)

    def _build_task_error(
        self, record: TaskRecord, error: Dict[str, Any], crashed: bool
    ) -> RayTaskError:
        """把 worker 报上来的错误信息变成 :class:`RayTaskError`。

        崩溃(worker 进程没了)用 :class:`WorkerCrashedError` —— 语义上它和
        「函数抛异常」是不同的东西:前者要换 worker 重跑,后者重跑大概率还失败。
        """
        cls = WorkerCrashedError if crashed else RayTaskError
        cause: Optional[BaseException] = None
        cause_type = error.get("type", "Exception")
        try:
            # 用 import builtins 而不是 __builtins__(后者在模块里是 dict)
            import builtins as _builtins

            cause_cls = getattr(_builtins, cause_type, None)
            if not isinstance(cause_cls, type):
                cause_cls = Exception
            cause = cause_cls(error.get("message", ""))
        except Exception:  # pragma: no cover - 构造函数签名千奇百怪
            cause = Exception(error.get("message", ""))
        return cls(
            function_name=record.name,
            cause=cause,
            traceback_str=error.get("traceback", ""),
            proctitle=f"miniray::{record.name}",
        )

    # ==================================================================== 对象写入
    def put_object(self, worker_id: str, object_id: str, payload: Dict[str, Any]) -> None:
        node_id = self._worker_nodes.get(worker_id, self._driver_node)
        if payload.get("kind") == "bytes":
            self._stores[node_id].put_bytes(bytes.fromhex(object_id), payload["data"])
        else:
            raise RaySystemError(f"put_object 不支持 {payload.get('kind')}")
        self._mark_object_ready(object_id, node_id)

    def allocate_object(self, worker_id: str, size: int) -> Dict[str, Any]:
        """给 worker 预留一块共享内存(数据由 worker 直接写进去,省一次拷贝)。"""
        node_id = self._worker_nodes.get(worker_id, self._driver_node)
        store = self._stores[node_id]
        try:
            block = store.allocate_block(int(size))
        except ObjectStoreFullError:
            raise
        if block is None:  # pragma: no cover - 分配器被禁用
            raise RaySystemError("共享内存分配器不可用")
        with self._lock:
            self._pending_blocks[f"{block.shm_name}:{block.offset}"] = (worker_id, block)
        return {
            "shm": block.shm_name,
            "offset": block.offset,
            "nbytes": block.size,
            "bucket": block.bucket,
        }

    def commit_object(
        self,
        worker_id: str,
        object_id: str,
        block: Dict[str, Any],
        *,
        kind: str,
        dtype: Optional[str] = None,
        shape: Optional[Sequence[int]] = None,
    ) -> None:
        """worker 已经把数据写进共享内存,这里登记成对象。"""
        node_id = self._worker_nodes.get(worker_id, self._driver_node)
        store = self._stores[node_id]
        handle = BlockHandle(
            block["shm"], int(block["offset"]), int(block["nbytes"]), int(block["bucket"])
        )
        with self._lock:
            self._pending_blocks.pop(f"{handle.shm_name}:{handle.offset}", None)
        if kind == "ndarray":
            descriptor = {
                "kind": "ndarray",
                "shm": handle.shm_name,
                "offset": handle.offset,
                "nbytes": handle.size,
                "bucket": handle.bucket,
                "dtype": dtype or "<f8",
                "shape": tuple(int(x) for x in (shape or ())),
                "readonly": True,
            }
        else:
            descriptor = {
                "kind": "bytes",
                "shm": handle.shm_name,
                "offset": handle.offset,
                "nbytes": handle.size,
                "bucket": handle.bucket,
            }
        store.commit_block(bytes.fromhex(object_id), handle, descriptor)
        self._mark_object_ready(object_id, node_id)

    def _store_result(self, object_id: bytes, payload: Dict[str, Any], node_id: str) -> None:
        """把 worker 报回来的结果写进对象存储。"""
        kind = payload.get("kind")
        store = self._stores[node_id]
        if kind == "bytes":
            store.put_bytes(object_id, payload["data"])
        elif kind == "error":
            store.put_bytes(object_id, serialization.dumps(_error_from_payload(payload["error"])))
        elif kind == "block":
            # 结果已经在共享内存里(worker 用 allocate/commit 写的)
            pass
        else:  # pragma: no cover - 防御
            raise RaySystemError(f"未知的结果负载: {kind}")

    def _store_error(
        self, object_id: bytes, error: BaseException, node_id: Optional[str] = None
    ) -> None:
        """把一个「错误对象」写进对象存储。

        ``node_id`` **必须和随后 ``_mark_object_ready`` 用的那个节点一致** ——
        对象目录里写着「这个对象在节点 N 上」,内容却写在了 driver 节点上,
        跨节点消费者就会在 N 上找不到它,于是回落成 ``ObjectLostError``:
        「对象已丢失」这句**指向了与事实完全相反的方向**(真正的失败原因在
        另一个节点上躺得好好的)。同节点的消费者碰巧能拿到,所以单节点下
        完全测不出来。默认值仍然是 driver 节点(与 ``_mark_lost`` 自洽)。
        """
        target = node_id or self._driver_node
        try:
            data = serialization.dumps(error)
        except Exception:  # pragma: no cover - 异常本身不可序列化时退化成字符串
            data = serialization.dumps(RayTaskError("<task>", Exception(str(error))))
        store = self._stores.get(target)
        if store is None:  # pragma: no cover - 防御:目标节点已经不存在
            store = self._stores[self._driver_node]
        store.put_bytes(object_id, data)

    def _mark_object_ready(self, object_id: str, node_id: str) -> None:
        """对象就绪的**唯一入口**:更新状态、对象目录、唤醒等待者、触发后续调度。"""
        with self._cond:
            self._object_states[object_id] = {"state": "READY", "node_id": node_id}
            self._scheduler.object_locations.setdefault(bytes.fromhex(object_id), set()).add(node_id)
            self._reconstructing.discard(object_id)   # 重建完成 → 解除守卫
            self._cond.notify_all()
        self.gcs.add_object_locations([object_id], node_id)
        lineage = self._lineage.get(object_id)
        if lineage is not None:
            lineage["node_id"] = node_id
        newly_ready = self._scheduler.mark_object_ready(bytes.fromhex(object_id), node_id)
        if self.config.local_mode:
            for record in newly_ready:
                spec = (record.lineage or {}).get("spec")
                if spec is not None:
                    self._execute_inline(record, spec)
            return
        self._record("object_ready", object_id=object_id, node_id=node_id)
        self._schedule_needed.set()

    # ==================================================================== 对象读取
    def fetch_objects(self, worker_id: str, object_ids: Sequence[str]) -> Dict[str, Any]:
        """取对象。同节点 → 零拷贝描述符;跨节点 → 拷贝。

        Ray 的做法是把跨节点对象**拉进本地 plasma**(``pull``),后续消费者就都是
        本地的;mini-ray 简化为「直接传数据、不缓存」—— 默认单节点下没有区别。
        """
        node_id = self._worker_nodes.get(worker_id, self._driver_node)
        store = self._stores[node_id]
        out: Dict[str, Any] = {}
        for object_id in object_ids:
            raw = bytes.fromhex(object_id)
            state = self._object_states.get(object_id, {})
            # 1) 本地节点对象存储里有 → 直接给(ndarray 给描述符,零拷贝)
            if store.contains(raw):
                out[object_id] = self._local_payload(store, object_id, raw)
                continue
            # 2) 别的节点有 → 拉过来(一次拷贝)
            payload = self._pull_from_other_node(object_id, node_id)
            if payload is not None:
                out[object_id] = payload
                continue
            # 3) 还没有/正在重建 → 等一会儿再试(Ray 里会阻塞在 pull 上)
            if state.get("state") in ("PENDING", "RECONSTRUCTING"):
                if self._wait_until_ready([object_id], 1, timeout=30.0):
                    if store.contains(raw):
                        out[object_id] = self._local_payload(store, object_id, raw)
                        continue
                    payload = self._pull_from_other_node(object_id, node_id)
                    if payload is not None:
                        out[object_id] = payload
                        continue
            # 4) 真没有:返回一个「丢失」错误对象,让调用方明确报错而不是死等
            reason = state.get("reason") or (
                f"对象状态为 {state.get('state', 'UNKNOWN')},等待就绪超时后仍不可用"
            )
            logger.warning(
                "对象 %s 取不到(%s):所在状态=%s,生产者任务=%s。"
                "如果状态是 RECONSTRUCTING 且一直不变,说明重建它的任务没能跑起来",
                object_id[:8],
                reason,
                state.get("state", "UNKNOWN"),
                (self._object_tasks.get(object_id) or "-")[:8],
            )
            out[object_id] = {
                "kind": "bytes",
                "data": serialization.dumps(ObjectLostError(object_id, reason)),
            }
        return out

    def _local_payload(self, store: ObjectStore, object_id: str, raw: bytes) -> Dict[str, Any]:
        descriptor = store.get_descriptor(raw)
        if descriptor is not None and descriptor.get("kind") == "ndarray":
            return {
                "kind": "ndarray",
                "shm": descriptor["shm"],
                "offset": descriptor["offset"],
                "nbytes": descriptor["nbytes"],
                "dtype": descriptor["dtype"],
                "shape": list(descriptor["shape"]),
                "readonly": True,
            }
        if descriptor is not None:  # 字节存在共享内存里
            return {
                "kind": "bytes_in_block",
                "shm": descriptor["shm"],
                "offset": descriptor["offset"],
                "nbytes": descriptor["nbytes"],
            }
        return {"kind": "bytes", "data": store.get_bytes(raw)}

    def _pull_from_other_node(self, object_id: str, requester_node: str) -> Optional[Dict[str, Any]]:
        """跨节点取对象(真实 Ray 里这一步是 ObjectManager 的 pull,数据面独立)。"""
        locations = self.gcs.get_object_locations([object_id]).get(object_id, [])
        for node_id in locations:
            if node_id == requester_node or node_id not in self._stores:
                continue
            store = self._stores[node_id]
            raw = bytes.fromhex(object_id)
            if not store.contains(raw):
                continue
            if store.is_ndarray(raw):
                array = store.get_ndarray(raw)
                return {
                    "kind": "ndarray_raw",
                    "dtype": array.dtype.str,
                    "shape": list(array.shape),
                    "data": array.tobytes(),
                }
            return {"kind": "bytes", "data": store.get_bytes(raw)}
        return None

    def wait_for_objects(
        self,
        worker_id: str,
        object_ids: Sequence[str],
        *,
        num_returns: int = 1,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """等至少 ``num_returns`` 个对象就绪(或超时)。

        实现是条件变量 + 超时,不是轮询 —— 对象就绪时 :meth:`_mark_object_ready`
        会 ``notify_all``。
        """
        deadline = None if timeout is None else time.time() + float(timeout)
        with self._cond:
            while True:
                ready = [
                    oid for oid in object_ids if self._object_states.get(oid, {}).get("state") == "READY"
                ]
                if len(ready) >= num_returns or not object_ids:
                    break
                if deadline is None:
                    self._cond.wait(timeout=_POLL_TIMEOUT)
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(_POLL_TIMEOUT, remaining))
            ready = [
                oid for oid in object_ids if self._object_states.get(oid, {}).get("state") == "READY"
            ]
            remaining_ids = [oid for oid in object_ids if oid not in set(ready)]
        return {"ready": ready, "remaining": remaining_ids}

    def _wait_until_ready(self, object_ids: Sequence[str], num_returns: int, *, timeout: float) -> bool:
        result = self.wait_for_objects("", object_ids, num_returns=num_returns, timeout=timeout)
        return len(result["ready"]) >= num_returns

    # ==================================================================== 引用计数
    def update_ref_counts(self, worker_id: str, deltas: Dict[str, int]) -> None:
        with self._lock:
            for object_id, delta in deltas.items():
                owners = self._ref_counts.setdefault(object_id, {})
                count = owners.get(worker_id, 0) + int(delta)
                if count > 0:
                    owners[worker_id] = count
                else:
                    owners.pop(worker_id, None)
            self._push_ref_counts(deltas.keys())

    def _push_ref_counts(self, object_ids: Sequence[str]) -> None:
        """把「集群范围内还有多少引用」压给对象存储(它据此决定能不能回收)。"""
        for object_id in object_ids:
            total = sum(self._ref_counts.get(object_id, {}).values())
            raw = bytes.fromhex(object_id)
            for store in self._stores.values():
                if store.contains(raw):
                    store.set_ref_count(raw, total)

    def pin_object(self, worker_id: str, object_id: str) -> None:
        node_id = self._worker_nodes.get(worker_id, self._driver_node)
        store = self._stores[node_id]
        raw = bytes.fromhex(object_id)
        if store.contains(raw):
            store.pin(raw, owner=worker_id)

    def unpin_object(self, worker_id: str, object_id: str) -> None:
        node_id = self._worker_nodes.get(worker_id, self._driver_node)
        store = self._stores[node_id]
        raw = bytes.fromhex(object_id)
        if store.contains(raw):
            store.unpin(raw, owner=worker_id)

    # ==================================================================== 生成器
    def task_chunk(
        self,
        task_id: str,
        generator_id: str,
        payload: Dict[str, Any],
        *,
        is_last: bool = False,
    ) -> Dict[str, Any]:
        """生成器 task 产出一个 chunk:登记成对象、唤醒下游,yield 出去的立刻可用。"""
        with self._lock:
            generator = self._generators.get(generator_id)
            if generator is None:
                raise MiniRayError(f"未知的生成器 {generator_id[:8]}")
            if generator["done"]:
                # 生成器已经结束(正常收尾 / 失败 / 被取消),之后的 chunk 一律丢弃 ——
                # 否则被 kill 的 worker 临死前报上来的那一个 chunk 会挂在 refs 尾巴上,
                # 消费者下次 __next__ 就把它当成有效数据取走。
                return {"object_id": "", "dropped": True}
            record = self._scheduler.tasks.get(task_id)
            node_id = (record.node_id if record else None) or self._driver_node
            object_id = ObjectID.from_random().hex()
            self._object_states[object_id] = {"state": "PENDING", "node_id": None}
            generator["refs"].append(object_id)
            if record is not None:
                record.result_ids.append(bytes.fromhex(object_id))
                # ★ 把「这个对象是哪个 task 产出的」记下来。
                #   生成器的 chunk 以前**没有**这条登记,后果有两个:
                #   1) ``ray.cancel(chunk_ref)`` 查不到对应的 task,静默什么都不做
                #      (实测:force=True 之后生成器照样跑完);
                #   2) state API 里 ``produced_by`` 永远是 None。
                self._object_tasks[object_id] = task_id
            with self._cond:
                self._cond.notify_all()
            self._scheduler.object_locations.setdefault(bytes.fromhex(object_id), set())
        self._store_result(bytes.fromhex(object_id), payload, node_id)
        self._mark_object_ready(object_id, node_id)
        if is_last:
            self._finish_generator(task_id)
        return {"object_id": object_id}

    def _finish_generator(self, task_id: str, error: Optional[Dict[str, Any]] = None) -> None:
        """标记生成器「结束了」。

        ``error`` 非空表示它是**失败**结束的 —— 消费者在取完已产出的 chunk 之后
        要收到这个异常,而不是 ``StopIteration``。少了这一步,生成器任务失败时
        消费端会**永远**等下一个 chunk(见 ``task_failed`` 的注释)。
        """
        with self._cond:
            for generator in self._generators.values():
                if generator["task_id"] == task_id:
                    generator["done"] = True
                    if error is not None:
                        generator["error"] = error
            self._cond.notify_all()

    def get_generator_state(self, generator_id: str) -> Dict[str, Any]:
        with self._lock:
            generator = self._generators.get(generator_id)
            if generator is None:
                raise MiniRayError(f"未知的生成器 {generator_id[:8]}")
            return {
                "refs": list(generator["refs"]),
                "done": bool(generator["done"]),
                "error": generator.get("error"),
            }

    def wait_for_generator(
        self, worker_id: str, generator_id: str, index: int, timeout: float = 60.0
    ) -> Dict[str, Any]:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                generator = self._generators.get(generator_id)
                if generator is None:
                    break
                if len(generator["refs"]) > index or generator["done"]:
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(_POLL_TIMEOUT, remaining))
        return self.get_generator_state(generator_id)

    # ==================================================================== actor
    def create_actor(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """创建 actor:占资源 → 起进程 → 把 ``__init__`` 作为第一个任务投进邮箱。"""
        actor_id = ActorID.from_random().hex()
        options = spec.get("options", {})
        # actor 的资源配置:actor 默认占 1 CPU(除非显式给 0)。
        # ``memory`` / ``resources`` 此前被**丢掉**了 —— 第三个位置参数是
        # ``memory`` 而不是 CPU,传错位置会让 num_gpus>0 的 actor 凭空多要
        # 1.0 memory,而节点总内存是 0.0,于是「没有节点能放下这个 actor」。
        resources = Resources.from_request(
            options.get("num_cpus"),
            options.get("num_gpus"),
            options.get("memory"),
            options.get("resources"),
        )
        max_concurrency = int(options.get("max_concurrency", 1) or 1)
        groups = dict(options.get("concurrency_groups") or {})
        is_async = bool(options.get("is_async", False))
        if is_async and max_concurrency <= 1:
            # Ray 的默认值:asyncio actor 的默认并发度是 1000
            # (``DEFAULT_MAX_CONCURRENCY_ASYNC``)。
            #
            # ⚠️ 这里以前还有一个 ``and not groups`` —— 于是「声明了并发组的
            # async actor」默认并发度塌成 1:默认组的邮箱只有一个执行槽位,
            # 整个 actor 退化成串行(实测 8 次 0.4 秒的调用要 3.66 秒,而其实
            # 0.4 秒就能跑完),而显式写 ``max_concurrency=8`` 就正常 ——
            # 说明组容量机制本身是好的,坏的只是这个默认值。
            max_concurrency = 1000
        concurrency = {"": max_concurrency}
        for name, value in groups.items():
            concurrency[name] = int(value)

        # 校验:方法声明的并发组必须真的存在。
        # 不校验的话,投进「未声明的组」的任务会进一个**没有执行槽位的邮箱**,
        # 谁也取不到,``ray.get`` 永久挂起 —— 而且不报任何错,因为
        # 「没人来取」和「还在排队等资源」在调用方看来一模一样。
        method_metadata = dict(options.get("method_metadata") or {})
        unknown_groups = sorted(
            {
                str(meta.get("concurrency_group") or "")
                for meta in method_metadata.values()
                if str(meta.get("concurrency_group") or "") not in concurrency
            }
        )
        if unknown_groups:
            raise MiniRayError(
                f"actor 的方法声明了未定义的并发组 {unknown_groups};"
                f"可用的组是 {sorted(concurrency)}(用 concurrency_groups=... 声明)"
            )

        # ★ actor 的 placement constraint:和 ``submit_task`` 用**同一段代码**解析。
        #   ``ActorClass.options(scheduling_strategy=…)`` 已经把它编码成 dict
        #   (``{"kind": "node_affinity", "node_id": …}`` /
        #    ``{"kind": "placement_group", "placement_group_id": …, "bundle_index": …}``),
        #   以前 create_actor **从来没读过它** —— 策略被静默丢弃,于是:
        #   * ``NodeAffinitySchedulingStrategy`` 不生效;
        #   * 「先占资源、再往 bundle 里放 actor」这个经典模式**直接死锁**
        #     (actor 永远 PENDING,而它的资源被锁在自己不认的放置组里)。
        strategy = options.get("scheduling_strategy") or {}
        if not isinstance(strategy, dict):  # 兼容:直接把策略对象传进来的写法
            encoder = getattr(strategy, "_encode", None)
            strategy = encoder() if encoder is not None else {}
        raw_bundle_index = strategy.get("bundle_index", -1)
        bundle_index = -1 if raw_bundle_index is None else int(raw_bundle_index)

        record = TaskRecord(
            task_id=TaskID.from_random(),
            name=f"actor:{spec.get('class_name', 'Actor')}",
            result_ids=[],
            deps=set(),
            resources=resources,
            owner=spec.get("owner", ""),
            node_affinity=strategy.get("node_id"),
            placement_group_id=strategy.get("placement_group_id"),
            bundle_index=bundle_index,
            is_actor_task=True,
            actor_id=actor_id,
            max_retries=0,
        )
        with self._lock:
            # 可调度性检查:如果**没有任何节点**的总资源装得下,那就是永远等不到,
            # 这种情况立刻报错(否则用户会看到一个永远卡住的 actor)。
            # 注意判据是「总量」而不是「当前可用量」—— 和 Ray 一样:
            # 资源暂时被占满时 actor 会**排队等待**,不是失败。
            feasible = any(
                node.total.satisfies(resources) for node in self._scheduler.nodes.values()
            )
            if not feasible:
                raise ScheduleError(
                    f"没有节点能放下这个 actor(需要 {resources.to_dict()},"
                    f"而单节点最大只有 "
                    f"{max((n.total.to_dict() for n in self._scheduler.nodes.values()), key=lambda d: d.get('CPU', 0))})。"
                    "actor 的资源是终身持有的,请减少 actor 数量或调大 num_cpus/num_gpus"
                )
            node_id = self._scheduler.pick_node(record)
            if node_id is not None:
                # 立刻扣资源:actor 的资源是**终身持有**的,从创建到销毁
                self._scheduler.assign(record, node_id)
            creation_object_id = ObjectID.from_random().hex()
            self._object_states[creation_object_id] = {"state": "PENDING", "node_id": None}
            runtime = _ActorRuntime(
                actor_id=actor_id,
                info={
                    "actor_id": actor_id,
                    "class_name": spec.get("class_name", "Actor"),
                    "node_id": node_id or "",
                    "job_id": self.config.job_id,
                    "name": options.get("name"),
                    "namespace": options.get("namespace", self.config.namespace),
                    "lifetime": options.get("lifetime", "non_detached"),
                    "max_restarts": int(options.get("max_restarts", 0) or 0),
                    "max_task_retries": int(options.get("max_task_retries", 0) or 0),
                    "max_concurrency": max_concurrency,
                    "concurrency_groups": groups,
                    "resources": resources.to_dict(),
                    "state": "PENDING",
                    "creation_object_id": creation_object_id,
                    "method_groups": {},
                    "is_async": is_async,
                },
                spec=spec,
                record=record,
                node_id=node_id or "",
                concurrency=concurrency,
                method_metadata=dict(options.get("method_metadata") or {}),
                # actor 终身持有的 GPU 序号:要传给 actor 进程,否则
                # ``ray.get_gpu_ids()`` 返回空、``CUDA_VISIBLE_DEVICES``
                # 不设 —— actor 会看到机器上**全部**的卡(第 30 章的场景)。
                gpu_ids=list(record.gpu_ids),
                runtime_env=options.get("runtime_env"),
            )
            runtime.mailboxes = {group: deque() for group in concurrency}
            runtime.inflight = {group: 0 for group in concurrency}
            self._actors[actor_id] = runtime
        self.gcs.call("register_actor", info=runtime.info)
        self._record("actor_created", actor_id=actor_id, node_id=node_id, name=runtime.info["name"])
        if self.config.local_mode:
            # local_mode 下 actor 就是一个普通 Python 对象,活在 driver 进程里
            self._create_local_actor(runtime)
        elif node_id is not None:
            self._spawn_actor_worker(runtime)
        else:
            # 资源暂时不够:留在 PENDING,由调度循环里的
            # _process_pending_actors() 等资源空出来再拉起
            runtime.state = "PENDING"
            self._record("actor_waiting", actor_id=actor_id)
            self._schedule_needed.set()
        return {
            "actor_id": actor_id,
            "creation_object_id": creation_object_id,
            "node_id": node_id or "",
        }

    def _create_local_actor(self, runtime: _ActorRuntime) -> None:
        """``local_mode``:直接在 raylet(driver)线程里实例化 actor。"""
        descriptor = FunctionDescriptor.from_dict(runtime.spec["function"])
        object_id = runtime.info["creation_object_id"]
        try:
            actor_class = self._function_for_local(descriptor)
            deps = _extract_deps(runtime.spec["args"])
            args = serialization.loads(runtime.spec["args"], ref_values=self._local_fetch([d.hex() for d in deps]))
            args, kwargs = _unpack_args(args)
            # ★ actor 级 runtime_env / GPU 可见性:分布式路径在 ``_run_actor_worker``
            #   里把整个 actor(含 ``__init__``)包在这个 context 里;local_mode 是
            #   另一条实现路径,以前完全没碰 —— 于是 ``env_vars`` 静默丢失、
            #   ``ray.get_gpu_ids()`` 返回 ``[]``。这里至少让 ``__init__`` 看到它。
            with runtime_env_context(runtime.runtime_env, runtime.gpu_ids):
                instance = actor_class(*args, **kwargs)
        except Exception as exc:
            runtime.state = "DEAD"
            self._store_error_bytes(object_id, exc)
            self.gcs.call("update_actor", actor_id=runtime.actor_id, state="DEAD")
            return
        self._local_actors[runtime.actor_id] = instance
        runtime.state = "ALIVE"
        self._stores[self._driver_node].put_bytes(bytes.fromhex(object_id), serialization.dumps(None))
        self.gcs.call("update_actor", actor_id=runtime.actor_id, state="ALIVE")
        self._mark_object_ready(object_id, self._driver_node)

    def _store_error_bytes(self, object_id: str, error: BaseException) -> None:
        self._stores[self._driver_node].put_bytes(
            bytes.fromhex(object_id), serialization.dumps(error)
        )
        self._mark_object_ready(object_id, self._driver_node)

    def _spawn_actor_worker(self, runtime: _ActorRuntime) -> None:
        actor_id = runtime.actor_id
        worker_id = f"actor-{actor_id[:12]}-{runtime.num_restarts}"
        config = {
            "raylet_address": self.address,
            "gcs_address": self.gcs.address,
            "node_id": runtime.node_id,
            "worker_id": worker_id,
            "actor_id": actor_id,
            "job_id": self.config.job_id,
            "namespace": self.config.namespace,
            "session_dir": self.config.session_dir,
            "worker_type": "actor",
            "driver_node_id": self._driver_node,
            "gpu_ids": list(runtime.gpu_ids),
            "runtime_env": runtime.runtime_env,
            "actor_spec": {
                "function": runtime.spec["function"],
                "args": runtime.spec["args"],
                # ★ ``__init__`` 的参数里可能内联着 ObjectRef(``A.remote(some_ref)``
                #   是 Ray 里很常见的写法)。这些依赖必须**列给 worker**,它才知道
                #   去 raylet 把值取回来再反序列化 —— worker 侧读的就是
                #   ``spec["creation_deps"]``。以前这个字段从来没人写过,
                #   于是参数里的 ref 在反序列化时找不到值,actor 创建**必然失败**
                #   (而 local_mode 走的是另一条路、自己算了 deps,所以看不出来)。
                "creation_deps": [oid.hex() for oid in _extract_deps(runtime.spec["args"])],
                "method_metadata": runtime.method_metadata,
                "is_async": runtime.info["is_async"],
                "max_concurrency": runtime.info["max_concurrency"],
                "concurrency_groups": runtime.info["concurrency_groups"],
                "class_name": runtime.info["class_name"],
            },
        }
        with self._lock:
            slot = self._pool.spawn(
                runtime.node_id, config, worker_type="actor", actor_id=actor_id
            )
            runtime.slot = slot
            self._worker_nodes[slot.worker_id] = runtime.node_id
            self._worker_types[slot.worker_id] = "actor"
        self._record("worker_spawned", worker_id=slot.worker_id, node_id=runtime.node_id, pid=slot.pid, worker_type="actor")

    def register_actor_methods(self, actor_id: str, methods: Dict[str, Any]) -> None:
        with self._lock:
            runtime = self._actors.get(actor_id)
            if runtime is None:
                return
            runtime.method_metadata.update(methods)
            self.gcs.call("update_actor", actor_id=actor_id, method_groups=methods)

    def actor_poll(self, actor_id: str, worker_id: str, group: str = "") -> Dict[str, Any]:
        """actor worker 来取方法调用(长轮询)。"""
        runtime = self._actors.get(actor_id)
        if runtime is None:
            return {"kind": "shutdown", "reason": "actor 不存在"}
        with runtime.cond:
            deadline = time.time() + _POLL_TIMEOUT
            while True:
                if runtime.state == "DEAD":
                    return {"kind": "shutdown", "reason": "actor 已死亡"}
                mailbox = runtime.mailboxes.setdefault(group, deque())
                if mailbox and runtime.inflight.get(group, 0) < runtime.concurrency.get(group, 1):
                    task = mailbox.popleft()
                    runtime.inflight[group] = runtime.inflight.get(group, 0) + 1
                    runtime.running[task["task_id"]] = {"group": group, "task": task}
                    return {"kind": "actor_task", "task": task}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"kind": "noop"}
                runtime.cond.wait(timeout=min(0.2, remaining))

    def submit_actor_task(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """提交 actor 方法调用:进邮箱,等 actor worker 来取。"""
        actor_id = spec["actor_id"]
        with self._lock:
            runtime = self._actors.get(actor_id)
            if runtime is None or runtime.state == "DEAD":
                # actor 已经死了:把异常写进结果对象,让 ray.get 报错
                result_ids = [
                    ObjectID.from_random().hex() for _ in range(max(1, int(spec.get("num_returns", 1))))
                ]
                dead_error = ActorDiedError(
                    f"actor {actor_id[:8]}",
                    cause=Exception("actor 已经死亡(被 kill 或重启次数用尽)"),
                    traceback_str="",
                )
                for object_id in result_ids:
                    self._store_error(bytes.fromhex(object_id), dead_error)
                    self._mark_object_ready(object_id, self._driver_node)
                return {"result_ids": result_ids}
            result_ids = [
                ObjectID.from_random().hex() for _ in range(max(1, int(spec.get("num_returns", 1))))
            ]
            for object_id in result_ids:
                self._object_states[object_id] = {"state": "PENDING", "node_id": None}
            method = spec["method_name"]
            metadata = runtime.method_metadata.get(method, {})
            group = metadata.get("concurrency_group", "") or ""
            task_id = TaskID.from_random().hex()
            task = {
                "task_id": task_id,
                "method_name": method,
                "args": spec["args"],
                "num_returns": int(spec.get("num_returns", 1)),
                "result_ids": result_ids,
                "runtime_env": spec.get("runtime_env", {}),
                "group": group,
                "is_creation": method == "__init__",
                "actor_id": actor_id,
            }
            # ★ 登记「结果对象 → task」「task → actor」,这样 ray.cancel(ref) 才能
            #   找到它。不登记的话 cancel 查不到记录,静默什么都不做(F9)。
            self._actor_task_index[task_id] = actor_id
            for object_id in result_ids:
                self._object_tasks[object_id] = task_id
            if self.config.local_mode:
                # local_mode:方法调用**立刻**在调用线程里执行(等价于直接调对象方法)。
                # 执行完就在**同一个锁作用域**里注销登记 —— 它不可能再被 cancel,
                # 留着这条记录只会让 cancel 以为「还有个在途的 actor task」。
                self._execute_local_actor_task(actor_id, task)
                self._actor_task_index.pop(task_id, None)
                return {"result_ids": result_ids}
            runtime.mailboxes.setdefault(group, deque()).append(task)
            self._record("actor_task_submitted", actor_id=actor_id, method=method, group=group)
        with runtime.cond:
            runtime.cond.notify_all()
        return {"result_ids": result_ids}

    def _execute_local_actor_task(self, actor_id: str, task: Dict[str, Any]) -> None:
        result_ids = [bytes.fromhex(oid) for oid in task["result_ids"]]
        instance = self._local_actors.get(actor_id)
        actor_runtime = self._actors.get(actor_id)
        try:
            if instance is None:
                raise ActorDiedError(
                    f"actor {actor_id[:8]}", cause=Exception("actor 创建失败或已死亡"), traceback_str=""
                )
            deps = _extract_deps(task["args"])
            args = serialization.loads(task["args"], ref_values=self._local_fetch([d.hex() for d in deps]))
            args, kwargs = _unpack_args(args)
            # ★ 与分布式路径**同样的两层**:外层是 actor 级 runtime_env + GPU
            #   (``worker.py`` 的 ``_run_actor_worker``),内层是方法级 runtime_env
            #   (``_SyncActorContext.run`` / ``_AsyncActorContext._handle``)。
            #   嵌套的 contextmanager 各自保存/还原,所以两层可以安全叠加。
            method = getattr(instance, task["method_name"])
            with runtime_env_context(
                actor_runtime.runtime_env if actor_runtime is not None else None,
                actor_runtime.gpu_ids if actor_runtime is not None else None,
            ):
                with runtime_env_context(task.get("runtime_env")):
                    result = method(*args, **kwargs)
        except Exception as exc:
            error_object = RayActorError(
                task["method_name"], cause=exc, traceback_str=traceback.format_exc()
            )
            for object_id in result_ids:
                self._stores[self._driver_node].put_bytes(
                    object_id, serialization.dumps(error_object)
                )
                self._mark_object_ready(object_id.hex(), self._driver_node)
            return
        results = _normalize_results(result, len(result_ids))
        for object_id, value in zip(result_ids, results):
            self._stores[self._driver_node].put_bytes(object_id, serialization.dumps(value))
            self._mark_object_ready(object_id.hex(), self._driver_node)

    def _actor_task_finished(self, actor_id: str, task_id: str) -> None:
        with self._lock:
            self._actor_task_index.pop(task_id, None)
            runtime = self._actors.get(actor_id)
            if runtime is None:
                return
            info = runtime.running.pop(task_id, None)
            if info is None:
                return
            group = info["group"]
            runtime.inflight[group] = max(0, runtime.inflight.get(group, 0) - 1)
            if info["task"].get("is_creation"):
                # actor 的 __init__ 跑完了 → actor 进入 ALIVE
                runtime.state = "ALIVE"
                self.gcs.call("update_actor", actor_id=actor_id, state="ALIVE")
                self._record("actor_ready", actor_id=actor_id)
        with runtime.cond:
            runtime.cond.notify_all()

    def report_actor_creation(
        self, actor_id: str, *, ok: bool, error: Optional[Dict[str, Any]] = None
    ) -> None:
        """actor worker 汇报 ``__init__`` 的结果(失败的话第一个方法调用会抛出来)。"""
        with self._lock:
            runtime = self._actors.get(actor_id)
            if runtime is None:
                return
            object_id = runtime.info["creation_object_id"]
            # 状态推进:__init__ 成功 → ALIVE;失败 → CREATION_FAILED
            # (失败时**不**标 DEAD:要让后续方法调用把真实的 __init__ 异常抛给用户,
            #  而不是笼统的「actor 已死亡」—— Ray 也是这样)
            if ok:
                runtime.state = "ALIVE"
            else:
                runtime.state = "CREATION_FAILED"
        self.gcs.call("update_actor", actor_id=actor_id, state=runtime.state)
        creation_node = runtime.node_id or self._driver_node
        if ok:
            self._stores[creation_node].put_bytes(
                bytes.fromhex(object_id), serialization.dumps(None)
            )
        else:
            # 同一个节点:内容与对象目录必须一致(见 _store_error 的说明)
            self._store_error(
                bytes.fromhex(object_id), _error_from_payload(error or {}), creation_node
            )
        self._mark_object_ready(object_id, creation_node)
        self._record("actor_ready" if ok else "actor_creation_failed", actor_id=actor_id)

    def kill_actor(self, actor_id: str, *, no_restart: bool = True) -> None:
        """杀死 actor(``ray.kill``)。"""
        with self._lock:
            runtime = self._actors.get(actor_id)
            if runtime is None:
                return
            if no_restart:
                runtime.no_restart = True
            slot = runtime.slot
        self._record("actor_killed", actor_id=actor_id)
        if slot is not None:
            self._pool.retire(slot)
        self._handle_actor_death(actor_id, reason="被 ray.kill 杀死")

    def _handle_actor_death(self, actor_id: str, *, reason: str) -> None:
        """actor 进程死了:要么重启,要么彻底失败。"""
        with self._lock:
            runtime = self._actors.get(actor_id)
            if runtime is None:
                return
            max_restarts = int(runtime.info.get("max_restarts", 0) or 0)
            in_flight = list(runtime.running.values())
            runtime.running.clear()
            if runtime.no_restart or runtime.num_restarts >= max_restarts:
                runtime.state = "DEAD"
                runtime.slot = None
                self.gcs.call("update_actor", actor_id=actor_id, state="DEAD")
                self.gcs.call(
                    "unregister_named_actor",
                    name=runtime.info.get("name") or "",
                    namespace=runtime.info.get("namespace") or self.config.namespace,
                )
                release = True
            else:
                runtime.num_restarts += 1
                runtime.state = "RESTARTING"
                self.gcs.call(
                    "update_actor", actor_id=actor_id, state="RESTARTING", num_restarts=runtime.num_restarts
                )
                release = False
                for group in runtime.inflight:
                    runtime.inflight[group] = 0
        self._record("actor_died", actor_id=actor_id, reason=reason, restarts=runtime.num_restarts)

        # 在飞的方法调用:Ray 的语义是「失败」,除非配了 max_task_retries。
        #
        # ⚠️ 重投递的前提是**还会有一个活的 actor 进程来取这个邮箱**。
        # ``release=True`` 表示这个 actor 已经彻底 DEAD(被 kill / 重启次数用尽),
        # 不会再有进程来 poll —— 此时把任务塞回邮箱,它就**永久挂起**:
        # ``actor_poll`` 对 DEAD 的 actor 直接返回 shutdown,而任务在邮箱里
        # 谁都不取,``ray.get`` 既不超时也不报错。实测:max_task_retries=0 时
        # 立刻抛 ActorDiedError,max_task_retries=2 时永久挂起 —— 恰恰相反。
        max_task_retries = int(runtime.info.get("max_task_retries", 0) or 0)
        doomed = list(in_flight)
        if release:
            # 「在飞」只是「还没被取走」的一半。邮箱里**排队中**的调用同样如此:
            # actor 一死就再没有进程会来取它们,``ray.get`` 永久挂起。
            # 这是同一个洞的另一半,必须一起堵上 —— 只补在飞那半,用户换个
            # ``max_concurrency`` 或者多提交一个调用就又会撞上。
            with runtime.cond:
                for mailbox in runtime.mailboxes.values():
                    while mailbox:
                        item = mailbox.popleft()
                        if not item.get("is_creation"):
                            doomed.append({"group": item.get("group", ""), "task": item})
                runtime.cond.notify_all()

        for info in doomed:
            task = info["task"]
            if max_task_retries > 0 and not release:
                with runtime.cond:
                    runtime.mailboxes.setdefault(info["group"], deque()).appendleft(task)
                    runtime.cond.notify_all()
                continue
            error = ActorDiedError(
                f"actor {actor_id[:8]}",
                cause=Exception(f"actor 在方法 {task['method_name']} 执行期间死亡:{reason}"),
                traceback_str="",
            )
            for object_id in task["result_ids"]:
                self._store_error(bytes.fromhex(object_id), error, runtime.node_id)
                self._mark_object_ready(object_id, runtime.node_id)
            with self._lock:
                self._actor_task_index.pop(task["task_id"], None)

        if release:
            with self._lock:
                self._scheduler._release(runtime.record)  # noqa: SLF001
            return
        self._spawn_actor_worker(runtime)
        # 重启后要重新跑 __init__
        with runtime.cond:
            runtime.mailboxes.setdefault("", deque()).appendleft(
                {
                    "task_id": TaskID.from_random().hex(),
                    "method_name": "__init__",
                    "args": runtime.spec["args"],
                    "num_returns": 0,
                    "result_ids": [],
                    "runtime_env": {},
                    "group": "",
                    "is_creation": True,
                    "actor_id": actor_id,
                }
            )
            runtime.cond.notify_all()

    def get_actor_info(self, actor_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            runtime = self._actors.get(actor_id)
            return dict(runtime.info) if runtime else None

    # ==================================================================== worker 死亡
    def _on_worker_death(self, slot: WorkerSlot) -> None:
        """worker 进程死了(崩溃/被杀)。这是容错的关键路径。"""
        worker_id = slot.worker_id
        node_id = slot.node_id
        self._record("worker_died", worker_id=worker_id, node_id=node_id, worker_type=slot.worker_type)
        # 1) 释放它 pin 住的对象 + 归还它没提交的共享内存
        for store in self._stores.values():
            store.release_owner(worker_id)
        with self._lock:
            pending = [
                key for key, (owner, _) in self._pending_blocks.items() if owner == worker_id
            ]
            for key in pending:
                _, block = self._pending_blocks.pop(key)
                self._stores[node_id].release_block(block)
            self._ref_counts and [self._ref_counts.pop(oid, None) for oid in []]
            for object_id, owners in list(self._ref_counts.items()):
                if worker_id in owners:
                    owners.pop(worker_id, None)
                    raw = bytes.fromhex(object_id)
                    for store in self._stores.values():
                        if store.contains(raw):
                            store.set_ref_count(raw, sum(owners.values()))
        if slot.worker_type == "actor" and slot.actor_id:
            self._handle_actor_death(slot.actor_id, reason="actor worker 进程崩溃")
            return
        # 2) 正在跑的任务:当崩溃处理(换 worker 重跑)
        with self._lock:
            record = self._scheduler.tasks.get(slot.task_id) if slot.task_id else None
        if record is not None and record.state is TaskState.RUNNING:
            self.task_failed(
                record.task_id.hex(),
                worker_id,
                {
                    "type": "WorkerCrashedError",
                    "message": f"worker 进程(pid={slot.pid})在执行任务时退出",
                    "traceback": "",
                },
                crashed=True,
            )
        self._worker_nodes.pop(worker_id, None)
        self._worker_types.pop(worker_id, None)

    def _retire_idle_workers(self) -> None:
        for slot in self._pool.retire_idle():
            self._record("worker_retired", worker_id=slot.worker_id, node_id=slot.node_id)

    # ==================================================================== 血缘重建
    def _reconstruct_object(self, object_id: str) -> bool:
        """对象丢了 → 重新执行生产它的 task(递归重建它的依赖)。

        这就是 Ray 的 **lineage reconstruction**:对象存储不是持久化的,
        但「怎么算出来」这件事是可重放的。Ray 官方文档里专门有一节讲它,
        也是 Ray 相比「只做缓存」的系统的关键差别。

        返回 ``True`` 表示已经发起重建。
        """
        with self._lock:
            if object_id in self._reconstructing:
                return True  # 已经在重建了,避免循环
            entry = self._lineage.get(object_id)
            if entry is None:
                return False
            self._reconstructing.add(object_id)
            self._object_states[object_id] = {"state": "RECONSTRUCTING", "node_id": None}
            spec = entry["task_spec"]
            deps = _extract_deps(spec["args"])
        self.num_reconstructions += 1
        self._record("object_reconstruct_start", object_id=object_id)
        logger.info("开始重建对象 %s(重放任务 %s)", object_id[:8], entry["task_id"][:8])

        # 1) 先重建依赖(深度优先)
        for dep in deps:
            dep_hex = dep.hex()
            # 注意用 _is_available 而不是 state == "READY":
            # 已经判定丢失的依赖状态也是 READY,但它不能当输入用 ——
            # 必须尝试重放它,否则任务会拿到 ObjectLostError 当参数。
            if not self._is_available(dep_hex):
                if not self._reconstruct_object(dep_hex):
                    self._mark_lost(object_id, f"依赖对象 {dep_hex[:8]} 无法重建")
                    return False
        # 2) 重新提交生产它的 task(结果对象 ID 保持不变)
        with self._lock:
            original_task = self._scheduler.tasks.get(entry["task_id"])
            result_ids = list(original_task.result_ids) if original_task else []
        if not result_ids:
            # 原始 task 记录已经不在了(被清理):退化成「单对象重放」
            result_ids = [bytes.fromhex(object_id)]
        replay_spec = dict(spec)
        replay_spec["name"] = f"reconstruct::{spec.get('name', 'task')}"
        replay_spec["max_retries"] = 0
        with self._lock:
            task_id = TaskID.from_random()
            record = TaskRecord(
                task_id=task_id,
                name=replay_spec["name"],
                result_ids=result_ids,
                deps=set(deps),
                resources=Resources.from_request(
                    replay_spec.get("num_cpus"),
                    replay_spec.get("num_gpus"),
                    replay_spec.get("memory"),
                    replay_spec.get("resources"),
                ),
                owner=replay_spec.get("owner", ""),
                max_retries=0,
                lineage={"function": replay_spec["function"], "args": replay_spec["args"],
                         "spec": replay_spec, "runtime_env": {}, "num_returns": len(result_ids)},
            )
            for oid in result_ids:
                self._object_states[oid.hex()] = {"state": "PENDING", "node_id": None}
                self._lineage[oid.hex()] = {
                    "task_spec": replay_spec,
                    "task_id": task_id.hex(),
                    "node_id": None,
                }
            self._scheduler.add_task(record, ready_objects=self._ready_deps_of(record))
            self._mark_deps_ready(record)
            # 注意:守卫**不能**在这里释放。多个对象共享同一个依赖时,
            # 每个父对象都会来问一次「这个依赖重建了吗」——
            # 如果提交完就解锁,同一个对象会被重复重放好几次(实测能到 4 倍)。
            # 正确的释放时机是「对象真的就绪」或「确认丢失」,见下面两处 discard。
            self._schedule_needed.set()
        logger.info(
            "重建任务已提交: %s → 结果对象 %s",
            replay_spec["name"],
            [oid.hex()[:8] for oid in result_ids],
        )
        return True

    def _mark_lost(self, object_id: str, reason: str) -> None:
        """对象彻底找不回来了:往对象存储里写一个 :class:`ObjectLostError`。"""
        error = ObjectLostError(object_id, reason)
        self._store_error(bytes.fromhex(object_id), error)
        with self._cond:
            self._object_states[object_id] = {"state": "READY", "node_id": self._driver_node, "lost": True}
            self._reconstructing.discard(object_id)   # 确认丢失 → 解除守卫
            self._cond.notify_all()
        self._scheduler.mark_object_ready(bytes.fromhex(object_id), self._driver_node)
        self._record("object_lost", object_id=object_id, reason=reason)
        self._schedule_needed.set()

    def lose_objects(self, *, node_id: Optional[str] = None) -> Dict[str, Any]:
        """**故障注入**:模拟节点故障(该节点上的对象全丢)。

        真实 Ray 里对应的是「节点挂了」或者「对象被驱逐后 lineage 重建失败」。
        这里用来演示/测试 lineage 重建:对象丢了之后,系统会重跑生产它的 task。
        """
        lost: List[str] = []
        targets = [node_id] if node_id else list(self._stores)
        for target in targets:
            store = self._stores.get(target)
            if store is None:
                continue
            # 只收集**本节点**这一轮丢的对象(不要复用 lost,否则会重复处理
            # 上一个节点的对象,并且把重建过又丢掉的对象算两次)
            node_lost = [raw.hex() for raw in store.object_ids()]
            for object_id in node_lost:
                raw = bytes.fromhex(object_id)
                if store.contains(raw):
                    store.delete(raw)
            lost.extend(node_lost)
            # ★ 关键的一步:对象已经从存储里删了,但 _object_states 里还写着
            #   READY —— 必须**立刻**把状态改成不可用。
            #   否则「重建依赖」的判定会以为依赖还在(状态是 READY),
            #   于是直接重放任务,任务拿到的却是一个 ObjectLostError 占位对象,
            #   报出与本因无关的 `'ObjectLostError' object is not subscriptable`。
            with self._lock:
                for object_id in node_lost:
                    state = self._object_states.get(object_id)
                    if state and state.get("state") == "READY" and not state.get("lost"):
                        self._object_states[object_id] = {"state": "LOST", "node_id": None}
        self.gcs.call("remove_object_locations", lost, targets[0] if targets else "")

        reconstructed = 0
        for object_id in lost:
            if self._lineage.get(object_id):
                # 有血缘 → 重新执行产出它的任务,对象会「原地复活」(ID 不变)
                self._reconstruct_object(object_id)
                reconstructed += 1
            else:
                # 没有血缘(比如 ray.put 的对象)→ 找不回来了,让调用方明确报错
                self._mark_lost(
                    object_id,
                    "节点故障;该对象不是由任务产出的(没有 lineage),无法重建",
                )
        return {"lost": lost, "reconstructed": reconstructed}

    # ==================================================================== 放置组
    def create_placement_group(
        self, bundles: List[Dict[str, float]], strategy: str, name: Optional[str] = None
    ) -> str:
        strategy = (strategy or "PACK").upper()
        if strategy not in ("PACK", "SPREAD", "STRICT_PACK", "STRICT_SPREAD"):
            raise MiniRayError(f"不支持的放置组策略: {strategy}")
        pg_id = self.gcs.call("create_placement_group", bundles=bundles, strategy=strategy, name=name)
        # 就绪对象在**创建时**就分配好 ID:这样 pg.ready() 立刻就能返回一个 ObjectRef
        # (虽然它还不可用),调用方拿到 ref 之后再去等 —— 与 Ray 的语义一致。
        ready_id = ObjectID.from_random().hex()
        with self._lock:
            self._object_states[ready_id] = {"state": "PENDING", "node_id": None}
            self._pg_ready_objects[pg_id] = ready_id
        self.gcs.call("update_placement_group", pg_id=pg_id, ready_object_id=ready_id)
        self._record("placement_group_created", pg_id=pg_id, strategy=strategy)
        self._schedule_needed.set()
        return pg_id

    #: actor 等资源超过这个秒数就打一次告警
    _ACTOR_PENDING_WARN_SECONDS = 10.0

    def _process_pending_actors(self) -> None:
        """给「资源不够、还在排队」的 actor 找机会分配资源。

        Ray 里 actor 创建是**异步**的:``Actor.remote()`` 立刻返回句柄,
        资源不够时 actor 停在 PENDING,等资源空出来再真正创建。
        期间提交的方法调用会排在邮箱里,等 actor 起来后按顺序执行。
        """
        with self._lock:
            waiting = [
                runtime
                for runtime in self._actors.values()
                if runtime.state == "PENDING" and runtime.node_id == ""
            ]
        for runtime in waiting:
            with self._lock:
                node_id = self._scheduler.pick_node(runtime.record)
                if node_id is None:
                    # 等太久了 → 提醒一次。这是「资源被谁占着」类问题最难自查的地方:
                    # actor 创建是异步的,资源不够时它会静默排队,用户只看到"卡住"
                    waited = time.time() - runtime.record.submitted_at
                    if waited > self._ACTOR_PENDING_WARN_SECONDS and not runtime.pending_warned:
                        runtime.pending_warned = True
                        available = ",".join(
                            f"{k}={v}" for k, v in self._scheduler.available_resources().items()
                        )
                        logger.warning(
                            "actor %s(%s)已等待资源 %.0f 秒:需要 %s,当前可用 %s。"
                            "常见原因:actor 的资源是终身持有的,已创建的 actor 占满了资源 —— "
                            "不再需要的 actor 请用 ray.kill() 释放",
                            runtime.actor_id[:8],
                            runtime.info.get("class_name", "Actor"),
                            waited,
                            runtime.record.resources.to_dict(),
                            available or "无",
                        )
                    continue
                self._scheduler.assign(runtime.record, node_id)
                runtime.node_id = node_id
                runtime.info["node_id"] = node_id
                # ★ GPU 序号是**这一刻**才由 assign 填进 record 的(创建时资源不够,
                #   所以那时的 record.gpu_ids 还是空的)。runtime.gpu_ids 是当初
                #   从 record 拷的一份快照,这里必须重新取一次 —— 否则这个 actor
                #   进程收到的 gpu_ids 永远是 []:ray.get_gpu_ids() 返回空、
                #   CUDA_VISIBLE_DEVICES 不设,actor 看得见机器上全部的卡。
                runtime.gpu_ids = list(runtime.record.gpu_ids)
            self.gcs.call("update_actor", actor_id=runtime.actor_id, node_id=node_id)
            self._record("actor_allocated", actor_id=runtime.actor_id, node_id=node_id)
            self._spawn_actor_worker(runtime)

    def _process_pending_placement_groups(self) -> None:
        """尝试给还没分配成功的放置组找资源。

        Ray 里放置组是**会一直等**的:资源不够就 PENDING,等别的任务释放后再分配。
        这里每个 tick 都重试一次。
        """
        try:
            pending = self.gcs.call("get_pending_placement_groups")
        except Exception:  # pragma: no cover - GCS 已关闭
            return
        for pg in pending:
            bundles = [Resources.from_bundle(b) for b in pg["bundles"]]
            assignment = self._scheduler.allocate_placement_group(
                bundles, pg["strategy"], self.node_ids
            )
            if assignment is None:
                continue
            self._scheduler.reserve_placement_group(pg["placement_group_id"], assignment, bundles)
            # 放置组就绪对象:{bundle 下标: 节点}
            ready_id = pg.get("ready_object_id") or self._pg_ready_objects.get(
                pg["placement_group_id"]
            )
            if ready_id is None:  # pragma: no cover - 防御
                continue
            payload: Dict[int, str] = {}
            for node_id, indexes in assignment.items():
                for index in indexes:
                    payload[index] = node_id
            self._store_result(
                bytes.fromhex(ready_id),
                {"kind": "bytes", "data": serialization.dumps(payload)},
                self._driver_node,
            )
            self.gcs.call(
                "update_placement_group",
                pg_id=pg["placement_group_id"],
                state="CREATED",
                assignment=assignment,
            )
            self._mark_object_ready(ready_id, self._driver_node)
            self._record("placement_group_ready", pg_id=pg["placement_group_id"])
            self._schedule_needed.set()

    def remove_placement_group(self, placement_group_id: str) -> None:
        self._scheduler.free_placement_group(placement_group_id)
        self.gcs.call("remove_placement_group", pg_id=placement_group_id)
        self._record("placement_group_removed", pg_id=placement_group_id)
        self._schedule_needed.set()

    def placement_group_ready_object(self, placement_group_id: str) -> Optional[str]:
        with self._lock:
            return self._pg_ready_objects.get(placement_group_id)

    # ==================================================================== local mode
    def _execute_inline(
        self, record: TaskRecord, spec: Dict[str, Any], *, generator_id: Optional[str] = None
    ) -> None:
        """``local_mode``:在 raylet 线程里**直接执行**任务(不开进程)。

        这是 Ray 的 ``ray.init(local_mode=True)``,调试时极其有用:
        没有跨进程边界,断点、print、pdb 全都正常。代价是没有并行度、
        任务里的全局状态会影响 driver 自己。

        ⚠️ 两条「同一件事有两条实现路径」的坑都在这个函数里:

        1. **进入执行前必须把自己标成不可调度**(``begin_inline``)。否则任务在
           READY 队列里躺着,调度线程每隔 0.2 秒扫一次就能把它派给一个**新拉起的
           子进程**再跑一遍。快任务来不及中招(< 一个 tick),慢任务必中。
        2. **``runtime_env`` / ``gpu_ids`` 必须和分布式路径一样生效**。分布式路径
           在 ``worker.py`` 里用 ``runtime_env_context`` 包住函数体;这里是另一条
           路径,以前完全没碰 —— 于是 ``local_mode`` 下 ``env_vars`` 静默丢失、
           ``ray.get_gpu_ids()`` 返回 ``[]``。
        """
        from .function_manager import FunctionDescriptor

        descriptor = FunctionDescriptor.from_dict(spec["function"])
        result_ids = list(record.result_ids)
        # ★ 1) 立刻脱离就绪队列(见上面的说明)
        self._scheduler.begin_inline(record)
        # ★ 2) 分配 GPU 序号 —— 分布式路径由 ``assign`` 做,local_mode 没有 assign
        self._allocate_inline_gpus(record)
        runtime_env = spec.get("runtime_env")
        try:
            func = self._function_for_local(descriptor)
            deps = [oid.hex() for oid in record.deps]
            values = self._local_fetch(deps)
            args = serialization.loads(spec["args"], ref_values=values)
            args, kwargs = _unpack_args(args)
            # ★ 2) runtime_env / GPU 可见性:与 worker.py 里那条路径用同一个
            #    contextmanager,行为才对得上(退出时自动还原 os.environ)
            with runtime_env_context(runtime_env, record.gpu_ids):
                if generator_id is not None:
                    generator = func(*args, **kwargs)
                    for chunk in generator:
                        self.task_chunk(
                            record.task_id.hex(), generator_id, _encode_result(chunk), is_last=False
                        )
                    self._finish_generator(record.task_id.hex())
                    self._finish_inline(record)
                    return
                result = func(*args, **kwargs)
            results = _normalize_results(result, len(result_ids))
            for object_id, value in zip(result_ids, results):
                self._stores[self._driver_node].put_bytes(
                    object_id, serialization.dumps(value)
                )
                self._mark_object_ready(object_id.hex(), self._driver_node)
            self._finish_inline(record)
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
            if generator_id is not None:
                # ⚠️ 生成器要单独走一条路,和 task_failed 里那段注释是同一个道理:
                #
                # 1. ``record.result_ids`` 对生成器是**边产边追加**的,里面包含
                #    消费者已经取走的 chunk —— 按「写进每一个结果对象」处理会把
                #    拿到手的数据换成异常对象(实测:同一个 ref 第一次拿到 0、
                #    第二次拿到 RayTaskError)。
                # 2. 不标记 done 的话,消费端 ``for ref in gen`` 会一直等下一个
                #    chunk,而 ``wait_for_generator`` 到点只是**返回**不抛错 ——
                #    循环永远转下去。
                self._finish_generator(record.task_id.hex(), error=error)
                self._fail_inline(record, error)
                return
            result_ids = list(record.result_ids) or result_ids
            error_object = self._build_task_error(record, error, crashed=False)
            for object_id in result_ids:
                self._store_error(object_id, error_object)
                self._mark_object_ready(object_id.hex(), self._driver_node)
            self._fail_inline(record, error)

    def _allocate_inline_gpus(self, record: TaskRecord) -> None:
        """``local_mode``:给就地执行的任务分配 GPU 序号。

        分布式路径里 ``record.gpu_ids`` 是 ``assign`` 填的;local_mode **不走**
        ``assign``,所以不显式分配的话它永远是空列表 —— ``ray.get_gpu_ids()``
        返回 ``[]``、``CUDA_VISIBLE_DEVICES`` 不设,任务能看见机器上**全部**的卡
        (第 30 章那个场景)。分配走节点的公共池,跑完由
        :meth:`_release_inline_gpus` 还回去 —— 从哪来、回哪去。
        """
        if not record.resources.gpu or record.gpu_ids:
            return
        node = self._scheduler.nodes.get(self._driver_node)
        if node is None:  # pragma: no cover - 防御
            return
        try:
            record.gpu_ids = node.take_gpus(int(record.resources.gpu))
        except ScheduleError:
            # 卡不够:不分配(任务照跑,``get_gpu_ids()`` 返回空),
            # 但要说出来 —— 静默是最难查的那种失败。
            logger.warning(
                "local_mode:节点 %s 没有足够的空闲 GPU,任务 %s 将看不到 CUDA_VISIBLE_DEVICES",
                self._driver_node[:8],
                record.name,
            )

    def _release_inline_gpus(self, record: TaskRecord) -> None:
        """把 :meth:`_allocate_inline_gpus` 拿的卡还回节点公共池。

        不能指望 ``_release``:它按 ``record.node_id`` 找节点,而 inline 任务的
        ``record.node_id`` 是 ``None``(从没被 assign 过)——
        不还的话每跑一个 GPU 任务就永久少一张可见的卡。
        """
        if not record.gpu_ids:
            return
        node = self._scheduler.nodes.get(self._driver_node)
        if node is not None:
            node.give_back_gpus(record.gpu_ids)
        record.gpu_ids = []

    def _finish_inline(self, record: TaskRecord) -> None:
        """local_mode:任务在 raylet 线程里跑完了 —— 调度器也必须收尾。

        少了这一步,``record`` 会一直停在 READY 状态、留在 READY 队列里,
        下一个 tick 的 ``_schedule_tasks`` 把它当普通任务捞起来派给一个新
        worker —— 于是 **local_mode 下每个任务都执行两次**(一次在 driver
        进程、一次在被拉起的子进程里),副作用翻倍;而任务本身看起来完全正常,
        因为 driver 里那次的结果先写进了对象存储。
        """
        self._release_inline_gpus(record)
        self._scheduler.task_finished(record.task_id.hex())
        self._schedule_needed.set()

    def _fail_inline(self, record: TaskRecord, error: Dict[str, Any]) -> None:
        """local_mode:任务失败了,同样要在调度器里收尾。

        ``retryable=False`` 是必须的 —— 这里有个很容易踩的坑:local_mode 的任务
        **从来没走过 ``assign``**,所以 ``num_attempts`` 恒为 0,按
        「``num_attempts <= max_retries``」判断会得到「可以重试」,于是这条失败的
        任务又被派给一个新拉起的子进程跑,而且重试满 ``max_retries`` 次 ——
        实测:一个失败的函数体被调用了 5 次(1 次在 driver、4 次在子进程里),
        每次的异常都往同一个结果对象上写一遍。

        而且重试在这里没有意义:inline 那次已经**把异常写进了结果对象**,
        调用方看到的就是最终结果,再跑几遍也改变不了它(这正是 local_mode
        「一个进程里跑完」的语义)。真实 Ray 的 local_mode 也不重试。
        """
        self._release_inline_gpus(record)
        self._scheduler.task_failed(
            record.task_id.hex(), error.get("message", ""), retryable=False
        )
        self._schedule_needed.set()

    def _function_for_local(self, descriptor: FunctionDescriptor):
        from .function_manager import FunctionManager

        manager = getattr(self, "_local_function_manager", None)
        if manager is None:
            manager = FunctionManager(self.gcs)
            self._local_function_manager = manager
        return manager.get(descriptor)

    def _local_fetch(self, object_ids: Sequence[str]) -> Dict[bytes, Any]:
        """local_mode 下取依赖:直接在本地对象存储里找。"""
        from . import runtime

        worker = runtime.get_core_worker(create=False)
        values: Dict[bytes, Any] = {}
        for object_id in object_ids:
            raw = bytes.fromhex(object_id)
            for store in self._stores.values():
                if store.contains(raw):
                    if store.is_ndarray(raw):
                        values[raw] = store.get_ndarray(raw)
                    else:
                        data = store.get_bytes(raw)
                        from .object_store import _open_shm

                        values[raw] = serialization.loads(data)
                    break
        return values

    # ==================================================================== 状态
    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            objects = {}
            for node_id, store in self._stores.items():
                stats = store.stats()
                objects[node_id] = stats
            return {
                "job_id": self.config.job_id,
                "namespace": self.config.namespace,
                "address": self.address,
                "local_mode": self.config.local_mode,
                "uptime": time.time() - self._started_at,
                "nodes": [
                    {
                        "node_id": node_id,
                        "resources": self._scheduler.nodes[node_id].total.to_dict(),
                        "available": self._scheduler.nodes[node_id].available.to_dict(),
                        "utilization": round(self._scheduler.nodes[node_id].utilization, 4),
                    }
                    for node_id in self.node_ids
                ],
                "scheduler": self._scheduler.stats(),
                "worker_pool": self._pool.stats(),
                "object_store": {
                    "per_node": objects,
                    "total_objects": sum(s["num_objects"] for s in objects.values()),
                    "total_used_bytes": sum(s["used_bytes"] for s in objects.values()),
                },
                "actors": [
                    {
                        "actor_id": actor_id,
                        "state": runtime.state,
                        "node_id": runtime.node_id,
                        "num_restarts": runtime.num_restarts,
                        "inflight": dict(runtime.inflight),
                        "mailbox": {g: len(q) for g, q in runtime.mailboxes.items()},
                        "info": dict(runtime.info),
                    }
                    for actor_id, runtime in self._actors.items()
                ],
                "num_reconstructions": self.num_reconstructions,
                "objects_tracked": len(self._object_states),
            }

    def cluster_resources(self) -> Dict[str, float]:
        """``ray.cluster_resources()`` 的实现(CPU/GPU/自定义 + 每节点一条)。"""
        return self._scheduler.cluster_resources()

    def available_resources(self) -> Dict[str, float]:
        """``ray.available_resources()`` 的实现(当前还能用的资源)。"""
        return self._scheduler.available_resources()

    def list_tasks(self) -> List[Dict[str, Any]]:
        out = []
        for record in self._scheduler.tasks.values():
            out.append(
                {
                    "task_id": record.task_id.hex(),
                    "name": record.name,
                    "state": record.state.value,
                    "node_id": record.node_id,
                    "worker_id": record.worker_id,
                    "num_attempts": record.num_attempts,
                    "duration": record.duration(),
                    "is_actor_task": record.is_actor_task,
                    "error": record.error,
                }
            )
        return out

    def list_workers(self) -> List[Dict[str, Any]]:
        """worker 池里每个档位的状态(供 state API 使用)。"""
        return [slot.info() for slot in self._pool.all_slots()]

    def list_objects(self) -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            for object_id, state in self._object_states.items():
                out.append(
                    {
                        "object_id": object_id,
                        "state": state.get("state"),
                        "node_id": state.get("node_id"),
                        "produced_by": self._object_tasks.get(object_id),
                        "refs": sum(self._ref_counts.get(object_id, {}).values()),
                    }
                )
        return out

    def shutdown_raylet(self) -> None:
        self._stopping.set()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _extract_deps(args_bytes: bytes) -> List[bytes]:
    """从序列化后的参数里**扫出**对象引用(不反序列化,避免副作用)。

    实现见 :func:`miniray.serialization.extract_object_ids` —— 扫描 pickle 流里
    的 persistent id 占位符。比反序列化一遍再遍历对象图便宜得多,而且不会触发
    用户对象的 ``__setstate__`` 之类的副作用。
    """
    return serialization.extract_object_ids(args_bytes)


def _encode_result(value: Any) -> Dict[str, Any]:
    """把小结果编码成负载(大对象走共享内存的路径由 worker 负责)。"""
    return {"kind": "bytes", "data": serialization.dumps(value)}


# ---------------------------------------------------------------------------
# 与真实 Ray 的差异
#   * 真实 Raylet 是**每个节点一个 C++ 进程**;mini-ray 是所有逻辑节点的 raylet
#     都跑在 driver 进程里的一个 RPC 服务,通过 ``num_nodes`` 模拟多节点。
#   * 跨节点对象传输在真实 Ray 里走 ObjectManager(pull,可 RDMA/NCCL,2.50 起有
#     Ray Direct Transport);mini-ray 退化成一次进程序内拷贝(默认单节点下无差别)。
#   * 对象溢出:真实 Ray 有独立的 IO worker 进程池 + LRU + 最小溢出块(100MB);
#     mini-ray 在 raylet 线程里同步溢出。
#   * 任务重试的默认值:Ray 是 ``max_retries=3``(生成器任务为 0),mini-ray 一致。
#   * Ray 还有 worker 缓存、资源抢占、autoscaler 反馈、task 优先级队列;
#     mini-ray 都没有。
# ---------------------------------------------------------------------------
