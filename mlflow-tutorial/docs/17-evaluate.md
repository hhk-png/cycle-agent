# 第 17 章 Evaluate：回归、分类与 LLM 评测

## 学习目标

- 从定义出发写出回归/分类指标，而不是只调用库。
- 理解宏平均/加权平均、混淆矩阵和阈值对指标的影响。
- 设计一个可复现的评测 run：数据、版本、切片、报告、置信区间。
- 理解 LLM 评测为什么不能只看一个“准确率”。

## 17.1 评测的第一原则：先定义问题

指标不是中立的。选择 AUC 还是 F1，会改变模型选择。评测前必须写清：

- 业务目标（减少欺诈损失、提高点击率、降低人工审核量）；
- 正负样本定义；
- 预测时点与可用特征（避免未来信息泄漏）；
- 阈值与代价（FP/FN 成本不同）；
- 评测集来源、时间窗口、切片；
- 可接受的最小提升与置信度。

没有这些，0.91 的 AUC 只是一个数字。

## 17.2 回归指标

给定预测 `p_i` 与目标 `y_i`：

```text
MSE  = mean((p_i - y_i)^2)
RMSE = sqrt(MSE)
MAE  = mean(|p_i - y_i|)
R²   = 1 - SSE / SST,  SST = sum((y_i - mean(y))^2)
MAPE = mean(|(p_i - y_i) / y_i|) * 100   （忽略 y_i=0）
```

注意：

- RMSE 对大误差更敏感；
- MAE 更稳健但不可导点；
- R² 可以为负（模型比均值还差）；
- MAPE 在接近 0 的目标上爆炸，不应作为唯一指标；
- 目标有异常值时，报告截断/分位数指标。

Mini 的 `regression_metrics` 返回 mse/rmse/mae/r2/max_error/mean_prediction/mean_target，目标为 0 时跳过 MAPE。

## 17.3 分类指标

混淆矩阵（二分类）：

```text
               预测正   预测负
实际正          TP       FN
实际负          FP       TN
```

```text
precision = TP / (TP + FP)
recall    = TP / (TP + FN)
f1        = 2PR / (P + R)
accuracy  = (TP + TN) / N
```

多分类：

- **macro**：每个类别指标等权平均，少数类影响被放大；
- **weighted**：按 support 加权，接近整体表现；
- **micro**：全局统计，和 accuracy 在多分类单标签下等价。

Mini 的 `classification_metrics` 返回：

- accuracy；
- macro/weighted precision/recall/f1；
- 每类 precision/recall/f1/support；
- 混淆矩阵（行=实际，列=预测，符合 sklearn/MLflow 惯例）。

### 阈值

分类器通常输出概率。阈值决定 TP/FP。报告必须在固定阈值下，或同时报告：

- AUC（阈值无关，但只衡量排序）；
- PR 曲线（类别不平衡时更有用）；
- 固定业务阈值下的 precision/recall；
- 校准曲线（概率是否可信）。

### AUC 与 LogLoss

Mini 实现二分类 ROC AUC（含 tie 平均秩）和 log loss：

```python
roc_auc_score(y_true, y_score, positive_label=1)
log_loss(y_true, probabilities, positive_label=1)
```

注意：

- AUC 高不代表概率校准好；
- log loss 对过度自信的错误惩罚极大；
- 多分类 AUC 需要 one-vs-rest/one-vs-one，Mini 不实现。

## 17.4 评测的数据泄漏

常见泄漏：

- 预处理在划分前做（标准化、缺失值填充、目标编码）；
- 时间序列随机划分，未来信息进入训练；
- 同一用户/实体同时出现在训练和测试；
- 用测试集反复调参；
- 特征包含标签的代理（如“已审核”状态）。

防线：

- 先划分，再 fit 预处理器；
- 时间切分/分组切分；
- 固定 holdout，调参只用验证集；
- 记录数据 digest 与切分逻辑；
- 对关键切片单独报告。

MLflow 记录的是“你声称用了什么”，不能自动发现泄漏。数据/特征平台与代码评审才是主防线。

## 17.5 评测的统计不确定性

一个 1000 样本的测试集，accuracy 0.90 的 95% 置信区间约为：

```text
1.96 * sqrt(0.9 * 0.1 / 1000) ≈ ±0.019
```

这意味着 0.90 与 0.915 的差异可能不显著。建议：

- 报告置信区间或 bootstrap；
- 配对比较（同一测试集）用 McNemar 检验；
- 多 seed 训练，报告均值/方差；
- 不要用一位小数以下的“提升”做上线决策。

Mini 不做显著性检验；生产评测应补上。

## 17.6 切片评估

整体指标会掩盖少数群体退化。必须按以下维度切片：

- 地区/语言/设备；
- 用户群体/新老用户；
- 类别/长尾；
- 时间（节假日、数据漂移）；
- 置信度区间；
- 业务价值分层。

实现方式：

```python
for slice_name, subset in slices.items():
    result = evaluate_predictions(preds[subset], targets[subset], task="classification")
    client.log_metrics(run_id, {f"slice_{slice_name}_{k}": v for k, v in result["metrics"].items()})
```

Mini 没有内置切片 API，但可以复用指标函数并记录带前缀的指标。

## 17.7 LLM 评测

传统指标不足以评价生成式输出。一个实用的分层：

### 确定性指标

- exact match；
- token F1；
- ROUGE-L（最长公共子序列）；
- 是否包含参考答案；
- JSON 可解析率；
- 引用/链接有效性；
- 延迟、token 成本。

Mini 的 `llm_metrics` 实现前五项中的 exact/token F1/ROUGE-L/contains/JSON validity。

### 模型评分（LLM-as-judge）

- 用另一个模型按 rubric 打分；
- 需要校准、去偏、重复采样；
- 记录 judge 模型版本、prompt、温度；
- 人工抽检一致性（Cohen's kappa 等）。

### 人工评测

- 盲评、随机顺序；
- 明确 rubric；
- 多人标注与一致性；
- 记录标注者、时间、争议解决。

### 线上指标

- 用户反馈（点赞/采纳/修正）；
- 任务完成率；
- 人工接管率；
- 成本/延迟；
- 安全事件。

### 评测集设计

- 覆盖真实分布与长尾；
- 包含对抗/越狱/隐私案例；
- 版本化数据集；
- 防止训练集污染；
- 定期更新，避免过拟合。

## 17.8 把评测写入 Run

Mini 的 `evaluate_model`：

```python
result = evaluate_model(
    model,
    X_test,
    y_test,
    task="classification",
    labels=["neg", "pos"],
    client=client,
    run_id=run_id,
    prefix="test",
    artifacts_path="evaluation",
)
```

行为：

- 调 `model.predict(data)`；
- 计算指标；
- 以 `test_<metric>` 写 run；
- 写 `evaluation/report.json`；
- 分类任务额外写 `confusion_matrix.json`；
- 返回 `EvaluationResult`。

注意：

- 评测数据本身不应只存在内存；应记录 digest/source；
- 阈值、标签顺序、评测脚本版本要记录；
- 如果 predictor 有随机性，固定 seed/温度；
- 评测应在独立 run 或明确标签下，避免与训练指标混淆。

### 17.8.1 真实 MLflow 的两条评测路径

传统模型走 `mlflow.models.evaluate`（顶层 `mlflow.evaluate` 是兼容别名，新代码建议明确命名空间）：

```python
import mlflow

result = mlflow.models.evaluate(
    model_uri="models:/m-abc123",       # 或 runs:/<run_id>/model
    data=eval_df,                        # 含特征列 + 目标列
    targets="label",
    model_type="classifier",
    evaluators=["default"],
    evaluator_config={"default": {"log_model_explainability": True}},
    validation_thresholds={"accuracy_score": 0.85},
)
print(result.metrics)
```

`model_type`、`evaluators`、`evaluator_config` 与阈值键随版本/任务变化；先用 `help(mlflow.models.evaluate)` 确认。

GenAI 走 `mlflow.genai.evaluate`，数据是 `inputs`/`expectations`，评分器是 scorer：

```python
import mlflow
from mlflow.genai.scorers import Correctness, RelevanceToQuery

result = mlflow.genai.evaluate(
    data=[
        {"inputs": {"question": "refund window?"},
         "expectations": {"expected_response": "30 days"}},
    ],
    predict_fn=my_app,
    scorers=[Correctness(), RelevanceToQuery()],
)
print(result.metrics)
```

两条路径的共同点：结果写入 run/trace，报告可下载；区别是传统评测围绕“特征 + 标签 + 指标”，GenAI 评测围绕“输入 + 期望 + scorer/judge”。第 34、39、44 章分别讲 LoggedModel、真实 GenAI 实战与评测数据集。

## 17.9 常见错误

| 错误 | 后果 | 修复 |
|---|---|---|
| 用训练集评估 | 指标虚高 | 固定 holdout |
| 先标准化再划分 | 泄漏 | Pipeline + 先划分 |
| 只报 accuracy | 不平衡数据误导 | PR/F1/混淆矩阵 |
| 不固定阈值 | 无法复现 | 记录阈值 |
| 不记录数据版本 | 无法解释漂移 | digest/source |
| LLM 只看 BLEU/ROUGE | 与人类判断脱节 | 多维 + 人工 |
| 评测集被调参污染 | 过拟合测试集 | 独立测试/轮换 |

## 17.10 与真实 MLflow 的差距

- 真实 MLflow Evaluate 支持内置评估器、模型类型自动识别、可视化、数据集输入、LLM 评估；17.8.1 给出了两条真实调用路径；
- 真实 GenAI 评测支持 scorer、评测数据集、judge 模型配置；会话级 scorer、模拟用户与配对统计比较见第 46 章；RAG 的检索/引用/grounding 指标与门禁见第 47 章与附录 M；
- Mini 只提供基础指标与 JSON 报告，不实现可视化/统计检验/模型评分（AgentOps 的配对 bootstrap 是第 46 章的教学子集，RAG 指标是第 47 章的教学子集）。

## 小结

- 指标必须绑定业务定义、阈值、数据版本和切片。
- 分类不要只看 accuracy；回归注意 MAPE/R² 的陷阱。
- 评测要报告不确定性，避免把噪声当提升。
- LLM 评测是确定性指标 + 模型评分 + 人工 + 线上反馈的组合。

## 练习

1. 为一个不平衡欺诈数据集选择 5 个指标，并解释为什么。
2. 设计一个带切片的评测报告 schema。
3. 如何检测“评测集泄漏进训练集”？

---

下一章：[第 18 章 Autolog](18-autolog.md)
