#!/usr/bin/env python
"""Static checks for the tutorial documentation.

This is deliberately conservative: it checks structure, links, code-fence
balance and placeholder markers, not prose quality.  Run it in CI alongside
pytest and the examples.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
README = ROOT / "README.md"

CHAPTER_NUMBERS = list(range(1, 48))
EXPECTED_APPENDICES = "ABCDEFGHIJKLM"
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*```")
PLACEHOLDERS = re.compile(r"\b(TODO|TBD|FIXME)\b|在此填写|待补充")


def _strip_query_and_anchor(target: str) -> str:
    target = target.strip()
    if " " in target and not target.startswith(("http://", "https://")):
        # Markdown title syntax: path "title"
        target = target.split(" ", 1)[0]
    return unquote(target.split("#", 1)[0])


def check_structure(errors: list[str]) -> tuple[list[Path], list[Path]]:
    if not README.is_file():
        errors.append("README.md is missing")
        return [], []
    if not DOCS.is_dir():
        errors.append("docs/ is missing")
        return [], []

    chapters = [DOCS / f"{number:02d}-" for number in CHAPTER_NUMBERS]
    chapter_files = [path for prefix in chapters for path in DOCS.glob(prefix.name + "*.md")]
    chapter_files.sort()
    if len(chapter_files) != len(CHAPTER_NUMBERS):
        errors.append(
            f"expected {len(CHAPTER_NUMBERS)} numbered chapters, found {len(chapter_files)}"
        )

    for number, path in zip(CHAPTER_NUMBERS, chapter_files):
        text = path.read_text(encoding="utf-8")
        if not re.search(rf"^# 第 {number} 章", text, re.MULTILINE):
            errors.append(f"{path.name}: missing heading '# 第 {number} 章 ...'")
        if "## 小结" not in text:
            errors.append(f"{path.name}: missing '## 小结'")
        if len(text) < 800:
            errors.append(f"{path.name}: suspiciously short ({len(text)} chars)")

    preface = DOCS / "00-preface.md"
    conclusion = DOCS / "99-conclusion.md"
    for path, heading in [(preface, "# 前言"), (conclusion, "# 结语")]:
        if not path.is_file():
            errors.append(f"{path.name}: missing")
        elif heading not in path.read_text(encoding="utf-8"):
            errors.append(f"{path.name}: missing heading {heading!r}")

    appendices = sorted(DOCS.glob("appendix-*.md"))
    if len(appendices) != len(EXPECTED_APPENDICES):
        errors.append(
            f"expected {len(EXPECTED_APPENDICES)} appendices, found {len(appendices)}"
        )
    for index, path in enumerate(appendices):
        text = path.read_text(encoding="utf-8")
        letter = EXPECTED_APPENDICES[index] if index < len(EXPECTED_APPENDICES) else "?"
        if not re.search(rf"^# 附录 {letter}\b", text, re.MULTILINE):
            errors.append(f"{path.name}: expected heading '# 附录 {letter}'")

    expected_names = {
        "00-preface.md",
        "99-conclusion.md",
        *[path.name for path in chapter_files],
        *[path.name for path in appendices],
    }
    actual_names = {path.name for path in DOCS.glob("*.md")}
    unexpected = sorted(actual_names - expected_names)
    if unexpected:
        errors.append(f"unexpected docs files: {unexpected}")

    return chapter_files, appendices


def check_readme(
    errors: list[str], docs: list[Path], chapter_count: int, appendix_count: int
) -> None:
    text = README.read_text(encoding="utf-8")
    expected = f"{chapter_count} 章正文 + {appendix_count} 个附录"
    if expected not in text:
        errors.append(f"README must state {expected!r}")
    # The count is now derived from the filesystem.  Catch the common failure
    # mode where a chapter is added but an old literal survives elsewhere.
    for number in range(1, 200):
        if number == chapter_count:
            continue
        # Use a digit boundary: "45 章正文" must not be read as "5 章正文".
        if re.search(rf"(?<!\d){number} 章正文", text):
            errors.append(f"README contains stale chapter count {number} 章正文")
            break
    for path in docs:
        relative = f"docs/{path.name}"
        if relative not in text:
            errors.append(f"README does not link {relative}")
    for phrase in [
        "python -m pytest -q",
        "python scripts/check_docs.py",
        "python scripts/verify.py",
    ]:
        if phrase not in text:
            errors.append(f"README missing command: {phrase}")


def check_markdown(path: Path, errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    fence_count = sum(1 for line in lines if FENCE_RE.match(line))
    if fence_count % 2:
        errors.append(f"{path.name}: unbalanced code fences ({fence_count})")
    if PLACEHOLDERS.search(text):
        errors.append(f"{path.name}: contains a TODO/TBD/placeholder marker")
    if "C:\\Users\\" in text or "/Users/" in text:
        errors.append(f"{path.name}: contains a local absolute user path")

    # Check links outside fenced code blocks.
    in_fence = False
    for line_number, line in enumerate(lines, start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK_RE.finditer(line):
            raw = match.group(1).strip()
            if raw.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_text = _strip_query_and_anchor(raw)
            if not target_text:
                continue
            target = (path.parent / target_text).resolve()
            try:
                target.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"{path.name}:{line_number}: link escapes tutorial root: {raw}")
                continue
            if not target.exists():
                errors.append(f"{path.name}:{line_number}: broken link: {raw}")


def main() -> int:
    errors: list[str] = []
    chapter_files, appendices = check_structure(errors)
    if README.is_file() and DOCS.is_dir():
        check_readme(
            errors,
            [DOCS / "00-preface.md", *chapter_files, DOCS / "99-conclusion.md", *appendices],
            len(chapter_files),
            len(appendices),
        )
        for path in [README, *sorted(DOCS.glob("*.md"))]:
            check_markdown(path, errors)

    if errors:
        print("Documentation check FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1

    total_tokens = sum(len(path.read_text(encoding="utf-8")) for path in DOCS.glob("*.md"))
    print(
        f"Documentation check passed: {len(chapter_files)} chapters, "
        f"{len(appendices)} appendices, {total_tokens:,} chars."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
