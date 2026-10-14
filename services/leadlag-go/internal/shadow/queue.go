package shadow

import (
	"math"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

// order is one simulated post-only (GTX) limit order with a conservative
// queue-position fill model, after hftbacktest's risk-averse model and
// Gatto's last-in-queue rule (PREREG Part 4):
//
//   - The order reaches the book latency_ms after the decision. It then joins
//     behind the whole displayed quantity at its price (bookTicker), or is
//     rejected if it would cross (post-only).
//   - Only trades move it up the queue: an aggressor trade at our price uses
//     up the quantity ahead of us; cancellations ahead are ignored. It fills
//     when the traded quantity exceeds the quantity ahead, or at once when a
//     trade prints strictly through our price. A fill is always complete
//     ($25 orders).
//   - Only trades executed after the order reached the book count. aggTrade
//     is published late (E − T p90 ≈ 150 ms), so a trade can arrive after
//     our order joined although it executed before; its quantity is already
//     out of the displayed size we joined behind. A trade whose exchange time
//     T is before liveNs, at millisecond resolution, is ignored (a trade in
//     the joining millisecond counts as before: the conservative side).
//   - If the touch moves behind our price with no trade, our order is the
//     best price: the quantity ahead becomes 0.
//   - A price behind the touch has an unknown queue (+Inf) until the touch
//     comes to it; it then joins behind the displayed quantity.
//   - A cancel or reprice takes effect latency_ms after the decision; until
//     then the order can still fill (a stale quote can be picked off).
type order struct {
	side     int8  // +1 buy, −1 sell; 0: no order
	px       int64 // E8
	liveNs   int64 // when the exchange accepts it
	live     bool
	cancelNs int64 // > 0: the cancel takes effect after this time
	nextPx   int64 // > 0: a reprice to nextPx takes effect at nextNs
	nextNs   int64
	ahead    float64 // quantity ahead of us at px
	joined   float64 // quantity ahead when it joined (reported)
}

// Order timer outcomes.
const (
	ordNone = iota
	ordRejected
	ordCancelled
)

func (o *order) active() bool { return o.side != 0 }

func (o *order) post(side int8, px, now, latNs int64) {
	*o = order{side: side, px: px, liveNs: now + latNs}
}

// cancel schedules the cancel (no effect on an order already cancelling).
func (o *order) cancel(now, latNs int64) {
	if o.side != 0 && o.cancelNs == 0 {
		o.cancelNs = now + latNs
	}
}

func (o *order) cancelling() bool { return o.cancelNs != 0 }

// reprice moves the order to px latency_ms from now; the queue is lost.
func (o *order) reprice(px, now, latNs int64) {
	if o.side != 0 && o.cancelNs == 0 && o.nextPx == 0 && px != o.px {
		o.nextPx, o.nextNs = px, now+latNs
	}
}

// join places the order in the book q at its price. It returns false for a
// post-only order that would cross.
func (o *order) join(q *quote) bool {
	if o.side > 0 {
		switch {
		case q.ask > 0 && o.px >= q.ask:
			return false
		case o.px > q.bid:
			o.ahead = 0
		case o.px == q.bid:
			o.ahead = float64(q.bidQty) / model.Scale
		default:
			o.ahead = math.Inf(1)
		}
	} else {
		switch {
		case q.bid > 0 && o.px <= q.bid:
			return false
		case o.px < q.ask:
			o.ahead = 0
		case o.px == q.ask:
			o.ahead = float64(q.askQty) / model.Scale
		default:
			o.ahead = math.Inf(1)
		}
	}
	o.joined = o.ahead
	o.live = true
	return true
}

// timers applies what is due at now: a cancel, the order reaching the book, a
// reprice. A rejected or cancelled order is cleared.
func (o *order) timers(now int64, q *quote) int {
	if o.side == 0 {
		return ordNone
	}
	if o.cancelNs != 0 && now > o.cancelNs {
		*o = order{}
		return ordCancelled
	}
	if !o.live && now >= o.liveNs {
		if !o.join(q) {
			*o = order{}
			return ordRejected
		}
	}
	if o.nextPx != 0 && now >= o.nextNs {
		o.px, o.liveNs, o.nextPx, o.nextNs, o.live = o.nextPx, o.nextNs, 0, 0, false
		if !o.join(q) {
			*o = order{}
			return ordRejected
		}
	}
	return ordNone
}

// onQuote updates the queue from a new touch.
func (o *order) onQuote(q *quote) {
	if !o.live {
		return
	}
	if o.side > 0 {
		switch {
		case q.bid < o.px:
			o.ahead = 0
		case q.bid == o.px && math.IsInf(o.ahead, 1):
			o.ahead = float64(q.bidQty) / model.Scale
		}
	} else {
		switch {
		case q.ask > o.px:
			o.ahead = 0
		case q.ask == o.px && math.IsInf(o.ahead, 1):
			o.ahead = float64(q.askQty) / model.Scale
		}
	}
}

// onTrade reports whether the trade (exchange time tradeMs) fills the order.
func (o *order) onTrade(px, qty, takerSign, tradeMs int64) bool {
	if !o.live || tradeMs*1_000_000 < o.liveNs {
		return false
	}
	if o.side > 0 {
		if px < o.px {
			return true
		}
		if px == o.px && takerSign < 0 {
			o.ahead -= float64(qty) / model.Scale
			return o.ahead < 0
		}
		return false
	}
	if px > o.px {
		return true
	}
	if px == o.px && takerSign > 0 {
		o.ahead -= float64(qty) / model.Scale
		return o.ahead < 0
	}
	return false
}
