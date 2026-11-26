"""MiniFlink 13：Async I/O 形状的维表查询。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time

from miniflink import StreamExecutionEnvironment


def lookup(key):
    time.sleep(0.005)
    return {"id": key, "name": f"user-{key}"}


env = StreamExecutionEnvironment()
(
    env.from_collection([1, 2, 3, 4])
    .async_map(lookup, capacity=3)
    .map(lambda row: row["name"])
    .print()
)
env.execute("async-io")
