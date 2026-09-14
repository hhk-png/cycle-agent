"""Worker 池 —— 进程的创建、复用、退休、回收。

Ray 的 worker 是**按需拉起、复用的 OS 进程**(不是线程,也不是协程)。
为什么必须是进程?因为 task 可能崩溃、可能改全局状态、可能要独占 GPU,
进程之间的隔离是硬要求 —— 这也是 Ray 和 asyncio 方案的本质区别。

mini-ray 的 worker 生命周期:

.. code-block:: text

   空闲 ──有 task──▶ STARTING(进程启动 + 连上 raylet)
                        │
                        ▼
                      IDLE ◀── 干完活,回来等下一个 task
                        │           │
                  分到 task         │ 空闲超过 worker_idle_timeout
                        ▼           ▼
                      BUSY       SHUTDOWN(进程退出,资源还给节点)
                        │
                   进程死了 ──▶ DEAD(raylet 回收它的资源/引用/任务)

「按需拉起」的阈值由 ``max_workers_per_node`` 决定:Ray 的默认值是节点的
CPU 数乘以一个系数(``num_workers_soft_limit``),超过之后 task 只能排队。
注意一个**反直觉**的点:0 CPU 的 task 不占资源,所以理论上可以无限并发 ——
限制它的是 worker 池大小。Ray 里也一样,这是很多「我的并发上不去」问题的根因。

.. rubric:: 为什么用 ``subprocess`` 而不是 ``multiprocessing``?

这是一个**踩过才知道**的坑,也是真实 Ray 的做法:

``multiprocessing`` 的 spawn 模式在启动子进程时,会让子进程**重新 import
用户的主模块**(为了能反序列化主模块里定义的函数/类)。后果是:

* 用户脚本里所有模块级代码会被**再执行一遍**(于是社区里流传着
  「Ray 脚本必须写 ``if __name__ == '__main__':`` 保护」的说法);
* 如果模块级代码里有副作用(建连接、写文件),子进程里会重复发生。

Ray 不用 spawn,它用 ``subprocess`` + ``python -c "import ray; …"`` 拉起 worker,
然后把 worker config 通过**管道/临时文件**递过去。这样子进程是一个干净的解释器,
根本不知道用户脚本的存在 —— 用户的函数是**按值**序列化送过去的。

mini-ray 照做。于是:普通脚本、``python -c``、Jupyter、pytest 里都能直接跑,
不需要任何 ``if __name__`` 保护。
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import RaySystemError, RaySystemError as _RaySystemError  # noqa: F401

#: worker 进程的引导代码。它做三件事:恢复 sys.path、读配置、进入 worker 主循环。
#: 注意这里**没有**任何对用户主模块的引用 —— 这正是与 spawn 的关键区别。
_WORKER_BOOTSTRAP = """
import sys, pickle
config_path = sys.argv[1]
with open(config_path, "rb") as fh:
    config = pickle.load(fh)
# 恢复父进程的 sys.path:worker 需要能 import miniray 以及用户自己的包
extra = [p for p in config.get("sys_path", []) if p]
sys.path[:0] = extra
from miniray.worker import worker_main
worker_main(config)
"""

__all__ = ["WorkerSlot", "WorkerPool", "WorkerState"]


class WorkerState(str):
    STARTING = "STARTING"
    IDLE = "IDLE"
    BUSY = "BUSY"
    DEAD = "DEAD"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class WorkerSlot:
    """一个 worker 进程在 raylet 里的「档位」。"""

    worker_id: str
    node_id: str
    worker_type: str = "task"  # task / actor / driver
    actor_id: Optional[str] = None
    process: Optional[subprocess.Popen] = None
    config_path: Optional[str] = None
    pid: Optional[int] = None
    state: str = WorkerState.STARTING
    task_id: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    last_idle_at: float = field(default_factory=time.time)
    num_tasks_executed: int = 0
    #: 待交付给 worker 的任务(worker 下次 request_work 立刻拿到)
    pending_task: Optional[Dict[str, Any]] = None
    #: 该 worker 是否该退出了(优雅停机/退休)
    shutdown_requested: bool = False
    #: 交给 worker 进程退出用的条件变量(长轮询挂在这上面)
    cond: threading.Condition = field(default_factory=threading.Condition)

    def deliver(self, task: Dict[str, Any]) -> None:
        """把任务交付给这个 worker(唤醒它的长轮询)。"""
        with self.cond:
            self.pending_task = task
            self.state = WorkerState.BUSY
            self.task_id = task.get("task_id")
            self.cond.notify_all()

    def request_shutdown(self) -> None:
        with self.cond:
            self.shutdown_requested = True
            self.cond.notify_all()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def exit_code(self) -> Optional[int]:
        return self.process.poll() if self.process is not None else None

    def info(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "node_id": self.node_id,
            "pid": self.pid,
            "state": self.state,
            "worker_type": self.worker_type,
            "actor_id": self.actor_id,
            "task_id": self.task_id,
            "num_tasks_executed": self.num_tasks_executed,
            "idle_seconds": round(time.time() - self.last_idle_at, 3),
        }


class WorkerPool:
    """按节点管理 worker 进程。

    :param max_workers_per_node: 每节点 worker 上限(0 表示按 CPU 数自动)
    :param worker_idle_timeout_ms: 空闲多久退休(0 表示不退休)
    """

    def __init__(
        self,
        *,
        max_workers_per_node: int = 0,
        worker_idle_timeout_ms: int = 0,
        on_worker_death: Optional[Callable[[WorkerSlot], None]] = None,
        session_dir: Optional[str] = None,
    ) -> None:
        self.max_workers_per_node = max_workers_per_node
        self.worker_idle_timeout_ms = worker_idle_timeout_ms
        self._on_worker_death = on_worker_death
        self._lock = threading.RLock()
        self._slots: Dict[str, WorkerSlot] = {}
        self._by_node: Dict[str, List[str]] = {}
        self._counter = 0
        self._session_dir = session_dir
        self.num_spawned = 0
        self.num_retired = 0
        self.num_crashed = 0

    # ---------------------------------------------------------------- 查询
    def get(self, worker_id: str) -> Optional[WorkerSlot]:
        return self._slots.get(worker_id)

    def all_slots(self) -> List[WorkerSlot]:
        return list(self._slots.values())

    def slots_on(self, node_id: str) -> List[WorkerSlot]:
        with self._lock:
            return [self._slots[wid] for wid in self._by_node.get(node_id, [])]

    def idle_slots(self, node_id: str) -> List[WorkerSlot]:
        return [slot for slot in self.slots_on(node_id) if slot.state == WorkerState.IDLE]

    def busy_count(self, node_id: str) -> int:
        return sum(1 for slot in self.slots_on(node_id) if slot.state == WorkerState.BUSY)

    def live_count(self, node_id: str) -> int:
        return sum(
            1
            for slot in self.slots_on(node_id)
            if slot.state in (WorkerState.STARTING, WorkerState.IDLE, WorkerState.BUSY)
        )

    def worker_limit(self, node_cpus: float) -> int:
        """worker 池上限。

        Ray 里这个上限是 ``num_workers_soft_limit``(默认约等于 CPU 数),
        而且它是**软**限制:池满时任务排队,但 raylet 会在有任务等、又有空闲资源时
        继续扩。mini-ray 用一个更宽松的硬上限(``max(16, 8×CPU)``)近似这个行为。

        为什么不能设得太紧?因为**很多小任务会各自拉起一个 worker**:
        任务成批到达时,上一批 worker 还没回到 IDLE,新任务就只能新建 worker。
        上限太紧会出现「CPU 空着、任务在 READY 队列里排队」的饿死现象 ——
        排查起来非常反直觉(资源明明够)。
        """
        return self.max_workers_per_node or max(16, int(node_cpus) * 8)

    def can_spawn(self, node_id: str, node_cpus: float) -> bool:
        return self.live_count(node_id) < self.worker_limit(node_cpus)

    # ---------------------------------------------------------------- 创建
    def spawn(
        self,
        node_id: str,
        worker_config: Dict[str, Any],
        *,
        worker_type: str = "task",
        actor_id: Optional[str] = None,
    ) -> WorkerSlot:
        """拉起一个 worker 进程。``worker_config`` 必须是可 pickle 的普通 dict。"""
        with self._lock:
            self._counter += 1
            worker_id = worker_config["worker_id"]
            slot = WorkerSlot(
                worker_id=worker_id,
                node_id=node_id,
                worker_type=worker_type,
                actor_id=actor_id,
            )
            config = dict(worker_config)
            config["is_actor_worker"] = worker_type == "actor"
            # 把父进程的 sys.path 一起带过去:worker 需要能 import miniray
            # 以及用户自己的包(比如脚本同目录下的模块)
            config["sys_path"] = list(sys.path)
            config_path = self._write_config(worker_id, config)
            slot.config_path = config_path
            try:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-c", _WORKER_BOOTSTRAP, config_path],
                    cwd=os.getcwd(),
                    # stdout/stderr 直接继承:worker 里的 print 会出现在同一个终端
                    # (真实 Ray 会转发日志并加 (pid) 前缀,mini-ray 用继承简化)
                    close_fds=True,
                )
            except OSError as exc:  # pragma: no cover - 启动失败
                raise RaySystemError(f"无法启动 worker 进程: {exc}") from exc
            slot.process = process
            slot.pid = process.pid
            self._slots[worker_id] = slot
            self._by_node.setdefault(node_id, []).append(worker_id)
            self.num_spawned += 1
            return slot

    def _write_config(self, worker_id: str, config: Dict[str, Any]) -> str:
        """把 worker 配置写到临时文件(用 pickle,因为里面有 bytes)。

        真实 Ray 是通过管道把 worker config 递给子进程的;写文件更简单,
        而且出问题时可以直接打开看 —— 调试友好。

        文件名用 **worker_id 的哈希**而不是它的前缀:同一个 actor 重启后的
        worker_id 只在尾部不同(``actor-xxx-0`` / ``actor-xxx-1``),
        按前缀命名会让两次重启共用同一个文件 —— 旧 worker 被回收时删文件,
        正好把新 worker 还没读的配置删掉。
        """
        import hashlib

        directory = self._session_dir or tempfile.gettempdir()
        digest = hashlib.sha1(worker_id.encode()).hexdigest()[:16]
        path = os.path.join(directory, f"worker-{digest}.config")
        with open(path, "wb") as handle:
            pickle.dump(config, handle, protocol=5)
        return path

    # ---------------------------------------------------------------- 退休
    def retire(self, slot: WorkerSlot, *, graceful_timeout: float = 2.0) -> None:
        """让一个空闲 worker 退出(进程真的结束,内存/句柄全部释放)。"""
        with self._lock:
            if slot.state in (WorkerState.DEAD, WorkerState.SHUTDOWN):
                return
            slot.state = WorkerState.SHUTDOWN
            self.num_retired += 1
        slot.request_shutdown()
        process = slot.process
        if process is not None:
            try:
                process.wait(timeout=graceful_timeout)
            except subprocess.TimeoutExpired:
                process.terminate()  # 不听话就强杀
                try:
                    process.wait(timeout=graceful_timeout)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    process.kill()
                    process.wait(timeout=graceful_timeout)
        self._remove(slot)

    def retire_idle(self, *, now: Optional[float] = None) -> List[WorkerSlot]:
        """把空闲太久的 worker 收掉(Ray 的 worker 池会缩容以省内存)。"""
        if not self.worker_idle_timeout_ms:
            return []
        now = now or time.time()
        timeout = self.worker_idle_timeout_ms / 1000.0
        retired: List[WorkerSlot] = []
        with self._lock:
            candidates = [
                slot
                for slot in self._slots.values()
                if slot.state == WorkerState.IDLE and (now - slot.last_idle_at) > timeout
            ]
        for slot in candidates:
            # 每个节点至少留一个 worker,避免把池子清空后又要立刻拉起
            if self.idle_slots(slot.node_id) and len(self.idle_slots(slot.node_id)) <= 1:
                continue
            self.retire(slot)
            retired.append(slot)
        return retired

    # ---------------------------------------------------------------- 回收
    def reap(self) -> List[WorkerSlot]:
        """找出已经死掉的 worker(崩溃 / 被 OOM killer 干掉 / 自己退出)。"""
        dead: List[WorkerSlot] = []
        for slot in list(self._slots.values()):
            if slot.state in (WorkerState.DEAD, WorkerState.SHUTDOWN):
                continue
            if not slot.is_alive():
                slot.state = WorkerState.DEAD
                dead.append(slot)
                self.num_crashed += 1
        for slot in dead:
            if self._on_worker_death is not None:
                try:
                    self._on_worker_death(slot)
                except Exception:  # pragma: no cover - 回调不该影响回收
                    import traceback

                    traceback.print_exc()
            self._remove(slot)
        return dead

    def _remove(self, slot: WorkerSlot) -> None:
        with self._lock:
            self._slots.pop(slot.worker_id, None)
            node_slots = self._by_node.get(slot.node_id)
            if node_slots and slot.worker_id in node_slots:
                node_slots.remove(slot.worker_id)
        if slot.config_path and os.path.exists(slot.config_path):
            try:
                os.remove(slot.config_path)
            except OSError:  # pragma: no cover
                pass

    def shutdown(self, *, timeout: float = 3.0) -> None:
        """关停所有 worker(优雅 + 兜底强杀)。

        **并行等待**很重要:逐个 ``wait(2 秒)`` 的话,10 个 worker 最坏要 20 秒
        才能关完(每次 shutdown 都这么慢,测试和交互式使用都会很难受)。
        正确做法是先全部发停机信号,再用同一个截止时间一起等。
        """
        slots = list(self._slots.values())
        for slot in slots:
            slot.request_shutdown()

        deadline = time.time() + timeout
        for slot in slots:
            process = slot.process
            if process is None:
                continue
            remaining = max(0.05, deadline - time.time())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.terminate()  # 不听话就强杀
        for slot in slots:
            process = slot.process
            if process is not None and process.poll() is None:  # pragma: no cover
                process.kill()
                process.wait(timeout=2.0)

        with self._lock:
            self._slots.clear()
            self._by_node.clear()
        for slot in slots:
            if slot.config_path and os.path.exists(slot.config_path):
                try:
                    os.remove(slot.config_path)
                except OSError:  # pragma: no cover
                    pass

    def stats(self) -> Dict[str, Any]:
        by_state: Dict[str, int] = {}
        for slot in self._slots.values():
            by_state[slot.state] = by_state.get(slot.state, 0) + 1
        return {
            "num_workers": len(self._slots),
            "by_state": by_state,
            "spawned": self.num_spawned,
            "retired": self.num_retired,
            "crashed": self.num_crashed,
        }
