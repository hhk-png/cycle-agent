# 附录 B 术语表

| 术语 | 英文 | 含义 |
|---|---|---|
| 策略 | policy | 给定状态输出动作分布的模型 |
| 轨迹 | trajectory | 一次完整的 prompt + 生成序列 |
| 优势 | advantage | 动作相对基线的好坏 |
| 回报 | return | 从某时刻开始的折扣奖励和 |
| GAE | Generalized Advantage Estimation | 用 TD 误差指数加权估计优势 |
| PPO | Proximal Policy Optimization | 用重要性比率裁剪约束更新 |
| GRPO | Group Relative Policy Optimization | 用同 prompt 组内基线替代 critic |
| DAPO | Decoupled clip and Dynamic sAmpling Policy Optimization | GRPO + clip-higher + 动态采样 + token loss + overlong penalty |
| GSPO | Group Sequence Policy Optimization | 序列级重要性比率 |
| RLVR | RL with Verifiable Rewards | 用可程序验证的奖励做 RL（数学/代码等） |
| verifiable reward | — | 由规则/测试/工具判定的奖励 |
| ORM | Outcome Reward Model | 对整个 response 打一个标量的结果奖励模型 |
| PRM | Process Reward Model | 对每个推理步骤打分的过程奖励模型 |
| step label | — | 每个步骤的监督标签；`local` 只标出错步，`suffix` 把错误传播到后续 |
| prefix scoring | — | 第 k 步只根据 prompt + 前 k 步打分，不能看到未来 |
| step aggregation | — | 把逐步分数合成轨迹分数：last/first/min/max/mean/product |
| potential-based shaping | — | 形如 γΦ(s')−Φ(s) 的奖励塑形，不改变最优策略 |
| PRM-only reward | — | 只用 PRM 分数做 RL；容易被策略 reward hacking |
| reward hacking | — | 策略学会骗奖励而非真正完成任务 |
| clip-higher | — | DAPO 的非对称裁剪，上界比下界宽 |
| overlong penalty | — | 对超长/截断响应的惩罚 |
| Dr.GRPO | — | 去掉 GRPO 标准差归一化的变体 |
| CISPO | Clipped IS Policy Optimization | 裁剪重要性比率并 detach 成权重，梯度走 log pi |
| GPG | Group Policy Gradient | 组归一化优势 + 简化策略梯度 |
| SAPO | Smooth Advantage Policy Optimization | 平滑优势/比率加权的损失变体 |
| DRO | Distributionally Robust Optimization | 对分布偏移更保守的重加权目标 |
| DPPO | Decoupled PPO | 用 TV/KL 信任域替代/补充比率裁剪 |
| OPD | On-Policy Distillation | 在 student 自己采样的轨迹上做 teacher 监督 |
| OTB | Optimal Token Baseline | token 级基线，降低稀疏奖励方差 |
| SPIN | Self-Play Fine-Tuning | 用真实答案与自生成样本构造偏好对 |
| VAPO | Value-based Augmented PPO | 价值预训练/长度自适应 GAE 等工程组合 |
| PRIME | — | 隐式过程奖励/在线偏好学习 |
| SPPO | Self-Play Preference Optimization | 自博弈偏好优化 |
| RLOO | REINFORCE Leave-One-Out | 用其余样本均值做基线 |
| ReMax | — | 用 greedy 奖励做基线 |
| KL | Kullback-Leibler divergence | 两个分布的差异 |
| k1/k2/k3 | — | KL 的三种估计量 |
| entropy | — | 策略分布的熵，鼓励探索 |
| clipfrac | — | 被裁剪的 token 比例 |
| ppo_kl | — | 当前策略与 old 策略的 KL |
| rollout | — | 用当前策略生成轨迹 |
| prefill | — | 处理 prompt 的阶段 |
| decode | — | 逐 token 生成的阶段 |
| KV cache | — | 缓存的 key/value，避免重复计算 |
| prefix caching | — | 复用相同前缀的 KV |
| continuous batching | — | 动态把新请求加入 batch |
| PagedAttention | — | 分页管理 KV cache |
| chunked prefill | — | 把长 prefill 分块 |
| preemption | — | KV 不足时驱逐/换出序列 |
| TTFT | Time To First Token | 首 token 延迟 |
| ITL | Inter-Token Latency | decode 的 token 间隔 |
| speculative decoding | — | 小模型提案、大模型验证的加速方法 |
| PD 分离 | Prefill-Decode disaggregation | prefill 与 decode 分资源 |
| FSDP | Fully Sharded Data Parallel | 参数/梯度/优化器分片 |
| FSDP2 | — | 基于 DTensor 的 FSDP |
| ZeRO | — | DeepSpeed 的分片策略 |
| TP | Tensor Parallel | 张量并行 |
| PP | Pipeline Parallel | 流水线并行 |
| CP | Context Parallel | 上下文/序列并行 |
| EP | Expert Parallel | MoE 专家并行 |
| SP | Sequence Parallel | 序列并行 |
| DP | Data Parallel | 数据并行 |
| HSDP | Hybrid Sharded Data Parallel | 分片+复制混合 |
| DTensor | Distributed Tensor | PyTorch 的分布式张量 |
| all-gather | — | 收集所有 rank 的分片 |
| reduce-scatter | — | 归约并散射 |
| all-reduce | — | 归约并广播 |
| all-to-all | — | 全交换（MoE/SP） |
| NCCL | NVIDIA Collective Communications Library | GPU 集合通信库 |
| HCCL | Huawei Collective Communication Library | 华为集合通信库 |
| Ray | — | 分布式执行框架 |
| placement group | — | Ray 的资源预留单位 |
| actor | — | Ray 的有状态进程 |
| worker | — | 执行计算的逻辑进程 |
| WorkerGroup | — | 一组 worker + dispatch |
| dispatch | — | 把调用分发到 worker |
| collect | — | 收集 worker 结果 |
| DataProto | — | verl 的批数据协议 |
| TensorDict | — | 带 batch 维的张量字典 |
| meta_info | — | DataProto 的元数据 |
| non_tensor_batch | — | 非张量字段 |
| old_log_prob | — | 采样时策略的 log-prob |
| ref_log_prob | — | 参考模型的 log-prob |
| rollout_log_probs | — | rollout 引擎记录的 log-prob |
| values | — | critic 的价值估计 |
| returns | — | GAE 回报 |
| response_mask | — | 哪些 token 参与策略损失 |
| attention_mask | — | 哪些 token 是真实输入 |
| position_ids | — | 位置编码 |
| cu_seqlens | — | varlen 的序列边界 |
| remove padding | — | 去掉 padding 的 varlen 布局 |
| dynamic bsz | — | 按 token 预算动态组 batch |
| micro-batch | — | 一次前向/反向的小批 |
| gradient accumulation | — | 多个 micro-batch 累加梯度 |
| mini-batch | — | 一次 PPO 更新的批 |
| global batch | — | 所有 DP rank 的合计批 |
| loss aggregation | — | token/sequence 级归一化 |
| reward manager | — | 把奖励放到 token 的组件 |
| reward model | — | 学习出来的标量奖励模型 |
| Bradley-Terry | — | 用分数差建模成对偏好的概率模型 |
| pairwise accuracy | — | 奖励模型在 chosen/rejected 对上的排序准确率 |
| reward overoptimization | — | 代理奖励继续涨、真实奖励下降的过优化现象 |
| calibration | — | 让预测分数与真实概率一致（ECE/温度缩放） |
| position bias | — | judge/RM 偏好第一个或最后一个候选 |
| length bias | — | RM 把“更长”误学成“更好” |
| RLAIF | RL from AI Feedback | 用 AI judge/规则代替人类偏好 |
| Constitutional AI | — | 用一组原则做自我批评/改写与偏好标注 |
| LLM-as-judge | — | 用 LLM 生成判断与分数 |
| ORM | Outcome Reward Model | 只看最终结果的奖励模型 |
| PRM | Process Reward Model | 给每个推理步骤打分的模型 |
| GenRM | Generative Reward Model | 生成解释与分数的奖励模型 |
| rubric | — | 可检查的评分条目 |
| verifier | — | 规则验证器 |
| sandbox | — | 隔离的代码执行环境 |
| pass@k | — | k 次采样中至少一次正确的概率 |
| avg@k | — | k 次采样的平均正确率 |
| maj@k | — | k 次采样多数投票后的正确率 |
| MCTS | Monte Carlo Tree Search | 蒙特卡洛树搜索 |
| UCT | Upper Confidence bounds applied to Trees | MCTS 的选择公式 |
| self-play | — | 模型与自身/自身副本对抗或互评 |
| data flywheel | — | 生成→过滤→训练→评测的循环 |
| SFT | Supervised Fine-Tuning | 监督微调 |
| DPO | Direct Preference Optimization | 直接偏好优化 |
| ORPO | — | 结合 SFT 与偏好优化 |
| distillation | — | 用 teacher 监督 student |
| LoRA | Low-Rank Adaptation | 低秩适配 |
| QLoRA | — | 量化基座 + LoRA |
| QAT | Quantization-Aware Training | 量化感知训练 |
| NVFP4 | NVIDIA FP4 | 4-bit 浮点（E2M1 + block scale） |
| ROCm | Radeon Open Compute | AMD 的 GPU 计算平台 |
| RCCL | ROCm Collective Communications Library | AMD GPU 集合通信库 |
| MoE | Mixture of Experts | 混合专家 |
| router | — | MoE 的专家选择器 |
| capacity factor | — | 每个专家的 token 容量 |
| router replay | — | 复用路由决策，保证重算/并行下一致 |
| hash router | — | 专家 id 由 input_ids 查表决定、不可学习的 router |
| R2 | — | actor log-prob 记录、actor 更新重放的路由模式 |
| R3 | — | rollout 后端记录、训练后端重放的路由模式 |
| MXFP4 | Microscaling FP4 | 带 block scale 的 4-bit 浮点布局 |
| UE8M0 | Unsigned E8M0 | 只表示 2 的幂的 8-bit scale |
| prepare/finalize | — | 分桶权重同步前后的全局钩子，必须各执行一次 |
| MTP | Multi-Token Prediction | 多 token 预测头/投机解码 |
| RoPE | Rotary Position Embedding | 旋转位置编码 |
| dynamic CP | Dynamic Context Parallel | 按 token 负载动态切分上下文并行 |
| sleep/wake | — | 释放/恢复推理显存 |
| checkpoint engine | — | 权重同步后端 |
| delta sync | — | 只同步变化的参数 |
| reshard | — | 训练布局 ↔ 推理布局转换 |
| model merger | — | 分片 checkpoint 合并 |
| safetensors | — | 安全的张量文件格式 |
| staleness | — | 样本版本与当前策略的差距 |
| partial rollout | — | 保存进行中的生成 |
| importance sampling | — | 用比率修正分布差异 |
| TransferQueue | — | verl 的数据队列 |
| AgentLoop | — | 多轮/工具调用循环 |
| tool call | — | 模型调用外部工具 |
| observation | — | 工具返回 |
| ToolRegistry | — | 工具注册表，合并 native/function 工具并拒绝重名 |
| FunctionTool | — | 由普通 Python 函数生成 schema 的工具 |
| BaseTool | — | 带 create/execute/calc_reward/release 生命周期的工具基类 |
| sandbox fusion | — | 把代码执行放到远程沙箱服务的模式 |
| separation | — | 训练与推理使用独立资源池的部署形态 |
| DetachActorWorker | — | 把 actor 模型 detach 到 CPU/restore 回 GPU 的 worker |
| GRPO group | — | 同一 prompt 的 n 条样本 |
| on-policy | — | 用当前策略采样并训练 |
| off-policy | — | 用旧策略样本训练 |
| MFU | Model FLOPs Utilization | 算力利用率 |
| TCO | Total Cost of Ownership | 总拥有成本 |
| ROI | Return on Investment | 投资回报 |
