package archive

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"strconv"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/fixed"
)

// Trade is one aggTrades row as packed into .llp files.
type Trade struct {
	AggID      int64
	TradeMs    int64
	Px         int64 // E8
	Qty        int64 // E8
	BuyerMaker bool
}

// ReadCSV streams an aggTrades CSV and calls fn for each row in file order.
// Columns: agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker.
// A header line is optional (older files have none). transact_time in µs (> 1e14)
// is converted to ms.
func ReadCSV(r io.Reader, fn func(Trade) error) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	var fields [8][]byte
	line := 0
	for sc.Scan() {
		line++
		b := sc.Bytes()
		if len(b) == 0 {
			continue
		}
		if line == 1 && (b[0] < '0' || b[0] > '9') {
			continue // header
		}
		n := split(b, &fields)
		if n < 7 {
			return fmt.Errorf("line %d: %d fields", line, n)
		}
		var t Trade
		var err error
		if t.AggID, err = atoi(fields[0]); err != nil {
			return fmt.Errorf("line %d agg_trade_id: %w", line, err)
		}
		if t.Px, err = fixed.ParseE8(fields[1]); err != nil {
			return fmt.Errorf("line %d price %q: %w", line, fields[1], err)
		}
		if t.Qty, err = fixed.ParseE8(fields[2]); err != nil {
			return fmt.Errorf("line %d quantity %q: %w", line, fields[2], err)
		}
		if t.TradeMs, err = atoi(fields[5]); err != nil {
			return fmt.Errorf("line %d transact_time: %w", line, err)
		}
		if t.TradeMs > 100_000_000_000_000 {
			t.TradeMs /= 1000
		}
		switch {
		case bytes.EqualFold(fields[6], []byte("true")):
			t.BuyerMaker = true
		case bytes.EqualFold(fields[6], []byte("false")):
		default:
			return fmt.Errorf("line %d is_buyer_maker %q", line, fields[6])
		}
		if err := fn(t); err != nil {
			return err
		}
	}
	return sc.Err()
}

func split(b []byte, out *[8][]byte) int {
	n := 0
	for n < len(out) {
		i := bytes.IndexByte(b, ',')
		if i < 0 {
			out[n] = bytes.TrimRight(b, "\r")
			return n + 1
		}
		out[n] = b[:i]
		b = b[i+1:]
		n++
	}
	return n
}

func atoi(b []byte) (int64, error) {
	if len(b) == 0 || len(b) > 19 {
		return 0, strconv.ErrSyntax
	}
	var v int64
	for _, c := range b {
		if c < '0' || c > '9' {
			return 0, strconv.ErrSyntax
		}
		v = v*10 + int64(c-'0')
	}
	return v, nil
}
