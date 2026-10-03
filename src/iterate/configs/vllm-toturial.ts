// Config name = file name; run with `node src/iterate/run.ts vllm-toturial`.
// To add a tutorial, copy this file, rename it, and edit the fields.

import type { TutorialConfig } from '../config.ts';

const config: TutorialConfig = {
  title: '教程迭代',
  targetDir: 'vllm-toturial', // results go under ./vllm-toturial/
  claudeFlags: [
    '-p',
    '--output-format',
    'text',
    '--effort',
    'high',
    '--dangerously-skip-permissions',
    '--verbose',
  ],
  startAt: 1, // start round; >1 resumes all rounds with the refine template
  dryRun: false, // true = print each round's prompt only (no claude)
  description:
    '生成一个vllm的教程，以章节的形式呈现，使用markdown。并且在其中需要包括一个简版的vllm的实现，这个实现需要按照工程化的思路实现，要包含vllm的所有的内容，并且可以运行，这个需要写完之后验证。教程也要包括所有的内容，现在的实现以及未来的方向和实现。尽可能的包括全部的信息',
  maxIterations: 10,
  firstRoundPrompt: `用户初始描述：{description}

请根据上述描述生成完整的教程。为了教程更完美，可以适当的删减、增加和修改章节和章节的内容，保证教程的可读性和连续性。
直接输出最终内容，并将内容保存到 {targetDir} 目录下。
只能操作 {targetDir} 目录下的文件，不能操作其他目录下的文件。`,
  refinePrompt: `用户初始描述：{description}
请读取 {targetDir} 目录下的文件章节内容，在此基础上结合用户的输入进一步细化和完善，输出更详细的版本。可以适当的删减、增加和修改章节和章节的内容，保证教程的可读性和连续性，使教程更完美，章节所覆盖的内容更完善。
直接输出最终内容，并将内容保存到 {targetDir}目录下。
每次先检查章节，看有没有可以增加和删除、修改的章节，如果有，可以对章节进行操作。
只能操作 {targetDir} 目录下的文件，不能操作其他目录下的文件。`,
};

export default config;
