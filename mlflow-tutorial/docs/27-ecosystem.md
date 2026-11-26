# 第 27 章 生态集成：Spark、Ray、PyTorch、Hugging Face 与 LLM 框架

## 学习目标

- 理解 MLflow 与训练/数据处理框架的集成层次：日志、模型、追踪、部署。
- 掌握 sklearn/XGBoost/PyTorch/Transformers 的常见集成模式。
- 了解 Spark/Ray 的分布式日志与模型分发问题。
- 设计 LLM 框架的追踪与评测接入，而不是只接 autolog。

## 27.1 集成层次

```text
Level 0：手动 client API
Level 1：autolog（参数/指标/模型）
Level 2：flavor 打包/加载
Level 3：分布式/服务运行时集成
Level 4：治理/审批/部署流水线
```

很多团队停在 Level 1，导致：

- 模型能记录但不能稳定加载；
- 指标有但数据版本缺失；
- trace 有但不能评测；
- 模型注册了但没有发布策略。

集成不是“调一个 autolog”，而是把生命周期接上。

## 27.2 scikit-learn

```python
import mlflow
import mlflow.sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from mlflow.models import infer_signature

mlflow.set_experiment("sklearn-demo")
mlflow.sklearn.autolog()

with mlflow.start_run():
    X_train, X_test, y_train, y_test = train_test_split(...)
    model = RandomForestClassifier(n_estimators=200, random_state=42)
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    mlflow.sklearn.log_model(
        model,
        artifact_path="model",
        signature=infer_signature(X_test, pred),
        input_example=X_test.head(3),
        registered_model_name="sklearn-demo",
    )
```

要点：

- 先划分再预处理，避免泄漏；
- `autolog` 记录 params/metrics，但不会替你固定数据版本；
- 签名需要输入/输出样例；
- `registered_model_name` 会创建 Registry 版本；
- sklearn 版本要记录，否则加载可能失败。

## 27.3 XGBoost / LightGBM

```python
import mlflow.xgboost

mlflow.xgboost.autolog()
with mlflow.start_run():
    model = xgb.train(params, dtrain, num_boost_round=...)
    mlflow.xgboost.log_model(model, "model")
```

注意：

- 原生 `xgb.train` 与 sklearn wrapper 的 autolog 行为可能不同；
- 早停/验证集指标要显式记录；
- 模型格式（json/ubj）与版本兼容；
- GPU/CPU 配置要记录。

## 27.4 PyTorch

```python
import mlflow.pytorch

with mlflow.start_run():
    for epoch in range(epochs):
        ...
        mlflow.log_metric("train_loss", loss.item(), step=epoch)
    mlflow.pytorch.log_model(model, "model")
```

要点：

- 设备（CPU/GPU）与权重格式；
- 输入签名对 tensor 来说更复杂；
- 大模型/分片需要对象存储和自定义加载；
- `state_dict` 与完整模型对象的取舍；
- 分布式训练要合并/只记录 rank 0 的指标。

## 27.5 Hugging Face Transformers

```python
import mlflow.transformers

mlflow.transformers.autolog()
with mlflow.start_run():
    trainer.train()
    mlflow.transformers.log_model(
        transformers_model={"model": model, "tokenizer": tokenizer},
        artifact_path="model",
        task="text-classification",
    )
```

注意：

- 模型很大，artifact 上传/下载是瓶颈；
- tokenizer/pipeline 必须一起打包；
- 依赖（transformers/torch/tokenizers）版本敏感；
- GPU 推理的 batch/量化配置要记录；
- 许可证/模型卡要随制品保存。

## 27.6 Spark

```python
import mlflow.pyfunc

model = mlflow.pyfunc.load_model("models:/churn@champion")
predict_udf = mlflow.pyfunc.spark_udf(spark, model_uri="models:/churn@champion")
df.withColumn("prediction", predict_udf(*feature_cols)).write...
```

要点：

- UDF 需要把模型分发到 executor；
- 大模型广播成本高；
- 输入列顺序/类型必须与签名一致；
- 批量写入要幂等；
- 记录分区/数据版本。

MLflow 的 `spark_udf` 是常见集成点，但生产还要考虑版本/缓存/资源。

## 27.7 Ray

Ray 的集成模式：

```python
import ray
from mini_mlflow import MiniClient  # 教学示例

@ray.remote
def train(config, tracking_uri, run_id):
    client = MiniClient(tracking_uri)
    client.start_run(run_id=run_id)
    ...
```

要点：

- 每个 worker 独立客户端/连接；
- 只让 rank 0 写共享指标，或给每个 worker 独立 run；
- 模型/数据分发；
- 失败重试与孤儿 run；
- Ray Tune 的 trial 与 run 映射。

不要假设 fluent 的 active run 会跨 Ray worker 传播；显式传 `run_id`/`tracking_uri`。

## 27.8 LLM 框架

常见：

- OpenAI/Anthropic SDK；
- LangChain；
- LlamaIndex；
- DSPy；
- 自研 Agent/工具调用。

接入点：

1. **Tracing**：记录 prompt/completion/token/tool/retriever；
2. **Evaluation**：数据集 + scorer + judge；
3. **Prompt 版本**：prompt registry/模板版本；
4. **Gateway**：统一 provider、密钥、限流；
5. **Feedback**：用户反馈写回 trace/评测集。

示意：

```python
import mlflow
from mlflow.entities import Document

@mlflow.trace(span_type="RETRIEVER")
def retrieve(query: str):
    return vector_store.search(query)

@mlflow.trace(span_type="LLM")
def generate(prompt: str):
    return llm.invoke(prompt)
```

真实 API 名称依版本；核心是 span 类型、输入输出和父子关系。

### 自动埋点的风险

- 记录完整 prompt，可能含 PII/密钥；
- 高基数 trace 拖垮存储；
- 递归/重复埋点；
- 框架版本不兼容；
- 成本/延迟开销。

生产策略：

- 采样 + 脱敏 + 截断；
- 只对关键路径全量；
- 异步导出；
- 设置租户/保留策略；
- 定期审计 trace 内容。

## 27.9 集成测试矩阵

| 集成 | 单元 | 契约 | 端到端 |
|---|---|---|---|
| sklearn | 模型 save/load | 签名/输入 | 训练→注册→服务 |
| XGBoost | 同上 | 同上 | 同上 |
| PyTorch | 设备/格式 | tensor schema | GPU 推理 |
| Spark | UDF 输入 | 分区/列 | 批处理 |
| Ray | 分布式日志 | run 映射 | 失败恢复 |
| LLM | trace | token/成本 | 评测闭环 |

不要只测“能 import”。

## 27.10 与真实 MLflow 的差距

- 真实 MLflow 有大量官方 flavor/autolog 集成；
- 版本兼容矩阵复杂；
- Mini 只支持 linear，其他集成只能讲模式；
- 生产需要固定版本、契约测试和制品准入。

## 小结

- 集成有层次：日志、打包、运行时、治理；不要停在 autolog。
- sklearn/XGBoost/PyTorch/HF 的核心是环境、签名、制品大小。
- Spark/Ray 要处理分布式 run/模型分发，不要依赖进程内 active run。
- LLM 接入的重点是 trace 的语义、隐私、采样和评测闭环。

## 练习

1. 为一个 PyTorch 训练脚本设计 run/step 指标结构。
2. Ray Tune 的每个 trial 应该映射为 run 还是 nested run？论证。
3. 设计一个 LLM trace 的脱敏/采样策略。

---

下一章：[第 28 章 GenAI 时代的 MLflow](28-genai-mlflow.md)
