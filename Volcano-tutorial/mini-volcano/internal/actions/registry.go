// Package actions implements Volcano's scheduling pipeline. Each action gets
// the same session snapshot and may mutate it through statements.
package actions

import (
	"fmt"
	"sort"

	"example.com/minivolcano/internal/framework"
)

type Action interface {
	Name() string
	Execute(ssn *framework.Session) error
}

var registry = map[string]func() Action{
	"enqueue":  func() Action { return &Enqueue{} },
	"allocate": func() Action { return &Allocate{} },
	"preempt":  func() Action { return &Preempt{} },
	"reclaim":  func() Action { return &Reclaim{} },
	"backfill": func() Action { return &Backfill{} },
	"shuffle":  func() Action { return &Shuffle{} },
}

func Names() []string {
	out := make([]string, 0, len(registry))
	for k := range registry {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func Build(name string) (Action, error) {
	f, ok := registry[name]
	if !ok {
		return nil, fmt.Errorf("unknown scheduler action %q", name)
	}
	return f(), nil
}
