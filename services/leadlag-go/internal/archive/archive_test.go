package archive

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const header = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"

// Real rows from SOLUSDT-aggTrades-2026-10-05.
const solRows = "1103111453,121.54,231.49,3572863298,3572863347,1791158400004,true\n" +
	"1103111454,121.53,107.99,3572863348,3572863371,1791158400109,true\n" +
	"1103111455,121.54,0.01,3572863372,3572863372,1791158400110,false\n"

func TestReadCSVHeaderAndNoHeader(t *testing.T) {
	for _, in := range []string{header + solRows, solRows, strings.ReplaceAll(header+solRows, "\n", "\r\n")} {
		var got []Trade
		if err := ReadCSV(strings.NewReader(in), func(tr Trade) error { got = append(got, tr); return nil }); err != nil {
			t.Fatal(err)
		}
		if len(got) != 3 {
			t.Fatalf("got %d rows", len(got))
		}
		want := Trade{AggID: 1103111453, TradeMs: 1791158400004, Px: 12_154_000_000, Qty: 23_149_000_000, BuyerMaker: true}
		if got[0] != want {
			t.Fatalf("row 0 = %+v, want %+v", got[0], want)
		}
		if got[2].BuyerMaker || got[2].Qty != 1_000_000 {
			t.Fatalf("row 2 = %+v", got[2])
		}
	}
}

func TestReadCSVMicroseconds(t *testing.T) {
	in := "1,1.5,2,1,1,1791158400004123,false\n"
	var got Trade
	if err := ReadCSV(strings.NewReader(in), func(tr Trade) error { got = tr; return nil }); err != nil {
		t.Fatal(err)
	}
	if got.TradeMs != 1791158400004 {
		t.Fatalf("TradeMs = %d", got.TradeMs)
	}
}

func TestReadCSVRejectsGarbage(t *testing.T) {
	for _, in := range []string{"1,abc,2,1,1,5,true\n", "1,1,2,1,1,5,maybe\n", "1,1,2\n"} {
		if err := ReadCSV(strings.NewReader(in), func(Trade) error { return nil }); err == nil {
			t.Errorf("%q: no error", in)
		}
	}
}

func TestLLPRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "x.llp")
	in := []Trade{
		{AggID: 1, TradeMs: 10, Px: 5, Qty: 7, BuyerMaker: true},
		{AggID: 2, TradeMs: 11, Px: 6_400_000_000_000, Qty: 1 << 60, BuyerMaker: false},
	}
	if err := WriteLLP(path, in); err != nil {
		t.Fatal(err)
	}
	out, err := ReadAllLLP(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != 2 || out[0] != in[0] || out[1] != in[1] {
		t.Fatalf("round trip: %+v", out)
	}
}

func TestLLPTruncatedIsBad(t *testing.T) {
	path := filepath.Join(t.TempDir(), "x.llp")
	in := make([]Trade, 1000)
	for i := range in {
		in[i] = Trade{AggID: int64(i), TradeMs: int64(i)}
	}
	if err := WriteLLP(path, in); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(path)
	os.WriteFile(path, b[:len(b)/2], 0o644)
	if _, err := ReadAllLLP(path); err == nil {
		t.Fatal("truncated file read without error")
	}
}

// makeZip builds an aggTrades zip in dir and returns its path and sha256.
func makeZip(t *testing.T, dir, name, csv string) (string, string) {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, _ := zw.Create(strings.TrimSuffix(name, ".zip") + ".csv")
	w.Write([]byte(csv))
	zw.Close()
	path := filepath.Join(dir, name)
	os.WriteFile(path, buf.Bytes(), 0o644)
	s := sha256.Sum256(buf.Bytes())
	return path, hex.EncodeToString(s[:])
}

func ms(day string, h int) int64 {
	d, _ := time.Parse(time.DateOnly, day)
	return d.Add(time.Duration(h) * time.Hour).UnixMilli()
}

func TestPackZipSplitsDaysAndRecordsGaps(t *testing.T) {
	l := Layout{Root: t.TempDir()}
	csv := header +
		fmt.Sprintf("1,10,1,1,1,%d,true\n", ms("2026-09-01", 1)) +
		fmt.Sprintf("2,10,1,2,2,%d,false\n", ms("2026-09-01", 2)) +
		fmt.Sprintf("5,10,1,5,5,%d,false\n", ms("2026-09-01", 3)) + // ids 3,4 missing
		fmt.Sprintf("6,11,1,6,6,%d,false\n", ms("2026-09-02", 0)) +
		fmt.Sprintf("7,11,1,7,7,%d,true\n", ms("2026-09-03", 0))
	zp, sum := makeZip(t, t.TempDir(), "SOLUSDT-aggTrades-2026-09.zip", csv)
	ms, err := l.PackZip("SOLUSDT", zp, sum, func(d string) bool { return d != "2026-09-03" })
	if err != nil {
		t.Fatal(err)
	}
	if len(ms) != 2 {
		t.Fatalf("manifests: %d", len(ms))
	}
	m1, _ := l.ReadManifest("SOLUSDT", "2026-09-01")
	if m1.Count != 3 || m1.MissingIDs != 2 || len(m1.GapRanges) != 1 || m1.GapRanges[0] != [2]int64{2, 5} || m1.SourceSHA256 != sum {
		t.Fatalf("day 1 manifest: %+v", m1)
	}
	if l.HasDay("SOLUSDT", "2026-09-03") {
		t.Fatal("day outside keep was packed")
	}
	tr, _ := ReadAllLLP(l.LLPPath("SOLUSDT", "2026-09-02"))
	if len(tr) != 1 || tr[0].AggID != 6 {
		t.Fatalf("day 2: %+v", tr)
	}
}

func TestPackZipRejectsDayGoingBack(t *testing.T) {
	l := Layout{Root: t.TempDir()}
	csv := fmt.Sprintf("1,10,1,1,1,%d,true\n", ms("2026-09-02", 1)) +
		fmt.Sprintf("2,10,1,2,2,%d,true\n", ms("2026-09-01", 23))
	zp, sum := makeZip(t, t.TempDir(), "X-aggTrades-2026-09.zip", csv)
	if _, err := l.PackZip("X", zp, sum, nil); err == nil {
		t.Fatal("no error for a row going back a day")
	}
}

func TestPlan(t *testing.T) {
	l := Layout{Root: t.TempDir()}
	from, _ := time.Parse(time.DateOnly, "2026-08-30")
	to, _ := time.Parse(time.DateOnly, "2026-10-02")
	now, _ := time.Parse(time.DateOnly, "2026-10-07")
	jobs := Plan(l, []string{"BTCUSDT"}, from, to, now)
	var names []string
	for _, j := range jobs {
		names = append(names, j.Name())
	}
	want := "BTCUSDT-aggTrades-2026-08-30.zip BTCUSDT-aggTrades-2026-08-31.zip BTCUSDT-aggTrades-2026-09.zip " +
		"BTCUSDT-aggTrades-2026-10-01.zip BTCUSDT-aggTrades-2026-10-02.zip"
	if got := strings.Join(names, " "); got != want {
		t.Fatalf("plan:\n got %s\nwant %s", got, want)
	}
	if len(jobs[2].Days) != 30 {
		t.Fatalf("September job has %d days", len(jobs[2].Days))
	}
	// A month whose days are all packed is skipped.
	for _, d := range jobs[2].Days {
		writeAtomic(l.ManifestPath("BTCUSDT", d), []byte("{}"))
	}
	if n := len(Plan(l, []string{"BTCUSDT"}, from, to, now)); n != 4 {
		t.Fatalf("after packing September: %d jobs", n)
	}
}

func TestParseChecksum(t *testing.T) {
	s, err := ParseChecksum("fc0a6af45034ae836c0c0cdc63f84e47560d8d04cf819cb9b45d6ea7ead55f19  SOLUSDT-aggTrades-2026-10-05.zip\n")
	if err != nil || s != "fc0a6af45034ae836c0c0cdc63f84e47560d8d04cf819cb9b45d6ea7ead55f19" {
		t.Fatal(s, err)
	}
	if _, err := ParseChecksum("<html>"); err == nil {
		t.Fatal("accepted garbage")
	}
}

// fakeArchive serves zips (by URL path suffix) with correct .CHECKSUM files,
// except for names listed in corrupt, whose checksum is wrong.
func fakeArchive(t *testing.T, files map[string]string, corrupt map[string]bool) (*httptest.Server, *atomic.Int32) {
	blobs := map[string][]byte{}
	for name, csv := range files {
		p, _ := makeZip(t, t.TempDir(), name, csv)
		blobs[name], _ = os.ReadFile(p)
	}
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		name := filepath.Base(r.URL.Path)
		if strings.HasSuffix(name, ".CHECKSUM") {
			zn := strings.TrimSuffix(name, ".CHECKSUM")
			b, ok := blobs[zn]
			if !ok {
				http.NotFound(w, r)
				return
			}
			s := sha256.Sum256(b)
			h := hex.EncodeToString(s[:])
			if corrupt[zn] {
				h = strings.Repeat("0", 64)
			}
			fmt.Fprintf(w, "%s  %s\n", h, zn)
			return
		}
		b, ok := blobs[name]
		if !ok {
			http.NotFound(w, r)
			return
		}
		hits.Add(1)
		w.Write(b)
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func TestFetcherFallsBackToDailyAndVerifies(t *testing.T) {
	files := map[string]string{
		"SOLUSDT-aggTrades-2026-09-01.zip": fmt.Sprintf("1,10,1,1,1,%d,true\n", ms("2026-09-01", 1)),
		"SOLUSDT-aggTrades-2026-09-02.zip": fmt.Sprintf("2,10,1,2,2,%d,true\n", ms("2026-09-02", 1)),
	}
	srv, _ := fakeArchive(t, files, nil)
	l := Layout{Root: t.TempDir()}
	f := &Fetcher{Layout: l, BaseURL: srv.URL, Retries: 1}
	// Monthly September is not published, so the job falls back to dailies; only
	// two exist, so the other 28 come back not-found.
	job := Job{Symbol: "SOLUSDT", Month: "2026-09", Days: daysBetween(mustDay("2026-09-01"), mustDay("2026-09-30"))}
	rs := f.Run(context.Background(), []Job{job}, 2)
	var ok, nf int
	for _, r := range rs {
		switch {
		case r.Err == nil:
			ok++
		case errors.Is(r.Err, ErrNotFound):
			nf++
		default:
			t.Fatalf("unexpected error: %v", r.Err)
		}
	}
	if ok != 2 || nf != 28 {
		t.Fatalf("ok=%d not_found=%d", ok, nf)
	}
	if !l.HasDay("SOLUSDT", "2026-09-02") {
		t.Fatal("day not packed")
	}
	if _, err := os.Stat(l.RawPath("SOLUSDT", "SOLUSDT-aggTrades-2026-09-01.zip")); !os.IsNotExist(err) {
		t.Fatal("zip kept without --keep-zip")
	}
}

func TestFetcherRejectsChecksumMismatch(t *testing.T) {
	name := "SOLUSDT-aggTrades-2026-09-01.zip"
	srv, hits := fakeArchive(t, map[string]string{name: "1,10,1,1,1,1,true\n"}, map[string]bool{name: true})
	f := &Fetcher{Layout: Layout{Root: t.TempDir()}, BaseURL: srv.URL, Retries: 1}
	rs := f.Run(context.Background(), []Job{{Symbol: "SOLUSDT", Day: "2026-09-01", Days: []string{"2026-09-01"}}}, 1)
	if len(rs) != 1 || rs[0].Err == nil || !strings.Contains(rs[0].Err.Error(), "sha256") {
		t.Fatalf("results: %+v", rs)
	}
	if hits.Load() != 2 {
		t.Fatalf("downloads: %d (want 1 + 1 retry)", hits.Load())
	}
	if f.Layout.HasDay("SOLUSDT", "2026-09-01") {
		t.Fatal("packed a file that failed its checksum")
	}
}

func mustDay(s string) time.Time {
	d, err := time.Parse(time.DateOnly, s)
	if err != nil {
		panic(err)
	}
	return d
}
