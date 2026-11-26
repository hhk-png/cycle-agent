# 第 21 章 安全与多租户

## 21.1 威胁模型

Prometheus 的攻击面：

- HTTP API/UI（未授权查询、admin 删除、reload）；
- `/metrics`（信息泄露）；
- 抓取目标（SSRF、凭证泄露）；
- remote write receiver（未授权写入/投毒）；
- federation（数据泄露）；
- 配置文件（密码/token）；
- TSDB 数据目录（篡改/删除）；
- 供应链（镜像/依赖）。

## 21.2 Web TLS 与认证

Prometheus 支持 `--web.config.file`：

```yaml
tls_server_config:
  cert_file: /etc/prometheus/tls/server.crt
  key_file: /etc/prometheus/tls/server.key
  min_version: TLS12
  cipher_suites:
    - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
    - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384

basic_auth_users:
  prometheus: $2y$10$...   # bcrypt hash
```

生成密码：

```bash
htpasswd -nBC 10 "" | tr -d ':\n'
```

注意：

- `basic_auth_users` 的值是 bcrypt hash，不是明文；
- 配置文件权限 0600；
- 证书轮换后需要 reload 或重启；
- 仅在反向代理无法提供 TLS 时使用内置 TLS。

## 21.3 反向代理与 SSO

生产常见模式：

```text
Grafana/用户 → OAuth2 Proxy / nginx → Prometheus
```

- 反向代理负责 OIDC/SAML、RBAC、审计；
- Prometheus 只监听 127.0.0.1 或集群内；
- 用 `--web.external-url` 生成正确的回链；
- 用 `--web.route-prefix` 处理子路径；
- 限制 `/api/v1/admin/*` 只对管理员开放。

Prometheus 自身没有用户/角色模型；多租户需要 Mimir/Cortex/Thanos 等。

## 21.4 Admin API 与 Lifecycle

危险端点：

```text
/api/v1/admin/tsdb/delete_series
/api/v1/admin/tsdb/snapshot
/api/v1/admin/tsdb/clean_tombstones
/-/reload
/-/quit
```

建议：

- 默认不开启 `--web.enable-admin-api`；
- `--web.enable-lifecycle` 只在需要时开启；
- 用反向代理限制来源 IP/身份；
- 删除操作要审计（谁、何时、删了什么）；
- 快照目录权限受限。

## 21.5 抓取安全

### 凭证

```yaml
scrape_configs:
  - job_name: secure
    scheme: https
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/secrets/secure-password
    tls_config:
      ca_file: /etc/prometheus/tls/ca.crt
      cert_file: /etc/prometheus/tls/client.crt
      key_file: /etc/prometheus/tls/client.key
      server_name: secure.internal
```

- 优先 `*_file` 而不是内联密码；
- 限制 secret 文件权限；
- 不要在 Git 中提交明文；
- 用 Vault/SealedSecrets/External Secrets 注入。

### SSRF

Prometheus 能访问其网络可达的任何地址。攻击者若可控服务发现/relabel，可能：

- 让 Prometheus 抓取云元数据服务（169.254.169.254）；
- 扫描内网；
- 读取内部管理接口。

防护：

- 限制 SD 来源与 relabel 权限；
- 用网络策略/防火墙限制 Prometheus 出站；
- 不要允许用户任意修改 scrape 配置；
- 对 `file_sd`/`http_sd` 的写入权限做严格控制。

## 21.6 Remote Write Receiver 安全

启用 `--web.enable-remote-write-receiver` 后，任何能访问该端口的人都能写入样本：

- 用 TLS + 认证；
- 只允许可信网络；
- 校验标签/指标名（接收端或前置代理）；
- 防止高基数投毒；
- 监控写入速率/序列增长。

Mini 的 `/api/v1/write` 默认无认证，仅用于教学；生产必须加保护。

## 21.7 多租户

单机 Prometheus 没有租户概念：

- 所有序列在同一个 TSDB；
- 任何查询都能看到所有数据；
- 没有租户级配额/隔离。

多租户方案：

- **Mimir/Cortex**：租户 ID header、每租户限额、查询隔离；
- **Thanos**：多租户需要额外组件/网关；
- **每租户一个 Prometheus**：隔离强、成本高；
- **反向代理 + 标签过滤**：弱隔离，容易绕过。

选型时明确：

- 租户是团队/业务/外部客户？
- 是否需要租户级 retention/配额？
- 是否需要跨租户全局查询？
- 合规要求（数据驻留/审计）？

## 21.8 Kubernetes 安全

- Prometheus 的 ServiceAccount 只需要 `get/list/watch` 目标资源；
- 不要给它 `secrets` 读权限；
- 用 NetworkPolicy 限制 scrape 入站/出站；
- PrometheusRule 的变更要走 GitOps/审批；
- Grafana 数据源凭证用 Secret；
- 镜像固定 digest，定期扫描 CVE。

## 21.9 供应链与升级

- 从官方 release 下载并校验 checksum/signature；
- 容器镜像用官方仓库/可信镜像；
- 关注 Prometheus 安全公告；
- 升级前读 changelog，尤其是 flag 移除/行为变更；
- 保留上一版本二进制以便回滚；
- 备份配置/TSDB 快照。

## 21.10 安全基线清单

- [ ] Prometheus 不直接暴露公网；
- [ ] TLS + 认证（内置或反代）；
- [ ] admin/lifecycle 默认关闭；
- [ ] remote write receiver 有认证/网络限制；
- [ ] 抓取凭证用文件/Secret，不入库；
- [ ] SD/relabel 变更受控；
- [ ] 出站网络策略限制 SSRF；
- [ ] TSDB 目录权限最小化；
- [ ] 审计删除/重载操作；
- [ ] 定期升级/漏洞扫描。

## 21.11 Web 配置与 TLS 细节

`--web.config.file` 的完整结构：

```yaml
tls_server_config:
  cert_file: /etc/prometheus/tls/server.crt
  key_file: /etc/prometheus/tls/server.key
  client_ca_file: /etc/prometheus/tls/clients-ca.crt
  client_auth_type: RequireAndVerifyClientCert
  min_version: TLS12
  max_version: TLS13
  cipher_suites:
    - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256

http_server_config:
  http2: true
  headers:
    X-Frame-Options: deny
    X-Content-Type-Options: nosniff
    Content-Security-Policy: "default-src 'self'"

basic_auth_users:
  admin: $2y$10$...
```

要点：

- `client_auth_type` 可做 mTLS（`RequireAndVerifyClientCert`）；
- `headers` 加安全响应头，但不能替代认证；
- `http2: false` 可规避某些代理/客户端问题；
- 配置文件含 hash/证书路径，权限 0600；
- 内置 basic auth 只有"一个用户名/密码"，没有角色；
- 证书轮换要 reload/重启，提前测试。

## 21.12 SSO/OIDC 与 RBAC 模式

Prometheus 没有 RBAC，常见组合：

```text
用户 → OIDC Provider → oauth2-proxy/网关 → Prometheus
                         │
                         ├─ 注入 X-Forwarded-User
                         ├─ 按组/角色限制路径
                         └─ 记录审计日志
```

模式：

- **只读 vs 管理**：代理按 HTTP 方法/路径区分，`/api/v1/admin/*` 只允许管理员；
- **团队隔离**：每个团队一个 Prometheus/Mimir 租户，Grafana 数据源权限控制；
- **查询限制**：代理限制 `query` 长度/时间范围/并发；
- **审计**：代理记录用户、查询、时间、结果状态。

注意：

- 不要信任客户端自带的 `X-Forwarded-User`，必须由代理覆盖；
- Grafana 的 `auth.proxy` 也要防伪造；
- OIDC token 过期/刷新要处理；
- 多租户查询头（`X-Scope-OrgID`）不能由最终用户任意设置。

## 21.13 Secret 管理与轮换

原则：

- Secret 不进 Git、不进镜像、不进日志；
- 用 `*_file` 而不是内联；
- 文件权限 0400/0600，owner 为 Prometheus 用户；
- 用 Vault/External Secrets/SealedSecrets 注入；
- 轮换要有流程和回滚。

轮换示例：

```text
1. 在目标端创建新凭证（旧凭证仍有效）
2. 更新 Secret/文件
3. reload Prometheus（或等下次抓取）
4. 验证抓取成功
5. 吊销旧凭证
6. 记录轮换时间/负责人
```

注意：

- `password_file`/`bearer_token_file` 在每次抓取时读取（通常），无需重启；
- TLS 证书轮换可能需要 reload；
- OAuth2 token 缓存可能导致旧 token 继续使用到过期；
- 轮换失败要有告警（`up=0`/401/403）。

## 21.14 审计与取证

Prometheus 自身不记录查询审计。至少记录：

```text
- 谁（身份/来源 IP）
- 何时
- 访问哪个端点
- 查询/参数（注意脱敏）
- 结果状态/耗时/字节数
- 管理操作（删除/快照/reload）
```

存储：

- 与指标分开，单独 retention；
- 防篡改（WORM/对象锁/日志服务）；
- 访问受控；
- 保留期满足合规（通常长于指标）。

取证时注意：

- 保留原始日志/快照/配置；
- 记录时间线；
- 不要在被污染的系统上分析；
- 与法务/安全团队协作。

## 21.15 威胁建模与安全测试

用 STRIDE 过一遍：

| 威胁 | 例子 | 控制 |
|---|---|---|
| Spoofing | 伪造代理头/租户头 | mTLS、签名、可信代理 |
| Tampering | 改配置/删除数据 | GitOps、RBAC、审计 |
| Repudiation | 否认删除操作 | 审计日志、签名 |
| Information disclosure | 查询敏感标签 | 最小权限、脱敏、网络隔离 |
| Denial of service | 大查询/高基数 | 限额、并发、基数治理 |
| Elevation of privilege | admin API 暴露 | 关闭/限制/认证 |

测试：

- 未认证访问 `/`, `/api/v1/query`, `/api/v1/admin/*`；
- 伪造 `X-Forwarded-User`/`X-Scope-OrgID`；
- SSRF：让 Prometheus 抓 `169.254.169.254`/内网；
- 大查询/高基数压测；
- 证书过期/错误 CA；
- Secret 权限/日志泄露；
- 依赖/镜像 CVE 扫描。

## 21.16 多租户配额与隔离

如果使用 Mimir/Cortex 等多租户后端，配额通常包括：

```text
ingestion rate / burst
max series per tenant
max label names/values
query length / concurrency
retention per tenant
```

工程要点：

- 租户 ID 由网关注入，不能由用户随意指定；
- 每租户的 remote write 队列/凭证隔离；
- 查询层按租户过滤，防止跨租户泄漏；
- 配额告警要提前（80%）；
- 大租户与小租户的资源隔离（shuffle sharding）；
- 删除/保留策略按租户配置。

单机 Prometheus 只能用"实例/命名空间隔离"模拟多租户，强度和成本都不如专用后端。

## 本章小结

- Prometheus 自带 TLS/basic auth，但企业级 SSO/RBAC 通常靠反向代理。
- Admin/lifecycle/remote write receiver 是高风险端点，默认应关闭或严格限制。
- 抓取配置能触发 SSRF，SD/relabel 权限必须受控。
- 单机没有多租户；多租户需要 Mimir/Thanos 或实例隔离。
- 安全是配置、网络、凭证、供应链的组合，不是单一开关。

## 延伸阅读

- Prometheus 官方 "Security" 与 `--web.config.file` 文档。
- Kubernetes NetworkPolicy/RBAC 文档。
- 本教程第 49 章（合规、数据生命周期与审计）与附录 L（安全基线）。
