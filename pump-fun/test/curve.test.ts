import { describe, it, expect } from 'vitest';
import {
  decodeBondingCurve,
  isEmptyCurve,
  curvePriceSol,
  curveRealSol,
  deriveBondingCurvePda,
  BONDING_CURVE_DISCRIMINATOR,
} from '../src/enrichment/curve.ts';
import { PROGRAM_IDS } from '../src/core/constants.ts';

/**
 * Fixtures are live mainnet bonding-curve accounts fetched 2026-09-18 via
 * getAccountInfo (processed): one fresh curve (virtual 30.9 SOL, real 0.9
 * SOL, complete=0) and one graduated curve (drained, complete=1).
 */
const FRESH_MINT = '7v4shBJmb73embNcid1dMQhZFhNTpBhtFPMFTm73b4bv';
const FRESH_PDA = '5WXUxYwfCGGJejU4ptmNpqQD9Rx4ozVa2wLCp6KoViYt';
const FRESH_DATA =
  'F7f4N2DYrGDUF053erMDALdfwTEHAAAA1H87K+m0AgC3s501AAAAAACAxqR+jQMAAMEp0sixukwfAbJ6JX39jcKJ9dqdN7lUxvMtnNWLAmHeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';
const GRAD_DATA =
  'F7f4N2DYrGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAxqR+jQMAAReF/axSlr8h7xFKBPsemakreHzVhv2jhkIhnU51IdmTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==';
const OWNER = PROGRAM_IDS.PUMP_FUN;

describe('decodeBondingCurve', () => {
  it('decodes a fresh mainnet curve with sane reserves', () => {
    const r = decodeBondingCurve(FRESH_DATA, OWNER, FRESH_PDA);
    expect(r).not.toBeNull();
    expect(r!.curveAddress).toBe(FRESH_PDA);
    expect(r!.complete).toBe(false);
    expect(r!.tokenTotalSupply).toBe(1000000000000000n);
    // 30.9 virtual SOL at creation scale, 0.9 real SOL collected.
    expect(r!.virtualSolReserves).toBe(30899527607n);
    expect(r!.realSolReserves).toBe(899527607n);
    expect(isEmptyCurve(r!)).toBe(false);
    const price = curvePriceSol(r!);
    expect(price).not.toBeNull();
    expect(price!).toBeGreaterThan(0);
    expect(curveRealSol(r!)).toBeCloseTo(0.899527607, 9);
  });

  it('decodes a graduated curve as drained and complete', () => {
    const r = decodeBondingCurve(GRAD_DATA, OWNER, 'curve');
    expect(r).not.toBeNull();
    expect(r!.complete).toBe(true);
    expect(r!.virtualSolReserves).toBe(0n);
    expect(isEmptyCurve(r!)).toBe(true);
    expect(curvePriceSol(r!)).toBeNull();
  });

  it('rejects foreign owners, bad discriminators, and short buffers', () => {
    expect(decodeBondingCurve(FRESH_DATA, 'other', FRESH_PDA)).toBeNull();
    const badDisc = Buffer.concat([Buffer.alloc(8, 7), Buffer.from(FRESH_DATA, 'base64').subarray(8)]).toString(
      'base64',
    );
    expect(decodeBondingCurve(badDisc, OWNER, FRESH_PDA)).toBeNull();
    expect(decodeBondingCurve(Buffer.alloc(20).toString('base64'), OWNER, FRESH_PDA)).toBeNull();
  });

  it('derives the PDA that owns the fixture account', () => {
    expect(deriveBondingCurvePda(FRESH_MINT)).toBe(FRESH_PDA);
    expect(BONDING_CURVE_DISCRIMINATOR).toBe('17b7f83760d8ac60');
  });
});
