"""MiniFlink 03：会话窗口。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    Duration,
    Record,
    SessionWindows,
    StreamExecutionEnvironment,
    SumAggregate,
    WatermarkStrategy,
)

events = [
    Record(("u1", 1), timestamp=1_000),
    Record(("u1", 2), timestamp=1_500),
    Record(("u1", 3), timestamp=5_000),
    Record(("u2", 10), timestamp=1_200),
]

env = StreamExecutionEnvironment()
(
    env.from_collection(
        events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
    )
    .key_by(lambda event: event[0])
    .window(SessionWindows.with_gap(Duration.of_seconds(1)))
    .aggregate(SumAggregate(1))
    .print()
)
env.execute("session-window")
