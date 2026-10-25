import type { WechatConfig } from '../wechat-config.ts';

/** WeChat publish config for the LanceDB tutorial (chapters 00~40). */
const config: WechatConfig = {
  sourceDir: 'lancedb-tutorial',
  filePrefix: 'lancedb教程',
  fromNumber: 0,
  titleSource: 'fileName',

  appId: 'wx27e3c5ea021918b7',
  secretFile: '.wechat-secret',

  startDate: '2026-11-01',
  digests: {},
  renderMode: 'wechat',

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;
