from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_exchange("orders", "topic")
        broker.declare_exchange("orders-dlx", "fanout")
        broker.declare_queue(
            "inventory",
            arguments={
                "x-dead-letter-exchange": "orders-dlx",
                "x-dead-letter-routing-key": "failed",
            },
        )
        broker.declare_queue("audit")
        broker.declare_queue("order-failures")
        broker.bind_queue("inventory", "orders", "order.created")
        broker.bind_queue("audit", "orders", "#")
        broker.bind_queue("order-failures", "orders-dlx", "")

        inventory = []
        audit = []

        def consume_inventory(message, delivery):
            try:
                if b"broken" in message.body:
                    raise ValueError("invalid order")
                inventory.append(message.body.decode())
                broker.ack(delivery.delivery_tag)
            except ValueError:
                broker.nack(delivery.delivery_tag, requeue=False)

        def consume_audit(message, delivery):
            audit.append(message.body.decode())
            broker.ack(delivery.delivery_tag)

        broker.add_consumer("inventory", consume_inventory, prefetch_count=1)
        broker.add_consumer("audit", consume_audit)

        broker.publish(
            "orders",
            "order.created",
            b"order-100",
            properties={"delivery_mode": 2},
        )
        broker.publish(
            "orders",
            "order.created",
            b"broken-order",
            properties={"delivery_mode": 2},
        )

        failures = broker.vhost().get_queue("order-failures").messages_ready
        print("inventory accepted:", inventory)
        print("audit observed:", audit)
        print("order failures:", failures)
        assert inventory == ["order-100"]
        assert len(audit) == 2
        assert failures == 1
    finally:
        broker.close()
    print("OK 18_order_pipeline")


if __name__ == "__main__":
    main()
