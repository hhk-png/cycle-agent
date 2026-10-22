"""mini-deepinfer: a small, testable inference engine that mirrors DeepInfer's ideas.

The package intentionally depends only on the Python standard library and NumPy.
It is a teaching implementation, not a drop-in replacement for DeepSeek's
production DeepInfer.
"""

from __future__ import annotations

__all__ = [
    "AdapterRegistry",
    "AdmissionDecision",
    "BatchItem",
    "BatchReport",
    "DecodeEstimate",
    "EdgeDevice",
    "EdgeRequest",
    "EngineConfig",
    "Gateway",
    "Graph",
    "GroupPlan",
    "HashEmbedder",
    "ImageEmbeddingCache",
    "ImageTokenPlan",
    "KVNamespace",
    "LLM",
    "LLMEngine",
    "LoRAAdapter",
    "MemoryPlan",
    "ModelConfig",
    "ModelRouter",
    "ModelSpec",
    "ModelManifest",
    "ParallelConfig",
    "PartialRollout",
    "PromptGroup",
    "ReasoningBudget",
    "ReasoningTracker",
    "RolloutConfig",
    "SamplingParams",
    "Segment",
    "TenantQuota",
    "ThinkingStreamParser",
    "Tracer",
    "TrafficRequest",
    "VectorIndex",
    "WeightVersion",
    "Worker",
    "admit",
    "adoption_curve",
    "breakeven_utilization",
    "dcf_value",
    "funnel_conversion",
    "obtainable_market",
    "revenue_multiple_value",
    "sales_capacity",
    "serviceable_market",
    "tam_bottom_up",
    "terminal_value",
    "cac_payback_months",
    "capacity_buffer",
    "circuit_breaker",
    "committed_contract_value",
    "estimate_decode",
    "estimate_rollout_cost",
    "group_advantages",
    "irr",
    "load_shedding",
    "ltv",
    "magic_number",
    "market_share",
    "mm1",
    "mmc",
    "npv",
    "payback_period",
    "plan_groups",
    "plan_memory",
    "plan_waves",
    "price_for_margin",
    "retry_budget",
    "rollout_throughput",
    "route",
    "route_edge_or_cloud",
    "run_offline_batch",
    "scenario_table",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    # Lazy re-exports keep ``import mini_deepinfer.model`` usable while the
    # engine modules are still importing each other.
    if name in ("EngineConfig", "ModelConfig", "ParallelConfig"):
        from . import config

        return getattr(config, name)
    if name == "SamplingParams":
        from .sampling_params import SamplingParams

        return SamplingParams
    if name == "LLMEngine":
        from .engine import LLMEngine

        return LLMEngine
    if name == "LLM":
        from .llm import LLM

        return LLM
    if name == "ModelManifest":
        from .manifest import ModelManifest

        return ModelManifest
    if name in ("Gateway", "TenantQuota"):
        from . import gateway

        return getattr(gateway, name)
    if name == "Tracer":
        from .tracing import Tracer

        return Tracer
    if name in ("BatchItem", "BatchReport", "plan_waves", "run_offline_batch"):
        from . import offline_batch

        return getattr(offline_batch, name)
    if name in ("ImageEmbeddingCache", "ImageTokenPlan"):
        from . import multimodal

        return getattr(multimodal, name)
    if name in ("ReasoningBudget", "ReasoningTracker", "ThinkingStreamParser"):
        from . import reasoning

        return getattr(reasoning, name)
    if name in ("AdapterRegistry", "LoRAAdapter"):
        from . import lora

        return getattr(lora, name)
    if name in ("ModelRouter", "ModelSpec"):
        from . import router

        return getattr(router, name)
    if name in ("HashEmbedder", "VectorIndex"):
        from . import embedding

        return getattr(embedding, name)
    if name == "Graph":
        from . import compiler

        return compiler.Graph
    if name in {
        "RolloutConfig",
        "PromptGroup",
        "GroupPlan",
        "PartialRollout",
        "WeightVersion",
        "KVNamespace",
        "plan_groups",
        "group_advantages",
        "estimate_rollout_cost",
        "rollout_throughput",
    }:
        from . import rl_rollout

        return getattr(rl_rollout, name)
    if name in {
        "EdgeDevice",
        "EdgeRequest",
        "MemoryPlan",
        "DecodeEstimate",
        "plan_memory",
        "estimate_decode",
        "route_edge_or_cloud",
    }:
        from . import edge

        return getattr(edge, name)
    if name in {
        "AdmissionDecision",
        "Worker",
        "TrafficRequest",
        "admit",
        "route",
        "retry_budget",
        "circuit_breaker",
        "capacity_buffer",
        "load_shedding",
        "mm1",
        "mmc",
    }:
        from . import traffic

        return getattr(traffic, name)
    if name in {
        "npv",
        "irr",
        "payback_period",
        "ltv",
        "cac_payback_months",
        "price_for_margin",
        "committed_contract_value",
        "breakeven_utilization",
        "scenario_table",
        "terminal_value",
        "dcf_value",
        "revenue_multiple_value",
        "implied_revenue_multiple",
        "venture_return",
    }:
        from . import finance

        return getattr(finance, name)
    if name in {
        "Segment",
        "tam_bottom_up",
        "serviceable_market",
        "obtainable_market",
        "adoption_curve",
        "funnel_conversion",
        "magic_number",
        "market_share",
        "sales_capacity",
    }:
        from . import market

        return getattr(market, name)
    raise AttributeError(name)
