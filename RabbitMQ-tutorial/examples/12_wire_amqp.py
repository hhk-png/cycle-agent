from __future__ import annotations

import threading

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker
from mini_rabbitmq.client import AMQPClient
from mini_rabbitmq.server import AMQPServer


def main() -> None:
    broker = Broker()
    server = AMQPServer(broker, port=0, heartbeat=2)
    server.start()
    client = AMQPClient(port=server.port, heartbeat=2)
    received = []
    done = threading.Event()
    try:
        client.connect()
        channel = client.channel()
        client.declare_exchange("wire-events", "topic", durable=True, channel=channel)
        client.declare_queue("wire-audit", durable=True, channel=channel)
        client.bind_queue("wire-audit", "wire-events", "order.#", channel=channel)
        client.qos(10, channel=channel)

        def consume(message, info):
            received.append(message.body.decode())
            client.ack(info.delivery_tag, channel=channel)
            done.set()

        client.consume("wire-audit", consume, channel=channel)
        client.publish(
            "wire-events",
            "order.created",
            b'{"order": 7}',
            properties={"delivery_mode": 2},
            channel=channel,
        )
        assert done.wait(2)
        print("wire received:", received[0])
        print("connection info:", server.connection_list()[0]["protocol"])
    finally:
        client.close()
        server.stop()
        broker.close()
    print("OK 12_wire_amqp")


if __name__ == "__main__":
    main()
