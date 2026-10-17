"""全文检索（分词管线 + 倒排索引 + BM25）的测试。

覆盖三类事情：

1. **Porter 词干化的正确性** —— 拿官方测试集里的成对样本对拍，
   再加上"对已经词干化的词幂等"这条元性质；
2. **分词器每一个开关都要真的改变词项流**（否则那个开关就是装饰品），
   包括位置空档、UTF-8 字节偏移、CJK 二元组这些容易做错的地方；
3. **BM25 的数值和语义** —— 手工推导的小语料分数（1e-6 精度）、
   IDF 单调性、k1/b 的敏感方向、短语查询的位置语义，以及
   ``_score`` **降序**这条最容易和向量检索搞反的约定。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from mini_lancedb.arrays import StringArray
from mini_lancedb.errors import NotSupportedError, QueryError, SchemaError
from mini_lancedb.fts.tokenizer import (
    ENGLISH_STOP_WORDS,
    AnalysisTrace,
    PorterStemmer,
    Tokenizer,
    TokenizerConfig,
    analyze,
    fold_ascii,
    is_cjk,
    stem_word,
    tokenize,
)
from mini_lancedb.index.base import (
    And,
    DictIndexStore,
    ExactMatch,
    IndexMetadata,
    IsIn,
    Or,
    RangeQuery,
)
from mini_lancedb.index.inverted import (
    BooleanQuery,
    InvertedIndex,
    MatchQuery,
    PhraseQuery,
    TermQuery,
    parse_query,
    varint_decode,
    varint_encode,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_index(
    texts,
    *,
    config: TokenizerConfig | None = None,
    k1: float = 1.2,
    b: float = 0.75,
    metadata: IndexMetadata | None = None,
) -> InvertedIndex:
    if isinstance(texts, str):
        texts = [texts]
    md = metadata or IndexMetadata(name="fts", index_type="FTS", columns=["text"])
    md.params["k1"] = k1
    md.params["b"] = b
    if config is not None:
        md.params["tokenizer"] = config.to_dict()
    return InvertedIndex.build(
        StringArray.from_strings(list(texts)), np.arange(len(texts)), metadata=md
    )


def texts_of(tokens) -> list:
    return [t.text for t in tokens]


def positions_of(tokens) -> list:
    return [t.position for t in tokens]


# ---------------------------------------------------------------------------
# 1. Porter 词干化
# ---------------------------------------------------------------------------

#: 官方 Porter 算法测试集（``voc.txt`` / ``output.txt``）里的代表样本。
PORTER_PAIRS = [
    ("running", "run"),
    ("dogs", "dog"),
    ("easily", "easili"),
    ("relational", "relat"),
    ("conditional", "condit"),
    ("ponies", "poni"),
    ("caresses", "caress"),
    ("cats", "cat"),
    ("agreed", "agre"),
    ("feed", "feed"),
    ("plastered", "plaster"),
    ("motoring", "motor"),
    ("happy", "happi"),
    ("sky", "sky"),
    ("hopping", "hop"),
    ("hoping", "hope"),
    ("controlling", "control"),
    ("generalizations", "gener"),
    ("rational", "ration"),
    ("national", "nation"),
    ("triplicate", "triplic"),
    ("formative", "form"),
    ("sensibility", "sensibl"),
    ("electrical", "electr"),
    ("callousness", "callous"),
]


def test_porter_stemmer_verified_pairs():
    """经典 Porter 算法的成对样本（25 组）。"""

    stemmer = PorterStemmer()
    bad = [
        (word, stemmer.stem(word), expected)
        for word, expected in PORTER_PAIRS
        if stemmer.stem(word) != expected
    ]
    assert bad == [], f"词干化不符合经典 Porter 输出: {bad}"


#: 经典 Porter **不是**幂等函数：这些词干再跑一遍还会继续缩。
#: 原因不是 bug，而是 step4 / step5 的删除只以 ``m``（词干里 VC 序列的个数）
#: 为准 —— 某个词干完全可能"第一轮 m 还不够、第二轮刚好越过阈值"。
#: 官方 Porter 实现（Martin Porter 的 C 版）行为完全相同。
PORTER_SECOND_ROUND = {
    "agre": "agr",
    "callous": "callou",
    "decis": "deci",
    "defens": "defen",
    "ceas": "cea",
}


def test_porter_stemmer_is_idempotent_on_stemmed_forms():
    """对**已经**是词干的词再跑一遍必须不动 —— 除了下面几个已知反例。"""

    stemmer = PorterStemmer()
    stems = sorted({expected for _, expected in PORTER_PAIRS})
    unstable = {
        s: stemmer.stem(s)
        for s in stems
        if s not in PORTER_SECOND_ROUND and stemmer.stem(s) != s
    }
    assert unstable == {}, f"词干化不幂等: {unstable}"
    # 反例本身也要钉住，免得将来"修好"了却没人知道
    for word, expected in PORTER_SECOND_ROUND.items():
        assert stemmer.stem(word) == expected, word


def test_porter_converges_to_a_fixed_point_within_a_few_rounds():
    """不幂等没关系，但**必须收敛** —— 迭代到不动点用不了几轮。

    这条性质比"幂等"更弱、却更真实：只要它成立，"索引时多算一轮"就不会
    发散，词干化仍然是可用的规范化手段。
    """

    for _, stem in PORTER_PAIRS:
        cur = stem
        for _ in range(5):
            nxt = stem_word(cur)
            if nxt == cur:
                break
            cur = nxt
        assert stem_word(cur) == cur, stem


def test_porter_stemmer_leaves_one_and_two_letter_words_alone():
    """长度 <= 2 的词一律不动（经典实现的门槛，防止 base/baseball 互串）。"""

    assert stem_word("a") == "a"
    assert stem_word("be") == "be"
    assert stem_word("is") == "is"
    assert stem_word("") == ""


def test_porter_stemmer_ignores_non_ascii():
    """非 ASCII（含 CJK、带变音符的词）原样返回，不做任何后缀匹配。"""

    assert stem_word("中文") == "中文"
    assert stem_word("café") == "café"
    assert stem_word("123") == "123"


def test_porter_happy_vs_hoping_symmetry():
    """``hopping`` / ``hoping`` 必须落到不同词干，否则会把"跳"和"希望"混起来。"""

    assert stem_word("hopping") == "hop"
    assert stem_word("hoping") == "hope"
    assert stem_word("hopping") != stem_word("hoping")


# ---------------------------------------------------------------------------
# 2. 分词器配置
# ---------------------------------------------------------------------------


def test_default_config_matches_lancedb_option_names_and_defaults():
    cfg = TokenizerConfig()
    assert cfg.base_tokenizer == "simple"
    assert cfg.lower_case is True
    assert cfg.stem is True
    assert cfg.remove_stop_words is True
    assert cfg.ascii_folding is False
    assert cfg.max_token_length == 40
    assert cfg.with_position is True
    assert cfg.ngram_min_length == 3
    assert cfg.ngram_max_length == 3
    assert cfg.language == "English"
    assert cfg.stemmer_language == "English"
    assert cfg.custom_stop_words is None


def test_config_validation_rejects_bad_values():
    with pytest.raises(SchemaError):
        TokenizerConfig(base_tokenizer="bogus")
    with pytest.raises(SchemaError):
        TokenizerConfig(max_token_length=0)
    with pytest.raises(SchemaError):
        TokenizerConfig(ngram_min_length=5, ngram_max_length=3)
    with pytest.raises(NotSupportedError):
        TokenizerConfig(stemmer_language="German")
    # 不词干化时语言字段是惰性的，不该因此报错
    assert TokenizerConfig(stemmer_language="German", stem=False).stem is False


def test_lower_case_flag_changes_stream():
    # 关掉词干化和停用词，把 lower_case 的效果单独隔离出来
    base = dict(stem=False, remove_stop_words=False)
    text = "The RUNNING Dogs"
    upper = texts_of(Tokenizer(TokenizerConfig(lower_case=False, **base)).tokenize(text))
    lower = texts_of(Tokenizer(TokenizerConfig(lower_case=True, **base)).tokenize(text))
    assert upper == ["The", "RUNNING", "Dogs"]
    assert lower == ["the", "running", "dogs"]


def test_stem_flag_changes_stream():
    text = "running dogs"
    with_stem = texts_of(Tokenizer(TokenizerConfig(stem=True)).tokenize(text))
    without = texts_of(Tokenizer(TokenizerConfig(stem=False)).tokenize(text))
    assert with_stem == ["run", "dog"]
    assert without == ["running", "dogs"]


def test_remove_stop_words_flag_changes_stream():
    text = "the quick brown fox"
    with_stop = texts_of(Tokenizer(TokenizerConfig(remove_stop_words=True)).tokenize(text))
    without = texts_of(Tokenizer(TokenizerConfig(remove_stop_words=False)).tokenize(text))
    assert with_stop == ["quick", "brown", "fox"]
    assert without == ["the", "quick", "brown", "fox"]


def test_custom_stop_words_replace_builtin_table():
    """``custom_stop_words`` 是**取代**而不是追加 —— 这是文档里写死的语义。"""

    cfg = TokenizerConfig(custom_stop_words=["frobnicate"])
    tok = Tokenizer(cfg)
    assert texts_of(tok.tokenize("the frobnicate fox")) == ["the", "fox"]
    assert "frobnicate" in cfg.stop_words
    assert "the" not in cfg.stop_words


def test_stop_word_table_is_a_decent_english_list():
    assert len(ENGLISH_STOP_WORDS) >= 150
    for w in ("the", "and", "of", "is", "not", "you", "they"):
        assert w in ENGLISH_STOP_WORDS
    # 停用词表必须全是小写，否则和 lower_case 的交互会出错
    assert all(w == w.lower() for w in ENGLISH_STOP_WORDS)


def test_ascii_folding_flag_and_byte_offsets():
    cfg = TokenizerConfig(ascii_folding=True, stem=False)
    plain = Tokenizer(TokenizerConfig(ascii_folding=False, stem=False))

    assert texts_of(plain.tokenize("Café")) == ["café"]
    tok = Tokenizer(cfg)
    folded = tok.tokenize("Café")
    assert texts_of(folded) == ["cafe"]
    # 折叠改变了字符数，但偏移必须仍然指向**原文**
    assert folded[0].start == 0
    assert folded[0].end == len("Café".encode("utf-8")) == 5


def test_ascii_folding_handles_one_to_many_expansion():
    """``ß`` 一个字符折叠成 ``ss`` 两个字符 —— 偏移映射必须扛得住。"""

    tok = Tokenizer(TokenizerConfig(ascii_folding=True, stem=False, lower_case=True))
    got = tok.tokenize("straße")
    assert texts_of(got) == ["strasse"]
    assert got[0].start == 0
    assert got[0].end == len("straße".encode("utf-8")) == 7


def test_fold_ascii_helper_special_cases():
    assert fold_ascii("Café") == "Cafe"
    assert fold_ascii("naïve") == "naive"
    assert fold_ascii("strasse") == "strasse"
    assert fold_ascii("Ærø") == "AEro"


def test_max_token_length_drops_long_tokens():
    """超长词项是**丢弃**（tantivy 的 RemoveLongFilter 语义），不是截断。"""

    long_word = "a" * 50
    cfg = TokenizerConfig(max_token_length=40, stem=False, remove_stop_words=False)
    tok = Tokenizer(cfg)
    assert texts_of(tok.tokenize(f"ok {long_word} end")) == ["ok", "end"]
    # 边界：正好等于上界必须留下
    edge = "b" * 40
    assert texts_of(tok.tokenize(edge)) == [edge]


def test_with_position_false_yields_none_positions():
    text = "one two three"
    with_pos = Tokenizer(TokenizerConfig(with_position=True, stem=False)).tokenize(text)
    without = Tokenizer(TokenizerConfig(with_position=False, stem=False)).tokenize(text)
    assert positions_of(with_pos) == [0, 1, 2]
    assert positions_of(without) == [None, None, None]


def test_position_increments_leave_gaps_for_removed_tokens():
    """被停用词吃掉的词项要在位置上**留空档**，这是短语查询判对的前提。"""

    tok = Tokenizer(TokenizerConfig(stem=False, remove_stop_words=True))
    got = tok.tokenize("the quick brown")
    assert texts_of(got) == ["quick", "brown"]
    assert positions_of(got) == [1, 2]

    # 关掉停用词过滤之后位置回到 0,1,2 —— 位置定义在"基础词项流"上
    tok2 = Tokenizer(TokenizerConfig(stem=False, remove_stop_words=False))
    assert positions_of(tok2.tokenize("the quick brown")) == [0, 1, 2]


def test_byte_offsets_are_utf8_not_char_indices():
    text = "héllo wörld"  # 每个带音符的字母占 2 字节
    got = Tokenizer(TokenizerConfig(stem=False)).tokenize(text)
    assert got[0].text == "héllo"
    assert got[0].start == 0
    assert got[0].end == len("héllo".encode("utf-8")) == 6
    assert got[1].start == 7  # "héllo " 共 7 字节
    assert text.encode("utf-8")[got[0].start : got[0].end].decode("utf-8") == "héllo"


def test_cjk_uses_character_bigrams():
    tok = Tokenizer(TokenizerConfig(stem=False, lower_case=False))
    got = tok.tokenize("中国人民银行")
    assert texts_of(got) == ["中国", "国人", "人民", "民银", "银行"]
    assert positions_of(got) == [0, 1, 2, 3, 4]
    assert all(t.is_cjk for t in got)
    # 偏移仍然是原文的字节坐标（每个汉字 3 字节）
    assert (got[0].start, got[0].end) == (0, 6)
    assert (got[1].start, got[1].end) == (3, 9)


def test_cjk_single_char_run_emits_the_char():
    tok = Tokenizer(TokenizerConfig(stem=False, lower_case=False, remove_stop_words=False))
    assert texts_of(tok.tokenize("中")) == ["中"]
    assert texts_of(tok.tokenize("a中b")) == ["a", "中", "b"]


def test_cjk_punctuation_does_not_join_bigram_runs():
    """全角逗号不是 CJK 段的一部分，否则会造出 ``好，`` 这种噪声二元组。"""

    tok = Tokenizer(TokenizerConfig(stem=False, lower_case=False))
    assert texts_of(tok.tokenize("你好，世界")) == ["你好", "世界"]


def test_is_cjk_range_check():
    assert is_cjk("中") and is_cjk("あ") and is_cjk("한")
    assert not is_cjk("a") and not is_cjk("，") and not is_cjk("")


def test_whitespace_tokenizer_keeps_punctuation():
    tok = Tokenizer(
        TokenizerConfig(base_tokenizer="whitespace", stem=False, remove_stop_words=False)
    )
    assert texts_of(tok.tokenize("hello, world!")) == ["hello,", "world!"]
    simple = Tokenizer(
        TokenizerConfig(base_tokenizer="simple", stem=False, remove_stop_words=False)
    )
    assert texts_of(simple.tokenize("hello, world!")) == ["hello", "world"]


def test_ngram_tokenizer_emits_all_sizes():
    tok = Tokenizer(
        TokenizerConfig(
            base_tokenizer="ngram",
            lower_case=False,
            stem=False,
            remove_stop_words=False,
            ngram_min_length=2,
            ngram_max_length=3,
        )
    )
    assert texts_of(tok.tokenize("abc")) == ["ab", "abc", "bc"]


def test_raw_tokenizer_and_max_length_interaction():
    """``raw`` + ``max_token_length`` 是个真实的坑：长文本会被整个丢掉。"""

    cfg = TokenizerConfig(base_tokenizer="raw", stem=False, remove_stop_words=False)
    assert texts_of(Tokenizer(cfg).tokenize("hello world")) == ["hello world"]

    short = TokenizerConfig(
        base_tokenizer="raw", stem=False, remove_stop_words=False, max_token_length=5
    )
    assert texts_of(Tokenizer(short).tokenize("hello world")) == []


def test_analyze_exposes_every_stage():
    trace = analyze("The RUNNING dogs")
    assert isinstance(trace, AnalysisTrace)
    assert trace.base == ["The", "RUNNING", "dogs"]
    assert trace.lowered == ["the", "running", "dogs"]
    assert trace.no_stop_words == ["running", "dogs"]
    assert trace.stemmed == ["run", "dog"]
    assert trace.final == ["run", "dog"]
    assert trace.counts()["base"] == 3 and trace.counts()["final"] == 2
    # 打印出来必须是人能读的
    assert "去停用词" in trace.render()


def test_tokenize_query_matches_index_side_by_default():
    """默认情况下查询侧和索引侧切出来的词项必须一致（位置也一样）。"""

    cfg = TokenizerConfig()
    tok = Tokenizer(cfg)
    text = "The running dogs are here"
    index_side = [(t.text, t.position) for t in tok.tokenize(text)]
    query_side = [(t.text, t.position) for t in tok.tokenize_query(text)]
    assert index_side == query_side


def test_tokenize_query_can_keep_stop_words():
    tok = Tokenizer(TokenizerConfig(stem=False))
    assert texts_of(tok.tokenize_query("the quick fox")) == ["quick", "fox"]
    assert texts_of(tok.tokenize_query("the quick fox", keep_stop_words=True)) == [
        "the",
        "quick",
        "fox",
    ]


def test_module_level_tokenize_helper():
    assert texts_of(tokenize("running dogs")) == ["run", "dog"]
    assert tokenize("") == []


# ---------------------------------------------------------------------------
# 3. varint
# ---------------------------------------------------------------------------


def test_varint_roundtrip_and_size():
    values = [0, 1, 127, 128, 300, 16383, 16384, 2**31]
    buf = varint_encode(values)
    assert varint_decode(bytes(buf), 0, len(buf)) == values
    # 小于 128 的值只占一个字节 —— 这正是位置差分能压小的原因
    assert len(varint_encode([0, 1, 127])) == 3
    assert len(varint_encode([128])) == 2


def test_varint_rejects_negative():
    with pytest.raises(ValueError):
        varint_encode([-1])


# ---------------------------------------------------------------------------
# 4. BM25 数值
# ---------------------------------------------------------------------------

#: 手工推导用的小语料（3 篇文档，不含停用词、不做词干化，好算）：
#:   d0 = "apple banana banana"   |d0|=3
#:   d1 = "apple cherry"          |d1|=2
#:   d2 = "banana"                |d2|=1
#: N=3, avgdl=2, k1=1.2, b=0.75
BM25_DOCS = ["apple banana banana", "apple cherry", "banana"]
BM25_CONFIG = TokenizerConfig(lower_case=True, stem=False, remove_stop_words=False)


def _approx(x, rel=1e-6):
    return pytest.approx(x, rel=1e-9, abs=rel)


def test_bm25_hand_computed_score_on_tiny_corpus():
    """逐项手算的 BM25 分数（见下方推导），要求 1e-6 内一致。

    公式（Lucene / tantivy 用的那一版）：:

        idf(t)  = ln(1 + (N - df + 0.5) / (df + 0.5))
        score   = Σ_t idf(t) · tf·(k1+1) / (tf + k1·(1 - b + b·|d|/avgdl))

    代入 N=3, avgdl=2, k1=1.2, b=0.75：

      df(apple)=2, df(banana)=2, df(cherry)=1
      idf(apple) = idf(banana) = ln(1 + 1.5/2.5) = ln(1.6)   = 0.47000362924573563
      idf(cherry)=              ln(1 + 2.5/1.5) = ln(8/3)   = 0.98082925301172630

      d0: |d|=3 → norm = 0.25 + 0.75·(3/2) = 1.375；分母 = tf + 1.65
          apple  tf=1 → 2.2/2.65   = 0.8301886792452831 → ×idf = 0.39019169220400696
          banana tf=2 → 4.4/3.65   = 1.2054794520547945 → ×idf = 0.56657971744691430
          合计 = 0.95677140965092125
      d1: |d|=2 → norm = 1.0；分母 = tf + 1.2
          apple  tf=1 → 2.2/2.2    = 1.0                → ×idf = 0.47000362924573563
          合计 = 0.47000362924573563
      d2: |d|=1 → norm = 0.25 + 0.75·0.5 = 0.625；分母 = 1 + 0.75 = 1.75
          banana tf=1 → 2.2/1.75   = 1.2571428571428571 → ×idf = 0.59086170533749631
          合计 = 0.59086170533749631
    """

    idx = make_index(BM25_DOCS, config=BM25_CONFIG)
    rows, scores = idx.search_ranked("apple banana")
    got = {int(r): float(s) for r, s in zip(rows, scores)}

    assert got == {
        0: _approx(0.95677140965092125),
        1: _approx(0.47000362924573563),
        2: _approx(0.59086170533749631),
    }
    # 也单独校验 idf 自身，防止"公式对但系数错"被容忍
    assert idx.idf(2) == _approx(math.log(1.6))
    assert idx.idf(1) == _approx(math.log(8.0 / 3.0))


def test_bm25_scores_are_descending_not_ascending():
    """``_score`` **越大越好** —— 和向量检索的 ``_distance`` 方向相反。"""

    idx = make_index(BM25_DOCS, config=BM25_CONFIG)
    _, scores = idx.search_ranked("apple banana")
    assert list(scores) == sorted(scores, reverse=True)
    # 距离是升序、分数是降序：这里用一个显式的反例钉住方向
    rows, _ = idx.search_ranked("apple banana")
    assert int(rows[0]) == 0  # 两词都命中的文档排第一


def test_idf_is_monotonically_decreasing_in_document_frequency():
    """越稀有的词 IDF 越高（这就是"停用词没有区分度"的量化形式）。"""

    idx = make_index(BM25_DOCS, config=BM25_CONFIG)
    assert idx.idf(1) > idx.idf(2) > idx.idf(3)
    # Lucene 的 ln(1 + ...) 形式保证 IDF 恒正 —— 经典 RSJ 公式在这里会变负
    assert idx.idf(3) > 0
    assert idx.idf(idx.num_docs) > 0


def test_rare_term_outscores_document_frequency_ordering():
    idx = make_index(BM25_DOCS, config=BM25_CONFIG)
    _, cherry = idx.score_term("cherry")
    _, apple = idx.score_term("apple")
    # "cherry" 只出现在 1 篇文档里，即使 tf 相同也应该更"值钱"
    assert float(cherry[0]) > float(apple[0])
    # 同一个词在短文档里的相对贡献更高（b>0 的长度归一化）
    assert idx.idf(1) > idx.idf(2)


def test_k1_controls_term_frequency_saturation():
    """``k1 → 0`` 时词频完全不起作用；``k1`` 大时才有差别。"""

    docs = ["apple", " ".join(["apple"] * 10)]
    # b=0 排除长度归一化的干扰，只看 tf 的效果
    near_zero = make_index(docs, config=BM25_CONFIG, k1=1e-9, b=0.0)
    normal = make_index(docs, config=BM25_CONFIG, k1=1.2, b=0.0)

    rows, s0 = near_zero.search_ranked("apple")
    got0 = {int(r): float(s) for r, s in zip(rows, s0)}
    assert got0[0] == pytest.approx(got0[1], rel=1e-6)

    rows, s1 = normal.search_ranked("apple")
    got1 = {int(r): float(s) for r, s in zip(rows, s1)}
    assert got1[1] > got1[0]
    # 词频从 1 涨到 10，分数**没有**涨到 10 倍 —— 这就是饱和
    assert got1[1] < 10 * got1[0]


def test_b_controls_length_normalization():
    """``b=0`` 完全不看长度；``b=0.75`` 会给长文档打折。"""

    docs = ["apple", "apple " + " ".join(["zzz"] * 20)]
    flat = make_index(docs, config=BM25_CONFIG, b=0.0)
    normal = make_index(docs, config=BM25_CONFIG, b=0.75)

    rows, s = flat.search_ranked("apple")
    got = {int(r): float(v) for r, v in zip(rows, s)}
    assert got[0] == pytest.approx(got[1], rel=1e-6)

    rows, s = normal.search_ranked("apple")
    got = {int(r): float(v) for r, v in zip(rows, s)}
    assert got[0] > got[1], "长文档在 b>0 时必须被扣分"


def test_avg_doc_length_and_doc_lengths():
    idx = make_index(BM25_DOCS, config=BM25_CONFIG)
    assert [int(x) for x in idx.doc_lengths.tolist()] == [3, 2, 1]
    assert idx.avg_doc_length == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 5. 查询解析
# ---------------------------------------------------------------------------


def test_parser_plain_terms_default_to_should():
    q = parse_query("quick brown")
    assert isinstance(q, BooleanQuery)
    assert q.must == [] and q.must_not == []
    assert [type(n) for n in q.should] == [TermQuery, TermQuery]


def test_parser_quoted_phrase_becomes_phrase_query():
    q = parse_query('"quick brown"')
    assert len(q.should) == 1
    node = q.should[0]
    assert isinstance(node, PhraseQuery)
    assert node.terms == ["quick", "brown"]
    assert node.offsets == [0, 1]


def test_parser_plus_minus_occurrences():
    q = parse_query("+quick -turtle")
    assert [n.term for n in q.must] == ["quick"]
    assert [n.term for n in q.must_not] == ["turtl"]
    assert q.should == []


def test_parser_and_or_not_keywords():
    q = parse_query("quick AND fox")
    assert sorted(n.term for n in q.must) == ["fox", "quick"]

    q = parse_query("quick OR turtle")
    assert sorted(n.term for n in q.should) == ["quick", "turtl"]

    q = parse_query("NOT turtle")
    assert [n.term for n in q.must_not] == ["turtl"]


def test_parser_adjacency_is_implicit_or():
    q = parse_query("quick fox")
    assert len(q.should) == 2 and q.must == []


def test_parser_parentheses_group_with_occurrence_marker():
    q = parse_query("+(quick OR turtle)")
    assert len(q.must) == 1
    inner = q.must[0]
    assert isinstance(inner, BooleanQuery)
    assert sorted(n.term for n in inner.should) == ["quick", "turtl"]


def test_parser_stop_words_in_query_are_dropped():
    q = parse_query("the fox")
    assert [n.term for n in q.should] == ["fox"]


def test_parser_hyphenated_word_is_one_term():
    q = parse_query("state-of-art")
    assert len(q.should) == 1


def test_parser_empty_query_matches_nothing():
    q = parse_query("")
    assert q.is_empty


# ---------------------------------------------------------------------------
# 6. 倒排索引：短语、布尔、接口
# ---------------------------------------------------------------------------

PHRASE_DOCS = [
    "the quick brown fox",
    "quick brown dogs",
    "slow green turtle",
    "quick the brown fox",
]


def test_phrase_query_respects_position_offsets():
    """``"quick brown"`` 必须排除 ``quick the brown`` —— 靠的就是位置空档。"""

    idx = make_index(PHRASE_DOCS)
    rows, _ = idx.search_ranked('"quick brown"')
    assert sorted(int(r) for r in rows) == [0, 1]
    assert 3 not in set(int(r) for r in rows)


def test_phrase_query_with_explicit_stop_word_matches_only_that_document():
    idx = make_index(PHRASE_DOCS)
    rows, _ = idx.search_ranked('"quick the brown"')
    assert sorted(int(r) for r in rows) == [3]


def test_phrase_query_with_unknown_term_matches_nothing():
    idx = make_index(PHRASE_DOCS)
    rows, _ = idx.search_ranked('"quick zzzzz brown"')
    assert len(rows) == 0


def test_phrase_scores_sum_member_terms():
    idx = make_index(PHRASE_DOCS)
    rows, scores = idx.search_ranked('"quick brown"')
    assert len(rows) == 2
    assert float(scores[0]) > 0
    assert float(scores[0]) == pytest.approx(float(scores[1]), rel=1e-9)


def test_cjk_phrase_query_uses_bigram_adjacency():
    """中文二元组 + 短语查询 = 精确的多字词匹配。"""

    docs = ["中国人民银行发布报告", "上海银行也发布了报告", "中国人民很高兴"]
    idx = make_index(docs)

    rows, _ = idx.search_ranked("中国人民银行")
    assert sorted(int(r) for r in rows) == [0]

    rows, _ = idx.search_ranked("上海银行")
    assert sorted(int(r) for r in rows) == [1]

    # 不用短语、只用 OR 时会连 "中国人民" 也捞进来 —— 这正是必须用短语的原因
    rows, _ = idx.search_ranked("中国 人民 银行")
    assert 2 in set(int(r) for r in rows)


def test_boolean_must_and_must_not():
    idx = make_index(PHRASE_DOCS)
    rows, _ = idx.search_ranked("+quick -turtle")
    assert sorted(int(r) for r in rows) == [0, 1, 3]
    rows, _ = idx.search_ranked("quick -turtle")
    assert 2 not in set(int(r) for r in rows)


def test_boolean_and_requires_all_terms():
    idx = make_index(PHRASE_DOCS)
    rows, _ = idx.search_ranked("quick AND turtle")
    assert len(rows) == 0
    rows, _ = idx.search_ranked("quick AND fox")
    assert sorted(int(r) for r in rows) == [0, 3]


def test_boolean_or_unions_and_ranks():
    idx = make_index(PHRASE_DOCS)
    rows, scores = idx.search_ranked("quick OR turtle")
    assert sorted(int(r) for r in rows) == [0, 1, 2, 3]
    # 只命中文档 2 的 turtle 分数应该高于只命中 quick 的文档
    by_row = {int(r): float(s) for r, s in zip(rows, scores)}
    assert by_row[2] > by_row[1]


def test_pure_negative_query_is_universe_minus_must_not():
    idx = make_index(PHRASE_DOCS)
    rows, _ = idx.search_ranked("NOT turtle")
    assert sorted(int(r) for r in rows) == [0, 1, 3]


def test_should_clauses_become_optional_when_must_exists():
    """``+quick brown``：must 命中就行，brown 只是加分项。"""

    idx = make_index(PHRASE_DOCS)
    rows, _ = idx.search_ranked("+quick brown")
    assert 2 not in set(int(r) for r in rows)  # 不含 quick
    assert sorted(set(int(r) for r in rows)) == [0, 1, 3]


def test_mask_parameter_filters_results():
    idx = make_index(PHRASE_DOCS)
    mask = np.ones(4, dtype=bool)
    mask[0] = False
    rows, _ = idx.search_ranked("quick", mask=mask)
    assert sorted(int(r) for r in rows) == [1, 3]
    with pytest.raises(QueryError):
        idx.search_ranked("quick", mask=np.ones(3, dtype=bool))


def test_search_returns_exact_match_for_scalar_interface():
    idx = make_index(PHRASE_DOCS)
    expr = idx.search(MatchQuery(query="turtle"))
    assert isinstance(expr, ExactMatch)
    assert sorted(int(r) for r in expr.row_ids) == [2]


def test_search_composes_match_queries_with_boolean_expressions():
    """``ScalarIndexExpr`` 的意义就在这：多个索引的答案能直接做交并补。"""

    idx = make_index(PHRASE_DOCS)
    got = idx.search(And([MatchQuery(query="quick"), MatchQuery(query="fox")]))
    assert sorted(int(r) for r in got.row_ids) == [0, 3]

    got = idx.search(Or([MatchQuery(query="turtle"), MatchQuery(query="slow")]))
    assert sorted(int(r) for r in got.row_ids) == [2]

    # 一颗答不了的叶子会让整棵树诚实地返回 None（而不是给半截答案）
    assert idx.search(RangeQuery(0, 1)) is None
    assert idx.search(And([MatchQuery(query="quick"), IsIn([0, 1])])) is None


def test_search_ranked_limit_k():
    idx = make_index(PHRASE_DOCS)
    rows, scores = idx.search_ranked("quick", k=2)
    assert len(rows) == 2 and len(scores) == 2


def test_ties_are_broken_by_row_id_for_reproducibility():
    docs = ["alpha", "alpha", "alpha"]
    idx = make_index(docs, config=TokenizerConfig(stem=False, remove_stop_words=False))
    rows, _ = idx.search_ranked("alpha")
    assert [int(r) for r in rows] == [0, 1, 2]


def test_null_and_empty_documents_are_skipped():
    docs = ["apple", None, "", "   ", "banana"]
    idx = make_index(docs, config=BM25_CONFIG)
    assert idx.num_docs == 5
    assert [int(x) for x in idx.doc_lengths.tolist()] == [1, 0, 0, 0, 1]
    rows, _ = idx.search_ranked("apple")
    assert [int(r) for r in rows] == [0]
    rows, _ = idx.search_ranked("banana")
    assert [int(r) for r in rows] == [4]


def test_with_position_false_disables_phrase_queries():
    idx = make_index(
        PHRASE_DOCS,
        config=TokenizerConfig(with_position=False),
    )
    assert idx.has_positions is False
    # 位置没有被记录 → 短语查询无法给出精确答案，必须显式报错而不是给错答案
    with pytest.raises(QueryError):
        idx.search_ranked('"quick brown"')
    # 单词项查询不受影响
    rows, _ = idx.search_ranked("turtle")
    assert sorted(int(r) for r in rows) == [2]
    # 没有位置也让索引更小
    with_pos = make_index(PHRASE_DOCS)
    assert idx.size_bytes < with_pos.size_bytes


def test_positions_are_stored_delta_encoded():
    idx = make_index(["apple apple apple apple apple"], config=BM25_CONFIG)
    assert idx.positions_of("apple", 0) == [0, 1, 2, 3, 4]
    # 位置升序 → 差分全是 1 → 每个位置只占一个字节（5 个位置 = 5 字节）
    assert len(idx.pos_data) == 5
    # 换成"位置跨度大"的文档，差分值不再是小整数，varint 就得花两个字节
    wide = make_index(["apple " + " ".join(["zzz"] * 200) + " apple"], config=BM25_CONFIG)
    assert wide.positions_of("apple", 0) == [0, 201]


# ---------------------------------------------------------------------------
# 7. 观测与持久化
# ---------------------------------------------------------------------------


def test_stats_reports_all_required_fields():
    idx = make_index(PHRASE_DOCS)
    s = idx.stats()
    for key in (
        "num_docs",
        "num_unique_terms",
        "avg_doc_length",
        "num_postings",
        "size_bytes",
    ):
        assert key in s, key
    assert s["num_docs"] == 4
    assert s["num_postings"] == idx.num_postings
    assert s["num_unique_terms"] == idx.num_terms
    assert s["size_bytes"] > 0
    assert s["has_positions"] is True
    assert s["k1"] == 1.2 and s["b"] == 0.75


def test_stats_num_postings_equals_sum_of_doc_frequencies():
    idx = make_index(PHRASE_DOCS)
    assert int(idx.doc_freqs.sum()) == idx.num_postings


def test_save_load_roundtrip_preserves_results_and_scores():
    idx = make_index(PHRASE_DOCS)
    store = DictIndexStore()
    files = idx.save(store)
    assert "terms.json" in files and "positions.bin" in files

    loaded = InvertedIndex.load(store, idx.metadata)
    assert loaded.num_docs == idx.num_docs
    assert loaded.term_list == idx.term_list
    assert loaded.avg_doc_length == pytest.approx(idx.avg_doc_length)
    for query in ("quick", '"quick brown"', "+quick -turtle", "quick OR turtle"):
        r1, s1 = idx.search_ranked(query)
        r2, s2 = loaded.search_ranked(query)
        assert [int(x) for x in r1] == [int(x) for x in r2], query
        assert np.allclose(s1, s2), query


def test_loaded_index_keeps_tokenizer_config():
    cfg = TokenizerConfig(stem=False, remove_stop_words=False, ascii_folding=True)
    idx = make_index(["Cafe"], config=cfg)
    store = DictIndexStore()
    idx.save(store)
    loaded = InvertedIndex.load(store, idx.metadata)
    assert loaded.tokenizer.config.ascii_folding is True
    assert loaded.tokenizer.config.stem is False
    assert loaded.tokenizer.config.remove_stop_words is False


def test_explain_breaks_score_into_terms():
    idx = make_index(BM25_DOCS, config=BM25_CONFIG)
    info = idx.explain("apple banana", 0)
    assert info["doc_length"] == 3
    parts = {p["term"]: p for p in info["parts"]}
    assert parts["apple"]["tf"] == 1
    assert parts["banana"]["tf"] == 2
    assert info["total_score"] == pytest.approx(
        parts["apple"]["score"] + parts["banana"]["score"]
    )
    # df 越大 IDF 越小，逐项拆开必须能看出来
    assert parts["apple"]["df"] == 2


def test_last_search_stats_records_hit_count():
    idx = make_index(PHRASE_DOCS)
    idx.search_ranked("turtle")
    assert idx.last_search_stats["num_hits"] == 1
    idx.search_ranked("nothinghere")
    assert idx.last_search_stats["num_hits"] == 0


def test_index_metadata_is_self_describing():
    idx = make_index(PHRASE_DOCS)
    assert idx.metadata.index_type == "FTS"
    assert "tokenizer" in idx.metadata.params
    assert idx.metadata.params["tokenizer"]["base_tokenizer"] == "simple"
    assert idx.metadata.num_indexed_rows == 4
    assert idx.type_name == "FTS"


def test_empty_column_builds_and_queries_cleanly():
    idx = make_index([])
    assert idx.num_docs == 0
    assert idx.num_terms == 0
    rows, scores = idx.search_ranked("anything")
    assert len(rows) == 0 and len(scores) == 0
    assert idx.stats()["num_docs"] == 0


def test_all_null_column_builds_and_queries_cleanly():
    idx = make_index([None, None, None])
    assert idx.num_docs == 3
    assert idx.num_terms == 0
    rows, _ = idx.search_ranked("anything")
    assert len(rows) == 0


def test_doc_frequency_ordering_sanity():
    docs = ["a", "a b", "a b c", "a b c d"]
    idx = make_index(
        docs, config=TokenizerConfig(stem=False, remove_stop_words=False)
    )
    df = {t: int(idx.doc_freqs[i]) for i, t in enumerate(idx.term_list)}
    assert df["a"] == 4 and df["b"] == 3 and df["c"] == 2 and df["d"] == 1
