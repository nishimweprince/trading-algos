package stats

import (
	"math"
	"math/rand/v2"
	"sort"
	"testing"
)

func TestIndexBoundsAndMonotone(t *testing.T) {
	prev := -1
	for _, v := range []int64{0, 1, 7, 8, 9, 15, 16, 17, 31, 32, 1000, 1 << 40, math.MaxInt64} {
		i := index(v)
		if i < prev || i >= nBuckets {
			t.Fatalf("index(%d) = %d (prev %d)", v, i, prev)
		}
		lo, w := lower(i)
		if v < lo || (w > 0 && v-lo >= w && v != math.MaxInt64) {
			t.Fatalf("v=%d not in bucket %d [%d,+%d)", v, i, lo, w)
		}
		prev = i
	}
}

func TestQuantileWithinBucketError(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	var h Hist
	vals := make([]int64, 100_000)
	for i := range vals {
		vals[i] = int64(r.ExpFloat64() * 50_000) // ns-ish
		h.Record(vals[i])
	}
	sort.Slice(vals, func(a, b int) bool { return vals[a] < vals[b] })
	for _, q := range []float64{0.5, 0.9, 0.99} {
		exact := float64(vals[int(q*float64(len(vals)-1))])
		got := float64(h.Quantile(q))
		if math.Abs(got-exact)/exact > 0.13 {
			t.Errorf("q%.2f = %.0f, exact %.0f", q, got, exact)
		}
	}
	if h.Quantile(1) != h.Max {
		t.Errorf("q1 = %d, max %d", h.Quantile(1), h.Max)
	}
}

func TestRecordNoAlloc(t *testing.T) {
	var h Hist
	if n := testing.AllocsPerRun(1000, func() { h.Record(12345) }); n != 0 {
		t.Fatalf("Record allocates %v", n)
	}
}
