# 第 40 章 支付与金融 API

## 40.1 支付 API 的第一原则：钱不能错

支付系统与普通业务系统最大的区别：

- 错误会直接造成资金损失；
- 重复执行可能导致重复扣款；
- 状态不一致会引发对账灾难；
- 合规要求严格；
- 故障影响信任；
- 监管介入。

因此支付 API 的设计必须围绕：

- 幂等；
- 状态机；
- 审计；
- 对账；
- 补偿；
- 安全；
- 合规。

### 支付的“不可能三角”

```text
一致性、可用性、延迟
```

支付通常优先一致性：

- 宁可拒绝，不可错扣；
- 宁可延迟，不可重复；
- 宁可人工，不可自动错账。

## 40.2 核心概念

```text
PaymentIntent   支付意图，描述“要收多少钱”
PaymentMethod   支付方式（卡、银行、钱包）
Charge          一次扣款尝试
Transaction     资金流水
Refund          退款
Dispute         争议/拒付
Payout          结算/打款
Ledger          账本
```

### PaymentIntent

表示一次支付的完整生命周期：

```json
{
  "id": "pi_123",
  "amount": 12900,
  "currency": "CNY",
  "amountCapturable": 12900,
  "amountCaptured": 0,
  "captureMethod": "manual",
  "status": "requires_confirmation",
  "paymentMethod": "pm_456",
  "clientSecret": "...",
  "createdAt": "2026-11-26T10:00:00Z"
}
```

状态：

```text
requires_payment_method → requires_confirmation → requires_action（3DS/SCA）
        │                         │                      │
        └─────────────────────────┴──────────────────────┘
                                  ↓
                              processing
                              ├→ requires_capture（授权成功，待捕获）
                              │       ├→ succeeded（捕获完成）
                              │       └→ canceled（授权过期/取消）
                              ├→ succeeded（自动捕获）
                              └→ canceled
```

| 状态 | 含义 |
|---|---|
| `requires_payment_method` | 还没有可用的支付方式 |
| `requires_confirmation` | 已创建，等待确认 |
| `requires_action` | 需要用户完成 3DS/SCA 等动作 |
| `processing` | 渠道处理中 |
| `requires_capture` | 已授权但未捕获，资金被冻结 |
| `succeeded` | 已捕获/扣款成功 |
| `canceled` | 已取消或授权过期 |

如果 `captureMethod=automatic`，通常不会出现 `requires_capture`；如果 `captureMethod=manual`，必须支持它，否则无法表达“授权成功但尚未扣款”。

### Charge

一次具体的扣款尝试，可能失败后重试。一个 PaymentIntent 可能有多个 Charge。

### 为什么分离 Intent 和 Charge

- Intent 表达“要收多少钱”；
- Charge 表达“实际扣款尝试”；
- 重试不改变 Intent；
- 多渠道/多方式；
- 审计清晰。

## 40.3 幂等

支付 API 必须支持幂等键：

```http
POST /payment_intents
Idempotency-Key: 7d3f9c2a-...
```

规则：

- 相同键 + 相同请求 → 返回原结果；
- 相同键 + 不同请求 → 422；
- 并发相同键 → 只执行一次；
- 保存响应至少 24 小时；
- 网络超时后，调用方用同一键重试或查询状态。

### 幂等的边界

- 支付创建；
- 支付确认；
- 退款；
- 取消；
- 转账；
- 结算。

所有涉及资金的操作都必须幂等。

### 超时后的处理

```text
1. 用相同幂等键重试
2. 或查询 PaymentIntent 状态
3. 或查询交易流水
4. 不要创建新的支付
```

## 40.4 支付流程

```text
1. 创建 PaymentIntent
2. 收集支付方式（客户端 token 化）
3. 确认支付
4. 可能需要 3DS/SCA
5. 渠道处理
6. Webhook 通知结果
7. 更新订单
8. 对账
```

### 创建

```http
POST /payment_intents
```

```json
{
  "amount": 12900,
  "currency": "CNY",
  "orderId": "ord_123",
  "captureMethod": "automatic",
  "metadata": { "tenantId": "t_123" }
}
```

### 确认

```http
POST /payment_intents/{id}/confirm
```

```json
{
  "paymentMethodId": "pm_456",
  "returnUrl": "https://example.com/checkout/complete"
}
```

### 3DS/SCA

响应可能需要跳转：

```json
{
  "status": "requires_action",
  "nextAction": {
    "type": "redirect",
    "url": "https://bank.example.com/3ds?..."
  }
}
```

完成后通过 Webhook 或查询确认。

#### SCA 与 3DS2 的工程细节

PSD2 等监管要求对许多电子支付执行强客户认证（SCA）。3-D Secure 2（3DS2）是常见的实现方式，但它不是“跳转一下就完事”：

- **Frictionless**：发卡行基于风险数据直接通过，用户无感；
- **Challenge**：需要用户输入 OTP、生物识别或银行 App 确认；
- **Exemptions**：低金额、可信受益人、企业支付、商户发起的交易（MIT）等可能豁免，但豁免失败时要能回退到 challenge；
- **Authentication value**：认证完成后，渠道会返回 `authenticationValue`、`eci`、`dsTransID` 等字段，授权请求要带上它们；
- **Return URL**：用户可能在银行页面停留、切换 App、甚至中途关闭，返回时订单状态可能仍是 `processing`；
- **幂等确认**：用户重复点击“确认支付”不能产生两笔扣款；
- **超时**：认证窗口过期后，`PaymentIntent` 应回到可重试状态，而不是永久卡住。

API 应该把“下一步动作”建模为数据，而不是让客户端猜：

```json
{
  "status": "requires_action",
  "nextAction": {
    "type": "three_ds_challenge",
    "challengeUrl": "https://acs.bank.example.com/challenge?...",
    "threeDS": {
      "dsTransId": "ds_123",
      "acsTransId": "acs_456",
      "messageVersion": "2.2.0"
    },
    "expiresAt": "2026-11-26T10:15:00Z"
  }
}
```

认证结果不要只放在前端回调参数里。服务端必须以渠道的认证查询或 Webhook 为准，否则用户可以伪造“认证成功”的返回页。

### 授权与捕获

- 授权（authorize）：冻结资金；
- 捕获（capture）：实际扣款；
- 自动捕获 vs 手动捕获；
- 部分捕获；
- 授权过期。

### 支付失败

- 可重试失败（如网络）；
- 不可重试失败（如卡被拒）；
- 需要用户行动（3DS）；
- 需要人工介入。

## 40.5 PCI DSS 与令牌化

### 不要存储卡号

PCI DSS 要求严格保护持卡人数据。最安全的做法：

- 客户端直接与支付渠道 token 化；
- 服务端只存 token；
- 不经过自己的服务器传输卡号；
- 使用渠道的 hosted fields / SDK；
- 最小化 PCI 范围。

### 令牌化

```text
卡号 → 支付渠道 → token (pm_456)
```

服务端只保存 token、品牌、后四位、过期时间。

### PCI 范围

- SAQ A：完全外包；
- SAQ A-EP：部分参与；
- SAQ D：最高要求。

减少范围可以显著降低成本。

### PCI DSS 4.0.1 对 API 的影响

PCI DSS 4.0.1 是当前有效版本；4.0 中 51 项“未来日期”要求在 **2025 年 3 月 31 日**起已全部强制，不再有宽限期。它把许多要求从“年度检查”变成“持续控制”。对 API 团队最相关的部分：

- **范围细分**：API、支付页、后台管理、日志系统要划分 CDE（持卡人数据环境）与相邻系统；能 token 化的地方不要落 PAN；
- **支付页脚本**：如果支付页加载第三方脚本，要有完整性监控与授权机制（6.4.3），并定期检查页面被篡改（11.6.1）；
- **多因素认证**：所有访问 CDE 的管理员与远程用户都要 MFA（8.4.2）；
- **密钥管理**：令牌化密钥、签名密钥、渠道密钥要分离，轮换与访问审计要可证明；
- **日志**：不得记录完整 PAN、CVV、磁道数据；日志保留、完整性保护与每日审查要落地；
- **漏洞管理**：API 依赖、容器镜像、第三方 SDK 都要进入扫描与修补流程；
- **分段**：用网络策略、身份认证与最小权限证明“即使 API 被攻破，也到不了持卡人数据”。

一个常见的误解是“我们把支付页交给渠道，所以完全不用管 PCI”。只要你的 JavaScript 参与了支付流程、你的服务器接收了支付结果、你的日志可能落卡数据，你就在某个 SAQ 范围内。先画数据流，再决定哪些字段根本不该进入你的系统。

### PSD3/PSR 与 DORA

欧盟支付监管在 2026 年仍处于过渡期：

- **PSD2** 仍是现行开放银行与 SCA 的基础；
- **PSD3 / PSR** 已在 2026 年进入立法收尾阶段（理事会已通过一读立场，最终通过时间与适用时间以 EU 官方文本为准）；PSR 作为直接适用法规，会进一步统一 SCA、开放银行、欺诈责任与 IBAN/name 校验；
- **DORA** 自 2025 年 1 月 17 日适用，支付机构作为金融实体需要满足 ICT 风险、事件报告、第三方登记与韧性测试要求。

对 API 的含义：同意管理、SCA 日志、TPP 权限、事件通知与第三方退出策略，都会进入支付 API 的契约与运行手册。更完整的合规映射见[附录 T](../appendices/t-compliance-engineering.md)。

## 40.6 退款

```http
POST /refunds
Idempotency-Key: ...
```

```json
{
  "paymentIntentId": "pi_123",
  "amount": 5000,
  "currency": "CNY",
  "reason": "customer_request"
}
```

状态：

```text
pending → succeeded
        └→ failed
```

要点：

- 部分退款；
- 多次退款总额不超过原支付；
- 退款幂等；
- 退款失败重试；
- 退款与订单/账务一致；
- 退款 Webhook；
- 退款对账；
- 退款时限；
- 手续费处理。

### 退款金额规则

```text
累计退款 ≤ 原支付金额
部分退款按比例退手续费（取决于合同）
币种一致
汇率按原交易
```

## 40.7 争议与拒付

```text
Dispute
├── needs_response
├── under_review
├── won
└── lost
```

API：

```text
GET  /disputes/{id}
POST /disputes/{id}/evidence
```

需要：

- 证据上传；
- 截止时间；
- 资金冻结；
- 结果通知；
- 账务处理；
- 申诉流程；
- 拒付率监控。

### 拒付的影响

- 资金损失；
- 手续费；
- 拒付率过高会被渠道处罚；
- 需要风控改进。

## 40.8 账本与复式记账

支付系统必须有可靠的账本：

```text
借：渠道应收    100
贷：商户收入    100
```

每笔交易至少两个分录，金额平衡。

### 为什么需要账本

- 对账；
- 审计；
- 退款；
- 结算；
- 多币种；
- 分账；
- 合规；
- 财务报告。

### 账本设计

- 不可变分录；
- 幂等；
- 双录；
- 币种；
- 账户；
- 交易 ID；
- 时间；
- 冲正而非修改；
- 余额快照；
- 审计。

### 复式记账示例

用户支付 129 元：

```text
借：渠道应收（资产） 129
贷：商户收入（收入） 129
```

退款 50 元：

```text
借：退款（费用） 50
贷：渠道应收（资产） 50
```

结算：

```text
借：银行存款（资产） 129
贷：渠道应收（资产） 129
```

### 账本的不变量

- 每笔交易借贷平衡；
- 余额 = 所有分录之和；
- 分录不可修改，只能冲正；
- 每笔分录有唯一 ID；
- 可追溯到业务事件。

## 40.9 对账

```text
内部交易 ↔ 渠道流水 ↔ 银行流水 ↔ 账本
```

对账维度：

- 时间；
- 金额；
- 币种；
- 手续费；
- 退款；
- 拒付；
- 结算；
- 汇率。

差异处理：

- 自动匹配；
- 人工复核；
- 补单；
- 冲正；
- 告警；
- 根因分析。

### 对账的时效

- T+0：实时/准实时；
- T+1：日终；
- T+N：渠道延迟。

对账越晚，修复成本越高。

### ISO 20022 与清算报文

跨境与银行间支付越来越多地使用 ISO 20022。它不是“另一种 JSON”，而是一套 XML 报文语义：

| 报文 | 用途 |
|---|---|
| `pain.001` | 客户发起付款 |
| `pain.002` | 付款状态报告 |
| `pacs.008` | 银行间客户信用转账 |
| `pacs.002` | 支付状态报告 |
| `camt.053` | 银行对账单 |
| `camt.054` | 借贷通知 |

API 与报文之间需要稳定映射：

- 你的 `paymentId` 对应 `EndToEndId` 或 `InstrId`（Instruction Identification），不能每次重试都生成新 ID；跨行追踪还可能使用 `UETR`，不要把这三个 ID 混为一谈；
- `UETR` 是跨境追踪的关键，要在 API、报文与对账系统中保持一致；
- 汇款附言（remittance information）有长度与字符限制，不能把整个 JSON 塞进去；
- 金额、币种、费用承担方（charge bearer）要显式建模；
- 状态模型不同：API 的 `processing` 可能对应报文中的多个状态，不能简单一一映射；
- 报文有版本（如 `pacs.008.001.08`），升级要走契约变更流程。

如果 API 只把 ISO 20022 当作“导出格式”，对账时就会发现自己无法回答“这笔钱到底走到哪一步”。

## 40.10 Webhook

支付结果通常通过 Webhook 通知：

- 签名；
- 重试；
- 去重；
- 顺序；
- 状态查询兜底。

不要只依赖 Webhook，也不要只依赖同步响应。两者结合。

### 支付 Webhook 的特殊要求

- 必须验签；
- 必须幂等；
- 必须可重放；
- 必须记录；
- 必须能对账；
- 必须防伪造。

## 40.11 合规

- PCI DSS；
- KYC/AML；
- 反洗钱；
- 数据保留；
- 隐私；
- 跨境；
- 支付牌照；
- 消费者保护；
- 强客户认证（SCA）；
- 制裁名单。

合规要求会影响 API 设计：

- 字段；
- 日志；
- 数据驻留；
- 审计；
- 删除；
- 留存。

### 区域支付体系与 API 差异

同样叫“转账”，不同市场的 API 语义差别很大：

| 体系 | 市场 | 关键特征 | API 影响 |
|---|---|---|---|
| SEPA / SEPA Instant | 欧元区 | IBAN、ISO 20022、即时到账 | 以 IBAN 为主标识，状态回调快 |
| FedNow / RTP | 美国 | 实时清算、请求付款（RfP） | 支持 request-for-payment 与异步确认 |
| Pix | 巴西 | Pix key、BR Code、24×7 | 以 key 代替账号，二维码与回调常见 |
| UPI | 印度 | VPA、collect/pay、移动优先 | 收款请求与用户批准是核心流程 |
| 开放银行（OBIE/Berlin Group/FDX） | 英国/欧盟/美国 | 账户信息与支付发起、SCA | 同意（consent）生命周期、重认证、数据最小化 |

这些差异不是“适配器里加几个字段”能解决的。它们会改变：

- 标识符：账号、IBAN、VPA、Pix key、钱包 ID；
- 授权模型：一次授权、周期性授权、收款请求、用户在场/不在场；
- 状态机：实时到账、批量清算、T+N 结算；
- 失败语义：账户不存在、限额、制裁、欺诈、用户拒绝；
- 合规：SCA、牌照、数据驻留、消费者撤销权。

做多市场支付 API 时，应先抽象出“支付意图、授权、执行、结算、对账”五层，再让区域适配器实现差异。不要试图用一个 `bankAccount` 字段覆盖所有市场。

## 40.12 可靠性与故障

- 渠道超时；
- 渠道重复回调；
- 渠道不可用；
- 多渠道路由；
- 熔断；
- 降级；
- 人工介入；
- 对账补偿；
- 资金冻结；
- 限额。

### 渠道故障

```text
主渠道不可用 → 切备渠道
备渠道也不可用 → 排队/人工
```

切换需要：

- 幂等；
- 状态查询；
- 对账；
- 客户通知。

## 40.13 本章小结

- 支付 API 的第一原则是资金正确。
- 幂等、状态机、账本、对账是核心。
- 不要存储卡号，使用令牌化。
- 支付意图、扣款、退款、争议是不同聚合。
- Webhook 与查询结合。
- SCA/3DS2 要建模为可恢复的下一步动作，认证结果以服务端查询为准。
- PCI DSS 4.0.1 的范围、脚本、MFA、日志与分段要求会直接约束 API 与部署。
- PSD3/PSR、DORA 等监管变化会继续影响 SCA、开放银行、事件报告与第三方退出。
- ISO 20022 与区域支付体系要求稳定的端到端 ID、状态映射与对账。
- 合规与安全贯穿设计；义务到 API 能力的映射见[附录 T](../appendices/t-compliance-engineering.md)。
- 开放银行的同意、SCA、FAPI、支付状态机与对账，见[附录 AI](../appendices/ai-vertical-apis.md)；金额、汇率、舍入与账期的语义，见[附录 AH](../appendices/ah-primitive-semantics.md)。
- 故障必须有补偿和对账。

## 设计练习

为“跨境电商支付平台”设计 API：

- 多币种；
- 卡、钱包、银行转账；
- 3DS；
- 部分退款；
- 拒付；
- 分账；
- 结算；
- 对账。

写出支付状态机、幂等流程、账本分录和 Webhook 设计。然后回答：

1. 支付超时但渠道成功，如何处理？
2. 部分退款如何记账？
3. 拒付后如何影响账本？
4. 多币种结算如何处理汇率？

---

上一章：[电商与交易 API](39-ecommerce.md)  
下一章：[SaaS 平台 API](41-saas.md)
