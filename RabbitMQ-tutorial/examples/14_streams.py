from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq.streams import Stream, SuperStream


def main() -> None:
    stream = Stream("telemetry", max_length_bytes=10_000)
    for i in range(5):
        stream.append(f'{{"seq": {i}}}', headers={"source": "sensor-1"})
    consumer = stream.subscribe("dashboard", start="first")
    batch = consumer.read(3)
    print("first batch:", [entry.offset for entry in batch])
    print("stream bytes:", stream.size_bytes)
    assert [entry.offset for entry in batch] == [0, 1, 2]
    assert stream.stats()["entries"] == 5
    assert consumer.read(10)[0].offset == 3

    super_stream = SuperStream("orders", partitions=3)
    for customer in ("alice", "bob", "carol", "alice"):
        super_stream.append(customer, f"order-for-{customer}")
    populated = sum(1 for p in super_stream.partitions if p.stats()["entries"])
    print("super stream partitions used:", populated)
    assert 1 <= populated <= 3
    print("OK 14_streams")


if __name__ == "__main__":
    main()
