from __future__ import annotations

import pytest

from mini_rabbitmq.errors import (
    AccessRefused,
    NotFound,
    PreconditionFailed,
    ResourceError,
)
from mini_rabbitmq.message import Message


def test_default_exchange_routes_by_queue_name(broker):
    broker.declare_queue("orders")
    result = broker.publish("", "orders", b"hello")
    assert result.routed == 1
    assert broker.get("orders").message.body == b"hello"


def test_missing_exchange_raises(broker):
    with pytest.raises(NotFound):
        broker.publish("missing", "x", b"hello")


def test_mandatory_returns_unroutable(broker):
    broker.declare_exchange("ex", "direct")
    result = broker.publish("ex", "none", b"hello", mandatory=True)
    assert result.routed == 0
    assert len(result.returned) == 1
    assert result.returned[0].body == b"hello"


def test_non_mandatory_unroutable_is_dropped(broker):
    broker.declare_exchange("ex", "direct")
    result = broker.publish("ex", "none", b"hello")
    assert result.routed == 0
    assert result.returned == []


def test_internal_exchange_refuses_publish(broker):
    broker.declare_exchange("internal", "fanout", internal=True)
    with pytest.raises(AccessRefused):
        broker.publish("internal", "", b"x")


def test_fanout_routes_all_queues(broker):
    broker.declare_exchange("logs", "fanout")
    for name in ("a", "b", "c"):
        broker.declare_queue(name)
        broker.bind_queue(name, "logs", "")
    result = broker.publish("logs", "ignored", b"line")
    assert sorted(result.queues) == ["a", "b", "c"]


def test_topic_routing(broker):
    broker.declare_exchange("logs", "topic")
    for name, pattern in [("errors", "app.error.#"), ("all", "#")]:
        broker.declare_queue(name)
        broker.bind_queue(name, "logs", pattern)
    broker.publish("logs", "app.error.db", b"x")
    assert broker.get("errors").message.body == b"x"
    assert broker.get("all").message.body == b"x"


def test_headers_routing(broker):
    broker.declare_exchange("h", "headers")
    broker.declare_queue("q")
    broker.bind_queue("q", "h", "", arguments={"x-match": "all", "format": "json"})
    broker.publish("h", "", b"x", headers={"format": "json"})
    assert broker.get("q").message.body == b"x"


def test_alternate_exchange(broker):
    broker.declare_exchange("main", "direct", arguments={"alternate-exchange": "ae"})
    broker.declare_exchange("ae", "fanout")
    broker.declare_queue("unrouted")
    broker.bind_queue("unrouted", "ae", "")
    broker.publish("main", "no-key", b"x")
    assert broker.get("unrouted").message.body == b"x"


def test_exchange_to_exchange_binding(broker):
    broker.declare_exchange("source", "direct")
    broker.declare_exchange("target", "fanout")
    broker.declare_queue("q")
    broker.bind_exchange("source", "target", "key")
    broker.bind_queue("q", "target", "")
    broker.publish("source", "key", b"x")
    assert broker.get("q").message.body == b"x"


def test_exchange_to_exchange_cycle_stops(broker):
    broker.declare_exchange("a", "fanout")
    broker.declare_exchange("b", "fanout")
    broker.declare_queue("q")
    broker.bind_exchange("b", "a", "")
    broker.bind_exchange("a", "b", "")
    broker.bind_queue("q", "b", "")
    result = broker.publish("a", "", b"x")
    assert result.queues == ["q"]
    assert broker.get("q").message.body == b"x"


def test_vhost_isolation(broker):
    broker.create_vhost("staging")
    broker.declare_queue("q")
    broker.declare_queue("q", vhost="staging")
    broker.publish("", "q", b"prod")
    broker.publish("", "q", b"stage", vhost="staging")
    assert broker.get("q").message.body == b"prod"
    assert broker.get("q", vhost="staging").message.body == b"stage"


def test_delete_vhost(broker):
    broker.create_vhost("temp")
    broker.declare_queue("q", vhost="temp")
    broker.delete_vhost("temp")
    with pytest.raises(NotFound):
        broker.vhost("temp")


def test_default_vhost_cannot_be_deleted(broker):
    with pytest.raises(PreconditionFailed):
        broker.delete_vhost("/")


def test_server_named_queue(broker):
    queue = broker.declare_queue("")
    assert queue.name.startswith("amq.gen-")
    broker.publish("", queue.name, b"x")
    assert broker.get(queue.name).message.body == b"x"


def test_reserved_queue_name(broker):
    with pytest.raises(AccessRefused):
        broker.declare_queue("amq.secret")


def test_reserved_exchange_name(broker):
    with pytest.raises(AccessRefused):
        broker.declare_exchange("amq.secret", "direct")


def test_default_exchange_cannot_be_declared_or_deleted(broker):
    with pytest.raises(AccessRefused):
        broker.declare_exchange("", "direct")
    with pytest.raises(AccessRefused):
        broker.delete_exchange("")


def test_queue_equivalence_check(broker):
    broker.declare_queue("q", durable=True)
    with pytest.raises(PreconditionFailed):
        broker.declare_queue("q", durable=False)
    same = broker.declare_queue("q", durable=True)
    assert same.name == "q"


def test_exchange_equivalence_check(broker):
    broker.declare_exchange("ex", "direct")
    with pytest.raises(PreconditionFailed):
        broker.declare_exchange("ex", "fanout")
    broker.declare_exchange("ex", "direct")


def test_passive_missing(broker):
    with pytest.raises(NotFound):
        broker.declare_queue("missing", passive=True)
    with pytest.raises(NotFound):
        broker.declare_exchange("missing", "direct", passive=True)


def test_policy_sets_max_length_and_ttl(broker):
    broker.set_policy(
        "small",
        "^policied$",
        {"max-length": 2, "message-ttl": 5000},
    )
    queue = broker.declare_queue("policied")
    assert queue.max_length == 2
    assert queue.message_ttl == 5000
    for i in range(3):
        broker.publish("", "policied", f"m{i}")
    assert queue.messages_ready == 2


def test_policy_applies_to_existing_queue(broker):
    queue = broker.declare_queue("q")
    assert queue.max_length == 0
    broker.set_policy("limit", ".*", {"max-length": 1})
    assert queue.max_length == 1


def test_delete_policy_restores_arguments(broker):
    queue = broker.declare_queue("q", arguments={"x-max-length": 5})
    broker.set_policy("limit", ".*", {"max-length": 1})
    assert queue.max_length == 1
    broker.delete_policy("limit")
    assert queue.max_length == 5


def test_policy_priority(broker):
    queue = broker.declare_queue("q")
    broker.set_policy("low", ".*", {"max-length": 5}, priority=1)
    broker.set_policy("high", ".*", {"max-length": 2}, priority=10)
    assert queue.max_length == 2


def test_alarm_blocks_publish(broker):
    broker.set_alarm("memory", True, "40% high watermark")
    with pytest.raises(ResourceError):
        broker.publish("", "q", b"x")
    broker.set_alarm("memory", False)
    broker.declare_queue("q")
    broker.publish("", "q", b"x")


def test_alarm_listener(broker):
    events = []
    broker.add_alarm_listener(lambda kind, active, reason: events.append((kind, active, reason)))
    broker.set_alarm("disk", True, "low free space")
    broker.set_alarm("disk", False, "ok")
    assert events == [("disk", True, "low free space"), ("disk", False, "ok")]


def test_auto_delete_exchange_after_unbind(broker):
    broker.declare_exchange("temp", "fanout", auto_delete=True)
    broker.declare_queue("q")
    broker.bind_queue("q", "temp", "")
    broker.unbind_queue("q", "temp", "")
    assert "temp" not in broker.vhost().exchanges


def test_delete_queue_removes_bindings_and_tags(broker):
    broker.declare_exchange("ex", "fanout")
    broker.declare_queue("q")
    broker.bind_queue("q", "ex", "")
    broker.publish("ex", "", b"x")
    delivery = broker.get("q")
    assert broker.delete_queue("q") == 1
    assert broker.vhost().exchanges["ex"].bindings == []
    with pytest.raises(PreconditionFailed):
        broker.ack(delivery.delivery_tag)


def test_queue_counts_and_metrics(broker):
    broker.declare_exchange("ex", "fanout")
    broker.declare_queue("q")
    broker.bind_queue("q", "ex", "")
    broker.add_consumer("q", lambda m, d: None)
    broker.publish("ex", "", b"x")
    queue = broker.vhost().get_queue("q")
    assert queue.messages_unacknowledged == 1
    assert broker.metrics.get("messages_published_total") == 1
    assert broker.metrics.get("messages_routed_total") == 1
    overview = broker.overview()
    assert overview["totals"]["queues"] == 1


def test_consume_convenience_and_delete_exchange(broker):
    broker.declare_exchange("ex", "fanout")
    broker.declare_queue("q")
    broker.bind_queue("q", "ex", "")
    assert broker.bind_exchange("ex", "ex") is True
    broker.delete_exchange("ex")
    assert "ex" not in broker.vhost().exchanges


def test_exclusive_queue_deleted_when_owner_closes(broker):
    owner = object()
    broker.declare_queue("exclusive", exclusive=True, owner=owner)
    broker.publish("", "exclusive", b"x")
    broker.close_owner(owner)
    assert "exclusive" not in broker.vhost().queues


def test_channel_owner_close_requeues_unacked(broker):
    owner = object()
    broker.declare_queue("q")
    broker.publish("", "q", b"x")
    delivery = broker.get("q", owner=owner)
    assert delivery is not None
    broker.close_owner(owner)
    assert broker.vhost().get_queue("q").messages_ready == 1
