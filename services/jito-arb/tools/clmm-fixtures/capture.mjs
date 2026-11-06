// Read-only capture. Run npm ci here, then npm run capture -- [RPC URL].
// Independent Anchor IDL decode: no C++ offsets or outputs are consumed.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { BorshAccountsCoder } from '@coral-xyz/anchor';
import { PublicKey } from '@solana/web3.js';
const base = new URL('../../', import.meta.url);
const idlBytes = fs.readFileSync(new URL('harness/idl/raydium_clmm.json', base));
const idl = JSON.parse(idlBytes);
const coder = new BorshAccountsCoder(idl);
const program = new PublicKey(idl.address);
const rpc = process.argv[2] ?? 'https://api.mainnet-beta.solana.com';
const hash = b => crypto.createHash('sha256').update(b).digest('hex');
const normalize = v => {
  if (v?.toBase58) return v.toBase58();
  if (v?.constructor?.name === 'BN') return v.toString(10);
  if (Array.isArray(v)) return v.map(normalize);
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,normalize(x)]));
  return v;
};
const accounts = {};
async function read(key, type) {
  const k = key.toBase58();
  if (accounts[k]) return accounts[k].expected;
  const response = await fetch(rpc, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'getAccountInfo',params:[k,{encoding:'base64',commitment:'finalized'}]})});
  const j = await response.json();
  if (j.error || !j.result?.value) throw new Error(`RPC account unavailable: ${k}: ${JSON.stringify(j.error)}`);
  const a = j.result.value;
  const bytes = Buffer.from(a.data[0], 'base64');
  if (a.owner !== program.toBase58()) throw new Error('unexpected owner');
  const expected = normalize(coder.decode(type, bytes));
  accounts[k] = {type,owner:a.owner,slot:j.result.context.slot,sha256:hash(bytes),data:a.data[0],expected};
  await new Promise(resolve=>setTimeout(resolve,500));
  return expected;
}
for (const key of ['3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv','8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj']) {
  const pool = new PublicKey(key);
  const p = await read(pool,'PoolState');
  await read(new PublicKey(p.amm_config),'AmmConfig');
  const [bitmap] = PublicKey.findProgramAddressSync([Buffer.from('pool_tick_array_bitmap_extension'),pool.toBuffer()],program);
  const e = await read(bitmap,'TickArrayBitmapExtension');
  const width = 60*p.tick_spacing;
  const starts = [];
  for (let index=Math.floor(-443636/width);index<=Math.floor(443636/width);++index) {
    let words,bit;
    if(index>=-512 && index<512) {words=p.tick_array_bitmap;bit=index+512;}
    else {const block=index>=512?Math.floor(index/512)-1:Math.floor((-index-1)/512)-1;words=index>=512?e.positive_tick_array_bitmap[block]:e.negative_tick_array_bitmap[block];bit=(index%512+512)%512;}
    if ((BigInt(words[Math.floor(bit/64)]) >> BigInt(bit%64)) & 1n) starts.push(index*width);
  }
  const current = Math.floor(p.tick_current/width)*width;
  const selected = starts.sort((a,b)=>Math.abs(a-current)-Math.abs(b-current)).slice(0,3);
  // Include a real non-default array if available, even when it is distant from the active price.
  const extended = starts.find(s=>s < -512*width || s >= 512*width);
  if (extended !== undefined && !selected.includes(extended)) selected.push(extended);
  for (const start of selected) {
    const be=Buffer.alloc(4);be.writeInt32BE(start);
    const [tick] = PublicKey.findProgramAddressSync([Buffer.from('tick_array'),pool.toBuffer(),be],program);
    await read(tick,'TickArrayState');
  }
}
const out={provenance:{captured_at:new Date().toISOString(),network:'mainnet-beta',commitment:'finalized',rpc:'public mainnet RPC (or explicit CLI override)',program_source:'raydium-io/raydium-clmm@6d7a0f82d2f293c553c69aaf943377367c6c4cc6',idl_source:'raydium-io/raydium-idl@e7e0c96fe77bcf6a020b84a44c47a722aac8e359/raydium_clmm/raydium_clmm.json',idl_sha256:hash(idlBytes),decoder:'@coral-xyz/anchor@0.31.1 BorshAccountsCoder; @solana/web3.js@1.98.4 PDA',note:'Accounts are separate finalized reads, not an atomic swap snapshot or chain parity evidence.'},accounts};
const target = new URL('tests/fixtures/clmm_accounts.json',base);
fs.writeFileSync(target, JSON.stringify(out,null,2)+'\n');
console.log(`Wrote ${Object.keys(accounts).length} independently decoded accounts to ${fileURLToPath(target)}`);
