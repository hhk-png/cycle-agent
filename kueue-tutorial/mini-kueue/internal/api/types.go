// Package api contains the Kubernetes-like API objects used by MiniKueue.
//
// The types intentionally mirror the *semantics* of Kueue's v1beta2 API, but
// they are not Kubernetes API types. The project is a deterministic simulator:
// it keeps the interesting control-plane logic (queueing, quota accounting,
// fair sharing, preemption, admission checks) and replaces the API server,
// etcd, pods and node kubelets with an in-memory model.
package api

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// ResourceName is a Kubernetes-style resource name. CPU is measured in
// millicores, memory in MiB, and all other resources in whole units.
type ResourceName string

const (
	CPU    ResourceName = "cpu"
	Memory ResourceName = "memory"
	Pods   ResourceName = "pods"
)

// ResourceList is a resource-name to quantity map. Quantities are integer
// values; see ResourceName for the units.
type ResourceList map[ResourceName]int64

// RL builds a ResourceList from alternating name/value arguments.
//
//	RL("cpu", 4000, "memory", 8192)
func RL(args ...interface{}) ResourceList {
	if len(args)%2 != 0 {
		panic("api.RL requires name/value pairs")
	}
	out := make(ResourceList, len(args)/2)
	for i := 0; i < len(args); i += 2 {
		name, ok := args[i].(string)
		if !ok {
			name = string(args[i].(ResourceName))
		}
		value, ok := args[i+1].(int64)
		if !ok {
			value = int64(args[i+1].(int))
		}
		out[ResourceName(name)] = value
	}
	return out
}

// Clone returns a deep copy.
func (r ResourceList) Clone() ResourceList {
	out := make(ResourceList, len(r))
	for k, v := range r {
		out[k] = v
	}
	return out
}

// Get returns the quantity for name.
func (r ResourceList) Get(name ResourceName) int64 {
	if r == nil {
		return 0
	}
	return r[name]
}

// Set stores a quantity.
func (r ResourceList) Set(name ResourceName, value int64) {
	r[name] = value
}

// Add adds other to r in place and returns r.
func (r ResourceList) Add(other ResourceList) ResourceList {
	for k, v := range other {
		r[k] += v
	}
	return r
}

// AddScaled adds scale*other in place and returns r.
func (r ResourceList) AddScaled(other ResourceList, scale int64) ResourceList {
	for k, v := range other {
		r[k] += scale * v
	}
	return r
}

// Sub subtracts other from r in place and returns r.
func (r ResourceList) Sub(other ResourceList) ResourceList {
	for k, v := range other {
		r[k] -= v
	}
	return r
}

// Max returns the component-wise maximum of r and other.
func (r ResourceList) Max(other ResourceList) ResourceList {
	out := r.Clone()
	for k, v := range other {
		if v > out[k] {
			out[k] = v
		}
	}
	return out
}

// IsZero reports whether every quantity is zero.
func (r ResourceList) IsZero() bool {
	for _, v := range r {
		if v != 0 {
			return false
		}
	}
	return true
}

// AnyNegative reports whether any quantity is negative.
func (r ResourceList) AnyNegative() bool {
	for _, v := range r {
		if v < 0 {
			return true
		}
	}
	return false
}

// Covers reports whether r has at least the quantities in need.
func (r ResourceList) Covers(need ResourceList) bool {
	for k, v := range need {
		if r[k] < v {
			return false
		}
	}
	return true
}

// String renders a stable, human-readable resource list.
func (r ResourceList) String() string {
	names := make([]string, 0, len(r))
	for name := range r {
		names = append(names, string(name))
	}
	sort.Strings(names)
	parts := make([]string, 0, len(names))
	for _, name := range names {
		parts = append(parts, fmt.Sprintf("%s=%s", name, FormatQuantity(ResourceName(name), r[ResourceName(name)])))
	}
	if len(parts) == 0 {
		return "{}"
	}
	return "{" + strings.Join(parts, ", ") + "}"
}

// FormatQuantity renders a quantity using the conventional unit for a resource.
func FormatQuantity(name ResourceName, value int64) string {
	switch name {
	case CPU:
		if value%1000 == 0 {
			return fmt.Sprintf("%d", value/1000)
		}
		return trimFloat(float64(value) / 1000)
	case Memory:
		if value >= 1024 && value%1024 == 0 {
			return fmt.Sprintf("%dGi", value/1024)
		}
		return fmt.Sprintf("%dMi", value)
	default:
		return fmt.Sprintf("%d", value)
	}
}

func trimFloat(v float64) string {
	s := fmt.Sprintf("%.3f", v)
	s = strings.TrimRight(s, "0")
	return strings.TrimRight(s, ".")
}

// QueueingStrategy controls whether an unadmittable head-of-line workload
// blocks later workloads in the same ClusterQueue.
type QueueingStrategy string

const (
	StrictFIFO     QueueingStrategy = "StrictFIFO"
	BestEffortFIFO QueueingStrategy = "BestEffortFIFO"
)

// PreemptionMode is the priority relationship required for a preemption.
type PreemptionMode string

const (
	PreemptNever             PreemptionMode = "Never"
	PreemptLower             PreemptionMode = "LowerPriority"
	PreemptLowerOrNewerEqual PreemptionMode = "LowerOrNewerEqualPriority"
	PreemptAny               PreemptionMode = "Any"
)

// BorrowWithinCohort mirrors ClusterQueue.spec.preemption.borrowWithinCohort.
type BorrowWithinCohort struct {
	Policy               PreemptionMode
	MaxPriorityThreshold *int32
}

// PreemptionPolicy mirrors ClusterQueue.spec.preemption.
type PreemptionPolicy struct {
	WithinClusterQueue  PreemptionMode
	ReclaimWithinCohort PreemptionMode
	// BorrowWithinCohort permits cohort preemption while the preemptor is
	// itself borrowing.
	BorrowWithinCohort *BorrowWithinCohort
}

// PreemptionTrigger mirrors PreemptionConfigActivationPolicy.trigger. The
// MiniKueue scheduler distinguishes "quota cannot fit" from "quota fits but
// the topology constraint cannot be satisfied"; the remaining triggers map to
// those two conditions.
type PreemptionTrigger string

const (
	// TriggerAlways activates the rule whenever preemption is considered.
	TriggerAlways PreemptionTrigger = "Always"
	// TriggerInsufficientQuota activates the rule when the workload cannot fit
	// its quota at all.
	TriggerInsufficientQuota PreemptionTrigger = "InsufficientQuota"
	// TriggerQuotaFeasibleAndInsufficientTopology activates the rule when the
	// workload fits its quota ledger but cannot be placed on the requested
	// topology.
	TriggerQuotaFeasibleAndInsufficientTopology PreemptionTrigger = "QuotaFeasibleAndInsufficientTopology"
)

// SelectorScope limits which admitted workloads may be preemption candidates.
type SelectorScope string

const (
	ScopeWithinLocalQueue   SelectorScope = "WithinLocalQueue"
	ScopeWithinClusterQueue SelectorScope = "WithinClusterQueue"
	ScopeWithinParentCohort SelectorScope = "WithinParentCohort"
	ScopeWithinCohortTree   SelectorScope = "WithinCohortTree"
	ScopeAnyClusterQueue    SelectorScope = "AnyClusterQueue"
)

// PriorityMode selects the priority value used by a constraint.
type PriorityMode string

const (
	PriorityBase    PriorityMode = "Base"
	PriorityBoosted PriorityMode = "Boosted"
)

// Numeric comparison operators used by configurable preemption.
type Comparison string

const (
	CompareLessThan           Comparison = "LessThan"
	CompareLessThanOrEqual    Comparison = "LessThanOrEqual"
	CompareGreaterThan        Comparison = "GreaterThan"
	CompareGreaterThanOrEqual Comparison = "GreaterThanOrEqual"
	CompareEqual              Comparison = "Equal"
	CompareNotEqual           Comparison = "NotEqual"
)

// Valid reports whether the comparison operator is supported.
func (c Comparison) Valid() bool {
	switch c {
	case CompareLessThan, CompareLessThanOrEqual, CompareGreaterThan,
		CompareGreaterThanOrEqual, CompareEqual, CompareNotEqual:
		return true
	default:
		return false
	}
}

// CompareInt evaluates value <op> target.
func CompareInt(value, target int64, op Comparison) bool {
	switch op {
	case CompareLessThan:
		return value < target
	case CompareLessThanOrEqual:
		return value <= target
	case CompareGreaterThan:
		return value > target
	case CompareGreaterThanOrEqual:
		return value >= target
	case CompareEqual:
		return value == target
	case CompareNotEqual:
		return value != target
	default:
		return false
	}
}

// PriorityConstraint compares the candidate's priority with the incoming
// workload's priority. Boosted adds 1000 to the candidate's priority, giving
// the teaching implementation a deterministic analogue of the real boosted
// priority mode.
type PriorityConstraint struct {
	Mode       PriorityMode
	Comparison Comparison
}

// NumericLabelConstraint filters candidates by an integer-valued label.
type NumericLabelConstraint struct {
	Key           string
	Comparison    Comparison
	FallbackValue int64
	MinValue      *int64
	MaxValue      *int64
}

// PreemptionCandidateSelector mirrors one candidateSelectors entry.
type PreemptionCandidateSelector struct {
	Scope                SelectorScope
	Priority             *PriorityConstraint
	NumericLabels        []NumericLabelConstraint
	LabelSelector        *LabelSelector
	ClusterQueueSelector *LabelSelector
}

// PreemptionRule is one rule in a PreemptionConfig.
type PreemptionRule struct {
	Name               string
	Trigger            PreemptionTrigger
	CandidateSelectors []PreemptionCandidateSelector
}

// PreemptionConfig is the MiniKueue analogue of the alpha
// kueue.x-k8s.io/v1alpha1 PreemptionConfig. Rules are additive; candidates
// selected by any rule are merged with the classic preemption candidates.
type PreemptionConfig struct {
	Name  string
	Rules []PreemptionRule
}

// StopPolicy mirrors ClusterQueue.spec.stopPolicy.
type StopPolicy string

const (
	StopNone         StopPolicy = "None"
	StopHold         StopPolicy = "Hold"
	StopHoldAndDrain StopPolicy = "HoldAndDrain"
)

// FairSharing is the weight used by the simplified fair-share ordering.
type FairSharing struct {
	Weight float64
}

// AdmissionScope selects how admission order is computed for a ClusterQueue.
// UsageBasedAdmissionFairSharing enables the MiniKueue Admission Fair Sharing
// ordering, which looks at historical LocalQueue usage rather than the current
// borrowed share.
type AdmissionScope string

const (
	AdmissionScopeNone                       AdmissionScope = ""
	AdmissionScopeUsageBasedAdmissionSharing AdmissionScope = "UsageBasedAdmissionFairSharing"
)

// ConcurrentAdmissionPolicy is a deliberately small model of Kueue's alpha
// concurrent admission. A workload may start on a later flavor and migrate to
// an earlier (more preferred) flavor once it becomes feasible.
type ConcurrentAdmissionPolicy struct {
	// Mode is documented as TryPreferredFlavors; an empty value is treated as
	// that mode.
	Mode string
	// LastAcceptableFlavorName bounds how far a workload may migrate. Empty
	// means the last flavor in the resource group.
	LastAcceptableFlavorName string
}

// LabelSelectorRequirement is a set-based namespace label requirement. It
// mirrors the subset of metav1.LabelSelectorRequirement that MiniKueue can
// evaluate without depending on apimachinery.
type LabelSelectorRequirement struct {
	Key      string
	Operator string // In, NotIn, Exists, DoesNotExist
	Values   []string
}

// Matches evaluates one requirement against namespace labels.
func (r LabelSelectorRequirement) Matches(labels map[string]string) bool {
	value, exists := labels[r.Key]
	switch r.Operator {
	case "In":
		if !exists {
			return false
		}
		for _, v := range r.Values {
			if value == v {
				return true
			}
		}
		return false
	case "NotIn":
		if !exists {
			return true
		}
		for _, v := range r.Values {
			if value == v {
				return false
			}
		}
		return true
	case "Exists":
		return exists
	case "DoesNotExist":
		return !exists
	default:
		return false
	}
}

// LabelSelector mirrors metav1.LabelSelector. A nil selector matches no
// namespace, while a non-nil empty selector matches all namespaces; this is
// exactly Kueue's namespaceSelector contract.
type LabelSelector struct {
	MatchLabels      map[string]string
	MatchExpressions []LabelSelectorRequirement
}

// Matches reports whether labels satisfy every requirement.
func (s *LabelSelector) Matches(labels map[string]string) bool {
	if s == nil {
		return false
	}
	for k, v := range s.MatchLabels {
		// A missing key is not equal to an empty expected value; Kubernetes
		// label equality requires the label to exist.
		if got, ok := labels[k]; !ok || got != v {
			return false
		}
	}
	for _, req := range s.MatchExpressions {
		if !req.Matches(labels) {
			return false
		}
	}
	return true
}

// FlavorQuota is one flavor entry inside a resource group.
type FlavorQuota struct {
	Name           string
	Nominal        ResourceList
	BorrowingLimit ResourceList
	LendingLimit   ResourceList
}

// ResourceGroup ties resources that must be assigned to the same flavor.
type ResourceGroup struct {
	CoveredResources []ResourceName
	Flavors          []FlavorQuota
}

// ClusterQueueSpec is a deliberately small subset of Kueue's v1beta2 spec.
type ClusterQueueSpec struct {
	// NamespaceSelector nil matches no namespace; a non-nil empty selector
	// matches every namespace, mirroring Kueue.
	NamespaceSelector *LabelSelector
	ResourceGroups    []ResourceGroup
	CohortName        string
	QueueingStrategy  QueueingStrategy
	Preemption        PreemptionPolicy
	AdmissionChecks   []string
	StopPolicy        StopPolicy
	FairSharing       *FairSharing
	// CapacityProvider, when set, makes this queue's effective nominal quota
	// come from the named provider instead of the static spec. It is the
	// MiniKueue analogue of Dynamic Quota Orchestration.
	CapacityProvider string
	// AdmissionScope selects the admission-ordering mechanism.
	AdmissionScope AdmissionScope
	// ConcurrentAdmission enables flavor migration for this queue.
	ConcurrentAdmission *ConcurrentAdmissionPolicy
	// DeviceClassMappings resolves DRA DeviceClasses to logical quota
	// resources (for example gpu-a100 -> nvidia.com/gpu).
	DeviceClassMappings map[string]ResourceName
	// PreemptionConfigName is the MiniKueue analogue of the
	// kueue.x-k8s.io/preemption-config-name annotation. It enables alpha
	// configurable preemption for this queue.
	PreemptionConfigName string
}

// ClusterQueue is cluster-scoped and owns quota.
type ClusterQueue struct {
	Name              string
	Labels            map[string]string
	Spec              ClusterQueueSpec
	CreationTimestamp int64
}

// Cohort groups ClusterQueues. ParentName enables a simple cohort tree; the
// mini scheduler aggregates capacity over the whole tree.
type Cohort struct {
	Name              string
	ParentName        string
	ResourceGroups    []ResourceGroup
	FairSharing       *FairSharing
	CreationTimestamp int64
}

// LocalQueue is namespaced and points at a ClusterQueue. StopPolicy is the
// queue-level emergency brake; it behaves like the ClusterQueue field but only
// affects workloads submitted through this LocalQueue.
type LocalQueue struct {
	Namespace    string
	Name         string
	ClusterQueue string
	StopPolicy   StopPolicy
	// FairSharing is the LocalQueue weight used by Admission Fair Sharing.
	FairSharing       *FairSharing
	CreationTimestamp int64
}

// ResourceFlavor describes a class of capacity. NodeLabels, taints and
// tolerations are intentionally metadata-only in this simulator.
type ResourceFlavor struct {
	Name         string
	NodeLabels   map[string]string
	TopologyName string
}

// Topology describes a physical hierarchy such as block/rack/hostname.
// Levels are ordered from coarsest to finest.
type Topology struct {
	Name   string
	Levels []string
}

// TopologyDomain is a leaf domain of a topology. Values has exactly one entry
// per topology level, and Capacity is the amount of schedulable capacity in
// that leaf. Real Kueue derives this from Nodes and running Pods; MiniKueue
// makes it explicit so placement is deterministic and testable.
type TopologyDomain struct {
	Name     string
	Topology string
	Values   []string
	Capacity ResourceList
}

// CapacityProvider reports the capacity a dynamic quota orchestrator may
// distribute. It is a deliberately small stand-in for Kueue's alpha DQO API.
type CapacityProvider struct {
	Name     string
	Capacity map[string]ResourceList // flavor name -> resources
}

// AdmissionCheck is a cluster-scoped gate evaluated after quota reservation.
type AdmissionCheck struct {
	Name           string
	ControllerName string
}

// WorkloadPriorityClass assigns a numeric priority without touching pod
// priority, matching Kueue's WorkloadPriorityClass concept.
type WorkloadPriorityClass struct {
	Name  string
	Value int32
}

// TopologyMode is the TAS placement requirement for a pod set.
type TopologyMode string

const (
	TopologyUnconstrained TopologyMode = "Unconstrained"
	TopologyPreferred     TopologyMode = "Preferred"
	TopologyRequired      TopologyMode = "Required"
)

// PodSetTopology is the mini representation of a TAS annotation.
type PodSetTopology struct {
	Mode  TopologyMode
	Level string
	Group string
	// SliceSize splits the PodSet into slices of at most SliceSize pods, each
	// slice placed inside one topology domain. Zero means no slicing. This is
	// the MiniKueue analogue of podset-slice-required-topology/slice-size.
	SliceSize int32
	// Spreading prefers the least-used domain instead of the first domain with
	// free capacity, approximating podset-topology-spreading.
	Spreading bool
}

// DeviceRequest asks for devices from a DRA-style DeviceClass. It is the
// MiniKueue analogue of a ResourceClaimTemplate's device request. The
// integration layer (or Submit) resolves DeviceClass to a logical quota
// resource through ClusterQueue.Spec.DeviceClassMappings.
type DeviceRequest struct {
	DeviceClass string
	Count       int32
}

// TransformStrategy controls whether the input resource survives a resource
// transformation. It mirrors Configuration.resources.transformations[].strategy.
type TransformStrategy string

const (
	// TransformRetain keeps the input quantity and adds the transformed
	// outputs, matching Kueue's "Retain" strategy.
	TransformRetain TransformStrategy = "Retain"
	// TransformReplace removes the input quantity and adds the transformed
	// outputs. An empty outputs list therefore means "ignore this resource for
	// quota accounting", which is Kueue's documented way to exclude a resource.
	TransformReplace TransformStrategy = "Replace"
)

// ResourceTransformation maps a requested resource to quota-accounting
// resources. It is a MiniKueue implementation of Kueue's cluster-level
// `resources.transformations` configuration.
//
// The real API expresses MultiplyBy as another resource name: the amount of
// that resource requested by the Pod multiplies the input. MiniKueue also
// accepts a plain non-negative integer as a fixed multiplier so small examples
// do not need a synthetic "count" resource. Outputs are per unit of input.
type ResourceTransformation struct {
	Input      ResourceName
	Strategy   TransformStrategy
	MultiplyBy string
	Outputs    ResourceList
}

// Apply transforms one Pod's resource requests. It never mutates the input.
func (t ResourceTransformation) Apply(perPod ResourceList) ResourceList {
	out := perPod.Clone()
	if out == nil {
		out = ResourceList{}
	}
	input := out.Get(t.Input)
	if input <= 0 {
		return out
	}
	multiplier := int64(1)
	if t.MultiplyBy != "" {
		if fixed, ok := parseFixedMultiplier(t.MultiplyBy); ok {
			multiplier = fixed
		} else if fromResource, ok := out[ResourceName(t.MultiplyBy)]; ok {
			multiplier = fromResource
		} else {
			// A named multiplier that the Pod does not request contributes
			// nothing. Treating it as 1 would silently over-account workloads
			// that forgot the multiplier resource.
			multiplier = 0
		}
	}
	amount := input * multiplier
	if t.Strategy == "" || t.Strategy == TransformReplace {
		delete(out, t.Input)
	}
	for resource, perUnit := range t.Outputs {
		out[resource] += amount * perUnit
	}
	return out
}

func parseFixedMultiplier(value string) (int64, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, false
	}
	n, err := strconv.ParseInt(value, 10, 64)
	if err != nil || n < 0 {
		return 0, false
	}
	return n, true
}

// ApplyResourceTransformations applies transformations in declaration order.
// Every rule reads the original PodSpec quantities, not the intermediate
// result, matching Kueue's behaviour: transformations are independent and do
// not chain into second-order effects. Deletions and outputs accumulate in the
// result map.
func ApplyResourceTransformations(perPod ResourceList, transformations []ResourceTransformation) ResourceList {
	if len(transformations) == 0 {
		return perPod
	}
	original := perPod.Clone()
	if original == nil {
		original = ResourceList{}
	}
	out := original.Clone()
	for _, transformation := range transformations {
		input := original.Get(transformation.Input)
		if input <= 0 {
			continue
		}
		multiplier := int64(1)
		if transformation.MultiplyBy != "" {
			if fixed, ok := parseFixedMultiplier(transformation.MultiplyBy); ok {
				multiplier = fixed
			} else if fromResource, ok := original[ResourceName(transformation.MultiplyBy)]; ok {
				multiplier = fromResource
			} else {
				multiplier = 0
			}
		}
		amount := input * multiplier
		if transformation.Strategy == "" || transformation.Strategy == TransformReplace {
			delete(out, transformation.Input)
		}
		for resource, perUnit := range transformation.Outputs {
			out[resource] += amount * perUnit
		}
	}
	return out
}

// PodSet is a homogeneous set of pods.
type PodSet struct {
	Name     string
	Count    int32
	Requests ResourceList // per pod
	Topology *PodSetTopology
	// Devices models DRA device requests. Mapped devices are counted as quota
	// resources; if the same resource is also present in Requests, the larger
	// of the two values is used to avoid double counting.
	Devices []DeviceRequest
}

// WorkloadSpec is the unit of admission.
type WorkloadSpec struct {
	Active                  bool
	QueueName               string
	Priority                int32
	PriorityClassName       string
	PodSets                 []PodSet
	CreationTimestamp       int64
	MaximumExecutionSeconds int64
	// DeviceClassMappings is copied from the target ClusterQueue at Submit time
	// so resource accounting is self-contained. An explicit non-nil map wins.
	DeviceClassMappings map[string]ResourceName
	// AdmissionGates are external pre-admission gates. The workload cannot
	// reserve quota until every gate is cleared.
	AdmissionGates []string
	// ResourceTransformations are copied from the controller configuration at
	// Submit time. They are applied per Pod after DRA devices are resolved and
	// before the total is compared against quota.
	ResourceTransformations []ResourceTransformation
}

// WorkloadState is the coarse lifecycle state used by the simulator.
type WorkloadState string

const (
	StatePending       WorkloadState = "Pending"
	StateQuotaReserved WorkloadState = "QuotaReserved"
	StateAdmitted      WorkloadState = "Admitted"
	StateFinished      WorkloadState = "Finished"
)

// Condition is a compact Kubernetes-style condition.
type Condition struct {
	Type               string
	Status             string // "True", "False", "Unknown"
	Reason             string
	Message            string
	LastTransitionTick int64
}

// AdmissionCheckState mirrors Workload.status.admissionChecks.
type AdmissionCheckState struct {
	Name    string
	State   string // Pending, Ready, Retry, Rejected
	Message string
	// RequeueAfterSeconds is the minimum delay before retrying after a Retry
	// state. Zero means retry on the next scheduling opportunity.
	RequeueAfterSeconds int32
	// RetryCount is incremented on every transition to Retry.
	RetryCount int32
	// LastTransitionTick records when the state last changed.
	LastTransitionTick int64
}

// EvictionStat mirrors Workload.status.schedulingStats.evictions. Entries are
// keyed by the (reason, underlyingCause) pair.
type EvictionStat struct {
	Reason          string
	UnderlyingCause string
	Count           int32
}

// FlavorAssignment records which flavor a resource group received.
type FlavorAssignment struct {
	GroupIndex int
	Flavor     string
	Resources  ResourceList
}

// TopologyAssignment records a TAS placement decision.
type TopologyAssignment struct {
	Topology  string
	PodSet    string
	Level     string
	Domain    string
	PodCount  int32
	Resources ResourceList
}

// Admission is the quota reservation attached to a workload.
type Admission struct {
	ClusterQueue        string
	FlavorAssignments   []FlavorAssignment
	Usage               ResourceList
	TopologyAssignments []TopologyAssignment
}

// WorkloadStatus is the observed state of a workload.
type WorkloadStatus struct {
	State           WorkloadState
	Conditions      []Condition
	Admission       *Admission
	AdmissionChecks []AdmissionCheckState
	RequeueAt       int64
	Requeues        int32
	Evictions       int32
	EvictionStats   []EvictionStat
	AdmittedAt      int64
	Sequence        int64
	// PodsReady tracks the WaitForPodsReady safety net. It is only meaningful
	// when the engine enables the feature; otherwise it is true on admission.
	PodsReady   bool
	PodsReadyAt int64
	// ReclaimablePods records pods reported by the integration layer as no
	// longer needed. PodSet.Spec.Count is immutable; admission accounting uses
	// Count - ReclaimablePods.
	ReclaimablePods map[string]int32
	// ExecutionTicks accumulates admitted time across eviction/re-admission
	// cycles. AdmittedAt is the start of the current admitted period, so the
	// cumulative execution time is ExecutionTicks + (clock - AdmittedAt).
	// maximumExecutionTimeSeconds is defined over this cumulative value; a
	// preemption and requeue must not reset the clock. Submit starts a new
	// incarnation and clears it.
	ExecutionTicks int64
}

// Workload is namespaced; UID is stable across updates.
type Workload struct {
	Namespace string
	Name      string
	UID       string
	Labels    map[string]string
	Spec      WorkloadSpec
	Status    WorkloadStatus
}

// Key returns namespace/name.
func (w *Workload) Key() string {
	return w.Namespace + "/" + w.Name
}

// EffectiveCount returns the pod count after subtracting pods that the job
// integration has reported as reclaimable. It never returns a negative value.
func (w *Workload) EffectiveCount(ps PodSet) int32 {
	count := ps.Count
	if w.Status.ReclaimablePods != nil {
		count -= w.Status.ReclaimablePods[ps.Name]
	}
	if count < 0 {
		return 0
	}
	return count
}

// PodSetPerPodUsage returns the effective per-pod resources of a PodSet,
// including DRA devices resolved through Spec.DeviceClassMappings. If the same
// logical resource is requested both as an extended resource and through a
// device, the larger value wins, matching Kueue's intent to avoid double
// counting.
func (w *Workload) PodSetPerPodUsage(ps PodSet) ResourceList {
	if len(ps.Devices) == 0 && len(w.Spec.ResourceTransformations) == 0 {
		// Fast path: no device requests and no transformations, the caller
		// only reads the map.
		return ps.Requests
	}
	perPod := ps.Requests.Clone()
	if perPod == nil {
		perPod = ResourceList{}
	}
	devices := ResourceList{}
	for _, d := range ps.Devices {
		if d.Count <= 0 || d.DeviceClass == "" {
			continue
		}
		resource, ok := w.Spec.DeviceClassMappings[d.DeviceClass]
		if !ok || resource == "" || resource == Pods {
			// Unmapped devices cannot be priced. Validation rejects this at
			// submit time for managed workloads; keeping this tolerant makes
			// direct construction in tests less brittle. Mapping to the reserved
			// pods resource is also ignored: pods are counted automatically.
			continue
		}
		devices[resource] += int64(d.Count)
	}
	for name, value := range devices {
		if value > perPod.Get(name) {
			perPod[name] = value
		}
	}
	return ApplyResourceTransformations(perPod, w.Spec.ResourceTransformations)
}

// TotalUsage returns the total resource usage of all pod sets plus the
// automatically accounted pods resource. Reclaimable pods are excluded.
func (w *Workload) TotalUsage() ResourceList {
	total := ResourceList{}
	for _, ps := range w.Spec.PodSets {
		count := w.EffectiveCount(ps)
		for name, value := range w.PodSetPerPodUsage(ps) {
			total[name] += value * int64(count)
		}
		total[Pods] += int64(count)
	}
	return total
}

// HasAdmissionGates reports whether an external gate still blocks admission.
func (w *Workload) HasAdmissionGates() bool {
	return len(w.Spec.AdmissionGates) > 0
}

// ClearAdmissionGate removes one gate. It reports whether the gate existed.
func (w *Workload) ClearAdmissionGate(name string) bool {
	out := w.Spec.AdmissionGates[:0]
	found := false
	for _, gate := range w.Spec.AdmissionGates {
		if gate == name {
			found = true
			continue
		}
		out = append(out, gate)
	}
	w.Spec.AdmissionGates = out
	return found
}

// WeightedSum converts a resource list into one scalar for fairness/usage
// accounting. CPU is millicores/1000, memory is MiB/1024 and other resources
// are whole units. Missing weights default to 1.
func (r ResourceList) WeightedSum(weights map[ResourceName]float64) float64 {
	var sum float64
	for name, value := range r {
		weight := 1.0
		if w, ok := weights[name]; ok {
			weight = w
		}
		scale := 1.0
		switch name {
		case CPU:
			scale = 0.001
		case Memory:
			scale = 1.0 / 1024.0
		}
		sum += float64(value) * scale * weight
	}
	return sum
}

// EffectivePriority resolves priorityClassName against WorkloadPriorityClass
// objects, falling back to the literal priority.
func (w *Workload) EffectivePriority(classes map[string]*WorkloadPriorityClass) int32 {
	if w.Spec.PriorityClassName != "" {
		if pc, ok := classes[w.Spec.PriorityClassName]; ok {
			return pc.Value
		}
	}
	return w.Spec.Priority
}

// Condition returns a condition by type.
func (s *WorkloadStatus) Condition(condType string) (Condition, bool) {
	for _, c := range s.Conditions {
		if c.Type == condType {
			return c, true
		}
	}
	return Condition{}, false
}

// SetCondition inserts or updates a condition.
func (s *WorkloadStatus) SetCondition(condType, status, reason, message string, tick int64) {
	for i := range s.Conditions {
		if s.Conditions[i].Type == condType {
			s.Conditions[i] = Condition{Type: condType, Status: status, Reason: reason, Message: message, LastTransitionTick: tick}
			return
		}
	}
	s.Conditions = append(s.Conditions, Condition{Type: condType, Status: status, Reason: reason, Message: message, LastTransitionTick: tick})
}

// ClearCondition removes a condition.
func (s *WorkloadStatus) ClearCondition(condType string) {
	out := s.Conditions[:0]
	for _, c := range s.Conditions {
		if c.Type != condType {
			out = append(out, c)
		}
	}
	s.Conditions = out
}

// GetAdmissionCheck returns the named check state.
func (s *WorkloadStatus) GetAdmissionCheck(name string) (AdmissionCheckState, bool) {
	for _, cs := range s.AdmissionChecks {
		if cs.Name == name {
			return cs, true
		}
	}
	return AdmissionCheckState{}, false
}

// SetAdmissionCheck inserts or updates a check state, preserving retry
// bookkeeping and any requeue delay previously set by the controller.
func (s *WorkloadStatus) SetAdmissionCheck(name, state, message string) {
	for i := range s.AdmissionChecks {
		if s.AdmissionChecks[i].Name == name {
			if s.AdmissionChecks[i].State != state {
				s.AdmissionChecks[i].LastTransitionTick = 0
			}
			s.AdmissionChecks[i].State = state
			s.AdmissionChecks[i].Message = message
			return
		}
	}
	s.AdmissionChecks = append(s.AdmissionChecks, AdmissionCheckState{Name: name, State: state, Message: message})
}

// SetAdmissionCheckRetry transitions a check to Retry and records the minimum
// delay before the next attempt. The retry counter is incremented for every
// transition into Retry, matching Kueue's status contract.
func (s *WorkloadStatus) SetAdmissionCheckRetry(name, message string, requeueAfterSeconds int32) {
	for i := range s.AdmissionChecks {
		if s.AdmissionChecks[i].Name == name {
			s.AdmissionChecks[i].State = "Retry"
			s.AdmissionChecks[i].Message = message
			s.AdmissionChecks[i].RequeueAfterSeconds = requeueAfterSeconds
			s.AdmissionChecks[i].RetryCount++
			return
		}
	}
	s.AdmissionChecks = append(s.AdmissionChecks, AdmissionCheckState{
		Name: name, State: "Retry", Message: message,
		RequeueAfterSeconds: requeueAfterSeconds, RetryCount: 1,
	})
}

// Clone returns a deep copy of the workload.
func (w *Workload) Clone() *Workload {
	out := *w
	out.Labels = cloneStringMap(w.Labels)
	out.Spec.AdmissionGates = append([]string(nil), w.Spec.AdmissionGates...)
	out.Spec.ResourceTransformations = cloneResourceTransformations(w.Spec.ResourceTransformations)
	if w.Spec.DeviceClassMappings != nil {
		out.Spec.DeviceClassMappings = make(map[string]ResourceName, len(w.Spec.DeviceClassMappings))
		for k, v := range w.Spec.DeviceClassMappings {
			out.Spec.DeviceClassMappings[k] = v
		}
	}
	out.Spec.PodSets = make([]PodSet, len(w.Spec.PodSets))
	for i, ps := range w.Spec.PodSets {
		out.Spec.PodSets[i] = ps
		out.Spec.PodSets[i].Requests = ps.Requests.Clone()
		out.Spec.PodSets[i].Devices = append([]DeviceRequest(nil), ps.Devices...)
		if ps.Topology != nil {
			topo := *ps.Topology
			out.Spec.PodSets[i].Topology = &topo
		}
	}
	out.Status.Conditions = append([]Condition(nil), w.Status.Conditions...)
	out.Status.AdmissionChecks = append([]AdmissionCheckState(nil), w.Status.AdmissionChecks...)
	out.Status.EvictionStats = append([]EvictionStat(nil), w.Status.EvictionStats...)
	if w.Status.ReclaimablePods != nil {
		out.Status.ReclaimablePods = make(map[string]int32, len(w.Status.ReclaimablePods))
		for name, count := range w.Status.ReclaimablePods {
			out.Status.ReclaimablePods[name] = count
		}
	}
	if w.Status.Admission != nil {
		out.Status.Admission = w.Status.Admission.Clone()
	}
	return &out
}

// Clone returns a deep copy of an admission.
func (a *Admission) Clone() *Admission {
	if a == nil {
		return nil
	}
	out := *a
	out.FlavorAssignments = make([]FlavorAssignment, len(a.FlavorAssignments))
	for i, fa := range a.FlavorAssignments {
		out.FlavorAssignments[i] = fa
		out.FlavorAssignments[i].Resources = fa.Resources.Clone()
	}
	out.Usage = a.Usage.Clone()
	out.TopologyAssignments = make([]TopologyAssignment, len(a.TopologyAssignments))
	for i, ta := range a.TopologyAssignments {
		out.TopologyAssignments[i] = ta
		out.TopologyAssignments[i].Resources = ta.Resources.Clone()
	}
	return &out
}

// Clone returns a deep copy of a PreemptionConfig.
func (p *PreemptionConfig) Clone() *PreemptionConfig {
	if p == nil {
		return nil
	}
	out := *p
	out.Rules = make([]PreemptionRule, len(p.Rules))
	for i, rule := range p.Rules {
		out.Rules[i] = rule
		out.Rules[i].CandidateSelectors = make([]PreemptionCandidateSelector, len(rule.CandidateSelectors))
		for j, selector := range rule.CandidateSelectors {
			cloned := selector
			if selector.Priority != nil {
				priority := *selector.Priority
				cloned.Priority = &priority
			}
			cloned.NumericLabels = make([]NumericLabelConstraint, len(selector.NumericLabels))
			for k, constraint := range selector.NumericLabels {
				cloned.NumericLabels[k] = constraint
				if constraint.MinValue != nil {
					value := *constraint.MinValue
					cloned.NumericLabels[k].MinValue = &value
				}
				if constraint.MaxValue != nil {
					value := *constraint.MaxValue
					cloned.NumericLabels[k].MaxValue = &value
				}
			}
			cloned.LabelSelector = cloneLabelSelector(selector.LabelSelector)
			cloned.ClusterQueueSelector = cloneLabelSelector(selector.ClusterQueueSelector)
			out.Rules[i].CandidateSelectors[j] = cloned
		}
	}
	return &out
}

func cloneStringMap(in map[string]string) map[string]string {
	if in == nil {
		return nil
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// Clone returns a deep copy of a ClusterQueue.
func (cq *ClusterQueue) Clone() *ClusterQueue {
	if cq == nil {
		return nil
	}
	out := *cq
	out.Labels = cloneStringMap(cq.Labels)
	out.Spec.NamespaceSelector = cloneLabelSelector(cq.Spec.NamespaceSelector)
	out.Spec.ResourceGroups = cloneResourceGroups(cq.Spec.ResourceGroups)
	out.Spec.Preemption = clonePreemptionPolicy(cq.Spec.Preemption)
	out.Spec.AdmissionChecks = append([]string(nil), cq.Spec.AdmissionChecks...)
	if cq.Spec.FairSharing != nil {
		fs := *cq.Spec.FairSharing
		out.Spec.FairSharing = &fs
	}
	if cq.Spec.ConcurrentAdmission != nil {
		ca := *cq.Spec.ConcurrentAdmission
		out.Spec.ConcurrentAdmission = &ca
	}
	out.Spec.DeviceClassMappings = cloneDeviceMappings(cq.Spec.DeviceClassMappings)
	return &out
}

// Clone returns a deep copy of a Cohort.
func (c *Cohort) Clone() *Cohort {
	if c == nil {
		return nil
	}
	out := *c
	out.ResourceGroups = cloneResourceGroups(c.ResourceGroups)
	if c.FairSharing != nil {
		fs := *c.FairSharing
		out.FairSharing = &fs
	}
	return &out
}

// Clone returns a deep copy of a LocalQueue.
func (q *LocalQueue) Clone() *LocalQueue {
	if q == nil {
		return nil
	}
	out := *q
	if q.FairSharing != nil {
		fs := *q.FairSharing
		out.FairSharing = &fs
	}
	return &out
}

// Clone returns a deep copy of a ResourceFlavor.
func (f *ResourceFlavor) Clone() *ResourceFlavor {
	if f == nil {
		return nil
	}
	out := *f
	out.NodeLabels = cloneStringMap(f.NodeLabels)
	return &out
}

// Clone returns a deep copy of a Topology.
func (t *Topology) Clone() *Topology {
	if t == nil {
		return nil
	}
	out := *t
	out.Levels = append([]string(nil), t.Levels...)
	return &out
}

// Clone returns a deep copy of a TopologyDomain.
func (d *TopologyDomain) Clone() *TopologyDomain {
	if d == nil {
		return nil
	}
	out := *d
	out.Values = append([]string(nil), d.Values...)
	out.Capacity = d.Capacity.Clone()
	return &out
}

// Clone returns a deep copy of a CapacityProvider.
func (p *CapacityProvider) Clone() *CapacityProvider {
	if p == nil {
		return nil
	}
	out := *p
	out.Capacity = make(map[string]ResourceList, len(p.Capacity))
	for flavor, resources := range p.Capacity {
		out.Capacity[flavor] = resources.Clone()
	}
	return &out
}

// Clone returns a deep copy of an AdmissionCheck.
func (c *AdmissionCheck) Clone() *AdmissionCheck {
	if c == nil {
		return nil
	}
	out := *c
	return &out
}

// Clone returns a deep copy of a WorkloadPriorityClass.
func (pc *WorkloadPriorityClass) Clone() *WorkloadPriorityClass {
	if pc == nil {
		return nil
	}
	out := *pc
	return &out
}

func cloneLabelSelector(s *LabelSelector) *LabelSelector {
	if s == nil {
		return nil
	}
	out := &LabelSelector{MatchLabels: cloneStringMap(s.MatchLabels)}
	out.MatchExpressions = make([]LabelSelectorRequirement, len(s.MatchExpressions))
	for i, req := range s.MatchExpressions {
		out.MatchExpressions[i] = req
		out.MatchExpressions[i].Values = append([]string(nil), req.Values...)
	}
	return out
}

func cloneResourceGroups(groups []ResourceGroup) []ResourceGroup {
	out := make([]ResourceGroup, len(groups))
	for i, group := range groups {
		out[i].CoveredResources = append([]ResourceName(nil), group.CoveredResources...)
		out[i].Flavors = make([]FlavorQuota, len(group.Flavors))
		for j, fq := range group.Flavors {
			out[i].Flavors[j] = fq
			out[i].Flavors[j].Nominal = fq.Nominal.Clone()
			out[i].Flavors[j].BorrowingLimit = fq.BorrowingLimit.Clone()
			out[i].Flavors[j].LendingLimit = fq.LendingLimit.Clone()
		}
	}
	return out
}

func clonePreemptionPolicy(p PreemptionPolicy) PreemptionPolicy {
	out := p
	if p.BorrowWithinCohort != nil {
		borrow := *p.BorrowWithinCohort
		if borrow.MaxPriorityThreshold != nil {
			threshold := *borrow.MaxPriorityThreshold
			borrow.MaxPriorityThreshold = &threshold
		}
		out.BorrowWithinCohort = &borrow
	}
	return out
}

func cloneResourceTransformations(in []ResourceTransformation) []ResourceTransformation {
	if in == nil {
		return nil
	}
	out := make([]ResourceTransformation, len(in))
	for i, transformation := range in {
		out[i] = transformation
		out[i].Outputs = transformation.Outputs.Clone()
	}
	return out
}

func cloneDeviceMappings(in map[string]ResourceName) map[string]ResourceName {
	if in == nil {
		return nil
	}
	out := make(map[string]ResourceName, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}
