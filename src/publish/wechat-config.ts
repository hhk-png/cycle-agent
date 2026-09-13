import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { select } from '@clack/prompts';
import type { TitleSource } from './articles.ts';
import { assertCookieIgnored, cookieFilePath } from './credential.ts';
import { listPublishConfigNames, publishConfigsDir, WECHAT_CONFIG_PREFIX } from './publish-config.ts';
import { isTTY } from '../shared/ui.ts';

/**
 * 微信公众号发布配置:每个教程一个文件,与掘金侧同住 src/publish/configs/,
 * 文件名以 `wechat-` 开头(如 `wechat-vllm.ts`),靠前缀与掘金配置区分。
 * 理由见 publish-config.ts 里 WECHAT_CONFIG_PREFIX 的注释。
 *
 * ⚠️ 与 publish-config.ts 一样,本模块必须**无副作用** —— 配置文件要
 * `import type { WechatConfig }` 从这里取类型,不能连带执行入口的 main()。
 */

/** 单个教程的微信发布配置 */
export interface WechatConfig {
  /** 源目录名(相对仓库根),如 'vllm-toturial' */
  sourceDir: string;
  /** 文件名前缀,匹配 <前缀>-<编号>-<标题>.md */
  filePrefix: string;
  /** 起始编号(公众号从第 08 章起) */
  fromNumber: number;
  /** 标题取原文 H1 还是文件名;省略按 'h1' */
  titleSource?: TitleSource;

  /** 公众号 AppID(不是密码,可以明文) */
  appId: string;
  /**
   * 存 AppSecret 的文件名(相对仓库根),默认 `.wechat-secret`。
   * 环境变量 `WECHAT_APPSECRET` 优先级更高。
   * ⚠️ 这个文件必须被 .gitignore 覆盖,写入前会强制校验(见 credential.ts)。
   */
  secretFile?: string;

  /** 摘要 digest,key = 两位编号。上限 120 字,建议 60~90;留空则让公众号按正文前 54 字自动生成 */
  digests: Record<string, string>;
  /** 个别篇的标题覆盖 —— 微信标题上限 **32 字**,超了会被拒 */
  titleOverrides?: Record<string, string>;
  /** 作者名(≤16 字);留空则不传该字段 */
  author?: string;
  /**
   * 封面图本地路径(如 'assets/cover.png')。留空则不传封面,
   * 改为**从草稿箱里已有草稿借一个 `thumb_media_id`** ——
   * 你手动排好第一篇并设好封面之后,脚本就能把那个封面复用到其余各期。
   */
  coverImage?: string;

  /** 排期起始日 `YYYY-MM-DD`,--plan 从这天开始逐日排 */
  startDate: string;

  /**
   * 拆篇时用的生效上限,默认 50 万(护栏,不是接口上限 —— 接口实测能收 18 万,
   * 见 src/publish/markdown.ts)。正常一章一期,不会触发拆分。
   */
  contentLimit?: number;
  /** 「阅读原文」链接;未认证订阅号不支持外链,默认留空 */
  contentSourceUrl?: string;

  /** 建草稿的间隔(毫秒) */
  delayMs: number;
  /** 单次请求超时(毫秒) */
  timeoutMs: number;
}

/** 默认的 AppSecret 文件名 */
export const DEFAULT_SECRET_FILE = '.wechat-secret';

export function wechatConfigFileName(name: string): string {
  return path.join(publishConfigsDir, `${name}.ts`);
}

/** 列出 src/publish/src/publish/configs/ 下已有的**微信**发布配置名(带 wechat- 前缀,返回值也带前缀) */
export function listWechatConfigNames(): string[] {
  if (!existsSync(publishConfigsDir)) return [];
  return readdirSync(publishConfigsDir)
    .filter((f) => f.endsWith('.ts') && f.startsWith(WECHAT_CONFIG_PREFIX))
    .map((f) => f.slice(0, -3))
    .sort();
}

export async function loadWechatConfig(name: string): Promise<WechatConfig> {
  const mod = (await import(pathToFileURL(wechatConfigFileName(name)).href)) as { default?: WechatConfig };
  if (!mod.default) throw new Error(`微信配置 ${name} 缺少 default 导出`);
  return mod.default;
}

/**
 * 未指定配置名时:只有一个就直用,多个则交互选择(TTY),否则报错并列出。
 * 连**名字**一起返回 —— 状态文件的名字要用它,不能只拿到配置对象。
 */
export async function pickWechatConfig(): Promise<{ name: string; config: WechatConfig }> {
  const names = listWechatConfigNames();
  if (names.length === 0) {
    throw new Error(
      `src/publish/src/publish/configs/ 下没有微信配置(需要 ${WECHAT_CONFIG_PREFIX}*.ts)。\n` +
        `  已有的掘金配置: ${listPublishConfigNames().join(', ') || '无'}`,
    );
  }
  if (names.length === 1) return { name: names[0], config: await loadWechatConfig(names[0]) };

  if (isTTY()) {
    const name = await select({
      message: '选择要发布的微信公众号配置:',
      options: names.map((n) => ({ value: n, label: n })),
    });
    if (typeof name !== 'string' || !name) process.exit(130);
    return { name, config: await loadWechatConfig(name) };
  }
  throw new Error(`src/publish/src/publish/configs/ 下有多个微信配置(${names.join(', ')}),请显式指定配置名`);
}

// ============ 凭据 ============

/** 只显示首尾各 4 位;长度不足时整体打码 */
export function maskSecret(s: string): string {
  if (s.length <= 8) return '****';
  return `${s.slice(0, 4)}…${s.slice(-4)}`;
}

export interface SecretReadResult {
  secret: string;
  /** 来源描述(用于打印,不含密钥本身) */
  source: string;
}

/**
 * 读 AppSecret:环境变量 `WECHAT_APPSECRET` 优先,其次配置文件。
 *
 * ⚠️ 与掘金的 sessionid 同级 —— 只打印掩码,永不回显明文,也不写进任何日志。
 * 读取前先确认该文件被 .gitignore 覆盖:**未通过就拒绝使用**,
 * 免得凭据已经躺在会被提交的位置上而我们还在用它发文章。
 */
export function readAppSecret(
  repoRoot: string,
  cfg: WechatConfig,
): SecretReadResult | { error: string } {
  const env = process.env.WECHAT_APPSECRET?.trim();
  if (env) return { secret: env, source: '环境变量 WECHAT_APPSECRET' };

  const fileName = cfg.secretFile ?? DEFAULT_SECRET_FILE;
  const risk = assertCookieIgnored(repoRoot, fileName);
  if (risk) return { error: risk };

  const file = cookieFilePath(repoRoot, fileName);
  if (!existsSync(file)) {
    return {
      error:
        `读不到 AppSecret:环境变量 WECHAT_APPSECRET 为空,${file} 也不存在。\n` +
        `  拿法:微信开发者平台(developers.weixin.qq.com/platform/)→ 我的业务 → 公众号\n` +
        `       → 基础信息 → 开发密钥 → 重置。⚠️ 平台不保存 AppSecret,只显示一次,\n` +
        `       忘了只能重置(重置会让旧的立刻失效)。\n` +
        `  ⚠️ 不要贴到对话里 —— 贴了就会留在记录里。写进文件即可:\n` +
        `     echo "你的AppSecret" > ${fileName}`,
    };
  }
  const secret = readFileSync(file, 'utf8').trim();
  if (!secret) return { error: `${file} 是空的。` };
  return { secret, source: `${fileName}(${maskSecret(secret)})` };
}

/** 校验配置里的必填项,返回问题列表(空数组=没问题)。不联网。 */
export function configProblems(cfg: WechatConfig): string[] {
  const out: string[] = [];
  if (!cfg.appId.trim()) {
    out.push('appId 还没填(微信开发者平台 → 我的业务 → 公众号 → 基础信息)');
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(cfg.startDate)) {
    out.push(`startDate 应为 YYYY-MM-DD,当前是 "${cfg.startDate}"`);
  }
  if (cfg.author && cfg.author.length > 16) {
    out.push(`author ${cfg.author.length} 字,超过 16 字上限`);
  }
  if (cfg.coverImage && !existsSync(path.resolve(process.cwd(), cfg.coverImage))) {
    out.push(`coverImage 指向的文件不存在: ${cfg.coverImage}`);
  }
  return out;
}
