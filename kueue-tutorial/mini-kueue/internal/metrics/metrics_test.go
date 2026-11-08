package metrics

import (
	"strings"
	"testing"
)

func TestCountersAndExposition(t *testing.T) {
	m := &Metrics{}
	m.IncCycle()
	m.IncAdmitted()
	m.IncFinished()
	m.IncEvicted(true)
	m.IncQuotaReserved()
	m.IncAdmissionCheckWait()
	m.IncPodsReadyTimeout()
	m.IncReclaimed(3)
	m.SetGauges(2, 1, 3, 4)
	s := m.Snapshot()
	if s.Cycles != 1 || s.AdmittedTotal != 1 || s.FinishedTotal != 1 || s.EvictedTotal != 1 ||
		s.PreemptedTotal != 1 || s.QuotaReservedTotal != 1 || s.AdmissionCheckWaits != 1 ||
		s.PodsReadyTimeouts != 1 || s.ReclaimedPodsTotal != 3 {
		t.Fatalf("unexpected snapshot: %+v", s)
	}
	if s.Pending != 2 || s.Reserved != 1 || s.Admitted != 3 || s.Finished != 4 {
		t.Fatalf("unexpected gauges: %+v", s)
	}
	out := m.Prometheus()
	for _, want := range []string{
		"minikueue_evicted_workloads_total 1",
		"minikueue_admission_check_waits_total 1",
		"minikueue_pods_ready_timeouts_total 1",
		"minikueue_reclaimed_pods_total 3",
		"minikueue_admitted_active_workloads 3",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("missing %q in:\n%s", want, out)
		}
	}
}
