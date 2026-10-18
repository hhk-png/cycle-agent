from __future__ import annotations

import copy

from minikuberay import constants as C
from minikuberay.models.meta import new_object
from tests.conftest import Harness, cluster_spec


def job_spec(**overrides):
    spec = {
        "entrypoint": "python -c \"print('hi')\"",
        "submissionMode": "K8sJobMode",
        "shutdownAfterJobFinishes": False,
        "backoffLimit": 0,
        "rayClusterSpec": cluster_spec(workers=0),
    }
    spec.update(overrides)
    return spec


def apply_job(harness: Harness, spec=None, name="job", annotations=None, labels=None):
    obj = new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, name, "default", spec=copy.deepcopy(spec or job_spec()), annotations=annotations, labels=labels)
    return harness.store.create(C.KIND_RAYJOB, obj, "default")


def run(harness: Harness, rounds: int = 50, advance: float = 0.5):
    harness.step(rounds, advance=advance)


def test_job_succeeds(harness):
    apply_job(harness)
    run(harness)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobStatus"] == "SUCCEEDED"
    assert job["status"]["jobDeploymentStatus"] == "Complete"
    assert job["status"]["succeeded"] == 1
    assert job["status"]["jobId"].startswith("job-")
    assert job["status"]["rayClusterName"].startswith("job-")


def test_job_creates_and_cleans_submitter(harness):
    apply_job(harness)
    run(harness, 8, 0.5)
    # The submitter exists while the Ray job is running.
    assert harness.list(C.KIND_JOB)
    run(harness, 30, 0.5)
    assert harness.list(C.KIND_JOB) == []


def test_job_shutdown_deletes_cluster(harness):
    apply_job(harness, job_spec(shutdownAfterJobFinishes=True))
    run(harness, 70, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Complete"
    assert harness.list(C.KIND_RAYCLUSTER) == []


def test_job_failure(harness):
    apply_job(harness, job_spec(), annotations={C.ANNOTATION_SIM_JOB_EXIT_CODE: "1"})
    run(harness, 50, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobStatus"] == "FAILED"
    assert job["status"]["jobDeploymentStatus"] == "Failed"
    assert job["status"]["reason"] == "AppFailed"
    assert job["status"]["failed"] == 1


def test_job_backoff_retries(harness):
    apply_job(harness, job_spec(backoffLimit=1), annotations={C.ANNOTATION_SIM_JOB_EXIT_CODE: "1"})
    run(harness, 160, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "Failed"
    assert job["status"]["failed"] == 2


def test_job_active_deadline(harness):
    apply_job(
        harness,
        job_spec(activeDeadlineSeconds=1),
        annotations={C.ANNOTATION_SIM_JOB_DURATION: "30"},
    )
    run(harness, 50, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "Failed"
    assert job["status"]["reason"] == "DeadlineExceeded"


def test_job_pre_running_deadline(harness):
    bad_cluster = cluster_spec(workers=0, cpu="100")
    apply_job(harness, job_spec(preRunningDeadlineSeconds=1, rayClusterSpec=bad_cluster))
    run(harness, 40, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "Failed"
    assert job["status"]["reason"] == "PreRunningDeadlineExceeded"


def test_job_suspend_and_resume(harness):
    apply_job(harness, job_spec(suspend=True, shutdownAfterJobFinishes=True))
    run(harness, 10, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Suspended"
    assert harness.list(C.KIND_RAYCLUSTER) == []
    job = harness.get(C.KIND_RAYJOB, "job")
    job["spec"]["suspend"] = False
    harness.store.update(C.KIND_RAYJOB, job)
    run(harness, 60, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Complete"


def test_job_ttl_after_finished(harness):
    apply_job(harness, job_spec(shutdownAfterJobFinishes=True, ttlSecondsAfterFinished=10))
    run(harness, 18, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Complete"
    # shutdownAfterJobFinishes removes the RayCluster immediately; the TTL
    # controls when the RayJob CR itself is deleted.
    assert harness.list(C.KIND_RAYCLUSTER) == []
    assert harness.store.exists(C.KIND_RAYJOB, "job", "default")
    harness.clock.advance(11)
    run(harness, 20, 0.5)
    assert not harness.store.exists(C.KIND_RAYJOB, "job", "default")


def test_deletion_rules_delete_cluster(harness):
    spec = job_spec(
        deletionStrategy={
            "deletionRules": [
                {"policy": "DeleteCluster", "condition": {"jobStatus": "FAILED", "ttlSeconds": 0}},
                {"policy": "DeleteNone", "condition": {"jobStatus": "SUCCEEDED", "ttlSeconds": 0}},
            ]
        }
    )
    apply_job(harness, spec, annotations={C.ANNOTATION_SIM_JOB_EXIT_CODE: "1"})
    run(harness, 80, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Failed"
    assert harness.list(C.KIND_RAYCLUSTER) == []


def test_deletion_rules_delete_self(harness):
    spec = job_spec(
        deletionStrategy={
            "deletionRules": [
                {"policy": "DeleteSelf", "condition": {"jobStatus": "SUCCEEDED", "ttlSeconds": 0}},
            ]
        }
    )
    apply_job(harness, spec)
    run(harness, 80, 0.5)
    assert not harness.store.exists(C.KIND_RAYJOB, "job", "default")


def test_deletion_rules_delete_workers(harness):
    spec = job_spec(
        shutdownAfterJobFinishes=False,
        rayClusterSpec=cluster_spec(workers=1, min_replicas=1),
        deletionStrategy={
            "deletionRules": [
                {"policy": "DeleteWorkers", "condition": {"jobStatus": "SUCCEEDED", "ttlSeconds": 0}},
            ]
        },
    )
    apply_job(harness, spec)
    run(harness, 60, 0.5)
    assert harness.list(C.KIND_RAYCLUSTER)
    workers = [
        pod
        for pod in harness.list(C.KIND_POD)
        if pod["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    ]
    assert workers == []


def test_cluster_selector_uses_existing_cluster(harness):
    harness.apply(C.KIND_RAYCLUSTER, "shared", cluster_spec(workers=0))
    harness.settle(30)
    apply_job(
        harness,
        {
            "entrypoint": "echo on shared",
            "submissionMode": "HTTPMode",
            "clusterSelector": {C.LABEL_CLUSTER: "shared"},
        },
    )
    run(harness, 60, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "Complete"
    assert job["status"]["rayClusterName"] == "shared"
    assert harness.store.exists(C.KIND_RAYCLUSTER, "shared", "default")


def test_http_mode_has_no_submitter_job(harness):
    apply_job(harness, job_spec(submissionMode="HTTPMode"))
    run(harness, 60, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Complete"
    assert harness.list(C.KIND_JOB) == []


def test_sidecar_mode_injects_sidecar(harness):
    apply_job(harness, job_spec(submissionMode="SidecarMode"))
    run(harness, 60, 0.5)
    cluster = harness.list(C.KIND_RAYCLUSTER)[0]
    containers = [item["name"] for item in cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"]]
    assert "ray-job-submitter" in containers
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Complete"


def test_interactive_mode_waits_then_adopts(harness):
    apply_job(harness, job_spec(submissionMode="InteractiveMode", entrypoint=""))
    run(harness, 40, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "Waiting"
    cluster_name = job["status"]["rayClusterName"]
    harness.runtime.submit_job("default", cluster_name, "interactive-1", "echo external", duration_seconds=1.0)
    job["spec"]["jobId"] = "interactive-1"
    harness.store.update(C.KIND_RAYJOB, job)
    run(harness, 40, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "Complete"


def test_explicit_job_id_is_preserved(harness):
    apply_job(harness, job_spec(jobId="my-fixed-id"))
    run(harness, 50, 0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobId"] == "my-fixed-id"


def test_validation_failure_status(harness):
    apply_job(harness, {"entrypoint": "echo", "submissionMode": "K8sJobMode"})
    run(harness, 10, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "ValidationFailed"
    assert job["status"]["reason"] == "ValidationFailed"


def test_deleting_running_job_stops_it(harness):
    apply_job(harness, job_spec(), annotations={C.ANNOTATION_SIM_JOB_DURATION: "100"})
    run(harness, 20, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    cluster_name = job["status"]["rayClusterName"]
    job_id = job["status"]["jobId"]
    harness.store.delete(C.KIND_RAYJOB, "job", "default")
    # The finalizer stops the job in the first reconciliation; the cluster is
    # garbage-collected afterwards, taking the runtime record with it.
    harness.step(1, 0.5)
    record = harness.runtime.get_job("default", cluster_name, job_id)
    assert record is not None and record.status == C.JOB_STATUS_STOPPED
    run(harness, 20, 0.5)
    assert not harness.store.exists(C.KIND_RAYJOB, "job", "default")


def test_runtime_env_and_metadata_passed(harness):
    apply_job(
        harness,
        job_spec(metadata={"team": "ml"}, runtimeEnvYAML="pip:\n  - requests\n"),
    )
    run(harness, 50, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    records = harness.runtime.list_jobs("default", job["status"]["rayClusterName"])
    assert records
    assert records[0].runtime_env.startswith("pip:")
    assert records[0].metadata["team"] == "ml"


def test_entrypoint_resources_are_forwarded(harness):
    apply_job(
        harness,
        job_spec(
            entrypointNumCpus="0.5",
            entrypointNumGpus="1",
            entrypointResources='{"Custom1": 2}',
        ),
    )
    run(harness, 50, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "Complete"
    records = harness.runtime.list_jobs("default", job["status"]["rayClusterName"])
    assert records
    assert records[0].metadata["entrypoint_num_cpus"] == "0.5"
    assert records[0].metadata["entrypoint_num_gpus"] == "1"
    assert records[0].metadata["entrypoint_resources"] == '{"Custom1": 2}'


def test_invalid_entrypoint_resources_are_rejected(harness):
    apply_job(harness, job_spec(entrypointNumCpus="-1"))
    run(harness, 10, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "ValidationFailed"
    assert "entrypointNumCpus" in job["status"]["message"]


def test_entrypoint_resources_must_be_a_json_string(harness):
    # Upstream models entrypointResources as a string.  Passing a map is a
    # common mistake and must fail early instead of being silently accepted.
    apply_job(harness, job_spec(entrypointResources={"Custom1": 2}))
    run(harness, 10, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "ValidationFailed"
    assert "JSON string" in job["status"]["message"]


def test_failed_submitter_fails_job_before_submission(harness):
    apply_job(
        harness,
        job_spec(
            submitterPodTemplate={
                "metadata": {
                    "annotations": {
                        "minikuberay.io/submitter-finished": "true",
                        "minikuberay.io/submitter-exit-code": "1",
                    }
                }
            }
        ),
    )
    run(harness, 60, 0.5)
    job = harness.get(C.KIND_RAYJOB, "job")
    assert job["status"]["jobDeploymentStatus"] == "Failed"
    assert job["status"]["reason"] == "SubmissionFailed"
    assert job["status"]["failed"] == 1
