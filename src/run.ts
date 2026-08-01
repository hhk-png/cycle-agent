import pc from 'picocolors';
import { killActiveClaude, runClaude } from './claude.ts';
import {
  isTTY,
  startSpinner,
  stopSpinnerActive,
  header,
  roundHeader,
  error,
  info,
  dim,
} from './ui.ts';
import { mkdir, mkdirSync } from 'node:fs';

const title = '教程迭代';
const targetDir = 'vllm-toturial'; // 目标目录名
const resultLabel = `结果: ./${targetDir}/`;
const claudeFlags = [
  '-p',
  '--output-format',
  'text',
  '--effort',
  'high',
  '--dangerously-skip-permissions',
  '--verbose',
];
const startAt = 1; // 起始轮次,默认 1
const dryRun = false; // true 时只打印每轮提示词, 不调用 claude（验证用）

const descArg =
  process.argv[2]
  ?? '生成一个vllm的教程，以章节的形式呈现，使用markdown。并且在其中需要包括一个简版的vllm的实现，这个实现需要按照工程化的思路实现，要包含vllm的所有的内容，并且可以运行，这个需要写完之后验证。教程也要包括所有的内容，现在的实现以及未来的方向和实现。尽可能的包括全部的信息'; // 初始描述（命令行第 1 个参数）
const iterArg = Number(process.argv[3]) || 10; // 最大迭代次数（命令行第 2 个参数）

const firstRoundPrompt = `用户初始描述：{description}

请根据上述描述生成完整的教程。为了教程更完美，可以适当的删减、增加和修改章节和章节的内容。
直接输出最终内容，并将内容保存到 {targetDir} 目录下。
只能操作 {targetDir} 目录下的文件，不能操作其他目录下的文件。`;

const refinePrompt = `用户初始描述：{description}
请读取 {targetDir} 目录下的文件章节内容，在此基础上结合用户的输入进一步细化和完善，输出更详细的版本。可以适当的删减、增加和修改章节和章节的内容，使教程更完美，章节所覆盖的内容更完善。
直接输出最终内容，并将内容保存到 {targetDir}目录下。
每次先检查章节，看有没有可以增加和删除、修改的章节，如果有，可以对章节进行操作。
只能操作 {targetDir} 目录下的文件，不能操作其他目录下的文件。`;

mkdirSync(targetDir, { recursive: true });

async function main(): Promise<number> {
  const interactive = isTTY();

  const description = descArg;
  const maxIterations = iterArg;

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

    const t0 = performance.now();
    const res = await runClaude(claudeFlags, prompt, {
      onData: (chunk) => sp?.update(Buffer.byteLength(chunk)),
    });
    const secs = ((performance.now() - t0) / 1000).toFixed(1);

    if (res.exitCode === 0) {
      sp?.stopSuccess(`✔ 第 ${i} 轮完成（${secs}s）`);
      // --verbose 的日志走 claude 的 stderr,这里一并打印
      if (res.stderr.trim()) console.error(res.stderr.trimEnd());
      if (res.stdout.trim()) info(res.stdout.trimEnd());
    } else {
      sp?.stopError(`✖ 第 ${i} 轮失败`);
      if (res.stderr.trim()) console.error(pc.red(res.stderr.trimEnd().slice(-500)));
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
