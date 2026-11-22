# 第 41 章 评测、可复现性与实验管理

## 41.1 为什么 loss 不是能力

训练 loss 只衡量“在训练分布上预测下一个 token 的好坏”。它不能直接回答：

- 模型会不会做数学/写代码？
- 长上下文里能否找到关键信息？
- 多语言/领域能力如何？
- 是否安全/合规？
- 是否被 benchmark 污染？

一个 loss 更低但评测更差的模型完全可能出现，原因包括：

- 数据分布不同（loss 对简单 token 过拟合）；
- 评测污染；
- 解码/提示差异；
- 灾难性遗忘；
- 对齐税。

因此，训练系统必须同时生产“模型”和“可比较的评测证据”。

## 41.2 评测的分类

| 类型 | 例子 | 关注点 |
|---|---|---|
| 语言建模 | perplexity、bits-per-byte | 分布拟合 |
| 知识/推理 | MMLU、GPQA、BBH | 多任务准确率 |
| 数学 | GSM8K、MATH | 推理链/答案匹配 |
| 代码 | HumanEval、MBPP、SWE-bench | 单元测试通过率 |
| 长上下文 | needle-in-haystack、长文 QA | 位置/长度鲁棒性 |
| 多语言 | MMMLU、FLORES | 语言覆盖 |
| 指令遵循 | IFEval、MT-Bench | 格式/约束 |
| 安全 | 毒性、越狱、PII | 风险 |
| 业务 | 客服/法律/医疗 | 领域指标 |

没有“万能 benchmark”。评测集要覆盖你的目标场景，并明确权重。

### 41.2.1 2025–2026 的评测变化：agent、长上下文与 LLM-as-judge

静态单选/短答案 benchmark（MMLU/GSM8K/HumanEval）仍然有用，但越来越难区分
强模型。生产评测需要补三类：

| 类型 | 例子 | 关键点 |
|---|---|---|
| 长上下文 | RULER、长文 QA、代码库问答 | 位置/长度分层报告，不只看单点 |
| Agent/工具 | SWE-bench、WebArena、τ-bench、终端/浏览器任务 | 环境可复现、工具版本、超时/重试 |
| 开放式质量 | LLM-as-judge、成对偏好、人评 | judge 偏差、位置偏差、评分标准 |

**LLM-as-judge 的陷阱**：

- 位置偏差：A/B 顺序会改变偏好；要交换顺序重复评；
- 长度/格式偏差：更长的回答得分更高；
- 自我偏好：同家族模型互评偏高；
- 评分标准漂移：prompt/模型版本变化会让历史分数不可比；
- 成本：一次评测可能比训练一个 step 还贵。

**Agent 评测的陷阱**：

- 环境不一致（依赖/网络/工具版本）会让分数不可复现；
- 重试/超时策略不同，pass@1 差异很大；
- 任务泄漏：训练数据包含仓库/issue 的解法；
- 只看最终答案，不看中间工具调用是否合理。

**在线 A/B** 是最终裁判，但需要：流量分配、指标定义（成功率/延迟/成本）、
护栏（安全/回归）、统计功效。离线分数提升不等于线上业务提升；上线前至少
做一次小流量灰度，并监控“分数涨但用户不买账”的反例。

评测成本要纳入预算：一个 10K 样本的 judge 评测，若每次 2K tokens、
judge 模型 $/M token 为 X，成本可以按 token 直接算；agent 评测还要加环境
启动/工具调用的 CPU/GPU 成本。不要把它当成“跑个脚本”。

## 41.3 评测协议的可比性

同一个 benchmark，不同协议可能差很多分：

- few-shot 数量/示例选择；
- prompt template；
- 是否 chain-of-thought；
- 解码参数（greedy/温度/top-p/max tokens）；
- 答案抽取/归一化；
- 是否用工具/代码执行；
- 模型版本/tokenizer；
- 评测脚本版本。

报告时必须写清楚：

```text
benchmark, split, few-shot, prompt template, decoding, max tokens,
answer extraction, model/tokenizer version, evaluation code commit.
```

否则“MMLU 80 分”无法比较。

### 41.3.1 最小 harness：把协议固化成配置

一个可复现的评测不应该靠手写脚本。最小做法是把 harness 配置、模型端点和
输出归档固定下来：

```bash
# 1) Hugging Face 权重：本地 harness
lm_eval --model hf \
  --model_args pretrained=/path/to/ckpt,dtype=bfloat16 \
  --tasks mmlu,hellaswag,arc_easy \
  --num_fewshot 5 --batch_size auto \
  --output_path eval/run-2026-11-22/hf.json

# 2) vLLM / OpenAI-compatible 端点：同一套 task
lm_eval --model local-completions \
  --model_args base_url=http://127.0.0.1:8000/v1/completions,tokenizer=/path/to/tokenizer \
  --tasks mmlu,hellaswag --num_fewshot 5 \
  --output_path eval/run-2026-11-22/vllm.json
```

参数名（`local-completions`、`tokenizer`、`model_args` 的写法）随 harness
版本变化，以当前 `lm_eval --help` 为准；不变的是“固定 task/few-shot/prompt/
decoding，并把逐题输出与配置一起归档”。

每个 run 目录至少保存：

```text
config.json          # task、few-shot、prompt template、decoding、batch
model.json           # checkpoint hash、tokenizer hash、并行配置、精度
samples.jsonl        # 逐题输入/输出/抽取结果（不是只存总分）
scores.json          # 聚合分数 + 标准误/置信区间
env.txt              # harness 版本、代码 commit、CUDA/驱动
```

三个容易忽略的点：

- **prompt template 是代码**：改一个空格/角色名就可能改分数，必须 hash 并
  进版本控制；
- **答案抽取是评测的一部分**：数学/代码/多选要用对应的抽取器，不能拿
  `exact_match` 一把梭；
- **LLM-as-judge 也是模型**：记录 judge 模型版本、prompt、温度和解码参数；
  关键结论要用人工标注校准，并报告 judge 与人工的一致率。

评测进入 CI 的方式：小规模 smoke（每 task 几十题）每次提交跑，全量评测
 nightly/发版前跑；用固定 batch 的 logits 和分数双重回归，分数变化超过
置信区间才告警。

## 41.4 污染与去污染

污染来源：

- 训练数据包含 benchmark 原题/答案；
- 训练数据包含 benchmark 的衍生/翻译；
- 评测集与训练集同源；
- 代码 benchmark 的函数/测试出现在训练集。

检测：

- n-gram 重叠（训练集 vs 测试集）；
- 嵌入相似度；
- 代码 AST/函数名匹配；
- 人工抽检。

处理：

- 从训练集移除污染样本；
- 保留污染率报告；
- 使用 held-out/私有评测集；
- 对污染严重的 benchmark 降低权重。

污染不可能完全消除，但“不检测/不披露”是不可接受的。

## 41.5 统计显著性

小评测集上的 1–2 分差异可能是噪声。至少报告：

- 样本数；
- 置信区间/标准误；
- 多次采样（温度>0 时）；
- 配对检验（同题比较）。

一个粗略公式：准确率 `p`、样本数 `n` 的标准误：

```text
SE ≈ sqrt(p(1-p)/n)
95% CI ≈ p ± 1.96 SE
```

例如 `p=0.8, n=1000`，`SE≈0.0126`，CI 约 ±2.5 分。不要用 100 道题得出“提升 3 分”的结论。

### 41.5.1 Bootstrap 与配对检验

**Bootstrap**：对评测样本有放回重采样，重复 B 次（如 1000），得到指标的
经验分布：

```python
import torch

# 每个样本 0/1；n 是评测集大小
accuracy = torch.tensor([1, 0, 1, 1, 0, 1], dtype=torch.float32)
n, B = accuracy.numel(), 1000
idx = torch.randint(0, n, (B, n))
scores = accuracy[idx].mean(dim=1)
ci_low, ci_high = torch.quantile(scores, torch.tensor([0.025, 0.975]))
```

优点：不依赖正态假设，适合准确率/F1/pass@k 等非线性指标；缺点：计算量随 B
上升，且不能修复评测集本身的偏差。

**配对检验**：同题比较两个模型，只看“一个对一个错”的样本：

```text
b = A 对 B 错的数量
c = A 错 B 对的数量
在 H0（无差异）下，b ~ Binomial(b+c, 0.5)
```

这比独立样本检验更敏感，因为去掉了题目难度差异。报告时同时给效应量
（差值）和置信区间，不要只给 p 值。

### 41.5.2 多重比较

如果你比较了 20 个 benchmark、10 个模型、5 个超参，总会有一个“显著提升”。
控制方法：

- 预注册主要指标（primary endpoint），其余作为探索性分析；
- Bonferroni/Holm 校正（保守）；
- Benjamini–Hochberg FDR（控制错误发现率）；
- 报告所有结果，不只报显著的。

一个实用原则：**先定指标，再看结果**；否则统计会变成“找故事”的工具。

## 41.6 Scaling law：训练前的预算工具

### 41.6.1 计算最优

Chinchilla 风格的经验关系：

```text
L(N, D) ≈ A/N^α + B/D^β + C
```

在固定计算预算 `C ≈ 6ND` 下，最优 `N/D` 约为 20 tokens/参数（不同数据/架构会变）。

实践含义：

- 小模型 + 多 token 可能比大模型 + 少 token 更划算；
- 数据质量/重复会改变指数；
- 长上下文/ MoE 的 scaling 不同；
- 不能把 Chinchilla 当成绝对定律。

### 41.6.2 训练中预测

用小模型/短训练拟合 loss 曲线，预测大模型是否值得跑：

- 拟合 `L(step)` 的幂律/饱和曲线；
- 观察不同模型规模的平行曲线；
- 用早期 loss 外推最终 loss；
- 对数据/架构变更重新拟合。

这能避免“花几百万美元跑完才发现不如小模型”的情况。

### 41.6.3 拟合协议：从几条曲线到预算决策

一个可执行的 scaling law 拟合流程：

```text
1. 选 5–8 个规模点（例如 70M/160M/410M/1B/2.8B/6.9B）；
2. 每个规模跑至少 3 个 token 预算（如 20×/100×/300× 参数）；
3. 固定数据混合、tokenizer、架构、优化器；只变 N 和 D；
4. 用 held-out loss（不是训练 loss）拟合；
5. 用留出的最大规模验证外推误差；
6. 报告参数置信区间，不只报点估计。
```

拟合时要注意：

- **训练 loss 会被正则/数据顺序影响**；用固定验证集；
- **学习率要按规模调**（否则拟合的是“LR 没调好”的曲线）；μP 可以减少
  每个规模都扫 LR 的成本；
- **数据重复**会让 `D` 的有效值小于名义值；去重后再算；
- **MoE** 的 `N` 是总参数还是激活参数要明确；FLOPs 用激活参数，显存用总参数；
- **长上下文**的 loss 与序列长度强相关，不能把 4K 的拟合直接外推到 128K。

一个简化的决策例子：

```text
预算 C = 1e22 FLOPs
候选 A：N=7B, D=240B（约 34 tokens/param）
候选 B：N=13B, D=128B（约 10 tokens/param）
训练 FLOPs：A = 6·7·240 ≈ 1.008e22，B = 6·13·128 ≈ 0.998e22（A 约高 1%）
拟合预测：A 的 loss 比 B 低 0.03
推理成本：A 的权重/KV 都更小，per-token 成本更低
决策：推理占比高选 A；只有“训练预算更紧、且能接受略高 loss”才选 B
```

这里最容易犯的错是只看参数量就断定“7B 训练更便宜”：在固定 FLOPs 预算下，
小模型要多看 token，7B×240B 与 13B×128B 的训练量几乎相同；而推理侧
7B 的权重和 KV cache 都更小，所以 A 在“训练一次、推理很多次”的场景更优。

scaling law 的常见误用：

| 误用 | 问题 |
|---|---|
| 只用 2 个规模点拟合 3 个参数 | 欠定，外推不可信 |
| 用训练 loss 而非验证 loss | 低估过拟合/数据重复 |
| 忽略数据质量变化 | 指数/常数漂移 |
| 把 Chinchilla 的 20× 当定律 | 架构/数据/推理预算都会改变最优 |
| 不报置信区间 | 把噪声当趋势 |

结论：scaling law 是**预算工具**，不是“预测未来”的水晶球。它最适合回答
“在固定算力下，应该把参数和 token 怎么分配”，而不是“这个模型一定多强”。

## 41.7 实验管理

一个可复现实验至少记录：

```text
代码 commit / 容器镜像
模型配置（结构/并行/精度）
训练配置（batch/lr/scheduler/seed）
数据版本/hash/混合权重
tokenizer 版本
硬件/拓扑/驱动/CUDA/NCCL/TE 版本
checkpoint 路径/hash
评测配置/结果
```

推荐做法：

- 配置用 JSON/YAML 版本化，不用“命令行手改”；
- 每次实验有唯一 ID；
- 日志结构化（JSONL/数据库）；
- checkpoint 带 metadata；
- 评测脚本与训练代码同仓；
- 结果看板按实验 ID 聚合；
- 重要实验保留完整环境镜像。

### 41.7.1 工具接线：TensorBoard、W&B、MLflow

工具不是重点，**链路的唯一键**才是：`run_id ↔ config_hash ↔ data_hash ↔
checkpoint_hash ↔ eval_result`。任何一环断了，看板就只是“漂亮的曲线”。

| 工具 | 强项 | 弱项 | 适用 |
|---|---|---|---|
| TensorBoard | 本地、零依赖、标量/直方图/图 | 无实验血缘/权限/远端协作 | 单机/小团队 |
| W&B | run config、artifact、对比、报告 | 托管依赖、成本、数据出境 | 研究/多团队 |
| MLflow | 自托管、模型 registry、artifact store | 运维重、UI/协作较弱 | 企业/合规 |
| 自建（JSONL + 数仓） | 完全可控、可接内部权限 | 要自己写聚合/看板 | 大规模平台 |

一个最小接线（伪代码，API 随 SDK 版本）：

```python
run = tracker.init(
    project="llm-pretrain",
    config=cfg.to_dict(),                 # 结构/并行/精度/数据 hash 全在里面
    tags=[git_sha, image_digest, cluster],
)
for step, metrics in train_loop():
    run.log(metrics, step=step)           # loss/lr/grad_norm/mfu/...
run.log_artifact(ckpt_path, metadata={"step": step, "config_hash": cfg.hash()})
run.finish()
```

工程细节：

- **只记可行动的指标**：每 step 的 loss/grad_norm/lr/tokens/s/MFU/显存，
  每 N step 的评测；不要把每个参数的直方图都上传，成本和噪声都高；
- **配置先序列化再启动**：命令行覆盖后的最终配置才是实验事实，记录“用户
  输入”不够；
- **离线/气隙环境**：W&B 有 offline 模式；TensorBoard 可写共享存储；敏感
  数据不要进第三方托管；
- **权限与保留**：谁能删 run/artifact、保留多久、checkpoint 是否含数据/
  PII，都要在接入前定好；
- **失败也要记录**：OOM/NaN 的 run 同样是证据，不要只保留成功曲线。

验收：给定一个看板上的 run，能在 5 分钟内回答“用了哪个 commit/镜像/数据
hash/并行配置/评测脚本”，并从 checkpoint 复现同一评测结果。

## 41.8 可复现性的层次

| 层次 | 含义 | 成本 |
|---|---|---|
| 结果可复现 | 同配置多次训练，指标在容差内 | 低 |
| 统计可复现 | 固定 seed/数据，loss 曲线接近 | 中 |
| 逐位可复现 | bitwise 相同 | 高 |
| 跨硬件可复现 | 不同 GPU/拓扑 bitwise 相同 | 几乎不可能 |

生产通常追求“统计可复现”；科研/合规可能需要“逐位可复现”。

逐位可复现需要：

- 固定 reduction 顺序（集合通信/kernel）；
- 关闭非确定性算法；
- 固定数据顺序/RNG；
- 相同并行度/拓扑；
- 相同软件/驱动版本。

代价是性能下降，且跨硬件几乎不可行。

### 41.8.1 一个最小复现包

把一次训练打包成别人能重跑的最小集合：

```text
repro/
  config.json              # 完整配置，含并行度/精度/seed
  env.lock                 # pip freeze / conda list / 镜像 digest
  data_manifest.json       # shard 列表/hash/tokenizer 版本
  commit.txt               # 代码 commit
  command.sh               # 启动命令（含 torchrun 参数）
  fingerprints.json        # 关键 tensor 的 hash/统计（可选）
  eval/                    # 评测脚本与结果
  README.md                # 已知差异/预期误差
```

`fingerprints.json` 是可选但很有用的技巧：记录第一个 step 的 loss、几个参数
的 hash、attention logits 的统计量。跨硬件不可能逐位一致，但可以快速判断
“是不是同一份模型/数据”。

RNG/确定性的细节见附录 G。

### 41.8.2 确定性 recipe（摘要）

如果目标是“同机同配置逐位复现”，至少：

```python
import torch
torch.use_deterministic_algorithms(True)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True
```

```bash
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export NCCL_ALGO=Ring
export NCCL_PROTO=Simple
```

并固定：

```text
seed / RNG 流（TP 一致、DP 不同）
数据 sampler 的 epoch/offset
并行度/拓扑/消息大小
checkpoint 的 RNG/sampler/optimizer 状态
kernel 的 deterministic 模式（如 FlashAttention）
```

代价是性能下降，且跨硬件仍不保证逐位一致。完整的 RNG 语义、激活重算与
排障顺序见附录 G。

## 41.9 消融实验方法论

一个可靠的消融：

1. 固定其他变量（数据/超参/并行/评测）；
2. 只改一个因素；
3. 跑多个 seed；
4. 报告均值/方差；
5. 用相同评测协议；
6. 记录计算预算（避免“多训了 2 倍 token”）。

常见错误：

- 同时改数据+模型+超参，无法归因；
- 只跑一个 seed，把噪声当提升；
- 用不同评测脚本/解码参数；
- 忽略训练成本差异；
- 在小规模上有效，直接外推大规模。

## 41.10 mini 的验证哲学

mini 的验证不是“跑通”，而是：

- 与单卡参考实现对比 loss/梯度；
- 覆盖 TP/PP/CP/DP/MoE 组合；
- 检查 GPipe/1F1B 一致；
- 检查 checkpoint 恢复；
- 检查生成合法；
- 记录数值误差。

这套方法可以迁移到生产：

```text
参考实现 → 固定权重/数据 → 并行实现 → loss/梯度/数值对比 → 回归测试
```

它不能证明生产 kernel 的正确性，但能抓住大多数并行语义错误。

## 41.11 本章小结

- loss 不是能力；评测要覆盖目标场景并披露协议。
- 污染检测/统计显著性/held-out 集是评测可信度的基础。
- Scaling law 是预算/决策工具，不是绝对定律。
- 实验管理要记录代码/配置/数据/硬件/评测，缺一不可。
- 可复现性分层；统计可复现通常是生产目标，逐位复现代价高且跨硬件难。
- 统计显著性要配 bootstrap/配对检验/多重比较校正；先定指标再看结果。
- 消融要单变量、多 seed、同评测、记预算。
- mini 的等价性测试是生产验证方法的缩小版。

下一章用真实故障案例把前面的原理串起来。
