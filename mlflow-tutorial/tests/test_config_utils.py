from __future__ import annotations

from pathlib import Path

import pytest

from mini_mlflow.config import MiniConfig, load_config
from mini_mlflow.errors import InvalidParameter
from mini_mlflow.utils import (
    coerce_metric,
    coerce_param_value,
    decode_page_token,
    encode_page_token,
    file_uri_to_path,
    json_safe,
    parse_pairs,
    path_to_file_uri,
    redact,
    safe_join,
    validate_metric_step,
    validate_name,
)


def test_config_paths_are_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = MiniConfig(
        backend_store_uri="sqlite:///relative/mlflow.db",
        artifact_root="./artifacts",
    )
    assert config.backend_path.is_absolute()
    assert config.artifact_root_path.is_absolute()
    assert config.backend_path.is_relative_to(tmp_path)
    config.ensure_directories()
    assert config.backend_path.parent.is_dir()
    assert config.artifact_root_path.is_dir()


def test_tracking_uri_roundtrip(tmp_path: Path) -> None:
    database = (tmp_path / "roundtrip.db").as_posix()
    config = MiniConfig(backend_store_uri=f"sqlite:///{database}", artifact_root=str(tmp_path / "a"))
    assert config.backend_path == Path(database).resolve()
    assert config.tracking_uri.startswith("sqlite:///")
    second = MiniConfig.from_env(backend_store_uri=config.tracking_uri, artifact_root=str(tmp_path / "a"))
    assert second.backend_path == config.backend_path


def test_file_uri_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "a b" / "artifact.txt"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    uri = path_to_file_uri(target)
    assert uri.startswith("file:")
    assert file_uri_to_path(uri) == target.resolve()


def test_config_rejects_remote_backend() -> None:
    with pytest.raises(InvalidParameter, match="SQLite"):
        MiniConfig(backend_store_uri="postgresql://localhost/mlflow")


def test_config_rejects_remote_artifact_store() -> None:
    with pytest.raises(InvalidParameter, match="local-only"):
        MiniConfig(artifact_root="s3://bucket/artifacts")


def test_config_env_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_MLFLOW_BACKEND_STORE_URI", "sqlite:///env.db")
    monkeypatch.setenv("MLFLOW_BACKEND_STORE_URI", "sqlite:///fallback.db")
    monkeypatch.setenv("MINI_MLFLOW_ARTIFACT_ROOT", str(tmp_path / "env-artifacts"))
    config = MiniConfig.from_env()
    assert config.backend_path.name == "env.db"
    assert config.artifact_root_path == (tmp_path / "env-artifacts").resolve()


def test_config_file_and_override(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "backend_store_uri: sqlite:///file.db\nartifact_root: artifacts\nport: 5010\n",
        encoding="utf-8",
    )
    config = load_config(path, port=5020)
    assert config.port == 5020
    assert config.backend_path.name == "file.db"
    assert config.artifact_root_path.name == "artifacts"


def test_config_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("unknown: true\n", encoding="utf-8")
    with pytest.raises(InvalidParameter, match="unknown config keys"):
        load_config(path)


@pytest.mark.parametrize("port", [-1, 70000, "bad"])
def test_config_invalid_port(port) -> None:
    with pytest.raises(InvalidParameter):
        MiniConfig(port=port)


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for value in ["../x", "a/../../x", "/etc/passwd", "C:/Windows", "a\x00b"]:
        with pytest.raises(InvalidParameter):
            safe_join(root, value)
    assert safe_join(root, "a/b.txt") == (root / "a" / "b.txt").resolve()


def test_safe_join_allows_dot_names(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert safe_join(root, "model/MLmodel").name == "MLmodel"
    assert safe_join(root, ".hidden").name == ".hidden"


def test_page_token_roundtrip() -> None:
    token = encode_page_token(40)
    assert decode_page_token(token) == 40
    assert decode_page_token(None) == 0
    with pytest.raises(InvalidParameter):
        decode_page_token("not-base64")


def test_coercions_reject_nan_and_structures() -> None:
    assert coerce_metric("0.5") == 0.5
    with pytest.raises(InvalidParameter):
        coerce_metric(float("nan"))
    with pytest.raises(InvalidParameter):
        coerce_metric(True)
    with pytest.raises(InvalidParameter):
        coerce_param_value({"not": "scalar"})
    assert coerce_param_value(True) == "true"
    with pytest.raises(InvalidParameter):
        validate_metric_step(-1)


def test_validate_name() -> None:
    assert validate_name("  hello  ") == "hello"
    with pytest.raises(InvalidParameter):
        validate_name("")
    with pytest.raises(InvalidParameter):
        validate_name("a\nb")


def test_parse_pairs_both_shapes() -> None:
    assert parse_pairs({"a": 1}, what="tags") == {"a": "1"}
    assert parse_pairs([{"key": "a", "value": 2}], what="tags") == {"a": "2"}
    with pytest.raises(InvalidParameter):
        parse_pairs("bad", what="tags")


def test_redact_and_json_safe() -> None:
    data = {"api_key": "secret", "nested": {"password": "pw", "x": 1}, "obj": object()}
    redacted = redact(data)
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["nested"]["password"] == "***REDACTED***"
    safe = json_safe(redacted)
    assert safe["nested"]["x"] == 1
    assert safe["obj"].startswith("<object")


def test_windows_file_uri_artifact_root() -> None:
    import os

    if os.name != "nt":
        pytest.skip("Windows-specific URI spelling")
    config = MiniConfig(artifact_root="file:///D:/data/artifacts", port=0)
    assert str(config.artifact_root_path).lower().startswith("d:")
    config2 = MiniConfig(artifact_root="file://D:/data/artifacts", port=0)
    assert str(config2.artifact_root_path).lower().startswith("d:")
    with pytest.raises(InvalidParameter):
        MiniConfig(artifact_root="file://server/share/artifacts", port=0)


def test_in_memory_sqlite_rejected() -> None:
    with pytest.raises(InvalidParameter, match="in-memory"):
        MiniConfig(backend_store_uri="sqlite:///:memory:")


def test_schema_v1_database_is_migrated(tmp_path: Path) -> None:
    import sqlite3

    from mini_mlflow.store.sqlite_store import SCHEMA_VERSION, SqliteStore

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES ('version', '1');
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY,
          experiment_id INTEGER NOT NULL,
          run_name TEXT NOT NULL DEFAULT '',
          user_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          start_time INTEGER NOT NULL,
          end_time INTEGER,
          artifact_uri TEXT NOT NULL,
          lifecycle_stage TEXT NOT NULL DEFAULT 'active',
          deleted_time INTEGER
        );
        """
    )
    connection.commit()
    connection.close()
    store = SqliteStore(path, default_artifact_location=(tmp_path / "artifacts").as_uri())
    try:
        columns = {row["name"] for row in store._connection().execute("PRAGMA table_info(runs)")}
        assert "deleted_with_experiment" in columns
        version = store._connection().execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()["value"]
        assert version == SCHEMA_VERSION == "7"
        tables = {
            row["name"]
            for row in store._connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "webhooks",
            "webhook_deliveries",
            "datasets",
            "dataset_records",
            "monitors",
            "monitor_windows",
        } <= tables
    finally:
        store.close()
