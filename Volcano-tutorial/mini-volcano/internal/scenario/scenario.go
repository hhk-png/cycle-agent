// Package scenario contains deterministic, self-checking experiments. They are
// both the CLI demo catalogue and the integration-test suite.
package scenario

import (
	"fmt"
	"sort"
	"strings"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/sim"
)

type Step struct {
	Tick  int
	Apply func(*sim.World) error
}

type Scenario struct {
	Name        string
	Description string
	Ticks       int
	Config      func() framework.Config
	Build       func(*sim.World) error
	Steps       []Step
	Check       func(*sim.World) error
	Explain     []string
}

type Outcome struct {
	Name string
	Err  error
}

func All() []*Scenario {
	var out []*Scenario
	out = append(out, coreScenarios()...)
	out = append(out, advancedScenarios()...)
	out = append(out, governanceScenarios()...)
	return out
}

func Names() []string {
	var out []string
	for _, s := range All() {
		out = append(out, s.Name)
	}
	sort.Strings(out)
	return out
}

func Get(name string) (*Scenario, error) {
	for _, s := range All() {
		if s.Name == name {
			return s, nil
		}
	}
	return nil, fmt.Errorf("unknown scenario %q (available: %s)", name, strings.Join(Names(), ", "))
}

// RunScenario builds a fresh world, applies what-if overrides, runs the tick
// loop and evaluates the scenario's assertions.
func RunScenario(sc *Scenario, overrides map[string]int64) (*sim.World, error) {
	cfg := framework.DefaultConfig()
	if sc.Config != nil {
		cfg = sc.Config()
	}
	w, err := sim.New(sc.Name, sc.Description, cfg)
	if err != nil {
		return nil, err
	}
	if sc.Build != nil {
		if err := sc.Build(w); err != nil {
			return nil, fmt.Errorf("%s build: %w", sc.Name, err)
		}
	}
	for key, value := range overrides {
		if err := applyOverride(w, key, value); err != nil {
			return nil, err
		}
	}
	if err := w.Start(); err != nil {
		return nil, fmt.Errorf("%s start: %w", sc.Name, err)
	}
	for tick := 1; tick <= sc.Ticks; tick++ {
		for _, step := range sc.Steps {
			if step.Tick == tick {
				if err := step.Apply(w); err != nil {
					return nil, fmt.Errorf("%s step at tick %d: %w", sc.Name, tick, err)
				}
			}
		}
		if err := w.Tick(); err != nil {
			return nil, fmt.Errorf("%s tick %d: %w", sc.Name, tick, err)
		}
	}
	if sc.Check != nil {
		if err := sc.Check(w); err != nil {
			return w, fmt.Errorf("%s check: %w", sc.Name, err)
		}
	}
	return w, nil
}

func Run(name string) (*sim.World, error) {
	sc, err := Get(name)
	if err != nil {
		return nil, err
	}
	return RunScenario(sc, nil)
}

func RunAll() []Outcome {
	var out []Outcome
	for _, sc := range All() {
		_, err := RunScenario(sc, nil)
		out = append(out, Outcome{Name: sc.Name, Err: err})
	}
	return out
}

func applyOverride(w *sim.World, key string, value int64) error {
	parts := strings.SplitN(key, "/", 2)
	if len(parts) != 2 {
		return fmt.Errorf("override %q must be queue/resource=value", key)
	}
	q := w.Cache.Queues[parts[0]]
	if q == nil {
		return fmt.Errorf("override: queue %q not found", parts[0])
	}
	if q.Spec.Capability == nil {
		q.Spec.Capability = api.Resource{}
	}
	q.Spec.Capability[parts[1]] = value
	return nil
}

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

func mkNode(name string, cpuCores float64, memGi int64, gpu int64, labels map[string]string) *api.Node {
	alloc := api.Resource{api.CPU: int64(cpuCores * 1000), api.Memory: memGi * 1024}
	if gpu > 0 {
		alloc[api.GPU] = gpu
	}
	return &api.Node{Name: name, Allocatable: alloc, Labels: labels}
}

func mkQueue(name string, weight int32, capability, guarantee api.Resource, reclaimable bool) *api.Queue {
	q := &api.Queue{Name: name, Spec: api.QueueSpec{
		Weight: weight, Capability: capability, Guarantee: guarantee, Reclaimable: &reclaimable,
	}}
	return q
}

func mkJob(name, queue string, priority, minAvailable, replicas int32, req api.Resource, duration int64) *api.Job {
	task := api.TaskSpec{Name: "worker", Replicas: replicas, Requests: req.Clone(), Duration: duration}
	spec := api.JobSpec{Queue: queue, Priority: priority, MaxRetry: 3, Tasks: []api.TaskSpec{task}}
	if minAvailable >= 0 {
		spec.MinAvailable = minAvailable
		spec.MinAvailableSet = true
	}
	return &api.Job{Name: name, Namespace: "default", Spec: spec}
}

func mkMultiTask(name, queue string, priority, minAvailable int32, tasks ...api.TaskSpec) *api.Job {
	spec := api.JobSpec{Queue: queue, Priority: priority, MaxRetry: 3, Tasks: tasks}
	if minAvailable >= 0 {
		spec.MinAvailable = minAvailable
		spec.MinAvailableSet = true
	}
	return &api.Job{Name: name, Namespace: "default", Spec: spec}
}

func annotate(j *api.Job, k, v string) *api.Job {
	if j.Spec.Annotations == nil {
		j.Spec.Annotations = map[string]string{}
	}
	j.Spec.Annotations[k] = v
	return j
}

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

func job(w *sim.World, name string) (*api.Job, error) {
	for _, j := range w.Cache.Jobs {
		if j.Name == name {
			return j, nil
		}
	}
	return nil, fmt.Errorf("job %q not found", name)
}

func expectPhase(w *sim.World, name string, phases ...api.JobPhase) error {
	j, err := job(w, name)
	if err != nil {
		return err
	}
	for _, p := range phases {
		if j.Status.Phase == p {
			return nil
		}
	}
	return fmt.Errorf("job %s: want phase in %v, got %s (message=%q)", name, phases, j.Status.Phase, j.Status.Message)
}

func expectRunning(w *sim.World, name string, atLeast int) error {
	j, err := job(w, name)
	if err != nil {
		return err
	}
	got := w.Cache.RunningCount(j.UID)
	if got < atLeast {
		return fmt.Errorf("job %s: want >= %d running pods, got %d (phase=%s message=%q)",
			name, atLeast, got, j.Status.Phase, j.Status.Message)
	}
	return nil
}

func expectNotRunning(w *sim.World, name string) error {
	j, err := job(w, name)
	if err != nil {
		return err
	}
	if n := w.Cache.RunningCount(j.UID); n > 0 {
		return fmt.Errorf("job %s: expected no running pods, got %d", name, n)
	}
	return nil
}

func expectQueueAllocated(w *sim.World, queue, resource string, want int64) error {
	q := w.Cache.Queues[queue]
	if q == nil {
		return fmt.Errorf("queue %q not found", queue)
	}
	got := w.Cache.QueueAllocated(queue)[resource]
	if got != want {
		return fmt.Errorf("queue %s %s: want %d, got %d", queue, resource, want, got)
	}
	return nil
}

func expectAllOnNodes(w *sim.World, name, prefix string) error {
	j, err := job(w, name)
	if err != nil {
		return err
	}
	for _, p := range w.Cache.JobPods(j.UID) {
		if !strings.HasPrefix(p.NodeName, prefix) {
			return fmt.Errorf("job %s pod %s is on %q, want prefix %q", name, p.Name, p.NodeName, prefix)
		}
	}
	return nil
}

func expectSameNode(w *sim.World, name string) error {
	j, err := job(w, name)
	if err != nil {
		return err
	}
	node := ""
	for _, p := range w.Cache.JobPods(j.UID) {
		if p.NodeName == "" {
			return fmt.Errorf("job %s pod %s is not bound", name, p.Name)
		}
		if node == "" {
			node = p.NodeName
		} else if p.NodeName != node {
			return fmt.Errorf("job %s pods are on different nodes: %s vs %s", name, node, p.NodeName)
		}
	}
	return nil
}

func expectReason(w *sim.World, name, substr string) error {
	j, err := job(w, name)
	if err != nil {
		return err
	}
	for _, r := range w.Scheduler.ExplainReasons(j.UID) {
		if strings.Contains(r, substr) {
			return nil
		}
	}
	return fmt.Errorf("job %s: no unschedulable reason containing %q; reasons=%v", name, substr, w.Scheduler.ExplainReasons(j.UID))
}
