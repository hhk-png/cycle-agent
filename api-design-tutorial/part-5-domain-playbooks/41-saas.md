# 第 41 章 SaaS 平台 API

## 41.1 SaaS API 的领域核心

SaaS 平台的 API 不只是“业务数据 CRUD”，还要管理：

- 组织/租户；
- 用户与成员；
- 角色与权限；
- 邀请与加入；
- 订阅与套餐；
- 功能权益；
- 用量计量；
- 审计日志；
- SSO/SCIM；
- 数据导出/删除。

这些是“平台能力”，跨所有业务模块。

### 平台能力 vs 业务能力

- 平台能力：组织、成员、权限、计费、审计；
- 业务能力：项目、文档、工单、报表。

平台能力不稳定，业务能力就无法发展。它们通常由平台团队提供，业务团队消费。

## 41.2 组织与用户模型

```text
Organization (tenant)
├── Membership
│   ├── User
│   └── Role
├── Invitation
├── Subscription
├── Entitlements
└── AuditLog
```

### 关键决策

- 用户是否可属于多个组织？
- 组织是否可嵌套？
- 删除组织时用户如何处理？
- 个人账户 vs 组织账户？
- 跨组织协作如何表达？
- 用户身份是否全局唯一？
- 是否支持访客？

### 常见模型

**模型 A：用户全局，成员关系属于组织**

```text
User (global)
Membership (user_id, org_id, role)
```

优点：用户可加入多个组织；缺点：成员关系复杂。

**模型 B：用户属于组织**

```text
Organization → User (org_id)
```

优点：简单；缺点：跨组织协作难。

**模型 C：个人账户 + 组织账户**

```text
User (personal)
Organization (business)
```

优点：灵活；缺点：身份切换复杂。

大多数 SaaS 采用模型 A。

## 41.3 组织 API

```text
POST /organizations
GET  /organizations/{orgId}
PATCH /organizations/{orgId}
DELETE /organizations/{orgId}
GET  /organizations/{orgId}/members
POST /organizations/{orgId}/invitations
GET  /organizations/{orgId}/audit-logs
```

### 创建组织

```http
POST /organizations
```

```json
{
  "name": "Acme Inc.",
  "slug": "acme",
  "region": "cn",
  "plan": "pro"
}
```

响应：

```json
{
  "id": "org_123",
  "name": "Acme Inc.",
  "slug": "acme",
  "status": "ACTIVE",
  "createdAt": "2026-11-26T10:00:00Z"
}
```

### 组织状态

```text
PENDING → ACTIVE → SUSPENDED → DELETED
```

- `PENDING`：待验证；
- `ACTIVE`：正常；
- `SUSPENDED`：暂停（欠费/违规）；
- `DELETED`：已删除。

### Slug 的唯一性

- 全局唯一；
- 不可随意改；
- 保留字；
- 大小写；
- 重定向。

## 41.4 成员与邀请

```text
GET    /organizations/{orgId}/members
POST   /organizations/{orgId}/invitations
GET    /invitations/{token}
POST   /invitations/{token}/accept
DELETE /organizations/{orgId}/members/{userId}
PATCH  /organizations/{orgId}/members/{userId}
```

邀请要点：

- 邮箱验证；
- 过期时间；
- 角色；
- 重发；
- 撤销；
- 域限制；
- 审计；
- 重复邀请；
- 已是成员。

### 邀请状态

```text
PENDING → ACCEPTED
        ├→ EXPIRED
        ├→ REVOKED
        └→ DECLINED
```

### 成员移除

- 转移所有权；
- 保留内容；
- 审计；
- 通知；
- 冷却期。

## 41.5 角色与权限

SaaS 通常需要：

- 系统角色（Owner、Admin、Member、Viewer、Billing）；
- 自定义角色；
- 资源级权限；
- 功能权限；
- API scope。

`Billing` 可以是固定角色，也可以是一个可授予的权限包；如果两者都存在，要在 API 与文档中明确它是否包含 `billing:write`、发票查看、支付方式管理等权限。

```json
{
  "role": "admin",
  "permissions": [
    "members:invite",
    "billing:read",
    "projects:create"
  ]
}
```

### 最小权限

- Owner 不可删除自己；
- 至少保留一个 Owner；
- 敏感操作二次确认；
- 审计；
- 角色变更通知。

### 角色模型

| 角色 | 权限 |
|---|---|
| Owner | 全部，包括删除组织 |
| Admin | 成员、设置、业务 |
| Member | 业务读写 |
| Viewer | 只读 |
| Billing | 账单 |

## 41.6 订阅与套餐

```text
GET  /subscriptions/{id}
POST /subscriptions
PATCH /subscriptions/{id}
POST /subscriptions/{id}/cancel
GET  /plans
```

订阅状态不是一条单向链：

```text
TRIALING → ACTIVE ⇄ PAST_DUE
              │         │
              │         └→ ACTIVE（催收成功）
              ├→ PAUSED ⇄ ACTIVE
              ├→ CANCELED（立即或期末）
              └→ EXPIRED（试用/宽限结束）
```

| 状态 | 含义 | 允许的下一步 |
|---|---|---|
| `TRIALING` | 试用中 | `ACTIVE`、`CANCELED`、`EXPIRED` |
| `ACTIVE` | 正常服务 | `PAST_DUE`、`PAUSED`、`CANCELED` |
| `PAST_DUE` | 扣款失败，催收中 | `ACTIVE`（恢复）、`CANCELED`、`EXPIRED` |
| `PAUSED` | 暂停服务 | `ACTIVE`、`CANCELED` |
| `CANCELED` | 已取消 | `EXPIRED` |
| `EXPIRED` | 已结束 | — |

还需要一个独立字段表达“期末取消”：

```json
{
  "status": "ACTIVE",
  "cancelAtPeriodEnd": true,
  "currentPeriodEnd": "2026-12-01T00:00:00Z"
}
```

不要用 `status=CANCELED` 表示“将在期末取消”，否则调用方无法区分“现在仍可用”和“已经停用”。催收恢复、暂停、降级、按比例计费都要能映射到这张表。
- 付款失败；
- 税务；
- 发票；
- 优惠券；
- 合同价。

### 升级/降级

- 立即生效 vs 周期末；
- 按比例计费；
- 权益变化；
- 数据保留；
- 降级限制（超出配额）。

## 41.7 权益（Entitlements）

不要在整个代码里散落 `if plan == "pro"`。使用权益：

```json
{
  "features": {
    "sso": true,
    "auditLogRetentionDays": 365,
    "maxProjects": 50,
    "apiRateLimitPerMinute": 5000
  }
}
```

API：

```text
GET /organizations/{orgId}/entitlements
```

权益来源：

- 套餐；
- 附加包；
- 试用；
- 合同；
- 促销；
- 手动覆盖。

### 权益的优先级

```text
手动覆盖 > 合同 > 附加包 > 套餐 > 默认
```

需要明确：

- 冲突时谁赢；
- 变更何时生效；
- 缓存 TTL；
- 审计。

### 权益的检查

- 服务端强制；
- 不依赖前端；
- 缓存；
- 变更失效；
- 审计。

## 41.8 用量计量

```text
POST /usage-events
```

```json
{
  "organizationId": "org_123",
  "meter": "api.requests",
  "quantity": 1,
  "unit": "request",
  "occurredAt": "2026-11-26T10:00:00Z",
  "recordedAt": "2026-11-26T10:00:01Z",
  "idempotencyKey": "req_abc:api.requests"
}
```

要点：

- 幂等；
- 去重；
- 迟到事件；
- 聚合；
- 账单周期；
- 配额；
- 超额；
- 审计；
- 退款/调整。

### 计量事件的设计

- 唯一 ID；
- 租户；
- 计量器；
- 数量；
- 时间；
- 幂等键；
- 属性；
- 来源。

### 计量与账单

```text
用量事件 → 聚合 → 账单行 → 发票
```

需要保证：

- 同一事件只计一次；
- 迟到事件能补；
- 调整可追溯；
- 账单可解释。

## 41.9 SSO 与 SCIM

企业客户通常要求：

- SAML/OIDC SSO；
- SCIM 用户/组同步；
- 域验证；
- 强制 SSO；
- JIT provisioning。

SCIM 端点：

```text
GET  /scim/v2/Users
POST /scim/v2/Users
PATCH /scim/v2/Users/{id}
GET  /scim/v2/Groups
```

SCIM 是标准协议（RFC 7643/7644），可以降低集成成本。

### SSO 配置

- IdP metadata；
- 证书；
- 域；
- 默认角色；
- JIT；
- 强制 SSO；
- 备用登录。

### SCIM 的坑

- 分页；
- 过滤；
- 属性映射；
- 删除语义；
- 组同步；
- 冲突；
- 速率限制。

## 41.10 审计日志

```text
GET /organizations/{orgId}/audit-logs?actor=...&action=...&from=...&to=...
```

记录：

- 谁；
- 做了什么；
- 对什么；
- 何时；
- IP/设备；
- 结果；
- 请求 ID；
- 策略版本。

审计日志应：

- 不可篡改；
- 可导出；
- 保留合规期限；
- 支持搜索；
- 租户隔离；
- 访问控制。

## 41.11 数据导出与删除

合规要求：

- 导出全部数据；
- 删除账户；
- 删除组织；
- 数据保留；
- 子处理者列表；
- 数据驻留。

API：

```text
POST /organizations/{orgId}/exports
GET  /exports/{id}
DELETE /organizations/{orgId}
```

删除是异步、可审计、可恢复（在保留期内）的流程。

### 删除流程

```text
1. 请求删除
2. 冷静期
3. 停用
4. 导出（可选）
5. 删除主数据
6. 删除衍生数据
7. 备份过期
8. 审计保留
```

### 导出格式

- JSON；
- CSV；
- 附件；
- 清单；
- 校验和。

## 41.12 多租户与计费

- 租户 ID 贯穿所有资源；
- 用量按租户；
- 账单按组织；
- 权限按成员；
- 缓存按租户；
- 日志按租户。

### 计费模型

- 按席位；
- 按用量；
- 按功能；
- 混合；
- 合同价。

### 账单的可解释性

客户需要知道：

- 用了什么；
- 单价；
- 折扣；
- 税费；
- 总额；
- 为什么涨了。

## 41.13 本章小结

- SaaS API 的平台能力包括组织、成员、角色、订阅、权益、用量、审计、SSO/SCIM。
- 多租户隔离贯穿所有层。
- 权益比硬编码套餐更可维护。
- 用量计量必须幂等、可对账。
- SSO/SCIM 是企业客户的刚需。
- 审计、导出、删除是合规基础。
- 订阅计费的计量、按比例、发票、贷项与收入确认，见[附录 V](../appendices/v-metering-billing.md)；账期锚点、按比例计算与金额舍入的语义，见[附录 AH](../appendices/ah-primitive-semantics.md)。
- 如果早期用 Supabase/Firebase/Hasura 等 BaaS 快速搭建，组织、成员、行级权限与密钥边界最终要收回，见[附录 AF](../appendices/af-database-as-api-baas.md)。

## 设计练习

为“团队协作 SaaS”设计 API：

- 组织、成员、邀请；
- 角色与权限；
- 套餐与权益；
- 用量计量；
- SSO/SCIM；
- 审计；
- 导出/删除。

写出核心资源、状态机与多租户规则。然后回答：

1. 用户属于多个组织时，令牌如何表达？
2. 降级后超出配额的数据如何处理？
3. 用量事件迟到如何计费？
4. 删除组织后，个人用户数据如何保留？

---

上一章：[支付与金融 API](40-payments.md)  
下一章：[IoT 与设备 API](42-iot.md)
