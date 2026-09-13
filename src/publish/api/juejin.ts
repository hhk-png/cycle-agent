import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

/**
 * 掘金(非官方)接口客户端 —— 全项目唯一的网络层。
 *
 * 这些接口靠抓包得来,没有官方文档,随时可能变。所有请求都集中在这里,
 * 便于实测调整请求头与端点。
 *
 * 约定:所有函数**永不抛异常**,失败一律用 JuejinResult 表达(对标 claude.ts 的 ClaudeResult),
 * 调用侧统一写 `if (!res.ok) { ... }`。
 */

const BASE_URL = 'https://api.juejin.cn';

/** Markdown 编辑模式(富文本为 0);不要用 TS enum,tsconfig 开了 erasableSyntaxOnly */
const EDIT_TYPE_MARKDOWN = 10;

/** 失败分类:网络层 / HTTP 非 2xx / 响应体异常 / 掘金业务错误 */
export type JuejinErrorKind = 'network' | 'http' | 'body' | 'business';

/** 一次掘金接口调用的结果 */
export interface JuejinResult<T> {
  /** HTTP 2xx 且 err_no === 0;调用侧先判它(对标 ClaudeResult 的 exitCode === 0) */
  ok: boolean;
  /** ok 时的数据;失败为 null */
  data: T | null;
  /** 0 成功;-1 网络/超时;-2 HTTP 非 2xx;-3 响应体异常;>0 掘金 err_no 原值 */
  errNo: number;
  /** 可直接展示给用户的描述 */
  errMsg: string;
  /** HTTP 状态码;网络失败为 null */
  httpStatus: number | null;
  /** ok 时为 null */
  kind: JuejinErrorKind | null;
  /** 登录态失效:401/403,或响应是登录页/提示未登录 */
  authExpired: boolean;
  /** 失败时截断的原始响应体(≤500 字),供 --debug 排查 */
  raw: string | null;
}

/** 掘金统一响应信封(线上的原始下划线字段) */
interface JuejinEnvelope<T> {
  err_no?: number;
  err_msg?: string;
  data?: T | null;
}

/** 调用选项 */
export interface JuejinOptions {
  /** 归一化后的 Cookie 串 */
  cookie: string;
  /** 单次请求超时(毫秒)。fetch 无默认超时,必须显式设置,否则一个挂住的请求会卡死整批 */
  timeoutMs: number;
}

/** 调用钩子 */
export interface JuejinHooks {
  /** 每次退避重试前回调 */
  onRetry?: (info: { attempt: number; errMsg: string; waitMs: number }) => void;
  /** --debug 时打印请求/响应摘要 */
  onDebug?: (line: string) => void;
}

/** 建草稿的入参 */
export interface DraftInput {
  title: string;
  /** 摘要,掘金要求 50~100 字 */
  briefContent: string;
  /** Markdown 正文(不含标题) */
  markContent: string;
  categoryId: string;
  tagIds: string[];
  /** 封面图 URL;空串表示不设封面 */
  coverImage: string;
}

/** 建草稿的响应(两种字段名都出现过,用 normalizeId 兼容) */
interface DraftCreated {
  id?: string | number;
  draft_id?: string | number;
}

/** 发布文章的响应 */
interface ArticlePublished {
  article_id?: string | number;
}

/** 掘金草稿列表里的单条草稿(只列我们关心的字段,其余原样保留在 raw) */
export interface DraftBrief {
  draftId: string;
  title: string;
  coverImage: string;
  updatedAt: string;
}

// ============ Cookie ============

/**
 * 归一化用户粘贴的 Cookie。
 * 容错:可能粘成 `Cookie: sessionid=xxx`、带引号、带换行、或只粘了裸 sessionid 值。
 * 返回 null 表示无法识别。
 */
export function normalizeCookie(raw: string): string | null {
  let s = raw.trim();
  if (!s) return null;

  // 去掉可能带上的 "Cookie:" 头名前缀
  s = s.replace(/^cookie\s*:\s*/i, '');
  // 去掉整体包裹的引号
  s = s.replace(/^["'`]|["'`]$/g, '');
  // 换行/多余空白折成 "; "
  s = s.split(/[\r\n]+/).map((line) => line.trim()).filter(Boolean).join('; ');
  s = s.replace(/\s*;\s*/g, '; ').replace(/;\s*$/, '');

  if (!s) return null;
  // 整串不含 "=" 说明只粘了裸值,补成 sessionid=<值>
  if (!s.includes('=')) s = `sessionid=${s}`;
  // 最终必须含有 sessionid,否则掘金认不出来
  if (!/(^|;\s*)sessionid=/.test(s)) return null;
  return s;
}

/**
 * 读取 Cookie:优先环境变量 JUEJIN_COOKIE,其次仓库根目录下 gitignored 的 .juejin-cookie 文件。
 * 返回归一化后的串,或 null(附带说明原因由调用侧打印)。
 */
export function loadCookie(repoRoot: string): { cookie: string } | { error: string } {
  const fromEnv = process.env.JUEJIN_COOKIE?.trim();
  if (fromEnv) {
    const cookie = normalizeCookie(fromEnv);
    return cookie ? { cookie } : { error: '环境变量 JUEJIN_COOKIE 里找不到 sessionid' };
  }

  const file = path.join(repoRoot, '.juejin-cookie');
  if (!existsSync(file)) {
    return {
      error:
        `没有找到掘金 Cookie。请二选一:\n` +
        `  · 设置环境变量 JUEJIN_COOKIE="sessionid=..."\n` +
        `  · 或把 sessionid 写进 ${file}`,
    };
  }
  const cookie = normalizeCookie(readFileSync(file, 'utf8'));
  return cookie ? { cookie } : { error: `${file} 里找不到 sessionid` };
}

// ============ 请求 ============

/** HTTP 方法。实测 query_category_briefs 只认 GET,POST 会返回「请求路由不存在」 */
type HttpMethod = 'GET' | 'POST';

/** 统一请求头;掘金对 UA / referer / origin 敏感,社区脚本常栽在这里 */
function buildHeaders(cookie: string, method: HttpMethod): Record<string, string> {
  const headers: Record<string, string> = {
    accept: 'application/json, text/plain, */*',
    'user-agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    origin: 'https://juejin.cn',
    referer: 'https://juejin.cn/',
  };
  // 只读命令(--categories/--tags)无需登录态,此时不发空 Cookie 头
  if (cookie) headers.cookie = cookie;
  // GET 没有 body,不带 content-type(实测该端点这样才通)
  if (method === 'POST') headers['content-type'] = 'application/json';
  return headers;
}

function fail<T>(
  errNo: number,
  kind: JuejinErrorKind,
  errMsg: string,
  extra: { httpStatus: number | null; raw: string | null; authExpired: boolean },
): JuejinResult<T> {
  return {
    ok: false,
    data: null,
    errNo,
    errMsg,
    kind,
    httpStatus: extra.httpStatus,
    raw: extra.raw,
    authExpired: extra.authExpired,
  };
}

/** 仅网络层失败、408/429/5xx 值得自动重试 */
export function isTransient(res: JuejinResult<unknown>): boolean {
  if (res.ok) return false;
  if (res.kind === 'network') return true;
  if (res.kind === 'http' && res.httpStatus !== null) {
    return res.httpStatus === 408 || res.httpStatus === 429 || res.httpStatus >= 500;
  }
  return false;
}

/** 业务错误码/文案看起来像登录态失效 */
function looksLikeAuthError(errMsg: string): boolean {
  return /登录|未登录|login|token|失效|过期/i.test(errMsg);
}

/** 单次请求,永不抛异常 */
async function juejinOnce<T>(
  apiPath: string,
  body: unknown,
  opts: JuejinOptions,
  hooks: JuejinHooks,
  method: HttpMethod = 'POST',
): Promise<JuejinResult<T>> {
  let httpStatus: number | null = null;
  try {
    const res = await fetch(`${BASE_URL}${apiPath}`, {
      method,
      headers: buildHeaders(opts.cookie, method),
      body: method === 'POST' ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(opts.timeoutMs),
    });
    httpStatus = res.status;
    const text = await res.text();
    hooks.onDebug?.(`${apiPath} → HTTP ${res.status} ${text.slice(0, 300)}`);

    if (!res.ok) {
      return fail<T>(-2, 'http', `HTTP ${res.status} ${res.statusText}`, {
        httpStatus,
        raw: text.slice(0, 500),
        authExpired: res.status === 401 || res.status === 403,
      });
    }
    // 被风控或未登录时,掘金常直接返回 HTML 页面而不是 JSON
    if (!(res.headers.get('content-type') ?? '').includes('json')) {
      const authExpired = /登录|login/i.test(text);
      return fail<T>(-3, 'body', authExpired ? 'Cookie 已失效(接口返回登录页)' : '接口返回的不是 JSON', {
        httpStatus,
        raw: text.slice(0, 500),
        authExpired,
      });
    }

    let env: JuejinEnvelope<T>;
    try {
      env = JSON.parse(text) as JuejinEnvelope<T>;
    } catch {
      return fail<T>(-3, 'body', '响应体 JSON 解析失败', {
        httpStatus,
        raw: text.slice(0, 500),
        authExpired: false,
      });
    }

    const errNo = env.err_no ?? -3;
    if (errNo === 0) {
      if (env.data === undefined || env.data === null) {
        return fail<T>(-3, 'body', 'err_no 为 0 但缺少 data', {
          httpStatus,
          raw: text.slice(0, 500),
          authExpired: false,
        });
      }
      return { ok: true, data: env.data, errNo: 0, errMsg: '', httpStatus, kind: null, authExpired: false, raw: null };
    }

    const errMsg = env.err_msg?.trim() || `掘金返回 err_no=${errNo}`;
    return fail<T>(errNo, 'business', errMsg, {
      httpStatus,
      raw: text.slice(0, 500),
      authExpired: looksLikeAuthError(errMsg),
    });
  } catch (err) {
    const e = err as Error;
    const msg =
      e.name === 'TimeoutError'
        ? `请求超时(${opts.timeoutMs}ms)`
        : e.name === 'AbortError'
          ? '请求被中断'
          : `网络错误: ${e.message}`;
    return fail<T>(-1, 'network', msg, { httpStatus, raw: null, authExpired: false });
  }
}

/**
 * 带退避重试的外层。retries 由各端点自己定 ——
 * 因为「超时」对建草稿(可安全重试)和对发布(重试可能产生第二篇公开文章)意味着完全不同的处理。
 */
async function juejinCall<T>(
  apiPath: string,
  body: unknown,
  opts: JuejinOptions,
  retries: number,
  hooks: JuejinHooks,
  method: HttpMethod = 'POST',
): Promise<JuejinResult<T>> {
  let res: JuejinResult<T> | null = null;
  for (let attempt = 1; attempt <= retries + 1; attempt++) {
    res = await juejinOnce<T>(apiPath, body, opts, hooks, method);
    if (res.ok || !isTransient(res) || attempt > retries) return res;
    const waitMs = Math.min(8000, 1000 * 2 ** (attempt - 1)) + Math.floor(Math.random() * 250);
    hooks.onRetry?.({ attempt, errMsg: res.errMsg, waitMs });
    await delay(waitMs);
  }
  return res as JuejinResult<T>;
}

/** 把 string | number 一律归一成字符串 id */
function normalizeId(v: string | number | undefined): string | null {
  if (v === undefined || v === null) return null;
  const s = String(v).trim();
  return s ? s : null;
}

// ============ 端点 ============

/**
 * 建草稿。失败最多重试 3 次 —— 草稿不公开,超时重试最坏只多一个草稿(可恢复)。
 */
export function createDraft(
  input: DraftInput,
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<{ draftId: string | null }>> {
  const body = {
    category_id: input.categoryId,
    tag_ids: input.tagIds,
    title: input.title,
    brief_content: input.briefContent,
    edit_type: EDIT_TYPE_MARKDOWN,
    mark_content: input.markContent,
    cover_image: input.coverImage,
    link_url: '',
    theme_ids: [],
  };
  return juejinCall<DraftCreated>('/content_api/v1/article_draft/create', body, opts, 3, hooks).then(
    (res) => {
      if (!res.ok) {
        return res as unknown as JuejinResult<{ draftId: string | null }>;
      }
      const draftId = normalizeId(res.data?.id ?? res.data?.draft_id);
      if (!draftId) {
        return fail<{ draftId: string | null }>(-3, 'body', '建草稿成功但响应里没有草稿 id', {
          httpStatus: res.httpStatus,
          raw: res.raw,
          authExpired: false,
        });
      }
      return { ...res, data: { draftId } };
    },
  );
}

/**
 * 发布草稿。**retries = 0** —— POST 超时/网络中断时「是否已发布」不可知,
 * 自动重试可能产生第二篇公开文章。这个歧义必须交给人判(见 publish.ts 的 publishAttemptedAt)。
 */
export function publishArticle(
  draftId: string,
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<{ articleId: string | null }>> {
  const body = { draft_id: draftId, sync_to_org: false, column_ids: [], theme_ids: [] };
  return juejinCall<ArticlePublished>('/content_api/v1/article/publish', body, opts, 0, hooks).then(
    (res) => {
      if (!res.ok) {
        return res as unknown as JuejinResult<{ articleId: string | null }>;
      }
      return { ...res, data: { articleId: normalizeId(res.data?.article_id) } };
    },
  );
}

/** 单个分类 */
export interface JuejinCategory {
  categoryId: string;
  categoryName: string;
}

/**
 * 查询掘金全部分类。
 *
 * ⚠️ **必须用 GET** —— 实测 POST 同一路径返回 `err_no=2「请求路由不存在」`,
 * GET 才返回 8 个分类。这是本文件里最容易写错的一处。
 * 该端点不需要登录态,未带 Cookie 也能拿到数据。
 */
export function queryCategories(
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<JuejinCategory[]>> {
  return juejinCall<Array<{ category_id?: string | number; category_name?: string }>>(
    '/tag_api/v1/query_category_briefs',
    {},
    opts,
    1,
    hooks,
    'GET',
  ).then((res) => {
    if (!res.ok) return res as unknown as JuejinResult<JuejinCategory[]>;
    const list = (res.data ?? [])
      .map((c) => ({
        categoryId: normalizeId(c.category_id) ?? '',
        categoryName: (c.category_name ?? '').trim(),
      }))
      .filter((c) => c.categoryId && c.categoryName);
    return { ...res, data: list };
  });
}

/** 单个标签 */
export interface JuejinTag {
  tagId: string;
  tagName: string;
}

/** 实测返回项:{ tag_id, tag: { tag_id, tag_name, ... } } —— 名字在嵌套的 tag 里,不在顶层 */
interface TagListEntry {
  tag_id?: string | number;
  tag_name?: string;
  tag?: { tag_id?: string | number; tag_name?: string };
}

/**
 * 按关键词查标签。该端点不需要登录态。
 *
 * ⚠️ `tag_name` 在嵌套的 `tag` 对象里(顶层只有 `tag_id`),两层都读一遍以防接口改版。
 */
export function queryTags(
  keyword: string,
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<JuejinTag[]>> {
  const body = { key_word: keyword, cursor: '0', limit: 20 };
  return juejinCall<TagListEntry[]>(
    '/tag_api/v1/query_tag_list',
    body,
    opts,
    1,
    hooks,
  ).then((res) => {
    if (!res.ok) return res as unknown as JuejinResult<JuejinTag[]>;
    const list = (res.data ?? [])
      .map((t) => ({
        tagId: normalizeId(t.tag?.tag_id ?? t.tag_id) ?? '',
        tagName: (t.tag?.tag_name ?? t.tag_name ?? '').trim(),
      }))
      .filter((t) => t.tagId && t.tagName);
    return { ...res, data: list };
  });
}

/**
 * 列出当前账号的草稿(实测 `data` 是数组)。用于 `--orphans` 列遗留草稿。
 *
 * ⚠️ **不要用它读正文或封面** —— 该端点会把 `mark_content` / `html_content`
 * 返回成空字符串(省带宽),据此判断会得到「草稿没内容」的假警报。
 * 要读真实值请用 `getDraft`。
 */
export function listDrafts(
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<DraftBrief[]>> {
  const body = { page_size: 20, offset: 0 };
  return juejinCall<unknown>('/content_api/v1/article_draft/list_by_user', body, opts, 1, hooks).then(
    (res) => {
      if (!res.ok) return res as unknown as JuejinResult<DraftBrief[]>;
      const raw = res.data;
      // 兼容 data 直接是数组、或 {drafts: [...]} / {list: [...]} 两种包裹
      const arr: unknown[] = Array.isArray(raw)
        ? raw
        : ((raw as { drafts?: unknown[]; list?: unknown[] } | null)?.drafts ??
           (raw as { list?: unknown[] } | null)?.list ??
           []);
      const list = arr
        .map((item) => {
          const d = item as Record<string, unknown>;
          return {
            draftId: normalizeId(d.draft_id as string | number) ?? '',
            title: String(d.title ?? '').trim(),
            coverImage: String(d.cover_image ?? '').trim(),
            updatedAt: String(d.ctime ?? d.update_time ?? d.updated_at ?? ''),
          };
        })
        .filter((d) => d.draftId);
      return { ...res, data: list };
    },
  );
}

/** 已发布文章的一行(只有 id 和标题) */
export interface ArticleBrief {
  articleId: string;
  title: string;
}

/**
 * 列出当前账号**已发布文章**的 id 与标题。
 *
 * ⚠️ 与草稿列表不同,这里的 `data` **直接是数组**(不是 `data.data`)——
 * 写成 `res.data.data` 会拿到 undefined,看起来像「一篇都没有」。
 *
 * ⚠️ 标题在**嵌套的 `article_info.title`** 里,顶层没有 `title` 字段;
 * 顶层只有 `article_id`、`article_info`、`author_user_info` 等。
 *
 * 用途:`--rename` 用它读**线上文章记录**的标题。不能只看本地状态 ——
 * 掘金把草稿的标题同步到文章记录是异步的,有时要重发一次才生效,
 * 光信本地状态会把「草稿改了但线上没变」当成已完成而跳过。
 *
 * 单页 50 条足够(整个账号的文章数远小于 50);超出时返回的列表不含目标 id,
 * 调用方应退化为「未知」而不是「不匹配」。
 */
export function listArticles(
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<ArticleBrief[]>> {
  const body = { page_size: 50, offset: 0 };
  return juejinCall<unknown>('/content_api/v1/article/list_by_user', body, opts, 1, hooks).then(
    (res) => {
      if (!res.ok) return res as unknown as JuejinResult<ArticleBrief[]>;
      const raw = res.data;
      const arr: unknown[] = Array.isArray(raw)
        ? raw
        : ((raw as { data?: unknown[] } | null)?.data ?? []);
      const list = arr
        .map((item) => {
          const a = item as Record<string, unknown>;
          const info = (a.article_info ?? {}) as Record<string, unknown>;
          return {
            articleId: normalizeId(a.article_id as string | number) ?? '',
            title: String(info.title ?? '').trim(),
          };
        })
        .filter((a) => a.articleId);
      return { ...res, data: list };
    },
  );
}

/** 草稿详情(只有这里能读到正文与封面的真实值,见 getDraft 的注释) */
export interface DraftDetail {
  draftId: string;
  title: string;
  coverImage: string;
  markContent: string;
  briefContent: string;
  editType: number;
}

/** 详情响应:数据在 data.article_draft 里,不在 data 顶层 */
interface DraftDetailEnvelope {
  draft_id?: string;
  article_draft?: {
    id?: string | number;
    title?: string;
    cover_image?: string;
    mark_content?: string;
    brief_content?: string;
    edit_type?: number;
  };
}

/**
 * 读单篇草稿详情。
 *
 * ⚠️ 两个实测要点,都是踩过的坑:
 *
 * 1. **数据在 `data.article_draft`,不在 `data` 顶层** —— 读错层级会拿到一堆
 *    undefined,看起来就像「草稿是空的」。
 * 2. **列表接口 `list_by_user` 会把 `mark_content` / `html_content` 抹成空字符串**
 *    (省带宽)。所以**校验正文、读封面都必须走这里** —— 用列表接口判断正文是否
 *    送达会得到「空正文」的假警报。
 */
export function getDraft(
  draftId: string,
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<DraftDetail>> {
  return juejinCall<DraftDetailEnvelope>(
    '/content_api/v1/article_draft/detail',
    { draft_id: draftId },
    opts,
    1,
    hooks,
  ).then((res) => {
    if (!res.ok) return res as unknown as JuejinResult<DraftDetail>;
    const d = res.data?.article_draft;
    if (!d) {
      return fail<DraftDetail>(-3, 'body', '草稿详情里没有 article_draft 字段', {
        httpStatus: res.httpStatus,
        raw: res.raw,
        authExpired: false,
      });
    }
    return {
      ...res,
      data: {
        draftId: normalizeId(d.id) ?? draftId,
        title: (d.title ?? '').trim(),
        coverImage: (d.cover_image ?? '').trim(),
        markContent: d.mark_content ?? '',
        briefContent: d.brief_content ?? '',
        editType: d.edit_type ?? 0,
      },
    };
  });
}

/** 改草稿的入参 */
export interface DraftUpdateInput {
  draftId: string;
  title: string;
  briefContent: string;
  markContent: string;
  categoryId: string;
  tagIds: string[];
  coverImage: string;
}

/**
 * 改一篇草稿(标题/正文/摘要/分类/标签/封面)。
 *
 * ⚠️ **键名是 `id`,不是 `draft_id`** —— 用 `draft_id` 会得到 `err_no=2「参数错误」`,
 * 而错误信息完全不提是哪个字段的问题。这是实测踩出来的。
 *
 * 用途:改**已发布**文章的标题。掘金的模型里已发布文章仍挂着一份草稿
 * (detail 里的 `article_id` 非 0),所以「编辑已发布文章」= 改草稿 + 重新 publish。
 * 实测 `publish` 会**就地更新**同一篇(article_id 不变),不会新建。
 *
 * ⚠️ 调用方务必把**所有字段都带上** —— 只传 title 有可能把正文/摘要清空。
 * 这也是本函数要求完整入参、而不提供「只改标题」便捷签名的原因。
 */
export function updateDraft(
  input: DraftUpdateInput,
  opts: JuejinOptions,
  hooks: JuejinHooks = {},
): Promise<JuejinResult<{ draftId: string | null }>> {
  const body = {
    id: input.draftId,
    title: input.title,
    brief_content: input.briefContent,
    mark_content: input.markContent,
    category_id: input.categoryId,
    tag_ids: input.tagIds,
    cover_image: input.coverImage,
    edit_type: EDIT_TYPE_MARKDOWN,
    link_url: '',
    theme_ids: [],
  };
  return juejinCall<DraftCreated>('/content_api/v1/article_draft/update', body, opts, 2, hooks).then(
    (res) => {
      if (!res.ok) return res as unknown as JuejinResult<{ draftId: string | null }>;
      return { ...res, data: { draftId: normalizeId(res.data?.id) ?? input.draftId } };
    },
  );
}
