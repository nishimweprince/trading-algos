// Package parse decodes Binance USDⓈ-M combined-stream frames (aggTrade,
// bookTicker, forceOrder) into model.Events without allocating. It scans the fixed shapes
// Binance sends; anything it does not need is skipped, so added fields
// (e.g. "nq") are harmless. Control frames go through encoding/json elsewhere.
package parse

import (
	"bytes"
	"errors"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/fixed"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

var (
	// ErrFrame is a frame that is not a combined-stream object.
	ErrFrame = errors.New("parse: malformed frame")
	// ErrUnknownStream is a stream we did not subscribe to (or a control reply).
	ErrUnknownStream = errors.New("parse: unknown stream")
	// ErrUnknownSymbol is a symbol outside the run's list.
	ErrUnknownSymbol = errors.New("parse: unknown symbol")
	// ErrField is a missing or malformed field.
	ErrField = errors.New("parse: bad field")
)

// Symbols maps upper-case symbol names to run indices (model.Event.Sym).
// Lookups with string(b) do not allocate.
type Symbols map[string]uint16

// NewSymbols indexes names in order.
func NewSymbols(names []string) Symbols {
	s := make(Symbols, len(names))
	for i, n := range names {
		s[n] = uint16(i)
	}
	return s
}

// Frame decodes one combined-stream frame into e. RecvNs is left to the caller.
func Frame(b []byte, syms Symbols, e *model.Event) error {
	stream, data, err := Envelope(b)
	if err != nil {
		return err
	}
	switch {
	case bytes.HasSuffix(stream, []byte("@aggTrade")):
		return AggTrade(data, syms, e)
	case bytes.HasSuffix(stream, []byte("@bookTicker")):
		return BookTicker(data, syms, e)
	case bytes.HasSuffix(stream, []byte("@forceOrder")):
		return ForceOrder(data, syms, e)
	}
	return ErrUnknownStream
}

// Envelope splits {"stream":"…","data":{…}} into its parts.
func Envelope(b []byte) (stream, data []byte, err error) {
	var sc scanner
	sc.b = b
	if !sc.open() {
		return nil, nil, ErrFrame
	}
	for {
		key, val, ok, end := sc.member()
		if end {
			break
		}
		if !ok {
			return nil, nil, ErrFrame
		}
		switch string(key) {
		case "stream":
			stream = val
		case "data":
			data = val
		}
	}
	if stream == nil || data == nil {
		return nil, nil, ErrUnknownStream
	}
	return stream, data, nil
}

// AggTrade decodes an aggTrade data object:
// {"e":"aggTrade","E":…,"a":…,"s":"BTCUSDT","p":"…","q":"…","f":…,"l":…,"T":…,"m":true}
func AggTrade(data []byte, syms Symbols, e *model.Event) error {
	var sc scanner
	sc.b = data
	if !sc.open() {
		return ErrFrame
	}
	*e = model.Event{Kind: model.KindTrade}
	var seen uint8
	for {
		key, val, ok, end := sc.member()
		if end {
			break
		}
		if !ok {
			return ErrFrame
		}
		if len(key) != 1 {
			continue
		}
		var err error
		switch key[0] {
		case 'E':
			e.ExchMs, err = atoi(val)
			seen |= 1
		case 'a':
			e.ID, err = atoi(val)
			seen |= 2
		case 's':
			idx, found := syms[string(val)]
			if !found {
				return ErrUnknownSymbol
			}
			e.Sym = idx
			seen |= 4
		case 'p':
			e.Px, err = fixed.ParseE8(val)
			seen |= 8
		case 'q':
			e.Qty, err = fixed.ParseE8(val)
			seen |= 16
		case 'T':
			e.TradeMs, err = atoi(val)
			seen |= 32
		case 'm':
			switch string(val) {
			case "true":
				e.BuyerMaker = true
			case "false":
			default:
				err = ErrField
			}
			seen |= 64
		}
		if err != nil {
			return ErrField
		}
	}
	if seen != 127 {
		return ErrField
	}
	return nil
}

// BookTicker decodes a bookTicker data object:
// {"e":"bookTicker","u":…,"s":"BNBUSDT","b":"…","B":"…","a":"…","A":"…","T":…,"E":…}
func BookTicker(data []byte, syms Symbols, e *model.Event) error {
	var sc scanner
	sc.b = data
	if !sc.open() {
		return ErrFrame
	}
	*e = model.Event{Kind: model.KindQuote}
	var seen uint8
	for {
		key, val, ok, end := sc.member()
		if end {
			break
		}
		if !ok {
			return ErrFrame
		}
		if len(key) != 1 {
			continue
		}
		var err error
		switch key[0] {
		case 'E':
			e.ExchMs, err = atoi(val)
			seen |= 1
		case 'u':
			e.ID, err = atoi(val)
			seen |= 2
		case 's':
			idx, found := syms[string(val)]
			if !found {
				return ErrUnknownSymbol
			}
			e.Sym = idx
			seen |= 4
		case 'b':
			e.Bid, err = fixed.ParseE8(val)
			seen |= 8
		case 'B':
			e.BidQty, err = fixed.ParseE8(val)
			seen |= 16
		case 'a':
			e.Ask, err = fixed.ParseE8(val)
			seen |= 32
		case 'A':
			e.AskQty, err = fixed.ParseE8(val)
			seen |= 64
		case 'T':
			e.TradeMs, err = atoi(val)
		}
		if err != nil {
			return ErrField
		}
	}
	if seen != 127 {
		return ErrField
	}
	return nil
}

// ForceOrder decodes a forceOrder (liquidation) data object:
// {"e":"forceOrder","E":…,"o":{"s":"BTCUSDT","S":"SELL","o":"LIMIT","f":"IOC","q":"…","p":"…","ap":"…","X":"FILLED","l":"…","z":"…","T":…}}
// Px is the average price "ap" (the order price "p" when ap is 0) and Qty the
// filled quantity "z" (the order quantity "q" when z is 0).
func ForceOrder(data []byte, syms Symbols, e *model.Event) error {
	var sc scanner
	sc.b = data
	if !sc.open() {
		return ErrFrame
	}
	*e = model.Event{Kind: model.KindLiq}
	var order []byte
	var seen uint8
	for {
		key, val, ok, end := sc.member()
		if end {
			break
		}
		if !ok {
			return ErrFrame
		}
		switch string(key) {
		case "E":
			var err error
			if e.ExchMs, err = atoi(val); err != nil {
				return ErrField
			}
			seen |= 1
		case "o":
			order = val
		}
	}
	if order == nil {
		return ErrField
	}
	sc = scanner{b: order}
	if !sc.open() {
		return ErrFrame
	}
	var px, ap, q, z int64
	for {
		key, val, ok, end := sc.member()
		if end {
			break
		}
		if !ok {
			return ErrFrame
		}
		var err error
		switch string(key) {
		case "s":
			idx, found := syms[string(val)]
			if !found {
				return ErrUnknownSymbol
			}
			e.Sym = idx
			seen |= 2
		case "S":
			switch string(val) {
			case "SELL":
				e.BuyerMaker = true
			case "BUY":
			default:
				err = ErrField
			}
			seen |= 4
		case "p":
			px, err = fixed.ParseE8(val)
			seen |= 8
		case "ap":
			ap, err = fixed.ParseE8(val)
		case "q":
			q, err = fixed.ParseE8(val)
			seen |= 16
		case "z":
			z, err = fixed.ParseE8(val)
		case "T":
			e.TradeMs, err = atoi(val)
			seen |= 32
		}
		if err != nil {
			return ErrField
		}
	}
	if seen != 63 {
		return ErrField
	}
	e.Px, e.Qty = ap, z
	if e.Px == 0 {
		e.Px = px
	}
	if e.Qty == 0 {
		e.Qty = q
	}
	return nil
}

// scanner walks one JSON object's members. String values are returned without
// quotes (escapes are not decoded; Binance symbols and decimals have none);
// object and array values are returned raw, braces included.
type scanner struct {
	b []byte
	i int
}

func (s *scanner) ws() {
	for s.i < len(s.b) {
		switch s.b[s.i] {
		case ' ', '\t', '\n', '\r':
			s.i++
		default:
			return
		}
	}
}

func (s *scanner) open() bool {
	s.ws()
	if s.i >= len(s.b) || s.b[s.i] != '{' {
		return false
	}
	s.i++
	return true
}

// member reads the next "key": value. end is true at the closing brace.
func (s *scanner) member() (key, val []byte, ok, end bool) {
	s.ws()
	if s.i >= len(s.b) {
		return nil, nil, false, false
	}
	if s.b[s.i] == ',' {
		s.i++
		s.ws()
	}
	if s.i < len(s.b) && s.b[s.i] == '}' {
		s.i++
		return nil, nil, true, true
	}
	if s.i >= len(s.b) || s.b[s.i] != '"' {
		return nil, nil, false, false
	}
	key, ok = s.str()
	if !ok {
		return nil, nil, false, false
	}
	s.ws()
	if s.i >= len(s.b) || s.b[s.i] != ':' {
		return nil, nil, false, false
	}
	s.i++
	s.ws()
	if s.i >= len(s.b) {
		return nil, nil, false, false
	}
	switch s.b[s.i] {
	case '"':
		val, ok = s.str()
	case '{', '[':
		val, ok = s.nested()
	default:
		start := s.i
		for s.i < len(s.b) && s.b[s.i] != ',' && s.b[s.i] != '}' && s.b[s.i] != ' ' && s.b[s.i] != '\n' {
			s.i++
		}
		val, ok = s.b[start:s.i], s.i > start
	}
	return key, val, ok, false
}

// str reads a quoted string starting at s.i and returns its contents.
func (s *scanner) str() ([]byte, bool) {
	s.i++ // opening quote
	start := s.i
	for s.i < len(s.b) {
		switch s.b[s.i] {
		case '\\':
			s.i += 2
			continue
		case '"':
			v := s.b[start:s.i]
			s.i++
			return v, true
		}
		s.i++
	}
	return nil, false
}

// nested reads a balanced {…} or […] value, skipping over strings.
func (s *scanner) nested() ([]byte, bool) {
	start, depth := s.i, 0
	for s.i < len(s.b) {
		switch s.b[s.i] {
		case '"':
			if _, ok := s.str(); !ok {
				return nil, false
			}
			continue
		case '{', '[':
			depth++
		case '}', ']':
			depth--
			if depth == 0 {
				s.i++
				return s.b[start:s.i], true
			}
		}
		s.i++
	}
	return nil, false
}

func atoi(b []byte) (int64, error) {
	if len(b) == 0 || len(b) > 19 {
		return 0, ErrField
	}
	var v int64
	for _, c := range b {
		if c < '0' || c > '9' {
			return 0, ErrField
		}
		v = v*10 + int64(c-'0')
	}
	return v, nil
}
