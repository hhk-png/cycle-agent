// Package controller implements the control-plane half of MiniVolcano: job
// materialisation, pod lifecycle, VolcanoJob policies, PodGroup/Queue status,
// CronJob and a small JobFlow DAG.
package controller

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/metrics"
)

type Manager struct {
	Cache    *cache.Cache
	Metrics  *metrics.Registry
	Now      int64
	Failures map[string]int64
	Fired    map[string]bool

	CronJobs []*api.CronJob
	Flows    []*api.JobFlow
	Events   []api.Event
}

func New(c *cache.Cache, reg *metrics.Registry) *Manager {
	if reg == nil {
		reg = metrics.New()
	}
	return &Manager{
		Cache:    c,
		Metrics:  reg,
		Failures: map[string]int64{},
		Fired:    map[string]bool{},
	}
}

func (m *Manager) Emit(e api.Event) {
	if e.Tick == 0 {
		e.Tick = m.Now
	}
	m.Events = append(m.Events, e)
}

// ScheduleFailure injects a deterministic pod failure at a given tick. It is
// used by retry and SLA scenarios; production failures come from kubelet.
func (m *Manager) ScheduleFailure(podUID string, tick int64) {
	m.Failures[podUID] = tick
}

func (m *Manager) AddCron(c *api.CronJob) error {
	if c == nil || c.Name == "" {
		return fmt.Errorf("cronjob name is required")
	}
	if c.ScheduleTicks <= 0 {
		return fmt.Errorf("cronjob %s: scheduleTicks must be > 0", c.Name)
	}
	if c.NextTick == 0 {
		c.NextTick = c.ScheduleTicks
	}
	if c.ConcurrencyPolicy == "" {
		c.ConcurrencyPolicy = "Allow"
	}
	m.CronJobs = append(m.CronJobs, c)
	return nil
}

func (m *Manager) AddFlow(f *api.JobFlow) error {
	if f == nil || f.Name == "" {
		return fmt.Errorf("jobflow name is required")
	}
	if f.Created == nil {
		f.Created = map[string]string{}
	}
	names := map[string]bool{}
	for _, s := range f.Steps {
		if s.Name == "" {
			return fmt.Errorf("jobflow %s: step name is required", f.Name)
		}
		if names[s.Name] {
			return fmt.Errorf("jobflow %s: duplicate step %s", f.Name, s.Name)
		}
		names[s.Name] = true
	}
	for _, s := range f.Steps {
		for _, d := range s.DependsOn {
			if !names[d] {
				return fmt.Errorf("jobflow %s: step %s depends on unknown step %s", f.Name, s.Name, d)
			}
		}
	}
	m.Flows = append(m.Flows, f)
	return nil
}

// Sync runs one controller reconcile. It is intentionally ordered: timers and
// DAG dependencies create work, pods advance, policies fire, then status is
// recomputed from the observed pod states.
func (m *Manager) Sync(now int64) {
	m.Now = now
	m.Cache.Now = now
	m.Metrics.Inc("minivolcano_controller_reconciles_total", "Controller reconcile loops", nil)

	m.syncCron(now)
	m.syncFlows(now)

	for _, job := range m.Cache.JobsSorted() {
		_ = m.Cache.MaterializeJob(job)
	}

	pods := make([]*api.Pod, 0, len(m.Cache.Pods))
	for _, p := range m.Cache.Pods {
		pods = append(pods, p)
	}
	sort.SliceStable(pods, func(i, j int) bool { return pods[i].UID < pods[j].UID })
	for _, p := range pods {
		m.advancePod(p, now)
	}
	for _, p := range pods {
		m.handlePodEvents(p, now)
	}
	m.recomputeStatuses(now)
}

func (m *Manager) advancePod(p *api.Pod, now int64) {
	job := m.Cache.Jobs[p.Job]
	if job == nil || job.Status.Phase.Terminal() {
		return
	}
	switch p.Phase {
	case api.PodAllocated:
		if now > p.ScheduleTick {
			p.Phase = api.PodRunning
			p.StartTick = now
			if job.Status.StartTick == 0 {
				job.Status.StartTick = now
			}
		}
	case api.PodRunning:
		if failAt, ok := m.Failures[p.UID]; ok && !m.Fired[p.UID] && now >= failAt {
			m.Fired[p.UID] = true
			if err := m.Cache.Finish(p.UID, false, now); err == nil {
				m.Emit(api.Event{Type: "PodFailed", Job: job.Name, Pod: p.Name, Node: p.NodeName,
					Queue: p.Queue, Message: fmt.Sprintf("injected failure at tick %d", now)})
				m.Metrics.Inc("minivolcano_pod_failures_total", "Pod failures", map[string]string{"job": job.Name})
			}
			return
		}
		if p.StartTick > 0 && now >= p.StartTick+m.taskDuration(job, p.Task) {
			if err := m.Cache.Finish(p.UID, true, now); err == nil {
				m.Emit(api.Event{Type: "PodSucceeded", Job: job.Name, Pod: p.Name, Node: p.NodeName, Queue: p.Queue})
				m.Metrics.Inc("minivolcano_pods_completed_total", "Pods completed", map[string]string{"job": job.Name})
			}
		}
	case api.PodPending:
		// Evicted pods are handled by the policy pass; pending normal pods wait
		// for the scheduler.
	}
}

func (m *Manager) taskDuration(job *api.Job, task string) int64 {
	for _, t := range job.Spec.Tasks {
		if t.Name == task {
			return api.DurationOr(t.Duration, 1)
		}
	}
	return 1
}

func (m *Manager) handlePodEvents(p *api.Pod, now int64) {
	job := m.Cache.Jobs[p.Job]
	if job == nil || job.Status.Phase.Terminal() {
		return
	}
	if p.PolicyHandled {
		return
	}
	if p.Phase == api.PodSucceeded {
		if m.taskCompleted(job, p.Task) {
			m.handleLifecycle(job, p, api.EventTaskCompleted, now)
		} else {
			p.PolicyHandled = true
		}
		return
	}
	switch {
	case p.Phase == api.PodFailed:
		m.handleLifecycle(job, p, api.EventPodFailed, now)
	case p.Phase == api.PodPending && p.Evicted:
		m.handleLifecycle(job, p, api.EventPodEvicted, now)
	}
}

func (m *Manager) taskCompleted(job *api.Job, task string) bool {
	for _, p := range m.Cache.JobPods(job.UID) {
		if p.Task == task && p.Phase != api.PodSucceeded {
			return false
		}
	}
	return true
}

func (m *Manager) handleLifecycle(job *api.Job, p *api.Pod, event api.LifecycleEvent, now int64) {
	policy, ok := m.policyFor(job, p.Task, event)
	if !ok {
		policy = m.defaultPolicy(job, event)
	}
	p.PolicyHandled = true
	switch policy.Action {
	case api.ActionRestartTask:
		if job.Status.Retries >= job.Spec.MaxRetry {
			m.failJob(job, fmt.Sprintf("maxRetry %d exceeded after %s", job.Spec.MaxRetry, event))
			return
		}
		if err := m.Cache.ResetForRestart(p.UID, now); err != nil {
			return
		}
		job.Status.Retries++
		job.Status.Phase = api.JobInqueue
		job.Status.Message = string(event) + " -> RestartTask"
		if pg := m.Cache.PodGroups[job.UID]; pg != nil {
			pg.Phase = api.PodGroupInqueue
		}
		m.Emit(api.Event{Type: "RestartTask", Job: job.Name, Pod: p.Name, Queue: p.Queue,
			Message: string(event)})
	case api.ActionRestartJob:
		m.restartJob(job, string(event), now)
	case api.ActionAbortJob, api.ActionTerminateJob:
		m.stopJob(job, api.JobAborted, string(event)+" -> "+string(policy.Action), now)
	case api.ActionTerminateTask:
		m.Emit(api.Event{Type: "TerminateTask", Job: job.Name, Pod: p.Name, Queue: p.Queue,
			Message: string(event)})
	case api.ActionCompleteJob:
		m.completeJob(job, string(event)+" -> CompleteJob", now)
	}
}

func (m *Manager) defaultPolicy(job *api.Job, event api.LifecycleEvent) api.LifecyclePolicy {
	switch event {
	case api.EventPodFailed:
		return api.LifecyclePolicy{Event: event, Action: api.ActionRestartTask}
	case api.EventPodEvicted:
		// Eviction is a scheduling decision, not a task failure. The pod stays
		// Pending and becomes eligible again; a job that wants stronger
		// semantics can opt into RestartJob/RestartTask explicitly.
		return api.LifecyclePolicy{Event: event, Action: ""}
	case api.EventJobUnknown:
		return api.LifecyclePolicy{Event: event, Action: api.ActionAbortJob}
	default:
		return api.LifecyclePolicy{Event: event, Action: ""}
	}
}

func (m *Manager) policyFor(job *api.Job, task string, event api.LifecycleEvent) (api.LifecyclePolicy, bool) {
	for _, t := range job.Spec.Tasks {
		if t.Name != task {
			continue
		}
		for _, p := range t.Policies {
			if p.Event == event {
				return p, true
			}
		}
	}
	for _, p := range job.Spec.Policies {
		if p.Event == event {
			return p, true
		}
	}
	return api.LifecyclePolicy{}, false
}

func (m *Manager) restartJob(job *api.Job, reason string, now int64) {
	if job.Status.Retries >= job.Spec.MaxRetry {
		m.failJob(job, fmt.Sprintf("maxRetry %d exceeded restarting job: %s", job.Spec.MaxRetry, reason))
		return
	}
	for _, p := range m.Cache.JobPods(job.UID) {
		_ = m.Cache.ResetForRestart(p.UID, now)
	}
	job.Status.Retries++
	job.Status.Phase = api.JobInqueue
	job.Status.Message = "RestartJob: " + reason
	if pg := m.Cache.PodGroups[job.UID]; pg != nil {
		pg.Phase = api.PodGroupInqueue
	}
	m.Metrics.Inc("minivolcano_job_restarts_total", "Job restarts", map[string]string{"job": job.Name})
	m.Emit(api.Event{Type: "RestartJob", Job: job.Name, Queue: job.Spec.Queue,
		Message: fmt.Sprintf("%s retries=%d", reason, job.Status.Retries)})
}

func (m *Manager) failJob(job *api.Job, reason string) {
	m.stopJob(job, api.JobFailed, reason, m.Now)
	m.Metrics.Inc("minivolcano_jobs_failed_total", "Jobs failed", nil)
}

func (m *Manager) stopJob(job *api.Job, phase api.JobPhase, reason string, now int64) {
	for _, p := range m.Cache.JobPods(job.UID) {
		if p.Phase.OccupiesNode() {
			_ = m.Cache.Finish(p.UID, false, now)
		}
		p.Message = reason
		p.PolicyHandled = true
	}
	job.Status.Phase = phase
	job.Status.FinishTick = now
	job.Status.Message = reason
	if pg := m.Cache.PodGroups[job.UID]; pg != nil {
		if phase == api.JobCompleted {
			pg.Phase = api.PodGroupCompleted
		} else {
			pg.Phase = api.PodGroupUnknown
		}
	}
	m.Emit(api.Event{Type: string(phase), Job: job.Name, Queue: job.Spec.Queue, Message: reason})
}

func (m *Manager) completeJob(job *api.Job, reason string, now int64) {
	if job.Status.Phase.Terminal() {
		return
	}
	// CompleteJob may fire while other replicas are still running (for
	// example TaskCompleted -> CompleteJob). Terminate them so they stop
	// holding node resources; otherwise a completed job leaks capacity.
	for _, p := range m.Cache.JobPods(job.UID) {
		if p.Phase.OccupiesNode() {
			_ = m.Cache.Finish(p.UID, true, now)
		} else if p.Phase == api.PodPending {
			// minSuccess may complete a job before every replica was ever
			// scheduled. Those replicas are terminated, not leaked as Pending.
			p.Phase = api.PodSucceeded
			p.FinishTick = now
		}
		p.Message = reason
		p.PolicyHandled = true
	}
	job.Status.Phase = api.JobCompleted
	job.Status.FinishTick = now
	job.Status.Message = reason
	job.Status.Running = 0
	job.Status.Succeeded = int32(m.Cache.SucceededCount(job.UID))
	job.Status.Failed = int32(m.Cache.FailedCount(job.UID))
	if pg := m.Cache.PodGroups[job.UID]; pg != nil {
		pg.Phase = api.PodGroupCompleted
		pg.Running = 0
		pg.Succeeded = job.Status.Succeeded
		pg.Failed = job.Status.Failed
	}
	m.Metrics.Inc("minivolcano_jobs_completed_total", "Jobs completed", map[string]string{"queue": job.Spec.Queue})
	m.Emit(api.Event{Type: "Completed", Job: job.Name, Queue: job.Spec.Queue, Message: reason})
}

func (m *Manager) recomputeStatuses(now int64) {
	for _, job := range m.Cache.JobsSorted() {
		m.recomputeJob(job, now)
	}
	for _, q := range m.Cache.QueuesSorted() {
		q.Status.Allocated = m.Cache.QueueAllocated(q.Name)
		q.Status.Inqueue, q.Status.Pending, q.Status.Running = 0, 0, 0
		for _, job := range m.Cache.Jobs {
			if job.Spec.Queue != q.Name {
				continue
			}
			switch job.Status.Phase {
			case api.JobInqueue:
				q.Status.Inqueue++
			case api.JobPending:
				q.Status.Pending++
			case api.JobRunning:
				q.Status.Running++
			}
		}
	}
}

func (m *Manager) recomputeJob(job *api.Job, now int64) {
	pods := m.Cache.JobPods(job.UID)
	var running, succeeded, failed int
	for _, p := range pods {
		switch p.Phase {
		case api.PodRunning:
			running++
		case api.PodSucceeded:
			succeeded++
		case api.PodFailed:
			failed++
		}
	}
	job.Status.Running = int32(running)
	job.Status.Succeeded = int32(succeeded)
	job.Status.Failed = int32(failed)

	if job.Status.Phase.Terminal() {
		return
	}
	if len(pods) > 0 && succeeded == len(pods) {
		m.completeJob(job, "all tasks succeeded", now)
		return
	}
	if job.Spec.MinSuccess != nil && *job.Spec.MinSuccess > 0 && succeeded >= int(*job.Spec.MinSuccess) {
		m.completeJob(job, fmt.Sprintf("minSuccess %d reached (%d succeeded)", *job.Spec.MinSuccess, succeeded), now)
		return
	}
	if len(pods) > 0 && succeeded+failed == len(pods) && failed > 0 && running == 0 {
		// No policy restarted the failure; the job cannot make progress.
		m.failJob(job, "all remaining pods are terminal with failures")
		return
	}
	ready := m.Cache.ReadyCount(job.UID)
	min := int(job.Spec.MinAvailable)
	if min == 0 {
		min = 1
	}
	if ready >= min && running > 0 && m.taskGatesReady(job) {
		job.Status.Phase = api.JobRunning
		if job.Status.StartTick == 0 {
			job.Status.StartTick = now
		}
	} else if job.Status.Phase == api.JobRunning && ready >= min {
		// Allocated tasks above min are still starting; remain Running.
	} else if pg := m.Cache.PodGroups[job.UID]; pg != nil && pg.Phase == api.PodGroupInqueue {
		job.Status.Phase = api.JobInqueue
	} else if job.Status.Phase != api.JobRestarting {
		job.Status.Phase = api.JobPending
	}
	m.recomputePodGroup(job)
}

func (m *Manager) taskGatesReady(job *api.Job) bool {
	if !job.RoleGatesActive() {
		return true
	}
	byTask := m.Cache.ReadyCountByTask(job.UID)
	for i := range job.Spec.Tasks {
		t := &job.Spec.Tasks[i]
		if want := t.EffectiveMinAvailable(); want > 0 && byTask[t.Name] < int(want) {
			return false
		}
	}
	return true
}

func (m *Manager) recomputePodGroup(job *api.Job) {
	pg := m.Cache.PodGroups[job.UID]
	if pg == nil {
		return
	}
	pg.Running = int32(m.Cache.RunningCount(job.UID))
	pg.Succeeded = int32(m.Cache.SucceededCount(job.UID))
	pg.Failed = int32(m.Cache.FailedCount(job.UID))
	switch job.Status.Phase {
	case api.JobCompleted:
		pg.Phase = api.PodGroupCompleted
	case api.JobFailed, api.JobAborted:
		pg.Phase = api.PodGroupUnknown
	case api.JobRunning:
		pg.Phase = api.PodGroupRunning
	case api.JobInqueue, api.JobRestarting:
		pg.Phase = api.PodGroupInqueue
	default:
		pg.Phase = api.PodGroupPending
	}
}
