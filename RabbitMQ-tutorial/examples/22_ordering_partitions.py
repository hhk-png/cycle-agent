"""顺序专题的可运行实验：FIFO 的边界、重投、优先级、分区与重排序。

这个示例不依赖真实 RabbitMQ，全部使用 Mini-RabbitMQ 的进程内模型。
它把“队列是 FIFO”拆成五个可观察事实：

1. 单队列 + 单消费者：同优先级、无重投时是 FIFO；
2. 竞争消费者：每个消费者的子序列有序，但全局顺序是交错的；
3. requeue：消息带 redelivered 标记回到队列前部，相对顺序已改变；
4. 优先级：队列会主动重排，FIFO 不再成立；
5. Super Stream：只有分区内有序，跨分区没有全局顺序。

最后用一个应用层 Resequencer 演示：当 broker 只能保证分区内顺序时，
如何用业务序列号把乱序恢复成每实体有序。
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker
from mini_rabbitmq.streams import SuperStream


class Resequencer:
    """按 (key, seq) 缓冲乱序事件，按连续序号向上游交付。

    真实系统通常把 buffer 放在数据库/Redis，并设置水位线、超时和
    缺失告警；这里用内存字典展示最小语义。
    """

    def __init__(self) -> None:
        self.expected: dict[str, int] = {}
        self.buffers: dict[str, dict[int, str]] = {}

    def push(self, key: str, seq: int, payload: str) -> list[tuple[str, int, str]]:
        buffer = self.buffers.setdefault(key, {})
        buffer[seq] = payload
        ready: list[tuple[str, int, str]] = []
        next_seq = self.expected.get(key, 0)
        while next_seq in buffer:
            ready.append((key, next_seq, buffer.pop(next_seq)))
            next_seq += 1
        self.expected[key] = next_seq
        return ready


def drain(broker: Broker, queue: str) -> list[tuple[str, bool]]:
    seen: list[tuple[str, bool]] = []
    while True:
        delivery = broker.get(queue)
        if delivery is None:
            return seen
        seen.append((delivery.message.body.decode(), delivery.message.redelivered))
        broker.ack(delivery.delivery_tag)


def main() -> None:
    broker = Broker()
    try:
        # 1) 单队列 + 单消费者：FIFO 成立
        broker.declare_queue("fifo")
        for i in range(5):
            broker.publish("", "fifo", f"m-{i}")
        fifo = [body for body, _ in drain(broker, "fifo")]
        print("single queue FIFO:", fifo)
        assert fifo == [f"m-{i}" for i in range(5)]

        # 2) 竞争消费者：每个消费者的子序列有序，全局是交错
        broker.declare_queue("work")
        seen: dict[str, list[int]] = {"worker-a": [], "worker-b": []}
        for name in seen:
            def work(message, delivery, worker=name):
                seen[worker].append(int(message.body.decode().split("-")[1]))
                broker.ack(delivery.delivery_tag)

            broker.add_consumer("work", work, prefetch_count=1)
        for i in range(6):
            broker.publish("", "work", f"job-{i}")
        print("competing consumers:", seen)
        assert sorted(seen["worker-a"] + seen["worker-b"]) == list(range(6))
        for sequence in seen.values():
            assert sequence == sorted(sequence)

        # 3) requeue：m-1 被重投，处理序列变成 m-1, m-2, m-1, m-3
        broker.declare_queue("retry")
        for i in range(1, 4):
            broker.publish("", "retry", f"m-{i}")
        first = broker.get("retry")
        second = broker.get("retry")
        assert first is not None and second is not None
        broker.nack(first.delivery_tag, requeue=True)
        broker.ack(second.delivery_tag)
        retried = drain(broker, "retry")
        print("after requeue:", retried)
        assert retried == [("m-1", True), ("m-3", False)]

        # 4) 优先级：队列会重排，FIFO 让位于 priority
        broker.declare_queue("priority", arguments={"x-max-priority": 10})
        for priority, body in ((1, "low"), (9, "urgent"), (5, "normal")):
            broker.publish(
                "", "priority", body, properties={"priority": priority}
            )
        priority_seen = [body for body, _ in drain(broker, "priority")]
        print("priority order:", priority_seen)
        assert priority_seen == ["urgent", "normal", "low"]

        # 5) Super Stream：每实体进入固定分区，分区内有序、全局无序
        stream = SuperStream("orders", partitions=3)
        keys = ["order-1", "order-2", "order-3", "order-4"]
        for seq in range(4):
            for key in keys:
                stream.append(key, f"{key}:{seq}")

        per_key: dict[str, list[int]] = {}
        for partition in stream.partitions:
            for entry in partition.read(0, 100):
                key, seq = entry.body.decode().split(":")
                per_key.setdefault(key, []).append(int(seq))
        print("per-key partition order:", per_key)
        assert per_key == {key: [0, 1, 2, 3] for key in keys}

        # 6) 应用层 Resequencer：把跨分区的乱序恢复成每实体有序
        resequencer = Resequencer()
        delivered: list[tuple[str, int, str]] = []
        out_of_order = [
            ("order-1", 1),
            ("order-2", 0),
            ("order-1", 0),
            ("order-1", 3),
            ("order-1", 2),
        ]
        for key, seq in out_of_order:
            delivered.extend(resequencer.push(key, seq, f"{key}:{seq}"))
        by_key: dict[str, list[str]] = {}
        for key, _, payload in delivered:
            by_key.setdefault(key, []).append(payload)
        print("resequenced:", by_key)
        assert by_key == {
            "order-1": ["order-1:0", "order-1:1", "order-1:2", "order-1:3"],
            "order-2": ["order-2:0"],
        }
    finally:
        broker.close()
    print("OK 22_ordering_partitions")


if __name__ == "__main__":
    main()
