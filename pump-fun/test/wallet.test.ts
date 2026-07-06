import { describe, it, expect, afterEach } from 'vitest';
import { Keypair } from '@solana/web3.js';
import { Wallet, WalletError } from '../src/executor/wallet.ts';
import { PROGRAM_IDS } from '../src/core/constants.ts';

const ENV = 'TEST_WALLET_KEY';

describe('Wallet', () => {
  afterEach(() => {
    delete process.env[ENV];
  });

  it('loads a keypair from a JSON byte-array secret', () => {
    const kp = Keypair.generate();
    process.env[ENV] = JSON.stringify([...kp.secretKey]);
    const w = Wallet.load(ENV, 'live');
    expect(w.publicKey).toBe(kp.publicKey.toBase58());
    expect(w.ephemeral).toBe(false);
  });

  it('requires a key in live mode', () => {
    expect(() => Wallet.load(ENV, 'live')).toThrow(WalletError);
  });

  it('falls back to an ephemeral key in dry-run', () => {
    const w = Wallet.load(ENV, 'dry-run');
    expect(w.ephemeral).toBe(true);
    // base58 pubkeys are 43-44 chars depending on leading-zero bytes.
    expect(w.publicKey.length).toBeGreaterThanOrEqual(43);
    expect(w.publicKey.length).toBeLessThanOrEqual(44);
  });

  it('refuses to sign for a non-whitelisted program', () => {
    const kp = Keypair.generate();
    process.env[ENV] = JSON.stringify([...kp.secretKey]);
    const w = Wallet.load(ENV, 'live');
    expect(() => w.assertWhitelisted([PROGRAM_IDS.PUMP_SWAP, PROGRAM_IDS.SYSTEM])).not.toThrow();
    expect(() => w.assertWhitelisted(['MaliciousProgram1111111111111111111111111111'])).toThrow(WalletError);
  });
});
