# Migration racer — interview record (Draft)

Date: 2026-10-05. Status: **Final** — accepted by operator ("accepted", 2026-10-05).
Source spec: `~/Downloads/Migration Racer Spec.md` (Oct 5, 2026, not in repo).
Existing code: `pump-fun/` TypeScript scalper (post-graduation, phases 0–6, paper/dry/live, Jito primary + RPC fallback, dashboard at pumpdesk.nishimweprince.dev, Frankfurt deploy in `pump-fun/deploy/fra/README.md`, branch `feat/fra-lean`, target `mode: dry-run`).

## Goal (settled)

Verify the spec's payout hypothesis offline first; then build a migration-racer variant in a new branch, dry-run it on the Frankfurt VM as its own process, expose its metrics through the existing dashboard, and report back on the deploy.

## Non-goals (settled)

- No live spend before verification; no capped live in this phase (separate later decision).
- No changes to the existing sniper's behavior; no new feed procurement; no standalone metrics server; no second-box provisioning.

## Artifact boundary (settled, pending acceptance)

In scope: offline R-verification script + findings; new branch `feat/migration-racer` from `feat/fra-lean`; racer entrypoint/process reusing send path, persistence, dashboard/API, deploy-fra; racer API endpoints + one minimal dashboard page; pm2 coexistence on the Frankfurt VM; deploy report.
Out of scope: live funding/trading, sniper behavior changes, new feeds, tunnel/SSH rework, password rotation (deferred by operator choice).

## Done means (settled, pending acceptance)

- [ ] R verification findings recorded (which hypothesis survives)
- [ ] New branch pushed; racer runs on Frankfurt VM in dry-run as its own pm2 process + wallet
- [ ] Racer metrics visible on the existing dashboard (endpoints + minimal page)
- [ ] Deploy report delivered (what runs where, mode, wallet separation, health checks)

## Decisions

- D1 (settled): variant in a new branch as a separate process/entrypoint reusing pump-fun's send path, persistence, dashboard/API, and deploy-fra. Racer never boots the scalper pipeline; gets its own process, core pinning, Helius key, wallet, and DB tables.
- D2 (settled): verify R offline first per spec §7 (sample migrates, rank callers, balance-diff caller payout), then build the racer to the surviving hypothesis. No live spend before verification.
- D3 (settled): dry-run soak first on Frankfurt; capped live only if dry-run numbers justify it, as a separate decision with a wallet cap set then.
- D4 (settled): reuse the existing LaserStream fra + Helius stack on the Frankfurt box; racer adds curve-remaining watching over existing RPC. No new feed procurement in this phase.
- D5 (settled): extend the existing dashboard — new racer API endpoints plus one minimal page, served through the existing tunnel and auth. No standalone metrics server.
- D6 (settled): same Frankfurt VM; racer runs as its own pm2 process with its own wallet/key alongside pump-desk-trader. `4.185.40.182` to be confirmed as that VM on first connection.
- D6b (settled): direct SSH with the provided password for this deploy. Risk accepted by operator: password shared in chat is treated as compromised per repo runbook; rotation after deploy strongly recommended but deferred by operator choice.
- D7 (settled): new branch `feat/migration-racer` from `feat/fra-lean`.

## Constraints (binding unless explicitly waived)

- Exactly one writer per wallet; foreign-signer guard halts entries. Racer gets its own wallet/key.
- Frankfurt target state is dry-run; going live is a separate decision.
- Secrets by env-var name only, never in `config.yaml`.
- No inbound ports; dashboard via Cloudflare Tunnel + Access + Basic Auth. Exception: D6b uses direct SSH + pasted password for this deploy (operator-accepted).

## Risks

- R (payout to `migrate` caller) unconfirmed — mitigated by D2 verify-first.
- Race competition with shred-level feeds; landing rate + tip sizing decide wins — measured in dry-run.
- Second process contends for the VM's 4 CPUs and RPC rate limits — mitigated by core pinning + separate Helius key.
- Pasted password treated as compromised — accepted for this deploy, rotation deferred.

## Validation (settled)

- Spec §7 offline checks gate the build (sample migrates, rank callers, balance-diff R).
- Dry-run soak gates any live discussion; live (if ever) judged on net PnL over a few hundred migrations, per spec §8.

## Unresolved / deferred (not blockers)

- Live wallet cap + go-live decision — separate decision after dry-run evidence (see D3).
- Racer wallet generation/funding — operator action, only needed at live.
- Explicit acceptance of this contract — pending.
