package api

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Resource mirrors a Kubernetes v1.ResourceList in a simplified form.
//
// Units used internally:
//   - "cpu": milli-cores (1000 == one core)
//   - "memory": MiB
//   - "nvidia.com/gpu" and other scalar resources: whole units
//
// Keeping the units fixed makes the simulator deterministic and avoids the
// float rounding problems that plague naive scheduler implementations.
type Resource map[string]int64

const (
	CPU    = "cpu"
	Memory = "memory"
	GPU    = "nvidia.com/gpu"
)

// R builds a Resource from cpu milli-cores, memory MiB and GPU count.
// Pass -1 for any dimension that should be omitted.
func R(cpuMilli, memMi, gpu int64) Resource {
	r := Resource{}
	if cpuMilli >= 0 {
		r[CPU] = cpuMilli
	}
	if memMi >= 0 {
		r[Memory] = memMi
	}
	if gpu >= 0 {
		r[GPU] = gpu
	}
	return r
}

// Cores is a convenience constructor for CPU-only resources.
func Cores(n float64) Resource {
	return Resource{CPU: int64(n * 1000)}
}

// Mi is a convenience constructor for memory-only resources.
func Mi(n int64) Resource {
	return Resource{Memory: n}
}

func (r Resource) Clone() Resource {
	if r == nil {
		return Resource{}
	}
	out := make(Resource, len(r))
	for k, v := range r {
		if v != 0 {
			out[k] = v
		}
	}
	return out
}

func (r Resource) Get(name string) int64 { return r[name] }

func (r Resource) Set(name string, v int64) Resource {
	if r == nil {
		r = Resource{}
	}
	if v == 0 {
		delete(r, name)
	} else {
		r[name] = v
	}
	return r
}

// Add returns r + o without mutating either operand.
func (r Resource) Add(o Resource) Resource {
	out := r.Clone()
	for k, v := range o {
		out[k] += v
	}
	return out
}

// Sub returns r - o. Dimensions may become negative; callers decide whether
// that is an error (the cache treats it as an invariant violation).
func (r Resource) Sub(o Resource) Resource {
	out := r.Clone()
	for k, v := range o {
		out[k] -= v
	}
	return out
}

// MulScalar multiplies every dimension by f and rounds down. It is used by the
// overcommit plugin; capacity accounting never uses fractional resources.
func (r Resource) MulScalar(f float64) Resource {
	out := make(Resource, len(r))
	for k, v := range r {
		out[k] = int64(float64(v) * f)
	}
	return out
}

func (r Resource) IsZero() bool {
	for _, v := range r {
		if v != 0 {
			return false
		}
	}
	return true
}

func (r Resource) Equal(o Resource) bool {
	for k, v := range r {
		if o[k] != v {
			return false
		}
	}
	for k, v := range o {
		if r[k] != v {
			return false
		}
	}
	return true
}

// LessEqual reports whether r <= o in every dimension. Missing dimensions are 0.
func (r Resource) LessEqual(o Resource) bool {
	for k, v := range r {
		if v > o[k] {
			return false
		}
	}
	for k, v := range o {
		if r[k] > v {
			return false
		}
	}
	return true
}

// WithinLimits reports whether every dimension explicitly present in limit is
// respected. Dimensions absent from a capability are unconstrained, matching
// Kubernetes ResourceList semantics for a partial queue capability.
func (r Resource) WithinLimits(limit Resource) bool {
	for k, max := range limit {
		if r[k] > max {
			return false
		}
	}
	return true
}

// Cmp compares two resources lexicographically after sorting dimensions.
// It is used only for deterministic tie-breaking, not for scheduling.
func (r Resource) Cmp(o Resource) int {
	keys := make(map[string]struct{}, len(r)+len(o))
	for k := range r {
		keys[k] = struct{}{}
	}
	for k := range o {
		keys[k] = struct{}{}
	}
	sorted := make([]string, 0, len(keys))
	for k := range keys {
		sorted = append(sorted, k)
	}
	sort.Strings(sorted)
	for _, k := range sorted {
		if r[k] < o[k] {
			return -1
		}
		if r[k] > o[k] {
			return 1
		}
	}
	return 0
}

// Dominant returns the resource dimension with the largest share of total and
// that share. This is the core primitive of DRF.
func (r Resource) Dominant(total Resource) (string, float64) {
	bestKey := ""
	best := 0.0
	for k, v := range r {
		if v <= 0 || total[k] <= 0 {
			continue
		}
		share := float64(v) / float64(total[k])
		if share > best || (share == best && k < bestKey) || bestKey == "" {
			bestKey, best = k, share
		}
	}
	return bestKey, best
}

func (r Resource) Keys() []string {
	keys := make([]string, 0, len(r))
	for k, v := range r {
		if v != 0 {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return keys
}

// String renders resources in a stable order, e.g. "cpu=1.5,memory=2Gi,nvidia.com/gpu=1".
func (r Resource) String() string {
	keys := r.Keys()
	if len(keys) == 0 {
		return "0"
	}
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+formatValue(k, r[k]))
	}
	return strings.Join(parts, ",")
}

// StringAll renders every key in the map, including zero values. It is used
// for diagnostics where omitting a zero dimension would hide the actual
// shortage (for example "need gpu=1, idle cpu=8").
func (r Resource) StringAll() string {
	keys := make([]string, 0, len(r))
	for k := range r {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if len(keys) == 0 {
		return "0"
	}
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+formatValue(k, r[k]))
	}
	return strings.Join(parts, ",")
}

func formatValue(key string, v int64) string {
	switch key {
	case CPU:
		return formatCPU(v)
	case Memory:
		return formatMemory(v)
	default:
		return strconv.FormatInt(v, 10)
	}
}

func formatCPU(milli int64) string {
	cores := float64(milli) / 1000.0
	s := strconv.FormatFloat(cores, 'f', 3, 64)
	s = strings.TrimRight(s, "0")
	s = strings.TrimRight(s, ".")
	if s == "" || s == "-0" {
		return "0"
	}
	return s
}

func formatMemory(mi int64) string {
	if mi%1024 == 0 {
		return fmt.Sprintf("%dGi", mi/1024)
	}
	return fmt.Sprintf("%dMi", mi)
}

// ParseQuantity parses the tiny subset used by the CLI and tests:
// "1500m" / "1.5" for CPU, "2Gi" / "512Mi" for memory, plain integers for
// scalar resources. The dimension is inferred from the unit.
func ParseQuantity(s string) (string, int64, error) {
	s = strings.TrimSpace(s)
	switch {
	case strings.HasSuffix(s, "m"):
		v, err := strconv.ParseInt(strings.TrimSuffix(s, "m"), 10, 64)
		if err != nil {
			return "", 0, fmt.Errorf("invalid cpu quantity %q", s)
		}
		return CPU, v, nil
	case strings.HasSuffix(s, "Gi"):
		v, err := strconv.ParseFloat(strings.TrimSuffix(s, "Gi"), 64)
		if err != nil {
			return "", 0, fmt.Errorf("invalid memory quantity %q", s)
		}
		return Memory, int64(v * 1024), nil
	case strings.HasSuffix(s, "Mi"):
		v, err := strconv.ParseInt(strings.TrimSuffix(s, "Mi"), 10, 64)
		if err != nil {
			return "", 0, fmt.Errorf("invalid memory quantity %q", s)
		}
		return Memory, v, nil
	default:
		v, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			return "", 0, fmt.Errorf("invalid scalar quantity %q", s)
		}
		return "", v, nil
	}
}
