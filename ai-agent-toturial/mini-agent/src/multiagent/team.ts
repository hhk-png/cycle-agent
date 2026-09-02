/**
 * multiagent/team.ts —— 编排者-工作者(Orchestrator-Worker)团队
 *
 * 这是多智能体协作里最常用的拓扑(教程第 07 章会对比各种拓扑):
 *   - leader(编排者):负责拆解任务、分派给 worker、汇总结果;
 *   - workers(工作者):各司其职(研究员/写作/代码…),把子任务结果交回;
 *   - 所有交互通过 MessageBus 通信,历史即黑板。
 *
 * 关键工程点:
 *   - 成员被抽象成 TeamMember 接口,RoleAgent 把一个 Agent 包装成成员;
 *   - 任务分派支持 round-robin(轮流)或按名字定向(如只给 researcher);
 *   - 单点故障隔离:某个 worker 失败不会拖垮整个团队。
 */

import type { Agent } from '../agent.ts';
import { MessageBus } from './bus.ts';
import { Planner, type PlanStep } from '../planner.ts';

/** 团队成员统一接口。 */
export interface TeamMember {
  name: string;
  role: string;
  /** 处理一项任务,返回结果文本。 */
  handle(task: string, ctx: TeamContext): Promise<string>;
}

/** 团队运行时上下文:成员之间协作的通道。 */
export interface TeamContext {
  bus: MessageBus;
  /** 向指定成员派发子任务。 */
  ask(name: string, task: string): Promise<string>;
  /** 广播一条黑板消息。 */
  post(kind: string, payload: unknown): void;
  /** 共享计划(黑板)。 */
  plan: PlanStep[];
}

/** 把一个 Agent 包装成团队成员的适配器。 */
export class RoleAgent implements TeamMember {
  public readonly agent: Agent;
  public readonly role: string;

  constructor(agent: Agent, role: string) {
    this.agent = agent;
    this.role = role;
  }

  get name(): string {
    return this.agent.name;
  }

  /** 暴露底层 Agent 的 provider,供编排者做规划。 */
  get provider(): import('../provider/types.ts').ChatProvider {
    return this.agent.provider;
  }

  async handle(task: string, _ctx: TeamContext): Promise<string> {
    const result = await this.agent.run(task);
    return result.output;
  }
}

export interface OrchestratorOptions {
  planner?: Planner;
  /** worker 选择策略:'round-robin' 或按名字定向。 */
  assign?: 'round-robin' | 'by-role';
}

export interface TeamRunResult {
  plan: PlanStep[];
  workerOutputs: Array<{ worker: string; step: string; output: string }>;
  final: string;
  busHistoryCount: number;
}

/**
 * 编排者。流程:
 *   1. leader 用 planner 把任务拆成步骤;
 *   2. 每个步骤派发给一个 worker,收集输出;
 *   3. leader 汇总所有 worker 的输出,产出最终答案。
 */
export class Orchestrator {
  private readonly leader: TeamMember;
  private readonly workers: TeamMember[];
  private readonly bus = new MessageBus();
  private readonly planner: Planner;
  private readonly assign: OrchestratorOptions['assign'];

  constructor(leader: TeamMember, workers: TeamMember[], opts: OrchestratorOptions = {}) {
    this.leader = leader;
    this.workers = workers;
    this.planner = opts.planner ?? new Planner();
    this.assign = opts.assign ?? 'round-robin';
    this.bus.attach(leader.name);
    for (const w of workers) this.bus.attach(w.name);
  }

  async run(task: string): Promise<TeamRunResult> {
    const ctx: TeamContext = {
      bus: this.bus,
      ask: async (name, subtask) => this.askWorker(name, subtask, ctx),
      post: (kind, payload) => this.bus.broadcast(this.leader.name, kind, payload),
      plan: [],
    };

    // 1. leader 拆解任务(用 leader 的 provider,没有则启发式)
    const plan = await this.planner.createPlan(task, this.leaderProvider());
    ctx.plan = plan;
    this.bus.broadcast(this.leader.name, 'plan', plan);

    // 2. 分派执行(单点失败不拖垮团队:记录 failed 后继续)
    const workerOutputs: TeamRunResult['workerOutputs'] = [];
    for (const [i, step] of plan.entries()) {
      const worker = this.pickWorker(i, step.description);
      this.bus.send(this.leader.name, 'assign', { step, to: worker.name }, worker.name);
      let output: string;
      try {
        output = await this.askWorker(worker.name, step.description, ctx);
        step.status = 'done';
      } catch (err) {
        output = `(worker 失败:${err instanceof Error ? err.message : String(err)})`;
        step.status = 'failed';
      }
      step.result = output;
      workerOutputs.push({ worker: worker.name, step: step.description, output });
      this.bus.broadcast(this.leader.name, 'step_done', { step, output });
    }

    // 3. leader 汇总
    const summaryInput =
      plan.map((s) => `- ${s.description}\n  结果:${s.result ?? '(无)'}`).join('\n');
    const final = await this.leader.handle(`请根据以下已完成计划汇总成一份最终答复:\n${summaryInput}`, ctx);

    return {
      plan,
      workerOutputs,
      final,
      busHistoryCount: this.bus.history().length,
    };
  }

  private pickWorker(index: number, step: string): TeamMember {
    if (this.assign === 'by-role') {
      const wanted = this.workers.find((w) => step.toLowerCase().includes(w.role.toLowerCase()));
      if (wanted) return wanted;
    }
    return this.workers[index % this.workers.length]!;
  }

  private async askWorker(name: string, task: string, ctx: TeamContext): Promise<string> {
    const worker = this.workers.find((w) => w.name === name) ?? this.workers[0]!;
    return worker.handle(task, ctx);
  }

  /** leader 的 provider(若是 RoleAgent 包装的 Agent,则用其 provider 做规划;否则启发式)。 */
  private leaderProvider(): import('../provider/types.ts').ChatProvider | undefined {
    if (this.leader instanceof RoleAgent) return this.leader.provider;
    return undefined;
  }
}
