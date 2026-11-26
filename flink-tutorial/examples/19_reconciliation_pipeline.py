"""MiniFlink 19：去重、checkpoint 恢复与对账。

演示一条最小正确性链路：

1. 输入含重复 event_id；
2. 按 event_id 键控去重；
3. 在微批边界做 checkpoint；
4. 从 checkpoint 恢复后继续处理；
5. 对比 source / unique / output 计数，验证不丢不重。

注意：MiniFlink 的 checkpoint 是单进程微批快照，不是生产 Flink 的
barrier 对齐协议；本示例证明的是语义形状，不是分布式正确性。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    KeyedProcessFunction,
    Record,
    StreamExecutionEnvironment,
    ValueStateDescriptor,
)

EVENTS = [
    {"event_id": "e1", "user_id": "u1", "amount": 10},
    {"event_id": "e2", "user_id": "u2", "amount": 20},
    {"event_id": "e1", "user_id": "u1", "amount": 10},   # 重复
    {"event_id": "e3", "user_id": "u1", "amount": 30},
    {"event_id": "e2", "user_id": "u2", "amount": 20},   # 重复
    {"event_id": "e4", "user_id": "u3", "amount": 40},
]


class Deduplicate(KeyedProcessFunction):
    """按当前 key 记录是否已经见过；状态随 checkpoint 快照。"""

    def process_element(self, value, ctx, out):
        seen = ctx.get_state(ValueStateDescriptor("seen", lambda: None))
        if seen.get() is None:
            seen.update(True)
            out.collect(value)


outputs: list[dict] = []

env = StreamExecutionEnvironment(parallelism=1)
env.enable_checkpointing(1_000, micro_batch_records=2)

(
    env.from_collection(
        [Record(event, timestamp=index) for index, event in enumerate(EVENTS)],
        parallelism=1,
    )
    .key_by(lambda event: event["event_id"])
    .process(Deduplicate())
    .sink_to(lambda event: outputs.append(event))
)

# 第一段：只跑一个微批，然后做 checkpoint。
first = env.create_execution()
first.run(max_rounds=1)
checkpoint = first.checkpoints[-1]
print("first checkpoint:", checkpoint.summary())
print("after first batch:", [event["event_id"] for event in outputs])

# 第二段：从 checkpoint 恢复，继续处理剩余输入。
restored = env.create_execution()
restored.restore(checkpoint)
restored.run()

source_total = len(EVENTS)
unique_total = len({event["event_id"] for event in EVENTS})
output_ids = [event["event_id"] for event in outputs]

print("source events:", source_total)
print("unique events:", unique_total)
print("output events:", len(output_ids))
print("output ids:", output_ids)

assert len(output_ids) == unique_total, "输出数量应等于唯一事件数"
assert len(output_ids) == len(set(output_ids)), "输出不应包含重复 event_id"
assert set(output_ids) == {event["event_id"] for event in EVENTS}, "输出应覆盖全部事件"

print("reconciliation: OK (no loss, no duplicate)")
