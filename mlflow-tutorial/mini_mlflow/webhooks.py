"""Outbound webhooks for registry and prompt events.

MLflow's OSS webhooks (3.3+) turn registry changes into HTTP callbacks.  The
important engineering problem is not the POST itself; it is delivering an
event **without losing it** when the receiver is down, while making retries
safe and keeping the signing secret out of logs.

This module implements the same shape:

* events are named ``entity.action`` (for example ``model_version.created``);
* every delivery is persisted in an outbox **before** the HTTP attempt;
* the dispatcher retries with exponential backoff and records the last error;
* payloads are signed as ``v1,<base64(HMAC-SHA256)>`` over
  ``delivery_id.timestamp.payload`` and sent with ``X-Mlflow-*`` headers;
* receivers must de-duplicate by ``X-Mlflow-Delivery-ID`` because delivery is
  at-least-once, not exactly-once.

The implementation is intentionally synchronous inside :meth:`drain_once` and
single-process.  Chapter 38 explains the production gaps: transactional
outbox, multiple workers with row claiming, secret encryption, dead-letter
replay tooling, and SSRF defenses beyond URL validation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from .entities import Webhook, WebhookDelivery
from .errors import InvalidParameter, ResourceNotFound
from .store.sqlite_store import SqliteStore
from .utils import compact_json, new_id, now_iso, now_ms, validate_name

LOGGER = logging.getLogger("mini_mlflow.webhooks")

# OSS MLflow event names use snake_case dotted identifiers.  Prompt events are
# part of the same model; MiniMLflow implements a subset and documents it.
EVENT_NAMES = frozenset(
    {
        "registered_model.created",
        "registered_model.renamed",
        "registered_model.deleted",
        "model_version.created",
        "model_version.deleted",
        "model_version.transitioned_stage",
        "model_version_tag.set",
        "model_version_tag.deleted",
        "model_version_alias.created",
        "model_version_alias.deleted",
        "prompt.created",
        "prompt.deleted",
        "prompt_version.created",
        "prompt_alias.created",
        "prompt_alias.deleted",
        "dataset.created",
        "dataset.records.merged",
        "dataset.deleted",
    }
)
WEBHOOK_STATUSES = {"ACTIVE", "DISABLED"}
_MAX_EVENTS = 50
_MAX_URL = 2048
_MAX_SECRET = 256
_MAX_DESCRIPTION = 5000


def _env_number(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise InvalidParameter(f"{name} must be a number") from exc
    if value < minimum:
        raise InvalidParameter(f"{name} must be >= {minimum}")
    return value


def validate_webhook_url(url: str) -> str:
    """Validate an outbound URL.

    This blocks obvious misuse (``file://``, credentials in the URL, fragments)
    but **does not** block private address space, because a local receiver is
    a legitimate development setup.  Production must add an allowlist or a
    DNS-resolution check; URL validation alone cannot stop DNS rebinding.
    """
    if not isinstance(url, str):
        raise InvalidParameter("webhook url must be a string")
    text = url.strip()
    if not text:
        raise InvalidParameter("webhook url must not be empty")
    if len(text) > _MAX_URL:
        raise InvalidParameter(f"webhook url exceeds {_MAX_URL} characters")
    if any(ord(char) < 32 for char in text):
        raise InvalidParameter("webhook url must not contain control characters")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        raise InvalidParameter("webhook url must use http or https")
    if not parsed.netloc or not parsed.hostname:
        raise InvalidParameter("webhook url must include a host")
    if parsed.username or parsed.password:
        raise InvalidParameter("webhook url must not embed credentials")
    if parsed.fragment:
        raise InvalidParameter("webhook url must not contain a fragment")
    try:
        parsed.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError as exc:
        raise InvalidParameter("webhook url has an invalid port") from exc
    return text


def validate_webhook_events(events: Sequence[str]) -> list[str]:
    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise InvalidParameter("webhook events must be a list of event names")
    clean: list[str] = []
    seen: set[str] = set()
    for item in events:
        if not isinstance(item, str):
            raise InvalidParameter("webhook event names must be strings")
        event = item.strip()
        if event not in EVENT_NAMES:
            raise InvalidParameter(
                f"unknown webhook event {event!r}; supported events: "
                + ", ".join(sorted(EVENT_NAMES))
            )
        if event not in seen:
            seen.add(event)
            clean.append(event)
    if not clean:
        raise InvalidParameter("a webhook must subscribe to at least one event")
    if len(clean) > _MAX_EVENTS:
        raise InvalidParameter(f"a webhook supports at most {_MAX_EVENTS} events")
    return clean


def validate_webhook_status(status: str) -> str:
    value = str(status or "").upper()
    if value not in WEBHOOK_STATUSES:
        raise InvalidParameter("webhook status must be ACTIVE or DISABLED")
    return value


def _default_sender(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> int:
    """POST one delivery; return the HTTP status.

    Redirects are rejected.  Following a redirect would let an attacker turn a
    validated URL into a request to an internal address (SSRF via 30x).
    """

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: ANN001
            raise urllib.error.HTTPError(
                req.full_url, code, "webhook redirects are not followed", hdrs, fp
            )

    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            # Read a bounded amount so a malicious receiver cannot exhaust
            # memory; the body is not used for retry decisions.
            response.read(64 * 1024)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        try:
            exc.read(64 * 1024)
        except Exception:  # noqa: BLE001 - best-effort drain
            pass
        return int(exc.code)


class WebhookService:
    def __init__(
        self,
        store: SqliteStore,
        *,
        sender: Callable[[str, bytes, Mapping[str, str], float], int] | None = None,
        timeout_s: float | None = None,
        max_attempts: int | None = None,
        retry_backoff_s: float | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.sender = sender or _default_sender
        self.timeout_s = (
            float(timeout_s)
            if timeout_s is not None
            else _env_number("MINI_MLFLOW_WEBHOOK_TIMEOUT_S", 5.0, minimum=0.1)
        )
        self.max_attempts = (
            int(max_attempts)
            if max_attempts is not None
            else int(
                _env_number("MINI_MLFLOW_WEBHOOK_MAX_ATTEMPTS", 4, minimum=1)
            )
        )
        self.retry_backoff_s = (
            float(retry_backoff_s)
            if retry_backoff_s is not None
            else _env_number("MINI_MLFLOW_WEBHOOK_RETRY_BACKOFF_S", 1.0, minimum=0.0)
        )
        if self.max_attempts < 1:
            raise InvalidParameter("webhook max_attempts must be >= 1")
        if self.retry_backoff_s < 0:
            raise InvalidParameter("webhook retry_backoff_s must be >= 0")
        self._clock = clock or now_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def create_webhook(
        self,
        name: str,
        url: str,
        events: Sequence[str],
        *,
        secret: str = "",
        description: str = "",
    ) -> Webhook:
        clean_name = validate_name(name, what="webhook name", max_len=250)
        clean_url = validate_webhook_url(url)
        clean_events = validate_webhook_events(events)
        clean_secret = self._validate_secret(secret)
        clean_description = self._validate_description(description)
        timestamp = self._clock()
        webhook = Webhook(
            webhook_id=new_id(),
            name=clean_name,
            url=clean_url,
            events=clean_events,
            secret=clean_secret,
            description=clean_description,
            status="ACTIVE",
            creation_timestamp=timestamp,
            last_updated_timestamp=timestamp,
        )
        return self.store.create_webhook(webhook)

    def get_webhook(self, webhook_id: str) -> Webhook:
        return self.store.get_webhook(str(webhook_id))

    def list_webhooks(self) -> list[Webhook]:
        return self.store.list_webhooks()

    def update_webhook(
        self,
        webhook_id: str,
        *,
        name: str | None = None,
        url: str | None = None,
        events: Sequence[str] | None = None,
        secret: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Webhook:
        webhook_id = str(webhook_id)
        self.store.get_webhook(webhook_id)  # fail early with 404
        clean_name = (
            validate_name(name, what="webhook name", max_len=250)
            if name is not None
            else None
        )
        clean_url = validate_webhook_url(url) if url is not None else None
        clean_events = (
            validate_webhook_events(events) if events is not None else None
        )
        clean_secret = self._validate_secret(secret) if secret is not None else None
        clean_description = (
            self._validate_description(description) if description is not None else None
        )
        clean_status = validate_webhook_status(status) if status is not None else None
        return self.store.update_webhook(
            webhook_id,
            name=clean_name,
            url=clean_url,
            events=clean_events,
            secret=clean_secret,
            description=clean_description,
            status=clean_status,
        )

    def delete_webhook(self, webhook_id: str) -> None:
        self.store.delete_webhook(str(webhook_id))

    # ------------------------------------------------------------------
    # Event production
    # ------------------------------------------------------------------
    def emit(self, event: str, data: Mapping[str, Any] | None = None) -> list[WebhookDelivery]:
        """Persist deliveries for every active webhook subscribed to ``event``.

        This is called after a registry/prompt mutation commits.  A production
        outbox would write the mutation and the delivery row in the **same**
        transaction; MiniMLflow's store methods own their transactions, so a
        crash between the two writes can lose the event.  The delivery table
        still gives at-least-once semantics for the events that were enqueued.
        """
        return self._enqueue(event, data, webhook_ids=None, force=False)

    def enqueue(
        self,
        event: str,
        data: Mapping[str, Any] | None = None,
        *,
        webhook_ids: Sequence[str] | None = None,
    ) -> list[WebhookDelivery]:
        return self._enqueue(event, data, webhook_ids=webhook_ids, force=False)

    def test_webhook(self, webhook_id: str) -> WebhookDelivery:
        webhook = self.store.get_webhook(str(webhook_id))
        deliveries = self._enqueue(
            "webhook.test",
            {"webhook_id": webhook.webhook_id, "name": webhook.name},
            webhook_ids=[webhook.webhook_id],
            force=True,
        )
        if not deliveries:
            raise ResourceNotFound(f"webhook {webhook_id!r} not found")
        # Test only this delivery; draining the whole outbox would also send
        # unrelated pending events, which is surprising for an operator
        # clicking "test" during an incident.
        return self._attempt(deliveries[0])

    def list_deliveries(
        self, *, webhook_id: str | None = None, limit: int = 100
    ) -> list[WebhookDelivery]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise InvalidParameter("limit must be a positive integer")
        return self.store.list_webhook_deliveries(
            webhook_id=str(webhook_id) if webhook_id is not None else None,
            limit=min(limit, 1000),
        )

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    def drain_once(self, *, limit: int = 100) -> list[WebhookDelivery]:
        """Attempt every delivery whose backoff has elapsed.

        Calling this repeatedly is safe: a delivery that already reached
        ``DELIVERED`` or ``FAILED`` is not selected again.  A receiver may
        still see duplicates after a timeout (the request may have succeeded
        while the response was lost), which is why the delivery id is signed
        and exposed to the receiver.
        """
        due = self.store.list_due_webhook_deliveries(self._clock(), limit=limit)
        updated: list[WebhookDelivery] = []
        for delivery in due:
            updated.append(self._attempt(delivery))
        return updated

    def _attempt(self, delivery: WebhookDelivery) -> WebhookDelivery:
        try:
            webhook = self.store.get_webhook(delivery.webhook_id)
        except ResourceNotFound:
            return self.store.update_webhook_delivery(
                delivery.delivery_id,
                status="FAILED",
                attempts=delivery.attempts,
                last_error="webhook no longer exists",
                response_status=None,
                next_attempt_time=self._clock(),
                delivered_time=None,
            )
        if webhook.status != "ACTIVE":
            return self.store.update_webhook_delivery(
                delivery.delivery_id,
                status="FAILED",
                attempts=delivery.attempts,
                last_error=f"webhook is {webhook.status}",
                response_status=None,
                next_attempt_time=self._clock(),
                delivered_time=None,
            )

        body = compact_json(delivery.payload).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "mini-mlflow-webhooks/1.0",
            "X-Mlflow-Delivery-ID": delivery.delivery_id,
            "X-Mlflow-Timestamp": timestamp,
            "X-Mlflow-Event": delivery.event,
        }
        if webhook.secret:
            signature = _signature(webhook.secret, delivery.delivery_id, timestamp, body)
            headers["X-Mlflow-Signature"] = f"v1,{signature}"

        attempts = delivery.attempts + 1
        try:
            response_status = int(self.sender(webhook.url, body, headers, self.timeout_s))
            if 200 <= response_status < 300:
                return self.store.update_webhook_delivery(
                    delivery.delivery_id,
                    status="DELIVERED",
                    attempts=attempts,
                    last_error="",
                    response_status=response_status,
                    next_attempt_time=self._clock(),
                    delivered_time=self._clock(),
                )
            error = f"HTTP {response_status}"
        except Exception as exc:  # noqa: BLE001 - network boundary
            response_status = None
            error = f"{type(exc).__name__}: {exc}"

        error = error[:1000]
        failed = attempts >= self.max_attempts
        next_attempt = self._clock()
        if not failed:
            # 1s, 2s, 4s ... after the first, second, third failure.  A jittered
            # exponential backoff is better in production (many receivers can
            # otherwise retry in lockstep); the deterministic version keeps
            # the tutorial and its tests reproducible.
            delay_ms = int(
                self.retry_backoff_s * 1000 * (2 ** (attempts - 1))
            )
            next_attempt += delay_ms
        return self.store.update_webhook_delivery(
            delivery.delivery_id,
            status="FAILED" if failed else "PENDING",
            attempts=attempts,
            last_error=error,
            response_status=response_status,
            next_attempt_time=next_attempt,
            delivered_time=None,
        )

    # ------------------------------------------------------------------
    # Background dispatcher (used by the HTTP server, not by tests)
    # ------------------------------------------------------------------
    def start_dispatcher(self, *, interval_s: float = 1.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if interval_s <= 0:
            raise InvalidParameter("dispatcher interval must be positive")
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.drain_once()
                except Exception:  # noqa: BLE001 - a worker must not die
                    LOGGER.exception("webhook dispatcher iteration failed")

        self._thread = threading.Thread(
            target=loop, name="mini-mlflow-webhooks", daemon=True
        )
        self._thread.start()

    def stop_dispatcher(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def close(self) -> None:
        self.stop_dispatcher()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _enqueue(
        self,
        event: str,
        data: Mapping[str, Any] | None,
        *,
        webhook_ids: Sequence[str] | None,
        force: bool,
    ) -> list[WebhookDelivery]:
        if not isinstance(event, str) or not event:
            raise InvalidParameter("webhook event must be a non-empty string")
        if not force and event not in EVENT_NAMES:
            raise InvalidParameter(f"unknown webhook event {event!r}")
        if data is not None and not isinstance(data, Mapping):
            raise InvalidParameter("webhook data must be a mapping")
        if "." not in event:
            raise InvalidParameter("webhook event must look like entity.action")
        entity, action = event.split(".", 1)
        payload = {
            "entity": entity,
            "action": action,
            "timestamp": now_iso(),
            "data": dict(data or {}),
        }
        compact_json(payload)  # fail before writing if data is not serializable

        if webhook_ids is None:
            subscribers = [
                webhook
                for webhook in self.store.list_webhooks()
                if event in webhook.events and webhook.status == "ACTIVE"
            ]
        else:
            wanted = {str(item) for item in webhook_ids}
            subscribers = [
                webhook
                for webhook in self.store.list_webhooks()
                if webhook.webhook_id in wanted
            ]
        timestamp = self._clock()
        deliveries: list[WebhookDelivery] = []
        for webhook in subscribers:
            deliveries.append(
                self.store.create_webhook_delivery(
                    WebhookDelivery(
                        delivery_id=new_id(),
                        webhook_id=webhook.webhook_id,
                        event=event,
                        payload=payload,
                        status="PENDING",
                        attempts=0,
                        created_time=timestamp,
                        next_attempt_time=timestamp,
                    )
                )
            )
        return deliveries

    @staticmethod
    def _validate_secret(secret: str) -> str:
        if secret is None:
            return ""
        if not isinstance(secret, str):
            raise InvalidParameter("webhook secret must be a string")
        if len(secret) > _MAX_SECRET:
            raise InvalidParameter(f"webhook secret exceeds {_MAX_SECRET} characters")
        return secret

    @staticmethod
    def _validate_description(description: str) -> str:
        if description is None:
            return ""
        if not isinstance(description, str):
            raise InvalidParameter("webhook description must be a string")
        if len(description) > _MAX_DESCRIPTION:
            raise InvalidParameter(
                f"webhook description exceeds {_MAX_DESCRIPTION} characters"
            )
        return description


def _signature(secret: str, delivery_id: str, timestamp: str, body: bytes) -> str:
    signed = f"{delivery_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(
    secret: str,
    delivery_id: str,
    timestamp: str,
    body: bytes,
    signature_header: str,
) -> bool:
    """Receiver-side helper used by tests and the chapter 38 example.

    The header is ``v1,<base64>``.  Use :func:`hmac.compare_digest`; a normal
    string comparison leaks timing information.
    """
    if not signature_header.startswith("v1,"):
        return False
    expected = _signature(secret, delivery_id, timestamp, body)
    return hmac.compare_digest(expected, signature_header[len("v1,") :])
