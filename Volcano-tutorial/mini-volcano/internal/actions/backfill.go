package actions

import (
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Backfill schedules BestEffort pods (zero requests) into capacity that the
// main allocation pass did not use. Volcano's backfill action also helps
// long-running pods pack into idle holes; the zero-request case is the one that
// matters for the gang semantics demonstrated here.
type Backfill struct{}

func (a *Backfill) Name() string { return "backfill" }

func (a *Backfill) Execute(ssn *framework.Session) error {
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
			if job.Status.Phase != api.JobInqueue && job.Status.Phase != api.JobRunning {
				continue
			}
			pending := ssn.PendingPods(job.UID)
			sort.SliceStable(pending, func(i, k int) bool { return ssn.TaskOrder(job, pending[i], pending[k]) < 0 })
			// Backfill is a gang-aware transaction too: binding one BestEffort
			// pod at a time would let a job start partially and bypass both
			// minAvailable and minTaskMember. Build one statement per job,
			// then commit only if the gang is satisfied.
			st := ssn.NewStatement()
			type boundPod struct {
				name string
				node string
			}
			var bound []boundPod
			for _, pod := range pending {
				if !pod.Requests.IsZero() {
					continue
				}
				node, _ := bestNode(ssn, st, pod)
				if node == "" {
					ssn.RecordUnschedulable(job.UID, "backfill: no node accepts BestEffort pod "+pod.Name)
					continue
				}
				if err := st.Bind(pod.UID, node); err != nil {
					continue
				}
				bound = append(bound, boundPod{name: pod.Name, node: node})
			}
			if len(bound) == 0 {
				st.Discard()
				continue
			}
			if !gangSatisfied(st, job) {
				st.Discard()
				ssn.RecordUnschedulable(job.UID, "backfill: "+gangReason(st, job, st.ReadyCount(job.UID), len(bound)))
				continue
			}
			st.Commit()
			for _, b := range bound {
				ssn.AddEvent(api.Event{Type: "Backfilled", Job: job.Name, Pod: b.name, Node: b.node,
					Queue: job.Spec.Queue})
			}
		}
	}
	return nil
}
