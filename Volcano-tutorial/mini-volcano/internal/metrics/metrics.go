// Package metrics provides a tiny Prometheus-compatible registry. It is not a
// replacement for client_golang; it exists so the simulator can exercise the
// observability contract without external dependencies.
package metrics

import (
	"sort"
	"strconv"
	"strings"
)

type family struct {
	kind    string // counter or gauge
	help    string
	samples map[string]*sample
}

type sample struct {
	labels map[string]string
	value  float64
}

type Registry struct {
	families map[string]*family
}

func New() *Registry {
	return &Registry{families: map[string]*family{}}
}

func (r *Registry) Add(name, help string, labels map[string]string, delta float64) {
	r.ensure(name, "counter", help)
	r.sample(name, labels).value += delta
}

func (r *Registry) Inc(name, help string, labels map[string]string) {
	r.Add(name, help, labels, 1)
}

func (r *Registry) Set(name, help string, labels map[string]string, value float64) {
	r.ensure(name, "gauge", help)
	r.sample(name, labels).value = value
}

// Reset drops a metric family. It is used for gauges whose label sets shrink
// between collection cycles, so stale samples do not linger.
func (r *Registry) Reset(name string) {
	delete(r.families, name)
}

func (r *Registry) ensure(name, kind, help string) {
	f, ok := r.families[name]
	if !ok {
		f = &family{kind: kind, help: help, samples: map[string]*sample{}}
		r.families[name] = f
		return
	}
	if help != "" {
		f.help = help
	}
}

func (r *Registry) sample(name string, labels map[string]string) *sample {
	f := r.families[name]
	k := labelKey(labels)
	if s, ok := f.samples[k]; ok {
		return s
	}
	s := &sample{labels: cloneLabels(labels)}
	f.samples[k] = s
	return s
}

func cloneLabels(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func labelKey(labels map[string]string) string {
	if len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteString("{")
	for i, k := range keys {
		if i > 0 {
			b.WriteString(",")
		}
		b.WriteString(k)
		b.WriteString("=\"")
		b.WriteString(escape(labels[k]))
		b.WriteString("\"")
	}
	b.WriteString("}")
	return b.String()
}

func escape(s string) string {
	s = strings.ReplaceAll(s, "\\", "\\\\")
	s = strings.ReplaceAll(s, "\"", "\\\"")
	s = strings.ReplaceAll(s, "\n", "\\n")
	return s
}

// Render produces deterministic Prometheus text exposition.
func (r *Registry) Render() string {
	names := make([]string, 0, len(r.families))
	for n := range r.families {
		names = append(names, n)
	}
	sort.Strings(names)
	var b strings.Builder
	for _, name := range names {
		f := r.families[name]
		b.WriteString("# HELP ")
		b.WriteString(name)
		b.WriteString(" ")
		b.WriteString(f.help)
		b.WriteString("\n# TYPE ")
		b.WriteString(name)
		b.WriteString(" ")
		b.WriteString(f.kind)
		b.WriteString("\n")
		keys := make([]string, 0, len(f.samples))
		for k := range f.samples {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			s := f.samples[k]
			b.WriteString(name)
			b.WriteString(k)
			b.WriteString(" ")
			b.WriteString(format(s.value))
			b.WriteString("\n")
		}
	}
	return b.String()
}

func format(v float64) string {
	if v == float64(int64(v)) {
		return strconv.FormatInt(int64(v), 10)
	}
	return strconv.FormatFloat(v, 'f', 4, 64)
}

// Value returns a sample value for tests and diagnostics.
func (r *Registry) Value(name string, labels map[string]string) (float64, bool) {
	f, ok := r.families[name]
	if !ok {
		return 0, false
	}
	s, ok := f.samples[labelKey(labels)]
	if !ok {
		return 0, false
	}
	return s.value, true
}

func (r *Registry) Names() []string {
	out := make([]string, 0, len(r.families))
	for n := range r.families {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}
