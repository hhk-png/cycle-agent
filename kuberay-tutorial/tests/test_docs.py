from __future__ import annotations

import json
from pathlib import Path

import yaml

import check_docs
from minikuberay.models.defaults import apply_defaults
from minikuberay.models.validation import validate_object

ROOT = Path(__file__).resolve().parent.parent


def test_documentation_consistency():
    assert check_docs.main() == 0


def test_all_sample_manifests_validate():
    for path in sorted((ROOT / "manifests" / "samples").glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not document:
                continue
            if document.get("kind", "").startswith("Ray"):
                assert validate_object(apply_defaults(document)) == [], path


def test_facts_match_repository():
    facts = json.loads((ROOT / "docs" / "facts.json").read_text(encoding="utf-8"))
    chapters = [path for path in ROOT.glob("[0-9][0-9]-*.md")]
    assert facts["chapters"] == len(chapters)
    assert facts["sample_manifests"] == len(list((ROOT / "manifests" / "samples").glob("*.yaml")))
    assert facts["tests"] == check_docs.count_tests()
    assert facts["python_modules"] == check_docs.count_modules()
