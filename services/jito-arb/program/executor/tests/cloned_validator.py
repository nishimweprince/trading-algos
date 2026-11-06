#!/usr/bin/env python3
"""Run unsigned round-trip executor diagnostics on a localhost validator.
Mainnet operations: account reads only. All files and validator state live in --work.
Requires built executor SBF, executor-probe and solana-test-validator. RPC_URL is an env var.
"""
import argparse,base64,json,os,pathlib,subprocess,time,urllib.request

def rpc(url,method,params):
    req=urllib.request.Request(url,json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),{'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=40) as f:d=json.load(f)
    if 'error'in d:raise RuntimeError(str(d['error']))
    return d['result']

def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--program',required=True)
    p.add_argument('--probe',required=True);p.add_argument('--validator',default='solana-test-validator');p.add_argument('--work',required=True)
    a=p.parse_args();m=json.load(open(a.manifest));root=pathlib.Path(a.work);root.mkdir(parents=True,exist_ok=True)
    url=os.environ['RPC_URL'];local='http://127.0.0.1:18899';loader='BPFLoaderUpgradeab1e11111111111111111111111'
    builtins={'11111111111111111111111111111111','ComputeBudget111111111111111111111111111111',m['executor']}
    keys=[k for k in m['accounts'] if k not in builtins and not k.startswith('Sysvar')]
    fetched={}
    for start in range(0,len(keys),100):
        batch=keys[start:start+100];vals=rpc(url,'getMultipleAccounts',[batch,{'encoding':'base64','commitment':'confirmed'}])['value']
        fetched.update(zip(batch,vals))
    # Upgradeable programs need their ProgramData account too. Program pointers are enum tag + 32-byte key.
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    def b58(b):
        n=int.from_bytes(b,'big');s=''
        while n:n,r=divmod(n,58);s=alphabet[r]+s
        return '1'*(len(b)-len(b.lstrip(b'\0')))+s
    for k,v in list(fetched.items()):
        if v and v['executable'] and v['owner']==loader:
            raw=base64.b64decode(v['data'][0])
            if int.from_bytes(raw[:4],'little')!=2:raise RuntimeError('unexpected program account')
            pd=b58(raw[4:36]);fetched[pd]=rpc(url,'getAccountInfo',[pd,{'encoding':'base64','commitment':'confirmed'}])['value']
    args=[a.validator,'--reset','--quiet','--ledger',str(root/'ledger'),'--rpc-port','18899','--faucet-port','18900',
          '--bind-address','127.0.0.1','--bpf-program',m['executor'],a.program]
    for k,v in fetched.items():
        if not v:continue
        # Fresh local slots precede mainnet program deployments; rebase ProgramData metadata.
        if v['owner']==loader:
            data=bytearray(base64.b64decode(v['data'][0]))
            if int.from_bytes(data[:4],'little')==3:
                data[4:12]=bytes(8)
                v=dict(v,data=[base64.b64encode(data).decode(),'base64'])
        f=root/(k+'.json');f.write_text(json.dumps({'pubkey':k,'account':v}));args+=['--account',k,str(f)]
    log=open(root/'validator.log','w');proc=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            if proc.poll()is not None:raise RuntimeError('validator exited; inspect '+str(root/'validator.log'))
            try:
                if rpc(local,'getSlot',[{'commitment':'processed'}])>=2:break
            except Exception:pass
            time.sleep(.5)
        else:raise RuntimeError('validator startup timed out')
        holder=rpc(local,'getAccountInfo',[m['holder'],{'encoding':'base64','commitment':'processed'}])['value']
        if not holder or holder['lamports']<20000000:
            raise RuntimeError('cloned payer missing or underfunded')
        base=[a.probe,'--pool',m['pool'],'--holder',m['holder'],'--executor',m['executor'],'--rpc',local,'--amount','1000000']
        def run(minimum):
            r=subprocess.run(base+['--min-out',str(minimum)],capture_output=True,text=True)
            (root/('probe-'+str(minimum)+'.log')).write_text(r.stderr+'\n'+r.stdout)
            if not r.stdout.strip():raise RuntimeError('probe produced no result: '+r.stderr[-500:])
            return json.loads(r.stdout.splitlines()[-1])
        first=run(0)
        if not first['ok'] or first['final_out']<=0:raise RuntimeError('baseline execution failed')
        # The pool can change observations/timestamps; simulation never commits its token balances.
        good=run(first['final_out']);bad=run(first['final_out']+1)
        result={'dex':m['kind'],'baseline':first,'at_minimum':good,'above_minimum':bad,
                'pass':good['ok'] and not bad['ok'] and bad['min_out_error']}
        (root/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
        if not result['pass']:raise RuntimeError('min_out gate failed')
    finally:
        proc.terminate()
        try:proc.wait(timeout=20)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()
if __name__=='__main__':main()
