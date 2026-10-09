package shadow

import (
	"math"
	"math/rand/v2"
	"testing"
)

func TestBetaRecoversAKnownBeta(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	lh, fh := []hist{newHist()}, []hist{newHist()}
	b := newBetaSet(1, 1, 10*60*1000, 5*60*1000)
	L, F := 60000.0, 100.0
	const want = 1.6
	for s := int64(0); s < 3600; s++ {
		now := s * betaGridNs
		b.tick(now, lh, fh)
		rl := r.NormFloat64() * 2e-4
		rf := want*rl + r.NormFloat64()*1e-4
		L *= math.Exp(rl)
		F *= math.Exp(rf)
		lh[0].push(now+1, L)
		fh[0].push(now+2, F)
		if s == 200 {
			if _, warm := b.beta(0, 0); warm {
				t.Fatal("warm before the warm-up")
			}
		}
	}
	got, warm := b.beta(0, 0)
	if !warm || math.Abs(got-want) > 0.15 {
		t.Fatalf("beta %.3f warm %v, want %.1f", got, warm, want)
	}
	// A long silence starts over.
	b.tick(3600*betaGridNs+betaMaxGapNs+1, lh, fh)
	if _, warm := b.beta(0, 0); warm {
		t.Fatal("still warm after a gap")
	}
}

func TestGapBaseResidual(t *testing.T) {
	b := gapBase{leader: 100, follower: 10, beta: 2}
	// leader +5 bp, follower +4 bp: 2·5 − 4 = 6 bp still to come for a long.
	if r := b.residual(1, 100.05, 10.004); math.Abs(r-6) > 1e-6 {
		t.Fatalf("residual %.6f", r)
	}
	if r := b.residual(-1, 99.95, 9.996); math.Abs(r-6) > 1e-6 {
		t.Fatalf("short residual %.6f", r)
	}
}
