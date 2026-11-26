"""MiniFlink 06：事件时间窗口 join。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    Duration,
    Record,
    StreamExecutionEnvironment,
    TumblingEventTimeWindows,
    WatermarkStrategy,
)

strategy = WatermarkStrategy.for_bounded_out_of_orderness(0)
orders = [
    Record(("o1", "p1", 2), timestamp=1_000),
    Record(("o2", "p2", 1), timestamp=2_000),
]
prices = [
    Record(("p1", 30), timestamp=1_200),
    Record(("p2", 10), timestamp=2_100),
]

env = StreamExecutionEnvironment()
(
    env.from_collection(orders, watermark_strategy=strategy)
    .join(env.from_collection(prices, watermark_strategy=strategy))
    .where(lambda order: order[1])
    .equal_to(lambda price: price[0])
    .window(TumblingEventTimeWindows.of(Duration.of_seconds(5)))
    .apply(lambda order, price: (order[0], price[0], order[2] * price[1]))
    .print()
)
env.execute("window-join")
