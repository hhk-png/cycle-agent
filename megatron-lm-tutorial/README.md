# Megatron-LM 深度教程

> 从单卡 Transformer 到万卡训练系统：原理、工程实现、生产栈、前沿与商业分析  
> 版本基线：Megatron Core **0.20.x**（2026 年 11 月），mini-megatron `0.4.4`

这是一套“可读、可跑、可对照”的 Megatron-LM 教程。它不只解释论文和 API，
还在 `mini_megatron/` 中实现了一个**工程化、可运行、经过数值等价性验证**的简化版：

- 张量并行（Column / Row Parallel Linear、词表并行 Embedding）
- 序列并行（LayerNorm/Dropout 的 sequence-sharded 布局）
- 流水线并行（GPipe、1F1B、前向 `isend` + 同步 recv/反向 send、global-rank
  寻址）
- **可执行的 VPP**（interleaved GPipe：chunk 归属、环形 p2p、(mb,chunk) 激活）
- 数据并行（梯度 all-reduce、梯度裁剪、ZeRO-1 优化器状态分片）
- **flat distributed optimizer**（连续 buffer、reduce-scatter 梯度、
  all-gather 参数、DP 分片 AdamW、检查点）
- **专家并行（EP）**（专家切分、变长 all-to-all dispatch/combine、容量、
  全局 aux、专家梯度缩放）
- 上下文并行（KV all-gather 的正确因果 mask、K/V 梯度聚合）
- **ring attention 参考实现**（online softmax、全局因果 mask、块负载统计、
  反向梯度与 dense 一致）
- **Ulysses 参考实现**（两次 all-to-all 布局变换、精确逆置换、全序列
  attention、通信量公式，与 dense 前向/反向一致）
- MoE（top-k 路由、capacity、负载均衡辅助损失、TP 专家；复制版 + EP 版）
- **aux-free 负载均衡与 node-limited routing**（DeepSeek 风格 bias 更新、
  每 token 节点数约束）
- 词表并行交叉熵（全局 max/sum log-sum-exp、ignore_index、CP 全局归一化；
  计算在 fp32 完成）
- 混合精度 autocast、FP8 数值模拟、激活重计算
- **MLA（Multi-head Latent Attention）独立参考实现**：低秩 query、latent
  KV cache、解耦 RoPE、prefill/decode 数值一致，含 6 项单测
- **paged KV cache**（block allocator、block table、append/gather/free、
  碎片统计，与 dense cache 等价）
- 分 rank 检查点（同布局恢复）、断点续训、文本生成、KV cache 解码、单测与
  多进程等价性测试
- 一阶代价估算（参数/显存/FLOPs/通信）、packing/document mask、
  VPP 规划器 + 执行器、模型权重与 AdamW 状态重分片工具
- 附录 A–N：数学、配置、术语、排障、参考、API、确定性、runbook、硬件/
  成本、声明登记、转换映射、练习、源码阅读实验、故障注入实验

教程覆盖“今天怎样用 Megatron”“它内部怎样实现”“明天往哪里走”“怎样落地”，
最后用四章讨论市场、TCO、竞争格局和商业化，再用一章讨论从实验到产品的组织与决策。

## 0. 本版更新（2026-11-22，第九版修订）

**新增可运行实现**

- `parallel/ulysses.py`：Ulysses 上下文并行参考实现。用两次 all-to-all 的
  布局变换（`[B,H,S/P,D] ↔ [B,H/P,S,D]`）把全序列 attention 变成普通
  head-切片 attention；含精确逆置换检查、全局因果 mask、反向量、通信量
  统计。`tests/unit/test_ulysses.py` 7 项：cp=1/2/4 与 dense 前向/反向一致、
  因果性（改未来 token 不影响过去）、布局往返、不可整除报错、通信公式；
  `scripts/ulysses_demo.py` 打印误差/往返/通信量。
- 新增 [9.2.4 Ulysses](chapters/09-context-parallel.md)：三种 CP 实现的
  资源账对比（all-gather KV / ring / Ulysses）、`2/P` 通信量推导、GQA 下
  `H_kv` 整除限制、选型表。附录 A.21 给出完整公式。

**新增内容**

- [34.3.2 MTP：辅助头、loss 与并行布局](chapters/34-frontier-2026.md)：
  token 偏移、共享 embedding/输出头、`λ/D` 加权、计算/显存账、PP 最后
  stage 放置、CP 下 `t_{i+k}` 的跨 rank 偏移、推理侧草稿一致性；对齐
  `core/transformer/multi_token_prediction.py` 的类名。
- [38.8.2 Megatron Energon](chapters/38-data-engineering.md)：样本级索引、
  shard/元数据、worker 解码、混合/packing、可恢复状态、五个生产坑与验收
  清单；区分内置 `.bin/.idx` 与 Energon。
- [33.2.2 NCCL Flight Recorder 与“先取证再杀”](chapters/33-debugging-and-observability.md)：
  环形缓冲区、超时落盘、按 collective 序号对齐、与 `NCCL_DEBUG`/Xid/IB
  计数的配合。
- [23.7.1 一个 step 失败了怎么办](chapters/23-training-loop.md)：OOM 重试
  三个不变量、NCCL 超时不可原地重试、SIGTERM/抢占、检查点原子性与重试
  可观测性。
- [5.9.1 梯度压缩](chapters/05-data-parallel.md)：低精度、PowerSGD、稀疏化
  的收益/代价、误差反馈、何时值得做与验收协议。
- [40.4.5 流式推理与 attention sink](chapters/40-inference-systems.md)：
  sink + 滑动窗口、分页对齐、位置编码、prefix cache 交互与验收。
- [12.4.2 QAT 与剪枝](chapters/12-mixed-precision-fp8-fp4.md)：fake-quant、
  直通梯度、PTQ→QAT→蒸馏顺序、结构化/2:4 稀疏的真实收益边界。
- [41.7.1 实验跟踪工具接线](chapters/41-evaluation-reproducibility.md)：
  TensorBoard/W&B/MLflow/自建对比，`run_id ↔ config_hash ↔ data_hash ↔
  checkpoint_hash` 链路。
- [11.6.1 memory snapshot](chapters/11-memory-optimization.md)：录制/落盘/
  可视化、峰值/碎片/泄漏的读图方法。
- [44.4.4 后训练与 RL 的 TCO](chapters/44-business-tco.md)：SFT/DPO/GRPO
  的成本分解、rollout 主导、colocated vs disaggregated、权重同步、
  goodput 与预算表。
- 附录 L 新增 L19b（把 Ulysses 参考接到真实 all-to-all），附录 C 补
  Ulysses/Energon/Flight Recorder 术语。

**修正（按审计证据逐条核对）**

- **EP/DP 梯度语义**：10.3.6 明确“跳过 DP + `/ep`”只在 `ep=dp` 成立；
  `dp>ep` 时专家有 `dp/ep` 个 EDP 副本，必须先做副本组归约，per-rank
  token 是 `tokens/dp` 而非 `/ep`。同步修正 04.4.4、05.7、A.12、22.1、
  25.8/25.12.1 的口径；全局梯度范数对专家只除 `cp`，不除 DP。
- **SP 通信方向**：03/04 统一为 Column `AG→RS`、Row `RS→AG`；删除“SP 省
  一半前向通信”的过度概括（Row 减半，但 Column 多一次 AG）。
- **70B 显存结论**：先 TP/PP 切权重/梯度，再叠 ZeRO；DP=8+ZeRO-1 仍是
  385 GB/卡，单靠优化器状态分片放不进 80 GB。
- **全局 batch**：`tiny_tp2` world=8 的最小合法 `global_batch` 是 8
  （`2×4` 的倍数），不是 16。
- **推理成本决策**：41 章的例子改为“7B×240B 训练量约等于 13B×128B，
  但 7B 推理更便宜”，推理占比高应选 7B。
- **TCO 算术**：44.7 把故障率乘进 $/GPU-hour（$4.21/$6.15/$13.9），
  明确 MFU 只影响任务 GPU-hours；43.5.1 统一到 40% MFU 基线（$1.48M），
  节省改为 11%（$0.16M/任务、$1.6M/年）。
- **其他数值/公式**：KV 量化补 K+V 的因子 2；SFT loss mask 用
  `labels[:, :prompt_len]` 或 packing mask；检查点公式去掉 `× shards`；
  08 的 1F1B in-flight 统一为 `pp-1`；09 的 head 记号改为 `n_h/d_h`；
  A.16 的 MLA 投影补转置；12 的 FP8 示例展示 `dim` 分支、硬件改为
  Hopper/Ada；13 的 Column Parallel 改为“输出列”、标清 mini 的三次
  Newton–Schulz 与生产 quintic 的区别。
- **求解器/代码一致性**：14.11 补 `valid()`，覆盖 `hidden/n_h/n_kv/vocab/
  ffn/seq_len % tp`、`num_experts % ep`；15 修正 `generation_kv` 未接
  CLI、`distributed_optimizer` 分类；18 修正全 mask 行的 `nan_to_num(0)`；
  22 的 decay 分组补 `shape[-1]==1`；23 展示 flat/zero1 两条分支；25 换成
  真实 `selected/index_copy` 循环；28 修正 `tiny_tp2` 的合法 world。
- **路径/API**：29 改为 `core/dist_checkpointing/`、MLA
  `multi_latent_attention.py`、补 optimizer/datasets/tokenizers/models/
  `transformer_block.py`/`fp4_utils.py` 与 MTP；B 拆分 `--fp8-recipe`
  与 `--fp4-format/--fp4-recipe`；修复 29 的 `grep -v ":"` 自删命令。
- **导航/一致性**：50 的“下一章”改为 51；L/M/N 附录页脚补齐；51 的
  部分导读改为本章导读；`d_z` 作为优化器切分倍数的唯一符号；E 的 0.19
  文档标注为历史版本；M 注明实验 13 在 M.15；README 的 NVFP4 表述改为
  “DeepSeek 结构 recipe，非原论文 FP8 口径”。
- **测试计数**：全量更新为 **130 passed**；`test_paged_cache=7`、
  `test_checkpoint_reshard=8`、`test_mla=6`（README/00/27/F/VERIFICATION）。

**验证**

- 在 **Python 3.14.6 + PyTorch 2.9.1+cpu（Windows 11，CPU）** 上执行：
  `pytest` **130 passed**（106.02 秒），`scripts/verify.py` **25 个验收
  用例全部 OK**（26 次子进程调用，含 compile；219.5 秒），
  `python scripts/check_docs.py` **docs consistency: OK**（53 章 + 14 附录）。
  详见 [VERIFICATION.md](VERIFICATION.md)。

### 第八版修订（2026-11-22）

**新增内容**

- 新增 [第 51 章 从 PyTorch 到 Megatron：渐进式迁移与验收](chapters/51-pytorch-migration.md)：
  六个阶段的迁移工单（基线 → DP → ZeRO/flat → TP/SP → PP → CP/EP）、
  每个阶段的放行门槛、可执行的 tiny 演练、12 个典型 bug 到附录 N 实验的
  索引，以及 mini → Megatron Core 的映射。原“总结与学习路径”顺延为第 52 章。
- 新增 8.11 非均匀层切分、flexible PP 与双向调度：为什么“层数均匀 ≠ 耗时
  均匀”、zero-bubble 的 B/W 拆分、DualPipe 与 VPP/zero-bubble 的区别；
  34.3.1 给出 DeepSeek 调度声明的证据分级与映射陷阱。
- 新增 30.4.4–30.4.6 SSM 深潜：并行/分块 scan、状态 dtype、CP 边界状态、
  packing 重置，以及 HybridModel 的混合比例与验收。
- 新增 32.2.2 Kubernetes 最小 PyTorchJob 模板：gang scheduling、`/dev/shm`、
  NCCL 网卡、拓扑亲和、抢占恢复与共享存储清单。
- 新增 40.8.1 KV cache 量化：per-channel K/per-token V、group-wise 粒度、
  分页 block 与 scale 的对齐、显存公式和五项验收。

**修正**

- `BatchProvider` 改为“先采样一个全局 batch，再按 DP rank 取连续切片”：
  修复了文档声称“每个 rank 拿全局 batch 的一个 shard”但代码让各 rank
  独立采样的问题。新增 `test_data_parallel_ranks_partition_one_global_batch`，
  对齐训练超参后 DP=2 与单进程的逐步 loss/grad norm 可以做到 0.0 差。
- 代码审计修复 5 处：全 mask 的 attention/MLA 行返回 NaN → 现在输出 0；
  PP>1 的 loss 归约零张量跟随模型 device（CUDA 下不再混用 CPU 张量）；
  checkpoint 重分片只拒绝真正的 EP>1（EP=1 的复制专家可正常 TP/PP 重分片）；
  `AuxFreeBalancer.load_state_dict` 恢复 `update_speed/normalize`；
  `PagedKVCache.gather` 在空区间也校验 `which`。新增 5 个回归测试。
- 修正 README 的总结章号（51→52）与依赖图；29.4.1 把 L1–L4（发布状态）与
  E1–E4（证据强度）混用的问题改为两套分级并列表述。
- 修正 10.3.6 的 MoE aux 口径：mini 与 Switch 一致，统计 capacity 截断
  **之前**的分配；并说明另一种口径的代价。25.6.1 的 `f_i` 分母补上 `top_k`。
- 修正 26.7 把 GDN/GDP 列为“动态推理”的分类错误：它们是模型结构，不是
  推理调度；与 34.1 的提醒保持一致。
- 修正 05/31/G 的 sampler 公式（去掉 `dp_rank` 项，改为全局 batch + 切片）；
  22.4.1 与 11.5 区分“reduce-scatter 通信形状”和“真的省掉梯度显存”：
  mini 的 flat 优化器保留完整 `grad_flat`，内存上仍是 ZeRO-1。
- 修正 03/28 的启动与 profile 命令：`tiny_tp2` 在 world=8 下不满足全局 batch
  整除关系；nsys 在没有 nvtx range 时不应使用 `--capture-range=nvtx`。
- 修正 01/14 的 462B 表述：它是本书的示例账本，不是公开训练事实；公开锚点
  统一指向附录 I.5。
- 修正第 51 章的 VocabParallelEmbedding 通信方向（前向 all-reduce、反向
  本地梯度）与 flat clip norm 的归约范围（整个 world，不是只归约 DP）。

**验证**

- 在 **Python 3.14.6 + PyTorch 2.9.1+cpu（Windows 11，CPU）** 上执行：
  `pytest` **123 passed**（100.80 秒），`scripts/verify.py` **25 个验收
  用例全部 OK**（26 次子进程调用，含 compile；211.6 秒），
  `python scripts/check_docs.py`
  **docs consistency: OK**（53 章 + 14 附录）。详见
  [VERIFICATION.md](VERIFICATION.md)。

### 第七版修订（2026-11-22）

**新增内容**

- 新增 [附录 N 故障注入与数值对照实验手册](chapters/N-fault-injection-labs.md)：
  16 个“故意弄坏一个不变量 → 跑最小命令 → 观察哪个断言变红”的实验，覆盖
  TP/SP/CP/PP/EP/flat optimizer/VPP/checkpoint/paged cache/RoPE，以及第七轮
  审计的恢复/FP8/flat-TP 三个新 bug。每个实验
  给出注入、命令、预期症状、根因与生产监控指标；与第 27 章的“病历”互补。
- 新增 18.1.1 RoPE 外推与长上下文位置编码：波长账、PI/NTK/YaRN/LongRoPE/
  ALiBi 对照、CP 全局位置、cache/训练-推理一致性验收。
- 新增 30.1.1 结构选择如何改变并行与数值：LayerNorm/RMSNorm、QK-norm、
  parallel block、SwiGLU/GeGLU、tied embedding、bias/residual scaling。
- 新增 34.4.1 稀疏/线性注意力四条路线与并行账本、34.4.2 1M 上下文系统验收。
- 新增 11.7.3 MoE 训练的额外显存（dispatch/combine、capacity padding、
  expert MLP 激活、grouped GEMM workspace 的量级算例）。
- 新增 25.12.1 `ep < dp` 的专家副本布局与两级梯度；40.7.3 PP 推理与 KV 的
  分 stage 放置；38.8.1 生产数据管线的最小接线与验收。
- 新增 48.3.4 可复现的 1B 估算（`configs/reference_1b.json`），并解释
  1.245B vs 1.38B、126 vs 153 GPU-hours 的口径差异。
- 附录 M 新增实验 13：用 `megatron.core` 跑一个 1-GPU 前向（标记为非 CI）。

**修正**

- 修正 ZeRO-1 状态公式：A.5 与 4.4.4 现在使用
  `4N/(tp·pp) + 12N/(tp·pp·d_z)`（权重/梯度复制，只有优化器状态分片）；
  70B、tp=8、d_z=8 为 48.1 GB/卡，不是 17.5 GB/卡。
- 修正 MoE 总参数倍数：改为 `(N_shared + E·N_expert)/(N_shared + N_expert)`，
  避免算出小于 1 的“倍数”。
- 修正 24 章的 DCP 异步 API（`async_save`/`AsyncStager`，不是
  `async_storage_writer=True`）；29 章的 SSM 路径（`core/ssm/`）与
  `get_gpt_decoder_block_spec`/`get_gpt_decoder_layer_specs` 的返回类型；
  8.10 的测试覆盖范围；37.7 的 PP p2p 字节数（4bsh）；17.6 的 LM head
  形状；A.16 的 MLA 吸收公式；15 的 `kv_cache_bytes` MLA 声明；40.7.2 的
  EP all-to-all 公式；16/22 的两处实现细节。
- `analysis.py` 修正 `gpu_hours` 的语义：现在返回**总 GPU-hours**，新增
  `cluster_hours` 返回墙钟小时；`estimate` 同时打印两者，避免把 152.7
  GPU-hours 误读成 19.1 小时；1F1B 的 in-flight 峰值改为 `pp-1`。
- 代码审计修复：`Trainer.fit` 恢复不再跳过一步；`simulate_fp8` 用
  straight-through estimator，梯度不再全零；flat optimizer 的 clip norm
  覆盖 TP/PP；checkpoint 重分片明确拒绝 flat/ZeRO 状态；RoPE 校验偶数
  head_dim；flat buffer 跟随模型 device。新增 5 个回归测试（pytest 117）。
- 收敛重复：49.3 的 FP8 recipe 表指向 12.2.3；26.9.2 的分页 cache 细节
  指向 40.3.1；44.2.3 的电费推导指向 I.3.1；6.9/9.5.1 的 bug 故事指向 27.5。

**验证**

- 在 **Python 3.14.6 + PyTorch 2.9.1+cpu（Windows 11，CPU）** 上执行：
  `pytest` **117 passed**（99.78 秒），`scripts/verify.py` **25 个验收用例
  全部 OK**（26 次子进程调用，含 compile；213.6 秒），
  `python scripts/check_docs.py` **docs consistency: OK**（52 章 + 14 附录）。
  详见 [VERIFICATION.md](VERIFICATION.md)。
- `configs/reference_1b.json` 的 `estimate` 输出已锁定在 48.3.4；新增
  `test_training_flops_and_mfu` 的 GPU-hours/墙钟断言，防止把“工作量”与
  “墙钟时间”混用。

### 第六版修订（2026-11-22）

**新增内容**

- 新增 [附录 M 源码阅读实验手册](chapters/M-source-reading-labs.md)：12 个带
  命令、观察点与验收问题的上游源码实验（并行组、TP/SP、attention、PP、
  MoE、distributed optimizer、checkpoint、推理、ModuleSpec、测试/CI）；
  第七版增至 13 个（新增 `megatron.core` 1-GPU 前向）。
- 新增 4.5.0 集合通信原语的字节账（all-reduce/reduce-scatter/all-gather/
  all-to-all 与 α-β 模型）；5.8.1 packed/变长序列的 batch 与 loss 口径；
  10.3.6 EP 的梯度与 aux 语义。
- 新增 14.11 配置求解器：硬约束过滤 → 枚举 → 通信/气泡/显存打分 → 候选；
  24.11 异步检查点的时序、staging、一致性与故障模式；29.6 `ModuleSpec`/
  Provider/逐层 spec 的模型组装机制。
- 新增 30.7.8–30.7.9 多模态并行账本（图像 token、ViT 参数/FLOPs、TP 通信、
  PP 放置）与端到端检查；41.3.1 最小评测 harness；49.3.1 NVFP4 训练 recipe
  与验收门；50.5.1 EP/FSDP2 重分片的 metadata、专家 id 与验收。
- 每个部分开头加“导读”（前置、目标、跳读建议）；第 00 章新增 0.6
  “按问题索引”，按症状直接跳到章节。

**修正**

- 48 章 `--train-iters 200000` 与 24B token 预算不符，改为 22,888 步并给出
  换算；44/I 的 1000×H100 功耗口径统一为 9 kW/节点（约 $1.02M/年）。
- HSDP 定义统一为“节点内分片、节点间复制”；reduce-scatter 与 all-reduce
  的字节数、LayerNorm 融合的 1/2 访存、8 vs 21 的 warmup=0 解释、
  μP/PyTorch 初始化方差、SP 反向 split+all-reduce vs reduce-scatter、
  TP 权重拼接符号、`a`/`k` 符号冲突、EP 通信的聚合/每卡口径、ZeRO 表的
  参数同步列、offload 的 `dp_z`、sampler 类名等均已修正。
- 修正代码片段与仓库不一致处：词表并行 CE 的 mask/clamp/fp32、Embedding
  的 `mask.unsqueeze(-1)`、attention 输出的 `contiguous()`、
  `PipelineCommunicator(..., device=None)`、`all_reduce_gradients` 的
  `skip_expert_parallel`/专家 `/ep`、`ZeRO1Optimizer` 的 param groups、
  EP 组创建顺序。
- 修正 `analysis.py` 的激活峰值：现在区分“单 micro-batch”和“in-flight
  峰值”（第六版为 1F1B≈`min(m,pp)`；第七版按实际调度修正为
  `max(1,min(m,pp-1))`，GPipe=`m`），`estimate` 同时打印两者。
- 修正附录 K 的上一附录链接与 final norm 命名、附录 L 的练习编号重复、
  35 章与 36/J 的 L1–L4 冲突（改为 S1–S4）、46 章结尾过渡、README 的
  roadmap 表述与 Qwen3.5-VL 断言。

**验证**

- 在 **Python 3.14.6 + PyTorch 2.9.1+cpu（Windows 11，CPU）** 上执行：
  `pytest` **111 passed**（95.48 秒），`scripts/verify.py` **25 个验收用例
  全部 OK**（26 次子进程调用，含 compile；208.8 秒），
  `python scripts/check_docs.py` **docs consistency: OK**（52 章 + 13 附录）。
  详见 [VERIFICATION.md](VERIFICATION.md)。
- 新增 `scripts/check_docs.py`：检查章号/附录连续性、SUMMARY 覆盖、相对
  链接、章/节引用范围与 README 计数，防止“加了一章只改了一半导航”。

### 第五版修订（2026-11-22）

**新增可运行实现**

- `optim/distributed_optimizer.py`：flat distributed optimizer。把参数
  flatten 成连续 buffer，用 reduce-scatter 得到 DP 分片的平均梯度，只在
  本地 shard 上做 AdamW，再 all-gather 参数。含 padding、逐元素 decay
  mask、shard 梯度范数、检查点与布局校验。`tests/distributed_optimizer_equiv.py`
  在 DP=2 下与普通 AdamW 对比 3 步：参数最大差 0.0、loss 差 0.0、
  梯度范数差 2.38e-07。
- `parallel/ring_attention.py`：ring attention 参考实现。单进程模拟
  `cp` 个 rank，显式实现 online softmax、全局因果 mask、KV 块跳过与负载
  统计；`scripts/ring_attention_demo.py` 在 cp=1/2/4/8 上与 dense 参考的
  最大误差约 3.6e-7–4.8e-7，`tests/unit/test_ring_attention.py` 还验证
  反向梯度一致。
- `parallel/moe_balance.py`：aux-free 负载均衡（DeepSeek 风格 bias 更新）
  与 node-limited routing。`scripts/moe_balance_demo.py` 把最大专家负载从
  3240 降到 1124（理想 1024），并验证每 token 最多命中 1/2 个节点。
- `inference/paged_cache.py`：分页 KV cache（block allocator、block table、
  append/gather/free、碎片统计、层锁步校验）。测试覆盖跨 block 边界、
  部分块、释放复用、耗尽报错。
- 修复 `analysis.py` 的两处计数错误：GELU 的 bias 数（2 个投影而非 3 个）
  与 MoE 激活参数漏算的 `4h` norms；新增精确断言。
- 词表并行 CE 改为在 fp32 完成 log-sum-exp，梯度 cast 回 logits dtype；
  新增 bf16 契约测试。

**新增章节**

- 新增第 VIII 部分“规模化实战”：第 48 章（1B 模型端到端：数据 → 配置 →
  启动 → 监控 → 恢复 → 评测 → 转换 → 部署）、第 49 章（kernel/TE/FP8/
  CUDA Graph/torch.compile/通信重叠/zero-bubble）、第 50 章（分布式优化器
  与 FSDP 深潜）。
- 新增第 47 章“从实验到产品：模型迭代、组织与决策”，补迭代闭环、团队
  接口、算力预算、go/no-go 门与技术债。
- 原“总结与学习路径”移为最后一章（本版为第 52 章），补第 VIII 部分、决策者路径、12 周计划
  的前置/产出与附录导航。

**文档深化与修正**

- 补 2D/2.5D TP 的通信公式与适用边界；补 ring attention 的可运行实现、
  online softmax 三个易错点与负载不均；补 aux-free/node-limited 的算法与
  实测数字；补 critical batch size；补通信重叠效率模型；补 zero-bubble
  ZB1P/ZB2P 气泡公式；补 grouped GEMM / split sizes / DeepEP 的系统视角；
  补 paged cache 的不变量与碎片公式；补异步检查点的原子可见/staging/
  一致性；补 flat optimizer 的逐行对照与生产差距。
- 修正事实/单位：tiny 激活示例的 `h`、14 章的交叉引用、70B/462B 的
  token/param 标签、NVLink 双向/单向口径、DP 通信的字节单位、PyTorch
  版本基线、G 附录的 EP RNG 矛盾、44 章自建/云 break-even 算术、43 章
  “数百万美元”的口径。
- 去重：42 章前 4 个案例改为“规模化增量”表并指向 27.5；31 章 packing/
  sampler 的实现细节指向 38 章，保留配方视角。
- 版本/路线图声明统一按 36.1 的 L1–L4 发布分级 + 附录 J 的 E1–E4 证据
  分级；把具体 release-note 断言
  降级为“以当前源码/标签为准”。

**验证**

- 在 **Python 3.14.6 + PyTorch 2.9.1+cpu（Windows 11，CPU）** 上执行：
  `pytest` **110 passed**（95.67 秒），`scripts/verify.py` **25 个验收用例
  全部 OK**（26 次子进程调用，含 compile；208.2 秒）。详见
  [VERIFICATION.md](VERIFICATION.md)。
- 新增 `configs/tiny_dp2_flat.json`，并纳入 pytest 冒烟矩阵与
  `verify.py` 的多进程 checkpoint/restore 步骤。

## 1. 快速开始

### 1.1 环境

推荐 Python **3.12+**（Megatron Core 0.20 已要求 Python ≥ 3.12；本教程在
Python 3.14.6 上也验证通过）。

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

CPU 环境安装 PyTorch 时，建议使用官方 CPU 轮子：

```bash
# Linux
pip install torch --index-url https://download.pytorch.org/whl/cpu
# Windows（本教程在 2.9.1+cpu 上验证；Windows 多进程请使用
# scripts/run_distributed.py 的 portable 后端，torchrun/Gloo 不会自动回退）
pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu
```

### 1.2 单进程训练

```bash
python -m mini_megatron.cli train --config configs/tiny.json
```

### 1.3 多进程训练

Windows 使用自带的 portable 启动器（避免部分 PyTorch Windows 轮子的 Gloo 问题）：

```bash
python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_pp2.json
# 专家并行：EP=2，以及 TP=2 × EP=2
python scripts/run_distributed.py --world-size 2 --config configs/tiny_ep2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_ep2.json
# flat distributed optimizer（reduce-scatter/all-gather）
python scripts/run_distributed.py --world-size 2 --config configs/tiny_dp2_flat.json
```

前沿模块的独立演示（单进程、CPU 可跑）：

```bash
python scripts/ring_attention_demo.py    # ring vs dense，cp=1/2/4/8
python scripts/moe_balance_demo.py       # aux-free bias + node-limited routing
python -m pytest -q tests/unit/test_paged_cache.py
```

VPP 执行器是独立的调度参考实现（不进 `Trainer`）：

```bash
python tests/parallel_vpp.py --world-size 2 --out vpp.json
```

Linux 上也可以：

```bash
torchrun --standalone --nproc_per_node=2 -m mini_megatron.cli train \
  --config configs/tiny_tp2.json
```

### 1.4 完整验证

```bash
python scripts/verify.py
python scripts/check_docs.py   # 章号/附录/SUMMARY/链接/计数一致性
```

它会依次运行单元测试、TP/PP/CP/DP/EP/MoE/flat optimizer 多进程训练、
VPP 等价性、检查点续训和生成。最后一次完整验证的结果会打印在命令末尾，详细记录见
[VERIFICATION.md](VERIFICATION.md)。本仓库的测试断言包含：

- TP=2 与 TP=1 的 loss 和梯度等价（误差 < 1e-5）
- PP=2 与 PP=1 的 loss 和梯度等价
- CP=2 与 CP=1 的 loss 和梯度等价
- EP=2 与专家复制 MoE 的 loss/logits/aux/梯度等价（全局 batch）
- interleaved VPP（pp=2, v=2）与 PP=1 的 loss/梯度等价
- GPipe 与 1F1B 在相同权重、相同数据下**最终 step** 的 loss/grad_norm 一致
  （不是逐步对比）
- 单进程、2 进程、4 进程的多种并行组合可完成前向/反向/优化器更新
- 检查点保存与恢复（单进程 / PP=2 / EP=2 / flat DP=2）、生成和 CLI 冒烟；
  多进程 ZeRO-1 checkpoint 恢复**未覆盖**（见 VERIFICATION 的已知不覆盖）
- TP 分片初始化：不同 TP rank 的权重块不同、同一 rank 可复现、复制参数一致
- AdamW 状态（`exp_avg/exp_avg_sq/step`）的 TP/PP 重分片往返一致
- MLA：形状/因果性/prefill+decode cache 等价/padding mask/反向
- RoPE cache 在设备变化时的重建（回归）
- flat optimizer：DP=2 下与 AdamW 逐元素一致、裁剪、检查点、padding；
  TP=2/DP=2 的全局 norm/clip 与参考一致（`tests/flat_tp_equiv.py`）
- ring attention：多 cp 下与 dense 一致、因果性、反向、负载统计
- aux-free：bias 方向、选择与权重分离、node-limited 预算
- paged KV：跨块 gather、部分块、释放复用、层锁步

最近一次完整验证（2026-11-22，Windows 11 CPU，Python 3.14.6，
PyTorch 2.9.1+cpu）：**130 passed，25 个验收用例全部 OK**（verify 219.5 秒），
文档一致性检查 **53 章 + 14 附录，OK**。
具体数值与历史记录见 [VERIFICATION.md](VERIFICATION.md)。

单独跑 MLA / ring / Ulysses / MoE / paged 演示：

```bash
python scripts/mla_demo.py
python scripts/ring_attention_demo.py
python scripts/ulysses_demo.py
python scripts/moe_balance_demo.py
python -m pytest -q tests/unit/test_mla.py tests/unit/test_ring_attention.py
python -m pytest -q tests/unit/test_ulysses.py tests/unit/test_moe_balance.py
python -m pytest -q tests/unit/test_paged_cache.py
```

## 2. 目录结构

```text
megatron-lm-tutorial/
├── README.md
├── SUMMARY.md
├── VERIFICATION.md            # 最近一次完整验证记录
├── requirements.txt
├── pyproject.toml
├── chapters/                  # 53 章教程（00–52）+ 附录 A–N
├── configs/                   # 可运行实验配置（reference_1b 仅用于 estimate）
├── mini_megatron/
│   ├── config.py              # 配置校验
│   ├── parallel/
│   │   ├── comm.py            # torch.distributed / portable 双后端
│   │   ├── state.py           # TP/PP/CP/DP/EP 进程组
│   │   ├── layers.py          # TP 线性层与词表并行 Embedding
│   │   ├── attention.py       # RoPE、GQA、核心注意力、CP
│   │   ├── cross_entropy.py   # 词表并行 CE
│   │   ├── transformer.py     # Block 与 Pipeline Stage
│   │   ├── moe.py             # 专家复制的 TP MoE
│   │   ├── expert_parallel.py # EP all-to-all dispatch/combine
│   │   ├── moe_balance.py     # aux-free bias / node-limited routing
│   │   ├── ring_attention.py  # ring attention / online softmax 参考
│   │   └── mla.py             # MLA 独立参考（latent KV / 解耦 RoPE）
│   ├── inference/
│   │   └── paged_cache.py     # 分页 KV cache / block allocator
│   ├── pipeline/
│   │   ├── schedules.py       # GPipe / 1F1B
│   │   ├── interleaved.py     # VPP chunk 映射/依赖校验/气泡估算
│   │   └── interleaved_exec.py# 可执行的 interleaved GPipe
│   ├── optim/
│   │   ├── adamw.py           # AdamW、LR、ZeRO-1、Muon
│   │   └── distributed_optimizer.py  # flat reduce-scatter/all-gather
│   ├── data/
│   │   ├── dataset.py         # 合成数据 / packed bin
│   │   └── packing.py         # document mask / position reset / loss mask
│   ├── checkpoint.py          # 分 rank 检查点（同布局恢复）
│   ├── checkpoint_reshard.py  # 权重 + 非 ZeRO AdamW 的 TP/PP 重分片
│   ├── sharding.py            # 全量 state dict 的 TP/PP 切片（用于等价性验证）
│   ├── analysis.py            # 参数/显存/FLOPs/通信估算
│   ├── training.py            # 训练循环
│   ├── generation.py          # 全量重算采样
│   ├── generation_kv.py       # KV cache 解码
│   └── cli.py                 # 命令行（train/generate/benchmark/info/estimate）
├── scripts/
│   ├── run_distributed.py
│   ├── verify.py
│   ├── smoke_test.py
│   ├── mla_demo.py            # MLA prefill/decode 与 cache 比例演示
│   ├── ring_attention_demo.py # ring vs dense + 负载统计
│   ├── moe_balance_demo.py    # aux-free 与 node-limited 演示
│   └── check_docs.py          # 章号/链接/计数一致性检查
├── tests/
│   ├── unit/
│   ├── parallel_equiv.py      # TP/PP/CP 等价性
│   ├── parallel_ep.py         # EP 等价性
│   ├── parallel_vpp.py        # VPP 等价性
│   ├── distributed_optimizer_equiv.py  # flat optimizer vs AdamW
│   ├── flat_tp_equiv.py       # TP+DP 的全局 clip norm vs 参考
│   └── zero_equiv.py          # ZeRO-1 vs AdamW
└── benchmarks/
```

## 3. 教程地图

| 部分 | 章节 | 主题 |
|---|---|---|
| I 基础与系统观 | 00–04 | 为什么需要 Megatron、架构地图、环境、计算与显存账本 |
| II 并行原理 | 05–14 | DP、TP、SP、PP、CP、MoE、显存、精度、优化器、性能建模 |
| III 工程实现 | 15–28 | mini-megatron 逐模块拆解、调度、检查点、测试、调优 |
| IV 生产栈 | 29–33 | Megatron Core 代码地图、模型家族、数据、集群、可观测性 |
| V 前沿 | 34–36 | HybridModel、DeepSeek 风格 MoE/MLA、1M 上下文、FP4、Muon、Bridge |
| VI 工程专题 | 37–42 | 硬件/拓扑/通信、数据工程、后训练、推理系统、评测、故障复盘 |
| VII 商业与组织 | 43–47 | 市场、TCO、自建/云/托管、开源商业化、从实验到产品 |
| VIII 规模化实战 | 48–51 | 1B 端到端、kernel/TE/编译栈、分布式优化器与 FSDP、PyTorch 迁移 |
| 总结 | 52 | 全书回顾、五条实践路径、下一步 |
| 附录 | A–N | 数学、配置、术语、排障、参考、API、确定性、runbook、硬件/成本、声明登记、转换映射、练习、源码阅读实验、故障注入实验 |

完整目录见 [SUMMARY.md](SUMMARY.md)。

### 章节依赖图（粗粒度）

```text
00 前言
 └─ 01 为什么 → 02 架构地图 → 04 账本
      ├─ 05 DP → 06 TP → 07 SP → 08 PP → 09 CP → 10 MoE
      │    └─ 11 显存 → 12 精度 → 13 优化器 → 14 策略
      ├─ 15–28 mini 实现（建议按 15→16→17→18→19→20→21→22→23→24→25→26→27→28）
      ├─ 29–33 生产栈（29 代码地图 → 30 模型 → 31 数据 → 32 集群 → 33 调试）
      ├─ 34–36 前沿（34 技术 → 35 Bridge → 36 路线图）
      ├─ 37–42 工程专题（37 硬件 → 38 数据 → 39 后训练 → 40 推理 → 41 评测 → 42 故障）
      ├─ 43–47 商业/组织（43 市场 → 44 TCO → 45 选型 → 46 开源/投资 → 47 到产品）
      ├─ 48–51 实战（48 端到端 → 49 kernel/TE → 50 优化器/FSDP → 51 迁移）
      └─ 52 总结
附录：A 数学 · B 配置 · C 术语 · D 排障 · E 参考 · F API · G 确定性 · H Runbook
      I 硬件/成本 · J 声明登记 · K 转换映射 · L 练习 · M 源码阅读实验
      N 故障注入与数值对照实验
```

依赖不是强制的：第 III 部分可以独立于第 II 部分阅读，但遇到“为什么这里要
all-reduce”时回到第 5–10 章；第 VI/VII 部分可以按角色跳读。

## 4. 这个实现“简化”在哪里

它不是 NVIDIA Megatron-LM 的复刻，而是把**决定正确性与性能的关键路径**保留下来：

| 生产 Megatron | mini-megatron |
|---|---|
| Transformer Engine FP8/FP4 kernel | FP8 数值模拟（cast + scale，不加速） |
| FlashAttention / cuDNN fused attention | 显式 score 矩阵，便于观察 mask 与 CP |
| 真正的 ring attention 通信/重叠 | 训练路径用 all-gather KV；另有单进程 ring 算术参考（无 P2P） |
| 专家并行 all-to-all + grouped GEMM | EP all-to-all（padding + Python 循环），`ep=dp`；无 grouped GEMM/EP 副本 |
| MLA 训练/推理（TP/CP/FP8/吸收） | 独立单进程参考（latent KV、解耦 RoPE、prefill/decode）；无 TP/CP/FP8/吸收 kernel |
| PagedAttention / 连续批处理 | paged KV 数据结构 + allocator；无 paged kernel/调度器 |
| 分布式优化器（连续 bucket、grad sharding、通信重叠） | flat 单 bucket + reduce-scatter/all-gather + 同步通信；无多 bucket/ZeRO-2/重叠 |
| FSDP1/FSDP2/HSDP | 概念与选型对照；实现是 flat ZeRO-1，不是 per-parameter DTensor |
| 分布式检查点与任意 TP/PP 重分片 | 同布局恢复 + 权重/非 ZeRO AdamW 的 TP/PP 重分片；ZeRO/EP 重排未实现 |
| aux-free 路由 / node-limited routing | 独立参考实现与测试；未接入 Trainer 的 EP 训练主链 |
| VPP / interleaved 1F1B / zero-bubble | GPipe、1F1B、可执行 interleaved GPipe；无 1F1B VPP/zero-bubble |
| CUDA Graph、异步通信、通信重叠 | 同步 + 前向 isend，保留了正确顺序与 p2p tag |
| Muon 大规模分布式实现 | 教学版 Muon（2D 参数的 Newton–Schulz） |

每一处简化在对应章节都会给出“生产实现应查什么代码/开关”的映射。

## 5. 版本与引用

教程中的“当前”以 2026-11 为基线：

- Megatron Core 最新标签约 **0.20.1**；0.20 系列包含 selective FP32、
  distributed Muon、DeepSeek 结构的 NVFP4 **recipe**（含 active MTP 层的
  FP4 量化；注意 DeepSeek-V3 原论文训练用的是 FP8，NVFP4 是后续框架 recipe，
  口径见 12.4/34.5）、推理侧 chunked prefill/prefix caching/speculative
  decoding、prefill-decode 分离等。
- 2026 Q3/Q4 roadmap 是 **L3 信号（设计/路线图），不是发布承诺**，重点包括
  HybridModel 的 EP overlap、Megatron FSDP v2、Blackwell 上的 2D MXFP8 /
  NVFP4、64K→1M 长上下文、多模态/VL 模型的 PP 支持等；采用判断与放弃条件
  见第 36 章，证据分级见附录 J。
- Hugging Face ↔ Megatron 的转换与 recipes 已主要放在 **Megatron Bridge**
  （Python 版本以 Bridge 的安装文档为准），Megatron-LM 本体继续聚焦 Megatron Core。

参考链接集中在 [附录 E](chapters/E-references.md)。厂商公布的 MFU/TFLOPs
数字会随精度、稀疏性、GPU 代际和 workload 变化，教程中均注明口径。

## 6. 许可与致谢

代码为教学目的编写，接口命名参考 NVIDIA Megatron-LM（Apache-2.0）。
Megatron、NeMo、Transformer Engine 等名称归 NVIDIA 所有。建议在生产中使用
官方 Megatron Core / Megatron Bridge，并把本项目当作理解内部机制的“透明模型”。
