package actions

import (
	"sort"
	"strconv"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Enqueue moves Pending jobs into Inqueue. It asks the plugins two questions:
// is the job admissible at all (JobValid), and does its queue/overcommit policy
// allow it right now (JobEnqueueable)?
//
// Volcano's enqueue action also has an optional idle-resource gate. MiniVolcano
// keeps that behind Config.EnqueueIdleCheck because several realistic
// preemption scenarios require a job to enter the queue before space exists.
type Enqueue struct{}

func (e *Enqueue) Name() string { return "enqueue" }

func (e *Enqueue) Execute(ssn *framework.Session) error {
	queues := ssn.Queues()
	sort.SliceStable(queues, func(i, j int) bool { return ssn.QueueOrder(queues[i], queues[j]) < 0 })
	for _, q := range queues {
		jobs := ssn.JobsOfQueue(q.Name)
		sort.SliceStable(jobs, func(i, j int) bool { return ssn.JobOrder(jobs[i], jobs[j]) < 0 })
		for _, job := range jobs {
			if job.Status.Phase.Terminal() || job.Status.Phase == api.JobRunning ||
				job.Status.Phase == api.JobInqueue || job.Status.Phase == api.JobRestarting {
				continue
			}
			pg := ssn.Cache.PodGroups[job.UID]
			if pg == nil {
				continue
			}
			if q.State() != api.QueueOpen {
				ssn.RecordUnschedulable(job.UID, "queue "+q.Name+" is "+string(q.State()))
				continue
			}
			if err := ssn.JobValid(job); err != nil {
				job.Status.Message = err.Error()
				ssn.RecordUnschedulable(job.UID, "invalid job: "+err.Error())
				continue
			}
			if !ssn.JobEnqueueable(job) {
				ssn.RecordUnschedulable(job.UID, "queue admission rejected (capability, hierarchy or overcommit)")
				continue
			}
			if ssn.Config.EnqueueIdleCheck && !job.MinResources().LessEqual(idleFor(ssn, q.Name, job.UID)) {
				ssn.RecordUnschedulable(job.UID, "insufficient idle resources for minResources "+job.MinResources().String())
				continue
			}
			pg.Phase = api.PodGroupInqueue
			job.Status.Phase = api.JobInqueue
			job.Status.Message = ""
			ssn.AddEvent(api.Event{Type: "Inqueue", Job: job.Name, Queue: q.Name,
				Message: "minAvailable=" + strconv.FormatInt(int64(job.Spec.MinAvailable), 10) + " minResources=" + job.MinResources().String()})
		}
	}
	return nil
}

func idleFor(ssn *framework.Session, queue, jobUID string) api.Resource {
	out := api.Resource{}
	for _, n := range ssn.Nodes() {
		out = out.Add(ssn.Cache.IdleFor(n.Node.Name, queue, jobUID, ssn.Now))
	}
	return out
}
