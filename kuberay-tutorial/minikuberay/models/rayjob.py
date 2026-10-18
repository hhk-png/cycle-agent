"""RayJob status and identity helpers."""

from __future__ import annotations

from .. import constants as C
from ..util import labels_match, make_name


def job_is_terminal(status: str | None) -> bool:
    return (status or "") in C.TERMINAL_JOB_STATUSES


def deployment_is_terminal(status: str | None) -> bool:
    return (status or "") in (C.DEPLOYMENT_COMPLETE, C.DEPLOYMENT_FAILED)


def deployment_is_active(status: str | None) -> bool:
    return (status or "") in (
        C.DEPLOYMENT_INITIALIZING,
        C.DEPLOYMENT_RUNNING,
        C.DEPLOYMENT_RETRYING,
        C.DEPLOYMENT_WAITING,
        C.DEPLOYMENT_SUSPENDING,
    )


def generate_job_id(rayjob_name: str, suffix: str | None = None) -> str:
    return make_name(rayjob_name, suffix)


def generate_cluster_name(rayjob_name: str, suffix: str | None = None) -> str:
    return make_name(rayjob_name, suffix)


def selector_matches(cluster: dict, selector: dict | None) -> bool:
    if not selector:
        return False
    if selector.get(C.LABEL_CLUSTER):
        # The default selector key is special: it names the cluster directly.
        if cluster.get("metadata", {}).get("name") != selector[C.LABEL_CLUSTER]:
            return False
    remaining = {key: value for key, value in selector.items() if key != C.LABEL_CLUSTER}
    return labels_match({"matchLabels": remaining}, cluster.get("metadata", {}).get("labels") or {})


def effective_deletion_policy(spec: dict, job_status: str, deployment_status: str) -> str | None:
    """Return the deletion policy for the current terminal state, if any."""
    strategy = spec.get("deletionStrategy")
    if strategy:
        rules = strategy.get("deletionRules") or []
        if rules:
            for rule in rules:
                condition = rule.get("condition") or {}
                if condition.get("jobStatus") and condition["jobStatus"] == job_status:
                    return rule.get("policy")
                if condition.get("jobDeploymentStatus") and condition["jobDeploymentStatus"] == deployment_status:
                    return rule.get("policy")
            return "DeleteNone"
        if job_status == C.JOB_STATUS_SUCCEEDED and strategy.get("onSuccess"):
            return (strategy["onSuccess"] or {}).get("policy")
        if job_status == C.JOB_STATUS_FAILED and strategy.get("onFailure"):
            return (strategy["onFailure"] or {}).get("policy")
        return "DeleteNone"
    if spec.get("shutdownAfterJobFinishes"):
        return "DeleteCluster"
    return None
