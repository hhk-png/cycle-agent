package actions

import (
	"testing"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/plugins"
)

// TestDisruptableVictimsAppliesPDBCumulatively guards the bug where each
// victim is checked against the committed cache: with 4 available and
// minAvailable=3, every individual eviction looks safe, but the second one is
// not. The statement replay in disruptableVictims must stop after the first.
func TestDisruptableVictimsAppliesPDBCumulatively(t *testing.T) {
	c := cache.New()
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(8000, 32768, 0)}); err != nil {
		t.Fatal(err)
	}
	j := &api.Job{Name: "p", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 1, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 4, Requests: api.Cores(1),
			Labels: map[string]string{"app": "p"}}},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 4; i++ {
		if err := c.Bind("default/p-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.AddPDB(&api.PodDisruptionBudget{
		Name: "p", Namespace: "default", Selector: map[string]string{"app": "p"},
		HasMin: true, MinAvailable: 3,
	}); err != nil {
		t.Fatal(err)
	}
	p, err := plugins.Build("pdb", nil)
	if err != nil {
		t.Fatal(err)
	}
	ssn := framework.NewSession(c, 0, framework.Config{Actions: []string{"shuffle"}},
		[]framework.PluginInstance{{Plugin: p, Tier: 0}})

	victims := []*api.Pod{
		c.Pods["default/p-w-0"],
		c.Pods["default/p-w-1"],
		c.Pods["default/p-w-2"],
	}
	got := disruptableVictims(ssn, victims)
	if len(got) != 1 {
		t.Fatalf("PDB minAvailable=3 must allow exactly one of three evictions, got %d", len(got))
	}
	if err := ssn.NewStatement().Evict(got[0].UID, "test"); err != nil {
		t.Fatalf("selected victim should be evictable: %v", err)
	}
}
