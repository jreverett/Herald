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

`python3 tests/run_tests.py` also rejects a run where no tests execute. GitHub
Actions runs it on Linux and macOS for pushes and pull requests.
The two tests for real harness-process attribution require Linux `/proc` and
skip on macOS. Ownership, delivery, queue, and portable lease tests run on both.

`tests/test_ownership.py` covers absent recipients, cross-mailbox listeners,
inspection without file writes, explicit takeover, returned work, stale queued
responses, recipient-aware warnings, legacy records, and queue diagnostics.
The protocol suite also reproduces the two-session reply failure with real
daemons and checks simultaneous claims and the queued-to-replied lifecycle.

On Windows, run `powershell -NoProfile -File tests/test_tray.ps1`. The test loads
the actual tray functions and builds Windows Forms menus with fake CLI responses.
It checks empty, failed, single-item and truncated lists, recipient labels,
blocked rows, preview clicks, and command arguments. It does not start a tray
icon or contact a real Herald store. GitHub Actions runs this test on Windows.
