package main

import (
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/controller"
	"example.com/minikueue/internal/multikueue"
	"example.com/minikueue/internal/scenario"
	"example.com/minikueue/internal/scheduler"
)

func main() {
	name := flag.String("scenario", "basic", "scenario to run (use -list to see all)")
	ticks := flag.Int("ticks", 5, "number of scheduling ticks")
	list := flag.Bool("list", false, "list scenarios")
	query := flag.String("query", "", "print visibility status for this ClusterQueue after the run")
	get := flag.String("get", "", "print resources after the run: workloads|clusterqueues|localqueues|cohorts|flavors|topologies")
	describe := flag.String("describe", "", "describe a workload after the run (namespace/name)")
	stop := flag.String("stop", "", "deactivate a workload before the run (namespace/name)")
	resume := flag.String("resume", "", "reactivate a workload before the run (namespace/name)")
	explain := flag.String("explain", "", "explain the admission decision for a workload (namespace/name)")
	whatIf := flag.String("what-if", "", "counterfactual quota override: clusterqueue/flavor/resource=value")
	flag.Parse()

	if *ticks < 0 {
		fmt.Fprintln(os.Stderr, "-ticks must be non-negative")
		os.Exit(2)
	}

	if *list {
		for _, n := range scenario.Names() {
			_, desc, err := scenario.Run(n)
			if err != nil {
				fmt.Fprintf(os.Stderr, "%s: %v\n", n, err)
				os.Exit(2)
			}
			fmt.Printf("%-20s %s\n", n, desc)
		}
		fmt.Printf("%-20s %s\n", "multikueue", "Manager/worker dispatch with nomination, selection and cleanup")
		fmt.Printf("%-20s %s\n", "multikueue-failover", "Lost-worker timeout and re-dispatch after the selected worker goes down")
		return
	}

	if *name == "multikueue" || *name == "multikueue-failover" {
		if *query != "" {
			fmt.Fprintln(os.Stderr, "-query is not supported for the multikueue scenarios")
		}
		if *get != "" || *describe != "" || *stop != "" || *resume != "" || *explain != "" || *whatIf != "" {
			fmt.Fprintln(os.Stderr, "-get, -describe, -stop, -resume, -explain and -what-if are not supported for the multikueue scenarios")
			os.Exit(2)
		}
		if *name == "multikueue-failover" {
			runMultiKueueFailover(*ticks)
		} else {
			runMultiKueue(*ticks)
		}
		return
	}

	engine, description, err := scenario.Run(*name)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}

	fmt.Printf("== MiniKueue / %s ==\n", *name)
	fmt.Println(description)
	fmt.Println()
	printConfig(engine)

	if *stop != "" {
		namespace, workloadName, err := splitNamespaced(*stop)
		if err != nil {
			fmt.Fprintf(os.Stderr, "-stop: %v\n", err)
			os.Exit(2)
		}
		if err := engine.Deactivate(namespace, workloadName); err != nil {
			fmt.Fprintf(os.Stderr, "-stop: %v\n", err)
			os.Exit(2)
		}
		fmt.Printf(">> operator: %s deactivated\n", *stop)
	}
	if *resume != "" {
		namespace, workloadName, err := splitNamespaced(*resume)
		if err != nil {
			fmt.Fprintf(os.Stderr, "-resume: %v\n", err)
			os.Exit(2)
		}
		if err := engine.Activate(namespace, workloadName); err != nil {
			fmt.Fprintf(os.Stderr, "-resume: %v\n", err)
			os.Exit(2)
		}
		fmt.Printf(">> operator: %s reactivated\n", *resume)
	}

	for i := 0; i < *ticks; i++ {
		result := engine.Tick()
		fmt.Printf("--- tick %d ---\n", result.Clock)
		for _, ev := range result.Events {
			fmt.Printf("  [%s] %s: %s\n", ev.Kind, ev.Workload, ev.Message)
		}
		if len(result.Events) == 0 {
			fmt.Println("  (no events)")
		}
		printWorkloads(engine)
		printUsage(engine)

		// A couple of scripted operator actions make the lifecycle visible
		// without turning the demo into an interactive program.
		switch *name {
		case "basic":
			if i == 1 {
				if err := engine.Finish("default", "job-a"); err == nil {
					fmt.Println(">> operator: job-a finished")
				}
			}
		case "admission-check":
			if i == 0 {
				if err := engine.ApproveCheck("default", "checked-job", "manual-check", "Ready", "approved by operator"); err == nil {
					fmt.Println(">> operator: manual-check approved")
				}
			}
		case "pods-ready":
			if i == 0 {
				if err := engine.MarkPodsReady("default", "ready-job"); err == nil {
					fmt.Println(">> operator: ready-job pods are ready")
				}
			}
		case "dynamic-reclaim":
			if i == 0 {
				if err := engine.ReportReclaimable("default", "elastic-job", "main", 1); err == nil {
					fmt.Println(">> operator: elastic-job reclaimed 1 pod")
				}
			}
		case "localqueue-hold":
			if i == 0 {
				if err := engine.SetLocalQueueStopPolicy("default", "held", api.StopNone); err == nil {
					fmt.Println(">> operator: local queue held released")
				}
			}
		case "retry-backoff":
			if i == 0 {
				if err := engine.ApproveCheckRetry("default", "retry-job", "capacity-check",
					"capacity temporarily unavailable", 2); err == nil {
					fmt.Println(">> operator: capacity-check requested Retry in 2 ticks")
				}
			}
			if i == 2 {
				if err := engine.ApproveCheck("default", "retry-job", "capacity-check", "Ready", "capacity available"); err == nil {
					fmt.Println(">> operator: capacity-check approved")
				}
			}
		case "concurrent-admission":
			if i == 0 {
				if cq, ok := engine.Store.GetClusterQueue("concurrent"); ok {
					cq.Spec.ResourceGroups[0].Flavors[0].Nominal["cpu"] = 2000
					if err := engine.Store.UpsertClusterQueue(cq); err == nil {
						fmt.Println(">> operator: reserved flavor quota raised to 2 CPU")
					}
				}
			}
		case "admission-gate":
			if i == 0 {
				for _, gate := range []string{"security-scan", "license-check"} {
					if err := engine.ClearAdmissionGate("default", "gated-job", gate); err == nil {
						fmt.Println(">> operator: cleared admission gate", gate)
					}
				}
			}
		}
	}

	if *query != "" {
		printQueueStatus(engine, *query)
	}
	if *get != "" {
		if err := printGet(engine, *get); err != nil {
			fmt.Fprintf(os.Stderr, "-get: %v\n", err)
			os.Exit(2)
		}
	}
	if *describe != "" {
		namespace, workloadName, err := splitNamespaced(*describe)
		if err != nil {
			fmt.Fprintf(os.Stderr, "-describe: %v\n", err)
			os.Exit(2)
		}
		if err := printDescribe(engine, namespace, workloadName); err != nil {
			fmt.Fprintf(os.Stderr, "-describe: %v\n", err)
			os.Exit(2)
		}
	}
	if *explain != "" {
		namespace, workloadName, err := splitNamespaced(*explain)
		if err != nil {
			fmt.Fprintf(os.Stderr, "-explain: %v\n", err)
			os.Exit(2)
		}
		if err := printExplain(engine, namespace, workloadName); err != nil {
			fmt.Fprintf(os.Stderr, "-explain: %v\n", err)
			os.Exit(2)
		}
	}
	if *whatIf != "" {
		if err := runWhatIf(*name, *ticks, *whatIf); err != nil {
			fmt.Fprintf(os.Stderr, "-what-if: %v\n", err)
			os.Exit(2)
		}
	}

	fmt.Println()
	fmt.Print(engine.Prometheus())
}

func runMultiKueue(ticks int) {
	m := scenario.MultiKueue(multikueue.AllAtOnce)
	fmt.Println("== MiniKueue / multikueue ==")
	fmt.Println("A manager dispatches one workload to two workers; the first admitting worker wins")
	fmt.Println()
	fmt.Printf("manager=%s dispatcher=%s workload=%s\n\n", m.Name, m.Config.Dispatcher, m.Workload.Key())
	for i := 0; i < ticks; i++ {
		events := m.Tick()
		fmt.Printf("--- tick %d ---\n", i+1)
		for _, event := range events {
			fmt.Printf("  %s\n", event.String())
		}
		if len(events) == 0 {
			fmt.Println("  (no events)")
		}
		fmt.Printf("  nominated: %v\n", m.NominatedSorted())
		for _, status := range m.Snapshot() {
			selected := ""
			if status.Selected {
				selected = "  <selected>"
			}
			fmt.Printf("  %-10s %-12s%s\n", status.Worker, status.State, selected)
		}
	}
	fmt.Printf("\nselected worker: %s\n", m.Selected)
}

func runMultiKueueFailover(ticks int) {
	m := scenario.MultiKueueFailover()
	fmt.Println("== MiniKueue / multikueue-failover ==")
	fmt.Println("worker-1 starts unreachable; worker-2 wins, then loses the connection")
	fmt.Println("The manager waits WorkerLostTimeout ticks, re-dispatches, and cleans up after recovery")
	fmt.Println()
	fmt.Printf("manager=%s dispatcher=%s lostTimeout=%d workload=%s\n\n",
		m.Name, m.Config.Dispatcher, m.Config.WorkerLostTimeout, m.Workload.Key())

	for i := 0; i < ticks; i++ {
		switch i {
		case 1:
			m.SetWorkerDown("worker-2", true)
			m.SetWorkerDown("worker-1", false)
			fmt.Println(">> operator: worker-2 lost, worker-1 recovered")
		case 3:
			if m.SetWorkerDown("worker-2", false) {
				fmt.Println(">> operator: worker-2 recovered")
			}
		}
		events := m.Tick()
		fmt.Printf("--- tick %d ---\n", i+1)
		for _, event := range events {
			fmt.Printf("  %s\n", event.String())
		}
		if len(events) == 0 {
			fmt.Println("  (no events)")
		}
		fmt.Printf("  nominated: %v\n", m.NominatedSorted())
		for _, status := range m.Snapshot() {
			selected := ""
			if status.Selected {
				selected = "  <selected>"
			}
			fmt.Printf("  %-10s %-12s%s\n", status.Worker, status.State, selected)
		}
	}
	fmt.Printf("\nselected worker: %s\n", m.Selected)
}

func printConfig(e *controller.Engine) {
	fmt.Println("ClusterQueues:")
	for _, cq := range e.Store.ListClusterQueues() {
		cohort := cq.Spec.CohortName
		if cohort == "" {
			cohort = "-"
		}
		extra := ""
		if cq.Spec.AdmissionScope != "" {
			extra += " admissionScope=" + string(cq.Spec.AdmissionScope)
		}
		if cq.Spec.ConcurrentAdmission != nil {
			extra += " concurrentAdmission=on"
		}
		if len(cq.Spec.DeviceClassMappings) > 0 {
			extra += fmt.Sprintf(" deviceMappings=%v", cq.Spec.DeviceClassMappings)
		}
		fmt.Printf("  %-10s cohort=%-8s strategy=%-14s preemption=%s/%s%s\n",
			cq.Name, cohort, cq.Spec.QueueingStrategy,
			emptyDash(string(cq.Spec.Preemption.WithinClusterQueue)),
			emptyDash(string(cq.Spec.Preemption.ReclaimWithinCohort)), extra)
	}
	fmt.Println("LocalQueues:")
	for _, q := range e.Store.ListLocalQueues() {
		stop := ""
		if q.StopPolicy != "" && q.StopPolicy != api.StopNone {
			stop = fmt.Sprintf(" stopPolicy=%s", q.StopPolicy)
		}
		fmt.Printf("  %s/%s -> %s%s\n", q.Namespace, q.Name, q.ClusterQueue, stop)
	}
	if topologies := e.Store.ListTopologies(); len(topologies) > 0 {
		fmt.Println("Topologies:")
		for _, t := range topologies {
			fmt.Printf("  %s levels=%v domains=%d\n", t.Name, t.Levels, len(e.Store.ListTopologyDomains(t.Name)))
		}
	}
	if providers := e.Store.ListCapacityProviders(); len(providers) > 0 {
		fmt.Println("CapacityProviders:")
		for _, p := range providers {
			fmt.Printf("  %s capacity=%v\n", p.Name, p.Capacity)
		}
	}
	if e.WaitForPodsReady != nil {
		fmt.Printf("WaitForPodsReady: timeout=%d ticks maxRetries=%d\n", e.WaitForPodsReady.Timeout, e.WaitForPodsReady.MaxRetries)
	}
	if e.AFS != nil {
		fmt.Printf("AdmissionFairSharing: halfLife=%.1f ticks entryPenalty=%.2f\n",
			e.AFS.HalfLifeTicks, e.AFS.EntryPenalty)
	}
	if e.Provisioning != nil {
		fmt.Printf("Provisioning: check=%s delay=%d ticks failures=%d\n",
			e.Provisioning.CheckName, e.Provisioning.DelayTicks, e.Provisioning.Failures)
	}
	if e.Backoff != nil {
		fmt.Printf("Backoff: base=%d max=%d multiplier=%.1f\n",
			e.Backoff.BaseTicks, e.Backoff.MaxTicks, e.Backoff.Multiplier)
	}
	if len(e.ResourceTransformations) > 0 {
		fmt.Println("ResourceTransformations:")
		for _, transformation := range e.ResourceTransformations {
			fmt.Printf("  %s strategy=%s multiplyBy=%s outputs=%s\n",
				transformation.Input, emptyDash(string(transformation.Strategy)),
				emptyDash(transformation.MultiplyBy), compact(transformation.Outputs))
		}
	}
	if len(e.PreemptionConfigs) > 0 {
		names := make([]string, 0, len(e.PreemptionConfigs))
		for name := range e.PreemptionConfigs {
			names = append(names, name)
		}
		sort.Strings(names)
		fmt.Println("PreemptionConfigs:")
		for _, name := range names {
			config := e.PreemptionConfigs[name]
			fmt.Printf("  %s rules=%d\n", name, len(config.Rules))
		}
	}
	fmt.Println()
}

func printWorkloads(e *controller.Engine) {
	workloads := controller.SortedWorkloads(e.Store)
	fmt.Printf("  %-8s %-12s %-14s %-6s %-24s %s\n", "NAMESPACE", "NAME", "STATE", "PRIO", "RESERVED", "CONDITIONS")
	for _, w := range workloads {
		reserved := "-"
		if w.Status.Admission != nil {
			parts := make([]string, 0, len(w.Status.Admission.FlavorAssignments))
			for _, fa := range w.Status.Admission.FlavorAssignments {
				parts = append(parts, fmt.Sprintf("g%d/%s", fa.GroupIndex, fa.Flavor))
			}
			for _, ta := range w.Status.Admission.TopologyAssignments {
				parts = append(parts, fmt.Sprintf("tas:%s/%s", ta.Level, ta.Domain))
			}
			reserved = w.Status.Admission.ClusterQueue + "[" + strings.Join(parts, ",") + "]"
		}
		conds := make([]string, 0, len(w.Status.Conditions))
		for _, c := range w.Status.Conditions {
			if c.Status == "True" {
				conds = append(conds, c.Type+"="+c.Reason)
			}
		}
		fmt.Printf("  %-8s %-12s %-14s %-6d %-24s %s\n",
			w.Namespace, w.Name, w.Status.State, w.EffectivePriority(e.Store.PriorityClasses()),
			reserved, strings.Join(conds, ","))
	}
	if len(workloads) == 0 {
		fmt.Println("  (no workloads)")
	}
}

func printUsage(e *controller.Engine) {
	cqs := e.Store.ListClusterQueues()
	sort.Slice(cqs, func(i, j int) bool { return cqs[i].Name < cqs[j].Name })
	fmt.Print("  usage: ")
	parts := make([]string, 0, len(cqs))
	for _, cq := range cqs {
		usage := e.Cache.CQUsage(cq.Name)
		parts = append(parts, fmt.Sprintf("%s=%s", cq.Name, compact(usage)))
	}
	fmt.Println(strings.Join(parts, "  "))
}

func printQueueStatus(e *controller.Engine, cq string) {
	status, ok := e.QueueStatus(cq)
	if !ok {
		fmt.Printf("\n== visibility: cluster queue %q not found ==\n", cq)
		return
	}
	fmt.Printf("\n== visibility: %s ==\n", status.ClusterQueue)
	fmt.Printf("  usage:  %s\n", compact(status.Usage))
	fmt.Printf("  nominal: %s\n", compact(status.Nominal))
	fmt.Printf("  pending=%d reserving=%d admitted=%d finished=%d\n",
		len(status.Pending), len(status.QuotaReserved), len(status.Admitted), len(status.Finished))
	if len(status.Pending) > 0 {
		fmt.Printf("  pending workloads: %s\n", strings.Join(status.Pending, ", "))
		reasons := make([]string, 0, len(status.PendingByReason))
		for reason, count := range status.PendingByReason {
			reasons = append(reasons, fmt.Sprintf("%s=%d", reason, count))
		}
		sort.Strings(reasons)
		fmt.Printf("  pending reasons: %s\n", strings.Join(reasons, ", "))
	}
}

func compact(r api.ResourceList) string {
	if len(r) == 0 {
		return "{}"
	}
	names := make([]string, 0, len(r))
	for name := range r {
		names = append(names, string(name))
	}
	sort.Strings(names)
	parts := make([]string, 0, len(names))
	for _, name := range names {
		v := r[api.ResourceName(name)]
		if v == 0 {
			continue
		}
		parts = append(parts, fmt.Sprintf("%s:%s", name, api.FormatQuantity(api.ResourceName(name), v)))
	}
	if len(parts) == 0 {
		return "{}"
	}
	return "{" + strings.Join(parts, ",") + "}"
}

func emptyDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

func splitNamespaced(value string) (string, string, error) {
	parts := strings.SplitN(value, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", fmt.Errorf("expected namespace/name, got %q", value)
	}
	return parts[0], parts[1], nil
}

// printGet is a deliberately small analogue of `kueuectl get` / `kubectl get`.
func printGet(e *controller.Engine, kind string) error {
	fmt.Printf("\n== get %s ==\n", kind)
	switch strings.ToLower(kind) {
	case "workloads", "workload", "wl":
		printWorkloads(e)
	case "clusterqueues", "clusterqueue", "cq":
		fmt.Printf("  %-16s %-12s %-14s %-10s %-24s %s\n",
			"NAME", "COHORT", "STRATEGY", "ACTIVE", "PREEMPTION", "USAGE")
		for _, cq := range e.Store.ListClusterQueues() {
			cohort := emptyDash(cq.Spec.CohortName)
			preemption := fmt.Sprintf("within=%s reclaim=%s",
				emptyDash(string(cq.Spec.Preemption.WithinClusterQueue)),
				emptyDash(string(cq.Spec.Preemption.ReclaimWithinCohort)))
			active := "True"
			if cq.Spec.StopPolicy == api.StopHold || cq.Spec.StopPolicy == api.StopHoldAndDrain {
				active = "False"
			}
			fmt.Printf("  %-16s %-12s %-14s %-10s %-24s %s\n",
				cq.Name, cohort, cq.Spec.QueueingStrategy, active, preemption,
				compact(e.Cache.CQUsage(cq.Name)))
		}
	case "localqueues", "localqueue", "lq":
		fmt.Printf("  %-10s %-16s %-16s %-12s\n", "NAMESPACE", "NAME", "CLUSTERQUEUE", "STOPPOLICY")
		for _, lq := range e.Store.ListLocalQueues() {
			fmt.Printf("  %-10s %-16s %-16s %-12s\n",
				lq.Namespace, lq.Name, lq.ClusterQueue, emptyDash(string(lq.StopPolicy)))
		}
	case "cohorts", "cohort":
		fmt.Printf("  %-16s %-16s %-10s\n", "NAME", "PARENT", "WEIGHT")
		for _, c := range e.Store.ListCohorts() {
			weight := float64(1)
			if c.FairSharing != nil && c.FairSharing.Weight > 0 {
				weight = c.FairSharing.Weight
			}
			fmt.Printf("  %-16s %-16s %-10.2f\n", c.Name, emptyDash(c.ParentName), weight)
		}
	case "flavors", "flavor", "resourceflavors":
		fmt.Printf("  %-16s %-20s %s\n", "NAME", "TOPOLOGY", "NODE LABELS")
		for _, f := range e.Store.ListResourceFlavors() {
			fmt.Printf("  %-16s %-20s %v\n", f.Name, emptyDash(f.TopologyName), f.NodeLabels)
		}
	case "topologies", "topology":
		for _, t := range e.Store.ListTopologies() {
			fmt.Printf("  %s levels=%v domains=%d\n", t.Name, t.Levels, len(e.Store.ListTopologyDomains(t.Name)))
		}
	default:
		return fmt.Errorf("unknown resource %q (use workloads|clusterqueues|localqueues|cohorts|flavors|topologies)", kind)
	}
	return nil
}

// printDescribe is the MiniKueue analogue of `kubectl describe workload`.
func printDescribe(e *controller.Engine, namespace, name string) error {
	w, ok := e.Store.GetWorkload(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	fmt.Printf("\n== describe workload %s ==\n", w.Key())
	fmt.Printf("  queue:                %s\n", w.Spec.QueueName)
	fmt.Printf("  state:                %s\n", w.Status.State)
	fmt.Printf("  active:               %v\n", w.Spec.Active)
	fmt.Printf("  priority:             %d\n", w.EffectivePriority(e.Store.PriorityClasses()))
	fmt.Printf("  priorityClassName:    %s\n", emptyDash(w.Spec.PriorityClassName))
	fmt.Printf("  creationTimestamp:    %d\n", w.Spec.CreationTimestamp)
	fmt.Printf("  maximumExecutionSecs: %d\n", w.Spec.MaximumExecutionSeconds)
	fmt.Printf("  cumulativeExecution:  %d ticks\n", w.Status.ExecutionTicks)
	fmt.Printf("  requeueAt:            %d\n", w.Status.RequeueAt)
	fmt.Printf("  requeues:             %d\n", w.Status.Requeues)
	fmt.Printf("  evictions:            %d\n", w.Status.Evictions)
	fmt.Printf("  sequence:             %d\n", w.Status.Sequence)
	fmt.Printf("  podsReady:            %v\n", w.Status.PodsReady)
	fmt.Printf("  totalUsage:           %s\n", compact(w.TotalUsage()))
	if w.Status.ReclaimablePods != nil {
		fmt.Printf("  reclaimablePods:      %v\n", w.Status.ReclaimablePods)
	}
	if w.Status.Admission != nil {
		fmt.Printf("  admission:            %s\n", scheduler.FormatAdmission(w.Status.Admission))
		for _, ta := range w.Status.Admission.TopologyAssignments {
			fmt.Printf("  topology:             %s %s/%s pods=%d resources=%s\n",
				ta.PodSet, ta.Level, ta.Domain, ta.PodCount, compact(ta.Resources))
		}
	}
	if len(w.Status.AdmissionChecks) > 0 {
		fmt.Println("  admissionChecks:")
		for _, check := range w.Status.AdmissionChecks {
			fmt.Printf("    - %s state=%s retries=%d requeueAfter=%ds message=%q\n",
				check.Name, check.State, check.RetryCount, check.RequeueAfterSeconds, check.Message)
		}
	}
	if len(w.Status.Conditions) > 0 {
		fmt.Println("  conditions:")
		for _, c := range w.Status.Conditions {
			fmt.Printf("    - %s=%s reason=%s message=%q sinceTick=%d\n",
				c.Type, c.Status, c.Reason, c.Message, c.LastTransitionTick)
		}
	}
	if len(w.Status.EvictionStats) > 0 {
		fmt.Println("  evictionStats:")
		for _, stat := range w.Status.EvictionStats {
			fmt.Printf("    - %s/%s count=%d\n", stat.Reason, stat.UnderlyingCause, stat.Count)
		}
	}
	return nil
}

// printExplain renders the structured decision diagnosis produced by
// Engine.Explain. It is the MiniKueue analogue of "why is this Pending?".
func printExplain(e *controller.Engine, namespace, name string) error {
	ex, ok := e.Explain(namespace, name)
	if !ok {
		return fmt.Errorf("workload %s/%s not found", namespace, name)
	}
	fmt.Printf("\n== explain %s/%s ==\n", namespace, name)
	fmt.Printf("  queue:        %s -> %s\n", ex.Queue, emptyDash(ex.ClusterQueue))
	fmt.Printf("  state:        %s  active=%v  requeues=%d  requeueAt=%d  evictions=%d\n",
		ex.State, ex.Active, ex.Requeues, ex.RequeueAt, ex.Evictions)
	fmt.Printf("  requested:    %s\n", compact(ex.Requested))
	fmt.Printf("  verdict:      %s - %s\n", ex.Reason, ex.Detail)
	fmt.Printf("  feasibility:  direct=%v quota-only=%v\n", ex.DirectFeasible, ex.QuotaFeasible)
	if ex.Admission != nil {
		fmt.Printf("  witness:      %s\n", scheduler.FormatAdmission(ex.Admission))
	}

	for _, group := range ex.Groups {
		fmt.Printf("  resource group %d: resources=%v requested=%s\n",
			group.Index, group.CoveredResources, compact(group.Requested))
		for _, flavor := range group.Flavors {
			status := "YES"
			if !flavor.Feasible {
				status = "NO"
			}
			fmt.Printf("    flavor %-16s %s", flavor.Flavor, status)
			if flavor.BlockedBy != "" {
				fmt.Printf(" (blocked by %s)", flavor.BlockedBy)
			}
			fmt.Println()
			for _, check := range flavor.Checks {
				fmt.Printf("      %-8s current=%d nominal=%d request=%d new=%d",
					check.Resource, check.Current, check.Nominal, check.Requested, check.NewUsage)
				if check.HasBorrowingLimit {
					fmt.Printf(" borrow=%d borrowingLimit=%d", check.Borrow, check.BorrowingLimit)
				}
				if check.HasCohortLimit {
					fmt.Printf(" cohort=%s limit=%d", check.CohortLimitName, check.CohortLimit)
				}
				if check.Lendable > 0 {
					fmt.Printf(" lendable=%d", check.Lendable)
				}
				if check.BlockedBy != "" {
					fmt.Printf("  -> %s: %s", check.BlockedBy, check.Detail)
				}
				fmt.Println()
			}
		}
	}

	if len(ex.Topology) == 0 {
		fmt.Println("  topology:     none referenced by the flavors")
	}
	for _, topology := range ex.Topology {
		fmt.Printf("  topology %s levels=%v\n", topology.Name, topology.Levels)
		for _, domain := range topology.Domains {
			fmt.Printf("    %-24s capacity=%s usage=%s free=%s\n",
				domain.Domain, compact(domain.Capacity), compact(domain.Usage), compact(domain.Free))
		}
	}

	if ex.Preemption != nil {
		fmt.Printf("  preemption:   enabled=%v trigger=%s candidates=%d\n",
			ex.Preemption.Enabled, ex.Preemption.Trigger, ex.Preemption.CandidateCount)
		if len(ex.Preemption.Candidates) > 0 {
			fmt.Printf("    candidates: %s\n", strings.Join(ex.Preemption.Candidates, ", "))
		}
		if len(ex.Preemption.Victims) > 0 {
			fmt.Printf("    victims:    %s\n", strings.Join(ex.Preemption.Victims, ", "))
		}
		if ex.Preemption.Detail != "" {
			fmt.Printf("    %s\n", ex.Preemption.Detail)
		}
	}

	if len(ex.AdmissionGates) > 0 {
		fmt.Printf("  admission gates: %v\n", ex.AdmissionGates)
	}
	if len(ex.AdmissionChecks) > 0 {
		fmt.Println("  admission checks:")
		for _, check := range ex.AdmissionChecks {
			fmt.Printf("    - %s state=%s retries=%d message=%q\n",
				check.Name, check.State, check.RetryCount, check.Message)
		}
	}
	if len(ex.Conditions) > 0 {
		fmt.Println("  conditions:")
		for _, condition := range ex.Conditions {
			fmt.Printf("    - %s=%s reason=%s message=%q\n",
				condition.Type, condition.Status, condition.Reason, condition.Message)
		}
	}
	return nil
}

// runWhatIf runs one scenario twice with the same deterministic inputs, applies
// a single quota override to the second run, and compares the resulting
// workload states. It is a counterfactual, not a prediction of production
// behaviour: the scenario is replayed from tick zero.
func runWhatIf(name string, ticks int, spec string) error {
	baseline, _, err := scenario.Run(name)
	if err != nil {
		return err
	}
	modified, _, err := scenario.Run(name)
	if err != nil {
		return err
	}
	if err := applyQuotaOverride(modified, spec); err != nil {
		return err
	}
	for i := 0; i < ticks; i++ {
		baseline.Tick()
		modified.Tick()
	}

	workloads := baseline.Store.ListWorkloads()
	sort.Slice(workloads, func(i, j int) bool { return workloads[i].Key() < workloads[j].Key() })

	fmt.Printf("\n== what-if: %s (ticks=%d) ==\n", spec, ticks)
	fmt.Printf("  %-24s %-14s %-14s\n", "WORKLOAD", "BEFORE", "AFTER")
	beforePending, afterPending := 0, 0
	beforeAdmitted, afterAdmitted := 0, 0
	for _, before := range workloads {
		after, _ := modified.Store.GetWorkload(before.Namespace, before.Name)
		afterState := api.StatePending
		if after != nil {
			afterState = after.Status.State
		}
		fmt.Printf("  %-24s %-14s %-14s\n", before.Key(), before.Status.State, afterState)
		if before.Status.State == api.StatePending {
			beforePending++
		}
		if afterState == api.StatePending {
			afterPending++
		}
		if before.Status.State == api.StateAdmitted {
			beforeAdmitted++
		}
		if afterState == api.StateAdmitted {
			afterAdmitted++
		}
	}
	fmt.Printf("  summary: pending %d -> %d, admitted %d -> %d\n",
		beforePending, afterPending, beforeAdmitted, afterAdmitted)
	return nil
}

// applyQuotaOverride parses clusterqueue/flavor/resource=value and updates the
// in-memory ClusterQueue. Only the nominal quota is changed; cohort limits,
// borrowing and preemption are deliberately left untouched so a single
// counterfactual has a single cause.
func applyQuotaOverride(e *controller.Engine, spec string) error {
	parts := strings.SplitN(spec, "=", 2)
	if len(parts) != 2 {
		return fmt.Errorf("expected clusterqueue/flavor/resource=value, got %q", spec)
	}
	value, err := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
	if err != nil || value < 0 {
		return fmt.Errorf("invalid quota value %q", parts[1])
	}
	path := strings.Split(parts[0], "/")
	if len(path) != 3 {
		return fmt.Errorf("expected clusterqueue/flavor/resource=value, got %q", spec)
	}
	cq, ok := e.Store.GetClusterQueue(path[0])
	if !ok {
		return fmt.Errorf("ClusterQueue %q not found", path[0])
	}
	resource := api.ResourceName(path[2])
	found := false
	for gi := range cq.Spec.ResourceGroups {
		for fi := range cq.Spec.ResourceGroups[gi].Flavors {
			flavor := &cq.Spec.ResourceGroups[gi].Flavors[fi]
			if flavor.Name != path[1] {
				continue
			}
			if flavor.Nominal == nil {
				flavor.Nominal = api.ResourceList{}
			}
			flavor.Nominal[resource] = value
			found = true
		}
	}
	if !found {
		return fmt.Errorf("flavor %q not found in ClusterQueue %q", path[1], path[0])
	}
	return e.Store.UpsertClusterQueue(cq)
}
