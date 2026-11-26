package plugins

import (
	"fmt"
	"strconv"
	"strings"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/cache"
	"example.com/minivolcano/internal/framework"
)

// TDM implements time-division multiplexing for revocable zones. Nodes labelled
// volcano.sh/revocable-zone are available to preemptable jobs only inside their
// configured window; ordinary jobs may use them outside the window.
//
// Windows are expressed in scheduler ticks: window.rz1=10-20. Real Volcano uses
// wall-clock windows and additionally evicts preemptable pods when a window
// ends; MiniVolcano's controller does not yet perform that eviction.
type TDM struct {
	windows map[string][2]int64
}

func newTDM(args map[string]string) (framework.Plugin, error) {
	w := map[string][2]int64{}
	for k, v := range args {
		if !strings.HasPrefix(k, "window.") {
			continue
		}
		zone := strings.TrimPrefix(k, "window.")
		parts := strings.Split(v, "-")
		if len(parts) != 2 {
			return nil, fmt.Errorf("tdm window %s=%q must be start-end", k, v)
		}
		start, err1 := strconv.ParseInt(strings.TrimSpace(parts[0]), 10, 64)
		end, err2 := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
		if err1 != nil || err2 != nil || end <= start {
			return nil, fmt.Errorf("tdm window %s=%q is invalid", k, v)
		}
		w[zone] = [2]int64{start, end}
	}
	if len(w) == 0 {
		return nil, fmt.Errorf("tdm requires at least one window.<zone>=start-end argument")
	}
	return &TDM{windows: w}, nil
}

func (t *TDM) Name() string { return "tdm" }

func (t *TDM) OnSessionOpen(ssn *framework.Session) {
	ssn.AddPredicateFn("tdm", func(pod *api.Pod, node *cache.NodeState) error {
		zone := node.Node.Labels["volcano.sh/revocable-zone"]
		job := ssn.JobByUID(pod.Job)
		preemptable := job != nil && job.Spec.Annotations["volcano.sh/preemptable"] == "true"
		if preemptable {
			if zone == "" {
				return fmt.Errorf("preemptable job requires a revocable-zone node; %s is not one", node.Node.Name)
			}
			w, ok := t.windows[zone]
			if !ok {
				return fmt.Errorf("no TDM window configured for zone %s", zone)
			}
			if ssn.Now < w[0] || ssn.Now >= w[1] {
				return fmt.Errorf("zone %s is outside its TDM window [%d,%d) at tick %d", zone, w[0], w[1], ssn.Now)
			}
			return nil
		}
		if zone == "" {
			return nil
		}
		if w, ok := t.windows[zone]; ok && ssn.Now >= w[0] && ssn.Now < w[1] {
			return fmt.Errorf("node %s is reserved for preemptable jobs in TDM window [%d,%d)", node.Node.Name, w[0], w[1])
		}
		return nil
	})

	ssn.AddNodeOrderFn("tdm", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
		job := ssn.JobByUID(pod.Job)
		if job != nil && job.Spec.Annotations["volcano.sh/preemptable"] == "true" &&
			node.Node.Labels["volcano.sh/revocable-zone"] != "" {
			return 2, nil
		}
		return 0, nil
	})
}

func (t *TDM) OnSessionClose(ssn *framework.Session) {}
