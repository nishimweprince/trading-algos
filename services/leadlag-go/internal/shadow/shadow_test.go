package shadow

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"sync/atomic"
	"testing"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/research"
)

const ms = int64(1_000_000)

var symbols = []string{"BTCUSDT", "SOLUSDT", "XRPUSDT"}

func params() Params {
	return Params{
		Run: "test", Exploratory: true, Leaders: []string{"BTCUSDT"}, Followers: []string{"SOLUSDT", "XRPUSDT"},
		Detector:  research.DetectorCfg{WMs: 500, Theta: 3, CooldownMs: 1000, HalfLifeMs: 60_000, WarmupMs: 10_000, MaxGapMs: 600_000},
		LatencyMs: 10, HorizonMs: 1000, NotionalUSD: 25, TakerFeeBp: 5, MaxQuoteAgeMs: 1000,
	}
}

func e8(v float64) int64 { return int64(math.Round(v * model.Scale)) }

// feed builds the event stream: 20 s of balanced small leader flow (so the
// detector's scale is warm), follower quotes every 100 ms, then at burstMs a
// burst of large taker buys (sign +1) or sells (−1); after the burst the
// followers' book moves by moveBp in the burst's direction.
type feed struct {
	evs []model.Event
}

func (f *feed) leader(tMs int64, qty float64, buy bool) {
	f.evs = append(f.evs, model.Event{Sym: 0, Kind: model.KindTrade, RecvNs: tMs*ms + 2*ms, TradeMs: tMs, ExchMs: tMs,
		Px: e8(60000), Qty: e8(qty), BuyerMaker: !buy})
}

func (f *feed) quote(sym uint16, tMs int64, bid, ask float64) {
	f.evs = append(f.evs, model.Event{Sym: sym, Kind: model.KindQuote, RecvNs: tMs * ms, ExchMs: tMs, Bid: e8(bid), Ask: e8(ask)})
}

func scenario(sign int, moveBp float64, quotes bool) []model.Event {
	f := &feed{}
	const burst = 20_000
	for t := int64(0); t < 23_000; t += 100 {
		if t < burst || t > burst+300 {
			f.leader(t, 0.01, (t/100)%2 == 0) // ~$600 alternating
		}
		if quotes || t < burst {
			mid := 100.0
			if t > burst+50 {
				mid *= 1 + float64(sign)*moveBp/1e4
			}
			f.quote(1, t+1, mid-0.01, mid+0.01)
			f.quote(2, t+2, mid/100-0.0001, mid/100+0.0001)
		}
	}
	for k := int64(0); k < 5; k++ { // the burst: $600k of one-sided flow
		f.leader(burst+k*10, 2, sign > 0)
	}
	// the stream must be in receive order, as the hub delivers it
	sortByRecv(f.evs)
	return f.evs
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

func TestALeaderBurstOpensAndClosesTakerTradesInTheFollowers(t *testing.T) {
	e := New(params(), symbols, nil, nil)
	run(e, scenario(+1, 20, true))
	if e.Signals != 1 {
		t.Fatalf("signals = %d, want 1", e.Signals)
	}
	if e.Total.N != 2 || e.Open() != 0 {
		t.Fatalf("trades = %d open = %d, want 2 and 0", e.Total.N, e.Open())
	}
	for _, tr := range e.Recent() {
		if tr.Dir != 1 || tr.Leader != "BTCUSDT" || !tr.Exploratory || tr.Run != "test" {
			t.Fatalf("trade %+v", tr)
		}
		// long: bought at the ask after the latency, sold at the bid after the horizon
		if tr.EntryNs-tr.SignalNs < 10*ms || tr.ExitNs-tr.EntryNs < 1000*ms {
			t.Fatalf("timing: entry %d ms after signal, exit %d ms after entry", (tr.EntryNs-tr.SignalNs)/ms, (tr.ExitNs-tr.EntryNs)/ms)
		}
		if tr.FeesBp != 10 || math.Abs(tr.NetBp-(tr.GrossBp-10)) > 1e-3 {
			t.Fatalf("fees: %+v", tr)
		}
		if tr.GrossBp < 15 || tr.GrossBp > 20 { // 20 bp move less the half-spreads paid
			t.Fatalf("gross %.3f bp, want 15-20", tr.GrossBp)
		}
		if math.Abs(tr.Qty*tr.EntryPx-25) > 1e-9 || math.Abs(tr.NetUSD-tr.NetBp/1e4*25) > 1e-5 {
			t.Fatalf("sizing: %+v", tr)
		}
	}
	if s := e.Total.Summarize(); s.N != 2 || s.HitRate != 1 {
		t.Fatalf("summary %+v", s)
	}
}

func TestASellBurstShortsAtTheBid(t *testing.T) {
	e := New(params(), symbols, nil, nil)
	run(e, scenario(-1, 20, true))
	if e.Total.N != 2 {
		t.Fatalf("trades = %d", e.Total.N)
	}
	for _, tr := range e.Recent() {
		if tr.Dir != -1 || tr.GrossBp < 15 {
			t.Fatalf("short trade %+v", tr)
		}
	}
}

func TestSkips(t *testing.T) {
	// No follower quote after the burst: the last one is older than max_quote_age_ms at the entry.
	p := params()
	p.MaxQuoteAgeMs = 1
	e := New(p, symbols, nil, nil)
	run(e, scenario(+1, 20, false))
	if e.Total.N != 0 || e.Skipped["stale_quote"] != 2 {
		t.Fatalf("stale: trades %d skipped %v", e.Total.N, e.Skipped)
	}

	var halted atomic.Bool
	halted.Store(true)
	e = New(params(), symbols, &halted, nil)
	run(e, scenario(+1, 20, true))
	if e.Signals != 1 || e.Total.N != 0 || e.Skipped["halted"] != 1 {
		t.Fatalf("halted: signals %d trades %d skipped %v", e.Signals, e.Total.N, e.Skipped)
	}

	// A follower with no quote at all.
	p = params()
	p.Followers = append(p.Followers, "DOGEUSDT")
	e = New(p, append(append([]string(nil), symbols...), "DOGEUSDT"), nil, nil)
	run(e, scenario(+1, 20, true))
	if e.Total.N != 2 || e.Skipped["no_quote"] != 1 {
		t.Fatalf("no quote: trades %d skipped %v", e.Total.N, e.Skipped)
	}
}

func TestAPositionBlocksANewEntryUntilItCloses(t *testing.T) {
	p := params()
	p.HorizonMs = 3000 // the first positions are still open during the second burst
	e := New(p, symbols, nil, nil)
	evs := scenario(+1, 20, true)
	// a second burst 1.0 s later: past the 1 s cooldown, while the first positions are still open
	f := &feed{evs: evs}
	for k := int64(0); k < 5; k++ {
		f.leader(21_000+k*10, 3, true)
	}
	f.quote(1, 24_000, 100.19, 100.21) // after the 3 s exits
	f.quote(2, 24_001, 1.0019, 1.0021)
	sortByRecv(f.evs)
	run(e, f.evs)
	if e.Signals != 2 || e.Skipped["busy"] != 2 || e.Total.N != 2 || e.Open() != 0 {
		t.Fatalf("signals %d trades %d skipped %v", e.Signals, e.Total.N, e.Skipped)
	}
}

func TestSameEventsSameTrades(t *testing.T) {
	evs := scenario(+1, 20, true)
	var a, b []Trade
	ea := New(params(), symbols, nil, func(t *Trade) { a = append(a, *t) })
	eb := New(params(), symbols, nil, func(t *Trade) { b = append(b, *t) })
	run(ea, evs)
	run(eb, evs)
	if len(a) != 2 || !reflect.DeepEqual(a, b) || !reflect.DeepEqual(ea.Total, eb.Total) {
		t.Fatalf("replay differs:\n%+v\n%+v", a, b)
	}
}

func TestLoadParams(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "shadow.json")
	if err := os.WriteFile(path, []byte(`{"run":"explore-1","exploratory":true,"leaders":["BTCUSDT"],"followers":["SOLUSDT"],
"detector":{"w_ms":500,"theta":3,"cooldown_ms":1000,"half_life_ms":1800000,"warmup_ms":1800000,"max_gap_ms":600000},
"latency_ms":10,"horizon_ms":1000,"notional_usd":25,"taker_fee_bp":5,"max_quote_age_ms":1000}`), 0o644); err != nil {
		t.Fatal(err)
	}
	p, sum, err := LoadParams(path)
	if err != nil || p.Run != "explore-1" || len(sum) != 64 || p.Detector != research.PrimaryDetector() {
		t.Fatalf("p %+v sum %q err %v", p, sum, err)
	}
	bad := params()
	bad.HorizonMs = 0
	if bad.Validate() == nil {
		t.Fatal("horizon 0 accepted")
	}
}

func TestFlowParamsKeepTheExplore1Defaults(t *testing.T) {
	p := params()
	if p.TriggerKind() != "flow" || p.ExitMode() != "taker" {
		t.Fatalf("trigger %s exit %s", p.TriggerKind(), p.ExitMode())
	}
	e := New(p, symbols, nil, nil)
	run(e, scenario(+1, 20, true))
	for _, tr := range e.Recent() {
		if tr.ExitKind != "taker" || tr.FeesBp != 10 || tr.MarkoutsBp != nil || tr.GapBp != 0 {
			t.Fatalf("flow trade %+v", tr)
		}
	}
	bad := params()
	bad.TopK = 2
	if bad.Validate() == nil {
		t.Fatal("top_k with the flow trigger accepted")
	}
	bad = params()
	bad.MarkoutsMs = []int64{500, 2000}
	if bad.Validate() == nil {
		t.Fatal("a markout past the horizon accepted")
	}
}

// Price trigger: BTC is the leader, SOL and XRP follow (1 bp spreads).
func priceParams() Params {
	return Params{
		Run: "test-price", Exploratory: true, Leaders: []string{"BTCUSDT"}, Followers: []string{"SOLUSDT", "XRPUSDT"},
		Trigger:   &TriggerCfg{Kind: "price", WindowMs: 1000, MinMoveBp: 4, CooldownMs: 5000, MinGapBp: 2},
		TopK:      1,
		LatencyMs: 10, HorizonMs: 2000, MarkoutsMs: []int64{100, 1000, 2000},
		NotionalUSD: 25, TakerFeeBp: 5, MaxQuoteAgeMs: 1000,
	}
}

// priceFeed: quotes every 100 ms until endMs. BTC's mid is 60000 and jumps by
// jumpBp at 20 s. SOL moves by solPreBp at 19.5 s (before the jump: it no
// longer lags); XRP moves by xrpPostBp at 20.2 s (after the entry: it catches
// up). xrpBidAfter, if > 0, lifts XRP's whole book from xrpBidAt so its bid
// reaches that price.
type priceFeed struct {
	jumpBp, solPreBp, xrpPostBp float64
	endMs                       int64
	extraJumpAt                 int64 // > 0: BTC falls back at extraJumpAt−1000 and jumps again here
	xrpBidAfter                 float64
	xrpBidAt                    int64
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
		if pf.xrpBidAfter > 0 && t >= pf.xrpBidAt {
			xrp = pf.xrpBidAfter + 0.00005
		}
		f.quote(2, t+2, xrp-0.00005, xrp+0.00005)
	}
	sortByRecv(f.evs)
	return f.evs
}

func TestPriceTriggerOpensTheLaggingFollower(t *testing.T) {
	e := New(priceParams(), symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 1 || e.Total.N != 1 || e.Open() != 0 {
		t.Fatalf("signals %d trades %d open %d skipped %v", e.Signals, e.Total.N, e.Open(), e.Skipped)
	}
	tr := e.Recent()[0]
	if tr.Follower != "XRPUSDT" || tr.Dir != 1 || tr.ExitKind != "taker" {
		t.Fatalf("trade %+v", tr)
	}
	if math.Abs(tr.LeaderMoveBp-6) > 0.1 || math.Abs(tr.GapBp-6) > 0.1 || math.Abs(tr.PreMoveBp) > 1e-6 {
		t.Fatalf("move %.3f gap %.3f pre %.3f", tr.LeaderMoveBp, tr.GapBp, tr.PreMoveBp)
	}
	if tr.GrossBp < 8 || tr.GrossBp > 10 { // 10 bp move less the half-spreads
		t.Fatalf("gross %.3f", tr.GrossBp)
	}
	// +100 ms samples the book before XRP's move lands (2 ms later); +1 s and +2 s see it.
	if len(tr.MarkoutsBp) != 3 || tr.MarkoutsBp[0] != 0 || tr.MarkoutsBp[1] < 9 || tr.MarkoutsBp[2] < 9 {
		t.Fatalf("markouts %v", tr.MarkoutsBp)
	}
	if s := e.Total.Summarize(); len(s.Markouts) != 3 {
		t.Fatalf("summary markouts %v", s.Markouts)
	}

	// top_k 2: SOL already moved 5 of the 6 bp (gap 1 < 2), so XRP still trades alone.
	p := priceParams()
	p.TopK = 2
	e = New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Total.N != 1 || e.Recent()[0].Follower != "XRPUSDT" {
		t.Fatalf("top_k 2: trades %d", e.Total.N)
	}
	// Both lag: top_k 1 takes one, top_k 0 takes both.
	p.TopK = 0
	e = New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Total.N != 2 {
		t.Fatalf("top_k 0: trades %d", e.Total.N)
	}
}

func TestPriceTriggerThresholdAndCooldown(t *testing.T) {
	e := New(priceParams(), symbols, nil, nil)
	run(e, priceFeed{jumpBp: 3, endMs: 25_000}.events())
	if e.Signals != 0 {
		t.Fatalf("3 bp fired %d signals", e.Signals)
	}
	// Back down at 22 s and up again at 23 s: inside the 5 s cooldown.
	e = New(priceParams(), symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, endMs: 26_000, extraJumpAt: 23_000}.events())
	if e.Signals != 1 {
		t.Fatalf("cooldown: %d signals", e.Signals)
	}
	// After the cooldown it fires again.
	e = New(priceParams(), symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, endMs: 30_000, extraJumpAt: 27_000}.events())
	if e.Signals != 2 {
		t.Fatalf("after cooldown: %d signals", e.Signals)
	}
}

func TestEntrySpreadFilter(t *testing.T) {
	p := priceParams()
	p.MaxEntrySpreadBp = 0.5 // the feed's spreads are 1 bp
	e := New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 1 || e.Total.N != 0 || e.Skipped["wide_spread"] != 1 || e.Open() != 0 {
		t.Fatalf("trades %d skipped %v", e.Total.N, e.Skipped)
	}
}

func TestMakerExit(t *testing.T) {
	p := priceParams()
	p.Exit = &ExitCfg{Mode: "maker", MakerTimeoutMs: 1000, MakerFeeBp: 2}

	// The book lifts through our resting ask after the horizon: a maker fill at the limit.
	e := New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: 10, endMs: 25_000, xrpBidAfter: 1.0015, xrpBidAt: 22_500}.events())
	if e.Total.N != 1 {
		t.Fatalf("trades %d open %d", e.Total.N, e.Open())
	}
	tr := e.Recent()[0]
	if tr.ExitKind != "maker" || tr.FeesBp != 7 || math.Abs(tr.ExitPx-1.00105) > 1e-9 {
		t.Fatalf("maker trade %+v", tr)
	}
	if s := e.ByExit[1].Summarize(); s.N != 1 {
		t.Fatalf("by_exit maker %+v", s)
	}

	// A flat book never reaches the limit: a taker exit after the timeout.
	e = New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: 10, endMs: 25_000}.events())
	tr = e.Recent()[0]
	if tr.ExitKind != "taker" || tr.FeesBp != 10 || (tr.ExitNs-tr.EntryNs)/ms < 3000 {
		t.Fatalf("timeout trade %+v (held %d ms)", tr, (tr.ExitNs-tr.EntryNs)/ms)
	}
}

func TestPriceReplayIsDeterministic(t *testing.T) {
	evs := priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events()
	p := priceParams()
	p.TopK = 0
	var a, b []Trade
	ea := New(p, symbols, nil, func(t *Trade) { a = append(a, *t) })
	eb := New(p, symbols, nil, func(t *Trade) { b = append(b, *t) })
	run(ea, evs)
	run(eb, evs)
	if len(a) != 2 || !reflect.DeepEqual(a, b) {
		t.Fatalf("replay differs:\n%+v\n%+v", a, b)
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
}
