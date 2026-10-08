package shadow

import (
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
	e := New(params(), symbols, nil, nil)
	p := params()
	p.MaxQuoteAgeMs = 1
	e = New(p, symbols, nil, nil)
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
