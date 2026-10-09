package shadow

import "math"

// gapBase holds the mids a signal's gap is measured from (the leader's and
// the follower's at the start of the trigger window) and the β in force.
type gapBase struct {
	leader, follower float64
	beta             float64
}

// residual is dir × (β·leader move − follower move) in bp, both moves from the
// base to the current mids. At the signal it equals the entry gap; it falls
// to 0 as the follower catches up.
func (b gapBase) residual(dir int8, leaderNow, followerNow float64) float64 {
	d := float64(dir)
	lm := (leaderNow - b.leader) / b.leader * 1e4
	fm := (followerNow - b.follower) / b.follower * 1e4
	return d*b.beta*lm - d*fm
}

const (
	betaGridNs     = int64(1_000_000_000) // returns are sampled on a 1 s grid
	betaMaxGapNs   = 10 * 60 * betaGridNs // no event for this long: start over
	defaultBetaHLm = 30 * 60 * 1000       // ms
)

// betaEst is an EWMA regression through the origin of the follower's 1 s log
// returns on the leader's: β = E[rl·rf] / E[rl²]. Mean returns over 1 s are
// negligible next to their spread, so they are not removed.
type betaEst struct {
	lastL, lastF float64 // mids at the previous grid point
	cov, varL    float64
	n            int
	startNs      int64
}

// betaSet keeps one estimator per (leader, follower) pair. tick samples every
// grid point up to now from the mid histories, so the estimate only depends on
// the event stream (replays reproduce it).
type betaSet struct {
	est      []betaEst // leader*nf + follower
	nf       int
	alpha    float64
	warmupNs int64
	nextNs   int64 // next grid point to sample; 0 before the first event
	lastNs   int64
}

func newBetaSet(nl, nf int, halfLifeMs, warmupMs int64) *betaSet {
	if halfLifeMs <= 0 {
		halfLifeMs = defaultBetaHLm
	}
	if warmupMs <= 0 {
		warmupMs = defaultBetaHLm
	}
	return &betaSet{
		est: make([]betaEst, nl*nf), nf: nf,
		alpha:    1 - math.Exp2(-float64(betaGridNs/1_000_000)/float64(halfLifeMs)),
		warmupNs: warmupMs * 1_000_000,
	}
}

func (b *betaSet) reset() {
	for i := range b.est {
		b.est[i] = betaEst{}
	}
}

// tick samples the grid points up to now.
func (b *betaSet) tick(now int64, lh, fh []hist) {
	if b.nextNs == 0 || now-b.lastNs > betaMaxGapNs {
		if b.nextNs != 0 {
			b.reset()
		}
		b.nextNs = (now/betaGridNs + 1) * betaGridNs
		b.lastNs = now
		return
	}
	b.lastNs = now
	for b.nextNs <= now {
		b.sample(b.nextNs, lh, fh)
		b.nextNs += betaGridNs
	}
}

func (b *betaSet) sample(t int64, lh, fh []hist) {
	for l := range lh {
		lm, okL := lh[l].at(t)
		for f := range fh {
			fm, okF := fh[f].at(t)
			if !okL || !okF || lm <= 0 || fm <= 0 {
				continue
			}
			e := &b.est[l*b.nf+f]
			if e.lastL > 0 {
				rl, rf := math.Log(lm/e.lastL), math.Log(fm/e.lastF)
				if e.n == 0 {
					e.cov, e.varL = rl*rf, rl*rl
				} else {
					e.cov += b.alpha * (rl*rf - e.cov)
					e.varL += b.alpha * (rl*rl - e.varL)
				}
				e.n++
			} else {
				e.startNs = t
			}
			e.lastL, e.lastF = lm, fm
		}
	}
}

// beta returns the pair's β and whether the warm-up has passed.
func (b *betaSet) beta(l, f int) (float64, bool) {
	e := &b.est[l*b.nf+f]
	if e.n == 0 || e.varL <= 0 || b.nextNs-betaGridNs-e.startNs < b.warmupNs {
		return 0, false
	}
	return e.cov / e.varL, true
}
