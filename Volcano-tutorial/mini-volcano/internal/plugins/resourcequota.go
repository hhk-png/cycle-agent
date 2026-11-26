package plugins

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// ResourceQuota gates enqueue on namespace quotas. Real Volcano's
// resourcequota plugin consults the namespace ResourceQuota objects, while the
// API Server has already rejected over-quota pods. MiniVolcano enforces the
// check one level earlier so the scheduler can explain the rejection.
//
// Usage counts only jobs that have been admitted (Inqueue/Running/...), not
// Pending jobs: otherwise every submitted job would inflate the usage of every
// other job, and admission order would change the result.
type ResourceQuota struct{}

func newResourceQuota(args map[string]string) (framework.Plugin, error) {
	return &ResourceQuota{}, nil
}

func (p *ResourceQuota) Name() string { return "resourcequota" }

func (p *ResourceQuota) OnSessionOpen(ssn *framework.Session) {
	ssn.AddJobEnqueueableFn("resourcequota", func(job *api.Job) bool {
		quotas := quotasFor(ssn, job.Namespace)
		if len(quotas) == 0 {
			return true
		}
		used := ssn.Cache.NamespaceAdmittedUsage(job.Namespace)
		need := job.MinResources()
		for _, quota := range quotas {
			if used.Add(need).WithinLimits(quota.Hard) {
				continue
			}
			ssn.RecordUnschedulable(job.UID, fmt.Sprintf(
				"namespace quota %s exceeded: used %s + minResources %s > hard %s",
				quota.Name, used, need, quota.Hard))
			return false
		}
		return true
	})
}

func (p *ResourceQuota) OnSessionClose(ssn *framework.Session) {}

func quotasFor(ssn *framework.Session, namespace string) []*api.ResourceQuota {
	var out []*api.ResourceQuota
	for _, q := range ssn.Cache.ResourceQuotas {
		if q.Namespace == namespace {
			out = append(out, q)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}
