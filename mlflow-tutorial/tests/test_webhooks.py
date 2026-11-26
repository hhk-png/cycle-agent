from __future__ import annotations

import threading
from typing import Any

import pytest

from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.errors import (
    InvalidParameter,
    ResourceAlreadyExists,
    ResourceNotFound,
)
from mini_mlflow.server import create_server
from mini_mlflow.tracking import TrackingService
from mini_mlflow.webhooks import EVENT_NAMES, verify_signature


class Recorder:
    """Deterministic HTTP sender used instead of a real network call."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers, timeout: float) -> int:
        status = self.statuses.pop(0) if self.statuses else 200
        self.calls.append(
            {
                "url": url,
                "body": body,
                "headers": dict(headers),
                "timeout": timeout,
                "status": status,
            }
        )
        return status


def _webhook(service: TrackingService, recorder: Recorder, **kwargs: Any):
    service.webhooks.sender = recorder
    defaults = {
        "name": "ops-hook",
        "url": "https://example.test/hooks/mlflow",
        "events": ["model_version.created", "model_version_alias.created"],
        "secret": "s3cret",
        "description": "ops",
    }
    defaults.update(kwargs)
    return service.webhooks.create_webhook(
        defaults["name"],
        defaults["url"],
        defaults["events"],
        secret=defaults["secret"],
        description=defaults["description"],
    )


def test_webhook_crud_and_secret_redaction(service: TrackingService) -> None:
    webhook = _webhook(service, Recorder())
    payload = webhook.to_dict()
    assert payload["name"] == "ops-hook"
    assert payload["status"] == "ACTIVE"
    assert payload["secret_set"] is True
    assert "secret" not in payload

    updated = service.webhooks.update_webhook(
        webhook.webhook_id,
        name="ops-hook-v2",
        events=["model_version.transitioned_stage"],
        status="DISABLED",
    )
    assert updated.name == "ops-hook-v2"
    assert updated.events == ["model_version.transitioned_stage"]
    assert updated.status == "DISABLED"

    assert [item.webhook_id for item in service.webhooks.list_webhooks()] == [
        webhook.webhook_id
    ]
    service.webhooks.delete_webhook(webhook.webhook_id)
    with pytest.raises(ResourceNotFound):
        service.webhooks.get_webhook(webhook.webhook_id)


def test_webhook_duplicate_name_is_rejected(service: TrackingService) -> None:
    _webhook(service, Recorder())
    with pytest.raises(ResourceAlreadyExists):
        _webhook(service, Recorder())


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/hook",
        "file:///etc/passwd",
        "https://user:pass@example.test/hook",
        "https://example.test/hook#fragment",
        "https:///no-host",
    ],
)
def test_webhook_url_validation(service: TrackingService, url: str) -> None:
    with pytest.raises(InvalidParameter):
        service.webhooks.create_webhook("bad", url, ["model_version.created"])


def test_webhook_event_and_status_validation(service: TrackingService) -> None:
    with pytest.raises(InvalidParameter):
        service.webhooks.create_webhook("bad", "https://example.test/x", [])
    with pytest.raises(InvalidParameter):
        service.webhooks.create_webhook("bad", "https://example.test/x", ["nope.created"])
    webhook = _webhook(service, Recorder())
    with pytest.raises(InvalidParameter):
        service.webhooks.update_webhook(webhook.webhook_id, status="PAUSED")


def test_event_delivery_is_signed_and_persisted(service: TrackingService) -> None:
    recorder = Recorder()
    webhook = _webhook(service, recorder)
    service.registry.create_registered_model("demo")
    version = service.registry.create_model_version(
        "demo", source="runs:/" + "0" * 32 + "/model"
    )
    service.registry.set_registered_model_alias("demo", "champion", version.version)

    deliveries = service.webhooks.drain_once()
    assert len(deliveries) == 2
    assert all(item.status == "DELIVERED" for item in deliveries)
    assert [call["headers"]["X-Mlflow-Event"] for call in recorder.calls] == [
        "model_version.created",
        "model_version_alias.created",
    ]
    assert recorder.calls[0]["url"] == webhook.url

    for call, delivery in zip(recorder.calls, deliveries):
        headers = call["headers"]
        assert headers["X-Mlflow-Delivery-ID"] == delivery.delivery_id
        assert verify_signature(
            "s3cret",
            headers["X-Mlflow-Delivery-ID"],
            headers["X-Mlflow-Timestamp"],
            call["body"],
            headers["X-Mlflow-Signature"],
        )
        assert not verify_signature(
            "wrong",
            headers["X-Mlflow-Delivery-ID"],
            headers["X-Mlflow-Timestamp"],
            call["body"],
            headers["X-Mlflow-Signature"],
        )

    body = recorder.calls[0]["body"].decode("utf-8")
    assert '"entity":"model_version"' in body
    assert '"action":"created"' in body
    assert '"version":"1"' in body


def test_event_filter_and_disabled_webhook(service: TrackingService) -> None:
    recorder = Recorder()
    webhook = _webhook(
        service, recorder, events=["model_version.created"], secret=""
    )
    service.registry.create_registered_model("demo")
    service.registry.create_model_version("demo", source="runs:/" + "1" * 32 + "/model")
    service.webhooks.drain_once()
    assert len(recorder.calls) == 1

    service.webhooks.update_webhook(webhook.webhook_id, status="DISABLED")
    service.registry.create_model_version("demo", source="runs:/" + "2" * 32 + "/model")
    assert service.webhooks.drain_once() == []
    assert len(recorder.calls) == 1


def test_retry_then_success_uses_backoff(service: TrackingService) -> None:
    recorder = Recorder([500, 502, 200])
    service.webhooks.max_attempts = 3
    service.webhooks.retry_backoff_s = 0.0
    _webhook(service, recorder, events=["model_version.created"])
    service.registry.create_registered_model("demo")
    service.registry.create_model_version("demo", source="runs:/" + "3" * 32 + "/model")

    first = service.webhooks.drain_once()
    assert first[0].status == "PENDING"
    assert first[0].attempts == 1
    assert first[0].last_error == "HTTP 500"

    second = service.webhooks.drain_once()
    assert second[0].status == "PENDING"
    assert second[0].attempts == 2

    third = service.webhooks.drain_once()
    assert third[0].status == "DELIVERED"
    assert third[0].attempts == 3
    assert third[0].response_status == 200
    assert len(recorder.calls) == 3


def test_network_exception_is_recorded(service: TrackingService) -> None:
    class Boom:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, url: str, body: bytes, headers, timeout: float) -> int:
            self.calls += 1
            raise OSError("connection refused")

    sender = Boom()
    service.webhooks.max_attempts = 2
    service.webhooks.retry_backoff_s = 0.0
    _webhook(service, sender, events=["model_version.created"])
    service.registry.create_registered_model("demo")
    service.registry.create_model_version("demo", source="runs:/" + "9" * 32 + "/model")

    first = service.webhooks.drain_once()[0]
    assert first.status == "PENDING"
    assert "OSError" in first.last_error
    second = service.webhooks.drain_once()[0]
    assert second.status == "FAILED"
    assert second.attempts == 2
    assert sender.calls == 2


def test_delivery_fails_after_max_attempts(service: TrackingService) -> None:
    recorder = Recorder([500, 500])
    service.webhooks.max_attempts = 2
    service.webhooks.retry_backoff_s = 0.0
    _webhook(service, recorder, events=["model_version.created"])
    service.registry.create_registered_model("demo")
    service.registry.create_model_version("demo", source="runs:/" + "4" * 32 + "/model")

    service.webhooks.drain_once()
    failed = service.webhooks.drain_once()[0]
    assert failed.status == "FAILED"
    assert failed.attempts == 2
    assert failed.last_error == "HTTP 500"


def test_outbox_survives_service_restart(service: TrackingService) -> None:
    recorder = Recorder()
    _webhook(service, recorder, events=["model_version.created"])
    service.registry.create_registered_model("demo")
    service.registry.create_model_version("demo", source="runs:/" + "5" * 32 + "/model")
    pending = service.webhooks.list_deliveries()
    assert len(pending) == 1 and pending[0].status == "PENDING"

    config = service.config
    service.close()

    restarted = TrackingService(config)
    try:
        restarted.webhooks.sender = recorder
        delivered = restarted.webhooks.drain_once()
        assert delivered[0].status == "DELIVERED"
        assert delivered[0].delivery_id == pending[0].delivery_id
    finally:
        restarted.close()


def test_test_webhook_and_disabled_status(service: TrackingService) -> None:
    recorder = Recorder()
    webhook = _webhook(service, recorder)
    delivery = service.webhooks.test_webhook(webhook.webhook_id)
    assert delivery.status == "DELIVERED"
    assert delivery.event == "webhook.test"
    assert delivery.payload["entity"] == "webhook"
    assert recorder.calls[0]["headers"]["X-Mlflow-Event"] == "webhook.test"

    service.webhooks.update_webhook(webhook.webhook_id, status="DISABLED")
    delivery = service.webhooks.test_webhook(webhook.webhook_id)
    assert delivery.status == "FAILED"
    assert "DISABLED" in delivery.last_error


def test_test_webhook_only_sends_test_delivery(service: TrackingService) -> None:
    recorder = Recorder()
    webhook = _webhook(
        service, recorder, events=["model_version.created"], secret=""
    )
    service.registry.create_registered_model("demo")
    service.registry.create_model_version("demo", source="runs:/" + "8" * 32 + "/model")
    pending = service.webhooks.list_deliveries(webhook_id=webhook.webhook_id)
    assert len(pending) == 1 and pending[0].status == "PENDING"

    test_delivery = service.webhooks.test_webhook(webhook.webhook_id)
    assert test_delivery.status == "DELIVERED"
    assert len(recorder.calls) == 1
    remaining = {
        item.delivery_id: item
        for item in service.webhooks.list_deliveries(webhook_id=webhook.webhook_id)
    }
    assert remaining[pending[0].delivery_id].status == "PENDING"


def test_registry_event_catalog(service: TrackingService) -> None:
    recorder = Recorder()
    _webhook(
        service,
        recorder,
        events=[
            "registered_model.created",
            "registered_model.renamed",
            "model_version.created",
            "model_version.transitioned_stage",
            "model_version_tag.set",
            "model_version_tag.deleted",
            "model_version_alias.created",
            "model_version_alias.deleted",
            "model_version.deleted",
            "registered_model.deleted",
        ],
        secret="",
    )
    service.registry.create_registered_model("demo")
    version = service.registry.create_model_version(
        "demo", source="runs:/" + "6" * 32 + "/model"
    )
    service.registry.set_model_version_tag("demo", version.version, "owner", "qa")
    service.registry.transition_model_version_stage("demo", version.version, "Staging")
    service.registry.set_registered_model_alias("demo", "canary", version.version)
    service.registry.delete_registered_model_alias("demo", "canary")
    service.registry.delete_model_version_tag("demo", version.version, "owner")
    service.registry.delete_model_version("demo", version.version)
    service.registry.rename_registered_model("demo", "demo-v2")
    service.registry.delete_registered_model("demo-v2", force=True)

    delivered = service.webhooks.drain_once()
    assert len(delivered) == 10
    assert {item.status for item in delivered} == {"DELIVERED"}
    assert {item.event for item in delivered} == {
        "registered_model.created",
        "registered_model.renamed",
        "model_version.created",
        "model_version.transitioned_stage",
        "model_version_tag.set",
        "model_version_tag.deleted",
        "model_version_alias.created",
        "model_version_alias.deleted",
        "model_version.deleted",
        "registered_model.deleted",
    }


def test_prompt_events(service: TrackingService) -> None:
    recorder = Recorder()
    _webhook(
        service,
        recorder,
        events=[
            "prompt.created",
            "prompt_version.created",
            "prompt_alias.created",
            "prompt_alias.deleted",
            "prompt.deleted",
        ],
        secret="",
    )
    prompt = service.prompts.register_prompt("support", "Hi {{name}}")
    service.prompts.set_alias("support", "production", prompt.version)
    service.prompts.delete_alias("support", "production")
    service.prompts.delete_prompt("support")

    delivered = service.webhooks.drain_once()
    assert len(delivered) == 5
    assert {item.event for item in delivered} == {
        "prompt.created",
        "prompt_version.created",
        "prompt_alias.created",
        "prompt_alias.deleted",
        "prompt.deleted",
    }


def test_drain_is_idempotent_after_delivery(service: TrackingService) -> None:
    recorder = Recorder()
    _webhook(service, recorder, events=["model_version.created"])
    service.registry.create_registered_model("demo")
    service.registry.create_model_version("demo", source="runs:/" + "7" * 32 + "/model")
    assert len(service.webhooks.drain_once()) == 1
    assert service.webhooks.drain_once() == []
    assert len(recorder.calls) == 1


def test_webhook_api_contract_remote(config: MiniConfig) -> None:
    server = create_server(config, host="127.0.0.1", port=0)
    recorder = Recorder()
    server.service.webhooks.sender = recorder
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = MiniClient(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        webhook = client.create_webhook(
            "remote-hook",
            "https://example.test/hooks",
            ["model_version.created"],
            secret="remote-secret",
        )
        assert client.get_webhook(webhook.webhook_id).name == "remote-hook"
        assert [item.webhook_id for item in client.list_webhooks()] == [
            webhook.webhook_id
        ]

        delivery = client.test_webhook(webhook.webhook_id)
        assert delivery.status == "DELIVERED"
        deliveries = client.list_webhook_deliveries(webhook_id=webhook.webhook_id)
        assert len(deliveries) == 1
        assert deliveries[0].delivery_id == delivery.delivery_id

        client.update_webhook(webhook.webhook_id, status="DISABLED")
        assert client.get_webhook(webhook.webhook_id).status == "DISABLED"
        client.delete_webhook(webhook.webhook_id)
        with pytest.raises(ResourceNotFound):
            client.get_webhook(webhook.webhook_id)
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_event_names_are_dotted() -> None:
    assert EVENT_NAMES
    assert all("." in name for name in EVENT_NAMES)
