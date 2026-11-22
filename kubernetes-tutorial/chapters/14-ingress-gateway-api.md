# 第 14 章 Ingress 与 Gateway API：南北向流量

## 14.1 四层与七层

- **四层（L4）**：TCP/UDP，按 IP/端口转发。Service NodePort/LoadBalancer 属于 L4。
- **七层（L7）**：HTTP/HTTPS，按 Host、Path、Header、Method 路由。Ingress/Gateway API 属于 L7。

L7 能提供：

- 一个负载均衡器承载多个域名/服务。
- 路径路由、重写、重定向。
- TLS 终止。
- 限流、认证、可观测性（取决于实现）。

代价：

- 需要 Ingress/Gateway Controller。
- 控制器实现差异大。
- 配置错误可能导致全站 404/502。

## 14.2 Ingress 资源

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: site
spec:
  ingressClassName: nginx
  tls:
    - hosts: [demo.example.com]
      secretName: demo-tls
  rules:
    - host: demo.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: api
                port:
                  number: 80
          - path: /
            pathType: Prefix
            backend:
              service:
                name: frontend
                port:
                  number: 80
```

字段：

- `ingressClassName`：选择哪个 Ingress Controller。
- `rules`：host + paths。
- `pathType`：
  - `Exact`：精确匹配。
  - `Prefix`：按路径段前缀匹配（`/api` 匹配 `/api`、`/api/v1`，不匹配 `/apix`）。
  - `ImplementationSpecific`：由控制器决定。
- `backend`：Service 名称 + 端口（number 或 name）。
- `tls`：证书 Secret（`kubernetes.io/tls`）。
- `defaultBackend`：没有匹配规则时的兜底。

Ingress 只描述路由意图，不包含：

- Controller 实现。
- 负载均衡器类型。
- 高级流量治理（重试、熔断、镜像、流量分割）。

这些通常通过 annotations 实现，导致：

- 控制器锁定。
- 注解不可移植。
- 缺少类型校验。
- 多团队协作时配置冲突。

## 14.3 Ingress Controller

常见：

- **ingress-nginx（已退役）**：社区版在 2026-03 后停止维护，不再发布安全补丁。存量集群应规划迁移；新项目不要选它。它也是“注解即 API”问题的典型。
- Traefik、HAProxy Ingress、Kong。
- AWS Load Balancer Controller（ALB/NLB）、GKE Ingress、Azure Application Gateway。
- Istio Gateway、Envoy Gateway、Cilium Gateway、Contour。
- NGINX Gateway Fabric（Gateway API 实现，区别于已退役的 ingress-nginx）。

部署形态：

```text
外部 LB → Ingress Controller Pods → Service → 后端 Pod
```

或：

```text
Ingress Controller 直接使用 hostNetwork/NodePort/LoadBalancer
```

生产关注：

- 高可用（多副本、反亲和、PDB）。
- 入口 LB 的健康检查。
- TLS 证书管理（cert-manager）。
- 配置重载时间与连接排空。
- 大规模路由表的性能。
- WAF、DDoS、限流。

## 14.4 路径匹配

`Prefix` 的语义容易搞错：

```text
/api 匹配 /api、/api/、/api/v1
不匹配 /apix
```

实现上按路径段边界匹配。不同控制器在大小写、编码、重复斜杠上的行为可能不同。

`Exact`：

```text
/api 只匹配 /api
```

`ImplementationSpecific` 依赖控制器，不建议跨控制器使用。

## 14.5 TLS

```bash
kubectl create secret tls demo-tls --cert=tls.crt --key=tls.key
```

```yaml
spec:
  tls:
    - hosts: [demo.example.com]
      secretName: demo-tls
```

Ingress Controller 读取 Secret，在入口终止 TLS。后端可以是 HTTP 或 HTTPS（取决于 annotation）。

生产建议：

- 使用 cert-manager 自动签发/续期。
- 证书 Secret 与 Ingress 同 namespace（跨 namespace 引用通常不允许）。
- 监控证书过期。
- 配置 HSTS、TLS 版本、密码套件（控制器特定）。
- 内部服务间也加密（mTLS/服务网格），不要假设内网可信。

## 14.6 Ingress 的局限

Ingress API 只解决了“HTTP 路由”的最小交集：

- 没有标准的方式表达流量分割、重试、超时、熔断。
- 没有标准的方式表达 TCP/UDP（部分控制器用 ConfigMap）。
- 没有角色分离：集群管理员和租户共用同一个 Ingress 资源模型。
- 注解不可移植，升级控制器可能破坏行为。
- 多控制器共存时，class 解析和默认 class 容易出错。

这些局限催生了 Gateway API。

## 14.7 Gateway API

Gateway API 用一组角色分离的资源：

```text
GatewayClass（基础设施提供者）
    │
Gateway（集群/平台管理员）
    │
HTTPRoute / GRPCRoute / TLSRoute / TCPRoute（应用团队）
```

### GatewayClass

由控制器实现者定义：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: example
spec:
  controllerName: example.com/gateway-controller
```

### Gateway

平台管理员创建入口网关：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: public
  namespace: infra
spec:
  gatewayClassName: example
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      tls:
        mode: Terminate
        certificateRefs:
          - name: wildcard-tls
      allowedRoutes:
        namespaces:
          from: Selector
          selector:
            matchLabels:
              gateway-access: "true"
```

### HTTPRoute

应用团队在自己的 namespace 创建路由：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: api
  namespace: team-a
spec:
  parentRefs:
    - name: public
      namespace: infra
  hostnames: [api.example.com]
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /v1
      backendRefs:
        - name: api
          port: 80
      filters:
        - type: RequestHeaderModifier
          requestHeaderModifier:
            add:
              - name: x-team
                value: team-a
```

跨 namespace 引用不是自动允许的，但不同引用关系由不同机制授权，不能混为一谈：

| 引用关系 | 由谁授权 | 典型失败条件 |
|---|---|---|
| Route → Gateway（`parentRefs`） | Gateway listener 的 `allowedRoutes` | listener 的 `namespaces.from` 不允许该 namespace |
| Route → Service（`backendRefs`） | Service 所在 namespace 的 `ReferenceGrant` | 缺少 `from: HTTPRoute` / `to: Service` 的 grant |
| Gateway → Secret（`certificateRefs`） | Secret 所在 namespace 的 `ReferenceGrant` | TLS 证书跨 namespace 但未授权 |

因此，上面 `team-a` 的 HTTPRoute 挂到 `infra/public` Gateway，**不需要** `ReferenceGrant`；需要的是 Gateway 的 listener 显式允许 `team-a`：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: public
  namespace: infra
spec:
  gatewayClassName: example
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      tls:
        certificateRefs:
          - name: public-cert   # 同 namespace，不需要 ReferenceGrant
      allowedRoutes:
        namespaces:
          from: Selector
          selector:
            matchLabels:
              gateway.example.com/tenant: "true"
```

如果 HTTPRoute 的 `backendRefs` 指向另一个 namespace 的 Service，才需要在该 Service 的 namespace 放 `ReferenceGrant`：

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-team-a-backends
  namespace: team-b          # 被引用的 Service 所在 namespace
spec:
  from:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      namespace: team-a
  to:
    - group: ""
      kind: Service
      name: api
```

排障时看两个条件：`Accepted` 表示 Route 是否被 Gateway 接纳（parentRefs / allowedRoutes）；`ResolvedRefs` 表示所有 backendRefs、certificateRefs 是否解析成功（ReferenceGrant / 对象是否存在）。前者 False 查 listener，后者 False 查 ReferenceGrant 和目标对象。

优势：

- 角色分离：基础设施团队管 Gateway，应用团队管 Route。
- 类型化字段：重试、超时、header 修改、流量分割。
- 跨 namespace 引用有明确授权（ReferenceGrant）。
- 可移植性更好。

### 流量分割

```yaml
backendRefs:
  - name: api-v1
    port: 80
    weight: 90
  - name: api-v2
    port: 80
    weight: 10
```

可用于金丝雀/蓝绿。更复杂的渐进发布可用 Argo Rollouts/Flagger。

### 14.7.1 不只 HTTPRoute：GRPCRoute、TLSRoute、TCPRoute

Gateway API 按协议拆分路由：

- **HTTPRoute**：HTTP/HTTPS，支持 path/header/query 匹配、重定向、header 修改、流量分割。
- **GRPCRoute**：按 service/method 匹配，适合 gRPC；比用 HTTPRoute 匹配 `:path` 更语义化。
- **TLSRoute**：按 SNI 路由 TLS 连接，通常用于透传（passthrough），不终止 TLS。
- **TCPRoute/UDPRoute**：四层转发，适合数据库代理、游戏、DNS 等。
- **BackendTLSPolicy**：描述 Gateway 到后端 Pod 的 TLS（后端证书校验、SNI），解决“网关到后端也要加密”。

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GRPCRoute
metadata:
  name: echo
  namespace: team-a
spec:
  parentRefs:
    - name: public
      namespace: infra
  hostnames: ["grpc.example.com"]
  rules:
    - matches:
        - method:
            service: echo.Echo
            method: SayHello
      backendRefs:
        - name: echo
          port: 50051
```

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: BackendTLSPolicy
metadata:
  name: echo-tls
  namespace: team-a
spec:
  targetRefs:
    - group: ""
      kind: Service
      name: echo
  validation:
    caCertificateRefs:
      - name: echo-ca
        group: ""
        kind: ConfigMap
    hostname: echo.team-a.svc
```

注意：

- Gateway API 的 CRD 版本（`v1`/`v1beta1`）与字段随安装的版本演进；示例以常见稳定字段为主，采用前核对目标集群的 CRD。
- 不同控制器对 Route 类型/字段的支持差异很大；选型时看 conformance 报告，而不是只看 CRD 是否存在。
- TLSRoute 的 passthrough 与 Gateway 的 TLS terminate 是两种模式，证书管理方式不同。
- 四层路由失去 HTTP 语义（header/重试），可观测性也更弱。

### 14.7.2 GAMMA：网格内的 Gateway API

GAMMA（Gateway API for Mesh Management and Administration）把 Gateway API 的资源用于服务网格内部（东西向）流量：

- 用 HTTPRoute 表达服务间的路由/流量分割。
- 由网格控制器实现，而不是入口网关。
- 目标是让南北向与东西向使用一致的策略模型。

现状：

- 不同网格（Istio、Linkerd、Cilium）支持程度不同。
- 与服务网格自己的 CRD（VirtualService、AuthorizationPolicy）可能重叠。
- 采用前确认“谁是真相源”，避免两套策略冲突。

### 14.7.3 Policy Attachment

Gateway API 的核心资源不包含所有策略（重试、超时、限流、WAF、IP 白名单）。这些通过 **Policy Attachment** 扩展：

- 策略对象通过 `targetRef` 挂到 Gateway/HTTPRoute/Service。
- 不同实现有自己的 Policy CRD（如 GCPBackendPolicy、EnvoyExtensionPolicy）。
- 可移植性下降：核心路由可移植，策略通常绑定实现。

设计建议：

- 把“路由”与“策略”分开管理，策略用独立的 Git 目录/所有权。
- 明确策略冲突的优先级（更具体的 target 覆盖更宽的）。
- 不要让应用团队直接改 Gateway 级策略（影响所有租户）。

### 14.7.4 实现选型与 conformance

Gateway API 是规范，不是实现。选型看：

- **Conformance**：是否通过 Core/Extended 测试；哪些 Route 类型支持。
- **数据面**：Envoy、NGINX、HAProxy、云 LB；性能与特性不同。
- **多租户**：ReferenceGrant、allowedRoutes、策略隔离是否可靠。
- **可观测**：访问日志、指标、追踪、错误码。
- **升级**：CRD 版本、控制器版本、数据面配置的兼容性。
- **运维**：证书管理、配置下发延迟、故障恢复。

常见实现：Envoy Gateway、Istio Gateway、Cilium Gateway、NGINX Gateway Fabric、Kong、Traefik、云厂商 Gateway 实现。不要假设“Gateway API 兼容”等于“行为一致”。

## 14.8 Gateway API 与 Ingress 的关系

- Ingress 仍然广泛使用，短期不会消失。
- Gateway API 是更现代的方向，但实现成熟度、团队熟悉度、云厂商支持因环境而异。
- 迁移通常从新服务开始，逐步替换 Ingress。
- 不是所有 Ingress 功能都有标准 Gateway API 对应；部分仍靠实现扩展（Policy attachment）。

## 14.9 服务网格与 Gateway 的边界

- **Ingress/Gateway**：南北向，集群外 → 集群内。
- **Service Mesh**：东西向，服务 → 服务；也可通过 ingress gateway 处理南北向。
- **API Gateway**：更偏 API 管理（认证、配额、开发者门户），可与 Gateway API 集成。

三者有重叠，选型要看团队职责和治理需求，不要为了“全都要”叠加三层代理。

## 14.10 MiniK8s 的实现

MiniK8s 同时实现 Ingress 和 Gateway API 两条南北向路径。前者用于对照历史 API，后者用于演示“状态契约与数据面分离”的现代实现方式。

### 14.10.1 Ingress 路由器

`mini_k8s/network/ingress.py` 实现简化路由器：

- 遍历 Ingress rules，按 host 精确/通配匹配。
- 按 path 最长前缀匹配。
- 支持 `defaultBackend`。
- 返回 Service、端口、ClusterIP。

```bash
python -m mini_k8s.cli apply -f examples/08-ingress.yaml
python -m mini_k8s.cli get ingress
```

测试：

- `test_ingress_router_longest_prefix_and_default_backend`
- `test_ingress_status_published`

### 14.10.2 Gateway API 控制器

`mini_k8s/controllers/gateway.py` 实现 `GatewayClass`、`Gateway`、`HTTPRoute` 与 `ReferenceGrant`：

1. **GatewayClass**：`spec.controllerName` 等于 `mini-k8s.io/gateway-controller` 时写 `Accepted=True`；其他控制器写 `Accepted=False/Unsupported`。
2. **Gateway**：
   - 校验 class 是否存在且被本控制器接受；
   - 逐 listener 写 `status.listeners`：`supportedKinds`（HTTP/HTTPS 支持 HTTPRoute）、`attachedRoutes`、`Accepted` 条件；
   - 至少有一个结构合法且受支持的 HTTP/HTTPS listener、且 class 可用时写 `Programmed=True`，并发布 `status.addresses`；只有 TCP/TLS/UDP listener 时 listener 为 `Accepted=False/UnsupportedProtocol`，Gateway 不会 Programmed。
3. **HTTPRoute**：每个 `parentRef` 在 `status.parents` 中占一项，计算两个关键条件：
   - `Accepted`：`sectionName`/`port` 是否命中 listener、listener 的 `allowedRoutes` 是否允许该 namespace、Route 与 listener 的 hostname 是否相交；
   - `ResolvedRefs`：`backendRefs` 是否是受支持的 Service、Service 及其端口是否存在、weight 是否合法、跨 namespace 是否有 `ReferenceGrant`。

`allowedRoutes.namespaces.from` 支持 `Same`（默认）、`All`、`Selector`；`SectionName`/`Port` 过滤与 hostname 交集都按 Gateway API 语义处理。`ReferenceGrant` 只允许 `from: {group: gateway.networking.k8s.io, kind: HTTPRoute, namespace: ...}` 到 `to: {group: "", kind: Service, name?: ...}` 的显式授权；没有 grant 时 Route 的 `ResolvedRefs=False/RefNotPermitted`，数据面不会加载它。

### 14.10.3 进程内 L7 路由表

`mini_k8s/network/gateway.py` 的 `GatewayRouter` 只消费“已接受”的 Route（`Accepted=True` 且 `ResolvedRefs=True`），并防御性复核 listener 的 `allowedRoutes`/hostname 与跨 namespace `ReferenceGrant`；即使控制器停止、status 变旧，授权也不会被绕过。实现：

- hostname 精确/通配匹配，并校验请求 host 是否落在 listener hostname 内；
- `Exact`、`PathPrefix`、`RegularExpression` 路径匹配；
- header、method、query 匹配；
- 优先级：hostname 具体度 → 路径类型 → 路径长度 → header/query 数量 → 创建时间；
- `backendRefs` 权重轮询；返回的 Service/namespace/port 可直接交给 `ServiceProxy`，从而串起 `HTTPRoute → Service → Endpoints → Pod`（EndpointSlice 由独立的控制器发布，ServiceProxy 使用的是 Endpoints）。

它不监听真实端口、不终止 TLS，也不实现重定向、重写、镜像、超时等 filter；这些属于数据面实现，MiniK8s 只验证控制面的状态与路由决策。

完整示例：

```bash
python -m mini_k8s.cli apply -f examples/18-gateway-api.yaml
python -m mini_k8s.cli get gatewayclasses
python -m mini_k8s.cli get gateways
python -m mini_k8s.cli get httproutes
```

```python
selected = cluster.gateway.route("shop.example.com", "/api/items")
# {'service': 'shop', 'namespace': 'default', 'port': 80, ...}
```

测试：`tests/test_gateway_api.py` 覆盖 class 接受/拒绝、Gateway 状态与 attachedRoutes、hostname 交集、路径优先级、跨 namespace ReferenceGrant、allowedRoutes、header/method 与加权后端。

MiniK8s 仍不实现真实 HTTP 监听、TLS 终止、GRPCRoute/TLSRoute/TCPRoute 和 Policy Attachment；第 40 章说明差距与替换路线。

## 14.11 生产清单

- 入口控制器多副本、反亲和、PDB、HPA。
- 外部 LB 健康检查指向控制器就绪端口。
- TLS 自动签发与过期监控。
- 路由配置经过 CI 校验（`kubectl apply --dry-run=server`）。
- 限制哪些 namespace 可以创建 Ingress/Gateway（RBAC/准入）。
- 监控 4xx/5xx、延迟、上游健康。
- 变更时连接排空，避免 502。
- 对公网入口做 WAF/DDoS/限流。

## 14.12 Ingress NGINX 的关键注解与调优（存量迁移参考）

> **Ingress NGINX 社区版已于 2026-03 停止维护**，不再发布安全补丁。本节保留是为了帮助存量集群理解和迁移，不是新项目的选型建议。新项目优先 Gateway API；已有注解要逐项映射到 Gateway API 的 `HTTPRoute` 过滤器/`BackendTLSPolicy`/`GRPCRoute`，不能假设可自动移植。

Ingress NGINX 曾经是使用最广的实现，也是“注解即 API”问题的典型。以下是生产最常改的几类注解，以及它们对应的故障。

### 请求体与超时

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "20m"
    nginx.ingress.kubernetes.io/proxy-connect-timeout: "10"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "60"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "60"
```

- 默认 `proxy-body-size` 只有 1m，上传大文件会得到 413。
- 长连接/流式接口需要调大 read timeout；但不要无限大，否则故障连接长期占用 worker。
- 上游超时与客户端超时要分层设置，避免“入口 60s、应用 30s、数据库 5s”互相误判。

### 重写与路径

```yaml
nginx.ingress.kubernetes.io/rewrite-target: /$2
```

配合正则路径使用时，捕获组很容易写错；`rewrite-target` 与 `use-regex` 的组合在不同版本行为有差异。优先在应用侧提供稳定路径，或使用 Gateway API 的 `URLRewrite` 过滤器。

### 限流与熔断

```yaml
nginx.ingress.kubernetes.io/limit-rps: "20"
nginx.ingress.kubernetes.io/limit-burst-multiplier: "5"
nginx.ingress.kubernetes.io/proxy-next-upstream: "error timeout http_502 http_503"
```

限流是入口保护，不是业务配额；真正的租户配额应在 API Gateway/应用层实现。

### TLS 与重定向

```yaml
nginx.ingress.kubernetes.io/ssl-redirect: "true"
nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
nginx.ingress.kubernetes.io/hsts: "true"
```

- 只有配置了 TLS Secret，`ssl-redirect` 才有意义。
- HSTS 一旦下发，浏览器会强制 HTTPS；预发布环境慎用。
- 证书轮换后，Ingress Controller 需要重载；`cert-manager` 通常自动处理。

### 连接排空

```yaml
nginx.ingress.kubernetes.io/upstream-keepalive-connections: "100"
nginx.ingress.kubernetes.io/upstream-keepalive-timeout: "60"
```

滚动发布时，入口到后端的长连接可能导致 502。需要：

- Pod 终止前先从 Endpoints 摘除（readiness 失败/preStop）。
- Ingress 侧连接排空（`worker-shutdown-timeout`）。
- 后端优雅退出，处理 SIGTERM。

### 不要滥用 configuration-snippet

`configuration-snippet`/`server-snippet` 可以注入任意 NGINX 配置，但：

- 绕过 Ingress 资源的类型校验。
- 多团队共享控制器时可能互相影响。
- 升级/迁移到其他控制器不可移植。
- 可能引入安全风险（SSRF、路径穿越）。

生产应优先使用类型化字段，确需 snippet 时做准入限制和审查。

## 14.13 Ingress NGINX 退役与迁移路线

Ingress NGINX 的退役不是“换个镜像”，而是入口架构的迁移。推荐按以下顺序：

1. **盘点**：列出所有 Ingress、注解、TLS Secret、后端协议（HTTP/HTTPS/gRPC）、流量特征（WebSocket、长连接、大文件）。
2. **选目标**：
   - 只需要 HTTP/HTTPS：Envoy Gateway、Traefik、Cilium Gateway、NGINX Gateway Fabric。
   - 需要 gRPC/GRPCRoute、跨 namespace 授权：Gateway API 的 `GRPCRoute` + `ReferenceGrant`。
   - 已有服务网格：Istio Gateway/Kubernetes Gateway API 集成。
3. **用工具做初稿**：`ingress2gateway` 可以把常见 Ingress 转成 Gateway API 资源，但注解语义、重写、限流通常需要手工补齐。
4. **双跑与灰度**：新旧入口同时存在，用 DNS/权重逐步切流；对比 4xx/5xx、延迟、TLS 握手、上传大小。
5. **下线旧控制器**：确认无流量后再删除，保留回滚窗口。
6. **补上类型化策略**：把 `proxy-body-size`、超时、重写、限流映射到 Gateway API 的 `HTTPRoute` filters、`BackendTLSPolicy`、`GRPCRoute` 或实现方策略 CRD。

迁移中最容易丢的行为：

- 注解里的重写/正则（`rewrite-target`、`use-regex`）。
- 大文件上传限制与超时。
- 客户端证书/mTLS、HSTS、重定向。
- 连接排空与长连接。
- 自定义 snippet；这部分通常没有等价物，必须重写。

## 14.14 生产检查清单（入口）

- [ ] 入口控制器仍在维护，有明确的安全响应渠道。
- [ ] 所有路由有 `pathType`/Gateway API 类型化匹配，不依赖隐式正则。
- [ ] TLS 证书轮换、过期告警、HSTS 策略明确。
- [ ] 上传大小、读写超时、连接排空与应用行为匹配。
- [ ] 多团队共享入口时有 namespace/Route 授权边界（`allowedRoutes`/`ReferenceGrant`）。
- [ ] 有 4xx/5xx、延迟、上游健康、证书到期监控。
- [ ] 迁移/回滚路径经过演练。

MiniK8s 的 Ingress 只做 host/path 路由，不实现上述注解；这些差异正是“Ingress 可移植性差”的具体表现。

## 本章小结

- Ingress 是 L7 路由的最小标准，但依赖控制器和 annotations。
- Gateway API 通过 GatewayClass/Gateway/Route 实现角色分离和类型化配置。
- TLS、路径匹配、控制器高可用是生产入口的关键。
- 南北向与东西向流量治理要分开选型。

## 延伸阅读

- [第 13 章 Service、Endpoints 与 DNS](13-service-endpoints-dns.md)
- [第 16 章 服务网格与零信任](16-service-mesh-zero-trust.md)
- [第 38 章 网络、存储与安全实现](38-network-storage-security.md)
- [示例 18：Gateway API 路由](../examples/18-gateway-api.yaml)：可执行的 GatewayClass/Gateway/HTTPRoute 场景。
