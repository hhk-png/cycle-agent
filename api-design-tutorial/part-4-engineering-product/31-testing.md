# 第 31 章 API 测试：从单元到契约、模糊与混沌

## 31.1 测试的目标不是覆盖率

API 测试的目标是降低发布风险。覆盖率是手段，不是目标。一个 90% 覆盖率的 API 仍可能：

- 越权；
- 重复扣款；
- 在并发下丢数据；
- 在依赖超时时雪崩；
- 在版本升级后破坏客户端；
- 在恶意输入下崩溃；
- 在时区边界算错日期。

好的测试策略应该覆盖：

- 业务正确性；
- 契约一致性；
- 权限与安全；
- 并发与幂等；
- 失败与恢复；
- 性能与容量；
- 兼容性；
- 数据迁移。

### 测试的边际收益

测试不是越多越好。应该优先测试：

- 不可逆操作（支付、删除、发布）；
- 跨组织边界（公开 API、合作伙伴）；
- 复杂状态机；
- 安全边界；
- 曾经出过事故的地方；
- 难以手工验证的并发场景。

## 31.2 测试层次

```text
        /\
       /  \      E2E（少量）
      /----\
     /      \    Contract（消费者驱动）
    /--------\
   /          \  Integration（API + DB + 依赖）
  /------------\
 /              \ Unit（大量）
/----------------\
```

### 单元测试

测试领域逻辑、校验、状态机、错误映射。快速、稳定、隔离。

适合：

- 金额计算；
- 状态迁移；
- 权限规则；
- 序列化；
- 边界条件。

### 集成测试

测试 API 与数据库、缓存、消息队列、外部依赖的交互。

需要：

- 真实数据库（容器）；
- 迁移；
- 测试数据；
- 清理；
- 网络隔离；
- 确定性；
- 超时；
- 并发控制。

### 契约测试

验证提供方是否满足消费者期望。见第 30 章。

### 端到端测试

从客户端视角验证完整流程。少量、关键路径、真实环境。

### 生产验证

- 金丝雀；
- 合成监控；
- 影子流量；
- 真实用户监控；
- 灰度对比。

## 31.3 API 测试矩阵

每个端点至少覆盖：

| 维度 | 用例 |
|---|---|
| 正常 | 最小请求、完整请求 |
| 校验 | 缺字段、错类型、越界、超长 |
| 认证 | 无 token、过期、错误签名 |
| 授权 | 无权限、他人资源、跨租户 |
| 资源 | 不存在、已删除、已归档 |
| 冲突 | 版本冲突、重复创建、状态非法 |
| 幂等 | 重复请求、并发重复 |
| 分页 | 空结果、首页、末页、非法游标 |
| 依赖 | 超时、错误、限流 |
| 性能 | 大请求、大响应、慢查询 |
| 兼容 | 旧客户端、未知字段、未知枚举 |

### 一个端点的最小测试集

以 `POST /orders` 为例：

```text
1. 正常创建 → 201 + Location
2. 缺少必填字段 → 400 + 字段路径
3. 金额为负 → 422
4. 库存不足 → 422 + 可操作错误
5. 无 token → 401
6. 无权限 → 403
7. 重复 Idempotency-Key → 返回原结果
8. 相同 key 不同 body → 422
9. 并发创建 → 只创建一个
10. 依赖超时 → 504（或明确约定的 503）+ Retry-After
11. 请求体过大 → 413
12. 未知字段 → 400（或忽略，需明确）
```

## 31.4 契约测试

### 消费者驱动

消费者定义期望：

```json
{
  "provider": "order-service",
  "consumer": "mobile-app",
  "interactions": [
    {
      "description": "get order",
      "request": { "method": "GET", "path": "/orders/ord_123" },
      "response": {
        "status": 200,
        "body": {
          "id": "ord_123",
          "status": "PAID",
          "total": { "amount": 12900, "currency": "CNY" }
        }
      }
    }
  ]
}
```

提供方在 CI 中验证。

### Schema 测试

- 请求/响应符合 OpenAPI；
- 错误符合 Problem Details；
- 事件符合 AsyncAPI/CloudEvents；
- 数据库迁移不破坏契约；
- 生成 SDK 与 schema 一致。

### 契约测试的边界

契约测试能发现：

- 字段缺失；
- 类型错误；
- 状态码变化；
- 结构变化。

不能发现：

- 业务语义错误；
- 性能问题；
- 权限问题；
- 并发问题。

## 31.5 属性测试与模糊测试

### 属性测试

定义不变量，让工具生成输入：

```python
from hypothesis import given, strategies as st

@given(
    payment=st.integers(min_value=1, max_value=10**9),
    refunds=st.lists(st.integers(min_value=1, max_value=10**9), max_size=5),
)
def test_total_refunds_never_exceed_payment(payment, refunds):
    state = Payment(amount=payment)
    for requested in refunds:
        # 业务规则：超过剩余可退金额时拒绝，而不是部分退款
        if requested > state.remaining:
            with pytest.raises(RefundExceedsRemaining):
                state.refund(requested)
        else:
            state.refund(requested)
    assert 0 <= state.refunded <= payment
    assert state.refunded + state.remaining == payment
```

属性测试的价值在于：它不只验证一个例子，而是验证“无论输入如何组合，不变量都成立”。上例的不变量是“累计退款不超过支付金额，且退款加剩余恒等于支付金额”。

适合：

- 金额计算；
- 状态机；
- 分页；
- 序列化/反序列化；
- 权限规则；
- 排序稳定性。

### 模糊测试

向 API 发送随机/变异输入，寻找崩溃、注入、解析错误。

工具：

- RESTler；
- Schemathesis；
- OWASP ZAP；
- AFL/libFuzzer（协议层）。

### 模糊测试的价值

- 发现未处理的输入；
- 发现解析器崩溃；
- 发现注入；
- 发现资源耗尽；
- 发现错误信息泄露。

## 31.6 并发与幂等测试

- 同时发送 100 个相同幂等键的请求；
- 同时修改同一资源；
- 并发创建唯一资源；
- 重试期间状态变化；
- 分布式锁失效；
- 消息重复消费；
- 缓存并发回填。

断言：

- 只产生一个副作用；
- 所有响应一致或明确冲突；
- 数据无重复/丢失；
- 幂等键状态正确；
- 版本号单调递增；
- 审计记录完整。

### 并发测试的实现

```python
key = "idem_123"
with ThreadPoolExecutor(max_workers=100) as pool:
    futures = [
        pool.submit(
            client.post,
            "/payments",
            headers={"Idempotency-Key": key},
            json=body,
        )
        for _ in range(100)
    ]
    responses = [f.result() for f in futures]

assert count_success(responses) == 1
assert all(r.status in (200, 201, 409) for r in responses)
```

## 31.7 故障注入

模拟：

- 数据库超时；
- Redis 不可用；
- 消息队列延迟；
- 第三方 500；
- DNS 失败；
- 网络分区；
- 磁盘满；
- 时钟漂移；
- 证书过期。

验证：

- 超时；
- 重试；
- 熔断；
- 降级；
- 错误映射；
- 恢复；
- 告警。

### 故障注入的层次

- 单元测试：mock 异常；
- 集成测试：容器故障；
- 预发：网络代理；
- 生产：混沌工程。

### 混沌工程：从故障注入到 Game Day

故障注入是“制造一个已知故障”；混沌工程是“用受控实验验证系统在不确定故障下的行为”。它需要假设、边界和停止条件，而不是随机杀 Pod。

一个最小实验模板：

```yaml
experiment: payment-dependency-latency
hypothesis: >
  当支付渠道 P99 延迟从 300ms 升到 2s 时，
  订单创建成功率仍高于 99%，
  且 P95 延迟低于 1.5s。
steadyState:
  - metric: http_requests_success_rate
    threshold: "> 0.99"
  - metric: order_create_p95_ms
    threshold: "< 1500"
method:
  - inject: latency
    target: payment-provider
    latency: 2s
    percentage: 20
blastRadius:
  tenants: ["internal", "design-partner"]
  regions: ["ap-east-1"]
  maxDuration: 10m
abortConditions:
  - success_rate < 0.97 for 2m
  - error_budget_burn_rate > 10
  - manual_stop
```

执行步骤：

1. **定义稳态**：用 SLI/SLO 描述“正常”是什么。
2. **提出假设**：故障发生时，哪些指标应保持不变？
3. **选择爆炸半径**：从内部租户、单个区域、小比例流量开始。
4. **注入故障**：延迟、错误、断连、DNS 失败、资源耗尽。
5. **观察**：对比实验组与对照组，不只看全局平均。
6. **停止/回滚**：达到 abort 条件立即停止。
7. **复盘**：记录假设是否成立、系统哪里先坏、告警是否及时。

Game Day 是把实验变成团队演练：

- 提前通知相关团队；
- 指定实验负责人、观察员、回滚负责人；
- 准备客户沟通模板；
- 记录时间线；
- 复盘后把发现转成测试、告警或设计变更。

混沌工程不是“生产环境随便玩”。它需要：

- 可观测性；
- 回滚能力；
- 爆炸半径控制；
- 业务批准；
- 对客户影响的评估；
- 明确的停止权。

## 31.8 安全测试

- 越权（BOLA）；
- 字段级越权；
- 注入；
- SSRF；
- mass assignment；
- 速率限制；
- 认证绕过；
- 令牌校验；
- CORS；
- 敏感信息泄露；
- 错误信息泄露。

工具：

- OWASP ZAP；
- Burp Suite；
- Schemathesis；
- 自研越权扫描。

### 越权测试矩阵

用两个账号 A/B：

```text
A 创建资源 R
B 读取 R → 期望 403/404
B 修改 R → 期望 403/404
B 删除 R → 期望 403/404
B 列出 R → 期望不包含 R
```

对每个资源类型都执行。

## 31.9 性能测试

- 基准延迟；
- 吞吐；
- 并发；
- 尾延迟；
- 长时间稳定性；
- 内存泄漏；
- 连接池；
- 数据库慢查询；
- 限流触发。

见第 33 章。

## 31.10 测试数据

- 工厂/夹具；
- 每测试隔离；
- 不依赖生产数据；
- 脱敏；
- 可重复；
- 清理；
- 租户隔离；
- 确定性。

### 测试数据的反模式

- 共享数据库；
- 测试顺序依赖；
- 硬编码 ID；
- 依赖当前时间；
- 依赖随机数；
- 依赖外部服务。

### 测试时钟与场景构造

对计费、订阅、试用、令牌过期、定时任务和 DST 相关逻辑，真实时间几乎无法稳定测试。需要：

- **可注入时钟**：应用通过 `Clock` 接口取时间，测试传入假时钟；
- **外部测试时钟**：沙箱统一推进时间，覆盖数据库、缓存和异步任务；
- **时间偏移而非跳变**：优先 `+30d`，避免时钟倒退破坏单调性假设；
- **覆盖边界**：月末、闰年、DST 缺口/重叠、账期锚点。

场景构造应通过 API 到达目标状态，而不是直接写数据库：

```text
paid_order() → 通过创建 + 支付接口构造
refunded_order() → 在 paid_order 基础上调用退款
past_due_subscription() → 用测试时钟推进到账期后
```

每个测试运行使用独立租户、独立随机种子和独立时钟；测试结束按租户清理。数据工厂、合成数据、黄金夹具与沙箱设计的完整方法，见[附录 AJ](../appendices/aj-sandbox-mock-test-data.md)。

## 31.11 CI/CD

```text
lint → unit → integration → contract → security → performance (nightly)
→ deploy staging → smoke → canary → production
```

- 快速反馈；
- 失败阻断；
- 可复现；
- 环境一致；
- 测试报告；
- flaky test 管理；
- 并行执行；
- 缓存依赖。

### Flaky 测试

- 隔离；
- 重跑；
- 记录；
- 修复；
- 不要长期忽略；
- 不要用重跑掩盖真实问题。

## 31.12 反模式

- 只测 happy path；
- 测试依赖共享数据库；
- 测试之间相互影响；
- 用 mock 掩盖真实依赖问题；
- 契约测试只在发布前跑；
- 没有越权测试；
- 没有并发测试；
- 性能测试只在上线前做一次；
- 覆盖率造假；
- 测试数据不可复现；
- 测试只验证状态码，不验证业务语义。

## 31.13 本章小结

- 测试目标是降低风险，不是覆盖率。
- 单元、集成、契约、E2E、生产验证各有位置。
- 每个端点都要覆盖校验、认证、授权、冲突、幂等、分页、依赖失败。
- 契约测试连接消费者与提供方。
- 属性/模糊测试能发现边界问题。
- 并发、故障、安全、性能必须被测试。
- 状态机表、性质测试与有状态测试能把“我想到的用例”扩展到“模型允许的路径”；必要时用 TLA+/Alloy，见[附录 AA](../appendices/aa-formal-contracts-model-checking.md)。
- 测试数据要确定、隔离、可清理；时间用可控时钟，状态通过 API 构造，见[附录 AJ](../appendices/aj-sandbox-mock-test-data.md)。
- CI 应快速、稳定、可复现。

## 设计练习

为一个“支付 API”制定测试计划：

- 正常支付；
- 重复支付；
- 余额不足；
- 渠道超时；
- 回调重复；
- 退款并发；
- 越权查询；
- 限流；
- 对账。

为每个场景写出测试层次与断言。然后回答：

1. 哪个场景最适合属性测试？
2. 哪个场景必须做并发测试？
3. 哪个场景只能通过故障注入验证？
4. 哪些测试应该在生产环境做？

---

上一章：[契约优先、代码生成与 Schema 治理](../part-3-protocols-architecture/30-contract-first.md)  
下一章：[可观测性：Trace、Metric、Log 与 API 分析](32-observability.md)
