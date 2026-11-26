# 第 43 章 综合实战：把流失预警做成可运营的 AI 产品

## 学习目标

- 把前 42 章的对象、协议和治理动作串成一条可执行的交付链，而不是一堆孤立 API。
- 用一个合成数据集走完“数据契约 → 训练 → 评测门禁 → 注册审批 → 服务化 → 反馈”的闭环。
- 说清每个阶段的产物、验收标准和失败模式，知道哪一步该自动化、哪一步必须人工。
- 把 MiniMLflow 的实现映射到真实 MLflow 3.x 的命令与 API，形成可迁移的交付模板。
- 用成本、风险和 90 天计划把“能跑”推进到“可运营”。

> 本章的客户流失数据是**合成的**，所有指标只用于演示方法，不代表任何真实业务效果。真实项目必须替换数据、重做基线、重新校准阈值，并经过合规评审。

## 43.1 业务问题：不是“训一个模型”

场景：一家订阅制 SaaS 公司希望提前识别高风险客户，让客户成功团队在续费前介入。业务方最初的需求是“做一个流失预测模型，AUC 越高越好”。这句话不能直接变成工程任务，至少要拆成四个问题：

1. **预测谁、提前多久？** 续费前 30 天，对当前活跃客户打分；已经流失的客户不进入打分池。
2. **干预是什么？** 客户成功团队只能覆盖每天约 50 个客户；模型要输出排序，而不是只给 0/1。
3. **错误的代价是什么？** 漏掉一个高价值客户（假阴性）通常比误判一个低价值客户（假阳性）更贵；但过度打扰也会伤害体验。
4. **成功怎么衡量？** 离线看 recall@50、校准和切片；线上看干预转化率、流失率、客诉和成本。

这四问决定了后面的每一个技术选择：

- 用**排序/概率**而不是硬分类；
- 用**成本敏感阈值**而不是默认 0.5；
- 用**时间切分**而不是随机切分，避免未来信息泄漏；
- 用**切片指标**而不是只看总 AUC；
- 用**人工审批 + 灰度**而不是模型一上线就全量。

## 43.2 成功标准与约束

一个可验收的目标示例：

| 维度 | 目标 | 验收方式 |
|---|---|---|
| 离线排序 | recall@50 ≥ 基线 + 10 个百分点 | 固定测试集 + 时间切分 |
| 校准 | 预测概率与真实流失率的偏差在可接受范围 | 校准曲线/分桶 |
| 切片 | 新客户/老客户、不同套餐的指标不出现断崖 | 分组报告 |
| 延迟 | 批量打分 p95 < 5 分钟；在线 p95 < 100 ms | 压测 |
| 可解释 | 每个高分客户能看到主要驱动因素 | 特征贡献/规则 |
| 治理 | 版本、审批、回滚、审计可追溯 | Registry + 日志 |
| 成本 | 单次打分成本可估算，干预成本有上限 | 成本模型 |

约束同样要写清楚：

- 训练数据只能用到打分时点之前的信息（point-in-time correctness）；
- 客户 PII 不进入模型服务日志；
- 模型版本必须经过审批才能改 `champion`；
- 干预名单每天生成一次，在线服务只做实时补充打分；
- 没有标签的反馈要单独存放，不能直接当成负样本。

## 43.3 架构与关键决策

一个最小可运营架构：

```text
数据快照（Parquet/表分区）
        │
        ▼
训练/评测 Job ──> Run（params/metrics/artifacts/dataset input）
        │
        ▼
Model Version ──> 审批 ──> alias: challenger → champion
        │
        ├──> 批量打分（每日）──> 干预名单 ──> CRM
        │
        └──> 在线评分服务（实时补充）──> 网关 ──> 业务系统
                                      │
                                      └──> Trace / 指标 / 预测日志
                                                    │
                                                    ▼
                                          人工反馈 / 真实标签
                                                    │
                                                    ▼
                                          下一轮训练与评测
```

关键 ADR（架构决策记录）：

- **ADR-1：离线批量为主，在线为辅。** 流失干预是每天一次的业务动作，不需要每个请求都实时推理；在线服务用于页面/客服系统即时查询。
- **ADR-2：模型输出概率，不直接输出名单。** 排序和阈值是业务策略，应该可配置、可回滚，不焊死在模型里。
- **ADR-3：Registry 是发布唯一入口。** 任何环境都不允许从本地文件直接加载生产模型。
- **ADR-4：评测门禁在 CI 中，不在 Notebook 里。** 没有通过门禁的版本不能设置 `champion`。
- **ADR-5：预测日志与真实标签分离存储。** 线上预测先落日志，标签晚到后关联，避免“用未来标签训练过去”。
- **ADR-6：先做可解释的线性基线，再考虑复杂模型。** 可解释性、校准和上线速度优先于零点几个点的 AUC。

这些决策不是永久真理，但必须在项目开始时写下来；否则团队会在“要不要实时”“要不要上深度学习”“要不要全量”上反复争论。

## 43.4 数据契约：先固定快照，再谈模型

数据契约至少包含：

```text
dataset: churn_snapshot
version: 2026-11-20
grain: one row per customer per snapshot date
primary key: customer_id + snapshot_date
features:
  tenure_months: int, >= 0
  monthly_calls: int, >= 0
  support_tickets: int, >= 0
  plan_price: double, > 0
label:
  churned_30d: 0/1, observed 30 days after snapshot
split:
  train: snapshot_date <= 2026-09-30
  validation: 2026-10-01 .. 2026-10-31
  test: 2026-11-01 .. 2026-11-20
```

为什么强调 snapshot date：

- 同一客户在不同时间有不同的特征和标签；
- 随机切分会让“未来行为”泄漏到训练；
- 线上打分必须使用与训练一致的时点语义。

在 MLflow 中记录数据血缘：

```python
with mlflow.start_run(run_name="candidate-2026-11") as run:
    mlflow.log_input(
        dataset,
        context="training",
    )
```

Mini 的等价调用：

```python
client.log_input(
    run_id,
    dataset_name="churn_snapshot",
    dataset_digest="sha256:...",
    dataset_source="s3://warehouse/churn/2026-11-20",
    context="training",
)
```

注意：`log_input` 记录的是**指针和摘要**，不是数据本身。真正的 point-in-time 正确性需要数据平台/特征库保证；MLflow 只是把“这次训练用了哪份快照”写进审计链。

数据质量门禁应该前移到训练之前：

- 行数、主键唯一性、标签缺失率；
- 特征分布与上一快照的 PSI；
- 空值率、异常值、类型漂移；
- 训练/服务特征的一致性（同一套特征定义）。

如果这些检查失败，训练 Job 应该失败，而不是训练出一个“看起来能跑”的模型。

## 43.5 训练与实验：先基线，再候选

一个可复现的训练 Run 至少记录。下面的代码使用 **MiniMLflow 的兼容 API**（Mini 的 `LinearRegression` 支持 `alpha`，`mlflow.log_model` 是通用入口）；迁移到真实 MLflow 时，训练器换成 sklearn/LightGBM，打包改用 `mlflow.sklearn.log_model(...)` / `mlflow.pyfunc.log_model(...)` 这类 flavor 专用入口：

```python
with mlflow.start_run(run_name="candidate-2026-11") as run:
    mlflow.log_params({
        "model": "linear",
        "alpha": 0.05,
        "features": "tenure,calls,tickets,price",
        "split": "time-based-2026-11",
        "seed": 42,
    })
    model = LinearRegression(alpha=0.05).fit(X_train, y_train)
    signature = mlflow.models.infer_signature(X_test, model.predict(X_test))
    mlflow.log_metric("train_rmse", train_rmse)
    mlflow.log_metric("test_rmse", test_rmse)
    mlflow.log_dict(report, "evaluation/report.json")
    mlflow.log_model(
        model,
        name="churn-propensity",
        signature=signature,
        input_example=X_test[:2],
    )
```

真实 MLflow 3.x 的 `log_model(name=...)` 会创建 LoggedModel；Mini 也支持这条路径（第 40 章）。如果还在 2.x 风格，则用 `artifact_path="model"` + Registry。关键不是 API 拼写，而是：

- **参数不可变**：训练中途改 alpha 应该报错；
- **指标带前缀/切片**：`test_rmse`、`slice_new_rmse`、`slice_tenured_rmse`；
- **数据与代码版本**：dataset digest、git commit、依赖锁；
- **模型有签名和输入样例**：服务边界才能校验；
- **评测报告是 artifact**：不只是一个会消失的 Notebook 输出。

基线至少包括：

1. **常数基线**：用训练集均值/流失率预测所有人；
2. **规则基线**：例如“续费前 30 天 + 最近 3 次投诉”；
3. **简单模型**：线性/逻辑回归；
4. **复杂模型**：树模型/集成，只有在基线和数据契约稳定后再上。

没有基线的“提升”无法解释。第 22 章的方法论在这里落地：实验命名、标签、基线、随机性、评审。

## 43.6 评测与发布门禁：把“好”变成可执行条件

离线评测要回答五个问题：

1. **排序好不好？** recall@K、precision@K、NDCG；
2. **概率准不准？** 校准曲线、Brier score、分桶偏差；
3. **切片稳不稳？** 新/老客户、套餐、地区、渠道；
4. **阈值合不合理？** 在干预容量（每天 50 人）下的召回/精确率；
5. **不确定性多大？** bootstrap 置信区间，避免“涨了 0.1% 就上线”。

一个成本敏感的门禁示例：

```text
必须满足：
  recall@50 >= baseline_recall@50 + 0.10
  calibration_error <= 0.05
  slice_new_recall@50 >= 0.50
  slice_tenured_recall@50 >= 0.45
  p95_latency_ms <= 100
否则：
  阻止设置 champion，保留 challenger
```

门禁要写成代码，在 CI 中跑：

```bash
python scripts/evaluate_gate.py \
  --model models:/churn@challenger \
  --dataset s3://warehouse/churn/2026-11-20 \
  --baseline models:/churn@champion
```

门禁的输出不是“通过/失败”两个字，而是一份可审计的报告：

- 评测数据 digest；
- 模型版本与 artifact digest；
- 每个指标的数值与置信区间；
- 失败项与 owner；
- 审批人/时间/理由。

第 23 章讲发布治理，第 39 章讲 GenAI 的 CI 门禁；传统模型的逻辑相同：**没有证据的发布不是发布，是赌博。**

### 43.6.1 公平性与切片

“总体指标通过”不代表“对所有人都公平”。流失预警至少检查：

- **受保护/敏感分组**：地区、渠道、套餐、客户规模；如果敏感属性不能入模，也要在评测时分组；
- **样本量**：小切片的高方差会让“差异”看起来比实际大，报告要带置信区间；
- **误差方向**：某群体是系统性高估还是低估，是否导致干预资源分配不均；
- **代理变量**：邮编、设备、语言可能间接编码敏感属性；
- **干预公平**：模型分数相同的人是否得到相同的人工处理。

公平性不是“删掉敏感列”就完成；它需要数据、评测、流程和业务一起处理。MLflow 能记录切片指标、版本和审批，但不能替你定义“公平”。把定义写进模型卡和门禁，才有可执行性。

## 43.7 打包、注册与审批

训练完成后，模型要经过：

```text
Run artifact → Model Version → 审批 → alias(challenger) → 灰度 → alias(champion)
```

一个可回滚的注册流程（Mini 的 `client.log_model`；真实 MLflow 用 flavor 专用 `log_model` 拿到 `ModelInfo`，或先 `create_model_version`，再设置 tag/alias）：

```python
info = client.log_model(
    run_id,
    model,
    registered_model_name="churn-propensity",
    signature=signature,
    input_example=X_test[:2],
)
version = info.version

client.set_model_version_tag("churn-propensity", version, "approval", "approved")
client.set_model_version_tag("churn-propensity", version, "eval_report", "runs:/.../evaluation")
client.set_registered_model_alias("churn-propensity", "challenger", version)
```

审批至少记录：

- 谁批准、什么时候、依据哪份评测报告；
- 模型卡：用途、限制、训练数据、特征、指标、已知偏差；
- 回滚目标：上一个 `champion` 的版本与 digest；
- 变更说明：这次改了什么，为什么。

模型卡不要写成营销文案。一个最小模型卡：

```json
{
  "intended_use": "rank active subscription customers for retention outreach",
  "not_intended_for": "credit decisions, pricing, individual contract termination",
  "training_data": "churn_snapshot 2026-11-20, 30-day label window",
  "features": ["tenure_months", "monthly_calls", "support_tickets", "plan_price"],
  "metrics": {"test_rmse": 0.04, "recall_at_50": 0.71},
  "limitations": ["synthetic demo data", "no causal claim about intervention"],
  "owner": "risk-ml",
  "reviewed_by": "risk-owner",
  "reviewed_at": "2026-11-26"
}
```

把模型卡作为 artifact 写进 Run，并在 Registry 版本上打标签指向它。这样审计时不需要靠人记忆。

## 43.8 服务化与契约测试

服务化的第一步不是写 Deployment，而是固定协议：

```bash
python -m mini_mlflow models serve \
  -m models:/churn-propensity@champion \
  --host 127.0.0.1 --port 5001
```

请求：

```bash
curl -s http://127.0.0.1:5001/invocations \
  -H 'Content-Type: application/json' \
  -d '{"dataframe_split":{"columns":["tenure_months","monthly_calls","support_tickets","plan_price"],
       "data":[ [12,3,1,49], [3,8,2,29] ]}}'
```

响应：

```json
{"predictions": [0.21, 0.74]}
```

契约测试至少覆盖：

- 健康检查与版本端点；
- 四种输入形状；
- 缺列/类型错误/非法 JSON；
- 鉴权失败；
- 预测数量与输入行数一致；
- 延迟在预算内；
- 响应不含内部路径/堆栈。

`examples/16_capstone_churn.py` 会把训练、评测、注册、别名、在线打分、trace 和 feedback 串起来，作为本章的可运行骨架。它不是生产代码，但它证明了“每一层都能接上”。

生产部署还要补：

- 网关鉴权、TLS、限流；
- 模型从对象存储加载，digest 校验；
- readiness 在模型加载完成后才通过；
- 灰度/影子/回滚；
- 预测日志与真实标签关联；
- 监控与告警。

第 41 章给出 Compose 参考编排，第 42 章给出服务协议与 K8s 清单；本章只负责把它们串成业务闭环。

## 43.9 可观测性与反馈：模型上线只是开始

线上至少记录：

```text
request_id, customer_id_hash, model_version, artifact_digest,
score, threshold, decision, latency_ms, feature_snapshot_date
```

不要记录明文 PII；用哈希/令牌化，并设置保留期。

三类监控：

1. **服务**：QPS、错误率、p95、超时、副本数；
2. **数据**：特征缺失率、分布漂移、打分分布、阈值命中率；
3. **业务**：干预名单转化率、流失率、客诉、成本/挽回客户。

反馈闭环：

```text
预测日志 ──> 30 天后真实流失标签 ──> 关联 ──> 线上指标
   │                                              │
   └── 人工反馈（客户成功标记“误判/有效”） ──> 评测集/规则调整
```

注意三个偏差：

- **选择偏差**：只有被干预的客户才有反馈，不能直接和未干预客户比较；
- **标签延迟**：30 天标签意味着监控天然滞后，需要短期代理指标；
- **反馈污染**：客户成功看到模型分数后可能改变行为（干预本身影响结果）。

如果要回答“模型是否真的提升了留存”，需要实验设计（A/B、 uplift modeling、因果推断），而不是只看相关性。这是数据科学问题，MLflow 只负责记录版本、预测和反馈，不替你证明因果。

把上面三类监控变成可执行的基线、窗口、阈值和告警，见第 45 章；示例 18 用同一套流失特征演示了稳定窗口与漂移窗口的 PSI/KS 报告。

## 43.10 自动化与事件：让发布可审计

一个事件驱动的发布链：

```text
Registry: model_version_alias.created (champion)
        │
        ▼
CI/CD: 校验门禁报告 → 生成部署清单（version + digest）
        │
        ▼
部署系统: 滚动更新 → 冒烟 → 切流
        │
        ▼
审计日志: 谁、何时、从哪个版本到哪个版本、依据哪份报告
```

MLflow 的 webhook/outbox（第 38 章）可以作为事件源，但不要把“收到事件”当成“已批准”。部署系统必须：

- 重新解析 alias，拿到具体版本；
- 校验审批标签/门禁报告；
- 固化版本与 digest；
- 记录部署 ID，支持回滚；
- 失败时告警，而不是静默重试。

事件的价值是**解耦与审计**，不是绕过审批。

## 43.11 运维与故障演练

上线前至少演练：

| 故障 | 预期行为 | 验证 |
|---|---|---|
| 模型 artifact 丢失 | readiness 失败，旧副本继续服务 | 删除测试 bucket 对象 |
| 对象存储超时 | 启动失败/重试，不接流量 | 注入网络延迟 |
| 数据库连接耗尽 | 5xx 上升，告警触发 | 压测/限制连接 |
| 模型版本错误 | 门禁拦截/回滚 | 发布一个故意低分版本 |
| 预测分布漂移 | 告警，触发复查 | 构造偏移输入 |
| 标签管道延迟 | 监控滞后，不误判模型退化 | 延迟标签导入 |
| 网关限流 | 429 + Retry-After，客户端退避 | 压测超过配额 |

回滚检查：

```text
[ ] 上一个 champion 的版本与 digest 可查
[ ] 旧模型在当前运行时能加载
[ ] 特征/预处理兼容
[ ] 缓存/连接池已清理
[ ] 流量切换完成
[ ] 业务指标恢复
[ ] 事故记录与复盘完成
```

## 43.12 成本与 ROI

成本要拆成：

```text
训练成本 = 数据扫描 + 计算 + 调参 + 人工
服务成本 = 副本数 × 单价 × 时间 + 网关 + 存储 + 监控
干预成本 = 客户成功人力 × 触达量 + 优惠/权益
事故成本 = 误判/漏判 × 业务损失 + 客诉 + 合规
```

收益要谨慎：

- 挽回客户数 × 客单价 × 毛利率；
- 减少的无效干预人力；
- 更快的迭代周期；
- 审计/合规准备时间下降。

示例（假设，仅演示）：

```text
每天打分 10 万客户，干预 50 人；
干预转化率提升 2 个百分点，平均客单价 $200/月，毛利率 70%；
每月增量毛利 ≈ 50 × 30 × 2% × 200 × 70% ≈ $4,200
服务+人力成本 ≈ $3,000/月
净收益 ≈ $1,200/月
```

这个数字很小，说明“模型本身”不是价值来源；真正决定 ROI 的是干预策略、覆盖面、转化率和成本。不要用“AUC 提升”直接推导业务收益。

## 43.13 从 Mini 到真实 MLflow 的替换表

| 环节 | Mini 示例 | 真实 MLflow 3.x |
|---|---|---|
| 客户端 | `MiniClient` | `mlflow` / `MlflowClient` |
| 实验 | `create_experiment` | `mlflow.set_experiment` |
| 数据血缘 | `log_input` | `mlflow.log_input(dataset, context=...)` |
| 评测数据集 | `client.create_dataset/merge_dataset_records` | `mlflow.genai.datasets.create_dataset/merge_records`（系统讲解见第 44 章） |
| 训练记录 | `start_run/log_metric` | `mlflow.start_run/log_metric` |
| 模型打包 | `log_model`（通用） | `mlflow.sklearn.log_model` / `mlflow.pyfunc.log_model` 等 flavor 入口 |
| 模型中心 | LoggedModel（第 40 章） | `models:/<model_id>` |
| 注册 | `create_model_version` | `mlflow.register_model` |
| 别名 | `set_registered_model_alias` | `MlflowClient.set_registered_model_alias` |
| 评分服务 | `mini_mlflow models serve` | `mlflow models serve` |
| 服务协议 | `/invocations` 子集 | FastAPI scoring server |
| Trace | `tracing.start_trace` | `mlflow.trace` / `start_span` |
| 反馈 | `log_feedback` | `mlflow.log_feedback` / assessment API |
| 事件 | `webhooks.py` | MLflow webhooks / 外部事件总线 |
| 部署 | 本地进程 | Compose/K8s/KServe/云推理 |

替换时最容易踩的坑：

- 真实 MLflow 的 `log_model` 参数与 flavor 行为随版本变化；
- `models:/name@alias` 的解析在客户端/服务端可能不同；
- 真实 Registry 的权限、workspace、tags 语义更复杂；
- Trace 的字段/查询方言与 Mini 不完全一致；
- 真实服务的依赖管理比 Mini 重得多。

替换策略：先替换“记录/注册”，再替换“服务”，最后替换“事件/治理”；每一步都保留回滚路径。

## 43.14 复盘与 90 天计划

### 第 1–2 周：问题与数据

- 写清业务问题、干预容量、成本矩阵；
- 固定数据快照与时点语义；
- 建基线（常数/规则）；
- 定义成功指标与门禁。

### 第 3–4 周：最小闭环

- 训练 + 评测 + 注册；
- 模型卡与审批；
- staging 评分服务；
- 契约测试与压测。

### 第 5–8 周：上线与观测

- 小流量灰度；
- 预测日志/真实标签关联；
- 监控与告警；
- 回滚演练。

### 第 9–12 周：评估与扩展

- 对比基线 ROI；
- 切片复盘与公平性检查；
- 决定扩展/停止；
- 写 runbook 与下一阶段计划。

复盘问题：

1. 模型失败时，团队多快能发现、定位、回滚？
2. 评测门禁是否真的阻止过一次坏发布？
3. 数据契约是否被遵守，还是一次性脚本绕过？
4. 业务方是否真的使用排序/名单，而不是只看报表？
5. 成本是否可解释，是否随着流量线性增长？
6. 如果明天换掉 MLflow，哪些资产能带走？

## 小结

- 一个可运营的模型产品，核心不是模型结构，而是数据契约、评测门禁、发布治理和反馈闭环。
- 排序/概率、成本敏感阈值、时间切分和切片指标，比“总 AUC”更接近业务。
- Registry 是发布唯一入口；alias 是可变指针；部署清单必须固化版本与 digest。
- 服务协议、契约测试、readiness、灰度和回滚，是上线的最低工程要求。
- 预测日志、真实标签和人工反馈要能关联；因果问题要用实验设计，不要用相关性冒充。
- Mini 的端到端示例证明闭环可运行；真实 MLflow 替换的是组件，不是原则。

## 练习

1. 用 `examples/16_capstone_churn.py` 跑一遍闭环，记录每一步的产物与失败点。
2. 把示例的随机切分改成时间切分，观察指标变化，并解释原因。
3. 设计一个 recall@50 的基线和一个成本敏感阈值，写出 CI 门禁伪代码。
4. 给模型卡补充“已知偏差”和“不适用场景”，并找一位同事做一次评审。
5. 画一张你所在团队的“数据 → 训练 → 注册 → 服务 → 反馈”图，标出没有 owner 的环节。
6. 估算你所在业务的单次打分成本、干预成本和事故成本，做一次敏感性分析。
7. 设计一次“模型版本错误”的故障演练，包含发现、定位、回滚、复盘四个阶段。

---

下一章：[第 44 章 评测数据集：从 `log_input` 到可运营的 Golden Set](44-evaluation-datasets.md)
