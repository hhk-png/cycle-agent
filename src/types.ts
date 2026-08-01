/** claude 子进程的执行结果 */
export interface ClaudeResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

/** runClaude 的可选钩子 */
export interface ClaudeHooks {
  /** claude 每次输出一块 stdout 时回调（供 spinner 显示实时字节数） */
  onData?: (chunk: string) => void;
}

/** spinner 句柄,由 ui.startSpinner 返回 */
export interface SpinnerHandle {
  /** 累计 claude 已输出的字节数 */
  update(bytes: number): void;
  stopSuccess(msg: string): void;
  stopError(msg: string): void;
}
