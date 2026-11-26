# 附录 F：gRPC / Protobuf 风格指南

## F.1 目录结构

```text
proto/
└── orders/
    └── v1/
        ├── orders.proto
        ├── orders_service.proto
        └── orders_types.proto
```

规则：

- 包名 `company.domain.v1`；
- 目录与包名一致；
- 版本在包名中；
- 每个主版本独立目录；
- 不要跨版本复用消息。

## F.2 命名

```proto
syntax = "proto3";

package acme.orders.v1;

import "google/protobuf/timestamp.proto";

option go_package = "example.com/api/orders/v1;ordersv1";
option java_package = "com.acme.orders.v1";
option java_multiple_files = true;

message Order {
  string id = 1;
  string customer_id = 2;
  OrderStatus status = 3;
  Money total = 4;
  google.protobuf.Timestamp created_at = 5;
}

enum OrderStatus {
  ORDER_STATUS_UNSPECIFIED = 0;
  ORDER_STATUS_PENDING_PAYMENT = 1;
  ORDER_STATUS_PAID = 2;
  ORDER_STATUS_SHIPPED = 3;
  ORDER_STATUS_CANCELLED = 4;
}

message Money {
  int64 amount_minor = 1;
  string currency = 2;
}
```

规则：

- 消息 `PascalCase`；
- 字段 `snake_case`；
- 枚举 `UPPER_SNAKE_CASE`；
- 枚举第一个值必须是 `*_UNSPECIFIED = 0`；
- 服务 `PascalCase` + `Service`；
- 方法 `PascalCase`；
- 请求/响应以 `Request`/`Response` 结尾。

## F.3 字段编号

- 1～15 编码更省空间，给高频字段；
- 发布后不可复用；
- 删除字段用 `reserved`；
- 不要重排；
- 不要改类型；
- 新增字段用新编号。

```proto
message Order {
  reserved 6, 7;
  reserved "legacy_status", "old_total";
}
```

## F.4 可选性

proto3 标量默认值无法区分“未设置”和“设置为 0”：

```proto
message UpdateOrderRequest {
  optional string note = 1;
  optional int32 quantity = 2;
}
```

或用 `google.protobuf.StringValue`（不推荐，优先 `optional`）。

## F.5 服务定义

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

## F.6 分页

```proto
message ListOrdersRequest {
  int32 page_size = 1;
  string page_token = 2;
  string filter = 3;
  string order_by = 4;
}

message ListOrdersResponse {
  repeated Order orders = 1;
  string next_page_token = 2;
}
```

规则：

- `page_size` 有上限；
- `page_token` 不透明；
- 不返回总数，除非必要；
- 排序稳定。

## F.7 字段掩码

```proto
import "google/protobuf/field_mask.proto";

message UpdateOrderRequest {
  Order order = 1;
  google.protobuf.FieldMask update_mask = 2;
}
```

用于部分更新，避免“零值是否要更新”的歧义。

## F.8 错误

```proto
import "google/rpc/status.proto";
import "google/rpc/error_details.proto";
```

注意：`google/rpc/*.proto` 来自 googleapis 仓库，不是 protobuf 的 well-known types。使用前要确认构建环境已包含这些 proto，或把它们 vendor 到仓库中；否则 `protoc` 会报找不到文件。

返回：

```text
status.Code: INVALID_ARGUMENT
message: "quantity must be between 1 and 99"
details:
  - BadRequest.FieldViolation
```

规则：

- 使用标准 gRPC 状态码；
- 错误消息可读；
- 使用 `details` 提供结构化信息；
- 不泄露内部堆栈；
- 区分可重试/不可重试。

## F.9 截止时间

```go
ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
defer cancel()
resp, err := client.GetOrder(ctx, req)
```

- 客户端必须设置 deadline；
- 服务端传播；
- 超时返回 `DEADLINE_EXCEEDED`；
- 不要无限等待。

## F.10 元数据

```go
md := metadata.Pairs(
  "authorization", "Bearer ...",
  "x-request-id", "req_123",
)
ctx := metadata.NewOutgoingContext(ctx, md)
```

- 键小写；
- 二进制用 `-bin`；
- 不放敏感大对象；
- 网关可能过滤。

## F.11 拦截器

```go
func UnaryAuthInterceptor(
    ctx context.Context,
    req any,
    info *grpc.UnaryServerInfo,
    handler grpc.UnaryHandler,
) (any, error) {
    if err := authenticate(ctx); err != nil {
        return nil, status.Error(codes.Unauthenticated, "invalid token")
    }
    return handler(ctx, req)
}
```

用于：

- 认证；
- 日志；
- 指标；
- 追踪；
- 重试；
- 限流；
- 错误映射。

## F.12 版本演进

兼容：

- 新增字段；
- 新增 RPC；
- 新增枚举值（需客户端宽容）；
- 保留字段编号。

破坏：

- 删除/重命名字段；
- 改类型；
- 改编号；
- 删除 RPC；
- 改包名；
- 改语义。

检测：

```bash
buf breaking --against '.git#branch=main'
```

## F.13 浏览器

- gRPC-Web；
- Connect；
- grpc-gateway；
- 不要把 gRPC 直接暴露给浏览器。

## F.14 检查清单

- [ ] 包名/版本正确。
- [ ] 字段编号不复用。
- [ ] 枚举有 `UNSPECIFIED`。
- [ ] 请求/响应命名规范。
- [ ] 分页有上限。
- [ ] 错误使用标准码。
- [ ] deadline 传播。
- [ ] 认证/授权拦截器。
- [ ] 可观测性。
- [ ] 破坏性检测。
- [ ] 文档/示例。
- [ ] 代码生成配置。

---

返回：[目录](../SUMMARY.md)
