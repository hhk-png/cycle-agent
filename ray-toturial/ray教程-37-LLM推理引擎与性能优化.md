仓库地址：https://github.com/hhk-png/cycle-agent

# 第 37 章：LLM 推理引擎与性能优化

> 这本书讲了很多层，但有一层始终没讲透。
>
> * 第 15 章讲了 **Ray Serve 怎么编排**（部署、扩缩、路由、PD 分离）；
> * 第 26 章讲了 **编译图怎么做低延迟传输**（通道、NCCL collective）；
> * 第 30 章讲了 **GPU 与集合通信**（`CUDA_VISIBLE_DEVICES`、NCCL、显存 OOM）；
> * 第 29 章讲了 **HuggingFace 怎么接进来**（`from_huggingface`、LoRA 微调）；
> * 第 12 章讲了 **批量推理怎么用 Ray Data 跑**（`vLLMEngineProcessorConfig`）。
>
> 但**引擎自己**那一层 —— **同一个模型、同一张卡，为什么换个引擎、
> 开一个开关，吞吐能差 5 到 10 倍** —— 一次都没讲过。
>
> > ⚠️ **一个措辞上的自我纠正（就在这一章写完后的独立复核里被抓到）**：
> > 这一段的初稿写的是「**「连续批处理」「投机解码」「CUDA Graph」
> > 在前 36 章里的命中数是 0**（这次是一句可验证的断言）」。
> > **这句话是站不住的** —— 只要把"前 36 章"理解成含第 00 章
> > （本书章号从 00 起），它就被**前言里的章节目录**证伪：
> > 第六轮刚给第 00 章加的那一节，逐字写着"投机解码""CUDA Graph"。
> > 而英文形式 `continuous batching` / `PagedAttention` 也确实在第 15 章 §15.4
> > 和第 29 章出现过（只是当类比提了一句，没有展开）。
> >
> > 准确的说法是定性的，**和本书前几轮修正过的那几条自我否证断言同一个形状**：
> > **本轮之前，全书没有任何一章真正讲过这三样东西怎么工作。**
> > 这件事本身值得记在这里 —— 它是全书 §0.6 那条态度的一次现场演示：
> > **越是"一 grep 就能证明"的断言，越要先真的 grep 一遍。**
>
> 这一章补的就是这一层。它回答的是这样一个问题：
>
> > **我照着第 15 章把服务起起来了，QPS 上不去、TTFT 忽高忽低，
> > 我该拧哪个旋钮？**
>
> 一句话概括这一章的立场：
> **Ray 负责"谁在哪、起几个"；引擎负责"这一批怎么算"。**
> 找错层，你会花几小时调 Ray 的 `autoscaling_config`，
> 而真正该动的是 `max_num_batched_tokens`。

---

## 37.1 先划清边界：编排层 vs 引擎层

这是全书反复强调的"哪一层负责"（这个概念在第 30 章 §30.9 讲得最透：
「所有 GPU 问题都应该先问一句：这该由哪一层负责？」）在推理场景里的具体化。

| 问题 | 负责的层 | 这一章讲吗 |
|---|---|---|
| 起几个副本 / 什么时候扩缩 | **Ray Serve**（`autoscaling_config`） | 不 —— 见第 15 章 §15.6、§15.17 |
| 请求路由到哪个副本（prefix/KV aware） | **Ray Serve LLM 的 router** | 不 —— 见第 15 章 §15.11 |
| prefill 与 decode 要不要拆开放 | **Ray Serve LLM（PD 分离）** | 不 —— 见第 15 章 §15.11 |
| 跨副本的 KV 怎么传（PD 分离里的 `kv_transfer_params`） | **Ray Serve LLM / 引擎的连接器** | 不 —— 见第 15 章 §15.11 |
| 模型权重怎么分到多卡（TP/PP） | **引擎 + Ray 的放置** | **半讲** —— §37.11 讲选型，第 30 章讲落地 |
| **一次前向里，哪些请求拼成一批** | **引擎（vLLM / SGLang）** | ✅ **这一章** |
| **KV cache 怎么存、放不下怎么办** | **引擎** | ✅ **这一章** |
| **同一段 prompt 反复出现，能不能不算第二遍** | **引擎** | ✅ **这一章** |
| **长 prompt 进来，会不会把别人卡住** | **引擎** | ✅ **这一章** |
| **用什么精度算、要不要开 CUDA Graph** | **引擎** | ✅ **这一章** |
| 多轮对话的状态放哪 | 应用层 / 引擎的 prefix cache | 半讲 —— §37.6 |

> ⚠️ **一个反复出现的误会**：很多人看到"吞吐上不去"，
> 第一反应是去调 Ray 的副本数和 `target_ongoing_requests`。
> 但如果你只有 1 个副本、GPU 利用率已经 90%，
> **扩副本只会让你多买几张卡做同样的事** —— 该调的是引擎的批处理参数。
> 判据很简单：**先看 GPU 利用率**（第 30 章 §30.6）。
> 利用率低 → 编排层（没喂饱）；利用率高但吞吐低 → 引擎层（算法/参数）。

---

## 37.2 先把度量定下来：四个数决定你该拧哪个旋钮

调优最大的陷阱是**优化了一个你并没有在测量的东西**。
推理服务只有四个数值得盯，它们各自对应完全不同的旋钮：

| 指标 | 全称 | 含义 | 用户感知 | 主要由谁决定 |
|---|---|---|---|---|
| **TTFT** | Time To First Token | 从发出请求到**第一个 token** 返回 | "卡不卡" | **prefill** 阶段（prompt 长度、chunked prefill、排队） |
| **TPOT** | Time Per Output Token | 之后**每个 token** 的平均间隔 | "吐字快不快" | **decode** 阶段（批大小、显存带宽） |
| **吞吐** | tokens/s 或 req/s | 单位时间产出 | 你的成本 | 批大小、并行度 |
| **并发** | 同时在飞的请求数 | —— | 它决定前三个 | 上面所有的综合结果 |

**它们是相互拉扯的**，这是整个调优的核心矛盾：

```
   加大批大小
        │
        ├──▶ 吞吐 ↑        （GPU 更忙，摊薄了权重读取）
        ├──▶ TPOT  ↑       （每个 token 要算更多序列，单步变慢）
        └──▶ TTFT  ↑↑      （新请求要排队等下一批，长 prompt 尤其惨）
```

所以"最优配置"**没有唯一答案**，取决于你的场景：

| 场景 | 优先保谁 | 倾向 |
|---|---|---|
| 在线对话（人盯着屏幕） | **TTFT** 和 **TPOT** | 小批、低延迟；宁可少赚吞吐 |
| 离线批量生成 | **吞吐** | 大批塞满；TTFT 无所谓 |
| Agent / 多轮工具调用 | **TTFT**（每轮都要等） | 前缀缓存收益最大 —— §37.6 |
| RAG（长 context 短回答） | **TTFT** | chunked prefill + prefix caching 是命门 |

> **一个 2026 年的现实**：Ray 侧的 `LLMConfig` 默认
> **`log_engine_metrics=True`**，会把引擎自己的指标
> （**prefix cache 命中率、TTFT、TPOT、KV cache 利用率、scheduler 状态**）
> 通过 Ray 的 Prometheus 端口导出 —— 但**要求你用 `_metrics_export_port` 初始化 Ray**，
> 否则那些指标没有出口。
> （这两句的出处是 Ray 2.58 源码里 `vLLMEngineProcessorConfig.log_engine_metrics`
> 这个字段自己的说明，**不是**第 15 章 —— 第 15 章没有讲指标导出这件事。）
> 只有知道了上面这张表，你才知道**为什么值得为它多配一个端口**。

---

## 37.3 批处理：从 static 到 continuous

这是推理引擎最核心的一个机制，也是**吞吐差距的最大来源**。

### static batching：等齐了再一起走

最朴素的做法：攒够 N 个请求，一起送进模型，一起等全部生成完，

```
时间 ──▶
请求 A（输出 5 个 token）  ████████░░░░░░░░░░░░
请求 B（输出 20 个 token） ████████████████████████
请求 C（输出 3 个 token）  ██████░░░░░░░░░░░░░░░░░
                           └──── 一批 ────┘
                           A 和 C 早就完了，但它们的槽位要等到 B 结束才能释放
```

**浪费在哪**：`░` 那些格子里，GPU 在等最慢的那个请求。
如果输出长度分布很散（真实场景总是这样），利用率可能只有 30%。

### continuous batching：谁先完谁先走

也叫 **iteration-level scheduling**（按"步"而不是按"批"调度）。
引擎每一步都重新决定这一批里放哪些序列：

```
时间 ──▶
请求 A  ████████
请求 B  ████████████████████████
请求 C  ██████
请求 D          ██████████████        ← 第 1 步之后就补进来了
请求 E                  ████████      ← 第 3 步之后又补进来
```

**关键差别**：**批是动态的，每一步都可以有请求离开、有请求加入**。
一个请求生成完 `</s>` 立刻退出，空出的槽位马上被排队的新请求填上。

**这是"高吞吐"的本质**：GPU 的空转格子被填满了。
现代引擎（vLLM 的 V1 引擎、SGLang、TensorRT-LLM）**默认都是 continuous batching**。

> **⚠️ 一个重要的顺序问题**：continuous batching 是"**decode 阶段**"的机制。
> 一个新请求要进来，得先做完 **prefill**（把它的 prompt 算一遍）。
> 如果 prefill 很长（比如 RAG 塞了 32K context），
> 它会占住整个批次好几步 —— 这就是 §37.7 要讲的 **chunked prefill**。

---

## 37.4 KV cache：显存的大头，以及它到底怎么算

要理解后面所有优化，必须先理解这一节。

### 为什么要缓存

自回归生成的每一步，都要对**前面所有 token** 做 attention。
如果不缓存，每生成一个新 token 就要把整段历史重算一遍 ——
生成长度 L 的序列，总计算量是 O(L²) 次的重复劳动。

KV cache 把每一步算出来的 **Key 和 Value 张量存下来**，
下一步直接复用。代价是**显存**。

### 它有多大（这个算式值得记住）

```
KV cache 字节数
  = 2（K 和 V）
  × num_layers
  × num_kv_heads          ← 注意不是 num_attention_heads（GQA 会少很多）
  × head_dim
  × dtype_bytes           ← fp16 = 2，fp8 = 1
  × 序列长度
  × 并发序列数
```

**代入一个具体例子**（Llama-3-8B 量级，`num_layers=32`、
`num_kv_heads=8`、`head_dim=128`、fp16）：

```
每个 token 的 KV ≈ 2 × 32 × 8 × 128 × 2 字节 = 131,072 字节 ≈ 128 KB
```

**记住这个数：每 token 约 128 KB**（GQA 模型）。那么：

| 并发 × 长度 | KV cache 占用 |
|---|---|
| 1 条 × 8K | ~1 GB |
| 32 条 × 8K | **~32 GB** |
| 8 条 × 128K | **~128 GB** |

这就是为什么 **长 context 的瓶颈从来不是算力，而是显存**。

### 三个直接推论

1. **并发上限由显存决定，不由算力决定。** 这解释了 §37.1 那个判据：
   如果你显存吃满了而 GPU 利用率不高，**加卡没用，减并发或量化才有用**。
2. **`max_model_len` 不要随手设成远超实际需求的值。**
   ⚠️ **机制要说准**（这里我自己在初稿里写错过，独立复核时被纠了）：
   在 **vLLM V1** 里，KV 池的大小是由
   `gpu_memory_utilization × 总显存 − 权重 − profiling 峰值激活` 算出来的，
   **不是**"按 `max_model_len` 预留"（那是 V0 时代的行为）。
   `max_model_len` 真正的作用是**两条**：
   * 它是**单条序列的长度上限**，引擎启动时会**校验** KV 池装得下它 ——
     设得太大可能**直接启动失败**（"max seq len is larger than …"）；
   * 它参与 `max_num_batched_tokens` 默认值的推导（旧路径下是
     `max(max_model_len, 默认值)` 再被 `max_num_seqs × max_model_len` 封顶）。
   所以"别乱设大"这条**实践建议仍然成立**，但理由是**启动校验与调度预留**，
   不是"白白锁住显存"。**具体行为以你那个版本为准。**
3. **`gpu_memory_utilization` 是"给模型执行器用的显存比例"** ——
   注意它**不只是** KV cache，权重、激活都在里面。
   引擎的做法是：先放权重，做完 profiling 看激活峰值，
   **剩下的才划给 KV cache**。
   ⚠️ **版本敏感**：这个参数的默认值改过 —— 0.90 用了很久，
   较新版本改成了 **0.92**（vLLM `CacheConfig.gpu_memory_utilization`）。
   **以你装的版本的 `--help` 为准**，不要背数字。

---

## 37.5 PagedAttention：把 KV cache 从"连续"变成"分页"

### 分页之前的问题

KV cache 是为每个序列**按最大长度预分配一块连续显存**的（早期做法）。
于是有三个浪费：

| 浪费类型 | 说明 |
|---|---|
| **内部碎片** | 预分配了 2048 个位置，实际只用了 300 个 → 剩下 1748 个空着 |
| **外部碎片** | 显存总量够，但没有一块**连续**空间满足 2048 → 分配失败 |
| **无法共享** | 两个请求有相同前缀，也只能各存一份 |

实测里这个浪费能到 **60%–80%** —— 也就是说 80 GB 的卡，
真正拿来存有用 KV 的只有 20 GB 上下。

### 分页的思路

**把操作系统的虚拟内存那一套搬过来**：

* KV cache 切成固定大小的 **block**（页），典型值 16 个 token 一块；
* 每个序列维护一张 **block table**（页表），记录逻辑块 → 物理块的映射；
* block **不需要连续**，物理上散落在显存各处就行；
* 分配按需进行 —— 序列长到需要新块时才分配。

**收益**：
* 内部碎片从"最多一个最大长度"降到"最多一个 block"（16 个 token）；
* 不再需要连续空间 → 外部碎片消失；
* 顺带得到了**共享**的能力（这就是下一节 prefix caching 的基础）。

这就是 **PagedAttention**（vLLM 的立身之作）。
**`block_size` 就是那个"页大小"** —— 调大能减少页表开销，
调小能减少内部碎片。**默认值请以你的版本为准**（长期是 16；
⚠️ 较新版本在该字段上写 `None` 作默认 —— 但**别把它读成"由后端自行决定"**：
源码注释的原话是 *"Accepts None (meaning \"use default\"). After construction,
always int."*，即 `None` 只是"用默认值"，构造完一定被解析成一个整数）

---

## 37.6 Prefix caching：同一个前缀，别算第二遍

### 为什么它对你特别值钱

回到 §37.2 的场景表：**Agent / 多轮对话 / RAG** 有一个共同特征 ——
**后面的请求和前面的请求共享一大段前缀**。

```
第 1 轮：「你是...（2000 token 系统提示）... 用户：你好」
第 2 轮：「你是...（同一段 2000 token）... 用户：你好 / AI：... / 用户：那这个呢」
                                        └──────── 完全相同的 2000 token ────────┘
```

没有 prefix caching：这 2000 个 token 每一轮都要**重新做一遍 prefill**，
每一轮都多花一份 TTFT。

有了 prefix caching：因为 KV cache 已经**按 block 分页**了，
引擎可以**按前缀的 hash 命中已有的 block**，直接复用，只算新增的那部分。

**注意这个因果关系**：prefix caching **依赖** PagedAttention 式的分页存储。
如果 KV 还是"每个序列一块连续内存"，共享根本无从谈起。

### 怎么用

* **引擎侧**：vLLM 的 `enable_prefix_caching` ——
  ⚠️ **版本敏感**：在 V1 引擎里它**默认是开的**
  （`CacheConfig.enable_prefix_caching` 默认 `True`），
  而更早的版本默认关闭。**先确认你的版本里它的状态**，
  因为"以为开了其实没开"会让你白调半天。
* **Ray 侧**：Ray Serve LLM 的路由演进方向就是 **prefix cache aware 路由**
  （第 15 章 §15.11 讲过 prefix cache → KV + token aware 的演进）。
  **机制上要记住**：prefix caching 是**单副本内**的，
  请求如果被路由到另一个副本，那份缓存就用不上 ——
  **路由策略和缓存策略必须一起考虑**，这是 Ray 层和引擎层的交界处。

### 一个反直觉的点

prefix cache 命中的判定是**按 block 的前缀 hash**，
所以**前缀必须从第 0 个 token 开始逐块完全一致**才算命中。
把变化的内容（时间戳、用户 ID、随机 few-shot 示例）放在**开头**，
会让整个缓存**全废**。**把稳定的部分放前面，变化的部分放后面** ——
这是零成本的收益，比任何参数调优都划算。

---

## 37.7 Chunked prefill：为什么一个长 prompt 会卡住所有人

### 问题

prefill 和 decode 是两种**计算特征完全不同**的工作：

| | prefill | decode |
|---|---|---|
| 一次算多少 token | 整个 prompt（可能几万） | 每序列 1 个 |
| 瓶颈 | **算力**（矩阵乘很大） | **显存带宽**（权重搬运是常数，算得少） |
| 是否适合和别的工作拼批 | 拼批效率高 | —— |

默认调度下，一个 32K 的 prompt 会**独占一个批次跑好几步**。
在这期间，**已经在中途的 decode 请求全部停摆** ——
用户看到的是"打字机突然卡住 2 秒"。

### 做法

**把一个长 prefill 切成小块，分摊到多个批次里**，
每个批次里既放"一块 prefill"也放"若干 decode"：

```
未开 chunked prefill:
  步 1: [████ prefill 32K(整段,占满整个 batch) ████]      ← 期间 decode 全停
  步 2: [decode] [decode] [decode] [decode]               ← 攒下来的请求一起走
        ⚠️ 注意:未开的时候 prefill **不会**被拆到多个 step ——
           它要么整段被调度,要么根本不调度(所以只有一步)

开了 chunked prefill:
  步 1: [prefill 8K] [decode] [decode]
  步 2: [prefill 8K] [decode] [decode]                     ← decode 一直在动
  步 3: [prefill 8K] [decode] [decode]
```

**代价**：那个长 prompt 自己的 TTFT 会**稍微变差**（它要分几步才跑完），
但**其他所有请求的 TPOT 不再被它拖累**。这是典型的"牺牲一个、救活一群"。

**旋钮**：`max_num_batched_tokens` 是一个批次能装多少 token 的上限 ——
它事实上就是"prefill 块的大小上限"。调大 → 单个长 prompt 更快，
但对别人影响更大；调小 → 整体更平稳。

⚠️ **版本敏感**：`enable_chunked_prefill` 在较新的 vLLM 里
**默认为 `True`**（`SchedulerConfig.enable_chunked_prefill = True`），
V1 引擎里这条路径事实上已经固定。旧版本默认关闭。**先查你的版本**。

> ⚠️ **和另一个说法的冲突**（第 15 章 §15.11 的排错表里有一条
> "vLLM 要求 `max_num_batched_tokens >= max_model_len`"）——
> 那条对应的是**chunked prefill 关闭**（或更早版本）的路径。
> **开了 chunked prefill 之后，`max_num_batched_tokens` 完全可以小于
> `max_model_len`** —— 本节讲的"32K prompt 切成 8K 块"正是建立在这上面。
> 两处按各自的版本前提读。

> **和第 15 章 PD 分离的关系**：PD 分离是把 prefill 和 decode
> **放到不同的副本（甚至不同的机器）上**，是**编排层**的解法；
> chunked prefill 是让它们在**同一个批次里共存**，是**引擎层**的解法。
> 两者解决同一个问题，可以叠加 —— 但先上 chunked prefill，
> 因为它的成本是 0（改一个参数），PD 分离的成本是加机器。

---

## 37.8 投机解码：用小模型换大模型的延迟

### 思路

decode 阶段每一步只算一个 token，是**显存带宽瓶颈**，
GPU 的算力大量闲置。投机解码（speculative decoding）利用这份闲置：

```
1. 草稿模型（小、快）先连着猜 k 个 token       ← 便宜
2. 大模型**一次前向**并行验证这 k 个猜测       ← 一次前向的成本 ≈ 生成 1 个 token
3. 从头开始，命中到第一个"猜错"的位置为止，接受前面全部正确的
4. 猜错的那个位置，用大模型自己的输出顶上
```

**为什么这是无损的**：验证过程保证输出分布和直接用大模型**完全一致**
（在正确实现下）。它是一个**纯粹的加速**，不是近似。

**收益取决于"接受率"**：草稿模型猜得越准，一次前向吃掉的 token 越多。
公开文献里常见的量级是**每次验证平均接受 2–4 个 token**，
对应大约 **2×–3× 的加速**（⚠️ 这个区间是**数量级**而非可引用的定值：
它强烈依赖草稿模型质量、`k` 的取值和采样参数，
**请以你自己负载上的实测为准**）。
如果接受率很低（草稿模型和主模型分布差太远），**反而会变慢** ——
多花的验证成本收不回来。

### 什么时候值得上

| 适合 | 不适合 |
|---|---|
| 单请求低延迟、batch 很小（GPU 本来就闲） | 大批量高吞吐（GPU 已经算满了，没闲置算力） |
| 输出有强模式（代码、结构化输出、翻译） | 高度开放、随机的生成 |
| 你有一个同系列的小模型 | 找不到分布接近的草稿模型 |

**注意这个反直觉的结论**：投机解码**提升延迟、不提升吞吐**，
在大 batch 场景下常常**让吞吐变差**。
所以它是 §37.2 里"在线对话优先保 TTFT/TPOT"那一栏的工具，
不要无脑开。

**其他形态**：还有 **Medusa / EAGLE** 这类"用额外的小预测头代替独立草稿模型"的做法，
原理同源（都是"猜多个 + 一次验证"），省掉了维护第二个模型。

---

## 37.9 量化：省显存、换精度

### 两笔账要分开算

量化影响**两个不同的东西**，很多人把它们混在一起：

| 量化对象 | 省的是什么 | 影响 |
|---|---|---|
| **权重**（W4/W8/AWQ/GPTQ） | **模型权重的显存** | 权重显存减半/减到 1/4，**KV cache 不变** |
| **KV cache**（fp8 / int8） | **KV cache 的显存** | 并发上限直接翻倍，**权重不变** |

**这是两件事**。§37.4 算过 KV cache 每 token ~128 KB ——
如果你的瓶颈是并发数（§37.4 推论 1），
**量化权重的收益很小，量化 KV cache 的收益才是直接的**。

### 常见的几种

| 名称 | 位宽 | 特点 |
|---|---|---|
| **FP8** | 8 | 新卡（Hopper 及以后）原生支持，通常几乎无精度损失，是当前首选 |
| **INT8 / W8A8** | 8 | 通用性好，需要校准 |
| **AWQ / GPTQ** | 4 | 权重量化，4 bit；质量损失比 8 bit 明显，但通常可接受 |
| **GGUF 系列** | 2–8 | 面向 CPU / 混合推理的格式家族 |

**代价与风险**：
* **精度**：4 bit 在长链推理、代码生成上更容易掉点，**必须自己评测**，
  不要相信"无损"的宣传语；
* **兼容性**：不是所有模型、所有卡、所有引擎都支持同一套量化格式 ——
  选之前先查引擎的支持矩阵；
* **可能反而变慢**：如果缺少对应的 kernel（比如老卡上跑 FP8），
  会退化到反量化再计算，**比不量化还慢**。

> **一个务实的顺序**：先试 **FP8 KV cache**（省显存、风险低、收益直接），
> 再看权重 8 bit，**最后**才考虑 4 bit。

---

## 37.10 CUDA Graph：消掉 kernel 启动开销

decode 阶段每步的计算量很小（每个序列 1 个 token），
但一次前向要启动**几百个 kernel**。每个 kernel 的启动开销是**微秒级**的，
几百个累加起来，在**小 batch 低延迟**场景下能占到总时间的相当一部分 ——
GPU 大量时间花在"等 CPU 把下一个 kernel 派下来"。

**CUDA Graph** 的做法：把一整串 kernel 的调用**录制成一张图**，
之后每次执行**整个图**，由驱动一次性提交，跳过逐个启动的开销。

**适用条件（很严格）**：
* **形状必须固定** —— 图是录死的，batch 大小、序列长度一变就得重录。
  所以它**只在固定形状的解码路径上有效**；
* 通常需要**静态的输入/输出缓冲区**；
* 动态控制流（比如 early exit）会让录制失败。

**实践含义**：
* 开了它，**batch 大小会被限制在若干个"捕获过的形状"上**，
  灵活性下降，换来的是低延迟；
* 引擎里的开关通常叫 `enforce_eager` 的**反面** ——
  `enforce_eager=True` 表示**禁用**图（强制 eager 执行）。
  **调试时把它打开**（关掉 CUDA Graph），因为图执行下的报错栈几乎不可读；
  **上线时关掉它**（启用图）。

---

## 37.11 并行：TP / PP / DP 在推理侧怎么选

| 方式 | 切什么 | 通信量 | 什么时候用 |
|---|---|---|---|
| **TP**（张量并行） | 把每一层的矩阵切到多卡 | **极高**（每层都要 all-reduce） | 单卡**装不下**模型时。要求卡间带宽高（NVLink 优先） |
| **PP**（流水线并行） | 把不同层放到不同卡 | 低（只在层边界传激活） | 层数很多、跨机带宽差时 |
| **DP**（数据并行） | 每张卡一份完整模型，切请求 | **零**（推理时互不通信） | **默认首选** —— 除非模型装不下 |

**推理和训练的选择逻辑是相反的**：

* 训练时 TP 是为了**放得下 + 算得快**，通常必须用；
* **推理时 DP 的性价比通常高得多**：每张卡一份完整模型，
  请求各走各的，**没有任何通信开销**，扩缩容也简单（加卡 = 加副本）。
* **只有当模型装不进单卡**（比如 70B 在 80GB 卡上）时，才不得不动用 TP。

**和 Ray 的接法**：TP 的大小通过引擎参数（`tensor_parallel_size`）
经 `engine_kwargs` 传下去，而**卡从哪来**由 Ray 决定 ——
这正是第 30 章 §30.7 讲的"拓扑与放置"。
**常见的坑**：TP=8 跨了两台机器，卡间只有以太网，
通信成为瓶颈，**比单卡还慢**。用 Ray 的放置组 + 节点亲和
（第 8 章 §8.5）把同一组的卡约束到同一台机器上。

---

## 37.12 这些旋钮在 Ray 上怎么拧

上面全是引擎概念。**在 Ray 里，它们统一从 `engine_kwargs` 进去** ——
这是 Ray 层与引擎层的正式接口。

### 在线服务：`LLMConfig.engine_kwargs`

```python
from ray import serve
from ray.serve.llm import LLMConfig, build_openai_app

llm_config = LLMConfig(
    model_loading_config={
        "model_id": "my-model",
        # 也可以直接给 model_source（本地路径 / 云存储 / 云镜像配置）
        # "model_source": "/mnt/models/llama-3-8b",
    },
    # accelerator_type="L4",                 # 约束加速器型号
    engine_kwargs={
        # ↓ 这里就是本章讲的旋钮，原样透传给 vLLM
        "max_model_len": 8192,               # §37.4 推论 2：别虚报
        "gpu_memory_utilization": 0.90,      # §37.4 推论 3（默认值版本相关）
        "enable_prefix_caching": True,       # §37.6
        "enable_chunked_prefill": True,      # §37.7
        "max_num_batched_tokens": 8192,      # §37.7 的块大小
        "max_num_seqs": 256,                 # §37.3 的批上限
        "kv_cache_dtype": "fp8",             # §37.9 的第二笔账
        "tensor_parallel_size": 1,           # §37.11：先 DP，装不下才 TP
        # "enforce_eager": True,             # §37.10：调试时打开
        # "speculative_config": {...},       # §37.8
    },
    # 引擎自己的指标（TTFT/TPOT/prefix cache 命中率…）从 Ray 的 Prometheus 端口出
    log_engine_metrics=True,
)

app = build_openai_app({"llm_configs": [llm_config]})
serve.run(app, blocking=True)
```

**`LLMConfig` 的字段全景**（对照上面代码看，哪些是 Ray 的、哪些是引擎的）：

| 字段 | 归谁管 | 作用 |
|---|---|---|
| `model_loading_config` | Ray | `model_id` / `model_source` / `tokenizer_source` |
| `llm_engine` | Ray | 引擎枚举（**当前只有 `"vLLM"` 一个值**） |
| **`engine_kwargs`** | **引擎** | **本章所有旋钮走这里** |
| `accelerator_type` | Ray | 约束用哪种加速卡 |
| `accelerator_config` | Ray | 加速卡的**硬件相关参数**（schema 由 `kind` 判别字段动态决定） |
| `placement_group_config` | Ray | `bundle_per_worker` / `bundles` / `strategy`（§37.11 的拓扑约束） |
| `lora_config` | Ray | `dynamic_lora_loading_path` / `max_num_adapters_per_replica` 等 |
| `deployment_config` | Ray Serve | 副本数、`autoscaling_config`（第 15 章） |
| `server_cls` | Ray | 换后端（除了 vLLM 还有 `SGLangServer` 等，见 §37.15） |
| `experimental_configs` | Ray | `stream_batching_interval_ms`（流式攒批） / `num_ingress_replicas` |
| `log_engine_metrics` | Ray | **默认 `True`**；需要 `_metrics_export_port` |
| `callback_config` | Ray | 模型初始化时的回调（可传类路径或 `Callback` 子类） |
| `runtime_env` | Ray | 依赖注入（附录 F） |

> ⚠️ **别把两个"批"搞混**（和 §12.4 那个 `concurrency` 的坑一样，属于**同名不同物**）：
> * `engine_kwargs` 里的 `max_num_seqs` / `max_num_batched_tokens` 是**引擎**的批；
> * `experimental_configs.stream_batching_interval_ms` 是 **Ray Serve** 把
>   *流式响应* 攒起来一起发的批 —— 它省的是 HTTP/框架开销，不是 GPU 计算。
>
> 名字都叫 batch，**层次完全不同**。

### 离线批量：`ray.data.llm`

离线跑几百万条时，引擎概念一样适用，只是入口换成第 12 章 §12.4 的 processor：

```python
from ray.data.llm import vLLMEngineProcessorConfig, build_processor

config = vLLMEngineProcessorConfig(
    model_source="meta-llama/Meta-Llama-3.1-8B-Instruct",   # ⚠️ 字段名是 model_source
    engine_kwargs={
        "max_model_len": 8192,
        "enable_chunked_prefill": True,
        "max_num_batched_tokens": 8192,     # 离线可以调大：只求吞吐
        "kv_cache_dtype": "fp8",
    },
    concurrency=4,                          # 默认是 (1,4) 的伸缩池；关掉 autoscaling 才是固定 4 池
    batch_size=64,                          # 进引擎前每批多少行
    log_engine_metrics=True,
)
processor = build_processor(config, preprocess=..., postprocess=...)
ds = processor(ray.data.from_items(prompts))
```

**在线和离线的取向差异，直接体现在参数上**：

| 参数 | 在线（保延迟） | 离线（保吞吐） |
|---|---|---|
| `max_num_batched_tokens` | 小（prefill 别挤别人） | **大**（塞满 GPU） |
| `max_num_seqs` | 中（留余量给新请求） | **大** |
| 投机解码 | 可能值得 | **通常不值**（§37.8） |
| 量化 | KV cache fp8 收益大 | 权重 + KV 都值得 |

> 📌 `ray.data.llm` **仍是 beta**：`build_processor` 在很新的 vLLM 上
> 曾因 `vllm.inputs.data` 被移除而报错（第 12 章 §12.4 记了这个坑）。
> 引擎升级要连同这层一起验证。

---

## 37.13 Ray 在这条链路上到底加什么值

既然引擎自己就能跑，为什么还要 Ray？**这个问题值得正面回答**
（第 20 章从"Ray 在推理栈里正收缩为放置与调度层"的角度谈过）。

| Ray 提供的能力 | 引擎自己有吗 | 说明 |
|---|---|---|
| **多副本 + 自动扩缩** | 无 | 引擎是进程内的，副本管理是编排问题 |
| **跨副本路由**（含 prefix/KV aware） | 无 | 引擎只看得到自己那一份 KV cache |
| **PD 分离** | 部分（引擎内有） | Ray 能做**跨机器**的 prefill/decode 池 |
| **自动扩缩到 0** | 无 | `downscale_to_zero_delay_s`（第 15 章 §15.17） |
| **GPU 放置与拓扑约束** | 无 | 放置组把 TP 组约束到同一台机器（§37.11） |
| **模型多路复用** | 部分 | 一套副本服务多个 LoRA / 模型（第 15 章 §15.16） |
| **批处理（离线）** | 无 | Ray Data 的流式执行器 + 背压（第 12 章） |
| 引擎内部的调度策略 | **有——这是引擎的强项** | **不要试图在 Ray 层模拟它** |

**一句话**：Ray 负责**把 GPU 摆好、把请求送对地方、把副本数调对**；
引擎负责**在这一块 GPU 上把这一批算得尽可能快**。
**两层各自有明确的旋钮，混着调就是浪费时间。**

---

## 37.14 调优顺序表与排错清单

### 按症状查（这个表比上面的参数表更该收藏）

| 症状 | 先查（引擎层） | 再查（编排层） |
|---|---|---|
| **TTFT 高，长 prompt 时特别明显** | 是否开了 **chunked prefill**；`max_num_batched_tokens` 是否太小 | 副本数够不够；有没有被其他长请求挤 |
| **TTFT 高，且 prompt 高度重复** | **prefix caching 开了吗**；有没有把变动内容放在 prompt 开头（§37.6） | 路由是不是把相同前缀的请求打散到了不同副本 |
| **TPOT 高** | batch 是不是太大（`max_num_seqs`）；**KV cache 是不是快满了**导致频繁换出 | 单副本 GPU 是否被别的进程抢 |
| **吞吐上不去，但 GPU 利用率低** | batch 上限太小；请求没喂满（`stream_batching_interval_ms`） | **副本数/并发路由** —— 这才是编排层该管的 |
| **吞吐上不去，GPU 利用率高** | 已经是引擎算法问题：考虑量化、投机解码（批大时通常无效）、TP | 加副本（但先确认单副本已榨干） |
| **吞吐忽高忽低、毛刺严重** | 长 prompt 冲击 → chunked prefill；CUDA Graph 形状重录 | 扩缩容抖动（`upscale_delay_s` / `downscale_delay_s`） |
| **OOM（显存）** | `gpu_memory_utilization` / `max_model_len` / `max_num_seqs` **三者共同决定 KV 池与并发上限**（§37.4） | 副本挤在同一张卡上（第 30 章 §30.2 的分数 GPU） |
| **报错栈完全看不懂** | `enforce_eager=True` 关掉 CUDA Graph 再看 | —— |
| **改了参数没效果** | 这个版本**支持**这个参数吗；参数名对不对（⚠️ **key 拼错通常会在引擎初始化时就报错** —— vLLM 的配置禁止额外字段，所以"拼错"一般不是静默的）；**参数名对、但语义不对**才是真正的静默失败源 | 参数有没有真的透传到引擎（Ray 侧只做 `engine_kwargs.copy()` 后原样转发，不校验） |

### 一个推荐的调优顺序

不要一次改五个参数 —— 你分不清是谁的功劳。按这个顺序单变量推进：

1. **先测基线**：固定一个真实负载，记下 TTFT / TPOT / 吞吐 / GPU 利用率；
2. **`max_model_len` 对齐真实需求**（§37.4 推论 2）—— 常常是最大的一笔白捡收益；
3. **开 prefix caching**（§37.6）—— 如果你的 prompt 有公共前缀；
   顺手把 prompt 里**变动的部分挪到后面**（零成本）；
4. **开 chunked prefill**（§37.7）—— 治 TTFT 毛刺；
5. **调 `max_num_seqs` / `max_num_batched_tokens`**（§37.3、§37.7）——
   在 TTFT 和吞吐之间找你要的那个点；
6. **KV cache fp8**（§37.9）—— 瓶颈是并发时收益最直接；
7. **最后**才考虑 TP（§37.11）、投机解码（§37.8）、权重 4 bit（§37.9）。

> ⚠️ **每一步都要回测**。这些机制**会互相影响**：
> 开了 chunked prefill 之后，`max_num_batched_tokens` 的含义变了；
> 开了 prefix caching 之后，TTFT 的分布变了（命中与不命中差很多），
> **只看平均值会被骗** —— 要看 P50 / P95 / P99（第 11 章 §11.5）。

---

## 37.15 推理引擎全景与分离式服务

前面 14 节讲的机制（continuous batching、PagedAttention、prefix caching、
chunked prefill…）**不是 vLLM 独有的**，它们是这一代推理引擎的**共同骨架**。
这一节回答两个"地图"问题：

* 除了 vLLM / SGLang，还有哪些引擎？它们各自站在哪，**在 Ray 上到底能不能直接用**？
* 如果你听过 **Dynamo / LMCache / NIXL** 这几个词 ——
  它们和 Ray Serve 究竟是**协作**还是**竞争**？

> ⚠️ **本节的规矩**：能回源码核对的，都给出**可复现的核实命令**；
> 核对不了的一律写"**未确认**"并告诉你该去哪儿查。
> **不背 API 名，也不抄 benchmark 数字** —— 那些数字依赖模型、卡型、
> 并发和采样参数，抄过来只会误导（§37.8 那条"数量级而非定值"的纪律同样适用）。

### 七个引擎，各自站在哪

| 引擎 | 定位 | 适合谁 | 在 Ray 上怎么用 |
|---|---|---|---|
| **vLLM** | 通用 GPU 推理引擎，PagedAttention 的出处 | **默认首选**：既要吞吐又要生态 | ✅ **官方一等公民**。`LLMConfig` 的引擎枚举里只有它；离线走 `ray.data.llm.vLLMEngineProcessorConfig` |
| **SGLang** | 前缀密集 / 结构化输出 / 多轮对话见长 | prompt 高度重复（Agent、RAG）、要 RadixAttention 类收益 | ✅ **官方支持**：`LLMConfig(server_cls=...)` 指向 `SGLangServer`；离线走 `SGLangEngineProcessorConfig` |
| **TensorRT-LLM** | NVIDIA 官方栈，压榨单机极限延迟 | 已锁定 NVIDIA 全家桶、愿意换编译流程 | ❌ **Ray 2.58.0 无官方集成** → 自己包 actor（见下） |
| **Triton Inference Server** | **不是引擎**，是多框架推理**网关**（TensorRT-LLM / PyTorch / ONNX / Python backend） | 已有非 LLM 模型要统一托管 | ❌ **无官方集成** → 当普通 HTTP 后端代理 |
| **LMDeploy** | 主打国产模型（Qwen / GLM / DeepSeek 等）+ W4A16 量化，支持多种硬件后端 | 私有化部署、非 NVIDIA 加速卡 | ❌ **无官方集成** → 自己包 actor |
| **TGI** | HuggingFace 官方 server，开箱即用 | 想最快跑起来、模型全在 HF 上 | ❌ **无官方集成** → 当普通 HTTP 后端代理 |
| **llama.cpp** | CPU / Apple Silicon / 边缘，GGUF 格式 | 没有 GPU、或要跑在笔记本/边缘盒子上 | ❌ **无官方集成** → 自己包 actor |

**怎么读这张表**：**Ray 的官方集成面很窄，只有 vLLM 和 SGLang。**
这不是巧合 —— `LLMConfig` 的引擎枚举
（`ray/llm/_internal/serve/core/configs/llm_config.py` 里的 `class LLMEngine`）
**只有 `vLLM` 一个值**；SGLang 走的是另一条口子 `server_cls`
（源码里该字段的说明原文是 *"e.g., LLMServer, SGLangServer or other Server backends"*），
实现落在 `python/ray/llm/_internal/serve/engines/sglang/`。

"**没有官方集成**"这个判断，你可以自己复核（这也正是 §0.6 那条纪律）：

```bash
# 把 ray 的安装目录挖出来，在里面搜这几个引擎的名字
python -c "import ray,os;print(os.path.dirname(ray.__file__))" \
  | xargs -I{} grep -ril "lmdeploy\|tensorrt\|llama\.cpp" {}
# 反向验证：这个名字一定搜得到（说明命令本身是有效的）
python -c "import ray,os;print(os.path.dirname(ray.__file__))" \
  | xargs -I{} grep -ril "sglang" {}
```

**没有集成 ≠ 不能用**，它只意味着**你得自己包一层**。两条正路：

* **在线**：写一个普通的 Ray Serve deployment，后端把请求转成该引擎的
  HTTP（OpenAI 兼容端点是通用接口），扩缩容和路由仍然交给 Serve ——
  你放弃的是 §37.6 那条 **prefix/KV cache aware 路由**（Ray 只对内置引擎
  知道 KV 布局）；
* **离线**：用一个 Ray Data 的 actor 池（`map_batches` + `ActorPoolStrategy`），
  每个 actor 持有一个引擎实例 —— **这就是第 12 章 §12.4 那套形态**，
  和第 29 章 §29.6 的 `VLLMPredict` 写法是同一个模子。

⚠️ **未确认**：TensorRT-LLM / Triton 是否有**非 Ray 官方**但被 NVIDIA 维护的
Ray 集成（例如随 TensorRT-LLM 仓库发布的示例）。上面的 grep 只证明
**Ray 自己这一侧**没有。请以对应项目的官方文档为准。

### 分离式服务：三个词，三件事

先别被缩写绕晕。**"分离式服务"（disaggregated serving）是把三件不同的事
放在了一起讲**，它们可以单独使用、也可以叠加：

| 词 | 它到底解决什么 | 典型角色 |
|---|---|---|
| **PD 分离** | 把 **prefill** 和 **decode** 拆到不同的副本/机器上（§37.7 讲过它和 chunked prefill 的取舍） | **编排策略** |
| **KV 传输** | prefill 算出来的 KV 怎么**搬到** decode 那一侧 | **数据面**（NIXL 在这里） |
| **KV 池化 / 分层** | KV 不只放显存，还能落到 CPU / 盘 / 远端，跨副本复用 | **缓存层**（LMCache 在这里） |

#### NVIDIA Dynamo：和 Ray Serve 是**协作，也带张力**

Dynamo 是 NVIDIA 开源的**推理编排层**，位于引擎之上，
把 vLLM / SGLang / TensorRT-LLM 组织成多机系统，自己做 KV-aware 路由、
KV Block Manager 和基于 SLA 的扩缩容（Planner）。
**这听起来和 Ray Serve LLM 高度重叠** —— 但 2.58.0 的现实是**它被接进来了**：

**源码级证据**（不是博客）：`ray/llm/_internal/serve/routing_policies/kv_aware/`
下的 `kv_aware_router.py` 里，`KVAwareRouter` 的类注释逐字写着
*"Scoring is delegated to the `KVTokenTracker` (which owns the
**Dynamo selection service** and the global KV index)"*，
另一处写着 *"Maps the candidate replicas to their **Dynamo worker ids**"*；
同目录的 `constants.py` 里还有一句
*"**Dynamo's selection service** dials it to recover events missed…"*。

所以准确的表述是：**Ray Serve LLM 的 KV-aware 路由复用了 Dynamo 的选择服务**，
Ray 保留自己的事件面 / 请求面 / 数据面与 worker 注册，
Dynamo 负责维护 KV 索引、算 cache overlap 与负载分数。
**这是协作。** 但两者**控制面仍然重叠**（Ray 是多跳的队列驱动扩缩，
Dynamo 是读 TTFT/ITL 指标直接调 `/scale`）——
社区在 ray-project/ray **issue #65690** 里正讨论这件事，
其中"用一层薄包装把 Dynamo 塞进 Ray、同时关掉 Dynamo 自己的 planner"
被质疑是反模式。**"该由谁的 autoscaler 说了算"这个问题目前没有定论 —— 标未确认。**

> ⚠️ **别把"协作"读成"Ray 是 Dynamo 的宿主"**：反过来也成立 ——
> Dynamo 可以完全不用 Ray 运行。**"Dynamo on Ray" 只是若干部署形态之一。**

#### LMCache：KV 的"分层缓存"

**它解决的是"KV 放不下 / 想跨请求复用"** —— 把 KV cache 从显存卸载到
CPU 内存、本地盘、Redis、S3 或远端，并能跨实例共享。
定位上它和 §37.6 的 prefix caching 是**同一目标的放大版**：
prefix caching 在**单副本的显存内**复用，LMCache 把它扩到**显存之外、副本之外**。

在 Ray 侧它是**一等公民**：`kv_connector: "LMCacheConnectorV1"`
（后端的注册名和实现路径见
`python/ray/llm/_internal/serve/engines/vllm/kv_transfer/factory.py`
里的 `BUILTIN_BACKENDS`）。官方文档页是
`docs.ray.io` 上的 **Serve LLM → KV cache offloading**。

#### NIXL：**只是传输层**

NIXL（NVIDIA Inference Xfer Library）**不是缓存、也不是引擎** ——
它是**点对点的 KV 传输库**（可用 UCX / libfabric / EFA 等后端，
走 NVLink / InfiniBand 这类高带宽通路）。
它解决的是 PD 分离里那句"**KV 怎么从 prefill 那台机器搬到 decode 那台**"。

Ray 侧同样有官方对接：`kv_connector: "NixlConnector"`。
另外 Ray 2.58.0 里还有一处**和 LLM 无关**的用法 ——
`python/ray/experimental/rdt/` 下的 `nixl_memory_pool.py` /
`nixl_tensor_transport.py`：Ray 的实验性张量直传模块把 NIXL 当作
**可选传输后端之一**（同目录还有 `cuda_ipc_transport.py` /
`collective_tensor_transport.py`）。
**同一根传输管，两个用途，别混。**（该模块是 `ray.experimental.*`，
按约定随时可能变；它的缩写展开本书**未确认**。）

#### 三者怎么叠加：`MultiConnector`

想同时要"跨实例搬 KV"和"本地分层缓存"，就用 `MultiConnector`
把两者串起来（顺序有讲究：**先查本地缓存、再走网络传输**）。
Ray 2.58.0 的 `BUILTIN_BACKENDS` 里一共注册了四个：

```python
# python/ray/llm/_internal/serve/engines/vllm/kv_transfer/factory.py
BUILTIN_BACKENDS = {
    "LMCacheConnectorV1": ...,
    "NixlConnector": ...,
    "MultiConnector": ...,
    "MoRIIOConnector": ...,   # 另一个传输后端（本书未展开，未确认其细节）
}
```

#### 在 Ray 上怎么用：PD 分离的入口

**Ray 2.58.0 已经把 PD 分离做成了一等公民**，入口是
`ray.serve.llm.build_pd_openai_app`（标 `@PublicAPI(stability="alpha")`），
配套的 server 类在 `ray/serve/llm/deployment.py` 里：
`PDPrefillServer` / `PDDecodeServer` / `DPServer`
（另有 `PDProxyServer`，源码里带弃用告警，说它将被
`PDDecodeServer` + `PDPrefillServer` 取代）。

配置**不用手写代码**，YAML 就够 —— 这是 Ray 2.58.0 仓库里
`doc/source/serve/doc_code/pd_dissagregation/nixl_example.yaml` 的骨架：

```yaml
applications:
  - args:
      prefill_config:
        model_loading_config:
          model_id: meta-llama/Llama-3.1-8B-Instruct
        engine_kwargs:
          kv_transfer_config:
            kv_connector: NixlConnector     # ← 换成 LMCacheConnectorV1 就是另一条路
            kv_role: kv_producer            # ← prefill 侧是生产者
            engine_id: engine1
      decode_config:
        model_loading_config:
          model_id: meta-llama/Llama-3.1-8B-Instruct
        engine_kwargs:
          kv_transfer_config:
            kv_connector: NixlConnector
            kv_role: kv_consumer            # ← decode 侧是消费者
            engine_id: engine2
    import_path: ray.serve.llm:build_pd_openai_app
    name: pd-disaggregation-nixl
    route_prefix: "/"
```

**四个字段名是确定的**（就是上面那四个）：
`kv_transfer_config` / `kv_connector` / `kv_role`（`kv_producer` / `kv_consumer`）
/ `engine_id`。**先跑通最简单的 `NixlConnector`，再加缓存层** ——
和 §37.7 那句"先上 chunked prefill，再考虑 PD 分离"是同一个顺序逻辑。

> ⚠️ **两条必须写清的边界**：
> ① **PD 分离要加机器**（prefill 池 + decode 池各自要副本），
> 所以它排在 chunked prefill 之后 —— 后者改一个参数就能试；
> ② **KV-aware 路由的生态还没定型**。2.58.0 里
> `ray/serve/llm/request_router.py` 公开的是
> `PrefixCacheAffinityRouter` / `KVAwareRouter` 两个类，
> 配置入口是 `ray.serve.config.RequestRouterConfig`；
> 会话粘性那一路则由 **`ray/serve/experimental/consistent_hash_router.py`
> 的 `ConsistentHashRouter`** 提供（**experimental**）——
> 它 **在** 2.58.0 里，但**默认不生效**：默认的 Power-of-Two-Choices
> 路由根本不读 `session_id`，必须显式挂到 deployment 的
> `request_router_config` 上（挂法见第 15 章 §15.7）。
>
> > ⚠️ **这一条在被写进本书时曾经是错的，值得留个记录。**
> > 初稿写的是"`ConsistentHashRouter` **不在** 2.58.0 里"，
> > 依据是"我列了 `python/ray/serve/_private/request_router/` 目录，里面没有它"。
> > **这是同一个错误的两个侧面：只查了一个目录，就把结论下到了整个包上。**
> > 它实际在 **`python/ray/serve/experimental/`** 下 —— 而第 15 章 §15.7
> > 早在上一轮就已经把它写进"三个可选 router"表里了，
> > 也就是说**本书自己就证伪了这句话**。
> > 教训和第 00 章 §0.6 那条一样，只是换了个形状：
> > **"我在某个地方没找到"永远不等于"它不存在"。**
> > 正确的核实命令要查两个目录（下面这条已经改对了）。
>
> ```bash
> # 查对了的样子：两个目录都查
> curl -s "https://api.github.com/repos/ray-project/ray/contents/python/ray/serve/_private/request_router?ref=ray-2.58.0" | grep '"name"'
> curl -s "https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/python/ray/serve/experimental/consistent_hash_router.py" | grep -n "^class "
> # → 39:class ConsistentHashRouter(RequestRouter):
> ```

### 一句话总结这一节

**引擎层在收敛（大家共用同一套机制），编排层在分裂（谁做 KV 感知路由、
谁做扩缩容还没有共识）。** 对你的实际含义是：

* **选引擎看负载特征，别看热度** —— 前缀密集看 SGLang，通用看 vLLM，
  锁定 NVIDIA 且要极限延迟才看 TensorRT-LLM；
* **"在 Ray 上"目前只有 vLLM / SGLang 是官方路径**，其余的要自己包一层；
* **分离式服务先别急着上**：它解决的是"单机已经榨干、要跨机分工"的问题，
  而大多数人的瓶颈还在 §37.14 那张症状表的前几行。

---

## 37.16 与 mini-ray 的关系

**直说：mini-ray 完全没有实现这一章的任何内容，而且这是对的。**

这一章讲的是 **GPU 上的注意力计算与显存管理**。
mini-ray 的定位是「纯 Python + NumPy，演示 Ray 的**分布式编排**机制」——
它连 GPU 都不碰（第 30 章 §30.9 有同样的说明）。

**但有三处类比值得记住**，因为它们说明"引擎层的优化"和
"分布式层的优化"其实是同一类思维：

| 本章的机制 | mini-ray 里的同构物 | 相通的思路 |
|---|---|---|
| **用 block table 映射分页的 KV cache** | 对象存储的**分段 + 空闲链表**（`object_store.py`） | 两者都在解决**变长数据 + 碎片**；都把"逻辑"和"物理"解耦 |
| **continuous batching 逐步重排批次** | raylet 的**调度循环**（`_schedule_tasks` 每一轮重新挑） | 都是"**不做一次性分配，每一轮重新决策**"——静态分配的浪费是同一个根因 |
| **prefix caching 按前缀 hash 复用** | **lineage 重建要求"任务确定性 + 对象 ID 稳定"**（第 10 章 §10.4） | 都要求"**可寻址的、内容确定的单元**"才能复用 |

**共同的那句话**：
> **静态分配 → 碎片和浪费；按需分配 + 可寻址的单元 → 复用。**
>
> 这条在分布式编排层叫"对象存储 + 引用计数"，
> 在推理引擎层叫"PagedAttention + prefix caching" —— **是同一个洞察的两个化身**。

这也是为什么把它放在 mini-ray 的对照里讲得通：
mini-ray 教不了你调 vLLM，但它教的**分配/复用/重排**那套直觉，
正是理解这一章的底子。

---

## 37.17 本章小结

* **编排层和引擎层是两个层。** 判据：**先看 GPU 利用率** ——
  低 → 编排层（没喂饱）；高但吞吐低 → 引擎层（算法/参数）。
* **四个数**：TTFT（prefill 决定）、TPOT（decode 决定）、吞吐、并发。
  它们**相互拉扯** —— 加大批大小换吞吐，代价是 TTFT 和 TPOT。**没有全局最优**。
* **KV cache 是显存大头**，量级记住 **每 token ~128 KB**（GQA 模型，fp16）。
  推论：**并发上限由显存决定，不由算力决定**。
* **PagedAttention** 是后面一半机制的地基：
  分了页，才有 prefix caching 的共享。
* **Prefix caching 的零成本收益**：把 prompt 里**变化的部分挪到后面**。
  另外记住它是**单副本内**的 —— **路由策略必须和它一起考虑**。
* **Chunked prefill** 是解决 TTFT 毛刺的第一把刀，
  代价是那个长请求自己稍微慢一点。
* **投机解码提升延迟、不提升吞吐** —— 大 batch 下常常是负收益。
* **量化的两笔账要分开**：量化权重省的是权重显存；
  量化 KV cache 才直接提高并发上限。
* **CUDA Graph** 用灵活性换低延迟；**调试时用 `enforce_eager=True` 关掉它**。
* **推理侧默认选 DP，不是 TP** —— 只有单卡装不下才动用 TP，
  且必须用放置组把 TP 组约束到同一台机器。
* **Ray 管"谁在哪、起几个"；引擎管"这一批怎么算"。**
  `engine_kwargs` 就是这两层的正式接口。
* **引擎全景（§37.15）**：Ray 的**官方集成面只有 vLLM 和 SGLang**
  （`llm_engine` 枚举里只有 `vLLM`；SGLang 走 `server_cls`）；
  TensorRT-LLM / Triton / LMDeploy / TGI / llama.cpp
  **在 2.58.0 里都要自己包一层**（在线包 Serve deployment，离线包 actor 池）。
  **选引擎看负载特征，别看热度。**
* **分离式服务是三件事**（§37.15）：**PD 分离**（编排）、
  **KV 传输**（NIXL，数据面）、**KV 分层缓存**（LMCache，缓存层）。
  Ray 侧的确定入口是 `build_pd_openai_app` +
  `kv_transfer_config` 的四个字段（`kv_connector` / `kv_role` /
  `engine_id` / `kv_transfer_config` 本身）——
  内置 connector 是 `LMCacheConnectorV1` / `NixlConnector` /
  `MultiConnector` / `MoRIIOConnector`。
  **Dynamo 的 KV 选择服务已被 Ray 的 KV-aware 路由复用（协作），
  但两套控制面/扩缩容仍然重叠，谁说了算尚无定论。**

**下一步**：拿一个真实负载，按 §37.14 的顺序**单变量**走一遍。
遇到"为什么慢"，先回 §37.14 的症状表定位到**层**，
再去调那个层的参数 —— 第 31 章 §31.11 那句话在这里同样成立：
**找错层，就会白花几小时。**

---

## 37.18 本章之后

> **全书的结语不在这里。** 第七轮新增了第 38 章（Ray 与 Agent 工作负载），
> **第八轮又新增了第 39 章（Ray 源码阅读与事实核查指南）**，它现在排在最后 ——
> 全书的结语也随之移到了
> **[第 39 章的「结语：这本书真的讲完了」](ray教程-39-Ray源码阅读与事实核查指南.md)**。
>
> ⚠️ **这件事本身值得留意**：本书到这一轮为止，已经**第四次**发生
> "收尾点后移"了 —— 第四轮加 34–36、第六轮加 37、第七轮加 38、第八轮加 39。
> 每次都有几处"**离全书结尾还有 N 章**"的计数过期，
> 而且每次都是**加章节的那一轮自己**把它带进来的。
> 这和分布式系统里的 bug 是同一种形状：**你没有改任何"错"的东西，
> 只是改了一个东西，它旁边的约束就全都不成立了**（第 10 章 §10.4 讲 lineage 时也是这个道理）。
>
> 💡 **第八轮把这件事自动化了一部分**：`check_docs.py` 现在会核对
> "**往后/后面还有 N 章**"这类自述计数与实际的章号差，
> 以及"**结语在哪一章**"的指向 —— 让下一轮加章节时，
> 这类过期**由脚本拦下，而不是靠人记得**（见第 39 章 §39.8 的清单思路）。
>
> 这一章自己该带走的一句话，留在 §37.17 的小结里：
> **编排层在分裂、引擎层在收敛；而"该拧哪个旋钮"取决于你的瓶颈在哪一层。**

如果你是从这一章直接跳到文末的，往下读的顺序建议是：
第 38 章（Agent 这一层负载）→ 第 39 章（核查方法）→ 结语。
