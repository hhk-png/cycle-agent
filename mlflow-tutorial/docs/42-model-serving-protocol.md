# 第 42 章 模型服务与推理协议：从 `/invocations` 到生产网关

## 学习目标

- 区分模型制品、模型运行时、评分协议和推理网关四个层次，知道问题应该落在哪一层。
- 读懂 MLflow 评分服务的端点、输入形状、输出约定和错误语义，并能用 `curl` 独立验证。
- 理解 `MLmodel` 在服务启动时如何被解析、加载和缓存，以及签名在边界上到底检查什么。
- 比较 `mlflow models serve`、容器镜像、MLServer/KServe V2、Triton、Ray Serve 的适用边界。
- 为在线推理设计延迟预算、批处理、并发、超时、优雅下线、灰度和回滚。
- 把 MiniMLflow 的 `scoring.py` 映射到真实协议，并知道它刻意省略了什么。

## 42.1 四个层次：不要把所有问题都叫“部署”

“把模型部署上线”这句话至少包含四层，混淆层次会导致错误的选型：

| 层次 | 解决的问题 | 典型产物 | 失败表现 |
|---|---|---|---|
| 模型制品 | 模型如何被描述、加载、复现 | `MLmodel`、权重、签名、依赖 | 本地能跑，换机器加载失败 |
| 模型运行时 | 用哪个进程/解释器/设备执行 | Python + PyFunc、ONNX Runtime、Triton backend | 依赖冲突、GPU 未初始化、冷启动长 |
| 评分协议 | 请求和响应长什么样 | `/invocations`、KServe V2、gRPC | 客户端字段对不上、错误码混乱 |
| 推理网关 | 流量如何路由、限流、观测、发布 | KServe、Seldon、Envoy、云推理服务 | 无法灰度、过载、故障扩散 |

MLflow 主要覆盖前两层，并提供一个**开发/验证用**的第三层实现（`mlflow models serve`）。第四层通常交给 KServe、Seldon、Triton、Ray Serve、BentoML、云厂商推理服务或自建网关。把 `mlflow models serve` 当成生产网关，是把“协议参考实现”误当成“流量产品”。

一个常见的架构分工：

```text
训练/注册                    部署控制面                     数据面
─────────                   ──────────                     ──────
Run → Model Version  ──>  解析 alias、固化版本  ──>  推理网关（路由/限流/灰度）
                                  │                         │
                                  └── 拉取 artifact ──> 模型运行时（PyFunc/ONNX/...）
                                                            │
                                                      评分协议 /invocations
```

控制面负责“部署哪个版本”，数据面负责“每个请求怎么算”。两者可以用同一个仓库、同一个 CI，但不能用同一个可用性假设：控制面可以分钟级不可用，数据面不行。

## 42.2 评分协议：端点和语义

MLflow 的模型服务遵循一个很小的 HTTP 契约。不同版本、不同后端会有细节差异，但核心端点稳定：

| 方法 | 路径 | 用途 | 是否应鉴权 |
|---|---|---|---|
| `GET` | `/ping` | 模型已加载则 200，未加载/加载失败则 404 | 通常免鉴权 |
| `GET` | `/health` | 与 `/ping` 同一类“模型加载状态”探针 | 通常免鉴权 |
| `GET` | `/version` | 服务端/MLflow 版本 | 可公开 |
| `POST` | `/invocations` | 执行预测 | 必须鉴权 |

三个容易混淆的点：

1. **`/ping` 与 `/health` 不是 liveness/readiness 两层语义。** 在 MLflow OSS 的 scoring server 里，两者都由“模型是否已加载”决定：已加载返回 200，否则 404。若需要“进程活着”与“模型就绪”分离，应在网关/包装层另加端点，或把 liveness 指向一个只返回进程状态的轻量端点。
2. **健康检查不要暴露内部信息。** 返回 `{"status":"ok"}` 即可；不要把数据库 DSN、模型路径、环境变量放进响应。
3. **`/invocations` 的路径是固定的。** 多模型路由应该由网关按模型名/租户前缀转发，而不是让每个模型自己发明 `/predict`。

### 42.2.1 输入形状

MLflow 评分服务支持的常见 JSON 形状：

```json
{"dataframe_split": {"columns": ["tenure", "calls"], "data": [[12, 3], [3, 8]]}}
```

```json
{"dataframe_records": [{"tenure": 12, "calls": 3}, {"tenure": 3, "calls": 8}]}
```

```json
{"instances": [{"tenure": 12, "calls": 3}, {"tenure": 3, "calls": 8}]}
```

```json
{"instances": [[12, 3], [3, 8]]}
```

```json
{"inputs": {"tenure": [12, 3], "calls": [3, 8]}}
```

它们不是“随便都能用”的同义词：

- `dataframe_split` 是列式，适合表格模型；列顺序由 `columns` 显式给出。
- `dataframe_records` 是行式字典，可读性好，但每行重复列名，体积更大。
- `instances` 是更通用的推理协议形状；当它是 `list[list]` 时，必须能从模型签名或特征名推断列顺序，否则应拒绝而不是猜。
- `inputs` 既可接列式字典，也可接记录列表；它是更宽松的入口，适合与外部协议对接。
- CSV 也可以作为请求体（`Content-Type: text/csv`），但生产 API 更推荐 JSON：类型、空值、嵌套结构的表达更明确。

**列顺序是服务事故的高发点。** 训练时特征顺序是 `[tenure, calls]`，服务时传成 `[calls, tenure]`，模型不会报错，只会给出稳定但错误的预测。签名和显式 `columns` 是把这类事故前移的手段。

### 42.2.2 输出形状

MLflow 评分服务通常返回：

```json
{"predictions": [0.82, 0.17]}
```

回归返回数值数组，分类返回标签或概率数组，具体取决于 flavor 的 `predict` 实现。不要在客户端假设“一定是标量”或“一定是二维数组”；把输出当作 `list`，再按模型签名/业务约定解释。

对于需要保留行级元数据的场景（请求 ID、模型版本、解释），常见做法是：

- 协议层仍返回 `predictions`，网关在响应头加 `X-Model-Version`、`X-Request-Id`；
- 或者由业务 API 包一层，返回 `{"predictions": [...], "model_version": "...", "request_id": "..."}`；
- 不要把模型版本写进每一个预测元素，除非下游确实需要逐条归因。

### 42.2.3 签名在边界上检查什么

签名（signature）描述输入列名、类型和输出。它在服务边界上的价值不是“替你做完整数据校验”，而是抓住最贵的集成错误：

- 缺列：请求没有模型需要的特征；
- 多列/错列：列顺序或命名漂移；
- 类型不符：字符串被当成数值、布尔被当成 0/1；
- 空值策略：哪些列允许空，空值如何填充。

不同 flavor 的严格程度不同。真实 MLflow 会在 PyFunc 层做一部分校验，但很多模型内部仍会做隐式类型转换；MiniMLflow 选择更严格的策略：有签名就逐列检查类型，缺列直接失败。生产实现要在“严格”和“可用”之间做选择：

- 严格：坏请求早失败，错误可归因；代价是客户端升级/字段变更更容易被拒。
- 宽松：兼容性好；代价是错误可能被静默吞掉，直到线上指标异常。

无论选哪种，**服务端必须自己校验**，不能只依赖 Python 客户端。否则绕过客户端直接发 HTTP 就能写入非法输入。

### 42.2.4 错误语义

一个可运营的评分服务至少区分：

| 状态码 | 含义 | 客户端应该做什么 |
|---|---|---|
| 400 | 请求体/字段/类型不合法 | 修正请求，不要重试 |
| 401/403 | 未认证/无权限 | 刷新凭证或申请权限 |
| 404 | 路径/模型不存在 | 检查路由与模型名 |
| 413 | 请求体过大 | 减小批量或改用异步 |
| 429 | 限流/配额 | 退避重试，遵守 `Retry-After` |
| 500 | 模型内部错误 | 有限重试；持续失败则回滚 |
| 503 | 依赖不可用/未就绪 | 重试并检查健康状态 |

错误响应不要回传堆栈、绝对路径、SQL、token。可以返回稳定的错误码和 `request_id`，把详细信息写进服务端日志。Mini 的评分服务统一返回 `{"error": "..."}`，生产应扩展为 `{"error": {"code": "...", "message": "...", "request_id": "..."}}`。

一个容易被忽略的传输层细节：**在返回 401/413 之前要先读取（或有界排空）请求体**。如果服务端在客户端还在发送大 body 时直接关闭连接，Windows 上客户端常看到 `ConnectionAbortedError`，而不是你精心构造的 413 JSON；在 HTTP/1.1 keep-alive 下还会污染下一个请求。Mini 的评分服务在拒绝超大请求时会排空一个有界前缀并显式 `Connection: close`。

## 42.3 `MLmodel` 在运行时长什么样

一个保存好的模型目录：

```text
model/
  MLmodel
  model.json
  requirements.txt
  input_example.json
  signature.json
```

`MLmodel` 是运行时的入口清单，核心字段包括：

```yaml
artifact_path: model
flavors:
  python_function:
    loader_module: mini_mlflow.models.linear
    data: model.json
    env: ...
  linear:
    model_path: model.json
signature:
  inputs:
    - {name: tenure, type: double}
  outputs:
    - {name: prediction, type: double}
run_id: ...
utc_time_created: ...
```

加载顺序通常是：

1. 读 `MLmodel`，确认有 `python_function` flavor；
2. 根据 `loader_module` 找到加载器（白名单）；
3. 读 `data` 指向的权重文件；
4. 反序列化为 flavor 对象；
5. 包装成统一的 `PyFuncModel`，暴露 `predict(data)`；
6. 读取签名，准备边界校验。

三个生产要点：

- **加载器必须是白名单。** 任意 `loader_module` + pickle 等于远程代码执行；第 11、20 章已经展开。
- **加载发生在启动时，不是每个请求。** 大模型/GPU 初始化可能几十秒；每请求加载会把 p99 拉到不可接受。
- **模型目录要可验证。** 记录 artifact digest、签名、SBOM；部署时先校验再加载，避免“拉到了半截文件仍然启动”。

模型 URI 的解析也应该在部署时固化：

```text
# 3.x：ModelVersion 通过 model_id 关联 LoggedModel
models:/churn@champion  →  version=17, model_id=m-..., source=models:/m-..., digest=sha256:...
# 2.x 兼容：source=runs:/<run>/model
```

部署清单应同时固化 `model_id`、Registry version 与 artifact digest；只记 alias 或只记 `runs:/` 都无法完整归因。

服务启动时记录解析结果，运行时不反复解析 alias。否则 alias 一改，正在运行的副本会在重启后悄悄换版本，回滚和归因都会失真。

## 42.4 `mlflow models serve` 的内部与边界

真实 MLflow 3.x 的 `mlflow models serve` 使用 FastAPI + uvicorn；MLServer 后端已在 3.13 移除（更早版本可 `--enable-mlserver`，更早的 Flask/gunicorn 也已退出）。参数随版本变化：

```bash
mlflow models serve \
  -m models:/churn@champion \
  -h 127.0.0.1 -p 5001 \
  -w 2 \
  --env-manager uv
```

需要知道的边界：

- `-w/--workers` 控制 worker 数；worker 之间不共享内存，模型会被重复加载。
- `--env-manager` 决定如何准备依赖（local/virtualenv/uv/conda 等，随版本变化）；生产更常见的是把依赖固化进镜像，而不是启动时创建环境。
- `--no-conda` 是旧版本常用参数；新版本以 `--env-manager` 为主，升级前用 `--help` 核对。
- 服务默认没有 TLS、没有用户体系、没有限流、没有多模型路由；这些都应该在网关层解决。
- 请求体大小、超时、并发上限由 Web 服务器/容器/网关共同决定，不能只改一个地方。

一个最小启动检查清单：

```text
[ ] 模型 URI 能解析到具体版本
[ ] artifact 完整且 digest 匹配
[ ] 依赖能在目标镜像中导入
[ ] /ping 与 /health 都返回 200
[ ] /invocations 用真实样例返回预期形状
[ ] 错误请求返回 4xx 而不是 500
[ ] 日志不包含 token/PII
```

## 42.5 容器、Kubernetes 与推理网关

### 42.5.1 从模型到镜像

真实 MLflow 提供 `mlflow models build-docker` 生成可运行的评分镜像。它的价值是“把运行时依赖固化”，不是“自动获得生产能力”。一个生产镜像至少还要：

- 固定基础镜像 digest，非 root 运行；
- 只读根文件系统（模型目录可读，临时目录可写）；
- 健康检查与优雅下线；
- 资源 requests/limits；
- 镜像扫描与 SBOM；
- 模型与镜像分离：模型更新不必重建镜像，镜像更新不必然换模型。

模型与镜像分离的两种做法：

| 做法 | 优点 | 缺点 |
|---|---|---|
| 模型烤进镜像 | 启动快、不可变、回滚简单 | 每次模型更新都要构建/推送镜像 |
| 启动时下载 artifact | 镜像通用、模型独立发布 | 冷启动慢、依赖对象存储可用性 |

大规模平台通常选择“基础镜像 + initContainer/ sidecar 下载模型 + 本地缓存”，并用 digest 固定模型版本。

### 42.5.2 Kubernetes 参考清单

下面的清单只表达结构，具体镜像、资源与探针参数要按压测结果调整：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: churn-scorer
spec:
  replicas: 3
  strategy:
    rollingUpdate: {maxSurge: 1, maxUnavailable: 0}
  selector:
    matchLabels: {app: churn-scorer}
  template:
    metadata:
      labels: {app: churn-scorer}
    spec:
      securityContext:
        runAsNonRoot: true
        readOnlyRootFilesystem: true
      containers:
        - name: scorer
          image: registry.example.com/churn-scorer@sha256:...
          ports: [{containerPort: 5001}]
          env:
            - {name: MODEL_URI, value: "models:/churn@champion"}
          readinessProbe:
            httpGet: {path: /health, port: 5001}
            periodSeconds: 5
          livenessProbe:
            httpGet: {path: /ping, port: 5001}
            periodSeconds: 10
          resources:
            requests: {cpu: "500m", memory: "1Gi"}
            limits: {cpu: "2", memory: "2Gi"}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
      volumes:
        - name: tmp
          emptyDir: {}
```

还要补：

- `HorizontalPodAutoscaler`：按 CPU/自定义指标（队列长度、p95）扩缩；
- `PodDisruptionBudget`：滚动升级/节点维护时保留最小可用副本；
- `NetworkPolicy`：只允许网关访问评分端口；
- `Ingress/Gateway API`：TLS、鉴权、限流、路由；
- `ServiceMonitor`/OTel Collector：指标与 trace；
- `Secret`/External Secrets：对象存储、token、模型仓库凭证。

### 42.5.3 与其他推理运行时对接

| 运行时/协议 | 适合 | 与 MLflow 的关系 |
|---|---|---|
| MLflow `models serve` | 开发、验证、低流量内部服务 | 直接加载 `models:/` |
| MLServer（KServe V2） | 标准推理协议、多框架 | MLflow 3.13 起移除了内置 MLServer 后端；需作为外部运行时接入，不能再用 `--enable-mlserver` |
| KServe | K8s 上的多模型、灰度、自动扩缩 | 常把 MLflow 当模型源/注册表 |
| Seldon | 企业推理图、A/B、解释 | 与 Registry 集成 |
| Triton | GPU、动态批处理、多框架 backend | 需要转换/导出模型格式 |
| Ray Serve | Python 组合、LLM 编排、弹性 | 可直接加载 PyFunc 或自定义 |
| BentoML | 打包/服务/部署一体化 | 与 MLflow 制品互转 |

KServe V2 协议的核心路径是：

```text
GET  /v2/health/ready
GET  /v2/models/{model}/ready
POST /v2/models/{model}/infer
```

它的输入输出是 `inputs`/`outputs` 的张量描述，比 MLflow 的 `dataframe_split` 更通用，也更适合多框架。选择协议时要看客户端生态，而不是只看“谁更标准”。

## 42.6 性能工程：延迟、吞吐与成本

### 42.6.1 延迟预算

一个在线请求的 p95 预算可以拆成：

```text
网络 + TLS          5 ms
网关鉴权/限流        2 ms
请求解析/校验        3 ms
特征读取/拼接       15 ms
预处理/tokenize      8 ms
模型推理            40 ms
后处理/序列化        5 ms
────────────────────────
合计               ~78 ms
```

这张表的作用不是精确，而是让团队知道“优化哪一段”。如果模型推理只占 40/78，先把特征读取从 15 ms 降到 5 ms，比换更快的 GPU 更划算。

### 42.6.2 批处理与并发

- **静态批处理**：客户端一次发多行；实现简单，但尾延迟由最大批次决定。
- **动态批处理**：服务端在几毫秒窗口内聚合请求；提高 GPU 利用率，但增加排队延迟。
- **连续批处理**：token 级调度，适合 LLM；实现复杂，通常由专用运行时提供。

批处理的关键参数是“最大批次”和“最大等待时间”。只调大批次会提高吞吐、恶化 p99；只调小等待会降低延迟、浪费 GPU。要用压测画吞吐-延迟曲线，而不是拍脑袋。

并发模型也要区分：

- 同步阻塞：每请求占一个线程，适合 CPU 推理；
- 异步 I/O：适合等待外部服务（特征库、LLM provider）；
- 多进程：绕开 GIL，但内存/模型副本翻倍；
- GPU：注意显存、并发 kernel、批处理与流式输出。

### 42.6.3 容量估算

用 Little 定律做粗算：

```text
并发请求数 ≈ QPS × 平均延迟（秒）
GPU 数 ≈ 峰值 QPS × 单请求计算量 / (单卡吞吐 × 目标利用率)
```

例：峰值 200 QPS，p95 80 ms，则平均并发约 16；如果单副本在 p95 内只能处理 40 QPS，至少需要 5 个副本，再留 30% 余量。这个估算必须用压测校正。

### 42.6.4 成本优化顺序

1. 减少无效请求（缓存、去重、批量）；
2. 降低单请求计算量（特征裁剪、模型蒸馏/量化）；
3. 提高利用率（动态批处理、GPU 共享）；
4. 用更便宜的运行时/硬件；
5. 最后才考虑“换更贵的实例”。

不要用“加机器”掩盖协议层的重复计算。

### 42.6.5 批量推理与流式响应

在线评分不是唯一形态。批量推理关注吞吐和成本，流式响应关注首 token 延迟和连接管理：

| 形态 | 关键指标 | 常用实现 |
|---|---|---|
| 批量打分 | 吞吐、单条成本、失败重试 | Spark UDF、Ray Data、批任务、模型批量评分 API（如 `mlflow.models.predict`，以锁定版本为准） |
| 在线请求-响应 | p95/p99、错误率 | `/invocations` + 网关 |
| 流式响应 | 首 token 延迟、token/s、取消率 | SSE / HTTP chunked / gRPC stream |

批量推理的工程要点：

- 分片与断点续跑，失败分片可重试；
- 输入/输出都带 `request_id` 或业务主键，保证幂等；
- 模型按 executor 缓存，避免每个分片重新加载；
- 输出表记录模型版本与 digest，便于回溯；
- 大批量不要走在线 `/invocations`，会把网关和超时拖垮。

LLM 流式响应的工程要点：

- 协议通常是 SSE（`text/event-stream`）或 chunked JSON；
- 客户端断开要能取消上游请求，避免继续计费；
- 首 token 延迟和总时长要分开监控；
- 代理层要关闭响应缓冲，否则“流式”会变成一次性返回；
- 重试要谨慎：已经输出部分 token 后重试可能重复计费/重复输出；
- 结构化输出（JSON）与流式展示要分开：先流式给人看，再校验/落库。

MLflow 的评分服务主要面向请求-响应；LLM 流式通常由应用网关/推理框架承担。理解协议边界，才能决定“哪些能力放在 MLflow，哪些放在网关”。

### 42.6.6 模型热更新

模型热更新有三种常见做法：

1. **滚动替换副本**：新副本加载新版本，旧副本逐步下线；最简单、最常用。
2. **进程内热加载**：收到信号后重新加载模型并原子切换；节省资源，但要处理并发请求、内存峰值和失败回滚。
3. **多模型共存 + 路由**：同一进程加载多个版本，由请求头/租户/流量比例选择；适合灰度和 A/B。

进程内热加载的陷阱：

- 新模型加载失败时，必须继续用旧模型，而不是让服务进入半可用状态；
- 旧模型对象要等在途请求结束后再释放，避免 use-after-free/显存被提前回收；
- 模型缓存 key 要包含版本 + digest，不能只用模型名；
- 热加载期间的延迟峰值要纳入 SLO；
- 多 worker 下每个 worker 都要收到 reload 信号，或者由编排层滚动替换。

对大多数团队，**滚动替换副本**比进程内热加载更可靠；只有在模型极大、副本成本极高时，才值得做进程内热加载。

## 42.7 可靠性与发布

### 42.7.1 启动、就绪与优雅下线

- 启动时加载模型，加载完成前 readiness 返回失败；
- 收到 SIGTERM 后停止接收新请求，等待在途请求完成（grace period）；
- 关闭线程池/连接池，刷新日志/指标；
- 失败时保留旧副本，不要一次性全量替换。

Kubernetes 的 `terminationGracePeriodSeconds` 要大于最长请求时间；否则滚动升级会切断长请求。

### 42.7.2 灰度、影子与回滚

| 模式 | 流量 | 验证目标 | 风险 |
|---|---|---|---|
| Canary | 5% → 25% → 100% | 错误率、延迟、业务指标 | 小流量可能看不出长尾 |
| Shadow | 复制请求，不返回结果 | 新模型输出分布、性能 | 双倍成本、隐私 |
| Blue/Green | 整量切换 | 回滚速度 | 需要双倍容量 |
| A/B | 按用户分桶 | 业务效果 | 需要实验设计 |

回滚的前提是“版本可寻址”：

```text
deployment manifest: model=churn version=17 digest=sha256:...
rollback:             model=churn version=16 digest=sha256:...
```

如果服务只记录 `models:/churn@champion`，回滚时无法知道当时到底加载了哪个版本。**部署清单必须固化解析后的版本和 digest。**

### 42.7.3 重试与幂等

- 只对可重试错误（429/503/超时）重试；
- 用指数退避 + 抖动，遵守 `Retry-After`；
- 请求带 `request_id`，服务端日志可关联；
- 写操作（如果有）用幂等键，避免重复扣款/重复下单；
- 超时不是“失败”，可能已经执行；客户端要有补偿/查询机制。

## 42.8 安全与多租户

评分服务的安全边界至少包括：

- **认证**：网关验证 token/OIDC，服务只信任网关转发的身份头；
- **授权**：按模型/租户/环境授权，不要“登录即可调用所有模型”；
- **隔离**：多租户共享 GPU 时，注意显存、缓存、日志、模型路径的隔离；
- **输入限制**：请求体大小、批量大小、字符串长度、嵌套深度；
- **输出限制**：避免把内部错误、PII、prompt 原文回传；
- **供应链**：模型 artifact 签名/校验，镜像 SBOM，依赖锁定；
- **网络**：评分服务只允许网关访问，禁止直接出公网；
- **密钥**：provider key、对象存储凭证放在 secret manager，不进入镜像/日志。

LLM 评分服务还多一层：prompt injection、工具调用越权、输出泄露。第 20、37 章已经展开；这里只强调：**推理网关不应该把“模型输出”直接当成可信指令执行。**

## 42.9 观测与反馈

### 42.9.1 三类指标

- **RED**：Rate、Errors、Duration（请求率、错误率、延迟分位）；
- **USE**：Utilization、Saturation、Errors（CPU/GPU/内存/队列）；
- **业务**：预测分布、空值率、阈值命中率、转化/流失、成本/请求。

只有 RED 不够：服务很快、错误率很低，但预测分布漂移，业务仍然会坏。

### 42.9.2 Trace 与日志

每次请求记录：

```text
request_id, model_name, model_version, artifact_digest,
input_shape, output_shape, latency_ms, status,
feature_source, prompt_version (if LLM)
```

敏感字段要脱敏/哈希/截断。Trace 适合看调用链，指标适合看趋势，日志适合看单次事故；三者用 `request_id` 关联。

### 42.9.3 反馈闭环

评分服务只产生预测，不产生“对错”。要把线上效果做起来，需要：

1. 记录预测与模型版本；
2. 收集真实标签/人工反馈（可能延迟）；
3. 按模型版本、切片、时间窗口计算指标；
4. 与离线评测对比，判断是数据漂移还是模型退化；
5. 触发再训练/回滚/阈值调整。

第 17、24 章讲评测与漂移，第 39 章讲 GenAI 的 feedback/assessment；本章只强调服务端要把“可关联的请求标识”留出来。

## 42.10 MiniMLflow 的评分实现

Mini 的评分服务在 `mini_mlflow/scoring.py`，刻意保持小：

```python
server = create_scoring_server(
    "models:/house-price-linear@champion",
    host="127.0.0.1",
    port=5001,
    auth_token="demo-token",
)
```

它实现了：

- 启动时解析并加载模型（`load_model`），不在请求里重复加载；
- `GET /ping`、`/health`、`/version`；
- `POST /invocations`；
- `dataframe_split`、`dataframe_records`、`instances`、`inputs`、裸 `{columns,data}`；
- 可选 Bearer token（常量时间比较）；
- 请求体上限（默认 16 MiB）；
- 400/401/404/413/500 的基本映射；
- `ThreadingHTTPServer`，每请求一个线程。

关键代码路径：

```python
def records_from_payload(payload, model):
    if "dataframe_split" in payload:
        ...
    if "dataframe_records" in payload:
        ...
    if "instances" in payload:
        ...
    if "inputs" in payload:
        ...
```

以及：

```python
data = records_from_payload(payload, self.server.model)
predictions = self.server.model.predict(data)
self._json({"predictions": predictions})
```

它**没有**实现：

- 动态批处理、队列、背压；
- 多 worker、优雅下线；
- 限流、配额、多租户；
- 指标暴露/Prometheus；
- TLS、OIDC；
- 多模型路由、灰度；
- 流式响应；
- 模型热更新；
- GPU。

这些不是“漏掉”，而是第 24、41 章留给生产网关/编排的职责。Mini 的价值是让你能用一个 `curl` 看清协议，再用同一套词汇读 KServe/Triton 的文档。

## 42.11 契约测试：把协议固定下来

服务协议最怕“今天能跑、明天客户端升级就坏”。最小契约测试矩阵：

| 用例 | 期望 |
|---|---|
| `GET /ping` | 200，`status=ok` |
| `GET /health` | 200，模型已加载 |
| `GET /version` | 200，含版本 |
| 未知路径 | 404 |
| 无 token 调 `/invocations` | 401 |
| `dataframe_split` | 200，预测数量 = 行数 |
| `dataframe_records` | 200 |
| `instances`（dict） | 200 |
| `instances`（list[list] + 签名） | 200 |
| 缺列/类型错误 | 400 |
| 非法 JSON | 400 |
| 超大请求体 | 413 |
| 模型内部异常 | 500，不泄露堆栈 |

`examples/15_serving_protocol.py` 把前 12 类做成可运行矩阵；`tests/test_serving_protocol.py` 把关键路径纳入 pytest。真实项目还应加：

- 并发/压测（Locust/k6）；
- 长稳测试（内存泄漏、连接泄漏）；
- 故障注入（模型加载失败、依赖超时）；
- 版本兼容测试（旧客户端 × 新服务端）。

## 42.12 生产检查清单

**制品与启动**

- [ ] 模型 URI 解析到具体版本，记录 digest；
- [ ] `MLmodel` 的 loader 在白名单内；
- [ ] 依赖锁定，镜像可复现；
- [ ] 启动加载失败时 readiness 失败，不接流量；
- [ ] 模型目录只读，临时目录可写。

**协议**

- [ ] `/ping`、`/health`、`/version`、`/invocations` 行为有契约测试；
- [ ] 输入形状、列顺序、类型有明确约定；
- [ ] 错误码稳定，响应不含堆栈/密钥/PII；
- [ ] 请求体/批量/超时有上限。

**性能**

- [ ] 有 p50/p95/p99 基线与压测报告；
- [ ] 批处理参数有吞吐-延迟曲线；
- [ ] 冷启动时间可接受，或已预热；
- [ ] 容量按峰值 QPS 与余量估算。

**可靠性**

- [ ] readiness/liveness 分离；
- [ ] 优雅下线，grace period > 最长请求；
- [ ] 灰度/回滚演练完成；
- [ ] 部署清单固化版本 + digest。

**安全**

- [ ] 网关鉴权，服务不直接暴露公网；
- [ ] 租户/模型级授权；
- [ ] 输入大小/深度限制；
- [ ] 依赖与镜像扫描；
- [ ] 日志脱敏。

**观测**

- [ ] RED/USE/业务指标；
- [ ] trace 含 request_id/model_version；
- [ ] 预测日志可关联真实标签；
- [ ] 告警阈值与值班 runbook。

## 42.13 与真实 MLflow 的差距

- 真实 MLflow 的 `models serve` 基于 FastAPI/uvicorn，Mini 基于标准库 `ThreadingHTTPServer`；
- 真实服务支持更多 flavor、依赖管理、环境管理器与容器构建；Mini 只有 linear flavor；
- 真实服务通过外部运行时（KServe/Triton 等）支持标准推理协议；MLServer 内置后端已在 3.13 移除；Mini 只实现最小 JSON 协议；
- 真实生产还需要网关、TLS、OIDC、限流、批处理、GPU、指标；Mini 全部省略；
- 真实 MLflow 的 artifact 代理/预签名、模型注册与部署编排更复杂；见第 41 章。

## 小结

- “部署”要拆成制品、运行时、协议、网关四层；MLflow 主要覆盖前两层，并提供协议参考实现。
- `/ping`、`/health`、`/version`、`/invocations` 是最小契约；输入形状和列顺序是高频事故点。
- 签名是边界契约，不是完整数据校验；服务端必须自己校验。
- 模型在启动时加载并缓存；版本与 digest 要在部署清单里固化。
- 生产网关要补批处理、并发、超时、限流、灰度、回滚、可观测性和安全。
- Mini 的评分服务是“可读的协议样本”，不是流量产品；契约测试是把它用对的前提。

## 练习

1. 用 `curl` 对 Mini 评分服务分别发送 `dataframe_split`、`dataframe_records`、`instances`、`inputs`，记录响应差异。
2. 给 Mini 评分服务增加 `/metrics`（Prometheus 文本格式），暴露请求数、错误数、延迟直方图。
3. 设计一个动态批处理方案：最大批次 32、最大等待 10 ms，说明它如何影响 p50/p99。
4. 为一个 200 QPS、p95 80 ms 的服务做容量估算，并给出 30% 余量下的副本数。
5. 写一份 canary 发布与自动回滚规则，包含错误率、p95、业务指标三个维度。
6. 解释为什么 `/ping` 和 `/health` 要分开，以及混用会导致什么线上故障。
7. 把第 41 章的 Compose 编排扩展一个评分服务，说明模型如何从 MinIO 加载、如何做 readiness。

---

下一章：[第 43 章 综合实战：把流失预警做成可运营的 AI 产品](43-capstone-churn.md)
