from __future__ import annotations

import os
from pathlib import Path

import pytest

from mini_mlflow.entities import Trace, Span
from mini_mlflow.errors import InvalidParameter, ResourceAlreadyExists, ResourceNotFound


def test_default_experiment_and_custom_artifact_location(service) -> None:
    assert service.get_experiment("0").name == "Default"
    experiment = service.create_experiment("custom", tags={"team": "x", "owner": "y"})
    assert experiment.experiment_id != "0"
    assert experiment.artifact_location.startswith("file:")
    assert experiment.tags == {"team": "x", "owner": "y"}
    with pytest.raises(ResourceAlreadyExists, match="already exists"):
        service.create_experiment("custom")


def test_experiment_delete_restore_and_default_protection(service) -> None:
    experiment = service.create_experiment("ephemeral")
    run = service.start_run(experiment_id=experiment.experiment_id, run_name="r")
    service.delete_experiment(experiment.experiment_id)
    assert service.get_experiment(experiment.experiment_id).lifecycle_stage == "deleted"
    with pytest.raises(ResourceNotFound):
        service.get_run(run.info.run_id)
    service.restore_experiment(experiment.experiment_id)
    assert service.get_run(run.info.run_id).info.lifecycle_stage == "active"
    with pytest.raises(InvalidParameter, match="Default"):
        service.delete_experiment("0")


def test_run_lifecycle_and_logging_after_end(service) -> None:
    run = service.start_run(run_name="lifecycle")
    rid = run.info.run_id
    assert run.info.status == "RUNNING"
    service.log_param(rid, "x", 1)
    service.log_metric(rid, "score", 0.5)
    service.end_run(rid, "FINISHED")
    with pytest.raises(InvalidParameter, match="logging requires"):
        service.log_metric(rid, "score", 0.6)
    service.end_run(rid, "FINISHED")  # idempotent
    with pytest.raises(InvalidParameter, match="already terminal"):
        service.end_run(rid, "FAILED")
    with pytest.raises(InvalidParameter, match="end status"):
        service.end_run(service.start_run().info.run_id, "UNKNOWN")


def test_resume_running_run(service) -> None:
    run = service.start_run(run_name="resume")
    resumed = service.start_run(run_id=run.info.run_id)
    assert resumed.info.run_id == run.info.run_id
    service.end_run(run.info.run_id)
    with pytest.raises(InvalidParameter, match="cannot resume"):
        service.start_run(run_id=run.info.run_id)


def test_params_are_immutable_but_idempotent(service) -> None:
    run = service.start_run()
    service.log_param(run.info.run_id, "a", 1)
    service.log_param(run.info.run_id, "a", 1)
    with pytest.raises(InvalidParameter, match="immutable"):
        service.log_param(run.info.run_id, "a", 2)


def test_log_batch_is_atomic_on_validation_error(service) -> None:
    run = service.start_run()
    with pytest.raises(InvalidParameter):
        service.log_batch(
            run.info.run_id,
            params={"valid": "yes", "bad": {"nested": 1}},
            metrics=[{"key": "m", "value": 1.0}],
        )
    stored = service.get_run(run.info.run_id)
    assert stored.data.params == {}
    assert stored.data.metrics == {}


def test_metric_history_latest_and_steps(service) -> None:
    run = service.start_run()
    rid = run.info.run_id
    service.log_metric(rid, "loss", 1.0, step=1, timestamp=1000)
    service.log_metric(rid, "loss", 0.5, step=2, timestamp=1000)
    service.log_metric(rid, "loss", 0.25, step=3, timestamp=999)  # older timestamp
    assert service.get_run(rid).data.metrics["loss"] == 0.5
    history = service.get_metric_history(rid, "loss")
    assert [item.value for item in history] == [0.25, 1.0, 0.5]
    with pytest.raises(InvalidParameter):
        service.log_metric(rid, "bad", 1.0, step=-1)


def test_run_name_tag_updates_info(service) -> None:
    run = service.start_run()
    service.set_tag(run.info.run_id, "mlflow.runName", "renamed")
    assert service.get_run(run.info.run_id).info.run_name == "renamed"


def test_tags_can_be_updated_and_deleted(service) -> None:
    run = service.start_run()
    service.set_tag(run.info.run_id, "a", "1")
    service.set_tag(run.info.run_id, "a", "2")
    assert service.get_run(run.info.run_id).data.tags["a"] == "2"
    service.delete_tag(run.info.run_id, "a")
    assert "a" not in service.get_run(run.info.run_id).data.tags


def test_run_input_dataset(service) -> None:
    run = service.start_run()
    service.log_input(
        run.info.run_id,
        dataset_name="train",
        dataset_digest="sha256:abc",
        dataset_source="s3://bucket/data",
        dataset_source_type="s3",
        tags={"version": "1"},
    )
    stored = service.get_run(run.info.run_id)
    assert stored.inputs[0].dataset_digest == "sha256:abc"
    assert stored.inputs[0].tags["version"] == "1"


def test_artifact_lifecycle(service, tmp_path: Path) -> None:
    run = service.start_run()
    rid = run.info.run_id
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    assert service.log_artifact(rid, source, "docs") == "docs/hello.txt"
    assert service.read_artifact(rid, "docs/hello.txt") == b"hello"
    files = service.list_artifacts(rid, "docs")
    assert [(item.path, item.is_dir) for item in files] == [("docs/hello.txt", False)]
    service.log_text(rid, "note", "docs/note.md")
    service.log_dict(rid, {"a": 1}, "docs/config.json")
    assert b'"a": 1' in service.read_artifact(rid, "docs/config.json")
    target = service.download_artifacts(rid, "docs", tmp_path / "downloaded")
    assert (target / "hello.txt").read_text(encoding="utf-8") == "hello"


def test_artifact_path_security(service) -> None:
    run = service.start_run()
    with pytest.raises(InvalidParameter):
        service.log_artifact_bytes(run.info.run_id, "../escape.txt", b"x")
    with pytest.raises(InvalidParameter):
        service.log_artifact_bytes(run.info.run_id, "/abs.txt", b"x")
    with pytest.raises(ResourceNotFound):
        service.read_artifact(run.info.run_id, "missing.txt")


def test_binary_artifact_and_listing(service) -> None:
    run = service.start_run()
    service.log_artifact_bytes(run.info.run_id, "blob.bin", b"\x00\x01")
    assert service.read_artifact(run.info.run_id, "blob.bin") == b"\x00\x01"
    names = {item.path for item in service.list_artifacts(run.info.run_id)}
    assert "blob.bin" in names


def test_invalid_tags_and_params(service) -> None:
    run = service.start_run()
    with pytest.raises(InvalidParameter):
        service.set_tag(run.info.run_id, "", "x")
    with pytest.raises(InvalidParameter):
        service.log_param(run.info.run_id, "p", None)
    with pytest.raises(InvalidParameter):
        service.log_metric(run.info.run_id, "m", float("inf"))


def test_search_invalid_filter_returns_parameter_error(service) -> None:
    with pytest.raises(InvalidParameter):
        service.search_runs("attributes.nope = 1")
    with pytest.raises(InvalidParameter):
        service.search_runs("metrics.acc >")


def test_trace_roundtrip(service) -> None:
    trace = Trace(
        trace_id="a" * 32,
        name="test-trace",
        experiment_id="0",
        tags={"env": "test"},
        spans=[
            Span(
                span_id="1" * 16,
                trace_id="a" * 32,
                name="root",
                start_time_ns=1_000,
                end_time_ns=2_000_000,
                inputs={"q": "hello"},
                outputs={"a": "world"},
            )
        ],
    )
    saved = service.log_trace(trace)
    assert saved.name == "test-trace"
    assert service.get_trace("a" * 32).spans[0].outputs == {"a": "world"}
    assert service.search_traces(filter_string='tags.env = "test"')[0].trace_id == "a" * 32
    assert not service.search_traces(filter_string='status = "ERROR"')


def test_trace_validation(service) -> None:
    with pytest.raises(InvalidParameter, match="at least one span"):
        service.log_trace(Trace(trace_id="b" * 32, name="empty"))
    with pytest.raises(InvalidParameter, match="unknown parent"):
        service.log_trace(
            Trace(
                trace_id="c" * 32,
                spans=[
                    Span(
                        span_id="2" * 16,
                        trace_id="c" * 32,
                        name="child",
                        parent_span_id="f" * 16,
                        start_time_ns=1,
                        end_time_ns=2,
                    )
                ],
            )
        )


def test_update_run_to_terminal_sets_end_time(service) -> None:
    run = service.start_run()
    info = service.update_run(run.info.run_id, status="FINISHED")
    assert info.status == "FINISHED"
    assert info.end_time is not None
    with pytest.raises(InvalidParameter, match="cannot be reopened"):
        service.update_run(run.info.run_id, status="RUNNING")
    with pytest.raises(InvalidParameter, match="logging requires"):
        service.log_metric(run.info.run_id, "late", 1.0)


def test_artifact_path_rejects_platform_hazards(service) -> None:
    run = service.start_run()
    paths = ["a:b.txt"]
    if os.name == "nt":
        paths += ["CON", "nul.txt", "foo/COM1.json"]
    for path in paths:
        with pytest.raises(InvalidParameter):
            service.log_artifact_bytes(run.info.run_id, path, b"x")


def test_trace_payload_limit(service, config) -> None:
    limited = service.config.derive(trace_max_payload_bytes=1024)
    from mini_mlflow.tracking import TrackingService

    other = TrackingService(limited)
    try:
        trace = Trace(
            trace_id="e" * 32,
            name="big",
            experiment_id="0",
            request={"q": "x" * 5000},
            spans=[
                Span(
                    span_id="3" * 16,
                    trace_id="e" * 32,
                    name="root",
                    start_time_ns=1,
                    end_time_ns=2,
                )
            ],
        )
        with pytest.raises(InvalidParameter, match="trace_max_payload_bytes"):
            other.log_trace(trace)
    finally:
        other.close()


def test_concurrent_end_run_has_single_winner(service) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    run = service.start_run()
    barrier = threading.Barrier(2)

    def finish(status: str) -> bool:
        barrier.wait(timeout=5)
        try:
            service.end_run(run.info.run_id, status)
            return True
        except InvalidParameter:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, ["FINISHED", "FAILED"]))
    assert sum(results) == 1
    assert service.get_run(run.info.run_id).info.status in {"FINISHED", "FAILED"}


def test_restore_experiment_does_not_resurrect_individually_deleted_run(service) -> None:
    experiment = service.create_experiment("delete-semantics")
    keep = service.start_run(experiment_id=experiment.experiment_id, run_name="keep")
    dropped = service.start_run(experiment_id=experiment.experiment_id, run_name="dropped")
    service.delete_run(dropped.info.run_id)
    service.delete_experiment(experiment.experiment_id)
    service.restore_experiment(experiment.experiment_id)
    assert service.get_run(keep.info.run_id).info.lifecycle_stage == "active"
    with pytest.raises(ResourceNotFound):
        service.get_run(dropped.info.run_id)


def test_restore_run_rejected_while_experiment_deleted(service) -> None:
    experiment = service.create_experiment("deleted-exp")
    run = service.start_run(experiment_id=experiment.experiment_id)
    service.delete_experiment(experiment.experiment_id)
    with pytest.raises(InvalidParameter, match="experiment is deleted"):
        service.restore_run(run.info.run_id)
    service.restore_experiment(experiment.experiment_id)
    assert service.get_run(run.info.run_id).info.status == "RUNNING"


def test_default_experiment_deletion_variants_blocked(service) -> None:
    for variant in ["0", "00", "+0"]:
        with pytest.raises(InvalidParameter, match="Default"):
            service.delete_experiment(variant)
    assert service.get_experiment("0").lifecycle_stage == "active"


def test_run_name_tag_is_validated(service) -> None:
    run = service.start_run()
    with pytest.raises(InvalidParameter):
        service.set_tag(run.info.run_id, "mlflow.runName", "bad\nname")
    with pytest.raises(InvalidParameter):
        service.set_tag(run.info.run_id, "mlflow.runName", "")
    service.set_tag(run.info.run_id, "mlflow.runName", "valid")
    assert service.get_run(run.info.run_id).info.run_name == "valid"


def test_trace_tag_payload_limit(service) -> None:
    from mini_mlflow.tracking import TrackingService

    limited = TrackingService(service.config.derive(trace_max_payload_bytes=1024))
    try:
        trace = Trace(
            trace_id="9" * 32,
            name="tag-bomb",
            experiment_id="0",
            tags={"big": "x" * 5000},
            spans=[
                Span(
                    span_id="8" * 16,
                    trace_id="9" * 32,
                    name="root",
                    start_time_ns=1,
                    end_time_ns=2,
                )
            ],
        )
        with pytest.raises(InvalidParameter, match="trace.tags"):
            limited.log_trace(trace)
    finally:
        limited.close()


def test_log_batch_rolls_back_on_store_conflict(service) -> None:
    run = service.start_run()
    service.log_param(run.info.run_id, "locked", "one")
    with pytest.raises(InvalidParameter, match="immutable"):
        service.log_batch(
            run.info.run_id,
            metrics=[{"key": "m", "value": 1.0}],
            params={"locked": "two", "new": "x"},
            tags={"batch": "should-not-persist"},
        )
    stored = service.get_run(run.info.run_id)
    assert "m" not in stored.data.metrics
    assert "new" not in stored.data.params
    assert "batch" not in stored.data.tags
