#!/usr/bin/env python3
"""Documentation and manifest consistency checks.

Run from the tutorial root:

    python check_docs.py

The script deliberately checks the things that tend to rot:

* every chapter linked from README exists and every local link resolves
* every ``minikuberay/...`` path mentioned in prose exists
* every complete YAML block in a chapter is parseable
* every manifest sample has a valid Ray CRD shape (after defaulting)
* the facts in ``docs/facts.json`` match the repository (test/chapter/sample
  counts), so changing code without updating the docs is visible
* every "第 N 章" reference names an existing chapter, every sample manifest
  is listed in ``manifests/samples/README.md``, and a chapter's "next" link
  points to the next chapter rather than skipping or repeating one
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
FACTS_PATH = ROOT / "docs" / "facts.json"
CHAPTER_RE = re.compile(r"^(\d{2})-[a-z0-9-]+\.md$")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
CODE_PATH_RE = re.compile(r"`((?:minikuberay|manifests|examples|tests|scripts)/[^`\n]+)`")
FENCE_RE = re.compile(r"```(yaml|yml)\n(.*?)```", re.S)
CHAPTER_REF_RE = re.compile(r"第\s*(\d{1,2})\s*章")
NEXT_LINK_RE = re.compile(r"→\s*\[第\s*(\d{1,2})\s*章")
SAMPLE_INDEX_RE = re.compile(r"`([a-z0-9-]+\.yaml)`")


def chapter_files() -> list[Path]:
    return sorted(path for path in ROOT.glob("*.md") if CHAPTER_RE.match(path.name))


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if ".research" not in path.parts and "node_modules" not in path.parts
    )


def check_links(errors: list[str]) -> int:
    count = 0
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            count += 1
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)}: broken link -> {target}")
    return count


def check_code_paths(errors: list[str]) -> int:
    count = 0
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for raw in CODE_PATH_RE.findall(text):
            target = raw.strip().split("::", 1)[0].split("#", 1)[0].rstrip(".,;:")
            if any(marker in target for marker in ("*", "...", "<", ">")):
                continue
            count += 1
            if not (ROOT / target).exists():
                errors.append(f"{path.relative_to(ROOT)}: missing referenced path -> {target}")
    return count


def check_chapter_sequence(errors: list[str]) -> list[Path]:
    chapters = chapter_files()
    numbers = []
    for chapter in chapters:
        match = CHAPTER_RE.match(chapter.name)
        numbers.append(int(match.group(1)))
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append(f"chapter numbering is not contiguous: {numbers}")
    return chapters


def check_readme(errors: list[str], chapters: list[Path]) -> None:
    readme_path = ROOT / "README.md"
    if not readme_path.exists():
        errors.append("README.md is missing")
        return
    readme = readme_path.read_text(encoding="utf-8")
    for chapter in chapters:
        if chapter.name not in readme:
            errors.append(f"README.md does not link chapter {chapter.name}")
    for required in ("python -m pytest", "python check_docs.py", "python -m minikuberay demo --fast"):
        if required not in readme:
            errors.append(f"README.md is missing verification command: {required}")


def check_chapter_references(errors: list[str], chapters: list[Path]) -> int:
    """Every "第 N 章" must point at an existing chapter, and every chapter's
    final "next" arrow must be N+1 rather than a skipped/repeated number."""
    count = 0
    numbers = {int(CHAPTER_RE.match(path.name).group(1)) for path in chapters}
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for raw in CHAPTER_REF_RE.findall(text):
            count += 1
            if int(raw) not in numbers:
                errors.append(f"{path.relative_to(ROOT)}: chapter reference -> 第 {raw} 章 does not exist")
        own = CHAPTER_RE.match(path.name)
        if own:
            expected = int(own.group(1)) + 1
            for raw in NEXT_LINK_RE.findall(text):
                if int(raw) != expected:
                    errors.append(
                        f"{path.relative_to(ROOT)}: next-chapter link points to 第 {raw} 章, expected 第 {expected} 章"
                    )
    return count


def check_sample_index(errors: list[str]) -> int:
    """The samples README is the index readers use; keep it in sync with disk."""
    index = ROOT / "manifests" / "samples" / "README.md"
    sample_dir = ROOT / "manifests" / "samples"
    files = {path.name for path in sample_dir.glob("*.yaml")}
    if not index.exists():
        errors.append("manifests/samples/README.md is missing")
        return len(files)
    text = index.read_text(encoding="utf-8")
    listed = set(SAMPLE_INDEX_RE.findall(text))
    for name in sorted(files - listed):
        errors.append(f"manifests/samples/README.md: sample {name} is not listed")
    for name in sorted(listed - files):
        errors.append(f"manifests/samples/README.md: lists missing sample {name}")
    return len(files)


def check_yaml_blocks(errors: list[str]) -> int:
    count = 0
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for kind, block in FENCE_RE.findall(text):
            if "apiVersion:" not in block or "kind:" not in block:
                continue
            count += 1
            try:
                documents = [doc for doc in yaml.safe_load_all(block) if doc]
            except yaml.YAMLError as exc:
                errors.append(f"{path.relative_to(ROOT)}: invalid YAML block: {exc}")
                continue
            for document in documents:
                validate_document(document, f"{path.relative_to(ROOT)} YAML block", errors)
    return count


def check_manifests(errors: list[str]) -> int:
    count = 0
    for path in sorted((ROOT / "manifests").rglob("*.yaml")):
        count += 1
        try:
            documents = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
        except yaml.YAMLError as exc:
            errors.append(f"{path.relative_to(ROOT)}: invalid YAML: {exc}")
            continue
        for document in documents:
            validate_document(document, str(path.relative_to(ROOT)), errors)
    return count


def validate_document(document, location: str, errors: list[str]) -> None:
    if not isinstance(document, dict):
        return
    kind = document.get("kind", "")
    if kind == "CustomResourceDefinition":
        if document.get("apiVersion") != "apiextensions.k8s.io/v1":
            errors.append(f"{location}: CRD must use apiextensions.k8s.io/v1")
        spec = document.get("spec") or {}
        if not spec.get("versions"):
            errors.append(f"{location}: CRD has no versions")
        else:
            version = spec["versions"][0]
            if not version.get("schema", {}).get("openAPIV3Schema"):
                errors.append(f"{location}: CRD version has no openAPIV3Schema")
            crd_kind = (spec.get("names") or {}).get("kind", "")
            if "status" not in (version.get("subresources") or {}) and crd_kind != "RayCronJob":
                errors.append(f"{location}: CRD version has no status subresource")
        return
    if kind in ("RayCluster", "RayJob", "RayService", "RayCronJob"):
        from minikuberay.models.defaults import apply_defaults
        from minikuberay.models.validation import validate_object

        try:
            defaulted = apply_defaults(document)
            problems = validate_object(defaulted)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{location}: defaulting failed: {type(exc).__name__}: {exc}")
            return
        if problems:
            errors.append(f"{location}: invalid {kind}: " + "; ".join(problems))


def count_tests() -> int:
    total = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
    return total


def count_modules() -> int:
    return len([path for path in (ROOT / "minikuberay").rglob("*.py") if path.name != "__pycache__"])


def check_facts(errors: list[str], *, chapters: list[Path], samples: int, tests: int) -> None:
    if not FACTS_PATH.exists():
        errors.append("docs/facts.json is missing")
        return
    try:
        facts = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"docs/facts.json is invalid JSON: {exc}")
        return
    actual = {
        "tests": tests,
        "chapters": len(chapters),
        "sample_manifests": len(list((ROOT / "manifests" / "samples").glob("*.yaml"))),
        "python_modules": count_modules(),
    }
    for key, value in actual.items():
        if facts.get(key) != value:
            errors.append(f"docs/facts.json: {key} is {facts.get(key)!r}, expected {value}")
    readme = (ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").exists() else ""
    for key, value in actual.items():
        marker = f"{key}={value}"
        if marker not in readme:
            errors.append(f"README.md is missing facts marker {marker}")
    if facts.get("upstream_kuberay") != "v1.7.1":
        errors.append("docs/facts.json: upstream_kuberay must be v1.7.1")


def main() -> int:
    errors: list[str] = []
    chapters = check_chapter_sequence(errors)
    check_readme(errors, chapters)
    link_count = check_links(errors)
    code_path_count = check_code_paths(errors)
    chapter_refs = check_chapter_references(errors, chapters)
    sample_index = check_sample_index(errors)
    yaml_blocks = check_yaml_blocks(errors)
    manifests = check_manifests(errors)
    tests = count_tests()
    check_facts(errors, chapters=chapters, samples=manifests, tests=tests)

    print(f"chapters      : {len(chapters)}")
    print(f"links checked : {link_count}")
    print(f"paths checked : {code_path_count}")
    print(f"chapter refs  : {chapter_refs}")
    print(f"sample index  : {sample_index}")
    print(f"yaml blocks   : {yaml_blocks}")
    print(f"manifests     : {manifests}")
    print(f"tests         : {tests}")
    print(f"modules       : {count_modules()}")
    if errors:
        print("\nFAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("\nOK: documentation, manifests and facts are consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
