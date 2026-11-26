package actions

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Reclaim enforces queue shares across queues. A queue below its deserved
// share may reclaim pods from queues that are over their share, stopping at the
// victim queue's deserved line so guarantees are never violated.
type Reclaim struct{}

func (a *Reclaim) Name() string { return "reclaim" }

func (a *Reclaim) Execute(ssn *framework.Session) error {
	queues := ssn.Queues()
	sort.SliceStable(queues, func(i, j int) bool { return ssn.QueueOrder(queues[i], queues[j]) < 0 })
	for _, q := range queues {
		if q.State() != api.QueueOpen || !underServed(ssn, q) {
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
			if tryReclaim(ssn, q, job) {
				break
			}
		}
	}
	return nil
}

func underServed(ssn *framework.Session, q *api.Queue) bool {
	if q.Status.Deserved.IsZero() {
		return false
	}
	used := ssn.Cache.QueueAllocated(q.Name)
	return used.LessEqual(q.Status.Deserved) && !used.Equal(q.Status.Deserved)
}

func tryReclaim(ssn *framework.Session, target *api.Queue, job *api.Job) bool {
	pending := ssn.PendingPods(job.UID)
	sort.SliceStable(pending, func(i, j int) bool { return ssn.TaskOrder(job, pending[i], pending[j]) < 0 })

	st := ssn.NewStatement()
	victims := collectReclaimVictims(ssn, st, target, job)
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
			if !victim.Phase.OccupiesNode() || victim.Queue == target.Name {
				continue
			}
			vq := ssn.Queue(victim.Queue)
			if vq == nil {
				continue
			}
			// Stop once the victim queue is no longer over its deserved
			// share. This is the "stop at the line" check; the separate
			// guarantee check below handles whole-pod overshoot.
			if !st.Overused(vq) {
				continue
			}
			if !st.Reclaimable(vq, victim) {
				continue
			}
			if victim.Priority > job.Spec.Priority {
				continue
			}
			// Reclaim may overshoot deserved because it evicts whole pods, but
			// it must never push the victim queue below its guarantee. Checking
			// "currently overused" is not enough: evicting one large pod can
			// take a queue from 8 GPU to 0 when its guarantee is 4.
			post := st.Trial().QueueAllocated(vq.Name).Sub(victim.Requests)
			if !vq.Spec.Guarantee.LessEqual(post) {
				continue
			}
			if err := st.Evict(victim.UID, "reclaimed for under-served queue "+target.Name); err != nil {
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
			"reclaim found no reclaimable victims (queue share, priority, gang or PDB protection)")
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
		ssn.AddEvent(api.Event{Type: "Reclaimed", Job: job.Name, Queue: target.Name,
			Message: fmt.Sprintf("victims=%d ready=%d/%d", evicted, ready, job.Spec.MinAvailable)})
		return true
	}
	st.Discard()
	ssn.RecordUnschedulable(job.UID,
		fmt.Sprintf("reclaim insufficient: evicted %d victims; %s", evicted, gangReason(st, job, ready, bound)))
	return false
}

func collectReclaimVictims(ssn *framework.Session, st *framework.Statement, target *api.Queue, job *api.Job) []*api.Pod {
	var out []*api.Pod
	for _, p := range st.Trial().Pods {
		if !p.Phase.OccupiesNode() || p.Queue == target.Name {
			continue
		}
		vq := ssn.Queue(p.Queue)
		if vq == nil || !st.Overused(vq) || !vq.Reclaimable() {
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
