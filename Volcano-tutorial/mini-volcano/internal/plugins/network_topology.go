package plugins

import (
	"fmt"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// NetworkTopology implements the gang-level part of Volcano's HyperNode-aware
// scheduling. For a hard-constrained job it picks the smallest HyperNode
// domain that can hold the gang and restricts every task to that domain.
// Soft mode turns locality into a node score instead.
type NetworkTopology struct {
	allowed map[string]string // job UID -> HyperNode name, or noDomain
	reasons map[string]string
}

const noDomain = "__no_domain__"

func newNetworkTopology(args map[string]string) (framework.Plugin, error) {
	return &NetworkTopology{}, nil
}

func (n *NetworkTopology) Name() string { return "network-topology-aware" }

func (n *NetworkTopology) OnSessionOpen(ssn *framework.Session) {
	n.allowed = map[string]string{}
	n.reasons = map[string]string{}

	if len(ssn.Cache.HyperNodes) == 0 {
		return
	}
	for _, job := range ssn.Jobs() {
		spec := job.Spec.NetworkTopology
		if spec == nil || spec.Mode == "soft" {
			continue
		}
		tier := spec.HighestTierAllowed
		if tier <= 0 {
			tier = 2
		}
		domains := n.domainsAtOrBelow(ssn, tier)
		if len(domains) == 0 {
			n.allowed[job.UID] = noDomain
			n.reasons[job.UID] = fmt.Sprintf("no HyperNode domain at tier <= %d", tier)
			ssn.RecordUnschedulable(job.UID, n.reasons[job.UID])
			continue
		}
		need := job.MinResources()
		best := ""
		bestCap := api.Resource(nil)
		for _, d := range domains {
			cap := n.domainCapacity(ssn, d.Name, job.Spec.Queue, job.UID)
			if !need.LessEqual(cap) {
				continue
			}
			if best == "" || cap.Cmp(bestCap) < 0 {
				best, bestCap = d.Name, cap
			}
		}
		if best == "" {
			n.allowed[job.UID] = noDomain
			n.reasons[job.UID] = fmt.Sprintf("no HyperNode at tier <= %d has %s free", tier, need)
			ssn.RecordUnschedulable(job.UID, n.reasons[job.UID])
			continue
		}
		n.allowed[job.UID] = best
	}

	ssn.AddPredicateFn("network-topology-aware", func(pod *api.Pod, node *cache.NodeState) error {
		domain, ok := n.allowed[pod.Job]
		if !ok {
			return nil
		}
		if domain == noDomain {
			return fmt.Errorf("network topology: %s", n.reasons[pod.Job])
		}
		for _, member := range ssn.Cache.HyperNodeMembers(domain) {
			if member == node.Node.Name {
				return nil
			}
		}
		return fmt.Errorf("node %s is outside job's HyperNode %s", node.Node.Name, domain)
	})

	ssn.AddNodeOrderFn("network-topology-aware", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		spec := jobSpec(ssn, pod)
		if spec == nil || spec.NetworkTopology == nil || spec.NetworkTopology.Mode != "soft" {
			return 0, nil
		}
		tier := spec.NetworkTopology.HighestTierAllowed
		if tier <= 0 {
			tier = 2
		}
		target := ssn.Cache.DomainOfNodeAtTier(node.Node.Name, tier)
		if target == "" {
			return 0, nil
		}
		count := 0
		for _, peer := range ssn.Cache.JobPods(pod.Job) {
			if peer.UID == pod.UID || !peer.Phase.OccupiesNode() || peer.NodeName == "" {
				continue
			}
			if ssn.Cache.DomainOfNodeAtTier(peer.NodeName, tier) == target {
				count++
			}
		}
		if count > 3 {
			count = 3
		}
		return float64(count) * 2, nil
	})
}

func (n *NetworkTopology) OnSessionClose(ssn *framework.Session) {}

func jobSpec(ssn *framework.Session, pod *api.Pod) *api.JobSpec {
	if job := ssn.JobByUID(pod.Job); job != nil {
		return &job.Spec
	}
	return nil
}

// domainsAtOrBelow returns the widest available domains with tier <= limit.
// The widest allowed tier gives the scheduler the most freedom while still
// honouring the job's locality requirement.
func (n *NetworkTopology) domainsAtOrBelow(ssn *framework.Session, limit int) []*api.HyperNode {
	bestTier := -1
	for _, h := range ssn.Cache.HyperNodes {
		if h.Tier <= limit && h.Tier > bestTier && len(ssn.Cache.HyperNodeMembers(h.Name)) > 0 {
			bestTier = h.Tier
		}
	}
	if bestTier < 0 {
		return nil
	}
	return ssn.Cache.HyperNodesAtTier(bestTier)
}

func (n *NetworkTopology) domainCapacity(ssn *framework.Session, domain, queue, jobUID string) api.Resource {
	out := api.Resource{}
	for _, node := range ssn.Cache.HyperNodeMembers(domain) {
		out = out.Add(ssn.Cache.IdleFor(node, queue, jobUID, ssn.Now))
	}
	return out
}
