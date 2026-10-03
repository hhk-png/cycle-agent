import type { WechatConfig } from '../wechat-config.ts';
import juejin from './ray-toturial.ts';

/**
 * WeChat Official Account publish config for the Ray tutorial (chapters 00~39).
 *
 * Source file names already carry `ray-tutorial-`, so no titlePrefix is needed.
 * Digests reuse the Juejin config (WeChat 120 > Juejin 100, both compliant).
 *
 * ⚠️ WeChat titles max 32 chars: 26/27/29 exceed it and are shortened below.
 */
const config: WechatConfig = {
  sourceDir: 'ray-toturial',
  filePrefix: 'ray教程',
  fromNumber: 0,

  titleSource: 'fileName',

  appId: 'wx27e3c5ea021918b7',
  secretFile: '.wechat-secret',

  author: '',

  coverImage: 'assets/wechat-cover.jpg',

  // follows the AI Agent series (23 issues from 2026-10-05); date only affects --plan
  startDate: '2026-11-01',

  contentSourceUrl: '',

  // ⚠️ titles max 32 chars; these three exceed that with their original names
  titleOverrides: {
    '26': 'ray教程-26-RayCompiledGraph与DAG',
    '27': 'ray教程-27-附录F-RayClient与多语言',
    '29': 'ray教程-29-Ray与PyTorch生态集成',
  },

  digests: {
    ...juejin.briefs,
  },

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;
