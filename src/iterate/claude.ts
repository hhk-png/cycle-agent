import spawn from 'cross-spawn';
import { spawnSync } from 'node:child_process';
import type { ClaudeResult, ClaudeHooks } from '../shared/types.ts';

let activeChild: ReturnType<typeof spawn> | null = null;

/**
 * Force-kill the Win32 process tree rooted at `rootPid`.
 *
 * `taskkill /T` on the pid recorded at spawn time is not enough. Ctrl+C is broadcast to every
 * process attached to the console, and the cmd.exe shim that spawn() gets on Windows dies from
 * that event immediately instead of forwarding it. By the time our SIGINT handler runs, the shim
 * is usually already gone, so `taskkill /T` on its pid finds nothing and the real claude.exe
 * survives as an orphan.
 *
 * The tree can still be rebuilt, because Windows keeps the *creating* pid in ParentProcessId even
 * after the parent exits. So snapshot the parent links, walk down from the shim, and kill every
 * descendant by its own pid rather than by tree traversal.
 */
function killTreeWindows(rootPid: number): void {
  const script = [
    `$root = ${rootPid}`,
    '$all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId',
    '# parent pid -> child pids, snapshotted before anything is killed',
    '$kids = @{}',
    'foreach ($p in $all) {',
    '  $k = [int64]$p.ParentProcessId',
    '  if (-not $kids.ContainsKey($k)) { $kids[$k] = New-Object System.Collections.ArrayList }',
    '  [void]$kids[$k].Add([int64]$p.ProcessId)',
    '}',
    '$order = New-Object System.Collections.ArrayList',
    '$q = New-Object System.Collections.Queue',
    '$q.Enqueue($root)',
    'while ($q.Count -gt 0) {',
    '  $cur = [int64]$q.Dequeue()',
    '  if ($kids.ContainsKey($cur)) { foreach ($c in $kids[$cur]) { [void]$order.Add($c); $q.Enqueue($c) } }',
    '}',
    '# Children first, so a parent exiting mid-sweep cannot orphan a child we have not reached yet',
    'for ($i = $order.Count - 1; $i -ge 0; $i--) { & taskkill /PID $order[$i] /T /F *> $null }',
    '& taskkill /PID $root /T /F *> $null',
  ].join('\n');

  spawnSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], {
    stdio: 'ignore',
    timeout: 20_000,
  });
}

/** On interruption, terminate the running claude subprocess and everything it started. */
export function killActiveClaude(): void {
  const child = activeChild;
  if (!child) return;
  const pid = child.pid;
  if (pid === undefined) return;

  if (process.platform === 'win32') {
    let swept = false;
    try {
      killTreeWindows(pid);
      swept = true;
    } catch {
      // PowerShell unavailable — fall back to the plain tree kill below
    }
    if (!swept) {
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
    }
  }

  try {
    child.kill(); // Redundant after the sweep on Windows, and the only mechanism on POSIX
  } catch {
    // Already gone
  }
}

/**
 * Pin the flags that are coupled to how runClaude reads stdout.
 *
 * `--output-format` is not a per-tutorial choice: the stream parser only understands stream-json, and
 * the passthrough only makes sense on text. So it is owned here rather than by a config — any value
 * a config still carries is dropped, which keeps a copied config from silently breaking the parser.
 * The rest of the flags (--effort, permissions) are passed through untouched.
 */
function normalizeFlags(flags: string[], stream: boolean): string[] {
  const out: string[] = [];
  for (let i = 0; i < flags.length; i++) {
    if (flags[i] === '--output-format') {
      i += 1; // drop the value too
      continue;
    }
    if (flags[i].startsWith('--output-format=')) continue;
    out.push(flags[i]);
  }
  if (stream) {
    if (!out.includes('--verbose')) out.push('--verbose'); // required by --print with stream-json
    out.push('--output-format', 'stream-json', '--include-partial-messages');
  } else {
    out.push('--output-format', 'text');
  }
  return out;
}

/** Tool input keys worth showing as a one-line hint, most specific first */
const TOOL_HINT_KEYS = ['file_path', 'path', 'command', 'pattern', 'url', 'prompt'];

function toolHint(json: string): string {
  try {
    const input = JSON.parse(json) as Record<string, unknown>;
    for (const key of TOOL_HINT_KEYS) {
      const value = input[key];
      if (typeof value === 'string' && value) return ` ${value.replace(/\s+/g, ' ').slice(0, 70)}`;
    }
  } catch {
    // Input never completed, or was not JSON — fall back to the bare tool name
  }
  return '';
}

/**
 * Turn claude's stream-json output into something worth watching.
 *
 * `--output-format text` emits nothing until the whole round finishes, which makes a long round
 * indistinguishable from a hung one. stream-json emits one JSON object per line while the model
 * generates, so text deltas can go straight to the caller's stream hook and tool calls can be
 * annotated with what they touch — this is the only progress signal the program has.
 */
function createStreamParser(onText: (text: string) => void) {
  let buffer = '';
  let text = '';
  /** Input JSON of the tool call currently streaming, so its target can be reported */
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
      emit(`${raw}\n`); // Not JSON — pass it through rather than swallow the output
      return;
    }

    if (msg.type === 'result') {
      // The text already streamed; this only covers a stream that never produced deltas
      if (!text && typeof msg.result === 'string') text = msg.result;
      return;
    }
    if (msg.type !== 'stream_event') return; // system/assistant events carry nothing incremental

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
      // Report the call only once its input is complete, so the hint names the real target
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
    /** Parse the trailing line that never received its newline */
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

/** Streaming off: hand the raw output through untouched, same shape as the stream parser */
function createPassthrough(onText: (text: string) => void) {
  let text = '';
  return {
    push(chunk: string): void {
      text += chunk;
      onText(chunk);
    },
    flush(): void {
      // Nothing is buffered between chunks
    },
    output(): string {
      return text;
    },
  };
}

/**
 * Feed the prompt to `claude -p` via stdin; resolve with exit code and the assistant text.
 *
 * `stream` defaults to true; pass false to fall back to the config's own --output-format, where the
 * whole round's output arrives at once when it ends.
 */
export function runClaude(
  args: string[],
  prompt: string,
  hooks: ClaudeHooks = {},
  options: { stream?: boolean } = {},
): Promise<ClaudeResult> {
  const stream = options.stream !== false;

  return new Promise((resolve) => {
    const child = spawn('claude', normalizeFlags(args, stream), { stdio: ['pipe', 'pipe', 'pipe'] });
    activeChild = child;

    const onText = (text: string): void => hooks.onStdout?.(text);
    const parser = stream ? createStreamParser(onText) : createPassthrough(onText);
    let stderr = '';

    child.stdout?.on('data', (chunk: Buffer) => parser.push(chunk.toString()));

    child.stderr?.on('data', (chunk: Buffer) => {
      const text = chunk.toString();
      stderr += text;
      hooks.onStderr?.(text);
    });

    // Even a spawn failure goes through resolve
    child.on('error', (err: Error) => {
      activeChild = null;
      const code = (err as NodeJS.ErrnoException).code;
      const msg =
        code === 'ENOENT'
          ? 'claude command not found; please make sure Claude Code is installed and logged in (npm i -g @anthropic-ai/claude-code)'
          : err.message;
      resolve({ exitCode: 1, stdout: '', stderr: msg });
    });

    child.on('close', (code) => {
      activeChild = null;
      parser.flush();
      resolve({ exitCode: code, stdout: parser.output(), stderr });
    });

    child.stdin?.end(prompt);
  });
}
