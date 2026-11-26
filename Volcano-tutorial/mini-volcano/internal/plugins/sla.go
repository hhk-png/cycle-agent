package plugins

import (
	"fmt"
	"strconv"
	"strings"

	"example.com/minivolcano/internal/api"
	"example.com/minivolcano/internal/framework"
)

// SLA boosts jobs that have waited longer than their declared waiting-time
// budget. Volcano's SLA plugin pipelines overdue jobs to improve their chance
// of starting; MiniVolcano reorders them and records an audit event.
type SLA struct {
	defaultWaiting int64
	overdue        map[string]bool
}

func newSLA(args map[string]string) (framework.Plugin, error) {
	def, err := parseTicks(stringArg(args, "sla-waiting-time", "0"))
	if err != nil {
		return nil, fmt.Errorf("sla-waiting-time: %w", err)
	}
	return &SLA{defaultWaiting: def, overdue: map[string]bool{}}, nil
}

func (s *SLA) Name() string { return "sla" }

func (s *SLA) OnSessionOpen(ssn *framework.Session) {
	s.overdue = map[string]bool{}
	for _, job := range ssn.Jobs() {
		if job.Status.Phase.Terminal() {
			continue
		}
		budget := s.defaultWaiting
		if v, ok := job.Spec.Annotations["volcano.sh/sla-waiting-time"]; ok && v != "" {
			parsed, err := parseTicks(v)
			if err == nil {
				budget = parsed
			}
		}
		if budget <= 0 {
			continue
		}
		waited := ssn.Now - job.CreationTick
		if waited >= budget {
			s.overdue[job.UID] = true
			ssn.RecordUnschedulable(job.UID, fmt.Sprintf("SLA exceeded: waited %d ticks, budget %d", waited, budget))
			ssn.AddEvent(api.Event{Type: "SLAViolated", Job: job.Name, Queue: job.Spec.Queue,
				Message: fmt.Sprintf("waited=%d budget=%d", waited, budget)})
		}
	}

	ssn.AddJobOrderFn("sla", func(l, r *api.Job) int {
		ls, rs := s.overdue[l.UID], s.overdue[r.UID]
		if ls == rs {
			return 0
		}
		if ls {
			return -1
		}
		return 1
	})
}

func (s *SLA) OnSessionClose(ssn *framework.Session) {}

func parseTicks(v string) (int64, error) {
	v = strings.TrimSpace(v)
	v = strings.TrimSuffix(v, "t")
	if v == "" {
		return 0, nil
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("invalid tick value %q", v)
	}
	return n, nil
}
