"""Live mirror of the walk-forward backtest (dv/eng.py + dv/gate.py), pure numpy.

Per strategy there is one *ungated* track that reproduces the backtest exactly (one position at a
time, entry at the next M1 open, 2 ATR stop, trail 1.5R after +1R, 480-minute max hold). Each entry
on that track is put to Jev; a trade Jev approves is the strategy's tagged trade. This is the same
construction as the backtest gate (the gate filtered the ungated trade list), so live and backtest
numbers are comparable.
"""
import numpy as np
from rules import generic_rules, ema


def resample(t, o, h, l, c, tf, complete_until):
    """tf-minute bars from M1 arrays; only groups whose close time <= complete_until (minute)."""
    if tf == 1:
        m = t < complete_until
        return dict(t=t[m], o=o[m], h=h[m], l=l[m], c=c[m])
    g = t // tf
    starts = np.flatnonzero(np.r_[True, g[1:] != g[:-1]])
    ends = np.r_[starts[1:], len(t)]
    bt = g[starts] * tf
    keep = bt + tf <= complete_until
    starts, ends, bt = starts[keep], ends[keep], bt[keep]
    return dict(t=bt, o=o[starts], c=c[ends - 1],
                h=np.array([h[s:e].max() for s, e in zip(starts, ends)]),
                l=np.array([l[s:e].min() for s, e in zip(starts, ends)]))


def atr(b, n=14):
    pc = np.r_[b['c'][0], b['c'][:-1]]
    tr = np.maximum(b['h'] - b['l'], np.maximum(abs(b['h'] - pc), abs(b['l'] - pc)))
    a = np.empty_like(tr); a[0] = tr[0]; k = 1.0 / n
    for i in range(1, len(tr)):
        a[i] = a[i - 1] + k * (tr[i] - a[i - 1])
    return a


def features(b, a, k, side, prev, prev2):
    """Same features as dv/gate.py, at signal bar k."""
    c = b['c']
    def lagv(n):
        return c[k - n] if k - n >= 0 else np.nan
    z16 = (c[k] - lagv(16)) / (a[k] * 4)
    z60 = (c[k] - lagv(60)) / (a[k] * np.sqrt(60))
    a240 = a[max(0, k - 239):k + 1].sum() / 240.0
    vr = a[k] / max(a240, 1e-12)
    slope = np.sign(np.diff(ema(c[:k + 1], 15), prepend=c[0]))
    flips = np.flatnonzero(slope[1:] != slope[:-1]) + 1
    last_flip = flips[-1] if len(flips) else 0
    age = min(k - last_flip, 120)
    return [float(side * z16), float(side * z60), float(vr), float(age), float(prev), float(prev2)]


class Track:
    """Ungated backtest-equivalent track for one strategy."""

    def __init__(self, cfg, spread, swapm):
        self.cfg = cfg; self.spread = spread; self.swapm = swapm
        self.pos = None          # open position dict
        self.pending = None      # signal waiting for next M1 open
        self.outcomes = []       # signs of closed ungated trades (for prev/prev2)
        self.history = []        # closed ungated trades: (side, r, exit_t) for the Jev v1 history state
        self.last_signal_bar = None

    # ---- per M1 bar ----
    def on_m1(self, t, o, h, l, c):
        """Returns list of events: ('entry', pos) / ('exit', pos)."""
        ev = []
        if self.pending is not None and t >= self.pending['entry_after']:
            p = self.pending; self.pending = None
            R = p['risk']; s = p['side']
            tpR = float(self.cfg.get('target_R', 0) or 0)
            self.pos = dict(p, entry_t=int(t), entry_px=float(o), sl=float(o - s * R), best=float(o), bars=0,
                            tp=float(o + s * tpR * R) if tpR > 0 else None)
            ev.append(('entry', self.pos))
        if self.pos is not None:
            P = self.pos; s = P['side']; R = P['risk']
            P['bars'] += 1
            res = None; reason = None
            if s > 0:
                if l <= P['sl']:
                    res = min(P['sl'], o) - P['entry_px']; reason = 'stop' if P['sl'] < P['entry_px'] + 1e-12 else 'trail'
                elif P.get('tp') is not None and h >= P['tp']:
                    res = P['tp'] - P['entry_px']; reason = 'target'
                else:
                    P['best'] = max(P['best'], h)
                    if self.cfg['trail_R'] > 0 and P['best'] - P['entry_px'] >= R:
                        P['sl'] = max(P['sl'], P['best'] - self.cfg['trail_R'] * R)
            else:
                if h >= P['sl']:
                    res = P['entry_px'] - max(P['sl'], o); reason = 'stop' if P['sl'] > P['entry_px'] - 1e-12 else 'trail'
                elif P.get('tp') is not None and l <= P['tp']:
                    res = P['entry_px'] - P['tp']; reason = 'target'
                else:
                    P['best'] = min(P['best'], l)
                    if self.cfg['trail_R'] > 0 and P['entry_px'] - P['best'] >= R:
                        P['sl'] = min(P['sl'], P['best'] + self.cfg['trail_R'] * R)
            if res is None and P['bars'] >= self.cfg['max_hold'] + 1:
                res = s * (c - P['entry_px']); reason = 'time'
            if res is not None:
                held = P['bars']
                r = (res - self.spread - self.swapm * P['entry_px'] * held) / R
                P.update(exit_t=int(t), exit_px=float(P['entry_px'] + s * res), r=float(r), reason=reason)
                self.outcomes.append(1 if r > 0 else -1)
                self.history.append((int(s), float(r), int(t))); self.history = self.history[-10:]
                ev.append(('exit', P)); self.pos = None
        return ev

    # ---- per completed tf bar ----
    def on_bar(self, b, a, sig, k):
        """Evaluate the rule at completed tf bar k (sig = rule side array over b). Returns pending or None."""
        if k < 300 or self.pos is not None or self.pending is not None:
            return None
        if self.last_signal_bar is not None and b['t'][k] <= self.last_signal_bar:
            return None
        self.last_signal_bar = int(b['t'][k])
        s = int(sig[k])
        if s == 0 or not np.isfinite(a[k]) or a[k] <= 0:
            return None
        prev = self.outcomes[-1] if self.outcomes else 0
        prev2 = self.outcomes[-2] if len(self.outcomes) > 1 else 0
        F = features(b, a, k, s, prev, prev2)
        close_t = int(b['t'][k]) + self.cfg['tf']
        # v1 history: last 12 bar moves (ATR units, trade direction) and the last 10 closed trades
        c = b['c']; lo = max(0, k - 11)
        mv = [float(np.round(s * ((c[i] - (c[i - 1] if i > 0 else c[0])) / a[i]) * 2) / 2) for i in range(lo, k + 1)]
        hist = list(self.history[-10:])
        self.pending = dict(side=s, risk=float(self.cfg['stop_atr'] * a[k]), signal_t=close_t, entry_after=close_t, F=F, moves=mv, hist=hist)
        return self.pending
