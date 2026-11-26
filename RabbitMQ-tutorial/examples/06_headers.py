from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


def main() -> None:
    broker = Broker()
    try:
        broker.declare_exchange("documents", "headers")
        broker.declare_queue("sales-pdf")
        broker.declare_queue("any-sales")
        broker.bind_queue(
            "sales-pdf",
            "documents",
            "",
            arguments={"x-match": "all", "department": "sales", "format": "pdf"},
        )
        broker.bind_queue(
            "any-sales",
            "documents",
            "",
            arguments={"x-match": "any", "department": "sales", "format": "pdf"},
        )
        broker.publish(
            "documents",
            "",
            b"Q3 report",
            headers={"department": "sales", "format": "pdf"},
        )
        broker.publish(
            "documents",
            "",
            b"Engineering notes",
            headers={"department": "engineering", "format": "pdf"},
        )
        print("sales-pdf:", broker.vhost().get_queue("sales-pdf").messages_ready)
        print("any-sales:", broker.vhost().get_queue("any-sales").messages_ready)
        assert broker.vhost().get_queue("sales-pdf").messages_ready == 1
        assert broker.vhost().get_queue("any-sales").messages_ready == 2
    finally:
        broker.close()
    print("OK 06_headers")


if __name__ == "__main__":
    main()
