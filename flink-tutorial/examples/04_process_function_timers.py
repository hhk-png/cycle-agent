"""MiniFlink 04：KeyedProcessFunction、状态与事件时间定时器。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    KeyedProcessFunction,
    Record,
    StreamExecutionEnvironment,
    ValueStateDescriptor,
    WatermarkStrategy,
)


class TrackLastSeen(KeyedProcessFunction):
    def open(self, runtime):
        self.last_seen = None

    def process_element(self, value, ctx, out):
        if self.last_seen is None:
            self.last_seen = ctx.get_state(ValueStateDescriptor("last", lambda: None))
        self.last_seen.update(ctx.timestamp)
        ctx.timer_service.register_event_time_timer(ctx.timestamp + 1_000, ctx.current_key)
        out.collect(("event", value, ctx.timestamp))

    def on_timer(self, timestamp, ctx, out):
        last = self.last_seen.get() if self.last_seen else None
        out.collect(("timer", ctx.current_key, timestamp, last))


events = [
    Record(("u1", "view"), timestamp=1_000),
    Record(("u1", "buy"), timestamp=1_500),
    Record(("u2", "view"), timestamp=2_000),
]

env = StreamExecutionEnvironment()
(
    env.from_collection(
        events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
    )
    .key_by(lambda event: event[0])
    .process(TrackLastSeen())
    .print()
)
env.execute("timers")
