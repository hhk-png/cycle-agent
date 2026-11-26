# 附录 F 延伸阅读与参考资料

## F.1 官方文档

- Apache Flink：https://flink.apache.org/
- Flink 文档（stable）：https://nightlies.apache.org/flink/flink-docs-stable/
- Flink REST API：https://nightlies.apache.org/flink/flink-docs-stable/docs/ops/rest_api/
- PyFlink 文档：https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/python/overview/
- State Processor API：https://nightlies.apache.org/flink/flink-docs-stable/docs/libs/state_processor_api/
- Table 连接器开发：https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/table/sourcessinks/
- Flink GitHub：https://github.com/apache/flink
- Flink Kubernetes Operator：https://nightlies.apache.org/flink/flink-kubernetes-operator-docs-stable/
- Flink CDC：https://nightlies.apache.org/flink/flink-cdc-docs-stable/
- Flink Agents：https://github.com/apache/flink-agents
- Apache Kafka：https://kafka.apache.org/documentation/
- Apache Paimon：https://paimon.apache.org/
- Apache Iceberg：https://iceberg.apache.org/
- Apache Hudi：https://hudi.apache.org/
- Apache Fluss（2026 年毕业为 TLP）：https://fluss.apache.org/

## F.2 版本与发布说明

- Flink Blog（release notes 索引）：https://flink.apache.org/posts/
- Flink 2.1 Release：https://flink.apache.org/2025/07/31/apache-flink-2.1.0-ushers-in-a-new-era-of-unified-real-time-data--ai-with-comprehensive-upgrades/
- Flink 2.2 Release：https://flink.apache.org/2025/12/04/apache-flink-2.2.0-advancing-real-time-data--ai-and-empowering-stream-processing-for-the-ai-era/
- Flink 2.3 Release：https://flink.apache.org/2026/06/25/apache-flink-2.3.0-release-announcement/
- Flink Agents 0.4：https://flink.apache.org/2026/10/30/apache-flink-agents-0.4.0-release-announcement/
- Model Inference（ML_PREDICT）：https://nightlies.apache.org/flink/flink-docs-release-2.2/zh/docs/dev/table/sql/queries/model-inference/
- Flink Improvement Proposals（FLIP）：https://cwiki.apache.org/confluence/display/FLINK/Flink+Improvement+Proposals
- DataStream API V2（FLIP-408）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-408%3A+Introduce+DataStream+API+V2
- State Access on DataStream V2（FLIP-433）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-433%3A+State+Access+on+DataStream+API+V2
- Materialized Tables（FLIP-550）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-550%3A+Add+similar+support+for+CREATE%2FALTER+operations+for+MATERIALIZED+TABLES+as+for+TABLES
- 物化表 `START_MODE`（FLIP-557）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-557%3A+Granular+Control+over+Data+Reprocessing+in+Materialized+Table+Evolution
- 物化表 `FRESHNESS` 可选（FLIP-551）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-551%3A+Make+FRESHNESS+Optional+for+Materialized+Tables
- 物化表 `DISTRIBUTED BY`（FLIP-542）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-542%3A+Make+materialized+table+DDL+consistent+with+regular+tables
- Upsert sink `ON CONFLICT`（FLIP-558）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-558%3A+SinkUpsertMaterializer+%26+ON+CONFLICT
- Changelog PTF（FLIP-564）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-564%3A+Support+FROM_CHANGELOG+and+TO_CHANGELOG+built-in+PTFs
- Flink Agents（FLIP-531）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-531%3A+Initiate+Flink+Agents+as+a+new+Sub-Project
- CTAS（FLIP-218）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-218%3A+Support+SELECT+clause+in+CREATE+TABLE%28CTAS%29
- CTAS 原子性（FLIP-305）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-305%3A+Support+atomic+for+CREATE+TABLE+AS+SELECT+%28CTAS%29+statement
- RTAS（FLIP-303）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-303%3A+Support+REPLACE+TABLE+AS+SELECT+statement
- 应用级生命周期（FLIP-549）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-549%3A+Support+Application+Management
- CTAS schema 定义（FLIP-463）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-463%3A+Schema+Definition+in+CREATE+TABLE+AS+Statement
- 批作业推测执行（FLIP-168）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-168%3A+Speculative+Execution+for+Batch+Job
- 混合 shuffle 与 Celeborn（FLIP-459）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-459%3A+Support+Flink+hybrid+shuffle+integration+with+Apache+Celeborn
- REST 请求参数清理（FLIP-337/338）：https://cwiki.apache.org/confluence/display/FLINK/Flink+Improvement+Proposals
- Queryable State 弃用（FLINK-32559）：https://issues.apache.org/jira/browse/FLINK-32559
- VARIANT 类型（FLIP-521）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-521%3A+Integrating+Variant+Type+into+Flink
- Delta Join（FLIP-486）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-486%3A+Introduce+A+New+DeltaJoin
- Streaming Multi-Way Join（FLIP-516）：https://cwiki.apache.org/confluence/display/FLINK/FLIP-516%3A+Streaming+Multi-Way+Join+Optimization

> 版本号和发布日期会变化；落地时以官方 release notes 与所用发行版为准。

## F.3 论文与经典

- Tyler Akidau et al., *The Dataflow Model: A Practical Approach to Balancing Correctness, Latency, and Cost in Massive-Scale, Unbounded, Out-of-Order Data Processing*, VLDB 2015.
- Tyler Akidau, Slava Chernyak, Reuven Lax, *Streaming Systems*, O'Reilly, 2018.
- Chandy & Lamport, *Distributed Snapshots: Determining Global States of Distributed Systems*, ACM TOCS, 1985.
- Akidau et al., *MillWheel: Fault-Tolerant Stream Processing at Internet Scale*, VLDB 2013.
- Kreps, Narkhede, Rao, *Kafka: a Distributed Messaging System for Log Processing*, NetDB 2011.
- Carbone et al., *Apache Flink: Stream and Batch Processing in a Single Engine*, IEEE Data Eng. Bull., 2015.
- Mei, Xia, Lan et al., *Disaggregated State Management in Apache Flink 2.0*, VLDB 2025.
- *Apache Flink 1.9+ 的 state/checkpoint 相关论文与 FLIP*。

## F.4 工程实践

- Martin Kleppmann, *Designing Data-Intensive Applications*（DDIA）。
- Flink Forward 演讲与 slides。
- 各云厂商的 Flink 最佳实践文档（AWS、阿里云、Confluent、华为云等）。
- Apache Flink 中文社区/邮件列表/Meetup。
- Paimon/Iceberg/Hudi 的官方文档与社区。

## F.5 本书代码

- `miniflink/`：教学实现；
- `examples/`：22 个示例；
- `tests/`：156 个测试；
- `chapters/`：41 章；
- `appendix/`：16 个附录；
- `check_docs.py`：文档一致性检查；
- `verify.sh`：一键验证。

## F.6 如何继续学习

1. 读完第 1～9 章，建立时间/状态/容错主线；
2. 跑 MiniFlink 测试和示例，理解实现；
3. 在本地 Flink 集群跑一个 Kafka→Flink→Kafka 作业；
4. 做一次故障注入和 savepoint 恢复；
5. 读 Flink 源码的关键类（附录 H 给路线）；
6. 在自己业务里做一个小型实时链路，记录 SLO 和成本；
7. 读 2.x 的 release notes/FLIP，理解演进方向（第 34 章）；
8. 评估实时 AI/检索/Agent 的边界（第 35 章）；
9. 建立对账/回放/幂等机制（第 36 章）；
10. 用 PyFlink 跑一个带 UDF 的端到端作业，验证依赖分发与 worker 内存（附录 I）；
11. 按附录 J 做一次可复现的压测，记录基线、瓶颈和恢复时间；
12. 按[附录 M](M-rest-operations.md)把提交、savepoint、停止和回滚串成一条可重试的发布流程；
13. 按[附录 N](N-realtime-warehouse-modeling.md)为一个业务域画出事实/维度模型，并设计与离线层的对账；
14. 按[附录 O](O-sql-patterns.md)把去重/TopN/窗口/维表/CDC 的生产 SQL 模式过一遍，选一个在自己的数据上跑；
15. 按[附录 P](P-release-change-management.md)为下一次有状态升级写一份变更单、兼容性门禁和回滚 Runbook。

## F.7 版本提示

Flink 2.x 仍在演进，连接器、SQL 语法、配置项可能变化。落地时以所用版本的官方文档和 release notes 为准；本书中的商业数字和成本算例仅用于教学分析。
