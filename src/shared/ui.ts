import { spinner } from '@clack/prompts';
import pc from 'picocolors';
import type { SpinnerHandle } from './types.ts';

/** Whether both stdin and stdout are TTYs */
export function isTTY(): boolean {
  return Boolean(process.stdin.isTTY && process.stdout.isTTY);
}

let timer: ReturnType<typeof setInterval> | null = null;
let active: ReturnType<typeof spinner> | null = null;

/** Start the clack spinner and periodically refresh a "label · elapsed seconds" line */
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

/** Stop the spinner on SIGINT */
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

/** Elapsed-time indicator shown while streaming */
export interface StreamClock {
  /** Write a streaming chunk. **Use this, don't write directly** — it keeps erase/write/top-up ordered. */
  write(chunk: string, to?: 'stdout' | 'stderr'): void;
  /** Streaming finished, erase the progress line */
  stop(): void;
}

/** The live clock (SIGINT must erase the half line it drew) */
let activeClock: StreamClock | null = null;

/** Stop the stream clock on SIGINT */
export function stopStreamClockActive(): void {
  activeClock?.stop();
  activeClock = null;
}

/**
 * The "elapsed Xs" clock shown while streaming.
 *
 * The spinner and the streaming body would overwrite each other, so run.ts tears the spinner down as
 * soon as output starts — but then the round has no time reference. This clock paints only at the start
 * of an empty line and erases before the next body chunk, so it never touches the body; it must write
 * the body itself (see StreamClock.write). In non-TTY it prints a plain line every 30s instead, since
 * \r would garble the log.
 */
export function startStreamClock(label: string): StreamClock {
  const start = performance.now();
  const tty = Boolean(process.stdout.isTTY);

  if (!tty) {
    const timer = setInterval(() => {
      console.log(`${label} · elapsed ${Math.floor((performance.now() - start) / 1000)}s`);
    }, 30_000);
    const plain: StreamClock = {
      write(chunk: string, to: 'stdout' | 'stderr' = 'stdout'): void {
        (to === 'stderr' ? process.stderr : process.stdout).write(chunk);
      },
      stop(): void {
        clearInterval(timer);
        if (activeClock === plain) activeClock = null;
      },
    };
    activeClock = plain;
    return plain;
  }

  /** Cursor is at the start of an empty line (safe to paint a new progress line) */
  let atLineStart = true;
  /** Progress currently owns the cursor's line */
  let painted = false;
  let lastPaint = 0;

  const erase = (): void => {
    if (painted) {
      process.stdout.write('\r\x1b[K');
      painted = false;
    }
  };

  /**
   * Paint only when the caller guarantees painted (redraw our own line) or atLineStart (new empty
   * line); otherwise erasing mid-line progress would really lose display. Do not move refresh into
   * setInterval — the start-of-line window is too short to hit; write() repaints on newline-ending
   * chunks, and lastPaint debounces dense markdown newlines.
   */
  const paint = (): void => {
    if (!painted && !atLineStart) return;
    const now = performance.now();
    if (now - lastPaint < 900) return;
    lastPaint = now;
    const secs = Math.floor((now - start) / 1000);
    process.stdout.write(`\r${pc.dim(`${label} · elapsed ${secs}s`)}\x1b[K`);
    painted = true;
  };

  // Fallback: also repaint while the stream stalls (waiting on the model)
  const timer = setInterval(paint, 1000);

  const clock: StreamClock = {
    write(chunk: string, to: 'stdout' | 'stderr' = 'stdout'): void {
      erase();
      (to === 'stderr' ? process.stderr : process.stdout).write(chunk);
      atLineStart = chunk.endsWith('\n');
      paint();
    },
    stop(): void {
      clearInterval(timer);
      erase();
      if (activeClock === clock) activeClock = null;
    },
  };
  activeClock = clock;
  return clock;
}

// ============ Logging (no spinner; picocolors disables color in non-TTY) ============

export function header(title: string): void {
  console.log(pc.bold(pc.cyan(`━━━ ${title} ━━━`)));
}

export function roundHeader(i: number, max: number): void {
  console.log(pc.bold(pc.cyan(`━━━ Round ${i}/${max} ━━━`)));
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
