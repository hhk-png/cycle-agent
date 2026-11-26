# 附录 AA：API 契约的形式化、模型检验与性质测试

> 大多数 API 事故不是“代码写错了”，而是“设计本身允许了错误发生”：状态机少了一个终态、并发路径没有互斥、补偿操作不幂等、权限判断漏了一个分支。测试只能证明我们想到了的路径；形式化方法试图证明“在所有可达状态下，某条性质都成立”。本附录不要求读者成为形式化方法专家，而是给出一套从轻到重的工具箱：什么时候用状态机表，什么时候写 TLA+，什么时候只需要性质测试。

## AA.1 形式化不是学术装饰

先看一个典型的状态机漏洞。退款 API 的规则是：

- 只有 `PAID` 或 `PARTIALLY_REFUNDED` 的订单可以退款；
- 累计退款不能超过支付金额；
- 退款完成后订单变为 `REFUNDED` 或 `PARTIALLY_REFUNDED`；
- 已取消的订单不能退款。

代码可能写成：

```python
def refund(order, amount):
    if order.status not in ("PAID", "PARTIALLY_REFUNDED"):
        raise InvalidState()
    if amount <= 0:
        raise InvalidAmount()
    if order.refunded + amount > order.paid:
        raise RefundExceedsPayment()
    order.refunded += amount
    order.status = "REFUNDED" if order.refunded == order.paid else "PARTIALLY_REFUNDED"
    save(order)
```

看起来正确。但并发下：

```text
两个请求同时读到 refunded = 800，paid = 1000
各自检查 800 + 300 <= 1000 通过
各自写回 refunded = 1100
```

不变量被破坏。测试如果只发一个请求，永远发现不了；如果发并发请求但没有断言“退款总额 ≤ 支付金额”，也可能通过。

形式化的价值在于：**它强迫你把“允许发生什么”写下来，然后系统地探索所有交错。** 它不是替代测试，而是把测试的靶子从“我想到的例子”扩展到“模型允许的所有路径”。

### 三种深度

| 层次 | 方法 | 成本 | 适合 |
|---|---|---|---|
| 轻量 | 状态机表、决策表、不变量清单 | 小时级 | 所有 API |
| 中量 | 性质测试、有状态测试、契约测试 | 天级 | 核心写路径、并发逻辑 |
| 重量 | TLA+/Alloy/模型检验 | 周级 | 支付、库存、权限、复制协议 |

大多数团队应该先做轻量，再对“错了会资损/泄露/不可恢复”的路径做中量，只在真正复杂且高风险的协议上做重量。不要为了形式化而形式化。

## AA.2 不变量、前后条件与精化

### AA.2.1 不变量

不变量（invariant）是在系统每个可达状态都必须为真的命题。API 设计中常见的不变量：

```text
退款总额 ≤ 支付金额
库存预占 + 已售 ≤ 总库存
订单状态只能沿状态机迁移
每个租户的资源只能被该租户的成员访问
幂等键对应的副作用最多发生一次
发票终态后不可修改
账本借贷平衡
```

不变量要写成可判定的形式。反例：

```text
系统是安全的
```

无法测试。正例：

```text
对任意订单 o：sum(o.refunds.amount) ≤ o.paid.amount
对任意请求 r：若 r 返回 201，则存在唯一资源 id 与 r.idempotency_key 关联
```

### AA.2.2 前后条件

每个操作都应有前置条件（precondition）与后置条件（postcondition）：

```text
refund(order, amount):
  前置：order.status ∈ {PAID, PARTIALLY_REFUNDED}
        amount > 0
        order.refunded + amount ≤ order.paid
  后置：order.refunded' = order.refunded + amount
        order.status' = REFUNDED if order.refunded' = order.paid
                         else PARTIALLY_REFUNDED
        产生一条不可变退款记录
        发出 RefundCompleted 事件
```

前置条件决定哪些请求应被拒绝；后置条件决定成功后系统必须处于什么状态。API 契约里的错误码，本质上就是前置条件失败的可识别分类。

### AA.2.3 精化

精化（refinement）回答：“实现是否满足抽象契约？”

```text
抽象规格：退款总额不超过支付金额
实现：数据库行锁 + 事务 + 唯一约束
精化映射：实现状态中的 refunded 字段对应抽象状态中的 refunded
```

形式化精化需要证明实现的每个行为都对应抽象规格的某个行为。工程上通常不做完整证明，而是：

- 用同一组不变量约束设计与实现；
- 用性质测试在实现上验证不变量；
- 用模型检验在设计上验证不变量；
- 用契约测试验证接口行为与规格一致。

## AA.3 状态机作为契约

### AA.3.1 状态机表

把状态机写成表，是最便宜的形式化：

| 当前状态 | 操作 | 守卫 | 下一状态 | 副作用 |
|---|---|---|---|---|
| `PENDING` | `pay` | 金额匹配 | `PAID` | 记账、发事件 |
| `PENDING` | `cancel` | 无 | `CANCELLED` | 释放库存 |
| `PAID` | `refund` | 金额 ≤ 可退 | `PARTIALLY_REFUNDED` / `REFUNDED` | 退款记录 |
| `PAID` | `ship` | 地址有效 | `SHIPPED` | 发事件 |
| `CANCELLED` | `pay` | — | 拒绝 | 无 |
| `REFUNDED` | `refund` | — | 拒绝 | 无 |

这张表可以直接生成：

- 状态迁移测试；
- 非法迁移测试；
- 前端按钮可用性；
- 文档中的状态图；
- 数据库约束（可选）。

### AA.3.2 状态机的常见缺陷

- **缺少终态**：状态永远可以继续迁移，导致“已退款订单还能发货”；
- **终态可逆**：`REFUNDED → PAID`，破坏审计；
- **隐式状态**：用多个布尔字段组合表达状态，产生非法组合；
- **副作用不在迁移内**：状态改了，库存没释放；
- **并发迁移**：两个请求同时从 `PAID` 迁移到不同状态；
- **补偿状态缺失**：失败后没有 `REFUND_FAILED` 或人工处理状态。

### AA.3.3 用代码表达状态机

```python
from enum import Enum

class OrderState(Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"

TRANSITIONS = {
    (OrderState.PENDING, "pay"): OrderState.PAID,
    (OrderState.PENDING, "cancel"): OrderState.CANCELLED,
    (OrderState.PAID, "ship"): OrderState.SHIPPED,
    (OrderState.PAID, "refund"): OrderState.PARTIALLY_REFUNDED,
    (OrderState.PARTIALLY_REFUNDED, "refund"): OrderState.PARTIALLY_REFUNDED,
}

def transition(state, action):
    next_state = TRANSITIONS.get((state, action))
    if next_state is None:
        raise InvalidTransition(state, action)
    return next_state
```

这段代码只表达“允许的迁移”，不表达“守卫”和“副作用”。生产实现应把三者放在同一个事务边界内，并用数据库条件更新或行锁保证原子性。

## AA.4 TLA+：验证并发协议

TLA+ 适合描述并发与分布式协议：状态、动作、时序性质。它不直接执行代码，而是让模型检验器（TLC）探索所有可达状态。

### AA.4.1 一个退款规格

下面是一个简化但可运行的 TLA+ 规格。它把退款拆成“读取并记录”和“提交”两步，用来复现前面提到的读-改-写竞态。

```tla
---------------- MODULE Refund ----------------
EXTENDS Integers, Sequences

CONSTANT Paid

VARIABLES refunded, history, pending

Init ==
    /\ refunded = 0
    /\ history = <<>>
    /\ pending = {}

\* 第一步：读到当前 refunded，记录在请求里；此时不做最终检查
Begin(id, amount) ==
    /\ id \in 1..2
    /\ amount \in 1..Paid
    /\ pending' = pending
        \union {[id |-> id, amount |-> amount, seen |-> refunded]}
    /\ UNCHANGED <<refunded, history>>

\* 第二步：用读取时的 seen 做检查，然后写入当前 refunded
Commit(req) ==
    /\ req \in pending
    /\ req.seen + req.amount <= Paid
    /\ refunded' = refunded + req.amount
    /\ history' = Append(history, req.amount)
    /\ pending' = pending \ {req}

Next ==
    \/ \E id \in 1..2, amount \in 1..Paid : Begin(id, amount)
    \/ \E req \in pending : Commit(req)
    \/ UNCHANGED <<refunded, history, pending>>

\* 不变量：累计退款不超过支付金额
Invariant ==
    refunded <= Paid

RECURSIVE Sum(_)
Sum(seq) ==
    IF seq = <<>> THEN 0
    ELSE Head(seq) + Sum(Tail(seq))

\* 另一个不变量：历史记录之和等于 refunded
HistoryMatches ==
    Sum(history) = refunded

====
```

在 TLC 中配置 `Paid = 10`、`INVARIANT Invariant`，它会探索所有可达状态，并给出反例：

```text
Begin(1, 6) 在 refunded = 0 时记录 seen = 0
Begin(2, 6) 也在 refunded = 0 时记录 seen = 0
Commit(1) 检查 0 + 6 <= 10，通过 → refunded = 6
Commit(2) 仍检查 0 + 6 <= 10，通过 → refunded = 12
Invariant violated
```

把 `Commit` 的守卫从 `req.seen + req.amount <= Paid` 改成 `refunded + req.amount <= Paid`，反例消失。这说明：**不变量检查必须与写入在同一个原子步骤内**，否则就需要版本号、CAS、行锁或幂等记录来补上竞态窗口。

### AA.4.2 规格能发现什么

- 并发交错导致的不变量破坏；
- 缺少守卫；
- 死锁（所有状态都无法继续）；
- 活锁/饥饿（在公平性假设下仍无法推进）；
- 状态机不可达状态；
- 补偿路径缺失。

### AA.4.3 规格不能发现什么

- 实现与规格不一致（除非做精化证明或测试）；
- 性能问题；
- 网络分区下的真实时序（除非显式建模）；
- 数据库隔离级别的细节；
- 业务需求本身错了。

TLA+ 的价值是“在设计阶段发现协议级错误”，不是“证明代码无 bug”。

### AA.4.4 什么时候值得写 TLA+

- 支付、清算、账本；
- 库存预占与释放；
- 分布式锁、选主、复制；
- 权限委派与撤销；
- 多步骤审批/工作流；
- 幂等键的并发语义。

如果一个协议的失败会导致资损、数据不可恢复或大范围越权，且团队对并发交错的直觉不够可靠，就值得写。

## AA.5 Alloy：验证数据模型约束

Alloy 适合描述对象之间的关系与约束，并在有限范围内寻找反例。它比 TLA+ 更轻，适合验证：

- 租户隔离；
- 权限模型；
- 资源所有权；
- 组织层级；
- 引用完整性。

### AA.5.1 一个租户隔离模型

```alloy
sig Tenant {}
sig User { tenant: one Tenant }
sig Document {
    owner: one User,
    tenant: one Tenant
}

fact TenantConsistency {
    all d: Document | d.tenant = d.owner.tenant
}

assert NoCrossTenantAccess {
    all d: Document, u: User |
        u.tenant = d.tenant implies u = d.owner
}
check NoCrossTenantAccess for 5
```

`check` 会寻找反例。上面的断言显然过强（同租户的其他用户也应该能访问），但它能帮助你明确“访问”到底由什么关系定义。更真实的模型会引入 `Role`、`Grant`、`Group`，并检查：

```alloy
assert NoOrphanDocument {
    all d: Document | some u: User | d.owner = u
}

assert TenantCannotOwnOtherTenantDoc {
    all d: Document | d.tenant = d.owner.tenant
}
```

### AA.5.2 Alloy 的边界

- 只检查有限范围（`for 5` 表示最多 5 个原子），不是无限证明；
- 找不到反例不等于证明，但能提高信心；
- 适合结构约束，不适合长时间并发时序；
- 建模成本高于状态机表，低于 TLA+。

## AA.6 模型检验的边界

| 问题 | 说明 |
|---|---|
| 状态爆炸 | 变量一多，可达状态指数增长；需要抽象、对称性削减、约束 |
| 环境建模 | 网络、时钟、故障模型的选择直接影响结论 |
| 公平性 | 不假设弱公平/强公平，可能得到不现实的活锁反例 |
| 精化 | 规格正确不等于实现正确；需要映射与额外证明 |
| 需求 | 规格只能验证“写下来的性质”，不能验证“性质写对了” |
| 维护 | 规格会随业务漂移，需要像代码一样评审 |

因此，形式化规格应聚焦“最贵、最不稳定”的核心，而不是覆盖所有 CRUD。

## AA.7 性质测试与有状态测试

对大多数团队，性质测试是性价比最高的“半形式化”手段。

### AA.7.1 性质测试

```python
from hypothesis import given, strategies as st, assume

@given(
    paid=st.integers(min_value=1, max_value=10**9),
    amounts=st.lists(st.integers(min_value=1, max_value=10**9), max_size=20),
)
def test_refund_invariant(paid, amounts):
    order = Order(paid=paid)
    for amount in amounts:
        assume(amount <= order.remaining)
        order.refund(amount)
        assert 0 <= order.refunded <= paid
        assert order.refunded + order.remaining == paid
```

性质测试会生成大量输入组合，但不会自动探索并发交错。它适合验证：

- 金额、数量、库存的算术不变量；
- 序列化/反序列化往返；
- 分页不重不漏；
- 状态机的合法迁移；
- 权限规则的单调性（如权限撤销后不能恢复）。

### AA.7.2 有状态测试

有状态测试（stateful testing）把 API 当成状态机，随机生成操作序列，并在每一步检查模型与实现是否一致：

```python
class OrderModel:
    def __init__(self, paid):
        self.paid = paid
        self.refunded = 0

    def refund(self, amount):
        if amount <= 0 or self.refunded + amount > self.paid:
            raise InvalidRefund()
        self.refunded += amount

class OrderMachine(RuleBasedStateMachine):
    @rule(amount=st.integers(min_value=1, max_value=1000))
    def refund(self, amount):
        try:
            self.api.refund(self.order_id, amount)
            self.model.refund(amount)
        except InvalidRefund:
            with pytest.raises(ClientError):
                self.api.refund(self.order_id, amount)
        assert self.api.get(self.order_id)["refunded"] == self.model.refunded
```

它能发现：

- 实现与模型的行为差异；
- 非法状态迁移；
- 幂等键失效；
- 部分成功处理错误；
- 错误码与模型不一致。

### AA.7.3 性质测试的陷阱

- 性质写错，测试会“假绿”；
- 生成器分布不合理，覆盖不到边界；
- 只测单线程，发现不了并发；
- 依赖真实时间/随机数，导致不可复现；
- 失败后没有保留最小反例（Hypothesis 会自动 shrink，要保存）。

## AA.8 线性一致性检查

线性一致性是并发对象的强保证。检查方法是：

1. 记录每个操作的调用时间、返回时间、输入、输出；
2. 构造一个历史（history）；
3. 让检查器寻找一个全序，使每个操作在其调用与返回之间原子生效，且满足对象语义；
4. 找不到 → 违反线性一致。

工具：

- Jepsen（Clojure）；
- Knossos（线性一致性检查器）；
- Porcupine（Go）；
- Elle（事务隔离级别检查）。

对 API 的适用方式：

```text
客户端并发调用：
  POST /counter/increment
  GET /counter
记录每个请求的 start/end/返回值
用检查器验证“读到的值”是否对应某个合法的原子顺序
```

大多数业务 API 不需要完整的线性一致性检查；但对分布式锁、库存、计数器、选主、账本，这项检查能发现“看起来能用、偶发丢更新”的问题。

## AA.9 把形式化接入工程

### AA.9.1 分级接入

| 级别 | 做法 | 触发条件 |
|---|---|---|
| L0 | 状态机表 + 不变量清单 | 所有核心资源 |
| L1 | 性质测试 + 有状态测试 | 金额、库存、权限、幂等 |
| L2 | 线性一致性检查 | 分布式锁、计数器、账本 |
| L3 | TLA+/Alloy 规格 + 模型检验 | 新协议、跨区域复制、支付清算 |

### AA.9.2 放进 CI

- L0：文档评审检查，状态机表与代码枚举一致；
- L1：每次 PR 跑性质测试（限制用例数），夜间跑更长时间；
- L2：夜间或发布前跑历史检查；
- L3：规格变更时跑 TLC，结果归档；不必每次 PR 跑。

### AA.9.3 规格的维护

- 规格与代码同仓库；
- 规格变更需要评审；
- 规格中的常量、假设、故障模型写清楚；
- 记录“验证了什么、没验证什么”；
- 规格过期比没有规格更危险。

## AA.10 三个案例

### AA.10.1 退款

不变量：`refunded ≤ paid`。并发缺陷：两个请求同时通过检查。修复：

- 数据库条件更新：`UPDATE orders SET refunded = refunded + ? WHERE id = ? AND refunded + ? <= paid`；
- 或行锁 + 事务；
- 或幂等键 + 唯一退款记录。

验证：性质测试覆盖金额组合；并发测试断言总额；TLA+ 验证协议。

### AA.10.2 库存预占

不变量：`reserved + sold ≤ stock`。常见缺陷：

- 预占与释放不幂等；
- 超时释放与支付成功竞争；
- 多仓库库存聚合错误；
- 负库存被“修正”成 0。

验证：

- 状态机：`AVAILABLE → RESERVED → SOLD` / `RESERVED → AVAILABLE`；
- 性质测试：任意预占/释放序列后不变量成立；
- 故障注入：释放消息丢失、重复、延迟。

### AA.10.3 优惠券

不变量：一张券最多核销一次；每用户最多用 N 张；过期不能用。常见缺陷：

- 并发核销；
- 退款后重复使用；
- 时区导致过期判断错误；
- 券与订单金额的舍入不一致。

验证：

- 唯一约束 `(coupon_id, user_id)`；
- 状态机 `ISSUED → REDEEMED → VOID`；
- 性质测试覆盖过期边界、退款、部分退款；
- 对账：券核销数 = 订单折扣数。

## AA.11 检查清单

- [ ] 每个核心资源都有状态机表，包含守卫、副作用、终态。
- [ ] 每个写操作都有前置/后置条件与错误码映射。
- [ ] 关键不变量写成可判定命题，并在测试中断言。
- [ ] 并发路径有原子性保证（条件更新/锁/唯一约束）。
- [ ] 金额、库存、权限、幂等有性质测试。
- [ ] 有状态测试覆盖操作序列，而不只是单请求。
- [ ] 分布式锁/计数器/账本考虑线性一致性检查。
- [ ] 新协议在实现前有 TLA+/Alloy 或其他模型，且记录假设。
- [ ] 形式化规格与代码同仓库、有评审、有失效日期。
- [ ] 明确“验证了什么、没验证什么”，不把模型检验当证明。

## AA.12 小结

- 形式化的目标是发现“设计允许的错误”，不是替代测试。
- 状态机表和不变量清单是最便宜的起点，应覆盖所有核心资源。
- 性质测试与有状态测试能系统覆盖输入与操作序列，性价比最高。
- TLA+ 适合并发协议与分布式状态机；Alloy 适合关系约束与权限模型。
- 模型检验有状态爆炸、环境假设与精化边界，结论要写清适用范围。
- 线性一致性检查适合锁、计数器、账本等强并发对象。
- 形式化要分级接入 CI，规格要与代码一起维护。

---

相关章节：[第 10 章 写操作、幂等、并发与一致性](../part-1-foundations/10-writes-idempotency-concurrency.md)、[第 12 章 异步任务与长时操作](../part-1-foundations/12-async-and-long-running.md)、[第 31 章 API 测试](../part-4-engineering-product/31-testing.md)、[第 34 章 可靠性与容错](../part-4-engineering-product/34-reliability.md)、[附录 Z 分布式一致性与多区域 API 设计](z-distributed-consistency-multi-region.md)

返回：[目录](../SUMMARY.md)
