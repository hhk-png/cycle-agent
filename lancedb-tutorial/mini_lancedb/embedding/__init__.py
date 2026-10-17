"""向量化函数包：把文本（或图片）变成向量，并让表记住用了哪个模型。

导入本包会**自动注册**所有内置函数和 provider 接口桩 ——
这是刻意的：``get_registry().get("hashing")`` 一句就能用，
不需要先 import 什么。代价是 import 顺序有副作用（注册表被写入），
所以：

* 注册名（``SOURCE``）一旦发布就不能改，见 :mod:`.base` 的说明；
* 想清掉用户后注册的函数用 ``get_registry().reset()``，
  它会保留内置那批（不会把 ``hashing`` 也清掉）。

.. list-table:: 内置（离线、确定、无依赖）
   :header-rows: 1

   * - 注册名
     - 类
     - 维数
     - 特点
   * - ``hashing``
     - :class:`~mini_lancedb.embedding.builtin.HashingEmbeddingFunction`
     - 128
     - 哈希词袋 + 次线性 TF + L2，认得**词面重叠**
   * - ``char-ngram``
     - :class:`~mini_lancedb.embedding.builtin.CharNgramEmbeddingFunction`
     - 256
     - 字符 3-gram，**中文首选兜底**
   * - ``fake``
     - :class:`~mini_lancedb.embedding.builtin.DeterministicFakeEmbedding`
     - 128
     - 种子高斯，**只准用于性能测试**，无任何语义
   * - ``bow``
     - :class:`~mini_lancedb.embedding.builtin.BOWEmbeddingFunction`
     - len(vocab)
     - 精确词袋，**可手算验证**

.. list-table:: Provider 接口桩（导入安全，调用需装依赖）
   :header-rows: 1

   * - 注册名
     - 包
     - 典型模型
     - 维数
   * - ``openai``
     - ``openai``
     - ``text-embedding-3-small``
     - 1536
   * - ``sentence-transformers``
     - ``sentence-transformers``
     - ``all-MiniLM-L6-v2``
     - 384
   * - ``cohere``
     - ``cohere``
     - ``embed-multilingual-v3.0``
     - 1024
   * - ``jina``
     - ``requests``
     - ``jina-embeddings-v3``
     - 1024
   * - ``ollama``
     - ``requests``
     - ``nomic-embed-text``
     - 768
   * - ``huggingface``
     - ``transformers``
     - ``BAAI/bge-small-zh-v1.5``
     - 512
   * - ``gemini``
     - ``google-genai``
     - ``text-embedding-004``
     - 768
"""

from __future__ import annotations

from .base import (
    TEXT,
    EmbeddingFunction,
    EmbeddingFunctionConfig,
    EmbeddingFunctionRegistry,
    TextEmbeddingFunction,
    VectorQuery,
    get_registry,
    register_embedding_function,
    resolve_variables,
    retry_call,
    retry_with_exponential_backoff,
    with_retry,
)
from .builtin import (
    BOWEmbeddingFunction,
    CharNgramEmbeddingFunction,
    DeterministicFakeEmbedding,
    HashingEmbeddingFunction,
    builtin_function_names,
    tokenize,
)
from .providers import (
    PROVIDERS,
    CohereEmbeddings,
    GeminiEmbeddings,
    HuggingFaceEmbeddings,
    JinaEmbeddings,
    OllamaEmbeddings,
    OpenAIEmbeddings,
    ProviderInfo,
    SentenceTransformerEmbeddings,
    describe_providers,
    format_providers_table,
)

__all__ = [
    # 接口
    "EmbeddingFunction",
    "TextEmbeddingFunction",
    "EmbeddingFunctionConfig",
    "VectorQuery",
    "TEXT",
    # 注册表
    "EmbeddingFunctionRegistry",
    "get_registry",
    "register_embedding_function",
    "resolve_variables",
    # 重试
    "with_retry",
    "retry_call",
    "retry_with_exponential_backoff",
    # 内置
    "HashingEmbeddingFunction",
    "CharNgramEmbeddingFunction",
    "DeterministicFakeEmbedding",
    "BOWEmbeddingFunction",
    "builtin_function_names",
    "tokenize",
    # provider 桩
    "OpenAIEmbeddings",
    "SentenceTransformerEmbeddings",
    "CohereEmbeddings",
    "JinaEmbeddings",
    "OllamaEmbeddings",
    "HuggingFaceEmbeddings",
    "GeminiEmbeddings",
    "PROVIDERS",
    "ProviderInfo",
    "describe_providers",
    "format_providers_table",
]


def registered_embedding_functions() -> list:
    """当前注册表里的所有名字（给教程/``describe`` 用）。"""

    return get_registry().names()
