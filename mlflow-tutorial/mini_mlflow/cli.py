"""Command-line interface.

Commands are intentionally composable: the JSON output of ``runs get`` can be
piped to ``jq``, and every command accepts ``--tracking-uri`` so a local
scratch database and a team server use the same binary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from .client import MiniClient
from .config import MiniConfig, load_config
from .errors import MiniMlflowError
from .gateway import GatewayConfig, load_gateway
from .projects import run_project
from .scoring import records_from_payload, serve_model
from .server import serve
from .tracking import TrackingService
from .store import SCHEMA_VERSION
from .version import __version__


def _print_json(value: Any) -> None:
    def default(obj: Any) -> Any:
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(f"not JSON serializable: {type(obj).__name__}")

    print(json.dumps(value, ensure_ascii=False, indent=2, default=default))


def _parse_pairs(values: Iterable[str] | None, *, what: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"{what} must be key=value, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"{what} key must not be empty")
        result[key] = value
    return result


def _client(args: argparse.Namespace) -> MiniClient:
    return MiniClient(args.tracking_uri)


def _add_tracking_uri(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tracking-uri",
        default=os.environ.get("MINI_MLFLOW_TRACKING_URI"),
        help="sqlite:///path/to.db or http://host:port (default: env or ./mlflow.db)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-mlflow",
        description="MiniMLflow: a runnable teaching implementation of MLflow",
    )
    parser.add_argument("--version", action="version", version=f"mini-mlflow {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    server = sub.add_parser("server", help="start the tracking server and UI")
    server.add_argument("--config", help="YAML config file")
    server.add_argument("--backend-store-uri", help="sqlite:///path/to/mlflow.db")
    server.add_argument("--artifact-root", help="local artifact directory")
    server.add_argument("--host", help="bind host (default 127.0.0.1)")
    server.add_argument("--port", type=int, help="bind port (default 5000)")
    server.add_argument("--auth-token", help="shared bearer token (also MINI_MLFLOW_AUTH_TOKEN)")
    server.add_argument("--tls-cert", help="PEM certificate for direct HTTPS")
    server.add_argument("--tls-key", help="PEM private key for direct HTTPS")
    server.add_argument("--gateway-config", help="YAML file enabling /gateway/* routes")
    server.set_defaults(func=_cmd_server)

    db = sub.add_parser("db", help="database utilities")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    init = db_sub.add_parser("init", help="create schema and default experiment")
    init.add_argument("--backend-store-uri", help="sqlite:///path/to/mlflow.db")
    init.add_argument("--artifact-root", help="local artifact directory")
    init.set_defaults(func=_cmd_db_init)

    doctor = sub.add_parser("doctor", help="run self-checks")
    doctor.add_argument("--backend-store-uri", help="temporary or existing database")
    doctor.add_argument("--artifact-root", help="artifact directory")
    doctor.set_defaults(func=_cmd_doctor)

    experiments = sub.add_parser("experiments", help="manage experiments")
    experiments_sub = experiments.add_subparsers(dest="experiments_command", required=True)
    exp_create = experiments_sub.add_parser("create")
    exp_create.add_argument("name")
    exp_create.add_argument("--artifact-location")
    exp_create.add_argument("--tag", action="append", default=[])
    _add_tracking_uri(exp_create)
    exp_create.set_defaults(func=_cmd_experiment_create)
    exp_list = experiments_sub.add_parser("list")
    exp_list.add_argument("--filter")
    _add_tracking_uri(exp_list)
    exp_list.set_defaults(func=_cmd_experiment_list)
    exp_get = experiments_sub.add_parser("get")
    exp_get.add_argument("experiment_id")
    _add_tracking_uri(exp_get)
    exp_get.set_defaults(func=_cmd_experiment_get)
    exp_delete = experiments_sub.add_parser("delete")
    exp_delete.add_argument("experiment_id")
    _add_tracking_uri(exp_delete)
    exp_delete.set_defaults(func=_cmd_experiment_delete)

    runs = sub.add_parser("runs", help="inspect runs")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    run_list = runs_sub.add_parser("list")
    run_list.add_argument("--filter")
    run_list.add_argument("--experiment-id")
    run_list.add_argument("--max-results", type=int, default=50)
    _add_tracking_uri(run_list)
    run_list.set_defaults(func=_cmd_run_list)
    run_get = runs_sub.add_parser("get")
    run_get.add_argument("run_id")
    _add_tracking_uri(run_get)
    run_get.set_defaults(func=_cmd_run_get)
    run_metrics = runs_sub.add_parser("metrics")
    run_metrics.add_argument("run_id")
    run_metrics.add_argument("key")
    _add_tracking_uri(run_metrics)
    run_metrics.set_defaults(func=_cmd_run_metrics)

    registry = sub.add_parser("registry", help="inspect registered models")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    reg_list = registry_sub.add_parser("list")
    _add_tracking_uri(reg_list)
    reg_list.set_defaults(func=_cmd_registry_list)
    reg_versions = registry_sub.add_parser("versions")
    reg_versions.add_argument("name")
    _add_tracking_uri(reg_versions)
    reg_versions.set_defaults(func=_cmd_registry_versions)
    reg_alias = registry_sub.add_parser("set-alias")
    reg_alias.add_argument("name")
    reg_alias.add_argument("alias")
    reg_alias.add_argument("version", type=int)
    _add_tracking_uri(reg_alias)
    reg_alias.set_defaults(func=_cmd_registry_set_alias)

    project = sub.add_parser("run", help="run an MLproject entry point")
    project.add_argument("project_dir")
    project.add_argument("-e", "--entry-point", default="main")
    project.add_argument("-P", "--param", action="append", default=[], help="key=value")
    project.add_argument("--experiment-name")
    project.add_argument("--experiment-id")
    project.add_argument("--run-name")
    project.add_argument("--timeout", type=float)
    _add_tracking_uri(project)
    project.set_defaults(func=_cmd_run_project)

    models = sub.add_parser("models", help="serve or invoke models")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    model_serve = models_sub.add_parser("serve")
    model_serve.add_argument("-m", "--model-uri", required=True)
    model_serve.add_argument("--host", default="127.0.0.1")
    model_serve.add_argument("--port", type=int, default=5001)
    model_serve.add_argument("--auth-token")
    _add_tracking_uri(model_serve)
    model_serve.set_defaults(func=_cmd_models_serve)
    model_predict = models_sub.add_parser("predict")
    model_predict.add_argument("-m", "--model-uri", required=True)
    model_predict.add_argument("--input", help="JSON request body")
    model_predict.add_argument("--input-file", help="read JSON request body from a file")
    _add_tracking_uri(model_predict)
    model_predict.set_defaults(func=_cmd_models_predict)

    prompts = sub.add_parser("prompts", help="manage prompt templates")
    prompts_sub = prompts.add_subparsers(dest="prompts_command", required=True)
    prompt_register = prompts_sub.add_parser("register")
    prompt_register.add_argument("name")
    prompt_source = prompt_register.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("--template")
    prompt_source.add_argument("--template-file")
    prompt_register.add_argument("--message", default="")
    prompt_register.add_argument("--tag", action="append", default=[])
    _add_tracking_uri(prompt_register)
    prompt_register.set_defaults(func=_cmd_prompt_register)
    prompt_get = prompts_sub.add_parser("get")
    prompt_get.add_argument("uri")
    _add_tracking_uri(prompt_get)
    prompt_get.set_defaults(func=_cmd_prompt_get)
    prompt_list = prompts_sub.add_parser("list")
    prompt_list.add_argument("--filter")
    _add_tracking_uri(prompt_list)
    prompt_list.set_defaults(func=_cmd_prompt_list)
    prompt_versions = prompts_sub.add_parser("versions")
    prompt_versions.add_argument("name")
    _add_tracking_uri(prompt_versions)
    prompt_versions.set_defaults(func=_cmd_prompt_versions)
    prompt_alias = prompts_sub.add_parser("set-alias")
    prompt_alias.add_argument("name")
    prompt_alias.add_argument("alias")
    prompt_alias.add_argument("version", type=int)
    _add_tracking_uri(prompt_alias)
    prompt_alias.set_defaults(func=_cmd_prompt_set_alias)

    datasets = sub.add_parser("datasets", help="manage evaluation datasets")
    datasets_sub = datasets.add_subparsers(dest="datasets_command", required=True)
    dataset_create = datasets_sub.add_parser("create")
    dataset_create.add_argument("name")
    dataset_create.add_argument("--records-file", required=True)
    dataset_create.add_argument("--source", default="")
    dataset_create.add_argument("--source-type", default="")
    dataset_create.add_argument("--tag", action="append", default=[])
    _add_tracking_uri(dataset_create)
    dataset_create.set_defaults(func=_cmd_dataset_create)
    dataset_list = datasets_sub.add_parser("list")
    dataset_list.add_argument("--filter")
    dataset_list.add_argument("--max-results", type=int, default=100)
    dataset_list.add_argument("--order-by", action="append", default=[])
    _add_tracking_uri(dataset_list)
    dataset_list.set_defaults(func=_cmd_dataset_list)
    dataset_get = datasets_sub.add_parser("get")
    dataset_get.add_argument("dataset_id")
    _add_tracking_uri(dataset_get)
    dataset_get.set_defaults(func=_cmd_dataset_get)
    dataset_merge = datasets_sub.add_parser("merge")
    dataset_merge.add_argument("dataset_id")
    dataset_merge.add_argument("--records-file", required=True)
    _add_tracking_uri(dataset_merge)
    dataset_merge.set_defaults(func=_cmd_dataset_merge)
    dataset_delete = datasets_sub.add_parser("delete")
    dataset_delete.add_argument("dataset_id")
    _add_tracking_uri(dataset_delete)
    dataset_delete.set_defaults(func=_cmd_dataset_delete)

    monitoring = sub.add_parser("monitoring", help="manage model monitors and drift windows")
    monitoring_sub = monitoring.add_subparsers(dest="monitoring_command", required=True)
    monitor_create = monitoring_sub.add_parser("create")
    monitor_create.add_argument("name")
    monitor_create.add_argument("--baseline-file", help="JSON list of baseline feature rows")
    monitor_create.add_argument(
        "--feature",
        action="append",
        default=[],
        help="name:numeric|name:categorical (repeatable)",
    )
    monitor_create.add_argument("--model-name", default="")
    monitor_create.add_argument("--model-version", default="")
    monitor_create.add_argument("--dataset-id", default="")
    monitor_create.add_argument(
        "--threshold", action="append", default=[], help="key=value (repeatable)"
    )
    monitor_create.add_argument("--tag", action="append", default=[])
    _add_tracking_uri(monitor_create)
    monitor_create.set_defaults(func=_cmd_monitor_create)
    monitor_list = monitoring_sub.add_parser("list")
    _add_tracking_uri(monitor_list)
    monitor_list.set_defaults(func=_cmd_monitor_list)
    monitor_get = monitoring_sub.add_parser("get")
    monitor_get.add_argument("monitor_id")
    _add_tracking_uri(monitor_get)
    monitor_get.set_defaults(func=_cmd_monitor_get)
    monitor_delete = monitoring_sub.add_parser("delete")
    monitor_delete.add_argument("monitor_id")
    _add_tracking_uri(monitor_delete)
    monitor_delete.set_defaults(func=_cmd_monitor_delete)
    monitor_window = monitoring_sub.add_parser("window")
    monitor_window.add_argument("monitor_id")
    monitor_window.add_argument("--records-file", required=True)
    monitor_window.add_argument("--window-start", type=int, default=0)
    monitor_window.add_argument("--window-end", type=int, default=0)
    _add_tracking_uri(monitor_window)
    monitor_window.set_defaults(func=_cmd_monitor_window)
    monitor_windows = monitoring_sub.add_parser("windows")
    monitor_windows.add_argument("monitor_id")
    monitor_windows.add_argument("--limit", type=int, default=100)
    _add_tracking_uri(monitor_windows)
    monitor_windows.set_defaults(func=_cmd_monitor_windows)

    feedback = sub.add_parser("feedback", help="attach feedback to a trace")
    feedback_sub = feedback.add_subparsers(dest="feedback_command", required=True)
    feedback_add = feedback_sub.add_parser("add")
    feedback_add.add_argument("--trace-id", required=True)
    feedback_add.add_argument("--name", required=True)
    feedback_add.add_argument("--value", required=True)
    feedback_add.add_argument("--value-type", choices=["str", "float", "int", "bool"], default="str")
    feedback_add.add_argument("--source-type", default="HUMAN")
    feedback_add.add_argument("--source-id", default="")
    feedback_add.add_argument("--rationale", default="")
    _add_tracking_uri(feedback_add)
    feedback_add.set_defaults(func=_cmd_feedback_add)
    feedback_list = feedback_sub.add_parser("list")
    feedback_list.add_argument("--trace-id", required=True)
    _add_tracking_uri(feedback_list)
    feedback_list.set_defaults(func=_cmd_feedback_list)

    gateway = sub.add_parser("gateway", help="validate or invoke an AI gateway config")
    gateway_sub = gateway.add_subparsers(dest="gateway_command", required=True)
    gateway_validate = gateway_sub.add_parser("validate")
    gateway_validate.add_argument("--config", required=True)
    gateway_validate.set_defaults(func=_cmd_gateway_validate)
    gateway_invoke = gateway_sub.add_parser("invoke")
    gateway_invoke.add_argument("--config", required=True)
    gateway_invoke.add_argument("--route", required=True)
    gateway_invoke.add_argument("--message", required=True)
    gateway_invoke.set_defaults(func=_cmd_gateway_invoke)

    webhooks = sub.add_parser("webhooks", help="manage outbound registry webhooks")
    webhooks_sub = webhooks.add_subparsers(dest="webhooks_command", required=True)
    webhook_create = webhooks_sub.add_parser("create")
    webhook_create.add_argument("name")
    webhook_create.add_argument("--url", required=True)
    webhook_create.add_argument(
        "--event", action="append", required=True, help="entity.action (repeatable)"
    )
    webhook_create.add_argument("--secret", default="")
    webhook_create.add_argument("--description", default="")
    _add_tracking_uri(webhook_create)
    webhook_create.set_defaults(func=_cmd_webhook_create)
    webhook_list = webhooks_sub.add_parser("list")
    _add_tracking_uri(webhook_list)
    webhook_list.set_defaults(func=_cmd_webhook_list)
    webhook_get = webhooks_sub.add_parser("get")
    webhook_get.add_argument("webhook_id")
    _add_tracking_uri(webhook_get)
    webhook_get.set_defaults(func=_cmd_webhook_get)
    webhook_update = webhooks_sub.add_parser("update")
    webhook_update.add_argument("webhook_id")
    webhook_update.add_argument("--name")
    webhook_update.add_argument("--url")
    webhook_update.add_argument("--event", action="append")
    webhook_update.add_argument("--secret")
    webhook_update.add_argument("--description")
    webhook_update.add_argument("--status", choices=["ACTIVE", "DISABLED"])
    _add_tracking_uri(webhook_update)
    webhook_update.set_defaults(func=_cmd_webhook_update)
    webhook_delete = webhooks_sub.add_parser("delete")
    webhook_delete.add_argument("webhook_id")
    _add_tracking_uri(webhook_delete)
    webhook_delete.set_defaults(func=_cmd_webhook_delete)
    webhook_test = webhooks_sub.add_parser("test")
    webhook_test.add_argument("webhook_id")
    _add_tracking_uri(webhook_test)
    webhook_test.set_defaults(func=_cmd_webhook_test)
    webhook_deliveries = webhooks_sub.add_parser("deliveries")
    webhook_deliveries.add_argument("webhook_id")
    webhook_deliveries.add_argument("--limit", type=int, default=100)
    _add_tracking_uri(webhook_deliveries)
    webhook_deliveries.set_defaults(func=_cmd_webhook_deliveries)

    logged = sub.add_parser("logged-models", help="manage MLflow 3 logged models")
    logged_sub = logged.add_subparsers(dest="logged_models_command", required=True)
    lm_list = logged_sub.add_parser("list")
    lm_list.add_argument("--experiment-id", action="append", default=[])
    lm_list.add_argument("--filter")
    lm_list.add_argument("--max-results", type=int, default=100)
    lm_list.add_argument("--order-by", action="append", default=[])
    _add_tracking_uri(lm_list)
    lm_list.set_defaults(func=_cmd_logged_model_list)
    lm_get = logged_sub.add_parser("get")
    lm_get.add_argument("model_id")
    _add_tracking_uri(lm_get)
    lm_get.set_defaults(func=_cmd_logged_model_get)
    lm_create = logged_sub.add_parser("create")
    lm_create.add_argument("name")
    lm_create.add_argument("--experiment-id")
    lm_create.add_argument("--model-type", default="")
    lm_create.add_argument("--source-run-id")
    lm_create.add_argument("--tag", action="append", default=[])
    lm_create.add_argument("--param", action="append", default=[])
    _add_tracking_uri(lm_create)
    lm_create.set_defaults(func=_cmd_logged_model_create)
    lm_finalize = logged_sub.add_parser("finalize")
    lm_finalize.add_argument("model_id")
    lm_finalize.add_argument(
        "--status", choices=["READY", "FAILED", "PENDING"], default="READY"
    )
    lm_finalize.add_argument("--message", default="")
    _add_tracking_uri(lm_finalize)
    lm_finalize.set_defaults(func=_cmd_logged_model_finalize)
    lm_tag = logged_sub.add_parser("tag")
    lm_tag.add_argument("model_id")
    lm_tag.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    lm_tag.add_argument("--delete", action="append", default=[])
    _add_tracking_uri(lm_tag)
    lm_tag.set_defaults(func=_cmd_logged_model_tag)
    lm_params = logged_sub.add_parser("params")
    lm_params.add_argument("model_id")
    lm_params.add_argument("--param", action="append", required=True, metavar="KEY=VALUE")
    _add_tracking_uri(lm_params)
    lm_params.set_defaults(func=_cmd_logged_model_params)
    lm_metrics = logged_sub.add_parser("metrics")
    lm_metrics.add_argument("model_id")
    lm_metrics.add_argument("--metric", action="append", required=True, metavar="KEY=VALUE")
    lm_metrics.add_argument("--step", type=int, default=0)
    _add_tracking_uri(lm_metrics)
    lm_metrics.set_defaults(func=_cmd_logged_model_metrics)
    lm_delete = logged_sub.add_parser("delete")
    lm_delete.add_argument("model_id")
    _add_tracking_uri(lm_delete)
    lm_delete.set_defaults(func=_cmd_logged_model_delete)

    return parser


def _config_from_args(args: argparse.Namespace) -> MiniConfig:
    overrides: dict[str, Any] = {}
    if getattr(args, "backend_store_uri", None):
        overrides["backend_store_uri"] = args.backend_store_uri
    elif getattr(args, "config", None) is None:
        tracking_uri = os.environ.get("MINI_MLFLOW_TRACKING_URI", "")
        if tracking_uri and not tracking_uri.startswith(("http://", "https://")):
            overrides["backend_store_uri"] = tracking_uri
    if getattr(args, "artifact_root", None):
        overrides["artifact_root"] = args.artifact_root
    if getattr(args, "host", None):
        overrides["host"] = args.host
    if getattr(args, "port", None) is not None:
        overrides["port"] = args.port
    if getattr(args, "auth_token", None):
        overrides["auth_token"] = args.auth_token
    return load_config(getattr(args, "config", None), **overrides)


def _cmd_server(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    gateway = load_gateway(args.gateway_config) if args.gateway_config else None
    serve(config, tls_cert=args.tls_cert, tls_key=args.tls_key, gateway=gateway)
    return 0


def _cmd_db_init(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    service = TrackingService(config)
    print(
        json.dumps(
            {
                "status": "ok",
                "backend": str(config.backend_path),
                "artifact_root": str(config.artifact_root_path),
                "schema_version": SCHEMA_VERSION,
            },
            ensure_ascii=False,
        )
    )
    service.close()
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    checks: list[dict[str, Any]] = []
    try:
        service = TrackingService(config)
        experiments, _ = service.search_experiments(max_results=1)
        checks.append({"check": "database", "ok": True, "detail": str(config.backend_path)})
        checks.append({"check": "artifacts", "ok": True, "detail": str(config.artifact_root_path)})
        checks.append({"check": "default_experiment", "ok": bool(service.get_experiment("0")), "detail": "id=0"})
        try:
            service.registry.search_registered_models(max_results=1)
            checks.append({"check": "registry", "ok": True, "detail": "query ok"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"check": "registry", "ok": False, "detail": str(exc)})
        service.close()
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "startup", "ok": False, "detail": str(exc)})
    payload = {"ok": all(item["ok"] for item in checks), "version": __version__, "checks": checks}
    _print_json(payload)
    return 0 if payload["ok"] else 1


def _cmd_experiment_create(args: argparse.Namespace) -> int:
    client = _client(args)
    tags = _parse_pairs(args.tag, what="--tag")
    experiment = client.create_experiment(args.name, args.artifact_location, tags)
    _print_json(experiment.to_dict())
    return 0


def _cmd_experiment_list(args: argparse.Namespace) -> int:
    client = _client(args)
    experiments, token = client.search_experiments(args.filter, max_results=100)
    _print_json({"experiments": [item.to_dict() for item in experiments], "next_page_token": token or ""})
    return 0


def _cmd_experiment_get(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.get_experiment(args.experiment_id).to_dict())
    return 0


def _cmd_experiment_delete(args: argparse.Namespace) -> int:
    client = _client(args)
    client.delete_experiment(args.experiment_id)
    _print_json({"status": "deleted", "experiment_id": args.experiment_id})
    return 0


def _cmd_run_list(args: argparse.Namespace) -> int:
    client = _client(args)
    filter_string = args.filter
    if args.experiment_id:
        clause = f"attributes.experiment_id = {int(args.experiment_id)}"
        filter_string = f"({filter_string}) and {clause}" if filter_string else clause
    runs, token = client.search_runs(filter_string, max_results=args.max_results)
    _print_json({"runs": [item.to_dict() for item in runs], "next_page_token": token or ""})
    return 0


def _cmd_run_get(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.get_run(args.run_id).to_dict())
    return 0


def _cmd_run_metrics(args: argparse.Namespace) -> int:
    client = _client(args)
    history = client.get_metric_history(args.run_id, args.key)
    _print_json({"metrics": [item.to_dict() for item in history]})
    return 0


def _cmd_registry_list(args: argparse.Namespace) -> int:
    client = _client(args)
    models = client.search_registered_models(max_results=200)
    _print_json({"registered_models": [item.to_dict() for item in models]})
    return 0


def _cmd_registry_versions(args: argparse.Namespace) -> int:
    client = _client(args)
    versions = client.search_model_versions(name=args.name, max_results=500)
    _print_json({"model_versions": [item.to_dict() for item in versions]})
    return 0


def _cmd_registry_set_alias(args: argparse.Namespace) -> int:
    client = _client(args)
    client.set_registered_model_alias(args.name, args.alias, args.version)
    _print_json({"status": "ok", "name": args.name, "alias": args.alias, "version": args.version})
    return 0


def _cmd_run_project(args: argparse.Namespace) -> int:
    client = _client(args)
    params = _parse_pairs(args.param, what="-P")
    result = run_project(
        args.project_dir,
        entry_point=args.entry_point,
        parameters=params,
        experiment_name=args.experiment_name,
        experiment_id=args.experiment_id,
        run_name=args.run_name,
        client=client,
        timeout=args.timeout,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.stderr:
        sys.stderr.write(result.stderr)
    _print_json(
        {
            "run_id": result.run_id,
            "status": result.status,
            "exit_code": result.exit_code,
            "command": result.command,
        }
    )
    return 0


def _cmd_models_serve(args: argparse.Namespace) -> int:
    serve_model(
        args.model_uri,
        host=args.host,
        port=args.port,
        tracking_uri=args.tracking_uri,
        auth_token=args.auth_token,
    )
    return 0


def _cmd_models_predict(args: argparse.Namespace) -> int:
    if args.input_file:
        payload = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    elif args.input:
        payload = json.loads(args.input)
    else:
        payload = json.load(sys.stdin)
    client = _client(args)
    model = client.load_model(args.model_uri)
    data = records_from_payload(payload, model) if isinstance(payload, dict) else payload
    _print_json({"predictions": model.predict(data)})
    return 0


def _cmd_prompt_register(args: argparse.Namespace) -> int:
    template = args.template
    if args.template_file:
        template = Path(args.template_file).read_text(encoding="utf-8")
    client = _client(args)
    prompt = client.register_prompt(
        args.name,
        template,
        commit_message=args.message,
        tags=_parse_pairs(args.tag, what="--tag"),
    )
    _print_json(prompt.to_dict())
    return 0


def _cmd_prompt_get(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.load_prompt(args.uri).to_dict())
    return 0


def _cmd_prompt_list(args: argparse.Namespace) -> int:
    client = _client(args)
    prompts = client.search_prompts(args.filter, max_results=200)
    _print_json({"prompts": [item.to_dict() for item in prompts]})
    return 0


def _cmd_prompt_versions(args: argparse.Namespace) -> int:
    client = _client(args)
    versions = client.list_prompt_versions(args.name)
    _print_json({"prompt_versions": [item.to_dict() for item in versions]})
    return 0


def _load_records_file(path: str) -> list[dict[str, Any]]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"records file is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("records file must contain a JSON list of objects")
    return payload


def _cmd_dataset_create(args: argparse.Namespace) -> int:
    client = _client(args)
    dataset = client.create_dataset(
        args.name,
        _load_records_file(args.records_file),
        source=args.source,
        source_type=args.source_type,
        tags=_parse_pairs(args.tag, what="--tag"),
    )
    _print_json(dataset.to_dict())
    return 0


def _cmd_dataset_list(args: argparse.Namespace) -> int:
    client = _client(args)
    datasets = client.search_datasets(
        args.filter,
        max_results=args.max_results,
        order_by=args.order_by or None,
    )
    _print_json({"datasets": [item.to_dict() for item in datasets]})
    return 0


def _cmd_dataset_get(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.get_dataset(args.dataset_id).to_dict())
    return 0


def _cmd_dataset_merge(args: argparse.Namespace) -> int:
    client = _client(args)
    dataset = client.merge_dataset_records(
        args.dataset_id, _load_records_file(args.records_file)
    )
    _print_json(dataset.to_dict())
    return 0


def _cmd_dataset_delete(args: argparse.Namespace) -> int:
    client = _client(args)
    client.delete_dataset(args.dataset_id)
    _print_json({"status": "deleted", "dataset_id": args.dataset_id})
    return 0


def _monitor_features(values: Iterable[str]) -> dict[str, str]:
    features: dict[str, str] = {}
    for item in values:
        if ":" not in item:
            raise ValueError(f"--feature must be name:numeric|name:categorical, got {item!r}")
        name, kind = item.rsplit(":", 1)
        if not name or kind not in {"numeric", "categorical"}:
            raise ValueError(f"--feature must be name:numeric|name:categorical, got {item!r}")
        features[name] = kind
    if not features:
        raise ValueError("at least one --feature is required")
    return features


def _monitor_thresholds(values: Iterable[str]) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for key, value in _parse_pairs(values, what="--threshold").items():
        try:
            thresholds[key] = float(value)
        except ValueError as exc:
            raise ValueError(f"threshold {key!r} must be numeric") from exc
    return thresholds


def _cmd_monitor_create(args: argparse.Namespace) -> int:
    client = _client(args)
    baseline: list[dict[str, Any]] = []
    if args.baseline_file:
        baseline = _load_records_file(args.baseline_file)
    if not baseline and not args.dataset_id:
        raise ValueError("provide --baseline-file or --dataset-id")
    monitor = client.create_monitor(
        args.name,
        baseline,
        _monitor_features(args.feature),
        model_name=args.model_name,
        model_version=args.model_version,
        dataset_id=args.dataset_id,
        thresholds=_monitor_thresholds(args.threshold) or None,
        tags=_parse_pairs(args.tag, what="--tag"),
    )
    _print_json(monitor.to_dict())
    return 0


def _cmd_monitor_list(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json({"monitors": [item.to_dict() for item in client.list_monitors()]})
    return 0


def _cmd_monitor_get(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.get_monitor(args.monitor_id).to_dict())
    return 0


def _cmd_monitor_delete(args: argparse.Namespace) -> int:
    client = _client(args)
    client.delete_monitor(args.monitor_id)
    _print_json({"status": "deleted", "monitor_id": args.monitor_id})
    return 0


def _cmd_monitor_window(args: argparse.Namespace) -> int:
    client = _client(args)
    window = client.record_monitor_window(
        args.monitor_id,
        _load_records_file(args.records_file),
        window_start=args.window_start,
        window_end=args.window_end,
    )
    _print_json(window.to_dict())
    return 0


def _cmd_monitor_windows(args: argparse.Namespace) -> int:
    client = _client(args)
    windows = client.list_monitor_windows(args.monitor_id, limit=args.limit)
    _print_json({"windows": [item.to_dict() for item in windows]})
    return 0


def _cmd_prompt_set_alias(args: argparse.Namespace) -> int:
    client = _client(args)
    client.set_prompt_alias(args.name, args.alias, args.version)
    _print_json(
        {"status": "ok", "name": args.name, "alias": args.alias, "version": args.version}
    )
    return 0


def _cmd_feedback_add(args: argparse.Namespace) -> int:
    value: Any = args.value
    if args.value_type == "float":
        value = float(args.value)
    elif args.value_type == "int":
        value = int(args.value)
    elif args.value_type == "bool":
        lowered = args.value.strip().lower()
        if lowered not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("--value must be true/false for --value-type bool")
        value = lowered in {"true", "1", "yes"}
    client = _client(args)
    assessment = client.log_feedback(
        args.trace_id,
        args.name,
        value,
        source_type=args.source_type,
        source_id=args.source_id,
        rationale=args.rationale,
    )
    _print_json(assessment.to_dict())
    return 0


def _cmd_feedback_list(args: argparse.Namespace) -> int:
    client = _client(args)
    assessments = client.list_assessments(args.trace_id)
    _print_json({"assessments": [item.to_dict() for item in assessments]})
    return 0


def _cmd_gateway_validate(args: argparse.Namespace) -> int:
    config = GatewayConfig.from_yaml(args.config)
    _print_json({"ok": True, "config": config.to_dict()})
    return 0


def _cmd_gateway_invoke(args: argparse.Namespace) -> int:
    gateway = load_gateway(args.config)
    response = gateway.invoke(args.route, args.message)
    _print_json(response.to_dict())
    return 0


def _cmd_webhook_create(args: argparse.Namespace) -> int:
    client = _client(args)
    webhook = client.create_webhook(
        args.name,
        args.url,
        args.event,
        secret=args.secret,
        description=args.description,
    )
    _print_json(webhook.to_dict())
    return 0


def _cmd_webhook_list(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json({"webhooks": [item.to_dict() for item in client.list_webhooks()]})
    return 0


def _cmd_webhook_get(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.get_webhook(args.webhook_id).to_dict())
    return 0


def _cmd_webhook_update(args: argparse.Namespace) -> int:
    client = _client(args)
    webhook = client.update_webhook(
        args.webhook_id,
        name=args.name,
        url=args.url,
        events=args.event,
        secret=args.secret,
        description=args.description,
        status=args.status,
    )
    _print_json(webhook.to_dict())
    return 0


def _cmd_webhook_delete(args: argparse.Namespace) -> int:
    client = _client(args)
    client.delete_webhook(args.webhook_id)
    _print_json({"status": "deleted", "webhook_id": args.webhook_id})
    return 0


def _cmd_webhook_test(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.test_webhook(args.webhook_id).to_dict())
    return 0


def _cmd_webhook_deliveries(args: argparse.Namespace) -> int:
    client = _client(args)
    deliveries = client.list_webhook_deliveries(
        webhook_id=args.webhook_id, limit=args.limit
    )
    _print_json({"deliveries": [item.to_dict() for item in deliveries]})
    return 0


def _cmd_logged_model_list(args: argparse.Namespace) -> int:
    client = _client(args)
    models, token = client.search_logged_models(
        args.experiment_id or None,
        filter_string=args.filter,
        max_results=args.max_results,
        order_by=args.order_by or None,
    )
    _print_json(
        {
            "models": [model.to_dict() for model in models],
            "next_page_token": token or "",
        }
    )
    return 0


def _cmd_logged_model_get(args: argparse.Namespace) -> int:
    client = _client(args)
    _print_json(client.get_logged_model(args.model_id).to_dict())
    return 0


def _cmd_logged_model_create(args: argparse.Namespace) -> int:
    client = _client(args)
    model = client.create_logged_model(
        args.name,
        experiment_id=args.experiment_id,
        model_type=args.model_type,
        source_run_id=args.source_run_id,
        tags=_parse_pairs(args.tag, what="--tag"),
        params=_parse_pairs(args.param, what="--param"),
    )
    _print_json(model.to_dict())
    return 0


def _cmd_logged_model_finalize(args: argparse.Namespace) -> int:
    client = _client(args)
    model = client.finalize_logged_model(
        args.model_id, args.status, status_message=args.message
    )
    _print_json(model.to_dict())
    return 0


def _cmd_logged_model_tag(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.set:
        client.set_logged_model_tags(
            args.model_id, _parse_pairs(args.set, what="--set")
        )
    for key in args.delete:
        client.delete_logged_model_tag(args.model_id, key)
    _print_json(client.get_logged_model(args.model_id).to_dict())
    return 0


def _cmd_logged_model_params(args: argparse.Namespace) -> int:
    client = _client(args)
    model = client.log_logged_model_params(
        args.model_id, _parse_pairs(args.param, what="--param")
    )
    _print_json(model.to_dict())
    return 0


def _cmd_logged_model_metrics(args: argparse.Namespace) -> int:
    client = _client(args)
    model = client.log_logged_model_metrics(
        args.model_id, _parse_pairs(args.metric, what="--metric"), step=args.step
    )
    _print_json(model.to_dict())
    return 0


def _cmd_logged_model_delete(args: argparse.Namespace) -> int:
    client = _client(args)
    client.delete_logged_model(args.model_id)
    _print_json({"status": "deleted", "model_id": args.model_id})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except MiniMlflowError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
