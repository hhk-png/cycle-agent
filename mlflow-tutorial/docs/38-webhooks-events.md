# 第 38 章 事件驱动与自动化治理：Webhook、Outbox 与签名

## 学习目标

- 说清“轮询 Registry”和“订阅 Registry 事件”的差别，以及为什么发布流水线需要后者。
- 理解 MLflow OSS 3.3+ webhooks 的事件命名、载荷、签名头与重试语义。
- 读懂 MiniMLflow 的 `webhooks.py`：订阅、持久化 outbox、签名、重试与后台分发。
- 能设计一个至少一次（at-least-once）且可去重、可审计、可回放的事件投递链路。
- 识别 SSRF、密钥泄漏、重放、重复投递和 outbox 膨胀五类生产风险。

## 38.1 为什么需要事件

第 12、23 章把 Registry 描述成“不可变版本 + 可变发布指针”。一旦有了指针，周边系统就会关心它何时移动：

- 模型版本创建后，CI 要跑评测与安全扫描；
- `champion` alias 切换后，部署系统要滚动更新；
- Prompt 版本发布后，评测服务要用固定版本跑回归；
- 版本被删除后，资产目录要标记失效；
- 审批系统要知道“谁在什么时候把哪个版本提升到了生产”。

最直接的实现是轮询：

```text
每分钟查询一次 registry，比较上次快照，发现差异后触发动作
```

轮询的问题不是“能不能用”，而是**延迟、成本和一致性**：

- 想做到秒级反应就要高频轮询，数据库和 API 承受无意义的读；
- 两次轮询之间的多次变更可能被合并，丢失“先创建后删除”这类中间状态；
- 每个消费者都要自己保存游标、处理分页和去重；
- 多副本部署时，多个轮询者会重复触发同一动作。

Webhook 把“变更”变成一次主动通知：Registry 在事务完成后，把事件写入 outbox，再由投递器 POST 给订阅方。消费者不再猜“发生了什么”，而是收到一条结构化事实。

```text
Registry mutation ──> outbox row ──> dispatcher ──HTTP POST──> CI / deploy / catalog
        │                  │
        └── 同一业务事实 ───┘        失败则重试，成功则标记 DELIVERED
```

注意最后一句：**Webhook 不是消息队列的替代品**。它适合“通知外部系统有事发生”，不适合承载大 payload、复杂路由或强顺序。需要强顺序、回放、多消费者分组时，应该把事件写入 Kafka/Pulsar/SQS，再由消费者投递。

## 38.2 真实 MLflow 的 webhooks：事实基线

MLflow OSS 从 3.3 起提供 webhooks，属于 tracking server/Registry 的一部分。写作时（2026-11，3.18）的公开行为可以概括为：

| 维度 | 行为 |
|---|---|
| Python API | `MlflowClient.create_webhook`、`get_webhook`、`list_webhooks`、`update_webhook`、`delete_webhook`、`test_webhook` |
| 事件命名 | `entity.action`，例如 `registered_model.created`、`model_version.created`、`model_version_alias.created`、`model_version_tag.set`、`prompt_version.created` |
| 载荷 | `{entity, action, timestamp, data}`；`data` 是实体快照/变更字段 |
| 签名 | `X-Mlflow-Signature: v1,<base64(HMAC-SHA256)>` |
| 签名内容 | `delivery_id.timestamp.payload` |
| 其他头 | `X-Mlflow-Delivery-ID`、`X-Mlflow-Timestamp` |
| 重试 | 指数退避；默认重试次数/超时由环境变量控制（如 `MLFLOW_WEBHOOK_REQUEST_MAX_RETRIES`、`MLFLOW_WEBHOOK_REQUEST_TIMEOUT`） |
| 存储 | 需要数据库后端；secret 可用 `MLFLOW_WEBHOOK_SECRET_ENCRYPTION_KEY` 加密 |
| 托管差异 | Databricks 的 Registry Webhook API 使用 `/registry-webhooks/*` 与 UPPER_SNAKE 事件名，签名头是 `X-Databricks-Signature`；不要把两套协议混用 |

这张表里有三个容易写错的点：

1. **事件名不是 UPPER_SNAKE**。OSS 用 `model_version.created`，不是 `MODEL_VERSION_CREATED`。很多旧教程复制的是 Databricks API。
2. **签名不是“对 payload 直接 HMAC”**。它把 delivery id 和 timestamp 一起签进去，目的是让接收方可以拒绝重放。
3. **Webhook 是 Registry/Prompt 的事件，不是训练 run 的通用事件总线**。Mini 也遵循这个边界。

真实 API 的最小用法：

```python
import os
from mlflow import MlflowClient

client = MlflowClient("http://127.0.0.1:5000")
webhook = client.create_webhook(
    name="promote-on-alias",
    url="https://ci.example.com/hooks/mlflow",
    events=["model_version.created", "model_version_alias.created"],
    secret=os.environ["MLFLOW_WEBHOOK_SECRET"],
    description="Trigger evaluation and deployment when an alias moves",
)
print(webhook.webhook_id)
```

`update_webhook` 可以改 `name/url/events/secret/description/status`；`test_webhook` 会向目标发一条测试事件，用来验证网络、鉴权和签名，而不是等到真实发布时才第一次调用。

## 38.3 Mini 的数据模型

Mini 用两张表表达“订阅”和“投递”：

```sql
CREATE TABLE webhooks (
  webhook_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  url TEXT NOT NULL,
  events_json TEXT NOT NULL,
  secret TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ACTIVE'
         CHECK (status IN ('ACTIVE','DISABLED')),
  creation_timestamp INTEGER NOT NULL,
  last_updated_timestamp INTEGER NOT NULL
);

CREATE TABLE webhook_deliveries (
  delivery_id TEXT PRIMARY KEY,
  webhook_id TEXT NOT NULL,
  event TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING'
         CHECK (status IN ('PENDING','DELIVERED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  response_status INTEGER,
  created_time INTEGER NOT NULL,
  next_attempt_time INTEGER NOT NULL,
  delivered_time INTEGER,
  FOREIGN KEY (webhook_id) REFERENCES webhooks(webhook_id) ON DELETE CASCADE
);
```

为什么拆成两张表，而不是“一条 webhook 带一个 last_status”？

- 一次变更可能命中多个 webhook，每个订阅方的失败与重试互相独立；
- 投递历史是审计证据：谁在什么时候收到了什么；
- outbox 可以在服务重启后继续投递，不会因为进程崩溃丢事件；
- 失败投递可以进入 dead-letter 队列，人工回放。

`events_json` 存的是订阅列表，例如：

```json
["model_version.created", "model_version_alias.created"]
```

Mini 的事件目录：

```text
registered_model.created / renamed / deleted
model_version.created / deleted / transitioned_stage
model_version_tag.set / deleted
model_version_alias.created / deleted
prompt.created / deleted
prompt_version.created
prompt_alias.created / deleted
```

这覆盖了“模型资产”和“Prompt 资产”的主要生命周期。没有覆盖的事件（例如 workspace、RBAC、MCP）在第 37 章的平台能力里，真实验证时要按版本文档确认事件名。

## 38.4 一次订阅的完整生命周期

### 38.4.1 创建

```python
hook = client.create_webhook(
    "promote-on-alias",
    "https://ci.example.com/hooks/mlflow",
    ["model_version.created", "model_version_alias.created"],
    secret="shared-secret",
    description="CI 在 alias 变化时触发评测",
)
```

服务端做四类校验：

1. **名称**：非空、长度受限、可读；
2. **URL**：只允许 `http`/`https`，必须有 host，不允许 URL 内嵌用户名/密码，不允许 fragment；
3. **事件**：必须是已知的 `entity.action`，非空，去重，最多 50 个；
4. **secret/description**：长度限制，secret 不会出现在 `to_dict()` 的默认输出里。

`to_dict()` 返回 `secret_set: true`，但不返回 secret 本身。这是最小权限原则：读 webhook 列表的运维人员不需要拿到签名密钥。

### 38.4.2 触发

Registry 的每个写操作在成功提交后调用 `emit`：

```python
def set_registered_model_alias(self, name, alias, version):
    clean_alias = validate_alias(alias)
    self.store.set_model_alias(name, clean_alias, int(version))
    self._emit(
        "model_version_alias.created",
        {"name": name, "alias": clean_alias, "version": str(version)},
    )
```

`_emit` 是**尽力而为**的：通知失败不会把已经成功的 alias 变更变成 500。这听起来合理，但它暴露了一个必须讲清楚的一致性窗口：

```text
1. store.set_model_alias 提交 ✅
2. 进程在 _emit 之前崩溃 ❌
   → alias 已变，但事件永远丢失
```

真正的修复不是“在 `_emit` 里重试”，而是**事务性 outbox**：把业务写入和 delivery 插入放在同一个数据库事务里。Mini 的 store 方法各自管理事务，所以本章只能演示 outbox 的投递侧，不能演示“写入与 outbox 原子”。第 38.5 节会给出生产改造方案。

### 38.4.3 入队

`WebhookService.emit` 做三件事：

```python
payload = {
    "entity": entity,
    "action": action,
    "timestamp": now_iso(),
    "data": dict(data or {}),
}
for webhook in active_subscribers(event):
    store.create_webhook_delivery(WebhookDelivery(...))
```

注意 `data` 里放的是**变更后的实体快照或关键字段**，不是整个数据库行。这样接收方不需要再回查 Registry 就能做基本判断，同时又不会把大对象（模型文件、完整 prompt 历史）塞进事件。

### 38.4.4 投递

`drain_once` 取所有到期的 `PENDING` 投递，逐条调用 sender。默认 sender 用 `urllib` 发 POST：

```python
headers = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Mlflow-Delivery-ID": delivery.delivery_id,
    "X-Mlflow-Timestamp": timestamp,
    "X-Mlflow-Event": delivery.event,
}
if webhook.secret:
    headers["X-Mlflow-Signature"] = "v1," + signature
```

成功（2xx）后写 `DELIVERED`、`response_status`、`delivered_time`；失败则增加 `attempts`，记录 `last_error`，按退避时间写 `next_attempt_time`。达到上限后写 `FAILED`。

### 38.4.5 后台分发

HTTP 服务启动时调用：

```python
server.service.webhooks.start_dispatcher()
```

后台线程每隔一秒 `drain_once()`。测试和示例不启动线程，而是显式调用 `drain_once()`，这样没有 sleep、没有竞态，失败路径可复现。

## 38.5 至少一次、去重与顺序

Webhook 投递的默认语义是 **at-least-once**：

- 接收方返回 200 之前进程崩溃 → 事件会重试；
- 接收方处理成功但响应丢失 → 事件会重试；
- 网络超时但请求已到达 → 事件会重试。

因此接收方必须幂等。最直接的幂等键是 `X-Mlflow-Delivery-ID`：

```python
seen = load_seen_delivery_ids()
if delivery_id in seen:
    return 200  # 已经处理过，直接确认
process(payload)
mark_seen(delivery_id)
return 200
```

不要用 `payload` 的哈希做唯一键：两个合法的相同变更（例如连续两次把 alias 指向同一版本）可能产生相同的业务内容，但它们是两个不同的事件。`delivery_id` 才是投递身份。

顺序也不是保证的：

- 两个 webhook 的投递互不相关；
- 同一个 webhook 的两次事件可能因为重试而乱序（先发的事件失败重试，后发的事件先成功）；
- 多副本 dispatcher 会并发投递。

如果业务要求“先创建版本，再切换 alias”，接收方应该用实体版本号/时间戳做状态机，而不是假设 HTTP 到达顺序。真正需要强顺序时，用消息队列的分区键，而不是 webhook。

## 38.6 签名、重放与密钥

### 38.6.1 签名构造

Mini 与 OSS MLflow 的形状一致：

```text
signed_content = delivery_id + "." + timestamp + "." + raw_body
signature      = base64(HMAC-SHA256(secret, signed_content))
header         = "v1," + signature
```

接收方验证：

```python
from mini_mlflow.webhooks import verify_signature

if not verify_signature(
    secret,
    headers["X-Mlflow-Delivery-ID"],
    headers["X-Mlflow-Timestamp"],
    raw_body,
    headers["X-Mlflow-Signature"],
):
    return 401
```

三个细节决定这段代码是否真的安全：

1. **必须对原始字节签名**，不能对解析后的 JSON 重新序列化。否则键顺序、空格、Unicode 转义都会让签名对不上，或者让攻击者构造“解析后相同、字节不同”的歧义。
2. **必须用 `hmac.compare_digest`**，不能用 `==`。字符串比较可能泄漏时序信息。
3. **必须校验时间戳**。Mini 只负责生成 `X-Mlflow-Timestamp`；接收方应拒绝超过窗口（例如 5 分钟）的请求，否则攻击者可以重放旧请求。仅靠签名不能防重放，因为旧请求的签名永远有效。

### 38.6.2 密钥管理

Mini 把 secret 明文存在 SQLite 的 `webhooks.secret` 列。这是教学简化，生产不能这样做：

- 使用 KMS/Vault/云 secret manager 加密；
- 或至少用 `MLFLOW_WEBHOOK_SECRET_ENCRYPTION_KEY` 一类的环境密钥做信封加密；
- 日志、错误响应、`to_dict()` 都不应出现 secret；
- 轮换时要支持双 secret 过渡：新 secret 生效后，旧 secret 保留一个重试窗口；
- 每个接收方使用独立 secret，不要全公司共用一个。

### 38.6.3 重放窗口

一个完整的接收方检查顺序：

```text
1. 读取原始 body（不要先解析）
2. 检查 timestamp 是否在 ±5 分钟内
3. 用 delivery_id + timestamp + body 验签
4. 查 delivery_id 是否已处理
5. 解析 JSON、校验 schema
6. 处理业务
7. 记录 delivery_id
8. 返回 2xx
```

第 4 步和第 7 步之间仍然可能并发重复；高并发接收方需要唯一约束/事务，而不是“先查再写”。

## 38.7 重试、退避与死信

Mini 的退避是确定性的：

```text
第 1 次失败 → 等 1s
第 2 次失败 → 等 2s
第 3 次失败 → 等 4s
...
达到 max_attempts → FAILED
```

生产应改为：

- **指数退避 + 抖动**：避免大量接收方在同一秒重试；
- **尊重 `Retry-After`**：429/503 时按服务端建议等待；
- **区分可重试与不可重试**：400/401/403/404 通常不应无限重试，2xx 才是成功；
- **死信队列**：达到上限后进入 DLQ，保留原始 payload 与最后错误，支持人工回放；
- **投递超时**：连接超时与读超时分开，避免慢接收方占满 worker；
- **并发上限**：每个接收方/租户独立限流，避免一个坏 endpoint 拖垮整个 dispatcher；
- **可观测性**：记录 `pending/delivered/failed` 数量、投递延迟、按 webhook/event 的错误率。

Mini 的 `FAILED` 就是最小死信：它不会自动消失，也不会被再次选中。可以用 `list_webhook_deliveries` 查看，但没有内置回放命令。生产应提供：

```text
webhook replay --delivery-id ... --reset-attempts
webhook replay --webhook-id ... --since ...
```

回放时必须保留原 `delivery_id`，否则接收方无法去重。

## 38.8 SSRF：URL 校验只是第一层

Webhook 的本质是“让服务器主动请求一个用户提供的 URL”。这天然是一个 SSRF 入口：

```text
https://attacker.example/redirect -> http://169.254.169.254/latest/meta-data/
http://127.0.0.1:6379/            -> 内网 Redis
http://[::1]:8500/                -> 本地服务
http://internal-db:5432/          -> 内网数据库
```

Mini 做了三件事：

1. 只允许 `http`/`https`；
2. 拒绝 URL 内嵌凭据和 fragment；
3. **不跟随重定向**（`_NoRedirect`），防止 30x 把合法 URL 变成内网请求。

但仅靠这些还不够：

- **私网地址**：Mini 为了本地示例允许 `127.0.0.1`；生产应默认拒绝 RFC1918、link-local、loopback、IPv6 ULA；
- **DNS rebinding**：校验时解析到公网 IP，请求时却解析到内网。需要在连接层固定解析结果，或用 egress 代理；
- **IPv4/IPv6 混合**：只检查 IPv4 会漏掉 `::1`、IPv4-mapped IPv6；
- **云元数据**：`169.254.169.254`、`fd00:ec2::254` 必须显式拒绝；
- **端口**：只允许 80/443，或按接收方白名单放行；
- **出口代理**：最稳的方案是让 webhook 流量走专用 egress proxy，由代理做域名/IP 策略；
- **响应大小**：Mini 只读 64 KiB 并丢弃，避免恶意响应耗尽内存。

一个可落地的生产策略：

```text
1. 域名白名单（按租户/环境）
2. DNS 解析后检查所有 A/AAAA 记录
3. 拒绝私网/loopback/link-local/元数据地址
4. 连接时使用已校验的 IP，禁止二次解析
5. 不跟随重定向
6. 只允许 443，强制 TLS
7. 出口代理 + 审计日志
```

## 38.9 与 Registry/Prompt 的接线

`TrackingService` 在构造时把同一个 `WebhookService` 注入两个 registry：

```python
self.webhooks = WebhookService(self.store)
self.registry = ModelRegistry(self.store, emit=self.webhooks.emit)
self.prompts = PromptRegistry(self.store, emit=self.webhooks.emit)
```

这样本地客户端、HTTP 服务端和 CLI 共享同一套事件语义。关键设计点：

- **事件在业务提交后发出**：不会出现“事件说创建成功，数据库里没有”；
- **通知失败不影响业务**：`_emit` 捕获异常并记录日志；
- **事件数据是快照**：接收方不需要回查，但也要意识到它可能已经过期；
- **payload 可能含敏感信息**：`prompt_version.created` 包含模板文本，`model_version.created` 包含 tags；接收方和日志都要按敏感数据处理。

如果要做“事务性 outbox”，改造点在 store 层：

```python
with store.transaction() as conn:
    store.set_model_alias_in(conn, name, alias, version)
    store.insert_delivery_in(conn, delivery)
# 事务提交后，dispatcher 才可能看到 delivery
```

这要求把 registry 的写方法与 outbox 插入放进同一个连接/事务。Mini 没有这样做，因为它的 store 方法是“一个方法一个事务”；这是教学实现与生产实现最重要的差距之一。

## 38.10 API、CLI 与可运行示例

### REST

```bash
# 创建
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/webhooks/create \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"promote-on-alias",
    "url":"https://ci.example.com/hooks/mlflow",
    "events":["model_version.created","model_version_alias.created"],
    "secret":"shared-secret"
  }'

# 列表 / 详情
curl -s http://127.0.0.1:5000/api/2.0/mlflow/webhooks/list
curl -s 'http://127.0.0.1:5000/api/2.0/mlflow/webhooks/get?webhook_id=<ID>'

# 测试投递
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/webhooks/test \
  -H 'Content-Type: application/json' -d '{"webhook_id":"<ID>"}'

# 投递历史
curl -s 'http://127.0.0.1:5000/api/2.0/mlflow/webhooks/deliveries?webhook_id=<ID>'
```

### CLI

```bash
python -m mini_mlflow webhooks create promote-on-alias \
  --url https://ci.example.com/hooks/mlflow \
  --event model_version.created \
  --event model_version_alias.created \
  --secret shared-secret

python -m mini_mlflow webhooks list
python -m mini_mlflow webhooks test <WEBHOOK_ID>
python -m mini_mlflow webhooks deliveries <WEBHOOK_ID>
```

### 示例

`examples/13_webhooks.py` 在本地启动一个 receiver，创建 webhook，训练并注册模型，切换 alias，然后验证：

- 两条事件都到达；
- `X-Mlflow-Signature` 验签通过；
- payload 的 `entity/action/data` 与操作对应；
- 投递记录为 `DELIVERED`。

运行：

```bash
python examples/13_webhooks.py
```

这个示例不需要外网。它也是“如何写接收方”的最小模板：先读原始 body，再验签，再解析。

## 38.11 运维清单

### 38.11.1 指标

按 webhook 和 event 两个维度打标签：

```text
webhook_deliveries_total{webhook,event,status}
webhook_delivery_latency_ms{webhook,event}
webhook_pending_deliveries{webhook}
webhook_failed_deliveries{webhook}
webhook_retries_total{webhook,event}
```

只看总量不够：一个接收方持续 500 会被总量掩盖。要能回答“哪个 endpoint 在拖后腿”。

### 38.11.2 保留与清理

`webhook_deliveries` 会随时间增长。建议：

- `DELIVERED` 保留 7–30 天，用于审计；
- `FAILED` 保留更久，直到人工处理；
- 按时间分区/归档；
- 删除 webhook 时级联删除投递（Mini 的外键已做），但生产应先导出审计；
- 对高频事件（例如每次 tag 变更）考虑采样或合并，避免事件风暴。

### 38.11.3 运维动作

- 发布前用 `test_webhook` 验证网络、TLS、鉴权、签名；
- 监控 pending 数持续增长（dispatcher 停止/接收方故障）；
- 轮换 secret 时同时接受新旧两个签名；
- 定期回放 DLQ，确认修复后没有丢失关键事件；
- 对接收方做超时/限流，不要让一个慢 endpoint 占满 worker；
- 记录“谁创建/修改了 webhook”，这本身是安全敏感操作。

## 38.12 与真实 MLflow 的差距

| 能力 | Mini | 真实 MLflow 3.x |
|---|---|---|
| 事件目录 | 模型 + Prompt 子集 | 模型、Prompt、tag、alias 等更完整 |
| 存储 | SQLite 明文 secret | 数据库 + 可选 secret 加密 |
| 投递 | 单进程、同步 `drain_once` | 后台 worker、可配置并发 |
| 重试 | 固定指数退避 | 指数退避 + 抖动，尊重 `Retry-After` |
| 事务性 outbox | 否（业务提交后才入队） | 设计目标；以版本实现为准 |
| 多副本 | 无行抢占 | 需要 claim/lock 或队列 |
| DLQ/回放 | 只有 `FAILED` 记录 | 平台/运维工具 |
| SSRF 防护 | scheme/凭据/重定向 | 部署方需补私网/egress 策略 |
| 签名时间窗 | 只生成，不校验 | 接收方按文档校验 |

结论：**Mini 演示的是控制面形状，不是生产投递器**。真正上线时，最需要补的不是“再多几个事件”，而是事务性 outbox、密钥加密、多副本 claiming、DLQ 与 SSRF 出口策略。

## 小结

- Webhook 把 Registry/Prompt 的变更变成结构化事件，减少轮询延迟与重复逻辑。
- 事件名是 `entity.action`；OSS 与 Databricks 的命名/签名头不同，不能混用。
- outbox 先落库再投递，保证“已入队的事件不丢”；投递语义是至少一次，接收方必须按 delivery id 去重。
- 签名覆盖 `delivery_id.timestamp.payload`，接收方还要校验时间窗；只用 HMAC 不防重放。
- 重试要指数退避、区分可重试错误、进入死信并可回放。
- SSRF 是 Webhook 的固有风险；URL 校验、禁重定向、私网/元数据地址封禁、出口代理缺一不可。
- Mini 的 `_emit` 在业务提交后执行，存在“提交后崩溃丢事件”的窗口；生产要用事务性 outbox 消除它。

## 练习

1. 为 `webhook_deliveries` 设计一个“按天分区 + 30 天归档”的策略，说明对 `drain_once` 查询的影响。
2. 写一个接收方中间件：验签、时间窗、delivery id 去重、schema 校验，返回 200/400/401 的判定顺序是什么？
3. 把 Mini 的固定退避改成“指数退避 + 抖动 + 尊重 429 Retry-After”，写出状态字段和测试用例。
4. 设计一个事务性 outbox：列出需要改的 store 方法、事务边界和回滚测试。
5. 给出一个 SSRF 防护清单，至少包含 DNS rebinding、IPv6、云元数据地址和重定向。
6. 如果接收方要求“同一模型的版本创建事件必须严格有序”，你会用 webhook 还是消息队列？论证。

---

下一章：[第 39 章 真实 MLflow 3.x GenAI 实战](39-real-genai-hands-on.md)
