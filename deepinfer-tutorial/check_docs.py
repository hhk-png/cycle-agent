#!/usr/bin/env python
"""Validate the tutorial's structure and its self-reported numbers.

The checker intentionally covers the failure modes that a plain markdown
linter misses:

* a chapter that exists but is an empty stub;
* README or chapter links that point nowhere;
* code paths mentioned in prose that do not exist;
* stale test/example counts;
* block quotes (the tutorial style asks for prose, not quote blocks);
* missing summary/exercise sections;
* a chapter number that disagrees with its filename, non-contiguous
  section numbers, or a missing chapter intro;
* a README that does not link every chapter and appendix.

Run with ``python check_docs.py``.  Exit code 0 means the document is
internally consistent; it does not replace reading it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAPTERS = [
    "deepinfer教程-00-前言-如何读这本书.md",
    "deepinfer教程-01-为什么需要推理框架.md",
    "deepinfer教程-02-DeepInfer全景.md",
    "deepinfer教程-03-快速上手与源码地图.md",
    "deepinfer教程-04-推理系统的性能模型.md",
    "deepinfer教程-05-DeepSeek-V4.1-Flash架构总览.md",
    "deepinfer教程-06-注意力、KV压缩与有界重放.md",
    "deepinfer教程-07-MoE与专家并行.md",
    "deepinfer教程-08-推测解码DSpark.md",
    "deepinfer教程-09-结构化输出.md",
    "deepinfer教程-10-低精度、确定性与batch-invariance.md",
    "deepinfer教程-11-采样、解码与输出契约.md",
    "deepinfer教程-12-引擎总体架构.md",
    "deepinfer教程-13-调度器.md",
    "deepinfer教程-14-KV-cache系统.md",
    "deepinfer教程-15-并行、通信与PD分离.md",
    "deepinfer教程-16-长上下文与Agent负载工程.md",
    "deepinfer教程-17-异步调度、CUDA-Graph与性能工程.md",
    "deepinfer教程-18-GPU执行模型与kernel工程.md",
    "deepinfer教程-19-可靠性、运维与容量规划.md",
    "deepinfer教程-20-mini-deepinfer的设计与边界.md",
    "deepinfer教程-21-玩具模型、Tokenizer与反向传播.md",
    "deepinfer教程-22-分页KV与调度器实现.md",
    "deepinfer教程-23-推测解码与结构化输出实现.md",
    "deepinfer教程-24-EPLB、并行模拟与KV分层.md",
    "deepinfer教程-25-HTTP服务与可观测性.md",
    "deepinfer教程-26-测试、验证与性能实验.md",
    "deepinfer教程-27-从玩具到生产.md",
    "deepinfer教程-28-安全、隐私与多租户.md",
    "deepinfer教程-29-评测体系与质量保证.md",
    "deepinfer教程-30-推理经济学.md",
    "deepinfer教程-31-竞争格局与开源战略.md",
    "deepinfer教程-32-部署选型与TCO.md",
    "deepinfer教程-33-工程化-从可运行到可维护.md",
    "deepinfer教程-34-模型接入、权重管理与版本治理.md",
    "deepinfer教程-35-云原生部署与Kubernetes.md",
    "deepinfer教程-36-推理产品化与商业路径.md",
    "deepinfer教程-37-合规、许可与供应链治理.md",
    "deepinfer教程-38-多模态推理工程-从像素到token.md",
    "deepinfer教程-39-推理模型与长思维链服务.md",
    "deepinfer教程-40-离线批处理-吞吐型推理服务.md",
    "deepinfer教程-41-多模型、多适配器与模型路由.md",
    "deepinfer教程-42-Embedding、Rerank与检索推理.md",
    "deepinfer教程-43-推理编译器与图优化.md",
    "deepinfer教程-44-强化学习推理与rollout引擎.md",
    "deepinfer教程-45-端侧、边缘与异构推理.md",
    "deepinfer教程-46-流量工程、多区域与容量运营.md",
    "deepinfer教程-47-商业分析进阶-现金流、定价与投资回报.md",
    "deepinfer教程-48-商业分析-市场测算与进入策略.md",
    "deepinfer教程-49-商业分析-估值、融资与资本回报.md",
    "deepinfer教程-50-未来方向.md",
    "deepinfer教程-51-结语与路线图.md",
]
APPENDICES = [
    "deepinfer教程-52-附录A-术语表.md",
    "deepinfer教程-53-附录B-公式与常数.md",
    "deepinfer教程-54-附录C-API与配置参考.md",
    "deepinfer教程-55-附录D-参考资料.md",
    "deepinfer教程-56-附录E-实验手册与命令速查.md",
    "deepinfer教程-57-附录F-Transformer推理基础.md",
    "deepinfer教程-58-附录G-一个请求的完整生命周期.md",
    "deepinfer教程-59-附录H-练习提示与参考答案.md",
    "deepinfer教程-60-附录I-mini-deepinfer模块、API与测试矩阵.md",
    "deepinfer教程-61-附录J-生产故障排查手册.md",
]
EXPECTED = CHAPTERS + APPENDICES
META_RE = re.compile(r"<!--\s*docs-meta:\s*(.*?)\s*-->")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
PATH_RE = re.compile(
    r"`((?:mini_deepinfer|examples|tests|deploy|ci)/[A-Za-z0-9_./-]+|train_tiny\.py|check_docs\.py)`"
)
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
CHAPTER_REF_RE = re.compile(r"第\s*(\d+)\s*章")
SECTION_REF_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\s*节")
APPENDIX_REF_RE = re.compile(r"附录\s*([A-Z])(?![A-Za-z])")


def strip_fenced_code(text: str) -> str:
    lines = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return "\n".join(lines)


def chinese_chars(text: str) -> int:
    return len(re.findall(r"[一-鿿]", text))


def collect_test_count() -> int | None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+)\s+tests? collected", result.stdout + result.stderr)
    return int(match.group(1)) if match else None


def h2_titles(text: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"^## (.+)$", text, flags=re.M)]


def numbered_sections(text: str) -> list[tuple[int, int]]:
    return [
        (int(major), int(minor))
        for major, minor in re.findall(r"^## (\d+)\.(\d+)", text, flags=re.M)
    ]


def parse_meta(readme: str) -> dict[str, int]:
    match = META_RE.search(readme)
    if not match:
        return {}
    meta = {}
    for part in match.group(1).split():
        if "=" in part:
            key, value = part.split("=", 1)
            if value.isdigit():
                meta[key.strip()] = int(value)
    return meta


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    seen_titles: dict[str, str] = {}
    for filename in EXPECTED:
        path = ROOT / filename
        if not path.exists():
            errors.append(f"missing chapter: {filename}")
            continue
        text = path.read_text(encoding="utf-8")
        if not text.startswith("# "):
            errors.append(f"{filename}: missing level-1 title")
        first_line = text.splitlines()[0]
        if first_line in seen_titles:
            errors.append(
                f"{filename}: duplicate level-1 title also used by {seen_titles[first_line]}"
            )
        seen_titles[first_line] = filename
        if chinese_chars(text) < (500 if filename in CHAPTERS else 200):
            errors.append(f"{filename}: content looks like a stub ({chinese_chars(text)} Chinese chars)")
        body = strip_fenced_code(text)

        # The filename, the H1 number and the section numbering must agree.
        chapter_match = (
            re.match(r"deepinfer教程-(\d+)-", filename) if filename in CHAPTERS else None
        )
        if chapter_match:
            number = int(chapter_match.group(1))
            title_match = re.match(r"# 第 (\d+) 章", first_line)
            if not title_match:
                errors.append(f"{filename}: H1 must start with '# 第 N 章'")
            elif int(title_match.group(1)) != number:
                errors.append(
                    f"{filename}: H1 says chapter {title_match.group(1)} but filename says {number}"
                )
            if "本章导读" not in "\n".join(text.splitlines()[:6]):
                errors.append(f"{filename}: missing '本章导读' before the first section")
            sections = numbered_sections(body)
            expected_sections = [(number, minor) for minor in range(1, len(sections) + 1)]
            if sections != expected_sections:
                errors.append(
                    f"{filename}: section numbers are not contiguous: {sections[:8]}..."
                )
        appendix_match = (
            re.match(r"deepinfer教程-\d+-附录([A-Z])-", filename)
            if filename in APPENDICES
            else None
        )
        if appendix_match:
            letter = appendix_match.group(1)
            title_match = re.match(r"# 附录 ([A-Z])", first_line)
            if not title_match:
                errors.append(f"{filename}: H1 must start with '# 附录 X'")
            elif title_match.group(1) != letter:
                errors.append(
                    f"{filename}: H1 says appendix {title_match.group(1)} but filename says {letter}"
                )

        titles = h2_titles(body)
        if filename in CHAPTERS:
            if "## 小结" not in body:
                errors.append(f"{filename}: missing '## 小结'")
            if "## 练习" not in body:
                errors.append(f"{filename}: missing '## 练习'")
            if titles[-2:] != ["小结", "练习"]:
                errors.append(
                    f"{filename}: '## 小结' and '## 练习' must be the last two sections"
                )
        for line_no, line in enumerate(body.splitlines(), start=1):
            if line.lstrip().startswith(">"):
                errors.append(f"{filename}:{line_no}: block quote is disallowed by style")
        if "TODO" in body or "FIXME" in body:
            errors.append(f"{filename}: contains TODO/FIXME")

    readme_path = ROOT / "README.md"
    if not readme_path.exists():
        errors.append("missing README.md")
        readme = ""
    else:
        readme = readme_path.read_text(encoding="utf-8")
    linked_targets = {
        target.split("#", 1)[0]
        for target in LINK_RE.findall(readme)
        if target and not target.startswith(("http://", "https://", "mailto:"))
    }
    for target in linked_targets:
        if target.endswith(".md") and not (ROOT / target).exists():
            errors.append(f"README link points to missing file: {target}")
    for filename in EXPECTED:
        if filename not in linked_targets:
            errors.append(f"README does not link to {filename}")

    # Relative links in every chapter must resolve too; the README check alone
    # lets a chapter link to a file that was renamed or never existed.
    for filename in EXPECTED:
        path = ROOT / filename
        if not path.exists():
            continue
        for target in LINK_RE.findall(strip_fenced_code(path.read_text(encoding="utf-8"))):
            target = target.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.endswith(".md") and not (ROOT / target).exists():
                errors.append(f"{filename}: link points to missing file: {target}")

    # Code paths mentioned in prose must resolve.
    for filename in EXPECTED + ["README.md"]:
        path = ROOT / filename
        if not path.exists():
            continue
        text = strip_fenced_code(path.read_text(encoding="utf-8"))
        for match in PATH_RE.finditer(text):
            candidate = match.group(1).rstrip(".,:;")
            if "..." in candidate or "*" in candidate:
                continue
            if not (ROOT / candidate).exists():
                errors.append(f"{filename}: referenced path does not exist: {candidate}")

    # Cross-references must point at a chapter/section/appendix that exists.
    chapter_numbers: set[int] = set()
    appendix_letters: set[str] = set()
    sections: set[tuple[int, int]] = set()
    for filename in CHAPTERS:
        match = re.match(r"deepinfer教程-(\d+)-", filename)
        if match:
            chapter_numbers.add(int(match.group(1)))
    for filename in APPENDICES:
        match = re.match(r"deepinfer教程-\d+-附录([A-Z])-", filename)
        if match:
            appendix_letters.add(match.group(1))
    for filename in CHAPTERS:
        path = ROOT / filename
        if not path.exists():
            continue
        body = strip_fenced_code(path.read_text(encoding="utf-8"))
        for major, minor in re.findall(r"^## (\d+)\.(\d+)", body, flags=re.M):
            sections.add((int(major), int(minor)))
    for filename in EXPECTED:
        path = ROOT / filename
        if not path.exists():
            continue
        body = strip_fenced_code(path.read_text(encoding="utf-8"))
        for match in CHAPTER_REF_RE.finditer(body):
            number = int(match.group(1))
            if number not in chapter_numbers:
                errors.append(f"{filename}: references missing chapter {number}")
        for match in SECTION_REF_RE.finditer(body):
            key = (int(match.group(1)), int(match.group(2)))
            if key not in sections:
                errors.append(f"{filename}: references missing section {key[0]}.{key[1]}")
        for match in APPENDIX_REF_RE.finditer(body):
            letter = match.group(1)
            if letter not in appendix_letters:
                errors.append(f"{filename}: references missing appendix {letter}")

    examples = sorted(
        path.name
        for path in (ROOT / "examples").glob("*.py")
        if not path.name.startswith("_")
    )
    meta = parse_meta(readme)
    if not meta:
        errors.append("README is missing <!-- docs-meta: ... -->")
    else:
        actual_tests = collect_test_count()
        if actual_tests is None:
            errors.append("could not collect the pytest test count")
        elif meta.get("tests") != actual_tests:
            errors.append(f"README tests={meta.get('tests')} but pytest collected {actual_tests}")
        if meta.get("examples") != len(examples):
            errors.append(f"README examples={meta.get('examples')} but found {len(examples)}")
        if meta.get("chapters") != len(CHAPTERS):
            errors.append(f"README chapters={meta.get('chapters')} but expected {len(CHAPTERS)}")
        if meta.get("appendices") != len(APPENDICES):
            errors.append(f"README appendices={meta.get('appendices')} but expected {len(APPENDICES)}")

    print(f"chapters={len(CHAPTERS)} appendices={len(APPENDICES)} examples={len(examples)}")
    print(f"pytest collected={collect_test_count()}")
    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print(f"FAILED with {len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("OK: document structure, links, code paths and metadata are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
