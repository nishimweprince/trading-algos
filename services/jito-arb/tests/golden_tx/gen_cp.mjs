// Offline independent account/data goldens from upstream IDLs and web3.js PDAs.
// NODE_PATH=<fixture dependencies>/node_modules node tests/golden_tx/gen_cp.mjs
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { PublicKey } = require('@solana/web3.js');
const { BorshInstructionCoder, BN } = require('@coral-xyz/anchor');
const spl = require('@solana/spl-token');
const idl = name => JSON.parse(readFileSync(new URL(`../../harness/idl/${name}.json`, import.meta.url)));
const cp = idl('raydium_cp_swap'), pump = idl('pump_amm'), fees = idl('pump_fees');
const key = n => new PublicKey(Uint8Array.from({ length: 32 }, (_, i) => i === 0 ? n : 0));
const inputs = Object.fromEntries(['pool','user','mint_a','mint_b','user_a','user_b','vault_a','vault_b','config','observation','recipient'].map((n,i) => [n,key(i+1)]));
inputs.token_a = spl.TOKEN_2022_PROGRAM_ID; inputs.token_b = spl.TOKEN_PROGRAM_ID;
const cases = [];
for (const [dex, source] of [['cpmm',cp],['pumpswap',pump]]) for (const a_to_b of [true,false]) {
  const name = dex === 'cpmm' ? 'swap_base_input' : a_to_b ? 'sell_v2' : 'buy_exact_quote_in_v2';
  const ix = source.instructions.find(x => x.name === name);
  const x = inputs;
  const roles = dex === 'cpmm' ? {
    payer:x.user, amm_config:x.config, pool_state:x.pool,
    input_token_account:a_to_b?x.user_a:x.user_b, output_token_account:a_to_b?x.user_b:x.user_a,
    input_vault:a_to_b?x.vault_a:x.vault_b, output_vault:a_to_b?x.vault_b:x.vault_a,
    input_token_program:a_to_b?x.token_a:x.token_b, output_token_program:a_to_b?x.token_b:x.token_a,
    input_token_mint:a_to_b?x.mint_a:x.mint_b, output_token_mint:a_to_b?x.mint_b:x.mint_a,
    observation_state:x.observation,
  } : {
    pool:x.pool,user:x.user,base_mint:x.mint_a,quote_mint:x.mint_b,
    user_base_token_account:x.user_a,user_quote_token_account:x.user_b,
    pool_base_token_account:x.vault_a,pool_quote_token_account:x.vault_b,
    base_token_program:x.token_a,quote_token_program:x.token_b,
    global_config:PublicKey.findProgramAddressSync([Buffer.from('global_config')],new PublicKey(source.address))[0],
    buyback_fee_recipient:spl.getAssociatedTokenAddressSync(x.mint_b,x.recipient,true,x.token_b),
  };
  const accounts = ix.accounts.map(a => {
    let pubkey = roles[a.name];
    if (a.address) pubkey = new PublicKey(a.address);
    if (a.pda) {
      const seeds = a.pda.seeds.map(s => s.kind === 'const' ? Buffer.from(s.value) : roles[s.path].toBuffer());
      const program = a.pda.program ? new PublicKey(a.pda.program.value) : new PublicKey(source.address);
      pubkey = PublicKey.findProgramAddressSync(seeds,program)[0];
    }
    if (!pubkey) throw Error(`unresolved ${a.name}`);
    return { name:a.name,key:pubkey.toBase58(),signer:!!a.signer,writable:!!a.writable };
  });
  const args = Object.fromEntries(ix.args.map((a,i) => [a.name,new BN(i===0?123456789:98765)]));
  const data = new BorshInstructionCoder(source).encode(name,args).toString('base64');
  cases.push({dex,a_to_b,program:source.address,accounts,data});
}
writeFileSync(fileURLToPath(new URL('cp_swaps.json',import.meta.url)),JSON.stringify({
  oracle:'pinned upstream IDLs; Anchor BorshInstructionCoder; web3.js PDA and spl-token ATA',
  programs:{cpmm:cp.address,pumpswap:pump.address,pump_fees:fees.address},
  inputs:Object.fromEntries(Object.entries(inputs).map(([k,v])=>[k,v.toBase58()])),cases
},null,1)+'\n');
