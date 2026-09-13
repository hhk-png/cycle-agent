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
import { dim, error, header, info, startSpinner, stopSpinnerActive, success } from '../shared/ui.ts';
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
 * 把教程章节发到微信公众号。
 *
 * ⚠️ **个人订阅号没有群发/发布接口**(调用返回 48001),所以脚本能做的
 * 最后一件事是「建草稿」。真正发表那一步必须在公众号后台点 —— 好消息是
 * 后台自带「定时发表」,排好之后它自己会发。本脚本负责的是:
 *
 *   1. 把 Markdown 转成公众号能用的 HTML(带 2 万字符上限的校验与自动拆篇)
 *   2. 生成一个**本地预览页**,可以逐期「复制正文」粘进编辑器(零密钥)
 *   3. 用草稿箱接口**把各期直接建成草稿**并记账(需要 AppID/AppSecret)
 *   4. 打印一张**排期表**,你按它在后台设定时发表
 *
 * 用法:
 *   node src/publish/wechat.ts --list            # 期次清单 + 长度/标题/摘要预检(不联网)
 *   node src/publish/wechat.ts --build           # 生成预览页与 HTML 片段(不联网)
 *   node src/publish/wechat.ts --plan            # 排期表(不联网)
 *   node src/publish/wechat.ts --ip              # 查本机公网出口 IP(配白名单用)
 *   node src/publish/wechat.ts --check           # 对账草稿箱(只读,需要密钥)
 *   node src/publish/wechat.ts --drafts          # 建草稿:建完第 1 期会停下让你看排版
 *   node src/publish/wechat.ts --drafts --yes    # 不再停顿,一次建完
 *   node src/publish/wechat.ts --drafts --only 08 --force
 *
 * 退出码:0 完成、1 出错、2 在确认点暂停(状态已落盘,可续跑)、130 中断。
 */

const REPO_ROOT = process.cwd();

/** 产物目录(已被 .gitignore 覆盖) */
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

/** 手写参数解析(与 publish.ts 一致,不引入解析库) */
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
        if (v === null) return { error: '--only 需要一个期号,如 --only 08 或 --only 18-2' };
        o.only = v;
        break;
      }
      case '--delay': {
        const v = needValue();
        const n = Number(v);
        if (v === null || !Number.isFinite(n) || n < 0) return { error: '--delay 需要毫秒数,如 --delay 5000' };
        o.delayMs = n;
        break;
      }
      default: {
        if (arg.startsWith('--')) return { error: `未知参数: ${arg}` };
        if (o.configName) return { error: `只能指定一个配置名,多给了: ${arg}` };
        o.configName = arg;
      }
    }
  }
  return o;
}

/** `--only 8` / `--only 08` / `--only 18-2` 都接受 */
function normalizeIssueId(v: string): string {
  const m = /^(\d+)(?:-(\d+))?$/.exec(v.trim());
  if (!m) return v.trim();
  return m[2] ? `${m[1].padStart(2, '0')}-${m[2]}` : m[1].padStart(2, '0');
}

function buildHooks(opts: CliOptions): wechat.WechatHooks {
  if (!opts.debug) return {};
  return { onDebug: (msg: string) => dim(`  · ${msg}`) };
}

// ============ 日期 ============

/** 'YYYY-MM-DD' 加天数(按 UTC 算,避免时区把日期挪一天) */
function addDays(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number);
  const t = new Date(Date.UTC(y, m - 1, d) + days * 86_400_000);
  return `${t.getUTCFullYear()}-${String(t.getUTCMonth() + 1).padStart(2, '0')}-${String(t.getUTCDate()).padStart(2, '0')}`;
}

function weekday(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number);
  return `周${'日一二三四五六'[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]}`;
}

/** 第 n 期(1 起)的发布日期 */
function issueDate(startDate: string, order: number): string {
  return addDays(startDate, order - 1);
}

// ============ 期次装载 ============

interface Loaded {
  cfgName: string;
  cfg: WechatConfig;
  issues: Issue[];
}

/** 载入配置 + 扫描文章 + 编排期次(长度/标题/摘要的问题都在这一步暴露) */
async function loadIssues(opts: CliOptions): Promise<Loaded | { error: string }> {
  let cfgName: string;
  let cfg: WechatConfig;
  try {
    if (opts.configName) {
      cfgName = opts.configName;
      if (!listWechatConfigNames().includes(cfgName)) {
        return {
          error: `src/publish/src/publish/configs/ 下没有微信配置: ${cfgName}(可用: ${listWechatConfigNames().join(', ') || '无'})`,
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

  const loaded = loadArticles(cfg.sourceDir, cfg.filePrefix, cfg.fromNumber, cfg.titleSource ?? 'h1');
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
    return { error: `没有匹配的期次: ${opts.only}(可用: ${issues.map((i) => i.id).join(', ')})` };
  }
  return found;
}

// ============ --list ============

function runList(cfg: WechatConfig, issues: Issue[]): number {
  header('微信公众号期次清单');
  const limit = issues[0]?.limit ?? WECHAT_CONTENT_LIMIT;
  info(
    `  共 ${issues.length} 期 · 正文上限 ${limit} 字符` +
      `${limit < WECHAT_CONTENT_LIMIT ? `(接口上限 ${WECHAT_CONTENT_LIMIT},配置里留了余量)` : ''}` +
      ` · 起始日 ${cfg.startDate}`,
  );
  info('');

  let warned = 0;
  for (const issue of issues) {
    const len = issue.content.length;
    const ratio = (len / issue.limit) * 100;
    const mark =
      len > issue.limit ? pc.red('超限') : ratio >= 98 ? pc.yellow('卡线') : pc.green('ok  ');
    const digest = issue.digest ? `${issue.digest.length}字` : pc.yellow('无');
    info(
      `  ${String(issue.order).padStart(2)}  ${issue.id.padEnd(6)} ${mark} ${String(len).padStart(5)}(${String(Math.round(ratio)).padStart(3)}%)` +
        ` 标题${String(issue.title.length).padStart(2)}字 摘要${digest}  ${issue.title}`,
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
  info(`  拆开的篇: ${[...byArticle].map(([no, p]) => `${no}→${p}期`).join(' ') || '无'}`);
  info(`  最长一期 ${Math.max(...issues.map((i) => i.content.length))} 字符`);
  if (tight.length > 0) {
    dim(
      `  贴在上限上(≥98%): ${tight.map((i) => `第${i.order}期 ${i.content.length}`).join(' · ')} ——` +
        ` 2 万是接口文档的硬上限,这几期是按「刚够」算的。若接口拒收,` +
        `把配置里的 contentLimit 设成 19000(留 5% 余量),它们会各自再拆一期。`,
    );
  }
  if (warned > 0) dim(`  以上 ${warned} 条提醒不阻塞发布(--build/--drafts 都会照做)`);
  return 0;
}

// ============ --plan ============

function runPlan(cfg: WechatConfig, issues: Issue[]): number {
  header('排期表');
  info(`  起始日 ${cfg.startDate}(${weekday(cfg.startDate)}) · 共 ${issues.length} 期 · 每天 1 期`);
  info(
    `  公众号后台的「定时发表」**最多只能提前 7 天**,所以分 ${Math.ceil(issues.length / 7)} 轮排完;` +
      `每轮的操作日就是那批里第 1 期的日期。`,
  );
  info('');

  const PER_ROUND = 7;
  for (let r = 0; r * PER_ROUND < issues.length; r++) {
    const batch = issues.slice(r * PER_ROUND, (r + 1) * PER_ROUND);
    const first = issueDate(cfg.startDate, batch[0].order);
    const last = issueDate(cfg.startDate, batch[batch.length - 1].order);
    info(
      pc.bold(`  第 ${r + 1} 轮`) +
        `  ${first}(${weekday(first)}) 起 ${batch.length} 期 → ${last}(${weekday(last)})  · 操作日 ${first}`,
    );
    for (const issue of batch) {
      const d = issueDate(cfg.startDate, issue.order);
      info(`      ${d}(${weekday(d)})  第 ${String(issue.order).padStart(2)} 期  ${issue.title}`);
    }
    info('');
  }

  const lastDate = issueDate(cfg.startDate, issues.length);
  info(`  最后一期落在 ${lastDate}(${weekday(lastDate)})。`);
  info(
    `  若今天已经发过一篇、排不下 ${cfg.startDate},把配置里的 startDate 改成 ${addDays(cfg.startDate, 1)} 即可。`,
  );
  return 0;
}

// ============ --build ============

/** 预览页:一页列全部期次,逐期「复制正文」直接粘进公众号编辑器 */
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
      <div class="eyebrow">第 ${issue.order} 期 · ${d}(${weekday(d)}) ${splitNote}</div>
      <h2 class="title">${escapeHtml(issue.title)}</h2>
      <div class="meta">正文 ${issue.content.length} 字符 / 上限 ${issue.limit} · 标题 ${issue.title.length} 字 / 上限 32 · 摘要 ${
        issue.digest ? `${issue.digest.length} 字` : '无(由公众号自动生成)'
      }</div>
    </div>
    <div class="actions">
      <button onclick="copyTitle(this, ${JSON.stringify(issue.title).replace(/"/g, '&quot;')})">复制标题</button>
      <button class="primary" onclick="copyBody(this, 'c-${issue.id}')">复制正文</button>
    </div>
  </div>
  ${warns}
  <details>
    <summary>看摘要</summary>
    <div class="digest">${escapeHtml(issue.digest || '(空)')}</div>
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
<title>${escapeHtml(cfgName)} · 微信公众号预览(${issues.length} 期)</title>
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
    <h1>${escapeHtml(cfgName)} · 微信公众号预览</h1>
    <p>共 <b>${issues.length}</b> 期,每期一天,起始日 ${cfg.startDate}(${weekday(cfg.startDate)})。</p>
    <p><b>用法</b>:点「复制正文」→ 到公众号后台「草稿箱 → 新的创作 → 写新图文」,在正文区 <b>Ctrl+V</b> 粘贴(排版会一起带过去),再把标题粘上、设好封面。</p>
    <p style="color:#c45656">未认证个人订阅号:正文里<b>不能带外部超链接</b>(外链会被剔除),现已把链接渲染成「文字 (地址)」纯文本;发表只能手动点或设「定时发表」。</p>
  </div>
  ${cards}
</div>
<script>
function flash(btn, ok, okText) {
  var old = btn.textContent;
  btn.textContent = ok ? okText : '复制失败,请手动选中';
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
  flash(btn, ok, '✔ 已复制');
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
  flash(btn, ok, '✔ 已复制');
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

  header('已生成预览页');
  success(`  预览页: ${path.relative(REPO_ROOT, preview).replace(/\\/g, '/')}`);
  info(`  另存了 ${issues.length} 份 HTML 片段(就是接口用的 content)与对应 Markdown`);
  info('');
  info(`  用浏览器打开预览页 → 逐期「复制正文」→ 粘进公众号编辑器`);
  info(`  (目录: ${path.relative(REPO_ROOT, outDir).replace(/\\/g, '/')}/)`);
  return 0;
}

// ============ 状态 ============

/**
 * 微信侧的状态文件与掘金侧**共用 publish-state.ts 的读写与锁**
 * (原子写、损坏硬停、pid 锁那三条不变量一样都不想丢),只是换了 namespace。
 *
 * 字段的对应关系:
 * - `status: 'pending' | 'drafted' | 'published'` = 未建草稿 / 草稿已建 / 已发表
 *   (个人订阅号没有发布接口,所以 `published` 是 `--check` 发现「草稿已不在草稿箱」时推断出来的)
 * - `draftId` = 草稿的 media_id
 * - `coverImage` = **这个系列共用的封面 thumb_media_id**
 *   (字段名在掘金那边是封面 URL;两边都是「全系列复用的封面引用」,含义一致)
 * - `articleId`/`url`/`publishAttemptedAt` 在个人订阅号上用不到,保持 null
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

/** 需要密钥的子命令走这里:校验配置 → 读密钥 → 读状态 → 取锁 */
function prepareOnline(loaded: Loaded, opts: CliOptions): Ready | { error: string } {
  const problems = configProblems(loaded.cfg);
  if (problems.length > 0) {
    return { error: `微信配置有问题:\n  - ${problems.join('\n  - ')}` };
  }
  const secretRes = readAppSecret(REPO_ROOT, loaded.cfg);
  if ('error' in secretRes) return { error: secretRes.error };
  dim(`  凭据: appId ${loaded.cfg.appId} · AppSecret ${secretRes.source}`);

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

// ============ 封面 ============

/**
 * 取封面。
 *
 * 顺序:状态里记着的 → 配置里的本地图片(上传成永久素材)→ **从草稿箱里借**。
 *
 * 「从草稿箱借」是这套流程的关键一步:公众号的草稿封面是必填项,而
 * 你在后台手动排第 1 期时一定会给它设一个封面 —— 那个 `thumb_media_id`
 * 就能给其余各期复用,于是**不需要准备任何本地图片**。
 */
async function resolveCover(
  ready: Ready,
  opts: CliOptions,
  hooks: wechat.WechatHooks,
): Promise<{ mediaId: string } | { error: string }> {
  const { cfg, state, options } = ready;
  if (state.coverImage) {
    dim(`  封面: 复用已记录的 thumb_media_id ${state.coverImage}`);
    return { mediaId: state.coverImage };
  }

  if (cfg.coverImage) {
    const abs = path.resolve(REPO_ROOT, cfg.coverImage);
    dim(`  封面: 上传本地图片 ${cfg.coverImage}`);
    const up = await wechat.uploadThumb(abs, options, hooks);
    if (!up.ok) {
      error(`上传封面失败: ${up.errMsg}`);
      error(wechat.errorHint(up.kind, up.errMsg));
      return { error: '上传封面失败' };
    }
    state.coverImage = up.data;
    if (!saveStateOrReport(ready.stateFile, state)) return { error: '状态写入失败' };
    success(`  ✔ 封面已上传 thumb_media_id=${up.data}`);
    return { mediaId: up.data };
  }

  if (opts.check) {
    // --check 是只读命令,允许「还没封面」这种状态
    return { mediaId: '' };
  }

  dim('  封面: 配置里没有 coverImage,去草稿箱里找一个现成的封面……');
  const drafts = await wechat.listDrafts(options, hooks);
  if (!drafts.ok) {
    error(`读草稿箱失败: ${drafts.errMsg}`);
    error(wechat.errorHint(drafts.kind, drafts.errMsg));
    return { error: '读草稿箱失败' };
  }
  const found = drafts.data.find((d) => d.thumbMediaId);
  if (found) {
    state.coverImage = found.thumbMediaId;
    if (!saveStateOrReport(ready.stateFile, state)) return { error: '状态写入失败' };
    success(`  ✔ 从草稿「${found.title}」借到封面 thumb_media_id=${found.thumbMediaId}`);
    return { mediaId: found.thumbMediaId };
  }

  info('');
  info('  ! 还没有封面:先建 1 期草稿,去后台给它设个封面,再重跑本命令 ——');
  info('    之后各期会自动复用那个封面(也可以直接在本配置里填 coverImage 指向一张本地图片)。');
  return { mediaId: '' };
}

// ============ --check ============

async function runCheck(ready: Ready, opts: CliOptions): Promise<number> {
  const hooks = buildHooks(opts);
  const { cfg, issues, options, state } = ready;

  header('对账草稿箱');
  info(`  appId ${cfg.appId} · 本地记着 ${issues.length} 期`);
  info('');

  const sp = startSpinner('拉取草稿箱');
  const drafts = await wechat.listDrafts(options, hooks);
  if (!drafts.ok) {
    sp.stopError('拉取草稿箱失败');
    error(drafts.errMsg);
    error(wechat.errorHint(drafts.kind, drafts.errMsg));
    return 1;
  }
  sp.stopSuccess(`草稿箱里现有 ${drafts.data.length} 条草稿`);
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
      dim(`  ! 第 ${issue.order} 期「${issue.title}」在草稿箱里有 ${matches.length} 条(重复草稿),media_id: ${matches.map((m) => m.mediaId).join(', ')}`);
    }

    if (st.draftId) {
      if (matches.some((m) => m.mediaId === st.draftId)) {
        onDraft++;
        if (st.title && st.title !== issue.title) {
          changed++;
          dim(`  ! 第 ${issue.order} 期草稿标题是「${st.title}」,与当前「${issue.title}」不一致`);
        }
      } else if (matches.length > 0) {
        recovered++;
        st.draftId = matches[0].mediaId;
        st.status = 'drafted';
        dim(`  ! 第 ${issue.order} 期本地记的 media_id 已不在草稿箱,改用同名的 ${matches[0].mediaId}`);
      } else if (st.status === 'drafted') {
        gone++;
        st.status = 'published';
        st.updatedAt = new Date().toISOString();
        info(`  · 第 ${issue.order} 期「${issue.title}」的草稿已不在草稿箱 → 视为**已发表**(也可能被手工删了)`);
      }
    } else if (matches.length > 0) {
      recovered++;
      st.draftId = matches[0].mediaId;
      st.status = 'drafted';
      st.title = issue.title;
      info(`  · 第 ${issue.order} 期「${issue.title}」草稿箱里有(本地没记),已补记 ${matches[0].mediaId}`);
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
      success(`  ✔ 记下封面 thumb_media_id=${cover.thumbMediaId}(来自草稿「${cover.title}」)`);
    }
  }

  if (!saveStateOrReport(ready.stateFile, state)) return 1;

  info('');
  header('对账结果');
  info(`  草稿箱里已建: ${onDraft + recovered} 期`);
  info(`  还没建:       ${notYet} 期`);
  if (gone > 0) info(`  草稿已不在(视为已发表): ${gone} 期`);
  if (dup > 0) error(`  重复草稿:     ${dup} 期(去草稿箱删掉多余的,或直接忽略:内容一样)`);
  if (changed > 0) error(`  标题不一致:   ${changed} 期`);
  if (orphans.length > 0) {
    dim(`  草稿箱里与本系列无关的草稿 ${orphans.length} 条:`);
    for (const o of orphans.slice(0, 10)) dim(`    ${o.mediaId}  ${o.title}`);
  }
  if (state.coverImage) info(`  封面 thumb_media_id: ${state.coverImage}`);
  else error('  还没有封面 —— 建草稿时建议先在后台手动排 1 期并设好封面');
  return 0;
}

// ============ --drafts ============

async function runDrafts(ready: Ready, opts: CliOptions): Promise<number> {
  const hooks = buildHooks(opts);
  const { cfg, issues, options, state, stateFile } = ready;

  const selected = selectIssues(issues, opts);
  if ('error' in selected) {
    error(selected.error);
    return 1;
  }
  const targets = selected;

  // 长度/标题/摘要的问题在 loadIssues 里已经全部暴露过(会在建任何草稿之前就报错停住)
  const tooLong = targets.filter((i) => i.content.length > i.limit);
  if (tooLong.length > 0) {
    error(`有 ${tooLong.length} 期超过上限,先解决: ${tooLong.map((i) => i.id).join(', ')}`);
    return 1;
  }

  header('建草稿');
  info(`  共 ${targets.length} 期 · 间隔 ${opts.delayMs ?? cfg.delayMs}ms`);
  info('');

  const cover = await resolveCover(ready, opts, hooks);
  if ('error' in cover) return 1;
  const thumbMediaId = cover.mediaId;
  info('');

  let created = 0;
  let skipped = 0;
  let stale = 0;

  for (const issue of targets) {
    const st = ensureArticle(state, issue.id, issue.contentHash);

    if (st.status === 'published') {
      dim(`  ✓ 第 ${issue.order} 期已发表过,跳过`);
      skipped++;
      continue;
    }
    // ⚠️ `!opts.force` 这个条件必须在**这一行**:内容没变时也要能被 --force 重建。
    // (踩过:改渲染器不改 markdown,哈希就相等,于是 --force 被上面这条跳过吃掉,
    //  永远重建不了 —— 只好像下面这样把它显式排除在「已是最新」之外。)
    if (st.status === 'drafted' && st.draftId && st.contentHash === issue.contentHash && !opts.force) {
      dim(`  ✓ 第 ${issue.order} 期草稿已建(${st.draftId}),跳过`);
      skipped++;
      continue;
    }
    if (st.status === 'drafted' && st.draftId && st.contentHash !== issue.contentHash && !opts.force) {
      stale++;
      dim(`  ! 第 ${issue.order} 期正文改过了,草稿是旧的(${st.draftId})—— 加 --force 会删掉它重建`);
      continue;
    }
    if (st.draftId && opts.force) {
      dim(`  · 删掉旧草稿 ${st.draftId}`);
      const del = await wechat.deleteDraft(st.draftId, options, hooks);
      if (!del.ok) {
        error(`删除旧草稿失败: ${del.errMsg}`);
        error(wechat.errorHint(del.kind, del.errMsg));
        return 1;
      }
      st.draftId = null;
      st.status = 'pending';
      if (!saveStateOrReport(stateFile, state)) return 1;
    }

    const sp = startSpinner(`建第 ${issue.order} 期草稿(${issue.title})`);
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
      sp.stopError('建草稿失败');
      error(res.errMsg);
      error(wechat.errorHint(res.kind, res.errMsg));
      info('');
      info(`  已建的草稿都在,重跑会跳过它们继续建剩下的。`);
      return 1;
    }
    // 先落盘再继续:任何时刻中断,最多损失一期的进度
    st.status = 'drafted';
    st.draftId = res.data;
    st.title = issue.title;
    st.contentHash = issue.contentHash;
    st.updatedAt = new Date().toISOString();
    if (!saveStateOrReport(stateFile, state)) {
      sp.stopError('状态写入失败');
      return 1;
    }
    sp.stopSuccess(`第 ${issue.order} 期已建 media_id=${res.data}`);
    created++;

    // 确认点:建完第 1 期停下来,让你去后台看排版。
    // 封面已经由 coverImage 自动上传并复用到全部各期了,所以这里只剩「看排版」一件事 ——
    // 而排版是**发出去就改不动**的(个人号没有群发接口,最终那一按在后台),值得停一次。
    if (created === 1 && !opts.yes && targets.length > 1) {
      info('');
      header('已建第 1 期草稿 —— 先去后台看一眼');
      info(`  1. 公众号后台 → 草稿箱 → 打开这篇:检查排版、代码块底色、表格、待办清单的方框`);
      info(`  2. 顺手看一眼封面裁切(封面已自动上传,各期共用这一张)`);
      info(`  3. 都满意后重跑(会复用已建的草稿,继续建剩下的 ${targets.length - 1} 期):`);
      info('');
      info(`     node src/publish/wechat.ts --drafts --yes`);
      info('');
      info(`  要改内容的话:改完 Markdown 重跑带 --force,会删掉旧草稿重建。`);
      return 2;
    }

    if (opts.delayMs ?? cfg.delayMs) await delay(opts.delayMs ?? cfg.delayMs);
  }

  header('完成');
  info(`  本次新建 ${created} 期 · 跳过 ${skipped} 期${stale > 0 ? ` · 内容过期 ${stale} 期(需 --force)` : ''}`);
  const total = Object.values(state.articles).filter((s) => s.status === 'drafted' || s.status === 'published').length;
  info(`  累计已建/已发 ${total} 期`);
  info('');
  info(`  下一步: node src/publish/wechat.ts --plan   # 按排期表在后台设定时发表`);
  return 0;
}

// ============ main ============

async function main(): Promise<number> {
  const parsed = parseArgs(process.argv.slice(2));
  if ('error' in parsed) {
    error(parsed.error);
    error('用法: node src/publish/wechat.ts [配置名] [--list|--build|--plan|--check|--drafts|--ip]');
    return 1;
  }
  const opts = parsed;

  const loaded = await loadIssues(opts);
  if ('error' in loaded) {
    error(loaded.error);
    return 1;
  }

  // ---- 不联网的子命令 ----
  if (opts.list) return runList(loaded.cfg, loaded.issues);
  if (opts.plan) return runPlan(loaded.cfg, loaded.issues);
  if (opts.build) return runBuild(loaded.cfg, loaded.cfgName, loaded.issues);

  if (opts.ip) {
    const res = await wechat.fetchEgressIp({ appId: '', appSecret: '', timeoutMs: loaded.cfg.timeoutMs });
    if (!res.ok) {
      error(`查出口 IP 失败: ${res.errMsg}`);
      return 1;
    }
    header('本机公网出口 IP');
    info(`  ${res.data}`);
    info(`  把它加进 公众号后台 → 设置与开发 → 基本配置 → IP 白名单`);
    info(`  (家用宽带 IP 会变,变了要重新加 —— 这是 API 路径唯一的日常维护点)`);
    return 0;
  }

  // ---- 以下需要密钥 ----
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

process.on('SIGINT', () => {
  stopSpinnerActive();
  console.log(pc.red('✖ 已中断'));
  // 不在信号处理器里写状态文件(写盘竞态);靠「每建一期即落盘」保证可续跑
  process.exit(130);
});

main()
  .then((code) => {
    process.exitCode = code;
  })
  .catch((err: unknown) => {
    error(`未预期的错误: ${(err as Error).message}`);
    process.exitCode = 1;
  });
