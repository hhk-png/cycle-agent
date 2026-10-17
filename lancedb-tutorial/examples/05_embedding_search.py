"""例 5：嵌入函数与"向量是谁生成的" —— 对应第 22 章。

这个脚本演示三件事：

1. 用**完全离线**的嵌入函数把文本转成向量（不需要 API key、不联网）；
2. 把这些向量写进表，并把"产生它们的函数名和维度"记进 schema 元数据；
3. 关掉连接、重新打开表，验证那条元数据还在。

第 2、3 步是这一章的重点：**表必须自己记得自己的向量是哪个模型产生的**。
否则换模型的那一刻，新旧向量不在同一个语义空间里，
距离计算失去意义 —— 而且不会报任何错。

跑法：  python -X utf8 examples/05_embedding_search.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mini_lancedb as ldb  # noqa: E402
from mini_lancedb.embedding import (  # noqa: E402
    CharNgramEmbeddingFunction,
    HashingEmbeddingFunction,
    get_registry,
)
from mini_lancedb.embedding.base import EmbeddingFunctionConfig  # noqa: E402
from mini_lancedb.embedding.providers import describe_providers  # noqa: E402

DOCS = [
    "Lance 文件格式用 40 字节的 footer 定位全部元数据",
    "IVF_PQ 把向量切成子空间分别量化，用查表算距离",
    "删除不重写数据文件，只写一个记录行号的删除文件",
    "manifest 是不可变的版本快照，时间旅行就是读另一个 manifest",
    "BM25 的 _score 越大越好，向量检索的 _distance 越小越好",
    "压实会合并小片段，代价是压低统计剪枝的粒度",
]


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def registry_tour() -> None:
    section("注册表：所有可用的嵌入函数")
    registry = get_registry()
    for name in registry.names():
        print(f"  {name}")

    section("在线模型的接口表（本教程不实际调用它们）")
    for row in describe_providers():
        flag = "需要 key" if row["needs_api_key"] else "本地可用"
        print(
            f"  {row['name']:<22}{row['typical_model']:<28}"
            f"{row['dims']:>5} 维  {flag}"
        )


def offline_quality() -> None:
    section("离线嵌入函数：它们真的能区分语义吗？")
    hashing = HashingEmbeddingFunction(dim=256)
    chargram = CharNgramEmbeddingFunction(dim=256)

    # 刻意做**同语言**的对照：跨语言时词形重叠天然为零，
    # 那测的不是嵌入函数的好坏，而是"你选错了模型"这件事。
    pairs = [
        ("vector index", "build a vector index over embeddings"),
        ("columnar format", "the columnar file format layout"),
        ("删除操作", "删除只写一个记录行号的文件"),
        ("版本快照", "manifest 是不可变的版本快照"),
    ]
    print(f"  {'查询':<18}{'相关文档':<34}{'hashing':>9}{'char-ngram':>12}")
    print("  " + "-" * 74)
    for query, related in pairs:
        h = cosine(
            hashing.compute_query_vectors([query])[0],
            hashing.compute_source_vectors([related])[0],
        )
        c = cosine(
            chargram.compute_query_vectors([query])[0],
            chargram.compute_source_vectors([related])[0],
        )
        print(f"  {query:<18}{related:<34}{h:>9.3f}{c:>12.3f}")
    print()
    print("  一个不太符合直觉的实测结论：**char-ngram 全面弱于 hashing**，")
    print("  中文上也是。原因不是它设计得差，而是本实现的 hashing")
    print("  在分词时**已经对 CJK 逐字（unigram）切开** ——")
    print("  它早就吃到了「不需要分词」这个红利，")
    print("  而 char-ngram 还要额外付出向量更稠密、粒度更粗的代价。")
    print("  （走字符二元组的是另一条线：全文检索的 tokenizer。）")
    print()
    print("  所以「哪个兜底嵌入更好」取决于你的分词器，不能只看名字。")
    print("  换成对中文一窍不通的空白符分词器，char-ngram 会立刻反超。")
    print("  这类「理论上更好、实测更差」的结论只能靠量，不能靠推。")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="mini_lancedb_embed_"))
    try:
        registry_tour()
        offline_quality()
        run(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run(workdir: Path) -> None:
    embedder = HashingEmbeddingFunction(dim=64)
    vectors = embedder.compute_source_vectors(DOCS)

    section("写入：向量 + 生成者的元数据")
    db = ldb.connect(str(workdir / "demo_db"))
    table = db.create_table(
        "notes",
        data=[
            {"id": i, "text": text, "vector": np.asarray(v, dtype="float32").tolist()}
            for i, (text, v) in enumerate(zip(DOCS, vectors))
        ],
        embedding_functions=[
            EmbeddingFunctionConfig(
                source_column="text", vector_column="vector", function=embedder
            )
        ],
    )
    print("  表:", table)
    print("  schema 元数据:", table.schema.metadata)

    section("检索")
    for query, expected in (("文件格式 footer", 0), ("量化 子空间", 1), ("压实 片段", 5)):
        qv = np.asarray(embedder.compute_query_vectors([query])[0], dtype="float32")
        hits = table.search(qv).limit(2).to_list()
        top = hits[0]
        mark = "OK" if top["id"] == expected else "MISS"
        print(f"  [{mark}] {query!r:<22}-> {top['text'][:34]} (id={top['id']})")

    section("关键验证：换一个进程/连接，元数据还在吗")
    del table, db
    db2 = ldb.connect(str(workdir / "demo_db"))
    reopened = db2.open_table("notes")
    metadata = reopened.schema.metadata
    print("  重新打开后的元数据:", metadata)
    assert "embedding_functions" in metadata, "元数据没有持久化！"
    print("  向量列的类型:", reopened.schema["vector"].type)
    print()
    print("  换模型的时候，读这条元数据就知道哪些表的向量需要重算。")
    print("  没有它，你只能靠记忆 —— 而记忆不写进版本控制。")


if __name__ == "__main__":
    main()
