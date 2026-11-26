package actions

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Shuffle implements Volcano's shuffle action: it does not choose nodes or
// queues. It asks the plugins for victim tasks, evicts them, and lets the next
// scheduling pass repack them. In Volcano the victims come from the
// rescheduling, tdm and pdb plugins through VictimTasksFn.
//
// MiniVolcano adds two safety checks that a teaching simulator can afford:
// combined gang protection across all suppliers, and a dry-run that proves each
// victim has a strictly better destination than the node it is leaving. Without
// the latter, a binpack allocate pass would put the pod straight back and the
// cluster would evict it again every tick.
type Shuffle struct{}

func (a *Shuffle) Name() string { return "shuffle" }

func (a *Shuffle) Execute(ssn *framework.Session) error {
	victims := gangSafeVictims(ssn, ssn.VictimTasks())
	victims = disruptableVictims(ssn, victims)
	if len(victims) == 0 {
		return nil
	}
	if !canRepack(ssn, victims) {
		return nil
	}

	st := ssn.NewStatement()
	for _, pod := range victims {
		if pod == nil || !pod.Phase.OccupiesNode() {
			continue
		}
		if err := st.Evict(pod.UID, "shuffle: low-utilization or over-utilized node"); err != nil {
			st.Discard()
			return nil
		}
	}
	st.Commit()
	ssn.AddEvent(api.Event{
		Type:    "Shuffled",
		Message: fmt.Sprintf("evicted %d pod(s) for repacking", len(victims)),
	})
	return nil
}

// gangSafeVictims applies a second, action-level minAvailable check. Plugins
// filter their own candidates, but two suppliers in the same tier are merged by
// VictimTasks; each supplier can independently believe "one more pod is safe"
// while the union is not.
func gangSafeVictims(ssn *framework.Session, victims []*api.Pod) []*api.Pod {
	if len(victims) == 0 {
		return nil
	}
	sort.SliceStable(victims, func(i, j int) bool {
		if victims[i].Name != victims[j].Name {
			return victims[i].Name < victims[j].Name
		}
		return victims[i].UID < victims[j].UID
	})

	active := map[string]int{}
	activeByTask := map[string]map[string]int{}
	minAvailable := map[string]int{}
	gates := map[string]map[string]int32{}
	roleGated := map[string]bool{}
	for _, p := range victims {
		if _, ok := active[p.Job]; ok {
			continue
		}
		active[p.Job] = 0
		for _, pod := range ssn.JobPods(p.Job) {
			if pod.Phase.OccupiesNode() {
				active[p.Job]++
			}
		}
		activeByTask[p.Job] = ssn.Cache.OccupyingCountByTask(p.Job)
		if j := ssn.JobByUID(p.Job); j != nil {
			minAvailable[p.Job] = int(j.Spec.MinAvailable)
			gates[p.Job] = j.MinTaskMember()
			roleGated[p.Job] = j.RoleGatesActive()
		}
	}

	out := make([]*api.Pod, 0, len(victims))
	for _, p := range victims {
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
		out = append(out, p)
	}
	return out
}

// disruptableVictims enforces PDB-style protection cumulatively. Individual
// DisruptableFn calls see the committed cache; two evictions from the same PDB
// can each look safe while the pair violates minAvailable. Replaying the
// evictions inside a statement makes each check observe the previous ones.
func disruptableVictims(ssn *framework.Session, victims []*api.Pod) []*api.Pod {
	if len(victims) == 0 {
		return nil
	}
	trial := ssn.NewStatement()
	out := make([]*api.Pod, 0, len(victims))
	for _, pod := range victims {
		if pod == nil || !pod.Phase.OccupiesNode() {
			continue
		}
		if !trial.Disruptable(pod) {
			continue
		}
		if err := trial.Evict(pod.UID, "shuffle protection dry-run"); err != nil {
			continue
		}
		out = append(out, pod)
	}
	return out
}

// canRepack proves, in a throwaway statement, that every victim can be bound
// to a node whose score is strictly better than the victim's current node.
// Only the evictions are committed by Execute; the trial bindings are discarded.
func canRepack(ssn *framework.Session, victims []*api.Pod) bool {
	trial := ssn.NewStatement()
	sources := map[string]bool{}
	for _, v := range victims {
		sources[v.NodeName] = true
		if err := trial.Evict(v.UID, "shuffle dry-run"); err != nil {
			return false
		}
	}
	for _, v := range victims {
		pod := trial.Trial().Pods[v.UID]
		if pod == nil || pod.Phase != api.PodPending {
			return false
		}
		target, ok := betterTarget(ssn, trial, pod, v.NodeName, sources)
		if !ok {
			return false
		}
		if err := trial.Bind(pod.UID, target); err != nil {
			return false
		}
	}
	return true
}

// betterTarget returns the highest-scoring feasible node that is not one of the
// source nodes and scores strictly higher than the pod's current node. In the
// fragmentation scenario this is the busier node (binpack); in the upstream
// lowNodeUtilization scenario it is the colder node (nodeorder/spread).
func betterTarget(ssn *framework.Session, st *framework.Statement, pod *api.Pod, source string, sources map[string]bool) (string, bool) {
	sourceNode := st.Trial().Nodes[source]
	if sourceNode == nil {
		return "", false
	}
	sourceScore, err := st.NodeScore(pod, source)
	if err != nil {
		return "", false
	}

	best := ""
	bestScore := 0.0
	for _, node := range ssn.Nodes() {
		name := node.Node.Name
		if name == source || sources[name] {
			continue
		}
		if err := st.Predicate(pod, name); err != nil {
			continue
		}
		score, err := st.NodeScore(pod, name)
		if err != nil {
			continue
		}
		if best == "" || score > bestScore || (score == bestScore && name < best) {
			best, bestScore = name, score
		}
	}
	if best == "" || bestScore <= sourceScore {
		return "", false
	}
	return best, true
}
