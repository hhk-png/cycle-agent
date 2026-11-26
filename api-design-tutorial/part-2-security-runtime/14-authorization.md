# 第 14 章 授权：RBAC、ABAC、ReBAC 与策略即代码

## 14.1 授权比认证更容易出错

认证失败通常表现为“进不来”，容易发现；授权失败可能表现为“看到了不该看的数据”，而且长期无人察觉。

OWASP API Security Top 10（2023）把 **Broken Object Level Authorization（BOLA）** 列为 API 风险第一位。原因很简单：API 通常用资源 ID 直接寻址，只要忘记检查“这个资源是否属于当前用户”，就会造成越权。

```http
GET /invoices/inv_123
Authorization: Bearer <user_A_token>
```

如果服务端只验证令牌有效，没有验证 `inv_123` 属于用户 A，那么用户 A 就能读取用户 B 的发票。

这类漏洞无法靠“加个角色”解决，必须在每个对象访问点做检查。

BOLA 的可怕之处在于：

- 接口看起来“正常工作”；
- 日志里是 200，不是 403；
- 监控不会报警；
- 只有数据泄露被利用时才被发现；
- 修复需要逐个端点排查。

## 14.2 授权的四个层次

### 1. 功能级授权

用户能否调用某个端点？

```text
orders:create
orders:read
orders:cancel
```

通常用 scope 或权限点表达。

功能级授权回答“这个用户是不是客服”，但不回答“这个客服能不能看这一单”。

### 2. 对象级授权

用户能否操作这个具体对象？

```text
user_A 能否读取 invoice inv_123？
```

这是 BOLA 的核心，必须逐对象检查。

对象级授权需要知道：

- 资源的租户/组织；
- 资源的所有者；
- 资源的共享关系；
- 当前用户的角色；
- 当前操作的上下文。

### 3. 字段级授权

用户能否看到/修改某个字段？

```text
客服可以看到客户手机号，但不能看到完整银行卡号；
普通用户可以看到自己的邮箱，但不能看到别人的。
```

字段级授权常见于：

- PII（个人身份信息）；
- 支付信息；
- 医疗记录；
- 财务数据；
- 内部备注。

### 4. 行级/租户级授权

用户能否访问某个租户、组织、项目下的数据？

```text
tenant_id 必须与令牌中的租户声明一致。
```

租户级授权是“横向隔离”的基础。没有它，一个租户可以看到另一个租户的数据。

### 四个层次的关系

```text
功能级：能调用吗？
  ↓
租户级：属于我的租户吗？
  ↓
对象级：这个对象归我管吗？
  ↓
字段级：这个字段能看吗？
```

四层都应检查。只做功能级授权，等于“有门禁就能进所有房间”。

## 14.3 RBAC

基于角色的访问控制：

```text
User → Role → Permissions
```

示例：

```text
role: admin
  - orders:read
  - orders:write
  - users:manage

role: support
  - orders:read
  - refunds:create
```

### 优点

- 直观；
- 易于管理；
- 适合职责清晰的系统；
- 容易审计；
- 容易与组织架构对应。

### 缺点

- 角色爆炸；
- 难以表达细粒度条件（“只能看自己部门的订单”）；
- 角色变更影响面大；
- 难以处理临时授权；
- 难以表达资源关系；
- 容易变成“超级角色”。

### 设计建议

- 角色是权限的集合，不要把角色硬编码在业务逻辑中；
- 权限点使用稳定命名；
- 支持角色继承时要小心冲突；
- 定期审计角色成员；
- 区分系统角色与租户自定义角色；
- 避免“admin 可以一切”的捷径；
- 角色变更要审计。

### 角色爆炸

当出现以下角色时，说明 RBAC 不够：

```text
sales_apac_manager_readonly
sales_apac_manager_readwrite
sales_emea_manager_readonly
support_tier1_refund_limited
support_tier2_refund_full
```

角色数量随“地区 × 职能 × 权限 × 租户”组合爆炸。此时应考虑 ABAC 或 ReBAC。

## 14.4 ABAC

基于属性的访问控制：

```text
Allow if:
  subject.department == resource.department
  AND subject.level >= resource.requiredLevel
  AND time.now < resource.expiresAt
```

属性来源：

- 用户属性（部门、职级、地区）；
- 资源属性（所有者、分类、敏感级别）；
- 环境属性（时间、IP、设备、风险评分）；
- 操作属性（读/写/删除）。

### 优点

- 表达力强；
- 减少角色爆炸；
- 适合动态条件；
- 可以表达“工作时间”“同部门”“风险低”等规则。

### 缺点

- 策略复杂；
- 属性质量依赖强；
- 难以调试；
- 性能开销；
- 审计困难；
- 属性来源可能不可信。

### ABAC 的工程挑战

- 属性从哪里来？令牌、数据库、用户服务？
- 属性是否实时？
- 属性缺失时默认允许还是拒绝？
- 策略如何测试？
- 策略变更如何回滚？
- 如何解释“为什么拒绝”？

**建议**：ABAC 策略应默认拒绝，属性缺失时拒绝，并提供决策日志。

## 14.5 ReBAC

基于关系的访问控制：

```text
user:alice is member of group:eng
group:eng is viewer of folder:design
folder:design contains document:spec
→ alice can view document:spec
```

ReBAC 适合：

- 文档/项目协作；
- 组织层级；
- 共享权限；
- 继承关系；
- 多租户协作产品。

Google Zanzibar 是代表性系统，开源实现包括 SpiceDB、OpenFGA。

### Zanzibar 模型

核心概念：

- **Object**：`document:spec`；
- **Relation**：`viewer`、`editor`、`owner`；
- **Tuple**：`document:spec#viewer@user:alice`；
- **Userset**：`group:eng#member`；
- **Rewrite rule**：`viewer = owner ∪ editor ∪ viewer of parent`。

查询：

```text
Check(user:alice, viewer, document:spec) → true
```

### 优点

- 自然表达“谁和谁有关系”；
- 支持继承与共享；
- 适合多租户协作产品；
- 权限变更即时生效（取决于一致性）；
- 可以回答“谁能看这个文档”。

### 缺点

- 关系图查询成本；
- 一致性（关系变更后权限何时生效）；
- 建模复杂；
- 需要专门基础设施；
- 深度递归可能导致慢查询；
- 调试困难。

### ReBAC 的一致性

关系写入后，权限检查可能：

- 强一致：立即生效，成本高；
- 最终一致：延迟生效，需要容忍窗口；
- 读己之写：写入者立即看到。

对安全敏感的撤销操作，应优先强一致或短延迟。

## 14.6 Scope 不是权限

OAuth scope 常被用作权限：

```text
scope: orders:read orders:write
```

但 scope 通常表达“应用被允许请求的能力范围”，不等于“当前用户对某个对象有权限”。

一个应用有 `orders:read` scope，不代表它能读所有用户的订单。仍需：

- 用户委托范围；
- 对象级检查；
- 租户隔离；
- 字段级过滤。

**Scope 是上限，不是授权结果。**

### Scope 的设计

- 粗粒度：`orders:read`、`orders:write`；
- 细粒度：`orders:read:own`、`orders:read:all`；
- 资源限定：`orders:read` + `resource` 参数；
- 不要用 scope 表达对象 ID。

Scope 过多会导致：

- 用户同意页面难以理解；
- 令牌变大；
- 权限管理复杂；
- 客户端请求过多 scope。

### Scope 与用户同意

用户同意页面应说明：

- 应用能做什么；
- 能访问哪些数据；
- 是否代表用户操作；
- 是否可以撤销。

不要用模糊的“访问您的数据”掩盖广泛权限。

## 14.7 策略即代码

把授权逻辑从业务代码中抽离，用可测试、可审计的策略表达：

### OPA / Rego

```rego
package authz

default allow := false

allow if {
  input.user.tenant_id == input.resource.tenant_id
  input.action == "read"
  "orders:read" in input.user.permissions
}

allow if {
  input.user.id == input.resource.owner_id
  input.action in {"read", "update"}
}
```

### Cedar

```cedar
permit(
  principal == User::"alice",
  action == Action::"read",
  resource == Document::"spec"
);
```

### 优点

- 集中管理；
- 可测试；
- 可审计；
- 支持复杂条件；
- 策略与代码分离；
- 可以版本化和回滚。

### 缺点

- 学习成本；
- 延迟；
- 策略漂移；
- 需要版本化与回滚；
- 不能替代对象级检查的工程纪律；
- 策略错误可能导致大面积拒绝或放行。

### 策略的测试

策略应像代码一样测试：

```text
allow(user_alice, read, doc_spec) == true
allow(user_bob, read, doc_spec) == false
allow(user_alice, delete, doc_spec) == false
```

测试应覆盖：

- 正常路径；
- 越权路径；
- 属性缺失；
- 角色边界；
- 租户边界；
- 时间/环境条件。

### 策略的部署

- 策略版本化；
- 灰度发布；
- 决策日志；
- 回滚；
- 与 API 版本解耦；
- 变更审计。

## 14.8 对象级授权的实现模式

### 模式一：每次查询都带租户/用户条件

```sql
SELECT *
FROM invoices
WHERE id = $1 AND tenant_id = $2 AND owner_id = $3;
```

优点：简单、可靠。缺点：每个查询都必须记得加条件。

改进：

- 使用数据库行级安全；
- 使用 repository 层强制注入条件；
- 使用代码生成；
- 使用 lint 检查。

### 模式二：授权服务

```text
POST /authorize
{
  "subject": "user:alice",
  "action": "read",
  "resource": "invoice:inv_123"
}
```

返回 `allow/deny`。适合集中策略，但增加网络调用和延迟。

优化：

- 批量授权；
- 本地缓存决策；
- 决策 TTL；
- 只缓存 allow，不缓存 deny；
- 权限变更时失效缓存。

### 模式三：数据层强制

使用行级安全（PostgreSQL RLS）、视图、租户 schema，确保即使应用层忘记过滤也不会越权。

示例：

```sql
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON invoices
  USING (tenant_id = current_setting('app.tenant_id')::text);
```

优点：最后一道防线；缺点：调试复杂、性能开销、跨租户查询困难。

### 模式四：能力令牌

令牌中包含可访问资源的范围：

```json
{
  "sub": "user_123",
  "scope": "invoices:read",
  "resource": ["inv_123", "inv_456"]
}
```

适合短期、细粒度访问，但令牌可能变大，且资源列表变化需要重新签发。

### 模式五：策略决策点与执行点分离

```text
PEP（Policy Enforcement Point）→ PDP（Policy Decision Point）
```

- PEP 在网关或服务内；
- PDP 集中评估策略；
- 决策包含原因；
- 支持审计。

## 14.9 多租户授权

关键规则：

1. 令牌必须包含租户/组织声明；
2. 每个请求都必须验证资源属于该租户；
3. 不允许通过修改 URL 中的租户 ID 越权；
4. 管理后台跨租户访问需要显式角色与审计；
5. 缓存键必须包含租户 ID；
6. 日志与事件必须带租户 ID；
7. 数据库查询必须带租户条件；
8. 后台任务必须带租户上下文。

常见漏洞：

- 只用 `tenant_id` 过滤列表，不检查单对象；
- 租户 ID 来自请求体而不是令牌；
- 缓存未按租户隔离；
- 后台接口忘记租户检查；
- 跨租户复制数据时未校验；
- 管理 API 使用超级权限。

### 租户切换

用户属于多个租户时：

- 令牌包含当前租户；
- 切换租户需要重新签发令牌或使用租户头；
- 租户头必须与令牌中的可访问租户列表匹配；
- 切换要审计；
- 不要让客户端随意指定租户。

## 14.10 委托与模拟

### 委托

用户授权应用代表自己操作：

```text
user → delegates to app → app calls API on behalf of user
```

需要：

- 明确的委托范围；
- 用户可撤销；
- 令牌包含 `sub`（用户）与 `client_id`（应用）；
- 审计双方身份；
- 委托链可追踪。

### 模拟（Impersonation）

管理员以用户身份操作：

- 必须显式授权；
- 记录操作者与被模拟者；
- 限制时间与范围；
- 高风险操作禁止；
- 用户可感知；
- 审计不可篡改；
- 模拟令牌短期有效。

### 混淆代理（Confused Deputy）

一个高权限服务被诱导代表低权限用户执行操作：

```text
攻击者 → 低权限请求 → 高权限服务 → 访问敏感资源
```

防护：

- 每次调用都检查用户权限；
- 不要用服务身份代替用户身份；
- 令牌绑定用户；
- 最小权限；
- 审计委托链。

## 14.11 授权审计

每个敏感操作应记录：

- 谁（subject）；
- 代表谁（actor/delegate）；
- 对什么（resource）；
- 做了什么（action）；
- 何时（timestamp）；
- 从哪里（IP/device）；
- 结果（allow/deny）；
- 策略版本；
- 请求 ID；
- 决策原因。

审计日志应防篡改、可查询、保留合规要求的时间。

### 审计的隐私

审计日志本身可能包含敏感信息：

- 不要记录完整请求体；
- 对 PII 脱敏；
- 限制访问；
- 保留期；
- 跨境传输合规。

## 14.12 测试授权

- 每个端点都要有“无权限”测试；
- 每个对象都要有“他人对象”测试；
- 测试角色边界；
- 测试租户边界；
- 测试字段级过滤；
- 测试委托/模拟；
- 使用属性测试生成组合；
- 定期做越权扫描；
- 测试权限变更后的缓存失效。

### 授权测试矩阵

| 主体 | 资源 | 动作 | 期望 |
|---|---|---|---|
| 所有者 | 自己的对象 | read | allow |
| 其他用户 | 他人对象 | read | deny |
| 同租户管理员 | 租户内对象 | read | allow |
| 其他租户管理员 | 跨租户对象 | read | deny |
| 无角色用户 | 任意对象 | write | deny |
| 被委托应用 | 用户对象 | read | allow（scope 内） |

### 自动化越权扫描

- 用两个测试账号交叉访问；
- 枚举资源 ID；
- 检查响应差异；
- 检查错误信息是否泄露；
- 在 CI 中运行。

## 14.13 本章小结

- 授权必须在功能、对象、字段、租户四个层次检查。
- BOLA 是 API 最常见、最危险的漏洞。
- RBAC 简单但难以表达细粒度条件；ABAC 表达力强但复杂；ReBAC 适合关系型协作。
- Scope 是权限上限，不是对象级授权。
- 策略即代码可集中管理，但不能替代工程纪律。
- 多租户必须从令牌、查询、缓存、日志全链路隔离。
- 委托与模拟需要显式授权和完整审计。
- 授权需要审计与持续测试。

## 设计练习

为“医疗记录 API”设计授权：

- 医生、护士、患者、管理员；
- 跨科室访问；
- 急诊临时授权；
- 患者授权家属查看；
- 审计要求。

选择 RBAC/ABAC/ReBAC 的组合，并写出关键策略与测试用例。然后回答：

1. 急诊临时授权如何过期？
2. 患者授权家属后，如何撤销？
3. 医生跨科室访问需要什么条件？
4. 如何证明“没有越权”？

---

上一章：[认证：从 API Key 到 OAuth、OIDC 与工作负载身份](13-authentication.md)  
下一章：[限流、配额、并发与成本控制](15-rate-limits-quotas.md)
