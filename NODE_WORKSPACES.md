# Why the Node projects are not npm workspaces

`services/notification-service` and `apps/docs` each keep their own
`node_modules` and `package-lock.json`. The root `package.json` is a task
runner (`npm --prefix ...`), not an npm workspace.

This is deliberate. Hoisting the two projects into one tree breaks the docs
site:

- `notification-service` depends on `zod@^3.24.0`.
- `nextra` and `nextra-theme-docs` depend on `zod@^4.1.12`.

Under npm workspaces, `zod@3` wins the root slot and npm then installs *two
separate* nested `zod@4` copies — one under `nextra`, one under
`nextra-theme-docs`. Nextra builds a schema in one instance and validates it in
the other, so every MDX page fails to prerender with:

```
Invalid input: expected nonoptional, received undefined  → at children
```

The two projects share no code and no dependencies worth deduplicating, so the
hoisting has no upside to trade against that. If they are ever unified, the zod
major-version split has to be resolved first.
