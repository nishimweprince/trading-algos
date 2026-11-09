// Offline provenance + independent decoder verification. No RPC requests.
import fs from 'node:fs';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { BorshAccountsCoder } from '@coral-xyz/anchor';
import { PublicKey } from '@solana/web3.js';
const base = new URL('../../', import.meta.url);
const idlBytes = fs.readFileSync(new URL('harness/idl/raydium_clmm.json', base));
const idl = JSON.parse(idlBytes), coder = new BorshAccountsCoder(idl);
const hash = b => crypto.createHash('sha256').update(b).digest('hex');
const f = JSON.parse(fs.readFileSync(new URL('tests/fixtures/clmm_accounts.json',base)));
const normalize = v => {
  if (v?.toBase58) return v.toBase58();
  if (v?.constructor?.name === 'BN') return v.toString(10);
  if (Array.isArray(v)) return v.map(normalize);
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,normalize(x)]));
  return v;
};
assert.equal(hash(idlBytes), f.provenance.idl_sha256);
for (const [key,a] of Object.entries(f.accounts)) {
  const bytes = Buffer.from(a.data,'base64');
  assert.equal(hash(bytes), a.sha256, key);
  assert.deepEqual(normalize(coder.decode(a.type,bytes)), a.expected, key);
  assert.equal(a.owner, idl.address);
  assert(a.slot > 0);
  const pool = a.expected.pool_id;
  let seeds;
  if (a.type === 'TickArrayBitmapExtension') seeds=[Buffer.from('pool_tick_array_bitmap_extension'),new PublicKey(pool).toBuffer()];
  if (a.type === 'TickArrayState') {
    const be=Buffer.alloc(4);be.writeInt32BE(a.expected.start_tick_index);
    seeds=[Buffer.from('tick_array'),new PublicKey(pool).toBuffer(),be];
  }
  if (seeds) assert.equal(PublicKey.findProgramAddressSync(seeds,new PublicKey(idl.address))[0].toBase58(),key);
}
console.log(`Verified ${Object.keys(f.accounts).length} real accounts: SHA-256, pinned IDL decode and web3.js PDAs`);
