import spawn from 'cross-spawn';
import { spawnSync } from 'node:child_process';
import type { ClaudeResult, ClaudeHooks } from '../shared/types.ts';

let activeChild: ReturnType<typeof spawn> | null = null;

/**
 * On interruption, terminate the running claude subprocess. Windows wraps claude in cmd.exe, so
 * `child.kill()` only kills the shell and the real claude (node) process lingers — hence `taskkill /T`
 * to kill the whole tree.
 */
export function killActiveClaude(): void {
  const child = activeChild;
  if (!child || child.killed) return;
  const pid = child.pid;
  if (pid === undefined) return;

  if (process.platform === 'win32') {
    try {
      // /T terminates child processes too, /F forces it
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
    } catch {
      child.kill(); // Fallback if taskkill is unavailable
    }
  } else {
    child.kill();
  }
}

/** Feed the prompt to `claude -p` via stdin; resolve with exit code and buffered output */
export function runClaude(
  args: string[],
  prompt: string,
  hooks: ClaudeHooks = {},
): Promise<ClaudeResult> {
  return new Promise((resolve) => {
    const child = spawn('claude', args, { stdio: ['pipe', 'pipe', 'pipe'] });
    activeChild = child;

    let stdout = '';
    let stderr = '';

    child.stdout?.on('data', (chunk: Buffer) => {
      const text = chunk.toString();
      stdout += text;
      hooks.onStdout?.(text);
    });

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
      resolve({ exitCode: code, stdout, stderr });
    });

    child.stdin?.end(prompt);
  });
}
