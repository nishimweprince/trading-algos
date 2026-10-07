// Package model holds the one event type that flows through replay, research and
// the live loop. Prices and quantities are fixed-point int64 scaled by 1e8 (E8):
// every Binance USDⓈ-M price and quantity has at most 8 decimals, so parsing is
// exact and the hot path never touches floats.
package model

// Scale is the fixed-point multiplier for prices and quantities.
const Scale = 100_000_000

// Kind is the event type.
type Kind uint8

const (
	// KindTrade is an aggregate trade (archive aggTrades row or @aggTrade frame).
	KindTrade Kind = 1
	// KindQuote is a best bid/ask update (@bookTicker frame).
	KindQuote Kind = 2
)

// Event is a market event for one symbol. Sym indexes the run's symbol list.
type Event struct {
	RecvNs  int64 // local receive time, unix ns (0 for archive data)
	ExchMs  int64 // exchange event time E (archive: = TradeMs)
	TradeMs int64 // trade / transaction time T
	ID      int64 // aggregate trade id (trades) or update id u (quotes)
	Px      int64 // trade price, E8
	Qty     int64 // trade quantity, E8
	Bid     int64 // quotes: best bid, E8
	Ask     int64 // quotes: best ask, E8
	BidQty  int64 // quotes: E8
	AskQty  int64 // quotes: E8
	Sym     uint16
	Kind    Kind
	// BuyerMaker is true when the buyer was the resting order, i.e. the taker sold.
	BuyerMaker bool
}

// TakerSign is +1 for a taker buy and -1 for a taker sell.
func (e *Event) TakerSign() int64 {
	if e.BuyerMaker {
		return -1
	}
	return 1
}
