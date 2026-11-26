import json, numpy as np
rows = json.load(open('/opt/dsi-analogs/bt_all.json'))
ok = [r for r in rows if r['IS']['n'] >= 30 and r['OOS']['n'] >= 30]
print('code     nvar  OOS>0  OOSt>2  corrIS/OOS  | top-5 by IS t -> OOS meanR,t,n')
for code in dict.fromkeys(r['code'] for r in rows):
    R = [r for r in ok if r['code'] == code]
    if not R: print(code, 'no variants'); continue
    im = np.array([r['IS']['meanR'] for r in R]); om = np.array([r['OOS']['meanR'] for r in R]); ot = np.array([r['OOS']['t'] for r in R])
    top = sorted(R, key=lambda r: -r['IS']['t'])[:5]
    print(f"{code:8} {len(R):4} {np.mean(om>0):6.2f} {np.mean(ot>2):6.2f} {np.corrcoef(im,om)[0,1]:8.2f}   | " +
          '; '.join(f"{r['tf']}m {r['rule']} {r['exit']} IS{r['IS']['meanR']:+.2f}/t{r['IS']['t']:.1f} OOS{r['OOS']['meanR']:+.2f}/t{r['OOS']['t']:.1f}/n{r['OOS']['n']}" for r in top[:3]))
print()
print('Deployed DSI configs (trail exit):')
for code, tf, rule in [('DSI30',1,'mom16_q2.0_trend'),('DSI30',15,'mom4_q1.5_trend'),('DSI30',5,'mom16_q2.0_trend'),('DSI10',1,'rsi20_trend'),('DSI10',5,'mom4_q1.5_trend')]:
    for r in rows:
        if r['code']==code and r['tf']==tf and r['rule']==rule and r['exit']=='trail':
            print(f"  {code} M{tf} {rule}: widened {r['widened']}, IS {r['IS']}, OOS {r['OOS']}")
print()
for code in ('CBF1000','CBF500','RB100','RB200'):
    R = [r for r in ok if r['code']==code]
    print(code, 'best 8 by min(IS t, OOS t):')
    for r in sorted(R, key=lambda r: -min(r['IS']['t'], r['OOS']['t']))[:8]:
        print(f"   M{r['tf']} {r['rule']:18} {r['exit']:5} wid{r['widened']:.2f} IS n{r['IS']['n']} {r['IS']['meanR']:+.3f} t{r['IS']['t']:.1f} hit{r['IS']['hit']} | OOS n{r['OOS']['n']} {r['OOS']['meanR']:+.3f} t{r['OOS']['t']:.1f} hit{r['OOS']['hit']}")
    # family summary
    for fam in ('trend','fade'):
        F=[r for r in R if r['rule'].endswith(fam)]
        print(f"   {fam}: {len(F)} var, OOS meanR median {np.median([r['OOS']['meanR'] for r in F]):+.3f}, OOS>0 {np.mean([r['OOS']['meanR']>0 for r in F]):.2f}")
