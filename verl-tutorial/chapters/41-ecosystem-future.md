# 第 41 章 verl 生态、版本演进与未来方向

## 41.1 从单仓库到生态

2026 年的 verl 已经不是一个单独的仓库，而是一个生态：

- **verl**：核心训练库；
- **verl-recipe**：算法 recipe（DAPO/GSPO/PRIME/SPPO/...）；
- **verl-omni**：多模态/扩散 RL；
- **verl-vla**：视觉-语言-动作/机器人；
- **verl-SpeCo**：投机解码协同训练；
- **RL-Insight**：在线可观测性；
- **uni-agent**：Agent 框架；
- **vexact**：零 mismatch 的 HF rollout。

这些项目共享 verl 的 worker/engine 抽象，但可以独立演进。选型时要看它们与核心库的版本兼容性（recipe 的 `REQUIRED_VERL.txt`）。以下列表来自公开仓库与社区记录；具体能力、维护状态与许可证以各项目 README 为准，无法核对的项目应视为线索而不是事实。

## 41.2 版本时间线（节选）

| 时间 | 事件 |
|---|---|
| 2024/08 | HybridFlow 被 EuroSys 2025 接收 |
| 2024/09 | arXiv 2409.19256 |
| 2025/03 | verl v0.3.0，DAPO 复现 |
| 2025/04 | Seed-Thinking-v1.5、VAPO |
| 2025/06 | Megatron 支持 671B/235B |
| 2025/07 | ReTool、ICML meetup |
| 2025/08 | PyTorch Expert Exchange |
| 2025/10 | PyTorch Conference |
| 2025/12 | Mind Lab 用 verl + Megatron-bridge 训练万亿参数 GRPO LoRA |
| 2026/01 | 迁移到 verl-project，recipe 独立 |
| 2026/03 | NVIDIA GTC26 |
| 2026/04 | Megatron LoRA/router replay at PyTorch Conference Europe |
| 2026/05 | uni-agent、verl-omni pre-release |
| 2026/06 | verl-SpeCo pre-release |
| 2026/07 | RL-Insight |
| 2026/08 | verl-vla、VeRL-Tinker、verl-omni v0.2.0 |
| 2026/11 | v0.9.1 |

这不是完整 changelog，只用于理解演进方向；时间线来自公开报道/社区记录，个别条目可能滞后或不准确，关键决策请查 release notes 与仓库 commit。

## 41.3 未来方向一：Agentic RL

Agent 训练需要：

- 多轮工具调用；
- 环境交互；
- 长轨迹；
- 过程奖励；
- 异步/流式；
- 可复现的沙箱。

verl 的 AgentLoop、uni-agent、sandbox fusion 是这条线。未来会看到：

- 更标准的环境接口；
- 更细粒度的过程奖励；
- 更强的工具解析/约束解码；
- 与评测/部署闭环。

## 41.4 未来方向二：异步与流式

fully async、one-step off-policy、partial rollout 已经在实验阶段。未来会继续：

- 更低偏差的 off-policy 修正；
- 更智能的 staleness 调度；
- 与推理服务的深度集成；
- 弹性资源。

难点是“吞吐收益”与“算法稳定性”的平衡。

## 41.5 未来方向三：长序列与稀疏激活

推理模型的上下文越来越长：

- 32K、128K、1M；
- 长链思考；
- 多轮工具。

技术方向：

- CP/SP/Ulysses；
- 稀疏 attention；
- KV cache 压缩/分页；
- 分层/检索；
- 长序列 reward/验证。

MoE 的 EP/容量/路由也会继续演进。

## 41.6 未来方向四：多模态与 Omni

- VLM RL（图像/视频理解）；
- 图像/视频生成 RL（扩散）；
- Omni（文本/图像/音频/视频）；
- VLA（机器人）。

难点：

- 多模态 token 对齐；
- 视觉塔训练/冻结；
- 生成式奖励；
- 仿真环境。

## 41.7 未来方向五：RL Infra 标准化

可能的方向：

- 统一的数据/轨迹协议；
- 标准的环境/工具接口；
- 跨框架的 checkpoint/权重格式；
- 可观测性标准（指标/追踪）；
- 硬件抽象（NVIDIA/AMD/Ascend）。

verl 的 `DataProto`、`AgentLoop`、checkpoint engine 已经在向“协议”方向走。

## 41.8 未来方向六：安全与对齐

RL 后训练不仅是能力，也是对齐：

- 奖励 hacking；
- 有害行为；
- 多轮工具滥用；
- 数据隐私；
- 模型窃取。

未来会有更多：

- 过程监督/可解释性；
- 红队/对抗训练；
- 安全约束/拒绝；
- 审计/合规。

## 41.9 对工程团队的含义

- 不要只押一个框架；理解架构比记 API 更重要。
- 数据/奖励/评测是长期资产，框架会换。
- 异步/多模态/Agent 是增量能力，先把同步文本 RL 做稳。
- 可观测性/容错是生产化的分水岭。
- 成本模型要前置，不要等到集群账单爆炸。

## 41.10 MiniVerl 的后续扩展

如果你要继续扩展 MiniVerl：

- 加 varlen attention；
- 加多轮 SFT；
- 加更真实的异步（线程/队列）；
- 加 PPO 的 value normalization；
- 加更多算法（PRIME/DPPO/DRO）；CISPO 已落地，可作为“配置 → flag → loss → 测试”的模板；
- 加树搜索/自博弈的最小实现；PRM 已落地在 `mini_verl/prm.py`（见 17.17、附录 K）；
- 加 profiler/可视化；
- 把 RM 从合成偏好扩展到 on-policy 数据与多 RM 集成（见 17.16、附录 X）。

LoRA 已经落地在 `mini_verl/lora.py`，标量 RM 已经落地在 `mini_verl/reward_model.py`，PRM 已经落地在 `mini_verl/prm.py`，三者都可以作为“如何把一个 verl 特性做成可测试教学模块”的模板。每加一个，都要补测试与文档，保持“可运行、可验证”。

## 41.11 未来方向七：过程奖励、树搜索与自我博弈

如果 2025～2026 年的 RL 后训练有一个最明确的方向，那就是**把稀疏的结果信号变密**：

- **PRM/过程监督**：给每一步打分，缓解长链推理的信用分配；
- **树搜索**：best-of-n、beam、MCTS 在推理时探索，把搜索轨迹反哺训练；
- **自我博弈/自奖励**：模型生成问题、互相评判、迭代提升，减少对人工标注的依赖。

这三者会改变 RL infra 的形态：

1. **rollout 不再只是“采样 N 条”**，而是一棵带价值估计的树；数据协议要能表达节点、父子关系与访问次数；
2. **reward 不再只是一次标量调用**，而是每步一次、可能带工具执行；
3. **训练数据不再只来自当前策略**，还来自搜索树、旧策略、对手模型，off-policy 校正变得更重要。

PRM 的训练与服务已经在 MiniVerl 中落地（17.17）；verl 当前的 AgentLoop/reward loop 已经提供了“多轮 + 多 reward 服务”的基础，但树搜索还不是一等公民。短期内更现实的落地是：在 controller 侧实现搜索，把展开的轨迹写成 DataProto，再交给现有 PPO/GRPO。详细设计见附录 K。

## 41.12 未来方向八：评测与数据飞轮

RL 的收益最终要落在**可验证的能力提升**上，而评测正在从“跑几个 benchmark”变成“持续的数据引擎”：

- **评测即训练信号**：验证集不只用于 early stop，还用于过滤、课程与数据配比；
- **污染控制**：训练数据与评测集的去重、时间切分、canary；
- **数据飞轮**：生成 → 过滤 → 训练 → 评测 → 再生成，每一环都要可追溯；
- **多目标评测**：正确率之外，还有成本、延迟、安全、鲁棒性、用户采纳。

工程上的含义是：**评测管线要和训练管线一样可复现**。同一个 checkpoint、同一组采样参数、同一个 parser，才能比较两次实验。把评测脚本散落在 notebook 里，是规模化之后最常见的技术债。

评测方法、基准地图与报告模板见附录 L。推理模型从数据难度到长度/熵稳定性的完整配方，见附录 Y；没有可验证答案时的奖励模型/RLAIF 链路，见附录 X。

## 41.13 未来方向九：PD 分离、数据面与弹性

如果 2025～2026 年的 RL infra 有一个共同的工程主题，那就是**把“训练”拆成可独立扩缩的阶段**：

- rollout 侧：prefill/decode 分离（PD）、多副本路由、前缀缓存亲和；
- 数据侧：TransferQueue/流式队列，把轨迹生产与消费解耦；
- 训练侧：FSDP/Megatron 的弹性 reshard、checkpoint 热迁移；
- 调度侧：按队列长度/利用率动态调整 rollout 与 train 的资源比例。

这会带来三个新的正确性问题：

1. **版本一致性**：PD 两侧、生产/消费两侧、训练/推理两侧看到的模型版本必须可比较；
2. **样本一致性**：partial rollout/队列重试不能产生重复或丢失样本；
3. **拓扑一致性**：弹性扩缩后，checkpoint 的分片布局与优化器状态必须能 reshard。

未来的框架差异可能不再只是“支持哪些算法”，而是“能否在不停止训练的情况下重新配置资源”。这也是 verl 的 checkpoint_engine、TransferQueue、fully_async/dynamic_schedule 几条线正在汇聚的方向。

## 41.14 如何跟踪 verl 的演进

verl 的迭代速度快，教程只能给“基线 + 方法”。跟踪演进时，按下面的顺序读，比刷推特有效：

1. **Release notes / tags**：先看破坏性变更（配置键、路径、checkpoint 格式）。
2. **`docs/advance/`**：新特性通常先有文档，再有 recipe；`determinism.md`、`mtp.md`、`dynamic_context_parallel.rst`、`skip_manager.rst` 都是例子。
3. **`examples/`**：能跑的脚本比文档更接近事实；看它的模型、并行、数据与超参。
4. **`tests/`**：测试固定了行为契约；`tests/trainer/ppo/test_core_algos_on_cpu.py`、`tests/checkpoint_engine/`、`tests/experimental/agent_loop/` 是行为事实的高密度区。
5. **`verl-recipe` 的 `REQUIRED_VERL.txt`**：recipe 与主库的兼容边界。
6. **PR/issue**：看设计讨论与已知限制，尤其是“尚未支持”的边界。

判断一个新特性是否可用，用四问：

| 问题 | 红灯信号 |
|---|---|
| 有测试吗？ | 只有 demo 脚本，没有 unit/e2e |
| 有文档吗？ | 只有 PR 描述 |
| 有 recipe/示例吗？ | 只有内部脚本，无法复现 |
| 有硬件/版本矩阵吗？ | 只在特定卡/特定版本可用 |

四项都绿，才适合放进生产候选；否则先在隔离环境做 spike。

升级流程：

```text
读 release notes
  -> 固定新版本（tag/commit）
  -> 跑 CPU 单测
  -> 跑目标后端的 GPU/e2e smoke
  -> 对比配置（未知键/改名）
  -> 对比 checkpoint 加载
  -> 小流量灰度
```

不要直接跟 main。main 适合“验证问题是否已修”，不适合作为生产基线。

## 41.15 DeepSeek V4：模型-系统协同设计的风向标

DeepSeek V4 的接入工作值得单独看，因为它展示了下一代模型的一个趋势：**模型结构不再假设"训练框架能自动适配"**，而是把系统约束写进模型设计，训练框架必须跟着改。

### 三个协同点

**1. 混合精度的权重表示**

DeepSeek-V4-Flash 的 dense 权重是 E4M3 FP8 + UE8M0 scale，routed experts 是 packed FP4；vLLM 首次加载时会把专家权重转成 MXFP4 或 MegaMoE 布局。训练侧（Megatron）导出的是原始 checkpoint 布局。两端的"同一份权重"表示不同，同步必须理解生命周期：

```text
prepare -> load all buckets -> finalize（重建 MXFP4/MegaMoE）
```

每桶后处理是错的；scale 反量化再量化也是错的。这类问题不会报错，只会让 log-prob 慢慢偏移。

**2. hash router 与层序**

前三层 routed layer 的专家 id 来自 `input_ids` 查表，不来自可学习 router。只拦截 learned top-k 会漏掉这三层，导致后续每一层的 router replay 错位。修复要求把 `input_ids` 传进 decoder、记录 hash 层、并显式校验层数。

这说明"层序"已经从实现细节变成模型契约的一部分。训练框架不能再假设"MoE 层就是 learned top-k 层"。

**3. 融合 kernel 的约束**

fused DSA kernel 要求每个本地 THD shard 至少包含一个 CSA window；短 shard 要先 pad 再 unpadded。MTP 关闭时 Megatron Bridge provider 也要关闭，并裁剪 CSA 元数据。这些约束不是"性能优化"，而是"不满足就跑不了"。

### 对训练框架的含义

- **模型接入不再是加载权重**：要同时实现配置转换、权重加载/保存、量化 scheme、路由记录、kernel 约束。
- **验证要分层**：先权重逐张量，再 logits/log-prob 相关，再 replay 一致性，最后 checkpoint 恢复。只跑 loss 会漏掉静默错误。
- **版本矩阵更复杂**：模型代码、Transformers、vLLM、Megatron-Core、kernel 版本互相约束，升级要一起升。
- **测试要固定层序/形状契约**：hash 层数、expert 布局、scale dtype 都应成为断言，而不是注释。

### 对使用者的建议

- 不要假设"HF 能加载"就等于"verl 能训练"；Megatron 默认走 Megatron-Bridge，provider 对架构/配置的支持是显式的，legacy registry 只是关闭 bridge 后的旧路径。
- 先用官方 recipe 跑通两个优化器 step，再改任何东西；第二步才会触发"推理侧已后处理"的同步路径。
- 把 rollout/actor log-prob 相关性做成常驻指标，而不是一次性检查。
- 遇到"生成质量下降但 loss 正常"，优先查权重同步与路由，而不是调学习率。

完整的失败模式、R2/R3、验证协议见附录 V；模型接入的通用清单见附录 U。

## 本章小结

- verl 已从单仓库演化为生态：recipe、omni、vla、SpeCo、RL-Insight、uni-agent。
- 未来方向：Agentic RL、异步/流式、长序列/MoE、多模态、标准化、安全。
- 过程奖励、树搜索、自博弈会把 rollout 从“采样”变成“搜索”，并改变数据协议。
- 评测与数据飞轮是长期资产；工程团队应把数据/奖励/评测当核心竞争力，框架只是执行层。
- DeepSeek V4 展示了模型-系统协同设计：混合精度权重、hash router、融合 kernel 都把系统约束写进模型契约。
- MiniVerl 可以继续扩展，但要保持测试与文档同步。

## 延伸阅读

- verl 博客/新闻：<https://github.com/verl-project/verl>
- verl-recipe：<https://github.com/verl-project/verl-recipe>
- 附录 K：过程奖励、树搜索与自我博弈
- 附录 L：评测与基准
- 附录 M：引擎后端与扩展
- 附录 N：安全、沙箱与多租户
- 附录 O：端到端案例研究
- 附录 U/V：模型接入与 DeepSeek V4 案例
- 下一章：商业分析 I：市场、玩家与价值链。
