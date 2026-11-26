package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// PDB protects workloads from voluntary disruption (preempt, reclaim, shuffle).
// It is the MiniVolcano analogue of Volcano's pdb plugin, which reads native
// policy/v1 PodDisruptionBudgets and registers Preemptable/Reclaimable hooks.
//
// The check is deliberately conservative:
//
//	available(pdb) - 1 >= minAvailable                (minAvailable form)
//	available(pdb) - 1 >= desired - maxUnavailable   (maxUnavailable form)
//
// "available" counts matching pods that currently occupy a node. "desired"
// counts matching non-terminal pods, which is the closest analogue of a
// controller's expected replica count without a ReplicaSet in the model.
type PDB struct{}

func newPDB(args map[string]string) (framework.Plugin, error) {
	return &PDB{}, nil
}

func (p *PDB) Name() string { return "pdb" }

func (p *PDB) OnSessionOpen(ssn *framework.Session) {
	ssn.AddPreemptableFn("pdb", func(_, victim *api.Pod) bool {
		return canDisrupt(ssn, victim)
	})
	ssn.AddReclaimableFn("pdb", func(_ *api.Queue, victim *api.Pod) bool {
		return canDisrupt(ssn, victim)
	})
	ssn.AddDisruptableFn("pdb", func(victim *api.Pod) bool {
		return canDisrupt(ssn, victim)
	})
}

func (p *PDB) OnSessionClose(ssn *framework.Session) {}

func canDisrupt(ssn *framework.Session, victim *api.Pod) bool {
	if victim == nil {
		return false
	}
	for _, budget := range ssn.Cache.PDBs {
		if !budget.Matches(victim) {
			continue
		}
		available := 0
		desired := 0
		for _, pod := range ssn.Cache.Pods {
			if !budget.Matches(pod) {
				continue
			}
			if pod.Phase.OccupiesNode() {
				available++
			}
			if pod.Phase != api.PodSucceeded && pod.Phase != api.PodFailed {
				desired++
			}
		}
		if budget.HasMin && available-1 < int(budget.MinAvailable) {
			return false
		}
		if budget.HasMax && available-1 < desired-int(budget.MaxUnavailable) {
			return false
		}
	}
	return true
}
