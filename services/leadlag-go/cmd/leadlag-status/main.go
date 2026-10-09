// Command leadlag-status serves the read-only status page. It polls the
// trader (:8060/v1/status) and the observer (:8061/v1/stats) every 5 s and
// answers every request from that cache, so viewers never reach the trader.
// It is the only process published through cloudflared.
package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"html/template"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"
)

var version = "dev"

//go:embed page.html
var pageHTML string

type cache struct {
	mu       sync.RWMutex
	trader   json.RawMessage
	observer json.RawMessage
	probe    json.RawMessage
	traderAt time.Time
	obsAt    time.Time
	traderEr string
	obsEr    string
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	addr := env("LL_STATUS_ADDR", "127.0.0.1:8062")
	traderURL := env("LL_TRADER_STATUS_URL", "http://127.0.0.1:8060/v1/status")
	obsURL := env("LL_OBSERVER_STATS_URL", "http://127.0.0.1:8061/v1/stats")
	probeFile := env("LL_PROBE_FILE", "/home/ll/leadlag/probe.json")

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	c := &cache{}
	client := &http.Client{Timeout: 3 * time.Second}
	go poll(ctx, client, c, traderURL, obsURL, probeFile)

	srv := &http.Server{Addr: addr, Handler: newMux(c), ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		srv.Close()
	}()
	log.Info("status started", "addr", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("http", "err", err)
		os.Exit(1)
	}
}

func newMux(c *cache) *http.ServeMux {
	tmpl := template.Must(template.New("page").Parse(pageHTML))
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("GET /api/summary", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		json.NewEncoder(w).Encode(c.summary())
	})
	// Every performance metric as one downloadable JSON file, without the
	// individual events (the shadow run's recent trades become a count).
	mux.HandleFunc("GET /api/metrics", func(w http.ResponseWriter, r *http.Request) {
		now := time.Now().UTC()
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Disposition",
			`attachment; filename="leadlag-metrics-`+now.Format("20060102T150405Z")+`.json"`)
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		enc.Encode(c.metrics(now))
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		tmpl.Execute(w, map[string]string{"Version": version})
	})
	return mux
}

// metrics is the download: the cached trader, observer and probe documents,
// with lists of individual events replaced by their counts.
func (c *cache) metrics(now time.Time) map[string]any {
	c.mu.RLock()
	defer c.mu.RUnlock()
	age := func(t time.Time) any {
		if t.IsZero() {
			return nil
		}
		return now.Sub(t).Round(time.Millisecond).Seconds()
	}
	return map[string]any{
		"generated_at": now.Format(time.RFC3339), "status_version": version,
		"source_age_s": map[string]any{"trader": age(c.traderAt), "observer": age(c.obsAt)},
		"errors":       map[string]string{"trader": c.traderEr, "observer": c.obsEr},
		"trader":       withoutEvents(c.trader),
		"observer":     decode(c.observer),
		"probe":        decode(c.probe),
	}
}

// withoutEvents decodes the trader document and replaces each shadow run's
// list of recent trades with its length.
func withoutEvents(raw json.RawMessage) any {
	doc, ok := decode(raw).(map[string]any)
	if !ok {
		return nil
	}
	strip := func(v any) {
		if sh, ok := v.(map[string]any); ok {
			if recent, ok := sh["recent"].([]any); ok {
				sh["recent_trades_omitted"] = len(recent)
			}
			delete(sh, "recent")
		}
	}
	strip(doc["shadow"])
	if runs, ok := doc["shadow_runs"].([]any); ok {
		for _, r := range runs {
			strip(r)
		}
	}
	return doc
}

func decode(raw json.RawMessage) any {
	if len(raw) == 0 {
		return nil
	}
	var v any
	if json.Unmarshal(raw, &v) != nil {
		return nil
	}
	return v
}

// summary is what the page's script renders.
func (c *cache) summary() map[string]any {
	c.mu.RLock()
	defer c.mu.RUnlock()
	ns := func(t time.Time) int64 {
		if t.IsZero() {
			return 0
		}
		return t.UnixNano()
	}
	return map[string]any{
		"now_ns": time.Now().UnixNano(), "version": version,
		"trader": c.trader, "trader_at_ns": ns(c.traderAt), "trader_error": c.traderEr,
		"observer": c.observer, "observer_at_ns": ns(c.obsAt), "observer_error": c.obsEr,
		"probe": c.probe,
	}
}

func poll(ctx context.Context, client *http.Client, c *cache, traderURL, obsURL, probeFile string) {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	for {
		tb, terr := get(ctx, client, traderURL)
		ob, oerr := get(ctx, client, obsURL)
		pb, _ := os.ReadFile(probeFile)
		c.mu.Lock()
		if terr == nil {
			c.trader, c.traderAt, c.traderEr = tb, time.Now(), ""
		} else {
			c.traderEr = terr.Error()
		}
		if oerr == nil {
			c.observer, c.obsAt, c.obsEr = ob, time.Now(), ""
		} else {
			c.obsEr = oerr.Error()
		}
		if json.Valid(pb) {
			c.probe = pb
		}
		c.mu.Unlock()
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func get(ctx context.Context, client *http.Client, url string) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK || !json.Valid(b) {
		return nil, errors.New(resp.Status)
	}
	return b, nil
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
