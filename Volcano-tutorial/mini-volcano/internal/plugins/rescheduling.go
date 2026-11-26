package plugins

import (
	"math"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// Rescheduling implements two victim-selection strategies:
//
//	lowNodeUtilization (default, upstream-inspired): evict from an over-utilized
//	  node when an under-utilized node exists, so the next allocate pass can
//	  spread the evicted pods onto the colder node.
//	fragmentation (teaching strategy): evict from a half-full node so a binpack
//	  allocate pass can consolidate the workload onto one node.
//
// It never evicts anything itself. It only supplies candidates through
// VictimTasksFn; the shuffle action performs the eviction.
//
// Real Volcano's rescheduling plugin defaults to a five-minute interval and
// uses CPU/memory/Pod-count percentage thresholds. MiniVolcano keeps the shape
// but expresses thresholds as 0..1 ratios and uses one threshold for all
// dimensions; the important semantic (which side of the utilization spectrum
// is the victim source) follows upstream.
type Rescheduling struct {
	strategy  string
	threshold float64
	target    float64
	interval  int64
	lastTick  int64
	started   bool
}

const (
	strategyLowNodeUtilization = "lowNodeUtilization"
	strategyFragmentation      = "fragmentation"
)

func newRescheduling(args map[string]string) (framework.Plugin, error) {
	strategy := stringArg(args, "strategy", strategyLowNodeUtilization)
	if strategy != strategyLowNodeUtilization && strategy != strategyFragmentation {
		return nil, &argError{key: "strategy", value: strategy, want: "lowNodeUtilization or fragmentation"}
	}

	defaultThreshold := 0.2
	if strategy == strategyFragmentation {
		defaultThreshold = 0.5
	}
	threshold, err := floatArg(args, "threshold", defaultThreshold)
	if err != nil {
		return nil, err
	}
	target, err := floatArg(args, "targetThreshold", 0.5)
	if err != nil {
		return nil, err
	}
	if math.IsNaN(threshold) || math.IsInf(threshold, 0) || threshold <= 0 || threshold > 1 {
		return nil, &argError{key: "threshold", value: stringArg(args, "threshold", ""), want: "0 < threshold <= 1"}
	}
	if math.IsNaN(target) || math.IsInf(target, 0) || target <= 0 || target > 1 {
		return nil, &argError{key: "targetThreshold", value: stringArg(args, "targetThreshold", ""), want: "0 < targetThreshold <= 1"}
	}
	if strategy == strategyLowNodeUtilization && threshold >= target {
		return nil, &argError{key: "threshold", value: stringArg(args, "threshold", ""), want: "threshold < targetThreshold for lowNodeUtilization"}
	}

	interval, err := intArg(args, "interval", 0)
	if err != nil {
		return nil, err
	}
	if interval < 0 {
		return nil, &argError{key: "interval", value: stringArg(args, "interval", ""), want: ">= 0"}
	}
	return &Rescheduling{strategy: strategy, threshold: threshold, target: target, interval: int64(interval)}, nil
}

func (r *Rescheduling) Name() string { return "rescheduling" }

func (r *Rescheduling) OnSessionOpen(ssn *framework.Session) {
	ssn.AddVictimTasksFns("rescheduling", func() []*api.Pod {
		if r.interval > 0 && r.started && ssn.Now-r.lastTick < r.interval {
			return nil
		}
		var victims []*api.Pod
		switch r.strategy {
		case strategyFragmentation:
			victims = r.fragmentationVictims(ssn)
		default:
			victims = r.lowNodeUtilizationVictims(ssn)
		}
		if len(victims) > 0 {
			r.lastTick = ssn.Now
			r.started = true
		}
		return victims
	})
}

func (r *Rescheduling) OnSessionClose(ssn *framework.Session) {}

// fragmentationVictims picks the least-utilized non-empty node below the
// threshold. If its pods are all protected, it tries the next candidate rather
// than becoming a permanent no-op.
func (r *Rescheduling) fragmentationVictims(ssn *framework.Session) []*api.Pod {
	type candidate struct {
		node *cache.NodeState
		util float64
	}
	var candidates []candidate
	for _, node := range ssn.Nodes() {
		if len(node.Pods) == 0 {
			continue
		}
		util := nodeUtilization(node)
		if util >= r.threshold {
			continue
		}
		candidates = append(candidates, candidate{node: node, util: util})
	}
	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].util != candidates[j].util {
			return candidates[i].util < candidates[j].util
		}
		return candidates[i].node.Node.Name < candidates[j].node.Node.Name
	})
	for _, c := range candidates {
		if victims := safeVictims(ssn, c.node); len(victims) > 0 {
			return victims
		}
	}
	return nil
}

// lowNodeUtilizationVictims mirrors the upstream direction: only act when at
// least one node is under-utilized, then evict from an over-utilized node until
// it falls back to the target threshold. The next allocate pass is expected to
// spread the evicted pods onto the colder node(s).
func (r *Rescheduling) lowNodeUtilizationVictims(ssn *framework.Session) []*api.Pod {
	underutilized := false
	for _, node := range ssn.Nodes() {
		if nodeUtilization(node) < r.threshold {
			underutilized = true
			break
		}
	}
	if !underutilized {
		return nil
	}

	var over []*cache.NodeState
	for _, node := range ssn.Nodes() {
		if len(node.Pods) == 0 {
			continue
		}
		if nodeUtilization(node) > r.target {
			over = append(over, node)
		}
	}
	sort.SliceStable(over, func(i, j int) bool {
		ui, uj := nodeUtilization(over[i]), nodeUtilization(over[j])
		if ui != uj {
			return ui > uj
		}
		return over[i].Node.Name < over[j].Node.Name
	})

	for _, node := range over {
		victims := safeVictims(ssn, node)
		if len(victims) == 0 {
			continue
		}
		// Evict only enough to reach the target; over-evicting makes the next
		// session thrash in the opposite direction.
		removed := api.Resource{}
		var selected []*api.Pod
		for _, p := range victims {
			if nodeUtilizationAfter(node, removed) <= r.target {
				break
			}
			selected = append(selected, p)
			removed = removed.Add(p.Requests)
		}
		if len(selected) > 0 {
			return selected
		}
	}
	return nil
}

// safeVictims returns pods on a node that can be evicted without taking their
// job below minAvailable. Succeeded pods are not counted as active: evicting a
// Running replica while a Succeeded replica "counts" toward gang would leave the
// job with zero running pods but a Running phase.
func safeVictims(ssn *framework.Session, node *cache.NodeState) []*api.Pod {
	pods := make([]*api.Pod, 0, len(node.Pods))
	for _, p := range node.Pods {
		if p.Phase.OccupiesNode() {
			pods = append(pods, p)
		}
	}
	// Name alone is not unique across namespaces; UID is.
	sort.SliceStable(pods, func(i, j int) bool {
		if pods[i].Name != pods[j].Name {
			return pods[i].Name < pods[j].Name
		}
		return pods[i].UID < pods[j].UID
	})

	active := map[string]int{}
	activeByTask := map[string]map[string]int{}
	minAvailable := map[string]int{}
	gates := map[string]map[string]int32{}
	roleGated := map[string]bool{}
	for _, p := range pods {
		if _, ok := active[p.Job]; ok {
			continue
		}
		active[p.Job] = activeCount(ssn, p.Job)
		activeByTask[p.Job] = ssn.Cache.OccupyingCountByTask(p.Job)
		if j := ssn.JobByUID(p.Job); j != nil {
			minAvailable[p.Job] = int(j.Spec.MinAvailable)
			gates[p.Job] = j.MinTaskMember()
			roleGated[p.Job] = j.RoleGatesActive()
		}
	}

	var victims []*api.Pod
	for _, p := range pods {
		if min := minAvailable[p.Job]; min > 0 && active[p.Job]-1 < min {
			continue
		}
		if roleGated[p.Job] {
			if want := gates[p.Job][p.Task]; want > 0 && activeByTask[p.Job][p.Task]-1 < int(want) {
				continue
			}
		}
		active[p.Job]--
		activeByTask[p.Job][p.Task]--
		victims = append(victims, p)
	}
	return victims
}

// activeCount counts pods that still occupy a node. It intentionally excludes
// Succeeded/Failed pods, which ReadyForGang counts.
func activeCount(ssn *framework.Session, jobUID string) int {
	n := 0
	for _, p := range ssn.JobPods(jobUID) {
		if p.Phase.OccupiesNode() {
			n++
		}
	}
	return n
}

// nodeUtilization is the maximum allocated/capacity ratio across dimensions.
// Dimensions absent from Allocatable are ignored, mirroring partial capability
// semantics: a GPU-only node should not be judged by its zero CPU capacity.
func nodeUtilization(node *cache.NodeState) float64 {
	return nodeUtilizationAfter(node, nil)
}

func nodeUtilizationAfter(node *cache.NodeState, removed api.Resource) float64 {
	best := 0.0
	for _, k := range node.Node.Allocatable.Keys() {
		cap := node.Node.Allocatable[k]
		if cap <= 0 {
			continue
		}
		used := node.Node.Allocated[k] - removed[k]
		if used < 0 {
			used = 0
		}
		u := float64(used) / float64(cap)
		if u > best {
			best = u
		}
	}
	return best
}
