from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_exchange("logs", "fanout")
        for queue in ("audit", "billing", "alerts"):
            broker.declare_queue(queue)
            broker.bind_queue(queue, "logs", "")

        broker.publish("logs", "", b"order.created")
        for queue in ("audit", "billing", "alerts"):
            delivery = broker.get(queue)
            assert delivery is not None
            print(f"{queue}: {delivery.message.body.decode()}")
            broker.ack(delivery.delivery_tag)
    finally:
        broker.close()
    print("OK 03_pubsub_fanout")


if __name__ == "__main__":
    main()
