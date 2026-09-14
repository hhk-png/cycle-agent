"""可观测性测试:State API、资源视图、timeline(JSON + HTML)、对象存储统计。"""

from __future__ import annotations

import json
import os
import time

import pytest

import miniray as ray
from miniray import state


@ray.remote
def work(seconds: float = 0.05):
    time.sleep(seconds)
    return "done"


@ray.remote
class Worker:
    def __init__(self):
        self.n = 0

    def inc(self):
        self.n += 1
        return self.n


# ---------------------------------------------------------------------------


def test_state_api_shapes(cluster):
    ray.get([work.remote(0.01) for _ in range(3)])
    actor = Worker.remote()
    ray.get(actor.inc.remote())

    tasks = state.list_tasks()
    assert tasks and all(
        {"task_id", "name", "state", "node_id", "num_attempts", "duration"} <= set(task)
        for task in tasks
    )

    objects = state.list_objects()
    assert objects and {"object_id", "state", "node_id", "refs"} <= set(objects[0])

    actors = state.list_actors()
    assert len(actors) == 1 and actors[0]["state"] == "ALIVE"
    assert actors[0]["mailbox"] == {"": 0}

    workers = state.list_workers()
    assert workers and {"worker_id", "pid", "state", "num_tasks_executed"} <= set(workers[0])

    nodes = state.list_nodes()
    assert len(nodes) == 1 and "utilization" in nodes[0]


def test_summaries(cluster):
    ray.get([work.remote(0.01) for _ in range(5)])
    summary = state.summarize_tasks()
    assert summary["total"] >= 5
    assert summary["by_state"].get("FINISHED", 0) >= 5
    assert summary["duration_total_s"] > 0

    objects = state.summarize_objects()
    assert objects["num_objects"] >= 5
    assert objects["used_bytes"] > 0
    assert "counters" in objects

    actors = state.summarize_actors()
    assert "by_state" in actors


def test_cluster_and_available_resources(cluster):
    resources = ray.cluster_resources()
    assert resources["CPU"] == pytest.approx(4.0)
    assert any(key.startswith("node:") for key in resources)

    available = ray.available_resources()
    assert available["CPU"] == pytest.approx(4.0)
    # 资源被占满时也要有这个键(值为 0),而不是 KeyError
    ray.get(work.remote(0.4))
    assert "CPU" in ray.available_resources()


def test_runtime_context(cluster):
    context = ray.get_runtime_context()
    assert context.get_worker_id() and context.get_node_id() and context.get_job_id()
    assert context.get_namespace() == "default"

    @ray.remote
    def describe():
        ctx = ray.get_runtime_context()
        return {
            "worker": ctx.get_worker_id(),
            "node": ctx.get_node_id(),
            "job": ctx.get_job_id(),
        }

    inside = ray.get(describe.remote())
    assert inside["job"] == context.get_job_id()
    node_ids = {node["NodeID"] for node in ray.nodes()}
    assert inside["node"] in node_ids, "worker 报的 node_id 必须是集群里的某个节点"
    assert inside["worker"] != context.get_worker_id()


def test_timeline_json(cluster, tmp_path):
    ray.get([work.remote(0.02) for _ in range(3)])
    path = str(tmp_path / "timeline.json")
    events = ray.timeline(path)
    assert events, "应该记下了一些事件"

    with open(path, encoding="utf-8") as handle:
        trace = json.load(handle)
    assert "traceEvents" in trace
    spans = [event for event in trace["traceEvents"] if event["ph"] == "X"]
    assert len(spans) >= 3
    assert all(span["dur"] >= 0 and span["tid"] >= 0 for span in spans)
    assert any("work" in span["name"] for span in spans)


def test_timeline_html(cluster, tmp_path):
    ray.get([work.remote(0.02) for _ in range(2)])
    path = str(tmp_path / "timeline.html")
    html = ray.timeline(html=path)
    assert html is None or isinstance(html, list)  # timeline() 返回事件列表

    with open(path, encoding="utf-8") as handle:
        content = handle.read()
    assert "<svg" in content and "mini-ray timeline" in content
    assert content.count("<rect") >= 2, "每个任务应该有一根条形"


def test_object_store_stats(cluster):
    ray.put({"payload": "x" * 1000})
    stats = state.summarize_objects()
    assert stats["capacity_bytes"] > 0
    assert stats["counters"]["added"] >= 1


def test_worker_pool_state(cluster):
    ray.get(work.remote(0.01))
    workers = state.list_workers()
    assert any(worker["state"] in ("IDLE", "BUSY") for worker in workers)
    assert any(worker["num_tasks_executed"] >= 1 for worker in workers)


def test_actor_mailbox_is_visible(cluster):
    actor = Worker.remote()
    ray.get([actor.inc.remote() for _ in range(3)])
    info = state.list_actors()[0]
    assert info["inflight"] == {"": 0}
    assert info["mailbox"] == {"": 0}
