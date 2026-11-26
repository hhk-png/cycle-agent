"""检查点、保存点、状态存储与重启策略。

MiniFlink 用“微批之间暂停并做全局一致快照”的方式实现本地检查点。生产
Flink 使用基于屏障的分布式快照（Chandy-Lamport 变体），不需要停止数据流；
两种方式的共同目标是：快照中每个算子的状态都对应同一批输入。
"""

from __future__ import annotations

import copy
import os
import pickle
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .core import JobExecutionError


class CheckpointingMode(str, Enum):
    EXACTLY_ONCE = "exactly_once"
    AT_LEAST_ONCE = "at_least_once"


@dataclass
class CheckpointConfig:
    interval_ms: int = 10_000
    timeout_ms: int = 60_000
    min_pause_ms: int = 500
    max_concurrent: int = 1
    mode: CheckpointingMode = CheckpointingMode.EXACTLY_ONCE
    externalized: bool = False
    # 教学运行时的微批大小；每批结束是一个可快照点
    micro_batch_records: int = 50

    def validate(self) -> None:
        if self.interval_ms <= 0:
            raise ValueError("检查点间隔必须大于 0")
        if self.micro_batch_records <= 0:
            raise ValueError("micro_batch_records 必须大于 0")
        if self.max_concurrent <= 0:
            raise ValueError("max_concurrent 必须大于 0")


@dataclass
class Checkpoint:
    checkpoint_id: int
    timestamp_ms: int
    task_states: dict[str, dict[str, Any]]
    completed: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        size = 0
        try:
            size = len(pickle.dumps(self.task_states))
        except Exception:
            size = -1
        return (
            f"Checkpoint(id={self.checkpoint_id}, tasks={len(self.task_states)}, "
            f"bytes={size})"
        )


class CheckpointStorage(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...
    def load(self, checkpoint_id: int) -> Checkpoint: ...
    def latest(self) -> Checkpoint | None: ...
    def list_checkpoints(self) -> list[int]: ...


class MemoryCheckpointStorage:
    def __init__(self) -> None:
        self._checkpoints: dict[int, Checkpoint] = {}

    def save(self, checkpoint: Checkpoint) -> None:
        self._checkpoints[checkpoint.checkpoint_id] = copy.deepcopy(checkpoint)

    def load(self, checkpoint_id: int) -> Checkpoint:
        if checkpoint_id not in self._checkpoints:
            raise KeyError(f"检查点 {checkpoint_id} 不存在")
        return copy.deepcopy(self._checkpoints[checkpoint_id])

    def latest(self) -> Checkpoint | None:
        if not self._checkpoints:
            return None
        return self.load(max(self._checkpoints))

    def list_checkpoints(self) -> list[int]:
        return sorted(self._checkpoints)


class FileCheckpointStorage:
    """把每个检查点写成独立文件，便于观察和手工恢复。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, checkpoint_id: int) -> str:
        return os.path.join(self.directory, f"chk-{checkpoint_id}.bin")

    def save(self, checkpoint: Checkpoint) -> None:
        tmp = self._path(checkpoint.checkpoint_id) + ".tmp"
        with open(tmp, "wb") as handle:
            pickle.dump(checkpoint, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path(checkpoint.checkpoint_id))

    def load(self, checkpoint_id: int) -> Checkpoint:
        path = self._path(checkpoint_id)
        if not os.path.exists(path):
            raise KeyError(f"检查点文件不存在: {path}")
        with open(path, "rb") as handle:
            return pickle.load(handle)

    def latest(self) -> Checkpoint | None:
        ids = self.list_checkpoints()
        return self.load(ids[-1]) if ids else None

    def list_checkpoints(self) -> list[int]:
        result = []
        for name in os.listdir(self.directory):
            if name.startswith("chk-") and name.endswith(".bin"):
                try:
                    result.append(int(name[4:-4]))
                except ValueError:
                    continue
        return sorted(result)


class CheckpointCoordinator:
    """生成检查点、记录最近成功检查点并支持恢复。"""

    def __init__(self, storage: CheckpointStorage, config: CheckpointConfig | None = None) -> None:
        self.storage = storage
        self.config = config or CheckpointConfig()
        self.config.validate()
        self.last_checkpoint_id = 0
        self.last_checkpoint: Checkpoint | None = None
        self.failed_checkpoints = 0
        self.completed_checkpoints = 0

    def trigger(self, task_states: dict[str, dict[str, Any]], metadata: dict[str, Any] | None = None) -> Checkpoint:
        self.last_checkpoint_id += 1
        checkpoint = Checkpoint(
            checkpoint_id=self.last_checkpoint_id,
            timestamp_ms=int(time.time() * 1000),
            task_states=task_states,
            metadata=metadata or {},
        )
        try:
            self.storage.save(checkpoint)
        except Exception:
            self.failed_checkpoints += 1
            raise
        self.last_checkpoint = checkpoint
        self.completed_checkpoints += 1
        return checkpoint

    def restore_latest(self) -> Checkpoint:
        checkpoint = self.storage.latest()
        if checkpoint is None:
            raise JobExecutionError("没有可用检查点")
        self.last_checkpoint = checkpoint
        self.last_checkpoint_id = max(self.last_checkpoint_id, checkpoint.checkpoint_id)
        return checkpoint

    def savepoint(self, task_states: dict[str, dict[str, Any]], label: str = "manual") -> Checkpoint:
        checkpoint = self.trigger(task_states, {"type": "savepoint", "label": label})
        return checkpoint

    def metrics(self) -> dict[str, int]:
        return {
            "completed": self.completed_checkpoints,
            "failed": self.failed_checkpoints,
            "last_checkpoint_id": self.last_checkpoint_id,
        }


class RestartStrategy:
    def should_restart(self, attempt: int) -> bool:
        raise NotImplementedError

    def delay_ms(self, attempt: int) -> int:
        return 0


@dataclass
class FixedDelayRestartStrategy(RestartStrategy):
    attempts: int
    delay_ms: int = 0

    def should_restart(self, attempt: int) -> bool:
        return attempt <= self.attempts

    def delay_ms(self, attempt: int) -> int:
        return self.delay_ms


@dataclass
class NoRestartStrategy(RestartStrategy):
    def should_restart(self, attempt: int) -> bool:
        return False


@dataclass
class FailureRateRestartStrategy(RestartStrategy):
    max_failures_per_interval: int
    interval_ms: int
    delay_ms: int = 0
    _failures: list[int] = field(default_factory=list)

    def should_restart(self, attempt: int) -> bool:
        now = int(time.time() * 1000)
        cutoff = now - self.interval_ms
        self._failures = [ts for ts in self._failures if ts >= cutoff]
        self._failures.append(now)
        return len(self._failures) <= self.max_failures_per_interval
