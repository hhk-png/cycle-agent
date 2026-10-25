import type { PublishConfig } from '../publish-config.ts';

/**
 * Juejin publish config for the KubeRay tutorial (chapters 01~69).
 *
 * Briefs are intentionally empty for now: these drafts are created with
 * `--skip-brief-check`; fill `briefs` before publishing.
 */
const config: PublishConfig = {
  sourceDir: 'kuberay-tutorial',
  filePrefix: 'kuberay教程',
  fromNumber: 1,
  titleSource: 'fileName',

  categoryId: '6809637769959178254', // 后端
  tagIds: [
    '6809640675944955918', // Kubernetes
    '6809641127432421384', // 云原生
    '6809641135154135054', // 分布式
  ],

  coverImage: '',
  delayMs: 5000,
  timeoutMs: 15000,
  briefs: {},
};

export default config;
