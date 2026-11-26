package controller

import (
	"example.com/minivolcano/internal/api"
)

// syncFlows creates jobs whose dependencies have completed. This is a tiny
// version of Volcano JobFlow: a DAG of VolcanoJobs with completion edges.
func (m *Manager) syncFlows(now int64) {
	for _, f := range m.Flows {
		if f.Created == nil {
			f.Created = map[string]string{}
		}
		progress := true
		for progress {
			progress = false
			for _, step := range f.Steps {
				if _, ok := f.Created[step.Name]; ok {
					continue
				}
				if !m.depsComplete(f, step) {
					continue
				}
				namespace := f.Namespace
				if namespace == "" {
					namespace = "default"
				}
				job := &api.Job{
					Name:         f.Name + "-" + step.Name,
					Namespace:    namespace,
					Spec:         step.Spec,
					CreationTick: now,
				}
				if err := m.Cache.SubmitJob(job); err != nil {
					m.Emit(api.Event{Type: "FlowError", Job: job.Name, Message: err.Error()})
					continue
				}
				f.Created[step.Name] = job.UID
				progress = true
				m.Emit(api.Event{Type: "FlowStep", Job: job.Name, Queue: job.Spec.Queue,
					Message: "started after dependencies completed"})
			}
		}
	}
}

func (m *Manager) depsComplete(f *api.JobFlow, step api.JobFlowStep) bool {
	for _, dep := range step.DependsOn {
		uid, ok := f.Created[dep]
		if !ok {
			return false
		}
		job := m.Cache.Jobs[uid]
		if job == nil || job.Status.Phase != api.JobCompleted {
			return false
		}
	}
	return true
}
