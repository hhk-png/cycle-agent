"""File-based service discovery plus relabeling."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_bootstrap_root = str(_Path(__file__).resolve().parent.parent)
if _bootstrap_root not in _sys.path:
    _sys.path.insert(0, _bootstrap_root)



import tempfile
from pathlib import Path

import yaml

from mini_prometheus.config import parse_config
from mini_prometheus.scrape.discovery import DiscoveryManager


def run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        sd = Path(directory) / "targets.yml"
        sd.write_text(
            yaml.safe_dump(
                [
                    {
                        "targets": ["10.0.0.1:9100", "10.0.0.2:9100"],
                        "labels": {"rack": "r1", "env": "prod"},
                    },
                    {
                        "targets": ["10.0.0.3:9100"],
                        "labels": {"rack": "r2", "env": "prod"},
                    },
                ]
            )
        )
        config = parse_config(
            {
                "scrape_configs": [
                    {
                        "job_name": "node",
                        "file_sd_configs": [{"files": [str(sd)]}],
                        "relabel_configs": [
                            {
                                "source_labels": ["rack"],
                                "regex": "r1",
                                "action": "keep",
                            },
                            {
                                "source_labels": ["__address__"],
                                "regex": "([^:]+):(.*)",
                                "target_label": "host",
                                "replacement": "$1",
                            },
                            {
                                "source_labels": ["host"],
                                "target_label": "instance",
                            },
                            {
                                "regex": "__meta_filepath",
                                "action": "labeldrop",
                            },
                        ],
                    }
                ]
            }
        )
        discovery = DiscoveryManager(base_dir=directory)
        targets = discovery.targets_for(config.scrape_configs[0])
        print(f"discovered and relabeled targets: {len(targets)}")
        for target in targets:
            visible = {
                key: value
                for key, value in target.labels.items()
                if not key.startswith("__")
            }
            print(f"  {visible}")
        sd.write_text(yaml.safe_dump([{"targets": [], "labels": {}}]))
        print(f"after empty file: {len(discovery.targets_for(config.scrape_configs[0]))} targets")


if __name__ == "__main__":
    run()
