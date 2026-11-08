#!/usr/bin/env python3
"""Lightweight documentation checks.

- every local Markdown link points to an existing file
- fenced code blocks are balanced
- the expected chapter files exist
- every YAML file under examples/ parses
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guidance
    print("PyYAML is required: python -m pip install pyyaml", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^```", re.MULTILINE)
CHAPTER_RE = re.compile(r"^# 第 (\d+) 章")


def check_markdown(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    if len(FENCE_RE.findall(text)) % 2 != 0:
        errors.append(f"{path}: unbalanced code fences")
    for target in LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            errors.append(f"{path}: broken link -> {target}")
    return errors


def check_chapter_heading(path: Path) -> list[str]:
    """The file prefix and the '# 第 N 章' heading must agree."""
    match = re.match(r"^(\d+)-", path.name)
    if not match:
        return [f"{path}: file name does not start with a chapter number"]
    first = path.read_text(encoding="utf-8").splitlines()[0]
    heading = CHAPTER_RE.match(first)
    if not heading:
        return [f"{path}: first line is not '# 第 N 章 ...'"]
    if int(heading.group(1)) != int(match.group(1)):
        return [
            f"{path}: heading says chapter {heading.group(1)}, file prefix is {match.group(1)}"
        ]
    return []


def check_stale_terms(path: Path) -> list[str]:
    """Catch field names that were renamed in v1beta2 but linger in prose."""
    errors: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        # Legacy spelling is allowed only in an explicit historical note.
        if "admissionCheckStates" in line and "历史" not in line and "旧" not in line:
            errors.append(
                f"{path}:{lineno}: v1beta2 uses status.admissionChecks, not admissionCheckStates"
            )
        if ".totalQuota" in line:
            errors.append(
                f"{path}:{lineno}: ResourceUsage uses .total, not .totalQuota"
            )
        if (
            path.name.startswith("38-")
            and re.search(r"\bminMember\b", line)
            and "Volcano" not in line
            and "YuniKorn" not in line
        ):
            errors.append(
                f"{path}:{lineno}: native PodGroup uses minCount, not minMember"
            )
    return errors


def count_go_tests() -> int:
    total = 0
    for path in (ROOT / "mini-kueue").rglob("*_test.go"):
        total += len(re.findall(r"^func Test", path.read_text(encoding="utf-8"), re.M))
    return total


def check_readme_claims(expected: list[str], yaml_count: int) -> list[str]:
    """README is the front page; its counts must match reality."""
    errors: list[str] = []
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    for pattern, actual, label in (
        (r"(\d+)\s*章文档", len(expected), "章文档"),
        (r"(\d+)\s*章教程", len(expected), "章教程"),
        (r"(\d+)\s*个 YAML", yaml_count, "个 YAML"),
        (r"(\d+)\s*个顶层测试", count_go_tests(), "个顶层测试"),
    ):
        for match in re.finditer(pattern, text):
            if int(match.group(1)) != actual:
                errors.append(
                    f"README: says {match.group(1)} {label}, actual {actual}"
                )

    linked = set(re.findall(r"\(docs/([^)#]+\.md)\)", text))
    missing = sorted(set(expected) - linked)
    if missing:
        errors.append(f"README: chapter table misses {', '.join(missing)}")
    return errors


def main() -> int:
    errors: list[str] = []
    expected = [
        "01-why-kueue.md",
        "02-core-objects.md",
        "03-install-and-quickstart.md",
        "04-admission-lifecycle.md",
        "05-quota-cohorts.md",
        "06-scheduling-and-fairness.md",
        "07-preemption-reclaim.md",
        "08-workload-integrations.md",
        "09-advanced-features.md",
        "10-operations.md",
        "11-security-multitenancy.md",
        "12-performance-scale.md",
        "13-mini-kueue-design.md",
        "14-mini-kueue-walkthrough.md",
        "15-gap-to-real-kueue.md",
        "16-future-roadmap.md",
        "17-kueue-internals.md",
        "18-production-adoption.md",
        "19-mini-kueue-extension-lab.md",
        "20-scheduling-edge-cases.md",
        "21-platform-design-patterns.md",
        "22-hands-on-labs.md",
        "23-appendix.md",
        "24-api-migration-upgrade.md",
        "25-integration-cookbook.md",
        "26-end-to-end-case-study.md",
        "27-verification-engineering.md",
        "28-physical-scheduling-layer.md",
        "29-queueing-capacity-engineering.md",
        "30-mini-to-production-controller.md",
        "31-kueuectl-operations.md",
        "32-high-availability-dr.md",
        "33-implementation-completion.md",
        "34-multikueue-deep-dive.md",
        "35-kueue-extension-development.md",
        "36-inference-elastic-workloads.md",
        "37-decision-explainability.md",
        "38-native-was-gang-scheduling.md",
    ]
    for name in expected:
        if not (DOCS / name).exists():
            errors.append(f"missing chapter: docs/{name}")

    chapter_files = sorted(DOCS.glob("*.md"))
    for path in [ROOT / "README.md", *chapter_files]:
        errors.extend(check_markdown(path))
    for path in chapter_files:
        errors.extend(check_chapter_heading(path))
        errors.extend(check_stale_terms(path))

    yaml_files = sorted((ROOT / "examples").rglob("*.yaml"))
    errors.extend(check_readme_claims(expected, len(yaml_files)))
    if not yaml_files:
        errors.append("no YAML examples found")
    for path in yaml_files:
        try:
            list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - report parser details
            errors.append(f"{path}: invalid YAML: {exc}")

    if errors:
        print("FAIL")
        for err in errors:
            print(" -", err)
        return 1
    print(f"OK: {len(expected)} chapters, {len(yaml_files)} YAML files, links/fences valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
