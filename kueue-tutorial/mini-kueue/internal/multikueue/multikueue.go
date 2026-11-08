// Package multikueue is a single-process model of Kueue's manager/worker
// MultiKueue architecture. A manager copies one Workload to nominated worker
// clusters; the first worker that admits it wins and the other copies are
// removed. The package intentionally uses the same MiniKueue Engine as a
// worker, so dispatch, quota, and admission semantics stay consistent.
package multikueue

import (
	"fmt"
	"sort"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/controller"
)

// Dispatcher selects which workers are nominated.
type Dispatcher string

const (
	// AllAtOnce copies the workload to every worker in the same round.
	AllAtOnce Dispatcher = "AllAtOnce"
	// Incremental nominates StepSize workers per round, expanding only when
	// none of the current nominees can admit.
	Incremental Dispatcher = "Incremental"
)

// Config controls dispatch.
type Config struct {
	Dispatcher Dispatcher
	StepSize   int

	// WorkerLostTimeout is the number of consecutive ticks a selected worker
	// may stay unreachable before the manager gives up on it and re-dispatches
	// to the remaining healthy workers. Zero re-dispatches on the first lost
	// tick. Real Kueue defaults workerLostTimeout to 15 minutes; MiniKueue
	// counts ticks so the behaviour is deterministic in tests.
	WorkerLostTimeout int
}

// Worker wraps an independent MiniKueue engine plus the copy it currently
// holds. Engine is nil-safe for tests that only exercise nomination.
type Worker struct {
	Name     string
	Engine   *controller.Engine
	Workload *api.Workload

	// Down simulates a worker cluster that cannot be reached. A down worker is
	// not nominated and is not ticked. If it was selected, the manager waits
	// WorkerLostTimeout ticks before re-dispatching. Its in-memory copy is left
	// in place to model the remote object that the manager cannot delete while
	// the connection is down; the copy is removed when the worker recovers.
	Down bool
}

// NewWorker builds a worker. The engine may be nil for dry-run tests.
func NewWorker(name string, engine *controller.Engine) *Worker {
	return &Worker{Name: name, Engine: engine}
}

// Event is an audit record emitted by one manager tick.
type Event struct {
	Tick    int64
	Kind    string
	Worker  string
	Message string
}

// Manager coordinates one workload across workers. It is deliberately
// single-workload: real MultiKueue supports many, but one is enough to show
// nomination, selection, cleanup, and status propagation.
type Manager struct {
	Name     string
	Workload *api.Workload
	Workers  []*Worker
	Config   Config

	Nominated []string
	Selected  string
	Step      int
	// rounds counts actual dispatch rounds, not ticks. Waiting for a lost
	// worker must not consume the Incremental dispatcher's expansion budget.
	rounds int
	Events []Event

	// selected is the winning Worker object. Selection and cleanup must use
	// pointer identity: worker names are not guaranteed unique in tests or in
	// loosely configured multi-cluster setups.
	selected  *Worker
	downTicks map[*Worker]int
}

// NewManager creates a manager. Workers are kept in the supplied order; that
// order is the incremental nomination preference.
func NewManager(name string, workload *api.Workload, workers []*Worker, cfg Config) *Manager {
	if cfg.Dispatcher == "" {
		cfg.Dispatcher = AllAtOnce
	}
	if cfg.StepSize <= 0 {
		cfg.StepSize = 3
	}
	if cfg.WorkerLostTimeout < 0 {
		cfg.WorkerLostTimeout = 0
	}
	return &Manager{
		Name: name, Workload: workload, Workers: workers, Config: cfg,
		downTicks: map[*Worker]int{},
	}
}

// SetWorkerDown marks a worker reachable or unreachable. It returns false when
// no worker has that name. Duplicate names are all updated: names are labels
// for humans, pointer identity is what the manager uses internally.
func (m *Manager) SetWorkerDown(name string, down bool) bool {
	found := false
	for _, worker := range m.Workers {
		if worker.Name != name {
			continue
		}
		worker.Down = down
		if !down {
			delete(m.downTicks, worker)
		}
		found = true
	}
	return found
}

// Tick performs one dispatch round. It returns the events of this round.
func (m *Manager) Tick() []Event {
	m.Step++
	m.Events = nil
	if m.Workload == nil {
		m.event("Invalid", "-", "manager has no workload")
		return m.Events
	}
	if !validDispatcher(m.Config.Dispatcher) {
		m.event("InvalidDispatcher", "-", fmt.Sprintf("unknown dispatcher %q", m.Config.Dispatcher))
		return m.Events
	}
	// A worker can go Down between ticks. Remove it from the previous round's
	// nomination list even if the selected worker is healthy and this Tick
	// returns early; otherwise status.nominatedClusterNames would advertise an
	// unreachable worker.
	m.pruneNominated()
	if m.Selected != "" {
		selected := m.selected
		switch {
		case selected == nil:
			// A selection restored by name alone is ambiguous when worker
			// names are duplicated. Drop it and re-dispatch rather than guess.
			m.Selected = ""
			m.rounds = 0
		case selected.Down:
			m.downTicks[selected]++
			if m.downTicks[selected] <= m.Config.WorkerLostTimeout {
				// No dispatch happened this round, so do not keep the previous
				// round's nomination list visible: it would suggest that a
				// down worker is still being considered.
				m.Nominated = m.Nominated[:0]
				m.event("WorkerUnavailable", selected.Name,
					fmt.Sprintf("selected worker is unreachable (%d/%d ticks)",
						m.downTicks[selected], m.Config.WorkerLostTimeout))
				return m.Events
			}
			m.event("WorkerLost", selected.Name, "lost-worker timeout exceeded; re-dispatching")
			m.Selected = ""
			m.selected = nil
			m.rounds = 0
			delete(m.downTicks, selected)
			// The old winner is unreachable, but any healthy loser copy is
			// stale and can be removed now.
			m.cleanupStale(nil)
		default:
			delete(m.downTicks, selected)
			// A selected copy can disappear (worker failure, manual deletion,
			// or a test cleanup). A copy that still exists but is temporarily
			// Pending stays selected; only a missing object is re-dispatched.
			if selected.Workload != nil && selected.Engine != nil {
				if current, ok := selected.Engine.Store.GetWorkload(
					selected.Workload.Namespace, selected.Workload.Name); ok {
					// A recovered worker may still hold a stale losing copy
					// from before it went down. Remove it while the winner is
					// healthy, then keep the selection. A copy that is
					// temporarily Pending (for example after eviction) also
					// stays selected: re-dispatching on every state change
					// would flap between workers.
					m.cleanupStale(selected)
					message := "workload already admitted on the selected worker"
					if current.Status.State != api.StateAdmitted {
						message = fmt.Sprintf("workload still present on the selected worker (state=%s)", current.Status.State)
					}
					m.event("Selected", m.Selected, message)
					return m.Events
				}
				selected.Workload = nil
			}
			m.Selected = ""
			m.selected = nil
			m.rounds = 0
			m.cleanupStale(nil)
		}
	}

	candidates := m.nominate()
	if len(candidates) == 0 {
		m.event("NoAvailableWorker", "-", "no healthy worker is available")
		return m.Events
	}
	// Keep the nomination list observable. Real MultiKueue publishes it in
	// status.nominatedClusterNames; without this the manager's decision is
	// invisible even though dispatch still works.
	m.Nominated = m.Nominated[:0]
	for _, worker := range candidates {
		m.Nominated = append(m.Nominated, worker.Name)
	}
	for _, worker := range candidates {
		if worker.Workload == nil {
			copy := m.Workload.Clone()
			if worker.Engine != nil {
				if err := worker.Engine.Submit(copy); err != nil {
					m.event("SubmitFailed", worker.Name, err.Error())
					continue
				}
			}
			worker.Workload = copy
			m.event("Dispatched", worker.Name, "workload copy created")
		}
	}

	// Tick every reachable worker holding a copy so that an error on one
	// worker does not hide progress on another.
	for _, worker := range m.Workers {
		if worker.Down || worker.Engine == nil || worker.Workload == nil {
			continue
		}
		worker.Engine.Tick()
	}

	for _, worker := range m.Workers {
		if worker.Down || worker.Workload == nil {
			continue
		}
		if m.Selected == "" && admitted(worker) {
			m.Selected = worker.Name
			m.selected = worker
			m.event("Selected", worker.Name, "worker admitted the workload first")
		}
	}
	if m.selected != nil {
		// Only a real winner makes the other copies stale. With no winner,
		// Incremental / AllAtOnce copies must survive across rounds so they can
		// keep competing for admission.
		m.cleanupStale(m.selected)
	}
	return m.Events
}

// cleanupStale removes copies from every healthy worker except the selected
// one. Down workers keep their copies until they recover; that models a remote
// object the manager cannot delete while the connection is broken. A nil
// selected worker means "no winner right now", so every healthy copy is stale.
func (m *Manager) cleanupStale(selected *Worker) {
	for _, worker := range m.Workers {
		if worker == selected || worker.Workload == nil || worker.Down {
			continue
		}
		if worker.Engine != nil {
			_ = worker.Engine.DeleteWorkload(worker.Workload.Namespace, worker.Workload.Name)
		}
		worker.Workload = nil
		m.event("CleanedUp", worker.Name, "losing copy removed")
	}
}

// pruneNominated removes names of workers that are currently Down from the
// last nomination list. Worker names are not guaranteed unique; if any worker
// with a name is Down, the name is removed.
func (m *Manager) pruneNominated() {
	if len(m.Nominated) == 0 {
		return
	}
	out := m.Nominated[:0]
	for _, name := range m.Nominated {
		down := false
		for _, worker := range m.Workers {
			if worker.Name == name && worker.Down {
				down = true
				break
			}
		}
		if !down {
			out = append(out, name)
		}
	}
	m.Nominated = out
}

// nominate returns the workers that should hold a copy this round. Down
// workers are skipped before the incremental limit is applied, so a lost
// worker does not consume one of the round's nomination slots.
func (m *Manager) nominate() []*Worker {
	healthy := make([]*Worker, 0, len(m.Workers))
	for _, worker := range m.Workers {
		if !worker.Down {
			healthy = append(healthy, worker)
		}
	}
	if len(healthy) == 0 {
		return nil
	}
	switch m.Config.Dispatcher {
	case Incremental:
		// Advance the expansion budget only when there is at least one healthy
		// worker to dispatch to. A tick spent waiting for a lost worker must
		// not turn stepSize=1 into "nominate everyone".
		m.rounds++
		limit := m.rounds * m.Config.StepSize
		if limit > len(healthy) {
			limit = len(healthy)
		}
		return healthy[:limit]
	case AllAtOnce:
		return healthy
	default:
		return nil
	}
}

func validDispatcher(d Dispatcher) bool {
	return d == AllAtOnce || d == Incremental
}

func admitted(worker *Worker) bool {
	if worker.Workload == nil || worker.Engine == nil {
		return false
	}
	current, ok := worker.Engine.Store.GetWorkload(worker.Workload.Namespace, worker.Workload.Name)
	if !ok {
		return false
	}
	return current.Status.State == api.StateAdmitted
}

// Status returns a stable snapshot of worker states for the CLI.
type Status struct {
	Worker   string
	State    string
	Selected bool
}

// Snapshot returns the current worker status in manager order.
func (m *Manager) Snapshot() []Status {
	out := make([]Status, 0, len(m.Workers))
	matchedByName := false
	for _, worker := range m.Workers {
		state := "-"
		if worker.Down {
			state = "Down"
		} else if worker.Workload != nil && worker.Engine != nil {
			if current, ok := worker.Engine.Store.GetWorkload(worker.Workload.Namespace, worker.Workload.Name); ok {
				state = string(current.Status.State)
			}
		}
		selected := worker == m.selected
		if m.selected == nil && m.Selected != "" && !matchedByName && worker.Name == m.Selected {
			// A selection restored by name has no pointer. Mark only the first
			// matching worker so duplicate names do not produce two "selected"
			// rows in the status view. An empty Selected means "nothing is
			// selected", not "the worker whose name is empty".
			selected = true
			matchedByName = true
		}
		out = append(out, Status{Worker: worker.Name, State: state, Selected: selected})
	}
	return out
}

// NominatedSorted returns the nominated worker names in stable order.
func (m *Manager) NominatedSorted() []string {
	out := append([]string(nil), m.Nominated...)
	sort.Strings(out)
	return out
}

func (m *Manager) event(kind, worker, message string) {
	m.Events = append(m.Events, Event{Tick: int64(m.Step), Kind: kind, Worker: worker, Message: message})
}

// String renders one event.
func (e Event) String() string {
	return fmt.Sprintf("[%s] %s: %s", e.Kind, e.Worker, e.Message)
}
