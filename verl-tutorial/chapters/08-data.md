# 第 8 章 数据准备：parquet、RLHFDataset 与样本契约

## 8.1 一条 RL 样本到底包含什么

监督学习的一条样本是 `(input, label)`。RL 后训练的一条样本至少包含四层信息：

1. **prompt**：给模型的输入，可能是纯文本，也可能是 chat messages；
2. **验证信息**：标准答案、测试用例、工具环境配置；
3. **来源信息**：`data_source`，用来选择奖励函数；
4. **采样元信息**：唯一 id、难度、能力标签、多轮工具参数。

verl 把这些字段放在 parquet 的列里。一个 GSM8K 样本的典型 JSON 形态：

```json
{
  "data_source": "openai/gsm8k",
  "prompt": [
    {"role": "user", "content": "Natalia sold clips to 48 of her friends... Let's think step by step and output the final answer after \"####\""}
  ],
  "ability": "math",
  "reward_model": {
    "style": "rule",
    "ground_truth": "72"
  },
  "extra_info": {
    "index": 0,
    "split": "train",
    "answer": "Natalia sold 48/2 = 24 clips in May... #### 72",
    "question": "Natalia sold clips to 48 of her friends..."
  }
}
```

不同任务的最小字段不同：

| 任务 | 必需字段 | 奖励 |
|---|---|---|
| 数学 | `prompt`、`reward_model.ground_truth` | 规则抽取 + 精确匹配 |
| 代码 | `prompt`、测试用例/沙箱配置 | 单元测试通过率 |
| 工具调用 | `prompt`、`extra_info.tools_kwargs` | 任务完成/工具返回校验 |
| 偏好学习 | `prompt`、`chosen`、`rejected` | DPO/ORPO loss |
| 多模态 | `prompt` 内含 image/video/audio | 视任务而定 |

## 8.2 v0.9.1 的数据流变化

旧版 verl 在 `RLHFDataset.__getitem__` 里完成 **prompt** 的 tokenization，返回 `input_ids`、`attention_mask`、`position_ids`。v0.9.1 把这一步整体移到了 AgentLoop：`__getitem__` **只返回 `raw_prompt`**，不再产出 `input_ids`：

```python
def __getitem__(self, item):
    """For rollout, apply_chat_template has been moved to AgentLoop,
    so we only return raw_prompt here."""
    row_dict = self.dataframe[item]
    row_dict["raw_prompt"] = self._build_messages(row_dict, key=self.prompt_key)
    row_dict["dummy_tensor"] = torch.tensor([0], dtype=torch.uint8)  # 让 DataProto.batch 非空
    row_dict["index"] = index
    row_dict["tools_kwargs"] = tools_kwargs
    row_dict["interaction_kwargs"] = interaction_kwargs
    return row_dict
```

这不是“只有多轮/raw-chat 路径才延后”，而是**所有路径**都由 AgentLoop 做 chat template 与 tokenization：单轮走 `SingleTurnAgentLoop`，多轮/工具走 `ToolAgentLoop`，V1 由 `AgentLoopManagerTQ` 统一调度。dataset 阶段唯一会 tokenize 的地方是 `maybe_filter_out_long_prompts`（为了过滤超长 prompt），它不是训练输入。

原因有三点：

1. **多轮对话需要 chat template 与工具调用交错**，不能在数据集阶段一次性 tokenize；
2. **多模态输入需要 processor**，不同模型的图像/视频处理不同；
3. **AgentLoop 可能多次调用模型**，tokenization 必须在轨迹生成时发生。

因此 v0.9.1 的 `DataProto.batch` 里可能只有一个 `dummy_tensor`，真正的 token 在 AgentLoop 输出里。读代码时不要假设 `data.batch["input_ids"]` 一定存在；也不要再按旧版“dataset tokenize → collate padding”的调用链去排查 mask 错位。

## 8.3 parquet 的组织方式

verl 通常把一个或多个原始数据源预处理成 parquet：可以是一个大文件，也可以按任务/来源拆成多个文件。每行是一个样本，列是上面的字段；`data.train_files` 与 `data.val_files` 接受单个文件、文件列表或 glob，list 里的所有行会被合并成一个逻辑数据集。

为什么用 parquet：

- 列式存储，按需读取；
- 支持嵌套结构（prompt 的 list of dict、reward_model 的 dict）；
- 比 JSONL 快，尤其是大文件；
- 与 HuggingFace `datasets` 兼容。

预处理脚本在 `examples/data_preprocess/`：

- `gsm8k.py`
- `geo3k.py`
- `full_hh_rlhf.py`
- `aime2024_multiturn_w_tool.py`
- `dapo_multiturn_w_tool.py`

它们通常做三件事：下载原始数据、构造 chat prompt、写入 `reward_model.ground_truth` 与 `extra_info`。

## 8.4 RLHFDataset 的关键参数

`verl/utils/dataset/rl_dataset.py` 的 `RLHFDataset.__init__` 读取：

| 参数 | 作用 |
|---|---|
| `prompt_key` | prompt 列名，默认 `prompt` |
| `max_prompt_length` | prompt 截断长度 |
| `truncation` | `error`/`left`/`right` |
| `return_raw_chat` | 是否返回原始 chat |
| `filter_overlong_prompts` | 是否过滤超长 prompt |
| `image_key` / `video_key` / `audio_key` | 多模态列 |
| `custom_cls` | 自定义数据集类 |
| `apply_chat_template_kwargs` | chat template 参数 |

### 截断策略

- `error`：超长直接报错，迫使你显式处理；
- `left`：从左边截断，保留最近的上下文；
- `right`：从右边截断。

对因果 LM，prompt 通常左截断（保留结尾指令），response 右截断（保留开头推理）。但数学/代码任务有时需要保留开头的问题描述，不能一概而论。

### 超长过滤

`filter_overlong_prompts=true` 会在加载时丢掉超过 `max_prompt_length` 的样本，并打印比例。对长文档任务，这个比例可能很高；要先统计再决定是否过滤。

## 8.5 collate：从样本到 batch

v0.9.1 的 `RLHFDataset.__getitem__` 只返回 `raw_prompt` 与元数据；tokenization 发生在 AgentLoop。`verl/utils/dataset/rl_dataset.py::collate_fn` 把一组 dict（`raw_prompt`、`extra_info`、`dummy_tensor` 等）合成 batch，不做 tokenize：

- `torch.Tensor` 字段 `torch.stack`；
- 非 tensor 字段转成 `np.ndarray(dtype=object)`；
- 返回一个 dict，后续被包进 `DataProto`。

MiniVerl 的 `collate_fn` 做了同样的事，但直接构造 `DataProto`：

```python
input_ids      # (B, max_prompt + max_response)
attention_mask # (B, T)
response_mask  # (B, T)，response 位置为 1
prompt_length  # (B,)
prompts        # object array
answers        # object array
data_source    # object array
uid            # 用于 GRPO 分组
```

### 一个常见错误：response_mask 的起点

如果 prompt 被左截断，`response_mask` 的起点是**截断后的 prompt 长度**，不是原始长度。如果 tokenizer 在 prompt 末尾加了 BOS/EOS，也要算进去。很多“奖励不升”的问题，最后都定位到 mask 错位。

MiniVerl 的测试 `test_collate_shapes_and_masks` 固定了：

```python
assert np.all(batch.batch["response_mask"].sum(axis=-1) == 1)
```

因为 bandit 任务的答案只有一个 token。换成多 token 答案时，这个断言要相应调整。

## 8.6 多轮数据与工具参数

多轮样本的 prompt 是 messages 列表：

```json
[
  {"role": "system", "content": "You are a helpful assistant."},
  {"role": "user", "content": "What is 3+4? Use the calculator tool."}
]
```

`extra_info.tools_kwargs` 可以携带：

- 工具名与参数 schema；
- 沙箱地址；
- 最大轮数；
- 每个任务的私有配置。

v0.9.1 的 `AgentLoop` 会读取 `raw_prompt` 与 `tools_kwargs`，调用 chat template，生成工具调用，执行工具，再把 observation 拼回对话。第 26 章详细讲。

## 8.7 多模态数据

多模态样本在 message content 里放 image/video/audio：

```json
{
  "role": "user",
  "content": [
    {"type": "image", "image": "..."},
    {"type": "text", "text": "Describe this image."}
  ]
}
```

`RLHFDataset.process_vision_info` 负责抽取图像/视频，`processor.apply_chat_template` 生成模型输入。v0.9.1 为视频解码引入了 `torchcodec`，因为 torchvision 0.26 移除了 `read_video`。

多模态 RL 的额外难点：

- 图像 token 与文本 token 的 mask 不同；
- rollout 引擎需要支持多模态输入；
- reward 可能依赖图像内容；
- 序列长度与显存显著增加。

第 29 章展开。

## 8.8 数据质量与课程学习

RL 对数据分布比 SFT 更敏感：

- **难度**：太简单的样本组内全对，优势为 0；太难的组内全错，也没有信号。DAPO 的动态采样就是为此。
- **多样性**：同一 prompt 的多个采样要有差异；温度太低会退化。
- **可验证性**：答案必须能被程序判定；模糊的“好答案”需要 reward model。
- **泄漏**：验证集不能与训练集重叠；数学题要注意同一题的不同表述。

课程学习（curriculum）在 verl 中通常通过数据采样器或自定义 `ReplayBuffer` 实现，而不是一个单独的配置项。

## 8.9 MiniVerl 的数据层

MiniVerl 用合成数据替代 parquet，保留了同样的字段：

```python
@dataclass
class Sample:
    prompt: str
    answer: str
    data_source: str
    uid: int
    index: int
```

四种数据集：

- `bandit`：`=A` → `x`，四动作 contextual bandit；
- `copy`：`ax=` → `ax`；
- `arithmetic`：`3+4=` → `7`；
- `math`：模板化应用题，答案 `\boxed{...}`。

`build_dataset(kind, size, seed)` 生成确定性样本；`collate_fn` 构造 `DataProto`；`make_batches` 负责 shuffle 与 drop_last。

MiniVerl 的 bandit 任务故意使用**受限解码**（`rollout.allowed_chars="xyzw"`）和线性策略，使 CPU 上的 PPO/GRPO 在几十步内可学到 100%。这不是为了“作弊”，而是为了把系统链路与算法链路的正确性分开验证：如果连四动作 bandit 都学不会，问题一定在实现而不是模型容量。

## 8.10 数据检查清单

在启动大规模训练前，至少检查：

1. `len(dataset)` 与 `train_batch_size` 是否匹配；
2. 每个 batch 的 `response_mask.sum()` 是否合理；
3. prompt/response 的截断比例；
4. reward 函数在 100 条样本上的分布（全 0？全 1？双峰？）；
5. `data_source` 是否覆盖所有奖励分支；
6. 多轮样本的 `tools_kwargs` 是否完整；
7. 验证集是否与训练集去重；
8. tokenizer 版本与模型版本是否一致。

## 8.11 数据引擎：从“一个 parquet”到持续生产线

真实项目很少只准备一次数据。更常见的是“生成 → 过滤 → 训练 → 评测 → 再生成”的飞轮。把这条链做成可复现的管线，比手工洗数据更重要。

### 四个阶段

```text
采集/合成 ──► 过滤/去重 ──► 难度分层 ──► 版本化 parquet
     ▲                                        │
     └────────── 评测/失败案例分析 ◄──────────┘
```

1. **采集/合成**：人工标注、强模型生成、拒绝采样（只保留验证器通过的轨迹）、环境回放。合成数据要记录生成模型、prompt 模板、采样参数。
2. **过滤/去重**：精确哈希 + 近似去重；去掉空答案、解析失败、超长、奖励全 0/全 1 的样本。
3. **难度分层**：用当前基线模型对每条样本采样 $n$ 次，按通过率分成“全对/全错/有区分度”。GRPO 只从有区分度的组里获得梯度。
4. **版本化**：每个 parquet 带 manifest（来源、过滤规则、哈希、日期、生成脚本）；训练配置引用数据版本，而不是路径。

### 拒绝采样（rejection sampling）

推理模型训练里最常见的合成方式：

```text
for prompt in seeds:
    samples = policy.sample(prompt, n=K)
    keep = [s for s in samples if verifier(s) == correct]
    write(prompt, keep)
```

它把“模型偶尔做对”的轨迹变成 SFT 数据。注意：

- **只保留正确轨迹会丢失负样本**；如果后续要做偏好学习，需要同时保留 chosen/rejected；
- **验证器必须与训练奖励一致但不共享 bug**；至少用独立 parser 复核；
- **多样性会塌缩**：只保留正确解，模型会越来越像自己的成功模式；可以按解的长度/风格分层保留。

### 去重与污染

- **精确去重**：对规范化后的 prompt+answer 做哈希。
- **近似去重**：MinHash/SimHash，处理同一题的不同表述。
- **污染检查**：训练集与验证集/公开 benchmark 的 n-gram 重叠；时间切分；canary 字符串。
- **测试泄漏**：代码任务尤其危险——单元测试可能出现在 prompt 或仓库历史里。

污染不是“查一次就完事”。每次新增数据都要重跑检查，并把报告存档。

### 难度分层与课程

一个实用的分层策略：

| 分组 | 基线通过率 | 用途 |
|---|---|---|
| 全对 | 1.0 | 少量保留，防止遗忘/稳定格式 |
| 有区分度 | 0.2～0.8 | 主训练集 |
| 全错 | 0.0 | 少量保留，配合更强 teacher/蒸馏；GRPO 无梯度 |

课程学习不是必须，但“先易后难”通常能让早期训练更稳。实现上可以通过数据采样器/`ReplayBuffer` 控制每组的采样比例，而不是改 loss。

### 数据 manifest

```yaml
dataset: math-rlvr-v3
created_at: 2026-11-01
sources:
  - name: gsm8k
    split: train
    license: MIT
  - name: synthetic-qwen3-32b
    generator: Qwen3-32B
    prompt_template: math_cot_v2
filters:
  - dedup: minhash-0.8
  - drop_empty_answer: true
  - max_prompt_tokens: 1024
  - drop_groups: all_correct_or_all_wrong
counts:
  total: 120000
  train: 110000
  val: 5000
  test: 5000
hashes:
  train: <sha256>
  val: <sha256>
```

训练脚本只认 `dataset` 版本；评测脚本只认 `test` 版本。这样“换数据”是一次显式的版本升级，而不是悄悄覆盖文件。

## 本章小结

- v0.9.1 的 dataset 只返回 `raw_prompt`；所有路径的 chat template/tokenization 都在 AgentLoop（单轮、多轮、V1 manager）完成，collate 只做 padding/stack。
- parquet 的字段约定是 prompt、data_source、reward_model、extra_info。
- response_mask 的起点、截断策略、超长过滤是三个高频错误源。
- 多轮与多模态数据需要额外的工具参数与 processor。
- MiniVerl 用合成数据保留同样的字段与 mask 契约。

## 延伸阅读

- 数据准备文档：<https://verl.readthedocs.io/en/latest/preparation/prepare_data.html>
- `verl/utils/dataset/README.md`
- `examples/data_preprocess/`
- 下一章：DataProto 与 TensorDict。
