package multikueue

import (
	"testing"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/controller"
	"example.com/minikueue/internal/store"
)

func workerEngine(t *testing.T, cpu int64) *controller.Engine {
	t.Helper()
	e := controller.New(store.New())
	if err := e.Store.UpsertClusterQueue(&api.ClusterQueue{
		Name: "worker-cq",
		Spec: api.ClusterQueueSpec{
			NamespaceSelector: &api.LabelSelector{},
			QueueingStrategy:  api.BestEffortFIFO,
			ResourceGroups: []api.ResourceGroup{{
				CoveredResources: []api.ResourceName{api.CPU},
				Flavors:          []api.FlavorQuota{{Name: "default", Nominal: api.RL("cpu", cpu)}},
			}},
		},
	}); err != nil {
		t.Fatal(err)
	}
	if err := e.Store.UpsertLocalQueue(&api.LocalQueue{Namespace: "default", Name: "q", ClusterQueue: "worker-cq"}); err != nil {
		t.Fatal(err)
	}
	e.Store.UpsertNamespace("default", nil)
	return e
}

func workload() *api.Workload {
	return &api.Workload{
		Namespace: "default", Name: "job",
		Spec: api.WorkloadSpec{
			Active: true, QueueName: "q",
			PodSets: []api.PodSet{{Name: "main", Count: 1, Requests: api.RL("cpu", 2000)}},
		},
	}
}

func TestAllAtOnceSelectsFirstAdmittingWorkerAndCleansLosers(t *testing.T) {
	small := workerEngine(t, 1000)
	large := workerEngine(t, 4000)
	m := NewManager("m", workload(), []*Worker{
		NewWorker("small", small),
		NewWorker("large", large),
	}, Config{Dispatcher: AllAtOnce})

	m.Tick()
	if m.Selected != "large" {
		t.Fatalf("selected = %q, want large", m.Selected)
	}
	if got := m.NominatedSorted(); len(got) != 2 || got[0] != "large" || got[1] != "small" {
		t.Fatalf("nominated = %v, want [large small]", got)
	}
	if _, ok := small.Store.GetWorkload("default", "job"); ok {
		t.Fatal("losing worker copy should be deleted")
	}
	if w, ok := large.Store.GetWorkload("default", "job"); !ok || w.Status.State != api.StateAdmitted {
		t.Fatalf("large worker state = %+v, ok=%v", w, ok)
	}
}

func TestIncrementalExpandsUntilAdmission(t *testing.T) {
	small := workerEngine(t, 1000)
	large := workerEngine(t, 4000)
	m := NewManager("m", workload(), []*Worker{
		NewWorker("small", small),
		NewWorker("large", large),
	}, Config{Dispatcher: Incremental, StepSize: 1})

	m.Tick() // only "small" is nominated
	if m.Selected != "" {
		t.Fatalf("selected too early: %q", m.Selected)
	}
	if got := m.NominatedSorted(); len(got) != 1 || got[0] != "small" {
		t.Fatalf("first-round nominated = %v, want [small]", got)
	}
	if _, ok := large.Store.GetWorkload("default", "job"); ok {
		t.Fatal("large should not be nominated in the first round")
	}
	if _, ok := small.Store.GetWorkload("default", "job"); !ok {
		t.Fatal("an unadmitted incremental copy must survive to the next round")
	}
	m.Tick() // now both are nominated
	if got := m.NominatedSorted(); len(got) != 2 || got[0] != "large" || got[1] != "small" {
		t.Fatalf("second-round nominated = %v, want [large small]", got)
	}
	if m.Selected != "large" {
		t.Fatalf("selected = %q, want large", m.Selected)
	}

	t.Run("lost worker wait does not consume incremental rounds", func(t *testing.T) {
		var workers []*Worker
		for _, name := range []string{"a", "b", "c", "d"} {
			workers = append(workers, NewWorker(name, workerEngine(t, 4000)))
		}
		m := NewManager("m", workload(), workers, Config{
			Dispatcher:        Incremental,
			StepSize:          1,
			WorkerLostTimeout: 2,
		})
		m.Tick() // round 1 -> a
		if m.Selected != "a" {
			t.Fatalf("initial selected = %q, want a", m.Selected)
		}
		m.SetWorkerDown("a", true)
		m.Tick() // waiting 1/2
		m.Tick() // waiting 2/2
		m.Tick() // timeout -> round 1 again, should nominate only b
		if got := m.NominatedSorted(); len(got) != 1 || got[0] != "b" {
			t.Fatalf("after failover nominated = %v, want [b]", got)
		}
	})
}

func TestDuplicateWorkerNamesStillCleanUpLosers(t *testing.T) {
	first := workerEngine(t, 4000)
	second := workerEngine(t, 4000)
	m := NewManager("m", workload(), []*Worker{
		NewWorker("same", first),
		NewWorker("same", second),
	}, Config{Dispatcher: AllAtOnce})

	m.Tick()
	if m.Selected != "same" {
		t.Fatalf("selected = %q", m.Selected)
	}
	// Pointer identity, not the name, decides which copy survived.
	if _, ok := first.Store.GetWorkload("default", "job"); !ok {
		t.Fatal("first worker's copy should be the winner")
	}
	if _, ok := second.Store.GetWorkload("default", "job"); ok {
		t.Fatal("second worker's copy should be deleted despite the duplicate name")
	}

	t.Run("restored name marks only one snapshot row", func(t *testing.T) {
		m := NewManager("m", workload(), []*Worker{
			NewWorker("same", workerEngine(t, 4000)),
			NewWorker("same", workerEngine(t, 4000)),
		}, Config{Dispatcher: AllAtOnce})
		m.Selected = "same"
		selected := 0
		for _, status := range m.Snapshot() {
			if status.Selected {
				selected++
			}
		}
		if selected != 1 {
			t.Fatalf("snapshot selected rows = %d, want 1", selected)
		}
	})

	t.Run("empty selected does not mark an empty-named worker", func(t *testing.T) {
		m := NewManager("m", workload(), []*Worker{
			NewWorker("", workerEngine(t, 4000)),
		}, Config{Dispatcher: AllAtOnce})
		if m.Snapshot()[0].Selected {
			t.Fatal("empty Selected must not match an empty worker name")
		}
	})
}

func TestUnknownDispatcherDoesNotBroadcast(t *testing.T) {
	first := workerEngine(t, 4000)
	second := workerEngine(t, 4000)
	m := NewManager("m", workload(), []*Worker{
		NewWorker("a", first),
		NewWorker("b", second),
	}, Config{Dispatcher: Dispatcher("Incremental ")}) // typo

	m.Tick()
	if len(m.Nominated) != 0 {
		t.Fatalf("nominated = %v, want none", m.Nominated)
	}
	if _, ok := first.Store.GetWorkload("default", "job"); ok {
		t.Fatal("first worker should not receive a copy")
	}
	if _, ok := second.Store.GetWorkload("default", "job"); ok {
		t.Fatal("second worker should not receive a copy")
	}
	if len(m.Events) == 0 || m.Events[0].Kind != "InvalidDispatcher" {
		t.Fatalf("events = %+v, want InvalidDispatcher", m.Events)
	}
}

func TestDeletedSelectedCopyIsRedispatched(t *testing.T) {
	first := workerEngine(t, 4000)
	second := workerEngine(t, 4000)
	w1 := NewWorker("a", first)
	w2 := NewWorker("b", second)
	m := NewManager("m", workload(), []*Worker{w1, w2}, Config{Dispatcher: AllAtOnce})

	m.Tick()
	if m.Selected == "" {
		t.Fatal("expected a selection")
	}
	winner := m.selected
	if winner == nil {
		t.Fatal("selected pointer is nil")
	}
	if err := winner.Engine.DeleteWorkload("default", "job"); err != nil {
		t.Fatal(err)
	}
	// Keep the old winner from winning again so the test can observe a real
	// re-dispatch to the other worker.
	if err := winner.Engine.SetLocalQueueStopPolicy("default", "q", api.StopHold); err != nil {
		t.Fatal(err)
	}
	m.Tick()
	if m.Selected == "" {
		t.Fatal("expected re-dispatch after the selected copy disappeared")
	}
	if m.selected == winner {
		t.Fatal("selected pointer should not stay on the deleted copy")
	}
	if _, ok := m.selected.Engine.Store.GetWorkload("default", "job"); !ok {
		t.Fatal("new selected worker has no workload copy")
	}
}

func TestDownWorkerIsNotNominated(t *testing.T) {
	first := workerEngine(t, 4000)
	second := workerEngine(t, 4000)
	w1 := NewWorker("first", first)
	w1.Down = true
	w2 := NewWorker("second", second)
	m := NewManager("m", workload(), []*Worker{w1, w2}, Config{Dispatcher: AllAtOnce})

	m.Tick()
	if m.Selected != "second" {
		t.Fatalf("selected = %q, want second", m.Selected)
	}
	if got := m.NominatedSorted(); len(got) != 1 || got[0] != "second" {
		t.Fatalf("nominated = %v, want [second]", got)
	}
	if _, ok := first.Store.GetWorkload("default", "job"); ok {
		t.Fatal("down worker should not receive a copy")
	}
	if statuses := m.Snapshot(); statuses[0].State != "Down" {
		t.Fatalf("first status = %q, want Down", statuses[0].State)
	}

	t.Run("down worker is pruned from a stale nomination list", func(t *testing.T) {
		a := workerEngine(t, 4000)
		b := workerEngine(t, 4000)
		c := workerEngine(t, 4000)
		m := NewManager("m", workload(), []*Worker{
			NewWorker("a", a), NewWorker("b", b), NewWorker("c", c),
		}, Config{Dispatcher: AllAtOnce})
		m.Tick() // selects a; nominated [a b c]
		m.SetWorkerDown("c", true)
		m.Tick() // selected a is healthy -> early return
		for _, name := range m.Nominated {
			if name == "c" {
				t.Fatalf("nominated = %v, want no down worker", m.Nominated)
			}
		}
	})
}

func TestFailoverAfterWorkerLostTimeout(t *testing.T) {
	first := workerEngine(t, 4000)
	second := workerEngine(t, 4000)
	w1 := NewWorker("first", first)
	w1.Down = true
	w2 := NewWorker("second", second)
	m := NewManager("m", workload(), []*Worker{w1, w2}, Config{
		Dispatcher:        AllAtOnce,
		WorkerLostTimeout: 1,
	})

	m.Tick()
	if m.Selected != "second" {
		t.Fatalf("selected = %q, want second", m.Selected)
	}

	// The winner goes away and the lost worker comes back. The first lost tick
	// is within the timeout, so the manager keeps the selection.
	if !m.SetWorkerDown("second", true) || !m.SetWorkerDown("first", false) {
		t.Fatal("SetWorkerDown failed to find a worker")
	}
	events := m.Tick()
	if m.Selected != "second" || m.selected != w2 {
		t.Fatalf("selection changed too early: %q", m.Selected)
	}
	if !hasEvent(events, "WorkerUnavailable") {
		t.Fatalf("events = %+v, want WorkerUnavailable", events)
	}
	if len(m.Nominated) != 0 {
		t.Fatalf("nominated = %v, want none while waiting for the lost worker", m.Nominated)
	}

	// The second lost tick exceeds the timeout: re-dispatch to worker-1.
	events = m.Tick()
	if m.Selected != "first" || m.selected != w1 {
		t.Fatalf("selected = %q, want first", m.Selected)
	}
	if !hasEvent(events, "WorkerLost") {
		t.Fatalf("events = %+v, want WorkerLost", events)
	}
	if w, ok := first.Store.GetWorkload("default", "job"); !ok || w.Status.State != api.StateAdmitted {
		t.Fatalf("first worker state = %+v, ok=%v", w, ok)
	}

	// When the old winner recovers it still holds the stale copy; the manager
	// must delete it instead of letting it compete with the new winner.
	if !m.SetWorkerDown("second", false) {
		t.Fatal("SetWorkerDown(second, false) failed")
	}
	events = m.Tick()
	if !hasEvent(events, "CleanedUp") {
		t.Fatalf("events = %+v, want CleanedUp for the recovered worker", events)
	}
	if _, ok := second.Store.GetWorkload("default", "job"); ok {
		t.Fatal("stale copy on the recovered worker should be deleted")
	}
}

func TestNoAvailableWorker(t *testing.T) {
	first := workerEngine(t, 4000)
	w1 := NewWorker("first", first)
	w1.Down = true
	m := NewManager("m", workload(), []*Worker{w1}, Config{Dispatcher: AllAtOnce})

	events := m.Tick()
	if !hasEvent(events, "NoAvailableWorker") {
		t.Fatalf("events = %+v, want NoAvailableWorker", events)
	}
	if m.Selected != "" {
		t.Fatalf("selected = %q, want empty", m.Selected)
	}
}

func hasEvent(events []Event, kind string) bool {
	for _, event := range events {
		if event.Kind == kind {
			return true
		}
	}
	return false
}

func TestRepeatedTicksDoNotDuplicateCopies(t *testing.T) {
	large := workerEngine(t, 4000)
	m := NewManager("m", workload(), []*Worker{NewWorker("large", large)}, Config{Dispatcher: AllAtOnce})
	m.Tick()
	m.Tick()
	m.Tick()
	if m.Selected != "large" {
		t.Fatalf("selected = %q", m.Selected)
	}
	if got := len(large.Store.ListWorkloads()); got != 1 {
		t.Fatalf("workloads = %d, want 1", got)
	}
}
