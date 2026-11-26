#!/usr/bin/env python3
"""Validate the Volcano tutorial's documentation and examples.

Checks are intentionally structural: chapter numbering, links, fence balance,
README/VERIFICATION counts, YAML/JSON parseability and the key fields of each
example. It does not require a Kubernetes cluster.

Usage:
    python scripts/check_docs.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
EXAMPLES = ROOT / "examples"
MINI = ROOT / "mini-volcano"

EXPECTED_CHAPTERS = 35
EXPECTED_EXAMPLES = 37
EXPECTED_YAML = 36
EXPECTED_SCENARIOS = 28
EXPECTED_PLUGINS = 23
EXPECTED_ACTIONS = 6
EXPECTED_TESTS = 141
EXPECTED_APPENDIX_SECTIONS = ("A", "T", "U", "V", "W")
EXPECTED_APPENDIX_COUNT = 23

errors: list[str] = []
checks: list[str] = []


def ok(msg: str) -> None:
    checks.append(msg)


def fail(msg: str) -> None:
    errors.append(msg)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_code_fences(text: str) -> str:
    out, in_code = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            out.append(line)
    return "\n".join(out)


def check_chapters() -> None:
    files = sorted(p for p in DOCS.glob("*.md"))
    if len(files) != EXPECTED_CHAPTERS:
        fail(f"docs: expected {EXPECTED_CHAPTERS} chapters, found {len(files)}")
    else:
        ok(f"docs: {len(files)} chapters")

    expected_names = [f"{i:02d}-" for i in range(1, EXPECTED_CHAPTERS + 1)]
    for prefix in expected_names:
        if not any(p.name.startswith(prefix) for p in files):
            fail(f"docs: missing chapter file with prefix {prefix}")

    for path in files:
        text = read(path)
        m = re.match(r"^(\d{2})-", path.name)
        if not m:
            fail(f"{path.name}: filename must start with NN-")
            continue
        num = int(m.group(1))
        first = text.splitlines()[0] if text.splitlines() else ""
        if num == 35:
            if not first.startswith("# 附录"):
                fail(f"{path.name}: first line must be '# 附录', got {first!r}")
            sections = re.findall(r"^## ([A-Z])\.", text, re.M)
            if len(sections) != EXPECTED_APPENDIX_COUNT:
                fail(f"{path.name}: expected {EXPECTED_APPENDIX_COUNT} appendix sections, found {len(sections)}")
            if len(set(sections)) != len(sections):
                fail(f"{path.name}: duplicate appendix section letters")
            for letter in EXPECTED_APPENDIX_SECTIONS:
                if letter not in sections:
                    fail(f"{path.name}: missing appendix section {letter}.")
        else:
            want = f"# 第 {num} 章"
            if not first.startswith(want):
                fail(f"{path.name}: first line must start with {want!r}, got {first!r}")
        if len(text) < 1000:
            fail(f"{path.name}: suspiciously short ({len(text)} chars)")

        if text.count("```") % 2 != 0:
            fail(f"{path.name}: unbalanced code fences ({text.count('```')})")

        body = strip_code_fences(text)
        h1 = [ln for ln in body.splitlines() if re.match(r"^#\s+", ln)]
        if len(h1) != 1:
            fail(f"{path.name}: expected exactly one H1, found {len(h1)}")

        for bad in ("TODO", "FIXME", "XXX", "Lorem ipsum"):
            if bad.lower() in body.lower():
                fail(f"{path.name}: contains placeholder {bad!r}")

        for line in body.splitlines():
            hm = re.match(r"^#{2,4} (\d+)\.", line)
            if hm and int(hm.group(1)) != num:
                fail(f"{path.name}: section {line.strip()!r} does not match chapter {num}")
                break

        for ref in re.findall(r"第\s*(\d+)\s*章", body):
            if not (1 <= int(ref) <= EXPECTED_CHAPTERS):
                fail(f"{path.name}: chapter reference 第 {ref} 章 is out of range")

        # Internal markdown links (ignore code fences).
        for target in re.findall(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", body):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                fail(f"{path.name}: broken link {target!r}")

    ok("docs: titles, fences, placeholders, section numbering and links checked")


def check_readme() -> None:
    readme = read(ROOT / "README.md")
    for path in sorted(DOCS.glob("*.md")):
        rel = f"docs/{path.name}"
        if rel not in readme:
            fail(f"README.md: missing link/reference to {rel}")
    for token in ("35 章", "28 个场景", "141", "37 个示例", "v1.16.0", "2026-10-28",
                  "Incubating", "capacity_model.py"):
        if token not in readme:
            fail(f"README.md: missing expected token {token!r}")
    ok("README.md: chapter links and headline counts present")


def check_counts() -> None:
    yamls = sorted(p for p in EXAMPLES.rglob("*.yaml")) + sorted(p for p in EXAMPLES.rglob("*.yml"))
    jsons = sorted(p for p in EXAMPLES.rglob("*.json"))
    total = len(yamls) + len(jsons)
    if total != EXPECTED_EXAMPLES:
        fail(f"examples: expected {EXPECTED_EXAMPLES} files, found {total}")
    if len(yamls) != EXPECTED_YAML:
        fail(f"examples: expected {EXPECTED_YAML} YAML files, found {len(yamls)}")
    if len(jsons) != 1:
        fail(f"examples: expected exactly 1 JSON config, found {len(jsons)}")
    ok(f"examples: {len(yamls)} YAML + {len(jsons)} JSON = {total}")

    # Scenarios: count constructor functions returning *Scenario.
    scenario_src = "\n".join(read(p) for p in sorted((MINI / "internal" / "scenario").glob("*.go"))
                             if not p.name.endswith("_test.go"))
    scenarios = len(re.findall(r"func \w+Scenario\(\) \*Scenario", scenario_src))
    if scenarios != EXPECTED_SCENARIOS:
        fail(f"mini: expected {EXPECTED_SCENARIOS} scenario constructors, found {scenarios}")
    else:
        ok(f"mini: {scenarios} scenario constructors")

    registry = read(MINI / "internal" / "plugins" / "registry.go")
    plugins = len(re.findall(r'^\s*"([a-z0-9-]+)":\s+new', registry, re.M))
    if plugins != EXPECTED_PLUGINS:
        fail(f"mini: expected {EXPECTED_PLUGINS} plugins, found {plugins}")
    else:
        ok(f"mini: {plugins} plugins")

    actions = read(MINI / "internal" / "actions" / "registry.go")
    acts = len(re.findall(r'^\s*"([a-z]+)":\s+func\(\) Action', actions, re.M))
    if acts != EXPECTED_ACTIONS:
        fail(f"mini: expected {EXPECTED_ACTIONS} actions, found {acts}")
    else:
        ok(f"mini: {acts} actions")

    verification = read(ROOT / "VERIFICATION.md")
    if "141" not in verification:
        fail("VERIFICATION.md: missing 141 PASS count")
    if "28 scenarios, 28 passed" not in verification:
        fail("VERIFICATION.md: missing 28/28 scenario result")


def require(doc: dict, key: str, where: str) -> None:
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            fail(f"{where}: missing field {key}")
            return
        cur = cur[part]


def check_job_spec(spec: dict, where: str) -> None:
    if spec.get("schedulerName") != "volcano":
        fail(f"{where}: schedulerName must be volcano")
    if not spec.get("queue"):
        fail(f"{where}: queue is required")
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        fail(f"{where}: at least one task is required")
        return
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            fail(f"{where}.tasks[{i}]: must be a mapping")
            continue
        for key in ("name", "replicas", "template"):
            if key not in task:
                fail(f"{where}.tasks[{i}]: missing {key}")
        if isinstance(task.get("replicas"), int) and task["replicas"] < 1:
            fail(f"{where}.tasks[{i}]: replicas must be >= 1")


def check_manifest(doc: dict, where: str) -> None:
    if not isinstance(doc, dict):
        fail(f"{where}: document is not a mapping")
        return
    api = doc.get("apiVersion")
    kind = doc.get("kind")
    if not api or not kind:
        fail(f"{where}: apiVersion/kind required")
        return
    spec = doc.get("spec") or {}
    if kind == "Job":
        check_job_spec(spec, where)
    elif kind == "CronJob":
        require(doc, "spec.schedule", where)
        require(doc, "spec.jobTemplate.spec.tasks", where)
        check_job_spec(doc["spec"]["jobTemplate"]["spec"], where + ".jobTemplate.spec")
    elif kind == "JobTemplate":
        check_job_spec(spec, where)
    elif kind == "JobFlow":
        flows = spec.get("flows")
        if not isinstance(flows, list) or not flows:
            fail(f"{where}: spec.flows required")
    elif kind == "PodGroup":
        require(doc, "spec.minMember", where)
        require(doc, "spec.queue", where)
    elif kind == "Queue":
        if "weight" not in spec and "capability" not in spec:
            fail(f"{where}: queue needs weight or capability")
    elif kind == "PriorityClass":
        if not isinstance(doc.get("value"), int):
            fail(f"{where}: value must be an integer")
    elif kind == "HyperNode":
        require(doc, "spec.tier", where)
        members = spec.get("members")
        if not isinstance(members, list) or not members:
            fail(f"{where}: spec.members required")
        else:
            for i, member in enumerate(members):
                if not isinstance(member, dict) or "type" not in member or "selector" not in member:
                    fail(f"{where}.members[{i}]: type/selector required")
    elif kind == "NamespaceQueue":
        ns = doc.get("metadata", {}).get("namespace")
        if not ns:
            fail(f"{where}: NamespaceQueue must be namespaced")
        require(doc, "spec.parent", where)
        if "capability" not in spec and "guarantee" not in spec:
            fail(f"{where}: NamespaceQueue needs capability or guarantee")
    elif kind == "PodDisruptionBudget":
        require(doc, "spec.selector", where)
        if "minAvailable" not in spec and "maxUnavailable" not in spec:
            fail(f"{where}: PodDisruptionBudget needs minAvailable or maxUnavailable")
    elif kind == "ConfigMap":
        if doc.get("metadata", {}).get("name") != "volcano-scheduler-configmap":
            fail(f"{where}: unexpected ConfigMap name")
        content = (doc.get("data") or {}).get("volcano-scheduler.conf")
        if not content:
            fail(f"{where}: volcano-scheduler.conf required")
        else:
            try:
                conf = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                fail(f"{where}: scheduler config is not valid YAML: {exc}")
                return
            if not isinstance(conf, dict) or "actions" not in conf or "tiers" not in conf:
                fail(f"{where}: scheduler config needs actions and tiers")
            else:
                actions = [a.strip() for a in str(conf["actions"]).split(",") if a.strip()]
                if not actions:
                    fail(f"{where}: empty actions")
                plugin_options = {"enablePreemptable", "enableHierarchy", "enableJobStarving"}
                for tier in conf["tiers"]:
                    if "name" not in tier or "plugins" not in tier:
                        fail(f"{where}: tier needs name/plugins")
                        continue
                    for plugin in tier["plugins"]:
                        args = plugin.get("arguments") or {}
                        for opt in plugin_options & set(args):
                            fail(f"{where}: {opt} is a plugin option, not an argument")
                        for opt in plugin_options & set(plugin):
                            if not isinstance(plugin[opt], bool):
                                fail(f"{where}: plugin option {opt} must be a boolean")
                        if plugin.get("name") == "rescheduling":
                            if any(str(k).startswith("rescheduling.") for k in args):
                                fail(f"{where}: rescheduling arguments use flat rescheduling.* keys")
                            if "strategies" in args and not isinstance(args["strategies"], list):
                                fail(f"{where}: rescheduling.strategies must be a list")
    elif kind == "Deployment":
        pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        if pod_spec.get("schedulerName") != "volcano":
            fail(f"{where}: deployment pod template must use volcano")
    elif kind == "Namespace":
        if not doc.get("metadata", {}).get("name"):
            fail(f"{where}: namespace needs a name")
    elif kind == "ResourceQuota":
        ns = doc.get("metadata", {}).get("namespace")
        if not ns:
            fail(f"{where}: ResourceQuota must be namespaced")
        hard = (doc.get("spec") or {}).get("hard")
        if not isinstance(hard, dict) or not hard:
            fail(f"{where}: ResourceQuota needs spec.hard")
    else:
        fail(f"{where}: unexpected kind {kind}")


def check_examples() -> None:
    yamls = sorted(p for p in EXAMPLES.rglob("*.yaml")) + sorted(p for p in EXAMPLES.rglob("*.yml"))
    for path in yamls:
        where = str(path.relative_to(ROOT))
        try:
            docs = list(yaml.safe_load_all(read(path)))
        except yaml.YAMLError as exc:
            fail(f"{where}: YAML parse error: {exc}")
            continue
        docs = [d for d in docs if d is not None]
        if not docs:
            fail(f"{where}: empty YAML")
        for i, doc in enumerate(docs):
            check_manifest(doc, f"{where}[{i}]")
        ok(f"{where}: {len(docs)} document(s)")

    for path in sorted(EXAMPLES.rglob("*.json")):
        where = str(path.relative_to(ROOT))
        try:
            data = json.loads(read(path))
        except json.JSONDecodeError as exc:
            fail(f"{where}: JSON parse error: {exc}")
            continue
        if not isinstance(data, dict) or not data.get("actions") or not data.get("tiers"):
            fail(f"{where}: MiniVolcano config needs actions/tiers")
        for tier in data.get("tiers", []):
            if "name" not in tier or "plugins" not in tier:
                fail(f"{where}: tier needs name/plugins")
        ok(f"{where}: MiniVolcano JSON config")


def check_mini_sync() -> None:
    # Docs that talk about MiniVolcano must not contradict the code's plugin/action counts.
    for name in ("README.md", "docs/21-mini-design.md", "docs/35-appendix.md"):
        text = read(ROOT / name)
        if "23" not in text:
            fail(f"{name}: expected plugin count 23")
        if "28" not in text:
            fail(f"{name}: expected scenario count 28")


def check_api_coverage() -> None:
    """Guard the v1.16 API/extension fields the tutorial relies on."""
    appendix = read(DOCS / "35-appendix.md")
    for token in ("minTaskMember", "subGroupPolicy", "minSuccess", "dependsOn",
                  "nodeGroupAffinity", "nodeGroupAntiAffinity",
                  "nodegroup-resource-limits", "cooldown-time", "cdp"):
        if token not in appendix:
            fail(f"docs/35-appendix.md: missing API token {token!r}")
    ext = read(DOCS / "33-extension-engineering.md")
    for token in ("PluginInterface", "OnPodCreate", "OnJobAdd", "RegisterPluginBuilder"):
        if token not in ext:
            fail(f"docs/33-extension-engineering.md: missing job-plugin token {token!r}")
    ok("docs: v1.16 task/queue fields and job-plugin interface documented")


def check_auxiliary_tools() -> None:
    model = ROOT / "scripts" / "capacity_model.py"
    if not model.is_file():
        fail("scripts/capacity_model.py: missing")
        return
    text = read(model)
    for token in ("def selftest", "def build_report", "def queue_plan"):
        if token not in text:
            fail(f"scripts/capacity_model.py: missing {token}")
    ok("scripts/capacity_model.py: present and self-testable")


def main() -> int:
    if not DOCS.is_dir():
        print("docs/ not found", file=sys.stderr)
        return 2
    check_chapters()
    check_readme()
    check_counts()
    check_examples()
    check_mini_sync()
    check_api_coverage()
    check_auxiliary_tools()

    print(f"checks passed: {len(checks)}")
    for line in checks:
        print("  OK", line)
    if errors:
        print(f"\nFAILED: {len(errors)} problem(s)", file=sys.stderr)
        for line in errors:
            print("  -", line, file=sys.stderr)
        return 1
    print("\nAll documentation and example checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
