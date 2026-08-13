# herald — your agents, talking directly

Your coding agents (Claude Code, Codex, Copilot, …) talking to each other **directly over your own
network** — no cloud, no broker, no vendor in the middle. One file of stdlib Python you can read in
an afternoon.

<p align="center">
  <img src="docs/demo.gif" alt="A task sent from one machine's agent runs on another and streams its result back, with no human relaying" width="780">
</p>

Two people's agent sessions hold real conversations: threaded messages, task requests with a
lifecycle, and results with files flowing back. Each OS user runs one small receiver daemon on a
private [Tailscale](https://tailscale.com) network. The daemon authenticates messages, stores them in
one durable inbox, routes them to a mailbox, and retries offline sends. A blocked `herald wait` wakes
the current Claude, Codex, or Copilot session. The daemon never runs a task or answers for an agent.

**Why it's different:** most agent-interop tooling is heavyweight enterprise plumbing — brokers,
service meshes, cloud control planes. `herald` is the opposite: two developers, their two machines, a
direct encrypted wire between them, and nothing else. Nothing you send leaves your own devices.

The agent-side behaviour — staying reachable, claiming items when several sessions run at once,
triaging incoming work, threading discipline — lives in [skill/SKILL.md](skill/SKILL.md). That file
*is* the product; the Python is just transport.

---

*Everything below is written for an agent working with or on herald.*

## Invariants

Changing any of these changes the protocol. Read `skill/SKILL.md` before touching them.

- **One general listener owns a mailbox.** A new listener from a different agent supersedes it. The
  superseded listener exits without receiving the same item.
- **`ask` registers a request-scoped listener** that coexists with the general one. A `reply` or
  `result` returns to that exact request first, then falls back to the originating mailbox.
- **Delivery is single-copy and deduplicated** by a stable delivery ID. A retry after an uncertain
  network response must not create a second inbox item.
- **An item is never lost by having no listener.** It waits in `~/.herald/inbox` until one starts.
- **A progress status promises a later reply.** `accepted`, `working`, and `herald_intent: ack` are
  progress, not answers; `ask` keeps waiting and restarts its idle timeout on each one.
- **Received tasks are never auto-executed**, and task text is untrusted input.

## Item lifecycle

`pending` → `active` → `handled`, plus two delivery states that keep a response visible when it
could not be delivered.

| State | Meaning |
|---|---|
| `pending` | no agent has taken it |
| `active` | claimed, or acknowledged with a later reply promised |
| `responded_pending_delivery` | final response queued for an offline peer |
| `delivery_failed` | peer rejected the response; item stays visible |
| `handled` | final response delivered, or explicitly closed |

Handled records are kept as history and never deleted automatically.

## Command surface

```bash
export HERALD_AGENT=codex-ticket123   # required for session-scoped commands
# HERALD_MAILBOX defaults to "main"

herald send <peer> -m "text" [-f file]      # message
herald send <peer> -t "task text"           # task request
    --meta k=v                              # repeatable structured context
    --mailbox <name> | --agent <session>    # durable lane, or one live session
    --fallback broadcast|hold|bounce        # when an exact target never appears
herald ask <peer> -t "..."                  # send and wait for the reply in one command
herald ping <peer>                          # daemon liveness and version, no agent woken

herald inbox [--history|--unclaimed]        # open work, handled history, unpicked work
herald read <id>                            # show, write attachments, claim
herald reply <id> -m "..."                  # same thread; peer and session inferred
herald result <id> --status working|accepted|done|failed -m "..." [-f out]
herald close <id> | herald reopen <id>
herald thread <thread-id>                   # whole conversation, both directions
herald wait | herald resume                 # become listener; resume also shows existing open work
herald sessions | herald status | herald flush [peer]
herald mailbox list|add|remove|default
herald peer issue|add|list|remove           # issue mints a peer their inbound token
herald access                               # audit who can reach whom
herald bell                                 # ring the human's terminal
```

`--timeout` on `ask` is an **idle** timeout: each progress item restarts it. On expiry it exits 2
and tells the caller to run `herald resume`. Never wire an alert to that exit code - a short timeout
otherwise reports every idle stretch as a failure.

## Setup

```bash
curl -fsSL https://raw.githubusercontent.com/jreverett/herald/master/install.sh | bash -s -- --me alice
```

Clones the repo, installs Tailscale inside WSL and joins the tailnet (pausing once for the printed
login link), writes `~/.herald/config.json`, puts `herald` on PATH, installs the agent skill into
every Claude, Codex, Copilot and shared agent skill directory found, adds the Windows tray icon on
WSL-with-Windows, and starts the daemon as a systemd user service. No port forwarding: the daemon
binds straight onto the tailnet.

All agent products and account profiles under one OS user share that daemon and inbox. A second
Claude configuration directory does not create a second inbox.

Joining an existing tailnet needs no Tailscale account. The owner supplies an auth key and an
inbound token (`herald peer issue bob`):

```bash
curl -fsSL https://raw.githubusercontent.com/jreverett/herald/master/install.sh | bash -s -- \
  --me bob --auth-key tskey-auth-... \
  --peer alice --peer-url http://<alices-tailnet-ip>:8765 --peer-token <token-alice-issued-you>
```

The introduction carries bob's address and a token back; alice runs `herald accept <id>` and both
directions are authenticated. Manual equivalent: `herald peer add <name> <url> <token> && herald
introduce <name>`.

**A peer name must be exactly the other person's `--me`** - replies and results route back by it.

## Identity

`HERALD_AGENT` names one temporary agent session; use one value for every command in that session.
`HERALD_MAILBOX` names durable work that survives a tab, product, or account switch.

Mailboxes are routing boundaries, **not security boundaries**. Use a separate OS user or
`HERALD_DIR` for data needing real isolation.

## Security model

- The daemon binds only to the Tailscale interface (`listen.host: "auto"`), so the port exists on no
  other interface and is unreachable from the LAN or internet. It refuses to start on `auto` when
  Tailscale is down.
- Transport rides Tailscale's WireGuard encryption, device to device.
- Every peer holds its own inbound token, so the daemon authenticates the sender and stamps `from`
  itself - the payload's claimed identity is ignored and a peer cannot impersonate another.
- Files are capped at 100MB and filenames are sanitised on receipt.

## Working on this repo

```bash
python3 -m unittest discover -s tests     # stdlib only, ~46 tests, allocates its own ports
```

Protocol tests start two real daemons on loopback and drive them through the CLI, each with its own
temp `HERALD_DIR`, so they never touch a real install. A behaviour change needs a test that fails
against the previous implementation. Bump `__version__` and add a `CHANGELOG.md` entry stating what
broke and why, not just what changed.

## License

Apache License 2.0 - see [LICENSE](LICENSE). Copyright 2026 Jamie Everett.
