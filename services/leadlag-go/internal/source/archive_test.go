package source

import (
	"io"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/archive"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

func day(s string) time.Time {
	d, err := time.Parse(time.DateOnly, s)
	if err != nil {
		panic(err)
	}
	return d
}

// writeDays packs random trades for each symbol across two days.
func writeDays(t *testing.T, l archive.Layout, syms []string, seed uint64) map[string]int {
	t.Helper()
	r := rand.New(rand.NewPCG(seed, 1))
	counts := map[string]int{}
	for _, sym := range syms {
		id := int64(1000)
		for _, d := range []string{"2026-09-01", "2026-09-02"} {
			base := day(d).UnixMilli()
			var ts []archive.Trade
			msOff := int64(0)
			for range 500 + r.IntN(500) {
				msOff += int64(r.IntN(3)) // many same-ms prints, across symbols too
				ts = append(ts, archive.Trade{AggID: id, TradeMs: base + msOff, Px: 1 + int64(r.IntN(100)), Qty: 1, BuyerMaker: r.IntN(2) == 0})
				id++
			}
			if err := archive.WriteLLP(l.LLPPath(sym, d), ts); err != nil {
				t.Fatal(err)
			}
			// a manifest marks the day packed
			if err := writeManifest(l, sym, d); err != nil {
				t.Fatal(err)
			}
			counts[sym] += len(ts)
		}
	}
	return counts
}

func writeManifest(l archive.Layout, sym, d string) error {
	return writeFile(l.ManifestPath(sym, d), []byte(`{"version":1}`))
}

func drain(t *testing.T, src Source) []model.Event {
	t.Helper()
	var out []model.Event
	var e model.Event
	for {
		err := src.Next(&e)
		if err == io.EOF {
			return out
		}
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, e)
	}
}

func TestArchiveMergeOrderAndDeterminism(t *testing.T) {
	l := archive.Layout{Root: t.TempDir()}
	syms := []string{"BTCUSDT", "ETHUSDT", "SOLUSDT"}
	counts := writeDays(t, l, syms, 7)
	cfg := ArchiveConfig{Layout: l, Symbols: syms, From: day("2026-09-01"), To: day("2026-09-02")}

	a, err := OpenArchive(cfg)
	if err != nil {
		t.Fatal(err)
	}
	first := drain(t, a)
	a.Close()

	total := 0
	for _, c := range counts {
		total += c
	}
	if len(first) != total {
		t.Fatalf("merged %d events, want %d", len(first), total)
	}
	perSym := map[uint16]int64{}
	for i, e := range first {
		if i > 0 {
			p := first[i-1]
			if e.TradeMs < p.TradeMs || (e.TradeMs == p.TradeMs && e.Sym < p.Sym) ||
				(e.TradeMs == p.TradeMs && e.Sym == p.Sym && e.ID < p.ID) {
				t.Fatalf("event %d out of order: %+v after %+v", i, e, p)
			}
		}
		if last, ok := perSym[e.Sym]; ok && e.ID != last+1 {
			t.Fatalf("sym %d skipped from id %d to %d", e.Sym, last, e.ID)
		}
		perSym[e.Sym] = e.ID
		if e.Kind != model.KindTrade || e.ExchMs != e.TradeMs {
			t.Fatalf("bad event %+v", e)
		}
	}

	b, _ := OpenArchive(cfg)
	second := drain(t, b)
	b.Close()
	for i := range first {
		if first[i] != second[i] {
			t.Fatalf("run 2 differs at %d", i)
		}
	}
}

func TestArchiveMissingDay(t *testing.T) {
	l := archive.Layout{Root: t.TempDir()}
	writeDays(t, l, []string{"BTCUSDT"}, 1)
	cfg := ArchiveConfig{Layout: l, Symbols: []string{"BTCUSDT"}, From: day("2026-09-01"), To: day("2026-09-03")}
	if _, err := OpenArchive(cfg); err == nil {
		t.Fatal("missing day accepted")
	}
	cfg.AllowMissing = true
	a, err := OpenArchive(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	cfg.AllowMissing = false
	cfg.SkipDays = map[string]bool{"2026-09-03": true}
	b, err := OpenArchive(cfg)
	if err != nil {
		t.Fatal(err)
	}
	b.Close()
}
