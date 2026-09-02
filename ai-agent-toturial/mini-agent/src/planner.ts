/**
 * planner.ts —— 计划与任务分解
 *
 * 「先计划,再执行」是 Agent 从玩具走向可靠的分水岭。Planner 负责把
 * 一个开放目标拆成若干可执行的步骤。
 *
 * 两种策略:
 *   1. provider 驱动:让模型输出 JSON 步骤数组(真实场景);
 *   2. 启发式兜底:按句子切分(离线、无模型也能工作)。
 *
 * 计划不是一次性的:执行过程中步骤可能失败,Agent 需要「重新计划」。
 * Planner 提供的 replan 接口留给编排层调用。
 */

import type { ChatProvider } from './provider/types.ts';
import type { Message } from './types.ts';

export type PlanStepStatus = 'pending' | 'in_progress' | 'done' | 'failed' | 'skipped';

export interface PlanStep {
  id: string;
  description: string;
  status: PlanStepStatus;
  result?: string;
}

export interface PlannerOptions {
  maxSteps?: number;
}

const PLAN_SYSTEM_PROMPT = [
  '你是一个任务规划器。',
  '把用户目标拆解为若干个有序步骤,每个步骤用一句简短的话描述。',
  '只输出 JSON 数组,不要输出任何其他文字。',
  '格式:[{"step": "步骤描述"}, ...]',
].join(' ');

export class Planner {
  private readonly maxSteps: number;
  private seq = 0;

  constructor(opts: PlannerOptions = {}) {
    this.maxSteps = opts.maxSteps ?? 6;
  }

  /** 生成计划。传 provider 则用模型驱动,否则用启发式切句。 */
  async createPlan(goal: string, provider?: ChatProvider): Promise<PlanStep[]> {
    let steps: PlanStep[];
    if (provider) {
      steps = await this.planWithProvider(goal, provider);
    } else {
      steps = this.planHeuristic(goal);
    }
    return steps.slice(0, this.maxSteps);
  }

  /** 把已完成/失败的计划折叠成一段文字,供执行器参考。 */
  static summarize(plan: PlanStep[]): string {
    if (plan.length === 0) return '(无计划)';
    return plan
      .map((s) => `${s.status === 'done' ? '[x]' : '[ ]'} ${s.description}${s.result ? ` → ${s.result}` : ''}`)
      .join('\n');
  }

  private async planWithProvider(goal: string, provider: ChatProvider): Promise<PlanStep[]> {
    const messages: Message[] = [
      { role: 'system', content: PLAN_SYSTEM_PROMPT },
      { role: 'user', content: goal },
    ];
    const completion = await provider.complete(messages, { temperature: 0 });
    const parsed = extractJsonArray(completion.content);
    if (!parsed) {
      // 模型没输出合法 JSON,退化为启发式
      return this.planHeuristic(goal);
    }
    return parsed.map((item: unknown) => {
      const desc = typeof item === 'string' ? item : (item as { step?: string }).step ?? String(item);
      return this.makeStep(desc);
    });
  }

  /** 启发式:按中文/英文标点切句,每句一步。 */
  private planHeuristic(goal: string): PlanStep[] {
    const sentences = goal
      .split(/[。！？.!?\n]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (sentences.length === 0) return [this.makeStep(goal)];
    return sentences.map((s) => this.makeStep(s));
  }

  private makeStep(description: string): PlanStep {
    return { id: `step_${++this.seq}`, description, status: 'pending' };
  }
}

/** 从模型输出里提取 JSON 数组(容忍```json 代码块包裹)。 */
function extractJsonArray(content: string): unknown[] | null {
  if (!content) return null;
  let text = content.trim();
  // 去掉 ```json ... ``` 包裹
  const fenceMatch = text.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fenceMatch) text = fenceMatch[1]!.trim();
  try {
    const parsed = JSON.parse(text);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    // 尝试找第一个 [ 到最后一个 ]
    const start = text.indexOf('[');
    const end = text.lastIndexOf(']');
    if (start >= 0 && end > start) {
      try {
        const parsed = JSON.parse(text.slice(start, end + 1));
        return Array.isArray(parsed) ? parsed : null;
      } catch {
        return null;
      }
    }
    return null;
  }
}
