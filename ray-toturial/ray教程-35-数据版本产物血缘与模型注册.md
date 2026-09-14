仓库地址：https://github.com/hhk-png/cycle-agent

# 第 35 章：数据版本、产物血缘与模型注册

> 第 32 章结尾把读者领到了一扇门口，然后停住了。
> 原文是：「**谁是这个模型的生产版本，是你的事**」。
>
> 这一章把那件事讲完 —— 因为「模型注册」并不是一个可以推给别人的问题，
> 它是**「这个线上模型到底是用哪份数据、哪组参数、哪个代码训出来的」**
> 这个问题的答案。答不上来，就有三类事故在等着你：
>
> * 线上模型效果变差，**你无法回滚** —— 因为不知道上一个好的是哪个；
> * 复现三个月前的结果，**复现不出来** —— 数据和代码都变了；
> * 出了合规审计，**拿不出证据链** —— 训练数据的来源说不清。
>
> **本章的目标：让你能在 10 秒内回答上面那个问题。**

---

## 35.1 先分清三件常被混为一谈的事

| 概念 | 回答的问题 | 典型载体 |
|---|---|---|
| **数据版本** | 这份**数据**是哪一份？变了吗？ | DVC / lakeFS / 对象存储的不可变路径 |
| **血缘（lineage）** | 这个**模型**是由什么产出的？ | checkpoint 里的元数据 + 实验目录 |
| **模型注册** | 哪个版本**该上生产**？怎么回滚？ | MLflow Model Registry / 自建表 |

**三件事的关系是一条链**：

```
数据版本  ──→  一次训练  ──→  产物（checkpoint）+ 血缘元数据  ──→  注册表  ──→  生产
   ↑                                   ↑                              │
   └────────── 出事时沿着链往回查 ──────┴──────────────────────────────┘
```

**Ray 只负责中间那一段。** 数据版本和注册表都是**你的职责** ——
这不是 Ray 的缺陷，而是它的边界（第 19 章、第 20 章反复讲的那个"编排层"定位）。

---

## 35.2 Ray 已经给了什么、还缺什么

先把边界划清楚，免得你在 Ray 的 API 里找一个不存在的东西。

| 能力 | Ray 有吗 | 在哪 |
|---|---|---|
| 实验目录（每次 run 一个唯一目录） | ✅ | `RunConfig(name=, storage_path=)`；结果在 `Result.path` |
| 指标与配置的持久化 | ✅ | `Result.metrics` + 实验目录下的 JSON |
| 产物（checkpoint）的持久化 | ✅ | `Checkpoint`，可写任意 URI |
| **checkpoint 上的自定义元数据** | ✅ | `Checkpoint.set_metadata({...})` / `.get_metadata()` |
| 把指标/产物推到外部追踪系统 | ✅ | 第 32 章的 `*LoggerCallback` |
| 实验续跑 | ✅ | `Tuner.restore()`（第 14 章 §14.16） |
| **数据集指纹** | ❌ **没有公开 API** | —— 本章 §35.3 自己造 |
| **模型注册表** | ❌ **不做** | —— 本章 §35.5 |
| **数据的存储与版本** | ❌ **不做** | —— DVC / lakeFS / 不可变路径（§35.7） |

> ⚠️ **"数据集指纹"那一行值得强调**：Ray Data 会让每个 `Dataset` 对象内部
> 有自己的 UUID，但**这不是一个跨进程稳定、可比较的"数据版本"标识**
> （重新读一次同一个目录会得到新的 UUID）。
> **不要把它当成数据版本用** —— 这是很容易踩的一个坑。
> 想要"这份数据是不是上次那份"，只能自己算（§35.3）。

---

## 35.3 数据指纹：把「哪份数据」变成可比较的字符串

### 目标

给定一个数据集，产出一个**短、稳定、可比**的字符串：

```
train@sha1:9f2a1c...    # 同样的文件集合 → 同样的串;变了一个字节 → 不同的串
```

### 方法一（推荐）：对「文件清单 + 元数据」做哈希

这是最实用、成本最低的做法 —— **不读数据内容，只读"有哪些文件、各多大"**：

```python
import hashlib, json
from pathlib import Path
import pyarrow.fs as pafs

def dataset_fingerprint(uri: str, fs=None) -> str:
    """
    对 `uri` 底下的文件清单(路径 + 大小)算一个稳定指纹。

    ⚠️ 刻意**不读文件内容** —— 对 TB 级数据读一遍要几小时,
       而"文件集合 + 大小"已经能捕捉 99% 的变更(重跑 ETL、换日期分区、
       上游回溯刷新)。要更强的保证就加上 mtime 或 ETag(见下)。
    """
    fs, path = pafs.FileSystem.from_uri(uri)
    entries = []
    for info in fs.get_file_info(pafs.FileSelector(path, recursive=True)):
        if info.type == pafs.FileType.File:
            entries.append((info.path, info.size))
    entries.sort()                     # ⚠️ 必须排序,否则遍历顺序会让指纹抖动
    h = hashlib.sha1()
    for p, s in entries:
        h.update(f"{p}\t{s}\n".encode())
    return h.hexdigest()[:16]

# 用法
fp_train = dataset_fingerprint("s3://bucket/features/v2026_09/train/")
print(fp_train)          # 例如 '9f2a1c7e4b2d8a01'
```

**四个必须注意的点**：

**① 一定要排序。** 文件系统的遍历顺序不保证稳定，
不排序的话同一个目录两次会得到不同的指纹 —— **那样这个指纹就毫无意义**。

**② 指纹要能区分"变没变"，不是"内容哈希"。**
* 只加 `size` → 能抓住"加了文件、删了文件、文件大小变了"；
* 加 `mtime` → 还能抓住"内容改了但大小没变"（推荐加上）；
* 加对象存储的 **ETag** → 最准（S3/GCS 的 ETag 对单段上传就是内容 MD5）。
  ⚠️ 但**分片上传的 ETag 不是内容哈希**，别把它当 MD5 用。

**③ 把指纹写进两个地方**：训练产物的元数据（§35.4）和特征表的目录名。
`v2026_09-9f2a1c7e/` 这种**"语义版本 + 指纹"**的命名，
让"翻目录"就能完成 90% 的排查。

**④ 指纹不是加密，别用它做完整性校验。** 它防的是"搞混了哪份数据"，
不防"数据被篡改"。

### 方法二：对内容做哈希（数据小的时候）

训练集只有几百 MB 时，可以老老实实读一遍：

```python
def content_fingerprint(ds) -> str:
    """对 ray.data.Dataset 的实际内容做哈希(只适合小数据)。"""
    h = hashlib.sha1()
    for batch in ds.iter_batches(batch_size=100_000, batch_format="pyarrow"):
        # ⚠️ 用 pyarrow 的确定性序列化,不要用 repr/pickle
        import pyarrow as pa
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, batch.schema) as w:
            w.write_batch(batch)
        h.update(sink.getvalue().to_pybytes())
    return h.hexdigest()[:16]
```

> ⚠️ **别用 `pandas.util.hash_pandas_object` 一把梭** ——
> 它只对 DataFrame 生效，而 Ray Data 的块边界会变，
> **同样的数据切成不同的块会得到不同的哈希**。
> 要么像上面这样自己累积（与分块方式无关），要么就用方法一。

### 方法三：直接不重算 —— 用不可变路径

**最简单也最有效的一招**：**写完的特征表永不覆盖**。

```
s3://bucket/features/v2026_09/           ← 写一次,以后只读
s3://bucket/features/v2026_09-9f2a1c7e/  ← 更好:带上指纹
```

这样"数据版本"就退化成了"路径"，**而路径是可以直接写进配置和文档的**。
代价是存储成本（旧版本要留着），但对特征表来说通常完全值得 ——
**这是本章性价比最高的一条建议。**

---

## 35.4 血缘：把「数据 → 实验 → 产物」串起来

有了指纹，下一步是**把它放进产物里**，而不是停在日志里。

### 用 `Checkpoint.set_metadata()` 携带血缘

Ray 的 `Checkpoint` 支持挂自定义元数据 —— **这是最正统的载体**：

```python
import ray.train
from ray.train import Checkpoint

def train_loop_per_worker(config):
    ...   # 训练
    loss = 0.123
    metrics = {"loss": loss, "auc": 0.87}

    with tempfile.TemporaryDirectory() as d:
        # 1. 存模型(本章以 torch 为例;GBDT 见第 34 章 §34.8)
        torch.save(model.state_dict(), os.path.join(d, "model.pt"))
        # 2. 存特征清单 —— 这一步决定了这个模型能不能被安全地部署
        json.dump(feature_spec, open(os.path.join(d, "feature_spec.json"), "w"))

        ckpt = Checkpoint.from_directory(d)

        # 3. 挂血缘元数据 ← 全章最关键的一行
        ckpt.set_metadata({
            "data.train_fingerprint": config["train_fp"],   # ← §35.3 算出来的
            "data.valid_fingerprint": config["valid_fp"],
            "data.uri":               config["train_uri"],
            "code.git_sha":           os.environ.get("GIT_SHA", "unknown"),
            "code.dirty":             os.environ.get("GIT_DIRTY", "unknown"),
            "params":                 json.dumps(config["params"], sort_keys=True),
            "feature_spec_version":   feature_spec["version"],
        })
        ray.train.report(metrics, checkpoint=ckpt)
```

**读回来验证**：

```python
best = results.get_best_result(metric="auc", mode="max")
meta = best.checkpoint.get_metadata()
print(meta["data.train_fingerprint"], meta["code.git_sha"])
```

### `git_sha` 从哪来？—— 别指望 Ray 帮你抓

Ray **不会**记录你的代码版本。三个实际可行的办法：

```bash
# ① CI 里注入(最可靠,推荐)
export GIT_SHA=$(git rev-parse HEAD)
export GIT_DIRTY=$(git diff --quiet || echo "-dirty")
ray job submit --runtime-env-json='{"env_vars": {"GIT_SHA": "'"$GIT_SHA"'"}}' ...

# ② 在代码里读(注意:worker 进程里不一定有 .git)
GIT_SHA = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()

# ③ 用 runtime_env 的 working_dir 哈希 —— ⚠️ 本书未确认它是否稳定可复现,
#    不要当作唯一的代码版本标识
```

> ⚠️ **`-dirty` 标记比 SHA 还重要**。绝大多数"复现不出来"的事故，
> 不是因为提交错了，而是因为**跑实验时工作区有未提交的改动**。
> 记录 `git diff --quiet` 的结果（哪怕只是一个布尔），能立刻排除掉这类怀疑。

### 血缘的三个层次：记多少才够

不是记得越多越好 —— **记"能用来定位"的那些**：

| 层次 | 记什么 | 什么事故靠它查 |
|---|---|---|
| **必需** | 数据指纹、git SHA、超参、Ray/框架版本 | 90% 的"结果对不上" |
| **推荐** | 特征清单（顺序 + 类型 + 编码表）、随机种子、镜像 digest | 部署后预测全错、结果不可复现 |
| **够了** | 模型结构哈希、完整环境快照 | 极少用到；**记录成本却很高** |
| **别记** | 完整数据集副本、整个 `state_dict` 进元数据 | 会把 checkpoint 撑爆 |

> ⚠️ **最后一行是个真实的坑**：`Checkpoint.set_metadata()` 的内容会**被序列化
> 进 checkpoint 的元数据**，不是外挂一个引用。往里塞大对象 =
> **每个 checkpoint 都膨胀一份**。元数据里只放**指针和短字符串**。

---

## 35.5 模型注册：Ray 不做，但你必须有

### 先说清楚"注册表"到底解决什么

它**不是**一个文件柜。它要回答的是**动态问题**：

| 问题 | 没有注册表时 | 有注册表时 |
|---|---|---|
| 现在生产跑的是哪个版本？ | 问同事 / 翻部署脚本 | `alias=production` 一查 |
| 上一个好版本是哪个？ | 靠记忆 | 按版本号往回翻，带指标 |
| 怎么回滚？ | 重新找文件、重新部署 | 把 alias 指回去（一条命令） |
| 这个版本能上生产吗？ | 口头确认 | 阶段/审批状态 |

**关键洞察**：注册表的核心不是"存文件"，而是**"给版本一个可变的名字"**
（`alias` / `stage`），让"生产用哪个"变成**一次指针切换**。

### MLflow Model Registry 的实际用法

第 32 章的 `MLflowLoggerCallback` 只负责**记实验**，不负责**注册**。
注册要自己调 —— 但代码很短：

```python
import mlflow
from mlflow import MlflowClient

mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
client = MlflowClient()

# ── 1. 把一次实验的产物注册成一个新版本 ──
#    run_id 从第 32 章的 MLflowLoggerCallback 那边拿(或自己 log)
model_uri = f"runs:/{run_id}/model"
mv = mlflow.register_model(model_uri, "fraud-detector")
print(mv.version)                   # 例如 '17'

# ── 2. 打上不可变的标签:血缘(与 §35.4 的元数据对齐) ──
client.set_model_version_tag("fraud-detector", mv.version,
                             "data.train_fingerprint", train_fp)
client.set_model_version_tag("fraud-detector", mv.version,
                             "git_sha", git_sha)
client.set_model_version_tag("fraud-detector", mv.version,
                             "eval_auc", "0.8742")

# ── 3. 晋升:用 alias 指向"生产" ──
client.set_registered_model_alias("fraud-detector", "production", mv.version)

# ── 4. 回滚 = 把 alias 指回去 ──
client.set_registered_model_alias("fraud-detector", "production", "16")
```

**四个必须知道的细节**：

**① `stage` 已不推荐，用 `alias`。**
老教程里的 `transition_model_version_stage(..., "Production")`
是 MLflow 2.x 起**弃用**的方向，新写法是 **`set_registered_model_alias()`**。
（⚠️ 弃用的确切版本边界请以你的 MLflow 版本为准 —— 但**"用 alias"这个方向是确定的**。）

**② 读取时按 alias，不要按版本号硬编码。**

```python
# ✓ 生产代码这么写
model = mlflow.pyfunc.load_model("models:/fraud-detector@production")
# ✗ 别写死版本号
model = mlflow.pyfunc.load_model("models:/fraud-detector/17")
```

**③ 注册和部署是两件事。**
注册表只是"指针"，**它不会自动把模型推到线上**。
真正的部署要靠你的 CD 流程去读 `@production` 再下发 ——
这个衔接见第 32 章 §32.7 与附录 G §G.2。

**④ 不用 MLflow 也行。**
最小可用版本是一张表 + 一个对象存储前缀：

```
s3://bucket/models/fraud-detector/17/{model.pt, feature_spec.json, meta.json}
s3://bucket/models/fraud-detector/PRODUCTION      ← 内容就是 "17"
```

`PRODUCTION` 这个文件**就是一个 alias** —— 回滚 = 改写它的内容。
**这就是模型注册的全部本质**，MLflow 只是把它做成了带 UI 和服务端的东西。

---

## 35.6 把第 32 章的「骨架回调」变成幂等固化步骤

第 32 章给了一个 `PromoteBest` 回调的骨架，并且**作者自己指出了它的竞态**：
如果多个 trial 同时结束，"最好的"可能在回调跑完之后才出现。

**这一章给答案**：不要在回调里做固化 ——
**回调只做标记，固化放到 `tuner.fit()` 之后**。

```python
class MarkBest(tune.Callback):
    """只做记录,不做副作用 —— 幂等、可重放。"""
    def __init__(self):
        self.best = None
    def on_trial_result(self, iteration, trials, trial, result, **info):
        # ⚠️ 显式判断 None:第一次比较时 self.best 是 None,
        #    直接用 < 会抛 TypeError(第 32 章 §32.5 提过这个细节)
        score = result.get("auc")
        if score is None:
            return
        if self.best is None or score > self.best["score"]:
            self.best = {"score": score, "trial": trial.trial_id,
                         "checkpoint": trial.checkpoint}

# ── 1. 跑实验:全程没有副作用 ──
marker = MarkBest()
results = tune.Tuner(..., run_config=RunConfig(callbacks=[marker])).fit()

# ── 2. 固化:实验结束之后,串行、幂等地做一次 ──
best = results.get_best_result(metric="auc", mode="max")
meta = best.checkpoint.get_metadata()

version = register(
    checkpoint=best.checkpoint,
    name="fraud-detector",
    tags={
        "data.train_fingerprint": meta["data.train_fingerprint"],
        "git_sha":                meta["code.git_sha"],
        "eval_auc":               f"{best.metrics['auc']:.4f}",
    },
)
if best.metrics["auc"] > PROD_BASELINE_AUC:
    promote("fraud-detector", "production", version)     # 指针切换
else:
    print(f"不晋升: {best.metrics['auc']:.4f} <= {PROD_BASELINE_AUC}")
```

**为什么这个顺序是对的**：

| 放在回调里 | 放在 `fit()` 之后 |
|---|---|
| 并发执行，有竞态 | 单线程，无竞态 |
| 中途失败会留下"半注册"状态 | 要么全做完，要么没开始 |
| 重跑实验会重复注册 | 重跑只影响最后一次 |
| 无法用"已知的全局最优"做决策 | 能拿到 `get_best_result()` |

> **这条修正本身值得记**：第 32 章的骨架方向是对的（"要有一个固化步骤"），
> 但**放错了位置**。**"有副作用的操作必须串行且幂等"** ——
> 这是分布式系统里最朴素也最常被违反的一条规则
> （第 10 章 §10.4 那个真实 bug 也是同一种形状）。

---

## 35.7 DVC / lakeFS 与 Ray 的接法

只把模型管起来是不够的 —— **数据也要能回到某个版本**。

| 工具 | 模型 | 与 Ray 的接法 |
|---|---|---|
| **不可变路径**（§35.3 方法三） | 写一次不覆盖 | ⭐ **最推荐**：零依赖，`read_parquet(uri)` 直接读 |
| **DVC** | Git 管指针，数据存远端 | DVC 管数据 → `dvc pull` 到本地/共享盘 → Ray Data 读该路径 |
| **lakeFS** | 对象存储上的 Git（分支/提交/回滚） | Ray Data 读 `s3://repo/branch/path`，**不需要改代码** |
| **Delta / Iceberg** | 表格式带事务与时间旅行 | Ray Data 原生支持（第 12 章 §12.3）；读历史快照即可复现 |
| **数据仓库快照** | 仓库侧的时间旅行 | 读 `AS OF` 的结果导出成 Parquet，再交给 Ray |

**三条实践建议**：

**① DVC 与 Ray 是"接力"关系，不是集成关系。**
DVC 负责"把哪个版本的数据放到某个路径"，Ray 负责"读那个路径并处理"。
**别指望 Ray 认识 DVC 的指针** —— 它只认识 URI。

```bash
# CI 里:先落数据,再跑训练
dvc pull data/features/v2026_09.dvc        # → 落到 data/features/v2026_09/
GIT_SHA=$(git rev-parse HEAD) ray job submit \
  --runtime-env-json='{"env_vars":{"GIT_SHA":"'"$GIT_SHA"'"}}' \
  -- python train.py --data data/features/v2026_09/
```

**② 表格式（Delta/Iceberg）是"顺带就解决"的那一类。**
如果你已经把特征存在 Iceberg 里，**时间旅行 + Ray Data 读历史快照**
就是最干净的复现方案 —— 不用引入任何新工具。

**③ 无论用哪个，§35.3 的指纹都要留着。**
它是**跨工具的最小公共分母**：DVC 的版本号、Iceberg 的 snapshot id、
S3 的路径，都能对应到一个指纹上。**出了问题时，指纹是唯一保证可比的东西。**

---

## 35.8 一个完整的可复现训练骨架

把本章所有东西串起来 —— **这段骨架可以直接抄**：

```python
# ─────────────────────────────────────────────────────────────
# train.py —— 一次可复现的训练,需要记的东西都记了
# ─────────────────────────────────────────────────────────────
import os, json, subprocess, hashlib
import ray, ray.data, ray.train
from ray.train import RunConfig, ScalingConfig, CheckpointConfig
from ray.train.torch import TorchTrainer
from ray.air.integrations.mlflow import MLflowLoggerCallback

DATA_URI   = os.environ["DATA_URI"]              # 不可变路径(§35.3 方法三)
GIT_SHA    = os.environ.get("GIT_SHA", "unknown")
GIT_DIRTY  = os.environ.get("GIT_DIRTY", "unknown")
PARAMS     = json.loads(os.environ.get("PARAMS_JSON", "{}"))

# ── ① 数据版本 ──
def fingerprint(uri: str) -> str:
    import pyarrow.fs as pafs
    fs, path = pafs.FileSystem.from_uri(uri)
    entries = sorted(
        (i.path, i.size)
        for i in fs.get_file_info(pafs.FileSelector(path, recursive=True))
        if i.type == pafs.FileType.File
    )
    h = hashlib.sha1()
    for p, s in entries:
        h.update(f"{p}\t{s}\n".encode())
    return h.hexdigest()[:16]

TRAIN_URI = f"{DATA_URI}/train"
VALID_URI = f"{DATA_URI}/valid"
train_ds  = ray.data.read_parquet(TRAIN_URI)
valid_ds  = ray.data.read_parquet(VALID_URI)

# ── ② 训练 + 血缘 ──
def loop(config):
    import torch, tempfile, ray.train
    model = build_model(config)
    for epoch in range(config["epochs"]):
        loss = train_one_epoch(model, config)
        with tempfile.TemporaryDirectory() as d:
            torch.save(model.state_dict(), os.path.join(d, "model.pt"))
            json.dump(feature_spec(), open(os.path.join(d, "feature_spec.json"), "w"))
            ckpt = ray.train.Checkpoint.from_directory(d)
            ckpt.set_metadata({
                "data.train_fingerprint": config["train_fp"],
                "data.valid_fingerprint": config["valid_fp"],
                "data.uri":               DATA_URI,
                "code.git_sha":           GIT_SHA,
                "code.git_dirty":         GIT_DIRTY,
                "params":                 json.dumps(config["params"], sort_keys=True),
            })
            ray.train.report({"loss": loss}, checkpoint=ckpt)

trainer = TorchTrainer(
    loop,
    train_loop_config={
        "epochs": 10,
        "params": PARAMS,
        # ⚠️ 指纹必须**在 driver 侧算一次**再传进去 ——
        #    每个 worker 各算一次是浪费,而且顺序可能不同
        "train_fp": fingerprint(TRAIN_URI),
        "valid_fp": fingerprint(VALID_URI),
    },
    datasets={"train": train_ds, "valid": valid_ds},
    scaling_config=ScalingConfig(num_workers=4, use_gpu=True),
    run_config=RunConfig(
        name=f"train-{GIT_SHA[:8]}",           # ← 实验名带代码版本,翻目录就能查
        storage_path=os.environ["RAY_STORAGE"], # ← 必须是共享存储
        checkpoint_config=CheckpointConfig(num_to_keep=2),
        callbacks=[MLflowLoggerCallback(
            tracking_uri=os.environ["MLFLOW_TRACKING_URI"],
            experiment_name="fraud-detector",
            save_artifact=True,
        )],
    ),
)

result = trainer.fit()
print("experiment dir:", result.path)          # ← 这串路径就是血缘的入口

# ── ③ 注册(§35.6:实验结束后串行做) ──
meta = result.checkpoint.get_metadata()
print(json.dumps(meta, indent=2, ensure_ascii=False))   # ← 先打印出来人眼确认
# register_and_promote(result, meta)             # ← 再接 §35.5 的代码
```

**这份骨架的四条设计原则**（比代码本身更重要）：

1. **指纹在 driver 侧算一次**，传给所有 worker —— 保证一致；
2. **实验名带 git SHA** —— `storage_path/train-9f2a1c7e/` 让人能直接翻；
3. **所有版本信息走环境变量** —— 不要硬编码，也**不要在 worker 里读 `.git`**；
4. **注册步骤默认注释掉，先打印** —— 第一次跑一定要人眼确认元数据对不对，
   再打开自动注册。

---

## 35.9 与 mini-ray 的关系

**mini-ray 完全没有这一章的内容**，而且这是**合理的设计取舍**：
mini-ray 是 Ray **Core** 的教学实现，而这一章讲的是**围绕 Ray 的工程实践** ——
数据版本、血缘、注册表，全都不是"分布式运行时"的组成部分。

> **但有一个真实的连接点**：本章反复强调的那条规则 ——
> **「有副作用的操作必须串行且幂等」** —— 正是第 10 章 mini-ray
> 那个真实 bug 的同一条规则。
>
> 第 10 章 §10.4 记录的事故是：`lose_objects()` 删了对象但**没有同步账本**，
> 于是"重建依赖"的判定基于过期的状态，直接重放任务。
> 一句话概括：**两份状态没对齐**。
>
> 本章 §35.6 讲的"回调里做注册"是**同一个形状** —— 并发执行的回调
> 与"谁是最好的"这个全局判定没对齐。**同一个错误，换个地方又犯一次。**
> 这就是为什么这本书反复在讲它。

**想动手观察"并发 + 共享状态"的危险**：
`mini-ray/examples/03_actors.py` 与 `tests/test_actors.py` 里的并发组测试，
用非常小的规模演示了"多个执行体改同一份状态"会发生什么。

---

## 35.10 本章小结

* **三件事要分清**：数据版本（数据是哪份）、血缘（模型由什么产出）、
  模型注册（哪个该上生产）。**Ray 只负责中间那段。**
* **Ray 没有数据集指纹 API** —— `Dataset` 内部的 UUID 不是跨进程稳定的版本标识，
  **别拿它当数据版本用**。自己算（§35.3）。
* **指纹算法**：对排序后的「文件路径 + 大小（+ mtime/ETag）」做哈希。
  **必须排序**，否则遍历顺序变化会让指纹抖动。
* **性价比最高的一招是"不可变路径"**：写完的特征表永不覆盖，
  数据版本就退化成路径 —— 零依赖，`read_parquet` 直接用。
* **血缘挂在 `Checkpoint.set_metadata()` 上**，不是挂在日志里。
  ⚠️ 元数据**会被序列化进 checkpoint**，所以只放指针和短字符串。
* **`git_dirty` 比 `git_sha` 还重要** —— 绝大多数"复现不出来"是因为
  跑实验时工作区有未提交的改动。
* **模型注册的本质是"给版本一个可变的名字"**（alias/stage），
  让"生产用哪个"变成一次指针切换。MLflow 只是把它做成了服务；
  一个 `PRODUCTION` 文本文件也能干同样的事。
* **用 `alias` 而不是 `stage`**，生产代码按 `@production` 读，
  **永远不要硬编码版本号**。
* **有副作用的操作（注册/晋升）必须放在 `fit()` 之后串行执行**，
  不能放在并发回调里 —— 这正是第 32 章那个骨架的问题所在。
* **DVC/lakeFS 与 Ray 是接力关系**：前者负责"把哪个版本放到某个路径"，
  后者只认识 URI。**别指望 Ray 认识 DVC 指针。**

> **下一章**：第 36 章《分布式追踪与 OpenTelemetry》——
> 回到第 11 章那个"四层观测模型"里唯一空着的一层。
> 本章解决的是"**事后**怎么查"，下一章解决"**事中**怎么追"。
