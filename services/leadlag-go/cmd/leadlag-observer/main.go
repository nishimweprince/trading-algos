// Command leadlag-observer receives the trader's records over a unix socket,
// writes the hourly tape and keeps feed statistics, served on a loopback API.
// It runs as its own process (own CPUs, lower priority) so neither disk I/O nor
// statistics can stall the trader.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/observe"
)

var version = "dev"

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	sock := env("LL_OBSERVER_SOCK", "/run/leadlag/observer.sock")
	tapeDir := env("LL_TAPE_DIR", "/data/leadlag/tape")
	addr := env("LL_OBSERVER_ADDR", "127.0.0.1:8061")
	maxPct, _ := strconv.ParseFloat(env("LL_DISK_MAX_PCT", "90"), 64)

	if err := os.MkdirAll(tapeDir, 0o755); err != nil {
		log.Error("tape dir", "err", err)
		os.Exit(1)
	}
	if err := os.MkdirAll(filepath.Dir(sock), 0o755); err != nil {
		log.Error("socket dir", "err", err)
		os.Exit(1)
	}
	// A stale socket from a crash would make Listen fail.
	if err := os.Remove(sock); err != nil && !errors.Is(err, os.ErrNotExist) {
		log.Error("remove stale socket", "err", err)
		os.Exit(1)
	}
	ln, err := net.Listen("unix", sock)
	if err != nil {
		log.Error("listen", "socket", sock, "err", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	journalDir := env("LL_JOURNAL_DIR", "/data/leadlag/shadow")
	o := observe.New(observe.Config{TapeDir: tapeDir, DiskMaxPct: maxPct, Log: log, JournalDir: journalDir})
	go o.Serve(ctx, ln)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("GET /v1/stats", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(o.Stats())
	})
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http", "err", err)
			stop()
		}
	}()
	log.Info("observer started", "build", version, "socket", sock, "tape", tapeDir, "addr", addr)

	t := time.NewTicker(2 * time.Second)
	defer t.Stop()
	o.Housekeep()
	for {
		select {
		case <-ctx.Done():
			srv.Close()
			// The trader (stopped first by systemd ordering) has closed its end; give
			// the reader a moment to drain, then close the hour file and its manifest.
			time.Sleep(500 * time.Millisecond)
			if err := o.Close(); err != nil {
				log.Error("close tape", "err", err)
			}
			os.Remove(sock)
			log.Info("observer stopped")
			return
		case <-t.C:
			o.Housekeep()
		}
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
