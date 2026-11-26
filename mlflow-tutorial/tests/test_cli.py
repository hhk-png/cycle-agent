from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mini_mlflow.config import MiniConfig
from mini_mlflow.entities import Span, Trace
from mini_mlflow.models import LinearRegression, save_model
from mini_mlflow.tracking import TrackingService
from mini_mlflow.utils import new_id

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MINI_MLFLOW_TRACKING_URI"] = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    env["MINI_MLFLOW_ARTIFACT_ROOT"] = str(tmp_path / "cli-artifacts")
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mini_mlflow", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mini_mlflow", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "1.0.0" in result.stdout


def test_cli_doctor_and_experiment_roundtrip(tmp_path: Path) -> None:
    env = _env(tmp_path)
    doctor = _run(["doctor"], env)
    assert doctor.returncode == 0
    payload = json.loads(doctor.stdout)
    assert payload["ok"] is True

    created = _run(["experiments", "create", "cli-exp", "--tag", "team=cli"], env)
    assert created.returncode == 0, created.stderr
    experiment = json.loads(created.stdout)
    assert experiment["name"] == "cli-exp"

    listed = _run(["experiments", "list"], env)
    assert listed.returncode == 0
    experiments = json.loads(listed.stdout)["experiments"]
    assert any(item["name"] == "cli-exp" for item in experiments)

    fetched = _run(["experiments", "get", experiment["experiment_id"]], env)
    assert json.loads(fetched.stdout)["name"] == "cli-exp"


def test_cli_run_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "MLproject").write_text(
        """
name: cli-project
entry_points:
  main:
    parameters:
      value:
        type: int
        default: 1
    command: "python main.py --value {value}"
""",
        encoding="utf-8",
    )
    (project / "main.py").write_text(
        """
import argparse, os
import mini_mlflow as mlflow
parser = argparse.ArgumentParser()
parser.add_argument("--value", type=int)
args = parser.parse_args()
with mlflow.start_run(run_id=os.environ["MINI_MLFLOW_RUN_ID"]):
    mlflow.log_metric("value", args.value)
print("done", args.value)
""",
        encoding="utf-8",
    )
    env = _env(tmp_path)
    result = _run(["run", str(project), "-P", "value=7"], env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout[result.stdout.rfind("{") :])
    assert payload["status"] == "FINISHED"
    runs = _run(["runs", "get", payload["run_id"]], env)
    stored = json.loads(runs.stdout)
    assert stored["data"]["metrics"][0]["value"] == 7.0


def test_cli_models_predict(tmp_path: Path) -> None:
    env = _env(tmp_path)
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    model_dir = tmp_path / "model"
    save_model(model_dir, model, input_example=[{"x": 3.0}])
    result = _run(
        [
            "models",
            "predict",
            "-m",
            str(model_dir),
            "--input",
            json.dumps({"dataframe_split": {"columns": ["x"], "data": [[3.0]]}}),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["predictions"] == [6.0]


def test_cli_unknown_resource_exits_nonzero(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = _run(["experiments", "get", "999"], env)
    assert result.returncode == 2
    error = json.loads(result.stderr)
    assert error["error_code"] == "RESOURCE_DOES_NOT_EXIST"


def test_cli_db_init(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = _run(["db", "init"], env)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert Path(payload["backend"]).exists()


def test_cli_models_predict_instances_with_signature(tmp_path: Path) -> None:
    env = _env(tmp_path)
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    model_dir = tmp_path / "model"
    save_model(model_dir, model, input_example=[{"x": 3.0}])
    result = _run(
        ["models", "predict", "-m", str(model_dir), "--input", json.dumps({"instances": [[3.0]]})],
        env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["predictions"] == [6.0]


def test_cli_prompt_roundtrip(tmp_path: Path) -> None:
    env = _env(tmp_path)
    created = _run(
        [
            "prompts",
            "register",
            "support",
            "--template",
            "Hi {{name}}",
            "--message",
            "initial",
            "--tag",
            "owner=qa",
        ],
        env,
    )
    assert created.returncode == 0, created.stderr
    prompt = json.loads(created.stdout)
    assert prompt["version"] == "1"
    assert prompt["variables"] == ["name"]

    aliased = _run(["prompts", "set-alias", "support", "production", "1"], env)
    assert aliased.returncode == 0, aliased.stderr
    loaded = _run(["prompts", "get", "prompts:/support@production"], env)
    assert loaded.returncode == 0, loaded.stderr
    assert json.loads(loaded.stdout)["template"] == "Hi {{name}}"

    listed = _run(["prompts", "list", "--filter", 'tags.owner = "qa"'], env)
    assert [item["name"] for item in json.loads(listed.stdout)["prompts"]] == ["support"]
    versions = _run(["prompts", "versions", "support"], env)
    assert len(json.loads(versions.stdout)["prompt_versions"]) == 1


def test_cli_feedback_roundtrip(tmp_path: Path) -> None:
    env = _env(tmp_path)
    config = MiniConfig(
        backend_store_uri=f"sqlite:///{(tmp_path / 'cli.db').as_posix()}",
        artifact_root=str(tmp_path / "cli-artifacts"),
        port=0,
    )
    service = TrackingService(config)
    trace_id = new_id()
    try:
        service.log_trace(
            Trace(
                trace_id=trace_id,
                name="cli-trace",
                spans=[
                    Span(
                        span_id="b" * 16,
                        trace_id=trace_id,
                        name="root",
                        start_time_ns=1,
                        end_time_ns=2,
                    )
                ],
            )
        )
    finally:
        service.close()

    added = _run(
        [
            "feedback",
            "add",
            "--trace-id",
            trace_id,
            "--name",
            "quality",
            "--value",
            "0.9",
            "--value-type",
            "float",
            "--source-id",
            "reviewer-1",
        ],
        env,
    )
    assert added.returncode == 0, added.stderr
    assert json.loads(added.stdout)["value"] == 0.9
    listed = _run(["feedback", "list", "--trace-id", trace_id], env)
    assert json.loads(listed.stdout)["assessments"][0]["name"] == "quality"


def test_cli_gateway_validate_and_invoke(tmp_path: Path) -> None:
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(
        """
providers:
  fast:
    kind: mock
    model: mock-fast
    cost_per_1k_input: 1.0
    cost_per_1k_output: 2.0
routes:
  chat:
    primary: fast
    cache_ttl_s: 30
""",
        encoding="utf-8",
    )
    env = _env(tmp_path)
    validated = _run(["gateway", "validate", "--config", str(config_path)], env)
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["ok"] is True

    invoked = _run(
        [
            "gateway",
            "invoke",
            "--config",
            str(config_path),
            "--route",
            "chat",
            "--message",
            "hello",
        ],
        env,
    )
    assert invoked.returncode == 0, invoked.stderr
    response = json.loads(invoked.stdout)
    assert response["provider"] == "fast"
    assert response["text"].endswith("hello")
    assert response["cost_usd"] > 0
