# 第 45 章 上线之后：模型监控、漂移检测与再训练闭环

> 第 43 章把模型送到线上，第 44 章把评测数据变成可版本化的资产。本章回答最后一个问题：模型上线之后，怎么知道它“还活着”？MLflow 本身不是监控系统，但它保存的模型版本、数据集 digest、run 指标和 trace/feedback 恰好是监控的锚点。MiniMLflow 新增 `monitors` / `monitor_windows` 两张表（schema v7）、`MonitoringService`、PSI/KS/卡方统计、REST/客户端/CLI、示例 18 与 12 个测试；真实 MLflow 的集成方式与生产差距在本章逐项给出。

## 学习目标

- 区分服务指标、数据漂移、预测漂移、性能衰减与业务指标，知道每类问题需要什么证据。
- 设计一个 monitor：基线从哪来、特征契约怎么定、阈值谁负责、窗口如何切。
- 理解 PSI、KS、卡方/Cramér's V 的公式、适用条件与常见误用，不用一个阈值打天下。
- 把 Mini 的 `MonitoringService` 映射到真实 MLflow 的 run/metric/artifact/dataset/registry 与外部监控栈。
- 设计告警、值班与再训练闭环，避免“一漂移就自动重训”的反射式运维。
- 诚实评估教学实现与生产监控之间的差距：流式聚合、标签延迟、分群、告警投递、成本与治理。

## 45.1 为什么监控不属于 MLflow 的核心

MLflow 的数据模型围绕“一次实验/一次请求/一个资产”组织：

```text
Experiment → Run → Metric history
                 → Artifact
                 → LoggedModel → ModelVersion → Alias
Trace → Span → Assessment
Dataset → DatasetRecord
```

监控的数据模型围绕“时间窗口 + 基线 + 告警”组织：

```text
Baseline (reference distribution)
   × Window (t1..t2)
   → statistic (PSI/KS/chi-square/...)
   → threshold decision
   → alert / ticket / dashboard
```

两者不是一回事。把每个预测都写成一条 trace，再在 MLflow 里做聚合，会得到一个又贵又慢的“伪监控系统”；反过来，只用一个 Prometheus 指标 `model_auc`，又无法回答“是哪个特征、哪个版本、哪批数据变了”。

正确的分工是：

| 层 | 负责 | 典型系统 |
|---|---|---|
| 身份与血缘 | 模型是谁、版本、来源 run、数据集 digest、prompt/trace | MLflow |
| 请求与标签存储 | 每次预测的输入/输出、延迟标签、人工反馈 | 数据仓库、特征平台、MLflow trace（采样） |
| 时间序列与告警 | 窗口聚合、阈值、去重、值班、面板 | Prometheus/Grafana、云监控、Evidently/WhyLabs |
| 决策与发布 | 回滚、重训、灰度、审批 | Registry alias + CI/CD + 人工 |

本章的立场是：**MLflow 不替你做监控，但监控必须引用 MLflow 的身份**。一个漂移报告如果不知道“哪个模型版本、哪份基线数据、哪次窗口”，它只是一张无法复盘的图。

### 45.1.1 不要把 3.x 的可观测性当成模型监控

MLflow 3.9 之后的 Trace Dashboard、成本视图、Gateway 预算与 guardrail，解决的是“LLM 调用链与花费”的可观测性；3.17 的 trace SQL 汇总也只是把 trace 查询变快。它们不等于“特征分布漂移”或“线上 AUC 衰减”：

- trace 记录的是被采样的一次调用，不是全量预测日志；
- trace 的 payload 可能含 PII，不适合长期保存；
- LLM 的“质量”需要 judge/人工评估，传统模型的“性能”需要延迟标签；
- 预算和限流是成本控制，不是数据分布监控。

把这两类能力混在一起，会导致两个极端：要么以为“有 trace 就不用监控”，要么把监控数据塞进 trace 表把存储打爆。

### 45.1.2 Mini 的范围

Mini 实现一个**批式、单进程、SQLite** 的最小闭环：

- monitor 定义与基线 profile 持久化；
- 一次窗口评估（不是流式）；
- PSI、两样本 KS、卡方/Cramér's V、缺失率、均值漂移；
- 不可变的窗口报告；
- REST/客户端/CLI/示例/测试。

它不实现：流式聚合、标签 join、告警投递、分群、自动分箱、分布式 sketch、权限与保留期。第 45.9 节逐项说明生产要补什么。

## 45.2 三个平面与一条时间轴

### 45.2.1 三个平面

**服务平面**回答“系统是否健康”：

- 延迟 p50/p95/p99、首 token 延迟；
- 错误率、超时、重试、限流命中；
- CPU/内存/GPU、队列长度、连接池；
- 每请求/每租户成本。

这些指标通常来自网关、服务框架和云监控，不应该由 MLflow 采集。MLflow 只需要记录“这次部署用了哪个模型版本”，让监控面板能把指标按版本切片。

**数据平面**回答“输入是否变了”：

- 数值特征：均值/方差、分位数、PSI、KS；
- 类别特征：类别分布、卡方、未见过类别、缺失率；
- 预测分数：分布、正例率、分桶校准；
- 数据质量：空值、越界、类型错误、重复。

数据平面可以在**没有标签**的情况下工作，因此是最早的预警信号；但它只能说明“输入变了”，不能证明“模型变差了”。

**结果平面**回答“模型是否还有效”：

- 有标签后：AUC、F1、召回@K、MAE、校准误差；
- 业务指标：转化率、流失率、客诉、人工采纳率；
- 分群：新/老客户、地区、套餐、渠道；
- 成本：误判损失、干预成本、补偿成本。

结果平面需要标签，而标签往往延迟、缺失或有选择偏差。这是监控系统最容易被忽视的部分。

### 45.2.2 一条预测的时间轴

```text
t0  请求到达
    ├─ 记录 request_id、model_version、features、prediction、score
    ├─ 采样写入 trace / 预测日志（注意 PII）
t1  预测返回（毫秒级）
    ├─ 服务指标：延迟、错误、成本
t2  人工/系统动作（小时~天）
    ├─ 是否干预、是否采纳、是否申诉
t3  真实标签到达（天~月）
    ├─ 是否流失、是否违约、是否有缺陷
t4  计算窗口指标（天/周）
    ├─ 数据漂移、预测漂移、性能、业务
```

关键结论：

- **数据漂移可以在 t1 之后就算**，不需要标签；
- **性能衰减必须等 t3**，而且只有拿到标签的那部分样本；
- **标签缺失不是随机缺失**：被人工处理的客户、被投诉的订单、被复核的样本，天然有更高的标签率；
- **告警的时间尺度不同**：服务指标分钟级，数据漂移小时/天级，性能周/月级。用同一个告警通道和同一个阈值，会被噪声淹没。

### 45.2.3 归因链

一条可复盘的监控报告至少应带：

```text
model_name + model_version（或 alias 解析出的具体版本）
+ baseline_dataset_id + baseline_digest
+ feature pipeline / prompt 版本（如适用）
+ window_start/window_end + sample_count
+ 统计量 + 阈值 + 结论
+ 生成时间 + 生成者（服务/人）
```

缺少任何一项，事故复盘都会退化成“上周好像漂了”。

## 45.3 定义监控对象：基线、契约与阈值

### 45.3.1 基线从哪里来

三种常见基线，各有代价：

| 基线 | 优点 | 风险 |
|---|---|---|
| 训练集分布 | 与模型学到的分布一致 | 训练集可能过时、含历史偏差、太大 |
| 评审通过的评测集 | 有数据契约、可复现、digest 稳定 | 可能不覆盖线上长尾 |
| 上一个稳定窗口 | 贴近当前业务、能捕捉季节性 | 会“漂移归一化”，慢慢把异常当正常 |

生产上通常组合使用：以评测集/训练集为**参考基线**，以上一个稳定窗口为**运营基线**，两者都保留。前者回答“离模型学到的世界有多远”，后者回答“最近有没有突变”。

Mini 支持两种来源：

- 显式传入 `baseline_records`；
- 传 `dataset_id`，直接使用第 44 章的受管数据集，并把 `dataset_id` 与 `digest` 写入 monitor。

```python
monitor = client.create_monitor(
    "churn-monitor",
    [],
    {"tenure": "numeric", "plan": "categorical"},
    dataset_id=dataset.dataset_id,
)
```

这样做的好处是：基线不是一份“复制出来的 CSV”，而是有身份、有版本、可审计的数据集。数据集被合并出新版本后，monitor 的 digest 不会自动变化；要更新基线，必须显式创建新 monitor 或走一次受控的 baseline 更新流程。

### 45.3.2 特征契约

监控不是“把所有列都算一遍 PSI”。你需要先声明：

- 哪些特征是**数值**，哪些是**类别**；
- 数值特征的分箱数（Mini 默认 10，范围 2–50）；
- 缺失值是否允许、缺失率上限；
- 类别基数上限（Mini 最多保留 100 个类别，其余归入 `<other>`）；
- 预测分数是否作为一个被监控的“特征”；
- 哪些特征属于同一业务分群（Mini 不实现分群，生产必须做）。

一个契约示例：

```json
{
  "tenure": {"kind": "numeric", "bins": 10},
  "monthly_charges": {"kind": "numeric", "bins": 10},
  "plan": {"kind": "categorical"},
  "support_calls": {"kind": "numeric", "bins": 6},
  "churn_score": {"kind": "numeric", "bins": 10}
}
```

契约的价值在于**把“监控什么”从代码里拿出来**。否则每次新增特征都要改监控脚本，最后没人知道哪些列真的被监控了。

### 45.3.3 阈值是组织决策，不是统计常数

常见的 PSI 经验阈值：

```text
PSI < 0.10        稳定
0.10 <= PSI < 0.25 需要调查
PSI >= 0.25       显著漂移，考虑动作
```

但它不是法律：

- 高基数类别、长尾分布、样本量很小的窗口，PSI 会剧烈波动；
- 业务对误报的容忍度不同：支付风控宁可多报，推荐排序可能更怕打扰；
- 同一阈值不能同时用于“全量”和“分群”；
- 阈值应该绑定**动作**：WARN 触发谁看、ALERT 触发什么 runbook、多久没处理升级。

Mini 的默认阈值：

```python
DEFAULT_THRESHOLDS = {
    "psi_warn": 0.10,
    "psi_alert": 0.25,
    "ks_stat": 0.10,
    "ks_pvalue": 0.01,
    "chi2_pvalue": 0.05,
    "missing_rate_warn": 0.05,
    "missing_rate_alert": 0.20,
    "unseen_rate_warn": 0.05,
    "unseen_rate_alert": 0.20,
    "mean_shift_warn": 0.50,
    "min_samples_warn": 30,
}
```

`min_samples_warn` 不是统计检验的一部分，而是工程保护：2 条样本算出来的 PSI 没有决策价值，应该先降级为“样本不足”。

## 45.4 统计方法：PSI、KS、卡方与阈值

### 45.4.1 PSI：分布的整体位移

PSI（Population Stability Index）比较两个分布在同一些桶里的占比：

```text
PSI = Σ (actual_i% - expected_i%) × ln(actual_i% / expected_i%)
```

实现细节：

- 数值特征用基线分位数切桶（Mini 用线性插值分位数），窗口数据按同一边界落桶；
- 类别特征按基线类别计数，窗口出现的新类别进入并集；
- 空桶加一个很小的平滑项（Mini 用 `1e-6`），否则 `ln(0)` 会爆炸；
- 窗口中没有值的特征不能算 PSI，应单独报“全缺失”。

PSI 的优点是可解释、可跨特征比较；缺点是：

- 对分箱敏感：10 桶和 20 桶可能给出不同结论；
- 对样本量敏感：小窗口的随机波动会被放大；
- 对类别基数敏感：新类别会让 PSI 飙升，但这可能是业务上新套餐，而不是模型坏了；
- 只反映边际分布，不反映特征之间的关系。

### 45.4.2 KS：数值分布的最大距离

两样本 Kolmogorov-Smirnov 统计量：

```text
D = max_x |F_baseline(x) - F_window(x)|
```

Mini 的实现要点：

- 基线保留最多 2000 个排序后的样本值（`sample`），窗口用全部值；
- 按合并后的唯一值逐点推进两个经验 CDF；
- 在同一个值上先消费完所有并列项，再计算差值，否则相同数据也会得到 `1/n` 的假距离；
- p 值用渐近公式（有效样本量 + 修正项），大样本下很小的差异也会显著。

因此 Mini 要求**同时**满足：

```text
D >= ks_stat 且 p <= ks_pvalue
```

只报 p 值会把“样本量很大”误当成“漂移很大”；只报 D 又忽略抽样波动。

### 45.4.3 卡方与 Cramér's V：类别分布

类别特征用卡方检验比较观测频数与基线期望频数：

```text
χ² = Σ (O_i - E_i)² / E_i
E_i = baseline_proportion_i × window_n
```

Mini 同时报告：

- `chi2` 与 `chi2_pvalue`（Wilson-Hilferty 近似，教学够用，监管场景请用统计库）；
- `cramers_v`（效应量，避免“大样本一切显著”）；
- `unseen_categories` 与 `unseen_rate`；
- `low_expected_counts`：期望频数 < 5 时卡方不可靠。

类别监控最常见的真实事故不是“分布缓慢变化”，而是**新类别没有对应模型参数**：新套餐、新渠道、新设备类型。PSI 和 unseen rate 会同时爆表，这时正确动作通常是“回退到默认策略/规则”，而不是立刻重训。

### 45.4.4 缺失率、均值漂移与预测漂移

- **缺失率**：`window_missing_rate - baseline_missing_rate`。上游字段改名、ETL 失败、客户端版本升级，都会先表现为缺失率变化。
- **均值漂移**：`(window_mean - baseline_mean) / baseline_std`，单位是标准差。它对连续漂移敏感，但对分布形状变化不敏感。
- **预测漂移**：把模型输出 `score` 当成一个数值特征监控。分数分布变化通常比特征漂移更接近业务，但它仍然不能证明性能下降。
- **校准漂移**：分桶比较预测概率与真实正例率。需要标签，且样本量要足够。

### 45.4.5 一个真实数字的例子

用示例 18 的合成数据（200 条基线、200 条窗口）：

| 特征 | 稳定窗口 PSI | 漂移窗口 PSI | 漂移窗口 KS | 结论 |
|---|---:|---:|---:|---|
| tenure | 0.00 | 17.20 | D=0.90, p≈0 | 新客 tenure 骤降 |
| monthly_charges | 0.00 | 13.12 | D=0.755, p≈0 | 客单价整体上移 |
| plan | 0.00 | 21.72 | — | 出现 enterprise 新类别（50%） |
| support_calls | 0.00 | 9.36 | D=0.70, p≈0 | 咨询次数上升 |

稳定窗口四条 PSI 都是 0，漂移窗口全部 ALERT。这个例子刻意把差异做得很大，目的是演示“报告长什么样”；真实场景的 PSI 往往在 0.05–0.3 之间，需要结合样本量、分群和业务解释。

### 45.4.6 漂移不等于退化

这是本章最重要的一句话：

```text
数据漂移 ≠ 模型性能下降
模型性能下降 ≠ 业务指标下降
业务指标下降 ≠ 模型是原因
```

可能的反例：

- 输入分布变了，但新样本恰好落在模型学得很好的区域，AUC 不降反升；
- 输入没变，但标签定义/业务流程变了，性能下降；
- 性能下降，但业务指标因为干预策略改了好转而上升；
- 业务指标下降，原因是供给、价格、竞品，而不是模型。

因此监控系统应该输出**证据**，而不是直接输出“重训”命令。动作必须经过决策矩阵（第 45.7 节）。

## 45.5 MiniMLflow 实现：两张表与一个服务

### 45.5.1 Schema v7

```sql
CREATE TABLE monitors (
    monitor_id              TEXT PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    model_name              TEXT NOT NULL DEFAULT '',
    model_version           TEXT NOT NULL DEFAULT '',
    baseline_json           TEXT NOT NULL,
    thresholds_json         TEXT NOT NULL DEFAULT '{}',
    tags_json               TEXT NOT NULL DEFAULT '{}',
    baseline_dataset_id     TEXT NOT NULL DEFAULT '',
    baseline_dataset_digest TEXT NOT NULL DEFAULT '',
    created_time            INTEGER NOT NULL,
    last_updated_time       INTEGER NOT NULL
);

CREATE TABLE monitor_windows (
    window_id     TEXT PRIMARY KEY,
    monitor_id    TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    window_end    INTEGER NOT NULL,
    sample_count  INTEGER NOT NULL CHECK (sample_count >= 0),
    status        TEXT NOT NULL DEFAULT 'STABLE'
                      CHECK (status IN ('STABLE', 'WARN', 'ALERT')),
    metrics_json  TEXT NOT NULL DEFAULT '{}',
    report_json   TEXT NOT NULL DEFAULT '{}',
    created_time  INTEGER NOT NULL,
    FOREIGN KEY (monitor_id) REFERENCES monitors(monitor_id) ON DELETE CASCADE
);
```

设计选择：

- **monitor 名称唯一，ID 才是身份**：改名不应破坏历史窗口；
- **baseline 是派生 JSON，不允许用户手填**：避免“元数据说一套、数据是另一套”；
- **窗口不可变**：每次评估生成新行，修正要新增窗口并注明原因，不能覆盖历史；
- **删除 monitor 级联删除窗口**：教学实现选择“资产删除即清理证据”，生产应改为软删除/保留期；
- **baseline 里保存最多 2000 个样本值**：PSI 用直方图，KS 用真实样本；只存直方图会在并列值上产生假漂移。

### 45.5.2 基线 profile

```json
{
  "record_count": 200,
  "features": {
    "tenure": {
      "kind": "numeric",
      "count": 200,
      "missing": 0,
      "missing_rate": 0.0,
      "mean": 30.5,
      "std": 17.32,
      "min": 1.0,
      "max": 60.0,
      "bins": 10,
      "edges": [1.0, 6.9, 12.8, "..."],
      "hist": [20, 20, 20, "..."],
      "sample": [1.0, 1.0, "..."]
    },
    "plan": {
      "kind": "categorical",
      "count": 200,
      "missing": 0,
      "missing_rate": 0.0,
      "counts": {"basic": 133, "pro": 67}
    }
  },
  "spec": {"tenure": {"kind": "numeric", "bins": 10}},
  "digest": "sha256:..."
}
```

`digest` 覆盖整个 baseline（包括分箱边界与样本），因此“同一批数据、不同分箱”会被识别为不同基线。这是刻意的：分箱变了，PSI 就不可比。

### 45.5.3 评估流程

```text
record_window(monitor_id, records)
  ├─ 读取 monitor（不存在 -> 404）
  ├─ 规范化记录：dataset record 取 inputs；普通行直接使用
  ├─ 对每个特征：
  │    ├─ 数值 -> 分桶、PSI、KS、均值漂移、缺失率
  │    └─ 类别 -> 并集计数、PSI、卡方/Cramér's V、unseen、缺失率
  ├─ 汇总严重度：STABLE < WARN < ALERT
  ├─ 样本数 < min_samples_warn -> 至少 WARN
  └─ 写 monitor_windows（不可变）+ 返回报告
```

每个特征的报告同时包含统计量、阈值命中的原因和状态。顶层 `report["reasons"]` 是所有特征原因的汇总，方便告警消息直接引用：

```json
{
  "status": "ALERT",
  "sample_count": 200,
  "reasons": [
    "tenure: PSI 17.204 >= 0.25",
    "tenure: KS D=0.900, p=0.0000 (threshold 0.1, 0.01)",
    "plan: unseen-category rate 0.500 >= alert"
  ]
}
```

### 45.5.4 失败模式与防护

| 场景 | Mini 行为 | 生产要补 |
|---|---|---|
| 某特征全缺失 | 该特征 ALERT，原因 `no non-missing values in window` | 上游 ETL 告警、字段契约校验 |
| 新类别 | PSI 上升 + `unseen_rate` + 类别列表 | 新类别映射、默认策略、模型回退 |
| 常量基线 | 单独处理“等于/不等于”，避免除零 | 业务上确认常量是否合理 |
| 小样本 | `min_samples_warn` 至少 WARN | 置信区间、最小样本门槛 |
| 期望频数 < 5 | 标记 `low_expected_counts` | 合并类别或用精确检验 |
| 并列值多 | 保留样本 + 按唯一值比较 CDF | 离散特征用专门检验 |
| NaN/Infinity | 拒绝并返回 400 | 上游数据质量门禁 |
| 窗口时间倒置 | 拒绝并返回 400 | 时区/时钟同步 |

### 45.5.5 REST、客户端与 CLI

REST：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/2.0/mlflow/monitors/create` | 创建 monitor |
| GET | `/api/2.0/mlflow/monitors/get` | 读取 |
| GET | `/api/2.0/mlflow/monitors/list` | 列表 |
| POST | `/api/2.0/mlflow/monitors/delete` | 删除（级联窗口） |
| POST | `/api/2.0/mlflow/monitors/windows/create` | 评估并持久化窗口 |
| GET | `/api/2.0/mlflow/monitors/windows/list` | 历史窗口 |

Python：

```python
monitor = client.create_monitor(
    "churn-monitor",
    baseline_rows,
    {"tenure": "numeric", "plan": "categorical"},
    model_name="churn",
    model_version="3",
    thresholds={"psi_alert": 0.3},
)
window = client.record_monitor_window(
    monitor.monitor_id, production_rows, window_start=..., window_end=...
)
print(window.status, window.report["reasons"])
```

CLI：

```bash
python -m mini_mlflow monitoring create churn-monitor \
  --baseline-file baseline.json \
  --feature tenure:numeric --feature plan:categorical \
  --model-name churn --threshold psi_alert=0.3

python -m mini_mlflow monitoring window <monitor_id> \
  --records-file window.json --window-start 1000 --window-end 2000

python -m mini_mlflow monitoring windows <monitor_id> --limit 20
```

### 45.5.6 测试证明了什么

`tests/test_monitoring.py` 的 12 个测试覆盖：

1. 基线统计、数据集绑定、digest、重名；
2. 稳定窗口 PSI≈0 且窗口持久化；
3. 数值漂移触发 PSI/KS ALERT；
4. 新类别触发 unseen ALERT；
5. 全缺失与缺失率；
6. 自定义阈值 + 小样本 WARN；
7. 常量基线；
8. 删除级联；
9. 非法输入（空记录、未知特征、NaN、时间倒置、dataset+records 同时传）；
10. 统计函数（PSI、KS、卡方临界值）；
11. REST/客户端往返；
12. CLI 创建/评估/列表/删除。

测试的重点不是“代码能跑”，而是**每个告警都必须有可解释的原因**。只断言 `status == "ALERT"` 而不检查 `reasons`，等于把监控变成黑盒。

### 45.5.7 示例 18 的输出

```text
stable:  STABLE, PSI all 0.00
drifted: ALERT,  PSI tenure=17.204, plan=21.716,
         monthly_charges=13.121, support_calls=9.357
reasons: tenure KS D=0.900; plan unseen-category rate 0.500; ...
windows: 2
```

示例同时把 PSI/KS 与完整报告写入一个 run 的 metric/artifact，演示“监控结果进入 MLflow 的 run 图谱”，而不是停留在脚本 stdout。

## 45.6 接入真实 MLflow：四种集成方式

### 45.6.1 方式一：把漂移指标写进 run

最简单也最通用的方式：每次窗口评估创建一个 run，把统计量写成 metric，把报告写成 artifact。

```python
import json
import mlflow

with mlflow.start_run(run_name="drift-check-2026-11-26"):
    mlflow.set_tag("monitor.name", "churn-monitor")
    mlflow.set_tag("model.version", "3")
    mlflow.set_tag("baseline.dataset_id", dataset_id)
    mlflow.set_tag("baseline.digest", baseline_digest)
    mlflow.log_metric("drift/tenure/psi", 0.07)
    mlflow.log_metric("drift/plan/unseen_rate", 0.01)
    mlflow.log_dict(report, "drift/report.json")
```

优点：

- 复用 MLflow 的实验、对比、artifact 与权限；
- 监控历史与训练历史在同一个 UI 里；
- 报告可下载、可审计。

缺点：

- 每个窗口一个 run 会放大元数据量；
- metric 不适合高频写入（分钟级窗口应进 TSDB）；
- 不适合做实时告警（MLflow 没有告警引擎）。

实践建议：**小时/天级窗口写 MLflow，分钟级指标写 Prometheus**。

### 45.6.2 方式二：用受管数据集做基线

```python
import mlflow
from mlflow.genai.datasets import create_dataset

dataset = create_dataset(
    name="churn-monitor-baseline",
    records=[{"inputs": row} for row in baseline_rows],
)
monitor = {
    "dataset_id": dataset.dataset_id,
    "digest": dataset.digest,
    "features": {"tenure": "numeric", "plan": "categorical"},
}
```

把 `dataset_id`/`digest` 写进 monitor 的 tag 或报告。这样“基线是什么”不靠口头约定，而是一个可查询、可合并、可删除的资产。真实 MLflow 的 `mlflow.data` 还能记录 source、schema、profile；评测数据集 API 在 3.x 仍快速演进，执行前查锁定版本文档。

### 45.6.3 方式三：用 trace/assessment 做标签与反馈

Trace 不是全量预测日志，但它是高质量样本的来源：

```python
traces = mlflow.search_traces(
    experiment_ids=[experiment_id],
    filter_string="attributes.status = 'OK'",
    max_results=500,
)

for trace in traces["trace"]:
    # assessment 里可能有 label / expectation / 人工反馈
    for assessment in trace.data.assessments:
        if assessment.name == "correct":
            label = assessment.value
```

注意：

- trace 通常是采样后的，不能直接当全量指标；
- assessment 的 `source_type` 要区分 HUMAN/LLM_JUDGE/CODE；
- feedback 是“人觉得好不好”，expectation 是“正确答案是什么”，不要混用；
- 标签回填要记录 join key（request_id/trace_id），否则无法复盘。

### 45.6.4 方式四：用 Registry alias 做版本归因

监控报告必须能回答“这是哪个版本”。部署系统读 alias，监控系统写具体版本：

```python
from mlflow import MlflowClient

client = MlflowClient()
version = client.get_model_version_by_alias("churn", "champion")
report["model_version"] = version.version
report["model_source"] = version.source
report["run_id"] = version.run_id
```

回滚时 alias 指回旧版本，监控面板按 `model_version` 切片就能看到“回滚后指标是否恢复”。不要用 `latest` 做归因：latest 会变，历史报告会失去意义。

### 45.6.5 Mini 与真实 MLflow 的映射

| Mini | 真实 MLflow | 说明 |
|---|---|---|
| `monitors` | 无原生表 | 用 run tags/artifacts 或外部配置保存定义 |
| `monitor_windows` | run + metric + artifact | 或者外部 TSDB |
| baseline digest | dataset digest / `mlflow.data` | 受管数据集是首选 |
| model_name/model_version | Registry name/version/alias | 部署清单固化 |
| `record_window` | 离线 job/流水线 | 不应该在每个请求里调用 |
| `reasons` | report artifact + alert message | 告警要带证据 |
| CLI `monitoring` | 无原生命令 | 用脚本/工作流封装 |

## 45.7 告警、值班与再训练闭环

### 45.7.1 告警设计

一个可运营的告警至少包含：

```text
what:    churn-monitor / tenure / PSI
value:   0.31 (threshold 0.25)
when:    2026-11-26 02:00-03:00 UTC
scope:   model churn v3, baseline dataset d-...
why:     PSI + KS + mean shift; 新客占比上升
runbook: docs/runbooks/churn-drift.md
owner:   #ml-oncall
```

反模式：

- 每个特征一条告警：一个窗口 20 个特征 → 20 条消息；
- 没有去重/分组：同一根因重复轰炸；
- 只有“ALERT”没有“谁处理、怎么处理”；
- 阈值从不复审：业务变了，阈值还是两年前的；
- 告警直接触发重训：没有人工确认，可能把噪声写进模型。

推荐分级：

| 级别 | 触发 | 动作 |
|---|---|---|
| INFO | 单特征 WARN | 记录，周报汇总 |
| WARN | 多特征 WARN 或单特征持续 WARN | 值班查看，检查上游数据 |
| ALERT | 关键特征 ALERT / unseen 激增 / 性能下降 | 立即排查，准备回滚 |
| PAGE | 服务指标 + 数据漂移同时异常 | 呼叫值班，启动事故流程 |

### 45.7.2 决策矩阵

| 数据漂移 | 性能下降 | 业务指标下降 | 建议动作 |
|---|---|---|---|
| 否 | 否 | 否 | 继续观察 |
| 是 | 否 | 否 | 调查数据/业务变化，不急于重训 |
| 是 | 是 | 是 | 回滚或重训，优先止损 |
| 否 | 是 | 是 | 检查标签、评估集、业务流程；可能是概念漂移 |
| 是 | 是 | 否 | 可能是分群/阈值问题，先做切片分析 |
| 否 | 否 | 是 | 先排除非模型因素（供给、价格、竞品） |

这张表不是决策自动化，而是**防止单指标反射**。真实决策还需要：

- 置信区间：性能变化是否超出抽样误差；
- 分群：整体没变，但某个高价值分群崩了；
- 成本：误判/漏判的代价是否值得重训；
- 时间：标签延迟是否让“性能下降”只是数据还没到。

### 45.7.3 再训练的护栏

如果决定重训：

1. **冻结评测集**：用第 44 章的受管数据集，记录 digest；不能边训边改评测集。
2. **数据质量门禁**：缺失率、越界、重复、标签分布；坏数据不进训练。
3. **基线对比**：新模型必须在固定评测集上超过当前 champion，且切片不出现断崖。
4. **审批与灰度**：alias `challenger` → canary → `champion`；保留回滚窗口。
5. **监控继承**：新版本必须绑定新的 baseline（或明确复用旧基线并说明理由）。
6. **记录决策**：为什么重训、用了哪份数据、谁批准、预期什么变化。

反模式：`if drift: retrain()`。这会把监控变成噪声放大器，也会让模型悄悄适应数据泄漏或标签错误。

### 45.7.4 一个事故复盘示例

```text
02:00  数据平面 ALERT：plan 出现 enterprise（50%），unseen_rate=0.5
02:05  值班确认：上游套餐系统新增企业版，特征管道未映射
02:10  业务确认：企业版客户不应走现有流失模型，改用规则策略
02:15  服务回退：网关路由到默认策略，模型分数仅记录不决策
02:30  数据修复：特征管道增加 enterprise 映射，回填历史窗口
次日   重训评估：新类别样本足够后，重新训练并走灰度
```

关键点：**先止损，再修数据，最后才是模型**。模型不是所有数据问题的答案。

## 45.8 成本、容量与治理

### 45.8.1 存储成本

监控成本主要来自：

```text
窗口数 = 监控对象数 × 特征数 × 时间粒度 × 保留期
```

例如 20 个模型 × 30 个特征 × 每小时 1 个窗口 × 保留 90 天 ≈ 129.6 万条特征级记录。若每条 1 KB，约 1.3 GB；看似不大，但查询和索引会放大。

控制手段：

- 只监控关键特征（10–20 个），不是全部列；
- 小时级窗口保留 7–30 天，天级聚合长期保留；
- 报告存对象存储，数据库只存摘要；
- 基线样本上限（Mini 2000）；高基数类别做 top-N + other；
- 采样高 QPS 请求，但**采样率要写进报告**，否则指标不可比。

### 45.8.2 计算成本

- PSI/KS 是 O(n log n)（排序）；大窗口先用直方图/分位数 sketch；
- 类别并集在高基数下会膨胀；生产用 count-min sketch 或 top-N；
- 分群监控会让窗口数乘以分群数；
- 不要在每个请求里同步算漂移；用批处理/流式聚合。

### 45.8.3 治理与隐私

- 预测日志可能含 PII：字段级脱敏、截断、加密、访问控制；
- 基线数据可能含训练集/评测集的敏感样本：继承数据集权限；
- 监控报告是审计证据：保留期、不可篡改、访问日志；
- 多租户：workspace/tenant 必须进入查询与存储键（第 37 章）；
- 数据许可证：基线数据的许可证要允许用于监控与再训练。

## 45.9 与真实平台的差距

Mini 是教学实现，以下能力**没有**实现：

- **流式聚合**：没有 Kafka/Flink、t-digest、count-min sketch；
- **标签 join**：没有延迟标签表、没有回填、没有选择偏差校正；
- **分群监控**：没有按地区/套餐/渠道切片；
- **告警投递**：没有邮件/Slack/PagerDuty、没有去重/静默/升级；
- **自动分箱**：分位数分箱是固定的，不随数据自适应；
- **精确统计**：卡方 p 值是近似，KS 是渐近；
- **分布式**：单 SQLite、单进程、无多副本；
- **权限**：单 token，无 RBAC/租户；
- **保留期与 GC**：删除 monitor 会级联删除窗口，生产需要软删除/归档；
- **与真实 MLflow 的自动集成**：需要自己写 job 把报告写进 run。

生产选型建议：

| 需求 | 建议 |
|---|---|
| 传统表格模型 + 已有数仓 | 数仓 SQL 算 PSI/KS + 调度 + Grafana |
| 需要开箱即用面板 | Evidently、WhyLabs、云厂商模型监控 |
| LLM 应用 | MLflow trace + judge/评估 + 外部指标系统 |
| 强实时 | 流式聚合 + TSDB + 告警平台 |
| 合规审计 | 不可变报告 + 数据目录 + 审批流 |

不要因为“MLflow 有 metric”就自己造一整套监控；也不要因为“监控平台有面板”就丢掉模型版本/数据 digest 的归因。两者接起来才是完整的。

## 小结

- MLflow 是身份与血缘平面，不是监控系统；监控要引用它的模型版本、数据集 digest 与 run/artifact。
- 监控分三个平面：服务、数据、结果；标签延迟决定了性能监控天然滞后。
- monitor = 派生基线 + 特征契约 + 阈值 + 归因信息；基线应绑定受管数据集。
- PSI、KS、卡方各回答不同问题；要同时看统计量、效应量、样本量与业务解释。
- 漂移不等于退化，退化不等于业务损失；动作要经过决策矩阵与人工确认。
- Mini 用两张表、一个服务、12 个测试实现最小闭环；示例 18 展示稳定与漂移窗口的完整报告。
- 生产要补流式聚合、标签 join、分群、告警投递、权限、保留期与成本控制。
- 先止损、再修数据、最后重训；监控的价值是让决策可复盘，而不是自动按按钮。

## 练习

1. 为你的模型选 10 个关键特征，写出特征契约（数值/类别、分箱、缺失策略），并说明为什么不是全部特征。
2. 用同一批数据分别做 5 桶、10 桶、20 桶 PSI，观察结论是否变化；给出你的分箱选择理由。
3. 设计一个“标签延迟 30 天”的性能监控：在标签到达前，你会用哪些代理指标？如何避免选择偏差？
4. 把示例 18 的 drift 报告写进真实 MLflow 的 run（metric + artifact + tag），并画出 UI 中如何按 model_version 过滤。
5. 为“新类别 enterprise 激增”写一份 runbook：谁通知、多久响应、先回滚还是先修数据、如何验证恢复。
6. 设计监控数据的保留期与成本模型：20 个模型、30 个特征、小时级窗口、90 天保留，估算存储与查询成本。
7. 论证“漂移但性能未下降”时不应自动重训的两个理由，并给出一个反例场景。
8. 如果要把 Mini 的监控接到 Prometheus，你会暴露哪些指标？哪些标签会导致基数爆炸？

---

返回：[教程首页](../README.md) · 上一章：[第 44 章 评测数据集](44-evaluation-datasets.md) · 下一章：[第 46 章 Agent 工程闭环](46-agent-ops.md) · 延伸：[第 47 章 GenAI 综合实战](47-genai-capstone.md)
