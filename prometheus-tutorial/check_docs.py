"""Validate the tutorial's structure, links, YAML fences and declared counts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CHAPTERS = ROOT / "chapters"
APPENDICES = ROOT / "appendices"
README = ROOT / "README.md"

errors: list[str] = []
warnings: list[str] = []


def error(message: str) -> None:
    errors.append(message)


def warn(message: str) -> None:
    warnings.append(message)


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        error(f"{relative(path)}: not valid UTF-8 ({exc})")
        return ""


def check_chapters() -> list[Path]:
    files = sorted(CHAPTERS.glob("[0-9][0-9]-*.md"))
    if not files:
        error("chapters/: no chapter files found")
        return []
    expected = list(range(1, len(files) + 1))
    numbers = []
    for path in files:
        match = re.match(r"^(\d{2})-", path.name)
        if not match:
            error(f"{relative(path)}: filename must start with NN-")
            continue
        numbers.append(int(match.group(1)))
    if numbers != expected:
        error(f"chapters/: numbering is not continuous: {numbers}")
    for path in files:
        text = read(path)
        if not text:
            continue
        number = int(path.name[:2])
        first = text.lstrip().splitlines()[0] if text.strip() else ""
        if not re.match(rf"^# 第 {number} 章\s+\S", first):
            error(f"{relative(path)}: first heading must be '# 第 {number} 章 ...' (got {first!r})")
        for section in ("本章小结", "延伸阅读"):
            if f"## {section}" not in text:
                error(f"{relative(path)}: missing '## {section}'")
        if text.count("```") % 2:
            error(f"{relative(path)}: unbalanced code fences ({text.count('```')})")
        for block in re.findall(r"```yaml\n(.*?)```", text, re.S):
            try:
                list(yaml.safe_load_all(block))
            except yaml.YAMLError as exc:
                first_line = str(exc).splitlines()[0] if str(exc) else "invalid YAML"
                error(f"{relative(path)}: fenced YAML does not parse: {first_line}")
        if re.search(r"\b(TODO|TBD|FIXME)\b|待补充|待完善", text):
            error(f"{relative(path)}: contains placeholder text")
        check_section_numbering(path, text, number)
        check_links(path, text)
    return files


def check_section_numbering(path: Path, text: str, chapter: int) -> None:
    """Require ``## N.1``, ``## N.2`` ... in order within each chapter.

    This catches renumbering accidents that the filename check cannot see
    (for example, inserting 6.4 twice or leaving a section numbered 7.2 in
    chapter 6).
    """
    expected = 1
    for line in text.splitlines():
        match = re.match(r"^## (\d+)\.(\d+)\s+\S", line)
        if not match:
            continue
        found_chapter, found_section = int(match.group(1)), int(match.group(2))
        if found_chapter != chapter:
            error(
                f"{relative(path)}: section heading uses chapter {found_chapter}, "
                f"expected {chapter}: {line!r}"
            )
            continue
        if found_section != expected:
            error(
                f"{relative(path)}: section {found_chapter}.{found_section} out of "
                f"order, expected {found_chapter}.{expected}"
            )
        expected = found_section + 1


def appendix_label(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA, 27 -> AB (spreadsheet-style)."""
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def appendix_label_from_name(path: Path) -> str:
    match = re.match(r"^([A-Z]+)-", path.name)
    return match.group(1) if match else ""


def appendix_sort_key(path: Path) -> tuple[int, str]:
    label = appendix_label_from_name(path)
    value = 0
    for char in label:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value, path.name


def check_appendices() -> list[Path]:
    files = sorted(APPENDICES.glob("[A-Z]*-*.md"), key=appendix_sort_key)
    if not files:
        error("appendices/: no appendix files found")
        return []
    expected = [appendix_label(index) for index in range(len(files))]
    found = [appendix_label_from_name(p) for p in files]
    if found != expected:
        error(f"appendices/: numbering must be continuous from A, found {found}")
    for path in files:
        text = read(path)
        if not text:
            continue
        first = text.lstrip().splitlines()[0] if text.strip() else ""
        if not re.match(r"^# 附录 [A-Z]+\s+\S", first):
            error(f"{relative(path)}: first heading must be '# 附录 X ...' (got {first!r})")
        if text.count("```") % 2:
            error(f"{relative(path)}: unbalanced code fences")
        for block in re.findall(r"```yaml\n(.*?)```", text, re.S):
            try:
                list(yaml.safe_load_all(block))
            except yaml.YAMLError as exc:
                first_line = str(exc).splitlines()[0] if str(exc) else "invalid YAML"
                error(f"{relative(path)}: fenced YAML does not parse: {first_line}")
        if re.search(r"\b(TODO|TBD|FIXME)\b|待补充|待完善", text):
            error(f"{relative(path)}: contains placeholder text")
        check_links(path, text)
    return files


LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def check_links(path: Path, text: str) -> None:
    for raw_target in LINK.findall(text):
        target = raw_target.strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        destination = (path.parent / target).resolve()
        try:
            destination.relative_to(ROOT)
        except ValueError:
            error(f"{relative(path)}: link escapes project root: {raw_target}")
            continue
        if not destination.exists():
            error(f"{relative(path)}: broken link {raw_target}")


CHAPTER_REF = re.compile(r"第 (\d{1,2}) 章")
APPENDIX_REF = re.compile(r"附录 ([A-Z]{1,2})(?![A-Z])")


def check_references(chapter_files: list[Path], appendix_files: list[Path]) -> None:
    numbers = {int(path.name[:2]) for path in chapter_files}
    letters = {appendix_label_from_name(path) for path in appendix_files}
    for path in chapter_files + appendix_files + [README]:
        text = read(path)
        for match in CHAPTER_REF.finditer(text):
            number = int(match.group(1))
            if number not in numbers:
                error(f"{relative(path)}: reference to missing chapter {number}")
        for match in APPENDIX_REF.finditer(text):
            letter = match.group(1)
            if letter not in letters:
                error(f"{relative(path)}: reference to missing appendix {letter}")


def count_tests() -> int:
    """Prefer pytest's collected count; fall back to function count."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        match = re.search(r"(\d+) tests? collected", result.stdout)
        if match:
            return int(match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    total = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        total += len(re.findall(r"^def test_", read(path), flags=re.MULTILINE))
    warn("falling back to test-function count (pytest collection unavailable)")
    return total


def count_scenarios() -> int:
    text = read(ROOT / "examples" / "run_all.py")
    return len(re.findall(r'^\s*\("\d{2}",', text, flags=re.MULTILINE))


def count_promtool_tests() -> int | None:
    """Run the rule unit tests and return the declared count.

    The README used to say "2 test(s) passed" while the two files actually
    contained three tests.  Verifying the real command keeps the quick-start
    output from drifting again.
    """
    command = [
        sys.executable,
        "-m",
        "mini_prometheus.promtool",
        "test",
        "rules",
        "examples/rules/demo_test.yml",
        "examples/rules/capstone_test.yml",
    ]
    try:
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        error(f"promtool test rules could not run: {exc}")
        return None
    match = re.search(r"SUCCESS: (\d+) test\(s\) passed", result.stdout)
    if result.returncode != 0 or not match:
        detail = (result.stderr or result.stdout).strip().splitlines()
        error(
            "promtool test rules failed or produced no count"
            + (f": {detail[-1]}" if detail else "")
        )
        return None
    return int(match.group(1))


def check_readme(chapter_files: list[Path], appendix_files: list[Path]) -> None:
    text = read(README)
    if not text:
        error("README.md is missing or empty")
        return
    if text.count("```") % 2:
        error("README.md: unbalanced code fences")
    check_links(README, text)

    for path in chapter_files:
        link = f"](chapters/{path.name})"
        if link not in text:
            error(f"README.md: missing TOC link for {path.name}")
    for path in appendix_files:
        link = f"](appendices/{path.name})"
        if link not in text:
            error(f"README.md: missing TOC link for {path.name}")

    test_count = count_tests()
    scenario_count = count_scenarios()
    chapter_count = len(chapter_files)
    appendix_count = len(appendix_files)
    if f"**{test_count} passed**" not in text:
        error(f"README.md: must declare '**{test_count} passed**' (pytest cases: {test_count})")
    if f"**{scenario_count} / {scenario_count} passed**" not in text:
        error(f"README.md: must declare '**{scenario_count} / {scenario_count} passed**'")
    promtool_count = count_promtool_tests()
    if promtool_count is not None and f"**{promtool_count} test(s) passed**" not in text:
        error(
            "README.md: must declare "
            f"'**{promtool_count} test(s) passed**' (promtool rule tests)"
        )
    if f"**{chapter_count} 章 + {appendix_count} 篇附录**" not in text:
        error(
            f"README.md: must declare '**{chapter_count} 章 + {appendix_count} 篇附录**'"
        )
    if (
        "python -m pytest" not in text
        or "python examples/run_all.py" not in text
        or "python check_docs.py" not in text
    ):
        error("README.md: missing one of the three verification commands")
    for phrase in ("Mini-Prometheus", "PromQL", "remote write"):
        if phrase.lower() not in text.lower():
            warn(f"README.md: expected to mention {phrase!r}")


def check_examples() -> int:
    files = sorted((ROOT / "examples").glob("[0-9][0-9]_*.py"))
    if not files:
        error("examples/: no numbered example scripts found")
    for path in files:
        text = read(path)
        if "def run()" not in text:
            error(f"{relative(path)}: missing run()")
    return len(files)


def check_code_compiles() -> None:
    import compileall

    for directory in ("mini_prometheus", "examples"):
        if not compileall.compile_dir(str(ROOT / directory), quiet=1, force=True):
            error(f"{directory}/: Python compilation failed")


def main() -> int:
    chapter_files = check_chapters()
    appendix_files = check_appendices()
    check_references(chapter_files, appendix_files)
    check_readme(chapter_files, appendix_files)
    example_count = check_examples()
    check_code_compiles()

    print("Mini-Prometheus tutorial document check")
    print(f"  chapters:  {len(chapter_files)}")
    print(f"  appendices:{len(appendix_files)}")
    print(f"  examples:  {example_count}")
    print(f"  tests:     {count_tests()}")
    for message in warnings:
        print(f"WARN  {message}")
    for message in errors:
        print(f"ERROR {message}")
    if errors:
        print(f"\n{len(errors)} error(s)")
        return 1
    print("\nOK: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
