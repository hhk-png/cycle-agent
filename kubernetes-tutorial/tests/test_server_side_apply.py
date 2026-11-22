"""Server-side apply: ownership, conflicts, force and stale-field removal."""

from __future__ import annotations

import pytest

from mini_k8s.errors import ApiError


def configmap(data: dict | None = None, name: str = "ssa") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": "default"},
        "data": data if data is not None else {"a": "1"},
    }


def managers(obj: dict) -> dict[str, dict]:
    return {
        entry["manager"]: entry
        for entry in obj.get("metadata", {}).get("managedFields", [])
    }


def test_apply_creates_and_records_managed_fields(api_cluster):
    created = api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-a")
    assert created["data"] == {"a": "1"}
    entry = managers(created)["team-a"]
    assert entry["operation"] == "Apply"
    assert entry["fieldsType"] == "FieldsV1"
    assert entry["fieldsV1"]["f:data"]["f:a"] == {}


def test_apply_same_manager_updates_without_conflict(api_cluster):
    api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-a")
    updated = api_cluster.api.apply(configmap({"a": "2", "b": "3"}), server_side=True, field_manager="team-a")
    assert updated["data"] == {"a": "2", "b": "3"}
    assert set(managers(updated)) == {"team-a"}


def test_apply_conflict_reports_owner_and_field(api_cluster):
    api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-a")
    with pytest.raises(ApiError) as exc:
        api_cluster.api.apply(configmap({"a": "2"}), server_side=True, field_manager="team-b")
    assert exc.value.status == 409
    assert "team-a" in exc.value.message
    assert ".data.a" in exc.value.message


def test_apply_same_value_does_not_conflict(api_cluster):
    api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-a")
    result = api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-b")
    assert result["data"]["a"] == "1"
    # The original owner keeps the field; applying the same value does not
    # silently transfer ownership.
    assert "f:a" in managers(result)["team-a"]["fieldsV1"]["f:data"]


def test_apply_force_takes_ownership(api_cluster):
    api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-a")
    result = api_cluster.api.apply(configmap({"a": "2"}), server_side=True,
                                   field_manager="team-b", force=True)
    assert result["data"]["a"] == "2"
    owners = managers(result)
    assert "team-a" not in owners
    assert "f:a" in owners["team-b"]["fieldsV1"]["f:data"]


def test_apply_removes_fields_omitted_by_the_same_manager(api_cluster):
    api_cluster.api.apply(configmap({"a": "1", "b": "2"}), server_side=True, field_manager="team-a")
    result = api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-a")
    assert result["data"] == {"a": "1"}
    assert "f:b" not in managers(result)["team-a"]["fieldsV1"].get("f:data", {})


def test_apply_null_deletes_a_field(api_cluster):
    api_cluster.api.apply(configmap({"a": "1", "b": "2"}), server_side=True, field_manager="team-a")
    result = api_cluster.api.apply(configmap({"a": "1", "b": None}), server_side=True, field_manager="team-a")
    assert result["data"] == {"a": "1"}
    assert "f:b" not in managers(result)["team-a"]["fieldsV1"].get("f:data", {})


def test_apply_requires_field_manager(api_cluster):
    api_cluster.api.apply(configmap(), server_side=True, field_manager="team-a")
    path = api_cluster.api.path_for("v1", "ConfigMap", "default", "ssa")
    with pytest.raises(ApiError) as exc:
        api_cluster.api.request(
            "PATCH", path, configmap({"a": "2"}),
            content_type="application/apply-patch+yaml",
        )
    assert exc.value.status == 400
    assert "fieldManager" in exc.value.message


def test_apply_ignores_client_managed_fields(api_cluster):
    body = configmap({"a": "1"})
    body["metadata"]["managedFields"] = [{
        "manager": "forged", "operation": "Apply", "fieldsType": "FieldsV1",
        "fieldsV1": {"f:data": {"f:a": {}}},
    }]
    result = api_cluster.api.apply(body, server_side=True, field_manager="team-a")
    assert set(managers(result)) == {"team-a"}


def test_apply_dry_run_does_not_persist(api_cluster):
    path = api_cluster.api.path_for("v1", "ConfigMap", "default", "ssa")
    result = api_cluster.api.request(
        "PATCH", path, configmap({"a": "1"}),
        content_type="application/apply-patch+yaml",
        params={"fieldManager": "team-a", "dryRun": "All"},
    )
    assert result["metadata"].get("uid") == "dry-run"
    with pytest.raises(ApiError) as exc:
        api_cluster.api.get("v1", "ConfigMap", "ssa")
    assert exc.value.status == 404


def test_plain_update_preserves_managed_fields(api_cluster):
    created = api_cluster.api.apply(configmap({"a": "1"}), server_side=True, field_manager="team-a")
    created["data"]["a"] = "9"
    updated = api_cluster.api.replace(created)
    assert updated["data"]["a"] == "9"
    assert "team-a" in managers(updated)


def test_apply_status_is_ignored_on_the_main_resource(api_cluster):
    body = configmap({"a": "1"})
    body["status"] = {"phase": "Running"}
    result = api_cluster.api.apply(body, server_side=True, field_manager="team-a")
    assert "status" not in result
