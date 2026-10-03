import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

/**
 * Juejin (unofficial) API client — the project's only network layer.
 * Endpoints are reverse-engineered from packet captures: no official docs, may change at any time.
 * Convention: functions never throw; failures are returned as JuejinResult and callers check `if (!res.ok)`.
 */

const BASE_URL = 'https://api.juejin.cn';

/** Markdown edit mode (rich text is 0); no TS enum — tsconfig has erasableSyntaxOnly */
const EDIT_TYPE_MARKDOWN = 10;

/** Failure categories: network / HTTP non-2xx / malformed body / Juejin business error */
export type JuejinErrorKind = 'network' | 'http' | 'body' | 'business';

/** Result of one Juejin API call */
export interface JuejinResult<T> {
  /** HTTP 2xx and err_no === 0; callers check this first */
  ok: boolean;
  /** Data when ok; null on failure */
  data: T | null;
  /** 0 success; -1 network/timeout; -2 HTTP non-2xx; -3 malformed body; >0 Juejin err_no */
  errNo: number;
  /** User-facing description */
  errMsg: string;
  /** HTTP status; null on network failure */
  httpStatus: number | null;
  /** null when ok */
  kind: JuejinErrorKind | null;
  /** 401/403, or the response is a login page */
  authExpired: boolean;
  /** Truncated raw body on failure (≤500 chars), for --debug */
  raw: string | null;
}

/** Juejin's response envelope (raw underscore fields) */
interface JuejinEnvelope<T> {
  err_no?: number;
  err_msg?: string;
  data?: T | null;
}

/** Call options */
export interface JuejinOptions {
  /** Normalized cookie string */
  cookie: string;
  /** Per-request timeout (ms); fetch has no default, so one hung request would stall the whole batch */
  timeoutMs: number;
}

/** Call hooks */
export interface JuejinHooks {
  /** Called before each backoff retry */
  onRetry?: (info: { attempt: number; errMsg: string; waitMs: number }) => void;
  /** Request/response summaries under --debug */
  onDebug?: (line: string) => void;
}

/** Input for creating a draft */
export interface DraftInput {
  title: string;
  /** Brief; Juejin requires 50~100 chars */
  briefContent: string;
  /** Markdown body (title excluded) */
  markContent: string;
  categoryId: string;
  tagIds: string[];
  /** Cover image URL; empty string means no cover */
  coverImage: string;
}

/** Both field names have been observed; normalizeId handles both */
interface DraftCreated {
  id?: string | number;
  draft_id?: string | number;
}

interface ArticlePublished {
  article_id?: string | number;
}

/** One draft from the list endpoint (other fields kept as-is in raw) */
export interface DraftBrief {
  draftId: string;
  title: string;
  coverImage: string;
  updatedAt: string;
}

// ============ Cookie ============

/**
 * Normalize a pasted cookie; tolerates a `Cookie:` prefix, quotes, newlines, or a bare sessionid.
 * Returns null if it cannot be recognized.
 */
export function normalizeCookie(raw: string): string | null {
  let s = raw.trim();
  if (!s) return null;

  // Strip a "Cookie:" header-name prefix
  s = s.replace(/^cookie\s*:\s*/i, '');
  // Strip quotes wrapping the whole string
  s = s.replace(/^["'`]|["'`]$/g, '');
  // Collapse newlines/extra whitespace into "; "
  s = s.split(/[\r\n]+/).map((line) => line.trim()).filter(Boolean).join('; ');
  s = s.replace(/\s*;\s*/g, '; ').replace(/;\s*$/, '');

  if (!s) return null;
  // A bare value with no "=" becomes sessionid=<value>
  if (!s.includes('=')) s = `sessionid=${s}`;
  // Must contain sessionid, otherwise Juejin will not recognize it
  if (!/(^|;\s*)sessionid=/.test(s)) return null;
  return s;
}

/**
 * Read the cookie from JUEJIN_COOKIE, else the gitignored .juejin-cookie file in the repo root.
 * Returns the normalized string, or a reason the caller prints.
 */
export function loadCookie(repoRoot: string): { cookie: string } | { error: string } {
  const fromEnv = process.env.JUEJIN_COOKIE?.trim();
  if (fromEnv) {
    const cookie = normalizeCookie(fromEnv);
    return cookie ? { cookie } : { error: 'Could not find sessionid in the JUEJIN_COOKIE environment variable' };
  }

  const file = path.join(repoRoot, '.juejin-cookie');
  if (!existsSync(file)) {
    return {
      error:
        `Juejin cookie not found. Choose one of the following:\n` +
        `  · Set the environment variable JUEJIN_COOKIE="sessionid=..."\n` +
        `  · Or write the sessionid into ${file}`,
    };
  }
  const cookie = normalizeCookie(readFileSync(file, 'utf8'));
  return cookie ? { cookie } : { error: `Could not find sessionid in ${file}` };
}

// ============ Request ============

/** query_category_briefs accepts only GET; POST returns "request route does not exist" */
type HttpMethod = 'GET' | 'POST';

/** Juejin is sensitive to UA / referer / origin */
function buildHeaders(cookie: string, method: HttpMethod): Record<string, string> {
  const headers: Record<string, string> = {
    accept: 'application/json, text/plain, */*',
    'user-agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    origin: 'https://juejin.cn',
    referer: 'https://juejin.cn/',
  };
  // Read-only commands need no auth; do not send an empty Cookie header
  if (cookie) headers.cookie = cookie;
  // GET has no body, so no content-type (this endpoint only works that way)
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

/** Only network failures and 408/429/5xx are worth retrying */
export function isTransient(res: JuejinResult<unknown>): boolean {
  if (res.ok) return false;
  if (res.kind === 'network') return true;
  if (res.kind === 'http' && res.httpStatus !== null) {
    return res.httpStatus === 408 || res.httpStatus === 429 || res.httpStatus >= 500;
  }
  return false;
}

/**
 * Business error looks like an expired session. The API words these in Chinese; those branches are
 * \u-escaped purely to keep this source file ASCII (they match "login", "not logged in",
 * "invalid", "expired" in the API's own wording). Do not drop them.
 */
function looksLikeAuthError(errMsg: string): boolean {
  return /\u767b\u5f55|\u672a\u767b\u5f55|login|token|\u5931\u6548|\u8fc7\u671f/i.test(errMsg);
}

/** Single request; never throws */
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
    // When rate-limited or logged out, Juejin returns an HTML page instead of JSON
    if (!(res.headers.get('content-type') ?? '').includes('json')) {
      // Chinese branch = the API's word for "login", \u-escaped to keep this file ASCII
      const authExpired = /\u767b\u5f55|login/i.test(text);
      return fail<T>(-3, 'body', authExpired ? 'Cookie has expired (the API returned a login page)' : 'The API did not return JSON', {
        httpStatus,
        raw: text.slice(0, 500),
        authExpired,
      });
    }

    let env: JuejinEnvelope<T>;
    try {
      env = JSON.parse(text) as JuejinEnvelope<T>;
    } catch {
      return fail<T>(-3, 'body', 'Failed to parse the response body JSON', {
        httpStatus,
        raw: text.slice(0, 500),
        authExpired: false,
      });
    }

    const errNo = env.err_no ?? -3;
    if (errNo === 0) {
      if (env.data === undefined || env.data === null) {
        return fail<T>(-3, 'body', 'err_no is 0 but data is missing', {
          httpStatus,
          raw: text.slice(0, 500),
          authExpired: false,
        });
      }
      return { ok: true, data: env.data, errNo: 0, errMsg: '', httpStatus, kind: null, authExpired: false, raw: null };
    }

    const errMsg = env.err_msg?.trim() || `Juejin returned err_no=${errNo}`;
    return fail<T>(errNo, 'business', errMsg, {
      httpStatus,
      raw: text.slice(0, 500),
      authExpired: looksLikeAuthError(errMsg),
    });
  } catch (err) {
    const e = err as Error;
    const msg =
      e.name === 'TimeoutError'
        ? `Request timed out (${opts.timeoutMs}ms)`
        : e.name === 'AbortError'
          ? 'Request aborted'
          : `Network error: ${e.message}`;
    return fail<T>(-1, 'network', msg, { httpStatus, raw: null, authExpired: false });
  }
}

/**
 * Backoff-retry wrapper; retries is per endpoint — a timeout is safely retryable for a draft
 * but for publish may create a second public article.
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

/** Normalize string | number into a string id */
function normalizeId(v: string | number | undefined): string | null {
  if (v === undefined || v === null) return null;
  const s = String(v).trim();
  return s ? s : null;
}

// ============ Endpoints ============

/** Create a draft; retry 3 — drafts are not public, so a timeout at worst leaves one extra draft */
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
        return fail<{ draftId: string | null }>(-3, 'body', 'Draft created successfully but the response had no draft id', {
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
 * Publish a draft; retries = 0 — on a POST timeout, whether it published is unknowable,
 * so a retry could create a second public article; the ambiguity is left to a human.
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

export interface JuejinCategory {
  categoryId: string;
  categoryName: string;
}

/**
 * Query all categories. ⚠️ GET only — POST on the same path returns err_no=2 "request route does not exist";
 * GET returns the 8 categories. Needs no auth (works with no cookie).
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

export interface JuejinTag {
  tagId: string;
  tagName: string;
}

/** { tag_id, tag: { tag_id, tag_name } } — the name is nested, not top-level */
interface TagListEntry {
  tag_id?: string | number;
  tag_name?: string;
  tag?: { tag_id?: string | number; tag_name?: string };
}

/**
 * Look up tags by keyword; no auth needed.
 * ⚠️ `tag_name` lives in the nested `tag` object (top level has only `tag_id`); both are read in case the API changes.
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
 * List the account's drafts; used by `--orphans`.
 * ⚠️ Do not read body/cover here — this endpoint blanks `mark_content`/`html_content` (bandwidth);
 * use `getDraft` for real values.
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
      // data may be an array directly or wrapped as {drafts: [...]} / {list: [...]}
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

/** A published article row (id and title only) */
export interface ArticleBrief {
  articleId: string;
  title: string;
}

/**
 * List published articles.
 * ⚠️ `data` is the array directly (not `data.data`); the title is nested in `article_info.title`.
 * Title sync from draft to article record is async (sometimes needs a re-send), so `--rename`
 * must read live records, not local state; when the target id is absent, treat it as unknown, not mismatched.
 * One page of 50 suffices (accounts have far fewer).
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

/** Draft detail; the only place with real body/cover values (see getDraft) */
export interface DraftDetail {
  draftId: string;
  title: string;
  coverImage: string;
  markContent: string;
  briefContent: string;
  editType: number;
}

/** Detail data lives in data.article_draft, not data top-level */
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
 * Read one draft's detail.
 * ⚠️ Data is in `data.article_draft`, not `data` — wrong level looks like an empty draft.
 * ⚠️ Validate body/cover here, never via list_by_user, which blanks `mark_content`/`html_content`.
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
      return fail<DraftDetail>(-3, 'body', 'The draft detail has no article_draft field', {
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

/** Input for updating a draft */
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
 * Update a draft (title/body/brief/category/tags/cover).
 * ⚠️ Key is `id`, not `draft_id` — the latter gives err_no=2 "parameter error" with no field named.
 * `publish` updates the same article in place (article_id unchanged); editing a published article = update draft + publish.
 * ⚠️ Pass every field — sending only title can wipe body/brief.
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
