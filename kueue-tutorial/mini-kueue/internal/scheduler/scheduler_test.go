package scheduler

import (
	"testing"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/cache"
)

func testWorkload(name string, priority int32, cpu int64, requests api.ResourceList) *api.Workload {
	if requests == nil {
		requests = api.RL("cpu", cpu)
	}
	return &api.Workload{
		Namespace: "default",
		Name:      name,
		Spec: api.WorkloadSpec{
			Active:            true,
			QueueName:         "q",
			Priority:          priority,
			CreationTimestamp: 1,
			PodSets:           []api.PodSet{{Name: "main", Count: 1, Requests: requests}},
		},
	}
}

func testCQ(name string, cpu int64) *api.ClusterQueue {
	return &api.ClusterQueue{
		Name: name,
		Spec: api.ClusterQueueSpec{
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU},
				Flavors: []api.FlavorQuota{{
					Name: "default", Nominal: api.RL("cpu", cpu),
				}},
			}},
			QueueingStrategy: api.BestEffortFIFO,
		},
	}
}

func commit(t testing.TB, c *cache.Cache, w *api.Workload, d Decision) {
	t.Helper()
	if d.Admission == nil {
		t.Fatal("expected admission")
	}
	w.Status.State = api.StateAdmitted
	w.Status.Admission = d.Admission
	if err := c.Add(w); err != nil {
		t.Fatal(err)
	}
}

func TestFlavorFallback(t *testing.T) {
	cq := &api.ClusterQueue{
		Name: "cq",
		Spec: api.ClusterQueueSpec{
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU},
				Flavors: []api.FlavorQuota{
					{Name: "spot", Nominal: api.RL("cpu", 2000)},
					{Name: "ondemand", Nominal: api.RL("cpu", 4000)},
				},
			}},
		},
	}
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))
	w := testWorkload("job", 0, 3000, nil)
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected admission, got %s", d.Reason)
	}
	if len(d.Admission.FlavorAssignments) != 1 || d.Admission.FlavorAssignments[0].Flavor != "ondemand" {
		t.Fatalf("unexpected assignment: %+v", d.Admission.FlavorAssignments)
	}
}

func TestCohortBorrowingAndCapacity(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	view := NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}})
	s := New(cache.New(), view)

	wa := testWorkload("a-job", 0, 6000, nil)
	da := s.TryAdmit(wa, a)
	if da.Admission == nil {
		t.Fatalf("expected a to borrow: %s", da.Reason)
	}
	commit(t, s.Cache, wa, da)

	wb := testWorkload("b-job", 0, 4000, nil)
	if db := s.TryAdmit(wb, b); db.Admission != nil {
		t.Fatal("expected cohort total quota to reject b")
	}
	wb2 := testWorkload("b-small", 0, 2000, nil)
	if db := s.TryAdmit(wb2, b); db.Admission == nil {
		t.Fatalf("expected b-small to fit exactly: %s", db.Reason)
	}
}

func TestNoCohortCannotBorrow(t *testing.T) {
	a := testCQ("a", 4000)
	s := New(cache.New(), NewView([]*api.ClusterQueue{a}, nil))
	if d := s.TryAdmit(testWorkload("job", 0, 6000, nil), a); d.Admission != nil {
		t.Fatal("expected admission to fail without a cohort")
	}
}

func TestExplicitBorrowingLimit(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	a.Spec.ResourceGroups[0].Flavors[0].BorrowingLimit = api.RL("cpu", 0)
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}}))
	if d := s.TryAdmit(testWorkload("job", 0, 6000, nil), a); d.Admission != nil {
		t.Fatal("expected explicit borrowingLimit=0 to reject borrowing")
	}
}

func TestExplicitLendingLimit(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	b.Spec.ResourceGroups[0].Flavors[0].LendingLimit = api.RL("cpu", 0)
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}}))
	if d := s.TryAdmit(testWorkload("job", 0, 6000, nil), a); d.Admission != nil {
		t.Fatal("expected explicit lendingLimit=0 to reject borrowing")
	}
}

func TestSameClusterQueuePreemption(t *testing.T) {
	cq := testCQ("cq", 2000)
	cq.Spec.Preemption.WithinClusterQueue = api.PreemptLower
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))
	low := testWorkload("low", 0, 2000, nil)
	commit(t, s.Cache, low, s.TryAdmit(low, cq))
	high := testWorkload("high", 10, 1000, nil)
	d := s.TryAdmit(high, cq)
	if d.Admission == nil {
		t.Fatalf("expected preemption: %s", d.Reason)
	}
	if len(d.Victims) != 1 || d.Victims[0].Name != "low" {
		t.Fatalf("unexpected victims: %+v", d.Victims)
	}
}

func TestReclaimFromBorrower(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	a.Spec.Preemption.ReclaimWithinCohort = api.PreemptLower
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}}))

	a1 := testWorkload("a1", 10, 2000, nil)
	b1 := testWorkload("b1", 0, 6000, nil)
	commit(t, s.Cache, a1, s.TryAdmit(a1, a))
	commit(t, s.Cache, b1, s.TryAdmit(b1, b))

	a2 := testWorkload("a2", 10, 2000, nil)
	d := s.TryAdmit(a2, a)
	if d.Admission == nil {
		t.Fatalf("expected reclaim: %s", d.Reason)
	}
	if len(d.Victims) != 1 || d.Victims[0].Name != "b1" {
		t.Fatalf("unexpected victims: %+v", d.Victims)
	}
}

func TestConfigurablePreemptionSelectorMatching(t *testing.T) {
	a := testCQ("a", 4000)
	a.Spec.CohortName = "shared"
	b := testCQ("b", 4000)
	b.Spec.CohortName = "shared"
	b.Labels = map[string]string{"tier": "shared"}
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}}))

	victim := testWorkload("victim", 0, 1000, nil)
	victim.Labels = map[string]string{"tier": "batch", "node-count": "4"}
	victim.Status.Admission = &api.Admission{ClusterQueue: "b"}
	incoming := testWorkload("incoming", 10, 1000, nil)
	incoming.Labels = map[string]string{"tier": "hero", "node-count": "8"}
	cqA := a

	cases := []struct {
		name     string
		selector api.PreemptionCandidateSelector
		want     bool
	}{
		{
			name:     "within local queue",
			selector: api.PreemptionCandidateSelector{Scope: api.ScopeWithinLocalQueue},
			want:     true,
		},
		{
			name:     "within cluster queue rejects other queue",
			selector: api.PreemptionCandidateSelector{Scope: api.ScopeWithinClusterQueue},
			want:     false,
		},
		{
			name:     "within cohort tree",
			selector: api.PreemptionCandidateSelector{Scope: api.ScopeWithinCohortTree},
			want:     true,
		},
		{
			name:     "within parent cohort excludes self",
			selector: api.PreemptionCandidateSelector{Scope: api.ScopeWithinParentCohort},
			want:     true,
		},
		{
			name:     "any cluster queue",
			selector: api.PreemptionCandidateSelector{Scope: api.ScopeAnyClusterQueue},
			want:     true,
		},
		{
			name: "lower priority",
			selector: api.PreemptionCandidateSelector{
				Scope:    api.ScopeWithinCohortTree,
				Priority: &api.PriorityConstraint{Mode: api.PriorityBase, Comparison: api.CompareLessThan},
			},
			want: true,
		},
		{
			name: "higher priority rejected",
			selector: api.PreemptionCandidateSelector{
				Scope:    api.ScopeWithinCohortTree,
				Priority: &api.PriorityConstraint{Mode: api.PriorityBase, Comparison: api.CompareGreaterThan},
			},
			want: false,
		},
		{
			name: "boosted priority",
			selector: api.PreemptionCandidateSelector{
				Scope:    api.ScopeWithinCohortTree,
				Priority: &api.PriorityConstraint{Mode: api.PriorityBoosted, Comparison: api.CompareLessThan},
			},
			want: true,
		},
		{
			name: "numeric label smaller than preemptor",
			selector: api.PreemptionCandidateSelector{
				Scope: api.ScopeWithinCohortTree,
				NumericLabels: []api.NumericLabelConstraint{{
					Key: "node-count", Comparison: api.CompareLessThan,
				}},
			},
			want: true,
		},
		{
			name: "numeric label missing preemptor falls back",
			selector: api.PreemptionCandidateSelector{
				Scope: api.ScopeWithinCohortTree,
				NumericLabels: []api.NumericLabelConstraint{{
					Key: "missing", Comparison: api.CompareLessThan, FallbackValue: 0,
				}},
			},
			want: false,
		},
		{
			name: "victim label selector",
			selector: api.PreemptionCandidateSelector{
				Scope:         api.ScopeWithinCohortTree,
				LabelSelector: &api.LabelSelector{MatchLabels: map[string]string{"tier": "batch"}},
			},
			want: true,
		},
		{
			name: "victim cluster queue selector",
			selector: api.PreemptionCandidateSelector{
				Scope:                api.ScopeWithinCohortTree,
				ClusterQueueSelector: &api.LabelSelector{MatchLabels: map[string]string{"tier": "shared"}},
			},
			want: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := s.selectorMatches(tc.selector, incoming, cqA, victim); got != tc.want {
				t.Fatalf("selectorMatches = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestQuotaFeasibleButTopologyInfeasible(t *testing.T) {
	cq := testCQ("cq", 8000)
	cq.Spec.ResourceGroups[0].CoveredResources = []api.ResourceName{api.CPU, api.ResourceName("example.com/gpu")}
	cq.Spec.ResourceGroups[0].Flavors[0].Nominal = api.RL("cpu", 8000, api.ResourceName("example.com/gpu"), 8)
	view := NewView([]*api.ClusterQueue{cq}, nil,
		WithFlavors([]*api.ResourceFlavor{{Name: "default", TopologyName: "topo"}}),
		WithTopologies(
			[]*api.Topology{{Name: "topo", Levels: []string{"hostname"}}},
			map[string][]*api.TopologyDomain{
				"topo": {{
					Name: "topo/h1", Topology: "topo", Values: []string{"h1"},
					Capacity: api.RL(api.ResourceName("example.com/gpu"), 2),
				}},
			},
		))
	s := New(cache.New(), view)
	w := testWorkload("tas", 0, 0, api.RL("cpu", 1000, api.ResourceName("example.com/gpu"), 4))
	w.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "hostname"}

	if _, ok := s.findAssignmentWithoutTopology(w, cq, nil); !ok {
		t.Fatal("quota should fit without topology")
	}
	if _, ok := s.findAssignment(w, cq, nil); ok {
		t.Fatal("topology should not fit: one host has only 2 devices")
	}
	d := s.TryAdmit(w, cq)
	if d.Admission != nil {
		t.Fatal("expected TryAdmit to fail")
	}
	if d.Reason != "InsufficientQuota" {
		t.Fatalf("reason = %s", d.Reason)
	}
}

func TestFairShareWeight(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	b.Spec.FairSharing = &api.FairSharing{Weight: 2}
	cohort := &api.Cohort{Name: "shared"}
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{cohort}))
	wa := testWorkload("a", 0, 2000, nil)
	wb := testWorkload("b", 0, 6000, nil)
	commit(t, s.Cache, wa, s.TryAdmit(wa, a))
	commit(t, s.Cache, wb, s.TryAdmit(wb, b))

	// A stays inside nominal, so its share is zero. B borrows 2000 against
	// A's 4000 nominal lendable, giving an unweighted ratio of 0.5 and a
	// weighted share of 0.25 (weight 2).
	if got := s.FairShare("a"); got != 0 {
		t.Fatalf("a share = %v, want 0", got)
	}
	if got := s.FairShare("b"); got != 0.25 {
		t.Fatalf("b share = %v, want 0.25", got)
	}
}

func TestFairShareCohortWeight(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	cohort := &api.Cohort{Name: "shared", FairSharing: &api.FairSharing{Weight: 2}}
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{cohort}))
	wa := testWorkload("a", 0, 6000, nil)
	commit(t, s.Cache, wa, s.TryAdmit(wa, a))

	// Unweighted ratio is 2000 borrowed / 4000 lendable = 0.5. The cohort
	// weight of 2 doubles the nominal share, so the DRS is 0.25.
	if got := s.FairShare("a"); got != 0.25 {
		t.Fatalf("a share = %v, want 0.25", got)
	}
}

func TestFairShareIgnoresNonBorrowingQueues(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}}))
	wa := testWorkload("a", 0, 2000, nil)
	wb := testWorkload("b", 0, 2000, nil)
	commit(t, s.Cache, wa, s.TryAdmit(wa, a))
	commit(t, s.Cache, wb, s.TryAdmit(wb, b))
	if got := s.FairShare("a"); got != 0 {
		t.Fatalf("a share = %v, want 0", got)
	}
	if got := s.FairShare("b"); got != 0 {
		t.Fatalf("b share = %v, want 0", got)
	}
}

func TestMultipleResourceGroups(t *testing.T) {
	cq := &api.ClusterQueue{
		Name: "cq",
		Spec: api.ClusterQueueSpec{
			ResourceGroups: []api.ResourceGroup{
				{
					CoveredResources: []api.ResourceName{api.CPU, api.Memory},
					Flavors: []api.FlavorQuota{
						{Name: "small", Nominal: api.RL("cpu", 2000, "memory", 4096)},
						{Name: "large", Nominal: api.RL("cpu", 4000, "memory", 8192)},
					},
				},
				{
					CoveredResources: []api.ResourceName{"nvidia.com/gpu"},
					Flavors: []api.FlavorQuota{
						{Name: "gpu1", Nominal: api.RL("nvidia.com/gpu", 1)},
						{Name: "gpu2", Nominal: api.RL("nvidia.com/gpu", 2)},
					},
				},
			},
		},
	}
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))
	w := testWorkload("multi", 0, 0, api.RL("cpu", 3000, "memory", 6144, "nvidia.com/gpu", 1))
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected admission: %s", d.Reason)
	}
	if len(d.Admission.FlavorAssignments) != 2 {
		t.Fatalf("assignments = %+v", d.Admission.FlavorAssignments)
	}
	if d.Admission.FlavorAssignments[0].Flavor != "large" || d.Admission.FlavorAssignments[1].Flavor != "gpu1" {
		t.Fatalf("unexpected assignments: %+v", d.Admission.FlavorAssignments)
	}
}

func TestHierarchicalCohortCapacity(t *testing.T) {
	root := &api.Cohort{Name: "root"}
	left := &api.Cohort{Name: "left", ParentName: "root"}
	right := &api.Cohort{Name: "right", ParentName: "root"}
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "left"
	b.Spec.CohortName = "right"
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{root, left, right}))

	wa := testWorkload("a", 0, 6000, nil)
	da := s.TryAdmit(wa, a)
	if da.Admission == nil {
		t.Fatalf("expected cross-branch borrowing: %s", da.Reason)
	}
	commit(t, s.Cache, wa, da)
	if d := s.TryAdmit(testWorkload("b", 0, 4000, nil), b); d.Admission != nil {
		t.Fatal("expected root cohort total to reject b")
	}
}

func TestEqualPriorityPreemptionIsAcyclic(t *testing.T) {
	cq := testCQ("cq", 2000)
	cq.Spec.Preemption.WithinClusterQueue = api.PreemptLowerOrNewerEqual
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))

	newer := testWorkload("newer", 0, 2000, nil)
	newer.Spec.CreationTimestamp = 2
	commit(t, s.Cache, newer, s.TryAdmit(newer, cq))

	older := testWorkload("older", 0, 1000, nil)
	older.Spec.CreationTimestamp = 1
	d := s.TryAdmit(older, cq)
	if d.Admission == nil || len(d.Victims) != 1 || d.Victims[0].Name != "newer" {
		t.Fatalf("older workload should preempt newer: %+v", d)
	}

	// The reverse direction must be rejected.
	cq2 := testCQ("cq2", 2000)
	cq2.Spec.Preemption.WithinClusterQueue = api.PreemptLowerOrNewerEqual
	s2 := New(cache.New(), NewView([]*api.ClusterQueue{cq2}, nil))
	olderVictim := testWorkload("older-victim", 0, 2000, nil)
	olderVictim.Spec.CreationTimestamp = 1
	commit(t, s2.Cache, olderVictim, s2.TryAdmit(olderVictim, cq2))
	newerIncoming := testWorkload("newer-incoming", 0, 1000, nil)
	newerIncoming.Spec.CreationTimestamp = 2
	if d := s2.TryAdmit(newerIncoming, cq2); d.Admission != nil {
		t.Fatal("newer workload must not preempt an older one at equal priority")
	}
}

func TestCohortSharedQuota(t *testing.T) {
	cohort := &api.Cohort{
		Name: "shared",
		ResourceGroups: []api.ResourceGroup{{
			CoveredResources: []api.ResourceName{api.CPU},
			Flavors: []api.FlavorQuota{{
				Name: "default", Nominal: api.RL("cpu", 4000),
			}},
		}},
	}
	a := testCQ("a", 0)
	b := testCQ("b", 0)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{cohort}))

	wa := testWorkload("a", 0, 3000, nil)
	da := s.TryAdmit(wa, a)
	if da.Admission == nil {
		t.Fatalf("expected shared quota admission: %s", da.Reason)
	}
	commit(t, s.Cache, wa, da)
	if d := s.TryAdmit(testWorkload("b", 0, 2000, nil), b); d.Admission != nil {
		t.Fatal("expected shared quota total to reject b")
	}
}

func tasCQ() *api.ClusterQueue {
	return &api.ClusterQueue{
		Name: "tas",
		Spec: api.ClusterQueueSpec{
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{"nvidia.com/gpu"},
				Flavors: []api.FlavorQuota{{
					Name: "tas", Nominal: api.RL("nvidia.com/gpu", 12),
				}},
			}},
		},
	}
}

func tasView(cq *api.ClusterQueue, domains []*api.TopologyDomain) View {
	return NewView([]*api.ClusterQueue{cq}, nil,
		WithFlavors([]*api.ResourceFlavor{{Name: "tas", TopologyName: "gpu-topology"}}),
		WithTopologies([]*api.Topology{{Name: "gpu-topology", Levels: []string{"rack", "hostname"}}},
			map[string][]*api.TopologyDomain{"gpu-topology": domains}),
	)
}

func tasDomains() []*api.TopologyDomain {
	return []*api.TopologyDomain{
		{Name: "h1", Topology: "gpu-topology", Values: []string{"rack-a", "h1"}, Capacity: api.RL("nvidia.com/gpu", 2)},
		{Name: "h2", Topology: "gpu-topology", Values: []string{"rack-a", "h2"}, Capacity: api.RL("nvidia.com/gpu", 2)},
		{Name: "h3", Topology: "gpu-topology", Values: []string{"rack-b", "h3"}, Capacity: api.RL("nvidia.com/gpu", 2)},
		{Name: "h4", Topology: "gpu-topology", Values: []string{"rack-b", "h4"}, Capacity: api.RL("nvidia.com/gpu", 2)},
	}
}

func TestTopologyRequiredPacksAcrossHostsInRack(t *testing.T) {
	cq := tasCQ()
	s := New(cache.New(), tasView(cq, tasDomains()))
	w := testWorkload("job", 0, 0, api.RL("nvidia.com/gpu", 1))
	w.Spec.PodSets[0].Count = 4
	w.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "rack"}

	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected admission: %s", d.Reason)
	}
	as := d.Admission.TopologyAssignments
	if len(as) != 1 || as[0].Domain != "rack-a" || as[0].PodCount != 4 {
		t.Fatalf("unexpected topology assignments: %+v", as)
	}
	commit(t, s.Cache, w, d)

	w2 := testWorkload("job2", 0, 0, api.RL("nvidia.com/gpu", 1))
	w2.Spec.PodSets[0].Count = 4
	w2.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "rack"}
	d2 := s.TryAdmit(w2, cq)
	if d2.Admission == nil || d2.Admission.TopologyAssignments[0].Domain != "rack-b" {
		t.Fatalf("expected rack-b: %+v", d2)
	}
	commit(t, s.Cache, w2, d2)

	w3 := testWorkload("job3", 0, 0, api.RL("nvidia.com/gpu", 1))
	w3.Spec.PodSets[0].Count = 4
	w3.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "rack"}
	if d3 := s.TryAdmit(w3, cq); d3.Admission != nil {
		t.Fatalf("expected topology capacity to reject job3: %+v", d3.Admission)
	}
}

func TestTopologyRackReservationBlocksHostnamePlacement(t *testing.T) {
	cq := tasCQ()
	// Only one rack, so there is no other rack for the hostname-level job.
	s := New(cache.New(), tasView(cq, tasDomains()[:2]))

	rackJob := testWorkload("rack-job", 0, 0, api.RL("nvidia.com/gpu", 1))
	rackJob.Spec.PodSets[0].Count = 4
	rackJob.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "rack"}
	d := s.TryAdmit(rackJob, cq)
	if d.Admission == nil {
		t.Fatalf("expected rack admission: %s", d.Reason)
	}
	commit(t, s.Cache, rackJob, d)

	// The rack is physically full (2+2 GPUs), so a hostname-level job of 2
	// GPUs must not double-book it despite the different assignment level.
	hostJob := testWorkload("host-job", 0, 0, api.RL("nvidia.com/gpu", 1))
	hostJob.Spec.PodSets[0].Count = 2
	hostJob.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyRequired, Level: "hostname"}
	if d2 := s.TryAdmit(hostJob, cq); d2.Admission != nil {
		t.Fatalf("hostname job double-booked a full rack: %+v", d2.Admission.TopologyAssignments)
	}
}

func TestTopologyMixedLevelsInsideOneWorkload(t *testing.T) {
	cq := tasCQ()
	s := New(cache.New(), tasView(cq, tasDomains()[:2]))

	w := testWorkload("mixed", 0, 0, api.RL("nvidia.com/gpu", 1))
	w.Spec.PodSets = []api.PodSet{
		{Name: "rack", Count: 4, Requests: api.RL("nvidia.com/gpu", 1),
			Topology: &api.PodSetTopology{Mode: api.TopologyRequired, Level: "rack"}},
		{Name: "host", Count: 2, Requests: api.RL("nvidia.com/gpu", 1),
			Topology: &api.PodSetTopology{Mode: api.TopologyRequired, Level: "hostname"}},
	}
	if d := s.TryAdmit(w, cq); d.Admission != nil {
		t.Fatalf("mixed-level workload double-booked the rack: %+v", d.Admission.TopologyAssignments)
	}
}

func TestTopologyPreferredFallsBack(t *testing.T) {
	cq := tasCQ()
	domains := []*api.TopologyDomain{
		{Name: "h1", Topology: "gpu-topology", Values: []string{"rack-a", "h1"}, Capacity: api.RL("nvidia.com/gpu", 1)},
		{Name: "h2", Topology: "gpu-topology", Values: []string{"rack-a", "h2"}, Capacity: api.RL("nvidia.com/gpu", 1)},
	}
	s := New(cache.New(), tasView(cq, domains))
	w := testWorkload("job", 0, 0, api.RL("nvidia.com/gpu", 1))
	w.Spec.PodSets[0].Count = 4
	w.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyPreferred, Level: "rack"}
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("preferred topology should fall back, got %s", d.Reason)
	}
	if len(d.Admission.TopologyAssignments) != 0 {
		t.Fatalf("expected no topology assignment, got %+v", d.Admission.TopologyAssignments)
	}
}

func TestTopologyUnconstrainedSpreads(t *testing.T) {
	// Unconstrained placement at hostname level must spread 4 pods across two
	// hosts that each have 2 GPUs.
	domains := []*api.TopologyDomain{
		{Name: "h1", Topology: "gpu-topology", Values: []string{"rack-a", "h1"}, Capacity: api.RL("nvidia.com/gpu", 2)},
		{Name: "h2", Topology: "gpu-topology", Values: []string{"rack-a", "h2"}, Capacity: api.RL("nvidia.com/gpu", 2)},
	}
	cq := tasCQ()
	s := New(cache.New(), tasView(cq, domains))
	w := testWorkload("job", 0, 0, api.RL("nvidia.com/gpu", 1))
	w.Spec.PodSets[0].Count = 4
	w.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyUnconstrained, Level: "hostname"}
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected admission: %s", d.Reason)
	}
	as := d.Admission.TopologyAssignments
	if len(as) != 2 || as[0].PodCount != 2 || as[1].PodCount != 2 {
		t.Fatalf("unexpected spread: %+v", as)
	}
	if as[0].Domain != "rack-a/h1" || as[1].Domain != "rack-a/h2" {
		t.Fatalf("unexpected domains: %+v", as)
	}
}

func TestTopologyUnconstrainedIgnoresUnknownUsageDomain(t *testing.T) {
	domains := []*api.TopologyDomain{
		{Name: "h1", Topology: "gpu-topology", Values: []string{"rack-a", "h1"}, Capacity: api.RL("nvidia.com/gpu", 2)},
	}
	cq := tasCQ()
	s := New(cache.New(), tasView(cq, domains))

	// A stale reservation references a domain that is no longer in the store.
	ghost := testWorkload("ghost", 0, 0, api.RL("nvidia.com/gpu", 1))
	ghost.Status.Admission = &api.Admission{
		ClusterQueue: "tas",
		FlavorAssignments: []api.FlavorAssignment{{
			GroupIndex: 0, Flavor: "tas", Resources: api.RL("nvidia.com/gpu", 1),
		}},
		Usage: api.RL("nvidia.com/gpu", 1),
		TopologyAssignments: []api.TopologyAssignment{{
			Topology: "gpu-topology", PodSet: "main", Level: "hostname", Domain: "ghost",
			PodCount: 1, Resources: api.RL("nvidia.com/gpu", 1),
		}},
	}
	if err := s.Cache.Add(ghost); err != nil {
		t.Fatal(err)
	}

	w := testWorkload("job", 0, 0, api.RL("nvidia.com/gpu", 1))
	w.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyUnconstrained, Level: "hostname"}
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected admission: %s", d.Reason)
	}
	for _, ta := range d.Admission.TopologyAssignments {
		if ta.Domain == "ghost" {
			t.Fatalf("placed into a domain without capacity: %+v", ta)
		}
	}
}

func TestTopologyGroupSharesDomain(t *testing.T) {
	cq := tasCQ()
	s := New(cache.New(), tasView(cq, tasDomains()))
	// A named group must share a domain. Group members may use Preferred; the
	// group itself supplies the shared-domain requirement.
	w := testWorkload("job", 0, 0, api.RL("nvidia.com/gpu", 1))
	w.Spec.PodSets[0].Topology = &api.PodSetTopology{Mode: api.TopologyPreferred, Level: "rack", Group: "trainers"}
	w.Spec.PodSets = append(w.Spec.PodSets, api.PodSet{
		Name: "sidecar", Count: 1, Requests: api.RL("nvidia.com/gpu", 1),
		Topology: &api.PodSetTopology{Mode: api.TopologyPreferred, Level: "rack", Group: "trainers"},
	})
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected admission: %s", d.Reason)
	}
	as := d.Admission.TopologyAssignments
	if len(as) != 2 || as[0].Domain != as[1].Domain {
		t.Fatalf("group should share one domain: %+v", as)
	}
}

func TestEffectiveQuotaOverridesNominal(t *testing.T) {
	cq := testCQ("cq", 4000)
	view := NewView([]*api.ClusterQueue{cq}, nil,
		WithEffectiveQuotas(map[string]map[string]api.ResourceList{
			"cq": {"default": api.RL("cpu", 1000)},
		}))
	s := New(cache.New(), view)
	if d := s.TryAdmit(testWorkload("big", 0, 2000, nil), cq); d.Admission != nil {
		t.Fatal("expected effective quota to reject 2000m")
	}
	if d := s.TryAdmit(testWorkload("ok", 0, 1000, nil), cq); d.Admission == nil {
		t.Fatalf("expected 1000m to fit: %s", d.Reason)
	}
}

func TestImplicitCohortBorrowing(t *testing.T) {
	a := testCQ("a", 4000)
	b := testCQ("b", 4000)
	a.Spec.CohortName = "implicit"
	b.Spec.CohortName = "implicit"
	// No Cohort objects: the name alone forms a flat, implicit cohort.
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, nil))
	w := testWorkload("borrower", 0, 6000, nil)
	if d := s.TryAdmit(w, a); d.Admission == nil {
		t.Fatalf("implicit cohort should allow borrowing: %s", d.Reason)
	}
}

func TestCohortBorrowingLimitCapsSubtree(t *testing.T) {
	root := &api.Cohort{Name: "root"}
	orgA := &api.Cohort{
		Name: "org-a", ParentName: "root",
		ResourceGroups: []api.ResourceGroup{{
			CoveredResources: []api.ResourceName{api.CPU},
			Flavors:          []api.FlavorQuota{{Name: "default", BorrowingLimit: api.RL("cpu", 2000)}},
		}},
	}
	orgB := &api.Cohort{
		Name: "org-b", ParentName: "root",
		ResourceGroups: []api.ResourceGroup{{
			CoveredResources: []api.ResourceName{api.CPU},
			Flavors:          []api.FlavorQuota{{Name: "default", LendingLimit: api.RL("cpu", 3000)}},
		}},
	}
	a := testCQ("a", 4000)
	a.Spec.CohortName = "org-a"
	b := testCQ("b", 4000)
	b.Spec.CohortName = "org-b"
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{root, orgA, orgB}))

	first := testWorkload("first", 0, 6000, nil)
	commit(t, s.Cache, first, s.TryAdmit(first, a))

	// Another 1000m would borrow 3000m in total for org-a, exceeding its
	// 2000m cohort-level borrowing limit.
	second := testWorkload("second", 0, 1000, nil)
	if d := s.TryAdmit(second, a); d.Admission != nil {
		t.Fatal("expected cohort borrowingLimit to reject the second workload")
	}
}

func slicingView(hosts []string) (View, *api.ClusterQueue) {
	domains := map[string][]*api.TopologyDomain{}
	for _, host := range hosts {
		domains["gpu-topology"] = append(domains["gpu-topology"], &api.TopologyDomain{
			Name: "rack-a/" + host, Topology: "gpu-topology", Values: []string{"rack-a", host},
			Capacity: api.RL("cpu", 8000, "nvidia.com/gpu", 4),
		})
	}
	flavor := &api.ResourceFlavor{Name: "tas-flavor", TopologyName: "gpu-topology"}
	cq := &api.ClusterQueue{
		Name: "tas", Spec: api.ClusterQueueSpec{
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU, "nvidia.com/gpu"},
				Flavors: []api.FlavorQuota{{
					Name: "tas-flavor", Nominal: api.RL("cpu", 32000, "nvidia.com/gpu", 16),
				}},
			}},
		},
	}
	return NewView([]*api.ClusterQueue{cq}, nil,
		WithFlavors([]*api.ResourceFlavor{flavor}),
		WithTopologies([]*api.Topology{{Name: "gpu-topology", Levels: []string{"rack", "hostname"}}}, domains)), cq
}

func tasWorkload(name string, count int32, topo *api.PodSetTopology) *api.Workload {
	w := testWorkload(name, 0, 0, api.RL("cpu", 1000, "nvidia.com/gpu", 1))
	w.Spec.PodSets[0].Count = count
	w.Spec.PodSets[0].Topology = topo
	return w
}

func TestTASSpreadingPrefersLeastUsedDomain(t *testing.T) {
	view, cq := slicingView([]string{"h1", "h2"})
	s := New(cache.New(), view)

	first := tasWorkload("first", 1, &api.PodSetTopology{Mode: api.TopologyRequired, Level: "hostname"})
	commit(t, s.Cache, first, s.TryAdmit(first, cq))

	spread := tasWorkload("spread", 1, &api.PodSetTopology{
		Mode: api.TopologyRequired, Level: "hostname", Spreading: true,
	})
	d := s.TryAdmit(spread, cq)
	if d.Admission == nil || len(d.Admission.TopologyAssignments) != 1 {
		t.Fatalf("expected spreading admission: %+v", d)
	}
	if got := d.Admission.TopologyAssignments[0].Domain; got != "rack-a/h2" {
		t.Fatalf("spreading domain = %q, want rack-a/h2", got)
	}
}

func TestTASSlicesSplitPodSet(t *testing.T) {
	view, cq := slicingView([]string{"h1", "h2", "h3", "h4"})
	s := New(cache.New(), view)
	w := tasWorkload("sliced", 4, &api.PodSetTopology{
		Mode: api.TopologyRequired, Level: "hostname", SliceSize: 2,
	})
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected sliced admission: %s", d.Reason)
	}
	as := d.Admission.TopologyAssignments
	if len(as) != 2 {
		t.Fatalf("assignments = %d, want 2", len(as))
	}
	for _, a := range as {
		if a.PodCount != 2 {
			t.Fatalf("slice pod count = %d, want 2", a.PodCount)
		}
	}
	if as[0].Domain == as[1].Domain {
		t.Fatalf("slices should use different domains: %+v", as)
	}
}

func TestCohortBorrowingLimitNotMaskedByUnderusedSibling(t *testing.T) {
	root := &api.Cohort{Name: "root"}
	orgA := &api.Cohort{Name: "org-a", ParentName: "root"}
	orgB := &api.Cohort{Name: "org-b", ParentName: "root"}
	a1 := testCQ("a1", 4000)
	a1.Spec.CohortName = "org-a"
	a2 := testCQ("a2", 4000)
	a2.Spec.CohortName = "org-a"
	b := testCQ("b", 4000)
	b.Spec.CohortName = "org-b"
	s := New(cache.New(), NewView([]*api.ClusterQueue{a1, a2, b}, []*api.Cohort{root, orgA, orgB}))

	// a2 borrows 3000m while a1 stays idle. With netting, the subtree would
	// show zero borrowing because a1's unused nominal cancels a2's excess.
	first := testWorkload("a2-big", 0, 7000, nil)
	commit(t, s.Cache, first, s.TryAdmit(first, a2))

	// Now impose a 2000m cohort-level borrowing cap. The existing 3000m and the
	// requested 1000m must be counted per member, not netted across the subtree.
	orgA.ResourceGroups = []api.ResourceGroup{{
		CoveredResources: []api.ResourceName{api.CPU},
		Flavors:          []api.FlavorQuota{{Name: "default", BorrowingLimit: api.RL("cpu", 2000)}},
	}}
	second := testWorkload("a2-more", 0, 1000, nil)
	if d := s.TryAdmit(second, a2); d.Admission != nil {
		t.Fatalf("expected cohort borrowingLimit to reject additional borrowing, got %+v", d.Admission)
	}
}

func TestBorrowWithinCohortPreemption(t *testing.T) {
	a := testCQ("a", 6000)
	b := testCQ("b", 2000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	b.Spec.Preemption.BorrowWithinCohort = &api.BorrowWithinCohort{Policy: api.PreemptLower}
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}}))

	a1 := testWorkload("a1", 0, 8000, nil)
	commit(t, s.Cache, a1, s.TryAdmit(a1, a))
	b1 := testWorkload("b1", 10, 3000, nil)
	d := s.TryAdmit(b1, b)
	if d.Admission == nil {
		t.Fatalf("expected borrow-within preemption: %s", d.Reason)
	}
	if len(d.Victims) != 1 || d.Victims[0].Name != "a1" {
		t.Fatalf("unexpected victims: %+v", d.Victims)
	}
}

func TestTASPartialSlice(t *testing.T) {
	view, cq := slicingView([]string{"h1", "h2", "h3", "h4"})
	s := New(cache.New(), view)
	w := tasWorkload("partial", 10, &api.PodSetTopology{
		Mode: api.TopologyRequired, Level: "hostname", SliceSize: 4,
	})
	d := s.TryAdmit(w, cq)
	if d.Admission == nil {
		t.Fatalf("expected partial-slice admission: %s", d.Reason)
	}
	as := d.Admission.TopologyAssignments
	if len(as) != 3 {
		t.Fatalf("assignments = %d, want 3 (4+4+2)", len(as))
	}
	var total int32
	for _, a := range as {
		if a.PodCount <= 0 || a.PodCount > 4 {
			t.Fatalf("slice pod count out of range: %+v", a)
		}
		total += a.PodCount
	}
	if total != 10 {
		t.Fatalf("slice coverage = %d, want 10", total)
	}
	if as[0].Domain == as[1].Domain || as[1].Domain == as[2].Domain || as[0].Domain == as[2].Domain {
		t.Fatalf("slices should use distinct domains: %+v", as)
	}
}
