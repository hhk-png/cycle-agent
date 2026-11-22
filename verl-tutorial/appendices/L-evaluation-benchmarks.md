# 附录 L 评测与基准

> RL 训练的目标是“更好的模型”，但“更好”必须被定义、被测量、被复现。这一篇讲评测的指标、口径、污染控制、成本与工程化，最后给一个报告模板。

## L.1 评测是训练系统的一部分

很多团队把评测当成训练结束后的一个 notebook。规模化之后这会出问题：

- 不同实验用了不同的采样参数，指标不可比；
- 评测脚本依赖本地文件，换机器就跑不了；
- 只看一个 benchmark，模型过拟合到它的分布；
- 训练集与评测集重叠，指标虚高；
- 没有置信区间，把噪声当提升。

评测应该和训练一样：**版本化、可复现、可回归**。一个 checkpoint 的评测结果，至少要和它的训练配置、代码 commit、镜像 digest 绑定。

## L.2 基准地图

| 能力 | 基准 | 指标 | 备注 |
|---|---|---|---|
| 小学数学 | GSM8K | exact match | 经典但接近饱和 |
| 竞赛数学 | MATH-500 / AIME | exact match / pass@k | AIME 年份要写清 |
| 科学知识 | GPQA Diamond | accuracy | 高难度、易污染 |
| 代码生成 | HumanEval / MBPP | pass@1 / pass@k | 函数级，较饱和 |
| 竞赛代码 | LiveCodeBench | pass@1 | 按时间切分，抗污染较好 |
| 软件工程 | SWE-bench Verified | resolved rate | 需要仓库环境与测试 |
| 工具/Agent | tau-bench、WebArena | task success | 多轮、有状态 |
| 多模态 | MMMU、MathVista | accuracy | 图像/图表理解 |
| 长上下文 | RULER、LongBench | accuracy by length | 要报告长度分桶 |
| 偏好/对话 | Arena-style pairwise | win rate | 需要人类或强模型评判 |

选择基准的原则：

1. **覆盖目标能力**，而不是只挑一个最高的；
2. **保留 held-out**，不要把所有公开集都放进训练；
3. **记录版本与日期**，尤其是会更新的 benchmark（LiveCodeBench、AIME）；
4. **区分训练内/训练外**，分别报告。

## L.3 指标：pass@k、avg@k、maj@k

设一道题采样 $n$ 条，其中 $c$ 条正确。

### pass@k

“k 条里至少一条正确”的无偏估计：

$$
\widehat{\text{pass@}k}
=1-\frac{\binom{n-c}{k}}{\binom{n}{k}}.
$$

要求 $n\ge k$。它衡量**探索上限**，适合代码/数学这种“做对一次就算成功”的任务。报告的 $k$ 必须和 $n$ 一起写；$n=1,k=1$ 就是普通的 pass@1。

### avg@k

先算每条样本是否正确，再对 $k$ 条取平均：

$$
\text{avg@}k=\frac1k\sum_{i=1}^k \mathbb{1}[\text{correct}_i].
$$

它衡量**平均可靠性**，对采样温度敏感。$k$ 越大，方差越小。

### maj@k

$k$ 条里多数投票的答案是否正确：

$$
\text{maj@}k=\mathbb{1}\left[\text{majority}(a_1,\dots,a_k)=\text{gold}\right].
$$

它衡量**集成后的能力**，适合答案可归一化的任务（数学、选择题）。投票规则（数字归一化、单位、多答案）会显著影响结果，必须在报告里写清。

### 三个指标不能混用

- pass@k 高但 avg@k 低：模型偶尔能做对，但不稳定；
- avg@k 高但 maj@k 低：答案分散，投票无效；
- 只报 pass@k 容易高估实际产品体验。

## L.4 采样口径

评测结果对以下参数极其敏感：

```text
temperature, top_p, top_k, max_new_tokens,
n（每题采样数）, stop sequences, repetition penalty,
prompt template, answer parser, chat template
```

最低要求：

- 固定并记录全部采样参数；
- 训练与评测使用**同一个 tokenizer/chat template**；
- 答案抽取器版本化（正则、boxed、最后一个数字）；
- 对超长输出设置明确的截断与惩罚；
- 评测时关闭训练专用的探索技巧（如强制格式、工具注入），除非产品也会用。

一个常见错误是训练用 `temperature=1.0` 探索，评测用 `temperature=0.0` 贪心，然后拿两个数字直接比较。它们回答的是不同问题。

## L.5 污染与泄漏

污染的来源：

- 预训练语料包含评测题；
- 训练集与评测集有重叠/近似重复；
- 题目风格、答案格式被训练数据泄露；
- 人工调参时反复看评测集（“人肉过拟合”）；
- 用强模型生成训练数据，而强模型见过评测集。

检查手段：

| 手段 | 做法 | 局限 |
|---|---|---|
| n-gram 去重 | 13-gram 重叠率 | 改写/翻译可绕过 |
| 语义去重 | embedding 相似度 | 阈值难定 |
| 时间切分 | 只用评测发布时间之前的数据 | 需要可靠时间戳 |
| canary | 在评测集插入特殊串，检测训练集是否包含 | 只能发现直接复制 |
| held-out | 保留私有集，不参与任何调参 | 成本高，但最可靠 |

报告时写清：训练数据截止时间、去重方法、是否有私有 held-out。没有这些信息，跨论文/跨团队的比较基本不可信。

## L.6 成本与统计显著性

### 评测成本

$$
\text{cost}\approx N_{\text{problem}}\times n\times
\left(\text{prompt tokens}\times p_{\text{in}}
+\text{response tokens}\times p_{\text{out}}\right).
$$

$n$ 从 1 提到 8，成本近似 8 倍；如果每题还要多次投票/评判，再乘。评测预算要和训练预算一起规划，别到最后才发现“训得起、评不起”。

### 置信区间

指标是样本估计，必须给不确定性。最简单的是对题目做 bootstrap：

```text
for b in 1..1000:
    sample problems with replacement
    compute metric on the resample
report 2.5% / 97.5% quantiles
```

粗略的样本量直觉：要在 $p\approx0.5$ 附近分辨 2 个百分点的差异，每组大约需要

$$
n\approx\frac{1.96^2\times0.25}{0.01^2}\approx 9600
$$

个独立样本。很多 benchmark 只有几百题，2 个点的差异很可能只是噪声。这也是为什么“多个 seed + 置信区间”比“单次最好成绩”可靠。

## L.7 评测工程

一个可复现的评测管线：

```text
checkpoint + tokenizer + chat template
  -> 固定 prompt 构造
  -> 批量生成（固定采样参数）
  -> 答案解析/归一化
  -> 判分（规则/单测/模型）
  -> 聚合（pass@k/avg@k/maj@k）
  -> 置信区间 + 分桶（长度/难度/题型）
  -> 落盘（原始输出 + 分数 + 配置 + commit）
```

要求：

- **原始输出落盘**：没有原始输出，无法复查 parser 是否误判；
- **缓存**：同一 checkpoint + 同一 prompt 的结果可复用，省钱；
- **沙箱**：代码评测在隔离环境跑，限制时间/内存/网络；
- **并行**：按题目分片，失败重试幂等；
- **回归**：每次模型更新跑一组固定 smoke 题，防止能力回退。

verl 的训练循环里可以挂验证（`trainer.val_before_train`、验证频率、`val_kwargs`），但完整 benchmark 通常用外部 harness 跑 checkpoint。两者分工：训练内验证用于早停/调参，外部评测用于最终报告。

### 可运行的评测入口（示意）

具体参数以各 harness 版本文档为准；关键是固定模型 revision、tokenizer、chat template 与采样参数。

```bash
# 通用文本 benchmark（GSM8K/MATH 等）
lm_eval --model vllm \
  --model_args pretrained=<hf_model>,tensor_parallel_size=1,dtype=bfloat16 \
  --tasks gsm8k,minerva_math \
  --batch_size auto \
  --output_path eval/gsm8k_math.json

# 代码：先生成，再用沙箱跑单测（evalplus / SWE-bench 风格）
python -m evalplus.evaluate --model <hf_model> --dataset humaneval
python -m swebench.harness.run_evaluation \
  --predictions_path predictions.jsonl \
  --run_id <run_id> \
  --max_workers 8
```

三个纪律：

1. **先跑 baseline**：同一 harness、同一 prompt、同一采样参数，否则提升无法归因；
2. **保存原始输出**：分数只是索引，复查要看生成文本；
3. **报告置信区间**：尤其是指标差距小于 2 个点时，不要用单次结果下结论。

## L.8 报告模板

```text
model: <base> + <adapter/checkpoint step>
code: <commit>
image: <digest>
train data cutoff: <date>
eval date: <date>

benchmarks:
  - name, version, split, n_problems
  - sampling: temperature, top_p, max_new_tokens, n
  - parser version
  - metric: pass@1 / pass@k / avg@k / maj@k
  - value + 95% CI
  - cost (GPU-hours or API $)

ablations:
  - what changed, what stayed fixed

known limitations:
  - contamination checks
  - failed categories
  - variance across seeds
```

报告里最重要的不是“最高分”，而是**可比性**：读者能不能用同样的配置复现你的数字。

## L.9 与 verl 验证循环的对接

训练中的验证通常做三件事：

1. **固定验证集**：`data.val_files`，不要和训练集混；
2. **固定采样**：验证的 `n`、temperature、max tokens 与训练探索解耦；
3. **记录样例**：不只记录分数，还记录生成文本，便于发现 parser/reward 问题。

一个实用做法是“双层验证”：

- 每 N 步跑小验证集（几十题），看趋势、早停；
- 每 M 步（或每个里程碑）跑完整 benchmark，出报告。

小验证集用于决策，大 benchmark 用于对外沟通。不要把两者混在一起，否则每次调参都在消耗评测预算并增加过拟合风险。

## L.10 常见错误

- 只报一个 benchmark，且是训练分布内的；
- pass@k 不写 $n$ 和 $k$；
- 评测温度与训练温度不一致；
- parser 改了但没重跑基线；
- 用验证集调超参，再把它当 held-out 报告；
- 没有原始输出，无法定位“分数掉了”；
- 忽略长度分桶：长题/短题的能力差异被平均掉；
- 把 API 成本、延迟排除在“效果”之外。

## 延伸阅读

- LiveCodeBench：<https://livecodebench.github.io/>
- SWE-bench：<https://www.swebench.com/>
- GPQA：<https://arxiv.org/abs/2311.12022>
- MMMU：<https://mmmu-benchmark.github.io/>
- 正文第 24 章：验证、指标与日志
- 正文第 41 章：评测与数据飞轮
