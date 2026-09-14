仓库地址：https://github.com/hhk-png/cycle-agent

# 第 32 章：实验追踪与 MLOps 集成

> 本章目标：回答一个前 31 章都没正面回答的问题 ——
> **训练/调参跑出来的那些数字和产物，最后去哪儿了？**
>
> 第 13 章讲了 Ray Train 怎么训、第 14 章讲了 Tune 怎么搜、
> 第 11 章讲了 Ray 自己的指标怎么接 Prometheus。
> 但**实验数据**（每个 trial 的 loss 曲线、超参组合、产出的模型权重）
> 的归宿，全书到这里一直是空的。
>
> 这一章补上它。所有 Ray 侧 API 都以 **2.58** 为准；
> 凡本书未能在官方文档/源码里核对到的，一律写"**未确认**"。

---

## 32.1 先把三个概念分开

「MLOps」这个词被用得太宽，落到 Ray 上其实是三件**互不相干**的事。
混在一起谈，就会出现"我到底该用哪个"的困惑。

| 层 | 回答的问题 | Ray 里的归属 | 本书的哪一章 |
|---|---|---|---|
| **指标与实验追踪** | 第 37 个 trial 的 loss 是多少？用的是什么学习率？ | Tune/Train 的 **Callback / Logger** | **本章** |
| **产物与模型管理** | 训出来的权重放哪、谁在用、怎么回滚？ | `Checkpoint` + 外部 registry | 本章 §32.5 |
| **服务与部署** | 模型怎么变成一个能扛流量的端点？ | Ray Serve | 第 15 章 |

> ⚠️ **一个常见误解**：以为 `ray.util.metrics`（第 11 章 §11.5）能当实验追踪用。
> **不能**。它是 **Prometheus 指标** —— 为**运维**服务（告警、看板、SLO），
> 标签基数被刻意压制，数据是**聚合后的时间序列**，且**不保留实验结构**
> （哪个 trial 属于哪个 experiment、超参是什么，它都不管）。
> 实验追踪需要的是**逐次试验的结构化记录**，那是 Callback 的职责。

---

## 32.2 Ray Train / Tune 的两个数据出口

Ray Train 和 Ray Tune 看似两套 API，但在**数据出口**上是同一套：
`RunConfig(callbacks=[...])`。这是本章唯一必须记住的一行。

```python
from ray.tune import RunConfig          # ⚠️ 给 Tuner 用 ray.tune
# from ray.train import RunConfig       # 给 Trainer 用 ray.train
# —— 同名不同类,见第 14 章 §14.1 的三件套警告
```

`Callback` 机制本身在第 14 章 §14.12 已经讲透（钩子清单、执行在 driver 串行、
"被早停"走的是 `on_trial_complete`）。**本章只讲"往哪儿送"**。

### 出口一：Tune 自带的三种 Logger

Tune **默认就会**把结果落盘，不需要你写任何代码：

| Logger | 产物 | 什么时候用 |
|---|---|---|
| `TBXLoggerCallback` | TensorBoard event 文件 | 想看 loss 曲线、要对比多个 trial |
| `CSVLoggerCallback` | 每个 trial 一个 `progress.csv` | 要拿去做自己的分析（pandas 直接读） |
| `JsonLoggerCallback` | `result.json` | 要程序化消费、嵌套结构更友好 |

这三个**本身就是用 `Callback` 实现的**（第 14 章 §14.12 提过）。
所以有一个开关能整体关掉它们：

```bash
TUNE_DISABLE_AUTO_CALLBACK_LOGGERS=1     # 我不想要 CSV/JSON,只要自己的
```

> ⚠️ **TensorBoard 的目录在哪**：不是 `storage_path` 根目录，而是
> `<storage_path>/<experiment_name>/`。用 `tensorboard --logdir <storage_path>`
> **能把多个实验的曲线叠在一张图上**（这是它「能跨实验」的那一面）。
> **如果你只指到某个实验目录，就看不到这个叠加** ——
> 这是"TensorBoard 是空的"最常见的两个原因之一（另一个是 `storage_path`
> 指向了各节点各自的 `/tmp`，见 §32.7 坑 1）。
>
> ⚠️ **口径澄清（与 §32.4 的表格一致）**：TensorBoard **能叠曲线**，
> 但**不能做超参对比** —— 它没有"按 `lr` 分组、按 `batch_size` 筛选"这类
> 超参感知的 UI，也不能记录「这次实验用的什么配置」。
> §32.4 表里写的"无跨实验对比 UI"指的是**后者**。两处说的是同一件事，
> 这里是精确版本：**叠图 ≠ 超参对比**。

### 出口二：外部追踪系统

这才是"实验追踪"通常指的东西 —— 把每次 trial 的**超参 + 指标 + 产物**
送进一个有 UI、能跨实验对比、能分享给同事的系统。

Ray 提供了对主流系统的官方集成，**它们全都是 Callback**：

```python
from ray.tune import RunConfig

# ── MLflow ──
from ray.air.integrations.mlflow import MLflowLoggerCallback

run_config = RunConfig(
    name="lr-sweep-01",
    storage_path="/mnt/shared/ray_results",
    callbacks=[
        MLflowLoggerCallback(
            tracking_uri="http://mlflow.internal:5000",
            experiment_name="bert-finetune",
            save_artifact=True,        # 把 checkpoint 也作为 MLflow artifact 上传
            tags={"team": "nlp", "task": "cls"},
        ),
    ],
)
```

```python
# ── Weights & Biases ──
from ray.air.integrations.wandb import WandbLoggerCallback

run_config = RunConfig(
    callbacks=[
        WandbLoggerCallback(
            project="bert-finetune",
            api_key_file="~/.wandb_key",    # 或用 WANDB_API_KEY 环境变量
            upload_checkpoints=True,
        ),
    ],
)
```

其他还有 `ray.air.integrations.comet.CometLoggerCallback` 等
（**完整清单未确认** —— 以你所用版本的 `ray.air.integrations` 命名空间为准）。

> ⚠️ **三件必须知道的事**：
>
> 1. **导入路径是 `ray.air.integrations.*`，不是 `ray.tune.integration.*`。**
>    `ray.tune.integration` 下也有一批老路径的转发模块，混用会拿到弃用警告。
>    另外 Train × Tune 的桥接回调 `TuneReportCallback` 确实在
>    `ray.tune.integration.ray_train`（见第 13 章 §13.8），**这是个例外，别类推**。
> 2. **这些包不在 `ray` 里。** 用 MLflow 集成要自己 `pip install mlflow`，
>    W&B 要 `pip install wandb`。Ray 只提供胶水代码。
>    **在集群上跑时，这些包必须通过 `runtime_env` 送到每个节点**
>    （见附录 F §F.2）—— 这是"本地跑得好好的、上集群就 `ModuleNotFoundError`"
>    的标准原因。
> 3. **API key 不要硬编码。** 用环境变量或 `api_key_file`，
>    并且**通过 `runtime_env.env_vars` 注入**而不是写进代码
>    （第 17 章 §17.5 讲的安全原则在这里同样适用 —— 你的实验代码会进
>    对象存储和日志）。

---

## 32.3 Ray Train 侧的追踪：`ray.train.report` 送什么

Train 和 Tune 的区别在于：Tune 的 trial 是 Tune 自己起的，
而 Train 的 worker 是 **Train 自己管的进程**。所以 Train 侧的指标
要先 `report` 上来，才能被 Callback 看到。

```python
import ray.train
from ray.train import ScalingConfig, RunConfig

def train_loop_per_worker(config):
    ...
    for epoch in range(10):
        loss = train_one_epoch(...)
        # ① 指标上报:每个 rank 都会 report,但只有 rank 0 的进 metrics
        ray.train.report(
            metrics={"loss": loss, "lr": current_lr},
            checkpoint=ray.train.Checkpoint.from_directory(ckpt_dir),
        )
```

### ⚠️ 谁负责写、谁负责读：rank-0 语义

这是 Rail Train 侧最容易踩的一个坑，值得单列：

| 动作 | 谁执行 | 说明 |
|---|---|---|
| `ray.train.report()` | **每个** rank 都调用 | 这是集合操作，必须所有 rank 都到 |
| 指标进 `metrics` | **只有 rank 0 的** | 其他 rank 的值被丢弃 |
| `Checkpoint.from_directory()` | 通常只有 rank 0 写盘 | 其他 rank 要么不写，要么写各自的副本 |
| `RunConfig(callbacks=[...])` | **driver 进程** | 不在 worker 里跑 |

**实践含义**：
* 你想记录的 per-rank 指标（比如每张卡的吞吐），**得先自己聚合**
  （`torch.distributed.all_reduce`）再 report，否则只有 rank 0 的数字被记录；
* 分布式 checkpoint 要按框架的约定写（DDP 下通常 rank 0 写、
  FSDP/DeepSpeed 用各自的 sharded API），**不要每个 rank 都往同一个目录写** ——
  那是文件损坏的经典成因。

> **和 mini-ray 的关系**：mini-ray 没有 Train，所以这一层它完全不涉及。
> 但它的 `state.list_tasks()` / `state.list_actors()` 提供了**同类问题的
> 低配答案**：当你的实验规模小到"看表格就够了"时，
> 一个 `summarize_tasks()` 往往比接一套追踪系统更快
> （第 11 章 §11.10、第 22 章 B.3）。

### 自定义 Callback：当官方集成不够用时

官方集成覆盖不到的情况（自研平台、内部指标系统），
自己写一个十几行的 `Callback` 就够了：

```python
from ray.tune import Callback
import requests

class MyPlatformCallback(Callback):
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def on_trial_result(self, iteration, trials, trial, result, **info):
        # ⚠️ 这个钩子触发极其频繁 —— 重活必须丢队列或攒批(第 14 章 §14.12)
        requests.post(self.endpoint, json={
            "trial": trial.trial_id,
            "config": trial.config,
            "metrics": result,
        }, timeout=1)

    def on_trial_complete(self, iteration, trials, trial, **info):
        # 正常跑完 / 被调度器砍掉 / 被 Stopper 停掉,走的都是这里
        print(f"[done] {trial.trial_id}")
```

> ⚠️ **`on_trial_result` 里做同步 HTTP 会拖慢整个实验**。
> 回调在 **driver 进程里串行执行**（第 14 章 §14.12 明确讲过），
> 一个 100ms 的请求 × 每个 trial 每轮 = 实打实的墙钟损失。
> 正确做法是丢进一个 `queue.Queue`，由后台线程批量发送；
> 或者用 `ray.util.queue.Queue` —— 那是 Ray 自己的分布式队列，
> 接口与标准库 `queue.Queue` 对齐。
> ⚠️ **别把两个出处搞混**：真实 Ray 的 `ray.util.queue.Queue`（标为"现行可用"）
> 在**附录 A §A.10** 的 API 状态表里；**第 22 章 B.9** 是 **mini-ray** 的
> `miniray/util/queue.py` 走读 —— 那里讲的是教学实现，不是真实 Ray 的用法。

---

## 32.4 追踪系统选型：一张表说清

| 系统 | 部署形态 | 强项 | 弱项 | 什么时候选它 |
|---|---|---|---|---|
| **TensorBoard** | 本地/单机 | 零依赖、Tune 自带 | 无超参管理、无跨实验对比 UI、多人协作差 | 个人调试、快速看曲线 |
| **MLflow** | 自建 server 或托管 | 开源、可自建、模型 registry 完整 | UI 体验一般、大规模元数据要调数据库 | 要**完全自控**、有合规要求 |
| **Weights & Biases** | SaaS（可私有部署） | UI/协作最好、报告功能强 | 数据出网（SaaS 形态）、收费 | 团队协作、要给人看报告 |
| **CSV / JSON** | 文件 | 无依赖、pandas 直接读 | 没有 UI、没有对比能力 | 一次性实验、CI 里做回归 |

**给一个可能不太受欢迎但实用的建议**：
**先用 Tune 自带的 TensorBoard + CSV，真的痛了再上系统。**
"痛"的信号很具体：① 你要在多个实验之间比同一个指标；
② 你要把结果发给不跑代码的人看；③ 你要复现三个月前某次实验的配置。
三个里中一个，就值得上 MLflow 或 W&B；一个都不中，接进去只是增加运维面。

---

## 32.5 产物管理：`Checkpoint` 之后的事

追踪系统记的是**数字**，模型权重是**产物**。这两件事在 Ray 里是分开的。

### `Checkpoint` 的三种落地方式

```python
import ray.train

ckpt = ray.train.Checkpoint.from_directory("/tmp/ckpt")   # 从目录建

# ① 拿到本地目录(会用就删,注意生命周期)
with ckpt.as_directory() as path:
    model = load_model(path)

# ② 拷到自己的位置(跨节点安全)
ckpt.to_directory("/mnt/models/bert-v3")

# ③ 给追踪系统当 artifact
#    MLflowLoggerCallback(save_artifact=True) / WandbLoggerCallback(upload_checkpoints=True)
```

> ⚠️ **`as_directory()` 是一个上下文管理器，不是普通函数。** 它返回的目录
> 在 `with` 块结束后**可能被清理**（尤其是从云存储拉下来的 checkpoint）。
> 出了 `with` 再持有那个路径 = 拿一个已经被删的目录。
> 要长期持有，用 `to_directory()` 拷出来。

### 与外部模型 registry 对齐

Ray **不做模型 registry**——它负责"把 checkpoint 搬运到该在的地方"，
"谁是这个模型的生产版本"是**你的事**（或 MLflow Model Registry / 内部平台的事）。

一个能落地的约定：

| 阶段 | 落在哪 | 谁写 | 谁读 |
|---|---|---|---|
| trial 中间产物 | `storage_path/<exp>/<trial>/checkpoint_*` | Tune 自动 | 只有 Tune |
| 选中的最佳模型 | 共享存储固定路径 / registry | **你的脚本**（`to_directory`） | 推理服务 |
| 上线版本 | registry 里的 alias / stage | **人或 CI**（带回滚） | 部署流水线 |

**关键是把第二步显式化**。见过太多次"最好的那个 checkpoint 只在
Tune 的 trial 目录里，而那个目录按 `num_to_keep` 已经被删了"
（第 14 章 §14.14 讲保留策略）——**用 `CheckpointConfig` 控制保留，
但别把"生产模型"的命运交给它**。

```python
# 在 Callback 里做「选出最佳 + 固化」这一步
from ray.tune import Callback

class PromoteBest(Callback):
    def __init__(self, dest, metric="loss"):
        self.dest, self.metric = dest, metric
        self.best = None

    def on_trial_complete(self, iteration, trials, trial, **info):
        score = trial.last_result.get(self.metric)
        if score is None or (self.best and score >= self.best[0]):
            return
        self.best = (score, trial)
        ckpt = trial.checkpoint                  # 拿到这个 trial 的 checkpoint
        if ckpt:
            ckpt.to_directory(f"{self.dest}/candidate")
            print(f"[promote] trial={trial.trial_id} {self.metric}={score}")
```

> ⚠️ **这段是骨架不是成品**：真实场景要考虑「多 trial 并发完成时的竞态」
> （两个 trial 同时判定自己更优）、「被早停的 trial 的 `last_result` 语义」
> （第 14 章 §14.12：被砍的 trial 也走 `on_trial_complete` 且 `error is None`）、
> 以及「写完一半崩了怎么办」。**最稳的做法是让 Callback 只记录候选，
> 由实验结束后的一个独立步骤做固化** —— 幂等、可重跑、不会因为
> driver 崩溃留下半个生产模型。

---

## 32.6 一个完整的、可对照的配置

把前面所有东西拼起来。**这是本章唯一一段你该抄走的代码**：

```python
import os

import ray
from ray import tune
from ray.tune import RunConfig, CheckpointConfig, FailureConfig
from ray.train import ScalingConfig
from ray.train.torch import TorchTrainer
from ray.air.integrations.mlflow import MLflowLoggerCallback
# ⚠️ 注意这里**没有** TuneReportCallback —— 见下面的说明

def train_loop_per_worker(config):
    import ray.train
    for epoch in range(config["epochs"]):
        loss = train_one_epoch(config["lr"], config["batch_size"])
        ray.train.report(metrics={"loss": loss})

def train_fn(config):
    trainer = TorchTrainer(
        train_loop_per_worker,
        train_loop_config=config,                        # ← 超参从这进
        scaling_config=ScalingConfig(num_workers=4, use_gpu=True),
    )
    trainer.fit()

ray.init()

tuner = tune.Tuner(
    train_fn,                                            # ← driver function,不是 Trainer 实例
    param_space={
        "lr": tune.loguniform(1e-5, 1e-3),
        "batch_size": tune.choice([16, 32, 64]),
        "epochs": 10,
    },
    run_config=RunConfig(
        name="bert-sweep",
        storage_path="/mnt/shared/ray_results",          # ⚠️ 必须全节点可访问
        checkpoint_config=CheckpointConfig(
            num_to_keep=3,
            checkpoint_score_attribute="loss",
            checkpoint_score_order="min",
        ),
        failure_config=FailureConfig(max_failures=2),    # ⚠️ 失败策略走这里,不是 tune.run(fail_fast=...)
        callbacks=[
            MLflowLoggerCallback(
                tracking_uri=os.environ["MLFLOW_TRACKING_URI"],
                experiment_name="bert-sweep",
                save_artifact=True,
            ),
        ],
    ),
)

results = tuner.fit()
best = results.get_best_result(metric="loss", mode="min")
print(best.metrics, best.checkpoint)
```

**这段代码里每一处 ⚠️ 都是真实踩过的坑**，逐条对应：

| ⚠️ | 对应章节 | 为什么 |
|---|---|---|
| `RunConfig` 从 `ray.tune` 导入 | 第 14 章 §14.1 | 三件套同名不同类，传错吃弃用警告 |
| **没有** `TuneReportCallback` | 第 13 章 §13.8 | 👇 见下面的专门说明 |
| `train_loop_config=config` | 第 13 章 §13.8 | 超参**只通过这一个口子**进 worker，不要用闭包捕获 |
| `storage_path` 必须共享 | 第 14 章 §14.16 | 多节点下 `/tmp` 是各节点各自的 |
| `FailureConfig` 而非顶层 `fail_fast` | 第 13 章 §13.2 | 删掉的是 **`tune.run(fail_fast=...)` 这个顶层参数**。`FailureConfig` **有** `fail_fast` 字段 —— 想快速失败就写 `FailureConfig(fail_fast=True)`（此时 `max_failures` 必须为 0） |
| MLflow 的包要进 `runtime_env` | 附录 F §F.2 | 只在 driver 装的包，worker 上没有 |

### 为什么这段代码里没有 `TuneReportCallback`

这是很容易抄错的一处，单独说清楚：

| 你的写法 | 要不要 `TuneReportCallback` |
|---|---|
| **driver function**：`tune.Tuner(train_fn, ...)`，`train_fn` 内部自己 `TorchTrainer(...).fit()`（**本节这种**） | **不要**。指标由 `ray.train.report()` 上报，Tune 从 Train 的运行结果里直接读得到 |
| V1 老写法：`Trainer(trainable=..., ...)`，把 Trainer **实例**交给 Tuner | 要。它负责把 Train 的指标桥接给 Tune（路径是 `ray.tune.integration.ray_train`，**不在** `ray.air.integrations` 下） |

> ⚠️ 本书早先的版本在这段示例里 import 了 `TuneReportCallback` 却从未使用 ——
> 那是**把 V1 的桥接回调误加到了 V2 的 driver function 写法上**。
> 判断方法很简单：**看 Tuner 的第一个参数是函数还是 Trainer 实例**。
> 是函数 → 不需要；是实例 → 需要。

---

## 32.7 坑清单

**坑 1：`storage_path` 指向了本地盘。**
多节点下每个节点的 `/tmp/ray_results` 是不同的目录。
症状是"TensorBoard 上只有一部分 trial"或"恢复实验时说找不到"。
**修**：共享存储（NFS / 云盘 / S3 类）。

**坑 2：追踪系统的包没进 `runtime_env`。**
本地起 Tune 能跑（driver 进程有包），一上集群就 `ModuleNotFoundError` ——
因为 **worker 是另外的进程、另外的机器**。
**修**：`ray.init(runtime_env={"pip": ["mlflow"]})` 或写进
`RunConfig` 的 `runtime_env`（附录 F §F.2）。

**坑 3：API key 进了代码。**
实验代码会被序列化进对象存储、写进日志、进 traceback。
**修**：环境变量 + `runtime_env.env_vars`，且**别把 key 打进任何
会被 Tune 记录的地方**（第 17 章 §17.5）。

**坑 4：以为 `on_trial_result` 里可以随便做重活。**
回调在 driver 串行执行。同步 HTTP / 写数据库会**直接拖慢整个实验**。
**修**：攒批 + 后台线程，或丢队列（§32.3）。

**坑 5：把实验追踪和 Prometheus 指标混为一谈。**
见 §32.1。两者受众、基数、保留策略完全不同。
**修**：运维看 Prometheus（第 11 章 §11.5），研究者看 MLflow/W&B（本章）。

**坑 6：每个 rank 都往同一个 checkpoint 目录写。**
文件损坏的经典成因。
**修**：按框架约定（DDP 通常 rank 0；FSDP/DeepSpeed 用 sharded API），§32.3。

**坑 7：`as_directory()` 出了 `with` 还在用。**
**修**：要长期持有就 `to_directory()` 拷贝（§32.5）。

**坑 8：把生产模型的生命周期交给 `CheckpointConfig`。**
`num_to_keep` 会在你没注意的时候删掉那个"最好的"checkpoint。
**修**：选中的模型显式固化到独立位置（§32.5）。

---

## 32.8 与 mini-ray 的关系

**mini-ray 不实现这一层，而且是明确不做。**

原因不必绕弯子：实验追踪的价值**几乎全部来自外部系统**
（UI、跨实验查询、团队协作、长期保留）——
Ray 在里面提供的是**胶水**，而胶水没有"内部机制"值得从零写一遍。
写一个 `Counter` 能让你理解指标怎么聚合（第 11 章 §11.5）；
写一个 `Callback` 转发器只会让你重新实现 `requests.post`。

**但要区分「mini-ray 不做」和「教程不讲」**（第 0 章的约定）：
这一章讲的所有东西，在真实 Ray 上都是**生产必做项**，
只是它属于"生态集成"而不是"分布式机制"。

不过 mini-ray 里有**两个直接可用的小工具**，在实验规模小的时候够用：

```python
import miniray as ray
from miniray import state
from miniray.util.metrics import Counter, Histogram

loss_hist = Histogram("train_loss", boundaries=[0.1, 0.5, 1.0, 2.0, 5.0])

@ray.remote(num_gpus=1)
def train_trial(lr):
    for epoch in range(10):
        loss = step(lr)
        loss_hist.observe(loss)              # 进程内记录
    return {"lr": lr, "loss": loss}

results = ray.get([train_trial.remote(lr) for lr in [1e-3, 1e-4, 1e-5]])
print(sorted(results, key=lambda r: r["loss"])[0])   # 手写「选最佳」
print(state.summarize_tasks())                        # 看每个 trial 的耗时/重试
print(loss_hist.snapshot())                           # 看分布与分位数
```

这就是「**没有追踪系统时的最小可用形态**」：
返回值当结果、`state` 当监视、`metrics` 当曲线。
它的天花板很明显（没有持久化、没有 UI、跨进程要自己聚合），
但**它足够让你在接 MLflow 之前先把实验跑通** ——
而"先跑通再上工具"通常比反过来省时间。

---

## 32.9 本章小结

* **三个概念分开看**：实验追踪（Callback/Logger）、产物管理（Checkpoint + registry）、
  服务部署（Serve）。本章管前两个。
* **一个入口**：`RunConfig(callbacks=[...])`，Train 和 Tune 通用。
* **Tune 自带 TensorBoard/CSV/JSON**，默认就开；
  `TUNE_DISABLE_AUTO_CALLBACK_LOGGERS=1` 关掉。
* **官方集成是 `ray.air.integrations.*`**（MLflow / W&B / Comet），
  但 `TuneReportCallback` 例外地来自 `ray.tune.integration.ray_train`。
* **追踪系统的包必须进 `runtime_env`**，否则本地能跑、集群报
  `ModuleNotFoundError`。
* **Train 侧是 rank-0 语义**：所有 rank 都 `report`，只有 rank 0 的指标被记录。
* **`Checkpoint` 负责搬运，不负责 registry** ——
  "把最佳模型固化到固定位置"这一步必须显式写，别交给 `num_to_keep`。
* **别用 `ray.util.metrics` 当实验追踪**（那是 Prometheus，为运维服务）；
  也别在 `on_trial_result` 里做重活（回调在 driver 串行）。
* **选型建议**：先用 TensorBoard + CSV，等到"要跨实验对比 / 要给不跑代码的人看 /
  要复现三个月前的配置"这三个信号出现其一，再上 MLflow 或 W&B。

---

> **本章之后还剩 6 章**：
> [第 33 章](ray教程-33-RayCLI全集与交互式开发.md)（Ray CLI 全集与交互式开发）、
> [第 34 章](ray教程-34-Ray与表格数据传统ML.md)（表格数据与传统 ML：XGBoost / LightGBM / scikit-learn）、
> [第 35 章](ray教程-35-数据版本产物血缘与模型注册.md)（数据版本、产物血缘与模型注册 ——
> 接着本章"registry 是你的事"往下讲）、
> [第 36 章](ray教程-36-分布式追踪与OpenTelemetry.md)（分布式追踪与 OTel，
> 补上第 11 章四层观测模型里唯一空着的那条腿）、
> [第 37 章](ray教程-37-LLM推理引擎与性能优化.md)（LLM 推理引擎与性能优化，
> 补上第 15 章刻意没下探的引擎层）、
> [第 38 章](ray教程-38-Ray与Agent工作负载.md)（Ray 与 Agent 工作负载 ——
> 把前面所有层重新组合成一条负载主线）、
> [第 39 章](ray教程-39-Ray源码阅读与事实核查指南.md)（Ray 源码阅读与事实核查指南 ——
> **全书的收尾章**，教你怎么核实前面这些章节里的每一个结论）。
>
> 到这里，教程的"用起来"部分已经完整 ——
> 从 Core 机制（02–11）、AI 库（12–16）、生产与安全（17–19）、
> 现状与未来（20），到参考层（21–28）与纵深（26、29–38），
> 再加上本章的 MLOps 收口与第 39 章的核查方法。
> 如果你是从第 0 章一路读下来的，现在可以回到
> [第 0 章 §0.8 的 7 天计划](ray教程-00-前言与导读.md) 检查一下自己的进度，
> 或者直接去 [附录 E 的端到端案例](ray教程-25-附录E-端到端实战案例.md)
> 把这一路学到的东西串一遍。
