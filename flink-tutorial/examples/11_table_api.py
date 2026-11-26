"""MiniFlink 11：Table API 聚合与窗口列。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import Duration, TableEnvironment

env = TableEnvironment()
orders = env.from_elements(
    [
        {"user": "alice", "amount": 20, "ts": 1_000},
        {"user": "bob", "amount": 15, "ts": 1_500},
        {"user": "alice", "amount": 30, "ts": 6_000},
    ],
    name="orders",
)

by_user = orders.group_by("user").aggregate(total="SUM(amount)", orders="COUNT(*)")
print("by user:", by_user.execute())

windowed = (
    orders.window_tumble("ts", Duration.of_seconds(5))
    .group_by("window_start", "window_end")
    .aggregate(revenue="SUM(amount)")
)
print("by window:", windowed.execute())
