package api

import "testing"

func testJob() *Job {
	return &Job{
		Name: "j", Namespace: "default",
		Spec: JobSpec{
			Queue: "default", MinAvailable: 2, MinAvailableSet: true,
			Tasks: []TaskSpec{{Name: "worker", Replicas: 2, Requests: Cores(1)}},
		},
	}
}

func TestValidateQueue(t *testing.T) {
	q := &Queue{Name: "q", Spec: QueueSpec{Weight: 1, Capability: Resource{GPU: 4}, Guarantee: Resource{GPU: 5}}}
	if err := ValidateQueue(q); err == nil {
		t.Fatal("guarantee > capability should fail")
	}
	q.Spec.Guarantee = Resource{GPU: 2}
	if err := ValidateQueue(q); err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	q.Spec.State = "Bogus"
	if err := ValidateQueue(q); err == nil {
		t.Fatal("invalid state should fail")
	}
}

func TestValidateJob(t *testing.T) {
	j := testJob()
	if err := ValidateJob(j); err != nil {
		t.Fatalf("valid job rejected: %v", err)
	}
	j.Spec.MinAvailable = 3
	if err := ValidateJob(j); err == nil {
		t.Fatal("minAvailable > replicas should fail")
	}
	j = testJob()
	j.Spec.Tasks = append(j.Spec.Tasks, TaskSpec{Name: "worker", Replicas: 1})
	if err := ValidateJob(j); err == nil {
		t.Fatal("duplicate task should fail")
	}
	j = testJob()
	j.Spec.NetworkTopology = &NetworkTopologySpec{Mode: "fuzzy"}
	if err := ValidateJob(j); err == nil {
		t.Fatal("bad topology mode should fail")
	}
	j = testJob()
	j.Spec.TaskTopology = []TaskTopologySpec{{TaskNames: []string{"ghost"}, Policy: "affinity"}}
	if err := ValidateJob(j); err == nil {
		t.Fatal("unknown task topology reference should fail")
	}
	j = testJob()
	j.Spec.Tasks[0].Replicas = 0
	j.Spec.MinAvailable = 0
	if err := ValidateJob(j); err == nil {
		t.Fatal("job with zero total replicas should fail")
	}
}

func TestValidatePolicy(t *testing.T) {
	if err := ValidatePolicy(LifecyclePolicy{Event: EventPodFailed, Action: ActionRestartJob}); err != nil {
		t.Fatalf("valid policy: %v", err)
	}
	if err := ValidatePolicy(LifecyclePolicy{Event: "Nope", Action: ActionRestartJob}); err == nil {
		t.Fatal("invalid event should fail")
	}
	if err := ValidatePolicy(LifecyclePolicy{Event: EventPodFailed, Action: "Explode"}); err == nil {
		t.Fatal("invalid action should fail")
	}
}

func TestMutateJobDefaultsMinAvailable(t *testing.T) {
	j := &Job{Name: "j", Namespace: "default", Spec: JobSpec{
		Tasks: []TaskSpec{{Name: "a", Replicas: 2}, {Name: "b", Replicas: 3}},
	}}
	MutateJob(j)
	if j.Spec.MinAvailable != 5 || !j.Spec.MinAvailableSet {
		t.Fatalf("minAvailable default: %d set=%v", j.Spec.MinAvailable, j.Spec.MinAvailableSet)
	}
	if j.Spec.SchedulerName != "volcano" || j.Spec.Queue != "default" {
		t.Fatalf("defaults: %+v", j.Spec)
	}
}

func TestMutateJobExplicitZeroDisablesGang(t *testing.T) {
	j := &Job{Name: "j", Namespace: "default", Spec: JobSpec{
		MinAvailable: 0, MinAvailableSet: true,
		Tasks: []TaskSpec{{Name: "a", Replicas: 2}},
	}}
	MutateJob(j)
	if j.Spec.MinAvailable != 0 {
		t.Fatalf("explicit zero must be preserved, got %d", j.Spec.MinAvailable)
	}
}

func TestMutateJobDefaultsToTaskMinima(t *testing.T) {
	j := &Job{Name: "j", Namespace: "default", Spec: JobSpec{
		Tasks: []TaskSpec{
			{Name: "master", Replicas: 1, MinAvailable: int32Value(1)},
			{Name: "worker", Replicas: 8, MinAvailable: int32Value(4)},
		},
	}}
	MutateJob(j)
	if j.Spec.MinAvailable != 5 {
		t.Fatalf("default minAvailable should be sum of task minima 5, got %d", j.Spec.MinAvailable)
	}
}

func TestRoleGatesInactiveWhenJobMinBelowTaskSum(t *testing.T) {
	j := &Job{Name: "j", Namespace: "default", Spec: JobSpec{
		MinAvailable: 3, MinAvailableSet: true,
		Tasks: []TaskSpec{
			{Name: "master", Replicas: 1, MinAvailable: int32Value(1)},
			{Name: "worker", Replicas: 8, MinAvailable: int32Value(4)},
		},
	}}
	MutateJob(j)
	if err := ValidateJob(j); err != nil {
		t.Fatalf("a job min below the role sum is legal (gates inactive): %v", err)
	}
	if j.RoleGatesActive() {
		t.Fatal("role gates must be inactive when job min < sum of task minima")
	}
	j.Spec.MinAvailable = 5
	if !j.RoleGatesActive() {
		t.Fatal("role gates must be active when job min >= sum of task minima")
	}
}

func TestValidateMinSuccess(t *testing.T) {
	j := testJob()
	j.Spec.Tasks[0].Replicas = 4
	j.Spec.MinSuccess = int32Value(3)
	if err := ValidateJob(j); err != nil {
		t.Fatalf("minSuccess within replicas: %v", err)
	}
	j.Spec.MinSuccess = int32Value(99)
	if err := ValidateJob(j); err == nil {
		t.Fatal("minSuccess above replicas must be rejected")
	}
}

func int32Value(v int32) *int32 { return &v }

func TestMinResources(t *testing.T) {
	j := testJob()
	if got := j.MinResources()[CPU]; got != 2000 {
		t.Fatalf("min resources cpu=%d", got)
	}
	j.Spec.MinAvailable = 1
	if got := j.MinResources()[CPU]; got != 1000 {
		t.Fatalf("min resources with min=1 cpu=%d", got)
	}
	j.Spec.MinAvailable = 0
	if got := j.MinResources()[CPU]; got != 1000 {
		t.Fatalf("minAvailable=0 should need one replica, got %d", got)
	}
}

func TestMinResourcesReservesRoleGatesFirst(t *testing.T) {
	j := &Job{Name: "j", Namespace: "default", Spec: JobSpec{
		MinAvailable: 5, MinAvailableSet: true,
		Tasks: []TaskSpec{
			// Worker is declared first and requests more CPU. A naive first-N
			// calculation would reserve 5 workers = 10 CPU; the role gates
			// require 4 workers + 1 master = 9 CPU.
			{Name: "worker", Replicas: 8, MinAvailable: int32Value(4), Requests: Cores(2)},
			{Name: "master", Replicas: 1, MinAvailable: int32Value(1), Requests: Cores(1)},
		},
	}}
	if got := j.MinResources()[CPU]; got != 9000 {
		t.Fatalf("role-aware min resources cpu=%d, want 9000", got)
	}
}

func TestCloneIsolation(t *testing.T) {
	q := &Queue{Name: "q", Spec: QueueSpec{Weight: 1}}
	c := q.Clone()
	if c.Spec.Capability != nil {
		t.Fatal("nil capability must stay nil after clone")
	}
	c.Spec.Capability = Resource{GPU: 1}
	if q.Spec.Capability != nil {
		t.Fatal("queue clone aliased capability")
	}
	j := testJob()
	j.Spec.Tasks[0].Requests[CPU] = 999
	jc := j.Clone()
	jc.Spec.Tasks[0].Requests[CPU] = 1
	if j.Spec.Tasks[0].Requests[CPU] != 999 {
		t.Fatal("job clone aliased task requests")
	}
}

func TestJobPhaseTerminal(t *testing.T) {
	for _, p := range []JobPhase{JobPending, JobInqueue, JobRunning, JobRestarting, JobCompleting, JobTerminating} {
		if p.Terminal() {
			t.Errorf("%s should not be terminal", p)
		}
	}
	for _, p := range []JobPhase{JobCompleted, JobAborted, JobFailed} {
		if !p.Terminal() {
			t.Errorf("%s should be terminal", p)
		}
	}
}

func TestValidateReservation(t *testing.T) {
	r := &Reservation{Name: "r", Queue: "q", NodeNames: []string{"n"}, Resources: Resource{GPU: 1}, StartTick: 0, EndTick: 1}
	if err := ValidateReservation(r); err != nil {
		t.Fatalf("valid reservation: %v", err)
	}
	r.EndTick = 0
	if err := ValidateReservation(r); err == nil {
		t.Fatal("empty window should fail")
	}
	r.EndTick = 2
	r.Resources = Resource{}
	if err := ValidateReservation(r); err == nil {
		t.Fatal("zero resources should fail")
	}
}

func TestValidateQueueNodeGroup(t *testing.T) {
	q := &Queue{Name: "q", Spec: QueueSpec{NodeGroup: &NodeGroupSpec{}}}
	if err := ValidateQueue(q); err == nil {
		t.Fatal("empty node group should be rejected")
	}
	q.Spec.NodeGroup = &NodeGroupSpec{Names: []string{"a", "a"}}
	if err := ValidateQueue(q); err == nil {
		t.Fatal("duplicate node group names should be rejected")
	}
	q.Spec.NodeGroup = &NodeGroupSpec{Names: []string{"a"}, Capability: map[string]Resource{"a": {CPU: -1}}}
	if err := ValidateQueue(q); err == nil {
		t.Fatal("negative group capability should be rejected")
	}
	q.Spec.NodeGroup = &NodeGroupSpec{Names: []string{"a"}, Capability: map[string]Resource{"a": {CPU: 4000}}}
	if err := ValidateQueue(q); err != nil {
		t.Fatalf("valid node group rejected: %v", err)
	}
}
