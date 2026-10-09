package live_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ipc"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/live"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/observe"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/tape"
)

var symbols = []string{"BTCUSDT", "ETHUSDT", "SOLUSDT"}

type captureEngine struct{ events []model.Event }

func (c *captureEngine) OnEvent(e *model.Event) { c.events = append(c.events, *e) }

func aggFrame(sym string, e, id, t int64, px string, buyerMaker bool) []byte {
	return fmt.Appendf(nil, `{"stream":"%s@aggTrade","data":{"e":"aggTrade","E":%d,"a":%d,"s":"%s","p":"%s","q":"0.010","nq":"0.010","f":1,"l":2,"T":%d,"m":%t,"st":1}}`,
		lower(sym), e, id, sym, px, t, buyerMaker)
}

func bookFrame(sym string, u, e int64, bid, ask string) []byte {
	return fmt.Appendf(nil, `{"stream":"%s@bookTicker","data":{"e":"bookTicker","u":%d,"s":"%s","ps":"%s","b":"%s","B":"1.5","a":"%s","A":"2.25","T":%d,"E":%d,"st":1}}`,
		lower(sym), u, sym, sym, bid, ask, e, e)
}

func lower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 32
		}
	}
	return string(b)
}

// TestLiveTapeReplayParity: frames pushed through a live hub (with producer,
// socket and observer) are taped, and replaying the tape gives the same events
// and digest. Includes a reconnect overlap (duplicate ids) and a control reply.
func TestLiveTapeReplayParity(t *testing.T) {
	dir, err := os.MkdirTemp("", "ll")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	sock := filepath.Join(dir, "o.sock")
	tapeDir := filepath.Join(dir, "tape")
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))

	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	obs := observe.New(observe.Config{TapeDir: tapeDir, Log: quiet})
	octx, ostop := context.WithCancel(context.Background())
	go obs.Serve(octx, ln)

	prod := ipc.NewProducer(sock, 1<<20, ipc.Hello{PID: 4242, Mode: "record", Symbols: symbols, StartNs: 1}, quiet)
	pctx, pstop := context.WithCancel(context.Background())
	pdone := make(chan struct{})
	go func() { prod.Run(pctx); close(pdone) }()
	for !prod.Connected.Load() {
		time.Sleep(10 * time.Millisecond)
	}

	eng := &captureEngine{}
	hub := live.NewHub(symbols, eng, prod)
	r := rand.New(rand.NewPCG(3, 4))
	base := time.Now().UnixNano()
	ids := map[string]int64{"BTCUSDT": 100, "ETHUSDT": 500, "SOLUSDT": 900}
	us := map[string]int64{"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1}
	hub.OnConn(ipc.ConnMarket, base, true)
	for i := range 3000 {
		recv := base + int64(i)*100_000
		sym := symbols[r.IntN(3)]
		exch := recv/1_000_000 - 5
		if r.IntN(2) == 0 {
			ids[sym]++
			hub.OnFrame(ipc.ConnMarket, recv, aggFrame(sym, exch, ids[sym], exch-3, fmt.Sprintf("%d.%02d", 100+r.IntN(5), r.IntN(100)), r.IntN(2) == 0))
		} else {
			us[sym]++
			hub.OnFrame(ipc.ConnPublic, recv, bookFrame(sym, us[sym], exch, "100.10", "100.20"))
		}
		if i == 1500 {
			// reconnect overlap: the same trade again, and a subscription reply
			hub.OnFrame(ipc.ConnMarket, recv+1, aggFrame("BTCUSDT", exch, ids["BTCUSDT"], exch-3, "101.00", false))
			hub.OnFrame(ipc.ConnMarket, recv+2, []byte(`{"result":null,"id":1}`))
			// a leader liquidation (forceOrder has no id: never a duplicate)
			hub.OnFrame(ipc.ConnMarket, recv+3, fmt.Appendf(nil, `{"stream":"btcusdt@forceOrder","data":{"e":"forceOrder","E":%d,"o":{"s":"BTCUSDT","S":"SELL","o":"LIMIT","f":"IOC","q":"0.5","p":"99.0","ap":"99.5","X":"FILLED","l":"0.5","z":"0.5","T":%d}}}`, exch, exch-1))
		}
	}
	liveSt := hub.Snapshot()
	pstop()
	<-pdone
	time.Sleep(200 * time.Millisecond) // observer drains the socket
	ostop()
	if err := obs.Close(); err != nil {
		t.Fatal(err)
	}

	if liveSt.DupTrades != 1 || liveSt.Other != 1 || liveSt.Events != uint64(len(eng.events)) {
		t.Fatalf("live stats: %+v (captured %d)", liveSt, len(eng.events))
	}
	st := obs.Stats()
	if st.Frames != 3003 || st.Trader.Hellos != 1 || st.Trader.Gaps != 0 {
		t.Fatalf("observer: frames %d hellos %d gaps %d", st.Frames, st.Trader.Hellos, st.Trader.Gaps)
	}

	files, err := tape.Files(tapeDir)
	if err != nil || len(files) == 0 {
		t.Fatalf("tape files %v %v", files, err)
	}
	var replayed *captureEngine
	segs, err := live.Replay(files, func(ipc.Hello) live.Engine { replayed = &captureEngine{}; return replayed })
	if err != nil {
		t.Fatal(err)
	}
	if len(segs) != 1 || !segs[0].Complete || segs[0].PID != 4242 {
		t.Fatalf("segments: %+v", segs)
	}
	if segs[0].Events != liveSt.Events || segs[0].Digest != liveSt.Digest {
		t.Fatalf("replay events %d digest %x, live %d %x", segs[0].Events, segs[0].Digest, liveSt.Events, liveSt.Digest)
	}
	for i := range eng.events {
		if eng.events[i] != replayed.events[i] {
			t.Fatalf("event %d differs:\n live   %+v\n replay %+v", i, eng.events[i], replayed.events[i])
		}
	}
	// manifest written on close
	m := filepath.Join(filepath.Dir(files[0]), filepath.Base(files[0][:len(files[0])-len(".jsonl.zst")])+".json")
	if _, err := os.Stat(m); err != nil {
		t.Fatalf("manifest: %v", err)
	}
}
