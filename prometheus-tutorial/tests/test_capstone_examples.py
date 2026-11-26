"""Validate the runnable configuration used by the capstone chapters."""

from __future__ import annotations

from pathlib import Path

from mini_prometheus.config import load_config
from mini_prometheus.promtool import run_test_file
from mini_prometheus.rules.manager import load_rule_groups

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples" / "config" / "capstone.yml"
RULE_TEST = ROOT / "examples" / "rules" / "capstone_test.yml"


def test_capstone_config_and_rules_load():
    config = load_config(str(CONFIG))
    assert len(config.scrape_configs) == 2
    groups = load_rule_groups(
        config.rule_files,
        base_dir=str(CONFIG.parent),
        evaluation_interval_ms=5_000,
    )
    assert len(groups) == 1
    assert len(groups[0].rules) == 3


def test_capstone_rule_test_suite_passes():
    outcomes = run_test_file(RULE_TEST)
    assert outcomes, "capstone rule test suite should contain at least one case"
    assert all(outcome.passed for outcome in outcomes), outcomes
