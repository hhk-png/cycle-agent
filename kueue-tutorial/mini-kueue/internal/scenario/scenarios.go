// Package scenario contains reproducible setups used by the CLI and tests.
package scenario

import (
	"fmt"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/controller"
	"example.com/minikueue/internal/store"
)

// New creates an engine with an empty store.
func New() *controller.Engine {
	return controller.New(store.New())
}

// MustCQ inserts a ClusterQueue or panics. Scenarios are fixtures; a panic is
// preferable to silently running a different experiment.
func MustCQ(e *controller.Engine, cq *api.ClusterQueue) {
	if err := e.Store.UpsertClusterQueue(cq); err != nil {
		panic(err)
	}
}

// MustCohort inserts a Cohort.
func MustCohort(e *controller.Engine, c *api.Cohort) {
	if err := e.Store.UpsertCohort(c); err != nil {
		panic(err)
	}
}

// MustLQ inserts a LocalQueue.
func MustLQ(e *controller.Engine, namespace, name, cq string) {
	if err := e.Store.UpsertLocalQueue(&api.LocalQueue{Namespace: namespace, Name: name, ClusterQueue: cq}); err != nil {
		panic(err)
	}
}

// MustNamespace registers namespace labels.
func MustNamespace(e *controller.Engine, name string, labels map[string]string) {
	e.Store.UpsertNamespace(name, labels)
}

// MustCheck inserts an AdmissionCheck.
func MustCheck(e *controller.Engine, name, controllerName string) {
	e.Store.UpsertAdmissionCheck(&api.AdmissionCheck{Name: name, ControllerName: controllerName})
}

// MustFlavor inserts a ResourceFlavor.
func MustFlavor(e *controller.Engine, f *api.ResourceFlavor) {
	e.Store.UpsertResourceFlavor(f)
}

// MustTopology inserts a Topology.
func MustTopology(e *controller.Engine, t *api.Topology) {
	if err := e.Store.UpsertTopology(t); err != nil {
		panic(err)
	}
}

// MustTopologyDomain inserts a leaf topology domain.
func MustTopologyDomain(e *controller.Engine, d *api.TopologyDomain) {
	if err := e.Store.UpsertTopologyDomain(d); err != nil {
		panic(err)
	}
}

// MustProvider inserts a CapacityProvider.
func MustProvider(e *controller.Engine, p *api.CapacityProvider) {
	if err := e.Store.UpsertCapacityProvider(p); err != nil {
		panic(err)
	}
}

// Workload builds an active workload with one pod set.
func Workload(namespace, name, queue string, priority int32, pods int32, requests api.ResourceList) *api.Workload {
	return &api.Workload{
		Namespace: namespace,
		Name:      name,
		Spec: api.WorkloadSpec{
			Active:    true,
			QueueName: queue,
			Priority:  priority,
			PodSets: []api.PodSet{{
				Name:     "main",
				Count:    pods,
				Requests: requests.Clone(),
			}},
		},
	}
}

// MustSubmit inserts a workload.
func MustSubmit(e *controller.Engine, w *api.Workload) {
	if err := e.Submit(w); err != nil {
		panic(err)
	}
}

// SimpleCQ creates a ClusterQueue with one resource group and one flavor.
func SimpleCQ(name, flavor string, quota api.ResourceList) *api.ClusterQueue {
	return &api.ClusterQueue{
		Name: name,
		Spec: api.ClusterQueueSpec{
			NamespaceSelector: &api.LabelSelector{},
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: resourceNames(quota),
				Flavors: []api.FlavorQuota{{
					Name:    flavor,
					Nominal: quota.Clone(),
				}},
			}},
			QueueingStrategy: api.BestEffortFIFO,
		},
	}
}

func resourceNames(r api.ResourceList) []api.ResourceName {
	names := make([]api.ResourceName, 0, len(r))
	for name := range r {
		names = append(names, name)
	}
	// Stable order is important for the first-fit flavor algorithm and for
	// readable tests.
	for i := 0; i < len(names); i++ {
		for j := i + 1; j < len(names); j++ {
			if names[j] < names[i] {
				names[i], names[j] = names[j], names[i]
			}
		}
	}
	return names
}

// Basic demonstrates FIFO admission followed by a quota release.
func Basic() *controller.Engine {
	e := New()
	MustCQ(e, SimpleCQ("basic", "default", api.RL("cpu", 2000, "memory", 2048)))
	MustLQ(e, "default", "q", "basic")
	MustNamespace(e, "default", nil)
	MustSubmit(e, Workload("default", "job-a", "q", 0, 1, api.RL("cpu", 1000, "memory", 512)))
	MustSubmit(e, Workload("default", "job-b", "q", 0, 1, api.RL("cpu", 1000, "memory", 512)))
	MustSubmit(e, Workload("default", "job-c", "q", 0, 1, api.RL("cpu", 1000, "memory", 512)))
	return e
}

// CohortBorrow demonstrates a tenant using unused quota from a peer.
func CohortBorrow() *controller.Engine {
	e := New()
	MustCohort(e, &api.Cohort{Name: "shared"})
	MustCQ(e, cohortCQ("team-a", 4000))
	MustCQ(e, cohortCQ("team-b", 4000))
	MustLQ(e, "team-a", "q", "team-a")
	MustLQ(e, "team-b", "q", "team-b")
	MustNamespace(e, "team-a", nil)
	MustNamespace(e, "team-b", nil)
	MustSubmit(e, Workload("team-a", "a-large", "q", 0, 1, api.RL("cpu", 6000)))
	MustSubmit(e, Workload("team-b", "b-normal", "q", 0, 1, api.RL("cpu", 4000)))
	return e
}

func cohortCQ(name string, cpu int64) *api.ClusterQueue {
	cq := SimpleCQ(name, "default", api.RL("cpu", cpu))
	cq.Spec.CohortName = "shared"
	return cq
}

// Preemption demonstrates a higher-priority workload evicting a lower-priority
// one from the same ClusterQueue.
func Preemption() *controller.Engine {
	e := New()
	cq := SimpleCQ("preempt", "default", api.RL("cpu", 2000))
	cq.Spec.Preemption.WithinClusterQueue = api.PreemptLower
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "preempt")
	MustNamespace(e, "default", nil)
	low := Workload("default", "low", "q", 0, 1, api.RL("cpu", 2000))
	low.Spec.CreationTimestamp = 1
	MustSubmit(e, low)
	// Admit the low-priority workload first so the scenario demonstrates an
	// actual eviction rather than priority ordering at submission time.
	e.Tick()
	high := Workload("default", "high", "q", 10, 1, api.RL("cpu", 1000))
	high.Spec.CreationTimestamp = 2
	MustSubmit(e, high)
	return e
}

// Reclaim demonstrates the owner of nominal quota reclaiming it from a
// borrowing peer.
func Reclaim() *controller.Engine {
	e := New()
	MustCohort(e, &api.Cohort{Name: "shared"})
	a := cohortCQ("team-a", 4000)
	a.Spec.Preemption.ReclaimWithinCohort = api.PreemptLower
	b := cohortCQ("team-b", 4000)
	b.Spec.Preemption.ReclaimWithinCohort = api.PreemptNever
	MustCQ(e, a)
	MustCQ(e, b)
	MustLQ(e, "team-a", "q", "team-a")
	MustLQ(e, "team-b", "q", "team-b")
	MustNamespace(e, "team-a", nil)
	MustNamespace(e, "team-b", nil)
	a1 := Workload("team-a", "a-steady", "q", 10, 1, api.RL("cpu", 2000))
	a1.Spec.CreationTimestamp = 1
	b1 := Workload("team-b", "b-borrower", "q", 0, 1, api.RL("cpu", 6000))
	b1.Spec.CreationTimestamp = 2
	MustSubmit(e, a1)
	MustSubmit(e, b1)
	// Let team-a and team-b become admitted, with team-b borrowing 2 CPUs,
	// before team-a submits the workload that triggers reclamation.
	e.Tick()
	a2 := Workload("team-a", "a-owner", "q", 10, 1, api.RL("cpu", 2000))
	a2.Spec.CreationTimestamp = 3
	MustSubmit(e, a2)
	return e
}

// AdmissionCheck demonstrates the two-phase admission lifecycle.
func AdmissionCheck() *controller.Engine {
	e := New()
	cq := SimpleCQ("checked", "default", api.RL("cpu", 2000))
	cq.Spec.AdmissionChecks = []string{"manual-check"}
	MustCQ(e, cq)
	MustCheck(e, "manual-check", "manual")
	MustLQ(e, "default", "q", "checked")
	MustNamespace(e, "default", nil)
	MustSubmit(e, Workload("default", "checked-job", "q", 0, 1, api.RL("cpu", 1000)))
	return e
}

// StrictVersusBestEffort demonstrates head-of-line blocking.
func StrictVersusBestEffort(strategy api.QueueingStrategy) *controller.Engine {
	e := New()
	cq := SimpleCQ("blocking", "default", api.RL("cpu", 2000))
	cq.Spec.QueueingStrategy = strategy
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "blocking")
	MustNamespace(e, "default", nil)
	big := Workload("default", "big-head", "q", 0, 1, api.RL("cpu", 3000))
	big.Spec.CreationTimestamp = 1
	MustSubmit(e, big)
	small := Workload("default", "small-tail", "q", 0, 1, api.RL("cpu", 1000))
	small.Spec.CreationTimestamp = 2
	MustSubmit(e, small)
	return e
}

// TopologyAware demonstrates TAS: a 4-GPU job must fit inside one rack.
// Each rack has two hosts with two GPUs each, so rack capacity (4) is what
// matters, not host capacity (2).
func TopologyAware() *controller.Engine {
	e := New()
	MustTopology(e, &api.Topology{Name: "gpu-topology", Levels: []string{"rack", "hostname"}})
	for _, d := range []struct {
		name string
		rack string
		host string
	}{
		{"rack-a/h1", "rack-a", "h1"},
		{"rack-a/h2", "rack-a", "h2"},
		{"rack-b/h3", "rack-b", "h3"},
		{"rack-b/h4", "rack-b", "h4"},
	} {
		MustTopologyDomain(e, &api.TopologyDomain{
			Name:     d.name,
			Topology: "gpu-topology",
			Values:   []string{d.rack, d.host},
			Capacity: api.RL("cpu", 8000, "nvidia.com/gpu", 2, "pods", 8),
		})
	}
	MustFlavor(e, &api.ResourceFlavor{Name: "tas-flavor", TopologyName: "gpu-topology"})
	cq := &api.ClusterQueue{
		Name: "tas",
		Spec: api.ClusterQueueSpec{
			NamespaceSelector: &api.LabelSelector{},
			QueueingStrategy:  api.BestEffortFIFO,
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU, "nvidia.com/gpu", api.Pods},
				Flavors: []api.FlavorQuota{{
					Name: "tas-flavor",
					Nominal: api.RL(
						"cpu", 32000,
						"nvidia.com/gpu", 12,
						"pods", 40,
					),
				}},
			}},
		},
	}
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "tas")
	MustNamespace(e, "default", nil)
	for i, name := range []string{"job-a", "job-b", "job-c"} {
		w := Workload("default", name, "q", 0, 4,
			api.RL("cpu", 1000, "nvidia.com/gpu", 1))
		w.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "rack"}
		w.Spec.CreationTimestamp = int64(i + 1)
		MustSubmit(e, w)
	}
	return e
}

// PodsReady demonstrates that an admitted workload which never reports ready
// pods is evicted instead of occupying quota forever.
func PodsReady() *controller.Engine {
	e := New()
	e.WaitForPodsReady = &controller.WaitForPodsReadyConfig{Timeout: 3, MaxRetries: 0}
	MustCQ(e, SimpleCQ("ready", "default", api.RL("cpu", 2000)))
	MustLQ(e, "default", "q", "ready")
	MustNamespace(e, "default", nil)
	MustSubmit(e, Workload("default", "ready-job", "q", 0, 1, api.RL("cpu", 1000)))
	MustSubmit(e, Workload("default", "stuck-job", "q", 0, 1, api.RL("cpu", 1000)))
	return e
}

// DynamicReclaim demonstrates a job returning unused pods to the queue.
func DynamicReclaim() *controller.Engine {
	e := New()
	MustCQ(e, SimpleCQ("reclaimable", "default", api.RL("cpu", 4000)))
	MustLQ(e, "default", "q", "reclaimable")
	MustNamespace(e, "default", nil)
	MustSubmit(e, Workload("default", "elastic-job", "q", 0, 2, api.RL("cpu", 2000)))
	MustSubmit(e, Workload("default", "waiting-job", "q", 0, 1, api.RL("cpu", 2000)))
	return e
}

// DynamicQuota demonstrates a capacity provider shrinking two queues'
// effective nominal quota from 4000/4000 to 3000/3000.
func DynamicQuota() *controller.Engine {
	e := New()
	MustProvider(e, &api.CapacityProvider{
		Name: "gpu-pool",
		Capacity: map[string]api.ResourceList{
			"default": api.RL("cpu", 6000),
		},
	})
	a := SimpleCQ("team-a", "default", api.RL("cpu", 4000))
	a.Spec.CapacityProvider = "gpu-pool"
	b := SimpleCQ("team-b", "default", api.RL("cpu", 4000))
	b.Spec.CapacityProvider = "gpu-pool"
	MustCQ(e, a)
	MustCQ(e, b)
	MustLQ(e, "team-a", "q", "team-a")
	MustLQ(e, "team-b", "q", "team-b")
	MustNamespace(e, "team-a", nil)
	MustNamespace(e, "team-b", nil)
	MustSubmit(e, Workload("team-a", "too-big", "q", 0, 1, api.RL("cpu", 4000)))
	MustSubmit(e, Workload("team-b", "fits", "q", 0, 1, api.RL("cpu", 3000)))
	return e
}

// LabelSelector demonstrates set-based namespaceSelector requirements.
func LabelSelector() *controller.Engine {
	e := New()
	cq := SimpleCQ("selective", "default", api.RL("cpu", 2000))
	cq.Spec.NamespaceSelector = &api.LabelSelector{
		MatchLabels: map[string]string{"environment": "production"},
		MatchExpressions: []api.LabelSelectorRequirement{{
			Key:      "team",
			Operator: "In",
			Values:   []string{"blue", "green"},
		}},
	}
	MustCQ(e, cq)
	for _, ns := range []string{"blue", "red", "green"} {
		MustLQ(e, ns, "q", "selective")
	}
	MustNamespace(e, "blue", map[string]string{"environment": "production", "team": "blue"})
	MustNamespace(e, "red", map[string]string{"environment": "production", "team": "red"})
	MustNamespace(e, "green", map[string]string{"environment": "development", "team": "green"})
	MustSubmit(e, Workload("blue", "blue-ok", "q", 0, 1, api.RL("cpu", 1000)))
	MustSubmit(e, Workload("red", "red-blocked", "q", 0, 1, api.RL("cpu", 1000)))
	MustSubmit(e, Workload("green", "green-blocked", "q", 0, 1, api.RL("cpu", 1000)))
	return e
}

// LocalQueueHold demonstrates that a LocalQueue stopPolicy only blocks its own
// workloads while other queues sharing the ClusterQueue continue.
func LocalQueueHold() *controller.Engine {
	e := New()
	MustCQ(e, SimpleCQ("shared", "default", api.RL("cpu", 2000)))
	MustLQ(e, "default", "open", "shared")
	if err := e.Store.UpsertLocalQueue(&api.LocalQueue{
		Namespace: "default", Name: "held", ClusterQueue: "shared", StopPolicy: api.StopHold,
	}); err != nil {
		panic(err)
	}
	MustNamespace(e, "default", nil)
	MustSubmit(e, Workload("default", "open-job", "open", 0, 1, api.RL("cpu", 1000)))
	MustSubmit(e, Workload("default", "held-job", "held", 0, 1, api.RL("cpu", 1000)))
	return e
}

// RetryBackoff demonstrates an AdmissionCheck Retry with requeueAfterSeconds.
// The operator retries once after a two-tick backoff and then approves.
func RetryBackoff() *controller.Engine {
	e := New()
	cq := SimpleCQ("retry", "default", api.RL("cpu", 2000))
	cq.Spec.AdmissionChecks = []string{"capacity-check"}
	MustCQ(e, cq)
	MustCheck(e, "capacity-check", "manual")
	MustLQ(e, "default", "q", "retry")
	MustNamespace(e, "default", nil)
	MustSubmit(e, Workload("default", "retry-job", "q", 0, 1, api.RL("cpu", 1000)))
	return e
}

// DRA demonstrates DRA-lite device accounting: ResourceClaimTemplate-style
// device requests are mapped to a logical quota resource. The second workload
// does not fit even though the Pod template only mentions devices.
func DRA() *controller.Engine {
	e := New()
	cq := SimpleCQ("dra", "default", api.RL("cpu", 8000, "nvidia.com/gpu", 6))
	cq.Spec.DeviceClassMappings = map[string]api.ResourceName{
		"gpu-a100": "nvidia.com/gpu",
	}
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "dra")
	MustNamespace(e, "default", nil)
	for i, name := range []string{"dra-job-a", "dra-job-b"} {
		w := Workload("default", name, "q", 0, 4, api.RL("cpu", 1000))
		w.Spec.PodSets[0].Devices = []api.DeviceRequest{{DeviceClass: "gpu-a100", Count: 1}}
		w.Spec.CreationTimestamp = int64(i + 1)
		MustSubmit(e, w)
	}
	return e
}

// ResourceTransform demonstrates Kueue-style resource transformations. Pods
// request vGPU memory/cores plus a vGPU count; the quota ledger instead tracks
// total memory and total cores, and the vGPU count itself is excluded. This is
// the same shape as the HAMi example in the Kueue documentation.
func ResourceTransform() *controller.Engine {
	e := New()
	transformations := []api.ResourceTransformation{
		{
			Input: "example.com/gpumem", Strategy: api.TransformReplace, MultiplyBy: "example.com/gpu",
			Outputs: api.RL("example.com/total-gpumem", 1),
		},
		{
			Input: "example.com/gpucores", Strategy: api.TransformReplace, MultiplyBy: "example.com/gpu",
			Outputs: api.RL("example.com/total-gpucores", 1),
		},
		// Empty outputs with Replace excludes the vGPU count from quota.
		{Input: "example.com/gpu", Strategy: api.TransformReplace},
	}
	if err := e.SetResourceTransformations(transformations); err != nil {
		panic(err)
	}
	cq := &api.ClusterQueue{
		Name: "transform",
		Spec: api.ClusterQueueSpec{
			NamespaceSelector: &api.LabelSelector{},
			QueueingStrategy:  api.BestEffortFIFO,
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{
					"example.com/total-gpumem", "example.com/total-gpucores", api.Pods,
				},
				Flavors: []api.FlavorQuota{{
					Name: "vgpu",
					Nominal: api.RL(
						"example.com/total-gpumem", 4096,
						"example.com/total-gpucores", 80,
						api.Pods, 4,
					),
				}},
			}},
		},
	}
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "transform")
	MustNamespace(e, "default", nil)
	for i, name := range []string{"vgpu-a", "vgpu-b", "vgpu-c"} {
		w := Workload("default", name, "q", 0, 1, api.RL(
			"example.com/gpu", 2,
			"example.com/gpumem", 1024,
			"example.com/gpucores", 20,
		))
		w.Spec.CreationTimestamp = int64(i + 1)
		MustSubmit(e, w)
	}
	return e
}

// ConfigurablePreemption demonstrates alpha PreemptionConfig rules. The
// ClusterQueue keeps classic preemption disabled; a config rule allows a
// higher-priority hero workload to evict a lower-priority batch workload in
// the same queue, provided the victim carries the expected label.
func ConfigurablePreemption() *controller.Engine {
	e := New()
	cq := SimpleCQ("configurable", "default", api.RL("cpu", 4000))
	// Deliberately no classic preemption: only the PreemptionConfig rule can
	// authorize an eviction.
	cq.Spec.PreemptionConfigName = "hero-protection"
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "configurable")
	MustNamespace(e, "default", nil)
	config := &api.PreemptionConfig{
		Name: "hero-protection",
		Rules: []api.PreemptionRule{{
			Name:    "evict-batch",
			Trigger: api.TriggerInsufficientQuota,
			CandidateSelectors: []api.PreemptionCandidateSelector{{
				Scope:    api.ScopeWithinClusterQueue,
				Priority: &api.PriorityConstraint{Mode: api.PriorityBase, Comparison: api.CompareLessThan},
				LabelSelector: &api.LabelSelector{MatchLabels: map[string]string{
					"workload-type": "batch",
				}},
			}},
		}},
	}
	if err := e.SetPreemptionConfig(config); err != nil {
		panic(err)
	}
	low := Workload("default", "batch-job", "q", 0, 1, api.RL("cpu", 4000))
	low.Labels = map[string]string{"workload-type": "batch"}
	low.Spec.CreationTimestamp = 1
	MustSubmit(e, low)
	// Admit the victim first so the scenario demonstrates an actual eviction.
	e.Tick()
	high := Workload("default", "hero-job", "q", 10, 1, api.RL("cpu", 4000))
	high.Labels = map[string]string{"workload-type": "hero"}
	high.Spec.CreationTimestamp = 2
	MustSubmit(e, high)
	return e
}

// FairSharingPreemption demonstrates fair-sharing preemption. ClusterQueue
// team-a borrows 2 CPU beyond its 4 CPU nominal; when team-b (which has been
// idle) submits a 4 CPU job, fair sharing lets it reclaim the borrowed
// capacity even though classic preemption is disabled.
func FairSharingPreemption() *controller.Engine {
	e := New()
	MustCohort(e, &api.Cohort{Name: "shared"})
	a := SimpleCQ("team-a", "default", api.RL("cpu", 4000))
	a.Spec.CohortName = "shared"
	b := SimpleCQ("team-b", "default", api.RL("cpu", 4000))
	b.Spec.CohortName = "shared"
	MustCQ(e, a)
	MustCQ(e, b)
	MustLQ(e, "team-a", "q", "team-a")
	MustLQ(e, "team-b", "q", "team-b")
	MustNamespace(e, "team-a", nil)
	MustNamespace(e, "team-b", nil)
	if err := e.SetFairSharingPreemption([]string{"LessThanOrEqualToFinalShare"}); err != nil {
		panic(err)
	}
	borrower := Workload("team-a", "borrower", "q", 0, 3, api.RL("cpu", 2000))
	borrower.Spec.CreationTimestamp = 1
	MustSubmit(e, borrower)
	e.Tick()
	reclaimer := Workload("team-b", "reclaimer", "q", 10, 2, api.RL("cpu", 2000))
	reclaimer.Spec.CreationTimestamp = 2
	MustSubmit(e, reclaimer)
	return e
}

// ProvisioningRequest demonstrates a ProvisioningRequest-style
// AdmissionCheck: the first provisioning attempt fails, the reservation is
// released and retried, and the second attempt becomes Ready after the
// configured provisioning delay.
func ProvisioningRequest() *controller.Engine {
	e := New()
	cq := SimpleCQ("provisioning", "default", api.RL("cpu", 4000))
	cq.Spec.AdmissionChecks = []string{"provisioning-check"}
	MustCQ(e, cq)
	MustCheck(e, "provisioning-check", "provisioning")
	MustLQ(e, "default", "q", "provisioning")
	MustNamespace(e, "default", nil)
	e.Provisioning = &controller.ProvisioningConfig{
		CheckName:  "provisioning-check",
		DelayTicks: 2,
		Failures:   1,
	}
	MustSubmit(e, Workload("default", "provisioned-job", "q", 0, 2, api.RL("cpu", 1000)))
	return e
}

// TASSlice demonstrates TAS slicing and spreading. Each host has four GPUs.
// A one-pod job lands on h1; a spreading two-pod job prefers the less-used h2;
// a four-pod job sliced by two occupies two more hosts.
func TASSlice() *controller.Engine {
	e := New()
	MustTopology(e, &api.Topology{Name: "gpu-topology", Levels: []string{"rack", "hostname"}})
	for _, d := range []struct {
		name string
		rack string
		host string
	}{
		{"rack-a/h1", "rack-a", "h1"},
		{"rack-a/h2", "rack-a", "h2"},
		{"rack-b/h3", "rack-b", "h3"},
		{"rack-b/h4", "rack-b", "h4"},
	} {
		MustTopologyDomain(e, &api.TopologyDomain{
			Name:     d.name,
			Topology: "gpu-topology",
			Values:   []string{d.rack, d.host},
			Capacity: api.RL("cpu", 8000, "nvidia.com/gpu", 4, "pods", 8),
		})
	}
	MustFlavor(e, &api.ResourceFlavor{Name: "tas-flavor", TopologyName: "gpu-topology"})
	cq := &api.ClusterQueue{
		Name: "tas-slice",
		Spec: api.ClusterQueueSpec{
			NamespaceSelector: &api.LabelSelector{},
			QueueingStrategy:  api.BestEffortFIFO,
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU, "nvidia.com/gpu", api.Pods},
				Flavors: []api.FlavorQuota{{
					Name: "tas-flavor",
					Nominal: api.RL(
						"cpu", 32000,
						"nvidia.com/gpu", 16,
						"pods", 32,
					),
				}},
			}},
		},
	}
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "tas-slice")
	MustNamespace(e, "default", nil)

	one := Workload("default", "one-pod", "q", 0, 1, api.RL("cpu", 1000, "nvidia.com/gpu", 1))
	one.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "hostname"}
	one.Spec.CreationTimestamp = 1
	MustSubmit(e, one)

	spread := Workload("default", "spread-two", "q", 0, 2, api.RL("cpu", 1000, "nvidia.com/gpu", 1))
	spread.Spec.PodSets[0].Topology = &api.PodSetTopology{
		Mode: api.TopologyRequired, Level: "hostname", Spreading: true,
	}
	spread.Spec.CreationTimestamp = 2
	MustSubmit(e, spread)

	sliced := Workload("default", "slice-four", "q", 0, 4, api.RL("cpu", 1000, "nvidia.com/gpu", 1))
	sliced.Spec.PodSets[0].Topology = &api.PodSetTopology{
		Mode: api.TopologyRequired, Level: "hostname", SliceSize: 2,
	}
	sliced.Spec.CreationTimestamp = 3
	MustSubmit(e, sliced)
	return e
}

// AdmissionFairSharing demonstrates UsageBasedAdmissionFairSharing: the
// LocalQueue with more historical usage is admitted after the quieter one.
func AdmissionFairSharing() *controller.Engine {
	e := New()
	e.AFS = &controller.AdmissionFairSharingConfig{
		HalfLifeTicks:   100,
		EntryPenalty:    0,
		ResourceWeights: map[api.ResourceName]float64{api.CPU: 1},
	}
	cq := SimpleCQ("afs", "default", api.RL("cpu", 2000))
	cq.Spec.AdmissionScope = api.AdmissionScopeUsageBasedAdmissionSharing
	MustCQ(e, cq)
	MustLQ(e, "default", "heavy", "afs")
	MustLQ(e, "default", "light", "afs")
	MustNamespace(e, "default", nil)

	// Build up history for "heavy", then release the quota.
	pre := Workload("default", "heavy-pre", "heavy", 0, 1, api.RL("cpu", 2000))
	pre.Spec.CreationTimestamp = 1
	MustSubmit(e, pre)
	e.Tick()
	if err := e.Finish("default", "heavy-pre"); err != nil {
		panic(err)
	}

	heavy := Workload("default", "heavy-next", "heavy", 0, 1, api.RL("cpu", 2000))
	heavy.Spec.CreationTimestamp = 2
	MustSubmit(e, heavy)
	light := Workload("default", "light-next", "light", 0, 1, api.RL("cpu", 2000))
	light.Spec.CreationTimestamp = 3
	MustSubmit(e, light)
	return e
}

// ConcurrentAdmission demonstrates flavor migration. The reserved flavor has
// zero quota initially, so the workload starts on spot; once reserved capacity
// appears, the controller migrates it to the preferred flavor.
func ConcurrentAdmission() *controller.Engine {
	e := New()
	cq := &api.ClusterQueue{
		Name: "concurrent",
		Spec: api.ClusterQueueSpec{
			NamespaceSelector:   &api.LabelSelector{},
			QueueingStrategy:    api.BestEffortFIFO,
			ConcurrentAdmission: &api.ConcurrentAdmissionPolicy{},
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU},
				Flavors: []api.FlavorQuota{
					{Name: "reserved", Nominal: api.RL("cpu", 0)},
					{Name: "spot", Nominal: api.RL("cpu", 4000)},
				},
			}},
		},
	}
	MustCQ(e, cq)
	MustLQ(e, "default", "q", "concurrent")
	MustNamespace(e, "default", nil)
	MustSubmit(e, Workload("default", "migrating-job", "q", 0, 1, api.RL("cpu", 2000)))
	return e
}

// AdmissionGate demonstrates a pre-admission external gate. The workload
// cannot reserve quota until the gate is cleared.
func AdmissionGate() *controller.Engine {
	e := New()
	MustCQ(e, SimpleCQ("gated", "default", api.RL("cpu", 2000)))
	MustLQ(e, "default", "q", "gated")
	MustNamespace(e, "default", nil)
	w := Workload("default", "gated-job", "q", 0, 1, api.RL("cpu", 1000))
	w.Spec.AdmissionGates = []string{"security-scan", "license-check"}
	MustSubmit(e, w)
	return e
}

// HierarchicalLimits demonstrates a cohort-level borrowingLimit. org-a may
// borrow at most 2 CPUs from its parent even though org-b has 4 idle CPUs.
func HierarchicalLimits() *controller.Engine {
	e := New()
	MustCohort(e, &api.Cohort{Name: "root"})
	MustCohort(e, &api.Cohort{
		Name:       "org-a",
		ParentName: "root",
		ResourceGroups: []api.ResourceGroup{{
			CoveredResources: []api.ResourceName{api.CPU},
			Flavors: []api.FlavorQuota{{
				Name:           "default",
				BorrowingLimit: api.RL("cpu", 2000),
			}},
		}},
	})
	MustCohort(e, &api.Cohort{
		Name:       "org-b",
		ParentName: "root",
		ResourceGroups: []api.ResourceGroup{{
			CoveredResources: []api.ResourceName{api.CPU},
			Flavors: []api.FlavorQuota{{
				Name:         "default",
				LendingLimit: api.RL("cpu", 3000),
			}},
		}},
	})

	a := SimpleCQ("team-a", "default", api.RL("cpu", 4000))
	a.Spec.CohortName = "org-a"
	b := SimpleCQ("team-b", "default", api.RL("cpu", 4000))
	b.Spec.CohortName = "org-b"
	MustCQ(e, a)
	MustCQ(e, b)
	MustLQ(e, "team-a", "q", "team-a")
	MustLQ(e, "team-b", "q", "team-b")
	MustNamespace(e, "team-a", nil)
	MustNamespace(e, "team-b", nil)

	within := Workload("team-a", "within-limit", "q", 0, 1, api.RL("cpu", 6000))
	within.Spec.CreationTimestamp = 1
	MustSubmit(e, within)
	over := Workload("team-a", "over-limit", "q", 0, 1, api.RL("cpu", 7000))
	over.Spec.CreationTimestamp = 2
	MustSubmit(e, over)
	return e
}

// ImplicitCohort demonstrates borrowing between ClusterQueues that share a
// cohortName but have no Cohort object.
func ImplicitCohort() *controller.Engine {
	e := New()
	a := SimpleCQ("implicit-a", "default", api.RL("cpu", 4000))
	a.Spec.CohortName = "implicit-shared"
	b := SimpleCQ("implicit-b", "default", api.RL("cpu", 4000))
	b.Spec.CohortName = "implicit-shared"
	MustCQ(e, a)
	MustCQ(e, b)
	MustLQ(e, "team-a", "q", "implicit-a")
	MustLQ(e, "team-b", "q", "implicit-b")
	MustNamespace(e, "team-a", nil)
	MustNamespace(e, "team-b", nil)
	MustSubmit(e, Workload("team-a", "borrower", "q", 0, 1, api.RL("cpu", 6000)))
	MustSubmit(e, Workload("team-b", "owner", "q", 0, 1, api.RL("cpu", 4000)))
	return e
}

// CohortSharedPool demonstrates an explicit Cohort that contributes its own
// nominal quota, beyond the sum of member ClusterQueues. Both member queues
// have zero nominal, so every admitted CPU comes from the Cohort's shared pool.
func CohortSharedPool() *controller.Engine {
	e := New()
	MustCohort(e, &api.Cohort{
		Name: "shared-pool",
		ResourceGroups: []api.ResourceGroup{{
			CoveredResources: []api.ResourceName{api.CPU},
			Flavors: []api.FlavorQuota{{
				Name:    "default",
				Nominal: api.RL("cpu", 4000),
			}},
		}},
	})
	a := SimpleCQ("pool-a", "default", api.RL("cpu", 0))
	a.Spec.CohortName = "shared-pool"
	b := SimpleCQ("pool-b", "default", api.RL("cpu", 0))
	b.Spec.CohortName = "shared-pool"
	MustCQ(e, a)
	MustCQ(e, b)
	MustLQ(e, "team-a", "q", "pool-a")
	MustLQ(e, "team-b", "q", "pool-b")
	MustNamespace(e, "team-a", nil)
	MustNamespace(e, "team-b", nil)
	MustSubmit(e, Workload("team-a", "first", "q", 0, 1, api.RL("cpu", 3000)))
	MustSubmit(e, Workload("team-b", "second", "q", 0, 1, api.RL("cpu", 3000)))
	return e
}

// Names returns scenario names in stable order.
func Names() []string {
	return []string{
		"basic",
		"cohort-borrow",
		"preemption",
		"configurable-preemption",
		"fair-sharing-preemption",
		"reclaim",
		"admission-check",
		"provisioning-request",
		"strict-fifo",
		"best-effort-fifo",
		"tas",
		"pods-ready",
		"dynamic-reclaim",
		"dynamic-quota",
		"label-selector",
		"localqueue-hold",
		"retry-backoff",
		"dra",
		"resource-transform",
		"tas-slice",
		"admission-fair-sharing",
		"concurrent-admission",
		"admission-gate",
		"hierarchical-limits",
		"implicit-cohort",
		"cohort-shared-pool",
	}
}

// Run constructs a named scenario. It does not tick the engine.
func Run(name string) (*controller.Engine, string, error) {
	switch name {
	case "basic":
		return Basic(), "FIFO admission, quota exhaustion, and release after completion", nil
	case "cohort-borrow":
		return CohortBorrow(), "A ClusterQueue borrows unused cohort quota", nil
	case "preemption":
		return Preemption(), "Higher-priority workload preempts a lower-priority peer", nil
	case "configurable-preemption":
		return ConfigurablePreemption(), "A PreemptionConfig rule authorizes preemption while classic policy stays Never", nil
	case "fair-sharing-preemption":
		return FairSharingPreemption(), "Fair sharing reclaims borrowed quota from a lower-priority queue", nil
	case "reclaim":
		return Reclaim(), "A nominal-quota owner reclaims capacity from a borrower", nil
	case "admission-check":
		return AdmissionCheck(), "Quota reservation followed by an external admission check", nil
	case "provisioning-request":
		return ProvisioningRequest(), "ProvisioningRequest-lite: fail, release quota, retry, then admit", nil
	case "strict-fifo":
		return StrictVersusBestEffort(api.StrictFIFO), "StrictFIFO blocks a smaller job behind a larger head", nil
	case "best-effort-fifo":
		return StrictVersusBestEffort(api.BestEffortFIFO), "BestEffortFIFO skips an unadmittable head", nil
	case "tas":
		return TopologyAware(), "Topology-aware placement packs a 4-GPU job into one rack", nil
	case "pods-ready":
		return PodsReady(), "WaitForPodsReady evicts a workload whose pods never become ready", nil
	case "dynamic-reclaim":
		return DynamicReclaim(), "A job reclaims unused pods and releases quota", nil
	case "dynamic-quota":
		return DynamicQuota(), "CapacityProvider shrinks effective nominal quota", nil
	case "label-selector":
		return LabelSelector(), "Set-based namespaceSelector admits only matching namespaces", nil
	case "localqueue-hold":
		return LocalQueueHold(), "LocalQueue Hold blocks only its own workloads", nil
	case "retry-backoff":
		return RetryBackoff(), "AdmissionCheck Retry releases quota and requeues with backoff", nil
	case "dra":
		return DRA(), "DRA device requests are priced through DeviceClass mappings", nil
	case "resource-transform":
		return ResourceTransform(), "Resource transformations convert vGPU requests into total memory/core quota", nil
	case "tas-slice":
		return TASSlice(), "TAS slicing and spreading place PodSets across topology domains", nil
	case "admission-fair-sharing":
		return AdmissionFairSharing(), "Admission Fair Sharing admits the LocalQueue with less history first", nil
	case "concurrent-admission":
		return ConcurrentAdmission(), "A workload migrates from spot to the preferred reserved flavor", nil
	case "admission-gate":
		return AdmissionGate(), "An external admission gate blocks quota reservation until cleared", nil
	case "hierarchical-limits":
		return HierarchicalLimits(), "A cohort-level borrowingLimit caps subtree borrowing", nil
	case "implicit-cohort":
		return ImplicitCohort(), "cohortName without a Cohort object still borrows (implicit cohort)", nil
	case "cohort-shared-pool":
		return CohortSharedPool(), "An explicit Cohort contributes nominal quota beyond its members", nil
	default:
		return nil, "", fmt.Errorf("unknown scenario %q (available: %v)", name, Names())
	}
}
