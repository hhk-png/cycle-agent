#!/usr/bin/env python3
"""教程一致性检查。

检查项：
1. README 中的 41 章、16 个附录链接全部存在且编号连续（数量由常量校验）；
2. 每章标题与编号一致，包含“本章小结”；
3. 全部 Markdown 相对链接可解析；
4. 示例数量为 22，每个示例至少有一个 Python 入口；
5. 测试函数数量为 156；
6. 不出现 TODO/TBD 等占位符；
7. README 声明的数字与实际一致。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
CHAPTERS = ROOT / "chapters"
APPENDIX = ROOT / "appendix"
EXAMPLES = ROOT / "examples"
TESTS = ROOT / "tests"

EXPECTED_CHAPTERS = 41
EXPECTED_APPENDICES = 16
EXPECTED_EXAMPLES = 22
EXPECTED_TESTS = 156

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


def check_readme_links() -> tuple[set[Path], set[Path]]:
    text = README.read_text(encoding="utf-8")
    chapter_links = {
        ROOT / target
        for target in re.findall(r"\]\((chapters/[^)#]+\.md)\)", text)
    }
    appendix_links = {
        ROOT / target
        for target in re.findall(r"\]\((appendix/[^)#]+\.md)\)", text)
    }
    if len(chapter_links) != EXPECTED_CHAPTERS:
        fail(f"README 章节链接数 {len(chapter_links)}，期望 {EXPECTED_CHAPTERS}")
    if len(appendix_links) != EXPECTED_APPENDICES:
        fail(f"README 附录链接数 {len(appendix_links)}，期望 {EXPECTED_APPENDICES}")
    for path in sorted(chapter_links | appendix_links):
        if not path.exists():
            fail(f"README 链接目标不存在: {path.relative_to(ROOT)}")
    return chapter_links, appendix_links


def check_chapters() -> None:
    files = sorted(CHAPTERS.glob("*.md"))
    if len(files) != EXPECTED_CHAPTERS:
        fail(f"章节文件数 {len(files)}，期望 {EXPECTED_CHAPTERS}")
    for expected, path in enumerate(files, start=1):
        text = path.read_text(encoding="utf-8")
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first.startswith(f"# 第{expected}章"):
            fail(f"{path.name} 标题不是“# 第{expected}章...”: {first}")
        if "## 本章小结" not in text:
            fail(f"{path.name} 缺少“## 本章小结”")
        if len(text) < 800:
            fail(f"{path.name} 过短，只有 {len(text)} 字符")
        if not path.name.startswith(f"{expected:02d}-"):
            fail(f"{path.name} 文件名编号与章节号不一致")


def check_appendices() -> None:
    files = sorted(APPENDIX.glob("*.md"))
    if len(files) != EXPECTED_APPENDICES:
        fail(f"附录文件数 {len(files)}，期望 {EXPECTED_APPENDICES}")
    for expected, path in enumerate(files):
        text = path.read_text(encoding="utf-8")
        letter = chr(ord("A") + expected)
        if not path.name.startswith(letter):
            fail(f"{path.name} 附录编号不是 {letter}")
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first.startswith(f"# 附录 {letter}"):
            fail(f"{path.name} 标题不是“# 附录 {letter}...”: {first}")
        if len(text) < 400:
            fail(f"{path.name} 过短，只有 {len(text)} 字符")


def check_examples() -> None:
    files = sorted(
        path
        for path in EXAMPLES.glob("*.py")
        if re.fullmatch(r"\d{2}[_-].*\.py", path.name)
    )
    if len(files) != EXPECTED_EXAMPLES:
        fail(f"示例数 {len(files)}，期望 {EXPECTED_EXAMPLES}")
    for path in files:
        source = path.read_text(encoding="utf-8")
        markers = (
            "__main__",
            "env.execute",
            "create_execution",
            "TableEnvironment",
            "LocalCluster",
        )
        if not any(marker in source for marker in markers):
            fail(f"{path.name} 看起来没有可执行入口")


def check_tests() -> int:
    count = 0
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                count += 1
    if count != EXPECTED_TESTS:
        fail(f"测试函数数 {count}，期望 {EXPECTED_TESTS}")
    return count


def _github_slug(heading: str) -> str:
    """近似 GitHub 的标题锚点：小写、去标点、空格转连字符。"""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s一-鿿-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text.strip())


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    anchors: set[str] = set()
    for heading in re.findall(r"^#{1,6}\s+(.+)$", text, flags=re.MULTILINE):
        anchors.add(_github_slug(heading))
    return anchors


def check_relative_links() -> None:
    link_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for path in list(CHAPTERS.glob("*.md")) + list(APPENDIX.glob("*.md")) + [README]:
        text = path.read_text(encoding="utf-8")
        for target in link_pattern.findall(text):
            target = target.strip()
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            bare, _, anchor = target.partition("#")
            if not bare:
                continue
            resolved = (path.parent / bare).resolve()
            if not resolved.exists():
                fail(f"{path.relative_to(ROOT)} 中的相对链接不存在: {target}")
                continue
            if anchor and resolved.suffix == ".md":
                if anchor not in _anchors(resolved):
                    fail(
                        f"{path.relative_to(ROOT)} 中的锚点不存在: "
                        f"{target}（可用锚点示例: "
                        f"{', '.join(sorted(_anchors(resolved))[:3])}）"
                    )


def check_placeholders() -> None:
    forbidden = ["TODO", "TBD", "FIXME", "待补充", "占位符"]
    for path in list(CHAPTERS.glob("*.md")) + list(APPENDIX.glob("*.md")) + [README]:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            if marker in text:
                fail(f"{path.relative_to(ROOT)} 含占位符 {marker}")


def check_readme_numbers() -> None:
    text = README.read_text(encoding="utf-8")
    checks = {
        str(EXPECTED_CHAPTERS) + " 章": "章节数",
        str(EXPECTED_APPENDICES) + " 个附录": "附录数",
        str(EXPECTED_EXAMPLES) + " 个示例": "示例数",
        str(EXPECTED_TESTS) + " 个测试": "测试数",
    }
    for needle, label in checks.items():
        if needle not in text:
            fail(f"README 未声明{label}“{needle}”")


def main() -> int:
    if not README.exists():
        print("README.md 不存在")
        return 1
    check_readme_links()
    check_chapters()
    check_appendices()
    check_examples()
    test_count = check_tests()
    check_relative_links()
    check_placeholders()
    check_readme_numbers()
    if errors:
        print("文档检查失败：")
        for error in errors:
            print(" -", error)
        return 1
    print(
        "文档检查通过："
        f"{EXPECTED_CHAPTERS} 章 / {EXPECTED_APPENDICES} 个附录 / "
        f"{EXPECTED_EXAMPLES} 个示例 / {test_count} 个测试"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
