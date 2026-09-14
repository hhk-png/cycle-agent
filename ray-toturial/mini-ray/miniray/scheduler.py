"""调度器 —— mini-ray 的「大脑」。

Ray 的调度分两层(和 YARN/Mesos 的经典结构一致):

* **GCS 侧**:全局的资源和放置组视图、actor 注册表(在这里是 ``gcs.py``);
* **raylet 侧**:本地调度器决定「这个 task 交给哪个 worker 跑」。
  真正的调度策略在 C++ 里(``src/ray/raylet/scheduling/policy/``),有
  hybrid / spread / random / node_affinity / node_label / bundle(PG)/
  topology_bundle 等实现。

mini-ray 把这两层压成一个 :class:`TaskScheduler`,但保留了三个关键概念:

1. **资源模型**:task 声明 ``num_cpus`` / ``num_gpus`` / 自定义资源,调度器只在
   资源装得下的节点上放它。0 CPU 的 task 可以无限并发(它不占资源)。
2. **依赖驱动**:task 的输入是 ObjectRef,只有所有依赖对象都就绪才进入
   ``READY``;对象就绪是一次「事件」,会唤醒一批等待它的 task。**没有轮询**。
3. **放置组预留**:放置组把一个 bundle 的资源**扣下来**,只有使用该 bundle 的
   task 能用 —— 这是 Ray 保证「N 个 actor 一定放得进同一批资源」的手段。

调度策略上 mini-ray 做了简化:默认策略是「有本地依赖优先 → 否则最空闲的节点」,
而 Ray 的 hybrid 策略用的是 ``scheduler_spread_threshold=0.5``(低于阈值就往上堆,
否则摊开)。差异写在文件末尾。
"""

from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .ids import NodeID, ObjectID, PlacementGroupID, TaskID

__all__ = [
    "Resources",
    "NodeResources",
    "TaskState",
    "TaskRecord",
    "TaskScheduler",
    "ScheduleError",
]

#: 默认调度策略
DEFAULT_SPREAD_THRESHOLD = 0.5


class ScheduleError(Exception):
    """调度阶段的错误(资源不足、放置组不存在……)。"""


# ---------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------


@dataclass
class Resources:
    """一坨资源。``custom`` 装自定义资源(如 ``{"TPU": 4}``)。

    用 float 而不是 int:Ray 允许 ``num_cpus=0.5`` 这种小数,做资源超卖/共享时有用。
    """

    cpu: float = 0.0
    gpu: float = 0.0
    memory: float = 0.0
    custom: Dict[str, float] = field(default_factory=dict)

    # ---------------------------------------------------------------- 构造
    @staticmethod
    def from_request(
        num_cpus: Optional[float] = None,
        num_gpus: Optional[float] = None,
        memory: Optional[float] = None,
        custom: Optional[Dict[str, float]] = None,
        *,
        is_task: bool = True,
    ) -> "Resources":
        """把 ``ray.remote(num_cpus=…)`` 里的声明变成资源需求。

        Ray 的默认值:task 默认占 1 个 CPU;actor 默认也占 1 个 CPU,
        但 actor 的资源是**终身持有**的(从创建到销毁),这一点由 raylet 保证。
        """
        cpu = 1.0 if num_cpus is None and is_task else (num_cpus or 0.0)
        return Resources(
            cpu=float(cpu),
            gpu=float(num_gpus or 0.0),
            memory=float(memory or 0.0),
            custom={k: float(v) for k, v in (custom or {}).items()},
        )

    @staticmethod
    def from_bundle(bundle: Dict[str, float]) -> "Resources":
        """放置组 bundle 用的 ``{"CPU": 2, "GPU": 1}`` 写法(键名大写,和 Ray 一致)。"""
        bundle = dict(bundle or {})
        return Resources(
            cpu=float(bundle.pop("CPU", 0.0)),
            gpu=float(bundle.pop("GPU", 0.0)),
            memory=float(bundle.pop("memory", 0.0)),
            custom={k: float(v) for k, v in bundle.items()},
        )

    # ---------------------------------------------------------------- 运算
    def to_dict(self) -> Dict[str, float]:
        out = {"CPU": self.cpu, "GPU": self.gpu}
        if self.memory:
            out["memory"] = self.memory
        out.update(self.custom)
        return {k: v for k, v in out.items() if v}

    def is_empty(self) -> bool:
        return not self.cpu and not self.gpu and not self.memory and not any(self.custom.values())

    def satisfies(self, need: "Resources") -> bool:
        """我够不够满足 ``need``?逐项比较,不含隐式约束。"""
        if self.cpu < need.cpu or self.gpu < need.gpu or self.memory < need.memory:
            return False
        for key, value in need.custom.items():
            if self.custom.get(key, 0.0) < value:
                return False
        return True

    def __add__(self, other: "Resources") -> "Resources":
        custom = dict(self.custom)
        for key, value in other.custom.items():
            custom[key] = custom.get(key, 0.0) + value
        return Resources(self.cpu + other.cpu, self.gpu + other.gpu, self.memory + other.memory, custom)

    def __sub__(self, other: "Resources") -> "Resources":
        custom = dict(self.custom)
        for key, value in other.custom.items():
            custom[key] = custom.get(key, 0.0) - value
        return Resources(self.cpu - other.cpu, self.gpu - other.gpu, self.memory - other.memory, custom)

    def copy(self) -> "Resources":
        return Resources(self.cpu, self.gpu, self.memory, dict(self.custom))


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------


@dataclass
class NodeResources:
    """一个节点的资源账本。

    .. code-block:: text

        total      总资源(来自 ray.init(num_cpus=…) / ray start --num-cpus=…)
        available  还能给「普通 task」用的(已扣掉正在跑的 + 放置组预留的)
        pg_free    放置组预留池:{pg_id: [bundle0 的剩余, bundle1 的剩余, …]}
        gpus_free  空闲 GPU 序号(用于分配 CUDA_VISIBLE_DEVICES)
    """

    node_id: str
    total: Resources
    available: Resources
    pg_free: Dict[str, List[Resources]] = field(default_factory=dict)
    pg_gpus: Dict[str, List[List[int]]] = field(default_factory=dict)
    gpus_free: List[int] = field(default_factory=list)

    @staticmethod
    def create(node_id: str, total: Resources) -> "NodeResources":
        return NodeResources(
            node_id=node_id,
            total=total,
            available=total.copy(),
            gpus_free=list(range(int(total.gpu))),
        )

    @property
    def utilization(self) -> float:
        """CPU 利用率(0~1)。Ray 的 hybrid 策略用它决定「堆还是摊」。"""
        if not self.total.cpu:
            return 0.0
        used = self.total.cpu - self.available.cpu
        return max(0.0, min(1.0, used / self.total.cpu))

    def take_gpus(self, count: int) -> List[int]:
        """分配 ``count`` 个 GPU 序号(取最小的若干个)。"""
        if count <= 0:
            return []
        if len(self.gpus_free) < count:
            raise ScheduleError(f"节点 {self.node_id} 没有足够的 GPU")
        taken = self.gpus_free[:count]
        del self.gpus_free[:count]
        return taken

    def give_back_gpus(self, ids: Sequence[int]) -> None:
        for gpu_id in ids:
            if gpu_id not in self.gpus_free:
                bisect.insort(self.gpus_free, gpu_id)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


class TaskState(str, Enum):
    """task 的生命周期状态。

    .. code-block:: text

        PENDING ──依赖就绪──▶ READY ──分到节点/worker──▶ RUNNING ──▶ FINISHED
           ▲                                          │
           └──────────── 重试(worker 挂了/抛异常) ◀──┘
                                                      └──▶ FAILED(重试用尽)

    Ray 里同样的状态机在 raylet 的 ``WorkStatus`` 与 GCS 的 task 表里各存一份;
    mini-ray 只有一份(raylet 进程内),因为控制面是单点的。
    """

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class TaskRecord:
    """调度器看到的 task。"""

    task_id: TaskID
    name: str
    #: 结果对象的 ID(顺序即返回顺序)
    result_ids: List[bytes]
    #: 依赖对象 ID
    deps: Set[bytes]
    resources: Resources
    #: 发起者(worker_id 字符串);driver 也是一个 owner
    owner: str
    #: 期望跑在哪个节点(硬约束):节点亲和 / 放置组 bundle
    node_affinity: Optional[str] = None
    placement_group_id: Optional[str] = None
    bundle_index: int = -1
    is_actor_task: bool = False
    actor_id: Optional[str] = None
    #: 生成器 task(num_returns="dynamic")
    is_generator: bool = False
    #: 允许的重试次数
    max_retries: int = 3
    #: 已经执行过几次
    num_attempts: int = 0
    state: TaskState = TaskState.PENDING
    #: 还差几个依赖
    missing_deps: int = 0
    #: 分到的节点 / worker
    node_id: Optional[str] = None
    worker_id: Optional[str] = None
    #: 分配到的 GPU 序号
    gpu_ids: List[int] = field(default_factory=list)
    #: 实际使用的放置组 bundle 下标(用于把资源还回**预留下**,而不是公共池)
    bundle_index_used: int = -1
    #: 时间戳(供 state API / timeline 使用)
    submitted_at: float = field(default_factory=time.time)
    scheduled_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    #: 取消标记
    cancel_requested: bool = False
    #: 「正在被 raylet 就地执行」(``local_mode``)。置真期间这个 task **不可调度** ——
    #: 见 :meth:`TaskScheduler.begin_inline`。
    inline_running: bool = False
    #: 资源是否已经还过一次(``_release`` 的幂等标记)
    released: bool = False
    #: 出错信息(重试用尽后)
    error: Optional[str] = None
    #: 该 task 的「血缘」:生产这些对象所需的全部信息(用于对象丢失后重建)
    lineage: Optional[Dict[str, Any]] = None

    def is_terminal(self) -> bool:
        return self.state in (TaskState.FINISHED, TaskState.FAILED, TaskState.CANCELLED)

    def duration(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return end - self.started_at


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class TaskScheduler:
    """决策 + 记账。**不做任何 I/O**(不起进程、不发消息),所以可以单独测。"""

    def __init__(
        self,
        nodes: Dict[str, NodeResources],
        *,
        spread_threshold: float = DEFAULT_SPREAD_THRESHOLD,
    ) -> None:
        self.nodes = nodes
        self.spread_threshold = spread_threshold
        self.tasks: Dict[str, TaskRecord] = {}
        #: 对象 → 所在节点集合(由 raylet 从 GCS 对象目录同步,用于本地性打分)
        self.object_locations: Dict[bytes, Set[str]] = {}
        #: 依赖 → 等它的 task
        self._waiting: Dict[bytes, Set[str]] = {}
        #: 就绪队列(FIFO;Ray 里也是按提交顺序 + 优先级)
        self._ready: List[str] = []
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 查询
    def node(self, node_id: str) -> NodeResources:
        return self.nodes[node_id]

    def ready_tasks(self) -> List[TaskRecord]:
        """还在 READY 队列里、**而且确实处于 READY 状态**的 task。

        ⚠️ 只按「id 在 ``_ready`` 里」筛是不够的 —— 一个 task 可能在没被
        ``assign`` 的情况下就已经结束了(local_mode 直接在 raylet 线程里跑完、
        或者被取消/判定失败)。那些 task 的 id 仍然留在队列里,下一轮调度会把
        它们**再捞起来跑一遍**:实测 local_mode 下每个任务都会执行两次,
        一次在 driver 进程、一次在被拉起的子进程里。状态过滤是最后一道闸门。

        ⚠️⚠️ **第二道闸门是 ``inline_running``**(见 :meth:`begin_inline`)。
        光有状态过滤还不够:local_mode 就地执行一个**慢**任务时(超过一个
        调度 tick,约 0.2 秒),record 直到 ``task_finished`` 之前都还停在
        ``READY``—— 于是调度线程在这一整个窗口里都能把它捞走、派给一个新
        拉起的子进程,任务体照样执行两次。只有把「正在 inline 执行」这件事
        显式记下来,这个窗口才关得上。
        """
        with self._lock:
            return [
                self.tasks[tid]
                for tid in self._ready
                if tid in self.tasks
                and self.tasks[tid].state is TaskState.READY
                and not self.tasks[tid].inline_running
            ]

    def begin_inline(self, record: TaskRecord) -> None:
        """把 task 标成「正在被 raylet 就地执行」,并**摘出就绪队列**。

        ``local_mode`` 下任务在 raylet 线程里直接跑,整个过程没有 ``assign``、
        也没有 worker —— 但它在调度器眼里必须**不可调度**。这一步要在
        ``add_task`` 之后、执行用户代码**之前**、并且**在同一个锁作用域里**做完:
        调度线程每个 tick 都会扫一遍就绪队列,中间任何一处留缝,任务就会被
        再派给一个子进程跑第二遍(副作用翻倍,而结果看起来完全正常)。

        幂等:重复调用直接返回(``submit_task`` 与 ``_execute_inline`` 都会调它,
        必须只算一次)。``num_attempts`` 在这里 +1:任务**真的执行了**一次,
        而 inline 路径从不走 ``assign``(那是唯一自增的地方),不记的话状态 API
        会显示 ``num_attempts=0`` —— 一个显然跑过的任务却写着「一次都没跑」。
        """
        with self._lock:
            if record.inline_running:
                return
            while record.task_id.hex() in self._ready:
                self._ready.remove(record.task_id.hex())
            record.inline_running = True
            record.num_attempts += 1

    def pending_tasks(self) -> List[TaskRecord]:
        return [t for t in self.tasks.values() if t.state is TaskState.PENDING]

    def running_tasks(self) -> List[TaskRecord]:
        return [t for t in self.tasks.values() if t.state is TaskState.RUNNING]

    # ---------------------------------------------------------------- 增删
    def add_task(self, record: TaskRecord, *, ready_objects: Optional[Set[bytes]] = None) -> None:
        """登记一个 task。

        ``ready_objects`` 是**提交这一刻已经就绪**的依赖对象集合。这一步看着
        不起眼,少了它就会出现一类很难查的挂起:把已有的 ObjectRef 当参数传给
        新任务时(``f.remote(existing_ref)`` 是 Ray 里最常见的写法之一),
        如果只按「依赖个数」计数,那个依赖永远不会再触发一次「就绪」事件,
        任务就永远停在 PENDING —— 而它其实早就该跑了。
        """
        ready_objects = ready_objects or set()
        with self._lock:
            self.tasks[record.task_id.hex()] = record
            missing = record.deps - ready_objects
            record.missing_deps = len(missing)
            for dep in missing:
                self._waiting.setdefault(dep, set()).add(record.task_id.hex())
            if record.missing_deps == 0:
                self._make_ready(record)

    def remove_task(self, task_id: str) -> None:
        with self._lock:
            record = self.tasks.pop(task_id, None)
            if record is None:
                return
            if task_id in self._ready:
                self._ready.remove(task_id)
            for dep in record.deps:
                waiters = self._waiting.get(dep)
                if waiters:
                    waiters.discard(task_id)

    def _make_ready(self, record: TaskRecord) -> None:
        record.state = TaskState.READY
        record.scheduled_at = time.time()
        # 重新变回「可调度」:上一次的 inline 执行标记必须清掉,
        # 否则一个被重试的任务会永远被 ready_tasks() 过滤掉。
        record.inline_running = False
        self._enqueue_ready(record.task_id.hex())

    def _enqueue_ready(self, task_id: str) -> None:
        """把 task 放回 READY 队列(**先摘掉旧条目**,保证同一 id 只出现一次)。

        重复入队是会真的发生的:调度阶段失败的任务(比如节点亲和指向一个
        不存在的节点)并没有被 ``assign`` 消费掉队列里的那条记录,重试路径
        再 append 一次,队列里就有两条 —— 下一轮两条都失败、都再 append,
        队列长度每 0.2 秒翻一倍(实测 2 秒从 1 涨到 6 万),同时 ``ray.get``
        永远等不到结果。
        """
        while task_id in self._ready:
            self._ready.remove(task_id)
        self._ready.append(task_id)

    # ------------------------------------------------------- 事件:对象就绪
    def mark_object_ready(self, object_id: bytes, node_id: Optional[str] = None) -> List[TaskRecord]:
        """一个对象就绪了 —— 唤醒等它的 task。返回变成 READY 的那些。

        这是调度器里最重要的一个函数:整个依赖图靠它「流动」起来。
        """
        newly_ready: List[TaskRecord] = []
        with self._lock:
            for task_hex in list(self._waiting.pop(object_id, ())):
                record = self.tasks.get(task_hex)
                if record is None or record.state is not TaskState.PENDING:
                    continue
                record.missing_deps -= 1
                if record.missing_deps <= 0:
                    newly_ready.append(record)
            for record in newly_ready:
                self._make_ready(record)
        return newly_ready

    def object_dep_count(self, object_id: bytes) -> int:
        return len(self._waiting.get(object_id, ()))

    # ------------------------------------------------------- 事件:任务结束
    def task_finished(self, task_id: str) -> None:
        with self._lock:
            record = self.tasks.get(task_id)
            if record is None:
                return
            record.state = TaskState.FINISHED
            record.finished_at = time.time()
            record.inline_running = False
            if task_id in self._ready:
                self._ready.remove(task_id)
            self._release(record)

    def task_failed(self, task_id: str, error: str, *, retryable: bool = True) -> bool:
        """任务失败。返回 True 表示「可以重试」(调用方负责重新入队)。

        :param retryable: ``False`` 表示这次失败**与执行无关**(典型的例子是
            调度阶段就失败了 —— 节点亲和指向一个不存在的节点)。这种失败重试
            多少次都不会成功,而且 ``num_attempts`` 永远不会自增,所以
            「``num_attempts <= max_retries``」这个判据会恒为真、无限重试下去。
            调度类的失败必须显式排除在重试之外。
        """
        with self._lock:
            record = self.tasks.get(task_id)
            if record is None:
                return False
            self._release(record)
            record.error = error
            record.worker_id = None
            record.gpu_ids = []
            if retryable and record.num_attempts <= record.max_retries and not record.cancel_requested:
                return True
            record.state = TaskState.FAILED
            record.finished_at = time.time()
            record.inline_running = False
            if task_id in self._ready:
                self._ready.remove(task_id)
            return False

    def mark_cancelled(self, task_id: str) -> None:
        """取消一个还没开始跑的 task。

        必须把它从 READY 队列里摘掉 —— 否则下一轮调度还会把它捞起来执行
        (这是「取消」这类操作最容易漏的一步:状态改了,队列没改)。
        """
        with self._lock:
            record = self.tasks.get(task_id)
            if record is None:
                return
            record.state = TaskState.CANCELLED
            record.cancel_requested = True
            record.finished_at = time.time()
            if task_id in self._ready:
                self._ready.remove(task_id)
            self._release(record)

    def _release(self, record: TaskRecord) -> None:
        """把 task 占的资源还回去。

        两个细节都是踩坑来的:

        1. **放置组任务的资源要还回该 bundle 的预留下**,不是公共池。
           否则第一轮跑完,资源进了公共池而 bundle 里显示「已用满」,
           同一组里的第二轮任务就永远排不上 —— 表现为「放置组里的任务只能跑一轮」。
        2. **故意不清空** ``record.node_id``:它记录「这个任务最后跑在哪」,
           state API 与 timeline 都要用(下一次 assign 会覆盖它)。

        注意:GPU 序号从哪里来、回哪里去,必须和 :meth:`assign` **成对** ——
        放置组的任务从该 bundle 的 GPU 池(``pg_gpus[pg_id][index]``)取,归还时
        也回**同一个** bundle 池。这条以前是错的:reserve 时把卡从公共池
        (``gpus_free``)挪进了 bundle 池,assign 却去**公共池**取(此时已经空了,
        ``take_gpus`` 直接抛 ``ScheduleError``),而 _release 又把卡还回公共池 ——
        于是「放置组里的 GPU 任务永远跑不起来」,而且第一次失败的 assign 还会把
        bundle 的 GPU 永久扣掉(``pg_free`` 变成 ``{}``),之后连节点都选不出来。

        3. **必须幂等**。``_release`` 有四个调用方(``task_finished`` /
           ``task_failed`` / ``mark_cancelled`` / raylet 的 worker 回收),
           而它们可能在两个线程里同时到达同一个 task:``ray.cancel(force=True)``
           先 ``mark_cancelled``(归还一次)再杀 worker,worker 临死前报的
           ``task_done`` 会在另一个 RPC 线程里再归还一次。多还一次资源的后果是
           ``available_resources()`` 能**大于** ``cluster_resources()`` ——
           之后每一次调度决策都建立在错误的账本上。
           (``record.node_id`` 是故意不清空的,所以不能拿它当「还没还」的判据。)
        """
        if record.released:
            return
        record.released = True
        node = self.nodes.get(record.node_id or "")
        if node is not None:
            pg_id = record.placement_group_id
            returned_to_bundle = False
            if pg_id is not None and record.bundle_index_used >= 0:
                bundles = node.pg_free.get(pg_id)
                if bundles is not None and record.bundle_index_used < len(bundles):
                    index = record.bundle_index_used
                    bundles[index] = bundles[index] + record.resources
                    returned_to_bundle = True
                    # GPU 必须回到**取它的那个 bundle 池**(与 assign 成对);
                    # 还回公共池会让 bundle 池越用越少,第二次就跑不起来了。
                    if record.gpu_ids:
                        pool = node.pg_gpus.setdefault(pg_id, [])
                        while len(pool) <= index:
                            pool.append([])
                        pool[index].extend(record.gpu_ids)
                        record.gpu_ids = []
            if not returned_to_bundle:
                node.available = node.available + record.resources
                if record.gpu_ids:
                    node.give_back_gpus(record.gpu_ids)
                    record.gpu_ids = []
        record.bundle_index_used = -1
        record.worker_id = None

    # ------------------------------------------------------------ 调度决策
    def pick_node(self, record: TaskRecord) -> Optional[str]:
        """给 task 选一个节点;选不出来返回 ``None``(等资源或等放置组)。

        优先级:

        1. **硬约束**(节点亲和 / 放置组 bundle)—— 不满足就只能等;
        2. **依赖本地性** —— 依赖对象就在这个节点上,跑过去省一次跨节点传输;
        3. **利用率** —— 按 Ray 的 hybrid 思路:最好的节点利用率低于
           ``spread_threshold`` 就往它上面堆(pack),否则挑最空的(spread)。

        真实 Ray 还考虑了「关键路径上的依赖」「top-k 随机化以避免惊群」等,
        见调度策略目录下的 ``hybrid_scheduling_policy.cc``。
        """
        # 1) 放置组:资源必须从该 bundle 的预留下出
        if record.placement_group_id is not None:
            return self._pick_pg_node(record)

        candidates: List[Tuple[float, str]] = []
        for node_id, node in self.nodes.items():
            if record.node_affinity is not None and node_id != record.node_affinity:
                continue
            if not node.available.satisfies(record.resources):
                continue
            locality = self._locality_score(record, node_id)
            candidates.append((locality, node_id))
        if not candidates:
            # 节点亲和是硬约束:等不到就永远等不到,报错比死等好
            if record.node_affinity is not None:
                raise ScheduleError(
                    f"task {record.name} 指定了节点亲和 {record.node_affinity},"
                    f"但该节点资源不足或不存在"
                )
            return None

        # 2) 有本地依赖的优先
        best_locality = max(score for score, _ in candidates)
        if best_locality > 0:
            local = [nid for score, nid in candidates if score == best_locality]
            return min(local)

        # 3) 最空闲的节点(pack/spread 的统一表达)
        return min(candidates, key=lambda item: (self.nodes[item[1]].utilization, item[1]))[1]

    def _pick_pg_node(self, record: TaskRecord) -> Optional[str]:
        pg_id = record.placement_group_id or ""
        bundle = record.bundle_index
        for node_id, node in self.nodes.items():
            bundles = node.pg_free.get(pg_id)
            if not bundles:
                continue
            indexes = range(len(bundles)) if bundle < 0 else [bundle]
            for index in indexes:
                if index >= len(bundles):
                    continue
                if bundles[index].satisfies(record.resources):
                    return node_id
        return None

    def _locality_score(self, record: TaskRecord, node_id: str) -> float:
        """该节点上「有多少个依赖对象」。raylet 会把对象位置喂进来。"""
        return float(sum(1 for dep in record.deps if node_id in self.object_locations.get(dep, ())))

    def assign(self, record: TaskRecord, node_id: str, bundle_index: Optional[int] = None) -> None:
        """把 task 落到节点上,扣资源。

        **必须原子**:任何一步抛异常都不能留下「账已经改了、任务却没拿到资源」
        的脏状态。原来的写法是先减 ``pg_free`` 再去公共池取 GPU —— 公共池此时
        已经空了(reserve 时把卡挪进了 bundle 池),于是 ``take_gpus`` 抛
        ``ScheduleError``,而 bundle 的 GPU 已经**永久扣掉**了(``pg_free`` 里的
        bundle 变成 ``{}``):这个放置组之后连节点都选不出来,表现为永久挂起。
        现在改成**先算 GPU 序号(可能抛),算出来了再改账本**。
        """
        with self._lock:
            # ★ 第二道闸门(F1):正在被 raylet 就地执行的 task 绝不能再被派出去。
            #   第一道是 ``ready_tasks()`` 的状态过滤;这里是兜底 ——
            #   万一有一条路径绕过了队列扫描(比如直接调 assign),也不能把
            #   一个正在 inline 跑的任务塞给 worker 跑第二遍。
            if record.inline_running:
                raise ScheduleError(
                    f"task {record.name} 正在被就地执行(local_mode),不能重复分配"
                )
            node = self.nodes[node_id]
            if record.placement_group_id is not None:
                pg_id = record.placement_group_id
                bundles = node.pg_free.get(pg_id)
                if bundles is None:
                    raise ScheduleError(
                        f"放置组 {pg_id[:8]} 在这个节点上没有预留资源,任务 {record.name} 无法落上去"
                    )
                index = bundle_index if bundle_index is not None and bundle_index >= 0 else self._pg_index_for(
                    node, pg_id, record.resources
                )
                # 1) 先取 GPU:从**这个 bundle 自己的**池里取,取不到就抛 ——
                #    此时账本还没动,调用方可以安全地下次重试。
                gpu_ids: List[int] = []
                if record.resources.gpu:
                    gpu_ids = self._take_pg_gpus(node, pg_id, index, int(record.resources.gpu))
                # 2) GPU 到手了,再改账本
                bundles[index] = bundles[index] - record.resources
                record.bundle_index_used = index
                record.gpu_ids = gpu_ids
            else:
                node.available = node.available - record.resources
                record.gpu_ids = node.take_gpus(int(record.resources.gpu))
            if record.task_id.hex() in self._ready:
                self._ready.remove(record.task_id.hex())
            record.node_id = node_id
            record.state = TaskState.RUNNING
            record.started_at = record.started_at or time.time()
            record.num_attempts += 1
            # 重新分配 = 重新持有资源,所以要把 ``_release`` 的幂等标记清掉 ——
            # 否则「失败重试」第二次跑完时,归还会被当成重复调用而跳过,资源泄漏。
            record.released = False

    def _take_pg_gpus(
        self, node: NodeResources, pg_id: str, index: int, count: int
    ) -> List[int]:
        """从某个 bundle 的 GPU 池里取 ``count`` 个卡号(与 reserve 成对)。

        reserve 时 bundle 声明的 GPU 是从节点公共池挪进 ``pg_gpus`` 的,所以
        使用 bundle 的 task 只能从这里取 —— 去公共池取必然取不到(卡已经被挪走),
        而失败一次就会把 bundle 的账扣坏。
        """
        pool = node.pg_gpus.setdefault(pg_id, [])
        while len(pool) <= index:
            pool.append([])
        bundle_gpus = pool[index]
        if len(bundle_gpus) < count:
            raise ScheduleError(
                f"放置组 {pg_id[:8]} 的 bundle {index} 里只有 {len(bundle_gpus)} 张 GPU,"
                f"但任务需要 {count} 张(预留时声明的数量不够)"
            )
        taken = bundle_gpus[:count]
        del bundle_gpus[:count]
        return taken

    def _pg_index_for(self, node: NodeResources, pg_id: str, need: Resources) -> int:
        bundles = node.pg_free[pg_id]
        for index, bundle in enumerate(bundles):
            if bundle.satisfies(need):
                return index
        raise ScheduleError(f"放置组 {pg_id[:8]} 在节点 {node.node_id} 上没有装得下的 bundle")

    # ------------------------------------------------------------ 放置组支持
    def allocate_placement_group(
        self, bundles: List[Resources], strategy: str, nodes: Iterable[str]
    ) -> Optional[Dict[str, List[int]]]:
        """尝试在集群里为放置组找到资源。成功返回 ``{node_id: [bundle 下标…]}``。

        策略语义(和 Ray 一致):

        * ``STRICT_PACK``:全部 bundle 必须在**同一个节点**;
        * ``PACK``:尽量少用节点(装不下才换节点);
        * ``STRICT_SPREAD``:每个 bundle 一个**不同**的节点;
        * ``SPREAD``:尽量摊开,但允许两个 bundle 落在同一节点。

        实现上先在**副本**上试算(模拟扣资源),成功后再由
        :meth:`reserve_placement_group` 真正扣 —— 这样失败时集群状态不变,
        不会出现「分配到一半」的脏状态。
        """
        node_list = [n for n in nodes if n in self.nodes]
        if not node_list:
            return None
        with self._lock:
            sim: Dict[str, Resources] = {nid: self.nodes[nid].available.copy() for nid in node_list}

            if strategy == "STRICT_PACK":
                for node_id in node_list:
                    need = Resources()
                    for bundle in bundles:
                        need = need + bundle
                    if sim[node_id].satisfies(need):
                        return {node_id: list(range(len(bundles)))}
                return None

            if strategy == "STRICT_SPREAD":
                if len(node_list) < len(bundles):
                    return None
                assignment: Dict[str, List[int]] = {}
                for index, bundle in enumerate(bundles):
                    for node_id in node_list:
                        if node_id in assignment:
                            continue
                        if sim[node_id].satisfies(bundle):
                            sim[node_id] = sim[node_id] - bundle
                            assignment[node_id] = [index]
                            break
                    else:
                        return None
                return assignment

            # PACK / SPREAD:逐个 bundle 贪心
            prefer_used = strategy == "PACK"
            assignment = {}
            for index, bundle in enumerate(bundles):
                ordered = sorted(
                    node_list,
                    key=lambda nid: (
                        (0 if nid in assignment else 1)
                        if prefer_used
                        else (1 if nid in assignment else 0),
                        sim[nid].to_dict().get("CPU", 0.0) * -1,
                        nid,
                    ),
                )
                for node_id in ordered:
                    if sim[node_id].satisfies(bundle):
                        sim[node_id] = sim[node_id] - bundle
                        assignment.setdefault(node_id, []).append(index)
                        break
                else:
                    return None
            return assignment

    def reserve_placement_group(
        self, pg_id: str, assignment: Dict[str, List[int]], bundles: List[Resources]
    ) -> None:
        """真正扣下资源,建立放置组的预留池(pg_free / pg_gpus)。

        GPU 在这里从节点公共池挪进 ``pg_gpus[pg_id][index]`` —— 使用该 bundle 的
        task 只会从那里取(见 :meth:`assign`),这是一个**成对**的约定,
        改动其中一半必然会坏(历史 bug:只在 reserve 这半边挪了卡)。
        """
        with self._lock:
            for node_id, indexes in assignment.items():
                node = self.nodes[node_id]
                pool = node.pg_free.setdefault(pg_id, [])
                gpu_pool = node.pg_gpus.setdefault(pg_id, [])
                for index in indexes:
                    bundle = bundles[index]
                    # 先取 GPU(可能抛)再改账本,保持原子
                    taken_gpus = node.take_gpus(int(bundle.gpu)) if bundle.gpu else []
                    while len(pool) <= index:
                        pool.append(Resources())
                        gpu_pool.append([])
                    pool[index] = bundle
                    node.available = node.available - bundle
                    gpu_pool[index].extend(taken_gpus)

    def free_placement_group(self, pg_id: str) -> None:
        """删除放置组:把**还没被用掉**的预留资源还给节点。

        GPU 要还回**公共池**(``gpus_free``):reserve 时 bundle 声明的卡是从
        公共池挪进 ``pg_gpus`` 的,使用 bundle 的 task 归还时又放回了 bundle 池
        (见 :meth:`_release`),所以删除放置组时这个池里剩下的卡必须还给公共池 ——
        否则每建一次放置组,节点就永久少几张可见的卡(``take_gpus`` 之后报
        「没有足够的 GPU」,而 ``available_resources()`` 里 GPU 却是满的)。
        """
        with self._lock:
            for node in self.nodes.values():
                for bundle in node.pg_free.pop(pg_id, []):
                    node.available = node.available + bundle
                for gpu_pool in node.pg_gpus.pop(pg_id, None) or []:
                    node.give_back_gpus(gpu_pool)

    # ---------------------------------------------------------------- 观测
    def cluster_resources(self) -> Dict[str, float]:
        """对齐 ``ray.cluster_resources()`` 的形状:CPU/GPU + 每个节点一条。"""
        total = Resources()
        out: Dict[str, float] = {}
        for node_id, node in self.nodes.items():
            total = total + node.total
            out[f"node:{node_id[:8]}"] = 1.0
        out.update(total.to_dict())
        return out

    def available_resources(self) -> Dict[str, float]:
        """当前可用资源。

        ``to_dict()`` 会把值为 0 的项过滤掉,但调用方常常直接
        ``available_resources()["CPU"]`` —— 资源被占满时那个 KeyError 非常讨厌。
        所以这里**补上集群里存在、但当前为 0 的键**(Ray 也是这个行为)。
        """
        total = Resources()
        for node in self.nodes.values():
            total = total + node.available
        out = total.to_dict()
        for key, value in self.cluster_resources().items():
            if key.startswith("node:") or key == "object_store_memory":
                continue
            out.setdefault(key, 0.0)
        return out

    def stats(self) -> Dict[str, Any]:
        by_state: Dict[str, int] = {}
        for record in self.tasks.values():
            by_state[record.state.value] = by_state.get(record.state.value, 0) + 1
        return {
            "num_tasks": len(self.tasks),
            "by_state": by_state,
            "ready_queue": len(self._ready),
            "nodes": {
                node_id: {
                    "total": node.total.to_dict(),
                    "available": node.available.to_dict(),
                    "utilization": round(node.utilization, 4),
                }
                for node_id, node in self.nodes.items()
            },
        }


# ---------------------------------------------------------------------------
# 与真实 Ray 的差异
#   * Ray 有两层调度(GCS 管全局、raylet 管本地)与多个策略实现;mini-ray 是
#     单层单策略(依赖本地性 → 最空闲),`spread_threshold` 只是「记录」不参与
#     决策(真实 Ray 的 hybrid 用它决定 pack/spread)。
#   * Ray 的调度是**基于 lease 的**(worker 向 raylet 申请租约,可以拒绝);这里是
#     直接指派。
#   * Ray 支持资源组/标签(label)/拓扑感知调度(2.56 起 GPU domain aware、
#     2.57 起 topology-aware 公开 API);mini-ray 只有 CPU/GPU/自定义资源 + 节点亲和。
#   * Ray 的调度队列有优先级、抢占、autoscaler 反馈回路;mini-ray 没有。
# ---------------------------------------------------------------------------
