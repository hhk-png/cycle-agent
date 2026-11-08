package queue

import (
	"testing"

	"example.com/minikueue/internal/api"
)

func wl(name string, priority int32, created int64) *api.Workload {
	return &api.Workload{
		Namespace: "default",
		Name:      name,
		Spec: api.WorkloadSpec{
			Active: true, QueueName: "q", Priority: priority, CreationTimestamp: created,
		},
	}
}

func TestSortPriorityThenCreation(t *testing.T) {
	workloads := []*api.Workload{
		wl("old-low", 0, 1),
		wl("new-high", 10, 3),
		wl("old-high", 10, 2),
	}
	Sort(workloads, nil)
	got := []string{workloads[0].Name, workloads[1].Name, workloads[2].Name}
	want := []string{"old-high", "new-high", "old-low"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v", got, want)
		}
	}
}

func TestFilterByClusterQueueHonorsBackoff(t *testing.T) {
	workloads := []*api.Workload{
		wl("ready", 0, 1),
		wl("backoff", 0, 2),
	}
	workloads[1].Status.State = api.StatePending
	workloads[1].Status.RequeueAt = 5
	workloads[0].Status.State = api.StatePending
	lq := map[string]string{"default/q": "cq"}
	got := FilterByClusterQueue(workloads, "cq", lq, nil, 4)
	if len(got) != 1 || got[0].Name != "ready" {
		t.Fatalf("filtered = %+v", got)
	}
	got = FilterByClusterQueue(workloads, "cq", lq, nil, 5)
	if len(got) != 2 {
		t.Fatalf("filtered at requeue time = %+v", got)
	}
}

func TestFairShareOrder(t *testing.T) {
	names := []string{"a", "b", "c"}
	scores := map[string]float64{"a": 0.8, "b": 0.2, "c": 0.5}
	got := FairShareOrder(names, func(n string) float64 { return scores[n] })
	want := []string{"b", "c", "a"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v", got, want)
		}
	}
}

func TestSortByScoreUsesScoreThenPriority(t *testing.T) {
	mk := func(name string, priority int32, created int64) *api.Workload {
		return &api.Workload{
			Namespace: "default", Name: name,
			Spec: api.WorkloadSpec{Priority: priority, CreationTimestamp: created},
		}
	}
	lowHistory := mk("low-history", 0, 3)
	highHistory := mk("high-history", 10, 1)
	highPriority := mk("high-priority", 10, 2)
	scores := map[string]float64{"low-history": 0, "high-history": 5, "high-priority": 0}
	got := SortByScore([]*api.Workload{highHistory, highPriority, lowHistory}, nil, func(w *api.Workload) float64 {
		return scores[w.Name]
	})
	if got[0].Name != "high-priority" || got[1].Name != "low-history" || got[2].Name != "high-history" {
		t.Fatalf("order = %v", []string{got[0].Name, got[1].Name, got[2].Name})
	}
}
