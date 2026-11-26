package controller

import (
	"testing"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/metrics"
)

func fixture(t *testing.T) *cache.Cache {
	t.Helper()
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(8000, 32768, 0)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
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

func job(name string, replicas int32, duration int64) *api.Job {
	return &api.Job{Name: name, Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MaxRetry: 2, MinAvailable: replicas, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: replicas, Requests: api.Cores(1), Duration: duration}},
	}}
}

func TestJobCompletes(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, job("j", 1, 1))
	if err := c.Bind("default/j-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	m := New(c, metrics.New())
	m.Sync(1)
	if j.Status.Phase != api.JobRunning {
		t.Fatalf("phase after tick1=%s", j.Status.Phase)
	}
	m.Sync(2)
	if j.Status.Phase != api.JobCompleted {
		t.Fatalf("phase after tick2=%s", j.Status.Phase)
	}
	if c.Nodes["n1"].Node.Allocated[api.CPU] != 0 {
		t.Fatal("completed job did not release resources")
	}
	if v, _ := m.Metrics.Value("minivolcano_jobs_completed_total", map[string]string{"queue": "default"}); v != 1 {
		t.Fatalf("completed metric=%v", v)
	}
}

func TestRestartJobPolicy(t *testing.T) {
	c := fixture(t)
	j := job("j", 2, 5)
	j.Spec.Policies = []api.LifecyclePolicy{{Event: api.EventPodFailed, Action: api.ActionRestartJob}}
	j = submit(t, c, j)
	for i := 0; i < 2; i++ {
		if err := c.Bind("default/j-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	m := New(c, metrics.New())
	m.Sync(1)
	m.ScheduleFailure("default/j-w-0", 2)
	m.Sync(2)
	if j.Status.Retries != 1 {
		t.Fatalf("retries=%d", j.Status.Retries)
	}
	for _, p := range c.JobPods(j.UID) {
		if p.Phase != api.PodPending {
			t.Fatalf("pod %s not reset: %s", p.Name, p.Phase)
		}
		if p.Restarts != 1 {
			t.Fatalf("pod %s restarts=%d", p.Name, p.Restarts)
		}
	}
}

func TestMaxRetryFailsJob(t *testing.T) {
	c := fixture(t)
	j := job("j", 1, 5)
	j.Spec.MaxRetry = 0
	j = submit(t, c, j)
	if err := c.Bind("default/j-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	m := New(c, metrics.New())
	m.Sync(1)
	m.ScheduleFailure("default/j-w-0", 2)
	m.Sync(2)
	if j.Status.Phase != api.JobFailed {
		t.Fatalf("phase=%s message=%s", j.Status.Phase, j.Status.Message)
	}
}

func TestEvictionDoesNotRestartByDefault(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, job("j", 1, 5))
	if err := c.Bind("default/j-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	m := New(c, metrics.New())
	m.Sync(1)
	if err := c.Evict("default/j-w-0", 2); err != nil {
		t.Fatal(err)
	}
	m.Sync(2)
	p := c.Pods["default/j-w-0"]
	if p.Phase != api.PodPending {
		t.Fatalf("evicted pod phase=%s", p.Phase)
	}
	if j.Status.Retries != 0 {
		t.Fatalf("eviction must not consume maxRetry, retries=%d", j.Status.Retries)
	}
}

func TestPodGroupAndQueueStatus(t *testing.T) {
	c := fixture(t)
	j := submit(t, c, job("j", 2, 5))
	for i := 0; i < 2; i++ {
		if err := c.Bind("default/j-w-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	m := New(c, metrics.New())
	m.Sync(1)
	pg := c.PodGroups[j.UID]
	if pg.Phase != api.PodGroupRunning {
		t.Fatalf("pg phase=%s", pg.Phase)
	}
	if pg.Running != 2 {
		t.Fatalf("pg running=%d", pg.Running)
	}
	if c.Queues["default"].Status.Running != 1 {
		t.Fatalf("queue running jobs=%d", c.Queues["default"].Status.Running)
	}
	if c.Queues["default"].Status.Allocated[api.CPU] != 2000 {
		t.Fatalf("queue allocated=%v", c.Queues["default"].Status.Allocated)
	}
}

func TestCompleteJobPolicy(t *testing.T) {
	c := fixture(t)
	j := job("j", 1, 5)
	j.Spec.Tasks[0].Policies = []api.LifecyclePolicy{{Event: api.EventTaskCompleted, Action: api.ActionCompleteJob}}
	j = submit(t, c, j)
	if err := c.Bind("default/j-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	m := New(c, metrics.New())
	m.Sync(1)
	m.Sync(6)
	if j.Status.Phase != api.JobCompleted {
		t.Fatalf("phase=%s", j.Status.Phase)
	}
}

func TestCronForbidConcurrency(t *testing.T) {
	c := fixture(t)
	m := New(c, metrics.New())
	if err := m.AddCron(&api.CronJob{
		Name: "c", Namespace: "default", ScheduleTicks: 1, ConcurrencyPolicy: "Forbid",
		NextTick: 1, Spec: api.JobSpec{Queue: "default", MinAvailable: 1, MinAvailableSet: true, MaxRetry: 1,
			Tasks: []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Cores(1), Duration: 5}}},
	}); err != nil {
		t.Fatal(err)
	}
	for tick := int64(1); tick <= 4; tick++ {
		m.Sync(tick)
	}
	if len(m.CronJobs[0].Created) != 1 {
		t.Fatalf("Forbid created %d runs", len(m.CronJobs[0].Created))
	}
}

func TestJobFlowCreatesFirstStep(t *testing.T) {
	c := fixture(t)
	m := New(c, metrics.New())
	if err := m.AddFlow(&api.JobFlow{
		Name: "f", Namespace: "default",
		Steps: []api.JobFlowStep{
			{Name: "a", Spec: api.JobSpec{Queue: "default", MinAvailable: 1, MinAvailableSet: true,
				Tasks: []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Cores(1), Duration: 1}}}},
			{Name: "b", DependsOn: []string{"a"}, Spec: api.JobSpec{Queue: "default", MinAvailable: 1, MinAvailableSet: true,
				Tasks: []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Cores(1), Duration: 1}}}},
		},
	}); err != nil {
		t.Fatal(err)
	}
	m.Sync(0)
	if _, ok := m.Flows[0].Created["a"]; !ok {
		t.Fatal("first step not created")
	}
	if _, ok := m.Flows[0].Created["b"]; ok {
		t.Fatal("dependent step created too early")
	}
}

func TestCronValidation(t *testing.T) {
	m := New(fixture(t), metrics.New())
	if err := m.AddCron(&api.CronJob{Name: "c", ScheduleTicks: 0}); err == nil {
		t.Fatal("zero schedule should fail")
	}
	if err := m.AddFlow(&api.JobFlow{Name: "f", Steps: []api.JobFlowStep{{Name: "b", DependsOn: []string{"missing"}}}}); err == nil {
		t.Fatal("unknown dependency should fail")
	}
}

func TestCompleteJobReleasesOtherReplicas(t *testing.T) {
	c := fixture(t)
	j := &api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MaxRetry: 2, MinAvailable: 2, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			{Name: "short", Replicas: 1, Requests: api.Cores(1), Duration: 1,
				Policies: []api.LifecyclePolicy{{Event: api.EventTaskCompleted, Action: api.ActionCompleteJob}}},
			{Name: "long", Replicas: 1, Requests: api.Cores(1), Duration: 100},
		},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"default/j-short-0", "default/j-long-0"} {
		if err := c.Bind(name, "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	m := New(c, metrics.New())
	m.Sync(1)
	m.Sync(2)
	stored := c.Jobs["default/j"]
	if stored.Status.Phase != api.JobCompleted {
		t.Fatalf("phase=%s", stored.Status.Phase)
	}
	if got := c.Nodes["n1"].Node.Allocated[api.CPU]; got != 0 {
		t.Fatalf("CompleteJob leaked %d CPU on the node", got)
	}
	if p := c.Pods["default/j-long-0"]; p.Phase != api.PodSucceeded {
		t.Fatalf("long replica should be terminated, got %s", p.Phase)
	}
}

func TestTaskCompletedFiresOnce(t *testing.T) {
	c := fixture(t)
	j := &api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MaxRetry: 2, MinAvailable: 1, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Cores(1), Duration: 1,
			Policies: []api.LifecyclePolicy{{Event: api.EventTaskCompleted, Action: api.ActionTerminateTask}}}},
	}}
	if err := c.SubmitJob(j); err != nil {
		t.Fatal(err)
	}
	if err := c.Bind("default/j-w-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	m := New(c, metrics.New())
	m.Sync(1)
	m.Sync(2)
	count := 0
	for _, e := range m.Events {
		if e.Type == "TerminateTask" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("TaskCompleted policy fired %d times, want 1", count)
	}
}

func TestRoleGatesBlockRunning(t *testing.T) {
	c := fixture(t)
	gate := func(v int32) *int32 { return &v }
	j := &api.Job{Name: "roles", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 5, MinAvailableSet: true,
		Tasks: []api.TaskSpec{
			{Name: "master", Replicas: 1, MinAvailable: gate(1), Requests: api.Cores(1), Duration: 100},
			{Name: "worker", Replicas: 8, MinAvailable: gate(4), Requests: api.Cores(1), Duration: 100},
		},
	}}
	j = submit(t, c, j)
	// Five workers, no master: the total gate is met but the role gate is not.
	for i := 0; i < 5; i++ {
		if err := c.Bind("default/roles-worker-"+string(rune('0'+i)), "n1", 0); err != nil {
			t.Fatal(err)
		}
	}
	m := New(c, metrics.New())
	m.Sync(1)
	if j.Status.Phase == api.JobRunning {
		t.Fatal("controller marked Running without the master role")
	}
	if err := c.Bind("default/roles-master-0", "n1", 0); err != nil {
		t.Fatal(err)
	}
	m.Sync(2)
	if j.Status.Phase != api.JobRunning {
		t.Fatalf("phase with all gates met=%s, want Running", j.Status.Phase)
	}
}
