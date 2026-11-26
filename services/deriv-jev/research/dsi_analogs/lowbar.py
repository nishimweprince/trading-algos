"""Full-sample rules passing a lower bar (net t >= TMIN, net > 0, same sign in >= 80% of weeks, >= MINW weeks with
>= 10 trades), with the same bar applied to controls and hour-of-day rules as the false-positive baseline."""
import json, glob, os, collections
D = os.environ.get('WF_DIR', '/opt/dsi-analogs/wf'); TMIN = float(os.environ.get('WF_TMIN', '2')); MINW = int(os.environ.get('MINW', '10'))
R = {os.path.basename(f)[:-5]: json.load(open(f)) for f in sorted(glob.glob(f"{D}/{os.environ.get('WF_OUT', 'out')}/*.json"))}
CTRL = {c for c in R if c == 'V75' or c.startswith('CTRL')}
ok = lambda st: st['weeks'] >= MINW and st['t_net'] >= TMIN and st['net'] > 0 and st['cons_net'] >= 0.8
real = [(c, k, st) for c in R if c not in CTRL for k, st in R[c]['stats'].items() if not k.startswith('hour')]
ctrl = [(c, k, st) for c in R if c in CTRL for k, st in R[c]['stats'].items()]
hour = [(c, k, st) for c in R if c not in CTRL for k, st in R[c]['stats'].items() if k.startswith('hour')]
P = [x for x in real if ok(x[2])]; Pc = [x for x in ctrl if ok(x[2])]; Ph = [x for x in hour if ok(x[2])]
print(f"bar: net t>={TMIN:g}, net>0, >=80% weeks same sign, >={MINW} weeks with >=10 trades")
print(f"  real rules {len(real)}: {len(P)} pass ({100*len(P)/max(len(real),1):.2f}%)")
print(f"  control rules {len(ctrl)}: {len(Pc)} pass ({100*len(Pc)/max(len(ctrl),1):.2f}%)   hour rules {len(hour)}: {len(Ph)} pass ({100*len(Ph)/max(len(hour),1):.2f}%)")
for c, k, st in sorted(P, key=lambda z: -z[2]['t_net']):
    print(f"  {c:16} {k:28} n{st['n']:6} net {st['net']:+7.3f}bp gross {st['gross']:+7.3f} t {st['t_net']:4.2f} weeks+ {st['cons_net']:.2f} ({st['weeks']} wks)")
for c, k, st in Pc + Ph:
    print(f"  [control/hour] {c:16} {k:22} net {st['net']:+.3f} t {st['t_net']:.2f} weeks+ {st['cons_net']:.2f}")
