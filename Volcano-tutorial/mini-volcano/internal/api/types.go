package api

import (
	"fmt"
	"sort"
	"strings"
)

// ---------------------------------------------------------------------------
// Enumerations
// ---------------------------------------------------------------------------

type JobPhase string

const (
	JobPending     JobPhase = "Pending"
	JobInqueue     JobPhase = "Inqueue"
	JobRunning     JobPhase = "Running"
	JobRestarting  JobPhase = "Restarting"
	JobCompleting  JobPhase = "Completing"
	JobCompleted   JobPhase = "Completed"
	JobTerminating JobPhase = "Terminating"
	JobAborted     JobPhase = "Aborted"
	JobFailed      JobPhase = "Failed"
)

func (p JobPhase) Terminal() bool {
	return p == JobCompleted || p == JobAborted || p == JobFailed
}

type PodPhase string

const (
	PodPending   PodPhase = "Pending"
	PodAllocated PodPhase = "Allocated"
	PodPipelined PodPhase = "Pipelined"
	PodBinding   PodPhase = "Binding"
	PodBound     PodPhase = "Bound"
	PodRunning   PodPhase = "Running"
	PodReleasing PodPhase = "Releasing"
	PodSucceeded PodPhase = "Succeeded"
	PodFailed    PodPhase = "Failed"
)

// OccupiesNode reports whether the pod still holds node resources.
func (p PodPhase) OccupiesNode() bool {
	switch p {
	case PodAllocated, PodPipelined, PodBinding, PodBound, PodRunning, PodReleasing:
		return true
	default:
		return false
	}
}

// ReadyForGang counts toward minAvailable, mirroring Volcano's gang plugin
// notion of "Ready" (Bound/Binding/Running/Allocated/Pipelined/Succeeded).
func (p PodPhase) ReadyForGang() bool {
	switch p {
	case PodAllocated, PodPipelined, PodBinding, PodBound, PodRunning, PodSucceeded:
		return true
	default:
		return false
	}
}

type PodGroupPhase string

const (
	PodGroupPending   PodGroupPhase = "Pending"
	PodGroupInqueue   PodGroupPhase = "Inqueue"
	PodGroupRunning   PodGroupPhase = "Running"
	PodGroupUnknown   PodGroupPhase = "Unknown"
	PodGroupCompleted PodGroupPhase = "Completed"
)

type QueueState string

const (
	QueueOpen    QueueState = "Open"
	QueueClosed  QueueState = "Closed"
	QueueClosing QueueState = "Closing"
	QueueUnknown QueueState = "Unknown"
)

// ---------------------------------------------------------------------------
// Cluster objects
// ---------------------------------------------------------------------------

type Taint struct {
	Key    string `json:"key"`
	Value  string `json:"value,omitempty"`
	Effect string `json:"effect"` // NoSchedule, PreferNoSchedule, NoExecute
}

type Toleration struct {
	Key      string `json:"key,omitempty"`
	Operator string `json:"operator,omitempty"` // Equal, Exists
	Value    string `json:"value,omitempty"`
	Effect   string `json:"effect,omitempty"`
}

type NUMANode struct {
	ID          int      `json:"id"`
	Allocatable Resource `json:"allocatable"`
	Allocated   Resource `json:"allocated,omitempty"`
}

type Node struct {
	Name          string            `json:"name"`
	Allocatable   Resource          `json:"allocatable"`
	Allocated     Resource          `json:"allocated,omitempty"`
	Labels        map[string]string `json:"labels,omitempty"`
	Taints        []Taint           `json:"taints,omitempty"`
	NUMANodes     []NUMANode        `json:"numaNodes,omitempty"`
	Unschedulable bool              `json:"unschedulable,omitempty"`
}

// Queue is cluster scoped, like scheduling.volcano.sh/v1beta1 Queue.
type Queue struct {
	Name   string      `json:"name"`
	Spec   QueueSpec   `json:"spec"`
	Status QueueStatus `json:"status,omitempty"`
}

// NodeGroupSpec is MiniVolcano's simplified model of the v1.16 nodegroup
// plugin. Real Volcano configures it through queue annotations; the teaching
// implementation keeps the same semantics in typed fields so misconfiguration
// is caught at admission time.
const NodeGroupLabel = "volcano.sh/nodegroup-name"

type NodeGroupSpec struct {
	// Names restricts the queue to nodes whose label
	// volcano.sh/nodegroup-name is in this list.
	Names []string `json:"names,omitempty"`
	// Capability optionally limits how much a queue may allocate inside one
	// node group. Missing dimensions are unconstrained.
	Capability map[string]Resource `json:"capability,omitempty"`
}

type QueueSpec struct {
	Weight      int32          `json:"weight,omitempty"`
	Capability  Resource       `json:"capability,omitempty"`
	Deserved    Resource       `json:"deserved,omitempty"`
	Guarantee   Resource       `json:"guarantee,omitempty"`
	Reclaimable *bool          `json:"reclaimable,omitempty"`
	Parent      string         `json:"parent,omitempty"`
	Priority    int32          `json:"priority,omitempty"`
	State       QueueState     `json:"state,omitempty"`
	NodeGroup   *NodeGroupSpec `json:"nodeGroup,omitempty"`
}

func (q *Queue) Reclaimable() bool {
	if q.Spec.Reclaimable == nil {
		return true
	}
	return *q.Spec.Reclaimable
}

func (q *Queue) State() QueueState {
	if q.Status.State == "" {
		if q.Spec.State == "" {
			return QueueOpen
		}
		return q.Spec.State
	}
	return q.Status.State
}

type QueueStatus struct {
	State     QueueState `json:"state,omitempty"`
	Allocated Resource   `json:"allocated,omitempty"`
	Deserved  Resource   `json:"deserved,omitempty"`
	Inqueue   int32      `json:"inqueue,omitempty"`
	Pending   int32      `json:"pending,omitempty"`
	Running   int32      `json:"running,omitempty"`
}

// ---------------------------------------------------------------------------
// Workload objects
// ---------------------------------------------------------------------------

type LifecycleEvent string

const (
	EventPodFailed     LifecycleEvent = "PodFailed"
	EventPodEvicted    LifecycleEvent = "PodEvicted"
	EventTaskCompleted LifecycleEvent = "TaskCompleted"
	EventJobUnknown    LifecycleEvent = "JobUnknown"
)

type LifecycleAction string

const (
	ActionAbortJob      LifecycleAction = "AbortJob"
	ActionRestartJob    LifecycleAction = "RestartJob"
	ActionTerminateJob  LifecycleAction = "TerminateJob"
	ActionRestartTask   LifecycleAction = "RestartTask"
	ActionTerminateTask LifecycleAction = "TerminateTask"
	ActionCompleteJob   LifecycleAction = "CompleteJob"
)

type LifecyclePolicy struct {
	Event  LifecycleEvent  `json:"event"`
	Action LifecycleAction `json:"action"`
}

type TaskTopologySpec struct {
	TaskNames []string `json:"taskNames"`
	// Policy is "affinity" (co-locate) or "anti-affinity" (spread).
	Policy string `json:"policy"`
}

type NetworkTopologySpec struct {
	// Mode is "hard" or "soft".
	Mode string `json:"mode,omitempty"`
	// HighestTierAllowed is the worst (numerically largest) HyperNode tier
	// the job may be spread across. Tier 1 is the closest domain.
	HighestTierAllowed int `json:"highestTierAllowed,omitempty"`
}

// PodGroupAntiAffinityTerm is MiniVolcano's model of the v1.16 PodGroup
// topology anti-affinity term. A term with Weight == 0 is required; a term
// with Weight > 0 is preferred. Upstream identifies the domain by tier name
// or tier number; the teaching implementation uses the numeric tier because
// HyperNode.Tier is already part of the Mini API.
type PodGroupAntiAffinityTerm struct {
	// MatchLabels selects the peer PodGroups by their labels.
	MatchLabels map[string]string `json:"matchLabels"`
	// TopologyTier is the HyperNode tier whose domains must differ.
	TopologyTier int `json:"topologyTier"`
	// Weight distinguishes preferred (1-100) from required (0).
	Weight int32 `json:"weight,omitempty"`
}

// TopologyAffinitySpec lives on PodGroup, mirroring the v1.16
// spec.topologyAffinity.podGroupAntiAffinity list.
type TopologyAffinitySpec struct {
	PodGroupAntiAffinity []PodGroupAntiAffinityTerm `json:"podGroupAntiAffinity,omitempty"`
}

type TaskSpec struct {
	Name         string            `json:"name"`
	Replicas     int32             `json:"replicas"`
	MinAvailable *int32            `json:"minAvailable,omitempty"`
	Requests     Resource          `json:"requests"`
	Duration     int64             `json:"duration,omitempty"` // ticks
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
	Tolerations  []Toleration      `json:"tolerations,omitempty"`
	Labels       map[string]string `json:"labels,omitempty"`
	Annotations  map[string]string `json:"annotations,omitempty"`
	Policies     []LifecyclePolicy `json:"policies,omitempty"`
}

// EffectiveMinAvailable is the role-level gate. MiniVolcano only creates a
// gate when the task sets minAvailable explicitly; an omitted field is covered
// by the job-level minAvailable (upstream defaults it to replicas and writes
// minTaskMember for every role). An explicit 0 means "no role gate" too.
func (t *TaskSpec) EffectiveMinAvailable() int32 {
	if t.MinAvailable == nil || *t.MinAvailable <= 0 {
		return 0
	}
	if *t.MinAvailable > t.Replicas {
		return t.Replicas
	}
	return *t.MinAvailable
}

type JobSpec struct {
	SchedulerName     string `json:"schedulerName,omitempty"`
	Queue             string `json:"queue,omitempty"`
	PriorityClassName string `json:"priorityClassName,omitempty"`
	Priority          int32  `json:"priority,omitempty"`
	MinAvailable      int32  `json:"minAvailable,omitempty"`
	// MinAvailableSet distinguishes an explicit "minAvailable: 0" (disable
	// gang) from an omitted field (default to the sum of replicas), matching
	// the CRD's admission defaulting semantics.
	MinAvailableSet bool                 `json:"-"`
	MinSuccess      *int32               `json:"minSuccess,omitempty"`
	MaxRetry        int32                `json:"maxRetry,omitempty"`
	Tasks           []TaskSpec           `json:"tasks"`
	Policies        []LifecyclePolicy    `json:"policies,omitempty"`
	Plugins         map[string][]string  `json:"plugins,omitempty"`
	TaskTopology    []TaskTopologySpec   `json:"taskTopology,omitempty"`
	NetworkTopology *NetworkTopologySpec `json:"networkTopology,omitempty"`
	Labels          map[string]string    `json:"labels,omitempty"`
	Annotations     map[string]string    `json:"annotations,omitempty"`
}

type PriorityClass struct {
	Name          string `json:"name"`
	Value         int32  `json:"value"`
	GlobalDefault bool   `json:"globalDefault,omitempty"`
}

type Condition struct {
	Type    string `json:"type"`
	Status  string `json:"status"`
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`
	Tick    int64  `json:"tick,omitempty"`
}

type JobStatus struct {
	Phase      JobPhase    `json:"phase,omitempty"`
	Running    int32       `json:"running,omitempty"`
	Succeeded  int32       `json:"succeeded,omitempty"`
	Failed     int32       `json:"failed,omitempty"`
	Retries    int32       `json:"retries,omitempty"`
	StartTick  int64       `json:"startTick,omitempty"`
	FinishTick int64       `json:"finishTick,omitempty"`
	Conditions []Condition `json:"conditions,omitempty"`
	Message    string      `json:"message,omitempty"`
}

type Job struct {
	UID          string    `json:"uid"`
	Name         string    `json:"name"`
	Namespace    string    `json:"namespace"`
	Spec         JobSpec   `json:"spec"`
	Status       JobStatus `json:"status,omitempty"`
	CreationTick int64     `json:"creationTick"`
	PodGroupName string    `json:"podGroupName,omitempty"`
}

func (j *Job) Key() string { return j.Namespace + "/" + j.Name }

func (j *Job) TotalReplicas() int32 {
	var n int32
	for _, t := range j.Spec.Tasks {
		n += t.Replicas
	}
	return n
}

// MinTaskMember is the teaching analogue of PodGroup.spec.minTaskMember.
// Volcano's Job controller copies each task's effective minAvailable into the
// PodGroup so the gang plugin can enforce role-level gates.
func (j *Job) MinTaskMember() map[string]int32 {
	out := make(map[string]int32, len(j.Spec.Tasks))
	for i := range j.Spec.Tasks {
		t := &j.Spec.Tasks[i]
		out[t.Name] = t.EffectiveMinAvailable()
	}
	return out
}

// SumTaskMinAvailable is the number of Pods the role gates require. It is the
// default for an omitted job-level minAvailable and a lower bound for an
// explicit one: a job-level value below this sum can never satisfy every
// role, so admission rejects it instead of leaving a permanently Pending job.
func (j *Job) SumTaskMinAvailable() int32 {
	var n int32
	for i := range j.Spec.Tasks {
		n += j.Spec.Tasks[i].EffectiveMinAvailable()
	}
	return n
}

// RoleGatesActive mirrors the upstream interplay documented in 4.2.4: role
// gates only take effect when the job-level gate can cover their sum. If the
// job asks for fewer Pods than the roles require, these Pods could never form
// a complete gang, so the role gates are skipped rather than deadlocking the
// job. (An explicit job-level 0 still means "gang disabled" in MiniVolcano.)
func (j *Job) RoleGatesActive() bool {
	if j.Spec.MinAvailable <= 0 {
		return false
	}
	return j.Spec.MinAvailable >= j.SumTaskMinAvailable()
}

// MinResources is the gang-size-aware minimum resource request. Volcano
// computes PodGroup.spec.minResources from minAvailable and the role gates;
// MiniVolcano first reserves each active role's minimum, then fills the
// remaining budget in declaration order. This matters when roles request
// different amounts: taking the first N pods blindly can overestimate the
// admission footprint and reject a job that actually fits.
func (j *Job) MinResources() Resource {
	// minAvailable == 0 disables gang. Admission should then need room for at
	// most one replica, not the whole job; otherwise a partial-capacity queue
	// would reject a job that could legitimately start a subset.
	if j.Spec.MinAvailable <= 0 {
		best := Resource(nil)
		bestSum := int64(0)
		for _, t := range j.Spec.Tasks {
			if t.Replicas <= 0 || t.Requests.IsZero() {
				continue
			}
			sum := int64(0)
			for _, v := range t.Requests {
				sum += v
			}
			if best == nil || sum < bestSum {
				best, bestSum = t.Requests.Clone(), sum
			}
		}
		if best == nil {
			return Resource{}
		}
		return best
	}
	need := j.Spec.MinAvailable
	out := Resource{}
	taken := make([]int32, len(j.Spec.Tasks))
	// Active role gates are mandatory: reserve them before any extra replica,
	// otherwise a worker-heavy first task can consume the whole budget.
	if j.RoleGatesActive() {
		for i := range j.Spec.Tasks {
			t := &j.Spec.Tasks[i]
			want := t.EffectiveMinAvailable()
			for taken[i] < want && need > 0 {
				out = out.Add(t.Requests)
				taken[i]++
				need--
			}
		}
	}
	for i := range j.Spec.Tasks {
		t := &j.Spec.Tasks[i]
		for taken[i] < t.Replicas && need > 0 {
			out = out.Add(t.Requests)
			taken[i]++
			need--
		}
	}
	return out
}

type Pod struct {
	UID           string            `json:"uid"`
	Name          string            `json:"name"`
	Namespace     string            `json:"namespace"`
	Job           string            `json:"job"` // job UID
	JobName       string            `json:"jobName"`
	Task          string            `json:"task"`
	TaskIndex     int32             `json:"taskIndex"`
	Queue         string            `json:"queue"`
	Priority      int32             `json:"priority"`
	Requests      Resource          `json:"requests"`
	Labels        map[string]string `json:"labels,omitempty"`
	NodeSelector  map[string]string `json:"nodeSelector,omitempty"`
	Tolerations   []Toleration      `json:"tolerations,omitempty"`
	NodeName      string            `json:"nodeName,omitempty"`
	Phase         PodPhase          `json:"phase"`
	CreationTick  int64             `json:"creationTick"`
	ScheduleTick  int64             `json:"scheduleTick,omitempty"`
	StartTick     int64             `json:"startTick,omitempty"`
	FinishTick    int64             `json:"finishTick,omitempty"`
	Restarts      int32             `json:"restarts,omitempty"`
	NUMANode      int               `json:"numaNode,omitempty"`
	Evicted       bool              `json:"evicted,omitempty"`
	EvictedTick   int64             `json:"evictedTick,omitempty"`
	PolicyHandled bool              `json:"policyHandled,omitempty"`
	Message       string            `json:"message,omitempty"`
}

func (p *Pod) Key() string { return p.Namespace + "/" + p.Name }

type PodGroup struct {
	UID              string                `json:"uid"`
	Name             string                `json:"name"`
	Namespace        string                `json:"namespace"`
	Queue            string                `json:"queue"`
	MinMember        int32                 `json:"minMember"`
	MinTaskMember    map[string]int32      `json:"minTaskMember,omitempty"`
	MinResources     Resource              `json:"minResources"`
	Priority         int32                 `json:"priority"`
	Phase            PodGroupPhase         `json:"phase"`
	Running          int32                 `json:"running,omitempty"`
	Succeeded        int32                 `json:"succeeded,omitempty"`
	Failed           int32                 `json:"failed,omitempty"`
	CreationTick     int64                 `json:"creationTick"`
	Labels           map[string]string     `json:"labels,omitempty"`
	TopologyAffinity *TopologyAffinitySpec `json:"topologyAffinity,omitempty"`
}

type Reservation struct {
	Name      string   `json:"name"`
	Queue     string   `json:"queue"`
	Job       string   `json:"job,omitempty"` // job UID, empty means queue reservation
	NodeNames []string `json:"nodeNames,omitempty"`
	Resources Resource `json:"resources"`
	StartTick int64    `json:"startTick"`
	EndTick   int64    `json:"endTick"`
	Active    bool     `json:"active,omitempty"`
}

func (r *Reservation) ActiveAt(tick int64) bool {
	return tick >= r.StartTick && tick < r.EndTick
}

// PodDisruptionBudget mirrors the subset of policy/v1 PDB that Volcano's pdb
// plugin consumes: a label selector plus minAvailable or maxUnavailable.
// Exactly one of the two fields is meaningful; use -1 for the unset one.
type PodDisruptionBudget struct {
	Name           string            `json:"name"`
	Namespace      string            `json:"namespace"`
	Selector       map[string]string `json:"selector"`
	MinAvailable   int32             `json:"minAvailable,omitempty"`
	MaxUnavailable int32             `json:"maxUnavailable,omitempty"`
	// HasMin/HasMax distinguish an explicit 0 from "not set".
	HasMin bool `json:"hasMin,omitempty"`
	HasMax bool `json:"hasMax,omitempty"`
}

func (p *PodDisruptionBudget) Key() string { return p.Namespace + "/" + p.Name }

func (p *PodDisruptionBudget) Matches(pod *Pod) bool {
	if p == nil || pod == nil || pod.Namespace != p.Namespace {
		return false
	}
	for k, v := range p.Selector {
		if pod.Labels[k] != v {
			return false
		}
	}
	return true
}

// ResourceQuota is MiniVolcano's model of a namespace ResourceQuota. Only
// "hard" limits are needed by the scheduler-side admission check; usage is
// computed from admitted jobs rather than from the API Server.
type ResourceQuota struct {
	Name      string   `json:"name"`
	Namespace string   `json:"namespace"`
	Hard      Resource `json:"hard"`
}

func (q *ResourceQuota) Key() string { return q.Namespace + "/" + q.Name }

func (p *PodDisruptionBudget) Clone() *PodDisruptionBudget {
	if p == nil {
		return nil
	}
	out := *p
	out.Selector = cloneStringMap(p.Selector)
	return &out
}

func (q *ResourceQuota) Clone() *ResourceQuota {
	if q == nil {
		return nil
	}
	out := *q
	out.Hard = q.Hard.Clone()
	return &out
}

// HyperNode models the topology.volcano.sh/v1alpha1 HyperNode tree. Tier 1 is
// the lowest (closest) domain; larger tiers are wider failure domains.
type HyperNode struct {
	Name     string   `json:"name"`
	Tier     int      `json:"tier"`
	Parent   string   `json:"parent,omitempty"`
	Children []string `json:"children,omitempty"`
	Members  []string `json:"members,omitempty"` // node names, leaves only
}

// CronJob is a MiniVolcano simulation object inspired by
// batch.volcano.sh/v1alpha1 CronJob (v1.13+).
type CronJob struct {
	Name              string   `json:"name"`
	Namespace         string   `json:"namespace"`
	ScheduleTicks     int64    `json:"scheduleTicks"`
	ConcurrencyPolicy string   `json:"concurrencyPolicy,omitempty"` // Allow, Forbid, Replace
	HistoryLimit      int      `json:"historyLimit,omitempty"`
	Spec              JobSpec  `json:"spec"`
	NextTick          int64    `json:"nextTick"`
	Created           []string `json:"created,omitempty"`
	LastJob           string   `json:"lastJob,omitempty"`
}

// JobFlowStep is one node of a tiny DAG, the analogue of Volcano JobFlow.
type JobFlowStep struct {
	Name      string   `json:"name"`
	DependsOn []string `json:"dependsOn,omitempty"`
	Spec      JobSpec  `json:"spec"`
}

type JobFlow struct {
	Name      string            `json:"name"`
	Namespace string            `json:"namespace,omitempty"`
	Steps     []JobFlowStep     `json:"steps"`
	Created   map[string]string `json:"created,omitempty"`
}

// Event is the scheduler/controller audit trail shown by the CLI.
type Event struct {
	Tick    int64  `json:"tick"`
	Type    string `json:"type"`
	Job     string `json:"job,omitempty"`
	Pod     string `json:"pod,omitempty"`
	Node    string `json:"node,omitempty"`
	Queue   string `json:"queue,omitempty"`
	Message string `json:"message,omitempty"`
}

func (e Event) String() string {
	parts := []string{fmt.Sprintf("t=%d", e.Tick), e.Type}
	if e.Queue != "" {
		parts = append(parts, "queue="+e.Queue)
	}
	if e.Job != "" {
		parts = append(parts, "job="+e.Job)
	}
	if e.Pod != "" {
		parts = append(parts, "pod="+e.Pod)
	}
	if e.Node != "" {
		parts = append(parts, "node="+e.Node)
	}
	if e.Message != "" {
		parts = append(parts, e.Message)
	}
	return strings.Join(parts, " ")
}

// ---------------------------------------------------------------------------
// Deep copy helpers
// ---------------------------------------------------------------------------

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

func cloneStringSlice(in []string) []string {
	if in == nil {
		return nil
	}
	return append([]string(nil), in...)
}

func cloneTolerations(in []Toleration) []Toleration {
	if in == nil {
		return nil
	}
	return append([]Toleration(nil), in...)
}

func cloneTaints(in []Taint) []Taint {
	if in == nil {
		return nil
	}
	return append([]Taint(nil), in...)
}

func cloneNUMANodes(in []NUMANode) []NUMANode {
	if in == nil {
		return nil
	}
	out := make([]NUMANode, len(in))
	for i := range in {
		out[i] = in[i]
		out[i].Allocatable = in[i].Allocatable.Clone()
		out[i].Allocated = in[i].Allocated.Clone()
	}
	return out
}

func (n *Node) Clone() *Node {
	if n == nil {
		return nil
	}
	out := *n
	out.Allocatable = n.Allocatable.Clone()
	out.Allocated = n.Allocated.Clone()
	out.Labels = cloneStringMap(n.Labels)
	out.Taints = cloneTaints(n.Taints)
	out.NUMANodes = cloneNUMANodes(n.NUMANodes)
	return &out
}

func cloneResourceOrNil(in Resource) Resource {
	if in == nil {
		return nil
	}
	return in.Clone()
}

func (q *Queue) Clone() *Queue {
	if q == nil {
		return nil
	}
	out := *q
	out.Spec.Capability = cloneResourceOrNil(q.Spec.Capability)
	out.Spec.Deserved = cloneResourceOrNil(q.Spec.Deserved)
	out.Spec.Guarantee = cloneResourceOrNil(q.Spec.Guarantee)
	if q.Spec.Reclaimable != nil {
		v := *q.Spec.Reclaimable
		out.Spec.Reclaimable = &v
	}
	if q.Spec.NodeGroup != nil {
		ng := &NodeGroupSpec{Names: cloneStringSlice(q.Spec.NodeGroup.Names)}
		if q.Spec.NodeGroup.Capability != nil {
			ng.Capability = make(map[string]Resource, len(q.Spec.NodeGroup.Capability))
			for group, cap := range q.Spec.NodeGroup.Capability {
				ng.Capability[group] = cap.Clone()
			}
		}
		out.Spec.NodeGroup = ng
	}
	out.Status.Allocated = q.Status.Allocated.Clone()
	out.Status.Deserved = q.Status.Deserved.Clone()
	return &out
}

func cloneTaskSpecs(in []TaskSpec) []TaskSpec {
	if in == nil {
		return nil
	}
	out := make([]TaskSpec, len(in))
	for i := range in {
		out[i] = in[i]
		if in[i].MinAvailable != nil {
			v := *in[i].MinAvailable
			out[i].MinAvailable = &v
		}
		out[i].Requests = in[i].Requests.Clone()
		out[i].NodeSelector = cloneStringMap(in[i].NodeSelector)
		out[i].Tolerations = cloneTolerations(in[i].Tolerations)
		out[i].Labels = cloneStringMap(in[i].Labels)
		out[i].Annotations = cloneStringMap(in[i].Annotations)
		out[i].Policies = clonePolicies(in[i].Policies)
	}
	return out
}

func clonePolicies(in []LifecyclePolicy) []LifecyclePolicy {
	if in == nil {
		return nil
	}
	return append([]LifecyclePolicy(nil), in...)
}

func cloneJobSpec(in JobSpec) JobSpec {
	out := in
	if in.MinSuccess != nil {
		v := *in.MinSuccess
		out.MinSuccess = &v
	}
	out.Tasks = cloneTaskSpecs(in.Tasks)
	out.Policies = clonePolicies(in.Policies)
	out.Labels = cloneStringMap(in.Labels)
	out.Annotations = cloneStringMap(in.Annotations)
	if in.Plugins != nil {
		out.Plugins = make(map[string][]string, len(in.Plugins))
		for k, v := range in.Plugins {
			out.Plugins[k] = cloneStringSlice(v)
		}
	}
	if in.TaskTopology != nil {
		out.TaskTopology = make([]TaskTopologySpec, len(in.TaskTopology))
		for i, t := range in.TaskTopology {
			out.TaskTopology[i] = TaskTopologySpec{TaskNames: cloneStringSlice(t.TaskNames), Policy: t.Policy}
		}
	}
	if in.NetworkTopology != nil {
		nt := *in.NetworkTopology
		out.NetworkTopology = &nt
	}
	return out
}

func (j *Job) Clone() *Job {
	if j == nil {
		return nil
	}
	out := *j
	out.Spec = cloneJobSpec(j.Spec)
	out.Status.Conditions = append([]Condition(nil), j.Status.Conditions...)
	return &out
}

func (p *Pod) Clone() *Pod {
	if p == nil {
		return nil
	}
	out := *p
	out.Requests = p.Requests.Clone()
	out.Labels = cloneStringMap(p.Labels)
	out.NodeSelector = cloneStringMap(p.NodeSelector)
	out.Tolerations = cloneTolerations(p.Tolerations)
	return &out
}

func (pg *PodGroup) Clone() *PodGroup {
	if pg == nil {
		return nil
	}
	out := *pg
	out.MinResources = pg.MinResources.Clone()
	out.MinTaskMember = make(map[string]int32, len(pg.MinTaskMember))
	for task, n := range pg.MinTaskMember {
		out.MinTaskMember[task] = n
	}
	if len(out.MinTaskMember) == 0 {
		out.MinTaskMember = nil
	}
	out.Labels = cloneStringMap(pg.Labels)
	out.TopologyAffinity = pg.TopologyAffinity.Clone()
	return &out
}

func (t *TopologyAffinitySpec) Clone() *TopologyAffinitySpec {
	if t == nil {
		return nil
	}
	out := &TopologyAffinitySpec{}
	out.PodGroupAntiAffinity = make([]PodGroupAntiAffinityTerm, len(t.PodGroupAntiAffinity))
	for i, term := range t.PodGroupAntiAffinity {
		out.PodGroupAntiAffinity[i] = term
		out.PodGroupAntiAffinity[i].MatchLabels = cloneStringMap(term.MatchLabels)
	}
	return out
}

func (r *Reservation) Clone() *Reservation {
	if r == nil {
		return nil
	}
	out := *r
	out.Resources = r.Resources.Clone()
	out.NodeNames = cloneStringSlice(r.NodeNames)
	return &out
}

func (h *HyperNode) Clone() *HyperNode {
	if h == nil {
		return nil
	}
	out := *h
	out.Children = cloneStringSlice(h.Children)
	out.Members = cloneStringSlice(h.Members)
	return &out
}

func (c *CronJob) Clone() *CronJob {
	if c == nil {
		return nil
	}
	out := *c
	out.Spec = cloneJobSpec(c.Spec)
	out.Created = cloneStringSlice(c.Created)
	return &out
}

func (f *JobFlow) Clone() *JobFlow {
	if f == nil {
		return nil
	}
	out := *f
	out.Steps = make([]JobFlowStep, len(f.Steps))
	for i, s := range f.Steps {
		out.Steps[i] = JobFlowStep{Name: s.Name, DependsOn: cloneStringSlice(s.DependsOn), Spec: cloneJobSpec(s.Spec)}
	}
	out.Created = cloneStringMap(f.Created)
	return &out
}

// SortJobs provides a deterministic fallback order: creation time, namespace,
// name. Plugins normally override this through JobOrderFn.
func SortJobs(jobs []*Job) {
	sort.SliceStable(jobs, func(i, j int) bool {
		if jobs[i].CreationTick != jobs[j].CreationTick {
			return jobs[i].CreationTick < jobs[j].CreationTick
		}
		return jobs[i].Key() < jobs[j].Key()
	})
}

func DurationOr(v, fallback int64) int64 {
	if v > 0 {
		return v
	}
	return fallback
}
