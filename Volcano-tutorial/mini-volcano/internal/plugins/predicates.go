package plugins

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// Predicates implements the hard filters that must pass before scoring:
// taints/tolerations and nodeSelector. Resource fit is enforced by the session
// itself so no plugin can accidentally overcommit a node.
type Predicates struct{}

func newPredicates(args map[string]string) (framework.Plugin, error) {
	return &Predicates{}, nil
}

func (p *Predicates) Name() string { return "predicates" }

func (p *Predicates) OnSessionOpen(ssn *framework.Session) {
	ssn.AddPredicateFn("predicates", func(pod *api.Pod, node *cache.NodeState) error {
		if node.Node.Unschedulable {
			return fmt.Errorf("node %s is unschedulable", node.Node.Name)
		}
		for k, v := range pod.NodeSelector {
			if node.Node.Labels[k] != v {
				return fmt.Errorf("node %s does not match selector %s=%s", node.Node.Name, k, v)
			}
		}
		for _, taint := range node.Node.Taints {
			if taint.Effect != "NoSchedule" && taint.Effect != "NoExecute" {
				continue
			}
			if !tolerated(pod.Tolerations, taint) {
				return fmt.Errorf("node %s has untolerated taint %s=%s:%s", node.Node.Name, taint.Key, taint.Value, taint.Effect)
			}
		}
		return nil
	})
}

func (p *Predicates) OnSessionClose(ssn *framework.Session) {}

func tolerated(tols []api.Toleration, taint api.Taint) bool {
	for _, tol := range tols {
		if tol.Effect != "" && tol.Effect != taint.Effect {
			continue
		}
		if tol.Operator == "Exists" {
			if tol.Key == "" || tol.Key == taint.Key {
				return true
			}
			continue
		}
		if tol.Key == taint.Key && tol.Value == taint.Value {
			return true
		}
	}
	return false
}
