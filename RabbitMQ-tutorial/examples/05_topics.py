from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_exchange("events", "topic")
        bindings = {
            "all_orders": "order.#",
            "created_only": "order.*.created",
            "payments": "payment.#",
        }
        for queue, pattern in bindings.items():
            broker.declare_queue(queue)
            broker.bind_queue(queue, "events", pattern)

        broker.publish("events", "order.eu.created", b"order-1")
        broker.publish("events", "payment.eu.captured", b"payment-1")

        observed = {}
        for queue in bindings:
            delivery = broker.get(queue)
            observed[queue] = delivery.message.body if delivery else None
            print(f"{queue}: {observed[queue].decode() if observed[queue] else '<empty>'}")
            if delivery:
                broker.ack(delivery.delivery_tag)
        assert observed["created_only"] == b"order-1"
        assert observed["all_orders"] == b"order-1"
        assert observed["payments"] == b"payment-1"
    finally:
        broker.close()
    print("OK 05_topics")


if __name__ == "__main__":
    main()
