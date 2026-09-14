仓库地址：https://github.com/hhk-png/cycle-agent

# 第 13 章：Ray Train 与分布式训练

> 本章目标：搞清楚 Ray Train 在分布式训练里**到底负责哪一段**、
> V2 默认开启之后写法变了什么、容错和抢占怎么工作，
> 以及**什么时候该直接用 torchrun 而不是 Ray**。
> 读完你应该能回答：Ray 是"训练框架"还是"进程编排器"？

---

## 13.1 定位：Ray Train 封装了什么

先把话说清楚，因为这里最容易产生误解：

> **Ray Train 不是训练框架，是训练的"声明式启动器 + 进程编排 + 容错外壳"。**

它不实现反向传播、不实现集合通信、不实现优化器。分工是：

| 层 | 谁负责 |
|---|---|
| 模型、损失、优化器、训练循环 | **你**（PyTorch / JAX / Lightning / HF） |
| 梯度同步、集合通信 | NCCL / `torch.distributed` |
| 并行策略（DDP / FSDP / DeepSpeed / Megatron） | PyTorch / DeepSpeed / Megatron-LM |
| **进程编排、资源放置、rank 分配** | **Ray Train** |
| **checkpoint 上报、失败重试、抢占处理** | **Ray Train** |

这张表就是本章的主线，加粗的两行是 Ray Train 的全部价值。

### 声明式是什么意思

用 torchrun，你要自己算 `WORLD_SIZE`、`RANK`、`MASTER_ADDR`，自己决定在哪台
机器上起几个进程。用 Ray Train，你写的是声明：
`ScalingConfig(num_workers=8, use_gpu=True)` —— 意思是"给我 8 个 worker，
每个 1 张 GPU"。至于这 8 个 worker 落在哪、怎么起进程、怎么注入环境变量、
挂了怎么拉起来，都是 Ray 的事。

**代价**：你对进程和拓扑的控制变弱了。比如"节点内 8 卡 NVLink 全连、跨节点
走 IB"这种拓扑敏感的手工排布，Ray Train 只给了粗粒度开关
（`ScalingConfig(placement_strategy="PACK"/"SPREAD")`，见 13.3）；要精确控制
就得回到 Ray Core 的放置组（`placement_group` +
`PlacementGroupSchedulingStrategy`）自己摆 —— 那是第 08 章的调度原语，不是
Ray Train 的 API。

> ⚠️ **别把"放置组"和"给 bundle 打标签"混为一谈**：第 08 章讲的是放置组本身
> （bundle、`STRICT_PACK`、`placement_group_bundle_index`），**没有**涉及
> bundle 级别的标签（`bundle_label`）用法 —— 本教程不展开它。用之前先查当前
> 版本 Ray Core 文档里 `scheduling_strategy` / bundle label 的说明。
> **已核实：`ScalingConfig` 里没有能指定 bundle 的参数** ——
> 2.58 的完整字段清单见 §13.3 的那张表（`num_workers` / `use_gpu` /
> `resources_per_worker` / `placement_strategy` / `accelerator_type` /
> `use_tpu` / `topology` / `label_selector` / `elastic_resize_monitor_interval_s`，
> 外加已弃用的 `trainer_resources`），**没有任何 bundle / placement group 入口**。
> Ray Train 的放置控制到 `placement_strategy`（`PACK` / `SPREAD`）这一层为止。

## 13.2 Ray Train V2：默认开启与迁移

* **Ray Train V2 自 2.51.0 起默认开启**（PR #57857，把 `is_v2_enabled()` 的
  默认值从 `False` 翻成 `True`）；在此之前（2.43–2.50）是 opt-in：
  `RAY_TRAIN_V2_ENABLED=1`。
* **回退到 V1 要显式设 `RAY_TRAIN_V2_ENABLED=0`**，但 V1 API 已归档到
  deprecated 目录，回退是过渡手段而不是长期方案。
* ⚠️ **官方文档有部分页面措辞过时**，仍写着"需设 `RAY_TRAIN_V2_ENABLED=1`"。
  以 release notes 与源码为准 —— 版本切换期留下没改干净的段落，在 Ray 的
  文档里不是孤例。

### V1 与 V2 的写法差异

| | V1（≤2.50 默认） | V2（≥2.51 默认） |
|---|---|---|
| 配置类位置 | `ray.air.config` 的 `RunConfig` / `ScalingConfig` | `ray.train` 的 `RunConfig` / `ScalingConfig` |
| 训练循环里取 rank | `session.get_world_rank()` | `ray.train.get_context().get_world_rank()` |
| 上报指标 | `session.report(...)` | `ray.train.report(...)` |
| 与 Tune 的关系 | Tune 跑 Trainer 实例 | Tune 跑 **driver function**（见 13.9） |
| 混用 | V1 Trainer 配 V2 config 会报错 | 反向同样被禁止（PR #57570 显式拦截） |

V2 新增的配置类与 API（2.58 一手清单）：

* `LoggingConfig` —— 结构化日志配置。2.58 里它**只有一个字段**：
  `log_level: str = "INFO"`（`ray/train/v2/api/config.py`）；
* `ValidationConfig` —— 训练中途的验证/评估钩子
  （源码里 `TrainController` 会据此建一个 `ValidationManager`
  并注册成 report/controller/worker 回调）；
* `PreemptionError` / `get_preemption_info()` —— 抢占处理（见 13.6）。

**迁移建议**：如果你的代码库是 2.45 之前写的，先做的不是改 API，
而是**确认自己有没有依赖 `session` 命名空间**。那是 V1 的核心特征。

还要确认的第二件事：代码里实例化的是哪个 `XxxTrainer`。**框架专属 Trainer
里有三个已经被删掉了**（`LightningTrainer` / `TransformersTrainer` /
`AccelerateTrainer`，2.9 起移除），而 `XGBoostTrainer` / `LightGBMTrainer`
仍在且已 V2 化。V2 的推荐路径是 `TorchTrainer` + 框架自己的胶水 ——
见 13.3 的「已移除的 Trainer 在 V2 下怎么办」。

## 13.3 现代写法：`ScalingConfig` + `TorchTrainer`

### 先澄清三个高频困惑

**① 没有 `ray.train.Trainer` 这个类。**
`ray.train` 的 `__all__` 里**不存在**通用的 `Trainer` —— 你要用的是
**后端专属**的 `XxxTrainer`：

```python
from ray.train.torch import TorchTrainer        # 最常用
from ray.train.xgboost import XGBoostTrainer
from ray.train.lightgbm import LightGBMTrainer  # 表格数据另一条路
```

> 🔴 **别再抄 `LightningTrainer` / `TransformersTrainer` / `AccelerateTrainer`
> —— 它们已经从 Ray 里删掉了。**
> 生命周期是：**2.7 标记弃用 → 2.8 调用即报错 → 2.9 移除**
> （REP *"Unify Torch based Trainers on the `TorchTrainer` API"*）。
> 本教程面向 2.58，这三个名字**import 就会失败**。
> 它们的功能统一收敛到 **`TorchTrainer`**：把框架自己的 `Trainer`
> 建在**训练函数内部**，再用 `prepare_trainer` / `RayTrainReportCallback`
> 把指标与 checkpoint 接回 `ray.train.report`（§13.7 有完整例子）。
> ⚠️ `ray.train.lightning` 这个模块**还在**，但 `__all__` 里剩下的是
> `prepare_trainer` / `RayDDPStrategy` / `RayFSDPStrategy` /
> `RayDeepSpeedStrategy` / `RayLightningEnvironment` / `RayTrainReportCallback`
> —— **没有 `LightningTrainer`**。

（网上和部分教程里写的 `ray.train.Trainer(...)` 是错的，会 `ImportError`。
本教程附录 A 早期版本也写错过，已修正。）

**② 同名不同类：`TorchTrainer` 有两个导入路径。**

```python
from ray.train.torch import TorchTrainer            # ← 用这个(会按开关指向 V1 或 V2)
from ray.train.torch.torch_trainer import TorchTrainer        # V1 实现
from ray.train.v2.torch.torch_trainer import TorchTrainer     # V2 实现
```

`ray.train.torch.TorchTrainer` 会根据 V2 开关帮你选对实现；
**直接 import 具体的 `.torch_trainer` / `.v2.torch_trainer` 是 V1/V2 混用报错
最常见的原因** —— 别那么写。

**③ 容错默认是**关**的。** `FailureConfig.max_failures` 默认 **0**，
意思是"失败一次就放弃"。所以"我配了 `FailureConfig` 但任务失败后没有恢复"
不是 bug —— 你多半没显式给 `max_failures`：

```python
from ray.train import FailureConfig, RunConfig
RunConfig(failure_config=FailureConfig(max_failures=3))   # 至少要给这个
```

（`-1` = 无限重试。⚠️ **`fail_fast` 在 V2 下已经不可用** —— 传了就报错，
不是「与 `max_failures` 互斥」那么温和。）

**⚠️ 本书第四轮在这里漏了一个字段，与 §13.5 冲突**：V2 的容错预算由
**三个**字段共同构成 ——

| 字段 | 默认值 | 管什么 |
|---|---|---|
| `max_failures` | `0` | "真实故障"的重试次数（**默认 0 = 一次就放弃**） |
| `max_preemption_failures` | `-1` | **抢占**单独计数、**不占** `max_failures` 的额度 |
| `controller_failure_limit` | `-1` | controller 自身崩溃的容忍次数 |

早先这里写的是"只由 `max_failures` 加 `controller_failure_limit` 控制"，
**漏掉了 `max_preemption_failures`** —— 而抢占与真实故障分开计数正是 V2 的
关键设计（见 §13.5），漏掉它会让读者以为"抢占会吃掉我的重试额度"。

### 已移除的 Trainer 在 V2 下怎么办

上面①里列的三个"框架专属 Trainer"是 **V1 时代**的产物：它们把某个框架的
`Trainer` 整体包进来，再透传一堆 `xxx_init_args` / `xxx_init_kwargs`。
**V2 里推荐的主路径不是它们，而是 `TorchTrainer` + 框架自己的胶水。**

| Trainer | 它替你做掉的事 | 状态 |
|---|---|---|
| `TorchTrainer` | 起 worker、注入 rank、`prepare_model`/`prepare_data_loader`、收 report | **V2 主路径**；也是**所有 torch 系框架的统一入口** |
| `LightningTrainer` | 整个 `pytorch_lightning.Trainer` 交给 Ray | ❌ **2.9 起已移除**。改用 `TorchTrainer` + `prepare_trainer` / `RayTrainReportCallback` |
| `TransformersTrainer` | 整个 HF `transformers.Trainer` 交给 Ray | ❌ **2.9 起已移除**。改用 `TorchTrainer` + `RayTrainReportCallback` —— 见第 29 章 |
| `AccelerateTrainer` | `accelerate` 的封装 | ❌ **2.9 起已移除**（同一份 REP） |
| `XGBoostTrainer` | 分布式梯度提升（`params` / `label_column` / `dataset`），与 torch 无关 | ✅ 仍在，且已 V2 化（`ray.train.v2.xgboost`）；另有 `LightGBMTrainer`。见第 34 章 |

V2 下的推荐写法（以 Lightning 为例，Transformers 同理 —— 把框架的 `Trainer`
建在训练函数**内部**，用 Ray 提供的 callback/工具把指标和 checkpoint 接回
`ray.train.report`）：

```python
import pytorch_lightning as pl
from ray.train.lightning import RayTrainReportCallback, prepare_trainer
from ray.train.torch import TorchTrainer

def train_loop_per_worker(config):
    model = MyLightningModule(...)
    ltrainer = pl.Trainer(max_epochs=config["epochs"],
                          callbacks=[RayTrainReportCallback()])  # 指标/checkpoint → Ray
    ltrainer = prepare_trainer(ltrainer)   # 换掉与 Ray 冲突的 DDP 包装、logger 等
    ltrainer.fit(model, datamodule=MyDataModule(...))

trainer = TorchTrainer(train_loop_per_worker,
                       scaling_config=ScalingConfig(num_workers=4, use_gpu=True))
```

这样写的好处是：进程编排、容错、抢占处理全部走 13.2–13.5 讲的那套 V2 机制，
而不是被夹在"框架专属 Trainer"的中间层里 —— 那一层的参数透传和 Ray 版本耦合，
是迁移时最容易碎的地方。

> ⚠️ **名称已核实，签名未核实**：2.58 的 `ray/train/lightning/__init__.py`
> 里 `__all__` 就是
> `["prepare_trainer", "RayDDPStrategy", "RayFSDPStrategy", "RayDeepSpeedStrategy",
> "RayLightningEnvironment", "RayTrainReportCallback"]`
> —— 上面用到的两个名字都在，且**没有 `LightningTrainer`**。
> 但这两个东西的**签名**（参数名与顺序）本书未逐个核对，
> **迁移前请查当前版本的 Train 文档**。
> 另外"框架专属 Trainer 哪些能在 V2 里用"这个问题已经不成立了 ——
> 它们**在 2.9 就从 Ray 里移除了**（见上面①），所以不存在"哪些仍能配 V1"。

---

最小可跑的 TorchTrainer：

```python
import ray.train
from ray.train import RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer

def train_loop_per_worker(config):
    import torch, torch.nn as nn
    # 1) 模型：交给 Ray 包一层 DDP
    model = ray.train.torch.prepare_model(
        nn.Sequential(nn.Linear(32, 64), nn.ReLU(), nn.Linear(64, 1)))
    # 2) 数据：交给 Ray 分片 + 对齐 DistributedSampler
    loader = ray.train.torch.prepare_data_loader(
        ray.train.get_dataset_shard("train").iter_torch_batches(batch_size=64))
    # 3) 训练循环：抄一份 PyTorch 的就行
    opt = torch.optim.Adam(model.parameters(), lr=config["lr"])
    for _ in range(config["epochs"]):
        for batch in loader:
            ...
            opt.zero_grad()
            loss.backward()      # 梯度同步发生在这一行（NCCL AllReduce）
            opt.step()
        ray.train.report({"loss": loss.item()})

trainer = TorchTrainer(
    train_loop_per_worker,
    # 上面的 get_dataset_shard("train") 取的就是这里声明的数据（见「数据入口」）
    datasets={"train": ds},          # ← 不要注释掉,否则训练函数取不到分片
    train_loop_config={"epochs": 3, "lr": 1e-3},
    scaling_config=ScalingConfig(num_workers=4, use_gpu=True),
    run_config=RunConfig(name="demo-run",
                         storage_path="/mnt/shared/ray_results"),  # 全节点可访问
)
result = trainer.fit()
```

### 关键点

**（1）`prepare_model` 做了什么？** 把模型搬到正确的 device，并**按你指定的
并行策略**换成对应的包装：默认是 DDP（`DistributedDataParallel`），
要 FSDP 得**显式给策略**（`prepare_model(model, parallel_strategy=...)`）。
⚠️ **它不是"看一眼 rank/world_size 自动选包装"的** —— 选哪种并行由参数决定，
rank/world_size 只用来决定"这个 worker 该拿哪一片"。
它**不做**的是：不改你的模型结构、不改优化器、不改 loss。所以
`loss.backward()` 触发的是标准 NCCL AllReduce —— **数据面不是 Ray 的**。

**（2）`prepare_data_loader` 做了什么？** 给 DataLoader 加分布式采样器，让每个
worker 拿到**不重叠**的分片，并把 device 设好。配合
`ray.train.get_dataset_shard("train")`（Ray Data 的 Dataset 会被自动切分）。
好处是数据从对象存储**直连训练 worker、不经过 driver**；用普通 `DataLoader`
也行，但要自己处理 sampler 和分片。

**（3）`ray.train.report` 与 `Result`。** `report(metrics, checkpoint=...)`
把指标和 checkpoint 上报给 controller。`trainer.fit()` 返回的 `Result`：

| 属性/方法 | 含义 |
|---|---|
| `result.metrics` | 最后一次 report 的指标 |
| `result.checkpoint` | 最新的 `Checkpoint` |
| `result.best_checkpoints` | 按 `CheckpointConfig` 排序的 checkpoint 列表 |
| `result.path` / `result.error` | run 的存储路径 / 失败时的异常 |
| `result.get_best_checkpoint(metric, mode)` | 按指标挑最好的那个 |

注意 `RunConfig(storage_path, name)` 这两项**在恢复时必须一致**，否则找不到
上次的 run 状态（官方 fault-tolerance 文档明确写了这一点）。

### 数据入口：`datasets=` 与 `DataConfig` 的分工

**先把最容易搞反的一件事说清楚**：

> **`datasets={"train": ds}` 是 V2 的现行写法，不是旧写法。**
> 很多教程（包括本教程的早期版本）把它标成"V1 时代的写法"，那是错的。
> V2 改的是**别的**东西 —— 数据集仍然走 `datasets=`。

```python
# ✓ V2 现行写法
trainer = TorchTrainer(
    train_loop_per_worker,
    datasets={"train": train_ds, "valid": valid_ds},   # key 就是分片名
    scaling_config=ScalingConfig(num_workers=4, use_gpu=True),
)
```

那 `DataConfig` 是干什么的？**它管的是「怎么切分」，不是「有哪些数据」**：

```python
from ray.train import DataConfig

trainer = TorchTrainer(
    train_loop_per_worker,
    datasets={"train": train_ds, "valid": valid_ds},
    # 只控制 ingestion / 切分行为,不承载数据本身
    dataset_config=DataConfig(
        datasets_to_split="all",      # "all"（默认） | ["train", ...]；想关掉切分传 []
        execution_options=None,       # 透传给 Ray Data 的 ExecutionOptions
        enable_shard_locality=True,   # 默认 True：分片尽量贴近 worker 所在节点
    ),
    scaling_config=ScalingConfig(num_workers=4, use_gpu=True),
    run_config=RunConfig(name="demo-run", storage_path="/mnt/shared/ray_results"),
)
```

训练函数侧按**名字**取分片（名字就是 `datasets` 的 key）：

```python
def train_loop_per_worker(config):
    train_shard = ray.train.get_dataset_shard("train")
    valid_shard = ray.train.get_dataset_shard("valid")     # 可能为 None，先判空
    for batch in train_shard.iter_torch_batches(batch_size=256, device="cuda"):
        ...
```

三点值得说清楚：

1. **为什么会混淆**：⚠️ **`DataConfig` 不是 V2 新增的** —— 它早在
   **Ray 2.6/2.7**（PR #35236）就存在了，当时是来替代弃用的 `DatasetConfig`，
   比 Train V2（2.43+）早了约一年半。所以 V1 教程里
   `dataset_config=DataConfig(...)` 本来就是常见写法。
   真正变的是：**V2 把「切分策略」从 Trainer 的隐式行为提升成了显式配置**，
   并且 `datasets=` 明确为数据入口；
2. **切分发生在 worker 侧**，不是 driver —— 底层就是第 12 章 §12.2 讲的
   `streaming_split`（每个 worker 拿一个互不重叠的流，数据直连 worker，不经过
   driver 的 Python 堆）；
3. `get_dataset_shard()` 只能在**训练函数里**调，driver 上调不到东西。

> ⚠️ **`DataConfig` 的精确字段名以你所用版本为准**：本书写作时
> `datasets_to_split` 与 `execution_options` 是核对过的形参。
> **第六轮标"未确认"的第三个字段现已核实**：2.58 的
> `ray/train/_internal/data_config.py` 里，`DataConfig.__init__` 的完整签名是
> `(datasets_to_split="all", execution_options=None, enable_shard_locality=True)`
> —— 除了上面两个，还有一个 **`enable_shard_locality`**（默认 `True`，
> 让分片尽量贴近持有它的 worker 所在节点）。
> 而 `datasets=` 这个入口本身是稳定的 —— 拿不准时就用它。

### `ScalingConfig` 的其他字段

`num_workers` / `use_gpu` 只是最常用的两个：

| 字段 | 作用 | 注意 |
|---|---|---|
| `num_workers` | worker 进程数 | DDP 下通常 = 总 GPU 数 |
| `use_gpu` | 每个 worker 申请 1 张 GPU | `use_gpu=True` 时默认每 worker 1 张卡，调度器按 GPU 排队。🔴 **没有 `use_cpu` 这个字段** —— 本书早先写的 `use_gpu` / `use_cpu` 配对是错的。2.58 的 `ray/air/config.py` 里 `ScalingConfig` 只有 `use_gpu`；**CPU 侧走 `resources_per_worker={"CPU": n}`**，不写则每个 worker 默认占 1 CPU |
| `resources_per_worker` | 显式资源字典，如 `{"CPU": 4, "GPU": 1}` | 会覆盖 `use_gpu`。⚠️ **支持小数 GPU**（如 `{"GPU": 0.1}`，Ray 资源精度 0.0001），此时多个 worker 会被分到**同一张卡**上 —— 但 DDP/NCCL 场景下这会撞 **"Duplicate GPU detected"**（issue #48012）。要用小数卡请配 `placement_strategy="SPREAD"` 或 `TRAIN_ENABLE_SHARE_CUDA_VISIBLE_DEVICES=0`。本书早先写的"GPU 必须是整数"是错的 |
| `placement_strategy` | `"PACK"` / `"SPREAD"` | `PACK` 让 worker 尽量挤在同一节点（利好 NCCL），`SPREAD` 摊开（利好容错与带宽均摊）。**默认值已核实为 `"PACK"`** —— `ray/air/config.py` 第 185 行：`placement_strategy: Union[str, SampleRange] = "PACK"` |
| `accelerator_type` | 只要某种加速器，如 `"A100"` | 依赖节点上被检测出的加速器资源名；写错的表现是"永远排队"，和资源真不够长得一模一样 |
| ~~`num_trainers_per_worker`~~ | — | ❌ **`ScalingConfig` 里没有这个字段**（2.40 与 2.58 都没有）。本书早先标"未确认"会让读者以为它存在，第五轮已删除 |
| `label_selector` | 按节点标签约束 worker 放置 | 可以是**单个 dict**，也可以是**与 `max_workers` 等长的 list**（逐 worker 指定；长度不对会 `ValueError`） |
| `use_tpu` | 每个 worker 申请 1 个 TPU | `[Experimental]`，配合 `topology` 使用 |
| `topology` | TPU 拓扑字符串 | `[Experimental]` |
| `elastic_resize_monitor_interval_s` | 弹性伸缩的巡检间隔（秒） | **默认已核实为 `60.0`**（`ray/train/v2/api/config.py` 第 102 行），必须非负 |
| `trainer_resources` | **V2 已弃用** —— 传非 `None` 值会抛 `DeprecationWarning`，V2 的 driver/controller **不再预留任何逻辑资源** | ⚠️ **没有"替代参数"**：`resources_per_worker` 描述的是 **worker** 的资源，不是控制面的。V2 的语义就是"控制面不占逻辑资源"，所以正确做法是**什么都不写**，而不是换一个字段。该项同时也是 V2 不支持 colocation 的原因。源码里的落点是 `ScalingConfig.__post_init__` 的 `raise DeprecationWarning(TRAINER_RESOURCES_DEPRECATION_MESSAGE)` |

> **这一版 `ScalingConfig` 的完整公开字段**（2.58 实测，可用于自查）：
> `num_workers` / `use_gpu` / `resources_per_worker` / `placement_strategy` /
> `accelerator_type` / `use_tpu` / `topology` / `label_selector` /
> `elastic_resize_monitor_interval_s` —— 外加一个**已弃用**的
> `trainer_resources`。（`num_workers` 还接受 `(min, max)` 二元组，见 §13.6。）

### 训练函数里的运行时助手

训练函数（`train_loop_per_worker`）里能用的东西其实就这几类：

| API | 返回 |
|---|---|
| `ray.train.get_context()` | **统一入口**。下面那些 rank 类信息都从它身上取 |
| `get_context().get_world_size()` / `.get_world_rank()` | 全局 worker 数 / 当前 worker 的全局 rank |
| `get_context().get_local_rank()` / `.get_local_world_size()` | **当前节点内**的 rank / 本节点 worker 数（决定用哪张卡） |
| `get_context().get_node_rank()` | 节点序号（多机时定位"我是第几台"） |
| `ray.train.torch.get_device()` | 当前 worker 该用的 `torch.device`；多卡 worker 取最小 index。**注意在 `ray.train.torch` 下，不是顶层 `ray.train`** |
| `ray.train.get_dataset_shard(name)` | 本 worker 的数据分片（见上） |
| `ray.train.get_checkpoint()` | 恢复用的最新 checkpoint，没有则 `None` |
| `ray.train.get_all_reported_checkpoints()` | **V2 新增**：本次 run 里 worker 报过的**全部** checkpoint（不只是最新的那个）——做"在训练中途挑最好的一份"时用它 |
| `ray.train.report(metrics, checkpoint=None)` | 上报指标 / checkpoint |

> ⚠️ **`get_world_size()` / `get_world_rank()` / `get_local_rank()` 这些
> 不是 `ray.train` 的模块级函数** —— 它们是 `TrainContext` 的**方法**。
> `ray/train/__init__.py` 的 `__all__` 里的**函数**只有
> `get_context` / `get_dataset_shard` / `get_checkpoint` / `report`
> （V2 另有 `get_all_reported_checkpoints` / `get_preemption_info`）——
> 其余导出项都是**类**（`RunConfig`、`ScalingConfig`、`Checkpoint`…），
> **没有任何 rank 类助手**。
> 写 `ray.train.get_local_rank()` 会直接 `AttributeError`；
> 正确写法是 **`ray.train.get_context().get_local_rank()`**。
> 本书早先把它列成模块级函数，是抄了非官方的记忆版本 —— **以你的版本上
> 的 Train API 页为准再核一次**（若某版本补了模块级别名，以那个版本为准）。

> **`get_device()` 不要自己拼**（完整路径是 `ray.train.torch.get_device()`）。
> 它会返回 `prepare_model` 真正用的那个 device
> （`cuda:0` / `cuda:1`… / `cpu`），你手写 `torch.device(f"cuda:{rank}")` 在多机
> 场景下会把卡对错。V1 里的 `session.get_world_rank()` 与 V2 的
> `ray.train.get_context().get_world_rank()` 是同一件事的新旧两种写法
> —— **但只有后者存在**：`ray.train.get_world_rank()` 这个模块级写法
> **不存在**（写了是 `AttributeError`）。
> ⚠️ 本书早先在这里留了半句"模块级快捷函数与 `get_context()` 方法都在"，
> 与本章 §13.3 的警告、§13.11 的小结**直接冲突** —— 第五轮已删除。

### checkpoint 的正确姿势

```python
import os, tempfile
from ray.train import Checkpoint

with tempfile.TemporaryDirectory() as tmpdir:
    torch.save(model.state_dict(), os.path.join(tmpdir, "model.pt"))
    ckpt = Checkpoint.from_directory(tmpdir)
    ray.train.report({"loss": loss}, checkpoint=ckpt)
```

**不要自己 `torch.save` 到固定路径然后不上报** —— 那样 Ray Train 不知道
checkpoint 的存在，失败重试时不会拿它恢复。这是最常见的"用了 Ray 但容错
没生效"的原因。

#### `Checkpoint` 的造法与读取姿势

🔴 **先说一条会直接报错的**：2.58 的 `ray.train.Checkpoint`
（`python/ray/train/_checkpoint.py`）是一个**目录即一切**的新类，
**只剩两个类方法 + 一个构造函数**。老教程里的
`Checkpoint.from_dict(...)` 与 `ckpt.to_dict()` **都已经不存在了** ——
它们不是"还有但弃用"，而是会抛 `AttributeError`：

```
AttributeError: The new `ray.train.Checkpoint` class does not support `from_dict()`.
Instead, only directories are supported.
```

`_checkpoint.py` 里的 `_CheckpointMetaClass.__getattr__`（第 30 行起）
对这些名字做了**拦截并抛迁移错误**：

| 名字 | 结果 |
|---|---|
| `from_dict` / `to_dict` / `from_bytes` / `to_bytes` / `get_internal_representation` | ❌ `AttributeError` + "only directories are supported" |
| `from_uri` / `to_uri` / `uri` | ❌ `AttributeError` + 提示改用**构造函数** `Checkpoint(path="s3://a/b/c")` |
| `get_preprocessor` / `set_preprocessor` | ❌ `AttributeError` + 提示改用 checkpoint metadata |

**要存一个字典怎么办**：自己 `pickle` 落到目录里，再 `from_directory`
（这正是官方迁移提示给的写法）：

```python
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "data.pkl"), "wb") as fp:
        pickle.dump({"some": "value"}, fp)
    ray.train.report(..., checkpoint=Checkpoint.from_directory(d))
```

| 造 | 用途 |
|---|---|
| `Checkpoint.from_directory(path)` | 你已经在**本地**目录里写好了文件（`torch.save` 出来的权重）—— **唯一推荐的造法，大模型也走这条**。它内部是 `cls(path, filesystem=pyarrow.fs.LocalFileSystem())`，所以**只吃本地路径** |
| `Checkpoint(path="s3://bucket/ckpt")` | **直接包一个路径**（本地或远端都行）。远端路径会从 URI 推断文件系统（`__init__` 里的 `pyarrow.fs.FileSystem.from_uri(path)`），也可显式传 `filesystem=` |
| ~~`Checkpoint.from_dict({...})`~~ | ❌ **已不存在**（见上）。本书早先把它列为"小对象的造法"，那是迁移前的 API |
| ~~`Checkpoint.from_uri(uri)`~~ | ❌ **不存在**。用上面的构造函数代替 |

| 读 | 说明 |
|---|---|
| `ckpt.as_directory()` | 拿到本地目录路径。**永远当上下文管理器用**：`with ckpt.as_directory() as d:`。本地目录只是原样 yield（不拷贝、退出不删）；远端会先下载到临时目录，**退出上下文即清理**（见下） |
| `ckpt.to_directory(path=None)` | 非上下文管理器版，**把内容写到本地目录并返回路径**。要"一个本地目录"又不想用 `with` 时用它 |
| `ckpt.path` | 构造时给的路径（`Checkpoint(path=…)` 的本地目录或 `s3://…` URI），交给下游（推理作业、Serve）用。⚠️ **没有 `get_uri()` 这个方法**（`uri` / `to_uri` 同样在拦截名单里） |
| `ckpt.get_metadata()` / `set_metadata()` / `update_metadata()` | checkpoint 的元数据字典。**这是旧的 preprocessor 通道的替代品** |
| ~~`ckpt.to_dict()`~~ | ❌ **已不存在**。要从 checkpoint 读回一个小对象，自己 `pickle.load` 那个目录里的文件 |
| `result.checkpoint` / `result.best_checkpoints` / `result.get_best_checkpoint(metric, mode)` | run 结束后从 `Result` 上拿（见本节前面的 `Result` 表） |

**保存权重（大模型版）**：

```python
import os, tempfile, torch
from ray.train import Checkpoint

def train_loop_per_worker(config):
    model = ray.train.torch.prepare_model(...)

    # ① 恢复：有 checkpoint 就先加载
    ckpt = ray.train.get_checkpoint()
    if ckpt is not None:
        # as_directory() 永远返回上下文管理器 —— 不要写 isinstance(x, str)
        with ckpt.as_directory() as d:
            model.load_state_dict(torch.load(os.path.join(d, "model.pt")))

    ...
    # ② 上报：先写进目录，再整体交给 Ray
    with tempfile.TemporaryDirectory() as tmpdir:
        torch.save(model.state_dict(), os.path.join(tmpdir, "model.pt"))
        ray.train.report({"loss": loss.item()},
                         checkpoint=Checkpoint.from_directory(tmpdir))
```

**为什么大权重必须走 `from_directory`**：`from_directory` 是**按文件搬**，
不经过 Python 对象这一层 —— Ray 只是把目录同步到 `storage_path`。

⚠️ **不要试图用 pickle 把大权重塞进一个文件走"目录"这条路**：
7B 模型的 `state_dict` 是几十 GB，`pickle.dump` 会在 driver/worker 的
Python 堆上把它整体物化一遍，再写盘；正确的做法是让
`torch.save(model.state_dict(), ...)` **直接写进那个临时目录**
（`torch.save` 是流式写文件，不构造一个巨大的内存 buffer），
然后 `Checkpoint.from_directory(tmpdir)`。
（本书早先在这里比较的是 `from_dict` —— 那个 API 在 2.58 已经不存在了，
详见上面那张"已被拦截的名字"表。）

> 🔴 **本书第四轮在这里写错了 —— "两种返回形态"根本不存在。**
> `as_directory()` 被 `@contextlib.contextmanager` 装饰、返回类型是
> `Iterator[str]`，**任何情况下都只返回上下文管理器**。差别只在**里面**：
>
> | checkpoint 位置 | `with ckpt.as_directory() as d:` 做了什么 |
> |---|---|
> | 本地目录 | 原样 yield 那个路径，**不拷贝**；退出上下文**不删** |
> | 远端路径 | 先下载到临时目录再 yield；退出上下文**删掉临时目录** |
> | 同节点多进程同时调用 | 只下载一次，其余进程等它，拿到**同一个**临时目录 |
>
> 所以正确的写法**只有一种**：`with ckpt.as_directory() as d:`。
> 早先那段 `if isinstance(cp, str): ... else: with cp as d:` 会永远走 else
> 分支（因为返回值从来不是 `str`），把一个不存在的坑教给了读者。
>
> ⚠️ 还要记住：**yield 出来的目录是只读的** —— 不要往里写东西，
> 远端场景下它退出上下文就没了。

#### checkpoint 保留策略：`CheckpointConfig`

到这一步"存得下来"已经解决了，接下来是"别把存储写爆"：

```python
from ray.train import CheckpointConfig, RunConfig

run_config = RunConfig(
    name="demo-run",
    storage_path="/mnt/shared/ray_results",
    checkpoint_config=CheckpointConfig(
        num_to_keep=2,                        # 只留 2 份；None（默认）= 一份都不删
        checkpoint_score_attribute="loss",    # 按哪个指标排序
        checkpoint_score_order="min",         # 默认 "max"；只接受 "min" | "max"
    ),
)
```

* 🔴 **`CheckpointConfig` 里那两个"看起来该有"的字段已经没了**：
  `checkpoint_frequency` 与 `checkpoint_at_end` 在 V2 的
  `CheckpointConfig.__post_init__` 里是 **`raise DeprecationWarning(...)`** ——
  **传进去就抛，不是静默忽略**（"何时存 checkpoint"现在完全由你的训练函数
  调 `ray.train.report` 的频率决定，见第 14 章 §14.14）。本书第 13/14 章早先
  把这两个列成可用字段，已一并更正；
* 保留是**在 `storage_path` 上删旧文件**：每上报一个新 checkpoint，controller 就
  按上面的规则重排一次，把超出的删掉。`num_to_keep` 留空 = 一个都不删 ——
  跑 1000 步就是 1000 份 checkpoint，这是把共享存储写满的头号原因。
  ⚠️ 它**必须是 `None` 或 `>= 1`**（`__post_init__` 里有
  `if self.num_to_keep is not None and self.num_to_keep <= 0: raise ValueError`）；
* `checkpoint_score_attribute` 必须和 `ray.train.report({...})` 里的 key 对得上，
  否则排不了序（此时是报错还是退回"留最近几份"，**未确认**）；
* 配上 score 之后，保留的是**按分数排序的前 k 个**，而不是"最近 k 个"；
  V2 是否额外保留最新一份 **未确认**；
* 恢复用的是**最新一份**，`result.checkpoint` 也是最新一份 —— 想要"最好的那个"
  得从 `result.best_checkpoints` / `get_best_checkpoint(...)` 里取。

## 13.4 并行策略：DDP / FSDP / DeepSpeed / Megatron 的关系

这张 ASCII 图是本章最重要的一张：

```
┌─────────────────────── Ray Train 的职责 ───────────────────────┐
│  决定"起几个进程、在哪台机器、谁是 rank 0"                       │
│  建 Controller，收集 report、管理 checkpoint、处理失败/抢占      │
└───────────────────────────┬────────────────────────────────────┘
                            │ 启动 worker（Ray actor），注入 rank/world_size
┌───────────────────────────▼────────────────────────────────────┐
│  并行策略层：DDP / FSDP / DeepSpeed / Megatron-LM / Lightning    │
│  决定"参数怎么切、梯度怎么同步、显存怎么省"                       │
└───────────────────────────┬────────────────────────────────────┘
                            │ torch.distributed API
┌───────────────────────────▼────────────────────────────────────┐
│  通信层：NCCL / Gloo / MPI   ← Ray 完全不参与                     │
│  AllReduce / AllGather / ReduceScatter 走这里                    │
└────────────────────────────────────────────────────────────────┘
```

**诚实结论：数据面不是 Ray 的。** 训练的热路径（`loss.backward()` 触发的
集合通信）一次 `ray.remote` 都不会走。Ray 只在**启动、上报、恢复**这三件事上出现。

这一点和 vLLM 的 `RayExecutorV2` 是同一个趋势（第 1 章提过）：
Ray 在 AI 栈里的位置正在收敛到"调度与放置"。

### 什么时候用哪个策略

| 策略 | 切什么 | 适用 | 在 Ray Train 里怎么开 |
|---|---|---|---|
| **DDP** | 不切，每卡全量副本 | 模型能塞进单卡显存 | `prepare_model` 默认 |
| **FSDP** | 参数 / 梯度 / 优化器状态分片 | 7B–70B，单卡放不下 | 自己用 FSDP 包装，或用 `prepare_model` 的 FSDP 路径 |
| **DeepSpeed** | ZeRO 各级 + offload | 需要 CPU/NVMe offload | `TorchTrainer` + DeepSpeed 配置 |
| **Megatron** | 张量并行 + 流水线并行 | 百 B 以上 | Megatron-LM 自己的启动器，Ray 只做编排 |

**取舍**：并行策略的选择跟 Ray 无关。别指望换成 Ray 就能训更大的模型 ——
显存问题要靠 FSDP/DeepSpeed 解决，Ray Train 只是让这些进程更容易被拉起来、
挂了更容易被恢复。

## 13.5 容错

Ray Train 的容错分三类，处理方式完全不同。

```
                ┌──────────────┐
                │   RUNNING    │
                └──────┬───────┘
        worker 挂 ─────┤───── 节点被抢占
                       │
        ┌──────────────▼───────────────┐
        │  WorkerGroupError            │  ← 真实故障（OOM/硬件/用户 bug）
        │  重试预算：max_failures      │
        └──────────────┬───────────────┘
                       │
        ┌──────────────▼───────────────┐
        │  PreemptionError             │  ← 计划内的抢占（spot 回收）
        │  重试预算：max_preemption_failures │
        └──────────────┬───────────────┘
                       │
              从最新 checkpoint 恢复 → 回到 RUNNING
```

### worker 失败与 `FailureConfig`

```python
from ray.train import FailureConfig, RunConfig

RunConfig(
    failure_config=FailureConfig(
        max_failures=3,                 # 真实故障重试次数
        max_preemption_failures=-1,     # 抢占重试预算，-1 = 不限制
    ),
)
```

**为什么把抢占单独拆一个预算**：抢占是**预期内**的事件（云上用 spot 实例
就会不断发生），不该消耗"真实故障"的重试额度。如果两者共用 `max_failures`，
一次 spot 回收就可能把一个健康的训练任务判死。这是 V2 里比较务实的一个设计。

### 抢占：`PreemptionError` / `get_preemption_info`

V2 新增的公开 API（PR #64360，Ray 2.58 一手清单里也列了）：

```python
info = ray.train.get_preemption_info()   # → PreemptionInfo | None
```

* 返回**当前 worker 即将被抢占**的信息，或 `None`（没在抢占中）；
* `PreemptionInfo` 里有受影响的 node id、world rank 列表，以及回收 deadline；
* **推荐反应是"存一个 just-in-time checkpoint 然后继续训练"** —— 节点真被
  回收时 Ray Train 会重启这个 run、从最新 checkpoint 继续，并且消耗的是
  `max_preemption_failures` 而不是 `max_failures`
  （docs.ray.io 的 `ray.train.get_preemption_info` API 页）。

`PreemptionError(preemption_info, drain_timed_out=False)`：
`drain_timed_out=False` 表示所有 worker 都已自己退出（观察到的事实）；
`True` 表示到了回收 deadline 还有 worker 在跑，Ray Train 停止等待并强制拆除。
它被刻意设计成**和 `WorkerGroupError` 不同的类型**，就是为了让两个重试预算
互不干扰。

### 恢复

恢复的前提是三点同时满足：

1. 训练循环里**真的调用了** `ray.train.report(..., checkpoint=...)`；
2. `RunConfig(storage_path, name)` 和上次**一致**；
3. 训练循环开头**读了** `ray.train.get_checkpoint()` 来恢复模型/优化器状态。

第 3 点常被漏掉 —— Ray 会把最新 checkpoint 放到 `get_checkpoint()`，但
**加载逻辑要你自己写**。Ray 只负责"把 checkpoint 送到你手上"。

## 13.6 与 `torchrun` 的对比

这是最需要诚实的一节。**很多场景下 torchrun 更好。**

| 维度 | `torchrun` | Ray Train |
|---|---|---|
| 单机 8 卡 DDP | 一行命令，无额外依赖 | 要装 Ray、起集群、写 Trainer |
| 固定拓扑 | 直接写死，可控 | Ray 自己决定放置，要控制得用 placement group |
| 弹性（节点随时来走） | 基本没有 | **有**，这是 Ray 的主场 |
| 失败恢复 | 自己写或上 torch elastic | 内置，checkpoint 上报 + 重试预算 |
| 多任务共享集群 | 难 | **有**，Ray 调度器管资源 |
| 与 Tune/Data 联动 | 要自己粘 | 原生 |
| 调试体验 | 好（就是普通多进程） | 差（跨进程、异步、traceback 被包装） |
| 依赖 | PyTorch 自带 | Ray + dashboard + 集群运维 |

**直接 torchrun 更好的情况**：单机多卡、拓扑固定、任务一次性跑完；不需要弹性、
也不跟别人共享集群；处于调试阶段（`torchrun --nproc_per_node=2` 的心智负担
远小于起一个 Ray 集群）；团队没有 Ray 运维能力（第 17 章 §17.7 会讲这个成本）。

**Ray Train 更好的情况**：集群共享、多个团队抢 GPU；跑在 spot 上、抢占是常态、
需要自动恢复；要跑**几十上百个**训练任务而不是一个；数据预处理和训练要在同一
条管道里；想让训练任务和 Serve 在同一个资源池里调度。

> 📌 **"弹性"这一行要补一个具体入口** —— 本书前四轮把它当成 Ray 的固有优势讲，
> 却**从没给过 API**。它就是 **`ScalingConfig(num_workers=...)` 传一个
> `(min, max)` 元组**：
>
> ```python
> from ray.train import ScalingConfig
>
> # 固定规模(默认形状):一定要 8 个 worker,少一个就不跑
> ScalingConfig(num_workers=8)
>
> # 弹性:在 4~8 之间伸缩 —— 节点走了缩到 4 还能继续,节点来了再涨回 8
> ScalingConfig(num_workers=(4, 8))
> ```
>
> ⚠️ **弹性只是"允许规模变化",不等于"自动帮你恢复"**。要有意义必须配两样东西：
> ① **`RunConfig(failure_config=FailureConfig(max_failures=N))`** —— 否则默认
> `max_failures=0`，缩容触发的一次 worker 失败就直接终止整个训练；
> ② **周期性 `ray.train.report(..., checkpoint=...)`** —— 没有 checkpoint，
> worker 数变化后新加入的 worker 无从恢复进度。
>
> 相关旋钮还有 `elastic_resize_monitor_interval_s`（2.58 的 `ScalingConfig`
> 里确实有这个字段），**默认值已核实为 `60.0` 秒**
> （`ray/train/v2/api/config.py` 第 102 行；负值会 `ValueError`）。
> 它控制"多久巡检一次规模变化"—— 调小反应快、调大省调度开销。
> **弹性伸缩的完整语义（缩容时在飞的那批梯度怎么办、`min_workers` 与
> checkpoint 的交互）本书未逐版本核实**，请以官方 elastic-training 文档为准。

一句话：**torchrun 解决"怎么起进程"，Ray Train 解决"怎么在共享、会坏的集群上
管理很多个训练任务"。** 后者才是它的价值。

## 13.7 完整示例：数据并行 + AllReduce 语义

§13.4 那张图讲的是"谁负责什么"，这一节把它跑起来让你看清 AllReduce 的语义。
PyTorch 环境不一定随时有，所以下面给两个版本：
先是一个**纯 Python + NumPy** 的可跑版本（用 multiprocessing 模拟 N 个 worker，
手工实现"梯度 AllReduce = 求平均"），再给对应的 PyTorch 版本做对照 ——
后者与 13.3 的最小例子是同一套写法，**只看差异部分即可**。

### 纯 Python 版（可跑，无需 PyTorch）

```python
"""用 multiprocessing 模拟 Ray Train 的 4 个 worker，手工实现 AllReduce 语义。

对应关系：
    起 N 个训练进程            <-> multiprocessing.Process
    DistributedSampler 分片    <-> 按下标取模切数据（下面 shard()）
    loss.backward() 内的 AllReduce <-> 主循环里的"收集 + 广播"
    ray.train.report           <-> print / queue 上报
"""
import multiprocessing as mp
import numpy as np

WORLD_SIZE, DIM, N, STEPS = 4, 8, 400, 200
W_TRUE = np.arange(DIM) / DIM


def shard(rank):
    """每个 worker 拿不重叠的一份数据。"""
    rng = np.random.default_rng(0)
    X = rng.normal(size=(N, DIM))
    y = X @ W_TRUE
    idx = np.arange(rank, N, WORLD_SIZE)
    return X[idx], y[idx]


def worker(rank, grad_q, bcast_q):
    X, y = shard(rank)
    w = np.zeros(DIM)                      # 每个 worker 一份完整模型副本
    for _ in range(STEPS):
        g = 2.0 * X.T @ (X @ w - y) / len(y)   # 本地梯度
        grad_q.put((rank, g))                  # 唯一的跨 worker 操作
        all_g = bcast_q.get()                  # 拿到所有 worker 的梯度
        w -= 0.5 * np.mean(all_g, axis=0)      # allreduce_mean 的语义
    print(f"worker {rank}: |w - w_true| = {np.linalg.norm(w - W_TRUE):.4f}")


def main():
    ctx = mp.get_context("spawn")
    grad_q, bcast_q = ctx.Queue(), ctx.Queue()
    procs = [ctx.Process(target=worker, args=(r, grad_q, bcast_q))
             for r in range(WORLD_SIZE)]
    for p in procs:
        p.start()
    # 玩具版集合通信：收齐 4 份梯度 → 广播回 4 份
    # （真实实现是 ring/tree AllReduce，但语义完全相同）
    for _ in range(STEPS):
        grads = [grad_q.get()[1] for _ in range(WORLD_SIZE)]
        for _ in range(WORLD_SIZE):
            bcast_q.put(grads)
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
```

**要点**：

1. **每个 worker 有自己的一份模型副本**（`w = np.zeros(DIM)`）——
   这就是 DDP 的 "data parallel"；
2. **数据被切成不重叠的分片** —— 就是 `DistributedSampler` /
   `prepare_data_loader` 干的事；
3. **唯一的跨 worker 操作是梯度求平均** —— 对应 NCCL AllReduce。
   **Ray Train 不参与这一步**，它只负责把那 4 个 `worker()` 拉起来。

### PyTorch 版对照（同样的语义，Ray Train 写法）

```python
import os, tempfile
from ray.train import Checkpoint, RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer
import ray.train

def train_loop_per_worker(config):
    import torch, torch.nn as nn
    model = ray.train.torch.prepare_model(nn.Linear(config["dim"], 1))
    loader = ray.train.torch.prepare_data_loader(
        ray.train.get_dataset_shard("train").iter_torch_batches(
            batch_size=config["batch_size"]))
    opt = torch.optim.SGD(model.parameters(), lr=config["lr"])
    for _ in range(config["epochs"]):
        for batch in loader:
            loss = nn.functional.mse_loss(model(batch["x"]).squeeze(-1), batch["y"])
            opt.zero_grad()
            loss.backward()                     # ← AllReduce 在这一行内部
            opt.step()
        # 上报 + 存 checkpoint：先落盘成目录，再整体交给 Ray
        # （⚠️ 不能写 Checkpoint.from_dict —— 2.58 已无此 API，见 §13.3）
        with tempfile.TemporaryDirectory() as d:
            torch.save(model.state_dict(), os.path.join(d, "model.pt"))
            ray.train.report({"loss": loss.item()},
                             checkpoint=Checkpoint.from_directory(d))

ds = ray.data.from_items([{"x": [...], "y": 0.0}])   # 真实场景是 read_parquet
trainer = TorchTrainer(train_loop_per_worker,
    datasets={"train": ds},                   # 数据走 datasets=;DataConfig 只管切分策略
    train_loop_config={"dim": 8, "batch_size": 32, "lr": 0.1, "epochs": 20},
    scaling_config=ScalingConfig(num_workers=4, use_gpu=False),
    run_config=RunConfig(name="dp-demo", storage_path="/tmp/ray_results"))
print(trainer.fit().metrics)
```

两版对照着看，Ray Train 的贡献就清楚了：**它把"起 4 个进程 + 注入 rank +
分片数据 + 收指标 + 存 checkpoint"这几件事从几十行样板代码变成几行配置**，
而梯度同步那部分两边完全一样。

## 13.8 与 Ray Tune 的集成写法变化

这是一个**破坏性变更**，老代码迁移时最容易卡住：

```python
# ✗ 旧写法（已弃用）
tuner = Tuner(
    TorchTrainer(train_fn, scaling_config=..., run_config=...),   # ← Trainer 实例
    param_space={"train_loop_config": {...}},
    tune_config=TuneConfig(num_samples=10),
)
```

Ray 会在 `TunerInternal` 里检测到 `isinstance(trainable, BaseTrainer)` 并打
弃用警告（源码里的原文是 "Passing a Trainer to the Tuner is deprecated"）。
原因是 Train V2 把控制流反转了：**Tune 现在跑的是 driver function，
而不是 Trainer 实例**（PR 说明里写的是 "In Train V2, tune runs trials of
`train_driver_fn` instead of Trainer instance"）。

```python
# ✓ 新写法：Tuner 包一个 driver function
from ray import tune
from ray.tune.integration.ray_train import TuneReportCallback

def train_driver(config):
    trainer = TorchTrainer(
        train_loop_per_worker,
        train_loop_config=config,                 # ← 调参的就是这个
        scaling_config=ScalingConfig(num_workers=2, use_gpu=True),
        run_config=RunConfig(
            name=f"trial-{tune.get_context().get_trial_id()}",   # ← 每个 trial 唯一
            callbacks=[TuneReportCallback()],     # ← 把 Train 的指标转给 Tune
        ),
    )
    trainer.fit()

tuner = tune.Tuner(
    train_driver,                                 # ← 传函数，不传 Trainer
    param_space={"lr": tune.loguniform(1e-4, 1e-1)},
    tune_config=tune.TuneConfig(num_samples=10, metric="loss", mode="min"),
)
results = tuner.fit()
```

两个**必需**的桥接件（漏了就静默失败）：

1. **`TuneReportCallback`**：把 Train 的 `report` 转发给 Tune。没有它，
   每个 trial 都会"成功"结束，但 Tune 收不到任何指标 —— 于是找不到 best trial；
2. **每个 trial 唯一的 run name**：否则多个 trial 会在共享存储里互相覆盖。

第 14 章会把 Tune 那一侧展开。

## 13.9 与 mini-ray 的关系

**mini-ray 没有实现 Ray Train**（README 的"明确不做"清单里列了
Ray Data/Train/Tune/Serve/RLlib）。但 Ray Train 的**每一层都建在 mini-ray
实现过的原语上**：

| Ray Train 的组件 | 对应的 Ray Core 原语 | mini-ray 里的位置 |
|---|---|---|
| Worker group（N 个训练进程） | actor（长驻、有状态、持有 GPU） | `actor.py` / `worker.py` |
| `ScalingConfig(num_workers, use_gpu)` | 资源模型 + `num_gpus` 申请 | `scheduler.py` |
| 拓扑敏感放置（同节点 8 卡） | 放置组 + bundle | `placement_group.py` |
| Controller 收集 report | actor RPC + `ray.get` | `raylet_client.py` |
| worker 挂了重启 | actor 重启 + 重试 | `test_actors.py` 重启用例 |

可跑的对照物：`examples/10_parameter_server.py` —— "一个中心 `ParameterServer`
actor + 每轮并发拉起的无状态 worker task"的骨架，结构上跟 Ray Train 的
"多个 train worker + 一个 controller"一致，只是把 all-reduce 换成了"推给中心
actor"。注意它用的是普通 actor（`ActorPool` 只是文件末尾留的练习），
不是自动伸缩的 actor 池。

**注意边界**：mini-ray 的 actor 重启是"进程级"的，**不重建训练状态** ——
从 checkpoint 恢复需要框架层（Ray Train）和应用层（你的训练循环）共同完成。
这也是为什么第 10 章的 lineage 重建对训练任务**不够用**：
训练的状态在 GPU 显存里，不在对象存储里。

## 13.10 本章小结

* **Ray Train 不是训练框架，是声明式启动器 + 进程编排 + 容错外壳。** 它负责
  "起几个进程、放哪、rank 怎么分、挂了怎么恢复"；并行策略（DDP/FSDP/
  DeepSpeed/Megatron）和集合通信（NCCL）**不属于它**，训练热路径上一次
  `ray.remote` 都没有。
* **Ray Train V2 自 2.51.0 起默认开启**（PR #57857），回退需
  `RAY_TRAIN_V2_ENABLED=0`；V1 API 已 deprecated，且 V1/V2 配置混用会报错。
  注意官方文档部分页面仍写着"需设 =1"，**以 release notes 和源码为准**。
* V2 的新东西：`LoggingConfig`、`ValidationConfig`、`PreemptionError`、
  `get_preemption_info()`、公开的抢占状态机。
* 现代写法：**`TorchTrainer(train_loop_per_worker, scaling_config, run_config)`** ——
  注意**没有**一个通用的 `ray.train.Trainer`（见下方"三个高频困惑"），
  循环里用 `prepare_model` / `prepare_data_loader`，用
  `ray.train.report(metrics, checkpoint=...)` 上报。
* **数据入口是 `datasets={"名字": ds}`**（V2 也仍然是这样 —— 别被"V2 换了入口"
  的说法带偏）；`DataConfig` 管的是**怎么切分**
  （`datasets_to_split` / `execution_options` / `enable_shard_locality` 三个字段），
  不承载数据本身。worker 侧用 `get_dataset_shard(name)` 按 key 取；
  切分在 worker 端发生（底层是 Ray Data 的 `streaming_split`），不经过 driver。
* **worker 侧的运行时助手**：统一入口是 **`ray.train.get_context()`**，
  rank/size 都从它上面取（`get_context().get_world_rank()` 等）——
  ⚠️ **`ray.train.get_world_rank()` 这种模块级写法不存在**（`AttributeError`）。
  device 一律用 `ray.train.torch.get_device()` 拿，
  别自己拼 `cuda:{rank}`，也不要写顶层的 `ray.train.get_device`（不存在）。
* **被删掉的是 `LightningTrainer` / `TransformersTrainer` / `AccelerateTrainer`
  这三个 V1 时代的框架专属 Trainer**（2.9 起 `import` 就会失败）：V2 推荐用
  `TorchTrainer` + 框架自己的胶水（如 Lightning 的 `prepare_trainer` /
  `RayTrainReportCallback`）。而 **`XGBoostTrainer` / `LightGBMTrainer` 仍然在
  且已 V2 化**（`ray.train.v2.xgboost` / `ray.train.v2.lightgbm`），别把它们
  和被删的那三个混为一谈。
* **checkpoint 保留要靠 `CheckpointConfig`**：`num_to_keep=None`（默认）意味着
  一份都不删，这是写爆共享存储的头号原因；读的时候记住 `as_directory()`
  **永远返回上下文管理器**（`with ckpt.as_directory() as d:`），
  不存在"有时返回 `str`"这回事。要 URI 用 `ckpt.path`
  —— `ckpt.get_uri()` 这个方法不存在。
* 🔴 **`Checkpoint` 只剩"目录"一条路**：2.58 的
  `ray.train.Checkpoint` 的造法只有 `Checkpoint.from_directory(local_dir)`
  与构造函数 `Checkpoint(path=...)`。
  **`from_dict` / `to_dict` / `from_bytes` / `to_bytes` / `from_uri` / `uri`
  都已被移除**（`_checkpoint.py` 的元类会拦下来抛 `AttributeError` 迁移错误），
  大权重更不能走"pickle 一整个 `state_dict`"这条路。要存字典就自己
  `pickle` 落到目录里再 `from_directory`；preprocessor 走
  `get_metadata()` / `set_metadata()`。
* **容错的前提是你真的上报了 checkpoint**。抢占和真实故障用**两个独立的重试
  预算**（`max_preemption_failures` vs `max_failures`）；恢复还需要
  `RunConfig(storage_path, name)` 一致，且训练循环自己实现"从
  `get_checkpoint()` 加载"的逻辑。
* **torchrun 在单机、固定拓扑、无弹性需求时更简单**。Ray Train 的价值在
  "共享集群 + 会坏的节点 + 很多个训练任务"。
* Tune 集成写法已变：**`Tuner(trainer)` 弃用**，改成 `Tuner` 包一个 driver
  function，并且必须带 `TuneReportCallback` 和每 trial 唯一的 run name。

下一章讲 Ray Tune：搜索空间、采样器、调度器，以及怎么把 trial 数量
换算成 GPU·小时。
