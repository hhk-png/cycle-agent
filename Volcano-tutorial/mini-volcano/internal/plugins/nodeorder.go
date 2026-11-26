package plugins

import (
	"math"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// NodeOrder scores nodes after predicates. The score is a sum of independent
// 0..10 components, matching Volcano's plugin composition model where each
// NodeOrderFn contributes to the total.
type NodeOrder struct {
	leastRequestedWeight float64
	balancedWeight       float64
	podCountWeight       float64
}

func newNodeOrder(args map[string]string) (framework.Plugin, error) {
	lr, err := floatArg(args, "leastRequestedWeight", 1.0)
	if err != nil {
		return nil, err
	}
	bal, err := floatArg(args, "balancedWeight", 1.0)
	if err != nil {
		return nil, err
	}
	pc, err := floatArg(args, "podCountWeight", 0.0)
	if err != nil {
		return nil, err
	}
	return &NodeOrder{leastRequestedWeight: lr, balancedWeight: bal, podCountWeight: pc}, nil
}

func (n *NodeOrder) Name() string { return "nodeorder" }

func (n *NodeOrder) OnSessionOpen(ssn *framework.Session) {
	ssn.AddNodeOrderFn("nodeorder", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		return n.score(node), nil
	})
}

func (n *NodeOrder) OnSessionClose(ssn *framework.Session) {}

func (n *NodeOrder) score(node *cache.NodeState) float64 {
	total := node.Node.Allocatable
	used := node.Node.Allocated
	var freeSum, utilSum, utilSqSum float64
	var dims int
	for _, k := range total.Keys() {
		cap := total[k]
		if cap <= 0 {
			continue
		}
		util := float64(used[k]) / float64(cap)
		if util > 1 {
			util = 1
		}
		freeSum += 1 - util
		utilSum += util
		utilSqSum += util * util
		dims++
	}
	if dims == 0 {
		return 0
	}
	least := 10 * freeSum / float64(dims)
	mean := utilSum / float64(dims)
	variance := utilSqSum/float64(dims) - mean*mean
	if variance < 0 {
		variance = 0
	}
	balanced := 10 * (1 - math.Sqrt(variance))
	podCount := 10.0
	if len(node.Pods) > 0 {
		podCount = 10 * (1 - math.Min(float64(len(node.Pods))/100.0, 1))
	}
	return n.leastRequestedWeight*least + n.balancedWeight*balanced + n.podCountWeight*podCount
}
