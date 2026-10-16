export interface ClaudeResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

export interface ClaudeHooks {
  onStdout?: (chunk: string) => void;
  onStderr?: (chunk: string) => void;
  onSpawn?: (pid: number) => void;
}

export interface SpinnerHandle {
  stopSuccess(msg: string): void;
  stopError(msg: string): void;
}
