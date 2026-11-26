from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mini_prometheus.config import ConfigError, load_config, parse_config
from mini_prometheus.util.timeutil import parse_duration


def base(**extra):
    data = {
        "global": {"scrape_interval": "30s", "scrape_timeout": "10s"},
        "scrape_configs": [
            {"job_name": "demo", "static_configs": [{"targets": ["localhost:9100"]}]}
        ],
    }
    data.update(extra)
    return data


def test_minimal_config():
    config = parse_config(base())
    assert config.global_config.scrape_interval == "30s"
    assert len(config.scrape_configs) == 1
    job = config.scrape_configs[0]
    assert job.job_name == "demo"
    assert job.scrape_interval == "30s"
    assert job.scrape_timeout == "10s"
    assert job.metrics_path == "/metrics"
    assert job.scheme == "http"


def test_global_defaults():
    config = parse_config({"scrape_configs": []})
    assert config.global_config.scrape_interval == "1m"
    assert config.global_config.scrape_timeout == "10s"
    assert config.global_config.evaluation_interval == "1m"


def test_global_rule_query_offset():
    config = parse_config({"scrape_configs": []})
    assert config.global_config.rule_query_offset == "0s"
    config = parse_config(
        {"global": {"rule_query_offset": "30s"}, "scrape_configs": []}
    )
    assert config.global_config.rule_query_offset == "30s"
    assert config.to_dict()["global"]["rule_query_offset"] == "30s"
    with pytest.raises(ConfigError):
        parse_config(
            {"global": {"rule_query_offset": "-1m"}, "scrape_configs": []}
        )


def test_timeout_exceeding_interval_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            {
                "global": {"scrape_interval": "1s", "scrape_timeout": "10s"},
                "scrape_configs": [],
            }
        )


def test_job_timeout_exceeding_interval_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            base(
                scrape_configs=[
                    {
                        "job_name": "x",
                        "scrape_interval": "5s",
                        "scrape_timeout": "10s",
                    }
                ]
            )
        )


def test_duplicate_job_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            base(
                scrape_configs=[
                    {"job_name": "x", "static_configs": [{"targets": ["a:1"]}]},
                    {"job_name": "x", "static_configs": [{"targets": ["b:2"]}]},
                ]
            )
        )


def test_invalid_duration():
    with pytest.raises(ConfigError):
        parse_config({"global": {"scrape_interval": "nonsense"}, "scrape_configs": []})


def test_unknown_key_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            base(scrape_configs=[{"job_name": "x", "unknown_key": 1}])
        )


def test_kubernetes_sd_rejected_with_clear_message():
    with pytest.raises(ConfigError) as exc:
        parse_config(
            base(
                scrape_configs=[
                    {
                        "job_name": "k8s",
                        "kubernetes_sd_configs": [{"role": "pod"}],
                    }
                ]
            )
        )
    assert "kubernetes_sd" in str(exc.value)


def test_invalid_scheme():
    with pytest.raises(ConfigError):
        parse_config(base(scrape_configs=[{"job_name": "x", "scheme": "ftp"}]))


def test_metrics_path_must_start_with_slash():
    with pytest.raises(ConfigError):
        parse_config(base(scrape_configs=[{"job_name": "x", "metrics_path": "metrics"}]))


def test_params_normalisation():
    config = parse_config(
        base(
            scrape_configs=[
                {
                    "job_name": "x",
                    "params": {"a": "1", "b": ["2", "3"]},
                }
            ]
        )
    )
    assert config.scrape_configs[0].params == {"a": ["1"], "b": ["2", "3"]}


def test_file_sd_config():
    config = parse_config(
        base(
            scrape_configs=[
                {
                    "job_name": "x",
                    "file_sd_configs": [{"files": ["a.yml"], "refresh_interval": "1m"}],
                }
            ]
        )
    )
    assert config.scrape_configs[0].file_sd_configs[0].files == ["a.yml"]


def test_http_sd_requires_url():
    with pytest.raises(ConfigError):
        parse_config(
            base(scrape_configs=[{"job_name": "x", "http_sd_configs": [{}]}])
        )


def test_dns_sd_requires_names():
    with pytest.raises(ConfigError):
        parse_config(
            base(scrape_configs=[{"job_name": "x", "dns_sd_configs": [{}]}])
        )


def test_relabel_validation():
    with pytest.raises(ConfigError):
        parse_config(
            base(
                scrape_configs=[
                    {
                        "job_name": "x",
                        "relabel_configs": [{"action": "replace"}],
                    }
                ]
            )
        )
    with pytest.raises(ConfigError):
        parse_config(
            base(
                scrape_configs=[
                    {
                        "job_name": "x",
                        "relabel_configs": [{"action": "hashmod", "target_label": "a"}],
                    }
                ]
            )
        )


def test_alerting_and_remote_config():
    config = parse_config(
        base(
            alerting={
                "alertmanagers": [
                    {
                        "static_configs": [{"targets": ["am:9093"]}],
                        "basic_auth": {"username": "u", "password": "p"},
                    }
                ]
            },
            remote_write=[{"url": "http://rw:9201/write", "name": "primary"}],
            remote_read=[{"url": "http://rr:9201/read"}],
        )
    )
    assert config.alerting[0].targets == ["am:9093"]
    assert config.alerting[0].basic_auth.username == "u"
    assert config.remote_write[0].url == "http://rw:9201/write"
    assert config.remote_read[0].url == "http://rr:9201/read"


def test_load_config_file(tmp_path):
    path = tmp_path / "prometheus.yml"
    path.write_text(yaml.safe_dump(base()), encoding="utf-8")
    config = load_config(path)
    assert config.source == str(path)
    assert config.scrape_configs[0].job_name == "demo"


def test_load_config_invalid_yaml(tmp_path):
    path = tmp_path / "prometheus.yml"
    path.write_text("scrape_configs: [", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.yml")


def test_to_dict_roundtrip():
    config = parse_config(base())
    data = config.to_dict()
    again = parse_config(data)
    assert again.scrape_configs[0].job_name == "demo"
    assert again.global_config.scrape_interval == "30s"


def test_external_labels():
    config = parse_config(
        {"global": {"external_labels": {"cluster": "c1"}}, "scrape_configs": []}
    )
    assert config.global_config.external_labels == {"cluster": "c1"}


def test_rule_files_parsed():
    config = parse_config(base(rule_files=["rules/*.yml"]))
    assert config.rule_files == ["rules/*.yml"]


def test_invalid_relabel_regex_rejected_at_load():
    with pytest.raises(ConfigError):
        parse_config(
            base(
                scrape_configs=[
                    {
                        "job_name": "x",
                        "relabel_configs": [
                            {
                                "source_labels": ["a"],
                                "target_label": "b",
                                "regex": "[",
                            }
                        ],
                    }
                ]
            )
        )


def test_file_sd_entry_must_be_mapping():
    with pytest.raises(ConfigError):
        parse_config(
            base(scrape_configs=[{"job_name": "x", "file_sd_configs": ["oops"]}])
        )
