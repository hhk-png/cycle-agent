"""Outbox + idempotent consumer, end to end.

Business state and the outbox row are committed in one SQLite transaction.
A publisher sends outbox rows to Mini-RabbitMQ; the consumer records the
message_id in a processed table before applying the business side effect.
The script then simulates two real failure modes:

* the publisher crashed after publishing but before marking the row sent;
* the consumer crashed after the side effect but before acking.

Both failures produce duplicate deliveries with the same message_id. The
business effect must still happen exactly once.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL
        );
        CREATE TABLE inventory (
            order_id TEXT PRIMARY KEY
        );
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aggregate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE processed (
            message_id TEXT PRIMARY KEY,
            consumer TEXT NOT NULL
        );
        """
    )
    return conn


def create_order(conn: sqlite3.Connection, order_id: str) -> None:
    """Commit business state and the outbox row in one local transaction."""
    with conn:
        conn.execute(
            "INSERT INTO orders(id, status) VALUES (?, 'created')", (order_id,)
        )
        conn.execute(
            "INSERT INTO outbox(aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (order_id, "order.created", json.dumps({"order_id": order_id})),
        )


def publish_outbox(conn: sqlite3.Connection, broker: Broker) -> int:
    """Publish pending rows and mark them sent only after a routed publish."""
    rows = conn.execute(
        "SELECT id, event_type, payload FROM outbox "
        "WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    for row_id, event_type, payload in rows:
        result = broker.publish(
            "orders",
            event_type,
            payload,
            properties={"delivery_mode": 2, "message_id": f"outbox-{row_id}"},
            mandatory=True,
        )
        if result.routed == 0:
            raise RuntimeError(f"event {row_id} was not routed")
        with conn:
            conn.execute(
                "UPDATE outbox SET status = 'sent' WHERE id = ?", (row_id,)
            )
    return len(rows)


def handle(conn: sqlite3.Connection, message) -> bool:
    """Apply the side effect once. Return False for a duplicate delivery."""
    message_id = message.message_id
    try:
        with conn:
            # The unique key is the idempotency barrier. It must be written
            # in the same transaction as the business effect.
            conn.execute(
                "INSERT INTO processed(message_id, consumer) "
                "VALUES (?, 'inventory')",
                (message_id,),
            )
            order_id = json.loads(message.body)["order_id"]
            conn.execute(
                "INSERT INTO inventory(order_id) VALUES (?)", (order_id,)
            )
        return True
    except sqlite3.IntegrityError:
        return False


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(Path(tmp) / "business.db")
        broker = Broker()
        try:
            broker.declare_exchange("orders", "topic", durable=True)
            broker.declare_queue("inventory.order-created", durable=True)
            broker.bind_queue(
                "inventory.order-created", "orders", "order.created"
            )

            create_order(conn, "order-100")
            assert publish_outbox(conn, broker) == 1

            # Failure mode 1: publisher sent the event, then crashed before
            # marking the outbox row. On restart it republishes the same
            # message_id.
            with conn:
                conn.execute("UPDATE outbox SET status = 'pending'")
            assert publish_outbox(conn, broker) == 1

            first = broker.get("inventory.order-created")
            assert first is not None
            assert handle(conn, first.message) is True
            # Failure mode 2: consumer applied the effect, then crashed
            # before ack. The broker requeues the delivery.
            broker.nack(first.delivery_tag, requeue=True)

            deliveries = 1
            while True:
                delivery = broker.get("inventory.order-created")
                if delivery is None:
                    break
                deliveries += 1
                assert handle(conn, delivery.message) is False
                broker.ack(delivery.delivery_tag)

            inventory = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
            processed = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
            sent = conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent'"
            ).fetchone()[0]

            print("deliveries observed:", deliveries)
            print("inventory rows:", inventory)
            print("processed ids:", processed)
            print("outbox sent:", sent)
            assert deliveries == 3
            assert inventory == 1
            assert processed == 1
            assert sent == 1
        finally:
            broker.close()
            conn.close()
    print("OK 19_outbox_idempotency")


if __name__ == "__main__":
    main()
