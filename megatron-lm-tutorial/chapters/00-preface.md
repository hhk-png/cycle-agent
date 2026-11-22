# 第 00 章 写在前面：这套教程要解决什么问题

> **第 I 部分导读（00–04）**
> 本部分建立共同语言：为什么规模会逼出并行、系统由哪些层组成、怎样把 mini
> 跑起来、怎样用一阶账本做决策。前置是 Python/PyTorch；读完应能给定
> `N/s/GPU 数` 估算显存与 GPU-hours，并说出至少三种并行维度的取舍。
> 03 可跳读，04 的账本建议精读——后面每一章都会回到它。

## 0.1 一个真实的困境

假设你已经会写一个 nanoGPT：几十行 `nn.TransformerEncoderLayer`，单卡训练一个
小模型，loss 能下降，故事似乎完整了。然后你拿到 64 张 H100，准备训一个 70B
模型，立刻会遇到四个彼此纠缠的问题：

1. **放不下**：70B 参数的 bf16 权重是 140 GB，Adam 的 fp32 主权重和两阶矩又是
   840 GB，单卡 80 GB 显存连参数都装不下。
2. **算不快**：即使把参数切开，矩阵乘的维度变小，GPU 利用率下降；通信成为新的
   瓶颈。
3. **对不齐**：张量并行切开后，LayerNorm、Dropout、词表、交叉熵的数值必须与
   单卡参考实现一致；否则 loss 曲线“看起来能跑”，梯度其实是错的。
4. **停不下**：千卡训练动辄数周，节点会坏、任务会被抢占、checkpoint 要能恢复，
   还要能换并行配置继续训练。

Megatron-LM 的价值，不是“又一个 Transformer 实现”，而是把上述问题拆成一组
**可组合的并行维度**，并为每个维度提供经过大规模验证的算子、调度与检查点格式。
这套教程希望让你同时获得三种能力：

- **判断力**：看到一个模型规模、序列长度和集群拓扑，能选出合理的并行方案；
- **实现力**：能从零写出 TP/PP/DP/CP 的关键路径，并解释每一行通信为什么存在；
- **工程力**：知道生产 Megatron 的代码在哪里、哪些开关会改变数值、怎样验证和排障。

## 0.2 四条阅读路线

本书章节有依赖，但不必线性读完。每条路线都可以把附录当作“随时查”的手册：
数学看 A，配置看 B，术语看 C，排障看 D/H，复现看 G，硬件/成本看 I，
版本声明看 J，转换看 K，练习看 L，源码阅读实验看 M。

**路线 A：系统工程师（推荐完整阅读）**  
00 → 01 → 02 → 03 → 04 → 05 → 06 → 07 → 08 → 09 → 10 → 11 → 12 → 13 →
14 → 15–28（实现）→ 29–33（生产）→ 34–36（前沿）→ 37–42（工程专题）→
43–47（商业与组织）→ 48–51（规模化实战）→ 52（总结）。

**路线 B：只想跑通 mini 实现**  
00 → 03 → 15 → 16 → 17 → 18 → 19 → 20 → 21 → 23 → 24 → 26 → 27。  
先把 `python scripts/verify.py` 跑通，再回来看原理；22/25/28 可在需要时再读。

**路线 C：技术负责人/投资人**  
00 → 01 → 02 → 04 → 14 → 29 → 34 → 35 → 36 → 37 → 39 → 40 → 41 → 42 →
43–47 → 48 → 50。  
重点看规模账本、并行策略、生态、故障、成本与优化器选型，不必逐行读算子。

**路线 D：平台/基础设施工程**  
00 → 03 → 04 → 14 → 27 → 28 → 29 → 32 → 33 → 37 → 38 → 40 → 41 → 42。  
重点是拓扑/通信、数据/推理、评测/故障，以及如何把 mini 的验证方法搬到生产。

## 0.3 代码约定

- `mini_megatron/` 中的代码是**可运行的**，不是伪代码。所有 mini 实现引用的
  类名都能在仓库里找到；生产 Megatron/TE/Bridge 的类名（如
  `CudaRNGStatesTracker`、`AutoMapping`）不在本仓库，会明确标注“生产侧”。
- 生产 Megatron 的类名、开关、目录会随版本变化。本书以 2026 年 11 月的
  Megatron Core 0.20.x 为基线，遇到可能漂移的 API 会明确写“以当前源码为准”。
公式中的符号统一如下（后文不再重复解释）：

| 符号 | 含义 | 符号 | 含义 |
|---|---|---|---|
| `b` | micro-batch size | `m` | 每步 micro-batch 数（grad accumulation） |
| `s` | 序列长度 | `h` | hidden size |
| `L` | 层数 | `N` | 参数量（注意区分总参数/激活参数） |
| `t` / `p` / `dp` / `c` | TP / PP / DP / CP size | `e` | EP size |
| `E` / `k` | 专家数 / top-k | `v` | VPP chunks per rank |
| `V` | 词表大小 | `n_h` / `n_kv` | query / KV head 数 |
| `bpe` | 每元素字节数（bf16=2） | `B_i` | 第 i 个 micro-batch 的 token 数 |
| `d_h` | 每 head 维度 | `d_c` / `d_R` | MLA latent / RoPE 维度 |
| `d_z` | 优化器状态的切分倍数（ZeRO-1 通常 = DP size） | | |

早期章节的通信公式里可能用 `d` 表示 DP size（如 `(d-1)/d`），以该处上下文
为准；涉及 attention 形状时统一用 `d_h`，避免与 DP 混淆。
所有“等价性”都指在 float32/bf16 的数值容差内与单卡参考实现一致；不是逐位
一致。真正的逐位确定性需要固定 reduction 顺序、关闭非确定性 kernel，成本很高。

mini 实现为了可读性牺牲了性能：显式 attention score、Python 循环路由、
同步通信。教程会在每处标注生产实现如何替换。

## 0.4 先看一张“能力边界”表

| 能力 | mini-megatron | 生产 Megatron Core |
|---|---|---|
| 模型 | decoder-only GPT；独立教学 MLA 模块（无 TP） | GPT/BERT/T5/Mamba/Hybrid/MoE/MLA/多模态 |
| TP | Column/Row/Embedding/CE | 同左 + fused kernels、TE、异步 all-reduce |
| SP | LayerNorm/Dropout 序列切分 | 同左，接入 TE |
| PP | GPipe、1F1B、可执行 interleaved GPipe | 1F1B、interleaved 1F1B/VPP、zero-bubble、灵活 stage |
| DP | all-reduce、ZeRO-1、flat distributed optimizer（reduce-scatter/all-gather） | DDP、distributed optimizer（多 bucket/重叠）、FSDP v2、HSDP |
| CP | all-gather KV；ring attention 单进程算术参考 | ring attention 通信/重叠、CP indexer、混合注意力 |
| MoE | top-k、capacity、aux、TP experts、EP all-to-all（padding + 循环）、aux-free/node-limited 参考 | EP/TP/PP、split sizes、grouped GEMM/DeepEP、DeepSeek 风格 MoE/MLA |
| 精度 | fp32/bf16 autocast、FP8 模拟 | TE FP8/FP4、NVFP4、selective FP32 |
| 检查点 | 每 rank 本地分片；权重/非 ZeRO Adam 可 TP/PP 重分片；flat optimizer 同 DP 恢复 | 分布式检查点、任意重分片、异步保存、ZeRO/EP 重排 |
| 推理 | PP=1 采样 + 教学版 KV cache + paged KV 数据结构 | paged attention kernel、chunked prefill、prefix cache、speculative、PD 分离 |
| 分析 | 参数/显存/FLOPs/通信估算 | 同左 + 真实 profiling；自动配置搜索多在外部工具/研究中 |
| 规模 | 单机 CPU/小 GPU | 千卡到万卡 |

这张表不是“功能清单”，而是阅读地图：每一行都对应后面的章节。

## 0.5 一个最小可验证承诺

本书附带一条命令：

```bash
python scripts/verify.py
```

它会实际执行：

- **130 个**单元/集成测试（数量随版本增加）；
- TP=2、PP=2、CP=2、DP=2、EP=2、MoE+TP、TP+EP、TP+PP、PP+DP、CP+PP、
  TP+DP+ZeRO、TP+DP+flat optimizer、TP+CP 等多进程训练；
- GPipe 与 1F1B 在相同权重下**最终 step** 的 loss/grad_norm 对比；
- 检查点保存、恢复（单进程/PP/EP/flat optimizer）、生成；
- 与 TP=1/PP=1/CP=1 参考模型的 loss 与梯度等价性检查；
- MLA 的因果性/cache 等价/反向，ring attention 的 dense 等价/反向，
  aux-free 均衡/node-limited routing，paged KV 的跨块/复用/碎片，
  RoPE cache 的设备变化回归。

如果这条命令没有全绿，后面的文字都不值得相信。请先让它绿，再读原理。

## 0.6 按问题索引

不必记住章节号；遇到问题时从这张表跳转。

| 你遇到的问题 | 先读 | 再读 |
|---|---|---|
| 模型/优化器状态放不下 | 04、11 | 50 |
| 单层 GEMM 太大或激活爆显存 | 06、11 | 14、49 |
| PP 气泡大、GPU 空转 | 08 | 21、49 |
| 长序列 OOM/注意力太慢 | 09 | 34.4、18.1.1、40 |
| 1M 上下文/稀疏注意力怎么选 | 34.4 | 09、31.7 |
| MoE 负载不均/通信爆炸 | 10 | 25、37 |
| loss 对但梯度错 | 27 | 09、17、18、附录 N |
| 想亲手验证一个并行不变量 | 附录 N | 27、09 |
| 训练 NaN/loss spike | 12、13 | 33、42 |
| MoE 训练 OOM/临时 buffer 爆 | 11.7.3 | 25、10 |
| 换卡数后 checkpoint 加载不了 | 24 | 50 |
| 吞吐低、MFU 上不去 | 14、28 | 37、49 |
| 数据怎么配比/packing | 31、38 | 05.8 |
| 后训练/RL 怎么接 | 39 | 40 |
| 推理成本/延迟高 | 40 | 44 |
| 流式/无限长会话的 KV 怎么控 | 40.4.5 | 40.3、40.8 |
| PP 推理/KV 怎么分 stage | 40.7.3 | 26、24 |
| 多模态/原始 web 数据怎么接 | 38.8.2 | 30.7、38.6 |
| 实验跑完却复现不了/看板对不上 | 41.7 | 41.8、附录 G |
| 显存碎片、OOM 但账算得下 | 11.6.1 | 11.6、附录 D |
| 1B 配置/GPU-hours/墙钟时间怎么估 | 48.3 | 04、14 |
| 评测分数不可信 | 41 | 38 |
| 事故/挂起怎么排查 | 32、42 | 附录 D/H |
| 该自建还是上云 | 44、45 | 43、46 |
| 怎么读上游源码 | 29 | 附录 M |

## 0.7 本章小结

- Megatron 解决的是“规模带来的系统性约束”，不是单个算子。
- 并行维度可以组合，但每一种组合都会引入新的数值与工程问题。
- 本教程用 mini 实现把关键路径压缩到可读规模，再用生产栈章节补齐差距。
- 阅读时始终问三个问题：**这一步的通信为什么存在？它改变了什么数值？失败时怎样定位？**

下一章从单卡训练的三堵墙（显存、算力、可靠性）开始，解释为什么需要多种并行，
并建立规模问题的第一张账本。
