# 附录 I：术语表

## A

**A2A（Agent2Agent）**  
Agent 与 Agent 之间进行能力发现、任务委派与产物交换的开放协议。1.0（2026-03）支持 JSON-RPC、gRPC、HTTP+JSON 三种绑定，并支持版本协商与签名 Agent Card。

**AAIF（Agentic AI Foundation）**  
Linux Foundation 下托管 MCP、A2A 等 Agent 协议的基金会组织，2026 年起成为相关治理主体。

**ABAC（Attribute-Based Access Control）**  
基于属性的访问控制，用主体、资源、环境属性表达策略。

**AG-UI**  
Agent 与前端 UI 之间的交互协议，关注流式消息、状态更新与人工确认。

**Anti-Corruption Layer（防腐层）**  
在旧系统与新模型之间做转换与隔离的适配层，防止旧术语、旧字段与旧错误污染新契约。

**API（Application Programming Interface）**  
两个独立主体之间可验证的长期约定，包含语法、语义、行为与运行承诺。

**API Gateway**  
南北向流量的入口，负责路由、认证、限流、缓存、可观测性等跨切面能力。

**API Committee（API 委员会）**  
跨团队维护风格指南、评审高风险契约、处理例外与推动迁移的治理机制；应有服务水平，避免成为审批瓶颈。

**API Key**  
一种长期凭证，通常用于服务器到服务器认证、计量与限流。

**ARR（Annual Recurring Revenue）**  
年度经常性收入，通常为 MRR × 12。

**AP2（Agent Payments Protocol）**  
面向 Agent 支付的委托协议，用可验证的意图委托与购物车委托限定支付范围。

**AsyncAPI**  
描述事件驱动 API 的规范。

**Arazzo**  
OpenAPI Initiative 的 API 工作流描述规范，用 YAML 描述跨多个操作的步骤、依赖与成功条件；用于文档、契约测试与工具生成，不是运行时编排引擎。

**Audit Log**  
不可篡改的操作记录，用于合规、排查与追责。

## B

**BaaS（Backend as a Service）**  
托管后端服务，通常提供数据库、认证、文件存储、实时订阅与边缘函数，客户端可经生成式 API 直接访问数据面。

**BFF（Backend for Frontend）**  
为特定客户端提供聚合与裁剪的后端层。

**BOLA（Broken Object Level Authorization）**  
对象级授权缺失，OWASP API 风险第一位。

**Bounded Context**  
领域驱动设计中的边界，边界内术语与模型一致。

**BCP 47**  
IETF 的语言标签标准（RFC 5646），用“语言 + 书写系统 + 地区”表达语言，如 `zh-Hans-CN`、`sr-Latn-RS`。

**Business Day（业务日）**  
按工作日规则、节假日日历、截止时间和时区计算出的可营业日期；不等于简单的日历日加一天。

**Bulkhead**  
舱壁模式，隔离资源池防止故障扩散。

## C

**Canary（金丝雀发布）**  
把新版本先暴露给小比例真实流量，基于 SLI 观察后再逐步扩大；关键在样本代表性、自动分析和快速回滚。

**CAP 定理**  
网络分区发生时，分布式系统只能在一致性（线性一致）与可用性之间取舍；不适用于无分区场景，也不包含延迟。

**Causal Consistency（因果一致）**  
有因果关系的操作按因果顺序可见，并发操作可以不同顺序可见。

**Cold Start（冷启动）**  
FaaS/边缘运行时首次处理请求时需要的调度、启动与初始化时间，直接影响尾延迟。

**Consistency Model（一致性模型）**  
对并发读写可见顺序的约定，如线性一致、顺序一致、因果一致、最终一致与会话保证。

**CAC（Customer Acquisition Cost）**  
获客成本。

**Cache-Control**  
HTTP 缓存指令。

**Canonicalization（规范化）**  
把语义相同但字节不同的表示转换为唯一字节序列的过程，用于签名、哈希与幂等指纹；JSON 常用 JCS（RFC 8785），CBOR 有确定性编码。

**CBOM（Cryptography Bill of Materials）**  
密码学物料清单，记录系统使用的算法、密钥、证书、协议与依赖，是后量子迁移与合规审计的基础。

**CBOR（Concise Binary Object Representation）**  
RFC 8949 定义的二进制数据格式，支持确定性编码、标签与 CDDL schema，常用于 IoT、COSE 与受限环境。

**CDC（Consumer-Driven Contract）**  
消费者驱动契约测试。注意与 Change Data Capture 区分，二者在同一本书中都会出现。

**Change Data Capture（CDC）**  
从数据库变更日志捕获插入/更新/删除并发布为事件，常用于迁移、搜索索引与数据同步。

**Content-Digest**  
RFC 9530 定义的 HTTP 内容摘要字段，用于绑定消息 body 的完整性，常与 RFC 9421 消息签名配合。

**CRD（CustomResourceDefinition）**  
Kubernetes 中定义自定义资源类型与版本的 API 对象；包含 schema、subresources、printer columns 与 conversion 配置。

**Crypto Agility（密码敏捷）**  
把算法、密钥长度、协议版本做成可配置、可协商、可迁移的能力，使系统能在不破坏契约的情况下更换密码学原语。

**Chargeback（内部计费）**  
按成本向内部团队实际收费，强激励但容易产生分摊争议；通常先做 showback。

**Choreography**  
服务通过事件协同，无中心协调者。

**Commitment（承诺消费）**  
客户承诺在一定期间内支付或使用的最低金额/用量；工程上要区分承诺金额、承诺用量、预付费与 true-up。

**CIBA（Client Initiated Backchannel Authentication）**  
由客户端在后端发起、用户在另一设备确认的认证流程，适合呼叫中心与 IoT。

**Circuit Breaker**  
熔断器，依赖持续失败时快速失败。

**CloudEvents**  
CNCF 的事件信封标准。

**CQRS**  
命令查询职责分离。

**CRDT（Conflict-free Replicated Data Type）**  
可在多个副本上并发修改并最终自动收敛的数据类型，常用于离线协作与同步。

**Credit Note（贷项通知单）**  
对已开票金额的正式冲减文件，用于退款、折扣、迟到事件或计费更正；不改历史发票。

**Cursor Pagination**  
基于不透明游标的分页。

**Calendar（业务日历）**  
定义工作日、节假日、截止时间与业务日约定的规则集合；金融、排班、结算必须显式引用日历及其版本。

**CLDR（Common Locale Data Repository）**  
Unicode 的本地化数据仓库，提供日期、数字、货币、排序、复数规则等 locale 数据。

**Consent（同意）**  
数据主体对特定目的、范围、期限和行为的授权；在医疗、开放银行等场景应建模为有生命周期的资源，而不是布尔字段。

**Conformance（符合性）**  
实现是否符合标准/规范的可验证程度；垂直 API 常通过一致性测试、认证与 `CapabilityStatement` 表达。

**CRS（Coordinate Reference System，坐标参考系统）**  
定义坐标如何映射到地球的参照系，如 EPSG:4326（WGS84）、EPSG:3857（Web Mercator）；必须与坐标值一起声明。

## D

**Data Act（EU Data Act）**  
欧盟《数据法案》，2025-09-12 起适用，要求云与数据处理服务提供开放接口、切换能力与可携带性，并逐步禁止切换费用。

**Data Clean Room（数据洁净室）**  
多方在受控环境中联合分析的机制：数据不落地到对方，查询需审批，输出需满足最小群体、聚合或差分隐私等限制，并保留审计。

**Data Contract（数据契约）**  
数据生产者与消费者之间关于 schema、语义、新鲜度、质量、所有权与生命周期的可执行约定，不只是表结构。

**Data Factory（数据工厂）**  
用确定性种子、独立租户与显式覆盖构造测试对象的代码设施；目标是可重复、可组合、可清理。

**Data Product（数据产品）**  
可发现、可寻址、可信、可互操作的数据资产，通常有明确 owner、SLA、质量报告与访问方式。

**Data Freshness（数据新鲜度）**  
数据从事件发生到可被查询的延迟，通常用数据截止时间（watermark）与 `data_age` 度量。

**DDD（Domain-Driven Design）**  
领域驱动设计。

**Dead Letter Queue（DLQ）**  
死信队列，存放无法处理的消息。

**Deprecation**  
弃用，标记接口/字段即将移除。

**Differential Privacy（差分隐私）**  
一种随机化机制的隐私保证：相邻数据集（只差一条记录）产生任意输出的概率比被限制在 e^ε 内；可用隐私预算组合多次查询。

**Distributed Monolith**  
分布式单体，服务拆分了但契约/数据仍高度耦合。

**DORA（Digital Operational Resilience Act）**  
欧盟金融行业数字运营韧性法规，2025-01-17 起适用，覆盖 ICT 风险、事件报告、第三方风险与韧性测试。

**DPoP（Demonstrating Proof of Possession）**  
将令牌绑定到客户端密钥的机制。

**Drift（漂移）**  
期望状态、实际状态与记录状态之间的不一致；控制平面 API 必须检测、归因并选择报告、自动修复或人工批准。

**Dual Write（双写）**  
迁移期同时写新旧两个系统；必须明确主写、从写、失败补偿与对账，否则会产生数据分裂。

**Dunning（催收）**  
发票逾期后的提醒、宽限、暂停与终止流程。

**DST（Daylight Saving Time，夏令时）**  
部分地区在夏季把本地时间前调一小时的规则；会产生“本地时间不存在”的春季缺口和“本地时间出现两次”的秋季重叠。

## E

**Egress Gateway（出口网关）**  
收敛并审计服务出站流量的网关，负责允许列表、TLS 发起、凭证注入、限流与审计。

**Entitlement（权益）**  
套餐或合同授予客户的可用能力/额度，通常独立于角色权限，用于功能开关、配额与计费。

**Entity**  
有独立身份和生命周期的领域对象。

**ETag**  
资源版本标识，用于缓存与并发控制。

**Event**  
已经发生的事实的不可变记录。

**Event Sourcing**  
以事件序列作为状态真相。

**Exactly-once**  
恰好一次投递，通常只在特定边界内成立。

**E.164**  
ITU-T 的国际电话号码格式：`+` 加国家码和号码，最多 15 位；API 中的电话号码应优先规范化为 E.164。

## F

**Experiment Flag（实验开关）**  
用于分流并比较效果的开关；应绑定实验 ID、主要指标、护栏指标、负责人与结束时间。

**Exposure（曝光）**  
实验单元首次进入某个变体的记录；分析应按首次曝光分组，避免重试和重复请求污染。

**FAPI（Financial-grade API）**  
OpenID Foundation 的金融级 API 安全配置，通常组合 PAR、PKCE、mTLS/DPoP 等约束。

**FDX（Financial Data Exchange）**  
美国的金融数据共享标准，定义账户、交易、支付与同意的 API 与安全要求；与 OBIE、Berlin Group 是不同区域的同类标准。

**FHIR（Fast Healthcare Interoperability Resources）**  
HL7 的医疗互操作资源标准，用 REST、资源、搜索、Bundle、Profile、术语与订阅表达临床和管理数据；R4 仍是部署最广的基线，R5 提供更多能力。

**Feature Flag（特性开关）**  
在不重新部署的情况下控制行为或可见性的配置；一旦改变消费者可见行为，就成为契约的一部分。

**Federated Learning（联邦学习）**  
多方在本地数据上训练、只交换模型更新或聚合结果的机器学习范式；隐私仍需安全聚合、差分隐私与防投毒机制保障。

**Fencing Token（隔离令牌）**  
每次成为写主时获得的单调递增令牌，存储层拒绝更小令牌的写，用于防止旧主恢复后继续写入。

**Federation（GraphQL）**  
把多个服务的 schema 组合成统一图。

**FinOps**  
云与 API 成本的持续运营实践：归因、预算、预测、异常、优化与跨团队协作。

**Filter**  
查询条件，不是搜索相关性排序。

**Finalizer（终结器）**  
Kubernetes 中阻止对象在清理完成前被删除的标记；必须幂等、有超时和强制移除流程。

## G

**GitOps**  
以 Git 中的声明作为期望状态，由同步器持续收敛集群并检测漂移的交付方式；PR 是评审入口，commit 是审计记录。

**Golden Path（黄金路径）**  
平台提供的默认最佳实践路径；应默认安全、可观测、合规，同时允许有成本地偏离。

**gRPC**  
基于 HTTP/2 和 Protobuf 的 RPC 框架。

**GraphQL**  
客户端声明字段的图查询语言与运行时。

**Grapheme Cluster（字形簇）**  
用户感知的一个字符，可能由多个 Unicode 码点组成（如 emoji、组合重音）；字符串长度限制必须说明按字节、码点还是字形簇计算。

## H

**HLC（Hybrid Logical Clock）**  
混合逻辑时钟，结合物理时钟与逻辑计数器，用于近似实时且保持因果顺序的事件排序。

**HQC**  
一种基于码的后量子密钥封装算法，2025 年被 NIST 选为 ML-KEM 之外的多样化备份，标准仍在制定中。

**Hasura**  
基于数据库 schema 自动生成 GraphQL API 的引擎，提供角色、行/列级权限、Actions、Event Triggers 与 Remote Schemas。

**HATEOAS**  
超媒体作为应用状态引擎。

**HMAC**  
基于哈希的消息认证码，常用于 Webhook 签名。

**Homomorphic Encryption（同态加密）**  
允许在密文上直接计算的密码学方案；按能力分为部分同态、leveled 同态、近似同态（如 CKKS）与全同态，代价是密文膨胀与计算开销。

**HTTP/2**  
二进制分帧、多路复用的 HTTP 版本。

**HTTP/3**  
基于 QUIC/UDP 的 HTTP 版本。

**HTTP QUERY**  
RFC 10008（2026-06）定义的安全、幂等、可缓存且可携带请求体的 HTTP 方法，用于复杂查询。

## I

**Idempotency**  
幂等，多次执行效果与一次相同。

**Idempotency-Key**  
用于让非幂等请求可安全重试的键。

**IdP（Identity Provider）**  
身份提供方。

**Ingress**  
Kubernetes 的 HTTP 入口资源。

**Invoice（发票）**  
金额的正式承诺文件；终态后不可修改，更正通过贷项通知单或补充发票。

**IANA tzdb（时区数据库）**  
维护全球时区、历史偏移和夏令时规则的数据库；API 应使用其中的 IANA 名称（如 `Asia/Shanghai`），并记录解析时使用的版本以保证可复现。

**Identifier（标识）**  
用于稳定指代实体的值；对外 ID 应不透明、不可枚举、不复用、URL 安全，且不携带业务语义或个人信息。

**ISO 4217**  
定义货币代码与小数位（exponent）的标准；金额必须带币种，最小单位应按币种而非固定两位处理。

**ISO 8601**  
日期和时间的国际标准；RFC 3339 是其互联网 profile，API 的时刻字段通常使用 RFC 3339。

## J

**JARM（JWT Secured Authorization Response Mode）**  
用 JWT 签名授权响应的扩展，防止授权码与 state 在返回途中被篡改。

**JTBD（Jobs-to-be-Done）**  
以“客户要完成的任务”而非功能或端点来理解需求的框架。

**JWT（JSON Web Token）**  
带签名的声明令牌，不是加密容器。

**JCS（JSON Canonicalization Scheme）**  
RFC 8785 定义的 JSON 规范化方案，规定键排序、数字序列化与字符串转义，用于签名与哈希。

**JOSE（JSON Object Signing and Encryption）**  
JWT/JWS/JWE/JWK/JWKS 等 JSON 密码学结构的统称；使用时的核心风险是算法混淆、密钥解析与声明校验。

**JSON Schema**  
描述 JSON 结构的 schema 标准。

**JSON:API**  
一种规范化的 JSON API 媒体类型与约定。

## K

**k-anonymity（k-匿名）**  
要求每个等价类至少包含 k 条记录，使单条记录无法与另外 k−1 条区分；不能抵抗同质性、背景知识与增量发布攻击。

**Keyset Pagination**  
基于排序键比较的分页。

**Kafka**  
分布式事件日志平台。

**Kill Switch（紧急开关）**  
用于故障、成本失控或安全事件时快速关闭一条路径的开关；需要独立通道、秒级生效、审计与演练。

## L

**Latency Percentile**  
延迟分位数，如 P95/P99。

**Linearizability（线性一致）**  
最强单对象一致性：每个操作看起来在其调用与返回之间的某个实时点原子生效，并遵守实时顺序。

**Little's Law**  
L = λ × W，描述并发、到达率与停留时间关系。

**Load Shedding（过载保护/主动丢弃）**  
系统过载时主动拒绝或降级部分请求，以保护关键流量与整体可用性。

**llms.txt**  
放在网站根目录、供大模型工具阅读的文档导航约定，不是 API 契约。

**LTV（Lifetime Value）**  
客户终身价值。

**Luhn**  
银行卡号等标识的校验位算法；只能发现录入错误，不能证明账户存在或有效。

**Legacy Modernization（遗留现代化）**  
在不中断业务的前提下，把旧协议、旧模型、旧实现逐步替换为可演进契约的工程实践。

## M

**MCP（Model Context Protocol）**  
AI 应用连接工具/资源/提示的开放协议。2026-07-28 修订采用无状态请求上下文、`server/discover`、MRTR 与扩展机制，不再依赖 `initialize` 握手与 `Mcp-Session-Id` 会话。

**Metering（计量）**  
把使用事件转换为可计价数量的过程，包括采集、去重、聚合、迟到处理与可重放存储。

**ML-DSA**  
FIPS 204 标准化的基于格的后量子数字签名算法（源自 Dilithium），用于替代 RSA/ECDSA/EdDSA。

**ML-KEM**  
FIPS 203 标准化的基于格的后量子密钥封装算法（源自 Kyber），用于替代 RSA/ECDH 密钥交换。

**Mock（模拟）**  
用契约生成的静态或状态化响应替代真实服务；只能验证消费者期望，不能证明提供方符合契约。

**MPC（Secure Multi-Party Computation，安全多方计算）**  
多个参与方在不共享原始数据的情况下联合计算函数；常见构件包括秘密分享、混淆电路、不经意传输与私有集合求交。

**mTLS**  
双向 TLS，服务间强身份认证。

**Multi-tenancy**  
多租户，一个系统服务多个隔离客户。

**Minor Unit（最小货币单位）**  
币种可表示的最小整数单位，如人民币的“分”、日元的“日元”；小数位由 ISO 4217 exponent 决定，不能统一按两位处理。

**Money（金额）**  
由数值、币种、精度、舍入规则和分摊规则组成的语义类型；禁止用浮点数表示。

**MQTT Sparkplug B**  
在 MQTT 之上定义工业设备主题、出生/死亡消息、序列号、别名与指标的规范，用于边缘节点与设备的可靠状态同步。

## N

**N+1 Query**  
列表查询中逐项查询关联数据导致的性能问题。

**NIS2**  
欧盟《网络与信息系统安全指令》第二版，转化截止日为 2024-10-17，强调风险管理、供应链安全与事件报告。

**NRR（Net Revenue Retention）**  
净收入留存。

**NRT（Near Real Time）**  
近实时。

**NFC（Unicode Normalization Form C）**  
Unicode 的一种规范化形式；比较、搜索、去重前通常先做 NFC，但“用户看到的相同”不一定等于“字节相同”。

## O

**OAuth 2.1**  
OAuth 2.0 的整合与安全增强版本，强调 PKCE、移除隐式/密码流程。截至 2026 年末仍是 IETF 草案，技术内容稳定但尚未成为 RFC。

**OHTTP（Oblivious HTTP，RFC 9458）**  
通过中继与网关两层转发隐藏客户端 IP 与请求内容的隐私传输方案，使用 HPKE 加密，不防时间关联与流量分析。

**OIDC（OpenID Connect）**  
在 OAuth 2.0 上提供身份层。

**OpenAPI**  
描述 HTTP API 的规范。3.2（2025-09）为当前版本，向后兼容 3.1；4.0（Moonwalk）尚未发布。

**ORM**  
对象关系映射。

**Outbox Pattern**  
业务表与事件表同事务写入，异步发布事件。

**OBIE（Open Banking Implementation Entity）**  
英国开放银行标准与治理机构，定义账户信息、支付发起、同意与错误模型；与 Berlin Group、FDX 是不同区域的同类标准。

**OPC UA（Open Platform Communications Unified Architecture）**  
工业互操作标准（IEC 62541），提供信息模型、服务、会话、安全、PubSub、报警与历史访问。

## P

**Pagination**  
分页。

**PAR（Pushed Authorization Requests）**  
先把授权参数推送到授权服务器、再用 `request_uri` 发起授权的 OAuth 流程。

**PATCH**  
部分更新方法。

**PCI DSS**  
支付卡行业数据安全标准。

**PACELC**  
CAP 的扩展：分区时在可用性与一致性之间取舍，否则在延迟与一致性之间取舍。

**PET（Privacy-Enhancing Technology，隐私增强技术）**  
用于在计算、发布或共享数据时限制个体可推断性的技术总称，包括差分隐私、MPC、同态加密、TEE、联邦学习、合成数据与元数据保护。

**PII（Personally Identifiable Information）**  
个人可识别信息。

**Point-in-Time Correctness（时点正确性）**  
训练样本只能使用事件发生时可见的特征，避免用未来信息造成标签泄漏。

**PostgREST**  
把 PostgreSQL 的表、视图与函数自动映射为 REST API 的服务，权限主要由数据库角色与 RLS 决定。

**PQC（Post-Quantum Cryptography，后量子密码）**  
能抵抗量子计算机攻击的公钥密码体系；NIST 首批标准为 FIPS 203（ML-KEM）、FIPS 204（ML-DSA）与 FIPS 205（SLH-DSA）。

**Price Book（价格表）**  
一组计量项、阶梯价格、币种与生效区间的版本化集合；计价必须能追溯到价格表版本。

**Privacy Budget（隐私预算）**  
一组查询/发布允许消耗的累计隐私损失上界；应像配额一样按数据集、租户与时间窗记账，耗尽后拒绝或降低精度。

**Problem Details（RFC 9457）**  
标准错误响应格式。

**Proration（按比例计算）**  
在账单周期中途升级、降级或变更套餐时，按剩余时间比例计算费用。

**PSD2（Payment Services Directive 2）**  
欧盟支付服务指令，要求开放银行、强客户认证（SCA）与第三方提供商准入；后续 PSD3/PSR 的落地进度以官方文本为准。

**Progressive Delivery（渐进发布）**  
把发布从一次性全量中拆出，通过暗发布、金丝雀、环形、蓝绿、brownout 等模式控制爆炸半径和回滚速度。

**Protobuf**  
Protocol Buffers，强类型二进制序列化格式。

## Q

**Quota**  
长期用量配额，区别于短期限流。

## R

**RACI**  
责任分配矩阵：Responsible（执行）、Accountable（最终负责）、Consulted（被咨询）、Informed（被通知）。

**RAG（Retrieval-Augmented Generation）**  
检索增强生成。

**RAR（Rich Authorization Requests）**  
用结构化 `authorization_details` 表达金额、账户、用途等细粒度授权的 OAuth 扩展。

**Reconciliation（协调/收敛）**  
控制器持续比较期望状态与实际状态并修复差异的循环；必须水平触发、幂等、可重入、有界重试。

**Rate Limit**  
限流。

**Read-Your-Writes（读己之写）**  
同一会话写入后，后续读至少能看到自己的写；通常用会话粘性或版本令牌实现。

**Refinement（精化）**  
证明实现行为满足抽象规格的方法；工程上常用不变量对齐、性质测试与契约测试替代完整证明。

**Rating（计价）**  
按价格表、套餐、折扣、承诺与税，把计量用量转换为金额的确定性规则引擎。

**RBAC（Role-Based Access Control）**  
基于角色的访问控制。

**ReBAC（Relationship-Based Access Control）**  
基于关系的访问控制。

**Reconciliation（对账）**  
比较两个系统或两个阶段的用量、金额、状态，定位并解释差异的控制活动。

**REST**  
表述性状态转移架构风格。

**Revenue Recognition（收入确认）**  
按履约义务与交付进度，判断收入何时、多少计入财务报表；开票不等于收入。

**Row-Level Security（RLS，行级安全）**  
数据库按行强制访问策略的机制；在自动生成 API 中常是唯一的数据隔离防线，必须默认拒绝并覆盖所有暴露面。

**RPO（Recovery Point Objective）**  
可接受的数据丢失量。

**RRULE**  
RFC 5545 定义的日历重复规则，如“每月最后一个周五”；与 cron 不同，RRULE 更适合表达业务日历语义。

**RTO（Recovery Time Objective）**  
可接受的恢复时间。

## S

**Saga**  
长事务的一组本地事务与补偿。

**Sandbox（沙箱）**  
与生产隔离、使用测试密钥、可重置、可注入失败的契约副本；对人或 Agent 而言都是可验证的环境。

**Secure Aggregation（安全聚合）**  
多方上传更新时只让服务器得到聚合结果、看不到单个参与方更新的协议；常与联邦学习、差分隐私配合。

**SCIM**  
系统间用户/组同步标准。

**SLH-DSA**  
FIPS 205 标准化的基于哈希的后量子签名算法（源自 SPHINCS+），签名大、速度慢，但安全假设保守。

**Standard Webhooks**  
一套 Webhook 签名与投递头的公共约定，使用 `webhook-id`、`webhook-timestamp` 与 `webhook-signature`，签名内容为 `${id}.${timestamp}.${body}`。

**Structured Fields（结构化字段）**  
RFC 8941/9651 定义的 HTTP 字段值类型系统，让头部可被可靠解析与组合，常用于签名、缓存状态与限流参数。

**Semantic Layer（语义层）**  
在物理表之上定义实体、维度、指标、关系与权限的一层，使 BI 与 API 使用同一口径。

**Serverless**  
由平台管理服务器与扩缩容的运行模型，通常按调用与执行时长计费；FaaS 是其常见形态。

**Split Brain（脑裂）**  
网络分区后出现多个自认为的主节点，导致并发写入与数据分叉。

**Staleness（陈旧度）**  
读到的数据落后于最新已确认写的程度；契约应给出上界与测量方式。

**Shadow Traffic（影子流量）**  
把生产流量复制到新系统进行比对，但不让新系统影响生产结果；写操作需特别处理副作用。

**Showback（成本展示）**  
向团队展示其成本，但不实际收费；用于建立成本意识与优化激励。

**Schema Registry**  
schema 版本与兼容性管理服务。

**Server-Side Apply（服务端应用）**  
由 API server 按字段管理者合并对象变更的机制；字段所有权不清晰时会导致 GitOps、Terraform 与控制器互相覆盖。

**SLA（Service Level Agreement）**  
对外服务等级协议。

**SLI（Service Level Indicator）**  
服务等级指标。

**SLO（Service Level Objective）**  
服务等级目标。

**SSE（Server-Sent Events）**  
基于 HTTP 的单向服务器推送。

**SSRF（Server-Side Request Forgery）**  
服务端请求伪造。

**State（Terraform 状态）**  
Terraform 记录“它认为存在什么”的状态文件；可能含明文密钥，需要加密、锁、版本控制与升级机制。

**Strangler Fig（绞杀者模式）**  
新实现逐步替换旧功能，旧系统像被藤蔓包裹一样逐步退役，避免大爆炸重写。

**STRIDE**  
威胁建模框架：Spoofing、Tampering、Repudiation、Information Disclosure、Denial of Service、Elevation of Privilege。

**Supabase**  
基于 PostgreSQL 的 BaaS，组合 PostgREST、GoTrue 认证、Realtime、Storage 与 Edge Functions；匿名 key 的安全性依赖 RLS，`service_role` key 绕过 RLS。

**SCA（Strong Customer Authentication，强客户认证）**  
至少结合知识、持有、固有中的两类要素的认证；开放银行支付与高价值操作的核心要求。

**SMART on FHIR**  
在 OAuth 2.0 之上为医疗应用定义的授权框架，使用 `patient/*.read`、`user/*.read`、`launch/patient`、`fhirUser` 等 scope 与启动上下文。

**SOME/IP（Scalable service-Oriented MiddlewarE over IP）**  
汽车以太网的面向服务通信协议，定义方法、事件、字段与服务发现。

**Sparkplug B**  
见 **MQTT Sparkplug B**。

**Synthetic Data（合成数据）**  
用生成模型产生的替代数据；只有经过差分隐私训练与严格评估时，才可能提供形式化隐私保证，否则仍可能记忆训练样本。

## T

**Table Format（开放表格式）**  
在数据文件之上提供快照、事务、schema 演进与时间旅行的元数据层，如 Iceberg、Delta Lake、Hudi。

**TAM/SAM/SOM**  
总市场/可服务市场/可获得市场。

**TCC（Try-Confirm-Cancel）**  
一种分布式事务模式：预留资源、确认执行、失败取消。

**TEE（Trusted Execution Environment，可信执行环境）**  
用硬件隔离保护运行时数据的执行环境，如 SGX/TDX、SEV-SNP、CCA、Nitro Enclaves；通常结合远程认证与密封存储使用。

**Terraform Provider**  
把云/基础设施 API 翻译成声明式资源与数据源的插件；契约由 schema、CRUD、Read、导入与 state 语义共同组成。

**Test Clock（测试时钟）**  
可注入、可推进或可偏移的时间源，用于稳定测试计费、试用、过期、定时任务与 DST 边界。

**TLA+**  
用于描述并发与分布式系统状态机、并用模型检验器（TLC）检查不变量的形式化规格语言。

**Tenant**  
租户。

**Timeout Budget**  
超时预算。

**TM Forum Open API**  
电信行业的标准化 REST API 集合，覆盖产品目录、产品订单、服务开通、资源库存、用量与账单；与 SID 信息模型配套。

**Token Bucket**  
令牌桶限流算法。

**Token Exchange（RFC 8693）**  
用一方令牌换取另一方令牌的 OAuth 扩展，支持委托与租户切换。

**TTFB（Time to First Byte）**  
首字节时间。

**True-up（期末补足）**  
承诺消费未达到约定额度时，期末补收差额或调整额度的安排。

**TTFC（Time to First Call）**  
首次调用时间。

**TTFI（Time to First Integration）**  
从注册到完成生产集成的时间。

**TTFV（Time to First Value）**  
从注册到完成一个真实业务任务、获得可感知价值的时间。

## U

**UUIDv7**  
按时间有序的 UUID 版本。

**ULID**  
时间有序的字符串 ID。

**Unit Cost（单位成本）**  
总成本除以业务量（请求、token、GB、租户等）；必须选对业务量并分租户/端点看。

**UCUM（Unified Code for Units of Measure）**  
机器可读的单位代码标准，大小写敏感，常用于医疗、实验室与药品剂量；比自定义单位字符串更适合契约。

**Upsert**  
不存在则创建，存在则更新。

## V

**Value Object**  
无独立身份、由属性定义的值对象。

**VIN（Vehicle Identification Number）**  
车辆识别代号，17 位，带校验位；可作为车辆外部标识，但不应替代内部稳定 ID。

**VSS（Vehicle Signal Specification）**  
COVESA 定义的车辆信号树，用 `Vehicle.Speed` 这类路径表达信号、类型与单位。

**Versioning**  
版本化，管理契约兼容性。

**Vary**  
HTTP 缓存键的请求头维度。

## W

**WASM（WebAssembly）**  
可移植的二进制运行时，常用于插件沙箱、边缘函数与多语言扩展。

**Watermark（水位线）**  
数据流中“截至某时间的事件已基本到齐”的标记，用于定义查询完整性与迟到数据处理。

**Webhook**  
服务端主动向调用方端点推送事件的反向 API。

**WebSocket**  
全双工通信协议。

**WebTransport**  
基于 HTTP/3 的多路复用传输。

**WORM（Write Once Read Many）**  
一次写入多次读取，常用于审计存储。

**WSJF（Weighted Shortest Job First）**  
加权最短作业优先，用延迟成本除以工作规模排序。

## X

**x402**  
基于 HTTP 402 与支付证明的机器间支付协议，常用于按次调用与微支付。

**XSS（Cross-Site Scripting）**  
跨站脚本攻击。

## Z

**Zero Trust**  
零信任，不因网络位置信任，每个请求都验证。

**Zanzibar**  
Google 的关系型授权系统，ReBAC 代表。

---

返回：[目录](../SUMMARY.md)
