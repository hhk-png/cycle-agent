"""A secure, deliberately small MLflow Projects runner.

Supported MLproject subset::

    name: demo
    entry_points:
      main:
        parameters:
          alpha:
            type: float
            default: 0.5
        command: "python train.py --alpha {alpha}"

Differences from production MLflow Projects are explicit:

* no Conda/Docker execution (the tutorial explains the environment problem,
  but does not ship an environment solver);
* commands are tokenized with ``shlex`` and executed without a shell, which
  prevents parameter injection but also means shell operators are unsupported;
* child processes resume the parent run explicitly via the injected
  ``MINI_MLFLOW_RUN_ID`` environment variable.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .client import MiniClient
from .errors import InvalidParameter, ProjectExecutionError, ResourceNotFound
from .utils import coerce_param_value

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SUPPORTED_TYPES = {"string", "int", "float", "bool", "path"}
_EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", "node_modules", "mlruns", "mlartifacts"}
_MAX_SOURCE_FILES = 200
_MAX_SOURCE_BYTES = 10 * 1024 * 1024


@dataclass
class ParameterSpec:
    name: str
    type: str = "string"
    default: Any = None
    required: bool = False

    def coerce(self, value: Any) -> Any:
        if self.type == "string" or self.type == "path":
            return str(value)
        if self.type == "int":
            if isinstance(value, bool):
                raise InvalidParameter(f"parameter {self.name!r} expects int")
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise InvalidParameter(f"parameter {self.name!r} expects int, got {value!r}") from exc
        if self.type == "float":
            if isinstance(value, bool):
                raise InvalidParameter(f"parameter {self.name!r} expects float")
            try:
                result = float(value)
            except (TypeError, ValueError) as exc:
                raise InvalidParameter(f"parameter {self.name!r} expects float, got {value!r}") from exc
            if not math.isfinite(result):
                raise InvalidParameter(f"parameter {self.name!r} must be finite")
            return result
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"true", "1", "yes"}:
                return True
            if text in {"false", "0", "no"}:
                return False
            raise InvalidParameter(f"parameter {self.name!r} expects bool, got {value!r}")
        raise InvalidParameter(f"unsupported parameter type {self.type!r}")


@dataclass
class EntryPoint:
    name: str
    command: str
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    working_dir: str | None = None


@dataclass
class ProjectSpec:
    root: Path
    name: str
    entry_points: dict[str, EntryPoint]


@dataclass
class ProjectRunResult:
    run_id: str
    status: str
    exit_code: int
    command: list[str]
    stdout: str
    stderr: str


def load_project(project_dir: str | Path) -> ProjectSpec:
    root = Path(project_dir).resolve()
    mlproject_path = root / "MLproject"
    if not mlproject_path.is_file():
        raise InvalidParameter(f"MLproject file not found under {root}")
    try:
        raw = yaml.safe_load(mlproject_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise InvalidParameter(f"invalid MLproject YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidParameter("MLproject must contain a YAML mapping")
    name = str(raw.get("name") or root.name)
    raw_entry_points = raw.get("entry_points")
    if not isinstance(raw_entry_points, dict) or not raw_entry_points:
        raise InvalidParameter("MLproject must define at least one entry point")
    entry_points: dict[str, EntryPoint] = {}
    for entry_name, entry_config in raw_entry_points.items():
        if not isinstance(entry_config, dict) or "command" not in entry_config:
            raise InvalidParameter(f"entry point {entry_name!r} must be a mapping with a command")
        command = entry_config["command"]
        if not isinstance(command, str) or not command.strip():
            raise InvalidParameter(f"entry point {entry_name!r} command must be a non-empty string")
        parameters: dict[str, ParameterSpec] = {}
        raw_parameters = entry_config.get("parameters") or {}
        if not isinstance(raw_parameters, dict):
            raise InvalidParameter(f"entry point {entry_name!r} parameters must be a mapping")
        for param_name, config in raw_parameters.items():
            if isinstance(config, dict):
                param_type = str(config.get("type", "string"))
                default = config.get("default")
            else:
                param_type = "string"
                default = config
            if param_type not in _SUPPORTED_TYPES:
                raise InvalidParameter(
                    f"parameter {param_name!r} has unsupported type {param_type!r}; "
                    f"supported: {sorted(_SUPPORTED_TYPES)}"
                )
            parameters[str(param_name)] = ParameterSpec(
                name=str(param_name),
                type=param_type,
                default=default,
                required=default is None and not (isinstance(config, dict) and "default" in config),
            )
        working_dir = entry_config.get("working_dir") or entry_config.get("working_directory")
        entry_points[str(entry_name)] = EntryPoint(
            name=str(entry_name),
            command=command,
            parameters=parameters,
            working_dir=str(working_dir) if working_dir else None,
        )
    return ProjectSpec(root=root, name=name, entry_points=entry_points)


def resolve_parameters(entry: EntryPoint, supplied: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(supplied) - set(entry.parameters)
    if unknown:
        raise InvalidParameter(f"unknown parameters for entry point {entry.name!r}: {sorted(unknown)}")
    values: dict[str, Any] = {}
    for name, spec in entry.parameters.items():
        if name in supplied:
            values[name] = spec.coerce(supplied[name])
        elif spec.default is not None:
            values[name] = spec.coerce(spec.default)
        elif spec.required:
            raise InvalidParameter(f"missing required parameter {name!r}")
        else:
            values[name] = spec.coerce("")
    return values


def _split_command(command: str) -> list[str]:
    """Quote-aware tokenizer that preserves Windows backslashes.

    We intentionally do not invoke a shell, so we only need whitespace,
    single/double quotes and (on POSIX) backslash escaping. ``shlex`` with
    POSIX mode eats unquoted Windows paths such as ``data	rain.csv``.
    """
    tokens: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    index = 0
    length = len(command)

    def flush() -> None:
        if buffer:
            tokens.append("".join(buffer))
            buffer.clear()

    while index < length:
        char = command[index]
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"' and index + 1 < length and command[index + 1] in ('"', "\\"):
                buffer.append(command[index + 1])
                index += 1
            else:
                buffer.append(char)
        else:
            if char.isspace():
                flush()
            elif char in {'"', "'"}:
                quote = char
            elif char == "\\" and os.name != "nt":
                if index + 1 >= length:
                    raise InvalidParameter("command ends with a dangling backslash")
                buffer.append(command[index + 1])
                index += 1
            else:
                buffer.append(char)
        index += 1
    if quote is not None:
        raise InvalidParameter("unterminated quote in entry point command")
    flush()
    return tokens


def build_command(entry: EntryPoint, parameters: Mapping[str, Any], python: str) -> list[str]:
    tokens = _split_command(entry.command)
    unknown: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in parameters:
            unknown.add(name)
            return match.group(0)
        return str(parameters[name])

    rendered = [_PLACEHOLDER_RE.sub(replace, token) for token in tokens]
    if unknown:
        raise InvalidParameter(f"command references unknown parameters: {sorted(unknown)}")
    if not rendered:
        raise InvalidParameter("entry point command is empty after parsing")
    executable = rendered[0]
    if executable in {"python", "python3"}:
        rendered[0] = python
    return rendered


def _snapshot_project(root: Path, destination: Path) -> tuple[int, int]:
    files = 0
    total = 0
    for path in sorted(root.rglob("*")):
        if files >= _MAX_SOURCE_FILES or total >= _MAX_SOURCE_BYTES:
            break
        relative = path.relative_to(root)
        if any(part in _EXCLUDE_DIRS for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix in {".db", ".db-wal", ".db-shm", ".pyc"}:
            continue
        size = path.stat().st_size
        if total + size > _MAX_SOURCE_BYTES:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files += 1
        total += size
    return files, total


def _end_if_running(client: MiniClient, run_id: str, status: str) -> None:
    run = client.get_run(run_id)
    if not run.info.is_terminal:
        client.end_run(run_id, status)


def run_project(
    project_dir: str | Path,
    *,
    entry_point: str = "main",
    parameters: Mapping[str, Any] | None = None,
    experiment_name: str | None = None,
    experiment_id: str | None = None,
    run_name: str | None = None,
    client: MiniClient | None = None,
    tracking_uri: str | None = None,
    timeout: float | None = None,
) -> ProjectRunResult:
    spec = load_project(project_dir)
    if entry_point not in spec.entry_points:
        raise ResourceNotFound(
            f"entry point {entry_point!r} not found; available: {sorted(spec.entry_points)}"
        )
    entry = spec.entry_points[entry_point]
    values = resolve_parameters(entry, parameters or {})
    # Validate before creating a run; otherwise an oversized value would leave
    # an orphan RUNNING run when logging fails.
    for key, value in values.items():
        try:
            coerce_param_value(value)
        except InvalidParameter as exc:
            raise InvalidParameter(f"parameter {key!r}: {exc}") from exc
    command = build_command(entry, values, sys.executable)
    working_dir = (spec.root / entry.working_dir).resolve() if entry.working_dir else spec.root
    if not working_dir.is_dir():
        raise InvalidParameter(f"working directory does not exist: {working_dir}")
    try:
        working_dir.relative_to(spec.root)
    except ValueError as exc:
        raise InvalidParameter("working directory must stay inside the project") from exc

    client = client or MiniClient(tracking_uri)
    if experiment_id is None:
        name = experiment_name or spec.name
        experiment = client.get_experiment_by_name(name)
        if experiment is None:
            experiment = client.create_experiment(name)
        experiment_id = experiment.experiment_id
    run = client.start_run(
        experiment_id=experiment_id,
        run_name=run_name or f"{spec.name}-{entry_point}",
        tags={
            "mlflow.project.name": spec.name,
            "mlflow.project.entryPoint": entry_point,
            "mlflow.source.type": "local",
            "mlflow.project.security": "no-shell",
        },
    )
    run_id = run.info.run_id
    stdout = ""
    stderr = ""
    exit_code = 127
    try:
        env = os.environ.copy()
        env.update(
            {
                "MINI_MLFLOW_TRACKING_URI": client.tracking_uri,
                "MLFLOW_TRACKING_URI": client.tracking_uri,
                "MINI_MLFLOW_RUN_ID": run_id,
                "MLFLOW_RUN_ID": run_id,
                "MINI_MLFLOW_EXPERIMENT_ID": str(experiment_id),
                "MLFLOW_EXPERIMENT_ID": str(experiment_id),
                "PYTHONPATH": os.pathsep.join(
                    [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH", "")]
                ).strip(os.pathsep),
            }
        )
        local_config = client.local_config
        if local_config is not None:  # keep child paths aligned and absolute
            env["MINI_MLFLOW_ARTIFACT_ROOT"] = str(local_config.artifact_root_path)
            env["MINI_MLFLOW_BACKEND_STORE_URI"] = local_config.tracking_uri
        if client.auth_token:
            env["MINI_MLFLOW_TRACKING_TOKEN"] = client.auth_token

        if values:
            client.log_params(run_id, {key: value for key, value in values.items()})
        client.set_tag(run_id, "mlflow.project.command", " ".join(command))
        client.log_text(
            run_id,
            yaml.safe_dump(
                {
                    "project": spec.name,
                    "entry_point": entry_point,
                    "parameters": values,
                    "command": command,
                    "cwd": str(working_dir),
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            "project/run_spec.yaml",
        )
        with tempfile.TemporaryDirectory(prefix="mini-mlflow-project-") as tmp_source:
            files, _size = _snapshot_project(spec.root, Path(tmp_source))
            if files:
                client.log_artifacts(run_id, tmp_source, artifact_path="source")

        try:
            completed = subprocess.run(
                command,
                cwd=str(working_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout, stderr = completed.stdout, completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\n[mini-mlflow] timed out after {timeout}s"
            exit_code = 124
        finally:
            client.log_text(run_id, stdout, "logs/stdout.log")
            client.log_text(run_id, stderr, "logs/stderr.log")
            _end_if_running(client, run_id, "FINISHED" if exit_code == 0 else "FAILED")

        if exit_code != 0:
            raise ProjectExecutionError(
                f"project entry point {entry_point!r} exited with code {exit_code}; "
                f"stderr tail: {stderr[-1000:]}"
            )
        run = client.get_run(run_id)
        if run.info.status != "FINISHED":
            raise ProjectExecutionError(
                f"child process exited 0 but the run ended as {run.info.status}; "
                "the entry point may have ended the run itself"
            )
    except BaseException:
        # Any setup/logging failure must not leave a permanently RUNNING run.
        try:
            _end_if_running(client, run_id, "FAILED")
        except Exception:  # noqa: BLE001 - preserve the original exception
            pass
        raise

    return ProjectRunResult(
        run_id=run_id,
        status=run.info.status,
        exit_code=exit_code,
        command=command,
        stdout=stdout,
        stderr=stderr,
    )
