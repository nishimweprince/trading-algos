# Forex Execution

`forex-execution` is a TypeScript/Fastify OANDA REST-v20 execution service. This implementation covers Phases 1 and 2: project bootstrap, validated configuration, practice/live URL resolution, an authenticated OANDA client, normalized broker errors, protected internal HTTP routing, health endpoints, normalized account/instrument APIs, account snapshot persistence, and instrument metadata persistence.

## Repository layout

This directory is intentionally a package folder within the parent `trading-algos` repository, not a nested Git repository. Run Git commands from the parent repository root.

## Docker

Docker setup is intentionally excluded because this service is expected to be run with pm2.

## Setup

```bash
cp .env.example .env
npm install
npm run dev
```

Populate `.env` with an OANDA practice account ID and API token before starting. The service defaults to `OANDA_ENV=practice`; live startup is rejected unless `OANDA_ENV=live` and `LIVE_TRADING_ENABLED=true` are both set.

## Scripts

- `npm run dev` - run with `tsx watch`.
- `npm run build` - compile TypeScript.
- `npm run typecheck` - run the TypeScript checker without emitting files.
- `npm run lint` - run ESLint.
- `npm run test` - run Vitest unit/integration tests.

## Endpoints

- `GET /health/live` - liveness check.
- `GET /health/ready` - configuration, OANDA authentication, and instrument metadata smoke test.
- `GET /api/v1/account/summary` - normalized account summary and persisted snapshot.
- `GET /api/v1/account/details` - normalized account details and persisted snapshot.
- `GET /api/v1/account/instruments` - normalized and cached tradable instruments.
- `GET /api/v1/account/snapshots` - recent persisted account snapshots.
- `GET /api/v1/account/changes?sinceTransactionId=...` - account changes since an OANDA transaction ID.

All non-health endpoints must provide `X-Internal-API-Key`.
