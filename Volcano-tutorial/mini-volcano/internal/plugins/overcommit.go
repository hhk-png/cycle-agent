package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Overcommit lets enqueue admit work against total*factor rather than current
// idle resources. Volcano's enqueue action has an overcommit-factor knob with a
// default of 1.2; making it a plugin keeps the action policy-free.
type Overcommit struct {
	factor float64
}

func newOvercommit(args map[string]string) (framework.Plugin, error) {
	f, err := floatArg(args, "overcommit-factor", 1.2)
	if err != nil {
		return nil, err
	}
	if f <= 0 {
		return nil, &argError{key: "overcommit-factor", value: "0", want: "a positive number"}
	}
	return &Overcommit{factor: f}, nil
}

func (o *Overcommit) Name() string { return "overcommit" }

func (o *Overcommit) OnSessionOpen(ssn *framework.Session) {
	ssn.AddJobEnqueueableFn("overcommit", func(job *api.Job) bool {
		budget := ssn.TotalResource.MulScalar(o.factor).Sub(ssn.Cache.TotalAllocated())
		return job.MinResources().LessEqual(budget)
	})
}

func (o *Overcommit) OnSessionClose(ssn *framework.Session) {}
