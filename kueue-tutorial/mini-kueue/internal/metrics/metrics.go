// Package metrics provides a tiny Prometheus-compatible surface for the
// simulator. It intentionally names the help text after the real Kueue
// metrics so the tutorial can compare the two.
package metrics

import (
	"fmt"
	"strings"
	"sync"
)

// Metrics is concurrency-safe.
type Metrics struct {
	mu sync.Mutex

	Cycles              int64
	AdmittedTotal       int64
	FinishedTotal       int64
	EvictedTotal        int64
	PreemptedTotal      int64
	QuotaReservedTotal  int64
	AdmissionCheckWaits int64
	PodsReadyTimeouts   int64
	ReclaimedPodsTotal  int64

	Pending  int64
	Reserved int64
	Admitted int64
	Finished int64
}

// Snapshot is a copy of the counters.
type Snapshot struct {
	Cycles              int64
	AdmittedTotal       int64
	FinishedTotal       int64
	EvictedTotal        int64
	PreemptedTotal      int64
	QuotaReservedTotal  int64
	AdmissionCheckWaits int64
	PodsReadyTimeouts   int64
	ReclaimedPodsTotal  int64
	Pending             int64
	Reserved            int64
	Admitted            int64
	Finished            int64
}

// IncCycle records a scheduling cycle.
func (m *Metrics) IncCycle() {
	m.mu.Lock()
	m.Cycles++
	m.mu.Unlock()
}

// IncAdmitted records a successful admission.
func (m *Metrics) IncAdmitted() {
	m.mu.Lock()
	m.AdmittedTotal++
	m.mu.Unlock()
}

// IncFinished records a completed workload.
func (m *Metrics) IncFinished() {
	m.mu.Lock()
	m.FinishedTotal++
	m.mu.Unlock()
}

// IncEvicted records an eviction.
func (m *Metrics) IncEvicted(preempted bool) {
	m.mu.Lock()
	m.EvictedTotal++
	if preempted {
		m.PreemptedTotal++
	}
	m.mu.Unlock()
}

// IncQuotaReserved records a quota reservation.
func (m *Metrics) IncQuotaReserved() {
	m.mu.Lock()
	m.QuotaReservedTotal++
	m.mu.Unlock()
}

// IncAdmissionCheckWait records a workload waiting on an admission check.
func (m *Metrics) IncAdmissionCheckWait() {
	m.mu.Lock()
	m.AdmissionCheckWaits++
	m.mu.Unlock()
}

// IncPodsReadyTimeout records a WaitForPodsReady timeout.
func (m *Metrics) IncPodsReadyTimeout() {
	m.mu.Lock()
	m.PodsReadyTimeouts++
	m.mu.Unlock()
}

// IncReclaimed records pods released through dynamic reclaim.
func (m *Metrics) IncReclaimed(count int64) {
	m.mu.Lock()
	m.ReclaimedPodsTotal += count
	m.mu.Unlock()
}

// SetGauges updates lifecycle gauges.
func (m *Metrics) SetGauges(pending, reserved, admitted, finished int64) {
	m.mu.Lock()
	m.Pending = pending
	m.Reserved = reserved
	m.Admitted = admitted
	m.Finished = finished
	m.mu.Unlock()
}

// Snapshot returns a consistent copy.
func (m *Metrics) Snapshot() Snapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	return Snapshot{
		Cycles:              m.Cycles,
		AdmittedTotal:       m.AdmittedTotal,
		FinishedTotal:       m.FinishedTotal,
		EvictedTotal:        m.EvictedTotal,
		PreemptedTotal:      m.PreemptedTotal,
		QuotaReservedTotal:  m.QuotaReservedTotal,
		AdmissionCheckWaits: m.AdmissionCheckWaits,
		PodsReadyTimeouts:   m.PodsReadyTimeouts,
		ReclaimedPodsTotal:  m.ReclaimedPodsTotal,
		Pending:             m.Pending,
		Reserved:            m.Reserved,
		Admitted:            m.Admitted,
		Finished:            m.Finished,
	}
}

// Prometheus renders a small exposition. The names use a minikueue_ prefix so
// they cannot be confused with the real controller's metrics.
func (m *Metrics) Prometheus() string {
	s := m.Snapshot()
	var b strings.Builder
	writeCounter(&b, "minikueue_admission_cycles_total", "Scheduling cycles executed.", s.Cycles)
	writeCounter(&b, "minikueue_admitted_workloads_total", "Workloads admitted.", s.AdmittedTotal)
	writeCounter(&b, "minikueue_finished_workloads_total", "Workloads finished.", s.FinishedTotal)
	writeCounter(&b, "minikueue_evicted_workloads_total", "Workloads evicted.", s.EvictedTotal)
	writeCounter(&b, "minikueue_preempted_workloads_total", "Workloads preempted.", s.PreemptedTotal)
	writeCounter(&b, "minikueue_quota_reserved_workloads_total", "Quota reservations.", s.QuotaReservedTotal)
	writeCounter(&b, "minikueue_admission_check_waits_total", "Reconciliations that observed a pending admission check.", s.AdmissionCheckWaits)
	writeCounter(&b, "minikueue_pods_ready_timeouts_total", "WaitForPodsReady timeouts.", s.PodsReadyTimeouts)
	writeCounter(&b, "minikueue_reclaimed_pods_total", "Pods released through dynamic reclaim.", s.ReclaimedPodsTotal)
	writeGauge(&b, "minikueue_pending_workloads", "Workloads pending admission.", s.Pending)
	writeGauge(&b, "minikueue_reserving_active_workloads", "Workloads holding quota before admission.", s.Reserved)
	writeGauge(&b, "minikueue_admitted_active_workloads", "Workloads currently admitted.", s.Admitted)
	writeGauge(&b, "minikueue_finished_workloads", "Workloads finished.", s.Finished)
	return b.String()
}

func writeCounter(b *strings.Builder, name, help string, value int64) {
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s counter\n%s %d\n", name, help, name, name, value)
}

func writeGauge(b *strings.Builder, name, help string, value int64) {
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s gauge\n%s %d\n", name, help, name, name, value)
}
