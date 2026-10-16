# -*- coding: utf-8 -*-
r"""06 —— 函数里的坑:一个"看起来对、其实不对"的巡回展。

ClickHouse 的函数库很大,但真正让人踩坑的是这么几类**语义细节**。本示例不追求
覆盖函数,只挑那些"不亲眼见一次就会写错"的:

1. ``length`` vs ``lengthUTF8`` —— 前者数是**字节**,后者数**码点**。
   一个中文字 = 3 字节;一个 emoji(非 BMP 码点)= 4 字节但只算 1 个字符。
2. ``substring`` 的**偏移语义** —— 位置从 **1** 开始;offset=0 返回空串;
   负 offset 表示"从结尾倒数"。还有"少给一个长度参数"的二参形式。
3. ``arrayMap`` 这类**高阶函数**要写 lambda(``x -> x * 2``)。
4. ``arrayJoin`` 是个**陷阱**:它改变行数,不能当普通标量函数用 ——
   真用法是 ``ARRAY JOIN`` 子句。这里如实演示它报错,以及正确写法。
5. **三值逻辑**的 ``and``/``or`` —— ``NULL AND 0 = 0``、``NULL OR 1 = 1``;
   而且 mini-clickhouse **不做短路求值**,所以 ``a AND 1/a > 1`` 里的除零照样会算。
6. ``toTypeName`` 告诉你**字面量的静态类型** —— ``1`` 是 UInt8、``1.5`` 是
   Float64、``NULL`` 是 Nullable(Nothing)。
7. **整数溢出 / 类型提升** —— ``toUInt8(255)+toUInt8(1)`` 不溢出,而是**加宽**到
   UInt16;而 ``toUInt8(-1)`` 会**回绕**成 255。规律不一样,得记住。

每条都先打 SQL 再打结果,好让你把"代码"和"行为"一一对上。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.errors import MiniChError  # noqa: E402
from minich.execution.context import Context  # noqa: E402


def q(ctx: Context, sql: str) -> None:
    """跑一条标量查询,把 SQL 和它的一行结果并排打出来。"""
    try:
        row = ctx.execute(sql).first_row()
        shown = row[0] if row and len(row) == 1 else row
        print(f"  {sql:<78} => {shown!r}")
    except MiniChError as exc:
        print(f"  {sql:<78} => [ERR] {exc.name}: {exc.message[:60]}")


def main() -> None:
    print("=" * 96)
    print("06 · 函数陷阱巡回 —— SQL 在左,结果在右,自己对照")
    print("=" * 96)

    ctx = Context()
    try:
        print("\n--- ① length 数字节,lengthUTF8 数码点 ---")
        q(ctx, "SELECT length('中文字')")
        q(ctx, "SELECT lengthUTF8('中文字')")
        q(ctx, "SELECT length('👋')")
        q(ctx, "SELECT lengthUTF8('👋')")
        print("  → '中文字' 是 9 字节 / 3 个字符;'👋' 是 4 字节 / 1 个字符。")

        print("\n--- ② substring 的偏移是 1-based,0 和负数都有特殊含义 ---")
        q(ctx, "SELECT substring('abcdef', 1, 3)")
        q(ctx, "SELECT substring('abcdef', 2)")
        q(ctx, "SELECT substring('abcdef', 0, 3)")
        q(ctx, "SELECT substring('abcdef', -2, 2)")
        print("  → 从 1 数;offset=0 返回空串(不是'从头');负数从结尾倒数。")

        print("\n--- ③ 高阶函数要写 lambda ---")
        q(ctx, "SELECT arrayMap(x -> x * 2, [1, 2, 3])")
        q(ctx, "SELECT arrayFilter(x -> x > 1, [1, 2, 3])")
        q(ctx, "SELECT arrayCount(x -> x > 1, [1, 2, 3])")
        q(ctx, "SELECT arraySort(x -> x, [3, 1, 2])")

        print("\n--- ④ arrayJoin 是陷阱:它是'改变行数'的函数,不能当标量 ---")
        q(ctx, "SELECT arrayJoin([1, 2, 3])")
        # 正确用法:ARRAY JOIN 子句,把它接在 FROM 上。
        ctx.execute("CREATE TABLE t6 (k String, arr Array(UInt8)) ENGINE = Memory")
        ctx.execute("INSERT INTO t6 VALUES ('a', [1,2,3]), ('b', [4,5])")
        print("  正确写法(ARRAY JOIN 子句):")
        print(ctx.execute(
            "SELECT k, x FROM t6 ARRAY JOIN arr AS x ORDER BY k, x").text())

        print("--- ⑤ 三值逻辑 and/or,NULL 不是 false ---")
        q(ctx, "SELECT 1 AND 0")
        q(ctx, "SELECT 1 OR 0")
        q(ctx, "SELECT NULL AND 0")
        q(ctx, "SELECT NULL OR 1")
        q(ctx, "SELECT NOT NULL")
        print("  → NULL AND 0 = 0(因为结果无论如何是 0);"
              "NULL OR 1 = 1;NOT NULL = NULL。")
        print("  ⚠ 不短路求值:下面的 AND 左边明明是 false,右边的除零**照样会算**并报错:")
        q(ctx, "SELECT 0 != 0 AND intDiv(1, 0) > 3")
        print("    (真实 ClickHouse 也**不保证**短路 —— WHERE 里别写'靠短路挡住'的式子。)")

        print("\n--- ⑥ toTypeName 看字面量的静态类型 ---")
        q(ctx, "SELECT toTypeName(1)")
        q(ctx, "SELECT toTypeName(1.5)")
        q(ctx, "SELECT toTypeName('a')")
        q(ctx, "SELECT toTypeName(NULL)")
        q(ctx, "SELECT toTypeName([1, 2, 3])")
        q(ctx, "SELECT toTypeName(1 = 1)")

        print("\n--- ⑦ 整数溢出 / 类型提升 ---")
        q(ctx, "SELECT toUInt8(255) + toUInt8(1)")
        q(ctx, "SELECT toTypeName(toUInt8(255) + toUInt8(1))")
        q(ctx, "SELECT toUInt8(3) - toUInt8(5)")
        q(ctx, "SELECT toTypeName(toUInt8(3) - toUInt8(5))")
        q(ctx, "SELECT toUInt8(-1)")
        q(ctx, "SELECT toInt8(300)")
        q(ctx, "SELECT toUInt64(18446744073709551615) * toUInt64(2)")
        print("  → 加/减/乘会**加宽**一个宽度(UInt8+UInt8=UInt16),不溢出;")
        print("    但到了 UInt64 顶格就**回绕**(18446744073709551615*2 → 少了 1);")
        print("    而 toUInt8(-1) 这类**转换**是直接回绕:255。")

        print("\n小结:这些坑的共同点是'类型和语义藏在细节里'。写 SQL 时把")
        print("      toTypeName() 当调试器用,比猜要快得多。")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()
