仓库地址：https://github.com/hhk-png/cycle-agent

# 第 14 章：Ray Tune 与超参搜索

> 本章目标：搞清楚 Tune 的四个抽象（`Tuner` / `TuneConfig` / `param_space` /
> `search_alg` / `scheduler`）怎么组合、采样器和调度器该选哪个、
> 以及**怎么把 trial 数量换算成 GPU·小时**。
> 读完你应该能设计一次"预算可控、结论可信"的调参实验。

---

## 14.1 定位与核心抽象

Ray Tune 解决的问题很朴素：**跑很多次训练、每次换一组超参，在预算用完之前把
最好的那组找出来。** 朴素做法的三个痛点（串行太慢、并行要自己管 GPU 与失败
重试、结果要自己汇总）正好对应下面这几个抽象：

```python
from ray import tune
from ray.tune import RunConfig      # ← 注意：是 ray.tune 的 RunConfig

tuner = tune.Tuner(
    trainable,                      # ① 要跑什么（函数 或 Trainable 类）
    param_space={...},              # ② 跑哪些组合（搜索空间）
    tune_config=tune.TuneConfig(
        num_samples=20,             #    跑多少组
        metric="loss", mode="min",  # ③ 怎么判断哪个好
        search_alg=..., scheduler=...,   # ④ 下一组试什么 / ⑤ 谁该提前停
        max_concurrent_trials=4,    #    同时跑几个
    ),
    run_config=RunConfig(...),      #    存储、失败处理、日志
)
results = tuner.fit()               # → ResultGrid
```

`tune.RunConfig` **是可用的**（`ray/tune/impl/config.py` 里有一个 pass-through
包装类，专门负责识别「你用的是不是 `ray.tune.RunConfig`」）。

> ⚠️ **真正要小心的是同名不同类**：
> **`ray.tune.RunConfig` 与 `ray.train.RunConfig` 不是同一个类。**
> 给 `Tuner` 的是前者，给 `Trainer` 的是后者 —— 搞混了会拿到弃用警告，
> 或者某些字段静默失效。
>
> ⚠️ **这是「三件套」而不是一个类**：`RunConfig`、**`CheckpointConfig`**、
> **`FailureConfig`** 在 `ray.tune` 与 `ray.train` 下**各有一套**，
> 每一套都有自己的 pass-through 包装类。给 `Tuner` 传 `ray.train.*` 的三个
> 中的任何一个，都会触发 `RayDeprecationWarning`
> （*"...should be imported from ray.tune when passing it to the Tuner"*）。
>
> ```python
> from ray import tune
> from ray.train import RunConfig as TrainRunConfig      # 给 Trainer 用
> from ray.tune import (RunConfig,                        # 给 Tuner 用 —— 三个都从这
>                       CheckpointConfig,
>                       FailureConfig)
> tune.Tuner(..., run_config=RunConfig(...))
> ```
>
> 两者字段有重叠（`name` / `storage_path` / `failure_config` / `checkpoint_config`），
> 所以**搞混之后往往不报错**，只是行为不对 —— 这类问题最难查。

### 术语对齐

| 术语 | 含义 |
|---|---|
| **trial** | 一次"用一组超参跑一遍训练"的尝试 |
| **trainable** | 被跑的东西：一个函数（`def trainable(config): ...`）或 `Trainable` 子类 |
| **search space（`param_space`）** | 超参的取值范围，值可以是分布对象 |
| **search algorithm（`search_alg`）** | 决定"下一组超参试什么" |
| **scheduler** | 决定"哪些 trial 现在就该停" |
| **`ResultGrid`** | `tuner.fit()` 的返回值，可索引、可筛选的结果集合 |
| **`Result`** | 单个 trial 的结果：metrics / checkpoint / error / path |
关键区分：**`search_alg` 管"往哪搜"，`scheduler` 管"什么时候放弃"** ——
两者正交，可以同时用（比如 Optuna 搜 + ASHA 早停）。

```python
results = tuner.fit()
best = results.get_best_result(metric="loss", mode="min")
df = results.get_dataframe()          # 所有 trial 的指标表 → pandas
for r in results:
    if r.error:                       # 失败的 trial 也在里面
        print(r.config, r.error)
```

`ResultGrid` 里**失败的 trial 不会消失**，它们带着 `error` 字段留在那儿。
统计"有多少 trial 是被 OOM 干掉的"是排查调参管道问题的第一步。

## 14.2 稳定性提示

* **`ray.tune.Tuner` 在源码里标注 `stability="beta"`**（`ray/tune/tuner.py`
  上的 `@PublicAPI(stability="beta")`）；`TuneConfig` 和 `create_scheduler`
  同样是 beta。也就是说 Tune 的**主入口本身**还没承诺稳定。
* **`ray.tune.run` 仍然存在**，但官方推荐用 `Tuner` + `tuner.fit()`。
  检索没有找到明确的"`tune.run` 已弃用"的官方声明 —— 准确表述是：
  **`tune.run` 未删除、但已不是推荐路径**。

还有一组容易踩的历史包袱：

| 老 API | 现状 |
|---|---|
| `tune.report(...)` / `tune.checkpoint_dir(...)` | **Ray 2.0 起弃用**，改用 `ray.tune.report` + `Checkpoint` |
| `ray.air.session.report(...)` | ⚠️ **方向别搞反**：它是 **2.0 起推荐的新 API**（不是被弃用的那个），到 **Train V2（2.51 起）**才被 `ray.train.report` / `ray.tune.report` 取代 —— 所以 V1 教程里出现它是正常的 |
| 在 Tune 函数里调 `ray.train.get_context()` / `get_checkpoint()` / `report` | 已升级为 `DeprecationWarning`（PR #57810），要用 `ray.tune.*` 对应版本 |
| `Checkpoint` 的导入位置 | 传给 `ray.tune.report` 时必须从 `ray.tune` 导入 |

**实践建议**：Tune 的训练函数里 `report` / `get_checkpoint` / `get_context`
一律从 `ray.tune` 取；Ray Train 的训练循环里一律从 `ray.train` 取。
位置错了不会立刻炸，但会淹没在警告里。

## 14.3 搜索空间

`param_space` 的每个值可以是常量，也可以是一个**分布对象**：

```python
from ray import tune

param_space = {
    "optimizer": tune.grid_search(["adam", "sgd"]),      # 枚举：每个值都试
    "activation": tune.choice(["relu", "gelu", "silu"]), # 离散选择
    "dropout": tune.choice([0.0, 0.1, 0.2, 0.3]),        # 等概率;要不等概率得用 sample_from
    "momentum": tune.uniform(0.8, 0.99),                 # 连续均匀
    "lr": tune.loguniform(1e-5, 1e-1),                   # 跨数量级必须用对数
    "batch_size": tune.randint(16, 129),                 # 注意上界是开区间
    "num_layers": tune.qrandint(2, 9, 2),                # 量化：2,4,6,8
    "hidden": tune.qlograndint(64, 1025, 64),            # 对数整数
    "beta": tune.sample_from(lambda spec: spec.config.lr * 10),
}
```

几个必须知道的细节：

* **`tune.randint(lower, upper)` 的上界是开区间**（`range` 语义）。
  写成 `randint(16, 128)` 你永远拿不到 128。
* **`loguniform` 和 `uniform` 不是一回事**。`lr` 用 `uniform(1e-5, 1e-1)` 时，
  采样值有 **≈99%** 的概率落在 1e-3 以上（`(0.1-0.001)/(0.1-1e-5)`）——
  你实际上几乎没搜小学习率区间。
* **`tune.choice` 没有权重参数**，它只做等概率选择。要"不等概率"得自己写
  `tune.sample_from(lambda spec: ...)`。
* **`grid_search` 会展开成笛卡尔积**。两个各 5 值的 `grid_search` = 25 个
  trial，并且**和 `num_samples` 相互作用**（网格固定，`num_samples` 控制每个
  网格点重复几次）。用之前先算一下 trial 总数。
* **`sample_from` 依赖执行顺序**，只有部分搜索算法支持；用随机搜索时它按
  `param_space` 的字典顺序求值。

### 搜索空间是最大的成本杠杆

一个常被忽略的事实：**搜索空间的维度比搜索算法重要得多。** 8 个连续超参
各做 10 点采样 = 10^8 种组合；而随机搜索在超过 ~10 维之后基本退化成碰运气。
所以工程上的第一优先级是**把维度砍下来**：

| 该固定就固定 | 该搜就搜 |
|---|---|
| 架构（层数/宽度）、优化器类型 —— 先定一个 baseline | 学习率（跨数量级，影响最大） |
| 数据增强强度、训练步数 —— 单独做消融 / 固定预算才公平 | weight decay、dropout、warmup 比例 |

## 14.4 采样器（`search_alg`）

| 采样器 | 导入 | 特点 | 何时用 |
|---|---|---|---|
| `BasicVariantGenerator` | 默认，无需指定 | 随机搜索 / 网格搜索 | 维度低、预算小、要可复现 |
| `OptunaSearch` | `ray.tune.search.optuna` | TPE，成熟、生态好 | 中等维度、预算中等（首选） |
| `HyperOptSearch` | `ray.tune.search.hyperopt` | TPE 的经典实现 | 与已有 HyperOpt 流程对接 |
| `AxSearch` | `ray.tune.search.ax` | 高斯过程 / BoTorch | 低维（<10）、每次评估很贵 |
| `BayesOptSearch` | `ray.tune.search.bayesopt` | 经典贝叶斯优化 | 需要 `bayesian-optimization` 包 |

**关键取舍**：贝叶斯类方法（Optuna TPE / Ax / BayesOpt）**需要串行或半串行的
评估**，至少要等若干个 trial 出结果才能建模。你并行开 32 个 trial 时，前 32 个
基本是随机的 —— 贝叶斯的价值被并行度稀释。所以 **低并发 + 贝叶斯** 通常优于
**高并发 + 随机**（前提是单次评估足够贵）；反过来，单次评估只要几分钟时，
**高并发随机搜索 + 好调度器**往往最快、最省心。

```python
from ray.tune.search.optuna import OptunaSearch

tuner = tune.Tuner(trainable, param_space=param_space,
    tune_config=tune.TuneConfig(
        num_samples=50, metric="loss", mode="min",
        search_alg=OptunaSearch(metric="loss", mode="min"),
        max_concurrent_trials=4))     # ← 别开太高，否则 TPE 建模用不上
```

## 14.5 调度器（`scheduler`）

调度器决定**提前停掉没希望的 trial**。这是省预算最直接的手段。

| 调度器 | 原理 | 适用 | 注意 |
|---|---|---|---|
| **ASHA**（`ASHAScheduler`） | 异步逐次减半：所有 trial 先跑少量 step，按指标淘汰一大部分（默认淘汰 3/4），剩下的跑更久，层层递进 | **默认首选**，尤其 trial 多、单次贵 | 需要按 step 上报指标；`grace_period` 要给够 |
| **HyperBand**（`HyperBandScheduler`） | 同步版逐次减半：分轮次，每轮内所有 trial 必须等齐 | 评估时间方差大 | 同步 → 有 straggler 问题，利用率低于 ASHA |
| **PBT**（`PopulationBasedTraining`） | 进化：定期把差 trial 的参数换成好 trial 的，并扰动 | 训练**过程**中的超参（lr 调度、熵系数） | 会"改配置"，不是纯粹早停；要 `perturbation_interval` |
| **MedianStoppingRule** | 指标低于同 step 中位数就停 | 最简单、最容易解释 | 比 ASHA 保守，省得少 |
| **FIFOScheduler** | 不早停，来了就跑（默认值） | 调试 / 评估次数很少 | — |

### ASHA 到底怎么工作

```
r=1  ┌ t1 ─┐┌ t2 ─┐┌ t3 ─┐┌ t4 ─┐┌ t5 ─┐┌ t6 ─┐┌ t7 ─┐┌ t8 ─┐  各跑 1 个 rung
     └─────┘└─────┘└─────┘└─────┘└─────┘└─────┘└─────┘└─────┘
          ↓ 按指标排序，淘汰后 75%（reduction_factor=4 的默认行为；不是 50%）
r=2  ┌ t1 ────────┐┌ t3 ────────┐                             各跑 2 个 rung
     └────────────┘└────────────┘
          ↓ 再淘汰 3/4
r=4  ┌ t1 ─────────────────────┐                              → best
     └─────────────────────────┘
```

> ⚠️ **默认淘汰比例是 3/4 而不是 1/2** —— 图里的 `reduction_factor=4`
> 就是"每级只留 1/4"。想要经典的"逐次减半"要显式写
> `reduction_factor=2`。本书早先的图与文字用了 50%，**与同节
> "默认 4（每级淘汰 3/4）"的说法矛盾**，这里以 `reduction_factor` 为准。

* **异步**：某个 trial 跑完一个 rung 就能升到下一级，不用等其他 trial ——
  这是它比 HyperBand 高效的原因（没有同步 barrier）。
* 总资源大致是 `num_samples × grace_period × reduction_factor /
  (reduction_factor - 1)` 这个量级，**远小于**让所有 trial 跑满。
* `reduction_factor` 默认 4（每级淘汰 3/4）。调成 2 更保守；调成 4 以上
  淘汰太狠，容易误杀"慢热"的配置。

### 该选哪个

| 你的情况 | 建议 |
|---|---|
| 不知道该选什么 | **ASHA**（`grace_period` 约为单次评估预算的 1/10） |
| trial 数少（<10）、评估极贵 | 不用调度器，或 `MedianStoppingRule` |
| 要调训练过程中的超参（lr schedule、RL 的熵系数） | **PBT** |
| 评估时间差异极大（有的 1 分钟有的 1 小时） | **ASHA**（HyperBand 会被 straggler 拖死） |
| 只是跑网格、要看完整曲线 | `FIFOScheduler`（不早停） |
| 想复现论文里的同步逐次减半 | `HyperBandScheduler` |

```python
from ray.tune.schedulers import ASHAScheduler

scheduler = ASHAScheduler(metric="loss", mode="min",
                          max_t=100, grace_period=10, reduction_factor=4)
```

`grace_period` 设太小会**误杀**：前 10 步 loss 高但后面会降的配置会被提前
干掉；设太大则省不下预算。经验值是"总步数的 10%"，且要和 lr warmup 长度对齐。

## 14.6 报告指标与检查点

### 现代写法

```python
from ray import tune
from ray.tune import Checkpoint

def trainable(config):
    model = build_model(config)
    for step in range(100):
        loss = train_one_step(model, config)
        tune.report({"loss": loss, "step": step})   # 上报 → 调度器才有得看

    with tempfile.TemporaryDirectory() as d:        # 结束时存 checkpoint
        torch.save(model.state_dict(), os.path.join(d, "model.pt"))
        tune.report({"loss": loss}, checkpoint=Checkpoint.from_directory(d))
```

要点：**上报频率决定调度器的反应速度** —— 这是这一节唯一需要你主动决策的
参数。一个 epoch 报一次，ASHA 就只能按 epoch 粒度淘汰；每个 epoch 要 10 分钟
时，早停就废了一半。另外 **`Checkpoint` 必须从 `ray.tune` 导入**（传给
`tune.report` 时）。不存 checkpoint 也能调参，只要你不打算
"用最优配置再训一次"或做容错恢复。

🔴 **`ray.tune.Checkpoint` 只有"目录"一个构造器**（这条本书早先写错过）：
`ray/tune/trainable/trainable_fn_utils.py` 里写得很清楚 ——

```python
@_copy_doc(TrainCheckpoint)
class Checkpoint(TrainCheckpoint):
    # NOTE: This is just a pass-through wrapper around `ray.train.Checkpoint`
    # in order to detect whether the import module was correct `ray.tune.Checkpoint`.
    pass
```

它是 `ray.train.Checkpoint` 的一个 **pass-through 子类**（存在的唯一目的是
"从哪个模块导入的"检测），所以**它的能力集与 `ray.train.Checkpoint` 完全一致**：
`Checkpoint.from_directory(local_dir)` 与 `Checkpoint(path=...)` 可用，
而 **`from_dict` / `to_dict` / `from_uri` / `uri` 这些名字在 2.58 已被移除**
—— 调用会抛 `AttributeError`（带一段迁移提示）。本书早先把
`from_dict({...})` 列成"最常用的构造器"是错的，见第 13 章 §13.3 里那张
"已被元类拦截的名字"表。

要上报一个小字典（比如 `{"step": n}`）**不要塞进 checkpoint** ——
它本来就该走 `tune.report` 的 metrics 字典。真要落成文件，
自己写进目录再 `from_directory`。

### `metric` / `mode` 与严格检查

`tune.TuneConfig(metric="loss", mode="min")` 表示越小越好；`mode="max"` 用于
accuracy 这类指标。如果你 `report` 了多个指标、但 `metric` 指定的那个**某次
report 里没出现**，Tune 默认直接报错。相关环境变量：

| 环境变量 | 作用 |
|---|---|
| `TUNE_DISABLE_STRICT_METRIC_CHECKING=1` | 关掉"每次 report 都必须含 metric"的严格检查 |
| `TUNE_DISABLE_AUTO_CALLBACK_LOGGERS` | 关掉自动挂上的日志 callback（**注意不是 `..._CALLBACK_SYNCER`**） |
| `TUNE_RESULT_BUFFER_LENGTH` | 结果缓冲长度，影响内存与 dashboard 刷新 |
| `TUNE_MAX_PENDING_TRIALS_PG` | 放置组模式下**同时挂起的 trial 数**上限。这才是真正的兜底并发开关；`RAY_TUNE_MAX_CONCURRENT_TRIALS` 这个环境变量**不存在**，别照抄 |

**实践建议**：如果 trial 可能中途抛异常退出、而你想让它"至少贡献已跑出的
指标"，就该设 `TUNE_DISABLE_STRICT_METRIC_CHECKING=1` —— 否则一个格式不
一致的 report 会让整个实验失败。

## 14.7 与 Ray Train 的组合（HPO + 分布式训练）

写法在第 13.8 节讲过，这里只说**取舍**。Tune 调分布式训练时成本会**相乘**：

```
总 GPU·小时 ≈ num_samples × (每 trial 的 GPU 数) × (每 trial 的墙钟小时)
例：20 个 trial × 每个 4 张 GPU × 每个 0.5 小时 = 40 GPU·小时
```

加 ASHA 早停后实际可能只花 1/3 到 1/2（大部分 trial 跑不到 30 分钟就被砍了）
—— **这就是为什么分布式训练场景下调度器不是可选项。**

### 并发控制：一个容易算错的参数

正确算法是 `max_concurrent_trials = floor(集群总 GPU 数 / 每个 trial 的 GPU 数)`。
比如 8 张卡、每个 trial 要 4 张卡 → `max_concurrent_trials=2`。

**常见的错**是把它设得比这个值大。后果不是"跑得更快"，而是**一部分 trial
卡在 PENDING**，占着 trial 名额不干活 —— dashboard 上看起来"都在跑"，实际
只有 2 个在推进。更糟的是它会让依赖"看到完整 trial 结果"的搜索算法
（TPE/贝叶斯）拿到一堆空结果。

另一种控制方式：**给 trainable 声明资源**，让 Tune 自己算能开几个。

```python
# ✅ 正确:用 tune.with_resources 包住 trainable
tuner = tune.Tuner(
    tune.with_resources(objective, {"cpu": 4, "gpu": 1}),
    param_space={...},
)
```

> ⚠️ **`TuneConfig` 没有 `resources_per_trial` 这个字段** ——
> 那是 `tune.run()` 时代的参数，**照抄会直接 `TypeError`**。
> `TuneConfig` 的实际字段是 `mode` / `metric` / `search_alg` / `scheduler` /
> `num_samples` / `max_concurrent_trials` / `time_budget_s` / `reuse_actors` /
> `trial_name_creator` / `trial_dirname_creator` / `chdir_to_trial_dir`。
> Tuner API 下声明资源只有两条路：
> ① `tune.with_resources(trainable, {"cpu": 4, "gpu": 1})`；
> ② trainable 本身是 Ray Train 的 `Trainer` 时，走它的 `ScalingConfig`。

**`with_resources` 和 `max_concurrent_trials` 选一个用就行**：
前者是"每个 trial 要多少"，后者是"同时最多几个"，两个都设容易互相矛盾。
**并且 `max_concurrent_trials` 会与 `ConcurrencyLimiter` 互斥** ——
如果你已经把 `search_alg` 包成了 `ConcurrencyLimiter`，
再设 `max_concurrent_trials` 会**直接抛异常**（官方 docstring 明确写了这一点）。
只用一个。

## 14.8 成本控制实践

调参最常见的失败不是"搜不到好结果"，而是"烧了三天 GPU 得出一个不可信的结论"。

**第 1 步：smoke run。** 先用 `num_samples=2`、`max_t=5` 跑通，确认训练函数
能跑、能上报、能存 checkpoint，`mode` 没搞反，以及每个 trial 的实际耗时和
GPU 占用。几分钟的投入能挡掉 80% 的低级错误。

**第 2 步：先定预算，再反推 trial 数。**

```
可用预算 = 40 GPU·小时
单个 trial 满配成本 = 4 GPU × 0.5 h = 2 GPU·小时
ASHA 平均只跑满配的 40%  → 每 trial 实际 ≈ 0.8 GPU·小时
→ num_samples ≈ 40 / 0.8 = 50
```

**一定不要反过来**（先定 `num_samples=200` 再看要花多少钱）。

**第 3 步：粗搜 → 精搜。**

| 阶段 | 搜索空间 | `num_samples` | 调度器 | 单 trial 预算 |
|---|---|---|---|---|
| 粗搜 | 宽（lr 1e-5–1e-1） | 大（30–100） | ASHA 激进 | 短（总步数的 10–20%） |
| 精搜 | 窄（围绕粗搜最优 ± 一个数量级） | 小（10–20） | ASHA 保守 | 长（60–100%） |

粗搜单 trial 预算短是因为**排序比精度重要** —— 你只需要知道哪个数量级的学习率
好，不需要知道它最终能到多少 loss。

**第 4 步：留出验证集。** Tune 报的 best 是**在搜索集上选出来的**，天然有
选择偏差，`num_samples` 越大越明显。务实做法：拿粗搜的 top-3 配置在**没参与
搜索的验证集**上各跑一次，再决定交付哪个。

### 成本换算速查

| 你改的旋钮 | 成本变化 |
|---|---|
| `num_samples` 翻倍 | 成本翻倍（线性） |
| `max_concurrent_trials` 翻倍 | 墙钟时间减半（资源够的话），**成本不变** |
| 每个 trial 的 GPU 数翻倍 | 成本翻倍，墙钟通常减不到一半 |
| 开 ASHA（`grace_period`=10% 总步数，`reduction_factor=4`） | 成本降到 1/3 – 1/2 |

**最重要的一条**：`max_concurrent_trials` 只影响**墙钟时间**，不影响总成本。
想省钱只有三个办法 —— 少搜、早停、缩短单次评估；想省时间只有一个办法 ——
提高并发（前提是资源够、且搜索算法能容忍）。

## 14.9 完整 HPO 示例

不依赖 GPU、不需要 PyTorch，只用 NumPy 和一个手写的目标函数 —— 可以直接跑起来
看调度器怎么砍 trial。目标函数故意做成"慢热"（早期 loss 高、后期会降），
用来观察 `grace_period` 怎么影响误杀。

```python
"""模拟"每个 trial 是一次训练"，找使 loss 最小的 (x, y, lr)。"""
import time
import numpy as np
from ray import tune
from ray.tune.schedulers import ASHAScheduler

OPTIMUM = np.array([0.3, -0.7])          # 真实最优（现实中你不知道）


def objective(x, y, lr, step):
    """带噪声的二次函数；lr 太小 → 收敛慢（模拟慢热），step 越大噪声越小。"""
    progress = min(1.0, lr * step / 20.0)
    base = (x - OPTIMUM[0]) ** 2 + (y - OPTIMUM[1]) ** 2
    return base * (2.0 - progress) + 0.05 * (1.0 - progress) * np.random.rand()


def trainable(config):
    for step in range(1, 101):
        loss = objective(config["x"], config["y"], config["lr"], step)
        tune.report({"loss": loss, "step": step})   # 每 step 上报，调度器才反应得过来
        time.sleep(0.001)                           # 模拟计算耗时


param_space = {"x": tune.uniform(-2.0, 2.0),
               "y": tune.uniform(-2.0, 2.0),
               "lr": tune.loguniform(1e-3, 1.0)}   # 跨数量级 → 必须 loguniform

tuner = tune.Tuner(
    trainable, param_space=param_space,
    tune_config=tune.TuneConfig(
        num_samples=40, metric="loss", mode="min",
        scheduler=ASHAScheduler(metric="loss", mode="min",
                                max_t=100,           # 单 trial 最多 100 step
                                grace_period=20,     # 至少 20 step 才允许淘汰
                                reduction_factor=4),
        max_concurrent_trials=8),
)
results = tuner.fit()

best = results.get_best_result(metric="loss", mode="min")
print("best config:", best.config, "最优解附近:", OPTIMUM)
steps = [r.metrics.get("step", 0) for r in results if not r.error]
print(f"平均实际跑了 {np.mean(steps):.1f}/100 step，"
      f"全跑满的成本是现在的 {100 / np.mean(steps):.1f} 倍")
print(results.get_dataframe()[["config/lr", "config/x", "loss", "step"]]
      .sort_values("loss").head(5))
```

跑完会看到两件事：`best.config` 的 `lr` 偏向 `loguniform` 区间的中上部（lr
太小的 trial 是"慢热"的，会被 ASHA 提前淘汰 —— 这就是 `grace_period` 的
取舍）；大量 trial 的 `step` 停在 20，成本只有"全跑满"的 1/3 左右。

换成真实训练时，把 `objective()` 换成 `model.train()` + `loss.backward()`、
`time.sleep` 换成真实 GPU 计算、加上 `checkpoint=Checkpoint.from_directory(d)`、
把 `max_concurrent_trials` 换成 `tune.with_resources(objective, {"gpu": 1})` 即可
（⚠️ 不是 `TuneConfig(resources_per_trial=...)` —— 那个字段不存在，见 §14.7）。
**唯一别改的是上报频率** —— 报得太稀，早停就失效了。

## 14.10 与 mini-ray 的关系

**mini-ray 没有实现 Ray Tune**（README 的"明确不做"清单里列了
Ray Data/Train/Tune/Serve/RLlib）。但 Tune 的核心机制**并不神秘**，
就是 Ray Core 几个原语的组合：

| Tune 的组件 | 对应的 Ray Core 原语 | mini-ray 里的位置 |
|---|---|---|
| trial（一次训练尝试） | 一个 actor 或一个远程函数 | `actor.py` / `remote.py` |
| `with_resources` / `max_concurrent_trials` | 资源模型 + 调度队列 | `scheduler.py` |
| 每个 trial 上报指标 | actor 方法调用 + driver 收集 | `raylet_client.py` |
| 失败的 trial 自动重试 | 任务重试 / actor 重启 | `test_fault_tolerance.py` |
| `ResultGrid` 汇总 | driver 侧的 `ray.get` 聚合 | `object_ref.py` |
| 早停（调度器） | **纯 driver 侧逻辑**（发 `ray.cancel`） | `ray.cancel` 在 `miniray/__init__.py`（由 `raylet.py` / `raylet_client.py` 支撑） |

最后一行值得单独说：**调度器本身不需要任何分布式原语**。ASHA 的全部逻辑就是
"读各 trial 的最新指标 → 排序 → 对后一半调 `ray.cancel(ref, force=True)`"：

```python
# mini-ray 风格的玩具 ASHA 骨架（伪代码）
import miniray as ray

refs = [trainable.remote(cfg) for cfg in sample_configs(40)]
for rung in [20, 40, 80, 100]:
    ray.wait(refs, num_returns=len(refs), timeout=rung)   # 等到这一级
    metrics = ray.get([m.remote(r) for r in refs])        # 取最新指标
    keep = top_half(refs, metrics)                        # 排序留一半
    for r in set(refs) - set(keep):
        ray.cancel(r, force=True)                         # 淘汰
    refs = keep
```

和真实 ASHA 的差别只在：真实的调度器是**异步**的（不等齐）、把
`grace_period` / `reduction_factor` 参数化、并和 checkpoint/恢复机制对接。
可运行的对照物见 `examples/06_fault_tolerance.py` 与 `tests/test_core.py` 里的
`ray.cancel` 用例。

## 14.11 `Stopper`：和调度器并列的另一条"提前停"路径

§14.5 的调度器是**跨 trial 比较**之后决定砍谁；`Stopper` 是**单个 trial 自己满足
条件就停**，或者让**整个实验**停。两者可以同时用，也可以在不用调度器时单独用。

```python
from ray import tune
from ray.tune import RunConfig
from ray.tune.stopper import (CombinedStopper, MaximumIterationStopper,
                              Stopper, TimeoutStopper, TrialPlateauStopper)

tuner = tune.Tuner(
    trainable, param_space=param_space,
    run_config=RunConfig(stop=CombinedStopper(
        # 平台期：指标在 num_results 次上报内标准差小于 std 就停掉这个 trial
        TrialPlateauStopper(metric="loss", mode="min",
                            std=0.01, num_results=4, grace_period=4),
        MaximumIterationStopper(max_iter=100),   # 最多 100 次迭代
        TimeoutStopper(timeout_s=600),           # 600 秒后停掉整个实验
    )),
    tune_config=tune.TuneConfig(num_samples=20, metric="loss", mode="min"),
)
```

| Stopper | 停谁 | 判据 | 关键参数 |
|---|---|---|---|
| `TrialPlateauStopper` | 单个 trial | 指标在 `num_results` 次上报内的标准差 < `std` | `metric` / `mode` / `std`（默认 0.01）/ `num_results`（默认 4）/ `grace_period`（默认 4）/ `metric_threshold` |
| `MaximumIterationStopper` | 单个 trial | 迭代数到上限 | `max_iter` |
| `TimeoutStopper` | **整个实验** | 墙钟超时 | `timeout_s` |
| `ExperimentPlateauStopper` | **整个实验** | 全局最优指标进入平台期 | `patience` 等 |
| `CombinedStopper` | 组合 | 任一子 stopper 触发即触发（**OR**） | 位置参数传多个 stopper |
| `FunctionStopper` / `NoopStopper` | — | 用一个函数当 stopper / 什么都不做 | — |

自定义 `Stopper` 只需要实现两个方法：

```python
class MyStopper(Stopper):
    def __call__(self, trial_id: str, result: dict) -> bool:
        """返回 True → 停掉这个 trial。result 就是最近一次 report 的 dict。"""
        return result.get("loss", 1e9) < 1e-3

    def stop_all(self) -> bool:
        """返回 True → 停掉整个实验（所有 trial）。"""
        return self._budget_exhausted
```

⚠️ **两个容易搞混的点**：

1. **`TimeoutStopper` 停的是整个实验，不是单个 trial。** 想限制"每个 trial 最多
   跑多久"，要么在训练函数里自己数墙钟并 `raise`，要么用
   `MaximumIterationStopper` 配合可控的每步耗时。
2. **`stop_all()` 一返回 True，整个实验就结束了** —— 包括还没跑完的 trial。要
   "优雅收尾"就得自己在 `stop_all()` 之前做检查，否则会被当成一次失败退出。

### `stop=` 的字典简写

Tune 支持用字典表达"指标到阈值就停"。键是**你在 `report` 里报过的指标名**
（也包括 `training_iteration`、`time_total_s` 这类自动填充字段），值是阈值：

```python
tuner = tune.Tuner(
    trainable, param_space=param_space,
    run_config=RunConfig(stop={"training_iteration": 20,   # 最多 20 轮
                               "mean_accuracy": 0.95}),    # 或准确率到 0.95
)
```

⚠️ 这里有个语义陷阱：**字典形式假定指标"越大越好"** —— trial 在指标**超过**
阈值时停。想表达"loss 低于 0.01 就停"，用
`TrialPlateauStopper(metric="loss", mode="min", metric_threshold=0.01)` 或自定义
`Stopper`，别指望字典。`time_budget_s` 本质就是自动装了一个 `TimeoutStopper`
（该参数现在该往哪儿传见 §14.16）。

> **未确认**：任务材料里提到 `stop={"metric": "loss", "mode": "min"}` 这种
> "指定指标 + 方向"的简写。本次检索到的 tune-stopping 官方文档里，字典形式是
> "指标名 → 阈值"，而 `metric` / `mode` 是 `TuneConfig` 的参数、不是 `stop` 的键。
> 该简写是否存在**未确认**，本节不建议使用。

## 14.12 `tune.Callback`：把实验过程钩出来

前面所有"上报 → 调度器反应"的链路都是**单向**的：训练函数往 Tune 里塞指标，
Tune 决定停不停。`Callback` 是唯一一个**在 Tune 内部事件上挂钩子**的入口。

```python
from ray import tune
from ray.tune import Callback, RunConfig

class EarlyStopWatcher(Callback):
    def __init__(self):
        self.stopped, self.done = [], []

    def on_trial_start(self, iteration, trials, trial, **info):
        print(f"[开始] {trial.trial_id} {trial.config}")

    def on_trial_complete(self, iteration, trials, trial, **info):
        # 被调度器砍掉 / 被 Stopper 停掉 / 正常跑完，走的都是这里，
        # 区别只在 trial.last_result 与你自己的迭代上限
        self.done.append(trial.trial_id)

    def on_trial_error(self, iteration, trials, trial, **info):
        print(f"[失败] {trial.trial_id}: {trial.error}")

    def on_checkpoint(self, iteration, trials, trial, checkpoint, **info):
        print(f"[检查点] {trial.trial_id} → {checkpoint.path}")

    def on_experiment_end(self, trials, **info):
        print(f"结束：{len(self.done)} 个 trial 走完")

tuner = tune.Tuner(trainable, param_space=param_space,
                   run_config=RunConfig(callbacks=[EarlyStopWatcher()]))
```

| 钩子 | 触发时机 | 常见用途 |
|---|---|---|
| `on_trial_start` | trial 起来之后 | 打日志、记录启动顺序 |
| `on_trial_result` | **每次** `report` 之后 | 自定义实时监控（它会非常频繁） |
| `on_trial_complete` | trial 结束（正常 / 被砍 / 被停） | 统计早停命中率、汇总指标 |
| `on_trial_error` | trial 抛异常 | 报警、把失败配置落盘 |
| `on_trial_save` / `on_checkpoint` | trial 存了 checkpoint | 额外搬运、校验 checkpoint |
| `on_trial_restore` | trial 从 checkpoint 恢复 | 恢复路径的可观测性 |
| `on_experiment_end` | 实验结束 | 生成报告、释放外部资源 |

**几个必须知道的点**：

* **回调挂在 `RunConfig(callbacks=[...])` 上**，**不是 `TuneConfig`** —— 放错了不
  会报错，只是永远不触发。
* **回调在 driver 进程里串行执行**。在 `on_trial_result` 里做重活（写数据库、
  发 HTTP）会直接拖慢整个实验；重活请丢队列或攒批。
* **"被早停"不是一个独立事件**。调度器砍 trial 走的就是 `on_trial_complete`，
  且 `error is None` —— 要靠 `trial.last_result` 和已知的迭代上限去区分"跑完了"
  与"被砍了"。这是拿到早停通知的唯一入口。
* **自定义指标与模型产物**：`report` 的 `metrics` 可以是任意可序列化字典（嵌套
  dict 会被展平成 `a/b`，方便 `get_dataframe()`），但**调度器只看 `metric` 指定
  的那一个标量**。导出的模型产物（ONNX、tokenizer、config.json）走
  `Checkpoint.from_directory(d)`，让 Tune 管搬运与保留策略，**不要塞进 metrics**
  —— metrics 会写进每条 result 与 JSON/CSV，塞大对象会让结果文件爆炸。
* Tune 自带的 TensorBoard / CSV / JSON logger 本身就是用 `Callback` 实现的，
  可用 `TUNE_DISABLE_AUTO_CALLBACK_LOGGERS=1` 关掉。

## 14.13 `with_resources` / `with_parameters`：给 trainable 挂东西

这两个函数不改变搜索算法，改变的是**"trainable 这个对象本身携带什么"**。它们
可以嵌套，且优先于 `TuneConfig` 里的同名设置：

```python
from ray import tune

def train_fn(config, train_data, val_data, model_ref):
    ...                     # train_data / val_data / model_ref 由 with_parameters 注入

trainable = tune.with_resources(
    tune.with_parameters(train_fn,
                         train_data=train_ds,      # 大对象：数据集
                         val_data=val_ds,
                         model_ref=model_ref),
    resources={"cpu": 4, "gpu": 0.5},                  # 支持小数 GPU
)

tuner = tune.Tuner(trainable, param_space=param_space)
```

| API | 作用 | 什么时候用 |
|---|---|---|
| `tune.with_resources(trainable, resources=...)` | 把资源需求绑在 trainable 上 | 一个实验里**不同 trainable 资源需求不同**；或你要交付一个自带资源声明的 trainable 对象 |
| `tune.with_parameters(trainable, **kwargs)` | 把固定参数 / 大对象注入 trainable | 参数不是超参、不该被搜，也不适合放进 `param_space` |

**`with_parameters` 的代价要说清楚**：它把这些对象**放进 Ray object store**，
每个 trial 通过 object ref 取用。好处是**大对象只传一次、所有 trial 共享** ——
比闭包（每个 trial 各自序列化一份）或写进 `param_space`（会进每条结果记录）都划算。
代价是：

1. **占对象存储内存**。放一个 50 GB 的数据集进去，会直接吃满对象存储；
2. **恢复实验时必须重新指定**。object ref 绑在**当前 Ray 会话**上，换集群、换
   脚本、对象被 GC 之后，`Tuner.restore()` 会报错并列出缺失的 `with_parameters`
   对象。修法**不是**给 `restore` 传 `with_parameters=`（它**没有**这个形参），
   而是把**重新包好的 trainable** 传进去：
   `tune.Tuner.restore(path, trainable=tune.with_parameters(fn, **same_kwargs))`；
3. **巨型数据集不该走这条路**，应该用 Ray Data 或共享存储路径（`/mnt/...`、
   `s3://...`），让每个 trial 自己按需读。

> **`with_resources` 与 `TuneConfig` 的分工**（⚠️ 前提是 `TuneConfig`
> **没有** `resources_per_trial` 字段 —— 见 §14.7）：
> * 声明**资源** → 只有 `tune.with_resources(trainable, {...})` 这一条路；
> * 声明**并发/搜索算法/调度器** → `TuneConfig`。
>
> 需要"同一个 `Tuner` 里不同 trainable 拿不同资源"时更是只能用它 ——
> 因为 `with_resources` 是**包在 trainable 上**的，天然支持按 trainable 区分。

## 14.14 checkpoint 的保留与清理

默认行为是**全都留着** —— 一个 50 trial、每轮存一次的实验，几天就能把盘写满，
大量时间花在往存储同步没用的东西上。控制点有两个：`CheckpointConfig`（保几个、
保哪几个）和 `RunConfig`（存在哪、叫什么）。

```python
from ray import tune
from ray.tune import CheckpointConfig       # ⚠️ 从 ray.tune 导入,不是 ray.train
                                            #    (同名不同类,传错会吃弃用警告)
from ray.tune import RunConfig

run_config = RunConfig(
    name="lr-sweep-01",
    storage_path="/mnt/shared/ray_results",   # 必须全节点可访问
    checkpoint_config=CheckpointConfig(
        num_to_keep=3,                        # 只留 3 个
        checkpoint_score_attribute="loss",    # 按哪个指标挑（得是 metrics 里的键）
        checkpoint_score_order="min",         # min：留指标最低的 3 个
    ),
)
```

| 字段 | 含义 | 注意 |
|---|---|---|
| `num_to_keep` | 最多在盘上留几个 checkpoint | `None` = 全留；**必须 `None` 或 `>= 1`** —— 给 0 或负数会直接 `ValueError`（`ray/train/v2/api/config.py` 的 `__post_init__`） |
| `checkpoint_score_attribute` | 用哪个指标给 checkpoint 打分 | 必须是 **checkpoint 字典里**的数值键；不设 = 保留**最近**的 N 个 |
| `checkpoint_score_order` | `"max"` / `"min"` | 方向要和 `mode` 一致，别搞反；**默认是 `"max"`**，取值只接受这两个（其它值 `ValueError`） |
| ~~`checkpoint_frequency`~~ | ❌ **已弃用**（2.58 里是 `_DEPRECATED` 哨兵） | 传了会**抛 `DeprecationWarning`**，不是"静默忽略"。**V2 下"多久存一次"由你的训练函数自己决定**——你想存就调 `ray.train.report(..., checkpoint=...)` |
| ~~`checkpoint_at_end`~~ | ❌ **已弃用** | 同上，传了就抛。源码里的原文是 *"`checkpoint_at_end` is deprecated since it does not apply to user-defined training functions."* —— 训练函数是你写的，**结束时的 checkpoint 也该由你自己报** |

> 🔴 **本书早先这两行写错了**（原来把 `checkpoint_frequency` / `checkpoint_at_end`
> 列成可用字段，还配了"存太勤会成瓶颈"的调参建议）。在 **V2（2.51 起默认开启）**
> 下它们**不是"还能用但已弃用"，而是传进去就抛**：
> `ray/train/v2/api/config.py` 的 `CheckpointConfig.__post_init__` 里对这两个字段
> 都是 `raise DeprecationWarning(...)`。**想控制存 checkpoint 的节奏，
> 唯一的地方是你的训练函数**（`ray.train.report` 的调用频率）——
> 这也和第 13 章 §13.3 说的"上报频率决定一切"是同一件事。

**三条实践经验**：

1. **调参实验里 checkpoint 的作用被高估了**。只需要"用最优配置再训一次"或做
   容错恢复时，`num_to_keep=1` + 指定打分指标是最省事的组合 —— 留最优一个，
   其余全删。
2. **`num_to_keep` 单独设基本没用**。不配 `checkpoint_score_attribute` 时它保留的
   是**最近的** N 个，通常不是你要的那个。
3. **`storage_path` 要给共享存储**。多节点下 `/tmp` 是各节点各自的，恢复时会
   找不到对象（§14.16）。

## 14.15 类式 `Trainable` 与 `reuse_actors`

前面所有例子都是**函数式 trainable**（`def trainable(config)`）—— 简单、够用。
但有两种情况需要**类式 `Trainable`**：

```python
from ray.tune import Trainable

class MyTrainable(Trainable):
    def setup(self, config):                 # 每个 trial 开始时调一次
        self.model = build_model(config)
        self.step_count = 0

    def step(self) -> dict:                  # 一次迭代，返回值即 report 的内容
        loss = self.model.train_one_step()
        self.step_count += 1
        return {"loss": loss, "training_iteration": self.step_count}

    def save_checkpoint(self, checkpoint_dir: str) -> str:
        torch.save(self.model.state_dict(), f"{checkpoint_dir}/model.pt")
        return checkpoint_dir                # 返回目录或 dict

    def load_checkpoint(self, checkpoint_dir: str) -> None:
        self.model.load_state_dict(torch.load(f"{checkpoint_dir}/model.pt"))

    def cleanup(self):                       # trial 结束时释放外部资源
        ...
```

| 生命周期方法 | 时机 | 用途 |
|---|---|---|
| `setup(config)` | 每 trial 一次 | 建模型 / 建连接。**别在 `__init__` 里做重活** |
| `step()` | 每次迭代 | 训练一步并返回指标 dict |
| `save_checkpoint(dir)` / `load_checkpoint(dir)` | Tune 决定存 / 恢复时 | 容错恢复、PBT 交换权重都依赖它 |
| `cleanup()` | trial 结束 | 关连接、删临时文件 |
| `reset_config(config)` / `reset()` | actor 复用（见下） | 换配置、重置状态 |

> 老教程里的 `_setup` / `_train` / `_save` / `_restore` 在较新版本已改为不带
> 下划线的名字（**具体在哪一版移除未确认**）。照抄老代码会得到"方法写了但从不
> 被调用"这类安静的错误。

**什么时候还必须用类式**：① 要用 `reuse_actors`；② 训练循环本身是"每次
`step()` 由 Tune 决定跑多久"，而不是写死的 `for step in range(N)`；③ 有昂贵的
可复用状态（大模型常驻、连接池）。

### `reuse_actors`：省掉每个 trial 的启动成本

默认每个 trial 新建一个 actor、跑完销毁。trial 多、而 trainable 启动成本高
（加载模型、编译 kernel）时，这一步会吃掉可观比例的时间：

```python
tuner = tune.Tuner(
    MyTrainable, param_space=param_space,
    tune_config=tune.TuneConfig(num_samples=50, reuse_actors=True),
)
```

* **前提**：trainable 是**类式**的 —— 函数式 trainable 每个 trial 带一套独立的
  闭包状态，无法安全复用（该限制的精确边界**未确认**，以官方文档为准）。
* **代价是状态会串味**。复用的 actor 会把上一个 trial 留在实例上的属性、没关掉
  的连接、没重置的 optimizer 带进下一个 trial。**每个 trial 独有的状态必须在
  `setup()` 里重建，不要放在 `__init__`。**
* **收益最明显的场景**：PBT（trial 反复被"重启"到别人的 checkpoint 上）和需要
  加载大模型的调参。

## 14.16 `time_budget_s` 与 `Tuner.restore()`

这两个是**运维层面**的开关：一个是"到点收工"，一个是"接着上次跑"。

```python
from ray import tune
from ray.tune import FailureConfig           # ⚠️ 同上:从 ray.tune 导入
from ray.tune import RunConfig
from ray.tune.stopper import TimeoutStopper

tuner = tune.Tuner(
    trainable, param_space=param_space,
    run_config=RunConfig(name="lr-sweep-01",
                         storage_path="/mnt/shared/ray_results",
                         failure_config=FailureConfig(max_failures=2),
                         stop=TimeoutStopper(timeout_s=2 * 3600)),  # 最多 2 小时
    tune_config=tune.TuneConfig(num_samples=200, metric="loss", mode="min"),
)
results = tuner.fit()      # 到点后未完成的 trial 被停掉，已完成的照常进 ResultGrid
```

> **关于 `time_budget_s`**：⚠️ 本书早先说"它是不是还接受未确认" —— 那个方向
> 找错了。它是 **`tune.TuneConfig` 的正式字段**（签名：
> `time_budget_s: Optional[Union[int, float, timedelta]] = None`），
> **不是 `RunConfig` 的**。所以它现在仍然可用，写法是：
>
> ```python
> tune.Tuner(trainable, param_space=..., tune_config=tune.TuneConfig(time_budget_s=3600))
> ```
>
> 内部它确实会被转换成一个 `TimeoutStopper`，所以**语义与手写
> `TimeoutStopper` 完全一致**，只是更简洁。想显式控制就用上面的写法。

### 恢复：`Tuner.restore()`

driver 挂掉、机器被抢占、或者参数写错了想改完接着跑 —— 都靠 `Tuner.restore()`：

```python
import os
from ray import tune

path = os.path.expanduser("~/ray_results/lr-sweep-01")   # = storage_path/name
assert tune.Tuner.can_restore(path)                      # 先检查再恢复

tuner = tune.Tuner.restore(
    path,
    trainable=trainable,          # 用了 with_parameters 时：重新包一遍
    resume_unfinished=True,       # 未完成的 trial 继续跑（默认）
    resume_errored=False,         # 失败的 trial 是否重跑
    restart_errored=False,        # 失败的是否从零重启（而非从 checkpoint 恢复）
)
results = tuner.fit()
```

> 早期版本用 `overwrite_trainable=` 覆盖 trainable，现在的形态是直接传
> `trainable=`（**改名发生的版本未确认**）。

⚠️ **恢复不是万能的，四个前提缺一不可**：

| 前提 | 说明 |
|---|---|
| `RunConfig(name, storage_path)` 与上次**完全一致** | 路径就是 `storage_path/name`，改名字等于换了个实验 |
| `param_space` 与上次一致 | ⚠️ **必须一致，不能借恢复改搜索空间**。`Tuner.restore()` 确实有 `param_space` 形参，但它的用途只有一个：**重建失效的 object ref**（`ray.put` 的数据集、`with_parameters` 传的对象）。内部 `_validate_param_space_on_restore` 会在**键集不一致时抛 `ValueError`** —— 官方明确写「Changing the hyperparameter search space then resuming is NOT supported」 |
| `with_parameters` 的对象要重新给 | object ref 绑在旧会话上（见 §14.13） |
| `storage_path` 是共享存储 | 多节点下 `/tmp` 是各节点各自的 |

**别用恢复来「续命」一个设计错了的实验**：`stop`、`num_samples`、`param_space`
**都不能借 `restore` 改**（`param_space` 传进去只是为了重绑 object ref，
键集对不上会直接 `ValueError`）。要改就开新实验，把旧的 `ResultGrid`
读出来做对比。
`tune.Tuner.can_restore(path)` 可以在脚本里做自动判断（判断失败就退化成新实验）。

## 14.17 采样器补全：`AxSearch`、`ConcurrencyLimiter` 与 `Repeater`

§14.4 的表里提到过 `AxSearch` / `BayesOptSearch`，这里补三件必须知道的事。

### `AxSearch` 的真实状态

| 事实 | 内容 |
|---|---|
| 是否被弃用 | **没有**（本次检索未找到任何"已弃用 / 停止维护"的声明） |
| 依赖 | `pip install ax-platform`（文档同时提到 `sqlalchemy`） |
| 搜索空间 | 支持从 `param_space` 自动转换，也接受手工 `space` |
| **不支持** | **网格搜索的转换**；**量化会被丢弃**（`qrandint` / `qloguniform` 这类只留一条 warning） |
| 保存 / 恢复 | ✅ **已实现** —— 2.58 的 `ray/tune/search/ax/ax_search.py` 里有真实的 `save(checkpoint_path)` / `restore(checkpoint_path)`（用 cloudpickle）。⚠️ 本书早先写"未实现"是错的（同样**确实未实现**的是 `TuneBOHB` / `SigOptSearch` / `DragonflySearch`） |

"量化被静默丢弃"这条最容易坑人：你以为在搜整数，实际在搜连续值。**用 `AxSearch`
前先把 `param_space` 里的 `q*` 系列换成显式列表或 `tune.choice`。**
**保存 / 恢复是实现了的**（见上表），所以 `AxSearch` 可以进需要 checkpoint 恢复的
流程（比如中途被抢占后续跑）；真正缺的是**网格搜索的转换**。

### `ConcurrencyLimiter`：贝叶斯类采样器的安全阀

⚠️ **这是一个真实的陷阱**：`OptunaSearch` / `HyperOptSearch` / `BayesOptSearch` /
`AxSearch` 这类采样器**内部维护串行状态** —— 它们要拿到上一个 trial 的结果才能
给出下一个建议。Ray 官方把它们当**顺序采样器**处理，并提供
`ConcurrencyLimiter` 作为显式包装器：

```python
from ray import tune
from ray.tune.search import ConcurrencyLimiter
from ray.tune.search.optuna import OptunaSearch

search_alg = ConcurrencyLimiter(
    OptunaSearch(metric="loss", mode="min"),
    max_concurrent=4,                     # 最多同时 4 个 trial 在跑
)
tuner = tune.Tuner(trainable, param_space=param_space,
                   tune_config=tune.TuneConfig(search_alg=search_alg, num_samples=50))
```

三条必须知道的行为：

1. **`ConcurrencyLimiter` 通过拦截 `suggest()` 实现限流**：在跑的 trial 达到
   `max_concurrent` 时返回 `None`，让调度器停下来等。
2. **它不是可选项**。不包的时候，Tune 对非 `BasicVariantGenerator` 的采样器会把
   "待处理 trial 上限"当作 **1** 处理。典型症状是：资源明明够、`num_samples`
   也设了，trial 却**一个一个串行跑**，墙上时间长得莫名其妙。
   （PR #63770 修的就是"`max_concurrent_trials` 对自定义采样器不生效"：修复前，
   一个 16 trial × 2 秒、4 worker、TPE 的场景要 60.4 秒（全程串行），修复后
   12.0 秒。修复思路是当采样器被 `ConcurrencyLimiter` 包住时，拿它的
   `max_concurrent` 当待处理上限；`Repeater` 套 `ConcurrencyLimiter` 这类多层
   包装也会被识别。）
3. **自带并发逻辑的采样器**（如 `SigOptSearch` / `HEBO`）如果被
   `ConcurrencyLimiter` 包住，**以包装器的 `max_concurrent` 为准**，随后由该
   采样器自己的内部逻辑接管（Searcher API 为此加了 `set_max_concurrency`，
   PR #20576）。

> **临时兜底**：`TUNE_MAX_PENDING_TRIALS_PG` 环境变量可以手工设待处理上限，在
> 不含 #63770 的版本上这是唯一的绕过办法。它是内部开关，别写进生产脚本。

### `Repeater`：同一组配置换多个种子重跑

```python
from ray.tune.search import ConcurrencyLimiter, Repeater
from ray.tune.search.optuna import OptunaSearch

search_alg = Repeater(
    ConcurrencyLimiter(OptunaSearch(metric="loss", mode="min"), max_concurrent=2),
    repeat=3,          # 每条建议的配置跑 3 次（不同种子）
)
```

`Repeater` 把内层采样器的每条建议**重复 N 次**，并用 `search_alg.metric` 上的
**平均值**回灌给内层采样器 —— 是为高方差训练（RL 最典型）设计的。

> ⚠️ 官方明确提示：**`Repeater` 不要和 `TrialScheduler` 一起用** —— 早停会砍掉
> 重复实验里的部分 run，平均值就不再是无偏的。要用 `Repeater` 就关掉
> `scheduler`，把预算花在"重复"上而不是"早停"上。

## 14.18 PBT 实战：什么时候它真的赢

§14.5 的表里 PBT 只有一行。它和 ASHA **不是同类东西**，值得单独看。

**ASHA 优化的是"哪组超参好"**（一组超参从头到尾不变）；**PBT 优化的是"这组超参
在训练过程中该怎么变"**。PBT 维护一个 population，定期把表现差的 trial **重启到
表现好的 trial 的 checkpoint 上**，再**扰动**它的超参继续跑：

```
iter 0    t1(0.3) t2(0.5) t3(0.9) t4(0.2)      ← 括号里是当前指标
             │        │        │        │
          每 perturbation_interval 次迭代做一次 exploit + explore
             ↓        ↓        ↓        ↓
iter N    t1(0.9) t2(0.9) t3(0.9) t4(0.9)      ← 差的 trial 被"重启"到好 trial 的
               ↑ 权重来自 t3，超参被扰动        checkpoint 上，再扰动超参
```

```python
import random
from ray import tune
from ray.tune.schedulers import PopulationBasedTraining

def explore(config):
    """custom_explore_fn：扰动后做一次合法化（PBT 不替你保证组合合法）"""
    config["train_batch_size"] = max(config["train_batch_size"],
                                     config["minibatch_size"] * 2)
    config["lr"] = min(max(config["lr"], 1e-6), 1e-2)
    return config

pbt = PopulationBasedTraining(
    time_attr="training_iteration",     # "时间"按什么量 —— 决定 interval 的单位
    perturbation_interval=10,           # 每 10 次迭代 exploit + explore 一次
    hyperparam_mutations={              # 哪些超参可以被改、怎么改
        "lr": lambda: random.uniform(1e-5, 1e-2),    # 可调用 → 连续扰动
        "minibatch_size": [128, 256, 512, 1024],     # 列表 → 从里面挑
    },
    resample_probability=0.25,          # 25% 概率重新采样，其余在旧值附近扰动
    custom_explore_fn=explore,          # 扰动后合法化（可选，但强烈建议）
)

tuner = tune.Tuner(
    MyTrainable,                        # 必须支持完整的 save / load
    param_space={"lr": 3e-4, "minibatch_size": 256},
    tune_config=tune.TuneConfig(
        scheduler=pbt, num_samples=8,   # population 大小 = num_samples
        metric="episode_return_mean", mode="max",
        reuse_actors=True,              # PBT 反复重启 trial，复用 actor 收益明显
    ),
)
```

**硬性要求（不满足就跑不起来或结果无意义）**：

| 要求 | 原因 |
|---|---|
| trainable 必须支持**完整的** save / load | PBT 要把甲 trial 的权重装进乙 trial；只存一半状态 = 静默错误 |
| `hyperparam_mutations` 里的参数要在 `param_space` 里有初值 | 只在 mutations 里出现、`param_space` 里没有的参数，初值也会从 mutations 采 |
| 只有列进 `hyperparam_mutations` 的参数会被改 | 想让它变必须写进去 |
| `perturbation_interval` 建议**等于** `checkpoint_interval`（或它的整数倍） | 否则 exploit 时拿到旧 checkpoint，"复制好配置"退化成随机 |

**它比 ASHA 贵，而且贵得理直气壮**：ASHA 靠**少跑**省钱（大部分 trial 在前 10%
就被淘汰），PBT **不早停** —— population 里每个成员都要持续跑；想让它"跑得快"
还得给足资源（官方 PBT 示例直接写"需要 ≥8 张 GPU 才能让所有 trial 并行，否则
只能轮转，效率大打折扣"）。所以：

| 场景 | 选谁 |
|---|---|
| 超参最优值**固定**、只想快点找到 | **ASHA** —— 这里用 PBT 是纯粹的浪费 |
| 超参最优值**随训练进程变化**（lr schedule、熵系数、数据课程、batch 缩放） | **PBT** |
| RL 任务、奖励高方差、需要在线适应 | **PBT**（它的主场） |
| 预算紧张、单 trial 很贵 | **ASHA** |

> **`quantile_fraction`**（被当作"好"的 trial 占比，只有这一档的 trial 有权
> 被抄袭）：`PopulationBasedTraining` 的默认值是 **0.25**
> —— 即每轮只把表现最好的 1/4 当作"老师"，其余 3/4 从它们身上抄参数再扰动。
> 调大它会让"抄袭"更宽容（多样性↑、收敛↓）；调小则更精英化。
> 仍建议按你所装版本的签名再确认一次。

## 14.19 本章小结

* Tune 的核心抽象是 **`Tuner`（要跑什么 + 怎么跑）+ `TuneConfig`（跑多少 /
  怎么判断好坏 / 怎么搜 / 怎么停）**，`tuner.fit()` 返回 `ResultGrid`。
* **`ray.tune.Tuner` 与 `TuneConfig` 在源码里都标注 `stability="beta"`**；
  `ray.tune.run` 仍在但已不是推荐路径（检索未找到明确的弃用声明）。
  Tune 训练函数里请用 `ray.tune.*` 的 `report` / `get_checkpoint` /
  `get_context`，`Checkpoint` 也要从 `ray.tune` 导入。
  ⚠️ 而且 **`ray.tune.Checkpoint` 的能力集与 `ray.train.Checkpoint` 完全一致**
  （它只是后者的 pass-through 子类）：**只有 `from_directory(local_dir)` 与
  `Checkpoint(path=...)` 两个造法** —— `from_dict` / `to_dict` / `from_uri`
  在 2.58 已移除，调用会抛 `AttributeError`（详见 §14.6 与第 13 章 §13.3）。
  小字典本来就该走 `tune.report` 的 metrics，不要塞进 checkpoint。
* 搜索空间里 **`randint` 上界是开区间**、**跨数量级的参数必须用
  `loguniform`**；`grid_search` 会展开成笛卡尔积，用前先算 trial 总数。
  搜索空间的**维度**比搜索算法更重要。
* 采样器：`BasicVariantGenerator`（随机，默认）→ `OptunaSearch`（TPE，
  中等维度首选）→ `AxSearch`/`BayesOptSearch`（低维、评估极贵）。贝叶斯类
  方法需要**串行或半串行**，`max_concurrent_trials` 开太大会稀释效果。
* 调度器：**ASHA 是默认首选**（异步逐次减半，无同步 barrier）；HyperBand
  同步、怕 straggler；PBT 用于调训练**过程**中的超参；MedianStoppingRule
  最保守。`grace_period` 设太小会误杀慢热配置。
* 成本公式：**总 GPU·小时 ≈ `num_samples` × 每 trial GPU 数 × 每 trial 小时**；
  `max_concurrent_trials` 只影响墙钟时间、**不影响总成本**，且应等于
  `floor(集群总 GPU / 每 trial GPU)`（设大了会让 trial 卡在 PENDING）。
* 标准流程：**smoke run → 定预算 → 粗搜（宽空间、短预算、激进早停）→
  精搜（窄空间、长预算）→ 用没参与搜索的验证集复核 top-3**。Tune 的调度器在
  Core 层就是"读指标 → 排序 → `ray.cancel`"，没有分布式魔法；mini-ray 提供了
  这些原语，但**没有**实现 Tune 本身。
* **`Stopper` 与调度器是两条独立的早停路径**：调度器跨 trial 比较，`Stopper`
  单 trial / 全实验判定。`stop=` 的字典简写是"指标名 → 阈值"且**假定越大越好**；
  `TimeoutStopper` 停的是**整个实验**。
* **`tune.Callback` 挂在 `RunConfig(callbacks=[...])` 上**（不是 `TuneConfig`），
  是拿到早停通知（`on_trial_complete`）与做自定义日志的唯一入口；回调在 driver
  里串行执行，别放重活。**模型产物走 `Checkpoint`，不要塞进 metrics。**
* **`with_resources` 绑资源、`with_parameters` 把大对象放进 object store**
  （只传一次但占内存）。⚠️ **恢复实验时 `Tuner.restore()` 没有
  `with_parameters=` 这个形参** —— 正确做法是把**重新包好的 trainable** 传进去：
  `tune.Tuner.restore(path, trainable=tune.with_parameters(fn, **same_kwargs))`
  （见 §14.13）。checkpoint 默认**全留**，用
  `CheckpointConfig(num_to_keep=N, checkpoint_score_attribute=...,
  checkpoint_score_order=...)` 控制。
* **`reuse_actors=True` 能省掉每个 trial 的启动成本**，代价是状态串味 —— 类式
  `Trainable` 里每个 trial 独有的状态必须在 `setup()` 里重建。恢复实验靠
  `Tuner.restore(path, trainable=...)`，前提是 `RunConfig(name, storage_path)`
  与 `param_space` 和上次完全一致。
* **贝叶斯类采样器（Optuna / HyperOpt / BayesOpt / Ax）不并发安全**：用
  `ConcurrencyLimiter(searcher, max_concurrent=N)` 包住，否则会退化成逐个串行
  跑（PR #63770 修的就是这件事）。`AxSearch` 还会**静默丢弃量化**、**不支持
  网格搜索转换** —— 但它的**保存 / 恢复是实现了的**（`save` / `restore` 走
  cloudpickle），能配合 checkpoint 恢复的流程。`Repeater` 用于换种子重复评估，
  但**不能和调度器同用**。
* **PBT 是"调训练过程中的超参"，不是早停**：需要 population 全体跑满、
  trainable 完整支持 save/load、`hyperparam_mutations` 与 `perturbation_interval`
  配好。它比 ASHA **贵**，只在"最优超参随训练进程变化"时才是正确选择。

下一章进 Ray Serve：把模型变成在线服务，以及它和 Ray Data LLM 的分工。
