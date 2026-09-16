/**
 * Reclaim rent from empty token accounts the wallet owns.
 *   npm run wallet:sweep-atas            # list only (dry run)
 *   npm run wallet:sweep-atas -- --execute   # close them (sends transactions)
 */
import { Connection } from '@solana/web3.js';
import { loadConfig, ConfigError } from '../config/load.ts';
import { Wallet } from './wallet.ts';
import { listEmptyTokenAccounts, sweepEmptyTokenAccounts } from './ataSweeper.ts';

async function main(): Promise<void> {
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  if (!config.rpc?.primaryHttp) throw new ConfigError('rpc.primaryHttp is required');
  const execute = process.argv.includes('--execute');
  const wallet = Wallet.load(config.wallet.keypairEnvVar, 'live');
  const connection = new Connection(config.rpc.primaryHttp, 'confirmed');

  const empties = await listEmptyTokenAccounts(connection, wallet.keypair.publicKey);
  const rent = empties.reduce((s, a) => s + a.lamports, 0) / 1e9;
  // eslint-disable-next-line no-console
  console.log(`wallet ${wallet.publicKey}: ${empties.length} empty token account(s), ${rent.toFixed(5)} SOL of rent`);
  for (const a of empties) console.log(`  ${a.address}  mint ${a.mint}  ${(a.lamports / 1e9).toFixed(5)} SOL`); // eslint-disable-line no-console
  if (!execute) {
    // eslint-disable-next-line no-console
    console.log('dry run — pass --execute to close them');
    return;
  }
  const r = await sweepEmptyTokenAccounts(connection, wallet, { priorityMicroLamports: config.fees.priorityFloorMicroLamports });
  // eslint-disable-next-line no-console
  console.log(`closed ${r.closed}/${r.found}, reclaimed ${(r.lamportsReclaimed / 1e9).toFixed(5)} SOL`, r.signatures, r.errors);
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
