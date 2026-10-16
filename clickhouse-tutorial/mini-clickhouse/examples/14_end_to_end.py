# -*- coding: utf-8 -*-
r"""14 —— 端到端:一条"迷你分析流水线",从造数到出报表(约 20 万行)。

整套示例的**收官作**,把前面所有零件拼成一条真实链路:

.. code-block:: text

   ① 造数    numbers() + 字符串/整型函数 → 20 万行合成 web 日志
   ② 建模    MergeTree,PARTITION BY day(按天分区)、ORDER BY ts(按时间排序)
   ③ 报表    "热门页面 / 每日独立访客 / 响应大小分位数 / 分区裁剪" 四问
   ④ 存储    system.parts:OPTIMIZE 前后,一个分区里的 part 2 个 → 1 个
   ⑤ 成本    每条查询的"钱"花在哪(列裁剪 / 分区裁剪 / 聚合要装多少状态)

看输出时注意两件事:

* 建模那两行决定了后面查询能少扫多少数据 —— ``PARTITION BY day`` 让"只看某一天"
  能跳过其余 part;列式存储则只读 ``SELECT`` 里真正用到的列;
* 四条报表**都扫了 20 万行**,差别只在**聚合要装多少状态**:低基数分组 < 基数
  估计(uniq) < 精确去重(uniqExact)/ 分位数。这决定了它们谁贵谁便宜。

数据是**确定性**生成的(固定算式,不含随机种子),所以每次跑行数/结果都一样。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.execution.context import Context  # noqa: E402

ROWS = 200_000


def timed(ctx: Context, sql: str, title: str) -> tuple[float, int]:
    """跑一条查询,打印 SQL/耗时/结果,返回 (耗时秒, 结果行数)。"""
    print(f"\n>>> {title}")
    print(f">>> {sql}")
    t0 = time.perf_counter()
    result = ctx.execute(sql)
    dt = time.perf_counter() - t0
    print(result.text())
    print(f"    [耗时 {dt * 1000:.0f} ms]")
    return dt, result.row_count()


def main() -> None:
    print("=" * 78)
    print("14 · 端到端迷你分析流水线 —— 20 万行 web 日志,从造数到报表")
    print("=" * 78)

    data_dir = tempfile.mkdtemp(prefix="minich14-")
    ctx = Context(data_path=data_dir)
    total = 0.0
    try:
        # ── ① 建模 ────────────────────────────────────────────────────
        # merge_selecting_sleep_ms 拉长:让 part 数在 OPTIMIZE 之前保持稳定,
        # 好让"前后对比"可复现(否则后台线程会先把 part 合掉)。
        ctx.execute(
            "CREATE TABLE logs ("
            "  ts     UInt32 CODEC(Delta, ZSTD(3)),"
            "  day    UInt16,"
            "  path   String CODEC(ZSTD(3)),"
            "  status UInt16,"
            "  bytes  UInt32"
            ") ENGINE = MergeTree PARTITION BY day ORDER BY ts "
            "SETTINGS merge_selecting_sleep_ms = 100000"
        )

        # ── ② 造数:numbers() + 字符串函数,确定性伪随机 ──────────────
        # 7 天窗口(day 0..6),200 个页面,约 90% 成功 / 少数 4xx/5xx。
        # 分**两批**各 ROWS/2 行 —— 这样每个分区会有 2 个 part,
        # 后面 OPTIMIZE 才有 part 可合。
        batches = 2
        per = ROWS // batches
        print(f"\n>>> 生成 {ROWS:,} 行合成日志({batches} 批 × {per:,} 行 → 每批 N 个 part)")
        t0 = time.perf_counter()
        for b in range(batches):
            offset = b * per
            ctx.execute(
                f"INSERT INTO logs "
                f"SELECT toUInt32(1700000000 + (number + {offset}) % 604800), "  # 一周内的时间戳
                f"       toUInt16((number + {offset}) % 7), "
                f"       concat('/api/v1/', toString((number + {offset}) * 2654435761 % 200)), "
                f"       toUInt16(if((number + {offset}) % 97 = 0, 500, "
                f"                   if((number + {offset}) % 31 = 0, 404, 200))), "
                f"       toUInt32((number + {offset}) * 40503 % 20000) "
                f"FROM numbers({per})"
            )
        insert_s = time.perf_counter() - t0
        total += insert_s
        n = ctx.execute("SELECT count() FROM logs").scalar()
        print(f"    写入 {n:,} 行,用时 {insert_s:.2f}s")

        before_parts = ctx.execute(
            "SELECT count() FROM system.parts WHERE active").scalar()
        print(f"    当前 part 数 = {before_parts}(7 个分区 × 2 批)")

        # ── ③ 报表查询 ────────────────────────────────────────────────
        print("\n" + "=" * 78)
        print("③ 三张典型分析报表")
        print("=" * 78)

        d1, _ = timed(
            ctx,
            "SELECT path, count() AS hits, round(avg(bytes), 0) AS avg_bytes, "
            "       countIf(status >= 400) AS errors "
            "FROM logs GROUP BY path ORDER BY hits DESC, path LIMIT 8",
            "报表 A:热门页面 Top 8(低基数分组 —— 便宜)",
        )

        d2, _ = timed(
            ctx,
            "SELECT day, uniqExact(path) AS uniques, count() AS hits "
            "FROM logs GROUP BY day ORDER BY day",
            "报表 B:每日独立访客(uniqExact 要存下所有值 —— 贵)",
        )

        d3, _ = timed(
            ctx,
            "SELECT quantile(0.5)(bytes) AS p50, quantile(0.95)(bytes) AS p95, "
            "       max(bytes) AS peak FROM logs",
            "报表 C:响应大小分位数(要全表扫描 + 草图 —— 贵)",
        )

        # 分区裁剪:WHERE 命中单个分区时,只开那一个 part。
        d4, _ = timed(
            ctx,
            "SELECT count() AS hits_day0 FROM logs WHERE day = 0",
            "报表 D:只看 day=0 —— 分区裁剪,只开 1 个 part",
        )

        total += d1 + d2 + d3 + d4

        # ── ④ 看存储:OPTIMIZE 前后 ──────────────────────────────────
        print("\n" + "=" * 78)
        print("④ system.parts:OPTIMIZE 前后")
        print("=" * 78)
        print("\n[合并前] 每个分区 2 个 part:")
        print(ctx.execute(
            "SELECT partition, name, rows, bytes_on_disk, level "
            "FROM system.parts WHERE active ORDER BY partition").text())

        # 只优化一个分区(PARTITION 子句)—— 让示例保持"快"。
        # 不加 PARTITION 的 `OPTIMIZE TABLE logs FINAL` 会按同样机制把**每个**
        # 分区都合到 1 个 part,但 20 万行全表重写要 ~10s,这里刻意只做一格。
        t0 = time.perf_counter()
        ctx.execute("OPTIMIZE TABLE logs PARTITION 0 FINAL")
        optimize_s = time.perf_counter() - t0
        total += optimize_s
        after_parts = ctx.execute(
            "SELECT count() FROM system.parts WHERE active").scalar()
        after_rows = ctx.execute(
            "SELECT sum(rows) FROM system.parts WHERE active").scalar()

        print(f"\n[合并后]  OPTIMIZE TABLE logs PARTITION 0 FINAL(用时 {optimize_s:.2f}s)")
        print(ctx.execute(
            "SELECT partition, name, rows, bytes_on_disk, level "
            "FROM system.parts WHERE active ORDER BY partition").text())
        print(f"    分区 0:2 个 part → 1 个(level 0 → 1);")
        print(f"    总 part 数:{before_parts} → {after_parts};"
              f"总行数 {after_rows:,}(一行不少)")
        print("    对每个分区都跑一次,就是 `OPTIMIZE TABLE logs FINAL` 的全表效果。")

        # ── ⑤ 成本小结 ───────────────────────────────────────────────
        print("\n" + "=" * 78)
        print("⑤ 每条查询的'钱'花在哪")
        print("=" * 78)
        print(f"""
  写入     {insert_s:5.2f}s  —— 2 批 INSERT,每批落成分区 part;列式 + codec 写了 {ROWS:,} 行
  报表 A   {d1:5.2f}s  —— 热门页面:扫 path/status/bytes 三列,按低基数键聚合,便宜
  报表 B   {d2:5.2f}s  —— 每日独立访客:uniqExact 要**存下所有 path** 去重,内存换准确
  报表 C   {d3:5.2f}s  —— 分位数:全表扫 bytes + 维护分位数草图,贵在"没有索引能帮你聚合"
  报表 D   {d4:5.2f}s  —— day=0 分区裁剪:只开 1 个 part,其余 6 个直接跳过(最便宜)
  OPTIMIZE {optimize_s:5.2f}s  —— 只合了 1 个分区(2→1);全表 OPTIMIZE FINAL 更慢,机制一样
  ────────────────────────────────────────────────────────────
  合计     {total:5.2f}s

  三条规律:① **列裁剪** —— 只读用到的列,这是 ClickHouse 快的根;
            ② **分区裁剪** —— WHERE 命中分区键就整块跳过(报表 D 最快);
            ③ **聚合代价 = 要装多少状态** —— 低基数 < 基数估计 < 精确去重/分位数。

  这是一条最小但完整的 OLAP 流水线:造数 → 建模(分区+排序键)→ 查询 → 观察存储。
  回头把 01~13 的机制串起来,你已经在脑子里装下了一台"能跑起来"的 ClickHouse。
""")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()
