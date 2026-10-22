from __future__ import annotations

import pytest

from mini_deepinfer.gateway import Gateway, TenantQuota, TokenBucket, WorkerLoad


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gateway(**overrides) -> tuple[Gateway, FakeClock]:
    clock = FakeClock()
    quota = TenantQuota(
        tenant_id="acme",
        api_key="key-acme",
        requests_per_minute=overrides.get("rpm", 60),
        tokens_per_minute=overrides.get("tpm", 10_000),
        max_prompt_tokens=overrides.get("max_prompt", 1_000),
        max_concurrent=overrides.get("max_concurrent", 4),
    )
    return Gateway([quota], clock=clock), clock


def test_token_bucket_refills_and_reports_retry_after() -> None:
    bucket = TokenBucket(capacity=2, refill_per_s=1.0, now=0.0)
    assert bucket.take(1, now=0.0)[0]
    assert bucket.take(1, now=0.0)[0]
    allowed, retry = bucket.take(1, now=0.5)
    assert not allowed
    assert retry == pytest.approx(0.5)
    assert bucket.take(1, now=1.0)[0]


def test_authentication_rejects_unknown_keys() -> None:
    gateway, _ = make_gateway()
    assert gateway.authenticate("key-acme").tenant_id == "acme"
    with pytest.raises(PermissionError):
        gateway.authenticate("wrong")


def test_prompt_size_and_concurrency_limits() -> None:
    gateway, _ = make_gateway(max_prompt=10, max_concurrent=1)
    assert gateway.admit("key-acme", 11).reason == "prompt too long"
    first = gateway.admit("key-acme", 4)
    assert first.allowed
    second = gateway.admit("key-acme", 4)
    assert not second.allowed
    assert second.reason == "concurrency limit"
    gateway.release("acme")
    assert gateway.admit("key-acme", 4).allowed


def test_request_rate_limit_recovers_after_a_minute() -> None:
    gateway, clock = make_gateway(rpm=2, tpm=1_000_000)
    assert gateway.admit("key-acme", 1).allowed
    assert gateway.admit("key-acme", 1).allowed
    blocked = gateway.admit("key-acme", 1)
    assert not blocked.allowed
    assert blocked.reason == "requests per minute"
    assert blocked.retry_after_s > 0
    clock.advance(60)
    assert gateway.admit("key-acme", 1).allowed


def test_token_rate_limit_is_charged_per_prompt() -> None:
    gateway, clock = make_gateway(rpm=100, tpm=10, max_prompt=100)
    assert gateway.admit("key-acme", 8).allowed
    blocked = gateway.admit("key-acme", 8)
    assert not blocked.allowed
    assert blocked.reason == "tokens per minute"
    clock.advance(60)
    assert gateway.admit("key-acme", 8).allowed


def test_prefix_affinity_prefers_the_worker_that_saw_the_prefix() -> None:
    gateway, _ = make_gateway()
    workers = [WorkerLoad("w0", inflight=2), WorkerLoad("w1", inflight=0)]
    assert gateway.route("prefix-a", workers) == "w1"
    # The second request for the same prefix goes back to w1 even though w0
    # now has less pressure, because the KV is already there.
    workers[0].inflight = 0
    assert gateway.route("prefix-a", workers) == "w1"
    assert gateway.prefix_affinity("prefix-a") == "w1"


def test_overloaded_affinity_worker_is_skipped() -> None:
    gateway, _ = make_gateway()
    workers = [WorkerLoad("w0", inflight=0), WorkerLoad("w1", inflight=0)]
    assert gateway.route("prefix-b", workers) == "w0"
    workers[0].inflight = workers[0].capacity
    assert gateway.route("prefix-b", workers) == "w1"


def test_drain_rejects_new_work_and_stats_are_visible() -> None:
    gateway, _ = make_gateway()
    assert gateway.admit("key-acme", 1).allowed
    gateway.start_drain()
    assert not gateway.admit("key-acme", 1).allowed
    assert gateway.admit("key-acme", 1).reason == "draining"
    stats = gateway.stats()
    assert stats["draining"] is True
    assert stats["admitted"] == 1
    assert stats["rejected"] == 2
    gateway.stop_drain()
    assert gateway.admit("key-acme", 1).allowed
