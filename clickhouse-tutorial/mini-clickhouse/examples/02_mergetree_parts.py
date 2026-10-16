# -*- coding: utf-8 -*-
r"""02 —— MergeTree 的 "part" 到底是什么。

这个示例展示 ClickHouse 存储里**最核心的一个概念**:一张 MergeTree 表在磁盘上
不是一个文件,而是**一堆叫 part 的不可变目录**。每做一次 ``INSERT``,就落一个
新 part;后台再把小 part 合并成大 part。

看输出时盯住 ``system.parts`` 的这几列(每一列回答一个问题):

.. code-block:: text

   partition   数据属于哪个分区(PARTITION BY 算出来的值)
   name        part 的名字,格式 <partition>_<min_block>_<max_block>_<level>
   rows        这个 part 里有多少行
   bytes_on_disk 这个 part 占了多少字节
   level       这个 part 被合并过几次(0 = 从没合并过)
   marks       稀疏索引的"标记"数量(颗粒度 = index_granularity,默认 8192 行)

**part 名字本身就是一部小历史**:``202401_1_1_0`` 读作"分区 202401、覆盖块号
1..1、第 0 层" —— 一次 INSERT 造一个 part,所以 ``min_block == max_block``;
两个 part 合并后,``min``/``max`` 取并集、``level`` 加一。

注意一个和线上版本的差异:mini-clickhouse 的 ``system.parts`` **没有** ``database``
和 ``table`` 两列(只列当前进程里所有 MergeTree 表的 part),所以下面的查询不能
写 ``WHERE table = ...`` —— 直接 ``SELECT`` 就是全部。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.execution.context import Context  # noqa: E402


def show(ctx: Context, sql: str) -> None:
    print(f"\n>>> {sql}")
    print(ctx.execute(sql).text())


def main() -> None:
    print("=" * 72)
    print("02 · MergeTree 的 part —— 一次 INSERT 一个 part,名字里写着它的来历")
    print("=" * 72)

    data_dir = tempfile.mkdtemp(prefix="minich02-")
    ctx = Context(data_path=data_dir)          # 给了 data_path,数据才真的落盘
    try:
        # PARTITION BY toYYYYMM(day) —— 按月分区,方便一次看到多个分区。
        show(ctx, "CREATE TABLE hits (id UInt64, day Date, url String) "
                  "ENGINE = MergeTree PARTITION BY toYYYYMM(day) ORDER BY id")

        # 三次 INSERT → 三个 part。
        batches = [
            ("2024-01-05", [("2024-01-05", 1), ("2024-01-06", 2)]),
            ("2024-02-10", [("2024-02-10", 3), ("2024-02-11", 4), ("2024-02-12", 5)]),
            ("2024-01-20", [("2024-01-20", 6)]),
        ]
        for day, rows in batches:
            values = ", ".join(f"({i}, '{d}', '/p/{i}')" for d, i in rows)
            show(ctx, f"INSERT INTO hits VALUES {values}")

        print("\n" + "-" * 72)
        print("看 system.parts:三次 INSERT 应该出现三个 part、跨两个分区")
        print("-" * 72)
        show(ctx, "SELECT partition, name, rows, bytes_on_disk, level, marks "
                  "FROM system.parts ORDER BY partition, name")

        print("""
读法(逐个 part 对照):

  · name 的格式是 <partition>_<min_block>_<max_block>_<level>。
    ``202401_1_1_0`` = 分区 202401、块号 1..1、level 0。块号区间只有 1..1,
    说明它是**单次 INSERT 的直系后代**,还没被合并过。
  · 块号是**整张表**递增的,不是每个分区各自从 1 开始 —— 所以第二次 INSERT
    落进 202402 分区的 part 才叫 ``202402_2_2_0``(它用的是全局的第 2 块),
    第三次 INSERT 落进 202401 分区的 part 叫 ``202401_3_3_0``。
  · 于是 202401 分区有两块(`_1_1_0` 和 `_3_3_0`)—— 同一个分区里多个 part
    是完全允许的,这也是后台合并要处理的对象。
  · level 全是 0:还没发生过合并。下一个示例 03 就专门演示合并。
""")

        print("-" * 72)
        print("按分区汇总:同一份数据被切成了几块、各占多大")
        print("-" * 72)
        # 注意:别名别和源列重名(比如把 sum(rows) 又叫成 rows),
        # 否则解析器会把 `rows` 当成"指向自己的别名"而报 cyclic aliases。
        show(ctx, "SELECT partition, count() AS n_parts, sum(rows) AS n_rows, "
                  "sum(bytes_on_disk) AS n_bytes "
                  "FROM system.parts GROUP BY partition ORDER BY partition")

        print("\n提示:part 越小越多,查询要打开的文件就越多 —— 这就是")
        print("      'too many parts' 为什么是 ClickHouse 最常见的写放大告警。")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()
