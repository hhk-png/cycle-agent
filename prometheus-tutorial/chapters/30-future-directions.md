# 第 30 章 未来方向：Prometheus、OpenTelemetry 与可观测性

## 30.1 已经确定的方向

以下不是猜测，而是已经在 3.x 中发生/推进的方向：

1. **原生直方图**：3.8 已稳定，3.9 起旧 feature flag 变为 no-op；抓取改为 `scrape_native_histograms: true`。
2. **Remote Write 2.0**：metadata/exemplar/native histogram/更高效的编码；截至 2026-11 仍是实验规范 `2.0-rc.4`。
3. **OTLP 集成**：Prometheus 直接接收 OTLP，Collector 生态成熟。
4. **UTF-8 指标名**：兼容更多命名体系，但生态迁移需要时间。
5. **Delta temporality**：OTel 的 delta 指标如何进入 Prometheus 模型。
6. **Agent 模式**：边缘抓取 + 中心存储的规模化。
7. **PromQL 函数/语义**：`info()`、native histogram 函数、`limitk/limit_ratio` 等。

## 30.2 原生直方图的成熟

原生直方图在 Prometheus 3.8（2025-11）已经稳定，不再是"未来功能"。已经在生产发生的变化：

- 高基数直方图的序列数大幅下降；
- 分位数精度提高，桶设计不再需要预先猜测；
- 抓取端用 `scrape_native_histograms: true` 显式开启，3.9 起旧 flag 不再生效；
- remote write 用 `send_native_histograms: true` 发送，接收端需要支持。

仍然存在的迁移工作：

- 客户端库/Collector/后端支持矩阵不一致；
- 旧面板/告警需要兼容层（`always_scrape_classic_histograms`、NHCB 或 recording rule）；
- 存储成本/查询性能要按真实分布验证，不能只看序列数；
- 与经典直方图共存期间的命名/查询语义要统一。

## 30.3 OpenTelemetry 的融合

趋势：

- OTel 成为 instrumentation 的标准；
- Prometheus 成为指标存储/查询的标准之一；
- Collector 作为转换/治理层；
- 语义约定（semantic conventions）统一指标名/属性。

可能的形态：

```text
App (OTel SDK) → Collector → Prometheus remote write / OTLP → 后端
```

而不是"OTel 取代 Prometheus"。两者在不同层解决问题：OTel 管采集/语义，Prometheus 管指标模型/查询/告警。

## 30.4 eBPF 与零侵入

eBPF 可以从内核采集：

- HTTP/gRPC 延迟；
- 网络连接/重传；
- 系统调用；
- 资源使用。

它降低 instrumentation 成本，但：

- 内核版本/权限限制；
- 语义/基数治理仍需要；
- 与 OpenMetrics/exemplar 的衔接。

Prometheus 生态通过 exporter/Collector 接收 eBPF 指标，而不是内置 eBPF。

## 30.5 基数与成本的长期压力

随着：

- 微服务数量增长；
- Kubernetes 对象爆炸；
- OTel 属性增多；
- AI/LLM 服务的新指标；

基数压力只会更大。长期方向：

- native histogram；
- 更智能的采样/聚合；
- 列式/对象存储；
- 按租户/指标的 retention；
- 查询下推/索引优化。

## 30.6 AI/LLM 可观测性

新场景：

- token 使用/成本；
- 推理延迟（TTFT/TPOT）；
- 模型/版本/租户维度；
- 向量检索/缓存命中；
- GPU 利用率/显存。

这些指标大多仍是 counter/gauge/histogram，Prometheus 模型适用；挑战在基数（用户/会话/模型版本）和成本。

## 30.7 不会被取代的部分

- **多维标签模型**：仍然是指标查询的核心抽象。
- **PromQL**：仍然是事实标准，生态/人才/工具围绕它。
- **pull + exposition**：仍然是简单可靠的采集方式。
- **本地 TSDB + remote write**：仍然是边缘/中心分层的基础。
- **Alertmanager 语义**：分组/抑制/静默仍然是告警治理的核心。

新技术会改变实现/规模，但这些语义短期不会消失。

## 30.8 对平台团队的建议

- 不要把"上 OTel"当成"下 Prometheus"；
- 先治理基数/查询，再考虑换存储；
- 关注 native histogram/RW2 的生态成熟度，不要盲目追新；
- 保留 PromQL 兼容层（Grafana/Mimir/VictoriaMetrics 都支持）；
- 建立指标规范/评审/预算；
- 为 AI/LLM 负载设计新的 SLI/成本指标。

## 30.9 观察信号

- Prometheus release notes / dev summit；
- CNCF 可观测性项目 landscape；
- OTel semantic conventions 版本；
- 主流后端（Thanos/Mimir/VictoriaMetrics/云托管）对 native histogram/RW2 的支持；
- Grafana 对 PromQL/OTel 的支持；
- 社区关于 delta temporality/UTF-8 的讨论。

## 30.10 AI/LLM 指标的设计细节

LLM 服务给指标模型带来三类新维度：

- **用量/成本**：输入/输出 token、缓存命中、每请求成本、每租户成本；
- **延迟**：首 token 时间（TTFT）、每输出 token 时间（TPOT）、端到端延迟、排队时间；
- **质量/安全**：错误率、拒答率、幻觉/越狱检测、内容过滤命中。

一个可落地的指标集：

```text
llm_requests_total{model,operation,status}
llm_request_duration_seconds{model,operation}          # histogram
llm_time_to_first_token_seconds{model}                 # histogram
llm_tokens_total{model,direction}                      # input/output
llm_token_cost_total{model,tenant}                     # 需要定价表
llm_cache_hits_total{model}
llm_gpu_utilization_ratio{gpu}
```

设计原则：

- `model`/`operation` 有界；不要把 prompt/response/用户输入做标签；
- `tenant` 只在多租户/计费必要时使用，并控制基数；
- token 计数用 counter，成本用 counter（按定价换算）或 recording rule；
- TTFT/TPOT 用 histogram，桶覆盖 SLO（如 100ms～30s）；
- 质量指标通常是采样/离线评估，不要假设每个请求都有；
- 成本指标要与账单口径一致（缓存、批处理、折扣）。

SLO 示例：

```promql
# 99% 请求 TTFT < 2s
histogram_quantile(0.99, sum by (le) (rate(llm_time_to_first_token_seconds_bucket[5m]))) < 2
```

AI 负载的特殊性：

- 流量突发/长尾明显，平均值没有意义；
- GPU 是稀缺资源，利用率/排队/显存要单独监控；
- prompt/response 可能含敏感信息，不能进指标/日志；
- 模型版本/供应商切换会影响延迟/成本/质量，标签要能区分；
- OTel GenAI 语义约定仍在演进，接入时固定版本/映射。

完整的 TTFT/TPOT 定义、vLLM/DCGM 指标、KV cache 容量算术、SLO/告警与 token 成本治理见附录 AI；跨行业（电商/支付/流媒体/SaaS/IoT）的指标与 SLO 案例见附录 AH。

### 30.10.1 训练集群与推理集群的监控差异

推理监控围绕"请求"（TTFT/TPOT、队列、KV cache、token 成本）；训练监控围绕"作业"（gang scheduling、step time、GPU 利用率、checkpoint、通信）。两者都用 Prometheus，但 SLI 和标签设计不同：

| 维度 | 推理 | 训练 |
|---|---|---|
| 核心 SLI | 延迟、goodput、错误率、token 成本 | step time、GPU 利用率、作业完成时间、有效算力 |
| 调度 | 请求队列/批处理 | gang scheduling、队列等待、抢占、重启 |
| GPU | 利用率、显存、KV cache | 利用率、ECC/XID、NVLink/NCCL、RDMA |
| 数据 | 请求体/响应体不进标签 | 数据加载吞吐、缓存命中、shuffle |
| 失败 | 超时/5xx/降级 | straggler、NCCL 错误、checkpoint 失败、节点故障 |
| 成本 | 每 token/每请求 | GPU-hours、作业利用率、空闲/排队 |

一组可落地的训练指标：

```text
training_job_info{job,team,priority,cluster}          # info 型，标签有界
training_step_duration_seconds{job}                   # histogram
training_tokens_per_second{job}                       # gauge
training_gpu_utilization_ratio{job,gpu}               # 来自 DCGM
training_gpu_memory_used_bytes{job,gpu}
training_nccl_errors_total{job}                       # counter
training_checkpoint_duration_seconds{job}             # histogram
training_checkpoint_failures_total{job}               # counter
training_queue_wait_seconds{job,priority}             # histogram
```

设计要点：

- `job` 标签不要用完整实验名/用户输入；用稳定的作业 ID + 有界的 `team`/`priority`/`cluster`，否则每次提交都产生新序列；
- step time 用 histogram 而不是只存平均，否则看不出长尾和 straggler；
- 训练作业生命周期短，指标要配合 Pushgateway/remote write/长期存储，避免作业结束后序列立刻消失；
- GPU 指标基数高（每卡、每 job），优先在 DCGM exporter/Collector 侧聚合或只保留关键卡；
- 训练和推理共用 Prometheus 时，用独立 job/租户和配额，避免训练作业的基数冲击推理告警。

## 30.11 技术雷达：什么时候跟进

把技术分成四类：

| 类别 | 行动 | 例子 |
|---|---|---|
| 采用 | 现在就用 | PromQL、exposition、remote write 1.0、rules、native histogram（3.8+）、exemplar（较新 3.x 默认，旧版本确认 flag） |
| 试验 | 小范围验证 | RW2（rc.4）、OTLP receiver、delta 转换、NHCB |
| 观察 | 关注生态 | eBPF 零侵入、AI 语义约定、新存储后端、`smoothed`/`anchored` 的规则用法 |
| 暂缓 | 不追新 | 未稳定的实验特性、单一厂商私有协议 |

跟进信号：

- 上游 release notes/roadmap；
- 客户端/Collector/长期存储的支持矩阵；
- 社区 issue/PR 的活跃度；
- 生产案例/故障报告；
- 你的查询/存储/告警链路是否端到端支持。

原则：

- 先治理基数/查询/成本，再换存储/协议；
- 新特性要有回滚路径；
- 不要为了"技术先进"牺牲可观测性本身的可靠性；
- 用 POC + 基准（第 48 章）验证，不用厂商 demo 下结论。

## 本章小结

- 已确定的方向：native histogram、RW2、OTLP、UTF-8、delta、agent。
- OTel 与 Prometheus 是互补而非替代；Collector 是融合层。
- eBPF/AI 负载带来新指标与新基数挑战；推理看请求，训练看作业/GPU/通信。
- 多维模型/PromQL/pull/本地 TSDB/Alertmanager 语义仍是长期基础。
- 平台团队应先治理基数/查询，再评估存储演进。

## 延伸阅读

- Prometheus roadmap/release notes。
- OpenTelemetry 语义约定与 Collector 文档。
- CNCF 可观测性 landscape。
- 本教程第 20 章（3.x/OTel）、第 50～51 章（综合实战）与第 52～56 章（商业）。
