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

func nanF() float64 { return math.NaN() }
