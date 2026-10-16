# -*- coding: utf-8 -*-
r"""05 —— 数据格式:同一份结果,七种写法;以及 NULL 和空字符串的区别。

ClickHouse 里 "FORMAT" 是一个**一等公民**:同一次查询,你可以要 TSV、要 CSV、
要 JSON、要一看就懂的方框图。协议层只认 ``Iterator[Block]``,格式层把它渲染成
文本或二进制 —— 所以"换格式"不重跑查询。

本示例拿一张**故意放了 NULL 和空串**的小表,依次渲染成:

.. code-block:: text

   TSV           制表符分隔,NULL 写成 \N,空串就是"空字段"
   CSV           逗号分隔,NULL 也是 \N,空串被引成 ""
   JSONEachRow   每行一个 JSON 对象,null 和 "" 泾渭分明
   PrettyCompact 终端里最好看的方框图(clickhouse-client 的默认)
   Values        反过来能当 INSERT ... VALUES 用(可回读)
   Native        二进制列式格式(本实现未完成,见下)

**整篇的重点是一句话:NULL 和空字符串是两个东西。** 盯住输出里的:

* TSV 第一行是 ``\N <TAB> <TAB> \N`` —— 两头是 NULL(哨兵 ``\N``),
  中间那一列是**空字符串**(两个制表符之间什么都没有);
* CSV 第一行是 ``\N,"",\N`` —— 空串被显式引成 ``""``,NULL 仍是裸的 ``\N``;
* JSONEachRow 里 NULL 是 ``null``、空串是 ``""``。

最后我们把 TSV 文本**读回来**,证明这两种值能被无损还原 —— 这就是"格式可往返"。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.block import Block, ColumnWithTypeAndName  # noqa: E402
from minich.execution.context import Context  # noqa: E402
from minich.formats import get_input_format  # noqa: E402
from minich.types import get_data_type  # noqa: E402

# 一张把"NULL / 空串 / 有值"三种情况都放进去的小表。
SETUP = [
    "CREATE TABLE fmts (a Nullable(String), b String, c Nullable(UInt8)) ENGINE = Memory",
    "INSERT INTO fmts VALUES (NULL, '', NULL), ('x', 'y', 5)",
]
OUTPUT_FORMATS = ["TSV", "CSV", "JSONEachRow", "Values", "PrettyCompact"]


def main() -> None:
    print("=" * 70)
    print("05 · 数据格式 —— NULL ≠ 空字符串,而且同一份结果能写成多种格式")
    print("=" * 70)

    ctx = Context()
    try:
        for sql in SETUP:
            ctx.execute(sql)

        for fmt in OUTPUT_FORMATS:
            print(f"\n--- {fmt} ---")
            # text(fmt) 就是"用 fmt 渲染这次查询结果"。
            rendered = ctx.execute("SELECT * FROM fmts").text(fmt)
            if fmt in ("TSV", "CSV", "Values"):
                print(repr(rendered))     # 用 repr 才看得见制表符/换行
            else:
                print(rendered)
            if fmt == "TSV":
                print("  ^ 第一行 `\\N\\t\\t\\N`:列 a 和 c 是 NULL(哨兵 \\N),"
                      "列 b 是**空字符串**(两个 tab 之间空的)")
            if fmt == "CSV":
                print("  ^ 第一行 `\\N,\"\",\\N`:NULL 仍是 \\N,空串被引成 \"\"")

        print("\n--- Native(二进制列式格式)---")
        # .. note::
        #
        #    **二进制格式不能走 `QueryResult.text()`。** 那条路返回的是
        #    ``str`` —— 文本格式的契约;而 Native/RowBinary 产出的是**字节**。
        #    早期版本在这里用 `.text("Native")` 去取,拿到的失败被归因成
        #    "引用了未定义的错误码",其实真正的原因是**输出目标只接受 str**。
        #
        #    正确的用法是直接拿格式对象,把它写进一个 ``bytearray``。
        #    (这条区分本身也是 ClickHouse 的一个真实设计:文本格式与二进制
        #    格式在客户端走的是**两条不同的路径**,前者可以边算边发,
        #    后者要按块序列化。)
        from minich.formats import get_output_format

        result = ctx.execute("SELECT * FROM fmts")
        blocks = result.blocks()
        header = blocks[0].clone_empty() if blocks else result.header
        fmt = get_output_format("Native")
        out = bytearray()
        fmt.write_prefix(header, out, ctx.settings)
        for b in blocks:
            fmt.write(b, out, ctx.settings)
        fmt.write_suffix(header, out, ctx.settings)
        print(f"  输出 {len(out)} 字节(含表头、列类型与数据)")

        # 往返:把刚写出的字节读回来,应当得到同样的行
        from minich.formats import get_input_format

        back = get_input_format("Native")
        rows_back = []
        for b in back.read(bytes(out), header, ctx.settings):
            for i in range(b.rows()):
                rows_back.append(tuple(c.column.get(i) for c in b.data))
        print(f"  读回来 {len(rows_back)} 行: {rows_back}")
        print("  ^ 注意 NULL 与空串都完整保住了 —— 这是二进制格式相对 CSV 的优势")

        print("\n--- 把 TSV 文本读回来(证明 NULL 与空串可无损往返)---")
        header = Block([
            ColumnWithTypeAndName("a", get_data_type("Nullable(String)"),
                                  get_data_type("Nullable(String)").create_column()),
            ColumnWithTypeAndName("b", get_data_type("String"),
                                  get_data_type("String").create_column()),
            ColumnWithTypeAndName("c", get_data_type("Nullable(UInt8)"),
                                  get_data_type("Nullable(UInt8)").create_column()),
        ])
        tsv_text = ctx.execute("SELECT * FROM fmts").text("TSV")
        reader = get_input_format("TSV")
        parsed = header.clone_empty()
        for block in reader.read(tsv_text, header, ctx.settings):
            parsed.merge(block)
        # Block.rows() 是"行数",不是行本身 —— 逐列取值拼出元组。
        restored = [tuple(col.column.get(i) for col in parsed.data)
                    for i in range(parsed.rows())]
        print(f"  读回的 TSV 文本: {tsv_text!r}")
        print(f"  还原成行        : {restored}")
        print("  第 1 行第 1 个是 None(NULL),第 2 个是 ''(空串)—— 没有串味。")

        print("\n小结:选格式看用途 —— 人看用 PrettyCompact,机器传用 TSV/CSV/JSON,")
        print("      判 NULL 永远靠哨兵(\\N / null),别用'字段是不是空的'去猜。")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()
