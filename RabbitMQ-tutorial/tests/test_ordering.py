"""顺序语义的可执行证据：FIFO 边界、重投、优先级、分区与单活消费者。

这些测试刻意把“顺序”拆成多个独立事实，避免用一条 happy path
证明“RabbitMQ 保证顺序”。每个测试都对应第 31 章的一张决策表。
"""

from __future__ import annotations

from mini_rabbitmq.streams import Stream, SuperStream


def drain(broker, queue):
    seen = []
    while True:
        delivery = broker.get(queue)
        if delivery is None:
            return seen
        seen.append((delivery.message.body, delivery.message.redelivered))
        broker.ack(delivery.delivery_tag)


def test_single_queue_fifo(broker):
    broker.declare_queue("fifo")
    for i in range(5):
        broker.publish("", "fifo", f"m-{i}")
    assert [body for body, _ in drain(broker, "fifo")] == [
        f"m-{i}".encode() for i in range(5)
    ]


def test_competing_consumers_keep_per_consumer_order(broker):
    broker.declare_queue("work")
    seen = {"a": [], "b": []}
    for name in seen:
        def work(message, delivery, worker=name):
            seen[worker].append(int(message.body.decode().split("-")[1]))
            broker.ack(delivery.delivery_tag)

        broker.add_consumer("work", work, prefetch_count=1)

    for i in range(6):
        broker.publish("", "work", f"job-{i}")

    assert sorted(seen["a"] + seen["b"]) == list(range(6))
    assert seen["a"] == sorted(seen["a"])
    assert seen["b"] == sorted(seen["b"])
    # 全局顺序是交错的：两个 Worker 的合并结果不等于 0..5。
    assert seen["a"] + seen["b"] != list(range(6))


def test_requeue_marks_redelivered_and_reorders(broker):
    broker.declare_queue("retry")
    for i in range(1, 4):
        broker.publish("", "retry", f"m-{i}")

    first = broker.get("retry")
    second = broker.get("retry")
    assert first is not None and second is not None
    broker.nack(first.delivery_tag, requeue=True)
    broker.ack(second.delivery_tag)

    assert drain(broker, "retry") == [(b"m-1", True), (b"m-3", False)]


def test_priority_reorders_ready_messages(broker):
    broker.declare_queue("priority", arguments={"x-max-priority": 10})
    for priority, body in ((1, "low"), (9, "urgent"), (5, "normal")):
        broker.publish("", "priority", body, properties={"priority": priority})
    assert [body for body, _ in drain(broker, "priority")] == [
        b"urgent",
        b"normal",
        b"low",
    ]


def test_super_stream_preserves_per_key_order():
    stream = SuperStream("orders", partitions=3)
    keys = ["order-1", "order-2", "order-3", "order-4"]
    for seq in range(4):
        for key in keys:
            stream.append(key, f"{key}:{seq}")

    per_key = {}
    for partition in stream.partitions:
        for entry in partition.read(0, 100):
            key, seq = entry.body.decode().split(":")
            per_key.setdefault(key, []).append(int(seq))

    assert per_key == {key: [0, 1, 2, 3] for key in keys}


def test_resequencer_handles_gaps_and_out_of_order():
    class Resequencer:
        def __init__(self):
            self.expected = {}
            self.buffers = {}

        def push(self, key, seq):
            buffer = self.buffers.setdefault(key, {})
            buffer[seq] = f"{key}:{seq}"
            ready = []
            next_seq = self.expected.get(key, 0)
            while next_seq in buffer:
                ready.append(buffer.pop(next_seq))
                next_seq += 1
            self.expected[key] = next_seq
            return ready

    resequencer = Resequencer()
    delivered = []
    for key, seq in [
        ("order-1", 1),
        ("order-2", 0),
        ("order-1", 0),
        ("order-1", 3),
        ("order-1", 2),
    ]:
        delivered.extend(resequencer.push(key, seq))

    assert delivered == [
        "order-2:0",
        "order-1:0",
        "order-1:1",
        "order-1:2",
        "order-1:3",
    ]


def test_stream_offsets_are_monotonic():
    stream = Stream("events")
    offsets = [stream.append(f"m-{i}").offset for i in range(5)]
    assert offsets == [0, 1, 2, 3, 4]

    consumer = stream.subscribe("reader", start="first")
    first_batch = consumer.read(2)
    assert [entry.offset for entry in first_batch] == [0, 1]
    second_batch = consumer.read(2)
    assert [entry.offset for entry in second_batch] == [2, 3]


def test_single_active_consumer_serializes_delivery(broker):
    broker.declare_queue(
        "sac", arguments={"x-single-active-consumer": True}
    )
    seen = {"a": [], "b": []}
    handles = {}
    for name in seen:
        def work(message, delivery, worker=name):
            seen[worker].append(message.body.decode())
            broker.ack(delivery.delivery_tag)

        handles[name] = broker.add_consumer("sac", work)

    for i in range(3):
        broker.publish("", "sac", f"m-{i}")

    assert seen["a"] == ["m-0", "m-1", "m-2"]
    assert seen["b"] == []

    broker.cancel_consumer("sac", handles["a"].consumer_tag)
    broker.publish("", "sac", "m-3")
    assert seen["b"] == ["m-3"]
