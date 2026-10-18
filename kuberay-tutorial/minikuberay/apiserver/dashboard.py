"""A tiny dependency-free KubeRay dashboard.

It is intentionally a single HTML document that talks to the same REST API as
``kubectl`` would.  The point is to show that a UI is just another API client.
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MiniKubeRay Dashboard</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d; --text:#c9d1d9; --muted:#8b949e; --accent:#58a6ff; --ok:#3fb950; --warn:#d29922; --bad:#f85149; }
  body { margin:0; font:14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:var(--bg); color:var(--text); }
  header { padding:20px 28px; border-bottom:1px solid var(--line); display:flex; gap:18px; align-items:baseline; }
  h1 { font-size:20px; margin:0; } .sub { color:var(--muted); }
  main { padding:22px 28px; display:grid; gap:20px; }
  section { background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  h2 { font-size:14px; margin:0; padding:12px 16px; border-bottom:1px solid var(--line); color:var(--accent); }
  table { width:100%; border-collapse:collapse; } th,td { text-align:left; padding:8px 16px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; } tr:last-child td { border-bottom:0; }
  .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:20px; }
  footer { padding:10px 28px 30px; color:var(--muted); }
</style>
</head>
<body>
<header><h1>MiniKubeRay</h1><span class="sub">operator dashboard · <span id="updated">loading</span></span></header>
<main>
  <div class="grid">
    <section><h2>RayClusters</h2><table><thead><tr><th>Name</th><th>State</th><th>Workers</th><th>CPU</th><th>GPU</th></tr></thead><tbody id="clusters"></tbody></table></section>
    <section><h2>RayJobs</h2><table><thead><tr><th>Name</th><th>Job</th><th>Deployment</th><th>Cluster</th></tr></thead><tbody id="jobs"></tbody></table></section>
    <section><h2>RayServices</h2><table><thead><tr><th>Name</th><th>Ready</th><th>Active cluster</th><th>Endpoints</th></tr></thead><tbody id="services"></tbody></table></section>
    <section><h2>RayCronJobs</h2><table><thead><tr><th>Name</th><th>Schedule</th><th>Last schedule</th></tr></thead><tbody id="cronjobs"></tbody></table></section>
    <section><h2>History Server <span class="sub">(survives deletion)</span></h2><table><thead><tr><th>Cluster</th><th>Namespace</th><th>State</th><th>Events</th><th>Deleted</th></tr></thead><tbody id="history"></tbody></table></section>
  </div>
</main>
<footer>API: <a style="color:var(--accent)" href="/api/v1/namespaces/default/rayclusters">/api/v1/namespaces/default/rayclusters</a> · history: <a style="color:var(--accent)" href="/api/history/sessions">/api/history/sessions</a> · metrics: <a style="color:var(--accent)" href="/metrics">/metrics</a></footer>
<script>
const q = s => document.querySelector(s);
const token = new URLSearchParams(location.search).get('token');
const authHeaders = token ? {Authorization: 'Bearer ' + token} : {};
const esc = v => String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const stateClass = s => s === 'ready' || s === 'Running' || s === 'SUCCEEDED' ? 'ok' : (s === 'failed' || s === 'FAILED' ? 'bad' : 'warn');
async function get(path) { const r = await fetch(path, {headers: authHeaders}); if (!r.ok) throw new Error(path + ': ' + r.status); return (await r.json()).items || []; }
async function refresh() {
  try {
    const [clusters, jobs, services, cronjobs] = await Promise.all([
      get('/api/v1/namespaces/default/rayclusters'),
      get('/api/v1/namespaces/default/rayjobs'),
      get('/api/v1/namespaces/default/rayservices'),
      get('/api/v1/namespaces/default/raycronjobs')
    ]);
    q('#clusters').innerHTML = clusters.map(c => `<tr><td>${esc(c.metadata.name)}</td><td class="${stateClass(c.status?.state)}">${esc(c.status?.state || 'pending')}</td><td>${esc(c.status?.readyWorkerReplicas ?? 0)}/${esc(c.status?.desiredWorkerReplicas ?? 0)}</td><td>${esc(c.status?.desiredCPU || '-')}</td><td>${esc(c.status?.desiredGPU || '0')}</td></tr>`).join('') || '<tr><td colspan="5">none</td></tr>';
    q('#jobs').innerHTML = jobs.map(j => `<tr><td>${esc(j.metadata.name)}</td><td class="${stateClass(j.status?.jobStatus)}">${esc(j.status?.jobStatus || '-')}</td><td>${esc(j.status?.jobDeploymentStatus || '-')}</td><td>${esc(j.status?.rayClusterName || '-')}</td></tr>`).join('') || '<tr><td colspan="4">none</td></tr>';
    q('#services').innerHTML = services.map(s => `<tr><td>${esc(s.metadata.name)}</td><td class="${stateClass(s.status?.serviceStatus)}">${esc(s.status?.serviceStatus || 'NotReady')}</td><td>${esc(s.status?.activeServiceStatus?.rayClusterName || '-')}</td><td>${esc(s.status?.numServeEndpoints ?? 0)}</td></tr>`).join('') || '<tr><td colspan="4">none</td></tr>';
    q('#cronjobs').innerHTML = cronjobs.map(c => `<tr><td>${esc(c.metadata.name)}</td><td>${esc(c.spec?.schedule)}</td><td>${esc(c.status?.lastScheduleTime || '-')}</td></tr>`).join('') || '<tr><td colspan="3">none</td></tr>';
    const historyResponse = await fetch('/api/history/sessions', {headers: authHeaders});
    const sessions = historyResponse.ok ? ((await historyResponse.json()).sessions || []) : [];
    q('#history').innerHTML = sessions.map(s => `<tr><td>${esc(s.cluster)}</td><td>${esc(s.namespace)}</td><td class="${stateClass(s.state)}">${esc(s.state || '-')}</td><td>${esc(s.eventCount ?? 0)}</td><td>${esc(s.deletedAt || '-')}</td></tr>`).join('') || '<tr><td colspan="5">none</td></tr>';
    q('#updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (e) { q('#updated').textContent = e.message; }
}
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""
