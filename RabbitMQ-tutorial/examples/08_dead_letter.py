from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_exchange("dlx", "direct")
        broker.declare_queue("failed")
        broker.bind_queue("failed", "dlx", "failed")
        broker.declare_queue(
            "payments",
            arguments={
                "x-dead-letter-exchange": "dlx",
                "x-dead-letter-routing-key": "failed",
            },
        )
        broker.publish("", "payments", b"card declined")
        attempt = broker.get("payments")
        assert attempt is not None
        broker.nack(attempt.delivery_tag, requeue=False)

        dead = broker.get("failed")
        assert dead is not None
        death = dead.message.headers["x-death"][0]
        print("reason:", death["reason"])
        print("original queue:", death["queue"])
        print("routing keys:", death["routing-keys"])
        assert death["reason"] == "rejected"
        broker.ack(dead.delivery_tag)
    finally:
        broker.close()
    print("OK 08_dead_letter")


if __name__ == "__main__":
    main()
