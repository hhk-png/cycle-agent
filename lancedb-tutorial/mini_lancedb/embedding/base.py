"""向量化函数（embedding function）的接口与注册表。

## 为什么需要一个"注册表"

一张 LanceDB 表里存的是**向量**，不是文本。等到下次打开这张表、想
``table.search("一些中文")`` 时，系统必须知道：**当初是谁把这些文本变成
向量的**。这件事不可能靠猜 —— 用错的模型编码查询向量，结果是
"不报错的错答案"：向量还在、ANN 还跑得动、距离还算得出来，
只是距离的数值毫无意义，召回的是一堆随机文档。

真实 LanceDB 的做法是把"用哪个函数、什么参数、编码哪一列"**写进
Arrow schema 的 metadata**，key 是 ``embedding_functions``：

.. code-block:: text

    schema.metadata["embedding_functions"] =
      [
        {"name": "sentence-transformers",          ← 注册表里的 key
         "model": {"name": "all-MiniLM-L6-v2"},    ← 构造参数
         "source_column": "text",
         "vector_column": "vector"}
      ]

保存的时候写进去，打开的时候读出来 —— 注册表的作用就是把
``"name"`` 这个字符串**映射回一个可以 ``create(**model)`` 的类**。
所以 :attr:`EmbeddingFunction.SOURCE` 这个字符串是**数据格式的一部分**，
改了它，老表就打不开了。这就是本模块反复强调"注册名必须稳定"的原因。

注意这里有个真实的坑：metadata 里存的是**函数名 + 参数**，
不存模型权重。所以"换一个同名的自定义实现"会让老表静默读出垃圾向量。
真实系统靠"函数名带版本/机构前缀"（``"sentence-transformers"``、
``"openai"``）来降低这个风险，但本质上仍是一个社会约定。

## 重试

调用远端 embedding API 是**最容易遇到限流和瞬时故障**的地方
（一次批量 1000 条文本、跑几分钟、中间挂一次就前功尽弃）。
所以接口把"带重试的调用"做成内建方法（``*_with_retry``），
指数退避 + 抖动，而不是留给每个调用方自己写。
"""

from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass
from functools import wraps
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

import numpy as np

from ..errors import QueryError

__all__ = [
    "TEXT",
    "EmbeddingFunction",
    "TextEmbeddingFunction",
    "EmbeddingFunctionConfig",
    "VectorQuery",
    "EmbeddingFunctionRegistry",
    "get_registry",
    "register_embedding_function",
    "retry_with_exponential_backoff",
    "with_retry",
    "retry_call",
    "resolve_variables",
]

#: 文本类输入允许的形态。**刻意不包含 pandas**：调用方给 list 就好。
TEXT = Union[str, Sequence[str], np.ndarray]


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def retry_with_exponential_backoff(
    fn: Callable[..., Any],
    *,
    max_retries: int = 7,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.0,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
    sleep: Optional[Callable[[float], None]] = None,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> Callable[..., Any]:
    """给 ``fn`` 套一层指数退避重试。

    * ``max_retries=0`` 表示**不重试**（直接透传异常）——
      本地确定性函数套重试只会掩盖 bug，所以这是默认之外的选项；
    * 延迟序列：``base_delay * 2 ** (attempt - 1)``，封顶 ``max_delay``；
    * ``jitter`` 是**确定性抖动**（``attempt`` 的倍数），不是随机数：
      "抖动"在分布式系统里的作用是打散大量客户端的重试时刻，
      但在这个教学实现里，可复现比真实更重要 —— 测试要能断言"重试了 3 次"。
      真要随机抖动，把 ``jitter=0`` 换成一个随机函数传进 ``sleep`` 就行。

    ``sleep`` 可注入是**为了测试**：测 7 次退避不该真的睡 8 秒。
    """

    wait = sleep if sleep is not None else time.sleep

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except retry_on as exc:
                attempt += 1
                if attempt > max_retries:
                    raise
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                if jitter:
                    delay += jitter * attempt
                if on_retry is not None:
                    on_retry(attempt, exc)
                wait(delay)

    return wrapper


def with_retry(
    *, max_retries: int = 7, base_delay: float = 0.5, **kwargs: Any
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器形态的重试。用法::

        @with_retry(max_retries=3, base_delay=0.0)
        def call_api(batch): ...

    线程安全、可重入（每次调用各自计数，没有任何共享状态）——
    这正是重试装饰器最容易写错的地方。
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return retry_with_exponential_backoff(
            fn, max_retries=max_retries, base_delay=base_delay, **kwargs
        )

    return decorator


def retry_call(
    fn: Callable[..., Any], *args: Any, max_retries: int = 7, **kwargs: Any
) -> Any:
    """不带装饰器的调用形态。"""

    return retry_with_exponential_backoff(fn, max_retries=max_retries)(*args, **kwargs)


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


class EmbeddingFunction(abc.ABC):
    """向量化函数的抽象基类。

    子类必须实现三件事：

    1. :meth:`ndims` —— 输出向量有几维。**必须和实际产出的向量宽度一致**，
       因为建表时要靠它写出 ``fixed_size_list<float32>[ndims]`` 的列类型；
       写小了会截断，写大了会补 0，两种都是静默的数据损坏。
    2. :meth:`compute_source_vectors` —— 编码**库里的那一列**（写入路径）。
    3. :meth:`compute_query_vectors` —— 编码**用户查的那段文本**（查询路径）。
       文本模态下两者完全一样（默认实现就是直接转发）；
       多模态下不一样：ColPali 用图片编码文档、用文字编码查询，
       这时两条路径必须分开实现，**这是这个接口最容易被忽略的价值**。

    类属性：

    * :attr:`SOURCE` —— 写进表 schema metadata 的**稳定标识**；
    * :attr:`name` —— 具体模型名（``"text-embedding-3-small"`` 之类）；
    * :attr:`max_retries` —— 带重试调用时的次数。
    """

    #: 注册表 key，也是写进 schema metadata 的名字。**改它 = 破坏旧表。**
    SOURCE: str = ""
    #: 具体模型名。同一个 SOURCE 下可以有很多 name（例如 openai 的三个模型）。
    name: str = ""
    #: ``compute_*_with_retry`` 默认重试次数。远端 API 用得上，本地函数用不上。
    max_retries: int = 7

    def __init__(self, **kwargs: Any) -> None:
        #: 构造时用户**原样**传进来的参数（可能含 ``$var:``）。
        #: ``create()`` 会覆盖它；直接 ``Cls(...)`` 时留空。
        self._original_args: Optional[Dict[str, Any]] = None
        self._init_kwargs: Dict[str, Any] = dict(kwargs)

    # -- 工厂 -------------------------------------------------------------
    @classmethod
    def create(cls, **kwargs: Any) -> "EmbeddingFunction":
        """**推荐的构造方式**（真实 LanceDB 也是这个 API）。

        和直接 ``Cls(**kwargs)`` 的区别有两条，都很重要：

        1. 会解析 ``$var:`` 变量，并把**原始参数**（含变量占位符）
           记在 ``_original_args`` 上，供 :meth:`safe_model_dump` 序列化 ——
           于是"写进表 metadata"的是 ``$var:OPENAI_API_KEY`` 而不是明文密钥；
        2. 敏感字段（:meth:`sensitive_keys`）如果传了不以 ``$var:`` 开头的
           字符串，直接报错。**密钥不进代码、不进 notebook、不进 git**
           这条规矩，靠文档是守不住的，只能靠接口拦。

        ``create()`` 是类方法而不是实例方法，因为它的输入是"配置"（dict），
        输出才是一个可用的向量化函数 —— 这正是从表 metadata 恢复的路径。
        """

        for key in cls.sensitive_keys():
            value = kwargs.get(key)
            if isinstance(value, str) and value and not value.startswith("$var:"):
                raise QueryError(
                    f"敏感参数 {key!r} 不允许硬编码",
                    hint=(
                        f"改成 '$var:{key.upper()}'，再用 "
                        f"get_registry().set_var({key.upper()!r}, ...) 在运行时注入"
                    ),
                )
        instance = cls(**resolve_variables(kwargs))
        instance._original_args = dict(kwargs)
        return instance

    # -- 子类必须实现 -----------------------------------------------------
    @abc.abstractmethod
    def ndims(self) -> int:
        """输出向量的维度。"""

    @abc.abstractmethod
    def compute_source_vectors(
        self, texts: Any, *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        """编码"源列"（写入路径）。返回长度 = 输入条数，允许元素为 ``None``
        （真实系统里"这条文本无效、跳过"是常见需求，用 None 显式表达）。"""

    # -- 默认实现 ---------------------------------------------------------
    def compute_query_vectors(
        self, queries: Any, *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        """编码用户查询（查询路径）。**文本模态下和源列完全一样**，
        所以默认转发；多模态必须重写。"""

        return self.compute_source_vectors(queries, *args, **kwargs)

    # -- 带重试的版本 -----------------------------------------------------
    def compute_source_vectors_with_retry(self, *args: Any, **kwargs: Any) -> List[Any]:
        return retry_with_exponential_backoff(
            self.compute_source_vectors, max_retries=self.max_retries
        )(*args, **kwargs)

    def compute_query_vectors_with_retry(self, *args: Any, **kwargs: Any) -> List[Any]:
        return retry_with_exponential_backoff(
            self.compute_query_vectors, max_retries=self.max_retries
        )(*args, **kwargs)

    # -- 便利方法 ---------------------------------------------------------
    def sanitize_input(self, texts: TEXT) -> List[str]:
        """把 ``str`` / ``list[str]`` / numpy 数组 / Arrow 数组统一成 ``list[str]``。

        特别处理**单个字符串**：``compute("hello")`` 如果被当成可迭代对象
        就会变成 ``["h", "e", "l", "l", "o"]`` —— 这是 embedding 库里
        最常见的一类 bug（而且不报错，只是向量全错）。
        """

        if isinstance(texts, str):
            return [texts]
        if isinstance(texts, np.ndarray):
            return [str(t) for t in texts.ravel().tolist()]
        to_pylist = getattr(texts, "to_pylist", None)
        if callable(to_pylist):
            return [str(t) for t in to_pylist()]
        if isinstance(texts, (list, tuple)):
            return [str(t) for t in texts]
        raise QueryError(
            "无法把输入规整成文本列表",
            got=type(texts).__name__,
            hint="支持 str / list[str] / np.ndarray / pa.Array",
        )

    def embedding_matrix(self, texts: TEXT) -> np.ndarray:
        """编码并堆成 ``(n, ndims)`` 的矩阵 —— 写列、算相似度都直接用这个。"""

        vectors = self.compute_source_vectors(texts)
        dim = self.ndims()
        out = np.zeros((len(vectors), dim), dtype=np.float32)
        for i, vec in enumerate(vectors):
            if vec is None:
                continue
            arr = np.asarray(vec, dtype=np.float32).ravel()
            if arr.size != dim:
                raise QueryError(
                    "向量宽度和 ndims() 不一致",
                    expected=dim,
                    got=int(arr.size),
                    function=type(self).__name__,
                )
            out[i] = arr
        return out

    def query_vector(self, text: str) -> np.ndarray:
        """编码单个查询，返回一维数组。"""

        vectors = self.compute_query_vectors(text)
        if len(vectors) != 1:
            raise QueryError("单个查询必须返回恰好一个向量", got=len(vectors))
        vec = vectors[0]
        if vec is None:
            raise QueryError("查询向量为 None", text=text)
        return np.asarray(vec, dtype=np.float32).ravel()

    def _model_config(self) -> Dict[str, Any]:
        """返回**能重建这个实例**的构造参数。

        每个子类都要重写。这是"把模型写进表 metadata"的全部依据：
        ``create(**config)`` 必须能还原出等价的对象。
        """

        return dict(self._init_kwargs)

    def safe_model_dump(self) -> Dict[str, Any]:
        """序列化用的参数字典。

        优先返回用户**原样**传入的参数（``create()`` 时记下来的），
        因为它们可能含 ``$var:api_key`` 这类占位符 ——
        把 ``$var`` 解析后的明文密钥写进表 metadata 是把密钥泄到磁盘上，
        真实 LanceDB 专门为此设计了 ``$var:`` 机制。
        """

        if self._original_args is not None:
            return dict(self._original_args)
        return self._model_config()

    @staticmethod
    def sensitive_keys() -> List[str]:
        """不允许硬编码、必须走 ``$var:`` 的参数名（例如 ``api_key``）。"""

        return []

    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.SOURCE,
            "name": self.name,
            "ndims": self.ndims(),
            "class": type(self).__name__,
        }

    # -- 相等性 -----------------------------------------------------------
    # 定义 __eq__/__hash__ 是为了"同一份配置只注册一次 / 建一次索引"这类判断。
    # 不用 __dict__ 直接比较，因为它含 _cache 这类运行时状态：
    # 编码过一批文本之后 __eq__ 就变了，那是 bug 不是特性。
    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        return self._model_config() == other._model_config()  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self).__name__, tuple(sorted(map(str, self._model_config().items())))))

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(name={self.name!r}, ndims={self.ndims()})"


class TextEmbeddingFunction(EmbeddingFunction):
    """文本模态的便捷基类：子类只需要实现 :meth:`generate_vectors`。"""

    def compute_source_vectors(
        self, texts: Any, *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        return self.generate_vectors(self.sanitize_input(texts), *args, **kwargs)

    @abc.abstractmethod
    def generate_vectors(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[Optional[np.ndarray]]:
        """编码一批已经规整好的字符串。"""


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------


@dataclass
class EmbeddingFunctionConfig:
    """"哪一列用什么函数编码"的完整描述。

    三个字段就是写进 schema metadata 的那三项。表可以有多组配置
    （多模态表常见：图片列和标题列用不同函数），所以它是一个列表。
    """

    vector_column: str
    source_column: str
    function: EmbeddingFunction

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.function.SOURCE or type(self.function).__name__,
            "model": self.function.safe_model_dump(),
            "source_column": self.source_column,
            "vector_column": self.vector_column,
        }


@dataclass
class VectorQuery:
    """"一段文本 + 一个 embedding 函数" = 一次待执行的向量查询。

    它存在的意义是把 :meth:`EmbeddingFunction.compute_query_vectors`
    **和它要查的那一列**绑在一起：调用方 ``table.search("机器 学习")``
    时并不知道要查哪一列、维度是多少，这些信息应该由表本身记着。
    """

    text: str
    function: EmbeddingFunction
    column: str = "vector"
    k: int = 10
    filter: Optional[str] = None

    def vectors(self) -> np.ndarray:
        """执行编码，返回 ``(1, ndims)`` 的查询矩阵（ANN 接口的形状）。"""

        vec = self.function.query_vector(self.text)
        expected = self.function.ndims()
        if vec.size != expected:
            raise QueryError(
                "查询向量宽度和 ndims() 不一致",
                expected=expected,
                got=int(vec.size),
            )
        return vec[None, :]

    def run(self, search_fn: Callable[[str, np.ndarray, int], Any]) -> Any:
        """用调用方提供的检索函数执行这次查询。

        ``search_fn(column, vectors, k)`` 的形状是照着
        ``mini_lancedb.index`` 那套接口定的（向量索引的 ``search``
        收 ``(q, d)`` 的矩阵、返回 ``SearchResult``）。
        """

        return search_fn(self.column, self.vectors(), self.k)

    def describe(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "column": self.column,
            "k": self.k,
            "ndims": self.function.ndims(),
            "function": self.function.SOURCE or type(self.function).__name__,
        }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class EmbeddingFunctionRegistry:
    """向量化函数的注册表（单例）。

    **key 是 :attr:`EmbeddingFunction.SOURCE`，也就是写进表 schema metadata
    的那个字符串。** 所以：

    * 名字一旦发布就不能改 —— 改了等于让所有老表读不出自己的函数；
    * 同一个名字只能注册一次（重复注册直接报错，而不是静默覆盖：
      静默覆盖意味着"老表按新实现解码"，是数据损坏级别的问题）。

    ``reset()`` 会把注册表恢复到**初始状态**（内置函数还在，用户后注册的
    清掉）。真实 LanceDB 的 ``reset()`` 是清空全部，因为它的内置函数也是
    "导入时才注册"的；我们这里显式区分了"内置"和"用户"，原因是测试需要
    一个可重复的干净起点，而把内置函数一起清掉会让 ``reset()`` 之后
    连 ``get_registry().get("hashing")`` 都拿不到 —— 那是纯粹的 footgun。
    """

    def __init__(self) -> None:
        self._functions: Dict[str, Type[EmbeddingFunction]] = {}
        #: 内置函数（``reset()`` 时要恢复的那批）
        self._builtin: Dict[str, Type[EmbeddingFunction]] = {}
        self._variables: Dict[str, str] = {}

    # -- 单例 -------------------------------------------------------------
    @classmethod
    def get_instance(cls) -> "EmbeddingFunctionRegistry":
        return _REGISTRY

    # -- 注册 -------------------------------------------------------------
    def register(self, alias: Optional[str] = None, *, builtin: bool = False):
        """返回一个类装饰器，把 ``EmbeddingFunction`` 子类注册进表。

        ``alias`` 省略时用类名。``builtin=True`` 表示"这是本库自带的"，
        :meth:`reset` 会保留它。
        """

        def decorator(cls: Type[EmbeddingFunction]) -> Type[EmbeddingFunction]:
            if not (isinstance(cls, type) and issubclass(cls, EmbeddingFunction)):
                raise TypeError("只能注册 EmbeddingFunction 的子类")
            key = alias or cls.__name__
            if key in self._functions:
                raise KeyError(
                    f"注册名 {key!r} 已经被 {self._functions[key].__name__} 占用"
                    "（注册名是写进表 metadata 的，不能静默覆盖）"
                )
            self._functions[key] = cls
            if builtin:
                self._builtin[key] = cls
            # SOURCE 就是注册名：一个字符串，同时是"注册表的 key"和
            # "表 metadata 里的 name"
            cls.SOURCE = key
            cls.__embedding_function_registry_alias__ = alias  # type: ignore[attr-defined]
            return cls

        return decorator

    def get(self, name: str) -> Type[EmbeddingFunction]:
        """按注册名取类。取不到抛 ``KeyError``（和真实 LanceDB 一致）。"""

        if name not in self._functions:
            raise KeyError(
                f"未注册的向量化函数 {name!r}；已注册: {sorted(self._functions)}"
            )
        return self._functions[name]

    def get_function(self, name: str, **kwargs: Any) -> EmbeddingFunction:
        """按注册名直接建实例（``get(name).create(**kwargs)`` 的简写）。"""

        return self.get(name).create(**kwargs)

    def functions(self) -> Dict[str, Type[EmbeddingFunction]]:
        """当前注册表的快照。"""

        return dict(self._functions)

    def names(self) -> List[str]:
        return sorted(self._functions)

    def reset(self) -> None:
        """清掉用户注册的函数，保留内置的。"""

        self._functions = dict(self._builtin)
        self._variables.clear()

    # -- 变量（``$var:`` 机制）--------------------------------------------
    def set_var(self, name: str, value: str) -> None:
        if ":" in name:
            raise QueryError("变量名里不能有冒号", name=name)
        self._variables[name] = str(value)

    def get_var(self, name: str) -> str:
        return self._variables[name]

    def has_var(self, name: str) -> bool:
        return name in self._variables

    # -- 表 metadata 的序列化 ---------------------------------------------
    def function_to_metadata(self, conf: EmbeddingFunctionConfig) -> Dict[str, Any]:
        return conf.to_dict()

    def get_table_metadata(
        self, configs: Optional[Iterable[EmbeddingFunctionConfig]]
    ) -> Optional[Dict[str, bytes]]:
        """把配置列表打包成"可以直接塞进 ``pa.Schema`` metadata"的字典。

        返回值里的 ``bytes`` 是 pyarrow 的硬要求（``schema.metadata`` 是
        ``dict[bytes, bytes]``），忘了编码是最常见的报错来源。
        """

        if configs is None:
            return None
        items = list(configs)
        if not items:
            return None
        payload = [self.function_to_metadata(conf) for conf in items]
        return {
            "embedding_functions": json.dumps(payload, ensure_ascii=False).encode("utf-8")
        }

    def parse_functions(
        self, metadata: Optional[Mapping[Any, Any]]
    ) -> Dict[str, EmbeddingFunctionConfig]:
        """从表 metadata 里还原出"向量列 → 配置"。

        **这就是"打开一张老表时，它怎么记得自己用的是什么模型"的答案。**
        读出来的 ``name`` 拿去 ``self.get(name).create(**model)``，
        于是解码器被重建出来。名字找不到（函数被删了 / 换库了）会抛
        ``KeyError``，因为这时候继续跑下去只会得到错误的向量。
        """

        if not metadata:
            return {}
        raw = metadata.get("embedding_functions") or metadata.get(b"embedding_functions")
        if raw is None:
            return {}
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        entries = json.loads(raw)
        out: Dict[str, EmbeddingFunctionConfig] = {}
        for entry in entries:
            cls = self.get(entry["name"])
            out[entry["vector_column"]] = EmbeddingFunctionConfig(
                vector_column=entry["vector_column"],
                source_column=entry["source_column"],
                function=cls.create(**entry.get("model") or {}),
            )
        return out


#: 全局单例。放在模块底部但**在函数之前**定义，因为下面的工厂要用到它。
_REGISTRY = EmbeddingFunctionRegistry()


def get_registry() -> EmbeddingFunctionRegistry:
    """取全局注册表。

    Examples
    --------
    >>> registry = get_registry()
    >>> fn = registry.get_function("hashing", dim=128)
    >>> fn.ndims()
    128
    """

    return _REGISTRY


def register_embedding_function(
    alias: Optional[str] = None, *, builtin: bool = False
) -> Callable[[Type[EmbeddingFunction]], Type[EmbeddingFunction]]:
    """模块级的注册装饰器，等价于 ``get_registry().register(alias, builtin=...)``。

    Examples
    --------
    >>> @register_embedding_function("my-model")
    ... class MyEmbedding(TextEmbeddingFunction):
    ...     def ndims(self): return 8
    ...     def generate_vectors(self, texts): return [np.zeros(8)] * len(texts)
    """

    return get_registry().register(alias, builtin=builtin)


def resolve_variables(args: Mapping[str, Any]) -> Dict[str, Any]:
    """把参数里的 ``$var:name`` / ``$var:name:default`` 换成注册表里的变量。

    这是真实 LanceDB 的机制，解决的问题很具体：**配置要能写进文件
    （或 Jupyter notebook、或版本控制），但密钥不能。**

    * ``"$var:OPENAI_API_KEY"`` —— 变量没设就报错；
    * ``"$var:OPENAI_API_KEY:sk-placeholder"`` —— 变量没设就用默认值，
      适合"不影响正确性的配置"（例如 ``device``）；
    * 敏感字段（:meth:`EmbeddingFunction.sensitive_keys`）**不允许**写死明文，
      必须走 ``$var:``。
    """

    out: Dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and value.startswith("$var:"):
            parts = value[5:].split(":", maxsplit=1)
            name = parts[0]
            default = parts[1] if len(parts) > 1 else None
            if _REGISTRY.has_var(name):
                out[key] = _REGISTRY.get_var(name)
            elif default is not None:
                out[key] = default
            else:
                raise QueryError(
                    f"变量 {name!r} 未设置",
                    key=key,
                    hint="用 get_registry().set_var(name, value) 设置；变量不跨进程保留",
                )
        else:
            out[key] = value
    return out
