import type { PublishConfig } from '../publish-config.ts';

/**
 * Juejin publish config for the DeepInfer tutorial (chapters 00~61).
 *
 * Briefs are intentionally empty for now: these drafts are created with
 * `--skip-brief-check`; fill `briefs` before publishing.
 */
const config: PublishConfig = {
  sourceDir: 'deepinfer-tutorial',
  filePrefix: 'deepinfer教程',
  fromNumber: 0,
  titleSource: 'fileName',

  categoryId: '6809637773935378440', // 人工智能
  tagIds: [
    '6809640642101116936', // 人工智能
    '6809641135154135054', // 分布式
    '7257794499869573175', // LLM
  ],

  coverImage: '',
  delayMs: 5000,
  timeoutMs: 15000,
  briefs: {},
};

export default config;
