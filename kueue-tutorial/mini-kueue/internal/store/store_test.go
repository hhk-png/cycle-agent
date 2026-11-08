package store

import (
	"testing"

	"example.com/minikueue/internal/api"
)

func cq(name string) *api.ClusterQueue {
	return &api.ClusterQueue{
		Name: name,
		Spec: api.ClusterQueueSpec{
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU},
				Flavors: []api.FlavorQuota{{
					Name: "default", Nominal: api.RL("cpu", 2000),
				}},
			}},
		},
	}
}

func TestWorkloadIsCloned(t *testing.T) {
	s := New()
	if err := s.UpsertClusterQueue(cq("cq")); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertLocalQueue(&api.LocalQueue{Namespace: "default", Name: "q", ClusterQueue: "cq"}); err != nil {
		t.Fatal(err)
	}
	w := &api.Workload{
		Namespace: "default", Name: "job",
		Spec: api.WorkloadSpec{
			Active: true, QueueName: "q",
			PodSets: []api.PodSet{{Name: "main", Count: 1, Requests: api.RL("cpu", 1000)}},
		},
	}
	if err := s.UpsertWorkload(w); err != nil {
		t.Fatal(err)
	}
	w.Spec.PodSets[0].Requests[api.CPU] = 9999
	got, _ := s.GetWorkload("default", "job")
	if got.Spec.PodSets[0].Requests.Get(api.CPU) != 1000 {
		t.Fatal("store did not clone the workload")
	}
	got.Spec.PodSets[0].Requests[api.CPU] = 1
	again, _ := s.GetWorkload("default", "job")
	if again.Spec.PodSets[0].Requests.Get(api.CPU) != 1000 {
		t.Fatal("GetWorkload did not clone")
	}
}

func TestSpecObjectsAreCloned(t *testing.T) {
	s := New()
	if err := s.UpsertClusterQueue(cq("cq")); err != nil {
		t.Fatal(err)
	}
	got, _ := s.GetClusterQueue("cq")
	got.Spec.ResourceGroups[0].Flavors[0].Nominal[api.CPU] = 999999
	again, _ := s.GetClusterQueue("cq")
	if again.Spec.ResourceGroups[0].Flavors[0].Nominal.Get(api.CPU) != 2000 {
		t.Fatal("GetClusterQueue returned a live pointer")
	}
	list := s.ListClusterQueues()
	list[0].Spec.ResourceGroups[0].Flavors[0].Nominal[api.CPU] = 1
	again, _ = s.GetClusterQueue("cq")
	if again.Spec.ResourceGroups[0].Flavors[0].Nominal.Get(api.CPU) != 2000 {
		t.Fatal("ListClusterQueues returned live pointers")
	}

	if err := s.UpsertLocalQueue(&api.LocalQueue{Namespace: "ns", Name: "q", ClusterQueue: "cq"}); err != nil {
		t.Fatal(err)
	}
	lq, _ := s.GetLocalQueue("ns", "q")
	lq.ClusterQueue = "mutated"
	lq, _ = s.GetLocalQueue("ns", "q")
	if lq.ClusterQueue != "cq" {
		t.Fatal("GetLocalQueue returned a live pointer")
	}

	s.UpsertResourceFlavor(&api.ResourceFlavor{Name: "f", NodeLabels: map[string]string{"a": "b"}})
	flavors := s.ListResourceFlavors()
	flavors[0].NodeLabels["a"] = "mutated"
	flavors = s.ListResourceFlavors()
	if flavors[0].NodeLabels["a"] != "b" {
		t.Fatal("ListResourceFlavors returned live pointers")
	}
}

func TestUpdateWorkloadPreservesUIDAndSpec(t *testing.T) {
	s := New()
	if err := s.UpsertClusterQueue(cq("cq")); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertLocalQueue(&api.LocalQueue{Namespace: "default", Name: "q", ClusterQueue: "cq"}); err != nil {
		t.Fatal(err)
	}
	w := &api.Workload{
		Namespace: "default", Name: "job", UID: "uid-1",
		Spec: api.WorkloadSpec{
			Active: true, QueueName: "q",
			PodSets: []api.PodSet{{Name: "main", Count: 1, Requests: api.RL("cpu", 1000)}},
		},
	}
	if err := s.UpsertWorkload(w); err != nil {
		t.Fatal(err)
	}
	w.UID = "uid-2"
	w.Spec.PodSets[0].Count = 100
	w.Status.State = api.StateAdmitted
	if err := s.UpdateWorkload(w); err != nil {
		t.Fatal(err)
	}
	got, _ := s.GetWorkload("default", "job")
	if got.UID != "uid-1" {
		t.Fatalf("UID changed: %q", got.UID)
	}
	if got.Spec.PodSets[0].Count != 1 {
		t.Fatalf("spec changed: count=%d", got.Spec.PodSets[0].Count)
	}
	if got.Status.State != api.StateAdmitted {
		t.Fatalf("status not updated: %s", got.Status.State)
	}
}

func TestUpsertTopologyRejectsIncompatibleLevels(t *testing.T) {
	s := New()
	if err := s.UpsertTopology(&api.Topology{Name: "topo", Levels: []string{"rack", "hostname"}}); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertTopologyDomain(&api.TopologyDomain{
		Name: "h1", Topology: "topo", Values: []string{"r1", "h1"}, Capacity: api.RL("cpu", 1000),
	}); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertTopology(&api.Topology{Name: "topo", Levels: []string{"zone"}}); err == nil {
		t.Fatal("expected incompatible level update to be rejected")
	}
	if got := s.ListTopologyDomains("topo"); len(got) != 1 {
		t.Fatalf("domain was silently removed: %d", len(got))
	}
	if topo, _ := s.GetTopology("topo"); len(topo.Levels) != 2 {
		t.Fatalf("topology was not rolled back: %v", topo.Levels)
	}
}

func TestUpsertWorkloadRejectsMissingQueue(t *testing.T) {
	s := New()
	w := &api.Workload{
		Namespace: "default", Name: "job",
		Spec: api.WorkloadSpec{
			Active: true, QueueName: "missing",
			PodSets: []api.PodSet{{Name: "main", Count: 1, Requests: api.RL("cpu", 1000)}},
		},
	}
	if err := s.UpsertWorkload(w); err == nil {
		t.Fatal("expected missing LocalQueue to be rejected")
	}
}

func TestValidateReferencesChecksFlavorTopology(t *testing.T) {
	s := New()
	s.UpsertResourceFlavor(&api.ResourceFlavor{Name: "f", TopologyName: "missing"})
	if err := s.ValidateReferences(); err == nil {
		t.Fatal("expected missing topology reference to fail")
	}
}

func TestLocalQueueRequiresClusterQueue(t *testing.T) {
	s := New()
	if err := s.UpsertLocalQueue(&api.LocalQueue{Namespace: "ns", Name: "q", ClusterQueue: "missing"}); err == nil {
		t.Fatal("expected missing CQ to be rejected")
	}
	if err := s.UpsertClusterQueue(cq("cq")); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertLocalQueue(&api.LocalQueue{Namespace: "ns", Name: "q", ClusterQueue: "cq"}); err != nil {
		t.Fatal(err)
	}
}

func TestCohortCycleRejected(t *testing.T) {
	s := New()
	if err := s.UpsertCohort(&api.Cohort{Name: "a", ParentName: "b"}); err == nil {
		t.Fatal("expected missing parent to be rejected")
	}
	if err := s.UpsertCohort(&api.Cohort{Name: "root"}); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertCohort(&api.Cohort{Name: "a", ParentName: "root"}); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertCohort(&api.Cohort{Name: "b", ParentName: "a"}); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertCohort(&api.Cohort{Name: "a", ParentName: "b"}); err == nil {
		t.Fatal("expected cycle to be rejected")
	}
}

func TestTopologyAndProviderStorage(t *testing.T) {
	s := New()
	if err := s.UpsertTopologyDomain(&api.TopologyDomain{Name: "h1", Topology: "missing", Values: []string{"r1"}}); err == nil {
		t.Fatal("expected missing topology to reject a domain")
	}
	if err := s.UpsertTopology(&api.Topology{Name: "topo", Levels: []string{"rack", "hostname"}}); err != nil {
		t.Fatal(err)
	}
	if err := s.UpsertTopologyDomain(&api.TopologyDomain{
		Name: "h1", Topology: "topo", Values: []string{"rack-a", "h1"},
		Capacity: api.RL("cpu", 4000),
	}); err != nil {
		t.Fatal(err)
	}
	if got := s.ListTopologyDomains("topo"); len(got) != 1 {
		t.Fatalf("domains = %d", len(got))
	}
	if got := s.AllTopologyDomains()["topo"]; len(got) != 1 {
		t.Fatalf("all domains = %d", len(got))
	}
	if _, ok := s.GetTopology("topo"); !ok {
		t.Fatal("topology not found")
	}

	if err := s.UpsertCapacityProvider(&api.CapacityProvider{
		Name: "pool", Capacity: map[string]api.ResourceList{"default": api.RL("cpu", 8000)},
	}); err != nil {
		t.Fatal(err)
	}
	if _, ok := s.GetCapacityProvider("pool"); !ok {
		t.Fatal("provider not found")
	}
	if got := s.ListCapacityProviders(); len(got) != 1 {
		t.Fatalf("providers = %d", len(got))
	}

	cq := cq("dynamic")
	cq.Spec.CapacityProvider = "missing"
	if err := s.UpsertClusterQueue(cq); err != nil {
		t.Fatal(err)
	}
	if err := s.ValidateReferences(); err == nil {
		t.Fatal("expected missing capacity provider reference to fail")
	}
}

func TestValidateReferences(t *testing.T) {
	s := New()
	implicit := cq("cq")
	implicit.Spec.CohortName = "implicit"
	if err := s.UpsertClusterQueue(implicit); err != nil {
		t.Fatal(err)
	}
	// A cohortName without a Cohort object is a valid implicit cohort.
	if err := s.ValidateReferences(); err != nil {
		t.Fatalf("implicit cohort should not be a missing reference: %v", err)
	}

	bad := cq("bad")
	bad.Spec.AdmissionChecks = []string{"missing-check"}
	if err := s.UpsertClusterQueue(bad); err != nil {
		t.Fatal(err)
	}
	if err := s.ValidateReferences(); err == nil {
		t.Fatal("expected missing admission check reference to fail")
	}
}
