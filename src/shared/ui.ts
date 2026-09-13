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

/** 流式输出期间的耗时指示器 */
export interface StreamClock {
  /**
   * 把一段流式输出交给它写。**必须走这里,不要自己再 write 一遍** ——
   * 顺序是「先擦进度行 → 写正文 → 正文以换行结尾的话补一行进度」,
   * 拆开做就会把进度文字拼进正文行里。
   */
  write(chunk: string, to?: 'stdout' | 'stderr'): void;
  /** 流式结束,擦掉进度行 */
  stop(): void;
}

/** 当前活着的计时器(SIGINT 时要能擦掉它画的那半行) */
let activeClock: StreamClock | null = null;

/** SIGINT 时停掉可能还在跑的计时器 */
export function stopStreamClockActive(): void {
  activeClock?.stop();
  activeClock = null;
}

/**
 * 流式输出期间的「已用 Xs」计时器。
 *
 * ⚠️ 为什么不能继续用 spinner:spinner 独占一行、靠不断重画那一行来刷时间,
 * 而流式正文是连续写出去的 —— 两者会互相覆盖,所以 run.ts 一收到输出就把
 * spinner 拆了。但拆了之后整轮就**再没有任何时间参考**,一轮跑十几分钟时很难受。
 *
 * 这个计时器的做法:进度只在**光标停在空行行首**时画(也就是刚写完一段以
 * 换行结尾的正文之后),它独占那一行;下一段正文到达前先 `\r\x1b[K` 擦掉。
 * 所以它永远不会擦到正文,也不会把进度文字拼进正文行里。
 *
 * 因此它必须**自己写正文**(见 StreamClock.write 的注释)—— 擦、写、补
 * 这个顺序不能拆到调用方去做。
 *
 * 非 TTY(stdout 被重定向到文件)时不能这么干 —— 那些 \r 会变成日志里的乱码,
 * 所以那种情况下改成每 30 秒打一行普通文本。picocolors 非 TTY 自动无色。
 */
export function startStreamClock(label: string): StreamClock {
  const start = performance.now();
  const tty = Boolean(process.stdout.isTTY);

  if (!tty) {
    const timer = setInterval(() => {
      console.log(`${label} · 已用 ${Math.floor((performance.now() - start) / 1000)}s`);
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

  /** 光标停在空行行首(可以安全新画一行进度) */
  let atLineStart = true;
  /** 进度当前正独占着光标所在的那一行 */
  let painted = false;
  let lastPaint = 0;

  const erase = (): void => {
    if (painted) {
      process.stdout.write('\r\x1b[K');
      painted = false;
    }
  };

  /**
   * 画/刷新进度。两条合法的前提(调用方保证):
   *   - painted 为真 —— 那一行只有我们,原地重画;
   *   - atLineStart 为真 —— 光标在空行行首,这是新的一行。
   * 其余情况一律不画:行中间那半行是正文,`\r\x1b[K` 一擦就是真丢显示。
   *
   * ⚠️ 别改成「只在 setInterval 里刷」:流式分片几十毫秒来一片,行首的窗口很短,
   * 1 秒一次的 tick 撞进行首的概率很低,可能十几秒才刷上一次,甚至一直撞不上。
   * 所以主要靠 write() 在「这段正文以换行结尾」时立刻补画 —— 那一刻光标正好在行首。
   * lastPaint 那个间隔是防抖:markdown 流里换行很密,不拦一下每片都会重画一次。
   */
  const paint = (): void => {
    if (!painted && !atLineStart) return;
    const now = performance.now();
    if (now - lastPaint < 900) return;
    lastPaint = now;
    const secs = Math.floor((now - start) / 1000);
    process.stdout.write(`\r${pc.dim(`${label} · 已用 ${secs}s`)}\x1b[K`);
    painted = true;
  };

  // 兜底:流**停住**了(在等模型、没有输出)也要能看出还在跑
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
