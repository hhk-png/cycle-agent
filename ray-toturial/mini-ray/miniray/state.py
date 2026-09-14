"""State API —— 可观测性(对齐 ``ray.util.state``)。

Ray 的可观测性有四层,mini-ray 实现了其中最常用的两层的简化版:

============================  ==========================================
Ray 的组件                     mini-ray 对应物
============================  ==========================================
Dashboard(Web UI)             无(见下面「为什么不做 dashboard」)
State API(``ray.util.state``) :mod:`miniray.state`(本文件)
``ray memory``                 ``list_objects()``
Timeline / Tracing             :mod:`miniray.timeline`
============================  ==========================================

**为什么不做 dashboard?** 因为它需要 HTTP 服务 + 前端资源 + 常驻进程,
而它的价值几乎全部来自「背后的那张状态表」。mini-ray 直接把表暴露成 Python API,
再配一个 :mod:`miniray.timeline` 的自包含 HTML 甘特图 —— 教学场景下更实用。

.. code-block:: python

    from miniray import state
    state.list_tasks()          # 每个 task 的状态/耗时/重试次数
    state.list_objects()        # 每个对象的所在节点/引用计数/是否落盘
    state.list_actors()         # actor 状态/邮箱积压/重启次数
    state.summarize_tasks()     # 聚合视图(先看这个)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import runtime

__all__ = [
    "list_tasks",
    "list_objects",
    "list_actors",
    "list_nodes",
    "list_workers",
    "list_placement_groups",
    "summarize_tasks",
    "summarize_objects",
    "summarize_actors",
    "get_state",
]


def _raylet_state() -> Dict[str, Any]:
    worker = runtime.get_core_worker()
    return worker._raylet.get_state()


def get_state() -> Dict[str, Any]:
    """整个集群的状态快照(原始的、最全的那一份)。"""
    return _raylet_state()


def list_nodes() -> List[Dict[str, Any]]:
    """节点列表:资源总量/可用量/利用率。"""
    return _raylet_state()["nodes"]


def list_tasks() -> List[Dict[str, Any]]:
    """所有 task 的状态。

    字段:``task_id`` / ``name`` / ``state`` / ``node_id`` / ``worker_id`` /
    ``num_attempts``(重试次数)/ ``duration`` / ``error``。
    """
    worker = runtime.get_core_worker()
    return worker._raylet.list_tasks()


def list_objects() -> List[Dict[str, Any]]:
    """所有被跟踪的对象:状态、所在节点、生产它的 task、引用计数。

    **排查「内存为什么下不去」就看这个**:找 ``refs`` 一直大于 0 的对象,
    它们是被某个地方漏掉的引用钉住了。
    """
    worker = runtime.get_core_worker()
    return worker._raylet.list_objects()


def list_actors() -> List[Dict[str, Any]]:
    """所有 actor 的状态(含邮箱积压、重启次数、在飞请求数)。"""
    return _raylet_state()["actors"]


def list_workers() -> List[Dict[str, Any]]:
    """worker 进程池的状态(pid / 状态 / 已执行任务数 / 空闲多久)。"""
    worker = runtime.get_core_worker()
    return worker._raylet.list_workers()


def list_placement_groups() -> List[Dict[str, Any]]:
    worker = runtime.get_core_worker()
    return worker._gcs.call("list_placement_groups")


def summarize_tasks() -> Dict[str, Any]:
    """task 的聚合视图:各状态的个数 + 耗时分布。"""
    tasks = list_tasks()
    by_state: Dict[str, int] = {}
    durations = []
    retried = 0
    for task in tasks:
        by_state[task["state"]] = by_state.get(task["state"], 0) + 1
        if task.get("duration") is not None:
            durations.append(task["duration"])
        if task.get("num_attempts", 0) > 1:
            retried += 1
    return {
        "total": len(tasks),
        "by_state": by_state,
        "retried": retried,
        "duration_total_s": round(sum(durations), 4),
        "duration_max_s": round(max(durations), 4) if durations else 0.0,
    }


def summarize_objects() -> Dict[str, Any]:
    """对象的聚合视图:个数、总字节、溢出/驱逐计数。"""
    state = _raylet_state()
    store = state["object_store"]
    counters: Dict[str, int] = {}
    for stats in store["per_node"].values():
        for key, value in stats["counters"].items():
            counters[key] = counters.get(key, 0) + value
    return {
        "num_objects": store["total_objects"],
        "used_bytes": store["total_used_bytes"],
        "capacity_bytes": sum(
            stats["capacity_bytes"] for stats in store["per_node"].values()
        ),
        "counters": counters,
        "num_reconstructions": state.get("num_reconstructions", 0),
    }


def summarize_actors() -> Dict[str, Any]:
    actors = list_actors()
    by_state: Dict[str, int] = {}
    for actor in actors:
        by_state[actor["state"]] = by_state.get(actor["state"], 0) + 1
    return {
        "total": len(actors),
        "by_state": by_state,
        "total_restarts": sum(actor.get("num_restarts", 0) for actor in actors),
    }
