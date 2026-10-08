// Package stats has allocation-free latency histograms for the hot path.
package stats

import (
	"math/bits"
)

// buckets: values 0..7 exact, then 8 log-linear sub-buckets per power of two
// (≤ 12.5% relative error), covering the whole int64 range.
const nBuckets = 8 + 61*8

// Hist is a log-linear histogram of non-negative int64 values (the caller picks
// the unit, e.g. ns or µs). It is not safe for concurrent use; callers guard it
// with the lock they already hold, and take snapshots by copying.
type Hist struct {
	Counts [nBuckets]uint64
	N      uint64
	Sum    int64
	Max    int64
}

func index(v int64) int {
	if v < 8 {
		if v < 0 {
			return 0
		}
		return int(v)
	}
	e := bits.Len64(uint64(v)) - 1 // ≥ 3
	m := int(uint64(v)>>(e-3)) & 7
	return (e-2)*8 + m
}

// lower returns the smallest value in bucket i, and the bucket's width.
func lower(i int) (int64, int64) {
	if i < 8 {
		return int64(i), 1
	}
	e := i/8 + 2
	m := int64(i % 8)
	return (8 + m) << (e - 3), int64(1) << (e - 3)
}

// Record adds one value.
func (h *Hist) Record(v int64) {
	if v < 0 {
		v = 0
	}
	h.Counts[index(v)]++
	h.N++
	h.Sum += v
	if v > h.Max {
		h.Max = v
	}
}

// Quantile returns an estimate of the q-quantile (0 ≤ q ≤ 1): the midpoint of
// the bucket holding it, capped at Max.
func (h *Hist) Quantile(q float64) int64 {
	if h.N == 0 {
		return 0
	}
	if q >= 1 {
		return h.Max
	}
	rank := uint64(q*float64(h.N-1)) + 1
	var cum uint64
	for i, c := range h.Counts {
		cum += c
		if cum >= rank {
			lo, w := lower(i)
			v := lo + w/2
			if v > h.Max {
				v = h.Max
			}
			return v
		}
	}
	return h.Max
}

// Merge adds o into h.
func (h *Hist) Merge(o *Hist) {
	for i, c := range o.Counts {
		h.Counts[i] += c
	}
	h.N += o.N
	h.Sum += o.Sum
	if o.Max > h.Max {
		h.Max = o.Max
	}
}

// Sub returns h − prev for a window between two cumulative snapshots. The
// window's Max is the upper edge of its highest non-empty bucket (capped at
// h.Max), since an exact maximum cannot be recovered from counts.
func (h *Hist) Sub(prev *Hist) Hist {
	var d Hist
	for i := range h.Counts {
		d.Counts[i] = h.Counts[i] - prev.Counts[i]
		if d.Counts[i] > 0 {
			lo, w := lower(i)
			d.Max = min(lo+w-1, h.Max)
		}
	}
	d.N = h.N - prev.N
	d.Sum = h.Sum - prev.Sum
	return d
}

// Reset clears h.
func (h *Hist) Reset() { *h = Hist{} }

// Summary is a JSON-friendly digest.
type Summary struct {
	N    uint64  `json:"n"`
	Mean float64 `json:"mean"`
	P50  int64   `json:"p50"`
	P90  int64   `json:"p90"`
	P99  int64   `json:"p99"`
	P999 int64   `json:"p999"`
	Max  int64   `json:"max"`
}

// Summarize digests h.
func (h *Hist) Summarize() Summary {
	s := Summary{N: h.N, Max: h.Max, P50: h.Quantile(0.5), P90: h.Quantile(0.9), P99: h.Quantile(0.99), P999: h.Quantile(0.999)}
	if h.N > 0 {
		s.Mean = float64(h.Sum) / float64(h.N)
	}
	return s
}
