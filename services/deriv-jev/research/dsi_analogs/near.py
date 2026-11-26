import json, glob, os
for f in sorted(glob.glob(os.environ.get('WF_DIR', '/opt/dsi-analogs/wf') + '/out/*.json')):
    x = json.load(open(f)); st = {k: v for k, v in x['stats'].items() if not k.startswith('hour')}
    top = sorted(st.items(), key=lambda kv: -abs(kv[1]['t_gross']))[:2]
    tn = max(st.items(), key=lambda kv: kv[1]['t_net'])
    print(f"{x['code']:8}", ' | '.join(f"{k} g{v['gross']:+.3f}bp t{v['t_gross']:+.1f} wk{v['cons_gross']:.2f} net{v['net']:+.3f}" for k, v in top),
          f"|| best net {tn[0]} {tn[1]['net']:+.3f} t{tn[1]['t_net']:+.1f} wk{tn[1]['cons_net']:.2f}")
