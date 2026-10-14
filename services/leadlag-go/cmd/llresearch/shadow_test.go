package main

import (
	"math"
	"strings"
	"testing"
	"time"
)

func TestShadowReportKillCriteria(t *testing.T) {
	day0 := time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)
	var ts []shadowTrade
	for i := range 600 {
		at := day0.Add(time.Duration(i%6) * 24 * time.Hour)
		ts = append(ts, shadowTrade{Run: "r", ExitNs: at.UnixNano(), GrossBp: 1 + float64(i%3)*0.1, NetBp: -3, ExitKind: "taker", EntryKind: "maker"})
	}
	rep := shadowReport("r", ts, nanF(), nanF(), true, 200)
	if rep.Trades != 600 || rep.Days != 6 || !strings.HasPrefix(rep.Verdict, "stop: mean net") {
		t.Fatalf("%+v", rep)
	}
	// Re-priced at 0 maker / 0 taker: net = gross > 0, but every exit crossed: f = 1.
	rep = shadowReport("r", ts, 0, 0, true, 200)
	if rep.Net.Mean <= 0 || !strings.HasPrefix(rep.Verdict, "stop: taker-exit") {
		t.Fatalf("%+v", rep)
	}
	if rep = shadowReport("r", ts[:10], nanF(), nanF(), false, 200); rep.Verdict != "continue" {
		t.Fatalf("%+v", rep)
	}
}

func TestPart5Verdict(t *testing.T) {
	day0 := time.Date(2026, 10, 15, 12, 0, 0, 0, time.UTC)
	mk := func(n, days int, net float64, takerEvery int) []shadowTrade {
		var ts []shadowTrade
		for i := range n {
			kind := "maker"
			if takerEvery > 0 && i%takerEvery == 0 {
				kind = "taker"
			}
			at := day0.Add(time.Duration(i%days) * 24 * time.Hour)
			ts = append(ts, shadowTrade{Run: "r", ExitNs: at.UnixNano(), NetBp: net + float64(i%5)*0.1, ExitKind: kind, EntryKind: "maker"})
		}
		return ts
	}
	for _, c := range []struct {
		name string
		ts   []shadowTrade
		want string
	}{
		{"taker share", mk(250, 3, 1, 3), "kill: taker"},
		{"too early", mk(300, 8, 1, 0), "continue"},
		{"too short", mk(500, 5, 1, 0), "continue"},
		{"negative", mk(500, 8, -2, 0), "kill: 95% CI upper"},
		{"positive", mk(500, 8, 1, 0), "pass"},
		{"spans 0", mk(500, 8, -0.2, 0), "extend"},
		{"inconclusive", mk(800, 15, -0.2, 0), "stop: inconclusive"},
	} {
		got := part5Verdict(shadowReport("r", c.ts, nanF(), nanF(), false, 500))
		if !strings.HasPrefix(got, c.want) {
			t.Fatalf("%s: %q", c.name, got)
		}
	}
}

func nanF() float64 { return math.NaN() }
