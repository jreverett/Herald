# Changelog

Versioning is `0.MAJOR.MINOR` while pre-1.0. `herald --version` prints the running version.

## 0.9.5

- **The installer now wires the tray's activity hooks itself**, for every Claude
  Code and Codex profile it finds, so a fresh install gets the amber state
  without anyone reading the README and editing JSON. Codex uses the same event
  names and the same payload fields as Claude Code, so `herald activity` needed
  no change to work there; only the wiring was missing.
- Merged, never overwritten: `~/.claude/settings.json` and `~/.codex/hooks.json`
  are shared with whatever else the user has hooked up, and on one machine here
  the Codex file already held three Hindsight hooks. Entries are matched by their
  command, so re-running the installer adds nothing and changes nothing.
- Codex only reads `hooks.json` when `[features] hooks = true`, so the installer
  sets that in `config.toml` if it is missing. Codex then skips any hook it has
  not been trusted with, silently, recording the trust as a hash per hook in
  `config.toml` - observed here, where a `codex exec` run executed the three
  already-trusted hooks and ignored the four new ones without a word. The
  installer says to start Codex once and approve the prompt; nothing pre-trusts a
  hook on the user's behalf.
- `Notification` is deliberately not wired. It fires for a session sitting at an
  empty prompt as well as for a permission prompt, and neither is herald's work.
  Codex does not emit it at all.
- The hook command is the absolute path to the `herald` wrapper where one exists,
  because a hook does not run through a login shell and cannot assume PATH.

## 0.9.4

- **The tray icon turns red when herald itself is waiting on you**, drawn as
  converging chevrons so the state does not rest on colour alone. With several
  tabs listening, an approval a peer's agent is waiting on was invisible until
  you happened to look at the right one.
- Red is read from the inbox, never from the harness. A session is reused for all
  sorts of work, so a permission prompt in an unrelated turn is not herald
  waiting on you, and Claude Code's `Notification` hook cannot tell the two
  apart. Two inbox conditions raise it: a task this side answered
  `--status accepted`, which promises an answer once the human decides, and an
  item on a mailbox no listener is attached to, which will sit unread until
  someone looks. Both work for Codex and Copilot too, since neither needs a hook.
- `herald result --status accepted` now records that status on the source item, so
  the daemon can tell an acknowledgement that is waiting on a human from one that
  is merely work in progress.
- Traffic overrides both resting states: an arrow is a four-second flash over
  whatever the icon was showing, and the resting state returns when it passes.
- **Fixed: the tooltip froze on "herald: starting..." as soon as any state added
  detail to it.** `NotifyIcon.Text` throws above 63 characters - verified on
  Windows PowerShell 5.1, where 63 assigns and 64 throws - and the script's own
  guard allowed 127. The throw aborted the poll before the text was set, on every
  sixth tick, while the icon kept updating from the other five, so the icon was
  right and the tooltip was stale. It is now assembled in priority order within
  the limit, dropping what does not fit, and the assignment is guarded.
- **Amber is scoped to herald work too.** The hooks fire in every session, so it
  used to light up whenever any tab took a tool call, whatever that tab was doing.
  A turn now counts only while its harness also holds a claimed inbox item it has
  not answered. Neither half is sufficient on its own: a marker alone reports any
  busy session, and a claim alone stays lit while the agent waits on its human.
- The two signals are keyed differently - a hook knows the editor's session id, a
  claim knows `HERALD_AGENT` - so they are joined on the harness pid, which both
  find by the same walk up from their own process. The listener records it,
  because the listener exits after one item while the harness above it stays and
  does the work. The remaining false positive is bounded: while a tab owes herald
  a reply, any turn in that tab reads as herald's work, since a turn cannot be
  attributed to a topic.
- `herald status` and `herald activity` report all three states.

## 0.9.3

- **The tray icon now shows when an agent is actually working**, as a slow amber
  breath on the idle shape. With several tabs listening there was no way to tell
  a session that was mid-turn from one that had finished and gone quiet, so the
  only signals were the send and receive arrows, each a 4-second flash.
- The signal is deliberately *not* a claimed inbox item. An item stays `active`
  from `herald read` until its reply, which includes the whole time an agent sits
  waiting for its human to answer a question - lighting the icon for that would
  report work that is not happening. `herald activity working|idle` records a
  running turn instead, `herald activity` reports what is running, and the daemon
  republishes the count and the tab names in `status.json` as `working` and
  `working_agents`.
- Setting a state prints nothing. Claude Code feeds hook stdout back to the model
  on `PostToolUse` and `UserPromptSubmit`, so anything written there would cost
  tokens on every tool call.
- Wire it to the harness rather than to the agent: `PostToolUse` and
  `UserPromptSubmit` stamp `working`, and `Stop`, `Notification` and `SessionEnd`
  clear it. `Stop` firing as the agent hands back is what makes "waiting on the
  human" read as idle. `SubagentStop` must not clear it - a subagent finishing
  does not end the parent turn.
- **A marker is held against the session's own liveness, not a short timer.** Only
  a tool call refreshes the stamp, so a turn that thought for more than 90 seconds
  without calling one used to decay and read as idle mid-work. The stamp now
  records the harness that ran the hook, as its pid **and** its start time, since a
  pid on its own is reused and an unrelated process on a recycled number would
  otherwise read as the original session. A dead session's marker goes immediately,
  because the clear it owes will never arrive; a live one whose start time still
  matches holds for 10 minutes. Start times count from boot, so the boot id is
  recorded too and a marker from an earlier boot gets only the short lease. The long lease is still bounded so that a clear lost to a broken
  hook cannot pin the signal on for a whole session.
- Rejected, with the measurement: driving the signal from the transcript file's
  mtime. A peer session reasoned for over two minutes with no tool call and its
  `.jsonl` took no model output at all in that window - the only writes were queue
  bookkeeping - so mtime tracks message boundaries, which is the granularity the
  stamps already have. It also fails in the wrong direction: a session that
  crashed seconds ago has a fresh mtime and would read as alive.
- Traffic still wins over the breath: an arrow is a moment, the breath is a state.

## 0.9.2

- **The tray icon's right-click menu now lists the inbox**, with **Close
  (reversible)** and **Delete permanently...** on each item, so clearing debris
  while debugging routing no longer needs a terminal. The list is built when the
  menu opens rather than on the animation tick, so it costs one call per
  right-click. Each action passes the item's own mailbox, because the default lane
  does not match an item that arrived on another one.
- `herald inbox --json` prints one object per item, and `[]` when empty. The tray
  reads this rather than the inbox directory: whether an item is open, and which
  mailbox and agent it belongs to, are herald's rules, and a second copy of them
  in the menu would drift and then mislead during exactly the debugging the menu
  is for.
- `herald rm <id>` deletes an item and its attachments outright. Unlike `close` it
  keeps no history, so the item leaves `herald thread`, `herald reply` can no
  longer answer it, and a delivery the sender is still retrying can arrive again
  as a new item. It refuses an item held by another live session unless `--force`
  is passed, so a menu click cannot pull work out from under a tab mid-task.

## 0.9.1

- **A second tab joining a mailbox that already had work in flight crashed on its
  first poll.** `herald wait` printed "Listening alongside ..." and then died with
  `KeyError: 'generation'`. Only a mailbox owner is given a generation, and the
  re-presentation check read it with a bare subscript, so a co-listener reached it
  with no key. The check is only reachable when an item is already active, which is
  why 0.9.0's tests did not see it. A listener now starts at generation 0, meaning
  it holds no mailbox generation, and only ownership raises it (owners count from
  1). Skipping the check for a co-listener was the other candidate and is wrong:
  the check is also what stops an item the listener already holds coming back on
  its next wait, so skipping it re-presents the same work on every poll.
- The test suite no longer rings the terminal bell. `ring_bell` walks up the
  process tree for the human's terminal, which it found from every test
  subprocess, so a run beeped whoever started it once per delivery. The harness
  now points `HERALD_BELL_TTY` at a file under the test's own directory.

## 0.9.0

- **Several tabs can now listen on one mailbox at the same time.** Herald assumed
  one person runs one agent session at a time. Two tabs on different topics broke
  that in two ways: an item addressed to a specific agent was handed to whichever
  session owned the mailbox, so each tab kept receiving the other's work; and
  starting a listener evicted the existing one, so they could not both listen
  anyway. An agent name is now an address - a live listener under that name gets
  the item wherever it is listening, and only untargeted work goes to the mailbox
  owner. A different name coexists and is told so on stderr; the same name
  returning reclaims its mailbox, as a restarted tab should; `herald resume`
  remains the explicit takeover. A single listener is unaffected.
- The ownership decision and claim happen inside one lock. Reading the owner
  first and writing after left a window where two tabs starting together both saw
  no owner and both claimed the mailbox.
- The test suite is deterministic. It previously failed about one run in three on
  untouched code, so a red run said nothing. Three races were fixed: eight places
  slept a fixed second and assumed a listener had started; the daemon startup
  budget was 8s, too tight under load and failing tests in `setUp`; and
  `wait_for_inbox` returned when an item file existed, which is before the daemon
  had routed it. Eight tests were added for the listener lifecycle, covering all
  four practical transitions between one listener and several.

## 0.8.5

- Taking a mailbox off a listener that is still alive now says so on stderr,
  naming the displaced agent and its heartbeat age. Takeover itself is unchanged
  and still the mechanism behind a provider handoff - the problem was that it was
  silent. A session that displaced a live one then received that session's mail
  with no indication, and mailbox-delivered items carry a `to_agent` label while
  `targeted` is false, so they arrive looking like ordinary work addressed to the
  new listener. Observed three times in one morning on a shared `main` mailbox;
  each item concerned a workstream the receiving session had never touched, and
  only the receiving agent declining to guess kept a fabricated answer out.
- `SKILL.md`: check `herald sessions` for a live owner before taking a mailbox,
  use a separate mailbox to stay reachable alongside another session, and never
  answer an item that is not this session's work.

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
