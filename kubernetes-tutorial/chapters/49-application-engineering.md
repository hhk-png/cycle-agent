# 第 49 章 应用工程：把代码写成能在 Kubernetes 上运行的系统

> Kubernetes 不会把一个单机应用自动变成分布式系统，它只会把应用原有的耦合暴露得更快。本章从应用视角出发，讨论进程如何启动、如何接收流量、如何退出、如何声明资源、如何重试和降级，以及如何把“能跑”变成“可运营”。平台机制见第 6、7、11、27、32 章；本章补的是**代码与配置必须满足的契约**。

## 49.1 云原生应用的运行契约

平台能提供调度、重启、服务发现、滚动发布和网络策略，但不能替应用处理业务语义。一个能在 Kubernetes 上稳定运行的服务，至少要满足下面这组契约：

| 契约 | 平台提供 | 应用必须做 | 不满足的典型症状 |
|---|---|---|---|
| 进程可替换 | 重启、重建 Pod | 不在本地保存不可恢复状态 | 重启后会话丢失、任务重复 |
| 配置外置 | 注入 ConfigMap/Secret | 启动时读取、校验、可热更新 | 镜像与环境和代码耦合 |
| 健康可观测 | 探针、事件、指标采集 | 提供有语义的健康端点 | 流量进入未就绪实例 |
| 优雅终止 | SIGTERM、grace period | 停止接流量、排空在途请求 | 发布时 502/499 |
| 资源可声明 | 调度、QoS、驱逐 | 给出真实 requests/limits | 被 OOMKill、CPU 节流、调度失败 |
| 可水平扩展 | Service、HPA | 无共享单点、支持并发 | 加副本反而更慢或数据错乱 |
| 幂等与可重试 | 重启、重试控制器 | 请求幂等、去重、可补偿 | 重复扣款、重复消息 |
| 可观测 | 日志/指标/追踪管道 | 结构化日志、RED 指标、trace 传播 | 出事只能猜 |
| 安全最小权限 | RBAC、PSA、沙箱 | 非 root、只读、最小 token | 逃逸面大、权限过宽 |
| 升级兼容 | 滚动更新、版本 skew | API/DB 向前兼容、可回滚 | 新旧版本同时运行时崩溃 |

这张表解释了一个常见现象：**同样的镜像在虚拟机上没问题，上 Kubernetes 后问题集中爆发**。原因通常不是 Kubernetes “不稳定”，而是它把“进程会随时被替换”这件事变成了常态。

可以用四个成熟度级别评估一个应用：

| 级别 | 特征 | 典型做法 |
|---|---|---|
| L0 直接搬迁 | 单实例、本地磁盘、手工配置 | 只是把 jar/二进制塞进容器 |
| L1 容器化 | 镜像可构建、配置基本外置 | Dockerfile + 环境变量 |
| L2 云原生 | 探针、资源声明、优雅终止、结构化日志 | 可以安全滚动更新 |
| L3 韧性系统 | 幂等、限流、降级、SLO、故障演练 | 可以承受节点/依赖故障 |

多数迁移事故发生在“L1 当成 L2 用”：镜像能启动，但没有 readiness、没有资源声明、退出不排空，于是第一次发布就把问题暴露给用户。

## 49.2 启动、就绪与存活：三种状态不是一回事

### 49.2.1 三个探针的职责边界

| 探针 | 回答的问题 | 失败后果 | 应该检查什么 |
|---|---|---|---|
| startupProbe | 进程是否完成启动 | 重启容器 | 迁移、预热、加载模型、建立连接池 |
| readinessProbe | 现在能否接收新流量 | 从 Endpoints 摘除 | 监听端口、依赖可用性、限流状态 |
| livenessProbe | 进程是否还活着/是否卡死 | 重启容器 | 事件循环、死锁、内部健康状态 |

一个常见错误是三个端点都指向 `/healthz`，而 `/healthz` 又检查数据库。结果数据库抖动时，所有 Pod 的 liveness 同时失败，平台开始集体重启应用，把一次依赖故障放大成全面故障。

原则：

- **liveness 只检查进程自身**。不要检查下游、磁盘配额、外部 API。
- **readiness 可以检查关键依赖，但要有降级策略**。依赖短暂不可用时，是摘流量还是继续服务降级结果，要按业务决定。
- **startup 给足冷启动预算**。模型加载、JVM 预热、缓存填充都放在 startup 窗口内。
- **探针阈值是预算，不是随手填的数字**。检测时间约为 `periodSeconds × failureThreshold`，再叠加 `timeoutSeconds`。

```text
startup 预算   = failureThreshold × periodSeconds + timeoutSeconds
故障发现时间   = failureThreshold × periodSeconds
最短恢复时间   ≈ 检测时间 + 重启/重新调度时间 + 再次启动时间
```

例如 `periodSeconds: 10`、`failureThreshold: 3` 意味着最坏要 30 秒才能发现故障；对延迟敏感服务可能太长，对启动很重的服务可能太短。探针参数要和 SLO、发布策略一起调。

### 49.2.2 一个可直接套用的探针配置

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: shop
spec:
  replicas: 4
  selector:
    matchLabels:
      app: checkout
  template:
    metadata:
      labels:
        app: checkout
    spec:
      terminationGracePeriodSeconds: 45
      containers:
        - name: checkout
          image: registry.example.com/shop/checkout@sha256:0123456789abcdef
          ports:
            - name: http
              containerPort: 8080
          startupProbe:
            httpGet:
              path: /healthz/startup
              port: http
            periodSeconds: 3
            failureThreshold: 40
            timeoutSeconds: 2
          readinessProbe:
            httpGet:
              path: /healthz/ready
              port: http
            periodSeconds: 5
            failureThreshold: 2
            timeoutSeconds: 2
            successThreshold: 1
          livenessProbe:
            httpGet:
              path: /healthz/live
              port: http
            periodSeconds: 10
            failureThreshold: 3
            timeoutSeconds: 2
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 1Gi
```

要点：

- 端口用**命名端口**，避免改端口时探针和 Service 不一致。
- startup 的 `failureThreshold: 40`、`periodSeconds: 3` 给约 120 秒启动预算；超过就重启，而不是无限等。
- readiness 的 `failureThreshold: 2` 让短暂抖动不至于立刻摘流量，但也不能太钝。
- liveness 的阈值比 readiness 宽松，避免把“暂时忙”误判为“死锁”。
- `terminationGracePeriodSeconds: 45` 与 49.3 的排空逻辑配套，不是孤立参数。

### 49.2.3 readiness 与依赖分级

把依赖分成三类，分别决定 readiness 行为：

| 依赖类型 | 例子 | readiness 策略 |
|---|---|---|
| 硬依赖 | 启动时必须连上的数据库、配置中心 | 未就绪则不 Ready，但要有超时和重试上限 |
| 软依赖 | 推荐服务、非关键缓存、搜索 | 降级继续服务，不因它摘掉全部流量 |
| 异步依赖 | 消息队列、日志管道 | 允许短暂堆积，用积压指标告警而不是摘流量 |

如果 readiness 把软依赖也当硬依赖，一个非关键服务故障就会让主服务全量不可用。反过来，如果硬依赖没就绪也返回 Ready，流量会在启动阶段打到必然失败的实例上。

**readiness gate** 用于把外部就绪条件纳入 Pod 状态：

```yaml
spec:
  readinessGates:
    - conditionType: example.com/load-balancer-ready
```

外部控制器写入对应 condition 后，Pod 才会被视为 Ready。它适合“平台资源比 Pod 晚就绪”的场景，例如负载均衡目标组注册、证书签发、服务网格注入完成。

### 49.2.4 探针的常见陷阱

- **HTTP 探针返回 3xx 也算成功**（200–399）。如果健康端点被重定向到登录页，可能一直“健康”。
- **exec 探针成本高**：每次探测都 fork 进程，在密集探测下会放大 CPU 和 PID 压力；能用 HTTP/gRPC 就别用 shell。
- **gRPC 探针**需要服务实现标准健康检查协议；不是所有框架默认支持。
- **探针超时太短**：GC 停顿或冷缓存会让健康检查偶尔超时，导致重启风暴。
- **在 liveness 里做重初始化**：重启是最后手段，不是恢复依赖的手段。
- **忽略 `successThreshold`**：liveness/startup 的 `successThreshold` 只能是 1；readiness 可以大于 1，用于避免抖动。

## 49.3 优雅终止：零停机的真实时序

### 49.3.1 删除一个 Pod 时发生了什么

```text
kubectl delete / rollout / 缩容 / 驱逐
  → API Server 写 deletionTimestamp，Pod 进入 Terminating
  → EndpointSlice 控制器把 Pod 标记为 terminating / ready=false
  → kube-proxy、Ingress、Service Mesh、云负载均衡逐步停止转发
  → kubelet 执行 preStop hook（同步，计入 grace period）
  → 向容器主进程发送 SIGTERM
  → 应用停止接收新请求、排空在途请求、关闭连接池
  → 超过 terminationGracePeriodSeconds 后 SIGKILL
```

关键点是：**“从 Endpoints 摘除”与“负载均衡真正停止转发”不是同一时刻**。kube-proxy 规则更新、Ingress 控制器重配置、云 LB 的健康检查周期都有延迟。应用如果收到 SIGTERM 立刻退出，仍可能有请求在旧连接上到达，表现为发布时的 502/499。

### 49.3.2 终止预算怎么算

```text
terminationGracePeriodSeconds
  ≥ preStop 等待时间
  + 应用排空在途请求的最长时间
  + 关闭连接池/提交 offset/刷盘时间
  + 安全余量
```

常见配置：

```yaml
spec:
  terminationGracePeriodSeconds: 45
  containers:
    - name: checkout
      lifecycle:
        preStop:
          httpGet:
            path: /internal/drain
            port: http
```

应用侧的 `/internal/drain` 应该：

1. 立即把 readiness 置为失败（或直接开始拒绝新请求）。
2. 等待一个“负载均衡传播窗口”，例如 5–10 秒。
3. 停止后台消费者拉取新消息。
4. 等待在途请求完成。
5. 关闭连接池、提交 offset、flush 缓冲。

如果 `preStop` 只是 `sleep 10`，而应用不处理 SIGTERM，SIGTERM 后进程仍可能直接退出，排空并没有发生。**preStop 解决“摘流传播”，SIGTERM handler 解决“进程内排空”，两者缺一不可。**

### 49.3.3 各语言的优雅退出

**Go**

```go
ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
defer stop()

srv := &http.Server{Addr: ":8080", Handler: mux}
go func() {
    if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
        log.Fatal(err)
    }
}()

<-ctx.Done()
shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
defer cancel()
_ = srv.Shutdown(shutdownCtx)
```

`Shutdown` 会停止接收新连接并等待在途请求；长连接（WebSocket、gRPC stream）需要业务自己跟踪并关闭。

**Java / Spring Boot**

```properties
server.shutdown=graceful
spring.lifecycle.timeout-per-shutdown-phase=20s
```

Spring Boot 2.3+ 支持 graceful shutdown；线程池、消息监听容器、连接池要按顺序关闭。JVM 收到 SIGTERM 后执行 shutdown hook，注意 hook 里不要做无限等待。

**Python**

- Gunicorn：`--graceful-timeout 30`，worker 收到 SIGTERM 后停止接受新请求并处理完当前请求。
- Uvicorn：`--timeout-graceful-shutdown 30`，需要应用配合 lifespan/shutdown 事件。
- 自己写的 `while True` 消费者要捕获 SIGTERM，设置停止标志，处理完当前批次再退出。

**Node.js**

```js
process.on('SIGTERM', () => {
  server.close(() => {
    // 关闭数据库/消息客户端后退出
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 20000).unref();
});
```

`server.close` 不会自动销毁 keep-alive 连接；需要 `server.closeIdleConnections()` 或跟踪 socket。

### 49.3.4 队列消费者与批处理

消费者比 HTTP 服务更容易在终止时丢消息或重复处理：

- 收到 SIGTERM 后**停止拉取新消息**，但继续处理已拉取的批次。
- 处理完成后提交 offset/ack；不要在拿到消息后立刻提交。
- 如果处理时间可能超过 grace period，要能 checkpoint 并在下次启动时从断点恢复。
- 幂等是底线：终止发生在“处理完但未提交”时，消息会被重复投递。
- CronJob 的 `concurrencyPolicy: Forbid` 可以避免上一轮未结束时又启动一轮，但不能替代应用内的锁。

### 49.3.5 零停机发布检查表

- [ ] readiness 在终止开始时尽快失败。
- [ ] preStop 或应用自身等待负载均衡传播窗口。
- [ ] SIGTERM handler 停止接新请求并排空在途请求。
- [ ] grace period 覆盖 preStop + 排空 + 关闭资源。
- [ ] PDB 允许滚动更新所需的并发驱逐。
- [ ] 长连接有最大生命周期和主动重连。
- [ ] 客户端对幂等请求配置了重试和超时。
- [ ] 发布期间观察 5xx、499、延迟和重启数。

## 49.4 配置、密钥与身份

### 49.4.1 配置来源与优先级

一个可运营的应用通常同时有多层配置：

```text
镜像内默认值
  < ConfigMap/环境变量
  < Secret
  < 启动参数/命令行
  < 外部配置中心（可热更新）
  < 特性开关（按请求/用户）
```

优先级要在代码里写清楚，并在启动日志中打印“最终生效的非敏感配置摘要”。否则排障时无法判断某个值到底来自哪里。

配置读取有两种时机：

| 方式 | 生效时机 | 适用 |
|---|---|---|
| 环境变量 | 容器启动时固定 | 简单、不可变配置 |
| 挂载文件 | kubelet 异步更新（有延迟） | 证书、大量配置、可热加载 |
| 启动参数 | 进程启动时 | 运行时/框架参数 |
| 配置中心 | 应用主动拉取/推送 | 动态开关、跨集群统一配置 |

### 49.4.2 配置热更新的三种做法

1. **重启生效**：把 ConfigMap 的哈希写进 Pod 模板 annotation，配置变化触发滚动更新。简单可靠，适合大多数服务。
2. **应用内热加载**：监听挂载文件变化（inotify 或轮询），校验后原子替换配置。要处理“写了一半”的文件和非法配置回滚。
3. **外部配置中心**：应用订阅变更，带版本号和回滚。适合大量动态开关，但引入了新的依赖和故障域。

Kubernetes 挂载的 ConfigMap/Secret 更新是**最终一致**的，且使用 `subPath` 挂载时不会自动更新。不要把“挂了 ConfigMap”当成“配置一定会实时同步”。

### 49.4.3 Secret 的正确用法

- 不要把密钥烘焙进镜像；镜像层和 registry 都可能被读取。
- 环境变量中的 Secret 可能出现在 `/proc`、崩溃转储、日志和子进程中；敏感值优先用文件挂载。
- 开启 etcd 静态加密和 KMS（第 11.13 节），并限制 `get/list secrets` 的 RBAC。
- 使用短期 ServiceAccount token（`automountServiceAccountToken: false` 或 projected token）而不是长期静态 token。
- 云上优先使用工作负载身份（IRSA、Workload Identity、Pod Identity）代替把云密钥放进 Secret（第 23.14 节）。
- 轮换要有“双密钥并存窗口”，否则轮换瞬间所有实例一起失效。

### 49.4.4 启动时校验，失败要快

配置错误越早暴露越好：

- 启动时解析并校验必填项、类型、范围、URL、证书有效期。
- 校验失败直接退出（非零退出码），让 Kubernetes 重启并暴露 CrashLoopBackOff；不要带着半配置启动。
- 对“可降级配置”区分致命与警告：例如推荐服务地址缺失可以降级，数据库连接串缺失必须失败。
- 在 readiness 中反映“配置已加载且通过校验”，而不是只检查端口。

## 49.5 资源、并发与容量

### 49.5.1 requests 与 limits 的应用侧含义

- `requests` 决定调度器如何记账，也影响 QoS 和驱逐顺序。
- `limits` 决定 cgroup 上限：CPU 超限被节流，内存超限被 OOMKill。
- 只写 `limits` 不写 `requests` 时，若没有 LimitRange 的 `defaultRequest`，Kubernetes 会把 request 默认成 limit（可能让 Pod 变成 Guaranteed），调度账本和你想的不一样。
- request 远低于真实用量会导致节点超卖，邻居互相争抢；request 远高于真实用量会浪费容量和成本。

应用团队要给出**基于压测的 P50/P95/P99 用量**，而不是拍脑袋。平台团队则要提供资源画像和 VPA 建议（第 20 章），但不能自动替业务决定延迟与成本的取舍。

### 49.5.2 不同语言的资源陷阱

| 运行时 | 主要内存构成 | 关键配置 | 常见问题 |
|---|---|---|---|
| JVM | heap + metaspace + direct buffer + thread stack + code cache | `-XX:MaxRAMPercentage`、`-XX:ActiveProcessorCount`、GC 选择 | 只算 heap，忽略堆外；CPU limit 导致 GC 线程不足 |
| Go | heap + goroutine stack + runtime | `GOMAXPROCS`、`GOGC`、`GOMEMLIMIT` | 不感知 cgroup CPU limit，GC 不及时 |
| Python | 解释器 + 每 worker 堆 + 原生扩展 | worker 数、`--preload`、对象池 | 多 worker 内存翻倍；GIL 限制 CPU 并行 |
| Node.js | V8 heap + external/native | `--max-old-space-size`、事件循环 | 单线程阻塞；heap 上限与容器 limit 不匹配 |
| .NET | GC heap + native + thread pool | Server GC、GC heap hard limit | 容器内 CPU/内存感知不足 |

几个具体建议：

- **JVM**：容器内存 1Gi 时不要默认 `-Xmx1g`；堆外、元空间、线程栈也要留空间。用 `MaxRAMPercentage=70–75` 起步，再压测调整。
- **Go**：设置 `GOMEMLIMIT` 接近容器 limit 的 80–90%，并让 `GOMAXPROCS` 感知 CPU quota（例如 `automaxprocs`）。
- **Python**：Gunicorn/Uvicorn 的 worker 数按 CPU 和内存共同决定；每个 worker 都是独立进程，内存不是共享的。
- **Node.js**：`--max-old-space-size` 要低于容器 limit，给 external memory 留余量。

### 49.5.3 并发、排队与过载

一条实用的容量关系：

```text
并发请求数 ≈ 吞吐（RPS） × 平均延迟（秒）        # Little's Law
目标利用率 ≈ 60%–75%（延迟敏感服务更低）
所需副本数 ≈ 峰值 RPS / 单副本安全 RPS × (1 + 余量)
```

当利用率接近 100%，排队延迟会非线性上升。CPU 节流、GC、连接池耗尽都会让“再加一点流量”变成“整体雪崩”。应用必须有限流和快速失败：

- 入口设置最大并发/连接数。
- 队列有界，满了立即拒绝或降级，不要无限排队。
- 下游超时 + 重试预算，避免重试放大流量。
- 过载时返回 429/503 并带 `Retry-After`，而不是拖到超时。

### 49.5.4 HPA 与应用设计

HPA 按 CPU/内存/自定义指标调整副本，但它不理解应用内部状态：

- CPU 型 HPA 依赖 requests 的准确性；request 设得离谱，扩容阈值就失真。
- 扩容有延迟（指标采集、HPA 周期、调度、拉镜像、启动探针），突发流量要靠 minReplicas/预热池兜底。
- 缩容太快会打断长连接和缓存；用 stabilization window 和 PDB 保护。
- 队列型负载更适合 KEDA 按积压扩缩（第 20.9 节、第 50.4 节）。
- 有状态分片不能简单加副本；扩缩要触发 rebalance，可能比故障更伤。

应用要暴露**能代表饱和度**的指标：并发数、队列长度、在途请求、连接池使用率、GC 停顿。只暴露 CPU 往往太晚。

## 49.6 韧性与一致性模式

Kubernetes 的重启不是业务重试：Pod 被删后，未完成的内存状态直接消失。应用必须自己处理失败语义。

| 模式 | 解决的问题 | 实现要点 |
|---|---|---|
| 超时 | 下游挂起拖垮调用方 | 每个远程调用都要有 deadline，且逐层递减 |
| 重试 | 瞬时故障 | 指数退避 + 抖动 + 最大次数 + 重试预算 |
| 幂等 | 重试/重复投递 | 幂等键、去重表、唯一约束、版本号 |
| 熔断 | 下游持续失败 | 半开探测、失败率阈值、快速失败 |
| 舱壁 | 故障隔离 | 按依赖/租户分线程池、连接池、队列 |
| 限流 | 过载保护 | 令牌桶/漏桶、并发上限、租户配额 |
| 降级 | 保核心功能 | 缓存旧值、关闭非关键功能、返回兜底 |
| 补偿 | 跨服务事务 | Saga、补偿动作、幂等重放 |

### 49.6.1 重试的三个陷阱

1. **重试风暴**：所有实例同时重试，把一次抖动放大成持续过载。要加抖动和预算。
2. **非幂等重试**：重复创建订单、重复扣款。写操作必须带幂等键或去重。
3. **重试不区分错误**：4xx（除 429/408）通常不该重试；连接失败、5xx、超时才适合。

### 49.6.2 跨服务一致性

Kubernetes 不提供分布式事务。常见做法：

- **Transactional Outbox**：业务写库和写 outbox 在同一事务；独立发布器把 outbox 投递到消息系统，消费者幂等处理。
- **CDC**：用 Debezium 等捕获数据库变更，避免应用双写；仍要处理 schema 演进和重复事件。
- **Saga**：把长事务拆成一组本地事务 + 补偿；补偿动作也必须幂等。
- **避免双写**：同时写数据库和消息队列，任何一步失败都会不一致；outbox/CDC 更可靠。

### 49.6.3 选主与分布式锁

需要单实例工作的场景（定时任务、缓存刷新、分区消费）常用 Lease 选主：

- 使用 `coordination.k8s.io/v1` Lease，续约周期远小于租期。
- 失去租约后立即停止工作，不要“再做完这一批”。
- 选主只保证“最多一个领导者”，不保证“恰好一次”；业务仍要幂等。
- 不要用“Pod 名字/节点名”做分布式锁，Pod 会被重建、名字会复用。

## 49.7 可观测性契约

应用要输出三类数据，并且能和 Kubernetes 元数据关联：

### 49.7.1 指标

- 入口：请求率、错误率、延迟分布（RED）。
- 资源：CPU、内存、GC、线程池、连接池（USE）。
- 业务：订单创建数、支付成功率、队列积压。
- 使用 Histogram 而不是平均值；桶要覆盖 SLO 边界。
- 控制标签基数：不要把用户 ID、请求 ID、完整 URL 放进标签。
- 暴露 `/metrics` 时限制访问，避免泄露内部信息。

### 49.7.2 日志

- 写 stdout/stderr，由平台采集；不要自己切割文件。
- 结构化 JSON，包含时间、级别、服务、实例、trace_id、request_id。
- 不打印密码、token、身份证、完整信用卡号；敏感字段脱敏。
- 错误日志带上下文和错误链，但避免把整个请求体打出来。
- 控制日志量：高频 debug 日志在线上默认关闭，按需动态开启。

### 49.7.3 追踪

- 使用 W3C `traceparent` 传播上下文；跨 HTTP、gRPC、消息队列都要传递。
- 异步消息用 span link 关联生产与消费，而不是强行做成父子 span。
- 采样策略要兼顾错误/慢请求（尾部采样）和成本。
- trace 要能跳到日志和指标；至少共享 trace_id 和资源属性。

### 49.7.4 SLO 与告警

应用团队应该定义 1–3 个用户可感知的 SLO，例如“99.9% 的结账请求在 800ms 内成功”。告警围绕**症状**（错误率、延迟、饱和度），而不是每个内部原因都告警。每个告警要绑定 runbook、负责人和降级开关。

## 49.8 本地开发与内循环

Kubernetes 开发最大的效率问题是“改一行代码要等镜像构建、推送、拉取、滚动更新”。内循环工具的目标是缩短这条链路。

| 工具/方式 | 作用 | 适合 | 代价 |
|---|---|---|---|
| Docker Compose | 本地起依赖 | 单服务/小团队 | 与集群环境有差异 |
| kind / k3d / minikube | 本地真实集群 | 验证清单、控制器、网络 | 资源占用、启动慢 |
| Skaffold | 构建 + 部署循环 | 标准 Kubernetes 开发 | 配置较多 |
| Tilt | 实时构建/日志/UI | 多服务开发 | 需要学习 Tiltfile |
| Garden | 端到端开发平台 | 大型多服务 | 重、学习成本 |
| Telepresence | 把本地进程接入集群网络 | 调试单个服务 | 网络拦截复杂 |
| mirrord | 把本地进程“注入”Pod 网络/文件系统 | 快速调试 | 权限与安全边界 |
| DevSpace | 开发/部署一体化 | 团队标准化 | 抽象层较多 |

一个务实的组合：

```text
单元测试 + Testcontainers（依赖）
  → kind/k3d + Skaffold/Tilt（清单与集成）
  → 远程开发集群 + mirrord/Telepresence（真实依赖调试）
  → staging（发布前验证）
```

常用命令：

```bash
kubectl port-forward svc/checkout 8080:80
kubectl logs -f deploy/checkout --tail=100
kubectl exec -it deploy/checkout -- sh
kubectl debug -it pod/checkout-xxxxx --image=nicolaka/netshoot --target=checkout
kubectl get events --sort-by=.lastTimestamp -A
```

开发集群的安全边界：

- 使用独立 namespace 和配额，避免一个开发者打爆集群。
- 不把生产 kubeconfig 给开发环境；用最小权限的 ServiceAccount。
- 开发集群可以放宽 PSA，但不能成为“验证生产安全策略”的环境。
- 定期清理遗留的 Deployment/PVC/LoadBalancer，避免持续计费。

## 49.9 构建、交付与升级兼容

### 49.9.1 镜像

```dockerfile
FROM golang:1.24 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/app ./cmd/app

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/app /app
USER nonroot:nonroot
ENTRYPOINT ["/app"]
```

要点：

- 多阶段构建，运行镜像不带编译器和源码。
- 非 root、只读根文件系统（可行时）、固定基础镜像 digest。
- 正确处理 PID 1 和信号；必要时用 `tini` 或应用自身处理 SIGTERM。
- 镜像要可复现：固定依赖版本、锁定 digest、记录 SBOM 和构建来源。
- 镜像越小，拉取和冷启动越快；但不要为了小而删掉调试所需的基础工具到无法排障。

### 49.9.2 数据库与 API 的滚动兼容

滚动更新期间新旧版本同时运行，数据库 schema 和 API 必须向前兼容。推荐 **expand/contract**：

1. **Expand**：新增字段/表/索引，旧代码忽略它。
2. **双写/回填**：新代码写新旧两处，后台回填历史数据。
3. **切换读**：新代码读新结构，旧代码仍能运行。
4. **Contract**：确认没有旧版本后，删除旧字段/旧代码。

反模式：

- 在每个 Pod 启动时跑迁移：多个副本并发执行，锁表和版本冲突。
- 迁移与代码发布强耦合：回滚代码后数据库已经不可逆。
- 直接重命名/删除列：旧版本 Pod 立刻报错。

迁移应由独立的 Job/Init Container 在发布前执行，并有锁、超时、回滚预案。大表变更要考虑在线 DDL 工具和锁等待。

### 49.9.3 发布策略与回滚

- 无状态服务优先滚动更新；需要精确流量控制时用 canary/蓝绿（第 32 章）。
- 回滚要能在一分钟内完成：镜像 digest、配置、数据库变更都要可回退。
- 发布前检查：资源余量、PDB、探针、迁移、依赖版本、特性开关。
- 发布后观察：错误率、延迟、重启、队列积压、业务指标；不要只看 Pod Ready。

## 49.10 迁移既有应用

### 49.10.1 就绪度评估

| 依赖 | 问题 | 迁移方案 |
|---|---|---|
| 本地磁盘 | 数据与实例绑定 | 对象存储/PVC/外部数据库 |
| 本地会话 | 实例重启丢失 | Redis/数据库/无状态 token |
| 固定 IP/主机名 | Pod IP 会变 | Service/DNS/服务发现 |
| 本地 cron | 多副本重复执行 | CronJob + 分布式锁/选主 |
| 本地文件上传 | 节点磁盘、扩容困难 | 对象存储 + 预签名 URL |
| 本地日志文件 | 采集困难 | stdout + 日志管道 |
| 硬编码配置 | 环境耦合 | ConfigMap/Secret/配置中心 |
| 内嵌数据库 | 数据持久性与备份 | 托管数据库/Operator |
| 本地缓存 | 多副本不一致 | 分布式缓存或本地缓存 + 失效策略 |
| 主机级 agent | 需要节点权限 | DaemonSet/特权评估 |

### 49.10.2 迁移模式

- **Strangler Fig**：新入口逐步把流量切到新服务，旧系统继续运行直到没有流量。
- **Adapter/Ambassador**：用边车或代理适配旧协议、旧认证、旧服务发现。
- **Shadow traffic**：复制生产流量到新服务，比较结果但不影响用户。
- **Canary**：按用户/流量比例逐步放量，观察 SLO 后全量。
- **Dual write**：过渡期同时写新旧存储，必须有对账和补偿。

### 49.10.3 迁移顺序

```text
无状态 Web/API
  → 定时任务/批处理
  → 内部工具
  → 文件/会话/缓存等有状态依赖
  → 数据库/消息等数据系统（Operator 或托管）
  → 核心交易（最后）
```

每步都要有回滚路径和验收指标。不要用“迁移完成”作为里程碑，用“旧路径流量为零且可回滚窗口结束”作为里程碑。

## 49.11 语言与运行时专项

| 运行时 | 信号/退出 | 内存配置 | 探针注意 | 常见坑 |
|---|---|---|---|---|
| Go | `signal.NotifyContext` + `http.Server.Shutdown` | `GOMEMLIMIT`、`GOGC`、`GOMAXPROCS` | 轻量 HTTP 探针 | 不感知 CPU quota；goroutine 泄漏 |
| Java | shutdown hook + Spring graceful | `MaxRAMPercentage`、Metaspace、DirectMemory | startup 给足 JIT/预热 | 堆外内存、GC 线程、线程池不关闭 |
| Python | 捕获 SIGTERM，停止 worker | worker 数 × 单 worker 内存 | 探针不要 fork 太重 | GIL、多 worker 内存翻倍、同步阻塞 |
| Node.js | `server.close` + 超时兜底 | `--max-old-space-size` | 事件循环阻塞会让探针超时 | keep-alive 连接、未捕获 Promise |
| .NET | `IHostApplicationLifetime` | GC heap hard limit、Server GC | 启动 JIT/EF 迁移 | 线程池饥饿、容器 CPU 感知 |

跨语言的共同原则：

- 把 SIGTERM 当作“请停止接新工作并完成收尾”，不是“立刻退出”。
- 退出码要能区分正常退出与失败；让重启策略和告警有意义。
- 不要在信号处理里做长时间阻塞操作；用超时和后台收尾。
- 记录启动/停止的关键事件，方便排障。

## 49.12 生产就绪清单

**生命周期**

- [ ] startup/readiness/liveness 语义分离，liveness 不查下游。
- [ ] SIGTERM 排空在途请求，preStop 覆盖摘流传播。
- [ ] grace period 覆盖 preStop + 排空 + 关闭资源。
- [ ] 长连接有上限和主动重连。

**配置与身份**

- [ ] 配置外置，启动时校验，非法配置快速失败。
- [ ] Secret 不进镜像，敏感值优先文件挂载，支持轮换。
- [ ] ServiceAccount 最小权限，不需要时关闭 automount。
- [ ] 云上使用工作负载身份而非静态密钥。

**资源与容量**

- [ ] requests/limits 基于压测，不是拍脑袋。
- [ ] 语言运行时内存配置与容器 limit 匹配。
- [ ] 有限流、有界队列、过载快速失败。
- [ ] HPA 指标代表真实饱和度，扩容延迟有兜底。

**韧性与数据**

- [ ] 远程调用有超时、重试预算、幂等。
- [ ] 跨服务一致性用 outbox/CDC/Saga，不依赖双写。
- [ ] 选主用 Lease，失去租约立即停止工作。
- [ ] 数据库迁移 expand/contract，独立 Job 执行。

**可观测性与交付**

- [ ] 结构化日志、RED 指标、trace 传播、SLO 告警。
- [ ] 镜像非 root、固定 digest、SBOM/签名。
- [ ] 发布可灰度、可回滚，迁移可回退。
- [ ] 有 runbook、降级开关和故障演练。

**反模式**

- 用 liveness 检查数据库，导致依赖故障时全量重启。
- 没有 readiness 就接流量，发布时把请求打到启动中的实例。
- 只写 limits 不写 requests，调度账本失真。
- 把配置写进镜像，靠重新构建改环境。
- 重试没有退避和幂等，故障时放大流量。
- 在 Pod 启动时跑数据库迁移，多副本并发锁表。
- 把 Kubernetes 重启当成业务重试，丢掉内存中的任务。

## 49.13 MiniK8s 对照

MiniK8s 可以观察应用契约中的一部分行为：

- 探针：HTTP/TCP 探针要求端口存在，exec 探针可通过命令失败模拟；`mini-k8s.io/simulate.probe-fail` 可强制失败（第 6.7 节）。
- 重启：liveness/startup 失败会重启容器，`restartCount` 累加；CrashLoopBackOff 的退避是模拟的。
- 资源：requests/limits 影响调度和 QoS；limits-only 会默认 request（第 7 章）。
- 配置：ConfigMap/Secret 挂载到容器的环境变量和文件；exec 会解析 env（第 11 章）。
- 终止：删除 Pod 会进入 Terminating，但 MiniK8s 不模拟真实 SIGTERM、连接排空和负载均衡摘流。

可以做的实验：

```bash
export MINIK8S_SERVER=http://127.0.0.1:8080
export MINIK8S_TOKEN=mini-admin-token
python -m mini_k8s.cli apply -f examples/15-probes.yaml
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli describe pod <pod-name>
```

把 readiness 探针改成必然失败，观察 Pod 是否从 Endpoints 摘除；把 liveness 改成失败，观察容器重启。然后思考：在真实集群里，这些行为还依赖哪些组件（kubelet、EndpointSlice 控制器、kube-proxy/Ingress、CNI）？

MiniK8s **不**验证真实进程信号、HTTP 连接排空、镜像构建和跨节点负载均衡。这些必须在 kind/k3d 或真实集群上做端到端验证（第 39.15 节、第 40 章）。

## 本章小结

- Kubernetes 对应用提出的核心要求是“可替换、可观测、可排空、可限流、可恢复”，不是会写 YAML。
- 三种探针职责不同：startup 管启动预算，readiness 管流量，liveness 只管进程是否卡死。
- 零停机的关键是摘流传播、preStop、SIGTERM 排空和足够的 grace period 四者配合。
- 配置与密钥要外置、校验、可轮换；身份优先使用短期凭证和工作负载身份。
- requests/limits 要基于压测；语言运行时内存配置必须与容器 limit 匹配。
- 韧性模式（超时、重试、幂等、熔断、限流、降级）是应用责任，Kubernetes 只提供重启和调度。
- 可观测性要在代码里设计：RED/USE 指标、结构化日志、trace 传播、SLO 告警。
- 本地内循环和迁移都要有明确的分层与回滚路径；不要用“能启动”作为生产就绪标准。
- MiniK8s 能验证探针、重启、资源和配置行为，但不能替代真实集群验证信号、网络和负载均衡。

## 延伸阅读

- [第 6 章 Pod 与生命周期](06-pod-lifecycle.md)：探针、重启、终止流程与生命周期钩子。
- [第 7 章 容器运行时、资源与 QoS](07-runtime-resources-qos.md)：requests/limits、CPU 节流、OOM 与 cgroups。
- [第 11 章 配置与密钥](11-configmap-secret.md)：ConfigMap/Secret、热更新、加密与 KMS。
- [第 20 章 自动伸缩](20-autoscaling.md)：HPA/VPA/KEDA 与应用指标设计。
- [第 27 章 可观测性](27-observability.md)：指标、日志、追踪、SLO 与生产栈。
- [第 32 章 GitOps 与渐进式交付](32-gitops-progressive-delivery.md)：发布、回滚、canary 与蓝绿。
- [第 50 章 事件驱动、工作流与数据管道](50-event-driven-workflows.md)：异步消费者、队列与工作流的工程细节。
- [附录 Q 扩展点与交付物开发实战](../appendices/Q-extension-development.md)：client-go/controller-runtime 与 Operator 工程骨架。
