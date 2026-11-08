package scenario

import "testing"

func TestMultiKueueFailoverScenario(t *testing.T) {
	m := MultiKueueFailover()

	m.Tick()
	if m.Selected != "worker-2" {
		t.Fatalf("selected = %q, want worker-2 (worker-1 starts down)", m.Selected)
	}

	if !m.SetWorkerDown("worker-2", true) || !m.SetWorkerDown("worker-1", false) {
		t.Fatal("SetWorkerDown failed to find a worker")
	}
	m.Tick() // 1/1: still waiting for the lost-worker timeout
	if m.Selected != "worker-2" {
		t.Fatalf("selected = %q, want worker-2 while waiting", m.Selected)
	}
	m.Tick() // 2/1: timeout exceeded, re-dispatch to worker-1
	if m.Selected != "worker-1" {
		t.Fatalf("selected = %q, want worker-1", m.Selected)
	}
}
