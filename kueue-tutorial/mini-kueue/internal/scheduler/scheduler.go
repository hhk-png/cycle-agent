// Package scheduler implements the quota decision core of Kueue: flavor
// assignment, cohort borrowing, fair-share scoring and classic preemption.
//
// It is deliberately pure with respect to the store: TryAdmit computes a
// decision and returns the victims that must be evicted. The controller then
// commits the evictions and the quota reservation. This separation makes the
// algorithm easy to unit-test and keeps rollback trivial.
package scheduler

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/cache"
)

// View is an immutable snapshot of queue configuration.
type View struct {
	ClusterQueues map[string]*api.ClusterQueue
	Cohorts       map[string]*api.Cohort
	Flavors       map[string]*api.ResourceFlavor
	Topologies    map[string]*api.Topology
	TopologyDoms  map[string][]*api.TopologyDomain
	// EffectiveQuotas overrides spec nominal quota when dynamic quota
	// orchestration is active: cq -> flavor -> resources.
	EffectiveQuotas map[string]map[string]api.ResourceList
}

// ViewOption customizes a View without breaking the two-argument constructor
// used by tests and simple callers.
type ViewOption func(*View)

// WithFlavors adds ResourceFlavor metadata (node labels and topology names).
func WithFlavors(flavors []*api.ResourceFlavor) ViewOption {
	return func(v *View) {
		for _, f := range flavors {
			v.Flavors[f.Name] = f
		}
	}
}

// WithTopologies adds Topology definitions and their leaf domains.
func WithTopologies(topologies []*api.Topology, domains map[string][]*api.TopologyDomain) ViewOption {
	return func(v *View) {
		for _, t := range topologies {
			v.Topologies[t.Name] = t
		}
		for topology, list := range domains {
			v.TopologyDoms[topology] = append([]*api.TopologyDomain(nil), list...)
		}
	}
}

// WithEffectiveQuotas installs dynamic nominal quotas.
func WithEffectiveQuotas(quotas map[string]map[string]api.ResourceList) ViewOption {
	return func(v *View) {
		v.EffectiveQuotas = quotas
	}
}

// NewView builds a scheduler view.
func NewView(cqs []*api.ClusterQueue, cohorts []*api.Cohort, opts ...ViewOption) View {
	v := View{
		ClusterQueues:   map[string]*api.ClusterQueue{},
		Cohorts:         map[string]*api.Cohort{},
		Flavors:         map[string]*api.ResourceFlavor{},
		Topologies:      map[string]*api.Topology{},
		TopologyDoms:    map[string][]*api.TopologyDomain{},
		EffectiveQuotas: map[string]map[string]api.ResourceList{},
	}
	for _, cq := range cqs {
		v.ClusterQueues[cq.Name] = cq
	}
	for _, c := range cohorts {
		v.Cohorts[c.Name] = c
	}
	for _, opt := range opts {
		opt(&v)
	}
	return v
}

// Scheduler evaluates admission decisions against a cache.
type Scheduler struct {
	Cache           *cache.Cache
	View            View
	PriorityClasses map[string]*api.WorkloadPriorityClass
	// PreemptionConfigs holds alpha configurable-preemption rules. They are
	// additive: candidates selected by a rule are merged with the classic
	// spec.preemption candidates.
	PreemptionConfigs map[string]*api.PreemptionConfig
	// FairSharingPreemption lists the enabled fair-sharing preemption
	// strategies: LessThanOrEqualToFinalShare and/or LessThanInitialShare.
	FairSharingPreemption []string
}

// New creates a scheduler.
func New(c *cache.Cache, view View) *Scheduler {
	return &Scheduler{Cache: c, View: view}
}

// Decision is the result of one admission attempt. Reason is a short,
// machine-readable code suitable for conditions; Detail is the human-readable
// explanation.
type Decision struct {
	Admission *api.Admission
	Victims   []*api.Workload
	Reason    string
	Detail    string
}

// TryAdmit attempts to reserve quota for w in cq. It does not mutate the
// cache. If Victims is non-empty, the controller must evict them before
// adding w.
func (s *Scheduler) TryAdmit(w *api.Workload, cq *api.ClusterQueue) Decision {
	if !w.Spec.Active {
		return Decision{Reason: "Inactive", Detail: "workload is inactive"}
	}
	usage := w.TotalUsage()

	if adm, ok := s.findAssignment(w, cq, nil); ok {
		return Decision{Admission: adm}
	}

	trigger := api.TriggerInsufficientQuota
	if _, quotaFits := s.findAssignmentWithoutTopology(w, cq, nil); quotaFits {
		// The quota ledger can accommodate the workload; the failure came from
		// placing PodSets on physical topology domains.
		trigger = api.TriggerQuotaFeasibleAndInsufficientTopology
	}
	if !s.preemptionEnabled(cq) {
		return Decision{Reason: "InsufficientQuota", Detail: "insufficient quota; preemption is disabled"}
	}
	if adm, victims, ok := s.findPreemption(w, cq, usage, trigger); ok {
		return Decision{Admission: adm, Victims: victims}
	}
	if adm, victims, ok := s.findFairSharingPreemption(w, cq, usage); ok {
		return Decision{Admission: adm, Victims: victims}
	}
	return Decision{Reason: "InsufficientQuota", Detail: "insufficient quota"}
}

// DirectAdmission evaluates a quota assignment without preemption and without
// mutating the cache. Excluded workloads are ignored during accounting; the
// controller uses this to test whether an already-running workload could
// migrate to a more preferred flavor.
func (s *Scheduler) DirectAdmission(w *api.Workload, cq *api.ClusterQueue, excluded map[string]bool) (*api.Admission, bool) {
	return s.findAssignment(w, cq, excluded)
}

func (s *Scheduler) preemptionEnabled(cq *api.ClusterQueue) bool {
	if s.configFor(cq) != nil || len(s.FairSharingPreemption) > 0 {
		return true
	}
	within := cq.Spec.Preemption.WithinClusterQueue
	reclaim := cq.Spec.Preemption.ReclaimWithinCohort
	borrow := cq.Spec.Preemption.BorrowWithinCohort
	return (within != "" && within != api.PreemptNever) ||
		(reclaim != "" && reclaim != api.PreemptNever) ||
		(borrow != nil && borrow.Policy != "" && borrow.Policy != api.PreemptNever)
}

func (s *Scheduler) configFor(cq *api.ClusterQueue) *api.PreemptionConfig {
	if cq == nil || cq.Spec.PreemptionConfigName == "" {
		return nil
	}
	return s.PreemptionConfigs[cq.Spec.PreemptionConfigName]
}

// findAssignment recursively assigns one flavor per resource group.
func (s *Scheduler) findAssignment(w *api.Workload, cq *api.ClusterQueue, removed map[string]bool) (*api.Admission, bool) {
	return s.findAssignmentWithTopology(w, cq, removed, true)
}

// findAssignmentWithoutTopology checks whether the quota ledger alone can
// accommodate the workload. The scheduler uses it to distinguish
// InsufficientQuota from QuotaFeasibleAndInsufficientTopology when choosing a
// configurable-preemption trigger.
func (s *Scheduler) findAssignmentWithoutTopology(w *api.Workload, cq *api.ClusterQueue, removed map[string]bool) (*api.Admission, bool) {
	return s.findAssignmentWithTopology(w, cq, removed, false)
}

func (s *Scheduler) findAssignmentWithTopology(w *api.Workload, cq *api.ClusterQueue, removed map[string]bool, requireTopology bool) (*api.Admission, bool) {
	usage := w.TotalUsage()
	assignments := make([]api.FlavorAssignment, 0, len(cq.Spec.ResourceGroups))
	var topology []api.TopologyAssignment

	var walk func(groupIndex int) bool
	walk = func(groupIndex int) bool {
		if groupIndex == len(cq.Spec.ResourceGroups) {
			if !requireTopology {
				topology = nil
				return true
			}
			planned, ok := s.planTopology(w, cq, assignments, removed)
			if !ok {
				return false
			}
			topology = planned
			return true
		}
		group := cq.Spec.ResourceGroups[groupIndex]
		requested := api.ResourceList{}
		for _, r := range group.CoveredResources {
			if q := usage.Get(r); q > 0 {
				requested[r] = q
			}
		}
		if requested.IsZero() {
			return walk(groupIndex + 1)
		}
		for _, fq := range group.Flavors {
			if !s.canFitFlavor(cq, fq, requested, removed) {
				continue
			}
			assignments = append(assignments, api.FlavorAssignment{
				GroupIndex: groupIndex,
				Flavor:     fq.Name,
				Resources:  requested.Clone(),
			})
			if walk(groupIndex + 1) {
				return true
			}
			assignments = assignments[:len(assignments)-1]
		}
		return false
	}

	if !walk(0) {
		return nil, false
	}
	return &api.Admission{
		ClusterQueue:        cq.Name,
		FlavorAssignments:   assignments,
		Usage:               usage,
		TopologyAssignments: topology,
	}, true
}

// canFitFlavor checks one resource group assignment against the CQ and, when
// present, its whole cohort tree. The check logic lives in evaluateFlavor so
// the diagnostics path and the admission path cannot drift apart.
func (s *Scheduler) canFitFlavor(cq *api.ClusterQueue, fq api.FlavorQuota, requested api.ResourceList, removed map[string]bool) bool {
	return s.evaluateFlavor(cq, fq, requested, removed, false).ok
}

// findPreemption builds a victim set that makes an assignment feasible.
func (s *Scheduler) findPreemption(w *api.Workload, cq *api.ClusterQueue, usage api.ResourceList, trigger api.PreemptionTrigger) (*api.Admission, []*api.Workload, bool) {
	candidates := s.preemptionCandidates(w, cq, usage, trigger)
	if len(candidates) == 0 {
		return nil, nil, false
	}
	removed := map[string]bool{}
	selected := make([]*api.Workload, 0, len(candidates))
	var admission *api.Admission
	for _, victim := range candidates {
		if victim.Key() == w.Key() {
			continue
		}
		removed[victim.Key()] = true
		selected = append(selected, victim)
		if adm, ok := s.findAssignment(w, cq, removed); ok {
			admission = adm
			break
		}
	}
	if admission == nil {
		return nil, nil, false
	}

	// Minimal target set: walk backwards and keep a victim only if the
	// admission is still feasible without it.
	for i := len(selected) - 1; i >= 0; i-- {
		key := selected[i].Key()
		delete(removed, key)
		if adm, ok := s.findAssignment(w, cq, removed); ok {
			selected = append(selected[:i], selected[i+1:]...)
			admission = adm
		} else {
			removed[key] = true
		}
	}
	if len(selected) == 0 {
		return nil, nil, false
	}
	// If the full assignment is feasible without any victim, direct admission
	// should have succeeded; keep the invariant explicit.
	if adm, ok := s.findAssignment(w, cq, nil); ok {
		return adm, nil, true
	}
	return admission, selected, true
}

// findFairSharingPreemption implements the two rules from the Fair Sharing
// KEP at a deliberately coarse granularity. It is only attempted when classic
// and configurable candidates cannot make room. Candidates are admitted
// workloads in the same cohort tree with a lower-or-equal priority and a
// positive DRS; the preemptor may take their quota only if its post-admission
// share obeys one of the configured strategies.
func (s *Scheduler) findFairSharingPreemption(w *api.Workload, cq *api.ClusterQueue, usage api.ResourceList) (*api.Admission, []*api.Workload, bool) {
	if len(s.FairSharingPreemption) == 0 {
		return nil, nil, false
	}
	root := s.rootCohort(cq)
	if root == "" {
		return nil, nil, false
	}
	incomingPriority := w.EffectivePriority(s.priorityClasses())
	var candidates []*api.Workload
	for _, victim := range s.Cache.Admitted() {
		if victim.Key() == w.Key() || victim.Status.Admission == nil {
			continue
		}
		vcq, ok := s.View.ClusterQueues[victim.Status.Admission.ClusterQueue]
		if !ok || s.rootCohort(vcq) != root {
			continue
		}
		if victim.EffectivePriority(s.priorityClasses()) > incomingPriority {
			continue
		}
		if s.FairShare(vcq.Name) <= 0 {
			// A queue that is inside its nominal quota has no borrowed share
			// for fair sharing to reclaim. Classic preemption may still apply.
			continue
		}
		candidates = append(candidates, victim)
	}
	if len(candidates) == 0 {
		return nil, nil, false
	}
	sort.SliceStable(candidates, func(i, j int) bool {
		pi := candidates[i].EffectivePriority(s.priorityClasses())
		pj := candidates[j].EffectivePriority(s.priorityClasses())
		if pi != pj {
			return pi < pj
		}
		if candidates[i].Status.AdmittedAt != candidates[j].Status.AdmittedAt {
			return candidates[i].Status.AdmittedAt > candidates[j].Status.AdmittedAt
		}
		return candidates[i].Key() < candidates[j].Key()
	})

	removed := map[string]bool{}
	selected := make([]*api.Workload, 0, len(candidates))
	var admission *api.Admission
	for _, victim := range candidates {
		removed[victim.Key()] = true
		selected = append(selected, victim)
		adm, ok := s.findAssignment(w, cq, removed)
		if !ok {
			continue
		}
		if s.fairShareAllows(w, cq, adm, victim, removed) {
			admission = adm
			break
		}
	}
	if admission == nil {
		return nil, nil, false
	}
	// Minimize the target set with the same reverse walk used by classic
	// preemption, re-checking the fair-sharing predicate for each removal.
	for i := len(selected) - 1; i >= 0; i-- {
		key := selected[i].Key()
		delete(removed, key)
		if adm, ok := s.findAssignment(w, cq, removed); ok && s.fairShareAllows(w, cq, adm, selected[i], removed) {
			selected = append(selected[:i], selected[i+1:]...)
			admission = adm
		} else {
			removed[key] = true
		}
	}
	if len(selected) == 0 {
		return nil, nil, false
	}
	if adm, ok := s.findAssignment(w, cq, nil); ok {
		return adm, nil, true
	}
	return admission, selected, true
}

func (s *Scheduler) fairShareAllows(w *api.Workload, cq *api.ClusterQueue, admission *api.Admission,
	victim *api.Workload, removed map[string]bool) bool {

	vcq, ok := s.View.ClusterQueues[victim.Status.Admission.ClusterQueue]
	if !ok {
		return false
	}
	targetOld := s.FairShare(vcq.Name)
	targetNew := s.fairShareValue(vcq.Name, nil, removed)
	preemptorNew := s.fairShareValue(cq.Name, admission, removed)
	for _, strategy := range s.FairSharingPreemption {
		switch strategy {
		case "LessThanOrEqualToFinalShare":
			if preemptorNew <= targetNew+1e-9 {
				return true
			}
		case "LessThanInitialShare":
			if preemptorNew < targetOld-1e-9 {
				return true
			}
		}
	}
	return false
}

// fairShareValue is FairShare with an optional hypothetical admission and a
// set of workloads to exclude. The admission's flavor assignments determine
// which flavor ledger receives the extra usage.
func (s *Scheduler) fairShareValue(cqName string, admission *api.Admission, removed map[string]bool) float64 {
	cq, ok := s.View.ClusterQueues[cqName]
	if !ok {
		return 0
	}
	root := s.rootCohort(cq)
	if root == "" {
		return 0
	}
	extra := map[string]api.ResourceList{}
	if admission != nil {
		for _, fa := range admission.FlavorAssignments {
			if extra[fa.Flavor] == nil {
				extra[fa.Flavor] = api.ResourceList{}
			}
			extra[fa.Flavor].Add(fa.Resources)
		}
	}
	members := s.cohortMembers(root)
	best := 0.0
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			for r := range fq.Nominal {
				nominal := s.nominal(cqName, fq.Name, r)
				usage := s.Cache.FlavorUsageExcluding([]string{cqName}, fq.Name, r, removed)
				usage += extra[fq.Name].Get(r)
				borrowed := usage - nominal
				if borrowed <= 0 {
					continue
				}
				lendable := s.Cache.FairShareLendable(members, cqName, fq.Name, r, removed,
					func(name, flavor string, resource api.ResourceName) int64 {
						return s.nominal(name, flavor, resource)
					},
					func(name, flavor string, resource api.ResourceName) (int64, bool) {
						return s.effectiveLendingLimit(name, flavor, resource)
					})
				lendable += s.cohortSharedFree(root, fq.Name, r, removed)
				ratio := float64(borrowed)
				if lendable > 0 {
					ratio = float64(borrowed) / float64(lendable)
				}
				if ratio > best {
					best = ratio
				}
			}
		}
	}
	return best / s.fairShareWeight(cq)
}

// fairShareWeight multiplies the ClusterQueue weight by the weight of every
// explicit ancestor cohort. Kueue's fair sharing gives a heavier subtree a
// larger nominal share, so the same absolute usage translates into a smaller
// dominant-resource share. Implicit cohorts have no object and therefore no
// weight.
func (s *Scheduler) fairShareWeight(cq *api.ClusterQueue) float64 {
	weight := 1.0
	if cq.Spec.FairSharing != nil && cq.Spec.FairSharing.Weight > 0 {
		weight *= cq.Spec.FairSharing.Weight
	}
	for _, name := range s.cohortPath(cq) {
		cohort, ok := s.View.Cohorts[name]
		if ok && cohort.FairSharing != nil && cohort.FairSharing.Weight > 0 {
			weight *= cohort.FairSharing.Weight
		}
	}
	return weight
}

func (s *Scheduler) preemptionCandidates(w *api.Workload, cq *api.ClusterQueue, usage api.ResourceList, trigger api.PreemptionTrigger) []*api.Workload {
	root := s.rootCohort(cq)
	incomingBorrows := s.wouldBorrow(cq, usage)
	sameCQMode := cq.Spec.Preemption.WithinClusterQueue
	cohortMode := cq.Spec.Preemption.ReclaimWithinCohort
	borrowCfg := cq.Spec.Preemption.BorrowWithinCohort
	allowCohort := false
	if root != "" {
		if incomingBorrows {
			allowCohort = borrowCfg != nil && borrowCfg.Policy != "" && borrowCfg.Policy != api.PreemptNever
			if allowCohort {
				cohortMode = borrowCfg.Policy
			}
		} else {
			allowCohort = cohortMode != "" && cohortMode != api.PreemptNever
		}
	}

	var out []*api.Workload
	inPriority := w.EffectivePriority(s.priorityClasses())
	for _, victim := range s.Cache.Admitted() {
		if victim.Key() == w.Key() || victim.Status.Admission == nil {
			continue
		}
		vcq, ok := s.View.ClusterQueues[victim.Status.Admission.ClusterQueue]
		if !ok {
			continue
		}
		priority := victim.EffectivePriority(s.priorityClasses())
		if vcq.Name == cq.Name {
			if s.preemptible(sameCQMode, victim, w, priority, inPriority) {
				out = append(out, victim)
			}
			continue
		}
		if !allowCohort || s.rootCohort(vcq) != root {
			continue
		}
		if !s.isBorrowing(vcq, usage) {
			continue
		}
		if !s.preemptible(cohortMode, victim, w, priority, inPriority) {
			continue
		}
		if incomingBorrows && borrowCfg != nil && borrowCfg.MaxPriorityThreshold != nil &&
			priority > *borrowCfg.MaxPriorityThreshold {
			continue
		}
		out = append(out, victim)
	}
	seen := make(map[string]bool, len(out))
	for _, victim := range out {
		seen[victim.Key()] = true
	}
	for _, victim := range s.configurableCandidates(w, cq, trigger) {
		if !seen[victim.Key()] {
			seen[victim.Key()] = true
			out = append(out, victim)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		bi, bj := s.isBorrowing(s.View.ClusterQueues[out[i].Status.Admission.ClusterQueue], usage),
			s.isBorrowing(s.View.ClusterQueues[out[j].Status.Admission.ClusterQueue], usage)
		if bi != bj {
			return bi
		}
		pi := out[i].EffectivePriority(s.priorityClasses())
		pj := out[j].EffectivePriority(s.priorityClasses())
		if pi != pj {
			return pi < pj
		}
		if out[i].Status.AdmittedAt != out[j].Status.AdmittedAt {
			return out[i].Status.AdmittedAt > out[j].Status.AdmittedAt
		}
		if out[i].Status.Sequence != out[j].Status.Sequence {
			return out[i].Status.Sequence > out[j].Status.Sequence
		}
		return out[i].Key() < out[j].Key()
	})
	return out
}

// configurableCandidates evaluates the alpha PreemptionConfig attached to a
// ClusterQueue. Rules are additive and independent of classic preemption:
// their candidates are merged into the classic candidate list before the
// minimal victim set is computed. This is intentionally narrower than real
// Kueue (no preemptor selectors, no multi-rule precedence), but it preserves
// the two properties operators care about: scope selection and asymmetric,
// declarative rules.
func (s *Scheduler) configurableCandidates(w *api.Workload, cq *api.ClusterQueue, trigger api.PreemptionTrigger) []*api.Workload {
	config := s.configFor(cq)
	if config == nil {
		return nil
	}
	var out []*api.Workload
	for _, rule := range config.Rules {
		if rule.Trigger != api.TriggerAlways && rule.Trigger != trigger {
			continue
		}
		for _, selector := range rule.CandidateSelectors {
			for _, victim := range s.Cache.Admitted() {
				if victim.Key() == w.Key() || victim.Status.Admission == nil {
					continue
				}
				if s.selectorMatches(selector, w, cq, victim) {
					out = append(out, victim)
				}
			}
		}
	}
	return out
}

func (s *Scheduler) selectorMatches(selector api.PreemptionCandidateSelector, w *api.Workload,
	cq *api.ClusterQueue, victim *api.Workload) bool {

	victimCQ, ok := s.View.ClusterQueues[victim.Status.Admission.ClusterQueue]
	if !ok {
		return false
	}
	root := s.rootCohort(cq)
	switch selector.Scope {
	case api.ScopeWithinLocalQueue:
		if victim.Namespace != w.Namespace || victim.Spec.QueueName != w.Spec.QueueName {
			return false
		}
	case api.ScopeWithinClusterQueue:
		if victimCQ.Name != cq.Name {
			return false
		}
	case api.ScopeWithinParentCohort:
		if root == "" || victimCQ.Name == cq.Name || s.rootCohort(victimCQ) != root {
			return false
		}
	case api.ScopeWithinCohortTree:
		if root == "" || s.rootCohort(victimCQ) != root {
			return false
		}
	case api.ScopeAnyClusterQueue:
		// No scope restriction.
	default:
		return false
	}
	if selector.Priority != nil {
		incoming := int64(w.EffectivePriority(s.priorityClasses()))
		if selector.Priority.Mode == api.PriorityBoosted {
			// Teaching analogue of boosted priority: the preemptor gets a
			// 1000-point bonus for the comparison. The real API computes the
			// boost from numeric labels; see chapter 7.
			incoming += 1000
		}
		victimPriority := int64(victim.EffectivePriority(s.priorityClasses()))
		if !api.CompareInt(victimPriority, incoming, selector.Priority.Comparison) {
			return false
		}
	}
	for _, constraint := range selector.NumericLabels {
		target := constraint.FallbackValue
		if raw, ok := w.Labels[constraint.Key]; ok {
			if parsed, ok := parseLabelInt(raw); ok {
				target = parsed
			}
		}
		value := constraint.FallbackValue
		if raw, ok := victim.Labels[constraint.Key]; ok {
			if parsed, ok := parseLabelInt(raw); ok {
				value = parsed
			}
		}
		if constraint.MinValue != nil && value < *constraint.MinValue {
			return false
		}
		if constraint.MaxValue != nil && value > *constraint.MaxValue {
			return false
		}
		if !api.CompareInt(value, target, constraint.Comparison) {
			return false
		}
	}
	if selector.LabelSelector != nil && !selector.LabelSelector.Matches(victim.Labels) {
		return false
	}
	if selector.ClusterQueueSelector != nil && !selector.ClusterQueueSelector.Matches(victimCQ.Labels) {
		return false
	}
	return true
}

func parseLabelInt(value string) (int64, bool) {
	parsed, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64)
	if err != nil {
		return 0, false
	}
	return parsed, true
}

func (s *Scheduler) preemptible(mode api.PreemptionMode, victim, incoming *api.Workload, victimPriority, incomingPriority int32) bool {
	switch mode {
	case api.PreemptAny:
		if victimPriority != incomingPriority {
			return true
		}
		return olderWins(victim, incoming)
	case api.PreemptLowerOrNewerEqual:
		if victimPriority < incomingPriority {
			return true
		}
		if victimPriority > incomingPriority {
			return false
		}
		// Equal-priority preemption must be acyclic. We use creation order as
		// a stable tie-break: the older workload wins.
		return olderWins(victim, incoming)
	case api.PreemptLower:
		return victimPriority < incomingPriority
	default:
		return false
	}
}

func olderWins(victim, incoming *api.Workload) bool {
	if victim.Spec.CreationTimestamp != incoming.Spec.CreationTimestamp {
		return victim.Spec.CreationTimestamp > incoming.Spec.CreationTimestamp
	}
	return victim.Key() > incoming.Key()
}

func (s *Scheduler) isBorrowing(cq *api.ClusterQueue, requested api.ResourceList) bool {
	if cq == nil {
		return false
	}
	for r := range requested {
		if !s.isCovered(cq, r) {
			continue
		}
		for _, group := range cq.Spec.ResourceGroups {
			for _, fq := range group.Flavors {
				if s.Cache.FlavorUsage(cq.Name, fq.Name, r) > s.nominal(cq.Name, fq.Name, r) {
					return true
				}
			}
		}
	}
	return false
}

func (s *Scheduler) wouldBorrow(cq *api.ClusterQueue, requested api.ResourceList) bool {
	for _, group := range cq.Spec.ResourceGroups {
		groupReq := api.ResourceList{}
		for _, r := range group.CoveredResources {
			if req := requested.Get(r); req > 0 {
				groupReq[r] = req
			}
		}
		if groupReq.IsZero() {
			continue
		}
		canFitNominal := false
		for _, fq := range group.Flavors {
			ok := true
			for r, req := range groupReq {
				if s.Cache.FlavorUsage(cq.Name, fq.Name, r)+req > s.nominal(cq.Name, fq.Name, r) {
					ok = false
					break
				}
			}
			if ok {
				canFitNominal = true
				break
			}
		}
		if !canFitNominal {
			return true
		}
	}
	return false
}

func (s *Scheduler) isCovered(cq *api.ClusterQueue, resource api.ResourceName) bool {
	for _, group := range cq.Spec.ResourceGroups {
		for _, r := range group.CoveredResources {
			if r == resource {
				return true
			}
		}
	}
	return false
}

func (s *Scheduler) priorityClasses() map[string]*api.WorkloadPriorityClass {
	return s.PriorityClasses
}

// FairShare returns a weighted dominant-resource share based on borrowed
// capacity. For every flavor/resource it computes
//
//	borrowed = max(0, usage - effectiveNominal)
//	lendable = unused nominal that peers are willing to lend
//	ratio    = borrowed / lendable
//
// and returns max(ratio) / weight. A queue that stays inside its nominal quota
// has share 0, matching Kueue's principle that fair sharing only arbitrates
// borrowable capacity. This is still a simplification: real Kueue also uses
// AlmostLCA for hierarchical cohorts and tracks admission history.
func (s *Scheduler) FairShare(cqName string) float64 {
	cq, ok := s.View.ClusterQueues[cqName]
	if !ok {
		return 0
	}
	root := s.rootCohort(cq)
	if root == "" {
		return 0
	}
	members := s.cohortMembers(root)
	best := 0.0
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			for r := range fq.Nominal {
				nominal := s.nominal(cqName, fq.Name, r)
				usage := s.Cache.FlavorUsage(cqName, fq.Name, r)
				borrowed := usage - nominal
				if borrowed <= 0 {
					continue
				}
				lendable := s.Cache.FairShareLendable(members, cqName, fq.Name, r, nil,
					func(name, flavor string, resource api.ResourceName) int64 {
						return s.nominal(name, flavor, resource)
					},
					func(name, flavor string, resource api.ResourceName) (int64, bool) {
						return s.effectiveLendingLimit(name, flavor, resource)
					})
				lendable += s.cohortSharedFree(root, fq.Name, r, nil)
				ratio := float64(borrowed)
				if lendable > 0 {
					ratio = float64(borrowed) / float64(lendable)
				}
				if ratio > best {
					best = ratio
				}
			}
		}
	}
	return best / s.fairShareWeight(cq)
}

// CQUsage returns aggregate usage, exposed for diagnostics.
func (s *Scheduler) CQUsage(cqName string) api.ResourceList {
	return s.Cache.CQUsage(cqName)
}

// WhyNotAdmitted returns a human-readable explanation for a failed decision.
func WhyNotAdmitted(d Decision) string {
	if d.Admission != nil {
		return "admissible"
	}
	if d.Detail != "" {
		return d.Detail
	}
	if d.Reason != "" {
		return d.Reason
	}
	return "insufficient quota"
}

// FormatAdmission returns a compact debug representation of an admission.
func FormatAdmission(a *api.Admission) string {
	if a == nil {
		return "<nil>"
	}
	parts := make([]string, 0, len(a.FlavorAssignments))
	for _, fa := range a.FlavorAssignments {
		parts = append(parts, fmt.Sprintf("g%d:%s%s", fa.GroupIndex, fa.Flavor, fa.Resources.String()))
	}
	return fmt.Sprintf("cq=%s usage=%s flavors=[%s]", a.ClusterQueue, a.Usage.String(), join(parts, ", "))
}

func join(parts []string, sep string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += sep
		}
		out += p
	}
	return out
}

// planTopology places TAS-managed PodSets on topology domains. It is called
// once a full flavor assignment has been found. The algorithm is deliberately
// structural rather than heuristic:
//
//   - Required: every pod of the PodSet must fit in one domain at the level.
//   - Preferred: try that; if impossible, fall back to unconstrained placement.
//   - Unconstrained: spread pods across domains, first-fit in sorted order.
//   - Group: PodSets sharing a group must use the same domain.
//
// Real Kueue derives domain capacity from Nodes and running Pods and has a
// richer packing/defragmentation heuristic. MiniKueue models leaf domains
// explicitly so the decision is deterministic and unit-testable.
func (s *Scheduler) planTopology(w *api.Workload, cq *api.ClusterQueue,
	assignments []api.FlavorAssignment, removed map[string]bool) ([]api.TopologyAssignment, bool) {

	type group struct {
		key       string
		topology  string
		level     string
		required  bool
		anyPod    bool
		pods      []int
		request   api.ResourceList
		podCount  int64
		sliceSize int32
		spreading bool
	}

	groups := map[string]*group{}
	var order []string
	for i, ps := range w.Spec.PodSets {
		count := w.EffectiveCount(ps)
		if ps.Topology == nil || count == 0 {
			continue
		}
		topologyName, ok := s.topologyForPodSet(cq, assignments, w, ps)
		if !ok {
			if ps.Topology.Mode == api.TopologyRequired {
				return nil, false
			}
			continue
		}
		topo, ok := s.View.Topologies[topologyName]
		if !ok {
			if ps.Topology.Mode == api.TopologyRequired {
				return nil, false
			}
			continue
		}
		level := ps.Topology.Level
		if level == "" {
			level = topo.Levels[len(topo.Levels)-1]
		}
		if topologyLevelIndex(topo, level) < 0 {
			if ps.Topology.Mode == api.TopologyRequired {
				return nil, false
			}
			continue
		}
		key := "podset:" + ps.Name
		if ps.Topology.Group != "" {
			key = "group:" + ps.Topology.Group
		}
		g := groups[key]
		if g == nil {
			g = &group{key: key, topology: topologyName, level: level, request: api.ResourceList{}}
			groups[key] = g
			order = append(order, key)
		} else if g.topology != topologyName || g.level != level {
			// A group must live in one topology level; otherwise "same domain"
			// has no well-defined meaning.
			return nil, false
		}
		// A named group always means "these PodSets share one domain", so it
		// behaves like Required even if an individual PodSet says Preferred or
		// Unconstrained. This matches the semantics of podset-group-name.
		g.required = g.required || ps.Topology.Mode == api.TopologyRequired || ps.Topology.Group != ""
		g.anyPod = g.anyPod || ps.Topology.Mode == api.TopologyPreferred
		if ps.Topology.SliceSize > 0 {
			if g.sliceSize != 0 && g.sliceSize != ps.Topology.SliceSize {
				// Mixed slice sizes inside one topology group have no
				// well-defined packing.
				return nil, false
			}
			g.sliceSize = ps.Topology.SliceSize
		}
		g.spreading = g.spreading || ps.Topology.Spreading
		g.pods = append(g.pods, i)
		g.podCount += int64(count)
		for r, per := range w.PodSetPerPodUsage(ps) {
			if s.topologyTracksResource(topologyName, r) {
				g.request[r] += per * int64(count)
			}
		}
		if s.topologyTracksPods(topologyName) {
			g.request[api.Pods] += int64(count)
		}
	}

	working := map[string]map[string]api.ResourceList{} // topology|level -> domain -> usage
	var out []api.TopologyAssignment

	for _, key := range order {
		g := groups[key]
		usageKey := g.topology + "|" + g.level
		usage := working[usageKey]
		if usage == nil {
			usage = s.TopologyUsage(g.topology, g.level, removed)
			if !s.topologyTracksPods(g.topology) {
				for domain := range usage {
					delete(usage[domain], api.Pods)
				}
			}
			working[usageKey] = usage
		}
		capacity := s.topologyCapacity(g.topology, g.level)
		domains := orderedDomains(capacity, usage, g.spreading)
		for _, domain := range domains {
			if usage[domain] == nil {
				usage[domain] = api.ResourceList{}
			}
		}

		if g.required || g.anyPod {
			// Slicing requires a single PodSet: "slice this PodSet into groups
			// of N pods, each in one domain". Grouping several PodSets and
			// slicing them together has no unambiguous meaning.
			if g.sliceSize > 0 {
				if len(g.pods) != 1 {
					return nil, false
				}
				ps := w.Spec.PodSets[g.pods[0]]
				perPod := w.PodSetPerPodUsage(ps)
				remaining := w.EffectiveCount(ps)
				for remaining > 0 {
					slice := g.sliceSize
					if remaining < slice {
						slice = remaining
					}
					request := scaleResources(perPod, slice)
					if s.topologyTracksPods(g.topology) {
						request[api.Pods] = int64(slice)
					}
					chosen := ""
					for _, domain := range orderedDomains(capacity, usage, true) {
						if free := topologyFree(capacity[domain], usage[domain]); free.Covers(request) {
							chosen = domain
							break
						}
					}
					if chosen == "" {
						return nil, false
					}
					s.addPlacement(working, g.topology, g.level, chosen, request, int64(slice), removed)
					out = append(out, api.TopologyAssignment{
						Topology:  g.topology,
						PodSet:    ps.Name,
						Level:     g.level,
						Domain:    chosen,
						PodCount:  slice,
						Resources: scaleResources(perPod, slice),
					})
					remaining -= slice
				}
				continue
			}

			chosen := ""
			for _, domain := range domains {
				if free := topologyFree(capacity[domain], usage[domain]); free.Covers(g.request) {
					chosen = domain
					break
				}
			}
			if chosen == "" {
				if g.required {
					return nil, false
				}
				// Preferred is a hint: fall back to no locality.
				continue
			}
			s.addPlacement(working, g.topology, g.level, chosen, g.request, g.podCount, removed)
			for _, idx := range g.pods {
				ps := w.Spec.PodSets[idx]
				count := w.EffectiveCount(ps)
				out = append(out, api.TopologyAssignment{
					Topology:  g.topology,
					PodSet:    ps.Name,
					Level:     g.level,
					Domain:    chosen,
					PodCount:  count,
					Resources: scaleResources(w.PodSetPerPodUsage(ps), count),
				})
			}
			continue
		}

		// Unconstrained / group with no locality requirement: spread each
		// PodSet independently. A group only constrains Required/Preferred.
		for _, idx := range g.pods {
			ps := w.Spec.PodSets[idx]
			perPod := w.PodSetPerPodUsage(ps)
			remaining := w.EffectiveCount(ps)
			for _, domain := range domains {
				if remaining == 0 {
					break
				}
				if _, known := capacity[domain]; !known {
					// Usage can mention a domain that no longer exists (for
					// example after a topology edit or a stale reservation).
					// Such a domain has no capacity and must never be used as
					// free space; otherwise the resource loop would skip every
					// untracked resource and place the whole PodSet there.
					continue
				}
				free := topologyFree(capacity[domain], usage[domain])
				place := remaining
				for r, per := range perPod {
					if per <= 0 {
						continue
					}
					if _, tracked := free[r]; !tracked {
						// A resource that no domain reports is not a TAS
						// capacity dimension; quota already accounts for it.
						continue
					}
					if room := free.Get(r) / per; room < int64(place) {
						place = int32(room)
					}
				}
				if s.topologyTracksPods(g.topology) {
					if room := free.Get(api.Pods); room < int64(place) {
						place = int32(room)
					}
				}
				if place <= 0 {
					continue
				}
				resources := scaleResources(perPod, place)
				s.addPlacement(working, g.topology, g.level, domain, resources, int64(place), removed)
				out = append(out, api.TopologyAssignment{
					Topology:  g.topology,
					PodSet:    ps.Name,
					Level:     g.level,
					Domain:    domain,
					PodCount:  place,
					Resources: resources,
				})
				remaining -= place
			}
			if remaining > 0 {
				return nil, false
			}
		}
	}
	return out, true
}

// topologyForPodSet finds the topology of the flavor assigned to a PodSet.
// A PodSet spanning several resource groups may produce several flavors; the
// first topology-capable flavor wins.
func (s *Scheduler) topologyForPodSet(cq *api.ClusterQueue, assignments []api.FlavorAssignment, w *api.Workload, ps api.PodSet) (string, bool) {
	perPod := w.PodSetPerPodUsage(ps)
	for _, fa := range assignments {
		if fa.GroupIndex < 0 || fa.GroupIndex >= len(cq.Spec.ResourceGroups) {
			continue
		}
		group := cq.Spec.ResourceGroups[fa.GroupIndex]
		covers := false
		for _, r := range group.CoveredResources {
			if perPod.Get(r) > 0 {
				covers = true
				break
			}
		}
		if !covers {
			continue
		}
		flavor, ok := s.View.Flavors[fa.Flavor]
		if ok && flavor.TopologyName != "" {
			return flavor.TopologyName, true
		}
	}
	return "", false
}

func topologyLevelIndex(t *api.Topology, level string) int {
	for i, name := range t.Levels {
		if name == level {
			return i
		}
	}
	return -1
}

// topologyCapacity aggregates leaf capacity by the prefix that identifies a
// domain at the requested level.
func (s *Scheduler) topologyCapacity(topology, level string) map[string]api.ResourceList {
	out := map[string]api.ResourceList{}
	topo, ok := s.View.Topologies[topology]
	if !ok {
		return out
	}
	levelIndex := topologyLevelIndex(topo, level)
	if levelIndex < 0 {
		return out
	}
	for _, d := range s.View.TopologyDoms[topology] {
		if len(d.Values) != len(topo.Levels) {
			continue
		}
		key := strings.Join(d.Values[:levelIndex+1], "/")
		if out[key] == nil {
			out[key] = api.ResourceList{}
		}
		out[key].Add(d.Capacity)
	}
	return out
}

// TopologyUsage returns a physical view of TAS reservations at the requested
// level. Assignments are recorded at the level named by the annotation, but a
// coarse-level reservation occupies physical capacity at every finer level and
// a fine-level reservation consumes aggregate capacity at every coarser
// level. Returning only exact-level assignments would let a rack-level job and
// a hostname-level job double-book the same GPUs.
func (s *Scheduler) TopologyUsage(topology, level string, excluded map[string]bool) map[string]api.ResourceList {
	out := map[string]api.ResourceList{}
	topo, ok := s.View.Topologies[topology]
	if !ok {
		return out
	}
	targetIdx := topologyLevelIndex(topo, level)
	if targetIdx < 0 {
		return out
	}
	for _, w := range s.Cache.Admitted() {
		if excluded[w.Key()] || w.Status.Admission == nil {
			continue
		}
		for _, ta := range w.Status.Admission.TopologyAssignments {
			if ta.Topology != topology {
				continue
			}
			sourceIdx := topologyLevelIndex(topo, ta.Level)
			if sourceIdx < 0 {
				continue
			}
			resources, pods := splitPods(ta.Resources)
			if ta.PodCount > 0 && pods == 0 {
				pods = int64(ta.PodCount)
			}
			switch {
			case sourceIdx == targetIdx:
				addTopologyUsage(out, ta.Domain, resources, pods)
			case sourceIdx > targetIdx:
				// Finer assignment: aggregate into its ancestor.
				addTopologyUsage(out, truncateDomain(ta.Domain, targetIdx), resources, pods)
			default:
				// Coarser assignment: distribute into the target-level
				// subdomains under the recorded domain.
				for domain, share := range s.distributeTopologyUsage(topo, sourceIdx, ta.Domain, targetIdx, resources, pods) {
					addTopologyUsage(out, domain, share.resources, share.pods)
				}
			}
		}
	}
	return out
}

type topologyShare struct {
	resources api.ResourceList
	pods      int64
}

// addPlacement records a placement in the working ledgers for every level of
// the topology, so later PodSets in the same scheduling decision see the
// physical capacity consumed by earlier PodSets even when they use a different
// constraint level.
func (s *Scheduler) addPlacement(working map[string]map[string]api.ResourceList, topology, level, domain string,
	resources api.ResourceList, podCount int64, removed map[string]bool) {

	topo, ok := s.View.Topologies[topology]
	if !ok {
		return
	}
	sourceIdx := topologyLevelIndex(topo, level)
	if sourceIdx < 0 {
		return
	}
	res, pods := splitPods(resources)
	if podCount > 0 {
		pods = podCount
	}
	// Only ledgers whose domains track the pods resource should accumulate a
	// pod count; otherwise spreading scores would change for topologies that
	// only model GPUs/CPU.
	usagePods := func(value int64) int64 {
		if s.topologyTracksPods(topology) {
			return value
		}
		return 0
	}
	for targetIdx, targetLevel := range topo.Levels {
		key := topology + "|" + targetLevel
		usage := working[key]
		if usage == nil {
			usage = s.TopologyUsage(topology, targetLevel, removed)
			working[key] = usage
		}
		switch {
		case targetIdx == sourceIdx:
			addTopologyUsage(usage, domain, res, usagePods(pods))
		case targetIdx > sourceIdx:
			for d, share := range s.distributeTopologyUsage(topo, sourceIdx, domain, targetIdx, res, pods) {
				addTopologyUsage(usage, d, share.resources, usagePods(share.pods))
			}
		default:
			addTopologyUsage(usage, truncateDomain(domain, targetIdx), res, usagePods(pods))
		}
	}
}

// distributeTopologyUsage splits a coarser-level reservation across target
// subdomains proportionally to their capacity. Resources and pods are kept
// consistent: pods are distributed first, then each subdomain receives
// per-pod resources times its pod share.
func (s *Scheduler) distributeTopologyUsage(topo *api.Topology, sourceIdx int, sourceDomain string,
	targetIdx int, resources api.ResourceList, pods int64) map[string]topologyShare {

	caps := s.topologySubdomains(topo.Name, sourceIdx, sourceDomain, targetIdx)
	if len(caps) == 0 {
		return map[string]topologyShare{sourceDomain: {resources: resources.Clone(), pods: pods}}
	}
	names := make([]string, 0, len(caps))
	scores := make(map[string]float64, len(caps))
	totalScore := 0.0
	for name, capacity := range caps {
		names = append(names, name)
		score := domainUsageScore(capacity)
		if score <= 0 {
			score = 1
		}
		scores[name] = score
		totalScore += score
	}
	sort.Strings(names)

	out := make(map[string]topologyShare, len(names))
	if pods > 0 {
		shares := largestRemainder(pods, names, scores, totalScore)
		for _, name := range names {
			count := shares[name]
			if count <= 0 {
				continue
			}
			out[name] = topologyShare{resources: api.ResourceList{}, pods: count}
		}
		// Per-pod resources. TAS requests are normally divisible by PodCount;
		// any integer remainder is assigned to the first non-empty share so the
		// total is preserved.
		for r, value := range resources {
			if r == api.Pods {
				continue
			}
			per := value / pods
			rem := value % pods
			first := true
			for _, name := range names {
				share, ok := out[name]
				if !ok || share.pods <= 0 {
					continue
				}
				share.resources[r] = per * share.pods
				if first && rem != 0 {
					share.resources[r] += rem
					first = false
				}
				out[name] = share
			}
		}
		return out
	}

	// No pod count: distribute each resource independently by capacity.
	for r, value := range resources {
		if r == api.Pods {
			continue
		}
		shares := largestRemainder(value, names, scores, totalScore)
		for _, name := range names {
			if shares[name] <= 0 {
				continue
			}
			share := out[name]
			if share.resources == nil {
				share.resources = api.ResourceList{}
			}
			share.resources[r] += shares[name]
			out[name] = share
		}
	}
	return out
}

func (s *Scheduler) topologySubdomains(topology string, sourceIdx int, sourceDomain string, targetIdx int) map[string]api.ResourceList {
	out := map[string]api.ResourceList{}
	topo, ok := s.View.Topologies[topology]
	if !ok || sourceIdx < 0 || targetIdx <= sourceIdx || targetIdx >= len(topo.Levels) {
		return out
	}
	for _, d := range s.View.TopologyDoms[topology] {
		if len(d.Values) != len(topo.Levels) {
			continue
		}
		domain := strings.Join(d.Values, "/")
		if truncateDomain(domain, sourceIdx) != sourceDomain {
			continue
		}
		key := truncateDomain(domain, targetIdx)
		if out[key] == nil {
			out[key] = api.ResourceList{}
		}
		out[key].Add(d.Capacity)
	}
	return out
}

func largestRemainder(total int64, names []string, scores map[string]float64, totalScore float64) map[string]int64 {
	out := make(map[string]int64, len(names))
	if total <= 0 || len(names) == 0 {
		return out
	}
	type remainder struct {
		name  string
		value float64
	}
	rems := make([]remainder, 0, len(names))
	var allocated int64
	for _, name := range names {
		exact := float64(total) * scores[name] / totalScore
		floor := int64(math.Floor(exact))
		out[name] = floor
		allocated += floor
		rems = append(rems, remainder{name: name, value: exact - math.Floor(exact)})
	}
	sort.SliceStable(rems, func(i, j int) bool {
		if rems[i].value != rems[j].value {
			return rems[i].value > rems[j].value
		}
		return rems[i].name < rems[j].name
	})
	for i := 0; allocated < total; i++ {
		name := rems[i%len(rems)].name
		out[name]++
		allocated++
	}
	return out
}

func truncateDomain(domain string, levelIdx int) string {
	if levelIdx < 0 {
		return ""
	}
	parts := strings.Split(domain, "/")
	if levelIdx+1 >= len(parts) {
		return domain
	}
	return strings.Join(parts[:levelIdx+1], "/")
}

func addTopologyUsage(out map[string]api.ResourceList, domain string, resources api.ResourceList, pods int64) {
	if out[domain] == nil {
		out[domain] = api.ResourceList{}
	}
	out[domain].Add(resources)
	if pods > 0 {
		out[domain][api.Pods] += pods
	}
}

func splitPods(resources api.ResourceList) (api.ResourceList, int64) {
	out := resources.Clone()
	if out == nil {
		out = api.ResourceList{}
	}
	pods := out.Get(api.Pods)
	delete(out, api.Pods)
	return out, pods
}

func (s *Scheduler) topologyTracksPods(topology string) bool {
	return s.topologyTracksResource(topology, api.Pods)
}

func (s *Scheduler) topologyTracksResource(topology string, resource api.ResourceName) bool {
	for _, d := range s.View.TopologyDoms[topology] {
		if _, ok := d.Capacity[resource]; ok {
			return true
		}
	}
	return false
}

func sortedDomainNames(capacity map[string]api.ResourceList, usage map[string]api.ResourceList) []string {
	seen := map[string]bool{}
	var names []string
	for name := range capacity {
		if !seen[name] {
			seen[name] = true
			names = append(names, name)
		}
	}
	for name := range usage {
		if !seen[name] {
			seen[name] = true
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

// orderedDomains returns domain names in placement order. With spreading the
// least-used domain comes first, approximating podset-topology-spreading; the
// name is the stable tie-breaker.
func orderedDomains(capacity map[string]api.ResourceList, usage map[string]api.ResourceList, spreading bool) []string {
	names := sortedDomainNames(capacity, usage)
	if !spreading {
		return names
	}
	sort.SliceStable(names, func(i, j int) bool {
		ui, uj := domainUsageScore(usage[names[i]]), domainUsageScore(usage[names[j]])
		if ui != uj {
			return ui < uj
		}
		return names[i] < names[j]
	})
	return names
}

func domainUsageScore(r api.ResourceList) float64 {
	var score float64
	for name, value := range r {
		switch name {
		case api.CPU:
			score += float64(value) / 1000
		case api.Memory:
			score += float64(value) / 1024
		default:
			score += float64(value)
		}
	}
	return score
}

func topologyFree(capacity, usage api.ResourceList) api.ResourceList {
	free := api.ResourceList{}
	for r, q := range capacity {
		free[r] = q - usage.Get(r)
		if free[r] < 0 {
			free[r] = 0
		}
	}
	return free
}

func scaleResources(requests api.ResourceList, count int32) api.ResourceList {
	out := api.ResourceList{}
	for r, per := range requests {
		out[r] = per * int64(count)
	}
	return out
}

// rootCohort returns the top of the cohort tree. A cohortName without a
// matching Cohort object is an implicit, flat cohort: it still borrows and
// lends within the group, it just has no shared pool, hierarchy or weight.
func (s *Scheduler) rootCohort(cq *api.ClusterQueue) string {
	if cq == nil || cq.Spec.CohortName == "" {
		return ""
	}
	name := cq.Spec.CohortName
	seen := map[string]bool{}
	for name != "" {
		if seen[name] {
			return ""
		}
		seen[name] = true
		c, ok := s.View.Cohorts[name]
		if !ok {
			return name
		}
		if c.ParentName == "" {
			return c.Name
		}
		name = c.ParentName
	}
	return ""
}

// cohortPath returns the explicit Cohort objects from the CQ's cohort up to
// the root. Implicit cohorts contribute no path entry because they have no
// limits or shared pool.
func (s *Scheduler) cohortPath(cq *api.ClusterQueue) []string {
	if cq == nil || cq.Spec.CohortName == "" {
		return nil
	}
	var out []string
	name := cq.Spec.CohortName
	seen := map[string]bool{}
	for name != "" {
		if seen[name] {
			return out
		}
		seen[name] = true
		c, ok := s.View.Cohorts[name]
		if !ok {
			return out
		}
		out = append(out, c.Name)
		name = c.ParentName
	}
	return out
}

// cohortBorrowingLimitAt returns the borrowing limit a cohort places on the
// total amount its subtree may borrow from its parent.
func (s *Scheduler) cohortBorrowingLimitAt(cohortName, flavor string, resource api.ResourceName) (int64, bool) {
	c, ok := s.View.Cohorts[cohortName]
	if !ok {
		return 0, false
	}
	for _, group := range c.ResourceGroups {
		for _, fq := range group.Flavors {
			if fq.Name == flavor {
				limit, ok := fq.BorrowingLimit[resource]
				return limit, ok
			}
		}
	}
	return 0, false
}

// cohortLendingLimitAt returns the lending limit a cohort places on the total
// amount its subtree may lend to siblings.
func (s *Scheduler) cohortLendingLimitAt(cohortName, flavor string, resource api.ResourceName) (int64, bool) {
	c, ok := s.View.Cohorts[cohortName]
	if !ok {
		return 0, false
	}
	for _, group := range c.ResourceGroups {
		for _, fq := range group.Flavors {
			if fq.Name == flavor {
				limit, ok := fq.LendingLimit[resource]
				return limit, ok
			}
		}
	}
	return 0, false
}

// cohortSubtreeBorrowed approximates how much a cohort subtree is currently
// borrowing. It sums each member's positive excess over its own nominal quota;
// it must not net an under-used member against an over-used one, otherwise a
// sibling sitting idle would mask another queue's borrowing and let the
// subtree exceed its cohort-level borrowingLimit. The real implementation
// tracks per-level borrowing; this aggregate is enough to demonstrate that a
// cohort-level borrowingLimit is a subtree-wide cap.
func (s *Scheduler) cohortSubtreeBorrowed(cohortName, flavor string, resource api.ResourceName, removed map[string]bool) int64 {
	var borrowed int64
	for name, cq := range s.View.ClusterQueues {
		if !s.cohortContains(cq, cohortName) {
			continue
		}
		usage := s.Cache.FlavorUsageExcluding([]string{name}, flavor, resource, removed)
		nominal := s.nominal(name, flavor, resource)
		if usage > nominal {
			borrowed += usage - nominal
		}
	}
	return borrowed
}

// cohortContains reports whether cq is a member of cohortName or one of its
// descendants.
func (s *Scheduler) cohortContains(cq *api.ClusterQueue, cohortName string) bool {
	for _, name := range s.cohortPath(cq) {
		if name == cohortName {
			return true
		}
	}
	// Implicit cohorts have no object but still form a flat group.
	if _, ok := s.View.Cohorts[cohortName]; !ok {
		return cq.Spec.CohortName == cohortName
	}
	return false
}

// effectiveLendingLimit lowers a ClusterQueue's lending limit by every
// ancestor cohort's lending limit. A cohort limit is meant to cap the whole
// subtree; applying it per member is conservative and easy to reason about.
func (s *Scheduler) effectiveLendingLimit(cqName, flavor string, resource api.ResourceName) (int64, bool) {
	limit, has := s.lendingLimit(cqName, flavor, resource)
	cq, ok := s.View.ClusterQueues[cqName]
	if !ok {
		return limit, has
	}
	for _, cohort := range s.cohortPath(cq) {
		cohortLimit, cohortHas := s.cohortLendingLimitAt(cohort, flavor, resource)
		if !cohortHas {
			continue
		}
		if !has || cohortLimit < limit {
			limit = cohortLimit
			has = true
		}
	}
	return limit, has
}

func (s *Scheduler) cohortMembers(root string) []string {
	if root == "" {
		return nil
	}
	var out []string
	for name, cq := range s.View.ClusterQueues {
		if s.rootCohort(cq) == root {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

func (s *Scheduler) cohortNominal(root, flavor string, resource api.ResourceName) int64 {
	var total int64
	for _, name := range s.cohortMembers(root) {
		total += s.nominal(name, flavor, resource)
	}
	for name, c := range s.View.Cohorts {
		if s.cohortRoot(name) != root {
			continue
		}
		for _, group := range c.ResourceGroups {
			for _, fq := range group.Flavors {
				if fq.Name == flavor {
					total += fq.Nominal.Get(resource)
				}
			}
		}
	}
	return total
}

func (s *Scheduler) cohortSharedFree(root, flavor string, resource api.ResourceName, removed map[string]bool) int64 {
	var shared, cqNominal int64
	for name, c := range s.View.Cohorts {
		if s.cohortRoot(name) != root {
			continue
		}
		for _, group := range c.ResourceGroups {
			for _, fq := range group.Flavors {
				if fq.Name == flavor {
					shared += fq.Nominal.Get(resource)
				}
			}
		}
	}
	members := s.cohortMembers(root)
	for _, name := range members {
		cqNominal += s.nominal(name, flavor, resource)
	}
	totalUsage := s.Cache.FlavorUsageExcluding(members, flavor, resource, removed)
	sharedUsed := totalUsage - cqNominal
	if sharedUsed < 0 {
		sharedUsed = 0
	}
	free := shared - sharedUsed
	if free < 0 {
		return 0
	}
	return free
}

func (s *Scheduler) cohortRoot(name string) string {
	c, ok := s.View.Cohorts[name]
	if !ok {
		return ""
	}
	seen := map[string]bool{}
	for c.ParentName != "" {
		if seen[c.Name] {
			return ""
		}
		seen[c.Name] = true
		parent, ok := s.View.Cohorts[c.ParentName]
		if !ok {
			return ""
		}
		c = parent
	}
	return c.Name
}

func (s *Scheduler) nominal(cqName, flavor string, resource api.ResourceName) int64 {
	// Dynamic quota orchestration overrides static nominal quota. The engine
	// recomputes effective quotas from CapacityProvider objects before each
	// scheduling cycle.
	if byFlavor, ok := s.View.EffectiveQuotas[cqName]; ok {
		if resources, ok := byFlavor[flavor]; ok {
			if value, ok := resources[resource]; ok {
				return value
			}
		}
	}
	cq, ok := s.View.ClusterQueues[cqName]
	if !ok {
		return 0
	}
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			if fq.Name == flavor {
				return fq.Nominal.Get(resource)
			}
		}
	}
	return 0
}

func (s *Scheduler) lendingLimit(cqName, flavor string, resource api.ResourceName) (int64, bool) {
	cq, ok := s.View.ClusterQueues[cqName]
	if !ok {
		return 0, false
	}
	for _, group := range cq.Spec.ResourceGroups {
		for _, fq := range group.Flavors {
			if fq.Name == flavor {
				value, ok := fq.LendingLimit[resource]
				return value, ok
			}
		}
	}
	return 0, false
}
