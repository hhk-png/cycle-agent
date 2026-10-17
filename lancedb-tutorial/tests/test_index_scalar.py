"""标量索引（BTREE / BITMAP / LABEL_LIST / NGRAM / ZONEMAP）的测试。

测试策略有一条贯穿始终的原则：**任何索引给出的行号都必须和暴力求值逐位相等**。
索引的价值是"更快地得到同一个答案"，所以"答案一样"是唯一不能妥协的性质。
凡是能从数据推出的期望值，都在测试里现算一遍（brute force），而不是抄一份
硬编码的结果 —— 硬编码的期望值会随着实现的 bug 一起被"修正"。

ZONEMAP 还多一条更强的性质：**绝不能把真正命中的块剪掉**。
它允许"该剪没剪"（只是慢），绝不允许"不该剪却剪了"（结果是错的）。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from mini_lancedb.arrays import array_from_pylist
from mini_lancedb.errors import IndexBuildError
from mini_lancedb.index.base import (
    And,
    DictIndexStore,
    ExactMatch,
    IndexMetadata,
    IsIn,
    Not,
    Or,
    RangeQuery,
)
from mini_lancedb.index.bitmap import BitmapIndex
from mini_lancedb.index.btree import BTreeIndex
from mini_lancedb.index.inverted import MatchQuery
from mini_lancedb.index.label_list import ArrayContains, LabelListIndex
from mini_lancedb.index.ngram import Contains, Like, NGramIndex
from mini_lancedb.index.zonemap import (
    AndMatcher,
    NullMatcher,
    PruningStats,
    RangeMatcher,
    SetMatcher,
    ZoneMapIndex,
    matcher_from_expr,
)
from mini_lancedb.schema import Field
from mini_lancedb.types import INT64, List as ListType, Struct as StructType, UTF8


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def meta(name: str, type_name: str, columns, **params) -> IndexMetadata:
    return IndexMetadata(
        name=name, index_type=type_name, columns=list(columns), params=dict(params)
    )


def int_array(values):
    return array_from_pylist(INT64, list(values))


def str_array(values):
    return array_from_pylist(UTF8, list(values))


def list_array(values, child=UTF8):
    return array_from_pylist(ListType(child), list(values))


def rows_of(expr, field="row_ids"):
    return sorted(int(x) for x in getattr(expr, field).tolist())


def brute(values, pred):
    return sorted(i for i, v in enumerate(values) if pred(v))


def cmp_op(a, b, inclusive, is_start):
    """区间比较的小工具，把"开/闭"两端写在一处，避免在测试里散落边界判断。"""

    if a is None or b is None:
        return False
    if is_start:
        return a >= b if inclusive else a > b
    return a <= b if inclusive else a < b


def in_range(v, start, end, inc_start, inc_end):
    if v is None:
        return False
    if start is not None and not cmp_op(v, start, inc_start, True):
        return False
    if end is not None and not cmp_op(v, end, inc_end, False):
        return False
    return True


# ---------------------------------------------------------------------------
# BTREE
# ---------------------------------------------------------------------------

BTREE_VALUES = [5, 3, 9, 1, 7, 3, 8, 2, 6, 4, 0, 9, 9]


def btree_index(values=None, **params):
    values = BTREE_VALUES if values is None else list(values)
    md = meta("bt", "BTREE", ["v"], **params)
    return BTreeIndex.build(int_array(values), np.arange(len(values)), metadata=md), values


def test_btree_range_matches_bruteforce():
    idx, values = btree_index()
    got = rows_of(idx.search(RangeQuery(3, 7)))
    assert got == brute(values, lambda v: 3 <= v <= 7)


@pytest.mark.parametrize(
    "start,end,inc_start,inc_end",
    [
        (0, 9, True, True),
        (3, 6, True, True),
        (3, 6, False, True),
        (3, 6, True, False),
        (3, 6, False, False),
        (9, 9, True, True),
        (4, 4, False, False),
        (None, 4, True, True),
        (5, None, True, True),
        (None, None, True, True),
    ],
)
def test_btree_range_boundary_matrix(start, end, inc_start, inc_end):
    idx, values = btree_index()
    got = rows_of(idx.search(RangeQuery(start, end, inc_start, inc_end)))
    assert got == brute(
        values, lambda v: in_range(v, start, end, inc_start, inc_end)
    )


def test_btree_equality_via_single_value_isin():
    idx, values = btree_index()
    for value in set(values):
        got = rows_of(idx.search(IsIn([value])))
        assert got == brute(values, lambda v: v == value), value


def test_btree_isin_multiple_values_is_a_union():
    idx, values = btree_index()
    wanted = [1, 9, 42]
    got = rows_of(idx.search(IsIn(wanted)))
    assert got == brute(values, lambda v: v in wanted)
    # 不存在的值不该带来任何行，也不该让结果变成"答不了"
    assert rows_of(idx.search(IsIn([1000]))) == []


def test_btree_handles_duplicate_keys():
    idx, values = btree_index()
    assert rows_of(idx.search(IsIn([9]))) == [2, 11, 12]
    assert rows_of(idx.search(IsIn([3]))) == [1, 5]


def test_btree_splits_and_stays_correct():
    """把叶节点容量压到 2、扇出压到 3，强制大量分裂，再全量对拍。"""

    values = list(range(40))
    random.Random(7).shuffle(values)
    idx, values = btree_index(values, max_leaf_size=2, max_children=3)
    assert idx.depth >= 3, "40 个值配 2 号叶节点应该长出好几层"
    assert idx.num_keys() == len(set(values))
    for start, end in [(0, 39), (5, 5), (10, 20), (30, 100), (-5, 3)]:
        got = rows_of(idx.search(RangeQuery(start, end)))
        assert got == brute(values, lambda v: start <= v <= end), (start, end)


def test_btree_leaf_chain_supports_full_scan():
    values = list(range(0, 100, 7))
    idx, values = btree_index(values, max_leaf_size=3)
    got = rows_of(idx.search(RangeQuery(None, None)))
    assert got == list(range(len(values)))
    lo, hi = idx.key_bounds()
    assert (lo, hi) == (min(values), max(values))


def test_btree_strings_sort_lexicographically():
    values = ["apple", "banana", "cherry", "apple", None, "date", ""]
    idx = BTreeIndex.build(
        str_array(values),
        np.arange(len(values)),
        metadata=meta("s", "BTREE", ["s"], max_leaf_size=2),
    )
    got = rows_of(idx.search(RangeQuery("b", "e")))
    assert got == brute(values, lambda v: v is not None and "b" <= v <= "e")
    assert rows_of(idx.search(IsIn(["apple"]))) == [0, 3]


def test_btree_excludes_null_rows_from_equality_and_range():
    values = [1, None, 2, None, 3]
    idx = BTreeIndex.build(
        int_array(values), np.arange(len(values)), metadata=meta("n", "BTREE", ["v"])
    )
    assert idx.stats()["null_rows"] == 2
    # null 不进索引 → 索引的"行宇宙"只有 3 行
    assert sorted(int(x) for x in idx.row_ids) == [0, 2, 4]
    assert rows_of(idx.search(RangeQuery(0, 10))) == [0, 2, 4]
    assert rows_of(idx.search(IsIn([None]))) == []


def test_btree_null_rows_are_not_pulled_in_by_not():
    """``NOT(col = 1)`` 不能把 null 行捞出来 —— SQL 三值逻辑要求它不匹配。"""

    values = [1, None, 2]
    idx = BTreeIndex.build(
        int_array(values), np.arange(len(values)), metadata=meta("n", "BTREE", ["v"])
    )
    got = rows_of(idx.search(Not(IsIn([1]))))
    assert got == [2]
    assert 1 not in got


def test_btree_boolean_composition():
    idx, values = btree_index()
    q = And([RangeQuery(3, 7), IsIn([3, 9])])
    assert rows_of(idx.search(q)) == brute(values, lambda v: 3 <= v <= 7 and v in (3, 9))

    q = Or([RangeQuery(0, 1), IsIn([9])])
    assert rows_of(idx.search(q)) == brute(values, lambda v: 0 <= v <= 1 or v == 9)

    q = Not(RangeQuery(3, 7))
    assert rows_of(idx.search(q)) == brute(values, lambda v: not (3 <= v <= 7))


def test_btree_exact_match_is_passed_through():
    idx, _ = btree_index()
    given = ExactMatch(np.asarray([1, 5], dtype=np.int64))
    assert idx.search(given) is given


def test_btree_returns_none_for_predicates_it_cannot_answer():
    idx, _ = btree_index()
    # FTS 的 MatchQuery 对 BTREE 毫无意义 —— 诚实地说"我答不了"
    assert idx.search(MatchQuery(query="hello")) is None


def test_btree_empty_column():
    idx = BTreeIndex.build(
        int_array([]), np.arange(0), metadata=meta("e", "BTREE", ["v"])
    )
    assert rows_of(idx.search(RangeQuery(1, 2))) == []
    assert rows_of(idx.search(IsIn([1]))) == []
    assert idx.stats()["num_keys"] == 0
    assert idx.key_bounds() == (None, None)


def test_btree_all_null_column():
    idx = BTreeIndex.build(
        int_array([None, None, None]),
        np.arange(3),
        metadata=meta("n", "BTREE", ["v"]),
    )
    assert rows_of(idx.search(RangeQuery(0, 100))) == []
    assert idx.stats()["null_rows"] == 3
    assert idx.stats()["num_keys"] == 0


def test_btree_single_value_column():
    idx = BTreeIndex.build(
        int_array([7, 7, 7, 7]), np.arange(4), metadata=meta("s", "BTREE", ["v"])
    )
    assert rows_of(idx.search(IsIn([7]))) == [0, 1, 2, 3]
    assert rows_of(idx.search(RangeQuery(0, 6))) == []
    assert rows_of(idx.search(RangeQuery(7, 7))) == [0, 1, 2, 3]
    assert idx.stats()["cardinality"] == pytest.approx(0.25)


def test_btree_save_load_roundtrip():
    values = list(range(30))
    random.Random(3).shuffle(values)
    idx, values = btree_index(values, max_leaf_size=3, max_children=3)
    store = DictIndexStore()
    files = idx.save(store)
    assert "keys.json" in files
    loaded = BTreeIndex.load(store, idx.metadata)
    for start, end in [(0, 29), (4, 11), (25, 40)]:
        assert rows_of(loaded.search(RangeQuery(start, end))) == rows_of(
            idx.search(RangeQuery(start, end))
        )
    assert loaded.num_keys() == idx.num_keys()
    assert loaded.key_bounds() == idx.key_bounds()


def test_btree_stats_fields():
    idx, _ = btree_index()
    s = idx.stats()
    for key in (
        "num_rows",
        "num_keys",
        "num_nodes",
        "num_leaves",
        "depth",
        "null_rows",
        "min_key",
        "max_key",
        "cardinality",
        "size_bytes",
    ):
        assert key in s, key
    assert s["index_type"] == "BTREE"
    assert s["num_rows"] == len(BTREE_VALUES)
    assert s["num_keys"] == len(set(BTREE_VALUES))
    assert s["min_key"] == min(BTREE_VALUES)
    assert s["max_key"] == max(BTREE_VALUES)


def test_btree_rejects_absurd_parameters():
    with pytest.raises(IndexBuildError):
        BTreeIndex.build(
            int_array([1]), np.arange(1), metadata=meta("x", "BTREE", ["v"], max_leaf_size=1)
        )


# ---------------------------------------------------------------------------
# BITMAP
# ---------------------------------------------------------------------------

BITMAP_VALUES = ["a", "b", "a", "c", "b", "a", None, "c", "a"]


def bitmap_index(values=None):
    values = BITMAP_VALUES if values is None else list(values)
    md = meta("bm", "BITMAP", ["c"])
    return BitmapIndex.build(str_array(values), np.arange(len(values)), metadata=md), values


def test_bitmap_equality_matches_bruteforce():
    idx, values = bitmap_index()
    for v in ("a", "b", "c"):
        assert rows_of(idx.search(IsIn([v]))) == brute(values, lambda x: x == v)


def test_bitmap_isin_matches_bruteforce():
    idx, values = bitmap_index()
    got = rows_of(idx.search(IsIn(["a", "c"])))
    assert got == brute(values, lambda v: v in ("a", "c"))


def test_bitmap_unknown_value_yields_empty_not_none():
    """索引**知道**这个值不存在，所以答案应该是"空集"而不是"我答不了"。"""

    idx, _ = bitmap_index()
    result = idx.search(IsIn(["nope"]))
    assert result is not None
    assert rows_of(result) == []


def test_bitmap_returns_none_for_range_queries():
    idx, _ = bitmap_index()
    assert idx.search(RangeQuery("a", "c")) is None


def test_bitmap_boolean_composition_in_word_space():
    idx, values = bitmap_index()
    q = And([IsIn(["a"]), IsIn(["c"])])
    assert rows_of(idx.search(q)) == []  # 一行只能有一个值

    q = Or([IsIn(["b"]), IsIn(["c"])])
    assert rows_of(idx.search(q)) == brute(values, lambda v: v in ("b", "c"))

    q = Not(IsIn(["b"]))
    # row 6 是 null → 不在索引的行宇宙里
    assert rows_of(idx.search(q)) == brute(values, lambda v: v is not None and v != "b")


def test_bitmap_not_excludes_null_rows():
    idx, _ = bitmap_index()
    got = rows_of(idx.search(Not(IsIn(["a"]))))
    assert 6 not in got, "null 行不该被 NOT 捞出来"
    assert got == [1, 3, 4, 7]


def test_bitmap_multiword_bitmaps_beyond_64_rows():
    values = ["v%d" % (i % 5) for i in range(300)]
    idx = BitmapIndex.build(
        str_array(values), np.arange(len(values)), metadata=meta("big", "BITMAP", ["c"])
    )
    assert idx.nwords == 5  # ceil(300 / 64)
    for v in ("v0", "v3"):
        assert rows_of(idx.search(IsIn([v]))) == brute(values, lambda x: x == v)
    assert rows_of(idx.search(Not(IsIn(["v0"])))) == brute(
        values, lambda v: v != "v0"
    )


def test_bitmap_empty_column():
    idx = BitmapIndex.build(
        str_array([]), np.arange(0), metadata=meta("e", "BITMAP", ["c"])
    )
    assert rows_of(idx.search(IsIn(["a"]))) == []
    assert idx.stats()["num_values"] == 0
    assert idx.stats()["nwords"] == 0


def test_bitmap_all_null_column():
    idx = BitmapIndex.build(
        str_array([None, None]), np.arange(2), metadata=meta("n", "BITMAP", ["c"])
    )
    assert rows_of(idx.search(IsIn(["a"]))) == []
    assert rows_of(idx.search(Not(IsIn(["a"])))) == []
    assert idx.stats()["null_rows"] == 2


def test_bitmap_single_value_column():
    idx = BitmapIndex.build(
        str_array(["x"] * 5), np.arange(5), metadata=meta("s", "BITMAP", ["c"])
    )
    assert rows_of(idx.search(IsIn(["x"]))) == [0, 1, 2, 3, 4]
    s = idx.stats()
    assert s["num_values"] == 1
    assert s["max_rows_per_value"] == 5
    # 1 张位图 = 1 个 64 位字 = 8 字节 → 每行摊到 64/5 = 12.8 比特。
    # 行数少的时候这个数字天然偏高（一整个字都浪费掉了），要在大数据上看。
    assert s["bits_per_row"] == pytest.approx(64 / 5, rel=1e-9)


def test_bitmap_save_load_roundtrip():
    idx, values = bitmap_index()
    store = DictIndexStore()
    idx.save(store)
    loaded = BitmapIndex.load(store, idx.metadata)
    assert loaded.value_list == idx.value_list
    for q in (IsIn(["a"]), Or([IsIn(["b"]), IsIn(["c"])]), Not(IsIn(["c"]))):
        assert rows_of(loaded.search(q)) == rows_of(idx.search(q))


def test_bitmap_stats_fields_and_cardinality_signal():
    idx, values = bitmap_index()
    s = idx.stats()
    for key in (
        "num_rows",
        "num_values",
        "nwords",
        "null_rows",
        "total_bitmap_bytes",
        "bits_per_row",
        "avg_rows_per_value",
        "size_bytes",
    ):
        assert key in s, key
    assert s["num_rows"] == len(values) - 1  # null 不计
    assert s["num_values"] == 3
    # 3 张位图 × 1 个字 = 192 比特，摊到 8 行就是每行 24 比特 ——
    # 这个数在小表上一定难看，它的价值在于**随行数增长极慢**（见下一个测试）
    assert s["bits_per_row"] == pytest.approx(3 * 64 / 8, rel=1e-9)


def test_bitmap_bytes_per_row_shrinks_as_the_table_grows():
    """位图的摊销成本随行数下降 —— 这才是低基数列该用 BITMAP 的量化依据。"""

    small = BitmapIndex.build(
        str_array(["v%d" % (i % 3) for i in range(8)]),
        np.arange(8),
        metadata=meta("s", "BITMAP", ["c"]),
    )
    big = BitmapIndex.build(
        str_array(["v%d" % (i % 3) for i in range(2000)]),
        np.arange(2000),
        metadata=meta("b", "BITMAP", ["c"]),
    )
    assert big.stats()["bits_per_row"] < small.stats()["bits_per_row"]
    # 2000 行、3 个值：ceil(2000/64)=32 个字/张 × 3 张 × 64 比特 / 2000 行 = 3.07
    assert big.stats()["bits_per_row"] == pytest.approx(3 * 32 * 64 / 2000, rel=1e-9)
    assert big.stats()["bits_per_row"] < 4


def test_bitmap_rejects_unhashable_values():
    """结构体（dict）不能做 BITMAP 的键 —— 明确报错，而不是悄悄退化成慢路径。"""

    structs = array_from_pylist(
        StructType([Field("a", INT64)]), [{"a": 1}, {"a": 2}]
    )
    with pytest.raises(IndexBuildError) as excinfo:
        BitmapIndex.build(structs, np.arange(2), metadata=meta("bad", "BITMAP", ["c"]))
    assert "可哈希" in str(excinfo.value)


# ---------------------------------------------------------------------------
# LABEL_LIST
# ---------------------------------------------------------------------------

LABEL_VALUES = [
    ["a", "b"],
    ["b"],
    ["c"],
    ["a", "c"],
    None,
    [],
    ["b", "c", "b"],  # 一行里重复的标签只算一次
    ["a"],
]


def label_index(values=None):
    values = LABEL_VALUES if values is None else list(values)
    md = meta("ll", "LABEL_LIST", ["tags"])
    return LabelListIndex.build(list_array(values), np.arange(len(values)), metadata=md), values


def test_label_list_array_contains_matches_bruteforce():
    idx, values = label_index()
    for label in ("a", "b", "c"):
        got = rows_of(idx.search(ArrayContains(label)))
        assert got == brute(values, lambda v: v is not None and label in v)


def test_label_list_isin_means_any_label_matches():
    idx, values = label_index()
    got = rows_of(idx.search(IsIn(["a", "c"])))
    assert got == brute(values, lambda v: v is not None and bool(set(v) & {"a", "c"}))


def test_label_list_and_or_not_composition():
    idx, values = label_index()
    q = And([ArrayContains("a"), ArrayContains("c")])
    assert rows_of(idx.search(q)) == brute(
        values, lambda v: v is not None and "a" in v and "c" in v
    )
    q = Or([ArrayContains("a"), ArrayContains("c")])
    assert rows_of(idx.search(q)) == brute(
        values, lambda v: v is not None and ("a" in v or "c" in v)
    )
    q = Not(ArrayContains("b"))
    # null 行被排除；空列表行**在**结果里（"不含 b"对它成立）
    assert rows_of(idx.search(q)) == [2, 3, 5, 7]


def test_label_list_duplicate_labels_counted_once():
    idx, _ = label_index()
    assert rows_of(idx.search(ArrayContains("b"))) == [0, 1, 6]
    # labels_per_row 按"被索引行的局部下标"对齐：原始第 6 行是第 6 个非 null 行
    assert idx.labels_per_row.tolist()[5] == 2  # ["b","c","b"] → 2 个不同标签


def test_label_list_returns_none_for_range():
    idx, _ = label_index()
    assert idx.search(RangeQuery("a", "c")) is None


def test_label_list_requires_a_list_column():
    with pytest.raises(IndexBuildError) as excinfo:
        LabelListIndex.build(
            str_array(["a", "b"]), np.arange(2), metadata=meta("x", "LABEL_LIST", ["c"])
        )
    assert "list" in str(excinfo.value)


def test_label_list_empty_column_and_all_null():
    empty = LabelListIndex.build(
        list_array([]), np.arange(0), metadata=meta("e", "LABEL_LIST", ["t"])
    )
    assert rows_of(empty.search(ArrayContains("a"))) == []

    allnull = LabelListIndex.build(
        list_array([None, None]), np.arange(2), metadata=meta("n", "LABEL_LIST", ["t"])
    )
    assert rows_of(allnull.search(ArrayContains("a"))) == []
    assert rows_of(allnull.search(Not(ArrayContains("a")))) == []


def test_label_list_single_repeated_label_column():
    idx = LabelListIndex.build(
        list_array([["x"], ["x"], ["x"]]),
        np.arange(3),
        metadata=meta("s", "LABEL_LIST", ["t"]),
    )
    assert rows_of(idx.search(ArrayContains("x"))) == [0, 1, 2]
    assert idx.stats()["num_labels"] == 1


def test_label_list_save_load_roundtrip():
    idx, _ = label_index()
    store = DictIndexStore()
    idx.save(store)
    loaded = LabelListIndex.load(store, idx.metadata)
    assert rows_of(loaded.search(ArrayContains("b"))) == rows_of(
        idx.search(ArrayContains("b"))
    )
    assert loaded.stats()["empty_rows"] == idx.stats()["empty_rows"]


def test_label_list_stats_fields():
    idx, values = label_index()
    s = idx.stats()
    for key in (
        "num_rows",
        "num_labels",
        "null_rows",
        "empty_rows",
        "num_label_assignments",
        "avg_labels_per_row",
        "max_labels_per_row",
        "bits_per_row",
        "size_bytes",
    ):
        assert key in s, key
    assert s["index_type"] == "LABEL_LIST"
    assert s["size_bytes"] > 0


# ---------------------------------------------------------------------------
# NGRAM
# ---------------------------------------------------------------------------

NGRAM_VALUES = [
    "the database is fast",
    "abacus and beads",
    "data base layer",
    "postgres rocks",
    "abase of data",
    None,
    "xyz",
]


def ngram_index(values=None, n=3):
    values = NGRAM_VALUES if values is None else list(values)
    md = meta("ng", "NGRAM", ["t"], ngram_size=n)
    return NGramIndex.build(str_array(values), np.arange(len(values)), metadata=md), values


@pytest.mark.parametrize("needle", ["aba", "data", "base", "cus", "database", "xyz", "zzz"])
def test_ngram_contains_matches_bruteforce(needle):
    idx, values = ngram_index()
    got = rows_of(idx.search(Contains(needle)))
    assert got == brute(values, lambda v: v is not None and needle in v)


def test_ngram_candidate_superset_and_verification_matters():
    """构造一个**假阳性**：gram 全中但子串不在。

    ``'uvwabccuvbcu'`` 含 ``abc`` / ``bcu`` / ``cuv`` / ``uvw`` 四个 3-gram，
    但显然不含 ``'abcuvw'`` —— 三个碎片都在，顺序和距离却不对。
    这正是"索引给候选、必须再验证"的原因。
    """

    values = ["uvwabccuvbcu", "abcuvw", "nothing"]
    idx = NGramIndex.build(
        str_array(values), np.arange(3), metadata=meta("fp", "NGRAM", ["t"])
    )
    candidates = idx.candidates("abcuvw")
    assert candidates == {0, 1}, "候选集应当包含那个假阳性行"

    result = idx.search(Contains("abcuvw"))
    assert rows_of(result) == [1]  # 验证步骤把它剔掉了
    assert idx.last_search_stats["raw_candidates"] == 2
    assert idx.last_search_stats["false_positives"] == 1


def test_ngram_short_needle_cannot_use_the_index():
    idx, _ = ngram_index()
    assert idx.search(Contains("ab")) is None


def test_ngram_case_insensitive_cannot_use_the_index():
    idx, _ = ngram_index()
    assert idx.search(Contains("Aba", case_insensitive=True)) is None


@pytest.mark.parametrize(
    "pattern,expected_supported",
    [
        ("%aba%", True),
        ("%data%", True),
        ("%zzz%", True),
        ("aba", True),
        ("%ab_%", False),
        ("aba%", False),
        ("%aba", False),
        ("a%b", False),
    ],
)
def test_ngram_like_pattern_support(pattern, expected_supported):
    idx, values = ngram_index()
    result = idx.search(Like(pattern))
    if not expected_supported:
        assert result is None
        return
    assert result is not None
    got = rows_of(result)
    if pattern.startswith("%") and pattern.endswith("%") and len(pattern) > 2:
        needle = pattern[1:-1]
        assert got == brute(values, lambda v: v is not None and needle in v)
    else:
        assert got == brute(values, lambda v: v == pattern)


def test_ngram_like_underscore_is_not_treated_as_literal():
    """``_`` 是通配符，把它当字面量搜会漏掉 'abc' —— 所以这类模式直接放弃。"""

    idx = NGramIndex.build(
        str_array(["abc"]), np.arange(1), metadata=meta("u", "NGRAM", ["t"])
    )
    assert idx.search(Like("%a_c%")) is None


def test_ngram_returns_none_for_unsupported_predicates():
    idx, _ = ngram_index()
    assert idx.search(RangeQuery("a", "z")) is None
    assert idx.search(IsIn(["x"])) is None


def test_ngram_boolean_composition():
    idx, values = ngram_index()
    q = And([Contains("data"), Contains("base")])
    assert rows_of(idx.search(q)) == brute(
        values, lambda v: v is not None and "data" in v and "base" in v
    )
    q = Or([Contains("abacus"), Contains("postgres")])
    assert rows_of(idx.search(q)) == brute(
        values, lambda v: v is not None and ("abacus" in v or "postgres" in v)
    )


def test_ngram_empty_and_all_null():
    empty = NGramIndex.build(
        str_array([]), np.arange(0), metadata=meta("e", "NGRAM", ["t"])
    )
    assert rows_of(empty.search(Contains("abc"))) == []
    assert empty.stats()["num_grams"] == 0

    allnull = NGramIndex.build(
        str_array([None, None]), np.arange(2), metadata=meta("n", "NGRAM", ["t"])
    )
    assert rows_of(allnull.search(Contains("abc"))) == []
    assert allnull.stats()["null_rows"] == 2


def test_ngram_single_value_column():
    idx = NGramIndex.build(
        str_array(["abcabc"]), np.arange(1), metadata=meta("s", "NGRAM", ["t"])
    )
    assert rows_of(idx.search(Contains("abc"))) == [0]
    # "ca" 只有 2 个字符、短于 gram 长度 → 索引答不了（返回 None 让上游全扫）
    assert idx.search(Contains("ca")) is None
    # "abcabc" 的 3-gram 去重后只有 3 个：abc / bca / cab（每个又出现一次）
    assert idx.stats()["num_grams"] == 3
    assert idx.stats()["num_postings"] == 3


def test_ngram_configurable_gram_size():
    values = ["abcdef"]
    idx4 = NGramIndex.build(
        str_array(values), np.arange(1), metadata=meta("n4", "NGRAM", ["t"], ngram_size=4)
    )
    assert rows_of(idx4.search(Contains("abcd"))) == [0]
    assert idx4.search(Contains("abc")) is None  # 短于 gram 长度
    assert idx4.stats()["ngram_size"] == 4


def test_ngram_save_load_roundtrip():
    idx, _ = ngram_index()
    store = DictIndexStore()
    idx.save(store)
    loaded = NGramIndex.load(store, idx.metadata)
    for needle in ("aba", "database", "zzz"):
        assert rows_of(loaded.search(Contains(needle))) == rows_of(
            idx.search(Contains(needle))
        )
    assert loaded.stats()["ngram_size"] == 3


def test_ngram_stats_fields():
    idx, _ = ngram_index()
    s = idx.stats()
    for key in (
        "num_rows",
        "ngram_size",
        "num_grams",
        "num_postings",
        "null_rows",
        "avg_grams_per_row",
        "avg_rows_per_gram",
        "max_rows_per_gram",
        "size_bytes",
    ):
        assert key in s, key
    assert s["index_type"] == "NGRAM"


# ---------------------------------------------------------------------------
# ZONEMAP
# ---------------------------------------------------------------------------

SORTED = list(range(1000))
SHUFFLED = list(SORTED)
random.Random(11).shuffle(SHUFFLED)


def zonemap_index(values, page_size=100, name="zm"):
    md = meta(name, "ZONEMAP", ["x"], page_size=page_size)
    return ZoneMapIndex.build(
        int_array(values), np.arange(len(values)), metadata=md
    )


def test_zonemap_prunes_blocks_on_selective_predicate():
    idx = zonemap_index(SORTED)
    keep, stats = idx.prune_expr(RangeQuery(0, 50))
    assert isinstance(stats, PruningStats)
    assert stats.blocks_total == 10
    assert stats.blocks_pruned == 9
    assert stats.rows_skipped == 900
    assert stats.bytes_skipped_estimate > 0
    assert stats.prune_ratio == pytest.approx(0.9)
    # 只有第 0 块可能含 0..50
    assert int(keep.sum()) == 100
    assert np.flatnonzero(idx.prune(RangeQuery(0, 50))).tolist() == list(range(1, 10))


@pytest.mark.parametrize(
    "start,end",
    [(0, 50), (0, 0), (500, 520), (999, 999), (100, 899), (250, 249), (2000, 3000)],
)
def test_zonemap_never_prunes_a_matching_row(start, end):
    """zone map 的正确性底线：剪掉的块里不能有真正命中的行。"""

    idx = zonemap_index(SORTED)
    keep, stats = idx.prune_expr(RangeQuery(start, end))
    survivors = [int(r) for r in np.flatnonzero(keep)]
    trues = brute(SORTED, lambda v: start <= v <= end)
    assert set(trues) <= set(survivors), (start, end)
    # 而且"块内再求值"的结果必须和全表暴力完全一致
    got, _ = idx.filter_by_expr(RangeQuery(start, end), lambda v: start <= v <= end)
    assert [int(x) for x in got] == trues


def test_zonemap_random_data_defeats_pruning():
    """随机排布的数据让 zone map 完全失效 —— 效果取决于数据布局，不是魔法。"""

    idx = zonemap_index(SHUFFLED)
    _, stats = idx.prune_expr(RangeQuery(0, 50))
    assert stats.blocks_pruned == 0
    assert stats.prune_ratio == 0.0

    # 同样的谓词、同样的数据量，只是排布不同
    sorted_idx = zonemap_index(SORTED)
    _, sorted_stats = sorted_idx.prune_expr(RangeQuery(0, 50))
    assert sorted_stats.blocks_pruned > stats.blocks_pruned


def test_zonemap_and_matcher_unions_pruning():
    idx = zonemap_index(SORTED)
    _, stats = idx.prune_expr(And([RangeQuery(0, 500), IsIn([250, 260])]))
    # AND 的两个条件各自都能排除大部分块 → 剪枝取并集
    assert stats.blocks_pruned >= 9

    # x 在 [0,500] 且 x 在 {2000}：第二个条件一个块都命中不了
    _, stats = idx.prune_expr(And([RangeQuery(0, 500), IsIn([2000])]))
    assert stats.blocks_pruned == stats.blocks_total

    _, stats = idx.prune_expr(Or([RangeQuery(0, 50), RangeQuery(900, 950)]))
    # OR 只能剪"两个条件都排除"的块 → 8 块
    assert stats.blocks_pruned == 8


def test_zonemap_unsupported_predicate_prunes_nothing():
    idx = zonemap_index(SORTED)
    # NOT 无法用 min/max 剪枝（需要"整块都满足"这种信息）
    assert idx.search(Not(RangeQuery(0, 50))) is None
    keep, stats = idx.prune_expr(Not(RangeQuery(0, 50)))
    assert stats.blocks_pruned == 0
    assert stats.matcher == "unsupported"
    assert bool(keep.all())


def test_zonemap_null_matcher_uses_null_count():
    values = [1, None, None, 4, 5, None]
    idx = zonemap_index(values, page_size=3)
    assert idx.blocks[0].null_count == 2
    assert idx.blocks[1].null_count == 1
    # IS NULL / IS NOT NULL 是唯一真正读 null_count 的谓词
    np.testing.assert_array_equal(idx.prune(NullMatcher(want_null=True)), [False, False])
    np.testing.assert_array_equal(idx.prune(NullMatcher(want_null=False)), [False, False])

    all_null = zonemap_index([None] * 10, page_size=4)
    assert all_null.prune(NullMatcher(want_null=True)).tolist() == [False, False, False]
    assert all_null.prune(NullMatcher(want_null=False)).tolist() == [True, True, True]
    assert all_null.prune(RangeQuery(0, 10)).tolist() == [True, True, True]


def test_zonemap_matcher_from_expr_translations():
    assert isinstance(matcher_from_expr(RangeQuery(1, 2)), RangeMatcher)
    assert isinstance(matcher_from_expr(IsIn([1, 2])), SetMatcher)
    assert isinstance(matcher_from_expr(And([RangeQuery(1, 2)])), AndMatcher)
    assert matcher_from_expr(Not(RangeQuery(1, 2))) is None
    assert matcher_from_expr(ExactMatch(np.asarray([1]))) is None


def test_zonemap_search_always_returns_none():
    """zone map 不是"索引"：它不产生行号集合，所以一律答不了。"""

    idx = zonemap_index(SORTED)
    for expr in (
        RangeQuery(0, 1),
        IsIn([1]),
        And([RangeQuery(0, 1)]),
        ExactMatch(np.asarray([0])),
    ):
        assert idx.search(expr) is None


def test_zonemap_block_statistics_are_exact():
    values = [3, 1, 2, 9, 8, 7]
    idx = zonemap_index(values, page_size=3)
    assert [b.min_value for b in idx.blocks] == [1, 7]
    assert [b.max_value for b in idx.blocks] == [3, 9]
    assert [b.row_start for b in idx.blocks] == [0, 3]
    assert [b.row_end for b in idx.blocks] == [3, 6]
    assert idx.blocks[0].as_dict()["min"] == 1
    assert idx.min_value == 1 and idx.max_value == 9


def test_zonemap_string_blocks_prune_correctly():
    # 刻意补零：字符串是按字典序比的，"a10" < "a9"，不补零的话
    # 块的 min/max 和查询区间会对不上，演示不出剪枝效果。
    values = ["a%03d" % i for i in range(100)]
    idx = ZoneMapIndex.build(
        str_array(values),
        np.arange(100),
        metadata=meta("zs", "ZONEMAP", ["s"], page_size=10),
    )
    keep, stats = idx.prune_expr(RangeQuery("a000", "a009"))
    assert stats.blocks_pruned == 9
    trues = brute(values, lambda v: "a000" <= v <= "a009")
    assert [int(x) for x in np.flatnonzero(keep)] == trues


def test_zonemap_empty_and_single_value_column():
    empty = ZoneMapIndex.build(
        int_array([]), np.arange(0), metadata=meta("e", "ZONEMAP", ["x"], page_size=4)
    )
    assert empty.blocks == []
    assert empty.prune(RangeQuery(0, 1)).tolist() == []
    assert empty.search(RangeQuery(0, 1)) is None

    single = zonemap_index([42] * 6, page_size=4)
    assert [b.min_value for b in single.blocks] == [42, 42]
    assert single.prune(RangeQuery(0, 41)).tolist() == [True, True]
    assert single.prune(RangeQuery(42, 42)).tolist() == [False, False]


def test_zonemap_pruning_stats_reports_bytes_skipped():
    idx = zonemap_index(SORTED)
    _, stats = idx.prune_expr(RangeQuery(0, 50))
    assert stats.bytes_skipped_estimate == int(round(900 * idx.bytes_per_row))
    assert stats.bytes_skipped_estimate > 0
    d = stats.as_dict()
    assert d["blocks_kept"] == stats.blocks_kept
    assert d["row_skip_ratio"] == pytest.approx(0.9)
    assert str(stats)


def test_zonemap_save_load_roundtrip():
    idx = zonemap_index(SORTED)
    store = DictIndexStore()
    idx.save(store)
    loaded = ZoneMapIndex.load(store, idx.metadata)
    assert len(loaded.blocks) == len(idx.blocks)
    assert loaded.page_size == idx.page_size
    assert loaded.prune_expr(RangeQuery(0, 50))[1].blocks_pruned == 9
    # zone map 永远不产生行号集合
    assert loaded.search(RangeQuery(0, 1)) is None
    assert loaded.value_at(5) == 5


def test_zonemap_filter_by_expr_is_exactly_bruteforce():
    """下推 + 求值的结果必须和"直接全表求值"逐行相等。"""

    values = [i % 97 for i in range(500)]
    idx = zonemap_index(values, page_size=50)
    pred = lambda v: 10 <= v < 20  # noqa: E731
    got, stats = idx.filter_by_expr(RangeQuery(10, 19), pred)
    assert [int(x) for x in got] == brute(values, pred)


def test_zonemap_stats_fields():
    idx = zonemap_index(SORTED)
    s = idx.stats()
    for key in (
        "num_rows",
        "page_size",
        "num_blocks",
        "blocks_with_nulls",
        "null_rows",
        "min_value",
        "max_value",
        "avg_block_span",
        "size_bytes",
    ):
        assert key in s, key
    assert s["index_type"] == "ZONEMAP"
    assert s["num_blocks"] == 10
    assert s["avg_block_span"] == pytest.approx(99.0)
    assert len(idx.block_table()) == 10


def test_zonemap_rejects_bad_page_size():
    with pytest.raises(IndexBuildError):
        ZoneMapIndex.build(
            int_array([1]), np.arange(1), metadata=meta("x", "ZONEMAP", ["v"], page_size=0)
        )


# ---------------------------------------------------------------------------
# 跨索引的公共契约
# ---------------------------------------------------------------------------


def test_all_indexes_declare_the_real_lancedb_type_name():
    assert BTreeIndex.type_name == "BTREE"
    assert BitmapIndex.type_name == "BITMAP"
    assert LabelListIndex.type_name == "LABEL_LIST"
    assert NGramIndex.type_name == "NGRAM"
    assert ZoneMapIndex.type_name == "ZONEMAP"


def test_index_metadata_is_filled_in_by_every_build():
    """每个索引建完之后都要把元数据补齐 —— 否则"这个索引覆盖哪些行"无从查起。

    ``num_indexed_rows`` 的含义是"**真的进了索引**的行数"：
    null 行不参与值匹配，所以除 ZONEMAP（它记录 null 计数）外都要扣掉。
    """

    def non_null(values):
        return sum(v is not None for v in values)

    builders = [
        (BTreeIndex, int_array(BTREE_VALUES), "BTREE", non_null(BTREE_VALUES)),
        (BitmapIndex, str_array(BITMAP_VALUES), "BITMAP", non_null(BITMAP_VALUES)),
        (LabelListIndex, list_array(LABEL_VALUES), "LABEL_LIST", non_null(LABEL_VALUES)),
        (NGramIndex, str_array(NGRAM_VALUES), "NGRAM", non_null(NGRAM_VALUES)),
        (ZoneMapIndex, int_array(SORTED), "ZONEMAP", len(SORTED)),
    ]
    for cls, array, type_name, expected_rows in builders:
        md = meta("i", type_name, ["c"])
        idx = cls.build(array, np.arange(len(array)), metadata=md)
        assert md.index_type == type_name
        assert md.num_indexed_rows == expected_rows, type_name
        assert md.size_bytes > 0
        assert idx.stats()["size_bytes"] > 0


def test_row_ids_mapping_is_respected_for_custom_row_ids():
    """索引返回的必须是**真实行号**，不是被索引行里的局部下标。"""

    values = [5, 3, 9, 1]
    custom = np.asarray([100, 205, 310, 415], dtype=np.int64)
    idx = BTreeIndex.build(
        int_array(values), custom, metadata=meta("r", "BTREE", ["v"])
    )
    assert rows_of(idx.search(RangeQuery(3, 5))) == [100, 205]
    assert rows_of(idx.search(IsIn([1]))) == [415]
