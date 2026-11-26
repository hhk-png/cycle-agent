import copy

import pytest

from miniflink import (
    Checkpoint,
    CheckpointConfig,
    CheckpointingMode,
    FailureRateRestartStrategy,
    FileCheckpointStorage,
    FixedDelayRestartStrategy,
    KeyedProcessFunction,
    MemoryCheckpointStorage,
    NoRestartStrategy,
    Record,
    StreamExecutionEnvironment,
    ValueStateDescriptor,
    WatermarkStrategy,
)
from miniflink.checkpointing import CheckpointCoordinator


def sample_checkpoint(checkpoint_id=1):
    return Checkpoint(checkpoint_id, 1000, {"task": {"operator": {}}})


def test_memory_storage_roundtrip_and_latest():
    storage = MemoryCheckpointStorage()
    storage.save(sample_checkpoint(1))
    storage.save(sample_checkpoint(2))
    assert storage.list_checkpoints() == [1, 2]
    assert storage.latest().checkpoint_id == 2
    assert storage.load(1).checkpoint_id == 1
    with pytest.raises(KeyError):
        storage.load(3)


def test_file_storage_roundtrip(tmp_path):
    storage = FileCheckpointStorage(str(tmp_path / "cp"))
    storage.save(sample_checkpoint(5))
    assert storage.latest().checkpoint_id == 5
    assert storage.list_checkpoints() == [5]


def test_coordinator_metrics_and_restore():
    coordinator = CheckpointCoordinator(MemoryCheckpointStorage())
    coordinator.trigger({"a": {}})
    coordinator.trigger({"a": {}})
    assert coordinator.metrics()["completed"] == 2
    assert coordinator.restore_latest().checkpoint_id == 2


def test_checkpoint_config_validation():
    with pytest.raises(ValueError):
        CheckpointConfig(interval_ms=0).validate()
    with pytest.raises(ValueError):
        CheckpointConfig(micro_batch_records=0).validate()
    assert CheckpointingMode.EXACTLY_ONCE.value == "exactly_once"


def test_restart_strategies():
    fixed = FixedDelayRestartStrategy(attempts=2, delay_ms=10)
    assert fixed.should_restart(1)
    assert fixed.should_restart(2)
    assert not fixed.should_restart(3)
    assert not NoRestartStrategy().should_restart(1)
    rate = FailureRateRestartStrategy(2, 60_000)
    assert rate.should_restart(1)
    assert rate.should_restart(2)
    assert not rate.should_restart(3)


def test_execution_checkpoint_and_restore():
    env = StreamExecutionEnvironment()
    env.enable_checkpointing(1000, micro_batch_records=2)
    out = []
    (
        env.from_collection([1, 2, 3, 4, 5])
        .map(lambda value: value * 2)
        .sink_to(out.append)
    )
    first = env.create_execution()
    first.run(max_rounds=1)
    checkpoint = first.checkpoint("manual")
    assert out == [2, 4]
    second = env.create_execution()
    second.restore(checkpoint)
    second.run()
    assert out == [2, 4, 6, 8, 10]

    # 作业已完成后的检查点恢复不应挂起，也不应重复写 source 记录。
    finished_checkpoint = second.checkpoint("finished")
    third = env.create_execution()
    third.restore(finished_checkpoint)
    third.run()
    assert out == [2, 4, 6, 8, 10]


def test_checkpoint_restore_rejects_incompatible_graph():
    env = StreamExecutionEnvironment()
    env.enable_checkpointing(1000)
    env.from_collection([1]).print()
    first = env.create_execution()
    first.run(max_rounds=1)
    checkpoint = first.checkpoints[-1]

    other = StreamExecutionEnvironment()
    other.enable_checkpointing(1000)
    other.from_collection([1]).map(lambda value: value + 1).print()
    second = other.create_execution()
    with pytest.raises(Exception):
        second.restore(checkpoint)


def test_manual_savepoint_survives_repeated_checkpoints():
    env = StreamExecutionEnvironment()
    env.enable_checkpointing(1000, micro_batch_records=1)
    values = []
    env.from_collection([1, 2, 3]).sink_to(values.append)
    execution = env.create_execution()
    execution.run(max_rounds=1)
    first = execution.checkpoints[-1]
    execution.run(max_rounds=1)
    second = execution.checkpoints[-1]
    assert first.checkpoint_id < second.checkpoint_id
    assert execution.coordinator.completed_checkpoints == len(execution.checkpoints)


def test_checkpoint_ids_continue_after_restore():
    env = StreamExecutionEnvironment()
    env.enable_checkpointing(1000, micro_batch_records=1)
    values = []
    env.from_collection([1, 2, 3]).sink_to(values.append)
    first = env.create_execution()
    first.run(max_rounds=1)
    checkpoint = first.checkpoint("before-restore")
    assert checkpoint.checkpoint_id >= 1

    restored = env.create_execution()
    restored.restore(checkpoint)
    restored.run(max_rounds=1)
    next_checkpoint = restored.checkpoints[-1]
    assert next_checkpoint.checkpoint_id > checkpoint.checkpoint_id


def test_offline_state_edit_and_restore():
    """演示 State Processor API 的核心语义：离线改状态后从新快照恢复。"""

    class Counter(KeyedProcessFunction):
        def process_element(self, value, ctx, out):
            state = ctx.get_state(ValueStateDescriptor("count", lambda: 0))
            state.update(state.get() + 1)
            out.collect((ctx.current_key, state.get()))

    def build(sink):
        env = StreamExecutionEnvironment()
        env.enable_checkpointing(1000, micro_batch_records=1)
        (
            env.from_collection(
                [Record(("a", 1), 1), Record(("a", 2), 2)],
                watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
            )
            .key_by(lambda row: row[0])
            .process(Counter())
            .sink_to(sink.append)
        )
        return env

    before = []
    first = build(before).create_execution()
    first.run(max_rounds=1)
    checkpoint = first.checkpoint("before-migration")
    assert before == [("a", 1)]

    stateful = next(
        state for state in checkpoint.task_states.values() if state["state"]["data"]
    )
    _, ttl_timestamp = stateful["state"]["data"]["a"]["count"]
    stateful["state"]["data"]["a"]["count"] = (100, ttl_timestamp)

    migrated = Checkpoint(
        checkpoint_id=checkpoint.checkpoint_id + 1,
        timestamp_ms=checkpoint.timestamp_ms,
        task_states=copy.deepcopy(checkpoint.task_states),
    )
    after = []
    restored = build(after).create_execution()
    restored.restore(migrated)
    restored.run()
    assert after == [("a", 101)]
