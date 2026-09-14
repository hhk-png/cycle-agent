仓库地址：https://github.com/hhk-png/cycle-agent

# 第 17 章：生产部署与安全

> 本章目标：把前面十几章的代码变成"能上线、能升级、出事能查"的系统。
> 五块内容：**部署形态怎么选**（§17.1–17.2）、**资源与可观测性怎么接**
> （§17.3–17.4）、**安全这个真实短板怎么补**（§17.5）、
> **多租户与配额**（§17.9）、**非 x86 架构与 Windows 的边界**（§17.10）。
> 中间还有升级流程（§17.6）、成本清单（§17.7）和四份事故复盘（§17.8）。
> 安全那一节请务必读完 —— 这不是"最佳实践"级别的建议，
> 而是有 CISA 强制修复期限的真实漏洞。

---

## 17.1 部署形态：先选对形态，再谈优化

```
① 本地多进程     ray.init()                    单机，开发与调试
② 手动集群       ray start --head/--address    几台机器，小团队
③ Kubernetes     KubeRay（CRD）                弹性、多租户、生产
④ 托管控制面     Anyscale（商业）               不想自己运维控制面
```

| 维度 | ① 本地 | ② `ray start` | ③ KubeRay | ④ Anyscale |
|---|---|---|---|---|
| 节点规模 | 1 台 | 2–20 台 | 10–数千台 | 任意 |
| 弹性扩缩 | 无 | 手动 / autoscaler（要配云凭据） | **K8s + autoscaler v2** | 内置 |
| 故障恢复 | driver 退出即结束 | 手动 | pod 重启 + GCS FT | 托管 |
| 多租户 | 无 | 无 | NetworkPolicy + RBAC | 内置 |
| 运维成本 | 0 | 低 | **高**（懂两套系统） | 中（花钱换人） |
| 适合谁 | 开发、单机实验 | 固定规模小集群、内部工具 | 生产、需要弹性 | 不想自建控制面 |
| 不适合 | 任何生产 | 需要频繁伸缩 | 团队没人懂 K8s 与 Ray 排障 | 预算敏感 / 数据合规限制 |

选择路径：不需要弹性且规模固定 → ②（比 K8s 少一层）；需要弹性且已有成熟
K8s 平台团队 → ③；没有且愿意付费 → ④。

⚠️ **关于 ④ 的重要变化**：**2026-07-30，英国 AI 云厂商 Nscale 宣布签署收购
Anyscale 的最终协议**，预计 2026 下半年完成（需监管批准）。金额官方未披露
（媒体报道约 16.5 亿美元，**非官方数字**）。**Ray 开源项目不在交易范围内** ——
它 2025 年已归 PyTorch Foundation 治理。对本章的意义：选 ④ 时要**把"商业
控制面的供应商稳定性"当成风险项**写进决策文档，同时清楚"框架"和"托管平台"
是两件事。

> mini-ray 四种形态都不支持：没有跨机部署、dashboard、autoscaler、鉴权
> （README 的"明确不做"清单里都列了）。本章内容无法在 mini-ray 上验证。

## 17.2 KubeRay：CRD 家族与 v1.7.0

| CRD | 用途 | 关键字段 |
|---|---|---|
| `RayCluster` | 一个 Ray 集群（head + worker groups） | `headGroupSpec` / `workerGroupSpecs` / `spec.upgradeStrategy` |
| `RayJob` | 一次性 job，可选自动清理集群 | `entrypoint` / `shutdownAfterJobFinishes` / `deletionPolicy` |
| `RayService` | Serve 应用 + 零停机升级 | `serveConfigV2` / `upgradeStrategy` |
| `RayCronJob` | 按 cron 调度 RayJob | `schedule` / `timeZone` / `jobTemplate` |

**发布时间：2026-08-20**（rc.0 于 2026-08-14；上一个稳定版 v1.6.2 是
2026-06-18）。官方介绍文章《Introducing KubeRay v1.6 and v1.7》**把两个版本放在
一篇里介绍**（文章日期 2026-08-25），合计 381 commit、约 75 位贡献者 ——
⚠️ **这不代表两者同时发布**：v1.6.0 实际发布于 2026-03-19，比 v1.7.0 早五个月。
逐项看：

**RayCronJob** —— v1.6 起以 **alpha** 引入，**默认关闭**，需打开 `RayCronJob`
feature gate；v1.7 新增 **`timeZone` 字段**，不设时默认用 KubeRay controller
所在节点的本地时区；`jobTemplate` 里放标准 RayJob spec。

**History Server（beta）** —— 用途是**RayCluster 被删除之后仍能访问 job 与
集群日志**；用惰性加载 + LRU 缓存降低内存与启动时间；集成 Ray 认证 token；
支持自动 sidecar 注入。

**RayService 增量升级与回滚** —— `NewCluster`（蓝绿）在新集群就绪后原子切流量；
`NewClusterWithIncrementalUpgrade` 逐步扩容新集群 + 用 Gateway API / HTTPRoute
按 `stepSizePercent` / `intervalSeconds` 迁移流量，需要
`RayServiceIncrementalUpgrade` feature gate 才能设 `ClusterUpgradeOptions`
（`maxSurgePercent`、`stepSizePercent`、`intervalSeconds`、`gatewayClassName`）。

⚠️ **状态存疑，本书不断言**：官方 v1.7 介绍文章把它列为 **beta** 并说明支持
回滚（Ray 侧文档 PR #65249）；但检索到一条 KubeRay 提交 "Revert
'[RayService] Promote Incremental Upgrade Feature to Beta (#4599)' (#4602)"，
把该 feature gate 的默认值改回 `false`、pre-release 状态改回 **Alpha**。
**请以你实际使用的 KubeRay 版本里 CRD 与 feature gate 的默认值为准。**

**`spec.upgradeStrategy.type: Recreate`（RayCluster）** —— spec 变化时**删除并
重建所有 Pod**。为什么需要它：GitOps 工具（ArgoCD 之类）期望"改 spec → 重建"
的确定性行为，而 RayCluster 默认不是这个语义。**用 ArgoCD 管 RayCluster 的
团队应该优先看这条。**

**安全相关（v1.7 新增）** —— 原生 **NetworkPolicy** 支持（alpha）、
**cert-manager 自动 mTLS 证书管理**、**基于 Kubernetes RBAC 的 token 认证**。

**GCS 容错：内嵌 RocksDB 替代外部 Redis（alpha）** —— 从 **KubeRay v1.7 +
Ray 2.57.0** 起不再必须外部 Redis；打开 `GCSFaultToleranceEmbeddedStorage`
feature gate（**默认关闭**），设 `gcsFaultToleranceOptions.backend: rocksdb`
（Ray 侧对应 `RAY_gcs_storage` 环境变量，但**该变量的确切取值本书未确认** ——
附录 A 的 A.11 标注了同样的疑问，以你所用版本为准）。
Operator 自动创建 PVC `{cluster}-gcs-pvc`、
挂到 head Pod 的 `/data/gcs`、注入所需环境变量；worker Pod 会注入
`RAY_gcs_rpc_server_reconnect_timeout_s=600`。**限制必须知道**：alpha、
**仅 Linux**、**单写者**（同一时刻只能有一个 GCS 进程打开该库，默认
`ReadWriteOnce` + 单 head 副本保证这点）；PVC 默认**随集群被垃圾回收**，
要保留必须设 `deletionPolicy: Retain`。

**Autoscaler v2：优先级感知的 worker group 选择** —— 在 `workerGroupSpecs`
里加 `priority`（**值越大越先扩容**，默认 0）。Ray 侧实现是 PR #62997，
选择顺序是"利用率 → 可恢复可用性 → 用户优先级 → 云资源可用性"；近期分配失败
的节点类型进入**可恢复冷却**（默认 600 秒）。⚠️ **一个实际踩过的坑**：
PR #65244 修了一个 bug —— KubeRay 的 autoscaling-config 生成器
（`_node_type_from_group_spec`）**没有把 worker group 的 `priority` 透传给
autoscaler**，导致 `NodeTypeConfig.priority` 恒为 0，**你在 CRD 上设的 priority
完全无效**。看到"priority 设了没用"，先查这个。

**其他**：`RayJob` 的 SidecarMode retry（alpha，`SidecarSubmitterRestart`
feature gate，依赖 Ray 2.54.0 的错误处理与 K8s v1.35 的 ContainerRestartRules）、
`RayJobDeletionPolicy`（beta）、可定制 Ingress 选项（alpha）、
`RayMultiHostIndexing`（beta）。

### 还没发布的东西：KubeRay Federation

⚠️ **不要把 KubeRay Federation 当成已发布能力。** 它至今仍是 **kuberay
issue #4561** 的一个**提案**，配有一份设计文档。它想解决的真实痛点是：
当前 KubeRay 局限在**单个 K8s 集群**内，GPU 被切碎在多个可用区 / 云厂商 /
本地机房，团队只能部署"20 个集群 × 10 张卡"而不是"1 个集群 × 200 张卡"。
提案里的 `FederatedRayCluster`（FRC）是用户面的权威资源，primary / member
RayCluster 是它生成的子资源，跨集群容量分配复用 autoscaler 的 worker group
选择逻辑（因此**不保证精确的 40/30/30 分配**）。**如果你的架构依赖多集群联邦，
现在只能靠上层自研调度，或用虚拟 kubelet 方案（提案里同时指出虚拟 kubelet
的扩展性上限：入口控制面会成为瓶颈，10K → 400K+ 核的峰值伸缩可能压垮它）。**

## 17.3 资源与容量

| | head 节点 | worker 节点 |
|---|---|---|
| GCS（集群元数据） | 跑 | 不跑 |
| Dashboard / State API | 跑（默认） | agent 跑 |
| Autoscaler | 跑 | 不跑 |
| Serve Controller / HTTP Proxy | 默认跑 | 视 `proxy_location` |
| 可以跑计算任务 | **能，但一般不该** | 能 |

**"head 上不要跑业务负载"** 是 Ray 的一条硬规则，理由见 17.8.1 ——
head 挂 = 整个集群挂，而它自己的磁盘和内存也容易被占用。

```bash
ray start --head \
  --num-cpus=16 --num-gpus=4 \
  --object-store-memory=$((16 * 1024 ** 3)) \
  --dashboard-host=127.0.0.1     # 见 17.5，别用 0.0.0.0
```

| 参数 | 说明 | 常见错误 |
|---|---|---|
| `--num-cpus` | 对外声明的 CPU 数 | 声明成物理核数，系统进程没得用 |
| `--num-gpus` | 对外声明的 GPU 数 | 与 CUDA_VISIBLE_DEVICES 不一致 |
| `--object-store-memory` | 对象存储上限 | 见 17.8.3，配小是很常见的坑 |

### system reserved：Ray 会自己扣一部分

Ray 会为系统进程预留一部分资源（cgroupv2 资源隔离路径，PR #57653 调整了算法）：

```
CPU 预留  = min(3.0,  max(1.0,   0.05 * 系统核数))     → 至少 1 核，至多 3 核
内存预留  = min(10GB, max(500MB, 0.10 * 系统内存))     → 至少 500MB，至多 10GB
```

对应环境变量 `RAY_DEFAULT_SYSTEM_RESERVED_CPU_PROPORTION`（**0.05**）与
`RAY_DEFAULT_SYSTEM_RESERVED_MEMORY_PROPORTION`（**0.10**）。两点注意：
① 这套默认值**只在 `enable_resource_isolation` 为 True 时生效**；
② **显式设置为 0 的覆盖会被拒绝**（PR #63864）。算法意图很直白：小机器上 5%
只有 0.05 核（太少），大机器上 5% 是 6 核（太多），所以上下都要 clamp。
**算容量时别忘了先减掉这部分**，否则 100 核集群实际只有 97 核可用。

### Autoscaler v2

Autoscaler v2 的 alpha 在 **Ray 2.10** 引入。
**"默认开不开"这件事本书标注了四轮的「未确认」，第八轮已核实 —— 而且答案是"分路径的"：**

| 路径 | v2 默认状态 | 判据（可复核） |
|---|---|---|
| **`ray up`**（VM 集群） | **2.50.0 起默认开启** | `python/ray/autoscaler/_private/commands.py` 里是 `os.getenv("RAY_UP_enable_autoscaler_v2", "<默认值>")`：**2.49.0 是 `"0"`、2.50.0 起是 `"1"`**（逐 tag 取该文件即可对比）。源码注释原文：*"The default value is 1 since Ray 2.50.0."* |
| **裸 `ray start`**（组件默认） | **关闭** | `ray_config_def.h` 里 `RAY_CONFIG(bool, enable_autoscaler_v2, false)` |
| **KubeRay** | **opt-in** —— 要显式写 `autoscalerOptions.version: v2` | operator 侧的判断要求该字段显式为 `v2` 才注入 |

> 🔴 **所以本书早先那句"2.54.0 起新的集群 autoscaler 默认开启"是错的**：
> 翻转发生在 **2.50.0**，而且**只对 `ray up` 这一条路径**成立 ——
> 这也解释了它为什么看起来"和源码对不上"（源码里那个 `false` 说的是**组件默认**，
> 不是 `ray up` 注入之后的值）。**两处说的不是同一层。**

v2 相比旧版的核心变化是把"期望的资源"和"节点供给"解耦，支持 `drain_node`
（17.6 会用到）、优先级感知的节点类型选择、更细的伸缩决策记录。

```yaml
enableInTreeAutoscaling: true
autoscalerOptions:
  upscalingMode: Default        # 或 Aggressive / Conservative
  idleTimeoutSeconds: 60
workerGroupSpecs:
  - groupName: gpu-h100
    priority: 10                # 注意 PR #65244 修过的透传 bug
    minReplicas: 0
    maxReplicas: 16
```

## 17.4 可观测性接入

**State API 与 CLI**：`ray.util.state` 是 **alpha API**（官方文档明确标注），
需要 `pip install "ray[default]"`（不是裸 `ray`）与 **dashboard 组件可用**
（`--include-dashboard=false` 会让 State API 一起失效）。

```bash
ray summary tasks / actors / objects      # 状态汇总
ray list tasks --filter "state=FAILED"
ray get task <TASK_ID>                    # 单个任务详情
```

**日志**：

```bash
ray logs                                  # 节点日志
ray logs actor --id <ACTOR_ID> --follow
ray logs task  --id <TASK_ID> --tail 200
```

⚠️ **重要限制**：Log API **只能拿到存活节点的日志**。节点被 autoscaler 缩容掉
之后日志就没了 —— 这正是 KubeRay v1.7 的 **History Server（beta）** 想解决的
问题。**如果你的排障流程依赖"事后回看被删掉的节点日志"，现在就必须把日志外送
到集群外的系统**，不能依赖 Ray 自己的 Log API。

**"外送"具体怎么送**（三条可落地的路，按介入深度排序）：

| 方式 | 做法 | 适用 |
|---|---|---|
| **① 收 stdout/stderr**（最省事） | `ray start` 可以 `--logging-config=<yaml>` 把日志按 logger 名分流到文件；**日志外送系统直接收容器 stdout** 是 K8s 上最标准的做法（Fluent Bit / Promtail DaemonSet） | K8s 上首选 |
| **② 挂日志目录** | Ray 的日志落在 `/tmp/ray/session_*/logs/`（`session_latest` 是软链）。把它挂成 emptyDir→hostPath，或用 sidecar 读 | 非 K8s、或要保留原始文件 |
| **③ 改 logging 配置** | `--logging-config` 指定每个 logger 的输出文件与级别（raystderr/rayout 一类） | 要按组件分流时 |

> **一句话**：Ray 的日志本身就是"写在本地磁盘上的文件"，
> **外送的责任在你的基础设施（DaemonSet / sidecar），不在 Ray**。
> 别指望"配一个 Ray 参数就把日志送进 Loki"。

**指标**：Ray 默认把物理指标、内部指标和自定义指标导出为时间序列，**默认对接
Prometheus**；Dashboard 的 **Metrics** 视图**需要 Prometheus + Grafana**
（或托管方案）—— 裸装 Ray 的 dashboard 看不到指标曲线，这不是 bug。

⚠️ **OTel metrics 的一个真实死锁 bug**：**2.52.0 – 2.56.1** 里
`OpenTelemetryMetricRecorder` 的**指标注册与 `collect()` 之间存在死锁**，会导致
**head 节点上的 dashboard agent 冻结**。后果很隐蔽：agent 不响应 job 提交请求
→ **提交的 job 永远不启动，然后被标记为失败**。修复在 **2.57.0**
（PR #64946、#65094，来源：Ray 2.57.0 release notes 与 Anyscale 平台 2026 年
8 月发布说明）。**"job 提交了但一直 pending 然后 failed"在这个版本区间里，
第一个该怀疑的就是它。**

**Dashboard 成熟度分层**：State API / State CLI 是 **alpha**；Task 级 dashboard
是 beta（⚠️ 官方文档中该标签的准确表述**未确认**）；Ray Data / Ray Train
dashboard 是 **GA**；Metrics 视图依赖外部 Prometheus + Grafana。

**统一健康端点 `/api/healthz`**：背景（issue #56204）是早期 KubeRay 的存活探针
是 `exec` 探针，靠 `wget` 打 `localhost:52365/api/local_raylet_healthz` 与
`localhost:8265/api/gcs_healthz` —— 问题有三：镜像里未必有 `wget`、exec 探针
给 Pod 额外负载、kubelet 拉进程容易超时。解决方式是加一个统一的
**`/api/healthz`**（PR #56943），它合并 raylet 与 GCS 的健康状态（先查 raylet，
不健康就直接返回；再查 GCS，不健康返回 503），**目的是让 K8s 用原生 HTTP 探针**。
⚠️ **版本未确认**：任务材料写"2.53+"，但本次检索**没有找到明确说明它进入哪个
版本的证据**（相关结果里出现的版本号是 2.54.0）。上线前请用你的镜像实际
`curl http://<head>:8265/api/healthz` 验证，不要照抄版本号。

**还有一个必须自己补的探针**：KubeRay 生成的 RayService **worker liveness
probe 只检查本地 raylet**（`/api/local_raylet_healthz`），readiness 才额外看
Serve 的 `/-/healthz`（kuberay issue #4685，Ray 2.54.0 + KubeRay v1.5.0）。
后果是 **proxy actor 卡住时 pod 不会被重启，部署永久卡死**。KubeRay 维护者不
推荐把 proxy actor 健康塞进 worker liveness，建议自定义探针或上层恢复。
**这条请写进你的探针设计文档。**

## 17.5 安全：必须写透的一节

### 默认不做鉴权：这是设计选择，也是真实短板

Ray 的设计假设是"集群跑在网络隔离的可信环境里"，所以**默认没有任何鉴权**。
任何人能连到 dashboard 端口（默认 8265）或 GCS / raylet 端口，就能**提交任务**
—— 而提交任务等价于**在集群节点上执行任意代码**。这不是"配置疏忽"，是刻意的
默认值；但在 2026 年，它的后果已经被真实利用事件验证过了。

### 鉴权之外：**内部 RPC 默认是明文的**

这是本书早先漏掉的一层，必须补上 —— **"谁能连上"和"连上之后传的东西会不会被看到"
是两个独立的问题**：

* Ray 各组件之间（driver ↔ GCS ↔ raylet ↔ core worker）的 gRPC 通信
  **默认不加密**。也就是说即使你做了网络隔离，**同网段内的抓包仍能还原出
  任务参数、对象内容和元数据**；
* Ray 提供 **TLS 开关**来加密这层通信：`ray start` 的 `--tls-*` 系列参数，
  以及 `RAY_TLS_*` 一族环境变量（证书路径、是否校验对端等）。
  ⚠️ **具体参数名与取值本书未逐条核实** —— 请以你所用版本的
  `ray start --help` 与官方 "TLS / Encryption" 文档为准；
* **实践建议**：在合规场景（跨租户、跨安全域、数据敏感）里，
  **"token 鉴权 + TLS + NetworkPolicy" 三件要一起做**，
  缺任何一件都留着一个明确的洞：

| 缺哪一件 | 剩下的洞 |
|---|---|
| 缺 token/鉴权 | 任何人可提交任务 = 任意代码执行 |
| 缺 TLS | 通信可被同网段嗅探/中间人 |
| 缺 NetworkPolicy | 攻击面扩散到整个集群网段 |

> **KubeRay 侧**：cert-manager mTLS 集成能帮你把证书这一层自动化
> （见 §17.2 的 v1.7 特性），但**它管的是 KubeRay 自己的 webhook/控制器**，
> 与上面 Ray 内部的 TLS 是两件事 —— **不要以为开了 cert-manager 就万事大吉**。

### CVE-2025-62593：从"浏览器打开一个网页"到 RCE

| 项 | 内容 |
|---|---|
| CVSS | **v4.0 = 9.4**（v3.1 = 8.8；引用时务必带上版本，否则与 NVD 页面对不上） |
| 影响版本 | **Ray < 2.52.0** |
| 修复版本 | **2.52.0**（2025-11-26，advisory GHSA-q279-jhrf-cc6v） |
| 类型 | 未认证远程代码执行（浏览器路径） |
| CISA KEV | **2026-08-17 列入**已知被利用漏洞目录 |

```
① 弱防护：Ray 为防浏览器攻击，检查 HTTP User-Agent 是否以 "Mozilla" 开头
      ↓  但 Fetch 规范允许脚本自行设置 User-Agent
         （Firefox / Safari 上可实现，Chrome 不是可行路径）
② DNS rebinding：受害者访问一个恶意页面 / 恶意广告
      ↓  恶意页面把域名重新绑定到受害者本机的 127.0.0.1:8265
③ 以 dashboard 进程的权限执行任意 shell 命令
```

**已被观测到的利用活动**（开源威胁情报，非 CISA 披露）：**RondoDox DDoS
僵尸网络**在公开披露前**两天**就集成了该漏洞利用（BitSight，2026-03 报告，
借助公开 PoC）；**"ShadowRay 2.0" 挖矿活动**（Oligo 观测）把未修补的 GPU 集群
变成可自我复制的挖矿僵尸网络。

**一个重要特征**：它**不要求 Ray 暴露在公网**。攻击面是"开发者浏览器 +
本机 8265 端口"，也可以横向扩展到企业内网中相邻的 Ray 实例 ——
这与更早的 **CVE-2023-48022（ShadowRay）** 的画像不同（后者要求暴露）。

**修复期限**：该漏洞被列入 CISA 的 **KEV 目录**，并受 **BOD 26-04** 约束 ——
美国联邦民用行政部门（FCEB）机构需要在指定日期前完成修复。

据多个一致的公开来源（CISA KEV 目录整理、Anyscale 官方博客
*Ray CVE-2025-62593: KEV*）：

| 事件 | 日期 |
|---|---|
| CISA 将 CVE-2025-62593 收录进 KEV | **2026-08-17** |
| FCEB 机构的修复期限 | **2026-08-20**，即 **3 天窗口** |

> **来源说明**：上表与本书写作时的多个二手来源一致（含 Anyscale 自己的
> 复盘），但**本书未逐字比对 CISA 的 KEV 目录原文**。取用前请以 CISA 原文为准。
>
> ⚠️ **别被具体天数带偏**：真正要记住的不是某个截止日期，而是
> **KEV 收录意味着「已被在野利用」**。BOD 26-04 下的 3 天窗口属于**异常紧**
> 的一档（这也是它被广泛报道的原因）；不同 BOD 与不同漏洞的窗口长度并不相同，
> **不要把它当成一个通用天数去推断别的 CVE**。
> 对非 FCEB 组织，它是一份「优先级最高」的信号，而不是一个法律期限。

### CVE-2026-27482

**中危**，位置是 dashboard 的 **DELETE 端点**缺少防护，**修复于 2.54.0**。
它比上一条温和，但说明了同一件事：**dashboard 是一个持续的弱点聚集地。**

### 缓解清单（按优先级排序）

```
[ ] 1. 升级。最低 2.52.0，实际建议 2.54+（含 27482 修复），有条件就
       2.57+/2.58（含 OTel 死锁修复）。对 2.52 之前的版本，升级是唯一完整的
       缓解手段。
[ ] 2. 开 RAY_AUTH_MODE=token。2.52 引入，默认关闭，必须显式开；它保护
       dashboard、job 提交与 Ray Client。官方建议即使开发环境也打开。
[ ] 3. 永远不要把 8265 / 6379 / 10001 等端口暴露到不可信网络。检查所有配置、
       镜像、Helm chart 里有没有 --dashboard-host=0.0.0.0 或 dashboard-host:
       0.0.0.0。K8s 里用 Service 类型与 NetworkPolicy 兜住。
[ ] 4. 清点。Ray 常常是传递依赖，传统软件清单发现不了。要查：开发者工作站、
       CI runner、容器镜像、K8s 工作负载、lockfile、基础镜像 / runner 镜像、
       云上的 AI 基础设施。
[ ] 5. 把 runtime_env 当成 RCE 面来看。runtime_env 的 pip / working_dir /
       env_vars 会执行任意代码，能设它的人 = 能在集群上执行代码的人；
       带 token 认证的集群里，token 的权限边界要按这个来设计。
[ ] 6. 监控异常：未授权的命令执行、凭据访问、Ray 节点发起的外部连接。
```

**第 4 条最容易被忽略。** "我们生产没跑 Ray" 和 "我们没有任何地方装过 Ray"
是完全不同的两句话 —— 一个装过 Ray 的开发者笔记本，只要他打开过 dashboard，
就在 CVE-2025-62593 的射程内。**CI runner 尤其危险**：权限高、长期在线、
跑的是不可信来源的代码。

### Ray 2.58 的实验性 Sandbox（gVisor）

方向上的补强：Ray 2.58 引入实验/alpha 的沙箱库 **`ray.experimental.sandbox`**
（PR #64964，2.58 分支 cherry-pick #65397；后续 #65622 让它可以跑 Docker 构建
的镜像），由 Google Cloud 与 Anyscale 联合推出。机制：**沙箱本身是 Ray Actor**
—— Ray 调度器决定它在哪个节点跑、预留多少 CPU/内存，actor 管生命周期
（create / destroy / recover / scale）；**隔离由 gVisor 的 `runsc` 提供**，
它在用户态实现大部分 Linux 系统调用接口，在负载与宿主内核之间加一层边界；
不需要把 Docker daemon / host socket 暴露给沙箱；亚秒级（官方称 sub-100ms
量级）启动。

安全默认值：**根文件系统默认只读**，只有配置的工作目录可写；**网络默认
`none`**，不显式选择其它模式就没有出网能力（合法模式为 `"none"` / `"public"` /
`"host"` / `"sandbox"`）；CPU / 内存限制通过 cgroup 强制。

前置条件与边界：Linux（x86_64 或 arm64）、**Ray 2.58.0+**、每个可用 worker
节点的 `$PATH` 里要有 `runsc`。**它仍是 alpha 安全组件，不是"开了就安全"** ——
安全属性高度依赖沙箱与周边基础设施的配置；宽松配置（公网出网、宽 Linux
capabilities、可写文件系统、不受控的镜像仓库）会放大攻击面，而 **gVisor 的
边界补偿不了薄弱的网络 / 身份 / 镜像 / K8s 策略**。首批适用场景很窄：CPU 型、
短生命周期、容器化、无网络、镜像受控；**GPU 支持、REST API、文件系统快照、
端口暴露都还是 future work**。Google Cloud 报出的规模数字是"20 秒内在 GKE
上千节点上拉起 10 万个隔离沙箱"—— **厂商自报结果，不是独立基准**。

**怎么定位它**：这是为 agentic RL / 模型生成代码执行这类"必须跑不可信代码"
的场景准备的。**普通推理服务不需要它** —— 那种场景该做的是上面的缓解清单。

## 17.6 发布与升级流程

**镜像固定 digest**：`rayproject/ray:2.58.0` 这种 tag 可变（同一 tag 可能被
重新推送），而滚动升级依赖"新旧镜像确实是同一个东西"这个前提。要写
`image: rayproject/ray@sha256:<digest>`。Ray 官方自己也做过这件事 ——
把 GCS RocksDB 的示例从浮动 nightly tag **固定到 `rayproject/ray:2.57.0`**
（PR #65429）。

### 优雅排空与 `RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S`

**背景：Ray 2.32 → 2.33 的一次回归，导致 KubeRay RayService 零停机升级期间
出现成片的 HTTP 500。**（issue #64181，修复 PR #64454）

```
2.32 的行为：
  KubeRay 用 bash -c "...; ray start ... --block" 启容器
  → bash 是 PID 1；非交互 bash 不转发 SIGTERM，内核又会抑制 PID 1 的
    默认信号动作
  → K8s 删除 Pod 的 SIGTERM 被"吞掉"，Ray 一直跑到 grace period 结束被 SIGKILL
  → 旧 proxy 与新副本自然完成排空，没有 500

2.33+ 的行为：
  bash 的"最后一条命令 exec 优化"让 ray start (Python) 变成 PID 1
  → SIGTERM 直达 Ray 的 handler
  → handler 调 kill_all_processes → SIGTERM raylet（1s 宽限）并立即拆掉本节点
    的 Serve replica
  → 但旧的 Uvicorn / Serve proxy 还在，且仍持有 keep-alive 连接
  → 转发到已被拆掉的 replica → 真实 HTTP 500
```

修复是优雅排空：`RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S=60`
（秒）。**它的默认值已核实为 `30.0` 秒，不是 0**
（`python/ray/_private/ray_constants.py` 的 `env_float(
"RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S", 30.0)`，同处注释写明
*"Defaults to 30s; cluster managers (e.g. KubeRay) tune it, typically to just
under the pod termination grace period. **0 disables draining** (immediate
teardown)."*）——
也就是说：**排空默认就是开着的**，`0` 才是"关掉、立刻拆进程"。
实现上是：SIGTERM handler 在
`kill_all_processes` 之前会 ① 通过 GCS 的 `drain_node` 把本节点标记为
draining（reason `PREEMPTION`，带 deadline，复用 autoscaler v2 的机制）；
② 让 drain-aware 组件（Ray Serve）在本地 proxy 上静默、迁移副本；
③ 轮询 raylet 是否退出（`dead_processes()`），受 timeout 上限约束，然后才继续
拆进程。

> ⚠️ **本书早先在这里写错了**：把默认值说成"文档 0 = 关闭"，
> 并据此推出"所以你必须显式设置它，否则不会排水"。**两句话都反了** ——
> 默认 30 秒就是生效的，显式设置的意义是**调成适合你 K8s 宽限期的值**，
> 而不是"打开开关"。下面那张表里对应的那一行也一并更正。

**这个修复本身有几个已知问题，你必须知道**（来自 PR #64454 的评审记录）：

| 问题 | 后果 |
|---|---|
| `_is_node_drained` 读的字段 GCS 没有拷贝到 RPC 背后的 store 里 | 轮询**几乎永远看不到 drained**，经常白等到 30 秒超时（而不是提前结束） |
| SIGTERM 可能在 `_node_id` 赋值前到达 | handler 抛 `AttributeError` 直接中止，`kill_all_processes` 不执行 |
| raylet 还没进 `all_processes` 的启动窗口内收到 SIGTERM | 明明没有 raylet 可排空，也会睡满整个 timeout |
| `drain_node` RPC 的 gRPC deadline 无上限，且 timeout 同时用于 RPC 与轮询 | SIGTERM 处理时间可能达到配置值的 **约 2 倍** |

**实践建议**：**显式设置** `RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S`
（理由不是"默认关闭"，而是**默认的 30 秒未必与你的
`terminationGracePeriodSeconds` 匹配** —— 它应该略小于后者，
并给"约 2 倍"的最坏情况留余量）；把它的值**设进 K8s 的
`terminationGracePeriodSeconds` 预算内**；升级后**必须做一次
真实的滚动升级演练并观察 5xx**，不要只看"pod 都 Ready 了"。
如果真的不需要排空（例如不跑 Serve、重启无所谓），设 `0` 显式关掉。

Serve 侧还有两个配套修复：PR #63886 处理 HAProxy direct-ingress 的排空竞态
（把最小排空期并进排空循环，并在 `shutdown()` 前先关闭 ingress 监听），
PR #60754 让**卡在排空中的副本被强制杀掉**（超时为
`max(graceful_shutdown_timeout_s, RAY_SERVE_DIRECT_INGRESS_MIN_DRAINING_PERIOD_S)`）。

### 滚动升级期间不中断请求的设计

```
                    ┌────────── 外部 LB / Gateway ──────────┐
                    ▼                                       ▼
            ┌───────────────┐                       ┌───────────────┐
            │ 旧 proxy +    │  流量按比例迁移         │ 新 proxy +    │
            │ 旧 replica    │ ────────────────────► │ 新 replica    │
            └───────────────┘                       └───────────────┘
              旧 pod 先 drain → 排空 → 删除
```

要做到"请求无中断"，下面四条必须**同时**成立：① **接流量的一层先摘掉**
（LB / Gateway / readiness probe 变 NotReady），而不是先杀进程；
② **进程收到 SIGTERM 后优雅排空**；③ **客户端超时与重试策略正确** ——
服务端排空只能保证"已建立的请求跑完"，客户端超时若比服务端排空时间还短，
照样报错；④ **探针不撒谎**（17.4 的 KubeRay worker liveness 问题就是反例）。

> **本章没讲的两件事**：
> ① **CI/CD** —— 怎么测 Ray 应用、镜像怎么构建与晋级、故障注入怎么进流水线；
> ② **没有 Kubernetes 怎么办** —— 研究机构与超算中心常用的 **Ray on Slurm**
> 路径，以及云上成本模型（含利用率怎么折算成有效成本）。
> 这两块在**附录 G（第 28 章）**：CI/CD 见 §G.2，Slurm 见 §G.3，成本见 §G.4。

## 17.7 成本与运维清单

```
【部署前】
[ ] 选定部署形态并记录理由（17.1 决策表）
[ ] head 节点规格 ≥ worker（GCS + dashboard + controller 都在它上面）
[ ] 明确"head 不跑业务负载"，用 taint / nodeSelector 强制
[ ] object store 内存已估算（经验：总内存 30% 以内）
[ ] 镜像固定到 digest，且所有节点预拉取
[ ] system reserved 的 CPU / 内存已从容量规划里扣除

【安全（17.5）】
[ ] 版本 ≥ 2.54（已修 CVE-2025-62593 与 CVE-2026-27482）
[ ] RAY_AUTH_MODE=token
[ ] dashboard 不对外暴露；NetworkPolicy 已配
[ ] CI runner / 开发机 / 基础镜像的 Ray 已清点
[ ] runtime_env 的写入权限已按"等价于 RCE"设计
[ ] 监控已覆盖：异常命令执行、凭据访问、节点异常外连

【可观测性】
[ ] State API 可用（装了 ray[default]，dashboard 没关）
[ ] 日志已外送到集群外（不依赖 Log API，它只能看活节点）
[ ] Prometheus + Grafana 已接（否则 dashboard 的 Metrics 视图是空的）
[ ] 版本不在 2.52.0–2.56.1 的 OTel 死锁区间内（或已知晓该风险）
[ ] 自定义 liveness probe 已覆盖 Serve proxy 健康（补 KubeRay #4685）

【升级流程】
[ ] RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S 与 terminationGracePeriodSeconds 对齐
    （默认 30 秒、排空默认开启；显式设成略小于 K8s 宽限期的值）
[ ] terminationGracePeriodSeconds > 2 × 上述值
[ ] 做过一次真实滚动升级演练，并记录了 5xx 曲线
[ ] 回滚路径已验证（RayService 的升级策略 + 旧镜像 digest 可用）
[ ] 客户端超时 < 服务端排空时间的假设已核对

【成本】
[ ] worker group 的 min/max 与实际负载曲线匹配（min=0 适合批任务）
[ ] 闲置超时（idleTimeoutSeconds）已设，避免 GPU 空转
[ ] 与 K8s Cluster Autoscaler 的关系已理清（见 17.8.2）
[ ] 有"每 GPU 每小时产出"的度量，而不只是利用率
```

## 17.8 常见事故复盘

### 17.8.1 head 节点磁盘被日志/对象打满

**症状**：整个集群突然不健康，job 提交失败，dashboard 打不开，新节点连不上。
**机制**：head 上同时跑着 GCS（元数据，含 RocksDB 或 Redis）、dashboard、
autoscaler，以及默认的 Serve controller 与 HTTP proxy，这些组件的日志全写在
`<RAY_LOG_DIR>`（通常 `/tmp/ray/session_latest/logs/`）下。集群规模大、任务多、
或某些组件刷错误日志时，**`/tmp` 被写满**。**为什么致命**：GCS 写不进去 =
集群元数据服务不可用 = **整个集群不可用**，即使所有 worker 都还健康。

**预防**：给 head 的 `/tmp/ray` 挂独立卷并设容量告警；`--temp-dir` 指到大盘；
**把日志外送**，本地只留短窗口；把业务负载从 head 上赶走。

### 17.8.2 autoscaler 的 min/max 配置冲突

**最经典的版本**：**Ray Autoscaler 与 K8s Cluster Autoscaler 不共享状态。**

```
你想要的：负载升高 → Ray autoscaler 想要 8 个 GPU worker → K8s 没有空闲节点
                   → Cluster Autoscaler 扩容节点 → worker pod 被调度上去

实际经常发生的：
  Ray autoscaler 看"资源不够" → 尝试创建 pod → pod Pending（节点不足）
  → Cluster Autoscaler 因为配额 / 节点池 max / 选择器不匹配而没扩
  → Ray autoscaler 认为"已经请求过了"，不再重试
  → 两边都认为对方在负责，集群卡在"资源不够又不扩"的状态
```

**变体**：worker group 的 `maxReplicas` 与节点池的 `maxNodes` 不一致 ——
Ray 想扩到 16，节点池只能到 8，于是 8 个 pod 永久 Pending。

**排查顺序**：① `kubectl get pods` 看有没有长期 Pending 的 worker pod；
② 看 Cluster Autoscaler 日志，确认它**是否认为这些 pod 该由它负责**
（节点池选择器、taint/toleration、资源请求是否匹配）；③ 核对 Ray 侧
`maxReplicas` 与节点池 `maxNodes` 的乘积关系；④ 看云配额。
**预防**：把"Ray 的 max ≤ 节点池能提供的上限"写成硬约束并加校验。

### 17.8.3 object store 配太小

**症状**：任务变慢、spill 到磁盘、`ray.get` 变慢，严重时任务超时。
**机制**：object store 是共享内存，`ray.put` 的对象和任务返回值都住在里面。
配小了 → 频繁 spill → 磁盘 IO 成瓶颈；配大了 → 挤压系统与其它进程内存 →
OOM killer 出手。**判断方法**：看 `ray summary objects` 里的对象大小分布与
dashboard 的内存曲线。**如果你的任务里有几个 GB 级的大对象在多个任务间传递，
object store 就是热点。** 经验值：取总内存 30% 以内，不要吃满；同时
**把大对象改成流式消费或分块传递往往比调参数更有效**。

### 17.8.4 镜像拉取慢

**症状**：autoscaler 扩出来的节点长时间 Pending / ContainerCreating，
Ray 侧看到的是"要了资源但迟迟不 ready"。
**机制**：Ray 镜像动辄几个 GB，新节点冷拉取时间可能远超 autoscaler 的容忍
窗口 —— **Ray 在等的其实不是"节点"，是"容器起来"**。
**排查**：`kubectl describe pod` 看 Events 里的拉取耗时；确认节点池开了镜像
缓存 / 流式加载（GKE 的 Image Streaming、AWS 的 SOCI 之类）。
⚠️ **一个需要自行验证的坑**：镜像流式加载方案通常有"镜像与节点在**同一
区域/可用区**才能生效"的前提；如果镜像仓库与节点池跨区，流式加载会静默退化
成完整拉取。**本教程未确认 GKE Image Streaming 的具体区域匹配规则，请以官方
文档为准**，但排查时值得先看这一条。
**预防**：用启动时预拉取的 DaemonSet 预热；把镜像做小（多阶段构建、只装需要
的 extras）；把节点池的镜像与 Ray 镜像版本绑定。

## 17.9 多租户与配额

前面 17.5 讲的是"外面的人能不能打进来"，这一节讲**里面的人怎么互不干扰** ——
这是把 Ray 交给多个团队时最先撞上的问题。

### 先把最难听的话说完：Ray **没有配额机制**

**这是事实，不是"没配好"。** 判定依据（可复现，且这次是**穷尽式**的）：

```bash
# 在 ray-2.58.0 的整棵 python/ray/ 树里检索 quota（排除 tests/）
grep -rin "quota" python/ray/ | grep -v "/tests/"
```

**28 处命中，逐条看完全部不构成租户配额**：`actor.py` 里
`memory: The heap memory quota for this actor.` 的 docstring 措辞、
`_private/utils.py` 的 cgroup `cpu.cfs_quota_us`、`experimental/sandbox/`
的镜像 cgroup 配额、`dashboard/.../job_supervisor.py` 的 win32 进程配额、
以及 `data/` 内部 actor 自动扩缩器的一句注释。
**没有"某个用户 / namespace 最多能用多少 CPU/GPU/内存"的一等概念。**

> 📌 **一处曾经的错误**：本书早先引的三个"零命中"路径里，
> `python/ray/_private/ray_option_utils.py` **在 2.58 已经不存在** ——
> 它在 `python/ray/_common/ray_option_utils.py`。
> （对照组：同目录的 `python/ray/_private/ray_constants.py` **仍然存在** ——
> 所以是这一个文件挪了家，不是整个 `_private/` 没了。）
> 结论没错，但**证据里引用了一个不存在的路径** ——
> 下一个想复现这条证据的人会搜空，然后反过来怀疑结论。
> 这类"结论对、证据错"的问题最难被发现，详见第 39 章 §39.5。

Ray 的资源模型是**集群级共享池 + 声明式申请**：

```
每个任务/actor 声明自己需要的资源（num_cpus / num_gpus / memory / 自定义资源）
        ↓
调度器（raylet + GCS）在集群的剩余资源里找得下的节点
        ↓
找不到 → 排队（PENDING），等到有资源为止
```

这套模型保证的是"**不会超发**"（声明 1 张卡就真占 1 张），
**不保证"公平"** —— 它**没有 fair-share / 权重 / 借用/归还**。
先提交的大作业可以把集群占满，后提交的只能等。**想让 Ray 做多租户配额，
必须在 Ray 之外加一层。**

### 五种隔离手段，哪些是真隔离

| 手段 | 层级 | 它实际做了什么 | 是真隔离吗 |
|---|---|---|---|
| `ray.init(namespace="team-a")` | Ray 逻辑分组 | 把 job 与**命名 actor** 归到一个命名空间；`ray.util.get_actor(name, namespace=...)` 按名字取。**不限制任何资源** | ❌ **只是约定**。它连"别人的 namespace 里有什么"都挡不住 —— 只是不给 namespace 就按当前命名空间找。**绝不能当租户边界用** |
| `ray start --num-cpus=N --num-gpus=M` / `--resources='{"model_server": 1}'` | 单节点 | 声明**这个节点对外提供多少**资源。调度器严格按声明值排队，所以它是**硬的** | ⚠️ **真核算，但粒度是节点、不是租户**。它防超发，不防"被别的租户抢光" |
| `@ray.remote(num_cpus=…, num_gpus=…, memory=…)` | 任务 / actor | 单次申请的资源量，决定"放不放得下" | ⚠️ 同上：**核算而非配额**。`memory` 尤其只是**声明值**，不强制 |
| 放置组（`placement_group` + `STRICT_PACK`） | 作业 | 把一批 bundle **一次性占住**，避免大作业被资源碎片饿死 | ⚠️ **能做粗粒度预留**，但**不阻止别人用剩下的资源**，也不按租户记账 |
| **KubeRay：每租户一个 `RayCluster`** | 集群 | 每个 RayCluster 有**自己的** head / GCS / raylet / 对象存储 / dashboard | ✅ **这是唯一"硬"的边界** —— 租户之间只共享 K8s 节点池 |
| **K8s `ResourceQuota` / `LimitRange`（按 namespace）** | K8s | 卡住该 namespace 里所有 Pod 的 CPU/内存/GPU **总量**与单 Pod 上下限 | ✅ **真配额，但在 K8s 层** —— Ray 自己看不见它，只会表现为"pod 起不来 / 长期 PENDING" |

**一句话判断**：**Ray 里的东西（namespace、资源声明、放置组）管的是"调度"，
不是"隔离"；真正能卡住租户的是 K8s 那一层（namespace + ResourceQuota +
每租户一个 RayCluster）。**

### 推荐的多租户架构（按隔离强度从高到低）

1. **每租户一个 K8s namespace + 一个 KubeRay `RayCluster` + 一份
   `ResourceQuota`/`LimitRange`** —— 生产级多租户的默认答案。
   隔离面：GCS 元数据、对象存储、dashboard 端口、`/tmp/ray` 日志、
   Ray 自己的控制面版本，全都独立。代价是**每租户一套控制面开销**
   （head 内存 + dashboard + GCS），租户很多时要算这笔账；
2. **共享一个 RayCluster，但每个租户一个 K8s namespace** —— 做不到：
   RayCluster 的 pod 属于创建它的那个 namespace。**共享集群就意味着共享
   namespace**，所以这条只能退化成"共享集群 + 约定"，见下；
3. **共享一个 RayCluster，靠 namespace + 约定** —— ⚠️ **只适合内部、
   互相信任的团队**。这一层没有硬边界：任何人都能提交
   `@ray.remote(num_gpus=8)` 把卡占满，也都能通过 dashboard（默认 8265）
   提交任意任务（= 在集群上执行任意代码，见 §17.5）。
   要勉强加硬一点，至少做三件事：
   * `RAY_AUTH_MODE=token`（否则 dashboard 是完全开放的）；
   * 用 `--num-cpus` / `--num-gpus` 把**每个节点的可分配量**卡到"留出系统余量"
     （见 §17.3 的 system reserved），别让一个团队把节点吃满；
   * 给每个团队的作业约定独立的 `namespace`，并且**只把它当命名规范**，
     不当安全边界。

### 四个必须纠正的常见误解

| 误解 | 事实 |
|---|---|
| "设了 `ray.init(namespace=...)` 就隔离了" | namespace 是**命名分组**，不是资源或权限边界。它管的是"按名字找 actor 时去哪个目录找" |
| "Ray 有公平调度 / 权重" | 没有 fair-share。Ray 的队列是公平性无关的：资源够就跑，不够就排队 |
| "限制并发就是配额" | 并发上限（`max_concurrency`、`num_cpus=0.1` 做闸门）限制的是**同时在跑的**，不是**累计占用**；10 个租户各跑一会儿和 1 个租户独占，在 Ray 眼里没区别 |
| "`memory=` 能当内存配额" | 它是**调度声明**，不是 cgroup 限制。声明少了不会被杀，声明多了会"有空闲却放不下" |

> **和 §17.5 的衔接**：多租户场景里"**谁能连上**"和"**谁能用多少**"是
> 两个独立问题。前者靠 `RAY_AUTH_MODE=token` + NetworkPolicy + TLS，
> 后者靠 K8s ResourceQuota + 每租户 RayCluster。**两件都做，缺一不可。**

---

## 17.10 非 x86 架构与 Windows

这一节回答三个很具体的问题：**ARM 上能不能跑**、**Windows 上能跑什么**、
**平台的边界在哪一行**。所有结论都对着 2.58 的源码核过。

### 官方 wheel 支持哪些平台（源码级清单）

判据是 `python/ray/_private/utils.py` 里的 `get_wheel_filename()` ——
它是 Ray 拼 nightly / release wheel 名字的那段逻辑，也等于官方"发了哪些平台
的包"的权威清单：

```python
assert sys_platform in ["darwin", "linux", "win32"]

if sys_platform == "darwin":
    os_string = "macosx_12_0_x86_64" if architecture == "x86_64" else "macosx_12_0_arm64"
elif sys_platform == "linux":
    os_string = "manylinux2014_aarch64" if architecture in ("aarch64", "arm64") \
                else "manylinux2014_x86_64"
elif sys_platform == "win32":
    os_string = "win_amd64"
```

读出来的四件事：

1. **操作系统只有三种**：`darwin` / `linux` / `win32`（代码里是 `assert`）；
2. **架构标签有五个**：`macosx_12_0_x86_64`、`macosx_12_0_arm64`、
   `manylinux2014_x86_64`、`manylinux2014_aarch64`、`win_amd64`；
3. 🔴 **Windows 只有 `win_amd64` —— 没有 `win_arm64`**。
   Windows on ARM 现在**没有官方 wheel**；
4. 架构判断用的是 `platform.processor()` 返回的**字符串比较**
   （`architecture or platform.processor()`），不是一个枚举 ——
   在少见组合上这个判断本身就不可靠，别拿它做能力探测。

### aarch64（ARM64）：Linux 上是**一等构建目标**

这不是"能装就行"，`.buildkite/linux_aarch64.rayci.yml` 里有一条**完整的
aarch64 流水线**，做的是：

* 构建并发布 `manylinux2014_aarch64` 的 **Python wheel**
  （`ray-wheel-build-aarch64`，Python **3.10 / 3.11 / 3.12 / 3.13 / 3.14**）
  与 **C++ wheel**（`ray-cpp-wheel-aarch64`）；
* 构建并推送 aarch64 的**镜像**：`ray` / `ray-extra` / **`ray-llm`**
  （`--architecture aarch64`；CUDA 覆盖 11.7.1-cudnn8 到 **13.0.0-cudnn**，
  ray-llm 是 py3.12 + cu13）；
* 在 aarch64 机器上**跑测试**：core（`//python/ray/tests/...`）、
  serve（`//python/ray/serve/...`）、llm（`//python/ray/llm/...`，`--except-tags gpu`）。

⚠️ **两条必须知道的限制**：

1. **aarch64 的测试矩阵比 x86 窄**：核心与 serve 的 aarch64 测试只跑
   **Python 3.10**，llm 只跑 **3.12**；x86 上是多版本并行。所以
   "aarch64 上某些 Python 版本的边角问题"在 CI 里**覆盖不到**；
2. **CI 构建 ≠ Docker Hub 上是多架构 manifest**。CI 明确会推送 aarch64 镜像，
   但"`docker pull rayproject/ray:2.58.0` 在 ARM 上能不能直接拿到原生镜像"
   取决于镜像仓库的 manifest 组织方式 —— **本书未确认这一点**，
   请用 `docker manifest inspect rayproject/ray:2.58.0` 自己看一眼。
   拿不到就显式拉 `-aarch64` 标签（CI 里用的就是 `ARCH_SUFFIX: "-aarch64"`）。

**macOS arm64（Apple Silicon）** 与 **macOS x86_64** 都有 wheel。
但 macOS 有两条额外限制（都与"它是个开发机"有关）：

* **对象存储容量被主动压到 2GB** —— `ray/_private/utils.py` 里
  `if sys.platform == "darwin": object_store_memory = min(object_store_memory,
  ray_constants.MAC_DEGRADED_PERF_MMAP_SIZE_LIMIT)`（2GB），
  注释写明是"避免性能退化"（issue #20388）。详见第 18 章 §18.3；
* **集群模式默认关闭** —— 见下（它和 Windows 是同一段源码管的）。

### Windows：**客户端 / driver 可以，集群节点不是官方支持路径**

这是本节最需要说清楚的一条。源码在
`python/ray/_private/ray_constants.py` 第 507 行起，原文是：

```python
# Whether to enable Ray clusters (in addition to local Ray).
# Ray clusters are not explicitly supported for Windows and OSX.
IS_WINDOWS_OR_OSX = sys.platform == "darwin" or sys.platform == "win32"
ENABLE_RAY_CLUSTERS_ENV_VAR = "RAY_ENABLE_WINDOWS_OR_OSX_CLUSTER"
ENABLE_RAY_CLUSTER = env_bool(
    ENABLE_RAY_CLUSTERS_ENV_VAR,
    not IS_WINDOWS_OR_OSX,
)
```

逐条读：

* **"Ray clusters are not explicitly supported for Windows and OSX"**
  —— 这句注释就是官方口径：**Windows / macOS 上多节点集群不是显式支持的**；
* **默认值被翻成 `False`**：在 Windows / macOS 上，`ray start` / `ray.init()`
  **默认不开集群模式**。单机使用不受影响；要"再加一个节点"必须显式设
  `RAY_ENABLE_WINDOWS_OR_OSX_CLUSTER=1`；
* **这不是文档里的说法，是 CLI 自己会打出来的提示**。Ray 仓库里的
  CLI 输出快照测试 `python/ray/tests/test_cli_patterns/test_ray_start_windows_osx.txt`
  里能看到那句：
  `RAY_ENABLE_WINDOWS_OR_OSX_CLUSTER=1 ray start --address='...'`
  —— 也就是说"在 Windows/macOS 上加节点需要这个开关"是产品行为，不是推测。

**只支持原生 CPython**：`python/setup.py` 里的
`is_invalid_windows_platform()` 把 **Cygwin / MSYS / MSYS2 / MinGW**
判为非法平台并直接抛 `OSError`（提示"请用官方原生 CPython"）。
所以"用 Git Bash 里的 Python 装 Ray"这条路是**被显式拒绝**的。

**Windows 上确实拿不到 / 走不了的功能**（每条都能从打包元数据或源码读出）：

| 功能 | 证据 | 后果 |
|---|---|---|
| **HAProxy direct-ingress 路径** | `python/setup.py` 的 serve extra：`"ray-haproxy>=2.8.25,<2.9.0; sys_platform == 'linux'"` | `ray-haproxy` **只在 Linux 上作为依赖安装**。Windows 上开 `RAY_SERVE_ENABLE_HA_PROXY=1` 没有默认二进制，得自己用 `RAY_SERVE_HAPROXY_BINARY_PATH` 指一个（该变量在 `ray/serve/_private/constants.py` 里有定义，注释写明"设置后优先于内置 ray-haproxy 包"） |
| **memray 内存剖析** | `python/setup.py` 的 observability extra：`"memray; sys_platform != 'win32'"` | Windows 上装不到 memray，第 31 章那套内存剖析手段在 Windows 上不可用 |
| **Unix domain socket 相关行为** | `ray/_private/utils.py: validate_socket_filepath()` 在 `win32` 上**直接 return**，注释是 "Don't check for Windows as it doesn't support Unix sockets." | 不是"支持"，而是"没走这条路"：macOS/Linux 上那条"Unix socket 路径太长"的 `OSError` 在 Windows 上**永远不会出现** |
| **节点 CPU 型号信息** | `ray/_private/utils.py: get_current_node_cpu_model_name()` 在**非 Linux**上直接 `return None`（它读 `/proc/cpuinfo`） | dashboard / 状态输出里的 CPU 型号字段在 Windows 与 macOS 上是空的 |
| **进程 fate-sharing 的实现** | `ray/_private/utils.py` 里 Windows 走 job object（`detect_fate_sharing_support_win32` / `win32_AssignProcessToJobObject`），Linux 走 `prctl`；`services.py` 里 `preexec_fn` 在 Windows 上是 `None` | 语义**不完全等价**：Windows 靠内核 job object 绑定生命周期，行为细节与 Linux 不同，别指望两边报错方式一致 |

**生产上的正确姿势**：KubeRay 的官方镜像、以及本书讲的部署形态（§17.1）
**全是 Linux**。所以 Windows 上的常见架构是：

```
Windows 开发机 / CI runner  ──（Ray Client / Jobs API / HTTP）──▶  Linux Ray 集群
        ↑ 只做 driver / 客户端                                    ↑ 真正的 worker
```

也就是说 **Windows 当"客户端"是正常用法**（`ray.init(address="ray://head:10001")`
或 `ray job submit`），**把 Windows 机器当成 worker 加进集群则不在官方支持范围内**。
如果你的环境里确实有必须留在 Windows 上的计算资源，务实的做法是在那台机器上
跑一个独立的单机 Ray（或干脆不进 Ray），通过 HTTP / 队列跟 Linux 集群交换数据。

### 平台支持一览

| 平台 | wheel | 单机 `ray.init()` | 多节点集群 | 备注 |
|---|---|---|---|---|
| **Linux x86_64** | ✅ `manylinux2014_x86_64` | ✅ | ✅ 一等 | 基准平台 |
| **Linux aarch64** | ✅ `manylinux2014_aarch64` | ✅ | ✅ 一等（有专门 CI） | 含 `ray-llm` aarch64 镜像；测试矩阵只覆盖 py3.10 / llm py3.12 |
| **macOS arm64** | ✅ `macosx_12_0_arm64` | ✅ | ⚠️ 需 `RAY_ENABLE_WINDOWS_OR_OSX_CLUSTER=1` | 对象存储上限 **2GB** |
| **macOS x86_64** | ✅ `macosx_12_0_x86_64` | ✅ | ⚠️ 同上 | 同上 |
| **Windows x86_64** | ✅ `win_amd64` | ✅ | ❌ **非官方支持**（需同名开关） | 仅原生 CPython；无 HAProxy 默认包；无 memray |
| **Windows on ARM** | ❌ **无 `win_arm64`** | ❌ | ❌ | `get_wheel_filename()` 里根本没有这个分支 |

> ⚠️ **判断规则**：这张表里"✅"指的是"**官方发了 wheel 并且这条路径有 CI**"，
> 不是"所有功能都等价"。**跨架构时最常踩的不是能不能装，而是
> ① 依赖 wheel 有没有 aarch64 版本（PyTorch / vLLM 这类），
> ② 自定义 Docker 基础镜像是不是多架构，
> ③ 二进制扩展（`ray._raylet`）的 ABI 是否匹配。**
> `pip install ray` 成功只证明第一层。

---

## 17.11 本章小结

* 部署形态四选一：本地多进程 / `ray start` 手动集群 / KubeRay / Anyscale。
  **Anyscale 正被 Nscale 收购（2026-07-30 宣布，待交割），但 Ray 开源项目不在
  交易范围**，已由 PyTorch Foundation 治理。
* KubeRay **v1.7.0 发布于 2026-08-20**：RayCronJob 加时区、History Server(beta)、
  RayCluster 的 `Recreate` 升级策略、原生 NetworkPolicy、cert-manager mTLS、
  token RBAC、GCS FT 用内嵌 RocksDB 替代外部 Redis、autoscaler v2 优先级感知
  worker group。**KubeRay Federation 至今只是 issue #4561 的提案。**
  RayService 增量升级的 beta/alpha 状态**存疑，本书不断言**。
* 资源规划要扣掉 system reserved（CPU 5%、内存 10%，带上下限），并且
  **给 head 留足够规格、别让 head 跑业务**。
* 可观测性：State API 是 **alpha**（要 `ray[default]` + dashboard）；Log API
  **只能看活节点**；Metrics 视图要自备 Prometheus；**2.52.0–2.56.1 有 OTel
  recorder 死锁**，症状是"job 提交了但永远不启动"。
* **安全是 Ray 的真实短板**：默认无鉴权是设计选择。CVE-2025-62593（CVSS 9.4，
  <2.52.0，浏览器路径 RCE）**2026-08-17 被 CISA 列入 KEV**，联邦机构 3 天内
  修补；CVE-2026-27482 修复于 2.54.0。缓解清单里最容易漏的是**清点 CI runner
  与开发机**。2.58 的实验性 Sandbox（gVisor）面向"必须跑不可信代码"的场景，
  不是给普通服务用的。
* 升级流程里最贵的一课是 **2.32→2.33 的排空回归**导致的 RayService HTTP 500
  （issue #64181 / PR #64454）。`RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S`
  **默认 `30.0` 秒、排空默认是开着的**（不是"默认关闭"——
  本书早先写反了，`0` 才是关掉），所以显式配置的理由是
  "**让 30 秒与你的 `terminationGracePeriodSeconds` 匹配**"，
  而不是"打开开关"。修复本身有 4 个已知问题（最坑的是
  `_is_node_drained` 字段没被 GCS 拷贝，轮询几乎永远等满超时），
  且实际处理时间可能达到配置值的约 2 倍。
* 四类高频事故：head 磁盘被打满、两套 autoscaler 不共享状态、object store
  配太小、镜像拉取慢。
* **多租户**：🔴 **Ray 原生没有配额机制**（`quota` 在 `ray_constants.py` /
  `node.py` / `ray_option_utils.py` 里零命中）。`ray.init(namespace=...)` 只是
  **命名分组，不是资源或权限边界**；`--num-cpus` / `@ray.remote(num_cpus=...)`
  是**资源核算**（防超发），不是配额（不防被抢光）；Ray **没有 fair-share**。
  真正的硬边界只有两个：**KubeRay 每租户一个 `RayCluster`**，以及
  **K8s namespace + `ResourceQuota` / `LimitRange`**（在节点层卡住，Ray 看不见）。
  共享一个 RayCluster 的多租户只适合内部互信团队，且至少要开
  `RAY_AUTH_MODE=token`。
* **平台边界**：官方 wheel 只有三种 OS（`darwin` / `linux` / `win32`）。
  **aarch64 Linux 是一等目标**（有完整 CI：`manylinux2014_aarch64` wheel、
  C++ wheel、`ray` / `ray-extra` / **`ray-llm`** aarch64 镜像，但测试矩阵只覆盖
  py3.10 / llm py3.12）；macOS arm64 与 x86_64 有 wheel，但**对象存储被压到 2GB**；
  **Windows 只有 `win_amd64`，没有 `win_arm64`**，且
  `ray_constants.py` 里那句 *"Ray clusters are not explicitly supported for
  Windows and OSX"* 就是官方口径 —— Windows / macOS 上**默认不开集群模式**，
  加节点要显式设 `RAY_ENABLE_WINDOWS_OR_OSX_CLUSTER=1`。
  Windows 上还缺 HAProxy 默认包（serve extra 写的是 `sys_platform == 'linux'`）
  与 memray（`sys_platform != 'win32'`），并且**只支持原生 CPython**
  （Cygwin / MSYS / MinGW 会被 setup.py 直接拒绝）。
  **生产上 Windows 的定位是"客户端 / driver"，不是集群节点。**

到这里，"工程实践"部分结束。第 18 章进入性能调优与反模式，
第 19 章做生态与选型，第 20 章谈现状与未来。
