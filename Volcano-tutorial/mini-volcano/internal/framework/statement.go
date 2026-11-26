package framework

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
)

// Statement is the transactional core of a scheduling decision. All mutations
// are applied to a private cache clone. Commit publishes the clone as the
// session's working snapshot; Discard throws it away.
//
// Volcano implements Statement as an operation log applied to the cache. A
// clone is less efficient but makes rollback trivially correct, which is the
// right trade-off for a teaching implementation.
type Statement struct {
	ssn     *Session
	trial   *cache.Cache
	events  []api.Event
	done    bool
	evicted []string
}

func (s *Session) NewStatement() *Statement {
	return &Statement{ssn: s, trial: s.Cache.Clone()}
}

func (st *Statement) Trial() *cache.Cache { return st.trial }

func (st *Statement) Event(e api.Event) {
	if e.Tick == 0 {
		e.Tick = st.ssn.Now
	}
	st.events = append(st.events, e)
}

func (st *Statement) Bind(podUID, nodeName string) error {
	if st.done {
		return fmt.Errorf("statement already finalized")
	}
	pod := st.trial.Pods[podUID]
	if pod == nil {
		return fmt.Errorf("pod %q not found in statement", podUID)
	}
	if err := st.trial.Bind(podUID, nodeName, st.ssn.Now); err != nil {
		return err
	}
	st.Event(api.Event{
		Type:    "Scheduled",
		Job:     pod.JobName,
		Pod:     pod.Name,
		Node:    nodeName,
		Queue:   pod.Queue,
		Message: fmt.Sprintf("requests=%s priority=%d", pod.Requests, pod.Priority),
	})
	return nil
}

func (st *Statement) Evict(podUID string, reason string) error {
	if st.done {
		return fmt.Errorf("statement already finalized")
	}
	pod := st.trial.Pods[podUID]
	if pod == nil {
		return fmt.Errorf("pod %q not found in statement", podUID)
	}
	node := pod.NodeName
	if err := st.trial.Evict(podUID, st.ssn.Now); err != nil {
		return err
	}
	st.evicted = append(st.evicted, podUID)
	st.Event(api.Event{
		Type:    "Evicted",
		Job:     pod.JobName,
		Pod:     pod.Name,
		Node:    node,
		Queue:   pod.Queue,
		Message: reason,
	})
	return nil
}

// withTrial temporarily points the session at the tentative cache. Plugins that
// query ssn.Cache (for example task-topology) therefore observe bindings made
// earlier in the same statement.
func (st *Statement) withTrial(fn func() error) error {
	old := st.ssn.Cache
	st.ssn.Cache = st.trial
	defer func() { st.ssn.Cache = old }()
	return fn()
}

func (st *Statement) Predicate(pod *api.Pod, nodeName string) error {
	node := st.trial.Nodes[nodeName]
	if node == nil {
		return fmt.Errorf("node %q not found", nodeName)
	}
	return st.withTrial(func() error { return st.ssn.Predicate(pod, node) })
}

func (st *Statement) NodeScore(pod *api.Pod, nodeName string) (float64, error) {
	node := st.trial.Nodes[nodeName]
	if node == nil {
		return 0, fmt.Errorf("node %q not found", nodeName)
	}
	var score float64
	err := st.withTrial(func() error {
		var e error
		score, e = st.ssn.NodeScore(pod, node)
		return e
	})
	return score, err
}

func (st *Statement) Allocatable(queue string, res api.Resource) bool {
	q := st.trial.Queues[queue]
	if q == nil {
		return false
	}
	ok := false
	_ = st.withTrial(func() error {
		ok = st.ssn.Allocatable(q, res)
		return nil
	})
	return ok
}

func (st *Statement) ReadyCount(jobUID string) int { return st.trial.ReadyCount(jobUID) }

func (st *Statement) ReadyCountByTask(jobUID string) map[string]int {
	return st.trial.ReadyCountByTask(jobUID)
}

func (st *Statement) Preemptable(preemptor, victim *api.Pod) bool {
	ok := false
	_ = st.withTrial(func() error {
		ok = st.ssn.Preemptable(preemptor, victim)
		return nil
	})
	return ok
}

func (st *Statement) Disruptable(victim *api.Pod) bool {
	ok := false
	_ = st.withTrial(func() error {
		ok = st.ssn.Disruptable(victim)
		return nil
	})
	return ok
}

func (st *Statement) Reclaimable(queue *api.Queue, victim *api.Pod) bool {
	ok := false
	_ = st.withTrial(func() error {
		ok = st.ssn.Reclaimable(queue, victim)
		return nil
	})
	return ok
}

func (st *Statement) Overused(queue *api.Queue) bool {
	ok := false
	_ = st.withTrial(func() error {
		ok = st.ssn.Overused(queue)
		return nil
	})
	return ok
}

func (st *Statement) JobStarving(job *api.Job) bool {
	ok := false
	_ = st.withTrial(func() error {
		ok = st.ssn.JobStarving(job)
		return nil
	})
	return ok
}

func (st *Statement) Commit() {
	if st.done {
		return
	}
	st.ssn.Cache = st.trial
	for _, e := range st.events {
		st.ssn.AddEvent(e)
	}
	st.done = true
}

func (st *Statement) Discard() {
	st.done = true
	st.events = nil
}
