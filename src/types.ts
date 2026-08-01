/** claude 子进程的执行结果 */
export interface ClaudeResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

/** runClaude 的可选钩子 */
export interface ClaudeHooks {
  /** claude 每输出一块 stdout 时回调（用于实时流式打印） */
  onStdout?: (chunk: string) => void;
  /** claude 每输出一块 stderr 时回调（用于实时流式打印，verbose 日志等） */
  onStderr?: (chunk: string) => void;
}

/** spinner 句柄,由 ui.startSpinner 返回 */
export interface SpinnerHandle {
  stopSuccess(msg: string): void;
  stopError(msg: string): void;
}
