import type { WechatConfig } from '../wechat-config.ts';

/** WeChat publish config for the KubeRay tutorial (chapters 01~69). */
const config: WechatConfig = {
  sourceDir: 'kuberay-tutorial',
  filePrefix: 'kuberay教程',
  fromNumber: 1,
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
