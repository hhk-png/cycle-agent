"""MiniFlink 10：内存 Kafka 的 source/sink 与 offset。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    InMemoryKafka,
    KafkaSink,
    StreamExecutionEnvironment,
    WatermarkStrategy,
)

bus = InMemoryKafka()
bus.create_topic("orders", 1)
for index in range(5):
    bus.publish("orders", {"id": index, "amount": (index + 1) * 10}, timestamp=1_000 + index)

env = StreamExecutionEnvironment()
stream = env.from_kafka(
    bus,
    "orders",
    group_id="demo",
    watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
).map(lambda order: {**order, "amount": order["amount"] * 2})
env.add_sink(lambda: KafkaSink(bus, "enriched"), stream, name="kafka-sink")
env.execute("kafka-pipeline")

print("enriched messages:", [message.value for message in bus.messages("enriched")])
