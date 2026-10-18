"""RayJob reconciler: cluster creation, submission modes and cleanup."""

from __future__ import annotations

from datetime import datetime, timezone

from .. import constants as C
from ..clock import iso, parse_iso
from ..models.core import new_job, pod_phase
from ..models.defaults import apply_defaults
from ..models.meta import (
    add_finalizer,
    deletion_timestamp,
    is_condition_true,
    name as obj_name,
    namespace as obj_namespace,
    owner_reference,
    remove_finalizer,
    set_condition,
)
from ..models.rayjob import (
    deployment_is_terminal,
    effective_deletion_policy,
    generate_cluster_name,
    generate_job_id,
    job_is_terminal,
    selector_matches,
)
from ..models.validation import validate_object
from ..util import canonical_json, deep_copy, get_ray_container, stable_hash
from .base import Controller
from .builders import build_head_pod
from .raycluster import RayClusterController


class RayJobController(Controller):
    name = "rayjob"
    kind = C.KIND_RAYJOB

    def reconcile(self, obj: dict) -> bool:
        job = deep_copy(obj)
        namespace = obj_namespace(job)
        name = obj_name(job)

        if deletion_timestamp(job):
            return self._cleanup(job)

        if (job.get("spec") or {}).get("managedBy") == C.MANAGED_BY_MULTIKUEUE:
            return False

        if add_finalizer(job, C.FINALIZER_RAYJOB):
            job["metadata"]["resourceVersion"] = obj["metadata"]["resourceVersion"]
            self.store.update(C.KIND_RAYJOB, job)
            return True

        defaulted = apply_defaults(job)
        if canonical_json(defaulted.get("spec")) != canonical_json(job.get("spec")):
            merged = deep_copy(job)
            merged["spec"] = defaulted["spec"]
            self.store.update(C.KIND_RAYJOB, merged)
            return True

        errors = validate_object(defaulted)
        if errors:
            return self._validation_failed(defaulted, "; ".join(errors))
        if defaulted["spec"].get("deletionStrategy") and not self.config.enabled("RayJobDeletionPolicy"):
            return self._validation_failed(defaulted, "deletionStrategy requires the RayJobDeletionPolicy feature gate")

        if defaulted.get("spec", {}).get("managedBy") == C.MANAGED_BY_MULTIKUEUE:
            return False

        status = deep_copy(defaulted.get("status") or {})
        deployment = status.get("jobDeploymentStatus", C.DEPLOYMENT_NEW)

        # Validation failure is a recoverable condition: once the user fixes
        # the spec, the job must re-enter initialization instead of staying
        # stuck in ValidationFailed forever.
        if deployment == C.DEPLOYMENT_VALIDATION_FAILED:
            status["jobDeploymentStatus"] = C.DEPLOYMENT_NEW
            status["jobStatus"] = C.JOB_STATUS_NEW
            status.pop("reason", None)
            status.pop("message", None)
            return self._commit(defaulted, status)

        if defaulted["spec"].get("suspend") and deployment not in (
            C.DEPLOYMENT_SUSPENDING,
            C.DEPLOYMENT_SUSPENDED,
        ):
            status["jobDeploymentStatus"] = C.DEPLOYMENT_SUSPENDING
            self._commit(defaulted, status)
            return True
        if not defaulted["spec"].get("suspend") and deployment == C.DEPLOYMENT_SUSPENDED:
            status["jobDeploymentStatus"] = C.DEPLOYMENT_NEW
            status["jobStatus"] = C.JOB_STATUS_NEW
            self._commit(defaulted, status)
            return True

        if deployment == C.DEPLOYMENT_NEW:
            if defaulted["spec"].get("suspend"):
                status["jobDeploymentStatus"] = C.DEPLOYMENT_SUSPENDED
                return self._commit(defaulted, status)
            return self._initialize(defaulted, status)

        if deployment in (C.DEPLOYMENT_INITIALIZING, C.DEPLOYMENT_WAITING):
            return self._initialize_or_wait(defaulted, status)

        if deployment == C.DEPLOYMENT_RUNNING:
            return self._running(defaulted, status)

        if deployment in (C.DEPLOYMENT_SUSPENDING, C.DEPLOYMENT_RETRYING):
            return self._cleanup_for_suspend_or_retry(defaulted, status)

        if deployment == C.DEPLOYMENT_SUSPENDED:
            return False

        if deployment_is_terminal(deployment):
            return self._finished(defaulted, status)

        return False

    # ------------------------------------------------------------------ helpers
    def _initialize(self, rayjob: dict, status: dict) -> bool:
        spec = rayjob.get("spec") or {}
        status["jobDeploymentStatus"] = C.DEPLOYMENT_INITIALIZING
        status["startTime"] = iso(self.clock.now())
        status["jobStatus"] = C.JOB_STATUS_NEW
        if spec.get("jobId"):
            status["jobId"] = spec["jobId"]
        elif spec.get("submissionMode") == "InteractiveMode":
            # The user supplies the ID after submitting; do not fabricate one.
            status.pop("jobId", None)
        else:
            status["jobId"] = generate_job_id(obj_name(rayjob), self._suffix(rayjob, "job"))
        if not spec.get("clusterSelector"):
            status["rayClusterName"] = self._cluster_name(rayjob)
        self._set_condition(rayjob, status, C.COND_JOB_SUBMISSION, "False", "NotSubmitted", "waiting for RayCluster")
        return self._commit(rayjob, status)

    def _initialize_or_wait(self, rayjob: dict, status: dict) -> bool:
        spec = rayjob.get("spec") or {}
        namespace = obj_namespace(rayjob)
        # Deadlines apply while the job is still initializing/waiting, not only
        # once it reaches Running.  Otherwise an unschedulable cluster would
        # keep the RayJob alive forever.
        for field, reason in (
            ("activeDeadlineSeconds", "DeadlineExceeded"),
            ("preRunningDeadlineSeconds", "PreRunningDeadlineExceeded"),
        ):
            if self._deadline_exceeded(rayjob, status, field):
                status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
                status["reason"] = reason
                status["message"] = f"{field} exceeded before the job reached Running"
                status["endTime"] = iso(self.clock.now())
                status["failed"] = int(status.get("failed", 0) or 0) + 1
                return self._commit(rayjob, status)
        if status.get("jobDeploymentStatus") == C.DEPLOYMENT_WAITING:
            # InteractiveMode: the user submits the job and supplies its ID.
            if spec.get("jobId"):
                status["jobId"] = spec["jobId"]
                status["jobDeploymentStatus"] = C.DEPLOYMENT_RUNNING
                return self._commit(rayjob, status)
            return False
        cluster = self._get_cluster(rayjob)
        if cluster is None:
            if spec.get("clusterSelector"):
                status["message"] = "no RayCluster matches clusterSelector"
                status["jobDeploymentStatus"] = C.DEPLOYMENT_INITIALIZING
                return self._commit(rayjob, status)
            cluster = self._create_cluster(rayjob)
            if cluster is None:
                return False
        status["rayClusterName"] = cluster["metadata"]["name"]
        status["rayClusterStatus"] = deep_copy(cluster.get("status") or {})
        status["dashboardURL"] = self._dashboard_url(cluster)

        deadline = self._deadline_exceeded(rayjob, status, "preRunningDeadlineSeconds")
        if deadline:
            status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
            status["reason"] = "PreRunningDeadlineExceeded"
            status["message"] = "RayJob did not reach Running before preRunningDeadlineSeconds"
            status["endTime"] = iso(self.clock.now())
            status["failed"] = int(status.get("failed", 0) or 0) + 1
            return self._commit(rayjob, status)

        if cluster.get("status", {}).get("state") != "ready":
            status["jobDeploymentStatus"] = C.DEPLOYMENT_INITIALIZING
            return self._commit(rayjob, status)

        if spec.get("submissionMode") == "InteractiveMode":
            status["jobDeploymentStatus"] = C.DEPLOYMENT_WAITING
            return self._commit(rayjob, status)

        if spec.get("submissionMode") == "K8sJobMode":
            self._ensure_submitter_job(rayjob, cluster)
            submitter = self._submitter_pod(rayjob)
            if submitter is not None and pod_phase(submitter) in ("Failed", "Succeeded"):
                phase = pod_phase(submitter)
                failed = self._submitter_failed(submitter)
                # A failed Pod may still be retried by its Job.  Only fail the
                # RayJob once submitterConfig.backoffLimit is exhausted; a
                # succeeded Pod with no Ray job record is always a failure.
                if failed and phase == "Failed" and not self._submitter_exhausted(rayjob):
                    status["jobDeploymentStatus"] = C.DEPLOYMENT_INITIALIZING
                    return self._commit(rayjob, status)
                if not failed:
                    # A successful submitter should have produced a job; if
                    # the record is not visible yet, keep initializing.
                    status["jobDeploymentStatus"] = C.DEPLOYMENT_INITIALIZING
                    return self._commit(rayjob, status)
                status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
                status["reason"] = "SubmissionFailed"
                status["message"] = f"submitter {phase.lower()} before the Ray job was submitted"
                status["endTime"] = iso(self.clock.now())
                status["failed"] = int(status.get("failed", 0) or 0) + 1
                return self._commit(rayjob, status)
            if submitter is None or pod_phase(submitter) != "Running":
                # The submitter Job/Pod must be running before the Ray job is
                # submitted; this keeps submitter failures observable.
                status["jobDeploymentStatus"] = C.DEPLOYMENT_INITIALIZING
                return self._commit(rayjob, status)

        # Submitting at the end of Initializing matches KubeRay: K8sJobMode
        # creates the submitter Job, then the Ray job is submitted asynchronously.
        self._submit(rayjob, cluster, status)
        status["jobDeploymentStatus"] = C.DEPLOYMENT_RUNNING
        return self._commit(rayjob, status)

    def _running(self, rayjob: dict, status: dict) -> bool:
        spec = rayjob.get("spec") or {}
        namespace = obj_namespace(rayjob)
        if spec.get("suspend"):
            status["jobDeploymentStatus"] = C.DEPLOYMENT_SUSPENDING
            return self._commit(rayjob, status)

        # A failed submitter means the entrypoint never reached Ray (or the
        # submission failed).  Surface it as a failed RayJob instead of waiting
        # for a runtime record that will never appear.
        if spec.get("submissionMode") in ("K8sJobMode", "SidecarMode"):
            submitter = self._submitter_pod(rayjob)
            if submitter is not None and self._submitter_failed(submitter):
                if spec.get("submissionMode") == "K8sJobMode" and pod_phase(submitter) == "Failed" and not self._submitter_exhausted(rayjob):
                    # The submitter Job will recreate the Pod; stay Running.
                    return self._commit(rayjob, status)
                status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
                status["reason"] = "SubmissionFailed"
                status["message"] = "submitter failed before the Ray job was submitted"
                status["endTime"] = iso(self.clock.now())
                status["failed"] = int(status.get("failed", 0) or 0) + 1
                return self._commit(rayjob, status)
        if self._deadline_exceeded(rayjob, status, "activeDeadlineSeconds"):
            self._stop_ray_job(rayjob, status)
            status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
            status["reason"] = "DeadlineExceeded"
            status["message"] = "activeDeadlineSeconds exceeded"
            status["endTime"] = iso(self.clock.now())
            status["failed"] = int(status.get("failed", 0) or 0) + 1
            return self._commit(rayjob, status)

        cluster = self._get_cluster(rayjob)
        if cluster is None:
            status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
            status["reason"] = "AppFailed"
            status["message"] = "RayCluster disappeared while the job was running"
            status["endTime"] = iso(self.clock.now())
            status["failed"] = int(status.get("failed", 0) or 0) + 1
            return self._commit(rayjob, status)
        status["rayClusterStatus"] = deep_copy(cluster.get("status") or {})

        if spec.get("submissionMode") == "InteractiveMode":
            if not spec.get("jobId"):
                return self._commit(rayjob, status)
            status["jobId"] = spec["jobId"]
            status["jobDeploymentStatus"] = C.DEPLOYMENT_RUNNING

        job_id = status.get("jobId", "")
        record = self.runtime.get_job(namespace, cluster["metadata"]["name"], job_id) if job_id else None
        if record is None:
            if spec.get("submissionMode") in ("K8sJobMode", "SidecarMode"):
                submitter = self._submitter_pod(rayjob)
                if submitter and pod_phase(submitter) in ("Succeeded", "Failed"):
                    # A completed submitter without a Ray job record means the
                    # job never reached Ray.  Reporting Complete here would
                    # hide a lost submission; upstream reports AppFailed.
                    status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
                    status["reason"] = "AppFailed"
                    status["message"] = "submitter completed but Ray job was not found"
                    status["endTime"] = iso(self.clock.now())
                    status["failed"] = int(status.get("failed", 0) or 0) + 1
                    return self._commit(rayjob, status)
            return self._commit(rayjob, status) if status else False

        if record.status not in C.TERMINAL_JOB_STATUSES:
            if not status.get("rayJobInfo", {}).get("startTime") and record.start_time:
                status["rayJobInfo"] = {**(status.get("rayJobInfo") or {}), "startTime": record.start_time}
            status["jobStatus"] = record.status
            return self._commit(rayjob, status)

        status["jobStatus"] = record.status
        status["jobDeploymentStatus"] = C.DEPLOYMENT_COMPLETE
        status["endTime"] = iso(self.clock.now())
        status["rayJobInfo"] = {
            "startTime": record.start_time,
            "endTime": record.end_time,
        }
        if record.status == C.JOB_STATUS_FAILED:
            status["jobDeploymentStatus"] = C.DEPLOYMENT_FAILED
            status["reason"] = "AppFailed"
            status["message"] = record.message
            status["failed"] = int(status.get("failed", 0) or 0) + 1
        elif record.status == C.JOB_STATUS_SUCCEEDED:
            status["succeeded"] = int(status.get("succeeded", 0) or 0) + 1
        if record.status == C.JOB_STATUS_STOPPED:
            status["jobDeploymentStatus"] = C.DEPLOYMENT_COMPLETE
        self._set_condition(rayjob, status, C.COND_JOB_SUBMISSION, "True", "Submitted", "job submitted")
        self._set_condition(rayjob, status, C.COND_JOB_FINISHED, "True", record.status, record.message)
        self._mark_submitter(rayjob, 0 if record.status != C.JOB_STATUS_FAILED else 1)
        return self._commit(rayjob, status)

    # ------------------------------------------------------------------ cluster
    @staticmethod
    def _suffix(rayjob: dict, purpose: str) -> str:
        return stable_hash(f"{obj_namespace(rayjob)}/{obj_name(rayjob)}/{purpose}", 5)

    def _cluster_name(self, rayjob: dict) -> str:
        return generate_cluster_name(obj_name(rayjob), self._suffix(rayjob, "cluster"))

    def _get_cluster(self, rayjob: dict) -> dict | None:
        namespace = obj_namespace(rayjob)
        status = rayjob.get("status") or {}
        selector = (rayjob.get("spec") or {}).get("clusterSelector")
        if selector:
            candidates = self.store.list(C.KIND_RAYCLUSTER, namespace)
            matching = [cluster for cluster in candidates if selector_matches(cluster, selector)]
            if not matching:
                return None
            matching.sort(key=lambda item: item.get("status", {}).get("state") != "ready")
            return matching[0]
        name = status.get("rayClusterName") or self._cluster_name(rayjob)
        try:
            return self.store.get(C.KIND_RAYCLUSTER, name, namespace)
        except Exception:
            return None

    def _create_cluster(self, rayjob: dict) -> dict | None:
        from ..models.meta import new_object

        spec = deep_copy((rayjob.get("spec") or {}).get("rayClusterSpec") or {})
        if not spec:
            return None
        name = self._cluster_name(rayjob)
        namespace = obj_namespace(rayjob)
        cluster_spec = deep_copy(spec)
        mode = (rayjob.get("spec") or {}).get("submissionMode", "K8sJobMode")
        if mode == "SidecarMode":
            head = cluster_spec.setdefault("headGroupSpec", {})
            head.setdefault("template", {}).setdefault("spec", {}).setdefault("containers", []).append(
                self._sidecar_container(rayjob)
            )
            cluster_spec["suspend"] = False
        if mode == "SidecarMode":
            annotations = (rayjob.get("metadata") or {}).get("annotations") or {}
            # Match KubeRay: sidecar-mode clusters must not recreate a head Pod
            # after provisioning (the submission ID would already be consumed).
            rayjob.setdefault("metadata", {}).setdefault("annotations", {})[C.ANNOTATION_DISABLE_HEAD_RESTART] = "true"
            _ = annotations
        cluster_annotations = {C.ANNOTATION_DISABLE_HEAD_RESTART: "true"} if mode == "SidecarMode" else {}
        # A RayJob embeds a RayClusterSpec, not a full RayCluster, so IPPR has
        # to be forwarded to the child CR by the controller.
        parent_annotations = (rayjob.get("metadata") or {}).get("annotations") or {}
        if C.ANNOTATION_IPPR in parent_annotations:
            cluster_annotations[C.ANNOTATION_IPPR] = parent_annotations[C.ANNOTATION_IPPR]
        cluster = new_object(
            C.RAY_API_VERSION,
            C.KIND_RAYCLUSTER,
            name,
            namespace,
            spec=cluster_spec,
            labels={
                C.LABEL_ORIGINATED_NAME: obj_name(rayjob),
                C.LABEL_ORIGINATED_CRD: C.KIND_RAYJOB,
                C.LABEL_JOB_SUBMISSION_MODE: mode,
                C.LABEL_KUBERAY_VERSION: "v1.7.1",
            },
            annotations=cluster_annotations,
            owner_references=[owner_reference(rayjob)],
        )
        try:
            return self.store.create(C.KIND_RAYCLUSTER, cluster, namespace)
        except Exception as exc:
            self.recorder.warning(rayjob, "FailedToCreateRayCluster", str(exc))
            return None

    def _sidecar_container(self, rayjob: dict) -> dict:
        spec = rayjob.get("spec") or {}
        return {
            "name": "ray-job-submitter",
            "image": (spec.get("rayClusterSpec", {}).get("headGroupSpec", {}).get("template", {}).get("spec", {}).get("containers") or [{}])[0].get("image", C.DEFAULT_IMAGE),
            "command": ["/bin/bash", "-c"],
            "args": ["echo 'sidecar submitter ready'; sleep infinity"],
            "env": [
                {"name": C.RAY_JOB_SUBMISSION_ID, "value": (rayjob.get("status") or {}).get("jobId", "")},
                {"name": C.RAY_DASHBOARD_ADDRESS, "value": "http://127.0.0.1:8265"},
            ],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
        }

    def _submit(self, rayjob: dict, cluster: dict, status: dict) -> None:
        namespace = obj_namespace(rayjob)
        spec = rayjob.get("spec") or {}
        job_id = status.get("jobId") or spec.get("jobId") or generate_job_id(obj_name(rayjob), self._suffix(rayjob, "job"))
        status["jobId"] = job_id
        metadata = rayjob.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        duration = annotations.get(C.ANNOTATION_SIM_JOB_DURATION)
        exit_code = int(annotations.get(C.ANNOTATION_SIM_JOB_EXIT_CODE, (spec.get("metadata") or {}).get("minikuberay.io/exit-code", 0)) or 0)
        # Entrypoint resource reservations are part of the submission request,
        # not of the RayCluster template.  Keep them on the simulated job so
        # tests (and the API/CLI) can observe what would have been sent.
        job_metadata = dict(spec.get("metadata") or {})
        for field, api_key in (
            ("entrypointNumCpus", "entrypoint_num_cpus"),
            ("entrypointNumGpus", "entrypoint_num_gpus"),
        ):
            value = spec.get(field)
            if value is not None:
                job_metadata[api_key] = str(value)
        if spec.get("entrypointResources"):
            value = spec["entrypointResources"]
            # The upstream field is already a JSON string; keep it verbatim so
            # what we record matches what would be sent to the Job API.
            job_metadata["entrypoint_resources"] = value if isinstance(value, str) else canonical_json(value)
        record = self.runtime.submit_job(
            namespace,
            cluster["metadata"]["name"],
            job_id,
            spec.get("entrypoint", ""),
            runtime_env=spec.get("runtimeEnvYAML", ""),
            duration_seconds=float(duration) if duration is not None else None,
            exit_code=exit_code,
            metadata=job_metadata,
            submission_id=job_id,
        )
        status["jobStatus"] = record.status
        status["dashboardURL"] = self._dashboard_url(cluster)
        self._set_condition(rayjob, status, C.COND_JOB_SUBMISSION, "True", "Submitted", f"Ray job {job_id} submitted")
        if spec.get("submissionMode") == "K8sJobMode":
            self._mark_submitter(rayjob, None)

    def _dashboard_url(self, cluster: dict) -> str:
        endpoints = (cluster.get("status") or {}).get("endpoints") or {}
        dashboard = endpoints.get("dashboard", "")
        return f"http://{dashboard}" if dashboard and "://" not in dashboard else dashboard

    # ----------------------------------------------------------------- submitter
    def _submitter_job_name(self, rayjob: dict) -> str:
        return f"{obj_name(rayjob)}-submitter"

    def _ensure_submitter_job(self, rayjob: dict, cluster: dict) -> None:
        from ..models.core import container, new_job

        namespace = obj_namespace(rayjob)
        name = self._submitter_job_name(rayjob)
        try:
            self.store.get(C.KIND_JOB, name, namespace)
            return
        except Exception:
            pass
        spec = rayjob.get("spec") or {}
        template = deep_copy(spec.get("submitterPodTemplate") or {})
        pod_spec = template.setdefault("spec", {})
        if not pod_spec.get("containers"):
            pod_spec["containers"] = [
                container(
                    "submitter",
                    C.DEFAULT_IMAGE,
                    command=["/bin/bash", "-c"],
                    args=[f"echo 'submitting {spec.get('entrypoint', '')}'; sleep 1"],
                    resources={"requests": {"cpu": "100m", "memory": "128Mi"}},
                )
            ]
        head_host = f"{cluster['metadata']['name']}-head-svc.{namespace}.svc.cluster.local"
        submitter_env = [
            {"name": C.RAY_JOB_SUBMISSION_ID, "value": (rayjob.get("status") or {}).get("jobId", "")},
            {"name": C.RAY_DASHBOARD_ADDRESS, "value": f"http://{head_host}:{C.PORT_DASHBOARD}"},
            {"name": C.FQ_RAY_IP, "value": head_host},
            {"name": C.RAY_ADDRESS, "value": f"{head_host}:{C.PORT_GCS}"},
        ]
        for env_item in submitter_env:
            if not any(item.get("name") == env_item["name"] for item in pod_spec["containers"][0].get("env") or []):
                pod_spec["containers"][0].setdefault("env", []).append(env_item)
        pod_spec.setdefault("restartPolicy", "Never")
        template.setdefault("metadata", {}).setdefault("labels", {})
        template["metadata"]["labels"]["job-name"] = name
        submitter_backoff = (spec.get("submitterConfig") or {}).get("backoffLimit")
        if submitter_backoff is None:
            # Upstream default for the submitter Job is 2, independent of the
            # RayJob-level backoffLimit.
            submitter_backoff = 2
        job = new_job(
            name,
            namespace,
            template,
            backoff_limit=int(submitter_backoff),
            labels={
                C.LABEL_ORIGINATED_NAME: obj_name(rayjob),
                C.LABEL_ORIGINATED_CRD: C.KIND_RAYJOB,
            },
            owner_references=[owner_reference(rayjob)],
        )
        try:
            self.store.create(C.KIND_JOB, job, namespace)
            self.recorder.normal(rayjob, "CreatedRayJobSubmitter", f"Created submitter Job {name}")
        except Exception as exc:
            self.recorder.warning(rayjob, "FailedToCreateRayJobSubmitter", str(exc))

    def _submitter_pod(self, rayjob: dict) -> dict | None:
        namespace = obj_namespace(rayjob)
        name = self._submitter_job_name(rayjob)
        pods = self.store.list(C.KIND_POD, namespace, label_selector={"job-name": name})
        return pods[0] if pods else None

    def _submitter_failed(self, pod: dict) -> bool:
        # The simulator (and tests) can declare the submitter's exit code
        # before the Pod reaches a terminal phase.  A non-zero exit always
        # means submission failed.
        code = (pod.get("metadata", {}).get("annotations") or {}).get("minikuberay.io/submitter-exit-code")
        try:
            if code is not None and int(code) != 0:
                return True
        except (TypeError, ValueError):
            pass
        return pod_phase(pod) == "Failed"

    def _submitter_exhausted(self, rayjob: dict) -> bool:
        """True when the submitter Job has exhausted its own backoffLimit."""
        try:
            job = self.store.get(C.KIND_JOB, self._submitter_job_name(rayjob), obj_namespace(rayjob))
        except Exception:
            return True
        for condition in (job.get("status") or {}).get("conditions") or []:
            if condition.get("type") == "Failed" and condition.get("status") == "True":
                return True
        return False

    def _delete_submitter(self, rayjob: dict) -> None:
        namespace = obj_namespace(rayjob)
        name = self._submitter_job_name(rayjob)
        for pod in self.store.list(C.KIND_POD, namespace, label_selector={"job-name": name}):
            try:
                self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
            except Exception:
                pass
        try:
            self.store.delete(C.KIND_JOB, name, namespace, force=True)
        except Exception:
            pass

    def _mark_submitter(self, rayjob: dict, exit_code: int | None) -> None:
        pod = self._submitter_pod(rayjob)
        if not pod:
            return
        annotations = pod.setdefault("metadata", {}).setdefault("annotations", {})
        if exit_code is not None:
            annotations["minikuberay.io/submitter-finished"] = "true"
            annotations["minikuberay.io/submitter-exit-code"] = str(exit_code)
            try:
                self.store.update(C.KIND_POD, pod)
            except Exception:
                pass

    # ------------------------------------------------------------------ cleanup
    def _stop_ray_job(self, rayjob: dict, status: dict) -> None:
        cluster = self._get_cluster(rayjob)
        if not cluster or not status.get("jobId"):
            return
        self.runtime.stop_job(obj_namespace(rayjob), cluster["metadata"]["name"], status["jobId"])

    def _cleanup(self, rayjob: dict) -> bool:
        namespace = obj_namespace(rayjob)
        status = rayjob.get("status") or {}
        if not job_is_terminal(status.get("jobStatus")):
            self._stop_ray_job(rayjob, status)
        try:
            self.store.delete(C.KIND_JOB, self._submitter_job_name(rayjob), namespace, force=True)
        except Exception:
            pass
        if remove_finalizer(rayjob, C.FINALIZER_RAYJOB):
            self.store.update(C.KIND_RAYJOB, rayjob)
            return True
        return False

    def _cleanup_for_suspend_or_retry(self, rayjob: dict, status: dict) -> bool:
        namespace = obj_namespace(rayjob)
        cluster = self._get_cluster(rayjob)
        cluster_deleted = True
        if cluster:
            try:
                self.store.delete(C.KIND_RAYCLUSTER, cluster["metadata"]["name"], namespace)
                cluster_deleted = False
            except Exception:
                cluster_deleted = False
        try:
            self.store.delete(C.KIND_JOB, self._submitter_job_name(rayjob), namespace, force=True)
        except Exception:
            pass
        if not cluster_deleted:
            return False
        status["rayClusterName"] = ""
        status["rayClusterStatus"] = {}
        status["dashboardURL"] = ""
        status["jobId"] = ""
        status["jobStatus"] = C.JOB_STATUS_NEW
        status["jobDeploymentStatus"] = (
            C.DEPLOYMENT_SUSPENDED if status.get("jobDeploymentStatus") == C.DEPLOYMENT_SUSPENDING else C.DEPLOYMENT_NEW
        )
        status.pop("reason", None)
        status.pop("message", None)
        status.pop("endTime", None)
        status.pop("rayJobInfo", None)
        status["conditions"] = [
            condition
            for condition in status.get("conditions") or []
            if condition.get("type") not in (C.COND_JOB_FINISHED, C.COND_JOB_SUBMISSION)
        ]
        return self._commit(rayjob, status)

    def _finished(self, rayjob: dict, status: dict) -> bool:
        spec = rayjob.get("spec") or {}
        namespace = obj_namespace(rayjob)
        self._delete_submitter(rayjob)
        job_status = status.get("jobStatus", C.JOB_STATUS_FAILED)
        deployment = status.get("jobDeploymentStatus")
        # Backoff: create a new cluster for the next attempt.  The counters
        # are incremented exactly once, when the job first reaches a terminal
        # state, not on every subsequent reconcile of that terminal state.
        failed = int(status.get("failed", 0) or 0)
        succeeded = int(status.get("succeeded", 0) or 0)
        backoff = int(spec.get("backoffLimit", 0) or 0)
        if deployment == C.DEPLOYMENT_FAILED and failed <= backoff and status.get("reason") not in ("DeadlineExceeded", "JobStatusCheckTimeoutExceeded"):
            status["jobDeploymentStatus"] = C.DEPLOYMENT_RETRYING
            return self._commit(rayjob, status)

        if spec.get("clusterSelector"):
            # The selected RayCluster is shared and not owned by this RayJob.
            # Never delete it or its workers, regardless of deletion fields.
            return False
        policy = effective_deletion_policy(spec, job_status, deployment or "")
        if (spec.get("deletionStrategy") or {}).get("deletionRules"):
            return self._apply_deletion_rules(rayjob, status)
        if policy is None or policy == "DeleteNone":
            return False
        if policy == "DeleteSelf":
            try:
                self.store.delete(C.KIND_RAYJOB, obj_name(rayjob), namespace)
            except Exception:
                pass
            return True
        if policy == "DeleteWorkers":
            return self._delete_workers(rayjob)
        if policy == "DeleteCluster":
            changed = False
            cluster = self._get_cluster(rayjob)
            if cluster:
                try:
                    self.store.delete(C.KIND_RAYCLUSTER, cluster["metadata"]["name"], namespace)
                    changed = True
                except Exception:
                    pass
            # KubeRay semantics: shutdownAfterJobFinishes removes the Ray
            # cluster immediately; ttlSecondsAfterFinished then deletes the
            # RayJob CR after the TTL (0 means keep the CR).
            ttl = int(spec.get("ttlSecondsAfterFinished", 0) or 0)
            if ttl and self._ttl_elapsed(status, ttl):
                try:
                    self.store.delete(C.KIND_RAYJOB, obj_name(rayjob), namespace)
                    changed = True
                except Exception:
                    pass
            return changed
        return False

    def _apply_deletion_rules(self, rayjob: dict, status: dict) -> bool:
        spec = rayjob.get("spec") or {}
        namespace = obj_namespace(rayjob)
        job_status = status.get("jobStatus", "")
        deployment = status.get("jobDeploymentStatus", "")
        rules = ((spec.get("deletionStrategy") or {}).get("deletionRules")) or []
        triggered = []
        for rule in rules:
            condition = rule.get("condition") or {}
            matches = (condition.get("jobStatus") == job_status) or (condition.get("jobDeploymentStatus") == deployment)
            if not matches:
                continue
            ttl = int(condition.get("ttlSeconds", 0) or 0)
            if ttl == 0 or self._ttl_elapsed(status, ttl):
                triggered.append(rule.get("policy"))
        if not triggered:
            return False
        # Most impactful first.
        order = {"DeleteSelf": 0, "DeleteCluster": 1, "DeleteWorkers": 2, "DeleteNone": 3}
        triggered.sort(key=lambda policy: order.get(policy, 9))
        policy = triggered[0]
        if policy == "DeleteSelf":
            try:
                self.store.delete(C.KIND_RAYJOB, obj_name(rayjob), namespace)
            except Exception:
                pass
            return True
        if policy == "DeleteCluster":
            cluster = self._get_cluster(rayjob)
            if cluster:
                try:
                    self.store.delete(C.KIND_RAYCLUSTER, cluster["metadata"]["name"], namespace)
                except Exception:
                    pass
            return True
        if policy == "DeleteWorkers":
            return self._delete_workers(rayjob)
        return False

    def _delete_workers(self, rayjob: dict) -> bool:
        cluster = self._get_cluster(rayjob)
        if not cluster:
            return False
        namespace = obj_namespace(rayjob)
        changed = False
        for group in (cluster.get("spec") or {}).get("workerGroupSpecs") or []:
            if int(group.get("replicas", 0) or 0) != 0 or int(group.get("minReplicas", 0) or 0) != 0:
                group["replicas"] = 0
                group["minReplicas"] = 0
                changed = True
        for pod in self.store.list(C.KIND_POD, namespace, label_selector={C.LABEL_CLUSTER: cluster["metadata"]["name"], C.LABEL_NODE_TYPE: C.NODE_TYPE_WORKER}):
            try:
                self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                changed = True
            except Exception:
                pass
        if changed:
            try:
                self.store.update(C.KIND_RAYCLUSTER, cluster)
            except Exception:
                pass
        return changed

    def _ttl_elapsed(self, status: dict, ttl: int) -> bool:
        end = status.get("endTime")
        if not end:
            return False
        return (self.clock.now() - parse_iso(end)).total_seconds() >= ttl

    def _deadline_exceeded(self, rayjob: dict, status: dict, field: str) -> bool:
        seconds = (rayjob.get("spec") or {}).get(field)
        if not seconds:
            return False
        start = status.get("startTime")
        if not start:
            return False
        return (self.clock.now() - parse_iso(start)).total_seconds() > int(seconds)

    # -------------------------------------------------------------------- status
    def _validation_failed(self, rayjob: dict, message: str) -> bool:
        existing = rayjob.get("status") or {}
        if existing.get("reason") == "ValidationFailed" and existing.get("message") == message:
            return False
        self.recorder.warning(rayjob, "InvalidRayJobSpec", message)
        status = deep_copy(existing)
        status["jobDeploymentStatus"] = C.DEPLOYMENT_VALIDATION_FAILED
        status["reason"] = "ValidationFailed"
        status["message"] = message
        status["observedGeneration"] = rayjob["metadata"].get("generation", 1)
        return self._commit(rayjob, status)

    def _set_condition(self, rayjob: dict, status: dict, condition_type: str, condition_status: str, reason: str, message: str) -> None:
        new_status = deep_copy(status)
        proxy = {"metadata": rayjob["metadata"], "status": new_status}
        set_condition(proxy, condition_type, condition_status, reason, message, clock=self.clock)
        status.clear()
        status.update(new_status)

    def _commit(self, rayjob: dict, status: dict) -> bool:
        namespace = obj_namespace(rayjob)
        name = obj_name(rayjob)
        try:
            current = self.store.get(C.KIND_RAYJOB, name, namespace)
        except Exception:
            return False
        status = deep_copy(status)
        status["observedGeneration"] = current["metadata"].get("generation", 1)
        old = deep_copy(current.get("status") or {})
        if canonical_json({k: v for k, v in old.items() if k != "lastUpdateTime"}) == canonical_json({k: v for k, v in status.items() if k != "lastUpdateTime"}):
            return False
        updated = deep_copy(current)
        updated["status"] = status
        self.store.update_status(updated)
        self.metrics.job_status(namespace, name, status.get("jobStatus", ""), status.get("jobDeploymentStatus", ""))
        return True
