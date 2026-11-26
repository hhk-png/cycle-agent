package actions

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Allocate is the core action. It walks queues in share order, jobs in priority
// order, tasks in plugin order, and nodes in score order. Every job is attempted
// in a statement; the statement is committed only when the gang constraint is
// satisfied. This is the rollback point that makes gang scheduling real.
type Allocate struct{}

func (a *Allocate) Name() string { return "allocate" }

func (a *Allocate) Execute(ssn *framework.Session) error {
	queues := ssn.Queues()
	sort.SliceStable(queues, func(i, j int) bool { return ssn.QueueOrder(queues[i], queues[j]) < 0 })
	for _, q := range queues {
		if q.State() != api.QueueOpen {
			continue
		}
		jobs := ssn.JobsOfQueue(q.Name)
		sort.SliceStable(jobs, func(i, j int) bool { return ssn.JobOrder(jobs[i], jobs[j]) < 0 })
		for _, j := range jobs {
			job := ssn.JobByUID(j.UID)
			if job == nil || job.Status.Phase.Terminal() {
				continue
			}
			pg := ssn.Cache.PodGroups[job.UID]
			if pg == nil || (pg.Phase != api.PodGroupInqueue && job.Status.Phase != api.JobRunning) {
				continue
			}
			if len(ssn.PendingPods(job.UID)) == 0 {
				continue
			}
			allocateJob(ssn, job)
		}
	}
	return nil
}

func allocateJob(ssn *framework.Session, job *api.Job) {
	pending := ssn.PendingPods(job.UID)
	sort.SliceStable(pending, func(i, j int) bool { return ssn.TaskOrder(job, pending[i], pending[j]) < 0 })

	st := ssn.NewStatement()
	allocated := 0
	attempted := map[string]bool{}
	tryBind := func(pod *api.Pod) bool {
		if attempted[pod.UID] {
			return false
		}
		attempted[pod.UID] = true
		if !st.Allocatable(job.Spec.Queue, pod.Requests) {
			ssn.RecordUnschedulable(job.UID, fmt.Sprintf("queue %s capability would be exceeded by %s", job.Spec.Queue, pod.Requests))
			return false
		}
		node, why := bestNode(ssn, st, pod)
		if node == "" {
			if why != "" {
				ssn.RecordUnschedulable(job.UID, why)
			} else {
				ssn.RecordUnschedulable(job.UID, fmt.Sprintf("no feasible node for %s/%s", pod.Task, pod.Name))
			}
			return false
		}
		if err := st.Bind(pod.UID, node); err != nil {
			ssn.RecordUnschedulable(job.UID, "bind failed: "+err.Error())
			return false
		}
		allocated++
		return true
	}

	// Pass 1: satisfy every active role gate before spending capacity on
	// extra replicas. Without this, a role with many replicas can consume all
	// slots and the other role never gets its minimum, even though a feasible
	// gang exists.
	if job.RoleGatesActive() {
		byTask := st.ReadyCountByTask(job.UID)
		for i := range job.Spec.Tasks {
			t := &job.Spec.Tasks[i]
			want := t.EffectiveMinAvailable()
			if want <= 0 {
				continue
			}
			have := byTask[t.Name]
			for _, pod := range pending {
				if have >= int(want) {
					break
				}
				if pod.Task != t.Name {
					continue
				}
				if tryBind(pod) {
					have++
				}
			}
		}
	}

	// Pass 2: fill any remaining capacity in TaskOrder.
	for _, pod := range pending {
		tryBind(pod)
	}

	ready := st.ReadyCount(job.UID)
	min := int(job.Spec.MinAvailable)
	if gangSatisfied(st, job) {
		if allocated > 0 {
			st.Commit()
			ssn.AddEvent(api.Event{Type: "Allocated", Job: job.Name, Queue: job.Spec.Queue,
				Message: fmt.Sprintf("pods=%d ready=%d/%d", allocated, ready, min)})
		} else {
			st.Discard()
		}
		return
	}
	st.Discard()
	ssn.RecordUnschedulable(job.UID, gangReason(st, job, ready, allocated))
}

// gangSatisfied is the single commit predicate used by allocate, preempt and
// reclaim: the job-level total first, then the task-level role gates.
func gangSatisfied(st *framework.Statement, job *api.Job) bool {
	if job.Spec.MinAvailable <= 0 {
		return true
	}
	if st.ReadyCount(job.UID) < int(job.Spec.MinAvailable) {
		return false
	}
	return taskGatesSatisfied(st, job)
}

// taskGatesSatisfied enforces PodGroup.spec.minTaskMember. A job can have
// enough total Pods and still fail here: 8 workers without the single master
// cannot start a parameter-server job.
func taskGatesSatisfied(st *framework.Statement, job *api.Job) bool {
	if !job.RoleGatesActive() {
		return true
	}
	if st.ReadyCount(job.UID) < int(job.Spec.MinAvailable) {
		return false
	}
	counts := st.ReadyCountByTask(job.UID)
	for i := range job.Spec.Tasks {
		t := &job.Spec.Tasks[i]
		want := t.EffectiveMinAvailable()
		if want > 0 && counts[t.Name] < int(want) {
			return false
		}
	}
	return true
}

func gangReason(st *framework.Statement, job *api.Job, ready, allocated int) string {
	if !job.RoleGatesActive() {
		return fmt.Sprintf("gang not ready after allocation: ready=%d minAvailable=%d; rolled back %d tentative bindings",
			ready, job.Spec.MinAvailable, allocated)
	}
	counts := st.ReadyCountByTask(job.UID)
	for i := range job.Spec.Tasks {
		t := &job.Spec.Tasks[i]
		want := t.EffectiveMinAvailable()
		if want > 0 && counts[t.Name] < int(want) {
			return fmt.Sprintf("gang role gate not ready: task=%s ready=%d/%d totalReady=%d/%d; rolled back %d tentative bindings",
				t.Name, counts[t.Name], want, ready, job.Spec.MinAvailable, allocated)
		}
	}
	return fmt.Sprintf("gang not ready after allocation: ready=%d minAvailable=%d; rolled back %d tentative bindings",
		ready, job.Spec.MinAvailable, allocated)
}

// bestNode returns the highest-scoring feasible node. Ties break by node name
// so scenarios are reproducible across platforms and map iterations.
func bestNode(ssn *framework.Session, st *framework.Statement, pod *api.Pod) (string, string) {
	best := ""
	bestScore := 0.0
	firstErr := ""
	for _, node := range ssn.Nodes() {
		if err := st.Predicate(pod, node.Node.Name); err != nil {
			if firstErr == "" {
				firstErr = err.Error()
			}
			continue
		}
		score, err := st.NodeScore(pod, node.Node.Name)
		if err != nil {
			continue
		}
		if best == "" || score > bestScore || (score == bestScore && node.Node.Name < best) {
			best, bestScore = node.Node.Name, score
		}
	}
	return best, firstErr
}
