package shadow

import (
	"math"
	"testing"
)

// book: bid 100.00 × 5, ask 100.01 × 3 (tick 0.01).
func book() quote {
	return quote{bid: e8(100), ask: e8(100.01), bidQty: e8(5), askQty: e8(3), recvNs: 1}
}

func TestOrderJoinsBehindTheDisplayedQueue(t *testing.T) {
	q := book()
	var o order
	o.post(1, e8(100), 0, 10)
	if o.timers(5, &q) != ordNone || o.live {
		t.Fatal("live before the latency")
	}
	if o.timers(10, &q) != ordNone || !o.live || o.ahead != 5 {
		t.Fatalf("join: %+v", o)
	}
	// Sells at our price use up the queue; buys at our price do not.
	if o.onTrade(e8(100), e8(3), -1) || o.onTrade(e8(100), e8(9), +1) {
		t.Fatal("filled early")
	}
	if o.ahead != 2 {
		t.Fatalf("ahead %v", o.ahead)
	}
	if o.onTrade(e8(100), e8(2), -1) {
		t.Fatal("filled at exactly the queue ahead (needs more)")
	}
	if !o.onTrade(e8(100), e8(0.001), -1) {
		t.Fatal("not filled once the queue ahead is used up")
	}
}

func TestOrderFillsOnATradeThrough(t *testing.T) {
	q := book()
	var o order
	o.post(-1, e8(100.01), 0, 0)
	o.timers(0, &q)
	if !o.onTrade(e8(100.02), e8(0.001), +1) {
		t.Fatal("a trade through our ask must fill")
	}
}

func TestOrderAloneAtTheTouchAfterTheBookMovesBehindIt(t *testing.T) {
	q := book()
	var o order
	o.post(1, e8(100), 0, 0)
	o.timers(0, &q)
	q.bid, q.bidQty = e8(99.99), e8(7)
	o.onQuote(&q)
	if o.ahead != 0 || !o.onTrade(e8(100), e8(0.001), -1) {
		t.Fatalf("ahead %v", o.ahead)
	}
}

func TestOrderBehindTheTouchWaitsForIt(t *testing.T) {
	q := book()
	var o order
	o.post(1, e8(99.99), 0, 0)
	o.timers(0, &q)
	if !math.IsInf(o.ahead, 1) || o.onTrade(e8(99.99), e8(100), -1) {
		t.Fatalf("behind the touch: %+v", o)
	}
	q.bid, q.bidQty = e8(99.99), e8(4)
	o.onQuote(&q)
	if o.ahead != 4 {
		t.Fatalf("ahead %v", o.ahead)
	}
}

func TestPostOnlyRejectsACrossingOrder(t *testing.T) {
	q := book()
	var o order
	o.post(1, e8(100.01), 0, 0)
	if o.timers(0, &q) != ordRejected || o.active() {
		t.Fatal("a bid at the ask must be rejected")
	}
	// A price inside the spread is the new best: nobody ahead.
	q.ask = e8(100.03)
	o.post(1, e8(100.01), 0, 0)
	if o.timers(0, &q) != ordNone || o.ahead != 0 {
		t.Fatalf("inside the spread: %+v", o)
	}
}

func TestCancelLatencyCanStillFill(t *testing.T) {
	q := book()
	var o order
	o.post(1, e8(100), 0, 0)
	o.timers(0, &q)
	o.cancel(100, 10)
	o.timers(110, &q) // the cancel lands only after 110
	if !o.active() || !o.onTrade(e8(99.99), e8(1), -1) {
		t.Fatal("an order whose cancel is in flight must still fill")
	}
	o.post(1, e8(100), 0, 0)
	o.timers(0, &q)
	o.cancel(100, 10)
	if o.timers(111, &q) != ordCancelled || o.active() {
		t.Fatal("cancel did not land")
	}
}

func TestRepriceLosesTheQueue(t *testing.T) {
	q := book()
	var o order
	o.post(1, e8(100), 0, 0)
	o.timers(0, &q)
	o.onTrade(e8(100), e8(4), -1) // 1 left ahead
	q.bid, q.bidQty = e8(100.005), e8(6)
	o.reprice(e8(100.005), 50, 10)
	o.timers(55, &q)
	if o.px != e8(100) || o.ahead != 1 {
		t.Fatalf("reprice applied early: %+v", o)
	}
	o.timers(60, &q)
	if o.px != e8(100.005) || o.ahead != 6 {
		t.Fatalf("after reprice: %+v", o)
	}
}
