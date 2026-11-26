"""Signed registry webhooks: outbox, delivery, retry, and verification.

    python examples/13_webhooks.py

The example starts a tiny local receiver so it needs no external service.  In
a real deployment the receiver would be your CI system, approval service, or
event bus; see chapter 38 for signing, retries, SSRF, and dead-letter design.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _common import client, print_json, unique_experiment

from mini_mlflow.models import LinearRegression
from mini_mlflow.webhooks import verify_signature

SECRET = "demo-webhook-secret"


class Receiver(BaseHTTPRequestHandler):
    events: list[dict] = []
    secret = SECRET

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        headers = self.headers
        verified = verify_signature(
            self.secret,
            headers.get("X-Mlflow-Delivery-ID", ""),
            headers.get("X-Mlflow-Timestamp", ""),
            body,
            headers.get("X-Mlflow-Signature", ""),
        )
        type(self).events.append(
            {
                "event": headers.get("X-Mlflow-Event"),
                "delivery_id": headers.get("X-Mlflow-Delivery-ID"),
                "verified": verified,
                "payload": json.loads(body.decode("utf-8")),
            }
        )
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def main() -> None:
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{receiver.server_address[1]}/hooks/mlflow"

    active = client()
    try:
        webhook = active.create_webhook(
            "promote-on-alias",
            url,
            ["model_version.created", "model_version_alias.created"],
            secret=SECRET,
            description="Demo: notify CI when a model version or alias changes",
        )
        print("webhook:")
        print_json(webhook.to_dict())

        experiment = unique_experiment(active, "webhook-demo")
        run = active.start_run(experiment_id=experiment.experiment_id, run_name="train")
        model = LinearRegression(alpha=0.01).fit(
            [{"floor": 2.0}, {"floor": 5.0}, {"floor": 8.0}], [2.1, 3.9, 6.2]
        )
        info = active.log_model(
            run.info.run_id,
            model,
            registered_model_name="webhook-house-price",
        )
        active.set_registered_model_alias(
            "webhook-house-price", "champion", info.version
        )
        active.end_run(run.info.run_id, "FINISHED")

        # A local service delivers in-process so the example is deterministic.
        # A remote server runs its own dispatcher; poll the delivery table.
        if not active.is_remote:
            active.service.webhooks.drain_once()
        deadline = time.time() + 5
        while len(Receiver.events) < 2 and time.time() < deadline:
            if active.is_remote:
                deliveries = active.list_webhook_deliveries(
                    webhook_id=webhook.webhook_id
                )
                if all(item.status == "DELIVERED" for item in deliveries) and deliveries:
                    time.sleep(0.05)
                    break
            time.sleep(0.05)

        deliveries = active.list_webhook_deliveries(webhook_id=webhook.webhook_id)
        print("\ndeliveries:")
        print_json([item.to_dict() for item in deliveries])
        print("\nreceived events:")
        print_json(Receiver.events)

        active.delete_webhook(webhook.webhook_id)
        print("\nwebhook deleted")
    finally:
        active.close()
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
