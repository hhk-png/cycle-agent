# 附录 AH 多行业监控指标体系案例库

> 第 1～12 章讲通用语义，第 50 章用一个订单系统把设计流程走通。本附录补足"技术广度"：把同一套方法应用到交易、支付、流媒体、SaaS、IoT 与 AI 推理六类负载，给出 SLI、指标集、PromQL、告警、基数预算与行业特有的坑。案例是设计起点，不是可以直接复制的生产清单；每个组织都要用自己的用户旅程与约束替换假设。

## AH.1 使用方法：从用户旅程到指标

不要从"有哪些指标"开始，按下面的顺序：

```text
用户目标 → 用户旅程 → 失败模式 → SLI（good/total） → 指标类型/标签
        → 告警（症状而非原因） → 基数预算 → 所有权/评审
```

四个检查点：

1. **分母写清楚**：是否包含健康检查、机器人、重试、内部调用？分母漂移会让 SLO 失真。
2. **在边缘测用户感受**：网关/播放器/设备端的失败，才是用户看到的失败；内部 5xx 可能被重试吞掉。
3. **指标类型匹配语义**：counter 用 rate，gauge 用 delta/avg，分布用 histogram，不要用平均值代替长尾。
4. **标签是预算**：每个标签都要问"它能把基数乘多少"；用户/设备/订单 ID 一律不进标签。

SLI 的六种原型：

| 原型 | good / total | 例子 | 常见错误 |
|---|---|---|---|
| 可用性 | 非 5xx / 全部 | HTTP 成功率 | 把 3xx/4xx 都算失败 |
| 延迟 | 低于阈值 / 全部 | `duration < 300ms` | 对 P99 设阈值而不是好请求比例 |
| 质量 | 成功播放/成功支付 / 尝试 | QoE、支付成功率 | 分母混入重试 |
| 新鲜度 | 在时限内到达 / 全部 | 订单终态、遥测延迟 | 用处理时间代替事件时间 |
| 饱和度 | 低于阈值的时间占比 | 队列深度/容量 | 只看平均值，不看峰值 |
| 正确性 | 对账一致 / 全部 | 账本、库存 | 用工程指标冒充业务正确性 |

## AH.2 电商与交易

### AH.2.1 用户旅程与失败模式

```text
浏览 → 搜索 → 商品页 → 加购 → 结算 → 支付 → 履约 → 售后
```

| 阶段 | 用户看到的失败 | 工程信号 |
|---|---|---|
| 搜索 | 无结果、超时 | 搜索延迟、零结果率、索引新鲜度 |
| 商品页 | 白屏、价格错误 | 5xx、缓存命中、价格服务错误 |
| 加购 | 加购失败、库存误判 | 购物车写失败、库存预留失败 |
| 结算 | 下单失败、重复下单 | 订单创建错误、幂等冲突 |
| 支付 | 支付失败、扣款未到账 | 支付成功率、回调延迟、对账差异 |
| 履约 | 长时间不发货 | 订单状态停留、履约队列积压 |

### AH.2.2 指标集

```text
# 请求层（RED）
shop_http_requests_total{service,route,method,status_class}
shop_http_request_duration_seconds{service,route,method}      # histogram
shop_http_inflight_requests{service}

# 业务事件
shop_events_total{event,result}        # event: view/search/add_to_cart/checkout/order_created
shop_orders_created_total{channel,region}
shop_order_state_transitions_total{from,to,result}
shop_order_state_age_seconds{state}    # 订单在状态上的停留时间
shop_payment_attempts_total{provider,result}
shop_inventory_reservations_total{result}
shop_inventory_oversell_total

# 依赖/饱和度
shop_search_query_duration_seconds{index}
shop_search_zero_results_total
shop_cache_hits_total{cache}
shop_queue_depth{queue}
```

标签预算示例：

| 标签 | 取值 | 说明 |
|---|---|---|
| `service` | 5～10 | 稳定服务名 |
| `route` | 20～50 | 模板化路径，禁止 ID |
| `method` | 5 | GET/POST/… |
| `status_class` | 5 | `2xx/3xx/4xx/5xx`，不要每个码 |
| `channel` | 5 | web/app/mini |
| `region` | 3 | |
| `event` | 20 | 有界业务事件 |
| `provider` | 5 | 支付渠道 |

`order_id`、`user_id`、`sku_id`、`cart_id` 一律不进标签；需要下钻时用日志/追踪或按需的临时查询。

### AH.2.3 PromQL

```promql
# 结算成功率（按渠道）
sum by (channel) (rate(shop_events_total{event="checkout",result="success"}[5m]))
/
sum by (channel) (rate(shop_events_total{event="checkout"}[5m]))

# 支付成功率（排除用户取消）
sum by (provider) (rate(shop_payment_attempts_total{result="success"}[5m]))
/
sum by (provider) (rate(shop_payment_attempts_total{result=~"success|failed"}[5m]))

# 搜索 P95
histogram_quantile(0.95,
  sum by (le, index) (rate(shop_search_query_duration_seconds_bucket[5m])))

# 订单状态停留过久（需要事件时间/状态时间指标）
max by (state) (shop_order_state_age_seconds) > 600

# 库存预留失败率
sum(rate(shop_inventory_reservations_total{result="failed"}[5m]))
/
sum(rate(shop_inventory_reservations_total[5m]))
```

### AH.2.4 告警

- 结算成功率 SLO burn（多窗口）；
- 支付渠道成功率骤降/单渠道不可用；
- 订单状态在 `paid` 停留超过履约 SLA；
- 库存超卖（`oversell_total > 0` 立即 page）；
- 搜索零结果率异常（可能是索引/分词发布事故）。

### AH.2.5 行业坑

- **重试双计**：支付/下单重试会让成功率分母膨胀；用幂等键去重或在网关层计数。
- **渠道口径**：不同支付渠道的"成功"定义不同（授权成功 ≠ 扣款成功）；标签要区分阶段。
- **时区**：日切、结算、报表按业务时区，Prometheus 时间函数按 UTC。
- **大促**：流量形态变化会让历史阈值失效；用同比/环比与 burn rate，而不是固定 QPS。

## AH.3 支付与金融

### AH.3.1 用户旅程与失败模式

```text
发起支付 → 风控 → 授权 → 捕获 → 清算 → 结算 → 对账 → 退款/拒付
```

| 阶段 | 失败模式 | 指标 |
|---|---|---|
| 授权 | 渠道超时、拒付、3DS 失败 | 授权成功率、延迟、错误码 |
| 捕获 | 部分捕获、重复捕获 | 捕获成功率、幂等冲突 |
| 清算 | 文件延迟、格式错误 | 清算文件年龄、解析失败 |
| 结算 | 到账延迟、金额不符 | 结算 lag、差异金额 |
| 对账 | 账本不平、重复入账 | 对账差异条数/金额 |
| 退款/拒付 | 退款失败、拒付率上升 | 退款成功率、拒付率 |

### AH.3.2 指标集

```text
pay_auth_requests_total{provider,card_network,result,error_type}
pay_auth_duration_seconds{provider,card_network}              # histogram
pay_capture_requests_total{provider,result}
pay_settlement_age_seconds{provider,currency}                  # gauge
pay_settlement_files_total{provider,status}
pay_ledger_entries_total{entry_type,status}
pay_ledger_reconciliation_diff_amount{currency,provider}
pay_refunds_total{provider,result}
pay_chargebacks_total{provider,reason}
pay_fraud_decisions_total{decision,rule_bucket}
```

注意：

- `card_network`（Visa/Mastercard/…）是有界枚举；**卡号、持卡人、CVV、账单地址永远不进指标**；
- `currency` 有界（几十种），但金额类指标要带币种，不能直接相加；
- `error_type` 用分类（timeout/declined/network/3ds），不要放渠道原始错误文本；
- `fraud_decisions` 的 `rule_bucket` 要粗粒度，否则规则 ID 会爆炸。

### AH.3.3 PromQL

```promql
# 授权成功率（按渠道；declined 是否计入分母取决于业务定义）
sum by (provider) (rate(pay_auth_requests_total{result="approved"}[5m]))
/
sum by (provider) (rate(pay_auth_requests_total{result=~"approved|declined|error"}[5m]))

# 授权 P99 延迟
histogram_quantile(0.99,
  sum by (le, provider) (rate(pay_auth_duration_seconds_bucket[5m])))

# 结算 lag（秒）
max by (provider, currency) (pay_settlement_age_seconds)

# 对账差异（金额）
sum by (currency) (pay_ledger_reconciliation_diff_amount) != 0

# 拒付率
sum by (provider) (rate(pay_chargebacks_total[1d]))
/
sum by (provider) (rate(pay_auth_requests_total{result="approved"}[1d]))
```

### AH.3.4 告警

- 单渠道授权成功率低于基线（可能是渠道故障/风控误杀）；
- 授权 P99 超过 SLO；
- 结算文件超过时限未到/解析失败；
- 对账差异金额 ≠ 0（按严重级别分级）；
- 拒付率突增（可能是欺诈/产品问题）；
- 账本重复入账/负余额（正确性告警，优先于性能）。

### AH.3.5 行业坑

- **金额精度**：指标是 float64，金额不要用浮点累计；用最小货币单位（分）的整数 counter，或由账本系统导出。
- **幂等与重试**：同一笔支付的重试要按业务键去重，否则成功率/延迟被稀释。
- **合规**：PCI DSS 范围、数据驻留、审计保留；指标系统不能成为敏感数据的旁路。
- **多币种**：不要对跨币种金额做 `sum`；先按币种聚合，再按汇率换算（如果需要）。
- **对账时间窗**：T+1 对账与实时指标的口径不同，告警要区分"实时异常"与"日终差异"。

## AH.4 流媒体与内容平台

### AH.4.1 用户旅程与失败模式

```text
打开 App → 获取 manifest → 起播 → 稳态播放 → seek/切码率 → 结束
```

| 阶段 | 用户看到的失败 | 指标 |
|---|---|---|
| 打开 | 首页加载慢/白屏 | 首页延迟、API 错误 |
| manifest | 无法获取播放列表 | manifest 错误、CDN 5xx |
| 起播 | 黑屏/转圈 | 起播时间、起播失败率 |
| 稳态 | 卡顿/降码率 | rebuffer 比、bitrate、丢帧 |
| seek | 卡顿/失败 | seek 延迟、失败率 |
| DRM | 无法解密 | license 失败、DRM 错误 |

### AH.4.2 指标集

```text
video_playback_start_seconds{player,region,network_type}       # histogram
video_playback_starts_total{player,cdn}
video_playback_start_failures_total{player,error_type}
video_rebuffer_seconds_total{player,region,cdn}
video_watch_seconds_total{player,region,cdn}
video_bitrate_bps{player,region,cdn}                           # gauge/histogram
video_playback_errors_total{player,error_type,cdn}
video_seek_duration_seconds{player}
video_drm_license_failures_total{drm_system,error_type}
cdn_requests_total{pop,status_class}
cdn_cache_hits_total{pop}
cdn_origin_requests_total{pop}
video_concurrent_streams{region}
```

关键比率：

```promql
# rebuffer 比（卡顿时间 / 观看时间）
sum by (region) (rate(video_rebuffer_seconds_total[5m]))
/
sum by (region) (rate(video_watch_seconds_total[5m]))

# 起播 P95
histogram_quantile(0.95,
  sum by (le, region) (rate(video_playback_start_seconds_bucket[5m])))

# 播放错误率（错误 / 起播尝试）
sum by (cdn) (rate(video_playback_errors_total[5m]))
/
sum by (cdn) (rate(video_playback_starts_total[5m]))

# CDN 5xx 比例
sum by (cdn) (rate(cdn_requests_total{status_class="5xx"}[5m]))
/
sum by (cdn) (rate(cdn_requests_total[5m]))

# CDN 命中率
sum by (pop) (rate(cdn_cache_hits_total[5m]))
/
sum by (pop) (rate(cdn_requests_total[5m]))
```

### AH.4.3 告警

- 起播时间 SLO burn（按地区/运营商）；
- rebuffer 比突增（CDN/源站/编码问题）；
- 单 CDN/POP 5xx 或命中率骤降；
- DRM license 失败率；
- 并发流突增导致源站/转码容量不足。

### AH.4.4 行业坑

- **设备/会话 ID 高基数**：`device_id`/`session_id` 不进标签；用 `player`/`region`/`network_type` 等有界维度。
- **CDN POP 基数**：POP 数量可控但可能上百；按需保留，或用 `cdn` + `region` 聚合。
- **观看时长分母**：只统计有效播放（排除 <5s 的误触），否则 rebuffer 比失真。
- **离线下载/投屏**：与在线播放的 QoE 口径不同，标签要能区分。
- **长尾设备**：低端设备/弱网的长尾严重，平均值会掩盖问题；用分位数和分桶。

## AH.5 SaaS 多租户

### AH.5.1 用户旅程与失败模式

```text
注册 → 首次配置 → API/控制台使用 → 集成/导出 → 计费/续费
```

| 阶段 | 失败模式 | 指标 |
|---|---|---|
| 注册 | 验证邮件延迟、注册失败 | 注册成功率、延迟 |
| 首次配置 | onboarding 卡住 | 完成率、步骤耗时 |
| API | 限流、超时、5xx | 可用性、延迟、配额拒绝 |
| 导出 | 大查询超时、文件失败 | 导出成功率、耗时 |
| 计费 | 用量漏记/重复 | 计量事件延迟、差异 |

### AH.5.2 指标集

```text
saas_api_requests_total{tier,route,status_class}
saas_api_request_duration_seconds{tier,route}                  # histogram
saas_quota_rejections_total{tier,reason}
saas_onboarding_events_total{step,result}
saas_export_jobs_total{result}
saas_export_duration_seconds{format}
saas_usage_events_total{meter,status}
saas_usage_pipeline_lag_seconds{meter}
saas_active_tenants{tier}
```

不要用 `tenant_id` 做标签：租户数可能上万，且泄露客户信息。用 `tier`（free/pro/enterprise）或有界的 `plan`；需要单租户排障时，用日志/追踪或按需的临时导出。

### AH.5.3 PromQL

```promql
# 按 tier 的 API 可用性（4xx 是用户错误，通常不计入分母）
sum by (tier) (rate(saas_api_requests_total{status_class=~"2xx|3xx"}[5m]))
/
sum by (tier) (rate(saas_api_requests_total{status_class=~"2xx|3xx|5xx"}[5m]))

# 配额拒绝率（可能是滥用或额度设置错误）
sum by (tier, reason) (rate(saas_quota_rejections_total[5m]))
/
sum by (tier) (rate(saas_api_requests_total[5m]))

# 计量管道延迟
max by (meter) (saas_usage_pipeline_lag_seconds)

# 导出失败率
sum by (format) (rate(saas_export_jobs_total{result="failed"}[5m]))
/
sum by (format) (rate(saas_export_jobs_total[5m]))
```

### AH.5.4 告警

- 按 tier 的 SLO burn（免费/付费分级）；
- 配额拒绝率异常（可能是攻击/配置错误）；
- 计量管道 lag 超过计费窗口；
- 导出任务积压/失败；
- onboarding 转化率骤降（业务指标，非 page 级）。

### AH.5.5 行业坑

- **噪声邻居**：单租户的高基数/大查询会拖垮共享实例；用配额、查询超时、独立读路径。
- **多租户隔离**：单机 Prometheus 没有租户概念；用独立实例或 Mimir 租户。
- **计量准确性**：计费指标要有对账与重放能力，不能只依赖 Prometheus 采样。
- **租户标签**：即使必须用，也要哈希/分桶，并设置基数上限。

## AH.6 IoT 与边缘

### AH.6.1 用户旅程与失败模式

```text
设备启动 → 联网/鉴权 → 遥测上报 → 命令下发 → OTA 升级
```

| 阶段 | 失败模式 | 指标 |
|---|---|---|
| 启动 | 启动失败/回滚 | 启动成功率、启动耗时 |
| 联网 | 离线、重连风暴 | 在线率、重连次数 |
| 遥测 | 延迟、丢失、时钟漂移 | 遥测年龄、消息失败率 |
| 命令 | 超时、重复执行 | 命令成功率/延迟、幂等冲突 |
| OTA | 升级失败、变砖 | OTA 成功率、回滚率 |

### AH.6.2 指标集

```text
iot_devices_online{fleet,region,firmware}
iot_devices_total{fleet,region,firmware}
iot_telemetry_age_seconds{fleet,region}                        # gauge，fleet 级最新遥测年龄
iot_devices_stale{fleet,region}                                # gauge，超过新鲜度阈值的设备数
iot_messages_total{protocol,result}
iot_message_lag_seconds{protocol,region}                       # histogram
iot_command_requests_total{command,result}
iot_command_duration_seconds{command}                          # histogram
iot_ota_jobs_total{result,firmware}
iot_battery_ratio{fleet}
iot_connection_errors_total{protocol,error_type}
```

### AH.6.3 PromQL

```promql
# 在线率（按 fleet）
sum by (fleet) (iot_devices_online)
/
sum by (fleet) (iot_devices_total)

# 遥测新鲜度（超过 5 分钟的设备比例；stale 由 exporter/边缘聚合）
sum by (fleet) (iot_devices_stale)
/
sum by (fleet) (iot_devices_total)

# 最新遥测年龄（fleet 级）
max by (fleet) (iot_telemetry_age_seconds)

# 命令成功率
sum by (command) (rate(iot_command_requests_total{result="success"}[5m]))
/
sum by (command) (rate(iot_command_requests_total[5m]))

# OTA 失败率
sum by (firmware) (rate(iot_ota_jobs_total{result="failed"}[5m]))
/
sum by (firmware) (rate(iot_ota_jobs_total[5m]))
```

### AH.6.4 告警

- 整批设备离线（按 fleet/region）；
- 遥测延迟超过业务时限；
- 命令失败率骤增（可能是协议/鉴权/网络）；
- OTA 失败率/回滚率超阈值；
- 重连风暴（可能压垮接入层）。

### AH.6.5 行业坑

- **设备 ID 高基数**：`device_id` 绝不进标签；用 fleet/region/firmware/硬件型号等有界维度。
- **固件版本基数**：版本数可能上千；只保留主版本/发布通道，或定期清理。
- **间歇连接**：设备离线是常态，staleness/`up` 语义要按"业务在线"重新定义；
- **时钟漂移**：设备时间不可信；用 `honor_timestamps: false` 或服务端打时间戳。
- **边缘架构**：边缘 Prometheus Agent + 中心 remote write；本地保留短、中心保留长；断网时要有磁盘缓冲与重放。
- **OTA 回滚**：升级失败可能让设备永久离线，告警要能区分"升级中"与"升级后失联"。

## AH.7 AI/LLM 推理

LLM 推理的 SLI、指标、成本与告警单独放在附录 AI，因为它的度量对象（token、TTFT、KV cache、GPU）与传统业务差异较大。这里只给一个跨行业对照：

| 维度 | 传统 Web | LLM 推理 |
|---|---|---|
| 请求单位 | request | request + token |
| 延迟 | 端到端 | TTFT + TPOT/ITL |
| 饱和度 | QPS/连接数 | 队列 + KV cache + GPU |
| 成本 | 实例/带宽 | token + GPU 秒 |
| 质量 | 5xx/业务成功 | 拒答/幻觉/内容过滤 |

## AH.8 跨行业模式与反模式

### AH.8.1 可复用的模式

- **RED**：Rate、Errors、Duration，适合请求型服务；
- **USE**：Utilization、Saturation、Errors，适合资源；
- **黄金信号**：延迟、流量、错误、饱和度；
- **业务事件**：把"用户完成了什么"变成 counter，而不是只看 HTTP；
- **新鲜度**：对批处理/流/设备，用"事件时间与现在的时间差"；
- **正确性**：对账、库存、账本，用独立的 correctness 指标；
- **成本**：token/GB/请求数，按租户/模型/渠道分摊。

### AH.8.2 常见反模式

| 反模式 | 后果 | 修复 |
|---|---|---|
| 用平均延迟 | 长尾被掩盖 | 好请求比例/分位数 |
| 用 uptime 代替可用性 | 进程活着但用户不能用 | 在边缘测成功率 |
| 分母混入重试/健康检查 | SLO 虚高 | 固定分母与排除规则 |
| 把 ID 放进标签 | 基数爆炸/泄露 | 日志/追踪/分桶 |
| 只告警原因不告警症状 | 用户先发现故障 | 以 SLO burn 为主 |
| 业务指标没有 owner | 坏了没人修 | 指标契约 + RACI |
| 告警阈值写死 | 流量变化后失效 | burn rate/同比 |
| 只看单实例 | 聚合掩盖故障 | 按 job/region/version 分层 |

## AH.9 基数预算对照

| 行业 | 高风险标签 | 预算建议 | 替代 |
|---|---|---|---|
| 电商 | order_id/user_id/sku_id | 每服务 ≤ 5 万活跃序列 | 日志/追踪、离线分析 |
| 支付 | card/account/error text | 每指标 ≤ 1 万 | 分桶/分类/账本系统 |
| 流媒体 | session/device/CDN POP | 每指标 ≤ 10 万 | player/region/cdn 聚合 |
| SaaS | tenant_id/route 原始路径 | 每 tier ≤ 5 万 | tier/plan、模板化 route |
| IoT | device_id/firmware 全量 | 每 fleet ≤ 20 万 | fleet/region/主版本 |
| LLM | tenant_id/prompt/session | 每模型 ≤ 5 万 | tier/model/route |

预算不是硬上限，而是评审触发器：超过时先问"这个维度真的需要进指标吗"。

## AH.10 告警与 SLO 对照

| 行业 | 主 SLI | 典型 SLO | Page 级告警 |
|---|---|---|---|
| 电商 | 结算成功率、支付成功率 | 99.9% / 99.5% | SLO burn、支付渠道故障、超卖 |
| 支付 | 授权成功率、结算 lag | 99.95% / lag < 5m | 渠道成功率、对账差异、重复入账 |
| 流媒体 | 起播时间、rebuffer 比 | P95 < 2s / < 1% | 起播 burn、CDN 5xx、DRM 失败 |
| SaaS | API 可用性/延迟 | 99.9% / P99 < 500ms | tier burn、配额异常、计量 lag |
| IoT | 在线率、遥测新鲜度 | 99% / < 5m | 批量离线、遥测 lag、OTA 失败 |
| LLM | TTFT、TPOT、成功率 | P99 TTFT < 2s | TTFT burn、队列/KV cache、GPU XID |

告警设计原则：

- page 级只覆盖"用户正在受损且需要人立即行动"；
- 原因类告警（CPU、磁盘、队列）降级为 ticket/看板，除非已证明与用户损害强相关；
- 每个 page 告警必须有 runbook、owner、抑制规则；
- 用多窗口 burn rate 降噪，低流量场景用绝对错误数兜底。

## AH.11 练习：给你自己的系统做一次

选一个你熟悉的业务，完成下面模板：

```text
1. 用户旅程（5～8 步）：
2. 每步的失败模式（至少 2 个）：
3. 3 个 SLI（good/total，写清分母）：
4. 8 个指标（名称/类型/单位/标签/基数预算）：
5. 2 个 page 告警 + 2 个 ticket 告警（含 runbook）：
6. 1 个高基数风险与替代方案：
7. 1 个业务正确性指标（对账/库存/计量）：
```

评审时重点看：

- 指标能否回答"哪个用户/哪类请求/哪个版本受影响"；
- 告警是否 symptoms-first；
- 分母是否可稳定计算；
- 标签是否会把基数乘爆；
- 是否有 owner 和弃用策略。

## AH.12 延伸阅读

- 本教程第 3 章（数据模型/基数）、第 4 章（类型）、第 25～27 章（SLO/告警/成本）、第 50～51 章（综合实战）。
- 附录 C（规则库）、D（命名）、Z（治理清单）、AI（LLM 推理监控）。
- Google SRE Workbook 的 SLO 章节；各行业公开的 SRE/可观测性案例。
