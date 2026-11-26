"""MiniFlink 08：CEP 检测短时间连续失败。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    Duration,
    Pattern,
    Record,
    StreamExecutionEnvironment,
    WatermarkStrategy,
    pattern,
)

events = [
    Record(("u1", "fail"), timestamp=1_000),
    Record(("u1", "fail"), timestamp=1_500),
    Record(("u1", "ok"), timestamp=2_000),
    Record(("u2", "fail"), timestamp=3_000),
    Record(("u2", "ok"), timestamp=4_000),
]

login_pattern = (
    Pattern.begin("first")
    .where(lambda event: event[1] == "fail")
    .next("second")
    .where(lambda event: event[1] == "fail")
    .within(Duration.of_seconds(5))
)

env = StreamExecutionEnvironment()
stream = env.from_collection(
    events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
)
(
    pattern(stream.key_by(lambda event: event[0]), login_pattern)
    .select(lambda match: (match.key, match["first"], match["second"]))
    .print()
)
env.execute("cep")
