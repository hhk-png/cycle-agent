package scheduler

import (
	"fmt"
	"testing"

	"example.com/minikueue/internal/api"
	"example.com/minikueue/internal/cache"
)

func BenchmarkTryAdmit(b *testing.B) {
	cq := testCQ("bench", 1_000_000)
	c := cache.New()
	for i := 0; i < 500; i++ {
		w := testWorkload(fmt.Sprintf("w%04d", i), 0, 1000, nil)
		s := New(c, NewView([]*api.ClusterQueue{cq}, nil))
		commit(b, c, w, s.TryAdmit(w, cq))
	}
	s := New(c, NewView([]*api.ClusterQueue{cq}, nil))
	w := testWorkload("candidate", 0, 1000, nil)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if d := s.TryAdmit(w, cq); d.Admission == nil {
			b.Fatal("unexpected rejection")
		}
	}
}
