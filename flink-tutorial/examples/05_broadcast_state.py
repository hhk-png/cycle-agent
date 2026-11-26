"""MiniFlink 05：广播状态做规则动态更新。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    BroadcastProcessFunction,
    MapStateDescriptor,
    StreamExecutionEnvironment,
)


class Enrich(BroadcastProcessFunction):
    def process_element(self, order, ctx, out):
        rules = ctx.get_broadcast_state(MapStateDescriptor("products", dict))
        out.collect((order["sku"], rules.get(order["sku"], "unknown")))

    def process_broadcast_element(self, rule, ctx, out):
        rules = ctx.get_broadcast_state(MapStateDescriptor("products", dict))
        rules.put(rule[0], rule[1])


env = StreamExecutionEnvironment()
orders = env.from_collection(
    [{"id": 1, "sku": "p1"}, {"id": 2, "sku": "p2"}, {"id": 3, "sku": "p9"}]
)
rules = env.from_collection([("p1", "book"), ("p2", "pen")])
(
    orders.key_by(lambda order: order["id"])
    .connect(rules.broadcast())
    .process(Enrich())
    .print()
)
env.execute("broadcast-state")
