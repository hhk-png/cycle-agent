import { spinner } from '@clack/prompts';
import pc from 'picocolors';
import type { SpinnerHandle } from './types.ts';

/** 是否在交互终端运行（stdin 与 stdout 都是 TTY） */
export function isTTY(): boolean {
  return Boolean(process.stdin.isTTY && process.stdout.isTTY);
}

let timer: ReturnType<typeof setInterval> | null = null;
let active: ReturnType<typeof spinner> | null = null;

/** 启动 clack spinner,并周期性刷新「标签 · 已用秒数」 */
export function startSpinner(label: string): SpinnerHandle {
  const start = performance.now();

  active = spinner();
  active.start(label);
  timer = setInterval(() => {
    const secs = Math.floor((performance.now() - start) / 1000);
    active?.message(`${label} · ${secs}s`);
  }, 100);

  return {
    stopSuccess(msg: string) {
      clearTimer();
      active?.stop(pc.green(msg));
      active = null;
    },
    stopError(msg: string) {
      clearTimer();
      active?.stop(pc.red(msg));
      active = null;
    },
  };

  function clearTimer(): void {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }
}

/** SIGINT 时停掉可能还在跑的 spinner */
export function stopSpinnerActive(): void {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  if (active) {
    active.stop();
    active = null;
  }
}

// ============ 日志（无 spinner 时使用;picocolors 非 TTY 自动无色） ============

export function header(title: string): void {
  console.log(pc.bold(pc.cyan(`━━━ ${title} ━━━`)));
}

export function roundHeader(i: number, max: number): void {
  console.log(pc.bold(pc.cyan(`━━━ 第 ${i}/${max} 轮 ━━━`)));
}

export function success(msg: string): void {
  console.log(pc.green(msg));
}

export function error(msg: string): void {
  console.error(pc.red(msg));
}

export function dim(msg: string): void {
  console.log(pc.dim(msg));
}

export function info(msg: string): void {
  console.log(msg);
}
