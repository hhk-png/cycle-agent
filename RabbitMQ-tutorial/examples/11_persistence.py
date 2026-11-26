from __future__ import annotations

import shutil
from pathlib import Path

import _bootstrap  # noqa: F401

from mini_rabbitmq import Broker


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "var" / "examples" / "persistence"


def main() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)
    data_file = DATA_DIR / "broker.json"
    try:
        first = Broker(data_file=data_file, fsync=True)
        first.declare_exchange("orders", "direct", durable=True)
        first.declare_queue("orders", durable=True)
        first.bind_queue("orders", "orders", "created")
        first.publish(
            "orders",
            "created",
            b'{"id": 42}',
            properties={"delivery_mode": 2, "content_type": "application/json"},
        )
        first.close()
        print("wrote:", data_file)

        recovered = Broker(data_file=data_file)
        delivery = recovered.get("orders")
        assert delivery is not None
        print("recovered:", delivery.message.body.decode())
        recovered.ack(delivery.delivery_tag)
        recovered.close()
        assert delivery.message.properties["delivery_mode"] == 2
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        parent = DATA_DIR.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    print("OK 11_persistence")


if __name__ == "__main__":
    main()
