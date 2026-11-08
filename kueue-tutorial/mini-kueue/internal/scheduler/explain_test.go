package scheduler

import (
	"testing"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/cache"
)

func TestExplainReportsClusterQueueNominal(t *testing.T) {
	cq := testCQ("cq", 2000)
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))
	for _, name := range []string{"a", "b"} {
		w := testWorkload(name, 0, 1000, nil)
		commit(t, s.Cache, w, s.TryAdmit(w, cq))
	}

	pending := testWorkload("c", 0, 1000, nil)
	explanation := s.Explain(pending, cq)
	if explanation.DirectFeasible || explanation.QuotaFeasible {
		t.Fatalf("expected infeasible, got %+v", explanation)
	}
	if explanation.Reason != "InsufficientQuota" {
		t.Fatalf("unexpected reason: %s", explanation.Reason)
	}
	if len(explanation.Groups) != 1 || len(explanation.Groups[0].Flavors) != 1 {
		t.Fatalf("unexpected groups: %+v", explanation.Groups)
	}
	flavor := explanation.Groups[0].Flavors[0]
	if flavor.Feasible || flavor.BlockedBy != "ClusterQueueNominal" {
		t.Fatalf("unexpected flavor verdict: %+v", flavor)
	}
	if len(flavor.Checks) != 1 {
		t.Fatalf("expected one resource check, got %+v", flavor.Checks)
	}
	check := flavor.Checks[0]
	if check.Current != 2000 || check.Nominal != 2000 || check.Requested != 1000 || check.NewUsage != 3000 {
		t.Fatalf("unexpected check numbers: %+v", check)
	}
	if explanation.Preemption == nil || explanation.Preemption.Enabled {
		t.Fatalf("expected disabled preemption, got %+v", explanation.Preemption)
	}
}

func TestExplainMatchesAdmissionDecision(t *testing.T) {
	cq := testCQ("cq", 3000)
	cq.Spec.Preemption.WithinClusterQueue = api.PreemptLower
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))
	low := testWorkload("low", 0, 2000, nil)
	commit(t, s.Cache, low, s.TryAdmit(low, cq))

	high := testWorkload("high", 10, 2000, nil)
	decision := s.TryAdmit(high, cq)
	explanation := s.Explain(high, cq)
	if (decision.Admission != nil) != (explanation.Admission != nil) {
		t.Fatalf("decision and explanation disagree: decision=%+v explanation=%+v", decision, explanation)
	}
	if decision.Admission == nil {
		t.Fatalf("expected preemption to admit, got %s", decision.Reason)
	}
	if len(decision.Victims) != 1 || decision.Victims[0].Key() != low.Key() {
		t.Fatalf("unexpected victims: %+v", decision.Victims)
	}
	if explanation.Preemption == nil || !explanation.Preemption.Feasible {
		t.Fatalf("expected feasible preemption: %+v", explanation.Preemption)
	}
	if len(explanation.Preemption.Victims) != 1 || explanation.Preemption.Victims[0] != low.Key() {
		t.Fatalf("unexpected explained victims: %+v", explanation.Preemption.Victims)
	}
}

func TestExplainExcludesOwnReservation(t *testing.T) {
	cq := testCQ("cq", 1000)
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))
	w := testWorkload("self", 0, 1000, nil)
	commit(t, s.Cache, w, s.TryAdmit(w, cq))

	explanation := s.Explain(w, cq)
	if !explanation.DirectFeasible {
		t.Fatalf("an admitted workload must not count itself: %+v", explanation)
	}
}

func TestExplainBorrowCheckDetails(t *testing.T) {
	a := testCQ("a", 1000)
	b := testCQ("b", 1000)
	a.Spec.CohortName = "shared"
	b.Spec.CohortName = "shared"
	s := New(cache.New(), NewView([]*api.ClusterQueue{a, b}, []*api.Cohort{{Name: "shared"}}))

	// Fill a beyond nominal; the 500m must be borrowed from b.
	wa := testWorkload("a-job", 0, 1500, nil)
	commit(t, s.Cache, wa, s.TryAdmit(wa, a))

	extra := testWorkload("a-extra", 0, 500, nil)
	explanation := s.Explain(extra, a)
	if !explanation.DirectFeasible {
		t.Fatalf("expected borrowing to be feasible: %+v", explanation)
	}
	check := explanation.Groups[0].Flavors[0].Checks[0]
	if check.Borrow != 1000 {
		t.Fatalf("expected borrow=1000 after the new workload, got %+v", check)
	}
	if check.Lendable != 1000 {
		t.Fatalf("expected lendable=1000 from the sibling, got %+v", check)
	}
	if check.BlockedBy != "" {
		t.Fatalf("expected no blocker, got %+v", check)
	}
}

func TestExplainReportsAdmissionGateAtControllerLevel(t *testing.T) {
	// The scheduler itself does not know about admission gates; the controller
	// wrapper does. This test pins the scheduler-side contract: the workload is
	// still feasible, so a caller must not infer Pending from DirectFeasible.
	cq := testCQ("cq", 1000)
	s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))
	w := testWorkload("gated", 0, 500, nil)
	w.Spec.AdmissionGates = []string{"security-scan"}
	if explanation := s.Explain(w, cq); !explanation.DirectFeasible {
		t.Fatalf("quota is available; gate is a controller concern: %+v", explanation)
	}
}
