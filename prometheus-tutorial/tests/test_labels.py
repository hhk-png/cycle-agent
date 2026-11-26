from __future__ import annotations

import pytest

from mini_prometheus.labels import (
    LabelError,
    Labels,
    Matcher,
    labels_from_string,
    match_all,
)


def test_empty_values_are_dropped():
    labels = Labels([("a", ""), ("b", "1")])
    assert labels.to_dict() == {"b": "1"}
    assert "a" not in labels


def test_labels_are_sorted_and_deduplicated():
    labels = Labels([("z", "1"), ("a", "2"), ("m", "3")])
    assert list(labels.items()) == [("a", "2"), ("m", "3"), ("z", "1")]


def test_duplicate_label_conflict_raises():
    with pytest.raises(LabelError):
        Labels([("a", "1"), ("a", "2")])


def test_duplicate_identical_label_is_allowed():
    assert Labels([("a", "1"), ("a", "1")]).get("a") == "1"


def test_merge_override_and_removal():
    left = Labels([("a", "1"), ("b", "2")])
    right = Labels([("b", "3"), ("c", "4"), ("d", "")])
    merged = left.merge(right)
    assert merged.to_dict() == {"a": "1", "b": "3", "c": "4"}


def test_without_and_keep():
    labels = Labels([("__name__", "x"), ("job", "a"), ("instance", "b")])
    assert labels.without("__name__").to_dict() == {"job": "a", "instance": "b"}
    assert labels.keep("job").to_dict() == {"job": "a"}


def test_get_name():
    assert Labels([("__name__", "http_requests_total")]).get_name() == "http_requests_total"
    assert Labels.empty().get_name() == ""


def test_labels_hash_and_equality():
    assert Labels([("a", "1")]) == Labels([("a", "1")])
    assert hash(Labels([("a", "1")])) == hash(Labels([("a", "1")]))
    assert Labels([("a", "1")]) != Labels([("a", "2")])


def test_labels_ordering():
    assert Labels([("a", "1")]) < Labels([("b", "1")])


def test_matcher_equality():
    assert Matcher("job", "=", "api").matches("api")
    assert not Matcher("job", "=", "api").matches("db")
    assert Matcher("job", "!=", "api").matches("db")
    assert not Matcher("job", "!=", "api").matches("api")


def test_matcher_missing_label_uses_empty_string():
    assert Matcher("job", "=", "").matches("")
    assert Matcher("job", "!=", "api").matches("")


def test_matcher_regex_is_fully_anchored():
    matcher = Matcher("code", "=~", "5..")
    assert matcher.matches("500")
    assert not matcher.matches("x500")
    assert not matcher.matches("5000")


def test_matcher_negative_regex():
    matcher = Matcher("code", "!~", "5..")
    assert matcher.matches("200")
    assert not matcher.matches("500")


def test_matcher_invalid_regex():
    with pytest.raises(LabelError):
        Matcher("a", "=~", "[")


def test_match_all():
    labels = Labels([("job", "api"), ("code", "500")])
    assert match_all([Matcher("job", "=", "api")], labels)
    assert not match_all(
        [Matcher("job", "=", "api"), Matcher("code", "=", "200")], labels
    )


def test_labels_from_string():
    labels = labels_from_string('{job="api",code="500"}')
    assert labels.get("job") == "api" and labels.get("code") == "500"


def test_labels_from_string_bare_name():
    assert labels_from_string("up").get_name() == "up"


def test_labels_from_string_escapes():
    labels = labels_from_string(r'{path="C:\\tmp\n",quote="\""}')
    assert labels.get("path") == "C:\\tmp\n"
    assert labels.get("quote") == '"'


def test_labels_map_and_size():
    labels = Labels([("a", "1"), ("b", "22")])
    mapped = labels.map(lambda name, value: (name.upper(), value + "x"))
    assert mapped.to_dict() == {"A": "1x", "B": "22x"}
    assert labels.bytes_size() == 5
