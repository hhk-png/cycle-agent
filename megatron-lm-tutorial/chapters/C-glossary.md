# 附录 C 术语表

> 术语按主题分组；同一术语在不同框架/论文中可能有细微差异，正文首次出现处
> 会给出本书采用的定义。

## C.1 并行与训练系统

| 术语 | 英文/全称 | 含义 |
|---|---|---|
| DP | Data Parallel | 数据并行：每个副本处理不同 batch，梯度 all-reduce |
| TP | Tensor Parallel | 张量并行：把单层矩阵按行/列切开 |
| PP | Pipeline Parallel | 流水线并行：不同层放到不同 stage |
| CP | Context Parallel | 上下文并行：按序列维切分，处理长上下文 |
| ring attention | — | CP 的一种：KV 块沿环流动，online softmax 累积 |
| Ulysses | — | CP 的一种：两次 all-to-all 换成全序列/head 切片布局 |
| EP | Expert Parallel | 专家并行：MoE 专家分布到不同 rank |
| SP | Sequence Parallel | 序列并行：LayerNorm/dropout 等按序列切分 |
| HSDP | Hybrid Sharded Data Parallel | 节点内分片、节点间复制的混合 FSDP |
| ZeRO | Zero Redundancy Optimizer | 分片优化器状态/梯度/参数以省显存 |
| ZeRO-1/2/3 | — | 分别分片优化器状态/梯度/参数 |
| FSDP | Fully Sharded Data Parallel | PyTorch 的全分片数据并行 |
| DDP | Distributed Data Parallel | PyTorch 的分布式数据并行 |
| 1F1B | One Forward One Backward | 流水线 steady state 交替前向/反向 |
| GPipe | — | 先全部前向、再全部反向的流水线调度 |
| VPP | Virtual Pipeline Parallel | 虚拟流水线：每 stage 多个 chunk，降低气泡 |
| interleaved | — | 与 VPP 常互换使用，指 chunk 交错调度 |
| zero-bubble | — | 把权重梯度计算填进气泡的调度 |
| bubble | — | 流水线中 GPU 空闲的时间比例 |
| micro-batch | — | 一次前向/反向的最小 batch |
| global batch | — | 一个优化器 step 覆盖的总 batch |
| gradient accumulation | — | 多个 micro-batch 累积后再更新 |
| model parallel | — | TP+PP+CP+EP 的统称 |
| data parallel size | — | `world_size / (tp·pp·cp)`（EP 另算） |
| rank | — | 进程在通信域中的编号 |
| local rank | — | 节点内/组内的编号 |
| global rank | — | 整个任务内的唯一编号 |
| process group | — | 一组可互相通信的 rank |
| collective | — | all-reduce/all-gather/reduce-scatter 等集合通信 |
| p2p | Point-to-Point | send/recv，PP 常用 |
| all-reduce | — | 所有 rank 求和（或其它 op）并广播结果 |
| all-gather | — | 收集所有 rank 的分片 |
| reduce-scatter | — | 先归约再按 rank 切分 |
| all-to-all | — | 每个 rank 向所有 rank 发送不同数据，EP 常用 |
| bucket | — | 把小梯度/消息打包，减少通信次数 |
| communication overlap | — | 通信与计算重叠 |
| activation checkpointing | — | 不存中间激活，反向重算 |
| recomputation | — | 同上，activation recomputation |
| offload | — | 把状态/激活放到 CPU/NVMe |
| sharding | — | 把参数/状态切分到多个 rank |
| resharding | — | 改变并行布局后重新切分检查点 |
| MFU | Model FLOPs Utilization | 实际算力 / 硬件峰值算力 |
| HFU | Hardware FLOPs Utilization | 含重算的硬件利用率 |
| tokens/s | — | 每秒处理的 token 数 |
| time-to-train | — | 训练到目标 loss/ token 数的时间 |
| GPU-hours | — | GPU 数 × 小时，成本核算单位 |

## C.2 模型结构

| 术语 | 英文/全称 | 含义 |
|---|---|---|
| Transformer | — | 基于 self-attention 的序列模型 |
| decoder-only | — | 只有 causal decoder，GPT/Llama 类 |
| encoder-only | — | 双向 encoder，BERT 类 |
| encoder-decoder | — | T5/BART 类，cross-attention |
| attention | — | 用 Q/K/V 计算加权求和 |
| self-attention | — | Q/K/V 来自同一序列 |
| cross-attention | — | Q 来自 decoder，K/V 来自 encoder |
| causal mask | — | 禁止看到未来 token 的 mask |
| padding mask | — | 忽略 padding token 的 mask |
| document mask | — | packing 时阻止跨文档 attention |
| MHA | Multi-Head Attention | 多头注意力 |
| MQA | Multi-Query Attention | 所有 Q head 共享一组 K/V |
| GQA | Grouped Query Attention | Q head 分组共享 K/V |
| MLA | Multi-head Latent Attention | 把 K/V 压缩到低秩 latent |
| RoPE | Rotary Position Embedding | 旋转位置编码 |
| ALiBi | Attention with Linear Biases | 线性位置偏置 |
| FFN | Feed-Forward Network | 前馈网络 |
| MLP | Multi-Layer Perceptron | 多层感知机 |
| SwiGLU | — | SwiGLU 激活的门控 FFN |
| GeLU | Gaussian Error Linear Unit | 常用激活 |
| LayerNorm | — | 层归一化 |
| RMSNorm | Root Mean Square Norm | 无均值的归一化 |
| residual | — | 残差连接 |
| pre-norm/post-norm | — | LayerNorm 放在子层前/后 |
| embedding | — | token/position 的向量表 |
| tied embedding | — | 输入 embedding 与输出层共享权重 |
| LM head | — | 输出词表 logits 的线性层 |
| MoE | Mixture of Experts | 稀疏专家模型 |
| expert | — | MoE 中的一个 FFN 专家 |
| router/gate | — | 决定 token 去哪些专家的网络 |
| top-k routing | — | 每个 token 选 k 个专家 |
| capacity factor | — | 专家容量系数，控制丢弃 |
| aux loss | — | 负载均衡辅助损失 |
| token permutation | — | 把 token 按专家重排 |
| grouped GEMM | — | 把多个专家的 GEMM 合并 |
| Mamba/SSM | State Space Model | 状态空间模型，线性复杂度 |
| hybrid model | — | Transformer + SSM/MLA 等混合结构 |
| multimodal | — | 文本+图像/音频/视频 |
| vision encoder | — | 图像编码器（ViT 等） |
| projector/adapter | — | 把视觉特征映射到语言模型 |
| KV cache | — | 推理时缓存的 K/V |
| context length | — | 模型可处理的最大序列长度 |

## C.3 精度与数值

| 术语 | 英文/全称 | 含义 |
|---|---|---|
| fp32 | 32-bit floating point | 单精度 |
| fp16 | 16-bit floating point | 半精度，范围小，需 loss scaling |
| bf16 | bfloat16 | 16 位，指数范围同 fp32 |
| FP8 | 8-bit floating point | 8 位浮点 |
| E4M3 | — | 4 位指数、3 位尾数的 FP8 |
| E5M2 | — | 5 位指数、2 位尾数的 FP8 |
| FP4 | 4-bit floating point | 4 位浮点 |
| NVFP4 | NVIDIA FP4 | NVIDIA 的 4 位格式 |
| MXFP8/MXFP4 | Microscaling FP | 块级微缩放格式 |
| scaling factor | — | 量化/反量化的缩放 |
| amax | — | 张量绝对值最大值，用于定 scale |
| delayed scaling | — | 用历史 amax 估计 scale |
| current scaling | — | 每步动态计算 scale |
| per-tensor scaling | — | 整个张量一个 scale |
| per-channel/block | — | 每通道/每块一个 scale |
| loss scaling | — | 放大 loss 防止 fp16 下溢 |
| GradScaler | — | PyTorch 的 loss scaling 工具 |
| autocast | — | 自动混合精度上下文 |
| master weights | — | fp32 主权重 |
| stochastic rounding | — | 随机舍入，降低量化偏差 |
| numerical stability | — | 数值稳定性 |
| NaN/Inf | — | 非数/无穷，训练异常信号 |
| deterministic | — | 确定性/可复现 |
| bitwise reproducible | — | 逐位可复现 |

## C.4 数据与后训练

| 术语 | 英文/全称 | 含义 |
|---|---|---|
| tokenizer | — | 文本到 token 的映射 |
| BPE | Byte Pair Encoding | 字节对编码 |
| WordPiece | — | BERT 使用的子词算法 |
| Unigram | — | SentencePiece 的概率子词 |
| vocab size | — | 词表大小 |
| special token | — | BOS/EOS/PAD/UNK 等 |
| chat template | — | 对话角色/分隔符模板 |
| packing | — | 把多条样本拼进固定长度序列 |
| padded sequence | — | padding 到固定长度的样本 |
| THD | total tokens, heads, dim | 变长序列布局 |
| token shard | — | 预处理后的 token 二进制分片 |
| Energon | Megatron Energon | 面向样本/多模态的生产数据集库（tar shard + 元数据） |
| sampler | — | 决定每个 step 取哪些样本 |
| curriculum | — | 数据课程：按阶段调整分布 |
| dedup | — | 去重 |
| MinHash/LSH | — | 近似去重算法 |
| contamination | — | 评测集泄漏进训练数据 |
| SFT | Supervised Fine-Tuning | 监督微调 |
| LoRA | Low-Rank Adaptation | 低秩适配 |
| QLoRA | Quantized LoRA | 量化主干 + LoRA |
| adapter | — | 插件式微调模块 |
| RLHF | RL from Human Feedback | 人类反馈强化学习 |
| reward model | — | 偏好/奖励模型 |
| DPO | Direct Preference Optimization | 直接偏好优化 |
| IPO/KTO/ORPO | — | 其它偏好优化变体 |
| GRPO | Group Relative Policy Optimization | 组相对策略优化 |
| PPO | Proximal Policy Optimization | 近端策略优化 |
| rollout | — | RL 中策略生成样本 |
| distillation | — | 蒸馏 |
| model merging | — | 模型权重合并 |

## C.5 推理与服务

| 术语 | 英文/全称 | 含义 |
|---|---|---|
| prefill | — | 处理 prompt，生成首个 token |
| decode | — | 自回归逐 token 生成 |
| TTFT | Time To First Token | 首字延迟 |
| TPOT | Time Per Output Token | 每输出 token 时间 |
| ITL | Inter-Token Latency | token 间延迟 |
| throughput | — | 吞吐（tokens/s） |
| goodput | — | 满足 SLO 的有效吞吐 |
| p50/p99 | — | 延迟分位数 |
| continuous batching | — | 动态加入/移除请求的批处理 |
| static batching | — | 固定 batch 等最慢请求 |
| chunked prefill | — | 把长 prefill 分块 |
| prefix caching | — | 复用相同前缀的 KV |
| PagedAttention | — | 分页管理 KV cache |
| paged KV cache | — | 按 block 分配的 KV cache |
| speculative decoding | — | 用小模型草稿+大模型验证加速 |
| PD 分离 | Prefill/Decode Disaggregation | 预填充与解码分开部署 |
| KV offload | — | 把 KV cache 换出到 CPU/存储 |
| quantization | — | 量化推理 |
| AWQ/GPTQ/FP8 | — | 常见推理量化方案 |
| serving | — | 模型服务/部署 |
| SLO | Service Level Objective | 服务等级目标 |
| SLA | Service Level Agreement | 服务等级协议 |

## C.6 硬件、通信与集群

| 术语 | 英文/全称 | 含义 |
|---|---|---|
| GPU | Graphics Processing Unit | 图形处理器/加速卡 |
| SM | Streaming Multiprocessor | GPU 计算单元 |
| HBM | High Bandwidth Memory | GPU 高带宽显存 |
| NVLink | — | NVIDIA GPU 高速互联 |
| NVSwitch | — | 节点内 GPU 全互联交换 |
| IB | InfiniBand | 高性能节点间网络 |
| RoCE | RDMA over Converged Ethernet | 以太网上的 RDMA |
| NCCL | NVIDIA Collective Communications Library | NVIDIA 集合通信库 |
| Flight Recorder | — | PyTorch 的 NCCL 集合通信环形记录器，超时可落盘 |
| Gloo | — | CPU/通用集合通信后端 |
| topology | — | 网络/GPU 拓扑 |
| rail-optimized | — | 按网络 rail 优化布局 |
| fat-tree | — | 胖树网络拓扑 |
| dragonfly | — | 蜻蜓网络拓扑 |
| bandwidth | — | 带宽 |
| latency | — | 延迟 |
| algbw/busbw | — | NCCL 算法带宽/总线带宽 |
| ring all-reduce | — | 环形 all-reduce |
| tree all-reduce | — | 树形 all-reduce |
| straggler | — | 拖后腿的慢节点/慢 rank |
| ECC | Error Correcting Code | 显存纠错 |
| PUE | Power Usage Effectiveness | 数据中心能效 |
| capex/opex | — | 资本支出/运营支出 |
| TCO | Total Cost of Ownership | 总拥有成本 |

## C.7 评测、实验与商业

| 术语 | 英文/全称 | 含义 |
|---|---|---|
| benchmark | — | 基准测试 |
| held-out set | — | 留出集 |
| few-shot | — | 少样本评测 |
| pass@k | — | k 次采样至少一次正确的概率 |
| perplexity | — | 困惑度 |
| scaling law | — | 规模律 |
| Chinchilla-optimal | — | 计算最优的 token/参数配比 |
| ablation | — | 消融实验 |
| statistical significance | — | 统计显著性 |
| bootstrap | — | 自助法估计置信区间 |
| multiple comparisons | — | 多重比较校正 |
| seed variance | — | 随机种子方差 |
| reproducibility | — | 可复现性 |
| experiment tracking | — | 实验管理/追踪 |
| runbook | — | 运维手册 |
| incident | — | 事故 |
| postmortem | — | 复盘 |
| RTO/RPO | Recovery Time/Point Objective | 恢复时间/恢复点目标 |
| TAM/SAM/SOM | — | 总市场/可服务市场/可获得市场 |
| unit economics | — | 单位经济模型 |
| gross margin | — | 毛利率 |
| lock-in | — | 供应商锁定 |
| open source | — | 开源 |
| Apache-2.0 | — | 常见宽松开源许可证 |
| fork | — | 代码分叉 |
| MFU 口径 | — | 峰值算力按精度/稀疏性/是否含重算的定义 |

## C.8 2026 前沿与易混术语

| 术语 | 英文/全称 | 含义 | 易混点 |
|---|---|---|---|
| MLA | Multi-head Latent Attention | 缓存低秩 latent + 共享 RoPE key 的注意力 | 不是 GQA；cache 不是完整 K/V |
| latent KV | — | MLA 的 `c_kv` 压缩表示 | 每 token `d_c` 维，不是 `2·n_h·d` |
| absorption | — | 推理时把 `W_UK`/`W_UV` 吸进 query/output | 训练权重不要预先吸收 |
| M-RoPE | Multimodal RoPE | 时间/高/宽三维位置编码 | 文本 token 三维取同值 |
| MTP | Multi-Token Prediction | 一次预测多个未来 token 的辅助头 | 既是训练目标也可当草稿头 |
| ETP | Expert Tensor Parallel | 专家内部再按 TP 切 | 与 EP 正交；延迟更高 |
| EPLB | Expert Parallel Load Balancer | 按负载调整专家→rank 映射 | 重排要搬权重；影响 batch invariance |
| wide EP | — | EP 跨越多个节点、专家数数百 | all-to-all 尾延迟是核心 |
| PD 分离 | Prefill/Decode Disaggregation | prefill 与 decode 分集群 | KV 传输量可能抵消收益 |
| NIXL | NVIDIA Inference Xfer Library | KV/张量传输库 | 不是模型格式 |
| SDC | Silent Data Corruption | 静默计算错误 | 不报错、只表现为 loss 漂移/NaN |
| DSA/NSA | DeepSeek Sparse Attention / Native Sparse Attention | 稀疏/混合注意力 | 与 CP/长上下文配合 |
| HybridEP | — | 混合专家并行的调度/通信优化 | 具体实现随版本变化 |
| goodput | — | 满足 SLO 的有效吞吐 | 不是总 tokens/s |
| E1–E4 | — | 本书的证据分级（代码存在/可运行/数值对齐/生产验证） | 见附录 J |
| L1–L4 | — | 本书的发布分级（已发布/已合并/路线图/传闻） | 见第 36.1 节 |
| MoE 激活参数 | active parameters | 每 token 实际参与的专家参数 | 与总参数不同；FLOPs 用它 |

## C.9 商业、成本与组织

| 术语 | 英文/缩写 | 含义 | 常见误用 |
|---|---|---|---|
| TCO | Total Cost of Ownership | 3–5 年总拥有成本 | 只算 GPU 单价 |
| capex / opex | — | 资本支出/运营支出 | 把租赁当 capex |
| 折旧 | depreciation | 硬件按年限摊销 | 忽略残值/代际贬值 |
| WACC | Weighted Average Cost of Capital | 加权平均资本成本 | 用它替代现金流 |
| NPV / IRR | — | 净现值/内部收益率 | 只看回本期 |
| PPA | Power Purchase Agreement | 长期购电协议 | 忽略照付不议条款 |
| PUE | Power Usage Effectiveness | 机房总功耗/IT 功耗 | 用 1.1 的理想值做预算 |
| neocloud | — | 专注 GPU 的云服务商 | 与超大规模云混为一谈 |
| capacity block | — | 预留 GPU 容量块 | 忽略闲置/承诺期 |
| sovereign AI | — | 国家/地区的自主 AI 能力 | 只谈硬件，忽略数据/合规 |
| gross margin | — | 推理服务的毛利 | 用峰值吞吐算成本 |
| goodput | — | 满足 SLO 的有效吞吐 | 与 tokens/s 混用 |
| SLA | Service Level Agreement | 可用性/延迟承诺 | 没有罚则的 SLA |
| 出口管制 | export control | 芯片/技术出口限制 | 只看型号不看算力密度 |
| 数据驻留 | data residency | 数据存储/处理地域 | 与“数据本地化”混用 |

## C.10 按章节找术语

| 想查 | 先看 |
|---|---|
| TP/SP/PP/DP/CP/EP 的定义与公式 | 05–10、附录 A |
| 显存/FLOPs/MFU/通信账 | 04、14、附录 I |
| mini 的类/命令/配置 | 15–28、附录 B/F |
| 生产代码地图/模型家族/数据/集群 | 29–33 |
| 前沿（MoE/MLA/FP8/FP4/Muon/长上下文） | 34–36 |
| 硬件/推理/评测/故障 | 37–42 |
| 市场/TCO/选型/开源/组织 | 43–47 |
| 端到端/kernel/优化器/迁移 | 48–51 |
| 声明分级/转换/练习 | 附录 J/K/L |

---

上一附录：[附录 B 配置速查](B-config-reference.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 D 故障排查清单](D-troubleshooting.md)
