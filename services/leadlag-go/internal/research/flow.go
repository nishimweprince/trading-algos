package research

import "math"

// DetectorCfg defines a leader flow event (PREREG 1.4).
type DetectorCfg struct {
	WMs        int64   `json:"w_ms"`
	Theta      float64 `json:"theta"`
	CooldownMs int64   `json:"cooldown_ms"`
	HalfLifeMs int64   `json:"half_life_ms"`
	WarmupMs   int64   `json:"warmup_ms"`
	MaxGapMs   int64   `json:"max_gap_ms"`
}

// PrimaryDetector is the pre-registered event definition.
func PrimaryDetector() DetectorCfg {
	return DetectorCfg{WMs: 500, Theta: 3, CooldownMs: 1000, HalfLifeMs: 30 * 60_000, WarmupMs: 30 * 60_000, MaxGapMs: 10 * 60_000}
}

type flowItem struct {
	t int64
	v float64
}

// Detector tracks one leader's rolling signed flow and fires on threshold crossings.
type Detector struct {
	cfg   DetectorCfg
	alpha float64 // EWMA weight per W bucket

	q        []flowItem // trades in (t−W, t], oldest first (head at qi)
	qi       int
	sum      float64
	bucketAt int64 // start of the current grid bucket
	bucket   float64
	varEWMA  float64
	weight   float64 // 1 − (1−α)^buckets, for EWMA bias correction
	warmTill int64
	lastT    int64
	lastFire int64
	above    bool
}

// NewDetector builds a detector.
func NewDetector(cfg DetectorCfg) *Detector {
	return &Detector{cfg: cfg, alpha: 1 - math.Exp(-math.Ln2*float64(cfg.WMs)/float64(cfg.HalfLifeMs)), lastFire: math.MinInt64 / 2}
}

// Event is a fired leader event.
type Event struct {
	T    int64   // leader trade time that crossed the threshold (ms)
	Dir  int8    // +1 buy flow, −1 sell flow
	Z    float64 // flow / scale at the crossing
	Flow float64 // signed USDT notional in the window
}

// OnTrade adds a leader trade (time ms, signed USDT notional) and reports a
// crossing. Trades must arrive in non-decreasing time.
func (d *Detector) OnTrade(t int64, signed float64) (Event, bool) {
	if d.lastT == 0 || t-d.lastT > d.cfg.MaxGapMs {
		d.reset(t)
	}
	d.lastT = t
	d.advanceGrid(t)
	d.bucket += signed

	d.q = append(d.q, flowItem{t, signed})
	d.sum += signed
	for d.qi < len(d.q) && d.q[d.qi].t <= t-d.cfg.WMs {
		d.sum -= d.q[d.qi].v
		d.qi++
	}
	if d.qi > 4096 && d.qi*2 > len(d.q) {
		n := copy(d.q, d.q[d.qi:])
		d.q, d.qi = d.q[:n], 0
	}

	if t < d.warmTill || d.varEWMA <= 0 {
		return Event{}, false
	}
	z := d.sum / math.Sqrt(d.varEWMA/d.weight)
	hit := math.Abs(z) >= d.cfg.Theta
	fire := hit && !d.above && t-d.lastFire >= d.cfg.CooldownMs
	d.above = hit
	if !fire {
		return Event{}, false
	}
	d.lastFire = t
	dir := int8(1)
	if z < 0 {
		dir = -1
	}
	return Event{T: t, Dir: dir, Z: z, Flow: d.sum}, true
}

func (d *Detector) reset(t int64) {
	d.q, d.qi, d.sum = d.q[:0], 0, 0
	d.bucketAt = t - t%d.cfg.WMs
	d.bucket, d.varEWMA, d.weight = 0, 0, 0
	d.warmTill = t + d.cfg.WarmupMs
	d.above = false
}

// advanceGrid closes every W bucket that ended at or before t, counting empty ones.
func (d *Detector) advanceGrid(t int64) {
	for t >= d.bucketAt+d.cfg.WMs {
		d.varEWMA += d.alpha * (d.bucket*d.bucket - d.varEWMA)
		d.weight += d.alpha * (1 - d.weight)
		d.bucket = 0
		d.bucketAt += d.cfg.WMs
	}
}
