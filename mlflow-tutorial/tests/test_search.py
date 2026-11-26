from __future__ import annotations

import pytest

from mini_mlflow.errors import InvalidParameter
from mini_mlflow.search import (
    Field,
    compile_experiment_filter,
    compile_run_filter,
    compile_run_order,
    evaluate_ast,
    parse_filter,
)


def _seed(service):
    experiment = service.create_experiment("search-lab", tags={"team": "nlp"})
    run_a = service.start_run(experiment_id=experiment.experiment_id, run_name="alpha")
    run_b = service.start_run(experiment_id=experiment.experiment_id, run_name="beta")
    run_c = service.start_run(experiment_id=experiment.experiment_id, run_name="gamma")
    service.log_params(run_a.info.run_id, {"model": "linear", "lr": "0.01"})
    service.log_params(run_b.info.run_id, {"model": "tree", "lr": "0.1"})
    service.log_params(run_c.info.run_id, {"model": "linear", "lr": "0.001"})
    service.log_metrics(run_a.info.run_id, {"acc": 0.70, "loss": 0.6})
    service.log_metrics(run_b.info.run_id, {"acc": 0.90, "loss": 0.2})
    service.log_metrics(run_c.info.run_id, {"acc": 0.80, "loss": 0.3})
    service.set_tag(run_a.info.run_id, "stage", "dev")
    service.set_tag(run_b.info.run_id, "stage", "prod")
    service.set_tag(run_c.info.run_id, "stage", "prod")
    return experiment, run_a, run_b, run_c


def test_compile_run_filter_parameterized() -> None:
    where, params = compile_run_filter(
        'metrics.acc >= 0.8 and params.model = "linear" and tags.stage in ("prod", "dev")'
    )
    assert "r.lifecycle_stage = 'active'" in where
    assert "EXISTS (SELECT 1 FROM metrics m" in where
    assert "EXISTS (SELECT 1 FROM run_params p" in where
    assert "EXISTS (SELECT 1 FROM run_tags t" in where
    assert params == ["acc", 0.8, "model", "linear", "stage", "prod", "dev"]


def test_compile_rejects_unknown_attribute_and_injection() -> None:
    with pytest.raises(InvalidParameter):
        compile_run_filter("attributes.password = 'x'")
    with pytest.raises(InvalidParameter):
        compile_run_filter("metrics.acc = 0.8; DROP TABLE runs")
    with pytest.raises(InvalidParameter):
        compile_run_filter("1 = 1")
    with pytest.raises(InvalidParameter):
        compile_run_filter("metrics.acc = 'high'")


def test_order_by_metrics_uses_placeholder() -> None:
    order, params = compile_run_order(["metrics.acc DESC", "attributes.start_time ASC"])
    assert params == ["acc"]
    assert "start_time ASC" in order


def test_parser_precedence() -> None:
    ast = parse_filter("a = 1 or b = 2 and c = 3")
    assert ast.op == "OR"
    assert ast.children[1].op == "AND"


def test_backtick_keys() -> None:
    where, params = compile_run_filter("tags.`model name` = 'x y'")
    assert "run_tags" in where
    assert params == ["model name", "x y"]


def test_search_runs_filters(service) -> None:
    _, run_a, run_b, run_c = _seed(service)
    runs, _ = service.search_runs("metrics.acc > 0.75")
    assert {run.info.run_name for run in runs} == {"beta", "gamma"}
    runs, _ = service.search_runs('params.model = "linear" and tags.stage = "prod"')
    assert [run.info.run_name for run in runs] == ["gamma"]
    runs, _ = service.search_runs("metrics.loss >= 0.2 and metrics.loss <= 0.3")
    assert {run.info.run_name for run in runs} == {"beta", "gamma"}
    runs, _ = service.search_runs("attributes.run_name LIKE 'a%'")
    assert [run.info.run_name for run in runs] == ["alpha"]


def test_search_uses_latest_metric(service) -> None:
    run = service.start_run(run_name="history")
    service.log_metric(run.info.run_id, "score", 0.1, step=1)
    service.log_metric(run.info.run_id, "score", 0.9, step=2)
    runs, _ = service.search_runs("metrics.score > 0.5")
    assert len(runs) == 1
    runs, _ = service.search_runs("metrics.score < 0.5")
    assert not runs


def test_search_order_and_pagination(service) -> None:
    _, _, _, _ = _seed(service)
    page1, token = service.search_runs(
        order_by=["metrics.acc DESC"], max_results=2
    )
    assert [run.info.run_name for run in page1] == ["beta", "gamma"]
    assert token
    page2, token2 = service.search_runs(
        order_by=["metrics.acc DESC"], max_results=2, page_token=token
    )
    assert [run.info.run_name for run in page2] == ["alpha"]
    assert token2 is None


def test_search_null_tags(service) -> None:
    run = service.start_run(run_name="untagged")
    service.set_tag(run.info.run_id, "present", "yes")
    runs, _ = service.search_runs("tags.present IS NOT NULL")
    assert len(runs) == 1
    runs, _ = service.search_runs("tags.missing IS NULL")
    assert len(runs) == 1


def test_search_excludes_deleted_runs(service) -> None:
    run = service.start_run(run_name="to-delete")
    service.delete_run(run.info.run_id)
    runs, _ = service.search_runs()
    assert not runs
    runs, _ = service.search_runs(include_deleted=True)
    assert len(runs) == 1


def test_experiment_search(service) -> None:
    service.create_experiment("zebra", tags={"owner": "a"})
    service.create_experiment("alpha", tags={"owner": "b"})
    experiments, _ = service.search_experiments("tags.owner = 'a'")
    assert [item.name for item in experiments] == ["zebra"]
    experiments, _ = service.search_experiments(order_by=["attributes.name ASC"])
    assert experiments[0].name == "Default"
    experiments, token = service.search_experiments(max_results=1)
    assert len(experiments) == 1 and token


def test_evaluate_ast_registry_style() -> None:
    node = parse_filter("name = 'm' and tags.team = 'x'")
    values = {"name": "m", "team": "x"}
    assert evaluate_ast(node, lambda field: values.get(field.key))
    node = parse_filter("name LIKE 'm%'")
    assert evaluate_ast(node, lambda field: "model")


def test_experiment_not_ilike_and_tag(service) -> None:
    service.create_experiment("Zebra", tags={"team": "NLP"})
    service.create_experiment("other", tags={"team": "vision"})
    experiments, _ = service.search_experiments("name NOT ILIKE 'z%'")
    assert [item.name for item in experiments] == ["Default", "other"]
    experiments, _ = service.search_experiments("tags.team NOT ILIKE 'n%'")
    names = {item.name for item in experiments}
    assert "other" in names and "Zebra" not in names


def test_numeric_param_filter_ignores_non_numeric_values(service) -> None:
    run = service.start_run(run_name="params")
    service.log_param(run.info.run_id, "text", "linear")
    service.log_param(run.info.run_id, "number", "2.5")
    runs, _ = service.search_runs("params.text < 10")
    assert runs == []
    runs, _ = service.search_runs("params.number < 10")
    assert [item.info.run_id for item in runs] == [run.info.run_id]


def test_search_runs_by_dataset_fields(service) -> None:
    experiment = service.create_experiment("dataset-search")
    training = service.start_run(
        experiment_id=experiment.experiment_id, run_name="training"
    )
    evaluation = service.start_run(
        experiment_id=experiment.experiment_id, run_name="evaluation"
    )
    service.log_input(
        training.info.run_id,
        dataset_name="transactions",
        dataset_digest="sha256:aaa",
        dataset_source="s3://bucket/transactions/2026-11-20",
        dataset_source_type="s3",
        context="training",
    )
    service.log_input(
        training.info.run_id,
        dataset_name="customers",
        dataset_digest="sha256:ccc",
        context="training",
    )
    service.log_input(
        evaluation.info.run_id,
        dataset_name="transactions",
        dataset_digest="sha256:bbb",
        context="evaluation",
    )

    def matching(filter_string: str) -> set[str]:
        runs, _ = service.search_runs(
            filter_string, experiment_ids=[experiment.experiment_id]
        )
        return {run.info.run_name for run in runs}

    assert matching('datasets.name = "transactions"') == {"training", "evaluation"}
    assert matching('datasets.digest = "sha256:bbb"') == {"evaluation"}
    assert matching('datasets.context = "training"') == {"training"}
    assert matching('datasets.name LIKE "trans%"') == {"training", "evaluation"}
    assert matching('datasets.name IN ("customers")') == {"training"}
    assert matching('datasets.context != "training"') == {"evaluation"}
    assert matching('datasets.name = "missing"') == set()


def test_dataset_predicates_are_independent_exists(service) -> None:
    # Each dataset predicate is its own EXISTS clause, so two predicates may
    # be satisfied by two different logged datasets on the same run.  This
    # mirrors "a run matches if any of its datasets meets the condition" and
    # is why callers should not read an AND as "same dataset".
    experiment = service.create_experiment("dataset-and")
    run = service.start_run(experiment_id=experiment.experiment_id, run_name="two-inputs")
    service.log_input(run.info.run_id, dataset_name="a", context="training")
    service.log_input(run.info.run_id, dataset_name="b", context="evaluation")
    runs, _ = service.search_runs(
        'datasets.name = "a" AND datasets.context = "evaluation"',
        experiment_ids=[experiment.experiment_id],
    )
    assert [item.info.run_id for item in runs] == [run.info.run_id]


def test_dataset_filter_validation(service) -> None:
    with pytest.raises(InvalidParameter):
        service.search_runs('datasets.source = "s3://bucket/x"')
    with pytest.raises(InvalidParameter):
        service.search_runs('datasets.name > "a"')
    with pytest.raises(InvalidParameter):
        service.search_runs("datasets.name IS NULL")


def test_registry_like_missing_tag_semantics(service) -> None:
    service.registry.create_registered_model("ModelA", tags={"team": "NLP"})
    service.registry.create_registered_model("other")
    found = service.registry.search_registered_models("name ILIKE 'model%'")
    assert [item.name for item in found] == ["ModelA"]
    found = service.registry.search_registered_models("tags.team != 'x'")
    assert [item.name for item in found] == ["ModelA"]
    found = service.registry.search_registered_models("tags.missing != 'x'")
    assert found == []
