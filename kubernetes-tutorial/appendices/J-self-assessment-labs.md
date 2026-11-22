# 附录 J 自测题库与实验手册

> 本附录把正文的“读懂”推进到“能判断、能动手、能解释”。题目按正文顺序编排，答案不追求背诵，而是回到机制与取舍；实验全部可以在 MiniK8s 上重复执行，不依赖真实集群。

## J.1 怎么用这份手册

建议分三轮：

1. **闭卷诊断**：每部分先做选择题，不查资料，记录不确定的题号。
2. **开卷补洞**：按题号回到对应章节，只读没把握的小节，再重做错题。
3. **动手验证**：完成 J.11 的对应实验，把“知道”变成“观察到”。

评分建议：

- 选择题每题 1 分，共 64 分。
- 简答题每题 5 分，共 80 分（按要点给分，不要求逐字）。
- 结业项目从 J.12 中任选一个，按 40 分计。
- 总分 184 分；110 分及格，150 分以上说明已经具备平台工程师的机制判断力。

比分数更重要的是**能否说出反例**：一个机制在什么条件下会失效，失效时先看什么证据。

## J.2 基础与架构（第 1–5 章）

**J.2.1** Kubernetes 最核心的抽象是什么？

A. 容器镜像  
B. 声明式 API + 控制循环  
C. 虚拟机  
D. 服务网格

**J.2.2** 哪个组件直接读写 etcd？

A. kube-scheduler  
B. kubelet  
C. kube-apiserver  
D. kube-proxy

**J.2.3** watch 断线后，客户端持有的 `resourceVersion` 已被 compaction，正确做法是？

A. 忽略错误继续 watch  
B. 从当前时间重新 watch  
C. 重新 List，拿到新版本后再 watch  
D. 重启 API Server

**J.2.4** `metadata.generation` 在什么时候递增？

A. status 变化时  
B. spec 变化时  
C. 对象被读取时  
D. 标签变化时

**J.2.5** 下列哪项不是 API Server 的职责？

A. 认证  
B. 准入控制  
C. 选择节点  
D. 授权

**J.2.6** CRD 的作用是？

A. 运行容器  
B. 注册新的 API 类型  
C. 替代 etcd  
D. 做四层负载均衡

**J.2.7** Server-Side Apply 判断字段冲突的主要依据是？

A. `resourceVersion`  
B. `managedFields`  
C. `uid`  
D. `generation`

**J.2.8** finalizer 的作用是？

A. 阻止对象被创建  
B. 把实际删除推迟到清理逻辑完成之后  
C. 给 Secret 加密  
D. 自动备份对象

**简答**

**J.2.9** 用自己的话解释“Kubernetes 是一个带控制器的分布式数据库”，并给出一个该抽象解释不了的现象。

**J.2.10** 为什么 Kubernetes 选择 watch 而不是轮询？watch 又带来了哪些新问题？

## J.3 工作负载与存储（第 6–12 章）

**J.3.1** Deployment 直接管理的是？

A. Pod  
B. ReplicaSet  
C. StatefulSet  
D. Node

**J.3.2** 哪个 Pod phase 表示所有容器都已成功终止且不会再重启？

A. Running  
B. Pending  
C. Succeeded  
D. Unknown

**J.3.3** readinessProbe 失败时会发生什么？

A. 容器被重启  
B. Pod 被删除  
C. Pod 从 Service 后端摘除，但容器继续运行  
D. 节点被标记 NotReady

**J.3.4** 下列哪项不是 StatefulSet 提供的？

A. 稳定的 Pod 名称（ordinal）  
B. 稳定的网络身份  
C. 稳定的存储绑定  
D. 数据库主从选举

**J.3.5** Guaranteed QoS 的条件是？

A. 只设置 requests  
B. 只设置 limits  
C. 所有容器都设置 requests 且 requests 等于 limits  
D. 使用 BestEffort

**J.3.6** emptyDir 卷的生命周期是？

A. 跟随节点  
B. 跟随 Pod  
C. 跟随 Namespace  
D. 永久保留

**J.3.7** StorageClass 的 `volumeBindingMode: WaitForFirstConsumer` 主要解决什么问题？

A. 加快镜像拉取  
B. 让卷绑定发生在 Pod 调度之后，满足拓扑约束  
C. 自动扩容  
D. 加密卷

**J.3.8** 滚动更新设置 `maxUnavailable: 0`、`maxSurge: 25%` 时，发布过程通常？

A. 先缩旧再扩新，允许短暂容量下降  
B. 先扩新再缩旧，尽量不减少可用副本  
C. 一次性删除全部旧 Pod  
D. 不创建新 Pod

**简答**

**J.3.9** requests 与 limits 分别影响调度和运行时，请说明二者不一致时可能出现的两种极端情况。

**J.3.10** 为什么“有状态服务上 Kubernetes”不等于“StatefulSet 帮你做高可用”？

## J.4 网络与服务（第 13–16 章）

**J.4.1** ClusterIP 的流量最终由谁转发到 Pod？

A. API Server  
B. kube-proxy、eBPF 或云负载均衡等数据面  
C. CoreDNS  
D. kubelet

**J.4.2** EndpointSlice 与 Endpoints 的关系是？

A. 完全无关  
B. EndpointSlice 是 Endpoints 的分片化演进  
C. EndpointSlice 只用于外部服务  
D. Endpoints 已删除

**J.4.3** Headless Service 的 DNS 查询通常返回？

A. 一个虚拟 IP  
B. Pod IP 列表  
C. 节点 IP 列表  
D. 只返回 CNAME

**J.4.4** 一个 Pod 没有被任何 NetworkPolicy 选中时，默认网络行为是？

A. 全部拒绝  
B. 全部允许  
C. 只允许 DNS  
D. 只允许同命名空间

**J.4.5** Ingress 资源本身能完成流量转发吗？

A. 能，API Server 内置代理  
B. 不能，需要 Ingress Controller 实现  
C. 只能转发 TCP  
D. 只能用于 TLS

**J.4.6** Gateway API 相比 Ingress 的主要改进不包括？

A. 角色分离（基础设施/集群/应用）  
B. 更强的路由表达能力  
C. 内置服务网格数据面  
D. 更好的跨实现可移植性

**J.4.7** CNI 插件负责的是？

A. 给 Pod 分配 IP 并配置网络连通  
B. 存储卷挂载  
C. 镜像签名  
D. 调度打分

**J.4.8** Sidecar 服务网格的主要代价不包括？

A. 额外的网络跳数与延迟  
B. 每个 Pod 的资源开销  
C. 注入、升级和排障复杂度  
D. 必须修改应用源代码

**简答**

**J.4.9** 一个 Service 有 ClusterIP，但访问超时。请按数据面顺序列出至少五个检查点。

**J.4.10** NetworkPolicy 的“默认拒绝”为什么通常要同时配置 DNS 放行？忽略了会发生什么？

## J.5 调度、控制器与扩展（第 17–22 章）

**J.5.1** 默认调度器选定节点后写哪个字段？

A. `status.nodeName`  
B. `spec.nodeName`  
C. `metadata.labels`  
D. `spec.affinity`

**J.5.2** 调度框架中，Filter 阶段的作用是？

A. 给节点打分  
B. 排除不满足硬约束的节点  
C. 执行绑定  
D. 驱逐低优先级 Pod

**J.5.3** 抢占能够发生的前提是？

A. 任何 Pending Pod 都可以抢占  
B. 高优先级 Pod 找不到可行节点，且存在可驱逐的低优先级 Pod  
C. 节点处于 NotReady  
D. Pod 没有设置 requests

**J.5.4** Informer 的本地缓存最重要的作用是？

A. 替代 etcd  
B. 减少控制器对 API Server 的重复读取  
C. 加密对象  
D. 自动扩缩容

**J.5.5** ownerReference 的主要作用是？

A. 建立归属关系并支持级联删除  
B. 设置资源配额  
C. 选择节点  
D. 加密 Secret

**J.5.6** HPA 最常用的默认指标是？

A. 磁盘 IO  
B. CPU 利用率  
C. 网络丢包率  
D. GPU 显存

**J.5.7** VPA 的主要行为是？

A. 增加 Pod 副本数  
B. 调整容器的 requests/limits  
C. 增加节点  
D. 修改 Service 类型

**J.5.8** Kueue 的核心价值是？

A. 替代 kubelet  
B. 在创建 Pod 之前做队列准入与配额管理  
C. 提供 CNI  
D. 做镜像扫描

**简答**

**J.5.9** 为什么“所有控制器都直接 watch 全量对象”在大集群里不可行？informer 和 workqueue 分别缓解了什么？

**J.5.10** gang scheduling 解决什么问题？不用它时，分布式训练可能出现什么资源死锁？

## J.6 安全与治理（第 23–26 章）

**J.6.1** Role 与 ClusterRole 的主要区别是？

A. Role 只能读，ClusterRole 只能写  
B. Role 作用于命名空间，ClusterRole 作用于集群范围  
C. Role 不能绑定  
D. ClusterRole 只能给系统组件

**J.6.2** 默认挂载 ServiceAccount token 的主要风险是？

A. 增加镜像体积  
B. token 泄露后可冒充 Pod 身份调用 API  
C. 降低网络带宽  
D. 阻止 Pod 启动

**J.6.3** Pod Security Admission 的三个级别是？

A. low/medium/high  
B. privileged/baseline/restricted  
C. dev/staging/prod  
D. allow/deny/audit

**J.6.4** Mutating Webhook 与 Validating Webhook 的通常顺序是？

A. Validating 先于 Mutating  
B. Mutating 先于 Validating  
C. 并行执行，无顺序  
D. 只执行其中一个

**J.6.5** Kubernetes Secret 默认在 etcd 中是？

A. 强加密  
B. base64 编码，未必加密  
C. 明文删除  
D. 只存哈希

**J.6.6** 原生 NetworkPolicy 不能直接表达？

A. 按 Pod 标签选择  
B. 按命名空间选择  
C. 按 HTTP 方法和路径过滤  
D. 按端口过滤

**J.6.7** 最小权限原则在 RBAC 中的正确做法是？

A. 给所有人 cluster-admin  
B. 只授予完成工作所需的 verb/resource/namespace  
C. 只使用 RoleBinding  
D. 禁用 ServiceAccount

**J.6.8** 验证容器镜像来源与完整性的常用工具是？

A. cosign / Sigstore  
B. kube-proxy  
C. CoreDNS  
D. metrics-server

**简答**

**J.6.9** 为什么“命名空间”不等于“安全边界”？请从网络、内核、API 三个层面说明。

**J.6.10** 一个 Pod 能读取同命名空间所有 Secret，可能由哪些配置导致？给出至少三条排查方向。

## J.7 可观测性与生产运维（第 27–33 章）

**J.7.1** 3 节点 etcd 集群最多容忍几台同时故障？

A. 0  
B. 1  
C. 2  
D. 3

**J.7.2** 控制面完全不可用时，已经运行的 Pod 通常会？

A. 立即停止  
B. 继续运行，但无法接收新的调度/变更  
C. 被 kubelet 删除  
D. 自动迁移到其他集群

**J.7.3** PodDisruptionBudget 保护的是？

A. 节点硬件故障  
B. 自愿驱逐（drain/eviction）  
C. 手动删除 Deployment  
D. 镜像拉取失败

**J.7.4** HPA 与 Cluster Autoscaler 的关系是？

A. HPA 增加节点，CA 增加副本  
B. HPA 调整副本数，CA 在需要时增加节点  
C. 两者完全等价  
D. 两者不能同时使用

**J.7.5** Kubernetes 版本偏斜（version skew）通常要求？

A. kubelet 可以比 API Server 新任意版本  
B. kubelet 不能比 API Server 新，且只允许有限的小版本落后  
C. 所有组件必须完全同版本  
D. etcd 必须比 API Server 新

**J.7.6** 排查 Pod 启动失败时，最应优先查看的第一手证据是？

A. 应用源码  
B. Pod status、conditions 与 Events  
C. 节点 BIOS  
D. 镜像仓库 UI

**J.7.7** SLO 为 99.9% 时，错误预算通常是？

A. 0.1%  
B. 1%  
C. 9%  
D. 99.9%

**J.7.8** GitOps 中的“漂移（drift）”指？

A. Git 仓库分支漂移  
B. 集群实际状态偏离 Git 中的期望状态  
C. 节点时钟漂移  
D. 网络 MTU 不一致

**简答**

**J.7.9** 一次滚动发布后错误率上升，但 Pod 都是 Running。请给出至少四条排查路径。

**J.7.10** 为什么“备份了 etcd”不等于“能恢复集群”？恢复演练要验证什么？

## J.8 MiniK8s 实现（第 34–40 章）

**J.8.1** MiniK8s 的 `ResourceStore` 中 `resourceVersion` 的主要用途是？

A. 加密对象  
B. watch 续接与乐观并发  
C. 计算资源配额  
D. 生成 DNS 记录

**J.8.2** MiniK8s 的控制器为什么直接轮询 store，而不是走 HTTP？

A. 为了更接近上游实现  
B. 为了测试更快、更确定，但牺牲了部分 API 契约  
C. 因为 Python 不支持 HTTP  
D. 为了支持多租户

**J.8.3** MiniK8s 的 kubelet 模拟了什么？

A. 真实内核命名空间  
B. 容器状态机、探针、日志与状态写回  
C. CNI 插件二进制  
D. GPU 驱动

**J.8.4** MiniK8s 的 API Server 支持哪种认证方式？

A. 仅 OIDC  
B. 静态 token 与 HMAC ServiceAccount token  
C. 仅 x509 客户端证书  
D. 仅云 IAM

**J.8.5** MiniK8s 的调度器打分主要考虑？

A. 节点温度  
B. 资源均衡、亲和性与软反亲和  
C. 镜像层数  
D. 磁盘转速

**J.8.6** MiniK8s 的网络模拟实现了？

A. 真实 iptables 规则  
B. 进程内 DNS、ServiceProxy、Ingress 与 NetworkPolicy 判定  
C. eBPF 程序  
D. BGP 路由

**J.8.7** 下列哪项是 MiniK8s 明确不支持的？

A. Deployment  
B. StatefulSet  
C. 真实容器隔离与 CNI/CSI 数据面  
D. ResourceQuota

**J.8.8** 把 MiniK8s 的内存存储替换为 etcd 时，最关键的映射是？

A. Pod 名称 → etcd key  
B. `resourceVersion` → etcd revision，watch → etcd watch  
C. Namespace → 数据库  
D. Label → 表名

**简答**

**J.8.9** 为什么 MiniK8s 的“测试全绿”不能证明它与真实 Kubernetes 语义一致？举一个测试可能编码了错误行为的例子。

**J.8.10** 如果要把 MiniK8s 的调度器替换为真实调度器，哪些接口/状态必须对齐？

## J.9 前沿与商业（第 41–46 章）

> 第 47 章的六个架构案例不单独出选择题；用 J.12 的结业项目练习“在约束下做取舍”。

**J.9.1** KubeVirt 让 Kubernetes 管理的是？

A. 函数  
B. 虚拟机  
C. Wasm 模块  
D. 物理交换机

**J.9.2** Knative 的核心能力是？

A. 请求驱动/事件驱动的自动伸缩与 Serverless 抽象  
B. 替代 etcd  
C. 提供 CSI  
D. 做镜像签名

**J.9.3** Wasm 在 Kubernetes 中的主要优势不包括？

A. 启动快  
B. 沙箱隔离  
C. 可直接复用所有 Linux 内核模块  
D. 体积小

**J.9.4** DRA 主要解决什么问题？

A. 让设备成为可声明、可选择、可共享的一等资源  
B. 替代 RBAC  
C. 加速 DNS  
D. 压缩镜像

**J.9.5** 托管 Kubernetes 的账单中，通常占比最大的往往是？

A. 控制面管理费  
B. 工作节点与相关基础设施  
C. API Server 证书  
D. kubectl 插件

**J.9.6** 企业采用多集群的最常见驱动力不包括？

A. 隔离与合规  
B. 容灾与地域就近  
C. 让每个开发人员拥有一个生产集群  
D. 组织/业务边界

**J.9.7** 平台工程的核心目标是？

A. 让所有团队直接学 kubectl  
B. 降低开发者的认知负荷，提供自助式黄金路径  
C. 禁止使用云服务  
D. 只做成本报表

**J.9.8** TCO 分析中最大的敏感项通常是？

A. 控制面单价  
B. 人力成本与资源利用率  
C. 域名费用  
D. 证书数量

**简答**

**J.9.9** “能跑在 Kubernetes 上”与“应该跑在 Kubernetes 上”有什么区别？用一个具体工作负载说明。

**J.9.10** 为什么开源项目的商业成功不等于技术最优？平台选型应如何平衡两者？

## J.10 参考答案

### 选择题

| 题号 | 答案 | 题号 | 答案 | 题号 | 答案 | 题号 | 答案 |
|---|---|---|---|---|---|---|---|
| J.2.1 | B | J.2.2 | C | J.2.3 | C | J.2.4 | B |
| J.2.5 | C | J.2.6 | B | J.2.7 | B | J.2.8 | B |
| J.3.1 | B | J.3.2 | C | J.3.3 | C | J.3.4 | D |
| J.3.5 | C | J.3.6 | B | J.3.7 | B | J.3.8 | B |
| J.4.1 | B | J.4.2 | B | J.4.3 | B | J.4.4 | B |
| J.4.5 | B | J.4.6 | C | J.4.7 | A | J.4.8 | D |
| J.5.1 | B | J.5.2 | B | J.5.3 | B | J.5.4 | B |
| J.5.5 | A | J.5.6 | B | J.5.7 | B | J.5.8 | B |
| J.6.1 | B | J.6.2 | B | J.6.3 | B | J.6.4 | B |
| J.6.5 | B | J.6.6 | C | J.6.7 | B | J.6.8 | A |
| J.7.1 | B | J.7.2 | B | J.7.3 | B | J.7.4 | B |
| J.7.5 | B | J.7.6 | B | J.7.7 | A | J.7.8 | B |
| J.8.1 | B | J.8.2 | B | J.8.3 | B | J.8.4 | B |
| J.8.5 | B | J.8.6 | B | J.8.7 | C | J.8.8 | B |
| J.9.1 | B | J.9.2 | A | J.9.3 | C | J.9.4 | A |
| J.9.5 | B | J.9.6 | C | J.9.7 | B | J.9.8 | B |

### 简答要点

**J.2.9** 要点：API Server 持久化对象、提供 watch；控制器只通过对象协作；解释不了“数据面仍在转发但控制面已失联”这类状态，因为它关注的是控制面的一致性，不是数据面的实时性。也可指出跨对象事务、外部副作用不在该模型内。

**J.2.10** 要点：watch 降低延迟与请求量、支持事件驱动；代价是连接多、断线重连、resourceVersion compaction、事件顺序与去重、API Server watch cache 内存、惊群/限流。

**J.3.9** 要点：requests 高、limits 低会被准入拒绝或运行时节流/OOM；requests 低、limits 高会过度调度、节点超卖，压力下争抢；两者相等才进入 Guaranteed。

**J.3.10** 要点：StatefulSet 只提供身份/顺序/存储绑定；复制、选主、脑裂处理、备份恢复仍由应用或 Operator 负责；节点/存储故障时仍需外部机制。

**J.4.9** 要点：Service/EndpointSlice 是否有 Ready 后端；Pod readiness；targetPort 与容器监听端口；NetworkPolicy；kube-proxy/eBPF 规则；DNS 解析；conntrack/MTU；节点路由。

**J.4.10** 要点：默认拒绝后 DNS 也被拒绝；需要放行 UDP/TCP 53 到 kube-system/CoreDNS，否则服务名解析失败，表现为“网络不通”但 IP 可通。

**J.5.9** 要点：全量 watch 造成 API Server/etcd 连接与事件风暴；informer 用 List+Watch+本地缓存合并事件、降低读放大；workqueue 做去重、限速、重试与并发控制。

**J.5.10** 要点：gang scheduling 保证一组 Pod 同时获得资源；否则部分 worker 启动后占着 GPU 等待，其他作业也无法使用剩余资源，形成死锁/饥饿。

**J.6.9** 要点：网络默认扁平，需 NetworkPolicy/CNI 隔离；内核共享，需 runtime sandbox/seccomp/cgroup；API 层 RBAC 只控制对象访问，不能阻止同节点逃逸或共享内核侧信道。

**J.6.10** 要点：ServiceAccount 绑定过宽 Role；automountServiceAccountToken 默认开启；Secret 未加密且同命名空间可读；RBAC 允许 list/get secrets；Pod 可访问 API；节点或备份泄露。

**J.7.9** 要点：新旧版本混合；readiness 过早通过；Endpoints 收敛延迟；资源 limit 导致节流；依赖服务/DB 连接池；Ingress/网关权重；日志与 trace 对比；回滚验证。

**J.7.10** 要点：快照可用不等于可启动；要验证证书、成员列表、数据目录、API Server 配置、恢复后对象一致性、应用连通性、恢复时间 RTO 与数据丢失 RPO。

**J.8.9** 要点：测试只覆盖已实现契约；断言可能把 bug 固化（例如把错误的滚动顺序写成期望）；模拟运行时无法暴露真实内核/网络/存储问题；需要契约测试、对照真实集群、对抗性审计。

**J.8.10** 要点：Pod/Node 状态与资源账本、filter/score 插件语义、PreFilter/PostFilter、抢占与 PDB、卷拓扑、扩展资源、绑定子资源、事件与重试、缓存一致性。

**J.9.9** 要点：技术可行性与经济/组织合理性不同；例如一个月跑一次的 2 核批处理，用 VM 或单机 cron 更便宜；Kubernetes 的调度、网络、存储、运维开销可能超过收益。

**J.9.10** 要点：商业成功受生态、支持、社区、锁定、渠道影响；技术选型要看成熟度、可运维性、退出成本、团队能力，而不是只看功能列表或融资额。

## J.11 实验手册

以下实验默认在仓库根目录、bash 兼容 shell 中执行，使用两个终端；`<pod-name>` 等占位符需要替换为实际名称。实验 1–10、12 只依赖 MiniK8s；实验 11 需要可选的 kind 集群。

### 实验 0：启动与基线

终端 A：

```bash
python -m mini_k8s --port 8080 --nodes 2
```

终端 B：

```bash
export MINIK8S_SERVER=http://127.0.0.1:8080
export MINIK8S_TOKEN=mini-admin-token
python -m mini_k8s.cli get nodes -o wide
python -m mini_k8s.cli cluster-info
python -m mini_k8s.cli api-resources | head -20
```

观察点：Node 的 allocatable、conditions、API Server 版本；记录基线，后续实验都从干净集群开始。

### 实验 1：一个 Pod 的完整旅程

```bash
python -m mini_k8s.cli apply -f examples/01-hello.yaml
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli get deployments
python -m mini_k8s.cli get replicasets
python -m mini_k8s.cli get services
python -m mini_k8s.cli get endpoints
python -m mini_k8s.cli describe pod <pod-name>
python -m mini_k8s.cli get events
```

问题：

1. Pod 的 `spec.nodeName` 是谁写的？
2. Endpoints 在 Pod Ready 之前是否包含它？
3. 删除一个 Pod 后，哪个控制器补副本？新 Pod 的 name/uid/IP 是否变化？

### 实验 2：滚动更新与回滚

```bash
python -m mini_k8s.cli apply -f examples/02-rollout.yaml
python -m mini_k8s.cli rollout status deployment rollout-demo
python -m mini_k8s.cli set image deployment/rollout-demo web=demo:v2
python -m mini_k8s.cli rollout status deployment rollout-demo
python -m mini_k8s.cli get replicasets
python -m mini_k8s.cli rollout undo deployment rollout-demo
```

观察点：新旧 ReplicaSet 的副本数变化、`deployment.kubernetes.io/revision`、`status.updatedReplicas/availableReplicas`。把 `maxUnavailable` 改为 50%，重复实验，比较发布过程中 Ready 副本的最低值。

### 实验 3：StatefulSet 的身份与存储

```bash
python -m mini_k8s.cli apply -f examples/03-statefulset.yaml
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli get pvc
python -m mini_k8s.cli exec db-0 -- hostname
python -m mini_k8s.cli delete pod db-0
python -m mini_k8s.cli get pods
python -m mini_k8s.cli get pvc
```

问题：删除 `db-0` 后，新 Pod 是否仍叫 `db-0`？PVC 是否重新绑定？DNS `db-0.db.default.svc.cluster.local` 是否稳定？

### 实验 4：配额、限制与驱逐边界

```bash
python -m mini_k8s.cli apply -f examples/11-quota-limitrange.yaml
python -m mini_k8s.cli describe resourcequota compute
python -m mini_k8s.cli apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: too-big
spec:
  containers:
    - name: c
      image: busybox
      resources:
        requests: {cpu: "8", memory: 32Gi}
EOF
```

观察点：请求被拒绝时的错误信息来自准入还是调度；ResourceQuota 统计的是 requests 还是 limits；LimitRange 是否给未写 resources 的容器补默认值。

### 实验 5：调度约束

```bash
python -m mini_k8s.cli apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: pinned
spec:
  nodeSelector:
    kubernetes.io/hostname: mini-node-1
  containers:
    - name: c
      image: busybox
EOF
python -m mini_k8s.cli get pod pinned -o wide
```

再构造一个带 `requiredDuringSchedulingIgnoredDuringExecution` 的 podAntiAffinity，让两个副本无法落在同一节点；观察 Pending 时的 `FailedScheduling` 事件。最后给节点加污点，验证 toleration：

```bash
python -m mini_k8s.cli taint mini-node-1 dedicated=gpu:NoSchedule
```

提交一个没有 toleration 的 Pod，确认它不会落到 `mini-node-1`；再加上对应的 toleration，确认可以调度。

### 实验 6：HPA 与 PDB

```bash
python -m mini_k8s.cli apply -f examples/12-hpa.yaml
python -m mini_k8s.cli get hpa
python -m mini_k8s.cli describe hpa cpu-app
python -m mini_k8s.cli apply -f examples/13-pdb.yaml
python -m mini_k8s.cli get pdb
python -m mini_k8s.cli drain mini-node-1
```

观察点：HPA 的 current/desired replicas 如何计算；drain 时 PDB 如何阻止驱逐；`kubectl drain` 的 `--ignore-daemonsets`/`--delete-emptydir-data` 在真实集群中的对应语义。实验结束后记得 `uncordon`：

```bash
python -m mini_k8s.cli uncordon mini-node-1
```

### 实验 7：Service、DNS 与 NetworkPolicy

MiniK8s 的 `exec` 是**模拟**的，不会真的在容器里运行 `curl`；因此这个实验用进程内 API 验证连通性，而不是伪造一个 `curl` 结果。

```python
from mini_k8s.cluster import MiniCluster
from pathlib import Path

c = MiniCluster(auto_start=True)
try:
    c.api.apply_file(Path("examples/09-networkpolicy.yaml"))
    c.wait_for_deployment("api")
    c.wait_for_pod("trusted-client")
    c.wait_for_pod("untrusted-client")

    print("DNS api ->", c.dns.resolve("api"))
    trusted = c.get("pods", "trusted-client")
    untrusted = c.get("pods", "untrusted-client")
    print("trusted  ->", c.proxy.connect(trusted, "api", "default", 80))
    print("untrusted->", c.proxy.connect(untrusted, "api", "default", 80))
finally:
    c.stop()
```

预期：`trusted` 的 `connected` 为 True，`untrusted` 为 False（NetworkPolicy 只允许 `app=trusted` 访问 `app=api` 的 8080）。

再观察策略对象本身：

```bash
python -m mini_k8s.cli apply -f examples/09-networkpolicy.yaml
python -m mini_k8s.cli describe networkpolicy api-allow-trusted
```

问题：DNS 解析成功但连接失败，说明问题在哪一层？NetworkPolicy 的 ingress/egress 方向分别作用于谁？如果客户端是 `untrusted-client`，被拒绝的包在哪个组件的检查里被丢弃？

### 实验 8：RBAC 与身份

```bash
python -m mini_k8s.cli apply -f examples/10-rbac.yaml
python -m mini_k8s.cli get roles -n team-dev
python -m mini_k8s.cli get rolebindings -n team-dev
python -m mini_k8s.cli get serviceaccounts -n team-dev
```

MiniK8s 的 `auth can-i` 检查的是当前 token 的身份。用开发 token 做一次真实的拒绝/放行实验：

```bash
# 放行前：developers 组没有权限，应返回 403
MINIK8S_TOKEN=mini-dev-token python -m mini_k8s.cli get pods
MINIK8S_TOKEN=mini-dev-token python -m mini_k8s.cli auth can-i get pods

# 以管理员身份给 developers 组绑定只读 Role
python -m mini_k8s.cli apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: developer-reader
  namespace: default
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: developer-reader
  namespace: default
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: developer-reader
subjects:
  - kind: Group
    name: developers
    apiGroup: rbac.authorization.k8s.io
EOF

MINIK8S_TOKEN=mini-dev-token python -m mini_k8s.cli get pods
MINIK8S_TOKEN=mini-dev-token python -m mini_k8s.cli auth can-i delete pods
```

观察 403 的 reason 与 status code；把 RoleBinding 的 `roleRef.name` 改错，确认绑定不会生效。

### 实验 9：CRD 与 Operator

```bash
python -m mini_k8s.cli apply -f examples/14-crd-operator.yaml
python -m mini_k8s.cli get crds
python -m mini_k8s.cli get websites
python examples/website_operator.py --server http://127.0.0.1:8080 --token mini-admin-token --once
```

观察点：CRD 注册后 discovery 是否出现新资源；Operator 如何 watch 自定义对象并创建底层 Deployment/Service；删除 CR 后 ownerReference 是否级联清理。

### 实验 10：故障注入

1. 删除 Deployment 的一个 Pod，观察补副本时间。
2. `cordon` 一个节点，再扩容，观察新 Pod 落到哪。
3. 提交一个 `restartPolicy: Never`、带 `mini-k8s.io/simulate.exit-code: "1"` 的 Pod，观察 `Failed` 与 Events。MiniK8s **不拉镜像**，因此无法模拟 `ImagePullBackOff`；这一项要留到实验 11 的真实集群里做。
4. 删除一个被 PVC 使用的 Pod，观察卷是否保留。
5. 让一个容器的探针永远失败，观察 Endpoints 摘除：

```bash
python -m mini_k8s.cli apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: never-ready
  annotations:
    mini-k8s.io/simulate.probe-fail: "true"
spec:
  containers:
    - name: app
      image: demo/probed:1.0
      readinessProbe:
        httpGet: {path: /ready, port: 8080}
      resources:
        requests: {cpu: 50m, memory: 32Mi}
EOF
python -m mini_k8s.cli get pod never-ready
python -m mini_k8s.cli describe pod never-ready
```

记录：每个故障的“第一手证据”是什么，恢复动作是什么，是否有中间状态。

### 实验 11：真实集群对照（可选，需要 kind）

```bash
kind create cluster --name k8s-lab
kubectl get nodes -o wide
kubectl apply -f examples/01-hello.yaml
kubectl get pods,endpointslices,events
kubectl describe pod <pod-name>
kubectl logs <pod-name>
```

对照 MiniK8s：真集群多出了哪些对象/字段（EndpointSlice、conditions、containerStatuses、QoS class、serviceAccount、tolerations）？哪些行为是模拟不了的（真实镜像、CNI、CSI、cgroup）？

### 实验 12：规模与性能基线

用脚本创建 90 个 Pod（90 × 100m = 9 CPU，低于 3 节点 × 4 CPU = 12 CPU 的容量），记录调度完成时间与每个 Pod 的绑定时刻：

```bash
python - <<'PY'
from mini_k8s.cluster import MiniCluster
import time

COUNT = 90
c = MiniCluster(node_count=3, node_pods=500, auto_start=True)
try:
    t0 = time.time()
    for i in range(COUNT):
        c.api.apply({
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"load-{i}", "namespace": "default"},
            "spec": {"containers": [{"name": "c", "image": "busybox",
                                      "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]},
        })
    deadline = time.time() + 20
    while time.time() < deadline:
        pods = [p for p in c.list("pods") if p["metadata"]["name"].startswith("load-")]
        scheduled = [p for p in pods if p.get("spec", {}).get("nodeName")]
        if len(scheduled) == COUNT:
            break
        time.sleep(0.1)
    elapsed = time.time() - t0
    bound = sorted(p["metadata"]["annotations"]["scheduler.mini-k8s.io/bound-at"] for p in scheduled)
    print(f"scheduled {len(scheduled)}/{COUNT} pods in {elapsed:.2f}s")
    print("first bound:", bound[0] if bound else "-")
    print("last  bound:", bound[-1] if bound else "-")
    assert len(scheduled) == COUNT, "not all pods scheduled; check capacity"
finally:
    c.stop()
PY
```

然后把 COUNT 改成 120（12 CPU 正好打满，留一点给系统开销），观察有多少 Pod 保持 Pending；再增加节点或降低 requests，解释变化。改变节点数、Pod 数、控制器 tick，画出“Pod 数 vs 调度完成时间”。

思考：真实集群的瓶颈会在同一处吗？API Server 写入、etcd fsync、调度器缓存、kubelet 创建容器，哪一段最先饱和？

## J.12 结业项目

从以下题目中选一个，完成后写一页设计说明与测试报告：

1. **给 MiniK8s 增加 VolumeSnapshot 子集**：定义 CRD、控制器、PVC 注解触发、状态条件、测试与示例。
2. **实现 ValidatingAdmissionPolicy 的最小子集**：支持 CEL 风格的简单表达式（如 `spec.replicas <= 5`），给出拒绝原因与事件。
3. **实现 Pod 拓扑分布的执行端**：当前调度器只做过滤，请补充 `ScheduleAnyway` 的软约束打分，并用实验证明分布更均匀。
4. **给控制器加真实 informer 语义**：用 List/Watch 替代轮询，处理 resourceVersion 过期、去重与重试，比较 API 请求量。
5. **写一个故障演练工具**：随机删除 Pod/节点、注入探针失败，自动收集事件并生成复盘报告。

验收标准：

- 有明确的 API 契约与边界，不破坏现有 217 个测试。
- 新功能至少有单元测试 + 一个端到端场景。
- `python -m pytest`、`python examples/run_all.py`、`python check_docs.py` 全绿。
- 文档说明“模拟了什么、没模拟什么、与真实 Kubernetes 的差距”。

## J.13 复盘点

做完题库与实验后，用下面五个问题自检：

1. 我能否在不知道答案时，说出该看哪个对象的哪个字段？
2. 我能否区分“控制面问题”与“数据面问题”？
3. 我能否为一个设计选择说出至少一个反例和代价？
4. 我能否把一个真实故障映射到本书的章节与 MiniK8s 模块？
5. 我能否把“能跑”与“该跑”分开判断？

如果有一项是否定的，回到对应章节与实验，不要用更多命令掩盖理解缺口。

## 延伸阅读

- [第 3 章 实验环境与 MiniK8s 快速开始](../chapters/03-lab-and-quickstart.md)
- [第 31 章 故障排查方法论](../chapters/31-troubleshooting.md)
- [第 39 章 测试策略](../chapters/39-testing.md)
- [附录 C 故障排查清单](C-troubleshooting-checklist.md)
- [附录 M 生产事故案例库](M-incident-casebook.md)
