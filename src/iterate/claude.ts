import spawn from 'cross-spawn';
import type { ClaudeResult, ClaudeHooks } from '../shared/types.ts';

function normalizeFlags(flags: string[], stream: boolean): string[] {
  const out: string[] = [];
  for (let i = 0; i < flags.length; i++) {
    if (flags[i] === '--output-format') {
      i += 1;
      continue;
    }
    if (flags[i].startsWith('--output-format=')) continue;
    out.push(flags[i]);
  }
  if (stream) {
    if (!out.includes('--verbose')) out.push('--verbose');
    out.push('--output-format', 'stream-json', '--include-partial-messages');
  } else {
    out.push('--output-format', 'text');
  }
  return out;
}

const TOOL_HINT_KEYS = ['file_path', 'path', 'command', 'pattern', 'url', 'prompt'];

function toolHint(json: string): string {
  try {
    const input = JSON.parse(json) as Record<string, unknown>;
    for (const key of TOOL_HINT_KEYS) {
      const value = input[key];
      if (typeof value === 'string' && value) return ` ${value.replace(/\s+/g, ' ').slice(0, 70)}`;
    }
  } catch {}
  return '';
}

function createStreamParser(onText: (text: string) => void) {
  let buffer = '';
  let text = '';
  let pending: { index: number; name: string; json: string } | null = null;

  const emit = (chunk: string): void => {
    text += chunk;
    onText(chunk);
  };

  const handle = (raw: string): void => {
    const line = raw.trim();
    if (!line) return;

    let msg: Record<string, any>;
    try {
      msg = JSON.parse(line) as Record<string, any>;
    } catch {
      emit(`${raw}\n`);
      return;
    }

    if (msg.type === 'result') {
      if (!text && typeof msg.result === 'string') text = msg.result;
      return;
    }
    if (msg.type !== 'stream_event') return;

    const ev = msg.event ?? {};
    if (ev.type === 'content_block_start' && ev.content_block?.type === 'tool_use') {
      pending = { index: ev.index, name: ev.content_block.name ?? 'tool', json: '' };
    } else if (ev.type === 'content_block_delta') {
      if (ev.delta?.type === 'text_delta') {
        emit(ev.delta.text ?? '');
      } else if (ev.delta?.type === 'input_json_delta' && pending && ev.index === pending.index) {
        pending.json += ev.delta.partial_json ?? '';
      }
    } else if (ev.type === 'content_block_stop' && pending && ev.index === pending.index) {
      emit(`\n→ ${pending.name}${toolHint(pending.json)}\n`);
      pending = null;
    }
  };

  return {
    push(chunk: string): void {
      buffer += chunk;
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) handle(line);
    },
    flush(): void {
      const rest = buffer;
      buffer = '';
      if (rest.trim()) handle(rest);
    },
    output(): string {
      return text;
    },
  };
}

function createPassthrough(onText: (text: string) => void) {
  let text = '';
  return {
    push(chunk: string): void {
      text += chunk;
      onText(chunk);
    },
    flush(): void {},
    output(): string {
      return text;
    },
  };
}

export function runClaude(
  args: string[],
  prompt: string,
  hooks: ClaudeHooks = {},
  options: { stream?: boolean } = {},
): Promise<ClaudeResult> {
  const stream = options.stream !== false;

  return new Promise((resolve) => {
    const child = spawn('claude', normalizeFlags(args, stream), { stdio: ['pipe', 'pipe', 'pipe'] });
    if (child.pid !== undefined) hooks.onSpawn?.(child.pid);

    const onText = (text: string): void => hooks.onStdout?.(text);
    const parser = stream ? createStreamParser(onText) : createPassthrough(onText);
    let stderr = '';

    child.stdout?.on('data', (chunk: Buffer) => parser.push(chunk.toString()));

    child.stderr?.on('data', (chunk: Buffer) => {
      const text = chunk.toString();
      stderr += text;
      hooks.onStderr?.(text);
    });

    child.on('error', (err: Error) => {
      const code = (err as NodeJS.ErrnoException).code;
      const msg =
        code === 'ENOENT'
          ? 'claude command not found; please make sure Claude Code is installed and logged in (npm i -g @anthropic-ai/claude-code)'
          : err.message;
      resolve({ exitCode: 1, stdout: '', stderr: msg });
    });

    child.on('close', (code) => {
      parser.flush();
      resolve({ exitCode: code, stdout: parser.output(), stderr });
    });

    child.stdin?.end(prompt);
  });
}
