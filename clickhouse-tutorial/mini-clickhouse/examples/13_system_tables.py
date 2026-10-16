# -*- coding: utf-8 -*-
r"""13 —— 可观测性之旅:用 system 表回答运维问题。

ClickHouse 把"服务端自己的状态"也做成**普通表**。好处是:你不需要另一套监控
API,``SELECT`` 就能查。本示例挨个看六张最常用的表,每张配一句"它回答什么问题":

.. code-block:: text

   system.tables     有哪些表、各自什么引擎、多少行/多少字节   → "谁在占地方?"
   system.parts      一张 MergeTree 在磁盘上被切成了多少块      → "是不是 too many parts?"
   system.query_log  刚跑过的查询:耗时、读了多行、成没成功      → "刚才那条为什么慢/为什么错?"
   system.events     **累积**计数器(从启动到现在)            → "一共插了多少行、压了多少字节?"
   system.metrics    **瞬时**值(此刻)                        → "现在有几个查询在跑?"
   system.settings   所有可调参数及当前值                      → "这个 setting 现在是什么值?"

关键区别(很容易记混):

* ``system.events`` 是**单调递增的累计量**(像 Prometheus 的 counter),
  看它要算**差值**;
* ``system.metrics`` 是**此刻的即时值**(像 gauge),直接看就行。

示例先跑几条查询"制造点历史",再逐张表去看 —— 这样 query_log / events 里才有内容。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.errors import MiniChError  # noqa: E402
from minich.execution.context import Context  # noqa: E402


def show(ctx: Context, question: str, sql: str) -> None:
    print(f"\n### {question}")
    print(f">>> {sql}")
    try:
        print(ctx.execute(sql).text())
    except MiniChError as exc:
        print(f"[{exc.name}] {exc.message}")


def main() -> None:
    print("=" * 78)
    print("13 · 可观测性 —— 六张 system 表,六个运维问题")
    print("=" * 78)

    ctx = Context()
    try:
        # 先"制造历史":建表、写数据、跑几条查询、踩一个错。
        ctx.execute("CREATE TABLE web (ts UInt32, url String, status UInt16) "
                    "ENGINE = MergeTree ORDER BY ts")
        ctx.execute("INSERT INTO web SELECT number + 1700000000, "
                    "concat('/p/', toString(number % 50)), "
                    "toUInt16(200 + number % 5 * 10) FROM numbers(20000)")
        ctx.execute("SELECT count() FROM web")
        ctx.execute("SELECT url, count() FROM web GROUP BY url ORDER BY count() DESC LIMIT 3")
        try:
            ctx.execute("SELECT * FROM no_such_table")     # 故意制造一次失败
        except MiniChError:
            pass

        show(ctx, "① 谁在占地方?—— system.tables",
             "SELECT name, engine, total_rows, total_bytes, formatReadableSize(total_bytes) AS size "
             "FROM system.tables WHERE database = 'default' ORDER BY total_bytes DESC")
        print("  一行一张表:引擎、总行数、总字节。挑大表先看这里。")

        show(ctx, "② 数据被切成几块?—— system.parts",
             "SELECT partition, name, rows, bytes_on_disk, level "
             "FROM system.parts WHERE active ORDER BY name LIMIT 5")
        print("  这是 MergeTree 在磁盘上的真实切分。注意 mini-clickhouse 的 system.parts")
        print("  没有 database/table 两列(只列本进程所有 MergeTree 表)。")

        show(ctx, "③ 刚才那条查询怎么样?—— system.query_log",
             "SELECT type, count() AS n FROM system.query_log GROUP BY type ORDER BY n DESC")
        show(ctx, "   最近几条查询的耗时与读行数",
             "SELECT query_duration_ms AS ms, read_rows, substr(query, 1, 50) AS q "
             "FROM system.query_log ORDER BY event_time DESC, query_id DESC LIMIT 5")
        print("  type 里 QueryFinish = 成功,ExceptionWhileProcessing = 跑挂了 ——")
        print("  上面那次 no_such_table 的失败就记在这。")

        show(ctx, "④ 一共干了多少活?—— system.events(累积计数器,看差值)",
             "SELECT event, value FROM system.events WHERE value > 0 "
             "ORDER BY value DESC LIMIT 8")
        print("  InsertedRows / CompressedSize 这些只会往上走,是 Prometheus 式的 counter。")

        show(ctx, "⑤ 现在是什么状态?—— system.metrics(瞬时值)",
             "SELECT metric, value FROM system.metrics WHERE value > 0 ORDER BY metric")
        print("  像 Query(正在跑的查询数)这种是 gauge,直接读当时的值。")

        print("\n### ⑥ 这个 setting 现在是多少?—— system.settings")
        try:
            ctx.execute("SET max_block_size = 4096")       # 改一个,让它 changed=1
            print(">>> SET max_block_size = 4096")
        except MiniChError as exc:
            print(f"  (SET 未生效: {exc.name})")
        show(ctx, "   只看被改过的 setting",
             "SELECT name, value, changed, type FROM system.settings "
             "WHERE changed = 1 ORDER BY name LIMIT 10")
        show(ctx, "   顺便看总数",
             "SELECT count() AS total_settings, countIf(changed) AS n_changed "
             "FROM system.settings")
        print("  改过的 setting 会置 changed=1 —— 排查'为什么行为和我预期不一样'时,")
        print("  先看这里有没有人偷偷改过默认值。")

        print("""
小结:

  · events = 累计(counter),metrics = 瞬时(gauge) —— 别混。
  · query_log 是"事后诸葛亮":慢查询、报错、读了多少行,全在它里面。
  · 这些表都是普通表:能 WHERE、能 GROUP BY、能 JOIN 别的表 —— 这就是
    "把可观测性做成 SQL"的威力。
""")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()
