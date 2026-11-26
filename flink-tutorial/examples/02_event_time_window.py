"""MiniFlink 02：事件时间滚动窗口与水位线。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    Duration,
    Record,
    StreamExecutionEnvironment,
    SumAggregate,
    TumblingEventTimeWindows,
    WatermarkStrategy,
)

events = [
    Record(("order", "book", 20), timestamp=1_000),
    Record(("order", "pen", 5), timestamp=1_800),
    Record(("order", "book", 30), timestamp=2_400),
    Record(("order", "pen", 7), timestamp=4_200),
]

strategy = WatermarkStrategy.for_bounded_out_of_orderness(500)
env = StreamExecutionEnvironment()
(
    env.from_collection(events, watermark_strategy=strategy)
    .key_by(lambda event: event[1])
    .window(TumblingEventTimeWindows.of(Duration.of_seconds(2)))
    .aggregate(SumAggregate(2))
    .map(lambda w: f"{w.key}: window={w.window} revenue={w.value}")
    .print()
)
env.execute("event-time-window")
