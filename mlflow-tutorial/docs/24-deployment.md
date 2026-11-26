# 第 24 章 部署：批量、在线、流式与 Kubernetes

## 学习目标

- 区分批量推理、在线推理、流式推理的架构与评测重点。
- 理解模型 URI 在部署流水线中的解析与固化。
- 设计一个最小在线评分服务，并知道生产网关需要什么。
- 识别延迟、漂移、冷启动、回滚和反馈闭环问题。

## 24.1 三种部署形态

| 形态 | 输入 | 延迟 | 典型系统 | 关键指标 |
|---|---|---|---|---|
| 批量 | 定时快照 | 分钟/小时 | Spark、Ray、批任务 | 吞吐、成本、数据完整性 |
| 在线 | 单请求/小批 | 毫秒/秒 | KServe、SageMaker、Triton | p95/p99、错误率、可用性 |
| 流式 | 事件流 | 秒级 | Kafka/Flink | 端到端延迟、背压、重复消费 |

同一个模型可能需要多种形态；它们共享模型制品，但部署/监控不同。

## 24.2 从 Registry 到服务

推荐：

```text
models:/fraud-detector@champion
```

部署系统：

1. 解析 alias → 具体版本；
2. 记录解析结果（部署清单）；
3. 拉取 artifact；
4. 校验 digest/签名；
5. 构建/启动推理镜像；
6. 健康检查；
7. 切流；
8. 回写部署记录。

不要在服务启动时每次动态解析 `latest`：

- 启动结果不可复现；
- 回滚困难；
- 缓存与指标无法归因。

生产部署应把**解析后的版本号 + artifact digest**写进部署清单。部署系统可以用第 38 章的 `model_version_alias.created` 事件触发滚动更新，但仍要在部署时重新解析 alias 并固化版本，不能把“收到事件”当成“版本已批准”。

## 24.3 批处理

```python
model = load_model("models:/fraud-detector@champion")
for partition in dataset.partitions():
    predictions = model.predict(partition.to_records())
    write(predictions)
```

要点：

- 记录输入数据版本/分区；
- 幂等写入（重跑不重复）；
- 大模型按 executor 缓存；
- 失败重试与断点；
- 输出表带 model_version/deploy_id；
- 监控分布/空值/延迟。

真实 MLflow 支持 Spark UDF，但分布式加载/广播模型需要谨慎（大小/序列化）。

## 24.4 在线推理

Mini 的评分服务：

```bash
python -m mini_mlflow models serve \
  -m models:/house-price-linear@champion \
  --host 127.0.0.1 --port 5001
```

请求：

```bash
curl -s http://127.0.0.1:5001/invocations \
  -H 'Content-Type: application/json' \
  -d '{"dataframe_split":{"columns":["floor"],"data":[[7.0]]}}'
```

响应：

```json
{"predictions": [5.2]}
```

Mini 支持：

- `dataframe_split`；
- `dataframe_records`；
- `instances`（list[dict] 或带签名的 list[list]）；
- 可选 Bearer token；
- `/ping`、`/health`。

不支持：

- 动态 batching；
- GPU；
- 并发限流；
- 模型热更新；
- 多模型路由；
- Prometheus 指标；
- gRPC。

真实 MLflow 的 `mlflow models serve` 在 3.x 使用 FastAPI + uvicorn：

```bash
mlflow models serve -m models:/churn@champion -p 5001 -w 2 --env-manager uv
```

要点：

- 端点仍是 `/invocations`、`/ping`、`/health`、`/version`；
- worker 数用 `-w`（容器镜像里可能由 `MLFLOW_MODELS_WORKERS` 控制）；
- 旧的 Flask/gunicorn/MLServer 参数不再适用；
- `models:/name@alias` 是部署推荐的解析方式；
- `mlflow models build-docker` 可生成可运行镜像，但生产仍要加鉴权、资源限制、探针和网络策略。

评分端点、输入形状、签名、容器与 Kubernetes 编排的完整拆解见第 42 章；本章只保留部署形态与治理的全局视角。

## 24.5 生产推理网关

需要：

- **无状态**：模型从 artifact 加载，实例可水平扩展；
- **动态批处理**：提高 GPU 利用率；
- **超时/重试/熔断**；
- **限流/配额**；
- **多版本/多模型路由**（KServe InferenceService、Seldon、Triton）；
- **健康检查/就绪探针**；
- **自动扩缩容**（HPA/KEDA）；
- **安全**：mTLS、鉴权、网络策略；
- **可观测性**：延迟、吞吐、错误、GPU 利用率、输入分布；
- **漂移检测**；
- **灰度/影子**。

MLflow 的 `models serve` 是开发工具，不应直接承担生产流量。

## 24.6 容器化

最小 Dockerfile：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY model /app/model
EXPOSE 5001
CMD ["python", "-m", "mini_mlflow", "models", "serve", "-m", "/app/model", "--host", "0.0.0.0", "--port", "5001"]
```

生产要：

- 固定基础镜像 digest；
- 非 root 用户；
- 只读文件系统；
- 健康检查；
- 资源 requests/limits；
- 镜像扫描/SBOM；
- 多阶段构建；
- 模型与镜像分离（避免每次模型更新重建镜像）。

## 24.7 冷启动与缓存

模型加载可能耗时：

- 大权重下载；
- 反序列化；
- GPU 初始化；
- 依赖导入。

优化：

- 预热实例；
- 本地缓存（按 digest）；
- 模型分层加载；
- 保持最小副本；
- 就绪探针在加载完成后才通过；
- 避免每次请求重新加载。

## 24.8 漂移与反馈

监控三类：

1. **数据漂移**：输入分布变化（PSI/KL/KS）。
2. **概念漂移**：输入→输出关系变化（需标签）。
3. **服务指标**：延迟、错误、饱和度、成本。

反馈闭环：

- 记录预测与 model_version；
- 收集真实标签（可能延迟）；
- 计算线上指标；
- 触发再训练/回滚；
- 保留审计。

注意：线上标签可能有偏（只有被人工处理的样本才有标签）。

本节只给出分类。完整的基线设计、PSI/KS/卡方实现、阈值、告警分级与再训练护栏见第 45 章；Mini 的 `monitoring` 服务与示例 18 给出可运行版本。

## 24.9 回滚与故障演练

回滚检查：

- alias 指回 previous；
- 服务加载旧 artifact 成功；
- 特征/预处理兼容；
- 缓存清理；
- 流量切换完成；
- 指标恢复。

演练：

- 在 staging 模拟 artifact 丢失；
- 模拟数据库/对象存储不可用；
- 测量 MTTR；
- 记录 runbook。

## 24.10 与真实 MLflow 的差距

- 真实 MLflow 提供 `models serve`、`build-docker`、批处理工具、Spark UDF；
- 生产部署依赖 KServe/Seldon/云服务；
- Mini 的评分服务只演示请求/响应契约；
- 真实系统有更完整的监控、批处理、GPU、模型路由。

## 小结

- 批量/在线/流式共享模型制品，但部署与指标不同。
- 部署清单应固化版本 + digest，而不是动态 latest。
- 在线服务需要网关能力：批处理、限流、健康检查、灰度、可观测性。
- 冷启动、漂移和反馈闭环是长期运行的关键。

## 练习

1. 为在线评分设计 p95 延迟预算，拆解模型加载/预处理/推理/网络。
2. 设计一个 canary 发布与自动回滚规则。
3. 如何在不泄露 PII 的前提下监控输入漂移？

---

下一章：[第 25 章 规模、性能与成本](25-scale-cost.md)
