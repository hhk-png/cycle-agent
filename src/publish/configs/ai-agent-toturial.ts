import type { PublishConfig } from '../publish-config.ts';

/**
 * Juejin publish config for the AI Agent tutorial (chapters 00~22).
 *
 * Source file names carry no series prefix, so filePrefix is empty and titlePrefix adds one;
 * the names can't change because the README's 23 chapter links are hard-coded to them.
 *
 * Digests: 50~100 chars, single line, ideally 60~90 (see all counts with
 * `node src/publish/publish.ts juejin ai-agent-toturial --list`).
 */
const config: PublishConfig = {
  sourceDir: 'ai-agent-toturial',
  filePrefix: '',
  fromNumber: 0,

  titleSource: 'fileName',
  titlePrefix: 'ai-agent教程-',

  // ids below measured with `publish.ts juejin <name> --categories` / `--tags <word>`
  categoryId: '6809637773935378440', // category: artificial intelligence
  // ⚠️ max 3 (Juejin server limit, err_no=4031)
  tagIds: [
    '6809640642101116936', // artificial intelligence
    '7516396389476401162', // Agent
    '7257794499869573175', // LLM
  ],

  // Cover: empty = not preset; set it on the first draft and the rest reuse its URL.
  coverImage: '',

  delayMs: 5000,
  timeoutMs: 15000,

  briefs: {
    '00': '本章包括:1.教程要解决的问题与适合的读者;2.六条阅读路径与推荐顺序;3.前置知识与环境准备;4.mini-agent 的目录结构与运行方法;5.全书章节地图。',
    '01': '本章包括:1.AI Agent 的可操作定义;2.与 LLM 应用、Workflow、RAG 的边界划分;3.感知-思考-行动-观察的核心循环;4.能力栈与分类学;5.选型时的判断依据。',
    '02': '本章包括:1.核心循环与 ReAct 范式;2.推理模型在决策中的作用;3.任务规划与反思式自我修正;4.常见失败模式与成因;5.七种工作流模式及各自的适用场景。',
    '03': '本章包括:1.模型接口的请求契约与流式输出;2.结构化输出与 function calling 机制;3.tool_choice 的控制粒度;4.多模态输入的接入方式;5.工具描述与参数的设计原则。',
    '04': '本章包括:1.记忆的分类与生命周期;2.短期滑窗与摘要压缩;3.向量库与 RAG 的分工;4.记忆读写架构的设计;5.产品案例中的取舍;6.遗忘策略与隐私合规。',
    '05': '本章包括:1.上下文预算的分配方法;2.消息顺序对效果的影响;3.Prompt Caching 的原理与用法;4.动态工具子集的裁剪;5.长对话的压缩策略;6.常见反模式清单。',
    '06': '本章包括:1.主流框架的演化;2.LangGraph 与 CrewAI 的编排模型;3.AutoGen 的多智能体设计;4.OpenAI 与 Claude 的 Agent SDK;5.选型对比表。',
    '07': '本章包括:1.mini-agent 的整体目录结构;2.工具注册与执行链路;3.记忆与上下文管理的实现;4.主循环与错误处理;5.工程化六条实践及代码落点;6.如何跑通验证。',
    '08': '本章包括:1.多智能体的拓扑结构;2.通信协议与消息格式;3.编排与任务分配方式;4.协作场景的评测与安全;5.A2A 协议的定位;6.常见反模式与替代方案。',
    '09': '本章包括:1.MCP 要解决的问题与由来;2.协议规范的核心概念;3.客户端与服务端各自的实现要点;4.从本地 Demo 到生产的升级路径;5.安全边界;6.生态现状。',
    '10': '本章包括:1.五维评测框架;2.指标的定义与计算方式;3.LLM-as-Judge 的用法与偏差;4.轨迹级评测怎么做;5.主流基准与安全基准;6.接入 CI 的工程化做法。',
    '11': '本章包括:1.Agent 的威胁模型;2.提示注入的攻击面与防御;3.身份认证与权限授权;4.工具滥用与越权;5.红队演练的组织方式;6.OWASP LLM Top 10 逐条对应。',
    '12': '本章包括:1.服务化与 API 契约设计;2.可观测三支柱与看板建设;3.可靠性与降级策略;4.成本核算与控制;5.人机协作的边界划分;6.CI/CD 的落地方式。',
    '13': '本章包括:1.2026 年的 Agent 生态格局;2.五个正在成形的方向;3.主流商业模式与落地情况;4.模型侧的趋势变化;5.尚未解决的开放问题与各自判断。',
    '14': '本章包括:1.按十二个主题分组的术语条目;2.每条的一句话定义;3.相关章节的交叉指引;4.拼音索引的查法;5.易混概念的对照说明与区分要点。',
    '15': '本章包括:1.mini-agent 的完整 API 参考;2.关键数据结构的约定;3.验证矩阵与运行方式;4.十个由浅入深的练习;5.常见陷阱与对应的调试方法。',
    '16': '本章包括:1.需求分析与边界界定;2.客服 Agent 的架构设计;3.工具与知识库的接入;4.评测方案与指标选取;5.部署上线流程;6.上线后的问题定位与迭代。',
    '17': '本章包括:1.概念混淆类问题;2.工程取舍类问题;3.效果调优类问题;4.部署与成本类问题;5.每个问题先给结论再讲原因;6.高频误区的纠正与自查清单。',
    '18': '本章包括:1.基础论文清单;2.工程实践指南;3.主流框架与工具;4.评测基准;5.基础设施选型;6.每条资源值得读在哪里、适合什么阶段的读者。',
    '19': '本章包括:1.五十条可命名的反模式及其命名;2.每条的症状描述;3.根因分析;4.修复做法;5.预防措施;6.按症状反查的索引与相邻模式的区分。',
    '20': '本章包括:1.提示词的结构解剖;2.Few-shot 与思维链的用法;3.可复用的提示模式目录;4.Agent 场景下的专门写法;5.版本化、评测与迭代的工程化做法。',
    '21': '本章包括:1.ANN 算法 IVF、HNSW 与 PQ 的原理与选型;2.距离度量的选择;3.向量库对比;4.混合检索与重排序;5.RAG 管线的分块、召回与调优思路。',
    '22': '本章包括:1.六十八条可命名的工程原则;2.每条原则的适用条件与反例;3.八个高频决策点的速查;4.与反模式目录的对应关系;5.决策时该怎么取舍。',
  },
};

export default config;
