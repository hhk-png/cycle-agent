"""``mini_lancedb.embedding`` 的测试。

重点在四件事：

1. **确定性** —— 包括**跨进程**（Python 对 ``str`` 的哈希带随机盐，
   用内置 ``hash()`` 会让索引和查询在两个进程里对不上）；
2. **向量宽度 / 方向 / 归一化** 这些"错了也不报错"的静默契约；
3. **注册表的往返** —— 注册名就是写进表 schema metadata 的那个字符串，
   所以它必须能被 ``create(**model)`` 还原；
4. **provider 桩的导入安全性** —— 没有 SDK、没有 key、没有网络也必须能 import。
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from mini_lancedb.errors import NotSupportedError, QueryError
from mini_lancedb.embedding import (
    BOWEmbeddingFunction,
    CharNgramEmbeddingFunction,
    CohereEmbeddings,
    DeterministicFakeEmbedding,
    EmbeddingFunctionConfig,
    GeminiEmbeddings,
    HashingEmbeddingFunction,
    HuggingFaceEmbeddings,
    JinaEmbeddings,
    OllamaEmbeddings,
    OpenAIEmbeddings,
    PROVIDERS,
    SentenceTransformerEmbeddings,
    TextEmbeddingFunction,
    VectorQuery,
    builtin_function_names,
    describe_providers,
    format_providers_table,
    get_registry,
    register_embedding_function,
    retry_call,
    tokenize,
    with_retry,
)

ROOT = Path(__file__).resolve().parents[1]

#: 四个内置函数。``bow`` 的词表故意包含中英文，方便复用同一批文本。
VOCAB = ["machine", "learning", "models", "cat", "dog", "neural", "network", "训练", "数据"]

FACTORIES = {
    "hashing": lambda: HashingEmbeddingFunction(dim=64),
    "char-ngram": lambda: CharNgramEmbeddingFunction(dim=64),
    "fake": lambda: DeterministicFakeEmbedding(dim=64),
    "bow": lambda: BOWEmbeddingFunction(VOCAB),
}

#: 包含全部词表词的一条文本，保证每个函数都产出非零向量。
RICH_TEXT = "machine learning models neural network cat dog 训练 数据"

#: 8 组"查询 / 词面重叠 / 不相关"。后 4 组的不相关文档**共享一部分常用词**，
#: 所以"相关 > 不相关"不是"非零 vs 零"这种白送的断言。
RELATED_PAIRS = [
    ("machine learning models",
     "machine learning models are trained on large datasets",
     "the models in this catalogue are cheap"),
    ("vector database index",
     "a vector database index speeds up similarity search",
     "the index of this book lists every chapter"),
    ("python programming language",
     "python is a popular programming language for data science",
     "language is a tool for communication between people"),
    ("rust memory safety",
     "rust guarantees memory safety without a garbage collector",
     "safety first is the slogan of the factory"),
    ("clickhouse column store",
     "clickhouse is a column store database for analytics",
     "my favorite movie is inception"),
    ("检索 增强 生成",
     "检索 增强 生成 让 大 模型 回答 更 准确",
     "生成 一 张 图片 需要 多少 时间"),
    ("神经网络 训练",
     "神经网络 训练 需要 大量 标注 数据",
     "今天 天气 真 好 适合 出去 散步"),
    ("股票 市场 波动",
     "股票 市场 波动 加剧 投资者 担忧",
     "the quick brown fox jumps over the lazy dog"),
]


def cosine(a, b):
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_registry_is_a_singleton():
    assert get_registry() is get_registry()


def test_builtin_functions_are_registered_on_import():
    names = get_registry().names()
    for expected in builtin_function_names():
        assert expected in names
    for expected in PROVIDERS:
        assert expected in names


def test_registry_register_get_create_roundtrip():
    registry = get_registry()
    registry.reset()

    @register_embedding_function("test-roundtrip")
    class RoundTrip(TextEmbeddingFunction):
        def __init__(self, dim: int = 4):
            super().__init__(dim=dim)
            self.dim = dim

        def _model_config(self):
            return {"dim": self.dim}

        def ndims(self):
            return self.dim

        def generate_vectors(self, texts):
            return [np.ones(self.dim, dtype=np.float32) for _ in texts]

    try:
        cls = registry.get("test-roundtrip")
        assert cls is RoundTrip
        assert RoundTrip.SOURCE == "test-roundtrip", "注册名同时是写进表 metadata 的 SOURCE"

        fn = cls.create(dim=16)
        assert fn.ndims() == 16
        assert fn.safe_model_dump() == {"dim": 16}
        assert fn.compute_source_vectors("x")[0].shape == (16,)
    finally:
        registry.reset()


def test_registry_rejects_duplicate_name():
    with pytest.raises(KeyError):
        register_embedding_function("hashing")(HashingEmbeddingFunction)


def test_registry_rejects_non_embedding_class():
    with pytest.raises(TypeError):
        register_embedding_function("not-a-function")(dict)


def test_registry_get_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get_registry().get("no-such-function")


def test_registry_reset_keeps_builtins():
    registry = get_registry()

    @register_embedding_function("test-to-be-reset")
    class Temporary(TextEmbeddingFunction):
        def ndims(self):
            return 1

        def generate_vectors(self, texts):
            return [np.zeros(1, dtype=np.float32) for _ in texts]

    assert "test-to-be-reset" in registry.names()
    registry.reset()
    assert "test-to-be-reset" not in registry.names()
    assert "hashing" in registry.names(), "reset() 不该把内置函数也清掉"


def test_registry_variables():
    registry = get_registry()
    registry.reset()
    registry.set_var("MY_KEY", "secret-value")
    assert registry.has_var("MY_KEY")
    assert registry.get_var("MY_KEY") == "secret-value"
    with pytest.raises(QueryError):
        registry.set_var("bad:name", "x")
    with pytest.raises(KeyError):
        registry.get_var("missing")
    registry.reset()
    assert not registry.has_var("MY_KEY")


# ---------------------------------------------------------------------------
# create() / 变量 / 敏感字段
# ---------------------------------------------------------------------------


def test_create_records_original_args_not_resolved_values():
    registry = get_registry()
    registry.reset()
    registry.set_var("DEMO_KEY", "sk-live-123")

    fn = OpenAIEmbeddings.create(name="text-embedding-3-small", api_key="$var:DEMO_KEY")
    assert fn.api_key == "sk-live-123", "运行时用的是解析后的值"
    assert fn.safe_model_dump()["api_key"] == "$var:DEMO_KEY", (
        "但是写进表的必须是占位符 —— 明文密钥进 metadata 就等于进了磁盘和 git"
    )
    registry.reset()


def test_create_rejects_hardcoded_sensitive_key():
    with pytest.raises(QueryError) as info:
        OpenAIEmbeddings.create(api_key="sk-hardcoded")
    assert "敏感参数" in str(info.value)
    assert "$var:" in str(info.value)


def test_create_reports_missing_variable():
    get_registry().reset()
    with pytest.raises(QueryError) as info:
        OpenAIEmbeddings.create(api_key="$var:NOT_SET_ANYWHERE")
    assert "NOT_SET_ANYWHERE" in str(info.value)


def test_create_supports_variable_defaults():
    get_registry().reset()
    fn = SentenceTransformerEmbeddings.create(device="$var:DEVICE:cpu")
    assert fn.device == "cpu"


# ---------------------------------------------------------------------------
# 表 metadata 的往返（"表怎么记住自己用了哪个模型"）
# ---------------------------------------------------------------------------


def test_table_metadata_roundtrip():
    registry = get_registry()
    registry.reset()
    conf = EmbeddingFunctionConfig(
        vector_column="vector",
        source_column="text",
        function=HashingEmbeddingFunction.create(dim=32),
    )
    meta = registry.get_table_metadata([conf])

    assert meta is not None
    payload = meta["embedding_functions"]
    assert isinstance(payload, bytes), "pyarrow 的 schema.metadata 只收 bytes"

    parsed = registry.parse_functions(meta)
    assert set(parsed) == {"vector"}
    restored = parsed["vector"]
    assert restored.source_column == "text"
    assert isinstance(restored.function, HashingEmbeddingFunction)
    assert restored.function.ndims() == 32


def test_table_metadata_accepts_str_keys():
    registry = get_registry()
    meta = registry.get_table_metadata(
        [EmbeddingFunctionConfig("v", "t", HashingEmbeddingFunction.create(dim=8))]
    )
    as_str = {"embedding_functions": meta["embedding_functions"].decode("utf-8")}
    assert registry.parse_functions(as_str)["v"].function.ndims() == 8


def test_table_metadata_none_and_empty():
    registry = get_registry()
    assert registry.get_table_metadata(None) is None
    assert registry.get_table_metadata([]) is None
    assert registry.parse_functions(None) == {}
    assert registry.parse_functions({}) == {}


def test_parse_functions_unknown_name_raises():
    registry = get_registry()
    with pytest.raises(KeyError):
        registry.parse_functions(
            {"embedding_functions": b'[{"name": "ghost", "model": {}, '
                                      b'"source_column": "t", "vector_column": "v"}]'}
        )


def test_function_config_to_dict():
    conf = EmbeddingFunctionConfig("vec", "text", HashingEmbeddingFunction(dim=16))
    d = conf.to_dict()
    assert d["name"] == "hashing"
    assert d["vector_column"] == "vec"
    assert d["source_column"] == "text"
    assert d["model"]["dim"] == 16


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def test_retry_succeeds_after_failures():
    calls = []

    @with_retry(max_retries=3, base_delay=0.0)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3


def test_retry_reraises_after_exhausting():
    calls = []

    @with_retry(max_retries=2, base_delay=0.0)
    def always_fails():
        calls.append(1)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        always_fails()
    assert len(calls) == 3, "1 次原始调用 + 2 次重试"


def test_retry_zero_disables_retrying():
    calls = []

    @with_retry(max_retries=0, base_delay=0.0)
    def fails():
        calls.append(1)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        fails()
    assert len(calls) == 1


def test_retry_backoff_schedule_is_exponential_and_capped():
    waits = []

    @with_retry(max_retries=4, base_delay=1.0, max_delay=4.0, sleep=waits.append)
    def fails():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        fails()
    # 1, 2, 4, 4（封顶）
    assert waits == [1.0, 2.0, 4.0, 4.0]


def test_retry_call_helper():
    state = {"n": 0}

    def once_then_ok():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first")
        return 42

    assert retry_call(once_then_ok, max_retries=2) == 42


def test_embedding_with_retry_methods_exist():
    fn = HashingEmbeddingFunction(dim=8)
    fn.max_retries = 0
    assert len(fn.compute_source_vectors_with_retry("hello")) == 1
    assert len(fn.compute_query_vectors_with_retry("hello")) == 1


# ---------------------------------------------------------------------------
# 每个内置函数都必须满足的硬契约
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_ndims_matches_produced_vector_width(name):
    fn = FACTORIES[name]()
    vectors = fn.compute_source_vectors(RICH_TEXT)
    assert len(vectors) == 1
    assert np.asarray(vectors[0]).shape == (fn.ndims(),)
    assert fn.embedding_matrix([RICH_TEXT, RICH_TEXT]).shape == (2, fn.ndims())


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_cosine_similarity_with_itself_is_one(name):
    fn = FACTORIES[name]()
    vec = fn.embedding_matrix([RICH_TEXT])[0]
    assert cosine(vec, vec) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_same_text_gives_same_vector(name):
    fn = FACTORIES[name]()
    a = fn.embedding_matrix([RICH_TEXT, "another text"])[0]
    b = fn.embedding_matrix([RICH_TEXT, "another text"])[0]
    assert a.tobytes() == b.tobytes()


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_query_path_delegates_to_source_path_for_text(name):
    fn = FACTORIES[name]()
    assert np.array_equal(
        np.asarray(fn.compute_query_vectors(RICH_TEXT)[0]),
        np.asarray(fn.compute_source_vectors(RICH_TEXT)[0]),
    )


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_serializable_by_registry_name(name):
    """序列化的核心不是 pickle，而是"注册名 + 参数能重建出等价的对象"。"""

    fn = FACTORIES[name]()
    registry = get_registry()
    conf = EmbeddingFunctionConfig("v", "t", fn)
    payload = registry.function_to_metadata(conf)
    rebuilt = registry.get(payload["name"]).create(**payload["model"])
    assert rebuilt.ndims() == fn.ndims()
    assert rebuilt.embedding_matrix([RICH_TEXT]).tobytes() == fn.embedding_matrix([RICH_TEXT]).tobytes()


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_picklable(name):
    fn = FACTORIES[name]()
    clone = pickle.loads(pickle.dumps(fn))
    assert clone.ndims() == fn.ndims()
    assert clone.embedding_matrix([RICH_TEXT]).tobytes() == fn.embedding_matrix([RICH_TEXT]).tobytes()


# ---------------------------------------------------------------------------
# 分词的公共语义
# ---------------------------------------------------------------------------


def test_tokenize_splits_cjk_per_character():
    """没有分词器时，"机器学习"必须被切成单字，否则和"机器学习算法"重叠为 0。"""

    assert tokenize("机器学习") == ["机", "器", "学", "习"]
    assert tokenize("Machine Learning") == ["machine", "learning"]


def test_tokenize_drops_punctuation_and_keeps_ascii_words():
    assert tokenize("hello, world!") == ["hello", "world"]
    assert tokenize("foo_bar baz2") == ["foo_bar", "baz2"]
    assert tokenize("") == []


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def test_hashing_ranks_related_texts_closer():
    """核心断言：8 组里词面重叠的文档必须全面胜出，且最小间隔 > 0.15。"""

    fn = HashingEmbeddingFunction(dim=128)
    matrix = fn.embedding_matrix([t for pair in RELATED_PAIRS for t in pair]).reshape(
        len(RELATED_PAIRS), 3, -1
    )
    margins = []
    for i, (query, related, unrelated) in enumerate(RELATED_PAIRS):
        good = cosine(matrix[i, 0], matrix[i, 1])
        bad = cosine(matrix[i, 0], matrix[i, 2])
        margins.append(good - bad)
        assert good > bad, f"{query!r}: 相关 {good:.4f} 没赢过不相关 {bad:.4f}"
    assert min(margins) > 0.15, f"最小间隔 {min(margins):.4f} 太脆"


def test_hashing_is_l2_normalized():
    fn = HashingEmbeddingFunction(dim=64)
    matrix = fn.embedding_matrix([RICH_TEXT, "short", "a longer piece of text here"])
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-6)


def test_hashing_empty_text_is_zero_vector():
    """空文本必须是**零向量**，不能归一化成任意单位向量 ——
    否则"空查询"会跟随机文档产生一个随机的相似度。"""

    fn = HashingEmbeddingFunction(dim=32)
    vec = fn.embedding_matrix([""])[0]
    assert float(np.linalg.norm(vec)) == 0.0


def test_hashing_sublinear_tf_suppresses_repetition():
    """重复 10 次不等于权重乘 10：``1 + log(10) ≈ 3.30``，而不是 10。

    文本 ``"cat"×10 + "dog"`` 里两个词的权重比：

    * 次线性：``(1+ln10) / ((1+ln10) + 1) = 3.3026 / 4.3026 ≈ 0.7676``
    * 线性：``10 / 11 ≈ 0.9091``

    L2 归一化是个公共因子，所以它同时出现在分子分母上，会被约掉 ——
    这就是为什么这个比值可以精确手算。
    """

    import math

    sublinear = HashingEmbeddingFunction(dim=64)
    linear = HashingEmbeddingFunction(dim=64, sublinear_tf=False)
    text = " ".join(["cat"] * 10 + ["dog"])

    ratio_sub = _token_weight_ratio(sublinear, text)
    ratio_lin = _token_weight_ratio(linear, text)
    assert ratio_sub == pytest.approx((1 + math.log(10)) / (2 + math.log(10)), abs=1e-6)
    assert ratio_lin == pytest.approx(10 / 11, abs=1e-6)
    assert ratio_sub < ratio_lin, "次线性必须让 dog 的份额变大"


def _token_weight_ratio(fn, text):
    """cat 的能量 / (cat + dog) 的能量之和 —— 不依赖哈希桶的具体位置。"""

    v = fn.embedding_matrix([text])[0]
    only_cat = fn.embedding_matrix(["cat"])[0]
    only_dog = fn.embedding_matrix(["dog"])[0]
    cat_energy = abs(float(np.dot(v, only_cat)))
    dog_energy = abs(float(np.dot(v, only_dog)))
    return cat_energy / (cat_energy + dog_energy)


def test_hashing_determinism_across_processes():
    """跨进程逐字节一致 —— 这条专门盯"别用内置 ``hash()``"。

    内置 ``hash(str)`` 带进程级随机盐（PYTHONHASHSEED），
    用它的后果是：写索引的进程和查询的进程把同一个词映射到不同的桶，
    检索结果静默变错，而且**本地测试永远发现不了**（都在一个进程里）。
    """

    code = (
        "import sys, numpy as np;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from mini_lancedb.embedding import HashingEmbeddingFunction, CharNgramEmbeddingFunction;"
        "h = HashingEmbeddingFunction(dim=64);"
        "c = CharNgramEmbeddingFunction(dim=64);"
        "v = np.concatenate([h.embedding_matrix(['machine learning models', '神经网络 训练']),"
        "                    c.embedding_matrix(['machine learning models', '神经网络 训练'])]);"
        "sys.stdout.write(v.tobytes().hex())"
    )
    outputs = []
    for seed in ("0", "1"):
        # 显式钉住 PYTHONHASHSEED，让"换种子结果会不会变"变成一个可控实验
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
            check=True,
        )
        outputs.append(proc.stdout.strip())
    assert outputs[0] == outputs[1], "换一个 PYTHONHASHSEED 结果就变了 ⇒ 用了内置 hash()"

    local = np.concatenate(
        [
            HashingEmbeddingFunction(dim=64).embedding_matrix(["machine learning models", "神经网络 训练"]),
            CharNgramEmbeddingFunction(dim=64).embedding_matrix(["machine learning models", "神经网络 训练"]),
        ]
    )
    assert local.tobytes().hex() == outputs[0], "子进程和本进程必须完全一致"


def test_hashing_describe():
    info = HashingEmbeddingFunction(dim=32).describe()
    assert info["source"] == "hashing"
    assert info["ndims"] == 32


def test_hashing_rejects_bad_dim():
    with pytest.raises(QueryError):
        HashingEmbeddingFunction(dim=0)


# ---------------------------------------------------------------------------
# char-ngram
# ---------------------------------------------------------------------------


def test_char_ngram_ranks_cjk_texts_closer():
    """中文上的词面重叠：字符 n-gram 不依赖分词，所以效果立竿见影。"""

    fn = CharNgramEmbeddingFunction(dim=256)
    query = "机器学习 模型 训练"
    related = "机器学习 模型 训练 需要 数据"
    unrelated = "今天 天气 不错 适合 散步"
    matrix = fn.embedding_matrix([query, related, unrelated])
    assert cosine(matrix[0], matrix[1]) > cosine(matrix[0], matrix[2]) + 0.2


def test_char_ngram_respects_n():
    a = CharNgramEmbeddingFunction(dim=256, n=2)
    b = CharNgramEmbeddingFunction(dim=256, n=3)
    assert a.embedding_matrix(["hello world"]).tobytes() != b.embedding_matrix(["hello world"]).tobytes()


def test_char_ngram_is_l2_normalized_and_empty_is_zero():
    fn = CharNgramEmbeddingFunction(dim=128)
    assert np.linalg.norm(fn.embedding_matrix(["机器 学习"])[0]) == pytest.approx(1.0, abs=1e-6)
    assert float(np.linalg.norm(fn.embedding_matrix([""])[0])) == 0.0


def test_char_ngram_rejects_bad_args():
    with pytest.raises(QueryError):
        CharNgramEmbeddingFunction(dim=8, n=0)


# ---------------------------------------------------------------------------
# fake（只给性能测试用）
# ---------------------------------------------------------------------------


def test_fake_is_deterministic_and_seed_sensitive():
    a = DeterministicFakeEmbedding(dim=32, seed=0).embedding_matrix(["x"])
    b = DeterministicFakeEmbedding(dim=32, seed=0).embedding_matrix(["x"])
    c = DeterministicFakeEmbedding(dim=32, seed=1).embedding_matrix(["x"])
    assert a.tobytes() == b.tobytes()
    assert a.tobytes() != c.tobytes()


def test_fake_has_no_semantic_structure():
    """这是"不能用它评估召回率"的**可执行证据**：

    词面高度重叠的一对文本和完全不相干的一对，相似度都接近 0
    （不同文本的随机向量近似正交）。所以拿它跑出来的召回率曲线是纯噪声。
    """

    fn = DeterministicFakeEmbedding(dim=256)
    matrix = fn.embedding_matrix(
        ["machine learning models", "machine learning models are trained", "the cat sat on the mat"]
    )
    related = cosine(matrix[0], matrix[1])
    unrelated = cosine(matrix[0], matrix[2])
    assert abs(related) < 0.3 and abs(unrelated) < 0.3, "随机向量之间应该近似正交"
    assert abs(np.linalg.norm(matrix[0]) - 1.0) < 1e-6, "但必须归一化，否则距离度量会被尺度主导"


def test_fake_describe_warns_never_use_for_quality():
    fn = DeterministicFakeEmbedding(dim=8)
    assert fn.SOURCE == "fake"
    assert fn.ndims() == 8


# ---------------------------------------------------------------------------
# BOW（可手算）
# ---------------------------------------------------------------------------


def test_bow_exact_counts_are_hand_verifiable():
    fn = BOWEmbeddingFunction(["cat", "dog", "fish"], normalize="none")
    vec = fn.compute_source_vectors("cat cat dog")[0]
    assert vec.tolist() == [2.0, 1.0, 0.0]


def test_bow_l2_normalized_variant():
    fn = BOWEmbeddingFunction(["cat", "dog", "fish"])
    vec = fn.compute_source_vectors("cat cat dog")[0]
    expected = np.asarray([2.0, 1.0, 0.0]) / np.sqrt(5.0)
    assert np.allclose(vec, expected)


def test_bow_binary_mode():
    fn = BOWEmbeddingFunction(["cat", "dog"], binary=True, normalize="none")
    assert fn.compute_source_vectors("cat cat cat dog")[0].tolist() == [1.0, 1.0]


def test_bow_out_of_vocabulary_tokens_are_dropped():
    fn = BOWEmbeddingFunction(["cat"], normalize="none")
    assert fn.compute_source_vectors("cat whale elephant")[0].tolist() == [1.0]


def test_bow_row_width_equals_vocabulary_size():
    fn = BOWEmbeddingFunction(["a", "b", "c", "d"])
    assert fn.ndims() == 4
    assert fn.embedding_matrix(["a b"]).shape == (1, 4)


def test_bow_rejects_bad_vocabulary():
    with pytest.raises(QueryError):
        BOWEmbeddingFunction([])
    with pytest.raises(QueryError):
        BOWEmbeddingFunction(["cat", "cat"])
    with pytest.raises(QueryError):
        BOWEmbeddingFunction(["cat"], normalize="l1")


# ---------------------------------------------------------------------------
# sanitize_input / embedding_matrix 的护栏
# ---------------------------------------------------------------------------


def test_sanitize_input_handles_str_list_and_ndarray():
    fn = HashingEmbeddingFunction(dim=8)
    assert fn.sanitize_input("hello") == ["hello"], "单个字符串不能被拆成一个个字符"
    assert fn.sanitize_input(["a", "b"]) == ["a", "b"]
    assert fn.sanitize_input(np.asarray(["a", "b"])) == ["a", "b"]


def test_sanitize_input_handles_pyarrow_array():
    pa = pytest.importorskip("pyarrow")
    fn = HashingEmbeddingFunction(dim=8)
    assert fn.sanitize_input(pa.array(["a", "b"])) == ["a", "b"]


def test_sanitize_input_rejects_numbers():
    with pytest.raises(QueryError):
        HashingEmbeddingFunction(dim=8).sanitize_input(123)


def test_embedding_matrix_detects_width_mismatch():
    class Wrong(TextEmbeddingFunction):
        def ndims(self):
            return 4

        def generate_vectors(self, texts):
            return [np.zeros(7, dtype=np.float32) for _ in texts]

    with pytest.raises(QueryError) as info:
        Wrong().embedding_matrix(["x"])
    assert "ndims" in str(info.value)


def test_query_vector_returns_1d():
    vec = HashingEmbeddingFunction(dim=16).query_vector("hello")
    assert vec.shape == (16,)
    assert vec.dtype == np.float32


class _NoneReturning(TextEmbeddingFunction):
    def ndims(self):
        return 2

    def generate_vectors(self, texts):
        return [None for _ in texts]


def test_query_vector_rejects_none():
    with pytest.raises(QueryError):
        _NoneReturning().query_vector("x")


# ---------------------------------------------------------------------------
# VectorQuery
# ---------------------------------------------------------------------------


def test_vector_query_builds_a_query_matrix():
    fn = HashingEmbeddingFunction(dim=32)
    query = VectorQuery("machine learning", fn, column="vec", k=5)
    matrix = query.vectors()
    assert matrix.shape == (1, 32)
    assert np.array_equal(matrix[0], fn.query_vector("machine learning"))


def test_vector_query_run_delegates_to_search_fn():
    fn = HashingEmbeddingFunction(dim=16)
    seen = {}

    def search_fn(column, vectors, k):
        seen["column"], seen["shape"], seen["k"] = column, vectors.shape, k
        return ["row"]

    assert VectorQuery("hello", fn, column="vec", k=7).run(search_fn) == ["row"]
    assert seen == {"column": "vec", "shape": (1, 16), "k": 7}


def test_vector_query_describe():
    info = VectorQuery("hello", HashingEmbeddingFunction(dim=8)).describe()
    assert info["function"] == "hashing"
    assert info["ndims"] == 8
    assert info["k"] == 10


# ---------------------------------------------------------------------------
# provider 桩
# ---------------------------------------------------------------------------

PROVIDER_CASES = [
    (OpenAIEmbeddings, "openai"),
    (SentenceTransformerEmbeddings, "sentence-transformers"),
    (CohereEmbeddings, "cohere"),
    (JinaEmbeddings, "requests"),
    (OllamaEmbeddings, "ollama"),
    (HuggingFaceEmbeddings, "transformers"),
    (GeminiEmbeddings, "google-genai"),
]


def test_providers_dict_covers_every_stub():
    assert set(PROVIDERS) == {name for _, name in
                              [(OpenAIEmbeddings, "openai"),
                               (SentenceTransformerEmbeddings, "sentence-transformers"),
                               (CohereEmbeddings, "cohere"),
                               (JinaEmbeddings, "jina"),
                               (OllamaEmbeddings, "ollama"),
                               (HuggingFaceEmbeddings, "huggingface"),
                               (GeminiEmbeddings, "gemini")]}
    for name, cls in PROVIDERS.items():
        assert cls.SOURCE == name, f"{name} 的 SOURCE 必须等于注册名"


@pytest.mark.parametrize("cls,name", PROVIDER_CASES)
def test_provider_ndims_is_known_offline(cls, name):
    """**导入和 ndims() 都不需要依赖** —— 这正是"接口桩"的意义。"""

    fn = cls()
    assert isinstance(fn.ndims(), int) and fn.ndims() > 0


@pytest.mark.parametrize("cls,name", PROVIDER_CASES)
def test_provider_raises_not_supported_with_install_hint(cls, name):
    fn = cls()
    with pytest.raises(NotSupportedError) as info:
        fn.compute_source_vectors("hello")
    message = str(info.value)
    assert "pip install" in message
    assert name in message or "ollama" in message


@pytest.mark.parametrize("cls,name", PROVIDER_CASES)
def test_provider_query_path_also_raises_or_delegates(cls, name):
    fn = cls()
    with pytest.raises(NotSupportedError):
        fn.compute_query_vectors("hello")


def test_provider_stubs_are_implementations_of_the_abc():
    from mini_lancedb.embedding import EmbeddingFunction

    for cls in PROVIDERS.values():
        assert issubclass(cls, EmbeddingFunction)
        assert not getattr(cls, "__abstractmethods__", frozenset()), (
            f"{cls.__name__} 还有没实现的抽象方法，说明它不是一个可用的接口桩"
        )


def test_unknown_model_dim_raises_not_supported():
    with pytest.raises(NotSupportedError):
        OpenAIEmbeddings(name="gpt-9000-embedding").ndims()
    with pytest.raises(NotSupportedError):
        HuggingFaceEmbeddings(name="someone/unknown-model").ndims()


def test_provider_dim_override_wins():
    assert OpenAIEmbeddings(name="text-embedding-3-large", dim=512).ndims() == 512
    assert OpenAIEmbeddings().ndims() == 1536
    assert CohereEmbeddings().ndims() == 1024
    assert HuggingFaceEmbeddings().ndims() == 512


def test_describe_providers_table_shape():
    rows = describe_providers()
    assert len(rows) == len(PROVIDERS)
    for row in rows:
        assert set(row) == {
            "name", "package", "typical_model", "dims",
            "needs_api_key", "runs_locally", "note",
        }
        assert row["name"] in PROVIDERS
        assert row["dims"] > 0
    assert {r["name"] for r in rows} == set(PROVIDERS)


def test_format_providers_table_is_rendered_text():
    text = format_providers_table()
    lines = text.splitlines()
    assert len(lines) == len(PROVIDERS) + 2
    for row in describe_providers():
        assert row["name"] in text


def test_provider_metadata_roundtrip_keeps_the_placeholder():
    registry = get_registry()
    registry.reset()
    registry.set_var("DEMO_KEY", "sk-live")
    fn = OpenAIEmbeddings.create(name="text-embedding-3-small", api_key="$var:DEMO_KEY")
    meta = registry.get_table_metadata([EmbeddingFunctionConfig("vec", "text", fn)])
    assert b"sk-live" not in meta["embedding_functions"], "密钥不能落进 metadata"
    assert b"$var:DEMO_KEY" in meta["embedding_functions"]

    registry.set_var("DEMO_KEY", "sk-live")
    restored = registry.parse_functions(meta)["vec"].function
    assert restored.api_key == "sk-live"
    registry.reset()


# ---------------------------------------------------------------------------
# 相等性
# ---------------------------------------------------------------------------


def test_embedding_functions_compare_by_config():
    assert HashingEmbeddingFunction(dim=32) == HashingEmbeddingFunction(dim=32)
    assert HashingEmbeddingFunction(dim=32) != HashingEmbeddingFunction(dim=64)
    assert HashingEmbeddingFunction(dim=32) != CharNgramEmbeddingFunction(dim=32)
    assert len({HashingEmbeddingFunction(dim=32), HashingEmbeddingFunction(dim=32)}) == 1


def test_equality_is_not_polluted_by_runtime_cache():
    """编码过一批文本之后 ``__eq__`` 不能变 —— 缓存是运行时状态，不是配置。"""

    a = HashingEmbeddingFunction(dim=32)
    b = HashingEmbeddingFunction(dim=32)
    a.compute_source_vectors("machine learning")
    assert a == b
    assert hash(a) == hash(b)


def test_repr_mentions_ndims():
    assert "32" in repr(HashingEmbeddingFunction(dim=32))
