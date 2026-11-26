"""Run every example scenario in order and fail on the first error."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SCENARIOS = [
    ("01", "promql tour", "01_promql_tour"),
    ("02", "scrape + rules", "02_scrape_and_rules"),
    ("03", "storage lifecycle", "03_storage_lifecycle"),
    ("04", "HTTP API", "04_api_server"),
    ("05", "alerting + Alertmanager-lite", "05_alerting"),
    ("06", "remote write/read", "06_remote_write"),
    ("07", "file_sd + relabel", "07_relabel_and_sd"),
    ("08", "staleness + failures", "08_staleness_and_failures"),
    ("09", "rule unit tests", "09_rule_unit_tests"),
    ("10", "OTLP ingestion", "10_otlp_ingest"),
]


def main() -> int:
    for number, title, module_name in SCENARIOS:
        print(f"\n=== {number}: {title} ===")
        module = importlib.import_module(module_name)
        module.run()
    print(f"\n{len(SCENARIOS)} / {len(SCENARIOS)} example scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
