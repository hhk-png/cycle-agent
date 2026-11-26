# 附录 E 面试题与实战清单

## E.1 基础

1. **流处理和批处理的本质区别？**  
   输入是否有界，以及是否需要持续维护状态/处理乱序。

2. **Event Time 和 Processing Time 的区别？**  
   Event Time 来自事件本身，可重放；Processing Time 来自机器时钟，不可重放。

3. **Watermark 的作用？**  
   告诉系统“某时间之前的数据已到齐”，用于关闭窗口/推进时间。

4. **Watermark 如何生成？**  
   周期生成（有界乱序/单调）或标点生成；多输入取最小值。

5. **Watermark 为什么会卡住？**  
   空闲分区/输入未开 idleness、source 不生成、时间戳解析错误、多输入取最小。

6. **窗口有哪些类型？**  
   滚动、滑动、会话、累积、全局；事件时间/处理时间版本。

7. **Trigger 和 Evictor 的区别？**  
   Trigger 决定何时算，Evictor 决定算哪些元素。

8. **Allowed Lateness 和 Side Output 的区别？**  
   Allowed Lateness 延长窗口并更新结果；Side Output 把超范围数据分流。

9. **会话窗口为什么需要 merge？**  
   会话边界动态合并，聚合器必须支持合并部分结果。

10. **滑动窗口的成本？**  
    一条记录进入 `size/slide` 个窗口，状态和计算量成倍增长。

## E.2 状态与容错

11. **Keyed State 和 Operator State 的区别？**  
    Keyed 按 key 分区，只能 keyBy 后用；Operator 与 subtask 绑定，用于 source/sink。

12. **HashMap 和 RocksDB backend 如何选？**  
    小状态/低延迟用 HashMap；大状态/增量快照用 RocksDB。

13. **ForSt 能替代 RocksDB 吗？**  
    2.3 仍实验，不支持 canonical savepoint/全量快照/changelog；生产大状态仍优先 RocksDB。

14. **Checkpoint 和 Savepoint 的区别？**  
    自动/手动、生命周期、格式稳定性、用途不同。

15. **什么是 barrier 对齐？**  
    多输入等所有屏障到齐再快照；期间缓冲后续记录。

16. **非对齐 checkpoint 解决什么问题？**  
    反压时跳过对齐等待，把 in-flight 数据写入快照；代价是快照更大。

17. **端到端 exactly-once 需要什么？**  
    source offset 进 checkpoint、sink 事务/幂等、checkpoint 完成提交、下游正确隔离级别。

18. **恢复后为什么会重复？**  
    从上一个 checkpoint 重放；如果 sink 非幂等/非事务，就会重复。

19. **Key group 和 rescale 的关系？**  
    key 哈希到 key group，key group 再分配给 subtask；扩缩容按 key group 迁移。

20. **本地恢复的代价？**  
    依赖节点稳定、占本地磁盘；节点漂移退化为远程恢复。

## E.3 SQL 与 Join

21. **动态表和 changelog 是什么？**  
    流上的表持续变化，用 +I/-U/+U/-D 表示变更。

22. **Regular Join 为什么会状态膨胀？**  
    需要保留两侧历史以等待未来匹配；应设 TTL 或改用 interval/temporal join。

23. **Lookup Join 如何优化？**  
    Async I/O、缓存、限流、超时、侧输出；小表可广播。

24. **窗口 TVF 和旧 GROUP BY TUMBLE 的区别？**  
    TVF 更标准，支持窗口连接/TopN/去重；旧写法逐步淘汰。

25. **SinkUpsertMaterializer 的作用？**  
    在有状态算子后保证 upsert 顺序，代价是额外状态。

26. **物化表的 FRESHNESS 是 SLA 吗？**  
    不是，是目标新鲜度；planner 据此选择 CONTINUOUS/FULL。

27. **Delta Join 适合什么？**  
    多流同源、相同 key 的 join，用外部索引点查替代两侧完整状态；有 SQL 模式与存储前提。

## E.4 运行与调优

28. **反压如何排查？**  
    找反压最高算子 → 看下游/sink → 看倾斜/GC/状态 → 看 checkpoint 竞争。

29. **Checkpoint 失败常见原因？**  
    存储、超时、反压、状态大、序列化、磁盘、并发。

30. **如何解决数据倾斜？**  
    采样热点、加盐、两阶段聚合、广播小表、拆作业。

31. **K8s 上如何升级 Flink 作业？**  
    savepoint + Operator upgradeMode + 灰度 + 回滚。

32. **大状态恢复慢怎么办？**  
    增量 checkpoint、本地恢复、减少状态、并行恢复、RocksDB 调优。

33. **网络缓冲怎么调？**  
    先看 buffer/credit 指标；`buffer-timeout` 平衡吞吐/延迟；`network.memory` 按规模。

34. **OOM 如何区分？**  
    堆内/堆外/容器 OOMKill/RocksDB 磁盘；看 GC、heap dump、容器事件、内存模型。

35. **作业频繁重启怎么办？**  
    看异常栈/失败 task/checkpoint/资源；不要用无限重启掩盖 bug。

## E.5 系统设计题

36. **设计一个实时风控平台。**  
    事件契约、Kafka、Flink 作业拆分、CEP/规则、维表、状态、EOS、SLO、容量、部署、演练。

37. **设计一个实时数仓。**  
    CDC、ODS/DWD/DWS/ADS、Paimon/OLAP、schema 演进、小文件、对账。

38. **设计一个多租户 Flink 平台。**  
    资源队列/namespace、Catalog/权限、配额、监控、计费、升级、隔离。

39. **设计一个 exactly-once 入湖链路。**  
    source offset、checkpoint、湖表 2PC、compaction、下游 read_committed。

40. **设计一个实时特征平台。**  
    窗口聚合、特征存储、点查、回填、一致性、延迟。

## E.6 实战清单

- [ ] 用 MiniFlink 跑通 word count、窗口、checkpoint；
- [ ] 给一个作业设计 watermark 和 allowedLateness；
- [ ] 用 Kafka + Flink 做端到端 EOS，并 kill TM 验证；
- [ ] 用 Paimon/Iceberg 做 CDC 入湖；
- [ ] 写一个 Async I/O 维表 join，带缓存和超时；
- [ ] 用 EXPLAIN 分析一个 SQL 计划；
- [ ] 压测一个热点 key，观察倾斜；
- [ ] 做一次 savepoint 升级和回滚；
- [ ] 写故障排查 runbook；
- [ ] 算一次 3 年 TCO 和 ROI；
- [ ] 做一次 checkpoint 失败注入；
- [ ] 做一次 watermark 卡住排查。

## E.7 前沿与正确性

41. **DataStream API V2 和 V1 的核心区别？**  
    隐藏算子内部、ProcessFunction 统一入口、显式流类型/分区、StateDeclaration、广义 watermark；V2 仍是实验。

42. **ForSt 适合什么场景？**  
    超大状态、频繁扩缩容、云原生、可接受实验特性；不适合强依赖 canonical savepoint 的核心链路。

43. **物化表和普通流作业的区别？**  
    声明 FRESHNESS，由 planner 选择 CONTINUOUS/FULL；新鲜度是目标不是 SLA。START_MODE 是 FLIP-557 的 2.4 路线/厂商方言，不在 2.3 开源语法中。

44. **PTF 和普通 UDF 的区别？**  
    PTF 有状态、定时器、多表输入输出、changelog；UDF 主要是一行进一行出。

45. **ML_PREDICT 的失败语义？**  
    模型调用不是事务性的；checkpoint 不能回滚外部副作用，需要幂等/死信/超时/限流。

46. **VECTOR_SEARCH 的一致性限制？**  
    默认按最新快照做处理时间关联，重放不一定可复现；要记录索引/embedding 版本。

47. **Flink Agents 如何保证外部动作可靠？**  
    Flink checkpoint + 外部 Action State Store + reconciler；仍需幂等键和审计。

48. **如何设计一个流式对账系统？**  
    定义不变量、事件级/窗口级/业务级对账、T+0/T+1/抽样、差异分类、告警/工单。

49. **影子链路怎么比较新旧结果？**  
    事件级 join、聚合差异、分布差异、成本差异；影子 sink 不能污染生产副作用。

50. **幂等键怎么设计？**  
    业务主键、确定性、进入 sink 唯一约束、跨系统统一；不要用随机/自增。

## E.8 开放题

- 实时和离线结果对不上，如何定位？
- 业务要求“绝对不丢”，但 sink 只支持 at-least-once，怎么办？
- 状态 10 TB，恢复要 2 小时，如何优化？
- 是否应该把所有实时任务迁到 Flink SQL？
- 流处理平台如何做多租户和计费？
- 什么时候不该用 Flink？
- 如何向非技术管理层论证流处理的 ROI？

回答时先澄清业务语义和约束，再给方案与取舍，不要直接背配置。
