"""MiniFlink 14：侧输出拆分正常与异常事件。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import OutputTag, ProcessFunction, StreamExecutionEnvironment


class Validate(ProcessFunction):
    def process_element(self, value, ctx, out):
        if value >= 0:
            out.collect(value)
        else:
            ctx.output(OutputTag("invalid"), value)


env = StreamExecutionEnvironment()
stream = env.from_collection([3, -1, 5, -7])
valid = stream.process(Validate())
valid.get_side_output(OutputTag("invalid")).map(lambda value: f"invalid={value}").print()
valid.print()
env.execute("side-outputs")
