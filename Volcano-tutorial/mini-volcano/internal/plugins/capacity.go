package plugins

import (
	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// Capacity implements hierarchical queue capacity. It is Volcano's answer to
// multi-tenant quota trees: a parent's capacity is shared by children according
// to weight, while every queue keeps an optional hard capability and a
// reclaimable guarantee.
//
// In Volcano the capacity and proportion plugins are mutually exclusive; the
// scheduler config validation enforces that rule.
type Capacity struct {
	enableHierarchy   bool
	enablePreemptable bool
}

func newCapacity(args map[string]string) (framework.Plugin, error) {
	h, err := boolArg(args, "enableHierarchy", false)
	if err != nil {
		return nil, err
	}
	pre, err := boolArg(args, "enablePreemptable", true)
	if err != nil {
		return nil, err
	}
	return &Capacity{enableHierarchy: h, enablePreemptable: pre}, nil
}

func (c *Capacity) Name() string { return "capacity" }

func parentOf(q *api.Queue) string {
	if q.Spec.Parent == "" {
		return "root"
	}
	return q.Spec.Parent
}

func (c *Capacity) OnSessionOpen(ssn *framework.Session) {
	queues := ssn.Queues()
	total := ssn.TotalResource
	cache := map[string]api.Resource{}
	visiting := map[string]bool{}

	var compute func(q *api.Queue) api.Resource
	compute = func(q *api.Queue) api.Resource {
		if v, ok := cache[q.Name]; ok {
			return v
		}
		if visiting[q.Name] {
			return api.Resource{}
		}
		visiting[q.Name] = true
		defer delete(visiting, q.Name)

		var deserved api.Resource
		if q.Name == "root" {
			deserved = total.Clone()
		} else if q.Spec.Deserved != nil {
			deserved = q.Spec.Deserved.Clone()
		} else {
			parentName := parentOf(q)
			parentDeserved := total
			if parent := ssn.Queue(parentName); parent != nil && parent.Name != q.Name {
				parentDeserved = compute(parent)
			}
			var siblingWeight int64
			for _, sib := range queues {
				if sib.Name == "root" {
					continue
				}
				if parentOf(sib) == parentName {
					siblingWeight += int64(sib.Spec.Weight)
				}
			}
			if siblingWeight <= 0 || q.Spec.Weight <= 0 {
				deserved = api.Resource{}
			} else {
				deserved = parentDeserved.MulScalar(float64(q.Spec.Weight) / float64(siblingWeight))
			}
		}
		if q.Spec.Capability != nil {
			for k, v := range deserved {
				if cap := q.Spec.Capability[k]; cap > 0 && v > cap {
					deserved[k] = cap
				}
			}
		}
		for k, v := range q.Spec.Guarantee {
			if deserved[k] < v {
				deserved[k] = v
			}
		}
		cache[q.Name] = deserved
		q.Status.Deserved = deserved
		return deserved
	}

	for _, q := range queues {
		if c.enableHierarchy || q.Spec.Parent == "" {
			compute(q)
		} else {
			// Without hierarchy enabled, parent relationships are ignored and
			// a queue's deserved defaults to its explicit value or capability.
			if q.Spec.Deserved != nil {
				q.Status.Deserved = q.Spec.Deserved.Clone()
			} else if q.Spec.Capability != nil {
				q.Status.Deserved = q.Spec.Capability.Clone()
			} else {
				q.Status.Deserved = total.Clone()
			}
		}
	}

	// Allocated including descendants, used for ancestor capability checks.
	var allocatedTree func(string, map[string]bool) api.Resource
	allocatedTree = func(name string, seen map[string]bool) api.Resource {
		if seen[name] {
			return api.Resource{}
		}
		seen[name] = true
		out := ssn.Cache.QueueAllocated(name)
		if !c.enableHierarchy {
			return out
		}
		for _, q := range queues {
			if q.Name == name || q.Name == "root" {
				continue
			}
			if parentOf(q) == name {
				out = out.Add(allocatedTree(q.Name, seen))
			}
		}
		return out
	}
	tree := func(name string) api.Resource { return allocatedTree(name, map[string]bool{}) }

	var effectiveCapability func(*api.Queue, map[string]bool) api.Resource
	effectiveCapability = func(q *api.Queue, seen map[string]bool) api.Resource {
		if seen[q.Name] {
			return nil
		}
		seen[q.Name] = true
		if q.Spec.Capability != nil {
			return q.Spec.Capability
		}
		if c.enableHierarchy {
			if parent := ssn.Queue(parentOf(q)); parent != nil && parent.Name != q.Name {
				return effectiveCapability(parent, seen)
			}
		}
		return nil
	}
	capOf := func(q *api.Queue) api.Resource { return effectiveCapability(q, map[string]bool{}) }

	allocatable := func(q *api.Queue, res api.Resource) bool {
		if q == nil || q.State() != api.QueueOpen {
			return false
		}
		if cap := capOf(q); cap != nil {
			if !tree(q.Name).Add(res).WithinLimits(cap) {
				return false
			}
		}
		if c.enableHierarchy {
			for name := parentOf(q); name != "" && name != "root"; {
				parent := ssn.Queue(name)
				if parent == nil {
					break
				}
				if parent.State() != api.QueueOpen {
					return false
				}
				if cap := capOf(parent); cap != nil {
					if !tree(parent.Name).Add(res).WithinLimits(cap) {
						return false
					}
				}
				name = parentOf(parent)
			}
		}
		return true
	}

	ssn.AddAllocatableFn("capacity", func(q *api.Queue, res api.Resource) bool {
		return allocatable(q, res)
	})

	ssn.AddQueueOrderFn("capacity", func(l, r *api.Queue) int {
		if l.Spec.Priority != r.Spec.Priority {
			if l.Spec.Priority > r.Spec.Priority {
				return -1
			}
			return 1
		}
		ls := shareOf(tree(l.Name), l.Status.Deserved)
		rs := shareOf(tree(r.Name), r.Status.Deserved)
		switch {
		case ls < rs:
			return -1
		case ls > rs:
			return 1
		}
		return 0
	})

	ssn.AddJobEnqueueableFn("capacity", func(job *api.Job) bool {
		q := ssn.Queue(job.Spec.Queue)
		return q != nil && allocatable(q, job.MinResources())
	})

	ssn.AddOverusedFn("capacity", func(q *api.Queue) bool {
		return !tree(q.Name).LessEqual(q.Status.Deserved)
	})

	ssn.AddReclaimableFn("capacity", func(q *api.Queue, victim *api.Pod) bool {
		if !c.enablePreemptable {
			return false
		}
		return q.Reclaimable() && !tree(q.Name).LessEqual(q.Status.Deserved)
	})
}

func (c *Capacity) OnSessionClose(ssn *framework.Session) {}
