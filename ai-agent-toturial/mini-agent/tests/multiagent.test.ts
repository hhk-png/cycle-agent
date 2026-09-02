import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Agent } from '../src/agent.ts';
import { MockProvider, type MockResponder } from '../src/provider/mock.ts';
import { ToolRegistry } from '../src/tools/registry.ts';
import { createBuiltinTools } from '../src/tools/builtin.ts';
import { MessageBus } from '../src/multiagent/bus.ts';
import { Orchestrator, RoleAgent, type TeamMember } from '../src/multiagent/team.ts';
import type { Message } from '../src/types.ts';

function member(name: string, replyPrefix: string): RoleAgent {
  const responder: MockResponder = (messages: Message[]) => {
    const last = [...messages].reverse().find((m) => m.role === 'user');
    return { text: `${replyPrefix}:${typeof last?.content === 'string' ? last.content.slice(0, 20) : ''}` };
  };
  const agent = new Agent({
    name,
    provider: new MockProvider({ responder, autoMath: false }),
    registry: new ToolRegistry().registerMany(createBuiltinTools()),
  });
  return new RoleAgent(agent, name);
}

const director: TeamMember = {
  name: 'director',
  role: 'director',
  async handle(task: string) {
    return `汇总结果:\n${task}`;
  },
};

test('MessageBus:点对点 + 黑板', () => {
  const bus = new MessageBus();
  bus.attach('a');
  bus.attach('b');
  bus.send('a', 'task', { x: 1 }, 'b');
  bus.broadcast('a', 'notice', 'hi');

  const bMsgs = bus.receive('b');
  assert.equal(bMsgs.length, 2); // 一条直发 + 一条广播
  assert.equal(bus.history().length, 2);
  assert.equal(bus.historyByKind('task').length, 1);
});

test('编排者-工作者:拆解、分派、汇总、黑板记录', async () => {
  const orch = new Orchestrator(director, [member('researcher', '研究'), member('writer', '写作')], {
    assign: 'round-robin',
  });
  const result = await orch.run('调研市场。写报告。');

  assert.ok(result.plan.length >= 2);
  assert.equal(result.workerOutputs.length, result.plan.length);
  assert.match(result.final, /汇总结果/);
  assert.ok(result.busHistoryCount >= result.plan.length + 1); // plan 广播 + 每步一条
  // 每个 worker 都被派过活
  assert.ok(result.workerOutputs.some((w) => w.worker === 'researcher'));
  assert.ok(result.workerOutputs.some((w) => w.worker === 'writer'));
});

test('单个 worker 失败不影响团队汇总', async () => {
  const flaky: TeamMember = {
    name: 'flaky',
    role: 'flaky',
    async handle() {
      throw new Error('worker 崩了');
    },
  };
  const ok: TeamMember = {
    name: 'ok',
    role: 'ok',
    async handle(task: string) {
      return `OK:${task}`;
    },
  };
  const orch = new Orchestrator(director, [flaky, ok]);
  const result = await orch.run('第一步。第二步。');
  // 编排层不把 worker 的异常变成团队崩溃,而是记录失败后继续
  assert.ok(result.final.length > 0);
});
