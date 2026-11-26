# 第 12 章 异步任务与长时操作

## 12.1 为什么不能一直同步等待

同步 API 适合“结果立即可得”的操作。但很多任务天然需要时间：

- 视频转码；
- 批量导入；
- 报表生成；
- 模型训练；
- 跨境支付清算；
- 数据导出；
- 大文件病毒扫描；
- 复杂审批流；
- 多步骤工作流。

如果强行同步：

- 请求超时；
- 连接占用；
- 网关断开；
- 调用方重试导致重复执行；
- 服务端无法区分“客户端断开”和“任务失败”；
- 一个慢任务拖垮线程池；
- 无法表达进度；
- 无法取消。

异步任务模式把“发起”和“完成”解耦。它的代价是：

- 调用方需要轮询或回调；
- 状态需要持久化；
- 需要处理重复、超时、取消；
- 可观测性更复杂；
- 结果需要存储和清理。

## 12.2 202 Accepted 与操作资源

```http
POST /video-transcodes
Content-Type: application/json

{
  "sourceUrl": "s3://bucket/input.mp4",
  "preset": "1080p"
}
```

响应：

```http
HTTP/1.1 202 Accepted
Location: /operations/op_123
Retry-After: 5
```

```json
{
  "id": "op_123",
  "status": "PENDING",
  "createdAt": "2026-11-26T10:00:00Z",
  "links": {
    "self": "/operations/op_123",
    "cancel": "/operations/op_123/cancel"
  }
}
```

`202` 表示“已接受，但尚未完成”。`Location` 指向操作资源，调用方通过它查询进度。

### 202 与 201 的区别

- `201 Created`：资源已创建，结果可用；
- `202 Accepted`：请求已接受，处理尚未完成；
- `200 OK`：同步完成。

不要用 200 + `{"status": "processing"}` 代替 202，除非你的网关或客户端不支持 202。

### 操作资源的设计

操作资源应包含：

- 唯一 ID；
- 类型（`video.transcode`、`report.export`）；
- 状态；
- 进度；
- 创建/开始/完成时间；
- 预计完成时间；
- 结果；
- 错误；
- 取消链接；
- 关联的业务资源 ID；
- 幂等键；
- 租户 ID；
- 元数据。

## 12.3 操作资源模型

```json
{
  "id": "op_123",
  "type": "video.transcode",
  "status": "RUNNING",
  "progress": {
    "percent": 42,
    "stage": "encoding"
  },
  "createdAt": "2026-11-26T10:00:00Z",
  "startedAt": "2026-11-26T10:00:05Z",
  "estimatedCompletionAt": "2026-11-26T10:08:00Z",
  "result": null,
  "error": null
}
```

完成：

```json
{
  "id": "op_123",
  "status": "SUCCEEDED",
  "result": {
    "outputUrl": "s3://bucket/output.mp4",
    "durationSeconds": 312
  },
  "completedAt": "2026-11-26T10:07:12Z"
}
```

失败：

```json
{
  "id": "op_123",
  "status": "FAILED",
  "error": {
    "code": "TRANSCODE_INVALID_CODEC",
    "message": "源文件编码不受支持",
    "retryable": false
  }
}
```

### 状态集合

建议统一为一套完整状态：

```text
PENDING → QUEUED → RUNNING → SUCCEEDED
   │         │         ├→ FAILED
   │         │         ├→ TIMED_OUT
   │         │         └→ CANCELLING → CANCELLED
   │         └→ CANCELLED
   └→ CANCELLED
```

| 状态 | 含义 | 终态 | 允许的下一步 |
|---|---|---|---|
| `PENDING` | 已接受，尚未入队 | 否 | `QUEUED`、`CANCELLED` |
| `QUEUED` | 已入队，等待执行 | 否 | `RUNNING`、`CANCELLED` |
| `RUNNING` | 正在执行 | 否 | `SUCCEEDED`、`FAILED`、`TIMED_OUT`、`CANCELLING` |
| `CANCELLING` | 已接受取消，等待清理 | 否 | `CANCELLED` |
| `SUCCEEDED` | 成功完成 | 是 | — |
| `FAILED` | 失败（含业务/系统错误） | 是 | — |
| `TIMED_OUT` | 超过最大执行时间 | 是 | — |
| `CANCELLED` | 已取消 | 是 | — |
| `EXPIRED` | 结果已过期（可选，表示结果不可再取） | 是 | — |

状态机要明确：

- 合法迁移；
- 终态；
- 超时阈值；
- 取消从哪些状态可用；
- 清理与结果保留策略；
- 重试是“同一任务”还是“新任务”。

如果不想引入 `TIMED_OUT`，也可以把它作为 `FAILED` 的 `reason` 字段；但必须在契约中二选一，不能两套并存。

### 状态设计的常见错误

- 状态太多，调用方无法处理；
- 状态语义模糊（`PROCESSING` 包含排队和执行）；
- 终态后又变回非终态；
- 失败后没有错误码；
- 进度百分比不单调；
- 取消后直接删除记录。

### 进度设计

进度应：

- 单调递增（除非重试）；
- 按阶段划分；
- 有预计完成时间；
- 对未知进度返回 `null`，不要返回 0；
- 不要过度承诺精确度。

```json
{
  "progress": {
    "percent": 42,
    "stage": "encoding",
    "unitsProcessed": 4200,
    "unitsTotal": 10000
  }
}
```

## 12.4 轮询设计

### 基本轮询

```http
GET /operations/op_123
```

调用方根据 `status` 决定是否继续轮询。

### 退避策略

- 初始 1 秒；
- 指数退避，上限 30 秒；
- 加入抖动；
- 遵循 `Retry-After`；
- 终态后停止；
- 总轮询时间上限。

### 长轮询

```http
GET /operations/op_123?wait=30s
```

服务端最多等待 30 秒，若状态变化立即返回，否则返回当前状态。长轮询减少无效请求，但增加连接占用。

### 轮询的代价

- 大量客户端轮询会造成流量；
- 短任务不适合轮询；
- 需要限流；
- 需要缓存；
- 移动端应考虑推送/Webhook；
- 轮询间隔与任务时长不匹配会浪费请求；
- 轮询可能被 CDN 缓存，导致看不到更新。

### 轮询的缓存控制

操作资源应返回：

```http
Cache-Control: no-store
```

或至少 `no-cache`，否则中间缓存可能返回旧状态，调用方会一直轮询到超时。

## 12.5 Webhook 回调

发起任务时提供回调：

```json
{
  "sourceUrl": "...",
  "callbackUrl": "https://partner.example.com/hooks/transcode",
  "callbackMetadata": {
    "tenantId": "t_123"
  }
}
```

完成后：

```http
POST /hooks/transcode
Content-Type: application/json
X-Signature: ...

{
  "eventId": "evt_123",
  "type": "video.transcode.succeeded",
  "operationId": "op_123",
  "occurredAt": "2026-11-26T10:07:12Z",
  "data": { "outputUrl": "..." }
}
```

Webhook 要求：

- 签名验证；
- 重试与退避；
- 去重；
- 顺序说明；
- 失败告警；
- 可重放。

详见第 26 章。

### Webhook 与轮询的组合

最可靠的设计是“双通道”：

- Webhook 用于低延迟通知；
- 操作资源用于补偿和查询；
- 调用方收到 Webhook 后仍查询操作资源确认；
- Webhook 丢失时，轮询兜底。

## 12.6 取消与超时

### 取消

```http
POST /operations/op_123/cancel
```

```json
{
  "reason": "user_requested"
}
```

响应：

```json
{
  "id": "op_123",
  "status": "CANCELLING"
}
```

取消是请求，不是保证。任务可能已经进入不可中断阶段。API 应返回：

- `202`：取消请求已接受；
- `409`：任务已处于终态，无法取消；
- 最终通过查询确认 `CANCELLED` 或 `SUCCEEDED`。

### 取消的语义

- 取消是否释放已占用的资源？
- 取消是否产生费用？
- 取消后结果是否保留？
- 取消是否可撤销？
- 取消的超时是多少？

### 超时

- 为每个任务定义最大执行时间；
- 超时后转为 `TIMED_OUT`（或 `FAILED` + `reason=TIMED_OUT`，二选一）；
- 清理中间资源；
- 通知调用方；
- 允许重试（幂等前提下）；
- 区分“客户端超时”和“任务超时”。

### 超时与重试

任务超时后，调用方可能重试。服务端应：

- 用幂等键返回同一任务；
- 或明确“重试是新任务”；
- 避免无限重试；
- 记录重试次数。

## 12.7 结果交付方式

| 方式 | 适合 |
|---|---|
| 轮询操作资源 | 简单、低频、内部 |
| Webhook | 外部、异步、可靠通知 |
| 事件流 | 多消费者、分析 |
| 预签名 URL | 大文件结果 |
| 邮件/站内信 | 人类用户 |
| 消息队列 | 内部服务 |
| SSE | 需要实时进度 |
| 结果内联 | 小结果、短任务 |

可以组合：完成后写结果到对象存储，发 Webhook，同时操作资源可查询。

### 结果存储与 TTL

- 结果文件有生命周期；
- 过期后返回 `410 Gone`；
- 调用方应尽快下载；
- 结果 URL 应预签名、短期有效；
- 结果可能包含敏感数据，需权限检查。

### 结果的分页

如果结果是一个大列表：

- 结果内联分页；
- 或生成文件；
- 或提供结果资源；
- 不要让调用方一次拉取百万条。

## 12.8 异步 API 的幂等

异步任务的幂等更复杂：

- 重复提交同一请求，是否返回同一个操作 ID？
- 操作 ID 如何与幂等键关联？
- 任务失败后重试，是同一操作还是新操作？
- 取消后重新提交，是否允许？

建议：

- 使用 `Idempotency-Key` 绑定操作；
- 相同键返回同一 `operationId`；
- 明确“重试失败任务”与“重新发起”的区别；
- 操作资源保留足够长时间供查询；
- 幂等键 TTL 覆盖最大重试窗口。

### 重试语义

```text
POST /operations/op_123/retry   # 重试同一操作
POST /video-transcodes          # 重新发起（新操作）
```

两者应区分：

- 重试保留操作 ID、历史、审计；
- 重新发起产生新操作，可能重复计费；
- 重试应幂等；
- 重新发起应受配额限制。

## 12.9 工作流与编排

当任务不是单一步骤，而是多步骤流程：

```text
上传 → 病毒扫描 → 转码 → 生成缩略图 → 发布
```

设计选择：

- **单一操作资源**：对外只暴露一个操作，内部编排；
- **多个操作资源**：每个步骤可查询；
- **工作流引擎**：Temporal、Cadence、Step Functions 等。

对外 API 应隐藏内部步骤，除非调用方需要：

- 查询每步进度；
- 重试某一步；
- 跳过某一步；
- 为每步计费。

### 工作流的状态

```text
PENDING → SCANNING → TRANSCODING → THUMBNAILING → PUBLISHING → SUCCEEDED
              ↓            ↓              ↓             ↓
            FAILED       FAILED         FAILED        FAILED
```

对外可以简化为：

```text
PENDING → RUNNING → SUCCEEDED / FAILED / CANCELLED
```

`progress.stage` 暴露细节。

### 持久化执行

普通异步任务只保存“状态”；持久化执行（durable execution）还保存“执行历史”。工作流引擎（Temporal、Cadence、Step Functions、Restate 等）把每一步的结果、定时器、外部信号写入事件历史，进程崩溃后可以从历史重放，恢复到崩溃前的位置。

```text
Workflow: OrderFulfillment
  1. reserveInventory(orderId)        → 结果写入历史
  2. waitForPayment(orderId, 30m)     → 定时器写入历史
  3. chargePayment(paymentId)         → 结果写入历史
  4. ship(orderId)                    → 结果写入历史
```

对 API 契约的影响：

- **工作流 ID 是业务标识**，不是内部实现细节；调用方用它查询、发信号、取消；
- **重放要求确定性**：工作流代码不能直接读随机数、当前时间、环境变量；这些必须作为活动结果或工作流输入写入历史；
- **活动（activity）至少执行一次**：副作用要有幂等键；
- **定时器是契约的一部分**：“30 分钟未支付则取消”要在文档中写明，且可查询剩余时间；
- **工作流可以运行数天甚至数月**，API 不能假设请求期间进程一直活着。

一个对外的工作流 API 通常包含：

```http
POST /workflows/order-fulfillment
Idempotency-Key: ord_123:fulfillment:v1

{
  "workflowId": "ord_123",
  "input": { "orderId": "ord_123", "warehouseId": "wh_1" }
}
```

```http
HTTP/1.1 202 Accepted
Location: /workflows/ord_123
```

```http
GET  /workflows/ord_123
POST /workflows/ord_123/signals/payment-confirmed
POST /workflows/ord_123/signals/address-updated
GET  /workflows/ord_123/queries/current-step
POST /workflows/ord_123/cancel
```

设计要点：

- `workflowId` 由调用方提供时，要绑定租户并做幂等；
- 信号（signal）是单向、异步、可重复的；重复信号的处理要幂等；
- 查询（query）只读，不能改变工作流状态；
- 取消是请求，最终状态要通过查询确认；
- 终止（terminate）与取消（cancel）语义不同：终止不执行补偿；
- 工作流历史有保留期，过期后只能查最终结果或归档；
- 工作流类型要有版本，升级不能让运行中的实例重放失败。

### 工作流版本化

持久化执行最容易被忽略的发布问题：修改工作流代码后，旧实例重放会走到不同的分支。

```text
v1: 支付成功后直接发货
v2: 支付成功后先风控，再发货
```

一个仍在 `waitForPayment` 的 v1 实例，在 v2 代码部署后恢复，会突然执行风控步骤，而历史里没有对应结果。

处理方式：

- 工作流类型带版本（`OrderFulfillment.v2`），新实例用新类型；
- 用补丁/版本分支让旧实例走旧逻辑；
- 不兼容变更时，等旧实例排空或显式迁移；
- 活动签名变更要兼容旧历史；
- 版本策略写进 API 文档，而不是只写在代码里。

### 人工任务

审批、复核、例外处理是工作流的常见步骤：

```text
PENDING_APPROVAL → APPROVED / REJECTED / EXPIRED
```

API 设计要明确：

- 审批人如何被通知；
- 审批是否有截止时间，超时后自动通过还是升级；
- 审批意见是否必填；
- 能否转交、撤回、批量审批；
- 审批与业务状态是否在同一个事务边界；
- 审计记录是否包含审批人、时间、意见、策略版本；
- 重复审批是否幂等。

人工任务不是“加一个 approve 接口”，而是一个有超时、升级、权限和审计的子状态机。

## 12.10 可观测性

异步任务需要：

- 任务队列长度；
- 等待时间；
- 执行时间分布；
- 成功率/失败率；
- 重试次数；
- 死信数量；
- 每个任务的 Trace；
- 调用方维度统计；
- 成本（转码分钟、token、计算时长）；
- 积压告警；
- 任务年龄（最老任务等了多久）。

没有这些，任务系统会变成黑盒。

### 任务可观测性的关键指标

| 指标 | 含义 | 告警 |
|---|---|---|
| queue_depth | 队列积压 | 持续增长 |
| oldest_task_age | 最老任务等待时间 | 超过 SLO |
| success_rate | 成功率 | 低于阈值 |
| p99_duration | 执行时间 | 超过预算 |
| retry_rate | 重试率 | 突增 |
| dlq_size | 死信数量 | 大于 0 |
| cost_per_task | 单任务成本 | 超预算 |

## 12.11 反模式

- 同步接口内部起线程执行，返回 200 但结果不确定；
- 202 但不提供查询方式；
- 操作资源永远不清理；
- 失败后静默丢弃；
- Webhook 无签名、无重试；
- 轮询无退避；
- 取消接口直接删除任务记录；
- 任务状态与业务状态混为一谈；
- 用 200 + `status: "processing"` 代替 202；
- 操作资源没有租户隔离；
- 任务结果永久保存，成本失控；
- 重试任务产生新操作 ID，导致重复计费。

## 12.12 本章小结

- 长时操作应使用 202 + 操作资源，而不是阻塞连接。
- 操作资源需要清晰的状态机、进度、结果和错误。
- 轮询要退避，Webhook 要签名/重试/去重。
- 取消是请求而非保证，必须有最终状态。
- 异步任务同样需要幂等和可观测性。
- 结果交付方式应与消费者场景匹配。
- 工作流对外应隐藏内部步骤，除非调用方需要。
- 持久化执行把“执行历史”也保存下来；工作流代码要确定性、活动要幂等、版本要兼容旧实例。
- 人工任务是带超时、升级、权限与审计的子状态机，不是一个 approve 接口。
- 异步系统的可观测性比同步系统更重要。
- 大文件导入/导出、分片、断点续传与迁移任务，见[附录 S](../appendices/s-bulk-import-export.md)。
- 运行在 FaaS/边缘上的异步任务，还要考虑超时、包大小、冷启动与事件源语义，见[附录 AC](../appendices/ac-serverless-edge-wasm.md)。

## 设计练习

为“批量发票开具 API”设计：

- 提交批量任务；
- 查询进度；
- 部分成功；
- 下载结果文件；
- 失败重试；
- 取消；
- Webhook 通知。

画出状态机，并定义至少 8 个错误码。然后回答：

1. 部分成功时，整体状态是什么？
2. 失败项如何重试而不重复成功项？
3. 取消后已开具的发票如何处理？
4. 结果文件保留多久？过期后如何重新生成？

---

上一章：[版本化与兼容性](11-versioning.md)  
下一章：[认证：从 API Key 到 OAuth、OIDC 与工作负载身份](../part-2-security-runtime/13-authentication.md)
