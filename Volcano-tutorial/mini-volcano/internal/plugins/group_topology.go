package plugins

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// GroupTopologyAffinity implements the v1.16 PodGroup topology anti-affinity
// semantics at a teaching level: PodGroups selected by labels must land in
// different HyperNode domains. A term with Weight == 0 is required; a term
// with Weight > 0 is preferred and contributes a negative NodeOrder score.
//
// Evaluation is symmetric: a rule declared by PodGroup A against selector S
// protects A from any matching B, and also protects B when B is scheduled
// later even if B does not repeat the rule itself. This is stricter than a
// literal per-object evaluation and matches the operator's intuition that
// anti-affinity is a property of the pair, not of one side.
//
// The plugin is allocate-only, matching the upstream guide: best-effort Pods
// placed by backfill and the placement probes used by preempt/reclaim/shuffle
// are not constrained. The Scheduler sets Session.Action before each action.
type GroupTopologyAffinity struct {
	weight float64
}

func newGroupTopologyAffinity(args map[string]string) (framework.Plugin, error) {
	w, err := floatArg(args, "weight", 1.0)
	if err != nil {
		return nil, err
	}
	if w <= 0 {
		return nil, fmt.Errorf("group-topology-affinity: weight must be > 0")
	}
	return &GroupTopologyAffinity{weight: w}, nil
}

func (g *GroupTopologyAffinity) Name() string { return "group-topology-affinity" }

type topologyConflict struct {
	peer   string
	tier   int
	domain string
	weight int32
}

func (g *GroupTopologyAffinity) OnSessionOpen(ssn *framework.Session) {
	ssn.AddPredicateFn("group-topology-affinity", func(pod *api.Pod, node *cache.NodeState) error {
		if ssn.Action != "allocate" {
			return nil
		}
		pg := ssn.Cache.PodGroups[pod.Job]
		if pg == nil {
			return nil
		}
		conflicts := g.conflicts(ssn, pg, node.Node.Name, false)
		if len(conflicts) == 0 {
			return nil
		}
		c := conflicts[0]
		if c.domain == "" {
			return fmt.Errorf("group topology: node %s has no HyperNode domain at tier %d required by anti-affinity with %s",
				node.Node.Name, c.tier, c.peer)
		}
		return fmt.Errorf("group topology: node %s is in domain %s at tier %d, but required PodGroup anti-affinity conflicts with %s",
			node.Node.Name, c.domain, c.tier, c.peer)
	})

	ssn.AddNodeOrderFn("group-topology-affinity", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		if ssn.Action != "allocate" {
			return 0, nil
		}
		pg := ssn.Cache.PodGroups[pod.Job]
		if pg == nil {
			return 0, nil
		}
		// Deduplicate by peer: if both PodGroups declare the same preferred
		// term, the pair should contribute its weight once, not twice.
		weights := map[string]int32{}
		for _, c := range g.conflicts(ssn, pg, node.Node.Name, true) {
			if c.weight > weights[c.peer] {
				weights[c.peer] = c.weight
			}
		}
		penalty := 0.0
		for _, w := range weights {
			penalty += float64(w)
		}
		return -penalty * g.weight, nil
	})
}

func (g *GroupTopologyAffinity) OnSessionClose(ssn *framework.Session) {}

func (g *GroupTopologyAffinity) conflicts(ssn *framework.Session, pg *api.PodGroup, nodeName string, preferred bool) []topologyConflict {
	groups := sortedPodGroups(ssn)
	var out []topologyConflict

	for _, declarer := range groups {
		if declarer.TopologyAffinity == nil {
			continue
		}
		for _, term := range declarer.TopologyAffinity.PodGroupAntiAffinity {
			if (term.Weight > 0) != preferred {
				continue
			}
			tier := term.TopologyTier
			if tier <= 0 {
				// Mini tolerates an omitted tier and treats it as the leaf
				// tier. Upstream requires either topologyTier or
				// topologyTierName; migration must write it explicitly.
				tier = 1
			}

			switch {
			case declarer.UID == pg.UID:
				// This PodGroup's own rule: matching peers that already hold
				// nodes are the only ones that can conflict. A term that
				// matches nobody is a no-op, even on a node outside the
				// HyperNode tree.
				for _, peer := range groups {
					if peer.UID == pg.UID || peer.Namespace != pg.Namespace ||
						!labelsMatch(peer.Labels, term.MatchLabels) ||
						!hasPlacedPods(ssn, peer) {
						continue
					}
					domain := ssn.Cache.DomainOfNodeAtTier(nodeName, tier)
					if domain == "" {
						if !preferred {
							out = append(out, topologyConflict{
								peer: peer.Name, tier: tier, weight: term.Weight,
							})
						}
						continue
					}
					if podsInDomain(ssn, peer, domain, tier) {
						out = append(out, topologyConflict{
							peer: peer.Name, tier: tier, domain: domain, weight: term.Weight,
						})
					}
				}
			case declarer.Namespace == pg.Namespace && labelsMatch(pg.Labels, term.MatchLabels):
				// A peer's rule selects us: only a declarer that already holds
				// nodes can conflict. Upstream defaults the selector to the
				// declaring namespace unless a namespace selector widens it.
				if !hasPlacedPods(ssn, declarer) {
					continue
				}
				domain := ssn.Cache.DomainOfNodeAtTier(nodeName, tier)
				if domain == "" {
					if !preferred {
						out = append(out, topologyConflict{
							peer: declarer.Name, tier: tier, weight: term.Weight,
						})
					}
					continue
				}
				if podsInDomain(ssn, declarer, domain, tier) {
					out = append(out, topologyConflict{
						peer: declarer.Name, tier: tier, domain: domain, weight: term.Weight,
					})
				}
			}
		}
	}
	return out
}

func sortedPodGroups(ssn *framework.Session) []*api.PodGroup {
	out := make([]*api.PodGroup, 0, len(ssn.Cache.PodGroups))
	for _, pg := range ssn.Cache.PodGroups {
		out = append(out, pg)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Namespace != out[j].Namespace {
			return out[i].Namespace < out[j].Namespace
		}
		if out[i].Name != out[j].Name {
			return out[i].Name < out[j].Name
		}
		return out[i].UID < out[j].UID
	})
	return out
}

func labelsMatch(labels, selector map[string]string) bool {
	for k, v := range selector {
		if labels[k] != v {
			return false
		}
	}
	return true
}

func hasPlacedPods(ssn *framework.Session, pg *api.PodGroup) bool {
	for _, pod := range ssn.Cache.JobPods(pg.UID) {
		if pod.Phase.OccupiesNode() && pod.NodeName != "" {
			return true
		}
	}
	return false
}

func podsInDomain(ssn *framework.Session, pg *api.PodGroup, domain string, tier int) bool {
	for _, pod := range ssn.Cache.JobPods(pg.UID) {
		if !pod.Phase.OccupiesNode() || pod.NodeName == "" {
			continue
		}
		if ssn.Cache.DomainOfNodeAtTier(pod.NodeName, tier) == domain {
			return true
		}
	}
	return false
}
