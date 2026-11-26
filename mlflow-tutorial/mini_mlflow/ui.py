"""Server-rendered HTML UI.

The UI is intentionally server-side and dependency-free.  It is a debugging
and storytelling surface, not a replacement for the API.  Every value that
came from user input is escaped before interpolation; no template engine means
no template-language injection surface.
"""

from __future__ import annotations

import html
import json
from typing import Any, Iterable
from urllib.parse import quote

from .entities import Experiment, FileInfo, Run, Trace
from .tracking import TrackingService
from .utils import format_timestamp

_CSS = """
:root {
  --bg:#f5f7fb; --panel:#ffffff; --ink:#172033; --muted:#5c6780;
  --line:#dce2ee; --accent:#2952cc; --accent-2:#eef2ff;
  --ok:#137a4b; --warn:#a35a00; --err:#b42318; --mono:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
header { background:#101a33; color:#fff; padding:14px 24px; }
header .brand { font-weight:700; font-size:18px; color:#fff; }
header nav { display:inline-flex; gap:18px; margin-left:28px; }
header nav a { color:#c9d5ff; }
main { max-width:1180px; margin:26px auto; padding:0 18px; }
h1 { font-size:26px; margin:0 0 6px; letter-spacing:-.02em; }
h2 { font-size:18px; margin:28px 0 10px; }
h3 { font-size:15px; margin:18px 0 8px; }
.sub { color:var(--muted); margin:0 0 18px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }
.card .num { font-size:28px; font-weight:700; letter-spacing:-.03em; }
.card .label { color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.04em; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; margin:14px 0; overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; color:var(--muted); font-weight:600; border-bottom:1px solid var(--line);
  padding:8px 10px; white-space:nowrap; }
td { border-bottom:1px solid #edf0f6; padding:8px 10px; vertical-align:top; }
tr:last-child td { border-bottom:0; }
code, .mono { font-family:var(--mono); font-size:12.5px; }
code { background:#f0f3fa; padding:1px 5px; border-radius:4px; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600; }
.FINISHED,.OK { background:#e7f6ee; color:var(--ok); }
.FAILED,.ERROR { background:#feeceb; color:var(--err); }
.RUNNING { background:#fff4df; color:var(--warn); }
.KILLED { background:#f0eefc; color:#5b46a8; }
.chips { display:flex; gap:6px; flex-wrap:wrap; }
.chip { background:var(--accent-2); color:#26408b; border-radius:6px; padding:2px 7px; font-size:12.5px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:12px; }
.kv { display:grid; grid-template-columns:145px 1fr; gap:6px 10px; font-size:14px; }
.kv dt { color:var(--muted); } .kv dd { margin:0; overflow-wrap:anywhere; }
form.search { display:flex; gap:8px; margin:12px 0; }
input[type=text] { width:min(620px,70vw); border:1px solid var(--line); border-radius:8px;
  padding:9px 11px; background:#fff; font-family:var(--mono); font-size:13px; }
button { border:1px solid var(--accent); background:var(--accent); color:white; border-radius:8px;
  padding:9px 14px; font-weight:600; cursor:pointer; }
button.secondary { background:#fff; color:var(--accent); }
pre { background:#101a33; color:#dfe7ff; padding:14px; border-radius:10px; overflow:auto;
  font-family:var(--mono); font-size:12.5px; line-height:1.5; }
.empty { color:var(--muted); padding:20px 4px; }
.error { background:#feeceb; border:1px solid #f5c2be; color:#7a2018; border-radius:10px; padding:12px 14px; }
.spanrow { display:grid; grid-template-columns:230px 1fr 95px; gap:10px; align-items:center;
  border-bottom:1px solid #edf0f6; padding:7px 0; font-size:13px; }
.bar { height:12px; background:var(--accent); border-radius:3px; min-width:2px; opacity:.75; }
.muted { color:var(--muted); }
.small { font-size:12.5px; }
@media (max-width:700px) { header nav { display:flex; margin:10px 0 0; } .kv { grid-template-columns:110px 1fr; } }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, content: str, *, subtitle: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · MiniMLflow</title><style>{_CSS}</style></head>
<body>
<header><span class="brand">MiniMLflow</span>
<nav><a href="/ui">概览</a><a href="/ui/traces">Traces</a><a href="/ui/registry">Registry</a>
<a href="/ui/prompts">Prompts</a><a href="/health">健康检查</a></nav></header>
<main><h1>{_e(title)}</h1><p class="sub">{_e(subtitle)}</p>{content}</main>
</body></html>"""


def _error_page(message: str) -> str:
    return _page("请求失败", f'<div class="error">{_e(message)}</div>', subtitle="可返回上一页修改搜索条件。")


def render_error(message: str) -> str:
    """Public error renderer used by the HTTP layer."""
    return _error_page(message)


def _status_badge(status: str) -> str:
    return f'<span class="badge {_e(status)}">{_e(status)}</span>'


def _run_link(run: Run) -> str:
    name = run.info.run_name or run.info.run_id[:10]
    return f'<a href="/ui/runs/{_e(run.info.run_id)}">{_e(name)}</a>'


def _experiment_link(experiment: Experiment) -> str:
    return f'<a href="/ui/experiments/{_e(experiment.experiment_id)}">{_e(experiment.name)}</a>'


def _chips(tags: dict[str, Any], *, limit: int = 12) -> str:
    if not tags:
        return '<span class="muted">—</span>'
    items = list(tags.items())[:limit]
    return '<div class="chips">' + "".join(
        f'<span class="chip">{_e(key)}={_e(value)}</span>' for key, value in items
    ) + "</div>"


def render_home(service: TrackingService, *, filter_string: str | None = None) -> str:
    try:
        experiments = service.store.list_experiments()
        runs, _ = service.search_runs(filter_string, max_results=50)
        traces = service.search_traces(max_results=10)
    except Exception as exc:  # noqa: BLE001 - UI renders the message
        return _error_page(str(exc))
    cards = f"""
<div class="cards">
  <div class="card"><div class="num">{len(experiments)}</div><div class="label">Experiments</div></div>
  <div class="card"><div class="num">{len(traces)}</div><div class="label">Recent traces</div></div>
  <div class="card"><div class="num">SQLite</div><div class="label">Metadata backend</div></div>
  <div class="card"><div class="num">file://</div><div class="label">Artifact store</div></div>
</div>"""
    exp_rows = "".join(
        f"<tr><td>{_experiment_link(experiment)}</td>"
        f"<td><code>{_e(experiment.experiment_id)}</code></td>"
        f"<td>{_e(format_timestamp(experiment.creation_time))}</td>"
        f"<td>{_chips(experiment.tags, limit=4)}</td></tr>"
        for experiment in experiments
    ) or '<tr><td colspan="4" class="empty">暂无 Experiment</td></tr>'
    run_rows = "".join(
        f"<tr><td>{_run_link(run)}</td>"
        f"<td>{_status_badge(run.info.status)}</td>"
        f"<td>{_e(run.info.experiment_id)}</td>"
        f"<td>{_e(format_timestamp(run.info.start_time))}</td>"
        f"<td>{len(run.data.metrics)}</td></tr>"
        for run in runs
    ) or '<tr><td colspan="5" class="empty">没有匹配的 run</td></tr>'
    trace_rows = "".join(
        f'<tr><td><a href="/ui/traces/{_e(trace.trace_id)}">{_e(trace.name)}</a></td>'
        f"<td>{_status_badge(trace.status)}</td><td>{_e(trace.experiment_id)}</td>"
        f"<td>{trace.latency_ms:.1f} ms</td><td>{len(trace.spans)}</td></tr>"
        for trace in traces
    ) or '<tr><td colspan="5" class="empty">还没有 trace</td></tr>'
    return _page(
        "实验与运行",
        f"""
{cards}
<form class="search" method="get" action="/ui">
  <input type="text" name="filter" value="{_e(filter_string or '')}"
    placeholder='例如 metrics.accuracy > 0.9 and tags.team = "nlp"'>
  <button type="submit">搜索 runs</button>
  <a href="/ui"><button type="button" class="secondary">清空</button></a>
</form>
<h2>Experiments</h2>
<div class="panel"><table><thead><tr><th>名称</th><th>ID</th><th>创建时间</th><th>标签</th></tr></thead>
<tbody>{exp_rows}</tbody></table></div>
<h2>Runs</h2>
<div class="panel"><table><thead><tr><th>Run</th><th>状态</th><th>Experiment</th><th>开始时间</th><th>指标数</th></tr></thead>
<tbody>{run_rows}</tbody></table></div>
<h2>最近 Traces</h2>
<div class="panel"><table><thead><tr><th>名称</th><th>状态</th><th>Experiment</th><th>延迟</th><th>Span 数</th></tr></thead>
<tbody>{trace_rows}</tbody></table></div>
""",
        subtitle="一个可以本地运行、端到端解释 MLflow 数据模型的教学实现。",
    )


def render_experiment(
    service: TrackingService, experiment_id: str, *, filter_string: str | None = None
) -> str:
    try:
        experiment = service.get_experiment(experiment_id)
        filter_parts = [f"attributes.experiment_id = {int(experiment.experiment_id)}"]
        if filter_string:
            filter_parts.append(f"({filter_string})")
        runs, _ = service.search_runs(" and ".join(filter_parts), max_results=200)
    except Exception as exc:  # noqa: BLE001
        return _error_page(str(exc))
    rows = "".join(
        f"<tr><td>{_run_link(run)}</td><td>{_status_badge(run.info.status)}</td>"
        f"<td>{_e(format_timestamp(run.info.start_time))}</td>"
        f"<td>{_e(format_timestamp(run.info.end_time))}</td>"
        f"<td>{_chips(run.data.params, limit=6)}</td>"
        f"<td>{_e(', '.join(f'{k}={v:.4g}' for k, v in list(run.data.metrics.items())[:4]))}</td></tr>"
        for run in runs
    ) or '<tr><td colspan="6" class="empty">该 experiment 下没有 run</td></tr>'
    return _page(
        experiment.name,
        f"""
<div class="panel"><dl class="kv">
<dt>Experiment ID</dt><dd><code>{_e(experiment.experiment_id)}</code></dd>
<dt>生命周期</dt><dd>{_e(experiment.lifecycle_stage)}</dd>
<dt>Artifact location</dt><dd><code>{_e(experiment.artifact_location)}</code></dd>
<dt>创建时间</dt><dd>{_e(format_timestamp(experiment.creation_time))}</dd>
<dt>标签</dt><dd>{_chips(experiment.tags)}</dd>
</dl></div>
<form class="search" method="get" action="/ui/experiments/{_e(experiment.experiment_id)}">
  <input type="text" name="filter" value="{_e(filter_string or '')}" placeholder="再叠加 run 过滤条件">
  <button type="submit">筛选</button>
</form>
<div class="panel"><table><thead><tr><th>Run</th><th>状态</th><th>开始</th><th>结束</th><th>参数</th><th>指标</th></tr></thead>
<tbody>{rows}</tbody></table></div>
""",
        subtitle=f"Experiment 详情 · {len(runs)} 个 run",
    )


def _artifact_listing(service: TrackingService, run_id: str) -> str:
    try:
        files = service.list_artifacts(run_id)
    except Exception as exc:  # noqa: BLE001
        return f'<div class="error">{_e(exc)}</div>'
    if not files:
        return '<div class="empty">无 artifact</div>'
    rows = []
    for item in files:
        if item.is_dir:
            rows.append(
                f"<tr><td><code>{_e(item.path)}/</code></td><td>目录</td><td></td></tr>"
            )
        else:
            url = f"/api/2.0/mlflow/artifacts/get?run_id={quote(run_id)}&path={quote(item.path)}"
            rows.append(
                f'<tr><td><a href="{_e(url)}"><code>{_e(item.path)}</code></a></td>'
                f"<td>文件</td><td>{item.file_size:,} B</td></tr>"
            )
    return (
        "<table><thead><tr><th>路径</th><th>类型</th><th>大小</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_run(service: TrackingService, run_id: str) -> str:
    try:
        run = service.get_run(run_id)
        experiment = service.get_experiment(run.info.experiment_id)
        histories = {
            key: service.get_metric_history(run_id, key) for key in list(run.data.metrics)[:8]
        }
    except Exception as exc:  # noqa: BLE001
        return _error_page(str(exc))
    param_rows = "".join(
        f"<tr><td><code>{_e(key)}</code></td><td><code>{_e(value)}</code></td></tr>"
        for key, value in sorted(run.data.params.items())
    ) or '<tr><td colspan="2" class="empty">无参数</td></tr>'
    metric_rows = "".join(
        f"<tr><td><code>{_e(key)}</code></td><td>{value:.10g}</td>"
        f"<td>{len(histories.get(key, []))}</td></tr>"
        for key, value in sorted(run.data.metrics.items())
    ) or '<tr><td colspan="3" class="empty">无指标</td></tr>'
    input_rows = "".join(
        f"<tr><td>{_e(item.dataset_name)}</td><td><code>{_e(item.dataset_digest)}</code></td>"
        f"<td>{_e(item.dataset_source_type)}</td><td>{_e(item.context)}</td></tr>"
        for item in run.inputs
    ) or '<tr><td colspan="4" class="empty">未记录输入数据集</td></tr>'
    history_rows = []
    for key, points in histories.items():
        values = ", ".join(f"step {point.step}: {point.value:.6g}" for point in points[-12:])
        history_rows.append(f"<tr><td><code>{_e(key)}</code></td><td><code>{_e(values)}</code></td></tr>")
    history_html = (
        "<table><thead><tr><th>指标</th><th>最近 12 个点</th></tr></thead><tbody>"
        + "".join(history_rows)
        + "</tbody></table>"
    ) if history_rows else '<div class="empty">无指标历史</div>'
    duration = (
        f"{(run.info.end_time - run.info.start_time) / 1000:.3f} s"
        if run.info.end_time
        else "进行中"
    )
    return _page(
        run.info.run_name or run.info.run_id[:12],
        f"""
<div class="panel"><dl class="kv">
<dt>Run ID</dt><dd><code>{_e(run.info.run_id)}</code></dd>
<dt>Experiment</dt><dd>{_experiment_link(experiment)}</dd>
<dt>状态</dt><dd>{_status_badge(run.info.status)}</dd>
<dt>Artifact URI</dt><dd><code>{_e(run.info.artifact_uri)}</code></dd>
<dt>开始</dt><dd>{_e(format_timestamp(run.info.start_time))}</dd>
<dt>结束</dt><dd>{_e(format_timestamp(run.info.end_time))}</dd>
<dt>耗时</dt><dd>{_e(duration)}</dd>
<dt>用户</dt><dd>{_e(run.info.user_id)}</dd>
<dt>标签</dt><dd>{_chips(run.data.tags)}</dd>
</dl></div>
<div class="grid">
<div class="panel"><h2>参数</h2><table><tbody>{param_rows}</tbody></table></div>
<div class="panel"><h2>指标（latest）</h2><table><thead><tr><th>键</th><th>值</th><th>历史点数</th></tr></thead>
<tbody>{metric_rows}</tbody></table></div>
</div>
<div class="panel"><h2>指标历史</h2>{history_html}</div>
<div class="panel"><h2>输入数据</h2><table><thead><tr><th>名称</th><th>摘要</th><th>类型</th><th>上下文</th></tr></thead>
<tbody>{input_rows}</tbody></table></div>
<div class="panel"><h2>Artifacts</h2>{_artifact_listing(service, run_id)}</div>
""",
        subtitle=f"Run 详情 · experiment {experiment.experiment_id}",
    )


def render_traces(service: TrackingService, *, filter_string: str | None = None) -> str:
    try:
        traces = service.search_traces(filter_string=filter_string, max_results=100)
    except Exception as exc:  # noqa: BLE001
        return _error_page(str(exc))
    rows = "".join(
        f'<tr><td><a href="/ui/traces/{_e(trace.trace_id)}"><code>{_e(trace.trace_id[:12])}</code></a></td>'
        f"<td>{_e(trace.name)}</td><td>{_status_badge(trace.status)}</td>"
        f"<td>{_e(trace.experiment_id)}</td><td>{trace.latency_ms:.1f} ms</td>"
        f"<td>{len(trace.spans)}</td><td>{_e(format_timestamp(trace.timestamp))}</td></tr>"
        for trace in traces
    ) or '<tr><td colspan="7" class="empty">没有 trace</td></tr>'
    return _page(
        "Traces",
        f"""
<form class="search" method="get" action="/ui/traces">
  <input type="text" name="filter" value="{_e(filter_string or '')}"
    placeholder='例如 status = "ERROR" and tags.environment = "prod"'>
  <button type="submit">筛选</button>
</form>
<div class="panel"><table><thead><tr><th>Trace ID</th><th>名称</th><th>状态</th><th>Experiment</th>
<th>延迟</th><th>Span</th><th>时间</th></tr></thead><tbody>{rows}</tbody></table></div>
""",
        subtitle="按 trace 粒度查看一次 LLM 调用或 Agent 执行。",
    )


def render_trace(service: TrackingService, trace_id: str) -> str:
    try:
        trace = service.get_trace(trace_id)
    except Exception as exc:  # noqa: BLE001
        return _error_page(str(exc))
    start = min((span.start_time_ns for span in trace.spans), default=0)
    total = max((span.end_time_ns or span.start_time_ns for span in trace.spans), default=0) - start
    total = max(total, 1)
    span_rows = []
    for span in sorted(trace.spans, key=lambda item: item.start_time_ns):
        offset = (span.start_time_ns - start) / total * 100
        width = max(((span.end_time_ns or span.start_time_ns) - span.start_time_ns) / total * 100, 0.8)
        indent = 14 if span.parent_span_id else 0
        span_rows.append(
            f'<div class="spanrow"><div style="padding-left:{indent}px" title="{_e(span.span_id)}">'
            f"<strong>{_e(span.name)}</strong><br><span class=\"muted small\">{_e(span.span_type)}</span></div>"
            f'<div><div class="bar" style="margin-left:{offset:.2f}%;width:{width:.2f}%"></div></div>'
            f"<div>{span.latency_ms if span.latency_ms is not None else 0:.1f} ms</div></div>"
        )
    payload = json.dumps(
        {"request": trace.request, "response": trace.response}, ensure_ascii=False, indent=2
    )
    assessment_rows = "".join(
        f"<tr><td><code>{_e(item.name)}</code></td><td>{_e(repr(item.value))}</td>"
        f"<td>{_e(item.source_type)}</td><td>{_e(item.source_id or '—')}</td>"
        f"<td>{_e(item.rationale or '—')}</td>"
        f"<td>{_e(format_timestamp(item.last_update_time))}</td></tr>"
        for item in trace.assessments
    ) or '<tr><td colspan="6" class="empty">还没有 feedback/expectation</td></tr>'
    return _page(
        trace.name,
        f"""
<div class="panel"><dl class="kv">
<dt>Trace ID</dt><dd><code>{_e(trace.trace_id)}</code></dd>
<dt>状态</dt><dd>{_status_badge(trace.status)}</dd>
<dt>Experiment</dt><dd>{_e(trace.experiment_id)}</dd>
<dt>总延迟</dt><dd>{trace.latency_ms:.1f} ms</dd>
<dt>Span 数</dt><dd>{len(trace.spans)}</dd>
<dt>标签</dt><dd>{_chips(trace.tags)}</dd>
</dl></div>
<div class="panel"><h2>Span 瀑布</h2>{''.join(span_rows) or '<div class="empty">无 span</div>'}</div>
<div class="panel"><h2>Feedback / Expectations</h2>
<table><thead><tr><th>名称</th><th>值</th><th>来源类型</th><th>来源</th><th>理由</th><th>更新时间</th></tr></thead>
<tbody>{assessment_rows}</tbody></table></div>
<div class="panel"><h2>请求 / 响应</h2><pre>{_e(payload)}</pre></div>
""",
        subtitle="Trace 详情 · 仅显示时间轴，不依赖前端图表库。",
    )


def render_registry(service: TrackingService) -> str:
    try:
        models = service.registry.search_registered_models(max_results=200)
        blocks = []
        for model in models:
            versions = service.registry.search_model_versions(name=model.name, max_results=200)
            alias = ", ".join(f"{key}→v{value}" for key, value in sorted(model.aliases.items())) or "—"
            rows = "".join(
                f"<tr><td>v{version.version}</td><td>{_status_badge(version.status)}</td>"
                f"<td>{_e(version.stage)}</td><td><code>{_e(version.run_id[:12] or '—')}</code></td>"
                f"<td><code>{_e(version.source)}</code></td>"
                f"<td>{_chips(version.tags, limit=4)}</td></tr>"
                for version in versions
            ) or '<tr><td colspan="6" class="empty">无版本</td></tr>'
            blocks.append(
                f'<div class="panel"><h2>{_e(model.name)}</h2>'
                f'<p class="muted">{_e(model.description or "无描述")} · aliases: <code>{_e(alias)}</code></p>'
                f"<table><thead><tr><th>版本</th><th>状态</th><th>Stage</th><th>Run</th>"
                f"<th>Source</th><th>标签</th></tr></thead><tbody>{rows}</tbody></table></div>"
            )
    except Exception as exc:  # noqa: BLE001
        return _error_page(str(exc))
    content = "".join(blocks) or '<div class="panel"><div class="empty">Registry 为空</div></div>'
    return _page("Model Registry", content, subtitle="版本、别名与来源血缘。")


def render_prompts(service: TrackingService) -> str:
    try:
        latest = service.prompts.search_prompts(max_results=200)
    except Exception as exc:  # noqa: BLE001
        return _error_page(str(exc))
    blocks = []
    for prompt in latest:
        versions = service.prompts.list_versions(prompt.name)
        alias_map = {
            alias: version.version
            for version in versions
            for alias in version.aliases
        }
        aliases = ", ".join(
            f"{alias}→v{version}" for alias, version in sorted(alias_map.items())
        ) or "—"
        rows = "".join(
            f"<tr><td>v{item.version}</td><td><code>{_e(item.template[:120])}</code></td>"
            f"<td>{_e(', '.join(item.variables) or '—')}</td>"
            f"<td>{_e(', '.join(item.aliases) or '—')}</td>"
            f"<td>{_e(item.commit_message or '—')}</td>"
            f"<td>{_e(format_timestamp(item.creation_timestamp))}</td></tr>"
            for item in versions
        )
        blocks.append(
            f'<div class="panel"><h2>{_e(prompt.name)}</h2>'
            f'<p class="muted">最新 v{prompt.version} · aliases: <code>{_e(aliases)}</code></p>'
            "<table><thead><tr><th>版本</th><th>模板</th><th>变量</th><th>别名</th>"
            f"<th>提交说明</th><th>创建时间</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    content = "".join(blocks) or '<div class="panel"><div class="empty">Prompt Registry 为空</div></div>'
    return _page(
        "Prompt Registry",
        content,
        subtitle="版本不可变，别名可变；模板只做 {{ variable }} 替换。",
    )
