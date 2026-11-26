from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker
from mini_rabbitmq.client import AMQPClient, ChannelClosedError
from mini_rabbitmq.server import AMQPServer


def main() -> None:
    broker = Broker()
    broker.declare_exchange("events", "topic")
    broker.add_user("app", "change-me", tags=("monitoring",))
    broker.set_permissions(
        "app",
        "/",
        configure="^allowed$",
        write="^events$",
        read="^allowed$",
    )
    server = AMQPServer(broker, port=0)
    server.start()
    client = AMQPClient(
        port=server.port, username="app", password="change-me"
    )
    try:
        client.connect()
        channel = client.channel()
        client.declare_queue("allowed", channel=channel)
        client.bind_queue("allowed", "events", "#", channel=channel)
        client.publish("events", "orders.created", b"x", channel=channel)
        print("allowed operation succeeded")

        try:
            client.declare_queue("forbidden", channel=channel)
        except ChannelClosedError as exc:
            print("denied operation:", exc.code, exc.message)

        # 信道级错误不会杀死连接；开新信道继续工作。
        new_channel = client.channel()
        assert client.declare_queue("allowed", passive=True, channel=new_channel)
        print("connection survived channel error")
    finally:
        client.close()
        server.stop()
        broker.close()
    print("OK 17_security_acl")


if __name__ == "__main__":
    main()
