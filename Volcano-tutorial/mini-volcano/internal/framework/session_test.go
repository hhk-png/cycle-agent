package framework_test

import (
	"testing"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

type fakePlugin struct {
	name string
	open func(*framework.Session)
}

func (f *fakePlugin) Name() string                        { return f.name }
func (f *fakePlugin) OnSessionOpen(s *framework.Session)  { f.open(s) }
func (f *fakePlugin) OnSessionClose(s *framework.Session) {}

func baseCache(t *testing.T) *cache.Cache {
	t.Helper()
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(4000, 8192, 0)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	j := &api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 1, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Cores(1)}},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	return c
}

func cfg() framework.Config {
	return framework.Config{Actions: []string{"enqueue"}}
}

func TestTierOrder(t *testing.T) {
	asc := &fakePlugin{name: "asc", open: func(s *framework.Session) {
		s.AddQueueOrderFn("asc", func(l, r *api.Queue) int {
			if l.Name < r.Name {
				return -1
			}
			if l.Name > r.Name {
				return 1
			}
			return 0
		})
	}}
	desc := &fakePlugin{name: "desc", open: func(s *framework.Session) {
		s.AddQueueOrderFn("desc", func(l, r *api.Queue) int {
			if l.Name < r.Name {
				return 1
			}
			if l.Name > r.Name {
				return -1
			}
			return 0
		})
	}}
	ssn := framework.NewSession(baseCache(t), 0, cfg(), []framework.PluginInstance{
		{Plugin: asc, Tier: 0}, {Plugin: desc, Tier: 1},
	})
	a := &api.Queue{Name: "a"}
	b := &api.Queue{Name: "b"}
	if got := ssn.QueueOrder(a, b); got != -1 {
		t.Fatalf("tier 0 should win, got %d", got)
	}
}

func TestJobAndTaskFallbackOrder(t *testing.T) {
	ssn := framework.NewSession(baseCache(t), 0, cfg(), nil)
	a := &api.Job{Name: "b", Namespace: "default", CreationTick: 2}
	b := &api.Job{Name: "a", Namespace: "default", CreationTick: 1}
	if ssn.JobOrder(a, b) <= 0 {
		t.Fatal("earlier creation tick should sort first")
	}
	p1 := &api.Pod{Name: "p1", TaskIndex: 1}
	p2 := &api.Pod{Name: "p2", TaskIndex: 0}
	if ssn.TaskOrder(a, p1, p2) <= 0 {
		t.Fatal("lower task index should sort first")
	}
}

func TestNodeScoreSumAndPredicate(t *testing.T) {
	p1 := &fakePlugin{name: "s1", open: func(s *framework.Session) {
		s.AddNodeOrderFn("s1", func(p *api.Pod, n *cache.NodeState) (float64, error) { return 1, nil })
	}}
	p2 := &fakePlugin{name: "s2", open: func(s *framework.Session) {
		s.AddNodeOrderFn("s2", func(p *api.Pod, n *cache.NodeState) (float64, error) { return 2, nil })
	}}
	c := baseCache(t)
	ssn := framework.NewSession(c, 0, cfg(), []framework.PluginInstance{
		{Plugin: p1, Tier: 0}, {Plugin: p2, Tier: 1},
	})
	pod := c.Pods["default/j-w-0"]
	score, err := ssn.NodeScore(pod, c.Nodes["n1"])
	if err != nil || score != 3 {
		t.Fatalf("score=%v err=%v", score, err)
	}
}

func TestPredicateFit(t *testing.T) {
	c := baseCache(t)
	ssn := framework.NewSession(c, 0, cfg(), nil)
	pod := c.Pods["default/j-w-0"].Clone()
	pod.Requests = api.Cores(8)
	if err := ssn.Predicate(pod, c.Nodes["n1"]); err == nil {
		t.Fatal("over-large pod should fail fit predicate")
	}
}

func TestStatementCommit(t *testing.T) {
	c := baseCache(t)
	ssn := framework.NewSession(c, 0, cfg(), nil)
	st := ssn.NewStatement()
	if err := st.Bind("default/j-w-0", "n1"); err != nil {
		t.Fatal(err)
	}
	if c.Pods["default/j-w-0"].Phase != api.PodPending {
		t.Fatal("statement mutated the base cache")
	}
	st.Commit()
	if ssn.Cache.Pods["default/j-w-0"].Phase != api.PodAllocated {
		t.Fatal("commit did not publish the trial cache")
	}
	if c.Pods["default/j-w-0"].Phase != api.PodPending {
		t.Fatal("session commit should not mutate the caller's base cache directly")
	}
	if len(ssn.Events) == 0 {
		t.Fatal("bind should emit an event")
	}
}

func TestStatementDiscard(t *testing.T) {
	c := baseCache(t)
	ssn := framework.NewSession(c, 0, cfg(), nil)
	st := ssn.NewStatement()
	if err := st.Bind("default/j-w-0", "n1"); err != nil {
		t.Fatal(err)
	}
	st.Discard()
	if ssn.Cache.Pods["default/j-w-0"].Phase != api.PodPending {
		t.Fatal("discard leaked a binding")
	}
	if len(ssn.Events) != 0 {
		t.Fatal("discard leaked an event")
	}
}

func TestStatementEvict(t *testing.T) {
	c := baseCache(t)
	if err := c.Bind("default/j-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	ssn := framework.NewSession(c, 0, cfg(), nil)
	st := ssn.NewStatement()
	if err := st.Evict("default/j-w-0", "test"); err != nil {
		t.Fatal(err)
	}
	if ssn.Cache.Pods["default/j-w-0"].Phase != api.PodAllocated {
		t.Fatal("eviction should be tentative")
	}
	st.Commit()
	if ssn.Cache.Pods["default/j-w-0"].Phase != api.PodPending {
		t.Fatal("committed eviction not visible")
	}
	if ssn.Cache.Nodes["n1"].Node.Allocated[api.CPU] != 0 {
		t.Fatal("eviction did not release resources")
	}
}

func TestDefaultJobValidAndReady(t *testing.T) {
	c := baseCache(t)
	ssn := framework.NewSession(c, 0, cfg(), nil)
	j := c.Jobs["default/j"]
	if ssn.JobReady(j) {
		t.Fatal("no ready pods, default job ready should be false")
	}
	if err := ssn.JobValid(j); err != nil {
		t.Fatal(err)
	}
	j.Spec.MinAvailable = 9
	if err := ssn.JobValid(j); err == nil {
		t.Fatal("invalid minAvailable should fail")
	}
}

func TestPreemptableDefault(t *testing.T) {
	ssn := framework.NewSession(baseCache(t), 0, cfg(), nil)
	hi := &api.Pod{Priority: 10, Job: "a"}
	lo := &api.Pod{Priority: 1, Job: "b"}
	if !ssn.Preemptable(hi, lo) {
		t.Fatal("high priority should preempt lower")
	}
	if ssn.Preemptable(lo, hi) {
		t.Fatal("low priority must not preempt higher")
	}
	if ssn.Preemptable(hi, &api.Pod{Priority: 0, Job: "a"}) {
		t.Fatal("same job must not preempt itself")
	}
}

func TestConfigValidate(t *testing.T) {
	good := framework.DefaultConfig()
	if err := good.Validate(); err != nil {
		t.Fatal(err)
	}
	bad := framework.Config{Actions: []string{"enqueue", "enqueue"}}
	if err := bad.Validate(); err == nil {
		t.Fatal("duplicate action should fail")
	}
	bad = framework.Config{Actions: []string{"enqueue"}, Tiers: []framework.TierConfig{{
		Name: "t", Plugins: []framework.PluginConfig{{Name: "gang"}, {Name: "gang"}},
	}}}
	if err := bad.Validate(); err == nil {
		t.Fatal("duplicate plugin should fail")
	}
}
