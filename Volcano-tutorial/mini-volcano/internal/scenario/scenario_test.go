package scenario_test

import (
	"fmt"
	"math/rand"
	"strings"
	"testing"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/scenario"
	"example.com/minivolcano/internal/sim"
)

func TestAllScenarios(t *testing.T) {
	outcomes := scenario.RunAll()
	if len(outcomes) == 0 {
		t.Fatal("no scenarios")
	}
	for _, o := range outcomes {
		if o.Err != nil {
			t.Errorf("%s: %v", o.Name, o.Err)
		}
	}
}

func TestDeterminism(t *testing.T) {
	for _, name := range []string{"basic", "priority", "topology", "fairshare"} {
		sc, err := scenario.Get(name)
		if err != nil {
			t.Fatal(err)
		}
		w1, err := scenario.RunScenario(sc, nil)
		if err != nil {
			t.Fatalf("%s first run: %v", name, err)
		}
		w2, err := scenario.RunScenario(sc, nil)
		if err != nil {
			t.Fatalf("%s second run: %v", name, err)
		}
		if w1.Summary() != w2.Summary() {
			t.Fatalf("%s summary is not deterministic:\n%s\n---\n%s", name, w1.Summary(), w2.Summary())
		}
		if len(w1.Events) != len(w2.Events) {
			t.Fatalf("%s event count differs: %d vs %d", name, len(w1.Events), len(w2.Events))
		}
	}
}

func TestWhatIf(t *testing.T) {
	sc, err := scenario.Get("proportion")
	if err != nil {
		t.Fatal(err)
	}
	copy := *sc
	copy.Check = nil
	w, err := scenario.RunScenario(&copy, map[string]int64{"team-a/nvidia.com/gpu": 2})
	if err != nil {
		t.Fatal(err)
	}
	if got := w.Cache.QueueAllocated("team-a")[api.GPU]; got != 0 {
		t.Fatalf("team-a should not fit a 6-GPU gang under a 2-GPU cap, got %d", got)
	}
	if got := w.Cache.QueueAllocated("team-b")[api.GPU]; got != 2 {
		t.Fatalf("team-b allocated=%d", got)
	}
}

func TestExplainEvidence(t *testing.T) {
	sc, err := scenario.Get("drf")
	if err != nil {
		t.Fatal(err)
	}
	w, err := scenario.RunScenario(sc, nil)
	if err != nil {
		t.Fatal(err)
	}
	text := w.Explain("a2")
	for _, want := range []string{"phase=Inqueue", "gang not ready", "pending=2"} {
		if !strings.Contains(text, want) {
			t.Fatalf("explain missing %q:\n%s", want, text)
		}
	}
}

func TestEventLogAndMetrics(t *testing.T) {
	sc, err := scenario.Get("basic")
	if err != nil {
		t.Fatal(err)
	}
	w, err := scenario.RunScenario(sc, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(w.EventLog(50), "Scheduled") {
		t.Fatal("event log missing Scheduled")
	}
	metrics := w.MetricsText()
	if !strings.Contains(metrics, "minivolcano_scheduler_sessions_total") {
		t.Fatal("metrics missing sessions counter")
	}
	if !strings.Contains(metrics, "minivolcano_queue_allocated") {
		t.Fatal("metrics missing queue gauge")
	}
}

// TestRandomizedInvariants is the property-based safety net: random workloads
// must never violate resource conservation, gang readiness or placement
// invariants.
func TestRandomizedInvariants(t *testing.T) {
	for seed := int64(1); seed <= 12; seed++ {
		seed := seed
		t.Run(fmt.Sprintf("seed-%d", seed), func(t *testing.T) {
			runRandom(t, seed)
		})
	}
}

func runRandom(t *testing.T, seed int64) {
	t.Helper()
	r := rand.New(rand.NewSource(seed))
	w, err := sim.New(fmt.Sprintf("random-%d", seed), "randomized invariant workload", framework.DefaultConfig())
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 4; i++ {
		gpu := int64(2 + r.Intn(4))
		if err := w.AddNode(&api.Node{
			Name:        fmt.Sprintf("n%d", i),
			Allocatable: api.R(8000, 32768, gpu),
		}); err != nil {
			t.Fatal(err)
		}
	}
	for i, weight := range []int32{1, 2} {
		if err := w.AddQueue(&api.Queue{Name: fmt.Sprintf("q%d", i), Spec: api.QueueSpec{Weight: weight}}); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 18; i++ {
		replicas := int32(1 + r.Intn(5))
		min := int32(r.Intn(int(replicas) + 1))
		req := api.Resource{api.CPU: int64(500 + 500*r.Intn(3)), api.Memory: 1024}
		if r.Intn(2) == 0 {
			req[api.GPU] = 1
		}
		priority := int32(0)
		if r.Intn(4) == 0 {
			priority = 10
		}
		j := &api.Job{
			Name: fmt.Sprintf("j-%d", i), Namespace: "default",
			Spec: api.JobSpec{
				Queue: fmt.Sprintf("q%d", r.Intn(2)), Priority: priority, MaxRetry: 2,
				MinAvailable: min, MinAvailableSet: true,
				Tasks: []api.TaskSpec{{Name: "w", Replicas: replicas, Requests: req, Duration: int64(1 + r.Intn(4))}},
			},
		}
		if err := w.SubmitJob(j); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	checkInvariants(t, w)
	for tick := 1; tick <= 25; tick++ {
		if err := w.Tick(); err != nil {
			t.Fatalf("tick %d: %v", tick, err)
		}
		checkInvariants(t, w)
	}
}

func checkInvariants(t *testing.T, w *sim.World) {
	t.Helper()
	for name, ns := range w.Cache.Nodes {
		sum := api.Resource{}
		for _, p := range ns.Pods {
			if !p.Phase.OccupiesNode() {
				continue
			}
			if p.NodeName != name {
				t.Fatalf("pod %s in node %s map but NodeName=%s", p.Name, name, p.NodeName)
			}
			sum = sum.Add(p.Requests)
		}
		for k, v := range ns.Node.Allocated {
			if v < 0 {
				t.Fatalf("node %s has negative %s=%d", name, k, v)
			}
			if v > ns.Node.Allocatable[k] {
				t.Fatalf("node %s over-allocated %s: %d > %d", name, k, v, ns.Node.Allocatable[k])
			}
		}
		if !sum.Equal(ns.Node.Allocated) {
			t.Fatalf("node %s accounting mismatch: pods=%s allocated=%s", name, sum, ns.Node.Allocated)
		}
	}
	for _, q := range w.Cache.Queues {
		sum := api.Resource{}
		for _, p := range w.Cache.Pods {
			if p.Queue == q.Name && p.Phase.OccupiesNode() {
				sum = sum.Add(p.Requests)
			}
		}
		if !sum.Equal(w.Cache.QueueAllocated(q.Name)) {
			t.Fatalf("queue %s accounting mismatch: %s vs %s", q.Name, sum, w.Cache.QueueAllocated(q.Name))
		}
	}
	for _, p := range w.Cache.Pods {
		if !p.Phase.OccupiesNode() {
			continue
		}
		if p.NodeName == "" {
			t.Fatalf("pod %s occupies a node but has no NodeName", p.Name)
		}
		if _, ok := w.Cache.Nodes[p.NodeName]; !ok {
			t.Fatalf("pod %s references missing node %s", p.Name, p.NodeName)
		}
	}
	for _, j := range w.Cache.Jobs {
		if j.Status.Phase == api.JobRunning && j.Spec.MinAvailable > 0 {
			if ready := w.Cache.ReadyCount(j.UID); ready < int(j.Spec.MinAvailable) {
				t.Fatalf("job %s Running with ready=%d min=%d", j.Name, ready, j.Spec.MinAvailable)
			}
		}
	}
}
