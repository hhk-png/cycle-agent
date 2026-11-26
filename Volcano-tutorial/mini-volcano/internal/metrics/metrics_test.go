package metrics

import (
	"strings"
	"testing"
)

func TestCounterAndGaugeRender(t *testing.T) {
	r := New()
	r.Inc("minivolcano_jobs_total", "Jobs", map[string]string{"queue": "a"})
	r.Inc("minivolcano_jobs_total", "Jobs", map[string]string{"queue": "a"})
	r.Set("minivolcano_queue_allocated", "Allocated", map[string]string{"queue": "a", "resource": "cpu"}, 1500)
	out := r.Render()
	for _, want := range []string{
		"# HELP minivolcano_jobs_total Jobs",
		"# TYPE minivolcano_jobs_total counter",
		`minivolcano_jobs_total{queue="a"} 2`,
		"# TYPE minivolcano_queue_allocated gauge",
		`minivolcano_queue_allocated{queue="a",resource="cpu"} 1500`,
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("render missing %q:\n%s", want, out)
		}
	}
}

func TestValueLookup(t *testing.T) {
	r := New()
	r.Add("c", "counter", map[string]string{"x": "y"}, 3)
	if v, ok := r.Value("c", map[string]string{"x": "y"}); !ok || v != 3 {
		t.Fatalf("value=%v ok=%v", v, ok)
	}
	if _, ok := r.Value("c", nil); ok {
		t.Fatal("unexpected sample")
	}
}

func TestLabelEscaping(t *testing.T) {
	r := New()
	r.Set("g", "gauge", map[string]string{"path": `a"b\c`}, 1)
	out := r.Render()
	if !strings.Contains(out, `path="a\"b\\c"`) {
		t.Fatalf("escaping failed:\n%s", out)
	}
}

func TestResetRemovesStaleGauge(t *testing.T) {
	r := New()
	r.Set("jobs", "gauge", map[string]string{"phase": "Pending"}, 1)
	r.Reset("jobs")
	r.Set("jobs", "gauge", map[string]string{"phase": "Running"}, 2)
	if _, ok := r.Value("jobs", map[string]string{"phase": "Pending"}); ok {
		t.Fatal("stale sample survived reset")
	}
	if v, ok := r.Value("jobs", map[string]string{"phase": "Running"}); !ok || v != 2 {
		t.Fatal("fresh sample missing")
	}
}

func TestRenderDeterministic(t *testing.T) {
	r := New()
	r.Set("b", "gauge", nil, 2)
	r.Set("a", "gauge", map[string]string{"z": "1"}, 1)
	r.Set("a", "gauge", map[string]string{"a": "1"}, 3)
	out := r.Render()
	if strings.Index(out, "# HELP a ") > strings.Index(out, "# HELP b ") {
		t.Fatal("metric families not sorted")
	}
	if strings.Index(out, `a{a="1"}`) > strings.Index(out, `a{z="1"}`) {
		t.Fatal("samples not sorted")
	}
}
