// Package fixed parses and formats the E8 fixed-point decimals used for prices and
// quantities (see model.Scale). Parsing allocates nothing.
package fixed

import (
	"errors"
	"strconv"
)

// ErrSyntax is returned for anything that is not an unsigned decimal with at most
// 8 significant fractional digits.
var ErrSyntax = errors.New("fixed: invalid decimal")

var pow10 = [9]int64{1, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000}

// ParseE8 parses an unsigned decimal ("142.35", "0.00001234", "7") or one in
// scientific notation ("1.6711908E7", which Binance's archive CSVs use for large
// quantities) into E8. The result must be exact: digits that E8 cannot hold
// (non-zero beyond the 8th decimal) are an error, never rounded.
func ParseE8(b []byte) (int64, error) {
	const maxI = 1<<63 - 1
	var m int64
	i, digits, frac, dot := 0, 0, 0, false
	for ; i < len(b); i++ {
		c := b[i]
		switch {
		case c >= '0' && c <= '9':
			digits++
			if dot {
				frac++
			}
			if m > (maxI-9)/10 {
				return 0, ErrSyntax
			}
			m = m*10 + int64(c-'0')
			continue
		case c == '.' && !dot:
			dot = true
			continue
		case c == 'e' || c == 'E':
		default:
			return 0, ErrSyntax
		}
		break
	}
	if digits == 0 {
		return 0, ErrSyntax
	}
	exp := 0
	if i < len(b) { // exponent
		i++
		neg := false
		if i < len(b) && (b[i] == '+' || b[i] == '-') {
			neg = b[i] == '-'
			i++
		}
		if i == len(b) {
			return 0, ErrSyntax
		}
		for ; i < len(b); i++ {
			c := b[i]
			if c < '0' || c > '9' || exp > 100 {
				return 0, ErrSyntax
			}
			exp = exp*10 + int(c-'0')
		}
		if neg {
			exp = -exp
		}
	}
	for p := exp - frac + 8; p != 0; {
		if p > 0 {
			if m > maxI/10 {
				return 0, ErrSyntax
			}
			m *= 10
			p--
		} else {
			if m%10 != 0 {
				return 0, ErrSyntax
			}
			m /= 10
			p++
		}
	}
	return m, nil
}

// ParseE8String is ParseE8 for a string.
func ParseE8String(s string) (int64, error) { return ParseE8([]byte(s)) }

// Format renders an E8 value as a minimal decimal string (for reports, not the hot path).
func Format(v int64) string {
	neg := v < 0
	if neg {
		v = -v
	}
	ip, fp := v/pow10[8], v%pow10[8]
	s := strconv.FormatInt(ip, 10)
	if fp != 0 {
		f := strconv.FormatInt(fp+pow10[8], 10)[1:]
		for f[len(f)-1] == '0' {
			f = f[:len(f)-1]
		}
		s += "." + f
	}
	if neg {
		s = "-" + s
	}
	return s
}

// Float converts E8 to float64 (research only).
func Float(v int64) float64 { return float64(v) / float64(pow10[8]) }
