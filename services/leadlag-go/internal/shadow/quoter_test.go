package shadow

import (
	"math"
	"reflect"
	"testing"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

var qsyms = []string{"BTCUSDT", "SOLUSDC"}

func quoterParams() Params {
	return Params{
		Run: "test-q", Exploratory: true, Leaders: []string{"BTCUSDT"}, Followers: []string{"SOLUSDC"},
		Trigger:   &TriggerCfg{WindowMs: 1000, MinMoveBp: 4, CooldownMs: 5000, MinGapBp: 2},
		TopK:      3,
		LatencyMs: 10, HorizonMs: 5000, MarkoutsMs: []int64{100, 1000},
		NotionalUSD: 25, TakerFeeBp: 4, MaxQuoteAgeMs: 1000,
		Ticks: map[string]int64{"SOLUSDC": e8(0.01)},
		Quoter: &QuoterCfg{MakerFeeBp: 0, MaxSpreadBp: 4, CooldownMs: 5000,
			EntryTimeoutMs: 3000, ReverseBp: 2, MaxReprices: 1, TargetGapFrac: 0.6, CloseGapBp: 0.5, StopMult: 1.5},
	}
}

// qfeed scripts BTC (sym 0) and SOLUSDC (sym 1): quotes every 100 ms to endMs.
// btc(t) and sol(t) give the mids; SOL's book is mid ± 0.01 with 5 on each
// side, or solQty(t) when set.
type qfeed struct {
	endMs  int64
	btc    func(t int64) float64
	sol    func(t int64) float64
	solQty func(t int64) (bid, ask float64)
	evs    []model.Event
}

func (q *qfeed) build() []model.Event {
	for t := int64(0); t < q.endMs; t += 100 {
		b := q.btc(t)
		q.evs = append(q.evs, model.Event{Sym: 0, Kind: model.KindQuote, RecvNs: t * ms, ExchMs: t, Bid: e8(b - 0.05), Ask: e8(b + 0.05), BidQty: e8(1), AskQty: e8(1)})
		s := q.sol(t)
		bq, aq := 5.0, 5.0
		if q.solQty != nil {
			bq, aq = q.solQty(t)
		}
		q.evs = append(q.evs, model.Event{Sym: 1, Kind: model.KindQuote, RecvNs: t*ms + ms, ExchMs: t, Bid: e8(s - 0.01), Ask: e8(s + 0.01), BidQty: e8(bq), AskQty: e8(aq)})
	}
	sortByRecv(q.evs)
	return q.evs
}

// trade adds a SOL trade at tMs: sell aggressor (hits bids) when sell.
func (q *qfeed) trade(tMs int64, px, qty float64, sell bool) {
	q.evs = append(q.evs, model.Event{Sym: 1, Kind: model.KindTrade, RecvNs: tMs*ms + 50*1000, TradeMs: tMs, ExchMs: tMs,
		Px: e8(px), Qty: e8(qty), BuyerMaker: sell})
}

func step(at int64, before, after float64) func(int64) float64 {
	return func(t int64) float64 {
		if t >= at {
			return after
		}
		return before
	}
}

func btcJump(t int64) float64 {
	if t >= 20_000 {
		return 60000 * (1 + 6e-4)
	}
	return 60000
}

func runQ(p Params, evs []model.Event) (*Engine, []Trade, []Attempt) {
	var trades []Trade
	var atts []Attempt
	e := New(p, qsyms, nil, func(t *Trade) { trades = append(trades, *t) })
	e.SetAttemptSink(func(a *Attempt) { atts = append(atts, *a) })
	run(e, evs)
	return e, trades, atts
}

func TestQuoterEntryFillsInTheQueueThenTheTargetFills(t *testing.T) {
	sol := func(t int64) float64 {
		if t >= 21_000 {
			return 100.05 // the follower catches up: bid 100.04, ask 100.06
		}
		return 100
	}
	q := &qfeed{endMs: 25_000, btc: btcJump, sol: sol}
	q.build()
	q.trade(20_300, 99.99, 3, true) // 3 of the 5 ahead
	q.trade(20_400, 99.99, 3, true) // 6 > 5: filled
	q.trade(21_200, 100.04, 1, false)
	sortByRecv(q.evs)
	e, trades, atts := runQ(quoterParams(), q.evs)
	if e.Signals != 1 || len(trades) != 1 || len(atts) != 1 {
		t.Fatalf("signals %d trades %d attempts %d skipped %v", e.Signals, len(trades), len(atts), e.Skipped)
	}
	tr := trades[0]
	// Long at the bid 99.99; target 99.99 + ceil(0.6 × 6 bp) = 100.03, resting
	// behind the touch until the book lifts past it at 21 s (then alone).
	if tr.Kind != "trade" || tr.Dir != 1 || tr.EntryPx != 99.99 || tr.ExitPx != 100.03 ||
		tr.ExitKind != "maker" || tr.ExitReason != "target" || tr.FeesBp != 0 || tr.QueueAhead != 5 {
		t.Fatalf("trade %+v", tr)
	}
	if math.Abs(tr.GrossBp-4.0004) > 1e-3 || math.Abs(tr.FillWaitMs-400.05) > 1 {
		t.Fatalf("gross %.4f wait %.2f", tr.GrossBp, tr.FillWaitMs)
	}
	if len(tr.MarkoutsBp) != 2 || len(tr.ExitMarkoutsBp) != 2 || tr.RecNs < tr.ExitNs+1000*ms {
		t.Fatalf("markouts %v %v rec %d", tr.MarkoutsBp, tr.ExitMarkoutsBp, tr.RecNs)
	}
	a := atts[0]
	if !a.Filled || a.EndReason != "filled" || a.QueueAhead != 5 || a.Follower != "SOLUSDC" || len(a.MarkoutsBp) != 2 {
		t.Fatalf("attempt %+v", a)
	}
	if e.Open() != 0 {
		t.Fatalf("open %d", e.Open())
	}
}

func TestQuoterUnfilledEntryTimesOut(t *testing.T) {
	q := &qfeed{endMs: 26_000, btc: btcJump, sol: step(99_999, 100, 100)}
	e, trades, atts := runQ(quoterParams(), q.build())
	if len(trades) != 0 || len(atts) != 1 || atts[0].Filled || atts[0].EndReason != "timeout" || e.Open() != 0 {
		t.Fatalf("trades %d attempts %+v open %d", len(trades), atts, e.Open())
	}
	if s := e.Status("x")["quoter"].(map[string]any); s["fill_rate"].(float64) != 0 {
		t.Fatalf("status %v", s)
	}
}

func TestQuoterRepricesOnceThenCancels(t *testing.T) {
	sol := func(t int64) float64 {
		switch {
		case t >= 20_600:
			return 100.06
		case t >= 20_300:
			return 100.03 // bid 100.02: 3 ticks above our 99.99
		}
		return 100
	}
	q := &qfeed{endMs: 23_000, btc: btcJump, sol: sol}
	e, _, atts := runQ(quoterParams(), q.build())
	if len(atts) != 1 || atts[0].EndReason != "chased" || e.q.reprices != 1 {
		t.Fatalf("attempts %+v reprices %d", atts, e.q.reprices)
	}
}

func TestQuoterCancelsOnALeaderReversal(t *testing.T) {
	btc := func(t int64) float64 {
		if t >= 20_000 && t < 20_500 {
			return 60000 * (1 + 6e-4)
		}
		return 60000
	}
	q := &qfeed{endMs: 23_000, btc: btc, sol: step(99_999, 100, 100)}
	_, _, atts := runQ(quoterParams(), q.build())
	if len(atts) != 1 || atts[0].EndReason != "reversal" {
		t.Fatalf("attempts %+v", atts)
	}
}

func TestQuoterStopsOutAsATaker(t *testing.T) {
	q := &qfeed{endMs: 25_000, btc: btcJump, sol: step(21_000, 100, 99.85)} // −15 bp ≤ −1.5 × 6
	q.build()
	q.trade(20_300, 99.98, 1, true) // through our bid: filled at once
	sortByRecv(q.evs)
	_, trades, _ := runQ(quoterParams(), q.evs)
	if len(trades) != 1 {
		t.Fatalf("trades %d", len(trades))
	}
	tr := trades[0]
	if tr.ExitReason != "stop" || tr.ExitKind != "taker" || tr.FeesBp != 4 || tr.ExitPx != 99.84 || tr.QueueAhead != 5 {
		t.Fatalf("trade %+v", tr)
	}
}

func TestQuoterTimeoutFlattens(t *testing.T) {
	q := &qfeed{endMs: 28_000, btc: btcJump, sol: step(99_999, 100, 100)}
	q.build()
	q.trade(20_300, 99.98, 1, true)
	sortByRecv(q.evs)
	_, trades, _ := runQ(quoterParams(), q.evs)
	if len(trades) != 1 || trades[0].ExitReason != "timeout" || trades[0].ExitKind != "taker" {
		t.Fatalf("trades %+v", trades)
	}
	if held := (trades[0].ExitNs - trades[0].EntryNs) / ms; held < 5000 || held > 5200 {
		t.Fatalf("held %d ms", held)
	}
}

func TestQuoterImbalanceGateSkipsAThinSide(t *testing.T) {
	p := quoterParams()
	p.Quoter.MinEntryImb, p.Quoter.CancelImb = 0.5, 0.25
	// Our bid side holds 2 of 8 at the signal: share 0.25 < 0.5.
	q := &qfeed{endMs: 25_000, btc: btcJump, sol: step(99_999, 100, 100), solQty: func(int64) (float64, float64) { return 2, 6 }}
	e, _, atts := runQ(p, q.build())
	if e.Signals != 1 || len(atts) != 0 || e.Skipped["imbalance"] != 1 {
		t.Fatalf("signals %d attempts %d skipped %v", e.Signals, len(atts), e.Skipped)
	}
	// A short would join the heavy ask side: the gate is per direction.
	if got := sideShare(&quote{bidQty: e8(2), askQty: e8(6)}, -1); got != 0.75 {
		t.Fatalf("ask share %v", got)
	}
}

func TestQuoterCancelsWhenItsSideThins(t *testing.T) {
	p := quoterParams()
	p.Quoter.MinEntryImb, p.Quoter.CancelImb = 0.5, 0.25
	// Balanced at the signal, then at 20.5 s our bid side drops to 1 of 6.
	qty := func(t int64) (float64, float64) {
		if t >= 20_500 {
			return 1, 5
		}
		return 5, 5
	}
	q := &qfeed{endMs: 25_000, btc: btcJump, sol: step(99_999, 100, 100), solQty: qty}
	_, trades, atts := runQ(p, q.build())
	if len(trades) != 0 || len(atts) != 1 || atts[0].EndReason != "imbalance" || atts[0].EntryImb != 0.5 {
		t.Fatalf("trades %d attempts %+v", len(trades), atts)
	}
}

// ladderParams: explore-4's exit ladder on the test quoter (soft stop at
// 0.5 × gap = 3 bp, hard stop 12 bp, 2 s of grace after the 5 s horizon).
func ladderParams() Params {
	p := quoterParams()
	p.Quoter.StopMult, p.Quoter.HardStopBp, p.Quoter.ExitGraceMs = 0.5, 12, 2000
	return p
}

// filledLong builds the feed and fills the long entry at 99.99 at 20.3 s.
func filledLong(q *qfeed, trades ...[3]float64) []model.Event {
	q.build()
	q.trade(20_300, 99.98, 1, true)
	for _, t := range trades { // [ms, px, qty] buy aggressors
		q.trade(int64(t[0]), t[1], t[2], false)
	}
	sortByRecv(q.evs)
	return q.evs
}

func TestQuoterSoftStopRestsAtTheTouchAndFollowsIt(t *testing.T) {
	sol := func(t int64) float64 {
		switch {
		case t >= 21_500:
			return 99.93 // ask 99.94: the exit follows it down
		case t >= 21_000:
			return 99.95 // −4 bp: past the 3 bp soft stop, short of the 12 bp hard stop
		}
		return 100
	}
	q := &qfeed{endMs: 25_000, btc: btcJump, sol: sol}
	_, trades, _ := runQ(ladderParams(), filledLong(q, [3]float64{22_000, 99.95, 1}))
	if len(trades) != 1 {
		t.Fatalf("trades %d", len(trades))
	}
	if tr := trades[0]; tr.ExitReason != "stop_soft" || tr.ExitKind != "maker" || tr.FeesBp != 0 || tr.ExitPx != 99.94 {
		t.Fatalf("trade %+v", tr)
	}
}

func TestQuoterHardStopCrosses(t *testing.T) {
	q := &qfeed{endMs: 25_000, btc: btcJump, sol: step(21_000, 100, 99.85)} // −14 bp
	e, trades, _ := runQ(ladderParams(), filledLong(q))
	if len(trades) != 1 {
		t.Fatalf("trades %d", len(trades))
	}
	if tr := trades[0]; tr.ExitReason != "stop" || tr.ExitKind != "taker" || tr.FeesBp != 4 || tr.ExitPx != 99.84 {
		t.Fatalf("trade %+v", tr)
	}
	st := e.Status("x")
	roll := st["rolling"].(map[string]any)
	if st["taker_crosses"].(map[string]uint64)["stop"] != 1 || roll["n"] != 1 || roll["taker_frac"] != 1.0 {
		t.Fatalf("status crosses %v rolling %v", st["taker_crosses"], roll)
	}
}

func TestQuoterHorizonRestsThenCrossesAfterTheGrace(t *testing.T) {
	flat := step(99_999, 100, 100)
	// Nobody lifts the touch: a taker exit 5 s + 2 s after the fill.
	q := &qfeed{endMs: 30_000, btc: btcJump, sol: flat}
	_, trades, _ := runQ(ladderParams(), filledLong(q))
	if len(trades) != 1 || trades[0].ExitReason != "timeout" || trades[0].ExitKind != "taker" {
		t.Fatalf("trades %+v", trades)
	}
	if held := (trades[0].ExitNs - trades[0].EntryNs) / ms; held < 7000 || held > 7200 {
		t.Fatalf("held %d ms", held)
	}
	// A buyer lifts our ask (moved from the 100.03 target to the 100.01 touch) inside the grace: a maker exit.
	q = &qfeed{endMs: 30_000, btc: btcJump, sol: flat}
	_, trades, _ = runQ(ladderParams(), filledLong(q, [3]float64{26_000, 100.02, 1}))
	if len(trades) != 1 {
		t.Fatalf("trades %d", len(trades))
	}
	if tr := trades[0]; tr.ExitReason != "timeout" || tr.ExitKind != "maker" || tr.ExitPx != 100.01 {
		t.Fatalf("trade %+v", tr)
	}
}

func TestQuoterReplayIsDeterministic(t *testing.T) {
	q := &qfeed{endMs: 28_000, btc: btcJump, sol: step(21_000, 100, 100.05)}
	q.build()
	q.trade(20_300, 99.98, 1, true)
	sortByRecv(q.evs)
	_, a, aa := runQ(quoterParams(), q.evs)
	_, b, bb := runQ(quoterParams(), q.evs)
	if len(a) != 1 || !reflect.DeepEqual(a, b) || !reflect.DeepEqual(aa, bb) {
		t.Fatalf("replay differs:\n%+v\n%+v", a, b)
	}
}

func TestQuoterParams(t *testing.T) {
	p := quoterParams()
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	bad := quoterParams()
	bad.Quoter.EntryTimeoutMs = 0
	if bad.Validate() == nil {
		t.Fatal("entry_timeout_ms 0 accepted")
	}
	bad = quoterParams()
	bad.Quoter.MinEntryImb, bad.Quoter.CancelImb = 0.4, 0.4
	if bad.Validate() == nil {
		t.Fatal("cancel_imb >= min_entry_imb accepted")
	}
	bad = quoterParams()
	bad.Quoter.HardStopBp = -1
	if bad.Validate() == nil {
		t.Fatal("hard_stop_bp < 0 accepted")
	}
	bad = quoterParams()
	bad.Quoter.MinEntryImb = 1.5
	if bad.Validate() == nil {
		t.Fatal("min_entry_imb > 1 accepted")
	}
}

// A quote with nothing to do must not allocate (the engine runs under the hub lock).
func TestQuoterQuoteEventDoesNotAllocate(t *testing.T) {
	q := &qfeed{endMs: 3_000, btc: func(int64) float64 { return 60000 }, sol: func(int64) float64 { return 100 }}
	e := New(quoterParams(), qsyms, nil, nil)
	run(e, q.build())
	ev := model.Event{Sym: 1, Kind: model.KindQuote, RecvNs: 3_000 * ms, Bid: e8(99.99), Ask: e8(100.01), BidQty: e8(5), AskQty: e8(5)}
	lv := model.Event{Sym: 0, Kind: model.KindQuote, RecvNs: 3_000 * ms, Bid: e8(59999.95), Ask: e8(60000.05)}
	n := testing.AllocsPerRun(200, func() {
		ev.RecvNs += ms
		lv.RecvNs += ms
		e.OnEvent(&ev)
		e.OnEvent(&lv)
	})
	if n != 0 {
		t.Fatalf("%v allocations per event pair", n)
	}
}
