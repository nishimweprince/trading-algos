// Prepare a per-DEX clone manifest from the real archived parity accounts.
// Optional --rpc URL performs getMultipleAccounts reads only. Never starts a
// validator, changes its ledger, deploys, signs, or sends a transaction.
import { readFileSync } from 'node:fs';
const args = process.argv.slice(2);
const dex = args[0];
if (!['cpmm','pumpswap'].includes(dex) || args.some((x,i) => i > 0 && x !== '--rpc' && args[i-1] !== '--rpc')) {
  throw Error('usage: node tools/cp_validator_preflight.mjs cpmm|pumpswap [--rpc URL]');
}
const capture = JSON.parse(readFileSync(new URL(`../tests/fixtures/parity_${dex}.json`, import.meta.url)));
const golden = JSON.parse(readFileSync(new URL('../tests/golden_tx/cp_swaps.json', import.meta.url)));
const programs = [golden.programs[dex]];
if (dex === 'pumpswap') programs.push(golden.programs.pump_fees);
// Clone SPL programs explicitly for a validator that does not ship Token-2022.
programs.push('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
  'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const accounts = [...new Set(capture.batches.flatMap(b => Object.entries(b.accounts).filter(([,v]) => v).map(([k]) => k)))].filter(k => !programs.includes(k));
const manifest = {dex, source:`parity_${dex}.json`, programs, accounts,
  validator_args:[...programs.flatMap(k=>['--clone-upgradeable-program',k]),...accounts.flatMap(k=>['--maybe-clone',k])],
  note:'Capture keys are a starting set. Add public simulation holder/ATAs, route accounts, and Kamino accounts for the chosen route. No executor deployment or validator launch is performed.'};
const rpcIndex = args.indexOf('--rpc');
if (rpcIndex >= 0) {
  const url = args[rpcIndex+1];
  if (!url) throw Error('--rpc requires URL');
  const keys = [...programs,...accounts], missing = [], wrongPrograms = [];
  for (let i=0;i<keys.length;i+=100) {
    const batch = keys.slice(i,i+100);
    const response = await fetch(url,{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:1,method:'getMultipleAccounts',params:[batch,{encoding:'base64',commitment:'confirmed',dataSlice:{offset:0,length:0}}]})});
    if (!response.ok) throw Error(`RPC HTTP ${response.status}`);
    const j = await response.json(); if(j.error) throw Error(JSON.stringify(j.error));
    j.result.value.forEach((a,n)=>{if(!a) missing.push(batch[n]); else if(programs.includes(batch[n]) && !a.executable) wrongPrograms.push(batch[n]);});
  }
  manifest.preflight = {missing,non_executable_programs:wrongPrograms};
  if (missing.length || wrongPrograms.length) process.exitCode = 1;
}
console.log(JSON.stringify(manifest,null,2));
