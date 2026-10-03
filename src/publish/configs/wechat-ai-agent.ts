import type { WechatConfig } from '../wechat-config.ts';
import juejin from './ai-agent-toturial.ts';

/**
 * WeChat Official Account publish config for the AI Agent tutorial (chapters 00~22).
 *
 * Digests reuse the Juejin config (WeChat's 120-char limit is looser than Juejin's 100).
 *
 * ⚠️ WeChat titles max 32 chars; titleOverrides shorten the over-limit ones. Judge by the
 * `loadArticles` title length, not the source file name length.
 */
const config: WechatConfig = {
  sourceDir: 'ai-agent-toturial',
  filePrefix: '',
  fromNumber: 0,

  // same as Juejin: file-name style + series prefix (source names have no prefix)
  titleSource: 'fileName',
  titlePrefix: 'ai-agent教程-',

  // ⬇️ WeChat platform (developers.weixin.qq.com/platform/) → My Business → Official Account → Basic Information
  appId: 'wx27e3c5ea021918b7',
  secretFile: '.wechat-secret',

  author: '',

  // shared with the vllm tutorial (2.35:1, white-padded per WeChat's cropping)
  coverImage: 'assets/wechat-cover.jpg',

  // issue 1's date, then daily; --plan uses it, drafts don't
  startDate: '2026-10-05',

  contentSourceUrl: '',

  // ⚠️ titles max 32 chars; 15 was 34 (over), 22 was exactly 32 (on the line)
  titleOverrides: {
    '15': 'ai-agent教程-15-附录B-工程参考',
    '22': 'ai-agent教程-22-附录I-原则与决策速查',
  },

  digests: {
    ...juejin.briefs,
  },

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;
