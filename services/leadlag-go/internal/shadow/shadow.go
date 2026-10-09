// Package shadow is the shadow-trading engine: leader events open simulated
// taker positions in the followers, filled against the live bookTicker, and
// close them after a fixed horizon. Nothing is sent to an exchange.
//
// Two triggers exist. "flow" (explore-1, PREREG Part 2) is the pre-registered
// taker-flow detector and opens every follower. "price" (explore-2, PREREG
// Part 3) fires when a leader's mid moves at least min_move_bp within
// window_ms, and opens the top_k followers that lag the leader most. The exit
// is a taker cross, or ("maker") a limit at our own touch that fills only when
// the book trades through it, crossing as a taker after maker_timeout_ms.
//
// The engine runs inside the hub (under its lock) like the record engine: it
// never blocks, never touches the disk and keeps its state in fixed slices.
// Every decision depends only on the events and their receive times, so a
// replay of the tape reproduces every shadow trade (leadlag --replay
// --shadow-trades).
package shadow

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"sync/atomic"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/research"
)

// TriggerCfg selects the signal. Absent (nil) means the flow detector.
type TriggerCfg struct {
	Kind       string  `json:"kind"`        // "flow" | "price"
	WindowMs   int64   `json:"window_ms"`   // price: the look-back of the leader move
	MinMoveBp  float64 `json:"min_move_bp"` // price: |leader mid move| that fires
	CooldownMs int64   `json:"cooldown_ms"` // price: per leader
	MinGapBp   float64 `json:"min_gap_bp"`  // price: a follower must lag the leader by at least this much
}

// ExitCfg selects the exit. Absent (nil) means a taker exit.
type ExitCfg struct {
	Mode           string  `json:"mode"` // "taker" | "maker"
	MakerTimeoutMs int64   `json:"maker_timeout_ms"`
	MakerFeeBp     float64 `json:"maker_fee_bp"`
}

// MaxMarkouts bounds markouts_ms (the slot keeps them in a fixed array).
const MaxMarkouts = 8

// Params fix one shadow run (research/PREREG.md Part 2 for explore-1, Part 3 for explore-2).
type Params struct {
	Run         string               `json:"run"`
	Exploratory bool                 `json:"exploratory"`
	Leaders     []string             `json:"leaders"`
	Followers   []string             `json:"followers"`
	Detector    research.DetectorCfg `json:"detector"`
	Trigger     *TriggerCfg          `json:"trigger,omitempty"`
	// TopK: price trigger only; 0 opens every qualifying follower.
	TopK int `json:"top_k,omitempty"`
	// LatencyMs: the earliest fill after a signal's receipt (order to exchange).
	LatencyMs int64 `json:"latency_ms"`
	// HorizonMs: the exit, measured from the entry fill.
	HorizonMs        int64    `json:"horizon_ms"`
	Exit             *ExitCfg `json:"exit,omitempty"`
	MarkoutsMs       []int64  `json:"markouts_ms,omitempty"`
	NotionalUSD      float64  `json:"notional_usd"`
	TakerFeeBp       float64  `json:"taker_fee_bp"`
	MaxQuoteAgeMs    int64    `json:"max_quote_age_ms"`
	MaxEntrySpreadBp float64  `json:"max_entry_spread_bp,omitempty"`
}

func (p *Params) priceTrigger() bool { return p.Trigger != nil && p.Trigger.Kind == "price" }
func (p *Params) makerExit() bool    { return p.Exit != nil && p.Exit.Mode == "maker" }

// TriggerKind and ExitMode name the run's trigger and exit.
func (p *Params) TriggerKind() string {
	if p.priceTrigger() {
		return "price"
	}
	return "flow"
}
func (p *Params) ExitMode() string {
	if p.makerExit() {
		return "maker"
	}
	return "taker"
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
	case p.MaxEntrySpreadBp < 0 || p.TopK < 0:
		return errors.New("shadow: max_entry_spread_bp and top_k must be >= 0")
	}
	if t := p.Trigger; t != nil {
		switch t.Kind {
		case "flow":
		case "price":
			if t.WindowMs <= 0 || t.MinMoveBp <= 0 || t.CooldownMs < 0 {
				return errors.New("shadow: price trigger needs window_ms > 0, min_move_bp > 0, cooldown_ms >= 0")
			}
		default:
			return fmt.Errorf("shadow: trigger kind %q (want flow or price)", t.Kind)
		}
	}
	if !p.priceTrigger() {
		if p.Detector.WMs <= 0 || p.Detector.Theta <= 0 || p.Detector.HalfLifeMs <= 0 {
			return errors.New("shadow: detector w_ms, theta and half_life_ms must be > 0")
		}
		if p.TopK != 0 {
			return errors.New("shadow: top_k needs the price trigger")
		}
	}
	if x := p.Exit; x != nil {
		switch x.Mode {
		case "taker":
		case "maker":
			if x.MakerTimeoutMs <= 0 {
				return errors.New("shadow: maker exit needs maker_timeout_ms > 0")
			}
		default:
			return fmt.Errorf("shadow: exit mode %q (want taker or maker)", x.Mode)
		}
	}
	if len(p.MarkoutsMs) > MaxMarkouts {
		return fmt.Errorf("shadow: at most %d markouts_ms", MaxMarkouts)
	}
	for i, m := range p.MarkoutsMs {
		if m <= 0 || m > p.HorizonMs || (i > 0 && m <= p.MarkoutsMs[i-1]) {
			return errors.New("shadow: markouts_ms must be ascending, > 0 and <= horizon_ms")
		}
	}
	return nil
}

// LoadParams reads and validates a params file; it also returns the file's sha256.
func LoadParams(path string) (Params, string, error) {
	var p Params
	b, err := os.ReadFile(path)
	if err != nil {
		return p, "", err
	}
	if err := json.Unmarshal(b, &p); err != nil {
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
	Run         string  `json:"run"`
	Exploratory bool    `json:"exploratory"`
	Leader      string  `json:"leader"`
	Follower    string  `json:"follower"`
	Dir         int8    `json:"dir"`         // +1 long, −1 short
	Z           float64 `json:"z"`           // flow: z at the crossing; price: the signed leader move in bp
	LeaderTMs   int64   `json:"leader_t_ms"` // flow: leader trade time that crossed θ; price: leader quote time
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
	// Price trigger: |leader move| over the window, the follower's own signed
	// move over it, and the gap (leader − follower), all in bp.
	LeaderMoveBp float64 `json:"leader_move_bp,omitempty"`
	PreMoveBp    float64 `json:"pre_move_bp,omitempty"`
	GapBp        float64 `json:"gap_bp,omitempty"`
	// MarkoutsBp: the follower mid at each markouts_ms after entry, signed, bp from the entry mid.
	MarkoutsBp []float64 `json:"markouts_bp,omitempty"`
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

func round(v float64, d int) float64 {
	p := math.Pow(10, float64(d))
	return math.Round(v*p) / p
}

type quote struct {
	bid, ask int64 // E8
	recvNs   int64
}

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

type slot struct {
	state    uint8 // idle, pending, open, exiting
	dueNs    int64 // pending: earliest fill; open: exit time; exiting: maker timeout
	leader   int   // index into leaders
	dir      int8
	z        float64
	leaderT  int64
	signalNs int64
	entryNs  int64
	entryPx  float64
	entryMid float64
	spreadBp float64
	qty      float64
	limit    float64 // exiting: our resting exit price
	move     float64 // price trigger: |leader move|, follower pre-move, gap (bp)
	preMove  float64
	gap      float64
	nMarks   int
	marks    [MaxMarkouts]float64
}

const (
	idle = iota
	pending
	open
	exiting
)

// RingSize is how many recent trades the status page shows.
const RingSize = 50

// moveBuckets label the by_move groups: |leader move| in bp (price) or |z| (flow).
var (
	priceBuckets = []float64{4, 8, 15}
	flowBuckets  = []float64{4, 6, 10}
)

// Engine is the shadow engine for one run's symbol list.
type Engine struct {
	p      Params
	halted *atomic.Bool // nil: never halted (replay)
	sink   func(*Trade) // called for every completed trade (replay writes them); may be nil

	leaderOf   []int // symbol index → leader index, −1 if not a leader
	followerOf []int // symbol index → follower index, −1
	leaders    []string
	followers  []string
	det        []*research.Detector
	quotes     []quote // per follower
	slots      []slot  // per follower
	active     int     // slots not idle

	// price trigger
	lhist    []hist // per leader
	fhist    []hist // per follower
	above    []bool
	lastFire []int64
	cand     []int     // scratch: candidate followers
	candGap  []float64 // scratch: their gaps
	candPre  []float64

	buckets []float64

	Signals   uint64
	Skipped   map[string]uint64
	Total     Agg
	ByFollow  []Agg
	ByLeader  []Agg
	ByExit    [2]Agg // taker, maker
	ByMove    []Agg  // len(buckets)+1
	Ring      [RingSize]Trade
	RingNext  int
	RingCount int
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
	price := p.priceTrigger()
	for i, s := range symbols {
		e.leaderOf[i], e.followerOf[i] = -1, -1
		switch {
		case isLeader[s]:
			e.leaderOf[i] = len(e.leaders)
			e.leaders = append(e.leaders, s)
			if price {
				e.lhist = append(e.lhist, newHist())
			} else {
				e.det = append(e.det, research.NewDetector(p.Detector))
			}
		case isFollower[s]:
			e.followerOf[i] = len(e.followers)
			e.followers = append(e.followers, s)
			if price {
				e.fhist = append(e.fhist, newHist())
			}
		}
	}
	e.quotes = make([]quote, len(e.followers))
	e.slots = make([]slot, len(e.followers))
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
	e.buckets = flowBuckets
	if price {
		e.buckets = priceBuckets
	}
	e.ByMove = make([]Agg, len(e.buckets)+1)
	for _, k := range []string{"halted", "busy", "no_quote", "stale_quote", "crossed", "wide_spread", "no_lagger"} {
		e.Skipped[k] = 0 // preallocated: OnEvent never grows the map
	}
	return e
}

// Followers and Leaders name the engine's groups in index order.
func (e *Engine) Followers() []string { return e.followers }
func (e *Engine) Leaders() []string   { return e.leaders }

// OnEvent implements live.Engine.
func (e *Engine) OnEvent(ev *model.Event) {
	now := ev.RecvNs
	// Timers first: a fill uses the book as it was before this event arrived.
	if e.active > 0 {
		e.runTimers(now)
	}
	if f := e.followerOf[ev.Sym]; f >= 0 {
		switch ev.Kind {
		case model.KindQuote:
			e.quotes[f] = quote{bid: ev.Bid, ask: ev.Ask, recvNs: now}
			if e.fhist != nil && ev.Bid > 0 && ev.Ask > ev.Bid {
				e.fhist[f].push(now, float64(ev.Bid+ev.Ask)/2/model.Scale)
			}
			if s := &e.slots[f]; s.state == exiting {
				bid, ask := float64(ev.Bid)/model.Scale, float64(ev.Ask)/model.Scale
				if (s.dir > 0 && bid >= s.limit) || (s.dir < 0 && ask > 0 && ask <= s.limit) {
					e.closeSlot(f, s, now, s.limit, true)
				}
			}
		case model.KindTrade:
			if s := &e.slots[f]; s.state == exiting {
				px := float64(ev.Px) / model.Scale
				if (s.dir > 0 && px > s.limit) || (s.dir < 0 && px < s.limit) {
					e.closeSlot(f, s, now, s.limit, true)
				}
			}
		}
		return
	}
	l := e.leaderOf[ev.Sym]
	if l < 0 {
		return
	}
	if e.p.priceTrigger() {
		if ev.Kind == model.KindQuote && ev.Bid > 0 && ev.Ask > ev.Bid {
			e.onLeaderQuote(l, now, ev.ExchMs, float64(ev.Bid+ev.Ask)/2/model.Scale)
		}
		return
	}
	if ev.Kind != model.KindTrade {
		return
	}
	notional := float64(ev.Px) / model.Scale * float64(ev.Qty) / model.Scale
	sig, fired := e.det[l].OnTrade(ev.TradeMs, float64(ev.TakerSign())*notional)
	if !fired {
		return
	}
	e.Signals++
	if e.halted != nil && e.halted.Load() {
		e.Skipped["halted"]++
		return
	}
	due := now + e.p.LatencyMs*1_000_000
	for f := range e.slots {
		s := &e.slots[f]
		if s.state != idle {
			e.Skipped["busy"]++
			continue
		}
		*s = slot{state: pending, dueNs: due, leader: l, dir: sig.Dir, z: sig.Z, leaderT: sig.T, signalNs: now}
		e.active++
	}
}

// onLeaderQuote runs the price trigger: the leader's mid moved at least
// min_move_bp over window_ms (crossing up from below, after the cooldown).
func (e *Engine) onLeaderQuote(l int, now, exchMs int64, mid float64) {
	tc := e.p.Trigger
	h := &e.lhist[l]
	h.push(now, mid)
	win := tc.WindowMs * 1_000_000
	past, ok := h.at(now - win)
	if !ok || past <= 0 {
		return
	}
	move := (mid - past) / past * 1e4
	hit := math.Abs(move) >= tc.MinMoveBp
	fire := hit && !e.above[l] && now-e.lastFire[l] >= tc.CooldownMs*1_000_000
	e.above[l] = hit
	if !fire {
		return
	}
	e.lastFire[l] = now
	e.Signals++
	if e.halted != nil && e.halted.Load() {
		e.Skipped["halted"]++
		return
	}
	dir := int8(1)
	if move < 0 {
		dir = -1
	}
	d := float64(dir)
	// Candidates: idle followers that lag the leader by at least min_gap_bp.
	e.cand = e.cand[:0]
	for f := range e.slots {
		if e.slots[f].state != idle {
			e.Skipped["busy"]++
			continue
		}
		fh := &e.fhist[f]
		cur, ok1 := fh.last()
		was, ok2 := fh.at(now - win)
		if !ok1 || !ok2 || was <= 0 {
			continue
		}
		pre := d * (cur - was) / was * 1e4
		gap := d*move - pre
		if gap < tc.MinGapBp {
			continue
		}
		e.candGap[f], e.candPre[f] = gap, pre
		// insertion by gap, descending; ties keep follower order
		i := len(e.cand)
		e.cand = append(e.cand, f)
		for i > 0 && e.candGap[e.cand[i-1]] < gap {
			e.cand[i] = e.cand[i-1]
			i--
		}
		e.cand[i] = f
	}
	if len(e.cand) == 0 {
		e.Skipped["no_lagger"]++
		return
	}
	k := len(e.cand)
	if e.p.TopK > 0 && k > e.p.TopK {
		k = e.p.TopK
	}
	due := now + e.p.LatencyMs*1_000_000
	for _, f := range e.cand[:k] {
		e.slots[f] = slot{state: pending, dueNs: due, leader: l, dir: dir, z: round(move, 3), leaderT: exchMs, signalNs: now,
			move: math.Abs(move), preMove: e.candPre[f], gap: e.candGap[f]}
		e.active++
	}
}

func (e *Engine) runTimers(now int64) {
	maxAge := e.p.MaxQuoteAgeMs * 1_000_000
	for f := range e.slots {
		s := &e.slots[f]
		if s.state == idle {
			continue
		}
		q := e.quotes[f]
		if s.state == open && s.nMarks < len(e.p.MarkoutsMs) {
			for s.nMarks < len(e.p.MarkoutsMs) && now >= s.entryNs+e.p.MarkoutsMs[s.nMarks]*1_000_000 {
				mid := float64(q.bid+q.ask) / 2 / model.Scale
				s.marks[s.nMarks] = float64(s.dir) * (mid - s.entryMid) / s.entryMid * 1e4
				s.nMarks++
			}
		}
		if now < s.dueNs {
			continue
		}
		switch s.state {
		case pending:
			reason := ""
			switch {
			case q.recvNs == 0:
				reason = "no_quote"
			case now-q.recvNs > maxAge:
				reason = "stale_quote"
			case q.bid <= 0 || q.ask <= q.bid:
				reason = "crossed"
			}
			bid, ask := float64(q.bid)/model.Scale, float64(q.ask)/model.Scale
			spreadBp := (ask - bid) / ((ask + bid) / 2) * 1e4
			if reason == "" && e.p.MaxEntrySpreadBp > 0 && spreadBp > e.p.MaxEntrySpreadBp {
				reason = "wide_spread"
			}
			if reason != "" {
				e.Skipped[reason]++
				s.state = idle
				e.active--
				continue
			}
			px := ask
			if s.dir < 0 {
				px = bid
			}
			s.state, s.entryNs, s.entryPx, s.entryMid = open, now, px, (ask+bid)/2
			s.spreadBp = spreadBp
			s.qty = e.p.NotionalUSD / px
			s.dueNs = now + e.p.HorizonMs*1_000_000
		case open:
			if e.p.makerExit() && q.bid > 0 && q.ask > q.bid {
				// Rest a limit at our own touch: a long sells at the ask, a short buys at the bid.
				s.limit = float64(q.ask) / model.Scale
				if s.dir < 0 {
					s.limit = float64(q.bid) / model.Scale
				}
				s.state = exiting
				s.dueNs = now + e.p.Exit.MakerTimeoutMs*1_000_000
				continue
			}
			e.takerExit(f, s, now, q)
		case exiting:
			e.takerExit(f, s, now, q)
		}
	}
}

// takerExit closes at the opposite touch of the latest quote, even if it is
// old: the position must close.
func (e *Engine) takerExit(f int, s *slot, now int64, q quote) {
	px := float64(q.bid) / model.Scale
	if s.dir < 0 {
		px = float64(q.ask) / model.Scale
	}
	e.closeSlot(f, s, now, px, false)
}

func (e *Engine) closeSlot(f int, s *slot, now int64, exitPx float64, maker bool) {
	e.close(f, s, now, exitPx, maker)
	s.state = idle
	e.active--
}

func (e *Engine) close(f int, s *slot, now int64, exitPx float64, maker bool) {
	gross := float64(s.dir) * (exitPx - s.entryPx) / s.entryPx * 1e4
	fees, kind := 2*e.p.TakerFeeBp, "taker"
	if maker {
		fees, kind = e.p.TakerFeeBp+e.p.Exit.MakerFeeBp, "maker"
	}
	net := gross - fees
	var marks []float64
	if s.nMarks > 0 {
		marks = make([]float64, s.nMarks) // allocates once per trade, like the journal's marshalling
		for i := range marks {
			marks[i] = round(s.marks[i], 4)
		}
	}
	t := &e.Ring[e.RingNext]
	*t = Trade{
		Run: e.p.Run, Exploratory: e.p.Exploratory, Leader: e.leaders[s.leader], Follower: e.followers[f],
		Dir: s.dir, Z: round(s.z, 3), LeaderTMs: s.leaderT, SignalNs: s.signalNs, EntryNs: s.entryNs, ExitNs: now,
		EntryPx: s.entryPx, ExitPx: exitPx, SpreadBp: round(s.spreadBp, 3), Qty: s.qty, NotionalUSD: e.p.NotionalUSD,
		GrossBp: round(gross, 4), FeesBp: fees, NetBp: round(net, 4), NetUSD: round(net/1e4*e.p.NotionalUSD, 6),
		ExitKind: kind, LeaderMoveBp: round(s.move, 3), PreMoveBp: round(s.preMove, 3), GapBp: round(s.gap, 3),
		MarkoutsBp: marks,
	}
	e.RingNext = (e.RingNext + 1) % RingSize
	if e.RingCount < RingSize {
		e.RingCount++
	}
	e.Total.add(t)
	e.ByFollow[f].add(t)
	e.ByLeader[s.leader].add(t)
	if maker {
		e.ByExit[1].add(t)
	} else {
		e.ByExit[0].add(t)
	}
	size := math.Abs(s.z)
	if e.p.priceTrigger() {
		size = s.move
	}
	b := 0
	for b < len(e.buckets) && size >= e.buckets[b] {
		b++
	}
	e.ByMove[b].add(t)
	if e.sink != nil {
		e.sink(t)
	}
}

// Open reports how many followers have a pending, open or exiting position.
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
func (e *Engine) moveLabels() []string {
	unit := "|z| "
	if e.p.priceTrigger() {
		unit = "move bp "
	}
	out := make([]string, 0, len(e.buckets)+1)
	lo := 0.0
	if !e.p.priceTrigger() {
		lo = e.p.Detector.Theta
	}
	for _, b := range e.buckets {
		out = append(out, fmt.Sprintf("%s%g–%g", unit, lo, b))
		lo = b
	}
	return append(out, fmt.Sprintf("%s%g+", unit, lo))
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
	for i, label := range e.moveLabels() {
		byM = append(byM, map[string]any{"bucket": label, "summary": e.ByMove[i].Summarize()})
	}
	skipped := make(map[string]uint64, len(e.Skipped))
	for k, v := range e.Skipped {
		skipped[k] = v
	}
	return map[string]any{
		"run": e.p.Run, "exploratory": e.p.Exploratory, "params_sha256": paramsSHA, "params": e.p,
		"trigger": e.p.TriggerKind(), "exit_mode": e.p.ExitMode(), "markouts_ms": e.p.MarkoutsMs,
		"signals": e.Signals, "open": e.active, "skipped": skipped,
		"total": e.Total.Summarize(), "by_follower": byF, "by_leader": byL,
		"by_exit": map[string]Summary{"taker": e.ByExit[0].Summarize(), "maker": e.ByExit[1].Summarize()},
		"by_move": byM, "recent": e.Recent(),
	}
}
