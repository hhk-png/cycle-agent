package scheduler

import (
	"fmt"
	"sort"

	"example.com/minikueue/internal/api"
)

// FitCheck is one resource-level check performed while evaluating a flavor.
// The numbers are the exact inputs used by canFitFlavor, so an explanation
// cannot drift from the admission decision without a test failing.
type FitCheck struct {
	Resource api.ResourceName
	// Requested is the amount this workload asks for; the other quantities
	// describe the state that request is compared against.
	Requested int64
	Current   int64
	Nominal   int64
	NewUsage  int64

	CohortUsage   int64
	CohortNominal int64

	Borrow            int64
	Lendable          int64
	BorrowingLimit    int64
	HasBorrowingLimit bool

	CohortLimitName string
	CohortLimit     int64
	HasCohortLimit  bool

	// BlockedBy is empty when the resource fits. Otherwise it names the first
	// constraint that rejected it.
	BlockedBy string
	Detail    string
}

// FlavorDiagnosis explains why one flavor in a resource group is or is not
// feasible for the workload.
type FlavorDiagnosis struct {
	Flavor    string
	Feasible  bool
	Checks    []FitCheck
	BlockedBy string
}

// GroupDiagnosis explains one resource group. A group is feasible when at
// least one of its flavors is feasible in isolation; the final decision still
// requires all groups to be feasible together.
type GroupDiagnosis struct {
	Index            int
	CoveredResources []api.ResourceName
	Requested        api.ResourceList
	Flavors          []FlavorDiagnosis
	Feasible         bool
}

// DomainDiagnosis is a leaf topology domain's free capacity.
type DomainDiagnosis struct {
	Domain   string
	Capacity api.ResourceList
	Usage    api.ResourceList
	Free     api.ResourceList
}

// TopologyDiagnosis summarises the physical domains referenced by the
// workload's flavors. It answers the "quota fits but placement does not"
// question without guessing.
type TopologyDiagnosis struct {
	Name    string
	Levels  []string
	Domains []DomainDiagnosis
}

// PreemptionDiagnosis records what the preemption search found.
type PreemptionDiagnosis struct {
	Enabled        bool
	Trigger        string
	CandidateCount int
	Candidates     []string
	Victims        []string
	Feasible       bool
	Detail         string
}

// Explanation is a structured, read-only account of one admission attempt.
// It never mutates the cache. The Admission field is the witness: when it is
// non-nil, it is a complete assignment that makes the workload admissible.
type Explanation struct {
	Workload       string
	ClusterQueue   string
	Requested      api.ResourceList
	Groups         []GroupDiagnosis
	QuotaFeasible  bool
	DirectFeasible bool
	Topology       []TopologyDiagnosis
	Preemption     *PreemptionDiagnosis
	Reason         string
	Detail         string
	Admission      *api.Admission
}

// Explain evaluates w against cq without mutating the cache and returns the
// full decision path: flavor checks, quota-only feasibility, topology
// feasibility, and preemption candidates. It is the diagnostic counterpart of
// TryAdmit and shares evaluateFlavor with the admission hot path.
func (s *Scheduler) Explain(w *api.Workload, cq *api.ClusterQueue) Explanation {
	out := Explanation{
		Workload:     w.Key(),
		ClusterQueue: cq.Name,
		Requested:    w.TotalUsage(),
	}
	if !w.Spec.Active {
		out.Reason = "Inactive"
		out.Detail = "workload is inactive"
		return out
	}

	// A workload that already holds quota is present in the cache. Exclude it
	// from its own feasibility calculation so the report answers "could this
	// workload fit here?" rather than counting it twice.
	removed := map[string]bool{w.Key(): true}

	for i, group := range cq.Spec.ResourceGroups {
		requested := api.ResourceList{}
		for _, r := range group.CoveredResources {
			if q := out.Requested.Get(r); q > 0 {
				requested[r] = q
			}
		}
		gd := GroupDiagnosis{
			Index:            i,
			CoveredResources: append([]api.ResourceName(nil), group.CoveredResources...),
			Requested:        requested.Clone(),
		}
		for _, fq := range group.Flavors {
			verdict := s.evaluateFlavor(cq, fq, requested, removed, true)
			fd := FlavorDiagnosis{
				Flavor:    fq.Name,
				Feasible:  verdict.ok,
				Checks:    verdict.checks,
				BlockedBy: verdict.failure,
			}
			gd.Flavors = append(gd.Flavors, fd)
			if verdict.ok {
				gd.Feasible = true
			}
		}
		out.Groups = append(out.Groups, gd)
	}

	_, out.QuotaFeasible = s.findAssignmentWithoutTopology(w, cq, removed)
	if adm, ok := s.findAssignment(w, cq, removed); ok {
		out.DirectFeasible = true
		out.Admission = adm
		out.Reason = "Admissible"
		out.Detail = "a direct flavor assignment exists"
		out.Topology = s.explainTopology(cq)
		return out
	}

	if out.QuotaFeasible {
		// The quota ledger fits; the failure is physical placement.
		out.Topology = s.explainTopology(cq)
		out.Reason = "TopologyInfeasible"
		out.Detail = "quota fits, but no TAS placement satisfies the topology constraints"
	} else {
		out.Reason = "InsufficientQuota"
		out.Detail = "no flavor assignment fits the quota ledger"
	}

	// Preemption is only meaningful after direct admission failed. Run the
	// same trigger classification as TryAdmit so the explanation matches the
	// real decision path.
	trigger := api.TriggerInsufficientQuota
	if out.QuotaFeasible {
		trigger = api.TriggerQuotaFeasibleAndInsufficientTopology
	}
	pd := &PreemptionDiagnosis{
		Enabled: s.preemptionEnabled(cq),
		Trigger: string(trigger),
	}
	if pd.Enabled {
		candidates := s.preemptionCandidates(w, cq, out.Requested, trigger)
		pd.CandidateCount = len(candidates)
		for _, c := range candidates {
			pd.Candidates = append(pd.Candidates, c.Key())
		}
		if adm, victims, ok := s.findPreemption(w, cq, out.Requested, trigger); ok {
			pd.Feasible = true
			pd.Victims = workloadKeys(victims)
			pd.Detail = "a minimal victim set makes the assignment feasible"
			out.Admission = adm
			out.Reason = "Preemptable"
			out.Detail = "admission requires preemption"
		} else if adm, victims, ok := s.findFairSharingPreemption(w, cq, out.Requested); ok {
			pd.Feasible = true
			pd.Victims = workloadKeys(victims)
			pd.Detail = "fair-sharing preemption makes the assignment feasible"
			out.Admission = adm
			out.Reason = "FairSharingPreemptable"
			out.Detail = "admission requires fair-sharing preemption"
		} else if len(candidates) == 0 {
			pd.Detail = "no admissible victim; check priority and preemption policy"
		} else {
			pd.Detail = "candidates exist, but no minimal victim set makes the workload fit"
		}
	} else {
		pd.Detail = "preemption is disabled for this ClusterQueue"
	}
	out.Preemption = pd

	if out.Reason == "InsufficientQuota" && pd.Enabled {
		out.Detail = "insufficient quota after preemption search"
	}
	return out
}

type flavorVerdict struct {
	ok      bool
	checks  []FitCheck
	failure string
}

// evaluateFlavor is the shared implementation of canFitFlavor. It returns the
// boolean verdict plus the exact per-resource checks, in stable resource
// order. Keeping one implementation is deliberate: an explanation that can
// disagree with the admission decision is worse than no explanation.
func (s *Scheduler) evaluateFlavor(cq *api.ClusterQueue, fq api.FlavorQuota, requested api.ResourceList, removed map[string]bool, detailed bool) flavorVerdict {
	verdict := flavorVerdict{ok: true}
	root := s.rootCohort(cq)
	members := s.cohortMembers(root)

	resources := make([]string, 0, len(requested))
	for r := range requested {
		resources = append(resources, string(r))
	}
	sort.Strings(resources)

	for _, name := range resources {
		r := api.ResourceName(name)
		req := requested.Get(r)
		cur := s.Cache.FlavorUsageExcluding([]string{cq.Name}, fq.Name, r, removed)
		nominal := s.nominal(cq.Name, fq.Name, r)
		newUsage := cur + req

		check := FitCheck{
			Resource:  r,
			Requested: req,
			Current:   cur,
			Nominal:   nominal,
			NewUsage:  newUsage,
		}

		if root == "" {
			if newUsage > nominal {
				check.BlockedBy = "ClusterQueueNominal"
				check.Detail = fmt.Sprintf("CQ usage %d + request %d > nominal %d", cur, req, nominal)
				verdict.ok = false
				if verdict.failure == "" {
					verdict.failure = "ClusterQueueNominal"
				}
			}
			if detailed {
				verdict.checks = append(verdict.checks, check)
			}
			continue
		}

		cohortTotal := s.Cache.FlavorUsageExcluding(members, fq.Name, r, removed)
		cohortNominal := s.cohortNominal(root, fq.Name, r)
		check.CohortUsage = cohortTotal
		check.CohortNominal = cohortNominal

		if cohortTotal+req > cohortNominal {
			check.BlockedBy = "CohortNominal"
			check.Detail = fmt.Sprintf("cohort usage %d + request %d > cohort nominal %d", cohortTotal, req, cohortNominal)
			verdict.ok = false
			if verdict.failure == "" {
				verdict.failure = "CohortNominal"
			}
			if detailed {
				verdict.checks = append(verdict.checks, check)
			}
			continue
		}

		if newUsage <= nominal {
			check.Detail = fmt.Sprintf("within nominal: usage %d + request %d <= nominal %d", cur, req, nominal)
			if detailed {
				verdict.checks = append(verdict.checks, check)
			}
			continue
		}

		borrow := newUsage - nominal
		check.Borrow = borrow
		blocked := false
		if limit, ok := fq.BorrowingLimit[r]; ok {
			check.BorrowingLimit = limit
			check.HasBorrowingLimit = true
			if borrow > limit {
				check.BlockedBy = "BorrowingLimit"
				check.Detail = fmt.Sprintf("borrow %d > flavor borrowingLimit %d", borrow, limit)
				blocked = true
			}
		}

		if !blocked {
			currentBorrow := cur - nominal
			if currentBorrow < 0 {
				currentBorrow = 0
			}
			incrementalBorrow := borrow - currentBorrow
			if incrementalBorrow < 0 {
				incrementalBorrow = 0
			}
			for _, cohort := range s.cohortPath(cq) {
				limit, ok := s.cohortBorrowingLimitAt(cohort, fq.Name, r)
				if !ok {
					continue
				}
				check.CohortLimitName = cohort
				check.CohortLimit = limit
				check.HasCohortLimit = true
				if s.cohortSubtreeBorrowed(cohort, fq.Name, r, removed)+incrementalBorrow > limit {
					check.BlockedBy = "CohortBorrowingLimit"
					check.Detail = fmt.Sprintf("subtree borrow after admission exceeds cohort %q borrowingLimit %d", cohort, limit)
					blocked = true
					break
				}
			}
		}

		if !blocked {
			lendable := s.Cache.LendableExcluding(members, cq.Name, fq.Name, r, removed,
				func(cqName, flavor string, resource api.ResourceName) int64 {
					return s.nominal(cqName, flavor, resource)
				},
				func(cqName, flavor string, resource api.ResourceName) (int64, bool) {
					return s.effectiveLendingLimit(cqName, flavor, resource)
				})
			lendable += s.cohortSharedFree(root, fq.Name, r, removed)
			check.Lendable = lendable
			if borrow > lendable {
				check.BlockedBy = "LendableQuota"
				check.Detail = fmt.Sprintf("borrow %d > lendable quota %d", borrow, lendable)
				blocked = true
			}
		}

		if blocked {
			verdict.ok = false
			if verdict.failure == "" {
				verdict.failure = check.BlockedBy
			}
		} else {
			check.Detail = fmt.Sprintf("borrow %d within borrowingLimit/lendable (%d)", borrow, check.Lendable)
		}
		if detailed {
			verdict.checks = append(verdict.checks, check)
		}
	}
	return verdict
}

// explainTopology lists the leaf domains of every topology referenced by the
// ClusterQueue's flavors. Usage is distributed down the topology tree by
// TopologyUsage, so a rack-level reservation is visible at hostname level.
func (s *Scheduler) explainTopology(cq *api.ClusterQueue) []TopologyDiagnosis {
	seen := map[string]bool{}
	var names []string
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			flavor, ok := s.View.Flavors[fq.Name]
			if !ok || flavor.TopologyName == "" || seen[flavor.TopologyName] {
				continue
			}
			seen[flavor.TopologyName] = true
			names = append(names, flavor.TopologyName)
		}
	}
	sort.Strings(names)

	out := make([]TopologyDiagnosis, 0, len(names))
	for _, name := range names {
		topology, ok := s.View.Topologies[name]
		if !ok || len(topology.Levels) == 0 {
			continue
		}
		leaf := topology.Levels[len(topology.Levels)-1]
		capacity := s.topologyCapacity(name, leaf)
		usage := s.TopologyUsage(name, leaf, nil)
		td := TopologyDiagnosis{
			Name:   name,
			Levels: append([]string(nil), topology.Levels...),
		}
		domains := make([]string, 0, len(capacity))
		for domain := range capacity {
			domains = append(domains, domain)
		}
		sort.Strings(domains)
		for _, domain := range domains {
			free := topologyFree(capacity[domain], usage[domain])
			td.Domains = append(td.Domains, DomainDiagnosis{
				Domain:   domain,
				Capacity: capacity[domain].Clone(),
				Usage:    usage[domain].Clone(),
				Free:     free,
			})
		}
		out = append(out, td)
	}
	return out
}

func workloadKeys(workloads []*api.Workload) []string {
	out := make([]string, 0, len(workloads))
	for _, w := range workloads {
		out = append(out, w.Key())
	}
	sort.Strings(out)
	return out
}
