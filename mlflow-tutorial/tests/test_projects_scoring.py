from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.errors import InvalidParameter, ProjectExecutionError, ResourceNotFound
from mini_mlflow.models import LinearRegression, save_model
from mini_mlflow.projects import build_command, load_project, run_project, resolve_parameters
from mini_mlflow.scoring import create_scoring_server


def _write_project(root: Path, script: str, *, command: str = "python train.py --alpha {alpha}", extra: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "MLproject").write_text(
        f"""
name: test-project
entry_points:
  main:
    parameters:
      alpha:
        type: float
        default: 0.5
    command: "{command}"
{extra}
""",
        encoding="utf-8",
    )
    (root / "train.py").write_text(script, encoding="utf-8")
    return root


TRAIN_SCRIPT = """
import argparse, os
import mini_mlflow as mlflow
parser = argparse.ArgumentParser()
parser.add_argument("--alpha", type=float)
args = parser.parse_args()
with mlflow.start_run(run_id=os.environ["MINI_MLFLOW_RUN_ID"]):
    mlflow.log_metric("alpha", args.alpha)
print("trained", args.alpha)
"""


def test_load_project_and_parameters(tmp_path: Path) -> None:
    project = load_project(_write_project(tmp_path / "project", TRAIN_SCRIPT))
    assert project.name == "test-project"
    entry = project.entry_points["main"]
    values = resolve_parameters(entry, {"alpha": "0.25"})
    assert values == {"alpha": 0.25}
    assert resolve_parameters(entry, {}) == {"alpha": 0.5}
    with pytest.raises(InvalidParameter):
        resolve_parameters(entry, {"unknown": 1})
    with pytest.raises(InvalidParameter):
        resolve_parameters(entry, {"alpha": "not-a-float"})


def test_build_command_does_not_use_shell(tmp_path: Path) -> None:
    project = load_project(_write_project(tmp_path / "project", TRAIN_SCRIPT))
    command = build_command(project.entry_points["main"], {"alpha": "0.5; echo pwned"}, "python")
    assert command[0] == "python"
    assert command[-1] == "0.5; echo pwned"
    assert len(command) == 4


def test_project_run_success(tmp_path: Path, config: MiniConfig) -> None:
    project = _write_project(tmp_path / "project", TRAIN_SCRIPT)
    client = MiniClient(config=config)
    result = run_project(project, parameters={"alpha": 0.25}, client=client)
    assert result.exit_code == 0
    assert result.status == "FINISHED"
    run = client.get_run(result.run_id)
    assert run.data.metrics["alpha"] == 0.25
    assert run.data.tags["mlflow.project.name"] == "test-project"
    assert "logs/stdout.log" in {item.path for item in client.list_artifacts(result.run_id, "logs")}
    client.close()


def test_project_run_failure(tmp_path: Path, config: MiniConfig) -> None:
    project = _write_project(
        tmp_path / "project",
        "import sys\nprint('bad', file=sys.stderr)\nsys.exit(3)\n",
    )
    client = MiniClient(config=config)
    with pytest.raises(ProjectExecutionError, match="code 3"):
        run_project(project, client=client)
    runs, _ = client.search_runs('tags.mlflow.project.name = "test-project"')
    assert runs[0].info.status == "FAILED"
    assert client.read_artifact(runs[0].info.run_id, "logs/stderr.log") == b"bad\n"
    client.close()


def test_project_unknown_entry_point(tmp_path: Path, config: MiniConfig) -> None:
    project = _write_project(tmp_path / "project", TRAIN_SCRIPT)
    client = MiniClient(config=config)
    with pytest.raises(ResourceNotFound):
        run_project(project, entry_point="missing", client=client)
    client.close()


def test_project_working_dir_escape_rejected(tmp_path: Path, config: MiniConfig) -> None:
    root = _write_project(tmp_path / "project", TRAIN_SCRIPT)
    (root / "MLproject").write_text(
        """
name: escape
entry_points:
  main:
    working_dir: ..
    parameters: {}
    command: "python train.py"
""",
        encoding="utf-8",
    )
    client = MiniClient(config=config)
    with pytest.raises(InvalidParameter, match="inside the project"):
        run_project(root, client=client)
    client.close()


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_scoring_server(tmp_path: Path) -> None:
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    save_model(tmp_path / "model", model, input_example=[{"x": 3.0}])
    server = create_scoring_server(str(tmp_path / "model"), host="127.0.0.1", port=0, auth_token="s3cret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _http_json(base + "/ping")[0] == 200
        status, payload = _http_json(
            base + "/invocations",
            {"dataframe_split": {"columns": ["x"], "data": [[3.0]]}},
            headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"},
        )
        assert status == 200
        assert payload["predictions"] == pytest.approx([6.0])
        status, payload = _http_json(
            base + "/invocations",
            {"instances": [[4.0]]},
            headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"},
        )
        assert status == 200 and payload["predictions"] == pytest.approx([8.0])
        status, _ = _http_json(base + "/invocations", {"instances": [[1.0]]})
        assert status == 401
        status, payload = _http_json(
            base + "/invocations",
            {"instances": [{"y": 1.0}]},
            headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"},
        )
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_project_with_no_parameters_and_cleanup(tmp_path: Path, config: MiniConfig) -> None:
    project = tmp_path / "zero"
    project.mkdir()
    (project / "MLproject").write_text(
        """
name: zero-params
entry_points:
  main:
    parameters: {}
    command: "python run.py"
""",
        encoding="utf-8",
    )
    (project / "run.py").write_text("print('ok')\n", encoding="utf-8")
    client = MiniClient(config=config)
    result = run_project(project, client=client)
    assert result.status == "FINISHED"
    assert client.get_run(result.run_id).data.params == {}
    client.close()


def test_project_setup_failure_marks_run_failed(tmp_path: Path, config: MiniConfig, monkeypatch) -> None:
    project = _write_project(tmp_path / "project", TRAIN_SCRIPT)
    client = MiniClient(config=config)
    original_set_tag = client.set_tag

    def boom(*args, **kwargs):
        raise InvalidParameter("simulated setup failure")

    monkeypatch.setattr(client, "set_tag", boom)
    with pytest.raises(InvalidParameter, match="simulated"):
        run_project(project, client=client)
    runs, _ = client.search_runs('tags.mlflow.project.name = "test-project"')
    assert runs and runs[0].info.status == "FAILED"
    client.close()


def test_command_tokenizer_preserves_windows_paths() -> None:
    import os

    from mini_mlflow.projects import _split_command

    tokens = _split_command(r'python train.py --data data\train.csv --name "hello world"')
    assert tokens[-1] == "hello world"
    if os.name == "nt":
        assert tokens[3] == r"data\train.csv"
