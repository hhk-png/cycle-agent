/**
 * telemetry/trace.ts —— 轻量链路追踪
 *
 * 生产级 Agent 必须有可观测性:每次工具调用花了多久、模型调用消耗了多少 token、
 * 循环走到第几步、为什么停止。mini-agent 用一个极简的 Span 树 + JSONL 输出
 * 把「时间」和「因果」记录下来,结构与 OpenTelemetry 的 span 概念对齐,
 * 方便迁移到真正的追踪系统(LangSmith、OTel、自建)。
 */

export type SpanKind = 'agent' | 'llm' | 'tool' | 'memory' | 'plan' | 'internal';

export interface TraceEvent {
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  kind: SpanKind;
  name: string;
  startedAt: number;
  /** -1 表示未结束 */
  durationMs: number;
  status: 'ok' | 'error';
  data: Record<string, unknown>;
}

export interface TracerOptions {
  /** 默认写 console;可换成写文件/上报。 */
  onEvent?: (ev: TraceEvent) => void;
  enabled?: boolean;
}

export class Span {
  readonly traceId: string;
  readonly spanId: string;
  readonly parentSpanId?: string;
  readonly name: string;
  readonly kind: SpanKind;
  readonly startedAt = Date.now();
  data: Record<string, unknown> = {};
  private children: Span[] = [];
  private ended = false;
  private tracer: Tracer;

  constructor(tracer: Tracer, name: string, kind: SpanKind, parentSpanId?: string) {
    this.tracer = tracer;
    this.name = name;
    this.kind = kind;
    this.parentSpanId = parentSpanId;
    this.traceId = tracer.traceId;
    this.spanId = Span.nextId();
  }

  setAttribute(key: string, value: unknown): this {
    this.data[key] = value;
    return this;
  }

  child(name: string, kind: SpanKind): Span {
    const s = new Span(this.tracer, name, kind, this.spanId);
    this.children.push(s);
    return s;
  }

  end(status: 'ok' | 'error' = 'ok'): void {
    if (this.ended) return;
    this.ended = true;
    this.tracer.emit(this, status);
    // 子 span 未结束的,强制收尾
    for (const c of this.children) c.end(c.data._error ? 'error' : 'ok');
  }

  private static seq = 0;
  private static nextId(): string {
    return `span_${Date.now().toString(36)}_${++Span.seq}`;
  }
}

export class Tracer {
  readonly traceId: string;
  private readonly onEvent?: (ev: TraceEvent) => void;
  private readonly enabled: boolean;

  constructor(opts: TracerOptions = {}) {
    this.traceId = `trace_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
    this.onEvent = opts.onEvent;
    this.enabled = opts.enabled ?? true;
  }

  /** 开启一个根 span。 */
  startSpan(name: string, kind: SpanKind = 'agent'): Span {
    return new Span(this, name, kind);
  }

  emit(span: Span, status: 'ok' | 'error'): void {
    if (!this.enabled) return;
    const ev: TraceEvent = {
      traceId: span.traceId,
      spanId: span.spanId,
      parentSpanId: span.parentSpanId,
      kind: span.kind,
      name: span.name,
      startedAt: span.startedAt,
      durationMs: Date.now() - span.startedAt,
      status,
      data: span.data,
    };
    if (this.onEvent) this.onEvent(ev);
    else console.error(`[trace] ${ev.kind} ${ev.name} ${ev.durationMs}ms ${status} ${JSON.stringify(ev.data)}`);
  }
}

/** 生成一个 JSONL 文件写入器,供把 trace 落盘。 */
export function createJsonlTraceWriter(filePath: string) {
  let wrote = 0;
  let queue: Promise<void> = Promise.resolve();
  return {
    onEvent: (ev: TraceEvent) => {
      // 串行追加,避免并发写坏文件
      queue = queue.then(async () => {
        await import('node:fs/promises').then((fs) => fs.appendFile(filePath, JSON.stringify(ev) + '\n'));
        wrote += 1;
      });
    },
    /** 等待所有已入队的写入完成。 */
    flush: () => queue,
    get count() {
      return wrote;
    },
  };
}
