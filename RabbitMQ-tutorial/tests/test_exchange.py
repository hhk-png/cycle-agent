from __future__ import annotations

import pytest

from mini_rabbitmq.errors import InvalidArgument
from mini_rabbitmq.exchange import (
    ConsistentHashExchange,
    DirectExchange,
    ExchangeTypeRegistry,
    FanoutExchange,
    HeadersExchange,
    TopicExchange,
    default_registry,
    topic_matches,
)


@pytest.mark.parametrize(
    "pattern,key,expected",
    [
        ("*", "a", True),
        ("*", "a.b", False),
        ("a.*", "a.b", True),
        ("a.*", "a.b.c", False),
        ("#", "a.b.c", True),
        ("#", "", True),
        ("a.#", "a", True),
        ("a.#", "a.b.c", True),
        ("a.#.z", "a.z", True),
        ("a.#.z", "a.b.c.z", True),
        ("a.#.z", "a.b.c", False),
        ("a.*.c", "a.b.c", True),
        ("a.*.c", "a.b", False),
        ("a.b", "a.b", True),
        ("a.b", "a.c", False),
    ],
)
def test_topic_matches(pattern, key, expected):
    assert topic_matches(pattern, key) is expected


def test_direct_exchange_exact_key():
    exchange = DirectExchange("d")
    exchange.add_binding("q1", "queue", "red")
    exchange.add_binding("q2", "queue", "blue")
    assert [b.destination for b in exchange.route("red")] == ["q1"]
    assert exchange.route("green") == []


def test_fanout_ignores_key():
    exchange = FanoutExchange("f")
    exchange.add_binding("q1", "queue", "")
    exchange.add_binding("q2", "queue", "ignored")
    assert {b.destination for b in exchange.route("anything")} == {"q1", "q2"}


def test_topic_multiple_bindings():
    exchange = TopicExchange("t")
    exchange.add_binding("q1", "queue", "stock.*")
    exchange.add_binding("q2", "queue", "stock.#")
    assert {b.destination for b in exchange.route("stock.usd")} == {"q1", "q2"}
    assert {b.destination for b in exchange.route("stock.usd.nyse")} == {"q2"}


def test_headers_all_and_any():
    all_exchange = HeadersExchange("h")
    all_exchange.add_binding(
        "q1", "queue", "", {"x-match": "all", "format": "pdf", "type": "report"}
    )
    assert len(all_exchange.route("", {"format": "pdf", "type": "report"})) == 1
    assert all_exchange.route("", {"format": "pdf"}) == []
    any_exchange = HeadersExchange("h2")
    any_exchange.add_binding("q", "queue", "", {"x-match": "any", "a": 1, "b": 2})
    assert len(any_exchange.route("", {"b": 2})) == 1
    assert any_exchange.route("", {"c": 3}) == []


def test_headers_exchange_ignores_x_keys_but_requires_criteria():
    exchange = HeadersExchange("h")
    exchange.add_binding("q", "queue", "", {"x-match": "all"})
    assert exchange.route("", {"x-match": "all"}) == []


def test_consistent_hash_is_deterministic_and_selects_one():
    exchange = ConsistentHashExchange("hash")
    exchange.add_binding("q1", "queue", "1")
    exchange.add_binding("q2", "queue", "1")
    exchange.add_binding("q3", "queue", "1")
    first = exchange.route("order-42")[0].destination
    second = exchange.route("order-42")[0].destination
    assert first == second
    assert first in {"q1", "q2", "q3"}
    assert all(len(exchange.route(f"key-{i}")) == 1 for i in range(30))


def test_consistent_hash_weight_bias():
    exchange = ConsistentHashExchange("hash")
    exchange.add_binding("heavy", "queue", "9")
    exchange.add_binding("light", "queue", "1")
    counts = {"heavy": 0, "light": 0}
    for i in range(300):
        counts[exchange.route(f"k{i}")[0].destination] += 1
    assert counts["heavy"] > counts["light"]


def test_consistent_hash_hash_header():
    exchange = ConsistentHashExchange("hash", arguments={"hash-header": "tenant"})
    exchange.add_binding("q1", "queue", "1")
    exchange.add_binding("q2", "queue", "1")
    a = exchange.route("ignored", {"tenant": "acme"})[0].destination
    b = exchange.route("other", {"tenant": "acme"})[0].destination
    assert a == b


def test_exchange_type_registry_and_unknown():
    registry = default_registry()
    assert {"direct", "fanout", "topic", "headers", "x-consistent-hash"} <= set(registry.names())
    with pytest.raises(InvalidArgument):
        registry.create("does-not-exist", "x")


def test_registry_custom_type():
    class Custom(DirectExchange):
        type_name = "custom"

    registry = ExchangeTypeRegistry()
    registry.register(Custom)
    exchange = registry.create("custom", "ex")
    assert isinstance(exchange, Custom)


def test_exchange_binding_deduplicates():
    exchange = DirectExchange("d")
    assert exchange.add_binding("q", "queue", "k") is True
    assert exchange.add_binding("q", "queue", "k") is False
    assert len(exchange.bindings) == 1


def test_exchange_remove_destination():
    exchange = DirectExchange("d")
    exchange.add_binding("q", "queue", "k")
    exchange.add_binding("q", "exchange", "k")
    assert exchange.remove_destination("q", "queue") == 1
    assert len(exchange.bindings) == 1


def test_exchange_to_dict():
    exchange = FanoutExchange("events", durable=True, arguments={"alternate-exchange": "ae"})
    data = exchange.to_dict()
    assert data["name"] == "events"
    assert data["type"] == "fanout"
    assert data["durable"] is True
    assert data["arguments"]["alternate-exchange"] == "ae"
