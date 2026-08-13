# Changelog

Versioning is `0.MAJOR.MINOR` while pre-1.0. `herald --version` prints the running version.

## 0.8.4

- `ask` now writes the attachments on a progress item, not only on the final
  reply. A peer that finishes work and reports it with `accepted` (because a
  remaining part needs its human) would name a file in the text while the caller
  received nothing - the attachment was on disk in the store but no path was ever
  printed, so it read as lost.

## 0.8.3

- `ask` now treats `--timeout` as an idle timeout rather than a total one. An
  acknowledgement restarts the wait, because a peer that sends `accepted`,
  `working`, or `herald_intent: ack` has committed to a later reply and should
  not then be cut off by the deadline set before it answered. Previously a peer
  could acknowledge in under two minutes, still be working, and lose the
  listener at 300s - leaving the real answer to sit unread with nothing
  listening.
- `ask` prints an acknowledgement in full instead of truncating it at 200
  characters, and flushes it, so the human sees the peer's stated plan while the
  work is still running rather than only in the final output.
- A timed-out `ask` now distinguishes "acknowledged but no final reply" from
  "no reply at all", and both say to run `herald resume`.

## 0.8.2

- `close` and `reopen` now work on an item whose agent session is gone. An item
  pinned to an exact agent, or claimed by one, was unreachable to every other
  agent once that session ended - and the reaper skips claimed items, so it
  never came back either. The only way out was to guess the dead session's name
  and set `HERALD_AGENT` to it. `reopen` also releases the dead pin, so the
  mailbox's general listener can pick the item up. An item whose target session
  is still live stays private to it, as before.

## 0.8.1

- Added the terminal bell (BEL), rung when the human is blocking the turn and an
  agent cannot proceed or reply without them. `herald result --status accepted`
  rings it, since that status means "waiting for my human to decide", and
  `herald bell` rings it for every other point where an agent must stop and ask.
  Arriving work, autonomous work, and progress updates stay silent.
- The bell goes to the terminal that owns the session, so it still arrives when
  an agent harness runs Herald with pipes for stdio and no controlling terminal.
  `HERALD_BELL=0` or `"bell": false` disables it; `HERALD_BELL_TTY` names an
  explicit device.

## 0.8.0

- Added durable mailboxes. Claude, Codex, Copilot, and multiple account profiles
  under one OS user now share one daemon and inbox without tying work ownership
  to a product session.
- Added unique listener instance IDs. One general consumer owns each mailbox,
  while request-scoped `ask` listeners can coexist and receive their exact
  replies. A new agent consumer cleanly supersedes the old one.
- Added `herald resume` to take over a mailbox and surface existing open work,
  including acknowledged work from a previous account or provider.
- Added explicit inbox states. Acknowledgements keep work active. Final replies
  become handled only after delivery. Queued or rejected final replies remain
  visible. `inbox` now shows open work by default, `--history` shows handled
  records, and `close` / `reopen` manage items explicitly.
- Added stable delivery IDs, receiver deduplication, atomic state updates, and a
  failed-delivery store. Retries cannot create duplicate inbox work.
- `--all` now creates one item per registered mailbox. `mailbox list`, `add`,
  `remove`, and `default` manage durable lanes. Mailbox changes take effect
  without a daemon restart.
- Legacy claimed items and consumed progress records appear as history instead
  of returning as new work.

## 0.7.4

- The receive protocol now requires an immediate acknowledgement for every
  message and task. An acknowledgement states the next action and whether a
  later reply will follow, including while the receiving agent waits for its
  human.
- Delayed message acknowledgements use `herald_intent: ack`. `herald ask` treats
  them as progress and continues to wait for the final reply.

## 0.7.3

- The skill now records that a timed-out `herald wait` exits 2. A short
  `--timeout` therefore reports every idle stretch as a failed background job,
  which teaches the reader to ignore listener exits entirely.

## 0.7.2

- The skill now states that a turn must never end with a reply outstanding and
  no listener running. `reply` and `send` start no listener of their own, and a
  `herald wait` exits once it delivers an item or its `--timeout` expires, so
  both routes leave an expected answer sitting unread in the inbox until
  someone happens to check. An open-ended expected reply gets a background
  `herald wait` with no timeout, and a returning wait is a prompt to restart it.

## 0.7.1

- Session-sensitive commands now require `HERALD_AGENT`, so an agent cannot
  send a message with a blank reply address or claim work under a shared
  hostname by mistake. The error gives the exact inline command format.
- The installer now links the Herald skill into existing Claude, Codex,
  Copilot, and shared agent skill directories instead of installing it only
  where an older Herald link already exists.
- Re-running the installer now restarts the existing daemon so an update does
  not leave the previous version running in memory.

## 0.7.0

- Single delivery: a message now goes to exactly one of the recipient's live
  sessions, not all of them. The daemon assigns each item to one session (the
  most recently active), so only that session's `herald wait` wakes; every other
  session skips it silently in-process and never wakes its agent. A reply routes
  back to the session that sent the request; if that session is gone it is
  reassigned to one other live session, not fanned out to every tab.
- `--all` on `send`/`reply`/`result` delivers to every one of the recipient's
  sessions, for a genuine announcement.

## 0.6.0

- Access control, part one: authenticated peer identity. Every peer now gets
  its own inbound token instead of one shared secret, so the daemon knows which
  peer sent each item and stamps `from` from the token - the payload's claimed
  identity is ignored and a peer can no longer impersonate another. The single
  shared inbox token is gone.
- `herald peer issue <name>` mints a peer their own inbound token and prints the
  two commands they run to connect. `introduce` mints a per-peer token too, so a
  completed introduce/accept leaves both directions authenticated. The daemon
  picks up newly issued tokens live (no restart).
- `herald access` audits who you can reach, who can reach you, and who is
  authenticated.
- Breaking: no migration from the shared-token config - re-run `install.sh` and
  reconnect peers. (Per-peer scoping - restricting a peer to part of the
  filesystem - and the spawn-on-delivery dispatcher build on this and come next.)

## 0.5.1

- Installer fixes from Simon's migration feedback. The skill is now relinked into
  every skills dir the machine uses - the standard `~/.claude`, any
  `CLAUDE_CONFIG_DIR`, and any other agent skills dir that already had the skill
  (extra Claude configs, other harnesses) - retiring the pre-rename `a2a` link in
  each, instead of only touching `~/.claude/skills`. Shell scripts are also now
  marked executable in git, so a fresh clone no longer needs `bash install.sh`.

## 0.5.0

- Streamlining to cut latency and token cost, which are dominated by the number
  of LLM round-trips per exchange.
- `herald ask <peer>` sends and blocks for the reply in a single command, so a
  synchronous request/reply is one turn instead of `send` + `wait` + `read`.
  Reachable peers only; an offline peer falls back to the async queue.
- `herald wait --read` prints and claims each item on wake, folding the `read`
  into the same turn.
- `herald ping <peer>` reports whether a peer's daemon is up and its version,
  answered by the daemon itself with no agent woken. The `/ping` endpoint now
  returns version and name.
- Skill guidance: work in as few turns as possible, don't re-verify setup before
  every action, and send a single result for quick tasks.
- Deferred: remote action-handler dispatch (running registered scripts in
  response to a peer's task) is held until the access-control identity and
  sandbox land - it must not ship before its security foundation.

## 0.4.0

- Renamed the project from `a2a` to `herald`. The former name collided with the
  Linux Foundation's Agent2Agent (A2A) protocol, which made the project
  undiscoverable and easy to mistake for that standard. The command is now
  `herald`, the config directory `~/.herald`, the env vars `HERALD_DIR` /
  `HERALD_AGENT`, and the daemon service `herald-daemon`. The installer migrates
  an existing `~/.a2a` install in place: it copies the config aside (token and
  peers preserved), retires the old daemon and command, and removes the legacy
  files only after the new daemon is confirmed running.

## 0.3.4

- Tray "Restart daemon" is now machine-agnostic. It hardcoded one machine's repo
  path (a silent no-op on any other clone) and would spawn a second daemon
  competing for the port on a systemd install rather than restarting the managed
  one. It now restarts the systemd service when present, otherwise falls back to
  the `herald` PATH wrapper the installer creates - no hardcoded paths.

## 0.3.3

- Fix tray cold-boot: the tray resolved the WSL `~/.herald` path only once at
  startup, so when it launched at login before WSL was warm the path stayed
  null and the icon pinned to grey "offline" permanently. Resolution is now
  retried lazily in the poll loop, so a cold-boot tray self-heals as soon as
  WSL answers.
- Fix reply mis-targeting: outbound items now stamp `from_agent` only when
  `HERALD_AGENT` is explicitly set. Previously it defaulted to the hostname, so a
  sender whose listener ran under a different name (the recommended convention)
  had every reply targeted at a session that never listens - held until the
  give-up window, then broadcast with a spurious "reassigned" notice. Unset now
  means broadcast, so replies reach any of the person's listeners immediately.
  To receive targeted replies, send and listen under the same `HERALD_AGENT`.

## 0.3.2

- The installer now sets up the Windows tray indicator automatically on
  WSL-with-Windows (previously a separate manual `setup-tray.ps1` step), so a
  normal install gives you the icon at every login with no extra step.
- `setup-tray.ps1` now installs a hidden VBScript launcher instead of a Startup
  shortcut to `powershell.exe`. A detached/shortcut launch from WSL or a script
  doesn't attach to the interactive desktop and paints its icon invisibly;
  wscript spawns the tray as a child that inherits the visible desktop, so the
  icon appears both at login and immediately on `enable`.

## 0.3.1

- Tray: send/receive arrows now linger ~4s (was 2s) so brief transfers stay
  visible long enough to notice.

## 0.3.0

- Session tracking: each `herald wait` now registers a heartbeat under
  `~/.herald/sessions/`, and a new `herald sessions` command lists the agent sessions
  currently listening. Records are pruned when a session exits or stops
  heartbeating. All file-based - no model tokens.
- Targeted delivery: `send`/`reply`/`result` accept `--agent <name>` to address
  a specific session of a peer. `reply`/`result` default to the originating
  session automatically. `herald wait` only wakes for items addressed to its own
  `HERALD_AGENT` (or broadcast), so N listeners no longer all wake per message.
  `herald inbox --mine` filters to items for this agent or broadcast.
- Claim stealing: `herald read` reclaims an item whose claiming session has died
  (no heartbeat, or its pid is gone on the same machine) instead of refusing.
- Undeliverable-target handling: if a targeted item's session never reappears,
  the daemon (after a give-up window) either releases it to any session and
  informs the sender (`--fallback broadcast`, default), keeps it pinned
  (`--fallback hold`), or bounces an undeliverable notice back
  (`--fallback bounce`). Reaping pauses for a grace period after the host wakes
  from sleep so sessions can re-check in first.

## 0.2.0

- New `herald status` command reports whether the daemon is running, its version,
  address, pid, uptime and queue depth (exits non-zero if down or stale).
- The daemon now maintains `~/.herald/status.json` with a heartbeat every few
  seconds, and touches `~/.herald/activity/{send,recv}` on each outgoing/incoming
  event, so an external monitor (e.g. a Windows tray icon) can show live
  running/sending/receiving state. The heartbeat and background retry now share
  one maintenance thread.
- Added a Windows system-tray app (`tray/`) that visualises this state: the
  "Two Roofs" chevron icon pivots per state (idle/sending/receiving/down),
  theme-aware for light and dark taskbars, with optional login auto-start.

## 0.1.0

- Offline sends are queued and retried instead of dropped. When a peer is
  unreachable, `send`/`reply`/`result` now report "Peer '<name>' is
  unreachable ... queued for retry" and spool the item under `~/.herald/queue/`.
- Queued items deliver on the next successful contact with that peer, are
  retried by the sender's daemon in the background, or can be pushed with the
  new `herald flush [peer]` command.
- Sends the peer actively rejects (bad token/URL) error out and are not queued.
- Added `herald --version` and a version line in the daemon startup banner.
