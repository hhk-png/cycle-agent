"""Admission control and routing for an inference service.

The engine in this repository deliberately knows nothing about tenants, API
keys or quotas.  Those concerns belong to the layer in front of it.  This
module is a small, deterministic model of that layer:

* authenticate an API key and resolve its tenant quota;
* enforce prompt size, concurrency, requests/minute and tokens/minute;
* route a request to a worker, preferring a worker that already holds the
  request's prefix (prefix affinity) when that worker is not overloaded;
* expose a drain state so a terminating instance stops admitting work.

It is not a production gateway: it has no TLS, no persistent quota store and
no distributed rate limiting.  The value is that the admission decisions are
ordinary, testable functions rather than configuration hidden in a proxy.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TenantQuota:
    tenant_id: str
    api_key: str
    requests_per_minute: int = 60
    tokens_per_minute: int = 100_000
    max_prompt_tokens: int = 8_192
    max_concurrent: int = 8

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("api_key must not be empty")
        for name in ("requests_per_minute", "tokens_per_minute", "max_prompt_tokens", "max_concurrent"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


@dataclass
class Admission:
    allowed: bool
    tenant_id: str | None
    reason: str | None = None
    retry_after_s: float = 0.0
    remaining_requests: float = 0.0
    remaining_tokens: float = 0.0


class TokenBucket:
    """A classic token bucket with an injectable clock."""

    def __init__(self, capacity: float, refill_per_s: float, *, now: float | None = None) -> None:
        if capacity <= 0 or refill_per_s <= 0:
            raise ValueError("capacity and refill rate must be positive")
        self.capacity = float(capacity)
        self.refill_per_s = float(refill_per_s)
        self._tokens = float(capacity)
        self._updated = time.monotonic() if now is None else float(now)

    def _refill(self, now: float) -> None:
        if now > self._updated:
            self._tokens = min(
                self.capacity, self._tokens + (now - self._updated) * self.refill_per_s
            )
            self._updated = now

    def available(self, now: float | None = None) -> float:
        self._refill(time.monotonic() if now is None else float(now))
        return self._tokens

    def take(self, amount: float, now: float | None = None) -> tuple[bool, float]:
        """Try to consume ``amount``; return ``(allowed, retry_after_seconds)``."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        now = time.monotonic() if now is None else float(now)
        self._refill(now)
        if self._tokens + 1e-12 >= amount:
            self._tokens -= amount
            return True, 0.0
        missing = amount - self._tokens
        return False, missing / self.refill_per_s

    def refund(self, amount: float) -> None:
        self._tokens = min(self.capacity, self._tokens + max(0.0, amount))


@dataclass
class WorkerLoad:
    name: str
    inflight: int = 0
    kv_tokens: int = 0
    capacity: int = 64

    @property
    def pressure(self) -> float:
        return self.inflight / max(1, self.capacity)

    def can_accept(self) -> bool:
        return self.inflight < self.capacity


@dataclass
class _TenantState:
    quota: TenantQuota
    request_bucket: TokenBucket
    token_bucket: TokenBucket
    active: int = 0


class Gateway:
    """Tenant-aware admission and prefix-affinity routing."""

    def __init__(
        self,
        quotas: list[TenantQuota],
        *,
        clock=time.monotonic,
        prefix_cache_limit: int = 4096,
    ) -> None:
        if not quotas:
            raise ValueError("at least one tenant quota is required")
        self._clock = clock
        self._lock = threading.Lock()
        self._tenants: dict[str, _TenantState] = {}
        self._by_key: dict[str, _TenantState] = {}
        for quota in quotas:
            if quota.tenant_id in self._tenants:
                raise ValueError(f"duplicate tenant {quota.tenant_id}")
            if quota.api_key in self._by_key:
                raise ValueError("duplicate api key")
            now = self._clock()
            state = _TenantState(
                quota=quota,
                request_bucket=TokenBucket(
                    quota.requests_per_minute, quota.requests_per_minute / 60.0, now=now
                ),
                token_bucket=TokenBucket(
                    quota.tokens_per_minute, quota.tokens_per_minute / 60.0, now=now
                ),
            )
            self._tenants[quota.tenant_id] = state
            self._by_key[quota.api_key] = state
        self._draining = False
        self._prefix_owner: dict[str, str] = {}
        self._prefix_cache_limit = prefix_cache_limit
        self.rejected = 0
        self.admitted = 0

    # ------------------------------------------------------------------
    # Identity and admission
    # ------------------------------------------------------------------
    def authenticate(self, api_key: str) -> TenantQuota:
        state = self._by_key.get(api_key)
        if state is None:
            raise PermissionError("unknown api key")
        return state.quota

    def admit(self, api_key: str, prompt_tokens: int, *, now: float | None = None) -> Admission:
        now = self._clock() if now is None else float(now)
        with self._lock:
            state = self._by_key.get(api_key)
            if state is None:
                self.rejected += 1
                return Admission(False, None, "unknown api key")
            quota = state.quota
            if self._draining:
                self.rejected += 1
                return Admission(False, quota.tenant_id, "draining")
            if prompt_tokens < 1:
                self.rejected += 1
                return Admission(False, quota.tenant_id, "empty prompt")
            if prompt_tokens > quota.max_prompt_tokens:
                self.rejected += 1
                return Admission(False, quota.tenant_id, "prompt too long")
            if state.active >= quota.max_concurrent:
                self.rejected += 1
                return Admission(False, quota.tenant_id, "concurrency limit")
            ok_req, retry_req = state.request_bucket.take(1.0, now)
            if not ok_req:
                self.rejected += 1
                return Admission(
                    False,
                    quota.tenant_id,
                    "requests per minute",
                    retry_after_s=retry_req,
                    remaining_requests=state.request_bucket.available(now),
                    remaining_tokens=state.token_bucket.available(now),
                )
            # Token quota is charged on prompt tokens here.  Production systems
            # usually charge input and output separately, because decode is far
            # more expensive per token.
            ok_tok, retry_tok = state.token_bucket.take(float(prompt_tokens), now)
            if not ok_tok:
                state.request_bucket.refund(1.0)
                self.rejected += 1
                return Admission(
                    False,
                    quota.tenant_id,
                    "tokens per minute",
                    retry_after_s=retry_tok,
                    remaining_requests=state.request_bucket.available(now),
                    remaining_tokens=state.token_bucket.available(now),
                )
            state.active += 1
            self.admitted += 1
            return Admission(
                True,
                quota.tenant_id,
                remaining_requests=state.request_bucket.available(now),
                remaining_tokens=state.token_bucket.available(now),
            )

    def release(self, tenant_id: str) -> None:
        with self._lock:
            state = self._tenants.get(tenant_id)
            if state is None:
                raise KeyError(tenant_id)
            state.active = max(0, state.active - 1)

    def active(self, tenant_id: str) -> int:
        state = self._tenants.get(tenant_id)
        return 0 if state is None else state.active

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def route(self, prefix_hash: str, workers: list[WorkerLoad]) -> str:
        """Choose a worker, preferring prefix affinity for cache locality."""
        if not workers:
            raise ValueError("no workers available")
        with self._lock:
            owner = self._prefix_owner.get(prefix_hash)
            by_name = {worker.name: worker for worker in workers}
            if owner is not None and owner in by_name and by_name[owner].can_accept():
                return owner
            # Least pressure, then least KV, then stable name order.  This is a
            # deliberately simple alternative to a global load balancer; the
            # point is that cache affinity is a first-class input.
            worker = min(workers, key=lambda w: (w.pressure, w.kv_tokens, w.name))
            if len(self._prefix_owner) >= self._prefix_cache_limit:
                # Drop an arbitrary old entry; a production router would use
                # an LRU with a TTL tied to KV retention.
                self._prefix_owner.pop(next(iter(self._prefix_owner)))
            self._prefix_owner[prefix_hash] = worker.name
            return worker.name

    def prefix_affinity(self, prefix_hash: str) -> str | None:
        return self._prefix_owner.get(prefix_hash)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start_drain(self) -> None:
        with self._lock:
            self._draining = True

    def stop_drain(self) -> None:
        with self._lock:
            self._draining = False

    @property
    def draining(self) -> bool:
        return self._draining

    def stats(self) -> dict:
        with self._lock:
            return {
                "tenants": len(self._tenants),
                "admitted": self.admitted,
                "rejected": self.rejected,
                "draining": self._draining,
                "prefix_entries": len(self._prefix_owner),
                "active": {tenant: state.active for tenant, state in self._tenants.items()},
            }
