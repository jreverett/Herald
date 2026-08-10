# herald — your agents, talking directly

Your coding agents (Claude Code, Codex, Copilot, …) talking to each other
**directly over your own network** — no cloud, no broker, no vendor in the
middle. One file of stdlib Python you can read in an afternoon.

<p align="center">
  <img src="docs/demo.gif" alt="A task sent from one machine's agent runs on another and streams its result back, with no human relaying" width="780">
</p>

Two people's agent sessions hold real conversations: threaded messages, task
requests with a lifecycle, and results with files flowing back. Each OS user
runs one small receiver daemon on a private [Tailscale](https://tailscale.com)
network. The daemon authenticates messages, stores them in one durable inbox,
routes them to a mailbox, and retries offline sends. A blocked `herald wait`
wakes the current Claude, Codex, or Copilot session. The daemon never runs a
task or answers for an agent.

**Why it's different:** most agent-interop tooling is heavyweight enterprise
plumbing — brokers, service meshes, cloud control planes. `herald` is the
opposite: two developers, their two machines, a direct encrypted wire between
them, and nothing else. Nothing you send leaves your own devices.

The agent-side behaviour (staying reachable, claiming items when several
sessions run at once, triaging incoming work, threading discipline) lives in
[skill/SKILL.md](skill/SKILL.md) — that file *is* the product; the Python is
just transport.

## Setup

One command, per person, in WSL:

```bash
curl -fsSL https://raw.githubusercontent.com/jreverett/herald/master/install.sh | bash -s -- --me alice
```

It clones the repo, installs Tailscale inside WSL and joins the tailnet
(pausing once for you to open the printed login link — the only manual step),
writes `~/.herald/config.json`, puts `herald` on PATH, installs the same agent
skill for existing Claude, Codex, Copilot, and shared agent skill directories, adds
the Windows system-tray status icon on WSL-with-Windows, and starts the daemon
as a systemd user service. In WSL there is no port forwarding to configure —
the daemon binds straight onto the tailnet. All agent products and account
profiles under that OS user share this daemon and `~/.herald` inbox. A second
Claude configuration directory does not create a second inbox.

**Joining someone who already runs herald — no Tailscale account needed:** the
tailnet owner generates an auth key (admin console → Settings → Keys → Auth
keys) and issues you an inbound token (`herald peer issue bob`), then sends both
with their address; one command installs everything, joins the network with no
sign-up or browser login, and introduces you:

```bash
curl -fsSL https://raw.githubusercontent.com/jreverett/herald/master/install.sh | bash -s -- \
  --me bob --auth-key tskey-auth-... \
  --peer alice --peer-url http://<alices-tailnet-ip>:8765 --peer-token <token-alice-issued-you>
```

Your introduction (delivered using that token, so alice's daemon authenticates
it as you) carries your own address and a token back; alice runs
`herald accept <id>` and both directions are connected and authenticated.
(Manual equivalent any time: `herald peer add <name> <url> <token> && herald
introduce <name>`.) Every peer has its own token, so the sender of each message
is authenticated — a peer can't impersonate another. `herald access` audits who
can reach whom.

Peer names must be exactly the name the other person installed with
(`--me`) — replies and task results are routed back by that name.

## Usage

```bash
export HERALD_AGENT=codex-ticket123   # distinct name for this agent session
# HERALD_MAILBOX defaults to the durable "main" mailbox

herald send bob -m "the QA refresh is done" -f ./results.csv
herald send bob -t "run the ImageGen tests" --meta repo=Studio --meta branch=feature/x
herald send bob -t "..." --mailbox work         # address bob's durable work mailbox
herald send bob -t "..." --agent bob-ticket99   # address one live session when required
herald ask bob -t "run the ImageGen tests"      # send AND wait for the reply, in one command
herald ping bob                                 # is bob's daemon up? which version? (no agent woken)

herald inbox                     # open work in this shared inbox
herald inbox --history           # handled history
herald inbox --unclaimed         # pending work not yet picked up
herald read <id>                 # show an item, write its files to cwd, claim it
herald reply <id> -m "..."       # reply into the same thread (peer + session inferred)
herald result <id> --status done -m "all green" -f test-output.txt
herald close <id>                # mark an item handled when no reply is needed
herald reopen <id>               # return a handled item to open work
herald thread <thread-id>        # whole conversation, both directions
herald wait [--timeout N]        # become the current listener for this mailbox
herald resume [--timeout N]      # take over and show existing open work first
herald sessions                  # agent sessions currently listening
herald mailbox list|add|remove|default
herald status                    # is the daemon running?
herald flush [peer]              # retry items queued for offline peers
herald peer issue|add|list|remove # manage peers (issue = mint a peer their inbound token)
herald access                    # audit who can reach whom, and who is authenticated
```

A typical exchange, no humans involved until judgement is needed:

```
alice's agent:  herald send bob -t "run the ImageGen tests" --meta branch=feature/x
bob's agent:  (woken by its background `herald wait`, claims the item on read)
                herald result <id> --status working -m "on it"
                ... runs the tests ...
                herald result <id> --status done -m "42 passed" -f results.trx
alice's agent:  (woken by its own `herald wait`, folds the result back into its work)
```

**Keeping it fast and cheap.** Most of what a listener does is acknowledge,
dispatch and simple triage, so run your *listening* session on a fast, low-cost
model and reserve a stronger one for sessions doing real work. `herald ask`,
`herald ping` and `wait --read` also cut the number of round-trips per exchange —
where most of the latency and token cost lives.

## Accounts, agents, and mailboxes

`HERALD_AGENT` names one temporary Claude, Codex, or Copilot session.
`HERALD_MAILBOX` names durable work that must survive a tab, product, or account
switch. It defaults to `main`.

- One general listener owns a mailbox at a time. A new listener from a different
  agent takes over. The old listener exits without receiving the same item.
- `herald ask` uses a request-scoped listener. It can run beside the general
  listener. Its reply returns to that exact request first.
- Use `herald resume` after an account or product switch. It shows open work that
  existed before the new session started. An acknowledged item remains open but
  is not acknowledged again.
- Use extra mailboxes only for deliberate work lanes. For example, run `herald
  mailbox add work`, then set `HERALD_MAILBOX=work` in the relevant launcher.
  `--mailbox work` addresses that lane. `--all` creates one item in every
  registered mailbox.
- Multiple mailboxes are routing boundaries, not security boundaries. Use a
  different OS user or `HERALD_DIR` for data that must be isolated.

Inbox items move through `pending`, `active`, and `handled`. An acknowledgement
keeps the original item active. A final reply or result marks it handled only
after delivery succeeds. A queued or rejected final response remains visible.
Handled JSON records stay as history; Herald does not delete them automatically.

If a peer is offline the send is queued and retried, not lost (`herald flush` to
push now).

## Human attention (optional)

The daemon is the single network receiver and durable source of truth. It keeps
running when no agent tab is open. Messages wait safely in `~/.herald/inbox`
until a listener starts or resumes. It also owns routing, delivery IDs,
deduplication, session leases, and offline retry.

The daemon can run a command whenever an item arrives — set `notify_command`
in config to an argv list; the item summary is appended as the last argument.
`notify-windows.sh` raises a Windows toast from WSL:

```json
"notify_command": ["/mnt/c/code/github/herald/notify-windows.sh"]
```

This is an attention signal only, for when you're not looking at the terminal.
Decisions still happen in the agent session, where the context is.

## Security model

- **No open ports on LAN or internet**: the daemon binds only to the Tailscale
  interface (`listen.host: "auto"`), so the port does not exist on any other
  interface — it is unreachable from the office network or the internet,
  satisfying strict no-unsecured-ports IT rules. The daemon refuses to start on
  `auto` if Tailscale isn't up.
- All transport rides Tailscale's WireGuard encryption, device-to-device.
- A bearer token identifies each peer; requests without a valid token are rejected.
- Mailboxes do not restrict an authenticated peer. Use a separate OS user or
  `HERALD_DIR` when work and personal data need separate access control.
- Received tasks are never auto-executed. The receiving agent triages them
  (skill/SKILL.md): safe read-only work runs autonomously, anything mutating is
  surfaced to the human, and task text is treated as untrusted input.
- File size capped at 100MB; filenames sanitised on receipt.

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 Jamie Everett.
