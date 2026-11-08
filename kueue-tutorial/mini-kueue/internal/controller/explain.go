package controller

import (
	"fmt"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/scheduler"
)

// WorkloadExplanation combines the scheduler's admission diagnosis with the
// controller-visible lifecycle state. The scheduler answers "can this fit?";
// the controller answers "is anything else holding it back?".
type WorkloadExplanation struct {
	scheduler.Explanation

	Namespace       string
	Name            string
	Queue           string
	State           api.WorkloadState
	Active          bool
	RequeueAt       int64
	Requeues        int32
	Evictions       int32
	AdmissionGates  []string
	Conditions      []api.Condition
	AdmissionChecks []api.AdmissionCheckState
}

// Explain returns a structured explanation for one workload. It is read-only
// and safe to call between ticks. The second result is false when the
// workload does not exist.
func (e *Engine) Explain(namespace, name string) (WorkloadExplanation, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()

	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return WorkloadExplanation{}, false
	}
	out := WorkloadExplanation{
		Namespace:       namespace,
		Name:            name,
		Queue:           w.Spec.QueueName,
		State:           w.Status.State,
		Active:          w.Spec.Active,
		RequeueAt:       w.Status.RequeueAt,
		Requeues:        w.Status.Requeues,
		Evictions:       w.Status.Evictions,
		AdmissionGates:  append([]string(nil), w.Spec.AdmissionGates...),
		Conditions:      append([]api.Condition(nil), w.Status.Conditions...),
		AdmissionChecks: append([]api.AdmissionCheckState(nil), w.Status.AdmissionChecks...),
	}
	out.Workload = w.Key()

	lq, lqOK := e.Store.GetLocalQueue(w.Namespace, w.Spec.QueueName)
	if !lqOK {
		out.Reason = "LocalQueueNotFound"
		out.Detail = fmt.Sprintf("LocalQueue %s/%s does not exist", w.Namespace, w.Spec.QueueName)
		return out, true
	}
	cq, cqOK := e.Store.GetClusterQueue(lq.ClusterQueue)
	if !cqOK {
		out.ClusterQueue = lq.ClusterQueue
		out.Reason = "ClusterQueueNotFound"
		out.Detail = fmt.Sprintf("ClusterQueue %q referenced by LocalQueue %s/%s does not exist",
			lq.ClusterQueue, w.Namespace, w.Spec.QueueName)
		return out, true
	}

	out.Explanation = e.Scheduler.Explain(w, cq)

	// Controller-level blockers take precedence in the human-facing verdict:
	// a workload can be perfectly admissible and still never be scheduled.
	switch {
	case !w.Spec.Active:
		out.Reason = "Inactive"
		out.Detail = "workload is inactive (stopped by an operator)"
	case len(w.Spec.AdmissionGates) > 0:
		out.Reason = "AdmissionGated"
		out.Detail = fmt.Sprintf("waiting for admission gates: %v", w.Spec.AdmissionGates)
	case cq.Spec.StopPolicy != "" && cq.Spec.StopPolicy != api.StopNone:
		out.Reason = "ClusterQueueStopped"
		out.Detail = fmt.Sprintf("ClusterQueue stopPolicy=%s", cq.Spec.StopPolicy)
	case lq.StopPolicy != "" && lq.StopPolicy != api.StopNone:
		out.Reason = "LocalQueueStopped"
		out.Detail = fmt.Sprintf("LocalQueue stopPolicy=%s", lq.StopPolicy)
	case w.Status.State == api.StateAdmitted:
		out.Reason = "Admitted"
		out.Detail = "workload is admitted"
	case w.Status.State == api.StateQuotaReserved:
		if check, ok := firstPendingCheck(w.Status.AdmissionChecks); ok {
			out.Reason = "AdmissionCheck" + check.State
			out.Detail = fmt.Sprintf("quota is reserved; AdmissionCheck %q is %s", check.Name, check.State)
		} else {
			out.Reason = "PendingAdmission"
			out.Detail = "quota is reserved; waiting for the admission checks to become Ready"
		}
	case w.Status.State == api.StateFinished:
		out.Reason = "Finished"
		out.Detail = "workload has finished"
	}
	return out, true
}

func firstPendingCheck(checks []api.AdmissionCheckState) (api.AdmissionCheckState, bool) {
	for _, check := range checks {
		if check.State != "Ready" {
			return check, true
		}
	}
	return api.AdmissionCheckState{}, false
}
