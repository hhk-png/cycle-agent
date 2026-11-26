# Volcano 深入教程：从调度语义到可运行的 MiniVolcano

> 面向 Kubernetes 批处理、HPC 与 AI 平台工程师。既讲清 Volcano 的对象、算法、控制器、拓扑、设备和商业逻辑，也带你从零实现一个有测试、有场景、可运行的简化调度器。

## 这套教程解决什么问题

Kubernetes 默认调度器以 Pod 为单位。它擅长回答“这个 Pod 放哪个节点”，却不擅长回答平台团队真正关心的问题：

- 一个 64 卡训练作业能否**一起启动**？
- 两个团队共享 GPU 时，谁应该先用？
- 集群满了以后，高优先级作业能否抢占？跨队列如何回收？
- 32 个 Pod 是否应该一起等待、一起启动？
- 一个队伍暂时不用配额时能否借给另一个队伍？原队伍需要时如何收回？
- 作业应该放在同一个机架/交换机下吗？
- 集群同时有 A100、H100、昇腾，作业如何选型？
- 多集群/多租户/混部/推理/Agent 场景如何扩展？

Volcano 是 CNCF 旗下的云原生批处理系统，源于华为 kube-batch，2019 年开源，2022 年成为 CNCF Incubating 项目。它用 **Job / PodGroup / Queue** 三个核心对象，加上 **Session + Actions + Plugins** 的调度框架，回答上述问题。

本教程有九条主线：

1. **概念与架构**：Job、PodGroup、Queue、HyperNode、控制器、Webhook。
2. **调度内核**：gang、proportion、DRF、fairshare、SLA、preempt、reclaim、binpack、拓扑、NUMA、DRA。
3. **形式化与实验**：不变量、复杂度、14 个可复现实验、故障注入。
4. **工程与运维**：可观测性、安全、性能、生产落地、多集群、迁移与容量规划。
5. **可运行实现**：`mini-volcano/` 用 Go 实现一个不依赖 Kubernetes 的确定性模拟器，23 个插件、6 个 action、28 个自检查场景、141 个测试 PASS。
6. **验证工程**：不变量、随机化、负向测试、开发期真实缺陷复盘。
7. **扩展与上游工程化**：扩展层选择、CRD/API 变更链、插件与 Action、测试金字塔、feature gate、发布与 CVE 响应、上游协作。
8. **大模型工作负载**：预训练/微调/推理/Agent 的形状、并行策略、gang 尺寸、checkpoint/弹性、容量模型与参考架构。
9. **商业分析**：市场规模、竞争格局、TCO/ROI 与采购决策。

## 本版亮点

- **事实基线到 v1.16.0**（2026-10-28）：NamespaceQueue、fairshare、PodGroup 拓扑反亲和、独立 HyperNode controller、Unschedulable Job Cache；同时覆盖 v1.15 的 gang 粒度抢占、DRA 队列配额、多分片策略、Benchmark 框架。
- **35 章完整教程**：从“为什么需要 Volcano”到“商业分析”，再到“扩展开发与上游工程化”和“大模型工作负载”；新增形式化语义、端到端实验手册、迁移与容量规划、扩展工程化、LLM 工作负载五章。附录 A–W 覆盖 API、配置、排障、自测、综合轨迹、容量计算、故障演练与任务级 gang 设计笔记。
- **综合调度轨迹（附录 T）**：用 32 卡、3 队列、6 作业的 14 tick 复盘，把 gang、拓扑、借用、回收、抢占回滚、PDB 与重调度放进同一张账本，并给出四个变体与逐项复现命令。
- **37 个示例（36 个真实 Volcano YAML + 1 个 Mini JSON）**：PyTorch/MPI/TF/Ray、Cron、JobFlow、HyperNode、DRA、TDM、SLA、PodGroup、NamespaceQueue、PDB、ResourceQuota、PodGroup 反亲和、task 级 gang、minSuccess 提前完成、队列×节点组亲和、LLM 预训练/微调/弹性/推理、LLM 队列、调度器配置。
- **API 深度补强**：新增 task 级 `minAvailable`/`dependsOn`/`maxRetry`、`minSuccess`、作业级 `volumes`、Queue `spec.affinity`（required/preferred、strict）、`cdp` 冷却保护，以及 `subGroupPolicy`/`partitionPolicy` 的字段与约束；关键语义按 v1.16.0 源码核对（`job_info.go`、`nodegroup`、`cdp`、`job_controller_actions.go`）。
- **MiniVolcano 可运行实现**：`go test ./...` 全绿，`go run ./cmd/minivolcano -all` 28 个场景通过；在作业级 gang 之上补齐 task 级 `minTaskMember` 与 `minSuccess`，并新增 `task-gang`、`min-success` 两个场景。
- **可运行容量/ROI 计算器**：`scripts/capacity_model.py` 只依赖标准库，17 项自检；把 GPU 成本、队列 deserved、训练时间、KV cache、推理副本与 ROI 放进同一份可复算报告（附录 U）。
- **生产故障演练剧本库（附录 V）**：guarantee 超容量、Webhook 证书过期、抢占抖动、hard 拓扑死锁、DRA claim 泄漏、PDB 阻塞 drain、leader 抖动七个剧本，每个都有取证、止血、根治与复盘题。
- **深度补强**：第 26 章把资源偏序、DRF、回收安全性、抢占贪心、拓扑约束和 13 条不变量写成可检查命题；第 27 章给出 14 个实验；第 28 章给出迁移决策树、容量公式和基准方法；第 34.10.6 节给出一个 70B/20 QPS 的端到端推理容量例子。
- **扩展工程化**：第 33 章把“写一个 Volcano 扩展”拆成扩展层选择、CRD/API 变更链、生产级调度器插件与 Action、作业插件（`OnPodCreate`/`OnJobAdd`）、控制器/Webhook 证书、测试金字塔、feature gate、构建发布、CVE 响应与上游协作，并给出可照着做的代码骨架与检查清单。
- **大模型工作负载**：新增第 34 章，覆盖预训练/微调/推理/Agent 的作业形状、TP/PP/DP/EP 与 rank 布局、gang 尺寸与整除约束、checkpoint/弹性、vLLM/LWS/Kthena、容量与排队模型，并配 5 个 LLM 示例。
- **工程补深**：非 NVIDIA 加速器（13.12）、多租户威胁模型（17.12）、调度器热路径优化（18.10）、平台 API 与租户体验（19.13）、排队论与 SLA（26.13）。
- **附录补全**：Kubernetes 调度基础与 Volcano 对照（Q）、错误信息与事件索引（R）、35 道自测题与参考答案（S）、综合调度轨迹（T）、容量/ROI 计算器（U）、生产故障演练剧本（V）、任务级 gang 设计笔记（W）。
- **可解释性**：`-explain` 输出 Pending 证据链（含 PDB 阻断原因），`-what-if` 做配额反事实。
- **验证记录与独立审计**：`VERIFICATION.md` 记录命令、计数、环境与限制；多轮只读对抗性审计发现的缺陷已修复并补回归测试。
- **商业三章**：市场口径、竞争模型、512 卡集群 ROI 模型。

## 先跑起来

### 运行 MiniVolcano

需要 Go 1.24+（本教程在 Go 1.27 上验证）。

```bash
cd mini-volcano
go test ./... -count=1
go run ./cmd/minivolcano -list
go run ./cmd/minivolcano -scenario basic
go run ./cmd/minivolcano -scenario gang -explain default/train
go run ./cmd/minivolcano -all
```

预期：

```text
PASS basic
PASS gang
PASS task-gang
PASS min-success
...
PASS throughput
PASS shuffle
PASS pdb
PASS resourcequota
PASS nodegroup
PASS group-topology

28 scenarios, 28 passed, 0 failed
```

常用命令：

```bash
# 抢占轨迹
go run ./cmd/minivolcano -scenario priority -events 10
# 反事实：把 team-a 的 GPU capability 改成 2
go run ./cmd/minivolcano -scenario proportion -what-if team-a/nvidia.com/gpu=2 -no-check
# 指标
go run ./cmd/minivolcano -scenario basic -metrics
# 碎片整理：低利用率节点疏散 + binpack 重打包
go run ./cmd/minivolcano -scenario shuffle -events 12
# PDB：一次中断允许、第二次阻断
go run ./cmd/minivolcano -scenario pdb -explain default/high-2
# namespace 配额与节点组
go run ./cmd/minivolcano -scenario resourcequota -events 20
go run ./cmd/minivolcano -scenario nodegroup -events 20
# 分组拓扑反亲和
go run ./cmd/minivolcano -scenario group-topology -events 12
# 角色门槛：1 master + 4/8 worker
go run ./cmd/minivolcano -scenario task-gang -events 8
# 提前完成：两个分片成功即结束
go run ./cmd/minivolcano -scenario min-success -events 8
# 自定义调度器配置
go run ./cmd/minivolcano -scenario basic -config ../examples/config/minivolcano-scheduler.json
```

### 运行容量/ROI 计算器

不需要集群，只需要 Python 标准库：

```bash
cd Volcano-tutorial
python scripts/capacity_model.py --selftest
# capacity model selftest: 17 checks passed

python scripts/capacity_model.py \
  --gpus 512 --price 2 --baseline 0.55 --improved 0.65 \
  --annual-cost 500000 \
  --queue train:3:120:400 --queue infer:1:60:160
```

完整参数、公式与 512 卡 + 70B 算例见附录 U。

### 在真实 Kubernetes 上体验 Volcano

```bash
helm repo add volcano-sh https://volcano-sh.github.io/helm-charts
helm repo update
helm install volcano volcano-sh/volcano \
  -n volcano-system --create-namespace \
  --version 1.16.0

kubectl get pods -n volcano-system
kubectl get crd | grep volcano
kubectl get queue

kubectl apply -f examples/queues/priorityclasses.yaml
kubectl apply -f examples/queues/queues.yaml
kubectl apply -f examples/jobs/vcjob-pytorch.yaml
kubectl get vcjob,podgroup -w
```

> 版本提示：Volcano 迭代很快。教程以 2026-11 的公开资料为准；安装命令中的版本号请替换成你实际使用的 release。升级前务必读目标版本的 release notes 与 CRD 变化。

## 知识地图

```text
                    ┌──────────────────────────────────┐
                    │ 01 为什么需要 Volcano            │
                    │ 02 架构与核心对象                │
                    └────────────────┬─────────────────┘
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          ▼                          ▼                          ▼
┌──────────────────┐      ┌──────────────────────┐    ┌──────────────────────┐
│ 03 安装/快速上手 │      │ 04 VolcanoJob 深入   │    │ 05 队列/配额/层级    │
└────────┬─────────┘      └──────────┬───────────┘    └──────────┬───────────┘
         └───────────────┬───────────┴───────────────┬───────────┘
                         ▼                           ▼
              ┌────────────────────┐      ┌────────────────────────┐
              │ 06 Gang            │      │ 07 调度器框架          │
              └─────────┬──────────┘      └───────────┬────────────┘
                        │                             │
        ┌───────────────┼───────────────┬─────────────┼──────────────┐
        ▼               ▼               ▼             ▼              ▼
 ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
 │ 08 公平    │ │ 09 抢占/   │ │ 10 打分/   │ │ 11 拓扑    │ │ 12 预留/   │
 │            │ │    回收    │ │    装箱    │ │            │ │ SLA/TDM    │
 └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
       └──────────────┴───────────────┴──────────────┴──────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
          ┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
          │ 13 异构/DRA      │ │ 14 作业生态  │ │ 15 控制器/Webhook│
          └────────┬─────────┘ └──────┬───────┘ └────────┬─────────┘
                   └──────────────────┼──────────────────┘
                                      ▼
                    ┌──────────────────────────────────┐
                    │ 16 可观测性  17 安全  18 性能    │
                    │ 19 生产落地  20 多集群            │
                    └────────────────┬─────────────────┘
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          ▼                          ▼                          ▼
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│ 21-25 MiniVolcano    │   │ 26 形式化语义        │   │ 27 实验手册          │
│ 设计/走读/场景/差距  │   │ 不变量/复杂度/证明   │   │ 14 个可复现实验      │
│ 验证                 │   │                      │   │                      │
└──────────┬───────────┘   └──────────┬───────────┘   └──────────┬───────────┘
           └──────────────────────────┼──────────────────────────┘
                                      ▼
                           ┌──────────────────────┐
                           │ 28 迁移/选型/容量    │
                           └──────────┬───────────┘
                                      ▼
          ┌──────────────────────────┼──────────────────────────┐
          ▼                          ▼                          ▼
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│ 29 未来方向          │   │ 30-32 商业分析       │   │ 33 扩展工程          │
│                      │   │ 市场/竞争/TCO        │   │ API/插件/测试/发布   │
└──────────┬───────────┘   └──────────┬───────────┘   └──────────┬───────────┘
           └──────────────────────────┼──────────────────────────┘
                                      ▼
                           ┌──────────────────────┐
                           │ 34 大模型工作负载    │
                           │ 训练/推理/Agent      │
                           └──────────┬───────────┘
                                      ▼
                           ┌──────────────────────┐
                           │ 35 附录 A–W          │
                           │ API/排障/自测        │
                           │ 轨迹/计算/演练       │
                           └──────────────────────┘
```

## 章节

| # | 章节 | 一句话 |
|---:|---|---|
| 01 | [为什么需要 Volcano](docs/01-why-volcano.md) | Pod 级调度器的边界与 Volcano 的定位 |
| 02 | [架构与核心对象](docs/02-architecture.md) | 四组件、三类对象、状态机、调度链路、NamespaceQueue |
| 03 | [安装与快速上手](docs/03-installation.md) | Helm、第一个 VCJob、托管 K8s 差异、排障 |
| 04 | [VolcanoJob 深入](docs/04-volcano-job.md) | tasks、minAvailable/minTaskMember、dependsOn、minSuccess、volumes、作业插件 |
| 05 | [队列、配额与层级](docs/05-queues.md) | weight/capability/guarantee、proportion/capacity、NamespaceQueue、ResourceQuota、NodeGroup affinity/limits |
| 06 | [Gang 调度](docs/06-gang.md) | 事务、Pipelined、minTaskMember、gangPreempt、碎片 |
| 07 | [调度器框架](docs/07-scheduler-framework.md) | Session、actions、plugins、tiers、扩展点、源码地图 |
| 08 | [公平分享](docs/08-fairness.md) | proportion、DRF、fairshare、SLA |
| 09 | [抢占与回收](docs/09-preemption-reclaim.md) | preempt vs reclaim、shuffle/rescheduling、PDB、cdp 冷却、抖动 |
| 10 | [节点打分与装箱](docs/10-node-scoring.md) | predicates、nodeorder、binpack、usage、NUMA |
| 11 | [拓扑感知](docs/11-topology.md) | task-topology、HyperNode、hard/soft、PodGroup 反亲和、subGroupPolicy/partitionPolicy |
| 12 | [预留、SLA、TDM 与混部](docs/12-reservation-sla-tdm.md) | guarantee、reservation 插件、时间窗、混部 |
| 13 | [异构设备与 DRA](docs/13-heterogeneous-dra.md) | GPU 型号、MIG/vGPU/deviceshare、DRA 配额、昇腾/AMD/国产 NPU |
| 14 | [作业生态与插件](docs/14-job-ecosystem.md) | JobFlow、Cron、LWS、Ray、Kubeflow、Spark |
| 15 | [控制器与 Webhook](docs/15-controllers-webhooks.md) | reconcile、admission、证书、故障 |
| 16 | [可观测性与运维](docs/16-observability-operations.md) | 指标、事件、SLO、Runbook |
| 17 | [安全与多租户](docs/17-security-multitenancy.md) | RBAC、隔离、CVE、加固、威胁模型 |
| 18 | [性能与规模化](docs/18-performance-scale.md) | 分片、Agent Scheduler、unschedulable cache、热路径优化 |
| 19 | [生产落地](docs/19-production-adoption.md) | 迁移、HA/DR、组织、iFLYTEK 案例、平台 API |
| 20 | [Volcano Global 与多集群](docs/20-volcano-global.md) | HyperJob、Karmada、数据感知 |
| 21 | [MiniVolcano 设计](docs/21-mini-design.md) | 目标、架构、对象映射、取舍 |
| 22 | [MiniVolcano 实现走读](docs/22-mini-walkthrough.md) | cache/session/statement/plugins/actions/controller、shuffle |
| 23 | [MiniVolcano 场景与实验](docs/23-mini-scenarios.md) | 28 个场景、explain、what-if、不变量 |
| 24 | [从 Mini 到生产](docs/24-mini-to-production.md) | 差距地图、三条迁移路径 |
| 25 | [验证工程](docs/25-verification.md) | 141 PASS、随机不变量、真实缺陷复盘 |
| 26 | [调度语义的形式化与不变量](docs/26-formal-semantics.md) | 资源偏序、DRF、回收安全性、抢占贪心、排队论、13 条不变量 |
| 27 | [端到端实验手册](docs/27-labs.md) | 14 个实验、预期输出、故障注入、归因模板 |
| 28 | [迁移、选型与容量规划](docs/28-migration-capacity.md) | 决策树、Kueue/YuniKorn/Slurm 迁移、容量公式、基准方法 |
| 29 | [未来方向](docs/29-future.md) | v1.17、推理、Agent、DRA、多集群 |
| 30 | [商业分析（一）：市场与价值链](docs/30-business-market.md) | 市场规模、价值链、客户与 TAM |
| 31 | [商业分析（二）：竞争格局](docs/31-business-competition.md) | Kueue/YuniKorn/KAI/Slurm/Run:ai |
| 32 | [商业分析（三）：TCO 与 ROI](docs/32-business-tco.md) | 512 卡模型、敏感性、定价、采购 |
| 33 | [从教程到上游：扩展开发与工程化](docs/33-extension-engineering.md) | 扩展层选择、CRD/API 变更链、调度器插件/Action/作业插件、测试金字塔、feature gate、发布与上游协作 |
| 34 | [大模型工作负载](docs/34-llm-workloads.md) | 预训练/微调/推理/Agent、并行策略、gang 尺寸、checkpoint/弹性、容量模型 |
| 35 | [附录](docs/35-appendix.md) | API、配置、术语、命令、Action/Plugin 全表、排障索引、配置配方、LLM 配方、FAQ、升级清单、覆盖矩阵、K8s 调度基础、错误索引、自测题、综合轨迹（T）、容量/ROI 计算器（U）、故障演练剧本（V）、任务级 gang 设计笔记（W） |

## MiniVolcano 场景

| 场景 | 验证点 |
|---|---|
| `basic` | enqueue→allocate→run→complete |
| `gang` | all-or-nothing |
| `task-gang` | 角色门槛：1 master + 4/8 worker，8 个 worker 不能替代 master |
| `min-success` | 2/4 成功即 Completed，未启动副本被终止 |
| `priority` | 同队列优先级抢占 |
| `reclaim` | 跨队列回收 |
| `proportion` | 加权份额 3:1 |
| `drf` | 主导资源公平 |
| `binpack` | 装箱 |
| `topology` | HyperNode hard |
| `group-topology` | PodGroup 级拓扑反亲和（required/preferred） |
| `task-topology` | ps/worker 亲和 |
| `reservation` | 时间窗预留（教学抽象） |
| `sla` | 等待预算 |
| `fairshare` | 历史用量 |
| `hierarchy` | 层级队列 cap/guarantee |
| `numa` | 单 NUMA 约束 |
| `tdm` | 时分复用窗口 |
| `retry` | RestartJob 与 maxRetry |
| `cron` | 周期作业与历史清理 |
| `jobflow` | DAG 依赖 |
| `backfill` | BestEffort |
| `heterogeneous` | A100 选择 |
| `throughput` | 批量完成 |
| `shuffle` | 低利用率节点疏散 + binpack 重打包 |
| `pdb` | PDB 允许一次中断、阻断第二次，gang 抢占回滚 |
| `resourcequota` | namespace 配额准入（2+1 通过，3 拒绝） |
| `nodegroup` | 队列限定节点组，组容量上限 |

## 验证

```bash
cd mini-volcano
gofmt -l .
go vet ./...
go build ./...
go test ./... -count=1
go run ./cmd/minivolcano -all

cd ..
python scripts/check_docs.py
python scripts/capacity_model.py --selftest
```

结果：

| 项 | 结果 |
|---|---|
| Go 包 | 9 个测试包全部 `ok` |
| 顶层测试 | 129 PASS |
| 随机不变量子测试 | 12 PASS |
| 合计 PASS 行 | 141 |
| 场景 | 28/28 passed |
| gofmt | 无输出 |
| go vet | 通过 |
| go build | 通过 |
| `-race` | 未运行（CGO 关闭） |
| 容量模型自检 | `capacity model selftest: 17 checks passed` |

详见 [VERIFICATION.md](VERIFICATION.md)。

## 目录结构

```text
Volcano-tutorial/
├── README.md
├── VERIFICATION.md
├── docs/                 # 35 章（34 章正文 + 附录）
├── examples/             # 37 个 YAML/JSON 示例
│   ├── queues/
│   ├── jobs/
│   └── config/
├── mini-volcano/         # Go 实现
│   ├── cmd/minivolcano/
│   ├── internal/{api,cache,framework,plugins,actions,controller,scheduler,sim,scenario,metrics}
│   └── README.md
└── scripts/
    ├── check_docs.py
    ├── capacity_model.py      # 容量/排队/ROI 计算器（标准库）
    └── verify_all.sh
```

## 版本与事实基线

| 事实 | 值 |
|---|---|
| 最新稳定版 | Volcano v1.16.0（GitHub tag 2026-10-14；CNCF 公告 2026-10-28） |
| v1.16 依赖 | Kubernetes 1.36.1，Go 1.26 构建 |
| CNCF 状态 | Incubating（截至 2026-11） |
| 开源协议 | Apache-2.0 |
| 前身 | kube-batch |
| 开源时间 | 2019-06 |
| CNCF Incubating | 2022-04 |

## 本次修订（2026-11-26，第九轮）

第九轮不改章节数（35 章）与插件/action 计数（23/6），把 MiniVolcano 从“只有作业级 gang”推进到**角色级 gang + 提前完成**，并补齐相应的测试、场景、示例与文档：

- **task 级 `minAvailable` → `minTaskMember`**：`TaskSpec.MinAvailable` 进入 API；`SubmitJob` 生成 `PodGroup.MinTaskMember`；`gang` 插件的 `JobValid/JobReady/JobStarving`、`allocate` 的提交条件、控制器的 Running 判定都同时检查“总数”和“每个角色”。
- **生效条件与上游一致**：`job.minAvailable < Σ task.minAvailable` 时角色门槛静默失效（`Job.RoleGatesActive()`），不会把作业锁死；`job.minAvailable` 省略时默认取 Σ task 最小值，没有 task 门槛时回退到总副本。
- **`minSuccess` 提前完成**：`JobSpec.MinSuccess` 达到即 Completed；尚未启动的副本被标记为终止，不泄漏 Pending；全部终止仍不足时仍走失败判定。
- **两个新场景**：`task-gang`（1 master + 4/8 worker，容量 5，证明“8 个 worker 不等于 gang 满足”）与 `min-success`（2/4 成功即完成，资源全部释放）。
- **十一个新测试**：角色门槛就绪、`minTaskMember` 默认值、角色门槛生效条件、`minSuccess` 校验、`minResources` 角色优先预留、`enableJobStarving=false` 不注册 hook、allocate 两轮门槛、allocate 角色不可用回滚、backfill 不部分提交、preempt 角色门槛、controller Running 门槛；测试 130→141（顶层 118→129），场景 26→28。
- **一个新示例**：`examples/jobs/vcjob-min-success.yaml`；示例 36→37（YAML 35→36）。
- **文档同步**：4.2.4/4.8、6.2.2、21.4/21.9、22.4.1、23.2/23.3、24.2.12、25.3/25.4/25.7.14、26.10、27.14、附录 E/F/P 的计数和边界。
- **新增附录 W（任务级 gang 设计笔记）**：把需求、数据模型、事务可见性、状态机、I13 测试矩阵、八个实现期语义缺陷、与上游差异和扩展清单整理成一份可复用的设计文档。
- **独立对抗性复核**：修正 preempt/reclaim 提交不带角色门槛、backfill 逐 Pod 绕过 gang、单轮 TaskOrder 在 `a1+b2` 形态下活锁、`enableJobStarving=false` 仍注册 hook、`MinResources` 高估准入预算、shuffle/rescheduling 只保护总数等问题；新增针对性回归测试，并把 `task-gang` 场景“因名称排序而假绿”的盲区写进第 25.7.14 节。
- **边界仍然明确**：`dependsOn`、作业级 `volumes`、Queue `spec.affinity`、`subGroupPolicy`/`partitionPolicy`、作业插件、`usage`/`deviceshare`/`gangPreempt` 仍未在 Mini 中实现；Mini 的显式 `minAvailable: 0` 仍是“关闭 gang”的教学扩展，上游 webhook 则会把 0 补成 Σ task 最小值。

## 第八轮（2026-11-26）

第八轮不改章节数（35 章）与 MiniVolcano 的代码/插件/action/场景/测试计数（23/6/26/130），新增 2 个示例（34→36），把 API 语义补到 v1.16.0 源码级，并新增作业插件开发一节：

- **task 级 gang**：4.2.4/6.2.2 讲清 task `minAvailable` → PodGroup `minTaskMember` 的数据流、`job.minAvailable >= Σ task.minAvailable` 的生效条件，以及 `dependsOn`、task 级 `maxRetry`、`minSuccess`、作业级 `volumes`。
- **队列 × 节点组**：5.7.3 新增 Queue `spec.affinity`（节点组亲和/反亲和、`strict`、`enablePreferredOrder`），并说明它与 `nodegroup-resource-limits` 注解的分工。
- **子组与分片**：11.6 用 v1.16 的 `subGroupPolicy` 与 `partitionPolicy` 字段重写，给出 1024 卡示例、字段约束与三者的选型。
- **cdp 冷却保护**：9.7.1 讲受害者过滤、`volcano.sh/cooldown-time`、配置与边界。
- **托管集群与 NUMA 策略**：3.11 新增 EKS/GKE/AKS/ACK 的节点初始化、污点、autoscaler×gang、存储/网络、控制面权限差异；10.6.1 新增 task `topologyPolicy`；20.2.4 补 `extendClusters` 与 job-forwarding。
- **作业插件开发**：33.6 新增 `PluginInterface`、注册、幂等、ConfigMap 例子、测试与上线清单，并把扩展层从六种扩为七种。
- **示例**：新增 `jobs/vcjob-task-gang.yaml`、`queues/queue-nodegroup-affinity.yaml`；示例 34→36（YAML 33→35）。
- **修正十余处源码级错误**：Queue 没有 `spec.state`，开/关应使用 `vcctl queue operate`（底层创建 `Command`，控制器维护 `status.state`）；`Inqueue` 是 PodGroup phase，不是 Job phase，Job 在 `minAvailable` 个 Pod Running 前一直是 `Pending`；`minAvailable: 0` 在创建时会被 webhook 补成 Σ task 有效 minima，不能用来关闭 gang；NUMA 拓扑 CRD 是 `Numatopology`（numatopo），不是 “NodeInfo CRD”；v1.16 Helm 的调度器配置键是 `volcano-scheduler.conf`，不是 `default-scheduler.conf`；`enablePreemptable`/`enableHierarchy`/`enableJobStarving` 是 plugin option（与 `name` 同级），写进 `arguments` 会静默失效；`gangpreempt`/`gangreclaim` action 名全小写，`lifecycle` 是 CCE 等发行版扩展而非 upstream v1.16；指标名改为 v1.16 实际名称（`volcano_schedule_attempts_total` 等），删除不存在的 `volcano_scheduler_session_total`；修正 nodegroup `strict` 语义（队列无 affinity 时任何节点都不落，不是“只落带标签节点”）；修正生命周期事件/动作表（`TerminateTask` 不存在，补 `RestartPod`/`RestartPartition`/`ResumeJob`，`Unknown` 不是 `JobUnknown`）；修正 `rescheduling` 的嵌套 `strategies` 结构、`usage.thresholds` 与 `deviceshare.*` 参数形状。
- **关键语义澄清**：`tasks[].dependsOn` 与严格 gang 叠加会死锁（下游 Pod 未创建、`JobValid` 永远凑不齐），用 dependsOn 时把 `minAvailable` 保持在第一阶段可满足的值或改用 JobFlow；PodGroup 名是 `<job>-<uid>` 而非 Job 名；`minSuccess` 在终止判定时仍需同时满足 `minAvailable`；`subGroupPolicy` 由 Job 的 `partitionPolicy` 生成，`minSubGroups: 0` 不做子组 gang 门控。
- **源码核对**：对 v1.16.0 tag 的 `JobSpec`/`TaskSpec`/`PodGroupSpec`/`QueueSpec`、`job_info.go`、`nodegroup`、`cdp`、`job_controller_actions.go` 逐项核对；新增字段未在真实集群 apply。
- **README**：把第三至七轮的修订记录压缩为下表，减少重复叙事。

## 修订历史

| 轮次 | 主要变化 | 计数 |
|---|---|---|
| 9 | Mini 的 task 级 gang 与 `minSuccess`；2 场景 + 11 测试 + 1 示例 | 35 章 / 37 示例 / 28 场景 / 141 PASS |
| 8 | v1.16 API 语义；作业插件开发；2 个示例 | 35 章 / 36 示例 / 26 场景 / 130 PASS |
| 7 | 附录 T/U/V；`capacity_model.py`；34.10.6 与 32.2.2 | 35 章 / 34 示例 / 130 PASS |
| 6 | 第 34 章 LLM 工作负载；5 个 LLM 示例；13.12/17.12/18.10/19.13/26.13 | 35 章 / 34 示例 / 130 PASS |
| 5 | 第 33 章扩展工程化；附录顺延 | 34 章 / 29 示例 / 130 PASS |
| 4 | 20 个主题补深；附录 Q/R/S | 33 章 / 29 示例 / 130 PASS |
| 3 | group-topology-affinity 插件与场景；治理插件复核 | 33 章 / 26 场景 / 130 PASS |
| 1–2 | 初版；shuffle/rescheduling；PDB/ResourceQuota/NodeGroup | 场景 22→26 |

## 限制

- MiniVolcano 不是 CRD、不是生产调度器；没有 informer/API Server/并发/DRA/多集群/安全。
- MiniVolcano 的 PDB / ResourceQuota / NodeGroup / group-topology-affinity 是简化模型：没有 `disruptionsAllowed`、Eviction API，配额只在 enqueue 检查，NodeGroup 用类型字段模拟上游 `spec.affinity` + 分组上限，拓扑反亲和用数字 tier 而非 `topologyTierName`。
- MiniVolcano 实现了作业级 `minAvailable`、task 级 `minTaskMember` 与 `minSuccess`，但**不实现** `dependsOn`、作业级 `volumes`、Queue `spec.affinity`/`strict`、`subGroupPolicy`/`partitionPolicy` 与作业插件；这些是真实 Volcano 的语义，见第 4、5、6、11、33 章。
- Mini 的 task 级门槛只在 `job.minAvailable >= Σ task.minAvailable` 时生效，与上游“低于则跳过角色检查”的行为一致；`dependsOn` 与严格 gang 叠加会死锁，Mini 没有实现 dependsOn，也没有模拟这个死锁。
- MiniVolcano 的 `minSuccess` 会把尚未启动的副本标记为 `Succeeded` 并释放配额；真实 Volcano 通过控制器终止/删除这些 Pod，观测到的 Pod 历史不同。
- 真实 Volcano 没有 Reservation CRD；教程中的 `Reservation` 是教学抽象。
- 示例中的镜像、节点名、GPU 标签、DRA DeviceClass、PVC、NCCL 网卡名需要按集群替换。
- 第 34 章的显存/算力/KV cache/排队公式是工程估算，不是 benchmark 承诺；LLM 示例中的训练脚本、vLLM 多机启动参数是占位。
- `scripts/capacity_model.py` 是透明的工程估算模型，不建模网络拥塞、存储 IO、故障率与调度开销；默认输入不是任何真实集群的 benchmark。
- 市场数字口径不同，不可直接比较；ROI 是模型示例，不是承诺。
- 教程以 2026-11 的公开资料为准，版本迭代后请查 release notes。

## 参考资料

完整列表见 [附录 G](docs/35-appendix.md)。核心入口：

- [Volcano 官网](https://volcano.sh/)
- [架构](https://volcano.sh/docs/home/architecture/)
- [调度器配置](https://volcano.sh/docs/userguide/user_guide_how_to_configure_scheduler/)
- [v1.16 发布（CNCF）](https://www.cncf.io/blog/2026/10/28/volcano-v1-16-released-namespacequeue-fairshare-and-a-broader-hypernode-ecosystem/)
- [iFLYTEK 案例](https://volcano.sh/blog/iflytek_case_study/)
- [Kueue](https://kueue.sigs.k8s.io/)

---

如果这套教程帮你把“作业为什么不跑”从猜测变成证据链，它的目的就达到了。
