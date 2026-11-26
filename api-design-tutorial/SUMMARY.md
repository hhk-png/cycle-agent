# 目录

[前言：为什么 API 设计是一门独立学科](00-preface.md)

## 第一部分：契约与建模

1. [API 的本质：契约、边界与产品](part-1-foundations/01-why-api-design.md)
2. [全景地图与设计方法](part-1-foundations/02-landscape-and-method.md)
3. [契约的四个层次](part-1-foundations/03-contract-boundary-product.md)
4. [从用例到资源：领域建模](part-1-foundations/04-domain-modeling.md)
5. [API 风格选型：REST、RPC、GraphQL、事件与流](part-1-foundations/05-style-selection.md)
6. [命名、标识与 URL 设计](part-1-foundations/06-naming-and-urls.md)
7. [请求、响应与表示设计](part-1-foundations/07-representations.md)
8. [错误设计：让失败可理解、可恢复](part-1-foundations/08-errors.md)
9. [列表、分页、过滤与搜索](part-1-foundations/09-lists-and-queries.md)
10. [写操作、幂等、并发与一致性](part-1-foundations/10-writes-idempotency-concurrency.md)
11. [版本化与兼容性](part-1-foundations/11-versioning.md)
12. [异步任务与长时操作](part-1-foundations/12-async-and-long-running.md)

## 第二部分：安全与运行

13. [认证：从 API Key 到 OAuth、OIDC 与工作负载身份](part-2-security-runtime/13-authentication.md)
14. [授权：RBAC、ABAC、ReBAC 与策略即代码](part-2-security-runtime/14-authorization.md)
15. [限流、配额、并发与成本控制](part-2-security-runtime/15-rate-limits-quotas.md)
16. [缓存与条件请求](part-2-security-runtime/16-caching.md)
17. [实时 API：SSE、WebSocket、WebTransport 与 WebRTC](part-2-security-runtime/17-realtime.md)
18. [文件、二进制与大对象传输](part-2-security-runtime/18-files-and-streams.md)
19. [多租户与数据隔离](part-2-security-runtime/19-multi-tenancy.md)
20. [安全纵深：OWASP API Top 10 与供应链](part-2-security-runtime/20-security-in-depth.md)

## 第三部分：协议与架构

21. [HTTP 深度：语义、连接、缓存与调试](part-3-protocols-architecture/21-http-deep-dive.md)
22. [REST 与超媒体：成熟度、链接与发现](part-3-protocols-architecture/22-rest-and-hypermedia.md)
23. [gRPC 与 Protobuf：高性能契约的代价](part-3-protocols-architecture/23-grpc-protobuf.md)
24. [GraphQL：灵活查询与治理成本](part-3-protocols-architecture/24-graphql.md)
25. [事件驱动：CloudEvents、AsyncAPI 与消息契约](part-3-protocols-architecture/25-event-driven.md)
26. [Webhook 工程：签名、重试、去重与可观测](part-3-protocols-architecture/26-webhooks.md)
27. [API 网关与边缘架构](part-3-protocols-architecture/27-gateway-and-edge.md)
28. [服务网格、东西向流量与内部 API](part-3-protocols-architecture/28-service-mesh-internal-apis.md)
29. [BFF、聚合层与后端边界](part-3-protocols-architecture/29-bff-and-aggregation.md)
30. [契约优先、代码生成与 Schema 治理](part-3-protocols-architecture/30-contract-first.md)

## 第四部分：工程质量与产品化

31. [API 测试：从单元到契约、模糊与混沌](part-4-engineering-product/31-testing.md)
32. [可观测性：Trace、Metric、Log 与 API 分析](part-4-engineering-product/32-observability.md)
33. [性能与容量：尾延迟、吞吐与成本](part-4-engineering-product/33-performance.md)
34. [可靠性与容错：超时、重试、熔断与背压](part-4-engineering-product/34-reliability.md)
35. [SDK 设计：把复杂度留在服务端](part-4-engineering-product/35-sdks.md)
36. [文档、示例与开发者体验](part-4-engineering-product/36-docs-and-dx.md)
37. [API 治理、评审与平台化](part-4-engineering-product/37-governance.md)
38. [发布、弃用与迁移](part-4-engineering-product/38-release-deprecation.md)

## 第五部分：领域实战

39. [电商与交易 API](part-5-domain-playbooks/39-ecommerce.md)
40. [支付与金融 API](part-5-domain-playbooks/40-payments.md)
41. [SaaS 平台 API](part-5-domain-playbooks/41-saas.md)
42. [IoT 与设备 API](part-5-domain-playbooks/42-iot.md)
43. [数据平台与分析 API](part-5-domain-playbooks/43-data-platform.md)
44. [内部平台与平台工程 API](part-5-domain-playbooks/44-internal-platform.md)

## 第六部分：AI 与未来

45. [LLM API 设计：流式、Token、工具与结构化输出](part-6-ai-future/45-llm-apis.md)
46. [RAG、向量检索与知识 API](part-6-ai-future/46-rag-vector.md)
47. [Agent 协议：MCP、A2A 与工具契约](part-6-ai-future/47-agents-mcp-a2a.md)
48. [AI 原生 API：当消费者不是人](part-6-ai-future/48-ai-native-apis.md)
49. [协议与架构的未来](part-6-ai-future/49-protocol-future.md)

## 第七部分：商业分析

50. [API 商业模式与价值链](part-7-business/50-business-models.md)
51. [API 定价与包装](part-7-business/51-pricing.md)
52. [单位经济与财务模型](part-7-business/52-unit-economics.md)
53. [市场、竞争与生态](part-7-business/53-market-ecosystem.md)
54. [运营指标与增长](part-7-business/54-operations-metrics.md)
55. [风险、组织与路线图](part-7-business/55-risk-org-roadmap.md)
56. [结语：把 API 当作长期资产](part-7-business/56-conclusion.md)

## 附录

- [附录 A：API 设计检查清单](appendices/a-design-checklist.md)
- [附录 B：HTTP 状态码与方法速查](appendices/b-http-status.md)
- [附录 C：错误目录模板](appendices/c-error-catalog.md)
- [附录 D：OpenAPI 3.2 模板（兼容 3.1）](appendices/d-openapi-template.md)
- [附录 E：AsyncAPI、CloudEvents 与 Webhook 模板](appendices/e-asyncapi-webhook.md)
- [附录 F：gRPC / Protobuf 风格指南](appendices/f-grpc-style.md)
- [附录 G：GraphQL Schema 风格指南](appendices/g-graphql-style.md)
- [附录 H：安全与隐私检查清单](appendices/h-security-privacy-checklist.md)
- [附录 I：术语表](appendices/i-glossary.md)
- [附录 J：标准、工具与延伸阅读](appendices/j-standards-reading.md)
- [附录 K：更多协议与领域速览](appendices/k-more-domains-protocols.md)
- [附录 L：移动端、离线优先与同步 API](appendices/l-mobile-offline-sync.md)
- [附录 M：API 设计模式与反模式速查](appendices/m-patterns-antipatterns.md)
- [附录 N：API 合同、SLA 与采购条款](appendices/n-api-contracts-legal.md)
- [附录 O：端到端实战——设计一个活动票务平台 API](appendices/o-end-to-end-ticketing.md)
- [附录 P：API 设计评审 Rubric 与题库](appendices/p-review-rubric-and-questions.md)
- [附录 Q：国际化、本地化与可访问性](appendices/q-i18n-accessibility.md)
- [附录 R：API 实现架构与框架选型](appendices/r-implementation-architecture.md)
- [附录 S：批量操作、导入导出与数据可携带性](appendices/s-bulk-import-export.md)
- [附录 T：合规工程——从监管条款到 API 能力](appendices/t-compliance-engineering.md)
- [附录 U：API 事故复盘集——十二个失败模式](appendices/u-incident-postmortems.md)
- [附录 V：API 计量、计费与收入工程](appendices/v-metering-billing.md)
- [附录 W：API 产品管理、开发者关系与增长](appendices/w-api-product-management.md)
- [附录 X：遗留 API 现代化与迁移实战](appendices/x-legacy-modernization.md)
- [附录 Y：API 成本工程与 FinOps](appendices/y-cost-engineering-finops.md)
- [附录 Z：分布式一致性与多区域 API 设计](appendices/z-distributed-consistency-multi-region.md)
- [附录 AA：API 契约的形式化、模型检验与性质测试](appendices/aa-formal-contracts-model-checking.md)
- [附录 AB：数据契约、指标语义与数据产品 API](appendices/ab-data-contracts-metrics.md)
- [附录 AC：Serverless、边缘与 WebAssembly 运行时的 API 设计](appendices/ac-serverless-edge-wasm.md)
- [附录 AD：API 密码学工程——消息签名、令牌约束与后量子迁移](appendices/ad-cryptography-pqc.md)
- [附录 AE：API 序列化、压缩与 Schema 演进](appendices/ae-serialization-schema-evolution.md)
- [附录 AF：数据库即 API、BaaS 与自动生成接口](appendices/af-database-as-api-baas.md)
- [附录 AG：隐私增强技术与 API——从差分隐私到机密计算](appendices/ag-privacy-enhancing-tech.md)
- [附录 AH：API 契约中的基础语义——时间、金钱、数量、标识与语言](appendices/ah-primitive-semantics.md)
- [附录 AI：行业垂直 API 深潜——医疗、开放银行、电信与工业](appendices/ai-vertical-apis.md)
- [附录 AJ：沙箱、Mock 与测试数据工程](appendices/aj-sandbox-mock-test-data.md)
- [附录 AK：特性开关、实验与渐进发布](appendices/ak-feature-flags-experiments-progressive-delivery.md)
- [附录 AL：基础设施即 API——Terraform Provider、CRD 与 GitOps 契约](appendices/al-infrastructure-as-api-terraform-crd-gitops.md)
