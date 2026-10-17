"""远端/本地模型的**接口桩**：导入永远安全，调用才报错。

## 为什么要有这样一个文件

教程里最尴尬的一段是："这里你可以用 OpenAI 的 embedding，把 key 填进去就行"。
读者照抄，然后：

* 没装 ``openai`` → ``ImportError: No module named 'openai'``，
  而且是在 ``import lancedb`` 那一行就炸了，整章都跑不动；
* 装了但没 key → 运行到一半抛一个 HTTP 401，前面 3 分钟的计算全废；
* 有 key 但只想跑那个"离线 demo" → 白白被拦在门外。

所以这里的每个 provider 都是**两段的**：

1. **类定义阶段**（导入时）—— 零依赖、零网络。所有模型名、维度、
   参数名都是**真值**，可以当文档读，``ndims()`` 也能直接调；
2. **调用阶段**（``compute_source_vectors``）—— 缺 SDK 就抛
   :class:`~mini_lancedb.errors.NotSupportedError`，消息里带**确切的安装命令**。

这也回答了"接口桩有什么用"：它把"我要用哪个模型、多少维、怎么配"
这件事和"我这台机器能不能跑"解耦了。离线环境里你可以读完这张表、
把配置写进表 metadata、把 pipeline 搭好，等有了依赖再换一行。

## 各家的真值（截至 2026 年，都是官方文档里的模型名）

.. list-table::
   :header-rows: 1

   * - SOURCE
     - 包
     - 典型模型
     - 维度
   * - ``openai``
     - ``openai``
     - ``text-embedding-3-small``
     - 1536（``text-embedding-3-large`` 3072，且支持 ``dimensions`` 截断）
   * - ``sentence-transformers``
     - ``sentence-transformers``
     - ``all-MiniLM-L6-v2``
     - 384（本地跑，不用 key）
   * - ``cohere``
     - ``cohere``
     - ``embed-multilingual-v3.0``
     - 1024（中文场景常用；``embed-english-v3.0`` 也是 1024）
   * - ``jina``
     - ``requests``（HTTP）
     - ``jina-embeddings-v3``
     - 1024（``jina-embeddings-v2-base-zh`` 是 768）
   * - ``ollama``
     - ``requests``（HTTP）
     - ``nomic-embed-text``
     - 768（完全本地；``mxbai-embed-large`` 是 1024）
   * - ``huggingface``
     - ``transformers`` + ``torch``
     - ``BAAI/bge-small-zh-v1.5``
     - 512（中文首选之一；``BAAI/bge-base-zh-v1.5`` 是 768）
   * - ``gemini``
     - ``google-genai``
     - ``text-embedding-004``
     - 768（``gemini-embedding-001`` 支持 768/1536/3072）

**"维度"这件事值得单独说一句**：它决定表里向量列的
``fixed_size_list<float32>[D]``。写错维度的后果不是报错，而是
"写入时被静默截断/补零"或者"查询时维度不匹配"。所以每个 provider 都把
``ndims()`` 写死在类里，而不是等到调用时才知道。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from ..errors import NotSupportedError
from .base import TextEmbeddingFunction, register_embedding_function

__all__ = [
    "OpenAIEmbeddings",
    "SentenceTransformerEmbeddings",
    "CohereEmbeddings",
    "JinaEmbeddings",
    "OllamaEmbeddings",
    "HuggingFaceEmbeddings",
    "GeminiEmbeddings",
    "PROVIDERS",
    "describe_providers",
    "format_providers_table",
]


def _missing(package: str, *, extra: str = "", model: str = "") -> NotSupportedError:
    """统一的"缺依赖"报错。

    报错信息里必须有三样东西：**装什么**、**为什么没装也能用别的**、
    **离线替代品是什么**。缺一样，读者就会卡在这一步。
    """

    hint = f"安装命令: pip install {package}"
    if extra:
        hint += f" {extra}"
    hint += "；本仓库不强制这个依赖 —— 不想装就用内置的 hashing / char-ngram（离线确定）"
    if model:
        hint += f"；这个 provider 的典型模型是 {model}"
    return NotSupportedError(f"未安装 {package}（provider 只是接口桩）", hint=hint)


# ---------------------------------------------------------------------------
# 1. OpenAI
# ---------------------------------------------------------------------------


@register_embedding_function("openai", builtin=True)
class OpenAIEmbeddings(TextEmbeddingFunction):
    """OpenAI ``/v1/embeddings``（也兼容任何 OpenAI 协议的服务）。

    Parameters
    ----------
    name : str, default "text-embedding-3-small"
        真实模型名。也可以是本地 vLLM / Ollama 暴露的同协议模型名。
    dim : int, optional
        ``text-embedding-3-*`` 支持 ``dimensions`` 参数**截断**输出维度
        （Matryoshka 训练的结果）。截断是省存储的正经手段，
        不是"偷工减料"：1536 → 512 通常只掉几个点的召回。
    base_url : str, optional
        改成本地地址（``http://localhost:11434/v1``）就能指向 Ollama。
    api_key : str, optional
        **必须**用 ``"$var:OPENAI_API_KEY"`` 的形式传，不能硬编码。
    """

    name = "text-embedding-3-small"
    DEFAULT_DIMS = {
        "text-embedding-ada-002": 1536,
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
    }

    def __init__(
        self,
        name: str = "text-embedding-3-small",
        *,
        dim: Optional[int] = None,
        base_url: Optional[str] = None,
        organization: Optional[str] = None,
        api_key: Optional[str] = None,
        batch_size: int = 256,
    ) -> None:
        super().__init__(
            name=name, dim=dim, base_url=base_url, organization=organization,
            api_key=api_key, batch_size=batch_size,
        )
        self.name = name
        self.dim = dim
        self.base_url = base_url
        self.organization = organization
        self.api_key = api_key
        self.batch_size = batch_size

    @staticmethod
    def sensitive_keys() -> List[str]:
        return ["api_key"]

    def _model_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dim": self.dim,
            "base_url": self.base_url,
            "organization": self.organization,
            "api_key": self.api_key,
            "batch_size": self.batch_size,
        }

    def ndims(self) -> int:
        if self.dim is not None:
            return int(self.dim)
        if self.name in self.DEFAULT_DIMS:
            return self.DEFAULT_DIMS[self.name]
        # 未知模型名：只能要求调用方显式给 dim，绝不猜
        raise NotSupportedError(
            f"不知道模型 {self.name!r} 的维度",
            hint="显式传 dim=...，或把它加进 DEFAULT_DIMS",
        )

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        raise _missing("openai", model="text-embedding-3-small")


# ---------------------------------------------------------------------------
# 2. Sentence Transformers（本地，不需要 key）
# ---------------------------------------------------------------------------


@register_embedding_function("sentence-transformers", builtin=True)
class SentenceTransformerEmbeddings(TextEmbeddingFunction):
    """本地 sentence-transformers。**没有 API key 的情况下最值得用的一个。**

    Parameters
    ----------
    name : str, default "all-MiniLM-L6-v2"
        384 维、80MB、CPU 上编码几千条/秒，适合 demo。
        中文场景换 ``BAAI/bge-small-zh-v1.5``（512 维）或
        ``shibing624/text2vec-base-chinese``（768 维）。
    device : str, optional
        ``"cpu"`` / ``"cuda"`` / ``"mps"``。None 表示自动选。
    normalize : bool, default True
        bge 系列**必须**归一化（官方要求），否则 cosine 分数会失真。
    """

    name = "all-MiniLM-L6-v2"
    KNOWN_DIMS = {
        "all-MiniLM-L6-v2": 384,
        "all-MiniLM-L12-v2": 384,
        "all-mpnet-base-v2": 768,
        "paraphrase-multilingual-MiniLM-L12-v2": 384,
        "BAAI/bge-small-zh-v1.5": 512,
        "BAAI/bge-base-zh-v1.5": 768,
        "BAAI/bge-large-zh-v1.5": 1024,
        "shibing624/text2vec-base-chinese": 768,
    }

    def __init__(
        self,
        name: str = "all-MiniLM-L6-v2",
        *,
        device: Optional[str] = None,
        normalize: bool = True,
        batch_size: int = 64,
        query_prefix: str = "",
        document_prefix: str = "",
    ) -> None:
        super().__init__(
            name=name, device=device, normalize=normalize, batch_size=batch_size,
            query_prefix=query_prefix, document_prefix=document_prefix,
        )
        self.name = name
        self.device = device
        self.normalize = normalize
        self.batch_size = batch_size
        #: bge 系列要求查询加指令前缀才有最佳效果。**这是一个真实的坑**：
        #: 忘了加前缀，检索质量会明显下降但不会报错。
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix

    def _model_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "device": self.device,
            "normalize": self.normalize,
            "batch_size": self.batch_size,
            "query_prefix": self.query_prefix,
            "document_prefix": self.document_prefix,
        }

    def ndims(self) -> int:
        if self.name in self.KNOWN_DIMS:
            return self.KNOWN_DIMS[self.name]
        raise NotSupportedError(
            f"不知道模型 {self.name!r} 的维度",
            hint="从 HF 模型卡里查出来（hidden_size），或先跑一次看输出长度",
        )

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        raise _missing("sentence-transformers", model="all-MiniLM-L6-v2")


# ---------------------------------------------------------------------------
# 3. Cohere
# ---------------------------------------------------------------------------


@register_embedding_function("cohere", builtin=True)
class CohereEmbeddings(TextEmbeddingFunction):
    """Cohere ``/v2/embed``。

    Parameters
    ----------
    name : str, default "embed-multilingual-v3.0"
        1024 维，100+ 语言，中文表现稳定。
        ``embed-english-v3.0`` 同样是 1024 维但只认英文。
        ``embed-multilingual-light-v3.0`` 是 384 维的轻量版。
    input_type : str, default "search_document"
        **Cohere 的必填参数，也是最容易搞错的一个**：
        写入库里的文本要用 ``"search_document"``，用户查询要用
        ``"search_query"``，两者编码出的向量分布**不一样**。
        用错等于查询和文档活在不同的向量空间里 —— 还是那句：
        不会报错，只是结果全错。
    """

    name = "embed-multilingual-v3.0"
    KNOWN_DIMS = {
        "embed-multilingual-v3.0": 1024,
        "embed-english-v3.0": 1024,
        "embed-multilingual-light-v3.0": 384,
        "embed-english-light-v3.0": 384,
        "embed-v4.0": 1536,
    }

    def __init__(
        self,
        name: str = "embed-multilingual-v3.0",
        *,
        input_type: str = "search_document",
        api_key: Optional[str] = None,
        truncate: str = "END",
    ) -> None:
        super().__init__(name=name, input_type=input_type, api_key=api_key, truncate=truncate)
        self.name = name
        self.input_type = input_type
        self.api_key = api_key
        self.truncate = truncate

    @staticmethod
    def sensitive_keys() -> List[str]:
        return ["api_key"]

    def _model_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "input_type": self.input_type,
            "api_key": self.api_key,
            "truncate": self.truncate,
        }

    def ndims(self) -> int:
        if self.name in self.KNOWN_DIMS:
            return self.KNOWN_DIMS[self.name]
        raise NotSupportedError(f"不知道模型 {self.name!r} 的维度")

    def compute_query_vectors(self, queries: Any, *args: Any, **kwargs: Any) -> List[Any]:
        # 查询路径**必须**换成 search_query —— 这是接口层面强制区分
        # 两条编码路径的现实例子
        raise _missing("cohere", model="embed-multilingual-v3.0")

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        raise _missing("cohere", model="embed-multilingual-v3.0")


# ---------------------------------------------------------------------------
# 4. Jina
# ---------------------------------------------------------------------------


@register_embedding_function("jina", builtin=True)
class JinaEmbeddings(TextEmbeddingFunction):
    """Jina AI ``https://api.jina.ai/v1/embeddings``。

    Parameters
    ----------
    name : str, default "jina-embeddings-v3"
        1024 维，中英都不错，支持 ``task`` 参数做查询/文档区分。
        ``jina-embeddings-v2-base-zh`` 是 768 维的中文专用版。
    task : str, default "retrieval.passage"
        写入用 ``"retrieval.passage"``，查询用 ``"retrieval.query"``。
    """

    name = "jina-embeddings-v3"
    KNOWN_DIMS = {
        "jina-embeddings-v3": 1024,
        "jina-embeddings-v2-base-zh": 768,
        "jina-embeddings-v2-base-en": 768,
        "jina-clip-v1": 768,
    }

    def __init__(
        self,
        name: str = "jina-embeddings-v3",
        *,
        task: str = "retrieval.passage",
        api_key: Optional[str] = None,
        base_url: str = "https://api.jina.ai/v1/embeddings",
    ) -> None:
        super().__init__(name=name, task=task, api_key=api_key, base_url=base_url)
        self.name = name
        self.task = task
        self.api_key = api_key
        self.base_url = base_url

    @staticmethod
    def sensitive_keys() -> List[str]:
        return ["api_key"]

    def _model_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "task": self.task,
            "api_key": self.api_key,
            "base_url": self.base_url,
        }

    def ndims(self) -> int:
        if self.name in self.KNOWN_DIMS:
            return self.KNOWN_DIMS[self.name]
        raise NotSupportedError(f"不知道模型 {self.name!r} 的维度")

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        raise _missing("requests", model="jina-embeddings-v3")


# ---------------------------------------------------------------------------
# 5. Ollama（完全本地）
# ---------------------------------------------------------------------------


@register_embedding_function("ollama", builtin=True)
class OllamaEmbeddings(TextEmbeddingFunction):
    """本地 Ollama 服务（``POST /api/embed``）。**零 API key、零费用。**

    Parameters
    ----------
    name : str, default "nomic-embed-text"
        768 维。``mxbai-embed-large`` 是 1024 维且质量更好。
        中文可以试 ``bge-m3``（1024 维）。
    base_url : str, default "http://localhost:11434"
        ``ollama serve`` 的默认地址。

    注意它不是 OpenAI 协议，所以不能把 ``base_url`` 塞给
    :class:`OpenAIEmbeddings``（那个要 ``/v1`` 路径）。反过来，
    Ollama **也**提供 OpenAI 兼容层（``http://localhost:11434/v1``），
    走那条路才可以用 OpenAIEmbeddings。
    """

    name = "nomic-embed-text"
    KNOWN_DIMS = {
        "nomic-embed-text": 768,
        "mxbai-embed-large": 1024,
        "bge-m3": 1024,
        "all-minilm": 384,
    }

    def __init__(
        self,
        name: str = "nomic-embed-text",
        *,
        base_url: str = "http://localhost:11434",
        timeout: float = 60.0,
    ) -> None:
        super().__init__(name=name, base_url=base_url, timeout=timeout)
        self.name = name
        self.base_url = base_url
        self.timeout = timeout

    def _model_config(self) -> Dict[str, Any]:
        return {"name": self.name, "base_url": self.base_url, "timeout": self.timeout}

    def ndims(self) -> int:
        if self.name in self.KNOWN_DIMS:
            return self.KNOWN_DIMS[self.name]
        raise NotSupportedError(f"不知道模型 {self.name!r} 的维度")

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        raise NotSupportedError(
            "Ollama provider 只是接口桩",
            hint=(
                f"需要本地跑 ollama serve 并 pull 模型（ollama pull {self.name}），"
                "然后 pip install requests；离线环境用内置的 hashing 兜底"
            ),
        )


# ---------------------------------------------------------------------------
# 6. HuggingFace transformers
# ---------------------------------------------------------------------------


@register_embedding_function("huggingface", builtin=True)
class HuggingFaceEmbeddings(TextEmbeddingFunction):
    """直接用 ``transformers`` 跑 BERT 类模型（自己写池化）。

    Parameters
    ----------
    name : str, default "BAAI/bge-small-zh-v1.5"
        512 维，中文首选之一（榜单质量/体积平衡好）。
        ``BAAI/bge-base-zh-v1.5`` 是 768 维，
        ``BAAI/bge-large-zh-v1.5`` 是 1024 维。
    pooling : str, default "cls"
        bge 用 CLS 池化；``sentence-transformers`` 的很多模型用 mean 池化。
        **池化方式和模型不匹配是无声的错误**：向量还在、维度还对、
        只是语义散了。
    normalize : bool, default True
    """

    name = "BAAI/bge-small-zh-v1.5"
    KNOWN_DIMS = {
        "BAAI/bge-small-zh-v1.5": 512,
        "BAAI/bge-base-zh-v1.5": 768,
        "BAAI/bge-large-zh-v1.5": 1024,
        "BAAI/bge-small-en-v1.5": 384,
        "BAAI/bge-base-en-v1.5": 768,
        "shibing624/text2vec-base-chinese": 768,
    }

    def __init__(
        self,
        name: str = "BAAI/bge-small-zh-v1.5",
        *,
        pooling: str = "cls",
        normalize: bool = True,
        device: Optional[str] = None,
        max_length: int = 512,
        query_prefix: str = "",
    ) -> None:
        super().__init__(
            name=name, pooling=pooling, normalize=normalize,
            device=device, max_length=max_length, query_prefix=query_prefix,
        )
        self.name = name
        self.pooling = pooling
        self.normalize = normalize
        self.device = device
        #: ``max_length`` 直接决定截断：超长文本**后面的内容会被丢掉**，
        #: 而且不报错。长文档要分块，不能指望把 max_length 调大。
        self.max_length = max_length
        self.query_prefix = query_prefix

    def _model_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "pooling": self.pooling,
            "normalize": self.normalize,
            "device": self.device,
            "max_length": self.max_length,
            "query_prefix": self.query_prefix,
        }

    def ndims(self) -> int:
        if self.name in self.KNOWN_DIMS:
            return self.KNOWN_DIMS[self.name]
        raise NotSupportedError(f"不知道模型 {self.name!r} 的维度")

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        raise _missing("transformers torch", model="BAAI/bge-small-zh-v1.5")


# ---------------------------------------------------------------------------
# 7. Gemini
# ---------------------------------------------------------------------------


@register_embedding_function("gemini", builtin=True)
class GeminiEmbeddings(TextEmbeddingFunction):
    """Google ``google-genai`` 的 embedding API。

    Parameters
    ----------
    name : str, default "text-embedding-004"
        768 维。``gemini-embedding-001`` 支持通过 ``output_dimensionality``
        取 768 / 1536 / 3072。
    task_type : str, default "RETRIEVAL_DOCUMENT"
        写入用 ``"RETRIEVAL_DOCUMENT"``，查询用 ``"RETRIEVAL_QUERY"``。
    """

    name = "text-embedding-004"
    KNOWN_DIMS = {
        "text-embedding-004": 768,
        "gemini-embedding-001": 3072,
        "embedding-001": 768,
    }

    def __init__(
        self,
        name: str = "text-embedding-004",
        *,
        dim: Optional[int] = None,
        task_type: str = "RETRIEVAL_DOCUMENT",
        api_key: Optional[str] = None,
    ) -> None:
        super().__init__(name=name, dim=dim, task_type=task_type, api_key=api_key)
        self.name = name
        self.dim = dim
        self.task_type = task_type
        self.api_key = api_key

    @staticmethod
    def sensitive_keys() -> List[str]:
        return ["api_key"]

    def _model_config(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dim": self.dim,
            "task_type": self.task_type,
            "api_key": self.api_key,
        }

    def ndims(self) -> int:
        if self.dim is not None:
            return int(self.dim)
        if self.name in self.KNOWN_DIMS:
            return self.KNOWN_DIMS[self.name]
        raise NotSupportedError(f"不知道模型 {self.name!r} 的维度")

    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        raise _missing("google-genai", model="text-embedding-004")


# ---------------------------------------------------------------------------
# 描述表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderInfo:
    """一行 provider 说明。"""

    name: str          # 注册名（SOURCE）
    package: str       # pip 包名
    model: str         # 典型模型
    dims: int          # 维度
    needs_key: bool    # 是否需要 API key
    runs_locally: bool # 是否能完全本地跑
    note: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "package": self.package,
            "typical_model": self.model,
            "dims": self.dims,
            "needs_api_key": self.needs_key,
            "runs_locally": self.runs_locally,
            "note": self.note,
        }


#: SOURCE → 类。
PROVIDERS: Dict[str, type] = {
    "openai": OpenAIEmbeddings,
    "sentence-transformers": SentenceTransformerEmbeddings,
    "cohere": CohereEmbeddings,
    "jina": JinaEmbeddings,
    "ollama": OllamaEmbeddings,
    "huggingface": HuggingFaceEmbeddings,
    "gemini": GeminiEmbeddings,
}

_INFO: List[ProviderInfo] = [
    ProviderInfo("openai", "openai", "text-embedding-3-small", 1536, True, False,
                 "生态最广；支持 dimensions 截断（Matryoshka）"),
    ProviderInfo("sentence-transformers", "sentence-transformers", "all-MiniLM-L6-v2", 384, False, True,
                 "本地跑，不用 key；中文换 BAAI/bge-small-zh-v1.5"),
    ProviderInfo("cohere", "cohere", "embed-multilingual-v3.0", 1024, True, False,
                 "input_type 必须区分 search_document / search_query"),
    ProviderInfo("jina", "requests", "jina-embeddings-v3", 1024, True, False,
                 "HTTP 直调；task 参数区分 passage / query"),
    ProviderInfo("ollama", "requests", "nomic-embed-text", 768, False, True,
                 "完全本地；OpenAI 兼容层在 /v1 路径下"),
    ProviderInfo("huggingface", "transformers", "BAAI/bge-small-zh-v1.5", 512, False, True,
                 "自己写池化；bge 用 CLS，池化选错是无声错误"),
    ProviderInfo("gemini", "google-genai", "text-embedding-004", 768, True, False,
                 "task_type 区分 RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY"),
]


def describe_providers() -> List[Dict[str, Any]]:
    """返回 provider 表（``list[dict]``，可直接喂给 pandas / markdown 渲染）。

    Examples
    --------
    >>> rows = describe_providers()
    >>> [r["name"] for r in rows][:2]
    ['openai', 'sentence-transformers']
    """

    return [info.as_dict() for info in _INFO]


def format_providers_table() -> str:
    """把 :func:`describe_providers` 渲染成等宽文本表，方便直接贴进终端/文档。"""

    rows = describe_providers()
    header = ("name", "package", "typical_model", "dims", "needs_api_key", "runs_locally")
    widths = [max(len(h), *(len(str(r[h])) for r in rows)) for h in header]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(row[h]).ljust(w) for h, w in zip(header, widths)))
    return "\n".join(lines)
