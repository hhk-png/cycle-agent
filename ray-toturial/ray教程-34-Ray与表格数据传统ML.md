仓库地址：https://github.com/hhk-png/cycle-agent

# 第 34 章：表格数据与传统 ML（XGBoost / LightGBM / scikit-learn）

> **这一章解决一个刺眼的事实**：本书此前**没有任何一章真正讲过**
> scikit-learn / GBDT 这条路 —— 它只在**目录（第 00 章）、术语表（第 23 章）
> 和第 32 章的一个链接**里出现过名字，没有一节是讲它的。
> 而工业界里 Ray 落地量最大的场景之一，恰恰是**表格数据 + GBDT**。
>
> 更具体地说：第 13 章给了 `XGBoostTrainer` 一个**表格里的名字**，
> 第 14 章整章的超参搜索**没有一个真实模型**（只有 `build_model(config)` 占位），
> 第 29 章把 90% 的篇幅给了 PyTorch / HuggingFace。
> 这一章补上这块：**不做深度学习的那些人，在这本书里终于有了入口。**
>
> **本章所有内容都能在纯 CPU 上跑完** —— 没有 GPU 也能从头做到尾。

---

## 34.1 先想清楚：你属于哪一类读者

这一章对两类人价值完全不同。

**第一类：你本来就是做 GBDT / 传统 ML 的。**
你有一张几百 GB 的特征表、一个调不动的 XGBoost、一条排队到明天的训练任务。
你想知道"Ray 能不能让我不改模型就跑得更快"。→ 直接看 §34.3、§34.5、§34.7。

**第二类：你做深度学习，但流水线里有一段是表格数据。**
比如推荐系统的特征预处理、风控的规则模型、评估阶段的一个 LightGBM 基线。
你已经在用 Ray 了，想知道"这一段要不要也放进来"。→ 直接看 §34.7 的判据。

> ⚠️ **两类人的共同陷阱**：以为"上了 Ray 就会更快"。
> **GBDT 的分布式化收益远没有深度学习那么明显**，而且代价很实在
> （见 §34.7 的判据表）。**这一章有一半篇幅在用来说"什么时候别用"。**

---

## 34.2 四条路径总览

表格数据的 Ray 用法有四种，先建立地图，再挑一条：

| 路径 | 形态 | 适合的数据量 | 分布式在哪一层 |
|---|---|---|---|
| **① 原样不动** | 单机 `xgboost` / `lightgbm` / `sklearn` | 单机内存放得下 | **没有** —— 就是单机 |
| **② 只把 ETL 放上来** | Ray Data 做特征工程 → `to_pandas()` → 单机训练 | 数据大但**训练集小** | **数据层** |
| **③ Train 的 GBDT Trainer** | `ray.train.xgboost.XGBoostTrainer` | 训练集本身也放不下 | **训练层** |
| **④ Tune + 单机模型** | `tune.Tuner` + sklearn/XGBoost 的 `Trainable` | 训练集小、**但要搜很多组超参** | **调度层** |

**怎么选，一句话**：

```
训练集能装进一台机器吗？
├── 能 ──→ 要搜超参吗？
│          ├── 要 ──→ ④ Tune + 单机模型（第 14 章的主场）
│          └── 不要 ─→ ① 或者 ② （看 ETL 重不重）
└── 不能 ─→ ③ XGBoostTrainer / LightGBMTrainer
```

> ⚠️ **注意 ③ 的前提是"训练集本身放不下"** —— 不是"原始数据放不下"。
> 这是最常见的误判：100 GB 的原始日志，做完特征工程 + 采样之后可能只有 2 GB。
> **那种情况的正确解法是 ②，不是 ③** —— 让 Ray Data 把 100 GB 压成 2 GB，
> 然后单机训练。多机 GBDT 的通信开销会让 ③ 比 ② **更慢**。

---

## 34.3 路径 ③：`XGBoostTrainer`

先看完整形态。**它长得和第 13 章的 `TorchTrainer` 几乎一样** ——
这本身就是 Ray Train 的设计目标：换框架不换骨架。

> 🔴 **下面这段是 V1 时代的 legacy kwargs 写法**（`label_column=` / `params=` /
> `num_boost_round=` / `datasets=`）。**在 2.58 上它会直接抛
> `DeprecationWarning`（不是"打条告警"）** —— `label_column` / `params` /
> `num_boost_round` 三者只要传一个就报错。
>
> 原因是 **Ray Train V2 自 2.51 起默认开启**（`is_v2_enabled()` 的默认值是
> `True`），于是 `ray.train.xgboost.XGBoostTrainer` 解析到的是
> `ray/train/v2/xgboost/xgboost_trainer.py`，而它的 `__init__` 里是
> **`raise`**（LightGBM 逐字相同）：
>
> ```python
> if (label_column is not None or params is not None or num_boost_round is not None):
>     raise DeprecationWarning(
>         "The legacy XGBoostTrainer API is deprecated. "
>         "Please switch to passing in a custom `train_loop_per_worker` function instead. "
>         "See this issue for more context: https://github.com/ray-project/ray/issues/50042")
> ```
>
> ⚠️ 注意别被 V1 那个同名文件骗了：`ray/train/xgboost/xgboost_trainer.py`
> **只 `_log_deprecation_warning`、不抛** —— 但 2.58 默认走的**不是它**。
> **示例保留在这里是为了对照 V1 的形态，不要照抄运行。**
> V2 的正确写法是把这些塞进一个 training function、用 `ray.train.report()` 上报，
> 骨架和 `TorchTrainer` 完全一致 —— 详见本节末尾的「关于 V1/V2 状态」。

```python
import ray
from ray.train import ScalingConfig, RunConfig, CheckpointConfig
from ray.train.xgboost import XGBoostTrainer

ray.init()

trainer = XGBoostTrainer(
    label_column="label",                    # ← 必填：哪一列是标签
    params={                                 # ← 原封不动的 XGBoost 参数
        "objective": "binary:logistic",
        "eval_metric": ["logloss", "error"],
        "eta": 0.05,
        "max_depth": 8,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "tree_method": "hist",               # CPU 上就用 hist
    },
    num_boost_round=200,                     # ← XGBoost 自己的轮数
    datasets={"train": train_ds, "valid": valid_ds},   # ← ray.data.Dataset
    scaling_config=ScalingConfig(
        num_workers=4,                       # 4 个 worker 各拿一片数据
        use_gpu=False,                       # GBDT 通常 CPU 更划算（见下）
        placement_strategy="PACK",
    ),
    run_config=RunConfig(
        name="xgb-tabular",
        checkpoint_config=CheckpointConfig(num_to_keep=2),
    ),
)

result = trainer.fit()
print(result.metrics)          # {'train-logloss': …, 'valid-logloss': …}
print(result.checkpoint)       # 里面是训练好的 booster
```

**三个必须知道的事实**：

**① `params` 就是原生 XGBoost 的参数字典，Ray 不翻译它。**
你在 `xgboost.train(params=...)` 里写什么，这里就写什么。
Ray 唯一多管的是"数据怎么分片"和"梯度怎么聚合"。

**② `datasets` 必须是 `ray.data.Dataset`，不是 DataFrame。**
这是和单机用法最大的接口差异：

```python
import pandas as pd
import ray.data

# ✗ 不行：XGBoostTrainer 不接受 pandas DataFrame
# XGBoostTrainer(datasets={"train": pd.read_parquet(...)}, ...)

# ✓ 正确：先转成 Ray Dataset
train_ds = ray.data.read_parquet("s3://bucket/features/train/")
valid_ds = ray.data.read_parquet("s3://bucket/features/valid/")
```

**③ `use_gpu=True` 对 GBDT 通常**不划算**。**
XGBoost / LightGBM 的 GPU 路径在**小而宽**的表上经常比 CPU 慢
（数据搬运 + 直方图构建的 kernel 启动开销）。
**默认先跑 CPU**，只有在"特征维度极高 + 树极深 + 数据极大"三件事同时成立时，
才值得试 GPU 路径（而且要用 `device="cuda"` 而非 `tree_method="gpu_hist"` —— 
后者是旧写法，**具体弃用状态请查你所用版本的 XGBoost 文档**）。

### 它到底做了什么（这一节决定了你什么时候不该用它）

`XGBoostTrainer` 的分布式机制是**数据并行 + 梯度/直方图聚合**：

```
每个 worker 拿一片数据 → 各自算局部直方图 → AllReduce 聚合成全局直方图
        → 每个 worker 用同一份全局直方图独立分裂 → 得到**完全相同**的树
```

**注意最后一步**：所有 worker 的模型是**逐位相同**的，所以这不是"ensemble"，
而是"一台机器算不动的活拆开算"。这带来两个直接后果：

* ✅ **结果与单机一致**（同样的参数、同样的数据、同样的轮数下）；
* ❌ **通信量随 worker 数增长** —— 每轮都要同步直方图。
  **worker 数超过一定规模后，加机器反而更慢。**

> ⚠️ **这是 GBDT 与深度学习最本质的差别**：
> 深度学习的算力需求是"无底洞"（模型越大越需要更多卡），
> 而 GBDT 的**可并行度有个天花板**。经验上 4–16 个 worker 是常见区间，
> 再往上要先做基准测试（§34.6 给了骨架），**别默认"加机器 = 加速"**。

### 关于 V1/V2 状态：可以查证，不必猜

第 13 章 §13.2 / §13.3 已经把这件事讲清楚了，这一章只重复结论并补上出处：

* ✅ **`XGBoostTrainer` / `LightGBMTrainer` 仍在，而且已经 V2 化。**
  `python/ray/train/xgboost/__init__.py` 在 `is_v2_enabled()` 为真时
  直接从 `ray.train.v2.xgboost.xgboost_trainer` 导入 `XGBoostTrainer`
  （LightGBM 同理，`ray.train.v2.lightgbm`）。**它们不是 V1 遗留物。**
* ❌ **被删掉的是另外三个**：`LightningTrainer` / `TransformersTrainer` /
  `AccelerateTrainer`。生命周期是 **2.7 弃用 → 2.8 调用即报错 → 2.9 移除**
  （REP *"Unify Torch based Trainers on the `TorchTrainer` API"*），
  2.58 里 `import` 就会失败。**它们不是"V2 状态未确认"，是"不存在"。**
* 🔴 **但本节示例用的仍是 legacy kwargs 签名**：`label_column=` / `params=` /
  `num_boost_round=` 这一套在 2.58 上**会抛 `DeprecationWarning`，跑不起来**
  （见 §34.3 开头的说明）。替代路径是 training function +
  `ray.train.report()`（issue #50042 是官方给的迁移上下文）。

**实践建议**：

| 你想做的事 | 用什么 | 理由 |
|---|---|---|
| 分布式 GBDT（**V1 形态，仅供对照**） | `XGBoostTrainer(label_column=..., params=...)` | ❌ **2.58 抛 `DeprecationWarning`**（V2 默认），不能照抄 |
| 分布式 GBDT（V2 写法） | `XGBoostTrainer` + training function + `ray.train.report()` | 与 `TorchTrainer` 同骨架 |
| 分布式 PyTorch | `TorchTrainer` | V2 主路径 |
| 单机 GBDT + 超参搜索 | `tune.Tuner` + 自己包（§34.5） | 完全不依赖 GBDT Trainer 的签名形态 |

> **升级前查一次**：以你所用版本的 Ray Train 文档为准，确认
> `ray.train.xgboost` 的**签名**是否又变过（模块本身是稳的，变的是 kwargs）。

---

## 34.4 路径 ③ 的另一种：`LightGBMTrainer`

```python
from ray.train.lightgbm import LightGBMTrainer

trainer = LightGBMTrainer(
    label_column="label",
    params={
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "num_leaves": 255,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
    },
    num_boost_round=300,
    datasets={"train": train_ds, "valid": valid_ds},
    scaling_config=ScalingConfig(num_workers=4, use_gpu=False),
)
result = trainer.fit()
```

**与 XGBoost 的三个实际差异**（这才是选型时该看的，不是"谁更快"）：

| 维度 | XGBoost | LightGBM |
|---|---|---|
| 默认分裂策略 | `max_depth` 级优先 | **leaf-wise**（`num_leaves`）—— 同样叶子数下通常更准，但**更容易过拟合小数据** |
| 类别特征 | 要自己编码（one-hot / target encoding） | **原生支持**（`categorical_feature`），且用直方图分裂，通常优于 one-hot |
| 大数据上的内存 | 相对高 | 相对低（直方图 + 直方图做差） |
| Ray 侧的坑 | 较少 | ⚠️ `params` 里的键名与 XGBoost **完全不同**（`num_leaves` vs `max_depth`、`feature_fraction` vs `colsample_bytree`）—— 抄错不报错，只是效果不对 |

> ⚠️ **LightGBM 的原生类别特征在分布式下要小心**：
> 它依赖类别取值集合的一致性。分片之后如果某个 worker 没见到某个类别，
> 分裂行为会与其他 worker 不一致。**稳妥做法是在 Ray Data 侧编码成整数，
> 并固定一份全局的类别映射表**（见 §34.5）。

---

## 34.5 路径 ②：Ray Data 做特征工程，GBDT 只吃最后那张表

**这是绝大多数团队真正该用的路径**，也是这一章最实用的一节。

核心思想：**把"大"留在 Ray Data 层，把"宽"交给单机 GBDT。**

```python
import ray
import ray.data
import pandas as pd
import numpy as np
import pyarrow.dataset as ds          # ← 谓词下推要用它拼表达式

ray.init()

# ── 1. 行级过滤:这一步把 1000 万行压到 200 万行 ──
#    ⚠️ 用 filter= 让下推生效,不要 map_batches 之后再过滤(第 12 章 §12.3)
raw = ray.data.read_parquet(
    "s3://bucket/raw/events/",
    filter=(ds.field("dt") >= "2026-01-01"),   # 谓词下推
)

# ── 2. 列裁剪 + 特征计算(这一步才是真正的 ETL) ──
def featurize(batch: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["uid_hash"]   = batch["uid"].astype(str).str[-6:].astype("int64")
    out["amount_log"] = np.log1p(batch["amount"].clip(lower=0))
    out["hour"]       = pd.to_datetime(batch["ts"], unit="s").dt.hour
    out["is_night"]   = out["hour"].isin([0, 1, 2, 3, 4, 5]).astype("int8")
    out["amt_per_cnt"] = batch["amount"] / (batch["cnt_7d"] + 1.0)
    out["label"]      = batch["label"].astype("int8")
    return out

feat = raw.map_batches(
    featurize,
    batch_format="pandas",
    batch_size=64_000,          # 第 12 章 §12.6:块别太大也别太小
    compute=ray.data.ActorPoolStrategy(size=8),   # CPU 密集,用 actor 池
)

# ── 4. 全局类别编码:必须在 driver 侧固定一张映射表 ──
#    这是分布式 GBDT 最容易被忽略的一步
CATS = ["city_id", "device_type", "channel"]
mapping = {c: {v: i for i, v in enumerate(  # 从全量数据里取唯一值
    feat.select_columns([c]).unique(c).to_pandas()[c].tolist()
)} for c in CATS}

def encode(batch: pd.DataFrame) -> pd.DataFrame:
    for c in CATS:
        batch[c] = batch[c].map(mapping[c]).fillna(-1).astype("int16")
    return batch

feat = feat.map_batches(encode, batch_format="pandas")

# ── 5. 写回:让下次训练直接读 Parquet,不必重跑 ETL ──
feat.write_parquet("s3://bucket/features/v2026_09/")

# ── 6. 拉到一个 worker 上训练 ──
#    ⚠️ 这一步的前提是上面已经把数据压到单机能装下了
pdf = feat.to_pandas()          # 200 万行 × 20 列 ≈ 几百 MB,可以

import xgboost as xgb
dtrain = xgb.DMatrix(pdf.drop(columns=["label"]), label=pdf["label"])
booster = xgb.train({"objective": "binary:logistic", "tree_method": "hist"},
                    dtrain, num_boost_round=300)
booster.save_model("/mnt/models/xgb_v2026_09.json")
```

> ⚠️ **一个只有踩过才知道的 import 陷阱**：上面写的是
> `import pyarrow.dataset as ds` + `ds.field("dt")`。
> 网上常见的 `import pyarrow.dataset as pa` 然后 `pa.dataset.field("dt")`
> **是错的** —— `pyarrow.dataset` 这个模块**自己就定义了一个叫 `dataset` 的函数**
> （读整个 Dataset 的便捷入口），于是 `pa.dataset` 是那个**函数对象**，
> `.field` 直接 `AttributeError`。`field` 是模块级导出，
> 所以要么 `ds.field(...)`，要么 `from pyarrow.dataset import field`。
> 别名取 `ds` 而不是 `pa`，就是为了不再撞上这个坑。

**六个关键点**：

**① 全局类别映射必须固定，且要**版本化**。**
这是分布式 GBDT 最隐蔽的坑：训练时的编码表和生产推理时的编码表**不一致**，
模型就会静默地给出错误结果（不报错，只是效果差）。
**把 `mapping` 存成文件，和生产代码用同一份**（见第 35 章的"数据指纹"）。

**② `to_pandas()` 会**把全量数据拉到 driver**。**
这是本节唯一的"单点"，也是判据所在：
* 如果 `to_pandas()` 会让 driver OOM，**说明你还不能走路径 ②** ——
  要么继续在 Ray Data 侧聚合/采样，要么走路径 ③；

**③ 别用 `to_pandas()` 拿训练循环的数据。**
要流式喂给单机模型，用 `iter_batches()`（第 12 章 §12.2）：
```python
for batch in feat.iter_batches(batch_size=100_000, batch_format="pandas"):
    ...   # 适合在线学习 / 分块预测,不适合 XGBoost 的 DMatrix
```

**④ 特征工程用 `ActorPoolStrategy` 还是 `TaskPoolStrategy`？**
判据和第 12 章一致：**每个 batch 都要重建重对象（如加载词表）就用 actor 池**，
纯函数式转换用 task 池（更便宜、更好调度）。

**⑤ 写完的 Parquet 是"特征表的版本"**，不是临时文件。
给它一个可追溯的路径（带日期/指纹），第 35 章会讲怎么把它和模型绑起来。

**⑥ 别忘了这一步省下了什么。** 路径 ② 的价值不是"训练更快"
（反正还是单机训练），而是**"ETL 从一台机器变成了一群机器"**——
原本要跑 6 小时的预处理变成了 20 分钟。

---

## 34.6 路径 ④：scikit-learn + Tune（把第 14 章的占位模型换掉）

第 14 章整章在讲 Tune，但它的示例里 `build_model(config)` 是个占位符 ——
**读者看完不知道真实模型长什么样**。这一节把它补成真的。

### 最小可跑的 sklearn Trainable

```python
import numpy as np
from sklearn.datasets import make_classification
from sklearn.model_selection import cross_val_score
from sklearn.ensemble import RandomForestClassifier
from ray import tune

def trainable(config):
    X, y = make_classification(
        n_samples=20_000, n_features=40, n_informative=12, random_state=0)
    clf = RandomForestClassifier(
        n_estimators=config["n_estimators"],
        max_depth=config["max_depth"],
        min_samples_leaf=config["min_samples_leaf"],
        n_jobs=-1,
        random_state=0,
    )
    # ⚠️ 用 cross_val_score 而不是单次 train/test:
    #    单次划分的方差会让 Tune 追着噪声跑
    score = cross_val_score(clf, X, y, cv=3, scoring="roc_auc").mean()
    tune.report({"auc": score})

tuner = tune.Tuner(
    trainable,
    param_space={
        "n_estimators":     tune.choice([200, 400, 800]),
        "max_depth":        tune.choice([None, 8, 16, 32]),
        "min_samples_leaf": tune.lograndint(1, 64),
    },
    tune_config=tune.TuneConfig(
        metric="auc",
        mode="max",
        num_samples=40,
        scheduler=tune.schedulers.ASHAScheduler(       # 第 14 章 §14.5
            metric="auc", mode="max", grace_period=1, reduction_factor=2),
        max_concurrent_trials=8,                       # 别超过机器核数/单 trial 核数
    ),
)
best = tuner.fit().get_best_result(metric="auc", mode="max")
print(best.config, best.metrics)
```

**四件事值得单独说**：

**① `n_jobs=-1` 会与 Tune 的并发打架。**
这是本节最容易出的事故：每个 trial 都用满所有核，`max_concurrent_trials=8`
就会开出 8 × 全部核的线程 —— **机器直接卡死**。
**规则**：要么 `n_jobs=1` + 靠 `max_concurrent_trials` 做并发，
要么 `n_jobs=K` + `max_concurrent_trials ≈ 总核数 / K`。**两者必须配合着算。**

**② `cross_val_score` 比单次划分更适合调参。**
单次 train/test 的 AUC 方差可能有 ±0.03，而你要找的差距往往就是 0.01 ——
**不交叉验证的话，Tune 搜的是噪声**（第 14 章 §14.8 的成本控制里也提到这点）。

**③ `tune.report` 还是 `ray.tune.report`？**
Tune 的训练函数里用 `tune.report`（或 `ray.tune.report`）——
**不要**用 `ray.train.report`（那是 Train 侧的，第 13 章 §13.1）。
两者在 Tune 里混用会有弃用警告。

**④ GBDT 的 Tune 和前面的路径不冲突。**
`tune.Tuner` 的第一个参数既可以是普通函数（上面这样），
也可以是一个 `XGBoostTrainer` 实例（把 Trainer 交给 Tune 搜它的 `params`）。
**后者才是"大 + 要调参"的组合**，第 14 章 §14.3 的 `param_space`
可以直接套到 `XGBoostTrainer` 的 `params` 上。

### 一个不需要改模型的"零改造"超参搜索

如果模型代码已经写好了（一个返回评分的函数），最短路径是：

```python
def trainable(config):
    from my_pipeline import build_features, train_and_eval
    X, y = build_features()                      # 你自己的代码
    auc = train_and_eval(X, y, **config)         # 你自己的代码
    tune.report({"auc": auc})
```

**除了 `tune.report` 那一行，你不需要改任何东西** ——
这正是 Tune 相比"重写训练循环"的价值。

---

## 34.7 判据：**什么时候不该上 Ray**

这一节是本章最重要的。表格数据这一块，"上 Ray"的收益门槛比深度学习高得多。

### 三条硬判据

| 判据 | 上 Ray 划算 | 别上 Ray |
|---|---|---|
| **数据规模** | 训练集 > 单机内存 | 单机能装下 → `n_jobs=-1` 就够 |
| **耗时** | 单次训练 > 1 小时且瓶颈是**数据量** | 瓶颈是**特征维度/树深度** → 加机器没用 |
| **流水线** | 上下游本来就跑在 Ray 上 | 孤立的一次性脚本 → 不值得引入 |

### 四个"看起来该用、其实不该用"的场景

**① "我有 1TB 日志要处理" —— 但训练集只有 2GB。**
→ 用路径 ②（Ray Data 做 ETL），**训练留在单机**。
多机 GBDT 的通信开销会让你亏回去。

**② "我机器有 128 核，但 XGBoost 只用了 8 核"。**
→ 先查 `nthread` / `n_jobs` 参数，再看是不是数据加载瓶颈。
**这不是分布式问题，是配置问题** —— 调参数比上集群便宜一百倍。

**③ "我要搜 1000 组超参"。**
→ 用路径 ④（Tune + 单机模型）。**这属于"调度层"的并行，
不是"训练层"的并行** —— 每组超参本身还是单机能跑完的。
用 `XGBoostTrainer` 反而给每组超参都套一层多机，纯浪费。

**④ "我的数据只有 10 万行，但特征有 5 万列"。**
→ 这是**宽**问题不是**大**问题。加机器不解决宽，
应该先做特征选择 / 降维。GBDT 在 5 万列上的瓶颈是**每轮的直方图构建**，
它是按列并行的，多机聚合反而放大开销。

### 一个反面对照表

| 你的情况 | 看起来该用 | 实际该用 | 为什么 |
|---|---|---|---|
| 100GB 原始数据 → 2GB 训练集 | `XGBoostTrainer` | Ray Data ETL + 单机 XGBoost | 多机 GBDT 通信 > 收益 |
| 50GB 训练集,单机 OOM | 采样 | `XGBoostTrainer` | 这正是它存在的理由 |
| 200 万行,想调 200 组超参 | `XGBoostTrainer` × 200 | Tune + 单机 | 并行粒度错了 |
| 单机跑 30 分钟 | Ray | 单机 | 引入复杂度不值得 |

---

## 34.8 checkpoint 与模型落盘

### `XGBoostTrainer` 的 checkpoint 里是什么

`result.checkpoint` 是一个标准的 Ray `Checkpoint`，里面是**训练好的 booster**
（XGBoost 的 `model.json` / LightGBM 的 `model.txt` 一类框架原生格式）。

```python
result = trainer.fit()

with result.checkpoint.as_directory() as d:
    import os
    print(os.listdir(d))          # 看看到底有哪些文件再取
    # ⚠️ 不要照抄别人博客里的文件名 —— 不同版本的布局可能不同(第 29 章 §29.3 的
    #    "checkpoint 目录嵌套"就是同一个坑的另一面),先 listdir 确认
    booster = xgb.Booster()
    booster.load_model(os.path.join(d, "<上面列出来的那个文件>"))
```

> ⚠️ **`as_directory()` 是上下文管理器，也可能返回 `str`** ——
> 第 13 章 §13.10 已经提醒过这一点。用 `with` 是最稳的写法。

### 三个落盘约定（和深度学习完全一致）

| 约定 | 做法 | 理由 |
|---|---|---|
| **别写本地路径** | 不要 `booster.save_model("/tmp/xgb.json")` | 抢占/重启后 `/tmp` 就没了 |
| **走 `ray.train.report(checkpoint=...)`** | 让 Ray 管目录 | 这是抢占恢复能工作的前提（第 13 章 §13.5） |
| **控制保留份数** | `CheckpointConfig(num_to_keep=2)` | 默认 `None` = 全留 = 写爆共享存储 |

### 模型的"产物"不只是权重

GBDT 的部署产物**至少包含三样东西**，少一样就会在生产上出问题：

```
① 模型文件          model.json / model.txt
② 特征顺序 + 编码表  [("amount_log", "float32"), ("city_id", "int16"), ...]
                    + 类别映射 {"city_id": {"北京": 0, "上海": 1, ...}}
③ 训练时的参数       params / num_boost_round / ray 与 xgboost 的版本号
```

**② 是最常被漏掉、也最容易造成事故的一项。**
特征顺序错一位，模型不报错，只是预测全错 —— 这就是第 35 章
§35.3「数据指纹」要解决的问题。

---

## 34.9 与 mini-ray 的关系

**mini-ray 没有实现 Ray Train / Ray Data / Tune**，
也没有实现任何 GBDT 相关的东西 —— 它的"明确不做"清单里写了
Ray Data/Train/Tune/Serve/RLlib。这是**设计取舍**，不是遗漏：
mini-ray 的目标是 Ray **Core** 的机制，而这一章讲的全是上层库。

> 但**有一个连接点是真实的**：这一章所有"分布式"的部分，
> 底层都是第 8 章讲的**资源模型 + 放置组**。
> `ScalingConfig(num_workers=4)` 展开成一个 `PlacementGroup`
> （`PACK` 策略 = 尽量挤同一节点），`use_gpu=False` 表示
> bundle 里不申请 GPU 资源 —— 这些机制 **mini-ray 全都实现了**，
> 可以用 `mini-ray/examples/05_scheduling.py` 和
> `examples/10_parameter_server.py` 亲手观察。

**想动手验证"分布式 GBDT 的收益在哪"**：本章不需要 GPU，
可以在单机上用 `ScalingConfig(num_workers=1/2/4/8)`
跑同一个 `XGBoostTrainer`，看 `result.metrics` 里的训练耗时怎么变 ——
**这就是本章 §34.3 那句「加机器不一定更快」的实测方法。**

---

## 34.10 本章小结

* **四条路径**：① 原样单机 · ② Ray Data 做 ETL + 单机训练 ·
  ③ `XGBoostTrainer` / `LightGBMTrainer`（训练集本身放不下）·
  ④ Tune + 单机模型（要搜超参）。**判据是"训练集大小"，不是"原始数据大小"。**
* **GBDT 的分布式是"数据并行 + 直方图聚合"**，所有 worker 的模型**逐位相同**；
  通信量随 worker 数增长，**加机器有天花板**（经验上 4–16 是常见区间）。
* **`use_gpu=True` 对 GBDT 通常不划算** —— 先跑 CPU，别默认加卡。
* **路径 ② 是大多数团队该用的**：把"大"留在 Ray Data 层，
  把"宽"交给单机 GBDT。`to_pandas()` 会不会 OOM 就是判据。
* **全局类别编码表必须固定且版本化** —— 训练/推理不一致会静默给出错误结果。
* **sklearn + Tune 是"调度层并行"不是"训练层并行"**：
  `n_jobs` 与 `max_concurrent_trials` **必须配合着算**，否则机器直接卡死。
* **部署产物 = 模型 + 特征顺序与编码表 + 训练参数**，
  漏掉中间那项是最常见的事故来源（第 35 章展开）。
* **这一章全程不需要 GPU** —— 表格数据的门槛本来就是 CPU 集群。

> **下一章**：第 35 章《数据版本、产物血缘与模型注册》——
> 本章留下的那个悬念（"这个线上模型是用哪份数据、哪组参数训出来的"）
> 在那里回答。第 32 章 §32.5 说"模型注册是你的事"，
> 第 35 章把那件事讲完。
