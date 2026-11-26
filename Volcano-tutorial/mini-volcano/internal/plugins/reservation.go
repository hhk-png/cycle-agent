package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Reservation protects capacity for a queue or a specific job during a tick
// window. The actual capacity subtraction lives in cache.IdleFor; this plugin
// adds scheduling priorities and activation bookkeeping.
type Reservation struct{}

func newReservation(args map[string]string) (framework.Plugin, error) {
	return &Reservation{}, nil
}

func (r *Reservation) Name() string { return "reservation" }

func (r *Reservation) OnSessionOpen(ssn *framework.Session) {
	reservedJobs := map[string]bool{}
	for _, res := range ssn.Cache.Reservations {
		res.Active = res.ActiveAt(ssn.Now)
		if res.Active && res.Job != "" {
			reservedJobs[res.Job] = true
		}
	}

	ssn.AddJobOrderFn("reservation", func(l, r *api.Job) int {
		ls, rs := reservedJobs[l.UID], reservedJobs[r.UID]
		if ls == rs {
			return 0
		}
		if ls {
			return -1
		}
		return 1
	})

	ssn.AddJobEnqueueableFn("reservation", func(job *api.Job) bool {
		q := ssn.Queue(job.Spec.Queue)
		return q != nil && q.State() == api.QueueOpen
	})
}

func (r *Reservation) OnSessionClose(ssn *framework.Session) {}
