package scenario_test

import (
	"fmt"
	"math/rand"
	"testing"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/controller"
	"example.com/minikueue/internal/scenario"
)

func TestAllScenariosRunAndPreserveQuotaInvariants(t *testing.T) {
	for _, name := range scenario.Names() {
		t.Run(name, func(t *testing.T) {
			e, _, err := scenario.Run(name)
			if err != nil {
				t.Fatal(err)
			}
			for i := 0; i < 8; i++ {
				e.Tick()
				checkInvariants(t, e)
			}
		})
	}
}

func TestCohortSharedPool(t *testing.T) {
	e, _, err := scenario.Run("cohort-shared-pool")
	if err != nil {
		t.Fatal(err)
	}
	e.Tick()

	first, ok := e.Store.GetWorkload("team-a", "first")
	if !ok {
		t.Fatal("first workload missing")
	}
	if first.Status.State != api.StateAdmitted {
		t.Fatalf("first workload: got state %s, want Admitted", first.Status.State)
	}
	second, ok := e.Store.GetWorkload("team-b", "second")
	if !ok {
		t.Fatal("second workload missing")
	}
	if second.Status.State != api.StatePending {
		t.Fatalf("second workload: got state %s, want Pending", second.Status.State)
	}
	// The first workload must be accounted on pool-a, and the shared pool must
	// be full at 3000m even though both member CQs have zero nominal.
	if got := e.Cache.FlavorUsage("pool-a", "default", api.CPU); got != 3000 {
		t.Fatalf("pool-a usage = %d, want 3000", got)
	}
	if got := e.Cache.FlavorUsage("pool-b", "default", api.CPU); got != 0 {
		t.Fatalf("pool-b usage = %d, want 0", got)
	}
}

func TestRandomizedStressPreservesQuotaInvariants(t *testing.T) {
	e := scenario.New()
	scenario.MustCohort(e, &api.Cohort{Name: "shared"})
	for _, team := range []string{"a", "b", "c"} {
		cq := scenario.SimpleCQ(team, "default", api.RL("cpu", 4000, "memory", 4096))
		cq.Spec.CohortName = "shared"
		cq.Spec.Preemption.WithinClusterQueue = api.PreemptLower
		cq.Spec.Preemption.ReclaimWithinCohort = api.PreemptLower
		scenario.MustCQ(e, cq)
		scenario.MustLQ(e, team, "q", team)
		scenario.MustNamespace(e, team, nil)
	}

	rng := rand.New(rand.NewSource(42))
	next := 0
	for i := 0; i < 80; i++ {
		// Keep some headroom so the random run exercises borrowing, pending
		// workloads and preemption rather than only immediate rejections.
		pods := int32(1 + rng.Intn(3))
		cpu := int64(500 * (1 + rng.Intn(6)))
		mem := int64(256 * (1 + rng.Intn(8)))
		priority := int32(rng.Intn(4) * 10)
		team := []string{"a", "b", "c"}[rng.Intn(3)]
		next++
		w := scenario.Workload(team, fmt.Sprintf("w%03d", next), "q", priority, pods,
			api.RL("cpu", cpu, "memory", mem))
		w.Spec.CreationTimestamp = int64(next)
		scenario.MustSubmit(e, w)
	}
	for tick := 0; tick < 40; tick++ {
		e.Tick()
		checkInvariants(t, e)
		if tick%3 == 0 {
			workloads := controller.SortedWorkloads(e.Store)
			for _, w := range workloads {
				if w.Status.State == api.StateAdmitted && rng.Intn(3) == 0 {
					if err := e.Finish(w.Namespace, w.Name); err != nil {
						t.Fatal(err)
					}
				}
			}
		}
	}
}

func checkInvariants(t *testing.T, e *controller.Engine) {
	t.Helper()
	cqs := e.Store.ListClusterQueues()
	cohorts := map[string]*api.Cohort{}
	for _, c := range e.Store.ListCohorts() {
		cohorts[c.Name] = c
	}
	byName := map[string]*api.ClusterQueue{}
	for _, cq := range cqs {
		byName[cq.Name] = cq
	}

	for _, cq := range cqs {
		for _, group := range cq.Spec.ResourceGroups {
			for _, fq := range group.Flavors {
				for r, nominal := range fq.Nominal {
					usage := e.Cache.FlavorUsage(cq.Name, fq.Name, r)
					if usage < 0 {
						t.Fatalf("negative usage: cq=%s flavor=%s resource=%s usage=%d", cq.Name, fq.Name, r, usage)
					}
					if cq.Spec.CohortName == "" && usage > nominal {
						t.Fatalf("non-cohort overuse: cq=%s flavor=%s resource=%s usage=%d nominal=%d",
							cq.Name, fq.Name, r, usage, nominal)
					}
					if limit, ok := fq.BorrowingLimit[r]; ok && usage > nominal+limit {
						t.Fatalf("borrowing limit exceeded: cq=%s flavor=%s resource=%s usage=%d nominal=%d limit=%d",
							cq.Name, fq.Name, r, usage, nominal, limit)
					}
				}
			}
		}
	}

	// Every cohort tree must respect its aggregate nominal quota.
	for _, cq := range cqs {
		root := cohortRoot(cq, cohorts)
		if root == "" {
			continue
		}
		members := cohortMembers(root, cqs, cohorts)
		flavorResources := map[string]map[api.ResourceName]int64{}
		for _, member := range members {
			for _, group := range member.Spec.ResourceGroups {
				for _, fq := range group.Flavors {
					if flavorResources[fq.Name] == nil {
						flavorResources[fq.Name] = map[api.ResourceName]int64{}
					}
					for r, n := range fq.Nominal {
						flavorResources[fq.Name][r] += n
					}
				}
			}
		}
		for name, c := range cohorts {
			if cohortRootName(name, cohorts) != root {
				continue
			}
			for _, group := range c.ResourceGroups {
				for _, fq := range group.Flavors {
					if flavorResources[fq.Name] == nil {
						flavorResources[fq.Name] = map[api.ResourceName]int64{}
					}
					for r, n := range fq.Nominal {
						flavorResources[fq.Name][r] += n
					}
				}
			}
		}
		for flavor, resources := range flavorResources {
			for r, nominal := range resources {
				var usage int64
				for _, member := range members {
					usage += e.Cache.FlavorUsage(member.Name, flavor, r)
				}
				if usage > nominal {
					t.Fatalf("cohort overuse: root=%s flavor=%s resource=%s usage=%d nominal=%d",
						root, flavor, r, usage, nominal)
				}
			}
		}
	}

	// TAS usage must respect domain capacity at every level. The simulator
	// reserves at the level named by the annotation, so aggregate capacity is
	// the right bound rather than per-host capacity.
	for _, topology := range e.Store.ListTopologies() {
		domains := e.Store.ListTopologyDomains(topology.Name)
		for levelIndex, level := range topology.Levels {
			capacity := map[string]api.ResourceList{}
			for _, d := range domains {
				if len(d.Values) != len(topology.Levels) {
					continue
				}
				key := ""
				for i := 0; i <= levelIndex; i++ {
					if i > 0 {
						key += "/"
					}
					key += d.Values[i]
				}
				if capacity[key] == nil {
					capacity[key] = api.ResourceList{}
				}
				capacity[key].Add(d.Capacity)
			}
			// Use the scheduler's physical view: a coarse-level reservation
			// occupies capacity at finer levels too, and vice versa.
			usage := e.Scheduler.TopologyUsage(topology.Name, level, nil)
			for domain, used := range usage {
				for r, q := range used {
					if limit, ok := capacity[domain][r]; ok && q > limit {
						t.Fatalf("topology overuse: topology=%s level=%s domain=%s resource=%s usage=%d capacity=%d",
							topology.Name, level, domain, r, q, limit)
					}
				}
			}
		}
	}

	// Admission usage must equal the workload's effective request, and TAS
	// assignments must cover exactly the PodSet's effective pod count. This is
	// the invariant that catches "quota released but topology still charged".
	for _, w := range e.Store.ListWorkloads() {
		if w.Status.State != api.StateQuotaReserved && w.Status.State != api.StateAdmitted {
			continue
		}
		if w.Status.Admission == nil {
			t.Fatalf("workload %s is %s without admission", w.Key(), w.Status.State)
		}
		if !sameResources(w.Status.Admission.Usage, w.TotalUsage()) {
			t.Fatalf("admission usage drifted for %s: admission=%s effective=%s",
				w.Key(), w.Status.Admission.Usage, w.TotalUsage())
		}
		assignedPods := map[string]int32{}
		for _, ta := range w.Status.Admission.TopologyAssignments {
			assignedPods[ta.PodSet] += ta.PodCount
		}
		for _, ps := range w.Spec.PodSets {
			effective := w.EffectiveCount(ps)
			if got := assignedPods[ps.Name]; got > 0 && got != effective {
				t.Fatalf("topology assignment for %s pod set %s covers %d pods, effective count is %d",
					w.Key(), ps.Name, got, effective)
			}
		}
	}

	// Explicit cohort-level borrowingLimit must cap the sum of each member's
	// positive borrow. Netting an idle sibling against a borrower would hide a
	// violation, so this check is intentionally per member.
	for _, cohort := range cohorts {
		for _, group := range cohort.ResourceGroups {
			for _, fq := range group.Flavors {
				for resource, limit := range fq.BorrowingLimit {
					var borrowed int64
					for _, cq := range cqs {
						if !cohortPathContains(cq, cohort.Name, cohorts) {
							continue
						}
						usage := e.Cache.FlavorUsage(cq.Name, fq.Name, resource)
						nominal := cqNominal(cq, fq.Name, resource)
						if usage > nominal {
							borrowed += usage - nominal
						}
					}
					if borrowed > limit {
						t.Fatalf("cohort %s borrowingLimit exceeded: flavor=%s resource=%s borrowed=%d limit=%d",
							cohort.Name, fq.Name, resource, borrowed, limit)
					}
				}
			}
		}
	}

	// Cache membership must exactly match the reserving lifecycle states.
	expected := map[string]bool{}
	for _, w := range e.Store.ListWorkloads() {
		if w.Status.State == api.StateQuotaReserved || w.Status.State == api.StateAdmitted {
			expected[w.Key()] = true
			if w.Status.Admission == nil {
				t.Fatalf("workload %s is %s without admission", w.Key(), w.Status.State)
			}
		}
	}
	for _, w := range e.Cache.Admitted() {
		if !expected[w.Key()] {
			t.Fatalf("cache contains %s but store state is %s", w.Key(), w.Status.State)
		}
		delete(expected, w.Key())
	}
	if len(expected) > 0 {
		t.Fatalf("store has reserving workloads missing from cache: %v", expected)
	}
}

func sameResources(a, b api.ResourceList) bool {
	keys := map[api.ResourceName]bool{}
	for name := range a {
		keys[name] = true
	}
	for name := range b {
		keys[name] = true
	}
	for name := range keys {
		if a.Get(name) != b.Get(name) {
			return false
		}
	}
	return true
}

func cqNominal(cq *api.ClusterQueue, flavor string, resource api.ResourceName) int64 {
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			if fq.Name == flavor {
				return fq.Nominal.Get(resource)
			}
		}
	}
	return 0
}

func cohortPathContains(cq *api.ClusterQueue, name string, cohorts map[string]*api.Cohort) bool {
	cur := cq.Spec.CohortName
	seen := map[string]bool{}
	for cur != "" {
		if cur == name {
			return true
		}
		if seen[cur] {
			return false
		}
		seen[cur] = true
		c, ok := cohorts[cur]
		if !ok {
			return false
		}
		cur = c.ParentName
	}
	return false
}

func cohortRoot(cq *api.ClusterQueue, cohorts map[string]*api.Cohort) string {
	if cq.Spec.CohortName == "" {
		return ""
	}
	return cohortRootName(cq.Spec.CohortName, cohorts)
}

func cohortRootName(name string, cohorts map[string]*api.Cohort) string {
	seen := map[string]bool{}
	for {
		c, ok := cohorts[name]
		if !ok {
			return ""
		}
		if seen[name] {
			return ""
		}
		seen[name] = true
		if c.ParentName == "" {
			return c.Name
		}
		name = c.ParentName
	}
}

func cohortMembers(root string, cqs []*api.ClusterQueue, cohorts map[string]*api.Cohort) []*api.ClusterQueue {
	var out []*api.ClusterQueue
	for _, cq := range cqs {
		if cohortRoot(cq, cohorts) == root {
			out = append(out, cq)
		}
	}
	return out
}
