import pc from 'picocolors';
import path from 'node:path';
import { killActiveClaude, runClaude } from './claude.ts';
import {
  isTTY,
  startSpinner,
  stopSpinnerActive,
  header,
  roundHeader,
  success,
  error,
  info,
  dim,
} from './ui.ts';
import { mkdirSync } from 'node:fs';
import {
  hasConfig,
  listConfigNames,
  loadConfig,
  pickConfig,
  type TutorialConfig,
} from './config.ts';

/**
 * 运行只从 configs/ 下的配置文件加载字段,不接收任何任务参数。
 * 每个教程一个文件(configs/<配置名>.ts),新建教程 = 复制一份配置文件再改。
 *
 * 用法:
 *   node src/run.ts               # configs/ 下唯一配置直用,多个则交互选择
 *   node src/run.ts <配置名>      # 运行 configs/<配置名>.ts 里的字段
 *   node src/run.ts --list        # 列出 configs/ 下所有配置
 */
async function main(): Promise<number> {
  const args = process.argv.slice(2);

  if (args[0] === '--list') {
    const names = listConfigNames();
    if (names.length === 0) info('configs/ 下还没有配置文件,复制 configs/ 下任一文件改名即可新建');
    else names.forEach((n) => info(`  ${n}`));
    return 0;
  }

  if (args.length > 1) {
    error('用法: node src/run.ts [配置名]');
    return 1;
  }

  const configName = args[0];
  let config: TutorialConfig;

  if (configName) {
    if (!hasConfig(configName)) {
      error(`configs/ 下没有配置: ${configName}(可用 node src/run.ts --list 查看)`);
      return 1;
    }
    config = await loadConfig(configName);
  } else {
    config = await pickConfig();
  }

  if (!config.description) {
    error('配置缺少初始描述,请在 configs/ 的配置文件里补充 description');
    return 1;
  }
  if (config.targetDir.includes('..') || path.isAbsolute(config.targetDir)) {
    error(`targetDir 不合法: ${config.targetDir}`);
    return 1;
  }

  const interactive = isTTY();
  const { title, targetDir, claudeFlags, startAt, dryRun, firstRoundPrompt, refinePrompt } = config;
  const description = config.description;
  const maxIterations = config.maxIterations;
  const resultLabel = `结果: ./${targetDir}/`;

  mkdirSync(targetDir, { recursive: true });

  header(title);
  dim(`  描述: ${truncate(description, 60)}`);
  dim(`  轮次: 第 ${startAt}..${maxIterations} 轮`);
  console.log('');

  for (let i = startAt; i <= maxIterations; i++) {
    roundHeader(i, maxIterations);

    const template = i === 1 ? firstRoundPrompt : refinePrompt;
    const prompt = template
      .replaceAll('{description}', description)
      .replaceAll('{targetDir}', targetDir);

    if (dryRun) {
      dim(`  -- dry-run · 第 ${i} 轮 --`);
      console.log(pc.dim(prompt));
      console.log('');
      continue;
    }

    const sp = interactive ? startSpinner(`第 ${i} 轮 · Claude 生成中`) : null;
    if (!interactive) dim(`▶ 第 ${i} 轮 · Claude 调用中 …`);

    // claude 一开始输出就停掉 spinner,转为实时流式打印
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
        process.stdout.write(chunk);
      },
      onStderr: (chunk) => {
        streamStart();
        process.stderr.write(chunk);
      },
    });
    const secs = ((performance.now() - t0) / 1000).toFixed(1);

    if (res.exitCode === 0) {
      if (streaming) {
        success(`✔ 第 ${i} 轮完成（${secs}s）`);
      } else {
        sp?.stopSuccess(`✔ 第 ${i} 轮完成（${secs}s）`);
        if (res.stdout.trim()) info(res.stdout.trimEnd());
      }
    } else {
      if (!streaming) {
        sp?.stopError(`✖ 第 ${i} 轮失败`);
        if (res.stderr.trim()) console.error(pc.red(res.stderr.trimEnd().slice(-500)));
      }
      error(`✖ 第 ${i} 轮失败，退出`);
      return 1;
    }
    console.log('');
  }

  header('迭代结束');
  info(resultLabel);
  return 0;
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

process.on('SIGINT', () => {
  stopSpinnerActive();
  killActiveClaude();
  console.log(pc.red('✖ 已中断'));
  process.exit(130);
});

main().then((code) => {
  process.exitCode = code;
});
