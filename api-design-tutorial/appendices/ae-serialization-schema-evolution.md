# 附录 AE：API 序列化、压缩与 Schema 演进

> 序列化不是“把对象转成 JSON”。它决定了契约能表达什么、旧客户端还能不能读、签名能不能对齐、带宽和 CPU 花在哪里，以及一次字段变更会不会悄悄破坏下游。

本附录把“wire format”当作契约的一部分，讨论：

1. 文本与二进制格式的取舍；
2. 压缩、流式与内容协商；
3. Schema 演进的兼容性规则；
4. 规范化、签名与幂等指纹；
5. 选型矩阵与迁移策略。

---

## AE.1 序列化是契约，不是实现细节

同一个领域对象，可以序列化成很多种字节：

```json
{ "amount": 12.34, "currency": "CNY", "paid_at": "2026-11-26T10:00:00Z" }
```

```protobuf
message Payment {
  int64 amount_minor = 1;   // 1234
  string currency = 2;      // "CNY"
  google.protobuf.Timestamp paid_at = 3;
}
```

```cbor
{"amount": 1234, "currency": "CNY", "paid_at": 1764151200}
```

它们看起来表达同一件事，但对消费者意味着完全不同的东西：

- `12.34` 是浮点数还是定点数？跨语言解析是否一致？
- `amount_minor` 的单位是什么？谁负责换算？
- `paid_at` 是字符串、时间戳还是结构化对象？时区在哪里？
- 字段缺失、`null`、默认值分别表示什么？
- 新增字段时，旧消费者会忽略、报错，还是误解？

序列化契约至少包含：

| 维度 | 问题 | 典型决策 |
|---|---|---|
| 语法 | 用什么格式、什么媒体类型 | JSON、Protobuf、CBOR、Avro |
| 类型 | 数字、时间、金额、二进制如何表达 | 字符串十进制、RFC 3339、base64 |
| 存在性 | 缺失、`null`、默认值 | 三态语义、显式 `optional` |
| 兼容 | 新增/删除/重命名字段 | 字段编号、默认值、保留字段 |
| 规范化 | 签名/哈希时字节是否唯一 | JCS、确定性 CBOR |
| 压缩 | 是否压缩、何时压缩 | gzip/br/zstd、阈值、Vary |
| 流式 | 一次返回还是分块 | NDJSON、SSE、chunked |

### 三个先问的问题

1. **谁解析？** 浏览器、移动端、服务、数据管道、AI Agent，能力不同。
2. **如何演进？** 是否需要长期兼容、独立发布、跨组织消费。
3. **代价在哪？** 带宽、CPU、延迟、电量、调试成本、存储成本。

如果答案不清楚，默认选“人类可读、工具成熟、演进规则明确”的方案，通常比追求极致性能更划算。

---

## AE.2 文本格式：JSON 仍是默认，但有边界

### JSON 的能力与陷阱

JSON（RFC 8259）是公开 API 的默认选择，原因是可读、工具多、调试容易、跨语言。但它不是自描述的类型系统：

| 陷阱 | 例子 | 建议 |
|---|---|---|
| 数字精度 | `0.1 + 0.2`、超过 2^53 的整数 | 金额/大整数用字符串 |
| 小数语义 | `12.30` 失去尾零 | 用最小货币单位 + 币种 |
| 重复键 | `{"a":1,"a":2}` | 拒绝或明确取最后一个 |
| `NaN`/`Infinity` | 非标准 JSON | 禁止，返回校验错误 |
| 注释 | JSON 不支持注释 | 不要假设客户端能解析 |
| 尾随逗号 | 非标准 | 严格解析 |
| 编码 | UTF-8 之外的编码 | 明确要求 UTF-8，拒绝 BOM 或正确处理 |
| Unicode | 组合字符、代理对 | 统一 NFC/NFD 策略 |
| 二进制 | 不能直接表达 | base64/base64url，限制大小 |
| 时间 | 字符串格式不统一 | RFC 3339 + 明确时区 |
| `null` vs 缺失 | 语义不同 | 在契约中定义三态 |

#### 数字：最容易被低估的兼容性问题

```json
{
  "order_id": 9007199254740993,
  "amount": 12.30,
  "ratio": 0.1
}
```

- JavaScript 的 `Number` 是 IEEE 754 双精度，`9007199254740993` 会变成 `9007199254740992`；
- `12.30` 解析后可能变成 `12.3`，展示与对账都受影响；
- 不同语言的 decimal 实现、舍入模式不同。

推荐：

```json
{
  "order_id": "ord_01J8Z9K3",
  "amount": { "value": "12.30", "currency": "CNY" },
  "ratio": "0.1"
}
```

金额用字符串十进制 + 币种；大整数用字符串；比例如果需要精确计算，也用字符串或明确精度。

#### 时间：不要只写“时间”

```json
{
  "created_at": "2026-11-26T10:00:00Z",
  "local_date": "2026-11-26",
  "timezone": "Asia/Shanghai"
}
```

- 时刻用 RFC 3339，带偏移或 `Z`；
- 日期用 `YYYY-MM-DD`，不要伪装成时刻；
- 时区用 IANA 名称，不要用 `GMT+8`；
- 闰秒、夏令时、历史时区变更由业务决定，不要假装不存在；
- 排序与比较统一用 UTC，展示再转本地。

#### `null`、缺失与默认值

三种状态必须区分：

| 状态 | 含义 | 示例 |
|---|---|---|
| 字段缺失 | 未提供/未设置 | `{}` |
| 字段为 `null` | 显式清空 | `{"nickname": null}` |
| 字段有值 | 设置为某值 | `{"nickname": "Alice"}` |

在 PATCH、配置、偏好设置中，这三者语义完全不同。契约要写清楚：

- 缺失 = 不修改；
- `null` = 清空；
- 值 = 设置为该值。

如果无法区分，客户端就无法表达“清空”与“不改”。

### JSON Lines / NDJSON

当响应是大量同构对象时，顶层数组有三个问题：

- 必须等全部数据生成完才能开始发送；
- 解析器要一次性持有整个数组；
- 中途失败时，客户端很难知道已收到多少。

NDJSON（Newline Delimited JSON）每行一个 JSON 对象：

```json
{"id":"ord_1","status":"PAID"}
{"id":"ord_2","status":"PENDING"}
{"id":"ord_3","status":"PAID"}
```

媒体类型常用 `application/x-ndjson`。要点：

- 每行必须是完整 JSON，不能跨行；
- 行尾换行是分隔符，最后一行也建议有换行；
- 错误可能发生在流中间，需要定义“已发送部分是否有效”；
- 客户端要能处理背压与取消；
- 不适合需要随机访问或整体排序的场景。

### JSON Text Sequences（RFC 7464）

用 ASCII RS（`0x1E`）分隔 JSON 文本，媒体类型 `application/json-seq`。它比 NDJSON 更明确地处理流边界，但工具支持较少。SSE 则是另一种流式文本协议，适合事件推送而不是批量数据。

### CSV、XML 与表单

- **CSV**：适合导出与人工查看，不适合作为主 API。要处理引号、换行、编码、公式注入（`=cmd`）、类型丢失、locale。
- **XML**：命名空间、Schema、SOAP 遗留系统；新公开 API 很少选它，但企业集成仍会遇到。
- **表单**：`application/x-www-form-urlencoded` 适合简单键值；`multipart/form-data` 适合文件上传；两者都不适合复杂嵌套结构。

---

## AE.3 二进制格式：性能与演进的另一种取舍

### 格式对比

| 格式 | Schema | 人类可读 | 零拷贝 | 演进能力 | 典型场景 | 主要代价 |
|---|---|---|---|---|---|---|
| JSON | 外部（JSON Schema） | 是 | 否 | 中 | 公开 API、配置 | 体积、解析成本、类型弱 |
| NDJSON | 外部 | 是 | 否 | 中 | 流式导出、日志 | 无统一帧、错误处理 |
| Protobuf | 内置（.proto） | 否 | 部分 | 强（字段编号） | gRPC、内部服务 | 工具链、调试 |
| Avro | 内置/注册中心 | 否 | 否 | 强（reader/writer schema） | Kafka、数据湖 | 需要 schema 解析 |
| CBOR | 可选（CDDL） | 否 | 否 | 中 | IoT、COSE、受限环境 | 工具较少 |
| MessagePack | 无 | 否 | 否 | 弱 | 缓存、内部传输 | 无 schema、兼容靠约定 |
| FlatBuffers | 内置 | 否 | 是 | 强 | 游戏、移动端 | 使用复杂、体积 |
| Cap'n Proto | 内置 | 否 | 是 | 强 | 高性能 RPC | 生态较小 |
| Thrift | 内置 | 否 | 否 | 中 | 遗留 RPC | 生态老化 |
| Arrow | 内置 | 否 | 是 | 强 | 列式分析、跨语言 | 不是请求/响应格式 |
| Parquet | 内置 | 否 | 否 | 强 | 数据湖、分析 | 不适合低延迟点查 |

### Protobuf：字段编号就是契约

```protobuf
syntax = "proto3";

message Order {
  string id = 1;
  int64 amount_minor = 2;      // 最小货币单位
  string currency = 3;
  OrderStatus status = 4;
  google.protobuf.Timestamp created_at = 5;
  reserved 6, 7;               // 已删除字段，永不复用
  reserved "legacy_total";
}

enum OrderStatus {
  ORDER_STATUS_UNSPECIFIED = 0;
  ORDER_STATUS_PENDING = 1;
  ORDER_STATUS_PAID = 2;
}
```

规则：

- 字段编号一旦发布就不能复用；删除字段要 `reserved`；
- 不要修改字段类型（尤其 int32/int64/string 之间）；
- 枚举要有 `UNSPECIFIED = 0`，消费者要容忍未知枚举值；
- `optional` 在 proto3 中表达“存在性”，但默认值语义仍要小心；
- `map` 的键顺序不保证，不要依赖序列化字节做签名；
- `Any`/`Struct` 提高灵活性，但削弱 schema 约束；
- Protobuf 的“确定性序列化”并非所有实现都保证，不要把它当规范化格式；
- Editions 正在取代 proto2/proto3 语法，但字段编号规则不变。

### Avro：schema 随数据走

Avro 的读写 schema 分离，适合数据管道：

```json
{
  "type": "record",
  "name": "Order",
  "namespace": "com.example.orders",
  "fields": [
    { "name": "id", "type": "string" },
    { "name": "amount_minor", "type": "long" },
    { "name": "currency", "type": "string", "default": "CNY" },
    { "name": "status", "type": { "type": "enum", "name": "OrderStatus", "symbols": ["PENDING", "PAID", "CANCELLED"] }, "default": "PENDING" }
  ]
}
```

兼容性由 reader schema 与 writer schema 的解析规则决定：

- 新增字段必须有默认值，旧数据才能被新 reader 读取；
- 删除字段需要 reader 有默认值或允许缺失；
- 重命名字段用 `aliases`；
- 枚举新增值对旧 reader 可能失败，需要默认值或升级策略；
- union 变更要谨慎，尤其是 `null` 分支。

Avro 适合 Kafka/数据湖，因为 schema 可以集中管理，消费者可以按版本解析；但它不适合作为公开 HTTP API 的唯一格式，因为客户端需要 schema 才能读懂。

### CBOR：紧凑、可扩展、适合受限环境

CBOR（RFC 8949）是二进制 JSON 的超集，支持：

- 整数、浮点、字节串、文本、数组、映射、标签；
- 确定性编码（deterministic encoding），适合签名与哈希；
- CDDL（RFC 8610）描述 schema；
- COSE 用于签名/加密消息。

示例（诊断记法）：

```text
{
  "id": "ord_1",
  "amount_minor": 1234,
  "currency": "CNY",
  "paid_at": 1764151200
}
```

CBOR 适合 IoT、带宽受限、需要确定性编码的场景；代价是调试工具与人才不如 JSON。

### MessagePack、FlatBuffers、Cap'n Proto

- **MessagePack**：紧凑、库多，但没有 schema，字段增删靠约定；适合缓存与内部传输，不适合长期契约。
- **FlatBuffers / Cap'n Proto**：零拷贝、随机访问，适合游戏、媒体、移动端高性能场景；使用复杂度高，调试困难。
- **Thrift**：遗留 RPC 生态，演进规则类似 Protobuf，但新项目较少选。

### Arrow / Parquet：分析型数据不是请求/响应

Arrow 是列式内存格式，Parquet 是列式存储格式。它们适合：

- 数据平台导出；
- 跨语言分析；
- 大规模扫描与聚合。

不适合：

- 单对象低延迟读写；
- 频繁小更新；
- 需要行级事务的在线 API。

数据平台 API 可以用 Parquet 做批量导出，用 Arrow 做流式分析，但不要把 Parquet 当作在线 API 的响应格式。

---

## AE.4 压缩：带宽、CPU 与安全的三角

### 内容协商

```http
Accept-Encoding: gzip, br, zstd
Content-Encoding: zstd
Vary: Accept-Encoding
```

要点：

- 服务器按 `Accept-Encoding` 选择算法，不能假设客户端支持；
- `Vary: Accept-Encoding` 让缓存按编码区分；
- `Content-Length` 是压缩后长度；流式响应可能没有；
- `ETag` 应随编码变化，或使用弱 ETag；
- 压缩后再加密（TLS 在传输层），不要自己“加密后压缩”。

### 算法取舍

| 算法 | 压缩率 | 速度 | 浏览器支持 | 适用 |
|---|---|---|---|---|
| gzip | 中 | 中 | 广泛 | 兼容性默认 |
| Brotli | 高 | 解压快、压缩慢 | 现代浏览器 | 静态资源、文本 API |
| zstd | 高 | 快 | 逐步增加 | 服务间、大响应、日志 |
| 不压缩 | 无 | 最快 | 全部 | 小响应、已压缩内容 |

压缩不是无条件收益：

- 小响应压缩后可能更大，且有 CPU 开销；
- 图片、视频、zip 等已压缩内容再压缩几乎无收益；
- 移动端解压耗电；
- 高并发下压缩 CPU 可能成为瓶颈；
- 压缩会改变字节，影响签名与 ETag。

### 压缩炸弹与 BREACH

- **压缩炸弹**：小体积压缩数据解压后极大，耗尽内存/CPU。接收方要限制解压后大小、压缩比、嵌套层数。
- **BREACH/CRIME**：当响应包含秘密且攻击者可注入输入时，压缩率会泄露秘密。对包含 CSRF token、会话标识、敏感字段的响应，禁用压缩或隔离攻击者可控内容。
- **流式解压**：边解压边处理，设置上限；不要一次性读入内存。
- **压缩与签名**：先规范化/签名原始语义，再压缩传输；不要对压缩后的字节做业务签名，除非契约明确。

### 压缩策略

```text
if response_size < 1 KB: no compression
elif content_type is already compressed: no compression
elif contains secrets and attacker-controlled input: no compression or isolate
else: negotiate br/zstd/gzip by client support and CPU budget
```

阈值要用真实流量测量，不要照搬。

---

## AE.5 Schema 演进：兼容性是方向，不是口号

### 四种兼容性

| 类型 | 含义 | 谁可以升级 |
|---|---|---|
| Backward（向后兼容） | 新消费者能读旧数据 | 消费者先升级 |
| Forward（向前兼容） | 旧消费者能读新数据 | 生产者先升级 |
| Full（完全兼容） | 双向都可读 | 任意顺序 |
| Transitive（传递兼容） | 与所有历史版本兼容 | 长期数据/事件 |

公开 API 通常追求 **backward + forward 的务实组合**：旧客户端不能被新服务破坏，新客户端要能处理旧服务/旧数据。事件与数据管道通常要求 **full/transitive**，因为消费者可能滞后很久。

### 变更类型与风险

| 变更 | JSON/OpenAPI | Protobuf | Avro | 风险 |
|---|---|---|---|---|
| 新增可选字段 | 通常安全 | 安全（新编号） | 需默认值 | 低 |
| 新增必填字段 | 破坏 | 破坏（旧数据缺失） | 需默认值 | 高 |
| 删除可选字段 | 可能破坏消费者 | 需 `reserved` | 需默认值 | 中 |
| 重命名字段 | 破坏（除非别名） | 破坏（编号不变但语义变） | 用 `aliases` | 高 |
| 类型收窄 | 破坏 | 破坏 | 可能破坏 | 高 |
| 类型放宽 | 通常安全 | 谨慎 | 谨慎 | 中 |
| 枚举新增值 | 旧客户端可能失败 | 需容忍未知值 | 需默认值 | 中 |
| 枚举删除值 | 破坏 | 破坏 | 破坏 | 高 |
| 默认值改变 | 语义破坏 | 语义破坏 | 语义破坏 | 中高 |
| 字段变为 `null` 可选 | 取决于契约 | `optional` | union | 中 |
| 数组/映射结构变化 | 破坏 | 破坏 | 破坏 | 高 |

### JSON Schema 的细节

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["id", "status"],
  "properties": {
    "id": { "type": "string" },
    "status": { "type": "string", "enum": ["PENDING", "PAID"] },
    "note": { "type": ["string", "null"] }
  },
  "additionalProperties": false
}
```

- `additionalProperties: false` 提高严格性，但会让新增字段变成破坏性变更；公开 API 通常允许未知字段以支持演进。
- `required` 只表示“必须出现”，不表示“不能为 null”；两者要分开。
- `oneOf`/`anyOf` 的判别可能歧义，优先用显式判别字段。
- `format` 在多数校验器中是注解而非强制；不要只依赖它。
- `unevaluatedProperties` 适合组合 schema，但工具支持不一。

### Protobuf/Avro 的演进规则

Protobuf：

- 字段编号永不复用；删除用 `reserved`；
- 不要改变字段的 wire type；
- 未知字段要保留（多数实现会保留），避免中间层丢字段；
- 枚举加 `UNSPECIFIED=0`，消费者容忍未知值；
- 不要依赖默认值表达“未设置”，需要存在性时用 `optional` 或 wrapper；
- `oneof` 新增分支对旧消费者是未知字段，通常安全，但业务语义要允许。

Avro：

- 新字段给默认值；
- 删除字段时 reader 要有默认值；
- 重命名用 `aliases`；
- 枚举新增值要给默认值或确保消费者升级；
- union 变更要测试所有 reader；
- Schema Registry 的兼容性模式要与团队发布顺序匹配。

### Schema Registry 与 CI

```yaml
# 概念示例：Confluent Schema Registry 兼容性配置
subject: orders-value
compatibility: BACKWARD_TRANSITIVE
```

CI 中至少要做：

1. 解析 schema，检查语法与引用；
2. 与上一个发布版本做兼容性 diff；
3. 对破坏性变更要求版本升级/迁移计划；
4. 生成代码并与服务端/客户端编译；
5. 用样例数据做 round-trip 测试；
6. 把 schema 版本与 API/事件版本关联。

没有 Registry 时，至少把 schema 放在仓库、打 tag、在 CI 中 diff。

---

## AE.6 规范化、签名与幂等指纹

### 为什么需要规范化

当字节参与安全判断时，“语义相同但字节不同”会导致：

- 签名验证失败；
- 幂等指纹不一致，重复扣款；
- 去重失效，重复处理；
- 缓存键分裂。

典型场景：

- Webhook/HTTP 消息签名；
- 请求体哈希；
- 幂等键的请求指纹；
- 审计日志的完整性校验；
- 事件去重。

### 三种策略

1. **对原始字节签名**：最简单、最安全。接收方必须保留原始 body，不能在验证前解析/重写。
2. **标准规范化**：使用 JCS（RFC 8785）、确定性 CBOR、XML C14N 等标准。
3. **自定义规范化**：通常不推荐；跨语言实现几乎必然出现差异。

### JCS（JSON Canonicalization Scheme，RFC 8785）

JCS 规定：

- 对象键按 UTF-16 代码单元排序；
- 数字按 ECMAScript 序列化规则；
- 字符串按 JSON 转义规则；
- 不允许 `NaN`/`Infinity`；
- 输出无多余空白。

示例：

```json
{"b":2,"a":1}
```

规范化后：

```json
{"a":1,"b":2}
```

JCS 适合签名与哈希，但要注意：

- 数字精度与 ECMAScript 规则绑定；
- 大整数可能丢精度，签名前要用字符串；
- Unicode  normalization 不在 JCS 范围内，业务要统一；
- 不是所有库都完全一致，要做跨语言测试向量。

### Protobuf 不是规范化格式

Protobuf 的序列化字节可能因以下因素不同：

- 字段顺序（实现可能按编号，也可能不保证）；
- 未知字段的保留；
- map 顺序；
- 默认值是否省略；
- varint 编码的非最短形式；
- packed/repeated 的编码选择。

因此：

- 不要对 Protobuf 序列化字节做签名，除非使用明确的确定性模式并验证所有实现；
- 需要签名时，签业务字段的规范化结构，或使用带确定性编码的格式（如 CBOR）；
- 幂等指纹应基于业务语义字段，而不是整个序列化字节。

### 幂等指纹

```text
fingerprint = HMAC-SHA256(
  key = tenant_id,
  message = method + "\n" + canonical_path + "\n" +
            canonical_query + "\n" + SHA256(canonical_body)
)
```

要点：

- 排除易变头部（`Date`、`Request-Id`、`Traceparent`）；
- 包含租户、端点、关键业务字段；
- 对 body 使用规范化或原始字节哈希；
- 指纹算法与版本要记录，迁移时兼容旧指纹；
- 不要把密钥放在客户端可推断的位置；
- 用 HMAC 而不是裸哈希，防止长度扩展与离线猜测。

---

## AE.7 大对象与流式传输

### 分块与流式

| 方式 | 适用 | 注意 |
|---|---|---|
| `Transfer-Encoding: chunked` | 未知长度的流 | 代理/HTTP 版本兼容 |
| NDJSON | 大量同构对象 | 错误中途、背压 |
| SSE | 服务器推送事件 | 单向、重连、Last-Event-ID |
| WebSocket | 双向实时 | 心跳、重连、背压 |
| multipart | 表单/文件 | 边界、大小限制 |
| 预签名 URL | 大文件上传/下载 | 权限、过期、清理 |
| Range 请求 | 断点续传 | ETag/Last-Modified 一致性 |
| tus | 可恢复上传 | 额外协议与存储 |

### 流式 JSON 的工程细节

- 每行/每帧必须是完整可解析单元；
- 定义流中错误的表达方式（错误帧、终止帧、HTTP trailer）；
- 客户端取消时要停止下游工作；
- 背压要能传到数据库/上游；
- 压缩与流式一起用时，注意 flush 与缓冲；
- 不要在流中间改变 schema；
- 记录已发送数量，支持断点续传或至少可诊断。

```python
# NDJSON 生成器：逐条产出，不把全部结果读入内存
def iter_orders(query):
    for row in db.stream(query):
        yield json.dumps(serialize_order(row), ensure_ascii=False) + "\n"
```

### 大对象的契约

- 明确最大尺寸与超限错误；
- 上传使用预签名 URL，避免把大文件塞进 JSON；
- 下载支持 Range 与 ETag；
- 病毒扫描、内容类型校验、文件名清洗；
- 生命周期与清理策略；
- 多租户配额与带宽限制。

---

## AE.8 性能与成本：用测量替代直觉

### 何时二进制真的更快

二进制格式的优势通常在：

- 大量小对象、高 QPS；
- 移动/弱网、带宽受限；
- 需要零拷贝/随机访问；
- 强类型代码生成减少解析错误。

但二进制也有代价：

- 调试困难，排障需要工具；
- schema/代码生成链路更长；
- 跨语言/跨版本兼容更复杂；
- 小 payload 时，序列化开销可能不是瓶颈；
- 压缩后 JSON 的体积差距可能缩小。

### 基准测试方法

不要用“网上说 Protobuf 快 10 倍”做决策。至少测：

1. 同一 schema、同一数据分布；
2. 序列化/反序列化各自耗时；
3. p50/p95/p99，而不是只看平均；
4. CPU、内存分配、GC；
5. payload 大小与压缩后大小；
6. 跨语言实现（客户端语言与服务端语言）；
7. 真实网络条件下的端到端延迟。

一个简化的对比表（需用自己的数据替换）：

| 指标 | JSON | Protobuf | CBOR | 备注 |
|---|---|---|---|---|
| 可读性 | 高 | 低 | 低 | 调试成本 |
| 体积 | 大 | 小 | 小 | 压缩后差距缩小 |
| 解析 CPU | 中 | 低 | 低 | 取决于实现 |
| 演进 | 中 | 强 | 中 | 需要纪律 |
| 工具生态 | 最强 | 强 | 中 | 语言/平台差异 |

### 成本

- 出口带宽：大响应 × 高 QPS × 跨区传输；
- 存储：日志/事件/归档的字节数；
- CPU：压缩/解压、序列化、校验；
- 移动端：流量与电量；
- 调试：二进制格式的排障时间；
- 迁移：格式切换的客户端改造。

---

## AE.9 选型矩阵与迁移

### 按场景选

| 场景 | 首选 | 备选 | 避免 |
|---|---|---|---|
| 公开 REST API | JSON + OpenAPI | JSON:API/HAL | 自定义二进制 |
| 内部高吞吐 RPC | Protobuf/gRPC | Connect、FlatBuffers | 大 JSON + 多层压缩 |
| 事件/Kafka | Avro/Protobuf + Registry | JSON + Schema | 无 schema 的 JSON |
| 数据湖/分析 | Parquet/Arrow | Avro | 在线点查用 Parquet |
| IoT/受限设备 | CBOR/MQTT | MessagePack | 大 XML/JSON |
| 浏览器流式 | NDJSON/SSE | JSON-seq | 顶层大数组 |
| 缓存 | MessagePack/CBOR | JSON | 无版本的对象序列化 |
| 签名/哈希 | JCS/确定性 CBOR | 原始字节 | 解析后再序列化 |

### 决策流程

```text
1. 消费者是谁？公开/内部/设备/数据管道
2. 需要人类可读与易调试吗？
3. 有强 schema 与代码生成需求吗？
4. 数据寿命与兼容窗口多长？
5. 带宽/CPU/电量哪个是瓶颈？
6. 需要签名/规范化吗？
7. 团队与工具链能维护吗？
8. 迁移与回退路径是什么？
```

### 格式迁移

从 JSON 迁移到 Protobuf/CBOR 时：

- 用内容协商双栈运行：`Accept: application/json` 与 `application/protobuf`；
- 先让新客户端用新格式，旧客户端保持 JSON；
- 用同一业务语义做 round-trip 对拍；
- 监控错误率、延迟、体积；
- 定义弃用日期与回退开关；
- 不要把“格式迁移”和“字段语义变更”混在一次发布里。

---

## AE.10 检查清单

### 契约与类型

- [ ] 每个字段有类型、单位、精度、时区、空值语义。
- [ ] 金额用字符串十进制 + 币种，或最小货币单位 + 币种。
- [ ] 大整数用字符串，避免 IEEE 754 精度丢失。
- [ ] 时间用 RFC 3339；日期与时刻分开。
- [ ] 缺失、`null`、默认值三态在契约中写清。
- [ ] 枚举有未知值处理策略。

### 演进

- [ ] 有 schema 与版本，存放在仓库/Registry。
- [ ] CI 做兼容性 diff，破坏性变更被拦截。
- [ ] Protobuf 字段编号不复用，删除字段 `reserved`。
- [ ] Avro 新字段有默认值，重命名用 `aliases`。
- [ ] JSON 新增字段对旧客户端安全，`additionalProperties` 策略明确。
- [ ] 事件/数据管道使用 full/transitive 兼容。

### 规范化与安全

- [ ] 签名验证使用原始字节或标准规范化。
- [ ] 不依赖 Protobuf 序列化字节做签名。
- [ ] 幂等指纹基于业务语义字段，版本化。
- [ ] 压缩前考虑 BREACH/压缩炸弹。
- [ ] 限制解压后大小、请求体大小、嵌套深度。

### 传输与性能

- [ ] 内容协商与 `Vary` 正确。
- [ ] 小响应/已压缩内容不盲目压缩。
- [ ] 流式响应有帧边界、错误表达、背压与取消。
- [ ] 大对象走预签名 URL/Range/分片，不塞进 JSON。
- [ ] 有真实基准测试，不靠直觉选格式。

---

## AE.11 小结

- 序列化是契约的一部分：类型、存在性、兼容性、规范化都会影响消费者。
- JSON 是默认，但金额、大整数、时间、`null` 需要额外纪律。
- 二进制格式在吞吐/体积上有优势，代价是工具、调试与演进复杂度。
- 压缩要权衡带宽、CPU、安全；BREACH 与压缩炸弹是真实风险。
- Schema 演进靠规则与 CI，不靠“大家小心”。
- 签名与幂等要求字节级确定性；JCS/确定性 CBOR 比自定义规范化可靠。
- 选型从消费者、寿命、瓶颈与团队能力出发，不追新。

相关章节：[第 7 章 请求、响应与表示设计](../part-1-foundations/07-representations.md)、[第 10 章 写操作、幂等、并发与一致性](../part-1-foundations/10-writes-idempotency-concurrency.md)、[第 23 章 gRPC 与 Protobuf](../part-3-protocols-architecture/23-grpc-protobuf.md)、[第 25 章 事件驱动](../part-3-protocols-architecture/25-event-driven.md)、[第 30 章 契约优先](../part-3-protocols-architecture/30-contract-first.md)、[附录 AB 数据契约、指标语义与数据产品 API](ab-data-contracts-metrics.md)、[附录 AD API 密码学工程](ad-cryptography-pqc.md)

---

返回：[目录](../SUMMARY.md)
