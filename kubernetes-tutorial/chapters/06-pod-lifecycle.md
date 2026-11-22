# 第 6 章 Pod 与生命周期

## 6.1 Pod 是什么

Pod 是 Kubernetes 的最小调度和运行单元。它不是“一个容器”，而是：

- 一个或多个共享网络命名空间的容器。
- 可选的 init containers。
- 可选的 ephemeral containers（调试用）。
- 一组卷。
- 一个调度身份（节点、优先级、ServiceAccount、securityContext）。

同一个 Pod 内的容器：

- 共享 network namespace：同一个 IP、同一个端口空间，可用 `localhost` 互访。
- 可共享卷。
- 一起被调度、一起被删除（默认）。
- 可以有不同的镜像、命令、资源。

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: web
  labels: {app: web}
spec:
  containers:
    - name: app
      image: nginx:1.27
      ports: [{containerPort: 80}]
      resources:
        requests: {cpu: 100m, memory: 64Mi}
        limits: {cpu: 500m, memory: 256Mi}
    - name: log-shipper
      image: fluent-bit:3
      resources:
        requests: {cpu: 20m, memory: 32Mi}
  volumes:
    - name: logs
      emptyDir: {}
```

## 6.2 Pod 相位（phase）

`status.phase` 只有五个值：

| Phase | 含义 |
|---|---|
| Pending | 已创建，但尚未调度，或镜像/卷/运行时准备中 |
| Running | 至少一个容器在运行，或正在启动/重启 |
| Succeeded | 所有容器成功退出，且不会重启 |
| Failed | 至少一个容器失败退出，且不会重启 |
| Unknown | 与节点失联，状态未知 |

注意：

- Pending 不等于“调度失败”；也可能在拉镜像、挂卷。
- Running 不等于“Ready”；就绪由 conditions 表达。
- Failed 是终态，但 Job 可能创建新 Pod 重试。
- 节点失联时 phase 可能仍是 Running，但 Node 条件 NotReady。

## 6.3 Pod conditions

conditions 比 phase 更细：

- `PodScheduled`：已绑定节点。
- `Initialized`：所有 init containers 成功。
- `ContainersReady`：所有容器 Ready。
- `Ready`：Pod 可以接收流量（Readiness Gates 也满足）。
- `DisruptionTarget`：Pod 即将被驱逐（1.26+）。
- `PodReadyToStartContainers`（1.28 alpha / 1.29 beta / 1.37 GA）：sandbox 已创建、网络就绪，主容器可以开始启动。

MiniK8s 的 kubelet 写入这四个基本条件：

```json
{
  "type": "Ready",
  "status": "True",
  "reason": "PodReady",
  "lastTransitionTime": "..."
}
```

EndpointSlice 控制器维护后端的 `ready`/`serving`/`terminating` 条件：只有 Ready 的 Pod 才接收新流量；终止中的 Ready Pod 可能以 `terminating: true` 继续保留，直到摘流完成。排障时不要只看 Endpoints 的地址列表，要看 EndpointSlice conditions。

### Readiness Gates

有时“就绪”不只是容器进程能响应，还要等外部系统确认（例如负载均衡注册、证书下发、数据预热）。`spec.readinessGates` 让外部控制器参与 Ready 判定：

```yaml
spec:
  readinessGates:
    - conditionType: example.com/registered
```

Pod 的 `Ready` 只有在以下条件都满足时才为 True：

- 所有容器 Ready（readinessProbe 通过）。
- `readinessGates` 中列出的每个 condition 都存在于 `status.conditions` 且 `status: "True"`。

外部控制器负责写入该 condition：

```yaml
status:
  conditions:
    - type: example.com/registered
      status: "True"
      reason: Registered
      lastTransitionTime: "..."
```

注意：没有控制器写这个 condition 时，Pod 永远不会 Ready——Endpoints 会为空。Ready gate 是“额外的与门”，不是替代 readinessProbe。

## 6.4 容器状态

每个容器有：

- `waiting`：等待中，常见原因 `ContainerCreating`、`ImagePullBackOff`、`CrashLoopBackOff`。
- `running`：运行中，含 `startedAt`。
- `terminated`：已退出，含 `exitCode`、`reason`、`finishedAt`。

以及：

- `restartCount`：重启次数。
- `lastState`：上一次终止状态。
- `ready`：是否通过 readiness。
- `started`：是否通过 startup probe（1.20+）。

排查时不要只看 Pod phase，要看 containerStatuses：

```bash
kubectl get pod web -o jsonpath='{.status.containerStatuses[*].state}'
kubectl describe pod web
```

## 6.5 init containers

init containers 在主容器之前**顺序执行**，每个必须成功退出：

```yaml
spec:
  initContainers:
    - name: wait-db
      image: busybox
      command: ["sh", "-c", "until nc -z db 5432; do sleep 1; done"]
    - name: migrate
      image: app:migrate
  containers:
    - name: app
      image: app:1.0
```

用途：

- 等待依赖服务。
- 初始化配置、下载模型、跑数据库迁移。
- 设置卷权限。

特性：

- 顺序执行，前一个失败则重试（受 restartPolicy 影响）。
- 不支持 readiness/liveness probe（有 startupProbe 语义但不完全一样）。
- 资源请求不是简单相加：对每种资源，有效 request = `max(init 阶段峰值, 所有主容器 + 原生 sidecar 之和) + Pod overhead`。普通 init 容器顺序执行，所以取单个 init 的最大值；原生 sidecar（`restartPolicy: Always` 的 init 容器）会持续运行，既要计入 init 阶段峰值，也要计入稳态之和。
- 失败会导致 Pod 一直 Initializing，事件里能看到原因。

一个容易算错的例子：主容器 500m、普通 init 容器 200m、原生 sidecar 100m。有效 CPU request = `max(500m + 100m, 200m + 100m) = 600m`，而不是 `max(500m, 200m) = 500m`。如果 Pod 还声明了 `overhead`（RuntimeClass），再加到结果上。MiniK8s 的 `pod_requests` 只实现了普通 init 容器的 `max(sum(app), max(init))`，不建模原生 sidecar 的稳态占用。

MiniK8s 的 kubelet 在一个 tick 内完成所有 init 容器，并在 `initContainerStatuses` 中写入 terminated 状态。真实运行时会真正执行命令。

## 6.6 Sidecar：从“多容器”到原生 sidecar

传统 sidecar 是普通容器：

```yaml
containers:
  - name: app
  - name: envoy
```

问题：Job 的 Pod 中，sidecar 不会退出，导致 Job 永远不完成；终止顺序也不可控。

Kubernetes 1.28 引入 alpha、1.29 进入 beta 的**原生 sidecar**：它仍是 `initContainers` 中的条目，但带 `restartPolicy: Always`。顺序是：

1. 普通 init containers 依次执行。
2. 原生 sidecar 启动（在应用容器之前）。
3. 应用容器启动；sidecar 与应用容器并行运行。
4. 应用容器退出后，sidecar 按逆序终止。

因此它既解决了“Job 因 sidecar 不退出而永远不完成”的问题，也保留了 init 阶段的启动顺序。GA 版本与默认开启状态以目标集群为准。

```yaml
initContainers:
  - name: proxy
    image: envoy
    restartPolicy: Always
```

MiniK8s 未实现原生 sidecar，只有普通多容器。

## 6.7 探针

### startupProbe

应用启动慢时使用。启动成功前，liveness/readiness 不执行。失败超过阈值则重启容器。

```yaml
startupProbe:
  httpGet: {path: /healthz, port: 8080}
  failureThreshold: 30
  periodSeconds: 2
```

### readinessProbe

决定是否接收流量。失败不会重启容器，只会把 Pod 从 Endpoints 摘除。

```yaml
readinessProbe:
  httpGet: {path: /ready, port: 8080}
  initialDelaySeconds: 2
  periodSeconds: 5
  failureThreshold: 2
```

### livenessProbe

决定是否重启容器。失败超过阈值，kubelet 杀死容器并按 restartPolicy 重启。

```yaml
livenessProbe:
  exec:
    command: ["cat", "/tmp/healthy"]
  periodSeconds: 10
  failureThreshold: 3
```

### 常见错误

- 用 liveness 检查下游依赖：数据库一抖，所有 Pod 一起重启，雪崩。
- readiness 检查太严格：依赖未就绪导致所有实例被摘除。
- 没有 startupProbe，慢启动应用被 liveness 杀死。
- `initialDelaySeconds` 太长，故障发现慢；太短，误杀。
- HTTP 探针检查了错误端口/路径，容器永远不 Ready。

原则：

- liveness 只检查“进程是否还活着/是否死锁”，不检查外部依赖。
- readiness 检查“现在能否服务”，可以依赖关键下游，但要有降级策略。
- startup 给足启动时间，成功后再交给 liveness。

MiniK8s 的探针是模拟的：HTTP/TCP 探针要求声明的端口存在；exec 探针中命令含 `false` 则失败；`mini-k8s.io/simulate.probe-fail` 可强制失败。

## 6.8 restartPolicy

Pod 级设置：

- `Always`（默认）：容器退出就重启，适合长期服务。
- `OnFailure`：非零退出才重启。
- `Never`：不重启，适合一次性任务。

与控制器配合：

- Deployment/StatefulSet/DaemonSet 的 Pod 必须是 `Always`。
- Job 的 Pod 必须是 `Never` 或 `OnFailure`。

重启是**容器级**的，不是 Pod 级。Pod 名字、IP、节点通常不变；容器重新创建。`restartCount` 累加。

CrashLoopBackOff 是 kubelet 的指数退避：10s、20s、40s… 最大 5 分钟。它不是独立状态，而是 waiting reason。

## 6.9 终止流程

删除 Pod 时：

1. API Server 设置 `deletionTimestamp`，Pod 进入 Terminating。
2. kubelet 从 Endpoints 摘除（实际由 EndpointSlice 控制器处理）。
3. 执行 preStop hook（如有）。
4. 发送 SIGTERM 给容器主进程。
5. 等待 `terminationGracePeriodSeconds`（默认 30s）。
6. 未退出则 SIGKILL。
7. 容器停止，Pod 删除。

```yaml
spec:
  terminationGracePeriodSeconds: 60
  containers:
    - name: app
      lifecycle:
        preStop:
          exec:
            command: ["sh", "-c", "sleep 5; nginx -s quit"]
```

常见问题：

- 应用不处理 SIGTERM，硬等 30s。
- preStop 时间超过 grace period，被 SIGKILL。
- 负载均衡摘除慢于进程退出，造成 502。解决：preStop sleep + readiness 先失败 + 足够的优雅期。
- 优雅期太短，长连接被切断。

### 生命周期钩子：postStart 与 preStop

`lifecycle` 提供两个钩子：

```yaml
containers:
  - name: app
    lifecycle:
      postStart:
        exec:
          command: ["sh", "-c", "echo started >> /tmp/lifecycle"]
      preStop:
        httpGet:
          path: /drain
          port: 8080
```

`postStart`：

- 在容器创建后触发，但**与 ENTRYPOINT 并发**，不保证在应用主进程之前完成；不要用它做“必须完成才能启动”的初始化（用 init container）。
- 失败会杀死容器并按 restartPolicy 重启；没有重试次数配置。
- 每个容器实例执行一次；容器重启后会再次执行。

`preStop`：

- 在发送 SIGTERM **之前**同步执行；执行期间计入 `terminationGracePeriodSeconds`。
- 成功后发送 SIGTERM；失败也会继续终止流程（记录事件），不会无限阻塞。
- 常用于等待负载均衡摘流、关闭连接池、flush 缓冲。
- 不支持参数；`exec` 命令在容器内运行。

常见时序错误：

```text
preStop sleep 30s + 应用处理 SIGTERM 需要 20s
terminationGracePeriodSeconds = 30s
→ preStop 用完 30s，应用还没收到 SIGTERM 就被 SIGKILL
```

grace period 要覆盖 `preStop + 应用退出`，并留出余量。

## 6.10 调度门（scheduling gates）

Pod 可以带 `spec.schedulingGates`，在门移除前调度器不会调度：

```yaml
spec:
  schedulingGates:
    - name: example.com/wait-for-config
```

用于外部控制器先准备资源（例如动态分配设备、预拉镜像）。移除 gate 后 Pod 才进入调度队列。

## 6.11 静态 Pod 与镜像 Pod

**Static Pod** 由 kubelet 直接读取本地 manifest 启动，不经过 API Server 调度；API Server 中会出现一个 mirror Pod 用于观察，但不能通过 API 删除/修改。

常见于：

- kubeadm 控制面组件（kube-apiserver、etcd、controller-manager、scheduler）。
- 节点级 agent。

镜像 Pod 的名字通常带节点名后缀，owner 是 Node。排查控制面组件时，`kubectl get pods -n kube-system` 看到的就是 mirror Pod；真正 manifest 在 `/etc/kubernetes/manifests/`。

## 6.12 Ephemeral containers

临时调试容器：

```bash
kubectl debug -it pod/web --image=busybox --target=app
```

特点：

- 不能加资源限制、端口、探针。
- 不能删除后重建。
- 用于排障，不适合长期运行。
- 需要 RBAC 权限 `pods/ephemeralcontainers`。

MiniK8s 未实现，但支持 `exec` 模拟。

## 6.13 Pod 安全上下文

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 2000
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: app
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
```

这些字段直接影响容器逃逸风险。Pod Security Admission 的完整规则与运行时加固见第 25 章；准入链与供应链安全见第 24 章。

## 6.14 Pod 的资源与 QoS

Pod 的 QoS 类别（Guaranteed / Burstable / BestEffort）由 requests/limits 推导，并影响节点压力下的驱逐顺序。完整定义、CPU/内存/临时存储语义与 OOM 处理见第 7 章 7.4 与 7.6。与生命周期相关的只有三点：

- 完全没有 requests/limits → BestEffort，压力时最先被驱逐。
- liveness 重启不会改变 QoS。
- 原地调整 requests/limits 在 1.35 已 GA（`resize` 子资源，CPU/内存；依赖 cgroup v2 和运行时支持）。不支持原地调整的字段/运行时仍需重建 Pod；细节见第 7 章与[附录 R](../appendices/R-version-evolution-1.32-1.37.md) R.4.2。

## 6.15 用 MiniK8s 观察

```bash
python -m mini_k8s.cli run demo --image nginx:1.27 --port 80
python -m mini_k8s.cli get pod demo -o yaml
python -m mini_k8s.cli describe pod demo
python -m mini_k8s.cli logs demo
python -m mini_k8s.cli exec demo -- echo hello
```

一个带就绪延迟的 Pod：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: slow-ready
  annotations:
    mini-k8s.io/simulate.ready-after-ticks: "5"
spec:
  containers:
    - name: app
      image: demo
      readinessProbe:
        httpGet: {path: /ready, port: 8080}
```

观察 `status.conditions` 从 Ready=False 变为 True，Endpoints 在 Ready 后才收录。

## 6.16 Pod 内的共享与隔离细节

“同 Pod 共享网络命名空间”说起来简单，实际排障常被这些细节绊住：

- **pause 容器**：每个 Pod 有一个基础设施容器（pause/sandbox），持有 network namespace、Pod IP 和端口空间。业务容器加入该命名空间；pause 退出会导致整个 Pod 重建。
- **localhost 互通但有端口冲突**：同一 Pod 内两个容器不能监听同一个 `0.0.0.0:8080`；端口声明只是文档，不阻止绑定。
- **`/etc/hosts` 与 DNS**：Pod 的 `/etc/hosts` 包含 Pod 自身主机名、`hostAliases` 配置；DNS 由 `dnsPolicy` 决定，`hostNetwork: true` 的 Pod 默认继承节点 DNS，需显式 `ClusterFirstWithHostNet`。
- **共享卷不等于共享文件系统**：`emptyDir`、ConfigMap、Secret 可以同时挂到多个容器；`subPath` 会阻止更新传播（见第 11 章）。
- **`shareProcessNamespace: true`**：容器之间可以看到彼此的进程，常用于调试/信号转发；默认关闭，因为这会削弱隔离。
- **PID/IPC 命名空间**：默认每个容器独立 PID namespace；`hostPID`/`hostIPC` 会看到宿主机进程，属于高权限配置，PSA restricted 会拒绝。
- **securityContext 的作用域**：Pod 级设置作为容器默认值，容器级可以覆盖；`fsGroup` 影响卷的组所有权，对非 root 写卷很关键。

一个常见误解是“Pod 是一个轻量 VM”。它不是：Pod 只是共享部分命名空间的容器组，没有独立的 init、内核或网络栈（除了 pause 持有的 network namespace）。真正需要强隔离时，应使用 RuntimeClass（gVisor/Kata）或独立节点/集群。

MiniK8s 为每个 Pod 分配一个模拟 IP，并用 `podIP` 表达网络身份，不实现 pause/命名空间；这些细节在真实集群排障时才会显现。

## 本章小结

- Pod 是调度、网络、存储和生命周期的原子单位。
- phase 粗粒度，conditions 与 containerStatuses 才是排障关键。
- init containers 顺序执行；sidecar 有普通与原生两种模式。
- 探针决定流量与重启，设计错误会放大故障。
- 终止是“摘流 → preStop → SIGTERM → grace → SIGKILL”的协作过程。

## 延伸阅读

- [第 7 章 容器运行时、资源与 QoS](07-runtime-resources-qos.md)
- [第 13 章 Service、Endpoints 与 DNS](13-service-endpoints-dns.md)
- [第 37 章 Kubelet 与运行时模拟](37-kubelet-runtime.md)
- [第 49 章 应用工程](49-application-engineering.md)：探针预算、SIGTERM 排空与零停机发布。
- [附录 P 字段语义、默认值与 YAML 陷阱](../appendices/P-defaults-and-yaml-traps.md)：Pod/探针/资源的默认值与高频错误。
