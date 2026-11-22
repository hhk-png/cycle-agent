#!/usr/bin/env python
"""Lightweight consistency checks for the tutorial's Markdown.

The checks are deliberately structural, not stylistic:

* every ``chapters/*.md`` file has a unique, sequential number (00..N) or
  appendix letter (A..Z);
* ``SUMMARY.md`` lists every chapter exactly once, in order;
* every relative Markdown link resolves to an existing file;
* ``第 N 章`` and ``N.M`` section references point inside the known ranges;
* the counts advertised in ``README.md`` match the files on disk.

It cannot verify prose or numbers; those still need a human/LLM audit.  The
point is to catch the failure mode where a chapter is added and only half the
navigation is updated.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = ROOT / "chapters"
SUMMARY = ROOT / "SUMMARY.md"
README = ROOT / "README.md"

MAIN_RE = re.compile(r"^# 第 (\d{2,}) 章\s+(.+)$")
APPENDIX_RE = re.compile(r"^# 附录 ([A-Z])\s+(.+)$")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
CHAPTER_REF_RE = re.compile(r"第\s*(\d{2,})\s*章")
# Only explicit references such as "见 24.5", "第 36.1 节", "§4.3".  A bare
# decimal (3.14, 2.38) is a number, not a section reference.
SECTION_REF_RE = re.compile(r"(?:见|参见|详见|第)\s*(\d{1,2})\.(\d{1,2})(?:\.\d{1,2})?")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def strip_code(text: str) -> str:
    """Remove fenced code blocks so code like ``x[tokens]`` is not a link."""
    return FENCE_RE.sub("", text)


def parse_chapter(path: Path) -> tuple[str, int | str, str] | None:
    first = path.read_text(encoding="utf-8").splitlines()[0].strip()
    m = MAIN_RE.match(first)
    if m:
        return "main", int(m.group(1)), m.group(2)
    m = APPENDIX_RE.match(first)
    if m:
        return "appendix", m.group(1), m.group(2)
    return None


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    files = sorted(CHAPTERS.glob("*.md"))
    mains: dict[int, Path] = {}
    appendices: dict[str, Path] = {}
    for path in files:
        parsed = parse_chapter(path)
        if parsed is None:
            errors.append(f"{path.name}: 首行不是 '# 第 NN 章 …' 或 '# 附录 X …'")
            continue
        kind, number, title = parsed
        if kind == "main":
            if number in mains:
                errors.append(
                    f"{path.name}: 章号 {number:02d} 与 {mains[number].name} 重复"
                )
            mains[number] = path
        else:
            key = str(number)
            if key in appendices:
                errors.append(
                    f"{path.name}: 附录 {key} 与 {appendices[key].name} 重复"
                )
            appendices[key] = path

    if mains:
        expected = set(range(0, max(mains) + 1))
        missing = sorted(expected - set(mains))
        if missing:
            errors.append(f"主章节号不连续，缺: {missing}")
    if appendices:
        expected_letters = [chr(c) for c in range(ord("A"), max(ord(x) for x in appendices) + 1)]
        missing = [x for x in expected_letters if x not in appendices]
        if missing:
            errors.append(f"附录字母不连续，缺: {missing}")

    # SUMMARY must list every file exactly once.
    if SUMMARY.exists():
        summary_text = SUMMARY.read_text(encoding="utf-8")
        listed = set()
        for target in LINK_RE.findall(summary_text):
            if target.startswith("chapters/"):
                listed.add(Path(target).name)
        actual = {p.name for p in files}
        for name in sorted(actual - listed):
            errors.append(f"SUMMARY.md 未列出 {name}")
        for name in sorted(listed - actual):
            errors.append(f"SUMMARY.md 列出了不存在的 {name}")
        occurrences = re.findall(r"\(chapters/([^)]+\.md)\)", summary_text)
        duplicates = {x for x in occurrences if occurrences.count(x) > 1}
        for name in sorted(duplicates):
            errors.append(f"SUMMARY.md 重复列出 {name}")

    # Relative links in all Markdown files.
    markdown_files = [README, SUMMARY, *files]
    for path in markdown_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for target in LINK_RE.findall(strip_code(text)):
            target = target.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"{path.name}: 链接不存在 -> {target}")

    # Chapter/section reference ranges.
    max_main = max(mains) if mains else -1
    headings: dict[int, set[tuple[int, int]]] = {}
    for number, path in mains.items():
        text = path.read_text(encoding="utf-8")
        headings[number] = {
            (int(a), int(b))
            for a, b in re.findall(r"^## (\d+)\.(\d+)", text, flags=re.MULTILINE)
        }
    for path in markdown_files:
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for ref in CHAPTER_REF_RE.findall(line):
                value = int(ref)
                if not 0 <= value <= max_main:
                    errors.append(f"{path.name}:{lineno}: 第 {value} 章 超出范围 0..{max_main}")
            for chapter, section in SECTION_REF_RE.findall(line):
                # Skip obvious versions/decimals and appendix-internal headings.
                if path.name[:1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" and path.name[1:2] == "-":
                    continue
                c, s = int(chapter), int(section)
                if c in headings and (c, s) not in headings[c] and s != 0:
                    warnings.append(
                        f"{path.name}:{lineno}: 引用了 {c}.{s}，但该章没有这一节"
                    )

    # README count claims.
    if README.exists():
        text = README.read_text(encoding="utf-8")
        m = re.search(r"(\d+)\s*章教程（00–(\d+)）", text)
        if m:
            claimed, last = int(m.group(1)), int(m.group(2))
            if claimed != len(mains):
                errors.append(f"README 声称 {claimed} 章，实际 {len(mains)} 章")
            if last != max_main:
                errors.append(f"README 声称 00–{last}，实际 00–{max_main}")
        m = re.search(r"附录 A[–-]([A-Z])", text)
        if m and appendices:
            last_letter = max(appendices)
            if m.group(1) != last_letter:
                errors.append(
                    f"README 声称附录 A–{m.group(1)}，实际 A–{last_letter}"
                )

    print(f"chapters: {len(mains)} main + {len(appendices)} appendices")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for warning in warnings[:80]:
            print(f"  - {warning}")
        if len(warnings) > 80:
            print(f"  ... 还有 {len(warnings) - 80} 条")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("docs consistency: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
