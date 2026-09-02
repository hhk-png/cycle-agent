/**
 * provider/openai.ts —— OpenAI 兼容接口适配器
 *
 * 通过标准 fetch 实现 /v1/chat/completions 调用,支持工具调用与流式。
 * 因为只依赖 HTTP,任何 OpenAI 兼容服务都能接:
 *   - OpenAI 官方
 *   - 本地 Ollama / vLLM / LM Studio / llama.cpp server
 *   - DeepSeek、Moonshot、通义等国内厂商(均提供 OpenAI 兼容端点)
 *
 * 未在离线验证范围内(需要 API Key / 本地服务),但这是 MiniAgent
 * 连接真实大模型的标准路径。MockProvider 已验证了 Agent 的完整逻辑,
 * 把 provider 换成 OpenAIProvider,行为等价。
 */

import type { Completion, CompleteOptions, Message, StreamEvent, ToolCall } from '../types.ts';

export interface OpenAIProviderOptions {
  /** API Key。也可以从环境变量 OPENAI_API_KEY 读取。 */
  apiKey?: string;
  /** 端点基地址,默认 https://api.openai.com/v1 */
  baseURL?: string;
  /** 模型名,默认 gpt-4o-mini */
  model?: string;
  /** 代理或自定义 fetch(便于测试注入)。 */
  fetch?: typeof fetch;
}

interface OpenAIMessage {
  role: string;
  content: string | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: Array<{ id: string; type: 'function'; function: { name: string; arguments: string } }>;
}

interface OpenAIResponse {
  choices: Array<{
    message: { content: string | null; tool_calls?: Array<{ id: string; function: { name: string; arguments: string } }> };
    finish_reason: 'stop' | 'tool_calls' | 'length' | 'content_filter' | null;
  }>;
  usage?: { prompt_tokens: number; completion_tokens: number };
}

export class OpenAIProvider {
  readonly id: string;
  private apiKey: string;
  private baseURL: string;
  private model: string;
  private fetcher: typeof fetch;

  constructor(opts: OpenAIProviderOptions = {}) {
    this.apiKey = opts.apiKey ?? process.env.OPENAI_API_KEY ?? '';
    this.baseURL = (opts.baseURL ?? process.env.OPENAI_BASE_URL ?? 'https://api.openai.com/v1').replace(/\/+$/, '');
    this.model = opts.model ?? 'gpt-4o-mini';
    this.fetcher = opts.fetch ?? fetch;
    this.id = `openai:${this.model}`;
  }

  private buildMessages(messages: Message[]): OpenAIMessage[] {
    return messages.map((m) => {
      if (m.role === 'tool') {
        return {
          role: 'tool',
          tool_call_id: m.toolCallId ?? '',
          content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
        };
      }
      if (m.role === 'assistant') {
        const out: OpenAIMessage = {
          role: 'assistant',
          content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
        };
        if (m.toolCalls && m.toolCalls.length > 0) {
          out.tool_calls = m.toolCalls.map((tc: ToolCall) => ({
            id: tc.id,
            type: 'function' as const,
            function: { name: tc.name, arguments: JSON.stringify(tc.arguments) },
          }));
        }
        return out;
      }
      return {
        role: m.role,
        content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
      };
    });
  }

  private buildTools(tools?: import('../types.ts').ToolDefinition[]) {
    if (!tools || tools.length === 0) return undefined;
    return tools.map((t) => ({
      type: 'function' as const,
      function: { name: t.name, description: t.description, parameters: t.inputSchema },
    }));
  }

  private parseToolCalls(raw?: Array<{ id: string; function: { name: string; arguments: string } }>): ToolCall[] {
    if (!raw) return [];
    return raw.map((tc) => {
      let args: Record<string, unknown> = {};
      try {
        args = JSON.parse(tc.function.arguments || '{}');
      } catch {
        args = { _parseError: tc.function.arguments };
      }
      return { id: tc.id, name: tc.function.name, arguments: args };
    });
  }

  async complete(messages: Message[], opts?: CompleteOptions): Promise<Completion> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages: this.buildMessages(messages),
      temperature: opts?.temperature ?? 0.7,
      max_tokens: opts?.maxTokens,
      stream: false,
    };
    const tools = this.buildTools(opts?.tools);
    if (tools) body.tools = tools;

    const res = await this.fetcher(`${this.baseURL}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: opts?.signal,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`OpenAIProvider HTTP ${res.status}: ${text.slice(0, 300)}`);
    }

    const data = (await res.json()) as OpenAIResponse;
    const choice = data.choices[0];
    const toolCalls = this.parseToolCalls(choice?.message?.tool_calls);
    return {
      content: choice?.message?.content ?? '',
      toolCalls,
      finishReason: (choice?.finish_reason ?? 'stop') as Completion['finishReason'],
      usage: data.usage
        ? { promptTokens: data.usage.prompt_tokens, completionTokens: data.usage.completion_tokens }
        : undefined,
    };
  }

  async *stream(messages: Message[], opts?: CompleteOptions): AsyncIterable<StreamEvent> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages: this.buildMessages(messages),
      temperature: opts?.temperature ?? 0.7,
      max_tokens: opts?.maxTokens,
      stream: true,
    };
    const tools = this.buildTools(opts?.tools);
    if (tools) body.tools = tools;

    const res = await this.fetcher(`${this.baseURL}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: opts?.signal,
    });

    if (!res.ok || !res.body) {
      throw new Error(`OpenAIProvider stream HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const acc: Array<{ index: number; id: string; name: string; args: string }> = [];
    let content = '';
    let finishReason: Completion['finishReason'] = 'stop';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;
          const payload = trimmed.slice(5).trim();
          if (payload === '[DONE]') continue;
          try {
            const chunk = JSON.parse(payload);
            const delta = chunk?.choices?.[0]?.delta;
            const reason = chunk?.choices?.[0]?.finish_reason;
            if (reason) finishReason = reason as Completion['finishReason'];
            if (!delta) continue;

            if (typeof delta.content === 'string' && delta.content.length > 0) {
              content += delta.content;
              yield { type: 'text_delta', delta: delta.content };
            }
            if (delta.tool_calls) {
              for (const tc of delta.tool_calls) {
                const idx = tc.index ?? 0;
                while (acc.length <= idx) acc.push({ index: idx, id: '', name: '', args: '' });
                if (tc.id) acc[idx].id = tc.id;
                if (tc.function?.name) acc[idx].name = tc.function.name;
                if (tc.function?.arguments) acc[idx].args += tc.function.arguments;
              }
            }
          } catch {
            // 忽略解析失败的 chunk(SSE 注释、keep-alive 等)
          }
        }
      }
    } finally {
      reader.releaseLock();
    }

    const toolCalls = acc.map((a) => {
      let args: Record<string, unknown> = {};
      try {
        args = JSON.parse(a.args || '{}');
      } catch {
        args = { _parseError: a.args };
      }
      return { id: a.id || `call_${Math.random().toString(36).slice(2)}`, name: a.name, arguments: args };
    });

    yield { type: 'done', completion: { content, toolCalls, finishReason } };
  }
}
