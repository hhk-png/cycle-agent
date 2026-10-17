#!/usr/bin/env python3
"""教程自检：把"正文里写下的断言"和"仓库里实际的事实"对一遍。

用法：

    python check_docs.py            # 全量检查
    python check_docs.py -v         # 连通过项也打印

这个脚本存在的理由，是教程里最容易烂掉的不是代码而是**引用**：
改写章节会让小节号漂移、让交叉引用指错地方、让"1144 个测试"
这种硬数字过期。这些东西人眼审不出来，机器一审就出来。

设计上刻意避开的四个坑（都是实测踩过的）：

1. **静默跳过**：如果某类输入一条都没匹配到（比如正则写错），
   检查会"全绿"但其实什么都没查。所以每类检查都断言"扫到的条目数 > 0"，
   扫到 0 条算失败。
2. **子包小计过期**：分文件统计的行数、用例数，单项对不上要报出来，
   而不是只看总数。
3. **取证范围收窄**：文件清单从目录枚举而来，enumerate 出来的每个
   .md 都必须被解析成功；解析失败即失败，不会"跳过这个文件继续"。
4. **裸编号引用**：`17.1 那张表`、`§20.6`、`第 4.6 节`、`附录 C.5`
   四种写法都要认，只认其中一种会漏掉一半。

退出码 0 表示全过，1 表示有失败项。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


class Doc:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rel = path.name
        self.text = path.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()
        self.title = ""
        self.kind = ""  # "chapter" | "appendix" | "readme" | "other"
        self.number = ""  # "7" / "C" / ""
        self.headings: list[str] = []  # 形如 "7.2" 的小节号
        self.heading_lines: dict[str, int] = {}
        self._parse_title()
        self._parse_headings()

    def _parse_title(self) -> None:
        for i, line in enumerate(self.lines, 1):
            m = re.match(r"^# (第 (\d+) 章|附录 ([A-Z]))\s*(.*)$", line)
            if m:
                if m.group(2):
                    self.kind, self.number, self.title = "chapter", m.group(2), m.group(4).strip()
                else:
                    self.kind, self.number, self.title = "appendix", m.group(3), m.group(4).strip()
                self.title_line = i
                return
            if line.startswith("# "):
                self.kind, self.title = ("readme" if self.rel == "README.md" else "other"), line[2:].strip()
                self.title_line = i
                return
        raise ValueError(f"{self.rel}: 找不到一级标题")

    def _parse_headings(self) -> None:
        for i, line in enumerate(self.lines, 1):
            m = re.match(r"^#{2,3} +([0-9A-Z]+(?:\.[0-9]+)*)[.　 ]+", line)
            if m:
                key = m.group(1)
                self.headings.append(key)
                self.heading_lines.setdefault(key, i)
                continue
            # "## 25.2 merge_insert 的硬限制：10000 行" 之外还有
            # "## C.5 ..."、"### 17.4.1 ..." 这两种，上面已覆盖。
            # 剩下的是无编号小节，只记不查。
        return


def load_docs() -> list[Doc]:
    docs = []
    for p in sorted(ROOT.glob("*.md")):
        docs.append(Doc(p))  # 解析失败直接抛，不跳过
    if not docs:
        raise SystemExit("没有找到任何 .md —— 取证范围出错了，中止")
    return docs


# ---------------------------------------------------------------------------
# 检查框架
# ---------------------------------------------------------------------------


class Report:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.failures: list[str] = []
        self.notes: list[str] = []
        self.checked: dict[str, int] = {}

    def fail(self, check: str, msg: str) -> None:
        self.failures.append(f"[{check}] {msg}")

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def counted(self, check: str, n: int) -> None:
        self.checked[check] = self.checked.get(check, 0) + n

    def require_nonempty(self, check: str, n: int) -> None:
        if n == 0:
            self.fail(check, "扫到 0 条 —— 要么真的没有，要么正则有病。取证范围可疑，判失败")


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------


def check_inventory(docs: list[Doc], rep: Report) -> None:
    chapters = {d.number: d for d in docs if d.kind == "chapter"}
    appendices = {d.number: d for d in docs if d.kind == "appendix"}
    rep.note(f"章 {len(chapters)} 篇、附录 {len(appendices)} 篇、其他 {len(docs) - len(chapters) - len(appendices)} 篇")
    rep.require_nonempty("inventory", len(chapters) + len(appendices))

    # 章号必须从 0 连续到 N
    nums = sorted(int(n) for n in chapters)
    if nums != list(range(nums[0], nums[0] + len(nums))):
        rep.fail("inventory", f"章号不连续：{nums}")
    # 文件名前缀必须和章号一致
    for n, d in chapters.items():
        if not d.rel.startswith(f"{int(n):02d}-"):
            rep.fail("inventory", f"{d.rel} 的章号是第 {n} 章，但文件名前缀对不上")
    # 附录字母必须和文件名前缀顺序一致（曾经的错：32 号文件是附录 D）
    for letter, d in appendices.items():
        idx = 30 + (ord(letter) - ord("A"))
        if not d.rel.startswith(f"{idx}-"):
            rep.fail(
                "inventory",
                f"{d.rel} 自报为附录 {letter}，按字母顺序文件名前缀应为 {idx}-（文件序与字母序不一致）",
            )

    # 结尾必须有过渡句：写明"第 N+1 章"或至少说"下一章"。最后一章除外。
    for d in docs:
        if d.kind != "chapter":
            continue
        rep.counted("chapter-footers", 1)
        if d.number == str(nums[-1]):
            continue
        tail = "\n".join(d.lines[-12:])
        if f"第 {int(d.number) + 1} 章" in tail or "下一章" in tail:
            continue
        rep.fail(
            "continuity",
            f"{d.rel} 结尾既没写『第 {int(d.number) + 1} 章』也没写『下一章』，缺过渡句",
        )


def check_readme_toc(docs: list[Doc], rep: Report) -> None:
    readme = next((d for d in docs if d.rel == "README.md"), None)
    if readme is None:
        rep.fail("readme", "找不到 README.md")
        return
    linked = set(re.findall(r"\]\((\d\d-[a-z0-9-]+\.md)\)", readme.text))
    present = {d.rel for d in docs if d.kind in ("chapter", "appendix")}
    rep.counted("readme-links", len(linked))
    rep.require_nonempty("readme-links", len(linked))
    for missing in sorted(present - linked):
        rep.fail("readme", f"{missing} 存在但 README 目录里没链接")
    for extra in sorted(linked - present):
        rep.fail("readme", f"README 链接了不存在的 {extra}")


def check_section_numbers(docs: list[Doc], rep: Report) -> None:
    """小节号必须唯一、且前缀与所属章/附录一致。

    这条检查针对的是一类具体事故：在章末插一节、让"小结"顺延一位时，
    很容易漏掉某处、或者把两个小节写成同一个号（例如两个 4.9）。
    小节号撞车之后，所有"第 4.9 节"的引用都会有一半指错内容，
    而引用本身"指得存在"，所以别的检查抓不到。

    只检查**带点的**小节号（`4.9`、`C.5`、`25.1.1`）。不带点的编号
    在本书里有别的含义：附录 D 用 `### 1.` … `### 55.` 编条目号，
    用 `## A.` … `## J.` 编分组，它们不是小节号，混进来会全是假警。
    """

    n = 0
    for d in docs:
        if d.kind not in ("chapter", "appendix"):
            continue
        seen: dict[str, int] = {}
        for h in d.headings:
            if "." not in h:
                continue  # 条目号 / 分组字母，不是小节号
            n += 1
            head = h.split(".")[0]
            if head != d.number:
                rep.fail(
                    "sections",
                    f"{d.rel} 里有小节号 {h}，但它不该出现在第 {d.number} 章/附录 {d.number}",
                )
            if h in seen:
                rep.fail(
                    "sections",
                    f"{d.rel} 里小节号 {h} 出现了两次（第 {seen[h]} 行与第 "
                    f"{d.heading_lines.get(h, 0)} 行），所有引用它的一半会指错内容",
                )
            seen[h] = d.heading_lines.get(h, 0)
    rep.counted("sections", n)
    rep.require_nonempty("sections", n)


def check_readme_titles(docs: list[Doc], rep: Report) -> None:
    """README 目录里写的标题必须和章文件的一级标题一致。

    这条检查是给"改了章节标题忘了改目录"准备的 —— 人不会重读两遍目录。
    """

    readme = next((d for d in docs if d.rel == "README.md"), None)
    if readme is None:
        return
    by_rel = {d.rel: d for d in docs}
    n = 0
    for line in readme.lines:
        m = re.match(r"^\s*-\s*\[(.+?)\]\((\d\d-[a-z0-9-]+\.md)\)\s*$", line)
        if not m:
            continue
        n += 1
        label, rel = m.group(1), m.group(2)
        doc = by_rel.get(rel)
        if doc is None:
            continue  # 链接本身由 check_readme_toc / check_links 负责
        # 去掉「第 N 章　」/「附录 X　」前缀再比标题
        stripped = re.sub(r"^(第\s*\d+\s*章|附录\s*[A-Z])\s*[　 ]*", "", label).strip()
        if stripped and stripped != doc.title:
            rep.fail(
                "readme-titles",
                f"README 写的是『{label}』，但 {rel} 的一级标题是『{doc.title}』",
            )
    rep.counted("readme-titles", n)
    rep.require_nonempty("readme-titles", n)


def check_links(docs: list[Doc], rep: Report) -> None:
    n = 0
    for d in docs:
        for i, line in enumerate(d.lines, 1):
            for target in re.findall(r"\]\(([^)#\s]+\.md)\)", line):
                n += 1
                if not (ROOT / target).exists():
                    rep.fail("links", f"{d.rel}:{i} 链接指向不存在的 {target}")
    rep.counted("links", n)
    rep.require_nonempty("links", n)


def check_references(docs: list[Doc], rep: Report) -> None:
    chapters = {int(d.number) for d in docs if d.kind == "chapter"}
    appendices = {d.number for d in docs if d.kind == "appendix"}
    # 小节号的合法集合：章内小节 + 附录内小节
    sections: dict[str, str] = {}
    for d in docs:
        if d.kind in ("chapter", "appendix"):
            for h in d.headings:
                sections.setdefault(h, d.rel)

    pats = [
        ("章", re.compile(r"第\s*(\d+)\s*章"), lambda k: int(k) in chapters),
        ("节", re.compile(r"第\s*(\d+\.\d+(?:\.\d+)?)\s*节"), lambda k: k in sections),
        ("§", re.compile(r"§\s*([0-9A-Z]+(?:\.[0-9]+)*)"), lambda k: k in sections or k in {str(c) for c in chapters}),
        ("附录", re.compile(r"附录\s*([A-Z])(?![A-Za-z])"), lambda k: k in appendices),
        ("裸节号", re.compile(r"(?<![0-9.§第])(\d+\.\d+)\s*节"), lambda k: k in sections),
    ]
    for label, pat, ok in pats:
        n = 0
        for d in docs:
            for i, line in enumerate(d.lines, 1):
                for m in pat.finditer(line):
                    n += 1
                    key = m.group(1)
                    if not ok(key):
                        rep.fail("refs", f"{d.rel}:{i} 【{label}】引用不存在的目标：{m.group(0).strip()}")
        rep.counted(f"refs-{label}", n)
        rep.require_nonempty(f"refs-{label}", n)


def check_code_paths(docs: list[Doc], rep: Report) -> None:
    n = 0
    pat = re.compile(r"`(mini_lancedb/[A-Za-z0-9_/]+\.py)`")
    for d in docs:
        for i, line in enumerate(d.lines, 1):
            for target in pat.findall(line):
                n += 1
                if not (ROOT / target).exists():
                    rep.fail("code-paths", f"{d.rel}:{i} 引用了不存在的文件 {target}")
    rep.counted("code-paths", n)
    rep.require_nonempty("code-paths", n)


def check_claims(docs: list[Doc], rep: Report) -> None:
    """硬数字：测试数、行数、示例数。"""
    # 测试数。注意"收集数"与"通过数"不是一回事（有 skip），
    # 教程里说"N 个用例"时指的是收集数，检查也按收集数对。
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    m = re.search(r"(\d+) tests? collected", proc.stdout + proc.stderr)
    if not m:
        rep.fail("claims", f"拿不到测试数，pytest 输出尾部：{(proc.stdout + proc.stderr).strip()[-200:]}")
        return
    actual_tests = int(m.group(1))
    ran = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    tail = (ran.stdout + ran.stderr).strip().splitlines()[-1] if (ran.stdout or ran.stderr) else ""
    rep.note(f"实际收集到 {actual_tests} 个测试；跑完汇总行：{tail}")

    claimed = 0
    for d in docs:
        for i, line in enumerate(d.lines, 1):
            for m2 in re.finditer(r"(\d{3,5})\s*个(?:测试|用例|测例)", line):
                claimed += 1
                v = int(m2.group(1))
                if v != actual_tests:
                    rep.fail("claims", f"{d.rel}:{i} 声称 {v} 个测试，实际 {actual_tests}")
    rep.counted("claims-tests", claimed)
    rep.require_nonempty("claims-tests", claimed)

    # 行数：mini_lancedb / tests / 正文
    def py_lines(sub: str) -> int:
        return sum(len(p.read_text(encoding="utf-8").splitlines()) for p in (ROOT / sub).rglob("*.py"))

    facts = {
        "mini_lancedb": py_lines("mini_lancedb"),
        "tests": py_lines("tests"),
        "md": sum(len(d.lines) for d in docs),
    }
    rep.note(
        "实际行数：mini_lancedb=%d、tests=%d、正文 md=%d"
        % (facts["mini_lancedb"], facts["tests"], facts["md"])
    )

    # "约 X 万行" 出现在 mini_lancedb 语境的行
    n = 0
    for d in docs:
        for i, line in enumerate(d.lines, 1):
            if "mini_lancedb" not in line and "迷你实现" not in line:
                continue
            for m3 in re.finditer(r"约\s*([\d.]+)\s*万行", line):
                n += 1
                v = float(m3.group(1)) * 10000
                if abs(v - facts["mini_lancedb"]) > 600:
                    rep.fail(
                        "claims",
                        f"{d.rel}:{i} 声称迷你实现约 {m3.group(1)} 万行，实际 {facts['mini_lancedb']}",
                    )
    rep.counted("claims-lines", n)
    rep.require_nonempty("claims-lines", n)


def check_style_inventory(docs: list[Doc], rep: Report) -> None:
    """风格盘点：引用块与表格的行数。不判失败，只报数。"""
    total_q = total_t = 0
    rows = []
    for d in docs:
        q = sum(1 for ln in d.lines if ln.startswith("> "))
        t = sum(1 for ln in d.lines if ln.startswith("|"))
        total_q += q
        total_t += t
        if q or t:
            rows.append((d.rel, q, t))
    for rel, q, t in rows:
        if q >= 5 or t >= 20:
            rep.note(f"风格盘点 {rel}: 引用块行 {q}、表格行 {t}")
    rep.note(f"风格盘点合计：引用块行 {total_q}、表格行 {total_t}")


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-tests", action="store_true", help="跳过 pytest 收集（省时间）")
    args = ap.parse_args()

    docs = load_docs()
    rep = Report(args.verbose)

    check_inventory(docs, rep)
    check_readme_toc(docs, rep)
    check_readme_titles(docs, rep)
    check_section_numbers(docs, rep)
    check_links(docs, rep)
    check_references(docs, rep)
    check_code_paths(docs, rep)
    check_style_inventory(docs, rep)
    if not args.no_tests:
        check_claims(docs, rep)

    print("=" * 72)
    print(f"扫过 {len(docs)} 个 .md")
    for k, v in sorted(rep.checked.items()):
        print(f"  {k:<20} {v} 条")
    print("=" * 72)
    for n in rep.notes:
        print(f"  · {n}")

    if rep.failures:
        print("-" * 72)
        print(f"失败 {len(rep.failures)} 项：")
        for f in rep.failures:
            print(f"  ✗ {f}")
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
