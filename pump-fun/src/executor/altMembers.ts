/**
 * ALT member selection for the H4 sellability probe (see alt-setup-cli.ts).
 *
 * Pure so it is unit-testable: every unique address referenced by the probe
 * instructions EXCEPT the per-mint/per-user ones (pool, vaults, base mint,
 * user, user ATA), plus the well-known static programs. Over-inclusion is
 * harmless (the probe only loads matching entries); under-inclusion just
 * leaves bytes on the table.
 *
 * Takes the minimal address-reporting shape (not the full web3.js
 * TransactionInstruction) so tests can use lightweight fakes.
 */
export interface AltInstructionAddresses {
  programId: { toBase58(): string };
  keys: Array<{ pubkey: { toBase58(): string } }>;
}

export function selectAltMembers(
  ixs: AltInstructionAddresses[],
  perProbeAddresses: ReadonlySet<string>,
  alwaysStatic: readonly string[],
): string[] {
  const members = new Set<string>(alwaysStatic);
  for (const ix of ixs) {
    members.add(ix.programId.toBase58());
    for (const k of ix.keys) members.add(k.pubkey.toBase58());
  }
  for (const a of perProbeAddresses) members.delete(a);
  return [...members].sort();
}
