# 第23章 安全、多租户与资源治理

## 23.1 流平台的安全边界

一个实时平台通常有多方参与：

- 平台团队：管理集群、升级、容量；
- 业务团队：提交作业、读数据、写结果；
- 数据团队：管理 Catalog、权限、质量；
- 安全团队：审计、合规、漏洞。

Flink 作业会接触 Kafka、数据库、对象存储、内部服务，任何一个环节配置不当都可能造成数据泄漏或越权。

```text
身份 → 认证 → 授权 → 数据访问 → 审计
  │       │       │         │         │
用户    Kerberos  Catalog   TLS     日志/合规
服务    mTLS      Ranger    加密
```

安全不是 Flink 的一个开关，而是平台、身份、网络、数据、供应链的组合。

## 23.2 认证

- **Kerberos**：Hadoop 生态常用，Flink 支持 keytab；
- **SASL**：Kafka 的 SCRAM/GSSAPI；
- **mTLS**：服务间双向 TLS；
- **OIDC/LDAP**：SQL Gateway/平台层认证；
- **云 IAM**：S3/OSS/Kinesis 等使用角色或实例凭证。

```yaml
security.kerberos.login.keytab: /etc/security/keytabs/flink.keytab
security.kerberos.login.principal: flink@EXAMPLE.COM
```

原则：

- 不要在生产配置里明文写密码；
- 使用 Secret 管理（K8s Secret、Vault、KMS）；
- 凭证定期轮换；
- 最小权限；
- 区分开发/测试/生产凭证。

### 凭证泄漏的常见途径

- 代码仓库里的 `application.conf`；
- 日志里打印的连接串；
- 异常堆栈里的 URL；
- 镜像层里的历史文件；
- SQL DDL 的 `WITH (...)` 参数；
- 聊天/工单里的粘贴。

任何一条都可能让凭证进入不可控的地方。要用 secret 扫描和审计兜底。

## 23.3 授权

Flink 本身不提供完整的细粒度 SQL 授权，通常依赖：

- Catalog（Hive Metastore/Ranger）做库表权限；
- 平台层做作业提交/查询权限；
- 网络策略限制访问；
- 数据脱敏/行级过滤在 UDF 或视图层实现。

多租户平台常见模型：

- 按队列/命名空间隔离资源；
- 按 Catalog/数据库隔离元数据；
- 按 Kafka topic/ACL 隔离数据；
- 按作业标签计费/配额。

```text
租户 A → namespace a → Catalog a → Kafka topic a-* → S3 prefix a/
租户 B → namespace b → Catalog b → Kafka topic b-* → S3 prefix b/
```

### 最小权限

- source 只读需要的 topic/表；
- sink 只写目标表/分区；
- checkpoint 目录只允许作业的服务账号；
- 不允许跨租户读状态/日志；
- 管理 API 与数据 API 分离。

## 23.4 传输与存储加密

- TLS：JobManager/TaskManager REST、Kafka、数据库、对象存储；
- 静态加密：S3/OSS 服务端加密、PVC 加密；
- checkpoint/savepoint 可能包含业务状态，必须加密和访问控制；
- 日志脱敏，避免打印身份证/手机号/密钥。

```yaml
security.ssl.rest.enabled: true
security.ssl.rest.keystore: /etc/ssl/flink.keystore
security.ssl.rest.truststore: /etc/ssl/flink.truststore
```

### 状态中的敏感数据

checkpoint 里可能有：

- 用户画像；
- 交易明细；
- 身份信息；
- 规则/密钥。

要像对待数据库备份一样对待 checkpoint：

- 加密存储；
- 访问审计；
- 保留期限；
- 删除流程。

### Kafka SASL/SSL 配置形状

```sql
CREATE TABLE secure_orders (
  order_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders',
  'properties.bootstrap.servers' = 'kafka:9093',
  'properties.security.protocol' = 'SASL_SSL',
  'properties.sasl.mechanism' = 'SCRAM-SHA-512',
  'properties.sasl.jaas.config' =
    'org.apache.kafka.common.security.scram.ScramLoginModule required username="flink" password="${SECRET}";',
  'properties.ssl.truststore.location' = '/etc/ssl/kafka.truststore.jks',
  'format' = 'json'
);
```

不要把真实密码写进 SQL。用平台注入/密钥管理替换 `${SECRET}`；如果平台不支持变量替换，就在提交前由部署系统渲染配置，并确保渲染后的文件不落盘、不进日志。

Flink 内部与 REST 的 TLS：

```yaml
security.ssl.internal.enabled: true
security.ssl.rest.enabled: true
security.ssl.rest.keystore: /etc/ssl/flink.keystore
security.ssl.rest.truststore: /etc/ssl/flink.truststore
```

生产上还要：

- 关闭不必要的 REST 端口；
- 用网络策略限制 JM/TM/客户端访问；
- 定期轮换证书；
- 对 SQL Gateway 做 OIDC/反向代理认证；
- 审计谁提交了哪个作业、读了哪些表。

## 23.5 多租户隔离

隔离层次：

1. **集群隔离**：每租户独立集群，最强隔离，成本最高；
2. **命名空间/队列隔离**：共享集群，资源队列隔离；
3. **作业隔离**：共享 TaskManager，靠 slot 和 classloader；
4. **逻辑隔离**：共享一切，只靠标签/权限，风险最高。

生产建议：

- 核心业务独立集群或独立 namespace；
- 不可信 UDF 不要和核心作业混部；
- 资源队列设置上限，防止单租户打满；
- 限制每个租户的并行度、状态大小、checkpoint 存储配额；
- 监控租户级成本。

### 故障隔离

多租户最怕“一个作业拖垮全集群”：

- 单个作业 OOM → 容器被杀，不影响其他（K8s）；
- 单个作业打满磁盘 → 本地磁盘配额/清理；
- 单个作业大量 checkpoint → 对象存储限流；
- 单个作业热点 → 资源队列上限；
- 单个 UDF 死循环 → CPU 限制/超时。

## 23.6 UDF 与 SQL 安全

UDF 是任意代码，风险包括：

- 读取本地文件/环境变量；
- 发起网络请求；
- 死循环/内存泄漏；
- 反序列化漏洞；
- 绕过数据权限。

措施：

- UDF 审核/签名；
- 禁止不受控的网络访问；
- 沙箱/独立集群；
- 限制依赖；
- SQL Gateway 限制语句类型和超时；
- 对动态 SQL 做参数化/白名单。

SQL 注入在流平台同样存在，尤其是把用户输入拼进 SQL 字符串时。

```java
// 危险：字符串拼接
String sql = "SELECT * FROM t WHERE user = '" + input + "'";

// 安全：参数化/白名单
if (!ALLOWED_USERS.contains(input)) throw new IllegalArgumentException();
```

## 23.7 审计

需要记录：

- 谁提交/取消了作业；
- 谁触发了 savepoint；
- 谁修改了 Catalog/表/权限；
- 谁执行了 SQL；
- 作业访问了哪些数据源；
- 失败/异常事件。

审计日志要与身份系统关联，并保留足够时间满足合规要求。

```json
{
  "ts": "2026-11-26T10:00:00Z",
  "actor": "alice",
  "action": "submit_job",
  "job": "risk-job",
  "catalog": "paimon",
  "tables": ["ods.orders", "dwd.orders"],
  "result": "success"
}
```

## 23.8 供应链与依赖

- 连接器/UDF 依赖来源可信；
- 锁定版本，生成 SBOM；
- 扫描 CVE；
- 避免 shaded jar 冲突；
- 镜像签名/扫描；
- 定期升级 Flink 和连接器。

Flink 2.x 的依赖升级（Java 17、Kryo、RocksDB）会带来兼容性风险。升级前要：

- 在副本环境构建；
- 跑集成测试；
- 扫描依赖；
- 准备回滚镜像。

## 23.9 安全基线检查

- [ ] 所有外部连接启用 TLS；
- [ ] 凭证来自 Secret，不落代码/日志；
- [ ] 每个租户独立 namespace/Catalog/资源队列；
- [ ] UDF 经过审核，禁止危险 API；
- [ ] SQL Gateway 有认证、超时、结果限制；
- [ ] checkpoint/savepoint 加密且访问受控；
- [ ] 审计日志覆盖提交、取消、savepoint、DDL；
- [ ] 依赖有 SBOM 和 CVE 扫描；
- [ ] 最小权限，定期轮换凭证。

## 23.10 威胁模型示例

以“多租户实时平台”为例：

| 威胁 | 入口 | 缓解 |
|---|---|---|
| 越权读表 | SQL Gateway/Catalog | RBAC + 行/列权限 + 审计 |
| 恶意 UDF | 作业 jar | UDF 审核/沙箱/独立集群 |
| 凭证泄漏 | 配置/日志/镜像 | Secret + 扫描 + 轮换 |
| 状态泄漏 | checkpoint/savepoint | 加密 + 访问控制 |
| 跨租户网络 | Pod/Service | NetworkPolicy + mTLS |
| 资源抢占 | 大作业/热点 | 队列配额 + 限流 |
| 供应链 | 连接器/依赖 | SBOM + CVE 扫描 + 签名 |
| 审计缺失 | 提交/DDL/savepoint | 全链路审计日志 |

```yaml
# NetworkPolicy：只允许访问 Kafka 与对象存储
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: flink-egress
spec:
  podSelector:
    matchLabels: {app: flink}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: {name: kafka}
      ports: [{port: 9092, protocol: TCP}]
    - to:
        - ipBlock: {cidr: 10.0.0.0/8}
      ports: [{port: 443, protocol: TCP}]
```

## 23.11 Secret 管理示例

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: flink-kafka
type: Opaque
stringData:
  bootstrap.servers: kafka:9092
  sasl.username: flink
  sasl.password: ${VAULT_KAFKA_PASSWORD}
```

```yaml
# FlinkDeployment 中引用
spec:
  flinkConfiguration:
    security.ssl.rest.enabled: "true"
  podTemplate:
    spec:
      containers:
        - name: flink-main-container
          envFrom:
            - secretRef: {name: flink-kafka}
```

原则：

- Secret 不落 Git 明文；
- 用 External Secrets/Vault/KMS 注入；
- 只挂载需要的 key；
- 定期轮换；
- 审计读取。

## 23.12 多租户配额

```text
租户 A：max slots 32，max state 500 GB，max checkpoints 20/天
租户 B：max slots 8，max state 50 GB，max checkpoints 5/天
```

配额要覆盖：

- 并行度/slot；
- 状态大小；
- checkpoint 存储；
- 网络/出口；
- UDF CPU/内存；
- 作业数/并发查询。

没有配额的多租户，最终会变成“谁先占满谁赢”。

## 23.13 MiniFlink 的边界

MiniFlink 是本地教学实现，没有认证、授权、TLS、多租户和审计。它的 SQL 使用本地 SQLite，不暴露网络端口。不要把它部署为共享服务。完整边界见[附录 C](../appendix/C-miniflink-limits.md)。

## 23.14 认证与加密的深度排障

安全配置的难点不是“抄对 YAML”，而是故障时能定位到是 principal、keytab、JAAS、listener、证书还是时钟。下面按协议拆。

### 23.14.1 Kerberos

典型配置：

```yaml
security.kerberos.login.use-ticket-cache: true
security.kerberos.login.keytab: /etc/security/keytabs/flink.keytab
security.kerberos.login.principal: flink@EXAMPLE.COM
security.kerberos.login.contexts: Client,KafkaClient
```

关键概念：

- **principal**：身份，必须与 keytab 中的条目一致；
- **keytab**：长期密钥，文件权限要限制（例如 0400），不要进镜像层；
- **ticket cache**：`kinit` 后的临时凭证；容器/短作业常用 keytab 直接登录；
- **contexts**：需要初始化 Kerberos 的上下文（HDFS、Kafka 等），漏配会导致某个连接没有凭证；
- **delegation token**：长时间作业访问 HDFS 等系统时需要续期，不能只靠初始 TGT；
- **时钟偏移**：Kerberos 默认容忍约 5 分钟，节点时间不同步会直接失败。

排查命令：

```bash
klist -kt /etc/security/keytabs/flink.keytab   # keytab 里有哪些 principal
kinit -kt /etc/security/keytabs/flink.keytab flink@EXAMPLE.COM
klist                                          # 当前 ticket 及过期时间
```

常见异常：

| 异常 | 原因 | 处理 |
|---|---|---|
| `Failed to find any Kerberos tgt` | 没有 ticket/keytab 未登录 | 检查 `use-ticket-cache`、keytab 路径、principal |
| `Client not found in Kerberos database` | principal 写错/域不对 | 对照 keytab 与 KDC |
| `Ticket expired` | TGT 过期/未续期 | 长作业开 token 续期，检查时钟 |
| `Clock skew too great` | 节点时间不同步 | 配 NTP/chrony |
| `GSSException: Defective token` | 协议/上下文不匹配 | 检查 `contexts`、listener |
| `Keytab file not found` | 路径/权限/挂载 | 检查容器挂载与文件权限 |

Kubernetes 上把 keytab 放 Secret，挂载为只读文件；不要让 principal/keytab 出现在日志、CR 明文或镜像历史里。

### 23.14.2 Kafka SASL

Kafka 的认证失败经常是“协议、机制、JAAS、listener”四者之一不匹配：

```sql
'properties.security.protocol' = 'SASL_SSL',
'properties.sasl.mechanism' = 'SCRAM-SHA-512',
'properties.sasl.jaas.config' =
  'org.apache.kafka.common.security.scram.ScramLoginModule required username="flink" password="...";',
'properties.ssl.truststore.location' = '/etc/ssl/kafka.truststore.jks'
```

检查顺序：

1. **listener**：broker 暴露的是 `SASL_SSL://` 还是 `PLAINTEXT://`？客户端协议必须匹配；
2. **mechanism**：SCRAM 用 `ScramLoginModule`，GSSAPI 用 `Krb5LoginModule`，不能混；
3. **JAAS**：`sasl.jaas.config` 的类名、用户名、密码、`serviceName` 要对；
4. **truststore**：`SASL_SSL` 需要 truststore，证书链要完整；
5. **hostname**：证书 SAN 与 broker 地址匹配，否则握手失败；
6. **ACL**：认证成功但授权失败是另一类错误（`TopicAuthorizationException`）。

常见异常：

| 异常 | 可能原因 |
|---|---|
| `SaslAuthenticationException` | 用户名/密码/mechanism/JAAS 错 |
| `GSSException` | Kerberos ticket/principal/serviceName 错 |
| `SSLHandshakeException` | truststore/证书/主机名/协议版本 |
| `TopicAuthorizationException` | ACL 未授权 |
| `TimeoutException: Failed to update metadata` | 网络/ listener/ DNS |

### 23.14.3 TLS

Flink 内部与 REST 的 TLS：

```yaml
security.ssl.internal.enabled: true
security.ssl.rest.enabled: true
security.ssl.rest.keystore: /etc/ssl/flink.keystore
security.ssl.rest.truststore: /etc/ssl/flink.truststore
```

常见问题：

- keystore/truststore 路径在容器里不存在或权限不足；
- 证书过期/主机名不匹配/CA 链不完整；
- JM/TM 使用的证书不同，内部握手失败；
- 客户端不信任自签 CA；
- 协议/加密套件不兼容。

排查：

```bash
keytool -list -v -keystore /etc/ssl/flink.keystore
openssl s_client -connect host:443 -showcerts
# JVM 级握手日志（仅排障时短时开启）
-Djavax.net.debug=ssl,handshake
```

`javax.net.debug` 会输出大量日志，也可能包含敏感信息，排障后立即关闭。

### 23.14.4 证书与凭证轮换

轮换要有过渡期：

```text
1. 新证书/新密钥同时可用（双信任）
2. 更新 Secret/keystore
3. 滚动重启 JM/TM
4. 验证新旧客户端都能连
5. 下线旧证书/旧密钥
```

不要“一键替换”后立刻删除旧凭证；长连接、缓存、未重启的 Pod 会立刻失败。

### 23.14.5 排障检查清单

- [ ] principal 与 keytab 条目一致，权限最小；
- [ ] `security.kerberos.login.contexts` 覆盖所有需要认证的系统；
- [ ] 长作业有 delegation token 续期；
- [ ] 节点时间同步；
- [ ] Kafka 的协议/mechanism/JAAS/listener 四者匹配；
- [ ] truststore/keystore/证书链/主机名正确；
- [ ] 证书轮换有双信任过渡和回滚；
- [ ] 日志不打印密码、token、keytab 内容；
- [ ] 认证失败有明确告警和 runbook；
- [ ] 定期演练凭证过期/轮换场景。

## 本章小结

安全不是 Flink 的一个开关，而是平台、身份、网络、数据、供应链的组合。多租户的核心是资源和故障隔离，UDF 的核心是信任边界。

下一章进入工程化开发与测试：如何让一个 Flink 作业从“能跑”变成“可维护、可升级、可回滚”。
