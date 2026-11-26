# 第 33 章 从教程到上游：扩展开发与工程化

前 32 章回答了三个问题：Volcano 怎么工作、怎么用、怎么选。这一章回答第四个问题：**当你需要 Volcano 没有的能力时，怎样把想法变成可测试、可升级、可回滚的扩展，而不是一个只能在自己机器上跑的 fork。**

第 7 章讲过插件扩展点，第 15 章讲过控制器骨架，第 21–25 章用一个可运行的 MiniVolcano 复刻了核心语义。本章不再重复这些基础，而是补上从“能跑”到“能维护”之间的工程链条：

```text
选扩展层 → 改 API → 写调度器插件/Action/作业插件 → 补控制器/Webhook
        → 测试金字塔 → 版本与 feature gate → 构建发布 → 上游协作
```

> 本章的代码是工程示意，函数签名、目录名和 Makefile 目标以你使用的 Volcano 版本为准。涉及上游流程的地方会明确标注“以仓库为准”，避免把某个版本的细节当成永久契约。

## 33.1 先选扩展层：七种做法与代价

很多团队的第一反应是“fork 一份改”。但在 Volcano 里，绝大多数需求都能在更小的扩展层解决。先看一张决策表：

| 扩展层 | 典型位置 | 决策延迟 | 与上游升级的耦合 | 适合做什么 | 主要风险 |
|---|---|---|---|---|---|
| 调度器插件 | `pkg/scheduler/plugins/` | 进程内，微秒–毫秒 | 中：跟随 scheduler 接口 | 排序、过滤、打分、准入、受害者提名 | hook 组合语义写错，静默失效 |
| Action | `pkg/scheduler/actions/` | 进程内，一轮 Session | 高：依赖 Session/Statement | 新增流水线阶段、批量事务 | 直接改 cache、回滚不完整 |
| 作业插件 | `pkg/controllers/job/plugins/` | Pod 创建前 | 低：接口小且稳定 | 注入 env/volume、创建 Service/ConfigMap、外部登记 | 不参与调度决策，但可改 Pod spec 间接约束放置；必须幂等 |
| Extender | 独立 HTTP 服务 | 网络往返，毫秒–数十毫秒 | 低：协议稳定 | 多语言策略、外部数据、已有决策服务 | 超时、雪崩、缓存一致性 |
| CRD + Controller | `staging/src/volcano.sh/apis` + `pkg/controllers/` | 不进调度热路径 | 中：API 版本 | 新对象、生命周期、作业编排 | CRD 升级/转换/finalizer |
| Webhook | `pkg/webhooks/` | 准入时，毫秒 | 中：API 版本 | 默认值、合法性校验、策略门禁 | 证书、failurePolicy、全集群拒写 |
| 独立调度器 | 新进程/新项目 | 自主 | 低（但运维成本高） | 模型与 Volcano 根本不同 | 双调度器、配额、HA、生态 |

一个实用的决策顺序：

```text
1. 只改“谁先跑、放哪、能不能放”         → 调度器插件
2. 需要新的调度阶段或批量事务             → Action（先确认插件 hook 不够）
3. 只改 Pod 模板/周边资源/外部登记         → 作业插件
4. 决策逻辑已在外部服务/多语言实现         → Extender
5. 需要新对象、新生命周期                 → CRD + Controller
6. 只做创建/更新时的校验与默认值           → Webhook
7. 作业模型、调度单位、并发模型都不同       → 独立调度器
```

六个常见反模式：

1. **把硬约束写进 NodeOrder**：分数再低也只是偏好，阻止不了违规放置；硬约束必须用 Predicate 或域选择。
2. **在控制器里做放置决策**：控制器不知道节点忙闲，也无法参与事务；这会制造“创建了但永远 Pending”的作业。
3. **用 Webhook 做配额**：Webhook 只看对象，不看集群实时账本；配额应放在 enqueue/allocate 或 Kueue 准入层。
4. **为了一个 hook  fork 整个调度器**：插件能解决的事情，fork 会把升级、CVE、社区修复全部变成自己的负债。
5. **把 Action 当成“大插件”**：Action 直接操作 Statement 和 Session，回滚责任更重；能用插件 hook 表达就不要写 Action。
6. **Extender 里做 O(节点数) 的同步调用**：每个 Pod 一次网络往返，延迟会随规模爆炸；必须批量、缓存、超时降级。

## 33.2 仓库、模块与构建

Volcano 的代码分成两个 Go module：

- **主仓库** `volcano.sh/volcano`：scheduler、controllers、webhooks、CLI、installer、e2e。
- **API 仓库** `volcano.sh/apis`：CRD 类型、deepcopy、client/informer/lister。主仓库通过 `staging/src/volcano.sh/apis` 暂存 API 改动，合并后再同步到独立仓库。

主仓库的关键目录：

```text
volcano/
├── cmd/
│   ├── scheduler/                 # vc-scheduler
│   ├── controller-manager/        # vc-controller-manager
│   ├── webhook-manager/           # vc-webhook-manager
│   ├── agent/                     # vc-agent（Agent Scheduler）
│   └── cli/                       # vcctl
├── pkg/
│   ├── scheduler/
│   │   ├── actions/               # enqueue/allocate/preempt/reclaim/backfill/shuffle
│   │   ├── plugins/               # gang/priority/drf/proportion/capacity/...
│   │   ├── framework/             # Session/Statement/Plugin 接口
│   │   ├── cache/                 # informer cache、Snapshot、Bind/Evict
│   │   └── metrics/               # Prometheus 指标
│   ├── controllers/               # Job/PodGroup/Queue/CronJob/JobFlow/HyperNode
│   ├── webhooks/                  # admission 的 mutate/validate
│   └── cli/                       # vcctl 实现
├── staging/src/volcano.sh/apis/   # API 类型的暂存区
├── installer/                     # 清单与 Helm chart
├── hack/                          # 代码生成、e2e、工具脚本
├── test/e2e/                      # e2e 套件
└── benchmark/                     # v1.15+ 的基准框架
```

构建与测试的常用入口（以目标版本的 `Makefile` 为准）：

```bash
git clone --depth 1 --branch v1.16.0 https://github.com/volcano-sh/volcano.git
cd volcano

# 构建全部二进制到 _output/bin/
make

# 只构建某个组件
make vc-scheduler
make vc-controller-manager
make vc-webhook-manager
make vcctl

# 构建镜像
make images

# 单元测试（会跑 -race；Windows 需要 cgo 或改用 Linux 容器）
make unit-test

# e2e：创建 KinD 集群、加载镜像、安装 Volcano、跑套件
make e2e
make e2e-test-schedulingaction
```

几个容易踩的构建细节：

- **动态插件需要 CGO**：`SUPPORT_PLUGINS=yes make vc-scheduler` 会打开 Go plugin 支持；静态编译的插件应直接编进二进制，不要混用。
- **输出目录是 `_output/bin/`**，不是 `bin/`；脚本里写死路径时注意。
- **API 代码生成**：主仓库用 `hack/update-gencode.sh`，API 仓库/staging 用 `hack/update-codegen.sh`；生成 deepcopy、client、informer、lister、conversion、openapi。生成文件不要手改。
- **本地验证生成是否最新**：CI 通常有 `hack/verify-gencode.sh` 或同类脚本；提交前跑一次，避免“代码能编译但生成物过期”。

如果你只想验证调度语义，不想起集群，MiniVolcano 仍然是最快的路径：

```bash
cd Volcano-tutorial/mini-volcano
go test ./... -count=1
go run ./cmd/minivolcano -all
```

Mini 的 Session/Statement/插件 hook 与真实 Volcano 同构，但它是单进程、无 informer、无 API Server 的教学实现。它的作用是验证算法，不是验证 API、并发和发布流程。

## 33.3 给 CRD 加字段：一条完整的链

给 Volcano 加一个字段，远比“在 struct 里加一行”复杂。下面以一个**假设的** `spec.maxBurst` 为例（不是上游现有字段），走一遍从 API 到控制器的链路。

### 33.3.1 第一步：改 API 类型

API 类型在 `staging/src/volcano.sh/apis/pkg/apis/batch/v1alpha1/job.go`（以实际版本为准）：

```go
type JobSpec struct {
    // ... 既有字段

    // MaxBurst 是示例字段：允许该作业在队列内超配的副本数上限。
    // +optional
    MaxBurst int32 `json:"maxBurst,omitempty"`
}
```

三条规则：

1. **字段必须可选**：新字段的零值要能表达“未设置”，否则旧对象升级后语义会变。
2. **json tag 与 CRD schema 一致**：`omitempty`、类型、校验范围要同步到 CRD YAML。
3. **不要复用已有字段的语义**：新增字段就新增字段；把新语义塞进 `annotations` 虽然快，但无法做 schema 校验，也无法被客户端类型安全地读取。

### 33.3.2 第二步：生成 deepcopy 与客户端

```bash
# 主仓库
bash hack/update-gencode.sh

# 或 API staging 目录
cd staging/src/volcano.sh/apis
bash hack/update-codegen.sh
```

生成物包括：

- `zz_generated.deepcopy.go`：`DeepCopyInto`/`DeepCopy`；
- `pkg/client/clientset/...`：typed client；
- `pkg/client/informers/...`：shared informer；
- `pkg/client/listers/...`：只读 lister；
- 如新增版本，还会有 conversion 与 openapi。

三个常见错误：

- **只改 struct 不跑生成**：指针/切片字段的 deepcopy 缺失，运行期出现共享底层数组的 bug。
- **手改生成文件**：下一次生成会被覆盖，review 也看不出差异。
- **忘记 CRD schema**：Kubernetes 会按 schema 裁剪未知字段，新字段在 API Server 里“消失”，但本地单测（不经 API Server）仍然通过。

### 33.3.3 第三步：更新 CRD 与默认值

CRD 的 schema 通常在 `installer/` 或 `config/crd/` 下（以版本为准）。至少要做三件事：

1. 把 `maxBurst` 加进 `openAPIV3Schema`，声明类型、范围、默认值。
2. 如果字段有默认值，优先用 CRD 的 `default`；需要跨字段逻辑时再用 mutating webhook。
3. 如果字段影响调度，给 scheduler/controller 加 feature gate 或配置开关，保证旧集群升级后行为不变。

一个最小 schema 片段：

```yaml
spec:
  properties:
    maxBurst:
      type: integer
      format: int32
      minimum: 0
      default: 0
```

### 33.3.4 第四步：Webhook 校验

默认值之外，合法性校验放在 validating webhook：

```go
func (v *JobValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
    job, ok := obj.(*batchv1alpha1.Job)
    if !ok {
        return nil, fmt.Errorf("expected Job, got %T", obj)
    }
    if job.Spec.MaxBurst < 0 {
        return nil, field.Invalid(field.NewPath("spec", "maxBurst"),
            job.Spec.MaxBurst, "must be >= 0")
    }
    if job.Spec.MaxBurst > 0 && job.Spec.MinAvailable == 0 {
        return nil, field.Invalid(field.NewPath("spec", "maxBurst"),
            job.Spec.MaxBurst, "maxBurst requires gang (minAvailable > 0)")
    }
    return nil, nil
}
```

校验要回答三个问题：

- **单字段是否合法**：范围、枚举、格式。
- **跨字段是否自洽**：例如 `maxBurst` 与 `minAvailable`、`queue`、`priorityClassName` 的组合。
- **失败信息是否可操作**：告诉用户“哪个字段、什么值、为什么不行”，不要只返回 `invalid spec`。

### 33.3.5 第五步：控制器与调度器读取字段

如果字段只影响调度，改插件即可；如果影响 Pod 创建/状态推进，改 controller：

```go
func (r *JobReconciler) desiredReplicas(job *batchv1alpha1.Job) int32 {
    total := int32(0)
    for _, t := range job.Spec.Tasks {
        total += t.Replicas
    }
    if job.Spec.MaxBurst > 0 && job.Spec.MaxBurst < total {
        return job.Spec.MaxBurst
    }
    return total
}
```

控制器必须假设“字段可能来自旧版本对象”，因此：

- 零值走旧逻辑；
- 不要用“字段是否存在”判断版本，用显式的 feature gate 或 annotation；
- 状态回写要幂等，重复 Reconcile 不产生额外写操作。

### 33.3.6 第六步：测试与升级

| 层级 | 测什么 | 工具 |
|---|---|---|
| API 单测 | 序列化/反序列化、deepcopy 隔离 | `go test` |
| Webhook | 合法/非法/边界组合 | table-driven + fake client |
| Controller | 创建、更新、删除、终态 | envtest / fake client |
| Scheduler | 字段对放置的影响 | 场景测试 / KWOK |
| e2e | 真实 API Server + CRD + 作业 | KinD + `make e2e` |
| 升级 | 旧对象在新版本下的行为 | 旧 CRD 创建对象 → 升级 → 读回 |

升级顺序与回滚见 33.10。这里先记住一条：**新字段的默认行为必须等于旧行为**，否则升级就是一次隐式的策略变更。

## 33.4 写一个生产级调度器插件

第 7.12 节给了一个最小插件骨架。这一节补上“生产级”需要的四件事：参数、状态、错误与测试。

### 33.4.1 包结构与注册

```text
pkg/scheduler/plugins/tenantbudget/
├── tenantbudget.go        # 插件实现
└── tenantbudget_test.go   # 单元测试
```

注册通常放在 `pkg/scheduler/plugins/factory.go` 或插件的 `init()`：

```go
func init() {
    framework.RegisterPluginBuilder(PluginName, New)
}
```

插件名必须与 scheduler ConfigMap 中的 `name` 完全一致；拼错会让 scheduler 启动失败，而不是静默忽略。

### 33.4.2 参数、默认值与校验

```go
const PluginName = "tenant-budget"

type tenantBudget struct {
    weight float64
}

func New(arguments map[string]string) framework.Plugin {
    p := &tenantBudget{weight: 1}
    if v, ok := arguments["tenant-budget.weight"]; ok && v != "" {
        if f, err := strconv.ParseFloat(v, 64); err == nil && f >= 0 {
            p.weight = f
        }
    }
    return p
}
```

参数设计的三条经验：

1. **加插件名前缀**：`tenant-budget.weight`，不要用 `weight`，避免与其他插件冲突。
2. **非法值要显式报错**：能在启动时失败就不要在调度时静默降级；调度器的静默降级很难排障。
3. **给默认值一个理由**：默认值应代表“最保守/最不改变现有行为”的策略。

### 33.4.3 选择扩展点

| 你想改变 | 注册 | 返回语义 | 典型错误 |
|---|---|---|---|
| 作业排序 | `AddJobOrderFn` | -1/0/1 | 与 priority 同层，公平插件永远轮不到 |
| 队列排序 | `AddQueueOrderFn` | -1/0/1 | 忽略层级队列的父子关系 |
| 任务顺序 | `AddTaskOrderFn` | -1/0/1 | 把已 Running 的任务排在 Pending 前 |
| 节点硬过滤 | `AddPredicateFn` | error | 把“偏好”写成硬约束，作业全 Pending |
| 节点打分 | `AddNodeOrderFn` | float64, error | 分数方向与 binpack/nodeorder 冲突 |
| 入队门控 | `AddJobEnqueueableFn` | bool | 只看队列，不看 namespace 配额 |
| 合法性 | `AddJobValidFn` | error | 校验依赖集群状态，结果不稳定 |
| gang 就绪 | `AddJobReadyFn` | bool | 把 Succeeded 算成可运行副本 |
| 抢占/回收保护 | `AddPreemptableFn` / `AddReclaimableFn` | bool | 与 PDB/gang 的 AND 语义冲突 |
| 受害者提名 | `AddVictimTasksFns` | []*TaskInfo | 提名了但没做 gang/PDB 累计校验 |

### 33.4.4 一个可用的例子：按注解限制租户预算

需求：队列 `shared` 中的作业，如果带 `example.com/gpu-budget` 注解，则其 `minResources` 不得超过该预算；不带注解的作业不受影响。

```go
func (p *tenantBudget) OnSessionOpen(ssn *framework.Session) {
    ssn.AddJobEnqueueableFn(PluginName, func(job *api.JobInfo) bool {
        raw := job.PodGroup.Annotations["example.com/gpu-budget"]
        if raw == "" {
            return true
        }
        budget, err := strconv.ParseInt(raw, 10, 64)
        if err != nil || budget < 0 {
            return false
        }
        // MinResources 是 v1.ResourceList；索引得到 Quantity，Value() 取整数。
        need := job.PodGroup.Spec.MinResources["nvidia.com/gpu"].Value()
        if need > budget {
            return false
        }
        return true
    })
}

func (p *tenantBudget) OnSessionClose(ssn *framework.Session) {}
```

这段代码故意很“笨”，因为它展示了三个生产要点：

1. **门控放在 enqueue**：预算不足的作业不应进入 Inqueue，否则会占住队列份额。
2. **只读 PodGroup 的声明资源**：不要在这里遍历节点；enqueue 阶段做 O(节点) 计算会拖慢整个 Session。
3. **非法注解直接拒绝**：不要猜用户意图；返回 false 并让上层记录原因，比“尽量放行”安全。

真实实现还应做：

- 把拒绝原因写进事件/日志（例如 `tenant-budget: budget=4, need=8`）；
- 对注解值做缓存，避免每个 Job 重复解析；
- 如果预算需要跨队列汇总，使用 Queue 的 allocated 而不是 PodGroup 的 minResources；
- 在 `OnSessionClose` 里清理本次 Session 的临时状态（插件实例是长生命周期的）。

### 33.4.5 错误信息与可观测性

调度器的错误信息是排障的第一现场。三条要求：

- **包含对象与数值**：`queue shared: budget=4, need=8` 比 `insufficient budget` 有用得多。
- **稳定可断言**：错误文本不要带 map 遍历顺序、随机 ID；测试才能稳定。
- **区分“不可行”与“暂时不行”**：Predicate 失败通常是不可行；enqueue 失败可能是配额暂时不足。两者的事件类型和告警策略不同。

指标方面，优先复用 `pkg/scheduler/metrics` 的既有指标，不要为每个插件造一套新前缀。至少记录：

- 插件拒绝次数（按原因）；
- 插件执行耗时（如做重计算）；
- 被门控的作业数。

### 33.4.6 测试三层

```go
func TestTenantBudgetRejectsOverBudget(t *testing.T) {
    c := fixture(t)
    job := submit(t, c, jobWithAnnotation("j", "shared", "example.com/gpu-budget", "2"))
    ssn := session(t, c, 0, framework.PluginConfig{Name: "tenant-budget"})
    if ssn.JobEnqueueable(job) {
        t.Fatal("8-GPU job must be rejected by a 2-GPU budget")
    }
}
```

三层验证：

1. **纯单元**：构造 JobInfo/PodGroup，断言 hook 返回值；覆盖合法、非法、缺失注解、边界值。
2. **框架**：构造 Session，验证 tier 顺序与组合语义；尤其验证“另一个插件也注册了同一 hook”时的短路行为。
3. **调度/e2e**：在 MiniVolcano 场景或 KWOK 集群里跑一组作业，断言最终 Inqueue/Pending 布局，而不只是事件。

MiniVolcano 的 `internal/plugins/plugins_test.go` 和 `internal/scenario` 可以作为这三层的模板：单元测试验证 hook，场景验证端到端布局，`-all` 验证没有回归。

## 33.5 写一个 Action：事务、顺序与回滚

Action 是调度流水线的阶段。它比插件强大，也比插件危险，因为它直接决定“这一轮要不要改集群”。

### 33.5.1 接口与骨架

```go
type Action interface {
    Name() string
    Execute(ssn *framework.Session) error
}
```

一个 gang 粒度抢占的骨架（示意，不是上游 gangPreempt 的完整实现）：

```go
func (a *GangPreempt) Execute(ssn *framework.Session) error {
    for _, job := range ssn.Jobs {
        if !ssn.JobStarving(job) {
            continue
        }
        st := framework.NewStatement(ssn)
        placed := 0
        for _, task := range job.TaskScheduler() {
            node := a.pickNode(ssn, st, task)
            if node == nil {
                continue
            }
            if err := st.Allocate(task, node); err == nil {
                placed++
            }
        }
        if placed >= int(job.MinAvailable) {
            if err := st.Commit(); err != nil {
                return err
            }
        } else {
            st.Discard()
        }
    }
    return nil
}
```

### 33.5.2 六条硬规则

1. **一个作业一个 Statement**：gang 的回滚粒度是作业；多个作业共用一个 Statement 会让一个失败拖垮全部。
2. **不要直接改 cache**：所有变更走 `st.Allocate`/`st.Evict`；Commit 前对 Session 不可见。
3. **Commit 只在全部前置条件满足后调用**：包括 gang 就绪、PDB 允许、受害者释放的资源足够。
4. **驱逐和绑定要么都提交，要么都丢弃**：先驱逐后绑定失败，会制造“抢了但没跑”的空窗。
5. **动作要幂等**：同一轮重复执行、或下一轮重放，不应产生重复事件/重复驱逐。
6. **顺序要确定**：作业、任务、节点、受害者都显式排序；不要依赖 map 遍历。

### 33.5.3 Action 顺序不是装饰

| 顺序 | 后果 |
|---|---|
| `enqueue` 在 `allocate` 前 | 作业先进入 Inqueue，allocate 才有输入 |
| `preempt` 在 `allocate` 后 | 先尝试正常放置，失败再抢 |
| `reclaim` 在 `preempt` 后 | 先处理队列内，再处理跨队列 |
| `backfill` 在 `allocate` 前后 | 放前面先填空闲，放后面做兜底；语义不同 |
| `shuffle` 在 `allocate` 前 | 同一轮先腾挪再分配，容易把 Pod 放回原处 |
| `shuffle` 在 `allocate` 后 | 本轮只驱逐，下一轮重打包；需要打分方向配合 |

### 33.5.4 什么时候不该写 Action

- 只需要改变“谁先跑/放哪” → 插件；
- 只需要在准入时拒绝 → Webhook 或 enqueue 插件；
- 需要外部服务决策 → Extender；
- 需要新的对象生命周期 → Controller。

Action 的正确使用场景是“现有流水线缺少一个阶段”，而不是“插件 hook 不好用”。

## 33.6 写一个作业插件（Job Plugin）

调度器插件决定“放哪”，作业插件决定“Pod 长什么样、作业周边有什么”。它运行在 **Job controller** 里，在 Pod 创建前后介入，适合注入环境变量、创建 Service/ConfigMap/Secret、注册外部系统、生成 rank 配置。

### 33.6.1 接口与生命周期

真实 Volcano 的作业插件接口在 `pkg/controllers/job/plugins/interface`：

```go
type PluginInterface interface {
    Name() string
    OnPodCreate(pod *v1.Pod, job *vcbatch.Job) error
    OnJobAdd(job *vcbatch.Job) error
    OnJobDelete(job *vcbatch.Job) error
    OnJobUpdate(job *vcbatch.Job) error
}
```

四个回调的时机：

| 回调 | 时机 | 典型用途 | 失败后果 |
|---|---|---|---|
| `OnJobAdd` | 作业第一次被 controller 处理 | 创建 Service/ConfigMap/外部登记 | 作业卡住，反复重试 |
| `OnPodCreate` | 每个 Pod 创建前 | 注入 env、volume、label、annotation | 该 Pod 创建失败 |
| `OnJobUpdate` | 作业 spec/status 更新 | 同步外部状态 | 更新流程报错 |
| `OnJobDelete` | 作业被 kill/删除 | 清理外部资源 | 资源泄漏 |

两个实现细节决定了写法：

1. **插件实例是每次调用现构造的**。controller 每次执行 `pb(client, args).OnPodCreate(...)`，不会复用对象。不要把状态放在插件 struct 里，要放在 Job `status` 的受控字段（如 `ControlledResources`）或外部对象中。**不要依赖修改 Job 的 annotation/labels 来持久化**：controller 通常只 `UpdateStatus`，对 metadata 的写入会随深拷贝丢弃。
2. **`job.Spec.Plugins` 是 map，遍历顺序不确定**。多个插件之间不要假设先后依赖；每个插件都必须独立、幂等。

### 33.6.2 注册与参数

插件通过 builder 注册：

```go
// pkg/controllers/job/plugins/factory.go
type PluginBuilder func(pluginsinterface.PluginClientset, []string) pluginsinterface.PluginInterface

func init() {
    RegisterPluginBuilder("env", env.New)
    RegisterPluginBuilder("svc", svc.New)
    // ...
    RegisterPluginBuilder("tenant", tenant.New)
}
```

`[]string` 来自 Job：

```yaml
spec:
  plugins:
    tenant: ["--label=example.com/tenant"]
```

插件名写错时，validating webhook 会拒绝创建，错误信息是 `unable to find job plugin: <name>`；不要等到 Pod 起不来才排查。

### 33.6.3 一个可用的例子：租户信息注入

需求：提交作业时必须带 `example.com/tenant` 标签；控制器为作业创建一个 ConfigMap 记录租户，并给所有容器注入 `TENANT` 环境变量。

```go
package tenant

import (
    "context"
    "fmt"
    "strings"

    v1 "k8s.io/api/core/v1"
    apierrors "k8s.io/apimachinery/pkg/api/errors"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

    vcbatch "volcano.sh/apis/pkg/apis/batch/v1alpha1"
    pluginsinterface "volcano.sh/volcano/pkg/controllers/job/plugins/interface"
)

const PluginName = "tenant"

type tenantPlugin struct {
    client pluginsinterface.PluginClientset
    args   []string
}

func New(client pluginsinterface.PluginClientset, args []string) pluginsinterface.PluginInterface {
    return &tenantPlugin{client: client, args: args}
}

func (p *tenantPlugin) Name() string { return PluginName }

func (p *tenantPlugin) labelKey() (string, error) {
    key := "example.com/tenant"
    for _, a := range p.args {
        if strings.HasPrefix(a, "--label=") {
            key = strings.TrimPrefix(a, "--label=")
        }
    }
    if key == "" {
        return "", fmt.Errorf("tenant: --label must not be empty")
    }
    return key, nil
}

func (p *tenantPlugin) tenant(job *vcbatch.Job) (string, error) {
    key, err := p.labelKey()
    if err != nil {
        return "", err
    }
    t := job.Labels[key]
    if t == "" {
        return "", fmt.Errorf("job %s/%s: label %s is required",
            job.Namespace, job.Name, key)
    }
    return t, nil
}

func (p *tenantPlugin) OnPodCreate(pod *v1.Pod, job *vcbatch.Job) error {
    t, err := p.tenant(job)
    if err != nil {
        return err
    }
    env := v1.EnvVar{Name: "TENANT", Value: t}
    for i := range pod.Spec.Containers {
        pod.Spec.Containers[i].Env = append(pod.Spec.Containers[i].Env, env)
    }
    for i := range pod.Spec.InitContainers {
        pod.Spec.InitContainers[i].Env = append(pod.Spec.InitContainers[i].Env, env)
    }
    return nil
}

func (p *tenantPlugin) OnJobAdd(job *vcbatch.Job) error {
    t, err := p.tenant(job)
    if err != nil {
        return err
    }
    if job.Status.ControlledResources == nil {
        job.Status.ControlledResources = map[string]string{}
    }
    // 用 ControlledResources 做幂等标记：重复 reconcile 不重复写。
    if job.Status.ControlledResources["plugin-"+PluginName] == PluginName {
        return nil
    }
    ctx := context.Background()
    cms := p.client.KubeClients.CoreV1().ConfigMaps(job.Namespace)
    name := job.Name + "-tenant"
    owner := metav1.OwnerReference{
        APIVersion: "batch.volcano.sh/v1alpha1",
        Kind:       "Job",
        Name:       job.Name,
        UID:        job.UID,
    }
    cm, err := cms.Get(ctx, name, metav1.GetOptions{})
    if apierrors.IsNotFound(err) {
        _, err = cms.Create(ctx, &v1.ConfigMap{
            ObjectMeta: metav1.ObjectMeta{
                Name: name, Namespace: job.Namespace,
                OwnerReferences: []metav1.OwnerReference{owner},
            },
            Data: map[string]string{"tenant": t},
        }, metav1.CreateOptions{})
    } else if err == nil {
        owned := false
        for _, ref := range cm.OwnerReferences {
            if ref.UID == job.UID {
                owned = true
            } else if ref.Kind == "Job" && ref.APIVersion == "batch.volcano.sh/v1alpha1" {
                // 同名 ConfigMap 已属于另一个 Job：不要抢，直接报错。
                return fmt.Errorf("configmap %s/%s is owned by another job", job.Namespace, name)
            }
        }
        if cm.Data == nil {
            cm.Data = map[string]string{}
        }
        cm.Data["tenant"] = t
        if !owned {
            cm.OwnerReferences = append(cm.OwnerReferences, owner)
        }
        _, err = cms.Update(ctx, cm, metav1.UpdateOptions{})
    }
    if err != nil {
        return err
    }
    job.Status.ControlledResources["plugin-"+PluginName] = PluginName
    return nil
}

func (p *tenantPlugin) OnJobUpdate(job *vcbatch.Job) error { return nil }

func (p *tenantPlugin) OnJobDelete(job *vcbatch.Job) error {
    if job.Status.ControlledResources["plugin-"+PluginName] != PluginName {
        return nil // 不是本插件创建的，别删
    }
    ctx := context.Background()
    err := p.client.KubeClients.CoreV1().ConfigMaps(job.Namespace).
        Delete(ctx, job.Name+"-tenant", metav1.DeleteOptions{})
    if err != nil && !apierrors.IsNotFound(err) {
        return err
    }
    // 清掉标记：RestartJob 会 kill 再 initiate，标记不清会跳过重建。
    delete(job.Status.ControlledResources, "plugin-"+PluginName)
    return nil
}
```

几个工程要点：

- **OwnerReference 指向 Job**：即使 `OnJobDelete` 没执行，GC 也能回收 ConfigMap；但外部资源（数据库记录、DNS）没有 GC，必须自己清理。已存在的同名 ConfigMap 如果是“无主”的，要补 ownerReference；如果属于另一个 Job，直接报错而不是抢占。
- **`ControlledResources` 是幂等标记，不是事务**。写标记前先完成外部操作；如果顺序反过来，失败重试会误以为已完成。
- **`OnJobDelete` 要检查并清除标记**。`RestartJob` 会走 kill → Restarting → Pending → `initiateJob`；如果只删资源不清标记，下一次 `OnJobAdd` 会因标记命中而跳过重建。上游 `svc`/`ssh` 都做了这个 guard。
- **参数要解析并校验**。`args` 是 `Job.spec.plugins[name]` 的原始字符串列表；本例只认 `--label=`，空值在 `OnJobAdd` 就失败。不要“接收参数但不使用”，否则 YAML 看起来生效、实际是硬编码。
- **`OnPodCreate` 失败会让 Pod 创建失败**，但不会自动回滚 `OnJobAdd` 创建的 ConfigMap；清理逻辑要能处理“Job 从未真正启动”的情况。
- **不要在这里做调度决策**。作业插件看不到节点忙闲，也不参与 gang；它能改 Pod 的 `nodeSelector`/`affinity` 来约束放置，但不会为作业预留资源。

### 33.6.4 测试与上线

| 层级 | 做什么 |
|---|---|
| 单元 | fake clientset + 构造 Job/Pod，断言 env、ConfigMap、幂等 |
| 控制器 | envtest 创建 Job，确认 Pod 模板被注入、ConfigMap 有 ownerReference |
| e2e | KinD 提交带 `plugins.tenant` 的作业，确认 Pod Running 且 env 存在 |
| 负向 | 缺标签、ConfigMap 已存在、Job 删除后资源清理 |

上线检查：

- [ ] 插件名已在 `factory.go` 注册，webhook 能识别；
- [ ] `OnJobAdd`/`OnPodCreate`/`OnJobDelete` 都幂等；
- [ ] 外部资源有 ownerReference 或显式清理；
- [ ] 参数有前缀与默认值，非法参数在 `OnJobAdd` 就失败；
- [ ] 指标/日志包含 job、namespace、plugin；
- [ ] 回滚路径：从 `spec.plugins` 移除插件不会破坏已创建的作业（但外部资源可能残留）。

### 33.6.5 与调度器插件、Webhook 的边界

| 维度 | 作业插件 | 调度器插件 | Webhook |
|---|---|---|---|
| 运行位置 | Job controller | vc-scheduler | admission |
| 输入 | Job/Pod | Session/cache | AdmissionReview |
| 能否影响放置 | 不参与决策，但可改 Pod 的 nodeSelector/affinity 间接约束 | 能（选择节点） | 不能（只能拒绝/改写） |
| 典型失败 | Pod 创建失败 | 作业 Pending | 对象写入被拒 |
| 配置 | `Job.spec.plugins` | scheduler ConfigMap tiers | 注册到 webhook configuration |

一个常见误用：想在作业插件里“给高优先级作业留节点”。它拿不到节点视图，也不在调度事务里；这类需求应该写调度器插件（33.4）或 enqueue 门控。作业插件能改 Pod 的 `nodeSelector`/`affinity` 来**约束**放置，甚至直接写 `nodeName`（这会绕过调度器，变成 kubelet 直接绑定）；但它不会让调度器为作业预留资源，也不参与 gang 决策。

安全边界也要单独看：作业插件以 controller 的 ServiceAccount 运行，权限通常远大于提交作业的租户。它接收的是 Job/Pod 对象，不能把租户可控字段（Job 名、label、annotation）直接拼进高权限资源名或云 API 调用而不做校验；否则一个能提交 Job 的租户就可能借插件越权。

## 33.7 Extender、scheduler-plugins 与 Kueue 的边界

### 33.7.1 Extender

Volcano 的 `extender` 插件把部分决策委托给外部 HTTP 服务。常见端点：

| 端点 | 作用 | 返回 |
|---|---|---|
| `/filter` | 过滤节点 | 可行节点列表 |
| `/prioritize` | 节点打分 | 节点 → 分数 |
| `/preempt` | 提名受害者 | 受害者列表 |
| `/bind` | 外部绑定 | 成功/失败 |

工程约束：

- **延迟预算**：每个 Pod 一次调用，P99 必须远小于调度周期；批量接口优先。
- **超时与降级**：超时后是“放行”还是“拒绝”，必须显式配置；默认拒绝会让整个集群停摆。
- **缓存**：节点状态、策略结果要缓存；缓存失效要有 TTL 和版本号。
- **安全**：mTLS、鉴权、网络策略；extender 能影响整个集群的放置。
- **可观测性**：每次调用的耗时、错误率、拒绝原因都要有指标。

### 33.7.2 scheduler-plugins

scheduler-plugins 是 kube-scheduler 的插件集，coscheduling 提供轻量 gang。它适合：

- 作业规模不大；
- 只需要“一起调度”，不需要队列/回收/拓扑；
- 已经深度使用 kube-scheduler，不想再运维一个调度器。

它不提供 Volcano 的 Queue/proportion/reclaim/JobFlow，也不理解 PodGroup 的完整生命周期。

### 33.7.3 Kueue

Kueue 负责“作业何时准入”，不负责“Pod 放哪”。组合模式：

```text
Kueue：ClusterQueue/LocalQueue/Admission → 决定 Workload 何时 admit
Volcano：PodGroup/gang/拓扑/设备 → 决定 admit 后的 Pod 放哪
```

组合的最大风险是**两套配额账本**：Kueue 认为还有配额，Volcano 认为队列已满；或者反过来。上线前必须做“同一批作业、两套配额”的对账，明确谁是权威。

### 33.7.4 一张选择表

| 需求 | 首选 | 次选 | 不建议 |
|---|---|---|---|
| 只缺一个排序/过滤规则 | Volcano 插件 | Extender | fork |
| 新调度阶段 | Action | 独立调度器 | 塞进插件 |
| 多语言/外部策略 | Extender | 独立调度器 | Webhook |
| 只缺准入/配额 | Kueue | Volcano Queue | 自研准入 |
| 轻量 gang + 原生调度器 | scheduler-plugins | Volcano | 自研 coscheduling |
| 完整作业级调度 | Volcano | YuniKorn | 拼装多个插件 |

## 33.8 控制器与 Webhook 的工程细节

### 33.8.1 Volcano 控制器用什么

Volcano 核心控制器基于 **client-go informer + workqueue + 自定义 controller 框架**，不是 controller-runtime。自研控制器时，controller-runtime 更省事；但如果你要改 Volcano 本体，先读 `pkg/controllers/framework` 的现有模式，不要引入第二套运行时。

无论用哪套，以下四件事必须做对：

1. **Reconcile 幂等**：同一个 key 处理多次，第二次不应产生写操作。
2. **finalizer 顺序**：先清理子资源/外部资源，再摘 finalizer；反过来会泄漏。
3. **状态用 status subresource**：不要把状态写进 spec；更新冲突用 `RetryOnConflict`。
4. **事件只发状态迁移**：每次 Reconcile 都发 Normal 事件会刷爆事件表。

### 33.8.2 Webhook 的证书生命周期

Volcano 的 admission 证书由 `volcano-admission-init` Job 生成，写入 `volcano-admission-secret`。这是安装期最脆弱的一环：

- `failurePolicy: Fail` 时，证书没就绪会让**整个集群的 Pod 创建失败**；
- `kubectl rollout restart` **不会**重新生成证书；
- Helm 升级曾出现“每次升级轮换证书，导致 webhook 报 `x509: certificate signed by unknown authority`”的问题，后续版本改为复用有效证书（以目标版本为准）；
- 修复方式通常是删除 init Job 与 Secret，再重新 apply/upgrade；生产要有证书过期告警。

排查顺序：

```bash
kubectl -n volcano-system get job,pod | grep admission
kubectl -n volcano-system logs job/volcano-admission-init
kubectl -n volcano-system get secret volcano-admission-secret -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -dates
kubectl get validatingwebhookconfiguration,mutatingwebhookconfiguration | grep volcano
```

### 33.8.3 Webhook 的输入边界

CVE-2026-44247 是一个典型教训：webhook 对 HTTP body 没有大小上限，任意集群内 Pod 都能用超大 body 把 admission 打到 OOM。修复方式是 `http.MaxBytesReader` + 413。工程上要补三类边界：

- **请求体大小**（3 MiB 级别的硬上限）；
- **并发与超时**（避免慢请求占满 goroutine）；
- **失败策略**（Fail 还是 Ignore，要按“拒绝写入”与“集群可用性”权衡）。

### 33.8.4 事件与日志

控制器/调度器的日志要能回答“谁在什么时间对哪个对象做了什么决策”。建议：

- 结构化日志（JSON），至少包含 `component`、`object`、`queue`、`reason`；
- 事件只记录用户可见的状态迁移；
- 对高频重复原因做聚合，避免日志/事件风暴；
- 关键决策（抢占、回收、驱逐）同时记录“候选集”和“最终选择”。

## 33.9 测试金字塔：单元、envtest、KWOK、e2e

| 层级 | 工具 | 能证明什么 | 不能证明什么 | 成本 |
|---|---|---|---|---|
| 单元 | `go test`、table-driven | 纯函数/hook 的边界 | 真实 API/并发 | 低 |
| 框架 | fake client、Session fixture | 插件组合、Statement 回滚 | informer/leader election | 低 |
| envtest | API Server + etcd | CRD schema、默认值、webhook、controller | 真实 kubelet/设备 | 中 |
| KWOK | 模拟节点 | 大规模放置、调度吞吐 | 容器启动/网络/存储 | 中 |
| e2e | KinD + 真实组件 | 安装、升级、作业生命周期 | 生产规模 | 高 |
| benchmark | `benchmark/` | 固定场景下的吞吐/延迟 | 业务语义正确性 | 中 |
| 混沌 | 故障注入 | 节点 NotReady、API 抖动、证书过期 | 所有故障组合 | 高 |

上游命令（以 Makefile 为准）：

```bash
make unit-test
make e2e
make e2e-test-schedulingbase
make e2e-test-schedulingaction
make e2e-test-jobp
make e2e-test-stress

cd benchmark
make setup VOLCANO_VERSION=v1.16.0
make test-gang-env JOBS=10 REPLICAS=100 MIN_AVAILABLE=100
```

测试设计的三条原则：

1. **断言结果，不只断言事件**：事件只能证明“发生了什么”，不能证明“最终布局正确”。
2. **确定性优先**：固定 seed、显式排序、不要断言挂钟时间；e2e 里的等待用条件轮询，不用 `sleep`。
3. **负向测试和正向一样重要**：配额拒绝、PDB 阻断、gang 回滚、证书过期，都是生产故障的高频来源。

MiniVolcano 的场景是“语义测试”，不是 e2e：它能证明 gang 事务、回收边界、拓扑规则，但不能证明 API Server 的 admission、informer 的延迟、并发绑定冲突。两者要组合使用。

## 33.10 版本、feature gate 与兼容性

Volcano 的功能分布在三条独立的版本线上：

```text
CRD/API 版本        ←→  控制器/调度器镜像版本
feature gate        ←→  组件启动参数
插件/配置           ←→  scheduler ConfigMap
```

升级时最危险的不是镜像 tag，而是三者不一致。以 NamespaceQueue 为例：

- 需要 scheduler、controller-manager、admission **三处**都开 `NamespaceQueue=true`；
- 需要层级 `capacity`（`enableHierarchy: true`），且不要同时启用 `proportion`；
- 父 Queue 必须通过 `allowedNamespaces` 授权；
- 作业用 `namespace/<name>` 引用，NamespaceQueue 必须 `Ready=True` 且 `Authorized=True`；
- 深度默认 5 层，controller-manager 与 admission 的 `--max-namespacequeue-depth` 要一致；
- NamespaceQueue CRD 在 v1.16 没有 Helm 卸载保留注解，卸载前要备份。

只开一处 gate 的典型症状：

```text
对象能创建，但 controller 不处理     → controller 没开 gate
controller 处理了，但 scheduler 不认  → scheduler 没开 gate
scheduler 认了，但提交被拒            → admission 没开 gate 或 webhook 未注册
```

### 33.10.1 CRD 版本与转换

如果新增 CRD 版本（例如 `v1beta1` → `v1`）：

1. 定义新版本类型；
2. 实现 conversion（webhook 或转换函数）；
3. 指定 `storage: true` 的存储版本；
4. 更新 client/informer/lister；
5. 保证新旧版本往返转换不丢字段；
6. 测试“旧对象读出新版本、新对象被旧控制器读取”的双向兼容。

单版本 CRD 也不要掉以轻心：schema 的 pruning、默认值、nullable 变化都会影响旧对象。

### 33.10.2 升级与回滚顺序

```text
备份 CRD/配置/队列/作业模板
  → 读 release notes 与 CRD diff
  → 升级 CRD
  → 升级 controller-manager
  → 升级 scheduler
  → 升级 admission
  → 验证默认队列、示例作业、指标
```

回滚注意：

- CRD 回滚可能丢字段；
- 新控制器写入的状态，旧控制器可能不认识；
- 保留旧镜像 tag 与旧 ConfigMap；
- 先在 staging 用相同对象做一次“升级→回滚→再升级”。

## 33.11 构建、发布与供应链

### 33.11.1 构建可复现的产物

```bash
# 多架构镜像（需要 buildx）
make images

# 指定仓库/标签
HUB=registry.example.com/volcano TAG=v1.16.0 make images
```

发布产物至少包括：

- `vc-scheduler`、`vc-controller-manager`、`vc-webhook-manager`、`vcctl`、`vc-agent`；
- 多架构镜像（`linux/amd64`、`linux/arm64`，按需）；
- Helm chart 与 installer 清单；
- CRD 清单与 RBAC 清单；
- release notes、升级说明、已知问题。

工程建议：

- 构建加 `-trimpath`，记录版本/commit 到二进制；
- 生成 SBOM（SPDX/CycloneDX），对镜像做签名/来源证明（cosign/slsa）；
- 镜像用不可变 tag（digest）部署，不要依赖 `latest`；
- 把 CRD diff 纳入发布检查，而不只是镜像 diff。

### 33.11.2 发布检查清单

- [ ] 代码冻结，release branch 创建；
- [ ] 单元/e2e/benchmark 通过；
- [ ] CRD 变更与 conversion 测试通过；
- [ ] 升级/回滚演练通过；
- [ ] 文档、示例、release notes 更新；
- [ ] CVE 扫描与依赖审计；
- [ ] 镜像多架构、签名、SBOM；
- [ ] Helm chart 版本与 appVersion 对齐；
- [ ] 已知问题与 workaround 写入 release notes。

### 33.11.3 CVE 响应

Volcano 已有多个与调度器相关的安全公告，例如：

- **CVE-2026-44247**：webhook 无界 body → OOM；修复在 v1.12.4、v1.13.3、v1.14.2 及后续 patch 线。
- **GHSA-j38h-7pfq-cxmw**：DRA 容量核算的过度迭代 → 调度阻塞；v1.15.2 修复。

响应流程：

1. 确认受影响版本与触发条件；
2. 评估暴露面（webhook 是否集群内可达、DRA 是否启用）；
3. 先在测试集群验证 patch；
4. 按 patch 线升级，不要跨 minor 跳；
5. 更新镜像/Helm/清单，通知租户；
6. 复盘“为什么没有更早发现”，补测试或准入限制。

## 33.12 上游协作：issue、设计文档、PR 与 release

### 33.12.1 先设计，再写代码

上游不接受“先斩后奏”的大功能。一个可落地的流程：

1. **搜索 issue/PR**：确认不是重复；找到相关设计文档与历史讨论。
2. **开 issue 描述问题**：场景、复现、期望行为、影响范围；不要一上来贴实现。
3. **写设计文档**：放在 `docs/design/` 或 issue 中，覆盖 API、调度语义、失败模式、测试、升级/回滚。
4. **在社区会议/评论中达成一致**：尤其是 API 字段、feature gate、默认行为。
5. **拆 PR**：API/生成物、控制器、调度器、测试、文档分开；每个 PR 可独立 review。
6. **补 release note**：用户需要知道“怎么启用、怎么回滚、有什么限制”。

### 33.12.2 PR 检查清单

- [ ] 只做一件事，没有混入无关重构；
- [ ] 有单元测试，关键路径有 e2e/场景；
- [ ] 有负向测试（非法输入、配额拒绝、回滚）；
- [ ] 有 feature gate 或默认关闭，升级不改变旧行为；
- [ ] 更新了 API 文档、示例、release notes；
- [ ] 生成物已重新生成（deepcopy/client/informer/lister/CRD）；
- [ ] 错误信息可操作，指标/事件可观测；
- [ ] 通过 lint、unit、e2e、codegen 校验。

### 33.12.3 版本节奏与社区

Volcano 大约每 6–9 个月一个 minor，patch 线维护多个版本。参与方式：

- 关注 GitHub release 与 roadmap issue（例如 v1.16 roadmap #5365、v1.17 计划）；
- 参加社区会议、CNCF Slack、邮件列表；
- 从文档、测试、小 bug 开始，逐步到插件/控制器；
- 用可复现的 benchmark 和故障复盘建立技术影响力；
- 遵守项目的 DCO/CLA 与 review 规范（以 `CONTRIBUTING.md` 为准）。

一个务实的“第一次贡献”路径：

```text
文档错别字/失效链接 → 补一个单元测试 → 修一个 Pending/事件 bug
→ 给插件加一个参数 → 提一个设计文档 → 实现一个小 feature
```

不要从“重写调度器”开始。

## 33.13 本章小结

- 先选扩展层：多数需求是插件，不是 fork；Action/Extender/Controller/Webhook 各有边界。
- 改 API 是一条链：类型 → deepcopy/client → CRD schema → webhook → controller → 测试 → 升级。
- 生产级插件要管参数、状态、错误、指标和三层测试；Action 要管事务、顺序和回滚。
- 作业插件是控制器侧的第四个扩展面：只改 Pod 与周边对象，不参与调度决策，但可通过 Pod spec 约束放置；实例每次重建，必须幂等。
- 测试金字塔从单元到 e2e 各证明不同的东西，不能互相替代。
- 版本、feature gate、CRD 转换与升级/回滚是工程的一部分，不是发布后的补丁。
- 上游协作从 issue/设计文档开始，PR 要小、可测、可回滚、有文档。

MiniVolcano 证明了“语义可以实现”；这一章说明“实现要变成产品，还需要 API、测试、版本、发布和社区”。下一章把前 33 章的能力落到大模型工作负载：预训练/微调/推理/Agent 的作业形状、并行策略、gang 尺寸、checkpoint/弹性与容量模型；附录见第 35 章。
