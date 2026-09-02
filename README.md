# CRABDECK X

Sovereign AI orchestration operating system. This checkout now includes a
production-shaped **Convex** backend and a React command deck: typed schema,
WorkOS identity, custom-function access control, and a real-time task queue.

## What you get

- **Reactive database + API** — queries update the UI without a WebSocket layer
- **WorkOS AuthKit** — identity on the client, `users` row mapped by `tokenIdentifier`
- **Type-safe functions** — argument and return validators on every public endpoint
- **Convex-style RLS** — `authedQuery` / `authedMutation` wrap ownership checks
- **Paginated task list** — indexed by user (and completed status)

## Prerequisites

- Node.js 18+
- A [WorkOS](https://workos.com) AuthKit client ID for sign-in
- Convex CLI access (`npx convex dev`). Cloud agents should set
  `CONVEX_AGENT_MODE=anonymous` so they do not touch your personal deployment.

## Setup

```bash
npm install
cp .env.example .env.local
# add VITE_WORKOS_CLIENT_ID

# Development backend — never use `npx convex deploy` here
npx convex dev

# In a second terminal
npm run dev
```

`npx convex dev` writes `VITE_CONVEX_URL` into `.env.local`, generates
`convex/_generated`, and watches the backend. Open http://localhost:5173.

**Production only:** `npx convex deploy`. Do not run that during development.

## Backend map

| File | Role |
| --- | --- |
| `convex/schema.ts` | `users` and `tasks` with indexes |
| `convex/lib/auth.ts` | `getCurrentUser` / `getCurrentUserOrNull` |
| `convex/lib/customFunctions.ts` | Authenticated query/mutation wrappers |
| `convex/users.ts` | `store` on first sign-in, `me` |
| `convex/tasks.ts` | Paginated list, get, create, update, remove |
| `convex/health.ts` | Public deployment heartbeat |
| `convex/crons.ts` | Daily cleanup of completed tasks older than 30 days |

## Scripts

```bash
npm run dev          # Vite
npm run dev:backend  # convex dev
npm run lint
npm run typecheck
npm test
```
