仓库地址：https://github.com/hhk-png/cycle-agent

# 第 12 章：Ray Data 与数据管道

> 本章目标：搞清楚 Ray Data 在什么问题上比 Spark/Dask 更合适、它的
> **流式执行引擎**到底省掉了什么、`Dataset` 的 block 模型怎么影响内存，
> 以及怎么用 `ray.data.llm` 做批推理。
> 读完你应该能判断：**这个 ETL/预处理任务该用 Ray Data 还是 Spark**，
> 以及 OOM 了从哪几个旋钮开始查。

---

## 12.1 定位：为什么 AI 负载还需要一个数据处理库

先摆一个容易被忽略的事实：**Spark 和 Dask 的抽象都是"表上的算子"，
而 AI 预处理经常不是表上的算子。**

举几个真实例子：

* 读 200 万个音频文件用 Whisper 转写，输出变长文本 —— 计算单元是
  "一个 batch 过一个模型"，不是 `SELECT ... GROUP BY`；
* 对每一行做 tokenize 再按 token 长度分桶 —— 中间状态是 Python 对象
  （`list[int]`），不是列；
* 读 50 万个视频片段抽帧、解码、增强 —— 单条记录几十 MB，"一行" 被拉爆。

共同点是：**UDF 是主体，SQL 是边角料**。Spark 能跑，但你要为"把 Python 对象
塞进 Arrow 列" 付很多税（序列化、pandas UDF 边界、driver 侧调度）。Ray Data
就是为这件事做的 —— 官方描述是「面向 AI 工作负载的、可扩展的数据处理库」
（docs.ray.io/data）。差异化在这几处：

| 维度 | Spark / Dask | Ray Data |
|---|---|---|
| 计算单元 | 表上的算子（DataFrame API 优先） | 任意 Python 函数（`map_batches`）+ 表算子 |
| 执行模型 | 按 stage 同步（Spark）/ 任务图（Dask） | **流式执行**：算子按 block 流水，不落地中间结果 |
| 与训练集成 | 需要落盘或额外连接器 | `iter_torch_batches` 直接喂 DDP 的 DataLoader |
| GPU 感知 | 弱（GPU 调度不是一等公民） | 一等公民：算子可声明 `num_gpus`，按 GPU 排队 |
| 部署模型 | 独立集群 | 与 Train/Serve 共享同一个 Ray 集群 |

最后一条是很多人选它的真正理由：**数据预处理和训练在同一集群、同一份资源池里**，
不需要"先跑 Spark 作业落地，再跑训练作业读回来"这套两段式。

### 流式执行引擎到底是什么意思

传统批处理引擎的心智模型是 **阶段（stage）**：每一步都要等上一步
**全部**完成（stage barrier），中间结果落盘或驻留内存。

```
传统：  read ──► map ──► [全部物化] ──► shuffle ──► [全部物化] ──► write
Ray：   block 1 ──► map ──► (直接推给下游) ──► ...
        block 2 ──► map ──► (直接推给下游) ──► ...
        block 3 ──► map ──► ...      ▲ read+map 逐 block → 融合成一个物理算子
```

Ray Data 的默认执行器是 `StreamingExecutor`（docs.ray.io/data/key-concepts）。
差别在四点：

1. **没有 stage barrier**（shuffle/join/sort 这类必须物化的算子除外），
   block 一算完就往下游推；
2. **中间结果不落地**，block 以对象形式在 Ray 对象存储里流动，内存不够才 spill；
3. **算子融合**：相邻的无状态算子合并成一个物理算子，省掉一次序列化 +
   对象存储往返；
4. **资源按需**：一边跑一边把 task 派给有资源的节点，不需要预先算好
   每个 stage 的并行度。

代价也很实在：**调试更难**。traceback 是异步、跨进程的；"I/O 慢还是 UDF 慢"
要靠 dashboard 的 operator 指标，不能靠打断点。

## 12.2 核心概念

### Dataset 与 block

```python
import ray

ds = ray.data.read_parquet("s3://bucket/events/")   # 此时什么都没跑
ds = ds.map_batches(lambda b: {"x": b["x"] * 2}, batch_format="pandas")
```

两个关键对象（docs.ray.io/data/key-concepts）：

* **`Dataset`**：逻辑上的分布式数据集合，是一个**惰性**的 DAG 描述 ——
  构造 `read_parquet` 的时候**不读数据**。
* **block**：物理分片，通常是 Arrow Table（也支持 pandas / 单列 ndarray），
  是调度、序列化、溢出（spill）的基本单位。

`Dataset` 的分区概念已被 block 取代，这是很多老文档对不上的地方：
`num_blocks()` 是真实分片数，`repartition()` 才显式触发 reshuffle。

### lazy vs eager

| 类别 | 例子 | 是否触发执行 |
|---|---|---|
| 变换（lazy） | `map` / `map_batches` / `filter` / `flat_map` / `select_columns` | 否，只改逻辑计划 |
| 消费（eager） | `iter_batches` / `iter_torch_batches` / `take` / `show` / `count` / `write_*` / `materialize`（旧名 `cache()`，2.4 已改名） | 是 |
| 阻塞（物化） | `sort` / `groupby` / `join` / `repartition` | 是，且会插入 barrier |

写 Ray Data 最常见的性能事故是**在循环里反复构造消费调用**：

```python
for epoch in range(10):
    for batch in ds.iter_batches(batch_size=1024):   # 反例：每次重读
        ...
```

⚠️ **`iter_batches` / `iter_torch_batches` 本身并没有"多 epoch 语义"。**
默认行为是：**每多跑一轮，整条 pipeline 就被重新执行一遍** —— 重新读文件、
重跑 UDF（issue ray-project/ray#45042 讨论的正是这条边界，以及它为什么容易
让人误以为"第二轮会变快"）。所以：

* 想让 pipeline **只算一次**：`ds = ds.materialize()`，之后每轮 `iter_*` 都读
  同一份缓存。（**`cache()` 不是它的"流式变体"，而是它在 Ray 2.4 之前的旧名** ——
  PR #34169 把它改名成了 `materialize()`，理由是「物化更像一个 action 而不是
  cache」。网上两套名字都能搜到，但它们是同一个东西，不存在两种物化语义）；
* 想每轮**换一个洗牌顺序**：`Dataset` 自身没有多 epoch 语义，走**消费端**洗牌
  （见下一节的 `local_shuffle_seed`），它不会让上游重跑；
* **别指望第二轮变快** —— 没有 `.materialize()` 就不会。

### 每轮换序：只能在消费端做

"每个 epoch 重新洗牌"这件事 `Dataset` 自己给不了。**注意别去网上找
`DatasetPipeline`（`ds.repeat().window().iter_epochs()`）那套写法** ——
它对应的 `python/ray/data/dataset_pipeline.py` 在 2.9 已标 `@Deprecated`，
到 2.58 连文件都没有了（`ray.data` 的 `__init__` 里 0 处 `DatasetPipeline`，
`Dataset.repeat` 同样不存在）。2.58 的正确答案是把洗牌放到**消费端**：

```python
ds = ray.data.range(10_000).materialize()   # 先物化：上游只算一次

for epoch in range(3):
    for batch in ds.iter_torch_batches(batch_size=256,
                                       local_shuffle_buffer_size=10_000,
                                       local_shuffle_seed=epoch):  # 每轮换 seed
        ...
```

* 关键差别：**`local_shuffle_*` 是消费端行为** —— 洗牌发生在读取侧的本地
  buffer 里，上游变换不会因此重跑；配合 `materialize()`，数据也只读同一份缓存；
* `local_shuffle_buffer_size` 越大洗得越均匀，代价是消费端的常驻内存
  （buffer 里的行要一直留着）；
* `local_shuffle_seed` 不给的话每轮顺序一样，"每轮洗牌"就只是"洗一次"；
* 如果只是要"block 顺序随机"而不重排行，还有 `randomize_block_order()`
  （全量重排是 `random_shuffle()`，贵得多）。**已核实**：2.58 的
  `python/ray/data/dataset.py` 里就是 `def randomize_block_order(`（第 2070 行），
  名字没有出入。

### 表算子：`repartition` / `sort` / `groupby` / `join` / `union` / `zip` / `limit`

前面的"lazy vs eager"表里把 `sort` / `groupby` / `join` / `repartition` 归成了
**物化 barrier**，这里给它们的签名和坑：

| 算子 | 签名（常用形态） | 语义与注意 |
|---|---|---|
| `repartition` | `ds.repartition(num_blocks, shuffle=False)` | 调 block 数。`shuffle=False` 只是拆分/合并（便宜）；`shuffle=True` 触发真正的重分布 |
| `sort` | `ds.sort(key, descending=False)` | `key` 可以是列名、列名列表，或 `Callable[[pyarrow.Table], ...]`；**全局有序 = 物化 barrier**，大表很贵 |
| `groupby` | `ds.groupby(key).map_groups(fn, batch_format=…)` | 每个分组作为一个 batch 传给 UDF，UDF 返回"一组新行"。同族还有 `.count()` / `.sum()` / `.mean()` / `.max()` / `.min()` / `.std()` / `.aggregate()` |
| `join` | `ds1.join(ds2, on=…, join_type="inner", num_partitions=N)` | ⚠️ **形参名是 `join_type`，不是 `how`**；取值 `inner` / `left_outer` / `right_outer` / **`full_outer`**（**没有** `outer`）/ `left_semi` / `right_semi` / `left_anti` / `right_anti`。**`num_partitions` 是必填**（写出多少个分区）—— 漏了会直接 `TypeError`。**两侧都要物化**，shuffle 路径见 12.3 的 Shuffle V2 |
| `union` | `ds1.union(ds2)` | 竖直拼接。两边 schema 必须兼容（列名与类型），且**不保证全局有序** |
| `random_shuffle` | `ds.random_shuffle()` | 全量重排，贵；只想要"block 顺序随机"用 `randomize_block_order()`。**已核实**：算子本身**没有**被弃用，被弃用的是它的 `num_blocks=` 形参 —— 传了会直接 `raise DeprecationWarning("...deprecated in Ray 2.9...")`（`dataset.py` 第 2047 行），要改输出块数请在 shuffle 之后调 `repartition()` |
| `zip` | `ds1.zip(ds2)` | 按**位置**横向拼列，要求两边 block 数相同、每个 block 的行数也相同。⚠️ **已弃用**（`#65111`）：它依赖全局有序，任务乱序完成时会**静默错配行** —— 请改用基于 key 的 `join`（见 12.6 的「API 收缩」） |
| `limit` | `ds.limit(n)` | 取前 n 行。并行下**不保证是哪 n 行**（要确定性就先 `sort`），且 `limit` 之后的算子仍可能被调度 |

```python
ds = ray.data.read_parquet("s3://bucket/events/")

# 分组聚合：一个 uid 一行
per_user = ds.groupby("uid").map_groups(
    lambda g: {"uid": g["uid"].iloc[0], "n": len(g["amount"]),
               "total": float(g["amount"].sum())},
    batch_format="pandas")

# 表连接（左侧保留）：行为表 ⋈ 用户画像表
# ⚠️ join_type= 而不是 how=；num_partitions= 必填
joined = ds.join(profiles, on="uid", join_type="left_outer", num_partitions=16)

# 全局排序 + 截断：最慢的 100 条
slowest = ds.sort("latency_ms", descending=True).limit(100)

# 把每天的分区拼成一个月
month = ds_day1.union(ds_day2).repartition(64, shuffle=True)
```

一个容易踩的地方：`groupby()` 本身**不触发执行**，也不返回 `Dataset` —— 你拿到
的是一个 `GroupedData`，要再调 `map_groups()` / 聚合方法才回到 `Dataset`。

### `map_batches` / `iter_batches` / `iter_torch_batches`

三个 API 的分工，看这张表就够了：

| API | 方向 | 输入 | 输出 | 典型用途 |
|---|---|---|---|---|
| `map_batches(fn, batch_size=…)` | 变换 | Arrow/pandas/numpy batch | 同类型 batch | 向量化预处理、UDF |
| `iter_batches(batch_size=…)` | 消费 | — | 默认 `batch_format="default"`（Arrow 数据给 `dict[str, np.ndarray]`） | 训练循环、自定义推理 |
| `iter_torch_batches(batch_size=…, collate_fn=…)` | 消费 | — | `torch.Tensor` batch | 直接喂 `DataLoader`/DDP |

`map_batches` 是主力。关键参数：

```python
from ray.data import ActorPoolStrategy, TaskPoolStrategy

ds.map_batches(fn,
    batch_size=1024,        # 每个 batch 的行数；传 "auto" 让 Ray 自己定（见 12.6）
    batch_format="pandas",  # "numpy" | "pandas" | "pyarrow" | None
    compute=TaskPoolStrategy(),   # 无状态：每个 batch 一个任务，用完即走
    num_gpus=0.5,           # 每个任务/actor 申请的 GPU
    zero_copy_batch=True)   # 尽量不做拷贝（有风险）
```

> ⚠️ **`concurrency=` 已经在 Ray 2.51 被弃用**（提示语原文，见
> `python/ray/data/_internal/util.py`：*"The argument ``concurrency`` is
> deprecated in Ray 2.51. Please specify argument ``compute`` instead. For more
> information, see https://docs.ray.io/en/master/data/transforming-data.html#stateful-transforms."*；
> 后续 PR #63576 把这个弃用铺到了 `filter(expr=...)` 等算子上）。
> 你在网上看到的绝大多数 Ray Data 教程都还在写 `concurrency=` —— 它们**能跑**
> （只是告警），但**已经不是推荐写法**，而且**固定池和伸缩池现在都用 `compute=`
> 表达**，不再是「`concurrency=` 管固定、`compute=` 管伸缩」的分工。
>
> 还有一个更老的拼法要认出来：`compute="tasks"` / `compute="actors"`。
> 2.58 的 `ray/data/_internal/compute.py` 里 `get_compute()` **仍然认这两个字符串**
> （降级调用不会炸），但同文件里那条报错的原文写的是
> *"In Ray 2.5, the compute spec must be either TaskPoolStrategy or
> ActorPoolStrategy"* —— 也就是说这套"字符串 spec"是 **2.5 时代**的接口。
> 现在应该写 `compute=TaskPoolStrategy()` / `compute=ActorPoolStrategy(...)`。
> （本书早先写"2.9 之前的写法"，与源码里的版本号对不上，这里更正。）

`ActorPoolStrategy` 是要重点理解的开关：UDF 在**长驻 actor** 里跑，所以加载
成本只在 `__init__` 付一次（`__init__` 里 `torch.jit.load("model.pt").cuda()`，
`__call__` 里 `torch.inference_mode()` 前向）。代价是 actor 会占住资源不放
（除非开 autoscaling pool），而且 actor 池**有状态** —— 挂了要重建，
重建期间数据流会停。

### 固定池与伸缩池：都写 `compute=`

```python
from ray.data import ActorPoolStrategy

# ① 固定池：起 N 个 actor,一直占着
ds.map_batches(Tokenizer,
    compute=ActorPoolStrategy(size=4),
    batch_size=512)

# ② 伸缩池：在 2~16 个 actor 之间跟着负载涨缩
ds.map_batches(Tokenizer,
    compute=ActorPoolStrategy(min_size=2, max_size=16),
    batch_size=512,
    num_gpus=0.25)
```

* 池子跟着**上游 block 的排队情况**涨、跟着空闲缩；
* ⚠️ **`min_size` 的下限是 `1`，传 `0` 会直接 `ValueError: min_size must be >= 1`**
  —— 池子缩不到 0。本书早先写的"`min_size=0` 允许全部缩掉"是错的
  （`ray/data/_internal/compute.py` 里显式校验）；
* 伸缩的粒度是 actor，**每次扩容都要重付一次 `__init__`**（加载模型、预热
  CUDA）—— 所以冷启动贵时应该把 `min_size` 设大（**至少 1**），
  让它别缩到没有；
* `max_size` 只是上限，实际能起几个还受集群可用资源约束（`num_gpus=1` 且
  `max_size=8` 但集群只有 4 张卡 → 最多 4 个）；
* 有状态 UDF 的构造参数走 `fn_constructor_args` / `fn_constructor_kwargs`，
  而不是闭包或全局变量（actor 是独立进程，闭包里的对象要能被序列化）：

```python
class Classifier:
    def __init__(self, model_name, device="cuda"):
        self.model = load(model_name).to(device).eval()

ds.map_batches(Classifier,
    fn_constructor_args=("distilbert-base-uncased",),
    fn_constructor_kwargs={"device": "cuda"},
    compute=ActorPoolStrategy(min_size=1, max_size=4), num_gpus=1)
```

这两个参数**只在 actor 模式（`ActorPoolStrategy`）下有意义**
（`TaskPoolStrategy` 下传了的行为**未确认**，别那么写）。

### 异步 UDF 与生成器 UDF

`map_batches` 的 UDF 除了普通函数/可调用类，还吃 `async def` 和生成器：

```python
class AsyncEmbedder:
    """UDF 大部分时间在等外部服务（HTTP/向量库），等的时候可以并发。"""
    async def __call__(self, batch):
        import asyncio
        embs = await asyncio.gather(*[self.embed_async(t) for t in batch["text"]])
        return {"text": batch["text"], "emb": embs}

ds.map_batches(AsyncEmbedder,
    compute=ActorPoolStrategy(size=4),
    max_concurrency=8,     # 单个 actor 上同时最多 8 个 batch 在跑
    batch_size=16)
```

```python
class Chunker:
    """生成器 UDF：输入 1 行 → 输出 N 行，且不必先攒出整个输出 batch。"""
    def __call__(self, batch):
        for text in batch["text"]:
            for chunk in split_into_chunks(text, 512):
                yield {"chunk": chunk}      # 每次 yield 一个 batch，成为下游的 block
```

* 异步 UDF 必须配 actor 模式（Ray 在 actor 的事件循环里跑它）—— 这条路径解决的是
  "I/O 等待占满 worker"的问题，不是算力不够；`max_concurrency` 只对异步 UDF 生效；
* 并发的异步 UDF 会让**输出顺序变得不可预测**（`preserve_order` 管的是 block
  级顺序，管不了同一 actor 内多个 batch 的完成顺序）；
* 生成器 UDF 解决的是"输出远大于输入"和"峰值内存"两件事（切长文档、切视频片段、
  流式吐 token）；代价是**下游的"一个 block"不再对应"一个输入 batch"**，依赖
  `batch_size` 的调优经验会失灵；
* `ray.data.llm` 的 processor（12.4）内部就是这套异步 + 生成器的组合。

### 消费端的旋钮：`iter_batches` / `iter_torch_batches`

训练循环里真正要调的参数都在消费端：

```python
# 用法 A：要"逐列 dtype"（此时**不能**传 collate_fn）
for epoch in range(config["epochs"]):
    for batch in shard.iter_torch_batches(
            batch_size=256,
            dtypes=torch.float32,          # 或 {"label": torch.long}，逐列指定
            device="cuda",                 # yield 之前就搬到 GPU
            local_shuffle_buffer_size=10_000,
            local_shuffle_seed=epoch,      # 每轮换 seed 才有"换序"效果
            prefetch_batches=2,
            drop_last=True):
        ...
```

```python
# 用法 B：要自定义拼 batch（此时**不能**传 dtypes，dtype 转换写进 collate_fn）
def to_xy(batch: dict) -> tuple:
    return (torch.as_tensor(batch["x"], dtype=torch.float32),
            torch.as_tensor(batch["label"], dtype=torch.long))

for batch in shard.iter_torch_batches(
        batch_size=256,
        device="cuda",
        collate_fn=to_xy,                  # 默认 None → dict[str, Tensor]
        local_shuffle_buffer_size=10_000,
        local_shuffle_seed=epoch,
        prefetch_batches=2,
        drop_last=True):
    ...
```

> 🔴 **`dtypes` 与 `collate_fn` 互斥，同时传会直接抛 `ValueError`。**
> 源码里的守卫（`python/ray/data/iterator.py`）：
> `if collate_fn is not None and dtypes is not None: raise ValueError(...)`，
> 文案是 *"collate_fn cannot be used with dtypes. You should manually convert
> the output Torch tensors to the desired dtype inside collate_fn."*
> `iter_torch_batches` 自己的 docstring 也写着 *"You can't use this parameter
> in conjunction with `dtypes`."*
> **两个都要，就把 dtype 转换放进 `collate_fn`**（即上面的用法 B）。

| 旋钮 | 作用 | 注意 |
|---|---|---|
| `batch_size` | 每次 yield 的行数 | ⚠️ **两个消费 API 的默认都是 `256`**（`iter_batches` / `iter_torch_batches` 的签名里写的都是 `batch_size: Optional[int] = 256`），**不是**"按 block 给"。即便如此，训练循环仍应显式写 —— 默认的 256 是个与显存/步数无关的通用值 |
| `local_shuffle_buffer_size` | 消费端维护一个本地缓冲，随机采样后 yield | **缓冲要 ≥ `batch_size`**，否则等于没洗；只在 block 内洗，跨 block 不洗 |
| `local_shuffle_seed` | 洗牌种子 | **不换 seed，每轮顺序完全一样**；想每轮不同就传 epoch |
| `prefetch_batches` | 预取几个 batch 备着 | `0` = 不预取（计算与消费完全串行）；预取越多越吃内存 |
| `dtypes` | 转 dtype，省掉训练循环里的 `.float()` | 只对 `iter_torch_batches` 有 |
| `device` | `"cuda"` / `torch.device` | 同上；直接省掉 `.to(device)` |
| `drop_last` | 丢掉最后不足一个 batch 的数据 | DDP 下想固定步数就用它 |
| `collate_fn` | 自定义拼 batch | 想把 dict 直接变成 `(x, y)` 就写它 |
| `batch_format` | `iter_batches` 专用：`"numpy"`/`"pandas"`/`"pyarrow"`/`"default"`。⚠️ **默认是 `"default"`**（对 Arrow 数据 = `dict[str, np.ndarray]`），不是 `"numpy"` —— 但 `"default"` 对**非 Arrow 数据**会给回原对象，所以想拿到确定的 numpy 就显式写 `"numpy"` | `iter_torch_batches` 没有这个参数（给的就是 tensor） |

### 切分：`split` / `streaming_split` / `train_test_split`

```python
train, test = ds.train_test_split(test_size=0.2, shuffle=True, seed=42)

shards = ds.split(n=8, equal=True)          # → list[Dataset]，切成 8 份

splits = ds.streaming_split(n=4, equal=True,
                            locality_hints=["node-1", "node-2", "node-1", "node-2"])
for batch in splits[0].iter_batches(batch_size=256):   # 每个消费者各拉各的
    ...
```

**为什么 `streaming_split` 对分布式训练重要**：

* `split(n)` 是"物化 + 切"：先把数据算出来（并驻留在对象存储里），再切成 n 份
  `Dataset`；而 `streaming_split(n)` 返回 n 个**迭代器**，不物化整份数据 ——
  每个 worker 自己从上游拉自己的那一份，内存占用是"流"的量级；
* `locality_hints` 告诉 Ray 每个消费者在哪台机器上，让分片尽量落在本地节点，
  省掉跨节点流量 —— 这就是多机训练里"数据贴近 GPU"的开关；
* **这就是 Ray Train 那边 `get_dataset_shard()` 底下干的事**（13.3）：每个 worker
  拿到一个互不重叠的流，而不是"driver 全量取回来再切"；
* 另外还有 `ds.split_at_indices([...])`（按行号切）和 `ds.split_proportionately([...])`
  （按比例切）—— **两个都已核实存在**（2.58 的 `dataset.py` 第 2545 行与第 2625 行）；
  `equal=True` 要求能均分，做不到时的报错条件**未确认**。

### 背压（backpressure）

流式执行器最怕**上游把下游/对象存储冲爆**。机制是 `ResourceManager` +
可插拔的 `BackpressurePolicy`：跟踪每个算子的 CPU/GPU/对象存储占用，
超预算就**不再派新 task**；`OutputBackpressureGuard` 是预算收得太紧、
可能死锁时的逃生通道。

```
上游 task ──► 对象存储 ──► 下游 task
     ▲              │
     │              ▼
  节流 ◄──── ResourceManager 监控 CPU/GPU/对象存储用量
              （超额 → 停止派 task，直到下游消费掉）
```

对使用者的直接含义：

* **driver 消费太慢会把上游憋住** —— 在 `iter_batches()` 里 sleep，
  整个 pipeline 会跟着慢下来，这是特性不是 bug；
* **最后一个算子不要返回特别大的输出**，消费端只有一个进程，会变成串行瓶颈；
* 训练时可以反过来用 `DataContext` 把对象存储预算压低（比如 2GB），
  多让内存给训练（docs.ray.io/data/memory-management）—— **具体 API 见 12.6 末尾**。

### 一张补齐的表：正文没展开的表算子

第 12 章的主体是"数据管道"，容易让人以为 Ray Data 只有 `map_batches` + `filter`。
下面这些是**同一套 `Dataset` 上的常规算子**，本书前四轮没有系统列过 ——
它们决定了"你需不需要为了几个聚合操作回去上 Spark/pandas"。

| 算子 | 签名要点 | 什么时候用 |
|---|---|---|
| `ds.unique(column)` | 返回该列的**去重值列表**（`List[Any]`，**不是** `Dataset`）。⚠️ 它会在 driver 上**物化**，值多时会撑爆 driver 内存 | 类别基数探查、拼枚举表（先确认基数不大） |
| `ds.aggregate(agg)` | 传**聚合对象**：`Count(on="uid")` / `Mean(on="amount")` / `Sum` / `Min` / `Max` / `Std` | 全量统计。⚠️ 别和 `groupby().aggregate()` 混 |
| `ds.groupby("k").aggregate(Sum("v"))` | 分组聚合 | 特征统计、样本权重 |
| `ds.rename_columns({"old": "new"})` | 改名 | 对齐下游 schema |
| `ds.with_column("c", col("a") * 2)` | 用**表达式**（`Expr`，`ray.data` 的 `col` / `lit`）加或覆盖一列 | 列间算术、常量列、简单派生列。**它不吃 `lambda row: ...`** |
| `ds.add_column("c", ...)` | ⚠️ **2.58 已弃用**（提示语：*Use `with_column` API instead*） | 新代码一律改用 `with_column` |
| `ds.iter_rows()` | 逐行产出 `dict` | 小数据、或需要逐行调用外部 API。**大表不要用**（Python 层循环） |

> ⚠️ **`with_column` 与 `add_column` 不是"两种回调粒度"，而是"新 API 与旧 API"。**
> `with_column` 吃的是**表达式** `Expr`：官方例子是
> `ds.with_column("id_2", col("id") * 2)`，走的是向量化的表达式求值，**不是**
> 逐行 Python 回调（写成 `lambda row: ...` 是错的）。`add_column` 在 2.58 已被
> 标 `@Deprecated`，官方文档不会再推荐它。真有逐行 / 按批的**任意 Python 逻辑**、
> 或者要在里面调外部库时，两条路都不合适 —— 用 `map_batches`。
>
> ⚠️ 另外，2.58 的 `Dataset` 上**没有** `to_torch(feature_columns=…,
> label_column=…)` 这个算子（已被移除；`DataIterator.to_torch` 还在但本身也标了
> 弃用，别依赖它）。要拿张量喂原生 PyTorch，出口只有 `iter_torch_batches(...)`
> —— 它是流式的，正是要这个才不至于把整表读进内存。

### 想关掉流式执行器怎么办：**关不掉了**

前面花了大量篇幅讲**流式执行器**（streaming executor）—— 它自 2.x 起是默认。
老教程里有一个"关掉它"的写法：

```python
# ❌ 这段代码在 2.58 上不会报错，但也完全不起作用 —— 见下
from ray.data import DataContext
ctx = DataContext.get_current()
ctx.use_streaming_executor = False     # 回到旧的 bulk executor
```

🔴 **这段是错的，别再抄**。两条独立的证据：

1. **`DataContext` 上没有 `use_streaming_executor` 这个字段**
   （2.58 的 `python/ray/data/context.py` 里零命中）；
2. **bulk executor 本身已经不存在** —— 整个仓库里只有
   `python/ray/data/_internal/execution/streaming_executor.py`，
   没有任何 `bulk_executor.py`。所以"回退到 bulk 路径"这个能力本身没了。

⚠️ **最坏的地方是它不报错**：`DataContext` 定了一个自定义的
`__setattr__`（`context.py` 第 1044 行起），里面是一长串
`if/elif` 分支 —— 用来给若干**已知**的弃用字段发警告（`use_polars`、
`use_arrow_tensor_v2`、`target_shuffle_max_block_size` …）。
它**没有兜底的 `else: raise`**，所以给一个不存在的字段赋值会
**静默写进实例、然后什么都不发生**。你在 Stack Overflow 上看到
"设了 `use_streaming_executor=False` 就回到旧执行器"，照抄的后果是
**代码看起来生效了、行为其实一点没变**。

**那还能调什么**：执行器的可调项现在全部挂在
`DataContext` / `ExecutionOptions` 上（下一节），
而不是"换一个执行器"。如果遇到流式背压与负载不合，
该动的是 `ExecutionOptions.resource_limits`（对象存储 / 内存预算）
与算子级的 `num_cpus` / `memory=` 声明。

## 12.3 数据源与数据汇

| 读 | 说明 | 状态 |
|---|---|---|
| `read_parquet` | 主力。支持列裁剪/谓词下推，2.57 起默认走 DataSourceV2 | |
| `read_csv` / `read_json` | 分块读 / NDJSON 语义，注意 schema 推断开销 | |
| `read_text` | 纯文本：一个 `text` 列（`include_paths=True` 时再加 `path`）。语料/日志预处理最省事 | |
| `read_binary_files` | `bytes` + `path` 两列，**不解码**；自己写解码逻辑时用它 | |
| `read_tfrecords` | TFRecord 记录流。**已核实依赖**：`tensorflow`（`tf.train.Example`）与 `tensorflow_metadata`（`tensorflow_metadata.proto.v0.schema_pb2`，`tf_schema=` 用），见 `_internal/datasource/tfrecords_datasource.py` 的 `import` 块 | |
| `read_webdataset` | WebDataset 的 tar 分片；解码方式由 `decoder` 参数决定 | 未确认 |
| `read_images` | 图像目录/路径列 → 解码后的 ndarray（多模态预处理常用） | |
| `read_videos` | 视频解码成帧；单条记录几十 MB，先读 12.6 的坑 1 | 未确认 |
| `read_audio` | 音频解码成波形 / ndarray | 未确认 |
| `read_numpy` / `read_pandas` / `read_arrow` | 从内存对象或文件读 | |
| `read_delta` / `read_iceberg` / `read_hudi` | 表格式湖仓 | |
| `read_sql` | 读任意 SQL 结果集（走数据库连接） | **alpha**（`read_api.py` 里是 `@PublicAPI(stability="alpha")`，已核实） |
| `read_mongo` | MongoDB 集合；需要 `pymongo` | **alpha**（同上，`@PublicAPI(stability="alpha")`，已核实） |
| `read_avro` | Avro 文件；**依赖已核实为 `fastavro`**（`avro_datasource.py` 里 `_check_import(self, module="fastavro", package="fastavro")`，再用 `fastavro.reader(f)`） | |
| `read_kafka` | 流式源（有限批语义） | |
| `read_zarr` / `read_lerobot` | **2.57 起**就有 —— 2.56 的 `read_api.py` 里两者都还没有，2.57/2.58 都在（本书早先写"2.58 新增"不准确，这里更正） | |
| `from_huggingface` | 从 HF `datasets.Dataset` 转过来 | **stable**（源码里是**裸 `@PublicAPI`**，而 `stability` 的默认值就是 `"stable"`） |
| `from_torch` | 从 `torch.utils.data.Dataset` 转过来；**单机、按条搬运**，只适合小数据。**已核实**：文档原文是 *"The data will be sequentially streamed with one single read task"*，实现里写死 `override_num_blocks=1`（另有一个 `local_read=` 开关，会加 `label_selector` 把读任务钉在当前节点上） | |
| `from_spark` / `from_dask` / `from_modin` | 从其它框架搬过来 | |

> **状态列怎么读**：空白 = 该 API 存在已久、文档未特别标注稳定性；标
> **未确认**的，是本次检索无法从官方 API 页确认其稳定性标注的项。
> 无论哪一类，**上生产前都请查当前版本 API reference 的 `PublicAPI` 标注** ——
> Ray Data 的连接器逐版本增补，稳定性标签变得比 API 本身还快。

> ⚠️ **这张表不是全集**（2.58 的 `read_api.py` 里还有）：`read_mcap`（机器人
> MCAP 日志）、`read_lance`、`read_bigquery`、`read_clickhouse`、
> `read_snowflake`、`read_databricks_tables`、`read_unity_catalog`、
> `read_delta_sharing_tables`，以及 `from_daft` / `from_mars` / `from_tf`。
> 判断某个 reader 在不在你的版本里，最快的办法是
> `python -c "import ray.data as d; print([n for n in dir(d) if n.startswith('read_')])"`。

| 写 | 说明 | 状态 |
|---|---|---|
| `write_parquet` | 默认，支持 `partition_cols` | |
| `write_csv` / `write_json` / `write_numpy` | 格式输出 | |
| `write_tfrecords` | TFRecord sink，**已核实存在**（`dataset.py` 第 5206 行，`@PublicAPI`）。⚠️ 本书早先说"写侧没有对应 sink"是错的；它还带 `tf_schema=` / `mode=`（`APPEND` / `OVERWRITE`） | |
| `write_webdataset` | WebDataset tar 分片 sink，**已核实存在**（第 5317 行，标 `stability="alpha"`）。同样纠正了早先的说法 | alpha |
| `write_images` | 把图像列落成图片文件（`read_images` 的反向） | 未确认 |
| `write_mongo` | 写 MongoDB；需要 `pymongo` | 未确认 |
| `write_delta` | Delta Lake。**APPEND / OVERWRITE**，driver 侧对事务日志做一次原子提交（docs.ray.io 的 `Dataset.write_delta` API 页） | |
| `write_iceberg` | Iceberg，**alpha**。支持 append / upsert / overwrite，`IcebergDatasink` 把 DataFile 元数据回传 driver 做原子提交 | |
| `write_kafka` | Kafka sink | |
| `write_delta(..., catalog=DatabricksUnityCatalog(...))` | 写 Unity Catalog。⚠️ **没有 `write_databricks_table` 这个方法**（`write_unity_catalog` 同样不存在）—— UC 是**走 `write_delta` 的 `catalog=` 参数**：传一个 `DatabricksUnityCatalog` 就落到 UC 管理的表上，非 UC 环境则退回普通 Delta 路径。⚠️ **版本分两段**：Catalog 抽象是 **2.57** 落地的，`write_delta` 本身是 **2.58** 才有的（见第 19 章 §19.2） | |
| `to_spark` / `to_dask` | 转成对方的数据结构。`to_spark` **需要 RayDP** | |

写侧**有**与 `read_tfrecords` / `read_webdataset` 对称的 sink
（`write_tfrecords` / `write_webdataset`，上一张表里已列出）——
⚠️ **本书早先说"没有"是错的**，这里更正。真正**没有**对应 sink 的是
`from_huggingface`（HF `datasets.Dataset`）与 `from_spark` 的**写回**方向：
要落成这些格式，用 `map_batches` 自己序列化再走文件类 sink，
或者干脆回上游框架写。

### 表格式与流式数据源：Delta / Hudi / Kinesis

上面两张表把"有哪些 reader / writer"列全了，但**表格式湖仓**与**流式源**这两块
前几轮几乎没展开。这一小节补齐，并且**把不支持的地方明写出来** ——
这一段比"支持什么"更重要。

#### Delta Lake：唯一一个**读写都齐**的表格式

```python
import ray

# 读：deltalake 库解析事务日志 → PyArrow dataset
ds = ray.data.read_delta(
    "s3://bucket/lake/trips/",
    version=42,                       # 时间旅行：不指定就读最新版本
    columns=["city", "fare"],         # 列裁剪
    storage_options={"AWS_ACCESS_KEY_ID": "...", "AWS_SECRET_ACCESS_KEY": "..."},
)

# 写：worker 写 Parquet，driver 用 deltalake 提交一次事务
ds.write_delta("s3://bucket/lake/summary/", mode="append", partition_by=["city"])
```

已核对的要点（2.58 的 `read_api.py:5058` / `dataset.py:4842`）：

* **读走 `deltalake`（delta-rs）库**：它读事务日志后构造 PyArrow dataset，
  所以**表级统一 schema 会被保留** —— 老文件缺的列会被 null 填充
  （schema evolution），Delta 日志里的列统计还能做 row-group 剪枝；
* **一次只能读一张表**：`path` 的文档明确写 *"Multiple tables are not supported"*。
  多表请自己循环或在上游解决；
* **写只支持 `APPEND` / `OVERWRITE`**（`mode=SaveMode.APPEND` 是默认值）。
  **没有 MERGE / upsert** —— 要 upsert 得自己写逻辑；
  新版本还加了 `schema_mode`（默认 `"merge"`，`"error"` 会在 schema 变化时拒绝写入）；
* **`write_delta` 没有 `partition_by` 之外的分区魔法**，`partition_by=["dt"]`
  就是按列值切目录；
* **Unity Catalog 走 `catalog=` 参数**（传 `DatabricksUnityCatalog(...)`），
  而不是另一个方法名 —— `write_databricks_table` / `write_unity_catalog`
  这两个方法**在 2.58 的 `Dataset` 上不存在**。

#### Apache Hudi：**只能读，不能写**

```python
ds = ray.data.read_hudi(
    table_uri="s3://bucket/hudi/trips",
    query_type="snapshot",                                  # 或 "incremental"
    filters=[("city", "=", "san_francisco")],                # ⚠️ 见下
    hudi_options={"hoodie.read.file_group.start_timestamp": "20230101123456789"},
)
```

已核对的要点（`read_api.py:3627`）：

* **实现依赖的是 `hudi-rs`**（`_internal/datasource/hudi_datasource.py`，
  文档指向 `github.com/apache/hudi-rs`）—— 这是 **Rust 版 Hudi reader**，
  不是 Spark 的那套 Java 栈。**它只读，没有写**；
* 🔴 **`write_hudi` 不存在** —— `_internal/datasource/` 目录下有
  `hudi_datasource.py`，**没有 `hudi_datasink.py`**。要写 Hudi，
  现在只能回 Spark/Flink（Hudi 的写入本来也是靠那两套）；
* `query_type` 只支持 `"snapshot"` 与 `"incremental"` 两种；
* ⚠️ **`filters` 只在分区列上生效** —— 文档原话是
  *"Currently, only filters on partition columns will be effective"*。
  写非分区列的 filter **不报错、也不过滤**，这是最容易静默出错的地方；
* 存储支持：本地路径、S3、GCS。

#### Iceberg：读写都有，但写是 **alpha**

`read_iceberg` / `write_iceberg` 都在，写侧标 alpha，支持
append / upsert / overwrite，靠 `IcebergDatasink` 把 DataFile 元数据回传
driver 做原子提交（详见上面写表的那一行）。

#### AWS Kinesis：**Ray Data 完全没有这个连接器**

🔴 **这一条是本节最需要照实说的**：Ray Data **没有 Kinesis 支持**。

判定依据（可复现）：在 `ray-2.58.0` 的整棵源码树里检索 `kinesis`，
**文件路径零命中**；`python/ray/data/read_api.py` 里没有任何 `read_kinesis`，
`python/ray/data/_internal/datasource/` 目录下也没有 `kinesis_*.py`。
`ray.data` 的 `__init__.py` 导出清单里同样没有它。
（**唯一一处内容命中**是 `datasource_v2` 里 `DatasourceCategory` 的 docstring
写了 `- STREAMING: Unbounded sources (kafka, kinesis)` ——
那只是把 kinesis 列为「无界源」这一类的**举例**，不构成连接器。）

```bash
# 自己复核：
curl -s https://api.github.com/repos/ray-project/ray/contents/python/ray/data/_internal/datasource?ref=ray-2.58.0 | grep -i kinesis   # 空
```

**那 AWS 上要接 Kinesis 怎么办**（按推荐度）：

1. **Kinesis → S3（Kinesis Data Firehose）→ `read_parquet`**。
   微批落 S3 是 AWS 上的标准形态，也最省事：Ray Data 侧只看见普通对象存储；
2. **Kinesis → Kafka（Managed Streaming for Kafka / MSK）→ `read_kafka`**。
   多一跳，但能吃到 Kafka 的 offset 语义；
3. **自己写 `Datasource`**：实现 `Datasource` 接口并把 `get_read_tasks()`
   包一层 Kinesis `GetRecords` / 增强扇出（EFO）。工作量不小，
   而且要自己处理 checkpoint 与分片再平衡。

#### Kafka：有连接器，但语义**是有界批**，不是流

```python
ds = ray.data.read_kafka(
    topics="events",
    bootstrap_servers="broker:9092",
    start_offset=0,
    end_offset=1000,          # 或 datetime、或 {"earliest"|"latest"}、或按分区给
    trigger="once",           # ⚠️ 目前**只支持 "once"**
)
ds.write_kafka(...)           # 写侧：kafka_datasink.py
```

核对到的边界（`read_api.py:5248` 与 `_internal/datasource/kafka_datasource.py`）：

* **只做"有界读"**：`trigger` 的类型标注就是 `Literal["once"]`，
  文档原话 *"Only the `once` trigger is supported for now, which performs a
  single bounded read"*。它**不是**流式引擎 —— 读到一个 offset 就结束；
* **每个分区只有一个读 task**：文档明写
  *"Currently we only have one read task for each partition"* ——
  分区数就是读并行度的上限，Kafka 那边分区少的话 Ray 这边加 task 没用；
* **key / value 回来的是裸 `bytes`**：Ray 不做反序列化
  （"to support any serialization format"），JSON / Avro / Protobuf
  都靠你自己 `map` 解码；
* 依赖是 **`confluent-kafka`**（不是 `kafka-python`）；
* `KafkaAuthConfig` 的字段会被映射成 librdkafka 的配置名
  （`security_protocol` → `security.protocol` 之类）。

#### 表格化：支持与不支持的现状

| 能力 | Delta | Hudi | Iceberg | Kinesis | Kafka |
|---|---|---|---|---|---|
| Ray Data 能读 | ✅ `read_delta` | ✅ `read_hudi` | ✅ `read_iceberg` | ❌ **无连接器** | ✅ `read_kafka` |
| Ray Data 能写 | ✅ `write_delta`（APPEND / OVERWRITE） | ❌ **无 `write_hudi`** | ✅ `write_iceberg`（alpha） | ❌ | ✅ `write_kafka` |
| 时间旅行 / 增量读 | ✅ `version=` | ✅ `query_type="incremental"` | ✅ `snapshot_id=` | — | ✅ 按 offset / 时间 |
| 谓词下推 | ✅（含列统计剪枝） | ⚠️ **仅分区列** | ✅ | — | — |
| 一次多表 | ❌ 不支持 | ❌ | ❌ | — | ✅（多 topic） |

**还要诚实写出的三个"没有"**：

1. **没有无界/流式源**。所有 reader 都是"跑一次、有界结束"的语义；
   Ray Data 路线图（issue #58665）里 **"Unbounded data source"** 还是
   **待办项**，不是现状。想要"常驻流处理"，现在是 Ray Data 之外的事；
2. **没有 exactly-once sink**。Delta / Iceberg 的写入靠 driver 侧一次原子提交
   拿到"整批要么全成"的语义，但**上游重跑会造成重复数据** ——
   幂等要自己在业务键上做；
3. **第三个连接器家族只覆盖一半**：除了 Hudi 只有读没有写，
   `from_huggingface`（HF `datasets.Dataset`）同样**只有读没有写**
   （见上一节末尾的说明）。

> **采集侧的实用建议**：如果你的源是"事件流"，
> **先想清楚需不需要"流"**。Ray Data 的强项是"大规模、有界、UDF/GPU 密集的
> 批处理"；真流式（秒级延迟、常驻、状态）该用 Flink / Spark Structured
> Streaming / Kafka Streams，把结果落成 Parquet / Delta 之后再交给 Ray Data。
> 反过来，把 Kinesis 硬塞进 Ray Data 通常是在拿批引擎模拟流引擎。

### 迁移提示：DataSourceV2 与 Hash Shuffle V2

这是 2.55–2.58 之间 Ray Data 最大的内部改动，表现为**行为变化**而非
API 变化，所以特别容易踩。

**DataSourceV2**（Ray 2.57 起**默认开启**，`#64821`）：重写文件读取的
scan/listing 路径，收益是 **row-group 级别的分块**（不再按整文件切）和
**谓词下推拆分**。⚠️ **作用范围比名字听起来窄**：开关
`use_datasource_v2` 目前**只在 `read_parquet()` 一处被读取**
（穷尽 grep：2.58 里它的读取点只有 `read_api.py:1576`，在 `read_parquet` 内；
其余命中都在 `context.py` 的定义/docstring 与测试里），
其余 `read_*` API 仍走 V1 —— 别以为换个 reader 也自动换了实现。
它在 2.56/2.57 之间出过一次事故，值得记住：
`ReadFiles.infer_metadata()` 早期拿不到 `size_bytes`，hash shuffle 的
aggregator 回落到"每个 1 GiB"的兜底值，TPC-H Q9 SF=100 的 autoscaling
release test 直接把 aggregator actor 撑到宿主机 OOM，于是
`DEFAULT_USE_DATASOURCE_V2` 被临时改回 `False`（PR #63674），修好后再打开。

**Hash Shuffle V2**（`#63598` 起，**2.58 才支持 `join`**）把旧的
**aggregator actor pool 换成两个无状态算子**：

```
V1（旧）：map ──► HashShuffleAggregator ──► reduce
                 （actor 池，分片常驻 actor 堆内存，
                   Ray 看不见、不能 spill）

V2（新）：map ──► ShuffleMapOp ──► ShuffleReduceOp
                 （两个无状态 task 算子，分片走对象存储，
                   能 spill、能被常规的调度/背压/资源核算管住）
```

为什么这是好事：V1 的 aggregator 内存**既不透明也不可回收**，capacity
必须在规划阶段就预留（估错了就 OOM）；V2 走对象存储之后，中间状态能被
spill、能被 lineage 追踪、能复用标准调度。barrier 还在（shuffle 天然要等），
但 barrier 之外的机制全统一了。
（来源：PR #63598、Anyscale 博客 "Shuffle V2 in Ray Data"）

**你该做什么**：如果 shuffle 密集作业在 2.56→2.57 之间内存行为变了，
先看是不是 DataSourceV2 带来的分区数变化；实在不行可以
`DataContext.get_current().use_datasource_v2 = False` 回退
（该开关 2.57 时仍存在，后续是否移除**未确认**）。

## 12.4 Ray Data LLM

这是 Ray Data 上最"AI 原生"的一块：**把 vLLM/SGLang 的离线批推理包装成一个
Ray Data 算子**。

```python
from ray.data.llm import vLLMEngineProcessorConfig, build_processor

config = vLLMEngineProcessorConfig(
    model_source="Qwen/Qwen2.5-7B-Instruct",
    engine_kwargs=dict(tensor_parallel_size=1, max_model_len=4096,
                       enable_prefix_caching=True),
    concurrency=4,          # 同时起几个 GPU stage（见下）
                            # ⚠️ 这是 processor config 自己的字段，
                            #    与 §12.2 被弃用的 map_batches(concurrency=)
                            #    不是同一个参数
    batch_size=64,          # 每个 stage 的推理 batch
)

processor = build_processor(
    config,
    preprocess=lambda row: {
        "messages": [{"role": "user", "content": row["prompt"]}],
        "sampling_params": {"temperature": 0.0, "max_tokens": 512},
    },
    postprocess=lambda row: {"answer": row["generated_text"], **row},
)

ds = ray.data.read_parquet("s3://bucket/prompts/")
out = processor(ds)      # processor 是「可调用对象」，直接套在 Dataset 上
out.write_parquet("s3://bucket/answers/")
```

三个设计点：

* **`processor(ds)` 而不是 `ds.map_batches(...)`**。processor 拿到 `Dataset`
  后自己决定怎么切分、怎么插 stage，返回一个**新的 Dataset** —— 它仍在惰性
  DAG 里，后面可以继续 `.filter()` / `.write_*()`。
* **`concurrency=n` 自动扩 GPU stage**。⚠️ 传**整数** `n` 时它是**伸缩池**：
  CPU stage 与 GPU stage 都用 `(1, n)` 的 autoscaling pool —— 等价于
  `map_batches(..., compute=ActorPoolStrategy(min_size=1, max_size=n))`，
  **不是** `ActorPoolStrategy(size=n)` 那种固定池（对照 §12.2「固定池与伸缩池」）。
  要显式给上下界就传元组 `(m, n)`。好处是你不用自己写 actor 池、自己算
  `num_gpus`，模型加载、tokenize、detokenize、采样参数都封装好了。
  > ⚠️ **别把两个 `concurrency` 搞混**：这里是 `vLLMEngineProcessorConfig`
  > 自己的字段，**没有**被 §12.2 那条弃用（PR #57035）波及；
  > 弃用的是 `map_batches(concurrency=)`。同名、不同物。
* **preprocess / postprocess 是普通 Python 函数**，会各自被插成 stage 前后的
  轻量算子。重活放 preprocess 会让 GPU 挨饿 —— 它按 batch 串在 GPU stage 前面。

### vLLM 与 SGLang 两种引擎

同一个 `build_processor` 吃不同的 engine config，**换引擎基本只改 config 类名**。
但引擎特有的 `engine_kwargs` 不通用（vLLM 的 `enable_prefix_caching`、
SGLang 的 `radix_attention` 之类）。

| Config | 引擎 | 状态 |
|---|---|---|
| `vLLMEngineProcessorConfig` | vLLM | beta |
| `SGLangEngineProcessorConfig` | SGLang | beta |
| `HttpRequestProcessorConfig` | 任意 OpenAI 兼容 HTTP 端点 | beta |
| `ServeDeploymentProcessorConfig` | 已经跑着的 Ray Serve 部署 | beta |

### 稳定性：如实说

**`ray.data.llm` 目前仍是 beta**（Ray 2.58 API reference）。依据：

* `vLLMEngineProcessorConfig` 的 API 页标注
  `PublicAPI (beta): This API is in beta and may change before becoming stable.`
  （docs.ray.io/en/latest/data/api/doc/ray.data.llm.vLLMEngineProcessorConfig.html）；
* Ray 的 "Promote LLM APIs to beta / stable tracker"（issue #61248）把这一批
  API 列为 beta，并讨论向 stable 推进的计划 —— **有升级打算但还没升**。

两个说明它确实还在动的已知问题：

* `build_processor` 在 vLLM ≥ 0.19 上会因 `vllm.inputs.data` 被移除而报
  `AttributeError`（issue #64275，修复见 PR #64337）。**引擎版本与
  `ray.data.llm` 的耦合是真实风险**，上生产要锁版本。
* 旧的 `build_llm_processor` 已被**移除**（PR #63569）；`apply_chat_template=True`
  这类布尔开关**已弃用**，改为 `chat_template_stage=ChatTemplateStageConfig(...)`
  （类定义在 `python/ray/data/llm.py`，同族还有 `TokenizerStageConfig` /
  `DetokenizeStageConfig`；也接受 `dict`）。
  `vLLMEngineProcessorConfig` 的 docstring 写的是
  `chat_template_stage: Chat templating stage config (bool | dict | ChatTemplateStageConfig)`，
  而旧的布尔 flag（`apply_chat_template` / `tokenize` / `detokenize`）
  *"are deprecated but still supported with deprecation warnings"*。
  网上 2.4x 时代的示例大多已经跑不通。

## 12.5 与 Spark / Dask 的分工与批评

**Ray Data 不是"更快的 Spark"。** 社区的批评很具体：

> 任意 SQL 查询在 Ray Data 上不一定比 Spark 快。

这不是黑它，是它自己路线图承认的。Ray Data Q4 Roadmap（issue #58665）
列了几条"把规模化能力补上"的目标：

* **scalability envelope**：发布 Ray Data 的规模边界 + 改进路线图，
  措辞是 "make Ray Data competitive with other data processing frameworks"；
* **External Shuffle Service（Apache Celeborn 集成，#58687）**：把 shuffle 从
  Ray 自己的对象存储里挪出去，交给专门的 shuffle service；
* **Ibis 集成**：社区在 issue 里追问这是"把 Ray Data 当 Ibis 后端"还是
  "给 Ray Data 加 DataFrame API"，**这条细节未确认**。

这清单读出的是定位：**强项在 UDF 密集、GPU 密集、与训练紧耦合的管道；
弱项在纯关系代数（多表 join、复杂聚合、超大规模 shuffle）。**

| 你的任务 | 建议 |
|---|---|
| 纯 SQL 数仓 ETL、多表 join、需要成熟优化器 | Spark / DuckDB / Trino |
| 单机放得下的 DataFrame 变换 | Polars / DuckDB（快得多，运维为零） |
| 大规模 UDF 预处理 + GPU 批推理 | **Ray Data** |
| 训练数据加载（要跟 DDP 对齐、要 shuffle） | **Ray Data**（`iter_torch_batches`） |
| 已有 Spark 管道只想跑一次推理 | 上游 Spark 落 parquet，Ray Data 只做推理段 |
| 需要被 BI 工具查询的结果表 | 写 Delta/Iceberg，用别的引擎查 |

一句话：**Ray Data 是"AI 管道的最后一公里"，不是数仓的地基。**

## 12.6 调优与常见坑

官方有一篇专门的 OOM 排查文档 `docs.ray.io/en/master/data/how-to-avoid-ooms.html`
（2.56 期间随 PR #64046 加入）。下面把它和经验揉在一起，按"最容易踩"排序。

### 坑 1：block / batch 太大

执行期间**一个 task 的输入 block + 至少一个输出 block 必须同时在内存里**，
而且输出 block 在进对象存储之前是在 **Python 堆**上的。

* Ray Data 能把读/map 阶段的 block 切成小于 target max block size 的块，
  但 **block splitting 默认是关的**；
* 单条记录本身巨大（4K 视频帧、长音频）时切不动；
* `map_batches` 返回的 batch 特别大时也切不动 —— 只能把 `batch_size` 调小。

2.56 起 CPU 的 `map_batches` 支持 `batch_size="auto"`：采样行大小后挑一个让
输入控制在约 16MB 以下的 batch size（**文档后来澄清这条只在不用 GPU 时生效**）。

### 坑 2：对象存储压力

Ray 默认把约 **30%** 的机器内存留给对象存储（常量
`DEFAULT_OBJECT_STORE_MEMORY_PROPORTION = 0.3`，可用环境变量
`RAY_DEFAULT_OBJECT_STORE_MEMORY_PROPORTION` 覆盖 —— 与第 3、7 章的写法是同一个
东西），Ray Data 的 block 都住在里面。

> ⚠️ **一个已经过时的建议，别再照抄**：早期官方文档确实建议
> 「join / shuffle 密集的负载把比例提到 **0.5**」，但**这条建议已被官方撤销**
> （`#63389` 删掉了推荐文案、`#62387` 删掉了相关告警）。撤销的理由正是
> 下面这个双向作用：**对象存储调大 → 能调度的逻辑内存变小 → 并发下降；
> 缓冲吃满物理内存反而更容易 OOM**。现行口径是：**不要靠这个旋钮解决 OOM**，
> 该调的是 UDF 的 `memory=` 提示与并行度（坑 3、坑 4）。

block 放不下会 spill 到磁盘，所以你可能不是 OOM 而是**磁盘爆**。

```
内存预算的争夺：
┌──────────────────────────────────────────────┐
│  UDF / 模型 / Python 堆   │  对象存储 block   │
│  ← 调高比例会压缩这一侧 → │  ← 默认 ~30% →    │
└──────────────────────────────────────────────┘
  调大对象存储 = 缓冲更稳，但并发下降、UDF 更容易 OOM
  —— 所以官方已撤回"shuffle 负载调到 0.5"的建议
```

### 坑 3：内存提示（memory hint）不是硬限制

`ds.map_batches(fn, memory=8 * 1024**3, num_cpus=1)` 只影响**调度决策**，不做
OS 级强制限制；选值依据是日志里的 `max_uss_bytes`。2.56 还加了
`DataContext.get_current().default_map_logical_memory_enabled` —— 注意名字以
`_enabled` 结尾，它是一个**要显式打开的开关**（不是默认生效的行为），
打开后给 map task 默认按每 CPU 约 4 GB 量级登记逻辑内存
（「4 GB」是量级，确切默认值以你所装版本的 `DataContext` API 页为准）。

### 坑 4：shuffle 的并行度与倾斜

`num_partitions` 不要比 aggregator 数量大太多（每个 partition 都是一个输出对象）。

🔴 **数据倾斜不要再去调 `join(partition_size_hint=...)`** —— 本书早先那句
"按最大分区给 hint"在 2.58 上**已经无效**。`dataset.py` 第 3181 行的文档字符串
写得很直白：

> `partition_size_hint`: (Optional) **Deprecated** and ignored. The join is now
> executed on the v2 hash-shuffle path, which sizes reduce-task memory from
> observed partition sizes rather than a hint. **This parameter has no effect**
> and will be removed in a future release.

传了它还会额外吃到一条 `DeprecationWarning`（第 3294 行的
`warnings.warn`）。**倾斜现在的处理方式是"让 V2 自己观察"**：
V2 hash shuffle 用 **bounded online sampling** 估 aggregator 大小（PR #63929），
取在线采样与逻辑估算的 `max`，并加一个**上限**（不超过集群内存均分的 50%）。
真要缓解倾斜，能动的是 `num_partitions`（拆得更细，让单个 reduce task 更小）。

### 坑 5：driver 侧内存无界增长

已知问题（issue #66016）：每个输出 block 的统计信息会**保留到 Dataset 生命周期
结束**，driver 内存随累计 block 数单调增长；issue 明确说这**不能靠调 block size /
concurrency / backpressure 解决**，对"产出大量小 block"的负载尤其明显。
缓解办法是分阶段处理、及时释放 Dataset 引用。

### 坑 6：GPU 利用率低

原因通常不是 Ray：`batch_size` 太小 GPU 空转、太大直接 OOM（7B 模型从 32/64
起调）；重的 preprocess 抢在 GPU stage 前面导致 GPU 等数据；`concurrency`
小于可用 GPU 数（TP=1 时每个 stage 吃 1 张卡）。

### 这些旋钮到底怎么设：`DataContext` / `ExecutionOptions`

前面反复说"把对象存储预算压到 2GB""关掉保序"，落地方式都是在 driver 上改
**进程级单例** `DataContext`：

```python
import ray
from ray.data import DataContext
from ray.data._internal.execution.interfaces import ExecutionResources

ctx = DataContext.get_current()

# ① 全局预算：对象存储给 Ray Data 用多少（默认是集群内存的一部分，见坑 2）
#    ⚠️ 整体替换，不能就地给 resource_limits 的字段赋值 —— 见下面第 3 条
ctx.execution_options.resource_limits = ExecutionResources.for_limits(
    object_store_memory=2 * 1024**3)

# ② 保序：OutputBackpressure 之外的另一条取舍。
#    注意 ExecutionOptions 的构造默认就是 preserve_order=False（"Off by default"）
ctx.execution_options.preserve_order = False

# ③ 回退 DataSourceV2（见 12.3）
ctx.use_datasource_v2 = False

ds = ray.data.read_parquet(...)   # 之后建的 Dataset 才会带上这些设置
```

* `DataContext.get_current()` 返回的是**进程级单例**，所以必须在 driver 上、在
  **建 Dataset 之前**改；改完再建的算子才吃到新值（已建好的 DAG 不会回头改）；
* 字段分两层：`execution_options`（`ExecutionOptions`，管 `resource_limits`、
  `preserve_order`、`verbose_progress`、`label_selector` 这类）和 `DataContext`
  自身的字段（`target_max_block_size`、`use_datasource_v2`、
  `default_map_logical_memory_enabled` 等）；
* ⚠️ **`resource_limits` 不能就地赋值**（这条本书早先标"未确认"，现已对源码核实）：
  `ExecutionResources` 用 `__slots__` + **只读 property** 实现
  （`execution_options.py` 第 24 行 `__slots__ = ("_cpu", "_gpu",
  "_object_store_memory", "_memory")`，第 106 行 `@property def
  object_store_memory`，**没有 setter**），而且它**不是 dataclass**
  —— 所以既不能 `...resource_limits.object_store_memory = X`（`AttributeError`），
  也不能用 `dataclasses.replace(...)`（`TypeError`）。正确写法是**整体替换**：
  `ctx.execution_options.resource_limits = ExecutionResources.for_limits(...)`
  —— `ExecutionOptions.resource_limits` 那个 setter（第 464 行）会自动帮你套
  `ExecutionResources.for_limits()`；
* ⚠️ **没有 `max_blocks_in_flight` 这个可调字段**（本书早先标"未确认"，
  现已核实：`DataContext` 与 `ExecutionOptions` 上都不存在）。它只以
  **私有**形式存在于 DataContext 内部
  （`_max_num_blocks_in_streaming_gen_buffer`，挂在 DataContext 上），
  不在公开面。别照抄网上的 `ctx.max_blocks_in_flight = 8` —— 和
  `use_streaming_executor` 一样，它会**静默写进去、然后什么都不发生**；
* `execution_options` 里两个**已弃用**的字段要认出来：`exclude_resources`
  （改用 `label_selector`）与 `actor_locality_enabled`（Ray Data 内部自己管
  actor locality）—— 本书早先把 "actor locality" 当成一个活的旋钮列出来，
  这里更正；
* 每台机器内存不同时，`object_store_memory` 这类预算最好按"每节点可用内存的
  比例"给，而不是写死绝对值（写死就是"小机器上 OOM、大机器上浪费"）；
* 只在某个算子上临时覆盖资源，用 `map_batches(..., num_cpus=…, num_gpus=…,
  memory=…)`。⚠️ **这三个是明确写在签名里的形参**（`map_batches` 签名里
  `num_cpus` / `num_gpus` / `memory` 分别在 `dataset.py` 第 568 / 569 / 570 行）；
  其余调度参数（`resources=` / `accelerator_type=` / `label_selector=` /
  `runtime_env=` / `max_calls=` 之类）在 2.58 的 `map` / `map_batches` 签名里
  **并没有各自的具名形参**，而是统一从 **`**ray_remote_args` 透传**进去的
  —— 换句话说，你能传什么取决于底层 ray remote 参数，而**不是**一份稳定的
  "worker 选项清单"。
  （`fallback_strategy=` 这类软约束回退选项的落点各版本一直在动，
  **2.58 的算子签名里查不到它**；要用请以你所用版本的算子签名为准。）
  ⚠️ 真正**具名且已报弃用**的是 **`ray_remote_args_fn=`**
  （`dataset.py` 第 573 行的形参，第 214 行的告警原文：
  *"`ray_remote_args_fn` is deprecated and will be removed in Ray 2.64."*）。
  至于 `ray_remote_args=`：**它根本不是 `map` / `map_batches` 的具名形参**
  —— 签名里只有 `**ray_remote_args` 这个 catch-all，所以写
  `ray_remote_args={...}` 会把整个字典当成**一个名为 `ray_remote_args` 的
  remote 参数**吞进去。它有具名形参的地方是 `write_*` / `iter_*` 一类
  API（例如 `write_delta(..., ray_remote_args=None)`）。
  这与改全局 `DataContext` 不是一回事：一个是算子级、一个是作业级。

> **提示**：调 `DataContext` 之前先量一遍。`ds.stats()` / dashboard 的 operator
> 指标（12.1 提过）能告诉你瓶颈在 I/O、CPU UDF 还是对象存储；没有基线就调参，
> 大概率只是把 OOM 从内存挪到磁盘。

## 12.7 完整示例：批量推理管道

一个"读 parquet → 分块 → GPU 批推理 → 写 Delta"的可读骨架，注意取舍注释。

```python
"""对 200 万条 query 做分类打标，落到 Delta 表。"""
import ray
ray.init(num_gpus=4, address="auto")
# 可选：DataContext.get_current().execution_options.preserve_order = False
# （不保序 → 省一次物化；与训练同机时反过来可压小 object_store_memory）

# ---- 1. 读：列裁剪下推到 parquet footer，尽早过滤脏数据 ----
ds = ray.data.read_parquet("s3://bucket/queries/",
                           columns=["query_id", "text", "dt"])
ds = ds.filter(lambda r: r["text"] and len(r["text"]) > 0)

# ---- 2. 预处理：CPU 侧，actor 池复用 tokenizer ----
class Tokenizer:
    def __init__(self):
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained("distilbert-base-uncased")

    def __call__(self, batch):
        enc = self.tok(batch["text"], padding="max_length", truncation=True,
                       max_length=128, return_tensors="np")
        return {"query_id": batch["query_id"],
                "input_ids": enc["input_ids"].tolist(),
                "attention_mask": enc["attention_mask"].tolist()}

ds = ds.map_batches(Tokenizer, compute=ActorPoolStrategy(size=8),
                    batch_size=512, batch_format="pandas",
                    memory=2 * 1024**3)     # 显式声明，避开拥挤节点

# ---- 3. 推理：GPU 侧，一个 actor 一张卡 ----
class Classifier:
    def __init__(self):
        import torch
        from transformers import AutoModelForSequenceClassification
        self.device = "cuda"
        self.model = (AutoModelForSequenceClassification
                      .from_pretrained("distilbert-base-uncased", num_labels=5)
                      .to(self.device).eval())

    def __call__(self, batch):
        import numpy as np, torch
        with torch.inference_mode():
            ids = torch.as_tensor(np.stack(batch["input_ids"])).to(self.device)
            am = torch.as_tensor(np.stack(batch["attention_mask"])).to(self.device)
            logits = self.model(input_ids=ids, attention_mask=am).logits
            return {"query_id": batch["query_id"],
                    "label": logits.argmax(-1).cpu().numpy().tolist(),
                    "confidence": torch.softmax(logits, -1)
                                    .max(-1).values.cpu().numpy().tolist()}

# num_gpus=1 每个 actor 一张卡；size=4 铺满 4 张卡
ds = ds.map_batches(Classifier,
    compute=ActorPoolStrategy(size=4), num_gpus=1,
                    batch_size=256, batch_format="numpy")

# ---- 4. 写：Delta Lake，driver 侧一次原子提交 ----
ds.write_delta("s3://bucket/lake/query_labels", mode="append", partition_by=["dt"])
```

### 三个值得单独说的点

**（1）为什么 tokenize 和推理要拆成两个算子？** 塞进同一个 actor，GPU 会在
tokenize 期间闲着，而 tokenize 是纯 CPU 活。拆开后 Ray Data 让它们按 block 流水：
CPU 池在 tokenize block N+1 时，GPU 池在跑 block N。

**（2）为什么 `batch_size` 两级不同？** CPU 侧 512 摊薄 actor 往返开销，
GPU 侧 256 是为了不爆显存。同一个 `batch_size` 用在两边几乎总是错的。

**（3）反例**：`ds.take_all()`（2M 行拉进 driver → OOM）、
`ds.map(lambda r: model(r["text"]))`（一行一次前向，GPU 利用率 <5%）。


## 12.8 与 mini-ray 的关系

**mini-ray 没有实现 Ray Data**，README 的"明确不做"清单里写得很清楚。它实现的
是 **Ray Core**（task / actor / object / 调度 / 容错），而 Ray Data 的流式执行器
恰好建在这些原语之上 —— 每个物理算子最终就是 `TaskPoolMapOperator` 里一堆
`ray.remote` task，或者 `ActorPoolMapOperator` 里一堆 actor。对应读法：

* `ShuffleMapOp` / `ShuffleReduceOp` = 两个普通 task 算子，只是输出走对象存储、
  并被标成"阻塞式物化算子"；
* 背压 = `ray.wait` 那套"别一次把 1000 个 ref 全 put 进去"的思路，放大成按
  CPU/GPU/内存三资源核算的调度器；
* actor 池 = mini-ray 的 `util.ActorPool`（经典 `ray.util.ActorPool` 语义：**池子
  是固定的一批 actor**，用 `submit` / `get_next` 手动驱动）。Ray Data 的
  `ActorPoolStrategy(min_size, max_size)` 相当于在它之上再加一层"看队列长度
  自动加/减 actor"的策略 —— 那层策略 mini-ray **没有**。

可运行的对照物：`examples/10_parameter_server.py` —— 一个中心
`ParameterServer` **actor**（有状态、权重串行更新）+ 每轮并发拉起的
`compute_gradient.remote(...)` **无状态 task**，正好是"有状态算子用 actor、
无状态算子用 task"这两条路线的雏形。注意它**没有**用 actor 池：文件末尾把
"用 `ActorPool` 重写 worker 部分"列成了练习。另外 `tests/test_core.py` 里的
`ray.wait` 用例是背压思路的起点。

## 12.9 本章小结

* Ray Data 的定位是 **AI 负载的数据处理库**，核心差异化是
  **streaming execution engine**：算子按 block 流水、算子融合、中间结果不落地、
  按资源做背压。心智模型是 **`Dataset`（惰性 DAG）+ block（物理分片）** ——
  lazy 变换不执行，`iter_*` / `write_*` / `materialize` 才执行；
  shuffle/join/sort 是物化 barrier。
* `map_batches` 是主力算子，`compute=ActorPoolStrategy(...)` + `num_gpus`
  是 GPU 批推理的三件套；`iter_torch_batches` 是喂训练循环的出口。
* **actor 池有两种**（都用 `compute=` 表达，`concurrency=` 已于 2.51 弃用）：
  `ActorPoolStrategy(size=N)` 是固定池，
  `ActorPoolStrategy(min_size=…, max_size=…)` 是伸缩池。
  有状态 UDF 的构造参数走 `fn_constructor_args` / `fn_constructor_kwargs`；
  `async def` UDF（配 actor + `max_concurrency`）和生成器 UDF 解决的是 I/O 等待
  与"输出远大于输入"两类问题。
* **多 epoch 不会自动复用**：不 `materialize()`，每轮都会重跑整条 pipeline
  （issue #45042 讨论的就是这条边界）。每轮换洗牌只能靠**消费端**的
  `local_shuffle_buffer_size` + 每轮换 `local_shuffle_seed` ——
  `DatasetPipeline`（`repeat` / `window`）那套 API 在 2.58 已经不存在了。
* 表算子里 `sort` / `groupby` / `join` / `repartition` 是**物化 barrier**；
  `groupby()` 只给 `GroupedData`，要再调 `map_groups()` / 聚合才回到 `Dataset`。
  给多个 worker 分数据用 `streaming_split`（不物化、支持 `locality_hints`），
  而不是 `split` —— 前者正是 Ray Train 取分片的底层机制。
  ⚠️ **`join(partition_size_hint=...)` 在 2.58 已"弃用且被忽略"**，
  倾斜要走 `num_partitions`，别再按"最大分区"给 hint（见了会吃一条 `DeprecationWarning`，
  而且完全不生效）。
* **流式执行器关不掉了**：2.58 里**没有** bulk executor 可回退
  （整个仓库只有 `streaming_executor.py`），而且 `ctx.use_streaming_executor = False`
  这个写法**不报错也不生效**（`DataContext.__setattr__` 没有兜底的 `raise`）。
  同理不存在 `DataContext.max_blocks_in_flight`。可调的是
  `ExecutionOptions.resource_limits`（**必须整体替换**，`ExecutionResources`
  的字段是只读 property）与算子级的 `num_cpus` / `memory=`。
* 作业级旋钮都挂在 `DataContext.get_current()`（对象存储预算、`preserve_order`、
  `use_datasource_v2` 等），必须在 driver 上、建 Dataset 之前设。
* 数据源/汇覆盖 parquet/csv/json/text/binary、**TFRecord / WebDataset
  （读写两侧都有：`read_*` 与 `write_tfrecords` / `write_webdataset`）**、
  images/videos/audio、**Delta（读写都有）/ Iceberg（读写都有，写是 alpha）/
  Hudi（只有读）**、Kafka（读写都有，但**只有有界批语义**）、Unity Catalog，
  还能 `to_spark` / `to_dask` 互转；较新连接器的稳定性标注**未确认**，以 API 页为准。
* 🔴 **三个明写的"没有"**：**没有 Kinesis 连接器**（整棵源码树零命中，
  AWS 场景请走 Firehose→S3→`read_parquet`）；**没有 `write_hudi`**；
  **没有任何无界/流式源**（"Unbounded data source" 还在路线图 #58665 上）——
  Ray Data 是批处理引擎，别拿它模拟流引擎。
* **DataSourceV2 自 2.57 默认开启**（2.56 期间因 aggregator OOM 被临时关过一次）；
  **Hash Shuffle V2 用两个无状态算子替换了 aggregator actor pool**。
* **`ray.data.llm` 仍是 beta**，用 `vLLMEngineProcessorConfig` /
  `SGLangEngineProcessorConfig` + `build_processor`，`concurrency=n`（整数 =
  `(1, n)` 伸缩池）自动扩 GPU
  stage；引擎版本耦合是真实风险（vLLM ≥ 0.19 的破坏性变更）。
* 不要拿它当 Spark 用。纯 SQL/多表 join/超大规模 shuffle 还是 Spark/DuckDB 的
  主场；官方路线图（#58665）自己列了 scalability envelope、Celeborn
  External Shuffle Service、Ibis 集成这些待办。
* OOM 排查优先顺序：**block/batch 大小 → 对象存储比例 → memory hint
  → shuffle 并行度/倾斜 → driver 侧统计泄漏**。

下一章进 Ray Train：Ray 到底在分布式训练里负责哪一段，
以及 V2 默认开启之后写法变了什么。
