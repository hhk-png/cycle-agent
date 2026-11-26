from __future__ import annotations

import time

import pytest

from mini_rabbitmq.errors import InvalidArgument, PreconditionFailed, PublishRejected, ResourceLocked
from mini_rabbitmq.message import Message
from mini_rabbitmq.queue import Queue


def make_queue(broker, name="q", **kwargs):
    return broker.declare_queue(name, **kwargs)


def test_fifo_order(broker):
    queue = make_queue(broker)
    for value in (b"one", b"two", b"three"):
        queue.enqueue(Message(value))
    assert [queue.get().message.body for _ in range(3)] == [b"one", b"two", b"three"]


def test_priority_order_and_clamp(broker):
    queue = make_queue(broker, arguments={"x-max-priority": 5})
    for priority, body in [(1, b"low"), (9, b"high"), (5, b"mid"), (3, b"x")]:
        queue.enqueue(Message(body, properties={"priority": priority}))
    assert [queue.get().message.body for _ in range(4)] == [b"high", b"mid", b"x", b"low"]


def test_priority_fifo_within_same_priority(broker):
    queue = make_queue(broker, arguments={"x-max-priority": 10})
    for body in (b"a", b"b", b"c"):
        queue.enqueue(Message(body, properties={"priority": 4}))
    assert [queue.get().message.body for _ in range(3)] == [b"a", b"b", b"c"]


def test_invalid_max_priority_rejected(broker):
    with pytest.raises(InvalidArgument):
        make_queue(broker, arguments={"x-max-priority": 999})


def test_overflow_drop_head(broker):
    queue = make_queue(broker, arguments={"x-max-length": 2, "x-overflow": "drop-head"})
    for i in range(4):
        queue.enqueue(Message(f"m{i}".encode()))
    assert queue.messages_ready == 2
    assert [queue.get().message.body for _ in range(2)] == [b"m2", b"m3"]


def test_overflow_reject_publish(broker):
    queue = make_queue(broker, arguments={"x-max-length": 1, "x-overflow": "reject-publish"})
    queue.enqueue(Message(b"first"))
    with pytest.raises(PublishRejected):
        queue.enqueue(Message(b"second"))
    assert queue.messages_ready == 1


def test_overflow_reject_publish_dlx(broker):
    broker.declare_exchange("dlx", "fanout")
    broker.declare_queue("dead")
    broker.bind_queue("dead", "dlx", "")
    queue = make_queue(
        broker,
        arguments={
            "x-max-length": 1,
            "x-overflow": "reject-publish-dlx",
            "x-dead-letter-exchange": "dlx",
        },
    )
    queue.enqueue(Message(b"first"))
    with pytest.raises(PublishRejected):
        queue.enqueue(Message(b"second"))
    dead = broker.get("dead")
    assert dead is not None and dead.message.body == b"second"
    assert dead.message.headers["x-death"][0]["reason"] == "maxlen"


def test_max_length_bytes(broker):
    first = Message(b"x" * 100)
    queue = make_queue(
        broker,
        arguments={
            "x-max-length-bytes": first.size + 10,
            "x-overflow": "reject-publish",
        },
    )
    queue.enqueue(first)
    with pytest.raises(PublishRejected):
        queue.enqueue(Message(b"y" * 100))


def test_message_ttl_expires(broker):
    broker.declare_exchange("dlx", "fanout")
    broker.declare_queue("dead")
    broker.bind_queue("dead", "dlx", "")
    queue = make_queue(broker, arguments={"x-dead-letter-exchange": "dlx"})
    queue.enqueue(Message(b"short", properties={"expiration": "5"}))
    time.sleep(0.02)
    assert queue.get() is None
    dead = broker.get("dead")
    assert dead is not None
    assert dead.message.headers["x-death"][0]["reason"] == "expired"


def test_queue_message_ttl(broker):
    queue = make_queue(broker, arguments={"x-message-ttl": 5})
    queue.enqueue(Message(b"short"))
    time.sleep(0.02)
    assert queue.get() is None


def test_zero_ttl_dead_letters_immediately(broker):
    broker.declare_exchange("dlx", "fanout")
    broker.declare_queue("dead")
    broker.bind_queue("dead", "dlx", "")
    queue = make_queue(broker, arguments={"x-message-ttl": 0, "x-dead-letter-exchange": "dlx"})
    assert queue.enqueue(Message(b"zero")) is False
    assert queue.messages_ready == 0
    assert broker.get("dead").message.body == b"zero"


def test_ack_removes_unacked(broker):
    queue = make_queue(broker)
    queue.enqueue(Message(b"x"))
    delivery = queue.get()
    assert queue.messages_unacknowledged == 1
    queue.ack(delivery.delivery_tag)
    assert queue.messages_unacknowledged == 0
    with pytest.raises(PreconditionFailed):
        queue.ack(delivery.delivery_tag)


def test_ack_multiple(broker):
    queue = make_queue(broker)
    for i in range(3):
        queue.enqueue(Message(str(i).encode()))
    deliveries = [queue.get() for _ in range(3)]
    queue.ack(deliveries[2].delivery_tag, multiple=True)
    assert queue.messages_unacknowledged == 0


def test_nack_requeue_marks_redelivered(broker):
    queue = make_queue(broker)
    queue.enqueue(Message(b"x"))
    delivery = queue.get()
    queue.nack(delivery.delivery_tag, requeue=True)
    again = queue.get()
    assert again.message.body == b"x"
    assert again.message.redelivered is True


def test_nack_without_requeue_dead_letters(broker):
    broker.declare_exchange("dlx", "fanout")
    broker.declare_queue("dead")
    broker.bind_queue("dead", "dlx", "")
    queue = make_queue(broker, arguments={"x-dead-letter-exchange": "dlx"})
    queue.enqueue(Message(b"bad"))
    delivery = queue.get()
    queue.nack(delivery.delivery_tag, requeue=False)
    assert broker.get("dead").message.headers["x-death"][0]["reason"] == "rejected"


def test_delivery_limit_dead_letters(broker):
    broker.declare_exchange("dlx", "fanout")
    broker.declare_queue("dead")
    broker.bind_queue("dead", "dlx", "")
    queue = make_queue(
        broker,
        arguments={"x-delivery-limit": 2, "x-dead-letter-exchange": "dlx"},
    )
    queue.enqueue(Message(b"retry"))
    first = queue.get()
    queue.nack(first.delivery_tag, requeue=True)
    second = queue.get()
    queue.nack(second.delivery_tag, requeue=True)
    dead = broker.get("dead")
    assert dead is not None
    assert dead.message.headers["x-death"][0]["reason"] == "delivery_limit"


def test_prefetch_limits_delivery(broker):
    queue = make_queue(broker)
    received = []
    broker.add_consumer(
        "q", lambda message, d: received.append(message.body), prefetch_count=2
    )
    for i in range(4):
        broker.publish("", "q", f"m{i}")
    assert received == [b"m0", b"m1"]
    delivery = next(iter(queue._unacked.values()))
    broker.ack(delivery.delivery_tag)
    assert received == [b"m0", b"m1", b"m2"]


def test_round_robin_two_consumers(broker):
    make_queue(broker)
    first, second = [], []
    broker.add_consumer("q", lambda m, d: first.append(m.body))
    broker.add_consumer("q", lambda m, d: second.append(m.body))
    for i in range(4):
        broker.publish("", "q", f"m{i}")
    assert first == [b"m0", b"m2"]
    assert second == [b"m1", b"m3"]


def test_consumer_priority(broker):
    make_queue(broker)
    high, low = [], []
    broker.add_consumer("q", lambda m, d: high.append(m.body), priority=10, prefetch_count=1)
    broker.add_consumer("q", lambda m, d: low.append(m.body), priority=1, prefetch_count=1)
    broker.publish("", "q", b"first")
    broker.publish("", "q", b"second")
    assert high == [b"first"]
    assert low == [b"second"]


def test_single_active_consumer(broker):
    make_queue(broker, arguments={"x-single-active-consumer": True})
    first, second = [], []
    c1 = broker.add_consumer("q", lambda m, d: first.append(m.body))
    broker.add_consumer("q", lambda m, d: second.append(m.body))
    broker.publish("", "q", b"a")
    broker.publish("", "q", b"b")
    assert first == [b"a", b"b"]
    assert second == []
    broker.cancel_consumer("q", c1.consumer_tag)
    broker.publish("", "q", b"c")
    assert second == [b"c"]


def test_exclusive_consumer(broker):
    make_queue(broker)
    broker.add_consumer("q", lambda m, d: None, exclusive=True)
    with pytest.raises(ResourceLocked):
        broker.add_consumer("q", lambda m, d: None)


def test_exclusive_queue_owner(broker):
    owner = object()
    other = object()
    broker.declare_queue("exclusive", exclusive=True, owner=owner)
    broker.add_consumer("exclusive", lambda m, d: None, owner=owner)
    with pytest.raises(ResourceLocked):
        broker.add_consumer("exclusive", lambda m, d: None, owner=other)


def test_auto_ack_consumer_loses_unacked_state(broker):
    make_queue(broker)
    received = []
    broker.add_consumer("q", lambda m, d: received.append(m.body), auto_ack=True)
    broker.publish("", "q", b"x")
    assert received == [b"x"]
    assert broker.vhost().get_queue("q").messages_unacknowledged == 0


def test_purge_only_ready_messages(broker):
    queue = make_queue(broker)
    queue.enqueue(Message(b"a"))
    queue.enqueue(Message(b"b"))
    delivery = queue.get()
    assert queue.purge() == 1
    assert queue.messages_unacknowledged == 1
    queue.nack(delivery.delivery_tag, requeue=False)


def test_requeue_owner(broker):
    queue = make_queue(broker)
    owner = object()
    queue.enqueue(Message(b"a"))
    queue.enqueue(Message(b"b"))
    queue.get(owner=owner)
    queue.get(owner=owner)
    assert queue.requeue_owner(owner) == 2
    assert queue.messages_ready == 2


def test_should_auto_delete(broker):
    queue = make_queue(broker, auto_delete=True)
    handle = broker.add_consumer("q", lambda m, d: None)
    assert queue.should_auto_delete() is False
    broker.cancel_consumer("q", handle.consumer_tag)
    assert "q" not in broker.vhost().queues


def test_queue_inactivity(broker):
    queue = make_queue(broker, arguments={"x-expires": 10})
    assert queue.expired_by_inactivity(now=queue.last_used + 1) is True
    broker.add_consumer("q", lambda m, d: None)
    assert queue.expired_by_inactivity(now=queue.last_used + 1) is False


def test_invalid_overflow(broker):
    with pytest.raises(InvalidArgument):
        make_queue(broker, arguments={"x-overflow": "explode"})


def test_quorum_queue_validation(broker):
    with pytest.raises(InvalidArgument):
        make_queue(
            broker,
            "q1",
            arguments={"x-queue-type": "quorum", "x-max-priority": 3},
        )
    queue = make_queue(
        broker, "q2", durable=True, arguments={"x-queue-type": "quorum"}
    )
    assert queue.queue_type == "quorum"


def test_stream_type_rejected_in_core_queue(broker):
    with pytest.raises(InvalidArgument):
        make_queue(broker, arguments={"x-queue-type": "stream"})


def test_queue_snapshot_restore_roundtrip(broker):
    queue = make_queue(broker, durable=True)
    queue.enqueue(Message(b"one", properties={"delivery_mode": 2}))
    queue.enqueue(Message(b"two", properties={"delivery_mode": 2}))
    snapshot = queue.snapshot()
    restored = Queue("q", durable=True, broker=broker)
    restored.restore(snapshot)
    assert restored.messages_ready == 2
    assert restored.get().message.body == b"one"


def test_expired_on_requeue(broker):
    queue = make_queue(broker)
    queue.enqueue(Message(b"x", properties={"expiration": "5"}))
    delivery = queue.get()
    time.sleep(0.02)
    queue.nack(delivery.delivery_tag, requeue=True)
    assert queue.messages_ready == 0
