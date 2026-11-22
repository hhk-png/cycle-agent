#!/usr/bin/env python3
"""Validate the tutorial against repository facts.

Checks:
  * chapter/appendix numbering and titles;
  * required sections, balanced code fences, placeholder text;
  * relative links and chapter/appendix/section references;
  * real verl v0.9.1 paths against a generated manifest;
  * MiniVerl/test/example paths mentioned in prose;
  * hard numbers (tests, examples, chapters, appendices) against reality.

Usage:
    python -X utf8 check_docs.py
    python -X utf8 check_docs.py -v
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAPTERS = ROOT / "chapters"
APPENDICES = ROOT / "appendices"
README = ROOT / "README.md"
MANIFEST = ROOT / "reference" / "verl-v0.9.1-paths.json"

HISTORICAL_PATHS = {
    "verl/workers/fsdp_workers.py",
    "verl/workers/megatron_workers.py",
    "verl/workers/actor/dp_actor.py",
    "verl/workers/actor/megatron_actor.py",
    "verl/workers/critic/dp_critic.py",
    "verl/workers/sharding_manager",
    "verl/workers/rollout/vllm_rollout_spmd.py",
}

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
REF_SECTION = re.compile(r"第\s*(\d+)\.(\d+)\s*节")
REF_SECTION2 = re.compile(r"§\s*(\d+)\.(\d+)")
REF_APPENDIX = re.compile(r"附录\s*([A-Z])(?:\.(\d+))?")
REF_CHAPTER = re.compile(r"第\s*(\d+)\s*章")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
PLACEHOLDER = re.compile(r"\b(TODO|TBD|FIXME)\b|待补充|待完善")


class Doc:
    def __init__(self, path: Path):
        self.path = path
        self.rel = path.relative_to(ROOT).as_posix()
        self.text = path.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()
        self.kind = "other"
        self.number = ""
        self.title = ""
        self.headings: list[str] = []
        self._parse()

    def _parse(self) -> None:
        found_title = False
        for line in self.lines:
            if found_title:
                break
            m = re.match(r"^# 第 (\d+) 章\s*(.*)$", line)
            if m:
                self.kind, self.number, self.title = "chapter", m.group(1), m.group(2).strip()
                found_title = True
                continue
            m = re.match(r"^# 附录 ([A-Z])\s*(.*)$", line)
            if m:
                self.kind, self.number, self.title = "appendix", m.group(1), m.group(2).strip()
                found_title = True
                continue
            if line.startswith("# "):
                self.kind = "readme" if self.rel == "README.md" else "other"
                self.title = line[2:].strip()
                found_title = True
        if not found_title:
            raise ValueError(f"{self.rel}: no level-1 heading")
        for line in self.lines:
            m = re.match(r"^##\s+([0-9A-Z]+(?:\.[0-9]+)+)\.?\s+", line)
            if m:
                self.headings.append(m.group(1))


def parse_all() -> list[Doc]:
    docs = [Doc(p) for p in sorted(CHAPTERS.glob("*.md"))]
    docs += [Doc(p) for p in sorted(APPENDICES.glob("*.md"))]
    docs.append(Doc(README))
    return docs


def load_manifest() -> set[str]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {p.rstrip("/") for p in data["paths"]}


def chapter_files() -> list[Path]:
    return sorted(CHAPTERS.glob("*.md"))


def appendix_files() -> list[Path]:
    return sorted(APPENDICES.glob("*.md"))


def example_scripts() -> list[Path]:
    return sorted((ROOT / "examples").glob("[0-9][0-9]_*.py"))


def collect_test_count() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        return -1, proc.stdout + proc.stderr
    count = 0
    for line in proc.stdout.splitlines():
        if re.match(r"tests/[^:]+::", line):
            count += 1
    return count, proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    notes: list[str] = []

    def fail(message: str) -> None:
        failures.append(message)

    def note(message: str) -> None:
        notes.append(message)

    # --------------------------------------------------------------
    # inventory
    # --------------------------------------------------------------
    chapters = chapter_files()
    appendices = appendix_files()
    if not chapters:
        fail("chapters/: no files")
    if not appendices:
        fail("appendices/: no files")

    expected_chapter_numbers = list(range(1, len(chapters) + 1))
    actual_chapter_numbers = []
    for path in chapters:
        m = re.match(r"^(\d{2})-", path.name)
        if not m:
            fail(f"{path.name}: filename must start with NN-")
            continue
        actual_chapter_numbers.append(int(m.group(1)))
    if actual_chapter_numbers != expected_chapter_numbers:
        fail(f"chapter numbering is not continuous: {actual_chapter_numbers}")

    expected_letters = [chr(ord("A") + i) for i in range(len(appendices))]
    actual_letters = [path.name[0] for path in appendices]
    if actual_letters != expected_letters:
        fail(f"appendix numbering is not continuous: {actual_letters}")

    docs = parse_all()
    by_number = {doc.number: doc for doc in docs if doc.kind == "chapter"}
    by_letter = {doc.number: doc for doc in docs if doc.kind == "appendix"}

    for doc in docs:
        if doc.kind == "chapter":
            expected = f"第 {doc.number} 章"
            if not doc.title:
                fail(f"{doc.rel}: empty chapter title")
            if not doc.path.name.startswith(f"{int(doc.number):02d}-"):
                fail(f"{doc.rel}: filename prefix does not match chapter number")
            if not doc.text.startswith(f"# {expected}"):
                fail(f"{doc.rel}: first heading must start with '# {expected}'")
        elif doc.kind == "appendix":
            if not doc.path.name.startswith(f"{doc.number}-"):
                fail(f"{doc.rel}: appendix filename must start with '{doc.number}-'")
            if not doc.text.startswith(f"# 附录 {doc.number}"):
                fail(f"{doc.rel}: first heading must start with '# 附录 {doc.number}'")

    # required sections and fences
    for doc in docs:
        if doc.kind != "chapter":
            continue
        for section in ("## 本章小结", "## 延伸阅读"):
            if section not in doc.text:
                fail(f"{doc.rel}: missing '{section}'")
        if doc.text.count("```") % 2:
            fail(f"{doc.rel}: unbalanced code fences ({doc.text.count('```')})")
        if PLACEHOLDER.search(doc.text):
            fail(f"{doc.rel}: contains placeholder text")

    for doc in docs:
        if doc.kind != "appendix":
            continue
        if doc.text.count("```") % 2:
            fail(f"{doc.rel}: unbalanced code fences")

    # headings
    for doc in docs:
        if doc.kind not in {"chapter", "appendix"}:
            continue
        prefix = doc.number
        pat = re.compile(rf"^##\s+{re.escape(prefix)}\.(\d+)\b")
        found = [int(m.group(1)) for line in doc.lines if (m := pat.match(line))]
        if found and found != list(range(1, len(found) + 1)):
            fail(f"{doc.rel}: section numbering is not continuous: {found[:12]}")
        if not found:
            note(f"{doc.rel}: no numbered sections")

    # --------------------------------------------------------------
    # links
    # --------------------------------------------------------------
    for doc in docs:
        for target in LINK.findall(doc.text):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            destination = (doc.path.parent / path_part).resolve()
            try:
                destination.relative_to(ROOT)
            except ValueError:
                fail(f"{doc.rel}: link escapes project root: {target}")
                continue
            if not destination.exists():
                fail(f"{doc.rel}: broken link {target}")

    # --------------------------------------------------------------
    # cross references
    # --------------------------------------------------------------
    all_section_keys = set()
    for doc in docs:
        if doc.kind in {"chapter", "appendix"}:
            for line in doc.lines:
                m = re.match(rf"^##\s+({re.escape(doc.number)}\.\d+)\b", line)
                if m:
                    all_section_keys.add(m.group(1))

    for doc in docs:
        for chapter, section in REF_SECTION.findall(doc.text) + REF_SECTION2.findall(doc.text):
            key = f"{chapter}.{section}"
            if key not in all_section_keys:
                fail(f"{doc.rel}: reference to missing section §{key}")
        for letter, section in REF_APPENDIX.findall(doc.text):
            if letter not in by_letter:
                fail(f"{doc.rel}: reference to missing appendix {letter}")
            elif section and f"{letter}.{section}" not in all_section_keys:
                fail(f"{doc.rel}: reference to missing appendix section {letter}.{section}")
        for number in REF_CHAPTER.findall(doc.text):
            if number not in by_number:
                fail(f"{doc.rel}: reference to missing chapter {number}")

    # --------------------------------------------------------------
    # paths in prose
    # --------------------------------------------------------------
    real_paths = load_manifest()
    for doc in docs:
        for span in CODE_SPAN.findall(doc.text):
            token = span.strip()
            if not token:
                continue
            token = token.split("::", 1)[0].strip()
            token = token.rstrip("/")
            if "..." in token:
                continue
            if token.startswith(("mini_verl/", "tests/", "examples/", "scripts/")):
                if any(ch in token for ch in "*<>{}"):
                    continue
                if (ROOT / token).exists():
                    continue
                # Some paths refer to the upstream verl tree (examples/grpo_trainer,
                # scripts/generate_trainer_config.sh, ...).  Accept them if they are
                # in the v0.9.1 manifest.
                if token in real_paths:
                    continue
                fail(f"{doc.rel}: referenced path does not exist: {token}")
            elif token.startswith("verl/"):
                if any(ch in token for ch in "*<>{}"):
                    continue
                if token in HISTORICAL_PATHS:
                    continue
                if token not in real_paths:
                    fail(f"{doc.rel}: verl path not in v0.9.1 manifest: {token}")

    # --------------------------------------------------------------
    # hard numbers
    # --------------------------------------------------------------
    test_count, pytest_output = collect_test_count()
    if test_count < 0:
        fail("pytest --collect-only failed:\n" + pytest_output[-2000:])
        test_count = 0
    example_count = len(example_scripts())
    note(f"facts: tests={test_count}, examples={example_count}, chapters={len(chapters)}, appendices={len(appendices)}")

    readme_text = README.read_text(encoding="utf-8")
    if f"**{test_count} passed**" not in readme_text:
        fail(f"README must contain '**{test_count} passed**'")
    if f"**{example_count} / {example_count} passed**" not in readme_text:
        fail(f"README must contain '**{example_count} / {example_count} passed**'")
    if f"**{len(chapters)} 章**" not in readme_text:
        fail(f"README must contain '**{len(chapters)} 章**'")
    if f"**{len(appendices)} 篇**" not in readme_text:
        fail(f"README must contain '**{len(appendices)} 篇**'")

    for doc in docs:
        if doc.kind not in {"chapter", "appendix", "readme"}:
            continue
        for number in re.findall(r"(\d+)\s*个测试", doc.text):
            if int(number) != test_count:
                fail(f"{doc.rel}: says {number} tests, repository has {test_count}")
        for number in re.findall(r"(\d+)\s*个示例", doc.text):
            if int(number) != example_count:
                fail(f"{doc.rel}: says {number} examples, repository has {example_count}")

    # README must link every chapter and appendix
    for doc in docs:
        if doc.kind in {"chapter", "appendix"}:
            if doc.path.name not in readme_text:
                fail(f"README does not link {doc.path.name}")

    # --------------------------------------------------------------
    # report
    # --------------------------------------------------------------
    if args.verbose:
        for message in notes:
            print(f"[note] {message}")
        print(f"[note] checked {len(docs)} documents")
    if failures:
        print(f"FAILED: {len(failures)} problem(s)")
        for message in failures:
            print(f"  - {message}")
        return 1
    print(
        f"OK: {len(chapters)} chapters, {len(appendices)} appendices, "
        f"{test_count} tests, {example_count} examples, all references resolved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
