package plugins

import (
	"math"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// FairShare is inspired by Volcano v1.16's Slurm-style fairshare plugin. It
// tracks exponentially decayed historical usage per queue and orders queues and
// jobs by that history. The default half-life is four scheduling ticks.
type FairShare struct {
	halfLife float64
	usage    map[string]float64
	lastTick int64
	started  bool
}

func newFairShare(args map[string]string) (framework.Plugin, error) {
	hl, err := floatArg(args, "halfLife", 4)
	if err != nil {
		return nil, err
	}
	if hl <= 0 {
		return nil, &argError{key: "halfLife", value: "0", want: "a positive number"}
	}
	return &FairShare{halfLife: hl, usage: map[string]float64{}}, nil
}

func (f *FairShare) Name() string { return "fairshare" }

func (f *FairShare) OnSessionOpen(ssn *framework.Session) {
	if !f.started {
		f.lastTick = ssn.Now
		f.started = true
	}
	dt := ssn.Now - f.lastTick
	if dt > 0 {
		decay := math.Pow(0.5, float64(dt)/f.halfLife)
		for q := range f.usage {
			f.usage[q] *= decay
		}
		for _, q := range ssn.Queues() {
			share := shareOf(ssn.Cache.QueueAllocated(q.Name), ssn.TotalResource)
			f.usage[q.Name] += share * float64(dt)
		}
		f.lastTick = ssn.Now
	}

	ssn.AddQueueOrderFn("fairshare", func(l, r *api.Queue) int {
		lu, ru := f.usage[l.Name], f.usage[r.Name]
		if lu < ru {
			return -1
		}
		if lu > ru {
			return 1
		}
		return 0
	})

	ssn.AddJobOrderFn("fairshare", func(l, r *api.Job) int {
		lu, ru := f.usage[l.Spec.Queue], f.usage[r.Spec.Queue]
		if lu < ru {
			return -1
		}
		if lu > ru {
			return 1
		}
		return 0
	})
}

func (f *FairShare) OnSessionClose(ssn *framework.Session) {}

// Usage exposes the decayed usage ledger for explain/diagnostics.
func (f *FairShare) Usage() map[string]float64 {
	out := make(map[string]float64, len(f.usage))
	for k, v := range f.usage {
		out[k] = v
	}
	return out
}
