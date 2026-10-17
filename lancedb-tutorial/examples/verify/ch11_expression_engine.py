# -*- coding: utf-8 -*-
"""第 11 章实测脚本：解析器报错、三值逻辑、NaN、向量化、索引改写、LIKE 快速路径。"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# 这些脚本原本在仓库根目录下直接跑；挪进 examples/verify/ 之后
# 需要自己把包的搜索路径补上。
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))


import sys, os, time

import numpy as np
import mini_lancedb as mldb
from mini_lancedb.record import RecordBatch
from mini_lancedb.schema import Schema, Field
from mini_lancedb.types import BOOL, FLOAT64, INT64, UTF8
from mini_lancedb.expr import (
    parse, evaluate, evaluate_predicate, fold_constants, simplify, to_index_expr,
    compile_like, tokenize,
    BinaryOp, UnaryOp, Literal,
)
from mini_lancedb.expr.rewrite import estimate_selectivity


def show(label, fn):
    try:
        print(f"{label:<46} -> {fn()}")
    except Exception as e:
        print(f"{label:<46} -> {type(e).__name__}: {e}")


print("=" * 74)
print("1. 解析器：报错带插入符")
print("=" * 74)
def err_text(sql):
    try:
        parse(sql)
        return "(没有报错)"
    except Exception as e:
        if getattr(e, "position", None) is None:
            return f"{type(e).__name__}: {e}"
        real = f"{type(e).__name__}: {e}"
        two = "\n".join([f"{type(e).__name__}: {e.message}",
                         "  " + str(e.expression),
                         "  " + " " * e.position + "^"])
        return "str(e) ->\n  " + real + "\n按 position 重建 ->\n" + two

for sql in ("a = (1 + 2", "a > ", "a BETWEEN 1", "a IN (1, 2", "a LIKE 'x' ESCAPE 'ab'",
            "a && b", "a = 'unterminated", "a = 1 AND (b = 2))"):
    print(f"--- parse({sql!r}) ---")
    print(err_text(sql))
print("--- parse('unknown_fn(a)') ---")
print(err_text("unknown_fn(a)")[:110] + " ...")
print()
print("--- token 也带 pos ---")
for tok in tokenize("a >= 1 AND b LIKE 'x'"):
    print(f"  {tok.kind:<8} {tok.value!r:<10} pos={tok.pos}")

print()
print("--- 带 schema 解析：列名写错立刻报错 ---")
sch = Schema([Field("id", INT64), Field("name", UTF8)])
show("parse('age > 1', schema)", lambda: parse("age > 1", sch))
dbq = mldb.connect("memory://")
tq = dbq.create_table("z", [{"id": 1, "name": "x"}])
show("count_rows('id')（裸列名当谓词）", lambda: tq.count_rows("id"))

print()
print("=" * 74)
print("2. 优先级与结合性（to_sql() 打印规范形式）")
print("=" * 74)
for sql in ("a AND b = c", "a OR b AND c", "NOT a = 1", "NOT NOT a", "-a :: int",
            "1 + 2 * 3", "a || b || c", "a BETWEEN 1 AND 2 OR c", "a IS NOT NULL AND b",
            "a = 1 AND b = 2 OR c = 3"):
    show(sql, lambda sql=sql: parse(sql).to_sql())

print()
print("=" * 74)
print("3. 三值逻辑：完整 AND / OR / NOT 真值表")
print("=" * 74)
T, F, N = Literal(True, BOOL), Literal(False, BOOL), Literal(None, BOOL)
blank = RecordBatch.from_pylist([{"_x": 1}], Schema([Field("_x", INT64)]))

def tri(expr):
    v = evaluate(expr, blank)
    if not bool(v.valid[0]):
        return "NULL"
    return "TRUE" if bool(v.values[0]) else "FALSE"

names = {id(T): "TRUE", id(F): "FALSE", id(N): "NULL"}
print(f"{'':>6}{'AND':^6}{'TRUE':>7}{'FALSE':>8}{'NULL':>7}   {'OR':^5}{'TRUE':>7}{'FALSE':>8}{'NULL':>7}   {'NOT':>6}")
for a in (T, F, N):
    ands = "".join(f"{tri(BinaryOp('AND', a, b)):>8}" for b in (T, F, N))
    ors = "".join(f"{tri(BinaryOp('OR', a, b)):>8}" for b in (T, F, N))
    print(f"{names[id(a)]:>6}{'':^6}{ands}   {'':^5}{ors}   {tri(UnaryOp('NOT', a)):>6}")

print()
print("=" * 74)
print("4. 最容易错的几条 NULL 语义")
print("=" * 74)
tbl = mldb.connect("memory://").create_table("n", [
    {"id": 1, "a": 1}, {"id": 2, "a": 2}, {"id": 3, "a": None}, {"id": 4, "a": 3},
])
print("表:", tbl.to_list())
for pred in ("a = 1", "a <> 1", "NOT (a = 1)", "a != 1", "a IS NULL", "a IS NOT NULL",
             "a IN (1, 2)", "a NOT IN (1, 2)", "a NOT IN (1, NULL)", "a IN (1, NULL)",
             "a = NULL", "a <> NULL", "a BETWEEN 1 AND 2", "a NOT BETWEEN 1 AND 2"):
    b = tbl._ds.scanner(columns=["id", "a"], filter=pred).disable_zone_maps().to_batch()
    print(f"  WHERE {pred:<22} 命中 {b.num_rows} 行 {[r['id'] for r in b.to_pylist()]}")

print()
print("--- 除零是 NULL 不是异常 ---")
sch2 = Schema([Field("a", INT64), Field("b", INT64)])
bb = RecordBatch.from_pylist([{"a": 6, "b": 3}, {"a": 7, "b": 0}, {"a": -7, "b": 2},
                              {"a": 6, "b": None}], sch2)
for e in ("a / b", "a % b", "a / b > 1", "b <> 0 AND a / b > 1"):
    v = evaluate(parse(e, sch2), bb)
    print(f"  {e:<22} -> {v.to_pylist()}   valid={[bool(x) for x in v.valid]}")

print()
print("=" * 74)
print("5. NaN 与 NULL：本实现与真实 LanceDB 的语义分叉")
print("=" * 74)
sch3 = Schema([Field("f", FLOAT64)])
b3 = RecordBatch.from_pylist([{"f": 1.0}, {"f": float("nan")}, {"f": None}, {"f": 2.0}], sch3)
print("  列:", b3.to_pylist())
for e in ("f = f", "f = 1.0", "f > 1.0", "f <> f", "f IS NULL", "f IS NOT NULL", "f < f OR f >= f"):
    v = evaluate(parse(e, sch3), b3)
    print(f"  {e:<18} -> {v.to_pylist()}")
print("  本实现：NaN 参与任何比较都得 NULL（当做未知）")
print("  真实 LanceDB 0.40.0 走 Arrow 比较内核（全序）：NaN = NaN 为真、NaN > 1.0 也为真")
print()
print("  NaN 会被 IS NULL 抓住吗？ ->", evaluate(parse("f IS NULL", sch3), b3).to_pylist())
print("  向量列上的直接后果：写成 NaN 的行既躲得过 IS NOT NULL，也躲得过 = 的比较")

print()
print("=" * 74)
print("6. 向量化求值 vs 逐行 Python")
print("=" * 74)
big = RecordBatch.from_pylist([{"n": i, "s": f"item-{i}"} for i in range(200000)],
                              Schema([Field("n", INT64), Field("s", UTF8)]))
ncol = big.column("n").to_pylist(); scol = big.column("s").to_pylist()

def bench(label, vec_fn, py_fn, reps=5):
    tv = tp = None
    for _ in range(reps):
        t0 = time.perf_counter(); rv = vec_fn(); dt = time.perf_counter() - t0
        tv = dt if tv is None else min(tv, dt)
        t0 = time.perf_counter(); rp = py_fn(); dt = time.perf_counter() - t0
        tp = dt if tp is None else min(tp, dt)
    print(f"  {label:<34} numpy {tv*1000:7.1f} ms | Python {tp*1000:7.1f} ms | 比值 {tp/tv:5.2f}x  "
          f"结果一致={list(rv) == list(rp)}")

bench("纯数值 n % 7 = 0",
      lambda: evaluate_predicate(parse("n % 7 = 0", big.schema), big),
      lambda: [i % 7 == 0 for i in ncol])
bench("纯字符串 starts_with(s, 'item-1')",
      lambda: evaluate_predicate(parse("starts_with(s, 'item-1')", big.schema), big),
      lambda: [str(x).startswith("item-1") for x in scol])
bench("算术 n * 3 + 1 < 100",
      lambda: evaluate_predicate(parse("n * 3 + 1 < 100", big.schema), big),
      lambda: [i * 3 + 1 < 100 for i in ncol])

print()
print("  字符串列慢在哪（20 万行）：")
arr = np.asarray(scol, dtype=object)
t0 = time.perf_counter(); x = arr.astype(str); t1 = time.perf_counter()
print(f"    object 数组 -> 定宽 unicode: {(t1-t0)*1000:7.1f} ms")
t0 = time.perf_counter(); np.char.startswith(x, "item-1"); t1 = time.perf_counter()
print(f"    然后 np.char.startswith:     {(t1-t0)*1000:7.1f} ms")
t0 = time.perf_counter(); [s.startswith("item-1") for s in list(arr)]; t1 = time.perf_counter()
print(f"    纯 Python startswith:        {(t1-t0)*1000:7.1f} ms")

print()
print("=" * 74)
print("7. 常量折叠与化简")
print("=" * 74)
for sql in ("a > 1 + 2", "a > 2 * 3 + 4", "TRUE AND a = 1", "FALSE AND a = 1",
            "a = 1 AND a = 1", "a = 1 OR a = 1", "NOT NOT (a = 1)", "a = 1 AND TRUE"):
    e = parse(sql)
    print(f"  {sql:<24} simplify -> {simplify(e).to_sql():<18} fold -> {fold_constants(simplify(e)).to_sql()}")
print("  折叠把 a > 1 + 2 变成 a > 3 —— 这才让 to_index_expr 认得出这是一个区间条件")

print()
print("=" * 74)
print("8. 索引改写（to_index_expr）")
print("=" * 74)
s = Schema([Field("n", INT64), Field("s", UTF8)])
CASES = [("n = 5", "n"), ("n != 5", "n"), ("n < 5", "n"), ("n >= 5", "n"),
         ("n IN (1, 2, 3)", "n"), ("n NOT IN (1, 2)", "n"), ("n BETWEEN 1 AND 9", "n"),
         ("s LIKE 'abc'", "s"), ("s LIKE 'abc%'", "s"), ("s LIKE '%abc'", "s"), ("s LIKE 'a_c'", "s"),
         ("n = 5 AND s = 'x'", "n"), ("n = 5 OR s = 'x'", "n"), ("n = 5 OR n = 7", "n"),
         ("NOT n = 5", "n"), ("n IS NULL", "n"), ("n + 1 = 5", "n"), ("n = NULL", "n"),
         ("n > nan", "n"), ("n = 5 AND n < 3", "n"), ("n NOT BETWEEN 1 AND 9", "n")]
for sql, col in CASES:
    try:
        ie = to_index_expr(parse(sql, s), col)
        print(f"  {sql:<24} (col={col}) -> {ie!r}")
    except Exception as e:
        print(f"  {sql:<24} (col={col}) -> {type(e).__name__}: {e}")

print()
print("=" * 74)
print("9. LIKE 的分类与两个快速路径")
print("=" * 74)
for pat in ("abc", "abc%", "%abc", "%abc%", "a%c", "a_c", "a%c%", "%%", "_"):
    m = compile_like(pat)
    print(f"  pattern={pat!r:<8} kind={m.kind:<14} literal={m.literal!r:<10} regex={m.regex.pattern!r}")
print("  带 ESCAPE：'50\\\\%' 里 \\\\ 是转义符、% 是字面量")
print("   compile_like('50%', '\\\\') ->", compile_like("50%", "\\").kind, repr(compile_like("50%", "\\").literal),
      repr(compile_like("50%", "\\").regex.pattern))
print("  不带 ESCAPE：'50\\\\%' 里 \\\\ 是字面字符、% 仍是通配符")
print("   compile_like('50\\\\%')    ->", compile_like("50\\%").kind, repr(compile_like("50\\%").literal),
      repr(compile_like("50\\%").regex.pattern))
print()
print("  真实数据上逐条验证：")
ts = mldb.connect("memory://").create_table("s", [{"s": x} for x in
    ["abc", "abcd", "xabc", "ab", "a_b", "a%c", "ABC", "", "aXc"]])
for pred in ("s LIKE 'abc'", "s LIKE 'abc%'", "s LIKE '%abc'", "s LIKE '%abc%'",
             "s LIKE 'a_c'", r"s LIKE 'a\%c' ESCAPE '\'", "s LIKE 'a%c'", "s LIKE 'a%c%'", "s LIKE '%'"):
    r = [x["s"] for x in ts._ds.scanner(columns=["s"], filter=pred).disable_zone_maps().to_batch().to_pylist()]
    print(f"  {pred:<32} -> {r}")

print()
print("=" * 74)
print("10. 选择性估计")
print("=" * 74)
for sql in ("n = 1", "n > 1", "n > 999999", "s LIKE 'item-%'", "s LIKE '%x%'",
            "n > 1 AND n < 5", "n > 1 OR n < 5", "NOT n = 1", "n IN (1,2,3)", "n IS NULL"):
    print(f"  {sql:<22} -> {estimate_selectivity(parse(sql, s)):.4f}")
st = {"n": {"ndv": 1000, "min": 0, "max": 999, "null_frac": 0.02}}
for sql in ("n = 1", "n > 5000", "n < 0"):
    print(f"  带统计量 {sql:<12} -> {estimate_selectivity(parse(sql, s), st):.6f}")

print()
print("=" * 74)
print("11. 运行期短路")
print("=" * 74)
b2 = RecordBatch.from_pylist([{"n": i} for i in range(200000)], Schema([Field("n", INT64)]))
for sql in ("FALSE AND n % 7 = 0", "TRUE AND n % 7 = 0", "TRUE OR n % 7 = 0", "FALSE OR n % 7 = 0"):
    ex = parse(sql, b2.schema)
    best = None
    for _ in range(5):
        t0 = time.perf_counter(); evaluate_predicate(ex, b2); dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    print(f"  {sql:<22} {best*1000:7.2f} ms")

print()
print("=" * 74)
print("12. LIKE 有向量化快速路径，SQL 函数没有")
print("=" * 74)
def bench1(sql, batch=big, reps=3):
    ex = parse(sql, batch.schema)
    best = None
    for _ in range(reps):
        t0 = time.perf_counter(); r = evaluate_predicate(ex, batch); dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best * 1000, int(r.sum())
for sql in ("s LIKE 'item-1%'", "starts_with(s, 'item-1')", "s LIKE '%1%'", "contains(s, '1')",
            "s LIKE 'item-1'", "lower(s) = 'item-1'"):
    t, n = bench1(sql)
    print(f"  {sql:<26} {t:8.1f} ms  命中 {n:,}")
print("  语义等价的两对写法差了约 3 倍。")
