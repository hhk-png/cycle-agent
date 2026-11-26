// Package scheduler wires actions and plugins into a Volcano-style scheduling
// loop. The loop is deliberately single-threaded and deterministic; production
// Volcano runs actions in a session with concurrent informer caches feeding it.
package scheduler

import (
	"fmt"

	"example.com/minivolcano/internal/actions"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/metrics"
	"example.com/minivolcano/internal/plugins"
)

type Scheduler struct {
	Cache   *cache.Cache
	Config  framework.Config
	Metrics *metrics.Registry
	Last    *framework.Session

	pluginInstances []framework.PluginInstance
	actions         []actions.Action
}

func New(c *cache.Cache, cfg framework.Config, reg *metrics.Registry) (*Scheduler, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if cfg.HasPlugin("proportion") && cfg.HasPlugin("capacity") {
		return nil, fmt.Errorf("scheduler config: proportion and capacity are mutually exclusive")
	}
	var instances []framework.PluginInstance
	for tierIdx, tier := range cfg.Tiers {
		for _, pc := range tier.Plugins {
			p, err := plugins.Build(pc.Name, pc.Args)
			if err != nil {
				return nil, fmt.Errorf("tier %q: %w", tier.Name, err)
			}
			instances = append(instances, framework.PluginInstance{Plugin: p, Tier: tierIdx, Args: pc.Args})
		}
	}
	var acts []actions.Action
	for _, name := range cfg.Actions {
		a, err := actions.Build(name)
		if err != nil {
			return nil, err
		}
		acts = append(acts, a)
	}
	if reg == nil {
		reg = metrics.New()
	}
	return &Scheduler{Cache: c, Config: cfg, Metrics: reg, pluginInstances: instances, actions: acts}, nil
}

// Run executes one session at the given tick and publishes the resulting
// snapshot back into the shared cache.
func (s *Scheduler) Run(now int64) (*framework.Session, error) {
	work := s.Cache.Clone()
	work.Now = now
	ssn := framework.NewSession(work, now, s.Config, s.pluginInstances)
	for _, a := range s.actions {
		ssn.Action = a.Name()
		if err := a.Execute(ssn); err != nil {
			s.Metrics.Inc("minivolcano_scheduler_action_errors_total", "Scheduler action errors", map[string]string{"action": a.Name()})
			ssn.Close()
			return nil, fmt.Errorf("action %s: %w", a.Name(), err)
		}
	}
	ssn.Close()

	publish(s.Cache, ssn.Cache)
	s.Last = ssn
	s.Metrics.Inc("minivolcano_scheduler_sessions_total", "Completed scheduling sessions", nil)
	for _, e := range ssn.Events {
		labels := map[string]string{"type": e.Type}
		s.Metrics.Inc("minivolcano_scheduler_events_total", "Scheduler events by type", labels)
	}
	return ssn, nil
}

func publish(dst, src *cache.Cache) {
	dst.Now = src.Now
	dst.Nodes = src.Nodes
	dst.Pods = src.Pods
	dst.Jobs = src.Jobs
	dst.PodGroups = src.PodGroups
	dst.Queues = src.Queues
	dst.Reservations = src.Reservations
	dst.HyperNodes = src.HyperNodes
	dst.PriorityClasses = src.PriorityClasses
}

// SetMetricsGauges publishes cache-derived gauges. It is called by the
// simulator after each tick, mirroring Volcano's periodic metrics collection.
func (s *Scheduler) SetMetricsGauges() {
	for _, name := range []string{
		"minivolcano_queue_allocated",
		"minivolcano_queue_deserved",
		"minivolcano_jobs",
		"minivolcano_node_idle",
	} {
		s.Metrics.Reset(name)
	}
	for _, q := range s.Cache.QueuesSorted() {
		used := s.Cache.QueueAllocated(q.Name)
		// Emit explicit zero samples for dimensions the queue knows about, so
		// a fully drained queue still has a gauge rather than a missing series.
		keys := map[string]bool{}
		for _, k := range used.Keys() {
			keys[k] = true
		}
		for _, k := range q.Status.Deserved.Keys() {
			keys[k] = true
		}
		for k := range keys {
			s.Metrics.Set("minivolcano_queue_allocated", "Resources allocated per queue",
				map[string]string{"queue": q.Name, "resource": k}, float64(used[k]))
		}
		for _, k := range q.Status.Deserved.Keys() {
			s.Metrics.Set("minivolcano_queue_deserved", "Deserved resources per queue",
				map[string]string{"queue": q.Name, "resource": k}, float64(q.Status.Deserved[k]))
		}
	}
	phaseCounts := map[string]int{}
	for _, j := range s.Cache.Jobs {
		phaseCounts[string(j.Status.Phase)]++
	}
	for phase, n := range phaseCounts {
		s.Metrics.Set("minivolcano_jobs", "Jobs by phase", map[string]string{"phase": phase}, float64(n))
	}
	for _, n := range s.Cache.NodesSorted() {
		idle := n.Node.Allocatable.Sub(n.Node.Allocated)
		for _, k := range n.Node.Allocatable.Keys() {
			s.Metrics.Set("minivolcano_node_idle", "Node idle resources by dimension",
				map[string]string{"node": n.Node.Name, "resource": k}, float64(idle[k]))
		}
	}
}

// ExplainReasons returns the last session's evidence for a job.
func (s *Scheduler) ExplainReasons(jobUID string) []string {
	if s.Last == nil {
		return nil
	}
	return s.Last.UnschedulableReasons(jobUID)
}
