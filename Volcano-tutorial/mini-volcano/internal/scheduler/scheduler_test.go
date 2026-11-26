package scheduler_test

import (
	"os"
	"path/filepath"
	"testing"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
	"example.com/minivolcano/internal/metrics"
	"example.com/minivolcano/internal/scheduler"
)

func fixture(t *testing.T) *cache.Cache {
	t.Helper()
	c := cache.New()
	if err := c.AddNode(&api.Node{Name: "n1", Allocatable: api.R(4000, 8192, 2)}); err != nil {
		t.Fatal(err)
	}
	if err := c.AddQueue(&api.Queue{Name: "default"}); err != nil {
		t.Fatal(err)
	}
	if err := c.SubmitJob(&api.Job{Name: "j", Namespace: "default", Spec: api.JobSpec{
		Queue: "default", MinAvailable: 1, MinAvailableSet: true,
		Tasks: []api.TaskSpec{{Name: "w", Replicas: 1, Requests: api.Cores(1)}},
	}}); err != nil {
		t.Fatal(err)
	}
	return c
}

func TestUnknownPlugin(t *testing.T) {
	cfg := framework.Config{Actions: []string{"enqueue"},
		Tiers: []framework.TierConfig{{Name: "t", Plugins: []framework.PluginConfig{{Name: "ghost"}}}}}
	if _, err := scheduler.New(fixture(t), cfg, nil); err == nil {
		t.Fatal("unknown plugin should fail")
	}
}

func TestUnknownAction(t *testing.T) {
	cfg := framework.Config{Actions: []string{"explode"}}
	if _, err := scheduler.New(fixture(t), cfg, nil); err == nil {
		t.Fatal("unknown action should fail")
	}
}

func TestProportionCapacityConflict(t *testing.T) {
	cfg := framework.DefaultConfig()
	cfg.Tiers[1].Plugins = append(cfg.Tiers[1].Plugins, framework.PluginConfig{Name: "capacity"})
	if _, err := scheduler.New(fixture(t), cfg, nil); err == nil {
		t.Fatal("proportion + capacity should be rejected")
	}
}

func TestRunPublishesAndCounts(t *testing.T) {
	c := fixture(t)
	reg := metrics.New()
	s, err := scheduler.New(c, framework.DefaultConfig(), reg)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Run(0); err != nil {
		t.Fatal(err)
	}
	if c.Pods["default/j-w-0"].Phase != api.PodAllocated {
		t.Fatalf("pod phase=%s", c.Pods["default/j-w-0"].Phase)
	}
	if v, _ := reg.Value("minivolcano_scheduler_sessions_total", nil); v != 1 {
		t.Fatalf("sessions metric=%v", v)
	}
	_ = s
}

func TestGauges(t *testing.T) {
	c := fixture(t)
	reg := metrics.New()
	s, err := scheduler.New(c, framework.DefaultConfig(), reg)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Run(0); err != nil {
		t.Fatal(err)
	}
	s.SetMetricsGauges()
	if _, ok := reg.Value("minivolcano_queue_allocated", map[string]string{"queue": "default", "resource": "cpu"}); !ok {
		t.Fatal("queue allocated gauge missing")
	}
	if _, ok := reg.Value("minivolcano_node_idle", map[string]string{"node": "n1", "resource": "cpu"}); !ok {
		t.Fatal("node idle gauge missing")
	}
}

func TestLoadConfig(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "sched.json")
	data := `{"actions":["enqueue","allocate"],"tiers":[{"name":"t1","plugins":[{"name":"gang"}]}]}`
	if err := os.WriteFile(path, []byte(data), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := framework.LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Actions) != 2 || len(cfg.Tiers) != 1 {
		t.Fatalf("config=%+v", cfg)
	}
	if err := os.WriteFile(path, []byte("{bad"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := framework.LoadConfig(path); err == nil {
		t.Fatal("invalid JSON should fail")
	}
}
