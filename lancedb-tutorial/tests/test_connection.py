"""``LanceDBConnection`` / 命名空间 / 权限 / I/O 计量的契约测试。

这个文件里有两类测试特别值得读：

* **``InstrumentedStore`` 的字节断言**（``test_*_reads_fewer_bytes``）——
  它们是整套代码里**唯一**能把"下推到底有没有生效"变成二值判断的东西。
  没有它们，"列投影 + 谓词下推能省 I/O"就只是一句听起来很对的话。
* **权限的拒绝路径** —— 权限测试里最有价值的是"该拒绝时真的拒绝了"，
  而不是"授权之后能通过"。只测 happy path 的权限测试覆盖率为零：
  把 ``check`` 写成 ``pass`` 它照样全绿。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

import mini_lancedb as mldb
from mini_lancedb import (
    AccessController,
    Permission,
    PermissionDenied,
    PermissionStore,
    StoreError,
    TableAlreadyExistsError,
    TableNotFoundError,
    with_tables_filtered,
)
from mini_lancedb.namespace import NamespaceNotFoundError

from .conftest import DIM, make_rows


def _seed(db, name="docs", n=60):
    rows = [{"id": i, "text": f"row {i} alpha", "vector": [float(i), 1.0] * (DIM // 2)}
            for i in range(n)]
    return db.create_table(name, rows)


# ===========================================================================
# connect
# ===========================================================================


def test_connect_no_args_is_memory():
    db = mldb.connect()
    assert db.uri == "memory://"
    assert db.table_names() == []


def test_connect_memory_scheme():
    assert mldb.connect("memory://").table_names() == []


def test_connect_local_path_creates_dir(tmp_path):
    target = tmp_path / "db"
    db = mldb.connect(str(target))
    assert target.exists()
    assert db.table_names() == []


def test_connect_file_scheme(tmp_path):
    db = mldb.connect("file://" + str(tmp_path / "db").replace("\\", "/"))
    assert db.table_names() == []


def test_connect_local_persists_across_connections(tmp_path):
    uri = str(tmp_path / "db")
    db = mldb.connect(uri)
    db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    # 换一个连接对象再打开 —— 数据必须在磁盘上
    again = mldb.connect(uri)
    assert again.table_names() == ["t"]
    assert again.open_table("t").count_rows() == 1


def test_connect_s3_is_simulated_and_offline():
    db = mldb.connect("s3://bucket/db")
    from mini_lancedb.storage.base import S3LikeStore

    assert isinstance(db.store, S3LikeStore)
    # 模拟存储的关键"坏脾气"必须存在，否则教程里"为什么不能 rename"
    # 的结论就没有可复现的证据了
    with pytest.raises(StoreError):
        db.store.rename("a", "b")


def test_connect_s3_supports_conditional_put():
    db = mldb.connect("s3://bucket/db")
    assert db.store.supports_conditional_put is True


def test_connect_gs_is_simulated():
    assert mldb.connect("gs://bucket/db").table_names() == []


def test_connect_unknown_scheme_raises():
    with pytest.raises(mldb.QueryError):
        mldb.connect("ftp://here/there")


def test_connect_accepts_but_ignores_credentials():
    db = mldb.connect("memory://", api_key="secret", region="eu-1",
                      storage_options={"k": "v"})
    assert db.table_names() == []


def test_connect_s3_can_create_and_read_tables():
    db = mldb.connect("s3://bucket/db")
    t = db.create_table("t", [{"id": i, "vector": [float(i), 1.0]} for i in range(5)])
    assert t.count_rows() == 5
    assert db.table_names() == ["t"]


# ===========================================================================
# 表名 / 打开 / 创建
# ===========================================================================


def test_table_names_sorted(db):
    for name in ("zeta", "alpha", "mu"):
        db.create_table(name, [{"id": 1, "vector": [1.0, 2.0]}])
    assert db.table_names() == ["alpha", "mu", "zeta"]


def test_table_names_limit(db):
    for name in ("a", "b", "c"):
        db.create_table(name, [{"id": 1, "vector": [1.0, 2.0]}])
    assert db.table_names(limit=2) == ["a", "b"]


def test_table_exists(db):
    _seed(db)
    assert db.table_exists("docs") is True
    assert db.table_exists("nope") is False


def test_contains_operator(db):
    _seed(db)
    assert "docs" in db
    assert "nope" not in db


def test_getitem_opens_table(db):
    _seed(db, n=3)
    assert db["docs"].count_rows() == 3


def test_getitem_missing_raises(db):
    with pytest.raises(TableNotFoundError):
        db["nope"]


def test_open_table_missing_raises_with_hint(db):
    with pytest.raises(TableNotFoundError) as exc:
        db.open_table("nope")
    assert "available" in exc.value.context


def test_len_and_iter(db):
    _seed(db, "a", 2)
    _seed(db, "b", 3)
    assert len(db) == 2
    assert sorted(t.name for t in db) == ["a", "b"]


def test_repr_lists_tables(db):
    _seed(db, "docs")
    text = repr(db)
    assert "docs" in text
    assert "memory://" in text


def test_open_table_at_version(db):
    t = _seed(db, n=5)
    t.add([{"id": 99, "vector": [0.0] * DIM}])
    old = db.open_table("docs", version=2)
    assert 99 not in [r["id"] for r in old.to_list()]


def test_create_table_on_s3_store_isolates_prefix(db):
    t = db.create_table("t", [{"id": 1, "vector": [1.0, 2.0]}])
    # 表的数据必须落在 "t.lance/" 前缀下，而不是污染根目录
    keys = [m.path for m in db.store.list("", recursive=True)]
    assert keys
    assert all(k.startswith("t.lance/") for k in keys)


def test_two_tables_do_not_share_fragments(db):
    a = db.create_table("a", [{"id": 1, "vector": [1.0, 2.0]}])
    b = db.create_table("b", [{"id": 2, "vector": [9.0, 9.0]}])
    a._test = None
    assert a.count_rows() == 1 and b.count_rows() == 1
    paths_a = {f.path for frag in a.to_lance().fragments for f in frag.files}
    paths_b = {f.path for frag in b.to_lance().fragments for f in frag.files}
    assert paths_a.isdisjoint(paths_b)


def test_drop_table(db):
    _seed(db)
    db.drop_table("docs")
    assert db.table_names() == []


def test_drop_table_missing_raises(db):
    with pytest.raises(TableNotFoundError):
        db.drop_table("nope")


def test_drop_table_ignore_missing(db):
    db.drop_table("nope", ignore_missing=True)
    assert db.table_names() == []


def test_rename_table(db):
    _seed(db, "old", 4)
    db.rename_table("old", "new")
    assert db.table_names() == ["new"]
    assert db.open_table("new").count_rows() == 4


def test_rename_table_missing_raises(db):
    with pytest.raises(TableNotFoundError):
        db.rename_table("nope", "new")


def test_rename_table_conflict_raises(db):
    _seed(db, "a", 1)
    _seed(db, "b", 1)
    with pytest.raises(TableAlreadyExistsError):
        db.rename_table("a", "b")


def test_rename_table_into_namespace_keeps_data(db):
    _seed(db, "a", 6)
    db.rename_table("a", "a2")
    t = db["a2"]
    assert t.count_rows() == 6
    # 追写仍然正常 —— 说明 manifest 里的相对路径没有因为改名而失效
    t.add([{"id": 100, "vector": [0.0] * DIM}])
    assert t.count_rows() == 7


# ===========================================================================
# 命名空间
# ===========================================================================


def test_create_and_list_namespaces(db):
    db.create_namespace(["tenant_a"])
    assert db.list_namespaces() == ["tenant_a"]


def test_create_namespace_auto_creates_parents(db):
    db.create_namespace(["a", "b", "c"])
    assert db.list_namespaces() == ["a"]
    assert db.list_namespaces(["a"]) == ["a.b"]
    assert db.list_namespaces(["a", "b"]) == ["a.b.c"]


def test_create_namespace_accepts_dotted_string(db):
    db.create_namespace("a.b")
    assert db.list_namespaces(["a"]) == ["a.b"]


def test_create_namespace_duplicate_raises(db):
    db.create_namespace(["a"])
    with pytest.raises(mldb.QueryError):
        db.create_namespace(["a"])


def test_create_namespace_exist_ok(db):
    db.create_namespace(["a"])
    db.create_namespace(["a"], mode="exist_ok")
    assert db.list_namespaces() == ["a"]


def test_create_namespace_root_raises(db):
    with pytest.raises(mldb.QueryError):
        db.create_namespace([])


def test_namespace_describe(db):
    db.create_namespace(["a", "b"])
    info = db.describe_namespace(["a", "b"])
    assert info["full_path"] == "a.b"
    assert info["parent"] == "a"


def test_namespace_unknown_raises(db):
    with pytest.raises(NamespaceNotFoundError):
        db.describe_namespace(["nope"])


def test_namespace_create_table(db):
    db.create_namespace(["a"])
    t = db.namespaces.create_table("docs", ["a"], data=[{"id": 1, "vector": [1.0, 2.0]}])
    assert t.count_rows() == 1
    # 物理表仍在根目录下 —— 命名空间是**目录**，不是存储隔离
    assert "docs" in db.table_names()
    assert db.list_namespaces(["a"]) == []


def test_namespace_list_tables(db):
    db.create_namespace(["a"])
    db.namespaces.create_table("docs", ["a"], data=[{"id": 1, "vector": [1.0, 2.0]}])
    assert db.namespaces.list_tables(["a"]) == ["a.docs"]


def test_namespace_open_table(db):
    db.create_namespace(["a"])
    db.namespaces.create_table("docs", ["a"], data=[{"id": 1, "vector": [1.0, 2.0]}])
    assert db.namespaces.open_table("docs", ["a"]).count_rows() == 1


def test_namespace_open_unregistered_table_raises(db):
    db.create_namespace(["a"])
    _seed(db, "docs", 2)
    with pytest.raises(TableNotFoundError):
        db.namespaces.open_table("docs", ["a"])


def test_namespace_duplicate_table_raises(db):
    db.create_namespace(["a"])
    db.namespaces.create_table("docs", ["a"], data=[{"id": 1, "vector": [1.0, 2.0]}])
    with pytest.raises(TableAlreadyExistsError):
        db.namespaces.create_table("docs", ["a"], data=[{"id": 2, "vector": [1.0, 2.0]}])


def test_namespace_create_table_in_missing_namespace_raises(db):
    with pytest.raises(NamespaceNotFoundError):
        db.namespaces.create_table("docs", ["ghost"], data=[{"id": 1, "vector": [1.0]}])


def test_drop_namespace_empty(db):
    db.create_namespace(["a"])
    db.drop_namespace(["a"])
    assert db.list_namespaces() == []


def test_drop_nonempty_namespace_raises(db):
    db.create_namespace(["a", "b"])
    with pytest.raises(mldb.QueryError):
        db.drop_namespace(["a"])


def test_drop_namespace_recursive(db):
    db.create_namespace(["a", "b", "c"])
    db.drop_namespace(["a"], mode="recursive")
    assert db.list_namespaces() == []


def test_drop_namespace_does_not_delete_physical_table(db):
    """⚠️ 删命名空间**不动任何数据** —— 这是这个设计里最重要的一条。"""

    db.create_namespace(["a"])
    db.namespaces.create_table("docs", ["a"], data=[{"id": 1, "vector": [1.0, 2.0]}])
    # 非空命名空间必须显式 recursive 才肯删（rmdir vs rm -rf 的道理）
    db.drop_namespace(["a"], mode="recursive")
    assert "docs" in db.table_names()
    assert db.open_table("docs").count_rows() == 1


def test_namespace_drop_table_unregisters_only(db):
    db.create_namespace(["a"])
    db.namespaces.create_table("docs", ["a"], data=[{"id": 1, "vector": [1.0, 2.0]}])
    db.namespaces.drop_table("docs", ["a"])
    assert db.namespaces.list_tables(["a"]) == []
    assert "docs" in db.table_names()


def test_namespace_drop_table_physical(db):
    db.create_namespace(["a"])
    db.namespaces.create_table("docs", ["a"], data=[{"id": 1, "vector": [1.0, 2.0]}])
    db.namespaces.drop_table("docs", ["a"], physical=True)
    assert db.table_names() == []


def test_namespace_drop_root_raises(db):
    with pytest.raises(mldb.QueryError):
        db.drop_namespace([])


def test_namespace_manifest_is_json_on_store(db):
    db.create_namespace(["a"])
    raw = json.loads(db.store.get("_namespaces.json").decode("utf-8"))
    assert "a" in raw["namespaces"]


def test_namespace_client_repr(db):
    db.create_namespace(["a"])
    assert "NamespaceClient" in repr(db.namespaces)


# ===========================================================================
# 权限
# ===========================================================================


def _controller(db):
    return AccessController(PermissionStore(db.store), superusers=set())


def test_permission_grant_then_check_passes(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.SELECT])
    ac.check("alice", "docs", Permission.SELECT)


def test_permission_denied_raises(db):
    ac = _controller(db)
    with pytest.raises(PermissionDenied):
        ac.check("mallory", "docs", Permission.SELECT)


def test_permission_denied_carries_context(db):
    ac = _controller(db)
    with pytest.raises(PermissionDenied) as exc:
        ac.check("mallory", "docs", Permission.DELETE)
    assert exc.value.principal == "mallory"
    assert exc.value.table == "docs"
    assert exc.value.permission is Permission.DELETE


def test_permission_allows_is_boolean_version(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.SELECT])
    assert ac.allows("alice", "docs", Permission.SELECT) is True
    assert ac.allows("alice", "docs", Permission.DROP) is False


def test_permission_alter_implies_select_and_insert(db):
    """隐含关系是权限系统里最容易漏掉、也最容易变成提权漏洞的东西。"""

    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.ALTER])
    ac.check("alice", "docs", Permission.SELECT)
    ac.check("alice", "docs", Permission.INSERT)
    assert ac.allows("alice", "docs", Permission.DROP) is False


def test_permission_drop_implies_create(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.DROP])
    ac.check("alice", "docs", Permission.CREATE)


def test_permission_create_does_not_imply_drop(db):
    """⚠️ 如果能建表就等于能删表，"给分析师一个沙箱"就变成了"能删生产表"。"""

    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.CREATE])
    assert ac.allows("alice", "docs", Permission.DROP) is False


def test_permission_effective_expands_implications(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.UPDATE])
    effective = {p.value for p in ac.effective("alice", "docs")}
    assert "update" in effective and "select" in effective


def test_permission_wildcard_grant(db):
    ac = _controller(db)
    ac.grant("alice", "*", [Permission.SELECT])
    ac.check("alice", "anything", Permission.SELECT)


def test_permission_revoke_all(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.SELECT, Permission.INSERT])
    ac.revoke("alice", "docs")
    assert ac.allows("alice", "docs", Permission.SELECT) is False


def test_permission_revoke_specific(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.SELECT, Permission.INSERT])
    ac.revoke("alice", "docs", [Permission.INSERT])
    assert ac.allows("alice", "docs", Permission.SELECT) is True
    assert ac.allows("alice", "docs", Permission.INSERT) is False


def test_permission_revoke_missing_raises(db):
    ac = _controller(db)
    with pytest.raises(mldb.QueryError):
        ac.revoke("nobody", "docs")


def test_permission_grant_is_idempotent(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.SELECT])
    ac.grant("alice", "docs", [Permission.SELECT])
    assert len(ac.store.all_grants()) == 1


def test_permission_unknown_name_raises(db):
    ac = _controller(db)
    with pytest.raises(mldb.QueryError):
        ac.grant("alice", "docs", ["fly"])


def test_permission_superuser_bypasses_checks(db):
    ac = AccessController(PermissionStore(db.store), superusers={"root"})
    ac.check("root", "docs", Permission.DROP)


def test_permission_controller_disabled_allows_everything(db):
    ac = AccessController(PermissionStore(db.store), enabled=False, superusers=set())
    ac.check("nobody", "docs", Permission.DROP)


def test_permission_filter_tables(db):
    ac = _controller(db)
    ac.grant("alice", "a", [Permission.SELECT])
    assert ac.filter_tables("alice", ["a", "b"]) == ["a"]


def test_permission_with_tables_filtered_reports_denied(db):
    ac = _controller(db)
    ac.grant("alice", "a", [Permission.SELECT])
    filtered = ac.with_tables_filtered("alice", ["a", "b"])
    assert list(filtered) == ["a"]
    assert filtered.denied == ("b",)
    assert len(filtered) == 1
    assert "a" in filtered


def test_permission_module_level_helper(db):
    ac = _controller(db)
    ac.grant("alice", "a", [Permission.SELECT])
    assert with_tables_filtered(ac, "alice", ["a", "b"]) == ["a"]


def test_permission_store_persists_to_store(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.SELECT])
    # 换一个 PermissionStore 实例重新读 —— 授权必须落在存储上
    fresh = PermissionStore(db.store)
    assert fresh.effective("alice", "docs") == {Permission.SELECT}


def test_permission_grants_do_not_leak_between_principals(db):
    ac = _controller(db)
    ac.grant("alice", "docs", [Permission.SELECT])
    assert ac.allows("bob", "docs", Permission.SELECT) is False


# ===========================================================================
# I/O 计量 —— 唯一的"下推有没有生效"的硬证据
# ===========================================================================


def _instrumented(db, n=200):
    db.instrument()
    rows = [
        {"id": i, "text": f"row {i} " + "pad " * 20, "vector": [float(i), 1.0] * (DIM // 2)}
        for i in range(n)
    ]
    t = db.create_table("io", rows)
    db.reset_io()
    return t


def test_instrument_wraps_store(db):
    from mini_lancedb.storage.base import InstrumentedStore

    db.instrument()
    assert isinstance(db.store, InstrumentedStore)


def test_instrument_is_idempotent(db):
    from mini_lancedb.storage.base import InstrumentedStore

    db.instrument()
    db.instrument()
    assert isinstance(db.store, InstrumentedStore)
    # 不能包两层 —— 那会把每次 I/O 记两遍，数字直接翻倍
    assert not isinstance(db.store.inner, InstrumentedStore)


def test_projected_filtered_scan_reads_fewer_bytes_than_full_scan(db):
    """一条"投影 + 过滤"的查询必须**严格**读得比全表少。

    这是列存全部价值的可执行定义。测试里两个数字都是实测的：
    全表扫一遍记下的字节数，和只取 ``id`` 且只留下命中行记下的字节数。
    """

    t = _instrumented(db, n=200)
    t.search().to_list()
    full_bytes = db.store.stats.bytes_read
    assert full_bytes > 0

    db.reset_io()
    rows = t.search().select(["id"]).where("id >= 195").limit(5).to_list()
    projected_bytes = db.store.stats.bytes_read

    assert len(rows) == 5
    assert projected_bytes < full_bytes


def test_projection_alone_reads_fewer_bytes(db):
    t = _instrumented(db, n=200)
    t.search().to_list()
    full_bytes = db.store.stats.bytes_read

    db.reset_io()
    t.search().select(["id"]).to_list()
    assert db.store.stats.bytes_read < full_bytes


def test_reset_io_zeroes_counters(db):
    t = _instrumented(db, n=20)
    t.search().to_list()
    assert db.store.stats.bytes_read > 0
    db.reset_io()
    assert db.store.stats.bytes_read == 0


def test_instrument_rebinds_existing_tables(db):
    """先建表、后开计量，表对象必须被重新指到计量过的 store 上。"""

    rows = [{"id": i, "text": f"r{i}", "vector": [float(i), 1.0]} for i in range(20)]
    t = db.create_table("pre", rows)
    db.instrument()
    db.reset_io()
    t.search().to_list()
    assert db.store.stats.bytes_read > 0


def test_io_events_record_operations(db):
    t = _instrumented(db, n=20)
    t.search().limit(3).to_list()
    ops = {op for op, _path, _n in db.store.events}
    assert ops
    assert ops <= {"GET", "GET_RANGE", "PUT", "PUT_IF_NOT_EXISTS", "DELETE", "HEAD", "LIST"}


def test_vector_search_reads_less_with_projection(db):
    t = _instrumented(db, n=200)
    t.search([1.0, 1.0] * (DIM // 2)).limit(5).to_list()
    full_bytes = db.store.stats.bytes_read

    db.reset_io()
    t.search([1.0, 1.0] * (DIM // 2)).limit(5).select(["id"]).to_list()
    assert db.store.stats.bytes_read < full_bytes


# ===========================================================================
# embedding 注册（兼容入口 + schema 声明）
# ===========================================================================


def test_embedding_function_registration_from_config(db):
    from mini_lancedb.embedding import EmbeddingFunctionConfig, get_registry

    conf = EmbeddingFunctionConfig(
        vector_column="vector",
        source_column="text",
        function=get_registry().get_function("hashing"),
    )
    t = db.create_table(
        "e", [{"id": 1, "text": "hello world"}], embedding_functions=[conf]
    )
    assert t.schema["vector"].vector_dim == 128


def test_embedding_metadata_lands_in_schema(db):
    from mini_lancedb.embedding import EmbeddingFunctionConfig, get_registry

    conf = EmbeddingFunctionConfig(
        vector_column="vector",
        source_column="text",
        function=get_registry().get_function("hashing"),
    )
    t = db.create_table("e", [{"id": 1, "text": "hi"}], embedding_functions=[conf])
    raw = (t.schema.metadata or {}).get("embedding_functions")
    assert raw and "vector" in raw


def test_embedding_function_registration_from_schema_metadata(db):
    """schema metadata 里的注册在**换一个表对象之后**仍然认得出来。"""

    from mini_lancedb.embedding import EmbeddingFunctionConfig, get_registry

    conf = EmbeddingFunctionConfig(
        vector_column="vector",
        source_column="text",
        function=get_registry().get_function("hashing"),
    )
    db.create_table("e", [{"id": 1, "text": "hi"}], embedding_functions=[conf])
    reopened = db.open_table("e")
    assert reopened._embedding_function_for("vector") is not None


def test_embedding_registration_from_func_markers_without_function():
    from mini_lancedb.connection import EmbeddingFunctionRegistration

    class _Field:
        def __init__(self, name, marker):
            self.name = name
            self.type = marker

    fields = [_Field("text", mldb.func.SourceField()), _Field("vector", mldb.func.VectorField())]
    regs = EmbeddingFunctionRegistration.from_schema_metadata(fields)
    # 标记能被配对，但**没有函数**时不会硬编一个出来
    assert len(regs) == 1
    assert regs[0].source_column == "text"
    assert regs[0].function is None


def test_func_markers_are_not_plain_fields():
    marker = mldb.func.SourceField()
    assert marker.kind == "source"
    assert repr(marker).startswith("func.")
