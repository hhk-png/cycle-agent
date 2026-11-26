"""A small, testable AI gateway.

The gateway sits between application code and model providers.  It owns the
policy that should not be duplicated in every service:

* **routing**: one logical route maps to a primary provider and fallbacks;
* **failure handling**: provider errors fail over instead of reaching users;
* **rate limiting**: a per-route token bucket protects quotas and downstreams;
* **cost accounting**: every request records tokens and USD cost;
* **caching**: identical deterministic requests can be served from memory;
* **secret hygiene**: configuration references environment variable names,
  never raw keys, and responses never echo credentials.

This is deliberately provider-agnostic.  ``MockProvider`` makes the whole
gateway runnable offline; ``CallableProvider`` is the seam for a real SDK.
Production gateways add persistent quotas, distributed caches, guardrails,
streaming, and per-tenant budgets -- see chapters 28 and 33.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import yaml

from .errors import InvalidParameter, MiniMlflowError, ResourceNotFound
from .utils import compact_json, new_id, redact


def _config_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(f"{name} must be an integer") from exc


def _config_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(f"{name} must be a number") from exc


class ProviderError(MiniMlflowError):
    code = "PROVIDER_ERROR"
    http_status = 502


class RouteNotFound(ResourceNotFound):
    code = "RESOURCE_DOES_NOT_EXIST"


class RateLimitExceeded(MiniMlflowError):
    code = "RATE_LIMITED"
    http_status = 429

    def __init__(self, message: str, *, retry_after_s: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_s = max(0.0, float(retry_after_s))


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: str = "mock"
    model: str = "mock-1"
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    timeout_ms: int = 30_000
    max_output_tokens: int = 1024
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidParameter("provider name must not be empty")
        if self.kind not in {"mock", "callable"}:
            raise InvalidParameter("provider kind must be 'mock' or 'callable'")
        for field_name in ("cost_per_1k_input", "cost_per_1k_output"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise InvalidParameter(f"{field_name} must be a finite number")
            if value < 0:
                raise InvalidParameter(f"{field_name} must be >= 0")
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise InvalidParameter("timeout_ms must be a positive integer")
        if isinstance(self.max_output_tokens, bool) or not isinstance(self.max_output_tokens, int):
            raise InvalidParameter("max_output_tokens must be an integer")
        if self.max_output_tokens <= 0:
            raise InvalidParameter("max_output_tokens must be > 0")
        if not isinstance(self.metadata, Mapping):
            raise InvalidParameter("provider metadata must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        # metadata may contain api_key_env, never the key itself.
        return {
            "name": self.name,
            "kind": self.kind,
            "model": self.model,
            "cost_per_1k_input": self.cost_per_1k_input,
            "cost_per_1k_output": self.cost_per_1k_output,
            "timeout_ms": self.timeout_ms,
            "max_output_tokens": self.max_output_tokens,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RouteSpec:
    name: str
    primary: str
    fallbacks: tuple[str, ...] = ()
    rate_limit_rpm: int | None = None
    cache_ttl_s: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidParameter("route name must not be empty")
        if not self.primary:
            raise InvalidParameter("route primary provider must not be empty")
        if self.rate_limit_rpm is not None:
            if isinstance(self.rate_limit_rpm, bool) or not isinstance(self.rate_limit_rpm, int):
                raise InvalidParameter("rate_limit_rpm must be an integer or null")
            if self.rate_limit_rpm <= 0:
                raise InvalidParameter("rate_limit_rpm must be > 0")
        if isinstance(self.cache_ttl_s, bool) or not isinstance(self.cache_ttl_s, int):
            raise InvalidParameter("cache_ttl_s must be an integer")
        if self.cache_ttl_s < 0:
            raise InvalidParameter("cache_ttl_s must be >= 0")

    @property
    def chain(self) -> tuple[str, ...]:
        return (self.primary, *self.fallbacks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "primary": self.primary,
            "fallbacks": list(self.fallbacks),
            "rate_limit_rpm": self.rate_limit_rpm,
            "cache_ttl_s": self.cache_ttl_s,
        }


@dataclass
class GatewayConfig:
    providers: dict[str, ProviderSpec]
    routes: dict[str, RouteSpec]

    def __post_init__(self) -> None:
        if not self.providers:
            raise InvalidParameter("gateway config must define at least one provider")
        if not self.routes:
            raise InvalidParameter("gateway config must define at least one route")
        for route in self.routes.values():
            for provider_name in route.chain:
                if provider_name not in self.providers:
                    raise InvalidParameter(
                        f"route {route.name!r} references unknown provider {provider_name!r}"
                    )
        # A route name that shadows a provider name is legal but confusing.
        # Keep it legal; the two namespaces are separate.

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GatewayConfig":
        if not isinstance(raw, Mapping):
            raise InvalidParameter("gateway config must be a mapping")
        raw_providers = raw.get("providers") or {}
        raw_routes = raw.get("routes") or {}
        if not isinstance(raw_providers, Mapping) or not isinstance(raw_routes, Mapping):
            raise InvalidParameter("providers and routes must be mappings")
        providers: dict[str, ProviderSpec] = {}
        for name, item in raw_providers.items():
            if not isinstance(item, Mapping):
                raise InvalidParameter(f"provider {name!r} must be a mapping")
            unknown = set(item) - {
                "kind",
                "model",
                "cost_per_1k_input",
                "cost_per_1k_output",
                "timeout_ms",
                "max_output_tokens",
                "metadata",
            }
            if unknown:
                raise InvalidParameter(f"provider {name!r} has unknown keys: {sorted(unknown)}")
            providers[str(name)] = ProviderSpec(
                name=str(name),
                kind=str(item.get("kind", "mock")),
                model=str(item.get("model", "mock-1")),
                cost_per_1k_input=_config_float(
                    item.get("cost_per_1k_input", 0.0), "cost_per_1k_input"
                ),
                cost_per_1k_output=_config_float(
                    item.get("cost_per_1k_output", 0.0), "cost_per_1k_output"
                ),
                timeout_ms=_config_int(item.get("timeout_ms", 30_000), "timeout_ms"),
                max_output_tokens=_config_int(
                    item.get("max_output_tokens", 1024), "max_output_tokens"
                ),
                metadata=dict(item.get("metadata") or {}),
            )
        routes: dict[str, RouteSpec] = {}
        for name, item in raw_routes.items():
            if not isinstance(item, Mapping):
                raise InvalidParameter(f"route {name!r} must be a mapping")
            unknown = set(item) - {"primary", "fallbacks", "rate_limit_rpm", "cache_ttl_s"}
            if unknown:
                raise InvalidParameter(f"route {name!r} has unknown keys: {sorted(unknown)}")
            fallbacks = item.get("fallbacks") or []
            if isinstance(fallbacks, str) or not isinstance(fallbacks, Sequence):
                raise InvalidParameter(f"route {name!r} fallbacks must be a list")
            routes[str(name)] = RouteSpec(
                name=str(name),
                primary=str(item.get("primary", "")),
                fallbacks=tuple(str(value) for value in fallbacks),
                rate_limit_rpm=(
                    _config_int(item["rate_limit_rpm"], "rate_limit_rpm")
                    if item.get("rate_limit_rpm") is not None
                    else None
                ),
                cache_ttl_s=_config_int(item.get("cache_ttl_s", 0), "cache_ttl_s"),
            )
        return cls(providers=providers, routes=routes)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "GatewayConfig":
        config_path = Path(path)
        if not config_path.is_file():
            raise InvalidParameter(f"gateway config not found: {config_path}")
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise InvalidParameter(f"invalid gateway YAML: {exc}") from exc
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": {name: spec.to_dict() for name, spec in sorted(self.providers.items())},
            "routes": {name: spec.to_dict() for name, spec in sorted(self.routes.items())},
        }


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class GatewayResponse:
    request_id: str
    route: str
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    attempts: tuple[dict[str, Any], ...] = ()
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "route": self.route,
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "attempts": [dict(item) for item in self.attempts],
            "cache_hit": self.cache_hit,
        }


class Provider(Protocol):
    spec: ProviderSpec

    def complete(self, messages: Sequence[Mapping[str, Any]], **params: Any) -> ProviderResponse:
        ...


class MockProvider:
    """Deterministic provider used by tests, examples and local demos."""

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        fail_times: int = 0,
        latency_ms: float = 0.0,
        prefix: str = "[mock]",
    ) -> None:
        self.spec = spec
        self.fail_times = max(0, int(fail_times))
        self.latency_ms = max(0.0, float(latency_ms))
        self.prefix = prefix
        self._lock = threading.Lock()

    def complete(self, messages: Sequence[Mapping[str, Any]], **params: Any) -> ProviderResponse:
        with self._lock:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise ProviderError(f"mock provider {self.spec.name!r} injected failure")
        if self.latency_ms:
            time.sleep(min(self.latency_ms / 1000.0, 0.25))
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break
        max_tokens = int(params.get("max_tokens") or self.spec.max_output_tokens)
        text = f"{self.prefix} {self.spec.model}: {last_user}".strip()
        if len(text) > max_tokens * 4:
            text = text[: max_tokens * 4]
        input_text = "\n".join(str(item.get("content", "")) for item in messages)
        return ProviderResponse(
            text=text,
            model=self.spec.model,
            input_tokens=_estimate_tokens(input_text),
            output_tokens=_estimate_tokens(text),
        )


class CallableProvider:
    """Adapter for a real SDK without adding a dependency to the tutorial.

    The callable receives ``(messages, model, **params)`` and returns either a
    string or ``{"text": ..., "input_tokens": ..., "output_tokens": ...}``.
    Exceptions are converted to ``ProviderError`` so the router fails over.
    """

    def __init__(self, spec: ProviderSpec, func: Callable[..., Any]) -> None:
        self.spec = spec
        self.func = func

    def complete(self, messages: Sequence[Mapping[str, Any]], **params: Any) -> ProviderResponse:
        try:
            result = self.func(list(messages), self.spec.model, **params)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise ProviderError(f"provider {self.spec.name!r} failed: {exc}") from exc
        if isinstance(result, str):
            return ProviderResponse(
                text=result,
                model=self.spec.model,
                input_tokens=_estimate_tokens(
                    "\n".join(str(item.get("content", "")) for item in messages)
                ),
                output_tokens=_estimate_tokens(result),
            )
        if not isinstance(result, Mapping) or "text" not in result:
            raise ProviderError("callable provider must return a string or {'text': ...}")
        return ProviderResponse(
            text=str(result["text"]),
            model=str(result.get("model") or self.spec.model),
            input_tokens=int(result.get("input_tokens", 0)),
            output_tokens=int(result.get("output_tokens", 0)),
        )


def _estimate_tokens(text: str) -> int:
    # A deliberately rough heuristic: real tokenizers are model-specific, and
    # pretending otherwise in a teaching implementation would be misleading.
    return max(1, len(text) // 4 + len(text.split()) // 2)


class _TokenBucket:
    def __init__(self, rpm: int) -> None:
        self.capacity = float(rpm)
        self.tokens = float(rpm)
        self.rate_per_second = rpm / 60.0
        self.updated = time.monotonic()

    def acquire(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate_per_second)
        self.updated = now
        if self.tokens < 1.0:
            needed = (1.0 - self.tokens) / self.rate_per_second
            raise RateLimitExceeded(
                f"route rate limit exceeded; retry in {needed:.2f}s", retry_after_s=needed
            )
        self.tokens -= 1.0


@dataclass
class _CacheEntry:
    expires_at: float
    response: GatewayResponse


class Gateway:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        providers: Mapping[str, Provider] | None = None,
        cache_size: int = 256,
    ) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_order: list[str] = []
        self._cache_size = max(1, int(cache_size))
        self._buckets = {
            name: _TokenBucket(route.rate_limit_rpm)
            for name, route in config.routes.items()
            if route.rate_limit_rpm is not None
        }
        self._stats: dict[str, dict[str, Any]] = {
            name: {
                "requests": 0,
                "successes": 0,
                "errors": 0,
                "cache_hits": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "cache_saved_usd": 0.0,
                "latency_ms_total": 0.0,
                "latency_ms_max": 0.0,
                "providers": {},
            }
            for name in config.routes
        }
        self.providers: dict[str, Provider] = {}
        for name, spec in config.providers.items():
            # A "callable" provider must be registered explicitly. Silently
            # substituting a mock would make a misconfigured production route
            # look healthy while returning fake answers.
            if spec.kind == "mock":
                self.providers[name] = MockProvider(spec)
        for name, provider in (providers or {}).items():
            if name not in config.providers:
                raise InvalidParameter(f"provider {name!r} is not present in gateway config")
            self.providers[name] = provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def routes(self) -> list[dict[str, Any]]:
        return [route.to_dict() for route in self.config.routes.values()]

    def complete(
        self,
        route: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        params: Mapping[str, Any] | None = None,
    ) -> GatewayResponse:
        spec = self.config.routes.get(route)
        if spec is None:
            raise RouteNotFound(f"gateway route {route!r} not found")
        clean_messages = _validate_messages(messages)
        clean_params = dict(params or {})
        if len(compact_json(clean_params).encode("utf-8")) > 64 * 1024:
            raise InvalidParameter("gateway params are too large")
        started = time.perf_counter()
        request_id = new_id()

        cache_key: str | None = None
        if spec.cache_ttl_s > 0:
            cache_key = hashlib.sha256(
                compact_json(
                    {"route": route, "messages": clean_messages, "params": clean_params}
                ).encode("utf-8")
            ).hexdigest()
            with self._lock:
                entry = self._cache.get(cache_key)
                if entry is not None and entry.expires_at > time.monotonic():
                    cached = replace(
                        entry.response,
                        request_id=request_id,
                        cache_hit=True,
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                    self._record(route, cached, error=False, cache_hit=True)
                    return cached
                if entry is not None:
                    self._cache.pop(cache_key, None)

        bucket = self._buckets.get(route)
        if bucket is not None:
            with self._lock:
                bucket.acquire()

        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for provider_name in spec.chain:
            provider = self.providers.get(provider_name)
            if provider is None:
                attempts.append(
                    {"provider": provider_name, "ok": False, "error": "provider not registered"}
                )
                last_error = ProviderError(f"provider {provider_name!r} not registered")
                continue
            attempt_start = time.perf_counter()
            try:
                result = provider.complete(clean_messages, **clean_params)
            except Exception as exc:  # noqa: BLE001 - router must fail over
                last_error = exc
                attempts.append(
                    {
                        "provider": provider_name,
                        "model": provider.spec.model,
                        "ok": False,
                        "error": str(exc),
                        "latency_ms": (time.perf_counter() - attempt_start) * 1000,
                    }
                )
                continue
            provider_spec = self.config.providers[provider_name]
            cost = (
                result.input_tokens / 1000.0 * provider_spec.cost_per_1k_input
                + result.output_tokens / 1000.0 * provider_spec.cost_per_1k_output
            )
            response = GatewayResponse(
                request_id=request_id,
                route=route,
                text=result.text,
                provider=provider_name,
                model=result.model,
                input_tokens=int(result.input_tokens),
                output_tokens=int(result.output_tokens),
                cost_usd=round(cost, 10),
                latency_ms=(time.perf_counter() - started) * 1000,
                attempts=tuple(attempts + [{"provider": provider_name, "ok": True}]),
            )
            with self._lock:
                if cache_key is not None and spec.cache_ttl_s > 0:
                    self._cache[cache_key] = _CacheEntry(
                        expires_at=time.monotonic() + spec.cache_ttl_s,
                        response=response,
                    )
                    self._cache_order.append(cache_key)
                    while len(self._cache_order) > self._cache_size:
                        oldest = self._cache_order.pop(0)
                        self._cache.pop(oldest, None)
                self._record(route, response, error=False, cache_hit=False)
            return response

        with self._lock:
            self._stats[route]["errors"] += 1
            self._stats[route]["requests"] += 1
        detail = "; ".join(str(item.get("error")) for item in attempts) or "no providers configured"
        raise ProviderError(
            f"all providers failed for route {route!r}: {detail}",
            details={"route": route, "attempts": attempts},
        )

    def invoke(self, route: str, prompt: str, **params: Any) -> GatewayResponse:
        return self.complete(
            route, [{"role": "user", "content": prompt}], params=params
        )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for value in self._stats.values():
                value.update(
                    {
                        "requests": 0,
                        "successes": 0,
                        "errors": 0,
                        "cache_hits": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "cache_saved_usd": 0.0,
                        "latency_ms_total": 0.0,
                        "latency_ms_max": 0.0,
                        "providers": {},
                    }
                )

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._cache_order.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record(
        self,
        route: str,
        response: GatewayResponse,
        *,
        error: bool,
        cache_hit: bool,
    ) -> None:
        bucket = self._stats[route]
        bucket["requests"] += 1
        if error:
            bucket["errors"] += 1
        else:
            bucket["successes"] += 1
        if cache_hit:
            # A cache hit did not call a provider: count the request and the
            # avoided spend, but do not double-count tokens/cost or provider
            # calls. Otherwise "cost" would grow with cache usage, which is
            # the opposite of what a cache is for.
            bucket["cache_hits"] += 1
            bucket["cache_saved_usd"] = round(
                bucket["cache_saved_usd"] + response.cost_usd, 10
            )
            return
        bucket["input_tokens"] += response.input_tokens
        bucket["output_tokens"] += response.output_tokens
        bucket["cost_usd"] += response.cost_usd
        bucket["latency_ms_total"] += response.latency_ms
        bucket["latency_ms_max"] = max(bucket["latency_ms_max"], response.latency_ms)
        provider_stats = bucket["providers"].setdefault(
            response.provider, {"calls": 0, "cost_usd": 0.0}
        )
        provider_stats["calls"] += 1
        provider_stats["cost_usd"] = round(
            provider_stats["cost_usd"] + response.cost_usd, 10
        )

    def public_config(self) -> dict[str, Any]:
        """Configuration safe to expose over HTTP; secrets are redacted."""
        return redact(self.config.to_dict())


def _validate_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise InvalidParameter("messages must be a sequence of {role, content} objects")
    if not messages:
        raise InvalidParameter("messages must not be empty")
    clean: list[dict[str, Any]] = []
    for index, item in enumerate(messages):
        if not isinstance(item, Mapping):
            raise InvalidParameter(f"message {index} must be an object")
        role = str(item.get("role", ""))
        if role not in {"system", "user", "assistant", "tool"}:
            raise InvalidParameter(
                f"message {index} role must be system, user, assistant or tool"
            )
        content = item.get("content")
        if not isinstance(content, str):
            raise InvalidParameter(f"message {index} content must be a string")
        if len(content.encode("utf-8")) > 1_000_000:
            raise InvalidParameter(f"message {index} content exceeds 1 MB")
        clean.append({"role": role, "content": content})
    return clean


def load_gateway(path: str | Path) -> Gateway:
    return Gateway(GatewayConfig.from_yaml(path))


def env_key(provider: ProviderSpec, variable: str) -> str:
    """Read an API key at call time, never from serialized configuration."""
    value = os.environ.get(variable)
    if not value:
        raise ProviderError(f"environment variable {variable!r} is not set")
    return value
