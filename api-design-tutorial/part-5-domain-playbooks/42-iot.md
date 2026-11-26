# 第 42 章 IoT 与设备 API

## 42.1 IoT 的特殊约束

IoT API 面对的不是数据中心里的稳定服务，而是：

- 海量设备；
- 弱网/断网；
- 低功耗；
- 有限算力；
- 长时间离线；
- 固件升级；
- 物理安全风险；
- 生命周期长达十年。

因此，IoT API 的设计重点不是“REST 好不好看”，而是：

- 身份与信任；
- 离线与重连；
- 消息模型；
- 幂等与顺序；
- 固件升级；
- 设备生命周期；
- 可观测性；
- 成本。

### 与传统 API 的差异

| 维度 | 传统 API | IoT API |
|---|---|---|
| 消费者 | 服务/应用 | 设备 |
| 网络 | 稳定 | 弱网/断网 |
| 身份 | 令牌 | 证书/安全元件 |
| 消息 | 请求—响应 | 发布/订阅 |
| 顺序 | 通常不要求 | 可能要求 |
| 生命周期 | 短 | 十年 |
| 安全 | 服务器 | 物理可接触 |

## 42.2 设备模型

```text
Device
├── deviceId
├── productId
├── firmwareVersion
├── status
├── lastSeenAt
├── tags
└── credentials

Product
├── model
├── capabilities
├── telemetry schema
└── firmware

Fleet
├── devices
├── policies
└── rollout
```

### 设备身份

- 一机一密；
- X.509 证书；
- 安全元件（SE/TEE）；
- 首次启动注册；
- 信任根；
- 吊销；
- 轮换。

不要用可猜测的设备 ID + 共享密钥。

### 设备状态

不要把“生命周期状态”和“连接状态”画成一条链。它们是两个维度：

**生命周期（lifecycle）**：

```text
PROVISIONED → ACTIVE → MAINTENANCE → RETIRED
                    └──────────────→ RETIRED（直接退役）
```

| 状态 | 含义 |
|---|---|
| `PROVISIONED` | 已注册，尚未激活或首次上线 |
| `ACTIVE` | 已激活，允许连接与上报 |
| `MAINTENANCE` | 维护中，限制命令或升级 |
| `RETIRED` | 已退役，凭证吊销，不再计费 |

**连接状态（connectivity）**：

```text
ONLINE ⇄ OFFLINE ⇄ DEGRADED
```

| 状态 | 含义 |
|---|---|
| `ONLINE` | 最近有心跳/消息，连接可用 |
| `OFFLINE` | 超过心跳窗口未收到消息 |
| `DEGRADED` | 可连接但质量差（高延迟、丢包、信号弱） |

生命周期是持久状态，连接状态会频繁变化。API 应分别暴露，例如：

```json
{
  "id": "dev_123",
  "lifecycle": "ACTIVE",
  "connectivity": "OFFLINE",
  "lastSeenAt": "2026-11-20T08:00:00Z"
}
```

## 42.3 设备接入

### 协议

- MQTT：发布/订阅、低带宽、QoS；
- CoAP：UDP、低功耗；
- LwM2M：设备管理；
- HTTP：简单但开销大；
- WebSocket：双向；
- 自定义 TCP/UDP。

### 接入流程

```text
1. 设备出厂预置身份
2. 首次连接认证
3. 注册/激活
4. 下发配置
5. 上报遥测
6. 接收命令
7. 固件升级
```

### MQTT 主题设计

```text
devices/{deviceId}/telemetry
devices/{deviceId}/commands
devices/{deviceId}/events
devices/{deviceId}/status
```

主题设计要考虑：

- 租户；
- 产品；
- 设备；
- 权限；
- 通配符；
- 保留消息。

### MQTT 主题与权限模型

主题不仅是字符串，也是权限边界。推荐结构：

```text
tenants/{tenantId}/products/{productId}/devices/{deviceId}/telemetry
tenants/{tenantId}/products/{productId}/devices/{deviceId}/commands
tenants/{tenantId}/products/{productId}/devices/{deviceId}/events
tenants/{tenantId}/products/{productId}/devices/{deviceId}/status
```

权限：

- 设备只能发布自己的 `telemetry/events/status`；
- 设备只能订阅自己的 `commands`；
- 服务端可以订阅 `tenants/{tenantId}/#`，但不能跨租户；
- 通配符 `#`/`+` 只授予受控服务，不授予设备；
- broker 的 ACL 必须与租户/产品绑定，不能只按用户名；
- 保留消息适合“最后状态”，不适合命令或敏感数据；
- 主题层级要稳定，设备固件升级不应要求改主题。

不要用 `devices/{deviceId}/#` 作为设备权限，否则设备可以订阅自己的命令并伪造其他主题。

## 42.4 遥测

```json
{
  "deviceId": "dev_123",
  "timestamp": "2026-11-26T10:00:00Z",
  "metrics": {
    "temperature": 23.5,
    "humidity": 45,
    "battery": 87
  }
}
```

设计要点：

- 批量上报；
- 压缩；
- 时间戳；
- 乱序；
- 去重；
- 保留策略；
- 聚合；
- 成本；
- 设备时间漂移。

### 时序数据

- 写入吞吐；
- 查询模式；
- 降采样；
- 冷热分层；
- 保留期；
- 标签基数；
- 乱序写入。

### 遥测的幂等

设备可能重发：

```json
{
  "deviceId": "dev_123",
  "sequence": 42,
  "timestamp": "2026-11-26T10:00:00Z",
  "metrics": {
    "temperature": 23.5,
    "humidity": 45,
    "battery": 87
  }
}
```

去重键优先使用：

- `deviceId + sequence`（设备端单调递增，最可靠）；
- 或服务端/协议层的消息 ID；
- `deviceId + timestamp` 只作为兜底，且要求设备时钟已同步；设备时钟漂移或重复时间戳会导致误去重。

如果设备可能重启并重置 sequence，使用 `deviceId + bootId + sequence`，或由服务端分配单调序号。

## 42.5 命令

```text
POST /devices/{deviceId}/commands
```

```json
{
  "command": "set_target_temperature",
  "parameters": { "value": 22 },
  "expiresAt": "2026-11-26T10:05:00Z",
  "idempotencyKey": "..."
}
```

### 命令语义

- 立即执行 vs 排队；
- 离线设备如何处理；
- 过期；
- 重试；
- 幂等；
- 确认；
- 结果上报；
- 超时。

命令不是“发出去就成功”。需要：

```text
PENDING → SENT → ACKED → SUCCEEDED
   │        │        └→ FAILED
   │        ├→ FAILED
   │        └→ EXPIRED（未在 TTL 内送达）
   └→ EXPIRED（入队后未发送就过期）
```

| 状态 | 含义 | 终态 |
|---|---|---|
| `PENDING` | 已接受，等待下发 | 否 |
| `SENT` | 已发布到设备主题 | 否 |
| `ACKED` | 设备确认收到 | 否 |
| `SUCCEEDED` | 设备报告执行成功 | 是 |
| `FAILED` | 发送失败或设备报告失败 | 是 |
| `EXPIRED` | 超过 `expiresAt` 未完成 | 是 |

命令的“成功”必须以设备 ACK/结果为准，不能以 broker 接受消息为准。

### 离线命令

- 保留时间；
- 过期；
- 覆盖（只保留最新）；
- 队列；
- 设备上线后拉取。

### 命令的幂等

设备可能重复执行：

- 命令 ID；
- 设备端去重；
- 结果缓存；
- 幂等操作。

## 42.6 离线与重连

- 设备可能离线数天；
- 消息需要持久化；
- 重连后同步；
- 命令过期；
- 配置版本；
- 固件版本；
- 状态对账。

### 影子/设备孪生

维护设备期望状态与报告状态：

```json
{
  "desired": { "targetTemperature": 22 },
  "reported": { "targetTemperature": 21, "currentTemperature": 23 },
  "version": 42,
  "updatedAt": "..."
}
```

设备上线后同步差异。

### 影子的一致性

- 版本号；
- 冲突解决；
- 最后写入胜出 vs 期望状态优先；
- 离线期间的变更；
- 审计。

### 设备影子的收敛算法

影子不是“数据库里存两份 JSON”，而是一个收敛过程：

```text
desired = { targetTemperature: 22, mode: "eco" }
reported = { targetTemperature: 21, mode: "eco", currentTemperature: 23 }
version = 42
```

设备上线时：

1. 比较 `desired` 与 `reported`；
2. 只下发差异字段；
3. 设备应用后上报新的 `reported` 与 `version`；
4. 服务端确认收敛，更新影子版本；
5. 失败则按策略重试或标记 `DEGRADED`。

冲突规则要明确：

- **期望状态优先**：设备应最终服从 `desired`，`reported` 只是事实；
- **版本号/CAS**：更新 `desired` 带 `If-Match`，防止覆盖并发修改；
- **最后写入胜出**：简单但可能覆盖用户的离线设置，谨慎使用；
- **字段级合并**：适合多来源修改不同字段。

离线期间：

- `desired` 可以继续更新；
- 设备上线后按差异收敛；
- 过期/冲突的期望状态要有策略（最新优先、人工确认、回滚）；
- 所有变更留审计。

不要用“影子”掩盖设备真实状态。`reported` 必须来自设备，不能由服务端猜测。

## 42.7 固件升级（OTA）

```text
POST /firmware-rollouts
```

```json
{
  "firmwareId": "fw_123",
  "target": { "productId": "prod_1", "version": "<1.2.0" },
  "strategy": "canary",
  "batchSize": 100,
  "abortOnFailureRate": 0.05
}
```

流程：

1. 上传固件；
2. 签名；
3. 创建 rollout；
4. 分批下发；
5. 设备下载；
6. 校验；
7. 安装；
8. 重启；
9. 上报版本；
10. 失败回滚。

安全：

- 固件签名；
- 防回滚；
- 安全启动；
- 加密；
- 断电保护；
- 失败恢复；
- 分阶段；
- 灰度。

### OTA 的状态

```text
PENDING → DOWNLOADING → VERIFYING → INSTALLING → REBOOTING → SUCCESS
                │            │            │
                └→ FAILED    └→ FAILED    └→ FAILED
                                                  │
                                                  └→ ROLLED_BACK
```

### OTA 分批与失败预算

OTA 不是“推送一个版本”，而是一次受控发布：

```text
第 1 批：内部设备 / 实验室 1%
第 2 批：设计伙伴 / 早期用户 5%
第 3 批：普通设备 20%
第 4 批：剩余设备
```

每批的进入条件：

- 下载成功率 > 99%；
- 安装成功率 > 98%；
- 启动后心跳恢复率 > 99%；
- 无严重故障/变砖报告；
- 关键指标（崩溃率、重启率、版本回退率）在阈值内。

失败预算：

```text
abortOnFailureRate = 0.05
```

达到阈值时：

- 停止后续批次；
- 保留已升级设备；
- 对失败设备重试或回滚；
- 分析是固件、网络、硬件还是分发问题；
- 通知客户与支持。

安全要求：

- 固件签名与安全启动；
- 防回滚（anti-rollback）；
- 断电保护与失败恢复；
- 下载完整性校验；
- 灰度与可回滚；
- 设备端保留旧版本分区（A/B 升级）。

没有失败预算的 OTA，会把一次固件缺陷放大成整批设备变砖。

## 42.8 设备生命周期

```text
制造 → 注册 → 激活 → 运行 → 维护 → 退役
```

需要：

- 设备注册；
- 激活；
- 凭证轮换；
- 固件更新；
- 故障诊断；
- 退役/擦除；
- 数据保留；
- 所有权转移。

### 退役

- 吊销凭证；
- 停止计费；
- 保留数据（合规）；
- 擦除设备；
- 通知；
- 审计。

## 42.9 安全

- 设备身份；
- mTLS；
- 证书轮换；
- 最小权限；
- 命令授权；
- 固件签名；
- 安全启动；
- 数据加密；
- 防重放；
- 速率限制；
- 异常检测。

设备可能被物理捕获，必须假设攻击者能读取密钥。

### 物理安全

- 安全元件；
- 密钥不可导出；
- 防篡改；
- 安全启动；
- 调试接口关闭；
- 固件签名。

## 42.10 可观测性

- 在线/离线设备数；
- 连接质量；
- 消息延迟；
- 命令成功率；
- 固件版本分布；
- 电池/信号；
- 异常设备；
- 每产品/区域指标；
- 连接抖动；
- 消息积压。

## 42.11 成本

- 连接数；
- 消息数；
- 存储；
- 查询；
- 出口流量；
- 固件分发；
- 支持。

海量设备下，单条消息成本会被放大。

### 成本优化

- 批量上报；
- 压缩；
- 降采样；
- 冷热分层；
- 边缘聚合；
- 按需查询；
- 生命周期。

## 42.12 本章小结

- IoT API 的核心是身份、离线、消息、命令、OTA 和生命周期。
- 设备身份必须强，不能依赖可猜测 ID。
- 遥测要考虑批量、乱序、去重、时序存储和成本。
- 命令需要状态机、过期、幂等和确认。
- 设备孪生解决离线状态同步。
- OTA 需要签名、分批、回滚和安全启动。
- 安全假设设备可能被物理攻破。
- OPC UA、MQTT Sparkplug B、汽车 VSS/SOME/IP 的协议细节与命令/OTA 模式，见[附录 AI](../appendices/ai-vertical-apis.md)；设备时间戳、单位与标识的语义，见[附录 AH](../appendices/ah-primitive-semantics.md)。

## 设计练习

为“智能家居平台”设计 API：

- 设备注册/激活；
- 遥测；
- 远程控制；
- 离线；
- 固件升级；
- 家庭共享；
- 隐私。

写出设备状态机、命令协议和 OTA 流程。然后回答：

1. 设备离线一周后上线，命令如何处理？
2. 家庭共享如何授权？
3. 固件升级失败如何回滚？
4. 如何防止设备被克隆？

---

上一章：[SaaS 平台 API](41-saas.md)  
下一章：[数据平台与分析 API](43-data-platform.md)
