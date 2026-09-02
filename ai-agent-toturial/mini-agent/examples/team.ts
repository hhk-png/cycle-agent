/**
 * team.ts —— 多智能体协作演示(编排者-工作者)
 *
 * 运行:node examples/team.ts
 *
 * 一个「总监」把任务拆解成步骤,派给「研究员」和「写手」,
 * 最后汇总成最终答复。所有交互通过 MessageBus(黑板)记录。
 *
 * 为了让演示离线且确定,每个成员内部都是一个带 MockProvider 的 Agent;
 * 换成真实 provider,团队逻辑一行不用改。
 */

import { Agent } from '../src/agent.ts';
import { MockProvider, type MockResponder } from '../src/provider/mock.ts';
import { ToolRegistry } from '../src/tools/registry.ts';
import { createBuiltinTools } from '../src/tools/builtin.ts';
import { Orchestrator, RoleAgent, type TeamMember } from '../src/multiagent/team.ts';
import type { Message } from '../src/types.ts';

/** 研究员:返回以任务为主题的摘要。 */
function makeResearcher(): RoleAgent {
  const responder: MockResponder = (messages: Message[]) => {
    const last = [...messages].reverse().find((m) => m.role === 'user');
    const task = typeof last?.content === 'string' ? last.content : '';
    return {
      text: `[研究] 针对「${task.slice(0, 40)}」,关键事实:1) Agent 市场规模快速增长;2) 工具调用与记忆是两大技术支柱。`,
    };
  };
  const agent = new Agent({
    name: 'researcher',
    provider: new MockProvider({ responder, autoMath: false }),
    registry: new ToolRegistry().registerMany(createBuiltinTools()),
    systemPrompt: '你是研究员,只输出研究要点。',
  });
  return new RoleAgent(agent, 'researcher');
}

/** 写手:根据给定内容输出成稿。 */
function makeWriter(): RoleAgent {
  const responder: MockResponder = (messages: Message[]) => {
    const last = [...messages].reverse().find((m) => m.role === 'user');
    const task = typeof last?.content === 'string' ? last.content : '';
    return { text: `[成稿] 根据研究「${task.slice(0, 40)}」,形成 3 句总结:第一,领域在快速演进;第二,基础设施趋于成熟;第三,落地需关注评测与安全。` };
  };
  const agent = new Agent({
    name: 'writer',
    provider: new MockProvider({ responder, autoMath: false }),
    registry: new ToolRegistry().registerMany(createBuiltinTools()),
    systemPrompt: '你是写手,把研究内容整理成简洁的成稿。',
  });
  return new RoleAgent(agent, 'writer');
}

/** 总监:汇总 worker 输出为最终答复(纯函数式成员,不依赖模型)。 */
function makeDirector(): TeamMember {
  return {
    name: 'director',
    role: 'director',
    async handle(task: string) {
      return `【最终答复】\n${task}\n\n(本答复由 3 名成员协作产生)`;
    },
  };
}

async function main(): Promise<void> {
  const orchestrator = new Orchestrator(makeDirector(), [makeResearcher(), makeWriter()], {
    assign: 'round-robin',
  });

  const task = '调研 AI Agent 的现状。写一份 3 句总结。';
  console.log(`任务:${task}\n`);
  const result = await orchestrator.run(task);

  console.log('=== 计划 ===');
  for (const s of result.plan) console.log(`  - ${s.status} ${s.description}`);

  console.log('\n=== 分派与产出 ===');
  for (const w of result.workerOutputs) {
    console.log(`  [${w.worker}] 「${w.step}」\n    ${w.output}`);
  }

  console.log(`\n=== 最终答复 ===\n${result.final}`);
  console.log(`\n消息总线历史:${result.busHistoryCount} 条(黑板)`);
}

void main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
