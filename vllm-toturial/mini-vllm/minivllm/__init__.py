"""mini-vLLM: a readable, runnable re-implementation of the vLLM architecture.

The package is organised to mirror vLLM's own module layout:

    config       -- EngineArgs / ModelConfig / CacheConfig / SchedulerConfig
    tokenizer    -- (mini) tokenizer backend
    model        -- GPT-style transformer (ModelRunner)
    attention    -- PagedAttention
    kv_cache     -- BlockManager / BlockAllocator / PrefixCache / KVStore
    scheduler    -- continuous batching + preemption + chunked prefill
    sampler      -- logits processing + token sampling
    engine       -- LLMEngine (synchronous orchestration)
    api_server   -- OpenAI-compatible FastAPI server
    cli          -- command line entry points
"""

from .config import (
    CacheConfig,
    EngineConfig,
    ModelConfig,
    SamplingParams,
    SchedulerConfig,
)
from .engine import LLMEngine
from .model import TinyGPT
from .tokenizer import CharTokenizer

__version__ = "0.1.0"

__all__ = [
    "CacheConfig",
    "CharTokenizer",
    "EngineConfig",
    "LLMEngine",
    "ModelConfig",
    "SamplingParams",
    "SchedulerConfig",
    "TinyGPT",
]
