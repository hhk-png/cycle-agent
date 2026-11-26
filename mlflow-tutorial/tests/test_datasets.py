from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from mini_mlflow import cli
from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.datasets import _digest
from mini_mlflow.errors import (
    Conflict,
    InvalidParameter,
    ResourceNotFound,
    SearchSyntaxError,
)
from mini_mlflow.server import create_server
from mini_mlflow.webhooks import EVENT_NAMES


def _records() -> list[dict]:
    return [
        {
            "inputs": {"question": "What is the refund window?", "locale": "en"},
            "expectations": {"answer": "30 days"},
            "tags": {"slice": "policy"},
        },
        {
            "inputs": {"question": "Can I return a used item?", "locale": "en"},
            "expectations": {"answer": "Only if defective"},
            "tags": {},
        },
    ]


def test_create_get_digest_schema_and_profile(service) -> None:
    dataset = service.datasets.create_dataset(
        "support-eval",
        _records(),
        source="s3://warehouse/eval/2026-11-20.jsonl",
        source_type="s3",
        tags={"owner": "cx", "stage": "golden"},
    )
    assert dataset.version == 1
    assert dataset.record_count == 2
    assert dataset.digest.startswith("sha256:")
    assert dataset.schema == {
        "locale": "string",
        "question": "string",
    }
    assert dataset.profile["record_count"] == 2
    assert dataset.profile["input_keys"] == ["locale", "question"]
    assert dataset.profile["expectation_keys"] == ["answer"]
    assert dataset.tags == {"owner": "cx", "stage": "golden"}

    fetched = service.datasets.get_dataset(dataset.dataset_id)
    assert fetched.records == _records()
    assert fetched.digest == dataset.digest

    # The digest is content-addressed: name, tags and source do not change it.
    same = service.datasets.create_dataset("support-eval-copy", _records())
    assert same.digest == dataset.digest
    assert same.dataset_id != dataset.dataset_id


def test_record_validation_rejects_ambiguous_payloads(service) -> None:
    with pytest.raises(InvalidParameter):
        service.datasets.create_dataset("empty", [])
    with pytest.raises(InvalidParameter):
        service.datasets.create_dataset("bad-shape", [{"inputs": []}])
    with pytest.raises(InvalidParameter):
        service.datasets.create_dataset("bad-inputs", [{"inputs": {}}])
    with pytest.raises(InvalidParameter):
        service.datasets.create_dataset(
            "unknown-field", [{"inputs": {"q": "hi"}, "output": "x"}]
        )
    with pytest.raises(InvalidParameter):
        service.datasets.create_dataset(
            "nan", [{"inputs": {"q": float("nan")}}]
        )
    with pytest.raises(InvalidParameter):
        service.datasets.create_dataset(
            "bad-tag", [{"inputs": {"q": "hi"}, "tags": {"": "x"}}]
        )


def test_merge_appends_records_and_increments_version(service) -> None:
    dataset = service.datasets.create_dataset("merge-me", _records())
    first_digest = dataset.digest
    merged = service.datasets.merge_records(
        dataset.dataset_id,
        [{"inputs": {"question": "Where is order A-1?"}, "expectations": {"answer": "shipped"}}],
    )
    assert merged.version == 2
    assert merged.record_count == 3
    assert merged.digest != first_digest
    assert len(service.datasets.get_dataset(dataset.dataset_id).records) == 3
    assert service.datasets.get_dataset(dataset.dataset_id).records[0] == _records()[0]

    with pytest.raises(InvalidParameter):
        service.datasets.merge_records(dataset.dataset_id, [])
    with pytest.raises(ResourceNotFound):
        service.datasets.merge_records("does-not-exist", _records())


def test_search_by_attributes_tags_and_order(service) -> None:
    first = service.datasets.create_dataset(
        "support-a", _records()[:1], tags={"owner": "cx", "stage": "draft"}
    )
    second = service.datasets.create_dataset(
        "support-b", _records()[1:], tags={"owner": "cx", "stage": "golden"}
    )
    other = service.datasets.create_dataset(
        "ranking", _records()[:1], tags={"owner": "search"}
    )

    by_name = service.datasets.search_datasets("attributes.name = 'support-a'")
    assert [item.dataset_id for item in by_name] == [first.dataset_id]

    by_tag = service.datasets.search_datasets("tags.owner = 'cx'")
    assert {item.dataset_id for item in by_tag} == {first.dataset_id, second.dataset_id}

    ordered = service.datasets.search_datasets(
        "tags.owner = 'cx'", order_by=["attributes.name DESC"]
    )
    assert [item.name for item in ordered] == ["support-b", "support-a"]

    limited = service.datasets.search_datasets(max_results=1)
    assert len(limited) == 1
    assert limited[0].records == []  # search returns metadata only
    assert limited[0].record_count == 1

    assert service.datasets.get_dataset(other.dataset_id).records

    with pytest.raises(SearchSyntaxError):
        service.datasets.search_datasets("datasets.name = 'support-a'")
    with pytest.raises(InvalidParameter):
        service.datasets.search_datasets(max_results=0)
    with pytest.raises(InvalidParameter):
        service.datasets.search_datasets(order_by=["records DESC"])


def test_merge_compare_and_swap_rejects_stale_snapshot(service) -> None:
    dataset = service.datasets.create_dataset("cas", _records())
    with pytest.raises(Conflict):
        service.datasets.store.merge_dataset_records(
            dataset.dataset_id,
            [{"inputs": {"q": "late"}, "expectations": {}, "tags": {}}],
            digest="sha256:stale",
            version=2,
            schema={},
            profile={},
            updated=1,
            expected_version=dataset.version + 1,
        )
    # The failed CAS must not have appended a row.
    assert service.datasets.get_dataset(dataset.dataset_id).record_count == 2


def test_concurrent_merges_keep_digest_consistent(service) -> None:
    dataset = service.datasets.create_dataset("concurrent", _records())
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(index: int) -> None:
        try:
            barrier.wait()
            service.datasets.merge_records(
                dataset.dataset_id,
                [{"inputs": {"question": f"concurrent-{index}"}}],
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    final = service.datasets.get_dataset(dataset.dataset_id)
    assert final.record_count == 5
    assert final.version == 4
    assert final.digest == _digest(final.records)


def test_delete_is_not_idempotent(service) -> None:
    dataset = service.datasets.create_dataset("temporary", _records())
    service.datasets.delete_dataset(dataset.dataset_id)
    with pytest.raises(ResourceNotFound):
        service.datasets.get_dataset(dataset.dataset_id)
    with pytest.raises(ResourceNotFound):
        service.datasets.delete_dataset(dataset.dataset_id)


def test_dataset_events_are_registered() -> None:
    assert {"dataset.created", "dataset.records.merged", "dataset.deleted"} <= EVENT_NAMES


def test_dataset_lifecycle_emits_webhook_events(service) -> None:
    calls: list[dict] = []

    def sender(url, body, headers, timeout):  # noqa: ANN001 - test double
        calls.append({"url": url, "body": body, "headers": dict(headers)})
        return 200

    service.webhooks.sender = sender
    service.webhooks.create_webhook(
        "dataset-hook",
        "https://example.test/hooks/dataset",
        ["dataset.created", "dataset.records.merged", "dataset.deleted"],
    )
    dataset = service.datasets.create_dataset("hooked", _records())
    service.datasets.merge_records(
        dataset.dataset_id, [{"inputs": {"question": "another"}}]
    )
    service.datasets.delete_dataset(dataset.dataset_id)
    service.webhooks.drain_once()
    assert [call["headers"]["X-Mlflow-Event"] for call in calls] == [
        "dataset.created",
        "dataset.records.merged",
        "dataset.deleted",
    ]


def test_dataset_record_and_byte_limits(service, monkeypatch) -> None:
    dataset = service.datasets.create_dataset("limits", _records())
    monkeypatch.setattr("mini_mlflow.datasets._MAX_TOTAL_RECORDS", 2)
    with pytest.raises(InvalidParameter):
        service.datasets.merge_records(
            dataset.dataset_id, [{"inputs": {"question": "over-limit"}}]
        )
    monkeypatch.undo()

    monkeypatch.setattr("mini_mlflow.datasets._MAX_RECORDS_PER_MERGE", 1)
    with pytest.raises(InvalidParameter):
        service.datasets.merge_records(dataset.dataset_id, _records())
    monkeypatch.undo()

    dataset = service.datasets.create_dataset("bytes", _records())
    monkeypatch.setattr("mini_mlflow.datasets._MAX_TOTAL_BYTES", 100)
    with pytest.raises(InvalidParameter):
        service.datasets.merge_records(
            dataset.dataset_id, [{"inputs": {"question": "x" * 200}}]
        )


@pytest.fixture()
def running_server(config: MiniConfig):
    server_config = config.derive(auth_token="secret-token")
    server = create_server(server_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_remote_dataset_roundtrip(running_server: str) -> None:
    client = MiniClient(running_server, auth_token="secret-token")
    try:
        created = client.create_dataset(
            "remote-eval",
            _records(),
            source="file:///tmp/eval.jsonl",
            source_type="local",
            tags={"owner": "cx"},
        )
        assert created.record_count == 2
        fetched = client.get_dataset(created.dataset_id)
        assert fetched.records == _records()

        merged = client.merge_dataset_records(
            created.dataset_id,
            [{"inputs": {"question": "Where is my order?"}}],
        )
        assert merged.version == 2
        assert merged.record_count == 3

        found = client.search_datasets("tags.owner = 'cx'")
        assert [item.dataset_id for item in found] == [created.dataset_id]

        client.delete_dataset(created.dataset_id)
        with pytest.raises(ResourceNotFound):
            client.get_dataset(created.dataset_id)
    finally:
        client.close()


def test_cli_dataset_create_list_get(tmp_path: Path, capsys) -> None:
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(_records()), encoding="utf-8")
    uri = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"

    code = cli.main(
        [
            "datasets",
            "create",
            "cli-eval",
            "--records-file",
            str(records_path),
            "--tag",
            "owner=cx",
            "--tracking-uri",
            uri,
        ]
    )
    assert code == 0
    created = json.loads(capsys.readouterr().out)
    assert created["record_count"] == 2
    assert created["name"] == "cli-eval"
    dataset_id = created["dataset_id"]

    code = cli.main(
        ["datasets", "list", "--filter", "tags.owner = 'cx'", "--tracking-uri", uri]
    )
    assert code == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["dataset_id"] for item in listed["datasets"]] == [dataset_id]

    code = cli.main(["datasets", "get", dataset_id, "--tracking-uri", uri])
    assert code == 0
    fetched = json.loads(capsys.readouterr().out)
    assert fetched["records"] == _records()

    code = cli.main(["datasets", "delete", dataset_id, "--tracking-uri", uri])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "deleted"

    # The CLI is a normal client: local artifact roots are configurable, and
    # the database it created must still be usable by a fresh process.
    client = MiniClient(uri)
    try:
        assert client.search_datasets("attributes.name = 'cli-eval'") == []
    finally:
        client.close()
