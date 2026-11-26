# 第 18 章 Autolog 与框架集成：便利的代价

## 学习目标

- 理解自动日志的实现方式：钩子、patch、回调、元数据推断。
- 知道自动日志会记录什么、不会记录什么。
- 识别性能、递归、版本兼容和隐私风险。
- 能为自己的框架设计一个可控的 autolog。

## 18.1 自动日志解决什么

手动记录：

```python
with mlflow.start_run():
    mlflow.log_params(model.get_params())
    model.fit(X, y)
    mlflow.log_metric("train_score", model.score(X, y))
    mlflow.sklearn.log_model(model, "model")
```

问题：

- 容易漏字段；
- 不同人写法不一致；
- 框架升级后 API 变化；
- 评测/模型/参数的口径不统一。

Autolog 的目标：

- 一行开启；
- 自动捕获 fit 参数、指标、模型；
- 尽量不改变训练行为；
- 失败不影响训练（只告警）。

真实 MLflow 支持 sklearn、xgboost、lightgbm、pytorch、tensorflow、spark、transformers 等；不同 flavor 的捕获范围差异很大。

## 18.2 实现机制

常见手段：

1. **Monkey patch**：替换 `fit`/`train` 方法；
2. **回调系统**：框架提供 callback（Keras、Lightning）；
3. **装饰器/上下文**：包装用户函数；
4. **事件/日志解析**：读取框架日志；
5. **元数据 introspection**：从模型对象提取参数/指标。

Mini 用第 2 种：

```python
# linear.py
_FIT_CALLBACKS = []

def register_fit_callback(callback):
    _FIT_CALLBACKS.append(callback)

# fit 末尾
for callback in list(_FIT_CALLBACKS):
    callback(self, X, y, {"training_rmse": ..., "training_r2": ...})
```

`autolog()` 注册回调：

- 读取 active run；
- 记录 `fit_intercept/alpha/n_features`；
- 记录训练指标；
- 打 tag `estimator_name`；
- 可选 `log_model`。

优点：

- 不修改训练逻辑；
- 容易测试；
- 失败可隔离。

缺点：

- 只能覆盖自己的模型；
- 需要框架作者/适配层配合；
- 回调里的异常可能影响训练（Mini 捕获并 warning）。

真实 MLflow 的 patch 还需要处理：

- 多线程/多进程；
- 递归调用（log_model 又触发 fit）；
- patch 冲突（其他库也 patch）；
- 版本差异；
- 关闭/重新开启；
- 性能开销。

## 18.3 什么时候触发

Mini 在 `fit` 完成后触发，因为：

- 训练指标需要模型已拟合；
- 参数已确定；
- 模型可序列化。

不在 fit 前触发的原因：

- 参数可能被 fit 修改；
- 失败时不应记录“成功模型”；
- 无法计算训练指标。

真实框架可能在：

- 每个 epoch 回调；
- 训练结束；
- 早停；
- checkpoint 保存时。

高频回调要限流/聚合，否则日志写入会拖慢训练。

## 18.4 自动记录什么

建议记录：

- 超参数（`get_params`）；
- 训练/验证指标；
- 模型结构摘要（层数、参数量）；
- 输入样例/签名（注意 PII）；
- 框架/版本；
- 随机种子；
- 训练时长；
- 模型 artifact。

不要自动记录：

- 原始训练数据（体积/隐私）；
- 完整日志（除非明确开启）；
- 敏感环境变量；
- 大 tensor/中间激活；
- 用户输入原文（LLM 场景需脱敏）。

Mini 的 `log_input_examples` 默认关闭，即使开启也只取前 10 条。

## 18.5 性能与可靠性

自动日志的隐藏成本：

- 每次 fit 都序列化模型；
- 大模型上传阻塞训练；
- 高频指标写库；
- 回调异常导致训练失败；
- 多进程重复上传。

原则：

- 日志失败不应改变训练结果（warning）；
- 大 artifact 异步/后台上传；
- 指标聚合后批量写；
- 提供开关和采样；
- 记录“autolog 被关闭/失败”的元数据，避免误以为完整。

Mini 捕获 `MiniMlflowError/ValueError/OSError` 并 warning；不会重试。

## 18.6 递归与重复

危险场景：

```python
def callback(model):
    client.log_model(model)   # 某些实现会在 log_model 中再次 fit/加载
```

真实 MLflow 用线程本地标志防止递归。Mini 的 `log_model` 不触发 fit，所以没有递归，但仍需注意：

- 同一个 fit 被多个回调重复记录；
- `autolog()` 多次调用导致重复注册（Mini 的 `register_fit_callback` 去重）；
- `disable_autolog` 只移除自己的回调（Mini 用 `unregister_fit_callback`）。

## 18.7 与手动日志的冲突

如果用户已经手动记录：

```python
mlflow.log_param("alpha", 0.1)
autolog()  # 又记录 alpha
```

结果是幂等（同值）或冲突（不同值）。应明确优先级：

- 手动记录优先；
- autolog 只补充缺失字段；
- 或 autolog 使用带前缀的 key。

Mini 没有实现“缺失才写”，因此同值幂等、不同值报错。生产应在 autolog 前检查已有参数，或提供 `log_models=False` 等开关。

## 18.8 框架集成矩阵

| 框架 | 捕获方式 | 主要风险 |
|---|---|---|
| scikit-learn | patch `fit` | 多线程、子估计器递归 |
| XGBoost/LightGBM | callback/train wrapper | 原生 API 版本差异 |
| PyTorch | Lightning callback / patch | 分布式、梯度累积 |
| Transformers | Trainer callback | 大模型、显存、PII |
| Spark ML | listener | 分布式日志聚合 |
| LangChain/LlamaIndex | callback | 高基数 trace、敏感 prompt |
| 自研 | 显式 hook | 需要维护契约 |

## 18.9 与真实 MLflow 的差距

- 真实 autolog 覆盖大量框架，处理递归/多线程/分布式；
- 真实实现会自动推断签名、依赖、输入样例；
- 真实实现有更细的开关（log_models、log_datasets、log_input_examples、silent）；
- Mini 只演示机制，不追求生态覆盖。

## 小结

- Autolog 是“钩子 + 元数据映射 + 失败隔离”，不是魔法。
- 自动记录会带来性能、隐私和版本兼容风险。
- 默认关闭敏感/大体积内容；日志失败不能影响训练。
- 手动记录与自动记录要有明确优先级。

## 练习

1. 为一个自研训练循环设计 autolog 接口，列出 hook 点。
2. 如何避免 autolog 在多进程训练中重复上传模型？
3. 设计一个“autolog 失败但不影响训练”的告警策略。

---

下一章：[第 19 章 Tracing](19-tracing.md)
