package controller_test

import (
	"testing"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/controller"
	"example.com/minikueue/internal/scenario"
)

func get(t *testing.T, e *controller.Engine, ns, name string) *api.Workload {
	t.Helper()
	w, ok := e.Store.GetWorkload(ns, name)
	if !ok {
		t.Fatalf("workload %s/%s not found", ns, name)
	}
	return w
}

func TestBasicLifecycle(t *testing.T) {
	e := scenario.Basic()
	e.Tick()
	if got := get(t, e, "default", "job-a").Status.State; got != api.StateAdmitted {
		t.Fatalf("job-a state = %s", got)
	}
	if got := get(t, e, "default", "job-c").Status.State; got != api.StatePending {
		t.Fatalf("job-c state = %s", got)
	}
	if err := e.Finish("default", "job-a"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if got := get(t, e, "default", "job-c").Status.State; got != api.StateAdmitted {
		t.Fatalf("job-c state after release = %s", got)
	}
	if got := e.Cache.FlavorUsage("basic", "default", api.CPU); got != 2000 {
		t.Fatalf("cache cpu = %d, want 2000", got)
	}
}

func TestStrictFIFOBlocksTail(t *testing.T) {
	e := scenario.StrictVersusBestEffort(api.StrictFIFO)
	e.Tick()
	if got := get(t, e, "default", "small-tail").Status.State; got != api.StatePending {
		t.Fatalf("small-tail state = %s", got)
	}
}

func TestBestEffortFIFOSkipsHead(t *testing.T) {
	e := scenario.StrictVersusBestEffort(api.BestEffortFIFO)
	e.Tick()
	if got := get(t, e, "default", "small-tail").Status.State; got != api.StateAdmitted {
		t.Fatalf("small-tail state = %s", got)
	}
	if got := get(t, e, "default", "big-head").Status.State; got != api.StatePending {
		t.Fatalf("big-head state = %s", got)
	}
}

func TestPreemptionEvictsAndRequeues(t *testing.T) {
	e := scenario.Preemption()
	if got := get(t, e, "default", "low").Status.State; got != api.StateAdmitted {
		t.Fatalf("low state = %s", got)
	}
	e.Tick()
	low := get(t, e, "default", "low")
	if low.Status.State != api.StatePending {
		t.Fatalf("low state after preemption = %s", low.Status.State)
	}
	if c, ok := low.Status.Condition("Evicted"); !ok || c.Reason != "Preempted" {
		t.Fatalf("expected Evicted/Preempted condition, got %+v", low.Status.Conditions)
	}
	high := get(t, e, "default", "high")
	if high.Status.State != api.StateAdmitted {
		t.Fatalf("high state = %s", high.Status.State)
	}
	if got := e.MetricsSnapshot().PreemptedTotal; got != 1 {
		t.Fatalf("preempted total = %d", got)
	}
	// After the high-priority workload finishes, the low-priority workload
	// becomes eligible again when its requeue backoff expires.
	if err := e.Finish("default", "high"); err != nil {
		t.Fatal(err)
	}
	e.Tick() // tick 3: still in backoff
	if got := get(t, e, "default", "low").Status.State; got != api.StatePending {
		t.Fatalf("low state during backoff = %s", got)
	}
	e.Tick() // tick 4: requeue at 4
	if got := get(t, e, "default", "low").Status.State; got != api.StateAdmitted {
		t.Fatalf("low state after backoff = %s", got)
	}
}

func TestAdmissionCheckTwoPhase(t *testing.T) {
	e := scenario.AdmissionCheck()
	e.Tick()
	w := get(t, e, "default", "checked-job")
	if w.Status.State != api.StateQuotaReserved {
		t.Fatalf("state = %s", w.Status.State)
	}
	if err := e.ApproveCheck("default", "checked-job", "manual-check", "Ready", "ok"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if got := get(t, e, "default", "checked-job").Status.State; got != api.StateAdmitted {
		t.Fatalf("state after approval = %s", got)
	}
}

func TestAdmissionCheckRejected(t *testing.T) {
	e := scenario.AdmissionCheck()
	e.Tick()
	if err := e.ApproveCheck("default", "checked-job", "manual-check", "Rejected", "bad image"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	w := get(t, e, "default", "checked-job")
	if w.Status.State != api.StatePending {
		t.Fatalf("state = %s", w.Status.State)
	}
	if w.Spec.Active {
		t.Fatal("rejected workload should be deactivated")
	}
	if c, ok := w.Status.Condition("Evicted"); !ok || c.Reason != "AdmissionCheck" {
		t.Fatalf("unexpected conditions: %+v", w.Status.Conditions)
	}
}

func TestAdmissionCheckRetryReleasesAndRequeues(t *testing.T) {
	e := scenario.AdmissionCheck()
	e.Tick()
	if err := e.ApproveCheck("default", "checked-job", "manual-check", "Retry", "capacity not ready"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	w := get(t, e, "default", "checked-job")
	if w.Status.State == api.StateAdmitted {
		t.Fatal("retry must not admit the workload")
	}
	if c, ok := w.Status.Condition("QuotaReserved"); !ok || c.Reason != "AdmissionCheckRetry" {
		// The workload may already have been re-reserved in the same tick;
		// either way it must not be Admitted.
		if w.Status.State != api.StateQuotaReserved {
			t.Fatalf("unexpected state after retry: %s, conditions=%+v", w.Status.State, w.Status.Conditions)
		}
	}
	if got := e.MetricsSnapshot().QuotaReservedTotal; got != 2 {
		t.Fatalf("quota reservations = %d, want 2", got)
	}
}

func TestConfigurablePreemptionAuthorizesEviction(t *testing.T) {
	e := scenario.ConfigurablePreemption()
	e.Tick()
	low := get(t, e, "default", "batch-job")
	if low.Status.State != api.StatePending {
		t.Fatalf("batch job state after preemption = %s", low.Status.State)
	}
	if c, ok := low.Status.Condition("Preempted"); !ok || c.Status != "True" {
		t.Fatalf("expected Preempted condition: %+v", low.Status.Conditions)
	}
	high := get(t, e, "default", "hero-job")
	if high.Status.State != api.StateAdmitted {
		t.Fatalf("hero job state = %s", high.Status.State)
	}
}

func TestFairSharingPreemptionReclaimsBorrowedQuota(t *testing.T) {
	e := scenario.FairSharingPreemption()
	e.Tick()
	borrower := get(t, e, "team-a", "borrower")
	if borrower.Status.State != api.StatePending {
		t.Fatalf("borrower state = %s", borrower.Status.State)
	}
	reclaimer := get(t, e, "team-b", "reclaimer")
	if reclaimer.Status.State != api.StateAdmitted {
		t.Fatalf("reclaimer state = %s", reclaimer.Status.State)
	}
}

func TestProvisioningRequestLiteRetriesThenAdmits(t *testing.T) {
	e := scenario.ProvisioningRequest()
	e.Tick() // tick 1: reserve, first provisioning attempt fails
	w := get(t, e, "default", "provisioned-job")
	if w.Status.State != api.StatePending {
		t.Fatalf("state after failed provisioning = %s", w.Status.State)
	}
	check, ok := w.Status.GetAdmissionCheck("provisioning-check")
	if !ok || check.RetryCount != 1 || check.RequeueAfterSeconds != 2 {
		t.Fatalf("provisioning check = %+v", check)
	}
	if w.Status.RequeueAt != 3 {
		t.Fatalf("requeueAt = %d, want 3", w.Status.RequeueAt)
	}
	e.Tick() // tick 2: backoff
	if got := get(t, e, "default", "provisioned-job").Status.State; got != api.StatePending {
		t.Fatalf("state during backoff = %s", got)
	}
	e.Tick() // tick 3: reserve again, provisioning in progress
	if got := get(t, e, "default", "provisioned-job").Status.State; got != api.StateQuotaReserved {
		t.Fatalf("state at reservation = %s", got)
	}
	e.Tick() // tick 4: delay not elapsed
	if got := get(t, e, "default", "provisioned-job").Status.State; got != api.StateQuotaReserved {
		t.Fatalf("state before provisioning delay = %s", got)
	}
	e.Tick() // tick 5: provisioned and admitted
	if got := get(t, e, "default", "provisioned-job").Status.State; got != api.StateAdmitted {
		t.Fatalf("state after provisioning = %s", got)
	}
}

func TestExponentialBackoffOverridesFixedPreemptionDelay(t *testing.T) {
	e := scenario.Preemption()
	e.Backoff = &controller.BackoffConfig{BaseTicks: 3, MaxTicks: 5, Multiplier: 2}
	e.Tick() // tick 2: high preempts low; first attempt delay = 3
	low := get(t, e, "default", "low")
	if low.Status.RequeueAt != 5 {
		t.Fatalf("requeueAt = %d, want 5", low.Status.RequeueAt)
	}
	if low.Status.Requeues != 1 {
		t.Fatalf("requeues = %d, want 1", low.Status.Requeues)
	}
}

func TestAutoApprovedCheck(t *testing.T) {
	e := scenario.New()
	cq := scenario.SimpleCQ("cq", "default", api.RL("cpu", 2000))
	cq.Spec.AdmissionChecks = []string{"auto"}
	scenario.MustCQ(e, cq)
	scenario.MustCheck(e, "auto", "auto")
	scenario.MustLQ(e, "default", "q", "cq")
	scenario.MustNamespace(e, "default", nil)
	scenario.MustSubmit(e, scenario.Workload("default", "job", "q", 0, 1, api.RL("cpu", 1000)))
	e.Tick()
	if got := get(t, e, "default", "job").Status.State; got != api.StateAdmitted {
		t.Fatalf("state = %s", got)
	}
}

func TestSubmitResetsStaleLifecycle(t *testing.T) {
	e := scenario.Basic()
	e.Tick()
	old := get(t, e, "default", "job-a")
	if err := e.Finish("default", "job-a"); err != nil {
		t.Fatal(err)
	}
	old.Status.RequeueAt = 99
	old.Status.Evictions = 3
	old.Status.Admission = nil
	if err := e.Submit(old); err != nil {
		t.Fatal(err)
	}
	w := get(t, e, "default", "job-a")
	if w.Status.State != api.StatePending || w.Status.RequeueAt != 0 || w.Status.Evictions != 0 {
		t.Fatalf("stale lifecycle not reset: %+v", w.Status)
	}
	if c, ok := w.Status.Condition("Finished"); ok && c.Status == "True" {
		t.Fatalf("stale Finished condition: %+v", c)
	}
}

func TestHoldAndDrain(t *testing.T) {
	e := scenario.Basic()
	e.Tick()
	cq, ok := e.Store.GetClusterQueue("basic")
	if !ok {
		t.Fatal("cq missing")
	}
	cq.Spec.StopPolicy = api.StopHoldAndDrain
	if err := e.Store.UpsertClusterQueue(cq); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if got := get(t, e, "default", "job-a").Status.State; got != api.StatePending {
		t.Fatalf("job-a state = %s", got)
	}
	if got := e.Cache.Len(); got != 0 {
		t.Fatalf("cache len = %d", got)
	}
}

func TestHoldKeepsAdmittedWorkloads(t *testing.T) {
	e := scenario.Basic()
	e.Tick()
	cq, _ := e.Store.GetClusterQueue("basic")
	cq.Spec.StopPolicy = api.StopHold
	if err := e.Store.UpsertClusterQueue(cq); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if got := get(t, e, "default", "job-a").Status.State; got != api.StateAdmitted {
		t.Fatalf("job-a state = %s", got)
	}
	if got := get(t, e, "default", "job-c").Status.State; got != api.StatePending {
		t.Fatalf("job-c state = %s", got)
	}
}

func TestDeactivateEvicts(t *testing.T) {
	e := scenario.Basic()
	e.Tick()
	if err := e.Deactivate("default", "job-a"); err != nil {
		t.Fatal(err)
	}
	if got := e.Cache.Has("default/job-a"); got {
		t.Fatal("job-a still reserves quota")
	}
	e.Tick()
	w := get(t, e, "default", "job-a")
	if w.Status.State != api.StatePending || w.Spec.Active {
		t.Fatalf("unexpected state: %+v", w)
	}
}

func TestMaximumExecutionTime(t *testing.T) {
	e := scenario.New()
	scenario.MustCQ(e, scenario.SimpleCQ("cq", "default", api.RL("cpu", 2000)))
	scenario.MustLQ(e, "default", "q", "cq")
	scenario.MustNamespace(e, "default", nil)
	w := scenario.Workload("default", "short", "q", 0, 1, api.RL("cpu", 1000))
	w.Spec.MaximumExecutionSeconds = 2
	scenario.MustSubmit(e, w)
	e.Tick()
	e.Tick()
	e.Tick()
	got := get(t, e, "default", "short")
	if got.Spec.Active {
		t.Fatal("workload should be deactivated after maximum execution time")
	}
}

func TestMaximumExecutionTimeIsCumulativeAcrossPreemption(t *testing.T) {
	e := scenario.New()
	cq := scenario.SimpleCQ("cq", "default", api.RL("cpu", 2000))
	cq.Spec.Preemption.WithinClusterQueue = api.PreemptLower
	scenario.MustCQ(e, cq)
	scenario.MustLQ(e, "default", "q", "cq")
	scenario.MustNamespace(e, "default", nil)

	low := scenario.Workload("default", "low", "q", 0, 1, api.RL("cpu", 2000))
	low.Spec.MaximumExecutionSeconds = 2
	low.Spec.CreationTimestamp = 1
	scenario.MustSubmit(e, low)
	e.Tick() // clock 1: low admitted

	high := scenario.Workload("default", "high", "q", 10, 1, api.RL("cpu", 1000))
	high.Spec.CreationTimestamp = 2
	scenario.MustSubmit(e, high)
	e.Tick() // clock 2: high preempts low; low has executed 1 tick
	if got := get(t, e, "default", "low").Status.ExecutionTicks; got != 1 {
		t.Fatalf("low execution ticks after preemption = %d, want 1", got)
	}
	if err := e.Finish("default", "high"); err != nil {
		t.Fatal(err)
	}
	e.Tick() // clock 3: low still in requeue backoff
	e.Tick() // clock 4: low re-admitted; cumulative clock continues
	if got := get(t, e, "default", "low").Status.State; got != api.StateAdmitted {
		t.Fatalf("low state after requeue = %s", got)
	}
	e.Tick() // clock 5: 1 earlier tick + 1 current tick reaches the limit
	low = get(t, e, "default", "low")
	if low.Spec.Active {
		t.Fatalf("cumulative maximum execution time not enforced: %+v", low.Status)
	}
}

func TestNamespaceSelector(t *testing.T) {
	e := scenario.New()
	cq := scenario.SimpleCQ("cq", "default", api.RL("cpu", 2000))
	cq.Spec.NamespaceSelector = &api.LabelSelector{MatchLabels: map[string]string{"team": "blue"}}
	scenario.MustCQ(e, cq)
	scenario.MustLQ(e, "blue", "q", "cq")
	scenario.MustLQ(e, "red", "q", "cq")
	scenario.MustNamespace(e, "blue", map[string]string{"team": "blue"})
	scenario.MustNamespace(e, "red", map[string]string{"team": "red"})
	scenario.MustSubmit(e, scenario.Workload("blue", "ok", "q", 0, 1, api.RL("cpu", 1000)))
	scenario.MustSubmit(e, scenario.Workload("red", "blocked", "q", 0, 1, api.RL("cpu", 1000)))
	e.Tick()
	if got := get(t, e, "blue", "ok").Status.State; got != api.StateAdmitted {
		t.Fatalf("blue state = %s", got)
	}
	red := get(t, e, "red", "blocked")
	if red.Status.State != api.StatePending {
		t.Fatalf("red state = %s", red.Status.State)
	}
	if c, ok := red.Status.Condition("Inadmissible"); !ok || c.Reason != "NamespaceSelector" {
		t.Fatalf("unexpected conditions: %+v", red.Status.Conditions)
	}
}

func TestSetBasedNamespaceSelector(t *testing.T) {
	e := scenario.LabelSelector()
	e.Tick()
	if got := get(t, e, "blue", "blue-ok").Status.State; got != api.StateAdmitted {
		t.Fatalf("blue state = %s", got)
	}
	for _, key := range []string{"red/red-blocked", "green/green-blocked"} {
		ns, name := splitKey(key)
		w := get(t, e, ns, name)
		if w.Status.State != api.StatePending {
			t.Fatalf("%s state = %s", key, w.Status.State)
		}
		if c, ok := w.Status.Condition("Inadmissible"); !ok || c.Reason != "NamespaceSelector" {
			t.Fatalf("%s: expected Inadmissible/NamespaceSelector, got %+v", key, w.Status.Conditions)
		}
	}
}

func TestStrictFIFOInadmissibleHeadDoesNotBlockQueue(t *testing.T) {
	e := scenario.New()
	cq := scenario.SimpleCQ("cq", "default", api.RL("cpu", 2000))
	cq.Spec.QueueingStrategy = api.StrictFIFO
	cq.Spec.NamespaceSelector = &api.LabelSelector{MatchLabels: map[string]string{"team": "blue"}}
	scenario.MustCQ(e, cq)
	scenario.MustLQ(e, "red", "q", "cq")
	scenario.MustLQ(e, "blue", "q", "cq")
	scenario.MustNamespace(e, "red", map[string]string{"team": "red"})
	scenario.MustNamespace(e, "blue", map[string]string{"team": "blue"})

	red := scenario.Workload("red", "inadmissible", "q", 0, 1, api.RL("cpu", 1000))
	red.Spec.CreationTimestamp = 1
	scenario.MustSubmit(e, red)
	blue := scenario.Workload("blue", "valid", "q", 0, 1, api.RL("cpu", 1000))
	blue.Spec.CreationTimestamp = 2
	scenario.MustSubmit(e, blue)

	e.Tick()
	if got := get(t, e, "red", "inadmissible").Status.State; got != api.StatePending {
		t.Fatalf("red state = %s", got)
	}
	if got := get(t, e, "blue", "valid").Status.State; got != api.StateAdmitted {
		t.Fatalf("blue state = %s; an inadmissible workload must not block StrictFIFO", got)
	}
}

func TestLocalQueueHoldOnlyBlocksItsOwnQueue(t *testing.T) {
	e := scenario.LocalQueueHold()
	e.Tick()
	if got := get(t, e, "default", "open-job").Status.State; got != api.StateAdmitted {
		t.Fatalf("open-job state = %s", got)
	}
	if got := get(t, e, "default", "held-job").Status.State; got != api.StatePending {
		t.Fatalf("held-job state = %s", got)
	}
	if err := e.SetLocalQueueStopPolicy("default", "held", api.StopNone); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if got := get(t, e, "default", "held-job").Status.State; got != api.StateAdmitted {
		t.Fatalf("held-job after release = %s", got)
	}
}

func TestAdmissionCheckRetryHonorsRequeueDelay(t *testing.T) {
	e := scenario.RetryBackoff()
	e.Tick() // tick 1: reserve, check pending
	if got := get(t, e, "default", "retry-job").Status.State; got != api.StateQuotaReserved {
		t.Fatalf("state before retry = %s", got)
	}
	if err := e.ApproveCheckRetry("default", "retry-job", "capacity-check", "no capacity", 2); err != nil {
		t.Fatal(err)
	}
	before := get(t, e, "default", "retry-job")
	if cs, ok := before.Status.GetAdmissionCheck("capacity-check"); !ok ||
		cs.State != "Retry" || cs.RetryCount != 1 || cs.RequeueAfterSeconds != 2 {
		t.Fatalf("retry bookkeeping = %+v", before.Status.AdmissionChecks)
	}
	e.Tick() // tick 2: retry releases quota, requeue at 3
	w := get(t, e, "default", "retry-job")
	if w.Status.State != api.StatePending {
		t.Fatalf("state after retry = %s", w.Status.State)
	}
	if w.Status.RequeueAt != 3 {
		t.Fatalf("requeueAt = %d, want 3", w.Status.RequeueAt)
	}
	e.Tick() // tick 3: eligible again, reserve
	if got := get(t, e, "default", "retry-job").Status.State; got != api.StateQuotaReserved {
		t.Fatalf("state after backoff = %s", got)
	}
}

func TestRetryCountPersistsAcrossReservations(t *testing.T) {
	e := scenario.RetryBackoff()
	e.Tick() // tick 1: reserve, check pending
	if err := e.ApproveCheckRetry("default", "retry-job", "capacity-check", "no capacity", 1); err != nil {
		t.Fatal(err)
	}
	e.Tick() // tick 2: release, then re-reserve because the backoff has elapsed
	w := get(t, e, "default", "retry-job")
	if w.Status.State != api.StateQuotaReserved {
		t.Fatalf("state after retry = %s", w.Status.State)
	}
	cs, ok := w.Status.GetAdmissionCheck("capacity-check")
	if !ok {
		t.Fatalf("missing check state: %+v", w.Status.AdmissionChecks)
	}
	if cs.State != "Pending" || cs.RetryCount != 1 {
		t.Fatalf("retry bookkeeping after re-reservation = %+v, want Pending/retryCount=1", cs)
	}
	// A second Retry must increment, not restart, the counter.
	if err := e.ApproveCheckRetry("default", "retry-job", "capacity-check", "still no capacity", 1); err != nil {
		t.Fatal(err)
	}
	w = get(t, e, "default", "retry-job")
	cs, _ = w.Status.GetAdmissionCheck("capacity-check")
	if cs.RetryCount != 2 {
		t.Fatalf("retryCount = %d, want 2", cs.RetryCount)
	}
}

func TestWaitForPodsReadyMaxRetriesCountsOnlyTimeouts(t *testing.T) {
	e := scenario.New()
	e.WaitForPodsReady = &controller.WaitForPodsReadyConfig{Timeout: 1, MaxRetries: 1}
	scenario.MustCQ(e, scenario.SimpleCQ("cq", "default", api.RL("cpu", 2000)))
	scenario.MustLQ(e, "default", "q", "cq")
	scenario.MustNamespace(e, "default", nil)
	scenario.MustSubmit(e, scenario.Workload("default", "job", "q", 0, 1, api.RL("cpu", 1000)))

	e.Tick() // tick 1: admitted
	e.Tick() // tick 2: first timeout, requeued
	w := get(t, e, "default", "job")
	if !w.Spec.Active {
		t.Fatal("first timeout must requeue, not deactivate")
	}
	e.Tick() // tick 3: backoff
	e.Tick() // tick 4: re-admitted
	w = get(t, e, "default", "job")
	if w.Status.State != api.StateAdmitted {
		t.Fatalf("state after requeue = %s", w.Status.State)
	}
	e.Tick() // tick 5: second timeout exceeds MaxRetries=1
	w = get(t, e, "default", "job")
	if w.Spec.Active {
		t.Fatal("workload should be deactivated after MaxRetries readiness timeouts")
	}
	if got := evictionStatCount(&w.Status, "Deactivated", "RequeuingLimitExceeded"); got != 1 {
		t.Fatalf("deactivation stat = %d, want 1; stats=%+v", got, w.Status.EvictionStats)
	}
	if got := e.MetricsSnapshot().PodsReadyTimeouts; got != 2 {
		t.Fatalf("pods ready timeouts = %d, want 2", got)
	}
}

func TestTASReclaimReleasesTopologyUsage(t *testing.T) {
	e := scenario.TASSlice()
	e.Tick()
	before := e.Cache.TopologyUsage("gpu-topology", "hostname", nil)
	if got := before["rack-b/h4"].Get("nvidia.com/gpu"); got != 2 {
		t.Fatalf("h4 gpu before reclaim = %d, want 2", got)
	}
	if err := e.ReportReclaimable("default", "slice-four", "main", 2); err != nil {
		t.Fatal(err)
	}
	after := e.Cache.TopologyUsage("gpu-topology", "hostname", nil)
	if got := after["rack-b/h4"].Get("nvidia.com/gpu"); got != 0 {
		t.Fatalf("h4 gpu after reclaim = %d, want 0", got)
	}
	if got := after["rack-b/h4"].Get(api.Pods); got != 0 {
		t.Fatalf("h4 pods after reclaim = %d, want 0", got)
	}
	w := get(t, e, "default", "slice-four")
	if len(w.Status.Admission.TopologyAssignments) != 1 {
		t.Fatalf("topology assignments = %d, want 1", len(w.Status.Admission.TopologyAssignments))
	}
	if got := w.Status.Admission.TopologyAssignments[0].PodCount; got != 2 {
		t.Fatalf("remaining pod count = %d, want 2", got)
	}
	if got := e.Cache.FlavorUsage("tas-slice", "tas-flavor", "nvidia.com/gpu"); got != 5 {
		t.Fatalf("gpu quota usage after reclaim = %d, want 5", got)
	}
}

func TestInadmissibleClearedWhenSelectorMatches(t *testing.T) {
	e := scenario.New()
	cq := scenario.SimpleCQ("cq", "default", api.RL("cpu", 0))
	cq.Spec.NamespaceSelector = &api.LabelSelector{MatchLabels: map[string]string{"team": "blue"}}
	scenario.MustCQ(e, cq)
	scenario.MustLQ(e, "blue", "q", "cq")
	scenario.MustNamespace(e, "blue", map[string]string{"team": "red"})
	scenario.MustSubmit(e, scenario.Workload("blue", "job", "q", 0, 1, api.RL("cpu", 1000)))
	e.Tick()
	w := get(t, e, "blue", "job")
	if c, ok := w.Status.Condition("Inadmissible"); !ok || c.Status != "True" {
		t.Fatalf("expected Inadmissible before fixing labels: %+v", w.Status.Conditions)
	}
	// Fix the namespace label. The workload is now structurally admissible but
	// still cannot fit the zero-quota queue; the stale Inadmissible condition
	// must be replaced by the quota reason.
	scenario.MustNamespace(e, "blue", map[string]string{"team": "blue"})
	e.Tick()
	w = get(t, e, "blue", "job")
	if c, ok := w.Status.Condition("Inadmissible"); ok && c.Status == "True" {
		t.Fatalf("stale Inadmissible condition: %+v", c)
	}
	if c, ok := w.Status.Condition("QuotaReserved"); !ok || c.Reason != "InsufficientQuota" {
		t.Fatalf("expected InsufficientQuota reason, got %+v", w.Status.Conditions)
	}
}

func evictionStatCount(status *api.WorkloadStatus, reason, cause string) int32 {
	var count int32
	for _, stat := range status.EvictionStats {
		if stat.Reason == reason && stat.UnderlyingCause == cause {
			count += stat.Count
		}
	}
	return count
}

func TestSchedulingStatsRecordsPreemption(t *testing.T) {
	e := scenario.Preemption()
	e.Tick()
	low := get(t, e, "default", "low")
	if len(low.Status.EvictionStats) != 1 {
		t.Fatalf("eviction stats = %+v", low.Status.EvictionStats)
	}
	stat := low.Status.EvictionStats[0]
	if stat.Reason != "Preempted" || stat.Count != 1 || stat.UnderlyingCause != "" {
		t.Fatalf("unexpected eviction stat: %+v", stat)
	}
}

func TestQueueStatusVisibility(t *testing.T) {
	e := scenario.Basic()
	e.Tick()
	status, ok := e.QueueStatus("basic")
	if !ok {
		t.Fatal("cluster queue not found")
	}
	if len(status.Admitted) != 2 || len(status.Pending) != 1 {
		t.Fatalf("admitted=%v pending=%v", status.Admitted, status.Pending)
	}
	if status.Pending[0] != "default/job-c" {
		t.Fatalf("pending = %v", status.Pending)
	}
	if status.PendingByReason["InsufficientQuota"] != 1 {
		t.Fatalf("pending reasons = %v", status.PendingByReason)
	}
	if status.Usage.Get(api.CPU) != 2000 || status.Nominal.Get(api.CPU) != 2000 {
		t.Fatalf("usage=%v nominal=%v", status.Usage, status.Nominal)
	}
}

func splitKey(key string) (string, string) {
	for i := 0; i < len(key); i++ {
		if key[i] == '/' {
			return key[:i], key[i+1:]
		}
	}
	return key, ""
}

func TestMetrics(t *testing.T) {
	e := scenario.Basic()
	e.Tick()
	m := e.MetricsSnapshot()
	if m.AdmittedTotal != 2 || m.Pending != 1 {
		t.Fatalf("metrics after tick 1: %+v", m)
	}
	if err := e.Finish("default", "job-a"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	m = e.MetricsSnapshot()
	if m.AdmittedTotal != 3 || m.FinishedTotal != 1 {
		t.Fatalf("metrics after finish: %+v", m)
	}
	if got := e.Prometheus(); len(got) == 0 {
		t.Fatal("empty prometheus output")
	}
}

func TestWaitForPodsReadyEvictsStuckWorkload(t *testing.T) {
	e := scenario.PodsReady()
	e.Tick() // tick 1: both admitted
	if err := e.MarkPodsReady("default", "ready-job"); err != nil {
		t.Fatal(err)
	}
	e.Tick() // 2
	e.Tick() // 3
	e.Tick() // 4: stuck-job has missed the 3-tick deadline
	stuck := get(t, e, "default", "stuck-job")
	if stuck.Status.State != api.StatePending {
		t.Fatalf("stuck-job state = %s", stuck.Status.State)
	}
	if c, ok := stuck.Status.Condition("Evicted"); !ok || c.Reason != "PodsReadyTimeout" {
		t.Fatalf("unexpected conditions: %+v", stuck.Status.Conditions)
	}
	ready := get(t, e, "default", "ready-job")
	if ready.Status.State != api.StateAdmitted || !ready.Status.PodsReady {
		t.Fatalf("ready-job should still be admitted and ready: %+v", ready.Status)
	}
	if got := e.MetricsSnapshot().PodsReadyTimeouts; got != 1 {
		t.Fatalf("pods ready timeouts = %d", got)
	}
}

func TestDynamicReclaimReleasesQuota(t *testing.T) {
	e := scenario.DynamicReclaim()
	e.Tick()
	if got := get(t, e, "default", "waiting-job").Status.State; got != api.StatePending {
		t.Fatalf("waiting-job state = %s", got)
	}
	if err := e.ReportReclaimable("default", "elastic-job", "main", 1); err != nil {
		t.Fatal(err)
	}
	if got := e.Cache.FlavorUsage("reclaimable", "default", api.CPU); got != 2000 {
		t.Fatalf("cache cpu after reclaim = %d, want 2000", got)
	}
	e.Tick()
	if got := get(t, e, "default", "waiting-job").Status.State; got != api.StateAdmitted {
		t.Fatalf("waiting-job state after reclaim = %s", got)
	}
	if got := e.MetricsSnapshot().ReclaimedPodsTotal; got != 1 {
		t.Fatalf("reclaimed total = %d", got)
	}
	// Reclaiming is one-way and cannot exceed the original count.
	if err := e.ReportReclaimable("default", "elastic-job", "main", 2); err == nil {
		t.Fatal("expected excessive reclaim to fail")
	}
}

func TestReclaimReleasesPodsQuota(t *testing.T) {
	e := scenario.New()
	scenario.MustCQ(e, scenario.SimpleCQ("pods-cq", "default", api.RL("cpu", 8000, api.Pods, 10)))
	scenario.MustLQ(e, "default", "q", "pods-cq")
	scenario.MustNamespace(e, "default", nil)
	scenario.MustSubmit(e, scenario.Workload("default", "job", "q", 0, 10, api.RL("cpu", 100)))
	e.Tick()

	if got := e.Cache.FlavorUsage("pods-cq", "default", api.Pods); got != 10 {
		t.Fatalf("pods before reclaim = %d, want 10", got)
	}
	if err := e.ReportReclaimable("default", "job", "main", 5); err != nil {
		t.Fatal(err)
	}
	if got := e.Cache.FlavorUsage("pods-cq", "default", api.Pods); got != 5 {
		t.Fatalf("pods after reclaim = %d, want 5", got)
	}
	if got := e.Cache.FlavorUsage("pods-cq", "default", api.CPU); got != 500 {
		t.Fatalf("cpu after reclaim = %d, want 500", got)
	}
	w, _ := e.Store.GetWorkload("default", "job")
	if got := w.Status.Admission.Usage.Get(api.Pods); got != 5 {
		t.Fatalf("admission pods = %d, want 5", got)
	}
}

func TestReclaimFinishedWorkloadRejected(t *testing.T) {
	e := scenario.DynamicReclaim()
	e.Tick()
	if err := e.Finish("default", "elastic-job"); err != nil {
		t.Fatal(err)
	}
	if err := e.ReportReclaimable("default", "elastic-job", "main", 1); err == nil {
		t.Fatal("expected reclaim of a finished workload to fail")
	}
}

func TestDynamicQuotaShrinksEffectiveNominal(t *testing.T) {
	e := scenario.DynamicQuota()
	e.Tick()
	if got := get(t, e, "team-a", "too-big").Status.State; got != api.StatePending {
		t.Fatalf("too-big state = %s", got)
	}
	if got := get(t, e, "team-b", "fits").Status.State; got != api.StateAdmitted {
		t.Fatalf("fits state = %s", got)
	}
	if got := e.Cache.FlavorUsage("team-b", "default", api.CPU); got != 3000 {
		t.Fatalf("team-b usage = %d", got)
	}
}

func TestDynamicQuotaMissingResourceIsZero(t *testing.T) {
	e := scenario.New()
	scenario.MustProvider(e, &api.CapacityProvider{
		Name: "pool",
		Capacity: map[string]api.ResourceList{
			"default": api.RL("cpu", 8000), // memory is deliberately not reported
		},
	})
	cq := scenario.SimpleCQ("cq", "default", api.RL("cpu", 4000, "memory", 8192))
	cq.Spec.CapacityProvider = "pool"
	scenario.MustCQ(e, cq)
	scenario.MustLQ(e, "default", "q", "cq")
	scenario.MustNamespace(e, "default", nil)
	// CPU alone would fit (4000 of 8000), but effective memory is zero because
	// the provider did not report it.
	scenario.MustSubmit(e, scenario.Workload("default", "job", "q", 0, 1, api.RL("cpu", 1000, "memory", 1024)))
	e.Tick()
	if got := get(t, e, "default", "job").Status.State; got != api.StatePending {
		t.Fatalf("job state = %s; unreported memory must be zero effective quota", got)
	}
}

func TestTopologyScenarioAdmitsTwoRacks(t *testing.T) {
	e, _, err := scenario.Run("tas")
	if err != nil {
		t.Fatal(err)
	}
	e.Tick()
	for _, name := range []string{"job-a", "job-b"} {
		if got := get(t, e, "default", name).Status.State; got != api.StateAdmitted {
			t.Fatalf("%s state = %s", name, got)
		}
	}
	if got := get(t, e, "default", "job-c").Status.State; got != api.StatePending {
		t.Fatalf("job-c state = %s", got)
	}
	domains := map[string]bool{}
	for _, name := range []string{"job-a", "job-b"} {
		w := get(t, e, "default", name)
		for _, ta := range w.Status.Admission.TopologyAssignments {
			domains[ta.Domain] = true
		}
	}
	if !domains["rack-a"] || !domains["rack-b"] {
		t.Fatalf("expected one job per rack, got %v", domains)
	}
}

func TestDRADeviceQuotaAccounting(t *testing.T) {
	e := scenario.DRA()
	e.Tick()
	if got := get(t, e, "default", "dra-job-a").Status.State; got != api.StateAdmitted {
		t.Fatalf("dra-job-a state = %s", got)
	}
	if got := get(t, e, "default", "dra-job-b").Status.State; got != api.StatePending {
		t.Fatalf("dra-job-b state = %s", got)
	}
	if got := e.Cache.FlavorUsage("dra", "default", "nvidia.com/gpu"); got != 4 {
		t.Fatalf("gpu usage = %d, want 4", got)
	}
}

func TestDRADeviceReclaimReleasesDeviceQuota(t *testing.T) {
	e := scenario.DRA()
	e.Tick()
	if err := e.ReportReclaimable("default", "dra-job-a", "main", 2); err != nil {
		t.Fatal(err)
	}
	if got := e.Cache.FlavorUsage("dra", "default", "nvidia.com/gpu"); got != 2 {
		t.Fatalf("gpu usage after reclaim = %d, want 2", got)
	}
	if got := e.Cache.FlavorUsage("dra", "default", api.CPU); got != 2000 {
		t.Fatalf("cpu usage after reclaim = %d, want 2000", got)
	}
}

func TestAdmissionGateBlocksAndClears(t *testing.T) {
	e := scenario.AdmissionGate()
	e.Tick()
	w := get(t, e, "default", "gated-job")
	if w.Status.State != api.StatePending {
		t.Fatalf("gated job state = %s", w.Status.State)
	}
	if c, ok := w.Status.Condition("AdmissionGated"); !ok || c.Status != "True" {
		t.Fatalf("expected AdmissionGated condition, got %+v", w.Status.Conditions)
	}
	for _, gate := range []string{"security-scan", "license-check"} {
		if err := e.ClearAdmissionGate("default", "gated-job", gate); err != nil {
			t.Fatal(err)
		}
	}
	e.Tick()
	if got := get(t, e, "default", "gated-job").Status.State; got != api.StateAdmitted {
		t.Fatalf("gated job state after clearing = %s", got)
	}
}

func TestAdmissionFairSharingOrdersByHistory(t *testing.T) {
	e := scenario.AdmissionFairSharing()
	beforeLight := e.AFSScore("default", "light")
	beforeHeavy := e.AFSScore("default", "heavy")
	if beforeLight >= beforeHeavy {
		t.Fatalf("expected light history below heavy: light=%v heavy=%v", beforeLight, beforeHeavy)
	}
	e.Tick()
	if got := get(t, e, "default", "light-next").Status.State; got != api.StateAdmitted {
		t.Fatalf("light-next state = %s", got)
	}
	if got := get(t, e, "default", "heavy-next").Status.State; got != api.StatePending {
		t.Fatalf("heavy-next state = %s", got)
	}
}

func TestConcurrentAdmissionMigratesToPreferredFlavor(t *testing.T) {
	e := scenario.ConcurrentAdmission()
	e.Tick()
	w := get(t, e, "default", "migrating-job")
	if w.Status.Admission == nil || w.Status.Admission.FlavorAssignments[0].Flavor != "spot" {
		t.Fatalf("expected initial spot admission: %+v", w.Status.Admission)
	}
	cq, ok := e.Store.GetClusterQueue("concurrent")
	if !ok {
		t.Fatal("cluster queue missing")
	}
	cq.Spec.ResourceGroups[0].Flavors[0].Nominal["cpu"] = 2000
	if err := e.Store.UpsertClusterQueue(cq); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	w = get(t, e, "default", "migrating-job")
	if w.Status.Admission == nil || w.Status.Admission.FlavorAssignments[0].Flavor != "reserved" {
		t.Fatalf("expected migration to reserved: %+v", w.Status.Admission)
	}
}

func TestConcurrentMigrationPreservesChecksAndReadiness(t *testing.T) {
	e := scenario.New()
	scenario.MustCheck(e, "manual", "manual")
	cq := &api.ClusterQueue{
		Name: "concurrent",
		Spec: api.ClusterQueueSpec{
			NamespaceSelector:   &api.LabelSelector{},
			QueueingStrategy:    api.BestEffortFIFO,
			AdmissionChecks:     []string{"manual"},
			ConcurrentAdmission: &api.ConcurrentAdmissionPolicy{},
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU},
				Flavors: []api.FlavorQuota{
					{Name: "reserved", Nominal: api.RL("cpu", 0)},
					{Name: "spot", Nominal: api.RL("cpu", 4000)},
				},
			}},
		},
	}
	scenario.MustCQ(e, cq)
	scenario.MustLQ(e, "default", "q", "concurrent")
	scenario.MustNamespace(e, "default", nil)
	scenario.MustSubmit(e, scenario.Workload("default", "job", "q", 0, 1, api.RL("cpu", 2000)))
	e.WaitForPodsReady = &controller.WaitForPodsReadyConfig{Timeout: 3}

	e.Tick()
	if err := e.ApproveCheck("default", "job", "manual", "Ready", "ok"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if err := e.MarkPodsReady("default", "job"); err != nil {
		t.Fatal(err)
	}

	updated, _ := e.Store.GetClusterQueue("concurrent")
	updated.Spec.ResourceGroups[0].Flavors[0].Nominal["cpu"] = 2000
	if err := e.Store.UpsertClusterQueue(updated); err != nil {
		t.Fatal(err)
	}
	e.Tick()

	w := get(t, e, "default", "job")
	if w.Status.State != api.StateAdmitted {
		t.Fatalf("state after migration = %s, want Admitted", w.Status.State)
	}
	if w.Status.Admission == nil || w.Status.Admission.FlavorAssignments[0].Flavor != "reserved" {
		t.Fatalf("expected reserved flavor after migration: %+v", w.Status.Admission)
	}
	if !w.Status.PodsReady {
		t.Fatal("PodsReady should survive flavor migration")
	}
	if c, ok := w.Status.Condition("PodsReady"); !ok || c.Status != "True" {
		t.Fatalf("PodsReady condition = %+v", w.Status.Conditions)
	}
	// The migration must not restart the readiness clock in a way that evicts
	// a workload whose pods are still running.
	e.Tick()
	e.Tick()
	if got := get(t, e, "default", "job").Status.State; got != api.StateAdmitted {
		t.Fatalf("workload was evicted after migration: %s", got)
	}
}

func TestRetryCountSurvivesPreemption(t *testing.T) {
	e := scenario.New()
	scenario.MustCheck(e, "manual", "manual")
	cq := scenario.SimpleCQ("preempt", "default", api.RL("cpu", 2000))
	cq.Spec.AdmissionChecks = []string{"manual"}
	cq.Spec.Preemption.WithinClusterQueue = api.PreemptLower
	scenario.MustCQ(e, cq)
	scenario.MustLQ(e, "default", "q", "preempt")
	scenario.MustNamespace(e, "default", nil)

	low := scenario.Workload("default", "low", "q", 0, 1, api.RL("cpu", 2000))
	low.Spec.CreationTimestamp = 1
	scenario.MustSubmit(e, low)
	e.Tick()
	if err := e.ApproveCheckRetry("default", "low", "manual", "retry", 1); err != nil {
		t.Fatal(err)
	}
	e.Tick() // release + re-reserve, carrying retryCount
	w := get(t, e, "default", "low")
	if w.Status.State != api.StateQuotaReserved {
		t.Fatalf("state after retry = %s", w.Status.State)
	}
	check, _ := w.Status.GetAdmissionCheck("manual")
	if check.RetryCount != 1 {
		t.Fatalf("retryCount = %d, want 1", check.RetryCount)
	}
	if err := e.ApproveCheck("default", "low", "manual", "Ready", "ok"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if got := get(t, e, "default", "low").Status.State; got != api.StateAdmitted {
		t.Fatalf("state = %s, want Admitted", got)
	}

	high := scenario.Workload("default", "high", "q", 10, 1, api.RL("cpu", 2000))
	high.Spec.CreationTimestamp = 2
	scenario.MustSubmit(e, high)
	e.Tick()

	lowNow := get(t, e, "default", "low")
	if lowNow.Status.State != api.StatePending {
		t.Fatalf("low state = %s, want Pending after preemption", lowNow.Status.State)
	}
	check, ok := lowNow.Status.GetAdmissionCheck("manual")
	if !ok {
		t.Fatal("check list was cleared by eviction")
	}
	if check.RetryCount != 1 {
		t.Fatalf("retryCount after preemption = %d, want 1", check.RetryCount)
	}
}

func TestConcurrentMigrationRespectsQueueHold(t *testing.T) {
	e := scenario.ConcurrentAdmission()
	e.Tick()
	cq, ok := e.Store.GetClusterQueue("concurrent")
	if !ok {
		t.Fatal("cluster queue missing")
	}
	cq.Spec.ResourceGroups[0].Flavors[0].Nominal["cpu"] = 2000
	if err := e.Store.UpsertClusterQueue(cq); err != nil {
		t.Fatal(err)
	}
	if err := e.SetLocalQueueStopPolicy("default", "q", api.StopHold); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	w := get(t, e, "default", "migrating-job")
	if w.Status.Admission == nil || w.Status.Admission.FlavorAssignments[0].Flavor != "spot" {
		t.Fatalf("migration must not run while the LocalQueue is held: %+v", w.Status.Admission)
	}
	if err := e.SetLocalQueueStopPolicy("default", "q", api.StopNone); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	w = get(t, e, "default", "migrating-job")
	if w.Status.Admission == nil || w.Status.Admission.FlavorAssignments[0].Flavor != "reserved" {
		t.Fatalf("expected migration after releasing the hold: %+v", w.Status.Admission)
	}
}

func TestHierarchicalLimitBlocksExcessBorrowing(t *testing.T) {
	e := scenario.HierarchicalLimits()
	e.Tick()
	if got := get(t, e, "team-a", "within-limit").Status.State; got != api.StateAdmitted {
		t.Fatalf("within-limit state = %s", got)
	}
	if got := get(t, e, "team-a", "over-limit").Status.State; got != api.StatePending {
		t.Fatalf("over-limit state = %s", got)
	}
}

func TestTASSliceScenario(t *testing.T) {
	e := scenario.TASSlice()
	e.Tick()
	w := get(t, e, "default", "slice-four")
	if w.Status.State != api.StateAdmitted {
		t.Fatalf("slice-four state = %s", w.Status.State)
	}
	if got := len(w.Status.Admission.TopologyAssignments); got != 2 {
		t.Fatalf("slice assignments = %d, want 2", got)
	}
	spread := get(t, e, "default", "spread-two")
	if got := spread.Status.Admission.TopologyAssignments[0].Domain; got != "rack-a/h2" {
		t.Fatalf("spread domain = %q, want rack-a/h2", got)
	}
}

func TestCohortBorrowRelease(t *testing.T) {
	e := scenario.CohortBorrow()
	e.Tick()
	if got := get(t, e, "team-b", "b-normal").Status.State; got != api.StatePending {
		t.Fatalf("b-normal state = %s", got)
	}
	if err := e.Finish("team-a", "a-large"); err != nil {
		t.Fatal(err)
	}
	e.Tick()
	if got := get(t, e, "team-b", "b-normal").Status.State; got != api.StateAdmitted {
		t.Fatalf("b-normal state after release = %s", got)
	}
}
