package actions

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Preempt implements intra-queue priority preemption. Volcano deliberately
// keeps cross-queue reclamation separate: preempt only evicts lower-priority
// pods in the same queue, while reclaim enforces queue shares.
type Preempt struct{}

func (a *Preempt) Name() string { return "preempt" }

func (a *Preempt) Execute(ssn *framework.Session) error {
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
			if pg == nil || pg.Phase != api.PodGroupInqueue {
				continue
			}
			if len(ssn.PendingPods(job.UID)) == 0 || !ssn.JobStarving(job) {
				continue
			}
			if tryPreempt(ssn, job) {
				break
			}
		}
	}
	return nil
}

func tryPreempt(ssn *framework.Session, job *api.Job) bool {
	pending := ssn.PendingPods(job.UID)
	sort.SliceStable(pending, func(i, j int) bool { return ssn.TaskOrder(job, pending[i], pending[j]) < 0 })

	st := ssn.NewStatement()
	victims := collectVictims(ssn, st, job)
	evicted := 0
	bound := 0
	for _, pod := range pending {
		if !st.Allocatable(job.Spec.Queue, pod.Requests) {
			break
		}
		node, _ := bestNode(ssn, st, pod)
		next := 0
		for node == "" && next < len(victims) {
			victim := victims[next]
			next++
			if !victim.Phase.OccupiesNode() {
				continue
			}
			if !st.Preemptable(pod, victim) {
				continue
			}
			if err := st.Evict(victim.UID, "preempted by higher-priority job "+job.Name); err != nil {
				continue
			}
			evicted++
			node, _ = bestNode(ssn, st, pod)
		}
		if node == "" {
			break
		}
		if err := st.Bind(pod.UID, node); err != nil {
			break
		}
		bound++
	}

	if evicted == 0 {
		ssn.RecordUnschedulable(job.UID,
			"preemption found no preemptable victims (priority, gang or PDB protection)")
		st.Discard()
		return false
	}
	if bound == 0 {
		st.Discard()
		return false
	}
	ready := st.ReadyCount(job.UID)
	if gangSatisfied(st, job) {
		st.Commit()
		ssn.AddEvent(api.Event{Type: "Preempted", Job: job.Name, Queue: job.Spec.Queue,
			Message: fmt.Sprintf("victims=%d ready=%d/%d", evicted, ready, job.Spec.MinAvailable)})
		return true
	}
	st.Discard()
	ssn.RecordUnschedulable(job.UID,
		fmt.Sprintf("preemption insufficient: evicted %d victims; %s", evicted, gangReason(st, job, ready, bound)))
	return false
}

func collectVictims(ssn *framework.Session, st *framework.Statement, job *api.Job) []*api.Pod {
	var out []*api.Pod
	for _, p := range st.Trial().Pods {
		if !p.Phase.OccupiesNode() || p.Queue != job.Spec.Queue || p.Job == job.UID {
			continue
		}
		if !st.Preemptable(priorityPod(job), p) {
			continue
		}
		out = append(out, p)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Priority != out[j].Priority {
			return out[i].Priority < out[j].Priority
		}
		if out[i].CreationTick != out[j].CreationTick {
			return out[i].CreationTick > out[j].CreationTick
		}
		return out[i].Name < out[j].Name
	})
	return out
}

// priorityPod represents the preemptor job for plugin questions that inspect a
// pod rather than a job. Queue and priority are the only fields the priority
// and gang plugins read.
func priorityPod(job *api.Job) *api.Pod {
	return &api.Pod{Job: job.UID, Queue: job.Spec.Queue, Priority: job.Spec.Priority}
}
