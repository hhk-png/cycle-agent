from __future__ import annotations

import base64
import json
import urllib.request

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker
from mini_rabbitmq.management import ManagementServer
from mini_rabbitmq.server import AMQPServer


def fetch(port: int, path: str):
    token = base64.b64encode(b"guest:guest").decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Basic {token}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def main() -> None:
    broker = Broker()
    server = AMQPServer(broker, port=0)
    server.start()
    management = ManagementServer(broker, port=0, amqp_server=server)
    management.start()
    try:
        broker.declare_queue("api-demo")
        broker.publish("", "api-demo", b"observable")
        overview = json.loads(fetch(management.port, "/api/overview"))
        queues = json.loads(fetch(management.port, "/api/queues"))
        metrics = fetch(management.port, "/metrics")
        print("management port:", management.port)
        print("queue count:", overview["object_totals"]["queues"])
        print("queue message count:", queues[0]["messages_ready"])
        assert "mini_rabbitmq_messages_published_total" in metrics
    finally:
        management.stop()
        server.stop()
        broker.close()
    print("OK 13_management_api")


if __name__ == "__main__":
    main()
