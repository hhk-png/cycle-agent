# 附录 L 安全基线

> 第 21 章讲"为什么"与威胁模型，本附录给"怎么配"：一份可以直接抄进仓库的安全基线，覆盖网络、认证、抓取、高风险端点、Kubernetes、数据、供应链与响应。基线分三档：小团队最小基线、生产基线、受监管环境基线。

## L.1 威胁模型：先画资产与攻击面

Prometheus 的安全问题通常不是"数据库被拖库"，而是下面几类：

| 资产 | 攻击面 | 典型后果 |
|---|---|---|
| 指标数据 | 查询 API、Grafana、联邦、remote read | 泄露拓扑、业务量、错误率、租户信息 |
| 抓取凭证 | 配置文件、Secret、日志、Git 历史 | 横向访问数据库/中间件/云 API |
| 写入口 | remote write receiver、OTLP receiver、Pushgateway | 伪造指标、污染告警、基数炸弹 |
| 管理能力 | admin API、lifecycle、配置重载 | 删除数据、触发重启、篡改规则 |
| 供应链 | 二进制/镜像/依赖 | 植入后门、CVE |
| 监控盲区 | Prometheus 自身不可观测 | 攻击/故障长期不被发现 |

建模时至少回答：

1. 谁能访问 `/api/v1/*`、`/graph`、`/federate`、`/metrics`？
2. 谁能写 remote write/OTLP/Pushgateway？
3. 抓取凭证存在哪里、谁能读、如何轮换？
4. 谁可以改配置/规则、变更是否留痕？
5. 指标里是否混入了用户 ID、URL、错误文本等敏感/高基数数据？

## L.2 网络与部署基线

- [ ] Prometheus 不直接暴露公网；
- [ ] 监听 `127.0.0.1` 或集群内地址，公网入口只走反向代理/网关；
- [ ] 反向代理提供 TLS、SSO、RBAC、审计；
- [ ] NetworkPolicy/防火墙限制入站与**出站**；
- [ ] 容器以非 root 运行，只读根文件系统（除 TSDB/配置目录）；
- [ ] 镜像固定 digest，不使用 `latest`；
- [ ] 明确 `--web.external-url` 与 `--web.route-prefix`，避免反代后链接/回调错误；
- [ ] 3.16+ 启用 TLS 的 `--web.config.file` 时，`--web.external-url` 默认变 `https`，反代场景显式设置；
- [ ] 代理层限制请求体大小、并发、超时，防止慢查询/大响应拖垮实例。

推荐拓扑：

```text
用户/ Grafana
      │ HTTPS + SSO
      ▼
反向代理 / API 网关（认证、限流、审计）
      │ 内网 HTTP 或 mTLS
      ▼
Prometheus（只监听内网）
      │ 出站
      ▼
exporters / 中间件 / 云 API
```

## L.3 Web 认证与 TLS：`web.config.file`

Prometheus 本身没有内置的用户体系/RBAC，但提供 TLS、basic auth 和安全响应头。生产通常把 SSO/RBAC 放在反向代理，Prometheus 只做最后一道认证。

```yaml
tls_server_config:
  cert_file: /etc/prometheus/tls/server.crt
  key_file: /etc/prometheus/tls/server.key
  client_auth_type: NoClientCert
  min_version: TLS12
  max_version: TLS13
  prefer_server_cipher_suites: true
http_server_config:
  http2: true
  headers:
    X-Content-Type-Options: nosniff
    X-Frame-Options: deny
    Referrer-Policy: no-referrer
basic_auth_users:
  prometheus: $2y$10$REPLACE_WITH_BCRYPT_HASH
```

要点：

- `--web.config.file` 权限 `0600`，属主为 Prometheus 进程用户；
- 密码只存 bcrypt 哈希，不要明文；生成后用 `promtool`/`htpasswd -bnBC 10` 验证；
- `basic_auth_users` 只有"认证"，没有授权：所有登录用户看到同样的数据；
- 需要按用户/团队隔离时，用反向代理 + OIDC + 每租户独立实例，或 Mimir 的多租户；
- 需要客户端证书时用 `client_auth_type: RequireAndVerifyClientCert` + `client_ca_file`；
- 证书轮换：先加载新证书、保留旧证书一个 TTL，再删旧证书；自动化要有失败回滚；
- 反代到 Prometheus 的一段尽量走回环/内网；跨节点用 mTLS；
- 安全头不要与 Grafana/前端冲突，按实际页面测试 CSP。

## L.4 抓取认证：basic、bearer、OAuth2、mTLS

抓取凭证的优先级：`*_file` > Secret 挂载 > 环境变量 > 明文配置。明文只允许出现在本地实验。

### L.4.1 Basic auth / Bearer

```yaml
scrape_configs:
  - job_name: app
    static_configs:
      - targets: ["app:8080"]
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/secrets/app-password
    # 或者
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/app-token
```

### L.4.2 OAuth2 client credentials

```yaml
scrape_configs:
  - job_name: cloud-api
    static_configs:
      - targets: ["api.example.com"]
    scheme: https
    oauth2:
      client_id: prometheus
      client_secret_file: /etc/prometheus/secrets/oauth-client-secret
      token_url: https://auth.example.com/oauth2/token
      scopes: ["metrics.read"]
      endpoint_params:
        audience: metrics
      tls_config:
        ca_file: /etc/prometheus/tls/ca.crt
```

### L.4.3 mTLS 与 TLS 校验

```yaml
    tls_config:
      ca_file: /etc/prometheus/tls/ca.crt
      cert_file: /etc/prometheus/tls/client.crt
      key_file: /etc/prometheus/tls/client.key
      server_name: app.internal
      insecure_skip_verify: false
      min_version: TLS12
```

- `insecure_skip_verify: true` 只用于临时排障，且要有到期时间；
- 私有 CA 要固定 `ca_file`，不要依赖系统信任库的隐式变化；
- 证书过期要告警：`probe_ssl_earliest_cert_expiry`（blackbox）或自建 exporter；
- 凭证文件轮换后确认 Prometheus 重新读取（多数 `*_file` 在每次使用时读取；仍建议 reload 验证）；
- 不要把凭证写进 `prometheus.yml` 的 Git 版本；用 SealedSecret/External Secrets/Vault Agent 注入。

## L.5 高风险端点

| 端点/开关 | 风险 | 基线 |
|---|---|---|
| `--web.enable-admin-api` | 删除序列、清理 tombstone、快照 | 默认关闭；只在受控网络、短时开启 |
| `--web.enable-lifecycle` | `/-/reload`、`/-/quit` | 默认关闭；用 SIGHUP/编排系统替代 |
| `--web.enable-remote-write-receiver` | 任意写入、基数炸弹 | 默认关闭；必须加认证/网络限制 |
| `--web.enable-otlp-receiver` | 任意 OTLP 写入 | 默认关闭；限制来源与体积 |
| `/federate` | 暴露聚合指标 | 只对可信上游开放，配 matcher 限制 |
| `/api/v1/status/config` | 配置泄露（可能含敏感字段） | 反代鉴权；确认脱敏 |
| `/api/v1/targets`、`/api/v1/metadata` | 目标/标签/元数据泄露 | 按需开放，避免公网 |
| `/metrics` | Prometheus 自身指标 | 只对监控网络开放 |

remote write receiver 的最小防护：

```yaml
# 反向代理层：只允许指定来源 + bearer token
# Prometheus 自身没有写入鉴权，必须由代理/网络策略承担
```

- 写入端做 relabel/allowlist，拒绝未知指标名；
- 限制请求体大小与每秒样本数；
- 监控 `prometheus_target_scrapes_exceeded_sample_limit_total` 等异常；
- 对公网/多租户场景，优先用 Mimir/Cortex 的租户与配额，而不是单机 receiver。

## L.6 SSRF 与抓取出站

抓取本质上是"由 Prometheus 发起任意 HTTP 请求"，因此是天然的 SSRF 面：

- [ ] 目标清单只能来自可信 SD（Kubernetes/文件/CMDB），不要让低权限用户写 SD；
- [ ] relabel 不允许把 `__address__` 改到元数据服务/管理网段（如 `169.254.169.254`、`127.0.0.1`、`::1`、内部 Admin 端口）；
- [ ] 3.15+ 的 `__unix_socket__` 只由可信 SD 设置，路径做前缀 allowlist，拒绝 `../`、抽象 socket 和敏感目录（该标签直接拨本地 socket，属于 SSRF/本地权限面）；
- [ ] 出站防火墙只放行 exporter 端口与 DNS/OAuth2 端点；
- [ ] 对不可信目标设 `follow_redirects: false`，或限制重定向目标；
- [ ] 代理场景显式配置 `proxy_url`/`proxy_from_environment`/`no_proxy`；
- [ ] 抓取超时、响应体大小、样本数/标签数限制全部开启；
- [ ] 定期审计 target 列表与 relabel 规则，防止"合法配置"变成扫描器。

```yaml
scrape_configs:
  - job_name: untrusted
    static_configs:
      - targets: ["third-party:443"]
    scheme: https
    follow_redirects: false
    scrape_timeout: 5s
    sample_limit: 5000
    label_limit: 30
    body_size_limit: 1048576
```

## L.7 Kubernetes 安全

- [ ] ServiceAccount 只授予 `get/list/watch` 必要的 `pods/services/endpoints/endpointslices/nodes`；
- [ ] 不授予 `secrets` 读权限；凭证用外部 Secret 注入；
- [ ] 用 namespace 级 RBAC 限制 Operator 的监视范围；
- [ ] Operator CRD（ServiceMonitor/PodMonitor/PrometheusRule）变更走 GitOps/审批；
- [ ] PrometheusRule 评审：避免高基数查询、避免 `absent()` 误报、限制 `for`；
- [ ] NetworkPolicy：默认拒绝，按 job 放行出站；
- [ ] Pod Security：非 root、只读根文件系统、drop capabilities、seccomp；
- [ ] Grafana 数据源凭证用 Secret，不写进 dashboard JSON；
- [ ] 多租户用独立实例或 Mimir 租户，不要靠 Grafana 文件夹做隔离。

最小 RBAC 示例（按实际 CRD 调整）：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: prometheus-sd
  namespace: observability
rules:
  - apiGroups: [""]
    resources: ["pods", "services", "endpoints"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list", "watch"]
# nodes 是集群级资源：若确实需要节点发现，另建 ClusterRole 并做最小绑定，
# 不要把它塞进 namespace Role（不会生效）。
```

## L.8 多租户与隔离

单机 Prometheus 没有租户概念。可选边界：

| 模式 | 隔离强度 | 成本 | 适用 |
|---|---|---|---|
| 每租户独立 Prometheus | 强 | 高 | 强合规、少量租户 |
| 共享 Prometheus + 命名空间/标签 | 弱 | 低 | 内部可信团队 |
| Mimir/Cortex 租户 | 中强 | 中 | 多团队 SaaS/平台 |
| 每租户独立查询层 | 中 | 中 | 查询隔离要求高 |

基线：

- [ ] 明确租户边界与数据归属；
- [ ] 查询隔离/配额（最大序列数、最大样本数、超时）；
- [ ] 写入配额（每秒样本数、活跃序列数）；
- [ ] 数据导出/删除权限分离；
- [ ] 跨租户查询默认禁止，确需时走审批与审计；
- [ ] 数据驻留/跨境符合合同与法规；
- [ ] 租户 ID 不进指标标签（除非低基数且必要），防止基数与泄露。

## L.9 数据、备份与删除

- [ ] TSDB 目录权限最小（仅 Prometheus 用户可读写）；
- [ ] 磁盘/对象存储加密（LUKS/KMS/SSE）；
- [ ] 快照/备份加密，备份账号只写不读；
- [ ] retention 与合规保留期一致；删除要有审计；
- [ ] `delete_series` 后执行 `clean_tombstones`，并验证空间释放；
- [ ] 远端存储的删除传播（Thanos/Mimir 的 delete API）纳入流程；
- [ ] 备份恢复演练：block 版本、权限、路径、校验和；
- [ ] 指标中禁止出现用户 ID、token、完整 URL、错误文本、IP 等敏感/高基数数据；
- [ ] 需要保留高基数细节时，放日志/追踪，不放进标签。

## L.10 供应链与升级

- [ ] 只从官方 release/受信镜像仓库获取二进制与镜像；
- [ ] 校验 checksum/签名；镜像固定 digest；
- [ ] 生成/保存 SBOM，定期扫描 CVE；
- [ ] 升级前读 `CHANGELOG.md` 的 `[CHANGE]`/`[SECURITY]`；
- [ ] 先在 staging 用同数据量/同查询集回归；
- [ ] 保留旧二进制、旧配置、旧快照与回滚路径；
- [ ] 存储格式变化（XOR2/ST）确认所有读取端兼容后再上线；
- [ ] LTS 版本用于保守生产，常规版本用于需要新特性的场景。

## L.11 监控、审计与响应

Prometheus 自身要被监控，安全事件才有可见性：

- [ ] 反代记录认证失败、4xx/5xx、异常查询、慢查询；
- [ ] 监控 `/api/v1/admin/*` 调用（应极少）；
- [ ] 监控 remote write/OTLP 写入量突增、未知指标名、基数增长；
- [ ] 监控配置/规则变更（Git 提交 + 部署记录 + reload 日志）；
- [ ] 监控证书/凭证过期时间；
- [ ] 监控 Prometheus 自身 `up`、WAL 损坏、磁盘、内存、规则错误；
- [ ] 告警通道与 Prometheus 解耦（元监控）；
- [ ] 安全事件 runbook：隔离、取证、轮换凭证、回滚、复盘；
- [ ] 定期渗透/配置审计/依赖扫描。

自监控查询示例：

```promql
# 规则错误
sum by (rule_group) (prometheus_rule_group_last_evaluation_timestamp_seconds) == 0

# 抓取失败
sum by (job) (up == 0)

# 写入突增（remote write receiver）
rate(prometheus_remote_storage_samples_in_total[5m])

# 查询错误/超时（如有对应指标）
rate(prometheus_http_requests_total{code=~"5.."}[5m])
```

## L.12 基线分级

### L.12.1 小团队最小基线

```text
1. 不暴露公网
2. 反代 TLS + SSO
3. admin/lifecycle/remote-write receiver 默认关闭
4. 凭证用 Secret/*_file，不进 Git
5. 出站限制 + sample/label/body limit
6. 备份/升级流程
7. 监控 Prometheus 自身
```

### L.12.2 生产基线

```text
最小基线 +
8. web.config.file + mTLS/SSO + 安全头
9. NetworkPolicy 默认拒绝
10. Kubernetes RBAC 最小化
11. 配置/规则 GitOps + 审计
12. 证书/凭证轮换与过期告警
13. 多租户边界（实例或 Mimir）
14. 事故响应 runbook
```

### L.12.3 受监管环境

```text
生产基线 +
15. 数据分类/最小化/保留期
16. 删除请求与删除传播验证
17. 审计日志保留与不可篡改
18. 数据驻留/跨境控制
19. SBOM/供应链证明
20. 定期第三方审计/渗透测试
```

## L.13 常见错误

- **只加 TLS 不加认证**：TLS 只保护传输，不阻止内网访问；
- **把 basic auth 当 RBAC**：所有用户仍能看全部指标、调用全部 API；
- **remote write receiver 直接暴露**：任何人都能写入/打爆基数；
- **在标签里放用户 ID/token/URL**：既高基数又泄露；
- **`insecure_skip_verify: true` 永久保留**：MITM 风险；
- **把 `--web.enable-admin-api` 当日常工具**：删除/快照接口没有细粒度授权；
- **忽略出站 SSRF**：relabel 可以把 Prometheus 变成内网扫描器；
- **备份不加密/不演练**：出事时才发现恢复不了；
- **升级不看存储格式兼容**：回滚时旧版本读不了新 block；
- **不监控 Prometheus 自身**：监控系统先挂，业务告警全哑。

## 延伸阅读

- 本教程第 21 章（安全与多租户）、第 49 章（合规、生命周期与审计）。
- Prometheus `--web.config.file`、`scrape_config`、`remote_write` 官方文档。
- Kubernetes RBAC/NetworkPolicy/Pod Security Standards 文档。
- NIST SP 800-190（容器安全）、CIS Benchmarks（Kubernetes）。
