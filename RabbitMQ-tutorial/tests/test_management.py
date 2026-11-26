from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from mini_rabbitmq.client import AMQPClient
from mini_rabbitmq.management import ManagementServer


@pytest.fixture
def management(server):
    instance = ManagementServer(server.broker, port=0, amqp_server=server)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def auth_header(username="guest", password="guest"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def call(management, method, path, *, body=None, headers=None, username="guest", password="guest"):
    data = None
    request_headers = dict(headers if headers is not None else auth_header(username, password))
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{management.port}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            if response.headers.get_content_type() == "application/json":
                payload = json.loads(raw) if raw else None
            else:
                payload = raw.decode("utf-8") if raw else ""
            return response.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = json.loads(raw) if raw else None
        return exc.code, payload


def test_management_requires_auth(management):
    status, payload = call(management, "GET", "/api/overview", headers={})
    assert status == 401
    assert payload["error"] == "not_authorized"
    status, _ = call(management, "GET", "/api/overview", password="wrong")
    assert status == 401


def test_whoami(management):
    status, payload = call(management, "GET", "/api/whoami")
    assert status == 200
    assert payload["name"] == "guest"


def test_overview_and_totals(management, broker):
    broker.declare_queue("q")
    broker.publish("", "q", b"x")
    status, payload = call(management, "GET", "/api/overview")
    assert status == 200
    assert payload["object_totals"]["queues"] == 1
    assert payload["queue_totals"]["messages"] == 1
    assert any(item["protocol"] == "amqp" for item in payload["listeners"])


def test_queue_endpoints(management, broker):
    broker.declare_queue("q")
    broker.publish("", "q", b"x")
    status, rows = call(management, "GET", "/api/queues")
    assert status == 200
    assert rows[0]["name"] == "q"
    status, row = call(management, "GET", "/api/queues/%2F/q")
    assert status == 200
    assert row["messages_ready"] == 1
    status, _ = call(management, "GET", "/api/queues/%2F/missing")
    assert status == 404


def test_queue_purge_and_delete(management, broker):
    broker.declare_queue("q")
    for i in range(3):
        broker.publish("", "q", f"m{i}")
    status, payload = call(management, "POST", "/api/queues/%2F/q/purge")
    assert status == 200
    assert payload["messages_purged"] == 3
    broker.publish("", "q", b"x")
    status, payload = call(management, "DELETE", "/api/queues/%2F/q")
    assert status == 200
    assert payload["messages_deleted"] == 1
    assert "q" not in broker.vhost().queues


def test_exchange_vhost_bindings_nodes(management, broker):
    broker.declare_exchange("ex", "fanout")
    broker.declare_queue("q")
    broker.bind_queue("q", "ex", "")
    status, exchanges = call(management, "GET", "/api/exchanges")
    assert status == 200
    assert any(row["name"] == "ex" for row in exchanges)
    status, bindings = call(management, "GET", "/api/bindings")
    assert status == 200
    assert bindings[0]["source"] == "ex"
    status, vhosts = call(management, "GET", "/api/vhosts")
    assert status == 200 and vhosts[0]["name"] == "/"
    status, nodes = call(management, "GET", "/api/nodes")
    assert status == 200 and nodes[0]["running"] is True
    status, channels = call(management, "GET", "/api/channels")
    assert status == 200


def test_policy_put_get_delete(management, broker):
    body = {
        "pattern": "^q$",
        "definition": {"max-length": 2},
        "apply-to": "queues",
        "priority": 1,
    }
    status, payload = call(management, "PUT", "/api/policies/%2F/limit", body=body)
    assert status == 201
    assert payload["name"] == "limit"
    broker.declare_queue("q")
    assert broker.vhost().get_queue("q").max_length == 2
    status, payload = call(management, "GET", "/api/policies/%2F/limit")
    assert status == 200 and payload["priority"] == 1
    status, _ = call(management, "DELETE", "/api/policies/%2F/limit")
    assert status == 204
    status, _ = call(management, "GET", "/api/policies/%2F/limit")
    assert status == 404


def test_publish_via_management(management, broker):
    broker.declare_exchange("api", "direct")
    broker.declare_queue("q")
    broker.bind_queue("q", "api", "rk")
    status, payload = call(
        management,
        "POST",
        "/api/exchanges/%2F/api/publish",
        body={
            "routing_key": "rk",
            "payload": "hello",
            "payload_encoding": "string",
            "properties": {"delivery_mode": 2},
        },
    )
    assert status == 200
    assert payload["routed"] is True
    delivery = broker.get("q")
    assert delivery.message.body == b"hello"
    assert delivery.message.properties["delivery_mode"] == 2


def test_health_endpoint_reflects_alarm(management, broker):
    status, payload = call(management, "GET", "/api/health/checks/alarms")
    assert status == 200
    assert payload["status"] == "ok"
    broker.set_alarm("memory", True, "high watermark")
    status, payload = call(management, "GET", "/api/health/checks/alarms")
    assert status == 503
    assert payload["status"] == "failed"


def test_metrics_endpoint(management, broker):
    broker.declare_queue("metrics-q")
    broker.publish("", "metrics-q", b"x")
    status, text = call(management, "GET", "/metrics")
    assert status == 200
    assert "mini_rabbitmq_messages_published_total" in text
    assert 'queue="metrics-q"' in text


def test_vhost_access_control(management, broker, server):
    broker.create_vhost("private")
    broker.auth.add_user("alice", "pw", tags=("management",))
    broker.auth.set_permissions("alice", "/", read=".*", write=".*", configure=".*")
    status, vhosts = call(
        management, "GET", "/api/vhosts", username="alice", password="pw"
    )
    assert status == 200
    assert [item["name"] for item in vhosts] == ["/"]
    status, _ = call(
        management, "GET", "/api/queues/%2Fprivate", username="alice", password="pw"
    )
    assert status == 403


def test_unknown_endpoint(management):
    status, payload = call(management, "GET", "/api/nope")
    assert status == 404
    assert payload["error"] == "not_found"
