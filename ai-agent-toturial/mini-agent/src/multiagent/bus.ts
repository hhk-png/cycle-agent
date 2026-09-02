/**
 * multiagent/bus.ts —— 智能体消息总线
 *
 * 多智能体协作的第一性问题是「通信」。mini-agent 用两种经典模式的组合:
 *   - 点对点消息(direct message):A 明确把任务发给 B;
 *   - 黑板(blackboard):所有消息落进历史,任何成员可读。
 *
 * 实现上是一个「邮箱 + 历史」模型:
 *   send()      → 投递到目标成员的邮箱(一对一);
 *   broadcast() → 投递到所有成员邮箱 + 历史(发布订阅);
 *   history()   → 全部消息(黑板,共享工作记忆)。
 *
 * 真实系统会把 Bus 换成消息队列(Kafka/RabbitMQ/NATS)或 Agent 间 HTTP,
 * 但「邮箱 + 历史」的语义完全可以平移过去。
 */

export interface BusMessage {
  id: string;
  from: string;
  to?: string;
  kind: string;
  payload: unknown;
  timestamp: number;
}

class Mailbox {
  private queue: BusMessage[] = [];
  post(msg: BusMessage): void {
    this.queue.push(msg);
  }
  take(): BusMessage[] {
    const all = this.queue;
    this.queue = [];
    return all;
  }
  get pendingCount(): number {
    return this.queue.length;
  }
}

export class MessageBus {
  private mailboxes = new Map<string, Mailbox>();
  private historyLog: BusMessage[] = [];
  private seq = 0;

  /** 注册(或取回)一个成员的邮箱。 */
  attach(name: string): void {
    if (!this.mailboxes.has(name)) this.mailboxes.set(name, new Mailbox());
  }

  /** 投递点对点消息到目标邮箱。 */
  send(from: string, kind: string, payload: unknown, to: string): BusMessage {
    const msg: BusMessage = { id: `m_${++this.seq}`, from, to, kind, payload, timestamp: Date.now() };
    const box = this.mailboxes.get(to);
    if (box) box.post(msg);
    this.historyLog.push(msg);
    return msg;
  }

  /** 广播给所有已注册成员。 */
  broadcast(from: string, kind: string, payload: unknown): BusMessage[] {
    const msg: BusMessage = { id: `m_${++this.seq}`, from, kind, payload, timestamp: Date.now() };
    const sent: BusMessage[] = [];
    for (const box of this.mailboxes.values()) {
      box.post(msg);
      sent.push(msg);
    }
    this.historyLog.push(msg);
    return sent;
  }

  /** 取走某个成员邮箱里的所有未读消息。 */
  receive(name: string): BusMessage[] {
    const box = this.mailboxes.get(name);
    return box ? box.take() : [];
  }

  /** 黑板:全部历史消息。 */
  history(): BusMessage[] {
    return this.historyLog.slice();
  }

  /** 黑板:按 kind 过滤的历史。 */
  historyByKind(kind: string): BusMessage[] {
    return this.historyLog.filter((m) => m.kind === kind);
  }

  clear(): void {
    this.historyLog = [];
    this.mailboxes.clear();
  }
}
