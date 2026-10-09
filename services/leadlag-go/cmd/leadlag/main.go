// Command leadlag is the trader process. It keeps the /market (aggTrade) and
// /public (bookTicker) connections up, sends every frame to the observer for
// taping, and runs the record engine. In shadow mode (LL_MODE=shadow) it also
// runs the shadow engine: simulated taker fills on the live book, no orders.
//
// Live:    leadlag                          (configuration from LL_* env, see deploy/env/.env.example)
// Parity:  leadlag --replay /data/leadlag/tape/2026-10-08
// Trades:  leadlag --replay <dir> --shadow-params shadow.json --shadow-trades trades.jsonl
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"runtime/metrics"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ipc"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/live"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/shadow"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/stats"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/tape"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ws"
)

var version = "dev"

type config struct {
	Mode         string
	Leaders      []string
	Followers    []string
	WSURL        string
	ObserverSock string
	AdminAddr    string
	RingMB       int
	WaitObserver time.Duration
	ShadowParams string
}

func loadConfig() config {
	return config{
		Mode:         env("LL_MODE", "record"),
		Leaders:      list(env("LL_LEADERS", "BTCUSDT,ETHUSDT")),
		Followers:    list(env("LL_FOLLOWERS", "SOLUSDT,XRPUSDT,DOGEUSDT")),
		WSURL:        env("LL_WS_URL", "wss://fstream.binance.com"),
		ObserverSock: env("LL_OBSERVER_SOCK", "/run/leadlag/observer.sock"),
		AdminAddr:    env("LL_ADMIN_ADDR", "127.0.0.1:8060"),
		RingMB:       atoi(env("LL_RING_MB", "64")),
		WaitObserver: dur(env("LL_WAIT_OBSERVER", "30s")),
		ShadowParams: env("LL_SHADOW_PARAMS", ""),
	}
}

func main() {
	replay := flag.String("replay", "", "replay tape files under this directory (or one file) and print each run's digest")
	shadowParams := flag.String("shadow-params", "", "with --replay: run the shadow engine with these params")
	shadowTrades := flag.String("shadow-trades", "", "with --replay --shadow-params: write the shadow trades here (JSON lines)")
	shadowAll := flag.Bool("shadow-all", false, "with --shadow-params: run the shadow engine on every run, not only the runs that used these params")
	flag.Parse()
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if *replay != "" {
		if err := runReplay(*replay, *shadowParams, *shadowTrades, *shadowAll); err != nil {
			fmt.Fprintln(os.Stderr, "leadlag:", err)
			os.Exit(1)
		}
		return
	}
	cfg := loadConfig()
	if cfg.Mode != "record" && cfg.Mode != "shadow" {
		log.Error("LL_MODE must be record or shadow", "mode", cfg.Mode)
		os.Exit(2)
	}
	if cfg.Mode == "shadow" && cfg.ShadowParams == "" {
		log.Error("LL_MODE=shadow needs LL_SHADOW_PARAMS")
		os.Exit(2)
	}
	if err := run(cfg, log); err != nil {
		log.Error("exit", "err", err)
		os.Exit(1)
	}
}

func run(cfg config, log *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	symbols := append(append([]string(nil), cfg.Leaders...), cfg.Followers...)
	host, _ := os.Hostname()
	started := time.Now()
	var halted atomic.Bool // stops new shadow entries; no effect in record mode
	var params shadow.Params
	var paramsSHA string
	if cfg.Mode == "shadow" {
		var err error
		if params, paramsSHA, err = shadow.LoadParams(cfg.ShadowParams); err != nil {
			return err
		}
		for _, s := range append(append([]string(nil), params.Leaders...), params.Followers...) {
			if !contains(symbols, s) {
				return fmt.Errorf("shadow params name %s, which LL_LEADERS/LL_FOLLOWERS do not record", s)
			}
		}
	}
	prod := ipc.NewProducer(cfg.ObserverSock, cfg.RingMB<<20, ipc.Hello{
		PID: os.Getpid(), Mode: cfg.Mode, Build: version, Symbols: symbols, StartNs: started.UnixNano(), Hostname: host,
		ParamsSHA256: paramsSHA,
	}, log)
	rec := live.NewRecordEngine(len(symbols))
	var eng live.Engine = rec
	var sh *shadow.Engine
	if cfg.Mode == "shadow" {
		// Completed trades go to the observer's journal through the ring (the
		// trader never writes to disk). Marshalling allocates, once per trade.
		journal := func(t *shadow.Trade) {
			if b, err := json.Marshal(t); err == nil {
				prod.Journal(t.ExitNs, b)
			}
		}
		sh = shadow.New(params, symbols, &halted, journal)
		eng = live.Engines{rec, sh}
		log.Info("shadow engine", "run", params.Run, "exploratory", params.Exploratory, "params_sha256", paramsSHA,
			"trigger", params.TriggerKind(), "exit", params.ExitMode(), "top_k", params.TopK,
			"latency_ms", params.LatencyMs, "horizon_ms", params.HorizonMs, "notional_usd", params.NotionalUSD)
	}
	hub := live.NewHub(symbols, eng, prod)

	var wg sync.WaitGroup
	prodCtx, stopProd := context.WithCancel(context.Background())
	wg.Add(1)
	go func() { defer wg.Done(); prod.Run(prodCtx) }()

	// Wait for the observer so the tape starts with this run's hello.
	deadline := time.Now().Add(cfg.WaitObserver)
	for !prod.Connected.Load() && time.Now().Before(deadline) && ctx.Err() == nil {
		time.Sleep(100 * time.Millisecond)
	}
	if !prod.Connected.Load() {
		log.Warn("observer not connected; starting without a tape (frames are discarded until it connects)", "socket", cfg.ObserverSock)
	}

	var aggStreams, bookStreams []string
	for _, s := range symbols {
		aggStreams = append(aggStreams, strings.ToLower(s)+"@aggTrade")
		bookStreams = append(bookStreams, strings.ToLower(s)+"@bookTicker")
	}
	feeds := []*ws.Feed{
		{Name: "market", URL: ws.StreamURL(cfg.WSURL, "market", aggStreams), Log: log},
		{Name: "public", URL: ws.StreamURL(cfg.WSURL, "public", bookStreams), Log: log},
	}
	conns := []byte{ipc.ConnMarket, ipc.ConnPublic}
	feedCtx, stopFeeds := context.WithCancel(ctx)
	for i, f := range feeds {
		conn := conns[i]
		f.OnFrame = func(recvNs int64, frame []byte) { hub.OnFrame(conn, recvNs, frame) }
		f.OnState = func(atNs int64, up bool) { hub.OnConn(conn, atNs, up) }
		wg.Add(1)
		go func() { defer wg.Done(); f.Run(feedCtx) }()
	}

	st := &statusPublisher{cfg: cfg, hub: hub, eng: rec, shadow: sh, paramsSHA: paramsSHA, prod: prod, feeds: feeds, symbols: symbols, started: started}
	wg.Add(1)
	go func() { defer wg.Done(); st.run(feedCtx) }()

	srv := &http.Server{Addr: cfg.AdminAddr, Handler: adminMux(st, &halted), ReadHeaderTimeout: 5 * time.Second}
	ln, err := net.Listen("tcp", cfg.AdminAddr)
	if err != nil {
		stopFeeds()
		stopProd()
		return err
	}
	go srv.Serve(ln)
	log.Info("leadlag started", "mode", cfg.Mode, "build", version, "symbols", symbols, "admin", cfg.AdminAddr)

	<-ctx.Done()
	log.Info("shutting down")
	srv.Close()
	stopFeeds()
	// Feeds first, then let the producer drain the ring to the observer.
	time.Sleep(200 * time.Millisecond)
	stopProd()
	wg.Wait()
	s := hub.Snapshot()
	log.Info("final", "frames_market", s.Frames[0], "frames_public", s.Frames[1], "events", s.Events,
		"digest", fmt.Sprintf("%016x", s.Digest), "ring_dropped", prod.Dropped.Load(), "discarded_bytes", prod.Discarded.Load())
	return nil
}

func adminMux(st *statusPublisher, halted *atomic.Bool) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"status":"ok","halted":%t}`, halted.Load())
	})
	mux.HandleFunc("GET /v1/status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if b := st.latest.Load(); b != nil {
			w.Write(*b)
			return
		}
		w.Write([]byte(`{}`))
	})
	mux.HandleFunc("POST /v1/halt", func(w http.ResponseWriter, r *http.Request) {
		halted.Store(true)
		fmt.Fprintln(w, `{"halted":true}`)
	})
	mux.HandleFunc("POST /v1/resume", func(w http.ResponseWriter, r *http.Request) {
		halted.Store(false)
		fmt.Fprintln(w, `{"halted":false}`)
	})
	return mux
}

// statusPublisher rebuilds the status JSON every 2 s off the hot path; the
// admin handler only serves the latest bytes.
type statusPublisher struct {
	cfg    config
	hub    *live.Hub
	eng    *live.RecordEngine
	shadow *shadow.Engine // nil in record mode
	prod   *ipc.Producer
	// paramsSHA is the shadow params file's sha256.
	paramsSHA string
	feeds     []*ws.Feed
	symbols   []string
	started   time.Time
	latest    atomic.Pointer[[]byte]

	window []windowPoint // ~60 s of cumulative snapshots for windowed latency
}

type windowPoint struct{ engine, loop stats.Hist }

type feedDoc struct {
	Up          bool   `json:"up"`
	Connects    uint64 `json:"connects"`
	LastFrameNs int64  `json:"last_frame_ns"`
	LastErr     string `json:"last_err,omitempty"`
}

func (p *statusPublisher) run(ctx context.Context) {
	t := time.NewTicker(2 * time.Second)
	defer t.Stop()
	samples := []metrics.Sample{
		{Name: "/sched/pauses/total/gc:seconds"},
		{Name: "/memory/classes/heap/objects:bytes"},
		{Name: "/gc/cycles/total:gc-cycles"},
		{Name: "/sched/goroutines:goroutines"},
	}
	for {
		p.publish(samples)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (p *statusPublisher) publish(samples []metrics.Sample) {
	s := p.hub.Snapshot()
	var engState []live.SymState
	var shadowDoc map[string]any
	p.hub.WithLock(func() {
		engState = append(engState, p.eng.Last...)
		if p.shadow != nil {
			shadowDoc = p.shadow.Status(p.paramsSHA)
		}
	})
	p.window = append(p.window, windowPoint{s.EngineNs, s.LoopNs})
	if len(p.window) > 31 {
		p.window = p.window[1:]
	}
	first := &p.window[0]
	engW, loopW := s.EngineNs.Sub(&first.engine), s.LoopNs.Sub(&first.loop)

	metrics.Read(samples)
	gc := samples[0].Value.Float64Histogram()

	feeds := map[string]feedDoc{}
	for _, f := range p.feeds {
		d := feedDoc{Up: f.Up.Load(), Connects: f.Connects.Load(), LastFrameNs: f.LastFrameNs.Load()}
		if e := f.LastErr.Load(); e != nil {
			d.LastErr = *e
		}
		feeds[f.Name] = d
	}
	type symDoc struct {
		Symbol string `json:"symbol"`
		live.SymState
	}
	syms := make([]symDoc, len(p.symbols))
	for i, name := range p.symbols {
		syms[i] = symDoc{Symbol: name, SymState: engState[i]}
	}
	doc := map[string]any{
		"now_ns": time.Now().UnixNano(), "started_ns": p.started.UnixNano(), "build": version, "mode": p.cfg.Mode,
		"pid": os.Getpid(), "leaders": p.cfg.Leaders, "followers": p.cfg.Followers,
		"feeds": feeds,
		"hub": map[string]any{
			"frames_market": s.Frames[0], "frames_public": s.Frames[1], "events": s.Events,
			"parse_errors": s.ParseErrors, "unknown_symbol": s.UnknownSym, "other": s.Other,
			"dup_trades": s.DupTrades, "dup_quotes": s.DupQuotes, "trade_id_gaps": s.TradeGaps,
			"engine_ns": s.EngineNs.Summarize(), "loop_ns": s.LoopNs.Summarize(),
			"engine_ns_60s": engW.Summarize(), "loop_ns_60s": loopW.Summarize(),
			"digest": fmt.Sprintf("%016x", s.Digest),
		},
		"ipc": map[string]any{
			"observer_connected": p.prod.Connected.Load(), "connects": p.prod.Connects.Load(),
			"ring_dropped": p.prod.Dropped.Load(), "discarded_bytes": p.prod.Discarded.Load(),
			"bytes_sent": p.prod.BytesSent.Load(), "ring_cap": p.prod.RingCap(), "ring_max_used": p.prod.MaxRingUsed.Load(),
		},
		"runtime": map[string]any{
			"gomaxprocs": runtime.GOMAXPROCS(0), "goroutines": samples[3].Value.Uint64(),
			"heap_bytes": samples[1].Value.Uint64(), "gc_cycles": samples[2].Value.Uint64(),
			"gc_pause_us": gcPauses(gc),
		},
		"symbols": syms,
	}
	if shadowDoc != nil {
		doc["shadow"] = shadowDoc
	}
	b, err := json.Marshal(doc)
	if err == nil {
		p.latest.Store(&b)
	}
}

// gcPauses digests the runtime's cumulative GC pause histogram (µs).
func gcPauses(h *metrics.Float64Histogram) map[string]float64 {
	var total uint64
	for _, c := range h.Counts {
		total += c
	}
	out := map[string]float64{"n": float64(total)}
	if total == 0 {
		return out
	}
	q := func(p float64) float64 {
		rank := uint64(p*float64(total-1)) + 1
		var cum uint64
		for i, c := range h.Counts {
			cum += c
			if cum >= rank {
				return h.Buckets[i+1] * 1e6
			}
		}
		return 0
	}
	out["p50"], out["p99"] = q(0.5), q(0.99)
	for i := len(h.Counts) - 1; i >= 0; i-- {
		if h.Counts[i] > 0 {
			out["max"] = h.Buckets[i+1] * 1e6
			break
		}
	}
	return out
}

// runReplay replays tape files. With shadowParams, the shadow engine runs on the
// trader runs whose hello carries the same params sha256 (all runs with
// shadowAll), so a replay of the whole tape reproduces the live shadow trades.
func runReplay(path, shadowParams, shadowTrades string, shadowAll bool) error {
	files := []string{path}
	if fi, err := os.Stat(path); err != nil {
		return err
	} else if fi.IsDir() {
		if files, err = tape.Files(path); err != nil {
			return err
		}
	}
	newEngine := func(h ipc.Hello) live.Engine { return live.NewRecordEngine(len(h.Symbols)) }
	engines := map[int]*shadow.Engine{} // segment index → its shadow engine
	segIdx := -1
	if shadowParams != "" {
		params, sum, err := shadow.LoadParams(shadowParams)
		if err != nil {
			return err
		}
		var out *json.Encoder
		if shadowTrades != "" {
			f, err := os.Create(shadowTrades)
			if err != nil {
				return err
			}
			defer f.Close()
			out = json.NewEncoder(f)
		}
		sink := func(t *shadow.Trade) {
			if out != nil {
				_ = out.Encode(t)
			}
		}
		fmt.Printf("shadow run %s params %s\n", params.Run, sum)
		newEngine = func(h ipc.Hello) live.Engine {
			segIdx++
			rec := live.NewRecordEngine(len(h.Symbols))
			if !shadowAll && h.ParamsSHA256 != sum {
				return rec
			}
			sh := shadow.New(params, h.Symbols, nil, sink)
			engines[segIdx] = sh
			return live.Engines{rec, sh}
		}
	}
	segs, err := live.Replay(files, newEngine)
	for i, s := range segs {
		fmt.Printf("pid %-8d build %-10s start %s  frames %10d events %10d digest %016x gaps %d complete %t\n",
			s.PID, s.Build, time.Unix(0, s.StartNs).UTC().Format(time.RFC3339), s.Frames, s.Events, s.Digest, s.Gaps, s.Complete)
		if s.OpenTail {
			fmt.Printf("  open tail: the run continues past the tape (last receive %s)\n", time.Unix(0, s.LastNs).UTC().Format(time.RFC3339Nano))
		}
		if sh, ok := engines[i]; ok {
			fmt.Printf("  tape_end_ns %d\n", s.LastNs)
			t := sh.Total.Summarize()
			fmt.Printf("  shadow signals %d trades %d mean net %.3f bp hit %.3f t %.2f net $%.4f (maker exits %d)\n",
				sh.Signals, t.N, t.MeanNet, t.HitRate, t.TStat, t.NetUSD, sh.ByExit[1].N)
		}
	}
	return err
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func list(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.ToUpper(strings.TrimSpace(p)); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func atoi(s string) int {
	var n int
	fmt.Sscan(s, &n)
	return n
}

func dur(s string) time.Duration {
	d, err := time.ParseDuration(s)
	if err != nil {
		return 30 * time.Second
	}
	return d
}
