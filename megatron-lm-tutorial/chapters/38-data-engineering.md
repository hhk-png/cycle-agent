# 第 38 章 数据工程：从原始文本到可训练 token

## 38.1 数据决定了模型能力的上限

并行和 kernel 决定“训练多快”，数据决定“能训出什么”。一个常见的误区是：把数据当成“tokenizer 之后的二进制文件”，只关心吞吐。真正的大规模训练里，数据工程至少包括：

1. 采集与合规；
2. 清洗、去重、质量过滤；
3. tokenizer 训练/适配；
4. 分片、索引、混合；
5. packing 与 document mask；
6. 分布式 sampler 与断点续训；
7. 数据课程与退火；
8. 评测污染检查。

这一章按数据流的顺序拆解，并给出可执行的检查清单。

## 38.2 数据来源与合规

常见来源：

| 来源 | 特点 | 风险 |
|---|---|---|
| 网页爬取 | 量大、噪声高 | 版权、隐私、低质量 |
| 代码仓库 | 结构化、长尾 | 许可证、密钥/敏感信息 |
| 书籍/论文 | 高质量、长文 | 版权 |
| 百科/问答 | 事实性强 | 偏见、过时 |
| 合成数据 | 可控、可扩 | 多样性/模型自噬 |
| 企业私有 | 领域相关 | 隔离、审计 |

合规不是“训练前删几个文件”：

- 数据许可证要可追溯（来源、许可、使用范围）；
- PII/密钥/恶意内容要检测和脱敏；
- 多租户场景要做数据隔离；
- 模型记忆/输出可能泄露训练数据；
- 出口管制/地区法规可能限制数据跨境。

工程上，建议给每个 shard 维护元数据：

```json
{
  "shard": "web-000123",
  "source": "common-crawl",
  "license": "unknown",
  "tokens": 1048576,
  "documents": 8123,
  "language": "en",
  "quality_score": 0.73,
  "dedup_cluster": 9123,
  "created_at": "2026-01-15"
}
```

## 38.3 清洗与质量过滤

清洗的目标不是“把文本变干净”，而是“提高单位 token 的信息密度”。常见步骤：

### 38.3.1 规则清洗

- 去除 HTML/模板/导航栏；
- 修复编码/乱码；
- 去除超短/超长/重复行；
- 过滤广告/垃圾/机器生成模板；
- 统一空白/标点。

### 38.3.2 质量分类器

- 语言识别（fastText/CLD）；
- 困惑度过滤（用小型 LM 打分，过高/过低都可能是噪声）；
- 分类器打分（教育价值/毒性/成人内容）；
- 启发式特征（标点比例、重复 n-gram、平均句长、停用词比例）。

### 38.3.3 去重

去重对训练效率影响巨大：重复数据会让模型记忆、浪费算力，并放大 benchmark 污染。

| 方法 | 粒度 | 优点 | 缺点 |
|---|---|---|---|
| 精确哈希 | 文档 | 快、确定 | 抓不到改写 |
| MinHash/LSH | 文档/段落 | 抓近似重复 | 参数/阈值难调 |
| SimHash | 文档 | 局部敏感 | 长文本稳定性 |
| Suffix array | 子串 | 抓长重复片段 | 计算/存储重 |
| 语义聚类 | 文档 | 抓同义改写 | 成本高、误杀 |

工程建议：

1. 先做精确去重（便宜）；
2. 再做 MinHash/LSH（覆盖大部分近似重复）；
3. 对高价值数据做子串级去重；
4. 保留去重簇 ID，便于审计和 ablation。

### 38.3.4 污染检查

Benchmark 污染是“评测分数虚高”的主要原因。至少要做：

- 训练集与评测集的 n-gram 重叠检查；
- 代码 benchmark 的函数名/测试用例匹配；
- 数学题的题干/答案匹配；
- 记录每个 benchmark 的污染率。

污染不一定能完全消除，但必须量化，并在报告中披露。

## 38.4 Tokenizer：词表就是模型的坐标系

### 38.4.1 主流算法

| 算法 | 代表 | 特点 |
|---|---|---|
| BPE | GPT/Llama | 合并高频 pair，简单高效 |
| WordPiece | BERT | 似然驱动的子词 |
| Unigram | T5/SentencePiece | 概率模型，支持采样 |
| byte-level BPE | GPT-2/3 | 无 OOV，适合多语言/代码 |

关键超参：

- 词表大小：32K（Llama 2）、50K（GPT-2）、100K+（多语言/代码）；
- 特殊 token：BOS/EOS/PAD/UNK，chat template 的角色标记；
- 数字/代码切分：影响算术/代码能力；
- 多语言覆盖：低资源语言可能被切得很碎。

### 38.4.2 词表大小与并行的关系

词表并行要求 `vocab_size % tp == 0`。生产上通常把词表 pad 到
`make_vocab_size_divisible_by × tp` 的倍数。设
`D = make_vocab_size_divisible_by`（常见 128，但**是可配置的**，以当前配置
为准）：

```python
multiple = D * tp                  # 不是只 pad 到 tp
padded_vocab = ((vocab_size + multiple - 1) // multiple) * multiple
```

为什么多乘一个 `D`：让每个 TP rank 分到的 vocab shard 仍是 `D` 的倍数，
便于 fused embedding/GEMM 对齐；只 pad 到 `tp` 虽然能整除，但 shard 大小
可能不是友好的块。`D=1` 时退化为“只满足整除”。

pad 出来的 token 不参与训练（loss mask/ignore_index），但会增加一点显存/
计算。不要为了“整除”直接改 tokenizer 词表，否则 HF ↔ Megatron 转换会错位。
本书 38.4.5 用同一个公式；如果看到只写 `% tp == 0` 的地方，那是**最低**
约束，不是推荐的 padding 粒度。

### 38.4.3 Tokenizer 与模型必须绑定

一个模型 checkpoint 必须能回答：

- tokenizer 类型/版本；
- 词表文件/hash；
- 特殊 token 的 id；
- chat template；
- 是否 byte fallback；
- 是否 pad 过词表。

转换模型时，tokenizer 不匹配是最隐蔽的错误之一：模型能跑，但生成乱码/评测崩盘。

### 38.4.4 训练一个 BPE tokenizer

BPE 的训练循环本质上是“反复合并最高频的相邻 pair”：

```text
1. 把语料切成字符/字节序列；
2. 统计相邻 pair 频次；
3. 合并频次最高的 pair，记入 merge 表；
4. 重复直到词表达到目标大小；
5. 剩下的字符用 byte fallback（避免 OOV）。
```

工程要点：

- **预分词**：先用正则/Unicode 规则切分（数字、标点、空格），再做 BPE；
  否则会把整段文本当成一个 pair。
- **byte fallback**：多语言/emoji/代码必须能把任意字节编码成 token；否则
  `<unk>` 会破坏 round-trip。
- **特殊 token**：BOS/EOS/PAD/UNK 以及 chat template 的角色标记（如
  `<|user|>`、`<|assistant|>`）要预留 id，且不能被 BPE 合并掉。
- **数字切分**：是否把数字逐位切分会影响算术能力；Llama 3 等模型专门调整了
  数字/标点的预分词规则。
- **词表大小**：不是越大越好。词表大 → embedding/输出层参数大、softmax 慢；
  词表小 → 序列变长、长上下文成本高。

一个可复现的 tokenizer 产物应包含：

```text
tokenizer.json / tokenizer.model     # 词表与 merge 规则
special_tokens_map.json              # 特殊 token
tokenizer_config.json                # 预处理/后处理/chat template
config.json                          # 模型侧 vocab_size/padded_vocab_size
```

### 38.4.5 词表 padding 与 embedding

Megatron 的 `padded_vocab_size` 通常大于 tokenizer 的 `vocab_size`：

```text
padded_vocab_size = ceil(vocab_size / (tp · 128)) · (tp · 128)
```

多 pad 出来的行：

- embedding 初始化为 0 或小随机；
- 输出层对应行在 loss 中被 mask；
- 生成时禁止采样这些 id；
- checkpoint 转换时丢弃/截断到 `vocab_size`。

如果直接把 tokenizer 词表扩到 padded size，HF 侧会多出无意义 token，转换/
评测都会错位。正确做法是**模型侧 pad，tokenizer 不动**。

### 38.4.6 验证清单

tokenizer 的 bug 往往在训练后才暴露，必须提前验证：

| 检查 | 方法 | 失败后果 |
|---|---|---|
| round-trip | `decode(encode(x)) == x` | 特殊字符/emoji 丢失 |
| 特殊 token id | 打印 BOS/EOS/PAD/角色 token | chat template 错位 |
| 压缩率 | tokens/字符 比 | 多语言/代码成本失控 |
| 词表覆盖 | 统计 `<unk>` 比例 | 低资源语言退化 |
| 训练/推理一致 | 用同一 tokenizer 文件 | 生成乱码 |
| pad 一致性 | `vocab_size` vs `padded_vocab_size` | embedding 行错位 |
| 版本/hash | 记录 tokenizer 文件 hash | 无法复现 |

一个常见事故：训练用了 A 版 tokenizer，推理用了 B 版（词表大小相同但 merge
不同）。模型能加载、能生成，但输出是“看似合理”的乱码。把 tokenizer hash 写进
checkpoint metadata 和实验记录，是最便宜的防范。

## 38.5 数据格式与分片

### 38.5.1 常见格式

| 格式 | 优点 | 缺点 |
|---|---|---|
| JSONL | 可读、易处理 | 体积大、解析慢 |
| Parquet | 列式、压缩好 | 需要库/ schema |
| WebDataset | 分片/tar、流式 | 需要解码 |
| token shard（bin/npy） | 训练读取最快 | 需要索引/预处理 |

大规模预训练通常在离线阶段把文本转成 **token shard**：

```text
uint16: vocab < 65536
uint32: 更大词表
```

每个 shard 记录：

- token 数；
- 文档边界（start/end）；
- 来源/质量/语言；
- tokenizer hash；
- 校验和。

### 38.5.2 为什么需要索引

只有 token 流不够：

- packing 需要文档边界；
- loss mask 需要知道哪些 token 是 padding/无效；
- 数据混合需要按来源采样；
- 断点续训需要定位到样本；
- 去重/污染审计需要回溯原文。

最小索引可以是：

```python
{
    "tokens": "shard_000123.bin",
    "doc_offsets": [0, 812, 1904, ...],  # 每个文档的起始 token
    "source_ids": [...],
    "lengths": [...],
}
```

## 38.6 Packing 与 document mask

### 38.6.1 不 packing 的浪费

如果每条样本都 padding 到 `max_seq_len`，短样本会浪费大量计算：

```text
真实 token：100
padding：   3900（seq_len=4000）
有效利用率：2.5%
```

### 38.6.2 Packing

把多条文档拼进一条固定长度序列：

```text
[doc A 0..99][doc B 0..199][doc C 0..49][pad ...]
```

关键：**不能让 attention 跨文档**。否则模型会把 B 的内容当成 A 的上下文，训练目标被污染。

实现方式：

1. **block-diagonal mask**：`[s, s]` 的块对角矩阵；
2. **document id + flash attention varlen**：用 cu_seqlens 表示每段长度；
3. **THD 布局**：`total_tokens × heads × dim`，配合变长 kernel。

### 38.6.3 position ids 与 loss mask

Packing 后：

- 每条文档的 position id 从 0 重新开始（或使用全局位置，取决于位置编码/任务）；
- labels 在文档边界处要处理：最后一条文档的最后一个 token 没有 next token，或者跨文档的 next token 应被 mask；
- padding token 的 loss 必须 ignore。

一个简化的 packer：

```python
def pack_documents(docs, seq_len, pad_id=0):
    input_ids, labels, doc_ids, position_ids, loss_mask = [], [], [], [], []
    for doc in docs:
        ids = doc["ids"]
        for start in range(0, len(ids) - 1, seq_len):
            chunk = ids[start:start + seq_len + 1]
            if len(chunk) < 2:
                continue
            x, y = chunk[:-1], chunk[1:]
            input_ids.append(x)
            labels.append(y)
            doc_ids.append([doc["id"]] * len(x))
            position_ids.append(list(range(len(x))))
            loss_mask.append([1] * len(x))
    # 这里省略 padding/拼接；生产实现要处理跨文档 label 边界
    return input_ids, labels, doc_ids, position_ids, loss_mask
```

真正的 packer 还要处理：

- 多文档拼接到恰好 `seq_len`；
- 跨文档的最后一个 label 置 `ignore_index`；
- 变长 batch 的负载均衡；
- CP/SP 下 mask 的全局位置。

mini 在 `mini_megatron/data/packing.py` 提供了一个可测试的 `PackedSequenceDataset`，用于观察文档边界/position reset/loss mask 的语义；它没有接入训练主循环，因为 mini 的 attention 目前只支持 causal/padding mask。

## 38.7 数据混合与课程

### 38.7.1 按 token 比例混合

不要按“文档数”混合。不同来源的平均长度差异很大：

```text
web:    1000 tokens/doc
code:   300 tokens/doc
books:  50000 tokens/doc
```

按文档数采样会让 books 的 token 占比远高于预期。正确做法：

```text
P(source) ∝ target_token_weight / tokens_per_doc(source)
```

或者直接按 token 预算采样。

### 38.7.2 温度采样

设来源权重 `w_i`，温度 `T`：

```text
p_i ∝ w_i^(1/T)
```

- `T=1`：按原始权重；
- `T>1`：更均匀（低资源来源上采样）；
- `T<1`：更集中（高资源来源主导）。

### 38.7.3 课程与退火

常见策略：

- 先通用网页/代码，后高质量/领域数据；
- 训练末期提高高质量数据比例（annealing）；
- 长上下文课程：4K → 8K → 32K → 128K；
- 学习率与数据课程同步退火。

课程不是“玄学”：它改变的是数据分布，进而改变 loss 曲面和最终能力。要做 ablation，不要凭感觉。

## 38.8 分布式 Sampler 与断点续训

Sampler 的契约：

- 每个 **数据并行副本** 看到不同样本；
- TP/PP/CP/EP 的模型并行副本看到相同样本；
- epoch 边界正确；
- 恢复时从正确位置继续（不重复/不跳过）；
- 混合权重按 token 而不是 step 计算。

一个可恢复的 sampler 至少保存：

```text
epoch
global_step
sample_index / shard_index
RNG state
blend weights version
```

如果恢复后 loss 跳变，优先检查：

1. sampler 是否从头开始；
2. 数据混合权重是否变了；
3. tokenizer/数据版本是否变了；
4. 并行度变化是否改变了每卡 token 数。

### 38.8.1 一个最小接线（生产）

把“数据集 → 混合 → sampler → 迭代器”接起来，通常只需要四类组件。不同
Megatron 版本的类名/参数会漂移，下表按**职责**给入口，代码是伪代码：

| 组件 | 职责 | 常见入口（以当前源码为准） |
|---|---|---|
| 索引数据集 | `.bin/.idx`、文档边界、mmap 读取 | `IndexedDataset` / `MMapIndexedDataset` |
| 混合数据集 | 多个来源按权重/温度采样 | `BlendedDataset` / `BlendedMegatronDatasetBuilder` |
| 采样器 | DP 分片、epoch、随机/顺序、可恢复 | `MegatronPretrainingSampler` / `MegatronPretrainingRandomSampler` |
| 迭代器 | train/valid/test 三路、drop_last、重置 | `build_train_valid_test_data_iterators` |

一个最小接线：

```python
# 伪代码：参数名/导入路径随版本变化，先用 --help 和源码核对
from megatron.core.datasets.gpt_dataset import GPTDataset, GPTDatasetConfig
from megatron.core.datasets.blended_megatron_dataset_builder import (
    BlendedMegatronDatasetBuilder,
)

config = GPTDatasetConfig(
    random_seed=seed,
    sequence_length=seq_len,
    blend=[[data_prefix], [weights]],      # 权重按 token 比例
    tokenizer=tokenizer,
    reset_position_ids=True,               # packing 时每个文档重置
    reset_attention_mask=True,             # 文档间 block-diagonal
    eod_mask_loss=True,
)
datasets = BlendedMegatronDatasetBuilder(
    GPTDataset, sizes=[n_train, n_valid, n_test], is_built_on_rank=is_built_on_rank,
    config=config,
).build()
train_iter, valid_iter, test_iter = build_train_valid_test_data_iterators(
    build_train_valid_test_datasets_provider
)
```

接线后逐项验收：

```text
[ ] 同一 step，DP rank 0/1 的 batch hash 不同；TP/PP/CP/EP 副本相同
[ ] global_batch = micro_batch × dp × grad_accum（单位是样本，不是 token）
[ ] 每步 token 数 = global_batch × seq_len；packing 时按有效 token 计 loss
[ ] blend 权重和为 1，且按 token 而不是文档数配比
[ ] vocab_size 按 make_vocab_size_divisible_by 补齐到 TP 的倍数
[ ] 保存/恢复 sampler 后，第一个 batch 不重复、不跳过
[ ] 训练/验证/测试集的 tokenizer hash 与模型配置一致
```

最常见的两个错误：

1. **把 `global_batch` 当 token 数**。Megatron 的 `--global-batch-size` 是
   样本数；`global_batch × seq_len` 才是 token 数。1M token、seq_len=4096
   对应 256 个样本，不是 1,000,000。
2. **只看 rank 0 的 batch**。DP 分片错误时，rank 0 可能完全正常，其他 rank
   重复或跳过数据；必须打印每个 rank 的 `(epoch, sample_id)` 或 batch hash。

### 38.8.2 Megatron Energon：面向样本与多模态的生产数据层

38.8.1 的 `GPTDataset` 面向**已经 tokenize 的定长 token 流**：`.bin/.idx`
里是 token id，样本按 `seq_len` 切窗或 packing。多模态、原始 web 数据、
变长对话则更适合 **Megatron Energon**（`megatron-energon`，NVIDIA 的独立
数据集库，不是 `megatron.core` 内部的一个小模块）。它和训练框架的关系是：

```text
原始样本（图/文/音/视频 + 标注）
  → WebDataset 式 tar shard（1–4 GB/片）
  → energon prepare：扫描 shard、生成 .nv-meta 元数据/样本 schema
  → 数据集定义（blend/weights/sample type）
  → get_train_dataset(...) → PyTorch DataLoader → 训练循环
```

**它解决的不是“读文件”，而是四件事：**

| 能力 | 作用 | 不用它会怎样 |
|---|---|---|
| 样本级索引 | 按 sample 而非 token 定位，变长样本友好 | 只能定长切窗，短样本浪费、长样本截断 |
| worker 内解码 | 图像/视频/音频在 DataLoader worker 里解码 | 主进程解码，GPU 等 CPU |
| 可恢复状态 | `state_dict`/`load_state_dict` 保存 shard/样本位置 | 恢复后重复或跳过数据 |
| 混合与 packing | 多来源按权重采样，样本内/跨样本 packing | 手写 sampler，断点续训难对齐 |

**最小接线（伪代码，API 随版本漂移）。** 先用 `energon prepare --help` 或
对应版本的文档确认参数名：

```python
from megatron.energon import get_train_dataset, get_val_dataset, WorkerConfig

worker = WorkerConfig(num_workers=8, ...)   # 解码/加载都在 worker 进程
train_ds = get_train_dataset(
    "/data/my_corpus",          # 含 .nv-meta 与 tar shard 的目录
    worker_config=worker,
    batch_size=micro_batch,
    shuffle_buffer_size=1000,
    max_samples_per_sequence=4,
    # 具体参数（packing、sample type、任务 encoder）按版本核对
)
loader = torch.utils.data.DataLoader(train_ds, batch_size=None, ...)
```

与 38.8.1 的 Megatron 内置数据集相比：

| 维度 | 内置 `GPTDataset` | Energon |
|---|---|---|
| 数据形态 | 已 tokenize 的 token 流 | 原始样本（文本/图像/视频/音频） |
| 索引粒度 | token/窗口 | 样本与 shard |
| 多模态 | 不负责 | 内置解码与 sample type |
| 断点续训 | sampler 状态 | 数据集 state_dict + worker 状态 |
| 适用 | 纯文本预训练 | 多模态、SFT、变长对话、web 数据 |

**五个生产坑。**

1. **元数据与 shard 不同步**。新增/替换 tar 后没有重新 `prepare`，样本数
   或 schema 会对不上；训练前校验 shard 列表、样本数、hash。
2. **worker 数与解码成本不匹配**。视频解码很重，`num_workers` 太小会让
   GPU 饿死；太大则 CPU/内存争抢。按“每 worker 样本/秒 × worker 数 ≥
   训练消耗”估算，并监控 data wait。
3. **packing 的文档边界**。Energon 可以在样本内/跨样本 packing；跨样本时
   必须重置 position ids、attention mask 和 loss mask（38.6），否则不同
   文档会互相注意。
4. **恢复语义**。保存的是“数据集迭代位置”，不是“训练 step”；改变
   shard 顺序、blend 权重或 worker 数后，旧 state 可能不可复用。恢复后要
   比对前 `N` 个 batch 的 hash。
5. **评测泄漏**。按 shard 随机划分 train/val 会让同一文档的副本跨集；
   去重必须在划分之前做（38.3.3），并保存划分清单。

**验收清单：**

```text
[ ] shard 数、样本数、总 token/图像数能从元数据复算
[ ] 同一 step，DP rank 间 batch hash 不同；TP/PP/CP/EP 副本相同
[ ] 恢复后第一个 batch 与中断前最后一个 batch 不重不漏
[ ] 图像/视频解码失败有计数与隔离，不会静默丢弃
[ ] packing 后 position ids、attention mask、loss mask 三者一致
[ ] data wait P99 低于 step time 的 10%
[ ] val/test 的 shard 与 train 无重叠（按文档/图像 hash 检查）
```

Energon 不是“更快的 DataLoader”这么简单：它把**样本 schema、解码、混合、
packing、恢复**都变成数据层契约。接进训练前，先把这些契约写成测试；否则
最常见的结果是“loss 能降，但恢复后数据悄悄错位”。

## 38.9 后训练数据

后训练的数据形态与预训练完全不同：

| 阶段 | 数据 | loss |
|---|---|---|
| SFT | prompt + response | 只算 response token |
| 偏好优化 | chosen/rejected pair | pair-wise/DPO |
| RLHF/GRPO | prompt + reward | 策略梯度/组相对 |
| 蒸馏 | teacher logits/response | KD loss |
| 工具调用 | 多轮轨迹 | 工具结果 mask |

关键工程问题：

- chat template 必须与推理时一致；
- response-only loss mask 要正确；
- 多轮对话的 role/边界；
- 长序列 packing 与 cross-sample attention；
- 偏好数据的质量/一致性；
- RL 的 rollout 与训练引擎权重同步。

这些内容在第 39 章展开。

## 38.10 数据管线的性能

数据管线常见瓶颈：

- tokenize 速度（CPU）；
- 存储 IO（对象存储/并行文件系统）；
- 解码/解压；
- Python DataLoader 的 GIL/进程开销；
- 网络（远程读取）；
- packing 的 CPU 计算。

优化手段：

- 离线 tokenize，训练时只读 token shard；
- 多进程/多线程 prefetch；
- 本地 NVMe 缓存；
- 大 shard/顺序读，避免小文件随机读；
- 用 `uint16` 而不是 `int64`；
- 预计算 packing/index；
- 数据加载与计算重叠（prefetch queue）。

一个经验：**GPU 利用率掉到 80% 以下，先看数据加载时间线**，不要先怀疑 kernel。

## 38.11 数据质量评估

训练 loss 低不等于数据好。需要：

- 领域评测（代码/数学/多语言/长文）；
- 去污染；
- 人工抽检；
- 数据 ablation（去掉某来源，看能力变化）；
- 训练中监控各来源 loss/梯度；
- 记忆/重复检测。

数据工作的 ROI 往往高于再调一轮并行度：同样的 GPU-hours，更高质量的数据能带来更大的能力提升。

## 38.12 mini 的数据实现

mini 提供：

- `SyntheticTokenDataset`：可预测的算术序列，便于观察 loss 下降；
- `BinTokenDataset`：uint16 memmap；
- `BatchProvider`：按 DP rank 采样，TP/PP 副本一致；
- `PackedSequenceDataset`（新增）：文档边界/position reset/loss mask 的教学实现。

它不处理：

- 真实 tokenizer；
- 混合/去重/质量过滤；
- 分布式文件系统；
- packing 接入训练；
- 数据恢复位置。

这些差距不影响并行正确性，但决定了它不能训练有用模型。

## 38.13 本章小结

- 数据决定模型能力上限；清洗/去重/质量/合规与并行同样重要。
- 词表要与 TP 对齐，但不要为了整除破坏 tokenizer。
- packing 必须配 document mask/position reset/loss mask。
- 混合按 token 比例，课程要 ablation，sampler 要可恢复。
- 后训练数据形态不同，chat template/loss mask 是关键。
- 数据管线吞吐经常是 GPU 利用率的第一瓶颈。

下一章进入后训练：SFT、LoRA、偏好优化与 RL。
