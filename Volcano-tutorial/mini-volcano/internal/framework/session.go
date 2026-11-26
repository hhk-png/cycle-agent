package framework

import (
	"fmt"
	"sort"
	"strings"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
)

// Plugin is the scheduled-side extension point. Plugins are long lived: the
// same instance is reused across sessions, which lets stateful plugins such as
// fairshare keep historical usage between scheduling rounds.
type Plugin interface {
	Name() string
	OnSessionOpen(ssn *Session)
	OnSessionClose(ssn *Session)
}

type PluginInstance struct {
	Plugin Plugin
	Tier   int
	Args   map[string]string
}

// Session is a single scheduling pass over an immutable snapshot of the
// cluster. Actions see one consistent view and may stage changes atomically.
type Session struct {
	Cache         *cache.Cache
	Now           int64
	TotalResource api.Resource
	Config        Config
	Events        []api.Event
	// Action is the action currently executing (allocate, backfill, ...).
	// Plugins that are documented as allocate-only can gate on it. It defaults
	// to "allocate" so direct unit tests that call Predicate/NodeScore without
	// a Scheduler still exercise the common path.
	Action string

	Plugins []PluginInstance

	// Unschedulable is the evidence trail used by -explain.
	Unschedulable map[string][]string

	hookSeq int
	tier    int
	plugin  string

	jobOrderHooks       []jobOrderHook
	queueOrderHooks     []queueOrderHook
	taskOrderHooks      []taskOrderHook
	predicateHooks      []predicateHook
	nodeOrderHooks      []nodeOrderHook
	preemptableHooks    []preemptableHook
	reclaimableHooks    []reclaimableHook
	jobReadyHooks       []jobReadyHook
	jobValidHooks       []jobValidHook
	jobEnqueueableHooks []jobEnqueueableHook
	overusedHooks       []overusedHook
	allocatableHooks    []allocatableHook
	jobStarvingHooks    []jobStarvingHook
	victimTasksHooks    []victimTasksHook
	disruptableHooks    []disruptableHook
}

func NewSession(c *cache.Cache, now int64, cfg Config, plugins []PluginInstance) *Session {
	if c.Now != now {
		c.Now = now
	}
	s := &Session{
		Cache:         c,
		Now:           now,
		TotalResource: c.TotalAllocatable(),
		Config:        cfg,
		Plugins:       plugins,
		Unschedulable: map[string][]string{},
		Action:        "allocate",
	}
	for _, p := range plugins {
		s.tier = p.Tier
		s.plugin = p.Plugin.Name()
		p.Plugin.OnSessionOpen(s)
	}
	s.sortHooks()
	return s
}

func (s *Session) Close() {
	for _, p := range s.Plugins {
		s.tier = p.Tier
		s.plugin = p.Plugin.Name()
		p.Plugin.OnSessionClose(s)
	}
}

func (s *Session) sortHooks() {
	sort.SliceStable(s.jobOrderHooks, func(i, j int) bool {
		return lessHook(s.jobOrderHooks[i].tier, s.jobOrderHooks[i].seq, s.jobOrderHooks[j].tier, s.jobOrderHooks[j].seq)
	})
	sort.SliceStable(s.queueOrderHooks, func(i, j int) bool {
		return lessHook(s.queueOrderHooks[i].tier, s.queueOrderHooks[i].seq, s.queueOrderHooks[j].tier, s.queueOrderHooks[j].seq)
	})
	sort.SliceStable(s.taskOrderHooks, func(i, j int) bool {
		return lessHook(s.taskOrderHooks[i].tier, s.taskOrderHooks[i].seq, s.taskOrderHooks[j].tier, s.taskOrderHooks[j].seq)
	})
	sort.SliceStable(s.predicateHooks, func(i, j int) bool {
		return lessHook(s.predicateHooks[i].tier, s.predicateHooks[i].seq, s.predicateHooks[j].tier, s.predicateHooks[j].seq)
	})
	sort.SliceStable(s.nodeOrderHooks, func(i, j int) bool {
		return lessHook(s.nodeOrderHooks[i].tier, s.nodeOrderHooks[i].seq, s.nodeOrderHooks[j].tier, s.nodeOrderHooks[j].seq)
	})
	sort.SliceStable(s.preemptableHooks, func(i, j int) bool {
		return lessHook(s.preemptableHooks[i].tier, s.preemptableHooks[i].seq, s.preemptableHooks[j].tier, s.preemptableHooks[j].seq)
	})
	sort.SliceStable(s.reclaimableHooks, func(i, j int) bool {
		return lessHook(s.reclaimableHooks[i].tier, s.reclaimableHooks[i].seq, s.reclaimableHooks[j].tier, s.reclaimableHooks[j].seq)
	})
	sort.SliceStable(s.jobReadyHooks, func(i, j int) bool {
		return lessHook(s.jobReadyHooks[i].tier, s.jobReadyHooks[i].seq, s.jobReadyHooks[j].tier, s.jobReadyHooks[j].seq)
	})
	sort.SliceStable(s.jobValidHooks, func(i, j int) bool {
		return lessHook(s.jobValidHooks[i].tier, s.jobValidHooks[i].seq, s.jobValidHooks[j].tier, s.jobValidHooks[j].seq)
	})
	sort.SliceStable(s.jobEnqueueableHooks, func(i, j int) bool {
		return lessHook(s.jobEnqueueableHooks[i].tier, s.jobEnqueueableHooks[i].seq, s.jobEnqueueableHooks[j].tier, s.jobEnqueueableHooks[j].seq)
	})
	sort.SliceStable(s.overusedHooks, func(i, j int) bool {
		return lessHook(s.overusedHooks[i].tier, s.overusedHooks[i].seq, s.overusedHooks[j].tier, s.overusedHooks[j].seq)
	})
	sort.SliceStable(s.allocatableHooks, func(i, j int) bool {
		return lessHook(s.allocatableHooks[i].tier, s.allocatableHooks[i].seq, s.allocatableHooks[j].tier, s.allocatableHooks[j].seq)
	})
	sort.SliceStable(s.jobStarvingHooks, func(i, j int) bool {
		return lessHook(s.jobStarvingHooks[i].tier, s.jobStarvingHooks[i].seq, s.jobStarvingHooks[j].tier, s.jobStarvingHooks[j].seq)
	})
	sort.SliceStable(s.victimTasksHooks, func(i, j int) bool {
		return lessHook(s.victimTasksHooks[i].tier, s.victimTasksHooks[i].seq, s.victimTasksHooks[j].tier, s.victimTasksHooks[j].seq)
	})
	sort.SliceStable(s.disruptableHooks, func(i, j int) bool {
		return lessHook(s.disruptableHooks[i].tier, s.disruptableHooks[i].seq, s.disruptableHooks[j].tier, s.disruptableHooks[j].seq)
	})
}

func lessHook(t1, s1, t2, s2 int) bool {
	if t1 != t2 {
		return t1 < t2
	}
	return s1 < s2
}

// ---------------------------------------------------------------------------
// Hook registration
// ---------------------------------------------------------------------------

func (s *Session) nextSeq() int { s.hookSeq++; return s.hookSeq }

func (s *Session) AddJobOrderFn(name string, fn JobOrderFn) {
	s.jobOrderHooks = append(s.jobOrderHooks, jobOrderHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddQueueOrderFn(name string, fn QueueOrderFn) {
	s.queueOrderHooks = append(s.queueOrderHooks, queueOrderHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddTaskOrderFn(name string, fn TaskOrderFn) {
	s.taskOrderHooks = append(s.taskOrderHooks, taskOrderHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddPredicateFn(name string, fn PredicateFn) {
	s.predicateHooks = append(s.predicateHooks, predicateHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddNodeOrderFn(name string, fn NodeOrderFn) {
	s.nodeOrderHooks = append(s.nodeOrderHooks, nodeOrderHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddPreemptableFn(name string, fn PreemptableFn) {
	s.preemptableHooks = append(s.preemptableHooks, preemptableHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddReclaimableFn(name string, fn ReclaimableFn) {
	s.reclaimableHooks = append(s.reclaimableHooks, reclaimableHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddJobReadyFn(name string, fn JobReadyFn) {
	s.jobReadyHooks = append(s.jobReadyHooks, jobReadyHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddJobValidFn(name string, fn JobValidFn) {
	s.jobValidHooks = append(s.jobValidHooks, jobValidHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddJobEnqueueableFn(name string, fn JobEnqueueableFn) {
	s.jobEnqueueableHooks = append(s.jobEnqueueableHooks, jobEnqueueableHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddOverusedFn(name string, fn OverusedFn) {
	s.overusedHooks = append(s.overusedHooks, overusedHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddAllocatableFn(name string, fn AllocatableFn) {
	s.allocatableHooks = append(s.allocatableHooks, allocatableHook{s.plugin, s.tier, s.nextSeq(), fn})
}
func (s *Session) AddJobStarvingFn(name string, fn JobStarvingFn) {
	s.jobStarvingHooks = append(s.jobStarvingHooks, jobStarvingHook{s.plugin, s.tier, s.nextSeq(), fn})
}

// AddVictimTasksFns registers one or more victim suppliers for shuffle. A
// plugin may register several strategies; Volcano keeps them as a set and lets
// the action short-circuit at the first tier that returns victims.
func (s *Session) AddVictimTasksFns(name string, fns ...VictimTasksFn) {
	for _, fn := range fns {
		s.victimTasksHooks = append(s.victimTasksHooks, victimTasksHook{s.plugin, s.tier, s.nextSeq(), fn})
	}
}

func (s *Session) AddDisruptableFn(name string, fn DisruptableFn) {
	s.disruptableHooks = append(s.disruptableHooks, disruptableHook{s.plugin, s.tier, s.nextSeq(), fn})
}

// ---------------------------------------------------------------------------
// Object accessors
// ---------------------------------------------------------------------------

func (s *Session) JobByUID(uid string) *api.Job      { return s.Cache.Jobs[uid] }
func (s *Session) PodByUID(uid string) *api.Pod      { return s.Cache.Pods[uid] }
func (s *Session) Queue(name string) *api.Queue      { return s.Cache.Queues[name] }
func (s *Session) Node(name string) *cache.NodeState { return s.Cache.Nodes[name] }

func (s *Session) Jobs() []*api.Job { return s.Cache.JobsSorted() }

func (s *Session) JobsOfQueue(queue string) []*api.Job {
	var out []*api.Job
	for _, j := range s.Cache.Jobs {
		if j.Spec.Queue == queue {
			out = append(out, j)
		}
	}
	api.SortJobs(out)
	return out
}

func (s *Session) Queues() []*api.Queue { return s.Cache.QueuesSorted() }

func (s *Session) Nodes() []*cache.NodeState { return s.Cache.NodesSorted() }

func (s *Session) JobPods(jobUID string) []*api.Pod { return s.Cache.JobPods(jobUID) }

func (s *Session) PendingPods(jobUID string) []*api.Pod { return s.Cache.PendingPods(jobUID) }

func (s *Session) ReadyCount(jobUID string) int { return s.Cache.ReadyCount(jobUID) }

func (s *Session) RunningCount(jobUID string) int { return s.Cache.RunningCount(jobUID) }

func (s *Session) QueueAllocated(queue string) api.Resource {
	return s.Cache.QueueAllocated(queue)
}

func (s *Session) QueueOpen(queue string) bool {
	q := s.Cache.Queues[queue]
	return q != nil && q.State() == api.QueueOpen
}

func (s *Session) AddEvent(e api.Event) {
	if e.Tick == 0 {
		e.Tick = s.Now
	}
	s.Events = append(s.Events, e)
}

func (s *Session) RecordUnschedulable(jobUID, reason string) {
	if reason == "" {
		return
	}
	for _, r := range s.Unschedulable[jobUID] {
		if r == reason {
			return
		}
	}
	s.Unschedulable[jobUID] = append(s.Unschedulable[jobUID], reason)
}

// ---------------------------------------------------------------------------
// Hook evaluation
// ---------------------------------------------------------------------------

func (s *Session) QueueOrder(l, r *api.Queue) int {
	for _, h := range s.queueOrderHooks {
		if v := h.fn(l, r); v != 0 {
			return v
		}
	}
	return strings.Compare(l.Name, r.Name)
}

func (s *Session) JobOrder(l, r *api.Job) int {
	for _, h := range s.jobOrderHooks {
		if v := h.fn(l, r); v != 0 {
			return v
		}
	}
	if l.CreationTick != r.CreationTick {
		if l.CreationTick < r.CreationTick {
			return -1
		}
		return 1
	}
	return strings.Compare(l.Key(), r.Key())
}

func (s *Session) TaskOrder(job *api.Job, l, r *api.Pod) int {
	for _, h := range s.taskOrderHooks {
		if v := h.fn(job, l, r); v != 0 {
			return v
		}
	}
	if l.TaskIndex != r.TaskIndex {
		if l.TaskIndex < r.TaskIndex {
			return -1
		}
		return 1
	}
	return strings.Compare(l.Name, r.Name)
}

func (s *Session) JobValid(job *api.Job) error {
	if len(s.jobValidHooks) == 0 {
		if job.Spec.MinAvailable > job.TotalReplicas() {
			return fmt.Errorf("minAvailable %d exceeds total replicas %d", job.Spec.MinAvailable, job.TotalReplicas())
		}
	}
	for _, h := range s.jobValidHooks {
		if err := h.fn(job); err != nil {
			return err
		}
	}
	return nil
}

func (s *Session) JobReady(job *api.Job) bool {
	if len(s.jobReadyHooks) == 0 {
		return s.Cache.ReadyCount(job.UID) >= int(job.Spec.MinAvailable)
	}
	for _, h := range s.jobReadyHooks {
		if !h.fn(job) {
			return false
		}
	}
	return true
}

func (s *Session) JobEnqueueable(job *api.Job) bool {
	for _, h := range s.jobEnqueueableHooks {
		if !h.fn(job) {
			return false
		}
	}
	return true
}

func (s *Session) JobStarving(job *api.Job) bool {
	if len(s.jobStarvingHooks) == 0 {
		// No plugin claims the job is starving. This matches the plugin model:
		// an absent hook is not a default policy, and preempt/reclaim should
		// not invent one.
		return false
	}
	for _, h := range s.jobStarvingHooks {
		if !h.fn(job) {
			return false
		}
	}
	return true
}

// Predicate runs the built-in fit check first, then all plugin predicates.
func (s *Session) Predicate(pod *api.Pod, node *cache.NodeState) error {
	if node == nil || node.Node == nil {
		return fmt.Errorf("node is nil")
	}
	if node.Node.Unschedulable {
		return fmt.Errorf("node %s is unschedulable", node.Node.Name)
	}
	idle := s.Cache.IdleFor(node.Node.Name, pod.Queue, pod.Job, s.Now)
	if !pod.Requests.LessEqual(idle) {
		// Show a zero for every dimension the pod requested. Printing only
		// non-zero idle dimensions makes "need gpu=1, idle cpu=8" look like a
		// contradiction when the real shortage is the omitted GPU dimension.
		shown := make(api.Resource, len(idle)+len(pod.Requests))
		for k, v := range idle {
			shown[k] = v
		}
		for k := range pod.Requests {
			if _, ok := shown[k]; !ok {
				shown[k] = 0
			}
		}
		return fmt.Errorf("node %s has insufficient idle resources (need %s, idle %s)",
			node.Node.Name, pod.Requests, shown.StringAll())
	}
	for _, h := range s.predicateHooks {
		if err := h.fn(pod, node); err != nil {
			return err
		}
	}
	return nil
}

func (s *Session) NodeScore(pod *api.Pod, node *cache.NodeState) (float64, error) {
	var total float64
	for _, h := range s.nodeOrderHooks {
		v, err := h.fn(pod, node)
		if err != nil {
			return 0, err
		}
		total += v
	}
	return total, nil
}

func (s *Session) Preemptable(preemptor, victim *api.Pod) bool {
	if preemptor == nil || victim == nil {
		return false
	}
	if preemptor.Job == victim.Job {
		return false
	}
	if victim.Priority >= preemptor.Priority {
		return false
	}
	for _, h := range s.preemptableHooks {
		if !h.fn(preemptor, victim) {
			return false
		}
	}
	return true
}

// Disruptable applies PDB-style protection to shuffle victims. It is separate
// from Preemptable because shuffle has no preemptor; the question is only
// whether the victim may be evicted at all.
func (s *Session) Disruptable(victim *api.Pod) bool {
	if victim == nil {
		return false
	}
	for _, h := range s.disruptableHooks {
		if !h.fn(victim) {
			return false
		}
	}
	return true
}

func (s *Session) Reclaimable(queue *api.Queue, victim *api.Pod) bool {
	if queue == nil {
		return false
	}
	if !queue.Reclaimable() {
		return false
	}
	for _, h := range s.reclaimableHooks {
		if !h.fn(queue, victim) {
			return false
		}
	}
	return true
}

func (s *Session) Overused(queue *api.Queue) bool {
	for _, h := range s.overusedHooks {
		if h.fn(queue) {
			return true
		}
	}
	return false
}

// VictimTasks collects shuffle candidates with tier short-circuiting. All
// functions in the first tier that produces any victims are merged, matching
// Volcano's "one tier wins" rule. Results are deduplicated by pod UID and
// sorted so shuffle is deterministic.
func (s *Session) VictimTasks() []*api.Pod {
	if len(s.victimTasksHooks) == 0 {
		return nil
	}
	for i := 0; i < len(s.victimTasksHooks); {
		tier := s.victimTasksHooks[i].tier
		var out []*api.Pod
		seen := map[string]bool{}
		for i < len(s.victimTasksHooks) && s.victimTasksHooks[i].tier == tier {
			for _, p := range s.victimTasksHooks[i].fn() {
				if p == nil || seen[p.UID] {
					continue
				}
				seen[p.UID] = true
				out = append(out, p)
			}
			i++
		}
		if len(out) > 0 {
			protected := out[:0]
			for _, p := range out {
				if s.Disruptable(p) {
					protected = append(protected, p)
				}
			}
			if len(protected) == 0 {
				continue
			}
			sort.SliceStable(protected, func(a, b int) bool {
				if protected[a].Name != protected[b].Name {
					return protected[a].Name < protected[b].Name
				}
				return protected[a].UID < protected[b].UID
			})
			return protected
		}
	}
	return nil
}

func (s *Session) Allocatable(queue *api.Queue, res api.Resource) bool {
	for _, h := range s.allocatableHooks {
		if !h.fn(queue, res) {
			return false
		}
	}
	return true
}

// UnschedulableReasons returns a copy of the evidence recorded for a job.
func (s *Session) UnschedulableReasons(jobUID string) []string {
	return append([]string(nil), s.Unschedulable[jobUID]...)
}
