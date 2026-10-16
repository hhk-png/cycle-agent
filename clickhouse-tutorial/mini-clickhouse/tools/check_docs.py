#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
文档结构自检 —— 把 README 里那句"文档侧自检"变成**可复现的命令**。

用法(在 ``clickhouse-tutorial/`` 或 ``clickhouse-tutorial/mini-clickhouse/`` 下都行)::

    python mini-clickhouse/tools/check_docs.py
    python tools/check_docs.py

退出码:``0`` = 全部通过;``1`` = 有检查项失败;``2`` = 找不到教程正文。

检查项(每一项都对应一类**真实发生过**的错误):

1. **章号连续性** —— 00..N 无跳号、无重号
2. **代码围栏闭合** —— ``` 成对出现(少一个反引号会让后面整段排版崩掉)
3. **跨章引用越界** —— "第 N 章"里的 N 必须存在
4. **§小节引用悬空** —— "第 N 章 §N.M"里的 §N.M 必须在该章里存在
5. **本地链接可达** —— markdown 相对链接指向的文件必须存在
6. **表格列数一致** —— 每行单元格数 == 表头(忽略转义竖线 ``\|``)
7. **自述数字一致性** —— 全书里的"测试数 / 示例数 / 篇数"必须只有一个值
8. **自指计数一致** —— "(往后|后面|离全书结尾)还有 N 章"必须等于真实章号差
9. **结语位置一致** —— "结语"标题只许出现在一章,且所有指向它的说法都指向那一章
10. **小节编号连续** —— 每章的 ``## N.1``、``## N.2``… 必须从 1 开始且无跳号;
    附录用字母前缀(``## A.1``),同样检查
11. **实现规模与注册表一致** —— "注册了 N 个标量函数 / N 张 system 表"必须等于
    从注册表里实际数出来的数字
12. **实现规模的行数/文件数一致** —— "``minich/`` 下 N 个 ``.py``、M 行"必须
    等于当场 ``find | wc -l`` 数出来的 123 个文件、86 242 行;同一项还管
    **单个模块**的行数 —— 各章末尾"与 mini-clickhouse 的对照"里那种
    "``merger.py``(992 行)"的写法,数字也得等于当场数出来的
13. **测试结果行一致** —— 全书所有形如 ``N passed, M skipped, K xfailed``
    的实测结果必须**完全相同**;README 与附录 B 各写一个值是最常见的漂移
14. **附录 B 的文件树与磁盘一致** —— B.1 那棵逐文件的树,每个文件名后面的
    行数必须等于当场 ``wc -l``;还要求不漏列、不多列任何一个文件
15. **自述的检查项数一致** —— 文档里"N 项检查"里的 N 必须等于本脚本
    真实执行的项数(脚本数自己)
16. **关键设计计数一致** —— "十个关键设计"这类说法必须等于第 31 章
    真实的 `## 29.N 关键设计…` 小节数
17. **子包行数一致** —— "`storages/merge_tree/` 12 142 行"这类**子包小计**
    必须等于当场数出来的值(第 12 项只管总量,刻意放过子包,于是漏过一次)
18. **自述的脚本行数一致** —— "`tools/check_docs.py`(N 行)"必须等于本脚本
    的真实行数(第 12、14、17 项的取证范围都止于 `minich/`,它掉进了缝里)
19. **`tests/` 规模矩阵一致** —— 附录 B 的 B.5.1 那张"测试文件 / 函数数 / 行数"
    表,逐行核对文件是否都在、"`def test_`"数是否对得上、合计是否正确;
    还要求不漏列、不多列任何一个测试文件
20. **收集条数一致** —— "参数化展开后 pytest 实际收集 N 条"必须等于当场
    `pytest --collect-only` 数出来的条数,且 ``passed + skipped + xfailed``
    必须等于同一个数

第 11–20 项是同一个形状的反复应用:错误不是有人写错了什么,
而是**"项目在长大"这个动作本身把它们变成错的**。数字是快照,快照会过期,
所以让脚本每次重新数一遍。第 15 项把这条原则推到了极致 —— 它约束的是
**这份检查清单本身的长度**:每加一项检查,所有"共 N 项"的说法就过期了。
第 16 项则是同一形状的又一个实例:给第 31 章加一节"关键设计",
所有"九个关键设计"的引用就一起作废。

第 17、18、19 三项还共享一个教训:**每一项检查都为了"不误报"而收窄了范围,
而收窄掉的那部分就没人管了**。第 12 项放过子包,于是第 17 项才存在;
第 12、14、17 项的取证范围都止于 `minich/`,于是 `tools/` 自己的行数
长期是错的;第 12、14、17、18 项一起放过了 `tests/`,于是 B.5.1 那张表
一直停在"23 个文件、891 个函数"上,而磁盘上早就多出了两个
`test_regressions_*.py` —— 连带把全书最显眼的那个数字 ``2779 passed``
也一起变成了过期值。所以每加一项检查,都值得再问一句
"它把什么排除在外了"。

第 20 项是唯一一项**需要真的调用外部命令**的检查(跑一次
``pytest --collect-only``,约 1 秒,不执行任何用例):因为"展开后有多少条"
是**参数化的产物**,静态读源码数不出来 —— 源码里只有 923 个
``def test_``,展开后却是 2 945 条。数不出来的东西,只能让它自己报。

为什么这二十项值得写成脚本? 因为**它们全都曾经靠人眼漏过**。第 8、9 两项尤其
典型:它们约束的东西**会被"新增一章"这个动作本身弄坏** —— 你没有改任何
"错"的内容,只是加了一章,旁边所有"后面还有几章""结语在哪"的说法就全过期了。
这类错误靠检查是抓不住的,必须交给机器。

第 6 项曾经误报过:Markdown 里 `` `a\|b` `` 是**字面竖线**,不是分隔符。
早期版本没处理它,于是把 `` `ray logs actor\|task` `` 这类正常表格报成
"列数不一致"。现在用 ``re.split(r'(?<!\\)\|')`` 切分 —— 宁可漏,不可误报:
**一个总在误报的检查,很快就会被所有人忽略。**
"""

from __future__ import annotations

import collections
import glob
import os
import re
import subprocess
import sys

# Windows 控制台默认是 GBK,会在中文/符号上抛 UnicodeEncodeError。
# 统一把输出重定向成 UTF-8,避免"检查通过了但脚本崩了"。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# ── 定位 clickhouse-tutorial/ (无论从哪个目录调用) ───────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_here)                       # mini-clickhouse/
if os.path.basename(ROOT) == "mini-clickhouse":
    ROOT = os.path.dirname(ROOT)                    # clickhouse-tutorial/
if not glob.glob(os.path.join(ROOT, "clickhouse教程-*.md")):
    sys.stderr.write(f"找不到教程正文(在 {ROOT} 下)。\n")
    sys.exit(2)

CHAPTER_RE = re.compile(r"^clickhouse教程-(\d+)-(.+)\.md$")

files = sorted(glob.glob(os.path.join(ROOT, "clickhouse教程-*.md")))
chapters: dict[int, str] = {}
for _f in files:
    _m = CHAPTER_RE.match(os.path.basename(_f))
    if _m:
        chapters[int(_m.group(1))] = _f

# 教程自己的 README **与 mini-clickhouse 的 README** —— 后者是读者会照着
# 跑的那一份(命令、数字、能力清单都在里面),也必须纳入检查。
_extra = [
    os.path.join(ROOT, "README.md"),
    os.path.join(ROOT, "mini-clickhouse", "README.md"),
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
    failures.append(f"章号不连续:缺 {missing},重 {dupes}(现有 {nums})")
else:
    notes.append(f"章节数 {len(nums)}(00-{nums[-1]:02d},连续无跳号)")

# ── 2. 代码围栏闭合 ─────────────────────────────────────────────────────
for p in docs:
    n = len(re.findall(r"^```", read(p), re.M))
    if n % 2:
        failures.append(f"{rel(p)}:代码围栏未闭合({n} 个 ```)")
if not any("代码围栏" in f for f in failures):
    notes.append("代码围栏未闭合 0")

# ── 3./4. 跨章与 §小节引用 ───────────────────────────────────────────────
SEC_NUM_RE = re.compile(r"^#{2,3}\s+(\d+\.\d+(?:\.\d+)?)", re.M)
SEC_ALPHA_RE = re.compile(r"^#{2,3}\s+([A-Z]\.\d+(?:\.\d+)?)", re.M)


def _norm_sec(sec: str) -> str:
    """把 ``03.04.1`` 归一成 ``3.4.1``。

    为什么必须归一:第 00–09 章的小节标题写成 ``## 03.4`` 这种**带前导零**的
    形式,而正文引用几乎都写成 ``§3.4``。两边一字符串比较就永远不等 ——
    于是第 4 项对第 01–09 章**从来没有真正生效过**,只在引用恰好也带前导零
    时才侥幸匹配上。和第 10 项那个 bug 是同一个形状:检查在"看起来通过"的
    状态下漏掉了一整类输入。归一之后,两种写法都能对上。
    """
    parts = sec.split(".")
    if parts[0].isdigit():
        return ".".join(str(int(x)) for x in parts)
    return parts[0] + "." + ".".join(str(int(x)) for x in parts[1:])


sections: dict[int, set[str]] = {}
for n, p in chapters.items():
    txt = read(p)
    secs = set(SEC_NUM_RE.findall(txt)) | set(SEC_ALPHA_RE.findall(txt))
    secs |= set(re.findall(r"§(\d+\.\d+(?:\.\d+)?)", txt))
    secs |= set(re.findall(r"§([A-Z]\.\d+(?:\.\d+)?)", txt))
    sections[n] = {_norm_sec(s) for s in secs}

# 字母前缀小节 → 章号。**判据只能是"这一章是谁",不能是"它在正文里提了谁"** ——
# 和第 10 项那条 bug 同源,而且这里真的踩了:第 17 章的开篇句是"……按名字排列的
# 完整速查表在附录 A(第 33 章)",于是 alpha_owner["A"] 被绑到了 **17**,全书所有
# `§A.N` 引用都拿第 17 章的小节集合去校验 —— 而第 17 章自己只提过零星几个 A 小节,
# 于是这项检查要么误报、要么漏检,取决于被引用的那个小节恰好有没有被第 17 章提到。
# 现在按**文件名**认领(和第 10 项用的是同一套判据:文件名里写 `附录 X` 就是它,
# 否则看一级标题),正文里的"附录 A"顶多算引用。
alpha_owner: dict[str, int] = {}
for n, p in chapters.items():
    _base = os.path.basename(p)
    _m = re.search(r"附录\s*([A-Z])", _base) or re.search(
        r"^#\s*.*?附录\s*([A-Z])", read(p), re.M
    )
    if _m:
        alpha_owner.setdefault(_m.group(1), n)

dangling_ch: list[str] = []
dangling_sec: list[str] = []

for p in docs:
    lines = read(p).split("\n")
    for i, line in enumerate(lines, 1):
        for m in re.finditer(r"第\s*(\d+)\s*章", line):
            cn = int(m.group(1))
            if cn not in chapters:
                dangling_ch.append(f"{rel(p)}:{i} 第 {cn} 章")
        for m in re.finditer(r"§\s*(\d+)\.(\d+)(?:\.(\d+))?", line):
            cn = int(m.group(1))
            sub = _norm_sec(".".join(g for g in m.groups() if g is not None))
            if cn in chapters and sub not in sections.get(cn, set()):
                dangling_sec.append(f"{rel(p)}:{i} §{sub}")
        for m in re.finditer(r"§\s*([A-Z])\.(\d+)(?:\.(\d+))?", line):
            letter = m.group(1)
            sub = _norm_sec(".".join(g for g in m.groups() if g is not None))
            owner = alpha_owner.get(letter)
            if owner is not None and sub not in sections.get(owner, set()):
                dangling_sec.append(f"{rel(p)}:{i} §{sub}(附录 {letter})")

if dangling_ch:
    failures.append("悬空章号引用:\n    " + "\n    ".join(dangling_ch[:20]))
else:
    notes.append("悬空章号引用 0")

if dangling_sec:
    # §小节引用多半是"章节重编号后忘了改",但**也可能只是指代一个
    # 尚未编号的段落**,所以列为警告而非失败。
    notes.append(
        f"[WARN] §小节引用疑似悬空 {len(dangling_sec)} 处(需人工确认):\n    "
        + "\n    ".join(dangling_sec[:20])
    )
else:
    notes.append("悬空 §小节引用 0")

# ── 5. 本地链接可达 ─────────────────────────────────────────────────────
broken: list[str] = []
for p in docs:
    for m in re.finditer(r"\]\(([^)\s]+)\)", read(p)):
        target = m.group(1).split("#")[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = os.path.normpath(os.path.join(os.path.dirname(p), target))
        if not os.path.exists(resolved):
            broken.append(f"{rel(p)} → {m.group(1)}")
if broken:
    failures.append("失效本地链接:\n    " + "\n    ".join(broken[:20]))
else:
    notes.append("失效本地链接 0")

# ── 6. 表格列数一致 ─────────────────────────────────────────────────────
SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def cells(line: str) -> int:
    """按**未转义**的竖线切分,返回单元格数。"""
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
        if (
            not in_fence
            and lines[i].strip().startswith("|")
            and i + 1 < len(lines)
            and SEP_RE.match(lines[i + 1])
        ):
            header_n = cells(lines[i])
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                if cells(lines[j]) != header_n:
                    table_bad.append(
                        f"{rel(p)}:{j + 1} 表头 {header_n} 列 / 该行 {cells(lines[j])} 列"
                    )
                j += 1
            i = j
        else:
            i += 1
if table_bad:
    failures.append("表格列数不一致:\n    " + "\n    ".join(table_bad[:20]))
else:
    notes.append("表格列数不一致 0")

# ── 7. 自述数字一致性 ───────────────────────────────────────────────────
corpus = "\n".join(read(p) for p in docs)

# 只抓**全书总量**的说法,不抓"某个文件的 6 个用例"这类局部计数 ——
# 局部计数是合法的。判据:同一个数字后面紧跟"全部",或形如 "N passed"。
tests_seen = set()
for m in re.finditer(r"(\d+)\s*个?(?:测试|用例)[^\n]{0,16}?全部", corpus):
    tests_seen.add(int(m.group(1)))
tests_seen |= {int(m.group(1)) for m in re.finditer(r"(\d+)\s+passed", corpus)}

examples_seen = {
    int(m.group(1)) for m in re.finditer(r"(\d+)\s*个示例[^\n]{0,16}?全部", corpus)
}

if len(tests_seen) > 1:
    failures.append(f"自述测试数不一致:出现了 {sorted(tests_seen)}")
else:
    notes.append(f"自述测试数唯一 {sorted(tests_seen)}")

if len(examples_seen) > 1:
    failures.append(f"自述示例数不一致:出现了 {sorted(examples_seen)}")
else:
    notes.append(f"自述示例数唯一 {sorted(examples_seen)}")

# ── 8. 自指计数 ─────────────────────────────────────────────────────────
_max_ch = max(chapters) if chapters else 0
_SELF_REF_RE = re.compile(r"(?:往后还有|后面还有|离全书结尾还有)\s*\**\s*(\d+)\s*\**\s*章")
self_ref_bad: list[str] = []
for n, p in chapters.items():
    for i, line in enumerate(read(p).split("\n"), 1):
        for m in _SELF_REF_RE.finditer(line):
            claimed, actual = int(m.group(1)), _max_ch - n
            if claimed != actual:
                self_ref_bad.append(
                    f"{rel(p)}:{i} 自述「还有 {claimed} 章」,实际本章之后有 {actual} 章"
                    f"(第 {n} 章 → 第 {_max_ch} 章)"
                )
if self_ref_bad:
    failures.append("自指计数过期:\n    " + "\n    ".join(self_ref_bad[:20]))
else:
    notes.append("自指计数(「还有 N 章」)与实际章号一致")

# ── 9. 「结语」只出现在一章 ──────────────────────────────────────────────
# 附录 E 用 ``## E.9 结语`` 这种带字母前缀的写法,所以匹配要放宽一点点。
_sink_re = re.compile(r"^#{2,3}\s*(?:[A-Z]\.\d+\s*)?结语")
_sinks = [n for n, p in chapters.items() if any(_sink_re.match(l) for l in read(p).split("\n"))]

if len(_sinks) != 1:
    failures.append(f"「结语」标题应恰好出现在一章里,实际出现在 {sorted(_sinks)}")
else:
    sink = _sinks[0]
    # 三种"声明式指向"。历史叙述("上一轮曾写结语在第 X 章")不匹配这些形式。
    _POINTERS = [
        re.compile(r"第\s*(\d+)\s*章\s*的?\s*[「『\"]?结语"),
        re.compile(r"结语\s*(?:也)?(?:随之)?移到了\s*\**第\s*(\d+)\s*章"),
        re.compile(r"收尾(?:点)?\s*(?:在|移到了)\s*\**第\s*(\d+)\s*章"),
    ]
    sink_bad: list[str] = []
    for n, p in chapters.items():
        for i, line in enumerate(read(p).split("\n"), 1):
            for rx in _POINTERS:
                for m in rx.finditer(line):
                    if int(m.group(1)) != sink:
                        sink_bad.append(
                            f"{rel(p)}:{i} 指向第 {m.group(1)} 章,"
                            f"但「结语」实际在第 {sink} 章"
                        )
    if sink_bad:
        failures.append("结语指向错误:\n    " + "\n    ".join(sink_bad[:20]))
    else:
        notes.append(f"结语位置一致(唯一结语在第 {sink} 章,指向它的说法全部正确)")

# ── 10. 小节编号连续 ────────────────────────────────────────────────────
# 判据:每章里**属于本章**的小节号必须从 .1 开始、逐个递增、无跳号。
# 附录用字母前缀(``A.1``),同样处理。
sec_gaps: list[str] = []
sec_skipped: list[str] = []
for n, p in chapters.items():
    txt = read(p)
    # 这一章用数字前缀还是字母前缀? **只认文件名与一级标题**,不认正文。
    #
    # 这里踩过一次坑:早先是在正文开头 400 个字符里找 ``附录\s*([A-Z])``。
    # 第 17 章的开篇句是"……按名字排列的完整速查表在附录 A",于是它被判成
    # 前缀为 ``A`` 的附录,去找 ``A.1``、``A.2``…… 一个也找不到,``found``
    # 为空、整章被静默跳过 —— 17 那一章的小节号**从来没被查过**。
    # 教训和第 8、9、10 项是同一条:**判据必须只依赖"这一章是谁",
    # 不能依赖"它在正文里提了谁"**;正文里提一句"附录 A"是完全正常的引用。
    base = os.path.basename(p)
    m = re.search(r"附录\s*([A-Z])", base) or re.search(
        r"^#\s*.*?附录\s*([A-Z])", txt, re.M
    )
    if m:
        prefix = m.group(1)
        found = [int(x.split(".")[1]) for x in SEC_ALPHA_RE.findall(txt)
                 if x.startswith(prefix + ".")]
    else:
        # 注意这里要**按数值**比,不能按字符串比。第 00–09 章的小节写成
        # ``## 05.1`` 这种带前导零的形式,而 ``str(n)`` 是 ``"5"`` ——
        # 早先用 ``x.split(".")[0] == str(n)`` 比,它们**全部不匹配**,
        # 于是 ``found`` 为空、被下面那句 ``continue`` 静默跳过:
        # 整整十章的"小节号是否连续"从来没被检查过(而且是**看起来通过**的)。
        # 这和第 8、9 项同源 —— 错误不来自谁写错了内容,而来自检查本身
        # 悄悄漏掉了一整类输入。宁可让它报出来,也不要让它无声地不查。
        found = [int(x.split(".")[1]) for x in SEC_NUM_RE.findall(txt)
                 if int(x.split(".")[0]) == n]
    if not found:
        # 没有识别出任何小节号。这**曾经**是上面那条 bug 的表现(整类输入
        # 静默漏检),现在把它显式记下来:宁可让这章报出来,也不要让它
        # 无声地"通过"。一项检查悄悄放过一整章,比它报错危险得多。
        sec_skipped.append(rel(p))
        continue
    found_sorted = sorted(set(found))
    if found_sorted != list(range(1, len(found_sorted) + 1)):
        sec_gaps.append(f"{rel(p)} 小节号 {found_sorted}(应从 1 连续)")
if sec_gaps:
    failures.append("小节编号不连续:\n    " + "\n    ".join(sec_gaps))
elif sec_skipped:
    # 有章节没被查过 —— 就不该报"连续"。这条成功信息只在**全部章节都真的
    # 被查过**时才成立,否则它是一句谎话。
    notes.append(
        f"[WARN] 小节编号检查跳过了 {len(sec_skipped)} 章(未识别出 `## N.M` 小节):"
        + "、".join(sec_skipped)
    )
else:
    notes.append("小节编号连续(每章从 .1 起、无跳号)")

# ── 11. 实现规模的数字必须与注册表一致 ──────────────────────────────────
# 为什么加这一项:这一轮里**多个章节是并行写的**,各自读到的实现快照不同 ——
# 于是第 04 章说"注册了 14 个引擎、MergeTree 尚未注册",第 17 章说"502 个函数、
# type_funcs 是空占位",附录 A 说"39 张系统表"。三处**都曾经是对的**,
# 只是写完之后实现又长了一大截。这和第 8、9 两项是同一个形状:
# **错误由"项目在长大"这个动作本身引发**,而不是有人写错了什么。
#
# 所以把"实现规模"也变成脚本能拦的东西:任何 "N 个<东西>" 的说法都必须等于
# 从注册表里**实际数出来**的数字。抓不到的写法故意放过(宁可漏,不可误报)。
_ACTUAL_COUNTS: dict[str, int] = {}
try:
    sys.path.insert(0, os.path.join(ROOT, "mini-clickhouse"))
    import minich.storages as _st  # noqa: F401  (导入即注册)
    from minich.aggregates import registered_aggregate_functions as _ra
    from minich.formats import registered_formats as _rfo
    from minich.functions.base import registered_functions as _rf
    from minich.settings import registered_settings as _rs
    from minich.storages.base import registered_storage_engines as _re
    from minich.storages.table_functions import registered_table_functions as _rtf
    from minich.system import system_table_names as _rst
    from minich.types import registered_type_names as _rt

    # 编码器没有"注册表"函数可数(它们是 `CompressionCodecFactory.get` 里
    # 一串 `if up == "..."` 分支),所以直接数 `compression.py` 里的具体类。
    # 这一条是补票:原先只有 README 说"8 种压缩编码",而 Appendix B 的
    # 文件树注释里列了 10 个 —— 两处都不假思索地写对了自己那一半,
    # 于是差了两个(LZ4HC 与链式容器 Multiple)。
    _CODEC_SRC = read(os.path.join(ROOT, "mini-clickhouse", "minich", "compression.py"))
    _ACTUAL_COUNTS = {
        "functions": len(_rf()),
        "aggregates": len(_ra()),
        "engines": len(_re()),
        "formats": len(_rfo()),
        "types": len(_rt()),
        "settings": len(_rs()),
        "system": len(_rst()),
        "table_functions": len(_rtf()),
        "codecs": len(re.findall(r"(?m)^class (CompressionCodec\w*)\(", _CODEC_SRC)),
    }
except Exception as _exc:  # pragma: no cover - 实现未装载时跳过这一项
    notes.append(
        f"[WARN] 跳过「实现规模」检查:无法导入 minich({type(_exc).__name__})"
    )

if _ACTUAL_COUNTS:
    _COUNT_PATTERNS = [
        (r"(\d+)\s*个函数名", "functions", "标量函数"),
        (r"注册了\s*\**(\d+)\**\s*个函数", "functions", "标量函数"),
        (r"(\d+)\s*个标量函数", "functions", "标量函数"),
        (r"(\d+)\s*个聚合函数", "aggregates", "聚合函数"),
        (r"(\d+)\s*个表引擎", "engines", "表引擎"),
        (r"引擎注册了\s*\**(\d+)\**\s*个", "engines", "表引擎"),
        (r"(\d+)\s*个表函数", "table_functions", "表函数"),
        (r"(\d+)\s*种格式", "formats", "格式"),
        (r"格式\s*\**(\d+)\**\s*个", "formats", "格式"),
        (r"(\d+)\s*种压缩编码", "codecs", "压缩编码"),
        (r"(\d+)\s*个类型族", "types", "类型族"),
        (r"类型族\s*\**(\d+)\**\s*个", "types", "类型族"),
        (r"(\d+)\s*条设置", "settings", "设置"),
        (r"设置\s*\**(\d+)\**\s*条", "settings", "设置"),
        (r"(\d+)\s*张\s*`?system`?\s*表", "system", "system 表"),
        (r"系统表\s*\**(\d+)\**\s*张", "system", "system 表"),
    ]
    count_bad: list[str] = []
    for _p in docs:
        for _ln, _line in enumerate(read(_p).split("\n"), 1):
            for _rx, _key, _label in _COUNT_PATTERNS:
                for _m in re.finditer(_rx, _line):
                    _claimed = int(_m.group(1))
                    _actual = _ACTUAL_COUNTS[_key]
                    if _claimed != _actual:
                        count_bad.append(
                            f"{rel(_p)}:{_ln} 自述「{_claimed} 个{_label}」,"
                            f"注册表里实际是 {_actual}"
                        )
    if count_bad:
        failures.append(
            "实现规模数字与注册表不一致:\n    " + "\n    ".join(count_bad[:20])
        )
    else:
        notes.append(
            "实现规模数字与注册表一致("
            + ", ".join(f"{k}={v}" for k, v in sorted(_ACTUAL_COUNTS.items()))
            + ")"
        )

# ── 12. 实现规模的行数/文件数必须当场数得出来 ────────────────────────────
# 和第 11 项同源,只是数的是"代码有多长"而不是"注册了多少个东西"。
# 这一项抓的是附录 B 里 82 243 / 84 347 打架那类错误:两处都自称来自
# "同一次快照",却给了两个数 —— 因为其中一处是上一轮快照的残留。
#
# 判据故意收紧:**只认**"行/文件数紧跟在一个数字后面、且同一行里出现了
# `minich`"的写法,并且跳过带"约/万"的近似值。宁可漏,不可误报。
_MINICH_DIR = os.path.join(ROOT, "mini-clickhouse", "minich")


def _actual_minich_scale() -> tuple[int, int] | None:
    try:
        files = [
            p
            for p in glob.glob(os.path.join(_MINICH_DIR, "**", "*.py"), recursive=True)
            if "__pycache__" not in p
        ]
        total = 0
        for p in files:
            with open(p, encoding="utf-8", errors="replace") as fh:
                total += sum(1 for _ in fh)
        return len(files), total
    except Exception:  # pragma: no cover - 实现缺失时跳过这一项
        return None


_SCALE = _actual_minich_scale()
if _SCALE is None:
    notes.append("[WARN] 跳过「实现规模行数」检查:读不到 mini-clickhouse/minich")
else:
    _n_files, _n_lines = _SCALE
    # 只认**总量**的写法:"N 个 `.py` 文件、M 行"。刻意不匹配
    # "minich/sql/ 4 个文件 4 873 行"(那是子包小计)和
    # "part.py(1881 行)"(那是单文件)—— 它们是合法的局部计数,
    # 早期版本把它们当成总量来比,于是 18 条里 17 条是误报。
    # 一个总在误报的检查,很快就会被所有人忽略。
    _TOTAL_CLAIM = re.compile(
        r"(\d+)\s*个\s*`?\.py`?\s*(?:文件)?\s*[、,，]?\s*\**\s*(\d[\d ]*)\**\s*行"
    )
    scale_bad: list[str] = []
    for _p in docs:
        for _ln, _line in enumerate(read(_p).split("\n"), 1):
            for _m in _TOTAL_CLAIM.finditer(_line):
                _cf = int(_m.group(1))
                _cl = int(_m.group(2).replace(" ", ""))
                if _cf != _n_files:
                    scale_bad.append(
                        f"{rel(_p)}:{_ln} 自述「{_cf} 个 .py」,实际 {_n_files} 个"
                    )
                if _cl != _n_lines:
                    scale_bad.append(
                        f"{rel(_p)}:{_ln} 自述「{_cl} 行」,实际 {_n_lines} 行"
                    )
    # 同一形状的第三半:正文(不只附录 B)里也会写死**单个模块**的行数 ——
    # 典型是各章末尾"与 mini-clickhouse 的对照"里那句"`merger.py`(992 行,
    # 合并本体与家族折叠)"。这一类的取证范围原先只覆盖"总量"(上面那段)
    # 和"附录 B 的那棵树"(第 14 项),于是第 18 章里 `merger.py` 的 971 与
    # `ttl.py` 的 468 在实现长到 992 / 475 之后,一直没人发现。
    # 判据:`` `x.py`(N 行)`` 这种**带反引号包裹 + 括号行数**的写法。
    # 只用**唯一**基名(同名文件在多个子包里出现的,跳过,宁可漏不可误报)。
    _by_base: dict[str, set[int]] = {}
    for _fp in glob.glob(os.path.join(_MINICH_DIR, "**", "*.py"), recursive=True):
        if "__pycache__" in _fp:
            continue
        with open(_fp, encoding="utf-8", errors="replace") as _fh:
            _by_base.setdefault(os.path.basename(_fp), set()).add(sum(1 for _ in _fh))
    _unique = {k: next(iter(v)) for k, v in _by_base.items() if len(v) == 1}
    _FILE_CLAIM = re.compile(r"`([\w_]+\.py)`\s*[（(]\s*([\d, ]+?)\s*行")
    for _p in docs:
        for _ln, _line in enumerate(read(_p).split("\n"), 1):
            for _m in _FILE_CLAIM.finditer(_line):
                _bn = _m.group(1)
                _cl = int(_m.group(2).replace(" ", "").replace(",", ""))
                if _bn in _unique and _cl != _unique[_bn]:
                    scale_bad.append(
                        f"{rel(_p)}:{_ln} 自述 `{_bn}` {_cl} 行,实际 {_unique[_bn]} 行"
                    )

    if scale_bad:
        failures.append("实现行数/文件数不一致:\n    " + "\n    ".join(scale_bad[:20]))
    else:
        notes.append(
            f"实现规模行数一致(minich: {_n_files} 个 .py、{_n_lines} 行,逐文件核对)"
        )

# ── 13. 测试结果行必须全书一致 ──────────────────────────────────────────
# README 写 2565 passed、附录 B 写 2565 但 xfailed 写 46 —— 这类漂移的
# 根源是"数字被复制到了多个地方,而只有一处会跟着重跑更新"。
# 判据:所有 `N passed, M skipped, K xfailed[...]` 元组必须唯一。
_RESULT_RE = re.compile(
    r"(\d+)\s+passed,\s*(\d+)\s+skipped,\s*(\d+)\s+xfailed"
    r"(?:,\s*(\d+)\s+xpassed)?"
)
results_seen: set[tuple[int, int, int]] = set()
results_where: dict[tuple[int, int, int], str] = {}
for _p in docs:
    for _ln, _line in enumerate(read(_p).split("\n"), 1):
        for _m in _RESULT_RE.finditer(_line):
            _key = (int(_m.group(1)), int(_m.group(2)), int(_m.group(3)))
            results_seen.add(_key)
            results_where.setdefault(_key, f"{rel(_p)}:{_ln}")

if len(results_seen) > 1:
    failures.append(
        "测试结果行不一致(同一份代码只能有一个实测结果):\n    "
        + "\n    ".join(
            f"{k[0]} passed / {k[1]} skipped / {k[2]} xfailed  ← {results_where[k]}"
            for k in sorted(results_seen)
        )
    )
elif results_seen:
    _k = next(iter(results_seen))
    notes.append(
        f"测试结果行一致({_k[0]} passed, {_k[1]} skipped, {_k[2]} xfailed)"
    )
else:
    notes.append("测试结果行一致(未在文档中声明实测结果)")

# ── 14. 附录 B 的文件树必须与磁盘逐行一致 ────────────────────────────────
# 第 12 项只查"总量",但附录 B 的 B.1 是一棵**逐文件**的树,每行都写着一个
# 行数。总量对得上、单个文件对不上,是完全可能的 —— 而且真的发生过:
# 总量改了,`part.py` / `key_condition.py` 那几行还是上一轮快照的值。
# 这棵树是读者用来判断"该读哪个文件"的依据,所以它也得由机器保证。
# 这一章由**内容自己认领**:哪个文件里写着 `## B.1 `,哪个就是附录 B。
# 早先这里写的是 ``chapters.get(32)`` —— 一给正文插一章,那个数字就过期了,
# 和第 8、9 两项是同一个形状的错误,只是这回错在检查脚本自己身上:
# 它一边警告"数字是快照、快照会过期",一边把自己也钉死在快照上。
_TREE_CH = next(
    (p for p in chapters.values() if re.search(r"^## B\.1 ", read(p), re.M)),
    None,
)
if _TREE_CH is None or not os.path.isdir(_MINICH_DIR):
    notes.append("[WARN] 跳过「附录 B 文件树」检查:找不到含 `## B.1` 的章节或 minich/")
else:
    _tree_actual: dict[str, int] = {}
    for _p in glob.glob(os.path.join(_MINICH_DIR, "**", "*.py"), recursive=True):
        if "__pycache__" in _p:
            continue
        _rel = os.path.relpath(_p, os.path.join(ROOT, "mini-clickhouse")).replace("\\", "/")
        with open(_p, encoding="utf-8", errors="replace") as _fh:
            _tree_actual[_rel] = sum(1 for _ in _fh)

    _tree_txt = read(_TREE_CH)
    _m1 = re.search(r"^## B\.1 ", _tree_txt, re.M)
    _m2 = re.search(r"^## B\.2 ", _tree_txt, re.M)
    _tree_bad: list[str] = []
    if not (_m1 and _m2):
        _tree_bad.append("找不到 B.1 / B.2 小节边界")
    else:
        _pkgs: dict[str, str] = {}      # 包路径 → 该包在树里出现的位置
        _seen: set[str] = set()
        _pkg = None
        for _ln, _line in enumerate(_tree_txt[_m1.end():_m2.start()].split("\n"), 1):
            _mh = re.match(r"^(minich\S*?/)\s+(\d+) 个文件\s+([\d ]+) 行", _line)
            if _mh:
                _pkg = _mh.group(1)
                _files = [k for k in _tree_actual if os.path.dirname(k) == _pkg.rstrip("/")]
                _nf, _nl = int(_mh.group(2)), int(_mh.group(3).replace(" ", ""))
                if _nf != len(_files):
                    _tree_bad.append(
                        f"B.1 {_pkg}:自述 {_nf} 个文件,实际 {len(_files)} 个"
                    )
                _al = sum(_tree_actual[k] for k in _files)
                if _nl != _al:
                    _tree_bad.append(f"B.1 {_pkg}:自述 {_nl} 行,实际 {_al} 行")
                continue
            _mr = re.match(r"^\s*([\d ]*\d)\s\s(\S+\.py)", _line)
            if _mr and _pkg:
                _key = _pkg.rstrip("/") + "/" + _mr.group(2)
                _claimed = int(_mr.group(1).replace(" ", ""))
                _seen.add(_key)
                if _key not in _tree_actual:
                    _tree_bad.append(f"B.1 列出了不存在的文件 {_key}")
                elif _claimed != _tree_actual[_key]:
                    _tree_bad.append(
                        f"B.1 {_key}:自述 {_claimed} 行,实际 {_tree_actual[_key]} 行"
                    )
        for _k in sorted(set(_tree_actual) - _seen):
            _tree_bad.append(f"B.1 漏列了文件 {_k}")
    if _tree_bad:
        failures.append("附录 B 文件树与磁盘不一致:\n    " + "\n    ".join(_tree_bad[:20]))
    else:
        notes.append(f"附录 B 文件树与磁盘一致({len(_tree_actual)} 个文件逐行核对)")

# ── 15. 文档里自述的"检查项数"必须等于本脚本真实执行的数量 ──────────────
# 这是第 8、9、11–14 项那个形状的最后一次应用,对象是**这份脚本自己**:
# 每加一项检查,README 与附录 B 里"11 项检查""13 项检查"的说法就全部过期。
# 与其每次手工同步,不如让脚本数自己:实现段落都写成 ``# ── N. 标题``,
# 数一遍就是真实项数。
_self_src = read(os.path.abspath(__file__))
# 注意 ``# ── 3./4. ...`` 这种合并写的标题:它一次覆盖两项检查,
# 所以不能数"有几行标题",而要数标题里有几个编号。
_n_checks = sum(
    len(re.findall(r"\d+", _g))
    for _g in re.findall(r"^# ── ([\d./]+)", _self_src, re.M)
)
_CHECK_COUNT_RE = re.compile(r"(\d+)\s*项(?:检查|结构自检|结构检查|全通过)")
count_bad: list[str] = []
for _p in docs:
    for _ln, _line in enumerate(read(_p).split("\n"), 1):
        for _m in _CHECK_COUNT_RE.finditer(_line):
            if int(_m.group(1)) != _n_checks:
                count_bad.append(
                    f"{rel(_p)}:{_ln} 自述「{_m.group(1)} 项检查」,实际 {_n_checks} 项"
                )
if count_bad:
    failures.append("自述检查项数过期:\n    " + "\n    ".join(count_bad[:20]))
else:
    notes.append(f"自述检查项数与实际一致({_n_checks} 项)")

# ── 16. "N 个关键设计"必须等于真实的小节数 ──────────────────────────────
# 同一个形状再出现一次:那一章的十个"关键设计"小节被别处反复引用
# ("九个关键设计"),而**加一节设计**这个动作会让所有引用一起过期。
# 判据:数 `## N.M 关键设计…` 形式的标题。章号同样**不写死** ——
# 由内容自己认领(和第 14 项同一个理由:插一章就会让硬编码的章号过期)。
_DESIGN_RE = re.compile(r"^##\s+(\d+)\.\d+\s+关键设计", re.M)
_DESIGN_CH = next(
    ((n, p) for n, p in chapters.items() if _DESIGN_RE.search(read(p))),
    None,
)
if _DESIGN_CH is None:
    notes.append("[WARN] 跳过「关键设计计数」检查:没有哪一章含 `## N.M 关键设计` 小节")
else:
    _n_ch, _design_path = _DESIGN_CH
    _n_designs = len(_DESIGN_RE.findall(read(_design_path)))
    _design_bad: list[str] = []
    for _p in docs:
        for _ln, _line in enumerate(read(_p).split("\n"), 1):
            for _m in re.finditer(r"(\d+)?\s*([一二三四五六七八九十]+)\s*个关键设计", _line):
                if _m.group(1):                      # 阿拉伯数字写法
                    _claimed = int(_m.group(1))
                else:                                # 中文数字写法
                    _cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                    _claimed = _cn.get(_m.group(2), 0)
                if _claimed != _n_designs:
                    _design_bad.append(
                        f"{rel(_p)}:{_ln} 自述「{_m.group(0).strip()}」,"
                        f"实际第 {_n_ch} 章有 {_n_designs} 个关键设计"
                    )
    if _design_bad:
        failures.append("关键设计计数过期:\n    " + "\n    ".join(_design_bad[:20]))
    else:
        notes.append(f"关键设计计数一致(第 {_n_ch} 章 {_n_designs} 个)")

# ── 17. 子包行数必须与磁盘一致 ──────────────────────────────────────────
# 第 12 项**刻意**只认"总量"句式(避免把子包小计误报成总量),代价是
# **子包小计本身没人管**。这一轮就真的漏了一次:附录 B 写
# "`storages/merge_tree/` 12 142 行、占存储层 69%",而磁盘上是 12 586 行
# —— 总量对得上、单个子包对不上。同一段里"14 个布尔位"(实为 12)、
# "这四个"(实列了 5 个)这类**数量词**也是同一形状,但它们要求读懂句子,
# 不宜交给正则,留给人工复核。
#
# 判据故意收窄:文件里出现的子包路径取自**磁盘上真实存在的目录**,
# 且数字必须紧跟在路径之后 20 个字符以内。宁可漏,不可误报。
_SUBPKG_DIRS = sorted(
    {
        os.path.dirname(os.path.relpath(p, os.path.join(ROOT, "mini-clickhouse"))).replace("\\", "/") + "/"
        for p in glob.glob(os.path.join(_MINICH_DIR, "**", "*.py"), recursive=True)
        if "__pycache__" not in p
    }
    - {"minich/"},
    key=len,
    reverse=True,
)
_SUBPKG_ACTUAL: dict[str, int] = {}
for _p in glob.glob(os.path.join(_MINICH_DIR, "**", "*.py"), recursive=True):
    if "__pycache__" in _p:
        continue
    _d = os.path.dirname(os.path.relpath(_p, os.path.join(ROOT, "mini-clickhouse"))).replace("\\", "/") + "/"
    with open(_p, encoding="utf-8", errors="replace") as _fh:
        _SUBPKG_ACTUAL[_d] = _SUBPKG_ACTUAL.get(_d, 0) + sum(1 for _ in _fh)

_subpkg_bad: list[str] = []
for _p in docs:
    for _ln, _line in enumerate(read(_p).split("\n"), 1):
        if "约" in _line:                      # 近似值放过
            continue
        for _d in _SUBPKG_DIRS:
            _idx = _line.find(_d)
            if _idx < 0:
                continue
            # 路径后面紧跟标识符/点,说明指的是**子包里的某个文件**
            # (如 `minich/storages/merge_tree/marks.py`),不是子包小计 ——
            # 否则会把文件名后面的行数算到子包头上(早期版本真这么误报过)。
            _nxt = _line[_idx + len(_d): _idx + len(_d) + 1]
            if _nxt and (_nxt.isalnum() or _nxt in "_."):
                continue
            _tail = _line[_idx + len(_d): _idx + len(_d) + 20]
            _m = re.search(r"(\d[\d ]*)\s*行", _tail)
            if not _m:
                continue
            _claimed = int(_m.group(1).replace(" ", ""))
            _actual = _SUBPKG_ACTUAL.get(_d, 0)
            if _claimed != _actual:
                _subpkg_bad.append(
                    f"{rel(_p)}:{_ln} 自述「{_d} {_claimed} 行」,实际 {_actual} 行"
                )
if _subpkg_bad:
    failures.append("子包行数与磁盘不一致:\n    " + "\n    ".join(_subpkg_bad[:20]))
else:
    notes.append("子包行数与磁盘一致(逐子包核对)")

# ── 18. 自述的"本脚本行数"必须与磁盘一致 ────────────────────────────────
# 第 12 项管 `minich/` 的规模数字,第 14 项管附录 B 那棵树,第 17 项管子包小计
# —— 三者的取证范围都**止于 `minich/`**。于是 `tools/check_docs.py` 自己
# 的行数掉进了缝里:附录 B 的 B.5.3 写"`tools/check_docs.py`(704 行)",
# 而磁盘上已经是 742 行,检查脚本年年跑绿,这个数字年年是错的。
#
# 这个漏检的形状和第 17 项一模一样:**上一项检查为了不误报而收窄了范围,
# 收窄掉的那部分就没人管了**。所以每加一项检查,都值得问一句
# "它把什么排除在外了"。
#
# 判据同样收窄:只认 ``check_docs.py``(N 行)`` 这种"路径后面紧跟括号行数"
# 的写法(B.1 那棵树里的裸数字由第 14 项负责,这里不重复管)。
_ACTUAL_SCRIPT_LINES = sum(1 for _ in open(os.path.abspath(__file__), encoding="utf-8"))
_SELF_LINES_RE = re.compile(r"check_docs\.py`?\s*[（(]\s*(\d[\d ]*)\s*行")
_self_lines_bad: list[str] = []
for _p in docs:
    for _ln, _line in enumerate(read(_p).split("\n"), 1):
        if "约" in _line:                      # 近似值放过
            continue
        for _m in _SELF_LINES_RE.finditer(_line):
            _claimed = int(_m.group(1).replace(" ", ""))
            if _claimed != _ACTUAL_SCRIPT_LINES:
                _self_lines_bad.append(
                    f"{rel(_p)}:{_ln} 自述「check_docs.py {_claimed} 行」,"
                    f"实际 {_ACTUAL_SCRIPT_LINES} 行"
                )
if _self_lines_bad:
    failures.append("自述脚本行数不一致:\n    " + "\n    ".join(_self_lines_bad[:20]))
else:
    notes.append(f"自述脚本行数与磁盘一致({_ACTUAL_SCRIPT_LINES} 行)")

# ── 19. `tests/` 规模矩阵必须与磁盘一致 ──────────────────────────────────
# 第 12、14、17 项的取证范围都止于 `minich/`,第 18 项止于 `tools/` ——
# `tests/` 从来没人管。于是这一轮真的漏了:B.5.1 那张表停在"23 个文件、
# 891 个函数",而磁盘上是 25 个文件、923 个函数。新增的两个
# `test_regressions_*.py` 一共带进来 34 条用例,连带把全书最显眼的那个
# ``2779 passed`` 也一起变成了过期值 —— 而它恰恰是 README 里**唯一**一个
# 宣称"由仓库里的命令产出,不是声明"的数字。
#
# 判据:附录 B 里写着 `## B.5.1` 的那一节,逐行核对那张表。
_TESTS_DIR = os.path.join(ROOT, "mini-clickhouse", "tests")
_test_actual: dict[str, tuple[int, int]] = {}
for _p in sorted(glob.glob(os.path.join(_TESTS_DIR, "test_*.py"))):
    with open(_p, encoding="utf-8") as _fh:
        _nl = sum(1 for _ in _fh)
    _test_actual[os.path.basename(_p)] = (
        len(re.findall(r"(?m)^[ \t]*def test_", read(_p))),
        _nl,
    )
_test_fun_total = sum(v[0] for v in _test_actual.values())
_test_line_total = sum(v[1] for v in _test_actual.values())

_TEST_CH = next(
    (p for p in chapters.values() if re.search(r"^### B\.5\.1 ", read(p), re.M)),
    None,
)
if _TEST_CH is None or not _test_actual:
    notes.append("[WARN] 跳过「tests/ 规模矩阵」检查:找不到 B.5.1 或 tests/")
else:
    _t_txt = read(_TEST_CH)
    _tm1 = re.search(r"^### B\.5\.1 ", _t_txt, re.M)
    _tm2 = re.search(r"^### B\.5\.2 ", _t_txt, re.M)
    _t_bad: list[str] = []
    if not (_tm1 and _tm2):
        _t_bad.append("找不到 B.5.1 / B.5.2 小节边界")
    else:
        _tbody = _t_txt[_tm1.end(): _tm2.start()]
        _seen_t: set[str] = set()
        _row_re = re.compile(
            r"^\|\s*`([\w_]+\.py)`\s*\|\s*(\d+)\s*\|\s*([\d ]+?)\s*\|\s*$"
        )
        _tot_re = re.compile(
            r"^\|\s*\*\*合计\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*([\d ]+?)\*\*\s*\|\s*$"
        )
        _claimed_total: tuple[int, int] | None = None
        for _line in _tbody.split("\n"):
            _mt = _tot_re.match(_line)
            if _mt:
                _claimed_total = (int(_mt.group(1)), int(_mt.group(2).replace(" ", "")))
                continue
            _mr = _row_re.match(_line)
            if not _mr:
                continue
            _fname = _mr.group(1)
            _cf, _cl = int(_mr.group(2)), int(_mr.group(3).replace(" ", ""))
            _seen_t.add(_fname)
            if _fname not in _test_actual:
                _t_bad.append(f"B.5.1 列了 `{_fname}`,磁盘上没有这个文件")
                continue
            _af, _al = _test_actual[_fname]
            if _cf != _af:
                _t_bad.append(f"B.5.1 {_fname}:自述 {_cf} 个测试函数,实际 {_af} 个")
            if _cl != _al:
                _t_bad.append(f"B.5.1 {_fname}:自述 {_cl} 行,实际 {_al} 行")
        for _fname in sorted(set(_test_actual) - _seen_t):
            _t_bad.append(f"B.5.1 漏列了测试文件 `{_fname}`")
        if _claimed_total is None:
            _t_bad.append("B.5.1 找不到「合计」行")
        elif _claimed_total != (_test_fun_total, _test_line_total):
            _t_bad.append(
                f"B.5.1 合计:自述 {_claimed_total[0]} 个函数 / {_claimed_total[1]} 行,"
                f"实际 {_test_fun_total} 个 / {_test_line_total} 行"
            )
        _mn = re.search(r"\*\*(\d+)\s*个\*\*\s*`test_\*\.py`", _tbody)
        if _mn and int(_mn.group(1)) != len(_test_actual):
            _t_bad.append(
                f"B.5.1 自述 {_mn.group(1)} 个 `test_*.py` 文件,实际 {len(_test_actual)} 个"
            )
    if _t_bad:
        failures.append("tests/ 规模矩阵与磁盘不一致:\n    " + "\n    ".join(_t_bad[:20]))
    else:
        notes.append(
            f"tests/ 规模矩阵与磁盘一致({len(_test_actual)} 个文件、"
            f"{_test_fun_total} 个测试函数、{_test_line_total} 行)"
        )

# ── 20. "展开后收集 N 条"必须等于 pytest 当场收集的条数 ──────────────────
# 这一项和第 11–19 项都不一样:它**必须真的调用一次外部命令**。原因是
# "参数化展开后有多少条"是**参数化的产物**,静态读源码数不出来 ——
# 源码里只有 923 个 `def test_`,展开后却是 2 945 条。数不出来的东西,
# 只能让它自己报。
#
# 代价约 1 秒(只收集、不执行),换来的是:B.5.1 的"收集 N 条"与
# README 里那句 ``N passed, M skipped, K xfailed`` 再也不会同时过期。
# 注意**不要**加 `-p no:cacheprovider` —— 那会让 pyproject 里的
# `cache_dir` 变成未知选项,凭空冒出一条 warning,而 B.5.1 明确断言
# 测试过程"没有 warning"。
_cp = None
try:
    _cp = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=os.path.join(ROOT, "mini-clickhouse"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
except Exception as _exc:  # pragma: no cover - 环境缺 pytest 时跳过
    notes.append(f"[WARN] 跳过「收集条数」检查:跑不起 pytest({_exc.__class__.__name__})")

if _cp is not None:
    _collected = sum(
        int(_m.group(1))
        for _m in re.finditer(r"(?m)^tests/[\w_]+\.py: (\d+)\s*$", _cp.stdout or "")
    )
    if _collected == 0:
        _hint = ((_cp.stderr or _cp.stdout or "").strip().split("\n") or [""])[-1]
        notes.append(f"[WARN] 跳过「收集条数」检查:pytest 没报出条数({_hint[:60]})")
    else:
        _c_bad: list[str] = []
        for _p in docs:
            for _ln, _line in enumerate(read(_p).split("\n"), 1):
                _mc = re.search(r"实际收集\s*\*\*([\d ]+?)\s*条\*\*", _line)
                if _mc and int(_mc.group(1).replace(" ", "")) != _collected:
                    _c_bad.append(
                        f"{rel(_p)}:{_ln} 自述收集 "
                        f"{int(_mc.group(1).replace(' ', ''))} 条,实际 {_collected} 条"
                    )
                _mr = _RESULT_RE.search(_line)
                if _mr:
                    _p_, _s_, _x_ = (int(_mr.group(i)) for i in (1, 2, 3))
                    if _p_ + _s_ + _x_ != _collected:
                        _c_bad.append(
                            f"{rel(_p)}:{_ln} 自述 {_p_}+{_s_}+{_x_}={_p_ + _s_ + _x_},"
                            f"但 pytest 收集了 {_collected} 条"
                        )
        if _c_bad:
            failures.append("收集条数与 pytest 不符:\n    " + "\n    ".join(_c_bad[:20]))
        else:
            notes.append(f"收集条数与 pytest 一致({_collected} 条)")

# ── 输出 ────────────────────────────────────────────────────────────────
print("文档侧自检(%d 篇正文 + %d 个 README)" % (len(files), len(docs) - len(files)))
print("=" * 68)
for n in notes:
    print("  " + n)
if failures:
    print("\n失败项:")
    for f in failures:
        print("  [FAIL] " + f)
    print("=" * 68)
    print(f"结果:{len(failures)} 项失败")
    sys.exit(1)
print("=" * 68)
print("结果:全部通过")
sys.exit(0)
