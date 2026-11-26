// Package plugins contains MiniVolcano's scheduler plugins. The set is a
// deliberately faithful subset of Volcano's built-ins; each plugin registers
// framework hooks in OnSessionOpen and keeps no per-session global state.
package plugins

import (
	"fmt"
	"sort"
	"strconv"
	"strings"

	"example.com/minivolcano/internal/framework"
)

type Builder func(args map[string]string) (framework.Plugin, error)

var registry = map[string]Builder{
	"gang":                    newGang,
	"priority":                newPriority,
	"drf":                     newDRF,
	"proportion":              newProportion,
	"capacity":                newCapacity,
	"predicates":              newPredicates,
	"nodeorder":               newNodeOrder,
	"binpack":                 newBinpack,
	"task-topology":           newTaskTopology,
	"network-topology-aware":  newNetworkTopology,
	"group-topology-affinity": newGroupTopologyAffinity,
	"tdm":                     newTDM,
	"reservation":             newReservation,
	"sla":                     newSLA,
	"fairshare":               newFairShare,
	"numa-aware":              newNUMA,
	"overcommit":              newOvercommit,
	"conformance":             newConformance,
	"resource-strategy-fit":   newResourceStrategyFit,
	"rescheduling":            newRescheduling,
	"pdb":                     newPDB,
	"resourcequota":           newResourceQuota,
	"nodegroup":               newNodeGroup,
}

func Names() []string {
	out := make([]string, 0, len(registry))
	for k := range registry {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func Build(name string, args map[string]string) (framework.Plugin, error) {
	b, ok := registry[name]
	if !ok {
		return nil, fmt.Errorf("unknown scheduler plugin %q (available: %s)", name, strings.Join(Names(), ", "))
	}
	return b(args)
}

func boolArg(args map[string]string, key string, def bool) (bool, error) {
	v, ok := args[key]
	if !ok || v == "" {
		return def, nil
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return false, fmt.Errorf("plugin argument %s=%q is not a bool", key, v)
	}
	return b, nil
}

func floatArg(args map[string]string, key string, def float64) (float64, error) {
	v, ok := args[key]
	if !ok || v == "" {
		return def, nil
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return 0, fmt.Errorf("plugin argument %s=%q is not a number", key, v)
	}
	if f < 0 {
		return 0, fmt.Errorf("plugin argument %s must be non-negative", key)
	}
	return f, nil
}

func intArg(args map[string]string, key string, def int) (int, error) {
	v, ok := args[key]
	if !ok || v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return 0, fmt.Errorf("plugin argument %s=%q is not an integer", key, v)
	}
	return n, nil
}

func stringArg(args map[string]string, key, def string) string {
	if v, ok := args[key]; ok && v != "" {
		return v
	}
	return def
}

// shareOf returns the dominant allocated/deserved ratio. A zero denominator
// with positive usage yields +Inf so the queue is ordered last.
func shareOf(allocated, capacity map[string]int64) float64 {
	best := 0.0
	for k, v := range allocated {
		if v <= 0 {
			continue
		}
		den := capacity[k]
		if den <= 0 {
			return 1e18
		}
		s := float64(v) / float64(den)
		if s > best {
			best = s
		}
	}
	return best
}
