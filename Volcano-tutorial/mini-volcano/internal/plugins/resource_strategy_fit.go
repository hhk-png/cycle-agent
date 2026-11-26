package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// ResourceStrategyFit is a compact version of Volcano's v1.13+ plugin. The real
// implementation evaluates multiple resource strategies (for example, pack GPU
// while spreading CPU); MiniVolcano exposes one strategy per plugin instance.
type ResourceStrategyFit struct {
	strategy string
	resource string
	weight   float64
}

func newResourceStrategyFit(args map[string]string) (framework.Plugin, error) {
	strategy := stringArg(args, "strategy", "binpack")
	if strategy != "binpack" && strategy != "spread" {
		return nil, &argError{key: "strategy", value: strategy, want: "binpack or spread"}
	}
	w, err := floatArg(args, "weight", 1.0)
	if err != nil {
		return nil, err
	}
	return &ResourceStrategyFit{
		strategy: strategy,
		resource: stringArg(args, "resource", ""),
		weight:   w,
	}, nil
}

func (r *ResourceStrategyFit) Name() string { return "resource-strategy-fit" }

func (r *ResourceStrategyFit) OnSessionOpen(ssn *framework.Session) {
	ssn.AddNodeOrderFn("resource-strategy-fit", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		if r.weight == 0 {
			return 0, nil
		}
		ratio := 0.0
		if r.resource != "" {
			cap := node.Node.Allocatable[r.resource]
			if cap > 0 {
				ratio = clamp01(float64(node.Node.Allocated[r.resource]+pod.Requests[r.resource]) / float64(cap))
			}
		} else {
			for k, cap := range node.Node.Allocatable {
				if cap <= 0 {
					continue
				}
				v := clamp01(float64(node.Node.Allocated[k]+pod.Requests[k]) / float64(cap))
				if v > ratio {
					ratio = v
				}
			}
		}
		if r.strategy == "spread" {
			ratio = 1 - ratio
		}
		return r.weight * 10 * ratio, nil
	})
}

func (r *ResourceStrategyFit) OnSessionClose(ssn *framework.Session) {}
