"""pytest 公共夹具。

约定:每个测试用一个**独立的小集群**(``num_cpus=4``),测完立刻 ``shutdown``。
集群启停大约几十毫秒,换来的是测试之间的完全隔离 —— 分布式框架的测试最怕
「上一个测试留下的 worker 还在跑」。
"""

from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import miniray as ray  # noqa: E402


@pytest.fixture()
def cluster():
    """默认集群:4 CPU、单节点、不回收空闲 worker(测试要确定性)。"""
    ray.init(num_cpus=4, logging_level="warning", worker_idle_timeout_ms=0)
    try:
        yield ray
    finally:
        ray.shutdown()


@pytest.fixture()
def make_cluster():
    """需要自定义参数的测试用这个(可以起多个不同配置的集群)。"""
    started = []

    def _make(**kwargs):
        kwargs.setdefault("logging_level", "warning")
        kwargs.setdefault("worker_idle_timeout_ms", 0)
        ray.init(**kwargs)
        started.append(True)
        return ray

    yield _make
    if started:
        ray.shutdown()
