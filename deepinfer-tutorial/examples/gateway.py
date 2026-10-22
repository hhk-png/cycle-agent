"""Simulate tenant admission, rate limits, prefix affinity and drain."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_deepinfer.gateway import Gateway, TenantQuota, WorkerLoad  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def main() -> None:
    clock = Clock()
    gateway = Gateway(
        [
            TenantQuota("search", "key-search", requests_per_minute=3, tokens_per_minute=2_000, max_prompt_tokens=1_000, max_concurrent=8),
            TenantQuota("batch", "key-batch", requests_per_minute=100, tokens_per_minute=1_000_000, max_prompt_tokens=8_000, max_concurrent=8),
        ],
        clock=clock,
    )

    print("tenant:", gateway.authenticate("key-search").tenant_id)
    for index in range(4):
        decision = gateway.admit("key-search", 600)
        print(f"search request {index + 1}: allowed={decision.allowed} reason={decision.reason}")
        if decision.allowed:
            gateway.release("search")

    clock.now += 60
    decision = gateway.admit("key-search", 600)
    print("after 60s refill:", decision.allowed, decision.reason)
    if decision.allowed:
        gateway.release("search")

    workers = [WorkerLoad("decode-0", inflight=3), WorkerLoad("decode-1", inflight=0)]
    first = gateway.route("prefix-42", workers)
    second = gateway.route("prefix-42", workers)
    print("prefix affinity:", first, second)
    workers[0].inflight = 0
    print("affinity survives lower load:", gateway.route("prefix-42", workers))

    gateway.start_drain()
    print("draining:", gateway.admit("key-batch", 100).reason)
    gateway.stop_drain()
    print("stats:", gateway.stats())


if __name__ == "__main__":
    main()
