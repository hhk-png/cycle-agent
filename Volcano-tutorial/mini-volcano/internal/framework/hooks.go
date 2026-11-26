package framework

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
)

// The hook signatures intentionally mirror Volcano's scheduler framework.
// A plugin registers only the hooks it needs; absent hooks are neutral.

type JobOrderFn func(l, r *api.Job) int
type QueueOrderFn func(l, r *api.Queue) int
type TaskOrderFn func(job *api.Job, l, r *api.Pod) int
type PredicateFn func(pod *api.Pod, node *cache.NodeState) error
type NodeOrderFn func(pod *api.Pod, node *cache.NodeState) (float64, error)
type PreemptableFn func(preemptor, victim *api.Pod) bool
type ReclaimableFn func(queue *api.Queue, victim *api.Pod) bool
type JobReadyFn func(job *api.Job) bool
type JobValidFn func(job *api.Job) error
type JobEnqueueableFn func(job *api.Job) bool
type OverusedFn func(queue *api.Queue) bool
type AllocatableFn func(queue *api.Queue, res api.Resource) bool
type JobStarvingFn func(job *api.Job) bool

// DisruptableFn answers whether a victim may be voluntarily disrupted at all,
// independent of which preemptor is asking. It is the hook shuffle uses; PDB
// protection is not about priority, so a preemptor-only predicate is not
// enough.
type DisruptableFn func(victim *api.Pod) bool

// VictimTasksFn supplies candidate pods for the shuffle action. Volcano models
// this as a per-plugin function set: rescheduling, tdm and pdb each contribute
// victims, and the action consumes the highest tier that returns any.
type VictimTasksFn func() []*api.Pod

type jobOrderHook struct {
	name string
	tier int
	seq  int
	fn   JobOrderFn
}

type queueOrderHook struct {
	name string
	tier int
	seq  int
	fn   QueueOrderFn
}

type taskOrderHook struct {
	name string
	tier int
	seq  int
	fn   TaskOrderFn
}

type predicateHook struct {
	name string
	tier int
	seq  int
	fn   PredicateFn
}

type nodeOrderHook struct {
	name string
	tier int
	seq  int
	fn   NodeOrderFn
}

type preemptableHook struct {
	name string
	tier int
	seq  int
	fn   PreemptableFn
}

type reclaimableHook struct {
	name string
	tier int
	seq  int
	fn   ReclaimableFn
}

type jobReadyHook struct {
	name string
	tier int
	seq  int
	fn   JobReadyFn
}

type jobValidHook struct {
	name string
	tier int
	seq  int
	fn   JobValidFn
}

type jobEnqueueableHook struct {
	name string
	tier int
	seq  int
	fn   JobEnqueueableFn
}

type overusedHook struct {
	name string
	tier int
	seq  int
	fn   OverusedFn
}

type allocatableHook struct {
	name string
	tier int
	seq  int
	fn   AllocatableFn
}

type jobStarvingHook struct {
	name string
	tier int
	seq  int
	fn   JobStarvingFn
}

type victimTasksHook struct {
	name string
	tier int
	seq  int
	fn   VictimTasksFn
}

type disruptableHook struct {
	name string
	tier int
	seq  int
	fn   DisruptableFn
}
