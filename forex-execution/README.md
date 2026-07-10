# Forex Execution

`forex-execution` is a TypeScript/Fastify OANDA REST-v20 execution service. This initial implementation covers Phase 1 only: project bootstrap, validated configuration, practice/live URL resolution, an authenticated OANDA client, normalized broker errors, protected internal HTTP routing, and health endpoints.

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
- `GET /health/ready` - configuration and OANDA authentication smoke test via account summary.

All non-health endpoints must provide `X-Internal-API-Key`.
