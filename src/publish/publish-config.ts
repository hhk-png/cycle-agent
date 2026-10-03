import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { select } from '@clack/prompts';
import type { TitleSource } from './articles.ts';
import { isTTY } from '../shared/ui.ts';

/**
 * Publish config: one file per tutorial under **src/publish/configs/** — this project's
 * own directory, fully separate from the iterate side (`src/iterate/configs/`). Each side
 * scans only its own dir, so adding a config to one never affects the other.
 *
 * Must stay **side-effect free**: config files `import type { PublishConfig }` from here.
 * Defining the type in the entry (src/publish/juejin.ts) would make that import run the
 * entry's main() and SIGINT handler just to read a type.
 */

/** Publish config for a single tutorial */
export interface PublishConfig {
  /** Source dir name (relative to repo root), e.g. 'vllm-toturial' */
  sourceDir: string;
  /** Filename prefix, matching <prefix>-<number>-<title>.md */
  filePrefix: string;
  /** Starting number; only chapters >= it are published */
  fromNumber: number;
  /** Title from the source H1 or the filename (without `.md`); defaults to 'h1'. `--rename` aligns to it too. */
  titleSource?: TitleSource;
  /** Only with `titleSource: 'fileName'`: series-name prefix for tutorials whose filenames lack one. */
  titlePrefix?: string;
  /** Juejin category id (look up real values with --categories) */
  categoryId: string;
  /** Juejin tag id list (**max 3**, a server-side limit; look up real values with --tags <keyword>) */
  tagIds: string[];
  /** Cover image URL; empty string means leave it, set manually in the editor and read back from the draft */
  coverImage: string;
  /** Per-article brief, key = two-digit number (e.g. '09'). Must be 50-100 chars, single-line plain text */
  briefs: Record<string, string>;
  /** Delay between articles (ms), to lower rate-limit risk */
  delayMs: number;
  /** Per-request timeout (ms) */
  timeoutMs: number;
}

export const publishConfigsDir = path.resolve(process.cwd(), 'src', 'publish', 'configs');

/**
 * Filename prefix for WeChat-side configs (e.g. `wechat-vllm.ts`). Juejin and WeChat
 * configs share src/publish/configs/, but each entry filters by prefix — otherwise the
 * Juejin side would see "multiple configs" and stop using its sole-config shortcut.
 */
export const WECHAT_CONFIG_PREFIX = 'wechat-';

export function publishConfigFileName(name: string): string {
  return path.join(publishConfigsDir, `${name}.ts`);
}

/** List **Juejin** config names (filenames without .ts) */
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

/** Load a publish config */
export async function loadPublishConfig(name: string): Promise<PublishConfig> {
  const url = pathToFileURL(publishConfigFileName(name)).href;
  const mod = (await import(url)) as { default?: PublishConfig };
  if (!mod.default) throw new Error(`Publish config ${name} is missing a default export`);
  return mod.default;
}

/** Pick a config: sole one is used directly, several prompt on a TTY, else error */
export async function pickPublishConfig(): Promise<PublishConfig> {
  const names = listPublishConfigNames();
  if (names.length === 0) {
    throw new Error('No publish config under src/publish/configs/ yet; copy one and rename it to create a new one');
  }
  if (names.length === 1) return loadPublishConfig(names[0]);

  if (isTTY()) {
    const name = await select({
      message: 'Select the tutorial config to publish:',
      options: names.map((n) => ({ value: n, label: n })),
    });
    if (typeof name !== 'string' || !name) process.exit(130);
    return loadPublishConfig(name);
  }
  throw new Error(`Multiple configs under src/publish/configs/ (${names.join(', ')}); specify a config name explicitly`);
}
