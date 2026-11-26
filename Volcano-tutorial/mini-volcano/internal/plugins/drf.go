package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// DRF implements Dominant Resource Fairness. The share of a job is the maximum
// of (demand / cluster capacity) over all dimensions. Jobs with a smaller
// dominant share are scheduled first.
//
// Demand includes currently allocated resources plus the requests of pending
// pods. This is the quantity that actually competes for the next allocation;
// using allocated resources alone would make every fresh job look identical.
type DRF struct{}

func newDRF(args map[string]string) (framework.Plugin, error) { return &DRF{}, nil }

func (d *DRF) Name() string { return "drf" }

func (d *DRF) OnSessionOpen(ssn *framework.Session) {
	total := ssn.TotalResource
	demand := func(job *api.Job) api.Resource {
		out := ssn.Cache.JobAllocated(job.UID)
		for _, p := range ssn.PendingPods(job.UID) {
			out = out.Add(p.Requests)
		}
		return out
	}
	share := func(res api.Resource) float64 {
		_, s := res.Dominant(total)
		return s
	}

	ssn.AddJobOrderFn("drf", func(l, r *api.Job) int {
		ls, rs := share(demand(l)), share(demand(r))
		if ls < rs {
			return -1
		}
		if ls > rs {
			return 1
		}
		return 0
	})

	ssn.AddTaskOrderFn("drf", func(job *api.Job, l, r *api.Pod) int {
		base := ssn.Cache.JobAllocated(job.UID)
		ls := share(base.Add(l.Requests))
		rs := share(base.Add(r.Requests))
		if ls < rs {
			return -1
		}
		if ls > rs {
			return 1
		}
		return 0
	})
}

func (d *DRF) OnSessionClose(ssn *framework.Session) {}
