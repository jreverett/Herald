# Tests

Stdlib `unittest` only — no third-party dependencies, matching herald's design.

```bash
python -m unittest discover -s tests
```

Pure helpers run in-process. The protocol is tested end to end by starting two
real daemons on loopback and driving them through the CLI. Coverage includes
authentication, durable mailbox routing, provider handoff, one-consumer
takeover, request-scoped `ask`, backlog resume, lifecycle transitions,
deduplicated delivery, broadcast copies, rejected and queued responses, legacy
history, exact-agent targeting, offline flush, and `ping`.

Each protocol test allocates its own free ports and a temp `HERALD_DIR`, so runs
are isolated and don't touch a real install.
