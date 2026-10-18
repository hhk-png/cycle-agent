from __future__ import annotations

from minikuberay import constants as C
from minikuberay.models.meta import new_object
from tests.conftest import Harness, cluster_spec


def cron_spec(schedule="*/5 * * * *", *, timezone="UTC", suspend=False):
    return {
        "schedule": schedule,
        "timeZone": timezone,
        "suspend": suspend,
        "jobTemplate": {
            "entrypoint": "echo scheduled",
            "shutdownAfterJobFinishes": True,
            "rayClusterSpec": cluster_spec(workers=0),
        },
    }


def apply_cron(harness: Harness, spec=None, name="cron"):
    obj = new_object(C.RAY_API_VERSION, C.KIND_RAYCRONJOB, name, "default", spec=spec or cron_spec())
    return harness.store.create(C.KIND_RAYCRONJOB, obj, "default")


def cron_harness():
    return Harness(feature_gates={"RayCronJob": True})


def test_cron_creates_job_on_schedule():
    harness = cron_harness()
    apply_cron(harness)
    harness.step(5, advance=60)
    assert harness.list(C.KIND_RAYJOB) == []
    harness.step(1, advance=60)
    jobs = harness.list(C.KIND_RAYJOB)
    assert len(jobs) == 1
    assert jobs[0]["metadata"]["name"] == "cron-29453765"
    assert jobs[0]["metadata"]["labels"][C.LABEL_CRONJOB_NAME] == "cron"
    assert jobs[0]["metadata"]["annotations"][C.ANNOTATION_CRONJOB_TIMESTAMP] == "2026-01-01T00:05:00Z"
    assert harness.get(C.KIND_RAYCRONJOB, "cron")["status"]["lastScheduleTime"] == "2026-01-01T00:05:00Z"


def test_cron_child_owner_reference():
    harness = cron_harness()
    cron = apply_cron(harness)
    harness.step(6, advance=60)
    job = harness.list(C.KIND_RAYJOB)[0]
    refs = job["metadata"]["ownerReferences"]
    assert refs[0]["kind"] == C.KIND_RAYCRONJOB
    assert refs[0]["uid"] == cron["metadata"]["uid"]
    assert refs[0]["controller"] is True


def test_cron_suspend():
    harness = cron_harness()
    apply_cron(harness, cron_spec(suspend=True))
    harness.step(10, advance=60)
    assert harness.list(C.KIND_RAYJOB) == []
    assert harness.get(C.KIND_RAYCRONJOB, "cron")["status"]["suspended"] is True
    cron = harness.get(C.KIND_RAYCRONJOB, "cron")
    cron["spec"]["suspend"] = False
    harness.store.update(C.KIND_RAYCRONJOB, cron)
    harness.step(8, advance=60)
    assert harness.list(C.KIND_RAYJOB)


def test_cron_timezone():
    harness = cron_harness()
    apply_cron(harness, cron_spec("0 0 * * *", timezone="Asia/Shanghai"))
    harness.step(17, advance=3600)
    jobs = harness.list(C.KIND_RAYJOB)
    assert jobs
    assert harness.get(C.KIND_RAYCRONJOB, "cron")["status"]["lastScheduleTime"] == "2026-01-01T16:00:00Z"


def test_cron_invalid_schedule_emits_event():
    harness = cron_harness()
    apply_cron(harness, cron_spec("not a cron"))
    harness.step(3, advance=60)
    assert harness.list(C.KIND_RAYJOB) == []
    assert any(event["reason"] == "InvalidRayCronJobSpec" for event in harness.list(C.KIND_EVENT))


def test_cron_feature_gate_disabled():
    harness = Harness()
    apply_cron(harness)
    harness.step(10, advance=60)
    assert harness.list(C.KIND_RAYJOB) == []


def test_cron_does_not_duplicate_tick():
    harness = cron_harness()
    apply_cron(harness)
    harness.step(6, advance=60)
    assert len(harness.list(C.KIND_RAYJOB)) == 1
    harness.step(1, advance=0)
    assert len(harness.list(C.KIND_RAYJOB)) == 1


def test_cron_multiple_ticks():
    harness = cron_harness()
    apply_cron(harness)
    harness.step(16, advance=60)
    jobs = harness.list(C.KIND_RAYJOB)
    assert len(jobs) == 3  # 00:05, 00:10, 00:15
    assert harness.get(C.KIND_RAYCRONJOB, "cron")["status"]["lastScheduleTime"] == "2026-01-01T00:15:00Z"
