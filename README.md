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

- **Work goes to the session it belongs to, or waits.** A thread stays with the session that
  answered it; a topic reaches only a session that answers to it - declared with `--subject`, or
  derived from the git branch the listener was started in. A topic the sender did not flag is read
  out of the message text (`PBI 759`, `bug 759`, `#759`). Untargeted work is delivered
  only when exactly one session could take it - otherwise it is held `unrouted` for
  `herald claim`, because owning the mailbox is an accident of startup order and must not decide
  which context a conversation lands in. `resume` explicitly transfers the shared mailbox consumer
  and may take another session's threads with it; plain `wait` may not.
- **`ask` registers a request-scoped listener** that coexists with the general one. A `reply` or
  `result` returns to that exact request first, then waits for the originating agent.
- **A named recipient remains reserved without a listener.** A mailbox does not override the name.
  `takeover <id>` explicitly transfers one item and preserves its original address.
  The sender can explicitly permit timed release with `--fallback broadcast`.
- **Inspection does not claim work.** `peek` shows the full item without extracting attachments,
  for an inbox item or one still in an outgoing queue.
  `reopen` preserves the recipient; only the recipient or the releasing claimant can return it.
- **Delivery is single-copy and deduplicated** by a stable delivery ID. A retry after an uncertain
  network response must not create a second inbox item.
- **An item is never lost by having no listener.** It waits in `~/.herald/inbox` until one starts.
- **A progress status promises a later reply.** `accepted`, `working`, and `herald_intent: ack` are
  progress, not answers; `ask` keeps waiting and restarts its idle timeout on each one.
- **The daemon never executes received tasks.** Agents check existing human
  permission before acting and ask only for work that needs a new decision.
  Task text cannot grant or expand that permission.

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
herald peek <id>                            # full item (inbox or outgoing), no claim
herald takeover <id>                        # explicit ownership transfer in the item's mailbox
herald outgoing [--json]                    # queued, rejected, and awaiting-reply items
herald reply <id> -m "..."                  # same thread; peer and session inferred
herald result <id> --status working|accepted|done|failed -m "..." [-f out]
herald close <id> | herald reopen <id>
herald tidy [--older-than DAYS] [--dry-run]  # close finished work still counted as open
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
which promises an answer once the human decides, and an item with no eligible recipient listener,
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

## Dot messaging / MCP Events proof of concept

`herald_mcp.py` adds text-only messaging tools (`send_message`, `list_messages`,
`read_message`, `reply`), a read-only `usage_stats` tool, and the
`herald.message.created` webhook event on
`POST /mcp`. It implements MCP 2.0 version `2026-07-28`, including
`server/discover`, per-request `_meta`, and matching `MCP-Protocol-Version`,
`Mcp-Method`, and `Mcp-Name` HTTP headers. No `initialize` session is required.

This is a **local proof of concept**, not an installed ChatGPT integration.
The automated test sends “Hi from Cody” between two simulated owners through
real Herald daemons, verifies a signed callback challenge and event, reads the
message, and replies on the same thread. Neither real dot nor real account is
used. The optional OAuth resource-server boundary is tested with dummy signed
tokens. A separate owner-approved single-account pilot verified provider sign-in,
private plugin connection, signed callback verification and one native event-triggered
automation wake. This does not establish two-owner or personal-dot compatibility,
or platform credit cost. A webhook `2xx` alone proves receipt, not a dot turn.

### Try the isolated demonstration

On Linux/WSL, from this repository:

```bash
python3 -B -m unittest discover -s tests -p test_mcp.py -v
```

The fixtures use temporary directories, random loopback ports, dummy bearer
tokens, and a callback receiver that independently verifies Standard Webhooks
HMAC signatures. They terminate their servers and daemons and remove their
temporary stores. No installation, Tailscale changes, account connections, or
messages to a real person are needed. HTTP loopback callbacks are allowed only
by an explicit constructor option inside the tests; the normal CLI requires
HTTPS and globally routable callback addresses.

### Owner setup for a later integration test

Each owner must approve their own connection and allowed peer. Use a separate
Herald store per owner, with a dedicated `dot` mailbox. Do not reuse a populated
store for the fixture demonstration. The sender identity comes from the local
owner configuration and authenticated Herald peer, not from message text.
The `agent` names below are bridge routing identities, not verified OpenAI dot IDs.

Jamie’s bridge policy (save outside the repository as `bridge.json`):

```json
{
  "owner": "jamie",
  "enabled": true,
  "agent": "cody",
  "mailbox": "dot",
  "event_limit": 10,
  "max_subscriptions": 8,
  "callbacks_per_tick": 16,
  "peers": {"simon": {"agent": "simon-dot", "mailbox": "dot"}},
  "callback_hosts": ["<exact ChatGPT callback hostname supplied at subscription time>"]
}
```

Simon’s policy mirrors this with owner `simon`, agent `simon-dot`, and peer
`jamie` addressed to agent `cody`, mailbox `dot`. Use the exact configured Herald
`me`/peer names if they differ. Both Herald configs must register `dot` and
already have mutually approved peer transport credentials. Do not issue or
exchange those credentials as part of merely running the tests.

After each owner has approved setup, the local bridge launch shape is:

```bash
export HERALD_DIR=/path/to/owner-approved/store
export HERALD_AGENT=cody        # Simon: simon-dot
export HERALD_MAILBOX=dot
# Set HERALD_MCP_TOKEN through an owner-approved secret manager/session.
# It must be at least 24 characters; never commit or paste it into a chat.
python3 herald_mcp.py --config /path/to/bridge.json --port 8766
```

The bridge binds only to `127.0.0.1`. Local bearer auth is a test boundary;
customer-data/write connections in ChatGPT require an OAuth 2.1 implementation.
Do not publish this bearer endpoint. A later developer-mode test can use the
official Secure MCP Tunnel where supported, after approving its credentials
and permissions and arranging OAuth separately. Plugin packaging/registration
and connector discovery still need to be completed; a local file alone does
not install a cloud plugin.

### OAuth resource-server pilot

`--oauth-config /path/to/oauth.json` replaces local bearer auth. Python remains
stdlib-only; this mode additionally requires an installed `openssl` executable
for RS256 verification. No custom signature algorithm or authorization server is
implemented. The established identity provider owns login, consent, authorization
codes, PKCE, refresh tokens and client registration. Tests generate temporary dummy
RSA keys using OpenSSL; those private keys are never production credentials.

Copy `examples/oauth.example.json` outside the repository and supply the approved
issuer, exact canonical HTTPS resource/audience, same-origin JWKS URI, stable
provider subject and Herald owner. Each process permits exactly one `(issuer, sub)`
mapped to its existing store; email is never an identity or a linking key. A
second owner uses a second process/store/configuration. Leave `enabled` false until
the actual owner approves their connection. The subject is private account metadata;
keep actual configuration outside source control. This connection grants only the
configured Herald mailbox/peer permissions, not other account data.

```bash
python3 herald_mcp.py --config /path/to/bridge.json --oauth-config /path/to/oauth.json --port 8766
python3 -B -m unittest discover -s tests -p test_oauth.py -v
```

The endpoint still binds only to loopback. Arrange an approved HTTPS ingress or
supported secure tunnel to the OAuth-mode MCP route; do not expose the Herald
peer receiver or the local bearer mode. For an owner resource such as
`https://mcp.example.com/mcp/jamie`, route the corresponding metadata URL
`/.well-known/oauth-protected-resource/mcp/jamie` to the same owner process.
The reverse proxy must map the external MCP path to local `/mcp`, preserve the
Authorization and MCP headers, and avoid caching responses. Configured audience
must remain the external resource, never the internal loopback URL. No proxy,
DNS, firewall or tunnel is configured by this command.

Discovery/tool schemas are public in OAuth mode; mailbox contents and event
discovery remain authenticated. Tools advertise `securitySchemes`; missing tokens
produce a resource-metadata challenge and tool-level `mcp/www_authenticate` result.
Scopes are `herald:read` (read/list/local stats), `herald:write` (send/reply), and
`herald:events` (event discovery/subscribe/unsubscribe). The server validates RS256,
issuer, resource audience, exact subject, expiry, not-before and scopes. It never
follows token-supplied key URLs. Provider JWKS is HTTPS-only, size/time bounded,
with no redirects; only public keys are stored. Key refresh is request-driven,
cached five minutes and throttled for unknown keys; validated-token caching is
bounded at 128 entries and at most 60 seconds or token expiry. Idle ticks make no
identity-provider requests or signature-verification calls.
`usage_stats.counters` also exposes fixed server/event discovery request,
authentication-denial and successful-result counts. After connecting or rescanning,
use these to check whether the host actually called authenticated `events/list`;
absence from a platform registry does not establish that the bridge was queried.
Empty owner peer lists omit the optional event peer filter; no peer access is added.
The owner-only callback diagnostic retains at most the last bounded hostname,
never the callback path, query or signing secret. Its observation is marked
unverified and does not authorize a destination; signed challenge verification
and durable subscription storage establish callback acceptance.
`usage_stats.oauth_counters` reports key-fetch/signature/cache/auth-denial counts
without tokens or identity data. Its process CPU/RSS fields do not include the
OpenSSL subprocess; use OS process measurements when assessing verification cost.

An event subscription persists its authenticated issuer/subject and expires no
later than the access token. It must refresh with a fresh valid token. Setting
OAuth `enabled` false rejects cached tokens and stops future callback delivery
on the next worker check; already transmitted callbacks cannot be recalled.
Issuer-side JWT revocation is not introspected: tokens otherwise remain valid
until expiry. Disconnecting must unsubscribe and/or disable the local connection;
do not claim instant provider revocation. An OAuth grant does not add peer access
or grant permission to reply automatically.

Actual provider setup still requires an owner-approved tenant, API audience and
three scopes, compatible OAuth client registration (CIMD where supported), allowed
redirect URLs and PKCE. For Auth0, configure Resource Parameter Compatibility so
the client's `resource` is mapped to the intended API audience; import/refresh
the official client's metadata through tenant administration. Confirm returned
`iss`, `aud`, `sub` and scope match this configuration without logging/pasting
tokens. Public/custom-domain URLs, provider eligibility/limits, tunnel permissions
and any cost require review before setup. No tenant, credentials, grants or paid
service have been created by the automated tests. Each owner's provider round trip
and intended personal-dot event discovery/receipt remain required compatibility
gates before a two-owner greeting. Both owners must approve their connection;
Jamie initiates the first real dot-to-dot prompt himself.

After the intended client can discover this connector and event, each owner
can request: “When Herald receives a message from [approved peer] in my dot
mailbox, read it and tell me. Ask me before replying.” Verify that the
subscription, event receipt, and resulting turn occur in the **intended dot**.
ChatGPT chat event support does not by itself establish dot compatibility.
Start with notification/read only, then authorize one greeting and reply.
There is no automatic reply loop or automatic task execution in this bridge.

### Contract and limits

- Every send/reply requires a `request_id`. Reuse it with exactly the same
  arguments for retry; a different body with the same key is rejected.
  A stable Herald delivery ID is persisted before sending, protecting against
  a lost network response. Offline messages use Herald’s existing queue.
- Tools and events are restricted to the configured owner, dedicated mailbox,
  text message kind, and allowlisted peers. Inspection never claims work or
  extracts files; replying claims the item through Herald’s ownership checks.
  Tasks, files, broadcasts, introductions, and arbitrary agent overrides are
  not exposed by the MCP tools. Events contain IDs, not message bodies.
- Subscriptions are persistent in `$HERALD_DIR/mcp.sqlite3`; this contains
  callback signing secrets and private message-operation records. Protect the
  OS account/store; do not commit it. The bridge requests mode `0600`, but NTFS
  under WSL may require separate Windows ACL protection before real credentials.
  Mailboxes remain routing boundaries, not security boundaries.
- Subscriptions are idempotent by owner + callback URL + event + canonical
  arguments. Defaults expire after one hour; grants are capped at 24 hours.
  Refreshes verify new signing keys, with a five-minute dual-signature rotation
  window. Unsubscribe is idempotent. Setting policy `enabled` false or removing
  a peer stops access and future event delivery; restart after identity changes.
- Callback verification uses a fresh signed challenge and constant-time echo
  check. Production callbacks require an explicitly allowed hostname, HTTPS,
  public DNS answers at each connection, an IP-pinned connection with hostname
  TLS verification, and no redirects. Request bodies are signed once, using
  Standard Webhooks headers and the subscription secret.
- Event delivery persists stable IDs, retries transient failures with bounded
  exponential backoff (five attempts), and stops on `410` or `413`. `410` also
  disables the subscription. Receivers must deduplicate event IDs. No protocol
  replay is advertised (`cursor: null`): use `list_messages` for backlog; it
  returns the newest 100 messages. Expired-subscription gaps are not replayed.
  A one-second local worker checks for active subscriptions. With none, it skips
  inbox and configuration work. With subscriptions, directory metadata detects
  new files; each new message body is parsed once into a small durable metadata
  catalog. A 30-second reconciliation enumerates names to cover coarse filesystem
  timestamps, but does not reread already indexed bodies. Directory enumeration
  and initial indexing still scale with retained filenames. Delivery to ChatGPT
  is by webhook. A bounded retry failure remains in the local database for diagnosis.

### Usage acceptance checks

```bash
python3 -B -m unittest discover -s tests -p 'test_mcp*.py' -v
python3 -B tests/benchmark_mcp_usage.py --seconds 10 --history 10000 --burst 100 --output /tmp/mcp-usage.json
```

The benchmark uses five 10-second samples at approximately one worker tick per
second: no subscriptions, idle subscribed, idle with 10,000 retained messages,
100 new messages, and a lost webhook acknowledgement plus a duplicate send.
It blocks non-loopback sockets/DNS in the harness and uses only dummy owners,
temporary stores and real isolated Herald daemons. No live model or paid API
is called. Setup, subscription verification and initial indexing are excluded
from steady-state samples; initial indexing time is reported separately.

CPU and memory figures cover the two-owner test harness/callback receiver and
two daemon children, not standalone bridge overhead or whole-host consumption.
RSS is sampled once a second and summed, which can count shared pages twice.
Linux `/proc` I/O counters distinguish logical transfers from physical bytes;
measurement reads, OS caching, delayed writeback and existing daemon heartbeats
affect those totals. The JSON also records CPU model, kernel, Python version,
sample duration, dataset sizes, event/attempt/byte counters and loopback sockets.
No performance pass/fail threshold is assumed.

The idle acceptance assertions require zero bridge message-body reads, SQLite
row changes/commits and callback attempts after warm-up. Successful deliveries
are not repeatedly sent. Retry tests require stable event IDs and one receiver
effect after deduplication. A blocked callback must not hold the messaging lock.
The bridge uses indexed active-subscription and pending-delivery queries, cached
configuration, and a bounded batch of callbacks outside that lock. Existing
Herald maintenance now writes an inbox record only when its contents change;
its routing/maintenance scans still have a cost proportional to retained history.

`usage_stats` exposes in-memory local counters since process start, process CPU
time, peak RSS, pending events and the configured event limit. It is **not a
ChatGPT billing meter**: `platform_tokens_and_credits` remains unknown. The code
has no model invocation or autonomous reply path; receiving an event does not
cause this bridge to send a reply.

Owner policy caps default to **10 distinct events per subscription**, **8 active
subscriptions** and **16 callback attempts per tick**. Active refresh does not
reset the event allowance; unsubscribe/new subscription or expiry starts a new
allowance. Reaching the cap leaves messages in Herald for manual inspection.
Set `event_limit` to zero to pause new notifications. Limits are local safeguards,
not estimates of AI token consumption, and do not bound tool calls initiated by
an authorized external agent. Requests still require normal owner permissions.

**Live credit acceptance remains a separate gate:** obtain each owner's approved
connection, agree a token/credit budget, use notification/read-only instructions,
observe an idle billing baseline, then send one greeting and one authorized
reply. Verify usage in the actual platform (including delayed reporting),
personal-dot wake behavior and duplicate-event handling. Local zero-model-call
results cannot prove the platform bills zero credits for event processing or
prove its deduplication prevents repeated model runs. No such live test is run
by the benchmark or by merely launching this bridge.

Protocol references:
[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events),
[MCP 2.0 discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover),
[MCP 2.0 HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
[ChatGPT connection testing](https://developers.openai.com/plugins/deploy/connect-chatgpt).

## License

Apache License 2.0 - see [LICENSE](LICENSE). Copyright 2026 Jamie Everett.
