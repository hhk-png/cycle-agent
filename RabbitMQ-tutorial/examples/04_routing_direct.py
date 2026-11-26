from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_exchange("direct_logs", "direct")
        for queue, key in (("errors", "error"), ("warnings", "warning"), ("all", "#")):
            broker.declare_queue(queue)
            # fanout semantics for the catch-all queue would be built with a
            # different exchange; here "all" is bound to both keys explicitly.
            if queue == "all":
                broker.bind_queue(queue, "direct_logs", "error")
                broker.bind_queue(queue, "direct_logs", "warning")
            else:
                broker.bind_queue(queue, "direct_logs", key)

        broker.publish("direct_logs", "error", b"db down")
        broker.publish("direct_logs", "warning", b"disk 80%")

        for queue in ("errors", "warnings", "all"):
            delivery = broker.get(queue)
            assert delivery is not None
            print(f"{queue}: {delivery.message.body.decode()}")
            broker.ack(delivery.delivery_tag)
    finally:
        broker.close()
    print("OK 04_routing_direct")


if __name__ == "__main__":
    main()
