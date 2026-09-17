import { describe, expect, it } from 'vitest';
import { selectAltMembers } from '../src/executor/altMembers.ts';

function ix(programId: string, keys: string[] = []) {
  return {
    programId: { toBase58: () => programId },
    keys: keys.map((k) => ({ pubkey: { toBase58: () => k } })),
  };
}

describe('selectAltMembers', () => {
  it('keeps static probe addresses and drops per-mint/per-user ones', () => {
    const members = selectAltMembers(
      [ix('progA', ['feeAcct', 'pool', 'user']), ix('progB', ['feeAcct', 'wsol'])],
      new Set(['pool', 'user']),
      ['tokenProgram'],
    );
    expect(members).toEqual(['feeAcct', 'progA', 'progB', 'tokenProgram', 'wsol']);
  });

  it('dedupes and always includes the static set even when unreferenced', () => {
    const members = selectAltMembers([ix('progA', ['x'])], new Set(), ['tokenProgram', 'progA']);
    expect(members).toEqual(['progA', 'tokenProgram', 'x']);
  });
});
