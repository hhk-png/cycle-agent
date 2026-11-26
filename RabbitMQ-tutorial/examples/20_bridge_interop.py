"""RabbitMQ -> append-only log bridge with at-least-once and idempotency.

A bridge is not a pipe. It reads from a source, transforms an event, writes to a
target, and only then acknowledges the source. A crash between the target write
and the source ack produces a redelivery; the target must deduplicate by a
stable event id. This example models that state machine with Mini-RabbitMQ as
the source and a small partitioned, append-only log as the target.

The checkpoint here mirrors a Kafka offset commit: advance it only after the
target write is durable and the source has been acknowledged. The example first
drains a batch normally, then injects a crash after the target write but before
the source ack, and proves that the redelivery does not create a second record.
"""

from __future__ import annotations

import json
import zlib

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


PARTITIONS = 3


class CrashAfterWrite(Exception):
    """Raised to model a process death after the target write, before ack."""


class AppendOnlyLog:
    """A tiny partitioned log: append-only, idempotent by event id."""

    def __init__(self, partitions: int = PARTITIONS) -> None:
        self.partitions: list[list[dict]] = [[] for _ in range(partitions)]
        self.seen: set[str] = set()
        self.appended = 0
        self.duplicates = 0

    def partition_for(self, key: str) -> int:
        return zlib.crc32(key.encode("utf-8")) % len(self.partitions)

    def append(self, key: str, event: dict) -> bool:
        event_id = event["id"]
        if event_id in self.seen:
            self.duplicates += 1
            return False
        self.seen.add(event_id)
        self.partitions[self.partition_for(key)].append(event)
        self.appended += 1
        return True

    def records(self) -> list[dict]:
        return [event for partition in self.partitions for event in partition]


class Bridge:
    """Read -> transform -> target write -> source ack -> checkpoint."""

    def __init__(self, broker: Broker, source: str, target: AppendOnlyLog) -> None:
        self.broker = broker
        self.source = source
        self.target = target
        self.checkpoint: str | None = None
        self.acked = 0

    def drain(self, *, crash_after_write: bool = False, owner: str = "bridge") -> int:
        moved = 0
        while True:
            delivery = self.broker.get(self.source, owner=owner)
            if delivery is None:
                return moved

            message = delivery.message
            payload = json.loads(message.body)
            event = {
                "id": message.message_id,
                "type": message.routing_key,
                "tenant": message.headers.get("tenant", "unknown"),
                "payload": payload,
            }
            key = payload["aggregate_id"]

            # 1) Durable target write. The event id is the idempotency barrier.
            self.target.append(key, event)

            if crash_after_write:
                # 2) Process dies here: target has the record, source was not
                #    acked. A real broker redelivers after the connection drops.
                raise CrashAfterWrite("bridge died after target write, before ack")

            # 3) Ack the source and advance the checkpoint only after the
            #    target write. Never ack first: that loses events on a crash.
            self.broker.ack(delivery.delivery_tag, owner=owner)
            self.checkpoint = event["id"]
            self.acked += 1
            moved += 1


def publish(broker: Broker, event_id: str, routing_key: str, aggregate: str) -> None:
    broker.publish(
        "orders",
        routing_key,
        json.dumps({"aggregate_id": aggregate, "event_id": event_id}),
        properties={
            "message_id": event_id,
            "delivery_mode": 2,
            "headers": {"tenant": "acme"},
        },
    )


def main() -> None:
    broker = Broker()
    try:
        broker.declare_exchange("orders", "topic", durable=True)
        broker.declare_queue("bridge.orders", durable=True)
        broker.bind_queue("bridge.orders", "orders", "order.*")

        publish(broker, "evt-1", "order.created", "order-1")
        publish(broker, "evt-2", "order.paid", "order-1")
        publish(broker, "evt-3", "order.created", "order-2")
        publish(broker, "evt-4", "order.paid", "order-2")
        publish(broker, "evt-5", "order.shipped", "order-1")

        target = AppendOnlyLog()
        bridge = Bridge(broker, "bridge.orders", target)
        assert bridge.drain() == 5
        assert bridge.checkpoint == "evt-5"

        # Inject a crash after the target write, before the source ack.
        publish(broker, "evt-6", "order.created", "order-3")
        try:
            bridge.drain(crash_after_write=True)
        except CrashAfterWrite as exc:
            print("injected crash:", exc)
        else:
            raise AssertionError("the crash injection did not fire")

        assert bridge.checkpoint == "evt-5"
        assert target.appended == 6

        # Simulate the broker detecting the dead consumer and requeueing the
        # unacknowledged delivery. A new bridge instance retries the event.
        broker.close_owner("bridge")
        assert bridge.drain() == 1

        assert bridge.checkpoint == "evt-6"
        assert bridge.acked == 6
        assert target.appended == 6
        assert target.duplicates == 1
        assert len(target.records()) == 6

        order_1 = [
            event["type"]
            for event in target.partitions[target.partition_for("order-1")]
            if event["payload"]["aggregate_id"] == "order-1"
        ]
        assert order_1 == ["order.created", "order.paid", "order.shipped"]

        print(
            "appended={} duplicates={} acked={} checkpoint={}".format(
                target.appended, target.duplicates, bridge.acked, bridge.checkpoint
            )
        )
        print("order-1 order:", " -> ".join(order_1))
    finally:
        broker.close()
    print("OK 20_bridge_interop")


if __name__ == "__main__":
    main()
