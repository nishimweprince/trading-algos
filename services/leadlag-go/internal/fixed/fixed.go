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

// ParseE8 parses an unsigned decimal ("142.35", "0.00001234", "7") into E8.
// Trailing fractional zeros beyond 8 digits are accepted; non-zero digits there
// are an error, since they cannot be represented exactly.
func ParseE8(b []byte) (int64, error) {
	if len(b) == 0 {
		return 0, ErrSyntax
	}
	var ip, fp int64
	i, frac, digits := 0, -1, 0
	for ; i < len(b); i++ {
		c := b[i]
		switch {
		case c >= '0' && c <= '9':
			digits++
			if frac < 0 {
				if ip > (1<<63-1)/10/100_000_000 {
					return 0, ErrSyntax
				}
				ip = ip*10 + int64(c-'0')
			} else if frac < 8 {
				fp = fp*10 + int64(c-'0')
				frac++
			} else if c != '0' {
				return 0, ErrSyntax
			}
		case c == '.' && frac < 0:
			frac = 0
		default:
			return 0, ErrSyntax
		}
	}
	if digits == 0 {
		return 0, ErrSyntax
	}
	if frac < 0 {
		frac = 0
	}
	return ip*pow10[8] + fp*pow10[8-frac], nil
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
