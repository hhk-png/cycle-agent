import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Agent } from '../src/agent.ts';
import { MockProvider, type MockResponder } from '../src/provider/mock.ts';
import { ToolRegistry } from '../src/tools/registry.ts';
import { createBuiltinTools } from '../src/tools/builtin.ts';
import type { Message } from '../src/types.ts';

function makeAgent(provider: MockProvider, opts: Partial<ConstructorParameters<typeof Agent>[0]> = {}): Agent {
  const registry = new ToolRegistry().registerMany(createBuiltinTools());
  return new Agent({ provider, registry, name: 'tester', ...opts });
}

test('完整循环:调用工具 → 观察 → 最终回答', async () => {
  const provider = new MockProvider({
    steps: [
      { text: '让我计算 2+3', toolCalls: [{ name: 'calculator', arguments: { expression: '2+3' } }] },
      (messages: Message[]) => {
        const lastTool = [...messages].reverse().find((m) => m.role === 'tool');
        return { text: `结果是 ${lastTool?.content}` };
      },
    ],
  });
  const agent = makeAgent(provider);
  const result = await agent.run('请计算 2+3');

  assert.equal(result.stats.stopReason, 'end_turn');
  assert.equal(result.stats.llmCalls, 2);
  assert.equal(result.stats.toolCalls, 1);
  assert.match(result.output, /结果是 5/);
  // 消息历史包含工具往返
  assert.ok(result.messages.some((m) => m.role === 'tool' && m.name === 'calculator'));
  assert.ok(result.messages.some((m) => m.role === 'assistant' && m.toolCalls && m.toolCalls.length > 0));
});

test('工具错误自愈:模型看到错误后修正', async () => {
  const responder: MockResponder = (messages: Message[]) => {
    const lastTool = [...messages].reverse().find((m) => m.role === 'tool');
    if (!lastTool) return { text: '试一下', toolCalls: [{ name: 'calculator', arguments: { expression: '1/0' } }] };
    const content = typeof lastTool.content === 'string' ? lastTool.content : JSON.stringify(lastTool.content);
    if (content.includes('错误') || content.includes('失败') || content.includes('除以零')) {
      return { text: '我修正为', toolCalls: [{ name: 'calculator', arguments: { expression: '10/2' } }] };
    }
    return { text: `答案 ${content}` };
  };
  const agent = makeAgent(new MockProvider({ responder }));
  const result = await agent.run('10/2 是多少');

  assert.equal(result.stats.toolCalls, 2);
  assert.equal(result.stats.toolErrors, 1);
  assert.match(result.output, /答案 5/);
});

test('迭代上限兜底', async () => {
  const provider = new MockProvider({
    responder: () => ({ text: '再试一次', toolCalls: [{ name: 'echo', arguments: { text: 'x' } }] }),
  });
  const agent = makeAgent(provider, { maxIterations: 3 });
  const result = await agent.run('不要停');
  assert.equal(result.stats.stopReason, 'max_iterations');
  assert.equal(result.stats.iterations, 3);
});

test('事件流:tool_call / tool_result / text / stop 都会触发', async () => {
  const provider = new MockProvider({
    steps: [
      { text: '算一下', toolCalls: [{ name: 'calculator', arguments: { expression: '1+1' } }] },
      { text: '答案是 2', finishReason: 'stop' },
    ],
  });
  const agent = makeAgent(provider);
  const seen: string[] = [];
  agent.on('tool_call', () => seen.push('tool_call'));
  agent.on('tool_result', () => seen.push('tool_result'));
  agent.on('text', () => seen.push('text'));
  agent.on('stop', () => seen.push('stop'));

  await agent.run('1+1');
  assert.deepEqual(seen, ['text', 'tool_call', 'tool_result', 'text', 'stop']);
});

test('记忆:运行后沉淀 user 与 assistant 消息', async () => {
  const provider = new MockProvider({ steps: [{ text: '你好', finishReason: 'stop' }] });
  const agent = makeAgent(provider);
  await agent.run('早上好');
  const recent = agent.memory.recent(10);
  assert.ok(recent.some((m) => m.content === '早上好'));
  assert.ok(recent.some((m) => m.content === '你好'));
});

test('多轮延续:第二轮能看到第一轮的记忆', async () => {
  const provider = new MockProvider({
    steps: [
      { text: '记住了', finishReason: 'stop' },
      (messages: Message[]) => {
        // 第二轮的完整历史里应该带着第一轮的 user 消息(记忆注入)
        const hasMemory = messages.some((m) => typeof m.content === 'string' && m.content.includes('我叫小明'));
        return { text: hasMemory ? '我记得你叫小明' : '我没有你的记忆' };
      },
    ],
  });
  const agent = makeAgent(provider);
  await agent.run('我叫小明');
  const second = await agent.run('我叫什么?');
  assert.match(second.output, /小明/);
});

test('AbortSignal 中断', async () => {
  const provider = new MockProvider({
    responder: () => ({ text: '…', toolCalls: [{ name: 'echo', arguments: { text: 'x' } }] }),
  });
  const agent = makeAgent(provider, { maxIterations: 10 });
  const controller = new AbortController();
  // 第一次迭代后中断
  let ran = 0;
  agent.on('tool_result', () => {
    ran += 1;
    if (ran === 1) controller.abort();
  });
  const result = await agent.run('跑吧', { signal: controller.signal });
  assert.equal(result.stats.stopReason, 'interrupted');
});
