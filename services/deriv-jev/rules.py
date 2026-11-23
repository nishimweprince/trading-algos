"""Rule families. Each rule maps bars -> side array (+1/-1/0) evaluated at bar close."""
import numpy as np


def ema(x, n):
    a = 2.0 / (n + 1); out = np.empty_like(x); out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0]); up = np.clip(d, 0, None); dn = np.clip(-d, 0, None)
    au = ema(up, 2 * n - 1); ad = ema(dn, 2 * n - 1)
    return 100 - 100 / (1 + au / np.maximum(ad, 1e-12))


def lag(x, n):
    y = np.empty_like(x); y[:n] = np.nan; y[n:] = x[:-n]; return y


def cross_up(x, thr):
    p = lag(x, 1); return (x > thr) & ~(p > thr)


def cross_dn(x, thr):
    p = lag(x, 1); return (x < thr) & ~(p < thr)


def generic_rules(b, a):
    """Family A/D: open rule search (momentum, breakout, RSI, EMA cross), trend and fade."""
    c = b['c']; R = {}
    for n in (4, 16, 48):
        z = (c - lag(c, n)) / (a * np.sqrt(n))
        for q in (1.0, 1.5, 2.0):
            s = np.zeros(len(c), np.int8); s[cross_up(z, q)] = 1; s[cross_dn(z, -q)] = -1
            R[f'mom{n}_q{q}_trend'] = s; R[f'mom{n}_q{q}_fade'] = -s
    for N in (12, 24, 96):
        hh = lag(_rollmax(b['h'], N), 1)
        ll = lag(_rollmin(b['l'], N), 1)
        s = np.zeros(len(c), np.int8); s[(c > hh) & ~(lag(c, 1) > lag(hh, 1))] = 1
        s[(c < ll) & ~(lag(c, 1) < lag(ll, 1))] = -1
        R[f'brk{N}_trend'] = s; R[f'brk{N}_fade'] = -s
    r = rsi(c)
    for lv in (20, 30):
        s = np.zeros(len(c), np.int8); s[cross_dn(r, lv)] = 1; s[cross_up(r, 100 - lv)] = -1
        R[f'rsi{lv}_fade'] = s; R[f'rsi{lv}_trend'] = -s
    for f, sl in ((8, 21), (21, 55)):
        d = ema(c, f) - ema(c, sl)
        s = np.zeros(len(c), np.int8); s[cross_up(d, 0)] = 1; s[cross_dn(d, 0)] = -1
        R[f'ema{f}_{sl}_trend'] = s; R[f'ema{f}_{sl}_fade'] = -s
    return R


def _rollmax(x, n):
    from numpy.lib.stride_tricks import sliding_window_view as sw
    out = np.full(len(x), np.nan); out[n - 1:] = sw(x, n).max(1); return out


def _rollmin(x, n):
    from numpy.lib.stride_tricks import sliding_window_view as sw
    out = np.full(len(x), np.nan); out[n - 1:] = sw(x, n).min(1); return out


def spike_flags(m, kind):
    """M1 spike flags. Boom spikes are up, Crash spikes are down."""
    body = m['c'] - m['o']
    scale = np.median(np.abs(body)) + 1e-12
    if kind == 'BOOM':
        return body > 15 * scale
    return body < -15 * scale


def cb_rules(b, m, kind):
    """Family C: Crash/Boom carry, post-spike, and spike hazard rules."""
    sp = spike_flags(m, kind)
    tsp = m['t'][sp]
    # minutes since last spike at each bar close
    close_t = b['t'] + (b['t'][1] - b['t'][0])
    idx = np.searchsorted(tsp, close_t) - 1
    since = np.where(idx >= 0, close_t - tsp[np.clip(idx, 0, None)], 1e9)
    carry = -1 if kind == 'BOOM' else 1
    n = len(b['c']); R = {}
    s = np.full(n, carry, np.int8); R['carry_always'] = s
    for T in (15, 60):
        s = np.zeros(n, np.int8); s[since <= T] = carry; R[f'carry_post_spike_le{T}'] = s
    for T in (240, 720):
        s = np.zeros(n, np.int8); s[since >= T] = carry; R[f'carry_quiet_ge{T}'] = s
        s = np.zeros(n, np.int8); s[since >= T] = -carry; R[f'hunt_ge{T}'] = s
    s = np.zeros(n, np.int8); s[since <= 15] = -carry; R['hunt_cluster_le15'] = s
    return R, sp
