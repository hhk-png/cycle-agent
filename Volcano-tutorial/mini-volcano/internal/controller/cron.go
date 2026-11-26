package controller

import (
	"fmt"

	"example.com/minivolcano/internal/api"
)

// syncCron implements a deterministic subset of the v1.13 Cron VolcanoJob:
// interval scheduling, Allow/Forbid/Replace concurrency and history GC.
func (m *Manager) syncCron(now int64) {
	for _, c := range m.CronJobs {
		if now < c.NextTick {
			continue
		}
		if c.LastJob != "" {
			last := m.Cache.Jobs[c.LastJob]
			if last != nil && !last.Status.Phase.Terminal() {
				switch c.ConcurrencyPolicy {
				case "Forbid":
					m.Emit(api.Event{Type: "CronSkipped", Job: last.Name, Queue: last.Spec.Queue,
						Message: "previous run still active (Forbid)"})
					c.NextTick = now + c.ScheduleTicks
					continue
				case "Replace":
					m.stopJob(last, api.JobAborted, "replaced by CronJob "+c.Name, now)
				}
			}
		}
		name := fmt.Sprintf("%s-%d", c.Name, now)
		job := &api.Job{
			Name:         name,
			Namespace:    c.Namespace,
			Spec:         c.Spec,
			CreationTick: now,
		}
		if job.Namespace == "" {
			job.Namespace = "default"
		}
		if err := m.Cache.SubmitJob(job); err != nil {
			m.Emit(api.Event{Type: "CronError", Job: name, Message: err.Error()})
			c.NextTick = now + c.ScheduleTicks
			continue
		}
		c.LastJob = job.UID
		c.Created = append(c.Created, job.UID)
		c.NextTick = now + c.ScheduleTicks
		m.Emit(api.Event{Type: "CronCreated", Job: job.Name, Queue: job.Spec.Queue,
			Message: fmt.Sprintf("next tick %d", c.NextTick)})
		m.gcCronHistory(c)
	}
}

func (m *Manager) gcCronHistory(c *api.CronJob) {
	if c.HistoryLimit <= 0 {
		return
	}
	for len(c.Created) > c.HistoryLimit {
		uid := c.Created[0]
		job := m.Cache.Jobs[uid]
		if job != nil && !job.Status.Phase.Terminal() {
			return
		}
		m.deleteJob(uid)
		c.Created = c.Created[1:]
	}
}

func (m *Manager) deleteJob(uid string) {
	job := m.Cache.Jobs[uid]
	if job == nil {
		return
	}
	for key, p := range m.Cache.Pods {
		if p.Job != uid {
			continue
		}
		// GC must release resources even if a leftover pod is still occupying
		// a node; deleting the map entries alone would orphan the accounting.
		if p.Phase.OccupiesNode() {
			_ = m.Cache.Finish(p.UID, true, m.Now)
		}
		delete(m.Cache.Pods, key)
	}
	delete(m.Cache.PodGroups, uid)
	delete(m.Cache.Jobs, uid)
}
