"""MiniFlink 16：文件 source 与原子文件 sink。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pathlib import Path

from miniflink import StreamExecutionEnvironment

root = Path(__file__).resolve().parent.parent
build = root / "build"
build.mkdir(exist_ok=True)
source = build / "input.txt"
target = build / "output.txt"
source.write_text("flink\nstream\nflink\n", encoding="utf-8")

env = StreamExecutionEnvironment()
(
    env.read_text_file(str(source))
    .flat_map(lambda line: [line.strip()])
    .map(lambda word: (word, 1))
    .key_by(lambda pair: pair[0])
    .reduce(lambda a, b: (a[0], a[1] + b[1]))
    .write_as_text(str(target), formatter=lambda pair: f"{pair[0]},{pair[1]}")
)
env.execute("file-pipeline")
print(target.read_text(encoding="utf-8"))
