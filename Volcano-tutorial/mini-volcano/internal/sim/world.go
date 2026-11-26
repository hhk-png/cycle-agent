// Package sim assembles the cache, controller and scheduler into a
// deterministic discrete-event world used by the scenarios and CLI.
package sim

import (
	"fmt"
	"sort"
	"strings"
	"text/tabwriter"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/controller"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/metrics"
	"example.com/minivolcano/internal/scheduler"
)

type World struct {
	Name        string
	Description string
	Cache       *cache.Cache
	Manager     *controller.Manager
	Scheduler   *scheduler.Scheduler
	Metrics     *metrics.Registry
	Now         int64
	Events      []api.Event
	started     bool

	// Overrides lets -what-if rerun a scenario with a queue resource change.
	Overrides map[string]int64
}

func New(name, description string, cfg framework.Config) (*World, error) {
	c := cache.New()
	reg := metrics.New()
	sched, err := scheduler.New(c, cfg, reg)
	if err != nil {
		return nil, err
	}
	return &World{
		Name:        name,
		Description: description,
		Cache:       c,
		Manager:     controller.New(c, reg),
		Scheduler:   sched,
		Metrics:     reg,
		Overrides:   map[string]int64{},
	}, nil
}

func (w *World) AddNode(n *api.Node) error                 { return w.Cache.AddNode(n) }
func (w *World) AddQueue(q *api.Queue) error               { return w.Cache.AddQueue(q) }
func (w *World) AddHyperNode(h *api.HyperNode) error       { return w.Cache.AddHyperNode(h) }
func (w *World) AddReservation(r *api.Reservation) error   { return w.Cache.AddReservation(r) }
func (w *World) AddPDB(p *api.PodDisruptionBudget) error   { return w.Cache.AddPDB(p) }
func (w *World) AddQuota(q *api.ResourceQuota) error       { return w.Cache.AddResourceQuota(q) }
func (w *World) AddPriorityClass(name string, value int32) { w.Cache.AddPriorityClass(name, value) }
func (w *World) SubmitJob(j *api.Job) error                { return w.Cache.SubmitJob(j) }
func (w *World) AddCron(c *api.CronJob) error              { return w.Manager.AddCron(c) }
func (w *World) AddFlow(f *api.JobFlow) error              { return w.Manager.AddFlow(f) }
func (w *World) FailPod(uid string, tick int64)            { w.Manager.ScheduleFailure(uid, tick) }

// Start performs the tick-0 reconcile and scheduling pass.
func (w *World) Start() error {
	if w.started {
		return nil
	}
	w.started = true
	if err := w.Cache.ValidateTopology(); err != nil {
		return err
	}
	if w.Scheduler.Config.HasPlugin("proportion") {
		for _, q := range w.Cache.Queues {
			if q.Spec.Parent != "" {
				return fmt.Errorf("queue %q uses parent %q, but proportion does not support hierarchical queues; use capacity", q.Name, q.Spec.Parent)
			}
		}
	}
	w.Manager.Sync(0)
	if err := w.runScheduler(0); err != nil {
		return err
	}
	w.Manager.Sync(0)
	w.Scheduler.SetMetricsGauges()
	w.syncEvents()
	return nil
}

// Tick advances one scheduler tick.
func (w *World) Tick() error {
	w.Now++
	w.Manager.Sync(w.Now)
	if err := w.runScheduler(w.Now); err != nil {
		return err
	}
	w.Manager.Sync(w.Now)
	w.Scheduler.SetMetricsGauges()
	w.syncEvents()
	return nil
}

func (w *World) Run(ticks int) error {
	if err := w.Start(); err != nil {
		return err
	}
	for i := 0; i < ticks; i++ {
		if err := w.Tick(); err != nil {
			return err
		}
	}
	return nil
}

func (w *World) runScheduler(now int64) error {
	ssn, err := w.Scheduler.Run(now)
	if err != nil {
		return err
	}
	for _, e := range ssn.Events {
		w.Manager.Emit(e)
	}
	return nil
}

func (w *World) syncEvents() {
	w.Events = append([]api.Event(nil), w.Manager.Events...)
}

func (w *World) FindJob(query string) *api.Job {
	if query == "" {
		return nil
	}
	for _, j := range w.Cache.Jobs {
		if j.Key() == query || j.Name == query {
			return j
		}
	}
	return nil
}

// Summary renders the three tables used by the CLI.
func (w *World) Summary() string {
	var b strings.Builder
	b.WriteString(fmt.Sprintf("MiniVolcano world %q at tick %d\n", w.Name, w.Now))

	b.WriteString("\nQUEUES\n")
	tw := tabwriter.NewWriter(&b, 0, 4, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tSTATE\tWEIGHT\tDESERVED\tALLOCATED\tPENDING/INQUEUE/RUNNING")
	for _, q := range w.Cache.QueuesSorted() {
		fmt.Fprintf(tw, "%s\t%s\t%d\t%s\t%s\t%d/%d/%d\n",
			q.Name, q.State(), q.Spec.Weight, q.Status.Deserved, q.Status.Allocated,
			q.Status.Pending, q.Status.Inqueue, q.Status.Running)
	}
	tw.Flush()

	b.WriteString("\nJOBS\n")
	tw = tabwriter.NewWriter(&b, 0, 4, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tQUEUE\tPHASE\tREADY/MIN\tRUNNING\tSUCCEEDED\tFAILED\tRETRIES")
	for _, j := range w.Cache.JobsSorted() {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%d/%d\t%d\t%d\t%d\t%d\n",
			j.Key(), j.Spec.Queue, j.Status.Phase, w.Cache.ReadyCount(j.UID), j.Spec.MinAvailable,
			j.Status.Running, j.Status.Succeeded, j.Status.Failed, j.Status.Retries)
	}
	tw.Flush()

	b.WriteString("\nNODES\n")
	tw = tabwriter.NewWriter(&b, 0, 4, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tALLOCATED\tALLOCATABLE\tPODS\tTAINTS")
	for _, n := range w.Cache.NodesSorted() {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%d\t%d\n",
			n.Node.Name, n.Node.Allocated, n.Node.Allocatable, len(n.Pods), len(n.Node.Taints))
	}
	tw.Flush()
	return b.String()
}

// Explain produces an evidence chain for a Pending/Running job.
func (w *World) Explain(query string) string {
	job := w.FindJob(query)
	if job == nil {
		return fmt.Sprintf("job %q not found\n", query)
	}
	var b strings.Builder
	fmt.Fprintf(&b, "JOB %s\n", job.Key())
	fmt.Fprintf(&b, "  phase=%s minAvailable=%d replicas=%d maxRetry=%d priority=%d\n",
		job.Status.Phase, job.Spec.MinAvailable, job.TotalReplicas(), job.Spec.MaxRetry, job.Spec.Priority)
	pg := w.Cache.PodGroups[job.UID]
	if pg != nil {
		fmt.Fprintf(&b, "  podgroup=%s phase=%s minMember=%d minResources=%s\n",
			pg.Name, pg.Phase, pg.MinMember, pg.MinResources)
	}
	fmt.Fprintf(&b, "  ready=%d running=%d succeeded=%d failed=%d pending=%d retries=%d\n",
		w.Cache.ReadyCount(job.UID), w.Cache.RunningCount(job.UID), w.Cache.SucceededCount(job.UID),
		w.Cache.FailedCount(job.UID), w.Cache.PendingCount(job.UID), job.Status.Retries)
	q := w.Cache.Queues[job.Spec.Queue]
	if q != nil {
		fmt.Fprintf(&b, "  queue=%s state=%s capability=%s deserved=%s allocated=%s\n",
			q.Name, q.State(), q.Spec.Capability, q.Status.Deserved, q.Status.Allocated)
	}
	fmt.Fprintf(&b, "  message=%q\n", job.Status.Message)

	reasons := w.Scheduler.ExplainReasons(job.UID)
	if len(reasons) == 0 {
		fmt.Fprintln(&b, "  reasons: none recorded in the last session")
	} else {
		fmt.Fprintln(&b, "  reasons:")
		for _, r := range reasons {
			fmt.Fprintf(&b, "    - %s\n", r)
		}
	}

	fmt.Fprintln(&b, "  pods:")
	pods := w.Cache.JobPods(job.UID)
	sort.SliceStable(pods, func(i, j int) bool { return pods[i].Name < pods[j].Name })
	for _, p := range pods {
		fmt.Fprintf(&b, "    %-40s %-10s node=%-12s numa=%d restarts=%d %s\n",
			p.Name, p.Phase, p.NodeName, p.NUMANode, p.Restarts, p.Message)
	}

	fmt.Fprintln(&b, "  recent events:")
	shown := 0
	for i := len(w.Events) - 1; i >= 0 && shown < 8; i-- {
		e := w.Events[i]
		if e.Job != job.Name && e.Job != job.Key() {
			continue
		}
		fmt.Fprintf(&b, "    %s\n", e)
		shown++
	}
	return b.String()
}

func (w *World) EventLog(limit int) string {
	var b strings.Builder
	start := 0
	if limit > 0 && len(w.Events) > limit {
		start = len(w.Events) - limit
	}
	for _, e := range w.Events[start:] {
		b.WriteString("  ")
		b.WriteString(e.String())
		b.WriteString("\n")
	}
	return b.String()
}

func (w *World) MetricsText() string {
	return w.Metrics.Render()
}
