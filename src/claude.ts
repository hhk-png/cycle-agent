import spawn from 'cross-spawn';
import { spawnSync } from 'node:child_process';
import type { ClaudeResult, ClaudeHooks } from './types.ts';

let activeChild: ReturnType<typeof spawn> | null = null;

/**
 * 中断时结束当前正在运行的 claude 子进程。
 * Windows 上 claude 经 cmd.exe 包装启动,`child.kill()` 只会杀掉 cmd 壳,
 * 真正的 claude(node)进程会残留在后台,所以用 `taskkill /T` 结束整棵进程树。
 */
export function killActiveClaude(): void {
  const child = activeChild;
  if (!child || child.killed) return;
  const pid = child.pid;
  if (pid === undefined) return;

  if (process.platform === 'win32') {
    try {
      // /T 连带子进程一起结束,/F 强制
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
    } catch {
      child.kill(); // taskkill 不可用时的兜底
    }
  } else {
    child.kill();
  }
}

/**
 * 通过 stdin 把 prompt 喂给 `claude -p`,缓冲 stdout/stderr。
 * 返回子进程退出码与捕获的输出。
 */
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

    // spawn 失败也走 resolve
    child.on('error', (err: Error) => {
      activeChild = null;
      const code = (err as NodeJS.ErrnoException).code;
      const msg =
        code === 'ENOENT'
          ? '未找到 claude 命令，请确认已安装并登录 Claude Code（npm i -g @anthropic-ai/claude-code）'
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
