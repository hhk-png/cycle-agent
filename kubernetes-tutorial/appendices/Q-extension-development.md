# 附录 Q 扩展点与交付物开发实战

本附录补上“读 MiniK8s”与“给真实集群交付扩展”之间的空白：用 Go 写客户端、informer、控制器、准入 webhook、kubectl 插件和 Helm chart。正文第 18、19、24、32 章讲机制，这里讲可运行的工程骨架；所有代码都是最小示例，生产使用要补错误处理、指标、限流、测试与发布流程。

## Q.1 先选扩展点

Kubernetes 的扩展点很多，先按“你要改什么”选，而不是按“哪个技术热”选：

| 需求 | 扩展点 | 不需要 | 代价 |
|---|---|---|---|
| 新增一种声明式资源 | CRD + controller | 改 API Server | 需要版本/转换/状态设计 |
| 在对象写入前后做策略 | ValidatingAdmissionPolicy（CEL）或 webhook | controller | webhook 有可用性与 TLS 成本 |
| 给对象注入默认值/边车 | MutatingWebhook | 改调度器 | 可能影响所有工作负载 |
| 改变调度决策 | Scheduler Framework 插件 | 改 kubelet | 需要维护调度器版本 |
| 新增设备/存储/网络 | DRA/CSI/CNI/CRI | CRD | 接口契约与节点运维 |
| 只做运维便利 | kubectl 插件 | 控制器 | 仅客户端，风险最低 |
| 打包交付 | Helm/Kustomize/OCI | 改集群 | 模板与值治理 |

一个经验法则：**能用 CEL/ValidatingAdmissionPolicy 表达的静态规则，不要写 webhook；能用 controller 表达的最终状态，不要写 webhook 做副作用。**

## Q.2 项目骨架与版本选择

Go 项目至少固定三类版本：Go、client-go/controller-runtime、目标 Kubernetes minor。它们之间有兼容矩阵，不能各自追最新。

```text
website-operator/
├── go.mod
├── cmd/manager/main.go
├── api/v1/website_types.go
├── internal/controller/website_controller.go
├── internal/webhook/website_webhook.go
├── config/crd/
├── config/rbac/
├── config/manager/
├── test/e2e/
├── charts/website-operator/
└── Makefile
```

```bash
go mod init example.com/website-operator
go get sigs.k8s.io/controller-runtime@latest
go get k8s.io/client-go@latest k8s.io/apimachinery@latest
go mod tidy
```

选择版本时：

- controller-runtime 的 `go.mod` 会声明它测试过的 client-go/k8s 版本；以它为准，不要手工把 client-go 升到不兼容的 minor。
- 目标集群版本决定 API 是否可用（例如 DRA、Gateway API、ValidatingAdmissionPolicy 的阶段）。
- CI 里同时跑 `go vet`、`staticcheck`、`go test ./...` 和 envtest；不要只跑单元测试。

## Q.3 client-go：最小可用客户端

### Q.3.1 配置与 typed client

```go
package main

import (
	"context"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

func clientset(kubeconfig string) (*kubernetes.Clientset, error) {
	var (
		cfg *rest.Config
		err error
	)
	if kubeconfig == "" {
		cfg, err = rest.InClusterConfig()
	} else {
		cfg, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
	}
	if err != nil {
		return nil, err
	}
	// 客户端默认 QPS/Burst 对控制器来说通常太低；按 API Server 容量调整。
	cfg.QPS = 50
	cfg.Burst = 100
	cfg.Timeout = 30 * time.Second
	return kubernetes.NewForConfig(cfg)
}

func listPods(ctx context.Context, cs *kubernetes.Clientset, namespace string) error {
	pods, err := cs.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{
		LabelSelector: "app=web",
		FieldSelector: "status.phase=Running",
		Limit:         500,
	})
	if err != nil {
		return err
	}
	for _, pod := range pods.Items {
		fmt.Printf("%s/%s node=%s\n", pod.Namespace, pod.Name, pod.Spec.NodeName)
	}
	return nil
}
```

要点：

- 所有调用都带 `context`；控制器里用 `ctx`，CLI 里用带超时的 `context.WithTimeout`。
- `List` 的 `Limit` 只是单页大小；要拿全量必须处理 `continue`。
- typed client 只覆盖内置资源；CRD 用 dynamic client 或生成代码。

### Q.3.2 分页与一致读

```go
func listAllPods(ctx context.Context, cs *kubernetes.Clientset, namespace string) ([]string, error) {
	var names []string
	opts := metav1.ListOptions{Limit: 500}
	for {
		page, err := cs.CoreV1().Pods(namespace).List(ctx, opts)
		if err != nil {
			return nil, err
		}
		for _, pod := range page.Items {
			names = append(names, pod.Name)
		}
		if page.Continue == "" {
			return names, nil
		}
		opts.Continue = page.Continue
	}
}
```

同一次 `continue` 序列是快照；token 过期或版本被 compaction 会返回 `410 Gone`。客户端应把 410 当成“重新 List”，而不是无限重试同一个 token。

### Q.3.3 写入与冲突

```go
func scale(ctx context.Context, cs *kubernetes.Clientset, namespace, name string, replicas int32) error {
	deploy, err := cs.AppsV1().Deployments(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return err
	}
	deploy.Spec.Replicas = &replicas
	// Update 会带上 resourceVersion；并发修改时返回 409，调用方应重新读取后重试。
	_, err = cs.AppsV1().Deployments(namespace).Update(ctx, deploy, metav1.UpdateOptions{})
	return err
}
```

不要吞掉 409。控制器应使用 `retry.RetryOnConflict` 或重新入队；脚本应把冲突暴露给用户。

## Q.4 dynamic client、Discovery 与 RESTMapper

CRD 没有 typed client 时，用 dynamic client：

```go
import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
)

var websiteGVR = schema.GroupVersionResource{
	Group: "example.com", Version: "v1", Resource: "websites",
}

func getWebsite(ctx context.Context, dyn dynamic.Interface, namespace, name string) error {
	obj, err := dyn.Resource(websiteGVR).Namespace(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return err
	}
	fmt.Println(obj.GetName(), obj.Object["spec"])
	return nil
}
```

Discovery + RESTMapper 可以把 `kind` 解析成 `resource`，避免硬编码 plural：

```go
import (
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/restmapper"
)

func mapper(cfg *rest.Config) (meta.RESTMapper, error) {
	dc, err := discovery.NewDiscoveryClientForConfig(cfg)
	if err != nil {
		return nil, err
	}
	groups, err := dc.ServerGroupsAndResources()
	if err != nil {
		return nil, err
	}
	return restmapper.NewDiscoveryRESTMapper(groups), nil
}
```

注意：

- Discovery 结果有缓存；CRD 刚创建时可能短暂看不到。
- 聚合 API 的 discovery 可能失败；RESTMapper 要处理部分发现错误，而不是整体失败。
- dynamic 对象的字段是 `map[string]interface{}`，要做类型断言和缺省处理。

## Q.5 informer、workqueue 与限速

控制器不要轮询 API Server。标准组合是 Reflector → DeltaFIFO → Indexer → SharedInformer → workqueue。

```go
import (
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/workqueue"
)

func podInformer(ctx context.Context, cs *kubernetes.Clientset, queue workqueue.TypedRateLimitingInterface[string]) cache.SharedIndexInformer {
	informer := cache.NewSharedIndexInformer(
		cache.NewListWatchFromClient(
			cs.CoreV1().RESTClient(), "pods", metav1.NamespaceAll,
			fields.Everything(),
		),
		&corev1.Pod{},
		0,
		cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc},
	)
	_, _ = informer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc: func(obj interface{}) {
			if pod, ok := obj.(*corev1.Pod); ok {
				queue.Add(pod.Namespace + "/" + pod.Name)
			}
		},
		UpdateFunc: func(_, newObj interface{}) {
			if pod, ok := newObj.(*corev1.Pod); ok {
				queue.Add(pod.Namespace + "/" + pod.Name)
			}
		},
		DeleteFunc: func(obj interface{}) {
			if tombstone, ok := obj.(cache.DeletedFinalStateUnknown); ok {
				obj = tombstone.Obj
			}
			if pod, ok := obj.(*corev1.Pod); ok {
				queue.Add(pod.Namespace + "/" + pod.Name)
			}
		},
	})
	return informer
}

func run(ctx context.Context, informer cache.SharedIndexInformer, queue workqueue.TypedRateLimitingInterface[string]) {
	go informer.Run(ctx.Done())
	if !cache.WaitForCacheSync(ctx.Done(), informer.HasSynced) {
		return
	}
	for {
		key, shutdown := queue.Get()
		if shutdown {
			return
		}
		func() {
			defer queue.Done(key)
			// reconcile(key)
			queue.Forget(key)
		}()
	}
}
```

要点：

- workqueue 的 key 最好是 `namespace/name`，不要放整个对象；对象会过期。
- 失败用 `queue.AddRateLimited(key)`，成功用 `Forget`；否则会无限快速重试。
- informer 的缓存可能落后；写操作前重新 GET 或接受 409。
- 事件处理函数要轻量，重活放 worker。
- 水平触发：不要假设你看到了每一个中间状态，只根据当前状态调谐。

## Q.6 controller-runtime：从 CRD 到可运行控制器

### Q.6.1 API 类型

```go
package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
type Website struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              WebsiteSpec   `json:"spec,omitempty"`
	Status            WebsiteStatus `json:"status,omitempty"`
}

type WebsiteSpec struct {
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=10
	Replicas int32  `json:"replicas,omitempty"`
	Image    string `json:"image"`
	Host     string `json:"host"`
}

type WebsiteStatus struct {
	ObservedGeneration int64              `json:"observedGeneration,omitempty"`
	ReadyReplicas      int32              `json:"readyReplicas,omitempty"`
	Conditions         []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
type WebsiteList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Website `json:"items"`
}
```

### Q.6.2 Reconcile

```go
package controller

import (
	"context"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	examplev1 "example.com/website-operator/api/v1"
)

const finalizer = "example.com/website-cleanup"

type WebsiteReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

func (r *WebsiteReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	var website examplev1.Website
	if err := r.Get(ctx, req.NamespacedName, &website); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if !website.DeletionTimestamp.IsZero() {
		if controllerutil.ContainsFinalizer(&website, finalizer) {
			// 清理外部资源（DNS、云 LB、对象存储...），必须幂等。
			controllerutil.RemoveFinalizer(&website, finalizer)
			if err := r.Update(ctx, &website); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{}, nil
	}
	if !controllerutil.ContainsFinalizer(&website, finalizer) {
		controllerutil.AddFinalizer(&website, finalizer)
		if err := r.Update(ctx, &website); err != nil {
			return ctrl.Result{}, err
		}
	}

	desiredReplicas := replicasFor(&website)
	deploy := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Name: website.Name, Namespace: website.Namespace,
	}}
	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, deploy, func() error {
		deploy.Spec.Replicas = &desiredReplicas
		deploy.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{"app": website.Name}}
		deploy.Spec.Template.ObjectMeta.Labels = map[string]string{"app": website.Name}
		deploy.Spec.Template.Spec.Containers = []corev1.Container{{
			Name: "web", Image: website.Spec.Image,
			Ports: []corev1.ContainerPort{{Name: "http", ContainerPort: 8080}},
		}}
		return ctrl.SetControllerReference(&website, deploy, r.Scheme)
	})
	if err != nil {
		return ctrl.Result{}, err
	}

	ready := int32(0)
	for _, condition := range deploy.Status.Conditions {
		if condition.Type == appsv1.DeploymentAvailable && condition.Status == corev1.ConditionTrue {
			ready = deploy.Status.ReadyReplicas
		}
	}
	website.Status.ObservedGeneration = website.Generation
	website.Status.ReadyReplicas = ready
	meta.SetStatusCondition(&website.Status.Conditions, metav1.Condition{
		Type: "Ready", Status: conditionStatus(ready >= desiredReplicas),
		Reason: "Reconciled", Message: fmt.Sprintf("%d/%d replicas ready", ready, desiredReplicas),
		ObservedGeneration: website.Generation,
	})
	if err := r.Status().Update(ctx, &website); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func replicasFor(website *examplev1.Website) int32 {
	if website.Spec.Replicas <= 0 {
		return 1
	}
	return website.Spec.Replicas
}

func conditionStatus(ok bool) metav1.ConditionStatus {
	if ok {
		return metav1.ConditionTrue
	}
	return metav1.ConditionFalse
}

func (r *WebsiteReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&examplev1.Website{}).
		Owns(&appsv1.Deployment{}).
		Complete(r)
}
```

要点：

- 先处理删除，再处理创建/更新；finalizer 的添加与清理都要幂等。
- `CreateOrUpdate` 只改你拥有的字段；不要覆盖别人写的注解/标签。
- OwnerReference 让 Deployment 随 Website 级联删除。
- Status 用 `r.Status().Update`，不要用普通 `Update` 混写 spec/status。
- 失败返回 error 让 controller-runtime 退避重试；不要在 Reconcile 里 sleep。
- 不要在同一 Reconcile 里等待 Deployment Ready；返回 `RequeueAfter` 或依赖 Owns 事件。

### Q.6.3 指标与事件

至少暴露：

- `reconcile_total{result="success|error"}`。
- `reconcile_duration_seconds`。
- 队列深度/重试次数。
- 业务状态：受管对象数、Ready 数、失败原因。

事件用于人读，指标用于告警；不要只写日志。controller-runtime 的 `mgr.GetEventRecorderFor` 可以发 Event。

## Q.7 envtest：控制器的真实 API 反馈

envtest 启动真实 kube-apiserver + etcd，不启动 kubelet/CNI。它比 fake client 更接近真实默认值/准入/状态子资源，适合控制器测试。

```go
package controller_test

import (
	"path/filepath"
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	examplev1 "example.com/website-operator/api/v1"
)

var (
	testEnv   *envtest.Environment
	k8sClient client.Client
)

func TestControllers(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "controller suite")
}

var _ = BeforeSuite(func() {
	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
	}
	cfg, err := testEnv.Start()
	Expect(err).NotTo(HaveOccurred())

	testScheme := runtime.NewScheme()
	Expect(clientgoscheme.AddToScheme(testScheme)).To(Succeed())
	Expect(examplev1.AddToScheme(testScheme)).To(Succeed())
	k8sClient, err = client.New(cfg, client.Options{Scheme: testScheme})
	Expect(err).NotTo(HaveOccurred())
})

var _ = AfterSuite(func() {
	Expect(testEnv.Stop()).To(Succeed())
})
```

验收测试至少覆盖：

1. 创建 Website 后生成 Deployment，OwnerReference 正确。
2. 修改 replicas/image 后 Deployment 收敛。
3. 删除 Website 后 finalizer 清理，Deployment 被级联删除。
4. 非法 spec 被 CRD schema 拒绝。
5. 状态子资源更新不增加 generation。
6. 控制器重启后能从现有对象恢复（幂等）。

fake client 适合纯逻辑单元测试，但它不执行默认值、schema、准入和 status 子资源；不要用它替代 envtest。

## Q.8 ValidatingAdmissionWebhook：策略边界

### Q.8.1 何时用 webhook

- 需要跨对象查询（例如“这个 Namespace 的配额是否已满”）。
- 需要调用外部系统（镜像签名验证、IAM 检查）。
- CEL 表达不了（复杂逻辑、外部数据）。

静态字段校验优先用 CRD schema 或 `ValidatingAdmissionPolicy`；webhook 是可用性风险，不是默认选择。

### Q.8.2 最小 webhook

```go
package webhook

import (
	"context"
	"net/http"

	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

type Validator struct {
	decoder admission.Decoder
}

func (v *Validator) Handle(ctx context.Context, req admission.Request) admission.Response {
	if req.Kind.Kind != "Website" {
		return admission.Allowed("not a Website")
	}
	var obj map[string]interface{}
	if err := v.decoder.DecodeRaw(req.Object, &obj); err != nil {
		return admission.Errored(http.StatusBadRequest, err)
	}
	spec, _ := obj["spec"].(map[string]interface{})
	host, _ := spec["host"].(string)
	if host == "" {
		return admission.Denied("spec.host is required")
	}
	return admission.Allowed("ok")
}

}
```

注册（decoder 由 manager 的 scheme 创建；不同 controller-runtime 版本的构造函数略有差异，以依赖版本文档为准）：

```go
decoder := admission.NewDecoder(mgr.GetScheme())
mgr.GetWebhookServer().Register("/validate-example-com-v1-website", &admission.Webhook{
	Handler: &Validator{decoder: decoder},
})
```

### Q.8.3 生产要点

- **TLS 是硬要求**：API Server 只通过 HTTPS 调 webhook；证书 SAN 必须是 Service DNS，通常用 cert-manager 注入。
- **failurePolicy**：`Fail` 更安全但会阻塞写入；`Ignore` 可用性高但可能绕过策略。按资源重要性选，并对 webhook 自身做高可用。
- **timeoutSeconds**：默认 10 秒，通常设 2–5 秒；webhook 里不要做慢查询。
- **namespaceSelector/objectSelector**：缩小拦截范围，避免把 kube-system 锁死。
- **sideEffects**：声明 `None` 或 `NoneOnDryRun`；dry-run 不能产生副作用。
- **规则顺序**：多个 webhook 按名称/顺序执行；DENY 一旦出现就拒绝，不保证你看到所有拒绝原因。
- **幂等与并发**：同一请求可能重试；外部检查要缓存/限流。
- **自举问题**：webhook 自己的 Deployment/Secret 不能被自己的策略卡住；用 namespaceSelector 排除。

## Q.9 kubectl 插件：最低风险的扩展

kubectl 插件就是一个以 `kubectl-` 开头的可执行文件，放在 `PATH` 里：

```bash
cat > kubectl-website <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
namespace="${NAMESPACE:-default}"
kubectl get websites.example.com -n "$namespace" "$@"
EOF
chmod +x kubectl-website
mv kubectl-website /usr/local/bin/
kubectl website
```

Go 版本可以用 `k8s.io/cli-runtime` 复用 kubeconfig/打印逻辑：

```go
package main

import (
	"context"
	"fmt"
	"os"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/cli-runtime/pkg/genericclioptions"
	"k8s.io/client-go/dynamic"
)

var websiteGVR = schema.GroupVersionResource{
	Group: "example.com", Version: "v1", Resource: "websites",
}

func main() {
	flags := genericclioptions.NewConfigFlags(true)
	cfg, err := flags.ToRESTConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	dyn, err := dynamic.NewForConfig(cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	list, err := dyn.Resource(websiteGVR).Namespace("default").List(context.Background(), metav1.ListOptions{})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, item := range list.Items {
		fmt.Println(item.GetName())
	}
}
```

插件适合“读/格式化/诊断”，不适合做需要持续调谐的控制器。发布时提供多平台二进制、校验和与版本命令。

## Q.10 Helm chart：把交付物打包

### Q.10.1 最小 chart

```text
charts/website-operator/
├── Chart.yaml
├── values.yaml
├── values.schema.json
├── templates/
│   ├── _helpers.tpl
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── rbac.yaml
│   └── servicemonitor.yaml
└── tests/
    └── deployment_test.yaml
```

```yaml
apiVersion: v2
name: website-operator
description: Website operator
type: application
version: 0.1.0
appVersion: "0.1.0"
kubeVersion: ">=1.30.0-0"
```

```yaml
# values.yaml
replicaCount: 2
image:
  repository: registry.example.com/website-operator
  tag: "0.1.0"
  pullPolicy: IfNotPresent
resources:
  requests: {cpu: 100m, memory: 128Mi}
  limits: {memory: 256Mi}
leaderElection:
  enabled: true
```

```gotmpl
# templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "website-operator.fullname" . }}
  labels:
    {{- include "website-operator.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "website-operator.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "website-operator.selectorLabels" . | nindent 8 }}
    spec:
      serviceAccountName: {{ include "website-operator.fullname" . }}
      containers:
        - name: manager
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          args:
            - --leader-elect={{ .Values.leaderElection.enabled }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
```

### Q.10.2 工程化要求

- `values.schema.json` 校验值类型/必填项；CI 里跑 `helm lint` 和 `helm template`。
- 用 `helm unittest` 或 `kubectl apply --dry-run=server` 做渲染验证。
- 镜像 tag 用 digest 或不可变 tag；不要用 `latest`。
- 提供 `PodDisruptionBudget`、`NetworkPolicy`、`ServiceMonitor`、`priorityClassName` 的可选开关。
- CRD 用单独目录/安装步骤；Helm 的 `crds/` 不会随 `helm upgrade` 升级 CRD。
- 发布到 OCI registry：`helm package` + `helm push`，并签名/附 SBOM。
- 记录 chart 与 operator 的兼容矩阵（Kubernetes minor、CRD 版本）。

## Q.11 Operator 交付与升级

- **CRD 版本**：`v1alpha1 → v1beta1 → v1` 要有转换策略；存储版本只升不降。
- **Conversion webhook**：跨版本读取要可逆；对每个版本对写测试。
- **Bundle/OLM**：需要目录、CSV、权限、升级图；不适合所有团队，先用 Helm 也能交付。
- **升级顺序**：先 CRD（兼容新旧），再 controller，再清理旧版本；不要在 controller 还没支持新字段时先改 CRD 语义。
- **回滚**：CRD 删除会级联删除 CR；回滚方案必须包含数据/对象备份。

## Q.12 验收清单与实验

一个扩展交付前至少回答：

1. **API**：CRD schema 是否拒绝非法输入？版本/转换是否有测试？
2. **控制器**：是否幂等？重启后能否收敛？finalizer 是否可能永久卡住？
3. **权限**：RBAC 是否最小？是否用了 `*` 或 cluster-admin？
4. **可用性**：webhook 的 failurePolicy/timeout/副本/PDB 是否合理？自举是否安全？
5. **可观测**：指标、事件、日志能否定位“为什么不工作”？
6. **升级**：CRD/controller/chart 的升级与回滚顺序是否演练过？
7. **交付**：镜像是否签名/扫描？chart 是否 lint/测试？是否有 SBOM 与支持矩阵？

实验（按顺序）：

1. 用 client-go 列出某 namespace 的 Pod，处理分页与 410。
2. 用 dynamic client 读取一个 CRD，打印 spec/status。
3. 用 informer + workqueue 统计 Pod 变化，观察重启后的初始 List。
4. 用 controller-runtime 实现一个最小 Website controller，envtest 覆盖创建/更新/删除。
5. 给 Website 加一个 validating webhook，用 `kubectl apply --dry-run=server` 验证拒绝路径。
6. 用 Helm 打包 controller，`helm lint`/`helm template`/`helm install --dry-run` 全通过。
7. 在 kind 上安装，删除 controller Pod 观察 leader election 与恢复。

## 延伸阅读

- [第 18 章 控制器模式与 Informer/Workqueue](../chapters/18-controller-pattern.md)：Reflector/DeltaFIFO/Indexer/限速队列的机制。
- [第 19 章 CRD 与 Operator 工程化](../chapters/19-crd-operator.md)：schema、转换、Kubebuilder 流程。
- [第 24 章 准入控制与供应链安全](../chapters/24-admission-supply-chain.md)：webhook 高可用、VAP/CEL、签名验证。
- [第 32 章 GitOps 与渐进式交付](../chapters/32-gitops-progressive-delivery.md)：chart 如何进入 GitOps。
- [第 40 章 与真实 Kubernetes 的差距及替换路线](../chapters/40-gap-and-replacement.md)：从 MiniK8s 到真实控制器。
- [附录 G 上游源码导读](G-source-reading.md)：client-go/controller-runtime 源码路径。
- [附录 J 自测题库与实验手册](J-self-assessment-labs.md)：更多动手题。
