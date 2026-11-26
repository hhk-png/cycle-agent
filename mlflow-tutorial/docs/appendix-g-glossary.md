# 附录 G 术语表

| 术语 | 含义 |
|---|---|
| Experiment | 一个业务问题/项目的容器，包含多个 Run |
| Run | 一次可审计的执行，记录参数、指标、标签、制品 |
| Param | 影响结果的不可变输入，通常字符串化 |
| Metric | 带时间戳/step 的数值历史，追加写入 |
| Tag | 可变的上下文标签（团队、状态、commit） |
| Artifact | 任意文件/目录，存放在 Artifact Store |
| Artifact URI | artifact 根路径（file/s3/...） |
| Run Status | RUNNING/SCHEDULED/FINISHED/FAILED/KILLED 等；后三者是终态 |
| Lifecycle Stage | active/deleted（软删除） |
| Model | 可加载的制品 + 契约（flavor、签名、依赖） |
| Flavor | 模型的原生格式/加载方式 |
| PyFunc | 统一的 Python 预测接口 |
| MLmodel | 模型目录的 YAML 元数据文件 |
| Model Signature | 输入/输出 schema |
| Registered Model | 稳定的逻辑模型名 |
| Model Version | 不可变的版本，指向 source |
| LoggedModel | 3.x 的一等模型实体，`model_id` 即身份，可未注册直接加载 |
| Source | 模型制品来源 URI（3.x 常为 `models:/<model_id>`，2.x 兼容 `runs:/...`） |
| Alias | 可变的发布指针（champion/canary）；`latest` 是 URI 关键字，不是普通 alias |
| Stage | 遗留的发布阶段（Staging/Production/Archived），3.x 不推荐 |
| Project | 用 MLproject 描述的可复现执行 |
| Entry Point | 项目的一个命令入口 |
| Evaluate | 计算/记录评测指标 |
| Scorer | 评测函数/模型（确定性或 LLM） |
| Trace | 一次请求的调用链 |
| Span | Trace 中的时间节点 |
| Webhook | 把实体变更以 HTTP POST 通知订阅方的事件机制 |
| Outbox | 先持久化待投递事件、再由 dispatcher 发送的模式 |
| Delivery ID | 一次投递的唯一标识，用于接收方幂等/去重 |
| HMAC | 基于密钥的哈希消息认证码，用于 webhook 签名 |
| Replay | 用旧请求/事件重放，需时间窗与去重防护 |
| Review Queue | 人工评审任务队列，答案写入 assessment |
| MCP | Model Context Protocol，Agent 工具/上下文协议 |
| Judge | 用模型按 rubric 评分的 scorer |
| OTel | OpenTelemetry，可观测性标准 |
| OTLP | OTel 协议（HTTP/gRPC） |
| LLM-as-judge | 用模型按 rubric 评分 |
| RAG | 检索增强生成 |
| Agent | 具备规划/工具调用能力的 LLM 系统 |
| Drift | 数据/概念分布变化 |
| Inference Gateway | 推理网关：路由、限流、鉴权、灰度和观测的流量入口 |
| Liveness / Readiness | 存活/就绪探针；前者判进程，后者判能否接流量 |
| Dynamic Batching | 动态批处理：在等待窗口内聚合请求以提高吞吐 |
| Cold Start | 冷启动：模型/运行时首次加载的额外延迟 |
| SSE | Server-Sent Events，LLM 流式输出的常见协议 |
| Model Digest | 模型制品的摘要，用于固化版本与校验完整性 |
| SLO | 服务等级目标（可用性/延迟/错误率） |
| Canary | 小流量发布 |
| Shadow | 影子流量 |
| Blue/Green | 蓝绿部署 |
| RBAC | 基于角色的访问控制 |
| Multi-tenancy | 多租户隔离 |
| RPO/RTO | 恢复点/恢复时间目标 |
| TTL | 生存时间/自动过期 |
| GC | 垃圾回收（孤儿 artifact/软删除清理） |
| CAS | 内容寻址存储 |
| Monitor | 绑定基线分布、特征契约与阈值的监控定义 |
| Baseline | 参考分布（训练集/评测集/稳定窗口），监控比较的起点 |
| Window | 一段被评估的生产记录；Mini 中是不可变报告 |
| PSI | 群体稳定性指数，衡量分桶占比的整体位移 |
| KS | Kolmogorov-Smirnov 两样本统计量，比较数值分布 CDF |
| Cramér's V | 类别关联强度，配合卡方避免“大样本一切显著” |
| Data Drift | 输入分布变化；不等于模型性能下降 |
| Concept Drift | 输入到输出的关系变化；需要标签才能确认 |
| Delayed Label | 真实结果晚于预测到达，性能监控天然滞后 |
| Session | 一段多轮对话；Mini 用 trace tag `mlflow.trace.session` 派生 |
| Turn | 会话中的一轮请求/响应，通常对应一条 trace |
| Conversation Simulator | 用模型或脚本扮演用户、生成多轮测试轨迹的组件 |
| Goal Checker | 判断模拟会话是否达标的模型/规则 |
| Session-level Scorer | 对整段会话打分的 scorer，结论通常挂首条 trace |
| Online Evaluation | 对生产 trace 按采样配置自动评分 |
| ScorerSamplingConfig | 在线评分配置：sample_rate + filter_string |
| Issue | 由重复失败聚合出的可分诊问题，带证据与严重度 |
| Assert Improved | 评测门禁：只有显著变好才通过，样本不足 fail closed |
| Paired Comparison | 同一评测集上逐 case 配对比较，消除题目难度差异 |
| BCa Bootstrap | 偏差校正加速 bootstrap 置信区间；真实 MLflow 比较 API 使用 |
| Skill Registry | 版本化 Agent 技能包并提供拉取/别名机制（3.18 实验） |
| Skill | 指令 + 脚本/资源包，供编码代理或 Agent 框架使用 |
| Idempotency | 幂等：重复执行结果一致 |
| Keyset Pagination | 基于排序键的分页 |
| SSRF | 服务端请求伪造 |
| RCE | 远程代码执行 |
| PII | 个人身份信息 |
| CORS | 跨源资源共享 |
| CSRF | 跨站请求伪造 |
| XSS | 跨站脚本 |
| WAL | SQLite 预写日志 |
| MVCC | 多版本并发控制（Postgres 等） |
| TCO | 总拥有成本 |
| ROI | 投资回报率 |
| ARR/MRR | 年/月经常性收入 |
| CAC/LTV | 获客成本/客户终身价值 |
| NRR | 净收入留存 |
| SAM/SOM/TAM | 可服务/可获得/总体市场 |

---

返回：[教程首页](../README.md) · [附录 H](appendix-h-exercises.md)
