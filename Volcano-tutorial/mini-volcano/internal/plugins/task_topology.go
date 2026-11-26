package plugins

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// TaskTopology enforces affinity/anti-affinity between task roles of the same
// job. It is the MiniVolcano analogue of Volcano's task-topology plugin:
// ps/worker co-location, or spreading inference replicas across nodes.
type TaskTopology struct{}

func newTaskTopology(args map[string]string) (framework.Plugin, error) {
	return &TaskTopology{}, nil
}

func (t *TaskTopology) Name() string { return "task-topology" }

func (t *TaskTopology) OnSessionOpen(ssn *framework.Session) {
	ssn.AddPredicateFn("task-topology", func(pod *api.Pod, node *cache.NodeState) error {
		job := ssn.JobByUID(pod.Job)
		if job == nil {
			return nil
		}
		peers := jobPeers(ssn, pod, node)
		for _, spec := range job.Spec.TaskTopology {
			if !containsString(spec.TaskNames, pod.Task) {
				continue
			}
			switch spec.Policy {
			case "anti-affinity":
				for _, peer := range peers {
					if containsString(spec.TaskNames, peer.Task) {
						return fmt.Errorf("task-topology anti-affinity: task %s conflicts with %s on node %s",
							pod.Task, peer.Task, node.Node.Name)
					}
				}
			case "affinity":
				// If peers already exist elsewhere, this role must join them.
				allPeers := allJobPeers(ssn, pod, spec.TaskNames)
				if len(allPeers) > 0 && len(peers) == 0 {
					return fmt.Errorf("task-topology affinity: task %s must co-locate with %v",
						pod.Task, spec.TaskNames)
				}
			}
		}
		return nil
	})

	ssn.AddNodeOrderFn("task-topology", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		job := ssn.JobByUID(pod.Job)
		if job == nil {
			return 0, nil
		}
		peers := jobPeers(ssn, pod, node)
		if len(peers) == 0 {
			return 0, nil
		}
		for _, spec := range job.Spec.TaskTopology {
			if !containsString(spec.TaskNames, pod.Task) {
				continue
			}
			for _, peer := range peers {
				if containsString(spec.TaskNames, peer.Task) {
					if spec.Policy == "affinity" {
						return 3, nil
					}
					return -3, nil
				}
			}
		}
		return 0, nil
	})
}

func (t *TaskTopology) OnSessionClose(ssn *framework.Session) {}

func jobPeers(ssn *framework.Session, pod *api.Pod, node *cache.NodeState) []*api.Pod {
	var out []*api.Pod
	for _, peer := range node.Pods {
		if peer.Job == pod.Job && peer.UID != pod.UID && peer.Phase.OccupiesNode() {
			out = append(out, peer)
		}
	}
	return out
}

func allJobPeers(ssn *framework.Session, pod *api.Pod, tasks []string) []*api.Pod {
	var out []*api.Pod
	for _, peer := range ssn.Cache.Pods {
		if peer.Job != pod.Job || peer.UID == pod.UID || !peer.Phase.OccupiesNode() {
			continue
		}
		if containsString(tasks, peer.Task) {
			out = append(out, peer)
		}
	}
	return out
}

func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}
