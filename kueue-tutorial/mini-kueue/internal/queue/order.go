// Package queue implements the deterministic workload ordering used by a
// ClusterQueue. Kueue orders by priority and creation time; StrictFIFO also
// stops at the first workload that cannot be admitted, while BestEffortFIFO
// continues scanning.
package queue

import (
	"sort"

	"example.com/minikueue/internal/api"
)

// Sort orders workloads in admission order: higher priority first, then older
// creation timestamp, then stable UID/name.
func Sort(workloads []*api.Workload, classes map[string]*api.WorkloadPriorityClass) {
	sort.SliceStable(workloads, func(i, j int) bool {
		pi := workloads[i].EffectivePriority(classes)
		pj := workloads[j].EffectivePriority(classes)
		if pi != pj {
			return pi > pj
		}
		if workloads[i].Spec.CreationTimestamp != workloads[j].Spec.CreationTimestamp {
			return workloads[i].Spec.CreationTimestamp < workloads[j].Spec.CreationTimestamp
		}
		return workloads[i].Key() < workloads[j].Key()
	})
}

// FilterByClusterQueue returns active pending workloads targeting cqName,
// ordered by admission order. Workloads whose requeue backoff has not elapsed
// are skipped.
func FilterByClusterQueue(workloads []*api.Workload, cqName string, localQueueToCQ map[string]string,
	classes map[string]*api.WorkloadPriorityClass, tick int64) []*api.Workload {

	out := make([]*api.Workload, 0, len(workloads))
	for _, w := range workloads {
		if w.Status.State != api.StatePending || !w.Spec.Active {
			continue
		}
		cq, ok := localQueueToCQ[w.Namespace+"/"+w.Spec.QueueName]
		if !ok || cq != cqName {
			continue
		}
		if w.Status.RequeueAt > tick {
			continue
		}
		out = append(out, w)
	}
	Sort(out, classes)
	return out
}

// SortByScore orders workloads by an external score (ascending), then by the
// normal priority/creation/key order. It is used for Admission Fair Sharing,
// where the score is the LocalQueue's decaying historical usage.
func SortByScore(workloads []*api.Workload, classes map[string]*api.WorkloadPriorityClass,
	score func(*api.Workload) float64) []*api.Workload {
	out := append([]*api.Workload(nil), workloads...)
	sort.SliceStable(out, func(i, j int) bool {
		si, sj := score(out[i]), score(out[j])
		if si != sj {
			return si < sj
		}
		pi := out[i].EffectivePriority(classes)
		pj := out[j].EffectivePriority(classes)
		if pi != pj {
			return pi > pj
		}
		if out[i].Spec.CreationTimestamp != out[j].Spec.CreationTimestamp {
			return out[i].Spec.CreationTimestamp < out[j].Spec.CreationTimestamp
		}
		return out[i].Key() < out[j].Key()
	})
	return out
}

// FairShareOrder returns ClusterQueue names ordered by ascending weighted
// dominant-resource share, then by insertion order for determinism. The score
// function is supplied by the scheduler.
func FairShareOrder(names []string, score func(string) float64) []string {
	out := append([]string(nil), names...)
	sort.SliceStable(out, func(i, j int) bool {
		si, sj := score(out[i]), score(out[j])
		if si != sj {
			return si < sj
		}
		return out[i] < out[j]
	})
	return out
}
