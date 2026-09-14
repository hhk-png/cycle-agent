仓库地址：https://github.com/hhk-png/cycle-agent

# 第 29 章：Ray 与 PyTorch / HuggingFace 生态集成

> 本章目标：讲清 **Ray 与 PyTorch / HuggingFace 这两套生态的接缝在哪**。
> 第 12–16 章分别讲了 Ray 自己的五个库（Data/Train/Tune/Serve/RLlib），
> 但真实工作里你很少「只用 Ray 的库」—— 你用的是
> `datasets` + `transformers` + `peft` + `accelerate`，
> 然后希望 Ray 把它们**编排**起来。
>
> 读完你应该能回答：**哪些东西该交给 Ray、哪些必须留在 HF 那一侧、
> 以及两者对不上时该改哪一边。**

---

## 29.1 为什么单开一章：三层，不是一层

先把边界画清楚 —— 这一章 90% 的困惑都来自**把三层当成一层**：

```
第 3 层   编排层    Ray Train / Ray Data / Ray Tune / Ray Serve
                   「谁在哪台机器上、起几个进程、挂了怎么办」
─────────────────────────────────────────────────────────────
第 2 层   训练层    transformers.Trainer / accelerate / DeepSpeed / FSDP / peft
                   「前向、反向、优化器、梯度同步、checkpoint 格式」
─────────────────────────────────────────────────────────────
第 1 层   张量层    PyTorch / CUDA / NCCL
                   「矩阵乘、集合通信、显存分配」
```

**每一层都只解决自己那层的问题，而且都假设下层已经就位。**

这张图解释了三件常见的事：

| 现象 | 原因 |
|---|---|
| 「Ray Train 里 `Trainer.train()` 报 NCCL 超时」 | 那是**第 1 层**的问题，Ray 帮不上；见第 30 章 |
| 「换了 Ray 之后 checkpoint 存的东西不一样了」 | 那是**第 2 层**的格式问题 —— Ray Train 只在外面套了一层目录约定 |
| 「`TransformersTrainer` 和直接 `Trainer` 有什么区别」 | `TransformersTrainer` **2.9 起已移除**；现在就是「用 `TorchTrainer` 把后者包起来」 |

> **本章的核心判断**：
> **Ray 永远不该取代第 1、2 层。** 当你的代码里出现「用 Ray 实现一个
> 分布式优化器」或者「用 Ray 的 actor 手写 all-reduce」时，
> 几乎一定走错了方向 —— 应该用 NCCL / `torch.distributed`（第 30 章）。

---

## 29.2 Ray Data ↔ HuggingFace Datasets

这一层是最干净的：**两个库都建立在 Apache Arrow 之上，所以互转几乎零代价**
（在同一个节点内是真正的零拷贝，跨节点才走一次序列化）。

### 两个方向的 API

```python
import ray
from datasets import load_dataset

# ── 方向一：HF Dataset → Ray Dataset ──
hf_ds = load_dataset("imdb", split="train")
ds = ray.data.from_huggingface(hf_ds)

# DatasetDict(多个 split)【不支持】—— 必须按 split 一个一个传
# ✗ ray.data.from_huggingface(load_dataset("glue", "mrpc"))        # DatasetDict
# ✗ ray.data.from_huggingface(load_dataset("glue", "mrpc", streaming=True))  # IterableDatasetDict
glue = load_dataset("glue", "mrpc")
train_ds = ray.data.from_huggingface(glue["train"])
val_ds = ray.data.from_huggingface(glue["validation"])

# ── 方向二：Ray Dataset → HF Dataset ──
back = ds.to_huggingface()
print(type(back))                          # <class 'datasets.Dataset'>
```

### 关键语义（会直接影响你能不能用）

| 要点 | 说明 |
|---|---|
| **转换是「视图」还是「拷贝」** | 同节点内**零拷贝**（共享 Arrow 缓冲区）；跨节点要传输。所以「转换」本身很快，**慢的是后面的 shuffle**。⚠️ 但**别把它当免费的午餐**：零拷贝只对"同节点 + 只读"成立，而且一旦你 `ray.put` 一份大数据集/大模型进对象存储，它会**被 pin 住**、挤占对象存储配额（见 §29.7 与第 7 章 §7.7）——所谓"零拷贝"省的是 CPU 与拷贝时间，**不省内存** |
| **`from_huggingface` 会保留 `features`** | HF 的 schema（`ClassLabel`、`Image`、`Audio` 这些）会被转成 Arrow 扩展类型；`to_huggingface()` 转回来时**能还原**，但如果你在中间做了 Ray Data 的算子，schema 可能被降级成普通类型 |
| **`DatasetDict` / `IterableDatasetDict` 不支持** | ❌ **整包传进去会失败**。官方签名注解里写得很直白：*"`DatasetDict` and `IterableDatasetDict` are not supported."* 必须**按 split 取出来再传**（见上面的例子）。⚠️ 本书早先写的是"可以一次转进来，拿到 `dict[str, Dataset]`"——**说反了** |
| **streaming 模式可以** | ✅ HF 的 `load_dataset(..., streaming=True)` 返回 `IterableDataset`，**可以直接喂给 `from_huggingface`** —— 签名就是 `Union[datasets.Dataset, datasets.IterableDataset]`，此时返回的是**流式** Ray `Dataset`（不落地）。⚠️ 本书早先写的是"**不能**直接喂"——**正好说反，而且它劝退的恰好是大数据集唯一正确的路径** |
| **官方更推荐的路** | 文档里有一句容易被跳过的话：*"It is recommended to use `read_parquet` with the `HfFileSystem` filesystem to read Hugging Face datasets rather than `from_huggingface`."* —— 即 `ray.data.read_parquet("hf://datasets/...")`。**`from_huggingface` 适合中小数据集或做原型** |

### 大数据的正确姿势：不要先落地

`load_dataset()` 默认会把整个数据集**下载并缓存在本地磁盘**。数据大的时候，
你其实想要的是「流式读、边读边处理」，这时候**根本不该先转成 HF Dataset**：

```python
# ✗ 会先下载全量到本地 HF 缓存
ds = ray.data.from_huggingface(load_dataset("bigcorp/big-corpus", split="train"))

# ✓ 直接让 Ray Data 去读底层的 parquet/arrow 文件
ds = ray.data.read_parquet("s3://bigcorp/big-corpus/data/")

# ✓ 或者用 HF 的 streaming 迭代器自己封一个数据源
from datasets import load_dataset as hf_load

def gen():
    for row in hf_load("bigcorp/big-corpus", split="train", streaming=True):
        yield row

ds = ray.data.from_items(list(gen()), override_num_blocks=32)
```

> ⚠️ **最后这条是三种写法里最差的一种，列出来是为了让你认出它**：
> `from_items()` 的签名是 `from_items(items: List[Any], *, parallelism: int = -1, ...)`，
> 它内部会调 `len(items)` 和 `items[j]` —— 所以**直接传 generator 会抛
> `TypeError: object of type 'generator' has no len()`**，必须先 `list()`；
> 而 `list(gen())` 等于把整个数据集**拉进 driver 的内存**，
> 上面那个 streaming 迭代器就白写了。
> 另外 `parallelism=` 已弃用，改用 **`override_num_blocks=`**
> （`from_items` 的块数控制参数）。
> **真正的大数据路径是上面那条 `read_parquet`。**

> **经验规则**：数据集能放进单机内存 → `from_huggingface` 图省事；
> 放不进 → 读底层文件格式，**别从 HF 数据集对象出发**。

### 与 tokenizer 的配合

Ray Data 的推荐做法是**把 tokenizer 放进 actor 池**，而不是每个 batch 重新构造
（`transformers` 的 tokenizer 加载有实测几十毫秒的开销）：

```python
from transformers import AutoTokenizer
from ray.data import ActorPoolStrategy

tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")

class Tokenize:
    def __init__(self):
        # 在 actor 里再加载一次 —— 每个 actor 一份,常驻
        self.tok = AutoTokenizer.from_pretrained("bert-base-uncased")

    def __call__(self, batch):
        return self.tok(batch["text"], padding="max_length",
                        truncation=True, max_length=128)

ds = ds.map_batches(
    Tokenize,
    batch_format="pandas",
    compute=ActorPoolStrategy(size=8),   # 8 个常驻 actor 复用 tokenizer
)
```

详见第 12 章的 `ActorPoolStrategy`。

---

## 29.3 Ray Train + HuggingFace Transformers

这是最容易搞混的一块，因为**历史上存在过三个不同的入口**。先把关系摆清楚：

| 入口 | 是什么 | 状态 |
|---|---|---|
| `ray.train.huggingface.TransformersTrainer` | 接收一个「返回 `transformers.Trainer` 的函数」 | ❌ **2.9 起已移除**（2.7 弃用 → 2.8 调用即报错 → 2.9 删除），`import` 就会失败（见下） |
| `ray.train.huggingface.transformers.prepare_trainer()` | 在**你自己的** `Trainer` 上打补丁，让它支持 Ray | 仍在（`TransformersTrainer` 移除后它还在这个模块里） |
| `ray.train.huggingface.transformers.RayTrainReportCallback` | 一个 HF `TrainerCallback`，负责把指标/checkpoint 上报给 Ray | **推荐保留使用** |

### 为什么 `TransformersTrainer` 没了

Ray Train V2（2.51 起默认）把架构换成了「TrainController + worker group」，
同时把框架专属 Trainer 收敛掉了一批。生命周期在 REP
*"Unify Torch based Trainers on the `TorchTrainer` API"* 里写得很明确：
**2.7 标记弃用 → 2.8 调用即报错 → 2.9 移除**。
所以 2.58 里 **`TransformersTrainer` / `LightningTrainer` / `AccelerateTrainer`
三个名字 `import` 就会失败**（`ray.train.huggingface` 的 `__init__` 里没有它们）。
没被删的是 `XGBoostTrainer` / `LightGBMTrainer` —— 它们**已 V2 化**
（`ray.train.v2.xgboost`），见第 34 章。第 13 章 §13.2 / §13.3 有同一张表。

**结论（对 2.58 是事实，不是判断）**：
**用 `TorchTrainer` + 一个自己写的 training function**，
在函数内部构造 HF 的 `Trainer`。理由：

* 这是 V2 的**唯一主路径**，长期支持有保证；
* 「启动方式」和「训练循环」解耦之后，你升级 HF 不再需要等 Ray 适配；
* 代价是你要自己写十几行样板（下面就是）。

### 推荐写法：`TorchTrainer` 包一个 HF `Trainer`

```python
import os
import ray.train
from ray.train import ScalingConfig, RunConfig, CheckpointConfig
from ray.train.torch import TorchTrainer
from ray.train.huggingface.transformers import RayTrainReportCallback

import torch
from datasets import load_dataset
from transformers import (
    AutoModelForSequenceClassification, AutoTokenizer,
    Trainer, TrainingArguments,
)


def train_func(config):
    # ① 每个 worker 都执行这段 —— 用 get_context() 拿自己的身份
    ctx = ray.train.get_context()
    rank = ctx.get_world_rank()          # 全局 rank,0 是主进程
    world = ctx.get_world_size()

    model_name = config["model_name"]
    tokenizer = AutoTokenizer.from_pretrained(model_name)

    # ② 数据分片:每个 rank 只读自己那一片
    raw = load_dataset("imdb", split="train")
    raw = raw.shard(num_shards=world, index=rank, contiguous=True)

    def tok(batch):
        return tokenizer(batch["text"], truncation=True, max_length=256)

    train_ds = raw.map(tok, batched=True)

    model = AutoModelForSequenceClassification.from_pretrained(model_name, num_labels=2)

    # ③ checkpoint 一定要交给 Ray 管 —— 目录由 Ray 注入,
    #    不要自己写死路径,否则抢占恢复时找不到
    ckpt = ray.train.get_checkpoint()          # 第一次是 None
    if ckpt is not None:
        # ⚠️ 目录层级陷阱:Ray 的 HF checkpoint 布局是
        #     checkpoint_00000*/checkpoint/    ← HF 的文件在**嵌套**的 checkpoint/ 里
        #     (Ray issue #40082)。所以不能直接把根目录丢给 from_pretrained。
        with ckpt.as_directory() as d:
            hf_dir = os.path.join(d, "checkpoint")
            model = AutoModelForSequenceClassification.from_pretrained(hf_dir)

    args = TrainingArguments(
        output_dir=config["output_dir"],
        per_device_train_batch_size=config["batch_size"],
        num_train_epochs=config["epochs"],
        learning_rate=config["lr"],
        fp16=torch.cuda.is_available(),
        logging_steps=10,
        # ⚠️⚠️ 最反直觉的一条:save_strategy 必须**保留**,不能设 "no"!
        #      RayTrainReportCallback 重写的是 on_save() —— 它只在
        #      HF 自己存过一次盘之后才把 checkpoint 交给 Ray。
        #      设成 "no" ⇒ HF 不存 ⇒ 回调不触发 ⇒ Ray 侧**一个 checkpoint 都没有**,
        #      get_checkpoint() 永远返回 None,抢占恢复全部失效。
        save_strategy="steps",
        save_steps=100,
        # ⚠️ 这两条必须对齐 —— 但要求来自 Ray 的回调,不是 HF:
        #    RayTrainReportCallback.on_save() 靠 HF 存完盘后调
        #    get_last_checkpoint() 找到那一份再上报,它的 docstring
        #    专门列了一组"有效/无效"的组合,对齐是其中之一。
        #    HF 自己只在旧版本、或 load_best_model_at_end=True 时才会因
        #    两者不一致而报错 —— 别把这条约束记在 HF 头上。
        evaluation_strategy="steps",
        eval_steps=100,              # save_steps % eval_steps == 0
        report_to=[],                # ← 这条关掉是对的(别让 HF 自己上报到 W&B)
    )

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        processing_class=tokenizer,             # 见下面约束 ③
        callbacks=[RayTrainReportCallback()],   # ← 关键的一行
    )

    # ④ 不要用 trainer.train() 的返回值做上报 —— 回调已经报了
    trainer.train()

    # ⑤ 想手动报指标也行(会覆盖同一步的回调上报)
    metrics = trainer.evaluate()
    ray.train.report(metrics, checkpoint=None)


trainer = TorchTrainer(
    train_func,
    train_loop_config={
        "model_name": "distilbert-base-uncased",
        "batch_size": 16,
        "epochs": 2,
        "lr": 2e-5,
        "output_dir": "/tmp/hf-out",
    },
    scaling_config=ScalingConfig(
        num_workers=4,
        use_gpu=True,                # 每个 worker 1 张卡
        placement_strategy="PACK",   # 同机优先,省跨节点通信
    ),
    run_config=RunConfig(
        name="hf-finetune",
        # num_to_keep 管的是「保留几份」，不是「开不开保存」：
        #   不设(None) = 全部保留，磁盘会一直涨
        #   设 2       = 只留最近 2 份，旧的删掉(抢占恢复够用)
        # 它**不决定存盘频率** —— 频率由上面 TrainingArguments 的 save_steps 决定
        checkpoint_config=CheckpointConfig(num_to_keep=2),
        # ⚠️ 默认 max_failures=0 —— 不设的话任何一次失败都直接终止
        failure_config=ray.train.FailureConfig(max_failures=3),
    ),
)

result = trainer.fit()
print(result.metrics)
print(result.checkpoint)
```

### 四个必须记住的约束

**① HF 的 `Trainer` 已经自己做了 DDP，Ray 不要再做一次。**
`TorchTrainer` 会设好 `WORLD_SIZE` / `RANK` / `MASTER_ADDR` / `MASTER_PORT`
这些环境变量，**HF 的 `Trainer` 会自己检测到并初始化 `torch.distributed`**。
所以：
* **不要**在 `train_func` 里调 `ray.train.torch.prepare_model()` —— 那是给
  「自己写训练循环」的人用的，配 HF `Trainer` 会冲突；
* 也不要自己 `dist.init_process_group()`。

> 这条违反直觉，但它是「Ray + HF」最常见的启动期报错来源：
> 两套 DDP 初始化打架，表现为 `Address already in use` 或者
> 进程数和 `world_size` 对不上。

**② checkpoint 交给 Ray 管，但 `save_strategy` 不能关 —— 这是最容易搞反的一条。**

`RayTrainReportCallback` 重写的是 **`on_save()`** 这个钩子，
它**只在 HF 自己存过一次盘之后**才把 checkpoint 交给 `ray.train.report()`。
所以正确组合是：

```python
save_strategy="steps",          # ✅ 保留 HF 的存盘 —— 回调靠它触发
evaluation_strategy="steps",    # ✅ 必须与 save_strategy 相同
save_steps=100, eval_steps=100, # ✅ save_steps % eval_steps == 0
report_to=[],                   # ✅ 关掉 HF 自己的上报(别让它直接推 W&B)
callbacks=[RayTrainReportCallback()],
```

| 写法 | 后果 |
|---|---|
| `save_strategy="no"` + 回调 | ❌ **一个 checkpoint 都不产生**。`get_checkpoint()` 永远 `None`，抢占恢复全部失效 —— 而且**不报错**，你要等到被抢占时才发现 |
| `save_strategy="steps"` 但 `evaluation_strategy` 不一致 | ❌ **Ray 的回调**会报错（它的 docstring 把这对组合列为无效）。HF 自己只在旧版本、或 `load_best_model_at_end=True` 时才抛异常 |
| `save_strategy="steps"` 且两者对齐 | ✅ 正常。HF 存盘 → 回调打包 → `ray.train.report()` |

> **那"每个 worker 各写一份完整模型"的担心呢？** 它对应的是**另一个**问题：
> 如果**不用** `RayTrainReportCallback`、直接在 `train_func` 里调
> `trainer.save_model(local_path)`，那确实会出现 4 个 worker 4 份 1GB 的本地文件。
> 用回调时，HF 写的是 `output_dir` 下的**本地**副本，回调负责把它**收拢成一份**
> 交给 Ray —— 所以本地那点开销是可接受的代价。
> **两件事不要混：要关的不是 `save_strategy`，是"自己手动存盘"。**

**③ `Trainer` 的 `tokenizer=` 参数正在被 `processing_class=` 取代。**
这不是"HF 5.x 的新变化" —— 弃用自 **transformers 4.46** 就开始了，
`tokenizer=` 走的是兼容路径。**以你锁定的 `transformers` 版本为准**；
本节的示例统一用 `processing_class=`，如果你锁的是 4.4x 且它报未知参数，换回 `tokenizer=`。

> 📌 **同一个版本、同一个类，还有一处改名值得记住**：`TrainingArguments`
> 的 **`evaluation_strategy` 在 transformers 4.46 起更名为 `eval_strategy`**
> （旧名走弃用告警，计划在 v5 移除）。本节示例保留 `evaluation_strategy=`
> 是因为 **Ray 官方文档也是这么写的**、且旧名目前仍可用 —— 但如果你的
> transformers 较新又不想看告警，把它换成 `eval_strategy=` 即可，
> **语义完全一样**（"与 `save_strategy` 对齐"这条要求两版都成立 ——
> 但记住它来自 Ray 的回调，不是 HF 的校验，见上面 ②）。

**④ `max_failures` 默认是 0。**
`RunConfig(failure_config=FailureConfig(max_failures=0))` 意味着
**任何一次 worker 失败都终止整个训练**。做长训练时这是反的 —— 见第 13 章 §13.5。

---

## 29.4 Accelerate / DeepSpeed / FSDP 的配置怎么穿过去

三种并行策略，三种传参方式。**别指望 Ray 帮你翻译** —— Ray 只负责把配置
送到每个 worker 的环境里，怎么解释是第 2 层的事。

### 方式一：`TrainingArguments` 直接传（最简单）

```python
from transformers import TrainingArguments

args = TrainingArguments(
    output_dir="/tmp/out",
    # ── FSDP ──
    fsdp="full_shard auto_wrap",
    fsdp_config={
        # ⚠️ 键名没有 fsdp_ 前缀!带前缀的 fsdp_transformer_layer_cls_to_wrap
        #    是**旧名**,HF 迁移指南里计划在 v5 移除。
        "transformer_layer_cls_to_wrap": "LlamaDecoderLayer",
        "backward_prefetch": "backward_pre",
        "activation_checkpointing": True,
    },
    # ── 或 DeepSpeed ──
    # deepspeed="/path/to/ds_config.json",
)
```

HF 的 `Trainer` 会自己读取这些字段并在内部完成初始化。
**Ray 侧你什么都不用改** —— 这就是为什么要用 HF `Trainer` 而不是自己写循环。

### 方式二：`accelerate` 配置

`accelerate` 的 `Accelerator` 会读环境变量（`ACCELERATE_*`）和
`accelerate config` 生成的 yaml。在 Ray 里最省事的做法是
**不要依赖 `accelerate config` 的交互式配置**，而是显式传参：

```python
from accelerate import Accelerator

def train_func(config):
    accelerator = Accelerator(
        mixed_precision="bf16",
        gradient_accumulation_steps=4,
        # 不要用 accelerate launch —— Ray 已经在做进程编排了
    )
    ...
```

> ⚠️ **不要在 Ray 里用 `accelerate launch`**。`accelerate launch` 是
> **进程启动器**，和 `torchrun` 是同一层的东西；Ray Train 已经在做这件事了。
> 嵌套一层的结果是「Ray 起了 N 个 worker，每个 worker 又起了 N 个进程」，
> GPU 数量瞬间超配。**这是从单机脚本迁移时的高频事故。**

### 方式三：DeepSpeed / FSDP 想直接控制

如果你要的是「不用 HF `Trainer`，自己写循环 + DeepSpeed」，
那就是标准的 `TorchTrainer` + `prepare_model` 路径：

```python
import ray.train
from ray.train.torch import TorchTrainer, prepare_model, prepare_optimizer

def train_func(config):
    model = build_model()
    model = prepare_model(model)                   # ← 只返回 model,不解包
    optimizer = build_optimizer(model)             # ← 先有优化器(你自己的代码)
    optimizer = prepare_optimizer(optimizer)       # ← 再单独包一层
    ...
```

> ⚠️ `prepare_model` **只返回模型**，没有第二个返回值。
> 写 `model, optimizer = prepare_model(model, ...)` 会直接抛
> `ValueError: not enough values to unpack` —— 这是从"以为它像
> `accelerator.prepare()` 一样返回一堆东西"的直觉来的。
> 优化器要用 **`ray.train.torch.prepare_optimizer()`** 单独包。

这一条路的细节在第 13 章 §13.4（并行策略）里。

### 一张对照表

| 你想要的 | 用什么 | Ray 侧要做什么 |
|---|---|---|
| 最少改动跑起来 | HF `Trainer` + `TorchTrainer` | 只传 `ScalingConfig` |
| FSDP / DeepSpeed | `TrainingArguments` 的字段 | 什么都不用做 |
| `accelerate` | 在 `train_func` 里构造 `Accelerator` | 不要用 `accelerate launch` |
| 完全自己控制 | `prepare_model` + 自写循环 | 见第 13 章 |
| 有抢占的长训练 | 上面任意一种 + `FailureConfig` + `CheckpointConfig` | **必做** |

---

## 29.5 PEFT / LoRA / QLoRA 的分布式微调

这一节给一个**完整、可直接改的 LoRA 微调脚本**。它是 2026 年最常见的
真实负载形态：单卡装不下全量微调，但 LoRA 可以，而且多个 rank 并行很划算。

### 为什么 LoRA 在 Ray 上特别合适

* **每个 rank 只需要一份基座 + 一份 LoRA 适配器** —— 显存需求小，
  `ScalingConfig(num_workers=N, use_gpu=True)` 直接线性加速；
* **checkpoint 很小**（LoRA 适配器通常几 MB 到几十 MB），
  存/取/上报的开销可以忽略 —— 这让「频繁 checkpoint」变得廉价，
  而频繁 checkpoint 正是抢占容错的前提（第 13 章 §13.5）。

### 完整示例

```python
"""LoRA 微调:Ray Train(编排) + HF Trainer(训练) + PEFT(参数高效)"""
import ray.train
from ray.train import ScalingConfig, RunConfig, CheckpointConfig, FailureConfig
from ray.train.torch import TorchTrainer
from ray.train.huggingface.transformers import RayTrainReportCallback

import torch
from datasets import load_dataset
from transformers import (
    AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments,
)
from peft import LoraConfig, get_peft_model, TaskType


def train_func(config):
    ctx = ray.train.get_context()
    rank, world = ctx.get_world_rank(), ctx.get_world_size()

    model_id = config["model_id"]
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # ── 1. 数据(M 个 rank 各取一片) ──
    raw = load_dataset(config["dataset"], split="train")
    raw = raw.shard(num_shards=world, index=rank, contiguous=True)

    def tok(batch):
        out = tokenizer(batch["text"], truncation=True,
                        max_length=config["max_len"], padding="max_length")
        out["labels"] = out["input_ids"].copy()      # 因果 LM:labels = inputs
        return out

    train_ds = raw.map(tok, batched=True, remove_columns=raw.column_names)

    # ── 2. 模型 + LoRA ──
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        # QLoRA 的话:load_in_4bit=True, bnb_4bit_quant_type="nf4", ...
        # 需要 bitsandbytes,且每个 rank 独立量化一次(慢,但省显存)
    )
    model = get_peft_model(model, LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=config["lora_r"],
        lora_alpha=config["lora_alpha"],
        lora_dropout=0.05,
        target_modules=["q_proj", "v_proj"],   # 按模型结构调整
    ))

    # ── 3. 从 Ray 的 checkpoint 恢复 ──
    ckpt = ray.train.get_checkpoint()
    if ckpt is not None:
        with ckpt.as_directory() as d:
            # ⚠️ 两个坑叠在一起:
            #  ① 目录层级:适配器文件在嵌套的 checkpoint/ 子目录里(见 §29.3)
            #  ② is_trainable=True 必须显式给!peft 的 load_adapter 默认
            #     is_trainable=False ⇒ 加载进来的适配器是**冻结**的,
            #     续训会**静默地什么都不训练**(不报错,只是 loss 不降)
            #  ③ 续训请用 PeftModel.from_pretrained,不要用 load_adapter
            #     (本书早先这段注释自相矛盾:它先说"不要用 default"、
            #      又说"要么省略",而省略就等于 default —— 必有一错。
            #      第五轮统一改为下面这条标准写法。)
            import os
            from peft import PeftModel
            model = PeftModel.from_pretrained(
                model, os.path.join(d, "checkpoint"), is_trainable=True)

    if rank == 0:
        model.print_trainable_parameters()      # 通常 <1%

    # ── 4. 训练 ──
    args = TrainingArguments(
        output_dir=config["output_dir"],
        per_device_train_batch_size=config["batch_size"],
        gradient_accumulation_steps=config["grad_accum"],
        num_train_epochs=config["epochs"],
        learning_rate=config["lr"],
        bf16=True,
        logging_steps=10,
        # ⚠️ 与 §29.3 同一条规则:不能设 "no",回调靠 on_save() 触发
        save_strategy="steps",
        save_steps=100,
        evaluation_strategy="steps",
        eval_steps=100,
        report_to=[],              # 这条关掉是对的
        ddp_find_unused_parameters=False,
    )

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        callbacks=[RayTrainReportCallback()],
    )
    trainer.train()


trainer = TorchTrainer(
    train_func,
    train_loop_config={
        "model_id": "meta-llama/Llama-3.2-1B",
        "dataset": "tatsu-lab/alpaca",
        "max_len": 512,
        "batch_size": 4,
        "grad_accum": 4,
        "epochs": 1,
        "lr": 2e-4,
        "lora_r": 16,
        "lora_alpha": 32,
        "output_dir": "/tmp/lora-out",
    },
    scaling_config=ScalingConfig(num_workers=4, use_gpu=True),
    run_config=RunConfig(
        name="lora-sft",
        checkpoint_config=CheckpointConfig(
            # num_to_keep 管「留几份」(不是「存多频繁」;存盘频率由上面
            # TrainingArguments 的 save_steps 决定 —— 这两件事经常被混为一谈)
            num_to_keep=2,
            # 设了它,num_to_keep 保留的就是【按该指标排名的前 K 份】,
            # 而不是【最近的 K 份】;不设则保留最近 K 份。
            # ⚠️ 它跟 result.checkpoint / get_best_checkpoint() 无关
            #    ——后两者的 metric/mode 是显式传参的。
            checkpoint_score_attribute="loss",
            checkpoint_score_order="min",
        ),
        failure_config=FailureConfig(max_failures=3),
    ),
)
result = trainer.fit()
```

### 三个 LoRA + 分布式特有的坑

**① checkpoint 里存的是「适配器」还是「全量模型」，要明确。**
`RayTrainReportCallback` 默认会把 HF `Trainer` 的 `save_model` 结果打包。
PEFT 模型 `save_pretrained` 出来的是**适配器**（好，小），
但如果你在 `Trainer` 上调过 `merge_and_unload()`，那就是全量权重（大）。
**推理时要加载的东西不一样** —— 别在部署时才发现。

**② 每个 rank 都会加载一份基座模型。**
LoRA 省的是**优化器状态和梯度**，不是基座。
4 个 rank = 4 份基座权重。如果基座本身就装不下，LoRA 救不了你，
要上 FSDP + LoRA（即 `TrainingArguments(fsdp=...)` 与 PEFT 组合）。

**③ QLoRA 的量化是每个 rank 各自做的。**
`load_in_4bit=True` 时每个 rank 独立量化一遍基座，**启动时间会明显变长**，
而且量化后的模型**不能再和 FSDP 组合**（4bit + FSDP 在多数版本上不兼容，
具体支持矩阵**未确认**，请查 `bitsandbytes` 与你 HF 版本的说明）。

---

## 29.6 分布式推理：把 HF 模型装进 Ray Data

训练之外，第二大类负载是**离线批量推理**。这里的正确形态是
**Ray Data + actor 池**（不是 Ray Serve —— Serve 是给在线服务用的，见第 15 章）。

```python
import ray
from ray.data import ActorPoolStrategy

class Predict:
    def __init__(self, model_id):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(model_id)
        # ⚠️ 批量 generate 必须左填充!tokenizer 默认是右填充(padding_side="right"),
        #    对 decoder-only 模型来说,右填充会让短 prompt 的续写从 pad token
        #    之后开始 —— **不报错,只是输出全错**。这是最难发现的一类 bug。
        self.tok.padding_side = "left"
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, torch_dtype=torch.bfloat16
        ).cuda().eval()

    def __call__(self, batch):
        import torch
        prompts = batch["prompt"]
        inputs = self.tok(prompts, return_tensors="pt",
                          padding=True, truncation=True,
                          max_length=1024).to("cuda")
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=128,
                                      do_sample=False)
        text = self.tok.batch_decode(out[:, inputs["input_ids"].shape[1]:],
                                     skip_special_tokens=True)
        return {"prompt": prompts, "completion": text}


ds = ray.data.read_parquet("s3://bucket/prompts/")

preds = ds.map_batches(
    Predict,
    fn_constructor_kwargs={"model_id": "meta-llama/Llama-3.2-1B"},
    batch_size=32,                     # ⚠️ 见下
    compute=ActorPoolStrategy(min_size=1, max_size=8),  # 池大小：最多 8 张卡
    num_gpus=1,                        # 每个 actor 一张卡
    max_concurrency=2,                 # 每个 actor 内同时处理 2 个 batch
    # ⚠️ 不要写成 concurrency=2 —— 那既是被 2.51 弃用的参数（见第 12 章 §12.2），
    #    又把「池大小」误当成了「单 actor 内并发」。
)

preds.write_parquet("s3://bucket/completions/")
```

### 为什么这里是 `map_batches` 而不是 vLLM

`transformers.generate()` 的吞吐**远低于** vLLM / SGLang ——
它没有 PagedAttention、没有 continuous batching。
所以真实的大规模推理**应该用 vLLM**：

```python
class VLLMPredict:
    def __init__(self, model_id):
        from vllm import LLM
        self.llm = LLM(model=model_id, tensor_parallel_size=1)

    def __call__(self, batch):
        from vllm import SamplingParams
        outs = self.llm.generate(batch["prompt"],
                                 SamplingParams(temperature=0, max_tokens=128))
        return {"prompt": batch["prompt"],
                "completion": [o.outputs[0].text for o in outs]}
```

**判断**：`transformers` 版本适合「一次性、小数据量、想少装依赖」；
超过几千条就该换 vLLM。这个切换点在实测中通常比人们预期的低得多。

### `batch_size` 是这里最重要的旋钮

它的作用**不是**「GPU 利用率」，而是**队列长度**：
`batch_size=32` 意味着每个 actor 一次拿 32 行；8 个 actor 就是 256 行在飞。
太大 → 显存 OOM（而且长序列会把显存需求顶上去）；
太小 → actor 大部分时间在等数据。

第 12 章 §12.6 讲了完整的调优方法（`ExecutionOptions`、`preserve_order`、
块大小怎么和 `batch_size` 配合）。

---

## 29.7 模型权重怎么送到每个 worker：三种方式

这是「Ray + 大模型」的一个实际问题。假设 8 个 worker 都要加载同一个 7B 模型：

| 方式 | 机制 | 适合 | 代价 |
|---|---|---|---|
| **每个 worker 自己 `from_pretrained`** | 8 个进程各自读一次磁盘/HF 缓存 | 默认做法，简单 | 8 次磁盘 IO；HF 缓存会自动去重，所以通常可接受 |
| **driver 加载后用 `ray.put` 广播** | 放一次进对象存储，各 worker 再 `ray.get` 反序列化（**Arrow/ndarray 类载荷可零拷贝共享，`nn.Module` 不行**）| 小模型、单机多卡 | 模型进了对象存储**会被 pin 住**，占内存 |
| **共享文件系统 / 本地 NVMe** | 权重预置到每台机器，`from_pretrained(本地路径)` | 大规模、多机 | 需要运维预置 |

```python
# 方式二的样子(注意:7B fp16 ≈ 14GB,对象存储放得下才行)
model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch.float16)
model_ref = ray.put(model)

@ray.remote(num_gpus=1)
def worker(model_ref):
    model = ray.get(model_ref)     # 同节点零拷贝,跨节点一次传输
    ...
```

> ⚠️ **方式二对「每个 worker 都要一份独立模型」的情况是错的。**
> 这里要**把"零拷贝"的适用范围收紧**（本书早先的表格与这段话口径不一，
> 第五轮已统一）：**零拷贝共享只对 Arrow / numpy ndarray 这类载荷成立**；
> 一个 `nn.Module` 经 `ray.get` 是**反序列化到各进程自己的堆里**的
> —— 所以"所有 worker 用同一份只读内存"这句话**对模型对象并不成立**。
> （GPU 张量是否走 CUDA IPC 共享，**未确认**，本机无 GPU 无法验证。）
>
> 于是结论不变、理由更准确：训练时每个 worker 都要**独立可写**的权重，
> `ray.put` 在这里既省不了内存、又添了一次对象存储往返，
> 8 个 worker 各自拿到的仍是**各自的副本**，**比直接 `from_pretrained` 更慢**。
> 只有「推理、只读、不需要改权重」时才值得考虑它。

**大模型的现实答案**是第三种 + HF 缓存的组合：
把权重预置到每台机器的本地 NVMe 或共享文件系统，让
`from_pretrained` 读本地路径，避免 8 个进程同时从 S3 拉 14GB。

---

## 29.8 坑清单

| 坑 | 症状 | 根因与修法 |
|---|---|---|
| **两层 DDP 打架** | `Address already in use` / `world_size` 对不上 | 用了 HF `Trainer` 又调了 `prepare_model()`。**删掉后者** |
| **嵌套 `accelerate launch`** | GPU 超配、显存 OOM 出现在「明明算好了卡数」的时候 | Ray 已经是启动器了。在 `train_func` 里构造 `Accelerator`，不要用 CLI |
| **HF 自己存 checkpoint** | 每个 worker 本地各一份完整模型，抢占恢复找不到 | `report_to=[]` + `RayTrainReportCallback`（`save_strategy` **保留**，不是设 `"no"`） |
| **checkpoint 目录嵌套** | `from_pretrained` 报找不到 `config.json`；`load_adapter` 报找不到适配器 | Ray 的 HF checkpoint 布局是 `checkpoint_00000*/checkpoint/`（issue #40082），要往下钻一层 |
| **LoRA 续训后 loss 不降** | 不报错，但模型没在学 | `peft.load_adapter()` 默认 **`is_trainable=False`** ⇒ 加载进来是冻结的。必须显式 `is_trainable=True` |
| **批量 `generate` 输出乱码** | 不报错，只是结果不对 | tokenizer 默认**右填充**。decoder-only 批量推理必须 `tokenizer.padding_side = "left"` |
| **`max_failures=0`** | 一次抖动整个训练终止 | `FailureConfig(max_failures=3)` |
| **`from_huggingface` 先下载全量** | 启动卡在「下载数据集」上几十分钟 | 数据大就直接 `read_parquet`，别经过 HF Dataset 对象 |
| **tokenizer 每个 batch 重建** | GPU 利用率低、CPU 打满 | 放进 actor 池（`ActorPoolStrategy`） |
| **闭包捕获了模块对象** | `PicklingError: cannot pickle 'module' object` | 在函数体内 import，不要靠闭包引用外层 import 的模块（mini-ray 与 Ray 都会撞上） |
| **`num_workers` × `use_gpu` 算错** | 实际用的 GPU 数和预期不符 | `use_gpu=True` = **每个 worker 1 张卡**。要 8 卡就 `num_workers=8` |
| **PEFT 存了全量权重** | checkpoint 从 20MB 变成 14GB | 检查有没有 `merge_and_unload()`；训练存适配器，部署前再合 |

---

## 29.9 与 mini-ray 的关系

诚实地说：**这一章的内容 mini-ray 一行都实现不了**，而且这是设计上的取舍。

mini-ray 明确不做 GPU 显存管理、不做 NCCL/RDMA、不做上层 AI 库
（见 `mini-ray/README.md` 的取舍清单）。第 29 章讲的全部依赖：

* 真实的 CUDA 设备与显存；
* NCCL 集合通信（第 30 章）；
* 第 2 层训练库（`transformers` / `peft` / `accelerate`）。

不过 mini-ray 仍然能演示这一章**唯一一个纯粹属于「编排层」的概念**：
**「一个函数 + N 个 worker + 每个 worker 拿到自己的 rank」这个模式本身**。

```python
import ray

@ray.remote(num_cpus=1)
def worker_step(rank, world_size):
    # 这就是 TorchTrainer 在编排层做的事:
    # 把 (rank, world_size) 注入每个进程,剩下的交给下层
    return f"rank {rank}/{world_size}"

ray.init(num_cpus=4)
try:
    refs = [worker_step.remote(r, 4) for r in range(4)]
    print(sorted(ray.get(refs)))
finally:
    ray.shutdown()
```

> **这个对照本身就说明了本章的论点**：Ray 的价值在「怎么把
> `(rank, world_size)` 送对、挂了怎么重来」，而不在「怎么算梯度」。
> 一旦你发现自己在用 Ray 实现后者，就是走错了层。

---

## 29.10 本章小结

* **三层要分清**：Ray 是编排层，`transformers`/`peft`/`accelerate` 是训练层，
  PyTorch/NCCL 是张量层。每层只解决自己的问题；
* **Ray Data ↔ HF Datasets 互转几乎零代价**（都建立在 Arrow 上），
  但数据大时不要经过 HF Dataset 对象，直接读底层文件；
* **`TransformersTrainer` / `LightningTrainer` / `AccelerateTrainer` 已经没了**
  （2.7 弃用 → 2.8 报错 → 2.9 移除）—— 用 `TorchTrainer` + HF `Trainer`，
  这是 V2 下的唯一主路径；`XGBoostTrainer` 没被删，且已 V2 化（第 34 章）；
* **`RayTrainReportCallback` 要从 `ray.train.huggingface.transformers` 导入**
  （`ray.train.huggingface` 的 `__init__` 是空的）；
* **`RayTrainReportCallback` + `save_strategy="steps"`（对齐 `evaluation_strategy`）
  + `report_to=[]`** 才是能工作的组合 ——
  ⚠️ **`save_strategy="no"` 是本节最反直觉的一条错**：回调重写的是 `on_save()`，
  HF 不存盘回调就不触发，结果是 Ray 侧**一个 checkpoint 都没有**，而且不报错；
* **HF `Trainer` 自带 DDP**，不要再调 `prepare_model()`，也不要用
  `accelerate launch` / `torchrun` 再套一层；
* **LoRA 特别适合 Ray**，因为 checkpoint 小 → 可以频繁存 → 抢占容错变便宜；
* **离线推理超过几千条就该用 vLLM**，`transformers.generate()` 只是入门形态；
* **mini-ray 覆盖不到这一章** —— 这是设计取舍，不是遗漏。

**下一章**（第 30 章）会往下走一层，讲 GPU 本身：`CUDA_VISIBLE_DEVICES`
到底怎么设、NCCL 环境变量有哪些、显存 OOM 的四种成因怎么区分。
