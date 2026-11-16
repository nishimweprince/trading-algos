"""Incremental port of the research detectors.

`PatternDetector.step` is the body of `patterns.detect_all`'s loop and `HsDetector.step` the body of `hs.detect`'s
loop, for one new LTF bar. The batch loops only ever read bars <= t, so feeding bars one by one gives the same signals
(tests/test_detectors.py checks that against the vendored originals on the full history).

Signals are dicts with the same keys the batch functions put in their DataFrames.
"""

from __future__ import annotations

ALL_PATTERNS = (
    "double_top", "double_bottom", "head_shoulders", "inverse_hs", "ascending_triangle", "descending_triangle",
    "rectangle", "channel_up", "channel_down", "bull_flag", "bear_flag", "resistance", "support",
)


class PatternDetector:
    """patterns.detect_all, one bar at a time."""

    def __init__(self, k: int):
        self.k = k
        self.h: list[float] = []
        self.l: list[float] = []
        self.c: list[float] = []
        self.A: list[float] = []
        self.piv: list[tuple[int, int, float]] = []
        self.armed: dict[str, dict] = {}
        self.ph_hist: list[tuple[int, float]] = []
        self.pl_hist: list[tuple[int, float]] = []

    def step(self, h_: float, l_: float, c_: float, atr: float) -> list[dict]:
        h, l, c, A = self.h, self.l, self.c, self.A
        h.append(h_); l.append(l_); c.append(c_); A.append(atr)  # noqa: E702
        t = len(c) - 1
        k = self.k
        if t < 2 * k:
            return []
        piv, armed, ph_hist, pl_hist = self.piv, self.armed, self.ph_hist, self.pl_hist
        out: list[dict] = []

        def arm(key, **kw):
            armed[key] = kw

        i = t - k
        new = None
        if h[i] == max(h[i - k:t + 1]):
            new = (1, i, h[i])
        if l[i] == min(l[i - k:t + 1]):
            new = (-1, i, l[i]) if new is None else None
        At = A[t]
        if new is not None and At > 0:
            if piv and piv[-1][0] == new[0]:
                if (new[0] == 1 and new[2] > piv[-1][2]) or (new[0] == -1 and new[2] < piv[-1][2]):
                    piv[-1] = new
                    added = True
                else:
                    added = False
            else:
                piv.append(new)
                added = True
            if added:
                if new[0] == 1:
                    ph_hist.append((new[1], new[2]))
                else:
                    pl_hist.append((new[1], new[2]))
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
                    slope = (L2 - L1) / (j2 - j1)

                    def nk(x, L1=L1, slope=slope, j1=j1):
                        return L1 + slope * (x - j1)

                    hg = s * (H2 - nk(i2))
                    if (hg >= 1.5 * At and s * (H2 - (max(H1, H3) if s == 1 else min(H1, H3))) >= 0.15 * hg
                            and abs(H1 - H3) <= 0.40 * hg and s * (H1 - nk(i1)) >= 0.3 * hg
                            and s * (H3 - nk(i3)) >= 0.3 * hg
                            and abs(L1 - L2) <= 0.50 * hg and 8 <= i3 - i1 <= 120
                            and 0.4 <= (i2 - i1) / max(i3 - i2, 1) <= 2.5):
                        arm('head_shoulders' if s == 1 else 'inverse_hs', conf=t, exp=24, lines=[(-s, L1, slope, j1)],
                            stop={-s: H3 + s * 0.2 * At}, tgt={-s: None}, hgt=hg, void=(s, H3))
                # ---- 4-pivot shapes: triangles, rectangle, channels
                if len(P) >= 4:
                    q = P[-4:]
                    tops = [(x[1], x[2]) for x in q if x[0] == 1]
                    bots = [(x[1], x[2]) for x in q if x[0] == -1]
                    (it1, T1), (it2, T2) = tops
                    (ib1, B1), (ib2, B2) = bots
                    span = q[-1][1] - q[0][1]
                    hg = max(T1, T2) - min(B1, B2)
                    if hg >= 1.5 * At and 8 <= span <= 120:
                        flatT, flatB = abs(T1 - T2) <= 0.15 * hg, abs(B1 - B2) <= 0.15 * hg
                        if flatT and B2 - B1 >= 0.25 * hg:
                            top = max(T1, T2)
                            arm('ascending_triangle', conf=t, exp=24, lines=[(1, top, 0.0, it2)],
                                stop={1: B2 - 0.2 * At}, tgt={1: top + (top - B1)}, void=(-1, B2))
                        if flatB and T1 - T2 >= 0.25 * hg:
                            bot = min(B1, B2)
                            arm('descending_triangle', conf=t, exp=24, lines=[(-1, bot, 0.0, ib2)],
                                stop={-1: T2 + 0.2 * At}, tgt={-1: bot - (T1 - bot)}, void=(1, T2))
                        if flatT and flatB:
                            top, bot = max(T1, T2), min(B1, B2)
                            mid = (top + bot) / 2
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
                        if t > i1:
                            ext = min(l[i1 + 1:t + 1]) if s == 1 else max(h[i1 + 1:t + 1])
                        else:
                            ext = p1
                        arm('bull_flag' if s == 1 else 'bear_flag', conf=t, exp=30, lines=[(s, p1, 0.0, i1)],
                            stop={s: 'flag'}, tgt={s: pole}, void=(-s, p1 - s * 0.5 * pole), ext=ext)
                # ---- support / resistance (>=3 pivots within 0.25A in last 300 bars)
                hist = ph_hist if new[0] == 1 else pl_hist
                recent = [(ix, px) for ix, px in hist if t - ix <= 300]
                cl = [(ix, px) for ix, px in recent if abs(px - new[2]) <= 0.25 * At]
                if len(cl) >= 3:
                    s = new[0]
                    lvl = max(px for _, px in cl) if s == 1 else min(px for _, px in cl)
                    first = min(ix for ix, _ in cl)
                    seg_ext = min(l[first:t + 1]) if s == 1 else max(h[first:t + 1])
                    # piv alternates and piv[-1] is `new`, so the last opposite pivot is piv[-2]
                    opp = [piv[-2]] if len(piv) >= 2 else []
                    if opp:
                        arm('resistance' if s == 1 else 'support', conf=t, exp=48, lines=[(s, lvl, 0.0, t)],
                            stop={s: opp[-1][2] - s * 0.2 * At}, tgt={s: lvl + s * abs(lvl - seg_ext)}, void=None)
                # bound memory: pivots older than 300 bars never matter for S/R (the list scan is O(n) otherwise)
                if len(ph_hist) > 1000:
                    del ph_hist[:500]
                if len(pl_hist) > 1000:
                    del pl_hist[:500]
                if len(piv) > 64:
                    del piv[:32]
        # ---- update flag extremes, check triggers
        for key in list(armed):
            p = armed[key]
            if key.endswith('flag'):
                sgn = 1 if key == 'bull_flag' else -1
                p['ext'] = min(p['ext'], l[t]) if sgn == 1 else max(p['ext'], h[t])
            if t - p['conf'] > p['exp']:
                del armed[key]
                continue
            if p.get('void') is not None:
                vs, vp = p['void']
                if (vs == 1 and h[t] > vp) or (vs == -1 and l[t] < vp):
                    del armed[key]
                    continue
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
                        if key in ('head_shoulders', 'inverse_hs'):
                            tg = line + side * p['hgt']
                        elif key.startswith('channel') or key.endswith('flag'):
                            tg = line + side * tg
                        out.append(dict(bar=t, pattern=key, side=side, stop=stp, target=tg, atr=A[t], line=line,
                                        conf=p['conf']))
                    fired = True
                    break
            if fired:
                del armed[key]
        return out


class HsDetector:
    """hs.detect, one bar at a time (a list of armed patterns, not one per name)."""

    def __init__(self, k: int):
        self.k = k
        self.h: list[float] = []
        self.l: list[float] = []
        self.c: list[float] = []
        self.A: list[float] = []
        self.piv: list[tuple[int, int, float]] = []
        self.armed: list[dict] = []

    def step(self, h_: float, l_: float, c_: float, atr: float) -> list[dict]:
        h, l, c, A = self.h, self.l, self.c, self.A
        h.append(h_); l.append(l_); c.append(c_); A.append(atr)  # noqa: E702
        t = len(c) - 1
        k = self.k
        if t < 2 * k:
            return []
        piv = self.piv
        out: list[dict] = []
        i = t - k
        new = None
        if h[i] == max(h[i - k:t + 1]):
            new = (1, i, h[i])
        if l[i] == min(l[i - k:t + 1]):
            new = (-1, i, l[i]) if new is None else None
        if new is not None:
            if piv and piv[-1][0] == new[0]:
                if (new[0] == 1 and new[2] > piv[-1][2]) or (new[0] == -1 and new[2] < piv[-1][2]):
                    piv[-1] = new
                new_added = piv[-1] is new
            else:
                piv.append(new)
                new_added = True
            if new_added and len(piv) >= 5:
                p = piv[-5:]
                s = p[4][0]
                if [q[0] for q in p] == [s, -s, s, -s, s]:
                    (_, i1, H1), (_, j1, L1), (_, i2, H2), (_, j2, L2), (_, i3, H3) = p
                    slope = (L2 - L1) / (j2 - j1)

                    def neck(x, L1=L1, slope=slope, j1=j1):
                        return L1 + slope * (x - j1)

                    hgt = s * (H2 - neck(i2))
                    a = A[t]
                    ok = (a > 0 and hgt >= 1.5 * a
                          and s * (H2 - (max(H1, H3) if s == 1 else min(H1, H3))) >= 0.15 * hgt
                          and abs(H1 - H3) <= 0.40 * hgt and s * (H1 - neck(i1)) >= 0.3 * hgt
                          and s * (H3 - neck(i3)) >= 0.3 * hgt and abs(L1 - L2) <= 0.50 * hgt
                          and 8 <= i3 - i1 <= 120 and 0.4 <= (i2 - i1) / max(i3 - i2, 1) <= 2.5)
                    if ok:
                        self.armed.append(dict(s=s, H3=H3, L1=L1, j1=j1, slope=slope, h=hgt, conf=t, i1=i1, i3=i3))
            if len(piv) > 64:
                del piv[:32]
        keep = []
        for p in self.armed:
            s = p['s']
            if s * (h[t] if s == 1 else l[t]) > s * p['H3'] or t - p['conf'] > 24:
                continue
            nk = p['L1'] + p['slope'] * (t - p['j1'])
            if s * (c[t] - nk) < 0:
                if s * (nk - c[t]) <= 0.75 * A[t]:
                    out.append(dict(bar=t, side=-s, neck=nk, H3=p['H3'], h=p['h'], atr=A[t], width=p['i3'] - p['i1']))
                continue
            keep.append(p)
        self.armed = keep
        return out
