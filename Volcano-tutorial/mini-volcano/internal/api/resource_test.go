package api

import "testing"

func TestResourceArithmetic(t *testing.T) {
	a := Resource{CPU: 1000, Memory: 2048, GPU: 1}
	b := Resource{CPU: 500, Memory: 1024}
	sum := a.Add(b)
	if sum[CPU] != 1500 || sum[Memory] != 3072 || sum[GPU] != 1 {
		t.Fatalf("add: %v", sum)
	}
	diff := a.Sub(b)
	if diff[CPU] != 500 || diff[Memory] != 1024 || diff[GPU] != 1 {
		t.Fatalf("sub: %v", diff)
	}
	if !b.LessEqual(a) {
		t.Fatal("b should be <= a")
	}
	if a.LessEqual(b) {
		t.Fatal("a should not be <= b")
	}
	if !a.Clone().Equal(a) {
		t.Fatal("clone should equal original")
	}
}

func TestResourceZeroAndKeys(t *testing.T) {
	r := Resource{CPU: 0, Memory: 1024}
	if r.IsZero() {
		t.Fatal("resource with memory should not be zero")
	}
	keys := r.Keys()
	if len(keys) != 1 || keys[0] != Memory {
		t.Fatalf("keys: %v", keys)
	}
	if (Resource{}).String() != "0" {
		t.Fatalf("empty string: %q", (Resource{}).String())
	}
}

func TestResourceString(t *testing.T) {
	r := R(1500, 2048, 1)
	want := "cpu=1.5,memory=2Gi,nvidia.com/gpu=1"
	if got := r.String(); got != want {
		t.Fatalf("got %q want %q", got, want)
	}
	if got := (Resource{CPU: 500}).String(); got != "cpu=0.5" {
		t.Fatalf("cpu 500m: %q", got)
	}
	if got := (Resource{CPU: 8000, GPU: 0}).StringAll(); got != "cpu=8,nvidia.com/gpu=0" {
		t.Fatalf("StringAll should include zero dimensions: %q", got)
	}
}

func TestResourceDominant(t *testing.T) {
	total := R(8000, 8192, 4)
	key, share := (Resource{CPU: 4000, Memory: 2048}).Dominant(total)
	if key != CPU || share != 0.5 {
		t.Fatalf("dominant=%s share=%v", key, share)
	}
	_, share = (Resource{GPU: 2}).Dominant(total)
	if share != 0.5 {
		t.Fatalf("gpu share=%v", share)
	}
}

func TestWithinLimits(t *testing.T) {
	used := Resource{CPU: 3000, Memory: 1024, GPU: 3}
	if !used.WithinLimits(Resource{GPU: 6}) {
		t.Fatal("partial capability must not constrain absent dimensions")
	}
	if used.WithinLimits(Resource{GPU: 2}) {
		t.Fatal("gpu 3 exceeds gpu 2")
	}
	if !used.WithinLimits(Resource{CPU: 3000, GPU: 3}) {
		t.Fatal("exact limit should pass")
	}
}

func TestMulScalar(t *testing.T) {
	r := R(8000, 4096, 4).MulScalar(0.5)
	if r[CPU] != 4000 || r[Memory] != 2048 || r[GPU] != 2 {
		t.Fatalf("mul: %v", r)
	}
}

func TestParseQuantity(t *testing.T) {
	tests := []struct {
		in    string
		key   string
		value int64
		ok    bool
	}{
		{"1500m", CPU, 1500, true},
		{"2Gi", Memory, 2048, true},
		{"512Mi", Memory, 512, true},
		{"4", "", 4, true},
		{"bad", "", 0, false},
		{"1.5Gi", Memory, 1536, true},
	}
	for _, tt := range tests {
		key, value, err := ParseQuantity(tt.in)
		if tt.ok && err != nil {
			t.Errorf("%s: unexpected error %v", tt.in, err)
			continue
		}
		if !tt.ok && err == nil {
			t.Errorf("%s: expected error", tt.in)
			continue
		}
		if ok := err == nil; ok && (key != tt.key || value != tt.value) {
			t.Errorf("%s: got (%q,%d), want (%q,%d)", tt.in, key, value, tt.key, tt.value)
		}
	}
}
