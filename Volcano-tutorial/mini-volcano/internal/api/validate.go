package api

import (
	"fmt"
	"strings"
)

func validateResource(name string, r Resource) error {
	for k, v := range r {
		if v < 0 {
			return fmt.Errorf("%s: resource %q must be non-negative, got %d", name, k, v)
		}
		if strings.TrimSpace(k) == "" {
			return fmt.Errorf("%s: resource name must not be empty", name)
		}
	}
	return nil
}

// ValidateQueue checks structural invariants of a Queue. Cluster-level checks
// (parent existence, cycles) are performed by the simulator.
func ValidateQueue(q *Queue) error {
	if q == nil {
		return fmt.Errorf("queue is nil")
	}
	if q.Name == "" {
		return fmt.Errorf("queue name must not be empty")
	}
	if q.Spec.Weight < 0 {
		return fmt.Errorf("queue %q: weight must be >= 0", q.Name)
	}
	switch q.Spec.State {
	case "", QueueOpen, QueueClosed, QueueClosing, QueueUnknown:
	default:
		return fmt.Errorf("queue %q: invalid state %q", q.Name, q.Spec.State)
	}
	// Fixed order keeps the first error deterministic when several fields are
	// invalid (map iteration would make the message vary run to run).
	for _, field := range []struct {
		label string
		value Resource
	}{
		{"capability", q.Spec.Capability},
		{"deserved", q.Spec.Deserved},
		{"guarantee", q.Spec.Guarantee},
	} {
		if err := validateResource(fmt.Sprintf("queue %q %s", q.Name, field.label), field.value); err != nil {
			return err
		}
	}
	if q.Spec.Capability != nil && q.Spec.Guarantee != nil && !q.Spec.Guarantee.WithinLimits(q.Spec.Capability) {
		return fmt.Errorf("queue %q: guarantee %s exceeds capability %s", q.Name, q.Spec.Guarantee, q.Spec.Capability)
	}
	if q.Spec.Capability != nil && q.Spec.Deserved != nil && !q.Spec.Deserved.WithinLimits(q.Spec.Capability) {
		return fmt.Errorf("queue %q: deserved %s exceeds capability %s", q.Name, q.Spec.Deserved, q.Spec.Capability)
	}
	if ng := q.Spec.NodeGroup; ng != nil {
		if len(ng.Names) == 0 && len(ng.Capability) == 0 {
			return fmt.Errorf("queue %q: nodeGroup needs names or capability", q.Name)
		}
		seen := map[string]bool{}
		for _, name := range ng.Names {
			if strings.TrimSpace(name) == "" {
				return fmt.Errorf("queue %q: nodeGroup name must not be empty", q.Name)
			}
			if seen[name] {
				return fmt.Errorf("queue %q: duplicate nodeGroup %q", q.Name, name)
			}
			seen[name] = true
		}
		for group, cap := range ng.Capability {
			if strings.TrimSpace(group) == "" {
				return fmt.Errorf("queue %q: nodeGroup capability name must not be empty", q.Name)
			}
			if err := validateResource(fmt.Sprintf("queue %q nodeGroup %q capability", q.Name, group), cap); err != nil {
				return err
			}
		}
	}
	return nil
}

func (q *Queue) Normalize() {
	if q.Spec.Weight == 0 {
		q.Spec.Weight = 1
	}
	if q.Spec.State == "" {
		q.Spec.State = QueueOpen
	}
	if q.Status.State == "" {
		q.Status.State = q.Spec.State
	}
}

func ValidateTask(t *TaskSpec) error {
	if strings.TrimSpace(t.Name) == "" {
		return fmt.Errorf("task name must not be empty")
	}
	if t.Replicas < 0 {
		return fmt.Errorf("task %q: replicas must be >= 0", t.Name)
	}
	if t.MinAvailable != nil {
		if *t.MinAvailable < 0 {
			return fmt.Errorf("task %q: minAvailable must be >= 0", t.Name)
		}
		if *t.MinAvailable > t.Replicas {
			return fmt.Errorf("task %q: minAvailable %d exceeds replicas %d", t.Name, *t.MinAvailable, t.Replicas)
		}
	}
	if t.Duration < 0 {
		return fmt.Errorf("task %q: duration must be >= 0", t.Name)
	}
	for _, p := range t.Policies {
		if err := ValidatePolicy(p); err != nil {
			return fmt.Errorf("task %q: %w", t.Name, err)
		}
	}
	return validateResource(fmt.Sprintf("task %q requests", t.Name), t.Requests)
}

func ValidateJob(j *Job) error {
	if j == nil {
		return fmt.Errorf("job is nil")
	}
	if j.Name == "" || j.Namespace == "" {
		return fmt.Errorf("job name and namespace must not be empty")
	}
	if len(j.Spec.Tasks) == 0 {
		return fmt.Errorf("job %s: at least one task is required", j.Key())
	}
	if j.TotalReplicas() == 0 {
		return fmt.Errorf("job %s: total replicas must be > 0", j.Key())
	}
	if j.Spec.MinAvailable < 0 {
		return fmt.Errorf("job %s: minAvailable must be >= 0", j.Key())
	}
	if j.Spec.MinAvailable > j.TotalReplicas() {
		return fmt.Errorf("job %s: minAvailable %d exceeds total replicas %d", j.Key(), j.Spec.MinAvailable, j.TotalReplicas())
	}
	// A job-level minAvailable below the sum of task gates is *not* a schema
	// error in Volcano: the role gates are simply not effective. MiniVolcano
	// matches that behavior via Job.RoleGatesActive; nothing to reject here.
	if j.Spec.MinSuccess != nil {
		if *j.Spec.MinSuccess < 0 {
			return fmt.Errorf("job %s: minSuccess must be >= 0", j.Key())
		}
		if *j.Spec.MinSuccess > j.TotalReplicas() {
			return fmt.Errorf("job %s: minSuccess %d exceeds total replicas %d", j.Key(), *j.Spec.MinSuccess, j.TotalReplicas())
		}
	}
	if j.Spec.MaxRetry < 0 {
		return fmt.Errorf("job %s: maxRetry must be >= 0", j.Key())
	}
	seen := map[string]bool{}
	for i := range j.Spec.Tasks {
		t := &j.Spec.Tasks[i]
		if err := ValidateTask(t); err != nil {
			return fmt.Errorf("job %s: %w", j.Key(), err)
		}
		if seen[t.Name] {
			return fmt.Errorf("job %s: duplicate task name %q", j.Key(), t.Name)
		}
		seen[t.Name] = true
	}
	if j.Spec.NetworkTopology != nil {
		switch j.Spec.NetworkTopology.Mode {
		case "", "hard", "soft":
		default:
			return fmt.Errorf("job %s: networkTopology.mode must be hard or soft", j.Key())
		}
		if j.Spec.NetworkTopology.HighestTierAllowed < 0 {
			return fmt.Errorf("job %s: highestTierAllowed must be >= 0", j.Key())
		}
	}
	for _, tt := range j.Spec.TaskTopology {
		switch tt.Policy {
		case "affinity", "anti-affinity":
		default:
			return fmt.Errorf("job %s: taskTopology policy must be affinity or anti-affinity", j.Key())
		}
		for _, name := range tt.TaskNames {
			if !seen[name] {
				return fmt.Errorf("job %s: taskTopology references unknown task %q", j.Key(), name)
			}
		}
	}
	for _, p := range j.Spec.Policies {
		if err := ValidatePolicy(p); err != nil {
			return fmt.Errorf("job %s: %w", j.Key(), err)
		}
	}
	return nil
}

func ValidatePolicy(p LifecyclePolicy) error {
	switch p.Event {
	case EventPodFailed, EventPodEvicted, EventTaskCompleted, EventJobUnknown:
	default:
		return fmt.Errorf("invalid lifecycle event %q", p.Event)
	}
	switch p.Action {
	case ActionAbortJob, ActionRestartJob, ActionTerminateJob, ActionRestartTask, ActionTerminateTask, ActionCompleteJob:
	default:
		return fmt.Errorf("invalid lifecycle action %q", p.Action)
	}
	return nil
}

// MutateJob simulates the Volcano admission webhook: defaults schedulerName,
// queue and minAvailable.
func MutateJob(j *Job) {
	if j.Spec.SchedulerName == "" {
		j.Spec.SchedulerName = "volcano"
	}
	if j.Spec.Queue == "" {
		j.Spec.Queue = "default"
	}
	if !j.Spec.MinAvailableSet {
		// v1.16 defaults the job gate to the sum of task-level gates, not to
		// the raw replica count. When no task declares an explicit gate, the
		// sum is 0, so fall back to the replica count to preserve the
		// traditional "all replicas" default.
		j.Spec.MinAvailable = j.SumTaskMinAvailable()
		if j.Spec.MinAvailable == 0 {
			j.Spec.MinAvailable = j.TotalReplicas()
		}
		j.Spec.MinAvailableSet = true
	}
	if j.PodGroupName == "" {
		j.PodGroupName = j.Name
	}
	if j.UID == "" {
		j.UID = j.Namespace + "/" + j.Name
	}
}

func ValidatePodGroup(pg *PodGroup) error {
	if pg == nil {
		return fmt.Errorf("podgroup is nil")
	}
	if pg.MinMember < 0 {
		return fmt.Errorf("podgroup %s/%s: minMember must be >= 0", pg.Namespace, pg.Name)
	}
	return validateResource("podgroup "+pg.Namespace+"/"+pg.Name, pg.MinResources)
}

func ValidateReservation(r *Reservation) error {
	if r == nil {
		return fmt.Errorf("reservation is nil")
	}
	if r.Name == "" {
		return fmt.Errorf("reservation name must not be empty")
	}
	if r.Queue == "" {
		return fmt.Errorf("reservation %q: queue is required", r.Name)
	}
	if r.StartTick < 0 || r.EndTick <= r.StartTick {
		return fmt.Errorf("reservation %q: invalid window [%d,%d)", r.Name, r.StartTick, r.EndTick)
	}
	if r.Resources.IsZero() {
		return fmt.Errorf("reservation %q: resources must be non-zero", r.Name)
	}
	return validateResource("reservation "+r.Name, r.Resources)
}
