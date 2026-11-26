"""MiniFlink 17：JobGraph、指标与 checkpoint。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import StreamExecutionEnvironment

env = StreamExecutionEnvironment(parallelism=2)
env.enable_checkpointing(1_000, micro_batch_records=3)
(
    env.from_collection(range(10), parallelism=2)
    .map(lambda value: value * 2)
    .print()
)
print(env.explain())
job = env.execute("metrics")
print(job.metrics()["job"])
print(job.metrics()["checkpoints"])
