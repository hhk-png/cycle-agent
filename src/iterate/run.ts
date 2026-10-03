import pc from 'picocolors';
import path from 'node:path';
import { killActiveClaude, runClaude } from './claude.ts';
import {
  isTTY,
  startSpinner,
  startStreamClock,
  stopSpinnerActive,
  stopStreamClockActive,
  header,
  roundHeader,
  success,
  error,
  info,
  dim,
} from '../shared/ui.ts';
import { mkdirSync } from 'node:fs';
import {
  hasConfig,
  listConfigNames,
  loadConfig,
  pickConfig,
  type TutorialConfig,
} from './config.ts';

/**
 * Fields come only from config files under src/iterate/configs/, one file per tutorial; copy a config
 * to start a new tutorial.
 *
 * Usage:
 *   node src/iterate/run.ts               # sole config, or select interactively if several
 *   node src/iterate/run.ts <config name> # run src/iterate/configs/<config name>.ts
 *   node src/iterate/run.ts --list        # list all configs
 */
async function main(): Promise<number> {
  const args = process.argv.slice(2);

  if (args[0] === '--list') {
    const names = listConfigNames();
    if (names.length === 0) info('There is no config file under src/iterate/configs/ yet; copy any file under src/iterate/configs/ and rename it to create one');
    else names.forEach((n) => info(`  ${n}`));
    return 0;
  }

  if (args.length > 1) {
    error('Usage: node src/iterate/run.ts [config name]');
    return 1;
  }

  const configName = args[0];
  let config: TutorialConfig;

  if (configName) {
    if (!hasConfig(configName)) {
      error(`No config under src/iterate/configs/: ${configName} (use node src/iterate/run.ts --list to see them)`);
      return 1;
    }
    config = await loadConfig(configName);
  } else {
    config = await pickConfig();
  }

  if (!config.description) {
    error('The config is missing an initial description; add description in the config file under src/iterate/configs/');
    return 1;
  }
  if (config.targetDir.includes('..') || path.isAbsolute(config.targetDir)) {
    error(`Invalid targetDir: ${config.targetDir}`);
    return 1;
  }

  const interactive = isTTY();
  const { title, targetDir, claudeFlags, startAt, dryRun, firstRoundPrompt, refinePrompt } = config;
  const description = config.description;
  const maxIterations = config.maxIterations;
  const resultLabel = `Result: ./${targetDir}/`;

  mkdirSync(targetDir, { recursive: true });

  header(title);
  dim(`  Description: ${truncate(description, 60)}`);
  dim(`  Rounds: round ${startAt}..${maxIterations}`);
  console.log('');

  for (let i = startAt; i <= maxIterations; i++) {
    roundHeader(i, maxIterations);

    const template = i === 1 ? firstRoundPrompt : refinePrompt;
    const prompt = template
      .replaceAll('{description}', description)
      .replaceAll('{targetDir}', targetDir);

    if (dryRun) {
      dim(`  -- dry-run · round ${i} --`);
      console.log(pc.dim(prompt));
      console.log('');
      continue;
    }

    const sp = interactive ? startSpinner(`Round ${i} · Claude generating`) : null;
    if (!interactive) dim(`▶ Round ${i} · Claude invoking …`);

    // Once claude starts outputting, stop the spinner and stream instead — but keep a time reference
    // via streamClock, which tops up an "elapsed Xs" line between body chunks.
    // ⚠️ Chunks must go through clock.write(); it guarantees erase → write → top-up order.
    const clock = startStreamClock(`Round ${i}`);
    let streaming = false;
    const streamStart = (): void => {
      if (!streaming) {
        streaming = true;
        stopSpinnerActive();
      }
    };

    const t0 = performance.now();
    const res = await runClaude(claudeFlags, prompt, {
      onStdout: (chunk) => {
        streamStart();
        clock.write(chunk);
      },
      onStderr: (chunk) => {
        streamStart();
        clock.write(chunk, 'stderr');
      },
    });
    clock.stop(); // Erase progress before the completion line overwrites it
    const secs = ((performance.now() - t0) / 1000).toFixed(1);

    if (res.exitCode === 0) {
      if (streaming) {
        success(`✔ Round ${i} complete (${secs}s)`);
      } else {
        sp?.stopSuccess(`✔ Round ${i} complete (${secs}s)`);
        if (res.stdout.trim()) info(res.stdout.trimEnd());
      }
    } else {
      if (!streaming) {
        sp?.stopError(`✖ Round ${i} failed`);
        if (res.stderr.trim()) console.error(pc.red(res.stderr.trimEnd().slice(-500)));
      }
      error(`✖ Round ${i} failed, exiting`);
      return 1;
    }
    console.log('');
  }

  header('Iteration finished');
  info(resultLabel);
  return 0;
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

process.on('SIGINT', () => {
  stopSpinnerActive();
  stopStreamClockActive();
  killActiveClaude();
  console.log(pc.red('✖ Interrupted'));
  process.exit(130);
});

main().then((code) => {
  process.exitCode = code;
});
