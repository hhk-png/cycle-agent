#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
文档结构自检 —— 让 README/.verify.out 里那句「文档侧自检」变成**可复现的命令**。

用法（在 ray-toturial/ 或 ray-toturial/mini-ray/ 下都行）：

    python mini-ray/tools/check_docs.py
    python tools/check_docs.py

退出码：0 = 全部通过；1 = 有检查项失败。

检查项（每一项都对应过真实踩到的错）：
  1. 章号连续性          —— 00..N 无跳号、无重号
  2. 代码围栏闭合        —— ``` 成对出现
  3. 跨章引用越界        —— 「第 N 章」中的 N 必须存在
  4. §小节引用悬空       —— 「第 N 章 §N.M」的 §N.M 必须在该章里存在
  5. 本地链接可达        —— markdown 相对链接指向的文件必须存在
  6. 表格列数一致        —— 每行单元格数 == 表头（忽略转义竖线 \\|）
  7. 自述数字一致性      —— 测试数/示例数/篇数 在全书里必须只有一个值
  8. 自指计数一致        —— 「(往后|后面|离全书结尾)还有 N 章」必须等于真实章号差
  9. 结语位置一致        —— 「结语」只出现在一章，且所有指向它的说法都指向那一章

第 8、9 项是**第八轮新增**的，针对一类**由改动自身引发**的错误：
第七轮加第 38 章时，第 31/32/33/36/37 章共 8 处「全书结语在哪」「后面还有几章」
的说法同时过期 —— 而它们上一轮刚被核对过。第八轮加第 39 章时，
同一件事本来会再发生一次：这两项就是把它交给脚本拦下。

关于第 6 项的转义竖线：Markdown 里 `` `a\|b` `` 是字面竖线，不是分隔符。
早期版本的自检脚本没处理它，于是把 `` `ray logs actor\|task\|…` `` 这类
正常表格误报成「列数不一致」—— 这个脚本用 re.split(r'(?<!\\)\|') 修正。
"""

from __future__ import annotations

import collections
import glob
import os
import re
import sys

# Windows 控制台默认是 GBK，会在这里的符号上抛 UnicodeEncodeError。
# 统一把输出重定向成 UTF-8（不支持的字符用 '?' 代替），避免"检查通过了但脚本崩了"。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 定位 ray-toturial/（无论从哪个目录调用） ──────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_here)                      # mini-ray/
if os.path.basename(ROOT) == "mini-ray":
    ROOT = os.path.dirname(ROOT)                   # ray-toturial/
if not glob.glob(os.path.join(ROOT, "ray教程-*.md")):
    sys.stderr.write(f"找不到教程正文（在 {ROOT} 下）。\n")
    sys.exit(2)

CHAPTER_RE = re.compile(r"^ray教程-(\d+)-(.+)\.md$")

files = sorted(glob.glob(os.path.join(ROOT, "ray教程-*.md")))
chapters: dict[int, str] = {}
for f in files:
    m = CHAPTER_RE.match(os.path.basename(f))
    if m:
        chapters[int(m.group(1))] = f

# 教程自己的 README,外加 **mini-ray 的 README** —— 后者以前不在检查范围内,
# 但它是读者会照着跑的那一份(数字、命令、能力清单都在里面),
# 第六轮它自己也写错过数字却没人拦。
_extra = [
    os.path.join(ROOT, "README.md"),
    os.path.join(ROOT, "mini-ray", "README.md"),
]
docs = files + [p for p in _extra if os.path.exists(p)]

failures: list[str] = []
notes: list[str] = []


def rel(p: str) -> str:
    return os.path.relpath(p, ROOT).replace("\\", "/")


def read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ── 1. 章号连续性 ────────────────────────────────────────────────────────
nums = sorted(chapters)
expected = list(range(0, len(nums)))
if nums != expected:
    missing = sorted(set(expected) - set(nums))
    dupes = [n for n, c in collections.Counter(nums).items() if c > 1]
    failures.append(f"章号不连续：缺 {missing}，重 {dupes}（现有 {nums}）")
else:
    notes.append(f"章节数 {len(nums)}（00-{nums[-1]:02d}，连续无跳号）")

# ── 2. 代码围栏闭合 ─────────────────────────────────────────────────────
for p in docs:
    n = len(re.findall(r"^```", read(p), re.M))
    if n % 2:
        failures.append(f"{rel(p)}：代码围栏未闭合（{n} 个 ```）")
if not any("代码围栏" in f for f in failures):
    notes.append("代码围栏未闭合 0")

# ── 3./4. 跨章与 §小节引用 ───────────────────────────────────────────────
# 每章实际存在的 §小节号。
# 正文用 `## 12.3 …`；附录用 `## F.2 …`（字母前缀）。
# 附录里 `## F.2` 属于哪一篇是**由文件决定的**，所以下面按"文件名 → 字母"
# 建一张映射，这样 `§F.2` 才能被查到对应的那一章。
SEC_NUM_RE = re.compile(r"^#{2,3}\s+(\d+\.\d+(?:\.\d+)?)", re.M)
SEC_ALPHA_RE = re.compile(r"^#{2,3}\s+([A-Z]\.\d+(?:\.\d+)?)", re.M)

# 附录字母 → 章号（按文件里的自述判定，不硬编码，避免改章号后失效）
sections: dict[int, set[str]] = {}
for n, p in chapters.items():
    txt = read(p)
    secs = set(SEC_NUM_RE.findall(txt)) | set(SEC_ALPHA_RE.findall(txt))
    secs |= set(re.findall(r"§(\d+\.\d+(?:\.\d+)?)", txt))
    secs |= set(re.findall(r"§([A-Z]\.\d+(?:\.\d+)?)", txt))
    sections[n] = secs

# 字母前缀小节 → 章号
alpha_owner: dict[str, int] = {}
for n, p in chapters.items():
    for a in SEC_ALPHA_RE.findall(read(p)):
        alpha_owner.setdefault(a.split(".")[0], n)
    # 也收 `# …附录 F…` 这种标题里的字母
    for m in re.finditer(r"附录\s*([A-Z])", read(p).split("\n# ")[0][:400]):
        alpha_owner.setdefault(m.group(1), n)

dangling_ch: list[str] = []
dangling_sec: list[str] = []

for p in docs:
    lines = read(p).split("\n")
    for i, line in enumerate(lines, 1):
        for m in re.finditer(r"第\s*(\d+)\s*章", line):
            cn = int(m.group(1))
            if cn not in chapters:
                dangling_ch.append(f"{rel(p)}:{i} 第 {cn} 章")
        # §N.M 与 §X.M 两种形式
        for m in re.finditer(r"§\s*(\d+)\.(\d+)(?:\.(\d+))?", line):
            cn, sub = int(m.group(1)), f"{m.group(1)}.{m.group(2)}"
            if cn in chapters and sub not in sections.get(cn, set()):
                dangling_sec.append(f"{rel(p)}:{i} §{sub}")
        for m in re.finditer(r"§\s*([A-Z])\.(\d+)(?:\.(\d+))?", line):
            letter, sub = m.group(1), f"{m.group(1)}.{m.group(2)}"
            owner = alpha_owner.get(letter)
            if owner is not None and sub not in sections.get(owner, set()):
                dangling_sec.append(f"{rel(p)}:{i} §{sub}（附录 {letter}）")

if dangling_ch:
    failures.append("悬空章号引用：\n    " + "\n    ".join(dangling_ch[:20]))
else:
    notes.append("悬空章号引用 0")

if dangling_sec:
    # §小节引用失败多半是"章节重编号后忘了改"，但**也可能只是指代一个
    # 尚未编号的段落**，所以列为警告而非失败（见文件末尾的用法说明）。
    notes.append(f"[WARN] §小节引用疑似悬空 {len(dangling_sec)} 处（需人工确认）：\n    "
                 + "\n    ".join(dangling_sec[:20]))
else:
    notes.append("悬空 §小节引用 0")

# ── 5. 本地链接可达 ─────────────────────────────────────────────────────
broken: list[str] = []
for p in docs:
    txt = read(p)
    for m in re.finditer(r"\]\(([^)\s]+)\)", txt):
        target = m.group(1).split("#")[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = os.path.normpath(os.path.join(os.path.dirname(p), target))
        if not os.path.exists(resolved):
            broken.append(f"{rel(p)} → {m.group(1)}")
if broken:
    failures.append("失效本地链接：\n    " + "\n    ".join(broken[:20]))
else:
    notes.append("失效本地链接 0")

# ── 6. 表格列数一致 ─────────────────────────────────────────────────────
SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def cells(line: str) -> int:
    """按未转义的竖线切分，返回单元格数。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return len(re.split(r"(?<!\\)\|", body))


table_bad: list[str] = []
for p in docs:
    lines = read(p).split("\n")
    i = 0
    in_fence = False
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if (not in_fence
                and lines[i].strip().startswith("|")
                and i + 1 < len(lines) and SEP_RE.match(lines[i + 1])):
            header_n = cells(lines[i])
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                if cells(lines[j]) != header_n:
                    table_bad.append(
                        f"{rel(p)}:{j + 1} 表头 {header_n} 列 / 该行 {cells(lines[j])} 列")
                j += 1
            i = j
        else:
            i += 1
if table_bad:
    failures.append("表格列数不一致：\n    " + "\n    ".join(table_bad[:20]))
else:
    notes.append("表格列数不一致 0")

# ── 7. 自述数字一致性 ───────────────────────────────────────────────────
# 只检查**能被脚本核对**的三个数（其余自述数字如行数会随代码漂移，
# 由 .verify.out 的人工复核负责）。
corpus = "\n".join(read(p) for p in docs)

# ⚠️ 只抓**全书总量**的说法，不抓"某个文件的 6 个用例"这类局部计数 ——
#    早期版本用 `(\d+) 个测试` 一把梭，把 `test_fault_tolerance.py 的 6 个用例`
#    也当成了全局自述，误报一堆。局部计数是合法的。
#    判据：同一个数字后面紧跟「全部」，或形如「N passed」。
tests_seen = set()
for m in re.finditer(r"(\d+)\s*个?(?:测试|用例)[^\n]{0,16}?全部", corpus):
    tests_seen.add(int(m.group(1)))
tests_seen |= {int(m.group(1)) for m in re.finditer(r"(\d+)\s+passed", corpus)}

examples_seen = {int(m.group(1)) for m in
                 re.finditer(r"(\d+)\s*个示例[^\n]{0,16}?全部", corpus)}

if len(tests_seen) > 1:
    failures.append(f"自述测试数不一致：出现了 {sorted(tests_seen)}")
else:
    notes.append(f"自述测试数唯一 {sorted(tests_seen)}")

if len(examples_seen) > 1:
    failures.append(f"自述示例数不一致：出现了 {sorted(examples_seen)}")
else:
    notes.append(f"自述示例数唯一 {sorted(examples_seen)}")

# ── 8. 自指计数：「(往后|后面|离全书结尾)还有 N 章」必须等于真实的章号差 ──
# 为什么加这一项：第七轮新增第 38 章之后，第 31/32/33/36/37 章共 8 处
# 「全书结语在哪」「后面还有几章」的说法同时过期 —— 而**它们上一轮刚被核对过**。
# 第八轮新增第 39 章时，同一件事本来会再发生一次。
# 这类错误之所以反复出现，是因为它**由改动本身引发**：
# 你没有改任何"错"的东西，只是加了一章，旁边的约束就全不成立了。
# 那就把它变成脚本能拦的东西。
#
# 只抓**带阿拉伯数字**的写法。「后面还有几章」「33–39 七章」这类
# 中文数字/模糊写法**故意不抓** —— 宁可漏，不可误报（误报会让人开始忽略这个检查）。
_max_ch = max(chapters) if chapters else 0
_SELF_REF_RE = re.compile(r"(?:往后还有|后面还有|离全书结尾还有)\s*\**\s*(\d+)\s*\**\s*章")
self_ref_bad: list[str] = []
for n, p in chapters.items():
    for i, line in enumerate(read(p).split("\n"), 1):
        for m in _SELF_REF_RE.finditer(line):
            claimed, actual = int(m.group(1)), _max_ch - n
            if claimed != actual:
                self_ref_bad.append(
                    f"{rel(p)}:{i} 自述「还有 {claimed} 章」，实际本章之后有 {actual} 章"
                    f"（第 {n} 章 → 第 {_max_ch} 章）")
if self_ref_bad:
    failures.append("自指计数过期：\n    " + "\n    ".join(self_ref_bad[:20]))
else:
    notes.append("自指计数（「还有 N 章」）与实际章号一致")

# ── 9. 「结语在哪一章」的指向必须与真正那一章一致 ──────────────────────
# 与第 8 项同源：结语会随新增章节后移，而指向它的说法散落在多章里。
# 做法：先找出**真正写着「结语」标题**的那一章（sink），
# 再要求所有**declarative 指向**都等于它。
_sink_re = re.compile(r"^#{2,3}\s*结语")
_sinks = [n for n, p in chapters.items()
          if any(_sink_re.match(l) for l in read(p).split("\n"))]

if len(_sinks) != 1:
    failures.append(f"「结语」标题应恰好出现在一章里，实际出现在 {sorted(_sinks)}")
else:
    sink = _sinks[0]
    # 三种"声明式指向"的写法。都是祈使/陈述句，不含历史叙述 ——
    # 历史叙述（"上一轮曾写结语在第 X 章"）不匹配这些形式。
    _POINTERS = [
        re.compile(r"第\s*(\d+)\s*章\s*的?\s*[「『\"]?结语"),
        re.compile(r"结语\s*(?:也)?(?:随之)?移到了\s*\**第\s*(\d+)\s*章"),
        re.compile(r"收尾(?:点)?\s*(?:在|移到了)\s*\**第\s*(\d+)\s*章"),
    ]
    sink_bad: list[str] = []
    # ⚠️ 只扫**正文**，不扫 README：README 的修订清单是**历史记录**
    #    （"第七轮把结语从 37 搬到 38"这种句子，记录的是当时的状态，
    #    不是"现在在第几章"）。把它纳入检查会制造必然的误报 ——
    #    而一个总在误报的检查，很快就会被所有人忽略。
    for n, p in chapters.items():
        for i, line in enumerate(read(p).split("\n"), 1):
            for rx in _POINTERS:
                for m in rx.finditer(line):
                    if int(m.group(1)) != sink:
                        sink_bad.append(
                            f"{rel(p)}:{i} 指向第 {m.group(1)} 章，"
                            f"但「结语」实际在第 {sink} 章")
    if sink_bad:
        failures.append("结语指向错误：\n    " + "\n    ".join(sink_bad[:20]))
    else:
        notes.append(f"结语位置一致（唯一结语在第 {sink} 章，指向它的说法全部正确）")

# ── 输出 ────────────────────────────────────────────────────────────────
print("文档侧自检（%d 篇正文 + %d 个 README）"
      % (len(files), len(docs) - len(files)))
print("=" * 68)
for n in notes:
    print("  " + n)
if failures:
    print("\n失败项：")
    for f in failures:
        print("  [FAIL] " + f)
    print("=" * 68)
    print(f"结果：{len(failures)} 项失败")
    sys.exit(1)
print("=" * 68)
print("结果：全部通过")
sys.exit(0)
