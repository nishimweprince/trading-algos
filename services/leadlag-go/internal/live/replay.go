package live

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ipc"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/tape"
)

// Segment is one trader run on the tape: from a hello with a new PID to the
// next. A live run and its replay must agree on Events and Digest when the
// segment has no gaps.
type Segment struct {
	PID      int      `json:"pid"`
	Build    string   `json:"build"`
	StartNs  int64    `json:"start_ns"`
	Symbols  []string `json:"symbols"`
	Frames   uint64   `json:"frames"`
	Events   uint64   `json:"events"`
	Digest   uint64   `json:"digest"`
	Gaps     int      `json:"gaps"`
	Hellos   int      `json:"hellos"`
	LastNs   int64    `json:"last_recv_ns"`
	Complete bool     `json:"complete"` // no gaps and a single hello (no observer reconnect)
}

// Replay runs tape files through fresh hubs (one per trader run) and returns
// each run's segment. newEngine builds the engine for a run's symbols.
func Replay(files []string, newEngine func(symbols []string) Engine) ([]Segment, error) {
	r := tape.NewReader(files)
	defer r.Close()
	var (
		segs []Segment
		hub  *Hub
		cur  *Segment
		recv int64
		l    tape.Line
	)
	finish := func() {
		if cur == nil {
			return
		}
		st := hub.Snapshot()
		cur.Frames = st.Frames[0] + st.Frames[1]
		cur.Events, cur.Digest = st.Events, st.Digest
		cur.Complete = cur.Gaps == 0 && cur.Hellos == 1
		segs = append(segs, *cur)
	}
	for {
		err := r.Next(&l)
		if err == io.EOF {
			break
		}
		if err != nil {
			return segs, err
		}
		switch l.Kind {
		case "hello":
			var w struct {
				Hello ipc.Hello `json:"hello"`
			}
			if err := json.Unmarshal(l.Raw, &w); err != nil {
				return segs, fmt.Errorf("replay: hello: %w", err)
			}
			if cur != nil && cur.PID == w.Hello.PID {
				cur.Hellos++ // observer reconnect within the same run
				continue
			}
			finish()
			cur = &Segment{PID: w.Hello.PID, Build: w.Hello.Build, StartNs: w.Hello.StartNs, Symbols: w.Hello.Symbols, Hellos: 1}
			hub = NewHub(w.Hello.Symbols, newEngine(w.Hello.Symbols), nil)
			hub.SetClock(func() int64 { return recv })
		case "gap":
			if cur != nil {
				cur.Gaps++
			}
		case "frame":
			if hub == nil {
				continue // frames before any hello cannot be attributed
			}
			recv = l.RecvNs
			hub.OnFrame(l.Conn, l.RecvNs, l.Frame)
			cur.LastNs = l.RecvNs
		}
	}
	finish()
	return segs, nil
}
