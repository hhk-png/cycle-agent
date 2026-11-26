package scenario

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/sim"
)

func governanceScenarios() []*Scenario {
	return []*Scenario{
		pdbScenario(),
		resourceQuotaScenario(),
		nodeGroupScenario(),
	}
}

// pdbScenario proves two properties at once: a PDB with spare capacity allows a
// single disruption, while a PDB with zero spare capacity blocks preemption
// entirely. high-2 needs two victims, but only one is ever available, so its
// gang allocation must roll back without evicting anyone.
func pdbScenario() *Scenario {
	return &Scenario{
		Name:        "pdb",
		Description: "PodDisruptionBudgets allow one victim from crit-a, block crit-b, and make a 2-pod preemption roll back.",
		Ticks:       5,
		Explain:     []string{"default/high-2"},
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 0, framework.PluginConfig{Name: "pdb"})
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 8, nil)); err != nil {
				return err
			}
			critA := mkJob("crit-a", "default", 0, 3, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)
			critA.Spec.Tasks[0].Labels = map[string]string{"app": "crit-a"}
			critB := mkJob("crit-b", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)
			critB.Spec.Tasks[0].Labels = map[string]string{"app": "crit-b"}
			if err := w.SubmitJob(critA); err != nil {
				return err
			}
			if err := w.SubmitJob(critB); err != nil {
				return err
			}
			if err := w.AddPDB(&api.PodDisruptionBudget{
				Name: "crit-a", Namespace: "default",
				Selector: map[string]string{"app": "crit-a"},
				HasMin:   true, MinAvailable: 3,
			}); err != nil {
				return err
			}
			return w.AddPDB(&api.PodDisruptionBudget{
				Name: "crit-b", Namespace: "default",
				Selector: map[string]string{"app": "crit-b"},
				HasMin:   true, MinAvailable: 4,
			})
		},
		Steps: []Step{
			{Tick: 2, Apply: func(w *sim.World) error {
				return w.SubmitJob(mkJob("high-1", "default", 100, 1, 1,
					api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
			}},
			{Tick: 3, Apply: func(w *sim.World) error {
				return w.SubmitJob(mkJob("high-2", "default", 100, 2, 2,
					api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
			}},
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "high-1", 1); err != nil {
				return err
			}
			if err := expectNotRunning(w, "high-2"); err != nil {
				return err
			}
			if err := expectRunning(w, "crit-a", 3); err != nil {
				return err
			}
			if err := expectRunning(w, "crit-b", 4); err != nil {
				return err
			}
			if got := w.Cache.QueueAllocated("default")[api.GPU]; got != 8 {
				return fmt.Errorf("cluster GPU accounting should stay at 8, got %d", got)
			}
			return expectReason(w, "high-2", "no preemptable victims")
		},
	}
}

// resourceQuotaScenario exercises the namespace admission gate. team-q has a
// 4-CPU quota. Job a (2 CPU) is admitted, job b (3 CPU) is rejected because
// 2+3 > 4, and job c (1 CPU) still fits, proving the check is per-job rather
// than a queue-wide latch.
func resourceQuotaScenario() *Scenario {
	return &Scenario{
		Name:        "resourcequota",
		Description: "A namespace ResourceQuota admits 2 CPU + 1 CPU jobs but rejects a 3 CPU job that would exceed 4 CPU.",
		Ticks:       3,
		Explain:     []string{"team-q/b"},
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 0, framework.PluginConfig{Name: "resourcequota"})
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("cpu-node", 8, 32, 0, nil)); err != nil {
				return err
			}
			if err := w.AddQuota(&api.ResourceQuota{
				Name: "team-q", Namespace: "team-q",
				Hard: api.Resource{"requests.cpu": 4000, "requests.memory": 32768},
			}); err != nil {
				return err
			}
			mk := func(name string, replicas int32) *api.Job {
				j := mkJob(name, "default", 0, replicas, replicas, api.Cores(1), 100)
				j.Namespace = "team-q"
				return j
			}
			if err := w.SubmitJob(mk("a", 2)); err != nil {
				return err
			}
			if err := w.SubmitJob(mk("b", 3)); err != nil {
				return err
			}
			return w.SubmitJob(mk("c", 1))
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "a", 2); err != nil {
				return err
			}
			if err := expectRunning(w, "c", 1); err != nil {
				return err
			}
			if err := expectNotRunning(w, "b"); err != nil {
				return err
			}
			if got := w.Cache.NamespaceAdmittedUsage("team-q")[api.CPU]; got != 3000 {
				return fmt.Errorf("admitted usage should be 3 CPU, got %d", got)
			}
			return expectReason(w, "b", "quota")
		},
	}
}

// nodeGroupScenario restricts team-a to node group "a" and team-b to "b".
// Node group "b" has spare GPUs the whole time; team-a must still queue on "a".
func nodeGroupScenario() *Scenario {
	return &Scenario{
		Name:        "nodegroup",
		Description: "Queues are confined to labeled node groups even when another group has idle GPUs.",
		Ticks:       3,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 1, framework.PluginConfig{Name: "nodegroup"})
		},
		Build: func(w *sim.World) error {
			qa := mkQueue("team-a", 1, nil, nil, true)
			qa.Spec.NodeGroup = &api.NodeGroupSpec{Names: []string{"a"}}
			qb := mkQueue("team-b", 1, nil, nil, true)
			qb.Spec.NodeGroup = &api.NodeGroupSpec{Names: []string{"b"}}
			if err := w.AddQueue(qa); err != nil {
				return err
			}
			if err := w.AddQueue(qb); err != nil {
				return err
			}
			for _, n := range []string{"a1", "a2"} {
				if err := w.AddNode(mkNode(n, 16, 64, 4, map[string]string{api.NodeGroupLabel: "a"})); err != nil {
					return err
				}
			}
			if err := w.AddNode(mkNode("b1", 16, 64, 4, map[string]string{api.NodeGroupLabel: "b"})); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("a-job", "team-a", 0, 6, 6,
				api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("b-job", "team-b", 0, 4, 4,
				api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "a-job", 6); err != nil {
				return err
			}
			if err := expectRunning(w, "b-job", 4); err != nil {
				return err
			}
			if err := expectGroup(w, "a-job", "a"); err != nil {
				return err
			}
			if err := expectGroup(w, "b-job", "b"); err != nil {
				return err
			}
			if got := w.Cache.QueueNodeGroupAllocated("team-a", "a")[api.GPU]; got != 6 {
				return fmt.Errorf("team-a group a allocation: want 6, got %d", got)
			}
			return nil
		},
	}
}

func expectGroup(w *sim.World, jobName, group string) error {
	j, err := job(w, jobName)
	if err != nil {
		return err
	}
	for _, p := range w.Cache.JobPods(j.UID) {
		node := w.Cache.Nodes[p.NodeName]
		if node == nil {
			return fmt.Errorf("job %s pod %s is not on a node", jobName, p.Name)
		}
		if got := node.Node.Labels[api.NodeGroupLabel]; got != group {
			return fmt.Errorf("job %s pod %s is in node group %q, want %q", jobName, p.Name, got, group)
		}
	}
	return nil
}
