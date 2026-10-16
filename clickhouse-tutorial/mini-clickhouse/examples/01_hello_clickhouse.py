# -*- coding: utf-8 -*-
r"""01 —— 最小可运行的 "Hello, ClickHouse"。

这个示例回答一个问题:**把 mini-clickhouse 当库用,最短的一条路是什么?**

它只做四件事,做完你就能读懂后面所有例子:

1. 造一个 ``Context`` —— 它相当于真实 ClickHouse 的一个 **server 进程**;
2. 建一张内存表(``ENGINE = Memory``)—— 数据不进磁盘,进程退出就没了;
3. 用 ``INSERT ... VALUES`` 写几行;
4. 用 ``SELECT`` 读回来,并且**每次都先把 SQL 打出来再打结果**。

看输出时注意三件事:

* 每条 SQL 前面都有 ``>>>`` 前缀 —— 好让你把"输入"和"结果"对上;
* ``QueryResult.text()`` 默认就是 ``clickhouse-client`` 的 ``PrettyCompact``
  (带方框的表格),这也是"和真客户端长得一样"的原因;
* ``QueryResult.scalar()`` / ``column_types()`` 是程序里最常用的两个取值方式 ——
  单行单列用 ``scalar()``,想知道列的类型用 ``column_types()``。

``Context`` 用一个 ``try/finally`` 包住并在最后 ``shutdown()``:这是本项目所有
示例的统一写法。内存表其实不需要它,但一旦用了 MergeTree,后台合并线程就会
在这里被停掉 —— 不写 ``shutdown()`` 读者会遇到"脚本跑完了终端却卡住"。
"""

from __future__ import annotations

import os
import sys

# 让 `python examples/xx.py` 无论从哪个目录调用都能 import 到 minich。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Windows 控制台默认 GBK,方框字符和中文会把它逼疯 —— 统一改 UTF-8。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.execution.context import Context  # noqa: E402


def run(ctx: Context, sql: str) -> None:
    """执行一条 SQL 并打印 —— 先打 SQL,再打结果。"""
    print(f"\n>>> {sql}")
    result = ctx.execute(sql)
    text = result.text()
    if text.strip():
        print(text)
    else:
        # DDL / INSERT 没有结果集,text() 是空串。
        print("(no result set — statement executed)")


def main() -> None:
    print("=" * 64)
    print("01 · Hello, ClickHouse —— Context → 建表 → 写入 → 查询")
    print("=" * 64)

    # 不传 data_path 就是纯内存的 server —— 对应 "不落盘" 的场景。
    ctx = Context()
    try:
        run(ctx, "CREATE TABLE hello (id UInt64, name String, score Float64) "
                 "ENGINE = Memory")

        run(ctx, "INSERT INTO hello VALUES "
                 "(1, 'alice', 90.5), (2, 'bob', 72.0), (3, 'carol', 88.25)")

        run(ctx, "SELECT * FROM hello ORDER BY id")

        run(ctx, "SELECT name, score FROM hello WHERE score >= 80 ORDER BY score DESC")

        run(ctx, "SELECT count() AS n, avg(score) AS avg_score FROM hello")

        # 想只要"一个值"就 scalar();想知道"这列是什么类型"就 column_types()。
        n = ctx.execute("SELECT count() FROM hello").scalar()
        types = ctx.execute("SELECT * FROM hello").column_types()
        print("\n--- 用 Python 取值(而不是看表格)---")
        print(f"count()        -> {n}")
        print(f"列类型          -> {types}")
        print("\n提示:Memory 引擎的数据只在内存里,脚本一结束就没了。")
        print("      下一个示例 02_mergetree_parts.py 会把数据真正写到磁盘上。")
    finally:
        # 必须调用:即便这里是内存表,也养成习惯 —— MergeTree 的后台线程靠它收尾。
        ctx.shutdown()


if __name__ == "__main__":
    main()
