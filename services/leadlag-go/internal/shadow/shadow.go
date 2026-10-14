// Package shadow is the shadow trader (explore-4, research/PREREG.md Part 5):
// a leader's price move signals post-only entries in the followers that lag
// it, filled against the live book by a queue-position model, and passive
// exits. Nothing is sent to an exchange.
//
// The trigger fires when a leader's mid moves at least min_move_bp within
// window_ms; the top_k followers that lag the leader most by β-adjusted gap
// get an entry order (quoter.go). Orders are simulated post-only limits with a
// conservative queue model (queue.go).
//
// The engine runs inside the hub (under its lock) like the record engine: it
// never blocks, never touches the disk and keeps its state in fixed slices.
// Every decision depends only on the events and their receive times, so a
// replay of the tape reproduces every shadow trade (leadlag --replay
// --shadow-trades).
package shadow

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"sync/atomic"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

// TriggerCfg is the price trigger.
type TriggerCfg struct {
	WindowMs   int64   `json:"window_ms"`   // the look-back of the leader move
	MinMoveBp  float64 `json:"min_move_bp"` // |leader mid move| that fires
	CooldownMs int64   `json:"cooldown_ms"` // per leader
	MinGapBp   float64 `json:"min_gap_bp"`  // a follower must lag the leader by at least this much
	// Beta: "one" (the default: gap = leader move − follower move) or
	// "rolling": gap = β·leader move − follower move, with β an EWMA
	// regression of the follower's 1 s mid returns on the leader's (beta.go).
	Beta           string `json:"beta,omitempty"`
	BetaHalfLifeMs int64  `json:"beta_half_life_ms,omitempty"` // default 30 min
	BetaWarmupMs   int64  `json:"beta_warmup_ms,omitempty"`    // default 30 min
	// StrongMoveBp > 0: the move must reach it, or reach min_move_bp with a
	// same-direction leader liquidation (forceOrder) of at least liq_min_usd
	// within liq_window_ms.
	StrongMoveBp float64 `json:"strong_move_bp,omitempty"`
	// LiqWindowMs > 0: a leader liquidation of at least liq_min_usd against
	// the move within the window skips the signal (liq_opposite).
	LiqWindowMs int64   `json:"liq_window_ms,omitempty"`
	LiqMinUSD   float64 `json:"liq_min_usd,omitempty"`
}

func (t *TriggerCfg) rollingBeta() bool { return t.Beta == "rolling" }

// MaxMarkouts bounds markouts_ms (the slot keeps them in a fixed array).
const MaxMarkouts = 8

// Params fix the shadow run (research/PREREG.md Part 5).
type Params struct {
	Run         string      `json:"run"`
	Exploratory bool        `json:"exploratory"`
	Leaders     []string    `json:"leaders"`
	Followers   []string    `json:"followers"`
	Trigger     *TriggerCfg `json:"trigger"`
	// TopK: how many lagging followers a signal posts in; 0: every one.
	TopK int `json:"top_k,omitempty"`
	// LatencyMs: decision to exchange, for every post, reprice and cancel.
	LatencyMs int64 `json:"latency_ms"`
	// HorizonMs: the exit starts this long after the entry fill.
	HorizonMs     int64   `json:"horizon_ms"`
	MarkoutsMs    []int64 `json:"markouts_ms,omitempty"`
	NotionalUSD   float64 `json:"notional_usd"`
	TakerFeeBp    float64 `json:"taker_fee_bp"`
	MaxQuoteAgeMs int64   `json:"max_quote_age_ms"`
	// Ticks: each follower's tick size, E8 (llfetch ticks).
	Ticks  map[string]int64 `json:"ticks"`
	Quoter *QuoterCfg       `json:"quoter"`
}

// Validate checks that the parameters describe a runnable experiment.
func (p *Params) Validate() error {
	switch {
	case p.Run == "":
		return errors.New("shadow: run label is empty")
	case len(p.Leaders) == 0 || len(p.Followers) == 0:
		return errors.New("shadow: leaders and followers are required")
	case p.LatencyMs < 0 || p.HorizonMs <= 0:
		return errors.New("shadow: latency_ms >= 0 and horizon_ms > 0 are required")
	case p.NotionalUSD <= 0 || p.TakerFeeBp < 0 || p.MaxQuoteAgeMs <= 0:
		return errors.New("shadow: notional_usd > 0, taker_fee_bp >= 0 and max_quote_age_ms > 0 are required")
	case p.TopK < 0:
		return errors.New("shadow: top_k must be >= 0")
	case p.Trigger == nil || p.Quoter == nil:
		return errors.New("shadow: trigger and quoter params are required")
	}
	t := p.Trigger
	if t.WindowMs <= 0 || t.MinMoveBp <= 0 || t.CooldownMs < 0 {
		return errors.New("shadow: trigger needs window_ms > 0, min_move_bp > 0, cooldown_ms >= 0")
	}
	switch t.Beta {
	case "", "one", "rolling":
	default:
		return fmt.Errorf("shadow: trigger beta %q (want one or rolling)", t.Beta)
	}
	if t.BetaHalfLifeMs < 0 || t.BetaWarmupMs < 0 || t.LiqWindowMs < 0 || t.LiqMinUSD < 0 {
		return errors.New("shadow: beta_half_life_ms, beta_warmup_ms, liq_window_ms and liq_min_usd must be >= 0")
	}
	if t.StrongMoveBp != 0 && (t.StrongMoveBp < t.MinMoveBp || t.LiqWindowMs <= 0) {
		return errors.New("shadow: strong_move_bp needs strong_move_bp >= min_move_bp and liq_window_ms > 0")
	}
	if err := p.Quoter.validate(p); err != nil {
		return err
	}
	if len(p.MarkoutsMs) > MaxMarkouts {
		return fmt.Errorf("shadow: at most %d markouts_ms", MaxMarkouts)
	}
	for i, m := range p.MarkoutsMs {
		// Markouts keep measuring after the exit, so they may outlast the horizon.
		if m <= 0 || (i > 0 && m <= p.MarkoutsMs[i-1]) {
			return errors.New("shadow: markouts_ms must be ascending and > 0")
		}
	}
	return nil
}

// LoadParams reads and validates a params file; it also returns the file's
// sha256. Unknown fields are an error, so a params file written for an older
// engine fails instead of running with half its settings.
func LoadParams(path string) (Params, string, error) {
	var p Params
	b, err := os.ReadFile(path)
	if err != nil {
		return p, "", err
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		return p, "", fmt.Errorf("shadow: %s: %w", path, err)
	}
	if err := p.Validate(); err != nil {
		return p, "", err
	}
	sum := sha256.Sum256(b)
	return p, hex.EncodeToString(sum[:]), nil
}

// Trade is one completed shadow round trip.
type Trade struct {
	Kind        string  `json:"kind"` // "trade"
	Run         string  `json:"run"`
	Exploratory bool    `json:"exploratory"`
	Leader      string  `json:"leader"`
	Follower    string  `json:"follower"`
	Dir         int8    `json:"dir"`         // +1 long, −1 short
	Z           float64 `json:"z"`           // the signed leader move in bp
	LeaderTMs   int64   `json:"leader_t_ms"` // the leader quote's exchange time
	SignalNs    int64   `json:"signal_recv_ns"`
	EntryNs     int64   `json:"entry_recv_ns"`
	ExitNs      int64   `json:"exit_recv_ns"`
	EntryPx     float64 `json:"entry_px"`
	ExitPx      float64 `json:"exit_px"`
	SpreadBp    float64 `json:"entry_spread_bp"`
	Qty         float64 `json:"qty"`
	NotionalUSD float64 `json:"notional_usd"`
	GrossBp     float64 `json:"gross_bp"`
	FeesBp      float64 `json:"fees_bp"`
	NetBp       float64 `json:"net_bp"`
	NetUSD      float64 `json:"net_usd"`
	ExitKind    string  `json:"exit_kind"` // "taker" | "maker"
	// ExitReason: why the exit started: timeout (the horizon), gap_closed,
	// target (the resting target filled), stop, stop_soft (the soft stop moved
	// the exit to the touch).
	ExitReason string `json:"exit_reason,omitempty"`
	// |leader move| over the window, the follower's own signed move over it,
	// and the gap (leader − follower), all in bp.
	LeaderMoveBp float64 `json:"leader_move_bp,omitempty"`
	PreMoveBp    float64 `json:"pre_move_bp,omitempty"`
	GapBp        float64 `json:"gap_bp,omitempty"`
	// MarkoutsBp: the follower mid at each markouts_ms after the entry fill,
	// signed, bp from the fill price.
	MarkoutsBp []float64 `json:"markouts_bp,omitempty"`
	// Beta: the β used for the gap (rolling β only).
	Beta       float64 `json:"beta,omitempty"`
	EntryKind  string  `json:"entry_kind,omitempty"` // "maker"
	EntryImb   float64 `json:"entry_imb"`            // our side's L1 share when the entry was posted
	QueueAhead float64 `json:"queue_ahead,omitempty"`
	FillWaitMs float64 `json:"fill_wait_ms,omitempty"` // post → entry fill
	// ExitMarkoutsBp: the mid at each markouts_ms after the exit, bp from the
	// exit price, signed for the exit order (positive: the exit was not picked off).
	ExitMarkoutsBp []float64 `json:"exit_markouts_bp,omitempty"`
	// RecNs: when the record was emitted (after the last markout).
	RecNs int64 `json:"rec_ns,omitempty"`
}

// Agg accumulates trades for one group (overall, a follower, a leader).
type Agg struct {
	N       int                  `json:"n"`
	Wins    int                  `json:"wins"`
	SumNet  float64              `json:"sum_net_bp"`
	SumNet2 float64              `json:"-"`
	SumGros float64              `json:"sum_gross_bp"`
	NetUSD  float64              `json:"net_usd"`
	SumMark [MaxMarkouts]float64 `json:"-"`
	NMark   int                  `json:"-"` // markouts per trade (the run's len(markouts_ms))
}

func (a *Agg) add(t *Trade) {
	a.N++
	if t.NetBp > 0 {
		a.Wins++
	}
	a.SumNet += t.NetBp
	a.SumNet2 += t.NetBp * t.NetBp
	a.SumGros += t.GrossBp
	a.NetUSD += t.NetUSD
	for i, m := range t.MarkoutsBp {
		a.SumMark[i] += m
	}
	if len(t.MarkoutsBp) > a.NMark {
		a.NMark = len(t.MarkoutsBp)
	}
}

// Summary is an Agg's derived statistics.
type Summary struct {
	N         int       `json:"n"`
	MeanGross float64   `json:"mean_gross_bp"`
	MeanNet   float64   `json:"mean_net_bp"`
	HitRate   float64   `json:"hit_rate"`
	TStat     float64   `json:"t_stat"`
	NetUSD    float64   `json:"net_usd"`
	Markouts  []float64 `json:"mean_markouts_bp,omitempty"`
}

// Summarize derives means, hit rate and the t-statistic of net bp.
func (a *Agg) Summarize() Summary {
	s := Summary{N: a.N, NetUSD: round(a.NetUSD, 4)}
	if a.N == 0 {
		return s
	}
	n := float64(a.N)
	mean := a.SumNet / n
	s.MeanNet, s.MeanGross = round(mean, 3), round(a.SumGros/n, 3)
	s.HitRate = round(float64(a.Wins)/n, 4)
	if a.N > 1 {
		v := (a.SumNet2 - n*mean*mean) / (n - 1)
		if v > 0 {
			s.TStat = round(mean/math.Sqrt(v/n), 3)
		}
	}
	for i := 0; i < a.NMark; i++ {
		s.Markouts = append(s.Markouts, round(a.SumMark[i]/n, 3))
	}
	return s
}

// round rounds v to d decimals. NaN and ±Inf become 0: every float in the
// status document and the journal goes through here, and encoding/json
// refuses non-finite numbers (one froze /v1/status on 2026-10-10).
func round(v float64, d int) float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return 0
	}
	p := math.Pow(10, float64(d))
	return math.Round(v*p) / p
}

type quote struct {
	bid, ask       int64 // E8
	bidQty, askQty int64 // E8
	recvNs         int64
}

func (q *quote) mid() float64 { return float64(q.bid+q.ask) / 2 / model.Scale }

// histCap bounds each symbol's mid history. Only mid changes are kept, so
// this covers far more than any trigger window even in a burst.
const histCap = 1 << 16

// hist is a ring of (receive time, mid), oldest first, one entry per mid change.
type hist struct {
	ns   []int64
	mid  []float64
	head int // index of the oldest entry
	n    int
}

func newHist() hist { return hist{ns: make([]int64, histCap), mid: make([]float64, histCap)} }

func (h *hist) push(ns int64, mid float64) {
	if h.n > 0 && h.mid[(h.head+h.n-1)%histCap] == mid {
		return
	}
	if h.n < histCap {
		i := (h.head + h.n) % histCap
		h.ns[i], h.mid[i] = ns, mid
		h.n++
		return
	}
	h.ns[h.head], h.mid[h.head] = ns, mid
	h.head = (h.head + 1) % histCap
}

// at returns the mid in force at time t: the latest entry with ns ≤ t.
func (h *hist) at(t int64) (float64, bool) {
	if h.n == 0 || h.ns[h.head] > t {
		return 0, false
	}
	lo, hi := 0, h.n-1 // invariant: entry lo has ns ≤ t
	for lo < hi {
		m := (lo + hi + 1) / 2
		if h.ns[(h.head+m)%histCap] <= t {
			lo = m
		} else {
			hi = m - 1
		}
	}
	return h.mid[(h.head+lo)%histCap], true
}

func (h *hist) last() (float64, bool) {
	if h.n == 0 {
		return 0, false
	}
	return h.mid[(h.head+h.n-1)%histCap], true
}

// RingSize is how many recent trades the status page shows.
const RingSize = 50

// RollN is the rolling window the status reads the kill-rule numbers from:
// the latest RollN trades' mean net bp and taker-exit share.
const RollN = 200

// moveBuckets label the by_move groups: |leader move| in bp.
var moveBuckets = []float64{4, 8, 15}

// Engine is the shadow engine for one run's symbol list.
type Engine struct {
	p      Params
	halted *atomic.Bool // nil: never halted (replay)
	sink   func(*Trade) // called for every completed trade (replay writes them); may be nil

	leaderOf   []int // symbol index → leader index, −1 if not a leader
	followerOf []int // symbol index → follower index, −1
	leaders    []string
	followers  []string
	quotes     []quote // per follower
	active     int     // followers with an entry or a position

	lhist    []hist // per leader
	fhist    []hist // per follower
	above    []bool
	lastFire []int64
	cand     []int     // scratch: candidate followers
	candGap  []float64 // scratch: their gaps
	candPre  []float64
	candBase []gapBase
	betas    *betaSet   // rolling β; nil with beta "one"
	liqNs    [][2]int64 // per leader: last qualifying liquidation receive time, [sell, buy]

	q *quoterState

	Signals   uint64
	Skipped   map[string]uint64
	Total     Agg
	ByFollow  []Agg
	ByLeader  []Agg
	ByExit    [2]Agg // taker, maker
	ByMove    []Agg  // len(moveBuckets)+1
	ByReason  [len(exitReasons)]Agg
	Ring      [RingSize]Trade
	RingNext  int
	RingCount int

	roll      [RollN]float64 // the latest trades' net bp
	rollTaker [RollN]bool
	rollNext  int
	rollCount int
	// Crosses counts taker exits by exit reason (stop: the hard stop;
	// timeout: the horizon's grace ran out).
	Crosses [len(exitReasons)]uint64
}

// New builds an engine for symbols (the hub's index order). Leaders and
// followers missing from symbols are ignored; halted may be nil.
func New(p Params, symbols []string, halted *atomic.Bool, sink func(*Trade)) *Engine {
	e := &Engine{p: p, halted: halted, sink: sink, Skipped: map[string]uint64{}}
	e.leaderOf = make([]int, len(symbols))
	e.followerOf = make([]int, len(symbols))
	isLeader, isFollower := map[string]bool{}, map[string]bool{}
	for _, s := range p.Leaders {
		isLeader[s] = true
	}
	for _, s := range p.Followers {
		isFollower[s] = true
	}
	for i, s := range symbols {
		e.leaderOf[i], e.followerOf[i] = -1, -1
		switch {
		case isLeader[s]:
			e.leaderOf[i] = len(e.leaders)
			e.leaders = append(e.leaders, s)
			e.lhist = append(e.lhist, newHist())
		case isFollower[s]:
			e.followerOf[i] = len(e.followers)
			e.followers = append(e.followers, s)
			e.fhist = append(e.fhist, newHist())
		}
	}
	e.quotes = make([]quote, len(e.followers))
	e.ByFollow = make([]Agg, len(e.followers))
	e.ByLeader = make([]Agg, len(e.leaders))
	e.above = make([]bool, len(e.leaders))
	e.lastFire = make([]int64, len(e.leaders))
	for i := range e.lastFire {
		e.lastFire[i] = math.MinInt64 / 2
	}
	e.cand = make([]int, 0, len(e.followers))
	e.candGap = make([]float64, len(e.followers))
	e.candPre = make([]float64, len(e.followers))
	e.ByMove = make([]Agg, len(moveBuckets)+1)
	e.candBase = make([]gapBase, len(e.followers))
	e.liqNs = make([][2]int64, len(e.leaders))
	for i := range e.liqNs {
		e.liqNs[i] = [2]int64{math.MinInt64 / 2, math.MinInt64 / 2}
	}
	if p.Trigger.rollingBeta() {
		e.betas = newBetaSet(len(e.leaders), len(e.followers), p.Trigger.BetaHalfLifeMs, p.Trigger.BetaWarmupMs)
	}
	for _, k := range []string{"halted", "busy", "no_quote", "stale_quote", "crossed", "wide_spread", "no_lagger",
		"beta_warmup", "liq_opposite"} {
		e.Skipped[k] = 0 // preallocated: OnEvent never grows the map
	}
	e.q = newQuoterState(e)
	return e
}

// exitReasons index ByReason.
var exitReasons = [...]string{"timeout", "gap_closed", "target", "stop", "stop_soft"}

func reasonIndex(r string) int {
	for i, x := range exitReasons {
		if x == r {
			return i
		}
	}
	return 0
}

// Run is the run label.
func (e *Engine) Run() string { return e.p.Run }

// Followers and Leaders name the engine's groups in index order.
func (e *Engine) Followers() []string { return e.followers }
func (e *Engine) Leaders() []string   { return e.leaders }

// OnEvent implements live.Engine.
func (e *Engine) OnEvent(ev *model.Event) {
	if e.betas != nil {
		e.betas.tick(ev.RecvNs, e.lhist, e.fhist)
	}
	e.quoterEvent(ev)
}

// onLeaderLiq records a leader liquidation of at least liq_min_usd.
func (e *Engine) onLeaderLiq(l int, now int64, ev *model.Event) {
	if e.p.Trigger.LiqWindowMs <= 0 {
		return
	}
	usd := float64(ev.Px) / model.Scale * float64(ev.Qty) / model.Scale
	if usd < e.p.Trigger.LiqMinUSD {
		return
	}
	side := 0 // sell
	if ev.TakerSign() > 0 {
		side = 1
	}
	e.liqNs[l][side] = now
}

// liqWithin reports a qualifying liquidation in direction dir within the window.
func (e *Engine) liqWithin(l int, now int64, dir int8) bool {
	side := 0
	if dir > 0 {
		side = 1
	}
	return now-e.liqNs[l][side] <= e.p.Trigger.LiqWindowMs*1_000_000
}

// trigger runs the price trigger on a leader quote: the leader's mid moved at
// least min_move_bp over window_ms (crossing up from below, after the
// cooldown). With strong_move_bp, a move counts only at strong_move_bp, or at
// min_move_bp with a same-direction liquidation in the window. It returns the
// signed move in bp.
func (e *Engine) trigger(l int, now int64, mid float64) (move float64, fire bool) {
	tc := e.p.Trigger
	h := &e.lhist[l]
	h.push(now, mid)
	past, ok := h.at(now - tc.WindowMs*1_000_000)
	if !ok || past <= 0 {
		return 0, false
	}
	move = (mid - past) / past * 1e4
	hit := math.Abs(move) >= tc.MinMoveBp
	if hit && tc.StrongMoveBp > 0 && math.Abs(move) < tc.StrongMoveBp {
		dir := int8(1)
		if move < 0 {
			dir = -1
		}
		hit = e.liqWithin(l, now, dir)
	}
	fire = hit && !e.above[l] && now-e.lastFire[l] >= tc.CooldownMs*1_000_000
	e.above[l] = hit
	if fire {
		e.lastFire[l] = now
	}
	return move, fire
}

// laggers fills e.cand with the followers that lag leader l by at least
// min_gap_bp, by gap descending (ties keep follower order), and returns the
// signal's direction. free reports whether a follower can take a position
// (the caller counts busy skips). Followers whose rolling β is still warming
// up are skipped.
func (e *Engine) laggers(l int, now int64, move float64, free func(f int) bool) int8 {
	tc := e.p.Trigger
	win := tc.WindowMs * 1_000_000
	dir := int8(1)
	if move < 0 {
		dir = -1
	}
	d := float64(dir)
	lWas, _ := e.lhist[l].at(now - win)
	e.cand = e.cand[:0]
	for f := range e.followers {
		if !free(f) {
			continue
		}
		fh := &e.fhist[f]
		cur, ok1 := fh.last()
		was, ok2 := fh.at(now - win)
		if !ok1 || !ok2 || was <= 0 {
			continue
		}
		beta := 1.0
		if e.betas != nil {
			b, warm := e.betas.beta(l, f)
			if !warm {
				e.Skipped["beta_warmup"]++
				continue
			}
			beta = b
		}
		pre := d * (cur - was) / was * 1e4
		gap := d*move - pre
		if e.betas != nil {
			gap = d*beta*move - pre
		}
		if gap < tc.MinGapBp {
			continue
		}
		e.candGap[f], e.candPre[f] = gap, pre
		e.candBase[f] = gapBase{leader: lWas, follower: was, beta: beta}
		// insertion by gap, descending; ties keep follower order
		i := len(e.cand)
		e.cand = append(e.cand, f)
		for i > 0 && e.candGap[e.cand[i-1]] < gap {
			e.cand[i] = e.cand[i-1]
			i--
		}
		e.cand[i] = f
	}
	return dir
}

func sign(v float64) int8 {
	if v < 0 {
		return -1
	}
	return 1
}

// record adds a finished trade to the ring and the aggregates and sends it to the sink.
func (e *Engine) record(tr *Trade, leader, f int, maker bool, move float64) {
	t := &e.Ring[e.RingNext]
	*t = *tr
	e.RingNext = (e.RingNext + 1) % RingSize
	if e.RingCount < RingSize {
		e.RingCount++
	}
	e.Total.add(t)
	e.ByFollow[f].add(t)
	if leader >= 0 {
		e.ByLeader[leader].add(t)
	}
	if maker {
		e.ByExit[1].add(t)
	} else {
		e.ByExit[0].add(t)
	}
	e.ByReason[reasonIndex(t.ExitReason)].add(t)
	e.roll[e.rollNext], e.rollTaker[e.rollNext] = t.NetBp, !maker
	e.rollNext = (e.rollNext + 1) % RollN
	if e.rollCount < RollN {
		e.rollCount++
	}
	if !maker {
		e.Crosses[reasonIndex(t.ExitReason)]++
	}
	b := 0
	for b < len(moveBuckets) && move >= moveBuckets[b] {
		b++
	}
	e.ByMove[b].add(t)
	if e.sink != nil {
		e.sink(t)
	}
}

// Open reports how many followers have an entry order or a position.
func (e *Engine) Open() int { return e.active }

// Recent returns the ring's trades, newest first (call under the hub lock).
func (e *Engine) Recent() []Trade {
	out := make([]Trade, 0, e.RingCount)
	for i := 1; i <= e.RingCount; i++ {
		out = append(out, e.Ring[(e.RingNext-i+RingSize)%RingSize])
	}
	return out
}

// moveLabels names the by_move buckets.
func moveLabels() []string {
	out := make([]string, 0, len(moveBuckets)+1)
	lo := 0.0
	for _, b := range moveBuckets {
		out = append(out, fmt.Sprintf("move bp %g–%g", lo, b))
		lo = b
	}
	return append(out, fmt.Sprintf("move bp %g+", lo))
}

// Status is the engine's state for the status page (call under the hub lock).
func (e *Engine) Status(paramsSHA string) map[string]any {
	byF := map[string]Summary{}
	for i, name := range e.followers {
		byF[name] = e.ByFollow[i].Summarize()
	}
	byL := map[string]Summary{}
	for i, name := range e.leaders {
		byL[name] = e.ByLeader[i].Summarize()
	}
	byM := make([]map[string]any, 0, len(e.ByMove))
	for i, label := range moveLabels() {
		byM = append(byM, map[string]any{"bucket": label, "summary": e.ByMove[i].Summarize()})
	}
	skipped := make(map[string]uint64, len(e.Skipped))
	for k, v := range e.Skipped {
		skipped[k] = v
	}
	byR := map[string]Summary{}
	for i, r := range exitReasons {
		if e.ByReason[i].N > 0 {
			byR[r] = e.ByReason[i].Summarize()
		}
	}
	takerFrac := 0.0
	if e.Total.N > 0 {
		takerFrac = round(float64(e.ByExit[0].N)/float64(e.Total.N), 4)
	}
	crosses := map[string]uint64{}
	for i, r := range exitReasons {
		if e.Crosses[i] > 0 {
			crosses[r] = e.Crosses[i]
		}
	}
	var rollSum float64
	var rollTakers int
	for i := 0; i < e.rollCount; i++ {
		rollSum += e.roll[i]
		if e.rollTaker[i] {
			rollTakers++
		}
	}
	rolling := map[string]any{"n": e.rollCount, "window": RollN}
	if e.rollCount > 0 {
		rolling["mean_net_bp"] = round(rollSum/float64(e.rollCount), 3)
		rolling["taker_frac"] = round(float64(rollTakers)/float64(e.rollCount), 4)
	}
	doc := map[string]any{
		"run": e.p.Run, "exploratory": e.p.Exploratory, "params_sha256": paramsSHA, "params": e.p,
		"markouts_ms": e.p.MarkoutsMs,
		"signals":     e.Signals, "open": e.active, "skipped": skipped,
		"total": e.Total.Summarize(), "by_follower": byF, "by_leader": byL,
		"by_exit":        map[string]Summary{"taker": e.ByExit[0].Summarize(), "maker": e.ByExit[1].Summarize()},
		"by_exit_reason": byR, "taker_exit_frac": takerFrac, "taker_crosses": crosses, "rolling": rolling,
		"by_move": byM, "recent": e.Recent(),
	}
	e.q.status(doc)
	return doc
}
