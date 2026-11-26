package plugins

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// NUMA implements NUMA-aware placement. The cache assigns a specific NUMA node
// on bind; this plugin makes the constraint visible during predicate evaluation
// and adds a small score for the most tightly fitting NUMA node.
type NUMA struct{}

func newNUMA(args map[string]string) (framework.Plugin, error) { return &NUMA{}, nil }

func (n *NUMA) Name() string { return "numa-aware" }

func (n *NUMA) OnSessionOpen(ssn *framework.Session) {
	ssn.AddPredicateFn("numa-aware", func(pod *api.Pod, node *cache.NodeState) error {
		if len(node.Node.NUMANodes) == 0 {
			return nil
		}
		for _, nn := range node.Node.NUMANodes {
			free := nn.Allocatable.Sub(nn.Allocated)
			if pod.Requests.WithinLimits(free) {
				return nil
			}
		}
		return fmt.Errorf("node %s: no single NUMA node can satisfy %s", node.Node.Name, pod.Requests)
	})

	ssn.AddNodeOrderFn("numa-aware", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		if len(node.Node.NUMANodes) == 0 {
			return 0, nil
		}
		bestWaste := int64(1 << 62)
		for _, nn := range node.Node.NUMANodes {
			free := nn.Allocatable.Sub(nn.Allocated)
			if !pod.Requests.WithinLimits(free) {
				continue
			}
			waste := free[api.CPU] - pod.Requests[api.CPU]
			if waste < bestWaste {
				bestWaste = waste
			}
		}
		if bestWaste == int64(1<<62) {
			return 0, nil
		}
		return 1, nil
	})
}

func (n *NUMA) OnSessionClose(ssn *framework.Session) {}
