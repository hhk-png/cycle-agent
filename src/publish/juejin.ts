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
 * 把教程文章发布到掘金。
 *
 * 用法:
 *   node src/publish/publish.ts juejin --list              # 列待发文章 + 标题 + 摘要字数(预校验)
 *   node src/publish/publish.ts juejin --categories        # 查掘金真实分类 id
 *   node src/publish/publish.ts juejin --tags vllm         # 按关键词查标签 id
 *   node src/publish/publish.ts juejin --suggest-briefs    # 打印摘要素材(只打印,不改文件)
 *   node src/publish/publish.ts juejin --dry-run           # 只打印不发送
 *   node src/publish/publish.ts juejin                     # 发布(建第 1 篇草稿后暂停等你确认)
 *   node src/publish/publish.ts juejin --yes               # 跳过确认,一次发完
 *   node src/publish/publish.ts juejin --drafts-only       # 只建草稿,不发布(草稿私有,不公开)
 *   node src/publish/publish.ts juejin --from 0 --to 4     # 只处理编号落在区间内的篇目
 *   node src/publish/publish.ts juejin --sync              # 把已有草稿对齐到配置里的标题与摘要
 *                                                  #  (改了 briefs/titleSource 之后跑它,否则草稿不会更新)
 *                                                  # 未发布的只改草稿;已发布的改完就地重新发布
 *
 * `--drafts-only` 与「发布」共用同一套状态与草稿:只建过草稿的篇目,
 * 之后去掉这个开关重跑,会**复用同一份草稿**直接发布,不会重复建。
 *
 * 退出码:
 *   0   本次待发列表全部处理完
 *   1   参数/配置/校验错误,或发布失败
 *   2   已在确认点暂停,等待人工确认(状态已落盘,可续跑)
 *   130 SIGINT
 *
 * 需要掘金 Cookie:环境变量 JUEJIN_COOKIE,或仓库根目录下 gitignored 的 .juejin-cookie。
 */

const REPO_ROOT = process.cwd();

/** 确认点暂停后的退出码 —— 暂停是检查点,不是失败 */
const EXIT_PAUSED = 2;

/**
 * 单篇最多标签数。
 * 3 是实测值 —— 建草稿时多给会返回 `err_no=4031 您最多可以为文章添加3个标签`。
 * 这个限制只在真正调接口时才暴露,dry-run 和 --list 都测不出来。
 */
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
  /** `--sync`(旧名 `--rename`):把草稿对齐到本地配置的标题与摘要 */
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

/** 手写参数解析(与 run.ts 一致,不引入解析库) */
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
      // `--rename` 是旧名,保留作别名(它当时只管标题,现在管标题+摘要)
      case '--sync': case '--rename': o.sync = true; break;
      case '--force': o.force = true; break;
      case '--skip-brief-check': o.skipBriefCheck = true; break;
      case '--skip-body-check': o.skipBodyCheck = true; break;
      case '--debug': o.debug = true; break;
      case '--tags': {
        const v = needValue();
        if (v === null) return { error: '--tags 需要一个关键词,如 --tags vllm' };
        o.tags = v;
        break;
      }
      case '--only': {
        const v = needValue();
        if (v === null) return { error: '--only 需要一个编号,如 --only 12' };
        o.only = v;
        break;
      }
      case '--from': {
        const v = needValue();
        if (v === null) return { error: '--from 需要一个编号,如 --from 15' };
        o.from = v;
        break;
      }
      case '--to': {
        const v = needValue();
        if (v === null) return { error: '--to 需要一个编号,如 --to 04' };
        o.to = v;
        break;
      }
      case '--republish': {
        const v = needValue();
        if (v === null) return { error: '--republish 需要一个编号,如 --republish 09' };
        o.republish = v;
        break;
      }
      case '--delay': {
        const v = needValue();
        const n = Number(v);
        if (v === null || !Number.isFinite(n) || n < 0) return { error: '--delay 需要毫秒数,如 --delay 30000' };
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

/** 把 '9' / '09' 都归一成两位编号 */
function normalizeNo(v: string): string {
  const n = Number(v);
  return Number.isFinite(n) ? String(n).padStart(2, '0') : v;
}

/** 复用 config.ts 的交互选择逻辑,但作用在发布配置上 */
async function resolveConfig(name: string | null): Promise<PublishConfig> {
  if (name) {
    if (!hasPublishConfig(name)) {
      throw new Error(
        `src/publish/src/publish/configs/ 下没有配置: ${name}(可用: ${listPublishConfigNames().join(', ') || '无'})`,
      );
    }
    return loadPublishConfig(name);
  }
  return pickPublishConfig();
}

/** 构造调用选项 */
function buildOptions(cookie: string, cfg: PublishConfig): juejin.JuejinOptions {
  return { cookie, timeoutMs: cfg.timeoutMs };
}

function buildHooks(opts: CliOptions): juejin.JuejinHooks {
  return {
    onRetry: ({ attempt, errMsg, waitMs }) =>
      dim(`  ↻ 第 ${attempt} 次重试(${errMsg}),${Math.round(waitMs / 1000)}s 后…`),
    onDebug: opts.debug ? (line) => dim(`  [debug] ${line}`) : undefined,
  };
}

// ============ 子命令 ============

/** 摘要素材:README 章节导航的一句话 + 正文的引言块 */
interface BriefMaterial {
  /** README 表格里的「内容一句话」 */
  oneLiner: string;
  /** 正文开头的引用块(「> 本章目标：…」),09/10/11 没有 */
  intro: string;
}

/** 从 README 的章节导航表 + 正文引言里凑素材 */
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
    // README 表格里编号是两位(| 09 |),但也兼容没有前导零的写法
    const row = tableLines.find((l) => new RegExp(`^\\|\\s*0?${a.order}\\s*\\|`).test(l));
    const cells = row ? row.split('|').map((c) => c.trim()) : [];
    // | 编号 | [标题](文件) | 内容一句话 |
    const oneLiner = cells.length >= 4 ? cells[3] : '';
    map.set(a.no, { oneLiner, intro: extractIntro(a.content) });
  }
  return map;
}

/** --suggest-briefs:把素材摆出来,只打印不改文件 */
function runSuggestBriefs(cfg: PublishConfig, articles: Article[]): number {
  const materials = gatherBriefMaterials(cfg, articles);
  header('摘要素材(只打印,不改文件)');
  info('');
  for (const a of articles) {
    const current = cfg.briefs[a.no]?.trim() ?? '';
    const { cp } = countBrief(current);
    const problem = current ? briefProblem(current) : '还没有摘要';
    info(`${a.no}  ${a.title}`);
    const issue = problem ? pc.yellow(`  [${problem}]`) : '';
    dim(`    当前(${cp} 字)${issue}: ${current || '(空)'}`);
    const material = materials.get(a.no);
    if (material?.oneLiner) dim(`    README: ${material.oneLiner}`);
    if (material?.intro) dim(`    引言  : ${material.intro.slice(0, 140)}${material.intro.length > 140 ? '…' : ''}`);
    info('');
  }
  return 0;
}

/** --list / --dry-run 共用的待发清单与摘要预检 */
function checkBriefs(cfg: PublishConfig, articles: Article[], skip: boolean): string[] {
  const problems: string[] = [];
  for (const a of articles) {
    const brief = cfg.briefs[a.no]?.trim() ?? '';
    if (!brief) {
      problems.push(`${a.no} 没有配置摘要`);
      continue;
    }
    const p = briefProblem(brief);
    if (p && !skip) problems.push(`${a.no} ${p}`);
  }
  return problems;
}

/** 按 --only / --from / --to 收窄待发范围 */
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
  header(`${cfg.sourceDir} 待发布文章`);
  info('');
  for (const a of articles) {
    const brief = cfg.briefs[a.no]?.trim() ?? '';
    const { cp, u16 } = countBrief(brief);
    const p = briefProblem(brief);
    const mark = p ? pc.red('✖') : briefTight(brief) ? pc.yellow('!') : pc.green('✓');
    info(`${mark} ${a.no}  ${a.title}`);
    dim(`     ${cp} 字(cp)/${u16}(u16) · 正文 ${a.content.length} 字符 · ${a.fileName}`);
    if (p) error(`     ${p}`);
    else if (briefHasMarkdown(brief)) dim('     提示: 摘要里含 markdown 标记,掘金按纯文本展示');
  }
  info('');
  info(`共 ${articles.length} 篇`);
  if (problems.length > 0) {
    error(`摘要校验未通过(${problems.length} 项),发布前必须先修好:`);
    problems.forEach((p) => error(`  · ${p}`));
    return 1;
  }
  if (!cfg.categoryId) {
    error('配置里缺少 categoryId,请用 --categories 查到真实 id 后填进配置');
    return 1;
  }
  if (cfg.tagIds.length === 0) {
    error('配置里 tagIds 为空,请用 --tags <关键词> 查到真实 id 后填进配置');
    return 1;
  }
  success('摘要与分类/标签配置齐备');
  return 0;
}

// ============ 发布主流程 ============

/** 建草稿 */
async function makeDraft(
  cfg: PublishConfig,
  state: PublishState,
  article: Article,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  interactive: boolean,
): Promise<{ draftId: string } | null> {
  const brief = cfg.briefs[article.no]?.trim() ?? '';
  const sp = interactive ? startSpinner(`${article.no} · 建草稿中`) : null;
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
    sp?.stopError(`✖ ${article.no} 建草稿失败`);
    error(`✖ ${article.no} 建草稿失败: ${res.errMsg}`);
    if (res.authExpired) {
      error('  登录态已失效。请更新 Cookie 后重跑 —— 此时一篇文章都还没公开。');
    }
    if (res.raw) dim(`  响应: ${res.raw.slice(0, 300)}`);
    return null;
  }

  const draftId = res.data?.draftId ?? '';
  sp?.stopSuccess(`✔ ${article.no} 草稿已建`);
  return { draftId };
}

/** 发布草稿 */
async function doPublish(
  state: PublishState,
  articleNo: string,
  draftId: string,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  interactive: boolean,
  save: () => boolean,
): Promise<'ok' | 'unknown' | 'failed'> {
  const sp = interactive ? startSpinner(`${articleNo} · 发布中`) : null;
  const res = await juejin.publishArticle(draftId, options, hooks);

  const st = state.articles[articleNo];
  // 先落盘「发布请求已发出」,这样无论下面走哪条分支都能正确续跑
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
    st.needsCheck = !articleId; // err_no=0 但没给 article_id
    st.publishAttemptedAt = null;
    st.updatedAt = new Date().toISOString();
    sp?.stopSuccess(`✔ ${articleNo} 已发布`);
    if (res.authExpired) {
      error('  登录态已失效。请更新 Cookie 后重跑 —— 此时一篇文章都还没公开。');
    }
    return 'ok';
  }

  sp?.stopError(`✖ ${articleNo} 发布失败`);
  if (res.kind === 'business') {
    error(`✖ ${articleNo} 发布失败: ${res.errMsg}`);
    if (res.raw) dim(`  响应: ${res.raw.slice(0, 300)}`);
    return 'failed';
  }
  // 网络/超时/5xx:是否已发布不可知,绝不能自动重发
  error(`✖ ${articleNo} 发布结果未知(${res.errMsg})`);
  return 'unknown';
}

/**
 * 发布前校验:草稿里存的正文必须与本地逐字一致。
 *
 * 这是本方案里排第一的风险 —— 有开源实现反馈掘金编辑器会在客户端重新渲染
 * Markdown,可能「接口返回成功,但正文是空的/乱码」。实测 detail 接口能读到
 * `mark_content`,所以能自动验:不必靠肉眼看,更不必等 14 篇空文章发出去才发现。
 *
 * 返回 null 表示可以继续;返回字符串表示必须停下的原因。
 * 读不回来不算失败(与封面读回同样的取舍),但**读到了却对不上就一定停** ——
 * 正文不一致时发布是不可逆的,宁可不发。
 */
async function verifyDraftContent(
  article: Article,
  draftId: string,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
): Promise<string | null> {
  const res = await juejin.getDraft(draftId, options, hooks);
  // ok 为 true 时 data 必然有值(getDraft 在缺 article_draft 时已返回失败),
  // 但 JuejinResult 的 ok/data 不是可辨识联合,TS 收窄不到,这里显式兜一层
  if (!res.ok || !res.data) return null;

  const remote = res.data.markContent.replace(/\r\n/g, '\n');
  if (remote === article.content) return null;

  if (!remote.trim()) {
    return (
      `${article.no} 草稿正文是空的 —— 本地 ${article.content.length} 字符没有存进去。\n` +
      `  草稿 id ${draftId}。不是本地解析的问题,是提交环节丢了正文;发出去会是一篇空文章。`
    );
  }
  return (
    `${article.no} 草稿正文与本地不一致(远端 ${remote.length} 字符 / 本地 ${article.content.length} 字符)。\n` +
    `  草稿 id ${draftId}。可能是掘金对 Markdown 做了规范化,也可能是提交被截断。`
  );
}

/**
 * 把人工在掘金编辑器里设置的封面 URL 读回来,存进状态供其余各篇复用。
 *
 * ⚠️ 刻意与「确认门」解耦:`--yes` 会跳过确认,但**封面读回不该跟着被跳过** ——
 * 否则第 1 篇之后各篇建草稿时 state.coverImage 仍是空的,结果只有第一篇带封面。
 * 读不回来不算失败(退化为不带封面),不阻塞发布。
 */
async function readBackCover(
  state: PublishState,
  draftId: string,
  options: juejin.JuejinOptions,
  hooks: juejin.JuejinHooks,
  save: () => boolean,
): Promise<void> {
  // 走 detail 而不是列表接口:列表会把大字段抹成空,封面读回会不可靠
  const res = await juejin.getDraft(draftId, options, hooks);
  if (!res.ok) {
    dim(`  读取草稿详情失败(${res.errMsg}),跳过封面读回,不影响发布。`);
    return;
  }
  const cover = res.data?.coverImage ?? '';
  if (cover) {
    state.coverImage = cover;
    save();
    success('  已读回封面 URL,将复用到其余各篇');
    dim(`  ${cover}`);
  } else {
    dim('  没读到封面 URL(可能还没设置)。继续发布不受影响,其余各篇也不会带封面。');
    dim('  若想补封面:设置好后重新运行本命令,会重新读取。');
  }
}

/**
 * 确认点:第 1 篇草稿建好后暂停,让你去编辑器里设封面 + 看排版。
 *
 * `draftsOnly` 只改提示语 —— 建草稿之前的那次停顿在两种模式下都值得有,
 * 因为排版是「发布出去就改不动」的东西,而建草稿是免费的(不公开)。
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
  const whatNext = draftsOnly ? '继续建草稿' : '继续发布';
  info('');
  header('已建好第 1 篇草稿,请人工确认');
  info(`  草稿地址: ${draftUrl}`);
  info(`  请打开它:① 设置封面(桌面上的 1788098108256..jpg) ② 检查 Markdown 排版`);
  info('');

  await readBackCover(state, draftId, options, hooks, save);
  info('');

  const interactive = isTTY();
  if (!interactive) {
    // 非 TTY:不猜、不阻塞。草稿已建好(无公开副作用),状态已落盘
    info(`  确认排版无误后运行: ${resumeCmd}`);
    info(`  (会复用已建好的草稿,不会重复建草稿)`);
    return 'pause';
  }

  const ans = await confirm({
    message: `确认排版与封面无误后${whatNext}这篇及剩余 ${remaining} 篇?`,
  });
  if (typeof ans !== 'boolean') process.exit(130);
  if (!ans) {
    info('');
    info(`已暂停。确认无误后运行: ${resumeCmd}`);
    info(`草稿 id 已保存(${draftId}),重跑会复用它,不会重复建草稿。`);
    return 'pause';
  }
  return 'continue';
}

export async function run(argv: string[]): Promise<number> {
  const parsed = parseArgs(argv);
  if ('error' in parsed) {
    error(parsed.error);
    error('用法: node src/publish/publish.ts juejin [配置名] [--list|--dry-run|--yes|--only NN|--from NN|...]');
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

  // ---- 不需要网络的子命令 ----
  if (opts.suggestBriefs) return runSuggestBriefs(cfg, all);
  if (opts.list) return runList(cfg, selectArticles(all, opts), opts);

  // ---- 以下需要 Cookie。dry-run 不发任何请求,没 Cookie 也放行 ----
  const cookieResult = juejin.loadCookie(REPO_ROOT);
  const cookieError = 'error' in cookieResult ? cookieResult.error : null;
  const cookie = 'error' in cookieResult ? '' : cookieResult.cookie;
  const options = buildOptions(cookie, cfg);
  const hooks = buildHooks(opts);

  /** 正式发布必须有 Cookie */
  const requireCookie = (): boolean => {
    if (cookieError) {
      error(cookieError);
      return false;
    }
    return true;
  };

  // --categories / --tags 实测不需要登录态(见 juejin.ts 的端点注释),没 Cookie 也放行
  if (opts.categories) {
    header('掘金分类');
    const res = await juejin.queryCategories(options, hooks);
    if (!res.ok) {
      error(`查询失败: ${res.errMsg}`);
      if (res.authExpired) error('  登录态已失效,请更新 Cookie。');
      if (res.raw) dim(`  响应: ${res.raw.slice(0, 300)}`);
      return 1;
    }
    for (const c of res.data ?? []) info(`  ${c.categoryId}  ${c.categoryName}`);
    return 0;
  }

  if (opts.tags !== null) {
    header(`掘金标签: ${opts.tags}`);
    const res = await juejin.queryTags(opts.tags, options, hooks);
    if (!res.ok) {
      error(`查询失败: ${res.errMsg}`);
      if (res.authExpired) error('  登录态已失效,请更新 Cookie。');
      if (res.raw) dim(`  响应: ${res.raw.slice(0, 300)}`);
      return 1;
    }
    if ((res.data ?? []).length === 0) info('  (没有匹配的标签)');
    for (const t of res.data ?? []) info(`  ${t.tagId}  ${t.tagName}`);
    return 0;
  }

  // ---- 发布流程 ----
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
 * 把**本地配置**与**掘金上已有的草稿**对齐(`--sync`,旧名 `--rename`)。
 *
 * 为什么需要它:标题和摘要都会在配置里被改,而草稿一旦建好就冻在那儿 ——
 * 建草稿时的正文哈希只覆盖「标题+正文」,摘要改了哈希不变,于是重跑 `--publish`
 * 会认为「草稿已是最新」而静默跳过,改了的摘要永远推不上去。`--sync` 就是补这条路。
 *
 * 它能处理的两种状态:
 *   · `drafted`  —— 就地改草稿,不发布(草稿仍是私有的)
 *   · `published`—— 改草稿 + 重新 publish。实测 publish 是**就地更新**:article_id
 *                   不变、链接不变、阅读量等数据保留,只是内容换了。
 *
 * ⚠️ `article_draft/update` 必须带全字段(只传 title 有把正文清空的风险),所以:
 *   · 标题、摘要 —— 取本地配置的当前值(这正是要同步的东西)
 *   · 正文 —— **只在源文件真的改过时**才换(`contentHash` 变了才动),否则原样传回
 *     远端读到的那份。不碰没打算碰的东西。
 *   · 分类/标签/封面 —— 优先用远端已有的值
 *
 * 安全阀:改完先**回读草稿**确认标题与摘要生效、且正文长度没变,才继续。
 * 正文被误改时就停住 —— 已公开文章的正文被删是不可逆的。
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
    error('没有匹配的文章(检查 --only / --from 的编号范围)');
    return 1;
  }

  // ---- 预检:全量报出不能同步的,一篇都不动 ----
  const problems: string[] = [];
  for (const a of selected) {
    const st = state.articles[a.no];
    if (!st) problems.push(`${a.no} 状态文件里没有记录`);
    else if (st.status === 'pending') problems.push(`${a.no} 还没建过草稿,先跑 --drafts-only`);
    else if (!st.draftId) problems.push(`${a.no} 没有记录草稿 id,无法同步`);
  }
  if (problems.length > 0) {
    error(`有 ${problems.length} 篇不能同步,本次一篇都不会动:`);
    problems.forEach((p) => error(`  · ${p}`));
    return 1;
  }

  const delayMs = opts.delayMs ?? cfg.delayMs;
  const interactive = isTTY();
  header(`同步草稿 ${cfg.sourceDir} → 掘金`);
  dim(
    `  标题来源: ${cfg.titleSource === 'fileName' ? '文件名(−.md)' : '原文 H1'} · ` +
      `待处理 ${selected.length} 篇 · ${opts.dryRun ? 'dry-run' : '对齐标题与摘要,已发布的重新发布'}`,
  );
  info('');

  // 读线上**文章记录**的标题。
  // 掘金把草稿标题同步到文章记录是**异步**的,而且不保证一次就生效(实测:同一批
  // 里有的几分钟就变,有的重发一次才变)。所以「本地记着改过了」不等于
  // 「线上已经是新标题」—— 只看本地状态会把没同步的当成已完成而永远跳过。
  // dry-run 不发任何请求,退化为只看本地记录。
  const liveTitles = new Map<string, string>();
  if (!opts.dryRun) {
    const liveRes = await juejin.listArticles(options, hooks);
    if (liveRes.ok) {
      for (const a of liveRes.data ?? []) liveTitles.set(a.articleId, a.title);
    } else {
      dim(`  ! 读线上文章列表失败(${liveRes.errMsg}),本次只按本地记录判断是否已改。`);
    }
  }

  let synced = 0;
  let skipped = 0;

  for (const article of selected) {
    const st = state.articles[article.no];
    // 读不到就当未知(例如文章数超过一页),此时按本地记录走,不能当成「不匹配」
    const live = st.articleId ? liveTitles.get(st.articleId) : undefined;

    const wantBrief = cfg.briefs[article.no]?.trim() ?? '';
    const titleSame = st.title === article.title;
    // 老状态文件没有 brief 字段 → undefined ≠ wantBrief → 会走一次全量刷新,正是想要的
    const briefSame = st.brief === wantBrief;
    const bodySame = st.contentHash === article.contentHash;

    // 三样都对齐、线上标题也对(或读不到)→ 完成态,不再碰(幂等)
    if (titleSame && briefSame && bodySame && (live === undefined || live === article.title)) {
      dim(`✓ ${article.no} 标题、摘要、正文都已是目标值,跳过`);
      skipped++;
      continue;
    }
    if (titleSame && live !== undefined && live !== article.title) {
      // 上次改完草稿也发过,但掘金的异步同步没生效 —— 草稿已是目标标题,
      // 下面自然会走「只补发布」那条路,不会重复改草稿
      dim(`  ! ${article.no} 线上标题仍是「${live}」,补发布一次`);
    }

    if (opts.dryRun) {
      const bits: string[] = [];
      if (!titleSame) bits.push(`标题「${st.title ?? '(未记录)'}」→「${article.title}」`);
      if (!briefSame) bits.push(st.brief === undefined ? '摘要(首次记录)' : '摘要');
      if (!bodySame) bits.push('正文');
      if (bits.length === 0) bits.push('仅补发布');
      info(`${article.no}  ${bits.join(' · ')}`);
      synced++;
      continue;
    }

    const draftId = st.draftId as string;
    const sp = interactive ? startSpinner(`${article.no} · 读草稿`) : null;
    const detail = await juejin.getDraft(draftId, options, hooks);
    if (!detail.ok || !detail.data) {
      sp?.stopError(`✖ ${article.no} 读取草稿失败`);
      error(`✖ ${article.no} 读取草稿失败: ${detail.errMsg}`);
      if (detail.authExpired) error('  登录态已失效,请更新 Cookie 后重跑。');
      if (detail.raw) dim(`  响应: ${detail.raw.slice(0, 300)}`);
      return 1;
    }
    const remote = detail.data;
    sp?.stopSuccess(`✔ ${article.no} ${remote.title || '(无标题)'}`);

    // 远端已是目标值,但状态里没记成功过 —— 说明上次改完草稿就中断了,
    // 不能当作「已完成」跳过,补一次发布即可(不必再改一遍草稿)
    const needUpdate = remote.title !== article.title || remote.briefContent.trim() !== wantBrief;

    if (needUpdate) {
      // 正文只在**源文件真的改过**时才换。远端读回来的 markdown 可能被掘金规范化过,
      // 拿它跟本地逐字比会永远「不一致」,于是每次都重写正文 —— 那是不该有的副作用。
      const contentChanged = st.contentHash !== article.contentHash;
      const sp2 = interactive ? startSpinner(`${article.no} · 改草稿`) : null;
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
        sp2?.stopError(`✖ ${article.no} 改草稿失败`);
        error(`✖ ${article.no} 改草稿失败: ${upd.errMsg}`);
        if (upd.authExpired) error('  登录态已失效,请更新 Cookie 后重跑。');
        if (upd.raw) dim(`  响应: ${upd.raw.slice(0, 300)}`);
        return 1;
      }
      sp2?.stopSuccess(`✔ ${article.no} 草稿已改`);

      // ---- 安全阀:回读确认标题与摘要生效、正文没被误伤 ----
      const after = await juejin.getDraft(draftId, options, hooks);
      if (after.ok && after.data) {
        if (after.data.title !== article.title) {
          error(`✖ ${article.no} 回读标题仍是「${after.data.title}」,改标题没生效,已停住没有重新发布。`);
          return 1;
        }
        if (after.data.briefContent.trim() !== wantBrief) {
          error(`✖ ${article.no} 回读摘要与目标值不一致,已停住没有重新发布(草稿 id ${draftId})。`);
          return 1;
        }
        if (after.data.markContent.length !== remote.markContent.length && !contentChanged) {
          error(
            `✖ ${article.no} 同步标题/摘要时顺手改动了正文(远端 ${remote.markContent.length} → ` +
              `${after.data.markContent.length} 字符),已停住没有重新发布。\n` +
              `  草稿 id ${draftId},线上文章没有受影响。`,
          );
          return 1;
        }
      } else {
        dim('  ! 回读草稿失败,跳过校验(草稿已按目标值提交)');
      }
    } else {
      dim(`  ${article.no} 草稿已是目标值,只需补一次发布`);
    }

    // 草稿这一侧已经对齐 —— 先把状态记上,后面的发布无论成败都不会重复改草稿
    st.title = article.title;
    st.brief = wantBrief;
    st.contentHash = article.contentHash;
    st.updatedAt = new Date().toISOString();
    if (!save()) return 1;

    // ---- 未发布的草稿:到此为止,不发布 ----
    if (st.status !== 'published') {
      dim(`  ✔ ${article.no} 草稿已同步(未发布): https://juejin.cn/editor/drafts/${draftId}`);
      info('');
      synced++;
      if (delayMs > 0 && article !== selected[selected.length - 1]) await delay(delayMs);
      continue;
    }

    // ---- 已发布:重新发布(就地更新,article_id 不变) ----
    const sp3 = interactive ? startSpinner(`${article.no} · 重新发布`) : null;
    const pub = await juejin.publishArticle(draftId, options, hooks);
    if (!pub.ok) {
      sp3?.stopError(`✖ ${article.no} 重新发布失败`);
      st.publishAttemptedAt = new Date().toISOString();
      st.updatedAt = st.publishAttemptedAt;
      save();
      error(`✖ ${article.no} 重新发布失败: ${pub.errMsg}`);
      if (pub.raw) dim(`  响应: ${pub.raw.slice(0, 300)}`);
      dim('  草稿已是新内容但线上还是旧的。确认后用 --sync 重跑,会跳过改草稿、只补发布。');
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

  header(opts.dryRun ? 'dry-run 结束(没有发送任何请求)' : '完成');
  if (opts.dryRun) {
    info(`  将要同步: ${synced} 篇`);
    dim('  dry-run 不联网,这里按本地记录判断;实际运行时还会读草稿逐篇比对。');
    return 0;
  }

  info(`  本次同步: ${synced} 篇 · 跳过(已是目标值): ${skipped} 篇`);

  // 刻意**不**在这里回读线上标题来判断成败:同步是异步的,刚发完几乎必然还没变过来,
  // 回读只会得到「全都没生效」的假警报。真正可靠的判断在下次运行的开头。
  if (synced > 0) {
    dim('  掘金把草稿标题同步到线上文章记录是异步的 —— 快的几分钟,慢的要再发一次才生效。');
    dim('  过几分钟再跑一次 --sync 核对:线上标题还没变的会被重新补发布(幂等),已生效的跳过。');
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

  // ---- 遗留草稿清单 ----
  if (opts.orphans) {
    const drafted = Object.entries(state.articles).filter(([, s]) => s.status === 'drafted');
    header('遗留草稿(--orphans)');
    if (drafted.length === 0) info('  没有待发布的草稿');
    for (const [no, s] of drafted) {
      info(`  ${no}  草稿 ${s.draftId}  https://juejin.cn/editor/drafts/${s.draftId}`);
    }
    return 0;
  }

  let selected = selectArticles(all, opts);
  if (selected.length === 0) {
    error(`没有匹配的文章(检查 --only / --from 的编号范围)`);
    return 1;
  }

  // ---- 摘要预检:在任何网络调用之前一次报出全部不合格项 ----
  const problems = checkBriefs(cfg, selected, opts.skipBriefCheck);
  if (problems.length > 0) {
    error(`摘要校验未通过(${problems.length} 项),一篇都不会发:`);
    problems.forEach((p) => error(`  · ${p}`));
    dim('  修好配置里的 briefs 后重跑。确实想跳过校验可加 --skip-brief-check。');
    return 1;
  }
  if (!opts.dryRun) {
    if (!cfg.categoryId) {
      error('配置里缺少 categoryId,请用 --categories 查到真实 id 后填进配置');
      return 1;
    }
    // 上限 3 是掘金服务端实测返回的("您最多可以为文章添加3个标签",err_no=4031)
    if (cfg.tagIds.length === 0 || cfg.tagIds.length > MAX_TAGS) {
      error(`tagIds 必须是 1~${MAX_TAGS} 个(当前 ${cfg.tagIds.length} 个),用 --tags <关键词> 查真实 id`);
      return 1;
    }
  }

  const interactive = isTTY();
  const delayMs = opts.delayMs ?? cfg.delayMs;

  const mode = opts.dryRun ? 'dry-run' : opts.draftsOnly ? '只建草稿(不发布)' : '实际发布';
  header(`${opts.draftsOnly ? '建草稿' : '发布'} ${cfg.sourceDir} → 掘金`);
  dim(`  待处理 ${selected.length} 篇 · 篇间隔 ${delayMs}ms · ${mode}`);
  if (!opts.dryRun && !opts.draftsOnly) {
    dim('  提示:一次性连发多篇可能触发掘金风控,被拦时可用 --delay 调大间隔后重跑。');
  }
  if (opts.draftsOnly) {
    dim('  草稿是私有的,不公开 —— 发不发、什么时候发,由你在掘金后台逐篇决定。');
  }
  info('');

  let gatePassed = opts.yes;
  /** --yes 路径下,封面读回最多尝试一次(失败了不必每篇都重试) */
  let coverChecked = false;
  let processed = 0;
  let drafted = 0;
  let planned = 0;

  for (const article of selected) {
    const st = ensureArticle(state, article.no, article.contentHash);

    // 已发布:跳过(--republish 显式指定才重发)
    if (st.status === 'published' && opts.republish !== article.no) {
      dim(`✓ ${article.no} 已发布,跳过  ${st.url ?? ''}`);
      continue;
    }

    // 上次发布结果未知:绝不自动重发
    if (st.status === 'drafted' && st.publishAttemptedAt && opts.republish !== article.no) {
      error(`✖ ${article.no} 上次发布结果未知(${st.publishAttemptedAt}),已停住等你确认。`);
      info('  请先到掘金创作中心确认这篇文章是否已发布:');
      info('   · 已发布 → 手工把状态文件里该篇 status 改成 published 并补上 url');
      info(`   · 未发布 → 用 --republish ${article.no} 重发(会复用已有草稿) `);
      return 1;
    }

    // 正文被改过:草稿已过期
    if (st.status === 'drafted' && st.contentHash !== article.contentHash) {
      if (!opts.force) {
        error(`✖ ${article.no} 的正文在建草稿后被改过,草稿已过期。`);
        info('  用 --force 重建草稿(旧草稿会变成孤儿,可用 --orphans 查看),或撤销对源文件的修改。');
        return 1;
      }
      dim(`  ! ${article.no} 正文已变更,按 --force 重建草稿`);
      st.status = 'pending';
      st.draftId = null;
      st.contentHash = article.contentHash;
    }

    if (opts.dryRun) {
      const brief = cfg.briefs[article.no]?.trim() ?? '';
      const { cp } = countBrief(brief);
      info(`${article.no}  ${article.title}`);
      dim(`     分类 ${cfg.categoryId} · 标签 ${cfg.tagIds.join(',')} · 封面 ${state.coverImage || '(空)'}`);
      dim(`     摘要(${cp} 字): ${brief}`);
      dim(`     正文 ${article.content.length} 字符,前 3 行:`);
      article.content.split('\n').slice(0, 3).forEach((l) => dim(`       | ${l}`));
      info('');
      planned++;
      continue;
    }

    // ---- 建草稿(先记后发) ----
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
      // 立刻落盘:即使后面发布失败或 Ctrl-C,草稿 id 也不会丢
      if (!save()) return 1;
      dim(`  草稿 id: ${draftId}`);
    } else {
      dim(`  ${article.no} 复用已有草稿 ${draftId}`);
    }

    // ---- 确认点 ----
    if (!gatePassed) {
      gatePassed = true;
      const remaining = selected.filter((x) => x.order > article.order).length;
      const gate = await runGate(state, draftId, article, remaining, options, hooks, save, opts.draftsOnly);
      if (gate === 'pause') return EXIT_PAUSED;
    } else if (!coverChecked && !state.coverImage) {
      // --yes 跳过了确认门,但封面读回不能跟着跳过(见 readBackCover 的注释)
      coverChecked = true;
      await readBackCover(state, draftId, options, hooks, save);
    }

    // ---- 发布前校验正文真的存进去了 ----
    if (!opts.skipBodyCheck) {
      const bodyProblem = await verifyDraftContent(article, draftId, options, hooks);
      if (bodyProblem) {
        error(`✖ ${bodyProblem}`);
        error('  已停住,没有发布。确认无误后可用 --skip-body-check 跳过本校验。');
        return 1;
      }
    }

    // ---- 只建草稿模式:到此为止,绝不调发布接口 ----
    // 状态停在 'drafted'(草稿已建待发布),正是这个模式该有的终态:
    // 下次不带 --drafts-only 重跑会复用同一份草稿直接发布,不会重复建。
    if (opts.draftsOnly) {
      st.title = article.title;
      st.updatedAt = new Date().toISOString();
      if (!save()) return 1;
      dim(`  ✔ ${article.no} 草稿已就绪(未发布): https://juejin.cn/editor/drafts/${draftId}`);
      info('');
      drafted++;
      if (delayMs > 0 && article !== selected[selected.length - 1]) await delay(delayMs);
      continue;
    }

    // ---- 发布 ----
    const outcome = await doPublish(state, article.no, draftId, options, hooks, interactive, save);
    if (outcome === 'ok') {
      // 记下这次发布的标题,`--rename` 靠它判断是否已改成目标标题
      st.title = article.title;
      if (!save()) return 1;
      if (st.needsCheck) {
        dim('  ! 发布成功但没拿到文章 id,请到创作中心确认');
      } else {
        info(`  ${st.url}`);
      }
      processed++;
    } else if (outcome === 'failed') {
      dim('  状态已保留,修好后可用相同命令重跑续发。');
      return 1;
    } else {
      dim('  因为「是否已发布不可知」,脚本不会自动重发。');
      dim('  确认未发布后用 --republish ' + article.no + ' 重发。');
      return 1;
    }
    info('');

    if (delayMs > 0 && article !== selected[selected.length - 1]) {
      await delay(delayMs);
    }
  }

  header(opts.dryRun ? 'dry-run 结束(没有发送任何请求)' : '完成');
  if (opts.dryRun) {
    info(`  校验通过、将要发布的: ${planned} 篇`);
  } else if (opts.draftsOnly) {
    info(`  本次新建草稿 ${drafted} 篇(均未发布)`);
    const total = Object.values(state.articles).filter((s) => s.status === 'drafted').length;
    info(`  状态文件累计待发布草稿 ${total} 篇`);
    info('');
    info('  要发布它们:到掘金创作中心的草稿箱逐篇点发布,或去掉 --drafts-only 重跑本命令。');
    info('  (草稿已存在,重跑会复用,不会重复建草稿)');
  } else {
    info(`  本次发布 ${processed} 篇`);
    const total = Object.values(state.articles).filter((s) => s.status === 'published').length;
    info(`  状态文件累计已发布 ${total} 篇`);
  }
  return 0;
}

// 入口在 src/publish/publish.ts —— 这里只导出 run(argv),不再自己执行。
// SIGINT 处理也统一放在那边,免得两个平台各注册一次。
