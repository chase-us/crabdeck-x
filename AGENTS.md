# Agent notes — CrabDeck Quantum

You are working in **crabdeck-x**, the Hermes + OpenClaw + Shell Cracked stack aimed at hermesclaw.ai.

## Read first

1. [SECURITY.md](SECURITY.md) — token, CORS, shell-exec defaults.
2. [`.cursor/skills/README.md`](.cursor/skills/README.md) — load the skill that matches the subsystem.
3. [artifacts/](artifacts/) — protocol and API contracts from the vault/bHive sprint.

## Hard rules

- Offload blocking work with `agents/offload.py` `run_blocking`. Watchdog is 20s.
- Heartbeats include `bhive_slot`. Python slots are unix **seconds** // 60. Node `minuteSlot` is **ms**.
- `ENABLE_SHELL_EXEC` stays `"0"` unless the operator explicitly opts in with an allowlist.
- Vault ingest is fail-open. Do not let a down `:7070` starve the gateway loop.
- Swarm round/quorum rules stay pure in `gateway/swarm.js`; peer prompt/dispatch logic stays shared in `agents/swarm.py`. OpenClaw never executes during a swarm round.
- Do not treat this VM as the production Cloudflare origin.

## Verify

```bash
python3 -m unittest discover -s vault
python3 -m unittest discover -s agents
python3 -m unittest discover -s orchestrator
(cd gateway && npm test)
(cd ui && npm run build)
```

UI changes require a real browser pass (tabs, gateway connect, Telemetry search), not a single screenshot.
