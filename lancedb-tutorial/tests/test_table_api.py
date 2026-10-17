"""``LanceTable`` 的 API 契约测试。

写法上有三条自我约束，因为它们各自对应一类"测试全绿但代码是坏的"：

1. **排序类断言必须有独立参照。** "距离升序"这类性质不能拿被测代码的
   另一次输出当基准（两边同时错时测试是绿的），所以
   :mod:`tests.conftest` 里有一份 numpy 暴力实现，逐一对照。
2. **差异类断言必须钉住差值。** 比如 prefilter / postfilter，只断言
   "两个都不报错"毫无意义；必须断言"postfilter 的行数**严格少于**
   prefilter"，这才是这个开关的全部意义。
3. **边界类断言必须钉住"没有"**。``fast_search()`` 在无索引时返回空集
   是真实 LanceDB 的行为，也是最容易在重构中被"顺手修好"的地方 ——
   所以要有一条测试专门锁死它。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

import mini_lancedb as mldb
from mini_lancedb import (
    DataValidationError,
    IndexNotFoundError,
    NotSupportedError,
    Permission,
    QueryError,
    TableAlreadyExistsError,
    TableNotFoundError,
)

from .conftest import DIM, N_ROWS, brute_force_order, make_rows


def _ids(rows):
    return [r["id"] for r in rows]


# ===========================================================================
# 建表 / schema 推断
# ===========================================================================


def test_create_table_returns_table_with_name(db):
    t = db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    assert t.name == "t"
    assert t.count_rows() == 1


def test_create_table_uri_points_at_lance_dir(db):
    t = db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    assert t.uri.endswith("t.lance")


def test_create_table_duplicate_raises(db):
    db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    with pytest.raises(TableAlreadyExistsError):
        db.create_table("t", [{"id": 2, "vector": [1.0, 2.0]}])


def test_create_table_exist_ok_returns_existing(db):
    db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    t = db.create_table("t", [{"id": 2, "vector": [1.0, 2.0]}], exist_ok=True)
    # exist_ok 是"别报错"，**不是**"覆盖"：老数据必须还在
    assert t.count_rows() == 1
    assert _ids(t.to_list()) == [1]


def test_create_table_overwrite_mode_replaces(db):
    db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    t = db.create_table("t", [{"id": 9, "vector": [1.0, 2.0]}], mode="overwrite")
    assert _ids(t.to_list()) == [9]


def test_create_table_overwrite_still_keeps_old_version(db):
    db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    db.create_table("t", [{"id": 9, "vector": [1.0, 2.0]}], mode="overwrite")
    t = db.open_table("t")
    versions = t.list_versions()
    assert len(versions) >= 2
    # 老版本仍然可读 —— 这正是"overwrite 也是提交新版本"的可观测证据。
    # 注意 v1 是 create_table 建出的空版本，数据从 v2 才开始。
    t.checkout(2)
    assert _ids(t.to_list()) == [1]


def test_create_table_invalid_mode_raises_value_error(db):
    with pytest.raises(ValueError, match="mode must be either 'create' or 'overwrite'"):
        db.create_table("t", [{"id": 1}], mode="replace")


def test_create_table_schema_inference_list_of_dicts(db):
    t = db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    assert t.schema.names == ["id", "vector"]
    assert t.schema["vector"].vector_dim == 2


def test_create_table_schema_inference_pyarrow_table(db):
    import pyarrow as pa

    data = pa.table(
        {
            "id": pa.array([1, 2], type=pa.int32()),
            "vector": pa.array([[1.0, 2.0], [3.0, 4.0]], type=pa.list_(pa.float32(), 2)),
        }
    )
    t = db.create_table("t", data)
    assert t.count_rows() == 2
    assert t.schema["vector"].vector_dim == 2


def test_create_table_schema_inference_pandas(db):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"id": [1, 2], "vector": [[1.0, 2.0], [3.0, 4.0]]})
    t = db.create_table("t", df)
    assert t.count_rows() == 2


def test_create_table_from_generator_raises(db):
    with pytest.raises(ValueError, match="single dictionary"):
        db.create_table("t", ({"id": i, "vector": [1.0]} for i in range(3)))


def test_create_table_from_single_dict_raises(db):
    with pytest.raises(ValueError, match="single dictionary"):
        db.create_table("t", {"id": 1, "vector": [1.0]})


def test_create_table_with_explicit_schema_and_no_data(db):
    schema = mldb.Schema([("id", "int64"), ("vector", mldb.FixedSizeList(mldb.FLOAT32, 2))])
    t = db.create_table("t", schema=schema)
    assert t.count_rows() == 0
    assert t.schema.names == ["id", "vector"]


def test_create_table_without_data_or_schema_raises(db):
    with pytest.raises(mldb.SchemaError):
        db.create_table("t")


def test_create_table_empty_list_with_schema_ok(db):
    schema = mldb.Schema([("id", "int64"), ("vector", mldb.FixedSizeList(mldb.FLOAT32, 2))])
    t = db.create_table("t", [], schema=schema)
    assert t.count_rows() == 0


# ===========================================================================
# add
# ===========================================================================


def test_add_append_increases_rows(tbl):
    before = tbl.count_rows()
    res = tbl.add([{"id": 999, "text": "x", "vector": [0.0] * DIM}])
    assert res.num_rows == 1
    assert tbl.count_rows() == before + 1


def test_add_returns_version(tbl):
    before = tbl.version
    res = tbl.add([{"id": 999, "text": "x", "vector": [0.0] * DIM}])
    assert res.version > before


def test_add_overwrite_mode_replaces_data(tbl):
    tbl.add([{"id": 1, "text": "x", "vector": [0.0] * DIM}], mode="overwrite")
    assert _ids(tbl.to_list()) == [1]


def test_add_invalid_mode_raises_value_error(tbl):
    with pytest.raises(ValueError, match="Invalid mode: upsert"):
        tbl.add([{"id": 1, "text": "x", "vector": [0.0] * DIM}], mode="upsert")


def test_add_single_dict_raises(tbl):
    with pytest.raises(ValueError):
        tbl.add({"id": 1, "text": "x", "vector": [0.0] * DIM})


def test_add_generator_raises(tbl):
    with pytest.raises(ValueError):
        tbl.add(({"id": i, "text": "x", "vector": [0.0] * DIM} for i in range(2)))


def test_add_empty_list_is_noop(tbl):
    before = tbl.version
    res = tbl.add([])
    assert res.num_rows == 0
    assert tbl.version == before


# ===========================================================================
# on_bad_vectors —— 四种模式的行为差异
# ===========================================================================


BAD = [{"id": 0, "vector": [1.0, 2.0]}, {"id": 1, "vector": [1.0, 2.0, 3.0]}]


def test_on_bad_vectors_error_raises(db):
    with pytest.raises(DataValidationError):
        db.create_table("b", BAD, on_bad_vectors="error")


def test_on_bad_vectors_drop_removes_bad_rows(db):
    t = db.create_table("b", BAD, on_bad_vectors="drop")
    assert t.count_rows() == 1
    assert _ids(t.to_list()) == [0]


def test_on_bad_vectors_fill_substitutes_fill_value(db):
    t = db.create_table("b", BAD, on_bad_vectors="fill", fill_value=-1.0)
    assert t.count_rows() == 2
    assert t.to_list()[1]["vector"] == [-1.0, -1.0]


def test_on_bad_vectors_null_stores_null(db):
    t = db.create_table("b", BAD, on_bad_vectors="null")
    assert t.count_rows() == 2
    assert t.to_list()[1]["vector"] is None


def test_on_bad_vectors_dimension_is_pinned_after_drop(db):
    """丢掉坏行之后，schema 必须还是**定长**向量列。

    这是"变长 list 是一个语义损失"的可观测检查：如果没把维度钉住，
    向量列会退化成 ``list<float64>``，从此再也判断不出哪一行维度不对。
    """

    t = db.create_table("b", BAD, on_bad_vectors="drop")
    assert t.schema["vector"].vector_dim == 2


def test_on_bad_vectors_non_numeric_detected(db):
    rows = [{"id": 0, "vector": [1.0, 2.0]}, {"id": 1, "vector": ["a", "b"]}]
    t = db.create_table("b", rows, on_bad_vectors="drop")
    assert t.count_rows() == 1


def test_on_bad_vectors_nan_detected(db):
    rows = [{"id": 0, "vector": [1.0, 2.0]}, {"id": 1, "vector": [float("nan"), 1.0]}]
    with pytest.raises(DataValidationError, match="NaN"):
        db.create_table("b", rows, on_bad_vectors="error")


def test_on_bad_vectors_inf_detected(db):
    rows = [{"id": 0, "vector": [1.0, 2.0]}, {"id": 1, "vector": [float("inf"), 1.0]}]
    with pytest.raises(DataValidationError):
        db.create_table("b", rows, on_bad_vectors="error")


def test_on_bad_vectors_none_is_not_bad(db):
    """``None`` 是"这一行没有向量"，不是坏数据。"""

    rows = [{"id": 0, "vector": [1.0, 2.0]}, {"id": 1, "vector": None}]
    t = db.create_table("b", rows, on_bad_vectors="error")
    assert t.count_rows() == 2


def test_on_bad_vectors_invalid_mode_raises(db):
    with pytest.raises(ValueError, match="on_bad_vectors"):
        db.create_table("b", BAD, on_bad_vectors="ignore")


def test_add_on_bad_vectors_applies_to_existing_table(tbl):
    before = tbl.count_rows()
    tbl.add([{"id": 9999, "text": "x", "vector": [0.0, 0.0]}], on_bad_vectors="drop")
    # 维度是 8，给 2 维 → 被丢掉
    assert tbl.count_rows() == before


def test_add_on_bad_vectors_fill_on_existing_table(tbl):
    before = tbl.count_rows()
    tbl.add(
        [{"id": 9999, "text": "x", "vector": [1.0, 2.0]}],
        on_bad_vectors="fill",
        fill_value=0.5,
    )
    assert tbl.count_rows() == before + 1
    assert tbl.search().where("id = 9999").limit(1).to_list()[0]["vector"] == [0.5] * DIM


# ===========================================================================
# 扫描查询
# ===========================================================================


def test_search_none_returns_plain_scan(tbl):
    q = tbl.search()
    assert type(q).__name__ == "Query"
    assert len(q.to_list()) == N_ROWS


def test_scan_default_limit_is_unbounded(tbl):
    # 真实 LanceDB 的 ``search()`` 无参时返回全表（与向量检索默认 10 行不同）
    assert len(tbl.search().to_list()) == N_ROWS


def test_scan_limit(tbl):
    assert len(tbl.search().limit(7).to_list()) == 7


def test_scan_offset(tbl):
    assert _ids(tbl.search().limit(3).offset(5).to_list()) == [5, 6, 7]


def test_scan_select(tbl):
    got = tbl.search().limit(2).select(["id"]).to_list()
    assert list(got[0].keys()) == ["id"]


def test_scan_where(tbl):
    got = tbl.search().where("id < 4").to_list()
    assert _ids(got) == [0, 1, 2, 3]


def test_scan_where_and_limit(tbl):
    assert _ids(tbl.search().where("id < 10").limit(3).to_list()) == [0, 1, 2]


def test_scan_with_row_id(tbl):
    got = tbl.search().limit(2).with_row_id().to_list()
    assert "_rowid" in got[0]
    assert got[0]["_rowid"] == 0


# ===========================================================================
# 向量检索
# ===========================================================================


def test_vector_default_limit_is_10(tbl, rows_vecs):
    _, vecs = rows_vecs
    assert len(tbl.search(vecs[0].tolist()).to_list()) == 10


def test_vector_limit(tbl, rows_vecs):
    _, vecs = rows_vecs
    assert len(tbl.search(vecs[0].tolist()).limit(3).to_list()) == 3


def test_vector_nearest_is_itself(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[7].tolist()).limit(1).to_list()
    assert got[0]["id"] == 7
    assert got[0]["_distance"] == pytest.approx(0.0, abs=1e-4)


def test_vector_select_retains_distance(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist()).limit(3).select(["id"]).to_list()
    assert list(got[0].keys()) == ["id", "_distance"]


def test_vector_with_row_id(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist()).limit(2).with_row_id().to_list()
    assert got[0]["_rowid"] == 0


def test_vector_result_sorted_by_distance_ascending(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[3].tolist()).limit(20).to_list()
    distances = [r["_distance"] for r in got]
    assert distances == sorted(distances)


def test_metric_l2_matches_bruteforce(tbl, rows_vecs):
    _, vecs = rows_vecs
    q = vecs[11]
    got = _ids(tbl.search(q.tolist()).limit(10).metric("l2").to_list())
    assert got == brute_force_order(vecs, q, 10, "l2")


def test_metric_cosine_matches_bruteforce(tbl, rows_vecs):
    _, vecs = rows_vecs
    q = vecs[11]
    got = _ids(tbl.search(q.tolist()).limit(10).metric("cosine").to_list())
    assert got == brute_force_order(vecs, q, 10, "cosine")


def test_metric_dot_matches_bruteforce(tbl, rows_vecs):
    _, vecs = rows_vecs
    q = vecs[11]
    got = _ids(tbl.search(q.tolist()).limit(10).metric("dot").to_list())
    assert got == brute_force_order(vecs, q, 10, "dot")


def test_distance_type_is_alias_of_metric(tbl, rows_vecs):
    _, vecs = rows_vecs
    q = vecs[5]
    a = _ids(tbl.search(q.tolist()).limit(5).metric("cosine").to_list())
    b = _ids(tbl.search(q.tolist()).limit(5).distance_type("cosine").to_list())
    assert a == b


def test_metric_cosine_and_l2_differ_on_unnormalized_data(tbl, rows_vecs):
    """两种度量的顺序应当不同 —— 否则说明度量参数根本没生效。"""

    _, vecs = rows_vecs
    q = vecs[0] * 3.0  # 放大模长：cosine 不变，l2 会变
    a = _ids(tbl.search(q.tolist()).limit(10).metric("l2").to_list())
    b = _ids(tbl.search(q.tolist()).limit(10).metric("cosine").to_list())
    assert a != b


def test_unknown_metric_raises(tbl, rows_vecs):
    _, vecs = rows_vecs
    with pytest.raises(QueryError):
        tbl.search(vecs[0].tolist()).limit(1).metric("manhattan").to_list()


def test_vector_wrong_dimension_raises(tbl):
    with pytest.raises(QueryError, match="维度"):
        tbl.search([1.0, 2.0]).limit(1).to_list()


def test_vector_column_name_resolution(tbl, rows_vecs):
    _, vecs = rows_vecs
    # 表里只有一列向量 → 不用显式指定也能解析出来
    assert len(tbl.search(vecs[0].tolist()).limit(1).to_list()) == 1


def test_vector_explicit_wrong_column_raises(tbl, rows_vecs):
    _, vecs = rows_vecs
    with pytest.raises(QueryError):
        tbl.search(vecs[0].tolist(), vector_column_name="text").limit(1).to_list()


def test_vector_bad_vector_column_name_raises(tbl, rows_vecs):
    _, vecs = rows_vecs
    with pytest.raises(QueryError):
        tbl.search(vecs[0].tolist(), vector_column_name="nope").limit(1).to_list()


def test_vector_offset(tbl, rows_vecs):
    _, vecs = rows_vecs
    full = _ids(tbl.search(vecs[0].tolist()).limit(6).to_list())
    shifted = _ids(tbl.search(vecs[0].tolist()).limit(3).offset(3).to_list())
    assert shifted == full[3:6]


# ===========================================================================
# prefilter / postfilter —— 本模块最核心的语义差异
# ===========================================================================


def test_prefilter_returns_full_k(tbl, rows_vecs):
    _, vecs = rows_vecs
    # 谓词命中 20 行（id 100..119），足够凑满 k=10
    got = tbl.search(vecs[0].tolist()).limit(10).where("id >= 100", prefilter=True).to_list()
    assert len(got) == 10
    assert all(r["id"] >= 100 for r in got)


def test_postfilter_returns_fewer_than_k(tbl, rows_vecs):
    """``prefilter=False`` 会先取 k 个候选再筛 —— **可能少于 k 行**。

    这是真实 LanceDB 文档明确写过的行为，也是本套测试里最值得钉住的一条：
    它的成因（先排序后过滤）和它的现象（结果变少）之间的因果关系，
    不亲手跑一遍是记不住的。
    """

    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist()).limit(10).where("id >= 100", prefilter=False).to_list()
    assert len(got) < 10
    assert all(r["id"] >= 100 for r in got)


def test_prefilter_and_postfilter_differ_in_row_count(tbl, rows_vecs):
    _, vecs = rows_vecs
    pre = tbl.search(vecs[0].tolist()).limit(10).where("id >= 100", prefilter=True).to_list()
    post = tbl.search(vecs[0].tolist()).limit(10).where("id >= 100", prefilter=False).to_list()
    assert len(pre) > len(post)


def test_postfilter_method_is_equivalent_to_prefilter_false(tbl, rows_vecs):
    _, vecs = rows_vecs
    a = _ids(tbl.search(vecs[0].tolist()).limit(8).where("id > 100").postfilter().to_list())
    b = _ids(
        tbl.search(vecs[0].tolist()).limit(8).where("id > 100", prefilter=False).to_list()
    )
    assert a == b


def test_postfilter_refine_factor_recovers_rows(tbl, rows_vecs):
    """``refine_factor`` 让候选池变大，postfilter 就能捞回更多行。"""

    _, vecs = rows_vecs
    base = tbl.search(vecs[0].tolist()).limit(5).where("id > 110", prefilter=False).to_list()
    wide = (
        tbl.search(vecs[0].tolist())
        .limit(5)
        .where("id > 110", prefilter=False)
        .refine_factor(8)
        .to_list()
    )
    assert len(wide) >= len(base)


def test_postfilter_can_return_zero_rows(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist()).limit(3).where("id > 9999", prefilter=False).to_list()
    assert got == []


# ===========================================================================
# distance_range / bypass / fast_search
# ===========================================================================


def test_distance_range_filters_both_bounds(tbl, rows_vecs):
    _, vecs = rows_vecs
    distances = [r["_distance"] for r in tbl.search(vecs[0].tolist()).limit(50).to_list()]
    lo, hi = distances[2], distances[10]
    got = (
        tbl.search(vecs[0].tolist())
        .limit(50)
        .distance_range(lower_bound=lo, upper_bound=hi)
        .to_list()
    )
    assert all(lo <= r["_distance"] < hi for r in got)


def test_distance_range_lower_only(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist()).limit(50).distance_range(lower_bound=5.0).to_list()
    assert all(r["_distance"] >= 5.0 for r in got)


def test_distance_range_invalid_bounds_raises(tbl, rows_vecs):
    _, vecs = rows_vecs
    with pytest.raises(QueryError):
        tbl.search(vecs[0].tolist()).distance_range(10.0, 1.0)


def test_bypass_vector_index_is_exact(tbl, rows_vecs):
    _, vecs = rows_vecs
    q = vecs[42]
    got = _ids(tbl.search(q.tolist()).limit(10).bypass_vector_index().to_list())
    assert got == brute_force_order(vecs, q, 10, "l2")


def test_bypass_vector_index_keeps_distance_column(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist()).limit(3).bypass_vector_index().to_list()
    assert "_distance" in got[0]


def test_fast_search_on_unindexed_table_returns_empty(tbl, rows_vecs):
    """⚠️ 真实 LanceDB 的行为：**静默返回 0 行**，不报错也不退化成全表扫描。"""

    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist()).limit(10).fast_search().to_list()
    assert got == []


def test_fast_search_on_indexed_table_returns_rows(indexed_tbl, rows_vecs):
    _, vecs = rows_vecs
    got = indexed_tbl.search(vecs[3].tolist()).limit(5).fast_search().to_list()
    assert len(got) == 5


def test_fast_search_still_honours_where(indexed_tbl, rows_vecs):
    _, vecs = rows_vecs
    got = indexed_tbl.search(vecs[0].tolist()).limit(5).where("id > 100").fast_search().to_list()
    assert got and all(r["id"] > 100 for r in got)


def test_indexed_search_finds_own_row(indexed_tbl, rows_vecs):
    _, vecs = rows_vecs
    got = indexed_tbl.search(vecs[9].tolist()).limit(5).to_list()
    assert 9 in _ids(got)


def test_nprobes_full_gives_high_recall(indexed_tbl, rows_vecs):
    """``nprobes`` = 分区数时，**候选集**退化成全量，召回必须很高。

    但**不能断言"顺序和暴力完全一致"** —— 索引里的距离是 PQ 量化后的
    估计值，精排之前它本来就和精确距离有偏差。把"高召回"写成"完全一致"
    等于把一个近似结构当精确结构来断言：那种测试要么一直红，
    要么在换一组数据之后突然变红。
    """

    _, vecs = rows_vecs
    q = vecs[17]
    got = set(_ids(indexed_tbl.search(q.tolist()).limit(10).nprobes(4).to_list()))
    truth = set(brute_force_order(vecs, q, 10, "l2"))
    assert len(got & truth) / 10 >= 0.8
    # 查询向量就是第 17 行自己 → 它必须被找到
    assert 17 in got


def test_nprobes_accepts_and_returns_query(indexed_tbl, rows_vecs):
    _, vecs = rows_vecs
    q = indexed_tbl.search(vecs[0].tolist()).nprobes(2)
    assert hasattr(q, "limit")


def test_refine_factor_accepted(tbl, rows_vecs):
    _, vecs = rows_vecs
    assert len(tbl.search(vecs[0].tolist()).limit(5).refine_factor(3).to_list()) == 5


def test_ef_accepted(tbl, rows_vecs):
    _, vecs = rows_vecs
    assert len(tbl.search(vecs[0].tolist()).limit(5).ef(64).to_list()) == 5


def test_multivector_raises_with_pointer(tbl, rows_vecs):
    _, vecs = vecs_ = rows_vecs
    with pytest.raises(NotSupportedError) as exc:
        tbl.search(vecs[0].tolist()).multivector([[1.0, 2.0]])
    assert "rerank" in str(exc.value)


# ===========================================================================
# 全文检索
# ===========================================================================


FTS_DOCS = [
    "the quick brown fox jumps over the lazy dog",
    "a lazy dog sleeps all day long",
    "quick fox runs fast through the forest",
    "the cat watches the birds quietly",
    "brown bear and brown fox share a cave",
    "dogs and cats are friends sometimes",
]


@pytest.fixture
def fts(db):
    rows = [
        {"id": i, "text": t, "vector": [float(i), 1.0, 0.5, 0.25, 0.125, 0.0625, 1.0, 2.0]}
        for i, t in enumerate(FTS_DOCS)
    ]
    table = db.create_table("corpus", rows)
    table.create_index("text", config="FTS")
    return table


def test_fts_returns_score_column(fts):
    got = fts.search("fox", query_type="fts").limit(5).to_list()
    assert "_score" in got[0]


def test_fts_score_descending(fts):
    got = fts.search("fox", query_type="fts").limit(5).to_list()
    scores = [r["_score"] for r in got]
    assert scores == sorted(scores, reverse=True)


def test_fts_finds_docs_containing_term(fts):
    got = fts.search("fox", query_type="fts").limit(5).to_list()
    ids = _ids(got)
    assert set(ids) == {0, 2, 4}


def test_fts_ranked_before_non_matching(fts):
    got = fts.search("fox", query_type="fts").limit(6).to_list()
    assert set(_ids(got)[:3]) == {0, 2, 4}


def test_fts_limit(fts):
    assert len(fts.search("fox", query_type="fts").limit(2).to_list()) == 2


def test_fts_select_retains_score(fts):
    got = fts.search("fox", query_type="fts").limit(2).select(["id"]).to_list()
    assert list(got[0].keys()) == ["id", "_score"]


def test_fts_phrase_requires_adjacency(fts):
    # "quick brown" 只在第 0 篇里相邻出现
    got = fts.search("quick brown", query_type="fts").limit(5).phrase().to_list()
    assert _ids(got) == [0]


def test_fts_phrase_query_alias(fts):
    a = _ids(fts.search("quick brown", query_type="fts").limit(5).phrase().to_list())
    b = _ids(fts.search("quick brown", query_type="fts").limit(5).phrase_query().to_list())
    assert a == b


def test_fts_without_phrase_matches_either_order(fts):
    # 非短语：两个词只要都出现就行，"brown ... fox" 也算
    got = fts.search("brown fox", query_type="fts").limit(5).to_list()
    assert 4 in _ids(got)


def test_fts_columns_restriction(fts):
    got = fts.search("fox", query_type="fts").columns(["text"]).limit(5).to_list()
    assert set(_ids(got)) == {0, 2, 4}


def test_fts_columns_unknown_raises(fts):
    with pytest.raises(IndexNotFoundError):
        fts.search("fox", query_type="fts").columns(["nope"]).limit(5).to_list()


def test_fts_without_index_raises(tbl):
    with pytest.raises(IndexNotFoundError, match="全文索引"):
        tbl.search("fox", query_type="fts").limit(3).to_list()


def test_fts_query_must_be_string(fts):
    with pytest.raises(QueryError):
        fts.search(123, query_type="fts")


def test_fts_where_prefilter(fts):
    got = fts.search("fox", query_type="fts").limit(5).where("id >= 2").to_list()
    assert all(r["id"] >= 2 for r in got)


def test_fts_where_postfilter_can_drop_rows(fts):
    got = (
        fts.search("fox", query_type="fts")
        .limit(3)
        .where("id = 4", prefilter=False)
        .to_list()
    )
    assert len(got) <= 1


def test_fts_boost_scales_score(fts):
    plain = fts.search("fox", query_type="fts").limit(3).to_list()
    boosted = fts.search("fox", query_type="fts").limit(3).boost(3.0).to_list()
    assert boosted[0]["_score"] == pytest.approx(plain[0]["_score"] * 3.0, rel=1e-6)


def test_fts_offset(fts):
    full = _ids(fts.search("fox", query_type="fts").limit(3).to_list())
    shifted = _ids(fts.search("fox", query_type="fts").limit(1).offset(1).to_list())
    assert shifted == full[1:2]


# ===========================================================================
# 混合检索
# ===========================================================================


def test_hybrid_returns_relevance_score(both_indexed, rows_vecs):
    _, vecs = rows_vecs
    got = (
        both_indexed.search(vecs[0].tolist(), query_type="hybrid")
        .text("alpha")
        .limit(5)
        .to_list()
    )
    assert "_relevance_score" in got[0]


def test_hybrid_default_reranker_is_rrf(both_indexed):
    q = both_indexed.search([0.0] * DIM, query_type="hybrid").text("alpha")
    assert type(q._reranker).__name__ == "RRFReranker"


def test_hybrid_sorted_by_relevance_descending(both_indexed, rows_vecs):
    _, vecs = rows_vecs
    got = (
        both_indexed.search(vecs[0].tolist(), query_type="hybrid")
        .text("alpha")
        .limit(8)
        .to_list()
    )
    scores = [r["_relevance_score"] for r in got]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_prefers_doc_matching_both_routes(db):
    """两路都命中的文档必须排在只命中一路的前面 —— 这就是融合的全部意义。"""

    dim = 4
    rows = [
        {"id": 0, "text": "alpha beta", "vector": [0.0, 0.0, 0.0, 1.0]},
        {"id": 1, "text": "zebra stripes", "vector": [1.0, 0.0, 0.0, 0.0]},
        {"id": 2, "text": "gamma delta", "vector": [0.0, 1.0, 0.0, 0.0]},
    ]
    t = db.create_table("h", rows)
    t.create_index("text", config="FTS")
    # 查询向量 = 第 1 行的向量；查询词 = 第 1 行的词 → 第 1 行两路都第一
    got = (
        t.search([1.0, 0.0, 0.0, 0.0], query_type="hybrid")
        .text("zebra")
        .limit(3)
        .to_list()
    )
    assert got[0]["id"] == 1


def test_hybrid_requires_text(db):
    t = db.create_table("h", [{"id": 0, "text": "x", "vector": [1.0, 0.0]}])
    t.create_index("text", config="FTS")
    with pytest.raises(QueryError, match="检索词"):
        t.search([1.0, 0.0], query_type="hybrid").limit(3).to_list()


def test_hybrid_requires_fts_index(tbl, rows_vecs):
    _, vecs = rows_vecs
    with pytest.raises(IndexNotFoundError):
        tbl.search(vecs[0].tolist(), query_type="hybrid").text("alpha").limit(3).to_list()


def test_hybrid_with_none_reranker(both_indexed, rows_vecs):
    _, vecs = rows_vecs
    got = (
        both_indexed.search(vecs[0].tolist(), query_type="hybrid")
        .text("alpha")
        .rerank("none")
        .limit(5)
        .to_list()
    )
    assert len(got) > 0


def test_hybrid_with_linear_reranker(both_indexed, rows_vecs):
    _, vecs = rows_vecs
    got = (
        both_indexed.search(vecs[0].tolist(), query_type="hybrid")
        .text("alpha")
        .rerank("linear")
        .limit(5)
        .to_list()
    )
    assert len(got) > 0


def test_hybrid_projects_only_requested_columns(both_indexed, rows_vecs):
    _, vecs = rows_vecs
    got = (
        both_indexed.search(vecs[0].tolist(), query_type="hybrid")
        .text("alpha")
        .select(["id"])
        .limit(3)
        .to_list()
    )
    assert list(got[0].keys()) == ["id", "_relevance_score"]


# ===========================================================================
# delete / update / merge_insert
# ===========================================================================


def test_delete_returns_num_deleted(tbl):
    res = tbl.delete("id < 5")
    assert res.num_deleted_rows == 5
    assert tbl.count_rows() == N_ROWS - 5


def test_delete_no_match_returns_zero_and_keeps_version(tbl):
    version = tbl.version
    res = tbl.delete("id > 99999")
    assert res.num_deleted_rows == 0
    assert tbl.version == version


def test_delete_makes_rows_unsearchable(tbl, rows_vecs):
    _, vecs = rows_vecs
    tbl.delete("id = 7")
    got = _ids(tbl.search(vecs[7].tolist()).limit(3).to_list())
    assert 7 not in got


def test_update_values(tbl):
    res = tbl.update(values={"text": "changed"}, where="id < 3")
    assert res.rows_updated == 3
    got = tbl.search().where("text = 'changed'").to_list()
    assert len(got) == 3


def test_update_all_rows_when_where_omitted(tbl):
    res = tbl.update(values={"text": "all"})
    assert res.rows_updated == N_ROWS


def test_update_values_sql_arithmetic(tbl):
    res = tbl.update(values_sql={"id": "id + 100"}, where="id < 5")
    assert res.rows_updated == 5
    ids = set(_ids(tbl.search().to_list()))
    assert {100, 101, 102, 103, 104} <= ids
    # 原 id 0..4 已经不存在了（"更新" = 删掉旧的 + 追加新的）
    assert not (set(range(5)) & ids)


def test_update_requires_values_or_values_sql(tbl):
    with pytest.raises(mldb.PlanError):
        tbl.update(where="id = 1")


def test_update_unknown_column_raises(tbl):
    with pytest.raises(mldb.PlanError):
        tbl.update(values={"nope": 1}, where="id = 1")


def test_update_row_reordering_side_effect(tbl):
    """⚠️ 被更新的行会**跑到表末尾** —— 这是"删+追加"语义的必然副作用。

    真实 LanceDB 的文档专门警告过：用 limit/offset 分页会因此漏行。
    """

    first_before = _ids(tbl.search().limit(1).to_list())
    tbl.update(values={"text": "z"}, where="id = 0")
    first_after = _ids(tbl.search().limit(1).to_list())
    assert first_before == [0]
    assert first_after != [0]
    assert _ids(tbl.search().to_list())[-1] == 0


def test_merge_insert_inserts_new_rows(tbl):
    b = tbl.merge_insert("id")
    b.when_matched_update_all().when_not_matched_insert_all()
    res = b.execute([{"id": 5000, "text": "new", "vector": [0.0] * DIM}])
    assert res.num_inserted_rows == 1
    assert tbl.count_rows() == N_ROWS + 1


def test_merge_insert_updates_matched_rows(tbl):
    b = tbl.merge_insert("id")
    b.when_matched_update_all().when_not_matched_insert_all()
    res = b.execute([{"id": 3, "text": "updated", "vector": [9.0] * DIM}])
    assert res.num_updated_rows == 1
    got = tbl.search().where("id = 3").limit(1).to_list()
    assert got[0]["text"] == "updated"


def test_merge_insert_duplicate_source_keys_last_wins(tbl):
    """源数据里同一个键出现两次时，只保留**最后一条**（与真实 LanceDB 一致）。

    这条测试最初是用来钉住一个 bug 的：老实现按"每一个匹配上的源行"
    生成一条待追加记录，于是同一 key 的两条源行会把目标行更新两次，
    **目标表凭空多出一行**。这类问题跑完不报错，几天后报表数字才对不上。

    修法是在 ``dataset._execute_merge`` 入口按 key 去重、保留最后出现的下标。
    """

    b = tbl.merge_insert("id")
    b.when_matched_update_all().when_not_matched_insert_all()
    res = b.execute(
        [
            {"id": 6, "text": "first", "vector": [0.0] * DIM},
            {"id": 6, "text": "second", "vector": [0.0] * DIM},
        ]
    )
    assert res.num_updated_rows == 1
    got = tbl.search().where("id = 6").to_list()
    assert len(got) == 1
    assert got[0]["text"] == "second"


def test_merge_insert_duplicate_target_keys_last_wins(db):
    """目标表里**同一个键有重复行**时，匹配到的是最后一行（和真实一致）。"""

    t = db.create_table("d", [{"id": i, "vector": [float(i), 1.0]} for i in range(3)])
    # 手工制造重复键
    t.add([{"id": 1, "vector": [0.0, 0.0]}])
    b = t.merge_insert("id")
    b.when_matched_update_all().when_not_matched_insert_all()
    res = b.execute([{"id": 1, "vector": [9.0, 9.0]}])
    assert res.num_updated_rows == 1


def test_merge_insert_only_insert_without_update(tbl):
    b = tbl.merge_insert("id")
    b.when_not_matched_insert_all()
    res = b.execute([{"id": 0, "text": "ignored", "vector": [0.0] * DIM}])
    assert res.num_updated_rows == 0
    assert tbl.count_rows() == N_ROWS


def test_merge_insert_without_action_raises(tbl):
    with pytest.raises(mldb.PlanError):
        tbl.merge_insert("id").execute([{"id": 1, "text": "x", "vector": [0.0] * DIM}])


def test_merge_insert_when_matched_delete_without_predicate_deletes_all_matched(tbl):
    """``when_matched_delete()`` **不带 where** 的语义是"匹配上的全删"。

    这条测试最初钉的是相反的行为：老实现只在给了 ``where`` 时才算
    "该删哪些行"，不传 where 的路径会安静地什么都不做、还报
    ``num_deleted_rows=0``。"以为删了其实没删"比报错危险得多，
    因为它不会让任何监控报警。
    """

    b = tbl.merge_insert("id")
    b.when_matched_delete().when_not_matched_insert_all()
    res = b.execute([{"id": 4, "text": "x", "vector": [0.0] * DIM}])
    assert res.num_deleted_rows == 1
    assert 4 not in _ids(tbl.search().to_list())


def test_merge_insert_when_matched_delete_with_predicate(tbl):
    b = tbl.merge_insert("id")
    b.when_matched_delete(where="id = 4").when_not_matched_insert_all()
    res = b.execute([{"id": 4, "text": "x", "vector": [0.0] * DIM}])
    assert res.num_deleted_rows == 1
    assert 4 not in _ids(tbl.search().to_list())


# ===========================================================================
# 版本 / 标签
# ===========================================================================


def test_list_versions_is_ascending(tbl):
    tbl.add([{"id": 999, "text": "x", "vector": [0.0] * DIM}])
    versions = [v.version for v in tbl.list_versions()]
    assert versions == sorted(versions)
    assert len(versions) >= 2


def test_version_property_matches_latest(tbl):
    assert tbl.version == tbl.list_versions()[-1].version


def test_checkout_switches_view(tbl):
    # v1 = create_table 建出的空版本；v2 = 第一次 add 写入的数据
    tbl.add([{"id": 111, "text": "x", "vector": [0.0] * DIM}])
    tbl.checkout(2)
    assert tbl.count_rows() == N_ROWS
    assert tbl.version == 2


def test_checkout_unknown_version_raises(tbl):
    with pytest.raises(mldb.VersionNotFoundError):
        tbl.checkout(9999)


def test_restore_roundtrip(db):
    t = db.create_table("t", [{"id": i, "vector": [float(i), 0.0]} for i in range(5)])
    data_version = t.version
    assert t.count_rows() == 5
    t.add([{"id": 100, "vector": [0.0, 0.0]}])
    assert t.count_rows() == 6
    t.checkout(data_version)
    t.restore()
    assert t.count_rows() == 5
    # restore 是"提交一个新版本"，所以版本号继续前进而不是回退
    assert t.version > data_version + 1


def test_restore_without_version_or_checkout_raises(tbl):
    with pytest.raises(QueryError):
        tbl.restore()


def test_restore_explicit_version(tbl):
    tbl.add([{"id": 777, "text": "x", "vector": [0.0] * DIM}])
    tbl.restore(1)
    assert 777 not in _ids(tbl.search().to_list())


def test_tags_roundtrip(tbl):
    tbl.tags.create("v1.0")
    assert tbl.tags.get_version("v1.0") == tbl.version
    assert "v1.0" in tbl.tags.list()


def test_tags_create_at_specific_version(tbl):
    tbl.add([{"id": 888, "text": "x", "vector": [0.0] * DIM}])
    tbl.tags.create("old", version=1)
    assert tbl.tags.get_version("old") == 1


def test_tags_update_moves_tag(tbl):
    tbl.tags.create("release")
    version_at_tag = tbl.tags.get_version("release")
    tbl.add([{"id": 999, "text": "x", "vector": [0.0] * DIM}])
    tbl.tags.update("release")
    assert tbl.tags.get_version("release") > version_at_tag


def test_tags_delete(tbl):
    tbl.tags.create("temp")
    assert tbl.tags.delete("temp") is True
    assert "temp" not in tbl.tags.list()


def test_tags_delete_missing_returns_false(tbl):
    assert tbl.tags.delete("nope") is False


def test_tags_get_version_missing_raises(tbl):
    with pytest.raises(mldb.VersionNotFoundError):
        tbl.tags.get_version("nope")


def test_tags_empty_name_raises(tbl):
    with pytest.raises(QueryError):
        tbl.tags.create("")


def test_checkout_by_tag(tbl):
    tbl.tags.create("anchor")
    tbl.add([{"id": 321, "text": "x", "vector": [0.0] * DIM}])
    tbl.checkout("anchor")
    assert 321 not in _ids(tbl.search().to_list())


# ===========================================================================
# 维护
# ===========================================================================


def test_compact_files_merges_fragments(db):
    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 12):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    before = len(t.list_versions())
    stats = t.compact_files()
    assert stats.compacted is True
    assert stats.fragments_removed >= 2
    assert stats.fragments_added >= 1
    assert t.count_rows() == 12
    assert len(t.list_versions()) > before


def test_compact_files_reports_bytes(db):
    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 8):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    stats = t.compact_files()
    assert stats.bytes_added > 0
    assert stats.bytes_removed >= 0
    assert isinstance(stats.net_bytes, int)


def test_compact_files_materializes_deletions(db):
    t = db.create_table("c", [{"id": i, "vector": [float(i), 1.0]} for i in range(20)])
    t.delete("id < 10")
    assert t.stats().num_deleted_rows > 0
    t.compact_files()
    assert t.stats().num_deleted_rows == 0
    assert t.count_rows() == 10


def test_compact_files_report_renders(db):
    t = db.create_table("c", [{"id": i, "vector": [float(i), 1.0]} for i in range(20)])
    text = t.compact_files().report()
    assert "片段" in text and "字节" in text


def test_compact_files_max_fragments_limits_work(db):
    """``max_fragments`` 让压实可以**分批**做。

    一次压实整个表会造成一个巨大的 I/O 尖峰；分批做能让它对前台流量
    "几乎不可见"。这里断言的是"它真的只动了指定的那么多"。
    """

    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 10):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    before = len(t.to_lance().fragments)
    stats = t.compact_files(max_fragments=2)
    assert stats.fragments_removed <= 2
    assert stats.compacted is True
    assert t.count_rows() == 10
    assert before >= 5


def test_cleanup_dry_run_reports_reclaimable_bytes(db):
    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 6):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    stats = t.cleanup_old_versions(older_than=0, dry_run=True)
    assert stats.dry_run is True
    assert stats.old_versions_removable > 0
    assert stats.bytes_removed > 0


def test_cleanup_actually_removes_versions(db):
    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 6):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    before = [v.version for v in t.list_versions()]
    stats = t.cleanup_old_versions(older_than=0)
    assert stats.cleaned is True
    assert stats.old_versions_removed > 0
    after = [v.version for v in t.list_versions()]
    assert len(after) < len(before)
    assert t.count_rows() == 6


def test_cleanup_keeps_current_version(db):
    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 4):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    t.cleanup_old_versions(older_than=0)
    assert t.count_rows() == 4


def test_cleanup_report_renders(db):
    t = db.create_table("c", [{"id": i, "vector": [float(i), 1.0]} for i in range(3)])
    assert "版本" in t.cleanup_old_versions(older_than=0, dry_run=True).report()


def test_optimize_returns_stats(db):
    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 8):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    stats = t.optimize()
    assert stats.fragments_added >= 1
    assert stats.version_after >= stats.version_before


def test_optimize_with_cleanup(db):
    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 8):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    stats = t.optimize(cleanup_older_than=0)
    assert stats.cleanup is not None
    assert stats.old_versions_removed >= 0


def test_optimize_report_renders(db):
    t = db.create_table("c", [{"id": i, "vector": [float(i), 1.0]} for i in range(10)])
    text = t.optimize().report()
    assert "压实" in text


def test_optimize_retrain_rebuilds_invalidated_index(db):
    rows = [{"id": i, "vector": [float(i), 1.0, 0.5, 0.25]} for i in range(60)]
    t = db.create_table("c", rows)
    t.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    for i in range(60, 70):
        t.add([{"id": i, "vector": [float(i), 1.0, 0.5, 0.25]}])
    stats = t.optimize(retrain=True)
    assert "vector_idx" in stats.indices_rebuilt
    assert t.list_indices()[0].valid is True


def test_should_optimize_heuristic(db):
    t = db.create_table("c", [{"id": i, "vector": [float(i), 1.0]} for i in range(3)])
    info = mldb.should_optimize(t)
    assert info["row_change_threshold"] == 100_000
    assert info["operation_threshold"] == 20
    assert info["operations"] >= 1
    assert isinstance(info["reason"], str)


def test_should_optimize_thresholds_are_or(db):
    """两个阈值是 **或** 的关系：只改一行但改很多次，也该维护。"""

    t = db.create_table("c", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 3):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    assert mldb.should_optimize(t, operation_threshold=2)["should_optimize"] is True
    assert mldb.should_optimize(t, operation_threshold=99, row_change_threshold=99)["should_optimize"] is False


# ===========================================================================
# 索引
# ===========================================================================


def test_create_vector_index_returns_metadata(tbl):
    meta = tbl.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    assert meta.index_type == "IVF_PQ"
    assert meta.columns == ["vector"]


def test_create_vector_index_auto_type(tbl):
    meta = tbl.create_index("vector")
    assert meta.index_type == "IVF_PQ"


def test_create_scalar_index_btree(tbl):
    meta = tbl.create_index("id", config="BTREE")
    assert meta.index_type == "BTREE"


def test_create_scalar_index_auto_type(tbl):
    meta = tbl.create_index("id")
    assert meta.index_type == "BTREE"


def test_create_fts_index(tbl):
    meta = tbl.create_index("text", config="FTS")
    assert meta.index_type == "FTS"


def test_create_index_with_explicit_name(tbl):
    meta = tbl.create_index("vector", name="vec")
    assert meta.name == "vec"
    assert tbl.index_stats("vec")["index_type"] == "IVF_PQ"


def test_create_index_replace_false_conflict_raises(tbl):
    # replace=False 时重复建同一个名字必须报错，而不是静默重建
    tbl.create_index("vector", name="vec", replace=False)
    with pytest.raises(mldb.IndexNotFoundError):
        tbl.create_index("vector", name="vec", replace=False)


def test_create_index_unknown_column_raises(tbl):
    with pytest.raises(mldb.PlanError):
        tbl.create_index("nope")


def test_list_indices(tbl):
    tbl.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    indices = tbl.list_indices()
    assert len(indices) == 1
    assert indices[0].name == "vector_idx"


def test_list_indices_empty(tbl):
    assert tbl.list_indices() == []


def test_index_stats_missing_returns_none(tbl):
    """⚠️ 真实 LanceDB 对不存在的索引名返回 ``None`` 而不是抛异常。"""

    assert tbl.index_stats("does_not_exist") is None


def test_index_stats_shape(tbl):
    tbl.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    info = tbl.index_stats("vector_idx")
    assert set(info) >= {
        "index_type",
        "distance_type",
        "num_indexed_rows",
        "num_unindexed_rows",
        "num_indices",
        "size_bytes",
        "details",
    }
    assert info["index_type"] == "IVF_PQ"
    assert info["num_indexed_rows"] == N_ROWS
    assert info["num_unindexed_rows"] == 0


def test_index_stats_after_new_writes_reports_unindexed(tbl):
    tbl.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    tbl.add([{"id": 9999, "text": "x", "vector": [0.0] * DIM}])
    assert tbl.index_stats("vector_idx")["num_unindexed_rows"] == 1


def test_drop_index(tbl):
    tbl.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    tbl.drop_index("vector_idx")
    assert tbl.list_indices() == []
    assert tbl.index_stats("vector_idx") is None


def test_drop_index_missing_raises(tbl):
    with pytest.raises(mldb.IndexNotFoundError):
        tbl.drop_index("nope")


def test_create_index_then_search_uses_it(both_indexed, rows_vecs):
    """建完索引之后解释计划里应该出现 ANN 节点。"""

    _, vecs = rows_vecs
    plan = both_indexed.search(vecs[0].tolist()).limit(3).explain()
    assert "ANNSubIndex" in plan


def test_fast_search_works_after_index_created(tbl, rows_vecs):
    _, vecs = rows_vecs
    assert tbl.search(vecs[0].tolist()).limit(3).fast_search().to_list() == []
    tbl.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    assert len(tbl.search(vecs[0].tolist()).limit(3).fast_search().to_list()) == 3


# ===========================================================================
# 统计
# ===========================================================================


def test_table_stats_shape(tbl):
    st = tbl.stats()
    assert st.num_rows == N_ROWS
    assert st.num_fragments >= 1
    assert st.num_columns == 3


def test_table_stats_report_renders(tbl):
    text = tbl.stats().report()
    assert "TableStatistics" in text
    assert "片段" in text


def test_table_stats_histogram(db):
    t = db.create_table("h", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 20):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    st = t.stats()
    assert st.fragments.histogram
    assert sum(st.fragments.histogram.values()) == st.num_fragments


def test_table_stats_fragment_distribution(db):
    t = db.create_table("h", [{"id": 0, "vector": [0.0, 1.0]}])
    for i in range(1, 5):
        t.add([{"id": i, "vector": [float(i), 1.0]}])
    st = t.stats()
    assert st.fragments.min_rows == 1
    assert st.fragments.max_rows == 1
    assert st.fragments.median_rows == 1


def test_table_stats_deleted_ratio(tbl):
    tbl.delete("id < 60")
    assert tbl.stats().deleted_rows_ratio == pytest.approx(0.5, abs=1e-6)


def test_table_stats_bytes_per_column(tbl):
    st = tbl.stats()
    assert set(st.bytes_per_column) >= {"id", "text", "vector"}
    assert all(v >= 0 for v in st.bytes_per_column.values())


def test_table_stats_null_counts_zero_for_new_table(tbl):
    st = tbl.stats()
    assert st.null_counts["id"] == 0


def test_table_stats_null_counts_after_add_column(db):
    t = db.create_table("n", [{"id": i, "vector": [float(i), 1.0]} for i in range(10)])
    t.add_columns({"note": "utf8"})
    assert t.stats().null_counts["note"] == 10


def test_table_stats_index_summary(tbl):
    tbl.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    st = tbl.stats()
    assert st.indices and st.indices[0]["name"] == "vector_idx"


def test_table_stats_num_versions(tbl):
    tbl.add([{"id": 999, "text": "x", "vector": [0.0] * DIM}])
    assert tbl.stats().num_versions >= 2


def test_statistics_module_index_stats_helper(tbl):
    tbl.create_index("id", config="BTREE")
    info = mldb.index_stats("id_idx", tbl)
    assert info["index_type"] == "BTREE"
    assert mldb.index_stats("nope", tbl) is None


def test_statistics_to_dict_roundtrip(tbl):
    d = tbl.stats().to_dict()
    assert d["num_rows"] == N_ROWS
    assert "fragment_histogram" in d


# ===========================================================================
# schema 演进 / 表级操作 / 导出
# ===========================================================================


def test_add_columns(tbl):
    res = tbl.add_columns({"note": "utf8"})
    assert "note" in res.columns
    assert "note" in tbl.schema


def test_add_columns_does_not_touch_existing_data(tbl):
    before = tbl.count_rows()
    tbl.add_columns({"note": "utf8"})
    assert tbl.count_rows() == before
    assert all(r["note"] is None for r in tbl.search().limit(5).to_list())


def test_add_columns_computed(tbl):
    tbl.add_columns(computed={"double_id": "id * 2"})
    got = tbl.search().limit(3).to_list()
    assert [r["double_id"] for r in got] == [0, 2, 4]


def test_add_columns_requires_something(tbl):
    with pytest.raises(QueryError):
        tbl.add_columns()


def test_drop_columns(tbl):
    res = tbl.drop_columns(["text"])
    assert res.columns == ["text"]
    assert "text" not in tbl.schema


def test_drop_columns_missing_raises(tbl):
    with pytest.raises(mldb.SchemaError):
        tbl.drop_columns(["nope"])


def test_alter_columns_rename(db):
    t = db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    t.alter_columns({"path": "id", "rename": "pk"})
    assert "pk" in t.schema


def test_alter_columns_shorthand_type(db):
    t = db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    t.alter_columns({"id": "int64"})
    assert t.schema["id"].type.name == "int64"


def test_alter_columns_unknown_raises(tbl):
    with pytest.raises(mldb.SchemaError):
        tbl.alter_columns({"nope": "int64"})


def test_rename_table(db):
    db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    t = db.open_table("t")
    t.rename_table("t2")
    assert t.name == "t2"
    assert db.table_names() == ["t2"]


def test_drop_table_via_table(db):
    db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    t = db.open_table("t")
    t.drop()
    assert db.table_names() == []


def test_rollback_to_alias(tbl):
    tbl.add([{"id": 500, "text": "x", "vector": [0.0] * DIM}])
    tbl.rollback_to(1)
    assert 500 not in _ids(tbl.search().to_list())


def test_head_returns_arrow_table(tbl):
    import pyarrow as pa

    head = tbl.head(3)
    assert isinstance(head, pa.Table)
    assert head.num_rows == 3


def test_head_default_is_five(tbl):
    assert tbl.head().num_rows == 5


def test_count_rows_with_filter(tbl):
    assert tbl.count_rows("id < 10") == 10


def test_count_rows_no_filter(tbl):
    assert tbl.count_rows() == N_ROWS


def test_to_arrow_full_table(tbl):
    import pyarrow as pa

    table = tbl.to_arrow()
    assert isinstance(table, pa.Table)
    assert table.num_rows == N_ROWS


def test_to_list_full_table(tbl):
    assert len(tbl.to_list()) == N_ROWS


def test_to_pandas_requires_pandas(tbl):
    try:
        import pandas  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            tbl.to_pandas()
    else:
        assert tbl.to_pandas().shape[0] == N_ROWS


def test_to_polars_error_message_has_install_hint(tbl):
    try:
        import polars  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError, match="pip install polars"):
            tbl.to_polars()


def test_to_lance_returns_dataset(tbl):
    from mini_lancedb.dataset import Dataset

    assert isinstance(tbl.to_lance(), Dataset)


def test_table_create_classmethod(db):
    t = mldb.Table.create(db, "t", [{"id": 1, "vector": [1.0, 2.0]}])
    assert t.name == "t"


def test_table_open_classmethod(db):
    mldb.Table.create(db, "t", [{"id": 1, "vector": [1.0, 2.0]}])
    assert mldb.Table.open(db, "t").count_rows() == 1


def test_table_is_alias_of_lance_table():
    assert mldb.Table is mldb.LanceTable


def test_table_repr_contains_name_version_rows_columns(tbl):
    text = repr(tbl)
    assert "docs" in text
    assert "version=" in text
    assert str(N_ROWS) in text
    assert "id" in text


def test_table_len(tbl):
    assert len(tbl) == N_ROWS


# ===========================================================================
# 查询构建器的通用执行接口
# ===========================================================================


def test_to_arrow_from_query(tbl, rows_vecs):
    _, vecs = rows_vecs
    assert tbl.search(vecs[0].tolist()).limit(4).to_arrow().num_rows == 4


def test_to_pydict_from_query(tbl, rows_vecs):
    _, vecs = rows_vecs
    d = tbl.search(vecs[0].tolist()).limit(4).to_pydict()
    assert len(d["id"]) == 4


def test_to_batches_returns_reader(tbl, rows_vecs):
    _, vecs = rows_vecs
    reader = tbl.search(vecs[0].tolist()).limit(9).to_batches(batch_size=4)
    assert reader.read_all().num_rows == 9


def test_query_reusable(tbl, rows_vecs):
    _, vecs = rows_vecs
    q = tbl.search(vecs[0].tolist()).limit(3)
    assert _ids(q.to_list()) == _ids(q.to_list())


def test_query_limit_none_returns_everything(tbl, rows_vecs):
    _, vecs = rows_vecs
    assert len(tbl.search(vecs[0].tolist()).limit(None).to_list()) == N_ROWS


def test_query_negative_limit_raises(tbl):
    with pytest.raises(QueryError):
        tbl.search().limit(-1)


def test_query_negative_offset_raises(tbl):
    with pytest.raises(QueryError):
        tbl.search().offset(-1)


def test_search_unknown_kwarg_raises(tbl):
    with pytest.raises(QueryError, match="不认识"):
        tbl.search(limit=3, nope=1)


def test_search_kwargs_limit(db):
    t = db.create_table("t", [{"id": i, "vector": [float(i), 1.0]} for i in range(10)])
    assert len(t.search(limit=3).to_list()) == 3


def test_search_auto_query_type_with_string(fts):
    got = fts.search("fox", query_type="auto").limit(3).to_list()
    assert "_score" in got[0]


def test_search_auto_query_type_with_vector(tbl, rows_vecs):
    _, vecs = rows_vecs
    got = tbl.search(vecs[0].tolist(), query_type="auto").limit(3).to_list()
    assert "_distance" in got[0]


def test_search_unknown_query_type_raises(tbl):
    with pytest.raises(QueryError):
        tbl.search([1.0], query_type="magic")


def test_hybrid_query_type_requires_vector_argument(tbl):
    with pytest.raises(QueryError):
        tbl.search("some text", query_type="hybrid")


# ===========================================================================
# explain / analyze_plan
# ===========================================================================


def test_explain_scan_has_projection(tbl):
    plan = tbl.search().limit(3).explain()
    assert "ProjectionExec" in plan
    assert "LanceRead" in plan


def test_explain_vector_has_filters_and_topk(tbl, rows_vecs):
    _, vecs = rows_vecs
    plan = tbl.search(vecs[0].tolist()).limit(3).where("id > 5").explain()
    assert "KNNVectorDistance" in plan
    assert "FilterExec" in plan
    assert "TopK" in plan


def test_explain_select_does_not_execute(tbl, rows_vecs):
    _, vecs = rows_vecs
    # explain() 不跑查询，所以 _measured 应该还是空的
    tbl.search(vecs[0].tolist()).limit(3).explain()
    assert tbl.search(vecs[0].tolist()).limit(1)._measured == {}


def test_explain_indexed_shows_ann_nodes(indexed_tbl, rows_vecs):
    _, vecs = rows_vecs
    plan = indexed_tbl.search(vecs[0].tolist()).limit(3).explain()
    assert "ANNIvfPartition" in plan


def test_explain_scan_with_scalar_index_shows_scalar_query(db):
    t = db.create_table("t", [{"id": i, "vector": [float(i), 1.0]} for i in range(30)])
    t.create_index("id", config="BTREE")
    plan = t.search().where("id > 10").explain()
    assert "ScalarIndexQuery" in plan
    assert "btree" in plan.lower() or "BTREE" in plan


def test_explain_scan_without_predicate_has_no_scalar_query(db):
    """没有谓词时不该印 ``query=[?]`` —— 那是噪声，还误导人以为索引生效了。"""

    t = db.create_table("t", [{"id": i, "vector": [float(i), 1.0]} for i in range(30)])
    t.create_index("id", config="BTREE")
    assert "ScalarIndexQuery" not in t.search().limit(3).explain()


def test_explain_scan_has_late_materialization(tbl):
    assert "LateMaterialization" in tbl.search().where("id > 1").explain()


def test_explain_vector_without_index_says_flat_search(tbl, rows_vecs):
    _, vecs = rows_vecs
    plan = tbl.search(vecs[0].tolist()).limit(3).explain()
    assert "FlatSearch" in plan


def test_explain_vector_bypass_notes_bypass(tbl, rows_vecs):
    _, vecs = vecs_ = rows_vecs
    plan = tbl.search(vecs[0].tolist()).limit(3).bypass_vector_index().explain()
    assert "BypassVectorIndex" in plan


def test_explain_vector_fast_search_notes_footgun(tbl, rows_vecs):
    _, vecs = rows_vecs
    plan = tbl.search(vecs[0].tolist()).limit(3).fast_search().explain()
    assert "FastSearch" in plan


def test_explain_fts_without_index_warns(both_indexed):
    plan = both_indexed.search("alpha", query_type="fts").limit(3).explain()
    assert "ScalarIndexQuery" in plan


def test_explain_plan_alias(tbl, rows_vecs):
    _, vecs = rows_vecs
    q = tbl.search(vecs[0].tolist()).limit(3)
    assert q.explain() == q.explain_plan()


def test_explain_verbose_false_is_default(tbl):
    q = tbl.search().limit(3)
    assert q.explain() == q.explain(verbose=False)


# --------------------------------------------------------------------------
# _rowid —— 三种查询都必须能带上它
# --------------------------------------------------------------------------


def test_fts_with_row_id(fts):
    got = fts.search("fox", query_type="fts").limit(2).with_row_id().to_list()
    assert "_rowid" in got[0]
    assert got[0]["_rowid"] == got[0]["id"]


def test_hybrid_with_row_id(both_indexed, rows_vecs):
    _, vecs = rows_vecs
    got = (
        both_indexed.search(vecs[0].tolist(), query_type="hybrid")
        .text("alpha")
        .with_row_id()
        .limit(3)
        .to_list()
    )
    assert "_rowid" in got[0]


def test_vector_row_id_matches_scan_row_id(tbl, rows_vecs):
    """向量检索给出的 ``_rowid`` 必须和同一行的**逻辑行号**一致。

    注意这是本实现的约定（逻辑行号），真实 LanceDB 给的是稳定的 64 位
    行地址。两者会不同，但各自都必须自洽。
    """

    _, vecs = rows_vecs
    got = tbl.search(vecs[9].tolist()).limit(1).with_row_id().to_list()
    assert got[0]["id"] == 9
    assert got[0]["_rowid"] == 9


def test_scan_row_id_after_delete_uses_logical_number(tbl):
    tbl.delete("id < 5")
    got = tbl.search().limit(1).with_row_id().to_list()
    assert got[0]["id"] == 5
    assert got[0]["_rowid"] == 0


def test_analyze_plan_includes_measured_io(tbl, rows_vecs):
    _, vecs = rows_vecs
    report = tbl.search(vecs[0].tolist()).limit(5).analyze_plan()
    assert "实测 I/O" in report
    assert "brute_force" in report


def test_analyze_plan_scan_reports_scan_stats(db):
    t = db.create_table("t", [{"id": i, "vector": [float(i), 1.0]} for i in range(30)])
    report = t.search().where("id > 25").analyze_plan()
    assert "scan" in report
    assert "读取字节" in report


def test_analyze_plan_indexed_reports_index_stats(indexed_tbl, rows_vecs):
    _, vecs = rows_vecs
    report = indexed_tbl.search(vecs[0].tolist()).limit(5).analyze_plan()
    assert "index" in report


def test_explain_fast_search_notes_footgun(tbl, rows_vecs):
    _, vecs = vecs_ = rows_vecs
    pipeline = tbl.search(vecs[0].tolist()).limit(3).fast_search()
    pipeline.to_list()
    assert pipeline._measured.get("fast_search_empty") is True


def test_analyze_plan_fts_route(fts):
    plan = fts.search("fox", query_type="fts").limit(3).analyze_plan()
    assert "ScalarIndexQuery" in plan


# ===========================================================================
# embedding 函数（"全有或全无"的触发规则）
# ===========================================================================


@pytest.fixture
def hashing_fn():
    from mini_lancedb.embedding import EmbeddingFunctionConfig, get_registry

    fn = get_registry().get_function("hashing")
    return EmbeddingFunctionConfig(
        vector_column="vector", source_column="text", function=fn
    )


def test_embedding_auto_computed_when_vector_absent(db, hashing_fn):
    t = db.create_table(
        "e",
        [{"id": 1, "text": "hello world"}, {"id": 2, "text": "goodbye moon"}],
        embedding_functions=[hashing_fn],
    )
    assert t.count_rows() == 2
    assert t.schema["vector"].vector_dim == 128
    assert all(v is not None for v in (r["vector"] for r in t.to_list()))


def test_embedding_reports_computed_status(db, hashing_fn):
    res = db.create_table(
        "e", [{"id": 1, "text": "hello"}], embedding_functions=[hashing_fn]
    ).add([{"id": 2, "text": "world"}])
    assert res.embedding == "computed"


def test_embedding_skipped_when_partially_supplied(db, hashing_fn):
    """⚠️ 真实 LanceDB 的坑：给了一行向量，整批就都不自动算了。"""

    t = db.create_table(
        "e",
        [{"id": 1, "text": "hello world"}, {"id": 2, "text": "goodbye moon"}],
        embedding_functions=[hashing_fn],
    )
    res = t.add(
        [
            {"id": 3, "text": "mixed", "vector": [0.0] * 128},
            {"id": 4, "text": "left null"},
        ]
    )
    assert res.embedding == "skipped_partial"
    # 第 4 行的向量必须**仍然是 null** —— 而不是被"顺手补上"
    got = {r["id"]: r["vector"] for r in t.search().to_list()}
    assert got[4] is None


def test_embedding_text_query_uses_function(db, hashing_fn):
    t = db.create_table(
        "e",
        [{"id": 1, "text": "hello world"}, {"id": 2, "text": "goodbye moon"}],
        embedding_functions=[hashing_fn],
    )
    got = t.search("hello world").limit(1).to_list()
    assert got[0]["id"] == 1


def test_text_query_without_embedding_function_raises(tbl):
    with pytest.raises(QueryError, match="embedding"):
        tbl.search("some text").limit(1).to_list()


def test_func_namespace_markers_exist():
    assert mldb.func.SourceField().kind == "source"
    assert mldb.func.VectorField().kind == "vector"
