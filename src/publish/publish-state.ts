import { closeSync, existsSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';

/**
 * 发布状态的持久化。
 *
 * ⚠️ 有两条不可退让的规则,改动前请先读:
 *
 * 1. **状态文件损坏必须硬停,绝不能当成空状态。**
 *    把损坏文件当空 = 14 篇已经发出去的文章会被重发一遍(公开且不可逆)。
 *    所以 loadState 解析失败时返回 error,且**不接受 --force 绕过**。
 *
 * 2. **每完成一步就落盘,不在信号处理器里写盘。**
 *    建草稿拿到 draftId 后立刻保存,再去发布 —— 这样即使发布失败或 Ctrl-C,
 *    孤儿草稿的 id 也不会丢。SIGINT 里写文件会有写盘竞态,一律不做。
 *    不变量:任何时刻中断,最多损失一篇的进度,最坏留下一个孤儿草稿(草稿不公开)。
 */

export const STATE_VERSION = 1;

/** 单篇的发布状态 */
export interface ArticleState {
  /** pending=未建草稿;drafted=草稿已建待发布;published=已发布 */
  status: 'pending' | 'drafted' | 'published';
  draftId: string | null;
  articleId: string | null;
  url: string | null;
  /** 建草稿/发布时正文的 sha256,用于发现「草稿建好后正文又被改过」 */
  contentHash: string;
  /**
   * 最近一次成功发布时用的标题。
   * `--rename` 靠它判断「这篇已经改成目标标题了」,从而跳过重复的改+发;
   * 老状态文件里没有这个字段(undefined),不影响读取,只是第一次 rename 会多走一轮。
   */
  title?: string;
  /** 发布请求已发出但结果未知的时刻(=超时/网络中断);此时禁止自动重发 */
  publishAttemptedAt: string | null;
  /** 已发布但没拿到 article_id,需人工回后台确认 */
  needsCheck: boolean;
  updatedAt: string;
}

/** 整个发布流程的状态 */
export interface PublishState {
  version: number;
  /** 从草稿读回的人工设置的封面 URL,14 篇复用;空串表示还没有 */
  coverImage: string;
  /** key = 两位编号,如 '09' */
  articles: Record<string, ArticleState>;
}

/**
 * 状态文件名(带配置名,避免以后发别的教程时串台)。
 * `namespace` 区分平台 —— 微信侧传 `'wechat-publish-state'`,
 * 两边的状态互不覆盖(同一篇教程在两个平台上的进度是独立的)。
 */
export function stateFileName(
  repoRoot: string,
  configName: string,
  namespace = 'juejin-publish-state',
): string {
  return path.join(repoRoot, `.${namespace}.${configName}.json`);
}

/** 全新的空状态 */
export function newState(): PublishState {
  return { version: STATE_VERSION, coverImage: '', articles: {} };
}

/** 取某篇的状态,不存在则创建 */
export function ensureArticle(state: PublishState, no: string, contentHash: string): ArticleState {
  const existing = state.articles[no];
  if (existing) return existing;
  const fresh: ArticleState = {
    status: 'pending',
    draftId: null,
    articleId: null,
    url: null,
    contentHash,
    publishAttemptedAt: null,
    needsCheck: false,
    updatedAt: new Date().toISOString(),
  };
  state.articles[no] = fresh;
  return fresh;
}

/**
 * 读取状态。
 * 文件不存在 → 全新空状态;**内容损坏或版本不符 → 返回 error**(见文件头规则 1)。
 */
export function loadState(file: string): { state: PublishState } | { error: string } {
  if (!existsSync(file)) return { state: newState() };

  let text: string;
  try {
    text = readFileSync(file, 'utf8');
  } catch (err) {
    return { error: `读取状态文件失败: ${(err as Error).message}` };
  }
  if (!text.trim()) return { state: newState() };

  let parsed: PublishState;
  try {
    parsed = JSON.parse(text) as PublishState;
  } catch (err) {
    return {
      error:
        `状态文件已损坏,无法解析: ${file}\n` +
        `  ${(err as Error).message}\n` +
        `  请修复它,或把它移走再重跑。**不要直接删除** —— 里面记着哪些文章已经发布过,\n` +
        `  删了会导致已发布的文章被重发。`,
    };
  }

  if (parsed.version !== STATE_VERSION) {
    return {
      error:
        `状态文件版本不符(文件 ${parsed.version},当前 ${STATE_VERSION}): ${file}\n` +
        `  请手动迁移或移走该文件后再重跑。`,
    };
  }
  if (!parsed.articles || typeof parsed.articles !== 'object') {
    return { error: `状态文件缺少 articles 字段,可能已损坏: ${file}` };
  }
  if (typeof parsed.coverImage !== 'string') parsed.coverImage = '';
  return { state: parsed };
}

/** 原子写:先写 .tmp 再 rename,避免中断留下半截 JSON */
export function saveState(file: string, state: PublishState): { ok: true } | { error: string } {
  const tmp = `${file}.tmp`;
  try {
    writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, 'utf8');
    renameSync(tmp, file);
    return { ok: true };
  } catch (err) {
    return { error: `写入状态文件失败: ${(err as Error).message}` };
  }
}

// ============ 并发锁 ============

/**
 * 取进程锁,防止两个终端同时跑导致状态互相覆盖。
 * 陈旧锁(持有进程已不存在)会被自动接管。
 */
export function acquireLock(file: string): { ok: true } | { error: string } {
  const lockFile = `${file}.lock`;
  const payload = JSON.stringify({ pid: process.pid, at: new Date().toISOString() });

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      closeSync(openSync(lockFile, 'wx'));
      writeFileSync(lockFile, payload, 'utf8');
      return { ok: true };
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'EEXIST') {
        return { error: `创建锁文件失败: ${(err as Error).message}` };
      }
      // 锁已存在:看持有者还活着吗
      let pid = 0;
      try {
        pid = Number(JSON.parse(readFileSync(lockFile, 'utf8')).pid) || 0;
      } catch {
        pid = 0; // 锁文件本身坏了,当陈旧锁处理
      }
      if (pid > 0 && isAlive(pid)) {
        return {
          error: `另一次发布正在进行中(pid ${pid})。\n  若确认没有其它进程在跑,请删除 ${lockFile} 后重试。`,
        };
      }
      // 陈旧锁:清掉后接管
      try {
        unlinkSync(lockFile);
      } catch {
        /* 竞争失败就再试一轮,下一轮会报错 */
      }
    }
  }
  return { error: `无法获得锁: ${lockFile}` };
}

/** 释放锁(尽力而为,失败不影响主流程) */
export function releaseLock(file: string): void {
  try {
    unlinkSync(`${file}.lock`);
  } catch {
    /* 忽略 */
  }
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM 说明进程存在但没权限发信号,仍算活着
    return (err as NodeJS.ErrnoException).code === 'EPERM';
  }
}
