# 第 23 章 gRPC 与 Protobuf：高性能契约的代价

## 23.1 gRPC 是什么

gRPC 是基于 HTTP/2 的远程过程调用框架，使用 Protobuf 作为接口定义语言（IDL）和序列化格式。

核心特征：

- 强类型契约；
- 代码生成；
- 四种调用模式；
- 二进制传输；
- 多语言支持；
- 基于 HTTP/2 的多路复用；
- 内置截止时间、元数据、状态码；
- 流式传输；
- 拦截器机制。

gRPC 的设计目标不是“替代 REST”，而是“让服务间调用像本地函数一样，同时不丢失网络现实”。

## 23.2 Protobuf 基础

```proto
syntax = "proto3";

package orders.v1;

import "google/protobuf/timestamp.proto";

option go_package = "example.com/api/orders/v1;ordersv1";

message Order {
  string id = 1;
  string customer_id = 2;
  OrderStatus status = 3;
  Money total = 4;
  google.protobuf.Timestamp created_at = 5;
}

enum OrderStatus {
  ORDER_STATUS_UNSPECIFIED = 0;
  ORDER_STATUS_PENDING = 1;
  ORDER_STATUS_PAID = 2;
  ORDER_STATUS_SHIPPED = 3;
  ORDER_STATUS_CANCELLED = 4;
}

message Money {
  int64 amount_minor = 1;
  string currency = 2;
}
```

### 字段编号

- 一旦发布，编号不可复用；
- 删除字段时使用 `reserved`；
- 不要随意重排编号；
- 新增字段用新编号；
- 1–15 编号占 1 字节，适合高频字段；
- 16–2047 占 2 字节；
- 19000–19999 保留给 Protobuf 内部。

```proto
message Order {
  reserved 6, 7;
  reserved "legacy_status";
}
```

### 默认值

proto3 的标量字段有默认值（0、空字符串、false），无法区分“未设置”和“设置为默认值”。需要区分时使用：

- `optional`（proto3 支持显式存在性）；
- `google.protobuf.*Value`（较少用）；
- 额外的 `has_*` 字段（不推荐）。

```proto
message UpdateOrderRequest {
  optional string shipping_address_id = 1;
}
```

### 枚举

- 第一个值必须是 0，通常命名为 `*_UNSPECIFIED`；
- 新增值应追加，不要重排；
- 客户端必须处理未知值；
- 不要复用已删除的值。

### Any 与 Struct

- `google.protobuf.Any`：任意消息，需要类型 URL；
- `google.protobuf.Struct`：动态 JSON 对象；
- 两者都会削弱类型契约，谨慎使用。

## 23.3 服务定义

```proto
service OrderService {
  rpc CreateOrder(CreateOrderRequest) returns (CreateOrderResponse);
  rpc GetOrder(GetOrderRequest) returns (Order);
  rpc ListOrders(ListOrdersRequest) returns (ListOrdersResponse);
  rpc WatchOrders(WatchOrdersRequest) returns (stream OrderEvent);
  rpc UploadOrders(stream CreateOrderRequest) returns (UploadSummary);
  rpc Chat(stream ChatMessage) returns (stream ChatMessage);
}
```

### 四种模式

1. **Unary**：一请求一响应；
2. **Server streaming**：一请求多响应；
3. **Client streaming**：多请求一响应；
4. **Bidirectional streaming**：双向流。

### 流式的使用场景

- 服务端流：订阅、日志尾随、大结果集；
- 客户端流：上传、批量导入、遥测；
- 双向流：聊天、协作、实时控制。

### 流式的注意事项

- 背压；
- 连接生命周期；
- 错误传播；
- 取消；
- 流数量限制；
- 消息大小限制；
- 重连与恢复。

## 23.4 截止时间与取消

```go
ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
defer cancel()

resp, err := client.GetOrder(ctx, &pb.GetOrderRequest{Id: "ord_123"})
```

- 客户端应始终设置 deadline；
- 服务端应传播 deadline；
- 超时返回 `DEADLINE_EXCEEDED`；
- 取消通过 context 传播；
- 不要无限等待。

### Deadline 的传播

```text
Client deadline 2s
  → Service A receives 2s
    → calls Service B with 1.5s remaining
      → calls DB with 1s remaining
```

每一层都应缩短 deadline，留出处理时间。

### 取消

- 客户端取消后，服务端应停止工作；
- 取消应传播到下游；
- 已产生的副作用需要补偿；
- 取消不是回滚。

## 23.5 状态码

gRPC 状态码与 HTTP 不同：

```text
OK
CANCELLED
UNKNOWN
INVALID_ARGUMENT
DEADLINE_EXCEEDED
NOT_FOUND
ALREADY_EXISTS
PERMISSION_DENIED
RESOURCE_EXHAUSTED
FAILED_PRECONDITION
ABORTED
OUT_OF_RANGE
UNIMPLEMENTED
INTERNAL
UNAVAILABLE
DATA_LOSS
UNAUTHENTICATED
```

映射到 HTTP 时（以 grpc-gateway / Google API 的常用映射为准）：

| gRPC | HTTP |
|---|---|
| OK | 200 |
| CANCELLED | 499（客户端关闭请求，非标准但常用） |
| UNKNOWN | 500 |
| INVALID_ARGUMENT | 400 |
| DEADLINE_EXCEEDED | 504 |
| NOT_FOUND | 404 |
| ALREADY_EXISTS | 409 |
| PERMISSION_DENIED | 403 |
| RESOURCE_EXHAUSTED | 429 |
| FAILED_PRECONDITION | 400 |
| ABORTED | 409 |
| OUT_OF_RANGE | 400 |
| UNIMPLEMENTED | 501 |
| INTERNAL | 500 |
| UNAVAILABLE | 503 |
| DATA_LOSS | 500 |
| UNAUTHENTICATED | 401 |

注意：

- `FAILED_PRECONDITION` 在 grpc-gateway 中通常映射为 400，不是 412；412 是 HTTP 条件请求语义，不要混用。
- 499 是 Nginx 的事实标准，不是 IANA 注册状态码。
- 如果同时提供 REST 与 gRPC，应在契约中明确两边的错误码对照，而不是让客户端猜。

### 错误详情

gRPC 支持结构化错误详情：

```proto
message ErrorInfo {
  string reason = 1;
  string domain = 2;
  map<string, string> metadata = 3;
}
```

使用 `google.rpc.Status` 携带：

- 错误码；
- 错误消息；
- 详情（`BadRequest`、`RetryInfo`、`QuotaFailure` 等）。

### 重试

gRPC 支持重试策略：

```json
{
  "maxAttempts": 3,
  "initialBackoff": "0.1s",
  "maxBackoff": "1s",
  "backoffMultiplier": 2,
  "retryableStatusCodes": ["UNAVAILABLE", "DEADLINE_EXCEEDED"]
}
```

只对幂等方法重试；非幂等需要幂等键。

## 23.6 元数据

```go
md := metadata.Pairs(
  "authorization", "Bearer ...",
  "x-request-id", "req_123",
)
ctx := metadata.NewOutgoingContext(ctx, md)
```

元数据类似 HTTP 头，但：

- 键小写；
- 二进制值用 `-bin` 后缀；
- 不应放敏感大对象；
- 网关可能过滤；
- 元数据大小有限；
- 不要用元数据传递业务数据。

### 常见元数据

- `authorization`；
- `x-request-id`；
- `traceparent`；
- `grpc-timeout`；
- `grpc-encoding`；
- 自定义租户 ID。

## 23.7 拦截器

拦截器用于：

- 认证；
- 日志；
- 指标；
- 追踪；
- 重试；
- 限流；
- 错误映射；
- 恢复 panic。

```go
func UnaryAuthInterceptor(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
    if err := authenticate(ctx); err != nil {
        return nil, status.Error(codes.Unauthenticated, "invalid token")
    }
    return handler(ctx, req)
}
```

### 拦截器链

```text
恢复 → 追踪 → 日志 → 认证 → 授权 → 限流 → 业务
```

顺序很重要：认证应在授权前，限流应在业务前，恢复应在最外层。

## 23.8 负载均衡与服务发现

gRPC 长连接会绕过传统 L4 负载均衡。常见方案：

- 客户端负载均衡（DNS、xDS）；
- L7 代理（Envoy）；
- 服务网格；
- Kubernetes headless service；
- 连接池管理。

需要注意：

- 连接复用导致流量不均；
- 重连风暴；
- 健康检查；
- 优雅关闭；
- 连接数上限；
- 子连接（subchannel）；
- 服务发现更新。

### 流量不均

10 个客户端连接 3 个服务实例，可能：

- 每个客户端只连一个实例；
- 某些实例过载；
- 某些实例空闲。

解决：

- 客户端轮询/最少连接；
- L7 代理；
- 服务网格；
- 连接数控制。

## 23.9 浏览器支持

浏览器不直接支持 gRPC（因为无法控制 HTTP/2 帧）。方案：

- **gRPC-Web**：通过代理转换；
- **Connect**：兼容 gRPC、gRPC-Web 和 HTTP/JSON；
- **REST 网关**：grpc-gateway 生成 REST 接口。

Connect 的优势是同一套 Protobuf 服务可以同时支持浏览器、移动端和内部服务。

### 选择

| 方案 | 浏览器 | 流式 | 兼容性 | 复杂度 |
|---|---|---|---|---|
| gRPC-Web | 是（需代理） | 服务端流 | 中 | 中 |
| Connect | 是 | 是 | 好 | 低 |
| REST 网关 | 是 | 否 | 最好 | 中 |
| 原生 gRPC | 否 | 是 | 差 | 低 |

## 23.10 Schema 演进

兼容规则：

- 不要改变字段编号；
- 不要改变字段类型（除非兼容，如 int32→int64 需谨慎）；
- 删除字段用 `reserved`；
- 新增字段应是可选的；
- 枚举新增值可能破坏旧客户端；
- 不要复用字段名/编号；
- 包名/服务名变更等于新 API；
- 不要改变字段语义。

### 兼容的变更

- 新增消息字段；
- 新增 RPC 方法；
- 新增枚举值（如果客户端宽容）；
- 新增服务；
- 新增包。

### 破坏的变更

- 删除字段；
- 改字段类型；
- 改字段编号；
- 改字段语义；
- 删除 RPC；
- 改 RPC 签名；
- 改包名。

### 破坏性变更检测

使用 `buf breaking` 等工具在 CI 中检测：

```bash
buf breaking --against '.git#branch=main'
```

### 版本策略

- 包名带版本：`orders.v1`、`orders.v2`；
- 同一主版本内保持兼容；
- 新主版本并行；
- 弃用旧版本；
- 不要用字段编号做版本。

### 更新语义与 FieldMask

gRPC 的 `Update` 常常需要回答：客户端只想改一个字段时，其他字段是“保持不变”还是“清零”？proto3 的默认值无法区分“未设置”和“显式设为 0/空字符串”。`google.protobuf.FieldMask` 是常用解法：

```proto
import "google/protobuf/field_mask.proto";

message UpdateOrderRequest {
  Order order = 1;
  google.protobuf.FieldMask update_mask = 2;
  string etag = 3;  // 乐观并发
}
```

```text
update_mask.paths = ["shipping_address", "items"]
```

语义：

- 空 `update_mask`：可以约定“更新所有字段”或“拒绝”，必须在契约中二选一；
- 只更新 mask 中的字段；
- 嵌套字段用点路径，如 `shipping_address.city`；
- 重复字段可用 `items` 整体替换，或定义更细的合并规则；
- 服务端要校验字段是否允许更新，拒绝未知/只读路径；
- 配合 `etag`/`update_time` 做乐观并发；
- JSON 映射中 `FieldMask` 通常是逗号分隔的 camelCase 路径，注意与 proto 的 snake_case 转换。

不要让客户端用“全量对象 + 默认值”表达部分更新，否则无法区分“没传”和“清零”。

## 23.11 性能

优势：

- 二进制编码，体积小；
- HTTP/2 多路复用；
- 强类型，减少解析开销；
- 流式传输；
- 连接复用；
- 头部压缩。

代价：

- 人类不可读；
- 调试工具要求高；
- 代理/网关兼容性；
- 代码生成依赖；
- 大消息可能造成内存压力；
- 流式连接管理复杂；
- 连接复用导致负载不均；
- 移动网络下长连接维护成本。

### 性能不是唯一标准

一个 JSON API 增加 10ms 解析开销，可能远小于一次跨区网络调用。不要为了“性能”选择团队无法运维的协议。

## 23.12 何时使用 gRPC

适合：

- 内部微服务；
- 高性能、低延迟；
- 强类型契约；
- 多语言；
- 流式传输；
- 服务网格；
- 需要代码生成。

不适合：

- 公开 API 面向广泛开发者；
- 浏览器直连；
- 需要 HTTP 缓存；
- 简单 CRUD；
- 团队缺乏 Protobuf/网关经验；
- 需要人类直接调试。

### 决策清单

1. 消费者是否是内部服务？
2. 是否需要强类型和代码生成？
3. 是否需要流式？
4. 是否能接受二进制调试成本？
5. 是否有网关/代理支持？
6. 团队是否能维护 proto 生态？
7. 是否需要 HTTP 缓存？
8. 是否需要公开开发者接入？

## 23.13 本章小结

- gRPC 是强类型、高性能的 RPC 框架，基于 HTTP/2 和 Protobuf。
- 字段编号和 schema 演进规则必须严格遵守。
- 始终设置 deadline，传播取消。
- 状态码、元数据、拦截器是工程化关键。
- 浏览器需要 gRPC-Web/Connect 或 REST 网关。
- gRPC 适合内部服务，不一定适合公开 API。
- 性能之外，还要考虑调试、运维和团队能力。
- Protobuf 的字段编号、未知字段与“序列化字节不是规范化格式”的工程含义，见[附录 AE](../appendices/ae-serialization-schema-evolution.md)。

## 设计练习

为一个“实时车队追踪系统”设计 gRPC 服务：

- 车辆上报位置（客户端流）；
- 调度员订阅车辆更新（服务端流）；
- 查询历史轨迹（unary）；
- 下发调度指令（双向流）；
- 认证与授权；
- 断线重连。

写出 proto 定义与错误处理策略。然后回答：

1. 位置上报如何避免重复和乱序？
2. 订阅断线后如何补齐？
3. 调度指令如何保证幂等？
4. 如何限制单个客户端的流数量？

---

上一章：[REST 与超媒体：成熟度、链接与发现](22-rest-and-hypermedia.md)  
下一章：[GraphQL：灵活查询与治理成本](24-graphql.md)
