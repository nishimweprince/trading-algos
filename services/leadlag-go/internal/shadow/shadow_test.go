package shadow

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

const ms = int64(1_000_000)

var symbols = []string{"BTCUSDT", "SOLUSDT", "XRPUSDT"}

func e8(v float64) int64 { return int64(math.Round(v * model.Scale)) }

// feed collects events; quote adds a book update with 5 on each side.
type feed struct {
	evs []model.Event
}

func (f *feed) quote(sym uint16, tMs int64, bid, ask float64) {
	f.evs = append(f.evs, model.Event{Sym: sym, Kind: model.KindQuote, RecvNs: tMs * ms, ExchMs: tMs, Bid: e8(bid), Ask: e8(ask),
		BidQty: e8(5), AskQty: e8(5)})
}

func sortByRecv(evs []model.Event) {
	for i := 1; i < len(evs); i++ {
		for j := i; j > 0 && evs[j].RecvNs < evs[j-1].RecvNs; j-- {
			evs[j], evs[j-1] = evs[j-1], evs[j]
		}
	}
}

func run(e *Engine, evs []model.Event) {
	for i := range evs {
		ev := evs[i]
		e.OnEvent(&ev)
	}
}

// Price trigger: BTC is the leader, SOL and XRP follow (1 bp spreads).
func priceParams() Params {
	return Params{
		Run: "test-price", Exploratory: true, Leaders: []string{"BTCUSDT"}, Followers: []string{"SOLUSDT", "XRPUSDT"},
		Trigger:   &TriggerCfg{WindowMs: 1000, MinMoveBp: 4, CooldownMs: 5000, MinGapBp: 2},
		TopK:      1,
		LatencyMs: 10, HorizonMs: 2000, MarkoutsMs: []int64{100, 1000},
		NotionalUSD: 25, TakerFeeBp: 4, MaxQuoteAgeMs: 1000,
		Ticks: map[string]int64{"SOLUSDT": e8(0.005), "XRPUSDT": e8(0.00005)},
		Quoter: &QuoterCfg{MaxSpreadBp: 4, CooldownMs: 5000, EntryTimeoutMs: 3000, ReverseBp: 2, MaxReprices: 1,
			TargetGapFrac: 0.6, CloseGapBp: 0.5, StopMult: 1.5},
	}
}

// priceFeed: quotes every 100 ms until endMs. BTC's mid is 60000 and jumps by
// jumpBp at 20 s. SOL moves by solPreBp at 19.5 s (before the jump: it no
// longer lags); XRP moves by xrpPostBp at 20.2 s (after the signal: it catches
// up).
type priceFeed struct {
	jumpBp, solPreBp, xrpPostBp float64
	endMs                       int64
	extraJumpAt                 int64 // > 0: BTC falls back at extraJumpAt−1000 and jumps again here
}

func (pf priceFeed) events() []model.Event {
	f := &feed{}
	for t := int64(0); t < pf.endMs; t += 100 {
		btc := 60000.0
		if t >= 20_000 {
			btc *= 1 + pf.jumpBp/1e4
		}
		if pf.extraJumpAt > 0 && t >= pf.extraJumpAt-1000 {
			btc = 60000
			if t >= pf.extraJumpAt {
				btc *= 1 + pf.jumpBp/1e4
			}
		}
		f.quote(0, t, btc-0.05, btc+0.05)
		sol := 100.0
		if t >= 19_500 {
			sol *= 1 + pf.solPreBp/1e4
		}
		f.quote(1, t+1, sol-0.005, sol+0.005)
		xrp := 1.0
		if t >= 20_200 {
			xrp *= 1 + pf.xrpPostBp/1e4
		}
		f.quote(2, t+2, xrp-0.00005, xrp+0.00005)
	}
	sortByRecv(f.evs)
	return f.evs
}

// runA runs p over evs and returns the engine and its finished attempts.
func runA(p Params, evs []model.Event) (*Engine, []Attempt) {
	var atts []Attempt
	e := New(p, symbols, nil, nil)
	e.SetAttemptSink(func(a *Attempt) { atts = append(atts, *a) })
	run(e, evs)
	return e, atts
}

func TestPriceTriggerPostsInTheLaggingFollower(t *testing.T) {
	e, atts := runA(priceParams(), priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 1 || len(atts) != 1 {
		t.Fatalf("signals %d attempts %d skipped %v", e.Signals, len(atts), e.Skipped)
	}
	a := atts[0]
	if a.Follower != "XRPUSDT" || a.Dir != 1 || a.Leader != "BTCUSDT" {
		t.Fatalf("attempt %+v", a)
	}
	if math.Abs(a.LeaderMoveBp-6) > 0.1 || math.Abs(a.GapBp-6) > 0.1 {
		t.Fatalf("move %.3f gap %.3f", a.LeaderMoveBp, a.GapBp)
	}

	// top_k 2: SOL already moved 5 of the 6 bp (gap 1 < 2), so XRP is still alone.
	p := priceParams()
	p.TopK = 2
	_, atts = runA(p, priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: 10, endMs: 25_000}.events())
	if len(atts) != 1 || atts[0].Follower != "XRPUSDT" {
		t.Fatalf("top_k 2: attempts %+v", atts)
	}
	// Both lag: top_k 1 takes one, top_k 0 takes both.
	p.TopK = 0
	_, atts = runA(p, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if len(atts) != 2 {
		t.Fatalf("top_k 0: attempts %d", len(atts))
	}
}

func TestPriceTriggerThresholdAndCooldown(t *testing.T) {
	e, _ := runA(priceParams(), priceFeed{jumpBp: 3, endMs: 25_000}.events())
	if e.Signals != 0 {
		t.Fatalf("3 bp fired %d signals", e.Signals)
	}
	// Back down at 22 s and up again at 23 s: inside the 5 s cooldown.
	e, _ = runA(priceParams(), priceFeed{jumpBp: 6, endMs: 26_000, extraJumpAt: 23_000}.events())
	if e.Signals != 1 {
		t.Fatalf("cooldown: %d signals", e.Signals)
	}
	// After the cooldown it fires again.
	e, _ = runA(priceParams(), priceFeed{jumpBp: 6, endMs: 30_000, extraJumpAt: 27_000}.events())
	if e.Signals != 2 {
		t.Fatalf("after cooldown: %d signals", e.Signals)
	}
}

func TestSkips(t *testing.T) {
	// The followers' last quotes are ~100 ms old at the signal.
	p := priceParams()
	p.TopK, p.MaxQuoteAgeMs = 0, 1
	e, atts := runA(p, priceFeed{jumpBp: 6, endMs: 25_000}.events())
	if len(atts) != 0 || e.Skipped["stale_quote"] != 2 || e.Skipped["no_lagger"] != 1 {
		t.Fatalf("stale: attempts %d skipped %v", len(atts), e.Skipped)
	}
	var halted atomic.Bool
	halted.Store(true)
	e = New(priceParams(), symbols, &halted, nil)
	run(e, priceFeed{jumpBp: 6, endMs: 25_000}.events())
	if e.Signals != 1 || e.Open() != 0 || e.Skipped["halted"] != 1 {
		t.Fatalf("halted: signals %d open %d skipped %v", e.Signals, e.Open(), e.Skipped)
	}
}

func liqAt(evs []model.Event, tMs int64, buy bool, usd float64) []model.Event {
	evs = append(evs, model.Event{Sym: 0, Kind: model.KindLiq, RecvNs: tMs*ms + 3*ms, ExchMs: tMs, TradeMs: tMs,
		Px: e8(60000), Qty: e8(usd / 60000), BuyerMaker: !buy})
	sortByRecv(evs)
	return evs
}

func TestStrongMoveOrALiquidation(t *testing.T) {
	p := priceParams()
	p.Trigger.StrongMoveBp, p.Trigger.LiqWindowMs, p.Trigger.LiqMinUSD = 8, 1000, 50_000
	// 6 bp alone does not reach 8 bp.
	e, _ := runA(p, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 0 {
		t.Fatalf("6 bp fired without a liquidation: %d", e.Signals)
	}
	// With a same-direction liquidation 300 ms before: it fires.
	e, atts := runA(p, liqAt(priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events(), 19_700, true, 60_000))
	if e.Signals != 1 || len(atts) != 1 {
		t.Fatalf("with liquidation: signals %d attempts %d", e.Signals, len(atts))
	}
	// Too small a liquidation does not count.
	e, _ = runA(p, liqAt(priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events(), 19_700, true, 10_000))
	if e.Signals != 0 {
		t.Fatal("a small liquidation counted")
	}
	// 10 bp needs no liquidation.
	e, _ = runA(p, priceFeed{jumpBp: 10, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 1 {
		t.Fatal("a strong move did not fire")
	}
}

func TestOppositeLiquidationSkips(t *testing.T) {
	p := priceParams()
	p.Trigger.LiqWindowMs, p.Trigger.LiqMinUSD = 1000, 50_000
	e, atts := runA(p, liqAt(priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events(), 19_700, false, 60_000))
	if e.Signals != 1 || len(atts) != 0 || e.Skipped["liq_opposite"] != 1 {
		t.Fatalf("signals %d attempts %d skipped %v", e.Signals, len(atts), e.Skipped)
	}
}

func TestRollingBetaWaitsForItsWarmUp(t *testing.T) {
	p := priceParams()
	p.Trigger.Beta, p.Trigger.BetaWarmupMs = "rolling", 60_000
	e, atts := runA(p, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 1 || len(atts) != 0 || e.Skipped["beta_warmup"] != 2 || e.Skipped["no_lagger"] != 1 {
		t.Fatalf("attempts %d skipped %v", len(atts), e.Skipped)
	}
}

func TestRollingBetaScalesTheGap(t *testing.T) {
	// The followers move 2× the leader every second for 10 minutes (β = 2),
	// then BTC jumps 6 bp: XRP's gap is 12 bp, not 6.
	f := &feed{}
	btc, sol, xrp := 60000.0, 100.0, 1.0
	const end = 609_000 // past the 3 s entry timeout and the markouts
	for t := int64(0); t < end; t += 100 {
		if t%1000 == 0 && t < 600_000 {
			r := 1e-4
			if (t/1000)%2 == 1 {
				r = -1e-4
			}
			btc *= 1 + r
			sol *= 1 + 2*r
			xrp *= 1 + 2*r
		}
		b := btc
		if t >= 602_000 {
			b = btc * (1 + 6e-4)
		}
		f.quote(0, t, b-0.05, b+0.05)
		f.quote(1, t+1, sol*(1-1e-5), sol*(1+1e-5))
		f.quote(2, t+2, xrp*(1-1e-5), xrp*(1+1e-5))
	}
	sortByRecv(f.evs)
	p := priceParams()
	p.Trigger.Beta, p.Trigger.BetaHalfLifeMs, p.Trigger.BetaWarmupMs = "rolling", 60_000, 60_000
	_, atts := runA(p, f.evs)
	if len(atts) != 1 {
		t.Fatalf("attempts %d", len(atts))
	}
	if a := atts[0]; math.Abs(a.Beta-2) > 0.05 || math.Abs(a.GapBp-12) > 0.5 {
		t.Fatalf("beta %.3f gap %.3f", a.Beta, a.GapBp)
	}
}

func TestLoadParams(t *testing.T) {
	dir := t.TempDir()
	b, err := json.Marshal(priceParams())
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "shadow.json")
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Fatal(err)
	}
	p, sum, err := LoadParams(path)
	if err != nil || p.Run != "test-price" || len(sum) != 64 {
		t.Fatalf("p %+v sum %q err %v", p, sum, err)
	}
	// An unknown field (here explore-3's engine) is refused.
	if err := os.WriteFile(path, append(b[:len(b)-1], []byte(`,"engine":"quoter"}`)...), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := LoadParams(path); err == nil {
		t.Fatal("an unknown field was accepted")
	}
	for name, mut := range map[string]func(*Params){
		"horizon 0":  func(p *Params) { p.HorizonMs = 0 },
		"no trigger": func(p *Params) { p.Trigger = nil },
		"no quoter":  func(p *Params) { p.Quoter = nil },
		"no ticks":   func(p *Params) { p.Ticks = nil },
		"beta":       func(p *Params) { p.Trigger.Beta = "two" },
		"markouts":   func(p *Params) { p.MarkoutsMs = []int64{1000, 500} },
	} {
		bad := priceParams()
		mut(&bad)
		if bad.Validate() == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

func TestHistAt(t *testing.T) {
	h := newHist()
	if _, ok := h.at(5); ok {
		t.Fatal("empty hist answered")
	}
	h.push(10, 1)
	h.push(20, 1) // unchanged mid: not stored
	h.push(30, 2)
	for _, c := range []struct {
		t    int64
		want float64
		ok   bool
	}{{9, 0, false}, {10, 1, true}, {25, 1, true}, {30, 2, true}, {99, 2, true}} {
		if got, ok := h.at(c.t); ok != c.ok || got != c.want {
			t.Fatalf("at(%d) = %v %v", c.t, got, ok)
		}
	}
	if h.n != 2 {
		t.Fatalf("n = %d", h.n)
	}
}

// A non-finite number anywhere in the aggregates must not break the status JSON.
func TestStatusMarshalsWithNonFiniteNumbers(t *testing.T) {
	a := Agg{N: 2, SumNet: math.Inf(1), SumNet2: math.NaN(), SumGros: math.Inf(-1), NetUSD: math.NaN(), NMark: 1}
	a.SumMark[0] = math.NaN()
	s := a.Summarize()
	if s.MeanNet != 0 || s.MeanGross != 0 || s.NetUSD != 0 || s.Markouts[0] != 0 {
		t.Fatalf("summary %+v", s)
	}
	if _, err := json.Marshal(s); err != nil {
		t.Fatal(err)
	}
	if _, err := json.Marshal(New(priceParams(), symbols, nil, nil).Status("x")); err != nil {
		t.Fatal(err)
	}
}
