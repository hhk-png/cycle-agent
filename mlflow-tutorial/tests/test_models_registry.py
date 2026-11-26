from __future__ import annotations

import json
from pathlib import Path

import pytest

import mini_mlflow as mlflow
from mini_mlflow.errors import InvalidParameter, ModelLoadError, ResourceAlreadyExists, ResourceNotFound, UnsupportedFlavor
from mini_mlflow.models import (
    LinearRegression,
    ModelSignature,
    PyFuncModel,
    infer_signature,
    load_model,
    save_model,
)
from mini_mlflow.models.signature import ColumnSpec, validate_input


def test_linear_regression_fit_predict() -> None:
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}, {"x": 3.0}], [3.0, 5.0, 7.0])
    assert model.coef_ == pytest.approx([2.0])
    assert model.intercept_ == pytest.approx(1.0)
    assert model.predict([{"x": 4.0}]) == pytest.approx([9.0])
    assert model.score([{"x": 1.0}, {"x": 2.0}, {"x": 3.0}], [3.0, 5.0, 7.0]) == pytest.approx(1.0)


def test_linear_regression_multiple_features_and_columnar() -> None:
    model = LinearRegression(alpha=1e-9).fit(
        {"a": [1.0, 2.0, 3.0], "b": [0.0, 1.0, 0.0]},
        [2.0, 4.0, 6.0],
    )
    assert model.predict({"a": [4.0], "b": [0.0]}) == pytest.approx([8.0], rel=1e-5)


def test_linear_regression_validation() -> None:
    model = LinearRegression()
    with pytest.raises(InvalidParameter):
        model.predict([{"x": 1.0}])
    with pytest.raises(InvalidParameter):
        model.fit([], [])
    with pytest.raises(InvalidParameter):
        model.fit([{"x": 1.0}], [1.0, 2.0])
    with pytest.raises(InvalidParameter):
        model.fit([{"x": "bad"}], [1.0])
    with pytest.raises(InvalidParameter):
        LinearRegression(alpha=-1)


def test_signature_inference_and_validation() -> None:
    signature = infer_signature(
        [{"x": 1, "name": "a"}, {"x": 1.5, "name": "b"}],
        [1.0, 2.0],
    )
    assert signature.to_dict()["inputs"] == [
        {"name": "x", "type": "double"},
        {"name": "name", "type": "string"},
    ]
    assert signature.outputs[0].type == "double"
    validate_input(signature, [{"x": 1, "name": "a"}])
    with pytest.raises(InvalidParameter, match="missing"):
        validate_input(signature, [{"x": 1}])
    with pytest.raises(InvalidParameter, match="expects string"):
        validate_input(signature, [{"x": 1, "name": 2}])


def test_signature_roundtrip() -> None:
    signature = ModelSignature(
        inputs=(ColumnSpec("a", "long"),),
        outputs=(ColumnSpec("prediction", "double"),),
    )
    assert ModelSignature.from_dict(signature.to_dict()) == signature


def test_save_and_load_model(tmp_path: Path) -> None:
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    info = save_model(
        tmp_path / "model",
        model,
        input_example=[{"x": 3.0}],
        metadata={"owner": "tests"},
    )
    assert (tmp_path / "model" / "MLmodel").is_file()
    loaded = load_model(tmp_path / "model")
    assert isinstance(loaded, PyFuncModel)
    assert loaded.predict([{"x": 5.0}]) == pytest.approx([10.0])
    assert loaded.metadata["model_uuid"]
    assert loaded.signature is not None


def test_save_model_refuses_nonempty_without_overwrite(tmp_path: Path) -> None:
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [1.0, 2.0])
    target = tmp_path / "model"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(InvalidParameter, match="not empty"):
        save_model(target, model)
    save_model(target, model, overwrite=True)
    assert (target / "MLmodel").is_file()


def test_save_model_rejects_unsupported_model(tmp_path: Path) -> None:
    with pytest.raises(InvalidParameter, match="fitted"):
        save_model(tmp_path / "m", LinearRegression())
    with pytest.raises(UnsupportedFlavor):
        save_model(tmp_path / "m2", object(), flavor="sklearn")


def test_unsafe_loader_is_rejected(tmp_path: Path) -> None:
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [1.0, 2.0])
    save_model(tmp_path / "m", model)
    mlmodel = tmp_path / "m" / "MLmodel"
    text = mlmodel.read_text(encoding="utf-8")
    mlmodel.write_text(
        text.replace("mini_mlflow.models.linear", "evil.module"),
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedFlavor, match="pickle"):
        load_model(tmp_path / "m")


def test_missing_mlmodel(tmp_path: Path) -> None:
    with pytest.raises(ModelLoadError):
        load_model(tmp_path)


def test_log_and_load_model_from_run(service) -> None:
    run = service.start_run(run_name="train")
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    info = service.log_model(
        run.info.run_id,
        model,
        input_example=[{"x": 3.0}],
        registered_model_name="linear-demo",
    )
    service.end_run(run.info.run_id)
    assert info.version == 1
    client = mlflow.MiniClient(config=service.config)
    loaded = client.load_model(info.model_uri)
    assert loaded.predict([{"x": 3.0}]) == pytest.approx([6.0])
    by_alias = load_model(f"models:/linear-demo/1", client=client)
    assert by_alias.predict([{"x": 4.0}]) == pytest.approx([8.0])
    client.close()


def test_registry_crud_and_search(service) -> None:
    registry = service.registry
    model = registry.create_registered_model("demo", description="d", tags={"team": "a"})
    assert model.description == "d"
    with pytest.raises(ResourceAlreadyExists):
        registry.create_registered_model("demo")
    registry.create_registered_model("other", tags={"team": "b"})
    found = registry.search_registered_models("tags.team = 'a'")
    assert [item.name for item in found] == ["demo"]
    found = registry.search_registered_models("name LIKE 'dem%'")
    assert [item.name for item in found] == ["demo"]
    renamed = registry.rename_registered_model("demo", "renamed")
    assert renamed.name == "renamed"
    assert registry.get_registered_model("renamed").name == "renamed"
    with pytest.raises(ResourceNotFound):
        registry.get_registered_model("demo")
    registry.update_registered_model("renamed", description="new")
    assert registry.get_registered_model("renamed").description == "new"
    registry.set_registered_model_tag("renamed", "k", "v")
    assert registry.get_registered_model("renamed").tags["k"] == "v"
    registry.delete_registered_model_tag("renamed", "k")
    assert "k" not in registry.get_registered_model("renamed").tags


def test_registry_versions_aliases_stages(service) -> None:
    registry = service.registry
    registry.create_registered_model("m")
    run = service.start_run()
    v1 = registry.create_model_version("m", source="runs:/a/model", run_id=run.info.run_id)
    v2 = registry.create_model_version("m", source="runs:/b/model")
    assert (v1.version, v2.version) == (1, 2)
    registry.set_model_version_tag("m", 1, "k", "v")
    assert registry.get_model_version("m", 1).tags["k"] == "v"
    registry.transition_model_version_stage("m", 2, "production")
    assert registry.get_model_version("m", 2).stage == "Production"
    registry.set_registered_model_alias("m", "champion", 2)
    assert registry.get_model_version_by_alias("m", "champion").version == 2
    assert "champion" in registry.get_model_version("m", 2).aliases
    assert registry.get_registered_model("m").aliases == {"champion": 2}
    registry.delete_registered_model_alias("m", "champion")
    with pytest.raises(ResourceNotFound):
        registry.get_model_version_by_alias("m", "champion")
    with pytest.raises(InvalidParameter):
        registry.set_registered_model_alias("m", "123", 1)
    with pytest.raises(InvalidParameter):
        registry.transition_model_version_stage("m", 1, "prod")
    versions = registry.search_model_versions("version >= 2", name="m")
    assert [item.version for item in versions] == [2]
    with pytest.raises(InvalidParameter):
        registry.search_model_versions("name = 'm'", max_results=0)


def test_registry_delete_requires_force(service) -> None:
    registry = service.registry
    registry.create_registered_model("m")
    registry.create_model_version("m", source="runs:/x/model")
    with pytest.raises(InvalidParameter, match="force"):
        registry.delete_registered_model("m")
    registry.delete_registered_model("m", force=True)
    with pytest.raises(ResourceNotFound):
        registry.get_registered_model("m")


def test_registry_rejects_invalid_source_run(service) -> None:
    with pytest.raises(ResourceNotFound):
        service.registry.create_model_version("missing", source="runs:/x/model")
    service.registry.create_registered_model("m")
    with pytest.raises(ResourceNotFound):
        service.registry.create_model_version("m", source="runs:/x/model", run_id="0" * 32)


def test_registry_invalid_names(service) -> None:
    with pytest.raises(InvalidParameter):
        service.registry.create_registered_model("../bad")
    with pytest.raises(InvalidParameter):
        service.registry.create_registered_model(".bad")


def test_get_latest_versions_per_stage(service) -> None:
    registry = service.registry
    registry.create_registered_model("m")
    registry.create_model_version("m", source="runs:/a/model")
    registry.transition_model_version_stage("m", 1, "Production")
    registry.create_model_version("m", source="runs:/b/model")
    registry.transition_model_version_stage("m", 2, "Staging")
    latest = registry.get_latest_versions("m")
    assert {item.stage: item.version for item in latest} == {
        "Production": 1,
        "Staging": 2,
    }
    production = registry.get_latest_versions("m", stages=["Production"])
    assert [item.version for item in production] == [1]


def test_predict_positional_rows_with_signature(tmp_path: Path) -> None:
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    save_model(tmp_path / "m", model, input_example=[{"x": 3.0}])
    loaded = load_model(tmp_path / "m")
    assert loaded.predict([[4.0]]) == pytest.approx([8.0])
