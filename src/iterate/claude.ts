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
