"""RayCronJob reconciler (alpha upstream, feature-gated)."""

from __future__ import annotations

from .. import constants as C
from ..models.defaults import apply_defaults
from ..models.meta import deletion_timestamp, name as obj_name, namespace as obj_namespace, owner_reference
from ..models.raycronjob import next_schedule
from ..models.validation import validate_object
from ..util import canonical_json, deep_copy
from .base import Controller


class RayCronJobController(Controller):
    name = "raycronjob"
    kind = C.KIND_RAYCRONJOB

    def reconcile(self, obj: dict) -> bool:
        cronjob = deep_copy(obj)
        namespace = obj_namespace(cronjob)
        name = obj_name(cronjob)
        if deletion_timestamp(cronjob):
            # Owned RayJobs are garbage-collected through ownerReferences.
            return False

        defaulted = apply_defaults(cronjob)
        if canonical_json(defaulted.get("spec")) != canonical_json(cronjob.get("spec")):
            merged = deep_copy(cronjob)
            merged["spec"] = defaulted["spec"]
            self.store.update(C.KIND_RAYCRONJOB, merged)
            return True

        errors = validate_object(defaulted)
        if errors:
            message = "; ".join(errors)
            annotations = defaulted.setdefault("metadata", {}).setdefault("annotations", {})
            if annotations.get("minikuberay.io/last-validation") != message:
                annotations["minikuberay.io/last-validation"] = message
                try:
                    self.store.update(C.KIND_RAYCRONJOB, defaulted)
                except Exception:
                    pass
                self.recorder.warning(defaulted, "InvalidRayCronJobSpec", message)
            return False

        spec = defaulted.get("spec") or {}
        if spec.get("suspend"):
            if not ((defaulted.get("status") or {}).get("suspended")):
                self.recorder.normal(defaulted, "SuspendedRayCronJob", "RayCronJob is suspended; no new RayJobs will be created")
                status = deep_copy(defaulted.get("status") or {})
                status["suspended"] = True
                return self._commit(defaulted, status)
            return False

        status = deep_copy(defaulted.get("status") or {})
        if status.get("suspended"):
            status["suspended"] = False
            self._commit(defaulted, status)
            # Re-read: defaulted is a stale copy and would otherwise re-commit
            # suspended=True together with the next scheduled job.
            try:
                status = deep_copy(self.store.get(C.KIND_RAYCRONJOB, name, namespace).get("status") or {})
            except Exception:
                status = {}

        timezone_name = spec.get("timeZone")
        last = status.get("lastScheduleTime") or defaulted["metadata"].get("creationTimestamp")
        from ..clock import parse_iso

        after = parse_iso(last) if last else self.clock.now()
        try:
            scheduled = next_schedule(spec.get("schedule", ""), after, timezone_name)
        except ValueError as exc:
            message = str(exc)
            # Deduplicate the warning: an unsatisfiable cron must not emit a
            # new random-named Event on every reconcile.
            if status.get("reason") != "InvalidSchedule" or status.get("message") != message:
                self.recorder.warning(defaulted, "InvalidRayCronJobSpec", message)
                status["reason"] = "InvalidSchedule"
                status["message"] = message
                self._commit(defaulted, status)
            return False
        if status.get("reason") == "InvalidSchedule":
            status.pop("reason", None)
            status.pop("message", None)
            self._commit(defaulted, status)
            return False
        if scheduled > self.clock.now():
            return False

        job_name = self._rayjob_name(name, scheduled)
        if not self.store.exists(C.KIND_RAYJOB, job_name, namespace):
            from ..models.meta import new_object

            annotations = {C.ANNOTATION_CRONJOB_TIMESTAMP: scheduled.isoformat().replace("+00:00", "Z")}
            # Carry IPPR through RayCronJob -> RayJob -> RayCluster; each
            # layer only embeds the next layer's spec, not its metadata.
            parent_annotations = (defaulted.get("metadata") or {}).get("annotations") or {}
            if C.ANNOTATION_IPPR in parent_annotations:
                annotations[C.ANNOTATION_IPPR] = parent_annotations[C.ANNOTATION_IPPR]
            job = new_object(
                C.RAY_API_VERSION,
                C.KIND_RAYJOB,
                job_name,
                namespace,
                spec=deep_copy(spec.get("jobTemplate") or {}),
                labels={C.LABEL_CRONJOB_NAME: name},
                annotations=annotations,
                owner_references=[owner_reference(defaulted)],
            )
            try:
                self.store.create(C.KIND_RAYJOB, job, namespace)
                self.recorder.normal(defaulted, "CreatedRayJob", f"Created RayJob {job_name} for schedule {scheduled.isoformat()}")
            except Exception as exc:
                self.recorder.warning(defaulted, "FailedToCreateRayJob", str(exc))
                return False

        status["lastScheduleTime"] = scheduled.isoformat().replace("+00:00", "Z")
        return self._commit(defaulted, status)

    @staticmethod
    def _rayjob_name(cronjob_name: str, scheduled) -> str:
        # Teaching simplification: upstream uses a random suffix plus
        # status.lastScheduleTime for idempotency.  Deriving the name from the
        # scheduled minute makes duplicate reconciles collide with AlreadyExists
        # and keeps tests deterministic.
        return f"{cronjob_name}-{int(scheduled.timestamp()) // 60}"

    def _commit(self, cronjob: dict, status: dict) -> bool:
        namespace = obj_namespace(cronjob)
        name = obj_name(cronjob)
        try:
            current = self.store.get(C.KIND_RAYCRONJOB, name, namespace)
        except Exception:
            return False
        old = current.get("status") or {}
        if canonical_json(old) == canonical_json(status):
            return False
        updated = deep_copy(current)
        updated["status"] = deep_copy(status)
        self.store.update_status(updated)
        return True
