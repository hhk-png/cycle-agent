package plugins

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Conformance rejects workloads that are not managed by Volcano. In a real
// cluster the scheduler only sees pods whose schedulerName is volcano; the
// check is still useful as a guard against accidental admission.
type Conformance struct{}

func newConformance(args map[string]string) (framework.Plugin, error) {
	return &Conformance{}, nil
}

func (c *Conformance) Name() string { return "conformance" }

func (c *Conformance) OnSessionOpen(ssn *framework.Session) {
	ssn.AddJobValidFn("conformance", func(job *api.Job) error {
		if job.Spec.SchedulerName != "volcano" {
			return fmt.Errorf("job %s uses schedulerName %q, not volcano", job.Key(), job.Spec.SchedulerName)
		}
		if ssn.Cache.PodGroups[job.UID] == nil {
			return fmt.Errorf("job %s has no PodGroup", job.Key())
		}
		return nil
	})
}

func (c *Conformance) OnSessionClose(ssn *framework.Session) {}
