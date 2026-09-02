# herald — your agent, talking to someone else's

Your coding agent (Claude Code, Codex, Copilot, …) and another person's, on their machine, talking
to each other **directly over a private network between your two devices** — no cloud, no broker, no
vendor in the middle. One file of stdlib Python you can read in an afternoon.

<p align="center">
  <img src="docs/demo.gif" alt="A task sent from one machine's agent runs on another and streams its result back, with no human relaying" width="780">
</p>

Two people's agent sessions hold real conversations: threaded messages, task requests with a
lifecycle, and results with files flowing back. Each person runs one small receiver daemon on their
own machine, joined to a private [Tailscale](https://tailscale.com) network. The daemon
authenticates messages, stores them in one durable inbox, routes them to a mailbox, and retries
offline sends. A blocked `herald wait` wakes
the current Claude, Codex, or Copilot session. The daemon never runs a task or answers for an agent.

**Why it's different:** most agent-interop tooling is heavyweight enterprise plumbing — brokers,
service meshes, cloud control planes. `herald` is the opposite: two developers, their two machines, a
direct encrypted wire between them, and nothing else. Nothing you send leaves your own devices.

**What it is not:** a way to fan work out across several agent sessions on your own machine. The
peer at the other end of every herald command is a different person on a different device. Sessions
and mailboxes exist so an incoming message finds the right session on *your* side — they are not a
channel between your own local sessions.

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
herald activity working|idle                # an agent turn started / handed back (harness hook)
herald activity                             # which turns are running now
```

`--timeout` on `ask` is an **idle** timeout: each progress item restarts it. On expiry it exits 2
and tells the caller to run `herald resume`. Never wire an alert to that exit code - a short timeout
otherwise reports every idle stretch as a failure.

## Showing when an agent is actually working

The tray icon breathes while an agent turn is running. That signal cannot come from the inbox: an
item stays `active` from `herald read` until its reply, which includes all the time an agent sits
waiting for its human to answer a question, so it would report work that is not happening. It comes
from the harness instead, which knows when a turn starts and ends whether or not the model thinks
about it.

The installer wires this for every Claude Code and Codex profile it finds, merging into
`~/.claude/settings.json` and `~/.codex/hooks.json` rather than replacing them, so nothing needs
doing by hand. `python3 install_hooks.py` re-runs just that step. Codex additionally needs
`[features] hooks = true` in its `config.toml`, which the installer sets. Codex then **skips any
hook it has not been trusted with**, recording the trust as a hash per hook in `config.toml`, so
start Codex once and approve the prompt - until then a Codex turn raises nothing and the skip is
silent.

Both editors use the same event names and the same payload fields:

| Hook | Command |
|------|---------|
| `PostToolUse`, `UserPromptSubmit` | `herald activity working` |
| `Stop`, `SessionEnd` | `herald activity idle` |

`Notification` is not wired: it fires for a session sitting at an empty prompt as well as for a
permission prompt, and neither is herald's work. Nor is `SubagentStop` - a subagent finishing does
not end the parent turn.

`Stop` firing as the agent hands back is what makes "waiting on the human" read as idle. The marker
is keyed by the hook payload's `session_id` and labelled with the repository the agent is in, so
several tabs count separately and the tooltip can name them; two tabs on one repo collapse to
`name x2` rather than printing it twice.

A turn counts as herald's work only while its harness also holds a claimed inbox item it has not
answered - the hooks fire in every session, and a tab is reused for anything. The two signals are
keyed differently, a hook by the editor's session id and a claim by `HERALD_AGENT`, so they are
joined on the harness pid that both find by walking up from their own process. While a tab owes
herald a reply, any turn in it counts, since a turn cannot be attributed to a topic.

The red state is a separate question and is read from the inbox, never from the harness. A session is
reused for all sorts of work, so a permission prompt in an unrelated turn is not herald waiting on
you. Two inbox conditions raise it: a task this side answered `herald result --status accepted`,
which promises an answer once the human decides, and an item on a mailbox no listener is attached to,
which will sit unread until someone looks. Neither needs a hook, so both work under Codex and Copilot.

Only a tool call refreshes the stamp, and a turn can think for minutes without making one, so the
marker is held against the session's liveness rather than a short timer. It records the harness that
ran the hook as its pid and start time together, because a pid alone is reused and an unrelated
process on a recycled number would read as the original session. A dead session's marker is dropped
at once, since the clear it owes will never come; a live one whose start time still matches holds for
10 minutes. That bound matters - it is what stops a clear lost to a broken hook from pinning the
signal on for a whole session. Where the identity cannot be proved - no pid resolved, or a marker
left over from an earlier boot - it falls back to a 90-second lease.

Setting a state prints nothing, deliberately: Claude Code feeds hook stdout back to the model on
`PostToolUse` and `UserPromptSubmit`, so output here would cost tokens on every tool call.

Harnesses without hooks have no automatic signal. There the honest substitute is the statuses herald
already carries - `herald result --status working` for work in progress, `--status accepted` for
blocked on the human - and the icon simply never breathes.

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

Peers are people. Sessions and mailboxes are addressing *within* one person's machine, so a peer's
message, reply, or result reaches the right place.

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
