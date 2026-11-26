package cache

import (
	"testing"

	"example.com/minivolcano/internal/api"
)

func testCache(t *testing.T) *Cache {
	t.Helper()
	c := New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(4000, 8192, 4)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	return c
}

func jobFixture(name string, replicas int32, req api.Resource) *api.Job {
	return &api.Job{
		Name: name, Namespace: "default",
		Spec: api.JobSpec{
			Queue: "default", MinAvailable: replicas, MinAvailableSet: true, MaxRetry: 2,
			Tasks: []api.TaskSpec{{Name: "worker", Replicas: replicas, Requests: req}},
		},
	}
}

func TestSubmitJobMaterializes(t *testing.T) {
	c := testCache(t)
	if err := c.SubmitJob(jobFixture("j", 3, api.Cores(1))); err != nil {
		t.Fatal(err)
	}
	if len(c.Pods) != 3 {
		t.Fatalf("pods=%d", len(c.Pods))
	}
	pg := c.PodGroups["default/j"]
	if pg == nil || pg.MinMember != 3 {
		t.Fatalf("podgroup: %+v", pg)
	}
	if pg.MinResources[api.CPU] != 3000 {
		t.Fatalf("min resources: %v", pg.MinResources)
	}
	if err := c.SubmitJob(jobFixture("j", 1, api.Cores(1))); err == nil {
		t.Fatal("duplicate job should fail")
	}
}

func TestSubmitJobUnknownQueue(t *testing.T) {
	c := testCache(t)
	j := jobFixture("j", 1, api.Cores(1))
	j.Spec.Queue = "ghost"
	if err := c.SubmitJob(j); err == nil {
		t.Fatal("unknown queue should fail")
	}
}

func TestPriorityClassResolution(t *testing.T) {
	c := testCache(t)
	c.AddPriorityClass("high", 100)
	j := jobFixture("j", 1, api.Cores(1))
	j.Spec.PriorityClassName = "high"
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	if c.Jobs["default/j"].Spec.Priority != 100 {
		t.Fatalf("priority=%d", c.Jobs["default/j"].Spec.Priority)
	}
	j2 := jobFixture("j2", 1, api.Cores(1))
	j2.Spec.PriorityClassName = "missing"
	if err := c.SubmitJob(j2); err == nil {
		t.Fatal("unknown priority class should fail")
	}
}

func TestBindEvictFinishAccounting(t *testing.T) {
	c := testCache(t)
	if err := c.SubmitJob(jobFixture("j", 2, api.Cores(1))); err != nil {
		t.Fatal(err)
	}
	pod := c.Pods["default/j-worker-0"]
	if err := c.Bind(pod.UID, "n1", 0); err != nil {
		t.Fatal(err)
	}
	if got := c.Nodes["n1"].Node.Allocated[api.CPU]; got != 1000 {
		t.Fatalf("allocated=%d", got)
	}
	if got := c.QueueAllocated("default")[api.CPU]; got != 1000 {
		t.Fatalf("queue allocated=%d", got)
	}
	if err := c.Evict(pod.UID, 1); err != nil {
		t.Fatal(err)
	}
	if c.Nodes["n1"].Node.Allocated[api.CPU] != 0 {
		t.Fatal("eviction did not release resources")
	}
	if c.Pods[pod.UID].Phase != api.PodPending || !c.Pods[pod.UID].Evicted {
		t.Fatalf("pod state: %+v", c.Pods[pod.UID])
	}
	if err := c.Bind(pod.UID, "n1", 2); err != nil {
		t.Fatal(err)
	}
	if err := c.Finish(pod.UID, true, 3); err != nil {
		t.Fatal(err)
	}
	if c.Pods[pod.UID].Phase != api.PodSucceeded {
		t.Fatalf("phase=%s", c.Pods[pod.UID].Phase)
	}
	if c.Pods[pod.UID].NodeName != "n1" {
		t.Fatal("completed pod should retain placement history")
	}
	if c.Nodes["n1"].Node.Allocated[api.CPU] != 0 {
		t.Fatal("completed pod should release resources")
	}
	if err := c.Finish(pod.UID, true, 4); err == nil {
		t.Fatal("double finish should fail")
	}
}

func TestBindCapacity(t *testing.T) {
	c := testCache(t)
	if err := c.SubmitJob(jobFixture("j", 1, api.Cores(5))); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-worker-0", "n1", 0); err == nil {
		t.Fatal("binding 5 CPU to a 4 CPU node should fail")
	}
}

func TestCloneIsolation(t *testing.T) {
	c := testCache(t)
	if err := c.SubmitJob(jobFixture("j", 1, api.Cores(1))); err != nil {
		t.Fatal(err)
	}
	clone := c.Clone()
	if err := clone.Bind("default/j-worker-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	if c.Nodes["n1"].Node.Allocated[api.CPU] != 0 {
		t.Fatal("clone mutated the original cache")
	}
	if clone.Nodes["n1"].Node.Allocated[api.CPU] != 1000 {
		t.Fatal("clone binding lost")
	}
	clone.Queues["default"].Spec.Weight = 9
	if c.Queues["default"].Spec.Weight == 9 {
		t.Fatal("queue clone aliased")
	}
}

func TestIdleForReservations(t *testing.T) {
	c := New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(0, 0, 8)}); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{"a", "b"} {
		if err := c.AddQueue(&api.Queue{Name: q}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddReservation(&api.Reservation{
		Name: "r", Queue: "a", NodeNames: []string{"n1"},
		Resources: api.Resource{api.GPU: 4}, StartTick: 1, EndTick: 3,
	}); err != nil {
		t.Fatal(err)
	}
	if got := c.IdleFor("n1", "b", "", 0)[api.GPU]; got != 8 {
		t.Fatalf("inactive reservation should not subtract, idle=%d", got)
	}
	if got := c.IdleFor("n1", "b", "", 1)[api.GPU]; got != 4 {
		t.Fatalf("active reservation idle=%d", got)
	}
	if got := c.IdleFor("n1", "a", "", 1)[api.GPU]; got != 8 {
		t.Fatalf("owning queue should see full node, idle=%d", got)
	}
	if got := c.IdleFor("n1", "b", "", 3)[api.GPU]; got != 8 {
		t.Fatalf("expired reservation idle=%d", got)
	}
}

func TestNUMAAllocation(t *testing.T) {
	c := New()
	n := &api.Node{Name: "n1", Allocatable: api.R(8000, 16384, 0), NUMANodes: []api.NUMANode{
		{ID: 0, Allocatable: api.R(4000, 8192, 0)},
		{ID: 1, Allocatable: api.R(4000, 8192, 0)},
	}}
	if err := c.AddNode(n); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	if err := c.SubmitJob(jobFixture("bad", 1, api.Cores(6))); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/bad-worker-0", "n1", 0); err == nil {
		t.Fatal("6 CPU cannot fit an 8 CPU NUMA node? it should fail only if >8; this test expects failure for 6? fix fixture")
	}
}

func TestNUMAAllocationFits(t *testing.T) {
	c := New()
	n := &api.Node{Name: "n1", Allocatable: api.R(8000, 16384, 0), NUMANodes: []api.NUMANode{
		{ID: 0, Allocatable: api.R(4000, 8192, 0)},
		{ID: 1, Allocatable: api.R(4000, 8192, 0)},
	}}
	if err := c.AddNode(n); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	if err := c.SubmitJob(jobFixture("j", 2, api.Cores(4))); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-worker-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	if c.Pods["default/j-worker-0"].NUMANode != 0 {
		t.Fatalf("numa=%d", c.Pods["default/j-worker-0"].NUMANode)
	}
	if err := c.Bind("default/j-worker-1", "n1", 0); err != nil {
		t.Fatal(err)
	}
	if c.Pods["default/j-worker-1"].NUMANode != 1 {
		t.Fatalf("numa=%d", c.Pods["default/j-worker-1"].NUMANode)
	}
}

func TestHyperNodeTree(t *testing.T) {
	c := New()
	for _, h := range []*api.HyperNode{
		{Name: "leaf1", Tier: 1, Parent: "rack1", Members: []string{"n1"}},
		{Name: "leaf2", Tier: 1, Parent: "rack1", Members: []string{"n2"}},
		{Name: "rack1", Tier: 2, Parent: "root", Children: []string{"leaf1", "leaf2"}},
	} {
		if err := c.AddHyperNode(h); err != nil {
			t.Fatal(err)
		}
	}
	members := c.HyperNodeMembers("rack1")
	if len(members) != 2 || members[0] != "n1" || members[1] != "n2" {
		t.Fatalf("members=%v", members)
	}
	if got := c.HyperNodeOfNode("n2"); got != "leaf2" {
		t.Fatalf("leaf=%s", got)
	}
	if got := c.DomainOfNodeAtTier("n1", 2); got != "rack1" {
		t.Fatalf("domain=%s", got)
	}
	if got := c.DomainOfNodeAtTier("n1", 9); got != "" {
		t.Fatalf("unexpected domain=%s", got)
	}
	anc := c.HyperNodeAncestors("leaf1")
	if len(anc) != 3 {
		t.Fatalf("ancestors=%v", anc)
	}
}

func TestResetForRestart(t *testing.T) {
	c := testCache(t)
	if err := c.SubmitJob(jobFixture("j", 1, api.Cores(1))); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-worker-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	if err := c.ResetForRestart("default/j-worker-0", 1); err != nil {
		t.Fatal(err)
	}
	p := c.Pods["default/j-worker-0"]
	if p.Phase != api.PodPending || p.Restarts != 1 || p.NodeName != "" {
		t.Fatalf("pod after reset: %+v", p)
	}
	if c.Nodes["n1"].Node.Allocated[api.CPU] != 0 {
		t.Fatal("reset did not release resources")
	}
}

func TestReservationOnlyHoldsUnusedCapacity(t *testing.T) {
	c := New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(0, 0, 8)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "a"}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "b"}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddReservation(&api.Reservation{Name: "r", Queue: "a", NodeNames: []string{"n1"},
		Resources: api.Resource{api.GPU: 4}, StartTick: 0, EndTick: 10}); err != nil {
		t.Fatal(err)
	}
	if err := c.SubmitJob(&api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "a", MinAvailable: 3, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 3, Requests: api.Resource{api.GPU: 1}}},
	}}); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		if err := c.Bind("default/j-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	// node idle = 5; reserved-but-unused = 1; guest sees 4.
	if got := c.IdleFor("n1", "b", "", 1)[api.GPU]; got != 4 {
		t.Fatalf("guest idle=%d, want 4", got)
	}
	if got := c.IdleFor("n1", "a", "", 1)[api.GPU]; got != 5 {
		t.Fatalf("owner idle=%d, want 5", got)
	}
}

func TestCloneKeepsSinglePodIdentity(t *testing.T) {
	c := testCache(t)
	if err := c.SubmitJob(jobFixture("j", 1, api.Cores(1))); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-worker-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	clone := c.Clone()
	uid := "default/j-worker-0"
	clone.Pods[uid].Phase = api.PodRunning
	if clone.Nodes["n1"].Pods[uid].Phase != api.PodRunning {
		t.Fatal("node.Pods and Pods point at different objects after Clone")
	}
}

func TestNUMAIgnoresNodeLevelDeviceDimensions(t *testing.T) {
	c := New()
	n := &api.Node{Name: "n1", Allocatable: api.R(4000, 8192, 1), NUMANodes: []api.NUMANode{
		{ID: 0, Allocatable: api.R(2000, 4096, 0)},
		{ID: 1, Allocatable: api.R(2000, 4096, 0)},
	}}
	if err := c.AddNode(n); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	if err := c.SubmitJob(&api.Job{Name: "gpu", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 1, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Resource{api.GPU: 1}}},
	}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/gpu-w-0", "n1", 0); err != nil {
		t.Fatalf("node-level GPU should not be blocked by CPU/memory-only NUMA nodes: %v", err)
	}
}

func TestValidateQueueParentCycle(t *testing.T) {
	c := New()
	a := &api.Queue{Name: "a"}
	a.Spec.Parent = "b"
	b := &api.Queue{Name: "b"}
	b.Spec.Parent = "a"
	if err := c.AddQueue(a); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(b); err != nil {
		t.Fatal(err)
	}
	if err := c.ValidateTopology(); err == nil {
		t.Fatal("queue parent cycle should be rejected")
	}
}

func TestHyperNodeRejectsNonMonotoneTier(t *testing.T) {
	c := New()
	if err := c.AddHyperNode(&api.HyperNode{Name: "p", Tier: 1}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddHyperNode(&api.HyperNode{Name: "c", Tier: 2, Parent: "p"}); err == nil {
		t.Fatal("child tier 2 under parent tier 1 should be rejected")
	}
}

func TestHyperNodeRejectsSelfParent(t *testing.T) {
	c := New()
	if err := c.AddHyperNode(&api.HyperNode{Name: "h", Tier: 1, Parent: "h"}); err == nil {
		t.Fatal("self parent should be rejected")
	}
}

func TestGovernanceObjectsCloneDeeply(t *testing.T) {
	c := testCache(t)
	q := &api.Queue{Name: "team", Spec: api.QueueSpec{
		NodeGroup: &api.NodeGroupSpec{
			Names:      []string{"a"},
			Capability: map[string]api.Resource{"a": api.Resource{api.CPU: 2000}},
		},
	}}
	if err := c.AddQueue(q); err != nil {
		t.Fatal(err)
	}
	if err := c.AddPDB(&api.PodDisruptionBudget{
		Name: "p", Namespace: "default",
		Selector: map[string]string{"app": "x"}, HasMin: true, MinAvailable: 2,
	}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddResourceQuota(&api.ResourceQuota{
		Name: "q", Namespace: "default", Hard: api.Resource{api.CPU: 4000},
	}); err != nil {
		t.Fatal(err)
	}
	clone := c.Clone()
	clone.Queues["team"].Spec.NodeGroup.Names[0] = "b"
	clone.Queues["team"].Spec.NodeGroup.Capability["a"][api.CPU] = 1
	clone.PDBs["default/p"].Selector["app"] = "y"
	clone.PDBs["default/p"].MinAvailable = 1
	clone.ResourceQuotas["default/q"].Hard[api.CPU] = 1
	if c.Queues["team"].Spec.NodeGroup.Names[0] != "a" ||
		c.Queues["team"].Spec.NodeGroup.Capability["a"][api.CPU] != 2000 ||
		c.PDBs["default/p"].Selector["app"] != "x" ||
		c.PDBs["default/p"].MinAvailable != 2 ||
		c.ResourceQuotas["default/q"].Hard[api.CPU] != 4000 {
		t.Fatal("clone mutated the original governance objects")
	}
}

func TestNamespaceAdmittedUsageAndLabels(t *testing.T) {
	c := New()
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	j := jobFixture("j", 2, api.Cores(1))
	j.Spec.Labels = map[string]string{"team": "ml"}
	j.Spec.Tasks[0].Labels = map[string]string{"app": "train"}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	if got := c.NamespaceAdmittedUsage("default")[api.CPU]; got != 0 {
		t.Fatalf("pending job must not consume quota, got %d", got)
	}
	c.Jobs["default/j"].Status.Phase = api.JobInqueue
	if got := c.NamespaceAdmittedUsage("default")[api.CPU]; got != 2000 {
		t.Fatalf("admitted usage: want 2000, got %d", got)
	}
	c.Pods["default/j-worker-0"].Phase = api.PodSucceeded
	if got := c.NamespaceAdmittedUsage("default")[api.CPU]; got != 1000 {
		t.Fatalf("terminal pods must not consume quota: want 1000, got %d", got)
	}
	for _, p := range c.Pods {
		if p.Labels["team"] != "ml" || p.Labels["app"] != "train" {
			t.Fatalf("pod labels not propagated: %v", p.Labels)
		}
	}
}

func TestQueueNodeGroupAllocated(t *testing.T) {
	c := New()
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	for _, n := range []string{"a1", "b1"} {
		group := "a"
		if n == "b1" {
			group = "b"
		}
		if err := c.AddNode(&api.Node{Name: n, Allocatable: api.R(4000, 8192, 4),
			Labels: map[string]string{api.NodeGroupLabel: group}}); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.SubmitJob(jobFixture("j", 2, api.Cores(1))); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-worker-0", "a1", 0); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-worker-1", "b1", 0); err != nil {
		t.Fatal(err)
	}
	if got := c.QueueNodeGroupAllocated("default", "a")[api.CPU]; got != 1000 {
		t.Fatalf("group a usage: want 1000, got %d", got)
	}
	if got := c.QueueNodeGroupAllocated("default", "b")[api.CPU]; got != 1000 {
		t.Fatalf("group b usage: want 1000, got %d", got)
	}
}
