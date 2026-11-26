from __future__ import annotations

import time

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_queue("priority", arguments={"x-max-priority": 10})
        for priority, body in ((1, "low"), (9, "urgent"), (5, "normal")):
            broker.publish(
                "", "priority", body, properties={"priority": priority}
            )
        order = []
        while True:
            delivery = broker.get("priority")
            if delivery is None:
                break
            order.append((delivery.message.body.decode(), delivery.message.priority))
            broker.ack(delivery.delivery_tag)
        print("priority order:", order)
        assert [item[0] for item in order] == ["urgent", "normal", "low"]

        broker.declare_queue("ttl", arguments={"x-message-ttl": 20})
        broker.publish("", "ttl", b"expires soon")
        time.sleep(0.05)
        assert broker.get("ttl") is None
        print("ttl: expired as expected")
    finally:
        broker.close()
    print("OK 07_priority_ttl")


if __name__ == "__main__":
    main()
