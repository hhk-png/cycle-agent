package plugins

import (
	"math"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Proportion divides cluster resources among queues by weight. It is the
// flat-queue counterpart of the hierarchical capacity plugin; Volcano's
// documentation treats them as alternatives and so does MiniVolcano.
//
// deserved_i = total * weight_i / sum(weight)
//
// deserved is then clamped by capability and lifted to guarantee. The guarantee
// floor is intentionally not re-balanced across other queues: a cluster whose
// guarantees exceed its capacity is a configuration error that should be
// visible, not silently normalised.
type Proportion struct {
	enablePreemptable bool
}

func newProportion(args map[string]string) (framework.Plugin, error) {
	pre, err := boolArg(args, "enablePreemptable", true)
	if err != nil {
		return nil, err
	}
	return &Proportion{enablePreemptable: pre}, nil
}

func (p *Proportion) Name() string { return "proportion" }

func (p *Proportion) OnSessionOpen(ssn *framework.Session) {
	queues := ssn.Queues()
	var totalWeight int64
	for _, q := range queues {
		totalWeight += int64(q.Spec.Weight)
	}
	total := ssn.TotalResource
	for _, q := range queues {
		var deserved api.Resource
		if totalWeight > 0 {
			deserved = total.MulScalar(float64(q.Spec.Weight) / float64(totalWeight))
		} else {
			deserved = api.Resource{}
		}
		if q.Spec.Capability != nil {
			for k, v := range deserved {
				if cap := q.Spec.Capability[k]; cap > 0 && v > cap {
					deserved[k] = cap
				}
			}
		}
		for k, v := range q.Spec.Guarantee {
			if deserved[k] < v {
				deserved[k] = v
			}
		}
		q.Status.Deserved = deserved
	}

	ssn.AddAllocatableFn("proportion", func(q *api.Queue, res api.Resource) bool {
		if q.Spec.Capability == nil {
			return true
		}
		used := ssn.Cache.QueueAllocated(q.Name)
		return used.Add(res).WithinLimits(q.Spec.Capability)
	})

	ssn.AddJobEnqueueableFn("proportion", func(job *api.Job) bool {
		q := ssn.Queue(job.Spec.Queue)
		if q == nil || q.State() != api.QueueOpen {
			return false
		}
		return ssn.Allocatable(q, job.MinResources())
	})

	ssn.AddOverusedFn("proportion", func(q *api.Queue) bool {
		used := ssn.Cache.QueueAllocated(q.Name)
		return !used.LessEqual(q.Status.Deserved)
	})

	ssn.AddReclaimableFn("proportion", func(q *api.Queue, victim *api.Pod) bool {
		if !p.enablePreemptable {
			return false
		}
		used := ssn.Cache.QueueAllocated(q.Name)
		return q.Reclaimable() && !used.LessEqual(q.Status.Deserved)
	})

	ssn.AddQueueOrderFn("proportion-capability", func(l, r *api.Queue) int {
		// A queue with zero deserved capacity must not jump ahead of queues
		// with real shares merely because both currently use nothing.
		ls, rs := ratioOrInf(ssn.Cache.QueueAllocated(l.Name), l.Status.Deserved),
			ratioOrInf(ssn.Cache.QueueAllocated(r.Name), r.Status.Deserved)
		switch {
		case ls < rs:
			return -1
		case ls > rs:
			return 1
		}
		return 0
	})
}

func ratioOrInf(used, cap api.Resource) float64 {
	if used.IsZero() {
		return 0
	}
	if cap.IsZero() {
		return math.Inf(1)
	}
	return shareOf(used, cap)
}

func (p *Proportion) OnSessionClose(ssn *framework.Session) {}
