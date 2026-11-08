package scenario

import (
	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/multikueue"
)

// MultiKueue builds a manager with two workers. worker-1 has a one-CPU quota
// and cannot admit the two-CPU workload; worker-2 has enough room.
func MultiKueue(dispatcher multikueue.Dispatcher) *multikueue.Manager {
	worker1 := New()
	MustCQ(worker1, SimpleCQ("worker-1-cq", "default", api.RL("cpu", 1000)))
	MustLQ(worker1, "default", "q", "worker-1-cq")
	MustNamespace(worker1, "default", nil)

	worker2 := New()
	MustCQ(worker2, SimpleCQ("worker-2-cq", "default", api.RL("cpu", 4000)))
	MustLQ(worker2, "default", "q", "worker-2-cq")
	MustNamespace(worker2, "default", nil)

	w := Workload("default", "multi-job", "q", 0, 1, api.RL("cpu", 2000))
	return multikueue.NewManager("demo-manager", w, []*multikueue.Worker{
		multikueue.NewWorker("worker-1", worker1),
		multikueue.NewWorker("worker-2", worker2),
	}, multikueue.Config{Dispatcher: dispatcher, StepSize: 1})
}

// MultiKueueFailover builds a two-worker manager used by the failover demo.
// worker-1 starts unreachable, so worker-2 is selected first. The caller then
// flips the two workers to show the lost-worker timeout and re-dispatch path.
func MultiKueueFailover() *multikueue.Manager {
	worker1 := New()
	MustCQ(worker1, SimpleCQ("worker-1-cq", "default", api.RL("cpu", 4000)))
	MustLQ(worker1, "default", "q", "worker-1-cq")
	MustNamespace(worker1, "default", nil)

	worker2 := New()
	MustCQ(worker2, SimpleCQ("worker-2-cq", "default", api.RL("cpu", 4000)))
	MustLQ(worker2, "default", "q", "worker-2-cq")
	MustNamespace(worker2, "default", nil)

	w1 := multikueue.NewWorker("worker-1", worker1)
	w1.Down = true
	w := Workload("default", "multi-job", "q", 0, 1, api.RL("cpu", 2000))
	return multikueue.NewManager("failover-manager", w, []*multikueue.Worker{
		w1,
		multikueue.NewWorker("worker-2", worker2),
	}, multikueue.Config{
		Dispatcher:        multikueue.AllAtOnce,
		StepSize:          1,
		WorkerLostTimeout: 1,
	})
}
