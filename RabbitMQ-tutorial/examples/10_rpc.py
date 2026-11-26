from __future__ import annotations

import uuid

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_queue("rpc_queue")
        broker.declare_queue("rpc_reply")

        def server(message, delivery):
            text = message.body.decode()
            value = sum(int(part) for part in text.split("+"))
            reply_to = message.properties["reply_to"]
            broker.publish(
                "",
                reply_to,
                f"{text}={value}",
                properties={
                    "correlation_id": message.properties["correlation_id"],
                    "delivery_mode": 1,
                },
            )
            broker.ack(delivery.delivery_tag)

        broker.add_consumer("rpc_queue", server)

        correlation_id = uuid.uuid4().hex
        broker.publish(
            "",
            "rpc_queue",
            b"20+22",
            properties={"reply_to": "rpc_reply", "correlation_id": correlation_id},
        )
        reply = broker.get("rpc_reply")
        assert reply is not None
        assert reply.message.properties["correlation_id"] == correlation_id
        print("reply:", reply.message.body.decode())
        broker.ack(reply.delivery_tag)
    finally:
        broker.close()
    print("OK 10_rpc")


if __name__ == "__main__":
    main()
