package scenario

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/sim"
)

func addPlugin(cfg framework.Config, tier int, plugins ...framework.PluginConfig) framework.Config {
	for len(cfg.Tiers) <= tier {
		cfg.Tiers = append(cfg.Tiers, framework.TierConfig{Name: fmt.Sprintf("tier-%d", len(cfg.Tiers)+1)})
	}
	cfg.Tiers[tier].Plugins = append(cfg.Tiers[tier].Plugins, plugins...)
	return cfg
}

func advancedScenarios() []*Scenario {
	return []*Scenario{
		topologyScenario(),
		groupTopologyScenario(),
		taskTopologyScenario(),
		reservationScenario(),
		slaScenario(),
		fairshareScenario(),
		hierarchyScenario(),
		numaScenario(),
		tdmScenario(),
		retryScenario(),
		cronScenario(),
		jobflowScenario(),
		backfillScenario(),
		heterogeneousScenario(),
		throughputScenario(),
		shuffleScenario(),
	}
}

// shuffleScenario starts from a deliberately fragmented placement (two pods on
// each node), lets the rescheduling plugin nominate the least-utilized node,
// and verifies that shuffle + binpack collapse the workload onto one node.
// The jobs use minAvailable=1 with two replicas so evicting one replica does
// not violate gang semantics.
func shuffleScenario() *Scenario {
	return &Scenario{
		Name:        "shuffle",
		Description: "Rescheduling nominates a half-full node; shuffle evicts its pods and binpack repacks them.",
		Ticks:       4,
		Config: func() framework.Config {
			return framework.Config{
				Actions: []string{"enqueue", "allocate", "shuffle", "backfill"},
				Tiers: []framework.TierConfig{
					{Name: "tier-1", Plugins: []framework.PluginConfig{
						{Name: "priority"},
						{Name: "gang"},
					}},
					{Name: "tier-2", Plugins: []framework.PluginConfig{
						{Name: "predicates"},
						{Name: "binpack", Args: map[string]string{"weight": "10"}},
						{Name: "rescheduling", Args: map[string]string{"strategy": "fragmentation", "threshold": "0.6"}},
					}},
				},
			}
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			for _, name := range []string{"node-a", "node-b"} {
				if err := w.AddNode(mkNode(name, 8, 32, 0, nil)); err != nil {
					return err
				}
			}
			for _, jobName := range []string{"left", "right"} {
				j := mkJob(jobName, "default", 0, 1, 2, api.Cores(2), 100)
				if err := w.SubmitJob(j); err != nil {
					return err
				}
			}
			// Fragment the initial state on purpose. Without this, allocate would
			// already binpack and shuffle would have nothing to fix.
			placements := map[string]string{
				"default/left-worker-0":  "node-a",
				"default/left-worker-1":  "node-b",
				"default/right-worker-0": "node-a",
				"default/right-worker-1": "node-b",
			}
			for uid, node := range placements {
				if err := w.Cache.Bind(uid, node, 0); err != nil {
					return err
				}
			}
			return nil
		},
		Check: func(w *sim.World) error {
			for _, name := range []string{"left", "right"} {
				if err := expectRunning(w, name, 2); err != nil {
					return err
				}
			}
			nodes := map[string]bool{}
			for _, name := range []string{"left", "right"} {
				j, err := job(w, name)
				if err != nil {
					return err
				}
				for _, p := range w.Cache.JobPods(j.UID) {
					if p.Phase != api.PodRunning {
						return fmt.Errorf("job %s pod %s: want Running, got %s", name, p.Name, p.Phase)
					}
					nodes[p.NodeName] = true
				}
			}
			if len(nodes) != 1 {
				return fmt.Errorf("shuffle should consolidate all pods on one node, got %v", nodes)
			}
			for node := range nodes {
				if got := w.Cache.Nodes[node].Node.Allocated[api.CPU]; got != 8000 {
					return fmt.Errorf("node %s should be fully packed (cpu=8000), got %d", node, got)
				}
			}
			sawShuffle := false
			for _, e := range w.Events {
				if e.Type == "Shuffled" {
					sawShuffle = true
					break
				}
			}
			if !sawShuffle {
				return fmt.Errorf("expected a Shuffled event")
			}
			return nil
		},
	}
}

func topologyScenario() *Scenario {
	return &Scenario{
		Name:        "topology",
		Description: "Hard HyperNode constraint places all four GPUs inside rack-1.",
		Ticks:       3,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 1,
				framework.PluginConfig{Name: "network-topology-aware"})
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			for _, name := range []string{"leaf-a1", "leaf-a2", "leaf-b1", "leaf-b2"} {
				if err := w.AddNode(mkNode(name, 8, 32, 2, nil)); err != nil {
					return err
				}
			}
			for _, h := range []*api.HyperNode{
				{Name: "leaf-a1", Tier: 1, Parent: "rack-1", Members: []string{"leaf-a1"}},
				{Name: "leaf-a2", Tier: 1, Parent: "rack-1", Members: []string{"leaf-a2"}},
				{Name: "leaf-b1", Tier: 1, Parent: "rack-2", Members: []string{"leaf-b1"}},
				{Name: "leaf-b2", Tier: 1, Parent: "rack-2", Members: []string{"leaf-b2"}},
				{Name: "rack-1", Tier: 2, Parent: "root", Children: []string{"leaf-a1", "leaf-a2"}},
				{Name: "rack-2", Tier: 2, Parent: "root", Children: []string{"leaf-b1", "leaf-b2"}},
				{Name: "root", Tier: 3, Children: []string{"rack-1", "rack-2"}},
			} {
				if err := w.AddHyperNode(h); err != nil {
					return err
				}
			}
			j := mkJob("train", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2)
			j.Spec.NetworkTopology = &api.NetworkTopologySpec{Mode: "hard", HighestTierAllowed: 2}
			return w.SubmitJob(j)
		},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "train", api.JobCompleted); err != nil {
				return err
			}
			return expectAllOnNodes(w, "train", "leaf-a")
		},
	}
}

// groupTopologyScenario models the v1.16 PodGroup anti-affinity feature used
// by multi-replica inference: two serving groups each fit in one rack, but the
// required term forces the second group into a different rack even though the
// first rack still has free capacity.
func groupTopologyScenario() *Scenario {
	return &Scenario{
		Name:        "group-topology",
		Description: "Required PodGroup anti-affinity places two serving groups in different racks.",
		Ticks:       3,
		Config: func() framework.Config {
			return framework.Config{
				Actions: []string{"enqueue", "allocate"},
				Tiers: []framework.TierConfig{
					{Name: "tier-1", Plugins: []framework.PluginConfig{
						{Name: "priority"},
						{Name: "gang"},
					}},
					{Name: "tier-2", Plugins: []framework.PluginConfig{
						{Name: "predicates"},
						{Name: "group-topology-affinity"},
						{Name: "nodeorder"},
					}},
				},
			}
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			for _, name := range []string{"node-a1", "node-a2", "node-b1"} {
				if err := w.AddNode(mkNode(name, 8, 32, 0, nil)); err != nil {
					return err
				}
			}
			for _, h := range []*api.HyperNode{
				{Name: "leaf-a1", Tier: 1, Parent: "rack-1", Members: []string{"node-a1"}},
				{Name: "leaf-a2", Tier: 1, Parent: "rack-1", Members: []string{"node-a2"}},
				{Name: "leaf-b1", Tier: 1, Parent: "rack-2", Members: []string{"node-b1"}},
				{Name: "rack-1", Tier: 2, Parent: "root", Children: []string{"leaf-a1", "leaf-a2"}},
				{Name: "rack-2", Tier: 2, Parent: "root", Children: []string{"leaf-b1"}},
				{Name: "root", Tier: 3, Children: []string{"rack-1", "rack-2"}},
			} {
				if err := w.AddHyperNode(h); err != nil {
					return err
				}
			}
			for _, name := range []string{"serve-a", "serve-b"} {
				j := mkJob(name, "default", 0, 1, 1, api.Cores(1), 100)
				j.Spec.Labels = map[string]string{"serving-group": "online"}
				if err := w.SubmitJob(j); err != nil {
					return err
				}
				// Only serve-a declares the rule. serve-b merely carries the
				// matching label, so the scenario also proves the symmetric
				// evaluation (one-sided rules are not bypassed).
				if name == "serve-a" {
					pg := w.Cache.PodGroups[j.UID]
					if pg == nil {
						return fmt.Errorf("podgroup for %s not found", name)
					}
					pg.TopologyAffinity = &api.TopologyAffinitySpec{
						PodGroupAntiAffinity: []api.PodGroupAntiAffinityTerm{{
							MatchLabels:  map[string]string{"serving-group": "online"},
							TopologyTier: 2,
						}},
					}
				}
			}
			return nil
		},
		Check: func(w *sim.World) error {
			want := map[string]string{"serve-a": "rack-1", "serve-b": "rack-2"}
			domains := map[string]string{}
			for _, name := range []string{"serve-a", "serve-b"} {
				if err := expectRunning(w, name, 1); err != nil {
					return err
				}
				j, err := job(w, name)
				if err != nil {
					return err
				}
				for _, p := range w.Cache.JobPods(j.UID) {
					domain := w.Cache.DomainOfNodeAtTier(p.NodeName, 2)
					if domain == "" {
						return fmt.Errorf("pod %s is not in a tier-2 domain", p.Name)
					}
					domains[name] = domain
				}
			}
			for name, domain := range want {
				if domains[name] != domain {
					return fmt.Errorf("%s should be in %s, got %s", name, domain, domains[name])
				}
			}
			// rack-1 still has a free node: serve-b did not go there merely
			// because rack-1 was full.
			free := 0
			for _, node := range []string{"node-a1", "node-a2"} {
				if w.Cache.Nodes[node].Node.Allocated[api.CPU] == 0 {
					free++
				}
			}
			if free != 1 {
				return fmt.Errorf("rack-1 should have exactly one free node, free=%d", free)
			}
			return nil
		},
	}
}

func taskTopologyScenario() *Scenario {
	return &Scenario{
		Name:        "task-topology",
		Description: "ps and worker roles with affinity share one node.",
		Ticks:       2,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 1,
				framework.PluginConfig{Name: "task-topology"})
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			for _, name := range []string{"node-a", "node-b", "node-c"} {
				if err := w.AddNode(mkNode(name, 4, 16, 0, nil)); err != nil {
					return err
				}
			}
			j := mkMultiTask("dist", "default", 0, 3,
				api.TaskSpec{Name: "ps", Replicas: 1, Requests: api.Cores(1), Duration: 100},
				api.TaskSpec{Name: "worker", Replicas: 2, Requests: api.Cores(1), Duration: 100},
			)
			j.Spec.TaskTopology = []api.TaskTopologySpec{{TaskNames: []string{"ps", "worker"}, Policy: "affinity"}}
			return w.SubmitJob(j)
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "dist", 3); err != nil {
				return err
			}
			return expectSameNode(w, "dist")
		},
	}
}

func reservationScenario() *Scenario {
	return &Scenario{
		Name:        "reservation",
		Description: "A 4-GPU reservation blocks an 8-GPU job until its window closes.",
		Ticks:       10,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 0,
				framework.PluginConfig{Name: "reservation"})
		},
		Build: func(w *sim.World) error {
			if err := w.AddQueue(mkQueue("reserved", 1, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddQueue(mkQueue("other", 1, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 8, nil)); err != nil {
				return err
			}
			if err := w.AddReservation(&api.Reservation{
				Name: "r1", Queue: "reserved", NodeNames: []string{"gpu-node"},
				Resources: api.Resource{api.GPU: 4}, StartTick: 0, EndTick: 6,
			}); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("r-job", "reserved", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("x-job", "other", 0, 8, 8, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2))
		},
		Steps: []Step{{
			Tick: 5,
			Apply: func(w *sim.World) error {
				if err := expectNotRunning(w, "x-job"); err != nil {
					return fmt.Errorf("reservation window still active: %w", err)
				}
				return nil
			},
		}},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "r-job", api.JobCompleted); err != nil {
				return err
			}
			return expectPhase(w, "x-job", api.JobCompleted)
		},
	}
}

func slaScenario() *Scenario {
	return &Scenario{
		Name:        "sla",
		Description: "An overdue job (waiting budget 1 tick) is scheduled before an older queued job.",
		Ticks:       5,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 0,
				framework.PluginConfig{Name: "sla"})
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 4, nil)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("j1", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("j2", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)); err != nil {
				return err
			}
			j3 := annotate(mkJob("j3", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2),
				"volcano.sh/sla-waiting-time", "1")
			return w.SubmitJob(j3)
		},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "j1", api.JobCompleted); err != nil {
				return err
			}
			if err := expectRunning(w, "j3", 4); err != nil {
				return err
			}
			return expectNotRunning(w, "j2")
		},
	}
}

func fairshareScenario() *Scenario {
	return &Scenario{
		Name:        "fairshare",
		Description: "Historical usage pushes the next free GPU slot to the queue that has consumed less.",
		Ticks:       5,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 0,
				framework.PluginConfig{Name: "fairshare", Args: map[string]string{"halfLife": "4"}})
		},
		Build: func(w *sim.World) error {
			if err := w.AddQueue(mkQueue("team-a", 1, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddQueue(mkQueue("team-b", 1, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 4, nil)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("a-first", "team-a", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("a-second", "team-a", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("b-job", "team-b", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
		},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "a-first", api.JobCompleted); err != nil {
				return err
			}
			if err := expectRunning(w, "b-job", 4); err != nil {
				return err
			}
			return expectNotRunning(w, "a-second")
		},
	}
}

func hierarchyScenario() *Scenario {
	return &Scenario{
		Name:        "hierarchy",
		Description: "Parent org is capped at 6 GPUs; two children receive 3 GPUs each.",
		Ticks:       2,
		Config: func() framework.Config {
			cfg := framework.DefaultConfig()
			cfg.Tiers[1].Plugins = []framework.PluginConfig{
				{Name: "drf"},
				{Name: "predicates"},
				{Name: "capacity", Args: map[string]string{"enableHierarchy": "true"}},
				{Name: "nodeorder"},
			}
			return cfg
		},
		Build: func(w *sim.World) error {
			org := mkQueue("org", 1, api.Resource{api.GPU: 6}, nil, true)
			if err := w.AddQueue(org); err != nil {
				return err
			}
			a := mkQueue("org-a", 1, nil, nil, true)
			a.Spec.Parent = "org"
			b := mkQueue("org-b", 1, nil, nil, true)
			b.Spec.Parent = "org"
			if err := w.AddQueue(a); err != nil {
				return err
			}
			if err := w.AddQueue(b); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 8, nil)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("a-job", "org-a", 0, 3, 3, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("b-job", "org-b", 0, 3, 3, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
		},
		Check: func(w *sim.World) error {
			if err := expectQueueAllocated(w, "org-a", api.GPU, 3); err != nil {
				return err
			}
			if err := expectQueueAllocated(w, "org-b", api.GPU, 3); err != nil {
				return err
			}
			if got := w.Cache.Queues["org"].Status.Deserved[api.GPU]; got != 6 {
				return fmt.Errorf("org deserved GPU: want 6, got %d", got)
			}
			return nil
		},
	}
}

func numaScenario() *Scenario {
	return &Scenario{
		Name:        "numa",
		Description: "A 12-CPU pod cannot fit any single 8-CPU NUMA node and stays Pending.",
		Ticks:       2,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 1,
				framework.PluginConfig{Name: "numa-aware"})
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			n := mkNode("numa-node", 16, 32, 0, nil)
			n.NUMANodes = []api.NUMANode{
				{ID: 0, Allocatable: api.Resource{api.CPU: 8000, api.Memory: 16 * 1024}},
				{ID: 1, Allocatable: api.Resource{api.CPU: 8000, api.Memory: 16 * 1024}},
			}
			if err := w.AddNode(n); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("big", "default", 0, 1, 1, api.Cores(12), 100)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("fit", "default", 0, 1, 1, api.Cores(4), 100))
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "fit", 1); err != nil {
				return err
			}
			if err := expectNotRunning(w, "big"); err != nil {
				return err
			}
			return expectReason(w, "big", "NUMA")
		},
	}
}

func tdmScenario() *Scenario {
	return &Scenario{
		Name:        "tdm",
		Description: "A preemptable job waits for the revocable-zone window while a normal job uses the dedicated node.",
		Ticks:       4,
		Config: func() framework.Config {
			return addPlugin(framework.DefaultConfig(), 1,
				framework.PluginConfig{Name: "tdm", Args: map[string]string{"window.rz1": "2-5"}})
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("normal-node", 16, 64, 4, nil)); err != nil {
				return err
			}
			rz := mkNode("revocable-node", 16, 64, 4, map[string]string{"volcano.sh/revocable-zone": "rz1"})
			if err := w.AddNode(rz); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("normal", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)); err != nil {
				return err
			}
			p := annotate(mkJob("preemptable", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100),
				"volcano.sh/preemptable", "true")
			return w.SubmitJob(p)
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "preemptable", 4); err != nil {
				return err
			}
			if err := expectAllOnNodes(w, "preemptable", "revocable-node"); err != nil {
				return err
			}
			return expectRunning(w, "normal", 4)
		},
	}
}

func retryScenario() *Scenario {
	return &Scenario{
		Name:        "retry",
		Description: "PodFailed -> RestartJob resets the gang, consumes maxRetry and eventually completes.",
		Ticks:       8,
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("cpu-node", 4, 16, 0, nil)); err != nil {
				return err
			}
			j := mkJob("retry", "default", 0, 2, 2, api.Cores(1), 2)
			j.Spec.MaxRetry = 2
			j.Spec.Policies = []api.LifecyclePolicy{{Event: api.EventPodFailed, Action: api.ActionRestartJob}}
			if err := w.SubmitJob(j); err != nil {
				return err
			}
			w.FailPod("default/retry-worker-0", 2)
			return nil
		},
		Check: func(w *sim.World) error {
			j, err := job(w, "retry")
			if err != nil {
				return err
			}
			if j.Status.Retries < 1 {
				return fmt.Errorf("expected at least one restart, got retries=%d", j.Status.Retries)
			}
			return expectPhase(w, "retry", api.JobCompleted)
		},
	}
}

func cronScenario() *Scenario {
	return &Scenario{
		Name:        "cron",
		Description: "CronJob creates a run every two ticks and GC keeps only two completed runs.",
		Ticks:       12,
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("cpu-node", 8, 32, 0, nil)); err != nil {
				return err
			}
			return w.AddCron(&api.CronJob{
				Name: "nightly", Namespace: "default", ScheduleTicks: 2,
				ConcurrencyPolicy: "Allow", HistoryLimit: 2, NextTick: 2,
				Spec: api.JobSpec{Queue: "default", Tasks: []api.TaskSpec{{
					Name: "job", Replicas: 2, Requests: api.Cores(1), Duration: 1,
				}}, MinAvailable: 2, MinAvailableSet: true},
			})
		},
		Check: func(w *sim.World) error {
			created := 0
			for _, e := range w.Events {
				if e.Type == "CronCreated" {
					created++
				}
			}
			if created < 3 {
				return fmt.Errorf("expected >=3 CronCreated events, got %d", created)
			}
			c := w.Manager.CronJobs[0]
			if len(c.Created) > 2 {
				return fmt.Errorf("history limit 2 not enforced: %d runs retained", len(c.Created))
			}
			return nil
		},
	}
}

func jobflowScenario() *Scenario {
	return &Scenario{
		Name:        "jobflow",
		Description: "A three-step JobFlow DAG runs prep -> train -> eval in dependency order.",
		Ticks:       10,
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("cpu-node", 8, 32, 0, nil)); err != nil {
				return err
			}
			step := func(name string, deps []string) api.JobFlowStep {
				return api.JobFlowStep{Name: name, DependsOn: deps, Spec: api.JobSpec{
					Queue: "default", MaxRetry: 3, MinAvailable: 1, MinAvailableSet: true,
					Tasks: []api.TaskSpec{{Name: "task", Replicas: 1, Requests: api.Cores(1), Duration: 1}},
				}}
			}
			return w.AddFlow(&api.JobFlow{
				Name: "pipeline", Namespace: "default",
				Steps: []api.JobFlowStep{
					step("prep", nil),
					step("train", []string{"prep"}),
					step("eval", []string{"train"}),
				},
			})
		},
		Check: func(w *sim.World) error {
			flow := w.Manager.Flows[0]
			if len(flow.Created) != 3 {
				return fmt.Errorf("expected 3 flow steps created, got %d", len(flow.Created))
			}
			for name, uid := range flow.Created {
				j := w.Cache.Jobs[uid]
				if j == nil || j.Status.Phase != api.JobCompleted {
					return fmt.Errorf("flow step %s not completed: %v", name, j)
				}
			}
			return nil
		},
	}
}

func backfillScenario() *Scenario {
	return &Scenario{
		Name:        "backfill",
		Description: "With allocate disabled, BestEffort pods are placed by the backfill action.",
		Ticks:       2,
		Config: func() framework.Config {
			return framework.Config{
				Actions: []string{"enqueue", "backfill"},
				Tiers: []framework.TierConfig{
					{Name: "tier-1", Plugins: []framework.PluginConfig{
						{Name: "priority"}, {Name: "gang"}, {Name: "conformance"},
					}},
					{Name: "tier-2", Plugins: []framework.PluginConfig{
						{Name: "predicates"}, {Name: "proportion"},
					}},
				},
			}
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("cpu-node", 4, 16, 0, nil)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("besteffort", "default", 0, 2, 2, api.Resource{}, 100))
		},
		Check: func(w *sim.World) error {
			return expectRunning(w, "besteffort", 2)
		},
	}
}

func heterogeneousScenario() *Scenario {
	return &Scenario{
		Name:        "heterogeneous",
		Description: "Node selectors keep an A100-only job off the H100 node.",
		Ticks:       3,
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("a100-1", 8, 32, 1, map[string]string{"gpu-type": "a100"})); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("a100-2", 8, 32, 1, map[string]string{"gpu-type": "a100"})); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("h100-1", 8, 32, 1, map[string]string{"gpu-type": "h100"})); err != nil {
				return err
			}
			j := mkJob("train", "default", 0, 2, 2, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2)
			j.Spec.Tasks[0].NodeSelector = map[string]string{"gpu-type": "a100"}
			return w.SubmitJob(j)
		},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "train", api.JobCompleted); err != nil {
				return err
			}
			return expectAllOnNodes(w, "train", "a100-")
		},
	}
}

func throughputScenario() *Scenario {
	return &Scenario{
		Name:        "throughput",
		Description: "Ten two-GPU jobs complete in waves on an eight-GPU node.",
		Ticks:       20,
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 32, 128, 8, nil)); err != nil {
				return err
			}
			for i := 0; i < 10; i++ {
				if err := w.SubmitJob(mkJob(fmt.Sprintf("load-%d", i), "default", 0, 2, 2,
					api.Resource{api.CPU: 1000, api.Memory: 1024, api.GPU: 1}, 1)); err != nil {
					return err
				}
			}
			return nil
		},
		Check: func(w *sim.World) error {
			for i := 0; i < 10; i++ {
				if err := expectPhase(w, fmt.Sprintf("load-%d", i), api.JobCompleted); err != nil {
					return err
				}
			}
			if v, _ := w.Metrics.Value("minivolcano_scheduler_sessions_total", nil); v < 20 {
				return fmt.Errorf("expected >=20 scheduling sessions, got %.0f", v)
			}
			return nil
		},
	}
}
