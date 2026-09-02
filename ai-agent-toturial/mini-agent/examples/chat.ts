/**
 * chat.ts —— 多轮对话演示(工具调用 + 长期记忆 + 上下文)
 *
 * 运行:node examples/chat.ts
 *
 * 演示内容:
 *   1. Agent 主循环(思考→行动→观察);
 *   2. 工具调用:从自然语言里识别「数学表达式 / 记住事实」意图并调用工具;
 *   3. 长期记忆:remember 工具把事实写入向量记忆,后续查询命中;
 *   4. 多轮对话:MemoryManager 保留上下文。
 *
 * 注意:这里的 MockProvider 用「规则 + 意图识别」模拟模型行为;
 * 换成真实大模型 provider,同样的 Agent 代码会由模型自主决策调用哪些工具。
 */

import { Agent } from '../src/agent.ts';
import { MockProvider, type MockResponder } from '../src/provider/mock.ts';
import { ToolRegistry } from '../src/tools/registry.ts';
import { createBuiltinTools } from '../src/tools/builtin.ts';
import { MemoryManager } from '../src/memory/manager.ts';
import { Tracer } from '../src/telemetry/trace.ts';
import type { Message } from '../src/types.ts';

/** 从自然语言里抽取一个「看起来是数学表达式」的子串。 */
function extractMath(text: string): string | null {
  const m = text.match(/[\d\s()+\-*/%.]+/);
  if (!m) return null;
  const cand = m[0].trim();
  if (!cand || !/[+\-*/%]/.test(cand)) return null;
  return cand;
}

function divider(title: string): void {
  console.log(`\n========== ${title} ==========`);
}

async function main(): Promise<void> {
  const memory = new MemoryManager();
  const registry = new ToolRegistry().registerMany(createBuiltinTools({ memoryManager: memory }));
  const tracer = new Tracer({ onEvent: (ev) => console.log(`  [trace] ${ev.kind} ${ev.name} ${ev.durationMs}ms ${ev.status}`) });

  /** 模拟「模型」的意图识别与决策。 */
  const responder: MockResponder = (messages: Message[]) => {
    const lastTool = [...messages].reverse().find((m) => m.role === 'tool');
    // 先处理「上一个工具的观察结果」
    if (lastTool?.name === 'calculator') {
      return { text: `${lastTool.content} 就是答案。` };
    }
    if (lastTool?.name === 'remember') {
      return { text: '好的,我已经把这条事实保存在长期记忆里了。' };
    }

    const lastUser = [...messages].reverse().find((m) => m.role === 'user');
    const text = typeof lastUser?.content === 'string' ? lastUser.content : '';

    // 意图:记住事实
    const rememberMatch = text.match(/记住[:：]?\s*(.+)/);
    if (rememberMatch) {
      return { text: '好的,我把这句话存进长期记忆。', toolCalls: [{ name: 'remember', arguments: { text: rememberMatch[1].trim() } }] };
    }

    // 意图:计算
    const expr = extractMath(text);
    if (expr) {
      return { text: '让我用计算器算一下。', toolCalls: [{ name: 'calculator', arguments: { expression: expr } }] };
    }

    // 意图:查询长期记忆
    if (/(生日|我叫什么|还记得我)/.test(text)) {
      const recall = memory.recallAsContext(text);
      return { text: recall ? `根据我的长期记忆:${recall}` : '我暂时没有相关的长期记忆。' };
    }

    // 兜底
    return { text: `你说的是「${text.slice(0, 20)}」… 我是零依赖的演示 Agent。` };
  };

  const agent = new Agent({
    name: 'chatty',
    provider: new MockProvider({ responder, autoMath: false }),
    registry,
    memory,
    tracer,
    systemPrompt: '你是乐于助人的助手。能用工具就用工具。回答要简洁。',
  });

  agent.on('tool_call', (p) => console.log(`  [行动] ${p.call.name}(${JSON.stringify(p.call.arguments)})`));
  agent.on('tool_result', (p) => console.log(`  [观察] ${p.call.name} 返回:${p.output.content}`));

  // ---- 第一轮:数学 ----
  divider('第一轮:计算 (2+3)*4');
  const r1 = await agent.run('请计算 (2+3)*4');
  console.log(`回答:${r1.output}`);
  console.log(`统计:${r1.stats.llmCalls} 次模型调用 / ${r1.stats.toolCalls} 次工具调用 / ${r1.stats.iterations} 轮`);

  // ---- 第二轮:记住一个事实 ----
  divider('第二轮:记住一个事实');
  const r2 = await agent.run('记住:我的生日是 1998-03-12');
  console.log(`回答:${r2.output}`);

  // ---- 第三轮:检索记忆 ----
  divider('第三轮:查询记忆');
  console.log(`  [记忆] 向量检索命中:${memory.recallAsContext('我的生日') || '(无)'}`);
  const r3 = await agent.run('我的生日是什么时候?');
  console.log(`回答:${r3.output}`);

  // ---- 第四轮:再次工具调用(多轮延续) ----
  divider('第四轮:再来一个计算');
  const r4 = await agent.run('请计算 10-3');
  console.log(`回答:${r4.output}`);

  console.log(`\n长期记忆条数:${memory.factCount}`);
}

void main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
