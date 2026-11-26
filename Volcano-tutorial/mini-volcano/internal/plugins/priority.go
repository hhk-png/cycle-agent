package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Priority orders jobs and tasks by numeric priority. Volcano resolves a
// PriorityClassName to an integer through the pod's priorityClass; MiniVolcano
// performs that resolution when the job is submitted.
type Priority struct{}

func newPriority(args map[string]string) (framework.Plugin, error) { return &Priority{}, nil }

func (p *Priority) Name() string { return "priority" }

func (p *Priority) OnSessionOpen(ssn *framework.Session) {
	ssn.AddJobOrderFn("priority", func(l, r *api.Job) int {
		if l.Spec.Priority > r.Spec.Priority {
			return -1
		}
		if l.Spec.Priority < r.Spec.Priority {
			return 1
		}
		return 0
	})
	ssn.AddTaskOrderFn("priority", func(job *api.Job, l, r *api.Pod) int {
		if l.Priority > r.Priority {
			return -1
		}
		if l.Priority < r.Priority {
			return 1
		}
		return 0
	})
	ssn.AddPreemptableFn("priority", func(preemptor, victim *api.Pod) bool {
		return preemptor.Priority > victim.Priority
	})
}

func (p *Priority) OnSessionClose(ssn *framework.Session) {}
