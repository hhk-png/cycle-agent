import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { select } from '@clack/prompts';
import type { TitleSource } from './articles.ts';
import { isTTY } from '../shared/ui.ts';

/**
 * 发布配置:每个教程一个文件,放在 **src/publish/configs/** 下 —— 即
 * 「发布」这个项目自己的目录里,与教程迭代那份(`src/iterate/configs/`)完全分开。
 *
 * 两个项目各自持有自己的配置文件,互不扫描、互不干扰:
 * `src/iterate/config.ts` 的 listConfigNames() 只看 `src/iterate/configs/`,
 * 本模块只看 `src/publish/configs/`。所以往任一边加配置文件都不会影响另一边,
 * 也不需要在扫描时按前缀过滤掉对方的东西。
 *
 * ⚠️ 本模块必须**无副作用** —— 配置文件要 `import type { PublishConfig }` 从这里取类型。
 * 若把类型定义在入口 src/publish/juejin.ts,导入它会连带执行入口的 main() 和 SIGINT 注册,
 * 用户只想读个类型结果开始发文章。这也是 TutorialConfig 住在 config.ts 而非 run.ts 的原因。
 */

/** 单个教程的发布配置 */
export interface PublishConfig {
  /** 源目录名(相对仓库根),如 'vllm-toturial' */
  sourceDir: string;
  /** 文件名前缀,匹配 <前缀>-<编号>-<标题>.md,如 'vllm教程' */
  filePrefix: string;
  /** 起始编号,只发布 >= 它的章节 */
  fromNumber: number;
  /**
   * 标题取原文 H1 还是文件名(去掉 `.md`)。
   * 省略时按 'h1'。掘金上前几章用的是文件名形式,`--rename` 也按它对齐。
   */
  titleSource?: TitleSource;
  /** 掘金分类 id(用 --categories 查真实值) */
  categoryId: string;
  /** 掘金标签 id 列表(**最多 3 个**,服务端实测限制;用 --tags <关键词> 查真实值) */
  tagIds: string[];
  /** 封面图 URL;空串表示留空,靠人工在编辑器里设置后从草稿读回 */
  coverImage: string;
  /** 每篇摘要,key = 两位编号(如 '09')。必须 50~100 字、单行纯文本 */
  briefs: Record<string, string>;
  /** 篇间延迟(毫秒),用于降低风控概率 */
  delayMs: number;
  /** 单次请求超时(毫秒) */
  timeoutMs: number;
}

export const publishConfigsDir = path.resolve(process.cwd(), 'src', 'publish', 'configs');

/**
 * 微信侧配置的文件名前缀(如 `wechat-vllm.ts`)。
 *
 * 掘金与微信两种配置同住 src/publish/configs/(同一个教程的两个平台是一件事,
 * 分目录反而难找),但**类型不同、归各自的入口管**:掘金侧的 pickPublishConfig
 * 一旦把微信配置也算进来,`node src/publish/juejin.ts` 就会从「只有一个配置时直用」
 * 变成「有多个配置,请显式指定」—— 平白把已有的用法弄坏。所以两边各按前缀过滤。
 */
export const WECHAT_CONFIG_PREFIX = 'wechat-';

export function publishConfigFileName(name: string): string {
  return path.join(publishConfigsDir, `${name}.ts`);
}

/** 列出 src/publish/configs/ 下已有的**掘金**发布配置名(文件名去掉 .ts) */
export function listPublishConfigNames(): string[] {
  if (!existsSync(publishConfigsDir)) return [];
  return readdirSync(publishConfigsDir)
    .filter((f) => f.endsWith('.ts') && !f.startsWith(WECHAT_CONFIG_PREFIX))
    .map((f) => f.slice(0, -3))
    .sort();
}

export function hasPublishConfig(name: string): boolean {
  return existsSync(publishConfigFileName(name));
}

/** 加载某个发布配置,并校验必填项 */
export async function loadPublishConfig(name: string): Promise<PublishConfig> {
  const url = pathToFileURL(publishConfigFileName(name)).href;
  const mod = (await import(url)) as { default?: PublishConfig };
  if (!mod.default) throw new Error(`发布配置 ${name} 缺少 default 导出`);
  return mod.default;
}

/** 未指定配置名时:只有一个就直用,多个则交互选择(TTY),否则报错并列出 */
export async function pickPublishConfig(): Promise<PublishConfig> {
  const names = listPublishConfigNames();
  if (names.length === 0) {
    throw new Error('src/publish/configs/ 下还没有发布配置,复制一份改名即可新建');
  }
  if (names.length === 1) return loadPublishConfig(names[0]);

  if (isTTY()) {
    const name = await select({
      message: '选择要发布的教程配置:',
      options: names.map((n) => ({ value: n, label: n })),
    });
    if (typeof name !== 'string' || !name) process.exit(130);
    return loadPublishConfig(name);
  }
  throw new Error(`src/publish/configs/ 下有多个配置(${names.join(', ')}),请显式指定配置名`);
}
