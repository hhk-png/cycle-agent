# 附录 AI：行业垂直 API 深潜——医疗、开放银行、电信与工业

> 主教程的领域章节覆盖电商、支付、SaaS、IoT、数据平台与内部平台。本附录选择四个标准密集、监管严格、失败代价高的行业——医疗健康、开放银行、电信、工业与汽车——做一次深潜。目的不是复述标准，而是回答一个更实用的问题：**当行业标准、监管义务、遗留系统和真实业务流程同时压在 API 上时，哪些设计决策仍然通用，哪些必须向领域妥协？**

## AI.1 为什么垂直 API 需要单独设计

通用 API 设计原则在垂直行业仍然成立：资源、契约、错误、分页、幂等、版本、可观测性。但垂直行业多了四层约束：

1. **标准即契约。** 你不能随意命名字段，不能随意定义状态，甚至不能随意选择 HTTP 方法；互操作是产品能否卖出去的前提。
2. **监管即需求。** 同意、审计、保留期、数据驻留、事件报告、可解释性不是“以后再加”，而是上线门槛。
3. **术语即领域。** 医疗的 `Observation`、电信的 `ProductOrder`、工业的 `NodeId`、金融的 `Consent` 都有精确含义，不能按直觉建模。
4. **流程即长事务。** 开户、理赔、装机、诊断、OTA 往往跨越数天、多个系统、多个人工环节，不能用一个同步请求表达。

### 四个垂直的共同结构

| 维度 | 医疗 | 开放银行 | 电信 | 工业/汽车 |
|---|---|---|---|---|
| 核心标准 | FHIR、HL7、DICOM | OBIE、Berlin Group、FDX、FAPI | TM Forum Open API、CAMARA | OPC UA、Sparkplug B、VSS、SOME/IP |
| 核心资源 | Patient、Observation、Consent | Consent、Account、Payment | ProductOrder、Service、Resource | Node、Device、Signal、Command |
| 身份 | 患者、医生、机构 | 客户、TPP、银行 | 客户、合作伙伴、网络 | 设备、网关、车队 |
| 授权核心 | SMART scope、同意、审计 | 同意 + SCA + FAPI | 合作方合同 + 客户同意 | 设备证书 + 角色 + 安全联锁 |
| 长流程 | 就诊、检验、理赔 | 授权、支付、结算 | 下单、开通、计费 | 命令、OTA、诊断 |
| 证据 | 审计、来源、版本 | 对账、争议、报告 | CDR、账单、SLA | 日志、签名、功能安全 |

### 一个判断原则

> **标准负责“能互通”，产品负责“值得用”。**

只做标准符合性，API 可能正确但难用；只做产品体验，API 可能好用但无法接入生态。垂直 API 的设计目标，是在标准契约之上增加一层**领域适配、错误可操作性、开发者体验和运营能力**，而不是重写标准。

## AI.2 医疗健康：FHIR

FHIR（Fast Healthcare Interoperability Resources）是目前医疗互操作最重要的资源模型。它既是 REST API，也是消息、文档和服务模型。FHIR R4（4.0.1）仍是部署最广的基线，R5 提供更多能力，后续版本仍在推进。

### AI.2.1 资源模型

FHIR 的核心是资源（Resource）：

```text
Patient          患者
Practitioner     医务人员
Organization     机构
Encounter        就诊
Observation      检验/观察
DiagnosticReport 诊断报告
MedicationRequest 处方
MedicationAdministration 给药
Condition        诊断/问题
Procedure        操作
Consent          同意
AuditEvent       审计事件
Provenance       来源
Subscription     订阅
Bundle           批量/事务/搜索集
OperationOutcome 错误
```

每个资源都有：

- `resourceType`；
- `id`（逻辑 ID）；
- `meta.versionId`、`meta.lastUpdated`；
- 业务字段；
- 可扩展的 `extension`；
- 可选的 `text`（人读叙述）。

```json
{
  "resourceType": "Patient",
  "id": "pat_123",
  "meta": {
    "versionId": "7",
    "lastUpdated": "2026-11-26T02:15:30.123Z",
    "profile": ["http://example.org/fhir/StructureDefinition/MyPatient"]
  },
  "identifier": [
    { "system": "http://example.org/mrn", "value": "MRN-001234" }
  ],
  "name": [{ "family": "张", "given": ["三"] }],
  "gender": "male",
  "birthDate": "1990-05-17"
}
```

### AI.2.2 身份、版本与并发

FHIR 的 URL 结构：

```text
[base]/[type]/[id]
[base]/[type]/[id]/_history/[vid]
```

- `id` 是逻辑 ID，跨版本稳定；
- `versionId` 是版本号；
- `meta.lastUpdated` 是最后更新时间；
- `ETag` 通常是 `W/"7"`；
- 更新时用 `If-Match` 做乐观并发；
- 条件创建用 `If-None-Exist`；
- 条件更新/删除用搜索参数定位。

```http
PUT [base]/Patient/pat_123
If-Match: W/"7"
Content-Type: application/fhir+json
```

如果版本不匹配，返回 `412 Precondition Failed`。如果资源不存在，返回 `404`；如果创建时已存在，返回 `409`。

FHIR 还有“条件创建”：

```http
POST [base]/Patient
If-None-Exist: identifier=http://example.org/mrn|MRN-001234
```

如果已存在匹配资源，服务端返回 `200` 和已有资源；否则创建并返回 `201`。这解决了“先查再建”的竞态，但前提是搜索参数和唯一性约束可靠。

### AI.2.3 搜索

FHIR 搜索是垂直 API 中最复杂的查询契约之一：

```http
GET [base]/Observation?patient=pat_123&code=http://loinc.org|8867-4&date=ge2026-01-01&_count=50&_sort=-date
```

常见参数：

| 参数 | 作用 |
|---|---|
| `_id` | 按逻辑 ID |
| `_lastUpdated` | 按最后更新时间 |
| `_tag`、`_profile` | 按标签/配置 |
| `_text` | 全文搜索 |
| `_content` | 内容搜索 |
| `_count` | 每页数量 |
| `_offset` | 偏移（R4 有限支持） |
| `_cursor` | 游标（R5 方向） |
| `_sort` | 排序，`-` 表示降序 |
| `_summary` | 摘要模式 |
| `_elements` | 字段裁剪 |
| `_total` | 总数策略 |
| `_include` | 包含引用资源 |
| `_revinclude` | 反向包含 |
| `_has` | 反向链式搜索 |
| `_filter` | 过滤表达式 |

搜索前缀：

| 前缀 | 含义 |
|---|---|
| `eq` | 等于 |
| `ne` | 不等于 |
| `gt` / `lt` | 大于/小于 |
| `ge` / `le` | 大于等于/小于等于 |
| `sa` | 起点之后 |
| `eb` | 终点之前 |
| `ap` | 近似 |

修饰符：

```text
:exact      精确匹配
:contains   包含
:missing    缺失/存在
:not        取反
:identifier 按标识
:of-type    按类型
:in         在集合中
:not-in     不在集合中
```

链式搜索：

```http
GET [base]/Observation?subject.name=张&_include=Observation:patient
GET [base]/Patient?_has:Observation:patient:code=8867-4
```

设计要点：

- 搜索参数必须由服务端声明在 `CapabilityStatement` 中；
- 未知参数应忽略还是报错，要按 FHIR 规范与产品策略决定；
- 复杂链式搜索可能拖垮数据库，必须有索引、深度限制和超时；
- `_include` 的结果必须去重，且不能绕过权限；
- `_total` 可能很贵，应支持 `none` / `estimate` / `accurate` 策略；
- 分页游标应绑定查询条件，不能跨查询复用。

### AI.2.4 Bundle 与事务

FHIR 用 `Bundle` 表达批量、事务、历史、搜索集和文档：

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:0f7c...",
      "resource": {
        "resourceType": "Patient",
        "name": [{ "family": "张", "given": ["三"] }]
      },
      "request": { "method": "POST", "url": "Patient" }
    },
    {
      "fullUrl": "urn:uuid:8a1b...",
      "resource": {
        "resourceType": "Observation",
        "status": "final",
        "code": { "coding": [{ "system": "http://loinc.org", "code": "8867-4" }] },
        "subject": { "reference": "urn:uuid:0f7c..." },
        "valueQuantity": { "value": 72, "unit": "beats/min", "system": "http://unitsofmeasure.org", "code": "/min" }
      },
      "request": { "method": "POST", "url": "Observation" }
    }
  ]
}
```

`Bundle.type`：

| 类型 | 语义 |
|---|---|
| `transaction` | 原子事务，全部成功或全部失败 |
| `batch` | 逐条执行，逐条返回结果 |
| `history` | 资源版本历史 |
| `searchset` | 搜索结果 |
| `collection` | 资源集合 |
| `document` | 文档 |
| `message` | 消息 |

事务语义是 FHIR 的强项，也是实现难点：

- 所有条目在同一事务中提交；
- 条目之间可以用 `urn:uuid:` 临时引用；
- 服务端返回 `Bundle.entry.response`，包含状态、位置、ETag；
- 失败时整个事务回滚，返回 `OperationOutcome`；
- 批处理 `batch` 则允许部分成功，每条结果独立。

### AI.2.5 Profile、扩展与术语

FHIR 的互操作不只靠资源类型，还靠：

- **Profile**：约束资源结构（必填字段、基数、取值）；
- **Extension**：扩展标准未覆盖的字段；
- **StructureDefinition**：Profile/Extension 的机器可读定义；
- **ImplementationGuide**：一组 Profile、搜索参数、示例和文档；
- **CapabilityStatement**：服务端声明支持哪些资源、交互、搜索参数和版本。

```json
{
  "resourceType": "CapabilityStatement",
  "fhirVersion": "4.0.1",
  "status": "active",
  "kind": "instance",
  "rest": [{
    "mode": "server",
    "resource": [{
      "type": "Patient",
      "interaction": [{ "code": "read" }, { "code": "search-type" }],
      "searchParam": [{ "name": "identifier", "type": "token" }]
    }]
  }]
}
```

术语系统是医疗 API 的另一个深水区：

- `CodeSystem`：编码系统；
- `ValueSet`：取值集合；
- `ConceptMap`：编码映射；
- 常见术语：LOINC、SNOMED CT、ICD-10/11、RxNorm、UCUM；
- 术语有版本、许可和地区差异；
- 服务端应支持 `$validate-code`、`$expand`、`$lookup`、`$translate`。

```http
GET [base]/ValueSet/$validate-code?url=http://example.org/vs/diagnosis&code=E11.9
```

不要假设不同机构的编码可以直接比较；映射需要 `ConceptMap` 和人工审核。

### AI.2.6 SMART on FHIR 与同意

SMART on FHIR 在 OAuth 2.0 之上定义医疗应用的授权：

```text
openid
fhirUser
launch
launch/patient
patient/*.read
user/*.read
offline_access
```

关键点：

- `patient/*.read` 表示“当前患者上下文内的读权限”；
- `user/*.read` 表示“当前医务人员可访问的读权限”；
- `launch/patient` 让应用在启动时获得患者上下文；
- `fhirUser` 映射到 `Practitioner` 或 `Patient`；
- `aud` 必须是 FHIR 服务的基础 URL；
- 令牌应短期、可刷新、可撤销；
- 作用域不等于数据权限，服务端仍要逐资源检查。

同意（Consent）是医疗 API 的核心资源：

```json
{
  "resourceType": "Consent",
  "status": "active",
  "scope": { "coding": [{ "code": "patient-privacy" }] },
  "category": [{ "coding": [{ "code": "treatment" }] }],
  "patient": { "reference": "Patient/pat_123" },
  "dateTime": "2026-11-26T02:00:00Z",
  "provision": {
    "type": "permit",
    "period": { "start": "2026-11-26", "end": "2027-11-26" },
    "purpose": [{ "coding": [{ "code": "TREAT" }] }],
    "actor": [{ "reference": "Practitioner/prac_456" }]
  }
}
```

设计要点：

- 同意是资源，不是布尔字段；
- 同意有范围、目的、时间、主体、行为者；
- 撤回同意要有传播机制；
- 紧急访问（break-glass）要有额外审计和事后复核；
- 审计用 `AuditEvent`，来源用 `Provenance`；
- 数据最小化：只返回当前目的需要的字段。

### AI.2.7 订阅与批量导出

FHIR 订阅用于“资源变化通知”：

- R5 使用 `SubscriptionTopic` 定义可订阅主题；
- `Subscription` 定义通道（rest-hook、websocket 等）；
- 通知通常是 `Bundle`（`type=history` 或空 Bundle + `$events`）；
- 投递语义通常是至少一次，消费者必须幂等；
- 通知不应携带过多临床数据，必要时让消费者回查。

批量导出：

```http
GET [base]/$export?_type=Patient,Observation&_since=2026-01-01T00:00:00Z
Accept: application/fhir+json
Prefer: respond-async
```

响应：

```http
202 Accepted
Content-Location: [base]/$export/exp_123
```

轮询状态，完成后返回：

```json
{
  "transactionTime": "2026-11-26T02:15:30Z",
  "request": "[base]/$export?_type=Patient,Observation",
  "requiresAccessToken": true,
  "output": [
    { "type": "Patient", "url": "https://.../Patient.ndjson" },
    { "type": "Observation", "url": "https://.../Observation.ndjson" }
  ],
  "error": []
}
```

设计要点：

- 导出结果用 NDJSON，一行一个资源；
- 文件 URL 应短期有效、可审计；
- `_since` 是增量导出的关键，但服务端必须定义“变更”的语义；
- 导出要遵守同意、权限和最小化；
- 大批量导出必须异步、可分片、可重试；
- 导出完成后要有清理和保留期。

### AI.2.8 错误与审计

FHIR 错误使用 `OperationOutcome`：

```json
{
  "resourceType": "OperationOutcome",
  "issue": [{
    "severity": "error",
    "code": "invalid",
    "diagnostics": "Observation.subject 必须引用一个 Patient 或 Group。",
    "expression": ["Observation.subject"]
  }]
}
```

HTTP 状态码映射：

| 情况 | 状态码 |
|---|---|
| 资源不存在 | 404 |
| 版本冲突 | 409 / 412 |
| 校验失败 | 422 |
| 未认证 | 401 |
| 无权限 | 403 |
| 不支持的方法 | 405 |
| 媒体类型不支持 | 415 |

审计要记录：

- 谁（主体）；
- 何时；
- 对哪个患者/资源；
- 做了什么（读/写/导出/打印）；
- 为什么（目的、同意、紧急访问）；
- 结果（成功/失败）；
- 来源系统与网络位置。

### AI.2.9 医疗 API 的设计教训

- 标准资源只是起点，Profile 和术语才决定互操作质量；
- 版本与并发必须用 `ETag`/`If-Match`，不能用“最后写入获胜”；
- 搜索是最容易拖垮系统的部分，必须有索引、深度限制和超时；
- 同意、审计、来源是一等资源，不是附属日志；
- 批量导出和订阅是互操作的刚需，不是高级功能；
- 医疗数据的最小化、保留、删除和跨境需要专门设计；
- 不要把 FHIR 的复杂性原样泄漏给内部领域模型；用防腐层隔离。

## AI.3 开放银行与金融数据

开放银行要求银行在客户同意下，向受监管的第三方提供商（TPP）开放账户信息和支付发起能力。它不是单一标准，而是一组区域标准的集合。

### AI.3.1 标准版图

| 区域 | 主要标准 | 特点 |
|---|---|---|
| 英国 | OBIE Read/Write API | 成熟、监管强制、生态完整 |
| 欧盟 | Berlin Group NextGenPSD2 | 覆盖广、字段细、各国实现有差异 |
| 美国 | FDX | 市场驱动、覆盖面广、版本多 |
| 澳大利亚 | Consumer Data Right（CDR） | 强监管、跨行业 |
| 巴西 | Open Finance Brasil | 监管驱动、快速扩展 |
| 印度 | Account Aggregator / UPI | 账户聚合与实时支付分离 |

这些标准的共同点：

- 同意（consent）是核心资源；
- 强客户认证（SCA）；
- 访问令牌短期、绑定受众；
- 支付发起与账户信息分离；
- 状态查询与回调结合；
- 错误码和状态机标准化；
- 对账与争议处理是产品的一部分。

### AI.3.2 同意是核心资源

开放银行的同意不是“用户点了同意”一个布尔值，而是一个有生命周期的资源：

```json
{
  "consentId": "cons_123",
  "status": "AUTHORISED",
  "permissions": [
    "ReadAccountsDetail",
    "ReadBalances",
    "ReadTransactionsDetail"
  ],
  "accountIds": ["acc_1", "acc_2"],
  "expirationDateTime": "2026-12-31T23:59:59Z",
  "transactionFromDateTime": "2026-01-01T00:00:00Z",
  "frequencyPerDay": 4,
  "recurringIndicator": true
}
```

生命周期：

```text
AWAITING_AUTHORISATION
        │
        ├→ AUTHORISED → REVOKED
        │        │
        │        └→ EXPIRED
        └→ REJECTED
```

设计要点：

- 同意有权限集合、账户范围、时间范围、频率；
- 同意可以撤回，撤回必须传播到所有数据访问路径；
- 同意过期后，令牌和缓存都不能继续使用；
- 同意状态要可查询、可审计；
- 数据最小化写进同意，而不是只写在隐私政策里；
- 同意不能用一个宽泛 scope 覆盖所有用途。

### AI.3.3 强客户认证与 FAPI

PSD2 的 SCA 要求至少两类要素：知识（密码）、持有（设备）、固有（生物特征）。常见流程：

- 重定向到银行 App/网页；
- 3DS2；
- 解耦认证（decoupled）；
- CIBA（客户端发起、另一设备确认）。

FAPI 2.0 提供金融级安全配置：

- PAR（Pushed Authorization Requests）；
- JARM（JWT Secured Authorization Response Mode）；
- mTLS 或 DPoP 绑定；
- 严格的令牌受众与短期有效；
- 请求签名与消息完整性；
- 证书生命周期管理（QWAC/QSEAL 等）。

```http
POST /as/par
Content-Type: application/x-www-form-urlencoded

response_type=code
&client_id=tpp_123
&redirect_uri=https://tpp.example/cb
&scope=openid accounts payments
&code_challenge=...
&code_challenge_method=S256
&authorization_details=[{"type":"payment_initiation",...}]
```

设计要点：

- 不要把 SCA 当作一次性弹窗，它是可恢复的下一步动作；
- 认证结果以服务端查询为准，不以重定向参数为准；
- 令牌绑定发送方，降低泄露风险；
- 证书轮换要有重叠窗口和吊销路径；
- 错误要区分“需要重新认证”“同意过期”“限额”“账户不可用”。

### AI.3.4 账户信息

典型资源：

```text
Account
Balance
Transaction
StandingOrder
Beneficiary
Card
```

账户信息 API 的关键语义：

- `AccountId` 的稳定性和映射；
- `bookingDate`（入账日）与 `valueDate`（起息日）不同；
- `transactionId` 与 `entryReference` 的区别；
- `creditDebitIndicator`；
- `status`（booked/pending/info）；
- 金额、币种、汇率；
- 交易对手信息可能被脱敏；
- 分页和增量同步；
- 数据新鲜度与截止时间。

```json
{
  "transactionId": "tx_123",
  "bookingDate": "2026-11-26",
  "valueDate": "2026-11-26",
  "creditDebitIndicator": "DBIT",
  "status": "booked",
  "amount": { "currency": "EUR", "amount": "100.00" },
  "creditorName": "Example GmbH",
  "remittanceInformation": "Invoice 2026-001"
}
```

### AI.3.5 支付发起

支付发起与账户信息是不同权限，不能用一个 scope 覆盖。典型流程：

1. 创建支付请求；
2. 返回 `paymentId` 和 SCA 链接；
3. 客户完成 SCA；
4. 银行处理；
5. 状态通过查询或回调更新；
6. 对账与争议。

```http
POST /v1/payments/sepa-credit-transfers
X-Request-ID: req_123
Content-Type: application/json
```

```json
{
  "endToEndIdentification": "e2e_123",
  "debtorAccount": { "iban": "DE89370400440532013000" },
  "instructedAmount": { "currency": "EUR", "amount": "100.00" },
  "creditorAccount": { "iban": "DE89370400440532013001" },
  "creditorName": "Example GmbH",
  "remittanceInformationUnstructured": "Invoice 2026-001"
}
```

响应：

```json
{
  "paymentId": "pay_123",
  "transactionStatus": "RCVD",
  "_links": {
    "scaRedirect": "https://bank.example/sca/...",
    "self": "/v1/payments/sepa-credit-transfers/pay_123"
  }
}
```

状态：

| 状态 | 含义 |
|---|---|
| `RCVD` | 已接收 |
| `PDNG` | 处理中 |
| `ACTC` | 已通过技术校验，待结算 |
| `ACSC` | 已结算 |
| `RJCT` | 已拒绝 |
| `CANC` | 已取消 |

不同标准/版本还有 `ACCP`、`ACFC`、`ACCC` 等细分状态；状态含义与迁移规则以对应标准文本为准。

设计要点：

- `paymentId` 与 `endToEndId` 是不同层次的标识；
- 幂等键防止重复发起；
- 超时后必须能查询，不能靠重试猜结果；
- 状态回调必须幂等，且能通过查询兜底；
- 拒绝原因要可操作；
- 退款、撤销、争议是独立流程。

### AI.3.6 状态、回调与对账

开放银行的回调通常只通知“状态变了”，不携带完整数据：

```json
{
  "paymentId": "pay_123",
  "status": "ACSC",
  "updatedAt": "2026-11-26T02:15:30Z"
}
```

消费者应：

- 校验签名/来源；
- 用 `paymentId` 回查权威状态；
- 幂等处理重复通知；
- 处理乱序和迟到；
- 定期全量对账，不能只依赖回调。

对账要覆盖：

- 银行流水；
- 内部账本；
- 支付状态；
- 手续费；
- 退款/拒付；
- 汇率；
- 时间边界。

### AI.3.7 错误模型

开放银行的错误模型通常包含：

- HTTP 状态码；
- 业务错误码；
- 消息列表；
- 关联 ID；
- 可操作的下一步。

Berlin Group 风格：

```json
{
  "tppMessages": [{
    "category": "ERROR",
    "code": "CONSENT_EXPIRED",
    "text": "The consent has expired."
  }]
}
```

OBIE 风格：

```json
{
  "Code": "400 Bad Request",
  "Message": "Consent expired",
  "Errors": [{
    "ErrorCode": "UK.OBIE.Rules.AuthorisationNotValid",
    "Message": "Authorisation is not valid",
    "Path": "/data/consent"
  }]
}
```

设计要点：

- 错误码稳定、可程序化处理；
- 消息可本地化，但调用方不解析文本；
- 区分认证、授权、业务规则、限流、系统故障；
- 返回关联 ID，便于跨机构排查；
- 不要泄露账户是否存在等敏感信息。

### AI.3.8 开放银行的设计教训

- 同意是资源，不是布尔值；撤回和过期必须传播；
- SCA 是可恢复的流程，不是一次性弹窗；
- 支付发起与账户信息权限分离；
- 幂等、查询兜底、对账是资金正确的基础；
- 标准碎片化是常态，适配层是必需品；
- 沙箱、模拟器、一致性测试是上线前提；
- 数据最小化、用途限制、审计是监管要求，也是产品信任。

## AI.4 电信：TM Forum 与网络 API

电信 API 的复杂度来自三个层面：业务支撑系统（BSS）、运营支撑系统（OSS）和网络。TM Forum 的 Open API 和 SID 信息模型是行业共同语言；CAMARA/GSMA Open Gateway 则把网络能力开放给开发者。

### AI.4.1 SID 与四层模型

TM Forum 的 SID（Information Framework）把电信业务分成几个核心域：

```text
Party / Customer
Product
Service
Resource
Usage / Bill
```

关键区分：

- **Product**：客户购买的东西（套餐、增值服务）；
- **ProductOffering**：可销售的产品目录项；
- **ProductOrder**：客户下单；
- **Service**：产品对应的服务实例；
- **Resource**：网络/IT 资源（SIM、端口、设备、链路）；
- **Usage**：用量；
- **Bill**：账单。

产品、服务、资源三层分离是电信 API 最重要的设计模式：客户买的是产品，产品由服务实现，服务由资源承载。把三层压成一张表，会导致目录、开通、计费、故障都无法解耦。

### AI.4.2 TM Forum Open API

常见 Open API：

| API | 作用 |
|---|---|
| TMF620 | Product Catalog Management |
| TMF622 | Product Ordering Management |
| TMF637 | Product Inventory Management |
| TMF640 | Service Activation and Configuration |
| TMF641 | Service Ordering Management |
| TMF639 | Resource Inventory Management |
| TMF635 | Usage Management |
| TMF678 | Customer Bill Management |
| TMF629 | Customer Management |
| TMF666 | Account Management |
| TMF681 | Communication Management |
| TMF688 | Event Management |
| TMF696 | Risk Management |
| TMF720 | Digital Identity Management |

设计要点：

- Open API 有标准资源、字段、状态和错误；
- 不同版本之间存在差异，必须声明版本；
- 扩展字段要有命名空间；
- 事件用 TMF688 或 CloudEvents 承载；
- 与内部模型之间要有防腐层；
- 一致性测试比“字段看起来一样”更重要。

### AI.4.3 订单编排与 fallout

电信订单通常不是“创建订单 → 返回成功”，而是跨多个系统、数小时到数天的编排：

```text
ProductOrder
   → ServiceOrder
      → ResourceOrder
         → 网络配置 / 开通 / 激活
```

订单状态：

```text
acknowledged → inProgress → completed
                     │
                     ├→ failed
                     └→ cancelled
```

订单项（OrderItem）可能有独立状态。常见问题：

- **Jeopardy**：预计完成时间即将错过，需要预警；
- **Fallout**：某一步失败，需要人工或自动修复；
- **补偿**：部分资源已开通，失败后要回滚；
- **重试**：幂等重试，不能重复开通；
- **对账**：订单、服务、资源、账单要一致。

设计要点：

- 订单 API 必须异步，返回 `202` + 订单资源；
- 状态查询、事件、回调三件套；
- 每个订单项有独立状态和错误；
- 支持取消、暂停、恢复、改单；
- 失败要可解释、可重试、可补偿；
- 订单 ID、服务 ID、资源 ID 要稳定且可追溯。

### AI.4.4 网络 API 与 CAMARA

CAMARA（GSMA Open Gateway）把网络能力抽象成开发者 API：

- Quality on Demand（按需 QoS）；
- Device Location；
- Number Verification；
- SIM Swap；
- Carrier Billing；
- Edge Cloud；
- Device Status / Roaming。

以 QoS 会话为例：

```http
POST /quality-on-demand/v1/sessions
Authorization: Bearer ...
Content-Type: application/json
```

```json
{
  "device": { "phoneNumber": "+8613800000000" },
  "qosProfile": "QOS_E",
  "duration": 3600,
  "applicationServer": {
    "ipv4Address": "203.0.113.10"
  }
}
```

响应：

```json
{
  "sessionId": "qos_123",
  "qosStatus": "REQUESTED",
  "expiresAt": "2026-11-26T03:15:30Z"
}
```

设计要点：

- 网络能力是异步、有状态、有时限的；
- 会话有创建、查询、续期、终止；
- 设备标识涉及隐私，必须获得同意；
- 号码验证、SIM Swap 等 API 是高风险能力，要有反欺诈和审计；
- QoS 不是“保证带宽”，要写清网络、区域、终端和应用的边界；
- 计费按会话/时长/流量，需与网络侧对账。

### AI.4.5 用量、计费与对账

电信计费链路：

```text
网络事件 → CDR/Usage →  mediation → rating → billing → invoice
```

API 设计要点：

- 用量记录有唯一 ID、时间、单位、来源；
- 去重、补录、迟到、更正；
- 计价规则版本化；
- 账单不可变，更正走贷项；
- 预付费/后付费/承诺消费；
- 多币种、税、折扣；
- 与网络侧、财务侧对账。

### AI.4.6 遗留 OSS/BSS

电信大量系统仍是：

- SOAP/XML；
- JMS/文件传输；
- 私有协议；
- 批处理；
- 长生命周期数据库。

现代化策略：

- 用适配层隔离；
- 用 Open API 做对外契约；
- 用 CDC/事件做数据同步；
- 用 Strangler 逐步替换；
- 不要一次性重写计费/开通。

### AI.4.7 电信 API 的设计教训

- 产品、服务、资源三层分离；
- 订单是长流程，必须异步、可查询、可补偿；
- 目录、库存、开通、计费、故障是不同 bounded context；
- 标准 API 需要适配层，不能直接暴露内部模型；
- 网络 API 的隐私、同意、反欺诈要求极高；
- 用量和账单必须可对账、可解释、可重放。

## AI.5 工业、汽车与设备

工业与汽车 API 的消费者通常不是人，而是 PLC、网关、车载 ECU、边缘节点和车队系统。设计重点从“请求—响应”转向“状态、命令、事件、离线、安全”。

### AI.5.1 OPC UA

OPC UA（IEC 62541）是工业互操作的核心标准：

- 信息模型：节点（Node）、引用（Reference）、类型（Type）；
- 服务：Read、Write、Browse、Call、Subscribe、History；
- 会话（Session）与安全策略；
- PubSub 模式；
- 报警与条件（Alarms & Conditions）；
- 历史访问（Historical Access）；
- 伴随规范（Companion Specifications）。

```text
ns=2;s=Machine1.Temperature
```

`NodeId` 由命名空间和标识组成。设计要点：

- 节点 ID 稳定，不随设备重启变化；
- 类型系统支持继承和组合；
- 方法调用（Call）有输入/输出参数和权限；
- 订阅有采样间隔、死区、队列大小；
- 会话有超时、重连、订阅恢复；
- 安全策略、证书、用户令牌要配置；
- 不要把 OPC UA 的复杂类型直接映射成内部领域模型。

### AI.5.2 MQTT Sparkplug B

Sparkplug B 在 MQTT 之上定义了工业设备的主题、状态和载荷规范：

```text
spBv1.0/{group_id}/NBIRTH/{edge_node_id}
spBv1.0/{group_id}/NDEATH/{edge_node_id}
spBv1.0/{group_id}/NDATA/{edge_node_id}/{device_id}
spBv1.0/{group_id}/DBIRTH/{edge_node_id}/{device_id}
spBv1.0/{group_id}/DDEATH/{edge_node_id}/{device_id}
spBv1.0/{group_id}/DDATA/{edge_node_id}/{device_id}
spBv1.0/{group_id}/STATE/{edge_node_id}
```

关键机制：

- `NBIRTH`/`DBIRTH` 声明节点/设备及其指标；
- `NDEATH`/`DDEATH` 表示离线；
- `seq` 是 0～255 的循环序列号；
- `bdSeq` 用于检测边缘节点重启；
- 指标（Metric）有 `name`、`alias`、`timestamp`、`datatype`、`value`；
- 别名（alias）减少载荷；
- 报告按异常（report by exception）；
- `STATE` 主题表达主应用在线状态；
- 保留消息和遗嘱消息用于状态恢复。

设计要点：

- 不要把 `NBIRTH` 当成普通数据；
- 消费者要处理重启、序列号回绕、别名变化；
- QoS 和保留策略要按主题区分；
- 时间戳可能来自设备，存在时钟偏差；
- 命令要有确认、超时、幂等和离线队列。

### AI.5.3 汽车：VSS、SOME/IP 与 UDS

汽车 API 的常见层次：

- **VSS（Vehicle Signal Specification）**：车辆信号树；
- **SOME/IP**：面向服务的车载通信；
- **CAN/LIN**：传统总线；
- **UDS（ISO 14229）**：诊断服务；
- **AUTOSAR**：软件架构标准；
- **OTA**：远程升级。

VSS 用路径表达信号：

```text
Vehicle.Speed
Vehicle.Powertrain.Transmission.CurrentGear
Vehicle.Cabin.Door.Row1.Left.IsOpen
Vehicle.CurrentLocation.Latitude
```

每个信号有数据类型、单位、最小/最大值、描述。设计要点：

- 信号路径稳定；
- 单位统一（km/h、V、°C）；
- 时间戳与采样率；
- 信号可能不可用/未知；
- 访问控制按信号/域；
- 不要把原始 CAN 帧直接暴露给应用。

SOME/IP 提供：

- 方法（Method）：请求—响应；
- 事件（Event）：发布—订阅；
- 字段（Field）：getter/setter/notifier；
- 服务发现（SD）。

UDS 诊断：

```text
0x10 诊断会话控制
0x22 按标识读数据
0x27 安全访问
0x31 例程控制
0x34/0x36/0x37 下载/传输/退出
```

设计要点：

- 诊断会话有状态和权限；
- 安全访问有种子/密钥和延迟；
- 刷写有签名、校验、回滚；
- 诊断 API 要审计、限速、防误操作。

### AI.5.4 数字孪生与命令

设备 API 的核心模式是“期望状态 + 报告状态”：

```json
{
  "deviceId": "dev_123",
  "desired": { "targetTemperature": 22 },
  "reported": { "targetTemperature": 21, "currentTemperature": 20.5 },
  "version": 42,
  "updatedAt": "2026-11-26T02:15:30Z"
}
```

命令模式：

```text
POST /devices/{id}/commands
{
  "commandId": "cmd_123",
  "name": "setTargetTemperature",
  "parameters": { "value": 22 },
  "expiresAt": "2026-11-26T02:20:30Z"
}
```

设计要点：

- 命令有唯一 ID、幂等、过期、确认；
- 设备离线时命令进入队列，还是直接失败？要写清；
- 命令结果有 `ACCEPTED`、`SUCCEEDED`、`FAILED`、`EXPIRED`；
- 期望状态与报告状态可能冲突，要有版本/时间戳/优先级；
- 高风险命令需要确认、联锁、权限和审计；
- 批量命令要限速、分批、可取消。

### AI.5.5 安全、功能安全与 OTA

工业/汽车的安全约束远高于普通 API：

- **功能安全**：ISO 26262（汽车）、IEC 61508（工业）；
- **网络安全**：ISO/SAE 21434、IEC 62443；
- **法规**：UNECE R155（网络安全）、R156（软件更新）；
- **OTA**：签名、A/B 分区、回滚、断电恢复、分批灰度；
- **命令安全**：紧急停止、互锁、权限、双重确认；
- **设备身份**：证书、安全元件、信任根、吊销；
- **审计**：谁在何时对哪台设备做了什么。

OTA 设计要点：

```text
1. 发布固件清单（版本、哈希、签名、兼容性）
2. 设备上报当前版本/硬件/电量/网络
3. 服务端计算目标版本与批次
4. 下发下载 URL（短期、签名）
5. 设备校验签名、写入备用分区
6. 重启切换、健康检查
7. 失败回滚、上报结果
8. 灰度扩大、暂停、回滚
```

不要把 OTA 做成“一个 POST 就完事”。它是有状态、可中断、可回滚的长流程。

### AI.5.6 车队运营

车队 API 要处理：

- 批量查询设备状态；
- 批量下发命令；
- 遥测采样与聚合；
- 离线设备；
- 设备生命周期；
- 固件合规；
- 成本；
- 数据质量。

设计要点：

- 批量命令要逐台返回结果；
- 离线设备要有超时和补偿；
- 遥测要支持降采样、聚合、异常上报；
- 时间同步与时钟偏差要处理；
- 设备 ID、VIN、序列号要区分；
- 数据保留和隐私要合规。

### AI.5.7 工业/汽车 API 的设计教训

- 状态与事件分离：期望状态、报告状态、事件流各有用途；
- 命令必须有 ID、幂等、过期、确认、审计；
- 设备身份与信任根是安全基础；
- 离线是常态，不是异常；
- 单位、时间戳、数据质量要写进契约；
- 安全联锁和功能安全不能靠 API 层“提示”；
- OTA 是长流程，必须有签名、回滚、灰度和证据。

## AI.6 跨行业模式

### AI.6.1 共同能力

| 能力 | 医疗 | 开放银行 | 电信 | 工业/汽车 |
|---|---|---|---|---|
| 同意/授权资源 | Consent | Consent | 合同 + 客户同意 | 设备授权 + 安全策略 |
| 身份与委托 | SMART、OIDC | OAuth、FAPI、mTLS | 合作方身份 | 设备证书、角色 |
| 长流程 | 就诊、理赔、导出 | 支付、授权、结算 | 订单、开通、计费 | 命令、OTA、诊断 |
| 目录/术语 | LOINC/SNOMED | 账户/产品目录 | 产品目录 | 信号/节点目录 |
| 审计/证据 | AuditEvent、Provenance | 对账、报告 | CDR、账单 | 日志、签名、功能安全 |
| 标准符合性 | CapabilityStatement | 监管认证 | TMF 一致性 | OPC UA/Sparkplug 认证 |

### AI.6.2 适配层

垂直标准通常不适合直接作为内部领域模型。推荐：

```text
外部标准契约
   ↓ 适配器/防腐层
内部领域模型
   ↓
业务服务
```

适配层负责：

- 字段映射；
- 状态映射；
- 错误映射；
- 标识映射；
- 版本转换；
- 标准 quirks 隔离；
- 一致性测试。

不要让 FHIR 的 `Bundle`、TMF 的 `ProductOrder`、OPC UA 的 `NodeId` 直接渗透到整个内部系统。标准会变，内部模型不应随之震荡。

### AI.6.3 标准符合性与产品差异

- 标准保证“能接入”；
- 产品保证“愿意用”；
- 两者不冲突，但不能互相替代；
- 认证/一致性测试是入场券，不是竞争力；
- 真正的差异在文档、沙箱、错误可操作性、支持、SLA、计费和生态。

### AI.6.4 沙箱、模拟器与认证

垂直 API 的上线通常需要：

- 沙箱环境；
- 模拟器（FHIR 服务器、银行模拟器、网络模拟器、设备模拟器）；
- 测试数据；
- 一致性测试；
- 认证/审计；
- 合作伙伴 onboarding；
- 版本兼容矩阵。

设计 API 时就要考虑“如何被测试”，而不是上线后再补。

### AI.6.5 版本与迁移

垂直标准的版本迁移往往以年为单位：

- FHIR R4 → R5；
- OBIE/Berlin Group 版本升级；
- TMF Open API 版本升级；
- OPC UA 伴随规范升级；
- VSS 版本升级。

策略：

- 同时支持多个版本；
- 用适配层隔离；
- 提供迁移指南和差异报告；
- 灰度、双跑、对账；
- 设定弃用时间表；
- 与合作伙伴共同测试。

## AI.7 合规与证据

垂直 API 的合规通常不是“有没有锁”，而是“能不能证明”：

| 行业 | 主要义务 | API 证据 |
|---|---|---|
| 医疗 | HIPAA、GDPR、EHDS、患者同意、审计 | Consent、AuditEvent、Provenance、访问日志、最小化 |
| 开放银行 | PSD2/PSD3、SCA、DORA、GDPR | 同意记录、SCA 日志、令牌绑定、对账、事件报告 |
| 电信 | 隐私、通信保密、 lawful intercept、消费者保护 | 同意、用量记录、账单、审计、保留策略 |
| 工业/汽车 | UNECE R155/R156、ISO 26262、ISO/SAE 21434 | 签名、OTA 记录、诊断日志、安全事件、功能安全证据 |

合规证据要：

- 可机器读取；
- 可追溯到具体请求/资源；
- 有保留期；
- 有访问控制；
- 可导出给审计/监管；
- 能证明“谁、何时、为什么、做了什么”。

更多合规映射见[附录 T](t-compliance-engineering.md)。

## AI.8 各垂直的未来方向

垂直标准不会停在当前版本。设计 API 时，除了满足今天的认证，还要为下面这些方向留出演进空间。

### 医疗

- **FHIR 版本演进**：R4 仍是部署基线，R5 逐步被采用，后续版本继续推进；契约要能同时支持多版本，而不是一次性切换。
- **患者介导的数据交换**：患者授权应用直接访问数据，SMART 与同意模型会更重要。
- **批量与流式并存**：`$export` 继续承担历史数据，订阅/事件承担增量；两者需要一致的权限、同意和审计。
- **AI 临床文档与编码**：模型生成的结构化数据要可追溯、可校验、可人工复核；`Provenance` 与术语映射是核心。
- **隐私增强**：去标识化、差分隐私、可信执行环境会进入医疗数据共享的常规工具箱。
- **标准化简化**：FHIR Shorthand、ImplementationGuide、自动一致性测试会降低接入成本。

### 开放银行

- **PSD3 / PSR 与 Open Finance**：从支付账户扩展到储蓄、投资、保险、养老金；API 范围与同意模型都会扩大。
- **可变重复支付（VRP）**：从一次性支付走向授权范围内的周期性/事件触发支付，同意、限额、撤销更复杂。
- **去中心化身份与可验证凭证**：客户身份、授权、资质可能不再只由银行签发。
- **Agent 发起支付**：AI Agent 代表用户支付时，需要预算、商户白名单、金额上限、一次性证明与人类确认。
- **实时支付与跨境**：ISO 20022、实时清算、跨境互操作会要求更稳定的端到端 ID 与状态映射。
- **反欺诈与隐私的平衡**：更多设备、行为、网络信号，同时受数据最小化和同意约束。

### 电信

- **CAMARA 与网络能力商品化**：QoS、位置、号码验证、SIM Swap 等能力会继续 API 化，计费、同意、反欺诈是瓶颈。
- **网络切片与按需网络**：从“会话”走向“切片/服务等级”的细粒度契约。
- **意图驱动与自治网络**：API 从“配置命令”走向“表达意图 + 闭环验证”，需要可解释的状态与冲突处理。
- **6G 与边缘**：更低延迟、更多设备、更强隐私；边缘 API 与中心 API 的分层要清晰。
- **合作伙伴生态**：运营商、云、开发者、行业 ISV 的多方结算与责任边界。

### 工业与汽车

- **OPC UA PubSub / TSN**：从请求—响应走向确定性、时间敏感的网络。
- **Asset Administration Shell（AAS）**：工业 4.0 的数字孪生标准，与 OPC UA 互补。
- **软件定义汽车（SDV）**：区域架构、中央计算、OTA、服务化接口；VSS/SOME/IP 与云端 API 的映射更关键。
- **功能安全与网络安全的融合**：OTA、远程命令、诊断都要有安全证据与失效安全策略。
- **边缘 AI**：设备端推理、模型更新、数据最小化；模型版本与设备版本要一起管理。
- **车队级自动化**：批量命令、编排、合规、保险与责任认定。

### 跨垂直

- **机器消费者优先**：Agent 会读文档、调工具、做支付；契约要机器可读、错误可恢复、权限可委托。
- **契约与合规自动化**：从 OpenAPI/FHIR/TMF 契约自动生成一致性测试、审计证据和监管报告。
- **隐私工程默认化**：同意、最小化、保留、删除、差分隐私从“附加项”变成默认能力。
- **多版本长期共存**：标准升级以年计，API 要能同时服务多个版本与多个区域的变体。
- **可观测性与证据链**：不仅要知道“发生了什么”，还要能证明“谁授权、谁执行、结果如何”。

## AI.9 检查清单

### 医疗

- [ ] 使用 FHIR R4/R5 的资源、搜索参数和交互，并在 `CapabilityStatement` 中声明。
- [ ] 版本并发使用 `ETag`/`If-Match`，条件创建使用 `If-None-Exist`。
- [ ] 搜索有索引、深度限制、超时和分页上限。
- [ ] Profile、Extension、术语版本和许可已管理。
- [ ] SMART scope、患者上下文、同意、审计、来源完整。
- [ ] 批量导出异步、NDJSON、短期 URL、权限过滤、保留期。
- [ ] `OperationOutcome` 错误可操作，状态码映射正确。
- [ ] 同意撤回能传播到缓存、索引、导出和订阅。

### 开放银行

- [ ] 同意是资源，有权限、账户、时间、频率、生命周期。
- [ ] SCA 是可恢复流程，认证结果以服务端查询为准。
- [ ] 令牌绑定受众/发送方，短期、可撤销。
- [ ] 支付发起与账户信息权限分离。
- [ ] 支付状态机、幂等、查询兜底、回调、对账完整。
- [ ] 错误码稳定、可程序化、含关联 ID。
- [ ] 沙箱、模拟器、一致性测试、合作伙伴 onboarding 就绪。

### 电信

- [ ] 产品、服务、资源三层分离。
- [ ] Open API 版本、扩展命名空间、事件契约明确。
- [ ] 订单异步、状态机、OrderItem 状态、jeopardy、fallout、补偿完整。
- [ ] 网络 API 有同意、隐私、反欺诈、审计。
- [ ] 用量、CDR、计价、账单、对账可追溯。
- [ ] 遗留 OSS/BSS 通过适配层隔离。

### 工业/汽车

- [ ] 设备身份、证书、信任根、吊销、轮换完整。
- [ ] 期望状态/报告状态/事件/命令分离。
- [ ] 命令有 ID、幂等、过期、确认、审计、联锁。
- [ ] 离线、重连、乱序、重复、时钟偏差处理明确。
- [ ] OTA 有签名、A/B 分区、回滚、灰度、断电恢复。
- [ ] 单位、时间戳、数据质量、坐标/信号语义写清。
- [ ] 功能安全与网络安全证据可提供。

### 跨行业

- [ ] 标准契约与内部模型之间有防腐层。
- [ ] 多版本共存、迁移、弃用、合作伙伴通知有计划。
- [ ] 沙箱、模拟器、一致性测试、认证路径明确。
- [ ] 审计、同意、保留、删除、驻留、导出证据可验证。
- [ ] 文档、错误、SDK、支持与 SLA 达到产品级。

## 设计练习

### 练习一：跨境远程医疗与保险理赔

设计一个“跨境远程医疗 + 保险直赔”API：

- 患者身份、医生身份、机构身份；
- 视频问诊、处方、检验报告；
- 患者同意、跨境数据传输、最小化；
- 保险资格查询、预授权、理赔；
- 支付、发票、对账；
- 审计、来源、保留期；
- FHIR 资源与保险资源的映射。

回答：

1. 哪些数据可以跨境，哪些必须留在本地？
2. 同意撤回后，已导出的数据如何处理？
3. 预授权与理赔的状态机如何设计？
4. FHIR `Consent`、`AuditEvent`、`Provenance` 如何与保险事件关联？
5. 如果保险公司要求额外字段，如何扩展而不破坏 FHIR 契约？

### 练习二：开放银行支付与对账

设计一个“TPP 发起支付 + 银行回调 + 商户对账”的 API：

- 同意创建、SCA、支付发起、状态查询；
- 幂等、超时、重复回调、乱序；
- 退款、撤销、争议；
- 多币种、汇率、手续费；
- 对账与差异处理；
- 错误码与关联 ID。

回答：

1. 支付超时但银行最终成功，商户如何知道？
2. 回调丢失时如何恢复？
3. 部分退款和多次退款如何记账？
4. 如何证明“没有重复扣款”？
5. 监管要求的事件报告如何从 API 证据生成？

### 练习三：电信订单编排

设计一个“家庭宽带开通”API：

- 产品目录、地址资格查询、下单；
- 服务订单、资源订单、网络开通；
- 预约、改约、取消；
- 失败、fallout、补偿、重试；
- 账单、用量、SLA；
- 合作伙伴/装维人员接口。

回答：

1. 订单在哪个层次聚合？产品订单、服务订单、资源订单如何关联？
2. 部分资源已开通但最终失败，如何回滚？
3. 装维人员上门后发现端口不可用，如何触发重新规划？
4. 客户改约如何影响 SLA 和计费？
5. 如何对账“订单—服务—资源—账单”？

### 练习四：车队 OTA 与命令

设计一个“电动车车队 OTA + 远程命令”API：

- 车辆身份、证书、车队分组；
- 固件清单、兼容性、分批 rollout；
- 下载、校验、安装、回滚；
- 远程解锁、空调、充电控制；
- 离线、弱网、断电；
- 安全联锁、紧急停止、审计；
- 遥测、诊断、告警。

回答：

1. OTA 中途断电如何恢复？
2. 车辆离线一周后重连，旧命令是否执行？
3. 远程解锁如何防止重放和越权？
4. 如何证明某次 OTA 符合 UNECE R156？
5. 车队批量命令如何限速、分批、取消？

## 小结

- 垂直 API 的难点不在“会不会 REST”，而在标准、监管、术语和长流程的叠加。
- 医疗：FHIR 资源、搜索、Bundle、Profile、术语、SMART、同意、审计、批量导出。
- 开放银行：同意资源、SCA、FAPI、支付状态机、回调、对账、错误模型。
- 电信：产品/服务/资源三层、TMF Open API、订单编排、fallout、网络 API、计费对账。
- 工业/汽车：OPC UA、Sparkplug B、VSS、SOME/IP、UDS、数字孪生、命令/确认、OTA、功能安全。
- 跨行业共同点：身份、同意、审计、长流程、目录、标准符合性、沙箱与迁移。
- 标准负责互通，产品负责好用；两者之间需要适配层、证据和运营能力。

## 相关章节

- [第 5 章 API 风格选型](../part-1-foundations/05-style-selection.md)
- [第 10 章 写操作、幂等、并发与一致性](../part-1-foundations/10-writes-idempotency-concurrency.md)
- [第 13 章 认证](../part-2-security-runtime/13-authentication.md)
- [第 14 章 授权](../part-2-security-runtime/14-authorization.md)
- [第 19 章 多租户与数据隔离](../part-2-security-runtime/19-multi-tenancy.md)
- [第 40 章 支付与金融 API](../part-5-domain-playbooks/40-payments.md)
- [第 42 章 IoT 与设备 API](../part-5-domain-playbooks/42-iot.md)
- [附录 K 更多协议与领域速览](k-more-domains-protocols.md)
- [附录 T 合规工程](t-compliance-engineering.md)
- [附录 AH API 契约中的基础语义](ah-primitive-semantics.md)

---

返回：[目录](../SUMMARY.md)
