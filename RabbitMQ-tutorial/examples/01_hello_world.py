from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_queue("hello")
        broker.publish("", "hello", b"Hello, RabbitMQ!")
        delivery = broker.get("hello")
        assert delivery is not None
        print("received:", delivery.message.body.decode())
        broker.ack(delivery.delivery_tag)
        assert broker.get("hello") is None
    finally:
        broker.close()
    print("OK 01_hello_world")


if __name__ == "__main__":
    main()
