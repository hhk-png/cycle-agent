import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { select } from '@clack/prompts';
import { isTTY } from './ui.ts';

/** 单个教程的完整配置;每个 configs/<名称>.ts 文件默认导出一个该对象 */
export interface TutorialConfig {
  /** 终端标题 */
  title: string;
  /** 目标目录名(结果保存到 ./<targetDir>/ 下) */
  targetDir: string;
  /** claude 启动参数 */
  claudeFlags: string[];
  /** 起始轮次,默认 1;>1 时所有轮次都用精炼模板(配合已有内容续跑) */
  startAt: number;
  /** true 时只打印每轮提示词,不调用 claude(验证用) */
  dryRun: boolean;
  /** 初始描述 */
  description: string;
  /** 最大迭代次数 */
  maxIterations: number;
  /** 首轮提示词模板,{description}/{targetDir} 会被替换 */
  firstRoundPrompt: string;
  /** 精炼轮提示词模板 */
  refinePrompt: string;
}

export const configsDir = path.resolve(process.cwd(), 'configs');

export function configFileName(name: string): string {
  return path.join(configsDir, `${name}.ts`);
}

/** 列出 configs/ 下已有的配置名(文件名去掉 .ts) */
export function listConfigNames(): string[] {
  if (!existsSync(configsDir)) return [];
  return readdirSync(configsDir)
    .filter((f) => f.endsWith('.ts'))
    .map((f) => f.slice(0, -3))
    .sort();
}

export function hasConfig(name: string): boolean {
  return existsSync(configFileName(name));
}

/** 加载某个已保存的配置 */
export async function loadConfig(name: string): Promise<TutorialConfig> {
  const url = pathToFileURL(configFileName(name)).href;
  const mod = (await import(url)) as { default?: TutorialConfig };
  if (!mod.default) throw new Error(`配置 ${name} 缺少 default 导出`);
  return mod.default;
}

/** 未指定配置名时:只有一个就直用,多个则交互选择(TTY),否则报错并列出 */
export async function pickConfig(): Promise<TutorialConfig> {
  const names = listConfigNames();
  if (names.length === 0) {
    throw new Error('configs/ 下还没有配置文件,复制 configs/ 下任一文件改名即可新建');
  }
  if (names.length === 1) return loadConfig(names[0]);

  if (isTTY()) {
    const name = await select({
      message: '选择要运行的教程配置:',
      options: names.map((n) => ({ value: n, label: n })),
    });
    if (typeof name !== 'string' || !name) process.exit(130);
    return loadConfig(name);
  }
  throw new Error(`configs/ 下有多个配置(${names.join(', ')}),请显式指定配置名`);
}
