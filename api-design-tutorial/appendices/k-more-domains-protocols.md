# 附录 K：更多协议与领域速览

> 主教程已经覆盖主流风格与六个高价值领域。本附录补齐一些常见但容易被忽略的协议与行业场景，作为广度索引。每一项只给出“设计要点”，不展开成完整章节。

## K.1 遗留与企业协议

### SOAP / WSDL

SOAP 使用 XML 信封、WSDL 描述、XSD 定义数据结构，常见于银行、保险、电信、政务。

设计要点：

- 契约优先：WSDL/XSD 是唯一真相；
- 命名空间与版本；
- `mustUnderstand` 语义；
- WS-Security、WS-Addressing；
- SOAP Fault 结构；
- MTOM 附件；
- 严格 schema 校验；
- 与 REST/JSON 网关的互操作。

SOAP 不会很快消失。设计新系统时通常不必选择它，但必须能与其集成。

### XML-RPC / JSON-RPC

轻量 RPC：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "orders.get",
  "params": { "id": "ord_123" }
}
```

特点：

- 简单；
- 传输无关；
- 支持通知（无 id）；
- 支持批量；
- 错误码；
- MCP 仍使用 JSON-RPC 2.0 作为消息模型；2026-07-28 修订把会话改为无状态请求上下文，但消息格式不变。

设计要点：

- 方法命名空间；
- 参数顺序/命名；
- 错误对象；
- 批量上限；
- 幂等；
- 认证。

### OData

OData 在 HTTP 上提供标准化的查询语法：

```text
GET /orders?$filter=status eq 'PAID'&$select=id,total&$expand=customer&$orderby=createdAt desc&$top=20
```

适合：

- 企业数据服务；
- 微软生态；
- 需要标准化查询/分页/元数据。

注意：

- 查询能力过强，需限制字段与复杂度；
- `$expand` 可能造成 N+1；
- 注入与权限；
- 与自有 API 风格的一致性。

### WebDAV / CalDAV / CardDAV

文件、日历、联系人协议：

- PROPFIND/PROPPATCH；
- REPORT；
- 锁；
- 属性；
- 同步令牌。

适合需要与现有客户端互操作的场景。设计时要处理 XML、命名空间、深度、权限和同步。

## K.2 性能与类型协议

### tRPC

TypeScript 端到端类型安全 RPC：

- 共享类型；
- 无需 codegen；
- 适合全栈 TypeScript；
- 与前端框架集成好。

限制：

- 强耦合 TypeScript；
- 不适合公开 API；
- 版本演进依赖客户端同步；
- 跨语言支持弱。

### Cap'n Proto / FlatBuffers

零拷贝序列化：

- 无需反序列化即可访问字段；
- 高性能；
- schema 演进；
- 适合游戏、实时系统、嵌入式。

代价：

- 工具链；
- 人类可读性；
- 调试；
- 生态。

### MessagePack / CBOR

二进制 JSON 替代：

- 更小；
- 更快；
- 支持二进制；
- 可读性差。

适合内部高性能场景，不适合作为公开 API 默认格式。

## K.3 消息与 IoT 协议

### MQTT

发布/订阅、低带宽、QoS 0/1/2、保留消息、遗嘱消息。

设计要点：

- 主题设计；
- QoS；
- 会话；
- 认证；
- 权限；
- 消息大小；
- 离线队列；
- 顺序。

### CoAP

UDP 上的轻量协议，适合低功耗设备：

- 请求/响应；
- 观察模式；
- 块传输；
- DTLS。

### LwM2M

设备管理协议：

- 注册；
- 观察；
- 固件升级；
- 连接管理；
- 安全。

### AMQP

消息队列协议：

- 交换机/队列/路由；
- 确认；
- 事务；
- 流控。

## K.4 行业协议

### FHIR / HL7（医疗）

FHIR 用资源模型表达患者、观察、处方、就诊等：

```text
Patient
Observation
MedicationRequest
Encounter
```

设计要点：

- 资源与扩展；
- 版本（R4/R5）；
- 患者同意；
- 审计；
- 去标识化；
- 批量导出；
- SMART on FHIR；
- 互操作。

医疗 API 的合规要求极高，权限、审计、数据保留和跨境都需要专门设计。

### OpenRTB / AdCOM（广告）

实时竞价：

```text
Bid Request → Bid Response → Win Notice → 计费
```

设计要点：

- 极低延迟；
- 高并发；
- 宏替换；
- 隐私/同意；
- 欺诈检测；
- 计费对账；
- 版本兼容。

### TM Forum Open API（电信）

电信行业的标准化 API：

- 客户、产品、订单、计费、服务；
- 事件；
- 开放 API 规范；
- 与 BSS/OSS 集成。

### NDC（航空）

航空分销标准：

- 报价；
- 订单；
- 支付；
- 变更；
- 取消。

设计要点：

- 可用性；
- 运价规则；
- 订单状态；
- 多渠道一致性；
- 对账。

### SCIM（身份）

已在前文讨论。补充：

- `/Users`、`/Groups`；
- PATCH；
- 过滤；
- 分页；
- 批量；
- 错误；
- 多租户。

## K.5 更多领域设计速览

### 社交与内容

核心资源：

```text
User, Follow, Post, Comment, Reaction, Feed, Report, ModerationCase
```

设计要点：

- 关注图；
- 时间线/Feed 扇出；
- 内容审核；
- 隐私；
- 举报/封禁；
- 反垃圾；
- 推荐；
- 删除/撤回；
- 计数一致性。

Feed 的扇出（写扩散 vs 读扩散）会显著影响 API 的延迟与一致性承诺。

### 实时协作

核心：

```text
Document, Operation, Presence, Comment, Version, Permission
```

设计要点：

- OT/CRDT；
- 操作顺序；
- 离线合并；
- 冲突解决；
- Presence；
- 权限变更；
- 版本历史；
- 大文档；
- 断线恢复。

实时协作 API 通常需要 WebSocket/WebTransport + 快照 + 增量。

### 医疗

核心：

```text
Patient, Practitioner, Encounter, Observation, Medication, Consent, AuditEvent
```

设计要点：

- 最小必要数据；
- 患者同意；
- 访问审计；
- 去标识化；
- 紧急访问（break-glass）；
- 数据保留；
- 互操作（FHIR）；
- 监管。

### 物流

核心：

```text
Shipment, Package, Route, Stop, TrackingEvent, ProofOfDelivery, Exception
```

设计要点：

- 多段运输；
- 轨迹；
- ETA；
- 异常；
- 签收；
- 逆向物流；
- 承运商集成；
- 对账。

### 游戏

核心：

```text
Player, Match, Session, Leaderboard, Inventory, Purchase, Report
```

设计要点：

- 低延迟；
- 匹配；
- 排行榜；
- 反作弊；
- 虚拟物品；
- 支付；
- 分区；
- 断线重连；
- 公平性。

### 教育

核心：

```text
Course, Enrollment, Assignment, Submission, Grade, Progress, Certificate
```

设计要点：

- LMS 互操作（SCORM/xAPI/LTI）；
- 隐私（学生数据）；
- 评估；
- 防作弊；
- 无障碍；
- 多语言；
- 家长/教师/学生角色。

### 电信

核心：

```text
Subscriber, Product, Order, Service, Usage, Bill, TroubleTicket
```

设计要点：

- TM Forum Open API；
- 号码/套餐；
- 开通/停复机；
- 用量；
- 计费；
- 网络 API；
- 合规。

### 政务/开放数据

核心：

```text
Dataset, Distribution, Catalog, Query, Export
```

设计要点：

- 开放标准（DCAT、OData）；
- 批量下载；
- 数据质量；
- 隐私；
- 许可；
- 无障碍；
- 长尾查询；
- 缓存。

### 广告/营销

核心：

```text
Campaign, Audience, Creative, Placement, Impression, Click, Conversion
```

设计要点：

- 同意；
- 归因；
- 频控；
- 预算；
- 反欺诈；
- 实时竞价；
- 测量；
- 隐私。

### 旅游

核心：

```text
Property, RoomType, Availability, Rate, Reservation, Cancellation
```

设计要点：

- 库存/价格实时性；
- 时区；
- 取消政策；
- 支付；
- 分销（GDS/NDC）；
- 超售；
- 对账。

### 地理空间与地图

核心：

```text
Place, Address, Coordinate, Route, RouteMatrix, TileSet, Geofence, MapMatch
```

设计要点：

- 坐标系要显式：WGS84（EPSG:4326）与 Web Mercator（EPSG:3857）不能混用；
- 几何用 GeoJSON（RFC 7946），注意经度在前、反子午线、极点与精度；
- 地点 ID 要稳定，地址解析要返回置信度与匹配级别；
- 路径规划要区分步行/驾车/骑行、实时路况、备选路线与距离矩阵；
- 瓦片用 `z/x/y`，注意缓存、attribution、许可与不要绕过供应商条款；
- 地理围栏要处理点在多边形内、边界、跨区域与隐私；
- 批量地理编码/路径计算适合异步任务，不要用同步接口拖垮超时；
- 相关标准：OGC API - Features、WMS/WMTS、Mapbox Vector Tile、ISO 19115。

### 搜索与推荐

核心：

```text
Index, Document, Query, Facet, RankingProfile, Feedback, Recommendation
```

设计要点：

- 倒排索引与向量检索各有所长，生产常用混合检索 + 重排；
- 查询 DSL 要区分“过滤”（布尔条件）与“相关性”（打分）；
- 分析器要按语言配置：分词、词干、同义词、拼写容错、CJK 切分；
- 排序要可解释：BM25、语义分、业务规则、新鲜度、个性化各占多少；
- 评估用 NDCG、MRR、Recall@k，线上看点击、转化与多样性；
- 注意位置偏差与反馈回路，不要把“排第一所以被点击”当成质量；
- 索引生命周期：全量重建、增量更新、别名切换、零停机重建；
- 多租户要隔离索引或在查询层强制租户过滤；
- 隐私：查询日志、个人数据、删除请求必须能传播到索引。

### 通信（短信、邮件、语音、推送）

核心：

```text
Message, Template, Campaign, Sender, DeliveryReceipt, OptOut, Suppression
```

设计要点：

- 渠道差异大：短信有发送方 ID 与模板审核，邮件有 SPF/DKIM/DMARC，推送有设备令牌失效；
- 发送是异步的，状态通过 Webhook 或查询回传：queued、sent、delivered、failed、bounced；
- 幂等键防止重复发送；同一用户多渠道要统一去重与频控；
- 合规：同意、退订、静默时段、模板审批、反垃圾；
- 成本按条/按分钟，失败重试要区分永久失败与临时失败；
- 抑制列表（bounce、complaint、opt-out）要在发送前检查；
- 不要在日志中记录完整消息内容与收件人。

### 开放银行与金融账户

核心：

```text
Account, Balance, Transaction, Consent, PaymentConsent, StandingOrder, Beneficiary
```

设计要点：

- 标准并不统一：英国 OBIE、欧盟 Berlin Group、美国 FDX，字段与流程各有差异；
- 同意（consent）是核心资源：创建、SCA 授权、访问、刷新、过期、撤销；
- 数据最小化与用途限制要写进同意，而不是只在隐私政策里；
- 支付发起与账户信息是不同权限，不能用一个 scope 覆盖；
- 状态回调要幂等，且要能通过查询兜底；
- 错误要区分：同意过期、需要重认证、账户不可用、限额、制裁；
- 高安全场景使用 FAPI 2.0、mTLS/DPoP、请求签名与证书轮换。

### 身份、目录与设备

核心：

```text
User, Group, Device, Session, Credential, Role
```

设计要点：

- SCIM 2.0 用于用户/组的下发与回收，支持过滤、分页、PATCH 与批量；
- 回收（deprovisioning）比开通更容易被忽略，必须有明确的 SLA；
- OIDC/SAML 负责联邦登录，SCIM 负责生命周期，两者不要混为一谈；
- 设备信任、证书生命周期、MDM 状态会影响 API 授权；
- 组嵌套与最终一致性会让权限判断出现延迟，关键操作要重新检查；
- 审计要记录“谁在何时通过哪个身份源获得了什么权限”。

### 区块链与账本 API

核心：

```text
Address, Transaction, Block, Event, Token, Contract
```

设计要点：

- 不要把节点 RPC 直接暴露给业务方；用索引器/网关做查询与限流；
- 明确最终性：确认数、重组（reorg）、回滚处理；
- nonce 管理、签名、手续费估算与交易替换是钱包侧的核心；
- 事件日志要能回填与去重，链上重组会导致“已确认”变成“未确认”；
- 不要把“已广播”当成“已结算”，链下账本仍要做对账；
- 密钥管理、合规、制裁筛查与隐私比链本身更难。

### 能源、公用事业与设备

核心：

```text
Meter, MeterReading, Usage, Tariff, Agreement, Outage, Device
```

设计要点：

- 区间用量（interval data）要带时区、单位（kWh）、计量周期与估算标记；
- 表计更换、估读、补录会造成历史数据修正，API 要支持版本与更正；
- 标准：Green Button（ESPI）、OpenADR、DLMS/COSEM、IEEE 2030.5；
- 需求响应命令是高风险操作，要有确认、超时与安全联锁；
- 用户授权与数据共享要可撤销，注意家庭隐私；
- 停电/复电事件适合事件流，不适合让客户端高频轮询。

### 保险

核心：

```text
Quote, Policy, Coverage, Claim, Document, Payment
```

设计要点：

- 报价、承保、出单、批改、取消是不同状态，不要用一个 `policy` 字段表达；
- 理赔是长流程：报案、查勘、定损、审核、赔付、追偿；
- 文档与证据要版本化、可审计、有保留期；
- 费率、承保规则与核保结果要可解释，且能复现；
- 健康/医疗数据涉及更严格的隐私与同意；
- 行业标准（如 ACORD）可作为字段参考，但不要假设所有伙伴都用同一版本。

### 供应链与物流标准

核心：

```text
Order, Shipment, Item, HandlingUnit, Event, Document
```

设计要点：

- 标识体系：GTIN、SSCC、GLN 等 GS1 标识要稳定且可校验；
- EDI（X12/EDIFACT）仍大量存在，API 与报文之间要有明确映射；
- 运输事件用 EPCIS 或自定义事件表达：打包、发运、中转、派送、签收、异常；
- 多方可见性要控制数据共享范围，避免泄露客户与价格；
- 异常（延误、破损、丢件）是核心用例，不是边角；
- 条码/RFID 扫描常在网络不稳定环境，离线队列与幂等很重要。

## K.6 跨领域模式

| 模式 | 适用 |
|---|---|
| 资源 + 动作 | 大多数业务 API |
| CQRS | 读写差异大 |
| 事件驱动 | 多消费者、异步 |
| Saga | 跨服务长事务 |
| Outbox | 可靠事件发布 |
| BFF | 多端聚合 |
| 网关聚合 | 简单跨服务查询 |
| 实时流 | 协作、监控、游戏 |
| 批量/导出 | 数据平台、报表 |
| Webhook | 外部异步通知 |
| Agent 工具 | AI 自动化 |

## K.7 选择建议

- 先看行业标准是否强制；
- 再看消费者生态；
- 再看团队能力；
- 最后看性能与成本；
- 不要为了统一而统一；
- 在边界处做协议转换，不要把转换逻辑散落各处。

> 本附录只做广度索引。医疗 FHIR、开放银行、电信 TM Forum/CAMARA、工业 OPC UA/Sparkplug B/汽车 VSS 的深入设计，见[附录 AI：行业垂直 API 深潜](ai-vertical-apis.md)；时间、金额、单位与标识的基础语义，见[附录 AH](ah-primitive-semantics.md)。

---

返回：[目录](../SUMMARY.md)
