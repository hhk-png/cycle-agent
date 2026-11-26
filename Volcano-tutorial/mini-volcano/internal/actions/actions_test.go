package actions_test

import (
	"testing"

	"example.com/minivolcano/internal/actions"
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/plugins"
)

func testCache(t *testing.T, gpus int64) *cache.Cache {
	t.Helper()
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(16000, 65536, gpus)}); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{"default", "a", "b"} {
		if err := c.AddQueue(&api.Queue{Name: q}); err != nil {
			t.Fatal(err)
		}
	}
	return c
}

func job(name, queue string, priority, replicas int32, req api.Resource, duration int64) *api.Job {
	return &api.Job{Name: name, Namespace: "default", Spec: api.JobSpec{
		Queue: queue, Priority: priority, MaxRetry: 3,
		MinAvailable: replicas, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: replicas, Requests: req, Duration: duration}},
	}}
}

func openSession(t *testing.T, c *cache.Cache, names ...string) *framework.Session {
	t.Helper()
	var instances []framework.PluginInstance
	for i, name := range names {
		p, err := plugins.Build(name, nil)
		if err != nil {
			t.Fatal(err)
		}
		instances = append(instances, framework.PluginInstance{Plugin: p, Tier: i})
	}
	return framework.NewSession(c, 0, framework.Config{Actions: []string{"enqueue", "allocate", "preempt", "reclaim", "backfill"}}, instances)
}

func run(t *testing.T, ssn *framework.Session, name string) actions.Action {
	t.Helper()
	a, err := actions.Build(name)
	if err != nil {
		t.Fatal(err)
	}
	if err := a.Execute(ssn); err != nil {
		t.Fatal(err)
	}
	return a
}

func markInqueue(j *api.Job, pg *api.PodGroup) {
	j.Status.Phase = api.JobInqueue
	pg.Phase = api.PodGroupInqueue
}

func TestEnqueueOpenAndClosed(t *testing.T) {
	c := testCache(t, 4)
	j := job("j", "default", 0, 2, api.Resource{api.GPU: 1}, 10)
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	c.Queues["default"].Spec.State = api.QueueClosed
	c.Queues["default"].Status.State = api.QueueClosed
	ssn := openSession(t, c, "gang", "proportion", "predicates")
	run(t, ssn, "enqueue")
	if ssn.Cache.Jobs["default/j"].Status.Phase == api.JobInqueue {
		t.Fatal("closed queue must not enqueue")
	}
	ssn.Cache.Queues["default"].Status.State = api.QueueOpen
	run(t, ssn, "enqueue")
	if ssn.Cache.Jobs["default/j"].Status.Phase != api.JobInqueue {
		t.Fatalf("phase=%s", ssn.Cache.Jobs["default/j"].Status.Phase)
	}
}

func TestAllocateGangCommit(t *testing.T) {
	c := testCache(t, 4)
	if err := c.SubmitJob(job("j", "default", 0, 4, api.Resource{api.GPU: 1}, 10)); err != nil {
		t.Fatal(err)
	}
	ssn := openSession(t, c, "gang", "priority", "proportion", "predicates", "nodeorder")
	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	for _, p := range ssn.Cache.JobPods("default/j") {
		if p.Phase != api.PodAllocated {
			t.Fatalf("pod %s phase=%s", p.Name, p.Phase)
		}
	}
}

func TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas(t *testing.T) {
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.Cores(4)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	gate := func(v int32) *int32 { return &v }
	j := &api.Job{Name: "roles", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 3, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			// a has the smaller gate but more replicas; filling all of a
			// first would starve b's gate even though a1+b2 fits.
			{Name: "a", Replicas: 4, MinAvailable: gate(1), Requests: api.Cores(1)},
			{Name: "b", Replicas: 4, MinAvailable: gate(2), Requests: api.Cores(1)},
		},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	ssn := openSession(t, c, "gang", "priority", "proportion", "predicates", "nodeorder")
	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	counts := ssn.Cache.ReadyCountByTask("default/roles")
	if counts["a"] < 1 || counts["b"] < 2 {
		t.Fatalf("role gates not both satisfied: ready=%v", counts)
	}
	if total := ssn.Cache.ReadyCount("default/roles"); total < 3 {
		t.Fatalf("job total ready=%d, want >= 3", total)
	}
}

func TestAllocateGangRollback(t *testing.T) {
	c := testCache(t, 4)
	if err := c.SubmitJob(job("j", "default", 0, 6, api.Resource{api.GPU: 1}, 10)); err != nil {
		t.Fatal(err)
	}
	ssn := openSession(t, c, "gang", "priority", "proportion", "predicates", "nodeorder")
	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	for _, p := range ssn.Cache.JobPods("default/j") {
		if p.Phase != api.PodPending {
			t.Fatalf("rollback leaked pod %s phase=%s", p.Name, p.Phase)
		}
	}
	if got := ssn.Cache.QueueAllocated("default")[api.GPU]; got != 0 {
		t.Fatalf("queue allocated=%d", got)
	}
}

func TestAllocateCompletesPartialGang(t *testing.T) {
	c := testCache(t, 4)
	j := job("j", "default", 0, 4, api.Resource{api.GPU: 1}, 10)
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := c.Bind("default/j-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	ssn := openSession(t, c, "gang", "priority", "proportion", "predicates", "nodeorder")
	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	allocated := 0
	for _, p := range ssn.Cache.JobPods("default/j") {
		if p.Phase == api.PodAllocated {
			allocated++
		}
	}
	if allocated != 4 {
		t.Fatalf("expected the full gang allocated (2 prior + 2 new), got %d", allocated)
	}
}

func TestAllocateRespectsQueueCapability(t *testing.T) {
	c := testCache(t, 8)
	c.Queues["default"].Spec.Capability = api.Resource{api.GPU: 2}
	if err := c.SubmitJob(job("j", "default", 0, 2, api.Resource{api.GPU: 1}, 10)); err != nil {
		t.Fatal(err)
	}
	if err := c.SubmitJob(job("too-big", "default", 0, 3, api.Resource{api.GPU: 1}, 10)); err != nil {
		t.Fatal(err)
	}
	ssn := openSession(t, c, "gang", "priority", "proportion", "predicates", "nodeorder")
	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	if got := ssn.Cache.QueueAllocated("default")[api.GPU]; got != 2 {
		t.Fatalf("capability exceeded: %d", got)
	}
	for _, p := range ssn.Cache.JobPods("default/too-big") {
		if p.Phase != api.PodPending {
			t.Fatalf("too-big job should have rolled back, pod=%s phase=%s", p.Name, p.Phase)
		}
	}
}

func TestPreemptOnlySameQueue(t *testing.T) {
	c := testCache(t, 8)
	low := job("low", "a", 0, 8, api.Resource{api.GPU: 1}, 100)
	if err := c.SubmitJob(low); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 8; i++ {
		if err := c.Bind("default/low-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	high := job("high", "b", 100, 4, api.Resource{api.GPU: 1}, 100)
	if err := c.SubmitJob(high); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/high"], c.PodGroups["default/high"])
	ssn := openSession(t, c, "priority", "gang", "proportion", "predicates", "nodeorder")
	run(t, ssn, "preempt")
	if got := ssn.Cache.QueueAllocated("a")[api.GPU]; got != 8 {
		t.Fatalf("cross-queue preemption happened: a allocated %d", got)
	}
}

func TestPreemptSameQueueEvicts(t *testing.T) {
	c := testCache(t, 8)
	low := job("low", "a", 0, 8, api.Resource{api.GPU: 1}, 100)
	if err := c.SubmitJob(low); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 8; i++ {
		if err := c.Bind("default/low-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	high := job("high", "a", 100, 4, api.Resource{api.GPU: 1}, 100)
	if err := c.SubmitJob(high); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/high"], c.PodGroups["default/high"])
	ssn := openSession(t, c, "priority", "gang", "proportion", "predicates", "nodeorder")
	run(t, ssn, "preempt")
	if got := ssn.Cache.QueueAllocated("a")[api.GPU]; got != 8 {
		t.Fatalf("total queue allocation should remain 8, got %d", got)
	}
	highAllocated := 0
	for _, p := range ssn.Cache.JobPods("default/high") {
		if p.Phase == api.PodAllocated {
			highAllocated++
		}
	}
	if highAllocated != 4 {
		t.Fatalf("high allocated=%d", highAllocated)
	}
}

func TestReclaimStopsAtDeserved(t *testing.T) {
	c := testCache(t, 8)
	qa := c.Queues["a"]
	qb := c.Queues["b"]
	qa.Spec.Weight, qb.Spec.Weight = 1, 1
	aj := job("a-job", "a", 0, 8, api.Resource{api.GPU: 1}, 100)
	if err := c.SubmitJob(aj); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 8; i++ {
		if err := c.Bind("default/a-job-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	bj := job("b-job", "b", 0, 4, api.Resource{api.GPU: 1}, 100)
	if err := c.SubmitJob(bj); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/b-job"], c.PodGroups["default/b-job"])
	ssn := openSession(t, c, "priority", "gang", "proportion", "predicates", "nodeorder")
	run(t, ssn, "reclaim")
	if got := ssn.Cache.QueueAllocated("a")[api.GPU]; got != 4 {
		t.Fatalf("a should be reclaimed to deserved 4, got %d", got)
	}
	if got := ssn.Cache.QueueAllocated("b")[api.GPU]; got != 4 {
		t.Fatalf("b allocated=%d", got)
	}
}

func TestBackfillBestEffort(t *testing.T) {
	c := testCache(t, 0)
	j := job("be", "default", 0, 2, api.Resource{}, 100)
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/be"], c.PodGroups["default/be"])
	ssn := openSession(t, c, "gang", "priority", "predicates")
	run(t, ssn, "backfill")
	for _, p := range ssn.Cache.JobPods("default/be") {
		if p.Phase != api.PodAllocated {
			t.Fatalf("pod %s not backfilled: %s", p.Name, p.Phase)
		}
	}
}

func TestActionRegistry(t *testing.T) {
	for _, name := range actions.Names() {
		if _, err := actions.Build(name); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if _, err := actions.Build("ghost"); err == nil {
		t.Fatal("unknown action should fail")
	}
}

// Regression: evicting victims is only useful if the preemptor actually binds a
// pending pod. ReadyCount can already be satisfied by a pre-existing Allocated
// pod, so "ready >= minAvailable" alone is not enough to commit.
func TestPreemptDoesNotEvictWhenPreemptorCannotBind(t *testing.T) {
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(8000, 16384, 0), Labels: map[string]string{"zone": "a"}}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddNode(&api.Node{Name: "n2", Allocatable: api.R(2000, 4096, 0), Labels: map[string]string{"zone": "b"}}); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{"default", "other"} {
		if err := c.AddQueue(&api.Queue{Name: q}); err != nil {
			t.Fatal(err)
		}
	}
	low := job("low", "default", 0, 2, api.Cores(1), 100)
	if err := c.SubmitJob(low); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := c.Bind("default/low-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	blocker := job("blocker", "other", 100, 2, api.Cores(1), 100)
	if err := c.SubmitJob(blocker); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := c.Bind("default/blocker-w-"+string(rune('0'+i)), "n2", 0); err != nil {
			t.Fatal(err)
		}
	}
	high := job("high", "default", 100, 2, api.Cores(1), 100)
	high.Spec.MinAvailable = 1
	high.Spec.Tasks[0].NodeSelector = map[string]string{"zone": "b"}
	if err := c.SubmitJob(high); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/high-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/high"], c.PodGroups["default/high"])
	ssn := openSession(t, c, "priority", "gang", "proportion", "predicates", "nodeorder")
	run(t, ssn, "preempt")
	for i := 0; i < 2; i++ {
		p := ssn.Cache.Pods["default/low-w-"+string(rune('0'+i))]
		if p.Phase != api.PodAllocated {
			t.Fatalf("low victim %s was evicted without a successful preemptor binding: %s", p.Name, p.Phase)
		}
	}
	if p := ssn.Cache.Pods["default/high-w-1"]; p.Phase != api.PodPending {
		t.Fatalf("pinned high pod should remain Pending, got %s", p.Phase)
	}
}

func TestGangDisabledJobPartiallyAdmits(t *testing.T) {
	c := testCache(t, 4)
	c.Queues["default"].Spec.Capability = api.Resource{api.GPU: 1}
	j := job("j", "default", 0, 4, api.Resource{api.GPU: 1}, 100)
	j.Spec.MinAvailable = 0
	j.Spec.MinAvailableSet = true
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	ssn := openSession(t, c, "gang", "priority", "proportion", "predicates", "nodeorder")
	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	if got := ssn.Cache.QueueAllocated("default")[api.GPU]; got != 1 {
		t.Fatalf("minAvailable=0 job should admit a subset, allocated=%d", got)
	}
}

func TestShuffleEvictsThenAllocateRepacks(t *testing.T) {
	c := cache.New()
	for _, name := range []string{"node-a", "node-b"} {
		if err := c.AddNode(&api.Node{Name: name, Allocatable: api.R(8000, 32768, 0)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"left", "right"} {
		j := job(name, "default", 0, 2, api.Cores(2), 100)
		j.Spec.MinAvailable = 1
		if err := c.SubmitJob(j); err != nil {
			t.Fatal(err)
		}
	}
	placements := map[string]string{
		"default/left-w-0":  "node-a",
		"default/left-w-1":  "node-b",
		"default/right-w-0": "node-a",
		"default/right-w-1": "node-b",
	}
	for uid, node := range placements {
		if err := c.Bind(uid, node, 0); err != nil {
			t.Fatal(err)
		}
	}

	var instances []framework.PluginInstance
	for i, spec := range []framework.PluginConfig{
		{Name: "gang"}, {Name: "priority"}, {Name: "predicates"},
		{Name: "binpack", Args: map[string]string{"weight": "10"}},
		{Name: "rescheduling", Args: map[string]string{"strategy": "fragmentation", "threshold": "0.6"}},
	} {
		p, err := plugins.Build(spec.Name, spec.Args)
		if err != nil {
			t.Fatal(err)
		}
		instances = append(instances, framework.PluginInstance{Plugin: p, Tier: i / 2, Args: spec.Args})
	}
	cfg := framework.Config{Actions: []string{"enqueue", "allocate", "shuffle", "backfill"}}
	ssn := framework.NewSession(c, 0, cfg, instances)

	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	run(t, ssn, "shuffle")

	pending := 0
	for _, p := range ssn.Cache.Pods {
		if p.Phase == api.PodPending {
			pending++
		}
	}
	if pending != 2 {
		t.Fatalf("shuffle should evict the two pods on the least-utilized node, pending=%d", pending)
	}
	sawShuffle := false
	for _, e := range ssn.Events {
		if e.Type == "Shuffled" {
			sawShuffle = true
		}
	}
	if !sawShuffle {
		t.Fatal("shuffle did not record an event")
	}

	// The next session's allocate must repack the evicted pods onto the node
	// that is already busy, leaving the other node empty.
	run(t, ssn, "allocate")
	used := map[string]int64{}
	for _, p := range ssn.Cache.Pods {
		if p.Phase.OccupiesNode() {
			used[p.NodeName] += p.Requests[api.CPU]
		}
	}
	if used["node-a"]+used["node-b"] != 8000 {
		t.Fatalf("total allocated CPU must stay 8000, got %v", used)
	}
	if !(used["node-a"] == 0 || used["node-b"] == 0) {
		t.Fatalf("shuffle+binpack should empty one node, got %v", used)
	}
}

func TestShuffleSkipsWhenNoBetterTarget(t *testing.T) {
	c := cache.New()
	for _, name := range []string{"node-a", "node-b"} {
		if err := c.AddNode(&api.Node{Name: name, Allocatable: api.R(8000, 32768, 0)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	j := job("j", "default", 0, 2, api.Cores(2), 100)
	j.Spec.MinAvailable = 1
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	// Both replicas on node-a: after one eviction node-b is still colder, so
	// binpack would put the pod straight back. Shuffle must refuse to evict.
	for _, uid := range []string{"default/j-w-0", "default/j-w-1"} {
		if err := c.Bind(uid, "node-a", 0); err != nil {
			t.Fatal(err)
		}
	}

	var instances []framework.PluginInstance
	for i, spec := range []framework.PluginConfig{
		{Name: "gang"}, {Name: "priority"}, {Name: "predicates"},
		{Name: "binpack", Args: map[string]string{"weight": "10"}},
		{Name: "rescheduling", Args: map[string]string{"strategy": "fragmentation", "threshold": "0.6"}},
	} {
		p, err := plugins.Build(spec.Name, spec.Args)
		if err != nil {
			t.Fatal(err)
		}
		instances = append(instances, framework.PluginInstance{Plugin: p, Tier: i / 2, Args: spec.Args})
	}
	ssn := framework.NewSession(c, 0, framework.Config{Actions: []string{"enqueue", "allocate", "shuffle"}}, instances)
	run(t, ssn, "enqueue")
	run(t, ssn, "shuffle")
	for _, p := range ssn.Cache.Pods {
		if p.Phase != api.PodAllocated {
			t.Fatalf("shuffle should not evict without a better target, pod %s is %s", p.Name, p.Phase)
		}
	}
	for _, e := range ssn.Events {
		if e.Type == "Shuffled" {
			t.Fatal("unexpected Shuffled event")
		}
	}
}

func TestReclaimNeverBreaksGuarantee(t *testing.T) {
	c := testCache(t, 8)
	c.Queues["a"].Spec.Weight = 1
	qb := c.Queues["b"]
	qb.Spec.Weight = 1
	qb.Spec.Guarantee = api.Resource{api.GPU: 4}

	// b occupies all 8 GPUs with a single large pod. Its deserved share is 4,
	// but evicting the 8-GPU pod would drop it to 0, below its guarantee.
	bj := job("b-job", "b", 0, 1, api.Resource{api.GPU: 8}, 100)
	if err := c.SubmitJob(bj); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/b-job-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	aj := job("a-job", "a", 0, 1, api.Resource{api.GPU: 1}, 100)
	if err := c.SubmitJob(aj); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/a-job"], c.PodGroups["default/a-job"])
	ssn := openSession(t, c, "priority", "gang", "proportion", "predicates", "nodeorder")
	run(t, ssn, "reclaim")
	if got := ssn.Cache.QueueAllocated("b")[api.GPU]; got != 8 {
		t.Fatalf("reclaim must not evict the only pod and break b's guarantee, got %d", got)
	}
	if got := ssn.Cache.QueueAllocated("a")[api.GPU]; got != 0 {
		t.Fatalf("a-job should remain pending, got %d GPU", got)
	}
}

func TestBackfillDoesNotPartiallyCommitGang(t *testing.T) {
	c := testCache(t, 4)
	j := &api.Job{Name: "best", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 2, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			{Name: "ok", Replicas: 1, Requests: api.Resource{}},
			{Name: "stuck", Replicas: 1, Requests: api.Resource{}, NodeSelector: map[string]string{"missing": "yes"}},
		},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/best"], c.PodGroups["default/best"])
	ssn := openSession(t, c, "gang", "proportion", "predicates")
	run(t, ssn, "backfill")
	for _, p := range ssn.Cache.JobPods("default/best") {
		if p.Phase != api.PodPending {
			t.Fatalf("backfill leaked partial gang pod %s phase=%s", p.Name, p.Phase)
		}
	}
	if got := ssn.Cache.ReadyCount("default/best"); got != 0 {
		t.Fatalf("backfill committed %d ready pods, want 0", got)
	}
}

func TestPreemptRequiresRoleGates(t *testing.T) {
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.Cores(6)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	gate := func(v int32) *int32 { return &v }
	low := job("low", "default", 0, 1, api.Cores(1), 100)
	if err := c.SubmitJob(low); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/low-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	high := &api.Job{Name: "high", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", Priority: 100, MaxRetry: 3, MinAvailable: 6, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			{Name: "a", Replicas: 10, MinAvailable: gate(1), Requests: api.Cores(1)},
			{Name: "b", Replicas: 5, MinAvailable: gate(5), Requests: api.Cores(1)},
		},
	}}
	if err := c.SubmitJob(high); err != nil {
		t.Fatal(err)
	}
	markInqueue(c.Jobs["default/high"], c.PodGroups["default/high"])
	ssn := openSession(t, c, "priority", "gang", "proportion", "predicates", "nodeorder")
	run(t, ssn, "preempt")
	if got := ssn.Cache.QueueAllocated("default")[api.CPU]; got != 1000 {
		t.Fatalf("preempt must not evict without satisfying b's role gate, allocated=%d", got)
	}
	if ssn.Cache.Pods["default/low-w-0"].Phase == api.PodPending {
		t.Fatal("victim was evicted despite the preemptor failing its role gate")
	}
}

func TestAllocateRollsBackWhenRoleUnavailable(t *testing.T) {
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.Cores(3)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	gate := func(v int32) *int32 { return &v }
	j := &api.Job{Name: "roles", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 3, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			{Name: "a", Replicas: 4, MinAvailable: gate(1), Requests: api.Cores(1)},
			// Every b pod is unschedulable, so the total can reach 3 with a
			// alone but b's gate can never be met.
			{Name: "b", Replicas: 2, MinAvailable: gate(2), Requests: api.Cores(1),
				NodeSelector: map[string]string{"missing": "yes"}},
		},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	ssn := openSession(t, c, "gang", "priority", "proportion", "predicates", "nodeorder")
	run(t, ssn, "enqueue")
	run(t, ssn, "allocate")
	if got := ssn.Cache.ReadyCount("default/roles"); got != 0 {
		t.Fatalf("role gate should roll back all bindings, ready=%d", got)
	}
	if got := ssn.Cache.QueueAllocated("default")[api.CPU]; got != 0 {
		t.Fatalf("queue allocated=%d, want 0", got)
	}
}
