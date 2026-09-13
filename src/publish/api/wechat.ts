import { readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * 微信公众号接口的唯一网络层。
 *
 * 与 juejin.ts 同一套骨架(Result 对象不抛异常、按端点给不同重试次数、
 * `AbortSignal.timeout`、onDebug 钩子),但不复用它 —— 那边的信封是掘金的
 * `err_no`/`err_msg`(错误时 HTTP 未必非 200),微信是 HTTP 200 + `errcode`/`errmsg`,
 * 两套错误模型硬凑在一起只会互相污染。
 *
 * ⚠️ 关于**个人订阅号**的两条硬限制(决定了本模块只做到「建草稿」):
 *
 * - 群发接口(`message/mass/sendall`)与发布接口(`freepublish/submit`)**仅认证号可调**,
 *   个人主体调用返回 `48001`。所以没有任何接口能把文章真正发出去,发表那一步
 *   必须在公众号后台点(后台自带「定时发表」)。
 * - `draft/add`(新增草稿)**个人号可用** —— 这也是本模块存在的理由。
 *
 * ⚠️ `access_token` 要求调用方的**公网出口 IP 在后台 IP 白名单里**,否则 `40164`。
 * 家用宽带 IP 会变,这是这条路径唯一的日常维护点。报这个错时会把出口 IP
 * 从错误信息里抠出来直接打给用户,省掉一轮排查。
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

/** 出错的原因分类 —— 调用方据此给不同的可操作提示,而不是笼统一句「失败了」 */
export type WechatErrorKind =
  /** 网络层:超时、DNS、连接中断 —— 重试可能有用 */
  | 'network'
  /** 40164:出口 IP 不在白名单 —— 必须去后台加白名单 */
  | 'ip-whitelist'
  /** 48001 等:账号没有该接口权限(个人订阅号发不了文章就是这一类) */
  | 'permission'
  /** 40001/42001:access_token 失效 */
  | 'token'
  /** 其它接口错误 */
  | 'api';

export type WechatResult<T> =
  | { ok: true; data: T }
  | { ok: false; errMsg: string; kind: WechatErrorKind; errCode: number };

/** 微信的错误信封:HTTP 200,错误在 body 里 */
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
 * 把错误翻译成「下一步该做什么」。
 * 40164 的错误信息里带着出口 IP,抠出来直接给用户,免得他再去搜「怎么看我公网 IP」。
 */
export function errorHint(kind: WechatErrorKind, errmsg: string): string {
  switch (kind) {
    case 'ip-whitelist': {
      const ip = /invalid ip\s+([0-9a-fA-F.:]+)/.exec(errmsg)?.[1] ?? '';
      return (
        `出口 IP${ip ? ` ${ip}` : ''} 不在 IP 白名单里。\n` +
        `  去微信开发者平台(developers.weixin.qq.com/platform/)→ 我的业务 → 公众号\n` +
        `    → 基础信息 → 开发密钥 →「API IP 白名单」,` +
        `${ip ? `把 ${ip} 加进去` : '把本机公网 IP 加进去'}。\n` +
        `  ⚠️ 2025-12-01 起这一项已从公众号后台的「开发接口管理」迁到开发者平台。\n` +
        `  不支持通配符(写 172.0.0.1 或 172.0.0.1/24,不能写 172.0.0.*),也不带端口;\n` +
        `  保存后要等几分钟生效。家用宽带 IP 会变,变了要重新加。\n` +
        `  当前出口 IP 可用 node src/publish/wechat.ts --ip 查。`
      );
    }
    case 'permission':
      return (
        `该账号没有这个接口的权限(48001)。\n` +
        `  群发/发布接口**仅认证号**可调,个人订阅号只能用草稿箱接口 ——\n` +
        `  这是微信的限制,不是脚本的问题;最后一篇要手动在后台点发表。`
      );
    case 'token':
      return 'access_token 失效(40001/42001),重跑一次即可(脚本每次运行会重新取 token)。';
    case 'network':
      return '网络请求失败(超时或连接中断),稍后重试。';
    default:
      return '接口返回错误,见上面的 errcode/errmsg。';
  }
}

function debug(hooks: WechatHooks, msg: string): void {
  hooks.onDebug?.(msg);
}

/**
 * 发一个请求。
 * `retries` 是**额外**重试次数(0 表示只试一次)。
 *
 * ⚠️ 重试次数按端点的副作用给:
 * - `token` 1 次(幂等,取新 token 会让旧 token 失效,但每次运行本来就只取一次)
 * - `material/add_material` 1 次(重复上传只是多一份素材,无害)
 * - `draft/add` **3 次** —— 与掘金的「发布不重试」相反:草稿不公开,
 *   最坏是多一个草稿,`--check` 能立刻对出重复;而漏建一篇要人工补,更烦。
 * - 只读接口 1 次
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
    if (attempt > 0) debug(hooks, `  重试 ${attempt}/${retries}`);
    try {
      const res = await fetch(url, { ...init, signal: AbortSignal.timeout(opts.timeoutMs) });
      const text = await res.text();
      if (!res.ok) {
        lastErr = `HTTP ${res.status}: ${text.slice(0, 200)}`;
        continue; // HTTP 层的 5xx/网关错误值得重试
      }
      let body: T;
      try {
        body = JSON.parse(text) as T;
      } catch {
        return { ok: false, errMsg: `返回的不是 JSON: ${text.slice(0, 200)}`, kind: 'api', errCode: 0 };
      }
      const code = body.errcode ?? 0;
      if (code === 0) return { ok: true, data: body };
      const kind = classify(code);
      const errmsg = body.errmsg ?? '';
      // token 失效 / 网络类错误可以重试,权限与 IP 白名单重试多少次都一样
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
 * token 只在**一次运行内**缓存(不落盘)。
 * 落盘能少一次请求,但会把一个凭据多写一份到磁盘上 —— 而每次运行取一次
 * 完全够用(微信侧对取 token 的频率有上限,但没有到「每次运行取一次」会超的程度)。
 */
let cachedToken: { value: string; expiresAt: number } | null = null;

/** 提前 5 分钟过期,避免卡在边界上 */
const TOKEN_SAFETY_MS = 5 * 60 * 1000;

export async function getAccessToken(
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<string>> {
  if (cachedToken && cachedToken.expiresAt - Date.now() > TOKEN_SAFETY_MS) {
    debug(hooks, '复用本次运行已取得的 access_token');
    return { ok: true, data: cachedToken.value };
  }
  const url =
    `${BASE}/cgi-bin/token?grant_type=client_credential` +
    `&appid=${encodeURIComponent(opts.appId)}&secret=${encodeURIComponent(opts.appSecret)}`;
  debug(hooks, 'GET /cgi-bin/token (appid 与 secret 不回显)');
  const res = await request<Envelope & { access_token?: string; expires_in?: number }>(
    url,
    { method: 'GET' },
    opts,
    1,
    hooks,
  );
  if (!res.ok) return res;
  const token = res.data.access_token;
  if (!token) return { ok: false, errMsg: '返回里没有 access_token', kind: 'api', errCode: 0 };
  cachedToken = { value: token, expiresAt: Date.now() + (res.data.expires_in ?? 7200) * 1000 };
  return { ok: true, data: token };
}

/** 供测试/多次运行使用:清掉内存里的 token */
export function resetTokenCache(): void {
  cachedToken = null;
}

// ============ 永久素材(封面) ============

const MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
};

/**
 * 手写 multipart 而不是用 FormData/Blob:
 * 仓库的 tsconfig 没有 DOM lib,依赖全局 FormData 的类型在各版本 Node 里不一致;
 * multipart 本身只有「一段头 + 内容 + 结束边界」,手写反而没有不确定性。
 */
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
 * 上传永久素材,返回 media_id(可作为草稿封面的 thumb_media_id)。
 *
 * 先按 `thumb` 传,失败再按 `image` 传:
 * `thumb` 类素材有 **64KB** 的硬限制,而公众号封面图通常远超 —— 两者都能当封面用,
 * 所以顺序试比让用户先去压缩图片友好。失败时两个错误都报出来,不吞。
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
    return { ok: false, errMsg: `读不到封面文件 ${filePath}: ${(err as Error).message}`, kind: 'api', errCode: 0 };
  }

  const tokenRes = await getAccessToken(opts, hooks);
  if (!tokenRes.ok) return tokenRes;
  const token = tokenRes.data;

  const errors: string[] = [];
  for (const type of ['thumb', 'image']) {
    const { body, contentType } = multipartBody('media', filePath, bytes);
    const url = `${BASE}/cgi-bin/material/add_material?access_token=${token}&type=${type}`;
    debug(hooks, `POST /cgi-bin/material/add_material type=${type} (${bytes.length} 字节)`);
    const res = await request<Envelope & { media_id?: string; url?: string }>(
      url,
      { method: 'POST', headers: { 'Content-Type': contentType }, body },
      opts,
      1,
      hooks,
    );
    if (res.ok && res.data.media_id) return { ok: true, data: res.data.media_id };
    errors.push(`type=${type}: ${res.ok ? '返回里没有 media_id' : res.errMsg}`);
  }
  return {
    ok: false,
    errMsg: `上传封面失败(两种素材类型都试过)\n  ${errors.join('\n  ')}`,
    kind: 'api',
    errCode: 0,
  };
}

// ============ 草稿 ============

/** 新增草稿要传的一篇 */
export interface DraftArticle {
  title: string;
  content: string;
  digest?: string;
  author?: string;
  thumbMediaId?: string;
  contentSourceUrl?: string;
}

/**
 * 新增草稿。
 * ⚠️ 这是**建立侧**的接口:重试可能留下重复草稿(草稿不公开,`--check` 能对出来)。
 * 返回的 media_id 是草稿自己的 id —— 立刻落盘,再建下一篇。
 */
export async function addDraft(
  article: DraftArticle,
  opts: WechatOptions,
  hooks: WechatHooks = {},
): Promise<WechatResult<string>> {
  const tokenRes = await getAccessToken(opts, hooks);
  if (!tokenRes.ok) return tokenRes;

  // 空字段一律不传:传 author:'' 之类的空串有可能被判成非法值
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

  debug(hooks, `POST /cgi-bin/draft/add title="${article.title}" content=${article.content.length} 字符`);
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
  if (!res.data.media_id) return { ok: false, errMsg: '新增草稿成功但没返回 media_id', kind: 'api', errCode: 0 };
  return { ok: true, data: res.data.media_id };
}

/**
 * 删除草稿(`--force` 重建时用)。
 * 只删草稿,动不到已发表的文章 —— 这也是 `--force` 敢做「删了重建」的底气,
 * 换成已公开的文章绝不会有这个操作。
 */
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

/** 草稿箱里的一条(只取对账需要的字段) */
export interface DraftBrief {
  mediaId: string;
  title: string;
  digest: string;
  thumbMediaId: string;
  updateTime: number;
}

/**
 * 拉草稿箱(只读)。
 * 用途有两个:对账(本地记的草稿还在不在),以及**在没有配封面图时
 * 从已有草稿里捡一个 thumb_media_id 复用**(封面是必填项,用户手动排好一篇
 * 之后我们就能把封面借过来)。
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
    if (offset > 500) break; // 兜底:绝不在分页上转死循环
  }
  return { ok: true, data: out.filter((d) => d.mediaId) };
}

// ============ 辅助 ============

/**
 * 查本机公网出口 IP(配 IP 白名单时用)。
 * 走第三方回显服务 —— 只在这个显式命令里调用,不在正常发文流程里调用。
 */
export async function fetchEgressIp(opts: WechatOptions): Promise<WechatResult<string>> {
  try {
    const res = await fetch('https://api.ipify.org', { signal: AbortSignal.timeout(opts.timeoutMs) });
    if (!res.ok) return { ok: false, errMsg: `HTTP ${res.status}`, kind: 'network', errCode: 0 };
    return { ok: true, data: (await res.text()).trim() };
  } catch (err) {
    return { ok: false, errMsg: (err as Error).message, kind: 'network', errCode: 0 };
  }
}
