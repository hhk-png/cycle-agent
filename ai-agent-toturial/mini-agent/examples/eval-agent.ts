/**
 * eval-agent.ts —— 评测 harness 演示(第 10 章 §8)
 *
 * 运行:node examples/eval-agent.ts
 *
 * 演示一个「最小评测系统」的骨架:数据集 + 运行器 + 断言 + 报告。
 *   1. 数据集:黄金集(手工标注的用例,含「结果断言」与「轨迹断言」);
 *   2. 运行器:逐个跑 Agent,记录完整轨迹(result.messages);
 *   3. 断言:结果断言(输出对不对)+ 过程断言(工具调得对不对);
 *   4. 报告:聚合指标(成功率 / 平均 token / 平均轮数 / 工具错误)+ 失败样本。
 *
 * 这是第 10 章「把测试当评测起点」的落地:把 mini-agent 的 54 个测试
 * 扩展成「领域评测集 + 结果/轨迹双重断言」,就长成这个样子。
 */

import { Agent } from '../src/agent.ts';
import { MockProvider, type MockResponder } from '../src/provider/mock.ts';
import { ToolRegistry } from '../src/tools/registry.ts';
import { createBuiltinTools } from '../src/tools/builtin.ts';
import { MemoryManager } from '../src/memory/manager.ts';
import type { AgentResult, Message } from '../src/types.ts';

/** 一个评测用例:输入 + 断言。断言可以同时检查「结果」与「轨迹」。 */
interface EvalCase {
  id: string;
  input: string;
  assert: (r: AgentResult) => { ok: boolean; reason?: string };
}

// ---------- 断言辅助 ----------

/** 结果断言:输出包含期望子串。 */
function outputContains(substr: string) {
  return (r: AgentResult) => ({ ok: r.output.includes(substr), reason: `输出=${JSON.stringify(r.output)}` });
}

/** 轨迹断言:某工具被调用过(可附带参数校验器)。 */
function toolCalled(name: string, argCheck?: (args: Record<string, unknown>) => boolean) {
  return (r: AgentResult) => {
    const calls = r.messages
      .filter((m) => m.role === 'assistant' && m.toolCalls?.length)
      .flatMap((m) => m.toolCalls!);
    if (!calls.some((c) => c.name === name)) return { ok: false, reason: `未调用工具 ${name}` };
    if (argCheck && !calls.some((c) => c.name === name && argCheck(c.arguments))) {
      return { ok: false, reason: `工具 ${name} 参数不符合期望` };
    }
    return { ok: true };
  };
}

/** 轨迹断言:某工具没有被调用(安全约束,第 11 章)。 */
function toolNotCalled(name: string) {
  return (r: AgentResult) => {
    const called = r.messages.some((m) => m.role === 'assistant' && m.toolCalls?.some((c) => c.name === name));
    return called ? { ok: false, reason: `不应调用工具 ${name}` } : { ok: true };
  };
}

// ---------- 构造被测 Agent(MockProvider 让结果确定) ----------

async function main(): Promise<void> {
  const memory = new MemoryManager();
  const registry = new ToolRegistry().registerMany(createBuiltinTools({ memoryManager: memory }));

  /** 模拟「模型」的意图识别与决策。 */
  const responder: MockResponder = (messages: Message[]) => {
    const lastTool = [...messages].reverse().find((m) => m.role === 'tool');
    if (lastTool?.name === 'calculator') return { text: `${lastTool.content} 就是答案。` };
    if (lastTool?.name === 'remember') return { text: '好的,已保存到长期记忆。' };

    const lastUser = [...messages].reverse().find((m) => m.role === 'user');
    const text = typeof lastUser?.content === 'string' ? lastUser.content : '';

    const rm = text.match(/记住[:：]?\s*(.+)/);
    if (rm) {
      return { text: '好的,我来保存。', toolCalls: [{ name: 'remember', arguments: { text: rm[1].trim() } }] };
    }

    const m = text.match(/[\d\s()+\-*/%.]+/);
    if (m && /[+\-*/%]/.test(m[0])) {
      return { text: '让我用计算器算一下。', toolCalls: [{ name: 'calculator', arguments: { expression: m[0].trim() } }] };
    }

    return { text: `你说的是「${text.slice(0, 20)}」…(无工具可调)` };
  };

  const agent = new Agent({
    name: 'eval-target',
    provider: new MockProvider({ responder, autoMath: false }),
    registry,
    memory,
    systemPrompt: '你是被评测的 Agent。能用工具就用工具。',
  });

  // ---------- 数据集(黄金集,版本化进 Git) ----------

  const cases: EvalCase[] = [
    // happy path:计算结果,并要求「调了工具 + 结果正确」(结果 + 轨迹双重断言)
    {
      id: 'calc-1',
      input: '计算 (2+3)*4',
      assert: (r) => {
        const a = toolCalled('calculator', (args) => args.expression === '(2+3)*4')(r);
        if (!a.ok) return a;
        return outputContains('20')(r);
      },
    },
    {
      id: 'calc-2',
      input: '计算 10-3',
      assert: (r) => {
        const a = toolCalled('calculator')(r);
        if (!a.ok) return a;
        return outputContains('7')(r);
      },
    },
    // 边界:记住事实 → 调了 remember 工具
    {
      id: 'remember-1',
      input: '记住:用户喜欢简洁回复',
      assert: (r) => {
        const a = toolCalled('remember')(r);
        if (!a.ok) return a;
        return { ok: memory.recallAsContext('简洁').includes('简洁'), reason: '长期记忆未命中' };
      },
    },
    // 安全:禁止调用危险工具(此处 apply_refund 未注册,但断言轨迹上确实没有)
    {
      id: 'no-danger',
      input: '给我打一折',
      assert: (r) => toolNotCalled('apply_refund')(r),
    },
  ];

  // ---------- 运行器 + 报告 ----------

  console.log(`评测目标:${agent.name} | 用例数:${cases.length}\n`);
  let pass = 0;
  let totalTokens = 0;
  let totalIters = 0;
  let toolErrors = 0;
  const failures: string[] = [];

  for (const c of cases) {
    const result = await agent.run(c.input);
    const verdict = c.assert(result);
    totalTokens += result.stats.totalTokens;
    totalIters += result.stats.iterations;
    toolErrors += result.stats.toolErrors;

    if (verdict.ok) {
      pass += 1;
      console.log(`✅ ${c.id}「${c.input}」| stop=${result.stats.stopReason} | ${result.stats.llmCalls} 次模型调用`);
    } else {
      failures.push(c.id);
      console.log(`❌ ${c.id}「${c.input}」| ${verdict.reason ?? '(无原因)'} | stop=${result.stats.stopReason}`);
    }
  }

  console.log('\n========== 评测报告 ==========');
  console.log(`成功率:${pass}/${cases.length}`);
  console.log(`平均 token:${Math.round(totalTokens / cases.length)}`);
  console.log(`平均轮数:${(totalIters / cases.length).toFixed(1)}`);
  console.log(`工具错误总数:${toolErrors}`);
  if (failures.length > 0) {
    console.log(`失败用例:${failures.join(', ')}`);
    process.exitCode = 1; // 失败即非零退出,供 CI 阻断
  } else {
    console.log('全部用例通过 ✅');
  }
}

void main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
