"""Validate the tutorial's structure, links, code fences and declared counts."""

from __future__ import annotations

import re
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
        fences = text.count("```")
        if fences % 2:
            error(f"{relative(path)}: unbalanced code fences ({fences})")
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


def check_appendices() -> list[Path]:
    files = sorted(APPENDICES.glob("[A-Z]-*.md"))
    if not files:
        error("appendices/: no appendix files found")
        return []
    expected = [chr(ord("A") + index) for index in range(len(files))]
    found = [p.name[0] for p in files]
    if found != expected:
        error(f"appendices/: numbering must be continuous from A, found {found}")
    for path in files:
        text = read(path)
        if not text:
            continue
        first = text.lstrip().splitlines()[0] if text.strip() else ""
        if not re.match(r"^# 附录 [A-Z]\s+\S", first):
            error(f"{relative(path)}: first heading must be '# 附录 X ...' (got {first!r})")
        if text.count("```") % 2:
            error(f"{relative(path)}: unbalanced code fences")
        for block in re.findall(r"```yaml\n(.*?)```", text, re.S):
            try:
                list(yaml.safe_load_all(block))
            except yaml.YAMLError as exc:
                first_line = str(exc).splitlines()[0] if str(exc) else "invalid YAML"
                error(f"{relative(path)}: fenced YAML does not parse: {first_line}")
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


def check_chapter_references(chapter_files: list[Path], appendix_files: list[Path]) -> None:
    """Catch stale bare references such as '第 30 章' after a renumbering."""
    numbers = {int(path.name[:2]) for path in chapter_files}
    for path in chapter_files + appendix_files + [README]:
        text = read(path)
        for match in CHAPTER_REF.finditer(text):
            number = int(match.group(1))
            if number not in numbers:
                error(f"{relative(path)}: reference to missing chapter {number}")
    reference_link = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
    for path in chapter_files + appendix_files + [README]:
        text = read(path)
        for match in reference_link.finditer(text):
            label, target = match.group(1), match.group(2).strip()
            filename = re.match(r"(\d{2})-", target)
            if filename and "第 " in label:
                referenced = re.search(r"第 (\d{1,2}) 章", label)
                if referenced and int(referenced.group(1)) != int(filename.group(1)):
                    error(f"{relative(path)}: link label {label!r} points to {target}")


def check_readme(chapter_files: list[Path], appendix_files: list[Path]) -> None:
    text = read(README)
    if not text:
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
        error(f"README.md: must declare '**{test_count} passed**' (actual test functions: {test_count})")
    if f"**{scenario_count} / {scenario_count} passed**" not in text:
        error(f"README.md: must declare '**{scenario_count} / {scenario_count} passed**'")
    if f"**{chapter_count} 章 + {appendix_count} 篇附录**" not in text:
        error(f"README.md: must declare '**{chapter_count} 章 + {appendix_count} 篇附录**'")
    if "python -m pytest" not in text or "python examples/run_all.py" not in text or "python check_docs.py" not in text:
        error("README.md: missing one of the three verification commands")


def count_tests() -> int:
    total = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = read(path)
        total += len(re.findall(r"^def test_", text, flags=re.MULTILINE))
    return total


def check_test_count_claims(chapter_files: list[Path], appendix_files: list[Path]) -> None:
    """Keep prose test counts in sync with the actual test functions."""
    actual = count_tests()
    claim = re.compile(r"(\d+)\s*个测试")
    for path in chapter_files + appendix_files:
        text = read(path)
        for match in claim.finditer(text):
            declared = int(match.group(1))
            if declared != actual:
                line = text.count("\n", 0, match.start()) + 1
                error(
                    f"{relative(path)}:{line}: declares {declared} tests, "
                    f"actual test functions: {actual}"
                )


def count_scenarios() -> int:
    text = read(ROOT / "examples" / "run_all.py")
    return len(re.findall(r"\)\s*,\s*\{\}\),", text)) or len(re.findall(r"^def scenario_", text, flags=re.MULTILINE))


def check_examples() -> int:
    files = sorted((ROOT / "examples").glob("[0-9][0-9]-*.yaml"))
    if not files:
        error("examples/: no YAML scenarios found")
    for path in files:
        text = read(path)
        try:
            documents = [doc for doc in yaml.safe_load_all(text) if doc is not None]
        except yaml.YAMLError as exc:
            error(f"{relative(path)}: invalid YAML: {exc}")
            continue
        if not documents:
            error(f"{relative(path)}: empty YAML")
        for document in documents:
            if "apiVersion" not in document or "kind" not in document:
                error(f"{relative(path)}: every document needs apiVersion and kind")
            if not document.get("metadata", {}).get("name"):
                error(f"{relative(path)}: every document needs metadata.name")
    return len(files)


def check_code_compiles() -> None:
    import compileall
    if not compileall.compile_dir(str(ROOT / "mini_k8s"), quiet=1, force=True):
        error("mini_k8s/: Python compilation failed")


def main() -> int:
    chapter_files = check_chapters()
    appendix_files = check_appendices()
    check_chapter_references(chapter_files, appendix_files)
    check_test_count_claims(chapter_files, appendix_files)
    check_readme(chapter_files, appendix_files)
    example_count = check_examples()
    check_code_compiles()

    print("MiniK8s tutorial document check")
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
    print("\nAll document checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
