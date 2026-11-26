package scenario

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/sim"
)

func addDefault(w *sim.World) error {
	return w.AddQueue(mkQueue("default", 1, nil, nil, true))
}

func coreScenarios() []*Scenario {
	return []*Scenario{
		basicScenario(),
		gangScenario(),
		taskGangScenario(),
		minSuccessScenario(),
		priorityScenario(),
		reclaimScenario(),
		proportionScenario(),
		drfScenario(),
		binpackScenario(),
	}
}

func basicScenario() *Scenario {
	return &Scenario{
		Name:        "basic",
		Description: "One gang job on two nodes: enqueue -> allocate -> run -> complete.",
		Ticks:       4,
		Explain:     []string{"default/web"},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("node-a", 4, 8, 0, nil)); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("node-b", 4, 8, 0, nil)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("web", "default", 0, 4, 4, api.Cores(1), 2))
		},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "web", api.JobCompleted); err != nil {
				return err
			}
			if got := w.Cache.TotalIdle()[api.CPU]; got != 8000 {
				return fmt.Errorf("all CPU should be released after completion, idle=%d", got)
			}
			return nil
		},
	}
}

func gangScenario() *Scenario {
	return &Scenario{
		Name:        "gang",
		Description: "A 4-GPU gang waits until the filler job releases all four GPUs; no partial start.",
		Ticks:       8,
		Explain:     []string{"default/train"},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 8, 32, 4, map[string]string{"gpu": "a100"})); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("filler", "default", 0, 2, 2, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 2}, 2)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("train", "default", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 2))
		},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "filler", api.JobCompleted); err != nil {
				return err
			}
			if err := expectPhase(w, "train", api.JobCompleted); err != nil {
				return err
			}
			return nil
		},
	}
}

func ptr32(v int32) *int32 { return &v }

// taskGangScenario is the role-aware counterpart of gangScenario. The job has
// nine replicas but only five are required, and the five must include the
// single master. Eight workers without a master do not satisfy the role gate.
func taskGangScenario() *Scenario {
	return &Scenario{
		Name:        "task-gang",
		Description: "Task-level minAvailable: 1 master + 4 of 8 workers start; the role gate is not just a total count.",
		Ticks:       3,
		Explain:     []string{"default/train"},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			// Five CPU slots: enough for the gate, not for all nine pods.
			if err := w.AddNode(mkNode("cpu-node", 5, 16, 0, nil)); err != nil {
				return err
			}
			// Declare worker first on purpose: the scheduler must still
			// schedule the scarce master role before filling workers.
			job := mkMultiTask("train", "default", 0, 5,
				api.TaskSpec{Name: "worker", Replicas: 8, MinAvailable: ptr32(4), Requests: api.Cores(1), Duration: 100},
				api.TaskSpec{Name: "master", Replicas: 1, MinAvailable: ptr32(1), Requests: api.Cores(1), Duration: 100},
			)
			return w.SubmitJob(job)
		},
		Check: func(w *sim.World) error {
			j, err := job(w, "train")
			if err != nil {
				return err
			}
			if j.Status.Phase != api.JobRunning {
				return fmt.Errorf("job should be Running, got %s (message=%q)", j.Status.Phase, j.Status.Message)
			}
			if got := w.Cache.RunningCount(j.UID); got != 5 {
				return fmt.Errorf("want exactly 5 running (1 master + 4 workers), got %d", got)
			}
			byTask := w.Cache.RunningCountByTask(j.UID)
			if byTask["master"] != 1 || byTask["worker"] != 4 {
				return fmt.Errorf("role gate violated: running=%v, want master=1 worker=4", byTask)
			}
			pg := w.Cache.PodGroups[j.UID]
			if pg == nil || pg.MinTaskMember["master"] != 1 || pg.MinTaskMember["worker"] != 4 {
				return fmt.Errorf("PodGroup.minTaskMember not populated: %+v", pg)
			}
			if got := w.Cache.PendingCount(j.UID); got != 4 {
				return fmt.Errorf("want 4 workers still pending, got %d", got)
			}
			return nil
		},
	}
}

// minSuccessScenario proves early completion: two of four replicas succeeding
// is enough, and the never-started replicas are terminated instead of leaking
// as Pending.
func minSuccessScenario() *Scenario {
	return &Scenario{
		Name:        "min-success",
		Description: "minSuccess=2 completes a 4-replica job after two successes and terminates the unscheduled replicas.",
		Ticks:       4,
		Explain:     []string{"default/train"},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("cpu-node", 2, 8, 0, nil)); err != nil {
				return err
			}
			job := mkJob("train", "default", 0, 2, 4, api.Cores(1), 2)
			job.Spec.Tasks[0].MinAvailable = ptr32(2)
			job.Spec.MinSuccess = ptr32(2)
			return w.SubmitJob(job)
		},
		Check: func(w *sim.World) error {
			if err := expectPhase(w, "train", api.JobCompleted); err != nil {
				return err
			}
			j, err := job(w, "train")
			if err != nil {
				return err
			}
			if got := w.Cache.SucceededCount(j.UID); got != 4 {
				return fmt.Errorf("want 4 succeeded pods after termination, got %d", got)
			}
			if got := w.Cache.TotalIdle()[api.CPU]; got != 2000 {
				return fmt.Errorf("all CPU should be released, idle=%d", got)
			}
			return nil
		},
	}
}

func priorityScenario() *Scenario {
	return &Scenario{
		Name:        "priority",
		Description: "A high-priority job arrives later and preempts four low-priority pods in the same queue.",
		Ticks:       5,
		Explain:     []string{"default/train-high"},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 8, nil)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("train-low", "default", 0, 8, 8, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
		},
		Steps: []Step{{
			Tick: 2,
			Apply: func(w *sim.World) error {
				return w.SubmitJob(mkJob("train-high", "default", 100, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
			},
		}},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "train-high", 4); err != nil {
				return err
			}
			if err := expectRunning(w, "train-low", 4); err != nil {
				return err
			}
			// The victim job is starving but lower priority; it must not
			// preempt the high-priority pods back.
			high, _ := job(w, "train-high")
			if high.Status.Phase != api.JobRunning {
				return fmt.Errorf("high-priority job should be Running, got %s", high.Status.Phase)
			}
			return nil
		},
	}
}

func reclaimScenario() *Scenario {
	return &Scenario{
		Name:        "reclaim",
		Description: "Queue A borrows the whole cluster; queue B reclaims its deserved share.",
		Ticks:       4,
		Explain:     []string{"default/b-job"},
		Build: func(w *sim.World) error {
			if err := w.AddQueue(mkQueue("team-a", 1, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddQueue(mkQueue("team-b", 1, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 8, nil)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("a-job", "team-a", 0, 8, 8, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("b-job", "team-b", 0, 4, 4, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "a-job", 4); err != nil {
				return err
			}
			if err := expectRunning(w, "b-job", 4); err != nil {
				return err
			}
			if got := w.Cache.QueueAllocated("team-a")[api.GPU]; got != 4 {
				return fmt.Errorf("team-a should be reclaimed to deserved 4 GPU, got %d", got)
			}
			return nil
		},
	}
}

func proportionScenario() *Scenario {
	return &Scenario{
		Name:        "proportion",
		Description: "Weighted queues receive a 3:1 share of eight GPUs without preemption.",
		Ticks:       3,
		Build: func(w *sim.World) error {
			if err := w.AddQueue(mkQueue("team-a", 3, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddQueue(mkQueue("team-b", 1, nil, nil, true)); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("gpu-node", 16, 64, 8, nil)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("a-job", "team-a", 0, 6, 6, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("b-job", "team-b", 0, 2, 2, api.Resource{api.CPU: 1000, api.Memory: 2048, api.GPU: 1}, 100))
		},
		Check: func(w *sim.World) error {
			if err := expectQueueAllocated(w, "team-a", api.GPU, 6); err != nil {
				return err
			}
			if err := expectQueueAllocated(w, "team-b", api.GPU, 2); err != nil {
				return err
			}
			if got := w.Cache.Queues["team-a"].Status.Deserved[api.GPU]; got != 6 {
				return fmt.Errorf("team-a deserved GPU: want 6, got %d", got)
			}
			if got := w.Cache.Queues["team-b"].Status.Deserved[api.GPU]; got != 2 {
				return fmt.Errorf("team-b deserved GPU: want 2, got %d", got)
			}
			return nil
		},
	}
}

func drfScenario() *Scenario {
	return &Scenario{
		Name:        "drf",
		Description: "DRF schedules the job with the smaller dominant share when only one CPU remains.",
		Ticks:       3,
		Explain:     []string{"default/a2"},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("cpu-node", 2, 8, 0, nil)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("pre", "default", 0, 1, 1, api.Cores(1), 100)); err != nil {
				return err
			}
			if err := w.SubmitJob(mkJob("a2", "default", 0, 2, 2, api.Cores(1), 100)); err != nil {
				return err
			}
			return w.SubmitJob(mkJob("b", "default", 0, 1, 1, api.Cores(1), 100))
		},
		Check: func(w *sim.World) error {
			if err := expectRunning(w, "pre", 1); err != nil {
				return err
			}
			if err := expectRunning(w, "b", 1); err != nil {
				return err
			}
			if err := expectNotRunning(w, "a2"); err != nil {
				return err
			}
			return expectReason(w, "a2", "gang not ready")
		},
	}
}

func binpackScenario() *Scenario {
	return &Scenario{
		Name:        "binpack",
		Description: "Binpack packs four jobs onto node-a and leaves node-b completely idle.",
		Ticks:       2,
		Config: func() framework.Config {
			return framework.Config{
				Actions: []string{"enqueue", "allocate", "preempt", "reclaim", "backfill"},
				Tiers: []framework.TierConfig{
					{Name: "tier-1", Plugins: []framework.PluginConfig{
						{Name: "priority"}, {Name: "gang"}, {Name: "conformance"},
					}},
					{Name: "tier-2", Plugins: []framework.PluginConfig{
						{Name: "drf"}, {Name: "predicates"}, {Name: "proportion"},
						{Name: "binpack", Args: map[string]string{"weight": "10"}},
					}},
				},
			}
		},
		Build: func(w *sim.World) error {
			if err := addDefault(w); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("node-a", 8, 32, 0, nil)); err != nil {
				return err
			}
			if err := w.AddNode(mkNode("node-b", 8, 32, 0, nil)); err != nil {
				return err
			}
			for i := 0; i < 4; i++ {
				if err := w.SubmitJob(mkJob(fmt.Sprintf("job-%d", i), "default", 0, 1, 1, api.Cores(2), 100)); err != nil {
					return err
				}
			}
			return nil
		},
		Check: func(w *sim.World) error {
			a := w.Cache.Nodes["node-a"]
			b := w.Cache.Nodes["node-b"]
			if a == nil || b == nil {
				return fmt.Errorf("nodes missing")
			}
			if a.Node.Allocated[api.CPU] != 8000 {
				return fmt.Errorf("node-a should be packed to 8 CPU, got %s", a.Node.Allocated)
			}
			if b.Node.Allocated[api.CPU] != 0 {
				return fmt.Errorf("node-b should be idle, got %s", b.Node.Allocated)
			}
			return nil
		},
	}
}
