"""MiniFlink 本地运行时。

执行模型：顶点按拓扑序展开成若干 Task；每个边在上下游 Task 之间建立带缓冲
的通道。调度器轮转推进 source 与各 Task，并沿通道传播 watermark 和结束信号。
checkpoint 在微批边界（所有通道排空后）做一次全局一致快照，因此不需要在
本地实现生产 Flink 的 barrier 对齐协议。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .checkpointing import (
    Checkpoint,
    CheckpointConfig,
    CheckpointCoordinator,
    CheckpointStorage,
    MemoryCheckpointStorage,
)
from .core import (
    MAX_WATERMARK,
    MIN_WATERMARK,
    Barrier,
    EndOfStream,
    JobExecutionError,
    Record,
    SideOutputRecord,
    StreamElement,
    Watermark,
)
from .functions import RuntimeContext
from .graph import Edge, JobGraph, Node, Partitioning
from .metrics import MetricGroup
from .state import KeyedStateBackend


@dataclass
class Channel:
    """一条上游子任务到下游子任务的输入通道。"""

    edge_index: int
    upstream_task_id: str
    queue: deque[StreamElement] = field(default_factory=deque)
    ended: bool = False
    watermark: int = MIN_WATERMARK


@dataclass
class Downstream:
    edge: Edge
    edge_index: int
    child_tasks: list["RuntimeTask"]
    queues: dict[int, deque[StreamElement]]
    source_subtask_index: int = 0
    round_robin: int = 0


def _stable_hash(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=False)
    return _stable_hash(repr(value))


class RuntimeTask:
    def __init__(
        self,
        task_id: str,
        node: Node,
        subtask_index: int,
        parallelism: int,
    ) -> None:
        self.id = task_id
        self.node = node
        self.subtask_index = subtask_index
        self.parallelism = parallelism
        self.context = RuntimeContext(
            task_name=f"{node.name}[{subtask_index}]",
            subtask_index=subtask_index,
            parallelism=parallelism,
            state_backend=KeyedStateBackend(),
        )
        self.context.metric_group = MetricGroup(f"task.{task_id}")
        self.operator = node.operator_factory(subtask_index, parallelism)
        self.operator.open(self.context)
        self.input_channels: list[list[Channel]] = [[] for _ in node.inputs]
        self.downstream: list[Downstream] = []
        self.current_watermark = MIN_WATERMARK
        self.last_side_record: Record | None = None
        self.finished = False
        self.records_in = 0
        self.records_out = 0
        self.operator._task = self  # 调试用，不参与序列化

    def snapshot(self) -> dict[str, Any]:
        return {
            "operator": self.operator.snapshot(),
            "state": self.context.state_backend.snapshot(),
            "timers": self.context.timer_service.snapshot(),
            "records_in": self.records_in,
            "records_out": self.records_out,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.operator.restore(snapshot.get("operator", {}))
        self.context.state_backend.restore(snapshot.get("state", {"kind": "keyed", "data": {}}))
        self.context.timer_service.restore(snapshot.get("timers", {}))
        self.records_in = snapshot.get("records_in", 0)
        self.records_out = snapshot.get("records_out", 0)

    def close(self) -> None:
        try:
            self.operator.close()
        except Exception:
            pass


class JobExecution:
    def __init__(
        self,
        graph: JobGraph,
        *,
        checkpoint_storage: CheckpointStorage | None = None,
        checkpoint_config: CheckpointConfig | None = None,
        checkpointing_enabled: bool = False,
    ) -> None:
        self.graph = graph
        self.checkpoint_storage = checkpoint_storage or MemoryCheckpointStorage()
        self.checkpoint_config = checkpoint_config or CheckpointConfig()
        self.checkpointing_enabled = checkpointing_enabled
        self.coordinator = CheckpointCoordinator(self.checkpoint_storage, self.checkpoint_config)
        self.metric_group = MetricGroup("job")
        self.side_outputs: dict[str, list[Any]] = {}
        self.records_processed = 0
        self.records_since_checkpoint = 0
        self.rounds = 0
        self.checkpoints: list[Checkpoint] = []
        self._closed = False

        topo = graph.topological_order()
        self.tasks_by_node: dict[str, list[RuntimeTask]] = {}
        self.non_source_tasks: list[RuntimeTask] = []
        self.source_tasks: list[RuntimeTask] = []
        self.tasks: list[RuntimeTask] = []
        self.task_index: dict[str, int] = {}

        for index, node in enumerate(topo):
            base = node.uid or f"{index:03d}-{node.name}"
            tasks: list[RuntimeTask] = []
            for subtask in range(node.parallelism):
                task_id = f"{base}#{subtask}"
                task = RuntimeTask(task_id, node, subtask, node.parallelism)
                tasks.append(task)
                self.tasks.append(task)
                self.task_index[task.id] = len(self.tasks) - 1
            self.tasks_by_node[node.id] = tasks
            if node.is_source:
                self.source_tasks.extend(tasks)
            else:
                self.non_source_tasks.extend(tasks)

        self._wire_edges(topo)

    def _wire_edges(self, topo: list[Node]) -> None:
        for node in topo:
            child_tasks = self.tasks_by_node[node.id]
            for edge_index, edge in enumerate(node.inputs):
                source_tasks = self.tasks_by_node[edge.source.id]
                for source_task in source_tasks:
                    queues = {i: deque() for i in range(len(child_tasks))}
                    for target_index, child in enumerate(child_tasks):
                        channel = Channel(
                            edge_index=edge_index,
                            upstream_task_id=source_task.id,
                            queue=queues[target_index],
                        )
                        child.input_channels[edge_index].append(channel)
                    source_task.downstream.append(
                        Downstream(
                            edge=edge,
                            edge_index=edge_index,
                            child_tasks=child_tasks,
                            queues=queues,
                            source_subtask_index=source_task.subtask_index,
                        )
                    )

    # ---------- 运行 ----------

    def run(self, *, max_rounds: int | None = None) -> "JobExecution":
        rounds = 0
        while not self.is_finished():
            self.run_round()
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
        self.metric_group.counter("recordsProcessed").inc(self.records_processed)
        return self

    def run_round(self) -> None:
        """从每个 source 产生一个微批，排空所有通道，然后可选地做检查点。"""
        self.rounds += 1
        budget = self.checkpoint_config.micro_batch_records
        for task in self.source_tasks:
            if task.finished:
                continue
            produced = 0
            while produced < budget and not task.finished:
                try:
                    element = task.operator.next_element()  # type: ignore[attr-defined]
                except JobExecutionError:
                    raise
                except Exception as exc:
                    raise JobExecutionError(
                        f"Source {task.node.name}#{task.subtask_index} 读取失败: {exc}"
                    ) from exc
                if element is None:
                    break
                if isinstance(element, Record):
                    produced += 1
                    self.records_processed += 1
                    self.records_since_checkpoint += 1
                    task.records_out += 1
                self._emit_from_source(task, element)
        self._drain()
        if self.checkpointing_enabled and not self.is_finished():
            self.checkpoint()
        elif self.checkpointing_enabled:
            self.checkpoint()

    def _drain(self) -> None:
        while True:
            progress = False
            for task in self.non_source_tasks:
                if task.finished:
                    continue
                if self._process_one(task):
                    progress = True
            for task in self.tasks:
                if task.finished:
                    continue
                try:
                    outputs = task.operator.poll()
                except JobExecutionError:
                    raise
                except Exception as exc:
                    raise JobExecutionError(
                        f"算子 {task.node.name}#{task.subtask_index} 异步回调失败: {exc}"
                    ) from exc
                if outputs:
                    self._handle_outputs(task, outputs)
                    progress = True
            if not progress:
                break

    def _process_one(self, task: RuntimeTask) -> bool:
        # 广播边优先只是单个 task 内部的尽力而为：跨 subtask、跨通道仍可能
        # 主数据先到。它不承诺“规则先装载”的确定性；业务要做 bootstrap 或
        # 接受最终一致（生产 Flink 也不保证跨流到达顺序）。
        edge_order = sorted(
            range(len(task.input_channels)),
            key=lambda index: not task.node.inputs[index].source.is_broadcast,
        )
        for edge_index in edge_order:
            channels = task.input_channels[edge_index]
            for channel in channels:
                if not channel.queue:
                    continue
                element = channel.queue.popleft()
                if isinstance(element, Record):
                    self._process_record(task, channel, element, edge_index)
                elif isinstance(element, Watermark):
                    if not channel.ended:
                        channel.watermark = max(channel.watermark, element.timestamp)
                    self._recompute_watermark(task)
                elif isinstance(element, EndOfStream):
                    channel.ended = True
                    channel.watermark = MAX_WATERMARK
                    self._recompute_watermark(task)
                    self._maybe_finalize(task)
                elif isinstance(element, Barrier):
                    pass
                return True
        return False

    def _process_record(
        self, task: RuntimeTask, channel: Channel, record: Record, edge_index: int
    ) -> None:
        task.records_in += 1
        task.last_side_record = record
        task.context.set_current_key(record.key)
        task.context.current_timestamp = record.timestamp
        task.context.current_watermark = task.current_watermark
        try:
            outputs = task.operator.process_record(record, edge_index)
        except Exception as exc:
            raise JobExecutionError(
                f"算子 {task.node.name}#{task.subtask_index} 处理记录失败: {exc}"
            ) from exc
        self._handle_outputs(task, outputs)

    def _handle_outputs(self, task: RuntimeTask, outputs: Iterable[StreamElement]) -> None:
        for element in outputs:
            if isinstance(element, Record):
                task.records_out += 1
                self._route_record(task, element, None)
            elif isinstance(element, Watermark):
                self._route_control(task, element)
            elif isinstance(element, EndOfStream):
                # 结束信号只由 _maybe_finalize / source 发出一次
                continue
            elif isinstance(element, Barrier):
                self._route_control(task, element)
            elif isinstance(element, SideOutputRecord):
                record = Record(element.value, element.timestamp, element.key)
                if not self._route_record(task, record, element.tag):
                    self.side_outputs.setdefault(element.tag, []).append(element.value)
        self._route_side_outputs(task)

    def _route_side_outputs(self, task: RuntimeTask) -> None:
        side_outputs = task.context.drain_side_output_records()
        if not side_outputs:
            return
        for tag, records in side_outputs.items():
            for side in records:
                record = Record(
                    side.value,
                    timestamp=side.timestamp,
                    key=side.key,
                )
                routed = self._route_record(task, record, tag)
                if not routed:
                    self.side_outputs.setdefault(tag, []).append(side.value)

    def _route_record(self, task: RuntimeTask, record: Record, tag: str | None) -> bool:
        routed = False
        for down in task.downstream:
            if down.edge.output_tag != tag:
                continue
            targets = self._select_targets(down, record)
            for target_index in targets:
                down.queues[target_index].append(record)
                routed = True
        return routed

    def _select_targets(self, down: Downstream, record: Record) -> list[int]:
        count = len(down.child_tasks)
        partitioning = down.edge.partitioning
        if partitioning is Partitioning.BROADCAST:
            return list(range(count))
        if partitioning is Partitioning.HASH:
            if record.key is None:
                raise JobExecutionError(
                    f"边 {down.edge.source.name} -> {down.child_tasks[0].node.name} "
                    "需要 hash 分区，但记录没有 key"
                )
            return [_stable_hash(record.key) % count]
        if partitioning is Partitioning.REBALANCE:
            target = down.round_robin % count
            down.round_robin += 1
            return [target]
        # FORWARD：并行度一致时按子任务编号直连，否则退化为轮询
        if count == down.edge.source.parallelism:
            return [min(down.source_subtask_index, count - 1)]
        target = down.round_robin % count
        down.round_robin += 1
        return [target]

    def _route_control(self, task: RuntimeTask, element: StreamElement) -> None:
        for down in task.downstream:
            for target_index in range(len(down.child_tasks)):
                down.queues[target_index].append(element)

    def _emit_from_source(self, task: RuntimeTask, element: StreamElement) -> None:
        if isinstance(element, Record):
            self._route_record(task, element, None)
        elif isinstance(element, Watermark):
            self._route_control(task, element)
        elif isinstance(element, EndOfStream):
            self._route_control(task, element)
            task.finished = True
        elif isinstance(element, Barrier):
            self._route_control(task, element)

    def _recompute_watermark(self, task: RuntimeTask) -> None:
        channels = [channel for group in task.input_channels for channel in group]
        if not channels:
            return
        active = [channel for channel in channels if not channel.ended]
        effective = min((channel.watermark for channel in active), default=MAX_WATERMARK)
        if effective <= task.current_watermark:
            return
        task.current_watermark = effective
        task.context.current_watermark = effective
        outputs = task.operator.process_watermark(effective)
        self._handle_outputs(task, outputs)
        self._maybe_finalize(task)

    def _maybe_finalize(self, task: RuntimeTask) -> None:
        if task.finished:
            return
        channels = [channel for group in task.input_channels for channel in group]
        if not channels or not all(channel.ended for channel in channels):
            return
        task.finished = True
        if task.current_watermark < MAX_WATERMARK:
            task.current_watermark = MAX_WATERMARK
            task.context.current_watermark = MAX_WATERMARK
            self._handle_outputs(task, task.operator.process_watermark(MAX_WATERMARK))
        self._handle_outputs(task, task.operator.end_of_input())
        self._route_control(task, EndOfStream())
        task.close()

    # ---------- 检查点 ----------

    def checkpoint(self, label: str = "periodic") -> Checkpoint:
        self._drain()
        states = {task.id: task.snapshot() for task in self.tasks}
        checkpoint = self.coordinator.trigger(
            states,
            {
                "label": label,
                "rounds": self.rounds,
                "records_processed": self.records_processed,
            },
        )
        self.checkpoints.append(checkpoint)
        self.records_since_checkpoint = 0
        self.metric_group.counter("checkpointsCompleted").inc()
        return checkpoint

    def restore(self, checkpoint: Checkpoint) -> "JobExecution":
        missing = [task.id for task in self.tasks if task.id not in checkpoint.task_states]
        if missing:
            raise JobExecutionError(
                "检查点与当前作业图不兼容，缺少任务状态: " + ", ".join(missing[:5])
            )
        for task in self.tasks:
            task.restore(checkpoint.task_states[task.id])
        self.records_processed = int(checkpoint.metadata.get("records_processed", 0))
        # 恢复后新的 checkpoint 编号必须继续递增，否则会覆盖存储中的旧快照，
        # 也会让 savepoint/恢复链路产生难以排查的“时间倒流”。
        self.coordinator.last_checkpoint_id = max(
            self.coordinator.last_checkpoint_id, checkpoint.checkpoint_id
        )
        self.coordinator.last_checkpoint = checkpoint
        return self

    # ---------- 查询 ----------

    def is_finished(self) -> bool:
        return all(task.finished for task in self.tasks)

    def metrics(self) -> dict[str, Any]:
        tasks = {
            task.id: {
                "operator": task.node.name,
                "subtask": task.subtask_index,
                "records_in": task.records_in,
                "records_out": task.records_out,
                "watermark": (
                    "MAX" if task.current_watermark == MAX_WATERMARK else task.current_watermark
                ),
                "finished": task.finished,
            }
            for task in self.tasks
        }
        return {
            "job": {
                "rounds": self.rounds,
                "records_processed": self.records_processed,
                "finished": self.is_finished(),
            },
            "checkpoints": self.coordinator.metrics(),
            "tasks": tasks,
            "side_outputs": {tag: len(values) for tag, values in self.side_outputs.items()},
        }

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self.tasks:
            task.close()


class LocalExecutor:
    """把构建、执行、打印指标串起来的便捷入口。"""

    def __init__(self, execution: JobExecution) -> None:
        self.execution = execution

    def submit(self, *, max_rounds: int | None = None) -> JobExecution:
        return self.execution.run(max_rounds=max_rounds)

    def get_job_graph(self) -> JobGraph:
        return self.execution.graph
