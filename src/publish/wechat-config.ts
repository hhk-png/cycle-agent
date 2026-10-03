import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { select } from '@clack/prompts';
import type { TitleSource } from './articles.ts';
import { assertCookieIgnored, cookieFilePath } from './credential.ts';
import { listPublishConfigNames, publishConfigsDir, WECHAT_CONFIG_PREFIX } from './publish-config.ts';
import { isTTY } from '../shared/ui.ts';

/**
 * WeChat publish config: one file per tutorial, living in src/publish/configs/ with a
 * `wechat-` filename prefix to separate it from Juejin configs (see WECHAT_CONFIG_PREFIX).
 *
 * Must stay **side-effect free** like publish-config.ts: config files `import type` from
 * here and must not trigger the entry's main().
 */

/** Publish config for a single tutorial */
export interface WechatConfig {
  /** Source dir name (relative to repo root), e.g. 'vllm-toturial' */
  sourceDir: string;
  /** Filename prefix, matching <prefix>-<number>-<title>.md */
  filePrefix: string;
  /** Starting number (WeChat starts at chapter 08) */
  fromNumber: number;
  /** Title from the source H1 or the filename; defaults to 'h1' */
  titleSource?: TitleSource;
  /** Only with `titleSource: 'fileName'`: series-name prefix added to the title (see PublishConfig.titlePrefix) */
  titlePrefix?: string;

  /** WeChat AppID (not a secret, so plaintext is fine) */
  appId: string;
  /**
   * File holding the AppSecret (relative to repo root), default `.wechat-secret`.
   * `WECHAT_APPSECRET` env var takes precedence. Must be covered by .gitignore,
   * enforced before writing (see credential.ts).
   */
  secretFile?: string;

  /** Digest map, key = two-digit number. Max 120 chars, 60-90 recommended; empty lets WeChat auto-generate from the first 54 chars */
  digests: Record<string, string>;
  /** Per-article title overrides — the WeChat title limit is **32 chars**, over is rejected */
  titleOverrides?: Record<string, string>;
  /** Author name (<=16 chars); empty omits the field */
  author?: string;
  /**
   * Local cover image path (e.g. 'assets/cover.png'). Empty borrows a `thumb_media_id`
   * from an existing draft, so a manually set cover is reused across issues.
   */
  coverImage?: string;

  /** Schedule start date `YYYY-MM-DD`; --plan lays out day by day from here */
  startDate: string;

  /** Effective split limit, default 500k — a guardrail, not the API limit (see src/publish/markdown.ts) */
  contentLimit?: number;
  /** "Read original" link; unverified subscription accounts don't support external links, so empty by default */
  contentSourceUrl?: string;

  /** Delay between draft creations (ms) */
  delayMs: number;
  /** Per-request timeout (ms) */
  timeoutMs: number;
}

/** Default AppSecret filename */
export const DEFAULT_SECRET_FILE = '.wechat-secret';

export function wechatConfigFileName(name: string): string {
  return path.join(publishConfigsDir, `${name}.ts`);
}

/** List **WeChat** config names (returned with the `wechat-` prefix) */
export function listWechatConfigNames(): string[] {
  if (!existsSync(publishConfigsDir)) return [];
  return readdirSync(publishConfigsDir)
    .filter((f) => f.endsWith('.ts') && f.startsWith(WECHAT_CONFIG_PREFIX))
    .map((f) => f.slice(0, -3))
    .sort();
}

export async function loadWechatConfig(name: string): Promise<WechatConfig> {
  const mod = (await import(pathToFileURL(wechatConfigFileName(name)).href)) as { default?: WechatConfig };
  if (!mod.default) throw new Error(`WeChat config ${name} is missing a default export`);
  return mod.default;
}

/** Pick a config: sole one is used directly, several prompt on a TTY, else error. Returns the name too, which the state file needs. */
export async function pickWechatConfig(): Promise<{ name: string; config: WechatConfig }> {
  const names = listWechatConfigNames();
  if (names.length === 0) {
    throw new Error(
      `No WeChat config under src/publish/src/publish/configs/ (need ${WECHAT_CONFIG_PREFIX}*.ts).\n` +
        `  Existing Juejin configs: ${listPublishConfigNames().join(', ') || 'none'}`,
    );
  }
  if (names.length === 1) return { name: names[0], config: await loadWechatConfig(names[0]) };

  if (isTTY()) {
    const name = await select({
      message: 'Select the WeChat config to publish:',
      options: names.map((n) => ({ value: n, label: n })),
    });
    if (typeof name !== 'string' || !name) process.exit(130);
    return { name, config: await loadWechatConfig(name) };
  }
  throw new Error(`Multiple WeChat configs under src/publish/src/publish/configs/ (${names.join(', ')}); specify a config name explicitly`);
}

// ============ Credentials ============

/** Show only the first/last 4 chars; mask entirely if too short */
export function maskSecret(s: string): string {
  if (s.length <= 8) return '****';
  return `${s.slice(0, 4)}…${s.slice(-4)}`;
}

export interface SecretReadResult {
  secret: string;
  /** Source description (printed; never the secret itself) */
  source: string;
}

/**
 * Read the AppSecret: `WECHAT_APPSECRET` env var first, then the config file. Only the
 * mask is ever printed. The file must be proven .gitignore'd first — if not, refuse to
 * use it rather than publish with a credential sitting in a committable location.
 */
export function readAppSecret(
  repoRoot: string,
  cfg: WechatConfig,
): SecretReadResult | { error: string } {
  const env = process.env.WECHAT_APPSECRET?.trim();
  if (env) return { secret: env, source: 'WECHAT_APPSECRET env var' };

  const fileName = cfg.secretFile ?? DEFAULT_SECRET_FILE;
  const risk = assertCookieIgnored(repoRoot, fileName);
  if (risk) return { error: risk };

  const file = cookieFilePath(repoRoot, fileName);
  if (!existsSync(file)) {
    return {
      error:
        `Cannot read AppSecret: WECHAT_APPSECRET env var is empty and ${file} does not exist.\n` +
        `  How to get it: WeChat developer platform (developers.weixin.qq.com/platform/) → My business → Public account\n` +
        `       → Basic info → Developer secret → Reset. ⚠️ The platform does not store AppSecret, it is shown only once,\n` +
        `       so if you forget it you can only reset (resetting invalidates the old one immediately).\n` +
        `  ⚠️ Do not paste it into a chat — it would stay in the record. Just write it to the file:\n` +
        `     echo "yourAppSecret" > ${fileName}`,
    };
  }
  const secret = readFileSync(file, 'utf8').trim();
  if (!secret) return { error: `${file} is empty.` };
  return { secret, source: `${fileName}(${maskSecret(secret)})` };
}

/** Validate required fields, returning problems (empty array = fine). No network. */
export function configProblems(cfg: WechatConfig): string[] {
  const out: string[] = [];
  if (!cfg.appId.trim()) {
    out.push('appId is not set (WeChat developer platform → My business → Public account → Basic info)');
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(cfg.startDate)) {
    out.push(`startDate must be YYYY-MM-DD, currently "${cfg.startDate}"`);
  }
  if (cfg.author && cfg.author.length > 16) {
    out.push(`author is ${cfg.author.length} chars, over the 16-char limit`);
  }
  if (cfg.coverImage && !existsSync(path.resolve(process.cwd(), cfg.coverImage))) {
    out.push(`coverImage points to a missing file: ${cfg.coverImage}`);
  }
  return out;
}
