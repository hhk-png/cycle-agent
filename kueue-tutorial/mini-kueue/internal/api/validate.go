package api

import (
	"fmt"
	"math"
)

// validWeight rejects negative, NaN and infinite fairness weights. NaN is
// especially dangerous because every comparison with it is false, so it would
// silently corrupt ordering.
func validWeight(weight float64) bool {
	return weight >= 0 && !math.IsNaN(weight) && !math.IsInf(weight, 0)
}

// ValidateClusterQueue checks structural invariants of a ClusterQueue.
func ValidateClusterQueue(cq *ClusterQueue) error {
	if cq == nil || cq.Name == "" {
		return fmt.Errorf("cluster queue name is required")
	}
	if len(cq.Spec.ResourceGroups) == 0 {
		return fmt.Errorf("cluster queue %q has no resource groups", cq.Name)
	}
	seenResource := map[ResourceName]string{}
	seenFlavorName := map[string]int{}
	for gi, group := range cq.Spec.ResourceGroups {
		if len(group.CoveredResources) == 0 {
			return fmt.Errorf("cluster queue %q resource group %d has no covered resources", cq.Name, gi)
		}
		for _, r := range group.CoveredResources {
			if r == "" {
				return fmt.Errorf("cluster queue %q resource group %d has an empty resource name", cq.Name, gi)
			}
			if prev, ok := seenResource[r]; ok {
				return fmt.Errorf("resource %q appears in both group %s and group %d", r, prev, gi)
			}
			seenResource[r] = fmt.Sprintf("%d", gi)
		}
		if len(group.Flavors) == 0 {
			return fmt.Errorf("cluster queue %q resource group %d has no flavors", cq.Name, gi)
		}
		seenFlavor := map[string]bool{}
		for _, fq := range group.Flavors {
			if fq.Name == "" {
				return fmt.Errorf("cluster queue %q resource group %d has an empty flavor name", cq.Name, gi)
			}
			if seenFlavor[fq.Name] {
				return fmt.Errorf("cluster queue %q resource group %d has duplicate flavor %q", cq.Name, gi, fq.Name)
			}
			seenFlavor[fq.Name] = true
			if prev, ok := seenFlavorName[fq.Name]; ok {
				return fmt.Errorf("flavor %q appears in both resource group %d and %d", fq.Name, prev, gi)
			}
			seenFlavorName[fq.Name] = gi
			for _, r := range group.CoveredResources {
				if _, ok := fq.Nominal[r]; !ok {
					return fmt.Errorf("flavor %q does not define nominal quota for covered resource %q", fq.Name, r)
				}
			}
			if fq.Nominal.AnyNegative() {
				return fmt.Errorf("flavor %q has a negative nominal quota", fq.Name)
			}
			if fq.BorrowingLimit.AnyNegative() {
				return fmt.Errorf("flavor %q has a negative borrowing limit", fq.Name)
			}
			if fq.LendingLimit.AnyNegative() {
				return fmt.Errorf("flavor %q has a negative lending limit", fq.Name)
			}
		}
	}
	if cq.Spec.QueueingStrategy == "" {
		cq.Spec.QueueingStrategy = BestEffortFIFO
	}
	switch cq.Spec.QueueingStrategy {
	case StrictFIFO, BestEffortFIFO:
	default:
		return fmt.Errorf("cluster queue %q has unsupported queueing strategy %q", cq.Name, cq.Spec.QueueingStrategy)
	}
	if cq.Spec.StopPolicy == "" {
		cq.Spec.StopPolicy = StopNone
	}
	switch cq.Spec.StopPolicy {
	case StopNone, StopHold, StopHoldAndDrain:
	default:
		return fmt.Errorf("cluster queue %q has unsupported stop policy %q", cq.Name, cq.Spec.StopPolicy)
	}
	if cq.Spec.FairSharing != nil && !validWeight(cq.Spec.FairSharing.Weight) {
		return fmt.Errorf("cluster queue %q has an invalid fair sharing weight", cq.Name)
	}
	seenCheck := map[string]bool{}
	for _, check := range cq.Spec.AdmissionChecks {
		if check == "" {
			return fmt.Errorf("cluster queue %q has an empty admission check name", cq.Name)
		}
		if seenCheck[check] {
			return fmt.Errorf("cluster queue %q lists admission check %q more than once", cq.Name, check)
		}
		seenCheck[check] = true
	}
	switch cq.Spec.AdmissionScope {
	case AdmissionScopeNone, AdmissionScopeUsageBasedAdmissionSharing:
	default:
		return fmt.Errorf("cluster queue %q has unsupported admission scope %q", cq.Name, cq.Spec.AdmissionScope)
	}
	if cq.Spec.ConcurrentAdmission != nil {
		if cq.Spec.QueueingStrategy != BestEffortFIFO {
			return fmt.Errorf("cluster queue %q: concurrent admission requires BestEffortFIFO", cq.Name)
		}
		if len(cq.Spec.ResourceGroups) != 1 {
			return fmt.Errorf("cluster queue %q: concurrent admission requires exactly one resource group", cq.Name)
		}
		if flavors := len(cq.Spec.ResourceGroups[0].Flavors); flavors > 32 {
			return fmt.Errorf("cluster queue %q: concurrent admission supports at most 32 flavors, got %d", cq.Name, flavors)
		}
		mode := cq.Spec.ConcurrentAdmission.Mode
		if mode != "" && mode != "TryPreferredFlavors" {
			return fmt.Errorf("cluster queue %q has unsupported concurrent admission mode %q", cq.Name, mode)
		}
		if last := cq.Spec.ConcurrentAdmission.LastAcceptableFlavorName; last != "" {
			found := false
			for _, fq := range cq.Spec.ResourceGroups[0].Flavors {
				if fq.Name == last {
					found = true
					break
				}
			}
			if !found {
				return fmt.Errorf("cluster queue %q concurrent admission references unknown lastAcceptableFlavorName %q", cq.Name, last)
			}
		}
	}
	covered := map[ResourceName]bool{}
	for _, group := range cq.Spec.ResourceGroups {
		for _, r := range group.CoveredResources {
			covered[r] = true
		}
	}
	for deviceClass, resource := range cq.Spec.DeviceClassMappings {
		if deviceClass == "" {
			return fmt.Errorf("cluster queue %q has an empty device class mapping key", cq.Name)
		}
		if resource == "" {
			return fmt.Errorf("cluster queue %q device class %q maps to an empty resource", cq.Name, deviceClass)
		}
		if resource == Pods {
			return fmt.Errorf("cluster queue %q device class %q maps to the reserved resource %q", cq.Name, deviceClass, Pods)
		}
		if !covered[resource] {
			return fmt.Errorf("cluster queue %q device class %q maps to uncovered resource %q", cq.Name, deviceClass, resource)
		}
	}
	if err := ValidateLabelSelector(cq.Spec.NamespaceSelector); err != nil {
		return fmt.Errorf("cluster queue %q namespaceSelector: %w", cq.Name, err)
	}
	within := cq.Spec.Preemption.WithinClusterQueue
	if within != "" && within != PreemptNever && within != PreemptLower && within != PreemptLowerOrNewerEqual {
		return fmt.Errorf("cluster queue %q has unsupported withinClusterQueue mode %q", cq.Name, within)
	}
	reclaim := cq.Spec.Preemption.ReclaimWithinCohort
	if reclaim != "" && reclaim != PreemptNever && reclaim != PreemptLower && reclaim != PreemptAny {
		return fmt.Errorf("cluster queue %q has unsupported reclaimWithinCohort mode %q", cq.Name, reclaim)
	}
	if borrow := cq.Spec.Preemption.BorrowWithinCohort; borrow != nil {
		if borrow.Policy != "" && borrow.Policy != PreemptNever && borrow.Policy != PreemptLower {
			return fmt.Errorf("cluster queue %q has unsupported borrowWithinCohort policy %q", cq.Name, borrow.Policy)
		}
	}
	return nil
}

// ValidateCohort checks a cohort definition and its parent chain.
func ValidateCohort(c *Cohort, all map[string]*Cohort) error {
	if c == nil || c.Name == "" {
		return fmt.Errorf("cohort name is required")
	}
	if c.ParentName != "" {
		if _, ok := all[c.ParentName]; !ok {
			return fmt.Errorf("cohort %q references missing parent %q", c.Name, c.ParentName)
		}
	}
	if c.FairSharing != nil && !validWeight(c.FairSharing.Weight) {
		return fmt.Errorf("cohort %q has an invalid fair sharing weight", c.Name)
	}
	seenFlavor := map[string]int{}
	for gi, group := range c.ResourceGroups {
		for _, fq := range group.Flavors {
			if fq.Name == "" {
				return fmt.Errorf("cohort %q group %d has an empty flavor name", c.Name, gi)
			}
			if prev, ok := seenFlavor[fq.Name]; ok {
				return fmt.Errorf("cohort %q flavor %q appears in both group %d and %d", c.Name, fq.Name, prev, gi)
			}
			seenFlavor[fq.Name] = gi
			if fq.Nominal.AnyNegative() || fq.BorrowingLimit.AnyNegative() || fq.LendingLimit.AnyNegative() {
				return fmt.Errorf("cohort %q group %d flavor %q has a negative quota", c.Name, gi, fq.Name)
			}
		}
	}
	seen := map[string]bool{c.Name: true}
	cur := c.ParentName
	for cur != "" {
		if seen[cur] {
			return fmt.Errorf("cohort %q participates in a parent cycle", c.Name)
		}
		seen[cur] = true
		parent, ok := all[cur]
		if !ok {
			return fmt.Errorf("cohort %q references missing parent %q", c.Name, cur)
		}
		cur = parent.ParentName
	}
	return nil
}

// ValidateLocalQueue checks that the LocalQueue has a name and target.
func ValidateLocalQueue(q *LocalQueue, clusterQueues map[string]*ClusterQueue) error {
	if q == nil || q.Namespace == "" || q.Name == "" {
		return fmt.Errorf("local queue namespace and name are required")
	}
	if q.ClusterQueue == "" {
		return fmt.Errorf("local queue %s/%s has no cluster queue", q.Namespace, q.Name)
	}
	if _, ok := clusterQueues[q.ClusterQueue]; !ok {
		return fmt.Errorf("local queue %s/%s references missing cluster queue %q", q.Namespace, q.Name, q.ClusterQueue)
	}
	switch q.StopPolicy {
	case "", StopNone, StopHold, StopHoldAndDrain:
	default:
		return fmt.Errorf("local queue %s/%s has unsupported stop policy %q", q.Namespace, q.Name, q.StopPolicy)
	}
	if q.FairSharing != nil && !validWeight(q.FairSharing.Weight) {
		return fmt.Errorf("local queue %s/%s has an invalid fair sharing weight", q.Namespace, q.Name)
	}
	return nil
}

// ValidateLabelSelector checks set-based selector requirements.
func ValidateLabelSelector(s *LabelSelector) error {
	if s == nil {
		return nil
	}
	for _, req := range s.MatchExpressions {
		if req.Key == "" {
			return fmt.Errorf("matchExpressions entry has an empty key")
		}
		switch req.Operator {
		case "In", "NotIn":
			if len(req.Values) == 0 {
				return fmt.Errorf("matchExpressions %q with operator %s requires values", req.Key, req.Operator)
			}
		case "Exists", "DoesNotExist":
			if len(req.Values) != 0 {
				return fmt.Errorf("matchExpressions %q with operator %s must not set values", req.Key, req.Operator)
			}
		default:
			return fmt.Errorf("matchExpressions %q has unsupported operator %q", req.Key, req.Operator)
		}
	}
	return nil
}

// ValidatePreemptionConfig checks a configurable-preemption definition.
func ValidatePreemptionConfig(config *PreemptionConfig) error {
	if config == nil || config.Name == "" {
		return fmt.Errorf("preemption config name is required")
	}
	if len(config.Rules) == 0 {
		return fmt.Errorf("preemption config %q has no rules", config.Name)
	}
	seenRule := map[string]bool{}
	for i, rule := range config.Rules {
		if rule.Name == "" {
			return fmt.Errorf("preemption config %q rule %d has no name", config.Name, i)
		}
		if seenRule[rule.Name] {
			return fmt.Errorf("preemption config %q has duplicate rule %q", config.Name, rule.Name)
		}
		seenRule[rule.Name] = true
		switch rule.Trigger {
		case TriggerAlways, TriggerInsufficientQuota, TriggerQuotaFeasibleAndInsufficientTopology:
		default:
			return fmt.Errorf("preemption config %q rule %q has unsupported trigger %q",
				config.Name, rule.Name, rule.Trigger)
		}
		if len(rule.CandidateSelectors) == 0 {
			return fmt.Errorf("preemption config %q rule %q has no candidate selectors", config.Name, rule.Name)
		}
		for j, selector := range rule.CandidateSelectors {
			where := fmt.Sprintf("preemption config %q rule %q selector %d", config.Name, rule.Name, j)
			switch selector.Scope {
			case ScopeWithinLocalQueue, ScopeWithinClusterQueue, ScopeWithinParentCohort,
				ScopeWithinCohortTree, ScopeAnyClusterQueue:
			default:
				return fmt.Errorf("%s has unsupported scope %q", where, selector.Scope)
			}
			if selector.Priority != nil {
				if selector.Priority.Mode != PriorityBase && selector.Priority.Mode != PriorityBoosted {
					return fmt.Errorf("%s has unsupported priority mode %q", where, selector.Priority.Mode)
				}
				if !selector.Priority.Comparison.Valid() {
					return fmt.Errorf("%s has unsupported priority comparison %q", where, selector.Priority.Comparison)
				}
			}
			for k, constraint := range selector.NumericLabels {
				if constraint.Key == "" {
					return fmt.Errorf("%s numeric label %d has an empty key", where, k)
				}
				if !constraint.Comparison.Valid() {
					return fmt.Errorf("%s numeric label %q has unsupported comparison %q",
						where, constraint.Key, constraint.Comparison)
				}
				if constraint.MinValue != nil && constraint.MaxValue != nil &&
					*constraint.MinValue > *constraint.MaxValue {
					return fmt.Errorf("%s numeric label %q has minValue > maxValue", where, constraint.Key)
				}
			}
			if err := ValidateLabelSelector(selector.LabelSelector); err != nil {
				return fmt.Errorf("%s labelSelector: %w", where, err)
			}
			if err := ValidateLabelSelector(selector.ClusterQueueSelector); err != nil {
				return fmt.Errorf("%s clusterQueueSelector: %w", where, err)
			}
		}
	}
	return nil
}

// ValidateResourceTransformations checks a resource-transformation list.
func ValidateResourceTransformations(transformations []ResourceTransformation) error {
	seen := map[ResourceName]bool{}
	for i, transformation := range transformations {
		if transformation.Input == "" {
			return fmt.Errorf("resource transformation %d has an empty input", i)
		}
		if transformation.Input == Pods {
			return fmt.Errorf("resource transformation %d uses reserved resource %q as input", i, Pods)
		}
		if seen[transformation.Input] {
			return fmt.Errorf("resource transformation for %q is declared more than once", transformation.Input)
		}
		seen[transformation.Input] = true
		switch transformation.Strategy {
		case "", TransformRetain, TransformReplace:
		default:
			return fmt.Errorf("resource transformation for %q has unsupported strategy %q",
				transformation.Input, transformation.Strategy)
		}
		for resource, quantity := range transformation.Outputs {
			if resource == "" {
				return fmt.Errorf("resource transformation for %q has an empty output resource", transformation.Input)
			}
			if resource == Pods {
				return fmt.Errorf("resource transformation for %q outputs reserved resource %q",
					transformation.Input, Pods)
			}
			if quantity < 0 {
				return fmt.Errorf("resource transformation for %q has a negative output quantity for %q",
					transformation.Input, resource)
			}
		}
	}
	return nil
}

// ValidateWorkload checks a workload before it enters the queue.
func ValidateWorkload(w *Workload) error {
	if w == nil || w.Namespace == "" || w.Name == "" {
		return fmt.Errorf("workload namespace and name are required")
	}
	if w.Spec.QueueName == "" {
		return fmt.Errorf("workload %s has no queue name", w.Key())
	}
	if len(w.Spec.PodSets) == 0 {
		return fmt.Errorf("workload %s has no pod sets", w.Key())
	}
	if w.Spec.MaximumExecutionSeconds < 0 {
		return fmt.Errorf("workload %s has a negative maximum execution time", w.Key())
	}
	if err := ValidateResourceTransformations(w.Spec.ResourceTransformations); err != nil {
		return fmt.Errorf("workload %s: %w", w.Key(), err)
	}
	seen := map[string]bool{}
	for i, ps := range w.Spec.PodSets {
		if ps.Name == "" {
			return fmt.Errorf("workload %s pod set %d has no name", w.Key(), i)
		}
		if seen[ps.Name] {
			return fmt.Errorf("workload %s has duplicate pod set %q", w.Key(), ps.Name)
		}
		seen[ps.Name] = true
		if ps.Count < 0 {
			return fmt.Errorf("workload %s pod set %q has a negative count", w.Key(), ps.Name)
		}
		if ps.Requests.AnyNegative() {
			return fmt.Errorf("workload %s pod set %q has a negative request", w.Key(), ps.Name)
		}
		if _, reserved := ps.Requests[Pods]; reserved {
			return fmt.Errorf("workload %s pod set %q requests reserved resource %q", w.Key(), ps.Name, Pods)
		}
		for resource := range ps.Requests {
			if resource == "" {
				return fmt.Errorf("workload %s pod set %q has an empty resource name", w.Key(), ps.Name)
			}
		}
		for j, device := range ps.Devices {
			if device.DeviceClass == "" {
				return fmt.Errorf("workload %s pod set %q device %d has no device class", w.Key(), ps.Name, j)
			}
			if device.Count < 0 {
				return fmt.Errorf("workload %s pod set %q device %q has a negative count", w.Key(), ps.Name, device.DeviceClass)
			}
		}
		if topo := ps.Topology; topo != nil {
			switch topo.Mode {
			case TopologyRequired, TopologyPreferred, TopologyUnconstrained:
			default:
				return fmt.Errorf("workload %s pod set %q has unsupported topology mode %q", w.Key(), ps.Name, topo.Mode)
			}
			if topo.SliceSize < 0 {
				return fmt.Errorf("workload %s pod set %q has a negative slice size", w.Key(), ps.Name)
			}
			if topo.SliceSize > 0 && topo.Mode != TopologyRequired {
				return fmt.Errorf("workload %s pod set %q: slicing requires Required topology", w.Key(), ps.Name)
			}
			if topo.SliceSize > 0 && topo.Group != "" {
				return fmt.Errorf("workload %s pod set %q: slicing cannot be combined with a topology group", w.Key(), ps.Name)
			}
			if topo.Group != "" && topo.Mode != TopologyRequired && topo.Mode != TopologyPreferred {
				// A group means "share one domain"; Unconstrained by definition
				// does not, so the combination has no consistent meaning.
				return fmt.Errorf("workload %s pod set %q: a topology group requires Required or Preferred mode", w.Key(), ps.Name)
			}
		}
	}
	for deviceClass, resource := range w.Spec.DeviceClassMappings {
		if deviceClass == "" {
			return fmt.Errorf("workload %s has an empty device class mapping key", w.Key())
		}
		if resource == "" {
			return fmt.Errorf("workload %s device class %q maps to an empty resource", w.Key(), deviceClass)
		}
		if resource == Pods {
			return fmt.Errorf("workload %s device class %q maps to the reserved resource %q", w.Key(), deviceClass, Pods)
		}
	}
	seenGate := map[string]bool{}
	for i, gate := range w.Spec.AdmissionGates {
		if gate == "" {
			return fmt.Errorf("workload %s admission gate %d is empty", w.Key(), i)
		}
		if seenGate[gate] {
			return fmt.Errorf("workload %s lists admission gate %q more than once", w.Key(), gate)
		}
		seenGate[gate] = true
	}
	for name, reclaimable := range w.Status.ReclaimablePods {
		if !seen[name] {
			return fmt.Errorf("workload %s reports reclaimable pods for unknown pod set %q", w.Key(), name)
		}
		if reclaimable < 0 {
			return fmt.Errorf("workload %s has a negative reclaimable count for pod set %q", w.Key(), name)
		}
		for _, ps := range w.Spec.PodSets {
			if ps.Name == name && reclaimable > ps.Count {
				return fmt.Errorf("workload %s reclaims %d pods from pod set %q with only %d",
					w.Key(), reclaimable, name, ps.Count)
			}
		}
	}
	return nil
}

// ValidateTopology checks a topology definition.
func ValidateTopology(t *Topology) error {
	if t == nil || t.Name == "" {
		return fmt.Errorf("topology name is required")
	}
	if len(t.Levels) == 0 {
		return fmt.Errorf("topology %q has no levels", t.Name)
	}
	seen := map[string]bool{}
	for _, level := range t.Levels {
		if level == "" {
			return fmt.Errorf("topology %q has an empty level", t.Name)
		}
		if seen[level] {
			return fmt.Errorf("topology %q has duplicate level %q", t.Name, level)
		}
		seen[level] = true
	}
	return nil
}

// ValidateTopologyDomain checks that a leaf domain matches its topology.
func ValidateTopologyDomain(d *TopologyDomain, topologies map[string]*Topology) error {
	if d == nil || d.Name == "" {
		return fmt.Errorf("topology domain name is required")
	}
	topo, ok := topologies[d.Topology]
	if !ok {
		return fmt.Errorf("topology domain %q references missing topology %q", d.Name, d.Topology)
	}
	if len(d.Values) != len(topo.Levels) {
		return fmt.Errorf("topology domain %q has %d values for %d levels", d.Name, len(d.Values), len(topo.Levels))
	}
	for i, value := range d.Values {
		if value == "" {
			return fmt.Errorf("topology domain %q has an empty value for level %q", d.Name, topo.Levels[i])
		}
	}
	if d.Capacity.AnyNegative() {
		return fmt.Errorf("topology domain %q has a negative capacity", d.Name)
	}
	return nil
}

// ValidateCapacityProvider checks a dynamic capacity provider.
func ValidateCapacityProvider(p *CapacityProvider) error {
	if p == nil || p.Name == "" {
		return fmt.Errorf("capacity provider name is required")
	}
	if len(p.Capacity) == 0 {
		return fmt.Errorf("capacity provider %q reports no capacity", p.Name)
	}
	for flavor, resources := range p.Capacity {
		if flavor == "" {
			return fmt.Errorf("capacity provider %q has an empty flavor name", p.Name)
		}
		if resources.AnyNegative() {
			return fmt.Errorf("capacity provider %q flavor %q has a negative capacity", p.Name, flavor)
		}
		for resource := range resources {
			if resource == "" {
				return fmt.Errorf("capacity provider %q flavor %q has an empty resource name", p.Name, flavor)
			}
		}
	}
	return nil
}

// ValidateWorkloadRefs checks references that require the full store.
func ValidateWorkloadRefs(w *Workload, localQueues map[string]*LocalQueue,
	clusterQueues map[string]*ClusterQueue, classes map[string]*WorkloadPriorityClass) error {
	if err := ValidateWorkload(w); err != nil {
		return err
	}
	lq, ok := localQueues[w.Namespace+"/"+w.Spec.QueueName]
	if !ok {
		return fmt.Errorf("workload %s references missing local queue %s/%s", w.Key(), w.Namespace, w.Spec.QueueName)
	}
	if w.Spec.PriorityClassName != "" {
		if _, ok := classes[w.Spec.PriorityClassName]; !ok {
			return fmt.Errorf("workload %s references missing priority class %q", w.Key(), w.Spec.PriorityClassName)
		}
	}
	cq, ok := clusterQueues[lq.ClusterQueue]
	if !ok {
		return fmt.Errorf("workload %s local queue references missing cluster queue %q", w.Key(), lq.ClusterQueue)
	}
	for _, ps := range w.Spec.PodSets {
		for _, device := range ps.Devices {
			if device.Count <= 0 {
				continue
			}
			mapping := w.Spec.DeviceClassMappings
			if mapping == nil {
				mapping = cq.Spec.DeviceClassMappings
			}
			resource, ok := mapping[device.DeviceClass]
			if !ok {
				return fmt.Errorf("workload %s device class %q is not mapped by cluster queue %q",
					w.Key(), device.DeviceClass, cq.Name)
			}
			if resource == "" {
				return fmt.Errorf("workload %s device class %q maps to an empty resource", w.Key(), device.DeviceClass)
			}
			if resource == Pods {
				return fmt.Errorf("workload %s device class %q maps to the reserved resource %q",
					w.Key(), device.DeviceClass, Pods)
			}
			if !clusterQueueCovers(cq, resource) {
				return fmt.Errorf("workload %s device class %q maps to resource %q not covered by cluster queue %q",
					w.Key(), device.DeviceClass, resource, cq.Name)
			}
		}
	}
	return nil
}

func clusterQueueCovers(cq *ClusterQueue, resource ResourceName) bool {
	for _, group := range cq.Spec.ResourceGroups {
		for _, r := range group.CoveredResources {
			if r == resource {
				return true
			}
		}
	}
	return false
}
