from __future__ import annotations

import pytest

from mini_prometheus.config import RelabelConfig
from mini_prometheus.labels import Labels
from mini_prometheus.scrape.relabel import RelabelError, relabel_samples, relabel_target


def rule(**kwargs):
    return RelabelConfig(**kwargs)


def test_replace_with_expansion():
    labels = {"__address__": "host1:9100", "job": "api"}
    result = relabel_target(
        labels,
        [rule(source_labels=["__address__"], regex="([^:]+):.*", target_label="host", replacement="$1")],
    )
    assert result["host"] == "host1"


def test_replace_named_group():
    result = relabel_target(
        {"a": "foo-bar"},
        [rule(source_labels=["a"], regex="(?P<x>foo)-(?P<y>bar)", target_label="z", replacement="${x}:${y}")],
    )
    assert result["z"] == "foo:bar"


def test_replace_is_fully_anchored():
    result = relabel_target(
        {"a": "xxhost"},
        [rule(source_labels=["a"], regex="host", target_label="b", replacement="yes")],
    )
    assert "b" not in result


def test_replace_empty_removes_label():
    result = relabel_target(
        {"a": "x", "b": "keep"},
        [rule(source_labels=["a"], regex="(.*)", target_label="b", replacement="")],
    )
    assert "b" not in result


def test_keep_and_drop():
    labels = {"__address__": "a:1", "env": "prod"}
    kept = relabel_target(labels, [rule(source_labels=["env"], regex="prod", action="keep")])
    assert kept is not None
    dropped = relabel_target(labels, [rule(source_labels=["env"], regex="prod", action="drop")])
    assert dropped is None


def test_hashmod():
    result = relabel_target(
        {"a": "foo"},
        [rule(source_labels=["a"], action="hashmod", modulus=4, target_label="shard")],
    )
    assert 0 <= int(result["shard"]) < 4
    # Upstream uses the first 8 bytes of MD5 as a big-endian uint64:
    # MD5("bar") = 37b51d194a7513e45b56f6524f2d51f2 -> 0 % 4.
    exact = relabel_target(
        {"a": "bar"},
        [rule(source_labels=["a"], action="hashmod", modulus=4, target_label="shard")],
    )
    assert exact["shard"] == "0"


def test_labelmap():
    result = relabel_target(
        {"__meta_k8s_pod": "p1", "__meta_k8s_ns": "default"},
        [rule(regex="__meta_k8s_(.*)", replacement="k8s_$1", action="labelmap")],
    )
    assert result["k8s_pod"] == "p1"
    assert result["k8s_ns"] == "default"


def test_labeldrop_and_labelkeep():
    dropped = relabel_target(
        {"job": "a", "tmp": "x"},
        [rule(regex="tmp", action="labeldrop")],
    )
    assert "tmp" not in dropped
    kept = relabel_target(
        {"job": "a", "tmp": "x"},
        [rule(regex="job", action="labelkeep")],
    )
    assert kept == {"job": "a"}


def test_lowercase_uppercase():
    assert relabel_target({"a": "AbC"}, [rule(source_labels=["a"], target_label="b", action="lowercase")])["b"] == "abc"
    assert relabel_target({"a": "AbC"}, [rule(source_labels=["a"], target_label="b", action="uppercase")])["b"] == "ABC"


def test_keepequal_and_dropequal():
    labels = {"a": "x", "b": "x"}
    assert relabel_target(labels, [rule(source_labels=["a"], target_label="b", action="keepequal")]) is not None
    assert relabel_target({"a": "x", "b": "y"}, [rule(source_labels=["a"], target_label="b", action="keepequal")]) is None
    assert relabel_target({"a": "x", "b": "y"}, [rule(source_labels=["a"], target_label="b", action="dropequal")]) is not None


def test_multiple_source_labels():
    result = relabel_target(
        {"a": "x", "b": "y"},
        [rule(source_labels=["a", "b"], regex="x;y", target_label="c", replacement="ok")],
    )
    assert result["c"] == "ok"


def test_custom_separator():
    result = relabel_target(
        {"a": "x", "b": "y"},
        [rule(source_labels=["a", "b"], separator="-", regex="x-y", target_label="c", replacement="ok")],
    )
    assert result["c"] == "ok"


def test_relabel_samples_drop():
    labels = Labels({"__name__": "x", "tmp": "1"})
    assert relabel_samples(labels, [rule(source_labels=["tmp"], regex="1", action="drop")]) is None


def test_relabel_samples_rename_metric():
    labels = Labels({"__name__": "old"})
    result = relabel_samples(
        labels,
        [rule(source_labels=["__name__"], regex="old", target_label="__name__", replacement="new")],
    )
    assert result.get_name() == "new"


def test_relabel_samples_empty_name_dropped():
    labels = Labels({"__name__": "x"})
    assert relabel_samples(labels, [rule(source_labels=["__name__"], target_label="__name__", replacement="")]) is None


def test_invalid_regex():
    with pytest.raises(RelabelError):
        relabel_target({"a": "x"}, [rule(source_labels=["a"], regex="[", target_label="b")])
