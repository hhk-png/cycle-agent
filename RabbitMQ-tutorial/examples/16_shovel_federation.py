from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker
from mini_rabbitmq.links import FederationLink, Shovel


def main() -> None:
    east = Broker()
    west = Broker()
    link = None
    try:
        east.declare_queue("east-jobs")
        west.declare_queue("west-jobs")
        east.publish("", "east-jobs", b"job-1")
        shovel = Shovel(
            "east-to-west",
            east,
            "east-jobs",
            west,
            destination_routing_key="west-jobs",
        )
        assert shovel.run_once() == 1
        moved = west.get("west-jobs")
        print("shovel moved:", moved.message.body.decode())
        west.ack(moved.delivery_tag)

        east.declare_exchange("east-events", "topic")
        west.declare_exchange("west-events", "topic")
        west.declare_queue("west-audit")
        west.bind_queue("west-audit", "west-events", "orders.#")
        link = FederationLink(
            "east-upstream",
            east,
            "east-events",
            west,
            "west-events",
            routing_key_pattern=r"orders\..*",
        )
        link.start()
        east.publish("east-events", "orders.created", b"order-9")
        replicated = west.get("west-audit")
        assert replicated is not None
        print("federation replicated:", replicated.message.body.decode())
        print("origin chain:", replicated.message.headers["x-federation-origin"])
    finally:
        if link is not None:
            link.stop()
        east.close()
        west.close()
    print("OK 16_shovel_federation")


if __name__ == "__main__":
    main()
