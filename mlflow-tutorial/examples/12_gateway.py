"""A runnable AI gateway: routing, fallback, cache, cost accounting.

    python examples/12_gateway.py

No network or API key is required. MockProvider makes behavior deterministic;
CallableProvider in ``mini_mlflow.gateway`` is the seam for a real SDK.
"""

from __future__ import annotations

from _common import print_json

from mini_mlflow.gateway import (
    Gateway,
    GatewayConfig,
    MockProvider,
    ProviderSpec,
)


def main() -> None:
    config = GatewayConfig.from_dict(
        {
            "providers": {
                "fast": {
                    "kind": "mock",
                    "model": "mock-fast",
                    "cost_per_1k_input": 0.15,
                    "cost_per_1k_output": 0.60,
                    "timeout_ms": 5000,
                },
                "strong": {
                    "kind": "mock",
                    "model": "mock-strong",
                    "cost_per_1k_input": 3.00,
                    "cost_per_1k_output": 15.00,
                    "timeout_ms": 30000,
                },
            },
            "routes": {
                "support": {
                    "primary": "fast",
                    "fallbacks": ["strong"],
                    "rate_limit_rpm": 60,
                    "cache_ttl_s": 30,
                }
            },
        }
    )
    # Inject one transient failure to demonstrate failover without sleeping.
    fast = MockProvider(
        ProviderSpec(
            name="fast",
            kind="mock",
            model="mock-fast",
            cost_per_1k_input=0.15,
            cost_per_1k_output=0.60,
        ),
        fail_times=1,
    )
    gateway = Gateway(config, providers={"fast": fast})

    messages = [
        {"role": "system", "content": "You are a concise support agent."},
        {"role": "user", "content": "Where is order A-1042?"},
    ]
    first = gateway.complete("support", messages)
    print("first response:")
    print_json(first.to_dict())

    second = gateway.complete("support", messages)
    print(f"second call cache_hit={second.cache_hit} provider={second.provider}")

    print("stats:")
    print_json(gateway.stats())

    print("public config (safe to expose; no secrets):")
    print_json(gateway.public_config())


if __name__ == "__main__":
    main()
