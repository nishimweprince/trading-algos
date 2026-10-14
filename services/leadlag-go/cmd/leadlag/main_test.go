package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ipc"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/live"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/observe"
)

const quoterRun = `{"run":"t-quoter","exploratory":true,"leaders":["BTCUSDT"],"followers":["SOLUSDC"],
"trigger":{"window_ms":1000,"min_move_bp":4,"cooldown_ms":5000,"min_gap_bp":2},
"latency_ms":5,"horizon_ms":3000,"markouts_ms":[500],"notional_usd":25,"taker_fee_bp":4,"max_quote_age_ms":1000,
"ticks":{"SOLUSDC":1000000},
"quoter":{"maker_fee_bp":0,"cooldown_ms":5000,"entry_timeout_ms":2000,"reverse_bp":2,"max_reprices":1,"target_gap_frac":0.6,"close_gap_bp":0.5,"stop_mult":1.5}}`

func writeParams(t *testing.T, dir, name, body string) (string, string) {
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte(body))
	return p, hex.EncodeToString(sum[:])
}

// tapeScenario tapes 30 s through a live hub and the observer: BTC jumps
// 6 bp at 20 s; SOLUSDC lags, then catches up at 21 s; a sell trade at 20.3 s
// fills the quoter's bid and a buy at 21 s its target. The hello lists the
// params file.
func tapeScenario(t *testing.T, dir string, sha string) string {
	sock, tapeDir := filepath.Join(dir, "o.sock"), filepath.Join(dir, "tape")
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	obs := observe.New(observe.Config{TapeDir: tapeDir, Log: quiet})
	octx, ostop := context.WithCancel(context.Background())
	go obs.Serve(octx, ln)
	symbols := []string{"BTCUSDT", "SOLUSDC"}
	prod := ipc.NewProducer(sock, 1<<22, ipc.Hello{PID: 7, Mode: "shadow", Symbols: symbols, StartNs: 1,
		ParamsSHA256: sha, ParamsSHA256s: []string{sha}}, quiet)
	pctx, pstop := context.WithCancel(context.Background())
	pdone := make(chan struct{})
	go func() { prod.Run(pctx); close(pdone) }()
	for !prod.Connected.Load() {
		time.Sleep(10 * time.Millisecond)
	}
	hub := live.NewHub(symbols, live.NewRecordEngine(len(symbols)), prod)
	base := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC).UnixNano()
	u := int64(0)
	book := func(recvMs int64, sym string, bid, ask float64) {
		u++
		ms := base/1e6 + recvMs
		hub.OnFrame(ipc.ConnPublic, base+recvMs*1e6, fmt.Appendf(nil,
			`{"stream":"x@bookTicker","data":{"e":"bookTicker","u":%d,"s":"%s","b":"%.2f","B":"5","a":"%.2f","A":"5","T":%d,"E":%d}}`, u, sym, bid, ask, ms, ms))
	}
	for ms := int64(0); ms < 30_000; ms += 100 {
		btc := 60000.0
		if ms >= 20_000 {
			btc *= 1.0006
		}
		book(ms, "BTCUSDT", btc-0.05, btc+0.05)
		sol := 100.0
		if ms >= 21_000 {
			sol = 100.08
		}
		book(ms+2, "SOLUSDC", sol-0.01, sol+0.01)
		if ms == 20_300 {
			e := base/1e6 + ms
			hub.OnFrame(ipc.ConnMarket, base+(ms+50)*1e6, fmt.Appendf(nil,
				`{"stream":"solusdc@aggTrade","data":{"e":"aggTrade","E":%d,"a":1,"s":"SOLUSDC","p":"99.98","q":"1","f":1,"l":1,"T":%d,"m":true}}`, e, e))
		}
		if ms == 21_000 { // the catch-up trades through the quoter's 100.03 target
			e := base/1e6 + ms
			hub.OnFrame(ipc.ConnMarket, base+(ms+3)*1e6, fmt.Appendf(nil,
				`{"stream":"solusdc@aggTrade","data":{"e":"aggTrade","E":%d,"a":2,"s":"SOLUSDC","p":"100.08","q":"1","f":2,"l":2,"T":%d,"m":false}}`, e, e))
		}
	}
	pstop()
	<-pdone
	time.Sleep(200 * time.Millisecond)
	ostop()
	if err := obs.Close(); err != nil {
		t.Fatal(err)
	}
	return tapeDir
}

func readRecords(t *testing.T, path string) map[string][]map[string]any {
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	out := map[string][]map[string]any{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var m map[string]any
		if err := json.Unmarshal(sc.Bytes(), &m); err != nil {
			t.Fatal(err)
		}
		k, _ := m["kind"].(string)
		out[m["run"].(string)+"/"+k] = append(out[m["run"].(string)+"/"+k], m)
	}
	return out
}

func TestReplayRunsTheShadowTraderAndTheControl(t *testing.T) {
	dir := t.TempDir()
	qp, qsum := writeParams(t, dir, "quoter.json", quoterRun)
	tapeDir := tapeScenario(t, dir, qsum)
	out := filepath.Join(dir, "replay.jsonl")
	if err := runReplay(tapeDir, qp, out, false, 0); err != nil {
		t.Fatal(err)
	}
	recs := readRecords(t, out)
	trades, atts := recs["t-quoter/trade"], recs["t-quoter/attempt"]
	if len(trades) != 1 || len(atts) != 1 {
		t.Fatalf("records: trades %d attempts %d", len(trades), len(atts))
	}
	if trades[0]["exit_reason"] != "target" || atts[0]["filled"] != true {
		t.Fatalf("trade %v\nattempt %v", trades[0], atts[0])
	}
	// A params file the tape's runs did not use replays nothing without --shadow-all.
	other, _ := writeParams(t, dir, "other.json", quoterRun[:len(quoterRun)-1]+` ,"top_k":2}`)
	if err := runReplay(tapeDir, other, out, false, 0); err != nil {
		t.Fatal(err)
	}
	if n := len(readRecords(t, out)); n != 0 {
		t.Fatalf("unused params replayed %d groups", n)
	}
	if err := runReplay(tapeDir, other, out, true, 0); err != nil {
		t.Fatal(err)
	}
	if n := len(readRecords(t, out)["t-quoter/trade"]); n != 1 {
		t.Fatalf("--shadow-all replayed %d trades", n)
	}
	// The +1 h placebo: the leader's jump now lands after the tape, so no signal fires.
	if err := runReplay(tapeDir, qp, out, false, time.Hour); err != nil {
		t.Fatal(err)
	}
	if n := len(readRecords(t, out)); n != 0 {
		t.Fatalf("control produced %d groups of records", n)
	}
	// A 9 s shift keeps the jump on the tape (at 29 s), after the follower caught up: no lagger.
	if err := runReplay(tapeDir, qp, out, false, 9*time.Second); err != nil {
		t.Fatal(err)
	}
	if n := len(readRecords(t, out)["t-quoter/attempt"]); n != 0 {
		t.Fatalf("shifted control posted %d entries", n)
	}
}

// A params file written for an older engine is refused, not half applied.
func TestOldParamsAreRefused(t *testing.T) {
	dir := t.TempDir()
	old, _ := writeParams(t, dir, "old.json", `{"run":"explore-3a","exploratory":true,"engine":"quoter","leaders":["BTCUSDT"],"followers":["SOLUSDC"],
"trigger":{"kind":"price","window_ms":1000,"min_move_bp":4,"cooldown_ms":5000,"min_gap_bp":2},
"latency_ms":5,"horizon_ms":3000,"notional_usd":25,"taker_fee_bp":4,"max_quote_age_ms":1000,"ticks":{"SOLUSDC":1000000},
"quoter":{"arm":"A","maker_fee_bp":0,"cooldown_ms":5000,"entry_timeout_ms":2000}}`)
	if err := runReplay(dir, old, "", false, 0); err == nil {
		t.Fatal("an explore-3 params file was accepted")
	}
}
