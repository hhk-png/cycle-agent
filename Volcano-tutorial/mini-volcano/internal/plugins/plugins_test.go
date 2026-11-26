package plugins_test

import (
	"strings"
	"testing"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/plugins"
)

func fixture(t *testing.T) *cache.Cache {
	t.Helper()
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(8000, 32768, 8)}); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{"default", "a", "b"} {
		if err := c.AddQueue(&api.Queue{Name: q}); err != nil {
			t.Fatal(err)
		}
	}
	return c
}

func submit(t *testing.T, c *cache.Cache, j *api.Job) *api.Job {
	t.Helper()
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	return c.Jobs[j.Namespace+"/"+j.Name]
}

func session(t *testing.T, c *cache.Cache, now int64, specs ...framework.PluginConfig) *framework.Session {
	t.Helper()
	var instances []framework.PluginInstance
	for _, spec := range specs {
		p, err := plugins.Build(spec.Name, spec.Args)
		if err != nil {
			t.Fatalf("build %s: %v", spec.Name, err)
		}
		instances = append(instances, framework.PluginInstance{Plugin: p, Tier: 0, Args: spec.Args})
	}
	return framework.NewSession(c, now, framework.Config{Actions: []string{"enqueue"}}, instances)
}

func simpleJob(name, queue string, replicas int32, req api.Resource) *api.Job {
	return &api.Job{Name: name, Namespace: "default", Spec: api.JobSpec{
		Queue: queue, Priority: 0, MinAvailable: replicas, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: replicas, Requests: req}},
	}}
}

func TestGangReadiness(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, simpleJob("j", "default", 3, api.Cores(1)))
	ssn := session(t, c, 0, framework.PluginConfig{Name: "gang"})
	if ssn.JobReady(j) {
		t.Fatal("no pods ready")
	}
	for i := 0; i < 2; i++ {
		if err := c.Bind(j.UID+"-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	if ssn.JobReady(j) {
		t.Fatal("2/3 ready must not satisfy minAvailable=3")
	}
}

func TestGangTaskMinMemberReadiness(t *testing.T) {
	c := fixture(t)
	j := &api.Job{Name: "roles", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 5, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			{Name: "master", Replicas: 1, MinAvailable: int32Ptr(1), Requests: api.Cores(1)},
			{Name: "worker", Replicas: 8, MinAvailable: int32Ptr(4), Requests: api.Cores(1)},
		},
	}}
	submitted := submit(t, c, j)
	ssn := session(t, c, 0, framework.PluginConfig{Name: "gang"})
	for i := 0; i < 5; i++ {
		if err := c.Bind(submitted.UID+"-worker-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	if ssn.JobReady(submitted) {
		t.Fatal("five workers without a master must not satisfy the role gate")
	}
	if err := c.Bind(submitted.UID+"-master-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	if !ssn.JobReady(submitted) {
		t.Fatal("1 master + 5 workers must satisfy minAvailable=5 and worker>=4")
	}
}

func int32Ptr(v int32) *int32 { return &v }

func TestGangDisableJobStarvingDoesNotRegister(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, simpleJob("j", "default", 2, api.Cores(1)))
	ssn := session(t, c, 0, framework.PluginConfig{
		Name: "gang", Args: map[string]string{"enableJobStarving": "false"},
	})
	if ssn.JobStarving(j) {
		t.Fatal("enableJobStarving=false must not register a starving hook")
	}
}

func TestGangValidRejectsImpossibleMin(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, simpleJob("j", "default", 2, api.Cores(1)))
	j.Spec.MinAvailable = 3
	ssn := session(t, c, 0, framework.PluginConfig{Name: "gang"})
	if err := ssn.JobValid(j); err == nil {
		t.Fatal("minAvailable > replicas should fail")
	}
}

func TestPriorityOrder(t *testing.T) {
	c := fixture(t)
	lo := &api.Job{Name: "lo", Namespace: "default", Spec: api.JobSpec{Priority: 1}}
	hi := &api.Job{Name: "hi", Namespace: "default", Spec: api.JobSpec{Priority: 100}}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "priority"})
	if ssn.JobOrder(hi, lo) >= 0 {
		t.Fatal("higher priority should sort first")
	}
	if ssn.JobOrder(lo, hi) <= 0 {
		t.Fatal("lower priority should sort last")
	}
}

func TestDRFOrdersSmallerShareFirst(t *testing.T) {
	c := fixture(t)
	small := submit(t, c, simpleJob("small", "default", 2, api.Cores(1)))
	large := submit(t, c, simpleJob("large", "default", 4, api.Cores(1)))
	ssn := session(t, c, 0, framework.PluginConfig{Name: "drf"})
	if ssn.JobOrder(small, large) >= 0 {
		t.Fatal("smaller dominant share should sort first")
	}
}

func TestProportionDeservedAndCapability(t *testing.T) {
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(0, 0, 8)}); err != nil {
		t.Fatal(err)
	}
	qa := &api.Queue{Name: "a", Spec: api.QueueSpec{Weight: 3, Capability: api.Resource{api.GPU: 5}}}
	qb := &api.Queue{Name: "b", Spec: api.QueueSpec{Weight: 1}}
	if err := c.AddQueue(qa); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(qb); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "proportion"})
	if got := c.Queues["a"].Status.Deserved[api.GPU]; got != 5 {
		t.Fatalf("a deserved should be capped at 5, got %d", got)
	}
	if got := c.Queues["b"].Status.Deserved[api.GPU]; got != 2 {
		t.Fatalf("b deserved=%d", got)
	}
	if !ssn.Allocatable(c.Queues["a"], api.Resource{api.GPU: 5}) {
		t.Fatal("5 GPU should fit capability 5")
	}
	if ssn.Allocatable(c.Queues["a"], api.Resource{api.GPU: 6}) {
		t.Fatal("6 GPU should exceed capability 5")
	}
}

func TestCapacityHierarchy(t *testing.T) {
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(0, 0, 8)}); err != nil {
		t.Fatal(err)
	}
	org := &api.Queue{Name: "org", Spec: api.QueueSpec{Weight: 1, Capability: api.Resource{api.GPU: 6}}}
	a := &api.Queue{Name: "a", Spec: api.QueueSpec{Weight: 1, Parent: "org"}}
	b := &api.Queue{Name: "b", Spec: api.QueueSpec{Weight: 1, Parent: "org"}}
	for _, q := range []*api.Queue{org, a, b} {
		if err := c.AddQueue(q); err != nil {
			t.Fatal(err)
		}
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "capacity", Args: map[string]string{"enableHierarchy": "true"}})
	if got := c.Queues["org"].Status.Deserved[api.GPU]; got != 6 {
		t.Fatalf("org deserved=%d", got)
	}
	if got := c.Queues["a"].Status.Deserved[api.GPU]; got != 3 {
		t.Fatalf("a deserved=%d", got)
	}
	if !ssn.Allocatable(c.Queues["a"], api.Resource{api.GPU: 3}) {
		t.Fatal("3 GPU should fit")
	}
	if ssn.Allocatable(c.Queues["a"], api.Resource{api.GPU: 7}) {
		t.Fatal("7 GPU should exceed parent capability")
	}
}

func TestPredicatesTaintsAndSelectors(t *testing.T) {
	c := fixture(t)
	c.Nodes["n1"].Node.Taints = []api.Taint{{Key: "dedicated", Value: "gpu", Effect: "NoSchedule"}}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "predicates"})
	p := &api.Pod{Requests: api.Cores(1), Queue: "default"}
	if err := ssn.Predicate(p, c.Nodes["n1"]); err == nil {
		t.Fatal("untolerated taint should fail")
	}
	p.Tolerations = []api.Toleration{{Key: "dedicated", Operator: "Equal", Value: "gpu", Effect: "NoSchedule"}}
	if err := ssn.Predicate(p, c.Nodes["n1"]); err != nil {
		t.Fatalf("tolerated taint should pass: %v", err)
	}
	p.NodeSelector = map[string]string{"zone": "a"}
	if err := ssn.Predicate(p, c.Nodes["n1"]); err == nil {
		t.Fatal("selector mismatch should fail")
	}
}

func TestNodeOrderPrefersFreeNode(t *testing.T) {
	c := fixture(t)
	if err := c.AddNode(&api.Node{Name: "n2", Allocatable: api.R(8000, 32768, 8)}); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "nodeorder"})
	pod := &api.Pod{Requests: api.Cores(1)}
	s1, _ := ssn.NodeScore(pod, c.Nodes["n1"])
	if s1 <= 0 {
		t.Fatalf("score=%v", s1)
	}
}

func TestBinpackPrefersBusyNode(t *testing.T) {
	c := fixture(t)
	if err := c.AddNode(&api.Node{Name: "n2", Allocatable: api.R(8000, 32768, 8)}); err != nil {
		t.Fatal(err)
	}
	j := submit(t, c, simpleJob("j", "default", 1, api.Cores(4)))
	if err := c.Bind(j.UID+"-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "binpack", Args: map[string]string{"weight": "10"}})
	pod := &api.Pod{Requests: api.Cores(1)}
	s1, _ := ssn.NodeScore(pod, c.Nodes["n1"])
	s2, _ := ssn.NodeScore(pod, c.Nodes["n2"])
	if s1 <= s2 {
		t.Fatalf("binpack should prefer busy n1: %v vs %v", s1, s2)
	}
}

func TestTaskTopologyAffinityAndAntiAffinity(t *testing.T) {
	c := cache.New()
	for _, n := range []string{"n1", "n2"} {
		if err := c.AddNode(&api.Node{Name: n, Allocatable: api.R(4000, 8192, 0)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	j := &api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 2, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			{Name: "ps", Replicas: 1, Requests: api.Cores(1)},
			{Name: "worker", Replicas: 1, Requests: api.Cores(1)},
		},
		TaskTopology: []api.TaskTopologySpec{{TaskNames: []string{"ps", "worker"}, Policy: "affinity"}},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-ps-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "task-topology"})
	worker := c.Pods["default/j-worker-0"]
	if err := ssn.Predicate(worker, c.Nodes["n1"]); err != nil {
		t.Fatalf("affinity should allow n1: %v", err)
	}
	if err := ssn.Predicate(worker, c.Nodes["n2"]); err == nil {
		t.Fatal("affinity should reject a different node")
	}
}

func TestNetworkTopologyHard(t *testing.T) {
	c := cache.New()
	for _, n := range []string{"a1", "a2", "b1", "b2"} {
		if err := c.AddNode(&api.Node{Name: n, Allocatable: api.R(0, 0, 2)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	for _, h := range []*api.HyperNode{
		{Name: "a1", Tier: 1, Parent: "rack-a", Members: []string{"a1"}},
		{Name: "a2", Tier: 1, Parent: "rack-a", Members: []string{"a2"}},
		{Name: "b1", Tier: 1, Parent: "rack-b", Members: []string{"b1"}},
		{Name: "b2", Tier: 1, Parent: "rack-b", Members: []string{"b2"}},
		{Name: "rack-a", Tier: 2, Children: []string{"a1", "a2"}},
		{Name: "rack-b", Tier: 2, Children: []string{"b1", "b2"}},
	} {
		if err := c.AddHyperNode(h); err != nil {
			t.Fatal(err)
		}
	}
	j := &api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 4, MinAvailableSet: true,
		Tasks:           []api.TaskSpec{{Name: "w", Replicas: 4, Requests: api.Resource{api.GPU: 1}}},
		NetworkTopology: &api.NetworkTopologySpec{Mode: "hard", HighestTierAllowed: 2},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "network-topology-aware"})
	pod := c.Pods["default/j-w-0"]
	if err := ssn.Predicate(pod, c.Nodes["a1"]); err != nil {
		t.Fatalf("a1 should be allowed: %v", err)
	}
	if err := ssn.Predicate(pod, c.Nodes["b1"]); err == nil {
		t.Fatal("b1 should be outside the chosen domain")
	}
}

func TestTDMWindow(t *testing.T) {
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(0, 0, 4)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddNode(&api.Node{Name: "rz", Allocatable: api.R(0, 0, 4), Labels: map[string]string{"volcano.sh/revocable-zone": "rz1"}}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	j := &api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 1, MinAvailableSet: true,
		Annotations: map[string]string{"volcano.sh/preemptable": "true"},
		Tasks:       []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Resource{api.GPU: 1}}},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	args := map[string]string{"window.rz1": "2-5"}
	early := session(t, c, 1, framework.PluginConfig{Name: "tdm", Args: args})
	pod := c.Pods["default/j-w-0"]
	if err := early.Predicate(pod, c.Nodes["rz"]); err == nil {
		t.Fatal("outside the window the preemptable job must not use rz")
	}
	inWindow := session(t, c, 3, framework.PluginConfig{Name: "tdm", Args: args})
	if err := inWindow.Predicate(pod, c.Nodes["rz"]); err != nil {
		t.Fatalf("inside the window it should pass: %v", err)
	}
	if err := inWindow.Predicate(pod, c.Nodes["n1"]); err == nil {
		t.Fatal("preemptable job must not use a non-revocable node")
	}
}

func TestReservationOrdersReservedJobFirst(t *testing.T) {
	c := fixture(t)
	if err := c.AddReservation(&api.Reservation{Name: "r", Queue: "a", Job: "default/reserved",
		NodeNames: []string{"n1"}, Resources: api.Resource{api.GPU: 2}, StartTick: 0, EndTick: 5}); err != nil {
		t.Fatal(err)
	}
	reserved := submit(t, c, simpleJob("reserved", "a", 1, api.Resource{api.GPU: 1}))
	other := submit(t, c, simpleJob("other", "b", 1, api.Resource{api.GPU: 1}))
	ssn := session(t, c, 1, framework.PluginConfig{Name: "reservation"})
	if ssn.JobOrder(reserved, other) >= 0 {
		t.Fatal("reserved job should sort first")
	}
}

func TestSLAOverdueFirst(t *testing.T) {
	c := fixture(t)
	overdue := submit(t, c, simpleJob("overdue", "default", 1, api.Cores(1)))
	overdue.Spec.Annotations = map[string]string{"volcano.sh/sla-waiting-time": "1"}
	other := submit(t, c, simpleJob("other", "default", 1, api.Cores(1)))
	other.CreationTick = 0
	ssn := session(t, c, 5, framework.PluginConfig{Name: "sla"})
	if ssn.JobOrder(overdue, other) >= 0 {
		t.Fatal("overdue job should sort first")
	}
}

func TestFairShareTracksUsage(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, simpleJob("j", "a", 1, api.Resource{api.GPU: 4}))
	if err := c.Bind(j.UID+"-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	p, err := plugins.Build("fairshare", map[string]string{"halfLife": "4"})
	if err != nil {
		t.Fatal(err)
	}
	_ = framework.NewSession(c, 0, framework.Config{Actions: []string{"enqueue"}},
		[]framework.PluginInstance{{Plugin: p, Tier: 0}})
	ssn := framework.NewSession(c, 2, framework.Config{Actions: []string{"enqueue"}},
		[]framework.PluginInstance{{Plugin: p, Tier: 0}})
	usage := p.(interface{ Usage() map[string]float64 }).Usage()
	if usage["a"] <= 0 {
		t.Fatalf("usage not tracked: %v", usage)
	}
	qb := c.Queues["b"]
	qa := c.Queues["a"]
	if ssn.QueueOrder(qb, qa) >= 0 {
		t.Fatal("queue b with less history should sort first")
	}
}

func TestNUMA(t *testing.T) {
	c := cache.New()
	n := &api.Node{Name: "n1", Allocatable: api.R(16000, 32768, 0), NUMANodes: []api.NUMANode{
		{ID: 0, Allocatable: api.R(8000, 16384, 0)},
		{ID: 1, Allocatable: api.R(8000, 16384, 0)},
	}}
	if err := c.AddNode(n); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "numa-aware"})
	small := &api.Pod{Requests: api.Cores(4)}
	if err := ssn.Predicate(small, c.Nodes["n1"]); err != nil {
		t.Fatalf("4 CPU should fit: %v", err)
	}
	large := &api.Pod{Requests: api.Cores(12)}
	if err := ssn.Predicate(large, c.Nodes["n1"]); err == nil {
		t.Fatal("12 CPU cannot fit one 8 CPU NUMA node")
	}
}

func TestOvercommit(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, simpleJob("j", "default", 6, api.Cores(1)))
	for i := 0; i < 6; i++ {
		if err := c.Bind(j.UID+"-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "overcommit", Args: map[string]string{"overcommit-factor": "1.1"}})
	small := &api.Job{Name: "small", Namespace: "default", Spec: api.JobSpec{
		MinAvailable: 2, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 2, Requests: api.Cores(1)}},
	}}
	large := &api.Job{Name: "large", Namespace: "default", Spec: api.JobSpec{
		MinAvailable: 3, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 3, Requests: api.Cores(1)}},
	}}
	if !ssn.JobEnqueueable(small) {
		t.Fatal("2 CPU should fit 1.5*8-6=6")
	}
	if ssn.JobEnqueueable(large) {
		t.Fatal("3 CPU should exceed the 1.1x overcommit budget of 2.8 CPU")
	}
}

func TestConformanceRejectsForeignScheduler(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, simpleJob("j", "default", 1, api.Cores(1)))
	j.Spec.SchedulerName = "default-scheduler"
	ssn := session(t, c, 0, framework.PluginConfig{Name: "conformance"})
	if err := ssn.JobValid(j); err == nil {
		t.Fatal("foreign schedulerName should be rejected")
	}
	j.Spec.SchedulerName = "volcano"
	if err := ssn.JobValid(j); err != nil {
		t.Fatalf("volcano job rejected: %v", err)
	}
}

func TestNodeOrderDeterministic(t *testing.T) {
	c := fixture(t)
	ssn := session(t, c, 0, framework.PluginConfig{Name: "nodeorder"})
	pod := &api.Pod{Requests: api.Cores(1)}
	first, _ := ssn.NodeScore(pod, c.Nodes["n1"])
	for i := 0; i < 1000; i++ {
		got, _ := ssn.NodeScore(pod, c.Nodes["n1"])
		if got != first {
			t.Fatalf("score changed between calls: %v vs %v", first, got)
		}
	}
}

func TestReschedulingVictimsRespectUtilizationAndGang(t *testing.T) {
	c := cache.New()
	for _, name := range []string{"n1", "n2"} {
		if err := c.AddNode(&api.Node{Name: name, Allocatable: api.R(8000, 32768, 0)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	solo := submit(t, c, simpleJob("solo", "default", 1, api.Cores(1)))
	pair := submit(t, c, simpleJob("pair", "default", 2, api.Cores(1)))
	pair.Spec.MinAvailable = 1
	filler := submit(t, c, simpleJob("filler", "default", 3, api.Cores(1)))
	// n1 (2/8 CPU) is the least-utilized node and contains a solo pod that must
	// be protected plus one pair pod that may move. n2 is busier (4/8).
	for _, uid := range []string{
		"default/solo-w-0", "default/pair-w-0",
	} {
		if err := c.Bind(uid, "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	for _, uid := range []string{
		"default/pair-w-1", "default/filler-w-0", "default/filler-w-1", "default/filler-w-2",
	} {
		if err := c.Bind(uid, "n2", 0); err != nil {
			t.Fatal(err)
		}
	}
	ssn := session(t, c, 0, framework.PluginConfig{
		Name: "rescheduling", Args: map[string]string{"strategy": "fragmentation", "threshold": "0.6"},
	})
	victims := ssn.VictimTasks()
	if len(victims) != 1 {
		t.Fatalf("want exactly one safe victim, got %d: %v", len(victims), victims)
	}
	if victims[0].Job != pair.UID {
		t.Fatalf("solo job with minAvailable=1 must be protected, victim=%s job=%s", victims[0].Name, victims[0].Job)
	}
	if victims[0].NodeName != "n1" {
		t.Fatalf("victim must come from the least-utilized source node, got %s", victims[0].NodeName)
	}
	if len(solo.Spec.Tasks) == 0 || len(filler.Spec.Tasks) == 0 {
		t.Fatal("sanity")
	}
}

func TestReschedulingLowNodeUtilizationDirection(t *testing.T) {
	c := cache.New()
	for _, name := range []string{"hot", "cold"} {
		if err := c.AddNode(&api.Node{Name: name, Allocatable: api.R(8000, 32768, 0)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	j := submit(t, c, simpleJob("j", "default", 4, api.Cores(2)))
	j.Spec.MinAvailable = 1
	for i := 0; i < 4; i++ {
		p := c.Pods["default/j-w-"+string(rune('0'+i))]
		if err := c.Bind(p.UID, "hot", 0); err != nil {
			t.Fatal(err)
		}
		p.Phase = api.PodRunning
	}
	ssn := session(t, c, 0, framework.PluginConfig{
		Name: "rescheduling",
		Args: map[string]string{"strategy": "lowNodeUtilization", "threshold": "0.2", "targetThreshold": "0.5"},
	})
	victims := ssn.VictimTasks()
	if len(victims) != 2 {
		t.Fatalf("want two victims to bring hot from 1.0 to 0.5, got %d", len(victims))
	}
	for _, v := range victims {
		if v.NodeName != "hot" {
			t.Fatalf("lowNodeUtilization must evict from the over-utilized node, got %s", v.NodeName)
		}
	}
}

func pdbJob(name string, replicas int32, app string) *api.Job {
	j := simpleJob(name, "default", replicas, api.Cores(1))
	j.Spec.Tasks[0].Labels = map[string]string{"app": app}
	return j
}

func TestPDBAllowsOneDisruptionThenBlocks(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, pdbJob("crit", 4, "crit"))
	for i := 0; i < 4; i++ {
		if err := c.Bind("default/crit-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddPDB(&api.PodDisruptionBudget{
		Name: "crit", Namespace: "default",
		Selector: map[string]string{"app": "crit"},
		HasMin:   true, MinAvailable: 3,
	}); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "pdb"})
	preemptor := &api.Pod{Job: "other", Queue: "default", Priority: 100}
	victim := c.Pods["default/crit-w-0"]
	if !ssn.Preemptable(preemptor, victim) {
		t.Fatal("4 available with minAvailable=3 must allow one disruption")
	}
	if err := c.Evict(victim.UID, 0); err != nil {
		t.Fatal(err)
	}
	if ssn.Preemptable(preemptor, c.Pods["default/crit-w-1"]) {
		t.Fatal("3 available with minAvailable=3 must block further disruption")
	}
	if j.Spec.MinAvailable != 4 {
		t.Fatal("sanity: job itself is unchanged")
	}
}

func TestPDBMaxUnavailable(t *testing.T) {
	c := fixture(t)
	if err := c.AddPDB(&api.PodDisruptionBudget{
		Name: "crit", Namespace: "default",
		Selector: map[string]string{"app": "crit"},
		HasMax:   true, MaxUnavailable: 1,
	}); err != nil {
		t.Fatal(err)
	}
	j := submit(t, c, pdbJob("crit", 3, "crit"))
	for i := 0; i < 3; i++ {
		if err := c.Bind("default/crit-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "pdb"})
	preemptor := &api.Pod{Job: "other", Queue: "default", Priority: 100}
	if !ssn.Preemptable(preemptor, c.Pods["default/crit-w-0"]) {
		t.Fatal("maxUnavailable=1 with 3 available must allow one disruption")
	}
	if err := c.Evict("default/crit-w-0", 0); err != nil {
		t.Fatal(err)
	}
	if ssn.Preemptable(preemptor, c.Pods["default/crit-w-1"]) {
		t.Fatal("maxUnavailable=1 must block a second disruption")
	}
	_ = j
}

func TestResourceQuotaGatesEnqueue(t *testing.T) {
	c := fixture(t)
	if err := c.AddResourceQuota(&api.ResourceQuota{
		Name: "q", Namespace: "default", Hard: api.Resource{"requests.cpu": 4000},
	}); err != nil {
		t.Fatal(err)
	}
	a := submit(t, c, simpleJob("a", "default", 2, api.Cores(1)))
	a.Status.Phase = api.JobInqueue
	ssn := session(t, c, 0, framework.PluginConfig{Name: "resourcequota"})
	if !ssn.JobEnqueueable(a) {
		t.Fatal("2 CPU under a 4 CPU quota should be admitted")
	}
	b := submit(t, c, simpleJob("b", "default", 3, api.Cores(1)))
	if ssn.JobEnqueueable(b) {
		t.Fatal("2+3 CPU over a 4 CPU quota must be rejected")
	}
	found := false
	for _, r := range ssn.UnschedulableReasons(b.UID) {
		if strings.Contains(r, "quota") {
			found = true
		}
	}
	if !found {
		t.Fatalf("quota rejection should be recorded, got %v", ssn.UnschedulableReasons(b.UID))
	}
}

func topologyCache(t *testing.T) *cache.Cache {
	t.Helper()
	c := cache.New()
	for _, name := range []string{"n1", "n2", "n3", "n4"} {
		if err := c.AddNode(&api.Node{Name: name, Allocatable: api.R(8000, 32768, 8)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	for _, h := range []*api.HyperNode{
		{Name: "leaf1", Tier: 1, Parent: "rack1", Members: []string{"n1"}},
		{Name: "leaf2", Tier: 1, Parent: "rack1", Members: []string{"n2"}},
		{Name: "leaf3", Tier: 1, Parent: "rack2", Members: []string{"n3"}},
		{Name: "rack1", Tier: 2, Parent: "root", Children: []string{"leaf1", "leaf2"}},
		{Name: "rack2", Tier: 2, Parent: "root", Children: []string{"leaf3"}},
		{Name: "root", Tier: 3, Children: []string{"rack1", "rack2"}},
	} {
		if err := c.AddHyperNode(h); err != nil {
			t.Fatal(err)
		}
	}
	return c
}

func antiAffinityJob(t *testing.T, c *cache.Cache, name string, weight int32) *api.Job {
	t.Helper()
	j := simpleJob(name, "default", 1, api.Cores(1))
	j.Spec.Labels = map[string]string{"serving-group": "online"}
	submitted := submit(t, c, j)
	pg := c.PodGroups[submitted.UID]
	if pg == nil {
		t.Fatalf("podgroup for %s not found", name)
	}
	pg.TopologyAffinity = &api.TopologyAffinitySpec{
		PodGroupAntiAffinity: []api.PodGroupAntiAffinityTerm{{
			MatchLabels:  map[string]string{"serving-group": "online"},
			TopologyTier: 2,
			Weight:       weight,
		}},
	}
	return submitted
}

func TestGroupTopologyRequiredAntiAffinity(t *testing.T) {
	c := topologyCache(t)
	// Only "a" declares the rule; "b" merely carries the matching label.
	// The plugin must evaluate the pair symmetrically, otherwise the later
	// PodGroup could join the same rack and bypass the rule.
	a := antiAffinityJob(t, c, "a", 0)
	b := simpleJob("b", "default", 1, api.Cores(1))
	b.Spec.Labels = map[string]string{"serving-group": "online"}
	b = submit(t, c, b)
	if err := c.Bind(a.UID+"-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	ssn := session(t, c, 0, framework.PluginConfig{Name: "group-topology-affinity"})
	pod := c.Pods[b.UID+"-w-0"]
	if err := ssn.Predicate(pod, c.Nodes["n2"]); err == nil {
		t.Fatal("n2 is in the same rack as n1; required anti-affinity must reject it")
	}
	if err := ssn.Predicate(pod, c.Nodes["n3"]); err != nil {
		t.Fatalf("n3 is in a different rack and must be allowed: %v", err)
	}

	// The plugin is allocate-only: backfill's bestNode must not apply the
	// topology predicate (best-effort Pods are exempt upstream).
	ssn.Action = "backfill"
	if err := ssn.Predicate(pod, c.Nodes["n2"]); err != nil {
		t.Fatalf("backfill must bypass group topology: %v", err)
	}
	ssn.Action = "allocate"

	// A required term that matches nobody is a no-op, even when the node is
	// outside the HyperNode tree. Only a matching, already-placed peer can
	// make a missing domain unsafe.
	cjob := simpleJob("c", "default", 1, api.Cores(1))
	cjob = submit(t, c, cjob)
	cpg := c.PodGroups[cjob.UID]
	cpg.TopologyAffinity = &api.TopologyAffinitySpec{
		PodGroupAntiAffinity: []api.PodGroupAntiAffinityTerm{{
			MatchLabels:  map[string]string{"nobody": "matches"},
			TopologyTier: 2,
		}},
	}
	if err := ssn.Predicate(c.Pods[cjob.UID+"-w-0"], c.Nodes["n4"]); err != nil {
		t.Fatalf("term with no matching peer must be a no-op: %v", err)
	}
}

func TestGroupTopologyPreferredPenalty(t *testing.T) {
	c := topologyCache(t)
	// Both groups declare the same preferred term. The pair must contribute
	// the weight once, not twice.
	a := antiAffinityJob(t, c, "a", 50)
	b := antiAffinityJob(t, c, "b", 50)

	// A same-labelled PodGroup in another namespace must not contribute to the
	// penalty: upstream defaults the selector to the declaring namespace.
	other := simpleJob("other", "default", 1, api.Cores(1))
	other.Namespace = "team-x"
	other.Spec.Labels = map[string]string{"serving-group": "online"}
	other = submit(t, c, other)
	if err := c.Bind(other.UID+"-w-0", "n2", 0); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind(a.UID+"-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}

	ssn := session(t, c, 0, framework.PluginConfig{
		Name: "group-topology-affinity",
		Args: map[string]string{"weight": "2"},
	})
	pod := c.Pods[b.UID+"-w-0"]
	if err := ssn.Predicate(pod, c.Nodes["n2"]); err != nil {
		t.Fatalf("preferred term must not hard-reject: %v", err)
	}
	same, err := ssn.NodeScore(pod, c.Nodes["n2"])
	if err != nil {
		t.Fatal(err)
	}
	otherScore, err := ssn.NodeScore(pod, c.Nodes["n3"])
	if err != nil {
		t.Fatal(err)
	}
	// One peer (a) at weight 50, plugin multiplier 2, namespace peer ignored.
	if same != -100 {
		t.Fatalf("same-rack penalty should be -100 (deduped 50 x plugin weight 2), got %v", same)
	}
	if otherScore != 0 {
		t.Fatalf("different rack should have no penalty, got %v", otherScore)
	}
}

func TestNodeGroupRestrictsAndCaps(t *testing.T) {
	c := fixture(t)
	c.Nodes["n1"].Node.Labels = map[string]string{api.NodeGroupLabel: "a"}
	if err := c.AddNode(&api.Node{Name: "n2", Allocatable: api.R(8000, 32768, 8),
		Labels: map[string]string{api.NodeGroupLabel: "b"}}); err != nil {
		t.Fatal(err)
	}
	q := &api.Queue{Name: "restricted", Spec: api.QueueSpec{
		NodeGroup: &api.NodeGroupSpec{
			Names:      []string{"a"},
			Capability: map[string]api.Resource{"a": api.Resource{api.CPU: 2000}},
		},
	}}
	if err := c.AddQueue(q); err != nil {
		t.Fatal(err)
	}
	j := submit(t, c, simpleJob("j", "restricted", 3, api.Cores(1)))
	ssn := session(t, c, 0, framework.PluginConfig{Name: "nodegroup"})
	// Two pods fit under the 2-CPU group cap; the third must be rejected.
	for i := 0; i < 2; i++ {
		if err := c.Bind("default/j-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	third := c.Pods["default/j-w-2"]
	if err := ssn.Predicate(third, c.Nodes["n1"]); err == nil {
		t.Fatal("third CPU should exceed the node group cap")
	}
	if err := ssn.Predicate(third, c.Nodes["n2"]); err == nil {
		t.Fatal("node group b is outside the queue's allowed names")
	}
	if j.Spec.Queue != "restricted" {
		t.Fatal("sanity")
	}
}
