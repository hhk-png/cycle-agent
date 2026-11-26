"""MiniFlink 07：区间 join。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    Duration,
    Record,
    StreamExecutionEnvironment,
    WatermarkStrategy,
)

strategy = WatermarkStrategy.for_bounded_out_of_orderness(0)
payments = [
    Record(("alice", 100), timestamp=1_000),
    Record(("alice", 200), timestamp=8_000),
]
shipments = [
    Record(("alice", "s1"), timestamp=2_000),
    Record(("alice", "s2"), timestamp=9_000),
]

env = StreamExecutionEnvironment()
left = env.from_collection(payments, watermark_strategy=strategy).key_by(lambda x: x[0])
right = env.from_collection(shipments, watermark_strategy=strategy).key_by(lambda x: x[0])
(
    left.interval_join(right)
    .between(Duration.of_seconds(0), Duration.of_seconds(3))
    .process(lambda payment, shipment: (payment, shipment))
    .print()
)
env.execute("interval-join")
