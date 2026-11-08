package controller_test

import (
	"testing"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/scenario"
)

func TestEngineExplainPendingWorkload(t *testing.T) {
	e := scenario.Basic()
	e.Tick()

	explanation, ok := e.Explain("default", "job-c")
	if !ok {
		t.Fatal("expected workload to exist")
	}
	if explanation.Reason != "InsufficientQuota" {
		t.Fatalf("unexpected reason: %s", explanation.Reason)
	}
	if explanation.DirectFeasible || explanation.QuotaFeasible {
		t.Fatalf("job-c must be infeasible: %+v", explanation)
	}
	if explanation.ClusterQueue != "basic" || explanation.Queue != "q" {
		t.Fatalf("unexpected queue mapping: %+v", explanation)
	}
	if explanation.Requested.Get(api.CPU) != 1000 {
		t.Fatalf("unexpected requested usage: %+v", explanation.Requested)
	}
	if len(explanation.Groups) != 1 || explanation.Groups[0].Feasible {
		t.Fatalf("unexpected groups: %+v", explanation.Groups)
	}
}

func TestEngineExplainAdmittedWorkload(t *testing.T) {
	e := scenario.Basic()
	e.Tick()

	explanation, ok := e.Explain("default", "job-a")
	if !ok {
		t.Fatal("expected workload to exist")
	}
	if explanation.Reason != "Admitted" || explanation.State != api.StateAdmitted {
		t.Fatalf("unexpected explanation: %+v", explanation)
	}
	if !explanation.DirectFeasible {
		t.Fatalf("admitted workload should still fit when excluding itself: %+v", explanation)
	}
}

func TestEngineExplainControllerBlockers(t *testing.T) {
	e := scenario.AdmissionGate()
	e.Tick()
	explanation, ok := e.Explain("default", "gated-job")
	if !ok {
		t.Fatal("expected workload to exist")
	}
	if explanation.Reason != "AdmissionGated" {
		t.Fatalf("expected AdmissionGated, got %+v", explanation)
	}

	e = scenario.LocalQueueHold()
	e.Tick()
	explanation, ok = e.Explain("default", "held-job")
	if !ok {
		t.Fatal("expected workload to exist")
	}
	if explanation.Reason != "LocalQueueStopped" {
		t.Fatalf("expected LocalQueueStopped, got %+v", explanation)
	}
}

func TestEngineExplainMissingWorkload(t *testing.T) {
	e := scenario.Basic()
	if _, ok := e.Explain("default", "missing"); ok {
		t.Fatal("expected missing workload to return false")
	}
}
