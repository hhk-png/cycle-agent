// Package store is the in-memory API server for MiniKueue. Specs are treated
// as immutable after insertion; Workload status is updated by the controller.
package store

import (
	"fmt"
	"sort"
	"sync"

	"example.com/minikueue/internal/api"
)

// Store is concurrency-safe.
type Store struct {
	mu            sync.RWMutex
	clusterQueues map[string]*api.ClusterQueue
	cohorts       map[string]*api.Cohort
	localQueues   map[string]*api.LocalQueue
	workloads     map[string]*api.Workload
	flavors       map[string]*api.ResourceFlavor
	topologies    map[string]*api.Topology
	domains       map[string]map[string]*api.TopologyDomain // topology -> domain name
	checks        map[string]*api.AdmissionCheck
	classes       map[string]*api.WorkloadPriorityClass
	providers     map[string]*api.CapacityProvider
	namespaces    map[string]map[string]string
}

// New creates an empty store.
func New() *Store {
	return &Store{
		clusterQueues: map[string]*api.ClusterQueue{},
		cohorts:       map[string]*api.Cohort{},
		localQueues:   map[string]*api.LocalQueue{},
		workloads:     map[string]*api.Workload{},
		flavors:       map[string]*api.ResourceFlavor{},
		topologies:    map[string]*api.Topology{},
		domains:       map[string]map[string]*api.TopologyDomain{},
		checks:        map[string]*api.AdmissionCheck{},
		classes:       map[string]*api.WorkloadPriorityClass{},
		providers:     map[string]*api.CapacityProvider{},
		namespaces:    map[string]map[string]string{},
	}
}

// LocalQueueKey builds namespace/name.
func LocalQueueKey(namespace, name string) string { return namespace + "/" + name }

// WorkloadKey builds namespace/name.
func WorkloadKey(namespace, name string) string { return namespace + "/" + name }

// UpsertClusterQueue inserts or replaces a ClusterQueue.
func (s *Store) UpsertClusterQueue(cq *api.ClusterQueue) error {
	if err := api.ValidateClusterQueue(cq); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.clusterQueues[cq.Name] = cq.Clone()
	return nil
}

// GetClusterQueue returns a deep copy of a ClusterQueue. Callers may mutate
// the result without affecting the store.
func (s *Store) GetClusterQueue(name string) (*api.ClusterQueue, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	cq, ok := s.clusterQueues[name]
	if !ok {
		return nil, false
	}
	return cq.Clone(), true
}

// ListClusterQueues returns deep copies of ClusterQueues sorted by name.
func (s *Store) ListClusterQueues() []*api.ClusterQueue {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.ClusterQueue, 0, len(s.clusterQueues))
	for _, cq := range s.clusterQueues {
		out = append(out, cq.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// UpsertCohort inserts or replaces a Cohort and validates the parent chain.
func (s *Store) UpsertCohort(c *api.Cohort) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := api.ValidateCohort(c, s.cohorts); err != nil {
		return err
	}
	old, hadOld := s.cohorts[c.Name]
	s.cohorts[c.Name] = c.Clone()
	// Validate existing cohorts again because replacing a cohort can close a
	// previously impossible cycle only in malformed states; this keeps the
	// invariant explicit.
	for _, existing := range s.cohorts {
		if err := api.ValidateCohort(existing, s.cohorts); err != nil {
			if hadOld {
				s.cohorts[c.Name] = old
			} else {
				delete(s.cohorts, c.Name)
			}
			return err
		}
	}
	return nil
}

// GetCohort returns a deep copy of a Cohort.
func (s *Store) GetCohort(name string) (*api.Cohort, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.cohorts[name]
	if !ok {
		return nil, false
	}
	return c.Clone(), true
}

// ListCohorts returns deep copies of all cohorts sorted by name.
func (s *Store) ListCohorts() []*api.Cohort {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.Cohort, 0, len(s.cohorts))
	for _, c := range s.cohorts {
		out = append(out, c.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// UpsertLocalQueue inserts or replaces a LocalQueue.
func (s *Store) UpsertLocalQueue(q *api.LocalQueue) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := api.ValidateLocalQueue(q, s.clusterQueues); err != nil {
		return err
	}
	s.localQueues[LocalQueueKey(q.Namespace, q.Name)] = q.Clone()
	return nil
}

// GetLocalQueue returns a deep copy of a LocalQueue.
func (s *Store) GetLocalQueue(namespace, name string) (*api.LocalQueue, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	q, ok := s.localQueues[LocalQueueKey(namespace, name)]
	if !ok {
		return nil, false
	}
	return q.Clone(), true
}

// LocalQueueToClusterQueue returns an immutable lookup map.
func (s *Store) LocalQueueToClusterQueue() map[string]string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]string, len(s.localQueues))
	for key, q := range s.localQueues {
		out[key] = q.ClusterQueue
	}
	return out
}

// ListLocalQueues returns LocalQueues sorted by key.
func (s *Store) ListLocalQueues() []*api.LocalQueue {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.LocalQueue, 0, len(s.localQueues))
	for _, q := range s.localQueues {
		out = append(out, q.Clone())
	}
	sort.Slice(out, func(i, j int) bool {
		return LocalQueueKey(out[i].Namespace, out[i].Name) < LocalQueueKey(out[j].Namespace, out[j].Name)
	})
	return out
}

// UpsertWorkload inserts or replaces a workload. References are validated
// against the current store so a direct call cannot create a Workload with a
// missing LocalQueue, ClusterQueue, priority class or DRA mapping.
func (s *Store) UpsertWorkload(w *api.Workload) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.validateWorkloadRefsLocked(w); err != nil {
		return err
	}
	s.workloads[WorkloadKey(w.Namespace, w.Name)] = w.Clone()
	return nil
}

// UpdateWorkload replaces the status of an existing workload. UID and Spec are
// immutable after creation: they are preserved from the stored object even if
// the caller passes a modified copy. Use UpsertWorkload for a new incarnation.
func (s *Store) UpdateWorkload(w *api.Workload) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := WorkloadKey(w.Namespace, w.Name)
	existing, ok := s.workloads[key]
	if !ok {
		return fmt.Errorf("workload %s does not exist", key)
	}
	updated := w.Clone()
	updated.UID = existing.UID
	updated.Spec = existing.Clone().Spec
	// spec.active and spec.admissionGates are the mutable lifecycle fields;
	// everything else in spec (pod sets, queue, priority) is fixed for this
	// incarnation.
	updated.Spec.Active = w.Spec.Active
	updated.Spec.AdmissionGates = append([]string(nil), w.Spec.AdmissionGates...)
	if err := s.validateWorkloadRefsLocked(updated); err != nil {
		return err
	}
	s.workloads[key] = updated
	return nil
}

func (s *Store) validateWorkloadRefsLocked(w *api.Workload) error {
	localQueues := make(map[string]*api.LocalQueue, len(s.localQueues))
	for key, q := range s.localQueues {
		localQueues[key] = q
	}
	return api.ValidateWorkloadRefs(w, localQueues, s.clusterQueues, s.classes)
}

// GetWorkload returns a clone of a workload.
func (s *Store) GetWorkload(namespace, name string) (*api.Workload, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	w, ok := s.workloads[WorkloadKey(namespace, name)]
	if !ok {
		return nil, false
	}
	return w.Clone(), true
}

// ListWorkloads returns clones of all workloads sorted by creation timestamp,
// then key.
func (s *Store) ListWorkloads() []*api.Workload {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.Workload, 0, len(s.workloads))
	for _, w := range s.workloads {
		out = append(out, w.Clone())
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Spec.CreationTimestamp != out[j].Spec.CreationTimestamp {
			return out[i].Spec.CreationTimestamp < out[j].Spec.CreationTimestamp
		}
		return out[i].Key() < out[j].Key()
	})
	return out
}

// DeleteWorkload removes a workload.
func (s *Store) DeleteWorkload(namespace, name string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.workloads, WorkloadKey(namespace, name))
}

// UpsertResourceFlavor inserts or replaces a ResourceFlavor.
func (s *Store) UpsertResourceFlavor(f *api.ResourceFlavor) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.flavors[f.Name] = f.Clone()
}

// ListResourceFlavors returns deep copies of flavors sorted by name.
func (s *Store) ListResourceFlavors() []*api.ResourceFlavor {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.ResourceFlavor, 0, len(s.flavors))
	for _, f := range s.flavors {
		out = append(out, f.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// UpsertTopology inserts or replaces a Topology.
func (s *Store) UpsertTopology(t *api.Topology) error {
	if err := api.ValidateTopology(t); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	old, hadOld := s.topologies[t.Name]
	s.topologies[t.Name] = t.Clone()
	// A changed level list can invalidate existing leaf domains. Reject the
	// update instead of silently deleting physical capacity.
	for _, d := range s.domains[t.Name] {
		if err := api.ValidateTopologyDomain(d, s.topologies); err != nil {
			if hadOld {
				s.topologies[t.Name] = old
			} else {
				delete(s.topologies, t.Name)
			}
			return fmt.Errorf("topology %q update invalidates domain %q: %w", t.Name, d.Name, err)
		}
	}
	return nil
}

// GetTopology returns a deep copy of a Topology.
func (s *Store) GetTopology(name string) (*api.Topology, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	t, ok := s.topologies[name]
	if !ok {
		return nil, false
	}
	return t.Clone(), true
}

// ListTopologies returns deep copies of all topologies sorted by name.
func (s *Store) ListTopologies() []*api.Topology {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.Topology, 0, len(s.topologies))
	for _, t := range s.topologies {
		out = append(out, t.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// UpsertTopologyDomain inserts or replaces a leaf domain.
func (s *Store) UpsertTopologyDomain(d *api.TopologyDomain) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := api.ValidateTopologyDomain(d, s.topologies); err != nil {
		return err
	}
	if s.domains[d.Topology] == nil {
		s.domains[d.Topology] = map[string]*api.TopologyDomain{}
	}
	s.domains[d.Topology][d.Name] = d.Clone()
	return nil
}

// ListTopologyDomains returns deep copies of domains for a topology sorted by
// name.
func (s *Store) ListTopologyDomains(topology string) []*api.TopologyDomain {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.TopologyDomain, 0, len(s.domains[topology]))
	for _, d := range s.domains[topology] {
		out = append(out, d.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// AllTopologyDomains returns every domain grouped by topology.
func (s *Store) AllTopologyDomains() map[string][]*api.TopologyDomain {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string][]*api.TopologyDomain, len(s.domains))
	for topology, domains := range s.domains {
		list := make([]*api.TopologyDomain, 0, len(domains))
		for _, d := range domains {
			list = append(list, d.Clone())
		}
		sort.Slice(list, func(i, j int) bool { return list[i].Name < list[j].Name })
		out[topology] = list
	}
	return out
}

// UpsertCapacityProvider inserts or replaces a dynamic capacity provider.
func (s *Store) UpsertCapacityProvider(p *api.CapacityProvider) error {
	if err := api.ValidateCapacityProvider(p); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.providers[p.Name] = p.Clone()
	return nil
}

// GetCapacityProvider returns a deep copy of a provider.
func (s *Store) GetCapacityProvider(name string) (*api.CapacityProvider, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p, ok := s.providers[name]
	if !ok {
		return nil, false
	}
	return p.Clone(), true
}

// ListCapacityProviders returns deep copies of providers sorted by name.
func (s *Store) ListCapacityProviders() []*api.CapacityProvider {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.CapacityProvider, 0, len(s.providers))
	for _, p := range s.providers {
		out = append(out, p.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// UpsertAdmissionCheck inserts or replaces an AdmissionCheck.
func (s *Store) UpsertAdmissionCheck(c *api.AdmissionCheck) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.checks[c.Name] = c.Clone()
}

// GetAdmissionCheck returns a deep copy of an AdmissionCheck.
func (s *Store) GetAdmissionCheck(name string) (*api.AdmissionCheck, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.checks[name]
	if !ok {
		return nil, false
	}
	return c.Clone(), true
}

// ListAdmissionChecks returns deep copies of checks sorted by name.
func (s *Store) ListAdmissionChecks() []*api.AdmissionCheck {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*api.AdmissionCheck, 0, len(s.checks))
	for _, c := range s.checks {
		out = append(out, c.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// UpsertPriorityClass inserts or replaces a WorkloadPriorityClass.
func (s *Store) UpsertPriorityClass(pc *api.WorkloadPriorityClass) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.classes[pc.Name] = pc.Clone()
}

// PriorityClasses returns a deep-copied priority class map.
func (s *Store) PriorityClasses() map[string]*api.WorkloadPriorityClass {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]*api.WorkloadPriorityClass, len(s.classes))
	for k, v := range s.classes {
		out[k] = v.Clone()
	}
	return out
}

// UpsertNamespace registers namespace labels for selector evaluation.
func (s *Store) UpsertNamespace(name string, labels map[string]string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.namespaces[name] = labels
}

// NamespaceLabels returns a copy of namespace labels.
func (s *Store) NamespaceLabels(name string) map[string]string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	labels := s.namespaces[name]
	out := make(map[string]string, len(labels))
	for k, v := range labels {
		out[k] = v
	}
	return out
}

// ValidateReferences checks cross-object references.
func (s *Store) ValidateReferences() error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, q := range s.localQueues {
		if _, ok := s.clusterQueues[q.ClusterQueue]; !ok {
			return fmt.Errorf("local queue %s/%s references missing cluster queue %q", q.Namespace, q.Name, q.ClusterQueue)
		}
	}
	for _, cq := range s.clusterQueues {
		// A cohortName without a matching Cohort object is an implicit, flat
		// cohort in Kueue, so it is not an error.
		for _, check := range cq.Spec.AdmissionChecks {
			if _, ok := s.checks[check]; !ok {
				return fmt.Errorf("cluster queue %q references missing admission check %q", cq.Name, check)
			}
		}
		if cq.Spec.CapacityProvider != "" {
			if _, ok := s.providers[cq.Spec.CapacityProvider]; !ok {
				return fmt.Errorf("cluster queue %q references missing capacity provider %q", cq.Name, cq.Spec.CapacityProvider)
			}
		}
	}
	for _, f := range s.flavors {
		if f.TopologyName != "" {
			if _, ok := s.topologies[f.TopologyName]; !ok {
				return fmt.Errorf("resource flavor %q references missing topology %q", f.Name, f.TopologyName)
			}
		}
	}
	for topology, domains := range s.domains {
		if _, ok := s.topologies[topology]; !ok {
			return fmt.Errorf("topology domains exist for missing topology %q", topology)
		}
		for _, d := range domains {
			if err := api.ValidateTopologyDomain(d, s.topologies); err != nil {
				return err
			}
		}
	}
	for _, w := range s.workloads {
		if err := s.validateWorkloadRefsLocked(w); err != nil {
			return fmt.Errorf("workload %s: %w", w.Key(), err)
		}
	}
	return nil
}
