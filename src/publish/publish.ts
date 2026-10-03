import pc from 'picocolors';
import { error, stopSpinnerActive } from '../shared/ui.ts';
import { listPublishConfigNames } from './publish-config.ts';
import { listWechatConfigNames } from './wechat-config.ts';
import { run as runJuejin } from './juejin.ts';
import { run as runWechat } from './wechat.ts';

/**
 * **The single publish entry point**: the first argument selects the platform.
 *
 *   node src/publish/publish.ts juejin [configName] [options…]
 *   node src/publish/publish.ts wechat [configName] [options…]
 *
 * Both platforms differ in state, locks, digests and covers, but share one rhythm
 * (--list → drafts → stop after the 1st → --yes), so a single entry leaves one place
 * to get it wrong. Aliases: jj / wx(plus the Chinese names, see ALIASES). Full flow: src/publish/README.md.
 */

/** Aliases for the two platforms */
const ALIASES: Record<string, 'juejin' | 'wechat'> = {
  juejin: 'juejin',
  jj: 'juejin',
  wechat: 'wechat',
  wx: 'wechat',
};

export function usage(): string {
  const juejinConfigs = listPublishConfigNames();
  const wechatConfigs = listWechatConfigNames();
  return [
    'Usage: node src/publish/publish.ts <platform> [configName] [options…]',
    '',
    'Platforms:',
    '  juejin, jj      publish to Juejin (create drafts / publish / sync)',
    '  wechat, wx      publish to a WeChat Official Account (drafts only)',
    '',
    `Juejin configs: ${juejinConfigs.join(', ') || '(none)'}`,
    `WeChat configs: ${wechatConfigs.join(', ') || '(none)'}`,
    '',
    'Typical flow (using ai-agent-toturial as an example):',
    '  1. Validate digests and config  node src/publish/publish.ts juejin ai-agent-toturial --list',
    '  2. Create drafts only (not public)  node src/publish/publish.ts juejin ai-agent-toturial --drafts-only',
    '     ↳ After the 1st one is created it stops, so you can set the cover and check the layout in the editor',
    '  3. Finish the rest      node src/publish/publish.ts juejin ai-agent-toturial --drafts-only --yes',
    '  4. Publish              node src/publish/publish.ts juejin ai-agent-toturial --yes',
    '  5. Sync after changing digests/titles  node src/publish/publish.ts juejin ai-agent-toturial --sync',
    '',
    'The WeChat side is the same, just replace juejin with wechat (it has no publish API, so it stops at drafts):',
    '  node src/publish/publish.ts wechat wechat-ai-agent --list',
    '  node src/publish/publish.ts wechat wechat-ai-agent --drafts',
    '',
    "Each platform's own options (use --help for details):",
    '  Juejin: --list --categories --tags <word> --suggest-briefs --dry-run --drafts-only',
    '        --yes --from NN --to NN --only NN --sync --republish NN --force --orphans',
    '  WeChat: --list --build --plan --check --drafts --ip --only NN --force --yes',
  ].join('\n');
}

async function main(): Promise<number> {
  const argv = process.argv.slice(2);

  if (argv.length === 0 || argv[0] === '--help' || argv[0] === '-h') {
    // No platform argument is not an error, it means you want the usage — use stdout, so the shell doesn't think it failed
    console.log(usage());
    return argv.length === 0 ? 1 : 0;
  }

  const platform = ALIASES[argv[0]];
  if (!platform) {
    error(`Unknown platform: ${argv[0]}(available: juejin / wechat)`);
    console.log(usage());
    return 1;
  }

  const rest = argv.slice(1);
  if (rest.includes('--help') || rest.includes('-h')) {
    console.log(usage());
    return 0;
  }

  return platform === 'juejin' ? runJuejin(rest) : runWechat(rest);
}

// Register SIGINT once: per-platform handlers would fire twice on one Ctrl-C. Don't write
// state here(disk-write race) — each completed step saves, keeping runs resumable.
process.on('SIGINT', () => {
  stopSpinnerActive();
  console.log(pc.red('✖ Interrupted'));
  process.exit(130);
});

main()
  .then((code) => {
    process.exitCode = code;
  })
  .catch((err: unknown) => {
    error(`Unexpected error: ${(err as Error).message}`);
    process.exitCode = 1;
  });
