/**
 * math-agent.ts —— 工具错误自愈演示
 *
 * 运行:node examples/math-agent.ts
 *
 * 这是 Agent 循环最经典的「韧性」演示:模型第一次给出错误的表达式,
 * 工具返回错误,模型看到错误后自我修正,第二次调用成功。
 * 真实世界里的模型经常出错,能否从错误中恢复,是 Agent 可靠性的分水岭。
 */

import { Agent } from '../src/agent.ts';
import { MockProvider, type MockResponder } from '../src/provider/mock.ts';
import { ToolRegistry } from '../src/tools/registry.ts';
import { createBuiltinTools } from '../src/tools/builtin.ts';
import type { Message } from '../src/types.ts';

/** 脚本:第一次给非法表达式,第二次给正确表达式,第三次输出答案。 */
const responder: MockResponder = (messages: Message[]) => {
  const lastTool = [...messages].reverse().find((m) => m.role === 'tool');
  if (!lastTool) {
    // 第一步:给一个「坏」表达式
    return { text: '让我算一下 2 + * 3。', toolCalls: [{ name: 'calculator', arguments: { expression: '2 + * 3' } }] };
  }
  if (lastTool.name === 'calculator') {
    const content = typeof lastTool.content === 'string' ? lastTool.content : JSON.stringify(lastTool.content);
    if (content.includes('失败')) {
      // 模型看到了错误,修正表达式
      return { text: '刚才的表达式有误,我修正为 (2+3)*4。', toolCalls: [{ name: 'calculator', arguments: { expression: '(2+3)*4' } }] };
    }
    // 拿到正确结果,给出最终答案
    return { text: `计算结果为 ${content}。` };
  }
  return { text: '完成。' };
};

async function main(): Promise<void> {
  const registry = new ToolRegistry().registerMany(createBuiltinTools());
  const provider = new MockProvider({ responder });

  const agent = new Agent({
    name: 'math-wiz',
    provider,
    registry,
    systemPrompt: '你负责用计算器工具解答数学问题。',
  });

  agent.on('tool_call', (p) => console.log(`  [行动] ${p.call.name}(${JSON.stringify(p.call.arguments)})`));
  agent.on('tool_result', (p) => console.log(`  [观察] 结果:${p.output.content}${p.output.isError ? ' [错误]' : ''}`));
  agent.on('text', (t) => console.log(`  [思考] ${t}`));

  console.log('用户:请计算 (2+3)*4');
  const result = await agent.run('请计算 (2+3)*4');
  console.log(`\n最终回答:${result.output}`);
  console.log(
    `统计:${result.stats.llmCalls} 次模型调用 / ${result.stats.toolCalls} 次工具调用 / ${result.stats.toolErrors} 次工具错误 / ${result.stats.iterations} 轮`,
  );
}

void main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
