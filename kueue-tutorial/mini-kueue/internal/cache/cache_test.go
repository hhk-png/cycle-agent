package cache

import (
	"testing"

	"example.com/minikueue/internal/api"
)

func admitted(name, cq, flavor string, cpu int64) *api.Workload {
	return &api.Workload{
		Namespace: "default",
		Name:      name,
		Spec:      api.WorkloadSpec{Active: true, QueueName: "q"},
		Status: api.WorkloadStatus{
			State: api.StateAdmitted,
			Admission: &api.Admission{
				ClusterQueue: cq,
				FlavorAssignments: []api.FlavorAssignment{{
					GroupIndex: 0, Flavor: flavor, Resources: api.RL("cpu", cpu),
				}},
				Usage: api.RL("cpu", cpu),
			},
		},
	}
}

func TestAddRemoveAndUsage(t *testing.T) {
	c := New()
	a := admitted("a", "cq", "f", 1000)
	b := admitted("b", "cq", "f", 2000)
	if err := c.Add(a); err != nil {
		t.Fatal(err)
	}
	if err := c.Add(b); err != nil {
		t.Fatal(err)
	}
	if got := c.FlavorUsage("cq", "f", api.CPU); got != 3000 {
		t.Fatalf("usage = %d", got)
	}
	if got := c.CQUsage("cq").Get(api.CPU); got != 3000 {
		t.Fatalf("cq usage = %d", got)
	}
	c.Remove(a.Key())
	if got := c.FlavorUsage("cq", "f", api.CPU); got != 2000 {
		t.Fatalf("usage after remove = %d", got)
	}
	if c.Len() != 1 {
		t.Fatalf("len = %d", c.Len())
	}
}

func TestAddRejectsDuplicateAndMissingAdmission(t *testing.T) {
	c := New()
	a := admitted("a", "cq", "f", 1000)
	if err := c.Add(a); err != nil {
		t.Fatal(err)
	}
	if err := c.Add(a); err == nil {
		t.Fatal("expected duplicate add to fail")
	}
	if err := c.Add(&api.Workload{}); err == nil {
		t.Fatal("expected missing admission to fail")
	}
}

func TestRefreshUpdatesStatusWithoutChangingUsageOrSequence(t *testing.T) {
	c := New()
	w := admitted("a", "cq", "f", 1000)
	if err := c.Add(w); err != nil {
		t.Fatal(err)
	}
	seq := w.Status.Sequence
	refreshed := w.Clone()
	refreshed.Status.State = api.StateAdmitted
	refreshed.Status.AdmittedAt = 7
	if err := c.Refresh(refreshed); err != nil {
		t.Fatal(err)
	}
	got, ok := c.Get("default/a")
	if !ok {
		t.Fatal("workload missing")
	}
	if got.Status.AdmittedAt != 7 {
		t.Fatalf("AdmittedAt = %d, want 7", got.Status.AdmittedAt)
	}
	if got.Status.Sequence != seq {
		t.Fatalf("sequence = %d, want %d", got.Status.Sequence, seq)
	}
	if usage := c.FlavorUsage("cq", "f", api.CPU); usage != 1000 {
		t.Fatalf("usage = %d, want 1000", usage)
	}
	if err := c.Refresh(admitted("missing", "cq", "f", 1)); err == nil {
		t.Fatal("expected refresh of a missing workload to fail")
	}
}

func TestExcluding(t *testing.T) {
	c := New()
	a := admitted("a", "cq", "f", 1000)
	b := admitted("b", "cq", "f", 2000)
	_ = c.Add(a)
	_ = c.Add(b)
	excluded := map[string]bool{a.Key(): true}
	if got := c.FlavorUsageExcluding([]string{"cq"}, "f", api.CPU, excluded); got != 2000 {
		t.Fatalf("excluding usage = %d", got)
	}
}

func TestTopologyUsageAndExcluding(t *testing.T) {
	c := New()
	a := admitted("a", "cq", "f", 1000)
	a.Status.Admission.TopologyAssignments = []api.TopologyAssignment{{
		Topology: "topo", PodSet: "main", Level: "rack", Domain: "rack-a",
		PodCount: 2, Resources: api.RL("cpu", 1000),
	}}
	b := admitted("b", "cq", "f", 2000)
	b.Status.Admission.TopologyAssignments = []api.TopologyAssignment{{
		Topology: "topo", PodSet: "main", Level: "rack", Domain: "rack-a",
		PodCount: 3, Resources: api.RL("cpu", 2000),
	}}
	_ = c.Add(a)
	_ = c.Add(b)
	usage := c.TopologyUsage("topo", "rack", nil)
	if got := usage["rack-a"].Get(api.CPU); got != 3000 {
		t.Fatalf("cpu usage = %d, want 3000", got)
	}
	if got := usage["rack-a"].Get(api.Pods); got != 5 {
		t.Fatalf("pods usage = %d, want 5", got)
	}
	usage = c.TopologyUsage("topo", "rack", map[string]bool{a.Key(): true})
	if got := usage["rack-a"].Get(api.CPU); got != 2000 {
		t.Fatalf("cpu usage excluding a = %d, want 2000", got)
	}
	if got := c.TopologyUsage("other", "rack", nil); len(got) != 0 {
		t.Fatalf("unexpected usage for unknown topology: %+v", got)
	}
}

func TestFairShareLendableDoesNotDependOnSiblingUsage(t *testing.T) {
	c := New()
	a := admitted("a", "a", "f", 1000)
	_ = c.Add(a)
	nominal := func(cq, flavor string, r api.ResourceName) int64 { return 4000 }
	noLimit := func(cq, flavor string, r api.ResourceName) (int64, bool) { return 0, false }

	before := c.FairShareLendable([]string{"a", "b"}, "b", "f", api.CPU, nil, nominal, noLimit)
	// a's own usage grows; the DRS denominator must stay at a's nominal (4000).
	_ = c.Refresh(admitted("a", "a", "f", 3000))
	after := c.FairShareLendable([]string{"a", "b"}, "b", "f", api.CPU, nil, nominal, noLimit)
	if before != 4000 || after != 4000 {
		t.Fatalf("fair-share lendable changed with sibling usage: before=%d after=%d, want 4000/4000", before, after)
	}

	// The feasibility view must still see only the currently unused 1000.
	if got := c.LendableExcluding([]string{"a", "b"}, "b", "f", api.CPU, nil, nominal, noLimit); got != 1000 {
		t.Fatalf("current lendable = %d, want 1000", got)
	}
}

func TestLendableExcluding(t *testing.T) {
	c := New()
	a := admitted("a", "a", "f", 1000)
	b := admitted("b", "b", "f", 4000)
	_ = c.Add(a)
	_ = c.Add(b)
	nominal := func(cq, flavor string, r api.ResourceName) int64 {
		if cq == "a" {
			return 4000
		}
		return 2000
	}
	// a has 3 CPUs unused and no explicit lending limit (unlimited).
	got := c.LendableExcluding([]string{"a", "b"}, "b", "f", api.CPU, nil, nominal,
		func(cq, flavor string, r api.ResourceName) (int64, bool) { return 0, false })
	if got != 3000 {
		t.Fatalf("lendable = %d, want 3000", got)
	}
	// An explicit zero lending limit means nothing is lendable.
	got = c.LendableExcluding([]string{"a", "b"}, "b", "f", api.CPU, nil, nominal,
		func(cq, flavor string, r api.ResourceName) (int64, bool) { return 0, true })
	if got != 0 {
		t.Fatalf("lendable with zero limit = %d, want 0", got)
	}
}
