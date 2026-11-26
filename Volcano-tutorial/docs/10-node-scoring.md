# 第 10 章 节点打分与装箱

## 10.1 两阶段：Predicate 与 Score

为 Pod 选节点分两步：

1. **Predicate（硬过滤）**：不满足条件的节点直接排除。
2. **NodeOrder（软打分）**：在所有可行节点中选分数最高的。

Volcano 的 predicates 插件负责常见的过滤：

- 节点是否 Ready/可调度；
- 污点与 toleration；
- nodeSelector；
- 资源是否足够（由框架本身也会检查）；
- 卷/端口等（取决于版本与插件）。

NodeOrder 由多个插件共同贡献，分数相加。

## 10.2 nodeorder：默认的“选空闲”

nodeorder 通常打三类分：

- **least requested**：节点越空，分越高；
- **balanced**：各维度利用率越均衡，分越高；
- **pod count**：节点上 Pod 越少，分越高（避免单节点 Pod 过多）。

伪代码：

```text
score = w1 * (1 - avg(used/cap))
      + w2 * (1 - stddev(used/cap))
      + w3 * (1 - min(podCount/100, 1))
```

它倾向于“打散”，适合无状态服务/推理副本，能降低单节点故障的影响。

## 10.3 binpack：尽量装满

binpack 的逻辑相反：节点越忙，分越高。

```text
score = weight * 10 * max((used + request) / capacity)
```

适合：

- 训练作业：把 GPU 聚拢，减少碎片；
- 需要连续资源的 gang：先把一个节点/机架填满，留出完整节点给大作业；
- 成本敏感：让空闲节点真正空闲，便于缩容。

配置：

```yaml
- name: binpack
  arguments:
    binpack.weight: "10"
```

`binpack.weight` 越大，越倾向于装箱。它与 nodeorder 同时存在时，两者分数相加；如果 nodeorder 的“选空闲”分数压过 binpack，装箱效果会被抵消。要么调大 binpack 权重，要么去掉 nodeorder。

## 10.4 resource-strategy-fit（v1.13+）

resource-strategy-fit 允许为不同资源指定不同策略。例如：

- GPU 装箱（减少碎片）；
- CPU 打散（避免单节点过热）；
- 内存装箱。

它不是简单的“全局 binpack”，而是按资源维度组合策略。具体参数随版本变化，核心思想是：**不同资源的最优放置方向可能相反**。

## 10.5 usage：按真实利用率打分

前面所有打分都基于 **requests**，而不是实际用量。一个 Pod request 了 8 CPU 却只用 1 CPU，nodeorder 仍然认为节点“很忙”。`usage` 插件补上这一层：它从 metrics-server 或 Prometheus 读取节点的真实使用率，参与 Predicate 与 NodeOrder。

### 10.5.1 它注册什么

- `PredicateFn`：节点 CPU/内存使用率超过阈值时，直接排除；
- `NodeOrderFn`：使用率越低，分数越高（`weight` 控制权重）。

配置示意：

```yaml
- name: usage
  enablePredicate: true          # plugin option；false 时只打分不阻断
  arguments:
    usage.weight: "5"
    cpu.weight: "1"
    memory.weight: "1"
    thresholds:
      cpu: 80
      mem: 80
```

语义：

- `thresholds.*` 是硬阈值，超过就不放新 Pod；
- `usage.weight` 影响软打分，不会单独决定放置；
- 没有指标/指标过期的节点，行为取决于插件实现，通常是跳过或按 0 处理，必须显式验证。

### 10.5.2 与 request-based 打分的冲突

usage 和 nodeorder/binpack 的方向可能相反：

- nodeorder 的 least-requested 认为“request 少”的节点更空；
- usage 认为“实际使用率低”的节点更空；
- 一个节点可能 request 很高但实际很闲（超卖），也可能 request 很低但实际很忙（噪声邻居）。

同时启用时，要明确谁主导：

- 如果目标是**避免热点**，usage 权重要高；
- 如果目标是**装箱/碎片**，binpack 权重要高；
- 如果目标是**隔离**，不要靠打分，直接分节点池。

### 10.5.3 适用与不适用

适合：

- 混部集群，在线服务实际用量与 request 差异大；
- 推理节点，GPU/CPU 利用率波动明显；
- 需要避免“request 很低但实际过载”的节点。

不适合：

- 指标链路不可靠（metrics-server 缺失/延迟高）；
- 对冷启动敏感的批作业——新节点没有历史指标；
- 需要确定性复现的测试环境。

### 10.5.4 排障

```bash
kubectl top nodes
kubectl get --raw /apis/metrics.k8s.io/v1beta1/nodes | head
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i usage
```

现象：作业突然大量 Pending，节点却看起来很空。先确认 usage 插件是否读到过期指标，或阈值是否把节点全部排除。

## 10.6 NUMA-aware

NUMA 节点内存在“本地内存”和“跨 socket 访问”的差异。一个需要 8 CPU + 16Gi 的 Pod，如果 CPU 在 socket 0、内存分配在 socket 1，性能会下降。

NUMA-aware 插件：

- 读取 `Numatopology` CRD（shortName `numatopo`）或节点拓扑；
- 要求 Pod 的资源能放进**单个 NUMA 节点**；
- 绑定后记录 NUMA 分配，避免超卖。

检查：

```bash
kubectl get nodeinfo
kubectl describe node <node> | grep -i numa
```

如果节点没有 NUMANodes 信息，插件通常会退化为普通调度。

### 10.6.1 task 的 topologyPolicy

NUMA 约束可以按 task 声明：

```yaml
tasks:
  - name: worker
    replicas: 4
    topologyPolicy: single-numa-node
    template:
      spec:
        containers:
          - name: worker
            resources:
              requests:
                cpu: "8"
                memory: 32Gi
```

`topologyPolicy` 的取值：

| 值 | 语义 | 不满足时 |
|---|---|---|
| `none`（默认） | 不做 NUMA 约束 | — |
| `best-effort` | 尽量把容器的 CPU/内存放在同一 NUMA | 仍可调度，可能跨 NUMA |
| `restricted` | 只接受满足 NUMA 的放置 | 节点被 Predicate 拒绝 |
| `single-numa-node` | 所有资源必须在同一个 NUMA 节点内 | 节点被 Predicate 拒绝 |

Job controller 会把 task 的策略写到 Pod 注解：

```yaml
metadata:
  annotations:
    volcano.sh/numa-topology-policy: single-numa-node
```

`numa-aware` 插件读取这个注解，结合 Numatopology/CPU manager 的拓扑数据做判断。三个工程前提：

1. 节点必须暴露 NUMA 拓扑（`Numatopology` CRD 或等价 provider）；没有拓扑数据时插件通常退化，`restricted` 的“硬约束”也就名存实亡；
2. `single-numa-node` 要求**单个** NUMA 节点装得下该容器的全部 requests；8 CPU + 32Gi 在 4 个 8 核 NUMA 上可能放不下 32Gi 的本地内存；
3. 它约束的是 CPU/内存的 NUMA 放置，不是 GPU 亲和；GPU-NUMA 对齐要靠设备插件/DRA 的设备属性或节点标签。

排障：

```bash
kubectl get pod <pod> -o jsonpath='{.metadata.annotations.volcano\.sh/numa-topology-policy}'
kubectl get nodeinfo <node> -o yaml
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i numa
```

如果 Pod 一直 Pending 且日志有 `cannot be assigned by numa`，先看 `topologyPolicy` 是否过严，再看节点拓扑是否完整。

## 10.7 打分冲突：一个真实案例

配置：

```yaml
- name: nodeorder
- name: binpack
  arguments:
    binpack.weight: "1"
```

集群两个节点，各 8 CPU。提交 4 个 2 CPU 的作业。

预期：binpack 装满 node-a，node-b 空闲。

实际：Pod 被分散到两个节点。

原因：nodeorder 的 least-requested 分数与 binpack 的打分方向相反，权重 1:1 时互相抵消。解决：

```yaml
- name: binpack
  arguments:
    binpack.weight: "10"
```

或者移除 nodeorder。这个案例说明：**打分插件不是越多越好，方向冲突会让配置失效**。

## 10.8 lifecycle：按生命周期聚拢

> 版本边界：`lifecycle` **不在 upstream v1.16 的 `pkg/scheduler/plugins` 中**；它常见于华为 CCE 等发行版的 Volcano 发行包。以下参数来自这些发行版的公开文档；如果你用 upstream Helm 镜像，配置里写 `lifecycle` 会导致插件找不到。使用前先确认发行版与二进制里的注册表。

`lifecycle` 插件解决的是“同类 Pod 应该待在一起，方便缩容”的问题。它统计服务副本数的变化（扩容/缩容事件），给生命周期相似的节点打高分，让同一服务的 Pod 聚拢；在开启 cluster-autoscaler 的集群里，它还会调整节点的缩容注解，优先缩容利用率低的节点。

常用参数（以目标版本为准）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `lifecycle.MaxGrade` | 3 | 副本变化等级数 |
| `lifecycle.MaxScore` | 200 | 打分上限，通常高于其他打分插件 |
| `lifecycle.SaturatedTresh` | 0.8 | 节点利用率超过该值后，不再往该节点聚拢 |
| `lifecycle.WindowSize` | 10 | 记录副本变化事件的窗口 |

适用：

- 有弹性伸缩的在线服务/推理副本；
- 希望缩容时优先腾空整节点；
- 能接受“生命周期统计”带来的额外状态。

不适用：

- 确定性批处理/训练：Pod 生命周期由作业决定，聚类收益小；
- 需要严格打散的场景：与 `binpack`/spread 的方向可能冲突；
- 节点用 `volcano.sh/long-lifecycle-node: true` 标记为“长生命周期”时，插件会跳过它。

`lifecycle` 不是“按 taint 驱逐”的插件；污点/容忍仍由 `predicates` 处理。把它和 `nodeorder`/`binpack` 同时启用时，要先确认谁主导打分。

## 10.9 碎片与“最大可调度 gang”

监控节点空闲总量不够，还要看“形状”：

```text
node-a: 2 GPU free
node-b: 2 GPU free
node-c: 2 GPU free
node-d: 2 GPU free
```

总空闲 8 GPU，但一个需要 8 GPU 单节点的作业无法调度。更有用的指标：

- 每个节点最大连续空闲 GPU；
- 每个机架/拓扑域的最大可调度 gang；
- 因碎片导致的 Pending 作业数。

binpack 和 topology-aware 是缓解碎片的主要手段，但它们的组合也可能让大 gang 更难凑齐：装箱把资源聚拢，拓扑又把范围缩小。生产上要针对主力作业形状调参。

## 10.10 节点打分与队列公平的交互

节点打分只决定“放哪”，不决定“谁能跑”。一个高优先级作业仍然可能因为队列 capability 被拒绝，一个低优先级作业也可能因为队列空闲而先跑。

排障时不要混淆：

- 作业 Pending 且没有 Pod 绑定 → 队列/优先级/gang 问题；
- 作业部分 Pod 绑定、部分 Pending → 节点形状/拓扑/打分问题。

## 10.11 MiniVolcano 的实现

MiniVolcano 提供：

- `predicates`：污点、toleration、nodeSelector；
- `nodeorder`：least requested + balanced + pod count；
- `binpack`：binpack/spread 两种模式，按资源或主导维度；
- `resource-strategy-fit`：单资源策略的简化版；
- `numa-aware`：单 NUMA 节点适配。

场景：

- `binpack`：两个 8 CPU 节点，四个 2 CPU 作业，node-a 打满，node-b 空闲；
- `numa`：16 CPU 节点分两个 8 CPU NUMA，12 CPU 的 Pod 无法放入单个 NUMA，保持 Pending；
- `heterogeneous`：nodeSelector 把作业限制在 A100 节点。

简化：

- 没有卷、端口、Pod 亲和等 predicates；
- 没有真实的 Numatopology CRD，NUMA 信息直接放在 Node 对象上；
- nodeorder 的 balanced 用方差近似；
- 没有图片本地性、网络带宽等打分；
- 没有 `usage` 插件（没有 metrics-server/真实利用率），所有账本都基于 requests。

## 10.12 打分的数值稳定性与可解释性

节点打分看起来只是“加几个权重”，但它是调度结果不确定性的主要来源之一。两个集群配置相同、输入相同，却因为 map 遍历顺序或浮点累加顺序产生不同的放置，排障会非常痛苦。

### 10.12.1 四个不稳定源

| 来源 | 表现 | 解决 |
|---|---|---|
| map 遍历 | 同一节点每次得分略有差异，平手规则失效 | 按 key 排序后累加 |
| 浮点累加 | 插件顺序不同导致末位差异 | 固定加法顺序；比较时用 epsilon 或整数化 |
| 分母为零 | `Inf`/`NaN` 传播，节点排序崩坏 | 容量为 0 的维度跳过；显式处理除零 |
| 权重范围不一致 | 某插件分数淹没其他插件 | 归一化到固定区间（如 0–100），再乘权重 |

MiniVolcano 的 `nodeorder` 在开发期就因为“按 map 遍历累加浮点”导致分数不稳定，被审计发现后改为按排序后的维度累加（第 25.8 节）。

### 10.12.2 让打分可解释

一个实用的做法是给每个插件分配固定的分数区间，并在调试日志里输出分项：

```text
node-a: total=73.5
  nodeorder.least-requested = 40.0
  nodeorder.balanced        = 20.0
  binpack                   = 10.0
  topology                  = 3.5
```

这样当用户问“为什么去了 node-a”，你能回答是哪个插件主导，而不是只看一个总数。

三条工程规则：

1. **插件分数归一化**。不要让一个插件返回 0–10000，另一个返回 0–1；权重会失去意义。
2. **平手有稳定 fallback**。分数相同按节点名/UID 排序，保证可复现。
3. **分数不参与硬约束**。再低的分数也不能阻止违规节点被选中；硬约束必须用 Predicate。

### 10.12.3 一个数值边界例子

节点有 8 CPU、0 GPU，Pod 请求 1 GPU。如果打分函数直接算 `requested/total`，GPU 维度的分母是 0：

```text
gpu_ratio = 1 / 0 = +Inf
score = -Inf 或 NaN
```

不同语言/实现的处理不同，可能导致该节点排到最前或最后。正确做法是：

```go
if capacity[k] == 0 {
    continue // 该维度不参与打分；可行性已由 Predicate 排除
}
```

Predicate 已经保证 Pod 不会放到没有该资源的节点，打分层不应再引入未定义行为。

### 10.12.4 如何验证打分稳定性

```bash
# 同一场景跑两次，diff 输出
go run ./cmd/minivolcano -scenario binpack -json > /tmp/run1.json
go run ./cmd/minivolcano -scenario binpack -json > /tmp/run2.json
diff /tmp/run1.json /tmp/run2.json

# 单元测试：固定输入，断言节点顺序与分数
go test ./internal/plugins -run TestNodeOrderDeterministic -count=1
```

真实集群上，建议对同一批作业做“回放”：固定节点、固定请求、固定配置，比较两次调度的绑定序列。如果序列不同，先查打分插件的遍历顺序和浮点累加，而不是怀疑调度器“随机”。

## 10.13 本章小结

- Predicate 是硬约束，Score 是偏好；先过滤再打分。
- nodeorder 倾向打散，binpack 倾向装箱；同时启用要调权重。
- resource-strategy-fit 允许按资源维度组合策略。
- usage 用真实利用率补充 requests 账本，但依赖指标链路，且可能与 nodeorder/binpack 冲突。
- NUMA-aware 要求单 NUMA 适配；task 的 `topologyPolicy` 决定 none/best-effort/restricted/single-numa-node。
- lifecycle 按 Pod 生命周期聚拢，适合弹性服务与缩容；它是发行版扩展，不在 upstream v1.16；与 binpack/spread 同时用要注意方向。
- 碎片要看“形状”，不只是总量；binpack + 拓扑是主要缓解手段。
- MiniVolcano 用 5 个插件覆盖了这些核心行为，未模拟 usage/lifecycle。

下一章进入拓扑感知：从任务角色亲和到 HyperNode 网络拓扑。
