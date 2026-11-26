from __future__ import annotations

from mini_prometheus.__main__ import build_parser

import subprocess
import sys
from pathlib import Path

import yaml

from mini_prometheus.labels import Labels
from mini_prometheus.tsdb import TSDB, TSDBOptions


def run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "mini_prometheus", *args],
        capture_output=True,
        text=True,
        cwd=cwd or str(Path(__file__).resolve().parent.parent),
        timeout=60,
    )


def write_config(tmp_path, **extra):
    data = {
        "global": {"scrape_interval": "30s", "scrape_timeout": "10s"},
        "scrape_configs": [
            {"job_name": "demo", "static_configs": [{"targets": ["localhost:9100"]}]}
        ],
    }
    data.update(extra)
    path = tmp_path / "prometheus.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_version():
    result = run("--version")
    assert result.returncode == 0
    assert "mini-prometheus" in result.stdout


def test_check_config_ok(tmp_path):
    rules = tmp_path / "rules.yml"
    rules.write_text(
        yaml.safe_dump(
            {"groups": [{"name": "g", "rules": [{"record": "r", "expr": "up"}]}]}
        )
    )
    path = write_config(tmp_path, rule_files=[str(rules)])
    result = run("check", "config", str(path))
    assert result.returncode == 0, result.stderr
    assert "SUCCESS" in result.stdout
    assert "1 scrape job" in result.stdout


def test_check_config_invalid(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("scrape_configs: [{job_name: x, scrape_timeout: 1h, scrape_interval: 1s}]")
    result = run("check", "config", str(path))
    assert result.returncode == 1
    assert "FAILED" in result.stderr


def test_check_rules_ok(tmp_path):
    rules = tmp_path / "rules.yml"
    rules.write_text(
        yaml.safe_dump(
            {
                "groups": [
                    {
                        "name": "g",
                        "rules": [
                            {"record": "a", "expr": "up"},
                            {"alert": "B", "expr": "up == 0"},
                        ],
                    }
                ]
            }
        )
    )
    result = run("check", "rules", str(rules))
    assert result.returncode == 0
    assert "2 rule(s)" in result.stdout


def test_check_rules_invalid(tmp_path):
    rules = tmp_path / "rules.yml"
    rules.write_text("groups: [{name: g, rules: [{record: r, expr: 'not(('}]}]")
    result = run("check", "rules", str(rules))
    assert result.returncode == 1


def test_tsdb_list(tmp_path):
    path = tmp_path / "data"
    database = TSDB(
        TSDBOptions(path=str(path), use_lockfile=False, block_range_ms=1000)
    ).open()
    database.append(Labels({"__name__": "x"}), 1000, 1)
    database.flush_head(force=True)
    database.close()
    result = run("tsdb", "list", "--storage.tsdb.path", str(path))
    assert result.returncode == 0
    assert "TSDB blocks" in result.stdout
    assert "series=1" in result.stdout


def test_tsdb_analyze(tmp_path):
    path = tmp_path / "data"
    database = TSDB(TSDBOptions(path=str(path), use_lockfile=False)).open()
    for index in range(10):
        database.append(Labels({"__name__": "x"}), index * 1000, float(index))
    database.flush_head(force=True)
    database.close()
    result = run("tsdb", "analyze", "--storage.tsdb.path", str(path))
    assert result.returncode == 0
    assert "samples=10" in result.stdout


def test_subcommand_usage_errors():
    assert run("check").returncode == 2
    assert run("tsdb").returncode == 2
    assert run("query").returncode == 2


def test_check_rules_relative_path():
    result = run("check", "rules", "examples/rules/demo.yml")
    assert result.returncode == 0, result.stderr
    assert "SUCCESS" in result.stdout


def test_otlp_cli_flags_parse():
    args = build_parser().parse_args(
        [
            "--web.enable-otlp-receiver",
            "--enable-feature",
            "otlp-deltatocumulative,other-feature",
            "--storage.otlp.translation-strategy",
            "UnderscoreEscapingWithSuffixes",
        ]
    )
    assert args.web_enable_otlp_receiver is True
    assert args.enable_features == ["otlp-deltatocumulative,other-feature"]
    assert args.otlp_translation_strategy == "UnderscoreEscapingWithSuffixes"
