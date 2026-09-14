"""Timeline —— 把「时间轴上发生了什么」画出来。

分布式系统里最难回答的问题之一是「**这段时间集群在干什么?**」。
日志是流水账、指标是聚合值,而 timeline 是**并行时间线**:
哪些任务在跑、谁在等、worker 什么时候起来的、什么时候闲下来了。

Ray 的 ``ray.timeline(filename)`` 输出 Chrome Trace Event JSON,
可以直接拖进 ``chrome://tracing`` 或 Perfetto 看。mini-ray 输出同样的格式,
**另外**附送一个自包含的 HTML 甘特图(不需要任何外部依赖,双击就能看)。

.. code-block:: python

    import miniray as ray
    ...
    ray.timeline("timeline.json")        # Chrome Trace JSON
    ray.timeline(html="timeline.html")   # 自包含 HTML(内嵌 SVG)
"""

from __future__ import annotations

import html as _html
import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import runtime

__all__ = ["timeline", "to_chrome_trace", "render_html"]


def timeline(
    filename: Optional[str] = None,
    *,
    html: Optional[str] = None,
    events: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """导出时间线。

    :param filename: Chrome Trace JSON 的输出路径(``.json``)。
        传 ``None`` 时不写文件,只返回事件列表。
    :param html: 额外输出一个自包含的 HTML 甘特图。
    :return: 原始事件列表(可以自己二次加工)。
    """
    if events is None:
        worker = runtime.get_core_worker()
        events = worker._raylet.get_timeline_events()
    if filename is not None:
        trace = to_chrome_trace(events)
        with open(filename, "w", encoding="utf-8") as handle:
            json.dump(trace, handle, ensure_ascii=False, indent=2)
    if html is not None:
        render_html(events, html)
    return events


def to_chrome_trace(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """转成 Chrome Trace Event Format(``chrome://tracing`` / Perfetto 能读)。

    每个 task 变成一根 ``X``(complete)事件条:``ts``/``dur`` 单位是微秒。
    ``pid`` 用节点序号、``tid`` 用 worker 序号 —— 这样在 trace viewer 里
    不同节点/worker 会自动分成不同的行,一眼看出并行度。
    """
    # 建立 节点/worker → 行号 的映射
    node_index: Dict[str, int] = {}
    worker_index: Dict[str, int] = {}
    for event in events:
        node_id = event.get("node_id")
        if node_id and node_id not in node_index:
            node_index[node_id] = len(node_index)
        worker_id = event.get("worker_id")
        if worker_id and worker_id not in worker_index:
            worker_index[worker_id] = len(worker_index)
    node_index.setdefault("unknown", len(node_index))

    # 收集 task 的时间跨度
    spans: Dict[str, Dict[str, Any]] = {}
    for event in events:
        kind = event.get("kind")
        task_id = event.get("task_id")
        if task_id is None:
            continue
        if kind == "task_scheduled":
            spans.setdefault(task_id, {})["start"] = event["ts"]
            spans[task_id]["name"] = event.get("name", "task")
            spans[task_id]["node_id"] = event.get("node_id", "unknown")
            spans[task_id]["worker_id"] = event.get("worker_id", "unknown")
        elif kind == "task_finished":
            span = spans.setdefault(task_id, {})
            span["end"] = event["ts"]
            span["duration"] = event.get("duration")
        elif kind in ("task_failed", "task_retry"):
            span = spans.setdefault(task_id, {})
            span["end"] = event["ts"]
            span["failed"] = True

    trace_events: List[Dict[str, Any]] = []
    for task_id, span in spans.items():
        start = span.get("start")
        end = span.get("end")
        if start is None:
            continue
        if end is None:
            end = start
        trace_events.append(
            {
                "name": span.get("name", "task"),
                "cat": "task",
                "ph": "X",
                "ts": int(start * 1_000_000),
                "dur": max(0, int((end - start) * 1_000_000)),
                "pid": node_index.get(span.get("node_id", "unknown"), 0),
                "tid": worker_index.get(span.get("worker_id", "unknown"), 0),
                "args": {"task_id": task_id, "worker_id": span.get("worker_id")},
            }
        )
    return {"traceEvents": trace_events, "displayTimeUnit": "ms"}


# ---------------------------------------------------------------------------
# HTML 甘特图
# ---------------------------------------------------------------------------

_ROW_HEIGHT = 18
_LABEL_WIDTH = 260
_CHART_WIDTH = 900
_PADDING = 16
_COLORS = {
    "task": "#4f7cff",
    "actor": "#b06bff",
    "failed": "#e35d6a",
    "worker": "#38b48b",
}


def _collect_spans(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    spans: Dict[str, Dict[str, Any]] = {}
    for event in events:
        kind = event.get("kind")
        task_id = event.get("task_id")
        if task_id is None:
            continue
        if kind == "task_scheduled":
            spans[task_id] = {
                "task_id": task_id,
                "name": event.get("name", "task"),
                "start": event["ts"],
                "end": None,
                "state": "RUNNING",
                "node_id": event.get("node_id", ""),
                "worker_id": event.get("worker_id", ""),
            }
        elif kind == "task_finished" and task_id in spans:
            spans[task_id]["end"] = event["ts"]
            spans[task_id]["state"] = "FINISHED"
        elif kind == "task_failed" and task_id in spans:
            spans[task_id]["end"] = event["ts"]
            spans[task_id]["state"] = "FAILED"
        elif kind == "task_retry" and task_id in spans:
            spans[task_id]["end"] = event["ts"]
            spans[task_id]["state"] = "RETRY"
    order = sorted(spans.values(), key=lambda span: span["start"])
    return order


def render_html(events: List[Dict[str, Any]], path: str) -> str:
    """把事件渲染成自包含的 HTML(内嵌 SVG 甘特图),写到 ``path``。"""
    spans = _collect_spans(events)
    actors = [
        {
            "name": event.get("actor_id", ""),
            "start": event["ts"],
            "end": None,
            "state": event.get("kind", ""),
        }
        for event in events
        if event.get("kind") == "actor_created"
    ]
    rows: List[Dict[str, Any]] = []
    for index, span in enumerate(spans):
        rows.append(
            {
                "index": index,
                "label": f"{span['name']}",
                "sub": span["task_id"][:8],
                "start": span["start"],
                "end": span["end"] or span["start"],
                "state": span["state"],
                "node": span["node_id"][:8],
                "worker": (span["worker_id"] or "")[:12],
            }
        )
    if not rows:
        rows = [
            {
                "index": 0,
                "label": "(没有任务事件)",
                "sub": "",
                "start": 0.0,
                "end": 0.0,
                "state": "FINISHED",
                "node": "",
                "worker": "",
            }
        ]
    t0 = min(row["start"] for row in rows)
    t1 = max(row["end"] for row in rows)
    if t1 <= t0:
        t1 = t0 + 1e-3
    span_total = t1 - t0

    def x_of(ts: float) -> float:
        return _LABEL_WIDTH + (ts - t0) / span_total * (_CHART_WIDTH - _LABEL_WIDTH)

    height = _PADDING * 2 + _ROW_HEIGHT * len(rows) + 40
    svg: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_CHART_WIDTH + _PADDING * 2}" '
        f'height="{height}" font-family="ui-monospace, Menlo, Consolas, monospace" font-size="11">',
        f'<rect width="100%" height="100%" fill="#0f1117"/>',
    ]
    # 时间轴刻度
    for tick in range(6):
        x = _LABEL_WIDTH + tick * (_CHART_WIDTH - _LABEL_WIDTH) / 5
        ts = t0 + span_total * tick / 5
        svg.append(
            f'<line x1="{x:.1f}" y1="{_PADDING + 20}" x2="{x:.1f}" y2="{height - _PADDING}" '
            f'stroke="#2a2f3a" stroke-width="1"/>'
        )
        svg.append(
            f'<text x="{x:.1f}" y="{_PADDING + 12}" fill="#6b7280" text-anchor="middle">'
            f"{(ts - t0) * 1000:.1f}ms</text>"
        )
    for row in rows:
        y = _PADDING + 24 + row["index"] * _ROW_HEIGHT
        color = _COLORS["failed"] if row["state"] == "FAILED" else _COLORS["task"]
        x1, x2 = x_of(row["start"]), x_of(row["end"])
        width = max(2.0, x2 - x1)
        svg.append(
            f'<text x="{_PADDING}" y="{y + 12}" fill="#c9d1d9">{_html.escape(row["label"][:34])}'
            f'<tspan fill="#4b5563"> {_html.escape(row["sub"])}</tspan></text>'
        )
        svg.append(
            f'<rect x="{x1:.1f}" y="{y + 2}" width="{width:.1f}" height="{_ROW_HEIGHT - 6}" '
            f'rx="3" fill="{color}" fill-opacity="0.85"/>'
        )
        svg.append(
            f'<text x="{x1 + width + 6:.1f}" y="{y + 12}" fill="#6b7280">'
            f"{(row['end'] - row['start']) * 1000:.1f}ms  {_html.escape(row['worker'])}</text>"
        )
    svg.append("</svg>")

    finished = sum(1 for row in rows if row["state"] == "FINISHED")
    failed = sum(1 for row in rows if row["state"] == "FAILED")
    body = f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>mini-ray timeline</title>
<style>
  body {{ background:#0f1117; color:#c9d1d9; font-family: ui-sans-serif, system-ui, sans-serif;
         margin:0; padding:24px; }}
  h1 {{ font-size:18px; margin:0 0 4px; }}
  .meta {{ color:#6b7280; font-size:12px; margin-bottom:16px; }}
  .card {{ background:#161a23; border:1px solid #232936; border-radius:8px; padding:16px; }}
  code {{ color:#9ca3af; }}
</style>
</head>
<body>
  <h1>mini-ray timeline</h1>
  <div class="meta">
    共 {len(rows)} 个任务 · 完成 {finished} · 失败 {failed} ·
    时间跨度 {span_total * 1000:.1f}ms ·
    总事件 {len(events)} 条(actor 事件 {len(actors)} 条)
  </div>
  <div class="card">
    {''.join(svg)}
  </div>
</body>
</html>
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return body


def default_timeline_path() -> str:
    return os.path.join(tempfile.gettempdir(), "miniray_timeline.json")
