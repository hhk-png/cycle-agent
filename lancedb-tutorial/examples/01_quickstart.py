"""例 1：最小完整流程 —— 对应第 2 章。

跑法（在 lancedb-tutorial 目录下）：

    python -X utf8 examples/01_quickstart.py

Windows 上加 ``-X utf8`` 是必须的：控制台默认 GBK，
而下面的输出里有中文，不加会直接抛 UnicodeEncodeError。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mini_lancedb as ldb  # noqa: E402


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="mini_lancedb_quickstart_"))
    try:
        run(workdir)
    finally:
        # 迷你实现把表存成一个目录，跑完清掉，别把临时文件留在磁盘上
        shutil.rmtree(workdir, ignore_errors=True)


def run(workdir: Path) -> None:
    rng = np.random.default_rng(42)

    section("1. 连接：一个目录就是一个数据库")
    db = ldb.connect(str(workdir / "my_db"))
    print("db =", db)
    print("注意：connect() 不做任何 I/O，第一次真正读盘在 create_table")

    section("2. 建表：schema 从数据推断")
    docs = [
        ("Why vector search matters", "semantic retrieval over embeddings"),
        ("Columnar file formats", "Parquet versus Lance layout"),
        ("Approximate nearest neighbour", "IVF PQ and HNSW tradeoffs"),
        ("Versioning and time travel", "immutable files and a version manifest"),
        ("Full text search", "BM25 scoring over an inverted index"),
        ("Hybrid retrieval", "BM25 plus embeddings and reranking"),
    ]
    rows = [
        {
            "id": i,
            "title": title,
            "text": body,
            "vector": rng.standard_normal(64).astype("float32").tolist(),
        }
        for i, (title, body) in enumerate(docs)
    ]
    tbl = db.create_table("notes", data=rows)
    print("table =", tbl)
    print()
    print(tbl.schema.pretty())
    print()
    print("注意 version=2：create_table 内部先建空表(v1)再写数据(v2)")

    section("3. 建索引")
    tbl.create_index("text", config="FTS", name="fts_idx")
    tbl.create_index(
        "vector", config="IVF_PQ", name="v_idx", num_partitions=2, num_sub_vectors=8
    )
    for info in tbl.list_indices():
        print(" ", info.describe())

    section("4. 向量检索：_distance 越小越好")
    for r in tbl.search(rows[2]["vector"]).limit(3).to_list():
        print(f"  {r['_distance']:9.3f}  {r['title']}")

    section("5. 全文检索：_score 越大越好（方向相反！）")
    for r in tbl.search("inverted index", query_type="fts").limit(3).to_list():
        print(f"  {r['_score']:9.4f}  {r['title']}")

    section("6. 混合检索：两路召回 + RRF 重排")
    try:
        q = tbl.search(rows[5]["vector"], query_type="hybrid").text(
            "reranking embeddings"
        )
        for r in q.limit(3).to_list():
            score = r.get("_relevance_score")
            print(f"  {score}  {r['title']}")
    except Exception as exc:  # pragma: no cover - 失败时给出可读信息
        print("  混合检索失败:", type(exc).__name__, exc)

    section("7. 纯标量查询：走列扫描，不碰向量索引")
    print("  ", [r["title"] for r in tbl.search().where("id >= 3").limit(10).to_list()])

    section("8. 增删改：每一次都是一个新版本")
    tbl.tags.create("v0.1")
    tbl.add(
        [
            {
                "id": 6,
                "title": "Streaming ingestion",
                "text": "batch writes and compaction",
                "vector": rng.standard_normal(64).astype("float32").tolist(),
            }
        ]
    )
    print("  add 之后  :", tbl)
    tbl.delete("id = 0")
    print("  delete 之后:", tbl)

    section("9. 时间旅行")
    tbl.checkout("v0.1")
    print(f"  checkout('v0.1') -> version={tbl.version} rows={tbl.count_rows()}")
    tbl.restore()
    print(f"  restore()        -> version={tbl.version} rows={tbl.count_rows()}")
    print("  注意 restore 是把旧内容作为**新版本**提交回来，不是回退指针")

    section("10. 维护")
    result = tbl.optimize()
    print("  optimize ->", type(result).__name__)
    print("  ", result.report() if hasattr(result, "report") else result)

    section("11. 表统计")
    stats = tbl.stats()
    # ``stats()`` 返回一个结构化的 TableStatistics 对象（不是裸 dict），
    # 想按 dict 用就调 ``to_dict()``；``report()`` 会渲染成对齐的文本表。
    print(stats.report())


if __name__ == "__main__":
    main()
