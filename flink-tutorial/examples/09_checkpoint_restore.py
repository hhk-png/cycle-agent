"""MiniFlink 09：检查点、source offset 与恢复。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pathlib import Path

from miniflink import FileCheckpointStorage, StreamExecutionEnvironment

root = Path(__file__).resolve().parent.parent
storage = FileCheckpointStorage(str(root / "build" / "checkpoints"))

env = StreamExecutionEnvironment()
env.enable_checkpointing(1_000, storage=storage, micro_batch_records=2)
(
    env.from_collection([1, 2, 3, 4, 5])
    .map(lambda value: value * 10)
    .sink_to(lambda value: print("value", value))
)

first = env.create_execution()
first.run(max_rounds=1)
checkpoint = first.checkpoints[-1]
print("checkpoint:", checkpoint.summary())

restored = env.create_execution()
restored.restore(checkpoint)
restored.run()
print("restored finished:", restored.is_finished(), "records:", restored.records_processed)
