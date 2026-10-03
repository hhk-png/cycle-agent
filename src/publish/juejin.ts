import path from 'node:path';
import { readFileSync } from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { confirm } from '@clack/prompts';
import pc from 'picocolors';
import { assertCookieIgnored } from './credential.ts';
import {
  briefHasMarkdown,
  briefProblem,
  briefTight,
  countBrief,
  extractIntro,
  findTitleLine,
  loadArticles,
  type Article,
} from './articles.ts';
import * as juejin from './api/juejin.ts';
import {
  hasPublishConfig,
  listPublishConfigNames,
  loadPublishConfig,
  pickPublishConfig,
  type PublishConfig,
} from './publish-config.ts';
import {
  acquireLock,
  ensureArticle,
  loadState,
  newState,
  releaseLock,
  saveState,
  stateFileName,
  type PublishState,
} from './publish-state.ts';
import { dim, error, header, info, isTTY, startSpinner, success } from '../shared/ui.ts';

/**
 * Publish tutorial articles to Juejin. Requires a Juejin Cookie: env JUEJIN_COOKIE, or the
 * gitignored .juejin-cookie in the repo root.
 *
 * Usage:
 *   node src/publish/publish.ts juejin --list              # pending articles + brief counts (pre-check)
 *   node src/publish/publish.ts juejin --categories        # real Juejin category ids
 *   node src/publish/publish.ts juejin --tags vllm         # tag ids by keyword
 *   node src/publish/publish.ts juejin --suggest-briefs    # print brief material only
 *   node src/publish/publish.ts juejin --dry-run           # print only, send nothing
 *   node src/publish/publish.ts juejin                     # publish (pause after the 1st draft for confirmation)
 *   node src/publish/publish.ts juejin --yes               # skip confirmation, publish all
 *   node src/publish/publish.ts juejin --drafts-only       # drafts only, never publish; re-running without it reuses the drafts
 *   node src/publish/publish.ts juejin --from 0 --to 4     # only articles in the number range
 *   node src/publish/publish.ts juejin --sync              # align drafts to config titles/briefs (run after editing briefs/titleSource;
 *                                                         #  unpublished update in place, published re-publish in place)
 *
 * Exit codes: 0 done · 1 argument/config/validation error or publish failure · 2 paused at the
 * confirmation point (state saved, resumable) · 130 SIGINT
 */

const REPO_ROOT = process.cwd();

/** Exit code after pausing at the confirmation point — pausing is a checkpoint, not a failure */
const EXIT_PAUSED = 2;

/** Max tags per article. 3 is the server's measured limit (`err_no=4031 ...at most 3 tags...`), only exposed by the real API. */
const MAX_TAGS = 3;

interface CliOptions {
  list: boolean;
  categories: boolean;
  tags: string | null;
  suggestBriefs: boolean;
  dryRun: boolean;
  draftsOnly: boolean;
  yes: boolean;
  only: string | null;
  from: string | null;
  to: string | null;
  delayMs: number | null;
  orphans: boolean;
  republish: string | null;
  /** `--sync` (old name `--rename`): align drafts with the titles and briefs in the local config */
  sync: boolean;
  force: boolean;
  skipBriefCheck: boolean;
  skipBodyCheck: boolean;
  debug: boolean;
  configName: string | null;
}

function defaultOptions(): CliOptions {
  return {
    list: false,
    categories: false,
    tags: null,
    suggestBriefs: false,
    dryRun: false,
    draftsOnly: false,
    yes: false,
    only: null,
    from: null,
    to: null,
    delayMs: null,
    orphans: false,
    republish: null,
    sync: false,
    force: false,
    skipBriefCheck: false,
    skipBodyCheck: false,
    debug: false,
    configName: null,
  };
}

/** Hand-written arg parsing (like run.ts, no library) */
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
      case '--categories': o.categories = true; break;
      case '--suggest-briefs': o.suggestBriefs = true; break;
      case '--dry-run': o.dryRun = true; break;
      case '--drafts-only': o.draftsOnly = true; break;
      case '--yes': case '-y': o.yes = true; break;
      case '--orphans': o.orphans = true; break;
      // `--rename` is the old alias (it covered titles only; now titles + briefs)
      case '--sync': case '--rename': o.sync = true; break;
      case '--force': o.force = true; break;
      case '--skip-brief-check': o.skipBriefCheck = true; break;
      case '--skip-body-check': o.skipBodyCheck = true; break;
      case '--debug': o.debug = true; break;
      case '--tags': {
        const v = needValue();
        if (v === null) return { error: '--tags needs a keyword, e.g. --tags vllm' };
        o.tags = v;
        break;
      }
      case '--only': {
        const v = needValue();
        if (v === null) return { error: '--only needs a number, e.g. --only 12' };
        o.only = v;
        break;
      }
      case '--from': {
        const v = needValue();
        if (v === null) return { error: '--from needs a number, e.g. --from 15' };
        o.from = v;
        break;
      }
      case '--to': {
        const v = needValue();
        if (v === null) return { error: '--to needs a number, e.g. --to 04' };
        o.to = v;
        break;
      }
      case '--republish': {
        const v = needValue();
        if (v === null) return { error: '--republish needs a number, e.g. --republish 09' };
        o.republish = v;
        break;
      }
      case '--delay': {
        const v = needValue();
        const n = Number(v);
        if (v === null || !Number.isFinite(n) || n < 0) return { error: '--delay needs a number of milliseconds, e.g. --delay 30000' };
        o.delayMs = n;
        break;
      }
      default: {
        if (arg.startsWith('--')) return { error: `Unknown argument: ${arg}` };
        if (o.configName) return { error: `Only one config name may be specified, got extra: ${arg}` };
        o.configName = arg;
      }
    }
  }
  return o;
}

function normalizeNo(v: string): string {
  const n = Number(v);
  return Number.isFinite(n) ? String(n).padStart(2, '0') : v;
}

async function resolveConfig(name: string | null): Promise<PublishConfig> {
  if (name) {
    if (!hasPublishConfig(name)) {
      throw new Error(
        `No config under src/publish/src/publish/configs/: ${name} (available: ${listPublishConfigNames().join(', ') || 'none'})`,
      );
    }
    return loadPublishConfig(name);
  }
  return pickPublishConfig();
}

function buildOptions(cookie: string, cfg: PublishConfig): juejin.JuejinOptions {
  return { cookie, timeoutMs: cfg.timeoutMs };
}

function buildHooks(opts: CliOptions): juejin.JuejinHooks {
  return {
    onRetry: ({ attempt, errMsg, waitMs }) =>
      dim(`  ↻ retry #${attempt} (${errMsg}), in ${Math.round(waitMs / 1000)}s…`),
    onDebug: opts.debug ? (line) => dim(`  [debug] ${line}`) : undefined,
  };
}

// ============ Subcommands ============

/** Brief material from the README chapter nav + body intro */
interface BriefMaterial {
  /** The "one-line description" from the README table */
  oneLiner: string;
  /** The blockquote at the start of the body ("> Chapter goal: ..."), absent for 09/10/11 */
  intro: string;
}

function gatherBriefMaterials(cfg: PublishConfig, articles: Article[]): Map<string, BriefMaterial> {
  const readmePath = path.join(cfg.sourceDir, 'README.md');
  const map = new Map<string, BriefMaterial>();
  let tableLines: string[] = [];
  try {
    tableLines = readFileSync(readmePath, 'utf8').replace(/\r\n/g, '\n').split('\n');
  } catch {
    tableLines = [];
  }
  for (const a of articles) {
    // Accept both | 09 | and | 9 |; cells are | no. | [title](file) | one-line description |
    const row = tableLines.find((l) => new RegExp(`^\\|\\s*0?${a.order}\\s*\\|`).test(l));
    const cells = row ? row.split('|').map((c) => c.trim()) : [];
    const oneLiner = cells.length >= 4 ? cells[3] : '';
    map.set(a.no, { oneLiner, intro: extractIntro(a.content) });
  }
  return map;
}

function runSuggestBriefs(cfg: PublishConfig, articles: Article[]): number {
  const materials = gatherBriefMaterials(cfg, articles);
  header('Brief material (print only, does not modify files)');
  info('');
  for (const a of articles) {
    const current = cfg.briefs[a.no]?.trim() ?? '';
    const { cp } = countBrief(current);
    const problem = current ? briefProblem(current) : 'no brief yet';
    info(`${a.no}  ${a.title}`);
    const issue = problem ? pc.yellow(`  [${problem}]`) : '';
    dim(`    Current (${cp} chars)${issue}: ${current || '(empty)'}`);
    const material = materials.get(a.no);
    if (material?.oneLiner) dim(`    README: ${material.oneLiner}`);
    if (material?.intro) dim(`    Intro : ${material.intro.slice(0, 140)}${material.intro.length > 140 ? '…' : ''}`);
    info('');
  }
  return 0;
}

function checkBriefs(cfg: PublishConfig, articles: Article[], skip: boolean): string[] {
  const problems: string[] = [];
  for (const a of articles) {
    const brief = cfg.briefs[a.no]?.trim() ?? '';
    if (!brief) {
      problems.push(`${a.no} has no brief configured`);
      continue;
    }
    const p = briefProblem(brief);
    if (p && !skip) problems.push(`${a.no} ${p}`);
  }
  return problems;
}

function selectArticles(articles: Article[], opts: CliOptions): Article[] {
  let list = articles;
  if (opts.only) {
    const want = normalizeNo(opts.only);
    list = list.filter((a) => a.no === want);
  }
  if (opts.from) {
    const from = Number(opts.from);
    list = list.filter((a) => a.order >= from);
  }
  if (opts.to) {
    const to = Number(opts.to);
    list = list.filter((a) => a.order <= to);
  }
  return list;
}

function runList(cfg: PublishConfig, articles: Article[], opts: CliOptions): number {
  const problems = checkBriefs(cfg, articles, opts.skipBriefCheck);
  header(`${cfg.sourceDir} pending articles`);
  info('');
  for (const a of articles) {
    const brief = cfg.briefs[a.no]?.trim() ?? '';
    const { cp, u16 } = countBrief(brief);
    const p = briefProblem(brief);
    const mark = p ? pc.red('✖') : briefTight(brief) ? pc.yellow('!') : pc.green('✓');
    info(`${mark} ${a.no}  ${a.title}`);
    dim(`     ${cp} chars(cp)/${u16}(u16) · body ${a.content.length} chars · ${a.fileName}`);
    if (p) error(`     ${p}`);
    else if (briefHasMarkdown(brief)) dim('     Hint: the brief contains markdown markup, Juejin displays it as plain text');
  }
  info('');
  info(`${articles.length} articles in total`);
  if (problems.length > 0) {
    error(`Brief validation failed (${problems.length} issues), fix them before publishing:`);
    problems.forEach((p) => error(`  · ${p}`));
    return 1;
  }
  if (!cfg.categoryId) {
    error('categoryId is missing from the config; look up the real id with --categories and put it in the config');
    return 1;
  }
  if (cfg.tagIds.length === 0) {
    error('tagIds is empty in the config; look up the real id with --tags <keyword> and put it in the config');
    return 1;
  }
  success('Brief and category/tag configs are complete');
  return 0;
}

// ============ Main publish flow ============

async function makeDraft(
  cfg: PublishConfig,
  state: PublishState,
  article: Article,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  interactive: boolean,
): Promise<{ draftId: string } | null> {
  const brief = cfg.briefs[article.no]?.trim() ?? '';
  const sp = interactive ? startSpinner(`${article.no} · creating draft`) : null;
  const res = await juejin.createDraft(
    {
      title: article.title,
      briefContent: brief,
      markContent: article.content,
      categoryId: cfg.categoryId,
      tagIds: cfg.tagIds,
      coverImage: state.coverImage,
    },
    options,
    hooks,
  );

  if (!res.ok) {
    sp?.stopError(`✖ ${article.no} draft creation failed`);
    error(`✖ ${article.no} draft creation failed: ${res.errMsg}`);
    if (res.authExpired) {
      error('  Login session has expired. Update the Cookie and re-run — at this point no article has been made public yet.');
    }
    if (res.raw) dim(`  Response: ${res.raw.slice(0, 300)}`);
    return null;
  }

  const draftId = res.data?.draftId ?? '';
  sp?.stopSuccess(`✔ ${article.no} draft created`);
  return { draftId };
}

async function doPublish(
  state: PublishState,
  articleNo: string,
  draftId: string,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  interactive: boolean,
  save: () => boolean,
): Promise<'ok' | 'unknown' | 'failed'> {
  const sp = interactive ? startSpinner(`${articleNo} · publishing`) : null;
  const res = await juejin.publishArticle(draftId, options, hooks);

  const st = state.articles[articleNo];
  // Persist "publish sent" first so every branch below can resume correctly
  if (!res.ok) {
    st.publishAttemptedAt = new Date().toISOString();
    st.updatedAt = st.publishAttemptedAt;
    save();
  }

  if (res.ok) {
    const articleId = res.data?.articleId ?? null;
    st.status = 'published';
    st.articleId = articleId;
    st.url = articleId ? `https://juejin.cn/post/${articleId}` : null;
    st.needsCheck = !articleId; // err_no=0 but no article_id given
    st.publishAttemptedAt = null;
    st.updatedAt = new Date().toISOString();
    sp?.stopSuccess(`✔ ${articleNo} published`);
    if (res.authExpired) {
      error('  Login session has expired. Update the Cookie and re-run — at this point no article has been made public yet.');
    }
    return 'ok';
  }

  sp?.stopError(`✖ ${articleNo} publish failed`);
  if (res.kind === 'business') {
    error(`✖ ${articleNo} publish failed: ${res.errMsg}`);
    if (res.raw) dim(`  Response: ${res.raw.slice(0, 300)}`);
    return 'failed';
  }
  // Network/timeout/5xx: whether it was published is unknown, so we must never auto-resend
  error(`✖ ${articleNo} publish result unknown (${res.errMsg})`);
  return 'unknown';
}

/**
 * Pre-publish validation: the draft body must match local text verbatim.
 *
 * Juejin's editor re-renders Markdown client-side (per an open-source report), so the API can
 * "succeed" with an empty/garbled body; the detail API's `mark_content` lets us check automatically.
 *
 * null = proceed; a string = why we must stop. A read-back mismatch always stops (publishing a bad body is irreversible).
 */
async function verifyDraftContent(
  article: Article,
  draftId: string,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
): Promise<string | null> {
  const res = await juejin.getDraft(draftId, options, hooks);
  // ok=true implies data (getDraft fails if article_draft missing), but ok/data isn't discriminable; guard explicitly
  if (!res.ok || !res.data) return null;

  const remote = res.data.markContent.replace(/\r\n/g, '\n');
  if (remote === article.content) return null;

  if (!remote.trim()) {
    return (
      `${article.no} the draft body is empty — the local ${article.content.length} characters were not saved.\n` +
      `  draft id ${draftId}. This is not a local parsing problem; the submission step lost the body; publishing it would produce an empty article.`
    );
  }
  return (
    `${article.no} the draft body does not match the local text (remote ${remote.length} chars / local ${article.content.length} chars).\n` +
    `  draft id ${draftId}. Juejin may have normalized the Markdown, or the submission may have been truncated.`
  );
}

/**
 * Read back the cover URL set manually in the Juejin editor so remaining articles reuse it.
 * ⚠️ Not skipped by --yes (else only the first article gets a cover); failure just means no cover.
 */
async function readBackCover(
  state: PublishState,
  draftId: string,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  save: () => boolean,
): Promise<void> {
  // Detail endpoint, not list: the list blanks large fields and breaks cover read-back
  const res = await juejin.getDraft(draftId, options, hooks);
  if (!res.ok) {
    dim(`  Failed to read draft detail (${res.errMsg}), skipping cover read-back, publishing is unaffected.`);
    return;
  }
  const cover = res.data?.coverImage ?? '';
  if (cover) {
    state.coverImage = cover;
    save();
    success('  Cover URL read back, will be reused for the remaining articles');
    dim(`  ${cover}`);
  } else {
    dim('  No cover URL was read (it may not be set yet). Continuing to publish is unaffected, and the remaining articles will have no cover either.');
    dim('  To add a cover: set it and re-run this command, and it will be read again.');
  }
}

/**
 * Confirmation point: pause after the 1st draft to set the cover and check layout.
 * `draftsOnly` only changes the wording; the pause is worthwhile in both modes.
 */
async function runGate(
  state: PublishState,
  draftId: string,
  article: Article,
  remaining: number,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  save: () => boolean,
  draftsOnly: boolean,
): Promise<'continue' | 'pause'> {
  const draftUrl = `https://juejin.cn/editor/drafts/${draftId}`;
  const resumeCmd = `node src/publish/publish.ts juejin${draftsOnly ? ' --drafts-only' : ''} --yes`;
  const whatNext = draftsOnly ? 'continue creating drafts' : 'continue publishing';
  info('');
  header('The 1st draft is created, please confirm manually');
  info(`  Draft URL: ${draftUrl}`);
  info(`  Please open it: (1) set the cover (the 1788098108256..jpg on the desktop) (2) check the Markdown layout`);
  info('');

  await readBackCover(state, draftId, options, hooks, save);
  info('');

  const interactive = isTTY();
  if (!interactive) {
    // Non-TTY: don't guess or block — draft created, state saved
    info(`  Once you've confirmed the layout is fine, run: ${resumeCmd}`);
    info(`  (reuses the created draft, will not create a duplicate)`);
    return 'pause';
  }

  const ans = await confirm({
    message: `Once you've confirmed the layout and cover are fine, ${whatNext} this article and the remaining ${remaining}?`,
  });
  if (typeof ans !== 'boolean') process.exit(130);
  if (!ans) {
    info('');
    info(`Paused. Once confirmed, run: ${resumeCmd}`);
    info(`Draft id saved (${draftId}); re-running will reuse it and will not create a duplicate.`);
    return 'pause';
  }
  return 'continue';
}

export async function run(argv: string[]): Promise<number> {
  const parsed = parseArgs(argv);
  if ('error' in parsed) {
    error(parsed.error);
    error('Usage: node src/publish/publish.ts juejin [config name] [--list|--dry-run|--yes|--only NN|--from NN|...]');
    return 1;
  }
  const opts = parsed;

  let cfg: PublishConfig;
  try {
    cfg = await resolveConfig(opts.configName);
  } catch (err) {
    error((err as Error).message);
    return 1;
  }

  const loaded = loadArticles(
    cfg.sourceDir,
    cfg.filePrefix,
    cfg.fromNumber,
    cfg.titleSource ?? 'h1',
    cfg.titlePrefix ?? '',
  );
  if ('error' in loaded) {
    error(loaded.error);
    return 1;
  }
  const all = loaded.articles;

  // ---- Subcommands that need no network ----
  if (opts.suggestBriefs) return runSuggestBriefs(cfg, all);
  if (opts.list) return runList(cfg, selectArticles(all, opts), opts);

  // ---- Below requires a Cookie. dry-run sends no requests, so it is allowed without one ----
  const cookieResult = juejin.loadCookie(REPO_ROOT);
  const cookieError = 'error' in cookieResult ? cookieResult.error : null;
  const cookie = 'error' in cookieResult ? '' : cookieResult.cookie;
  const options = buildOptions(cookie, cfg);
  const hooks = buildHooks(opts);

  const requireCookie = (): boolean => {
    if (cookieError) {
      error(cookieError);
      return false;
    }
    return true;
  };

  // --categories/--tags need no login in testing (see juejin.ts endpoints), allowed without a Cookie
  if (opts.categories) {
    header('Juejin categories');
    const res = await juejin.queryCategories(options, hooks);
    if (!res.ok) {
      error(`Query failed: ${res.errMsg}`);
      if (res.authExpired) error('  Login session has expired, please update the Cookie.');
      if (res.raw) dim(`  Response: ${res.raw.slice(0, 300)}`);
      return 1;
    }
    for (const c of res.data ?? []) info(`  ${c.categoryId}  ${c.categoryName}`);
    return 0;
  }

  if (opts.tags !== null) {
    header(`Juejin tags: ${opts.tags}`);
    const res = await juejin.queryTags(opts.tags, options, hooks);
    if (!res.ok) {
      error(`Query failed: ${res.errMsg}`);
      if (res.authExpired) error('  Login session has expired, please update the Cookie.');
      if (res.raw) dim(`  Response: ${res.raw.slice(0, 300)}`);
      return 1;
    }
    if ((res.data ?? []).length === 0) info('  (no matching tags)');
    for (const t of res.data ?? []) info(`  ${t.tagId}  ${t.tagName}`);
    return 0;
  }

  // ---- Publish flow ----
  if (!opts.dryRun && !requireCookie()) return 1;

  const ignoreProblem = assertCookieIgnored(REPO_ROOT);
  if (ignoreProblem) {
    error(ignoreProblem);
    return 1;
  }

  const stateFile = stateFileName(REPO_ROOT, opts.configName ?? 'vllm-toturial');
  const loadedState = loadState(stateFile);
  if ('error' in loadedState) {
    error(loadedState.error);
    return 1;
  }
  const state = loadedState.state;

  const lock = acquireLock(stateFile);
  if ('error' in lock) {
    error(lock.error);
    return 1;
  }

  try {
    if (opts.sync) return await runSync(cfg, state, all, opts, options, hooks, stateFile);
    return await runPublish(cfg, state, all, opts, options, hooks, stateFile);
  } finally {
    releaseLock(stateFile);
  }
}

/**
 * Align the **local config** with the **drafts already on Juejin** (`--sync`, old name `--rename`).
 *
 * The draft hash covers only title+body, so a changed brief is invisible and --publish would skip the draft forever.
 *
 * ⚠️ `article_draft/update` needs all fields (title-only risks wiping the body): title/brief from
 * config, body only when `contentHash` changed, category/tags/cover from remote.
 *
 * `drafted` updates in place; `published` re-publishes in place (article_id/link/stats kept).
 * Read back after update: a clobbered public body is irreversible, so any mismatch stops the run.
 */
async function runSync(
  cfg: PublishConfig,
  state: PublishState,
  all: Article[],
  opts: CliOptions,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  stateFile: string,
): Promise<number> {
  const save = (): boolean => {
    const r = saveState(stateFile, state);
    if ('error' in r) {
      error(r.error);
      return false;
    }
    return true;
  };

  const selected = selectArticles(all, opts);
  if (selected.length === 0) {
    error('No matching articles (check the number range of --only / --from)');
    return 1;
  }

  // ---- Pre-check: list everything that can't sync; touch nothing ----
  const problems: string[] = [];
  for (const a of selected) {
    const st = state.articles[a.no];
    if (!st) problems.push(`${a.no} has no record in the state file`);
    else if (st.status === 'pending') problems.push(`${a.no} has no draft yet, run --drafts-only first`);
    else if (!st.draftId) problems.push(`${a.no} has no recorded draft id, cannot sync`);
  }
  if (problems.length > 0) {
    error(`There are ${problems.length} articles that cannot be synced, none will be touched this run:`);
    problems.forEach((p) => error(`  · ${p}`));
    return 1;
  }

  const delayMs = opts.delayMs ?? cfg.delayMs;
  const interactive = isTTY();
  header(`Sync drafts ${cfg.sourceDir} → Juejin`);
  dim(
    `  Title source: ${cfg.titleSource === 'fileName' ? 'file name (−.md)' : 'original H1'} · ` +
      `pending ${selected.length} articles · ${opts.dryRun ? 'dry-run' : 'align titles and briefs, re-publish published ones'}`,
  );
  info('');

  // Read titles from the **online article record**: Juejin syncs draft titles to it asynchronously
  // and not always on the first try, so local records alone would skip un-synced articles forever (dry-run: local only).
  const liveTitles = new Map<string, string>();
  if (!opts.dryRun) {
    const liveRes = await juejin.listArticles(options, hooks);
    if (liveRes.ok) {
      for (const a of liveRes.data ?? []) liveTitles.set(a.articleId, a.title);
    } else {
      dim(`  ! Failed to read the online article list (${liveRes.errMsg}), this run judges changes from local records only.`);
    }
  }

  let synced = 0;
  let skipped = 0;

  for (const article of selected) {
    const st = state.articles[article.no];
    // Unreadable → unknown (e.g. more than one page); use the local record, don't call it a mismatch
    const live = st.articleId ? liveTitles.get(st.articleId) : undefined;

    const wantBrief = cfg.briefs[article.no]?.trim() ?? '';
    const titleSame = st.title === article.title;
    // Old state files lack a brief field → undefined ≠ wantBrief → one full refresh, as intended
    const briefSame = st.brief === wantBrief;
    const bodySame = st.contentHash === article.contentHash;

    // All three aligned and live title matches too (or unreadable) → done, skip (idempotent)
    if (titleSame && briefSame && bodySame && (live === undefined || live === article.title)) {
      dim(`✓ ${article.no} title, brief, and body are already at the target values, skipping`);
      skipped++;
      continue;
    }
    if (titleSame && live !== undefined && live !== article.title) {
      // Draft updated+published last time but async sync didn't take — it already has the target title, so just re-publish
      dim(`  ! ${article.no} the online title is still "${live}", publishing once more`);
    }

    if (opts.dryRun) {
      const bits: string[] = [];
      if (!titleSame) bits.push(`title "${st.title ?? '(not recorded)'}" → "${article.title}"`);
      if (!briefSame) bits.push(st.brief === undefined ? 'brief (first recording)' : 'brief');
      if (!bodySame) bits.push('body');
      if (bits.length === 0) bits.push('publish only');
      info(`${article.no}  ${bits.join(' · ')}`);
      synced++;
      continue;
    }

    const draftId = st.draftId as string;
    const sp = interactive ? startSpinner(`${article.no} · read draft`) : null;
    const detail = await juejin.getDraft(draftId, options, hooks);
    if (!detail.ok || !detail.data) {
      sp?.stopError(`✖ ${article.no} failed to read draft`);
      error(`✖ ${article.no} failed to read draft: ${detail.errMsg}`);
      if (detail.authExpired) error('  Login session has expired, please update the Cookie and re-run.');
      if (detail.raw) dim(`  Response: ${detail.raw.slice(0, 300)}`);
      return 1;
    }
    const remote = detail.data;
    sp?.stopSuccess(`✔ ${article.no} ${remote.title || '(no title)'}`);

    // Remote at target but success never recorded — last run was interrupted after the draft update; just publish
    const needUpdate = remote.title !== article.title || remote.briefContent.trim() !== wantBrief;

    if (needUpdate) {
      // Replace the body only when the **source file really changed**: remote markdown may be normalized, so verbatim comparison would rewrite it every run
      const contentChanged = st.contentHash !== article.contentHash;
      const sp2 = interactive ? startSpinner(`${article.no} · update draft`) : null;
      const upd = await juejin.updateDraft(
        {
          draftId,
          title: article.title,
          briefContent: wantBrief,
          markContent: contentChanged ? article.content : remote.markContent,
          categoryId: cfg.categoryId,
          tagIds: cfg.tagIds,
          coverImage: remote.coverImage || state.coverImage,
        },
        options,
        hooks,
      );
      if (!upd.ok) {
        sp2?.stopError(`✖ ${article.no} draft update failed`);
        error(`✖ ${article.no} draft update failed: ${upd.errMsg}`);
        if (upd.authExpired) error('  Login session has expired, please update the Cookie and re-run.');
        if (upd.raw) dim(`  Response: ${upd.raw.slice(0, 300)}`);
        return 1;
      }
      sp2?.stopSuccess(`✔ ${article.no} draft updated`);

      // ---- Safety valve: read back to confirm title/brief took effect and body wasn't clobbered ----
      const after = await juejin.getDraft(draftId, options, hooks);
      if (after.ok && after.data) {
        if (after.data.title !== article.title) {
          error(`✖ ${article.no} the title read back is still "${after.data.title}"; the title update did not take effect, stopped without re-publishing.`);
          return 1;
        }
        if (after.data.briefContent.trim() !== wantBrief) {
          error(`✖ ${article.no} the brief read back does not match the target; stopped without re-publishing (draft id ${draftId}).`);
          return 1;
        }
        if (after.data.markContent.length !== remote.markContent.length && !contentChanged) {
          error(
            `✖ ${article.no} syncing the title/brief incidentally changed the body (remote ${remote.markContent.length} → ` +
              `${after.data.markContent.length} chars); stopped without re-publishing.\n` +
              `  draft id ${draftId}, the online article was not affected.`,
          );
          return 1;
        }
      } else {
        dim('  ! Failed to read the draft back, skipping validation (the draft was submitted with the target values)');
      }
    } else {
      dim(`  ${article.no} the draft is already at the target value, only needs one more publish`);
    }

    // Draft side is aligned — record state first so a later publish, success or failure, won't update the draft again
    st.title = article.title;
    st.brief = wantBrief;
    st.contentHash = article.contentHash;
    st.updatedAt = new Date().toISOString();
    if (!save()) return 1;

    // ---- Unpublished draft: stop here, no publish ----
    if (st.status !== 'published') {
      dim(`  ✔ ${article.no} draft synced (not published): https://juejin.cn/editor/drafts/${draftId}`);
      info('');
      synced++;
      if (delayMs > 0 && article !== selected[selected.length - 1]) await delay(delayMs);
      continue;
    }

    // ---- Published: re-publish (in-place update, article_id unchanged) ----
    const sp3 = interactive ? startSpinner(`${article.no} · re-publishing`) : null;
    const pub = await juejin.publishArticle(draftId, options, hooks);
    if (!pub.ok) {
      sp3?.stopError(`✖ ${article.no} re-publish failed`);
      st.publishAttemptedAt = new Date().toISOString();
      st.updatedAt = st.publishAttemptedAt;
      save();
      error(`✖ ${article.no} re-publish failed: ${pub.errMsg}`);
      if (pub.raw) dim(`  Response: ${pub.raw.slice(0, 300)}`);
      dim('  The draft has the new content but the live version is still old. After confirming, re-run with --sync; it skips the draft update and only publishes.');
      return 1;
    }

    const articleId = pub.data?.articleId ?? st.articleId;
    st.articleId = articleId;
    st.url = articleId ? `https://juejin.cn/post/${articleId}` : st.url;
    st.publishAttemptedAt = null;
    st.updatedAt = new Date().toISOString();
    if (!save()) return 1;

    sp3?.stopSuccess(`✔ ${article.no} → ${article.title}`);
    if (st.url) info(`  ${st.url}`);
    info('');
    synced++;

    if (delayMs > 0 && article !== selected[selected.length - 1]) {
      await delay(delayMs);
    }
  }

  header(opts.dryRun ? 'dry-run finished (no requests were sent)' : 'Done');
  if (opts.dryRun) {
    info(`  To be synced: ${synced} articles`);
    dim('  dry-run does not go online, so this judges from local records; the actual run also reads each draft and compares.');
    return 0;
  }

  info(`  Synced this run: ${synced} articles · skipped (already at target): ${skipped} articles`);

  // Not reading the live title here: sync is async and won't have changed right after publishing,
  // so a read-back would just false-alarm. The reliable check is at the next run's start.
  if (synced > 0) {
    dim('  Juejin syncs draft titles to the online article record asynchronously — fast ones take minutes, slow ones need another publish to take effect.');
    dim('  Re-run --sync in a few minutes to check: titles not yet changed are re-published (idempotent), effective ones are skipped.');
  }
  return 0;
}

async function runPublish(
  cfg: PublishConfig,
  state: PublishState,
  all: Article[],
  opts: CliOptions,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  stateFile: string,
): Promise<number> {
  const save = (): boolean => {
    const r = saveState(stateFile, state);
    if ('error' in r) {
      error(r.error);
      return false;
    }
    return true;
  };

  if (opts.orphans) {
    const drafted = Object.entries(state.articles).filter(([, s]) => s.status === 'drafted');
    header('Orphan drafts (--orphans)');
    if (drafted.length === 0) info('  No drafts pending publication');
    for (const [no, s] of drafted) {
      info(`  ${no}  draft ${s.draftId}  https://juejin.cn/editor/drafts/${s.draftId}`);
    }
    return 0;
  }

  let selected = selectArticles(all, opts);
  if (selected.length === 0) {
    error(`No matching articles (check the number range of --only / --from)`);
    return 1;
  }

  // ---- Brief pre-check: report every failure at once before any network call ----
  const problems = checkBriefs(cfg, selected, opts.skipBriefCheck);
  if (problems.length > 0) {
    error(`Brief validation failed (${problems.length} issues), nothing will be published:`);
    problems.forEach((p) => error(`  · ${p}`));
    dim('  Fix the briefs in the config and re-run. To skip validation anyway, add --skip-brief-check.');
    return 1;
  }
  if (!opts.dryRun) {
    if (!cfg.categoryId) {
      error('categoryId is missing from the config; look up the real id with --categories and put it in the config');
      return 1;
    }
    // The limit of 3 is what the Juejin server returned in testing ("you can add at most 3 tags to an article", err_no=4031)
    if (cfg.tagIds.length === 0 || cfg.tagIds.length > MAX_TAGS) {
      error(`tagIds must be 1~${MAX_TAGS} (currently ${cfg.tagIds.length}), look up the real id with --tags <keyword>`);
      return 1;
    }
  }

  const interactive = isTTY();
  const delayMs = opts.delayMs ?? cfg.delayMs;

  const mode = opts.dryRun ? 'dry-run' : opts.draftsOnly ? 'drafts only (no publish)' : 'actual publish';
  header(`${opts.draftsOnly ? 'Create drafts' : 'Publish'} ${cfg.sourceDir} → Juejin`);
  dim(`  Pending ${selected.length} articles · interval ${delayMs}ms · ${mode}`);
  if (!opts.dryRun && !opts.draftsOnly) {
    dim('  Hint: publishing many articles in a row may trigger Juejin risk control; if blocked, increase the interval with --delay and re-run.');
  }
  if (opts.draftsOnly) {
    dim('  Drafts are private and not public — whether to publish and when is up to you, article by article, in the Juejin backend.');
  }
  info('');

  let gatePassed = opts.yes;
  /** On the --yes path, try the cover read-back at most once (no need to retry per article) */
  let coverChecked = false;
  let processed = 0;
  let drafted = 0;
  let planned = 0;

  for (const article of selected) {
    const st = ensureArticle(state, article.no, article.contentHash);

    // Already published: skip (only --republish names a specific article to re-send)
    if (st.status === 'published' && opts.republish !== article.no) {
      dim(`✓ ${article.no} already published, skipping  ${st.url ?? ''}`);
      continue;
    }

    // Last publish result unknown: never auto-resend
    if (st.status === 'drafted' && st.publishAttemptedAt && opts.republish !== article.no) {
      error(`✖ ${article.no} last publish result unknown (${st.publishAttemptedAt}), stopped waiting for your confirmation.`);
      info('  First check in the Juejin creator center whether this article was published:');
      info('   · published → manually change the status of that article in the state file to published and fill in the url');
      info(`   · not published → re-publish with --republish ${article.no} (reuses the existing draft) `);
      return 1;
    }

    // Body changed: the draft is stale
    if (st.status === 'drafted' && st.contentHash !== article.contentHash) {
      if (!opts.force) {
        error(`✖ the body of ${article.no} was changed after the draft was created; the draft is stale.`);
        info('  Use --force to recreate the draft (the old draft becomes an orphan, viewable with --orphans), or revert the change to the source file.');
        return 1;
      }
      dim(`  ! ${article.no} body changed, recreating the draft per --force`);
      st.status = 'pending';
      st.draftId = null;
      st.contentHash = article.contentHash;
    }

    if (opts.dryRun) {
      const brief = cfg.briefs[article.no]?.trim() ?? '';
      const { cp } = countBrief(brief);
      info(`${article.no}  ${article.title}`);
      dim(`     category ${cfg.categoryId} · tags ${cfg.tagIds.join(',')} · cover ${state.coverImage || '(empty)'}`);
      dim(`     brief (${cp} chars): ${brief}`);
      dim(`     body ${article.content.length} chars, first 3 lines:`);
      article.content.split('\n').slice(0, 3).forEach((l) => dim(`       | ${l}`));
      info('');
      planned++;
      continue;
    }

    // ---- Create draft (record first, then publish) ----
    let draftId = st.draftId;
    if (!draftId) {
      const made = await makeDraft(cfg, state, article, options, hooks, interactive);
      if (!made) return 1;
      draftId = made.draftId;
      st.draftId = draftId;
      st.status = 'drafted';
      st.contentHash = article.contentHash;
      st.brief = cfg.briefs[article.no]?.trim() ?? '';
      st.updatedAt = new Date().toISOString();
      // Persist immediately: the draft id survives even if a later publish fails or the user hits Ctrl-C
      if (!save()) return 1;
      dim(`  Draft id: ${draftId}`);
    } else {
      dim(`  ${article.no} reusing existing draft ${draftId}`);
    }

    if (!gatePassed) {
      gatePassed = true;
      const remaining = selected.filter((x) => x.order > article.order).length;
      const gate = await runGate(state, draftId, article, remaining, options, hooks, save, opts.draftsOnly);
      if (gate === 'pause') return EXIT_PAUSED;
    } else if (!coverChecked && !state.coverImage) {
      // --yes skips the confirmation gate, but the cover read-back must not be skipped with it (see readBackCover)
      coverChecked = true;
      await readBackCover(state, draftId, options, hooks, save);
    }

    // ---- Pre-publish validation that the body really got stored ----
    if (!opts.skipBodyCheck) {
      const bodyProblem = await verifyDraftContent(article, draftId, options, hooks);
      if (bodyProblem) {
        error(`✖ ${bodyProblem}`);
        error('  Stopped, nothing was published. Once you are sure it is fine, you can skip this validation with --skip-body-check.');
        return 1;
      }
    }

    // ---- Drafts-only mode: stop here, never call publish ----
    // State stays 'drafted', so a later run without this flag reuses the same draft and publishes it.
    if (opts.draftsOnly) {
      st.title = article.title;
      st.updatedAt = new Date().toISOString();
      if (!save()) return 1;
      dim(`  ✔ ${article.no} draft ready (not published): https://juejin.cn/editor/drafts/${draftId}`);
      info('');
      drafted++;
      if (delayMs > 0 && article !== selected[selected.length - 1]) await delay(delayMs);
      continue;
    }

    const outcome = await doPublish(state, article.no, draftId, options, hooks, interactive, save);
    if (outcome === 'ok') {
      // Record the published title; `--rename` uses it to tell whether the target title is live
      st.title = article.title;
      if (!save()) return 1;
      if (st.needsCheck) {
        dim('  ! Publish succeeded but no article id was returned, please confirm in the creator center');
      } else {
        info(`  ${st.url}`);
      }
      processed++;
    } else if (outcome === 'failed') {
      dim('  State has been kept; once fixed, re-run the same command to continue publishing.');
      return 1;
    } else {
      dim('  Because "whether it was published is unknown", the script will not auto-resend.');
      dim('  Once you confirm it was not published, re-publish with --republish ' + article.no + '.');
      return 1;
    }
    info('');

    if (delayMs > 0 && article !== selected[selected.length - 1]) {
      await delay(delayMs);
    }
  }

  header(opts.dryRun ? 'dry-run finished (no requests were sent)' : 'Done');
  if (opts.dryRun) {
    info(`  Validated and to be published: ${planned} articles`);
  } else if (opts.draftsOnly) {
    info(`  New drafts created this run: ${drafted} (none published)`);
    const total = Object.values(state.articles).filter((s) => s.status === 'drafted').length;
    info(`  Total drafts pending publication in the state file: ${total}`);
    info('');
    info('  To publish them: go to the draft box in the Juejin creator center and publish them one by one, or re-run this command without --drafts-only.');
    info('  (drafts already exist; re-running reuses them and does not create duplicates)');
  } else {
    info(`  Published this run: ${processed} articles`);
    const total = Object.values(state.articles).filter((s) => s.status === 'published').length;
    info(`  Total published in the state file: ${total}`);
  }
  return 0;
}

// Entry point is src/publish/publish.ts — this file only exports run(argv); SIGINT handling lives there too.
