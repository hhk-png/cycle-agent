package plugins

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Gang implements Volcano's all-or-nothing scheduling contract.
//
//	minAvailable = 0        -> gang disabled
//	running+allocated >= minAvailable -> the job may be marked Running
//
// The actual rollback is performed by the allocate action through a
// framework.Statement; this plugin only answers readiness questions.
type Gang struct {
	enablePreemptable bool
	enableJobStarving bool
}

func newGang(args map[string]string) (framework.Plugin, error) {
	pre, err := boolArg(args, "enablePreemptable", true)
	if err != nil {
		return nil, err
	}
	starving, err := boolArg(args, "enableJobStarving", true)
	if err != nil {
		return nil, err
	}
	return &Gang{enablePreemptable: pre, enableJobStarving: starving}, nil
}

func (g *Gang) Name() string { return "gang" }

func (g *Gang) OnSessionOpen(ssn *framework.Session) {
	ssn.AddJobValidFn("gang", func(job *api.Job) error {
		if job.Spec.MinAvailable < 0 {
			return fmt.Errorf("minAvailable must be >= 0")
		}
		if job.Spec.MinAvailable > job.TotalReplicas() {
			return fmt.Errorf("minAvailable %d exceeds total replicas %d", job.Spec.MinAvailable, job.TotalReplicas())
		}
		for i := range job.Spec.Tasks {
			t := &job.Spec.Tasks[i]
			if t.MinAvailable != nil && *t.MinAvailable > t.Replicas {
				return fmt.Errorf("task %s: minAvailable %d exceeds replicas %d", t.Name, *t.MinAvailable, t.Replicas)
			}
		}
		return nil
	})

	ssn.AddJobReadyFn("gang", func(job *api.Job) bool {
		if job.Spec.MinAvailable <= 0 {
			// Explicit 0 disables gang in MiniVolcano; there is no gate to
			// satisfy, so the job is ready by definition.
			return true
		}
		if ssn.ReadyCount(job.UID) < int(job.Spec.MinAvailable) {
			return false
		}
		if !job.RoleGatesActive() {
			return true
		}
		byTask := ssn.Cache.ReadyCountByTask(job.UID)
		for i := range job.Spec.Tasks {
			t := &job.Spec.Tasks[i]
			if want := t.EffectiveMinAvailable(); want > 0 && byTask[t.Name] < int(want) {
				return false
			}
		}
		return true
	})

	if g.enableJobStarving {
		ssn.AddJobStarvingFn("gang", func(job *api.Job) bool {
			if job.Spec.MinAvailable <= 0 {
				return ssn.RunningCount(job.UID) == 0
			}
			if ssn.RunningCount(job.UID) < int(job.Spec.MinAvailable) {
				return true
			}
			if !job.RoleGatesActive() {
				return false
			}
			byTask := ssn.Cache.RunningCountByTask(job.UID)
			for i := range job.Spec.Tasks {
				t := &job.Spec.Tasks[i]
				if want := t.EffectiveMinAvailable(); want > 0 && byTask[t.Name] < int(want) {
					return true
				}
			}
			return false
		})
	}

	ssn.AddTaskOrderFn("gang", func(job *api.Job, l, r *api.Pod) int {
		// Unscheduled tasks first.
		lReady, rReady := l.Phase.ReadyForGang(), r.Phase.ReadyForGang()
		if lReady != rReady {
			if !lReady {
				return -1
			}
			return 1
		}
		// A role gate with a small quota is the scarce resource: schedule the
		// single master before filling eight workers, otherwise a capacity
		// that exactly fits minAvailable can spend every slot on workers and
		// roll back forever. Gated roles come before ungated ones; among
		// gated roles, smaller quotas come first.
		lg, rg := l.Task, r.Task
		lWant, rWant := int32(0), int32(0)
		lIdx, rIdx := len(job.Spec.Tasks), len(job.Spec.Tasks)
		for i := range job.Spec.Tasks {
			t := &job.Spec.Tasks[i]
			if t.Name == lg {
				lWant, lIdx = t.EffectiveMinAvailable(), i
			}
			if t.Name == rg {
				rWant, rIdx = t.EffectiveMinAvailable(), i
			}
		}
		if (lWant > 0) != (rWant > 0) {
			if lWant > 0 {
				return -1
			}
			return 1
		}
		if lWant != rWant {
			if lWant < rWant {
				return -1
			}
			return 1
		}
		if lIdx != rIdx {
			return lIdx - rIdx
		}
		if l.TaskIndex != r.TaskIndex {
			if l.TaskIndex < r.TaskIndex {
				return -1
			}
			return 1
		}
		return 0
	})

	ssn.AddPreemptableFn("gang", func(preemptor, victim *api.Pod) bool {
		if !g.enablePreemptable {
			return false
		}
		if preemptor.Job == victim.Job {
			return false
		}
		// Volcano's gang plugin prefers preempting victims that are not yet
		// contributing to their own job's gang. MiniVolcano allows both but
		// keeps the priority check in the framework.
		return true
	})
}

func (g *Gang) OnSessionClose(ssn *framework.Session) {}
