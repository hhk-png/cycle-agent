package api

import (
	"math"
	"testing"
)

func TestResourceListAndFormatting(t *testing.T) {
	r := RL("cpu", 1500, "memory", 2048, "nvidia.com/gpu", 2)
	if got := r.Get(CPU); got != 1500 {
		t.Fatalf("cpu = %d, want 1500", got)
	}
	if got := FormatQuantity(CPU, 1500); got != "1.5" {
		t.Fatalf("cpu format = %q", got)
	}
	if got := FormatQuantity(Memory, 2048); got != "2Gi" {
		t.Fatalf("memory format = %q", got)
	}
	if got := FormatQuantity("nvidia.com/gpu", 2); got != "2" {
		t.Fatalf("gpu format = %q", got)
	}
	if !r.Covers(RL("cpu", 1500)) {
		t.Fatal("expected covers")
	}
	if r.Covers(RL("cpu", 1501)) {
		t.Fatal("did not expect covers")
	}
	if got := r.Clone(); got.Get(CPU) != 1500 {
		t.Fatalf("clone lost data: %v", got)
	}
}

func TestWorkloadTotalUsage(t *testing.T) {
	w := testWorkload("team", "job", "q", 0, 3, RL("cpu", 1000, "memory", 512))
	w.Spec.PodSets = append(w.Spec.PodSets, PodSet{
		Name: "worker", Count: 2, Requests: RL("cpu", 2000, "nvidia.com/gpu", 1),
	})
	got := w.TotalUsage()
	if got.Get(CPU) != 3*1000+2*2000 {
		t.Fatalf("cpu total = %d", got.Get(CPU))
	}
	if got.Get(Memory) != 3*512 {
		t.Fatalf("memory total = %d", got.Get(Memory))
	}
	if got.Get(Pods) != 5 {
		t.Fatalf("pods total = %d", got.Get(Pods))
	}
	if got.Get("nvidia.com/gpu") != 2 {
		t.Fatalf("gpu total = %d", got.Get("nvidia.com/gpu"))
	}
}

func TestDRADeviceUsageAndDoubleCounting(t *testing.T) {
	w := testWorkload("team", "job", "q", 0, 4, RL("cpu", 1000))
	w.Spec.DeviceClassMappings = map[string]ResourceName{"gpu-a100": "nvidia.com/gpu"}
	w.Spec.PodSets[0].Devices = []DeviceRequest{{DeviceClass: "gpu-a100", Count: 1}}
	if got := w.TotalUsage().Get("nvidia.com/gpu"); got != 4 {
		t.Fatalf("device gpu total = %d, want 4", got)
	}

	// The same logical resource requested both as an extended resource and
	// through a device is counted once, using the larger value.
	w.Spec.PodSets[0].Requests["nvidia.com/gpu"] = 2
	if got := w.TotalUsage().Get("nvidia.com/gpu"); got != 8 {
		t.Fatalf("double-count gpu total = %d, want 8 (max(2,1)*4)", got)
	}
	w.Spec.PodSets[0].Requests["nvidia.com/gpu"] = 4
	if got := w.TotalUsage().Get("nvidia.com/gpu"); got != 16 {
		t.Fatalf("double-count gpu total = %d, want 16 (max(4,1)*4)", got)
	}
}

func TestDeviceMappingValidation(t *testing.T) {
	cq := &ClusterQueue{
		Name: "gpu-cq",
		Spec: ClusterQueueSpec{
			ResourceGroups: []ResourceGroup{{
				CoveredResources: []ResourceName{"nvidia.com/gpu"},
				Flavors: []FlavorQuota{{
					Name: "default", Nominal: RL("nvidia.com/gpu", 1),
				}},
			}},
		},
	}
	for _, target := range []ResourceName{"", "example.com/phantom", Pods} {
		bad := cq.Clone()
		bad.Spec.DeviceClassMappings = map[string]ResourceName{"gpu-a100": target}
		if err := ValidateClusterQueue(bad); err == nil {
			t.Fatalf("mapping to %q should be rejected", target)
		}
	}
	good := cq.Clone()
	good.Spec.DeviceClassMappings = map[string]ResourceName{"gpu-a100": "nvidia.com/gpu"}
	if err := ValidateClusterQueue(good); err != nil {
		t.Fatalf("valid mapping rejected: %v", err)
	}

	w := testWorkload("team", "job", "q", 0, 1, RL("cpu", 1000))
	w.Spec.DeviceClassMappings = map[string]ResourceName{"gpu-a100": ""}
	w.Spec.PodSets[0].Devices = []DeviceRequest{{DeviceClass: "gpu-a100", Count: 1}}
	if err := ValidateWorkload(w); err == nil {
		t.Fatal("empty device mapping should be rejected")
	}
	if err := ValidateWorkloadRefs(w, map[string]*LocalQueue{
		"team/q": {Namespace: "team", Name: "q", ClusterQueue: "gpu-cq"},
	}, map[string]*ClusterQueue{"gpu-cq": good}, nil); err == nil {
		t.Fatal("uncovered device mapping should be rejected")
	}
}

func TestDeviceMappingToPodsDoesNotDoubleCount(t *testing.T) {
	w := testWorkload("team", "job", "q", 0, 2, RL("cpu", 1000))
	w.Spec.DeviceClassMappings = map[string]ResourceName{"gpu-a100": Pods}
	w.Spec.PodSets[0].Devices = []DeviceRequest{{DeviceClass: "gpu-a100", Count: 1}}
	// Defensive accounting ignores a mapping to the reserved pods resource.
	if got := w.TotalUsage().Get(Pods); got != 2 {
		t.Fatalf("pods total = %d, want 2", got)
	}
}

func TestValidateRejectsEmptyResourceNames(t *testing.T) {
	w := testWorkload("team", "job", "q", 0, 1, RL("cpu", 1000))
	w.Spec.PodSets[0].Requests[ResourceName("")] = 1
	if err := ValidateWorkload(w); err == nil {
		t.Fatal("empty workload resource name should be rejected")
	}
	p := &CapacityProvider{
		Name:     "pool",
		Capacity: map[string]ResourceList{"default": {ResourceName(""): 1}},
	}
	if err := ValidateCapacityProvider(p); err == nil {
		t.Fatal("empty provider resource name should be rejected")
	}
}

func TestAdmissionGatesAndWeightedSum(t *testing.T) {
	w := testWorkload("team", "job", "q", 0, 1, RL("cpu", 2000))
	w.Spec.AdmissionGates = []string{"security-scan", "license-check"}
	if !w.HasAdmissionGates() {
		t.Fatal("expected gates")
	}
	if !w.ClearAdmissionGate("security-scan") || w.ClearAdmissionGate("missing") {
		t.Fatal("gate removal result mismatch")
	}
	if !w.HasAdmissionGates() || len(w.Spec.AdmissionGates) != 1 {
		t.Fatalf("remaining gates = %v", w.Spec.AdmissionGates)
	}
	w.ClearAdmissionGate("license-check")
	if w.HasAdmissionGates() {
		t.Fatal("expected no gates")
	}

	score := RL("cpu", 2000, "memory", 2048, "nvidia.com/gpu", 1).
		WeightedSum(map[ResourceName]float64{"nvidia.com/gpu": 10})
	// 2 CPU + 2 GiB + 1 GPU * 10 = 14
	if score != 14 {
		t.Fatalf("weighted sum = %v, want 14", score)
	}
}

func TestValidateClusterQueue(t *testing.T) {
	good := testCQ()
	if err := ValidateClusterQueue(good); err != nil {
		t.Fatalf("valid CQ rejected: %v", err)
	}
	bad := testCQ()
	bad.Spec.ResourceGroups[0].Flavors[0].Nominal = RL()
	if err := ValidateClusterQueue(bad); err == nil {
		t.Fatal("expected missing nominal quota to be rejected")
	}
}

func TestConcurrentAdmissionValidatesLastAcceptableFlavor(t *testing.T) {
	cq := testCQ()
	cq.Spec.ConcurrentAdmission = &ConcurrentAdmissionPolicy{LastAcceptableFlavorName: "missing"}
	if err := ValidateClusterQueue(cq); err == nil {
		t.Fatal("expected unknown lastAcceptableFlavorName to be rejected")
	}
	cq.Spec.ConcurrentAdmission.LastAcceptableFlavorName = "default"
	if err := ValidateClusterQueue(cq); err != nil {
		t.Fatalf("valid lastAcceptableFlavorName rejected: %v", err)
	}
}

func TestReclaimablePodsReduceUsage(t *testing.T) {
	w := testWorkload("team", "job", "q", 0, 3, RL("cpu", 1000))
	if got := w.TotalUsage().Get(CPU); got != 3000 {
		t.Fatalf("cpu = %d, want 3000", got)
	}
	w.Status.ReclaimablePods = map[string]int32{"main": 2}
	if got := w.TotalUsage().Get(CPU); got != 1000 {
		t.Fatalf("cpu after reclaim = %d, want 1000", got)
	}
	if got := w.TotalUsage().Get(Pods); got != 1 {
		t.Fatalf("pods after reclaim = %d, want 1", got)
	}
	w.Status.ReclaimablePods["main"] = 4
	if err := ValidateWorkload(w); err == nil {
		t.Fatal("expected excessive reclaimable count to be rejected")
	}
}

func TestValidateTopologyAndDomain(t *testing.T) {
	topo := &Topology{Name: "t", Levels: []string{"rack", "hostname"}}
	if err := ValidateTopology(topo); err != nil {
		t.Fatalf("valid topology rejected: %v", err)
	}
	if err := ValidateTopology(&Topology{Name: "bad", Levels: []string{"rack", "rack"}}); err == nil {
		t.Fatal("expected duplicate level to be rejected")
	}
	good := &TopologyDomain{Name: "h1", Topology: "t", Values: []string{"r1", "h1"}, Capacity: RL("cpu", 1000)}
	if err := ValidateTopologyDomain(good, map[string]*Topology{"t": topo}); err != nil {
		t.Fatalf("valid domain rejected: %v", err)
	}
	bad := &TopologyDomain{Name: "h1", Topology: "t", Values: []string{"r1"}}
	if err := ValidateTopologyDomain(bad, map[string]*Topology{"t": topo}); err == nil {
		t.Fatal("expected value/level mismatch to be rejected")
	}
	if err := ValidateCapacityProvider(&CapacityProvider{Name: "p", Capacity: map[string]ResourceList{"f": RL("cpu", -1)}}); err == nil {
		t.Fatal("expected negative capacity to be rejected")
	}
}

func TestLabelSelector(t *testing.T) {
	var nilSelector *LabelSelector
	if nilSelector.Matches(map[string]string{"team": "a"}) {
		t.Fatal("nil selector must match no namespace")
	}
	all := &LabelSelector{}
	if !all.Matches(nil) {
		t.Fatal("empty selector must match every namespace")
	}
	sel := &LabelSelector{
		MatchLabels: map[string]string{"env": "prod"},
		MatchExpressions: []LabelSelectorRequirement{
			{Key: "team", Operator: "In", Values: []string{"a", "b"}},
			{Key: "blocked", Operator: "DoesNotExist"},
		},
	}
	if !sel.Matches(map[string]string{"env": "prod", "team": "a"}) {
		t.Fatal("expected match")
	}
	if sel.Matches(map[string]string{"env": "dev", "team": "a"}) {
		t.Fatal("did not expect match with wrong label")
	}
	if sel.Matches(map[string]string{"env": "prod", "team": "c"}) {
		t.Fatal("did not expect match outside In values")
	}
	if sel.Matches(map[string]string{"env": "prod", "team": "a", "blocked": "true"}) {
		t.Fatal("did not expect match when DoesNotExist is violated")
	}
	emptyValue := &LabelSelector{MatchLabels: map[string]string{"team": ""}}
	if emptyValue.Matches(map[string]string{}) {
		t.Fatal("missing label must not equal an empty expected value")
	}
	if !emptyValue.Matches(map[string]string{"team": ""}) {
		t.Fatal("present empty label should match")
	}
	notIn := &LabelSelector{MatchExpressions: []LabelSelectorRequirement{
		{Key: "team", Operator: "NotIn", Values: []string{"c"}},
	}}
	if !notIn.Matches(map[string]string{"team": "a"}) || notIn.Matches(map[string]string{"team": "c"}) {
		t.Fatal("NotIn evaluation is wrong")
	}
	if err := ValidateLabelSelector(&LabelSelector{MatchExpressions: []LabelSelectorRequirement{
		{Key: "team", Operator: "Exists", Values: []string{"x"}},
	}}); err == nil {
		t.Fatal("expected Exists with values to be rejected")
	}
	if err := ValidateLabelSelector(&LabelSelector{MatchExpressions: []LabelSelectorRequirement{
		{Key: "team", Operator: "Unknown"},
	}}); err == nil {
		t.Fatal("expected unknown operator to be rejected")
	}
}

func TestValidateFairSharingWeightsRejectsNonFinite(t *testing.T) {
	cq := testCQ()
	cq.Spec.FairSharing = &FairSharing{Weight: math.NaN()}
	if err := ValidateClusterQueue(cq); err == nil {
		t.Fatal("expected NaN weight to be rejected")
	}
	cq.Spec.FairSharing = &FairSharing{Weight: math.Inf(1)}
	if err := ValidateClusterQueue(cq); err == nil {
		t.Fatal("expected infinite weight to be rejected")
	}
	cq.Spec.FairSharing = &FairSharing{Weight: 2}
	if err := ValidateClusterQueue(cq); err != nil {
		t.Fatalf("valid weight rejected: %v", err)
	}
	if err := ValidateLocalQueue(&LocalQueue{
		Namespace: "ns", Name: "q", ClusterQueue: "cq",
		FairSharing: &FairSharing{Weight: math.NaN()},
	}, map[string]*ClusterQueue{"cq": cq}); err == nil {
		t.Fatal("expected NaN LocalQueue weight to be rejected")
	}
}

func TestValidateClusterQueueAdmissionChecks(t *testing.T) {
	cq := testCQ()
	cq.Spec.AdmissionChecks = []string{"check-a", "check-a"}
	if err := ValidateClusterQueue(cq); err == nil {
		t.Fatal("expected duplicate admission check to be rejected")
	}
	cq.Spec.AdmissionChecks = []string{"check-a", ""}
	if err := ValidateClusterQueue(cq); err == nil {
		t.Fatal("expected empty admission check name to be rejected")
	}
}

func TestValidateWorkloadTopologyModes(t *testing.T) {
	base := func() *Workload {
		return testWorkload("default", "job", "q", 0, 1, RL("cpu", 1000))
	}
	good := base()
	good.Spec.PodSets[0].Topology = &PodSetTopology{Mode: TopologyRequired, Level: "rack"}
	if err := ValidateWorkload(good); err != nil {
		t.Fatalf("valid topology rejected: %v", err)
	}
	unknown := base()
	unknown.Spec.PodSets[0].Topology = &PodSetTopology{Mode: "Bogus", Level: "rack"}
	if err := ValidateWorkload(unknown); err == nil {
		t.Fatal("expected unknown topology mode to be rejected")
	}
	groupUnconstrained := base()
	groupUnconstrained.Spec.PodSets[0].Topology = &PodSetTopology{
		Mode: TopologyUnconstrained, Level: "rack", Group: "g",
	}
	if err := ValidateWorkload(groupUnconstrained); err == nil {
		t.Fatal("expected Unconstrained group to be rejected")
	}
}

func TestValidateWorkloadRejectsNegativeExecutionAndDuplicateGates(t *testing.T) {
	w := testWorkload("default", "job", "q", 0, 1, RL("cpu", 1000))
	w.Spec.MaximumExecutionSeconds = -1
	if err := ValidateWorkload(w); err == nil {
		t.Fatal("expected negative maximum execution time to be rejected")
	}
	w = testWorkload("default", "job", "q", 0, 1, RL("cpu", 1000))
	w.Spec.AdmissionGates = []string{"gate", "gate"}
	if err := ValidateWorkload(w); err == nil {
		t.Fatal("expected duplicate admission gates to be rejected")
	}
}

func TestValidateWorkloadRejectsReservedPods(t *testing.T) {
	w := testWorkload("default", "bad", "q", 0, 1, RL("pods", 1))
	if err := ValidateWorkload(w); err == nil {
		t.Fatal("expected pods request to be rejected")
	}
}

func TestResourceTransformationsMatchHamiExample(t *testing.T) {
	w := testWorkload("default", "hami", "q", 0, 1, RL(
		"example.com/gpu", 2,
		"example.com/gpumem", 1024,
		"example.com/gpucores", 20,
	))
	w.Spec.ResourceTransformations = []ResourceTransformation{
		{
			Input: "example.com/gpumem", Strategy: TransformReplace, MultiplyBy: "example.com/gpu",
			Outputs: RL("example.com/total-gpumem", 1),
		},
		{
			Input: "example.com/gpucores", Strategy: TransformReplace, MultiplyBy: "example.com/gpu",
			Outputs: RL("example.com/total-gpucores", 1),
		},
		// Replace with empty outputs is the documented way to exclude the
		// vGPU count resource from quota accounting.
		{Input: "example.com/gpu", Strategy: TransformReplace},
	}
	usage := w.TotalUsage()
	if got := usage.Get("example.com/total-gpumem"); got != 2048 {
		t.Fatalf("total-gpumem = %d, want 2048", got)
	}
	if got := usage.Get("example.com/total-gpucores"); got != 40 {
		t.Fatalf("total-gpucores = %d, want 40", got)
	}
	if _, ok := usage["example.com/gpu"]; ok {
		t.Fatalf("input gpu should be ignored after Replace: %v", usage)
	}
	if usage.Get(Pods) != 1 {
		t.Fatalf("pods = %d, want 1", usage.Get(Pods))
	}
}

func TestResourceTransformationsRetainAndFixedMultiplier(t *testing.T) {
	w := testWorkload("default", "mig", "q", 0, 2, RL("nvidia.com/mig-3g.20gb", 4))
	w.Spec.ResourceTransformations = []ResourceTransformation{{
		Input: "nvidia.com/mig-3g.20gb", Strategy: TransformRetain, MultiplyBy: "1",
		Outputs: RL("nvidia.com/gpu", 1),
	}}
	usage := w.TotalUsage()
	if got := usage.Get("nvidia.com/mig-3g.20gb"); got != 8 {
		// 4 per pod * 2 pods, retained.
		t.Fatalf("mig = %d, want 8", got)
	}
	if got := usage.Get("nvidia.com/gpu"); got != 8 {
		t.Fatalf("gpu = %d, want 8", got)
	}
}

func TestResourceTransformationMissingMultiplierContributesZero(t *testing.T) {
	w := testWorkload("default", "missing", "q", 0, 1, RL("example.com/gpumem", 1024))
	w.Spec.ResourceTransformations = []ResourceTransformation{{
		Input: "example.com/gpumem", Strategy: TransformReplace, MultiplyBy: "example.com/gpu",
		Outputs: RL("example.com/total-gpumem", 1),
	}}
	usage := w.TotalUsage()
	if _, ok := usage["example.com/gpumem"]; ok {
		t.Fatalf("input should be replaced: %v", usage)
	}
	if got := usage.Get("example.com/total-gpumem"); got != 0 {
		t.Fatalf("total-gpumem = %d, want 0", got)
	}
}

func TestValidateResourceTransformationsRejectsReservedName(t *testing.T) {
	err := ValidateResourceTransformations([]ResourceTransformation{{
		Input: "nvidia.com/gpu", Strategy: TransformReplace, Outputs: RL("pods", 1),
	}})
	if err == nil {
		t.Fatal("expected pods output to be rejected")
	}
	err = ValidateResourceTransformations([]ResourceTransformation{
		{Input: "a", Outputs: RL("x", 1)},
		{Input: "a", Outputs: RL("y", 1)},
	})
	if err == nil {
		t.Fatal("expected duplicate input to be rejected")
	}
}

func TestValidatePreemptionConfig(t *testing.T) {
	good := &PreemptionConfig{
		Name: "cfg",
		Rules: []PreemptionRule{{
			Name:    "rule",
			Trigger: TriggerInsufficientQuota,
			CandidateSelectors: []PreemptionCandidateSelector{{
				Scope:    ScopeWithinCohortTree,
				Priority: &PriorityConstraint{Mode: PriorityBase, Comparison: CompareLessThan},
			}},
		}},
	}
	if err := ValidatePreemptionConfig(good); err != nil {
		t.Fatalf("valid config rejected: %v", err)
	}
	badTrigger := good.Clone()
	badTrigger.Rules[0].Trigger = "Sometimes"
	if err := ValidatePreemptionConfig(badTrigger); err == nil {
		t.Fatal("expected unsupported trigger to be rejected")
	}
	badScope := good.Clone()
	badScope.Rules[0].CandidateSelectors[0].Scope = "Everywhere"
	if err := ValidatePreemptionConfig(badScope); err == nil {
		t.Fatal("expected unsupported scope to be rejected")
	}
	badCompare := good.Clone()
	badCompare.Rules[0].CandidateSelectors[0].Priority.Comparison = "Approximately"
	if err := ValidatePreemptionConfig(badCompare); err == nil {
		t.Fatal("expected unsupported comparison to be rejected")
	}
	badNumeric := good.Clone()
	badNumeric.Rules[0].CandidateSelectors[0].NumericLabels = []NumericLabelConstraint{{
		Key: "node-count", Comparison: CompareLessThan,
	}}
	if err := ValidatePreemptionConfig(badNumeric); err != nil {
		t.Fatalf("numeric label should be valid: %v", err)
	}
	badNumeric.Rules[0].CandidateSelectors[0].NumericLabels[0].Key = ""
	if err := ValidatePreemptionConfig(badNumeric); err == nil {
		t.Fatal("expected empty numeric label key to be rejected")
	}
}

func TestConditions(t *testing.T) {
	var s WorkloadStatus
	s.SetCondition("Admitted", "True", "Admitted", "ok", 1)
	s.SetCondition("Admitted", "False", "Evicted", "no", 2)
	c, ok := s.Condition("Admitted")
	if !ok || c.Status != "False" || c.LastTransitionTick != 2 {
		t.Fatalf("unexpected condition: %+v", c)
	}
	s.ClearCondition("Admitted")
	if _, ok := s.Condition("Admitted"); ok {
		t.Fatal("condition was not cleared")
	}
}

func testWorkload(namespace, name, queue string, priority int32, pods int32, requests ResourceList) *Workload {
	return &Workload{
		Namespace: namespace,
		Name:      name,
		Spec: WorkloadSpec{
			Active:    true,
			QueueName: queue,
			Priority:  priority,
			PodSets: []PodSet{{
				Name: "main", Count: pods, Requests: requests,
			}},
		},
	}
}

func testCQ() *ClusterQueue {
	return &ClusterQueue{
		Name: "cq",
		Spec: ClusterQueueSpec{
			ResourceGroups: []ResourceGroup{{
				CoveredResources: []ResourceName{CPU},
				Flavors: []FlavorQuota{{
					Name: "default", Nominal: RL("cpu", 2000),
				}},
			}},
		},
	}
}
