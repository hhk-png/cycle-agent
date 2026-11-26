"""Zero-downtime migration from a v1 topology/schema to v2.

The migration follows expand -> dual publish + shadow consume -> backfill ->
cutover -> rollback drill -> re-cutover -> decommission. A business row is
keyed by ``order_id`` and written with ``INSERT OR IGNORE``, so at-least-once
redelivery during the migration cannot double-book an order.

The example models the important property that a message system cannot be
"restarted all at once": old and new producers/consumers overlap in time. The
v2 consumer must therefore understand both v1 and v2 payloads until the
backfill and the rollback window are over.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


V1_EXCHANGE = "orders.v1"
V1_QUEUE = "orders.v1.worker"
V2_EXCHANGE = "orders.v2"
V2_QUEUE = "orders.v2.worker"


@dataclass
class Ledger:
    """A tiny idempotent projector. The primary key is the business key."""

    conn: sqlite3.Connection
    table: str
    applied: int = 0
    duplicates: int = 0

    def apply(self, order_id: str, amount: float, currency: str) -> bool:
        cursor = self.conn.execute(
            f"INSERT OR IGNORE INTO {self.table}"
            "(order_id, amount, currency) VALUES (?, ?, ?)",
            (order_id, amount, currency),
        )
        if cursor.rowcount == 0:
            self.duplicates += 1
            return False
        self.applied += 1
        return True

    def ids(self) -> set[str]:
        rows = self.conn.execute(f"SELECT order_id FROM {self.table}").fetchall()
        return {row[0] for row in rows}


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE business (
            order_id TEXT PRIMARY KEY,
            amount   REAL NOT NULL,
            currency TEXT NOT NULL
        );
        CREATE TABLE shadow (
            order_id TEXT PRIMARY KEY,
            amount   REAL NOT NULL,
            currency TEXT NOT NULL
        );
        """
    )
    return conn


def event_v1(order_id: str, amount: float) -> dict:
    return {"id": order_id, "amount": amount}


def event_v2(order_id: str, amount: float, currency: str = "CNY") -> dict:
    return {
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "schema_version": 2,
    }


class Producer:
    """A producer that can publish v1 only, v2 only, or both during migration."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.mode = "v1"  # v1 | v2 | dual
        self.published = 0

    def publish(self, order_id: str, amount: float) -> None:
        message_id = f"evt-{order_id}"
        properties = {"message_id": message_id, "delivery_mode": 2}
        if self.mode in ("v1", "dual"):
            result = self.broker.publish(
                V1_EXCHANGE,
                "order.created",
                json.dumps(event_v1(order_id, amount)),
                properties=properties,
                mandatory=True,
            )
            if result.routed == 0:
                raise RuntimeError(f"{message_id} was not routed to v1")
        if self.mode in ("v2", "dual"):
            result = self.broker.publish(
                V2_EXCHANGE,
                "orders.created",
                json.dumps(event_v2(order_id, amount)),
                properties=properties,
                mandatory=True,
            )
            if result.routed == 0:
                raise RuntimeError(f"{message_id} was not routed to v2")
        self.published += 1


class Migration:
    """Owns the topology, both consumers and the phase bookkeeping."""

    def __init__(self, broker: Broker, conn: sqlite3.Connection) -> None:
        self.broker = broker
        self.business = Ledger(conn, "business")
        self.shadow = Ledger(conn, "shadow")
        self.producer = Producer(broker)
        self.v2_mode = "shadow"  # shadow | primary
        self.v1_serving = True
        self.v2_serving = False
        self.phase_log: list[str] = []
        self.serving_gap = 0

    # ---- topology -----------------------------------------------------
    def declare_v1(self) -> None:
        self.broker.declare_exchange(V1_EXCHANGE, "topic", durable=True)
        self.broker.declare_queue(V1_QUEUE, durable=True)
        self.broker.bind_queue(V1_QUEUE, V1_EXCHANGE, "order.created")

    def declare_v2(self) -> None:
        self.broker.declare_exchange(V2_EXCHANGE, "topic", durable=True)
        self.broker.declare_queue(V2_QUEUE, durable=True)
        self.broker.bind_queue(V2_QUEUE, V2_EXCHANGE, "orders.created")

    # ---- consumers ----------------------------------------------------
    def _decode_v1(self, body: bytes) -> tuple[str, float, str]:
        payload = json.loads(body)
        return payload["id"], float(payload["amount"]), "CNY"

    def _decode_v2(self, body: bytes) -> tuple[str, float, str]:
        """Read both schemas: v2 consumers must survive the v1 backfill."""
        payload = json.loads(body)
        order_id = payload.get("order_id") or payload["id"]
        currency = payload.get("currency", "CNY")
        return order_id, float(payload["amount"]), currency

    def handle_v1(self, delivery) -> None:
        order_id, amount, currency = self._decode_v1(delivery.message.body)
        self.business.apply(order_id, amount, currency)
        self.broker.ack(delivery.delivery_tag, owner="v1-consumer")

    def handle_v2(self, delivery) -> None:
        order_id, amount, currency = self._decode_v2(delivery.message.body)
        target = self.shadow if self.v2_mode == "shadow" else self.business
        target.apply(order_id, amount, currency)
        self.broker.ack(delivery.delivery_tag, owner="v2-consumer")

    def drain_v1(self) -> int:
        return self._drain(V1_QUEUE, "v1-consumer", self.handle_v1)

    def drain_v2(self) -> int:
        return self._drain(V2_QUEUE, "v2-consumer", self.handle_v2)

    def _drain(
        self, queue: str, owner: str, handler: Callable[[object], None]
    ) -> int:
        count = 0
        while True:
            delivery = self.broker.get(queue, owner=owner)
            if delivery is None:
                return count
            try:
                handler(delivery)
            except Exception:
                # A malformed payload is parked rather than hot-looped. The
                # example has no DLX; dropping is explicit and observable.
                self.broker.nack(delivery.delivery_tag, requeue=False, owner=owner)
                raise
            count += 1

    # ---- phases -------------------------------------------------------
    def _record(self, phase: str) -> None:
        if not (self.v1_serving or self.v2_serving):
            self.serving_gap += 1
        self.phase_log.append(
            f"{phase}: business={self.business.applied} "
            f"shadow={self.shadow.applied} "
            f"v1={'on' if self.v1_serving else 'off'} "
            f"v2={self.v2_mode}{'/on' if self.v2_serving else '/off'}"
        )

    def run(self) -> None:
        # Phase 0: v1 is the only live topology.
        self.declare_v1()
        for number in range(1, 6):
            self.producer.publish(f"order-{number}", 100.0 + number)
        self.drain_v1()
        self._record("phase 0  v1 live")

        # Phase 1: expand. Declare v2, dual publish, v2 consumes in shadow.
        self.declare_v2()
        self.v2_serving = True
        self.producer.mode = "dual"
        for number in range(6, 9):
            self.producer.publish(f"order-{number}", 100.0 + number)
        self.drain_v1()
        self.drain_v2()
        self._record("phase 1  expand + shadow")

        # Phase 2: backfill historical events into v2 only. v2 must read the
        # v1 payload shape, which is why the decoder accepts both schemas.
        for number in range(1, 6):
            self.broker.publish(
                V2_EXCHANGE,
                "orders.created",
                json.dumps(event_v1(f"order-{number}", 100.0 + number)),
                properties={"message_id": f"backfill-{number}", "delivery_mode": 2},
                mandatory=True,
            )
        self.drain_v2()
        self._record("phase 2  backfill")

        # Phase 3: cutover. v2 becomes the primary writer; v1 stops serving.
        self.v2_mode = "primary"
        self.v1_serving = False
        self.producer.mode = "v2"
        for number in range(9, 11):
            self.producer.publish(f"order-{number}", 100.0 + number)
        self.drain_v2()
        self._record("phase 3  cutover")

        # Phase 4: rollback drill. v1 takes traffic again while v2 shadows it.
        # The old consumer must still be deployable and the new consumer must
        # tolerate the old schema. Then re-cut to v2.
        self.v2_mode = "shadow"
        self.v1_serving = True
        self.producer.mode = "dual"
        self.producer.publish("order-11", 111.0)
        self.drain_v1()
        self.drain_v2()
        self._record("phase 4  rollback drill")

        self.v2_mode = "primary"
        self.v1_serving = False
        self.producer.mode = "v2"
        self.producer.publish("order-12", 112.0)
        self.drain_v2()
        self._record("phase 5  re-cutover")

        # Phase 6: a duplicate publish (same message_id) must not double-book.
        self.producer.publish("order-12", 112.0)
        self.drain_v2()
        self._record("phase 6  duplicate publish")

        # Phase 7: contract. Drain, verify empty, then remove v1.
        self.drain_v1()
        self.drain_v2()
        self.broker.delete_queue(V1_QUEUE, if_empty=True)
        self.broker.delete_exchange(V1_EXCHANGE, if_unused=True)
        self._record("phase 7  decommission v1")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(Path(tmp) / "migration.db")
        broker = Broker()
        try:
            migration = Migration(broker, conn)
            migration.run()

            for line in migration.phase_log:
                print(line)

            business_ids = migration.business.ids()
            shadow_ids = migration.shadow.ids()
            expected = {f"order-{number}" for number in range(1, 13)}

            print(f"business rows={len(business_ids)} duplicates={migration.business.duplicates}")
            print(f"shadow rows={len(shadow_ids)} duplicates={migration.shadow.duplicates}")
            print(f"serving gaps={migration.serving_gap}")

            assert business_ids == expected, "the migration lost or invented orders"
            assert shadow_ids == {f"order-{number}" for number in list(range(1, 9)) + [11]}
            assert migration.business.duplicates == 1
            assert migration.serving_gap == 0
            assert V1_QUEUE not in broker.queue_names()
            assert V1_EXCHANGE not in broker.exchange_names()
            assert broker.get(V2_QUEUE) is None

            print("business ids:", " ".join(sorted(business_ids)))
            print("queues: v1=0 v2=0")
        finally:
            broker.close()
            conn.close()
    print("OK 21_zero_downtime_migration")


if __name__ == "__main__":
    main()
