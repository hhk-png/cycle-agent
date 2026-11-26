"""示例 21：离线状态迁移（MiniFlink 版 State Processor API）。

真实 Flink 用 SavepointReader/SavepointWriter 离线读写 savepoint；MiniFlink
没有分布式 savepoint 格式，但 checkpoint 里的 task_states 是一个可读的字典。
这个示例演示同一套思路：

    读快照 → 找到 keyed state → 修改 → 写回新快照 → 从新快照恢复

生产环境请使用 Flink 的 State Processor API（见附录 L），不要依赖内部字典格式。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    Checkpoint,
    KeyedProcessFunction,
    Record,
    StreamExecutionEnvironment,
    ValueStateDescriptor,
    WatermarkStrategy,
)


class Counter(KeyedProcessFunction):
    """每个 key 维护一个计数，便于观察迁移前后的差异。"""

    def process_element(self, value, ctx, out):
        state = ctx.get_state(ValueStateDescriptor("count", lambda: 0))
        state.update(state.get() + 1)
        out.collect((ctx.current_key, state.get()))


def build_env(sink: list) -> StreamExecutionEnvironment:
    env = StreamExecutionEnvironment()
    env.enable_checkpointing(1000, micro_batch_records=1)
    (
        env.from_collection(
            [
                Record(("a", 1), timestamp=1),
                Record(("a", 2), timestamp=2),
                Record(("a", 3), timestamp=3),
            ],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .process(Counter())
        .sink_to(sink.append)
    )
    return env


def main() -> None:
    # 1. 先跑一条记录，得到一个包含 keyed state 的快照
    before: list = []
    first = build_env(before).create_execution()
    first.run(max_rounds=1)
    checkpoint = first.checkpoint("before-migration")
    assert before == [("a", 1)], before

    # 2. 找到有状态的 task。真实 Flink 用 operator uid 定位；
    #    MiniFlink 直接遍历 task_states，挑出 state.data 非空的算子。
    stateful = next(
        state
        for state in checkpoint.task_states.values()
        if state["state"]["data"]
    )

    # 3. 状态布局：{key: {state_name: (value, ttl_timestamp)}}。
    #    这里把 count 从 1 改成 100，模拟“离线修正历史状态”。
    old_value, ttl_timestamp = stateful["state"]["data"]["a"]["count"]
    stateful["state"]["data"]["a"]["count"] = (100, ttl_timestamp)
    assert old_value == 1, old_value

    # 4. 用修改后的状态构造一个新快照。真实 Flink 用 SavepointWriter 生成
    #    新 savepoint；这里换一个 checkpoint id，避免覆盖原快照。
    migrated = Checkpoint(
        checkpoint_id=checkpoint.checkpoint_id + 1,
        timestamp_ms=checkpoint.timestamp_ms,
        task_states=copy.deepcopy(checkpoint.task_states),
        metadata={"type": "savepoint", "label": "migrated"},
    )

    # 5. 从迁移后的快照恢复，继续处理剩余两条记录
    after: list = []
    restored = build_env(after).create_execution()
    restored.restore(migrated)
    restored.run()

    print("before migration:", before)
    print("after migration:", after)
    assert after == [("a", 101), ("a", 102)], after


if __name__ == "__main__":
    main()
