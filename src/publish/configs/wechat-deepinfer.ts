import type { WechatConfig } from '../wechat-config.ts';

/**
 * WeChat publish config for the DeepInfer tutorial.
 *
 * Used first as the test bed for `renderMode: 'wechat'` — WeChat-like typography with a
 * light code box, instead of the tutorial's colored-heading / dark-code look. (Truly
 * unstyled `native` HTML was tried first and renders as browser defaults in the draft
 * box: WeChat does not apply its editor theme to API-created drafts.)
 */
const config: WechatConfig = {
  sourceDir: 'deepinfer-tutorial',
  filePrefix: 'deepinfer教程',
  fromNumber: 0,
  // The article title must match the Markdown file name (without `.md`).
  titleSource: 'fileName',

  appId: 'wx27e3c5ea021918b7',
  secretFile: '.wechat-secret',

  startDate: '2026-11-01',

  // No Juejin brief set for this tutorial yet; WeChat auto-generates the digest from
  // the first 54 chars (only a warning, never a failure).
  digests: {},

  renderMode: 'wechat',

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;
