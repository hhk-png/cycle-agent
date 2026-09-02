import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Planner } from '../src/planner.ts';
import { MockProvider } from '../src/provider/mock.ts';

test('启发式规划:按句子切分', async () => {
  const planner = new Planner();
  const plan = await planner.createPlan('调研市场。分析竞品。写报告。');
  assert.equal(plan.length, 3);
  assert.ok(plan[0]!.description.includes('调研市场'));
  assert.equal(plan[0]!.status, 'pending');
});

test('启发式规划:单句目标成为单步', async () => {
  const planner = new Planner();
  const plan = await planner.createPlan('写一首诗');
  assert.equal(plan.length, 1);
});

test('模型驱动规划:解析 JSON 数组', async () => {
  const provider = new MockProvider({
    steps: [
      {
        text: '[{"step": "第一步"}, {"step": "第二步"}]',
        finishReason: 'stop',
      },
    ],
  });
  const planner = new Planner();
  const plan = await planner.createPlan('任何目标', provider);
  assert.equal(plan.length, 2);
  assert.equal(plan[0]!.description, '第一步');
});

test('模型输出带代码块也能解析', async () => {
  const provider = new MockProvider({
    steps: [{ text: '```json\n[{"step": "分析"}, {"step": "结论"}]\n```' }],
  });
  const planner = new Planner();
  const plan = await planner.createPlan('目标', provider);
  assert.equal(plan.length, 2);
});

test('模型输出非法 JSON 时退化为启发式', async () => {
  const provider = new MockProvider({ steps: [{ text: '抱歉,我无法理解' }] });
  const planner = new Planner();
  const plan = await planner.createPlan('调研市场。写报告。', provider);
  assert.equal(plan.length, 2);
});

test('summarize 渲染计划', () => {
  const planner = new Planner();
  const plan = [{ id: 's1', description: 'a', status: 'done' as const, result: 'x' }];
  const s = Planner.summarize(plan);
  assert.match(s, /\[x\] a/);
});
