import { readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * The only network layer for the WeChat Official Accounts API.
 *
 * Same skeleton as juejin.ts (never-throwing Result, per-endpoint retries, AbortSignal.timeout, onDebug)
 * but deliberately not reused — WeChat errors are HTTP 200 + `errcode`/`errmsg`, unlike Juejin's `err_no`/`err_msg`.
 *
 * ⚠️ Personal subscription accounts: `message/mass/sendall` and `freepublish/submit` are verified-only (return `48001`),
 * so the final publish must be clicked in the backend; `draft/add` does work for personal accounts — why this module exists.
 * ⚠️ `access_token` needs the caller's egress IP in the backend whitelist, else `40164`; home IPs change.
 */

const BASE = 'https://api.weixin.qq.com';

export interface WechatOptions {
  appId: string;
  appSecret: string;
  timeoutMs: number;
}

export interface WechatHooks {
  onDebug?: (msg: string) => void;
}

/** Error categories drive actionable hints instead of a vague "it failed" */
export type WechatErrorKind =
  /** Network layer: timeout, DNS, connection drop — retry may help */
  | 'network'
  /** 40164: egress IP not whitelisted — must whitelist in the backend */
  | 'ip-whitelist'
  /** 48001 etc.: account lacks permission (a personal account cannot publish) */
  | 'permission'
  /** 40001/42001: access_token expired */
  | 'token'
  /** Other API errors */
  | 'api';

export type WechatResult<T> =
  | { ok: true; data: T }
  | { ok: false; errMsg: string; kind: WechatErrorKind; errCode: number };

/** WeChat error envelope: HTTP 200, error in the body */
interface Envelope {
  errcode?: number;
  errmsg?: string;
}

function classify(errcode: number): WechatErrorKind {
  if (errcode === 40164) return 'ip-whitelist';
  if (errcode === 48001) return 'permission';
  if (errcode === 40001 || errcode === 42001 || errcode === 40014) return 'token';
  return 'api';
}

/**
 * Turn an error into "what to do next"; the 40164 message carries the egress IP, extracted for direct display.
 */
export function errorHint(kind: WechatErrorKind, errmsg: string): string {
  switch (kind) {
    case 'ip-whitelist': {
      const ip = /invalid ip\s+([0-9a-fA-F.:]+)/.exec(errmsg)?.[1] ?? '';
      return (
        `Egress IP${ip ? ` ${ip}` : ''} is not in the IP whitelist.\n` +
        `  Go to the WeChat Developer Platform (developers.weixin.qq.com/platform/) → My Business → Official Account\n` +
        `    → Basic Info → Developer Keys → "API IP Whitelist", and ` +
        `${ip ? `add ${ip}` : 'add the public IP of this machine'}.\n` +
        `  ⚠️ As of 2025-12-01 this item has moved from "Development Interface Management" in the Official Accounts backend to the Developer Platform.\n` +
        `  Wildcards are not supported (write 172.0.0.1 or 172.0.0.1/24, not 172.0.0.*), and no port;\n` +
        `  after saving it takes a few minutes to take effect. Home broadband IPs change, so re-add it when they do.\n` +
        `  You can check the current egress IP with node src/publish/wechat.ts --ip.`
      );
    }
    case 'permission':
      return (
        `This account does not have permission for this endpoint (48001).\n` +
        `  The mass-send/publish endpoints **can only be called by verified accounts**; a personal subscription account can only use the draft-box endpoints —\n` +
        `  this is a WeChat limitation, not a problem with the script; the final publish must be clicked manually in the backend.`
      );
    case 'token':
      return 'access_token expired (40001/42001); just run it again (the script fetches a new token on every run).';
    case 'network':
      return 'Network request failed (timeout or connection drop); try again later.';
    default:
      return 'The API returned an error; see the errcode/errmsg above.';
  }
}

function debug(hooks: WechatHooks, msg: string): void {
  hooks.onDebug?.(msg);
}

/**
 * Send one request; `retries` is extra attempts (0 = one try).
 * Retry count by side effect: token 1 (idempotent), material/add_material 1 (a duplicate asset is harmless),
 * draft/add 3 (drafts are not public, so worst case is one extra draft that `--check` reconciles — unlike Juejin's no-retry publish),
 * read-only 1.
 */
async function request<T extends Envelope>(
  url: string,
  init: RequestInit,
  opts: WechatOptions,
  retries: number,
  hooks: WechatHooks,
): Promise<WechatResult<T>> {
  let lastErr = '';
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (attempt > 0) debug(hooks, `  Retry ${attempt}/${retries}`);
    try {
      const res = await fetch(url, { ...init, signal: AbortSignal.timeout(opts.timeoutMs) });
      const text = await res.text();
      if (!res.ok) {
        lastErr = `HTTP ${res.status}: ${text.slice(0, 200)}`;
        continue; // HTTP-layer 5xx/gateway errors are worth retrying
      }
      let body: T;
      try {
        body = JSON.parse(text) as T;
      } catch {
        return { ok: false, errMsg: `Response is not JSON: ${text.slice(0, 200)}`, kind: 'api', errCode: 0 };
      }
      const code = body.errcode ?? 0;
      if (code === 0) return { ok: true, data: body };
      const kind = classify(code);
      const errmsg = body.errmsg ?? '';
      // token expiry / network errors can retry; permission and IP-whitelist never benefit
      if (kind === 'token' && attempt < retries) {
        lastErr = `${code} ${errmsg}`;
        continue;
      }
      return { ok: false, errMsg: `${code} ${errmsg}`, kind, errCode: code };
    } catch (err) {
      lastErr = (err as Error).message;
    }
  }
  return { ok: false, errMsg: lastErr, kind: 'network', errCode: 0 };
}

// ============ access_token ============

/**
 * Cached only within one run (never on disk): persisting saves one request but writes another copy of a credential,
 * and one fetch per run is well within WeChat's rate cap.
 */
let cachedToken: { value: string; expiresAt: number } | null = null;

/** Expire 5 min early to avoid boundary races */
const TOKEN_SAFETY_MS = 5 * 60 * 1000;

export async function getAccessToken(
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<string>> {
  if (cachedToken && cachedToken.expiresAt - Date.now() > TOKEN_SAFETY_MS) {
    debug(hooks, 'Reusing the access_token already obtained in this run');
    return { ok: true, data: cachedToken.value };
  }
  const url =
    `${BASE}/cgi-bin/token?grant_type=client_credential` +
    `&appid=${encodeURIComponent(opts.appId)}&secret=${encodeURIComponent(opts.appSecret)}`;
  debug(hooks, 'GET /cgi-bin/token (appid and secret are not echoed)');
  const res = await request<Envelope & { access_token?: string; expires_in?: number }>(
    url,
    { method: 'GET' },
    opts,
    1,
    hooks,
  );
  if (!res.ok) return res;
  const token = res.data.access_token;
  if (!token) return { ok: false, errMsg: 'The response has no access_token', kind: 'api', errCode: 0 };
  cachedToken = { value: token, expiresAt: Date.now() + (res.data.expires_in ?? 7200) * 1000 };
  return { ok: true, data: token };
}

/** For tests/multiple runs: clear the in-memory token */
export function resetTokenCache(): void {
  cachedToken = null;
}

// ============ Permanent assets (cover) ============

const MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
};

/** Hand-rolled multipart: tsconfig has no DOM lib and global FormData types vary across Node versions */
function multipartBody(field: string, filePath: string, bytes: Buffer): { body: Buffer; contentType: string } {
  const boundary = `----wechatpublish${Date.now().toString(16)}`;
  const mime = MIME[path.extname(filePath).toLowerCase()] ?? 'image/png';
  const name = path.basename(filePath);
  const head =
    `--${boundary}\r\n` +
    `Content-Disposition: form-data; name="${field}"; filename="${name}"\r\n` +
    `Content-Type: ${mime}\r\n\r\n`;
  const tail = `\r\n--${boundary}--\r\n`;
  return {
    body: Buffer.concat([Buffer.from(head, 'utf8'), bytes, Buffer.from(tail, 'utf8')]),
    contentType: `multipart/form-data; boundary=${boundary}`,
  };
}

/**
 * Upload a permanent asset, returning media_id (usable as a draft cover's thumb_media_id).
 * Tries `thumb` then `image`: `thumb` has a hard 64KB limit that covers usually exceed, and both work as a cover.
 * Reports both errors on failure, no swallowing.
 */
export async function uploadThumb(
  filePath: string,
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<string>> {
  let bytes: Buffer;
  try {
    bytes = readFileSync(filePath);
  } catch (err) {
    return { ok: false, errMsg: `Cannot read cover file ${filePath}: ${(err as Error).message}`, kind: 'api', errCode: 0 };
  }

  const tokenRes = await getAccessToken(opts, hooks);
  if (!tokenRes.ok) return tokenRes;
  const token = tokenRes.data;

  const errors: string[] = [];
  for (const type of ['thumb', 'image']) {
    const { body, contentType } = multipartBody('media', filePath, bytes);
    const url = `${BASE}/cgi-bin/material/add_material?access_token=${token}&type=${type}`;
    debug(hooks, `POST /cgi-bin/material/add_material type=${type} (${bytes.length} bytes)`);
    const res = await request<Envelope & { media_id?: string; url?: string }>(
      url,
      { method: 'POST', headers: { 'Content-Type': contentType }, body },
      opts,
      1,
      hooks,
    );
    if (res.ok && res.data.media_id) return { ok: true, data: res.data.media_id };
    errors.push(`type=${type}: ${res.ok ? 'the response has no media_id' : res.errMsg}`);
  }
  return {
    ok: false,
    errMsg: `Cover upload failed (both asset types were tried)\n  ${errors.join('\n  ')}`,
    kind: 'api',
    errCode: 0,
  };
}

// ============ Draft ============

/** One article for a draft */
export interface DraftArticle {
  title: string;
  content: string;
  digest?: string;
  author?: string;
  thumbMediaId?: string;
  contentSourceUrl?: string;
}

/** Build the endpoint payload; empty fields are omitted (an empty string like author:'' may be judged invalid) */
function buildDraftPayload(article: DraftArticle): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    title: article.title,
    content: article.content,
    need_open_comment: 0,
    only_fans_can_comment: 0,
  };
  if (article.digest) payload.digest = article.digest;
  if (article.author) payload.author = article.author;
  if (article.thumbMediaId) payload.thumb_media_id = article.thumbMediaId;
  if (article.contentSourceUrl) payload.content_source_url = article.contentSourceUrl;
  return payload;
}

/**
 * Create a draft (creation-side: a retry may leave a duplicate; drafts are not public, so `--check` reconciles them).
 * The returned media_id is the draft's own id — persist it immediately, then create the next.
 */
export async function addDraft(
  article: DraftArticle,
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<string>> {
  const tokenRes = await getAccessToken(opts, hooks);
  if (!tokenRes.ok) return tokenRes;

  const payload = buildDraftPayload(article);

  debug(hooks, `POST /cgi-bin/draft/add title="${article.title}" content=${article.content.length} chars`);
  const res = await request<Envelope & { media_id?: string }>(
    `${BASE}/cgi-bin/draft/add?access_token=${tokenRes.data}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ articles: [payload] }),
    },
    opts,
    3,
    hooks,
  );
  if (!res.ok) return res;
  if (!res.data.media_id) return { ok: false, errMsg: 'Draft created successfully but no media_id was returned', kind: 'api', errCode: 0 };
  return { ok: true, data: res.data.media_id };
}

/**
 * Update a draft in place (use this after a digest/body change, not delete+recreate).
 * Differs from `addDraft`: path `draft/update`, an extra `media_id` in the body, and `articles` is an object (not an array) plus an `index`.
 * Delete+create swaps the media_id and the issue is absent in between — a mid-failure loses it; digest edits are the most
 * common change, so use this idempotent path (safe to retry).
 * ⚠️ Replaces the whole issue and omits `digest` when empty, so an existing digest can be cleared — our issues all have one.
 */
export async function updateDraft(
  mediaId: string,
  article: DraftArticle,
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<true>> {
  const tokenRes = await getAccessToken(opts, hooks);
  if (!tokenRes.ok) return tokenRes;

  const payload = buildDraftPayload(article);

  debug(hooks, `POST /cgi-bin/draft/update media_id=${mediaId} content=${article.content.length} chars`);
  const res = await request<Envelope>(
    `${BASE}/cgi-bin/draft/update?access_token=${tokenRes.data}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_id: mediaId, index: 0, articles: payload }),
    },
    opts,
    2,
    hooks,
  );
  return res.ok ? { ok: true, data: true } : res;
}

/** Delete a draft (for `--force` rebuild); only drafts, never published articles — why `--force` dares to delete+recreate */
export async function deleteDraft(
  mediaId: string,
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<true>> {
  const tokenRes = await getAccessToken(opts, hooks);
  if (!tokenRes.ok) return tokenRes;
  debug(hooks, `POST /cgi-bin/draft/delete media_id=${mediaId}`);
  const res = await request<Envelope>(
    `${BASE}/cgi-bin/draft/delete?access_token=${tokenRes.data}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_id: mediaId }),
    },
    opts,
    1,
    hooks,
  );
  return res.ok ? { ok: true, data: true } : res;
}

/** One draft-box item (only fields needed for reconciliation) */
export interface DraftBrief {
  mediaId: string;
  title: string;
  digest: string;
  thumbMediaId: string;
  updateTime: number;
}

/**
 * Fetch the draft box (read-only): reconcile locally recorded drafts, and when no cover is set,
 * scavenge a `thumb_media_id` from an existing draft (cover is required).
 */
export async function listDrafts(
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<DraftBrief[]>> {
  const tokenRes = await getAccessToken(opts, hooks);
  if (!tokenRes.ok) return tokenRes;

  const out: DraftBrief[] = [];
  const PAGE = 20;
  for (let offset = 0; ; offset += PAGE) {
    debug(hooks, `POST /cgi-bin/draft/batchget offset=${offset}`);
    const res = await request<
      Envelope & {
        total_count?: number;
        item_count?: number;
        item?: {
          media_id?: string;
          update_time?: number;
          content?: { news_item?: { title?: string; digest?: string; thumb_media_id?: string }[] };
        }[];
      }
    >(
      `${BASE}/cgi-bin/draft/batchget?access_token=${tokenRes.data}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ offset, count: PAGE, no_content: 0 }),
      },
      opts,
      1,
      hooks,
    );
    if (!res.ok) return res;

    const items = res.data.item ?? [];
    for (const it of items) {
      const news = it.content?.news_item?.[0];
      out.push({
        mediaId: it.media_id ?? '',
        title: news?.title ?? '',
        digest: news?.digest ?? '',
        thumbMediaId: news?.thumb_media_id ?? '',
        updateTime: it.update_time ?? 0,
      });
    }
    const total = res.data.total_count ?? out.length;
    if (out.length === 0 || items.length === 0 || out.length >= total) break;
    if (offset > 500) break; // Safety net: never loop forever on pagination
  }
  return { ok: true, data: out.filter((d) => d.mediaId) };
}

// ============ Helpers ============

/** Look up the public egress IP (for whitelist setup); third-party echo, only in this explicit command */
export async function fetchEgressIp(opts: WechatOptions): Promise<WechatResult<string>> {
  try {
    const res = await fetch('https://api.ipify.org', { signal: AbortSignal.timeout(opts.timeoutMs) });
    if (!res.ok) return { ok: false, errMsg: `HTTP ${res.status}`, kind: 'network', errCode: 0 };
    return { ok: true, data: (await res.text()).trim() };
  } catch (err) {
    return { ok: false, errMsg: (err as Error).message, kind: 'network', errCode: 0 };
  }
}
