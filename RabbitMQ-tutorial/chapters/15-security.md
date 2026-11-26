# 第 15 章 安全：认证、授权、TLS 与审计

> 本章定位：消息系统承载的是业务事实，不是“无害日志”。安全边界包括网络、传输、身份、权限、管理面、密钥和审计。默认配置几乎总是不能直接上生产。

## 威胁模型

先问“谁可能攻击什么”：

- 未认证的互联网访问者；
- 被攻陷的业务应用；
- 有网络访问权但权限过大的内部服务；
- 恶意/误操作的管理员；
- 供应链（镜像、插件、客户端库）；
- 物理/云平台层面的磁盘与网络；
- 消息内容中的敏感数据（PII、支付信息）。

不同威胁对应不同控制：

| 威胁 | 控制 |
|---|---|
| 窃听 | TLS、网络隔离 |
| 冒充客户端 | PLAIN + TLS、mTLS、OAuth |
| 越权访问队列 | vhost ACL、topic authorization |
| 管理面暴露 | 不公网、HTTPS、强认证、审计 |
| 密钥泄露 | 轮换、最小权限、密钥管理 |
| 供应链 | 镜像签名、固定版本、扫描 |
| 数据泄露 | 消息加密/脱敏、保留策略 |

## 认证

RabbitMQ 支持多种认证机制：

- **PLAIN**：用户名/密码。RabbitMQ 默认允许 PLAIN over 明文，不会强制 TLS；生产必须用策略/网络配置要求 TLS，否则密码在网络上明文传输。
- **AMQPLAIN**：旧式 SASL，兼容性用途，不推荐新系统。
- **EXTERNAL**：依赖 TLS 客户端证书，适合 mTLS。
- **OAuth 2.0**：通过插件接入身份提供商，适合集中身份管理。
- **LDAP / HTTP backend**：通过插件对接企业目录/认证服务。

内部用户数据库适合小规模；大规模/企业环境通常接 OAuth/LDAP。无论哪种，都要：

- 禁用/限制 guest；
- 强密码/密钥轮换；
- 最小权限；
- 监控失败登录；
- 避免在代码/日志中泄露凭据。

### guest 用户

RabbitMQ 默认创建 `guest/guest`，且默认只允许从 localhost 登录。不要把 guest 用于生产，也不要把管理端口暴露到公网后继续用 guest。创建独立用户、独立 vhost、最小权限。

## 授权

RabbitMQ 的 vhost 权限有三类：

- **configure**：声明/删除交换机、队列、绑定、策略；
- **write**：向交换机发布；
- **read**：从队列消费/拉取。

权限值是正则，匹配资源名：

```bash
rabbitmqctl set_permissions -p /app app "^order\." "^order\." "^order\."
```

设计原则：

1. 每个服务独立用户；
2. 只允许自己域的交换机/队列；
3. 生产/预发/开发用不同 vhost；
4. 禁止通配 `.*` 除非确实需要；
5. 定期审计权限；
6. 删除离职/下线服务用户；
7. 管理 API 写权限与数据面分开。

注意：vhost 权限不限制消息内容，也不阻止同 vhost 内被授权用户读取其他队列。需要更强隔离时，使用独立 vhost/集群或 topic authorization 插件。

## TLS

AMQP over TLS 默认端口 5671。关键配置：

- 服务端证书/私钥；
- CA 证书；
- `verify_peer`、`fail_if_no_peer_cert`；
- 客户端证书（mTLS）；
- 协议版本/加密套件；
- 证书轮换；
- 主机名验证。

不要使用自签名证书却不校验客户端；不要关闭证书验证来“解决连接问题”。TLS 配置错误应在预发环境修复，而不是生产降级。

管理 UI/API 也应使用 HTTPS，通常由反向代理终止 TLS。不要让 15672 直接暴露公网。Erlang distribution 端口（25672）和 epmd（4369）只对集群节点开放。

## 网络安全

- 把 broker 放在私有子网；
- 安全组/网络策略只允许应用和运维网段；
- 管理面单独网段/VPN；
- 禁止公网访问 15672、25672、4369；
- 跨区域使用 TLS/VPN/专线；
- 限制出站（Shovel/Federation 上游）；
- 监控异常连接和端口扫描。

Kubernetes 中使用 NetworkPolicy 限制 namespace/pod 访问；不要依赖 Service 的 ClusterIP 作为安全边界。

## 管理面安全

管理 API 能删除队列、发布消息、修改策略，权限极高。保护措施：

- HTTPS；
- 强认证/OAuth/SSO；
- 只允许运维网段；
- 最小权限用户；
- 审计所有写操作；
- 禁用不需要的端点/插件；
- 不在浏览器保存长期凭据；
- 定期轮换 API 凭据。

管理 UI 的“publish message”功能可以绕过业务校验，生产应限制。

## 密钥与证书轮换

需要管理的密钥：

- 用户密码；
- TLS 服务端/客户端证书；
- CA；
- OAuth client secret/token；
- Erlang cookie；
- 备份加密密钥；
- 云厂商访问密钥。

轮换计划：

1. 建立新密钥/证书；
2. 在预发验证；
3. 双证书/双密码过渡（若支持）；
4. 滚动更新节点/客户端；
5. 撤销旧密钥；
6. 审计使用情况。

Erlang cookie 轮换会影响集群节点互信，必须按集群变更流程执行，不能单节点随意改。

## 审计与合规

RabbitMQ 的日志、管理事件、连接/频道信息和外部 SIEM 可以组成审计链：

- 谁创建/删除了队列；
- 谁修改了 policy/permission；
- 哪个用户/连接发布了异常消息；
- 认证失败/权限拒绝；
- 管理 API 写操作；
- 证书/密钥变更；
- 数据保留/删除。

日志中不要记录消息正文和密码。需要追踪消息时，用 message_id/correlation_id，并对敏感字段脱敏。

合规场景还要考虑：

- 数据驻留（消息是否跨区域复制）；
- 保留期限；
- 加密（传输/静态）；
- 访问审批；
- 备份加密；
- 删除请求（消息/日志中的 PII）。

## 合规映射：从技术控制到审计证据

不同合规框架（GDPR、PCI DSS、HIPAA、SOC 2、等保 2.0）条款不同，但落到消息系统时，控制面高度重叠：

| 控制面 | RabbitMQ 手段 | 审计证据 |
|---|---|---|
| 身份 | 独立用户、OAuth/OIDC、LDAP、mTLS | 用户列表、IdP 配置、证书 |
| 授权 | vhost ACL、topic authorization、管理/数据面分离 | 权限导出、定期评审 |
| 传输加密 | TLS/AMQPS、mTLS、管理 HTTPS | 配置、证书链、扫描报告 |
| 静态加密 | 云盘/KMS、PVC、备份加密 | 加密配置、密钥策略 |
| 审计 | 管理事件、日志、SIEM、Firehose（短期） | 日志样本、保留策略 |
| 保留/删除 | TTL、max-length、Stream retention、DLQ 清理 | policy 导出、清理记录 |
| 数据驻留 | 集群/备份/复制区域、Federation 方向 | 拓扑图、区域清单 |
| 可用性/完整性 | Quorum、备份、DR、恢复演练 | RPO/RTO 报告、演练记录 |
| 供应商 | 合同、SLA、安全公告、退出条款 | DPA、SOC 2 报告、支持合同 |

三个最容易出错的点：

1. **TLS 不等于合规**：它只覆盖传输加密，不覆盖访问、审计、保留、删除和驻留；
2. **删除队列不等于删除数据**：Quorum 副本、Stream segment、DLQ、备份、日志和下游都可能保留；
3. **header/routing key 也可能含个人信息**：日志脱敏不能只看正文。

合规不是“买一个产品”，而是“数据分类 + 控制 + 证据 + 演练”。完整的框架映射、删除权传播、审计访谈问题和检查清单见[附录 Y](../appendices/Y-compliance-data-governance.md)；跨区域数据驻留与灾备见[附录 U](../appendices/U-disaster-recovery-continuity.md)。

## 供应链与插件

- 固定 RabbitMQ/Erlang/插件版本；
- 使用官方镜像/签名包；
- 扫描 CVE；
- 审核第三方插件（认证后端、一致性哈希、管理扩展等）；对依赖 Mnesia 的历史插件（如延迟交换机）要确认在 4.3+ 是否还能工作；
- 限制插件启停权限；
- 备份插件配置；
- 升级前验证插件兼容性。

插件是代码，拥有 broker 进程权限。不要安装来源不明的插件。

## Mini-RabbitMQ 的安全实现

Mini 实现了：

- PLAIN 认证；
- PBKDF2-HMAC-SHA256 密码哈希（带盐、迭代次数）；
- 用户 tags；
- vhost configure/write/read 正则 ACL；
- guest 仅限 loopback；
- 管理 API Basic Auth；
- 连接/信道权限检查；
- 用户/权限持久化。

测试在 `tests/test_auth.py`、`tests/test_wire_server.py`、`tests/test_management.py`，示例在 `examples/17_security_acl.py`。

Mini 没有实现：

- TLS 自动化测试（服务端支持传入 SSLContext，但默认不启用）；
- OAuth/LDAP/mTLS；
- topic authorization；
- 审计日志/SIEM；
- 速率限制/账户锁定；
- 密钥轮换；
- 管理面 HTTPS。

这些缺口在第 25 章统一列出。不要把 Mini 的 ACL 当成生产安全方案。

## 安全检查清单

- [ ] 禁用/限制 guest；
- [ ] 每服务独立用户、最小权限；
- [ ] 生产/非生产 vhost 隔离；
- [ ] TLS 全链路，校验证书；
- [ ] 管理面不暴露公网；
- [ ] 集群端口仅节点互通；
- [ ] 密钥/证书有轮换计划；
- [ ] 管理写操作审计；
- [ ] 插件来源可信、版本固定；
- [ ] 备份加密、恢复演练；
- [ ] 日志脱敏，不记录消息正文/密码；
- [ ] 定期权限复核。

## OAuth 2.0 与 OIDC 流程

RabbitMQ 的 OAuth 2.0 插件让客户端用 access token 连接，而不是长期用户名/密码。典型流程：

```text
1. 客户端向身份提供商（IdP）申请 token（client credentials / user login）
2. IdP 返回 access token（JWT，含 scope/audience/claim）
3. 客户端在 AMQP 认证响应中携带 token
4. RabbitMQ 校验签名、issuer、audience、expiry、scope
5. 根据 token 的 scope/claim 映射到 vhost 权限/用户
```

关键配置：

- issuer/audience；
- JWKS/公钥或 introspection endpoint；
- token 过期/时钟偏差；
- scope → permission 映射；
- 是否允许 token 续期/刷新；
- 审计 token 使用。

常见坑：

- token 过期后客户端不刷新，重连失败；
- clock skew 导致 token 被判定未生效/已过期；
- scope 过宽，等于把管理员权限发给业务；
- 只校验签名不校验 audience/issuer；
- 日志里打印完整 token。

4.4 起 OIDC discovery 校验更严格：issuer 必须匹配、端点必须是 HTTPS；Cloud Foundry UAA 等使用非标准 discovery 的环境会直接登录失败。升级前要验证 IdP 的 discovery 文档，必要时临时配置 `auth_oauth2.discovery.verify_issuer = false`，但这会削弱安全保证，只能作为过渡。

OAuth 适合集中身份管理/短期凭据，但不要把“有 token”当成“有业务权限”；授权仍要最小化。

## LDAP/HTTP 认证后端

企业环境常见：

- **LDAP/Active Directory**：用户名/密码由目录服务校验，RabbitMQ 映射用户/tag；
- **HTTP backend**：RabbitMQ 调用外部 HTTP 服务校验凭据/返回权限；
- **auth backend cache**：缓存认证结果，减少目录服务压力；
- **internal**：RabbitMQ 内部用户库。

设计要点：

- 认证成功不等于授权；vhost 权限仍要配置；
- 目录服务故障时的降级策略（拒绝还是缓存）；
- 连接风暴会放大认证后端压力；
- 缓存 TTL/失效要合理；
- 凭据轮换/离职用户要及时失效；
- 审计认证失败。

## mTLS 与证书轮换

mTLS 用客户端证书证明身份，适合服务间/零信任网络。配置要点：

- 服务端 `verify_peer` + `fail_if_no_peer_cert`；
- CA 链完整；
- 客户端证书 SAN/CN 与身份映射；
- 证书用途/扩展；
- 证书轮换/吊销（CRL/OCSP）；
- 4.4 在 Erlang/OTP 29 且 crypto 链接 OpenSSL 3.5+ 时，TLS 1.3 默认优先使用后量子混合密钥交换（X25519MLKEM768），客户端/负载均衡的 TLS 栈要支持，否则可能握手失败；
- TLS 版本/加密套件；
- 私钥保护（文件权限/KMS/HSM）。

轮换流程：

1. 生成新证书/私钥；
2. 在预发验证；
3. 服务端/客户端双证书过渡（若支持）；
4. 滚动更新；
5. 撤销旧证书；
6. 监控握手失败。

不要在轮换时关闭证书校验；那会把一次可控变更变成安全事件。

## Topic authorization

vhost ACL 控制“能不能操作交换机/队列”，但不控制“能不能向某个 routing key 发布/从某个 key 消费”。Topic authorization 在 3.7+ 是**内置能力**（不是插件），通过 `rabbitmqctl set_topic_permissions` 配置；默认关闭，需要显式启用。

适用：

- 多租户共享交换机；
- 限制某服务只能发布/订阅自己的 topic；
- 防止越权读取其他租户事件。

注意：

- 规则复杂度会上升；
- 性能开销；
- 不是所有云厂商/托管版本开放该能力；
- 只覆盖 topic 交换机的 routing key，不覆盖 headers/direct/fanout 的全部语义；
- 仍需 vhost/网络隔离兜底。

## STRIDE 威胁建模

用 STRIDE 过一遍消息系统：

| 威胁 | 例子 | 控制 |
|---|---|---|
| Spoofing | 冒充生产者/消费者 | TLS/mTLS、OAuth、强密码 |
| Tampering | 篡改消息/拓扑 | TLS、签名、权限、审计 |
| Repudiation | 否认发布/删除 | 审计日志、消息 ID、签名 |
| Information disclosure | 消息/凭据泄露 | TLS、加密、脱敏、最小权限 |
| Denial of service | 连接/队列打满 | 配额、限流、告警、隔离 |
| Elevation of privilege | 普通用户改 policy | 最小权限、管理面隔离、审批 |

把每个威胁映射到“预防/检测/响应”，比买一个“安全产品”更有用。

## 本章小结

RabbitMQ 安全是分层工程：网络隔离、TLS、认证、vhost ACL、管理面保护、密钥轮换、审计和供应链。PLAIN 必须配 TLS，guest 不能生产使用，权限要按服务最小化。Mini 实现了认证与 ACL 的核心，但不覆盖生产安全的全部边界。

下一章进入可观测性：指标、日志、追踪、Firehose 和告警，回答“系统现在到底健康吗、消息卡在哪里”。

---

[上一章：第 14 章 协议生态与多语言客户端](14-protocol-ecosystem-clients.md) | [返回目录](../README.md) | [下一章：第 16 章 可观测性：指标、日志、追踪与 Firehose](16-observability.md)
