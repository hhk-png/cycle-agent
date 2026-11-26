package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// Binpack prefers nodes that are already busy, reducing fragmentation. With
// mode=spread the sign is flipped, which makes the plugin useful for balancing
// inference replicas across failure domains.
type Binpack struct {
	weight   float64
	mode     string
	resource string
}

func newBinpack(args map[string]string) (framework.Plugin, error) {
	w, err := floatArg(args, "weight", 1.0)
	if err != nil {
		return nil, err
	}
	mode := stringArg(args, "mode", "binpack")
	if mode != "binpack" && mode != "spread" {
		return nil, &argError{key: "mode", value: mode, want: "binpack or spread"}
	}
	return &Binpack{weight: w, mode: mode, resource: stringArg(args, "resource", "")}, nil
}

func (b *Binpack) Name() string { return "binpack" }

func (b *Binpack) OnSessionOpen(ssn *framework.Session) {
	ssn.AddNodeOrderFn("binpack", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		if b.weight == 0 {
			return 0, nil
		}
		ratio := b.utilisationAfter(pod, node)
		if b.mode == "spread" {
			ratio = 1 - ratio
		}
		return b.weight * 10 * ratio, nil
	})
}

func (b *Binpack) OnSessionClose(ssn *framework.Session) {}

func (b *Binpack) utilisationAfter(pod *api.Pod, node *cache.NodeState) float64 {
	if b.resource != "" {
		cap := node.Node.Allocatable[b.resource]
		if cap <= 0 {
			return 0
		}
		return clamp01(float64(node.Node.Allocated[b.resource]+pod.Requests[b.resource]) / float64(cap))
	}
	best := 0.0
	for k, cap := range node.Node.Allocatable {
		if cap <= 0 {
			continue
		}
		r := clamp01(float64(node.Node.Allocated[k]+pod.Requests[k]) / float64(cap))
		if r > best {
			best = r
		}
	}
	return best
}

func clamp01(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 1 {
		return 1
	}
	return v
}

type argError struct {
	key, value, want string
}

func (e *argError) Error() string {
	return "plugin argument " + e.key + "=" + e.value + " is invalid (want " + e.want + ")"
}
