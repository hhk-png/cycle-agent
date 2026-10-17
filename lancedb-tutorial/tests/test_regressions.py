"""回归测试：每一个用例都对应一个**真实踩过的 bug**。

这个文件是整个测试套件里最值得读的一个。它记录的是一类特殊的 bug：

* 不崩溃、不报错、测试全绿，只是结果不对；
* 或者是"只在某一种数据分布 / 某一种调用顺序下才出现"；
* 或者是"监控指标静默变成 0"。

按类别分成九组，每组开头都写了"当初是怎么发现的"。
第八、九组是唯一两组"会丢数据"的：它们不归在"结果不对"那一类里，
而是"写成功了、当时不报错、事后才炸"。
维护这份文件的规则只有一条：**修了 bug 就在这里加一条**，
而且注释里要写清楚"不修会怎样"，因为它比断言本身更有价值。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mini_lancedb as ldb  # noqa: E402
from mini_lancedb.arrays import StringArray  # noqa: E402
from mini_lancedb.dataset import Dataset  # noqa: E402
from mini_lancedb.encoding.codecs import (  # noqa: E402
    DeltaEncoder,
    FsstEncoder,
    decode_numeric,
)
from mini_lancedb.encoding.compress import lz4_compress, lz4_decompress  # noqa: E402
from mini_lancedb.encoding.compress import crosscheck_lz4  # noqa: E402
from mini_lancedb.errors import TypeMismatchError  # noqa: E402
from mini_lancedb.record import RecordBatch  # noqa: E402
from mini_lancedb.scanner import Scanner  # noqa: E402
from mini_lancedb.storage.base import InMemoryStore, InstrumentedStore  # noqa: E402
from mini_lancedb.types import INT64, parse_type  # noqa: E402

DIM = 8


# ---------------------------------------------------------------------------
# 一、编码器的往返测试"自洽但不能证明正确"
# ---------------------------------------------------------------------------


def test_delta_decoder_adds_the_base_value_back():
    """delta 解码漏加首值 —— 只在首值非零的列上暴露。

    当初的症状：把数据分批写入，第一批（从 0 开始）读回来完全正确，
    后面的批次读回来是 ``[10000, 1, 2, 3, ...]`` 这种鬼东西。
    根因是解码器写成了 ``out[1:] = cumsum(deltas)``，
    漏了 ``+ first``；首值为 0 时它恰好是对的。

    教训：**编码器的测试必须覆盖"基值非零"的输入**。
    只用 ``range(n)`` 这种从 0 开始的数据测，永远抓不到这类 bug。
    """

    data = np.arange(1000, 2000, dtype=np.int64)
    page = DeltaEncoder.encode(data, INT64, None)
    assert np.array_equal(decode_numeric(page, INT64), data)

    # 基值为负也要对
    signed = np.arange(-500, 500, dtype=np.int64)
    page = DeltaEncoder.encode(signed, INT64, None)
    assert np.array_equal(decode_numeric(page, INT64), signed)


def test_lz4_roundtrip_holds_for_many_shapes():
    """LZ4 的边界：空输入、单字节、高重复、纯随机。

    最麻烦的曾经是空输入 —— 我们不产出任何字节，C 实现拒绝解码。
    参考实现会为 0 字节输入产出一个"只有一个 0 token"的块。
    """

    rng = np.random.default_rng(0)
    samples = [
        b"",
        b"a",
        b"aaaa",
        b"hello world " * 100,
        bytes(10_000),
        rng.integers(0, 256, 4096, dtype=np.uint8).tobytes(),
        bytes([rng.integers(0, 4) for _ in range(5000)]),
    ]
    for data in samples:
        compressed = lz4_compress(data)
        assert lz4_decompress(compressed, len(data)) == data


def test_lz4_is_byte_compatible_with_the_c_implementation():
    """和 C 实现对拍 —— 自洽的往返测试**不足以**证明格式正确。

    当初的 bug：编码器写出了一个 offset=0 的序列（"匹配到自己"）。
    我们自己的解码器能读出大部分内容，直到几百字节之后才因为
    "字面量超界"报错 —— 报错位置和真正的起因完全无关。
    只有拿 C 实现对拍才定位到。
    """

    if crosscheck_lz4(b"probe") is None:
        pytest.skip("环境里没有 lz4 包，跳过交叉验证")
    rng = np.random.default_rng(1)
    for data in (
        b"",
        b"the quick brown fox " * 30,
        bytes(range(256)) * 8,
        rng.integers(0, 256, 2048, dtype=np.uint8).tobytes(),
    ):
        result = crosscheck_lz4(data)
        assert all(result.values()), result


def test_fsst_roundtrip_on_realistic_long_strings():
    """FSST 解码把"每个符号"当成了"每一行"。

    手工构造的短字符串（一行一个符号）能过，真实文档文本（一行几十个符号）
    会解出错误的行数和内容，**而且不报错**。后果是把乱码喂给嵌入模型 ——
    模型不会因为输入是乱码而报错，它只会给出一个错误的向量。

    修法是在页里显式存行 offsets。
    """

    docs = [f"document number {i} about lance and vector search" for i in range(3000)]
    array = StringArray.from_strings(docs)
    page = FsstEncoder.encode(array.offsets, array.buffer, len(array))
    offsets, buffer = FsstEncoder.decode(page)
    decoded = StringArray(array.type, offsets, buffer, None)
    assert decoded.to_pylist() == docs


# ---------------------------------------------------------------------------
# 二、重载了比较运算符的 AST 节点：拿它去比较永远不会报错
# ---------------------------------------------------------------------------


def test_between_filter_returns_rows_with_zone_map_pruning_enabled(configless_dataset):
    """``BETWEEN`` 在开启统计剪枝时静默返回 0 行。

    根因：``_stats_can_match`` 里写的是 ``expr.high < lo``，
    而 ``Expr.__lt__`` 被重载成"构造一个比较节点"，
    于是这个条件**恒真** —— 每个片段都被判成"不可能命中"。

    最阴的地方在于：同一语义的 ``>= AND <=`` 写法完全正常，
    删行的路径（走另一套求值）也完全正常，
    关掉 pruning 之后 BETWEEN 也"好了"。所以它很容易被误判成环境问题。
    """

    ds = configless_dataset
    spanned = len(ds.scanner(columns=[], filter="n BETWEEN 450 AND 460").to_batch())
    assert spanned == 11

    total = len(ds.scanner(columns=[], filter="n BETWEEN 400 AND 499").to_batch())
    assert total == 100

    # 和等价的显式写法结果必须一致
    explicit = len(ds.scanner(columns=[], filter="n >= 450 AND n <= 460").to_batch())
    assert explicit == spanned

    # count_rows 走的也是剪枝路径，同样要对
    assert ds.scanner(columns=[], filter="n BETWEEN 450 AND 460").count_rows() == 11


def test_negative_literals_are_readable_by_the_pruner(configless_dataset):
    """负数字面量被解析成 ``UnaryOp("-", Literal(1))``。

    直接读 ``node.value`` 会拿到 ``None``，于是统计剪枝拿不到阈值、
    退化成"不剪枝"；更糟的是某些路径会把它当成"无法判断"而做出错误决策。
    现在统一走 ``_literal_value()`` 解值。
    """

    ds = configless_dataset
    got = ds.scanner(columns=[], filter="n > -1").to_batch()
    assert got.num_rows == ds.num_rows


# ---------------------------------------------------------------------------
# 三、索引与行号：一个会崩，一个会算错
# ---------------------------------------------------------------------------


def test_vector_search_still_works_after_delete_with_index(configless_dataset):
    """建完索引再删行，向量检索直接崩。

    症状：``IndexError: 行号越界: 196（共 100 行）``，
    或者带 where 时 ``QueryError: 掩码长度与索引行数不一致``。

    根因是索引里存的是**建索引那一刻的逻辑行号**，而删除会让
    逻辑行号整体前移，索引却没有任何失效或重映射处理。
    现在 ``_delete_append`` 会把索引里的行号从旧编号搬到新编号，
    搬不动就标记失效（降级成暴力检索，而不是崩溃）。
    """

    ds = configless_dataset
    ds.create_index("vector", index_type="IVF_FLAT", name="v", num_partitions=8)
    before = ds.num_rows
    ds.delete("id < 400")
    assert ds.num_rows == before - 400

    # 删完之后查一个还在的向量，必须能命中它自己。
    # 注意索引返回的是**逻辑行号**，要回表才能知道它是哪个 id。
    target_vector = np.asarray(
        ds.take_rows([0], columns=["vector"]).to_numpy("vector"), dtype=np.float32
    )
    result = ds.load_index("v").search(
        target_vector, 3, params={"nprobes": 8}
    )
    hits = [int(x) for x in result.row_ids[0] if x >= 0]
    assert hits, "删除之后索引返回了空结果"
    assert 0 in hits, "最近邻应该是查询向量自己那一行"

    # 索引里的每个行号都必须落在当前的行数范围内 —— 越界就说明没重映射
    assert max(hits) < ds.num_rows
    ids = ds.take_rows(hits, columns=["id"])["id"].to_pylist()
    assert 400 in ids, "删除 id<400 之后，第 0 行应该是 id=400"

    # 而且整个查询链路也要能跑通
    rows = ds.scanner(columns=["id"]).to_batch()
    assert rows.num_rows == ds.num_rows


def test_index_row_ids_are_remapped_not_stale(configless_dataset):
    """重映射的正确性：删掉一半之后，索引里的行号必须都指向活着的行。"""

    ds = configless_dataset
    ds.create_index("vector", index_type="IVF_FLAT", name="v", num_partitions=4)
    ds.delete("id % 2 = 0")  # 删掉所有偶数 id
    index = ds.load_index("v")
    alive = {int(x) for x in ds.scanner(columns=["id"]).to_batch()["id"].to_pylist()}
    # 索引里的行号是逻辑行号，必须落在 [0, num_rows) 内
    assert len(index.row_ids) <= ds.num_rows
    assert int(index.row_ids.max()) < ds.num_rows
    assert len(alive) == ds.num_rows


# ---------------------------------------------------------------------------
# 四、剪枝决策的粒度
# ---------------------------------------------------------------------------


def test_unrelated_index_does_not_disable_zone_map_pruning(configless_dataset):
    """在任意一列上建索引，会把整张表的统计剪枝全部关掉。

    根因是判断粒度写成了"这张表有没有索引"，
    而不是"这个谓词能不能被某个索引回答"。
    后果：给一个列建索引之后，**另一个列**上的查询从剪掉 9/10 个片段
    变成 0/10，读取字节涨好几倍。
    """

    ds = configless_dataset
    scanner = ds.scanner(columns=["id"], filter="n > 19000")
    scanner.to_batch()
    before = scanner.stats.fragments_pruned_by_stats
    assert before > 0, "前提不成立：这个查询本来就该被剪枝"

    # 在一个和谓词无关的列上建索引
    ds.create_index("txt", index_type="BTREE", name="txt_idx")

    scanner = ds.scanner(columns=["id"], filter="n > 19000")
    scanner.to_batch()
    assert scanner.stats.fragments_pruned_by_stats == before, (
        "建了一个无关列的索引之后，原来能剪的片段剪不动了"
    )


# ---------------------------------------------------------------------------
# 五、监控指标静默归零
# ---------------------------------------------------------------------------


def test_index_mask_also_skips_whole_fragments(configless_dataset):
    """"建了索引反而更慢" —— 索引给了行号，却没有跳过片段。

    症状：一个 `WHERE n BETWEEN 100 AND 200` 在 5 万行、25 个片段的表上
    只命中 1 个片段里的 101 行，但扫描器仍然把 25 个片段全打开。
    实测比不建索引、靠统计剪枝还慢 60 倍。

    修法有两处：
    1. 不再因为"这个谓词有索引可用"就**强制保留**所有片段
       （统计剪枝本来就是保守的，强制保留是纯粹的自我伤害）；
    2. 索引掩码也参与片段裁剪 —— 掩码里一行都不剩的片段直接跳过。
    """

    from mini_lancedb.dataset import clear_index_cache

    ds = configless_dataset
    ds.create_index("n", index_type="BTREE", name="n_idx")
    # 先把索引读进缓存 —— 否则下面量到的"读取字节"几乎全是
    # 冷启动加载索引文件的成本，和我们要验证的"跳过片段"无关。
    clear_index_cache()
    ds.load_index("n_idx")

    scanner = ds.scanner(columns=["id"], filter="n BETWEEN 100 AND 200")
    batch = scanner.to_batch()
    assert batch.num_rows == 101
    assert scanner.stats.fragments_total == 10
    assert scanner.stats.fragments_scanned <= 2, (
        f"索引命中了 {batch.num_rows} 行，却扫了 "
        f"{scanner.stats.fragments_scanned} 个片段"
    )
    # 读取量必须远小于"把所有片段都打开"的量级
    assert scanner.stats.bytes_read < 60_000


def test_index_cache_is_version_keyed_and_invalidates(configless_dataset):
    """索引缓存必须跟着版本失效。

    索引文件是不可变的，所以"版本 + 文件清单"变了key 就该变。
    如果 key 里漏掉这些，压实或重建索引之后会读到**旧的索引对象** ——
    它的行号指向已经不存在的行，查询结果会静默出错。
    """

    from mini_lancedb.dataset import clear_index_cache

    ds = configless_dataset
    ds.create_index("vector", index_type="IVF_FLAT", name="v", num_partitions=4)
    clear_index_cache()
    first = ds.load_index("v")
    assert ds.load_index("v") is first, "同一个版本应该命中缓存"

    # 追加数据**不应该**让缓存失效：新行本来就还没进索引，
    # 索引文件一个字节都没变（真实 LanceDB 里这叫 num_unindexed_rows）。
    extra = RecordBatch.from_pylist(
        [{"id": 99999, "n": 99999, "txt": "new", "vector": [0.0] * DIM}]
    )
    ds.write([extra])
    assert ds.load_index("v") is first, "追加数据不该让索引缓存失效"

    # 删行会改写索引文件（行号要重映射），key 必须跟着变
    ds.delete("id < 100")
    second = ds.load_index("v")
    assert second is not first, "删行之后索引文件变了，缓存必须失效"
    assert int(second.row_ids.max()) < ds.num_rows
    clear_index_cache()


def test_prefixed_table_forwards_io_stats(configless_dir):
    """从前缀 store 拿到的表，所有 I/O 计数恒为 0。

    ``ObjectStore.__init__`` 给每个实例新建一份 ``StoreStats``，
    前缀壳因此自带一份永远是零的计数器。不报错，只是所有 I/O 数字都是假的 ——
    而性能调优**全靠**这些数字。
    """

    db = ldb.connect(str(configless_dir / "stats_db"))
    rows = [{"id": i, "n": i, "txt": f"row {i}"} for i in range(500)]
    table = db.create_table("t", data=rows)
    scanner = table.to_lance().scanner(columns=["id"])
    scanner.to_batch()
    assert scanner.stats.bytes_read > 0, "前缀 store 没有把 I/O 统计转发出去"


def test_explain_does_not_double_count_pruned_fragments(configless_dataset):
    """``explain(analyze=True)`` 把每个片段打印两遍、计数翻倍。

    ``build_plan()`` 又跑了一次 ``_prune_fragments()``，
    而那个方法是累加的。输出里出现过"剪掉 18 个片段（总共 10 个）"
    这种明显不可能的数字。
    """

    ds = configless_dataset
    scanner = ds.scanner(columns=["id"], filter="n > 19000")
    text = scanner.explain(analyze=True)
    assert "18" not in text.split("片段总数")[1][:40]
    summary = scanner.stats.summary()
    assert summary["被 zone map 剪掉的片段"] <= summary["片段总数"]
    assert len(scanner.build_plan().pruned) == summary["片段总数"]


# ---------------------------------------------------------------------------
# 六、schema 演进：元数据撒谎
# ---------------------------------------------------------------------------


def test_alter_columns_type_change_rewrites_data(configless_dir):
    """改列类型只改 manifest、不改数据，于是 schema 和数据的类型对不上。

    症状：schema 说这一列是 ``fixed_size_list<float32>[8]``，
    读出来的数组还是 ``fixed_size_list<float64>[8]``。
    正常读路径**不会报错**，只有显式 ``validate()`` 才抓得到 ——
    这类"元数据撒谎"的 bug 会一路传播到用户的业务代码里。
    """

    store = InMemoryStore()
    rng = np.random.default_rng(0)
    batch = RecordBatch.from_pylist(
        [
            {"id": i, "v": rng.standard_normal(DIM).astype("float64").tolist()}
            for i in range(200)
        ]
    )
    ds = Dataset.create(store, "mem://t", batch.schema)
    ds.write([batch])
    ds.alter_columns({"v": f"fixed_size_list<float32>[{DIM}]"})

    out = ds.scanner().to_batch()
    out.validate()  # 不修的话这里会抛 TypeMismatchError
    assert out.column("v").type == ds.schema["v"].type
    assert out.num_rows == 200


def test_alter_columns_rejects_vector_dimension_change(configless_dir):
    """改向量维度不是类型转换，是数据变换 —— 必须明确拒绝。

    早期版本会"改成功"，然后 500 行 × 8 维被当成 1000 行 × 4 维读出来：
    行数静默翻倍。现在直接抛 ``TypeMismatchError`` 并给出提示。
    """

    store = InMemoryStore()
    rng = np.random.default_rng(1)
    batch = RecordBatch.from_pylist(
        [{"v": rng.standard_normal(8).astype("float32").tolist()} for _ in range(50)]
    )
    ds = Dataset.create(store, "mem://t", batch.schema)
    ds.write([batch])
    with pytest.raises(TypeMismatchError):
        ds.alter_columns({"v": "fixed_size_list<float32>[4]"})


def test_parse_type_handles_nested_and_parameterised_types():
    """类型字符串解析器：嵌套、带参数的写法都要能往返。

    曾经崩在 ``timestamp[us]``（只有 unit 没有 tz 时找不到 ``]``）、
    ``struct<...>``、``map<...>`` 和嵌套的 ``fixed_size_list``
    （用 ``partition(">")`` 会切在**内层**的尖括号上）。
    """

    cases = [
        "int32",
        "utf8",
        "binary",
        "list<int64>",
        "large_list<utf8>",
        "fixed_size_list<float32>[128]",
        "struct<x: int64, y: utf8>",
        "struct<x: int64, y: struct<a: utf8, b: int32>>",
        "map<utf8, int64>",
        "timestamp[us]",
        "timestamp[us, tz=UTC]",
        "decimal128(38, 10)",
        f"fixed_size_list<fixed_size_list<float16>[128]>[{DIM}]",
        "struct<tags: list<utf8>, meta: map<utf8, int64>>",
    ]
    for text in cases:
        parsed = parse_type(text)
        # 往返：把类型转成 JSON 再读回来，必须还是同一个类型
        assert parse_type(parsed.name) == parsed, text


# ---------------------------------------------------------------------------
# 七、upsert 的静默错误
# ---------------------------------------------------------------------------


def test_merge_insert_duplicate_source_keys_keep_the_last(configless_dir):
    """源数据里同一个键出现两次，目标是"更新两次"还是"只保留最后一条"？

    老实现按"每一个匹配上的源行"生成一条待追加记录，
    结果是目标表**凭空多出一行**。跑完不报错，几天后报表数字才对不上。
    """

    db = ldb.connect(str(configless_dir / "merge_db"))
    table = db.create_table(
        "t", data=[{"id": i, "v": [float(i)] * DIM} for i in range(5)]
    )
    table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
        [{"id": 2, "v": [1.0] * DIM}, {"id": 2, "v": [2.0] * DIM}]
    )
    got = table.search().where("id = 2").to_list()
    assert len(got) == 1
    assert got[0]["v"][0] == 2.0


def test_merge_insert_when_matched_delete_without_where_deletes_all_matched(configless_dir):
    """``when_matched_delete()`` 不给 where 时的语义是"匹配上的全删"。

    老实现只在给了 where 时才算"该删哪些行"，不传 where 的路径
    安静地什么都不做、还报 ``num_deleted_rows=0``。
    "以为删了其实没删"不会触发任何监控，比报错危险得多。
    """

    db = ldb.connect(str(configless_dir / "merge_db2"))
    table = db.create_table(
        "t", data=[{"id": i, "v": [float(i)] * DIM} for i in range(5)]
    )
    result = (
        table.merge_insert("id")
        .when_matched_delete()
        .when_not_matched_insert_all()
        .execute([{"id": 3, "v": [0.0] * DIM}])
    )
    assert result.num_deleted_rows == 1
    assert 3 not in {r["id"] for r in table.search().to_list()}


# ---------------------------------------------------------------------------
# 八、标签与回收：一个会丢数据的顺序错误
# ---------------------------------------------------------------------------


def test_cleanup_old_versions_keeps_tagged_versions_readable(configless_dir):
    """被标签指向的版本，它引用的数据文件不能被回收。

    老实现里 ``referenced_tags`` 是在 ``keep_set`` / ``live_files``
    **算完之后**才算的。后果是：标签保住了那个版本的 manifest，
    却保不住它的数据文件和删除文件 —— 两者已经被当成垃圾删掉了。

    这个 bug 特别值得记，因为**它的表现是"事后才炸"**：

    * ``cleanup_old_versions`` 本身成功返回，报告里还会写着
      "保留的版本 vN"，看上去完全正常；
    * 失败发生在几天后某个 ``checkout("release-1")`` 上，
      报的是 ``StoreError: 对象不存在``，指向一个 ``_deletions/`` 里的文件。

    不修会怎样：把一个发布快照打成标签、跑一次清理、然后想回到那个快照 ——
    数据已经没了，而且没有任何一步曾经报过错。标签被当成"发布保险"用，
    它却连自己的数据都保不住。
    """

    db = ldb.connect(str(configless_dir / "tag_gc_db"))
    table = db.create_table(
        "t", data=[{"id": i, "v": [float(i)] * DIM} for i in range(4)]
    )
    table.delete("id = 2")  # 有删除文件，才盖得住"删除文件也被回收"这条
    tagged_version = table.version
    table.tags.create("release-1")

    # 覆写一次，让被标签那个版本引用的片段变成"没有别的版本在引用"
    table.add([{"id": 9, "v": [9.0] * DIM}], mode="overwrite")
    assert table.version > tagged_version

    table.cleanup_old_versions(older_than=0)

    # 关键断言：清理之后，标签指向的版本仍然读得出来
    reopened = db.open_table("t")
    reopened.checkout("release-1")
    assert sorted(r["id"] for r in reopened.search().to_list()) == [0, 1, 3]


def test_cleanup_still_removes_untagged_versions(configless_dir):
    """修上面那个 bug 时不能把回收变成空操作 —— 没被标签保护的版本照样要删。"""

    db = ldb.connect(str(configless_dir / "tag_gc_db2"))
    table = db.create_table("t", data=[{"id": i, "v": [float(i)] * DIM} for i in range(3)])
    for i in range(4):
        table.add([{"id": 100 + i, "v": [float(i)] * DIM}])
    before = len(table.list_versions())
    stats = table.cleanup_old_versions(older_than=0)
    assert stats.old_versions_removed > 0
    assert len(table.list_versions()) < before
    assert table.count_rows() == 7


# ---------------------------------------------------------------------------
# 九、全 null 列：两个叠在一起的编码 bug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src_type,dst_type",
    [("float32", "float64"), ("int32", "int64"), ("int64", "int32")],
)
def test_alter_columns_on_all_null_column_stays_readable(configless_dir, src_type, dst_type):
    """把一列**全 null** 的列改类型，完了整张表必须还读得出来。

    这里曾经连着埋了两个 bug，而且第一个会**把第二个盖住**：

    1. ``DictionaryNumericEncoder.can_encode`` 只挡了 ``len(data) == 0``。
       全 null 列的 ``len(data)`` 是 N 而不是 0，于是它算出空字典、
       ``bit_width_for(-1) == 0``、cost 也等于 0 —— 一路"通过"，
       而且因为 payload 最小还会在 exhaustive 选择里**胜出**。
       解码时 ``uniq[idx]`` 拿空数组去索引，越界。
    2. 修掉 1 之后 ``ConstantEncoder`` 接手，它的元数据写的是
       ``_jsonable(np.array(0, ...))`` —— 0 维 ndarray 不是 numpy 标量，
       被原样塞进 JSON，写盘时 ``TypeError: Object of type ndarray is not
       JSON serializable``。

    不修会怎样：``alter_columns`` **提交成功并返回新版本号**，
    之后任何一次读整张表都抛 ``IndexError``。表的元数据说它是好的，
    数据却读不出来 —— 这是"最坏的一类损坏"，因为监控看不到任何异常，
    直到有人来查。
    """

    db = ldb.connect(str(configless_dir / f"allnull_{src_type}_{dst_type}"))
    table = db.create_table("t", data=[{"id": 1, "label": "a", "x": 1.0}])
    # 新加的列在已有片段上全是 null —— 这正是触发条件
    table.add_columns({"score": src_type})
    result = table.alter_columns({"score": dst_type})
    assert result.version > 0

    rows = table.search().to_list()
    assert len(rows) == 1
    assert rows[0]["score"] is None
    assert table.schema.field("score").type.name.lower() == dst_type


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def configless_dir():
    """一个用完就删的临时目录。"""

    path = Path(tempfile.mkdtemp(prefix="mini_lancedb_regress_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def configless_dataset():
    """2 万行、10 个片段的表 —— 片段数够多，剪枝才有效果可测。"""

    store = InstrumentedStore(InMemoryStore())
    n = 20_000
    rng = np.random.default_rng(0)
    vectors = rng.standard_normal((n, DIM)).astype("float32")
    rows = [
        {"id": i, "n": i, "txt": f"row {i:06d}", "vector": vectors[i].tolist()}
        for i in range(n)
    ]
    batch = RecordBatch.from_pylist(rows)
    ds = Dataset.create(store, "mem://regress", batch.schema)
    for start in range(0, n, 2000):
        ds.write([batch.slice(start, 2000)])
    return ds
