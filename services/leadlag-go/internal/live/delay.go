package live

import "github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"

// Delay is a placebo wrapper for replays (leadlag --replay --leader-shift): the
// events of the delayed symbols reach the inner engine shiftNs later, with
// their receive and exchange times moved by the same amount, and in order with
// the other symbols' events. Leader events then carry no information about the
// followers' following window, so any "response" the inner engine measures is
// the control the real one is compared against (PREREG Part 4).
type Delay struct {
	inner   Engine
	delayed []bool // by symbol index
	shiftNs int64
	buf     []model.Event // FIFO of shifted events not yet delivered
	head    int
}

// NewDelay wraps inner; delayed[sym] marks the symbols to shift.
func NewDelay(inner Engine, delayed []bool, shiftNs int64) *Delay {
	return &Delay{inner: inner, delayed: delayed, shiftNs: shiftNs}
}

// OnEvent implements Engine.
func (d *Delay) OnEvent(e *model.Event) {
	d.flush(e.RecvNs)
	if int(e.Sym) < len(d.delayed) && d.delayed[e.Sym] {
		s := *e
		s.RecvNs += d.shiftNs
		s.ExchMs += d.shiftNs / 1_000_000
		if s.TradeMs != 0 {
			s.TradeMs += d.shiftNs / 1_000_000
		}
		d.buf = append(d.buf, s)
		return
	}
	d.inner.OnEvent(e)
}

// flush delivers the shifted events due at or before now.
func (d *Delay) flush(now int64) {
	for d.head < len(d.buf) && d.buf[d.head].RecvNs <= now {
		d.inner.OnEvent(&d.buf[d.head])
		d.head++
	}
	if d.head > 4096 && d.head*2 > len(d.buf) {
		n := copy(d.buf, d.buf[d.head:])
		d.buf, d.head = d.buf[:n], 0
	}
}

// Pending reports how many shifted events have not been delivered yet (the
// last shiftNs of the tape; they have no follower events left to meet).
func (d *Delay) Pending() int { return len(d.buf) - d.head }
