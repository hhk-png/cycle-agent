from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker
from mini_rabbitmq.client import AMQPClient
from mini_rabbitmq.server import AMQPServer


def main() -> None:
    broker = Broker()
    server = AMQPServer(broker, port=0)
    server.start()
    client = AMQPClient(port=server.port)
    try:
        client.connect()
        channel = client.channel()
        client.confirm_select(channel=channel)
        client.declare_exchange("confirmed-ex", "direct", channel=channel)
        client.declare_queue("confirmed-q", channel=channel)
        client.bind_queue("confirmed-q", "confirmed-ex", "ok", channel=channel)

        client.publish("confirmed-ex", "ok", b"persisted", channel=channel)
        assert client.wait_for_confirms(channel=channel)

        client.publish(
            "confirmed-ex", "missing", b"returned", mandatory=True, channel=channel
        )
        assert client.wait_for_confirms(channel=channel)
        returned = client.returned_messages()
        print("confirmed publishes:", 2)
        print("returned:", returned[0].message.body.decode())

        got = client.get("confirmed-q", channel=channel)
        assert got is not None and got.message.body == b"persisted"
        client.ack(got.delivery_tag, channel=channel)
    finally:
        client.close()
        server.stop()
        broker.close()
    print("OK 09_publisher_confirms")


if __name__ == "__main__":
    main()
