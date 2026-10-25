# 第 58 章 Kubernetes 新原语与 KubeRay：原生 sidecar、调度门、拓扑感知与 Pod 级资源

KubeRay 不是一座孤岛。它生成的 Pod 最终要交给 kube-scheduler、kubelet、CNI、CSI 和容器运行时；Kubernetes 每引入一个新原语，KubeRay 的行为边界就会移动一点。

这一章挑出 2025–2026 年最值得关注的五个原语：

1. **原生 sidecar 容器**（Kubernetes 1.33 GA）；
2. **容器级重启策略**（Kubernetes 1.35，KubeRay `SidecarSubmitterRestart` 的基础）；
3. **Pod Scheduling Readiness / `schedulingGates`**（1.30 稳定）；
4. **拓扑感知路由**（`trafficDistribution: PreferSameZone`，1.31+）；
5. **Pod 级资源**（1.34 beta，1.36 原地扩缩 beta）。

它们分别回答了五个问题：sidecar 怎么启动/退出、失败容器怎么只重启自己、Pod 什么时候才该被调度、流量怎么留在本区、资源到底记在容器还是 Pod 上。每一个都会和 KubeRay 的某个字段或某个故障模式发生交互。

> 版本说明：Kubernetes 的小版本节奏约每四个月一次。本章的“1.3x”是能力引入的最低版本，具体 API 是否默认开启以目标集群的 feature gate 为准。KubeRay 基线仍是 v1.7.1。

## 58.1 一张版本矩阵

| 能力 | 引入 | 稳定/默认 | 对 KubeRay 的影响 |
|---|---|---|---|
| 原生 sidecar 容器 | 1.28 alpha | 1.29 beta，**1.33 GA** | 日志/指标/证书 sidecar 的启动顺序；RayJob SidecarMode |
| 容器级 `restartPolicy` | 1.35 | 1.35 | `SidecarSubmitterRestart`：只重启 submitter，不重启 head |
| `schedulingGates` | 1.26 alpha | 1.30 稳定 | 把 worker Pod 按住，直到配额/缓存/模型就绪 |
| `trafficDistribution` | 1.31 alpha | 1.33 GA（`PreferClose`）；`PreferSameZone` 1.34 beta、`PreferSameNode` 1.35 GA | Serve Service 的跨区流量与成本 |
| Pod 级 `resources` | 1.32 alpha | 1.34 beta | 调度记账单位变化；Ray 资源探测可能看不到 |
| Pod 级原地扩缩 | 1.36 beta | 1.36 beta | IPPR 目前只做容器级 |
| DRA（结构化参数） | 1.30 alpha | 1.34/1.35 GA | GPU/加速器申请；见第 36 章 |

矩阵的用法不是“版本越高越好”，而是：**升级集群前，先确认 KubeRay 生成的 Pod 在新版本里的默认行为是否改变**。

## 58.2 原生 sidecar 容器

### 58.2.1 它解决的两个老问题

在 1.33 之前，sidecar 就是一个普通容器：

```yaml
spec:
  containers:
    - name: ray
      image: rayproject/ray:2.59.0
    - name: log-shipper
      image: fluent-bit:latest
```

这带来两个竞态：

1. **启动竞态**：`log-shipper` 可能比 `ray` 晚启动，Ray 早期的日志丢失；
2. **退出竞态**：Pod 终止时 `ray` 和 `log-shipper` 同时收到 SIGTERM，`ray` 的最后一批日志可能来不及被采集。

原生 sidecar 把 sidecar 放进 `initContainers`，但给它 `restartPolicy: Always`：

```yaml
spec:
  initContainers:
    - name: log-shipper
      image: fluent-bit:latest
      restartPolicy: Always          # 关键：sidecar 语义
      volumeMounts:
        - {name: logs, mountPath: /var/log/ray}
  containers:
    - name: ray
      image: rayproject/ray:2.59.0
      volumeMounts:
        - {name: logs, mountPath: /var/log/ray}
```

### 58.2.2 生命周期语义

启动：

```text
initContainers 按顺序启动
  ├─ 普通 init container：运行到退出
  └─ 原生 sidecar：启动后保持运行，readinessProbe 通过才继续
containers 按顺序启动（Ray 是 containers[0]）
```

- sidecar 的 `readinessProbe` **参与 Pod 的 Ready 判定**；sidecar 没 ready，Pod 不会 Ready；
- sidecar 可以放在普通 init container 之间，形成“初始化 -> 代理就绪 -> 再初始化 -> 主容器”的链；
- Pod 级 `restartPolicy` 不作用于 sidecar；sidecar 只看自己的 `initContainers[].restartPolicy: Always`。

终止：

```text
1. 主容器（ray）收到 SIGTERM，优雅退出
2. 主容器全部退出后，sidecar 按出现顺序的逆序停止
3. sidecar 退出，Pod 结束
```

这个顺序意味着：**日志采集器比 Ray 活得久**，可以把 Ray 退出前的日志刷出去。这正是第 52 章讲的优雅终止想要的。

### 58.2.3 Job 完成语义

普通容器形式的 sidecar 有一个经典问题：主容器退出后，sidecar 还在跑，Job 永远不会 `Complete`。原生 sidecar 修好了这一点：

- 主容器退出 -> Job 可以完成；
- sidecar 不阻塞 Job 的完成判定；
- kubelet 仍然按“先主后 sidecar”的顺序清理。

这对 RayJob 的 `K8sJobMode`/`SidecarMode` 都有意义：submitter 退出后，日志/指标 sidecar 不会把 Job 卡住。

### 58.2.4 在 Ray Pod 里怎么用

KubeRay 会把用户模板里的 `initContainers` 原样带进 Ray Pod。因此可以给 head/worker 加：

- 日志采集器（fluent-bit、vector、otel-collector）；
- 证书/密钥注入代理；
- 模型/数据预热器；
- 网络代理（服务网格的 ambient/istio-init 另说）。

一个 worker 模板示例：

```yaml
workerGroupSpecs:
  - groupName: workers
    replicas: 2
    template:
      spec:
        initContainers:
          - name: log-shipper
            image: fluent/fluent-bit:3.1
            restartPolicy: Always
            resources:
              requests: {cpu: 50m, memory: 64Mi}
            volumeMounts:
              - {name: ray-logs, mountPath: /var/log/ray}
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests: {cpu: "2", memory: 4Gi}
            volumeMounts:
              - {name: ray-logs, mountPath: /var/log/ray}
        volumes:
          - name: ray-logs
            emptyDir: {}
```

注意：

- sidecar 的 requests 会计入 Pod 的总 requests，影响调度与状态统计；但 Ray 的逻辑容量只看 Ray 容器（MiniKubeRay 的 `ray_group_capacity` 也是这么做的），不要把 sidecar 的 CPU 当成 Ray 可调度容量；
- sidecar 的 readiness 会推迟 Pod Ready，进而推迟 `RayCluster` 的 Ready condition；
- sidecar 镜像要能离线拉取，否则整个 Ray Pod 起不来；
- sidecar 如果崩溃，kubelet 会按 `restartPolicy: Always` 重启它；这不会重启 Ray 容器，但会让 Pod 的 Ready 抖动。

### 58.2.5 原生 sidecar 与 KubeRay 的 `SidecarMode`

名字很像，但不是一回事：

| | 原生 sidecar | KubeRay `SidecarMode` |
|---|---|---|
| 位置 | `initContainers` + `restartPolicy: Always` | head Pod 的普通 `containers` |
| 作用 | 日志/代理/初始化 | 在 head 本地提交 Ray Job |
| 生命周期 | 跟 Pod 同生共死 | 跟 head Pod 同生共死 |
| 启动顺序 | 受 init 顺序约束 | 与 head 容器并行 |
| 失败影响 | 只影响自己 | 可能导致 RayJob 失败/重试 |

KubeRay 的 `ray-job-submitter` 目前是普通容器，不是原生 sidecar。它的重启由 `SidecarSubmitterRestart` 控制（下一节）。

### 58.2.6 原生 sidecar 的坑

1. **原生 sidecar 的 `restartPolicy: Always` 必须写在 `initContainers` 上**。Kubernetes 1.35 起普通 `containers` 也可以设置容器级 `restartPolicy`（`ContainerRestartRules` beta，支持 `Always`/`OnFailure`/`Never` 与退出码规则），但那是“只重启这个容器”的语义，不具备 init 容器的启动顺序保证；不要把两者混为一谈。
2. **sidecar 必须自己处理 SIGTERM**；如果它忽略信号，kubelet 会在 `terminationGracePeriodSeconds` 后 SIGKILL。
3. **sidecar 的 readinessProbe 不是可选的**：没有 probe 时，kubelet 认为容器一启动就 Ready，启动竞态又回来了。
4. **sidecar 不能依赖主容器的文件**：启动顺序是 sidecar 先于主容器，主容器还没写任何东西。
5. **Job 的 `backoffLimit` 不会因为 sidecar 重启而增加**；sidecar 的失败不计入 Job 的容器失败（它是 init container 语义）。
6. **监控要区分 sidecar 的重启和主容器的重启**；否则会把日志采集器的抖动误判为 Ray 崩溃。

## 58.3 容器级重启策略与 `SidecarSubmitterRestart`

### 58.3.1 为什么需要“只重启 submitter”

RayJob 的 `SidecarMode` 把 submitter 放进 head Pod。如果 submitter 因为网络抖动/临时错误退出：

- 传统做法：整个 RayJob 失败 -> 重试整个作业；
- 理想做法：只重启 submitter 容器，Ray head 和正在运行的 job 不受影响。

Kubernetes 1.35 的容器级 `restartPolicy` 让这件事成为可能：

```yaml
containers:
  - name: ray-job-submitter
    image: rayproject/ray:2.59.0
    restartPolicy: OnFailure       # 只作用于这个容器
```

KubeRay v1.7 的 `SidecarSubmitterRestart` gate（alpha，默认关闭）正是用它实现：

- 需要 KubeRay v1.7+、Ray 2.54+、**Kubernetes 1.35+**；
- submitter 非零退出 -> 只重启 submitter；
- 重启后先查询 Ray job 状态：如果 job 还在跑，重新 attach 日志流，而不是重复提交；
- `submitterConfig.backoffLimit`（默认 2）限制重启次数；SidecarMode 下该字段不能被用户覆盖（webhook 会拒绝）。

### 58.3.2 版本偏差是最危险的地方

官方文档特别提醒：**head Pod 所在的 kubelet 也必须是 1.35+**。如果控制面是 1.35 而节点 kubelet 是 1.34：

- API Server 接受 container-level `restartPolicy`；
- kubelet 静默忽略它；
- submitter 退出后不会重启；
- operator 的 30 秒 submitter-finished 超时可能把 RayJob 标成 `Failed`，而 Ray job 其实还在运行。

这类“控制面接受、数据面忽略”的偏差，比直接报错更难排查。上线前用：

```bash
kubectl version --short
kubectl get nodes -o custom-columns=NAME:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion
```

确认所有可能运行 head Pod 的节点都满足版本要求。

### 58.3.3 什么时候不该用

- Ray 作业本身失败（entrypoint 非零退出）不会触发容器重启；它会让 RayJob 按正常失败路径走；
- submitter 重启不等于作业重跑；如果你的 entrypoint 有副作用，必须自己保证幂等（第 10.13 节）；
- 不要用它替代 `backoffLimit` 的作业级重试；两者解决的是不同层的问题。

## 58.4 Pod Scheduling Readiness 与 `schedulingGates`

### 58.4.1 语义

在 Pod spec 里加：

```yaml
spec:
  schedulingGates:
    - name: example.com/wait-for-model-cache
```

效果：

- Pod 被创建，但 **kube-scheduler 完全忽略它**；
- Pod 停在 `Pending`，condition 为 `PodScheduled=False`、reason `SchedulingGated`；
- 不占节点资源，不参与抢占，**Cluster Autoscaler 也不会为它扩容**；
- 移除 gate 后，Pod 才进入正常的调度队列。

关键限制：

- **gate 只能在创建时加**，创建后不能新增；
- 移除 gate 用普通 patch/update；
- gate 名称是任意字符串，但建议用域名前缀避免冲突；
- 如果所有 gate 都被移除，Pod 不会自动“重新排队”到队首，仍然按优先级正常调度。

### 58.4.2 典型用途

1. **等待配额/队列准入**：Kueue 用 gate 把 Pod 按住，直到 Workload 被 admit；
2. **等待镜像/模型缓存**：先让 DaemonSet 把 20GB 模型拉到节点，再放行 Pod；
3. **等待对象存储挂载**：CSI 卷还没 attach 时不要让调度器反复重试；
4. **等待外部批处理窗口**：只在夜间放行训练 Pod；
5. **等待网络策略/证书**：sidecar 依赖的 Secret 还没就绪。

Kueue 是最典型的例子：它不直接创建 Pod，而是给 Pod 加 `kueue.x-k8s.io/admission` gate；Workload 被 admit 后由 Kueue 移除 gate。**不要手工管理 Kueue 的 gate**，否则会和它的状态机打架。

### 58.4.3 在 KubeRay 里怎么用

KubeRay 把用户模板原样传给 Pod，因此可以在 worker/head 模板里写 `schedulingGates`：

```yaml
workerGroupSpecs:
  - groupName: gpu-workers
    replicas: 2
    template:
      spec:
        schedulingGates:
          - name: example.com/wait-for-model-cache
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
```

但要非常小心它和 autoscaler 的交互：

- Ray autoscaler 判断“节点是否可用”看的是 **Raylet 是否注册**，不是 Pod 是否 Pending；
- 被 gate 住的 Pod 不会注册 Raylet；
- autoscaler 会认为 demand 仍然未满足，可能继续把 `replicas` 加到 `maxReplicas`；
- 直到 gate 被移除、Pod 启动、Raylet 注册，扩容才“看起来生效”。

因此：

- gate 的移除必须有确定的责任人（平台控制器/运维脚本），不能靠人记得；
- 在 gate 期间观察 `RayCluster.status.desiredWorkerReplicas` 与 `readyWorkerReplicas` 的差距；
- 不要把 gate 当作“暂停”用；暂停 RayCluster 用 `spec.suspend`（第 7 章）。

### 58.4.4 排查

```bash
kubectl get pod <pod> -o jsonpath='{.spec.schedulingGates}'
kubectl get pod <pod> -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")]}'
kubectl describe pod <pod> | grep -A3 SchedulingGated
```

如果 Pod 一直 Pending 且没有任何调度事件，先看 gate，再看资源。

## 58.5 拓扑感知路由

### 58.5.1 Service 的三种流量策略

Kubernetes Service 默认把流量均匀分到所有 Ready 的 Endpoint，可能跨可用区。三种控制方式：

| 机制 | 作用 | 适用 |
|---|---|---|
| `internalTrafficPolicy: Cluster` | 所有 Endpoint 都可能 | 默认 |
| `internalTrafficPolicy: Local` | 只发给本节点的 Endpoint | 本节点有端点时；没有则流量黑洞 |
| `trafficDistribution: PreferSameZone` | 优先本区，没本区再回退 | 多 AZ、跨区成本敏感 |

`trafficDistribution` 在 1.31 引入、1.33 GA；最初的取值是 `PreferClose`，1.34 起被标记为 deprecated，改用语义更清楚的 `PreferSameZone`（行为等价）或 `PreferSameNode`（1.35 GA，优先同节点）。新集群应直接写 `PreferSameZone`，老清单里的 `PreferClose` 仍然工作但会有弃用告警：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: demo-serve-svc
spec:
  type: ClusterIP
  selector:
    ray.io/serve: "true"
    ray.io/cluster: demo
  ports:
    - name: serve
      port: 8000
      targetPort: 8000
  trafficDistribution: PreferSameZone
```

它的数据面依赖 EndpointSlice 的 zone hints：kube-proxy 优先选择与客户端同区的 Endpoint；如果本区没有 Ready 的 Endpoint，回退到全部。

### 58.5.2 对 Ray 的两个 Service 的不同影响

RayCluster 有两个关键 Service：

1. **head Service（headless）**：worker 通过 DNS 找 head；
2. **Serve Service（ClusterIP）**：外部流量进 Ray Serve。

headless Service（`clusterIP: None`）**不经过 kube-proxy 负载均衡**，DNS 直接返回所有 Pod IP。`trafficDistribution` 对 headless 的 DNS 结果没有意义；worker 会拿到所有 head/worker 地址。Ray 自己的连接逻辑决定连哪个。

Serve Service 是 ClusterIP，`PreferSameZone` 能把推理请求留在本区：

- 降低跨 AZ 延迟；
- 降低跨 AZ 流量费；
- 但可能造成本区过载/其他区空闲，需要结合副本打散（第 44 章）。

### 58.5.3 `Local` 的陷阱

`internalTrafficPolicy: Local` 听起来更省，但它没有回退：

- 本节点没有 Ready Endpoint 时，流量被丢弃（connection refused/timeout）；
- 节点 drain/重启时，Service 不会把流量转到其他节点；
- 对 Ray Serve 这种长连接/流式响应，表现是请求直接失败。

生产上优先用 `PreferSameZone`（或 1.35+ 的 `PreferSameNode`），不要用 `Local`，除非你明确知道每个节点都有本地副本。

### 58.5.4 与 Gateway API 的关系

RayService 的增量升级用 Gateway API 做加权路由（第 11、33 章）。Gateway 实现自己的负载均衡策略，`trafficDistribution` 只作用于 Service，不直接作用于 HTTPRoute。要在 Gateway 层做区域亲和，需要看具体实现（Envoy Gateway 的 zone-aware routing、Istio 的 locality load balancing 等）。

### 58.5.5 观测

```bash
# EndpointSlice 的 zone hints
kubectl get endpointslice -l kubernetes.io/service-name=demo-serve-svc -o yaml

# 每个 Endpoint 的 zone
kubectl get pods -l ray.io/serve=true -o custom-columns=NAME:.metadata.name,ZONE:.spec.nodeName
```

用实际请求的延迟分布验证，而不是只看配置。

## 58.6 Pod 级资源

### 58.6.1 语义

Kubernetes 1.34 起，Pod 可以声明 Pod 级资源：

```yaml
spec:
  resources:
    requests: {cpu: "4", memory: 8Gi}
    limits:   {cpu: "4", memory: 8Gi}
  containers:
    - name: ray
      image: rayproject/ray:2.59.0
      # 不写 requests/limits，继承 Pod 级
    - name: log-shipper
      image: fluent-bit:3.1
      # 也不写
```

规则：

- 容器自己写了 requests/limits，就以容器的为准；
- 容器没写，就用 Pod 级的；
- 调度器按 Pod 级资源记账；
- 1.36 起 Pod 级资源也支持原地扩缩（beta）。

### 58.6.2 对 KubeRay 的影响

KubeRay 把模板原样传递，所以 Pod 级资源能写进去。但有两个坑：

1. **Ray 的资源探测**：Raylet 启动时通常读取 Ray 容器的 cgroup/requests 来推导 `num-cpus`/`memory`。如果只写 Pod 级 `resources`、Ray 容器没有 requests，Ray 可能看到 0 CPU，导致 Pod 起来了但 Ray 不调度任何 task。上线前用 `ray status` 验证；保守做法是**容器级和 Pod 级都写**。
2. **IPPR 的校验**：IPPR 要求 Ray 容器同时有 CPU/memory requests（第 57.3.2 节）。只写 Pod 级资源的模板无法通过 IPPR 校验。两者不要混用，除非你确认 Ray 版本支持 Pod 级探测。

### 58.6.3 什么时候值得用

- Pod 里有多个容器，想按整体预留资源，而不是每个容器各自预留；
- 想让 sidecar 的 requests 不单独参与调度（由 Pod 级统一记账）；
- 想用 1.36 的 Pod 级原地扩缩做整 Pod 的垂直调整。

对 Ray 来说，**最稳的写法仍然是 Ray 容器显式 requests**，因为 Ray 的资源模型、autoscaler 和 IPPR 都围绕容器 requests 构建。Pod 级资源是“调度层”的能力，不是“Ray 层”的替代。

## 58.7 DRA 与其他 Job 原语

### 58.7.1 DRA

第 36 章详细讲了 Dynamic Resource Allocation。这里只补一个和新版本相关的点：

- DRA 的 `resource.k8s.io/v1` 在 1.34/1.35 才稳定；
- `v1beta1` 与 `v1` 的字段形状不同（`exactly` 等）；
- KubeRay 只负责把 `ResourceClaimTemplate`/`ResourceClaim` 放进 Pod 模板，不负责设备分配；
- Ray 侧仍要显式声明 GPU 数量，否则 Raylet 看不到加速器。

### 58.7.2 Job 的 `podFailurePolicy` / `successPolicy`

KubeRay 的 `K8sJobMode` 会创建一个 Kubernetes Job 作为 submitter。Kubernetes 1.31+ 的 `podFailurePolicy` 可以：

- 按退出码决定“重试还是失败”；
- 忽略某些可容忍的失败（例如 Spot 中断）；
- 与 `backoffLimit` 组合。

但 KubeRay 的 RayJob API **没有直接暴露** `podFailurePolicy`；submitter Job 的策略由 operator 决定。如果确实需要，只能在 fork 的 operator 里加字段（第 38 章），或者改用 `HTTPMode`/`SidecarMode` 自行控制重试。

### 58.7.3 `terminationGracePeriodSeconds` 与 `preStop`

第 52 章已经展开。和本章的关系是：原生 sidecar 的退出顺序让 `preStop` 的预算更容易算——主容器先退，sidecar 后退；`terminationGracePeriodSeconds` 要覆盖两者的总和。

## 58.8 组合矩阵：哪些能一起用

| 组合 | 是否推荐 | 原因 |
|---|---|---|
| 原生 sidecar + RayCluster | 推荐 | 日志/指标代理顺序可控；注意资源与 readiness |
| 原生 sidecar + IPPR | 谨慎 | sidecar 的资源不参与 IPPR；IPPR 只改 Ray 容器；sidecar 可能成为瓶颈 |
| `schedulingGates` + autoscaler | 谨慎 | 被 gate 的 Pod 不注册 Raylet，autoscaler 可能持续扩容 |
| `trafficDistribution` + RayService | 推荐 | 降低跨区延迟/成本；配合副本打散 |
| Pod 级资源 + IPPR | 不推荐 | IPPR 要求容器 requests；Ray 探测可能看不到 Pod 级 |
| Pod 级资源 + 多 sidecar | 推荐 | 统一记账，避免 sidecar 各自预留 |
| `SidecarSubmitterRestart` + 1.35 之前 kubelet | 禁止 | 控制面接受、数据面忽略，RayJob 会被误判失败 |

## 58.9 实验：把原语语义跑成断言

```bash
python examples/k8s_primitives_lab.py
```

预期输出：

```text
1 sidecar-start order=['fetch-model', 'log-shipper', 'otel-collector', 'ray'] main_running=True
2 sidecar-stop  order=['ray', 'otel-collector', 'log-shipper']
3 job-complete  completed=True sidecar_still_running=False
4 scheduling-gate gated_scheduled=False phase_while_gated=Pending after_removal=True phase_after=Running
5 routing       Cluster=['10.0.1.5', '10.0.1.6', '10.0.2.7']
                PreferSameZone(zone-a)=['10.0.1.5', '10.0.1.6'] fallback=['10.0.1.5', '10.0.1.6', '10.0.2.7'] Local=['10.0.1.5', '10.0.1.6']
6 pod-resources pod_only_scheduler_cpu=4 pod_only_ray_sees=0 mixed_scheduler_cpu=1 mixed_ray_sees=1
OK: sidecars ordered correctly, Jobs completed, gates blocked scheduling, PreferSameZone stayed in-zone, and Pod-level resources were visible to the scheduler but not to Ray's container-based detection.
```

每个断言证明什么：

1. **启动顺序**：普通 init container 先跑完，sidecar 依次启动并 ready，最后才是 Ray；
2. **退出顺序**：Ray 先退，sidecar 逆序退出，日志采集器有机会 flush；
3. **Job 完成**：sidecar 不阻塞 Job；
4. **调度门**：gate 存在时调度器不绑定；移除后才进入 Running；
5. **路由**：`PreferSameZone` 只选本区 Ready 端点，本区没有时回退；`Local` 不回退；
6. **资源记账**：Pod 级资源对调度器可见，但 Ray 的容器级探测看不到——这就是“能调度不等于 Ray 能用”。

对应的 pytest：

```bash
python -m pytest tests/test_examples.py -q
```

### 58.9.1 自己改一改

1. 把 `log-shipper` 的 readiness 设为 `False`：主容器还会启动吗？Pod 会 Ready 吗？
2. 给 `GatedPod` 加第二个 gate：需要移除几次才能调度？
3. 把 `PreferSameZone` 的客户端 zone 改成 `zone-c`：为什么回退到全部端点？
4. 把 Pod 级资源改成容器级：`ray_sees` 是否变成 4？
5. 把 sidecar 的资源 requests 加到 2 CPU：Ray Pod 的总 requests 变成多少？调度会不会失败？

## 58.10 练习题

1. **原生 sidecar 与 Ray 的优雅终止**：Ray 的 drain 需要 30 秒，log-shipper 需要 10 秒。`terminationGracePeriodSeconds` 至少是多少？如果 sidecar 的 `preStop` 也执行，预算怎么加？
2. **`SidecarSubmitterRestart` 的版本偏差**：控制面 1.35、节点 1.34 时，RayJob 会表现成什么？如何用 `kubectl version` 和事件快速定位？
3. **调度门与 autoscaler**：一个 10 副本的 group 被 gate 住 5 分钟，Ray autoscaler 会做什么？如何避免把 `maxReplicas` 打满？
4. **拓扑感知路由**：为什么 headless Service 不受 `trafficDistribution` 影响？worker 加入 head 的流量是否需要担心跨区成本？
5. **Pod 级资源与 Ray**：设计一个实验，验证你使用的 Ray 版本是否能从 Pod 级 `resources` 推导 `num-cpus`。如果看不到，最小的修复是什么？
6. **组合设计**：一个多 AZ 的 LLM 推理平台，要求同区优先、sidecar 收集日志、支持 IPPR。画出 Pod 模板和 Service 配置，并列出三个最重要的验证项。
7. **故障注入**：在 `KubeRay` 的 e2e 测试里，如何模拟“gate 永远不被移除”？应该断言什么 condition/事件？

## 58.11 小结

- 原生 sidecar 把“启动顺序、退出顺序、Job 完成”三件事标准化；KubeRay 的模板可以直接使用。
- `SidecarSubmitterRestart` 依赖 1.35 的容器级重启；版本偏差会让失败静默。
- `schedulingGates` 是平台控制器的工具，不是 Ray autoscaler 的暂停键；被 gate 的 Pod 不会注册 Raylet。
- `PreferSameZone` 适合多 AZ 的 Serve 流量；`Local` 没有回退，慎用。
- Pod 级资源改变调度记账，但不会自动改变 Ray 的资源探测；Ray 场景优先写容器 requests。
- 新原语的价值不在“版本号”，而在它们和 KubeRay 状态机的交互。每一个都要用实验/断言验证，而不是假设。

到这里，教程的主线结束。回看整张地图：

- 第 1–18 章建立 KubeRay 的模型、CRD、工作负载与生产运维；
- 第 19–25 章用一个可运行的 MiniKubeRay 把控制平面拆开；
- 第 26–44 章进入真实集群、生态、网络、规模、多 AZ 与灾备；
- 第 45–56 章深入数据、训练、推理、可观测性、治理、弹性、生命周期与诊断；
- 第 57–58 章补齐 2026 年的两个新方向：垂直弹性（IPPR）与 Kubernetes 新原语。

如果只想记住一句话：**KubeRay 的复杂性不在 YAML，而在“控制面状态机 + Kubernetes 数据面语义 + Ray 运行时语义”三者的接缝处。** 本教程的每一章、每个实验、每个测试，都是在把某一条接缝摊开给你看。

### 继续深入

- 想跑真实集群：从[第 26 章](kuberay教程-26-真实集群实验-从kind到第一个RayService.md)开始；
- 想改 operator：从[第 30 章](kuberay教程-30-KubeRay源码导读与二次开发.md)、[第 38 章](kuberay教程-38-从MiniKubeRay到生产Operator-移植与二次开发.md)开始；
- 想排障：从[第 56 章](kuberay教程-56-Ray活体诊断-State-API、ray-CLI与证据链.md)和[附录 C](kuberay教程-61-附录C-故障排查手册.md)开始；
- 想验证本教程：从[附录 K](kuberay教程-69-附录K-可运行实验与验证索引.md)开始，五分钟跑完全量检查；
- 想跟进上游：读[第 43 章](kuberay教程-43-从Issue到Release-KubeRay贡献、API治理与发布工程.md)，关注 KubeRay release notes 与 KEP/REP。
