"""Jev System One call (stdlib only). State text and question are identical to dv/gate.py."""
import json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import numpy as np

QUESTION = {'reaches_1R': {'type': 'noul', 'instructions':
            'This is a trend-following entry. Is it likely that price moves at least one stop-distance in the '
            'proposed trade direction before it moves one stop-distance against it?'}}


def state_text(code, F, side):
    def bk(x, cuts, labels):
        return labels[int(np.searchsorted(cuts, x))]
    return {
        'instrument': f'{code}, a synthetic index whose drift switches between up, down and flat regimes',
        'proposed_trade': 'long' if side > 0 else 'short',
        'last_16_bar_move_in_trade_direction': bk(F[0], [-1, 0, 1, 2, 3], ['strongly against', 'against', 'flat', 'with', 'strongly with', 'very strongly with']),
        'last_60_bar_move_in_trade_direction': bk(F[1], [-1, 0, 1, 2], ['against', 'flat', 'with', 'strongly with', 'very strongly with']),
        'volatility_vs_recent_average': bk(F[2], [0.8, 0.95, 1.1, 1.3], ['much lower', 'lower', 'normal', 'higher', 'much higher']),
        'bars_since_trend_direction_last_flipped': bk(F[3], [3, 10, 30, 60], ['0-2', '3-9', '10-29', '30-59', '60+']),
        'previous_trade_of_this_strategy': {1: 'won', -1: 'lost', 0: 'none'}[int(F[4])],
        'trade_before_that': {1: 'won', -1: 'lost', 0: 'none'}[int(F[5])],
    }


def _ago(mins):
    return ['under 15 min', '15-60 min', '1-4 h', '4-24 h', 'over 1 day'][int(np.searchsorted([15, 60, 240, 1440], mins))]


def state_v1(code, F, side, hist, moves, now_min):
    """Jev history v1 (identical to dv/gate_rich.py): current state + last 10 closed trades + last 12 bar moves."""
    st = dict(state_text(code, F, side))
    st['recent_closed_trades_of_this_strategy_oldest_first'] = [
        {'direction_vs_proposed': 'same' if hs == side else 'opposite', 'result_R': float(np.round(hr * 2) / 2), 'closed': _ago(now_min - ht)}
        for hs, hr, ht in hist]
    st['last_12_bar_moves_in_trade_direction_atr_units_oldest_first'] = moves
    return st


def ask(key, model, state, url='https://api.typesafe.ai/v1/systemone', timeout=8, tries=3):
    """Returns (p, latency_s, error). Never raises."""
    body = json.dumps(dict(model=model, state=state, questions=QUESTION)).encode()
    err = None
    for i in range(tries):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            return float(d['answers']['reaches_1R']['noul']), time.time() - t0, None
        except urllib.error.HTTPError as e:
            err = f'HTTP {e.code}'
            if e.code in (400, 401, 403):
                break
        except Exception as e:
            err = type(e).__name__ + ': ' + str(e)[:120]
        time.sleep(1 + i)
    return None, None, err


def ask_median(key, model, state, n=3):
    """Ask Jev n times in parallel (its answers vary between calls) and decide on the median.
    Returns (median_p, [all p], max_latency_s, error). Uses whatever answers came back; None only if all failed."""
    with ThreadPoolExecutor(n) as ex:
        res = list(ex.map(lambda _: ask(key, model, state), range(n)))
    ps = [p for p, _, _ in res if p is not None]
    lat = max([l for _, l, _ in res if l is not None], default=None)
    errs = [e for _, _, e in res if e]
    if not ps:
        return None, [], lat, errs[0] if errs else 'no answer'
    return float(np.median(ps)), ps, lat, (f'{len(errs)} of {n} calls failed' if errs else None)
