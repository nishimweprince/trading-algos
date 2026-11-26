import sys, numpy as np
sys.path.insert(0, '/opt/trading-algos/services/deriv-jev')
from rules import cross_up, cross_dn, lag
def dz_rules(b, L_tf, a):
    R = {}; sd = np.nanstd(np.diff(L_tf)) + 1e-15
    for n in (4, 16, 48):
        z = (L_tf - lag(L_tf, n)) / (sd * np.sqrt(n))
        for q in (1.0, 1.5, 2.0):
            s = np.zeros(len(z), np.int8); s[cross_up(z, q)] = 1; s[cross_dn(z, -q)] = -1
            R[f'dz{n}_q{q}_trend'] = s; R[f'dz{n}_q{q}_fade'] = -s
    return R
