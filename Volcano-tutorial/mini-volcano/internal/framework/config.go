package framework

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
)

// PluginConfig is one entry in a tier, mirroring Volcano's scheduler config:
//
//	tiers:
//	- name: t1
//	  plugins:
//	  - name: priority
//	  - name: gang
//	    arguments:
//	      enablePreemptable: "true"
type PluginConfig struct {
	Name string            `json:"name"`
	Args map[string]string `json:"args,omitempty"`
}

type TierConfig struct {
	Name    string         `json:"name"`
	Plugins []PluginConfig `json:"plugins"`
}

// Config is the JSON-friendly equivalent of volcano-scheduler.conf. Real
// Volcano uses YAML; MiniVolcano uses JSON to stay dependency-free, and the
// tutorial's check_docs.py validates the real YAML separately.
type Config struct {
	Actions          []string     `json:"actions"`
	Tiers            []TierConfig `json:"tiers"`
	EnqueueIdleCheck bool         `json:"enqueueIdleCheck,omitempty"`
}

// DefaultConfig enables the same core plugins as Volcano's default config,
// plus preempt/reclaim so the simulator can demonstrate the full pipeline.
func DefaultConfig() Config {
	return Config{
		Actions: []string{"enqueue", "allocate", "preempt", "reclaim", "backfill"},
		Tiers: []TierConfig{
			{Name: "tier-1", Plugins: []PluginConfig{
				{Name: "priority"},
				{Name: "gang", Args: map[string]string{"enablePreemptable": "true"}},
				{Name: "conformance"},
			}},
			{Name: "tier-2", Plugins: []PluginConfig{
				{Name: "drf"},
				{Name: "predicates"},
				{Name: "proportion"},
				{Name: "nodeorder"},
				{Name: "binpack", Args: map[string]string{"weight": "1"}},
			}},
		},
	}
}

// Validate checks structural constraints that do not require the plugin
// registry. The scheduler performs registry and cross-plugin validation.
func (c Config) Validate() error {
	if len(c.Actions) == 0 {
		return fmt.Errorf("scheduler config: at least one action is required")
	}
	seenAction := map[string]bool{}
	for _, a := range c.Actions {
		if a == "" {
			return fmt.Errorf("scheduler config: action name must not be empty")
		}
		if seenAction[a] {
			return fmt.Errorf("scheduler config: duplicate action %q", a)
		}
		seenAction[a] = true
	}
	seenPlugin := map[string]bool{}
	for _, tier := range c.Tiers {
		if tier.Name == "" {
			return fmt.Errorf("scheduler config: tier name must not be empty")
		}
		for _, p := range tier.Plugins {
			if p.Name == "" {
				return fmt.Errorf("scheduler config: tier %q has an unnamed plugin", tier.Name)
			}
			if seenPlugin[p.Name] {
				return fmt.Errorf("scheduler config: plugin %q appears more than once", p.Name)
			}
			seenPlugin[p.Name] = true
		}
	}
	return nil
}

// LoadConfig reads the JSON form of the scheduler configuration. The real
// volcano-scheduler.conf is YAML with the same actions/tiers shape; examples/
// contains that form for a live cluster.
func LoadConfig(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return Config{}, fmt.Errorf("parse scheduler config %s: %w", path, err)
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c Config) PluginNames() []string {
	var out []string
	for _, t := range c.Tiers {
		for _, p := range t.Plugins {
			out = append(out, p.Name)
		}
	}
	sort.Strings(out)
	return out
}

func (c Config) HasPlugin(name string) bool {
	for _, t := range c.Tiers {
		for _, p := range t.Plugins {
			if p.Name == name {
				return true
			}
		}
	}
	return false
}
