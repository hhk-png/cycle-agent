// Package controller wires the queue, scheduler, cache and in-memory store
// into a deterministic reconciliation loop. One Tick is one scheduling cycle.
//
// Time is simulated: one tick is treated as one second for the purpose of
// maximum execution time. This keeps demos and tests fast and reproducible.
package controller

import (
	"fmt"
	"math"
	"sort"
	"sync"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/cache"
	"example.com/minikueue/internal/metrics"
	"example.com/minikueue/internal/queue"
	"example.com/minikueue/internal/scheduler"
	"example.com/minikueue/internal/store"
)

// Event is an audit-friendly scheduling event.
type Event struct {
	Tick     int64
	Kind     string
	Workload string
	Message  string
}

// Result is returned by Tick.
type Result struct {
	Clock  int64
	Events []Event
}

// WaitForPodsReadyConfig models Kueue's WaitForPodsReady safety net. A
// workload that does not report all pods ready within Timeout ticks is
// evicted and requeued. After MaxRetries consecutive timeouts it is
// deactivated so it cannot loop forever.
type WaitForPodsReadyConfig struct {
	Timeout    int64
	MaxRetries int32
}

// ProvisioningConfig models the observable behaviour of a
// ProvisioningRequest-style AdmissionCheck. The check holds the quota
// reservation while capacity is being provisioned, retries Failures times
// before succeeding, and only reports Ready after DelayTicks at the
// reservation. It does not talk to a cloud autoscaler.
type ProvisioningConfig struct {
	// CheckName is the AdmissionCheck this config drives.
	CheckName string
	// DelayTicks is how long provisioning takes, and the retry backoff.
	DelayTicks int64
	// Failures is the number of initial attempts that end in Retry.
	Failures int32
}

// BackoffConfig is an exponential requeue policy for evictions and
// AdmissionCheck retries that do not specify an explicit delay.
type BackoffConfig struct {
	BaseTicks  int64
	MaxTicks   int64
	Multiplier float64
}

// AdmissionFairSharingConfig models the observable parts of Kueue's
// UsageBasedAdmissionFairSharing: historical LocalQueue usage decays with a
// half-life, and each newly submitted workload pays an entry penalty so a
// tenant cannot flood the queue before sampling catches up.
type AdmissionFairSharingConfig struct {
	// HalfLifeTicks is the number of ticks after which historical usage is
	// halved. Zero disables decay.
	HalfLifeTicks float64
	// EntryPenalty is added to a LocalQueue's history when a workload is
	// submitted. It is expressed in the same weighted units as usage.
	EntryPenalty float64
	// ResourceWeights lets GPU-heavy workloads count more than CPU-only ones.
	ResourceWeights map[api.ResourceName]float64
}

// Engine is the MiniKueue control plane. It is safe for concurrent use.
type Engine struct {
	mu sync.Mutex

	clock     int64
	Store     *store.Store
	Cache     *cache.Cache
	Scheduler *scheduler.Scheduler
	Metrics   *metrics.Metrics

	// AutoApproveChecks makes every AdmissionCheck pass on the next
	// reconciliation. A check whose controllerName is "auto" is always
	// auto-approved; "manual" checks must be approved via ApproveCheck.
	AutoApproveChecks bool
	// WaitForPodsReady enables the all-or-nothing safety net. Nil disables it.
	WaitForPodsReady *WaitForPodsReadyConfig
	// AFS enables Admission Fair Sharing ordering. Nil disables it.
	AFS *AdmissionFairSharingConfig
	// Provisioning enables the ProvisioningRequest-lite AdmissionCheck.
	Provisioning *ProvisioningConfig
	// Backoff configures exponential requeue delays. Nil keeps the classic
	// fixed delays (2 ticks for preemption, immediate for an explicit Retry).
	Backoff *BackoffConfig
	// ResourceTransformations is the cluster-level resource transformation
	// list. It is copied into each Workload at Submit time so later changes do
	// not rewrite an existing reservation's accounting basis.
	ResourceTransformations []api.ResourceTransformation
	// PreemptionConfigs holds alpha configurable-preemption rules keyed by
	// config name. A ClusterQueue opts in through
	// spec.preemptionConfigName.
	PreemptionConfigs map[string]*api.PreemptionConfig
	// FairSharingPreemption enables fair-sharing preemption strategies.
	FairSharingPreemption []string

	// effective holds per-CQ effective nominal quotas computed from
	// CapacityProvider objects at the start of each tick.
	effective map[string]map[string]api.ResourceList
	// afsUsage is the decaying historical usage per LocalQueue key.
	afsUsage map[string]float64

	events []Event
}

// New creates an engine over an already-populated store.
func New(st *store.Store) *Engine {
	e := &Engine{
		Store:    st,
		Cache:    cache.New(),
		Metrics:  &metrics.Metrics{},
		afsUsage: map[string]float64{},
	}
	e.rebuildScheduler()
	return e
}

// Clock returns the current simulated tick.
func (e *Engine) Clock() int64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.clock
}

func (e *Engine) rebuildScheduler() {
	view := scheduler.NewView(
		e.Store.ListClusterQueues(),
		e.Store.ListCohorts(),
		scheduler.WithFlavors(e.Store.ListResourceFlavors()),
		scheduler.WithTopologies(e.Store.ListTopologies(), e.Store.AllTopologyDomains()),
		scheduler.WithEffectiveQuotas(e.effective),
	)
	e.Scheduler = scheduler.New(e.Cache, view)
	e.Scheduler.PriorityClasses = e.Store.PriorityClasses()
	e.Scheduler.PreemptionConfigs = e.PreemptionConfigs
	e.Scheduler.FairSharingPreemption = e.FairSharingPreemption
}

// SetFairSharingPreemption enables one or more fair-sharing preemption
// strategies. Unknown names are rejected.
func (e *Engine) SetFairSharingPreemption(strategies []string) error {
	for _, strategy := range strategies {
		switch strategy {
		case "LessThanOrEqualToFinalShare", "LessThanInitialShare":
		default:
			return fmt.Errorf("unsupported fair-sharing preemption strategy %q", strategy)
		}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.FairSharingPreemption = append([]string(nil), strategies...)
	if e.Scheduler != nil {
		e.Scheduler.FairSharingPreemption = e.FairSharingPreemption
	}
	return nil
}

// SetPreemptionConfig installs or replaces one configurable-preemption
// definition. The returned error is the same validation the real CRD webhook
// would apply.
func (e *Engine) SetPreemptionConfig(config *api.PreemptionConfig) error {
	if err := api.ValidatePreemptionConfig(config); err != nil {
		return err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.PreemptionConfigs == nil {
		e.PreemptionConfigs = map[string]*api.PreemptionConfig{}
	}
	e.PreemptionConfigs[config.Name] = config.Clone()
	if e.Scheduler != nil {
		e.Scheduler.PreemptionConfigs = e.PreemptionConfigs
	}
	return nil
}

// recomputeEffectiveQuotas distributes each CapacityProvider's reported
// capacity across the ClusterQueues that reference it, proportionally to
// their static nominal quota. The result overrides spec nominal quota for
// scheduling and fair-share calculations until the next tick.
func (e *Engine) recomputeEffectiveQuotas() {
	next := map[string]map[string]api.ResourceList{}
	providers := e.Store.ListCapacityProviders()
	if len(providers) == 0 {
		e.effective = next
		return
	}
	cqs := e.Store.ListClusterQueues()
	for _, provider := range providers {
		// Deterministic member order keeps integer remainder distribution
		// stable across runs.
		var members []*api.ClusterQueue
		for _, cq := range cqs {
			if cq.Spec.CapacityProvider == provider.Name {
				members = append(members, cq)
			}
		}
		sort.Slice(members, func(i, j int) bool { return members[i].Name < members[j].Name })
		for flavor, capacity := range provider.Capacity {
			for resource, available := range capacity {
				type share struct {
					name    string
					nominal int64
				}
				var shares []share
				var totalNominal int64
				for _, cq := range members {
					nominal := staticNominal(cq, flavor, resource)
					if nominal <= 0 {
						continue
					}
					shares = append(shares, share{name: cq.Name, nominal: nominal})
					totalNominal += nominal
				}
				if totalNominal == 0 {
					continue
				}
				var allocated int64
				for i, s := range shares {
					value := available * s.nominal / totalNominal
					if i == len(shares)-1 {
						value = available - allocated
					}
					allocated += value
					if next[s.name] == nil {
						next[s.name] = map[string]api.ResourceList{}
					}
					if next[s.name][flavor] == nil {
						next[s.name][flavor] = api.ResourceList{}
					}
					next[s.name][flavor][resource] = value
				}
			}
			// A provider that reports a flavor must report every resource the
			// subtree configures for it. A missing resource means "no capacity",
			// not "fall back to the static number"; otherwise an unreported
			// dimension would silently keep an old quota. Resource fallback is
			// only correct for flavors the provider does not report at all
			// (those are not orchestrated).
			for _, cq := range members {
				for _, group := range cq.Spec.ResourceGroups {
					for _, fq := range group.Flavors {
						if fq.Name != flavor {
							continue
						}
						if next[cq.Name] == nil {
							next[cq.Name] = map[string]api.ResourceList{}
						}
						if next[cq.Name][flavor] == nil {
							next[cq.Name][flavor] = api.ResourceList{}
						}
						for resource := range fq.Nominal {
							if _, reported := capacity[resource]; reported {
								continue
							}
							next[cq.Name][flavor][resource] = 0
						}
					}
				}
			}
		}
	}
	e.effective = next
}

func staticNominal(cq *api.ClusterQueue, flavor string, resource api.ResourceName) int64 {
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			if fq.Name == flavor {
				return fq.Nominal.Get(resource)
			}
		}
	}
	return 0
}

func (e *Engine) event(kind, workload, message string, args ...interface{}) {
	if len(args) > 0 {
		message = fmt.Sprintf(message, args...)
	}
	e.events = append(e.events, Event{Tick: e.clock, Kind: kind, Workload: workload, Message: message})
}

// SetResourceTransformations installs the cluster-level transformation list.
// It validates the list eagerly and deep-copies it. New workloads copy the
// list at Submit; existing workloads keep the list they were submitted with.
func (e *Engine) SetResourceTransformations(transformations []api.ResourceTransformation) error {
	if err := api.ValidateResourceTransformations(transformations); err != nil {
		return err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.ResourceTransformations = cloneTransformations(transformations)
	return nil
}

func cloneTransformations(in []api.ResourceTransformation) []api.ResourceTransformation {
	if in == nil {
		return nil
	}
	out := make([]api.ResourceTransformation, len(in))
	for i, transformation := range in {
		out[i] = transformation
		out[i].Outputs = transformation.Outputs.Clone()
	}
	return out
}

// Submit inserts a workload and marks it Pending.
func (e *Engine) Submit(w *api.Workload) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if w == nil {
		return fmt.Errorf("nil workload")
	}
	if len(w.Spec.ResourceTransformations) == 0 && len(e.ResourceTransformations) > 0 {
		// Copy the configuration at submit time. The Workload owns the frozen
		// list for its lifetime; changing the engine configuration later must
		// not retroactively re-price an existing reservation.
		w.Spec.ResourceTransformations = append([]api.ResourceTransformation(nil), e.ResourceTransformations...)
		for i := range w.Spec.ResourceTransformations {
			w.Spec.ResourceTransformations[i].Outputs = e.ResourceTransformations[i].Outputs.Clone()
		}
	}
	if err := api.ValidateWorkloadRefs(w, localQueueMap(e.Store), clusterQueueMap(e.Store), e.Store.PriorityClasses()); err != nil {
		return err
	}
	if w.UID == "" {
		w.UID = w.Key()
	}
	if w.Spec.CreationTimestamp == 0 {
		w.Spec.CreationTimestamp = e.clock + 1
	}
	// Submit is an upsert. Release any reservation left by a previous
	// incarnation and reset lifecycle bookkeeping so a stale backoff,
	// eviction counter or admission cannot leak into the new run.
	e.Cache.Remove(w.Key())
	w.Status.RequeueAt = 0
	w.Status.Requeues = 0
	w.Status.Evictions = 0
	w.Status.EvictionStats = nil
	w.Status.AdmittedAt = 0
	w.Status.ExecutionTicks = 0
	w.Status.Sequence = 0
	w.Status.ReclaimablePods = nil
	// Resolve DRA device mappings once at submit time so accounting is
	// self-contained and does not depend on later ClusterQueue edits.
	if lq, ok := e.Store.GetLocalQueue(w.Namespace, w.Spec.QueueName); ok {
		if cq, ok := e.Store.GetClusterQueue(lq.ClusterQueue); ok && w.Spec.DeviceClassMappings == nil {
			if len(cq.Spec.DeviceClassMappings) > 0 {
				w.Spec.DeviceClassMappings = make(map[string]api.ResourceName, len(cq.Spec.DeviceClassMappings))
				for class, resource := range cq.Spec.DeviceClassMappings {
					w.Spec.DeviceClassMappings[class] = resource
				}
			}
		}
	}
	w.Status.State = api.StatePending
	w.Status.Admission = nil
	w.Status.AdmissionChecks = nil
	w.Status.PodsReady = false
	w.Status.PodsReadyAt = 0
	w.Status.SetCondition("QuotaReserved", "False", "Pending", "waiting for quota", e.clock)
	if w.HasAdmissionGates() {
		w.Status.SetCondition("AdmissionGated", "True", "AdmissionGated",
			fmt.Sprintf("waiting for gates: %v", w.Spec.AdmissionGates), e.clock)
	} else {
		w.Status.ClearCondition("AdmissionGated")
	}
	w.Status.ClearCondition("Admitted")
	w.Status.ClearCondition("Finished")
	w.Status.ClearCondition("Evicted")
	w.Status.ClearCondition("Preempted")
	w.Status.ClearCondition("Inadmissible")
	w.Status.ClearCondition("PodsReady")
	e.recordAFSSubmit(w)
	return e.Store.UpsertWorkload(w)
}

// Finish marks a workload complete and releases its quota.
func (e *Engine) Finish(namespace, name string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	if w.Status.State == api.StateFinished {
		return nil
	}
	e.accumulateExecution(w)
	e.Cache.Remove(w.Key())
	w.Status.State = api.StateFinished
	w.Status.Admission = nil
	w.Status.AdmissionChecks = nil
	w.Status.SetCondition("Finished", "True", "Completed", "workload finished", e.clock)
	w.Status.SetCondition("QuotaReserved", "False", "Finished", "workload finished", e.clock)
	w.Status.SetCondition("Admitted", "False", "Finished", "workload finished", e.clock)
	e.Store.UpdateWorkload(w)
	e.Metrics.IncFinished()
	e.event("Finished", w.Key(), "workload finished")
	return nil
}

// Deactivate stops a workload and evicts it if it currently holds quota.
func (e *Engine) Deactivate(namespace, name string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	w.Spec.Active = false
	if e.Cache.Has(w.Key()) {
		e.evict(w, "Deactivated", "workload was deactivated", "", false, 0, false)
		return nil
	}
	w.Status.SetCondition("Inadmissible", "True", "Deactivated", "workload is inactive", e.clock)
	e.Store.UpdateWorkload(w)
	return nil
}

// Activate re-enables a workload that was deactivated or rejected. It clears
// the structural Inadmissible condition and makes the workload eligible for
// the next scheduling pass. AdmissionCheck history is preserved so retry
// counts remain meaningful.
func (e *Engine) Activate(namespace, name string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	w.Spec.Active = true
	w.Status.RequeueAt = 0
	w.Status.ClearCondition("Inadmissible")
	w.Status.SetCondition("QuotaReserved", "False", "Pending", "waiting for quota", e.clock)
	if err := e.Store.UpdateWorkload(w); err != nil {
		return err
	}
	e.event("Activated", w.Key(), "workload reactivated")
	return nil
}

// SetLocalQueueStopPolicy changes the LocalQueue emergency brake. The next
// Tick applies it to reservations and future admissions.
func (e *Engine) SetLocalQueueStopPolicy(namespace, name string, policy api.StopPolicy) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	q, ok := e.Store.GetLocalQueue(namespace, name)
	if !ok {
		return fmt.Errorf("local queue %s/%s not found", namespace, name)
	}
	q.StopPolicy = policy
	return e.Store.UpsertLocalQueue(q)
}

// ApproveCheck sets an AdmissionCheckState. state must be Ready, Retry or
// Rejected. Retry asks the controller to release the reservation and requeue;
// with no delay it is eligible again on the next scheduling pass.
func (e *Engine) ApproveCheck(namespace, name, check, state, message string) error {
	return e.approveCheck(namespace, name, check, state, message, 0)
}

// ApproveCheckRetry is ApproveCheck for the Retry state with an explicit
// minimum delay, mirroring status.admissionChecks[*].requeueAfterSeconds.
func (e *Engine) ApproveCheckRetry(namespace, name, check, message string, requeueAfterSeconds int32) error {
	if requeueAfterSeconds < 0 {
		return fmt.Errorf("requeueAfterSeconds must not be negative")
	}
	return e.approveCheck(namespace, name, check, "Retry", message, requeueAfterSeconds)
}

func (e *Engine) approveCheck(namespace, name, check, state, message string, requeueAfterSeconds int32) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if state != "Ready" && state != "Retry" && state != "Rejected" {
		return fmt.Errorf("state must be Ready, Retry or Rejected")
	}
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	if state == "Retry" {
		w.Status.SetAdmissionCheckRetry(check, message, requeueAfterSeconds)
	} else {
		w.Status.SetAdmissionCheck(check, state, message)
	}
	for i := range w.Status.AdmissionChecks {
		if w.Status.AdmissionChecks[i].Name == check {
			w.Status.AdmissionChecks[i].LastTransitionTick = e.clock
			break
		}
	}
	return e.Store.UpdateWorkload(w)
}

// MarkPodsReady reports that all pods of an admitted workload are running.
// It is the simulator's equivalent of the Job integration observing ready
// Pods; without WaitForPodsReady it is unnecessary.
func (e *Engine) MarkPodsReady(namespace, name string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	if w.Status.State != api.StateAdmitted {
		return fmt.Errorf("workload %s is not admitted", w.Key())
	}
	w.Status.PodsReady = true
	w.Status.PodsReadyAt = e.clock
	w.Status.SetCondition("PodsReady", "True", "PodsReady", "all pods are ready", e.clock)
	if err := e.Store.UpdateWorkload(w); err != nil {
		return err
	}
	e.event("PodsReady", w.Key(), "all pods are ready")
	return nil
}

// checkPodsReady implements the all-or-nothing safety net: an admitted
// workload that does not become fully ready within the timeout is evicted and
// requeued, rather than leaving half of its pods running forever.
func (e *Engine) checkPodsReady() {
	if e.WaitForPodsReady == nil || e.WaitForPodsReady.Timeout <= 0 {
		return
	}
	for _, w := range e.Store.ListWorkloads() {
		if w.Status.State != api.StateAdmitted || w.Status.PodsReady {
			continue
		}
		if w.Status.AdmittedAt <= 0 || e.clock-w.Status.AdmittedAt < e.WaitForPodsReady.Timeout {
			continue
		}
		e.Metrics.IncPodsReadyTimeout()
		e.event("PodsReadyTimeout", w.Key(), "pods did not become ready within %d ticks", e.WaitForPodsReady.Timeout)
		maxRetries := e.WaitForPodsReady.MaxRetries
		// Count only PodsReadyTimeout evictions. Requeues also includes
		// preemptions, and using it here would deactivate a workload that was
		// preempted once and then missed one readiness deadline.
		if maxRetries > 0 && evictionCount(&w.Status, "PodsReadyTimeout") >= maxRetries {
			e.evict(w, "Deactivated", "pods never became ready; requeuing limit exceeded", "RequeuingLimitExceeded", false, 0, false)
			w.Spec.Active = false
			_ = e.Store.UpdateWorkload(w)
			continue
		}
		e.evict(w, "PodsReadyTimeout", "pods did not become ready; requeueing", "WaitForStart", true, 2, false)
	}
}

// ReportReclaimable records that a pod set no longer needs part of its
// reservation. It releases quota in the cache immediately. PodSet.Spec.Count
// stays immutable; accounting uses Count - ReclaimablePods, matching Kueue's
// reclaimablePods contract in spirit. Reclaiming is one-way.
func (e *Engine) ReportReclaimable(namespace, name, podSet string, count int32) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if count <= 0 {
		return fmt.Errorf("reclaimable count must be positive")
	}
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	if w.Status.State == api.StateFinished {
		return fmt.Errorf("workload %s is already finished", w.Key())
	}
	var ps *api.PodSet
	for i := range w.Spec.PodSets {
		if w.Spec.PodSets[i].Name == podSet {
			ps = &w.Spec.PodSets[i]
			break
		}
	}
	if ps == nil {
		return fmt.Errorf("workload %s has no pod set %q", w.Key(), podSet)
	}
	already := w.Status.ReclaimablePods[podSet]
	if int64(already)+int64(count) > int64(ps.Count) {
		return fmt.Errorf("cannot reclaim %d pods from %s/%s with %d remaining",
			count, w.Key(), podSet, int64(ps.Count)-int64(already))
	}
	if w.Status.Admission == nil || !e.Cache.Has(w.Key()) {
		// Pending workloads keep the report so a future admission accounts for
		// it; there is no cache entry to shrink.
		if w.Status.ReclaimablePods == nil {
			w.Status.ReclaimablePods = map[string]int32{}
		}
		w.Status.ReclaimablePods[podSet] = already + count
		if err := e.Store.UpdateWorkload(w); err != nil {
			return err
		}
		e.Metrics.IncReclaimed(int64(count))
		e.event("Reclaimed", w.Key(), "reclaimed %d pods from %s", count, podSet)
		return nil
	}

	e.Cache.Remove(w.Key())
	oldAdmission := w.Status.Admission.Clone()
	if w.Status.ReclaimablePods == nil {
		w.Status.ReclaimablePods = map[string]int32{}
	}
	w.Status.ReclaimablePods[podSet] = already + count
	if err := applyReclaim(w, e.Store, podSet, count); err != nil {
		// Put the old reservation and reclaim counter back so the cache,
		// admission usage and status stay consistent.
		w.Status.Admission = oldAdmission
		if already == 0 {
			delete(w.Status.ReclaimablePods, podSet)
		} else {
			w.Status.ReclaimablePods[podSet] = already
		}
		_ = e.Cache.Add(w)
		return err
	}
	if err := e.Cache.Add(w); err != nil {
		return err
	}
	if err := e.Store.UpdateWorkload(w); err != nil {
		return err
	}
	e.Metrics.IncReclaimed(int64(count))
	e.event("Reclaimed", w.Key(), "reclaimed %d pods from %s; usage is now %s", count, podSet, w.TotalUsage().String())
	return nil
}

// applyReclaim subtracts the reclaimed per-pod requests from the flavor
// assignment that covers each resource. A PodSet may span several resource
// groups; each resource belongs to exactly one group by validation.
func applyReclaim(w *api.Workload, st *store.Store, podSet string, count int32) error {
	cqName := w.Status.Admission.ClusterQueue
	cq, ok := st.GetClusterQueue(cqName)
	if !ok {
		return fmt.Errorf("admission references missing cluster queue %q", cqName)
	}
	var ps *api.PodSet
	for i := range w.Spec.PodSets {
		if w.Spec.PodSets[i].Name == podSet {
			ps = &w.Spec.PodSets[i]
			break
		}
	}
	if ps == nil {
		return fmt.Errorf("workload %s has no pod set %q", w.Key(), podSet)
	}
	// The automatic "pods" resource is not part of PodSetPerPodUsage, so it
	// must be released explicitly when the ClusterQueue covers it. Otherwise
	// quota is freed for CPU/GPU but the pod-count ledger stays inflated.
	if count > 0 && clusterQueueCoversResource(cq, api.Pods) {
		for i := range w.Status.Admission.FlavorAssignments {
			fa := &w.Status.Admission.FlavorAssignments[i]
			if fa.GroupIndex < 0 || fa.GroupIndex >= len(cq.Spec.ResourceGroups) {
				continue
			}
			if !resourceGroupCovers(cq.Spec.ResourceGroups[fa.GroupIndex], api.Pods) {
				continue
			}
			if fa.Resources == nil {
				fa.Resources = api.ResourceList{}
			}
			fa.Resources[api.Pods] -= int64(count)
			if fa.Resources[api.Pods] < 0 {
				fa.Resources[api.Pods] = 0
			}
			break
		}
	}
	// Use the effective per-pod usage so DRA device resources are released too,
	// not only the extended resources listed in PodSet.Requests.
	for resource, per := range w.PodSetPerPodUsage(*ps) {
		delta := per * int64(count)
		groupIndex := -1
		for gi, group := range cq.Spec.ResourceGroups {
			for _, r := range group.CoveredResources {
				if r == resource {
					groupIndex = gi
					break
				}
			}
			if groupIndex >= 0 {
				break
			}
		}
		if groupIndex < 0 {
			continue
		}
		for i := range w.Status.Admission.FlavorAssignments {
			fa := &w.Status.Admission.FlavorAssignments[i]
			if fa.GroupIndex != groupIndex {
				continue
			}
			if fa.Resources == nil {
				fa.Resources = api.ResourceList{}
			}
			fa.Resources[resource] -= delta
			if fa.Resources[resource] < 0 {
				fa.Resources[resource] = 0
			}
			break
		}
	}
	releaseTopologyPods(w, ps, count)
	w.Status.Admission.Usage = w.TotalUsage()
	return nil
}

func clusterQueueCoversResource(cq *api.ClusterQueue, resource api.ResourceName) bool {
	for _, group := range cq.Spec.ResourceGroups {
		if resourceGroupCovers(group, resource) {
			return true
		}
	}
	return false
}

func resourceGroupCovers(group api.ResourceGroup, resource api.ResourceName) bool {
	for _, r := range group.CoveredResources {
		if r == resource {
			return true
		}
	}
	return false
}

// releaseTopologyPods shrinks TAS assignments when a PodSet reports
// reclaimable pods. Quota and topology are two separate ledgers; releasing only
// FlavorAssignments would leave the domain permanently charged and could block
// later TAS workloads. Assignments are reduced from the tail so a sliced
// PodSet keeps its earlier slices intact.
func releaseTopologyPods(w *api.Workload, ps *api.PodSet, count int32) {
	if count <= 0 || len(w.Status.Admission.TopologyAssignments) == 0 {
		return
	}
	perPod := w.PodSetPerPodUsage(*ps)
	remaining := count
	assignments := w.Status.Admission.TopologyAssignments
	for i := len(assignments) - 1; i >= 0 && remaining > 0; i-- {
		ta := &assignments[i]
		if ta.PodSet != ps.Name || ta.PodCount <= 0 {
			continue
		}
		reduce := ta.PodCount
		if reduce > remaining {
			reduce = remaining
		}
		for resource, per := range perPod {
			if ta.Resources == nil {
				ta.Resources = api.ResourceList{}
			}
			ta.Resources[resource] -= per * int64(reduce)
			if ta.Resources[resource] < 0 {
				ta.Resources[resource] = 0
			}
		}
		ta.PodCount -= reduce
		remaining -= reduce
		if ta.PodCount <= 0 {
			assignments = append(assignments[:i], assignments[i+1:]...)
		}
	}
	w.Status.Admission.TopologyAssignments = assignments
}

// MetricsSnapshot returns counters.
func (e *Engine) MetricsSnapshot() metrics.Snapshot {
	return e.Metrics.Snapshot()
}

// Prometheus returns the text exposition.
func (e *Engine) Prometheus() string {
	return e.Metrics.Prometheus()
}

// Tick runs one scheduling cycle.
func (e *Engine) Tick() Result {
	e.mu.Lock()
	defer e.mu.Unlock()

	e.clock++
	e.events = nil
	e.Metrics.IncCycle()
	e.decayAFS()
	e.recomputeEffectiveQuotas()
	e.rebuildScheduler()
	e.ensureCache()
	e.applyStopPolicies()
	e.reapDeactivated()
	e.expireExecution()
	e.checkPodsReady()
	e.migrateConcurrentAdmissions()
	e.admitReadyReservations()
	e.schedule()
	e.admitReadyReservations()
	e.updateGauges()

	return Result{Clock: e.clock, Events: append([]Event(nil), e.events...)}
}

func (e *Engine) ensureCache() {
	for _, w := range e.Store.ListWorkloads() {
		if w.Status.Admission == nil {
			continue
		}
		if w.Status.State != api.StateQuotaReserved && w.Status.State != api.StateAdmitted {
			continue
		}
		if !e.Cache.Has(w.Key()) {
			_ = e.Cache.Add(w)
		}
	}
}

// applyStopPolicies applies the most restrictive policy from the ClusterQueue
// and the LocalQueue that admitted the workload. Hold cancels reservations but
// lets admitted workloads finish; HoldAndDrain evicts both states.
func (e *Engine) applyStopPolicies() {
	for _, w := range e.Store.ListWorkloads() {
		if w.Status.Admission == nil {
			continue
		}
		cq, ok := e.Store.GetClusterQueue(w.Status.Admission.ClusterQueue)
		if !ok {
			continue
		}
		policy := cq.Spec.StopPolicy
		if lq, ok := e.Store.GetLocalQueue(w.Namespace, w.Spec.QueueName); ok {
			switch lq.StopPolicy {
			case api.StopHoldAndDrain:
				policy = api.StopHoldAndDrain
			case api.StopHold:
				if policy == "" || policy == api.StopNone {
					policy = api.StopHold
				}
			}
		}
		switch policy {
		case api.StopHold:
			if w.Status.State == api.StateQuotaReserved {
				e.releaseReservation(w, "Stopped", "queue is on hold", 0)
			}
		case api.StopHoldAndDrain:
			if w.Status.State == api.StateQuotaReserved || w.Status.State == api.StateAdmitted {
				e.evict(w, "Stopped", "queue is draining", "", true, 0, false)
			}
		}
	}
}

func (e *Engine) reapDeactivated() {
	for _, w := range e.Store.ListWorkloads() {
		if w.Spec.Active || !e.Cache.Has(w.Key()) {
			continue
		}
		e.evict(w, "Deactivated", "workload is inactive", "", false, 0, false)
	}
}

func (e *Engine) expireExecution() {
	for _, w := range e.Store.ListWorkloads() {
		if w.Status.State != api.StateAdmitted || w.Spec.MaximumExecutionSeconds <= 0 || w.Status.AdmittedAt <= 0 {
			continue
		}
		// The limit is cumulative across Admit/Evict cycles. AdmittedAt only
		// marks the current period; ExecutionTicks holds the earlier periods.
		executed := w.Status.ExecutionTicks + (e.clock - w.Status.AdmittedAt)
		if executed >= w.Spec.MaximumExecutionSeconds {
			e.evict(w, "MaximumExecutionTime", "maximum execution time exceeded", "", false, 0, false)
			w.Spec.Active = false
			_ = e.Store.UpdateWorkload(w)
		}
	}
}

func (e *Engine) schedule() {
	workloads := e.Store.ListWorkloads()
	lqToCQ := e.Store.LocalQueueToClusterQueue()
	classes := e.Store.PriorityClasses()

	cqs := e.Store.ListClusterQueues()
	names := make([]string, 0, len(cqs))
	byName := map[string]*api.ClusterQueue{}
	for _, cq := range cqs {
		names = append(names, cq.Name)
		byName[cq.Name] = cq
	}
	// Lower weighted share first. This is the simple fair-share ordering used
	// by the mini scheduler; the real Kueue queue manager has a richer
	// hierarchy-aware implementation.
	names = queue.FairShareOrder(names, e.Scheduler.FairShare)

	for _, name := range names {
		cq := byName[name]
		if cq.Spec.StopPolicy != api.StopNone {
			continue
		}
		pending := queue.FilterByClusterQueue(workloads, cq.Name, lqToCQ, classes, e.clock)
		if e.afsEnabled(cq) {
			pending = queue.SortByScore(pending, classes, func(w *api.Workload) float64 {
				return e.afsScore(w.Namespace, w.Spec.QueueName)
			})
		}
		for _, w := range pending {
			if lq, ok := e.Store.GetLocalQueue(w.Namespace, w.Spec.QueueName); ok &&
				lq.StopPolicy != "" && lq.StopPolicy != api.StopNone {
				// A held LocalQueue only blocks its own workloads; other
				// LocalQueues sharing the ClusterQueue keep moving.
				continue
			}
			if w.HasAdmissionGates() {
				w.Status.SetCondition("AdmissionGated", "True", "AdmissionGated",
					fmt.Sprintf("waiting for gates: %v", w.Spec.AdmissionGates), e.clock)
				w.Status.SetCondition("QuotaReserved", "False", "AdmissionGated",
					"workload is blocked by an admission gate", e.clock)
				_ = e.Store.UpdateWorkload(w)
				if cq.Spec.QueueingStrategy == api.StrictFIFO {
					break
				}
				continue
			}
			if !e.namespaceMatches(cq, w.Namespace) {
				// Inadmissible is a structural condition, not a queue head that
				// can become admissible when quota frees up. Like Kueue's
				// inadmissible backlog, it must not block StrictFIFO.
				e.setInadmissible(w, "NamespaceSelector", "namespace does not match the ClusterQueue selector")
				continue
			}
			decision := e.Scheduler.TryAdmit(w, cq)
			if decision.Admission == nil {
				e.setPendingReason(w, decision.Reason, decision.Detail)
				if cq.Spec.QueueingStrategy == api.StrictFIFO {
					break
				}
				continue
			}
			for _, victim := range decision.Victims {
				e.evict(victim, "Preempted", fmt.Sprintf("preempted to accommodate %s", w.Key()), "", true, 2, true)
			}
			e.reserve(w, cq, decision.Admission)
		}
	}
}

func (e *Engine) namespaceMatches(cq *api.ClusterQueue, namespace string) bool {
	// This mirrors Kueue: null means no namespace is eligible, while an empty
	// non-nil selector matches every namespace. LabelSelector.Matches also
	// evaluates set-based requirements (In, NotIn, Exists, DoesNotExist).
	return cq.Spec.NamespaceSelector.Matches(e.Store.NamespaceLabels(namespace))
}

func (e *Engine) reserve(w *api.Workload, cq *api.ClusterQueue, admission *api.Admission) {
	e.reserveWith(w, cq, admission, true)
}

func (e *Engine) reserveWith(w *api.Workload, cq *api.ClusterQueue, admission *api.Admission, countAFS bool) {
	// A Retry releases the reservation but must not lose the retry bookkeeping:
	// retryCount is a property of the Workload, not of one reservation. Carry it
	// across re-reservations so operators can distinguish a first retry from a
	// check that has failed many times.
	previousRetries := map[string]int32{}
	for _, cs := range w.Status.AdmissionChecks {
		if cs.RetryCount > 0 {
			previousRetries[cs.Name] = cs.RetryCount
		}
	}
	w.Status.State = api.StateQuotaReserved
	w.Status.Admission = admission
	w.Status.AdmissionChecks = nil
	for _, name := range cq.Spec.AdmissionChecks {
		w.Status.AdmissionChecks = append(w.Status.AdmissionChecks, api.AdmissionCheckState{
			Name:               name,
			State:              "Pending",
			Message:            "waiting for admission check",
			RetryCount:         previousRetries[name],
			LastTransitionTick: e.clock,
		})
	}
	w.Status.RequeueAt = 0
	w.Status.PodsReady = false
	w.Status.PodsReadyAt = 0
	if e.WaitForPodsReady != nil {
		w.Status.SetCondition("PodsReady", "False", "WaitForStart", "waiting for pods to become ready", e.clock)
	}
	w.Status.SetCondition("QuotaReserved", "True", "Reserved", scheduler.FormatAdmission(admission), e.clock)
	w.Status.SetCondition("Admitted", "False", "AdmissionChecks", "waiting for admission checks", e.clock)
	w.Status.ClearCondition("Evicted")
	w.Status.ClearCondition("Preempted")
	w.Status.ClearCondition("Inadmissible")
	if err := e.Cache.Add(w); err != nil {
		// The cache is the source of truth for quota. If it rejects the
		// reservation, leave the workload pending rather than corrupting
		// accounting.
		w.Status.State = api.StatePending
		w.Status.Admission = nil
		w.Status.AdmissionChecks = nil
		w.Status.SetCondition("QuotaReserved", "False", "CacheError", err.Error(), e.clock)
		_ = e.Store.UpdateWorkload(w)
		e.event("Error", w.Key(), "cache rejected reservation: %v", err)
		return
	}
	_ = e.Store.UpdateWorkload(w)
	e.Metrics.IncQuotaReserved()
	if countAFS {
		e.recordAFSUsage(w, admission.Usage)
	}
	e.event("QuotaReserved", w.Key(), "reserved %s on %s", admission.Usage.String(), admission.ClusterQueue)
	e.admitIfReady(w)
}

func (e *Engine) admitIfReady(w *api.Workload) {
	if w.Status.State != api.StateQuotaReserved {
		return
	}
	if len(w.Status.AdmissionChecks) > 0 {
		for _, cs := range w.Status.AdmissionChecks {
			if cs.State != "Ready" {
				return
			}
		}
	}
	w.Status.State = api.StateAdmitted
	w.Status.AdmittedAt = e.clock
	if e.WaitForPodsReady == nil {
		// Without the feature gate the simulator treats pods as ready as soon
		// as the workload is admitted, mirroring Kueue's default behaviour.
		w.Status.PodsReady = true
		w.Status.PodsReadyAt = e.clock
	} else {
		w.Status.SetCondition("PodsReady", "False", "WaitForStart", "waiting for pods to become ready", e.clock)
	}
	w.Status.SetCondition("Admitted", "True", "Admitted", "workload admitted", e.clock)
	_ = e.Store.UpdateWorkload(w)
	// Keep the quota ledger's snapshot in sync with the admitted status.
	if err := e.Cache.Refresh(w); err != nil {
		e.event("Warning", w.Key(), "cache refresh after admission failed: %v", err)
	}
	e.Metrics.IncAdmitted()
	e.event("Admitted", w.Key(), "workload admitted")
}

func (e *Engine) admitReadyReservations() {
	for _, w := range e.Store.ListWorkloads() {
		if w.Status.State != api.StateQuotaReserved {
			continue
		}
		if e.evaluateChecks(w) {
			e.admitIfReady(w)
		}
	}
}

// evaluateChecks returns true when all checks are Ready. It handles Retry and
// Rejected according to Kueue's admission-check contract.
func (e *Engine) evaluateChecks(w *api.Workload) bool {
	if len(w.Status.AdmissionChecks) == 0 {
		return true
	}
	for i := range w.Status.AdmissionChecks {
		cs := &w.Status.AdmissionChecks[i]
		switch cs.State {
		case "Ready":
			continue
		case "Rejected":
			e.evict(w, "AdmissionCheck", fmt.Sprintf("admission check %s rejected", cs.Name), "", false, 0, false)
			w.Spec.Active = false
			_ = e.Store.UpdateWorkload(w)
			return false
		case "Retry":
			var requeueAt int64
			if cs.RequeueAfterSeconds > 0 {
				base := cs.LastTransitionTick
				if base == 0 {
					base = e.clock
				}
				requeueAt = base + int64(cs.RequeueAfterSeconds)
			}
			e.releaseReservation(w, "AdmissionCheckRetry",
				fmt.Sprintf("admission check %s requested retry", cs.Name), requeueAt)
			return false
		default:
			// ProvisioningRequest-lite: hold the reservation, fail the first
			// Failures attempts, then become Ready after DelayTicks from the
			// current reservation. The retry counter and requeue delay are the
			// same fields an external provisioning controller would use.
			if e.Provisioning != nil && cs.Name == e.Provisioning.CheckName {
				delay := e.Provisioning.DelayTicks
				if delay < 0 {
					delay = 0
				}
				if cs.RetryCount < e.Provisioning.Failures {
					attempt := cs.RetryCount + 1
					w.Status.SetAdmissionCheckRetry(cs.Name, "provisioning attempt failed", int32(delay))
					e.event("ProvisioningRetry", w.Key(), "provisioning attempt %d failed", attempt)
					e.releaseReservation(w, "ProvisioningFailed",
						fmt.Sprintf("provisioning attempt %d failed", attempt), e.clock+delay)
					return false
				}
				started := cs.LastTransitionTick
				if started == 0 {
					started = e.clock
					cs.LastTransitionTick = started
				}
				if e.clock-started < delay {
					e.Metrics.IncAdmissionCheckWait()
					_ = e.Store.UpdateWorkload(w)
					return false
				}
				cs.State = "Ready"
				cs.Message = "provisioned capacity is ready"
				cs.LastTransitionTick = e.clock
				e.event("ProvisioningReady", w.Key(), "provisioned capacity is ready")
				_ = e.Store.UpdateWorkload(w)
				continue
			}
			// Auto-approve mode, or a check whose controller is literally
			// named "auto", passes immediately.
			check, _ := e.Store.GetAdmissionCheck(cs.Name)
			if e.AutoApproveChecks || (check != nil && check.ControllerName == "auto") {
				w.Status.SetAdmissionCheck(cs.Name, "Ready", "auto-approved")
				_ = e.Store.UpdateWorkload(w)
				continue
			}
			e.Metrics.IncAdmissionCheckWait()
			return false
		}
	}
	return true
}

func (e *Engine) releaseReservation(w *api.Workload, reason, message string, requeueAt int64) {
	e.accumulateExecution(w)
	e.Cache.Remove(w.Key())
	w.Status.State = api.StatePending
	w.Status.Admission = nil
	// Keep the check list (and especially retryCount) so a Retry followed by a
	// re-reservation reports cumulative attempts instead of resetting to 1.
	for i := range w.Status.AdmissionChecks {
		w.Status.AdmissionChecks[i].State = "Pending"
		w.Status.AdmissionChecks[i].Message = message
		w.Status.AdmissionChecks[i].LastTransitionTick = e.clock
	}
	w.Status.SetCondition("QuotaReserved", "False", reason, message, e.clock)
	w.Status.SetCondition("Admitted", "False", reason, message, e.clock)
	if requeueAt == 0 && e.Backoff != nil && reason == "AdmissionCheckRetry" {
		// An external check asked for an immediate retry, but the operator
		// configured exponential backoff. Use the same attempt counter as
		// eviction requeues so retry storms decay instead of hot-looping.
		requeueAt = e.clock + e.requeueDelay(w.Status.Requeues)
		w.Status.Requeues++
	}
	w.Status.RequeueAt = requeueAt
	_ = e.Store.UpdateWorkload(w)
	e.event("ReservationReleased", w.Key(), "%s", message)
}

// requeueDelay returns the exponential delay for an attempt. A zero or
// negative base is treated as 1 tick; a zero multiplier is treated as 2.
// Negative results are impossible, and MaxTicks caps the growth.
func (e *Engine) requeueDelay(attempt int32) int64 {
	if e.Backoff == nil {
		return 2
	}
	base := e.Backoff.BaseTicks
	if base <= 0 {
		base = 1
	}
	multiplier := e.Backoff.Multiplier
	if multiplier <= 0 {
		multiplier = 2
	}
	delay := float64(base) * math.Pow(multiplier, float64(attempt))
	if e.Backoff.MaxTicks > 0 && delay > float64(e.Backoff.MaxTicks) {
		return e.Backoff.MaxTicks
	}
	if delay < 1 {
		return 1
	}
	return int64(delay)
}

// accumulateExecution folds the current admitted period into the cumulative
// execution counter. It is called whenever a workload stops being admitted
// (eviction, finish, migration), so a later admission continues the clock
// instead of restarting maximumExecutionTimeSeconds.
func (e *Engine) accumulateExecution(w *api.Workload) {
	if w.Status.State == api.StateAdmitted && w.Status.AdmittedAt > 0 {
		w.Status.ExecutionTicks += e.clock - w.Status.AdmittedAt
	}
	w.Status.AdmittedAt = 0
}

func (e *Engine) evict(w *api.Workload, reason, message, underlyingCause string, requeue bool, delay int64, preempted bool) {
	// Preemption victims come from a cache snapshot. Re-read the current object
	// so eviction does not overwrite newer status (admission time, execution
	// accounting, conditions) with that snapshot.
	if latest, ok := e.Store.GetWorkload(w.Namespace, w.Name); ok {
		// Copy the current state into the caller's object rather than
		// rebinding the local: callers such as evaluateChecks and
		// checkPodsReady mutate w after evict and then write it back.
		*w = *latest
	}
	e.accumulateExecution(w)
	e.Cache.Remove(w.Key())
	w.Status.State = api.StatePending
	w.Status.Admission = nil
	// Keep the check list (and retryCount) across evictions, just like
	// releaseReservation does. reserveWith rebuilds the list from the
	// ClusterQueue and carries retryCount forward by name; clearing it here
	// would make a preemption/drain/timeout silently reset the retry history.
	for i := range w.Status.AdmissionChecks {
		w.Status.AdmissionChecks[i].State = "Pending"
		w.Status.AdmissionChecks[i].Message = "waiting for admission check"
		w.Status.AdmissionChecks[i].LastTransitionTick = e.clock
	}
	w.Status.SetCondition("Evicted", "True", reason, message, e.clock)
	w.Status.SetCondition("QuotaReserved", "False", reason, message, e.clock)
	w.Status.SetCondition("Admitted", "False", reason, message, e.clock)
	if preempted {
		w.Status.SetCondition("Preempted", "True", "Preempted", message, e.clock)
	}
	w.Status.Evictions++
	recordEviction(&w.Status, reason, underlyingCause)
	if requeue {
		if e.Backoff != nil {
			delay = e.requeueDelay(w.Status.Requeues)
		}
		w.Status.RequeueAt = e.clock + delay
		w.Status.Requeues++
	} else {
		w.Status.RequeueAt = 0
		w.Spec.Active = false
	}
	_ = e.Store.UpdateWorkload(w)
	e.Metrics.IncEvicted(preempted)
	e.event("Evicted", w.Key(), "%s", message)
}

// recordEviction updates the per-workload schedulingStats entry for a
// (reason, underlyingCause) pair.
// evictionCount returns how many times a workload was evicted for a reason.
// The scheduler stats are the durable record; counters such as Requeues are
// shared across eviction causes and must not be used for per-cause limits.
func evictionCount(status *api.WorkloadStatus, reason string) int32 {
	var count int32
	for _, stat := range status.EvictionStats {
		if stat.Reason == reason {
			count += stat.Count
		}
	}
	return count
}

func recordEviction(status *api.WorkloadStatus, reason, underlyingCause string) {
	for i := range status.EvictionStats {
		if status.EvictionStats[i].Reason == reason && status.EvictionStats[i].UnderlyingCause == underlyingCause {
			status.EvictionStats[i].Count++
			return
		}
	}
	status.EvictionStats = append(status.EvictionStats, api.EvictionStat{
		Reason:          reason,
		UnderlyingCause: underlyingCause,
		Count:           1,
	})
}

func (e *Engine) setPendingReason(w *api.Workload, reason, detail string) {
	if reason == "" {
		reason = "InsufficientQuota"
	}
	if detail == "" {
		detail = reason
	}
	// The workload reached the quota check, so a previous structural
	// inadmissibility (for example a namespaceSelector mismatch that has since
	// been fixed) no longer applies. Leaving it behind makes the visibility
	// reason misleading.
	w.Status.ClearCondition("Inadmissible")
	w.Status.SetCondition("QuotaReserved", "False", reason, detail, e.clock)
	_ = e.Store.UpdateWorkload(w)
}

func (e *Engine) setInadmissible(w *api.Workload, reason, message string) {
	w.Status.SetCondition("Inadmissible", "True", reason, message, e.clock)
	w.Status.SetCondition("QuotaReserved", "False", "Inadmissible", message, e.clock)
	_ = e.Store.UpdateWorkload(w)
}

func (e *Engine) updateGauges() {
	var pending, reserved, admitted, finished int64
	for _, w := range e.Store.ListWorkloads() {
		switch w.Status.State {
		case api.StatePending:
			pending++
		case api.StateQuotaReserved:
			reserved++
		case api.StateAdmitted:
			admitted++
		case api.StateFinished:
			finished++
		}
	}
	e.Metrics.SetGauges(pending, reserved, admitted, finished)
}

func localQueueMap(st *store.Store) map[string]*api.LocalQueue {
	out := map[string]*api.LocalQueue{}
	for _, q := range st.ListLocalQueues() {
		out[store.LocalQueueKey(q.Namespace, q.Name)] = q
	}
	return out
}

func clusterQueueMap(st *store.Store) map[string]*api.ClusterQueue {
	out := map[string]*api.ClusterQueue{}
	for _, cq := range st.ListClusterQueues() {
		out[cq.Name] = cq
	}
	return out
}

// DeleteWorkload removes a workload and releases any quota it holds. It is the
// simulator's analogue of deleting a Workload object (for example, a losing
// MultiKueue copy).
func (e *Engine) DeleteWorkload(namespace, name string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	key := namespace + "/" + name
	if _, ok := e.Store.GetWorkload(namespace, name); !ok {
		return fmt.Errorf("workload %s not found", key)
	}
	e.Cache.Remove(key)
	e.Store.DeleteWorkload(namespace, name)
	e.event("Deleted", key, "workload deleted")
	return nil
}

// ClearAdmissionGate removes one external pre-admission gate. The workload can
// participate in scheduling again once no gates remain.
func (e *Engine) ClearAdmissionGate(namespace, name, gate string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	if !w.ClearAdmissionGate(gate) {
		return fmt.Errorf("workload %s has no admission gate %q", w.Key(), gate)
	}
	if !w.HasAdmissionGates() {
		w.Status.ClearCondition("AdmissionGated")
		w.Status.SetCondition("QuotaReserved", "False", "Pending", "waiting for quota", e.clock)
	}
	if err := e.Store.UpdateWorkload(w); err != nil {
		return err
	}
	e.event("AdmissionGateCleared", w.Key(), "gate %s cleared; remaining=%v", gate, w.Spec.AdmissionGates)
	return nil
}

func (e *Engine) afsEnabled(cq *api.ClusterQueue) bool {
	return e.AFS != nil && cq.Spec.AdmissionScope == api.AdmissionScopeUsageBasedAdmissionSharing
}

func (e *Engine) lqWeight(namespace, name string) float64 {
	if lq, ok := e.Store.GetLocalQueue(namespace, name); ok && lq.FairSharing != nil && lq.FairSharing.Weight > 0 {
		return lq.FairSharing.Weight
	}
	return 1
}

func (e *Engine) afsScore(namespace, name string) float64 {
	key := store.LocalQueueKey(namespace, name)
	return e.afsUsage[key] / e.lqWeight(namespace, name)
}

// AFSScore returns the current weighted historical usage of a LocalQueue. It
// is exposed for the CLI visibility query and tests.
func (e *Engine) AFSScore(namespace, name string) float64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.afsScore(namespace, name)
}

func (e *Engine) recordAFSSubmit(w *api.Workload) {
	if e.AFS == nil || e.AFS.EntryPenalty <= 0 {
		return
	}
	lq, ok := e.Store.GetLocalQueue(w.Namespace, w.Spec.QueueName)
	if !ok {
		return
	}
	cq, ok := e.Store.GetClusterQueue(lq.ClusterQueue)
	if !ok || !e.afsEnabled(cq) {
		return
	}
	key := store.LocalQueueKey(w.Namespace, w.Spec.QueueName)
	e.afsUsage[key] += e.AFS.EntryPenalty
}

func (e *Engine) recordAFSUsage(w *api.Workload, usage api.ResourceList) {
	if e.AFS == nil || w.Status.Admission == nil {
		return
	}
	cq, ok := e.Store.GetClusterQueue(w.Status.Admission.ClusterQueue)
	if !ok || !e.afsEnabled(cq) {
		return
	}
	key := store.LocalQueueKey(w.Namespace, w.Spec.QueueName)
	e.afsUsage[key] += usage.WeightedSum(e.AFS.ResourceWeights)
}

// decayAFS applies one tick of exponential decay. Half-life is measured in
// ticks; a zero or negative half-life keeps history forever.
func (e *Engine) decayAFS() {
	if e.AFS == nil || e.AFS.HalfLifeTicks <= 0 {
		return
	}
	factor := math.Pow(0.5, 1/e.AFS.HalfLifeTicks)
	for key, value := range e.afsUsage {
		e.afsUsage[key] = value * factor
	}
}

// migrateConcurrentAdmissions implements the observable part of concurrent
// admission: a workload admitted on a later flavor moves to an earlier
// (preferred) flavor once that flavor becomes feasible. It never migrates to a
// worse flavor, so the process terminates.
func (e *Engine) migrateConcurrentAdmissions() {
	for _, w := range e.Store.ListWorkloads() {
		if !w.Spec.Active || w.Status.Admission == nil {
			continue
		}
		if w.Status.State != api.StateAdmitted && w.Status.State != api.StateQuotaReserved {
			continue
		}
		cq, ok := e.Store.GetClusterQueue(w.Status.Admission.ClusterQueue)
		if !ok || cq.Spec.ConcurrentAdmission == nil {
			continue
		}
		// Migration is a new reservation. A queue that is on hold must not
		// create one, exactly like the normal scheduling path.
		if cq.Spec.StopPolicy != "" && cq.Spec.StopPolicy != api.StopNone {
			continue
		}
		if lq, ok := e.Store.GetLocalQueue(w.Namespace, w.Spec.QueueName); ok &&
			lq.StopPolicy != "" && lq.StopPolicy != api.StopNone {
			continue
		}
		current := admissionFlavorIndex(cq, w.Status.Admission)
		if current <= 0 {
			continue
		}
		excluded := map[string]bool{w.Key(): true}
		admission, ok := e.Scheduler.DirectAdmission(w, cq, excluded)
		if !ok {
			continue
		}
		next := admissionFlavorIndex(cq, admission)
		if next < 0 || next >= current || !migrationAllowed(cq, next) {
			continue
		}
		// Migration ends the current admitted period and starts a new one. The
		// period is folded into ExecutionTicks, so maximumExecutionTimeSeconds
		// remains cumulative across the move. An already-admitted workload has
		// already passed its admission checks and its pods are already running;
		// re-reserving must not send it back to Pending/QuotaReserved or reset
		// PodsReady, otherwise it would be un-admitted or spuriously evicted by
		// WaitForPodsReady.
		wasAdmitted := w.Status.State == api.StateAdmitted
		podsReady := w.Status.PodsReady
		podsReadyAt := w.Status.PodsReadyAt
		e.accumulateExecution(w)
		e.Cache.Remove(w.Key())
		w.Status.State = api.StatePending
		w.Status.Admission = nil
		w.Status.AdmissionChecks = nil
		w.Status.SetCondition("QuotaReserved", "False", "Migrating",
			fmt.Sprintf("migrating from %s to %s", flavorName(cq, current), flavorName(cq, next)), e.clock)
		e.reserveWith(w, cq, admission, false)
		if wasAdmitted {
			for i := range w.Status.AdmissionChecks {
				w.Status.AdmissionChecks[i].State = "Ready"
				w.Status.AdmissionChecks[i].Message = "preserved across flavor migration"
			}
			e.admitIfReady(w)
			if podsReady {
				w.Status.PodsReady = true
				w.Status.PodsReadyAt = podsReadyAt
				w.Status.SetCondition("PodsReady", "True", "PodsReady", "all pods are ready", podsReadyAt)
				_ = e.Store.UpdateWorkload(w)
				_ = e.Cache.Refresh(w)
			}
		}
		e.event("Migrated", w.Key(), "migrated from flavor %s to %s",
			flavorName(cq, current), flavorName(cq, next))
	}
}

func admissionFlavorIndex(cq *api.ClusterQueue, admission *api.Admission) int {
	if len(cq.Spec.ResourceGroups) != 1 || len(admission.FlavorAssignments) != 1 {
		return -1
	}
	flavor := admission.FlavorAssignments[0].Flavor
	for i, fq := range cq.Spec.ResourceGroups[0].Flavors {
		if fq.Name == flavor {
			return i
		}
	}
	return -1
}

func flavorName(cq *api.ClusterQueue, index int) string {
	if index < 0 || len(cq.Spec.ResourceGroups) != 1 || index >= len(cq.Spec.ResourceGroups[0].Flavors) {
		return ""
	}
	return cq.Spec.ResourceGroups[0].Flavors[index].Name
}

func migrationAllowed(cq *api.ClusterQueue, next int) bool {
	last := cq.Spec.ConcurrentAdmission.LastAcceptableFlavorName
	if last == "" {
		return true
	}
	for i, fq := range cq.Spec.ResourceGroups[0].Flavors {
		if fq.Name == last {
			return next <= i
		}
	}
	return true
}

// QueueStatus is a lightweight visibility API for one ClusterQueue. It is the
// simulator's analogue of Kueue's Visibility API: a read-only view of pending,
// reserving and admitted workloads plus quota usage.
type QueueStatus struct {
	ClusterQueue    string
	Pending         []string
	QuotaReserved   []string
	Admitted        []string
	Finished        []string
	Usage           api.ResourceList
	Nominal         api.ResourceList
	PendingByReason map[string]int
}

// QueueStatus returns a stable snapshot for cqName. The second result is false
// when the ClusterQueue does not exist.
func (e *Engine) QueueStatus(cqName string) (QueueStatus, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	cq, ok := e.Store.GetClusterQueue(cqName)
	if !ok {
		return QueueStatus{}, false
	}
	out := QueueStatus{
		ClusterQueue:    cqName,
		Usage:           e.Cache.CQUsage(cqName),
		Nominal:         e.nominalFor(cq),
		PendingByReason: map[string]int{},
	}
	for _, w := range e.Store.ListWorkloads() {
		admittedTo := ""
		if w.Status.Admission != nil {
			admittedTo = w.Status.Admission.ClusterQueue
		}
		lq, _ := e.Store.GetLocalQueue(w.Namespace, w.Spec.QueueName)
		targets := lq != nil && lq.ClusterQueue == cqName
		if !targets && admittedTo != cqName {
			continue
		}
		key := w.Key()
		switch w.Status.State {
		case api.StatePending:
			out.Pending = append(out.Pending, key)
			reason := "InsufficientQuota"
			if c, ok := w.Status.Condition("QuotaReserved"); ok && c.Reason != "" {
				reason = c.Reason
			}
			if c, ok := w.Status.Condition("Inadmissible"); ok && c.Status == "True" {
				reason = c.Reason
			}
			out.PendingByReason[reason]++
		case api.StateQuotaReserved:
			out.QuotaReserved = append(out.QuotaReserved, key)
		case api.StateAdmitted:
			out.Admitted = append(out.Admitted, key)
		case api.StateFinished:
			out.Finished = append(out.Finished, key)
		}
	}
	sort.Strings(out.Pending)
	sort.Strings(out.QuotaReserved)
	sort.Strings(out.Admitted)
	sort.Strings(out.Finished)
	return out, true
}

// nominalFor aggregates effective (or static) nominal quota per resource.
// Flavors are separate pools: different workloads may occupy different flavors
// at the same time, so aggregate capacity is the sum across flavors. A single
// workload can only use one flavor per resource group, but the visibility
// query compares Nominal with the summed usage, so sum is the consistent
// summary here. (Real Kueue exposes per-flavor status.)
func (e *Engine) nominalFor(cq *api.ClusterQueue) api.ResourceList {
	out := api.ResourceList{}
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			for _, r := range group.CoveredResources {
				if byFlavor, ok := e.effective[cq.Name]; ok {
					if resources, ok := byFlavor[fq.Name]; ok {
						if value, ok := resources[r]; ok {
							out[r] += value
							continue
						}
					}
				}
				out[r] += fq.Nominal.Get(r)
			}
		}
	}
	return out
}

// SortedWorkloads is a diagnostic helper used by the CLI and tests.
func SortedWorkloads(st *store.Store) []*api.Workload {
	out := st.ListWorkloads()
	sort.Slice(out, func(i, j int) bool {
		if out[i].Namespace != out[j].Namespace {
			return out[i].Namespace < out[j].Namespace
		}
		return out[i].Name < out[j].Name
	})
	return out
}
