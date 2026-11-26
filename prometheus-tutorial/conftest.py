from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_prometheus.labels import Labels  # noqa: E402
from mini_prometheus.promql.engine import Engine, InMemoryQuerier  # noqa: E402
from mini_prometheus.tsdb import TSDB, TSDBOptions  # noqa: E402


@pytest.fixture()
def tsdb(tmp_path):
    database = TSDB(
        TSDBOptions(
            path=str(tmp_path / "data"),
            retention_ms=365 * 24 * 3600 * 1000,
            block_range_ms=60 * 60 * 1000,
            wal_segment_size=64 * 1024,
            use_lockfile=False,
        )
    ).open()
    yield database
    database.close()


@pytest.fixture()
def querier():
    return InMemoryQuerier()


@pytest.fixture()
def engine(querier):
    return Engine(querier)


def series(labels: dict, points: list[tuple[int, float]]):
    return Labels(labels), points
