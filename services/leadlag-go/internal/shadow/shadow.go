// Package shadow is the shadow-trading engine: leader flow events (the
// pre-registered detector) open simulated taker positions in the followers,
// filled against the live bookTicker, and close them after a fixed horizon.
// Nothing is sent to an exchange.
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

// Params fix one shadow run (research/PREREG.md Part 2 for the exploratory run).
type Params struct {
	Run         string               `json:"run"`
	Exploratory bool                 `json:"exploratory"`
	Leaders     []string             `json:"leaders"`
	Followers   []string             `json:"followers"`
	Detector    research.DetectorCfg `json:"detector"`
	// LatencyMs: the earliest fill after a signal's receipt (order to exchange).
	LatencyMs int64 `json:"latency_ms"`
	// HorizonMs: the exit, measured from the entry fill.
	HorizonMs     int64   `json:"horizon_ms"`
	NotionalUSD   float64 `json:"notional_usd"`
	TakerFeeBp    float64 `json:"taker_fee_bp"`
	MaxQuoteAgeMs int64   `json:"max_quote_age_ms"`
}

// Validate checks that the parameters describe a runnable experiment.
func (p *Params) Validate() error {
	switch {
	case p.Run == "":
		return errors.New("shadow: run label is empty")
	case len(p.Leaders) == 0 || len(p.Followers) == 0:
		return errors.New("shadow: leaders and followers are required")
	case p.Detector.WMs <= 0 || p.Detector.Theta <= 0 || p.Detector.HalfLifeMs <= 0:
		return errors.New("shadow: detector w_ms, theta and half_life_ms must be > 0")
	case p.LatencyMs < 0 || p.HorizonMs <= 0:
		return errors.New("shadow: latency_ms >= 0 and horizon_ms > 0 are required")
	case p.NotionalUSD <= 0 || p.TakerFeeBp < 0 || p.MaxQuoteAgeMs <= 0:
		return errors.New("shadow: notional_usd > 0, taker_fee_bp >= 0 and max_quote_age_ms > 0 are required")
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
	Dir         int8    `json:"dir"` // +1 long, −1 short
	Z           float64 `json:"z"`
	LeaderTMs   int64   `json:"leader_t_ms"` // leader trade time that crossed θ
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
}

// Agg accumulates trades for one group (overall, a follower, a leader).
type Agg struct {
	N       int     `json:"n"`
	Wins    int     `json:"wins"`
	SumNet  float64 `json:"sum_net_bp"`
	SumNet2 float64 `json:"-"`
	SumGros float64 `json:"sum_gross_bp"`
	NetUSD  float64 `json:"net_usd"`
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
}

// Summary is an Agg's derived statistics.
type Summary struct {
	N         int     `json:"n"`
	MeanGross float64 `json:"mean_gross_bp"`
	MeanNet   float64 `json:"mean_net_bp"`
	HitRate   float64 `json:"hit_rate"`
	TStat     float64 `json:"t_stat"`
	NetUSD    float64 `json:"net_usd"`
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

type slot struct {
	state    uint8 // 0 idle, 1 entry pending, 2 open
	dueNs    int64 // pending: earliest fill; open: exit time
	leader   int   // index into leaders
	dir      int8
	z        float64
	leaderT  int64
	signalNs int64
	entryNs  int64
	entryPx  float64
	spreadBp float64
	qty      float64
}

const (
	idle = iota
	pending
	open
)

// RingSize is how many recent trades the status page shows.
const RingSize = 50

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

	Signals   uint64
	Skipped   map[string]uint64
	Total     Agg
	ByFollow  []Agg
	ByLeader  []Agg
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
	for i, s := range symbols {
		e.leaderOf[i], e.followerOf[i] = -1, -1
		switch {
		case isLeader[s]:
			e.leaderOf[i] = len(e.leaders)
			e.leaders = append(e.leaders, s)
			e.det = append(e.det, research.NewDetector(p.Detector))
		case isFollower[s]:
			e.followerOf[i] = len(e.followers)
			e.followers = append(e.followers, s)
		}
	}
	e.quotes = make([]quote, len(e.followers))
	e.slots = make([]slot, len(e.followers))
	e.ByFollow = make([]Agg, len(e.followers))
	e.ByLeader = make([]Agg, len(e.leaders))
	for _, k := range []string{"halted", "busy", "no_quote", "stale_quote", "crossed"} {
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
		if ev.Kind == model.KindQuote {
			e.quotes[f] = quote{bid: ev.Bid, ask: ev.Ask, recvNs: now}
		}
		return
	}
	l := e.leaderOf[ev.Sym]
	if l < 0 || ev.Kind != model.KindTrade {
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

func (e *Engine) runTimers(now int64) {
	maxAge := e.p.MaxQuoteAgeMs * 1_000_000
	for f := range e.slots {
		s := &e.slots[f]
		if s.state == idle || now < s.dueNs {
			continue
		}
		q := e.quotes[f]
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
			if reason != "" {
				e.Skipped[reason]++
				s.state = idle
				e.active--
				continue
			}
			bid, ask := float64(q.bid)/model.Scale, float64(q.ask)/model.Scale
			px := ask
			if s.dir < 0 {
				px = bid
			}
			s.state, s.entryNs, s.entryPx = open, now, px
			s.spreadBp = (ask - bid) / ((ask + bid) / 2) * 1e4
			s.qty = e.p.NotionalUSD / px
			s.dueNs = now + e.p.HorizonMs*1_000_000
		case open:
			// Exit on the latest quote even if it is old: the position must close.
			px := float64(q.bid) / model.Scale
			if s.dir < 0 {
				px = float64(q.ask) / model.Scale
			}
			e.close(f, s, now, px)
			s.state = idle
			e.active--
		}
	}
}

func (e *Engine) close(f int, s *slot, now int64, exitPx float64) {
	gross := float64(s.dir) * (exitPx - s.entryPx) / s.entryPx * 1e4
	fees := 2 * e.p.TakerFeeBp
	net := gross - fees
	t := &e.Ring[e.RingNext]
	*t = Trade{
		Run: e.p.Run, Exploratory: e.p.Exploratory, Leader: e.leaders[s.leader], Follower: e.followers[f],
		Dir: s.dir, Z: round(s.z, 3), LeaderTMs: s.leaderT, SignalNs: s.signalNs, EntryNs: s.entryNs, ExitNs: now,
		EntryPx: s.entryPx, ExitPx: exitPx, SpreadBp: round(s.spreadBp, 3), Qty: s.qty, NotionalUSD: e.p.NotionalUSD,
		GrossBp: round(gross, 4), FeesBp: fees, NetBp: round(net, 4), NetUSD: round(net/1e4*e.p.NotionalUSD, 6),
	}
	e.RingNext = (e.RingNext + 1) % RingSize
	if e.RingCount < RingSize {
		e.RingCount++
	}
	e.Total.add(t)
	e.ByFollow[f].add(t)
	e.ByLeader[s.leader].add(t)
	if e.sink != nil {
		e.sink(t)
	}
}

// Open reports how many followers have a pending or open position.
func (e *Engine) Open() int { return e.active }

// Recent returns the ring's trades, newest first (call under the hub lock).
func (e *Engine) Recent() []Trade {
	out := make([]Trade, 0, e.RingCount)
	for i := 1; i <= e.RingCount; i++ {
		out = append(out, e.Ring[(e.RingNext-i+RingSize)%RingSize])
	}
	return out
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
	skipped := make(map[string]uint64, len(e.Skipped))
	for k, v := range e.Skipped {
		skipped[k] = v
	}
	return map[string]any{
		"run": e.p.Run, "exploratory": e.p.Exploratory, "params_sha256": paramsSHA, "params": e.p,
		"signals": e.Signals, "open": e.active, "skipped": skipped,
		"total": e.Total.Summarize(), "by_follower": byF, "by_leader": byL, "recent": e.Recent(),
	}
}
