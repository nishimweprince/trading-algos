package shadow

import (
	"errors"
	"fmt"
	"math"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

// QuoterCfg configures the trader's orders (PREREG Parts 4–5).
//
// Entry: when the price trigger fires, post-only bids
// (up move) or asks (down move) go to the touch of the top_k followers that
// lag the leader most by β-adjusted gap. The entry is repriced once if the
// touch moves away by more than a tick and then cancelled, and cancelled when
// the leader gives back reverse_bp, the gap closes, or entry_timeout_ms
// passes. A filled entry rests a post-only exit at entry ± max(1 tick,
// target_gap_frac × gap), moves it to the touch when the residual gap is at
// most close_gap_bp, and is flattened as a taker after horizon_ms or when the
// follower moves stop_mult × gap against it.
//
// Queue imbalance (explore-4): our side's share of the follower's L1 size,
// bid/(bid+ask) for a long and ask/(bid+ask) for a short. A thin queue on our
// side is the one about to be swept, and a fill there is adversely selected
// (the microprice sits on the other side of mid). An entry needs a share of at
// least min_entry_imb, and a working entry is cancelled once it falls under
// cancel_imb.
type QuoterCfg struct {
	MakerFeeBp float64 `json:"maker_fee_bp"`
	// MaxSpreadBp > 0: no new entry while the follower's spread is wider.
	MaxSpreadBp float64 `json:"max_spread_bp,omitempty"`
	// CooldownMs: a follower takes no signal for this long after going flat.
	CooldownMs int64 `json:"cooldown_ms"`

	EntryTimeoutMs int64   `json:"entry_timeout_ms,omitempty"`
	ReverseBp      float64 `json:"reverse_bp,omitempty"`
	MaxReprices    int     `json:"max_reprices,omitempty"`
	TargetGapFrac  float64 `json:"target_gap_frac,omitempty"`
	CloseGapBp     float64 `json:"close_gap_bp,omitempty"`
	StopMult       float64 `json:"stop_mult,omitempty"`

	// MinEntryImb, CancelImb: queue-imbalance gate and cancel, 0: off.
	MinEntryImb float64 `json:"min_entry_imb,omitempty"`
	CancelImb   float64 `json:"cancel_imb,omitempty"`
}

func (c *QuoterCfg) validate(p *Params) error {
	if c.MakerFeeBp < 0 || c.MaxSpreadBp < 0 || c.CooldownMs < 0 {
		return errors.New("shadow: quoter maker_fee_bp, max_spread_bp and cooldown_ms must be >= 0")
	}
	for _, f := range p.Followers {
		if p.Ticks[f] <= 0 {
			return fmt.Errorf("shadow: quoter needs ticks[%s] (llfetch ticks)", f)
		}
	}
	if c.EntryTimeoutMs <= 0 || c.ReverseBp < 0 || c.MaxReprices < 0 || c.TargetGapFrac < 0 || c.CloseGapBp < 0 || c.StopMult < 0 {
		return errors.New("shadow: quoter needs entry_timeout_ms > 0 and reverse_bp, max_reprices, target_gap_frac, close_gap_bp, stop_mult >= 0")
	}
	if c.MinEntryImb < 0 || c.MinEntryImb > 1 || c.CancelImb < 0 || c.CancelImb > 1 ||
		(c.MinEntryImb > 0 && c.CancelImb >= c.MinEntryImb) {
		return errors.New("shadow: quoter min_entry_imb and cancel_imb must be in [0, 1], cancel_imb < min_entry_imb")
	}
	return nil
}

// Attempt is one entry order, filled or not (journal kind "attempt").
// Its markouts run from the post, whatever happened to the order, so filled
// and unfilled attempts can be compared: the fills' adverse selection.
type Attempt struct {
	Kind         string    `json:"kind"`
	Run          string    `json:"run"`
	Exploratory  bool      `json:"exploratory"`
	Leader       string    `json:"leader"`
	Follower     string    `json:"follower"`
	Dir          int8      `json:"dir"`
	SignalNs     int64     `json:"signal_recv_ns"`
	PostNs       int64     `json:"post_recv_ns"`
	PostPx       float64   `json:"post_px"`
	LeaderMoveBp float64   `json:"leader_move_bp"`
	GapBp        float64   `json:"gap_bp"`
	Beta         float64   `json:"beta,omitempty"`
	SpreadBp     float64   `json:"spread_bp"`
	EntryImb     float64   `json:"entry_imb"`   // our side's L1 share at the post
	QueueAhead   float64   `json:"queue_ahead"` // when it joined the book (−1: never did)
	Filled       bool      `json:"filled"`
	FillWaitMs   float64   `json:"fill_wait_ms,omitempty"`
	EndReason    string    `json:"end_reason"` // filled, timeout, gap_closed, reversal, chased, rejected, imbalance
	MarkoutsBp   []float64 `json:"markouts_bp,omitempty"`
	RecNs        int64     `json:"rec_ns"`
}

// attemptEnds name Attempt.EndReason (and index the status counts).
var attemptEnds = [...]string{"filled", "timeout", "gap_closed", "reversal", "chased", "rejected", "imbalance"}

// marks samples signed markouts on the markouts_ms grid from fromNs.
type marks struct {
	fromNs int64
	ref    float64
	sign   float64
	n      int
	v      [MaxMarkouts]float64
}

func (m *marks) step(now int64, mid float64, grid []int64) {
	for m.n < len(grid) && now >= m.fromNs+grid[m.n]*1_000_000 {
		m.v[m.n] = m.sign * (mid - m.ref) / m.ref * 1e4
		m.n++
	}
}

func (m *marks) out() []float64 {
	if m.n == 0 {
		return nil
	}
	o := make([]float64, m.n)
	for i := range o {
		o[i] = round(m.v[i], 4)
	}
	return o
}

// Quoter slot states.
const (
	qFlat     = iota
	qEntry    // the entry order is working
	qOpen     // a position, its exit order working
	qFlatting // a position, exit order cancelling; a taker exit at flatNs
)

type qslot struct {
	state    uint8
	dir      int8
	leader   int
	signalNs int64
	sigMid   float64 // the leader's mid at the signal (reversal)
	move     float64
	preMove  float64
	gap      float64
	z        float64
	leaderT  int64
	base     gapBase
	spreadBp float64
	imb      float64 // our side's L1 share at the post

	entry    order
	exit     order
	reprices int
	attempt  int // index into q.attempts, −1 none

	postNs    int64
	fillNs    int64
	entryPx   float64
	entryAh   float64
	reason    string // exit reason once decided
	exitMode  uint8  // 0 target, 1 at the touch
	flatNs    int64
	coolUntil int64
	em        marks // entry-fill markouts
}

type pendAttempt struct {
	a Attempt
	f int
	m marks
}

type pendTrade struct {
	t      Trade
	leader int
	f      int
	maker  bool
	em, xm marks
}

const maxPending = 4096

type quoterState struct {
	cfg   *QuoterCfg
	latNs int64
	ticks []int64 // per follower, E8
	slots []qslot

	attempts []pendAttempt
	trades   []pendTrade
	dropped  uint64 // pending records lost to a full buffer

	AttemptSink func(*Attempt)

	nAttempts  int
	ends       [len(attemptEnds)]uint64
	markFilled Agg // attempts' markouts, filled
	markMissed Agg // and not filled
	rejects    uint64
	reprices   uint64
}

func newQuoterState(e *Engine) *quoterState {
	q := &quoterState{cfg: e.p.Quoter, latNs: e.p.LatencyMs * 1_000_000, slots: make([]qslot, len(e.followers)),
		attempts: make([]pendAttempt, 0, 64), trades: make([]pendTrade, 0, 64)}
	for _, f := range e.followers {
		q.ticks = append(q.ticks, e.p.Ticks[f])
	}
	for i := range q.slots {
		q.slots[i].attempt = -1
		q.slots[i].leader = -1
	}
	for _, k := range []string{"cooldown", "rejected", "imbalance"} {
		e.Skipped[k] = 0
	}
	return q
}

// SetAttemptSink receives every finished attempt (the journal; replay).
func (e *Engine) SetAttemptSink(f func(*Attempt)) {
	e.q.AttemptSink = f
}

func (e *Engine) halt() bool { return e.halted != nil && e.halted.Load() }

// quoterEvent runs one event through the quoter.
func (e *Engine) quoterEvent(ev *model.Event) {
	now := ev.RecvNs
	e.qTimers(now)
	if f := e.followerOf[ev.Sym]; f >= 0 {
		s := &e.q.slots[f]
		switch ev.Kind {
		case model.KindQuote:
			qt := &e.quotes[f]
			*qt = quote{bid: ev.Bid, ask: ev.Ask, bidQty: ev.BidQty, askQty: ev.AskQty, recvNs: now}
			if ev.Bid > 0 && ev.Ask > ev.Bid {
				e.fhist[f].push(now, float64(ev.Bid+ev.Ask)/2/model.Scale)
			}
			s.entry.onQuote(qt)
			s.exit.onQuote(qt)
			e.qFollow(f, s, now)
		case model.KindTrade:
			if s.entry.onTrade(ev.Px, ev.Qty, ev.TakerSign(), ev.TradeMs) {
				e.qFill(f, s, &s.entry, now)
			}
			if s.exit.onTrade(ev.Px, ev.Qty, ev.TakerSign(), ev.TradeMs) {
				e.qFill(f, s, &s.exit, now)
			}
		}
		return
	}
	l := e.leaderOf[ev.Sym]
	if l < 0 {
		return
	}
	switch {
	case ev.Kind == model.KindLiq:
		e.onLeaderLiq(l, now, ev)
	case ev.Kind == model.KindQuote && ev.Bid > 0 && ev.Ask > ev.Bid:
		mid := float64(ev.Bid+ev.Ask) / 2 / model.Scale
		move, fire := e.trigger(l, now, mid)
		e.qReversals(l, now, mid)
		if fire {
			e.qSignal(l, now, ev.ExchMs, move)
		}
	}
}

// qSignal posts entries in the top_k laggers, ranked by gap / spread.
func (e *Engine) qSignal(l int, now, exchMs int64, move float64) {
	e.Signals++
	if e.halt() {
		e.Skipped["halted"]++
		return
	}
	if e.p.Trigger.LiqWindowMs > 0 && e.liqWithin(l, now, -sign(move)) {
		e.Skipped["liq_opposite"]++
		return
	}
	dir := e.laggers(l, now, move, func(f int) bool {
		s := &e.q.slots[f]
		if s.state != qFlat {
			e.Skipped["busy"]++
			return false
		}
		if now < s.coolUntil {
			e.Skipped["cooldown"]++
			return false
		}
		return true
	})
	// Book checks, then rank by gap per bp of spread.
	maxAge := e.p.MaxQuoteAgeMs * 1_000_000
	n := 0
	for _, f := range e.cand {
		q := &e.quotes[f]
		switch {
		case q.recvNs == 0:
			e.Skipped["no_quote"]++
			continue
		case now-q.recvNs > maxAge:
			e.Skipped["stale_quote"]++
			continue
		case q.bid <= 0 || q.ask <= q.bid:
			e.Skipped["crossed"]++
			continue
		}
		if c := e.q.cfg.MaxSpreadBp; c > 0 && spreadBp(q) > c {
			e.Skipped["wide_spread"]++
			continue
		}
		if c := e.q.cfg.MinEntryImb; c > 0 && sideShare(q, dir) < c {
			e.Skipped["imbalance"]++
			continue
		}
		e.cand[n] = f
		n++
	}
	e.cand = e.cand[:n]
	if n == 0 {
		e.Skipped["no_lagger"]++
		return
	}
	score := func(f int) float64 { return e.candGap[f] / spreadBp(&e.quotes[f]) }
	for i := 1; i < n; i++ {
		for j := i; j > 0 && score(e.cand[j]) > score(e.cand[j-1]); j-- {
			e.cand[j], e.cand[j-1] = e.cand[j-1], e.cand[j]
		}
	}
	k := n
	if e.p.TopK > 0 && k > e.p.TopK {
		k = e.p.TopK
	}
	lMid, _ := e.lhist[l].last()
	for _, f := range e.cand[:k] {
		q := &e.quotes[f]
		s := &e.q.slots[f]
		px := q.bid
		if dir < 0 {
			px = q.ask
		}
		*s = qslot{state: qEntry, dir: dir, leader: l, signalNs: now, sigMid: lMid, move: math.Abs(move),
			preMove: e.candPre[f], gap: e.candGap[f], z: round(move, 3), leaderT: exchMs, base: e.candBase[f],
			spreadBp: spreadBp(q), imb: sideShare(q, dir), postNs: now, attempt: -1}
		s.entry.post(dir, px, now, e.q.latNs)
		e.active++
		if len(e.q.attempts) < maxPending {
			a := Attempt{Kind: "attempt", Run: e.p.Run, Exploratory: e.p.Exploratory, Leader: e.leaders[l],
				Follower: e.followers[f], Dir: dir, SignalNs: now, PostNs: now, PostPx: float64(px) / model.Scale,
				LeaderMoveBp: round(math.Abs(move), 3), GapBp: round(s.gap, 3), SpreadBp: round(s.spreadBp, 3),
				EntryImb: round(s.imb, 4), QueueAhead: -1}
			if e.betas != nil {
				a.Beta = round(s.base.beta, 4)
			}
			e.q.attempts = append(e.q.attempts, pendAttempt{a: a, f: f, m: marks{fromNs: now, ref: q.mid(), sign: float64(dir)}})
			s.attempt = len(e.q.attempts) - 1
			e.q.nAttempts++
		} else {
			e.q.dropped++
		}
	}
}

func spreadBp(q *quote) float64 {
	return float64(q.ask-q.bid) / (float64(q.ask+q.bid) / 2) * 1e4
}

// sideShare is the L1 size on the side a dir order joins (bid for a long) as
// a share of both sides; 0.5 with no sizes.
func sideShare(q *quote, dir int8) float64 {
	tot := q.bidQty + q.askQty
	if tot <= 0 {
		return 0.5
	}
	if dir > 0 {
		return float64(q.bidQty) / float64(tot)
	}
	return float64(q.askQty) / float64(tot)
}

// qReversals cancels entries of leader l once it gives back reverse_bp.
func (e *Engine) qReversals(l int, now int64, mid float64) {
	rb := e.q.cfg.ReverseBp
	if rb <= 0 {
		return
	}
	for f := range e.q.slots {
		s := &e.q.slots[f]
		if s.state == qEntry && s.leader == l && !s.entry.cancelling() &&
			float64(s.dir)*(mid-s.sigMid)/s.sigMid*1e4 <= -rb {
			e.qCancelEntry(f, s, now, "reversal")
		}
	}
}

func (e *Engine) qCancelEntry(f int, s *qslot, now int64, why string) {
	s.entry.cancel(now, e.q.latNs)
	e.qEndAttempt(s, why, now)
}

// qEndAttempt fixes how the attempt ended (its markouts keep running).
func (e *Engine) qEndAttempt(s *qslot, why string, now int64) {
	if s.attempt < 0 {
		return
	}
	a := &e.q.attempts[s.attempt].a
	switch {
	case why == "filled": // also after a cancel was sent: it filled before the cancel landed
		a.EndReason, a.Filled = why, true
		a.FillWaitMs = round(float64(now-s.postNs)/1e6, 3)
	case a.EndReason == "":
		a.EndReason = why
	}
}

// qFollow manages resting orders after a follower quote.
func (e *Engine) qFollow(f int, s *qslot, now int64) {
	q := &e.quotes[f]
	if q.bid <= 0 || q.ask <= q.bid {
		return
	}
	tick := e.q.ticks[f]
	if s.state != qEntry || s.entry.cancelling() || s.entry.nextPx != 0 {
		return
	}
	o := &s.entry
	away := (o.side > 0 && q.bid > o.px+tick) || (o.side < 0 && q.ask < o.px-tick)
	if !away {
		return
	}
	if s.reprices < e.q.cfg.MaxReprices {
		s.reprices++
		e.q.reprices++
		px := q.bid
		if o.side < 0 {
			px = q.ask
		}
		o.reprice(px, now, e.q.latNs)
		return
	}
	e.qCancelEntry(f, s, now, "chased")
}

// qFill handles a fill of order o.
func (e *Engine) qFill(f int, s *qslot, o *order, now int64) {
	px := float64(o.px) / model.Scale
	if o == &s.entry {
		s.entryAh = o.joined
		*o = order{}
		e.qEndAttempt(s, "filled", now)
		if s.attempt >= 0 {
			e.q.attempts[s.attempt].a.QueueAhead = round(s.entryAh, 6)
		}
		e.qOpened(f, s, now, px)
		e.qPostTarget(f, s, now)
		return
	}
	*o = order{}
	reason := s.reason
	if reason == "" {
		reason = "target"
	}
	e.qClose(f, s, now, px, true, reason)
}

func (e *Engine) qOpened(f int, s *qslot, now int64, px float64) {
	s.state, s.fillNs, s.entryPx = qOpen, now, px
	s.em = marks{fromNs: now, ref: px, sign: float64(s.dir)}
}

// qPostTarget rests the exit at entry ± max(1 tick, frac × gap), never
// crossing the book.
func (e *Engine) qPostTarget(f int, s *qslot, now int64) {
	q := &e.quotes[f]
	tick := e.q.ticks[f]
	entry := int64(math.Round(s.entryPx * model.Scale))
	off := int64(math.Ceil(e.q.cfg.TargetGapFrac*s.gap/1e4*float64(entry)/float64(tick))) * tick
	off = max(off, tick)
	if s.dir > 0 {
		px := max(entry+off, q.bid+tick)
		s.exit.post(-1, px, now, e.q.latNs)
	} else {
		px := min(entry-off, q.ask-tick)
		s.exit.post(1, px, now, e.q.latNs)
	}
}

// qExitAtTouch moves the exit to our own touch (a long sells at the ask) when
// that is more aggressive than where it rests; it reports whether it moved.
func (e *Engine) qExitAtTouch(f int, s *qslot, now int64) bool {
	q := &e.quotes[f]
	px := q.ask
	if s.dir < 0 {
		px = q.bid
	}
	o := &s.exit
	if !o.active() {
		o.post(-s.dir, px, now, e.q.latNs)
		return true
	}
	cur := o.px
	if o.nextPx != 0 {
		cur = o.nextPx
	}
	if (s.dir > 0 && px >= cur) || (s.dir < 0 && px <= cur) {
		return false
	}
	o.reprice(px, now, e.q.latNs)
	return true
}

// qFlatten cancels the resting exit and schedules a taker exit for when the cancel lands.
func (e *Engine) qFlatten(s *qslot, now int64, why string) {
	s.reason = why
	s.state = qFlatting
	s.flatNs = now + e.q.latNs
	s.exit.cancel(now, e.q.latNs)
}

// qTimers runs everything due at now: order timers, entry and position
// management, markouts, finished records.
func (e *Engine) qTimers(now int64) {
	q := e.q
	for f := range q.slots {
		s := &q.slots[f]
		qt := &e.quotes[f]
		mid := qt.mid()
		if s.entry.timers(now, qt) == ordRejected {
			q.rejects++
			e.Skipped["rejected"]++
			e.qEndAttempt(s, "rejected", now)
		}
		if s.exit.timers(now, qt) == ordRejected {
			q.rejects++
			e.Skipped["rejected"]++
		}
		if s.state != qFlat && s.state != qEntry && qt.bid > 0 && qt.ask > qt.bid {
			s.em.step(now, mid, e.p.MarkoutsMs)
		}
		switch s.state {
		case qEntry:
			if !s.entry.active() { // cancelled or rejected, never filled
				s.state = qFlat
				s.coolUntil = now + q.cfg.CooldownMs*1_000_000
				e.active--
				continue
			}
			if s.entry.cancelling() {
				continue
			}
			if now >= s.postNs+q.cfg.EntryTimeoutMs*1_000_000 {
				e.qCancelEntry(f, s, now, "timeout")
			} else if r, ok := e.qResidual(f, s); ok && r <= q.cfg.CloseGapBp {
				e.qCancelEntry(f, s, now, "gap_closed")
			} else if c := q.cfg.CancelImb; c > 0 && qt.bid > 0 && sideShare(qt, s.dir) < c {
				e.qCancelEntry(f, s, now, "imbalance")
			}
		case qOpen:
			if !s.exit.active() { // the exit was rejected (it would have crossed): rest it at the touch
				e.qExitAtTouch(f, s, now)
			}
			adverse := float64(s.dir) * (mid - s.entryPx) / s.entryPx * 1e4
			switch {
			case now >= s.fillNs+e.p.HorizonMs*1_000_000:
				e.qFlatten(s, now, "timeout")
			case q.cfg.StopMult > 0 && adverse <= -q.cfg.StopMult*s.gap:
				e.qFlatten(s, now, "stop")
			case s.exitMode == 0:
				if r, ok := e.qResidual(f, s); ok && r <= q.cfg.CloseGapBp {
					s.exitMode = 1
					if e.qExitAtTouch(f, s, now) {
						s.reason = "gap_closed"
					}
				}
			}
		case qFlatting:
			if now >= s.flatNs {
				px := float64(qt.bid) / model.Scale
				if s.dir < 0 {
					px = float64(qt.ask) / model.Scale
				}
				e.qClose(f, s, now, px, false, s.reason)
			}
		}
	}
	e.qPending(now)
}

func (e *Engine) qResidual(f int, s *qslot) (float64, bool) {
	lc, ok1 := e.lhist[s.leader].last()
	fc, ok2 := e.fhist[f].last()
	if !ok1 || !ok2 || s.base.leader <= 0 || s.base.follower <= 0 {
		return 0, false
	}
	return s.base.residual(s.dir, lc, fc), true
}

// qClose ends a round trip; the trade is recorded once its markouts are in.
func (e *Engine) qClose(f int, s *qslot, now int64, exitPx float64, maker bool, reason string) {
	q := e.q
	gross := float64(s.dir) * (exitPx - s.entryPx) / s.entryPx * 1e4
	fees, kind := q.cfg.MakerFeeBp+e.p.TakerFeeBp, "taker"
	if maker {
		fees, kind = 2*q.cfg.MakerFeeBp, "maker"
	}
	net := gross - fees
	t := Trade{Kind: "trade", Run: e.p.Run, Exploratory: e.p.Exploratory, Leader: e.leaders[s.leader], Follower: e.followers[f],
		Dir: s.dir, Z: round(s.z, 3), LeaderTMs: s.leaderT, SignalNs: s.signalNs, EntryNs: s.fillNs, ExitNs: now,
		EntryPx: s.entryPx, ExitPx: exitPx, SpreadBp: round(s.spreadBp, 3), Qty: e.p.NotionalUSD / s.entryPx,
		NotionalUSD: e.p.NotionalUSD, GrossBp: round(gross, 4), FeesBp: fees, NetBp: round(net, 4),
		NetUSD: round(net/1e4*e.p.NotionalUSD, 6), ExitKind: kind, ExitReason: reason,
		LeaderMoveBp: round(s.move, 3), PreMoveBp: round(s.preMove, 3), GapBp: round(s.gap, 3),
		EntryKind: "maker", EntryImb: round(s.imb, 4), QueueAhead: round(s.entryAh, 6),
		FillWaitMs: round(float64(s.fillNs-s.postNs)/1e6, 3)}
	if e.betas != nil {
		t.Beta = round(s.base.beta, 4)
	}
	if len(q.trades) < maxPending {
		q.trades = append(q.trades, pendTrade{t: t, leader: s.leader, f: f, maker: maker, em: s.em,
			xm: marks{fromNs: now, ref: exitPx, sign: -float64(s.dir)}})
	} else {
		q.dropped++
	}
	*s = qslot{state: qFlat, leader: -1, attempt: -1, coolUntil: now + q.cfg.CooldownMs*1_000_000}
	e.active--
}

// qPending advances the markouts of finished attempts and trades and emits
// the ones that are complete, in order.
func (e *Engine) qPending(now int64) {
	q := e.q
	grid := e.p.MarkoutsMs
	n := 0
	for i := range q.attempts {
		p := &q.attempts[i]
		f := p.f
		qt := &e.quotes[f]
		if qt.bid > 0 && qt.ask > qt.bid {
			p.m.step(now, qt.mid(), grid)
		}
		done := p.m.n == len(grid) && p.a.EndReason != ""
		slotRef := e.q.slots[f].attempt == i
		if done {
			if slotRef {
				e.q.slots[f].attempt = -1
			}
			p.a.MarkoutsBp, p.a.RecNs = p.m.out(), now
			e.qAttemptDone(&p.a)
			continue
		}
		if slotRef {
			e.q.slots[f].attempt = n
		}
		q.attempts[n] = *p
		n++
	}
	q.attempts = q.attempts[:n]
	n = 0
	for i := range q.trades {
		p := &q.trades[i]
		qt := &e.quotes[p.f]
		if qt.bid > 0 && qt.ask > qt.bid {
			mid := qt.mid()
			p.em.step(now, mid, grid)
			p.xm.step(now, mid, grid)
		}
		if p.em.n == len(grid) && p.xm.n == len(grid) {
			p.t.MarkoutsBp, p.t.ExitMarkoutsBp, p.t.RecNs = p.em.out(), p.xm.out(), now
			e.record(&p.t, p.leader, p.f, p.maker, p.t.LeaderMoveBp)
			continue
		}
		q.trades[n] = *p
		n++
	}
	q.trades = q.trades[:n]
}

func (e *Engine) qAttemptDone(a *Attempt) {
	q := e.q
	for i, r := range attemptEnds {
		if r == a.EndReason {
			q.ends[i]++
		}
	}
	t := Trade{MarkoutsBp: a.MarkoutsBp}
	if a.Filled {
		q.markFilled.add(&t)
	} else {
		q.markMissed.add(&t)
	}
	if q.AttemptSink != nil {
		q.AttemptSink(a)
	}
}

// status adds the quoter's own numbers to the status document.
func (q *quoterState) status(doc map[string]any) {
	ends := map[string]uint64{}
	for i, r := range attemptEnds {
		ends[r] = q.ends[i]
	}
	var done uint64
	for _, v := range q.ends {
		done += v
	}
	fill := 0.0
	if done > 0 {
		fill = round(float64(q.ends[0])/float64(done), 4)
	}
	doc["quoter"] = map[string]any{
		"attempts": q.nAttempts, "attempts_done": done, "attempt_ends": ends, "fill_rate": fill,
		"markouts_filled": q.markFilled.Summarize().Markouts, "markouts_unfilled": q.markMissed.Summarize().Markouts,
		"rejects": q.rejects, "reprices": q.reprices, "dropped": q.dropped,
		"pending_attempts": len(q.attempts), "pending_trades": len(q.trades),
	}
}
