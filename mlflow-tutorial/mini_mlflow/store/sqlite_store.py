"""SQLite persistence layer.

This module deliberately knows nothing about HTTP or models.  It translates
rows into domain entities and is the only place that contains SQL.  The
service layer owns validation and lifecycle rules; putting both here would
make the business rules impossible to test without a database.

Concurrency model
-----------------
* One SQLite connection per thread (connections are cheap; sharing one is not
  safe by default).
* WAL mode for concurrent readers + a single writer.
* Short transactions, ``busy_timeout``, and no long-running read transactions.
* The schema is initialized with ``CREATE TABLE IF NOT EXISTS`` and a
  ``schema_meta`` version row.  A real deployment should use numbered
  migrations rather than mutating an existing schema in place.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..entities import (
    Assessment,
    Dataset,
    Experiment,
    FileInfo,
    LoggedModel,
    Metric,
    ModelVersion,
    Monitor,
    MonitorWindow,
    PromptVersion,
    RegisteredModel,
    Run,
    RunData,
    RunInfo,
    RunInput,
    Span,
    Trace,
    Webhook,
    WebhookDelivery,
)
from ..errors import (
    Conflict,
    InvalidParameter,
    ResourceAlreadyExists,
    ResourceNotFound,
)
from ..utils import chunked, compact_json, now_ms

SCHEMA_VERSION = "7"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class SqliteStore:
    def __init__(
        self,
        path: str | Path,
        *,
        default_artifact_location: str,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_artifact_location = default_artifact_location
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection and schema management
    # ------------------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self.busy_timeout_ms / 1000,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        try:
            # BEGIN IMMEDIATE serializes writers before the business check and
            # the write. Without it, a SELECT (e.g. "is the run terminal?")
            # can run outside the transaction and another writer can end the
            # run before our INSERT commits.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            columns = {item["name"] for item in conn.execute("PRAGMA table_info(runs)")}
            if "deleted_with_experiment" not in columns:
                conn.execute(
                    "ALTER TABLE runs ADD COLUMN deleted_with_experiment INTEGER NOT NULL DEFAULT 0"
                )
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
                    (SCHEMA_VERSION,),
                )
            elif row["value"] in {"1", "2", "3", "4", "5", "6"}:
                # v1 -> v2: remember which runs were deleted by an experiment
                # deletion so restoring the experiment does not resurrect runs
                # deleted individually.
                # v2 -> v3: add assessments + prompt registry tables.
                # v3 -> v4: add webhooks + webhook_deliveries (outbox).
                # v4 -> v5: add logged_models + tags/params/metrics tables.
                # v5 -> v6: add evaluation datasets + dataset_records.
                # v6 -> v7: add monitors + monitor_windows.
                # The CREATE TABLE IF NOT EXISTS statements above are the whole
                # migration; existing rows are untouched.
                conn.execute("UPDATE schema_meta SET value=? WHERE key='version'", (SCHEMA_VERSION,))
            elif row["value"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {row['value']} is not supported by this build "
                    f"(expected {SCHEMA_VERSION})"
                )
            conn.execute(
                "INSERT OR IGNORE INTO experiments"
                " (experiment_id, name, artifact_location, lifecycle_stage, creation_time, last_update_time)"
                " VALUES (0, 'Default', ?, 'active', ?, ?)",
                (self.default_artifact_location, now_ms(), now_ms()),
            )

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------
    @staticmethod
    def _experiment_from_row(row: sqlite3.Row, tags: dict[str, str] | None = None) -> Experiment:
        return Experiment(
            experiment_id=str(row["experiment_id"]),
            name=row["name"],
            artifact_location=row["artifact_location"],
            lifecycle_stage=row["lifecycle_stage"],
            creation_time=row["creation_time"],
            last_update_time=row["last_update_time"],
            tags=tags or {},
        )

    @staticmethod
    def _run_info_from_row(row: sqlite3.Row) -> RunInfo:
        return RunInfo(
            run_id=row["run_id"],
            experiment_id=str(row["experiment_id"]),
            run_name=row["run_name"],
            user_id=row["user_id"],
            status=row["status"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            artifact_uri=row["artifact_uri"],
            lifecycle_stage=row["lifecycle_stage"],
        )

    def _experiment_tags(self, experiment_id: str | int) -> dict[str, str]:
        rows = self._connection().execute(
            "SELECT key, value FROM experiment_tags WHERE experiment_id=? ORDER BY key",
            (experiment_id,),
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------
    def create_experiment(
        self,
        name: str,
        artifact_location: str,
        *,
        creation_time: int | None = None,
        tags: dict[str, str] | None = None,
    ) -> Experiment:
        timestamp = creation_time or now_ms()
        with self.transaction() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO experiments(name, artifact_location, lifecycle_stage, creation_time, last_update_time)"
                    " VALUES (?, ?, 'active', ?, ?)",
                    (name, artifact_location, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ResourceAlreadyExists(f"experiment with name {name!r} already exists") from exc
                raise
            experiment_id = str(cursor.lastrowid)
            for key, value in (tags or {}).items():
                conn.execute(
                    "INSERT INTO experiment_tags(experiment_id, key, value) VALUES (?, ?, ?)",
                    (experiment_id, key, value),
                )
        return self.get_experiment(experiment_id, include_deleted=True)

    def get_experiment(self, experiment_id: str, *, include_deleted: bool = True) -> Experiment:
        row = self._connection().execute(
            "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if row is None or (row["lifecycle_stage"] == "deleted" and not include_deleted):
            raise ResourceNotFound(f"experiment {experiment_id!r} not found")
        return self._experiment_from_row(row, self._experiment_tags(experiment_id))

    def get_experiment_by_name(self, name: str, *, include_deleted: bool = True) -> Experiment | None:
        row = self._connection().execute(
            "SELECT * FROM experiments WHERE name=?", (name,)
        ).fetchone()
        if row is None or (row["lifecycle_stage"] == "deleted" and not include_deleted):
            return None
        return self._experiment_from_row(row, self._experiment_tags(row["experiment_id"]))

    def list_experiments(self, *, include_deleted: bool = False) -> list[Experiment]:
        clause = "" if include_deleted else "WHERE lifecycle_stage='active'"
        rows = self._connection().execute(
            f"SELECT * FROM experiments {clause} ORDER BY experiment_id"
        ).fetchall()
        return [self._experiment_from_row(row, self._experiment_tags(row["experiment_id"])) for row in rows]

    def update_experiment(
        self,
        experiment_id: str,
        *,
        name: str | None = None,
        artifact_location: str | None = None,
    ) -> Experiment:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"experiment {experiment_id!r} not found")
            new_name = name if name is not None else row["name"]
            new_location = row["artifact_location"] if artifact_location is None else artifact_location
            try:
                conn.execute(
                    "UPDATE experiments SET name=?, artifact_location=?, last_update_time=?"
                    " WHERE experiment_id=?",
                    (new_name, new_location, now_ms(), experiment_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ResourceAlreadyExists(f"experiment with name {new_name!r} already exists") from exc
        return self.get_experiment(experiment_id, include_deleted=True)

    def set_experiment_tag(self, experiment_id: str, key: str, value: str) -> None:
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if not exists:
                raise ResourceNotFound(f"experiment {experiment_id!r} not found")
            conn.execute(
                "INSERT INTO experiment_tags(experiment_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(experiment_id, key) DO UPDATE SET value=excluded.value",
                (experiment_id, key, value),
            )
            conn.execute(
                "UPDATE experiments SET last_update_time=? WHERE experiment_id=?",
                (now_ms(), experiment_id),
            )

    def delete_experiment(self, experiment_id: str) -> None:
        try:
            if int(str(experiment_id)) == 0:
                raise InvalidParameter("the Default experiment cannot be deleted")
        except (TypeError, ValueError) as exc:
            raise InvalidParameter("experiment_id must be an integer") from exc
        timestamp = now_ms()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"experiment {experiment_id!r} not found")
            conn.execute(
                "UPDATE experiments SET lifecycle_stage='deleted', last_update_time=? WHERE experiment_id=?",
                (timestamp, experiment_id),
            )
            conn.execute(
                "UPDATE runs SET lifecycle_stage='deleted', deleted_time=?, deleted_with_experiment=1"
                " WHERE experiment_id=? AND lifecycle_stage='active'",
                (timestamp, experiment_id),
            )

    def restore_experiment(self, experiment_id: str) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"experiment {experiment_id!r} not found")
            conn.execute(
                "UPDATE experiments SET lifecycle_stage='active', last_update_time=? WHERE experiment_id=?",
                (now_ms(), experiment_id),
            )
            conn.execute(
                "UPDATE runs SET lifecycle_stage='active', deleted_time=NULL, deleted_with_experiment=0"
                " WHERE experiment_id=? AND lifecycle_stage='deleted' AND deleted_with_experiment=1",
                (experiment_id,),
            )

    def search_experiment_rows(
        self, where_sql: str, params: Sequence[Any], order_sql: str, limit: int, offset: int
    ) -> list[sqlite3.Row]:
        sql = f"SELECT e.* FROM experiments e WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        return self._connection().execute(sql, (*params, limit, offset)).fetchall()

    # ------------------------------------------------------------------
    # Runs: creation, reads, lifecycle
    # ------------------------------------------------------------------
    def create_run(
        self,
        *,
        run_id: str,
        experiment_id: str,
        run_name: str,
        user_id: str,
        artifact_uri: str,
        status: str = "RUNNING",
        start_time: int | None = None,
        tags: dict[str, str] | None = None,
    ) -> Run:
        timestamp = start_time or now_ms()
        with self.transaction() as conn:
            exp = conn.execute(
                "SELECT lifecycle_stage FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if exp is None:
                raise ResourceNotFound(f"experiment {experiment_id!r} not found")
            if exp["lifecycle_stage"] != "active":
                raise InvalidParameter("cannot create a run in a deleted experiment")
            try:
                conn.execute(
                    "INSERT INTO runs(run_id, experiment_id, run_name, user_id, status, start_time,"
                    " end_time, artifact_uri, lifecycle_stage)"
                    " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'active')",
                    (run_id, experiment_id, run_name, user_id, status, timestamp, artifact_uri),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ResourceAlreadyExists(f"run {run_id!r} already exists") from exc
                raise
            for key, value in (tags or {}).items():
                conn.execute(
                    "INSERT INTO run_tags(run_id, key, value) VALUES (?, ?, ?)",
                    (run_id, key, value),
                )
        return self.get_run(run_id, include_deleted=True)

    def get_run_row(self, run_id: str) -> sqlite3.Row:
        row = self._connection().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ResourceNotFound(f"run {run_id!r} not found")
        return row

    def get_run(self, run_id: str, *, include_deleted: bool = False) -> Run:
        row = self.get_run_row(run_id)
        if row["lifecycle_stage"] == "deleted" and not include_deleted:
            raise ResourceNotFound(f"run {run_id!r} not found (deleted)")
        info = self._run_info_from_row(row)
        data = self._run_data([run_id]).get(run_id, RunData())
        inputs = self.get_run_inputs(run_id)
        return Run(info=info, data=data, inputs=inputs)

    def _run_data(self, run_ids: Sequence[str]) -> dict[str, RunData]:
        result = {run_id: RunData() for run_id in run_ids}
        if not run_ids:
            return result
        conn = self._connection()
        for batch in chunked(list(run_ids), 400):
            placeholders = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT run_id, key, value FROM run_params WHERE run_id IN ({placeholders})",
                batch,
            ):
                result[row["run_id"]].params[row["key"]] = row["value"]
            for row in conn.execute(
                f"SELECT run_id, key, value FROM run_tags WHERE run_id IN ({placeholders})",
                batch,
            ):
                result[row["run_id"]].tags[row["key"]] = row["value"]
            for row in conn.execute(
                f"""
                SELECT run_id, key, value FROM (
                    SELECT run_id, key, value, timestamp, id,
                           ROW_NUMBER() OVER (
                               PARTITION BY run_id, key
                               ORDER BY timestamp DESC, id DESC
                           ) AS rn
                    FROM metrics
                    WHERE run_id IN ({placeholders})
                ) WHERE rn = 1
                """,
                batch,
            ):
                result[row["run_id"]].metrics[row["key"]] = row["value"]
        return result

    def end_run(self, run_id: str, status: str, end_time: int) -> RunInfo:
        """Atomically move a running run to a terminal state.

        The conditional UPDATE closes the read-then-write race: only one
        caller can win. A lost race is resolved by reading the winner's status
        and either accepting an identical terminal state (idempotent) or
        raising a conflict.
        """
        terminal = ("FINISHED", "FAILED", "KILLED")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, lifecycle_stage FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None or row["lifecycle_stage"] == "deleted":
                raise ResourceNotFound(f"run {run_id!r} not found")
            if row["status"] in terminal:
                if row["status"] == status:
                    return self._run_info_from_row(self.get_run_row(run_id))
                raise InvalidParameter(
                    f"run {run_id} is already terminal as {row['status']}; cannot change it to {status}"
                )
            cursor = conn.execute(
                "UPDATE runs SET status=?, end_time=? WHERE run_id=? AND status NOT IN (?,?,?)",
                (status, end_time, run_id, *terminal),
            )
            if cursor.rowcount == 0:
                current = conn.execute(
                    "SELECT status FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if current and current["status"] == status:
                    return self._run_info_from_row(self.get_run_row(run_id))
                raise InvalidParameter("run status changed concurrently")
        return self._run_info_from_row(self.get_run_row(run_id))

    def update_run(
        self,
        run_id: str,
        *,
        run_name: str | None = None,
        status: str | None = None,
        end_time: int | None = None,
    ) -> RunInfo:
        if status in {"FINISHED", "FAILED", "KILLED"}:
            raise InvalidParameter("terminal statuses must go through end_run()")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ResourceNotFound(f"run {run_id!r} not found")
            new_name = row["run_name"] if run_name is None else run_name
            new_status = row["status"] if status is None else status
            new_end = row["end_time"] if end_time is None else end_time
            conn.execute(
                "UPDATE runs SET run_name=?, status=?, end_time=? WHERE run_id=?",
                (new_name, new_status, new_end, run_id),
            )
            if run_name is not None:
                conn.execute(
                    "INSERT INTO run_tags(run_id, key, value) VALUES (?, 'mlflow.runName', ?)"
                    " ON CONFLICT(run_id, key) DO UPDATE SET value=excluded.value",
                    (run_id, run_name),
                )
        return self._run_info_from_row(self.get_run_row(run_id))

    def delete_run(self, run_id: str) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ResourceNotFound(f"run {run_id!r} not found")
            conn.execute(
                "UPDATE runs SET lifecycle_stage='deleted', deleted_time=?, deleted_with_experiment=0"
                " WHERE run_id=?",
                (now_ms(), run_id),
            )

    def restore_run(self, run_id: str) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT r.run_id, e.lifecycle_stage AS experiment_stage"
                " FROM runs r JOIN experiments e ON e.experiment_id=r.experiment_id"
                " WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"run {run_id!r} not found")
            if row["experiment_stage"] == "deleted":
                raise InvalidParameter("cannot restore a run while its experiment is deleted")
            conn.execute(
                "UPDATE runs SET lifecycle_stage='active', deleted_time=NULL,"
                " deleted_with_experiment=0 WHERE run_id=?",
                (run_id,),
            )

    # ------------------------------------------------------------------
    # Run data
    # ------------------------------------------------------------------
    def log_batch(
        self,
        run_id: str,
        *,
        metrics: Sequence[tuple[str, float, int, int]],
        params: dict[str, str],
        tags: dict[str, str],
    ) -> None:
        """Persist one batch atomically.

        ``metrics`` items are ``(key, value, timestamp, step)``.  The service
        layer validates values and run state; the store still enforces the
        parameter immutability invariant to protect against future callers.
        """
        with self.transaction() as conn:
            state = conn.execute(
                "SELECT status, lifecycle_stage FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if state is None or state["lifecycle_stage"] == "deleted":
                raise ResourceNotFound(f"run {run_id!r} not found")
            if state["status"] in {"FINISHED", "FAILED", "KILLED"}:
                raise InvalidParameter(
                    f"run {run_id} is {state['status']}; logging requires a running run"
                )
            for key, value in params.items():
                row = conn.execute(
                    "SELECT value FROM run_params WHERE run_id=? AND key=?", (run_id, key)
                ).fetchone()
                if row is not None and row["value"] != value:
                    raise InvalidParameter(
                        f"parameter {key!r} already has value {row['value']!r}; parameters are immutable"
                    )
                if row is None:
                    conn.execute(
                        "INSERT INTO run_params(run_id, key, value) VALUES (?, ?, ?)",
                        (run_id, key, value),
                    )
            for key, value, timestamp, step in metrics:
                conn.execute(
                    "INSERT INTO metrics(run_id, key, value, timestamp, step) VALUES (?, ?, ?, ?, ?)",
                    (run_id, key, value, timestamp, step),
                )
            for key, value in tags.items():
                conn.execute(
                    "INSERT INTO run_tags(run_id, key, value) VALUES (?, ?, ?)"
                    " ON CONFLICT(run_id, key) DO UPDATE SET value=excluded.value",
                    (run_id, key, value),
                )

    def get_metric_history(self, run_id: str, key: str) -> list[Metric]:
        rows = self._connection().execute(
            "SELECT key, value, timestamp, step FROM metrics"
            " WHERE run_id=? AND key=? ORDER BY timestamp, id",
            (run_id, key),
        ).fetchall()
        return [Metric.from_dict(dict(row)) for row in rows]

    def get_latest_metrics(self, run_id: str) -> dict[str, float]:
        return self._run_data([run_id]).get(run_id, RunData()).metrics

    def delete_run_tag(self, run_id: str, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM run_tags WHERE run_id=? AND key=?", (run_id, key))

    def log_run_input(self, run_id: str, run_input: RunInput) -> None:
        with self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not exists:
                raise ResourceNotFound(f"run {run_id!r} not found")
            conn.execute(
                "INSERT INTO run_inputs(run_id, dataset_name, dataset_digest, dataset_source,"
                " dataset_source_type, context, tags_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    run_input.dataset_name,
                    run_input.dataset_digest,
                    run_input.dataset_source,
                    run_input.dataset_source_type,
                    run_input.context,
                    compact_json(run_input.tags),
                ),
            )

    def get_run_inputs(self, run_id: str) -> list[RunInput]:
        rows = self._connection().execute(
            "SELECT * FROM run_inputs WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            RunInput(
                dataset_name=row["dataset_name"],
                dataset_digest=row["dataset_digest"],
                dataset_source=row["dataset_source"],
                dataset_source_type=row["dataset_source_type"],
                context=row["context"],
                tags=json.loads(row["tags_json"] or "{}"),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search_runs(
        self,
        where_sql: str,
        params: Sequence[Any],
        order_sql: str,
        order_params: Sequence[Any],
        limit: int,
        offset: int,
    ) -> list[Run]:
        sql = f"SELECT r.* FROM runs r WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        rows = self._connection().execute(
            sql, (*params, *order_params, limit, offset)
        ).fetchall()
        infos = [self._run_info_from_row(row) for row in rows]
        data = self._run_data([info.run_id for info in infos])
        inputs = self._run_inputs_many([info.run_id for info in infos])
        return [
            Run(
                info=info,
                data=data.get(info.run_id, RunData()),
                inputs=inputs.get(info.run_id, []),
            )
            for info in infos
        ]

    def _run_inputs_many(self, run_ids: Sequence[str]) -> dict[str, list[RunInput]]:
        result: dict[str, list[RunInput]] = {run_id: [] for run_id in run_ids}
        if not run_ids:
            return result
        for batch in chunked(list(run_ids), 400):
            placeholders = ",".join("?" for _ in batch)
            for row in self._connection().execute(
                f"SELECT * FROM run_inputs WHERE run_id IN ({placeholders}) ORDER BY id",
                batch,
            ):
                result[row["run_id"]].append(
                    RunInput(
                        dataset_name=row["dataset_name"],
                        dataset_digest=row["dataset_digest"],
                        dataset_source=row["dataset_source"],
                        dataset_source_type=row["dataset_source_type"],
                        context=row["context"],
                        tags=json.loads(row["tags_json"] or "{}"),
                    )
                )
        return result

    # ------------------------------------------------------------------
    # Registered models
    # ------------------------------------------------------------------
    def _registered_model_from_row(self, row: sqlite3.Row) -> RegisteredModel:
        tags = {
            tag["key"]: tag["value"]
            for tag in self._connection().execute(
                "SELECT key, value FROM registered_model_tags WHERE name=?", (row["name"],)
            )
        }
        aliases = {
            alias["alias"]: alias["version"]
            for alias in self._connection().execute(
                "SELECT alias, version FROM model_version_aliases WHERE name=?", (row["name"],)
            )
        }
        return RegisteredModel(
            name=row["name"],
            creation_timestamp=row["creation_timestamp"],
            last_updated_timestamp=row["last_updated_timestamp"],
            description=row["description"],
            tags=tags,
            aliases=aliases,
        )

    def create_registered_model(self, name: str, *, description: str = "", tags: dict[str, str] | None = None) -> RegisteredModel:
        timestamp = now_ms()
        with self.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO registered_models(name, creation_timestamp, last_updated_timestamp, description)"
                    " VALUES (?, ?, ?, ?)",
                    (name, timestamp, timestamp, description),
                )
            except sqlite3.IntegrityError as exc:
                raise ResourceAlreadyExists(f"registered model {name!r} already exists") from exc
            for key, value in (tags or {}).items():
                conn.execute(
                    "INSERT INTO registered_model_tags(name, key, value) VALUES (?, ?, ?)",
                    (name, key, value),
                )
        return self.get_registered_model(name)

    def get_registered_model(self, name: str) -> RegisteredModel:
        row = self._connection().execute(
            "SELECT * FROM registered_models WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"registered model {name!r} not found")
        return self._registered_model_from_row(row)

    def list_registered_models(self) -> list[RegisteredModel]:
        rows = self._connection().execute(
            "SELECT * FROM registered_models ORDER BY name"
        ).fetchall()
        return [self._registered_model_from_row(row) for row in rows]

    def update_registered_model(
        self, name: str, *, new_name: str | None = None, description: str | None = None
    ) -> RegisteredModel:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM registered_models WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"registered model {name!r} not found")
            target_name = new_name or name
            target_description = row["description"] if description is None else description
            try:
                conn.execute(
                    "UPDATE registered_models SET name=?, description=?, last_updated_timestamp=?"
                    " WHERE name=?",
                    (target_name, target_description, now_ms(), name),
                )
            except sqlite3.IntegrityError as exc:
                raise ResourceAlreadyExists(f"registered model {target_name!r} already exists") from exc
        return self.get_registered_model(target_name)

    def delete_registered_model(self, name: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM registered_models WHERE name=?", (name,))
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"registered model {name!r} not found")

    def set_registered_model_tag(self, name: str, key: str, value: str) -> None:
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM registered_models WHERE name=?", (name,)).fetchone():
                raise ResourceNotFound(f"registered model {name!r} not found")
            conn.execute(
                "INSERT INTO registered_model_tags(name, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(name, key) DO UPDATE SET value=excluded.value",
                (name, key, value),
            )
            conn.execute(
                "UPDATE registered_models SET last_updated_timestamp=? WHERE name=?",
                (now_ms(), name),
            )

    def delete_registered_model_tag(self, name: str, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM registered_model_tags WHERE name=? AND key=?", (name, key))

    # ------------------------------------------------------------------
    # Model versions
    # ------------------------------------------------------------------
    def _model_version_from_row(self, row: sqlite3.Row, aliases: list[str] | None = None) -> ModelVersion:
        tags = {
            tag["key"]: tag["value"]
            for tag in self._connection().execute(
                "SELECT key, value FROM model_version_tags WHERE name=? AND version=?",
                (row["name"], row["version"]),
            )
        }
        if aliases is None:
            aliases = [
                item["alias"]
                for item in self._connection().execute(
                    "SELECT alias FROM model_version_aliases WHERE name=? AND version=? ORDER BY alias",
                    (row["name"], row["version"]),
                )
            ]
        return ModelVersion(
            name=row["name"],
            version=row["version"],
            creation_timestamp=row["creation_timestamp"],
            last_updated_timestamp=row["last_updated_timestamp"],
            description=row["description"],
            source=row["source"],
            run_id=row["run_id"],
            status=row["status"],
            stage=row["current_stage"],
            tags=tags,
            aliases=aliases,
        )

    def create_model_version(
        self,
        name: str,
        *,
        source: str,
        run_id: str = "",
        description: str = "",
        tags: dict[str, str] | None = None,
        status: str = "READY",
    ) -> ModelVersion:
        timestamp = now_ms()
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM registered_models WHERE name=?", (name,)).fetchone():
                raise ResourceNotFound(f"registered model {name!r} not found")
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version FROM model_versions WHERE name=?",
                (name,),
            ).fetchone()
            version = int(row["max_version"]) + 1
            conn.execute(
                "INSERT INTO model_versions(name, version, creation_timestamp, last_updated_timestamp,"
                " description, source, run_id, status, current_stage)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'None')",
                (name, version, timestamp, timestamp, description, source, run_id, status),
            )
            for key, value in (tags or {}).items():
                conn.execute(
                    "INSERT INTO model_version_tags(name, version, key, value) VALUES (?, ?, ?, ?)",
                    (name, version, key, value),
                )
        return self.get_model_version(name, version)

    def get_model_version(self, name: str, version: int | str) -> ModelVersion:
        try:
            parsed_version = int(version)
        except (TypeError, ValueError) as exc:
            raise InvalidParameter("version must be an integer") from exc
        row = self._connection().execute(
            "SELECT * FROM model_versions WHERE name=? AND version=?", (name, parsed_version)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"model version {name!r} v{parsed_version} not found")
        return self._model_version_from_row(row)

    def list_model_versions(self, name: str) -> list[ModelVersion]:
        rows = self._connection().execute(
            "SELECT * FROM model_versions WHERE name=? ORDER BY version DESC", (name,)
        ).fetchall()
        aliases_by_version: dict[int, list[str]] = {}
        for alias in self._connection().execute(
            "SELECT alias, version FROM model_version_aliases WHERE name=? ORDER BY alias", (name,)
        ):
            aliases_by_version.setdefault(alias["version"], []).append(alias["alias"])
        return [self._model_version_from_row(row, aliases_by_version.get(row["version"], [])) for row in rows]

    def list_all_model_versions(self) -> list[ModelVersion]:
        names = [row["name"] for row in self._connection().execute("SELECT name FROM registered_models")]
        result: list[ModelVersion] = []
        for name in names:
            result.extend(self.list_model_versions(name))
        return result

    def update_model_version(
        self, name: str, version: int, *, description: str | None = None
    ) -> ModelVersion:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM model_versions WHERE name=? AND version=?", (name, version)
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"model version {name!r} v{version} not found")
            conn.execute(
                "UPDATE model_versions SET description=?, last_updated_timestamp=?"
                " WHERE name=? AND version=?",
                (row["description"] if description is None else description, now_ms(), name, version),
            )
        return self.get_model_version(name, version)

    def update_model_version_stage(self, name: str, version: int, stage: str) -> ModelVersion:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM model_versions WHERE name=? AND version=?", (name, version)
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"model version {name!r} v{version} not found")
            conn.execute(
                "UPDATE model_versions SET current_stage=?, last_updated_timestamp=?"
                " WHERE name=? AND version=?",
                (stage, now_ms(), name, version),
            )
        return self.get_model_version(name, version)

    def delete_model_version(self, name: str, version: int) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM model_versions WHERE name=? AND version=?", (name, version)
            )
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"model version {name!r} v{version} not found")

    def set_model_version_tag(self, name: str, version: int, key: str, value: str) -> None:
        with self.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM model_versions WHERE name=? AND version=?", (name, version)
            ).fetchone():
                raise ResourceNotFound(f"model version {name!r} v{version} not found")
            conn.execute(
                "INSERT INTO model_version_tags(name, version, key, value) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name, version, key) DO UPDATE SET value=excluded.value",
                (name, version, key, value),
            )
            conn.execute(
                "UPDATE model_versions SET last_updated_timestamp=? WHERE name=? AND version=?",
                (now_ms(), name, version),
            )

    def delete_model_version_tag(self, name: str, version: int, key: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM model_version_tags WHERE name=? AND version=? AND key=?",
                (name, version, key),
            )

    def set_model_alias(self, name: str, alias: str, version: int) -> None:
        with self.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM model_versions WHERE name=? AND version=?", (name, version)
            ).fetchone():
                raise ResourceNotFound(f"model version {name!r} v{version} not found")
            conn.execute(
                "INSERT INTO model_version_aliases(name, alias, version) VALUES (?, ?, ?)"
                " ON CONFLICT(name, alias) DO UPDATE SET version=excluded.version",
                (name, alias, version),
            )
            conn.execute(
                "UPDATE registered_models SET last_updated_timestamp=? WHERE name=?",
                (now_ms(), name),
            )

    def delete_model_alias(self, name: str, alias: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM model_version_aliases WHERE name=? AND alias=?", (name, alias)
            )

    def get_model_version_by_alias(self, name: str, alias: str) -> ModelVersion:
        row = self._connection().execute(
            "SELECT version FROM model_version_aliases WHERE name=? AND alias=?", (name, alias)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"alias {alias!r} does not exist for registered model {name!r}")
        return self.get_model_version(name, row["version"])

    # ------------------------------------------------------------------
    # Logged models (MLflow 3)
    # ------------------------------------------------------------------
    @staticmethod
    def _logged_model_from_row(
        row: sqlite3.Row,
        *,
        tags: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        metrics: dict[str, Metric] | None = None,
    ) -> LoggedModel:
        return LoggedModel(
            model_id=row["model_id"],
            experiment_id=str(row["experiment_id"]),
            name=row["name"],
            artifact_location=row["artifact_location"],
            creation_timestamp=int(row["creation_timestamp"]),
            last_updated_timestamp=int(row["last_updated_timestamp"]),
            model_type=row["model_type"],
            source_run_id=row["source_run_id"],
            status=row["status"],
            status_message=row["status_message"],
            tags=tags or {},
            params=params or {},
            metrics=metrics or {},
        )

    def _logged_model_data(
        self, model_ids: Sequence[str]
    ) -> dict[str, tuple[dict[str, str], dict[str, str], dict[str, Metric]]]:
        result = {model_id: ({}, {}, {}) for model_id in model_ids}
        if not model_ids:
            return result
        conn = self._connection()
        for batch in chunked(list(model_ids), 400):
            placeholders = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT model_id, key, value FROM logged_model_tags"
                f" WHERE model_id IN ({placeholders})",
                batch,
            ):
                result[row["model_id"]][0][row["key"]] = row["value"]
            for row in conn.execute(
                f"SELECT model_id, key, value FROM logged_model_params"
                f" WHERE model_id IN ({placeholders})",
                batch,
            ):
                result[row["model_id"]][1][row["key"]] = row["value"]
            for row in conn.execute(
                f"""
                SELECT model_id, key, value, timestamp, step FROM (
                    SELECT model_id, key, value, timestamp, step, id,
                           ROW_NUMBER() OVER (
                               PARTITION BY model_id, key
                               ORDER BY step DESC, timestamp DESC, id DESC
                           ) AS rn
                    FROM logged_model_metrics
                    WHERE model_id IN ({placeholders})
                ) WHERE rn = 1
                """,
                batch,
            ):
                result[row["model_id"]][2][row["key"]] = Metric(
                    key=row["key"],
                    value=row["value"],
                    timestamp=row["timestamp"],
                    step=row["step"],
                )
        return result

    def create_logged_model(self, model: LoggedModel) -> LoggedModel:
        with self.transaction() as conn:
            experiment = conn.execute(
                "SELECT lifecycle_stage FROM experiments WHERE experiment_id=?",
                (model.experiment_id,),
            ).fetchone()
            if experiment is None:
                raise ResourceNotFound(f"experiment {model.experiment_id!r} not found")
            if experiment["lifecycle_stage"] != "active":
                raise InvalidParameter("cannot create a logged model in a deleted experiment")
            if model.source_run_id is not None:
                run = conn.execute(
                    "SELECT experiment_id, lifecycle_stage FROM runs WHERE run_id=?",
                    (model.source_run_id,),
                ).fetchone()
                if run is None:
                    raise ResourceNotFound(f"source run {model.source_run_id!r} not found")
                if run["lifecycle_stage"] == "deleted":
                    raise InvalidParameter("source run is deleted")
                if str(run["experiment_id"]) != str(model.experiment_id):
                    raise InvalidParameter("source run belongs to a different experiment")
            try:
                conn.execute(
                    "INSERT INTO logged_models(model_id, experiment_id, name, model_type,"
                    " source_run_id, status, status_message, artifact_location,"
                    " creation_timestamp, last_updated_timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        model.model_id,
                        model.experiment_id,
                        model.name,
                        model.model_type,
                        model.source_run_id,
                        model.status,
                        model.status_message,
                        model.artifact_location,
                        model.creation_timestamp,
                        model.last_updated_timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ResourceAlreadyExists(
                        f"logged model {model.model_id!r} already exists"
                    ) from exc
                raise
            for key, value in model.tags.items():
                conn.execute(
                    "INSERT INTO logged_model_tags(model_id, key, value) VALUES (?, ?, ?)",
                    (model.model_id, key, value),
                )
            for key, value in model.params.items():
                conn.execute(
                    "INSERT INTO logged_model_params(model_id, key, value) VALUES (?, ?, ?)",
                    (model.model_id, key, value),
                )
        return self.get_logged_model(model.model_id)

    def get_logged_model_row(self, model_id: str) -> sqlite3.Row:
        row = self._connection().execute(
            "SELECT lm.*, e.lifecycle_stage AS experiment_lifecycle"
            " FROM logged_models lm JOIN experiments e"
            " ON e.experiment_id = lm.experiment_id WHERE lm.model_id=?",
            (model_id,),
        ).fetchone()
        if row is None or row["experiment_lifecycle"] == "deleted":
            raise ResourceNotFound(f"logged model {model_id!r} not found")
        return row

    def get_logged_model(self, model_id: str) -> LoggedModel:
        row = self.get_logged_model_row(model_id)
        tags, params, metrics = self._logged_model_data([model_id])[model_id]
        return self._logged_model_from_row(
            row, tags=tags, params=params, metrics=metrics
        )

    @staticmethod
    def _active_logged_model_row(conn: sqlite3.Connection, model_id: str) -> sqlite3.Row:
        """Return the row only when the owning experiment is active.

        Every mutation must use this check **inside** its transaction.  A
        post-commit ``get_logged_model`` is not enough: the write would have
        already committed and then the 404 would be a lie.
        """
        row = conn.execute(
            "SELECT lm.* FROM logged_models lm JOIN experiments e"
            " ON e.experiment_id = lm.experiment_id"
            " WHERE lm.model_id=? AND e.lifecycle_stage='active'",
            (model_id,),
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"logged model {model_id!r} not found")
        return row

    def list_logged_models(self) -> list[LoggedModel]:
        rows = self._connection().execute(
            "SELECT lm.* FROM logged_models lm JOIN experiments e"
            " ON e.experiment_id = lm.experiment_id"
            " WHERE e.lifecycle_stage = 'active'"
            " ORDER BY lm.creation_timestamp DESC, lm.model_id ASC"
        ).fetchall()
        data = self._logged_model_data([row["model_id"] for row in rows])
        return [
            self._logged_model_from_row(
                row,
                tags=data[row["model_id"]][0],
                params=data[row["model_id"]][1],
                metrics=data[row["model_id"]][2],
            )
            for row in rows
        ]

    def search_logged_models(
        self,
        where_sql: str,
        params: Sequence[Any],
        order_sql: str,
        order_params: Sequence[Any],
        limit: int,
        offset: int,
    ) -> list[LoggedModel]:
        sql = (
            "SELECT lm.* FROM logged_models lm JOIN experiments e"
            " ON e.experiment_id = lm.experiment_id"
            f" WHERE e.lifecycle_stage = 'active' AND ({where_sql})"
            f" ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )
        rows = self._connection().execute(
            sql, (*params, *order_params, limit, offset)
        ).fetchall()
        data = self._logged_model_data([row["model_id"] for row in rows])
        return [
            self._logged_model_from_row(
                row,
                tags=data[row["model_id"]][0],
                params=data[row["model_id"]][1],
                metrics=data[row["model_id"]][2],
            )
            for row in rows
        ]

    def finalize_logged_model(
        self, model_id: str, status: str, status_message: str, updated: int
    ) -> LoggedModel:
        with self.transaction() as conn:
            row = self._active_logged_model_row(conn, model_id)
            if row["status"] != status and row["status"] != "LOGGED_MODEL_PENDING":
                raise InvalidParameter(
                    f"logged model {model_id} is already finalized as {row['status']}"
                )
            conn.execute(
                "UPDATE logged_models SET status=?, status_message=?,"
                " last_updated_timestamp=? WHERE model_id=?",
                (status, status_message, updated, model_id),
            )
        return self.get_logged_model(model_id)

    def set_logged_model_tag(
        self, model_id: str, key: str, value: str, updated: int
    ) -> None:
        with self.transaction() as conn:
            self._active_logged_model_row(conn, model_id)
            conn.execute(
                "INSERT INTO logged_model_tags(model_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(model_id, key) DO UPDATE SET value=excluded.value",
                (model_id, key, value),
            )
            conn.execute(
                "UPDATE logged_models SET last_updated_timestamp=? WHERE model_id=?",
                (updated, model_id),
            )

    def delete_logged_model_tag(self, model_id: str, key: str, updated: int) -> None:
        with self.transaction() as conn:
            self._active_logged_model_row(conn, model_id)
            cursor = conn.execute(
                "DELETE FROM logged_model_tags WHERE model_id=? AND key=?",
                (model_id, key),
            )
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"tag {key!r} not found on logged model {model_id}")
            conn.execute(
                "UPDATE logged_models SET last_updated_timestamp=? WHERE model_id=?",
                (updated, model_id),
            )

    def log_logged_model_params(
        self, model_id: str, params: dict[str, str], updated: int
    ) -> LoggedModel:
        with self.transaction() as conn:
            self._active_logged_model_row(conn, model_id)
            for key, value in params.items():
                row = conn.execute(
                    "SELECT value FROM logged_model_params WHERE model_id=? AND key=?",
                    (model_id, key),
                ).fetchone()
                if row is not None and row["value"] != value:
                    raise InvalidParameter(
                        f"logged model parameter {key!r} is immutable "
                        f"(existing value {row['value']!r}, new value {value!r})"
                    )
                if row is None:
                    conn.execute(
                        "INSERT INTO logged_model_params(model_id, key, value)"
                        " VALUES (?, ?, ?)",
                        (model_id, key, value),
                    )
            conn.execute(
                "UPDATE logged_models SET last_updated_timestamp=? WHERE model_id=?",
                (updated, model_id),
            )
        return self.get_logged_model(model_id)

    def log_logged_model_metrics(
        self,
        model_id: str,
        metrics: Sequence[Metric],
        updated: int,
    ) -> LoggedModel:
        with self.transaction() as conn:
            self._active_logged_model_row(conn, model_id)
            for metric in metrics:
                conn.execute(
                    "INSERT INTO logged_model_metrics(model_id, key, value, timestamp, step)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (model_id, metric.key, metric.value, metric.timestamp, metric.step),
                )
            conn.execute(
                "UPDATE logged_models SET last_updated_timestamp=? WHERE model_id=?",
                (updated, model_id),
            )
        return self.get_logged_model(model_id)

    def delete_logged_model(self, model_id: str) -> None:
        with self.transaction() as conn:
            self._active_logged_model_row(conn, model_id)
            cursor = conn.execute(
                "DELETE FROM logged_models WHERE model_id=?", (model_id,)
            )
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"logged model {model_id!r} not found")

    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------
    def log_trace(self, trace: Trace) -> None:
        if not trace.trace_id:
            raise InvalidParameter("trace_id must not be empty")
        payload = (
            trace.trace_id,
            trace.experiment_id,
            trace.name,
            trace.timestamp,
            trace.status,
            compact_json(trace.tags),
            compact_json(trace.request_metadata),
            None if trace.request is None else compact_json(trace.request),
            None if trace.response is None else compact_json(trace.response),
            trace.request_metadata.get("user", ""),
        )
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO traces(trace_id, experiment_id, name, timestamp, status, tags_json,"
                " request_metadata_json, request_json, response_json, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(trace_id) DO UPDATE SET"
                " experiment_id=excluded.experiment_id, name=excluded.name, timestamp=excluded.timestamp,"
                " status=excluded.status, tags_json=excluded.tags_json,"
                " request_metadata_json=excluded.request_metadata_json,"
                " request_json=excluded.request_json, response_json=excluded.response_json",
                payload,
            )
            conn.execute("DELETE FROM spans WHERE trace_id=?", (trace.trace_id,))
            for span in trace.spans:
                if span.trace_id and span.trace_id != trace.trace_id:
                    raise InvalidParameter("span trace_id does not match parent trace")
                conn.execute(
                    "INSERT INTO spans(trace_id, span_id, parent_span_id, name, span_type,"
                    " start_time_ns, end_time_ns, status, inputs_json, outputs_json,"
                    " attributes_json, events_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        trace.trace_id,
                        span.span_id,
                        span.parent_span_id,
                        span.name,
                        span.span_type,
                        span.start_time_ns,
                        span.end_time_ns,
                        span.status,
                        None if span.inputs is None else compact_json(span.inputs),
                        None if span.outputs is None else compact_json(span.outputs),
                        compact_json(span.attributes),
                        compact_json(span.events),
                    ),
                )

    def _trace_from_row(
        self, row: sqlite3.Row, assessments: list[Assessment] | None = None
    ) -> Trace:
        spans = [
            Span(
                span_id=span["span_id"],
                trace_id=span["trace_id"],
                parent_span_id=span["parent_span_id"],
                name=span["name"],
                span_type=span["span_type"],
                start_time_ns=span["start_time_ns"],
                end_time_ns=span["end_time_ns"],
                status=span["status"],
                inputs=json.loads(span["inputs_json"]) if span["inputs_json"] else None,
                outputs=json.loads(span["outputs_json"]) if span["outputs_json"] else None,
                attributes=json.loads(span["attributes_json"] or "{}"),
                events=json.loads(span["events_json"] or "[]"),
            )
            for span in self._connection().execute(
                "SELECT * FROM spans WHERE trace_id=? ORDER BY start_time_ns, rowid",
                (row["trace_id"],),
            )
        ]
        return Trace(
            trace_id=row["trace_id"],
            name=row["name"],
            experiment_id=str(row["experiment_id"]),
            timestamp=row["timestamp"],
            status=row["status"],
            tags=json.loads(row["tags_json"] or "{}"),
            request_metadata=json.loads(row["request_metadata_json"] or "{}"),
            request=json.loads(row["request_json"]) if row["request_json"] else None,
            response=json.loads(row["response_json"]) if row["response_json"] else None,
            spans=spans,
            assessments=(
                assessments
                if assessments is not None
                else self.list_assessments(row["trace_id"])
            ),
        )

    def get_trace(self, trace_id: str) -> Trace:
        row = self._connection().execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
        if row is None:
            raise ResourceNotFound(f"trace {trace_id!r} not found")
        return self._trace_from_row(row)

    def list_traces(
        self,
        *,
        experiment_ids: Sequence[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Trace]:
        if experiment_ids:
            placeholders = ",".join("?" for _ in experiment_ids)
            rows = self._connection().execute(
                f"SELECT * FROM traces WHERE experiment_id IN ({placeholders})"
                " ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (*experiment_ids, limit, offset),
            ).fetchall()
        else:
            rows = self._connection().execute(
                "SELECT * FROM traces ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        assessments = self._assessments_many([row["trace_id"] for row in rows])
        return [
            self._trace_from_row(row, assessments.get(row["trace_id"], []))
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Assessments (feedback / expectations)
    # ------------------------------------------------------------------
    @staticmethod
    def _assessment_from_row(row: sqlite3.Row) -> Assessment:
        return Assessment(
            assessment_id=row["assessment_id"],
            trace_id=row["trace_id"],
            name=row["name"],
            value=json.loads(row["value_json"]),
            source_type=row["source_type"],
            source_id=row["source_id"],
            rationale=row["rationale"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            is_expectation=bool(row["is_expectation"]),
            valid=bool(row["valid"]),
            create_time=row["create_time"],
            last_update_time=row["last_update_time"],
        )

    def _assessments_many(self, trace_ids: Sequence[str]) -> dict[str, list[Assessment]]:
        result: dict[str, list[Assessment]] = {trace_id: [] for trace_id in trace_ids}
        if not trace_ids:
            return result
        for batch in chunked(list(trace_ids), 400):
            placeholders = ",".join("?" for _ in batch)
            for row in self._connection().execute(
                f"SELECT * FROM assessments WHERE trace_id IN ({placeholders})"
                " ORDER BY name, source_type, source_id, create_time",
                batch,
            ):
                result[row["trace_id"]].append(self._assessment_from_row(row))
        return result

    def upsert_assessment(self, assessment: Assessment) -> Assessment:
        with self.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM traces WHERE trace_id=?", (assessment.trace_id,)
            ).fetchone():
                raise ResourceNotFound(f"trace {assessment.trace_id!r} not found")
            existing = conn.execute(
                "SELECT assessment_id, create_time FROM assessments"
                " WHERE trace_id=? AND name=? AND source_type=? AND source_id=? AND is_expectation=?",
                (
                    assessment.trace_id,
                    assessment.name,
                    assessment.source_type,
                    assessment.source_id,
                    int(assessment.is_expectation),
                ),
            ).fetchone()
            if existing is not None:
                assessment.assessment_id = existing["assessment_id"]
                assessment.create_time = existing["create_time"]
                conn.execute(
                    "UPDATE assessments SET value_json=?, value_type=?, rationale=?,"
                    " metadata_json=?, valid=?, last_update_time=? WHERE assessment_id=?",
                    (
                        json.dumps(assessment.value, ensure_ascii=False, sort_keys=True),
                        type(assessment.value).__name__,
                        assessment.rationale,
                        compact_json(assessment.metadata),
                        int(assessment.valid),
                        assessment.last_update_time,
                        assessment.assessment_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO assessments(assessment_id, trace_id, name, value_json, value_type,"
                    " source_type, source_id, rationale, metadata_json, is_expectation, valid,"
                    " create_time, last_update_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        assessment.assessment_id,
                        assessment.trace_id,
                        assessment.name,
                        json.dumps(assessment.value, ensure_ascii=False, sort_keys=True),
                        type(assessment.value).__name__,
                        assessment.source_type,
                        assessment.source_id,
                        assessment.rationale,
                        compact_json(assessment.metadata),
                        int(assessment.is_expectation),
                        int(assessment.valid),
                        assessment.create_time,
                        assessment.last_update_time,
                    ),
                )
        return self.get_assessment(assessment.assessment_id)

    def get_assessment(self, assessment_id: str) -> Assessment:
        row = self._connection().execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"assessment {assessment_id!r} not found")
        return self._assessment_from_row(row)

    def list_assessments(self, trace_id: str) -> list[Assessment]:
        rows = self._connection().execute(
            "SELECT * FROM assessments WHERE trace_id=?"
            " ORDER BY name, source_type, source_id, create_time",
            (trace_id,),
        ).fetchall()
        return [self._assessment_from_row(row) for row in rows]

    def delete_assessment(self, assessment_id: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM assessments WHERE assessment_id=?", (assessment_id,)
            )
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"assessment {assessment_id!r} not found")

    # ------------------------------------------------------------------
    # Prompt registry
    # ------------------------------------------------------------------
    @staticmethod
    def _prompt_version_from_row(
        row: sqlite3.Row, aliases: list[str] | None = None
    ) -> PromptVersion:
        return PromptVersion(
            name=row["name"],
            version=row["version"],
            template=row["template"],
            commit_message=row["commit_message"],
            creation_timestamp=row["creation_timestamp"],
            tags=json.loads(row["tags_json"] or "{}"),
            aliases=aliases or [],
        )

    def create_prompt_version(
        self,
        name: str,
        template: str,
        *,
        commit_message: str = "",
        tags: dict[str, str] | None = None,
    ) -> PromptVersion:
        timestamp = now_ms()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO prompts(name, creation_timestamp, last_updated_timestamp)"
                " VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET"
                " last_updated_timestamp=excluded.last_updated_timestamp",
                (name, timestamp, timestamp),
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version FROM prompt_versions WHERE name=?",
                (name,),
            ).fetchone()
            version = int(row["max_version"]) + 1
            conn.execute(
                "INSERT INTO prompt_versions(name, version, template, commit_message,"
                " creation_timestamp, tags_json) VALUES (?, ?, ?, ?, ?, ?)",
                (name, version, template, commit_message, timestamp, compact_json(tags or {})),
            )
        return self.get_prompt_version(name, version)

    def get_prompt_version(self, name: str, version: int | str) -> PromptVersion:
        try:
            parsed = int(version)
        except (TypeError, ValueError) as exc:
            raise InvalidParameter("prompt version must be an integer") from exc
        row = self._connection().execute(
            "SELECT * FROM prompt_versions WHERE name=? AND version=?", (name, parsed)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"prompt {name!r} version {parsed} not found")
        aliases = [
            item["alias"]
            for item in self._connection().execute(
                "SELECT alias FROM prompt_aliases WHERE name=? AND version=? ORDER BY alias",
                (name, parsed),
            )
        ]
        return self._prompt_version_from_row(row, aliases)

    def get_latest_prompt_version(self, name: str) -> PromptVersion:
        row = self._connection().execute(
            "SELECT version FROM prompt_versions WHERE name=? ORDER BY version DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"prompt {name!r} has no versions")
        return self.get_prompt_version(name, row["version"])

    def list_prompt_versions(self, name: str) -> list[PromptVersion]:
        rows = self._connection().execute(
            "SELECT * FROM prompt_versions WHERE name=? ORDER BY version DESC", (name,)
        ).fetchall()
        aliases: dict[int, list[str]] = {}
        for item in self._connection().execute(
            "SELECT alias, version FROM prompt_aliases WHERE name=? ORDER BY alias", (name,)
        ):
            aliases.setdefault(item["version"], []).append(item["alias"])
        return [
            self._prompt_version_from_row(row, aliases.get(row["version"], []))
            for row in rows
        ]

    def list_prompt_names(self) -> list[str]:
        return [
            row["name"]
            for row in self._connection().execute("SELECT name FROM prompts ORDER BY name")
        ]

    def get_prompt_row(self, name: str) -> sqlite3.Row:
        row = self._connection().execute(
            "SELECT * FROM prompts WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"prompt {name!r} not found")
        return row

    def set_prompt_alias(self, name: str, alias: str, version: int) -> None:
        with self.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM prompt_versions WHERE name=? AND version=?", (name, version)
            ).fetchone():
                raise ResourceNotFound(f"prompt {name!r} version {version} not found")
            conn.execute(
                "INSERT INTO prompt_aliases(name, alias, version) VALUES (?, ?, ?)"
                " ON CONFLICT(name, alias) DO UPDATE SET version=excluded.version",
                (name, alias, version),
            )
            conn.execute(
                "UPDATE prompts SET last_updated_timestamp=? WHERE name=?", (now_ms(), name)
            )

    def delete_prompt_alias(self, name: str, alias: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM prompt_aliases WHERE name=? AND alias=?", (name, alias)
            )
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"alias {alias!r} does not exist for prompt {name!r}")

    def get_prompt_version_by_alias(self, name: str, alias: str) -> PromptVersion:
        row = self._connection().execute(
            "SELECT version FROM prompt_aliases WHERE name=? AND alias=?", (name, alias)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"alias {alias!r} does not exist for prompt {name!r}")
        return self.get_prompt_version(name, row["version"])

    def delete_prompt(self, name: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM prompts WHERE name=?", (name,))
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"prompt {name!r} not found")

    # ------------------------------------------------------------------
    # Evaluation datasets
    # ------------------------------------------------------------------
    @staticmethod
    def _dataset_from_row(
        row: sqlite3.Row, records: list[dict[str, Any]] | None = None
    ) -> Dataset:
        return Dataset(
            dataset_id=row["dataset_id"],
            name=row["name"],
            version=row["version"],
            digest=row["digest"],
            source=row["source"],
            source_type=row["source_type"],
            schema=json.loads(row["schema_json"] or "{}"),
            profile=json.loads(row["profile_json"] or "{}"),
            tags=json.loads(row["tags_json"] or "{}"),
            records=list(records or []),
            record_count=row["record_count"],
            created_time=row["created_time"],
            last_updated_time=row["last_updated_time"],
        )

    def create_dataset(self, dataset: Dataset) -> Dataset:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO datasets(dataset_id, name, version, digest, source, source_type,"
                " schema_json, profile_json, tags_json, record_count, created_time,"
                " last_updated_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset.dataset_id,
                    dataset.name,
                    dataset.version,
                    dataset.digest,
                    dataset.source,
                    dataset.source_type,
                    compact_json(dataset.schema),
                    compact_json(dataset.profile),
                    compact_json(dataset.tags),
                    dataset.record_count,
                    dataset.created_time,
                    dataset.last_updated_time,
                ),
            )
            for index, record in enumerate(dataset.records, start=1):
                conn.execute(
                    "INSERT INTO dataset_records(dataset_id, record_id, record_json)"
                    " VALUES (?, ?, ?)",
                    (dataset.dataset_id, index, compact_json(record)),
                )
        return self.get_dataset(dataset.dataset_id)

    def get_dataset(self, dataset_id: str) -> Dataset:
        row = self._connection().execute(
            "SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"dataset {dataset_id!r} not found")
        records = [
            json.loads(item["record_json"])
            for item in self._connection().execute(
                "SELECT record_json FROM dataset_records WHERE dataset_id=?"
                " ORDER BY record_id",
                (dataset_id,),
            )
        ]
        return self._dataset_from_row(row, records)

    def list_datasets(self) -> list[Dataset]:
        """Return metadata only; records are loaded by :meth:`get_dataset`."""
        rows = self._connection().execute(
            "SELECT * FROM datasets ORDER BY created_time DESC, name, dataset_id"
        ).fetchall()
        return [self._dataset_from_row(row) for row in rows]

    def merge_dataset_records(
        self,
        dataset_id: str,
        records: Sequence[dict[str, Any]],
        *,
        digest: str,
        version: int,
        schema: dict[str, str],
        profile: dict[str, Any],
        updated: int,
        expected_version: int,
    ) -> Dataset:
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT version FROM datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if current is None:
                raise ResourceNotFound(f"dataset {dataset_id!r} not found")
            # Compare-and-swap: the digest/schema/profile were derived from a
            # snapshot at ``expected_version``.  If another writer merged in
            # between, those derived fields would be stale, so the service
            # retries from a fresh read instead of committing a wrong digest.
            if int(current["version"]) != expected_version:
                raise Conflict(
                    f"dataset {dataset_id!r} changed from version "
                    f"{expected_version} to {current['version']}; retry the merge"
                )
            row = conn.execute(
                "SELECT COALESCE(MAX(record_id), 0) AS max_record FROM dataset_records"
                " WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
            next_record_id = int(row["max_record"]) + 1
            for offset, record in enumerate(records):
                conn.execute(
                    "INSERT INTO dataset_records(dataset_id, record_id, record_json)"
                    " VALUES (?, ?, ?)",
                    (dataset_id, next_record_id + offset, compact_json(record)),
                )
            conn.execute(
                "UPDATE datasets SET version=?, digest=?, schema_json=?, profile_json=?,"
                " record_count=record_count+?, last_updated_time=? WHERE dataset_id=?",
                (
                    version,
                    digest,
                    compact_json(schema),
                    compact_json(profile),
                    len(records),
                    updated,
                    dataset_id,
                ),
            )
        return self.get_dataset(dataset_id)

    def delete_dataset(self, dataset_id: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM datasets WHERE dataset_id=?", (dataset_id,))
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"dataset {dataset_id!r} not found")

    # ------------------------------------------------------------------
    # Model monitoring
    # ------------------------------------------------------------------
    @staticmethod
    def _monitor_from_row(row: sqlite3.Row) -> Monitor:
        return Monitor(
            monitor_id=row["monitor_id"],
            name=row["name"],
            model_name=row["model_name"],
            model_version=row["model_version"],
            baseline=json.loads(row["baseline_json"] or "{}"),
            thresholds=json.loads(row["thresholds_json"] or "{}"),
            tags=json.loads(row["tags_json"] or "{}"),
            baseline_dataset_id=row["baseline_dataset_id"],
            baseline_dataset_digest=row["baseline_dataset_digest"],
            created_time=row["created_time"],
            last_updated_time=row["last_updated_time"],
        )

    @staticmethod
    def _monitor_window_from_row(row: sqlite3.Row) -> MonitorWindow:
        return MonitorWindow(
            window_id=row["window_id"],
            monitor_id=row["monitor_id"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            sample_count=row["sample_count"],
            status=row["status"],
            metrics=json.loads(row["metrics_json"] or "{}"),
            report=json.loads(row["report_json"] or "{}"),
            created_time=row["created_time"],
        )

    def create_monitor(self, monitor: Monitor) -> Monitor:
        with self.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO monitors(monitor_id, name, model_name, model_version,"
                    " baseline_json, thresholds_json, tags_json, baseline_dataset_id,"
                    " baseline_dataset_digest, created_time, last_updated_time)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        monitor.monitor_id,
                        monitor.name,
                        monitor.model_name,
                        monitor.model_version,
                        compact_json(monitor.baseline),
                        compact_json(monitor.thresholds),
                        compact_json(monitor.tags),
                        monitor.baseline_dataset_id,
                        monitor.baseline_dataset_digest,
                        monitor.created_time,
                        monitor.last_updated_time,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ResourceAlreadyExists(
                        f"monitor named {monitor.name!r} already exists"
                    ) from exc
                raise
        return self.get_monitor(monitor.monitor_id)

    def get_monitor(self, monitor_id: str) -> Monitor:
        row = self._connection().execute(
            "SELECT * FROM monitors WHERE monitor_id=?", (monitor_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"monitor {monitor_id!r} not found")
        return self._monitor_from_row(row)

    def list_monitors(self) -> list[Monitor]:
        rows = self._connection().execute(
            "SELECT * FROM monitors ORDER BY created_time DESC, name, monitor_id"
        ).fetchall()
        return [self._monitor_from_row(row) for row in rows]

    def delete_monitor(self, monitor_id: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM monitors WHERE monitor_id=?", (monitor_id,)
            )
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"monitor {monitor_id!r} not found")

    def create_monitor_window(self, window: MonitorWindow) -> MonitorWindow:
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM monitors WHERE monitor_id=?", (window.monitor_id,)
            ).fetchone()
            if exists is None:
                raise ResourceNotFound(f"monitor {window.monitor_id!r} not found")
            conn.execute(
                "INSERT INTO monitor_windows(window_id, monitor_id, window_start,"
                " window_end, sample_count, status, metrics_json, report_json,"
                " created_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    window.window_id,
                    window.monitor_id,
                    window.window_start,
                    window.window_end,
                    window.sample_count,
                    window.status,
                    compact_json(window.metrics),
                    compact_json(window.report),
                    window.created_time,
                ),
            )
        return self.get_monitor_window(window.window_id)

    def get_monitor_window(self, window_id: str) -> MonitorWindow:
        row = self._connection().execute(
            "SELECT * FROM monitor_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"monitor window {window_id!r} not found")
        return self._monitor_window_from_row(row)

    def list_monitor_windows(
        self, monitor_id: str, *, limit: int = 100
    ) -> list[MonitorWindow]:
        # Check existence explicitly so an empty history is distinguishable
        # from a typo in the monitor id.
        self.get_monitor(monitor_id)
        rows = self._connection().execute(
            "SELECT * FROM monitor_windows WHERE monitor_id=?"
            " ORDER BY window_end DESC, created_time DESC, window_id DESC LIMIT ?",
            (monitor_id, int(limit)),
        ).fetchall()
        return [self._monitor_window_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Webhooks and the delivery outbox
    # ------------------------------------------------------------------
    @staticmethod
    def _webhook_from_row(row: sqlite3.Row) -> Webhook:
        return Webhook(
            webhook_id=row["webhook_id"],
            name=row["name"],
            url=row["url"],
            events=list(json.loads(row["events_json"] or "[]")),
            secret=row["secret"],
            description=row["description"],
            status=row["status"],
            creation_timestamp=row["creation_timestamp"],
            last_updated_timestamp=row["last_updated_timestamp"],
        )

    @staticmethod
    def _webhook_delivery_from_row(row: sqlite3.Row) -> WebhookDelivery:
        return WebhookDelivery(
            delivery_id=row["delivery_id"],
            webhook_id=row["webhook_id"],
            event=row["event"],
            payload=json.loads(row["payload_json"] or "{}"),
            status=row["status"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            response_status=row["response_status"],
            created_time=row["created_time"],
            next_attempt_time=row["next_attempt_time"],
            delivered_time=row["delivered_time"],
        )

    def create_webhook(self, webhook: Webhook) -> Webhook:
        with self.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO webhooks(webhook_id, name, url, events_json, secret,"
                    " description, status, creation_timestamp, last_updated_timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        webhook.webhook_id,
                        webhook.name,
                        webhook.url,
                        compact_json(webhook.events),
                        webhook.secret,
                        webhook.description,
                        webhook.status,
                        webhook.creation_timestamp,
                        webhook.last_updated_timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ResourceAlreadyExists(
                        f"webhook named {webhook.name!r} already exists"
                    ) from exc
                raise
        return self.get_webhook(webhook.webhook_id)

    def get_webhook(self, webhook_id: str) -> Webhook:
        row = self._connection().execute(
            "SELECT * FROM webhooks WHERE webhook_id=?", (webhook_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"webhook {webhook_id!r} not found")
        return self._webhook_from_row(row)

    def get_webhook_by_name(self, name: str) -> Webhook:
        row = self._connection().execute(
            "SELECT * FROM webhooks WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"webhook named {name!r} not found")
        return self._webhook_from_row(row)

    def list_webhooks(self) -> list[Webhook]:
        rows = self._connection().execute(
            "SELECT * FROM webhooks ORDER BY name"
        ).fetchall()
        return [self._webhook_from_row(row) for row in rows]

    def update_webhook(
        self,
        webhook_id: str,
        *,
        name: str | None = None,
        url: str | None = None,
        events: list[str] | None = None,
        secret: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Webhook:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM webhooks WHERE webhook_id=?", (webhook_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFound(f"webhook {webhook_id!r} not found")
            try:
                conn.execute(
                    "UPDATE webhooks SET name=?, url=?, events_json=?, secret=?,"
                    " description=?, status=?, last_updated_timestamp=? WHERE webhook_id=?",
                    (
                        row["name"] if name is None else name,
                        row["url"] if url is None else url,
                        row["events_json"] if events is None else compact_json(events),
                        row["secret"] if secret is None else secret,
                        row["description"] if description is None else description,
                        row["status"] if status is None else status,
                        now_ms(),
                        webhook_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ResourceAlreadyExists(
                        f"webhook name {name!r} already exists"
                    ) from exc
                raise
        return self.get_webhook(webhook_id)

    def delete_webhook(self, webhook_id: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM webhooks WHERE webhook_id=?", (webhook_id,))
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"webhook {webhook_id!r} not found")

    def create_webhook_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO webhook_deliveries(delivery_id, webhook_id, event,"
                " payload_json, status, attempts, last_error, response_status,"
                " created_time, next_attempt_time, delivered_time)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery.delivery_id,
                    delivery.webhook_id,
                    delivery.event,
                    compact_json(delivery.payload),
                    delivery.status,
                    delivery.attempts,
                    delivery.last_error,
                    delivery.response_status,
                    delivery.created_time,
                    delivery.next_attempt_time,
                    delivery.delivered_time,
                ),
            )
        return self.get_webhook_delivery(delivery.delivery_id)

    def get_webhook_delivery(self, delivery_id: str) -> WebhookDelivery:
        row = self._connection().execute(
            "SELECT * FROM webhook_deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"webhook delivery {delivery_id!r} not found")
        return self._webhook_delivery_from_row(row)

    def list_due_webhook_deliveries(self, now: int, *, limit: int = 100) -> list[WebhookDelivery]:
        rows = self._connection().execute(
            "SELECT * FROM webhook_deliveries"
            " WHERE status='PENDING' AND next_attempt_time <= ?"
            " ORDER BY next_attempt_time, created_time LIMIT ?",
            (now, max(1, int(limit))),
        ).fetchall()
        return [self._webhook_delivery_from_row(row) for row in rows]

    def list_webhook_deliveries(
        self, *, webhook_id: str | None = None, limit: int = 100
    ) -> list[WebhookDelivery]:
        if webhook_id is not None:
            rows = self._connection().execute(
                "SELECT * FROM webhook_deliveries WHERE webhook_id=?"
                " ORDER BY created_time DESC, delivery_id LIMIT ?",
                (webhook_id, max(1, int(limit))),
            ).fetchall()
        else:
            rows = self._connection().execute(
                "SELECT * FROM webhook_deliveries"
                " ORDER BY created_time DESC, delivery_id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._webhook_delivery_from_row(row) for row in rows]

    def update_webhook_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        attempts: int,
        last_error: str,
        response_status: int | None,
        next_attempt_time: int,
        delivered_time: int | None,
    ) -> WebhookDelivery:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE webhook_deliveries SET status=?, attempts=?, last_error=?,"
                " response_status=?, next_attempt_time=?, delivered_time=?"
                " WHERE delivery_id=?",
                (
                    status,
                    attempts,
                    last_error,
                    response_status,
                    next_attempt_time,
                    delivered_time,
                    delivery_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ResourceNotFound(f"webhook delivery {delivery_id!r} not found")
        return self.get_webhook_delivery(delivery_id)
