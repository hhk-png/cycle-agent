from __future__ import annotations

from mini_rabbitmq import Broker
from mini_rabbitmq.links import FederationLink, Shovel


def test_shovel_forwards_and_acks_source():
    source = Broker()
    destination = Broker()
    try:
        source.declare_queue("source-q")
        destination.declare_queue("dest-q")
        source.publish("", "source-q", b"hello", headers={"trace": "abc"})
        shovel = Shovel(
            "s1",
            source,
            "source-q",
            destination,
            destination_routing_key="dest-q",
        )
        assert shovel.run_once() == 1
        assert source.get("source-q") is None
        received = destination.get("dest-q")
        assert received.message.body == b"hello"
        assert received.message.headers["trace"] == "abc"
        assert received.message.headers["x-shovel-origin"] == ["s1"]
        assert shovel.stats.forwarded == 1
    finally:
        source.close()
        destination.close()


def test_shovel_preserves_message_properties():
    source = Broker()
    destination = Broker()
    try:
        source.declare_queue("source-q")
        destination.declare_queue("dest-q")
        source.publish(
            "",
            "source-q",
            b"payload",
            properties={
                "delivery_mode": 2,
                "correlation_id": "corr",
                "headers": {"k": "v"},
            },
        )
        shovel = Shovel(
            "s1", source, "source-q", destination, destination_routing_key="dest-q"
        )
        shovel.run_once()
        received = destination.get("dest-q")
        assert received.message.properties["delivery_mode"] == 2
        assert received.message.properties["correlation_id"] == "corr"
    finally:
        source.close()
        destination.close()


def test_shovel_requeues_when_destination_has_no_route():
    source = Broker()
    destination = Broker()
    try:
        source.declare_queue("source-q")
        source.publish("", "source-q", b"hello")
        shovel = Shovel(
            "s1", source, "source-q", destination, destination_routing_key="nowhere"
        )
        assert shovel.run_once() == 0
        assert source.get("source-q").message.body == b"hello"
        assert shovel.stats.failed == 1
    finally:
        source.close()
        destination.close()


def test_shovel_loop_guard_drops_and_acks():
    source = Broker()
    destination = Broker()
    try:
        source.declare_queue("source-q")
        source.publish("", "source-q", b"loop", headers={"x-shovel-origin": ["s1"]})
        shovel = Shovel(
            "s1", source, "source-q", destination, destination_routing_key="dest-q"
        )
        assert shovel.run_once() == 0
        assert source.get("source-q") is None
        assert shovel.stats.loop_dropped == 1
    finally:
        source.close()
        destination.close()


def test_shovel_respects_max_messages():
    source = Broker()
    destination = Broker()
    try:
        source.declare_queue("source-q")
        destination.declare_queue("dest-q")
        for i in range(5):
            source.publish("", "source-q", f"m{i}")
        shovel = Shovel(
            "s1", source, "source-q", destination, destination_routing_key="dest-q"
        )
        assert shovel.run_once(max_messages=2) == 2
        assert destination.vhost().get_queue("dest-q").messages_ready == 2
        assert source.vhost().get_queue("source-q").messages_ready == 3
    finally:
        source.close()
        destination.close()


def test_federation_forwards_matching_messages():
    source = Broker()
    destination = Broker()
    try:
        source.declare_exchange("upstream", "topic")
        source.declare_queue("source-q")
        source.bind_queue("source-q", "upstream", "orders.#")
        destination.declare_exchange("downstream", "topic")
        destination.declare_queue("dest-q")
        destination.bind_queue("dest-q", "downstream", "orders.#")
        link = FederationLink(
            "up",
            source,
            "upstream",
            destination,
            "downstream",
            routing_key_pattern=r"orders\..*",
        )
        link.start()
        source.publish("upstream", "orders.created", b"x")
        received = destination.get("dest-q")
        assert received is not None
        assert received.message.body == b"x"
        assert received.message.headers["x-federation-origin"] == ["up"]
        assert link.forwarded == 1
    finally:
        link.stop()
        source.close()
        destination.close()


def test_federation_ignores_non_matching_routing_key():
    source = Broker()
    destination = Broker()
    link = None
    try:
        source.declare_exchange("upstream", "topic")
        destination.declare_exchange("downstream", "topic")
        destination.declare_queue("dest-q")
        destination.bind_queue("dest-q", "downstream", "#")
        link = FederationLink(
            "up",
            source,
            "upstream",
            destination,
            "downstream",
            routing_key_pattern=r"orders\..*",
        )
        link.start()
        source.publish("upstream", "users.created", b"x")
        assert destination.get("dest-q") is None
        assert link.forwarded == 0
    finally:
        if link is not None:
            link.stop()
        source.close()
        destination.close()


def test_federation_skips_own_origin():
    source = Broker()
    destination = Broker()
    try:
        source.declare_exchange("upstream", "fanout")
        destination.declare_exchange("downstream", "fanout")
        destination.declare_queue("dest-q")
        destination.bind_queue("dest-q", "downstream", "")
        link = FederationLink(
            "up", source, "upstream", destination, "downstream"
        )
        link.start()
        source.publish(
            "upstream",
            "",
            b"x",
            headers={"x-federation-origin": ["up"]},
        )
        assert destination.get("dest-q") is None
        assert link.skipped == 1
    finally:
        link.stop()
        source.close()
        destination.close()


def test_federation_stop_removes_listener():
    source = Broker()
    destination = Broker()
    try:
        source.declare_exchange("upstream", "fanout")
        destination.declare_exchange("downstream", "fanout")
        destination.declare_queue("dest-q")
        destination.bind_queue("dest-q", "downstream", "")
        link = FederationLink(
            "up", source, "upstream", destination, "downstream"
        )
        link.start()
        link.stop()
        source.publish("upstream", "", b"x")
        assert destination.get("dest-q") is None
    finally:
        source.close()
        destination.close()
