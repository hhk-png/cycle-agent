"""本地“集群”模拟：JobManager、TaskManager、Slot 与作业状态。

它不提供进程隔离或真正的分布式调度，只用于演示 Flink 的部署模型：
JobManager 接收 JobGraph，按 Slot 容量分配 Task，TaskManager 提供执行槽。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .api import StreamExecutionEnvironment
from .graph import JobGraph
from .runtime import JobExecution


class JobStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


@dataclass
class TaskManager:
    id: str
    slots: int
    used_slots: int = 0

    def allocate(self, count: int) -> bool:
        if self.used_slots + count > self.slots:
            return False
        self.used_slots += count
        return True

    def release(self, count: int) -> None:
        self.used_slots = max(0, self.used_slots - count)


@dataclass
class ClusterJob:
    job_id: str
    name: str
    graph: JobGraph
    status: JobStatus = JobStatus.CREATED
    execution: JobExecution | None = None
    error: str | None = None


class LocalCluster:
    def __init__(self, task_managers: int = 1, slots_per_task_manager: int = 4) -> None:
        if task_managers <= 0 or slots_per_task_manager <= 0:
            raise ValueError("TaskManager 数量和 slot 数必须大于 0")
        self.task_managers = [
            TaskManager(f"tm-{i}", slots_per_task_manager)
            for i in range(task_managers)
        ]
        self.jobs: dict[str, ClusterJob] = {}
        self._counter = 0

    @property
    def total_slots(self) -> int:
        return sum(tm.slots for tm in self.task_managers)

    @property
    def available_slots(self) -> int:
        return sum(tm.slots - tm.used_slots for tm in self.task_managers)

    def _required_slots(self, graph: JobGraph) -> int:
        # 不同顶点可以复用 slot，因此所需 slot 是单顶点并行度的最大值；
        # 真实 Flink 的 slot sharing 还受 slot sharing group 影响。
        return max((node.parallelism for node in graph.nodes), default=0)

    def submit(
        self,
        env: StreamExecutionEnvironment,
        *,
        job_name: str = "MiniFlink Job",
        max_rounds: int | None = None,
    ) -> ClusterJob:
        graph = env.get_execution_plan()
        required = self._required_slots(graph)
        if required > self.available_slots:
            raise RuntimeError(
                f"slot 不足：需要 {required}，可用 {self.available_slots}"
            )
        allocations: list[tuple[TaskManager, int]] = []
        remaining = required
        for tm in self.task_managers:
            free = tm.slots - tm.used_slots
            take = min(free, remaining)
            if take and tm.allocate(take):
                allocations.append((tm, take))
                remaining -= take
            if remaining == 0:
                break
        self._counter += 1
        job = ClusterJob(f"job-{self._counter:04d}", job_name, graph)
        self.jobs[job.job_id] = job
        job.status = JobStatus.RUNNING
        try:
            execution = JobExecution(
                graph,
                checkpoint_storage=env.checkpoint_storage,
                checkpoint_config=env.checkpoint_config,
                checkpointing_enabled=env.checkpointing_enabled,
            )
            execution.run(max_rounds=max_rounds)
            job.execution = execution
            job.status = (
                JobStatus.FINISHED if execution.is_finished() else JobStatus.RUNNING
            )
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
        finally:
            for tm, count in allocations:
                tm.release(count)
        return job

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status is JobStatus.RUNNING:
            job.status = JobStatus.CANCELED

    def metrics(self) -> dict[str, Any]:
        return {
            "slots_total": self.total_slots,
            "slots_available": self.available_slots,
            "jobs": {
                job_id: {
                    "name": job.name,
                    "status": job.status.value,
                    "error": job.error,
                }
                for job_id, job in self.jobs.items()
            },
        }
