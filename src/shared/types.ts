/** Result of the claude subprocess */
export interface ClaudeResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

/** Optional hooks for runClaude */
export interface ClaudeHooks {
  /** Called per stdout chunk (real-time streaming print) */
  onStdout?: (chunk: string) => void;
  /** Called per stderr chunk (real-time streaming print, verbose logs) */
  onStderr?: (chunk: string) => void;
}

/** Returned by ui.startSpinner */
export interface SpinnerHandle {
  stopSuccess(msg: string): void;
  stopError(msg: string): void;
}
