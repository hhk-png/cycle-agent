from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_queue("jobs")
        workers = {"worker-a": [], "worker-b": []}

        for name in workers:
            def work(message, delivery, worker=name):
                workers[worker].append(message.body.decode())
                broker.ack(delivery.delivery_tag)

            broker.add_consumer("jobs", work, prefetch_count=1)

        for i in range(6):
            broker.publish("", "jobs", f"job-{i}")

        print("worker-a:", workers["worker-a"])
        print("worker-b:", workers["worker-b"])
        assert sorted(workers["worker-a"] + workers["worker-b"]) == [
            f"job-{i}" for i in range(6)
        ]
        assert broker.vhost().get_queue("jobs").messages_unacknowledged == 0
    finally:
        broker.close()
    print("OK 02_work_queue")


if __name__ == "__main__":
    main()
