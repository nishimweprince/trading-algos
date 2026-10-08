package research

import (
	"encoding/json"
	"math"
	"math/rand/v2"
	"sort"
)

// Summarize pools per-day stats into a mean with a day-block bootstrap CI
// (2.5/97.5 percentiles of the pooled mean over `boot` resamples of days) and a
// day-clustered t-statistic (PREREG 1.5).
func Summarize(days []DayStat, boot int, seed uint64) Summary {
	var used []DayStat
	var n int64
	var sum, sumsq float64
	for _, d := range days {
		if d.N > 0 {
			used = append(used, d)
			n += d.N
			sum += d.Sum
			sumsq += d.SumSq
		}
	}
	out := Summary{N: n, Days: len(used)}
	if n == 0 {
		out.Mean, out.Lo, out.Hi, out.TStat = math.NaN(), math.NaN(), math.NaN(), math.NaN()
		return out
	}
	mean := sum / float64(n)
	out.Mean = mean
	out.SDbp = math.Sqrt(math.Max(sumsq/float64(n)-mean*mean, 0))
	// CR0 cluster-robust standard error of the pooled mean.
	var v float64
	for _, d := range used {
		r := d.Sum - mean*float64(d.N)
		v += r * r
	}
	out.TStat = math.NaN()
	if se := math.Sqrt(v) / float64(n); se > 0 && len(used) >= 2 {
		out.TStat = mean / se
	}
	if boot <= 0 || len(used) < 2 {
		out.Lo, out.Hi = math.NaN(), math.NaN()
		return out
	}
	r := rand.New(rand.NewPCG(seed, 0x6c6c))
	means := make([]float64, 0, boot)
	for range boot {
		var bn int64
		var bs float64
		for range used {
			d := used[r.IntN(len(used))]
			bn += d.N
			bs += d.Sum
		}
		if bn > 0 {
			means = append(means, bs/float64(bn))
		}
	}
	sort.Float64s(means)
	out.Lo = means[int(0.025*float64(len(means)-1))]
	out.Hi = means[int(math.Ceil(0.975*float64(len(means)-1)))]
	return out
}

// MarshalJSON writes NaN (no CI: fewer than two days) as null.
func (s Summary) MarshalJSON() ([]byte, error) {
	num := func(v float64) any {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil
		}
		return v
	}
	return json.Marshal(map[string]any{"n": s.N, "days": s.Days, "mean_bp": num(s.Mean), "ci_lo_bp": num(s.Lo),
		"ci_hi_bp": num(s.Hi), "t_day_clustered": num(s.TStat), "sd_bp": num(s.SDbp)})
}

// UnmarshalJSON reads null back as NaN.
func (s *Summary) UnmarshalJSON(b []byte) error {
	var m struct {
		N     int64    `json:"n"`
		Days  int      `json:"days"`
		Mean  *float64 `json:"mean_bp"`
		Lo    *float64 `json:"ci_lo_bp"`
		Hi    *float64 `json:"ci_hi_bp"`
		TStat *float64 `json:"t_day_clustered"`
		SDbp  *float64 `json:"sd_bp"`
	}
	if err := json.Unmarshal(b, &m); err != nil {
		return err
	}
	get := func(p *float64) float64 {
		if p == nil {
			return math.NaN()
		}
		return *p
	}
	*s = Summary{N: m.N, Days: m.Days, Mean: get(m.Mean), Lo: get(m.Lo), Hi: get(m.Hi), TStat: get(m.TStat), SDbp: get(m.SDbp)}
	return nil
}
