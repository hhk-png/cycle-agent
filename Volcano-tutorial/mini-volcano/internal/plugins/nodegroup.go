package plugins

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// NodeGroup restricts a queue to a named set of nodes and optionally caps how
// much of each group the queue may consume. It mirrors the v1.16 nodegroup
// plugin at a semantic level: real Volcano reads queue annotations, MiniVolcano
// uses typed queue fields.
//
// The predicate runs against the statement's trial cache, so a queue cannot
// overshoot a per-group cap by binding several pods in one allocation round.
type NodeGroup struct{}

func newNodeGroup(args map[string]string) (framework.Plugin, error) {
	return &NodeGroup{}, nil
}

func (p *NodeGroup) Name() string { return "nodegroup" }

func (p *NodeGroup) OnSessionOpen(ssn *framework.Session) {
	ssn.AddPredicateFn("nodegroup", func(pod *api.Pod, node *cache.NodeState) error {
		queue := ssn.Queue(pod.Queue)
		if queue == nil || queue.Spec.NodeGroup == nil {
			return nil
		}
		group := node.Node.Labels[api.NodeGroupLabel]
		if group == "" {
			return fmt.Errorf("node %s has no %s label", node.Node.Name, api.NodeGroupLabel)
		}
		if len(queue.Spec.NodeGroup.Names) > 0 && !inGroupList(queue.Spec.NodeGroup.Names, group) {
			return fmt.Errorf("queue %s is limited to node groups %v, node %s is in %q",
				queue.Name, queue.Spec.NodeGroup.Names, node.Node.Name, group)
		}
		if cap, ok := queue.Spec.NodeGroup.Capability[group]; ok {
			used := ssn.Cache.QueueNodeGroupAllocated(queue.Name, group)
			if !used.Add(pod.Requests).WithinLimits(cap) {
				return fmt.Errorf("queue %s node group %s cap %s exceeded: used %s + pod %s",
					queue.Name, group, cap, used, pod.Requests)
			}
		}
		return nil
	})
}

func (p *NodeGroup) OnSessionClose(ssn *framework.Session) {}

func inGroupList(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}
