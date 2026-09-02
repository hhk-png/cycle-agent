// 本教程的完整配置。配置名 = 文件名(configs/ai-agent-toturial.ts → ai-agent-toturial)。
// 运行: node src/run.ts ai-agent-toturial
// 新建其他教程: 复制本文件改名,再修改里面的字段即可。

import type { TutorialConfig } from '../src/config.ts';

const config: TutorialConfig = {
  title: 'AI Agent 教程迭代',
  targetDir: 'ai-agent-toturial', // 目标目录名(结果保存到 ./ai-agent-toturial/ 下)
  claudeFlags: [
    '-p',
    '--output-format',
    'text',
    '--effort',
    'high',
    '--dangerously-skip-permissions',
    '--verbose',
  ],
  startAt: 1, // 起始轮次,默认 1;>1 时所有轮次都用精炼模板续跑
  dryRun: false, // true 时只打印每轮提示词,不调用 claude(验证用)
  description:
    '生成一个AI Agent(智能体)的教程，以章节的形式呈现，使用markdown。并且在其中需要包括一个简版的AI Agent的实现，使用typescript写，这个实现需要按照工程化的思路实现，要包含AI Agent的所有核心内容，并且可以运行，这个需要写完之后验证。教程也要包括所有内容：AI Agent的概念与工作原理、主流框架、大模型接口与工具调用、记忆管理、多智能体协作、MCP、Agent评测与安全、生产部署，以及现状与未来方向。尽可能的包括全部的信息，要有技术深度，要想写一本能传播多年的书一样对待，比如重构、代码大全那样。',
  maxIterations: 10,
  firstRoundPrompt: `用户初始描述：{description}

请根据上述描述生成完整的教程。为了教程更完美，可以适当的删减、增加和修改章节和章节的内容。
直接输出最终内容，并将内容保存到 {targetDir} 目录下。
只能操作 {targetDir} 目录下的文件，不能操作其他目录下的文件。
`,
  refinePrompt: `用户初始描述：{description}
请读取 {targetDir} 目录下的文件章节内容，在此基础上结合用户的输入进一步细化和完善，输出更详细的版本。可以适当的删减、增加和修改章节和章节的内容，使教程更完美，章节所覆盖的内容更完善。
直接输出最终内容，并将内容保存到 {targetDir}目录下。
每次先检查章节，看有没有可以增加和删除、修改的章节，如果有，可以对章节进行操作。
只能操作 {targetDir} 目录下的文件，不能操作其他目录下的文件。`,
};

export default config;
