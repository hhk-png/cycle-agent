"""MiniFlink 15：并行度、hash 分区与聚合。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import StreamExecutionEnvironment

env = StreamExecutionEnvironment(parallelism=3)
words = ["a", "b", "a", "c", "b", "a", "d", "c", "b", "a"]
(
    env.from_collection(words, parallelism=3)
    .map(lambda word: (word, 1))
    .key_by(lambda pair: pair[0])
    .reduce(lambda left, right: (left[0], left[1] + right[1]))
    .print()
)
job = env.execute("parallel-word-count")
print("processed:", job.records_processed)
