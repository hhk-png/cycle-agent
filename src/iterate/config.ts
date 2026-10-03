import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { select } from '@clack/prompts';
import { isTTY } from '../shared/ui.ts';

/** Config for one tutorial; each src/iterate/configs/<name>.ts default-exports one */
export interface TutorialConfig {
  /** Terminal title */
  title: string;
  /** Results are saved under ./<targetDir>/ */
  targetDir: string;
  /** claude launch arguments */
  claudeFlags: string[];
  /** Start round; when >1 every round uses the refine template (resume on existing content) */
  startAt: number;
  /** When true, print each prompt without invoking claude (verification) */
  dryRun: boolean;
  /** Initial description */
  description: string;
  /** Maximum number of iterations */
  maxIterations: number;
  /** Template for round 1; {description}/{targetDir} are substituted */
  firstRoundPrompt: string;
  /** Refine-round prompt template */
  refinePrompt: string;
}

export const configsDir = path.resolve(process.cwd(), 'src', 'iterate', 'configs');

export function configFileName(name: string): string {
  return path.join(configsDir, `${name}.ts`);
}

/** Config names under src/iterate/configs/ (file names without .ts) */
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

/** Load a saved config */
export async function loadConfig(name: string): Promise<TutorialConfig> {
  const url = pathToFileURL(configFileName(name)).href;
  const mod = (await import(url)) as { default?: TutorialConfig };
  if (!mod.default) throw new Error(`Config ${name} is missing a default export`);
  return mod.default;
}

/** No name given: use the only one, interactively select among several (TTY), else error */
export async function pickConfig(): Promise<TutorialConfig> {
  const names = listConfigNames();
  if (names.length === 0) {
    throw new Error('There is no config file under src/iterate/configs/ yet; copy any file under src/iterate/configs/ and rename it to create one');
  }
  if (names.length === 1) return loadConfig(names[0]);

  if (isTTY()) {
    const name = await select({
      message: 'Select the tutorial config to run:',
      options: names.map((n) => ({ value: n, label: n })),
    });
    if (typeof name !== 'string' || !name) process.exit(130);
    return loadConfig(name);
  }
  throw new Error(`There are multiple configs under src/iterate/configs/ (${names.join(', ')}); please specify a config name explicitly`);
}
