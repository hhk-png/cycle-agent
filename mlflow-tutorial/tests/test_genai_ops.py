from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import pytest

from mini_mlflow import gateway as gateway_module
from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.entities import Span, Trace
from mini_mlflow.errors import InvalidParameter, ResourceNotFound
from mini_mlflow.gateway import (
    CallableProvider,
    Gateway,
    GatewayConfig,
    MockProvider,
    ProviderError,
    ProviderSpec,
    RateLimitExceeded,
)
from mini_mlflow.server import create_server
from mini_mlflow.utils import new_id


def _trace(experiment_id: str = "0", *, name: str = "chat", tags: dict | None = None) -> Trace:
    trace_id = new_id()
    return Trace(
        trace_id=trace_id,
        name=name,
        experiment_id=experiment_id,
        tags=dict(tags or {}),
        spans=[
            Span(
                span_id="a" * 16,
                trace_id=trace_id,
                name=name,
                span_type="LLM",
                start_time_ns=1_000_000,
                end_time_ns=2_000_000,
                inputs={"q": "hi"},
                outputs={"a": "hello"},
            )
        ],
    )


# ---------------------------------------------------------------------------
# Prompt registry
# ---------------------------------------------------------------------------
def test_prompt_versions_alias_and_format(service) -> None:
    first = service.prompts.register_prompt(
        "support-reply",
        "Hello {{ name }}, order {{order_id}} is {{status}}.",
        commit_message="initial",
        tags={"owner": "support"},
    )
    second = service.prompts.register_prompt(
        "support-reply",
        "Hi {{name}}! Order {{order_id}}: {{status}}.",
        commit_message="tone",
    )
    assert (first.version, second.version) == (1, 2)
    assert first.variables == ["name", "order_id", "status"]
    assert first.format(name="Ada", order_id="A-1", status="shipped") == (
        "Hello Ada, order A-1 is shipped."
    )
    with pytest.raises(InvalidParameter):
        first.format(name="Ada")
    with pytest.raises(InvalidParameter):
        first.format(name="Ada", order_id="A-1", status="ok", typo="x")

    service.prompts.set_alias("support-reply", "production", 2)
    loaded = service.prompts.load("prompts:/support-reply@production")
    assert loaded.version == 2
    assert service.prompts.load("prompts:/support-reply/1").version == 1
    assert service.prompts.load("support-reply").version == 2
    assert service.prompts.list_versions("support-reply")[0].aliases == ["production"]


def test_prompt_single_brace_conversion_keeps_literal_braces(service) -> None:
    prompt = service.prompts.register_prompt(
        "json-reply",
        'Return JSON: {"name": "{{name}}", "ok": true}',
    )
    single = prompt.to_single_brace_format()
    assert single == 'Return JSON: {{"name": "{name}", "ok": true}}'
    assert single.format(name="Ada") == 'Return JSON: {"name": "Ada", "ok": true}'


def test_prompt_rejects_unsafe_or_invalid_templates(service) -> None:
    with pytest.raises(InvalidParameter):
        service.prompts.register_prompt("bad", "{% for x in y %}{{x}}{% endfor %}")
    with pytest.raises(InvalidParameter):
        service.prompts.register_prompt("bad", "   ")
    with pytest.raises(InvalidParameter):
        service.prompts.register_prompt("bad name", "hello")
    service.prompts.register_prompt("ok", "hello")
    with pytest.raises(InvalidParameter):
        service.prompts.set_alias("ok", "123", 1)
    with pytest.raises(ResourceNotFound):
        service.prompts.load("ok@missing")
    service.prompts.register_prompt("deletable", "hi {{x}}")
    service.prompts.delete_prompt("deletable")
    with pytest.raises(ResourceNotFound):
        service.prompts.get("deletable")


def test_prompt_search(service) -> None:
    service.prompts.register_prompt("alpha", "a", tags={"team": "nlp"})
    service.prompts.register_prompt("beta", "b", tags={"team": "cv"})
    found = service.prompts.search_prompts("tags.team = 'nlp'")
    assert [item.name for item in found] == ["alpha"]
    ordered = service.prompts.search_prompts(order_by=["attributes.name DESC"])
    assert [item.name for item in ordered] == ["beta", "alpha"]


# ---------------------------------------------------------------------------
# Assessments
# ---------------------------------------------------------------------------
def test_feedback_upsert_and_trace_roundtrip(service) -> None:
    trace = service.log_trace(_trace())
    first = service.log_feedback(
        trace.trace_id,
        "quality",
        0.6,
        source_type="human",
        source_id="reviewer-1",
        rationale="too terse",
        metadata={"ticket": 12},
    )
    assert first.source_type == "HUMAN"
    second = service.log_feedback(
        trace.trace_id,
        "quality",
        0.9,
        source_type="HUMAN",
        source_id="reviewer-1",
        rationale="fixed",
    )
    assert second.assessment_id == first.assessment_id
    assert second.create_time == first.create_time
    assert second.value == 0.9

    stored = service.get_trace(trace.trace_id)
    assert len(stored.assessments) == 1
    assert stored.assessments[0].value == 0.9
    assert service.get_assessment(first.assessment_id).rationale == "fixed"

    expectation = service.log_feedback(
        trace.trace_id, "quality", 1.0, source_type="HUMAN", source_id="reviewer-1",
        is_expectation=True,
    )
    assert expectation.assessment_id != first.assessment_id
    assert len(service.list_assessments(trace.trace_id)) == 2
    service.delete_assessment(expectation.assessment_id)
    assert len(service.list_assessments(trace.trace_id)) == 1


def test_feedback_validation(service) -> None:
    trace = service.log_trace(_trace())
    with pytest.raises(InvalidParameter):
        service.log_feedback(trace.trace_id, "quality", {"nested": 1})
    with pytest.raises(InvalidParameter):
        service.log_feedback(trace.trace_id, "quality", 1.0, source_type="ROBOT")
    with pytest.raises(ResourceNotFound):
        service.log_feedback("f" * 32, "quality", 1.0)


def test_trace_search_by_assessment(service) -> None:
    good = service.log_trace(_trace(name="good", tags={"team": "nlp"}))
    bad = service.log_trace(_trace(name="bad", tags={"team": "nlp"}))
    service.log_feedback(good.trace_id, "quality", 0.95)
    service.log_feedback(bad.trace_id, "quality", 0.2)
    # An invalidated judgment must not resurrect a trace in the search.
    service.log_feedback(bad.trace_id, "quality", 0.99, valid=False)
    found = service.search_traces(filter_string="assessments.quality > 0.8")
    assert [item.trace_id for item in found] == [good.trace_id]
    found = service.search_traces(
        filter_string="assessments.quality > 0.8 and tags.team = 'nlp'"
    )
    assert [item.trace_id for item in found] == [good.trace_id]


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------
def _gateway_config(*, rate_limit: int | None = None, cache_ttl: int = 0) -> GatewayConfig:
    return GatewayConfig.from_dict(
        {
            "providers": {
                "fast": {
                    "kind": "mock",
                    "model": "mock-fast",
                    "cost_per_1k_input": 1.0,
                    "cost_per_1k_output": 2.0,
                },
                "strong": {"kind": "mock", "model": "mock-strong"},
            },
            "routes": {
                "chat": {
                    "primary": "fast",
                    "fallbacks": ["strong"],
                    "rate_limit_rpm": rate_limit,
                    "cache_ttl_s": cache_ttl,
                }
            },
        }
    )


def test_gateway_success_cost_and_stats() -> None:
    gw = Gateway(_gateway_config())
    response = gw.invoke("chat", "hello world")
    assert response.provider == "fast"
    assert response.text.startswith("[mock] mock-fast:")
    assert response.input_tokens > 0 and response.output_tokens > 0
    expected = (
        response.input_tokens / 1000 * 1.0 + response.output_tokens / 1000 * 2.0
    )
    assert response.cost_usd == pytest.approx(expected, rel=1e-6)
    stats = gw.stats()["chat"]
    assert stats["requests"] == 1 and stats["successes"] == 1
    assert stats["providers"]["fast"]["calls"] == 1


def test_gateway_fallback_and_total_failure() -> None:
    config = _gateway_config()
    primary = MockProvider(config.providers["fast"], fail_times=1)
    gw = Gateway(config, providers={"fast": primary})
    response = gw.invoke("chat", "hello")
    assert response.provider == "strong"
    assert [item["provider"] for item in response.attempts] == ["fast", "strong"]
    assert response.attempts[0]["ok"] is False

    always = MockProvider(config.providers["fast"], fail_times=10)
    fallback = MockProvider(config.providers["strong"], fail_times=10)
    gw = Gateway(config, providers={"fast": always, "strong": fallback})
    with pytest.raises(ProviderError) as excinfo:
        gw.invoke("chat", "hello")
    assert "all providers failed" in str(excinfo.value)
    assert excinfo.value.details["attempts"][0]["provider"] == "fast"


def test_gateway_rate_limit_and_cache() -> None:
    gw = Gateway(_gateway_config(rate_limit=1))
    gw.invoke("chat", "first")
    with pytest.raises(RateLimitExceeded):
        gw.invoke("chat", "second")

    gw = Gateway(_gateway_config(cache_ttl=60))
    first = gw.invoke("chat", "same")
    second = gw.invoke("chat", "same")
    assert first.cache_hit is False and second.cache_hit is True
    assert second.text == first.text
    stats = gw.stats()["chat"]
    assert stats["cache_hits"] == 1
    # A cache hit must not double-charge tokens/cost or provider calls.
    assert stats["cost_usd"] == pytest.approx(first.cost_usd)
    assert stats["cache_saved_usd"] == pytest.approx(first.cost_usd)
    assert stats["providers"]["fast"]["calls"] == 1
    different = gw.invoke("chat", "different")
    assert different.cache_hit is False


def test_gateway_callable_provider_and_config_validation() -> None:
    config = GatewayConfig.from_dict(
        {"providers": {"p": {"kind": "callable"}}, "routes": {"r": {"primary": "p"}}}
    )
    spec = config.providers["p"]
    provider = CallableProvider(
        spec,
        lambda messages, model, **params: {
            "text": messages[-1]["content"].upper(),
            "input_tokens": 3,
            "output_tokens": 2,
        },
    )
    gw = Gateway(config, providers={"p": provider})
    response = gw.complete("r", [{"role": "user", "content": "hi"}])
    assert response.text == "HI"
    assert response.cost_usd == 0.0

    # A missing callable registration must fail loudly, never silently mock.
    with pytest.raises(ProviderError):
        Gateway(config).complete("r", [{"role": "user", "content": "hi"}])

    with pytest.raises(InvalidParameter):
        GatewayConfig.from_dict({"providers": {"p": {}}, "routes": {"r": {"primary": "missing"}}})
    with pytest.raises(InvalidParameter):
        GatewayConfig.from_dict(
            {"providers": {"p": {"cost_per_1k_input": -1}}, "routes": {"r": {"primary": "p"}}}
        )
    with pytest.raises(InvalidParameter):
        GatewayConfig.from_dict(
            {"providers": {"p": {"typo": 1}}, "routes": {"r": {"primary": "p"}}}
        )
    with pytest.raises(ResourceNotFound):
        Gateway(config).complete("missing", [{"role": "user", "content": "x"}])


def test_gateway_rejects_bad_messages() -> None:
    gw = Gateway(_gateway_config())
    with pytest.raises(InvalidParameter):
        gw.complete("chat", [])
    with pytest.raises(InvalidParameter):
        gw.complete("chat", [{"role": "robot", "content": "x"}])
    with pytest.raises(InvalidParameter):
        gw.complete("chat", [{"role": "user", "content": {"x": 1}}])


# ---------------------------------------------------------------------------
# HTTP / remote parity
# ---------------------------------------------------------------------------
@pytest.fixture()
def genai_server(config: MiniConfig):
    gw = Gateway(_gateway_config(cache_ttl=30))
    server_config = config.derive(auth_token="secret-token")
    server = create_server(server_config, host="127.0.0.1", port=0, gateway=gw)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", gw
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_remote_prompt_feedback_and_gateway(genai_server) -> None:
    base, gw = genai_server
    client = MiniClient(base, auth_token="secret-token")
    try:
        prompt = client.register_prompt("remote-prompt", "Hi {{name}}", tags={"owner": "qa"})
        client.set_prompt_alias("remote-prompt", "prod", 1)
        loaded = client.load_prompt("prompts:/remote-prompt@prod")
        assert loaded.template == "Hi {{name}}"
        assert client.search_prompts("tags.owner = 'qa'")[0].name == "remote-prompt"
        assert len(client.list_prompt_versions("remote-prompt")) == 1

        trace = client.log_trace(_trace())
        assessment = client.log_feedback(trace.trace_id, "quality", 1.0, rationale="great")
        assert client.get_assessment(assessment.assessment_id).trace_id == trace.trace_id
        assert client.list_assessments(trace.trace_id)[0].value == 1.0
        assert client.get_trace(trace.trace_id).assessments[0].rationale == "great"
        client.delete_assessment(assessment.assessment_id)

        routes = client.gateway_routes()
        assert routes["routes"][0]["name"] == "chat"
        first = client.gateway_invoke(
            "chat", [{"role": "user", "content": "hello"}]
        )
        assert first["provider"] == "fast"
        second = client.gateway_invoke(
            "chat", [{"role": "user", "content": "hello"}]
        )
        assert second["cache_hit"] is True
        stats = client.gateway_stats()["stats"]["chat"]
        assert stats["requests"] == 2
        assert gw.stats()["chat"]["cache_hits"] == 1
    finally:
        client.close()


@pytest.fixture()
def plain_server(config: MiniConfig):
    server_config = config.derive(auth_token="secret-token")
    server = create_server(server_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_prompt_ui_renders_versions(genai_server) -> None:
    base, _ = genai_server
    client = MiniClient(base, auth_token="secret-token")
    try:
        client.register_prompt("ui-prompt", "Hi {{name}}", commit_message="ui")
        client.set_prompt_alias("ui-prompt", "prod", 1)
    finally:
        client.close()
    request = urllib.request.Request(
        base + "/ui/prompts", headers={"Authorization": "Bearer secret-token"}
    )
    with urllib.request.urlopen(request) as response:
        body = response.read().decode("utf-8")
    assert "ui-prompt" in body
    assert "Hi {{name}}" in body
    assert "prod" in body


def test_gateway_disabled_returns_404(plain_server) -> None:
    client = MiniClient(plain_server, auth_token="secret-token")
    try:
        with pytest.raises(ResourceNotFound) as excinfo:
            client.gateway_routes()
        assert "not configured" in str(excinfo.value)
    finally:
        client.close()
