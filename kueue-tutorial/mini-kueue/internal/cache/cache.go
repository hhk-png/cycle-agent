// Package cache tracks quota reserved by workloads. It is the analogue of
// Kueue's scheduler cache: admission decisions are made against this cache,
// and preemption is simulated by subtracting candidate workloads from a
// computed usage total before committing the decision.
package cache

import (
	"fmt"
	"sort"
	"sync"

	"example.com/minikueue/internal/api"
)

type cqFlavorKey struct {
	cq     string
	flavor string
}

// Cache is concurrency-safe.
type Cache struct {
	mu sync.RWMutex
	// usage[cq][flavor][resource] is the reserved quantity.
	usage map[string]map[string]api.ResourceList
	// workloads holds every workload that currently reserves quota.
	workloads map[string]*api.Workload
	sequence  int64
}

// New creates an empty cache.
func New() *Cache {
	return &Cache{
		usage:     map[string]map[string]api.ResourceList{},
		workloads: map[string]*api.Workload{},
	}
}

// Add records a workload's quota reservation. The workload must have
// Status.Admission set. Adding the same key twice is an error.
func (c *Cache) Add(w *api.Workload) error {
	if w == nil || w.Status.Admission == nil {
		return fmt.Errorf("workload has no admission")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	key := w.Key()
	if _, exists := c.workloads[key]; exists {
		return fmt.Errorf("workload %s already reserves quota", key)
	}
	c.sequence++
	w.Status.Sequence = c.sequence
	if c.usage[w.Status.Admission.ClusterQueue] == nil {
		c.usage[w.Status.Admission.ClusterQueue] = map[string]api.ResourceList{}
	}
	for _, fa := range w.Status.Admission.FlavorAssignments {
		if c.usage[w.Status.Admission.ClusterQueue][fa.Flavor] == nil {
			c.usage[w.Status.Admission.ClusterQueue][fa.Flavor] = api.ResourceList{}
		}
		c.usage[w.Status.Admission.ClusterQueue][fa.Flavor].Add(fa.Resources)
	}
	c.workloads[key] = w.Clone()
	return nil
}

// Remove releases a workload's quota reservation.
func (c *Cache) Remove(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	w, ok := c.workloads[key]
	if !ok {
		return
	}
	cq := w.Status.Admission.ClusterQueue
	for _, fa := range w.Status.Admission.FlavorAssignments {
		if c.usage[cq][fa.Flavor] == nil {
			continue
		}
		c.usage[cq][fa.Flavor].Sub(fa.Resources)
	}
	delete(c.workloads, key)
}

// Refresh replaces the cached copy of a workload's status without changing
// quota usage or its admission sequence. The controller reserves quota while
// the workload is still QuotaReserved and only then sets Admitted; without a
// refresh the cache would keep returning the pre-admission snapshot (notably
// AdmittedAt=0) to preemption and diagnostics.
func (c *Cache) Refresh(w *api.Workload) error {
	if w == nil {
		return fmt.Errorf("nil workload")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	key := w.Key()
	existing, ok := c.workloads[key]
	if !ok {
		return fmt.Errorf("workload %s does not reserve quota", key)
	}
	updated := w.Clone()
	updated.Status.Sequence = existing.Status.Sequence
	c.workloads[key] = updated
	return nil
}

// Has reports whether a workload reserves quota.
func (c *Cache) Has(key string) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	_, ok := c.workloads[key]
	return ok
}

// Get returns a clone of a reserved workload.
func (c *Cache) Get(key string) (*api.Workload, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	w, ok := c.workloads[key]
	if !ok {
		return nil, false
	}
	return w.Clone(), true
}

// FlavorUsage returns usage for a ClusterQueue/flavor/resource.
func (c *Cache) FlavorUsage(cq, flavor string, resource api.ResourceName) int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.flavorUsageLocked(cq, flavor, resource)
}

func (c *Cache) flavorUsageLocked(cq, flavor string, resource api.ResourceName) int64 {
	if c.usage[cq] == nil {
		return 0
	}
	return c.usage[cq][flavor].Get(resource)
}

// CQFlavorUsage returns a clone of all resource usage for a CQ/flavor.
func (c *Cache) CQFlavorUsage(cq, flavor string) api.ResourceList {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.usage[cq] == nil || c.usage[cq][flavor] == nil {
		return api.ResourceList{}
	}
	return c.usage[cq][flavor].Clone()
}

// CQUsage returns aggregate usage across all flavors of a CQ.
func (c *Cache) CQUsage(cq string) api.ResourceList {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := api.ResourceList{}
	for _, byResource := range c.usage[cq] {
		out.Add(byResource)
	}
	return out
}

// TotalFlavorUsage sums usage for a set of ClusterQueues for one
// flavor/resource.
func (c *Cache) TotalFlavorUsage(cqs []string, flavor string, resource api.ResourceName) int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	var total int64
	for _, cq := range cqs {
		total += c.flavorUsageLocked(cq, flavor, resource)
	}
	return total
}

// FlavorUsageExcluding sums usage for a set of ClusterQueues after subtracting
// the workloads in excluded (keyed by namespace/name).
func (c *Cache) FlavorUsageExcluding(cqs []string, flavor string, resource api.ResourceName, excluded map[string]bool) int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	var total int64
	for _, cq := range cqs {
		for key, w := range c.workloads {
			if excluded[key] || w.Status.Admission.ClusterQueue != cq {
				continue
			}
			for _, fa := range w.Status.Admission.FlavorAssignments {
				if fa.Flavor == flavor {
					total += fa.Resources.Get(resource)
				}
			}
		}
	}
	return total
}

// LendableExcluding computes how much of a flavor/resource other CQs can lend
// *right now* to the target CQ. An absent lendingLimit entry means "no limit".
// This is the feasibility view used by diagnostics (and by borrowing checks):
// a sibling that has already used its whole nominal has nothing to lend at
// this moment, even though its nominal still counts in the fairness denominator.
func (c *Cache) LendableExcluding(cqs []string, target string, flavor string, resource api.ResourceName,
	excluded map[string]bool, nominal func(cq, flavor string, resource api.ResourceName) int64,
	lendingLimit func(cq, flavor string, resource api.ResourceName) (int64, bool)) int64 {

	c.mu.RLock()
	defer c.mu.RUnlock()
	var total int64
	for _, cq := range cqs {
		if cq == target {
			continue
		}
		used := int64(0)
		for key, w := range c.workloads {
			if excluded[key] || w.Status.Admission.ClusterQueue != cq {
				continue
			}
			for _, fa := range w.Status.Admission.FlavorAssignments {
				if fa.Flavor == flavor {
					used += fa.Resources.Get(resource)
				}
			}
		}
		unused := nominal(cq, flavor, resource) - used
		if unused <= 0 {
			continue
		}
		if limit, ok := lendingLimit(cq, flavor, resource); ok && unused > limit {
			unused = limit
		}
		total += unused
	}
	return total
}

// FairShareLendable computes the DRS denominator: the siblings' *nominal*
// quota (capped by lendingLimit), not their momentary free quota. Kueue's
// fair-sharing share must not move merely because a peer is busy right now.
// Use LendableExcluding for "can I borrow now" diagnostics.
func (c *Cache) FairShareLendable(cqs []string, target string, flavor string, resource api.ResourceName,
	excluded map[string]bool, nominal func(cq, flavor string, resource api.ResourceName) int64,
	lendingLimit func(cq, flavor string, resource api.ResourceName) (int64, bool)) int64 {

	c.mu.RLock()
	defer c.mu.RUnlock()
	var total int64
	for _, cq := range cqs {
		if cq == target {
			continue
		}
		lendable := nominal(cq, flavor, resource)
		if lendable <= 0 {
			continue
		}
		if limit, ok := lendingLimit(cq, flavor, resource); ok && lendable > limit {
			lendable = limit
		}
		total += lendable
	}
	return total
}

// TopologyUsage aggregates TAS reservations by domain for one topology/level.
// The key is the domain path joined with "/". Workloads in excluded are
// ignored, which lets the scheduler simulate preemption without mutating the
// cache.
func (c *Cache) TopologyUsage(topology, level string, excluded map[string]bool) map[string]api.ResourceList {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := map[string]api.ResourceList{}
	for key, w := range c.workloads {
		if excluded[key] || w.Status.Admission == nil {
			continue
		}
		for _, ta := range w.Status.Admission.TopologyAssignments {
			if ta.Topology != topology || ta.Level != level {
				continue
			}
			if out[ta.Domain] == nil {
				out[ta.Domain] = api.ResourceList{}
			}
			out[ta.Domain].Add(ta.Resources)
			if ta.PodCount > 0 {
				out[ta.Domain][api.Pods] += int64(ta.PodCount)
			}
		}
	}
	return out
}

// Admitted returns clones of all workloads that currently hold quota.
func (c *Cache) Admitted() []*api.Workload {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make([]*api.Workload, 0, len(c.workloads))
	for _, w := range c.workloads {
		out = append(out, w.Clone())
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Status.Sequence != out[j].Status.Sequence {
			return out[i].Status.Sequence < out[j].Status.Sequence
		}
		return out[i].Key() < out[j].Key()
	})
	return out
}

// Len returns the number of workloads holding quota.
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.workloads)
}

// UsageSnapshot returns the internal usage map as deep copies. It is useful
// for diagnostics and tests.
func (c *Cache) UsageSnapshot() map[cqFlavorKey]api.ResourceList {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := map[cqFlavorKey]api.ResourceList{}
	for cq, flavors := range c.usage {
		for flavor, usage := range flavors {
			out[cqFlavorKey{cq: cq, flavor: flavor}] = usage.Clone()
		}
	}
	return out
}

// Key is exported for tests that inspect UsageSnapshot.
type Key = cqFlavorKey
