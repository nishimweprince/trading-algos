"""
Objective detectors for the Autochartist patterns with >70% published success rate.
One online pass over LTF bars. Fractal pivots of width k are known only k bars after they print and are kept
in an alternating high/low list. A completed shape "arms" a setup; a setup triggers on the first close
through its breakout line (no more than 0.75 ATR past it), within its expiry window, unless invalidated.

Notation: h = pattern height, A = ATR(14) of the LTF at confirmation. Every pattern needs h >= 1.5 A and a
span of 8..120 bars. Stops/targets are fixed at arming (flag stop at trigger).

  double_top / double_bottom   tops within 0.15h, trigger: close through the middle trough/peak.
                               stop: higher top + 0.2A ; target: neckline -/+ h
  head_shoulders / inverse_hs  as hs.detect (incl. shoulder depth >= 0.3h), sloped neckline.
                               stop: right shoulder +/- 0.2A ; target: neckline -/+ h
  ascending / descending triangle  4 pivots: flat side within 0.15h, other side converging by >= 0.25h.
                               long (asc) through the flat top / short (desc) through the flat bottom.
                               stop: last opposite pivot -/+ 0.2A ; target: line +/- h
  rectangle                    4 pivots, tops within 0.15h, bottoms within 0.15h. Two-sided breakout.
                               stop: rectangle midline ; target: line +/- h
  channel_up / channel_down    4 pivots, both lines sloping the same way, slope ratio 0.5..2.
                               Two-sided breakout of the (extrapolated) lines. stop: channel midline ; target: +/- width
  flag (bull/bear)             pole: two consecutive pivots >= 3A apart within <= 12 bars. Trigger: close beyond
                               the pole extreme within 30 bars; void if retrace > 50% of pole.
                               stop: opposite extreme since pole end -/+ 0.2A ; target: +/- pole
  resistance / support         >= 3 pivot highs (lows) within 0.25A of each other over the last 300 bars.
                               resistance -> long through the level; support -> short through it.
                               stop: last opposite pivot -/+ 0.2A ; target: level +/- (level to extreme between touches)
Expiry: 24 bars after arming (48 for support/resistance).
"""
import numpy as np, pandas as pd

def detect_all(lt, a, k):
    h, l, c = lt.h.values, lt.l.values, lt.c.values
    A = a.values; n = len(c)
    piv = []; armed = {}; out = []
    ph_hist, pl_hist = [], []   # (idx, price) of pivot highs / lows for S/R

    def arm(key, **kw):
        armed[key] = kw

    for t in range(2 * k, n):
        i = t - k; new = None
        if h[i] == h[i - k:t + 1].max(): new = (1, i, h[i])
        if l[i] == l[i - k:t + 1].min(): new = (-1, i, l[i]) if new is None else None
        At = A[t]
        if new is not None and At > 0:
            if piv and piv[-1][0] == new[0]:
                if (new[0] == 1 and new[2] > piv[-1][2]) or (new[0] == -1 and new[2] < piv[-1][2]): piv[-1] = new; added = True
                else: added = False
            else:
                piv.append(new); added = True
            if added:
                if new[0] == 1: ph_hist.append((new[1], new[2]))
                else: pl_hist.append((new[1], new[2]))
                P = piv
                # ---- double top / bottom (last 3 pivots s,-s,s)
                if len(P) >= 3:
                    (s, i0, p0), (_, i1, p1), (_, i2, p2) = P[-3:]
                    hg = s * ((p0 + p2) / 2 - p1)
                    if hg >= 1.5 * At and abs(p0 - p2) <= 0.15 * hg and 8 <= i2 - i0 <= 120:
                        ext = max(p0, p2) if s == 1 else min(p0, p2)
                        arm('double_top' if s == 1 else 'double_bottom', conf=t, exp=24, lines=[(-s, p1, 0.0, i1)],
                            stop={-s: ext + s * 0.2 * At}, tgt={-s: p1 - s * hg}, void=(s, ext))
                # ---- H&S / inverse (last 5 pivots)
                if len(P) >= 5:
                    (s, i1, H1), (_, j1, L1), (_, i2, H2), (_, j2, L2), (_, i3, H3) = P[-5:]
                    slope = (L2 - L1) / (j2 - j1); nk = lambda x: L1 + slope * (x - j1)
                    hg = s * (H2 - nk(i2))
                    if (hg >= 1.5 * At and s * (H2 - (max(H1, H3) if s == 1 else min(H1, H3))) >= 0.15 * hg
                            and abs(H1 - H3) <= 0.40 * hg and s * (H1 - nk(i1)) >= 0.3 * hg and s * (H3 - nk(i3)) >= 0.3 * hg
                            and abs(L1 - L2) <= 0.50 * hg and 8 <= i3 - i1 <= 120 and 0.4 <= (i2 - i1) / max(i3 - i2, 1) <= 2.5):
                        arm('head_shoulders' if s == 1 else 'inverse_hs', conf=t, exp=24, lines=[(-s, L1, slope, j1)],
                            stop={-s: H3 + s * 0.2 * At}, tgt={-s: None}, hgt=hg, void=(s, H3))
                # ---- 4-pivot shapes: triangles, rectangle, channels
                if len(P) >= 4:
                    q = P[-4:]
                    tops = [(x[1], x[2]) for x in q if x[0] == 1]; bots = [(x[1], x[2]) for x in q if x[0] == -1]
                    (it1, T1), (it2, T2) = tops; (ib1, B1), (ib2, B2) = bots
                    span = q[-1][1] - q[0][1]
                    hg = max(T1, T2) - min(B1, B2)
                    if hg >= 1.5 * At and 8 <= span <= 120:
                        flatT, flatB = abs(T1 - T2) <= 0.15 * hg, abs(B1 - B2) <= 0.15 * hg
                        if flatT and B2 - B1 >= 0.25 * hg:
                            top = max(T1, T2)
                            arm('ascending_triangle', conf=t, exp=24, lines=[(1, top, 0.0, it2)], stop={1: B2 - 0.2 * At},
                                tgt={1: top + (top - B1)}, void=(-1, B2))
                        if flatB and T1 - T2 >= 0.25 * hg:
                            bot = min(B1, B2)
                            arm('descending_triangle', conf=t, exp=24, lines=[(-1, bot, 0.0, ib2)], stop={-1: T2 + 0.2 * At},
                                tgt={-1: bot - (T1 - bot)}, void=(1, T2))
                        if flatT and flatB:
                            top, bot = max(T1, T2), min(B1, B2); mid = (top + bot) / 2
                            arm('rectangle', conf=t, exp=24, lines=[(1, top, 0.0, it2), (-1, bot, 0.0, ib2)],
                                stop={1: mid, -1: mid}, tgt={1: top + hg, -1: bot - hg}, void=None)
                        st, sb = (T2 - T1) / (it2 - it1), (B2 - B1) / (ib2 - ib1)
                        if st * sb > 0 and 0.5 <= st / sb <= 2 and not (flatT or flatB):
                            up = st > 0
                            width = (T2 - (B1 + sb * (it2 - ib1)))  # vertical distance at last top
                            if width >= 1.5 * At:
                                arm('channel_up' if up else 'channel_down', conf=t, exp=24,
                                    lines=[(1, T1, st, it1), (-1, B1, sb, ib1)], stop={1: 'mid', -1: 'mid'},
                                    tgt={1: width, -1: width}, void=None)
                # ---- flag: pole between last two pivots
                if len(P) >= 2:
                    (_, i0, p0), (s, i1, p1) = P[-2:]
                    pole = s * (p1 - p0)
                    if pole >= 3 * At and i1 - i0 <= 12:
                        arm('bull_flag' if s == 1 else 'bear_flag', conf=t, exp=30, lines=[(s, p1, 0.0, i1)],
                            stop={s: 'flag'}, tgt={s: pole}, void=(-s, p1 - s * 0.5 * pole), ext=(l[i1 + 1:t + 1].min() if s == 1 else h[i1 + 1:t + 1].max()) if t > i1 else p1)
                # ---- support / resistance (>=3 pivots within 0.25A in last 300 bars)
                hist = ph_hist if new[0] == 1 else pl_hist
                recent = [(ix, px) for ix, px in hist if t - ix <= 300]
                cl = [(ix, px) for ix, px in recent if abs(px - new[2]) <= 0.25 * At]
                if len(cl) >= 3:
                    s = new[0]
                    lvl = max(px for _, px in cl) if s == 1 else min(px for _, px in cl)
                    first = min(ix for ix, _ in cl)
                    seg_ext = l[first:t + 1].min() if s == 1 else h[first:t + 1].max()
                    opp = [x for x in piv if x[0] == -s]
                    if opp:
                        arm('resistance' if s == 1 else 'support', conf=t, exp=48, lines=[(s, lvl, 0.0, t)],
                            stop={s: opp[-1][2] - s * 0.2 * At}, tgt={s: lvl + s * abs(lvl - seg_ext)}, void=None)
        # ---- update flag extremes, check triggers
        for key in list(armed):
            p = armed[key]
            if key.endswith('flag'):
                sgn = 1 if key == 'bull_flag' else -1
                p['ext'] = min(p['ext'], l[t]) if sgn == 1 else max(p['ext'], h[t])
            if t - p['conf'] > p['exp']:
                del armed[key]; continue
            if p.get('void') is not None:
                vs, vp = p['void']
                if (vs == 1 and h[t] > vp) or (vs == -1 and l[t] < vp):
                    del armed[key]; continue
            fired = False
            for side, p0, slope, j0 in p['lines']:
                line = p0 + slope * (t - j0)
                if side * (c[t] - line) > 0:
                    if side * (c[t] - line) <= 0.75 * A[t]:
                        stp = p['stop'][side]
                        if stp == 'mid':
                            other = [x for x in p['lines'] if x[0] == -side][0]
                            stp = (line + other[1] + other[2] * (t - other[3])) / 2
                        elif stp == 'flag':
                            stp = p['ext'] - side * 0.2 * A[t]
                        tg = p['tgt'][side]
                        if key in ('head_shoulders', 'inverse_hs'): tg = line + side * p['hgt']
                        elif key.startswith('channel') or key.endswith('flag'): tg = line + side * tg
                        out.append(dict(bar=t, pattern=key, side=side, stop=stp, target=tg, atr=A[t], line=line, conf=p['conf']))
                    fired = True; break
            if fired: del armed[key]
    return pd.DataFrame(out)
