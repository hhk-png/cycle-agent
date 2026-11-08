#!/usr/bin/env python3
"""Static checks for a Kueue controller Configuration.

This is a teaching linter, not a schema validator. It catches the mistakes that
most often turn into a crash-looping controller or a silently changed scheduling
behaviour:

- top-level field typos and legacy v1beta1 shapes;
- unknown feature gates and a few known dependency inversions;
- feature gate conflicts with the command-line flag convention;
- dangerous namespace-management combinations;
- malformed durations / visibility intervals;
- reserved resource names and duplicate inputs in resource transformations;
- obviously invalid fair-sharing / preemption strategy names.

Input can be either a bare `Configuration` document or the ConfigMap that Helm
renders (it reads `data["controller_manager_config.yaml"]`).

Usage:
    python scripts/check_kueue_config.py examples/config/kueue-config.yaml
    python scripts/check_kueue_config.py --json config.yaml
    python scripts/check_kueue_config.py --strict config.yaml

Exit codes: 0 = no errors (warnings allowed), 1 = errors, 2 = input problem.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - environment guidance
    print("PyYAML is required: python -m pip install pyyaml", file=sys.stderr)
    sys.exit(2)

API_PREFIX = "config.kueue.x-k8s.io/"

# Top-level fields of Configuration v1beta2. Keep this list intentionally
# explicit: an unknown top-level key is usually a typo or a field that moved
# between versions.
TOP_LEVEL = {
    "apiVersion",
    "kind",
    "namespace",
    "featureGates",
    "manageJobsWithoutQueueName",
    "managedJobsNamespaceSelector",
    "integrations",
    "queueVisibility",
    "waitForPodsReady",
    "fairSharing",
    "admissionFairSharing",
    "multiKueue",
    "resources",
    "internalCertManagement",
    "health",
    "metrics",
    "webhook",
    "leaderElection",
    "controller",
    "objectRetentionPolicies",
    "clientConnection",
}

# Known gates in the v0.20/v0.21 line. Unknown gates are warnings, not errors:
# the list is version-dependent and the tutorial should not pretend to be a
# schema. Add new gates here when you upgrade.
KNOWN_GATES = {
    "AdmissionFairSharing",
    "AdmissionFairSharingAnchorAtQuotaReservation",
    "ConcurrentAdmission",
    "ConfigurablePreemptions",
    "DynamicQuotaOrchestration",
    "ElasticJobsViaWorkloadSlices",
    "ElasticJobsViaWorkloadSlicesWithPartialReplicaScaleUp",
    "KueueDRAIntegration",
    "KueueDRAIntegrationConsumableCapacity",
    "KueueDRAIntegrationDeviceTaints",
    "KueueDRAIntegrationExtendedResource",
    "KueueDRAIntegrationPartitionableDevices",
    "KueueDRAIntegrationPrioritizedList",
    "KueueDRADeviceFeasibility",
    "FairSharingRefill",
    "FlavorFungibilityPreserveScanProgress",
    "HighMaxParallelismWithinReconcile",
    "ReservedResourceNameValidation",
    "ACRequeueAfter",
    "TASCacheTopologyTree",
    "TASGroupedPodSetSlicing",
    "TASNodeFeasibilityForAllLevels",
    "LocalQueueDefaulting",
    "ManagedJobsNamespaceSelectorAlwaysRespected",
    "MultiKueue",
    "MultiKueueClusterProfile",
    "MultiKueueKubeConfigPathValidation",
    "MultiKueueOrchestratedPreemption",
    "MultiKueueRemoteSpecSync",
    "PartialAdmission",
    "PrioritySortingWithinCohort",
    "TASBalancedPlacement",
    "TASFailedNodeReplacement",
    "TASFailedNodeReplacementFailFast",
    "TASMultiLayerTopology",
    "TASPartialSlices",
    "TASReplaceNodeOnNodeTaints",
    "TASReplaceNodeOnPodTermination",
    "TASResidualPodAccounting",
    "TASTopologySpreading",
    "TopologyAwareScheduling",
    "WorkloadPriorityClassDefaulting",
    "WorkloadRequestUseMergePatch",
}

# Dependencies where "dependency false + dependent true" is rejected at startup.
GATE_DEPENDENCIES = {
    "AdmissionFairSharingAnchorAtQuotaReservation": "AdmissionFairSharing",
    "KueueDRAIntegrationExtendedResource": "KueueDRAIntegration",
    "KueueDRAIntegrationConsumableCapacity": "KueueDRAIntegration",
    "KueueDRAIntegrationDeviceTaints": "KueueDRAIntegration",
    "KueueDRADeviceFeasibility": "KueueDRAIntegration",
    "MultiKueueOrchestratedPreemption": "MultiKueue",
    "MultiKueueRemoteSpecSync": "MultiKueue",
    "TASBalancedPlacement": "TopologyAwareScheduling",
    "TASFailedNodeReplacement": "TopologyAwareScheduling",
    "TASFailedNodeReplacementFailFast": "TopologyAwareScheduling",
    "TASMultiLayerTopology": "TopologyAwareScheduling",
    "TASPartialSlices": "TopologyAwareScheduling",
    "TASReplaceNodeOnNodeTaints": "TopologyAwareScheduling",
    "TASReplaceNodeOnPodTermination": "TopologyAwareScheduling",
    "TASResidualPodAccounting": "TopologyAwareScheduling",
    "TASTopologySpreading": "TopologyAwareScheduling",
}

KNOWN_FRAMEWORKS = {
    "batch/job",
    "jobset.x-k8s.io/jobset",
    "kubeflow.org/mpijob",
    "kubeflow.org/pytorchjob",
    "kubeflow.org/tfjob",
    "kubeflow.org/xgboostjob",
    "kubeflow.org/jaxjob",
    "ray.io/rayjob",
    "ray.io/raycluster",
    "pod",
    "deployment",
    "statefulset",
    "leaderworkerset.x-k8s.io/leaderworkerset",
    "trainer.kubeflow.org/trainjob",
    "sparkoperator.k8s.io/sparkapplication",
    "workload.codeflare.dev/appwrapper",
}

KNOWN_PREEMPTION_STRATEGIES = {
    "LessThanOrEqualToFinalShare",
    "LessThanInitialShare",
}

DURATION_PART_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ns|us|µs|ms|s|m|h)")
DURATION_FACTORS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)


def load_document(path: Path) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    if len(docs) != 1:
        raise ValueError(f"expected exactly one YAML document, got {len(docs)}")
    doc = docs[0]
    if not isinstance(doc, dict):
        raise ValueError("document is not a mapping")

    # Accept the Helm-rendered ConfigMap form.
    if doc.get("kind") == "ConfigMap":
        data = doc.get("data") or {}
        raw = data.get("controller_manager_config.yaml")
        if not raw:
            raise ValueError(
                "ConfigMap does not contain data.controller_manager_config.yaml"
            )
        inner = yaml.safe_load(raw)
        if not isinstance(inner, dict):
            raise ValueError("controller_manager_config.yaml is not a mapping")
        return inner
    return doc


def parse_duration(value: Any) -> float | None:
    """Parse a Go duration like "30m", "1h30m" or "500ms" into seconds."""
    if not isinstance(value, str) or not value:
        return None
    pos = 0
    total = 0.0
    for match in DURATION_PART_RE.finditer(value):
        if match.start() != pos:
            return None
        total += float(match.group(1)) * DURATION_FACTORS[match.group(2)]
        pos = match.end()
    return total if pos == len(value) else None


def is_duration(value: Any) -> bool:
    return parse_duration(value) is not None


def check_feature_gates(cfg: dict[str, Any], report: Report) -> None:
    gates = cfg.get("featureGates")
    if gates is None:
        return
    if not isinstance(gates, dict):
        report.error("featureGates must be a map[string]bool")
        return

    for name, enabled in gates.items():
        if not isinstance(name, str) or not isinstance(enabled, bool):
            report.error(f"featureGates[{name!r}] must map a string to a bool")
            continue
        if name not in KNOWN_GATES:
            report.warn(
                f"unknown feature gate {name!r}; confirm it exists in your Kueue version"
            )
        dep = GATE_DEPENDENCIES.get(name)
        if dep and enabled and gates.get(dep) is False:
            report.error(
                f"feature gate {name}=true requires {dep}=true (otherwise the controller refuses to start)"
            )

    # The CLI flag and the Configuration field are mutually exclusive.
    if cfg.get("__used_feature_gates_flag__"):
        report.error(
            "featureGates in Configuration cannot be combined with --feature-gates"
        )


def check_management(cfg: dict[str, Any], report: Report) -> None:
    manage = cfg.get("manageJobsWithoutQueueName")
    if manage is not None and not isinstance(manage, bool):
        report.error("manageJobsWithoutQueueName must be a boolean")

    selector = cfg.get("managedJobsNamespaceSelector")
    if selector is not None:
        if not isinstance(selector, dict):
            report.error("managedJobsNamespaceSelector must be a LabelSelector")
        elif selector == {}:
            report.warn(
                "managedJobsNamespaceSelector: {} matches every namespace; "
                "the opt-in label loses its meaning"
            )
        if manage is False:
            report.info(
                "managedJobsNamespaceSelector is still enforced when "
                "manageJobsWithoutQueueName=false (v0.19+)"
            )
    elif manage is True:
        report.warn(
            "manageJobsWithoutQueueName=true without managedJobsNamespaceSelector "
            "affects every namespace, including system namespaces"
        )

    if manage is True:
        report.info(
            "manageJobsWithoutQueueName only takes over unlabeled jobs; it does not "
            "choose a LocalQueue. Use a LocalQueue named 'default' for defaulting."
        )


def check_integrations(cfg: dict[str, Any], report: Report) -> None:
    integrations = cfg.get("integrations")
    if integrations is None:
        return
    if not isinstance(integrations, dict):
        report.error("integrations must be a mapping")
        return
    frameworks = integrations.get("frameworks")
    if frameworks is not None:
        if not isinstance(frameworks, list):
            report.error("integrations.frameworks must be a list")
        else:
            for framework in frameworks:
                if framework not in KNOWN_FRAMEWORKS:
                    report.warn(
                        f"unknown integration framework {framework!r}; "
                        "the controller will log an error or skip it"
                    )


def check_wait_for_pods_ready(cfg: dict[str, Any], report: Report) -> None:
    block = cfg.get("waitForPodsReady")
    if block is None:
        return
    if not isinstance(block, dict):
        report.error("waitForPodsReady must be a mapping")
        return
    for field in ("timeout", "recoveryTimeout", "unscheduledTimeout"):
        value = block.get(field)
        if value is not None and not is_duration(value):
            report.error(
                f"waitForPodsReady.{field} must be a Go duration such as 30m or 1h"
            )
    timeout = block.get("timeout")
    parsed_timeout = parse_duration(timeout)
    if parsed_timeout is not None and parsed_timeout <= 0:
        report.warn(
            "waitForPodsReady.timeout is zero; readiness eviction would fire immediately"
        )


def check_queue_visibility(cfg: dict[str, Any], report: Report) -> None:
    block = cfg.get("queueVisibility")
    if block is None:
        return
    if not isinstance(block, dict):
        report.error("queueVisibility must be a mapping")
        return
    interval = block.get("updateIntervalSeconds")
    if interval is not None and (not isinstance(interval, int) or interval <= 0):
        report.error("queueVisibility.updateIntervalSeconds must be a positive integer")


def check_resources(cfg: dict[str, Any], report: Report) -> None:
    resources = cfg.get("resources")
    if resources is None:
        return
    if not isinstance(resources, dict):
        report.error("resources must be a mapping")
        return

    transformations = resources.get("transformations")
    if transformations is not None:
        if not isinstance(transformations, list):
            report.error("resources.transformations must be a list")
        else:
            seen_inputs: set[str] = set()
            for i, transformation in enumerate(transformations):
                where = f"resources.transformations[{i}]"
                if not isinstance(transformation, dict):
                    report.error(f"{where} must be a mapping")
                    continue
                inp = transformation.get("input")
                if not inp:
                    report.error(f"{where}.input is required")
                elif inp == "pods":
                    report.error(f"{where}.input uses reserved resource name 'pods'")
                elif inp in seen_inputs:
                    report.error(f"{where}.input {inp!r} is duplicated")
                else:
                    seen_inputs.add(inp)

                strategy = transformation.get("strategy")
                if strategy not in (None, "Retain", "Replace"):
                    report.error(f"{where}.strategy must be Retain or Replace")

                outputs = transformation.get("outputs")
                if outputs is not None:
                    if not isinstance(outputs, dict):
                        report.error(f"{where}.outputs must be a map")
                    elif "pods" in outputs:
                        report.error(
                            f"{where}.outputs uses reserved resource name 'pods'"
                        )

                multiplier = transformation.get("multiplyBy")
                if isinstance(multiplier, str) and multiplier == "pods":
                    report.error(f"{where}.multiplyBy uses reserved resource name 'pods'")

    mappings = resources.get("deviceClassMappings")
    if mappings is not None:
        if isinstance(mappings, dict):
            for device_class, resource in mappings.items():
                if resource == "pods":
                    report.error(
                        f"resources.deviceClassMappings[{device_class!r}] maps to reserved 'pods'"
                    )
        elif isinstance(mappings, list):
            for i, mapping in enumerate(mappings):
                if isinstance(mapping, dict):
                    for key, value in mapping.items():
                        if value == "pods":
                            report.error(
                                f"resources.deviceClassMappings[{i}].{key} uses reserved 'pods'"
                            )
        else:
            report.error("resources.deviceClassMappings must be a map or list")


def check_fair_sharing(cfg: dict[str, Any], report: Report) -> None:
    block = cfg.get("fairSharing")
    if block is None:
        return
    if not isinstance(block, dict):
        report.error("fairSharing must be a mapping")
        return
    strategies = block.get("preemptionStrategies")
    if strategies is not None:
        if not isinstance(strategies, list):
            report.error("fairSharing.preemptionStrategies must be a list")
        else:
            for strategy in strategies:
                if strategy not in KNOWN_PREEMPTION_STRATEGIES:
                    report.warn(
                        f"unknown fairSharing.preemptionStrategy {strategy!r}"
                    )

    afs = cfg.get("admissionFairSharing")
    if afs is not None:
        if not isinstance(afs, dict):
            report.error("admissionFairSharing must be a mapping")
            return
        half_life = afs.get("usageHalfLifeTime")
        if half_life is not None and not is_duration(half_life):
            report.error("admissionFairSharing.usageHalfLifeTime must be a Go duration")
        interval = afs.get("usageSamplingInterval")
        if interval is not None and not is_duration(interval):
            report.error("admissionFairSharing.usageSamplingInterval must be a Go duration")


def check_top_level(cfg: dict[str, Any], report: Report) -> None:
    api = cfg.get("apiVersion")
    if not isinstance(api, str) or not api.startswith(API_PREFIX):
        report.error(f"apiVersion must start with {API_PREFIX!r}, got {api!r}")
    elif api != "config.kueue.x-k8s.io/v1beta2":
        report.warn(f"apiVersion {api!r} is not v1beta2; check the migration guide")

    if cfg.get("kind") != "Configuration":
        report.error(f"kind must be Configuration, got {cfg.get('kind')!r}")

    for key in cfg:
        if key.startswith("__"):
            continue
        if key not in TOP_LEVEL:
            report.warn(f"unknown top-level field {key!r} (typo or moved between versions)")

    namespace = cfg.get("namespace")
    if namespace is not None and not isinstance(namespace, str):
        report.error("namespace must be a string")


def check(cfg: dict[str, Any]) -> Report:
    report = Report()
    check_top_level(cfg, report)
    check_feature_gates(cfg, report)
    check_management(cfg, report)
    check_integrations(cfg, report)
    check_wait_for_pods_ready(cfg, report)
    check_queue_visibility(cfg, report)
    check_resources(cfg, report)
    check_fair_sharing(cfg, report)
    return report


def render(report: Report, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {
                "errors": report.errors,
                "warnings": report.warnings,
                "infos": report.infos,
                "ok": not report.errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    lines: list[str] = []
    for msg in report.errors:
        lines.append(f"ERROR: {msg}")
    for msg in report.warnings:
        lines.append(f"WARN:  {msg}")
    for msg in report.infos:
        lines.append(f"INFO:  {msg}")
    if report.errors:
        lines.append(f"FAIL: {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    else:
        lines.append(f"OK: 0 errors, {len(report.warnings)} warning(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Configuration YAML or rendered ConfigMap")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures (useful in CI)",
    )
    parser.add_argument(
        "--feature-gates-flag",
        action="store_true",
        help="the Deployment also passes --feature-gates (mutually exclusive with featureGates)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_document(args.path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    if args.feature_gates_flag:
        cfg["__used_feature_gates_flag__"] = True

    report = check(cfg)
    print(render(report, args.json))
    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
