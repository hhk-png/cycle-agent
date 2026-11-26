// Package cache holds the scheduler's cluster snapshot. Every session works on
// a deep copy of this cache, so actions can stage changes and either commit or
// discard them atomically.
package cache

import (
	"fmt"
	"sort"
	"strings"

	"example.com/minivolcano/internal/api"
)

type NodeState struct {
	Node *api.Node
	Pods map[string]*api.Pod // pods currently occupying the node
}

type Cache struct {
	Now             int64
	Nodes           map[string]*NodeState
	Pods            map[string]*api.Pod
	Jobs            map[string]*api.Job
	PodGroups       map[string]*api.PodGroup
	Queues          map[string]*api.Queue
	Reservations    map[string]*api.Reservation
	HyperNodes      map[string]*api.HyperNode
	PriorityClasses map[string]int32
	PDBs            map[string]*api.PodDisruptionBudget
	ResourceQuotas  map[string]*api.ResourceQuota
}

func New() *Cache {
	return &Cache{
		Nodes:           map[string]*NodeState{},
		Pods:            map[string]*api.Pod{},
		Jobs:            map[string]*api.Job{},
		PodGroups:       map[string]*api.PodGroup{},
		Queues:          map[string]*api.Queue{},
		Reservations:    map[string]*api.Reservation{},
		HyperNodes:      map[string]*api.HyperNode{},
		PriorityClasses: map[string]int32{},
		PDBs:            map[string]*api.PodDisruptionBudget{},
		ResourceQuotas:  map[string]*api.ResourceQuota{},
	}
}

func (c *Cache) Clone() *Cache {
	out := New()
	out.Now = c.Now
	// Clone pods first, then rebuild node.Pods with pointers into out.Pods.
	// Keeping a single pod object per UID avoids the two maps drifting apart
	// after a phase transition.
	for k, v := range c.Pods {
		out.Pods[k] = v.Clone()
	}
	for k, v := range c.Nodes {
		node := &NodeState{Node: v.Node.Clone(), Pods: make(map[string]*api.Pod, len(v.Pods))}
		for pk := range v.Pods {
			if p, ok := out.Pods[pk]; ok {
				node.Pods[pk] = p
			}
		}
		out.Nodes[k] = node
	}
	for k, v := range c.Jobs {
		out.Jobs[k] = v.Clone()
	}
	for k, v := range c.PodGroups {
		out.PodGroups[k] = v.Clone()
	}
	for k, v := range c.Queues {
		out.Queues[k] = v.Clone()
	}
	for k, v := range c.Reservations {
		out.Reservations[k] = v.Clone()
	}
	for k, v := range c.HyperNodes {
		out.HyperNodes[k] = v.Clone()
	}
	for k, v := range c.PriorityClasses {
		out.PriorityClasses[k] = v
	}
	for k, v := range c.PDBs {
		out.PDBs[k] = v.Clone()
	}
	for k, v := range c.ResourceQuotas {
		out.ResourceQuotas[k] = v.Clone()
	}
	return out
}

func (c *Cache) AddNode(n *api.Node) error {
	if n == nil || n.Name == "" {
		return fmt.Errorf("node name must not be empty")
	}
	if _, ok := c.Nodes[n.Name]; ok {
		return fmt.Errorf("node %q already exists", n.Name)
	}
	node := n.Clone()
	if node.Allocatable == nil {
		node.Allocatable = api.Resource{}
	}
	if node.Allocated == nil {
		node.Allocated = api.Resource{}
	}
	c.Nodes[node.Name] = &NodeState{Node: node, Pods: map[string]*api.Pod{}}
	return nil
}

func (c *Cache) AddQueue(q *api.Queue) error {
	if err := api.ValidateQueue(q); err != nil {
		return err
	}
	q.Normalize()
	if _, ok := c.Queues[q.Name]; ok {
		return fmt.Errorf("queue %q already exists", q.Name)
	}
	c.Queues[q.Name] = q.Clone()
	return nil
}

func (c *Cache) AddHyperNode(h *api.HyperNode) error {
	if h == nil || h.Name == "" {
		return fmt.Errorf("hypernode name must not be empty")
	}
	if h.Tier < 0 {
		return fmt.Errorf("hypernode %q: tier must be >= 0", h.Name)
	}
	if _, ok := c.HyperNodes[h.Name]; ok {
		return fmt.Errorf("hypernode %q already exists", h.Name)
	}
	if parent, ok := c.HyperNodes[h.Parent]; ok && parent.Tier <= h.Tier {
		return fmt.Errorf("hypernode %q: tier %d must be greater than parent %q tier %d",
			h.Name, h.Tier, parent.Name, parent.Tier)
	}
	for _, child := range h.Children {
		if c, ok := c.HyperNodes[child]; ok && c.Tier >= h.Tier {
			return fmt.Errorf("hypernode %q: tier %d must be less than child %q tier %d",
				h.Name, h.Tier, child, c.Tier)
		}
	}
	for p := h.Parent; p != ""; {
		if p == h.Name {
			return fmt.Errorf("hypernode %q: parent chain contains a cycle", h.Name)
		}
		parent, ok := c.HyperNodes[p]
		if !ok {
			break
		}
		p = parent.Parent
	}
	c.HyperNodes[h.Name] = h.Clone()
	return nil
}

// ValidateTopology checks cross-object references that AddQueue/AddHyperNode
// cannot validate in isolation. It must run before a scheduling session.
func (c *Cache) ValidateTopology() error {
	for _, q := range c.Queues {
		parent := q.Spec.Parent
		if parent == "" || parent == "root" {
			continue
		}
		seen := map[string]bool{q.Name: true}
		for parent != "" && parent != "root" {
			if seen[parent] {
				return fmt.Errorf("queue %q: parent chain contains a cycle", q.Name)
			}
			seen[parent] = true
			p, ok := c.Queues[parent]
			if !ok {
				return fmt.Errorf("queue %q: parent %q not found", q.Name, parent)
			}
			parent = p.Spec.Parent
		}
	}
	for _, h := range c.HyperNodes {
		if h.Parent != "" {
			if _, ok := c.HyperNodes[h.Parent]; !ok {
				return fmt.Errorf("hypernode %q: parent %q not found", h.Name, h.Parent)
			}
		}
		for _, child := range h.Children {
			ch, ok := c.HyperNodes[child]
			if !ok {
				return fmt.Errorf("hypernode %q: child %q not found", h.Name, child)
			}
			if ch.Tier >= h.Tier {
				return fmt.Errorf("hypernode %q: tier %d must be less than child %q tier %d",
					h.Name, h.Tier, child, ch.Tier)
			}
		}
	}
	return nil
}

func (c *Cache) AddReservation(r *api.Reservation) error {
	if err := api.ValidateReservation(r); err != nil {
		return err
	}
	if len(r.NodeNames) == 0 {
		return fmt.Errorf("reservation %q: at least one node is required", r.Name)
	}
	for _, n := range r.NodeNames {
		if _, ok := c.Nodes[n]; !ok {
			return fmt.Errorf("reservation %q: node %q not found", r.Name, n)
		}
	}
	if _, ok := c.Queues[r.Queue]; !ok {
		return fmt.Errorf("reservation %q: queue %q not found", r.Name, r.Queue)
	}
	c.Reservations[r.Name] = r.Clone()
	return nil
}

func (c *Cache) AddPDB(p *api.PodDisruptionBudget) error {
	if p == nil || p.Name == "" || p.Namespace == "" {
		return fmt.Errorf("pdb name and namespace must not be empty")
	}
	if !p.HasMin && !p.HasMax {
		return fmt.Errorf("pdb %s: minAvailable or maxUnavailable is required", p.Key())
	}
	if p.HasMin && p.HasMax {
		return fmt.Errorf("pdb %s: minAvailable and maxUnavailable are mutually exclusive", p.Key())
	}
	if p.HasMin && p.MinAvailable < 0 {
		return fmt.Errorf("pdb %s: minAvailable must be >= 0", p.Key())
	}
	if p.HasMax && p.MaxUnavailable < 0 {
		return fmt.Errorf("pdb %s: maxUnavailable must be >= 0", p.Key())
	}
	if _, ok := c.PDBs[p.Key()]; ok {
		return fmt.Errorf("pdb %q already exists", p.Key())
	}
	c.PDBs[p.Key()] = p.Clone()
	return nil
}

func (c *Cache) AddResourceQuota(q *api.ResourceQuota) error {
	if q == nil || q.Name == "" || q.Namespace == "" {
		return fmt.Errorf("resourcequota name and namespace must not be empty")
	}
	if q.Hard.IsZero() {
		return fmt.Errorf("resourcequota %s: hard limits must not be empty", q.Key())
	}
	for k, v := range q.Hard {
		if v < 0 {
			return fmt.Errorf("resourcequota %s: %s must be >= 0", q.Key(), k)
		}
	}
	if _, ok := c.ResourceQuotas[q.Key()]; ok {
		return fmt.Errorf("resourcequota %q already exists", q.Key())
	}
	stored := q.Clone()
	// Kubernetes quotas commonly use requests.cpu / requests.memory /
	// requests.nvidia.com/gpu. The scheduler's Resource keys are unprefixed,
	// so normalise on ingest instead of forcing every caller to remember the
	// difference.
	normalized := make(api.Resource, len(stored.Hard))
	for k, v := range stored.Hard {
		normalized[strings.TrimPrefix(k, "requests.")] = v
	}
	stored.Hard = normalized
	c.ResourceQuotas[q.Key()] = stored
	return nil
}

// SubmitJob performs the mutation normally done by the admission webhook and
// job controller, then materialises one Pod per task replica.
func (c *Cache) AddPriorityClass(name string, value int32) {
	c.PriorityClasses[name] = value
}

func (c *Cache) SubmitJob(j *api.Job) error {
	if j == nil {
		return fmt.Errorf("job is nil")
	}
	if j.Spec.PriorityClassName != "" {
		v, ok := c.PriorityClasses[j.Spec.PriorityClassName]
		if !ok {
			return fmt.Errorf("job %s: priorityClassName %q not found", j.Key(), j.Spec.PriorityClassName)
		}
		j.Spec.Priority = v
	}
	api.MutateJob(j)
	if err := api.ValidateJob(j); err != nil {
		return err
	}
	if _, ok := c.Jobs[j.UID]; ok {
		return fmt.Errorf("job %s already exists", j.Key())
	}
	if _, ok := c.Queues[j.Spec.Queue]; !ok {
		return fmt.Errorf("job %s: queue %q not found", j.Key(), j.Spec.Queue)
	}
	job := j.Clone()
	job.CreationTick = c.Now
	job.Status.Phase = api.JobPending
	job.PodGroupName = job.Name
	c.Jobs[job.UID] = job

	pg := &api.PodGroup{
		UID:           job.UID,
		Name:          job.PodGroupName,
		Namespace:     job.Namespace,
		Queue:         job.Spec.Queue,
		MinMember:     job.Spec.MinAvailable,
		MinTaskMember: job.MinTaskMember(),
		MinResources:  job.MinResources(),
		Priority:      job.Spec.Priority,
		Phase:         api.PodGroupPending,
		CreationTick:  c.Now,
		// Volcano derives the PodGroup from the Job; propagating labels makes
		// PodGroup-level selectors (including topology anti-affinity) usable.
		Labels: cloneMap(job.Spec.Labels),
	}
	c.PodGroups[job.UID] = pg

	return c.MaterializeJob(job)
}

// MaterializeJob creates any missing task pods. It is idempotent so the job
// controller can call it on every reconcile.
func (c *Cache) MaterializeJob(job *api.Job) error {
	for ti := range job.Spec.Tasks {
		t := &job.Spec.Tasks[ti]
		for i := int32(0); i < t.Replicas; i++ {
			name := fmt.Sprintf("%s-%s-%d", job.Name, t.Name, i)
			key := job.Namespace + "/" + name
			if _, ok := c.Pods[key]; ok {
				continue
			}
			req := t.Requests.Clone()
			labels := cloneMap(job.Spec.Labels)
			if labels == nil {
				labels = map[string]string{}
			}
			for k, v := range t.Labels {
				labels[k] = v
			}
			c.Pods[key] = &api.Pod{
				UID:          key,
				Name:         name,
				Namespace:    job.Namespace,
				Job:          job.UID,
				JobName:      job.Name,
				Task:         t.Name,
				TaskIndex:    i,
				Queue:        job.Spec.Queue,
				Priority:     job.Spec.Priority,
				Requests:     req,
				Labels:       labels,
				NodeSelector: cloneMap(t.NodeSelector),
				Tolerations:  append([]api.Toleration(nil), t.Tolerations...),
				Phase:        api.PodPending,
				CreationTick: c.Now,
				NUMANode:     -1,
			}
		}
	}
	return nil
}

// Bind assigns a pod to a node and reserves its resources. Predicates are the
// caller's responsibility; Bind enforces the final capacity invariant.
func (c *Cache) Bind(podUID, nodeName string, tick int64) error {
	p, ok := c.Pods[podUID]
	if !ok {
		return fmt.Errorf("pod %q not found", podUID)
	}
	if p.Phase != api.PodPending {
		return fmt.Errorf("pod %s is %s, not Pending", p.Name, p.Phase)
	}
	ns, ok := c.Nodes[nodeName]
	if !ok {
		return fmt.Errorf("node %q not found", nodeName)
	}
	if ns.Node.Unschedulable {
		return fmt.Errorf("node %q is unschedulable", nodeName)
	}
	if !p.Requests.LessEqual(ns.Node.Allocatable.Sub(ns.Node.Allocated)) {
		return fmt.Errorf("node %q has insufficient idle resources for pod %s (need %s, idle %s)",
			nodeName, p.Name, p.Requests, ns.Node.Allocatable.Sub(ns.Node.Allocated))
	}
	numaID := -1
	if len(ns.Node.NUMANodes) > 0 {
		found := false
		for i := range ns.Node.NUMANodes {
			free := ns.Node.NUMANodes[i].Allocatable.Sub(ns.Node.NUMANodes[i].Allocated)
			if p.Requests.WithinLimits(free) {
				numaID = ns.Node.NUMANodes[i].ID
				ns.Node.NUMANodes[i].Allocated = ns.Node.NUMANodes[i].Allocated.Add(p.Requests)
				found = true
				break
			}
		}
		if !found {
			return fmt.Errorf("pod %s cannot fit in any NUMA node of %q", p.Name, nodeName)
		}
	}
	p.NodeName = nodeName
	p.Phase = api.PodAllocated
	p.ScheduleTick = tick
	p.NUMANode = numaID
	p.Evicted = false
	p.Message = ""
	ns.Node.Allocated = ns.Node.Allocated.Add(p.Requests)
	ns.Pods[p.UID] = p
	return nil
}

// Evict releases a pod's resources and returns it to Pending. The pod object is
// kept so the job controller can apply lifecycle policies and reschedule it.
func (c *Cache) Evict(podUID string, tick int64) error {
	p, ok := c.Pods[podUID]
	if !ok {
		return fmt.Errorf("pod %q not found", podUID)
	}
	if !p.Phase.OccupiesNode() {
		return fmt.Errorf("pod %s is %s and does not occupy a node", p.Name, p.Phase)
	}
	c.releaseNodeResources(p)
	p.Phase = api.PodPending
	p.NodeName = ""
	p.ScheduleTick = 0
	p.StartTick = 0
	p.NodeName = ""
	p.NUMANode = -1
	p.Evicted = true
	p.EvictedTick = tick
	p.PolicyHandled = false
	p.Message = "evicted"
	return nil
}

// Finish marks a running or allocated pod terminal and releases its resources.
func (c *Cache) Finish(podUID string, success bool, tick int64) error {
	p, ok := c.Pods[podUID]
	if !ok {
		return fmt.Errorf("pod %q not found", podUID)
	}
	if !p.Phase.OccupiesNode() {
		return fmt.Errorf("pod %s is %s and cannot finish", p.Name, p.Phase)
	}
	c.releaseNodeResources(p)
	p.Phase = api.PodSucceeded
	if !success {
		p.Phase = api.PodFailed
	}
	p.FinishTick = tick
	// Keep NodeName/NUMANode for placement history. Kubernetes pods retain
	// spec.nodeName after completion; only the resource accounting is released.
	p.PolicyHandled = false
	return nil
}

// ResetForRestart returns a pod to Pending without recording a failure. It is
// used by the RestartJob lifecycle action.
func (c *Cache) ResetForRestart(podUID string, tick int64) error {
	p, ok := c.Pods[podUID]
	if !ok {
		return fmt.Errorf("pod %q not found", podUID)
	}
	if p.Phase.OccupiesNode() {
		c.releaseNodeResources(p)
	}
	p.Phase = api.PodPending
	p.NodeName = ""
	p.ScheduleTick = 0
	p.StartTick = 0
	p.FinishTick = 0
	p.NUMANode = -1
	p.Evicted = false
	p.EvictedTick = 0
	p.PolicyHandled = false
	p.Restarts++
	p.Message = ""
	return nil
}

func (c *Cache) releaseNodeResources(p *api.Pod) {
	if p.NodeName == "" {
		return
	}
	ns, ok := c.Nodes[p.NodeName]
	if !ok {
		return
	}
	ns.Node.Allocated = ns.Node.Allocated.Sub(p.Requests)
	for k := range ns.Node.Allocated {
		if ns.Node.Allocated[k] <= 0 {
			delete(ns.Node.Allocated, k)
		}
	}
	if p.NUMANode >= 0 {
		for i := range ns.Node.NUMANodes {
			if ns.Node.NUMANodes[i].ID == p.NUMANode {
				ns.Node.NUMANodes[i].Allocated = ns.Node.NUMANodes[i].Allocated.Sub(p.Requests)
				for k := range ns.Node.NUMANodes[i].Allocated {
					if ns.Node.NUMANodes[i].Allocated[k] <= 0 {
						delete(ns.Node.NUMANodes[i].Allocated, k)
					}
				}
			}
		}
	}
	delete(ns.Pods, p.UID)
}

// IdleFor returns node idle resources after subtracting reservations that do
// not belong to the requesting queue/job at the given tick.
func (c *Cache) IdleFor(nodeName, queue, jobUID string, tick int64) api.Resource {
	ns, ok := c.Nodes[nodeName]
	if !ok {
		return api.Resource{}
	}
	idle := ns.Node.Allocatable.Sub(ns.Node.Allocated)
	for _, r := range c.Reservations {
		if !r.ActiveAt(tick) {
			continue
		}
		if !contains(r.NodeNames, nodeName) {
			continue
		}
		if r.Queue == queue && (r.Job == "" || r.Job == jobUID) {
			continue
		}
		// Only the *unused* part of a reservation is unavailable to other
		// tenants. If the owner already consumed 3 of 4 reserved GPUs, a guest
		// should lose 1 GPU of headroom, not all 4.
		ownerUsed := api.Resource{}
		for _, p := range ns.Pods {
			if !p.Phase.OccupiesNode() {
				continue
			}
			if (r.Job != "" && p.Job == r.Job) || (r.Job == "" && p.Queue == r.Queue) {
				ownerUsed = ownerUsed.Add(p.Requests)
			}
		}
		remaining := r.Resources.Sub(ownerUsed)
		for k, v := range remaining {
			if v < 0 {
				remaining[k] = 0
			}
		}
		idle = idle.Sub(remaining)
	}
	return idle
}

func cloneMap(in map[string]string) map[string]string {
	if in == nil {
		return nil
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func (c *Cache) Fits(nodeName, queue, jobUID string, req api.Resource, tick int64) bool {
	return req.LessEqual(c.IdleFor(nodeName, queue, jobUID, tick))
}

// QueueAllocated sums the requests of all pods in a queue that still occupy
// node resources.
func (c *Cache) QueueAllocated(queue string) api.Resource {
	out := api.Resource{}
	for _, p := range c.Pods {
		if p.Queue == queue && p.Phase.OccupiesNode() {
			out = out.Add(p.Requests)
		}
	}
	return out
}

func (c *Cache) JobAllocated(jobUID string) api.Resource {
	out := api.Resource{}
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase.OccupiesNode() {
			out = out.Add(p.Requests)
		}
	}
	return out
}

// NamespaceAdmittedUsage sums the requests of every pod belonging to a job
// that has already been admitted (Inqueue/Running/Restarting/...). Pending
// jobs are excluded: in MiniVolcano, quota is enforced when a job is enqueued,
// not when its pods are materialised. This keeps admission deterministic and
// independent of submission order for jobs that never leave Pending.
func (c *Cache) NamespaceAdmittedUsage(namespace string) api.Resource {
	out := api.Resource{}
	for _, p := range c.Pods {
		if p.Namespace != namespace {
			continue
		}
		job := c.Jobs[p.Job]
		if job == nil || job.Status.Phase == api.JobPending || job.Status.Phase.Terminal() {
			continue
		}
		if p.Phase == api.PodSucceeded || p.Phase == api.PodFailed {
			continue
		}
		out = out.Add(p.Requests)
	}
	return out
}

// QueueNodeGroupAllocated sums the resources a queue currently occupies on
// nodes belonging to a specific node group.
func (c *Cache) QueueNodeGroupAllocated(queue, group string) api.Resource {
	out := api.Resource{}
	for _, p := range c.Pods {
		if p.Queue != queue || !p.Phase.OccupiesNode() {
			continue
		}
		ns, ok := c.Nodes[p.NodeName]
		if !ok || ns.Node.Labels[api.NodeGroupLabel] != group {
			continue
		}
		out = out.Add(p.Requests)
	}
	return out
}

func (c *Cache) TotalAllocatable() api.Resource {
	out := api.Resource{}
	for _, n := range c.Nodes {
		out = out.Add(n.Node.Allocatable)
	}
	return out
}

func (c *Cache) TotalAllocated() api.Resource {
	out := api.Resource{}
	for _, n := range c.Nodes {
		out = out.Add(n.Node.Allocated)
	}
	return out
}

func (c *Cache) TotalIdle() api.Resource {
	return c.TotalAllocatable().Sub(c.TotalAllocated())
}

// JobPods returns pods of a job in deterministic task/index order.
func (c *Cache) JobPods(jobUID string) []*api.Pod {
	var pods []*api.Pod
	for _, p := range c.Pods {
		if p.Job == jobUID {
			pods = append(pods, p)
		}
	}
	sort.SliceStable(pods, func(i, j int) bool {
		if pods[i].Task != pods[j].Task {
			return pods[i].Task < pods[j].Task
		}
		if pods[i].TaskIndex != pods[j].TaskIndex {
			return pods[i].TaskIndex < pods[j].TaskIndex
		}
		return pods[i].Name < pods[j].Name
	})
	return pods
}

func (c *Cache) PendingPods(jobUID string) []*api.Pod {
	var out []*api.Pod
	for _, p := range c.JobPods(jobUID) {
		if p.Phase == api.PodPending {
			out = append(out, p)
		}
	}
	return out
}

func (c *Cache) ReadyCount(jobUID string) int {
	n := 0
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase.ReadyForGang() {
			n++
		}
	}
	return n
}

// ReadyCountByTask counts gang-ready Pods per role. It backs the task-level
// minAvailable gate, which is stricter than the job-level count: 5 ready Pods
// are not enough if the master role has none.
func (c *Cache) ReadyCountByTask(jobUID string) map[string]int {
	out := map[string]int{}
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase.ReadyForGang() {
			out[p.Task]++
		}
	}
	return out
}

func (c *Cache) RunningCount(jobUID string) int {
	n := 0
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase == api.PodRunning {
			n++
		}
	}
	return n
}

func (c *Cache) RunningCountByTask(jobUID string) map[string]int {
	out := map[string]int{}
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase == api.PodRunning {
			out[p.Task]++
		}
	}
	return out
}

// OccupyingCountByTask counts Pods that still hold node resources. It is the
// victim-side analogue of ReadyCountByTask: a Succeeded replica must not
// protect a role gate when deciding whether an eviction is safe.
func (c *Cache) OccupyingCountByTask(jobUID string) map[string]int {
	out := map[string]int{}
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase.OccupiesNode() {
			out[p.Task]++
		}
	}
	return out
}

func (c *Cache) SucceededCount(jobUID string) int {
	n := 0
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase == api.PodSucceeded {
			n++
		}
	}
	return n
}

func (c *Cache) FailedCount(jobUID string) int {
	n := 0
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase == api.PodFailed {
			n++
		}
	}
	return n
}

func (c *Cache) PendingCount(jobUID string) int {
	n := 0
	for _, p := range c.Pods {
		if p.Job == jobUID && p.Phase == api.PodPending {
			n++
		}
	}
	return n
}

func (c *Cache) JobsSorted() []*api.Job {
	out := make([]*api.Job, 0, len(c.Jobs))
	for _, j := range c.Jobs {
		out = append(out, j)
	}
	api.SortJobs(out)
	return out
}

func (c *Cache) QueuesSorted() []*api.Queue {
	out := make([]*api.Queue, 0, len(c.Queues))
	for _, q := range c.Queues {
		out = append(out, q)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func (c *Cache) NodesSorted() []*NodeState {
	out := make([]*NodeState, 0, len(c.Nodes))
	for _, n := range c.Nodes {
		out = append(out, n)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Node.Name < out[j].Node.Name })
	return out
}

// ---------------------------------------------------------------------------
// HyperNode topology helpers
// ---------------------------------------------------------------------------

// HyperNodeMembers expands a HyperNode into its leaf node names.
func (c *Cache) HyperNodeMembers(name string) []string {
	return c.hyperNodeMembers(name, map[string]bool{})
}

func (c *Cache) hyperNodeMembers(name string, seen map[string]bool) []string {
	if seen[name] {
		return nil
	}
	seen[name] = true
	h, ok := c.HyperNodes[name]
	if !ok {
		return nil
	}
	if len(h.Members) > 0 {
		return append([]string(nil), h.Members...)
	}
	var out []string
	for _, child := range h.Children {
		out = append(out, c.hyperNodeMembers(child, seen)...)
	}
	sort.Strings(out)
	return out
}

// HyperNodeOfNode returns the leaf HyperNode containing a node.
func (c *Cache) HyperNodeOfNode(nodeName string) string {
	for _, h := range c.HyperNodes {
		if contains(h.Members, nodeName) {
			return h.Name
		}
	}
	return ""
}

// HyperNodeAncestors returns the leaf and all parents, closest first.
func (c *Cache) HyperNodeAncestors(name string) []string {
	var out []string
	seen := map[string]bool{}
	for name != "" && !seen[name] {
		seen[name] = true
		out = append(out, name)
		h, ok := c.HyperNodes[name]
		if !ok {
			break
		}
		name = h.Parent
	}
	return out
}

// HyperNodesAtTier returns HyperNodes at exactly the given tier, sorted by name.
func (c *Cache) HyperNodesAtTier(tier int) []*api.HyperNode {
	var out []*api.HyperNode
	for _, h := range c.HyperNodes {
		if h.Tier == tier {
			out = append(out, h)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// DomainOfNodeAtTier returns the ancestor HyperNode at a tier, or "".
func (c *Cache) DomainOfNodeAtTier(nodeName string, tier int) string {
	leaf := c.HyperNodeOfNode(nodeName)
	for _, name := range c.HyperNodeAncestors(leaf) {
		if h, ok := c.HyperNodes[name]; ok && h.Tier == tier {
			return name
		}
	}
	return ""
}
