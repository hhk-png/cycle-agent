# 第 35 章 · Kueue 扩展开发：AdmissionCheck、Job 集成与 client-go

前 34 章主要在"使用 Kueue"和"理解 Kueue"。本章换一个角色：**你要给 Kueue 加能力，或者把 Kueue 接进自己的平台**。这通常意味着写三类代码：

1. 一个 **AdmissionCheck 控制器**，把外部系统（许可证、安全扫描、容量、审批）接入准入；
2. 一个 **Job 集成**，让 Kueue 理解你自己的作业对象；
3. 一组 **client-go 自动化**，把队列、Workload、配额操作接进平台 API/CLI/流水线。

本章以 Kueue v0.20.x 稳定线（截至 2026-11 为 v0.20.5）的公开 API 为背景；v0.21 尚未 GA。Kueue 的内部包不保证兼容；扩展应尽量依赖 `apis/`、`client-go/` 和文档化的控制器契约。

## 35.1 扩展点全景

| 扩展点 | 你要实现什么 | 稳定性 | 典型场景 |
|---|---|---|---|
| AdmissionCheck 控制器 | 两个 reconciler：AdmissionCheck 与 Workload | 公开契约，推荐 | 许可证、审批、安全扫描、容量置备、多集群 |
| Job 集成 | `GenericJob` 接口 + reconciler + webhook | 公开契约，推荐 | 自研训练框架、内部作业 CRD |
| client-go 自动化 | 用生成的 clientset/informer 读写 Kueue 对象 | 公开 API | 平台 API、CLI、报表、自动化 |
| Webhook | 默认值、校验、不可变字段 | 公开 API | 平台策略、租户约束 |
| Visibility | 只读查询 Workload/队列 | 公开 API | 租户控制台 |
| 指标/事件 | 自定义指标、事件 | 公开 API | 可观测性 |
| Scheduler/抢占算法 | 修改 Kueue 核心 | 非扩展点，需 fork/upstream | 特殊调度策略 |

最后一行很重要：**Kueue 没有稳定的"调度插件"接口**。如果你需要改变 Flavor 分配、公平或抢占算法，正确路径是 upstream KEP/PR，或者维护一个 fork；不要指望通过 AdmissionCheck 绕过调度语义。

## 35.2 开发环境

### 35.2.1 依赖

- Go 1.24+；
- controller-runtime / kubebuilder；
- 一个 kind/minikube 集群；
- envtest 二进制（`setup-envtest`）；
- Kueue 的 CRD 和 API 包。

`go.mod` 里直接依赖 Kueue 的公开模块：

```text
require (
    sigs.k8s.io/kueue v0.20.x
    sigs.k8s.io/controller-runtime v0.2x.x
    k8s.io/api v0.3x.x
    k8s.io/apimachinery v0.3x.x
    k8s.io/client-go v0.3x.x
)
```

版本要对齐：Kueue 的 `k8s.io/*` 依赖版本必须和你的 controller-runtime 兼容，否则会出现 `SchemeBuilder` 注册冲突或 API 类型不匹配。

### 35.2.2 目录骨架

```text
my-kueue-extension/
├── api/v1alpha1/                 # 自定义参数 CRD
├── cmd/controller/main.go
├── internal/controller/
│   ├── admissioncheck/           # AdmissionCheck + Workload reconciler
│   └── jobintegration/           # 自定义 Job 集成
├── config/
│   ├── crd/
│   ├── rbac/
│   └── webhook/
└── test/
    ├── envtest/
    └── e2e/
```

### 35.2.3 先看参考实现

Kueue 自带两个最好的 AdmissionCheck 参考：

- `pkg/controller/admissionchecks/provisioning`：对接 Cluster Autoscaler 的 ProvisioningRequest；
- `pkg/controller/admissionchecks/multikueue`：多集群分发。

读它们时重点看：

1. 如何声明 `controllerName`；
2. 如何维护 `AdmissionCheck` 的 `Active` 条件；
3. 如何写 `Workload.status.admissionChecks`；
4. 如何处理 `Retry`/`Rejected`；
5. 如何避免重复处理。

## 35.3 AdmissionCheck 控制器的契约

### 35.3.1 两个 reconciler

一个 AdmissionCheck 控制器必须有两个互补的 reconciler：

| Reconciler | 监听 | 职责 |
|---|---|---|
| AdmissionCheck reconciler | `AdmissionCheck` | 只处理 `spec.controllerName` 匹配的检查；维护 `Active` 条件；校验参数 |
| Workload reconciler | `Workload` | 找到本控制器负责的检查状态；执行外部逻辑；写 `Pending/Ready/Retry/Rejected` |

只做其中一个是不够的：

- 没有 AdmissionCheck reconciler，Kueue 不知道这个检查是否可用，CQ 可能一直 `Active=False`；
- 没有 Workload reconciler，检查永远停在 `Pending`，Workload 永远不 Admitted。

### 35.3.2 controllerName

`AdmissionCheck.spec.controllerName` 是所有权边界。约定：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: license-check
spec:
  controllerName: example.com/license-check
```

- 一个控制器只处理自己 `controllerName` 的检查；
- 不同控制器不能处理同一个检查；
- 同一控制器可以处理多个 `AdmissionCheck` 对象；
- `controllerName` 应该是稳定的 DNS 风格标识；
- v1beta2 的 `admissionChecksStrategy.admissionChecks[]` 支持 `onFlavors`，可以让检查只在指定 Flavor 上执行（例如只有 GPU Flavor 需要置备检查）。

### 35.3.3 Active 条件

AdmissionCheck 只有在 `Active=True` 时才能被 ClusterQueue 使用。`Active=False` 的常见原因：

- 参数对象不存在/非法；
- 外部系统不可用；
- 控制器没有部署；
- 控制器版本不兼容。

`Active` 条件的 `Reason` 要可读，例如：

```text
Active=True  Reason=Ready
Active=False Reason=ParametersNotFound
Active=False Reason=InvalidParameters
Active=False Reason=ControllerNotReady
```

### 35.3.4 参数对象

`spec.parameters` 指向一个集群级 CRD，用来放控制器配置：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: license-check
spec:
  controllerName: example.com/license-check
  parameters:
    apiGroup: example.com
    kind: LicenseCheckConfig
    name: default
---
apiVersion: example.com/v1
kind: LicenseCheckConfig
metadata:
  name: default
spec:
  required: true
  ttlSeconds: 3600
  failOpen: false
```

参数对象应该：

- 集群级（不是命名空间级），因为 AdmissionCheck 是集群级；
- 有校验 webhook；
- 对影响准入语义的字段做不可变/版本化；
- 有明确的默认值。

### 35.3.5 Workload 状态机

Kueue 在 Workload 上维护 `status.admissionChecks[]`：

| State | 含义 | Kueue 的行为 |
|---|---|---|
| `Pending` | 检查尚未完成 | 保持配额预留，等待 |
| `Ready` | 检查通过 | 如果所有检查都 Ready，Workload Admitted |
| `Retry` | 暂时失败，可重试 | 释放配额，按退避重排队 |
| `Rejected` | 永久失败 | Workload 停用/不重试 |

```text
QuotaReserved
  └─ AdmissionCheckState=Pending
       ├─ 外部成功 → Ready → 全部 Ready → Admitted
       ├─ 暂时失败 → Retry → 释放配额 → 退避 → 重新预留
       └─ 永久失败 → Rejected → Workload 停用
```

关键设计问题：

- 什么算"暂时"？网络超时、容量不足、外部系统 5xx 通常是 Retry；参数非法、许可证永久吊销通常是 Rejected。
- Retry 会释放配额，所以外部系统必须能接受"同一作业稍后重来"。
- Rejected 不应该被用来表达"现在不行"；它会让 Workload 停用。

### 35.3.6 幂等与收敛

AdmissionCheck 控制器会被重复调用，必须满足：

- 同一 Workload、同一检查，重复 reconcile 不重复申请外部资源；
- 已经 Ready 的检查不再调用外部系统；
- 外部请求有幂等键（Workload UID + check name）；
- 失败后重试不会产生重复计费/重复审批；
- 并发更新用 `resourceVersion` 冲突重试或 server-side apply。

## 35.4 实现 AdmissionCheck reconciler

下面是一个最小但完整的骨架。错误处理和日志省略，重点是状态维护。

```go
package admissioncheck

import (
	"context"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	kueue "sigs.k8s.io/kueue/apis/kueue/v1beta2"
)

type Reconciler struct {
	client.Client
	ControllerName string
}

func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var ac kueue.AdmissionCheck
	if err := r.Get(ctx, req.NamespacedName, &ac); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// 不是我的检查，直接忽略。多个 ACC 可以共存于同一集群。
	if ac.Spec.ControllerName != r.ControllerName {
		return ctrl.Result{}, nil
	}

	active, reason, message := r.validate(ctx, &ac)
	cond := metav1.Condition{
		Type:               "Active", // Kueue 的常量名以目标版本为准
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: ac.Generation,
		LastTransitionTime: metav1.Now(),
	}
	if active {
		cond.Status = metav1.ConditionTrue
	}
	setCondition(&ac.Status.Conditions, cond)

	if err := r.Status().Update(ctx, &ac); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	return ctrl.Result{}, nil
}

func (r *Reconciler) validate(ctx context.Context, ac *kueue.AdmissionCheck) (bool, string, string) {
	if ac.Spec.Parameters == nil {
		return false, "ParametersNotFound", "spec.parameters is required"
	}
	var cfg LicenseCheckConfig
	key := types.NamespacedName{Name: ac.Spec.Parameters.Name}
	if err := r.Get(ctx, key, &cfg); err != nil {
		return false, "ParametersNotFound", err.Error()
	}
	if cfg.Spec.TTLSeconds <= 0 {
		return false, "InvalidParameters", "ttlSeconds must be positive"
	}
	return true, "Ready", "controller is ready"
}

func setCondition(conds *[]metav1.Condition, next metav1.Condition) {
	for i := range *conds {
		if (*conds)[i].Type != next.Type {
			continue
		}
		if (*conds)[i].Status == next.Status &&
			(*conds)[i].Reason == next.Reason &&
			(*conds)[i].Message == next.Message {
			// 状态没变，保留原 LastTransitionTime，避免无意义抖动。
			return
		}
		(*conds)[i] = next
		return
	}
	*conds = append(*conds, next)
}
```

要点：

- `ObservedGeneration` 让读者知道这个条件对应哪一代 spec；
- 条件没变时不要刷新 `LastTransitionTime`；
- 参数对象变化要触发 AdmissionCheck reconcile（用 `Watches` + map function）；
- `Status().Update` 失败要重试，不要吞掉冲突。

注册：

```go
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&kueue.AdmissionCheck{}).
		Watches(&LicenseCheckConfig{}, handler.EnqueueRequestsFromMapFunc(r.mapConfigToChecks)).
		Complete(r)
}
```

`mapConfigToChecks` 列出所有引用该参数的 AdmissionCheck，逐个入队。

## 35.5 实现 Workload reconciler

Workload reconciler 负责真正的检查逻辑。它不应该处理所有 Workload，而是只处理：

- `Workload.status.conditions` 里 `QuotaReserved=True`；
- `status.admissionChecks` 里存在本控制器负责的检查；
- 检查状态是 `Pending` 或需要重新评估。

下面的 `evaluate` 返回值和 `decisionX` 常量是业务占位；重点看状态维护、幂等和冲突处理。

```go
type WorkloadReconciler struct {
	client.Client
	ControllerName string
	CheckNames     map[string]struct{} // 本控制器负责的 AdmissionCheck 名称
}

func (r *WorkloadReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var wl kueue.Workload
	if err := r.Get(ctx, req.NamespacedName, &wl); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	idx := r.checkIndex(&wl)
	if idx < 0 {
		return ctrl.Result{}, nil
	}
	state := wl.Status.AdmissionChecks[idx]

	// 已经终态，幂等返回。
	switch state.State {
	case kueue.CheckStateReady, kueue.CheckStateRejected:
		return ctrl.Result{}, nil
	}

	// 没有预留配额时不做外部动作；Kueue 会在预留后重新入队。
	if !quotaReserved(&wl) {
		return ctrl.Result{}, nil
	}

	decision, message, retryAfter := r.evaluate(ctx, &wl, state.Name)

	state.Message = message
	state.LastTransitionTime = metav1.Now()
	switch decision {
	case decisionReady:
		state.State = kueue.CheckStateReady
	case decisionRetry:
		state.State = kueue.CheckStateRetry
		retry := retryAfter
		state.RequeueAfterSeconds = &retry
	case decisionRejected:
		state.State = kueue.CheckStateRejected
	}
	wl.Status.AdmissionChecks[idx] = state

	if err := r.Status().Update(ctx, &wl); err != nil {
		return ctrl.Result{}, err
	}
	if decision == decisionRetry {
		return ctrl.Result{RequeueAfter: time.Duration(retryAfter) * time.Second}, nil
	}
	return ctrl.Result{}, nil
}
```

几个细节：

1. **只改自己负责的 state。** 不要重写整个 `admissionChecks` 列表，否则会覆盖其他控制器的状态。
2. **不要自己设置 `Admitted=True`。** Kueue 会在所有检查 Ready 后设置。
3. **Retry 的退避优先用 `requeueAfterSeconds`（在 `Workload.status.admissionChecks[]` 上）。** 旧的 `AdmissionCheck.spec.retryDelayMinutes` 在 v1beta2 已被移除，它不是 `AdmissionCheckState` 的同级字段。
4. **外部调用要有超时。** 不要在 reconcile 里无限等待。
5. **状态更新冲突要重试。** 用 `retry.RetryOnConflict` 或 controller-runtime 的默认重试。
6. **podSetUpdates 只在确实需要改 Pod 模板时使用。** 例如外部系统要求注入 nodeSelector/tolerations；否则不要动。

### 35.5.1 `podSetUpdates` 的边界

`AdmissionCheckState.podSetUpdates` 允许检查控制器修改 PodSet 的：

- `nodeSelector`
- `tolerations`
- `labels`
- `annotations`

它是**增量（additive）**更新：只能补充/覆盖上述字段，不能改资源请求、副本数、镜像、命令等。用途是让外部系统在准入时注入物理约束或标识（例如"这个作业必须落在带某标签的节点"）。具体支持范围以目标版本的 `PodSetUpdate` API 为准。

滥用后果：

- 修改与 Flavor 注入冲突；
- Pod 模板与 Workload 账本不一致；
- 作业行为不可预测。

原则：**能用 Flavor 表达的就用 Flavor；podSetUpdates 只用于 AdmissionCheck 特有的、无法在队列层表达的约束。**

## 35.6 参数 CRD 与校验

```go
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Cluster
// +kubebuilder:subresource:status
type LicenseCheckConfig struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              LicenseCheckConfigSpec `json:"spec,omitempty"`
}

type LicenseCheckConfigSpec struct {
	// +kubebuilder:validation:Minimum=1
	TTLSeconds int32 `json:"ttlSeconds"`
	// +kubebuilder:default=true
	Required bool `json:"required,omitempty"`
	// +kubebuilder:default=false
	FailOpen bool `json:"failOpen,omitempty"`
}
```

校验建议：

- `TTLSeconds` 必须为正；
- `FailOpen=true` 要有显式审计（它意味着外部系统故障时放行）；
- 影响准入语义的字段变更要考虑不可变；
- 参数对象删除时，AdmissionCheck 应该转为 `Active=False`，而不是静默使用旧值。

## 35.7 测试策略

### 35.7.1 单元测试：fake client

用 `controller-runtime/pkg/client/fake` 构造对象，直接调用 `Reconcile`：

```go
func TestAdmissionCheckBecomesInactiveWithoutParameters(t *testing.T) {
	scheme := runtime.NewScheme()
	_ = kueue.AddToScheme(scheme)
	ac := &kueue.AdmissionCheck{
		ObjectMeta: metav1.ObjectMeta{Name: "license-check"},
		Spec: kueue.AdmissionCheckSpec{
			ControllerName: "example.com/license-check",
			Parameters: &kueue.AdmissionCheckParametersReference{
				APIGroup: "example.com", Kind: "LicenseCheckConfig", Name: "missing",
			},
		},
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(ac).WithStatusSubresource(ac).Build()
	r := &Reconciler{Client: c, ControllerName: "example.com/license-check"}

	if _, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: ac.Name},
	}); err != nil {
		t.Fatal(err)
	}
	var got kueue.AdmissionCheck
	_ = c.Get(context.Background(), types.NamespacedName{Name: ac.Name}, &got)
	if conditionStatus(got.Status.Conditions, "Active") != metav1.ConditionFalse {
		t.Fatalf("Active = %v, want False", got.Status.Conditions)
	}
}
```

### 35.7.2 状态迁移表

至少覆盖：

| 初始状态 | 外部结果 | 期望 |
|---|---|---|
| Pending | 成功 | Ready，Workload Admitted |
| Pending | 暂时失败 | Retry，配额释放，重新排队 |
| Pending | 永久失败 | Rejected，Workload 停用 |
| Ready | 再次 reconcile | 不调用外部，状态不变 |
| Retry | 退避后成功 | Ready |
| Rejected | 再次 reconcile | 不变 |

### 35.7.3 envtest

envtest 启动真实 API server + etcd，安装 Kueue CRD 和你的 CRD，适合验证：

- webhook 默认值/校验；
- status subresource；
- 条件传播；
- 与 Kueue 控制器的交互（如果同时启动 Kueue）。

```go
testEnv := &envtest.Environment{
	CRDDirectoryPaths: []string{
		filepath.Join("..", "..", "config", "crd", "bases"),
		filepath.Join("testdata", "kueue-crds"),
	},
	ErrorIfCRDPathMissing: true,
}
cfg, err := testEnv.Start()
```

### 35.7.4 集成/故障测试

- 外部系统超时：检查是否 Retry，而不是把 reconcile 卡死；
- 并发 reconcile：同一 Workload 两个 worker 同时处理，状态是否幂等；
- 参数删除：AdmissionCheck 是否 Active=False；
- 控制器重启：是否从 status 恢复，不重复申请外部资源；
- 版本升级：旧 status 字段是否兼容。

## 35.8 部署

### 35.8.1 RBAC

至少需要：

```yaml
rules:
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["admissionchecks"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["admissionchecks/status"]
  verbs: ["get", "update", "patch"]
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["workloads"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["workloads/status"]
  verbs: ["get", "update", "patch"]
- apiGroups: ["example.com"]
  resources: ["licensecheckconfigs"]
  verbs: ["get", "list", "watch"]
```

不要给集群级 `*` 权限；AdmissionCheck 控制器通常只需要 Kueue 的少量资源和自己的参数 CRD。

### 35.8.2 高可用

- 多副本 + leader election；
- 同一个 `controllerName` 只能有一个活跃控制器；
- 外部调用要幂等；
- 用 workqueue 的 rate limiter 防止外部系统故障时打爆；
- 指标暴露 reconcile 次数、延迟、错误、重试。

### 35.8.3 Webhook 证书

如果参数 CRD 有校验/默认值 webhook：

- 用 cert-manager 或内置证书轮换；
- webhook 不可用时，API 写入会失败，要有告警；
- 不要把 AdmissionCheck 的关键逻辑放在 webhook 里，webhook 只做静态校验。

## 35.9 自定义 Job 集成

### 35.9.1 什么时候需要

Kueue 已内置 Job、JobSet、Kubeflow、Ray、MPI、LWS、Pod、Deployment、StatefulSet 等集成。只有以下情况才需要自己写：

- 自研训练框架/CRD；
- 内部作业平台对象；
- 需要特殊的 PodSet 映射或暂停机制；
- 需要把外部调度器的作业接入 Kueue。

### 35.9.2 GenericJob 接口

一个集成要实现 `jobframework.GenericJob` 的核心方法：

```go
type GenericJob interface {
	Object() client.Object
	IsSuspended() bool
	Suspend() error
	PodSets() []kueue.PodSet
	RunWithPodSetsInfo([]podset.PodSetInfo) error
	RestorePodSetsInfo([]podset.PodSetInfo) bool
	Finished() (message string, success, finished bool)
	IsActive() bool
	PodsReady() bool
	GVK() schema.GroupVersionKind
}
```

不同版本的签名可能略有差异（例如较新的实现会在 `RunWithPodSetsInfo` 上加 `context.Context`，`Finished` 也曾使用 `metav1.Condition`），以你依赖的 Kueue 版本为准。关键语义：

- `PodSets()`：把作业的 Pod 模板映射成 Kueue 的 PodSet（名字、副本数、资源请求）；
- `Suspend()`：确保 Workload 未准入前不创建 Pod；
- `RunWithPodSetsInfo()`：把 Flavor/TAS 注入的 nodeSelector、affinity、tolerations 应用到 Pod 模板，并解除暂停；
- `RestorePodSetsInfo()`：驱逐/停止时恢复原始模板；
- `Finished()/IsActive()/PodsReady()`：生命周期状态。

### 35.9.3 集成骨架

```go
type MyJob struct {
	myv1.MyJob
}

var _ jobframework.GenericJob = &MyJob{}

func (j *MyJob) Object() client.Object { return &j.MyJob }

func (j *MyJob) IsSuspended() bool { return j.Spec.Suspend }

func (j *MyJob) Suspend() error {
	j.Spec.Suspend = true
	return nil
}

func (j *MyJob) PodSets() []kueue.PodSet {
	return []kueue.PodSet{{
		Name:     "main",
		Count:    *j.Spec.Replicas,
		Template: *j.Spec.Template.DeepCopy(),
	}}
}

func (j *MyJob) RunWithPodSetsInfo(info []podset.PodSetInfo) error {
	if len(info) > 0 {
		j.Spec.Template.Spec.NodeSelector = info[0].NodeSelector
		j.Spec.Template.Spec.Tolerations = info[0].Tolerations
	}
	j.Spec.Suspend = false
	return nil
}

func (j *MyJob) RestorePodSetsInfo(info []podset.PodSetInfo) bool {
	// 恢复原始 nodeSelector/tolerations；返回是否真的做了修改。
	return true
}

func (j *MyJob) Finished() (message string, success, finished bool) {
	finished = j.Status.Phase == myv1.Finished || j.Status.Phase == myv1.Failed
	success = j.Status.Phase == myv1.Finished
	return "", success, finished
}

func (j *MyJob) IsActive() bool { return j.Status.Phase == myv1.Running }
func (j *MyJob) PodsReady() bool { return j.Status.ReadyReplicas == *j.Spec.Replicas }

// GVK 让 jobframework 知道这个对象对应哪个 CRD。
func (j *MyJob) GVK() schema.GroupVersionKind {
	return myv1.GroupVersion.WithKind("MyJob")
}
```

注册集成通常需要：

- `jobframework.RegisterIntegration` 或等价的注册函数；
- 一个 `ReconcilerFactory`；
- 一个 webhook 工厂（默认值/校验/队列名注入）；
- 把 GVK 加入 manager 的 scheme；
- 在 Kueue Configuration 的 `integrations` 列表里启用。

### 35.9.4 幂等与暂停

集成最容易出的问题：

1. **重复创建 Workload**：reconcile 重入时按 owner UID 查找已有 Workload；
2. **暂停不彻底**：Pod 已经创建，再 suspend 来不及；必须在创建前 gate；
3. **恢复时覆盖用户修改**：`RunWithPodSetsInfo` 只改自己的字段，不要整对象替换；
4. **驱逐后不清理**：`RestorePodSetsInfo` + suspend，必要时删除 Pod；
5. **完成状态不收敛**：明确什么算 Finished，避免长期 Running。

### 35.9.5 测试矩阵

- 单 PodSet / 多 PodSet；
- suspend/resume；
- 准入前 Pod 不存在；
- 驱逐/重排队；
- 资源变化/reclaimable pods；
- 完成/失败；
- 控制器重启；
- webhook 默认值；
- 与 TAS/DRA 的组合（如果支持）。

## 35.10 client-go 自动化

### 35.10.1 生成客户端

Kueue 提供生成的 clientset/informers/listers：

```go
import (
	kueueclient "sigs.k8s.io/kueue/client-go/clientset/versioned"
	kueueinformers "sigs.k8s.io/kueue/client-go/informers/externalversions"
)

cfg, err := rest.InClusterConfig()
if err != nil {
	return err
}
clientset, err := kueueclient.NewForConfig(cfg)
if err != nil {
	return err
}
factory := kueueinformers.NewSharedInformerFactory(clientset, 30*time.Second)
workloads := factory.Kueue().V1beta2().Workloads()
```

版本注意：

- v0.20 一代主推 `v1beta2`；
- 老版本可能只有 `v1beta1`；
- 用 `factory.Kueue().V1beta2()` 前先确认生成的 clientset 包含该版本；
- 如果只需要通用 CRD 操作，也可以用 `dynamic.Interface` + GVR。

### 35.10.2 等待 Workload 准入

```go
func WaitAdmitted(ctx context.Context, c kueueclient.Interface, ns, name string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		wl, err := c.KueueV1beta2().Workloads(ns).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return err
		}
		for _, cond := range wl.Status.Conditions {
			if cond.Type == "Admitted" && cond.Status == metav1.ConditionTrue {
				return nil
			}
			// Inadmissible is a Reason on QuotaReserved=False, not a condition
			// type of its own.
			if cond.Type == "QuotaReserved" && cond.Status == metav1.ConditionFalse && cond.Reason == "Inadmissible" {
				return fmt.Errorf("workload is inadmissible: %s", cond.Message)
			}
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("timeout waiting for %s/%s", ns, name)
		}
		time.Sleep(2 * time.Second)
	}
}
```

生产上应使用 informer + workqueue，而不是轮询；轮询只适合脚本/CLI。

### 35.10.3 配额日报

```go
func QuotaReport(ctx context.Context, c kueueclient.Interface) ([]QueueUsage, error) {
	cqs, err := c.KueueV1beta2().ClusterQueues().List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, err
	}
	var out []QueueUsage
	for _, cq := range cqs.Items {
		// 从 status.flavorsReservation / flavorsUsage 读取
		out = append(out, QueueUsage{
			Name:      cq.Name,
			Cohort:    cq.Spec.CohortName,
			Reserving: cq.Status.FlavorsReservation,
			Usage:     cq.Status.FlavorsUsage,
		})
	}
	return out, nil
}
```

字段名以目标版本 API reference 为准；不同版本的 status 结构会变。

### 35.10.4 批量 stop/resume

```go
func StopWorkloads(ctx context.Context, c kueueclient.Interface, ns string, names []string) error {
	for _, name := range names {
		wl, err := c.KueueV1beta2().Workloads(ns).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return err
		}
		active := false
		wl.Spec.Active = &active
		if _, err := c.KueueV1beta2().Workloads(ns).Update(ctx, wl, metav1.UpdateOptions{}); err != nil {
			return err
		}
	}
	return nil
}
```

注意：

- 已准入的 Workload 被停用会触发驱逐；
- 批量操作要有 dry-run、限速、审计；
- 冲突时重新 Get 再 Update，或使用 patch。

## 35.11 Webhook 扩展

### 35.11.1 默认值

典型默认值：

- 队列名标签从 namespace 注解/label 继承；
- `suspend: true`；
- 优先级类默认值；
- 作业框架的 group label。

### 35.11.2 校验

典型校验：

- 队列名必须指向存在的 LocalQueue；
- 资源请求不能为负；
- 不允许修改已准入 Workload 的关键字段；
- PodSet 名称/数量合法；
- 参数对象引用有效。

### 35.11.3 证书与可用性

- webhook 证书轮换；
- webhook 超时（默认 10s 左右）要短于 API server 的请求超时；
- webhook 不可用会导致创建/更新失败，必须监控；
- 不要把耗时逻辑放进 webhook。

## 35.12 发布与兼容性

### 35.12.1 版本策略

- 你的控制器与 Kueue 版本要有兼容矩阵；
- 依赖 `v1beta2` 时，明确最低 Kueue 版本；
- 参数字段变更要向后兼容（新增可选字段，不删除旧字段）；
- 破坏性变更需要新的 API 版本或显式迁移。

### 35.12.2 指标与事件

建议暴露：

```text
myacc_reconcile_total{result="success|error|retry"}
myacc_reconcile_duration_seconds
myacc_external_requests_total{result="ok|timeout|error"}
myacc_pending_checks
myacc_rejected_total
```

事件：

- `AdmissionCheckActivated`
- `AdmissionCheckDeactivated`
- `CheckReady`
- `CheckRetry`
- `CheckRejected`

### 35.12.3 升级检查清单

- [ ] Kueue CRD 版本兼容；
- [ ] 参数对象 schema 可转换；
- [ ] Workload status 字段兼容；
- [ ] webhook 证书有效；
- [ ] RBAC 覆盖新资源；
- [ ] 控制器只处理自己的 controllerName；
- [ ] 旧 Workload 的检查状态能继续收敛；
- [ ] 有回滚路径。

## 35.13 MiniKueue 对照实验

MiniKueue 没有真实的 controller-runtime，但它把 AdmissionCheck 的状态机压缩进了 `Engine.evaluateChecks`：

- `admission-check`：手动把检查置为 Ready；
- `retry-backoff`：Retry + 退避；
- `provisioning-request`：置备失败/重试/延迟 Ready。

你可以把它当作"ACC 状态机的确定性参考"：

```bash
go run ./cmd/minikueue -scenario admission-check -ticks 2
go run ./cmd/minikueue -scenario retry-backoff -ticks 4
go run ./cmd/minikueue -scenario provisioning-request -ticks 5
```

### 35.13.1 实验：给 MiniKueue 加一个 LicenseCheck provider

目标：实现一个"许可证检查"控制器语义。

步骤：

1. 在 `internal/api` 增加 `LicenseConfig{TTL, FailOpen}`；
2. 在 `controller.Engine` 增加 `License *LicenseConfig`；
3. 在 `evaluateChecks` 里对 `controllerName == "license"` 的检查做决策：
   - 如果 TTL 未过期 → Ready；
   - 如果 TTL 过期且 `FailOpen` → Ready（带警告）；
   - 否则 → Retry 或 Rejected；
4. 增加一个场景 `license-check`；
5. 增加测试：Ready/Retry/Rejected 三条路径；
6. 跑 `gofmt`、`go test`、场景命令。

验收标准：

- 检查状态随 tick 收敛；
- Retry 会释放配额并重新预留；
- Rejected 会让 Workload 停用；
- 重复 tick 不重复计数；
- 有对应的单元测试和场景输出。

这个实验对应真实 ACC 的四个核心问题：Active、状态机、幂等、退避。

## 35.14 检查清单

### AdmissionCheck

- [ ] 只处理自己的 `controllerName`；
- [ ] AdmissionCheck 的 `Active` 条件正确；
- [ ] 参数对象缺失/非法时 `Active=False`；
- [ ] 只修改自己负责的 `AdmissionCheckState`；
- [ ] Ready/Retry/Rejected 语义清晰；
- [ ] Retry 使用退避，不忙等；
- [ ] 外部调用幂等、有超时；
- [ ] 状态更新冲突可重试；
- [ ] 重复 reconcile 不重复申请资源；
- [ ] 有指标/事件/日志；
- [ ] 有 envtest/集成测试；
- [ ] 有 RBAC/HA/证书方案。

### Job 集成

- [ ] `PodSets()` 映射正确；
- [ ] 准入前不创建 Pod；
- [ ] 准入后注入 Flavor/TAS 并解除暂停；
- [ ] 驱逐后恢复并暂停；
- [ ] 完成/失败状态收敛；
- [ ] 资源变化通过 reclaimable pods/受支持路径报告；
- [ ] 重复 reconcile 不重复创建 Workload；
- [ ] 有 webhook 默认值/校验；
- [ ] 有测试矩阵。

## 35.15 本章小结

- Kueue 的稳定扩展点是 **AdmissionCheck**、**Job 集成**、**client-go/webhook**；调度算法不是插件接口。
- AdmissionCheck 控制器需要两个 reconciler：AdmissionCheck（Active）和 Workload（状态机）。
- `controllerName` 是所有权边界；只改自己负责的 `AdmissionCheckState`。
- Pending/Ready/Retry/Rejected 的语义不同：Retry 释放配额，Rejected 停用 Workload。
- 参数 CRD 要集群级、可校验、可版本化；`podSetUpdates` 只用于无法在队列层表达的约束。
- 自定义 Job 集成的核心是 `GenericJob`：PodSets、Suspend、RunWithPodSetsInfo、Restore、Finished。
- client-go 适合平台 API/CLI/报表；生产用 informer，脚本可以轮询。
- 测试要覆盖状态迁移、幂等、冲突、重启和外部故障。
- MiniKueue 的 AdmissionCheck 场景可以作为状态机的确定性参考。

---

[← 上一章：MultiKueue 多集群调度深入](34-multikueue-deep-dive.md) | [返回目录](../README.md) | [下一章：推理、弹性与混合负载 →](36-inference-elastic-workloads.md)
