# 附录 A 术语表

本附录按主题列出书中反复出现的术语。中文解释尽量给出“它在系统里做什么”，而不是只给字面翻译。遇到正文里没展开的概念，可以先在这里找一句话定义，再回到对应章节。

## A.1 性能指标

**TTFT（Time To First Token）**：从请求到达到第一个输出 token 的时间。主要由排队、prefill 和第一步 decode 决定。

**TPOT（Time Per Output Token）**：第一个 token 之后，平均每个输出 token 的时间。主要由 decode 的 step time 决定。

**E2E（End-to-End）**：从请求到达到最后一个 token 的时间，约等于 TTFT 加 `(输出 token 数 - 1) × TPOT`，再加网络和排队。

**吞吐（Throughput）**：单位时间处理的 token 数或请求数。要区分输入 token、输出 token 和总 token。

**Goodput**：在 SLO 内完成的有用请求吞吐。比裸吞吐更接近业务价值。

**P50/P95/P99**：延迟分布的分位数。P99 是尾延迟，通常比均值更能反映用户体验。

**Roofline**：用算力上限和带宽上限画出的性能边界。算术强度低于 ridge point 时受带宽限制。

**算术强度（Arithmetic Intensity）**：FLOPs 除以搬运字节数。

**Ridge Point**：`峰值算力 / 峰值带宽`，算术强度超过它时受算力限制，低于它时受带宽限制。

**Bubble**：流水线中 GPU 或 CPU 空转的时间片。

**Goodput 与吞吐的差**：超时、失败、超过 SLO 的请求即使产出了 token，也不计入 goodput。

## A.2 模型与推理

**Prefill**：处理 prompt 的阶段，所有 prompt token 并行计算，决定 TTFT。

**Decode**：逐 token 生成的阶段，每步一个 query，通常受带宽限制，决定 TPOT。

**KV Cache**：缓存历史 token 的 Key/Value，避免重复计算。

**Paged Attention / Paged KV**：把 KV cache 切成固定大小的 block，用页表映射逻辑 token 到物理 block。

**Prefix Caching**：用内容哈希复用相同前缀的 KV block。

**Continuous Batching**：以 step 为单位动态加入/移除请求，而不是静态 batch。

**Chunked Prefill**：把长 prompt 切成块，分多个 step 做 prefill，和 decode 混排。

**Preemption**：显存不足时抢占某个序列，停止它或释放 KV 后重算。

**LCFS（Longest Completion First）**：优先调度输出最长的序列，显存不足时抢占输出最短的。

**MLA（Multi-head Latent Attention）**：用低秩 latent 压缩 KV 的注意力结构。

**GQA/MQA**：多个 query 头共享一组或一组分组的 KV 头。

**CSA2（Compressed Sparse Attention 2）**：DeepSeek-V4.1 的压缩 + 稀疏注意力，含 Full/Reindex/Reuse 模式。

**SWA（Sliding Window Attention）**：只让 query 看最近 window 个 key。

**YoCo（You Only Cache Once）**：一种跨层/跨阶段复用 KV、减少重复计算的设计；DeepInfer 用它做有界重放。

**CED（Causal Encoder-Decoder）**：前若干层 encoder、后若干层 decoder，decoder 的 global KV 从 encoder 最后 hidden 投影。

**Engram**：哈希 n-gram 条件记忆，把大量参数放在可检索的表里。

**mHC（multi-head residual mixing）**：多 residual stream 的可学习混合。

**MoE（Mixture of Experts）**：多个专家 + 门控，每个 token 只激活 top-k 个。

**EP（Expert Parallelism）**：把专家分散到多张卡。

**EPLB（Expert Parallel Load Balancing）**：给热点专家分配冗余副本，并把物理专家均衡放到各 rank。

**DP（Data Parallelism）**：每张卡有完整模型，处理不同请求。

**TP（Tensor Parallelism）**：把同一个矩阵乘的输出维（列并行）或输入维（行并行）切到多张卡；列并行用 all-gather，行并行用 all-reduce。

**CP（Context Parallelism）**：把序列切到多张卡，attention 跨卡交换 K/V。

**PD 分离**：Prefill 和 Decode 部署到不同实例。

**EPD 分离**：再加 Vision Encoder 阶段。

**Pixel-unshuffle**：把 `u×u` 邻域重排成通道，空间尺寸缩小 `u` 倍、通道数增加 `u²` 倍；DeepSeek-ViT 用 3×3，把 1024×1024 图片的 patch 网格从 64×64 降到 22×22。

**图像 token 预算**：一张图片经过 patch、unshuffle、池化和 aligner 后进入 LLM 的 token 数；它决定 prefill 和 KV，而不是原始 patch 数。

**Wide-EP**：大规模专家并行，常见于 decode，EP64 到 EP128。

**MTP（Multi-Token Prediction）**：一次预测多个 token 的模型头。

**Mega MoE**：把 dispatch、专家 GEMM、combine 融合的 MoE kernel。

## A.3 推测与约束

**Speculative Decoding**：draft 模型提议多个 token，目标模型并行验证。

**DSpark**：DeepSeek 的推测解码方案，含半自回归 draft、Confidence Head、可变长验证。

**Confidence Head**：预测每个 draft token 被接受概率的模块。

**Acceptance Rate**：被接受的 draft token 数除以验证的 draft token 数。

**Expected Accepted Length**：一次推测 step 期望产出的 token 数。若 `a_i` 是第 i 个 draft 的逐位置接受概率，则 `E = 1 + a1 + a1*a2 + ...`；若 `c_i` 是前 i 个都被接受的累计概率，则 `E = 1 + c1 + c2 + ...`，两种写法等价。

**Structured Output**：用文法约束生成，使输出符合 regex/JSON Schema/EBNF。

**Grammar / Automaton**：描述合法 token 序列的形式语言；regex 用 NFA/DFA，JSON/EBNF 用更复杂的文法。

**Token Mask**：把非法 token 的 logits 置为负无穷的位图。

**xgrammar**：DeepInfer 使用的文法编译/匹配库。

**Trigger Token**：grammar 开始生效前必须出现的 token 序列。

**Bound Token**：出现后强制走完 trigger 序列的 token。

**Rollback**：推测解码拒绝 draft 后，把 KV/grammar 状态退回到已接受前缀。

**Reasoning Budget**：一个请求允许用于思考的 token 上限，通常还包含答案上限和最小答案保留；防止模型把预算全部花在思考上。

**Thinking / Reasoning Channel**：推理模型输出中与最终答案分离的思考流；API 常用 `reasoning_content` 与 `content` 两个字段，流式解析要处理标记跨 chunk。

**Over-thinking / Under-thinking**：思考预算过大导致延迟和成本上升、甚至引入错误假设；预算过小导致漏掉关键步骤。评测要画预算-准确率曲线。

## A.4 精度与数值

**FP8 E4M3**：1 字节，4 位指数、3 位尾数，适合权重和激活。

**FP8 E5M2**：1 字节，5 位指数、2 位尾数，动态范围更大、精度更低。

**FP4 E2M1**：0.5 字节，2 位指数、1 位尾数，只有 16 个可表示值。

**MXFP4**：每 32 个值共享一个 ue8m0 scale（2 的幂）。

**NVFP4**：每 16 个值共享一个 E4M3 scale。

**Scale**：把浮点张量映射到低精度可表示范围的系数；有 per-tensor、per-channel、per-block 等粒度。

**Batch Invariance**：同一请求在不同 batch 组成下输出一致。

**Deterministic Sampling**：相同输入、参数、seed 下输出一致。

**PPL（Perplexity）**：语言模型的困惑度，常用于数值 baseline，不直接代表任务质量。

## A.5 系统与运维

**ZOS（Zero-Overhead Scheduling）**：把 CPU 调度/后处理与 GPU 执行重叠的流水线。

**CUDA Graph**：把一组 kernel 捕获成图，用一次 replay 启动，减少 launch 开销。

**Stream**：CUDA 中按顺序执行的操作队列；不同 stream 可以并发。

**Event**：CUDA 中用于 stream 间同步或计时的标记。

**TMA（Tensor Memory Accelerator）**：Hopper/Blackwell 的多维异步拷贝单元。

**Warp Specialization**：把 warp 分成 producer/consumer/epilogue 等角色，形成流水线。

**Persistent Kernel**：启动固定数量 block，循环处理多个 tile，减少 launch 和复用权重。

**Split-K**：把 GEMM 的 K 维切分并行，最后归约，适合小 M/N 大 K。

**Grouped GEMM**：把多个不同形状的 GEMM 合并成一个 kernel，常用于 MoE。

**3FS**：DeepSeek 的分布式文件系统，用于 KV cache 等存储。

**TTL（Time To Live）**：缓存条目的存活时间。

**LRU（Least Recently Used）**：淘汰最久未使用的条目。

**COW（Copy-on-Write）**：共享块被写入时复制一份，保护其他引用者。

**GDR（GPUDirect RDMA）**：网卡直接读写 GPU 显存，绕过 CPU/SM。

**EC（Erasure Coding）**：纠删码，用冗余分片容忍节点故障。

**SLO（Service Level Objective）**：服务等级目标，例如 P95 TTFT < 800 ms。

**Error Budget**：SLO 允许的失败/不达标预算，用来平衡发布速度和可靠性。

**TCO（Total Cost of Ownership）**：三年全成本，含硬件、电力、网络、存储、人力。

**Grace Period**：实例关机前等待/转发未完成请求的窗口。

**EP Size Re-determination**：GPU 故障后，按健康 GPU 数重新确定 EP 规模。

**Drain**：停止接收新请求、等待 running 请求完成的优雅退出过程。

**Wave（离线批处理）**：离线调度的一次准入单元；wave 内的请求仍由引擎做连续批处理，wave 之间按优先级、截止时间和成本排序。

**Makespan**：一个离线批次从开始到全部完成的时间；离线服务的目标通常是最小化 makespan 或截止时间错过率，而不是最小化单请求 TTFT。

**Backfill**：先调度大请求，再用小请求填它等待窗口的调度策略；前提是不推迟大请求的截止时间。

**Checkpoint / Shard**：离线批处理的可恢复单元；checkpoint 记录请求或 shard 的状态和结果，失败时只重跑未完成部分。

**Idempotency**：同一个请求 id 重跑多次仍得到可识别、可对账的结果；离线重试和计费依赖它。

## A.6 安全与评测

**特殊 token 注入**：用户文本伪装成控制 token，试图改变角色或绕过模板。

**Schema DoS**：用超大/超深/递归的 schema 耗尽编译资源。

**前缀侧信道**：通过缓存命中的延迟推断其他请求的前缀是否存在。

**租户隔离**：不同租户的 KV、日志、配额、缓存互不可见。

**Judge**：用模型或人工给生成结果打分；需要校准和去偏。

**Pass@k**：生成 k 个样本，至少一个通过测试的概率/比例。

**Canary / 灰度**：先给小流量，观察指标，再逐步放量。

**回归门禁**：发布前必须通过的自动化检查，例如 PPL、任务分、延迟阈值。

## A.7 工程、部署与商业

**Manifest**：把模型权重、tokenizer、chat template、grammar、dtype、量化方式和引擎版本绑定在一起的清单，用哈希和版本号支持启动校验与回滚。

**Fingerprint**：对 manifest 的规范化内容做哈希得到的稳定身份；内容相同则 fingerprint 相同，与 JSON 排版无关。

**Token Bucket**：一种限流算法，桶按固定速率补充令牌，请求消耗令牌；支持突发，常用于请求/分钟和 token/分钟的配额。

**Prefix Affinity**：把相同前缀的请求路由到同一实例，以提高前缀缓存命中率；代价是负载可能倾斜。


**PDB（PodDisruptionBudget）**：Kubernetes 中保证节点维护/驱逐时至少保留多少副本的策略。

**HPA（Horizontal Pod Autoscaler）**：Kubernetes 的自动扩缩容控制器，可按 CPU、内存或自定义指标调整副本数。

**KEDA**：基于事件和外部指标（队列长度、消息数）的 Kubernetes 扩缩容组件。

**Device Plugin**：Kubernetes 中把 GPU/NPU 等扩展资源暴露给调度器的插件。

**Topology Spread Constraints**：Kubernetes 中控制 Pod 在节点/机架/区域分布的策略，常用于把 EP 组放到同一互联域。

**OpenTelemetry**：跨服务的 trace/metrics/logs 标准与 SDK；W3C trace context 用于在网关、引擎、存储之间传播请求身份。

**Span**：trace 中的一个带起止时间的操作，例如 `prefill`、`decode.forward`、`all_to_all`。

**SBOM**：Software Bill of Materials，列出产物中的所有组件、版本、许可证和来源，常用 SPDX 或 CycloneDX 格式。

**SLSA**：供应链完整性框架，用来描述构建的可信等级和来源证明。


**Unit Economics**：单位经济，通常指每百万 token 的收入、成本、毛利，以及每 GPU 小时的有效产出。


**Contribution Margin**：收入减去可变成本后的贡献毛利，用来判断每个客户/每类流量是否值得服务。


**Data Residency**：数据驻留要求，规定数据必须存储在某个国家/地区，KV 缓存和日志也可能受约束。

**Data Processing Agreement（DPA）**：数据处理协议，规定服务商如何处理客户数据、子处理者、删除和审计义务。

**Right to Erasure**：用户要求删除个人数据的权利；推理服务要覆盖 prompt、输出、日志、trace 和 KV 副本。

**Model Card**：模型的能力、训练数据概况、评测、偏差、适用场景和禁止用途的披露文档。

**Intended Use**：模型/服务的预期用途；超出范围的用途可能带来合规和质量风险。

## A.8 商业、云原生与硬件补充

**Capex / Opex**：资本支出（GPU、服务器、机房）和运营支出（电力、带宽、人力、维保）。TCO 要把两者放在同一时间尺度上摊。

**Nominal / Effective Cost**：名义成本是设备满负荷时的每 GPU 小时成本；有效成本是除以实际利用率后的成本。5.57 美元/有效 GPU 小时可能对应 3.90 美元/小时的名义成本。

**CAC（Customer Acquisition Cost）**：获取一个客户的平均成本，含销售、市场、POC 和方案投入。CAC 回收期 = CAC / 每客户每月毛利。

**POC（Proof of Concept）**：企业采购前的概念验证，通常要跑客户数据和评测；它既是销售成本，也是产品反馈来源。

**Payback Period**：投入回收期，分简单回收期（累计现金流首次转正）和折现回收期；推理服务常用“CAC 回收期”和“设备投资回收期”两个口径，前者看客户，后者看资本开支。回收期忽略回收之后的现金流，不能单独作为投资决策依据。

**WORM（Write Once Read Many）**：一次写入、多次读取的存储，常用于审计日志和合规留痕，防止事后篡改。

**SLSA / SBOM**：SLSA 是供应链完整性等级框架；SBOM 是软件物料清单，列出镜像/包里的组件和版本。

**OTLP**：OpenTelemetry Protocol，trace/metrics/logs 的统一导出协议；推理服务用它把 span 发到可观测平台。

**DCGM**：NVIDIA 的 GPU 监控工具，采集利用率、显存、温度、ECC、NVLink 等指标；Ascend 有对应的 NPU 监控工具。

**ServiceMonitor / PodMonitor**：Prometheus Operator 的自定义资源，分别按 Service 和 Pod 发现抓取目标；不是 Kubernetes 内置对象。

**CXL**：Compute Express Link，用于 CPU/GPU/加速器之间共享内存和缓存的互联标准；对推理的影响主要在 KV 分层和内存池。

**MCP（Model Context Protocol）**：把工具、资源和提示模板标准化的协议；推理服务需要把 MCP 工具 schema 编译成文法或校验器，并处理权限、审计和超时。

## A.9 多模型、检索与编译补充

**LoRA（Low-Rank Adaptation）**：用两个低秩矩阵 `A`、`B` 表示权重更新，`W' = W + (alpha/r) A B`。它把“每个微调模型一份完整权重”变成“一份基座加许多小适配器”。

**Adapter / Adapter Registry**：适配器是一组按模块组织的 LoRA 权重；registry 负责加载、校验、驻留、LRU 淘汰和 pin。被 in-flight batch 使用的适配器不能被换出。

**Rank / Alpha**：`rank` 是低秩矩阵的中间维度，`alpha` 是缩放系数；有效缩放是 `alpha/r`。把适配器 padding 到更大 rank 时，必须同步调整 `alpha`。

**Model Routing**：在上下文、质量、延迟和成本约束下选择模型；常见策略是“可行集合里选最便宜”，并显式处理无可行解。

**Cascade**：先调用便宜模型，置信度不足再升级到强模型；期望成本是 `c0 + (1-a0)c1 + ...`。接受率越高越便宜，但平均质量可能越低。

**Bi-encoder / Cross-encoder**：bi-encoder 分别编码 query 和 document，用向量相似度召回；cross-encoder 把两者拼在一起打分，成本更高但排序更准，通常只用于 rerank。

**Pooling**：把 token 级 hidden state 压成一个向量，常见有 CLS、mean、last-token；必须与训练时一致。

**ANN（Approximate Nearest Neighbor）**：近似最近邻搜索，用召回率换延迟和内存；常见结构有 IVF、HNSW、PQ。

**Recall@k / MRR / nDCG**：检索指标。Recall@k 看前 k 条是否包含相关文档；MRR 看第一个相关结果的排名；nDCG 考虑相关性等级和位置折扣。

**SSA（Static Single Assignment）**：每个张量只被定义一次的图表示，便于做死代码消除、活跃性分析和内存规划。

**Constant Folding / DCE**：常量折叠在编译期算出常量表达式；死代码消除删除对输出没有贡献的节点。

**Operator Fusion**：把多个算子合成一个 kernel，减少 launch 和 HBM 往返；融合受消费者数量、寄存器压力和共享内存限制。

**Memory Planning**：按张量活跃区间复用 buffer，降低峰值显存；需要处理对齐、碎片、in-place 和 CUDA Graph 的固定地址。

**Autotuning**：对同一算子尝试多个 kernel/ tile 配置，选最快的并缓存；代价是首次延迟和编译时间。

## A.10 训练侧、端侧、流量与财务补充

**Rollout**：强化学习后训练中，让当前策略对一批 prompt 生成回答的过程；产出是轨迹、奖励和 logprob，不是用户响应。

**Group Size（G）**：GRPO 中每个 prompt 采样的回答数。组内共享 prompt 前缀，优势按组内奖励归一化。

**GRPO（Group Relative Policy Optimization）**：用组内相对奖励代替价值模型的策略优化方法；优势为 `(r - mean)/std`。

**Advantage**：策略梯度中的相对优势；GRPO 用组内归一化奖励作为优势。

**Behavior Policy / Logprob**：生成轨迹时实际使用的策略及其对数概率；训练器用它计算重要性采样比，必须与采样分布一致。

**Weight Sync**：把训练后的新权重同步到 rollout 引擎的过程；必须原子切换版本，并使旧 KV 失效。

**Staleness**：rollout 使用的策略版本落后于训练器当前版本的步数；适度的 staleness 提高样本利用率，过大会增加训练方差。

**Partial Rollout**：在 token 或时间预算用完时挂起一条 rollout，稍后恢复；挂起期间要保留 KV、RNG、grammar 和停止条件状态。

**KV Namespace**：在 KV key 中加入模型、权重步数、tokenizer、模板和策略标识，使跨版本的缓存命中变成显式 miss。

**Edge Inference**：在手机、PC、嵌入式设备或浏览器上运行模型；batch 小、内存/功耗受限、部署碎片化。

**GGUF / k-quants**：llama.cpp 生态的模型格式和块量化方案，支持 mmap 和多种 bit 宽。

**AWQ / GPTQ**：面向 GPU 的 4-bit 权重量化方法，保留重要通道或用二阶信息减少误差。

**QAT（Quantization-Aware Training）**：训练时模拟量化误差，通常比训练后量化（PTQ）质量好。

**Ridge Point（端侧）**：设备峰值算力除以内存带宽；decode 的算术强度低于它时受带宽限制。

**J/token**：每生成一个 token 消耗的能量，端侧产品的重要指标；等于平均功率除以 tok/s。

**Thermal Throttling**：设备长时间高负载后降频；短 benchmark 报告峰值，真实使用看到持续吞吐。

**Context Shifting**：丢弃最旧的 KV 以腾出窗口，而不是重新 prefill 整个上下文；会损失远距离记忆。

**Cloud-Edge Routing**：根据隐私、延迟、网络、成本和模型能力，决定请求在端侧还是云端执行。

**Admission Control**：在请求进入队列前决定 admit/queue/reject，防止无限队列和过载。

**Little 定律（流量）**：并发 = 到达率 × 平均停留时间；用于容量和队列估算。

**Erlang C**：M/M/c 队列中请求需要等待的概率；用于多副本服务的排队估算。

**Power of Two Choices（P2C）**：随机选两个 worker，取负载较低者；低成本近似 least-loaded。

**Retry Budget**：把重试限制为成功流量的一定比例，防止故障时重试风暴。

**Circuit Breaker**：三态（closed/open/half-open）的故障隔离机制；失败率超阈值时快速失败，冷却后探测恢复。

**Hedging**：在原请求可能超时时提前发副本，取先返回者；降低尾延迟但增加负载。

**Capacity Buffer**：为峰值、故障域和目标利用率预留的容量；健康容量不等于可承接容量。

**NPV（Net Present Value）**：未来现金流按折现率折现后的总和；大于 0 表示项目创造价值。

**IRR（Internal Rate of Return）**：使 NPV 为 0 的折现率；多次变号现金流可能有多个解。

**LTV（Lifetime Value）**：客户生命周期内贡献的毛利；简单模型为 `ARPU × 毛利率 / 流失率`。

**Committed Contract**：客户承诺最低用量换取折扣的合同；价值包括承诺收入、超量和容量预留成本。

**Breakeven Utilization**：贡献覆盖固定成本所需的利用率；大于 1 表示在当前价格和成本下无法回本。

**Scenario Analysis**：对收入、成本、利用率等假设做最好/基准/最坏情景，判断下行风险。

**Rule of Forty**：收入增长率加利润率大于 40% 的经验法则，用于订阅型基础设施业务。

**MAP（Mean Average Precision）**：检索评测指标，先算每个查询的 Average Precision，再对查询取平均；对“相关结果排在前面”比 Recall 更敏感。

**IVF / HNSW / PQ**：三类近似最近邻索引。IVF 先聚类再在少数簇内搜索；HNSW 用多层可导航小世界图；PQ 把向量分段量化以压缩内存。它们都在召回率、延迟和内存之间取舍。

**Hard Negative**：与查询相似但不相关的负样本；rerank 和 embedding 评测必须包含它，否则模型在“明显不相关”的样本上虚高。

**CSE（Common Subexpression Elimination）**：公共子表达式消除，图编译 pass 的一种；把重复计算的子图合并成一个节点。

**Epilogue**：GEMM 之后的融合操作，例如加 bias、激活、缩放或写回低精度；把 epilogue 融进 GEMM 可以减少一次 HBM 往返。

**Kernel Dispatch**：按形状、dtype、硬件和并行配置选择具体 kernel 的过程；dispatch 表缺失会让某个形状回退到慢路径。

**Load Shedding**：过载时主动拒绝或丢弃低优先级请求，保住高优先级请求的 SLO；和准入控制、熔断配合使用。

**Reward Model / Verifier**：RL 或 rollout 中给样本打分的模型或程序；前者是学习出来的，后者通常是规则、单元测试或沙箱执行。

**Importance Sampling Ratio**：RL 中 `π(a|s) / μ(a|s)` 的校正因子；离策略样本的方差随比率增大而变大，需要截断或过滤。

**WACC（Weighted Average Cost of Capital）**：加权平均资本成本，现金流折现率的常用基准；风险更高的业务应使用更高的折现率。

**ARPU（Average Revenue Per User）**：每用户平均收入，通常按月计；LTV 模型的输入之一。

**Gross Margin**：毛利除以收入；推理服务要扣除 GPU、电力、带宽、存储和支持成本，不能只减 GPU。

**Tornado Chart**：把各假设对结果的影响按大小排序的条形图；用来判断“哪个变量最值得验证”。

**Aligner**：把视觉编码器的输出投影到 LLM hidden 维度的模块；它的参数量、dtype 和训练方式会影响多模态质量。

**VE（Vision Encoder）**：视觉编码器，把图片 patch 变成视觉 token/embedding；EPD 分离中的 V 阶段。

**Patch**：图片被切分后的局部小块；patch 数决定视觉塔的计算量，LLM token 数则由 unshuffle、池化和投影决定，两者不能混用。

## A.11 市场、销售与估值

**TAM（Total Addressable Market）**：所有潜在客户按你的价格付费形成的收入池；是收入口径，不是 token 口径。

**SAM（Serviceable Addressable Market）**：产品、区域、语言、合规和渠道真正能覆盖的那部分 TAM。

**SOM（Serviceable Obtainable Market）**：在一定时间内，考虑竞争和执行能力后能拿到的份额；要写清时间（第几年）和爬坡假设。

**自下而上测算**：从客户数、每客户用量、单价开始加总市场，而不是直接引用行业报告的总量。

**JTBD（Jobs To Be Done）**：客户“雇佣”产品完成的任务；决定评测指标、定价锚点和销售话术。

**采用曲线（Adoption Curve）**：描述渗透率随时间“慢启动、快扩散、饱和”的 logistic 曲线；参数是上限、拐点和速度。

**GTM（Go-To-Market）**：产品到达客户的路径，包括 PLG、销售驱动、渠道/云市场和开源 + 商业支持。

**漏斗（Funnel）**：从线索到付费的逐级转化；每一级要有进入条件、退出条件和停留时间。

**Magic Number**：净新增 ARR 除以销售市场投入；大于 1 通常表示增长引擎有效率，小于 0.5 表示投入没有转化为收入。

**NRR（Net Revenue Retention）**：扣除流失和降级、加上扩展收入后的净留存；比毛留存更能反映客户价值。

**CAC 回收期**：获客成本除以每月贡献毛利；常见目标是 12 到 18 个月以内。

**DCF（Discounted Cash Flow）**：把未来自由现金流按资本成本折现的估值方法；终值通常占大头。

**WACC（Weighted Average Cost of Capital）**：股权和债务的加权资本成本，用作 DCF 的折现率。

**终值（Terminal Value）**：显式预测期之后的持续价值；Gordon 公式为 `FCF × (1+g)/(WACC-g)`，要求 `WACC > g`。

**EV/Revenue**：企业价值除以收入；倍数随增长、毛利、留存和资本强度变化，不是常数。

**MOIC（Multiple on Invested Capital）**：退出价值除以投资额；用于衡量单笔投资的回报倍数，不含时间和税。

**收入质量**：经常性收入占比、客户集中度、毛利真实性、capex 强度和可持续性；并购和估值时比收入规模更重要。
