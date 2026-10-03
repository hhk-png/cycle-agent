import type { PublishConfig } from '../publish-config.ts';

/**
 * Juejin publish config for the vLLM tutorial (chapters 09~22).
 *
 * Look up real category/tag ids with `publish.ts juejin --categories` and `--tags <word>`.
 *
 * Digests: 50~100 chars, single line, ideally 60~90 (see all counts with `publish.ts juejin --list`).
 */
const config: PublishConfig = {
  sourceDir: 'vllm-toturial',
  filePrefix: 'vllm教程',
  fromNumber: 9,

  // title = file name minus .md, e.g. `vllm教程-09-量化` (the H1 differs)
  titleSource: 'fileName',

  // ids measured 2026-09; category and tag "artificial intelligence" are different ids
  categoryId: '6809637773935378440', // category: artificial intelligence
  // ⚠️ max 3 (Juejin server limit, err_no=4031)
  tagIds: [
    '6809640642101116936', // artificial intelligence
    '7257794499869573175', // LLM
    '6809640679082295303', // deep learning
  ],

  // Cover: empty = not preset; set it on the first draft and the other 13 reuse its URL.
  coverImage: '',

  delayMs: 5000,
  timeoutMs: 15000,

  briefs: {
    '09': '从显存容量与带宽瓶颈切入，梳理量化的分类、定标粒度与量化数学，对比 GPTQ、AWQ、FP8、GGUF 等主流方案的精度与吞吐取舍，并给出在 vLLM 中加载量化模型的完整方法。',
    '10': '把 vLLM 从「能跑」推向「能上生产」：Docker 与 Kubernetes 部署、可观测性建设、结构化输出与工具调用，覆盖上线前后的关键工程环节与常见坑。',
    '11': '梳理 vLLM 与周边生态的集成：Hugging Face 模型加载、OpenAI SDK 兼容调用、LangChain 与 LlamaIndex 接入，以及自定义模型接入。',
    '12': '站在当下梳理推理引擎正在发生的变化：架构演进、PD 分离、MoE 优化、算法级加速与长上下文，最后落到 vLLM 自身的路线图与判断。',
    '13': '把使用 vLLM 时最高频的问题按主题整理成速查手册，先按主题一问一答，最后附一张「报错信息 → 原因 → 修复」的排查表，遇到问题先查这里。',
    '14': '当单卡放不下模型或吞吐不够时，如何用多卡多机把推理变大：四种并行范式 TP、PP、EP、DP 的原理与通信代价，vLLM 的多卡执行组织，以及并行策略选型。',
    '15': '让 vLLM 服务不止能读文字：多模态 LLM 的输入在推理引擎里如何组织、vLLM 如何支持视觉语言模型，以及 LoRA 适配器如何让一个底座服务多个业务。',
    '16': '全教程的查手册章节：把 vLLM 的环境变量与命令行参数按主题整理成可检索的参考表，并附 EngineArgs 映射，方便在遇到问题时快速定位。',
    '17': '解决最实际的工程问题：这张卡能跑多大的模型、多少并发。从显存三大部分讲起，给出可手算的公式、完整的容量规划实例，以及读懂 vLLM 启动日志的方法。',
    '18': '全教程的工程手册：把 mini-vLLM 从能看懂升级为能上手改，含完整 API 参考、关键数据约定、验证矩阵、9 个由浅入深的练习，以及常见陷阱与调试方法。',
    '19': '把 vLLM 支持的那一堆模型讲清楚：MHA、GQA、MLA 等注意力变体，RoPE、ALiBi 位置编码与 MoE，以及这些术语如何决定 KV cache 与内核实现。',
    '20': '一次完整的、可照做的部署实战：从真实需求出发，走完选型、容量规划、部署、客户端接入、压测调优到可观测性的全流程，并用 mini-vLLM 在本地复现。',
    '21': '把压测系统化：指标怎么定义、用什么工具量、负载怎么设计、结果怎么解读，以及如何从数据定位瓶颈并进入调优循环，建立一套可复现的性能基准流程。',
    '22': '把整本教程反复出现的术语集中成可检索的速查表，每个词条给一句话定义加关键章节引用，按主题分组并附索引，阅读中遇到陌生概念随时定位。',
  },
};

export default config;
