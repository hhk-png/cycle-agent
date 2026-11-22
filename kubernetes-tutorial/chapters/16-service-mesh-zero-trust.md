# 第 16 章 服务网格与零信任

## 16.1 服务网格解决什么问题

Kubernetes 原生提供：

- Service 发现与 L4 负载均衡。
- NetworkPolicy L3/L4 隔离。
- Ingress L7 南北向。

它不提供：

- 服务间 mTLS 身份。
- 细粒度 L7 授权（方法/路径/header）。
- 重试、超时、熔断、流量镜像。
- 金丝雀/流量分割（Gateway API 部分覆盖南北向）。
- 统一的服务级指标/追踪。
- 跨集群/跨云的服务发现。

服务网格把这些问题下沉到数据面代理，让应用代码不感知。

## 16.2 架构

经典 sidecar 模式：

```text
Pod
├── app container
└── proxy sidecar (Envoy/linkerd2-proxy)
      │
      ├── 出站流量劫持
      ├── 入站流量劫持
      └── mTLS / 策略 / 遥测
```

控制面：

- **Istio**：istiod（配置分发、证书签发、服务发现）。
- **Linkerd**：destination、identity、proxy-injector。
- **Cilium Service Mesh**：eBPF + Envoy，无 sidecar 或按需。

数据面：

- Envoy（Istio、Cilium、Ambient）。
- linkerd2-proxy（Rust，轻量）。
- 其他。

## 16.3 Sidecar 模式的代价

- 每个 Pod 多一个容器：CPU/内存开销。
- 启动顺序/就绪依赖：应用可能在 sidecar 未就绪时启动。
- 网络路径多一跳：延迟增加。
- 升级需要重启 Pod（或热重启）。
- 调试复杂：iptables 劫持、Envoy 配置、证书。
- 对 Job/批处理不友好：sidecar 不退出导致 Job 卡住。

原生 sidecar（init container + restartPolicy: Always）缓解了生命周期问题，但资源开销仍在。

## 16.4 Sidecarless / Ambient

**Istio Ambient**：

- ztunnel（每节点 L4 代理）处理 mTLS、L4 授权、遥测。
- waypoint proxy（按 namespace/服务）处理 L7。
- 应用 Pod 不需要 sidecar。

**Cilium Service Mesh**：

- eBPF 处理 L3/L4、Service、策略。
- Envoy 按需处理 L7。
- 无 sidecar 或 per-node。

优势：

- 降低资源开销和注入复杂度。
- 升级不重启应用。
- 适合大规模。

代价：

- 架构更新，排障模型不同。
- L7 能力可能不如全 sidecar 灵活。
- 对内核/CNI 依赖更强。

## 16.5 mTLS 与身份

服务网格用 SPIFFE 风格身份：

```text
spiffe://cluster.local/ns/default/sa/web
```

证书由控制面签发，短有效期，自动轮换。代理之间建立 mTLS。

好处：

- 服务间加密。
- 双向身份认证。
- 不依赖网络位置（零信任）。
- 授权策略可以基于身份，而不是 IP。

注意：

- mTLS 只保护网格内流量；入口/出口需要额外配置。
- 应用如果自己做 TLS，可能与网格冲突（双重加密/证书不匹配）。
- 证书轮换失败会导致大面积 503。

### PeerAuthentication：从 PERMISSIVE 到 STRICT

mTLS 不是“开了就全局强制”。Istio 用 `PeerAuthentication` 分层控制：mesh 级默认、namespace 级覆盖、workload 级覆盖。迁移顺序通常是先 `PERMISSIVE`（同时接受明文和 mTLS，方便发现网格外调用），确认所有调用方都在网格内后，再切 `STRICT`。

```yaml
# mesh 级默认：只允许 mTLS
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system
spec:
  mtls:
    mode: STRICT
---
# 某个需要兼容旧客户端的 namespace 单独放宽
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: legacy
  namespace: legacy-apps
spec:
  mtls:
    mode: PERMISSIVE
  portLevelMtls:
    8080:
      mode: DISABLE
```

要点：

- `PERMISSIVE` 会**掩盖**未接入网格的调用方；迁移期用它观察，但不要把它当长期安全状态。
- 端口级 `portLevelMtls` 常用于健康检查/指标端口；只对确有必要且受网络策略保护的端口放开。
- 切 `STRICT` 前用 `istioctl x describe pod`、`istioctl analyze` 和目标服务的 telemetry 确认没有明文来源。
- 常见故障：非网格客户端（Job、外部系统、另一个集群）突然 503；探针走明文；证书轮换失败。回滚是改回 `PERMISSIVE`，不是删策略。

## 16.6 流量管理

Istio 示例：

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: reviews
spec:
  host: reviews
  subsets:
    - name: v1
      labels: {version: v1}
    - name: v2
      labels: {version: v2}
---
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: reviews
spec:
  hosts: [reviews]
  http:
    - route:
        - destination:
            host: reviews
            subset: v1
          weight: 90
        - destination:
            host: reviews
            subset: v2
          weight: 10
      retries:
        attempts: 3
        perTryTimeout: 2s
      timeout: 5s
```

`subset` 不是凭空存在的：它由 `DestinationRule` 定义，按 Pod 标签（这里是 `version: v1/v2`）选出实例。缺少 DestinationRule 时，VirtualService 能创建但流量无法按 subset 路由。**新清单用 `networking.istio.io/v1` / `security.istio.io/v1`**；`v1beta1` 只是旧集群可能仍在提供的兼容版本，不再是“覆盖最广”的推荐。升级前用 `kubectl api-resources | grep istio.io` 确认实际 served 版本。

能力：

- 按权重/header/cookie 路由。
- 重试、超时、熔断、连接池。
- 故障注入。
- 流量镜像。
- 跨集群路由。

这些能力很强，但也容易配置出灾难：

- 重试放大故障（重试风暴）。
- 超时设置不匹配，导致级联超时。
- 熔断阈值不合理，正常流量被拒。
- 多团队共用网格，策略冲突。

## 16.7 可观测性

网格自动产生：

- 请求量、错误率、延迟（RED）。
- 服务依赖图。
- mTLS 状态。
- 访问日志。
- 分布式追踪上下文传播。

代价：

- 指标基数爆炸（服务 × 版本 × 目的地 × 状态码）。
- 日志量大。
- 追踪采样与存储成本。

需要治理：

- 控制标签维度。
- 采样率。
- 指标聚合/降采样。
- 只对关键路径开全量追踪。

## 16.8 零信任

零信任的核心：

1. 不默认信任网络位置。
2. 每个请求都要认证和授权。
3. 最小权限。
4. 持续验证。

服务网格提供：

- 工作负载身份（SPIFFE）。
- mTLS。
- L4/L7 授权策略。
- 审计。

但不提供：

- 终端用户身份（需要 OIDC/JWT）。
- 数据级授权。
- 供应链安全。
- 运行时威胁检测。

零信任是体系，不是单一产品。

## 16.9 什么时候需要服务网格

适合：

- 多团队、多语言微服务，需要统一 mTLS 和流量治理。
- 需要细粒度 L7 授权/审计。
- 需要金丝雀、镜像、故障注入。
- 跨集群/混合云服务通信。
- 有专门平台团队维护。

不适合：

- 少量服务，Kubernetes Service + Ingress 足够。
- 团队没有能力维护控制面/代理。
- 对延迟极度敏感，无法接受额外一跳。
- 批处理/Job 为主，sidecar 生命周期麻烦。
- 只是想要“看起来云原生”。

## 16.10 选型对比

| 方案 | 数据面 | 优点 | 代价 |
|---|---|---|---|
| Istio sidecar | Envoy per Pod | 功能最全、生态成熟 | 资源开销、复杂度 |
| Istio Ambient | ztunnel + waypoint | 低开销、无需注入 | 新架构、L7 按需 |
| Linkerd | Rust micro-proxy | 轻量、简单、mTLS 默认 | 功能少于 Istio |
| Cilium Mesh | eBPF + Envoy | 高性能、与 CNI 集成 | 内核依赖、学习曲线 |
| 无网格 | kube-proxy + Ingress | 简单、低成本 | 缺少 mTLS/L7 治理 |

不要用“功能清单”选型；用团队能力、故障预算和实际需求选。

## 16.11 迁移策略

1. 先安装控制面，关闭自动注入。
2. 选一个非关键服务注入 sidecar，验证 mTLS/指标。
3. 配置默认 PERMISSIVE mTLS，逐步迁移。
4. 开启 STRICT mTLS，确保所有调用方都在网格内或有入口网关。
5. 引入 L7 授权，从监控模式到强制。
6. 逐步接入流量管理。
7. 建立网格 SLO 和故障演练。

常见事故：

- 开启 STRICT mTLS 后，网格外调用方全部失败。
- sidecar 注入后，健康检查端口被劫持。
- 出口流量被网格拦截，外部 API 不通。
- 升级控制面导致所有代理断连。

### 16.11.1 授权策略：从“加密”到“只允许该请求”

mTLS 只证明“谁在说话”，不决定“能说什么”。L7 授权策略才是零信任的访问控制层。以 Istio 为例：

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: orders-read-only
  namespace: shop
spec:
  selector:
    matchLabels: {app: orders}
  action: ALLOW
  rules:
    - from:
        - source:
            principals: ["cluster.local/ns/shop/sa/frontend"]
      to:
        - operation:
            methods: ["GET"]
            paths: ["/api/orders/*"]
    - from:
        - source:
            namespaces: ["observability"]
      to:
        - operation:
            methods: ["GET"]
            paths: ["/metrics"]
---
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: deny-non-shop
  namespace: shop
spec:
  action: DENY
  rules:
    - from:
        - source:
            notPrincipals: ["cluster.local/ns/shop/sa/*"]
```

要点：

- **默认允许与默认拒绝不是一句话**：没有任何 ALLOW 策略选中该 workload 时，请求默认允许；一旦有 ALLOW 策略选中它，不匹配任何 ALLOW 规则的请求就被拒绝（这就是“默认拒绝”的实现方式）。DENY 策略优先级高于 ALLOW，所以**不要**用“全局 DENY + 若干 ALLOW 例外”来实现白名单——例外不会生效。
- **mTLS 只做认证**：它证明调用方身份，不自动限制“能调用什么”；零信任还需要 ALLOW 策略。
- **身份优先**：用 `principals`（SPIFFE）而不是 IP/命名空间；命名空间只是弱约束。
- **L7 粒度**：方法、路径、header、JWT claim；不要把业务授权全塞进网格，应用仍需校验资源级权限。
- **策略顺序**：DENY 优先于 ALLOW；多策略叠加时要画出最终效果。
- **可观测**：先用 dry-run/审计模式观察会被拒绝的请求，再强制。

常见错误：

- 只开 mTLS，没有授权策略，网格内任意服务可互相调用。
- 用 `namespaces` 代替 `principals`，同命名空间的恶意工作负载可绕过。
- 把 `/metrics` 暴露给所有服务。
- 授权策略依赖 header，但入口没有可信地清洗/注入 header。

### 16.11.2 出口流量与 ServiceEntry

默认情况下，sidecar 会拦截 Pod 的出站流量。访问集群外服务时：

- 未注册的外部主机可能被阻断（取决于 `outboundTrafficPolicy`）。
- 需要 `ServiceEntry` 显式声明外部依赖，才能应用超时/重试/授权/遥测。
- 生产常用 **egress gateway** 集中出口，做审计、限流、固定 IP。

```yaml
apiVersion: networking.istio.io/v1
kind: ServiceEntry
metadata:
  name: payments-api
  namespace: shop
spec:
  hosts: ["api.payments.example.com"]
  ports:
    - number: 443
      name: https
      protocol: HTTPS
  resolution: DNS
  location: MESH_EXTERNAL
```

风险：

- 为了“让流量通”把 `outboundTrafficPolicy` 设为 `ALLOW_ANY`，等于放弃出口治理。
- 外部 API 的证书校验被 sidecar/应用重复处理，配置冲突。
- 出口网关成为单点，需要 HA 与容量规划。

### 16.11.3 证书轮转与故障模式

网格证书通常短有效期（小时/天级），自动轮换。轮转失败的表现：

- 大面积 503/连接重置。
- 代理日志出现证书过期/握手失败。
- 新旧控制面证书链不信任。

排查：

1. 看代理证书的 `NotAfter` 与 SAN。
2. 看控制面（istiod/identity）是否健康、能否签发。
3. 看时间同步（NTP）；证书验证依赖时钟。
4. 看信任根（root CA）轮换是否同步到所有集群。
5. 看 sidecar/ztunnel 版本是否一致。

运行建议：

- 监控证书剩余时间，提前告警。
- 根证书轮换要按厂商 runbook，分阶段信任新旧 CA。
- 不要把根私钥放在普通 Secret 中；使用 KMS/HSM。
- 多集群共享信任根时，轮换影响面更大。

### 16.11.4 多集群网格

多集群服务通信的常见模式：

- **东西向网关**：每个集群一个 ingress gateway，跨集群服务通过网关互访。
- **扁平网络**：跨集群 Pod IP 可路由（Cilium Cluster Mesh/Submariner）。
- **控制面共享 vs 独立**：共享控制面简单但故障域大；独立控制面隔离好但配置同步复杂。

关键设计：

- 信任域（trust domain）与身份命名：不同集群的 SPIFFE ID 是否可区分。
- 服务发现：DNS/Service 的跨集群解析。
- 故障转移：本地优先、跨集群兜底，避免跨区延迟。
- 策略：哪些服务允许跨集群调用，出口/入口如何审计。
- 可观测：跨集群追踪上下文、指标聚合。

### 16.11.5 性能与容量

Sidecar 的成本不只是内存：

- 每 Pod 一个代理，连接数/并发高时 CPU 显著。
- 多一跳网络，p99 延迟增加（通常亚毫秒到几毫秒，取决于配置）。
- 控制面下发配置的规模：服务数 × 端点数的乘积。
- 指标/日志/追踪的采集开销。

容量评估：

- 压测开启/关闭网格的延迟与 CPU。
- 限制 Envoy 并发/缓冲区，避免 OOM。
- 控制指标标签基数。
- 对大流量服务考虑 Ambient/无 sidecar 或 L4 直通。

### 16.11.6 落地顺序（更细）

1. 明确要解决的问题（mTLS？L7 授权？金丝雀？跨集群？），不要“全都要”。
2. 选一个低风险 namespace，安装控制面，关闭自动注入。
3. 注入一个服务，验证启动、就绪、指标、日志、延迟。
4. 开 PERMISSIVE mTLS，观察网格内外调用。
5. 开 STRICT，补 ServiceEntry/网关，确保外部调用不中断。
6. 加 AuthorizationPolicy，先审计后强制。
7. 引入流量管理（超时/重试/金丝雀），设置预算与告警。
8. 建立网格 SLO：控制面可用性、证书轮转、代理版本偏斜、配置下发延迟。

## 16.12 MiniK8s 的边界

MiniK8s 不实现服务网格。它提供：

- Service/Endpoints/DNS。
- NetworkPolicy 判定。
- ServiceProxy 模拟。

这些足以理解 L4 服务发现和策略，但 mTLS、L7 路由、重试/熔断需要真实网格实验。

如果你要在真实集群实验：

```bash
istioctl install --set profile=demo
kubectl label namespace default istio-injection=enabled
kubectl apply -f <bookinfo>
istioctl analyze
```

或：

```bash
linkerd install | kubectl apply -f -
linkerd check
linkerd viz install | kubectl apply -f -
```

## 本章小结

- 服务网格把 mTLS、L7 流量治理和遥测下沉到数据面。
- Sidecar 功能全但开销大；Ambient/eBPF 降低开销但架构更新。
- mTLS 是零信任的基础，但不是全部。
- 网格复杂度高，适合有平台团队和多服务治理需求的组织。
- 选型要看团队能力、延迟预算和运维成本，而不是功能列表。

## 延伸阅读

- [第 14 章 Ingress 与 Gateway API](14-ingress-gateway-api.md)
- [第 15 章 CNI、eBPF 与 NetworkPolicy](15-cni-ebpf-networkpolicy.md)
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
