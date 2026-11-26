#!/usr/bin/env python3
"""Validate the tutorial's structure, links, code references and stated counts.

This is deliberately dependency-free apart from the project's own test tooling.
It protects against the most common documentation rot: broken local links,
missing chapters, stale counts, unclosed fences and references to files that no
longer exist.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
EXPECTED_CHAPTERS = 32
EXPECTED_APPENDICES = 26
EXPECTED_EXAMPLES = 22
EXPECTED_TESTS = 219
MIN_CHAPTER_CHARS = 1800
MIN_APPENDIX_CHARS = 900
ALLOWED_FENCE_LANGS = {
    "",
    "python",
    "py",
    "bash",
    "sh",
    "shell",
    "powershell",
    "text",
    "plaintext",
    "json",
    "yaml",
    "yml",
    "toml",
    "ini",
    "properties",
    "xml",
    "java",
    "go",
    "javascript",
    "js",
    "typescript",
    "ts",
    "csharp",
    "c#",
    "ruby",
    "php",
    "erlang",
    "sql",
    "http",
    "dockerfile",
    "mermaid",
    "diff",
    "makefile",
    "markdown",
    "console",
}
FORBIDDEN = re.compile(r"\b(?:TODO|TBD|FIXME)\b|待补充|占位符|Lorem ipsum")
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
PATH_RE = re.compile(
    r"(?<![\w/])((?:mini_rabbitmq|examples|tests|chapters|appendices|scripts)/"
    r"[A-Za-z0-9_./-]+\.(?:py|md|json|toml|txt|sh|ps1))"
)


class Checker:
    def __init__(self, *, check_tests: bool = True) -> None:
        self.check_tests = check_tests
        self.errors: list[str] = []
        self.links_checked = 0

    def fail(self, path: Path | str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def run(self) -> int:
        chapters = sorted(ROOT.glob("chapters/[0-9][0-9]-*.md"))
        appendices = sorted(ROOT.glob("appendices/[A-Z]-*.md"))
        examples = sorted(ROOT.glob("examples/[0-9][0-9]_*.py"))
        readme = ROOT / "README.md"

        if not readme.exists():
            self.fail("README.md", "missing")
            self.report()
            return 1
        if len(chapters) != EXPECTED_CHAPTERS:
            self.fail("chapters/", f"expected {EXPECTED_CHAPTERS}, found {len(chapters)}")
        if len(appendices) != EXPECTED_APPENDICES:
            self.fail("appendices/", f"expected {EXPECTED_APPENDICES}, found {len(appendices)}")
        if len(examples) != EXPECTED_EXAMPLES:
            self.fail("examples/", f"expected {EXPECTED_EXAMPLES}, found {len(examples)}")

        self.check_numbering(chapters, appendices)
        docs = [readme, *chapters, *appendices]
        for document in docs:
            self.check_document(document)

        chapter_names = {path.name for path in chapters}
        appendix_names = {path.name for path in appendices}
        readme_text = readme.read_text(encoding="utf-8")
        for name in sorted(chapter_names | appendix_names):
            if name not in readme_text:
                self.fail("README.md", f"index does not link {name}")

        self.check_readme_counts(readme_text, chapters, appendices, examples)
        self.check_quoted_outputs(docs)
        if self.check_tests:
            self.check_test_count()
        self.report(len(chapters), len(appendices), len(examples))
        return 0 if not self.errors else 1

    def check_numbering(self, chapters: list[Path], appendices: list[Path]) -> None:
        numbers = []
        for path in chapters:
            match = re.match(r"([0-9]{2})-", path.name)
            if not match:
                self.fail(path, "filename must start with two digits and a dash")
                continue
            numbers.append(int(match.group(1)))
            expected_h1 = f"# 第 {match.group(1)} 章"
            text = path.read_text(encoding="utf-8")
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if not first.startswith(expected_h1):
                self.fail(path, f"H1 must start with {expected_h1!r}")
        if numbers != list(range(1, EXPECTED_CHAPTERS + 1)):
            self.fail("chapters/", f"numbering must be contiguous 01..{EXPECTED_CHAPTERS:02d}, got {numbers}")

        letters = []
        for path in appendices:
            match = re.match(r"([A-Z])-", path.name)
            if not match:
                self.fail(path, "appendix filename must start with A-Z and a dash")
                continue
            letters.append(match.group(1))
            expected_h1 = f"# 附录 {match.group(1)}"
            text = path.read_text(encoding="utf-8")
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if not first.startswith(expected_h1):
                self.fail(path, f"H1 must start with {expected_h1!r}")
        expected_letters = [chr(ord("A") + index) for index in range(EXPECTED_APPENDICES)]
        if letters != expected_letters:
            self.fail(
                "appendices/",
                f"letters must be contiguous A..{chr(ord('A') + EXPECTED_APPENDICES - 1)}, got {letters}",
            )

    def check_document(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            self.fail(path, f"not UTF-8: {exc}")
            return
        if FORBIDDEN.search(text):
            self.fail(path, "contains TODO/TBD/placeholder text")
        if not text.endswith("\n"):
            self.fail(path, "missing trailing newline")
        self.check_fences(path, text)
        if path.parent.name == "chapters":
            body = re.sub(r"\s+", "", text)
            if len(body) < MIN_CHAPTER_CHARS:
                self.fail(path, f"chapter too short: {len(body)} < {MIN_CHAPTER_CHARS}")
            if "## 本章小结" not in text:
                self.fail(path, "missing '## 本章小结'")
            if "返回目录" not in text:
                self.fail(path, "missing navigation back to README")
        elif path.parent.name == "appendices":
            body = re.sub(r"\s+", "", text)
            if len(body) < MIN_APPENDIX_CHARS:
                self.fail(path, f"appendix too short: {len(body)} < {MIN_APPENDIX_CHARS}")
        self.check_links(path, text)
        self.check_code_paths(path, text)

    def check_fences(self, path: Path, text: str) -> None:
        in_fence = False
        fence_lang = ""
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                if not in_fence:
                    in_fence = True
                    fence_lang = stripped[3:].strip().lower()
                    if fence_lang not in ALLOWED_FENCE_LANGS:
                        self.fail(path, f"line {line_number}: unknown fence language {fence_lang!r}")
                else:
                    in_fence = False
                    fence_lang = ""
        if in_fence:
            self.fail(path, f"unclosed code fence ({fence_lang or 'plain'})")

    def check_links(self, path: Path, text: str) -> None:
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            self.links_checked += 1
            candidate = (path.parent / unquote(target)).resolve()
            try:
                candidate.relative_to(ROOT)
            except ValueError:
                self.fail(path, f"link escapes tutorial directory: {target}")
                continue
            if not candidate.exists():
                self.fail(path, f"broken local link: {target}")

    def check_code_paths(self, path: Path, text: str) -> None:
        for match in PATH_RE.finditer(text):
            reference = match.group(1)
            if any(token in reference for token in ("*", "<", ">", "...", "…")):
                continue
            candidate = ROOT / reference
            if not candidate.exists():
                self.fail(path, f"documented path does not exist: {reference}")

    def check_readme_counts(
        self,
        text: str,
        chapters: list[Path],
        appendices: list[Path],
        examples: list[Path],
    ) -> None:
        pattern = re.compile(
            r"共\s*(\d+)\s*章[，,]\s*(\d+)\s*篇附录[，,]\s*(\d+)\s*个(?:可运行)?示例[，,]\s*(\d+)\s*项(?:自动化)?测试"
        )
        match = pattern.search(text)
        if not match:
            self.fail(
                "README.md",
                "missing count sentence: 共 N 章，N 篇附录，N 个可运行示例，N 项测试",
            )
            return
        declared = tuple(int(value) for value in match.groups())
        actual = (len(chapters), len(appendices), len(examples), EXPECTED_TESTS)
        if declared != actual:
            self.fail("README.md", f"declared counts {declared} != actual {actual}")

    def check_quoted_outputs(self, docs: list[Path]) -> None:
        """Catch stale numbers copied into prose, not just the README index."""
        verify_re = re.compile(
            r"VERIFY OK:\s*(\d+)\s*tests,\s*(\d+)\s*examples,\s*"
            r"(\d+)\s*chapters,\s*(\d+)\s*appendices"
        )
        examples_re = re.compile(r"EXAMPLES OK\s*(\d+)/(\d+)")
        docs_re = re.compile(
            r"文档校验通过：(\d+)\s*章\s*/\s*(\d+)\s*附录\s*/\s*(\d+)\s*个示例"
        )
        expected_verify = (
            EXPECTED_TESTS,
            EXPECTED_EXAMPLES,
            EXPECTED_CHAPTERS,
            EXPECTED_APPENDICES,
        )
        expected_docs = (EXPECTED_CHAPTERS, EXPECTED_APPENDICES, EXPECTED_EXAMPLES)
        for path in docs:
            text = path.read_text(encoding="utf-8")
            for match in verify_re.finditer(text):
                declared = tuple(int(value) for value in match.groups())
                if declared != expected_verify:
                    self.fail(path, f"stale VERIFY OK counts {declared} != {expected_verify}")
            for match in examples_re.finditer(text):
                declared = tuple(int(value) for value in match.groups())
                if declared != (EXPECTED_EXAMPLES, EXPECTED_EXAMPLES):
                    self.fail(path, f"stale EXAMPLES OK count {declared}")
            for match in docs_re.finditer(text):
                declared = tuple(int(value) for value in match.groups())
                if declared != expected_docs:
                    self.fail(path, f"stale doc-check counts {declared} != {expected_docs}")

    def check_test_count(self) -> None:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.fail("tests/", f"cannot collect tests: {exc}")
            return
        match = re.search(r"(\d+) tests collected", result.stdout)
        if result.returncode != 0 or match is None:
            self.fail("tests/", "pytest collection failed")
            return
        collected = int(match.group(1))
        if collected != EXPECTED_TESTS:
            self.fail("tests/", f"collected {collected} tests, expected {EXPECTED_TESTS}")

    def report(self, chapters: int = 0, appendices: int = 0, examples: int = 0) -> None:
        if self.errors:
            print(f"文档校验失败：{len(self.errors)} 个问题", file=sys.stderr)
            for error in self.errors:
                print(f"  - {error}", file=sys.stderr)
        else:
            print(
                f"文档校验通过：{chapters} 章 / {appendices} 附录 / "
                f"{examples} 个示例 / {self.links_checked} 个本地链接 / "
                f"{EXPECTED_TESTS} 项测试"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="skip pytest collection count validation",
    )
    args = parser.parse_args(argv)
    return Checker(check_tests=not args.skip_tests).run()


if __name__ == "__main__":
    raise SystemExit(main())
