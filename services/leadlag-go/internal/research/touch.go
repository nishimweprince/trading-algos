// Package research is the M3 signal study: leader flow events, follower
// responses with explicit observability and latency, day-block bootstrap
// statistics, the screen and gate G1. Definitions follow research/PREREG.md.
package research

// Touch estimates a follower's mid from its trades (PREREG 1.3). A taker buy
// prints at the ask and a taker sell at the bid, so the latest trade moved
// half a spread toward the middle is the freshest mid available. The
// (last buy + last sell)/2 alternative keeps one stale side after a jump and
// then "catches up" after entry, which would show up as false response.
type Touch struct {
	BuyPx, SellPx, LastPx float64
	BuyT, SellT, LastT    int64
	LastBuy               bool
	HS                    float64 // EWMA half-spread as a fraction of price (0 until sampled)
}

// hsAlpha weights each fresh half-spread sample in the EWMA.
const hsAlpha = 0.05

// Update applies one trade at time t (ms). Before updating the side, a
// half-spread sample is taken when the opposite side traded within
// spreadStaleMs and the book is not crossed.
func (s *Touch) Update(t int64, px float64, takerBuy bool, spreadStaleMs int64) {
	if takerBuy {
		s.BuyPx, s.BuyT = px, t
	} else {
		s.SellPx, s.SellT = px, t
	}
	s.LastPx, s.LastT, s.LastBuy = px, t, takerBuy
	if hs, ok := s.halfSpread(t, spreadStaleMs); ok {
		if s.HS == 0 {
			s.HS = hs
		} else {
			s.HS += hsAlpha * (hs - s.HS)
		}
	}
}

// Mid is the latest trade price moved half a spread toward the middle; the last
// trade price itself until a spread has been observed. Zero before any trade.
func (s *Touch) Mid() float64 {
	if s.LastBuy {
		return s.LastPx * (1 - s.HS)
	}
	return s.LastPx * (1 + s.HS)
}

func (s *Touch) halfSpread(now, staleMs int64) (float64, bool) {
	if s.BuyT == 0 || s.SellT == 0 || now-s.BuyT > staleMs || now-s.SellT > staleMs || s.SellPx >= s.BuyPx {
		return 0, false
	}
	mid := (s.BuyPx + s.SellPx) / 2
	return (s.BuyPx - s.SellPx) / 2 / mid, true
}

// HalfSpreadBp is the current fresh half-spread in bp, if both sides are fresh.
func (s *Touch) HalfSpreadBp(now, staleMs int64) (float64, bool) {
	hs, ok := s.halfSpread(now, staleMs)
	return hs * 1e4, ok
}
