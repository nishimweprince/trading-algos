"""Strategies for the paper trader and its dashboard: the wr70 and jev groups from config.json, plus the extra accounts
in paper_variants.json (each a clone of a base group with overrides and its own tag prefix and group)."""
import copy, json, os

BASE = os.path.dirname(os.path.abspath(__file__))


def load():
    cfg = json.load(open(os.path.join(BASE, 'config.json')))
    strats = [s for s in cfg['strategies'] if s.get('group') in ('wr70', 'jev')]
    labels = {'jev': 'Former Jev group (5 strategies, no Jev filter)', 'wr70': 'WR70 group (9 strategies)'}
    notes = {}
    path = os.path.join(BASE, 'paper_variants.json')
    if os.path.exists(path):
        for v in json.load(open(path))['variants']:
            for s in [x for x in cfg['strategies'] if x.get('group') == v['base_group']]:
                c = copy.deepcopy(s); c.update(v.get('overrides', {}))
                c['tag'] = f"{v['prefix']}-{s['tag']}"; c['group'] = v['group']; c['base_tag'] = s['tag']
                strats.append(c)
            labels[v['group']] = v['label']; notes[v['group']] = v.get('backtest', '')
    return strats, labels, notes
