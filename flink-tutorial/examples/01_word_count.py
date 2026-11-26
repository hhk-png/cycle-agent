"""MiniFlink 01：有界单词计数。

运行：python examples/01_word_count.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import StreamExecutionEnvironment

env = StreamExecutionEnvironment(parallelism=1)
lines = ["flink is fast", "flink is stateful", "stream processing with flink"]

(
    env.from_collection(lines)
    .flat_map(lambda line: line.split())
    .map(lambda word: (word, 1))
    .key_by(lambda pair: pair[0])
    .reduce(lambda a, b: (a[0], a[1] + b[1]))
    .sink_to(lambda value: print(f"{value[0]:8s} {value[1]}"))
)
job = env.execute("word-count")
print("job finished:", job.is_finished())
