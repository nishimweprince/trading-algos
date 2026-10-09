package shadow

import (
	"math"
	"testing"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

func gapParams() Params {
	p := priceParams()
	p.Exit = &ExitCfg{Mode: "gap", MakerTimeoutMs: 1000, MakerFeeBp: 0, CloseGapBp: 0.5, StopMult: 1.5}
	p.TakerFeeBp = 4
	return p
}

func TestGapExitStartsWhenTheFollowerCatchesUp(t *testing.T) {
	e := New(gapParams(), symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Total.N != 1 {
		t.Fatalf("trades %d skipped %v", e.Total.N, e.Skipped)
	}
	tr := e.Recent()[0]
	held := (tr.ExitNs - tr.EntryNs) / ms
	// XRP closes the gap 200 ms after the signal; the resting ask times out 1 s later (the book stays flat).
	if tr.ExitReason != "gap_closed" || held > 1500 || tr.ExitKind != "taker" || tr.FeesBp != 8 {
		t.Fatalf("trade %+v held %d ms", tr, held)
	}
	if e.ByReason[reasonIndex("gap_closed")].N != 1 {
		t.Fatal("by_exit_reason")
	}
}

func TestGapExitStop(t *testing.T) {
	e := New(gapParams(), symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, solPreBp: 5, xrpPostBp: -10, endMs: 25_000}.events())
	tr := e.Recent()[0]
	if tr.ExitReason != "stop" || tr.ExitKind != "taker" || tr.GrossBp > -9 {
		t.Fatalf("trade %+v", tr)
	}
}

func TestTakerExitsKeepTheirReason(t *testing.T) {
	p := priceParams()
	e := New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if tr := e.Recent()[0]; tr.ExitReason != "timeout" || tr.Beta != 0 {
		t.Fatalf("trade %+v", tr)
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
	e := New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 0 {
		t.Fatalf("6 bp fired without a liquidation: %d", e.Signals)
	}
	// With a same-direction liquidation 300 ms before: it fires.
	e = New(p, symbols, nil, nil)
	run(e, liqAt(priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events(), 19_700, true, 60_000))
	if e.Signals != 1 || e.Total.N != 1 {
		t.Fatalf("with liquidation: signals %d trades %d", e.Signals, e.Total.N)
	}
	// Too small a liquidation does not count.
	e = New(p, symbols, nil, nil)
	run(e, liqAt(priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events(), 19_700, true, 10_000))
	if e.Signals != 0 {
		t.Fatal("a small liquidation counted")
	}
	// 10 bp needs no liquidation.
	e = New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 10, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 1 {
		t.Fatal("a strong move did not fire")
	}
}

func TestOppositeLiquidationSkips(t *testing.T) {
	p := priceParams()
	p.Trigger.LiqWindowMs, p.Trigger.LiqMinUSD = 1000, 50_000
	e := New(p, symbols, nil, nil)
	run(e, liqAt(priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events(), 19_700, false, 60_000))
	if e.Signals != 1 || e.Total.N != 0 || e.Skipped["liq_opposite"] != 1 {
		t.Fatalf("signals %d trades %d skipped %v", e.Signals, e.Total.N, e.Skipped)
	}
}

func TestRollingBetaWaitsForItsWarmUp(t *testing.T) {
	p := priceParams()
	p.Trigger.Beta, p.Trigger.BetaWarmupMs = "rolling", 60_000
	e := New(p, symbols, nil, nil)
	run(e, priceFeed{jumpBp: 6, xrpPostBp: 10, endMs: 25_000}.events())
	if e.Signals != 1 || e.Total.N != 0 || e.Skipped["beta_warmup"] != 2 || e.Skipped["no_lagger"] != 1 {
		t.Fatalf("trades %d skipped %v", e.Total.N, e.Skipped)
	}
}

func TestRollingBetaScalesTheGap(t *testing.T) {
	// The followers move 2× the leader every second for 10 minutes (β = 2),
	// then BTC jumps 6 bp: XRP's gap is 12 bp, not 6.
	f := &feed{}
	btc, sol, xrp := 60000.0, 100.0, 1.0
	const end = 605_000
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
	e := New(p, symbols, nil, nil)
	run(e, f.evs)
	if e.Total.N != 1 {
		t.Fatalf("trades %d skipped %v", e.Total.N, e.Skipped)
	}
	tr := e.Recent()[0]
	if math.Abs(tr.Beta-2) > 0.05 || math.Abs(tr.GapBp-12) > 0.5 {
		t.Fatalf("beta %.3f gap %.3f", tr.Beta, tr.GapBp)
	}
}
