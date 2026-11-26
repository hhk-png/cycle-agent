package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/scenario"
	"example.com/minivolcano/internal/sim"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run() error {
	var (
		list    = flag.Bool("list", false, "list scenarios")
		name    = flag.String("scenario", "basic", "scenario name")
		all     = flag.Bool("all", false, "run every scenario and its assertions")
		ticks   = flag.Int("ticks", 0, "override scenario tick count")
		cfgPath = flag.String("config", "", "scheduler config JSON (overrides scenario config)")
		explain = flag.String("explain", "", "comma-separated job names to explain")
		whatIf  = flag.String("what-if", "", "override queue capability, e.g. team-a/nvidia.com/gpu=4")
		metrics = flag.Bool("metrics", false, "print Prometheus metrics")
		events  = flag.Int("events", 12, "number of recent events to print (0 disables)")
		asJSON  = flag.Bool("json", false, "print a JSON summary")
		noCheck = flag.Bool("no-check", false, "skip scenario assertions (useful with -ticks/-what-if)")
	)
	flag.Parse()

	if *list {
		fmt.Printf("%-16s %-5s %s\n", "NAME", "TICKS", "DESCRIPTION")
		for _, s := range scenario.All() {
			fmt.Printf("%-16s %-5d %s\n", s.Name, s.Ticks, s.Description)
		}
		return nil
	}

	if *all {
		outcomes := scenario.RunAll()
		failed := 0
		for _, o := range outcomes {
			if o.Err != nil {
				failed++
				fmt.Printf("FAIL %-16s %v\n", o.Name, o.Err)
			} else {
				fmt.Printf("PASS %-16s\n", o.Name)
			}
		}
		fmt.Printf("\n%d scenarios, %d passed, %d failed\n", len(outcomes), len(outcomes)-failed, failed)
		if failed > 0 {
			return fmt.Errorf("%d scenario(s) failed", failed)
		}
		return nil
	}

	sc, err := scenario.Get(*name)
	if err != nil {
		return err
	}
	if *ticks > 0 {
		sc.Ticks = *ticks
	}
	if *noCheck {
		sc.Check = nil
	}
	var cfg *framework.Config
	if *cfgPath != "" {
		loaded, err := framework.LoadConfig(*cfgPath)
		if err != nil {
			return err
		}
		cfg = &loaded
		sc.Config = func() framework.Config { return loaded }
	}

	overrides, err := parseOverride(*whatIf)
	if err != nil {
		return err
	}
	w, runErr := scenario.RunScenario(sc, overrides)
	if w == nil {
		return runErr
	}

	fmt.Printf("SCENARIO %s\n%s\n", sc.Name, sc.Description)
	if cfg != nil {
		fmt.Printf("CONFIG actions=%s plugins=%s\n", strings.Join(cfg.Actions, ","), strings.Join(cfg.PluginNames(), ","))
	}
	fmt.Println()
	fmt.Print(w.Summary())

	if *events > 0 {
		fmt.Printf("\nEVENTS (last %d)\n%s", *events, w.EventLog(*events))
	}

	if *explain != "" {
		for _, q := range strings.Split(*explain, ",") {
			q = strings.TrimSpace(q)
			if q == "" {
				continue
			}
			fmt.Printf("\nEXPLAIN %s\n%s", q, w.Explain(q))
		}
	}

	if *asJSON {
		out, err := json.MarshalIndent(jsonSummary(w), "", "  ")
		if err != nil {
			return err
		}
		fmt.Printf("\nJSON\n%s\n", out)
	}

	if *metrics {
		fmt.Printf("\nMETRICS\n%s", w.MetricsText())
	}

	if runErr != nil {
		return runErr
	}
	return nil
}

func parseOverride(s string) (map[string]int64, error) {
	out := map[string]int64{}
	if strings.TrimSpace(s) == "" {
		return out, nil
	}
	for _, item := range strings.Split(s, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		key, val, ok := strings.Cut(item, "=")
		if !ok {
			return nil, fmt.Errorf("what-if %q must be queue/resource=value", item)
		}
		n, err := strconv.ParseInt(val, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("what-if value %q is not an integer", val)
		}
		out[strings.TrimSpace(key)] = n
	}
	return out, nil
}

func jsonSummary(w *sim.World) map[string]any {
	queues := []map[string]any{}
	for _, q := range w.Cache.QueuesSorted() {
		queues = append(queues, map[string]any{
			"name": q.Name, "state": q.State(), "weight": q.Spec.Weight,
			"deserved": q.Status.Deserved.String(), "allocated": q.Status.Allocated.String(),
		})
	}
	jobs := []map[string]any{}
	for _, j := range w.Cache.JobsSorted() {
		jobs = append(jobs, map[string]any{
			"name": j.Name, "namespace": j.Namespace, "queue": j.Spec.Queue,
			"phase": j.Status.Phase, "ready": w.Cache.ReadyCount(j.UID),
			"minAvailable": j.Spec.MinAvailable, "retries": j.Status.Retries,
			"message": j.Status.Message,
		})
	}
	nodes := []map[string]any{}
	for _, n := range w.Cache.NodesSorted() {
		nodes = append(nodes, map[string]any{
			"name": n.Node.Name, "allocated": n.Node.Allocated.String(),
			"allocatable": n.Node.Allocatable.String(), "pods": len(n.Pods),
		})
	}
	sort.Slice(queues, func(i, j int) bool { return queues[i]["name"].(string) < queues[j]["name"].(string) })
	return map[string]any{
		"scenario": w.Name,
		"tick":     w.Now,
		"queues":   queues,
		"jobs":     jobs,
		"nodes":    nodes,
		"events":   len(w.Events),
	}
}
