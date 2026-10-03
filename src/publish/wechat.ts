import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import pc from 'picocolors';
import { loadArticles } from './articles.ts';
import { escapeHtml, WECHAT_CONTENT_LIMIT } from './markdown.ts';
import {
  acquireLock,
  ensureArticle,
  loadState,
  releaseLock,
  saveState,
  stateFileName,
  type PublishState,
} from './publish-state.ts';
import { dim, error, header, info, startSpinner, success } from '../shared/ui.ts';
import * as wechat from './api/wechat.ts';
import {
  configProblems,
  listWechatConfigNames,
  loadWechatConfig,
  pickWechatConfig,
  readAppSecret,
  type WechatConfig,
} from './wechat-config.ts';
import { buildIssues, type Issue } from './wechat-issues.ts';

/**
 * Publish tutorial chapters to a WeChat Official Account.
 *
 * ⚠️ Personal subscription accounts have no publish API(returns 48001), so this
 * stops at creating drafts; the final publish must be done in the backend(or via
 * its "scheduled publishing"). It does: Markdown→HTML(with a 20k-char limit check
 * and auto-splitting), a zero-key local preview page, draft creation via the API
 * (needs AppID/AppSecret), and a schedule table.
 *
 * Usage:
 *   node src/publish/publish.ts wechat --list            # issue list + length/title/digest precheck(offline)
 *   node src/publish/publish.ts wechat --build           # generate the preview page and HTML fragments(offline)
 *   node src/publish/publish.ts wechat --plan            # schedule table(offline)
 *   node src/publish/publish.ts wechat --ip              # look up this machine's public egress IP(for whitelisting)
 *   node src/publish/publish.ts wechat --check           # reconcile the draft box(read-only, requires keys)
 *   node src/publish/publish.ts wechat --drafts          # create drafts: after issue 1 it stops so you can check the layout
 *   node src/publish/publish.ts wechat --drafts --yes    # no more stops, create them all in one go
 *   node src/publish/publish.ts wechat --drafts --only 08 --force
 *
 * Exit codes: 0 done, 1 error, 2 paused at a confirmation point(state already saved to disk, resumable), 130 interrupted.
 */

const REPO_ROOT = process.cwd();

/** Output directory(already covered by .gitignore) */
const OUT_DIR = path.join(REPO_ROOT, '.wechat-out');

interface CliOptions {
  list: boolean;
  build: boolean;
  plan: boolean;
  check: boolean;
  drafts: boolean;
  ip: boolean;
  only: string | null;
  force: boolean;
  yes: boolean;
  debug: boolean;
  delayMs: number | null;
  configName: string | null;
}

function defaultOptions(): CliOptions {
  return {
    list: false,
    build: false,
    plan: false,
    check: false,
    drafts: false,
    ip: false,
    only: null,
    force: false,
    yes: false,
    debug: false,
    delayMs: null,
    configName: null,
  };
}

/** Hand-written argument parsing(consistent with publish.ts, no parsing library) */
function parseArgs(argv: string[]): CliOptions | { error: string } {
  const o = defaultOptions();
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const needValue = (): string | null => {
      const v = argv[++i];
      return v === undefined ? null : v;
    };
    switch (arg) {
      case '--list': o.list = true; break;
      case '--build': o.build = true; break;
      case '--plan': o.plan = true; break;
      case '--check': o.check = true; break;
      case '--drafts': o.drafts = true; break;
      case '--ip': o.ip = true; break;
      case '--force': o.force = true; break;
      case '--yes': case '-y': o.yes = true; break;
      case '--debug': o.debug = true; break;
      case '--only': {
        const v = needValue();
        if (v === null) return { error: '--only needs an issue number, e.g. --only 08 or --only 18-2' };
        o.only = v;
        break;
      }
      case '--delay': {
        const v = needValue();
        const n = Number(v);
        if (v === null || !Number.isFinite(n) || n < 0) return { error: '--delay needs a number of milliseconds, e.g. --delay 5000' };
        o.delayMs = n;
        break;
      }
      default: {
        if (arg.startsWith('--')) return { error: `Unknown argument: ${arg}` };
        if (o.configName) return { error: `Only one config name may be given, extra: ${arg}` };
        o.configName = arg;
      }
    }
  }
  return o;
}

/** `--only 8` / `--only 08` / `--only 18-2` are all accepted */
function normalizeIssueId(v: string): string {
  const m = /^(\d+)(?:-(\d+))?$/.exec(v.trim());
  if (!m) return v.trim();
  return m[2] ? `${m[1].padStart(2, '0')}-${m[2]}` : m[1].padStart(2, '0');
}

function buildHooks(opts: CliOptions): wechat.WechatHooks {
  if (!opts.debug) return {};
  return { onDebug: (msg: string) => dim(`  · ${msg}`) };
}

// ============ Dates ============

/** 'YYYY-MM-DD' plus a number of days(computed in UTC, so the time zone can't shift the date by a day) */
function addDays(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number);
  const t = new Date(Date.UTC(y, m - 1, d) + days * 86_400_000);
  return `${t.getUTCFullYear()}-${String(t.getUTCMonth() + 1).padStart(2, '0')}-${String(t.getUTCDate()).padStart(2, '0')}`;
}

function weekday(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number);
  return `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][new Date(Date.UTC(y, m - 1, d)).getUTCDay()]}`;
}

/** Publish date of issue n(1-based) */
function issueDate(startDate: string, order: number): string {
  return addDays(startDate, order - 1);
}

// ============ Issue loading ============

interface Loaded {
  cfgName: string;
  cfg: WechatConfig;
  issues: Issue[];
}

/** Load the config + scan the articles + assemble the issues(length/title/digest problems all surface at this step) */
async function loadIssues(opts: CliOptions): Promise<Loaded | { error: string }> {
  let cfgName: string;
  let cfg: WechatConfig;
  try {
    if (opts.configName) {
      cfgName = opts.configName;
      if (!listWechatConfigNames().includes(cfgName)) {
        return {
          error: `No WeChat config under src/publish/src/publish/configs/: ${cfgName}(available: ${listWechatConfigNames().join(', ') || 'none'})`,
        };
      }
      cfg = await loadWechatConfig(cfgName);
    } else {
      const picked = await pickWechatConfig();
      cfgName = picked.name;
      cfg = picked.config;
    }
  } catch (err) {
    return { error: (err as Error).message };
  }

  const loaded = loadArticles(
    cfg.sourceDir,
    cfg.filePrefix,
    cfg.fromNumber,
    cfg.titleSource ?? 'h1',
    cfg.titlePrefix ?? '',
  );
  if ('error' in loaded) return { error: loaded.error };

  const built = buildIssues(loaded.articles, {
    digests: cfg.digests,
    titleOverrides: cfg.titleOverrides,
    contentLimit: cfg.contentLimit,
  });
  if ('error' in built) return { error: built.error };

  return { cfgName, cfg, issues: built.issues };
}

function selectIssues(issues: Issue[], opts: CliOptions): Issue[] | { error: string } {
  if (!opts.only) return issues;
  const id = normalizeIssueId(opts.only);
  const found = issues.filter((i) => i.id === id || i.no === id);
  if (found.length === 0) {
    return { error: `No matching issue: ${opts.only}(available: ${issues.map((i) => i.id).join(', ')})` };
  }
  return found;
}

// ============ --list ============

function runList(cfg: WechatConfig, issues: Issue[]): number {
  header('WeChat Official Account issue list');
  const limit = issues[0]?.limit ?? WECHAT_CONTENT_LIMIT;
  info(
    `  ${issues.length} issues · body limit ${limit} chars` +
      `${limit < WECHAT_CONTENT_LIMIT ? `(API limit ${WECHAT_CONTENT_LIMIT}, the config leaves some headroom)` : ''}` +
      ` · start date ${cfg.startDate}`,
  );
  info('');

  let warned = 0;
  for (const issue of issues) {
    const len = issue.content.length;
    const ratio = (len / issue.limit) * 100;
    const mark =
      len > issue.limit ? pc.red('OVER') : ratio >= 98 ? pc.yellow('TIGHT') : pc.green('ok  ');
    const digest = issue.digest ? `${issue.digest.length} chars` : pc.yellow('none');
    info(
      `  ${String(issue.order).padStart(2)}  ${issue.id.padEnd(6)} ${mark} ${String(len).padStart(5)}(${String(Math.round(ratio)).padStart(3)}%)` +
        ` title ${String(issue.title.length).padStart(2)} chars digest ${digest}  ${issue.title}`,
    );
    for (const w of issue.warnings) {
      warned++;
      dim(`        ! ${w}`);
    }
  }

  const split = issues.filter((i) => i.parts > 1);
  const byArticle = new Map<string, number>();
  for (const i of split) byArticle.set(i.no, i.parts);
  const tight = issues.filter((i) => i.content.length / i.limit >= 0.98);
  info('');
  info(`  Split articles: ${[...byArticle].map(([no, p]) => `${no}→${p} issues`).join(' ') || 'none'}`);
  info(`  Longest single issue ${Math.max(...issues.map((i) => i.content.length))} chars`);
  if (tight.length > 0) {
    dim(
      `  Hugging the limit(≥98%): ${tight.map((i) => `issue ${i.order} ${i.content.length}`).join(' · ')} —` +
        ` 20,000 is the hard limit in the API docs, and these issues are sized to "just barely fit". If the API rejects them,` +
        ` set contentLimit in the config to 19000(leaving 5% headroom) and each of them will be split into one more part.`,
    );
  }
  if (warned > 0) dim(`  The above ${warned} warnings do not block publishing(--build/--drafts will proceed anyway)`);
  return 0;
}

// ============ --plan ============

function runPlan(cfg: WechatConfig, issues: Issue[]): number {
  header('Schedule table');
  info(`  Start date ${cfg.startDate}(${weekday(cfg.startDate)}) · ${issues.length} issues · 1 issue per day`);
  info(
    `  The Official Account backend's "scheduled publishing" **can only be set at most 7 days in advance**, so it takes ${Math.ceil(issues.length / 7)} rounds to schedule them all;` +
      ` the operating day for each round is the date of that batch's 1st issue.`,
  );
  info('');

  const PER_ROUND = 7;
  for (let r = 0; r * PER_ROUND < issues.length; r++) {
    const batch = issues.slice(r * PER_ROUND, (r + 1) * PER_ROUND);
    const first = issueDate(cfg.startDate, batch[0].order);
    const last = issueDate(cfg.startDate, batch[batch.length - 1].order);
    info(
      pc.bold(`  Round ${r + 1}`) +
        `  ${first}(${weekday(first)}) starts ${batch.length} issues → ${last}(${weekday(last)})  · operating day ${first}`,
    );
    for (const issue of batch) {
      const d = issueDate(cfg.startDate, issue.order);
      info(`      ${d}(${weekday(d)})  issue ${String(issue.order).padStart(2)}  ${issue.title}`);
    }
    info('');
  }

  const lastDate = issueDate(cfg.startDate, issues.length);
  info(`  The last issue lands on ${lastDate}(${weekday(lastDate)}).`);
  info(
    `  If you have already published one today and ${cfg.startDate} doesn't fit, just change startDate in the config to ${addDays(cfg.startDate, 1)}.`,
  );
  return 0;
}

// ============ --build ============

/** Preview page: lists all issues on one page, "copy body" issue by issue to paste straight into the Official Account editor */
function renderPreview(cfg: WechatConfig, cfgName: string, issues: Issue[]): string {
  const cards = issues
    .map((issue) => {
      const d = issueDate(cfg.startDate, issue.order);
      const warns = issue.warnings.map((w) => `<div class="warn">! ${escapeHtml(w)}</div>`).join('');
      const splitNote = issue.parts > 1 ? `<span class="tag">${issue.part}/${issue.parts}</span>` : '';
      return `
<section class="card">
  <div class="head">
    <div>
      <div class="eyebrow">Issue ${issue.order} · ${d}(${weekday(d)}) ${splitNote}</div>
      <h2 class="title">${escapeHtml(issue.title)}</h2>
      <div class="meta">body ${issue.content.length} chars / limit ${issue.limit} · title ${issue.title.length} chars / limit 32 · digest ${
        issue.digest ? `${issue.digest.length} chars` : 'none(auto-generated by the Official Account)'
      }</div>
    </div>
    <div class="actions">
      <button onclick="copyTitle(this, ${JSON.stringify(issue.title).replace(/"/g, '&quot;')})">Copy title</button>
      <button class="primary" onclick="copyBody(this, 'c-${issue.id}')">Copy body</button>
    </div>
  </div>
  ${warns}
  <details>
    <summary>Show digest</summary>
    <div class="digest">${escapeHtml(issue.digest || '(empty)')}</div>
  </details>
  <div class="phone">
    <div class="content" id="c-${issue.id}">${issue.content}</div>
  </div>
</section>`;
    })
    .join('\n');

  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>${escapeHtml(cfgName)} · WeChat Official Account preview(${issues.length} issues)</title>
<style>
  body { margin:0; background:#f2f3f5; font:14px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; color:#1f2329; }
  .page { max-width:860px; margin:0 auto; padding:24px 16px 80px; }
  .top { background:#fff; border-radius:8px; padding:16px 20px; margin-bottom:16px; border-left:4px solid #07c160; }
  .top h1 { margin:0 0 8px; font-size:18px; }
  .top p { margin:4px 0; color:#646a73; }
  .card { background:#fff; border-radius:8px; padding:16px 20px; margin-bottom:16px; }
  .head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
  .eyebrow { color:#07c160; font-size:12px; font-weight:600; }
  .tag { background:#e8f7ee; color:#07904a; border-radius:3px; padding:0 4px; font-weight:400; }
  .title { margin:4px 0 6px; font-size:16px; line-height:1.5; word-break:break-all; }
  .meta { color:#8a9099; font-size:12px; }
  .actions { display:flex; gap:8px; flex-shrink:0; }
  button { font:inherit; font-size:13px; padding:6px 12px; border:1px solid #d0d3d6; background:#fff; border-radius:4px; cursor:pointer; white-space:nowrap; }
  button:hover { border-color:#07c160; color:#07c160; }
  button.primary { background:#07c160; border-color:#07c160; color:#fff; }
  button.primary:hover { opacity:.9; color:#fff; }
  .warn { color:#c45656; font-size:12px; margin-top:8px; }
  details { margin-top:10px; }
  summary { cursor:pointer; color:#8a9099; font-size:12px; }
  .digest { color:#4e5969; background:#f7f8fa; border-radius:4px; padding:8px 10px; margin-top:6px; font-size:13px; }
  .phone { margin-top:12px; border:1px solid #e5e6eb; border-radius:6px; padding:16px; max-width:677px; }
  .content { font-size:15px; line-height:1.8; color:#333; overflow-wrap:break-word; }
  .content h2 { font-size:18px; margin:22px 0 10px; }
  .content h3 { font-size:16px; margin:18px 0 8px; }
  .content p { margin:12px 0; }
  .content img { max-width:100%; }
  .content a { color:#576b95; }
</style>
</head>
<body>
<div class="page">
  <div class="top">
    <h1>${escapeHtml(cfgName)} · WeChat Official Account preview</h1>
    <p>${issues.length} issues total, one per day, starting ${cfg.startDate}(${weekday(cfg.startDate)}).</p>
    <p><b>Usage</b>: click "Copy body" → in the Official Account backend go to "Drafts → New creation → Write a new article", then in the body area press <b>Ctrl+V</b> to paste(the formatting comes along), then paste the title and set the cover.</p>
    <p style="color:#c45656">Unverified personal subscription account: the body <b>cannot contain external hyperlinks</b>(external links get stripped), so links are currently rendered as plain "text (address)"; publishing can only be done by hand or via "scheduled publishing".</p>
  </div>
  ${cards}
</div>
<script>
function flash(btn, ok, okText) {
  var old = btn.textContent;
  btn.textContent = ok ? okText : 'Copy failed, please select manually';
  setTimeout(function () { btn.textContent = old; }, 1800);
}
function copyBody(btn, id) {
  var el = document.getElementById(id);
  if (!el) return flash(btn, false);
  var range = document.createRange();
  range.selectNodeContents(el);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  sel.removeAllRanges();
  flash(btn, ok, '✔ Copied');
}
function copyTitle(btn, text) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  flash(btn, ok, '✔ Copied');
}
</script>
</body>
</html>
`;
}

function runBuild(cfg: WechatConfig, cfgName: string, issues: Issue[]): number {
  const outDir = path.join(OUT_DIR, cfgName);
  mkdirSync(outDir, { recursive: true });

  for (const issue of issues) {
    writeFileSync(path.join(outDir, `${issue.id}.html`), issue.content, 'utf8');
    writeFileSync(path.join(outDir, `${issue.id}.md`), `# ${issue.title}\n\n${issue.markdown}\n`, 'utf8');
  }
  const preview = path.join(outDir, 'preview.html');
  writeFileSync(preview, renderPreview(cfg, cfgName, issues), 'utf8');

  header('Preview page generated');
  success(`  Preview page: ${path.relative(REPO_ROOT, preview).replace(/\\/g, '/')}`);
  info(`  Also saved ${issues.length} HTML fragments(the content used by the API) and the corresponding Markdown`);
  info('');
  info(`  Open the preview page in a browser → "copy body" issue by issue → paste into the Official Account editor`);
  info(`  (directory: ${path.relative(REPO_ROOT, outDir).replace(/\\/g, '/')}/)`);
  return 0;
}

// ============ State ============

/**
 * The WeChat state file **shares publish-state.ts's read/write and lock** with the
 * Juejin side(atomic writes, hard stop on corruption, pid lock), just under a
 * different namespace.
 *
 * Field mapping: `status` = pending/drafted/published(with `published` inferred by
 * `--check` when the draft is gone); `draftId` = the draft's media_id; `coverImage` =
 * the series-wide cover thumb_media_id(holds the cover URL on the Juejin side);
 * `articleId`/`url`/`publishAttemptedAt` are unused on personal accounts, kept null.
 */
function statePath(cfgName: string): string {
  return stateFileName(REPO_ROOT, cfgName, 'wechat-publish-state');
}

function buildOptions(cfg: WechatConfig, secret: string, opts: CliOptions): wechat.WechatOptions {
  return { appId: cfg.appId, appSecret: secret, timeoutMs: cfg.timeoutMs };
}

interface Ready {
  cfgName: string;
  cfg: WechatConfig;
  issues: Issue[];
  options: wechat.WechatOptions;
  state: PublishState;
  stateFile: string;
}

/** Subcommands that need keys go through here: validate config → read the secret → read state → take the lock */
function prepareOnline(loaded: Loaded, opts: CliOptions): Ready | { error: string } {
  const problems = configProblems(loaded.cfg);
  if (problems.length > 0) {
    return { error: `The WeChat config has problems:\n  - ${problems.join('\n  - ')}` };
  }
  const secretRes = readAppSecret(REPO_ROOT, loaded.cfg);
  if ('error' in secretRes) return { error: secretRes.error };
  dim(`  Credentials: appId ${loaded.cfg.appId} · AppSecret ${secretRes.source}`);

  const stateFile = statePath(loaded.cfgName);
  const loadedState = loadState(stateFile);
  if ('error' in loadedState) return { error: loadedState.error };

  return {
    cfgName: loaded.cfgName,
    cfg: loaded.cfg,
    issues: loaded.issues,
    options: buildOptions(loaded.cfg, secretRes.secret, opts),
    state: loadedState.state,
    stateFile,
  };
}

function saveStateOrReport(stateFile: string, state: PublishState): boolean {
  const r = saveState(stateFile, state);
  if ('error' in r) {
    error(r.error);
    return false;
  }
  return true;
}

// ============ Cover ============

/**
 * Resolve the cover: state → local image in config(uploaded as a permanent asset)→
 * **borrowed from the draft box**. Borrowing matters because a draft requires a cover,
 * and the `thumb_media_id` you set on issue 1 in the backend can be reused across the
 * rest — so no local image is needed.
 */
async function resolveCover(
  ready: Ready,
  opts: CliOptions,
  hooks: wechat.WechatHooks,
): Promise<{ mediaId: string } | { error: string }> {
  const { cfg, state, options } = ready;
  if (state.coverImage) {
    dim(`  Cover: reusing the recorded thumb_media_id ${state.coverImage}`);
    return { mediaId: state.coverImage };
  }

  if (cfg.coverImage) {
    const abs = path.resolve(REPO_ROOT, cfg.coverImage);
    dim(`  Cover: uploading local image ${cfg.coverImage}`);
    const up = await wechat.uploadThumb(abs, options, hooks);
    if (!up.ok) {
      error(`Failed to upload cover: ${up.errMsg}`);
      error(wechat.errorHint(up.kind, up.errMsg));
      return { error: 'Failed to upload cover' };
    }
    state.coverImage = up.data;
    if (!saveStateOrReport(ready.stateFile, state)) return { error: 'Failed to write state' };
    success(`  ✔ Cover uploaded thumb_media_id=${up.data}`);
    return { mediaId: up.data };
  }

  if (opts.check) {
    // --check is a read-only command, so the "no cover yet" state is allowed
    return { mediaId: '' };
  }

  dim('  Cover: no coverImage in the config, looking in the draft box for an existing cover……');
  const drafts = await wechat.listDrafts(options, hooks);
  if (!drafts.ok) {
    error(`Failed to read the draft box: ${drafts.errMsg}`);
    error(wechat.errorHint(drafts.kind, drafts.errMsg));
    return { error: 'Failed to read the draft box' };
  }
  const found = drafts.data.find((d) => d.thumbMediaId);
  if (found) {
    state.coverImage = found.thumbMediaId;
    if (!saveStateOrReport(ready.stateFile, state)) return { error: 'Failed to write state' };
    success(`  ✔ Borrowed the cover thumb_media_id=${found.thumbMediaId} from draft "${found.title}"`);
    return { mediaId: found.thumbMediaId };
  }

  info('');
  info('  ! No cover yet: create 1 draft issue first, set a cover for it in the backend, then rerun this command —');
  info('    After that the other issues will automatically reuse that cover(you can also just fill in coverImage in this config to point at a local image).');
  return { mediaId: '' };
}

// ============ --check ============

async function runCheck(ready: Ready, opts: CliOptions): Promise<number> {
  const hooks = buildHooks(opts);
  const { cfg, issues, options, state } = ready;

  header('Reconcile the draft box');
  info(`  appId ${cfg.appId} · ${issues.length} issues recorded locally`);
  info('');

  const sp = startSpinner('Fetching the draft box');
  const drafts = await wechat.listDrafts(options, hooks);
  if (!drafts.ok) {
    sp.stopError('Failed to fetch the draft box');
    error(drafts.errMsg);
    error(wechat.errorHint(drafts.kind, drafts.errMsg));
    return 1;
  }
  sp.stopSuccess(`The draft box currently has ${drafts.data.length} drafts`);
  info('');

  const byTitle = new Map<string, wechat.DraftBrief[]>();
  for (const d of drafts.data) {
    const list = byTitle.get(d.title) ?? [];
    list.push(d);
    byTitle.set(d.title, list);
  }

  let onDraft = 0;
  let gone = 0;
  let recovered = 0;
  let dup = 0;
  let notYet = 0;
  let changed = 0;

  for (const issue of issues) {
    const st = ensureArticle(state, issue.id, issue.contentHash);
    const matches = byTitle.get(issue.title) ?? [];

    if (matches.length > 1) {
      dup++;
      dim(`  ! Issue ${issue.order} "${issue.title}" has ${matches.length} entries in the draft box(duplicate drafts), media_id: ${matches.map((m) => m.mediaId).join(', ')}`);
    }

    if (st.draftId) {
      if (matches.some((m) => m.mediaId === st.draftId)) {
        onDraft++;
        if (st.title && st.title !== issue.title) {
          changed++;
          dim(`  ! Issue ${issue.order}'s draft title is "${st.title}", which does not match the current "${issue.title}"`);
        }
      } else if (matches.length > 0) {
        recovered++;
        st.draftId = matches[0].mediaId;
        st.status = 'drafted';
        dim(`  ! Issue ${issue.order}'s locally recorded media_id is no longer in the draft box, switching to the same-titled ${matches[0].mediaId}`);
      } else if (st.status === 'drafted') {
        gone++;
        st.status = 'published';
        st.updatedAt = new Date().toISOString();
        info(`  · Issue ${issue.order} "${issue.title}"'s draft is no longer in the draft box → treated as **published**(it may also have been deleted by hand)`);
      }
    } else if (matches.length > 0) {
      recovered++;
      st.draftId = matches[0].mediaId;
      st.status = 'drafted';
      st.title = issue.title;
      info(`  · Issue ${issue.order} "${issue.title}" is in the draft box(not recorded locally), backfilled ${matches[0].mediaId}`);
    } else {
      notYet++;
    }
  }

  const knownTitles = new Set(issues.map((i) => i.title));
  const orphans = drafts.data.filter((d) => !knownTitles.has(d.title));

  if (!state.coverImage) {
    const cover = drafts.data.find((d) => d.thumbMediaId);
    if (cover) {
      state.coverImage = cover.thumbMediaId;
      success(`  ✔ Recorded the cover thumb_media_id=${cover.thumbMediaId}(from draft "${cover.title}")`);
    }
  }

  if (!saveStateOrReport(ready.stateFile, state)) return 1;

  info('');
  header('Reconciliation result');
  info(`  Created in the draft box: ${onDraft + recovered} issues`);
  info(`  Not created yet:          ${notYet} issues`);
  if (gone > 0) info(`  Draft no longer present(treated as published): ${gone} issues`);
  if (dup > 0) error(`  Duplicate drafts:         ${dup} issues(delete the extras from the draft box, or just ignore them: the content is the same)`);
  if (changed > 0) error(`  Title mismatch:           ${changed} issues`);
  if (orphans.length > 0) {
    dim(`  Drafts in the draft box unrelated to this series: ${orphans.length}`);
    for (const o of orphans.slice(0, 10)) dim(`    ${o.mediaId}  ${o.title}`);
  }
  if (state.coverImage) info(`  Cover thumb_media_id: ${state.coverImage}`);
  else error('  No cover yet — when creating drafts it is recommended to first manually schedule 1 issue in the backend and set a cover');
  return 0;
}

// ============ --drafts ============

async function runDrafts(ready: Ready, opts: CliOptions): Promise<number> {
  const hooks = buildHooks(opts);
  const { cfgName, cfg, issues, options, state, stateFile } = ready;

  const selected = selectIssues(issues, opts);
  if ('error' in selected) {
    error(selected.error);
    return 1;
  }
  const targets = selected;

  // Length/title/digest problems have already all surfaced in loadIssues(it errors out and stops before creating any draft)
  const tooLong = targets.filter((i) => i.content.length > i.limit);
  if (tooLong.length > 0) {
    error(`${tooLong.length} issues exceed the limit, fix them first: ${tooLong.map((i) => i.id).join(', ')}`);
    return 1;
  }

  header('Create drafts');
  info(`  ${targets.length} issues · interval ${opts.delayMs ?? cfg.delayMs}ms`);
  info('');

  const cover = await resolveCover(ready, opts, hooks);
  if ('error' in cover) return 1;
  const thumbMediaId = cover.mediaId;
  info('');

  let created = 0;
  let skipped = 0;
  let updated = 0;

  for (const issue of targets) {
    const st = ensureArticle(state, issue.id, issue.contentHash);

    if (st.status === 'published') {
      dim(`  ✓ Issue ${issue.order} has already been published, skipping`);
      skipped++;
      continue;
    }
    // ⚠️ `!opts.force` MUST stay on this line: --force must rebuild even when the hash is
    // unchanged(a renderer-only change leaves the hash equal), otherwise the skip above swallows it.
    if (st.status === 'drafted' && st.draftId && st.contentHash === issue.contentHash && !opts.force) {
      dim(`  ✓ Issue ${issue.order}'s draft already exists(${st.draftId}), skipping`);
      skipped++;
      continue;
    }
    // Content changed(body or digest)→ **update in place**: delete+create would swap the
    // media_id, leaving the issue absent from the draft box mid-operation and truly lost on failure.
    if (st.status === 'drafted' && st.draftId && st.contentHash !== issue.contentHash && !opts.force) {
      const sp = startSpinner(`Updating issue ${issue.order}'s draft(${issue.title})`);
      const upd = await wechat.updateDraft(
        st.draftId,
        {
          title: issue.title,
          content: issue.content,
          digest: issue.digest || undefined,
          author: cfg.author || undefined,
          thumbMediaId: thumbMediaId || undefined,
          contentSourceUrl: cfg.contentSourceUrl || undefined,
        },
        options,
        hooks,
      );
      if (!upd.ok) {
        sp.stopError('Failed to update draft');
        error(upd.errMsg);
        error(wechat.errorHint(upd.kind, upd.errMsg));
        info('');
        info(`  Draft ${st.draftId} still has the old content, nothing was lost. Rerun to continue;`);
        info('  If this API is unavailable on a personal account, adding --force will delete it and rebuild the whole thing.');
        return 1;
      }
      st.contentHash = issue.contentHash;
      st.title = issue.title;
      st.updatedAt = new Date().toISOString();
      if (!saveStateOrReport(stateFile, state)) {
        sp.stopError('Failed to write state');
        return 1;
      }
      sp.stopSuccess(`Issue ${issue.order} updated media_id=${st.draftId}`);
      updated++;
      if (opts.delayMs ?? cfg.delayMs) await delay(opts.delayMs ?? cfg.delayMs);
      continue;
    }
    if (st.draftId && opts.force) {
      dim(`  · Deleting old draft ${st.draftId}`);
      const del = await wechat.deleteDraft(st.draftId, options, hooks);
      if (!del.ok) {
        error(`Failed to delete the old draft: ${del.errMsg}`);
        error(wechat.errorHint(del.kind, del.errMsg));
        return 1;
      }
      st.draftId = null;
      st.status = 'pending';
      if (!saveStateOrReport(stateFile, state)) return 1;
    }

    const sp = startSpinner(`Creating issue ${issue.order}'s draft(${issue.title})`);
    const res = await wechat.addDraft(
      {
        title: issue.title,
        content: issue.content,
        digest: issue.digest || undefined,
        author: cfg.author || undefined,
        thumbMediaId: thumbMediaId || undefined,
        contentSourceUrl: cfg.contentSourceUrl || undefined,
      },
      options,
      hooks,
    );
    if (!res.ok) {
      sp.stopError('Failed to create draft');
      error(res.errMsg);
      error(wechat.errorHint(res.kind, res.errMsg));
      info('');
      info(`  All drafts created so far are still there; rerunning will skip them and continue with the rest.`);
      return 1;
    }
    // Save to disk before continuing: if interrupted at any moment, at most one issue of progress is lost
    st.status = 'drafted';
    st.draftId = res.data;
    st.title = issue.title;
    st.contentHash = issue.contentHash;
    st.updatedAt = new Date().toISOString();
    if (!saveStateOrReport(stateFile, state)) {
      sp.stopError('Failed to write state');
      return 1;
    }
    sp.stopSuccess(`Issue ${issue.order} created media_id=${res.data}`);
    created++;

    // Confirmation point: stop after issue 1 to check the layout, which cannot be changed
    // once published(the final push is manual in the backend).
    if (created === 1 && !opts.yes && targets.length > 1) {
      info('');
      header('Issue 1 draft created — go take a look in the backend first');
      info(`  1. Official Account backend → Drafts → open this one: check the layout, code block background, tables, the checkboxes in to-do lists`);
      info(`  2. While you're at it, check the cover crop(the cover is already auto-uploaded and shared by all issues)`);
      info(`  3. Once you're satisfied, rerun(it will reuse the drafts already created and continue with the remaining ${targets.length - 1} issues):`);
      info('');
      info(`     node src/publish/publish.ts wechat --drafts --yes`);
      info('');
      info(`  To change the content: edit the Markdown and rerun with --force, which deletes the old drafts and rebuilds them.`);
      return 2;
    }

    if (opts.delayMs ?? cfg.delayMs) await delay(opts.delayMs ?? cfg.delayMs);
  }

  header('Done');
  info(`  Created ${created} issues this run · updated in place ${updated} · skipped ${skipped}`);
  const total = Object.values(state.articles).filter((s) => s.status === 'drafted' || s.status === 'published').length;
  info(`  ${total} issues created/published in total`);
  info('');
  info(`  Next: node src/publish/publish.ts wechat ${cfgName} --plan   # set up scheduled publishing in the backend per the schedule table`);
  return 0;
}

// ============ main ============

export async function run(argv: string[]): Promise<number> {
  const parsed = parseArgs(argv);
  if ('error' in parsed) {
    error(parsed.error);
    error('Usage: node src/publish/publish.ts wechat [configName] [--list|--build|--plan|--check|--drafts|--ip]');
    return 1;
  }
  const opts = parsed;

  const loaded = await loadIssues(opts);
  if ('error' in loaded) {
    error(loaded.error);
    return 1;
  }

  // ---- Offline subcommands ----
  if (opts.list) return runList(loaded.cfg, loaded.issues);
  if (opts.plan) return runPlan(loaded.cfg, loaded.issues);
  if (opts.build) return runBuild(loaded.cfg, loaded.cfgName, loaded.issues);

  if (opts.ip) {
    const res = await wechat.fetchEgressIp({ appId: '', appSecret: '', timeoutMs: loaded.cfg.timeoutMs });
    if (!res.ok) {
      error(`Failed to look up the egress IP: ${res.errMsg}`);
      return 1;
    }
    header("This machine's public egress IP");
    info(`  ${res.data}`);
    info(`  Add it to Official Account backend → Settings and Development → Basic Configuration → IP Whitelist`);
    info(`  (home broadband IPs change, and a changed IP must be re-added — this is the only routine maintenance point on the API path)`);
    return 0;
  }

  // ---- The following require keys ----
  const ready = prepareOnline(loaded, opts);
  if ('error' in ready) {
    error(ready.error);
    return 1;
  }

  const lock = acquireLock(ready.stateFile);
  if ('error' in lock) {
    error(lock.error);
    return 1;
  }
  try {
    if (opts.check) return await runCheck(ready, opts);
    return await runDrafts(ready, opts);
  } finally {
    releaseLock(ready.stateFile);
  }
}

// The entry point is in src/publish/publish.ts — this file only exports run(argv) and no longer executes itself.
